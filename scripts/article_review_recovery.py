#!/usr/bin/env python3
"""Fresh post-publication review; never authenticates absent historical custody."""
import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path('/home/ubuntu/business')
SCHEMA = 'glp-postpublication-review-recovery-v1'
SUBJECTS = {'00-authority.json', '01-evidence-ledger.json', '02-serp-brief.json',
            '03-editorial-brief.md', '04-draft.md', '05-claim-source-map.json',
            '06-article.json', '07-qa-receipt.json'}


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def spec(path):
    return {'path': str(Path(path).resolve(strict=True)), 'sha256': sha(path)}


def read(path):
    return json.loads(Path(path).read_text())


def checked(value):
    path = Path(value['path']).resolve(strict=True)
    require(path.is_file() and sha(path) == value['sha256'], 'recovery_artifact_drift')
    return path


def normalize_live(text):
    def decode(value):
        raw = bytes.fromhex(value)
        return bytes(v ^ raw[0] for v in raw[1:]).decode()
    text = re.sub(r'/cdn-cgi/l/email-protection#([0-9a-f]+)',
                  lambda m: 'mailto:' + decode(m[1]), text)
    text = re.sub(r'<span class="__cf_email__" data-cfemail="([0-9a-f]+)">\[email&#160;protected\]</span>',
                  lambda m: decode(m[1]), text)
    return text.replace('<script data-cfasync="false" src="/cdn-cgi/scripts/5c5dd728/cloudflare-static/email-decode.min.js"></script>', '')


def subjects_match(original, fresh, rows, manifest):
    expected = {row['path']: row['sha256'] for row in rows}
    actual = {row['path']: row['sha256'] for row in manifest['subject_files']}
    require(set(expected) == SUBJECTS and len(rows) == len(expected), 'original_subject_set')
    require(set(actual) == SUBJECTS | {'08-live-guide.html'}
            and len(manifest['subject_files']) == len(actual), 'recovery_subject_set')
    for name, digest in expected.items():
        require(actual[name] == digest and sha(original / name) == digest
                and sha(fresh / name) == digest, 'original_subject_drift')
    require(sha(fresh / '08-live-guide.html') == actual['08-live-guide.html'], 'live_subject_drift')


def recovery_path(entry_path, root):
    ident = Path(entry_path).parent.name
    require(re.fullmatch(r'[a-f0-9]{24}', ident), 'entry_id_invalid')
    return root / 'reports/glp-clinic-scout/seo-machine/article-review-recovery' / ident / 'RECOVERY.json'


