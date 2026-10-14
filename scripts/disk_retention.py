"""Bounded host retention extensions. Unknown provenance is a refusal, not age-based authority."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

BUS = Path('/home/ubuntu/business')
FLAG = BUS / 'site-system/flagship-v2'
POLICY = BUS / 'ops/disk-retention-policy.json'
GB = 10**9
AMBER_FREE = 40 * GB
RED_FREE = 15 * GB


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def protected(path):
    p = Path(path)
    names = p.parts
    return (p.is_symlink() or p.resolve() != p.absolute() or
            any(n.startswith(('sealed-', 'tmux-', '.org.chromium.', 'state.db')) or
                n in {'secrets', 'browser-profiles', 'chromium', 'claude-1000', 'hermes-results', '.git'}
                for n in names))


def growth_sensor():
    try:
        data = json.loads(subprocess.check_output(['lsblk', '-b', '-J', '-o', 'NAME,SIZE,TYPE,START'], text=True))
        disk = next(x for x in data['blockdevices'] if x['name'] == 'nvme0n1')
        part = next(x for x in disk['children'] if x['name'] == 'nvme0n1p1')
        # Small EFI/boot partitions and GPT alignment are expected, not lost capacity.
        end = int(part['start']) * 512 + int(part['size'])
        gap = max(0, int(disk['size']) - end)
        return {'ok': True, 'disk_bytes': disk['size'], 'root_partition_bytes': part['size'],
                'unused_tail_bytes': gap, 'partition_growth_needed': gap > 1024**2,
                'status': 'PARTITION_SMALLER_THAN_DISK' if gap > 1024**2 else 'GROWN'}
    except Exception as e:
        return {'ok': False, 'status': 'GROWTH_UNKNOWN', 'error_type': type(e).__name__}


def load_policy(path=POLICY):
    try:
        policy = json.loads(Path(path).read_text())
        if policy.get('schema') != 'disk-retention-v1':
            raise ValueError('schema')
        return policy
    except (OSError, ValueError):
        return {'schema': 'disk-retention-v1', 'superseded_candidates': [], 'archives': [],
                'seals': [], 'protected_roots': [], 'inventory_status': 'UNKNOWN'}


def pinned_json(binding):
    p = Path(binding['path'])
    if protected(p) or digest(p) != binding['sha256']:
        raise ValueError('evidence_pin_or_path')
    return json.loads(p.read_text())


def candidate_cleanup(api, policy, *, dry_run, now=None, flag=FLAG):
    now = time.time() if now is None else now
    rows = []
    candidates = []
    releases = flag / 'releases'
    if releases.exists():
        candidates += [(p, 86400, 'stale_COW') for p in releases.glob('.candidate.cow-*')]
    for entry in policy.get('superseded_candidates', []):
        p = Path(entry['path'])
        if p.parent not in (flag/'candidates', flag/'work') or p.name == 'glp-visual-ultracode-20260821':
            continue
        try:
            proof = pinned_json(entry['proof'])
            if proof.get('superseded_path') != str(p) or proof.get('status') != 'SUPERSEDED':
                raise ValueError('supersession_not_proven')
            candidates.append((p/'node_modules', 3*86400, 'superseded_node_modules'))
        except Exception as exc:
            rows.append({'path': str(p), 'ok': True, 'skipped': type(exc).__name__})
    for p, age, rule in candidates:
        if protected(p) or any(api.is_under(p, Path(x)) or api.is_under(Path(x), p) for x in policy.get('protected_roots', [])):
            rows.append({'path': str(p), 'ok': True, 'skipped': 'protected'})
            continue
        identity = api.path_identity(p)
        if not identity or not p.is_dir() or now - api.newest_descendant_mtime(p) <= age:
            continue
        valid, reason = api.revalidate_candidate(p, identity, require_directory=True)
        if not valid:
            rows.append({'path': str(p), 'ok': True, 'skipped': reason})
            continue
        amount = api.allocated_bytes(p)
        if not dry_run:
            shutil.rmtree(p)
        rows.append({'path': str(p), 'rule': rule, 'ok': dry_run or not p.exists(),
                     'eligible_bytes': amount if dry_run else 0, 'bytes_freed': 0 if dry_run else amount})
    return rows


def archive_retention(api, policy, *, dry_run, now=None):
    """Only archive artifacts, never the live state database; byte proof before unlink."""
    now = time.time() if now is None else now
    rows = []
    for entry in policy.get('archives', []):
        row = {'rule': 'verified_archive_zstd', 'ok': True, 'bytes_freed': 0}
        try:
            proof = pinned_json(entry['verify'])
            p = Path(proof['archive'])
            if protected(p) or p.suffix != '.db' or not p.name.startswith(('state-archive-', 'archive.')):
                raise ValueError('archive_path_refused')
            if proof.get('result') != 'PASS' or proof.get('integrity_check') != 'ok' or proof.get('problems'):
                raise ValueError('verify_not_green')
            expected = proof.get('archive_sha256')
            if not expected or len(expected) != 64:
                raise ValueError('missing_archive_hash')
            z = p.with_suffix(p.suffix + '.zst')
            row['path'] = str(p)
            if p.exists():
                if api.path_is_active(p, api.process_inventory()) or digest(p) != expected:
                    raise ValueError('archive_active_or_hash_changed')
                if not dry_run:
                    tmp = z.with_name(z.name + '.retention-tmp')
                    if tmp.exists():
                        raise ValueError('ambiguous_compression_temp')
                    subprocess.run(['zstd', '-q', '-T1', str(p), '-o', str(tmp)], check=True, timeout=900)
                    if decompressed_digest(tmp) != expected:
                        raise ValueError('compressed_hash_mismatch')
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, z)
                    if digest(p) != expected or api.path_is_active(p, api.process_inventory()):
                        raise ValueError('archive_changed_before_unlink')
                    amount = p.stat().st_blocks * 512
                    p.unlink()
                    row['bytes_freed'] = max(0, amount - z.stat().st_blocks*512)
            elif not z.exists():
                raise ValueError('archive_missing')
            backup = entry.get('backup')
            if backup and now - float(entry['verified_at_epoch']) >= 86400:
                b = Path(backup['path'])
                if protected(b) or b.suffix not in {'.gz', '.tgz', '.tar'} or not b.name.startswith('state-'):
                    raise ValueError('backup_path_refused')
                if b.exists():
                    if decompressed_digest(z) != expected or digest(b) != backup['sha256']:
                        raise ValueError('backup_or_archive_hash_mismatch')
                    if api.path_is_active(b, api.process_inventory()):
                        raise ValueError('backup_active')
                    if not dry_run:
                        row['bytes_freed'] += b.stat().st_blocks * 512
                        b.unlink()
                    row['backup_removed'] = not dry_run
            row['status'] = 'DRY_RUN' if dry_run else 'VERIFIED_COMPRESSED'
        except Exception as exc:
            row.update(ok=False, status='RETENTION_REFUSED', error_type=type(exc).__name__, reason=str(exc)[:100])
        rows.append(row)
    return rows


def decompressed_digest(path):
    h = hashlib.sha256()
    proc = subprocess.Popen(['zstd', '-q', '-d', '-c', str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for chunk in iter(lambda: proc.stdout.read(1024*1024), b''):
            h.update(chunk)
        if proc.wait() != 0:
            raise ValueError('zstd_verify_failed')
    finally:
        proc.stdout.close()
    return h.hexdigest()


def run_extensions(api, *, dry_run, pressure, maintenance_state, now):
    policy = load_policy()
    rows = []
    pointer_ok = False
    try:
        pointer = json.loads((BUS/'reports/glp-clinic-scout/seo-machine/current-content-base.json').read_text())
        if pointer.get('status') != 'PASS' or not pointer.get('release_root'):
            raise ValueError('current_base_not_pass')
        policy.setdefault('protected_roots', []).append(pointer['release_root'])
        pointer_ok = True
    except (OSError, ValueError):
        rows.append({'ok': True, 'rule': 'candidate_cleanup', 'status': 'GATED_CURRENT_BASE_UNKNOWN'})
    if pressure:
        if pointer_ok:
            rows.extend(candidate_cleanup(api, policy, dry_run=dry_run, now=now))
        for p in [Path('/home/ubuntu/.npm/_cacache'), Path('/home/ubuntu/.cache/pip')]:
            # A process merely named claude elsewhere does not veto unrelated safe classes.
            if p.exists() and not protected(p) and not api.path_is_active(p, api.process_inventory()):
                rows.append(api.safe_remove_cache(p, dry_run=dry_run))
    rows.extend(archive_retention(api, policy, dry_run=dry_run, now=now))
    # Seal removal remains fail-closed until the current deployment AND open-chain
    # owners supply exact pins. Age or an ACCEPT string alone never grants deletion.
    rows.append({'ok': True, 'rule': 'superseded_intermediate_seals', 'status': 'GATED',
                 'reason': 'Current deployment/base/open-chain exclusion manifest and successor binding required; no seal deletion inferred',
                 'registered_candidates': len(policy.get('seals', []))})
    if api.action_due(maintenance_state, 'evidence_dedup_at', 168, now):
        # Evidence copies are frequently root-owned/read-only. Preserve their
        # modes and ownership instead of chmodding them or failing every cadence.
        # The helper still refuses all sealed/symlink/mutable files and limits time.
        command = ['sudo', '-n', '/usr/bin/python3', str(BUS/'scripts/hardlink_dedup.py'),
                   str(BUS/'reports'), str(FLAG/'candidates'), '--max-seconds', '600']
        if dry_run:
            command.append('--dry-run')
        for path in policy.get('protected_roots', []):
            command.extend(['--exclude', path])
        completed = subprocess.run(command, capture_output=True, text=True, timeout=650)
        try:
            result = json.loads(completed.stdout)
            result['ok'] = bool(result.get('ok')) and completed.returncode == 0
        except ValueError:
            result = {'ok': False, 'status': 'DEDUP_TRANSPORT_FAILED', 'returncode': completed.returncode}
        result['rule'] = 'weekly_evidence_hardlink_dedup'
        rows.append(result)
        if result['ok'] and not dry_run:
            maintenance_state['evidence_dedup_at'] = now
    return rows
