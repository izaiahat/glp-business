#!/usr/bin/env python3
"""Reuse exact pre-publication five-gate acceptance, not an author's attestation.

This is completion evidence only: no model calls, effects, publication or gate
waivers. Missing review goes back to the existing Astra refill/review controller.
"""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SESSION_DB = Path('/home/ubuntu/.hermes/state.db')
SCHEMA = 'glp-published-article-independent-acceptance-v1'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def spec(path):
    path = Path(path).resolve(strict=True)
    return {'path': str(path), 'sha256': sha(path)}


def read(path):
    return json.loads(Path(path).read_text())


def require(condition, why):
    if not condition:
        raise ValueError('independent health acceptance refused: ' + why)


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def validate_carrier(run, *, natural):
    effects = run.get('effects') or {}
    require(run.get('action') == 'article_carrier_consume' and run.get('status') == 'PASS'
            and run.get('rc') == 0 and effects.get('machine_status') == 'PASS_PUBLISHED'
            and effects.get('output_count') == 1 and effects.get('live_status') == 200,
            'carrier has no one-article live publication')
    if natural:
        # A scheduled pending-job GET is not a new natural publication. Its
        # envelope can retain scheduled invocation while recovering manual work.
        # Until original dispatch custody is authenticated, never promote any
        # reconciliation (even an alleged scheduled origin) to natural proof.
        require('reconciled_prior_receipt' not in run,
                'reconciled origin cannot prove a natural dispatch')
        require(run.get('invocation') == 'scheduled' and run.get('run_type', 'scheduled') == 'scheduled',
                'natural due slot not proved (manual recovery is not natural)')
        match = re.fullmatch(r'(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}):America/Los_Angeles', str(run.get('due_slot')))
        require(match is not None, 'natural due slot missing')
        due = datetime.fromisoformat(match[1] + 'T' + match[2]).replace(tzinfo=ZoneInfo('America/Los_Angeles'))
        started = datetime.fromisoformat(run['started_utc'].replace('Z', '+00:00')).astimezone(due.tzinfo)
        require(started.date() == due.date() and started >= due, 'different day or pre-due invocation')


def author_session(db, session_id, raw, reviewed_at):
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
        row = conn.execute('select model,started_at,ended_at from sessions where id=?', (session_id,)).fetchone()
    require(row is not None, 'author session not authenticated')
    require(row[0] and row[0] != raw['model'] and session_id != raw['runtime']['native_session_id'],
            'writer cannot accept own claims (same model or session)')
    require(row[2] is not None and row[1] <= row[2] <= reviewed_at, 'author execution not completed before review')
    return {'session_id': session_id, 'model': row[0]}


def packet_authors(db, paths, raw, reviewed_at):
    """Authenticate real successful source edits, never a worker-supplied model name."""
    writes = []
    with sqlite3.connect(f'file:{db}?mode=ro', uri=True) as conn:
        # Successful write/patch results retain exact paths in files_modified/diff.
        # Read-only calls and terminal stdout claiming authorship are not evidence.
        # FTS excludes tool results but retains their caller. Find candidate
        # sessions by exact packet path, then use the messages session index.
        # Include old sessions: no timestamp window may hide a writing reviewer.
        indexed = conn.execute("select 1 from sqlite_master where name='messages_fts_trigram'").fetchone()
        if indexed:
            query = '"' + str(paths[0].parent).replace('"', '""') + '"'
            sessions = conn.execute('select distinct s.id,s.model from sessions s join messages m '
                'on m.session_id=s.id join messages_fts_trigram f on f.rowid=m.id '
                'where messages_fts_trigram match ?', (query,)).fetchall()
        else:
            sessions = conn.execute('select id,model from sessions').fetchall()
        for sid, model in sessions:
            for mid, content, when in conn.execute(
                    'select id,content,timestamp from messages where session_id=? '
                    'and role="tool" and tool_name in ("write_file","patch")', (sid,)):
                if not content or not any(str(path) in content for path in paths):
                    continue
                value = json.loads(content)
                if value.get('success') is not True and value.get('verified') is not True:
                    continue
                require(when <= reviewed_at, 'source edited after review seal')
                require(model and model != raw['model'] and sid != raw['runtime']['native_session_id'],
                        'writer cannot accept own claims (same model or session)')
                writes.append({'session_id': sid, 'model': model, 'message_id': mid,
                               'message_sha256': hashlib.sha256(content.encode()).hexdigest()})
    require(writes, 'authenticated packet writer edits missing; retain writer provenance during refill')
    return sorted({row['message_id']: row for row in writes}.values(), key=lambda row: row['message_id'])


