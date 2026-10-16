#!/usr/bin/env python3
"""Deterministic host-disk retention and conservative cleanup.

The only Git worktree deletion path is ``git worktree remove`` after a clean,
remote-reachable commit and fresh all-UID process-reference check. The script
never deletes protected business/runtime data; regenerable rules are explicit,
age-checked, no-follow and bounded.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, "/home/ubuntu/business/ops")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from discord_fmt import meaning, render, title  # noqa: E402
from disk_retention import AMBER_FREE, RED_FREE, growth_sensor, run_extensions, protected
from bounded_pruner import ScanBudget, tree_stats

GIB = 1024 ** 3
REPO = Path("/home/ubuntu/.hermes/hermes-agent")
SANDBOX_ROOT = Path("/home/ubuntu/sandboxes")
REHEARSAL_PREFIX = "hermes-update-rehearsal-"
REPAIR_REPORT_ROOT = Path("/home/ubuntu/business/reports/hermes-update-repair")
REPORT_ROOT = Path("/home/ubuntu/business/reports/host-disk-prune")
LOCK_PATH = Path("/home/ubuntu/.hermes/runstate/host-disk-pruner.lock")
MAINTENANCE_STATE_PATH = Path("/home/ubuntu/.hermes/runstate/host-disk-pruner-state.json")
DEFAULT_MAX_COUNT = 2
DEFAULT_MAX_BYTES = 4 * GIB
DEFAULT_TRIGGER_FREE = RED_FREE
HARD_ALERT_FREE = RED_FREE
DEFAULT_MAINTENANCE_INTERVAL_HOURS = 168
DEFAULT_NPM_VERIFY_INTERVAL_HOURS = 168
DEFAULT_TMP_MIN_AGE_HOURS = 336
DEFAULT_NPX_MIN_AGE_HOURS = 720
JOURNAL_VACUUM_THRESHOLD = 256 * 1024 ** 2
BUILDER_TOKENS = ("wrangler", "npm", "npx", "claude")
PROTECTED_PATHS = (
    Path('/home/ubuntu/.hermes/secrets'),
    Path('/home/ubuntu/.hermes/state'),
    Path('/home/ubuntu/business/.worktrees'),
    Path("/home/ubuntu/.hermes/state.db"),
    Path("/home/ubuntu/.hermes/checkpoints"),
    Path("/home/ubuntu/business/.enzyme"),
    Path("/home/ubuntu/.hermes/profiles"),
    REPO,
    Path("/home/ubuntu/business/reports"),
    Path("/home/ubuntu/.hermes/browser-profiles"),
    Path("/swapfile"),
)
NPM_CACHE = Path("/home/ubuntu/.npm/_cacache")
NPX_CACHE = Path("/home/ubuntu/.npm/_npx")
UV_CACHE = Path("/home/ubuntu/.cache/uv")
JOURNAL_ROOT = Path("/var/log/journal")
UV_BIN = "/home/ubuntu/.local/bin/uv"
NPM_BIN = "/home/ubuntu/.local/bin/npm"
SAFE_TMP_EXACT = {
    "amazon-cloudwatch-agent.deb",
    "aws",
    "awscliv2.zip",
}
SAFE_TMP_PREFIXES: tuple[str, ...] = ("glp-playwright-",)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enzyme_vault_usage(root: Path = Path('/home/ubuntu/business/.enzyme')) -> dict[str, Any]:
    """Metadata only. Missing/racing storage is UNKNOWN, never a zero-sized vault."""
    try:
        if root.is_symlink() or root.resolve() != root.absolute() or not root.is_dir():
            raise ValueError('vault_path')
        logical = allocated = files = targets = 0
        seen = set()
        for path in root.rglob('*'):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError('vault_symlink')
            if not stat.S_ISREG(info.st_mode):
                continue
            logical += info.st_size
            files += 1
            identity = (info.st_dev, info.st_ino)
            if identity not in seen:
                allocated += info.st_blocks * 512
                seen.add(identity)
            targets += int(path.parent == root / 'targets' and path.suffix == '.db')
        return {'ok': True, 'path': str(root), 'logical_bytes': logical,
                'allocated_bytes': allocated, 'regular_files': files,
                'target_databases': targets, 'primary_db_bytes': (root / 'enzyme.db').stat().st_size,
                'method': 'lstat_no_symlinks_unique_inode_allocated_bytes'}
    except (OSError, ValueError) as exc:
        return {'ok': False, 'path': str(root), 'status': 'ENZYME_VAULT_UNKNOWN',
                'error_type': type(exc).__name__}


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


_RUN_DEADLINE = None


def remaining_budget(limit):
    if _RUN_DEADLINE is None: return float(limit)
    return max(0.01,min(float(limit),_RUN_DEADLINE-time.monotonic()))


def run(cmd: list[str], *, cwd: Path = REPO, timeout: int = 300, env: dict[str, str] | None = None) -> dict[str, Any]:
    timeout = remaining_budget(min(timeout,60))
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
            env=merged_env,
        )
        return {
            "cmd": cmd,
            "returncode": completed.returncode,
            "stdout_tail": (completed.stdout or "")[-4000:],
            "stderr_tail": (completed.stderr or "")[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "cmd": cmd,
            "returncode": 124,
            "status": "TIME_BUDGET_PARTIAL",
            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
            "stderr_tail": "timeout",
        }


def allocated_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        proc = subprocess.run(
            ["du", "-sx", "--block-size=1", str(path)],
            text=True, capture_output=True, timeout=remaining_budget(10),
        )
    except subprocess.TimeoutExpired:
        return 0  # Unknown size cannot authorize a cache deletion.
    if proc.returncode != 0 or not proc.stdout.strip():
        return 0
    return int(proc.stdout.split()[0])


def is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def protected_delete_target(path: Path) -> bool:
    resolved = path.resolve(strict=False)
    return any(resolved == root.resolve(strict=False) or is_under(resolved, root) for root in PROTECTED_PATHS)


def process_inventory() -> list[dict[str, Any]]:
    """Collect argv/env/cwd AND fd/maps references once per inventory, not per file."""
    rows: list[dict[str, Any]] = []
    for proc_dir in Path('/proc').iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            cmdline = (proc_dir/'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace')
            cwd = os.readlink(proc_dir/'cwd')
            environ = (proc_dir/'environ').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace')
            refs = {cwd}
            for name in ('cwd', 'exe', 'root'):
                try:
                    refs.add(os.readlink(proc_dir/name))
                except OSError:
                    pass
            try:
                with os.scandir(proc_dir/'fd') as fds:
                    for fd in fds:
                        try:
                            refs.add(os.readlink(fd.path))
                        except OSError:
                            pass
            except OSError:
                pass
            try:
                for line in (proc_dir/'maps').read_text(errors='ignore').splitlines():
                    fields = line.split(maxsplit=5)
                    if len(fields) == 6 and fields[5].startswith('/'):
                        refs.add(fields[5])
            except OSError:
                pass
            rows.append({'pid': int(proc_dir.name), 'cmdline': cmdline,
                         'cwd': cwd, 'environ': environ, '_raw_refs': refs})
        except OSError:
            continue
    # Resolve each unique reference just ONCE, preserving the old symlink semantics.
    normalized = {}
    for row in rows:
        row['_refs'] = []
        for ref in row.pop('_raw_refs'):
            if not ref.startswith('/'):
                continue
            if ref not in normalized:
                try:
                    normalized[ref] = str(Path(ref.removesuffix(' (deleted)')).resolve(strict=False))
                except (OSError, ValueError):
                    normalized[ref] = ref.removesuffix(' (deleted)')
            row['_refs'].append(normalized[ref])
    return rows


def active_builders(processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    own_pid = os.getpid()
    for row in processes:
        if row["pid"] == own_pid:
            continue
        hay = row["cmdline"].lower()
        if any(token in hay for token in BUILDER_TOKENS):
            out.append({"pid": row["pid"], "command": hay.split()[0] if hay.split() else 'unknown'})
    return out


def _reference_is_under(reference: str | Path, path: Path) -> bool:
    try:
        return is_under(Path(str(reference).removesuffix(" (deleted)")), path)
    except (OSError, ValueError):
        return False


def path_is_active(path: Path, processes: list[dict[str, Any]]) -> bool:
    """Fail closed on argv/env/cwd plus live /proc executable/root/fd/maps references."""
    path_s = str(path.resolve(strict=False))
    for row in processes:
        if '_refs' in row:
            if (any(ref == path_s or ref.startswith(path_s.rstrip('/')+'/') for ref in row['_refs'])
                    or path_s in row.get('cmdline', '') or path_s in row.get('environ', '')):
                return True
            continue
        if _reference_is_under(row.get("cwd", "/"), path):
            return True
        if path_s in row.get("cmdline", "") or path_s in row.get("environ", ""):
            return True
        proc = Path("/proc") / str(row.get("pid"))
        for name in ("cwd", "exe", "root"):
            try:
                if _reference_is_under(os.readlink(proc / name), path):
                    return True
            except (FileNotFoundError, PermissionError, OSError):
                pass
        try:
            for fd in (proc / "fd").iterdir():
                try:
                    if _reference_is_under(os.readlink(fd), path):
                        return True
                except (FileNotFoundError, PermissionError, OSError):
                    continue
        except (FileNotFoundError, PermissionError, OSError):
            pass
        try:
            for line in (proc / "maps").read_text(errors="ignore").splitlines():
                fields = line.split(maxsplit=5)
                if len(fields) == 6 and fields[5].startswith("/") and _reference_is_under(fields[5], path):
                    return True
        except (FileNotFoundError, PermissionError, OSError):
            pass
    return False


def path_identity(path: Path) -> tuple[int, int, int, int, int, int] | None:
    try:
        stat = path.lstat()
        return stat.st_dev, stat.st_ino, stat.st_mode, stat.st_uid, stat.st_ctime_ns, stat.st_size
    except (FileNotFoundError, PermissionError, OSError):
        return None


def path_is_active_privileged(path: Path) -> bool:
    """Fresh all-UID veto; UNKNOWN/permission errors are active, never deletion."""
    command=['sudo','-n','/usr/bin/python3','-I',str(Path(__file__).parent/'pruner_process_reference_guard.py'),
             str(path),str(os.getpid())]
    result=run(command,cwd=Path('/'),timeout=3)
    return result['returncode']!=0 or result['stdout_tail'].strip()!='INACTIVE'


def newest_descendant_mtime(path: Path, *, cutoff=None, deadline=None) -> float:
    # Young entries return immediately; no per-file Path.resolve or second du walk.
    # Without an age cutoff this retains the old exact-newest API.
    try:
        newest, _ = tree_stats(path, cutoff=cutoff,
                               deadline=deadline or time.monotonic()+20,
                               protected=protected)
        return newest
    except ScanBudget:
        return float('inf')


def revalidate_candidate(
    path: Path,
    expected_identity: tuple[int, int, int, int, int, int],
    *,
    require_directory: bool,
) -> tuple[bool, str]:
    current = path_identity(path)
    if current != expected_identity:
        return False, "identity_changed"
    if path.is_symlink():
        return False, "symlink_refused"
    if require_directory and not path.is_dir():
        return False, "not_real_directory"
    if not require_directory and not (path.is_dir() or path.is_file()):
        return False, "unsupported_type"
    if path_is_active_privileged(path):
        return False, "active_or_unknown_all_uid_reference"
    if path_identity(path)!=expected_identity:
        return False,"identity_changed_during_reference_scan"
    return True, "ok"


def load_maintenance_state() -> dict[str, Any]:
    try:
        data = json.loads(MAINTENANCE_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def write_maintenance_state(data: dict[str, Any]) -> None:
    MAINTENANCE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = MAINTENANCE_STATE_PATH.with_name(MAINTENANCE_STATE_PATH.name + f".tmp-{os.getpid()}")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    os.replace(temp, MAINTENANCE_STATE_PATH)


def action_due(state: dict[str, Any], key: str, interval_hours: float, now: float) -> bool:
    try:
        last = float(state.get(key) or 0)
    except (TypeError, ValueError):
        last = 0
    return now - last >= interval_hours * 3600


def command_maintenance(
    name: str,
    cmd: list[str],
    measured_paths: tuple[Path, ...],
    *,
    dry_run: bool,
    timeout: int = 600,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    before = sum(allocated_bytes(path) for path in measured_paths)
    if dry_run:
        return {
            "name": name,
            "ok": True,
            "dry_run": True,
            "observed_bytes": before,
            "eligible_bytes": 0,
            "bytes_freed": 0,
            "cmd": cmd,
        }
    result = run(cmd, cwd=Path("/"), timeout=timeout, env=env)
    after = sum(allocated_bytes(path) for path in measured_paths)
    return {
        "name": name,
        "ok": result["returncode"] == 0,
        "bytes_before": before,
        "bytes_after": after,
        "bytes_freed": max(0, before - after),
        "result": result,
    }


@dataclass
class WorktreeRecord:
    path: str
    branch: str | None
    head: str | None
    allocated_bytes: int
    mtime: float
    active: bool
    verified: bool


def parse_worktrees(text: str, processes: list[dict[str, Any]]) -> list[WorktreeRecord]:
    blocks = [b for b in text.strip().split("\n\n") if b.strip()]
    records: list[WorktreeRecord] = []
    for block in blocks:
        data: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(" ")
            data[key] = value
        path = Path(data.get("worktree", ""))
        if not path.name.startswith(REHEARSAL_PREFIX):
            continue
        path_s = str(path)
        active = any(
            is_under(Path(row["cwd"]), path)
            or path_s in row["cmdline"]
            or path_s in row["environ"]
            for row in processes
        )
        repair_id = path.name[len(REHEARSAL_PREFIX):]
        verified = False
        for status_path in (
            REPAIR_REPORT_ROOT / repair_id / "status.json",
            Path("/home/ubuntu/business/reports/hermes-update-protocol-logs") / repair_id / "auto-apply-summary.json",
        ):
            if not status_path.exists():
                continue
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
                states = {
                    str(payload.get("status") or "").lower(),
                    str(payload.get("rehearsal_summary_status") or "").lower(),
                    str(payload.get("auto_apply_status") or "").lower(),
                }
                if states & {"rehearsal_pass", "success", "no_update"}:
                    verified = True
            except Exception:
                pass
        records.append(
            WorktreeRecord(
                path=path_s,
                branch=(data.get("branch") or "").removeprefix("refs/heads/") or None,
                head=data.get("HEAD"),
                allocated_bytes=allocated_bytes(path),
                mtime=path.stat().st_mtime if path.exists() else 0.0,
                active=active,
                verified=verified,
            )
        )
    return records


def choose_removals(
    records: list[WorktreeRecord],
    *,
    max_count: int,
    max_bytes: int,
    explicit_keep: set[str] | None = None,
) -> tuple[list[WorktreeRecord], set[str]]:
    """Choose oldest removable trees while retaining active/latest evidence."""
    keep = set(explicit_keep or set())
    for record in records:
        if record.active:
            keep.add(record.path)
    verified = [r for r in records if r.verified]
    newest = max(verified or records, key=lambda r: r.mtime, default=None)
    if newest:
        keep.add(newest.path)

    remaining = list(records)
    removals: list[WorktreeRecord] = []
    for record in sorted(records, key=lambda r: r.mtime):
        over_count = len(remaining) > max_count
        over_bytes = sum(r.allocated_bytes for r in remaining) > max_bytes
        if not (over_count or over_bytes):
            break
        if record.path in keep:
            continue
        removals.append(record)
        remaining.remove(record)
    return removals, keep


def remove_worktree(record: WorktreeRecord, *, dry_run: bool) -> dict[str, Any]:
    # Tracked content is a hard exclusion in the current operator authorization.
    return {'path': record.path, 'ok': True,
            'skipped': 'tracked_worktree_requires_operator_decision', 'bytes_freed': 0}


def remove_disposable(path: Path, *, deadline=None, as_root=False) -> bool:
    """Delete one already-validated allowlisted object, bounded, no shell/globs.

    A subprocess timeout is unknown/partial and earns no removal credit; no
    detached worker survives. This legacy pathname API does NOT provide the
    expected-member/reference exclusion contract required for GLP-354 acceptance.
    The child only uses shutil's symlink-safe rmtree.
    """
    # Absence is not this invocation's retirement. In particular, an original
    # renamed away between admission and this helper must never earn byte credit.
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    timeout = remaining_budget(20 if deadline is None else min(20, max(0.01, deadline-time.monotonic())))
    command = [sys.executable, '-I', '-c',
               'import pathlib,shutil,sys; p=pathlib.Path(sys.argv[1]); '
               'shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()',
               str(path)]
    if as_root:
        if path.parent not in {Path('/var/log/atop'),Path('/var/log/nginx')} or not path.is_file() or path.is_symlink():
            return False
        command = ['sudo','-n','/usr/bin/python3',*command[1:]]
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout)
        return result.returncode == 0 and not path.exists()
    except subprocess.TimeoutExpired:
        # Killed/reaped transport plus an absent pathname does not prove our
        # child retired the admitted inodes. Preserve unknown accounting as zero.
        return False


def safe_remove_cache(path: Path, *, dry_run: bool, known_bytes=None, deadline=None) -> dict[str, Any]:
    if protected_delete_target(path) or protected(path):
        return {"path": str(path), "ok": False, "reason": "protected path refusal"}
    before = allocated_bytes(path) if known_bytes is None else known_bytes
    if before == 0:
        return {"path": str(path), "ok": True, "bytes_freed": 0}
    if not dry_run and not remove_disposable(path, deadline=deadline):
        return {'path': str(path), 'ok': True, 'status': 'TIME_BUDGET_PARTIAL', 'bytes_freed': 0}
    return {
        "path": str(path), "ok": True, "dry_run": dry_run,
        "eligible_bytes": before if dry_run else 0,
        "bytes_freed": 0 if dry_run else before,
    }


def is_safe_tmp_name(name: str) -> bool:
    return name in SAFE_TMP_EXACT or any(name.startswith(prefix) for prefix in SAFE_TMP_PREFIXES)


def _aged_children(processes, *, dry_run, min_age_hours, root, time_budget,
                   maintenance_state, npx):
    cutoff = time.time() - min_age_hours*3600
    deadline = time.monotonic()+remaining_budget(time_budget)
    state = maintenance_state if maintenance_state is not None else {}
    key = 'npx_scan' if npx else 'tmp_scan'
    scan = state.setdefault(key, {'done': []})
    done = set(scan['done'])
    rows = []
    if not root.exists():
        state.pop(key, None); return rows
    # Only top-level allowlisted children. Sort small candidate lists by known
    # reclaimable size from previous scans, NEVER recursively sort the filesystem.
    with os.scandir(root) as entries:
        paths = [Path(e.path) for e in entries
                 if (bool(re.fullmatch(r'[0-9a-f]{16}', e.name)) if npx else is_safe_tmp_name(e.name))]
    sizes = state.setdefault('child_sizes', {})
    paths.sort(key=lambda p: sizes.get(str(p), 0), reverse=True)
    for path in paths:
        if path.name in done:
            continue
        if time.monotonic() >= deadline:
            rows.append({'ok': True, 'rule': key, 'status': 'TIME_BUDGET_PARTIAL'}); break
        try:
            s = path.lstat()
            if (s.st_uid != os.getuid() or protected(path) or path.is_symlink()
                    or not is_under(path, root) or (npx and not path.is_dir())):
                done.add(path.name); continue
            identity = path_identity(path)
            newest, amount = tree_stats(path, cutoff=cutoff, deadline=deadline, protected=protected)
            if newest < cutoff:
                sizes[str(path)] = amount
                if path_is_active(path, processes):
                    rows.append({'path': str(path), 'ok': True, 'skipped': 'active_reference', 'bytes_freed': 0})
                else:
                    valid, reason = (True, 'ok') if dry_run else revalidate_candidate(path, identity, require_directory=npx)
                    if valid:
                        row = safe_remove_cache(path, dry_run=dry_run, known_bytes=amount, deadline=deadline)
                        row.update(min_age_hours=min_age_hours, rule='inactive_npx_child' if npx else 'exact_known_disposable')
                        rows.append(row)
                        if row.get('status') == 'TIME_BUDGET_PARTIAL':
                            break
                    else:
                        rows.append({'path': str(path), 'ok': True, 'skipped': reason, 'bytes_freed': 0})
            done.add(path.name)
        except ScanBudget:
            rows.append({'path': str(path), 'ok': True, 'rule': key, 'status': 'TIME_BUDGET_PARTIAL'}); break
        except OSError:
            done.add(path.name)
        finally:
            scan['done'] = list(done)
            if not dry_run and maintenance_state is not None:
                write_maintenance_state(state)
    else:
        state.pop(key, None)
    return rows


def stale_tmp_cleanup(processes, *, dry_run, min_age_hours=DEFAULT_TMP_MIN_AGE_HOURS,
                      root=Path('/tmp'), time_budget=20, maintenance_state=None):
    return _aged_children(processes, dry_run=dry_run, min_age_hours=min_age_hours,
                          root=root, time_budget=time_budget, maintenance_state=maintenance_state, npx=False)


def stale_npx_cleanup(processes, *, dry_run, min_age_hours=DEFAULT_NPX_MIN_AGE_HOURS,
                      root=NPX_CACHE, time_budget=20, maintenance_state=None):
    return _aged_children(processes, dry_run=dry_run, min_age_hours=min_age_hours,
                          root=root, time_budget=time_budget, maintenance_state=maintenance_state, npx=True)


def journal_vacuum(*, dry_run: bool) -> dict[str, Any]:
    before = allocated_bytes(JOURNAL_ROOT)
    if before < JOURNAL_VACUUM_THRESHOLD:
        return {"name": "journal_vacuum", "ok": True, "skipped": "below_threshold", "bytes_before": before, "bytes_freed": 0}
    if dry_run:
        return {"name": "journal_vacuum", "ok": True, "dry_run": True, "age_hours": 336,
                "eligible_bytes": 0, "eligible_attribution_known": False, "bytes_freed": 0}
    result = run(["sudo", "-n", "journalctl", "--vacuum-time=14d"], cwd=Path("/"), timeout=20)
    after = allocated_bytes(JOURNAL_ROOT)
    return {
        "name": "journal_vacuum",
        "ok": result["returncode"] == 0,
        "bytes_before": before,
        "bytes_after": after,
        "bytes_freed": max(0, before - after),
        "result": result,
    }


def evidence_maintenance(*, dry_run, report_path):
    """Privileged scoped helper, bounded independently of the gateway ticker."""
    budget=min(1050,max(1,int(remaining_budget(1080))-30))
    command=['sudo','-n','/usr/bin/python3','-I',
             str(Path(__file__).parent/'evidence_preserving_pruner.py'),
             '--report',str(report_path),'--max-seconds',str(budget)]
    if dry_run: command.append('--dry-run')
    completed=subprocess.run(command,capture_output=True,text=True,
                             timeout=budget+25,cwd='/')
    if completed.returncode or not report_path.is_file():
        return [{'rule':'evidence_preserving_maintenance','ok':False,
                 'status':'HELPER_FAILED','returncode':completed.returncode,'bytes_freed':0}]
    result=json.loads(report_path.read_text())
    return result['rows']


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument('--safe-rules-only',action='store_true')
    parser.add_argument("--worktrees-only", action="store_true")
    parser.add_argument("--force-maintenance", action="store_true")
    parser.add_argument("--emit-noop", action="store_true")
    parser.add_argument("--max-count", type=int, default=DEFAULT_MAX_COUNT)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--trigger-free-bytes", type=int, default=DEFAULT_TRIGGER_FREE)
    parser.add_argument("--maintenance-interval-hours", type=float, default=DEFAULT_MAINTENANCE_INTERVAL_HOURS)
    parser.add_argument("--npm-verify-interval-hours", type=float, default=DEFAULT_NPM_VERIFY_INTERVAL_HOURS)
    parser.add_argument("--tmp-min-age-hours", type=float, default=DEFAULT_TMP_MIN_AGE_HOURS)
    parser.add_argument("--npx-min-age-hours", type=float, default=DEFAULT_NPX_MIN_AGE_HOURS)
    parser.add_argument("--protect-path", action="append", default=[])
    parser.add_argument("--report-dir")
    args = parser.parse_args()
    report_dir = Path(args.report_dir) if args.report_dir else REPORT_ROOT / stamp()
    report_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    phase_seconds = {}
    def timed_phase(name, operation):
        phase_start = time.monotonic()
        try:
            return operation()
        finally:
            phase_seconds[name] = round(time.monotonic()-phase_start, 3)

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(render(title("warn", "Host Disk", "another retention run holds the lock"), meaning("no action; the active run owns cleanup"), output_class="alert"))
            return 0

        global _RUN_DEADLINE
        _RUN_DEADLINE = started+(210 if args.safe_rules_only else 1150)
        before = shutil.disk_usage("/")
        now = time.time()
        processes = process_inventory()
        builders = active_builders(processes)
        inventory = run(["git", "worktree", "list", "--porcelain"], timeout=180)
        records = parse_worktrees(inventory["stdout_tail"], processes)
        explicit_keep = {str(Path(p).resolve(strict=False)) for p in args.protect_path}
        removals, keep = choose_removals(
            records,
            max_count=max(1, args.max_count),
            max_bytes=max(GIB, args.max_bytes),
            explicit_keep=explicit_keep,
        )
        pressure_trigger = (
            before.free < args.trigger_free_bytes
            or len(records) > args.max_count
            or sum(r.allocated_bytes for r in records) > args.max_bytes
        )
        worktree_results = [remove_worktree(r, dry_run=args.dry_run) for r in removals] if pressure_trigger else []
        if worktree_results and not args.dry_run:
            run(["git", "worktree", "prune"], timeout=180)

        maintenance_state = load_maintenance_state()
        routine_due = (
            not args.worktrees_only
            and (
                args.force_maintenance
                or action_due(
                    maintenance_state,
                    "routine_checked_at",
                    max(DEFAULT_MAINTENANCE_INTERVAL_HOURS, args.maintenance_interval_hours),
                    now,
                )
            )
        )
        npm_due = (
            not args.worktrees_only
            and (
                args.force_maintenance
                or action_due(
                    maintenance_state,
                    "npm_verified_at",
                    max(DEFAULT_NPM_VERIFY_INTERVAL_HOURS, args.npm_verify_interval_hours),
                    now,
                )
            )
        )

        cache_results: list[dict[str, Any]] = []
        tmp_results: list[dict[str, Any]] = []
        npx_results: list[dict[str, Any]] = []
        uv_result: dict[str, Any] | None = None
        npm_result: dict[str, Any] | None = None
        journal_result: dict[str, Any] | None = None

        # Explicit regenerable roots run in measured-size order. Reports/evidence
        # are protected; no full-root recursive inventory delays the cron effect.
        extension_results = [] if args.worktrees_only else timed_phase('extensions', lambda: run_extensions(
            sys.modules[__name__], dry_run=args.dry_run, pressure=before.free < RED_FREE,
            maintenance_state=maintenance_state, now=now))

        if routine_due:
            routine_roots = [('tmp', Path('/tmp')), ('npx', NPX_CACHE)]
            sizes = maintenance_state.get('child_sizes', {})
            routine_roots.sort(key=lambda pair: sum(v for p, v in sizes.items()
                                if str(p).startswith(str(pair[1]).rstrip('/')+'/')), reverse=True)
            for kind, root in routine_roots:
                action = stale_tmp_cleanup if kind == 'tmp' else stale_npx_cleanup
                threshold = (max(DEFAULT_TMP_MIN_AGE_HOURS, args.tmp_min_age_hours) if kind == 'tmp'
                             else max(DEFAULT_NPX_MIN_AGE_HOURS, args.npx_min_age_hours))
                result = timed_phase(kind, lambda: action(processes, dry_run=args.dry_run,
                                     min_age_hours=threshold, root=root, maintenance_state=maintenance_state))
                if kind == 'tmp': tmp_results = result
                else: npx_results = result
            uv_result = timed_phase('uv', lambda: command_maintenance(
                "uv_cache_prune",
                [UV_BIN, "cache", "prune", "--no-progress"],
                (UV_CACHE,),
                dry_run=args.dry_run,
                timeout=30,
                env={"HOME": "/home/ubuntu", "UV_CACHE_DIR": str(UV_CACHE)},
            ))
            journal_result = timed_phase('journal', lambda: journal_vacuum(dry_run=args.dry_run))
            routine_ok = all(
                row.get("ok", True) and row.get('status') != 'TIME_BUDGET_PARTIAL'
                for row in tmp_results + npx_results + [uv_result, journal_result]
            )
            if routine_ok and not args.dry_run:
                maintenance_state["routine_checked_at"] = now
                write_maintenance_state(maintenance_state)

        if npm_due:
            npm_result = timed_phase('npm', lambda: command_maintenance(
                "npm_cache_verify",
                [NPM_BIN, "cache", "verify"],
                (NPM_CACHE,),
                dry_run=args.dry_run,
                timeout=60,
                env={"HOME": "/home/ubuntu", "npm_config_cache": "/home/ubuntu/.npm"},
            ))
            if npm_result.get("ok", False) and not args.dry_run:
                maintenance_state["npm_verified_at"] = now
                write_maintenance_state(maintenance_state)

        # No arbitrary broad roots are removed: new regenerable chunks have
        # explicit age/reference proofs; native npm/uv maintenance complements them.

        growth = growth_sensor()
        if not args.dry_run:
            maintenance_state["updated_at"] = utc_now()
            write_maintenance_state(maintenance_state)

        if not args.worktrees_only and not args.safe_rules_only:
            extension_results.extend(timed_phase('evidence_preserving',lambda: evidence_maintenance(
                dry_run=args.dry_run,report_path=report_dir/'evidence-preserving.json')))

        after = shutil.disk_usage("/")
        final_inventory = run(["git", "worktree", "list", "--porcelain"], timeout=180)
        final_records = parse_worktrees(final_inventory["stdout_tail"], process_inventory())
        maintenance_results = tmp_results + npx_results + cache_results + extension_results
        for row in (uv_result, npm_result, journal_result):
            if row is not None:
                maintenance_results.append(row)
        reclaimed = sum(int(row.get("bytes_freed") or 0) for row in worktree_results + maintenance_results)
        eligible = sum(int(row.get("eligible_bytes") or 0) for row in worktree_results + maintenance_results)
        root_delta = after.free - before.free
        failures = [row for row in worktree_results + maintenance_results if not row.get("ok", True)]
        cap_ok = len(final_records) <= args.max_count and sum(r.allocated_bytes for r in final_records) <= args.max_bytes
        planned_remaining = final_records
        if args.dry_run:
            planned_remaining = [r for r in records if r.path not in {x.path for x in removals}]
            cap_ok = len(planned_remaining) <= args.max_count and sum(r.allocated_bytes for r in planned_remaining) <= args.max_bytes

        receipt = {
            "timestamp_utc": utc_now(),
            "runtime_budget_seconds": 1150,
            "runtime_budget_exhausted": remaining_budget(1)<0.02,
            "runtime_seconds": round(time.monotonic()-started, 3),
            "phase_seconds": phase_seconds,
            "incremental_safe_rules_pending": bool(maintenance_state.get('safe_rule_cursors') or maintenance_state.get('git_safe_cursor')),
            "incremental_artifact_scan_pending": 'artifact_scan' in maintenance_state,
            "thresholds": {"amber_free_bytes": AMBER_FREE, "red_free_bytes": RED_FREE},
            "extension_results": extension_results,
            "growth_sensor": growth,
            "enzyme_vault": enzyme_vault_usage(),
            "exit_semantics": {"0": "completed", "1": "maintenance_failed", "2": "acted_and_still_RED"},
            "dry_run": args.dry_run,
            "triggered": pressure_trigger,
            "routine_maintenance_due": routine_due,
            "npm_verify_due": npm_due,
            "root_before": {"free_bytes": before.free, "used_bytes": before.used, "total_bytes": before.total},
            "root_after": {"free_bytes": after.free, "used_bytes": after.used, "total_bytes": after.total},
            "reclaimed_bytes": reclaimed,
            "eligible_bytes": eligible,
            "root_free_delta_bytes": root_delta,
            "worktrees_before": [asdict(r) for r in records],
            "worktrees_after": [asdict(r) for r in final_records],
            "kept_paths": sorted(keep),
            "worktree_results": worktree_results,
            "active_builders": builders,
            "cache_results": cache_results,
            "tmp_results": tmp_results,
            "npx_results": npx_results,
            "uv_result": uv_result,
            "npm_result": npm_result,
            "journal_result": journal_result,
            "failures": failures,
            "cap_ok": cap_ok,
            "protected_paths_untouched": True,
            "routine_policy": {
                "tmp_exact_names_only": True,
                "tmp_min_age_hours": max(DEFAULT_TMP_MIN_AGE_HOURS, args.tmp_min_age_hours),
                "tmp_newest_descendant_age_checked": True,
                "npx_per_directory_only": True,
                "npx_min_age_hours": max(DEFAULT_NPX_MIN_AGE_HOURS, args.npx_min_age_hours),
                "npx_real_directory_only": True,
                "uv_official_prune": True,
                "npm_official_verify": True,
                "explicit_cache_environment": True,
                "unknown_tmp_untouched": True,
                "active_npx_untouched": True,
                "live_fd_maps_exe_root_checked": True,
                "candidate_identity_revalidated": True,
                "broad_cache_deletion_disabled": True,
            },
        }
        receipt_path = report_dir / "receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        hard = after.free < HARD_ALERT_FREE
        mode = "dry-run" if args.dry_run else "run"
        amount = eligible if args.dry_run else reclaimed
        verb = "eligible" if args.dry_run else "reclaimed"
        headline = (
            f"{mode} {verb} **{amount / GIB:.2f} GiB** · root "
            f"**{after.used / after.total * 100:.1f}% used / {after.free / GIB:.1f} GiB free** · "
            f"rehearsals **{len(records)}→{len(planned_remaining)}** · builders **{len(builders)}** · "
            f"receipt {receipt_path}"
        )
        if hard:
            print(render(title("action", "Host Disk", headline), meaning("stop nonessential writes and expand or reclaim disk immediately"), output_class="alert"))
        elif after.free < AMBER_FREE:
            print(render(title("warn", "Host Disk", 'AMBER · ' + headline), output_class="alert"))
        elif failures or not cap_ok:
            print(render(title("warn", "Host Disk", headline), meaning("inspect the receipt; protected or failed items prevented the retention cap"), output_class="alert"))
        elif amount > 0 or args.emit_noop:
            print(render(title("infra", "Host Disk", headline), output_class="report"))
        if growth.get('partition_growth_needed') or not growth.get('ok'):
            print(render(title('warn', 'Host Disk', growth['status']), output_class='alert'))
        if failures or not cap_ok:
            return 1
        return 2 if hard and not args.dry_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
