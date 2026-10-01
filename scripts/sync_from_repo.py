#!/usr/bin/env python3
"""Import reviewed glp-business/main source into the live business workspace.

Checks every curated file against the prior imported byte hash before replacing it.
Never touches non-curated files, runtime state, reports, or the site tree.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

REMOTE = "git@github.com:izaiahat/glp-business.git"
DEFAULT_ROOT = Path("/home/ubuntu/business")
STATE = "ops/.rebuild-sync-state.json"  # deliberately excluded from Git
MANIFEST = "ops/rebuild-source-manifest.json"
EPILOG = """\
workflow: always preview first, then apply.
  1. python3 scripts/sync_from_repo.py --dry-run
     Prints DRY_RUN diffs=N and the paths that would change; writes nothing.
     Exit 2 (REFUSED) means local edits conflict; resolve them before applying.
  2. python3 scripts/sync_from_repo.py --apply
     Rechecks every hash and writes only the paths the dry run reported.
For a preexisting curated file absent from sync state, explicitly pass
--adopt-reviewed-baseline PATH on BOTH commands. Its live bytes must match an
older committed revision of that path in the reviewed checkout; arbitrary
local edits and files with an existing baseline still refuse.
"""


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


def reviewed_prior_hashes(checkout, rel):
    """Hashes of older committed versions, never working-tree or current HEAD bytes."""
    head_hash = hashlib.sha256(subprocess.check_output(
        ["git", "-C", str(checkout), "show", f"HEAD:{rel}"]
    )).hexdigest()
    # Default path history prunes side branches at TREESAME merges.
    commits = subprocess.check_output(
        ["git", "-C", str(checkout), "log", "--full-history", "--format=%H", "HEAD", "--", rel],
        text=True,
    ).splitlines()
    return {
        hashlib.sha256(subprocess.check_output(
            ["git", "-C", str(checkout), "show", f"{commit}:{rel}"]
        )).hexdigest()
        for commit in commits
    } - {head_hash}


@contextmanager
def source_lock(root):
    """Serialize maintained imports/cutover before their first source read.

    The stable workspace directory inode is the lock: dry-run creates nothing.
    This does not retrofit an already-loaded old importer; drain those under the
    existing single-writer lane before adopting this version. Do not rename the
    workspace or bypass sync() with _sync_locked(). Not a hostile-writer fence.
    """
    root = Path(root).resolve(strict=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('source_import_busy') from exc
        held, current = os.fstat(fd), root.stat()
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError('source_import_root_moved')
        yield
    finally:
        os.close(fd)


def sync(root, checkout, apply, adopt_reviewed_baseline=(), only=()):
    with source_lock(root):
        return _sync_locked(root, checkout, apply, adopt_reviewed_baseline, only)


def _sync_locked(root, checkout, apply, adopt_reviewed_baseline=(), only=()):
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
    selected = set(only)
    if len(selected) != len(tuple(only)) or not selected <= set(files):
        raise ValueError("--only requires unique curated paths (not the global manifest)")
    # A partial import must not publish the candidate global manifest as installed.
    # Keep unselected state baselines intact for a later full import.
    target_hashes = ({p: target_hashes[p] for p in selected} if selected else target_hashes)
    if not selected:
        target_hashes[MANIFEST] = sha(checkout / MANIFEST)
    state_path = root / STATE
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    previous = state.get("hashes", {})
    if not isinstance(previous, dict):
        raise ValueError("invalid sync state")
    adopted = set(adopt_reviewed_baseline)
    if len(adopted) != len(tuple(adopt_reviewed_baseline)) or any(
        p not in target_hashes or p in previous or p == MANIFEST for p in adopted
    ):
        raise ValueError("adoption requires a unique curated path selected and absent from sync state")
    prior_hashes = {p: reviewed_prior_hashes(checkout, p) for p in adopted}
    stale = sorted(set(previous) - set(target_hashes)) if not selected else []
    changed, conflicts, retirement_moves, expected, expected_modes = [], [], {}, {}, {}
    for p in sorted(set(target_hashes) | set(stale)):
        local = safe(root, p)
        current = sha(local)
        expected[p] = current
        if selected and current is not None:
            expected_modes[p] = stat.S_IMODE(local.stat().st_mode)
        target = target_hashes.get(p)
        parts = Path(p).parts
        # Only a full import may retire (move) a live original; a scoped import
        # never moves or deletes an unselected path and copies the archive instead.
        retirement_source = (safe(root, str(Path(parts[0]) / parts[2]))
                             if not selected and len(parts) == 3 and parts[0] in {"scripts", "tests"}
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
        elif current is None or (p in adopted and current in prior_hashes[p]):
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
                    # Scoped replacements retain the destination's preflight mode;
                    # a newly created destination still takes the checkout mode.
                    os.chmod(tmp, expected_modes[p] if p in expected_modes else src.stat().st_mode & 0o777)
                    # Serialized writers are still required: this detects intervening
                    # ordinary edits, not an atomic compare-and-swap against outsiders.
                    if sha(dst) != expected[p]:
                        raise ValueError("destination changed during sync: " + p)
                    if p in expected_modes and stat.S_IMODE(dst.stat().st_mode) != expected_modes[p]:
                        raise ValueError("destination mode changed during sync: " + p)
                    os.replace(tmp, dst)
                finally:
                    if os.path.exists(tmp): os.unlink(tmp)
            elif dst.exists():
                dst.unlink()
        state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".sync-state-", dir=state_path.parent)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"schema": 1, "hashes": {**previous, **target_hashes} if selected else target_hashes}, f, indent=2, sort_keys=True)
                f.write("\n"); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, state_path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Import reviewed glp-business/main source into the live business workspace.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--checkout", type=Path, help="local reviewed Git checkout for offline verification")
    parser.add_argument("--adopt-reviewed-baseline", action="append", default=[], metavar="PATH",
                        help="adopt a preexisting file only if bytes match a prior committed revision")
    parser.add_argument("--only", action="append", default=[], metavar="PATH",
                        help="import only named curated paths; retain other baselines and installed global manifest")
    args = parser.parse_args()
    if args.apply == args.dry_run:
        parser.error("choose exactly one of --dry-run or --apply")
    if args.checkout:
        return sync(args.root, args.checkout.resolve(strict=True), args.apply, args.adopt_reviewed_baseline, args.only)
    with tempfile.TemporaryDirectory(prefix="glp-business-sync-") as temp:
        subprocess.run(["git", "clone", "--quiet", "--branch", "main", REMOTE, temp + "/repo"], check=True)
        return sync(args.root, Path(temp) / "repo", args.apply, args.adopt_reviewed_baseline, args.only)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"SYNC_REFUSED: {exc}", file=sys.stderr)
        sys.exit(2)