def acceptance(carrier_path, *, natural=False, root=None, session_db=None):
    root = Path(root or ROOT).resolve()
    reports = root / 'reports'
    carrier_path = Path(carrier_path).resolve(strict=True)
    require(carrier_path.parent == reports / 'ops/n8n-to-cron-20260924/runs'
            and carrier_path.name.startswith('article_carrier_consume-'), 'unregistered carrier receipt')
    run = read(carrier_path)
    validate_carrier(run, natural=natural)
    bridge = run.get('bridge') or {}
    require(bridge.get('status') == 'done' and bridge.get('rc') == 0 and bridge.get('timed_out') is False,
            'carrier bridge did not succeed')
    heartbeat = ((run['effects'].get('heartbeat') or {}).get('data') or {}).get('rows') or []
    require(len(heartbeat) == 1 and heartbeat[0].get('machine') == 'article_carrier'
            and heartbeat[0].get('output_count') == 1, 'carrier publication binding missing')
    consumed_path = Path(heartbeat[0]['receipt']).resolve(strict=True)
    buffer = reports / 'glp-clinic-scout/seo-machine/article-buffer'
    require(consumed_path.parent == buffer / 'consumed', 'unregistered consumption receipt')
    scripts = root / 'systems/seo-blog-machine/scripts'
    sys.path.insert(0, str(scripts))
    import article_buffer as ab
    import opus_review_loop as loop
    consumption = ab._read_sealed(consumed_path, ab.CONSUME_SCHEMA, 'consumption_sha256')
    require(consumption.get('status') == 'PASS_BUFFER_PUBLISHED', 'not a published consumption')
    messages = []
    for line in str(bridge.get('stdout_tail') or '').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('schema') == 'n8n.article_carrier.v1':
            messages.append(row)
    require(len(messages) == 1 and messages[0].get('operation_id') == consumption.get('operation_id'),
            'carrier and published operation differ')
    if natural:
        require(messages[0].get('status') == 'PASS_PUBLISHED', 'scheduled run recovered a refusal, not a publish')
    entry_id = consumption.get('entry_id')
    require(re.fullmatch(r'[a-f0-9]{24}', str(entry_id)) is not None, 'entry id invalid')
    entry_path = buffer / 'entries' / entry_id / 'entry.json'
    entry = ab.validate_entry(entry_path, current_routes=set(), current_topic_ids=set())
    packet = entry_path.parent / 'packet'
    qa = read(ab._verify_spec(entry['qa'], 'independent_qa'))
    require(qa.get('status') == 'PASS' and qa.get('failures') == []
            and isinstance(qa.get('checks'), dict) and qa['checks']
            and all(value is True for value in qa['checks'].values()), 'claim QA not PASS')
    receipt_path = ab._verify_spec(entry['pass_receipt'], 'independent_pass')
    raw_path = ab._verify_spec(entry['raw_review'], 'independent_raw')
    manifest_path = ab._verify_spec(entry['subject_manifest'], 'independent_manifest')
    receipt, raw, manifest = read(receipt_path), read(raw_path), read(manifest_path)
    require(receipt.get('schema') == 'glp-opus-seo-round-receipt-v1'
            and receipt.get('receipt_sha256') == ab._opus_payload_hash(receipt, 'receipt_sha256')
            and receipt.get('status') == 'PASS_OPUS_SEO_DRAFT'
            and receipt.get('tools_disabled') is True, 'five-gate receipt invalid')
    require(manifest.get('manifest_sha256') == ab._opus_payload_hash(manifest, 'manifest_sha256')
            and receipt.get('subject_manifest_sha256') == sha(manifest_path)
            and receipt.get('raw_review_sha256') == sha(raw_path), 'review/manifest binding invalid')
    ab._subject_rows(packet, manifest)
    payload = loop.parse_result(raw)
    loop.validate_review(payload, raw, manifest, receipt['round'])
    require(payload['verdict'] == receipt['status'] and payload['gates'] == receipt['gates']
            and payload['findings'] == receipt['findings'] == []
            and all(row['status'] == 'PASS' and not row['findings'] for row in receipt['gates'])
            and receipt.get('model') == raw.get('hermes_runtime', {}).get('model')
            and receipt.get('provider') == raw.get('hermes_runtime', {}).get('provider'),
            'raw independent decision differs from five-gate PASS')
    publication = consumption.get('publication') or {}
    source = publication.get('source_entry') or {}
    hashes = {row['path']: row['sha256'] for row in entry['packet_files']}
    require(source.get('entry_id') == entry_id and source.get('route') == entry['target_route']
            and consumption.get('target_route') == entry['target_route']
            and source.get('article', {}).get('sha256') == hashes['06-article.json']
            and source.get('qa', {}).get('sha256') == hashes['07-qa-receipt.json']
            and source.get('review', {}).get('sha256') == sha(receipt_path),
            'published bytes not bound to accepted packet')
    live = publication.get('live') or {}
    require(live.get('http') == 200 and live.get('url') == 'https://glpclinicscout.com' + entry['target_route']
            and publication.get('deployment_id'), 'exact published route/deployment missing')
    original_packet = Path(receipt['subject_manifest_path']).resolve().parent.parent / 'packet'
    author_paths = [original_packet / row['path'] for row in manifest['subject_files']
                    if row['path'] in ab.REQUIRED_SUBJECTS - {'06-article.json', '07-qa-receipt.json'}]
    for path in author_paths:
        require(path.is_file() and sha(path) == hashes[path.name], 'original author packet drifted')
    import hermes_sol_article_review as reviewer_transport
    recovery = None
    original_review = spec(receipt_path)
    if 'result_custody' not in raw.get('hermes_runtime', {}):
        # Explicit manual, post-publication supplement. Original entry/claim and
        # all historical review checks above remain immutable. This is not
        # evidence of an authenticated review before the original publication.
        from article_review_recovery import validate_recovery
        recovered = validate_recovery(entry_path, root=root, natural=natural,
                                      session_db=session_db or SESSION_DB)
        recovery = recovered['recovery']
        receipt, raw, manifest = recovered['receipt'], recovered['raw'], recovered['manifest']
        receipt_path = Path(recovered['document']['review']['path'])
        raw_path = Path(recovered['document']['raw_review']['path'])
        manifest_path = Path(recovered['document']['subject_manifest']['path'])
        custody = recovered['custody']
    else:
        custody = reviewer_transport.validate_result_custody(
            raw, manifest=manifest, packet=packet, session_db=session_db or SESSION_DB)
    require(timestamp(manifest['sealed_at_utc']) <= custody['started_at']
            and custody['ended_at'] <= timestamp(receipt['reviewed_at_utc']),
            'reviewer execution outside sealed review interval')
    runtime = raw['hermes_runtime']
    reviewer = {'model': runtime['model'], 'provider': runtime['provider'], 'session_id': runtime['session_id'],
            'postpublication_review': recovery is not None, 'result_custody': custody}
    authors = packet_authors(session_db or SESSION_DB, author_paths,
                             {'model': reviewer['model'], 'runtime': {'native_session_id': reviewer['session_id']}},
                             timestamp(manifest['sealed_at_utc']))
    return {'schema': SCHEMA, 'status': 'PASS', 'carrier': spec(carrier_path),
            'consumption': spec(consumed_path), 'entry': spec(entry_path),
            'packet_closure_sha256': entry['packet_closure_sha256'],
            'article_sha256': hashes['06-article.json'], 'review': spec(receipt_path),
            'raw_review': spec(raw_path), 'subject_manifest': spec(manifest_path),
            'authors': authors, 'reviewer': reviewer,
            'original_review': original_review, 'postpublication_recovery': recovery,
            'url': live['url'], 'deployment_id': publication['deployment_id'], 'natural_due_slot': natural}


def validate(proof, *, natural, root=None):
    require(isinstance(proof, dict) and proof.get('schema') == SCHEMA, 'independent receipt missing')
    require(acceptance(proof['carrier']['path'], natural=natural, root=root) == proof,
            'independent receipt changed or unbound')