def validate_documents(entry_path, doc, *, root=ROOT, session_db=None):
    import hermes_sol_article_review as transport
    sys.path.insert(0, str(root / 'systems/seo-blog-machine/scripts'))
    import opus_review_loop as loop
    entry = read(entry_path)
    require(doc['schema'] == SCHEMA and doc['status'] == 'PASS_POSTPUBLICATION_REVIEW', 'recovery_schema')
    require(checked(doc['entry']) == entry_path.resolve(), 'recovery_entry_replay')
    require(doc['original_review'] == entry['pass_receipt']
            and doc['original_raw_review'] == entry['raw_review'], 'historical_review_binding')
    checked(doc['original_review']); checked(doc['original_raw_review'])
    old = read(checked(entry['raw_review']))
    require('result_custody' not in old.get('hermes_runtime', {}), 'historical_custody_present')
    import sqlite3
    database = Path(session_db or transport.SESSION_DB).resolve(strict=True)
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
        old_session = old.get('hermes_runtime', {}).get('session_id')
        require(old_session and db.execute('select count(*) from sessions where id=?', (old_session,)).fetchone()[0] == 0
                and db.execute('select count(*) from messages where session_id=?', (old_session,)).fetchone()[0] == 0,
                'historical_native_session_present')
    round_path = checked(doc['review'])
    raw_path = checked(doc['raw_review'])
    manifest_path = checked(doc['subject_manifest'])
    receipt, raw, manifest = read(round_path), read(raw_path), read(manifest_path)
    require(receipt['receipt_sha256'] == loop.payload_hash(receipt, 'receipt_sha256')
            and manifest['manifest_sha256'] == loop.payload_hash(manifest, 'manifest_sha256')
            and receipt['raw_review_sha256'] == sha(raw_path)
            and receipt['subject_manifest_sha256'] == sha(manifest_path), 'fresh_review_binding')
    fresh = checked(doc['live_snapshot']).parent
    require(doc['live_snapshot']['path'] == str(fresh / '08-live-guide.html'), 'live_snapshot_path')
    subjects_match(entry_path.parent / 'packet', fresh, entry['packet_files'], manifest)
    payload = loop.parse_result(raw)
    loop.validate_review(payload, raw, manifest, receipt['round'])
    require(payload['verdict'] == receipt['status'] == 'PASS_OPUS_SEO_DRAFT'
            and payload['gates'] == receipt['gates'] and payload['findings'] == receipt['findings'] == []
            and all(g['status'] == 'PASS' and not g['findings'] for g in receipt['gates']), 'fresh_review_not_PASS')
    custody = transport.validate_result_custody(raw, manifest=manifest, packet=fresh, session_db=session_db)
    from article_health_acceptance import timestamp
    require(timestamp(manifest['sealed_at_utc']) <= custody['started_at']
            and custody['ended_at'] <= timestamp(receipt['reviewed_at_utc']), 'fresh_review_chronology')
    pointer = read(root / 'reports/glp-clinic-scout/seo-machine/current-content-base.json')
    binding = doc['live_binding']
    live_file = Path(pointer['release_root']) / 'public' / entry['target_route'].strip('/') / 'index.html'
    require(binding['url'] == 'https://glpclinicscout.com' + entry['target_route']
            and binding['http_status'] == 200 and binding['deployment_id'] == pointer['deployment_id']
            and sha(live_file) == binding['deployed_sha256']
            and sha(checked(doc['live_snapshot'])) == binding['live_sha256']
            and normalize_live(checked(doc['live_snapshot']).read_text()) == live_file.read_text(),
            'reviewed_live_deployment_drift')
    return {'receipt': receipt, 'raw': raw, 'manifest': manifest, 'custody': custody, 'document': doc}


def validate_recovery(entry_path, *, root=ROOT, natural=False, session_db=None):
    require(not natural, 'postpublication_review_not_natural')
    entry_path = Path(entry_path).resolve(strict=True)
    path = recovery_path(entry_path, root)
    result = validate_documents(entry_path, read(path), root=root, session_db=session_db)
    result['recovery'] = spec(path)
    return result


def seal(entry_path, history, packet, binding_path, *, root=ROOT):
    entry = read(entry_path)
    rounds = sorted(history.glob('round-*.receipt.json'))
    if not rounds:
        rounds = sorted(history.glob('round-*-receipt.json'))
    require(len(rounds) == 1, 'exact_single_review_required')
    receipt = read(rounds[0])
    manifest_path = Path(receipt['subject_manifest_path'])
    raw_path = Path(receipt['raw_review_path'])
    doc = {'schema': SCHEMA, 'status': 'PASS_POSTPUBLICATION_REVIEW',
           'entry': spec(entry_path), 'original_review': entry['pass_receipt'],
           'original_raw_review': entry['raw_review'], 'review': spec(rounds[0]),
           'raw_review': spec(raw_path), 'subject_manifest': spec(manifest_path),
           'live_snapshot': spec(packet / '08-live-guide.html'), 'live_binding': read(binding_path),
           'historical_custody_repaired': False, 'natural_acceptance': False}
    validate_documents(entry_path, doc, root=root)
    path = recovery_path(entry_path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(doc, stream, indent=2, sort_keys=True)
        stream.write('\n')
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entry', type=Path, required=True)
    parser.add_argument('--history', type=Path, required=True)
    parser.add_argument('--packet', type=Path, required=True)
    parser.add_argument('--live-binding', type=Path, required=True)
    args = parser.parse_args()
    from glp_release_main import lock_module
    with lock_module().acquire('main-site-release', owner='article-review-recovery', root=ROOT):
        path = seal(args.entry.resolve(strict=True), args.history.resolve(strict=True),
                    args.packet.resolve(strict=True), args.live_binding.resolve(strict=True))
    print(json.dumps({'status': 'PASS_POSTPUBLICATION_REVIEW', 'recovery': spec(path)}))


if __name__ == '__main__':
    main()
