#!/usr/bin/env python3
"""Prepare an UNAPPROVED, technical-only successor of an immutable sponsor ZIP.

Never installs, activates, writes approval receipts or uses the network. The
existing sender's complete-archive/hash guard is unchanged: the operator must
approve the NEW hash separately. Copy, cadence, PDFs and capture evidence are
retained byte for byte, not regenerated. Candidate dependencies are labelled
unmerged; they are never represented as a supported installation.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import zipfile

MANIFEST = 'ops/sponsor-machine/assets.json'
CLIENT = 'scripts/firecrawl_client.py'
READER = 'scripts/glp328_readonly_evidence.py'
REQUIRED = {
    'ops/sponsor-machine/campaign.json', 'ops/sponsor-machine/copy.json',
    'scripts/glp_sponsor_campaign.py', 'scripts/glp_sponsor_paypal.py',
    'scripts/glp_sponsor_readiness.py', 'scripts/glp_sponsor_alexandria.py',
    'scripts/bouncer_shared_account_guard.py', CLIENT,
    'inbox_control_plane/control_plane.py', 'inbox_control_plane/sponsor_replies.py',
}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key:' + key)
        result[key] = value
    return result


def parse(data):
    return json.loads(data, object_pairs_hook=unique_object)


def git(checkout, *args):
    return subprocess.check_output(['git', '-C', str(checkout), *args], stderr=subprocess.PIPE)


def head(checkout):
    return git(checkout, 'rev-parse', 'HEAD').decode().strip()


def merged(checkout, commit):
    result = subprocess.run(['git', '-C', str(checkout), 'merge-base', '--is-ancestor',
                             commit, 'origin/main'], capture_output=True)
    if result.returncode not in (0, 1):
        raise ValueError('origin_main_custody_missing')
    return result.returncode == 0


def safe_name(name):
    p = PurePosixPath(name)
    if not name or '\\' in name or p.is_absolute() or '..' in p.parts or str(p) != name:
        raise ValueError('unsafe_packet_member')
    if p.name in {'approval.json', 'activation.json', 'ledger.json'} or name.startswith('data/'):
        raise ValueError('runtime_state_in_packet')
    return name


def build_successor(packet, expected_sha256, source_checkout, dependency_checkout,
                    dependency_head, output):
    packet, source_checkout, dependency_checkout, output = map(
        Path, (packet, source_checkout, dependency_checkout, output))
    original = packet.read_bytes()
    if not re.fullmatch('[0-9a-f]{64}', expected_sha256) or sha(original) != expected_sha256:
        raise ValueError('predecessor_packet_hash_mismatch')
    if output.exists() or output.is_symlink():
        raise ValueError('output_already_exists')
    if any(output.resolve().is_relative_to(p.resolve()) for p in (source_checkout, dependency_checkout)):
        raise ValueError('output_inside_source_checkout')
    source_head = head(source_checkout)
    if not re.fullmatch('[0-9a-f]{40}', dependency_head) or head(dependency_checkout) != dependency_head:
        raise ValueError('dependency_head_mismatch')
    source_merged = merged(source_checkout, source_head)
    dependency_merged = merged(dependency_checkout, dependency_head)
    if not source_merged:
        raise ValueError('sponsor_source_not_merged')
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        infos = archive.infolist()
        names = [safe_name(i.filename) for i in infos]
        if len(names) != len(set(names)):
            raise ValueError('duplicate_packet_member')
        if sum(i.file_size for i in infos) > 16 * 1024 * 1024:
            raise ValueError('packet_expansion_limit')
        if any(i.is_dir() or stat.S_ISLNK(i.external_attr >> 16) for i in infos):
            raise ValueError('nonregular_packet_member')
        members = {n: archive.read(n) for n in names}
    sealed = parse(members[MANIFEST])
    if not isinstance(sealed, dict) or not REQUIRED.issubset(sealed) or MANIFEST in sealed:
        raise ValueError('full_source_binding_missing')
    for rel, digest in sealed.items():
        safe_name(rel)
        if not re.fullmatch('[0-9a-f]{64}', str(digest)) or sha(members[rel]) != digest:
            raise ValueError('predecessor_manifest_mismatch:' + rel)
    config = parse(members['ops/sponsor-machine/campaign.json'])
    if config.get('send_enabled') is not False or config.get('packet_sha256') is not None:
        raise ValueError('predecessor_not_disabled_first')
    # All unchanged sealed source is required at the merged sponsor revision.
    # Read immutable Git objects, never mutable worktree files.
    for rel, old_bytes in members.items():
        if rel in sealed and (rel.endswith('.py') or rel.startswith('ops/sponsor-machine/')) and rel not in (CLIENT, READER, MANIFEST):
            if git(source_checkout, 'show', source_head + ':' + rel) != old_bytes:
                raise ValueError('nontechnical_source_drift:' + rel)
    updates = {
        CLIENT: git(dependency_checkout, 'show', dependency_head + ':' + CLIENT),
        READER: git(source_checkout, 'show', source_head + ':' + READER),
    }
    new_members = {**members, **updates}
    new_manifest = {**sealed, **{p: sha(b) for p, b in updates.items()}}
    new_members[MANIFEST] = (json.dumps(new_manifest, indent=2, sort_keys=True) + '\n').encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for info in infos:
            archive.writestr(info, new_members[info.filename])
        if READER not in members:
            info = zipfile.ZipInfo(READER, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, updates[READER])
    successor = buf.getvalue()
    result = {
        'schema': 'sponsor.technical_successor.v1',
        'status': 'UNAPPROVED_TECHNICAL_SUCCESSOR',
        'predecessor_sha256': expected_sha256,
        'packet_sha256': sha(successor), 'source_head': source_head,
        'dependency_head': dependency_head, 'source_merged': source_merged,
        'dependency_merged': dependency_merged,
        'installation_allowed': False, 'operator_approved': False,
        'approval_required': 'APPROVE SPONSOR PACKET ' + sha(successor),
        'changed_members': sorted(p for p in new_members if members.get(p) != new_members[p]),
        'technical_sources': {p: {'previous_sha256': sha(members[p]) if p in members else None,
                                  'successor_sha256': sha(b)} for p, b in updates.items()},
        'unchanged_members': sorted(p for p in members if p not in (MANIFEST, CLIENT, READER)),
        'preserved': 'Exact original packet remains immutable; unchanged copy/cadence and rendered assets retained. New hash needs separate exact authority; no old receipt is transplanted.',
        'effects': {'installs': 0, 'sends': 0, 'financial_writes': 0, 'deploys': 0, 'approval_writes': 0},
    }
    if packet.read_bytes() != original:
        raise ValueError('predecessor_changed_during_build')
    output.mkdir(parents=True, exist_ok=False)
    (output/'sponsor-review-packet.zip').write_bytes(successor)
    (output/'SUCCESSOR.json').write_text(json.dumps(result, indent=2, sort_keys=True) + '\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--packet', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--dependency-checkout', type=Path, required=True)
    parser.add_argument('--dependency-head', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = build_successor(args.packet, args.expected_sha256, args.checkout,
                             args.dependency_checkout, args.dependency_head, args.output)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
