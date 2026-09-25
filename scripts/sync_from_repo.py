#!/usr/bin/env python3
"""Import reviewed glp-business/main source into the live business workspace.

Checks every curated file against the prior imported byte hash before replacing it.
Never touches non-curated files, runtime state, reports, or the site tree.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

REMOTE = "git@github.com:izaiahat/glp-business.git"
DEFAULT_ROOT = Path("/home/ubuntu/business")
STATE = "ops/.rebuild-sync-state.json"  # deliberately excluded from Git
MANIFEST = "ops/rebuild-source-manifest.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def safe(root, rel):
    p = Path(rel)
    if p.is_absolute() or not p.parts or any(part in ("..", ".") for part in p.parts):
        raise ValueError("unsafe manifest path")
    q = root / p
    if q.is_symlink() or any(parent.is_symlink() for parent in list(q.parents)[:-1] if parent != root and root in parent.parents):
        raise ValueError("symlink in curated path")
    return q


def sync(root, checkout, apply):
    root = root.resolve(strict=True)
    manifest = json.loads((checkout / MANIFEST).read_text())
    if manifest.get("schema") != "GLP_BUSINESS_SOURCE_V1":
        raise ValueError("unknown source manifest schema")
    files = manifest["files"]
    if not isinstance(files, dict) or MANIFEST in files:
        raise ValueError("invalid manifest")
    listed = set(subprocess.check_output(["git", "-C", str(checkout), "ls-files", "-z"]).decode().split("\0")) - {""}
    if listed != set(files) | {MANIFEST} or len(listed) != manifest["tracked_count"]:
        raise ValueError("tracked checkout and allowlist differ")
    target_hashes = {p: sha(safe(checkout, p)) for p in files}
    for p, info in files.items():
        if info["sha256"] != target_hashes[p]:
            raise ValueError("manifest source hash mismatch: " + p)
    new_manifest_hash = sha(checkout / MANIFEST)
    target_hashes[MANIFEST] = new_manifest_hash
    state_path = root / STATE
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    previous = state.get("hashes", {})
    if not isinstance(previous, dict):
        raise ValueError("invalid sync state")
    stale = sorted(set(previous) - set(target_hashes))
    changed, conflicts, retirement_moves = [], [], {}
    for p in sorted(set(target_hashes) | set(stale)):
        local = safe(root, p)
        current = sha(local)
        target = target_hashes.get(p)
        parts = Path(p).parts
        retirement_source = (safe(root, str(Path(parts[0]) / parts[2]))
                             if len(parts) == 3 and parts[0] in {"scripts", "tests"}
                             and parts[1] == ".retired-20260925" else None)
        if retirement_source is not None and retirement_source.is_file():
            if sha(retirement_source) != target or (current is not None and current != target):
                conflicts.append(str(retirement_source.relative_to(root)))
                continue
            retirement_moves[p] = retirement_source
            changed.append(p)
            continue
        if current == target:
            continue
        baseline = previous.get(p)
        if p in previous:
            if current != baseline:
                conflicts.append(p)
            else:
                changed.append(p)
        elif current is None:
            changed.append(p)
        else:
            conflicts.append(p)
    if conflicts:
        print("REFUSED local edits/unrecognized curated paths:", ", ".join(conflicts), file=sys.stderr)
        return 2
    print(f"{'APPLY' if apply else 'DRY_RUN'} diffs={len(changed)} conflicts=0 tracked={len(listed)}")
    if changed:
        print("changes:", ", ".join(changed))
    if apply:
        for p in changed:
            dst = safe(root, p)
            if p in retirement_moves:
                src = retirement_moves[p]
                # Recheck immediately before the move; an intervening editor must
                # not be replaced even if the earlier preflight matched.
                if sha(src) != target_hashes[p] or (sha(dst) not in (None, target_hashes[p])):
                    raise ValueError("retirement source changed during sync: " + p)
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)
            elif p in target_hashes:
                src = safe(checkout, p)
                dst.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".sync-", dir=dst.parent)
                try:
                    with os.fdopen(fd, "wb") as f:
                        f.write(src.read_bytes())
                        f.flush(); os.fsync(f.fileno())
                    os.chmod(tmp, src.stat().st_mode & 0o777)
                    os.replace(tmp, dst)
                finally:
                    if os.path.exists(tmp): os.unlink(tmp)
            elif dst.exists():
                dst.unlink()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".sync-state-", dir=state_path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"schema": 1, "hashes": target_hashes}, f, indent=2, sort_keys=True)
                f.write("\n"); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, state_path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--checkout", type=Path, help="local reviewed Git checkout for offline verification")
    args = parser.parse_args()
    if args.apply == args.dry_run:
        parser.error("choose exactly one of --dry-run or --apply")
    if args.checkout:
        return sync(args.root, args.checkout.resolve(strict=True), args.apply)
    with tempfile.TemporaryDirectory(prefix="glp-business-sync-") as temp:
        subprocess.run(["git", "clone", "--quiet", "--depth", "1", "--branch", "main", REMOTE, temp + "/repo"], check=True)
        return sync(args.root, Path(temp) / "repo", args.apply)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"SYNC_REFUSED: {exc}", file=sys.stderr)
        sys.exit(2)
