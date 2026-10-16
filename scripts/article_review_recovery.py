#!/usr/bin/env python3
"""Fresh post-publication review of a repaired successor packet.

Never authenticates absent historical custody and never rewrites the original
entry, claim, QA or review. The successor contract replaces strict
eight-original-byte equality, which genuinely regenerated 06/07 metadata cannot
satisfy:

* identity subjects (00 authority, 02 SERP) stay byte-identical to the original;
* reviewed source repairs (01, 03, 04, 05) are free but must be the exact bytes
  in the sealed fresh PASS manifest, authored by a non-reviewer session;
* 06 must equal what the INSTALLED compiler regenerates from those sources;
* 07 must be a full-PASS validator receipt over exactly those 00-06 bytes;
* the reviewed candidate HTML (08) must project onto the deployed page, and the
  deployed release source must carry exactly these 06/07 bytes and this review.
Copying old QA into the successor cannot manufacture equality: 07 must hash the
successor 06 and the recompiled 06 must match.

The additive review-only supplement admits no source repairs and never replaces
published article/QA/review bytes. Its native-reviewed candidate must equal the
complete deployed file. Current compiler/QA and independent native custody stay
mandatory; preparation alone is neither a PASS nor permission to invoke/publish.
"""
import argparse
import fcntl
import hashlib
import html as htmllib
import json
import re
import sys
from pathlib import Path

ROOT = Path('/home/ubuntu/business')
SCHEMA = 'glp-postpublication-review-recovery-v2'
SUPPLEMENT_SCHEMA = 'glp-postpublication-review-supplement-v1'
SUBJECTS = {'00-authority.json', '01-evidence-ledger.json', '02-serp-brief.json',
            '03-editorial-brief.md', '04-draft.md', '05-claim-source-map.json',
            '06-article.json', '07-qa-receipt.json'}
CANDIDATE = '08-candidate-guide.html'
CORE = tuple(sorted(SUBJECTS))
IDENTITY = ('00-authority.json', '02-serp-brief.json')
SOURCE_REPAIRS = ('01-evidence-ledger.json', '03-editorial-brief.md', '04-draft.md', '05-claim-source-map.json')


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


def require_live_matches_sealed(raw: bytes, expected: bytes):
    # Delegate CF authentication to its maintained owner. Recovery never strips
    # scripts or invents a second raw-response oracle.
    from glp_deploy_glp34_authority_v1 import current_restore_cf_email
    compared = current_restore_cf_email(raw, expected=expected) if raw != expected else raw
    require(compared == expected, 'reviewed_live_deployment_drift')


def site_wording(text):
    """The site build's global dated-wording pass (scripts/build-staging.mjs)."""
    text = re.sub(r'\bwas checked (2026-\d{2}-\d{2})(?!\d)', r'is current as of \1', text, flags=re.I)
    text = re.sub(r'\bchecked through (2026-\d{2}-\d{2})(?!\d)', r'current through \1', text, flags=re.I)
    text = re.sub(r'\bchecked (2026-\d{2}-\d{2})(?!\d)', r'current as of \1', text, flags=re.I)
    return re.sub(r'\brelated coverage\b', 'connected coverage', text, flags=re.I)


def article_projection(page):
    """Reviewed article region, compared as exact (normalized) markup.

    Covers the meta description, the H1, and one contiguous body from the
    article's lead content (evidence band when present, else the quick-answer
    section) through the source list: evidence dates, quick answer, medical /
    evidence-boundary disclosure, TOC, sections, FAQ and sources. Only the site
    shell outside that region (navigation, hero imagery, share links, footer)
    and dated-wording differences are excluded. Inputs here are sealed HTML;
    live CF transformations are authenticated separately by the owning oracle.
    """
    page = site_wording(page)
    meta = re.findall(r'<meta name="description" content="([^"]*)"', page)
    h1 = re.findall(r'<h1\b[^>]*>(.*?)</h1>', page, flags=re.S)
    answer = page.find('<section class="answer">')
    band = page.find('<p class="guide-evidence-band">')
    toc = page.find('<nav class="guide-toc"')
    disclosure = page.find('<aside class="disclosure">')
    anchor = page.find('<section class="guide-sources" id="sources">')
    end = page.find('</section>', anchor) if anchor >= 0 else -1
    start = band if 0 <= band < answer else answer
    require(len(meta) == 1 and len(h1) == 1 and page.count('<section class="answer">') == 1
            and 0 <= start <= answer < disclosure < toc < anchor < end
            and page.find('<h1') < start, 'article_projection_missing')
    return {'meta_description': htmllib.unescape(meta[0]), 'h1': h1[0],
            'body': page[start:end + len('</section>')]}


def require_live_matches_candidate(live, candidate):
    """The deployed article region must equal the independently reviewed candidate."""
    require(article_projection(live) == article_projection(candidate), 'live_differs_from_reviewed_candidate')


PUBLICATION_CONTEXT = '09-publication-context.json'


def publication_context_contract(original_packet, fresh, old, *, root):
    """Bind the native-reviewed supplement to the immutable original publication.

    This is historical context, never release authority or a replacement for the
    native ten-subject custody and complete live-byte checks in validate_documents.
    """
    from datetime import datetime
    from email.utils import parsedate_to_datetime
    from zoneinfo import ZoneInfo
    ctx = read(fresh / PUBLICATION_CONTEXT)
    require(ctx.get('scope') == 'POSTPUBLICATION_RECOVERY_REVIEW_ONLY_NOT_NEW_PUBLICATION'
            and ctx.get('no_invocation_authority') is True, 'publication_context_scope')
    require(ctx['fresh_native_build']['status'] == 'PASS_NATIVE_BUILD'
            and ctx['fresh_native_build']['returncode'] == 0
            and ctx['fresh_native_build']['candidate']['sha256'] == sha(fresh / CANDIDATE),
            'publication_context_candidate_binding')
    require(all(sha(original_packet / name) == digest for name, digest in old.items()),
            'publication_context_original_bytes')
    buffer = root / 'reports/glp-clinic-scout/seo-machine/article-buffer'
    claim = checked(ctx['original_consumption']['claim'])
    require(claim.parent == buffer / 'claims' and re.fullmatch(r'[a-f0-9]{64}\.json', claim.name),
            'publication_context_claim')
    consumed_path = buffer / 'consumed' / claim.name
    consumed = read(consumed_path)
    unsigned = {k: v for k, v in consumed.items() if k != 'consumption_sha256'}
    # article_buffer's sealed consumption canonical format includes its newline.
    digest = hashlib.sha256((json.dumps(unsigned, sort_keys=True, separators=(',', ':'),
                                       ensure_ascii=False) + '\n').encode()).hexdigest()
    require(consumed == ctx['original_consumption']
            and consumed.get('schema') == 'glp-seo-article-buffer-consumption-v1'
            and consumed.get('status') == 'PASS_BUFFER_PUBLISHED'
            and consumed.get('consumption_sha256') == digest
            and original_packet == buffer / 'entries' / consumed['entry_id'] / 'packet',
            'publication_context_original_consumption')
    publication = consumed['publication']
    terminal_path = Path(publication['receipt']).resolve(strict=True)
    require(terminal_path.name == '10-final-publication-receipt.json'
            and terminal_path.parent.parent == buffer.parent
            and terminal_path.parent.name.startswith('current-release-'),
            'publication_context_original_release')
    first = ctx['first_publication']
    evidence = first['evidence']
    expected_paths = [terminal_path.parent / 'CURRENT-RECEIPT.json', terminal_path, consumed_path]
    require(len(evidence) == 4 and evidence[:3] == [
        {'path': str(p.relative_to(root)), 'sha256': sha(p)} for p in expected_paths],
        'publication_context_publication_evidence')
    header_path = root / evidence[3]['path']
    require(header_path.resolve().is_relative_to(root / 'reports'), 'publication_context_header_path')
    headers = checked({'path': str(header_path), 'sha256': evidence[3]['sha256']}).read_text()
    terminal, current = read(terminal_path), read(expected_paths[0])
    deployment = current['stages']['pages_deployment']
    intent = current['stages']['pages_deploy_intent']
    require(ctx['original_terminal'] == terminal
            and terminal.get('schema') == 'glp-opus-production-terminal-receipt-v1'
            and terminal.get('status') == current.get('status') == 'PASS_PRODUCTION_TERMINAL'
            and terminal['deployment']['deployment_id'] == deployment['deployment_id']
            == publication['deployment_id'] == first['deployment_id']
            and terminal['site_head'] == current['site_head'] == first['site_head']
            == deployment['commit_hash'] == intent['commit_hash'], 'publication_context_deployment_binding')
    earliest = datetime.fromisoformat(first['not_before_utc'].replace('Z', '+00:00'))
    latest = datetime.fromisoformat(first['observed_live_by_utc'].replace('Z', '+00:00'))
    observed_dates = re.findall(r'^date:\s*(.+)$', headers, re.I | re.M)
    require(first.get('timezone') == 'America/Los_Angeles'
            and first['not_before_utc'] == intent['created_at_utc']
            and len(observed_dates) == 1 and earliest <= latest == parsedate_to_datetime(observed_dates[0])
            and earliest.astimezone(ZoneInfo(first['timezone'])).date().isoformat()
            == latest.astimezone(ZoneInfo(first['timezone'])).date().isoformat() == first['date'],
            'publication_context_publication_dates')
    return sha(fresh / PUBLICATION_CONTEXT)


def successor_contract(original, fresh, rows, manifest, *, compile_packet, root=None):
    """Positively exercised derivation from the original packet to the successor."""
    expected = {row['path']: row['sha256'] for row in rows}
    actual = {row['path']: row['sha256'] for row in manifest['subject_files']}
    require(set(expected) == SUBJECTS and len(rows) == len(expected), 'original_subject_set')
    require(set(actual) in (SUBJECTS | {CANDIDATE}, SUBJECTS | {CANDIDATE, PUBLICATION_CONTEXT})
            and len(manifest['subject_files']) == len(actual), 'recovery_subject_set')
    for name, digest in actual.items():
        require(sha(fresh / name) == digest, 'successor_subject_drift')
    for name in IDENTITY:
        require(actual[name] == expected[name] == sha(original / name), 'identity_subject_changed')
    article = compile_packet(fresh)
    regenerated = json.dumps(article, indent=2, sort_keys=True) + '\n'
    require(regenerated.encode() == (fresh / '06-article.json').read_bytes(), 'successor_article_not_compiled')
    require(article.get('render_contract', {}).get('schema') == 'glp-accepted-guide-render-v1',
            'successor_render_contract_missing')
    qa = read(fresh / '07-qa-receipt.json')
    require(qa.get('status') == 'PASS' and qa.get('failures') == [] and qa.get('checks')
            and all(v is True for v in qa['checks'].values()), 'successor_qa_not_PASS')
    bound = {name: row['sha256'] for name, row in (qa.get('artifacts') or {}).items()}
    require(bound == {name: actual[name] for name in SUBJECTS - {'07-qa-receipt.json'}}, 'successor_qa_unbound')
    require(qa['checked_at_utc'] <= manifest['sealed_at_utc'], 'successor_qa_after_seal')
    changed = sorted(name for name in SOURCE_REPAIRS if actual[name] != expected[name])
    result = {'identity': list(IDENTITY), 'source_repairs': changed,
              'article_regenerated_sha256': hashlib.sha256(regenerated.encode()).hexdigest(),
              'qa_sha256': actual['07-qa-receipt.json']}
    if PUBLICATION_CONTEXT in actual:
        result['publication_context_sha256'] = publication_context_contract(
            original, fresh, expected, root=Path(root or ROOT).resolve())
    return result


def recovery_path(entry_path, root):
    ident = Path(entry_path).parent.name
    require(re.fullmatch(r'[a-f0-9]{24}', ident), 'entry_id_invalid')
    return root / 'reports/glp-clinic-scout/seo-machine/article-review-recovery' / ident / 'RECOVERY.json'


def authenticate_source_origin(packet, proof, names, root, database, before):
    """Origin provenance only: authenticate an old native input's sealed hashes."""
    import argparse
    import hermes_sol_article_review as transport
    import opus_review_loop as loop
    manifest_path = checked(proof['manifest'])
    raw_path = checked(proof['raw'])
    require(packet.is_dir() and not packet.is_symlink()
            and packet.resolve().is_relative_to((root / 'reports').resolve()), 'copied_source_review_path')
    manifest, raw = read(manifest_path), read(raw_path)
    require(manifest['manifest_sha256'] == loop.payload_hash(manifest, 'manifest_sha256'), 'copied_source_seal')
    rows = {row['path']: row for row in manifest['subject_files']}
    require(all(n in rows and sha(packet/n) == rows[n]['sha256'] for n in names), 'copied_source_sealed_bytes')
    inputs, runtime = raw['review_input'], raw['hermes_runtime']
    args = argparse.Namespace(prompt=inputs.get('inline_prompt'),
        prompt_file=(inputs.get('prompt_file') or {}).get('path'),
        context_file=[v['path'] for v in inputs['context_files']],
        json_schema_file=(inputs.get('json_schema_file') or {}).get('path'),
        max_context_chars=inputs['max_context_chars'], max_total_chars=inputs['max_total_chars'],
        model=runtime['model'], provider=runtime['provider'], effort=runtime['effort'], disable_tools=True)
    captured = {}
    payload = transport.build_payload(args, captured, transport.load_schema(args, captured))
    require(transport.review_input(args, payload, captured, runtime) == inputs, 'copied_source_input')
    require(len(captured['context_files']) == 1, 'copied_source_context_count')
    context = captured['context_files'][0]['content']
    digest = manifest['manifest_sha256']
    headers = ('SEALED ARTICLE SUBJECTS — READ ONLY\nmanifest_sha256=' + digest + '\n',
               '# SEALED PACKET CONTENT — ALREADY IN BAND\n\nSubject manifest SHA-256: ' + digest + '\n')
    require(context.startswith(headers), 'copied_source_context_seal')
    from article_review_physical_lines import physical_lines
    for name in names:
        lines = physical_lines((packet / name).read_bytes())
        numbered = '\n'.join(f'{i:04d}|{line}' for i, line in enumerate(lines, 1))
        legacy_lines = '\n'.join(f'{i}|{line}' for i, line in enumerate(lines, 1))
        section = (f'===== {name} =====\n{numbered}\n' if context.startswith(headers[0])
                   else f'## FILE: {name}\n\n{legacy_lines}\n')
        require(section in context, 'copied_source_native_subject_bytes')
    custody = transport.authenticate_result(runtime['session_id'], payload, raw['result'],
        model=runtime['model'], provider=runtime['provider'], effort=runtime['effort'], session_db=database)
    require(custody == runtime['result_custody'], 'copied_source_native_custody')
    from article_health_acceptance import timestamp
    sealed = timestamp(manifest['sealed_at_utc'])
    require(sealed <= custody['started_at'] <= custody['ended_at'] <= before, 'copied_source_chronology')
    return sealed


def copied_source_authors(fresh, names, origins, root, database, reviewer, sealed_at, *, authenticate=None):
    """Bind copied subjects to their original paths, retaining native writer checks."""
    if authenticate is None:
        from article_health_acceptance import packet_authors
        authenticate = packet_authors
    require(not origins or set(origins) == set(names), 'copied_source_name_set')
    sources = []
    for name in names:
        source = fresh / name
        if origins:
            supplied = Path(origins[name]['path'])
            require(supplied.is_absolute() and not supplied.is_symlink(), 'copied_source_path')
            source = checked(origins[name])
            require(source.name == name and source.is_relative_to((root / 'reports').resolve())
                    and source.read_bytes() == (fresh / name).read_bytes(), 'copied_source_bytes')
        sources.append(source)
    return authenticate(database, sources, reviewer, sealed_at)


def successor_source_authors(fresh, names, origins, proof, root, database, reviewer, sealed_at):
    """Authenticate byte-identical aliases at their old seal, new edits at this seal."""
    require(set(origins) <= set(names), 'copied_source_name_set')
    authors = []
    if origins:
        require(len({Path(v['path']).parent for v in origins.values()}) == 1,
                'copied_source_directory_set')
        copied_names = sorted(origins)
        alias_packet = Path(next(iter(origins.values()))['path']).parent
        origin_seal = authenticate_source_origin(alias_packet, proof, copied_names,
                                                root, database, sealed_at)
        authors.extend(copied_source_authors(fresh, copied_names, origins, root,
                                             database, reviewer, origin_seal))
    direct_names = sorted(set(names) - set(origins))
    if direct_names:
        authors.extend(copied_source_authors(fresh, direct_names, {}, root,
                                             database, reviewer, sealed_at))
    return authors


def validate_successor_documents(entry_path, doc, *, root=ROOT, session_db=None):
    """Authenticate retained subjects only, never deployment/recovery acceptance."""
    import hermes_sol_article_review as transport
    sys.path.insert(0, str(root / 'systems/seo-blog-machine/scripts'))
    import opus_review_loop as loop
    import compile_review_packet_article as compiler
    entry = read(entry_path)
    require(doc['schema'] in (SCHEMA, SUPPLEMENT_SCHEMA)
            and doc['status'] == 'PASS_POSTPUBLICATION_REVIEW', 'recovery_schema')
    require(doc.get('natural_acceptance') is False and doc.get('historical_custody_repaired') is False,
            'recovery_scope')
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
    fresh = checked(doc['candidate']).parent
    require(doc['candidate']['path'] == str(fresh / CANDIDATE), 'candidate_path')
    derivation = successor_contract(entry_path.parent / 'packet', fresh, entry['packet_files'], manifest,
                                    compile_packet=compiler.compile_packet, root=root)
    require(doc['successor'] == derivation, 'successor_derivation_changed')
    if doc['schema'] == SUPPLEMENT_SCHEMA:
        require(not derivation['source_repairs'], 'supplement_cannot_repair_source')
        supplement_job = read(checked(doc['review_only_job']))
        loop.validate_review_only_job(supplement_job, apply=False, review_only_once=True)
        require(supplement_job['artifact_role'] == 'postpublication_review_only_job'
                and Path(supplement_job['packet_dir']).resolve(strict=True) == fresh
                and receipt['job_id'] == manifest['job_id'] == supplement_job['job_id'],
                'supplement_review_job_binding')
        import review_transports
        route = review_transports.transport(supplement_job['review_transport'])
        require(raw['hermes_runtime']['transport_id'] == receipt['review_transport'] == route['transport_id'],
                'supplement_native_route_mismatch')
        validate_review_only_binding(supplement_job, route, root=root, session_db=session_db)
        intent_path = Path(supplement_job['history_dir'])/'native-review-intent.json'
        intent = read(checked(doc['native_review_intent']))
        require(checked(doc['native_review_intent']) == intent_path.resolve(strict=True)
                and intent['job_id'] == receipt['job_id']
                and intent['subject_manifest_sha256'] == sha(manifest_path)
                and intent.get('retry_allowed') is False,
                'supplement_single_native_intent')
    payload = loop.parse_result(raw)
    loop.validate_review(payload, raw, manifest, receipt['round'])
    require(payload['verdict'] == receipt['status'] == 'PASS_OPUS_SEO_DRAFT'
            and payload['gates'] == receipt['gates'] and payload['findings'] == receipt['findings'] == []
            and all(g['status'] == 'PASS' and not g['findings'] for g in receipt['gates']), 'fresh_review_not_PASS')
    custody = transport.validate_result_custody(raw, manifest=manifest, packet=fresh, session_db=session_db)
    from article_health_acceptance import timestamp, packet_authors
    require(timestamp(manifest['sealed_at_utc']) <= custody['started_at']
            and custody['ended_at'] <= timestamp(receipt['reviewed_at_utc']), 'fresh_review_chronology')
    runtime = raw['hermes_runtime']
    successor_authors = successor_source_authors(
        fresh, derivation['source_repairs'], doc.get('source_authorship', {}),
        doc.get('source_authorship_review'), root, database,
        {'model': runtime['model'], 'runtime': {'native_session_id': runtime['session_id']}},
        timestamp(manifest['sealed_at_utc']))
    return {'receipt': receipt, 'raw': raw, 'manifest': manifest, 'custody': custody, 'document': doc,
            'successor_authors': successor_authors}


def validate_supplement_deployed_source(entry_path, doc, release, fresh):
    """A supplement cannot replace, update or newly publish the accepted source."""
    from article_source_carrier import source_rows
    entry = read(entry_path)
    job = read(checked(doc['review_only_job']))
    binding = job['postpublication_binding']
    rows = [r for r in source_rows(release) if r.get('entry_id') == entry['entry_id']
            and r.get('route') == entry['target_route']]
    require(len(rows) == 1 and rows[0] == binding['deployed_source'], 'supplement_published_source_drift')
    row = rows[0]
    originals = {'article': entry_path.parent/'packet/06-article.json',
                 'qa': entry_path.parent/'packet/07-qa-receipt.json',
                 'review': checked(entry['pass_receipt'])}
    for key, original in originals.items():
        source = release/row[key]['path']
        require(source.resolve(strict=True).is_relative_to(release.resolve(strict=True))
                and not source.is_symlink() and sha(source) == row[key]['sha256'] == sha(original),
                'supplement_original_published_source_changed')
    live_file = release/'public'/entry['target_route'].strip('/')/'index.html'
    require(live_file.read_bytes() == (fresh/CANDIDATE).read_bytes(), 'supplement_complete_candidate_drift')


def validate_documents(entry_path, doc, *, root=ROOT, session_db=None):
    result = validate_successor_documents(entry_path, doc, root=root, session_db=session_db)
    entry = read(entry_path)
    fresh = checked(doc['candidate']).parent
    round_path = checked(doc['review'])
    pointer = read(root / 'reports/glp-clinic-scout/seo-machine/current-content-base.json')
    release = Path(pointer['release_root'])
    if doc['schema'] == SUPPLEMENT_SCHEMA:
        validate_supplement_deployed_source(entry_path, doc, release, fresh)
    else:
        rows = [r for r in read(release / 'data/blog/accepted-guides/updates.json')['entries']
                if r['route'] == entry['target_route']]
        require(len(rows) == 1 and rows[0]['article']['sha256'] == sha(fresh / '06-article.json')
                and rows[0]['qa']['sha256'] == sha(fresh / '07-qa-receipt.json')
                and rows[0]['review']['sha256'] == sha(round_path)
                and all(sha(release / rows[0][k]['path']) == rows[0][k]['sha256'] for k in ('article', 'qa', 'review')),
                'deployed_source_not_successor')
    binding = doc['live_binding']
    live_file = release / 'public' / entry['target_route'].strip('/') / 'index.html'
    snapshot = checked(doc['live_snapshot'])
    require(binding['url'] == 'https://glpclinicscout.com' + entry['target_route']
            and binding['http_status'] == 200 and binding['deployment_id'] == pointer['deployment_id']
            and sha(live_file) == binding['deployed_sha256'] and sha(snapshot) == binding['live_sha256'],
            'reviewed_live_deployment_drift')
    require_live_matches_sealed(snapshot.read_bytes(), live_file.read_bytes())
    require_live_matches_candidate(live_file.read_text(), (fresh / CANDIDATE).read_text())
    return result


def validate_recovery(entry_path, *, root=ROOT, natural=False, session_db=None):
    require(not natural, 'postpublication_review_not_natural')
    entry_path = Path(entry_path).resolve(strict=True)
    path = recovery_path(entry_path, root)
    result = validate_documents(entry_path, read(path), root=root, session_db=session_db)
    result['recovery'] = spec(path)
    return result


def successor_documents(entry_path, history, packet, *, root=ROOT,
                        source_author_packet=None, source_author_review=None, review_only_job_path=None):
    """Build in-memory validator inputs; do not write a receipt or invoke a model."""
    sys.path.insert(0, str(root / 'systems/seo-blog-machine/scripts'))
    import compile_review_packet_article as compiler
    entry = read(entry_path)
    rounds = sorted(history.glob('round-*-receipt.json'))
    require(len(rounds) == 1, 'exact_single_review_required')
    receipt = read(rounds[0])
    manifest = read(receipt['subject_manifest_path'])
    doc = {'schema': SCHEMA, 'status': 'PASS_POSTPUBLICATION_REVIEW',
           'entry': spec(entry_path), 'original_review': entry['pass_receipt'],
           'original_raw_review': entry['raw_review'], 'review': spec(rounds[0]),
           'raw_review': spec(receipt['raw_review_path']), 'subject_manifest': spec(receipt['subject_manifest_path']),
           'candidate': spec(packet / CANDIDATE),
           'successor': successor_contract(entry_path.parent / 'packet', packet, entry['packet_files'], manifest,
                                           compile_packet=compiler.compile_packet, root=root),
           'historical_custody_repaired': False, 'natural_acceptance': False}
    if source_author_packet is not None:
        origin = Path(source_author_packet)
        require(origin.is_absolute() and origin.is_dir() and not origin.is_symlink(), 'copied_source_directory')
        names = [n for n in doc['successor']['source_repairs']
                 if (origin / n).is_file() and (origin / n).read_bytes() == (packet / n).read_bytes()]
        require(names, 'copied_source_matching_subjects_missing')
        require(all(not (origin / n).is_symlink() for n in names), 'copied_source_symlink')
        doc['source_authorship'] = {n: spec(origin / n) for n in names}
        require(source_author_review is not None, 'copied_source_native_review_required')
        origin_review = read(source_author_review)
        doc['source_authorship_review'] = {
            'manifest': spec(origin_review['subject_manifest_path']),
            'raw': spec(origin_review['raw_review_path'])}
    else:
        require(source_author_review is None, 'copied_source_review_without_packet')
    if review_only_job_path is not None:
        require(source_author_packet is None and source_author_review is None,
                'supplement_cannot_replace_authorship')
        doc.update(schema=SUPPLEMENT_SCHEMA, review_only_job=spec(review_only_job_path),
                   native_review_intent=spec(history/'native-review-intent.json'))
    return doc


def corrected_successor_documents(entry_path, history, packet, job_path, *, root=ROOT):
    """Authenticate a consumed corrected-review-only job, not deployed health.

    Reuse the native binding/blocked-history, compiler/QA and fixed-DB readers.
    Never extend its nine subjects, invoke transport, or issue a recovery seal.
    """
    import opus_review_loop as loop
    import review_transports
    import hermes_sol_article_review as native
    from article_health_acceptance import packet_authors, author_session, timestamp
    job_spec = spec(job_path)
    job = read(checked(job_spec))
    loop.validate_review_only_job(job, apply=False, review_only_once=True)
    binding = job.get('postpublication_binding') or {}
    require(job.get('artifact_role') == 'postpublication_review_only_job'
            and job.get('materialization_reason') == 'corrected_successor_review_only'
            and binding.get('review_subject') == 'corrected_successor'
            and Path(job['packet_dir']).resolve(strict=True) == packet
            and Path(job['history_dir']).resolve(strict=True) == history
            and checked(binding['entry']) == entry_path.resolve(strict=True),
            'corrected_successor_job_binding')
    require(list(history.glob('round-*-receipt.json')) == [history/'round-01-receipt.json'],
            'exact_single_review_required')
    receipt_spec = spec(history/'round-01-receipt.json')
    receipt = read(checked(receipt_spec))
    manifest_spec = {'path': receipt['subject_manifest_path'], 'sha256': receipt['subject_manifest_sha256']}
    raw_spec = {'path': receipt['raw_review_path'], 'sha256': receipt['raw_review_sha256']}
    manifest_path, raw_path = checked(manifest_spec), checked(raw_spec)
    require(manifest_path == history/'round-01-subject-manifest.json'
            and raw_path == history/'round-01-sol-raw.json', 'corrected_successor_history_paths')
    manifest, raw = read(manifest_path), read(raw_path)
    require(receipt.get('schema') == 'glp-opus-seo-round-receipt-v1'
            and receipt['receipt_sha256'] == loop.payload_hash(receipt, 'receipt_sha256')
            and manifest.get('schema') == 'glp-opus-seo-subject-manifest-v1'
            and manifest.get('status') == 'SEALED_FOR_OPUS_REVIEW'
            and manifest['manifest_sha256'] == loop.payload_hash(manifest, 'manifest_sha256')
            and receipt['round'] == manifest['round'] == 1
            and all(receipt[k] == manifest[k] == job[k] for k in ('job_id', 'topic_id')),
            'corrected_successor_review_binding')
    require(len(manifest['subject_files']) == 9
            and {r['path'] for r in manifest['subject_files']} == SUBJECTS | {CANDIDATE},
            'review_only_exact_subject_set')
    route = review_transports.transport(job['review_transport'])
    require(raw['hermes_runtime']['transport_id'] == receipt['review_transport'] == route['transport_id'],
            'corrected_successor_native_route')
    validate_review_only_binding(job, route, root=root)
    entry = read(checked(binding['entry']))
    import compile_review_packet_article as compiler
    derivation = successor_contract(entry_path.parent/'packet', packet, entry['packet_files'], manifest,
                                    compile_packet=compiler.compile_packet, root=root)
    payload = loop.parse_result(raw)
    loop.validate_review(payload, raw, manifest, 1)
    require(payload['verdict'] == receipt['status'] == 'PASS_OPUS_SEO_DRAFT'
            and payload['gates'] == receipt['gates'] and payload['findings'] == receipt['findings'] == []
            and [g['gate'] for g in receipt['gates']] == list('ABCDE')
            and all(g['status'] == 'PASS' and not g['findings'] for g in receipt['gates']),
            'fresh_review_not_PASS')
    # Mirror the maintained downstream receipt bar; a valid raw runtime cannot
    # launder contradictory receipt metadata into the staged source bytes.
    effects = receipt.get('effects')
    require(all(receipt.get(key) == route[key] for key in ('provider', 'model', 'effort'))
            and receipt.get('tools_disabled') is True
            and isinstance(effects, dict)
            and set(effects) == {'publishes', 'deployments', 'cache_purges', 'gsc_submissions'}
            and all(type(value) is int and value == 0 for value in effects.values()),
            'corrected_successor_receipt_contract')
    execution_spec = spec(history/'round-01-review-execution.json')
    execution = read(checked(execution_spec))
    custody = native.validate_result_custody(raw, manifest=manifest, packet=packet)
    # The fixed DB proves this exact raw session/input/result is authentic; the
    # retained execution marker selects WHICH authentic result consumed the sole
    # intent. Chronology alone would also admit a later, ungranted native retry.
    require(execution.get('raw_review_sha256') == raw_spec['sha256'],
            'corrected_successor_consumed_execution')
    intent_spec = spec(history/'native-review-intent.json')
    intent = read(checked(intent_spec))
    require(intent.get('schema') == 'glp-native-review-only-intent-v1'
            and intent['job_id'] == job['job_id']
            and intent['subject_manifest_sha256'] == manifest_spec['sha256']
            and intent.get('retry_allowed') is False and intent.get('publication_authorized') is False
            and timestamp(manifest['sealed_at_utc']) <= timestamp(intent['created_at_utc'])
            <= custody['started_at'] <= custody['ended_at'] <= timestamp(receipt['reviewed_at_utc']),
            'corrected_successor_single_native_intent')
    # Check ACTUAL reviewer model/session, not only the configured proposed route.
    runtime = raw['hermes_runtime']
    reviewer = {'model': runtime['model'], 'runtime': {'native_session_id': runtime['session_id']}}
    sealed = timestamp(manifest['sealed_at_utc'])
    original = read(checked(binding['original_job']))
    successor = binding['successor']
    groups = [([Path(original['packet_dir'])/n for n in CORE[:6]], binding['original_source_authors']),
              ([checked(successor['sources'][n]) for n in CORE[:6]], successor['source_authors'])]
    authors = []
    for paths, expected in groups:
        found = packet_authors(native.SESSION_DB, paths, reviewer, sealed)
        require(found == expected, 'review_only_native_author_drift')
        authors.extend(found)
    for name in sorted(set(CORE[:6]) - set(successor['original_aliases'])):
        packet_authors(native.SESSION_DB, [checked(successor['sources'][name])], reviewer, sealed)
    for sid in {a['session_id'] for a in authors}:
        author_session(native.SESSION_DB, sid, reviewer, sealed)
    inputs = [job_spec, binding['entry'], receipt_spec, manifest_spec, raw_spec, intent_spec, execution_spec]
    # Preserve the native reader's captured digests, not fresh snapshots taken
    # after authentication. Context/prompt/schema are dependencies, not extra
    # clinical subjects, and must survive custody through the final writer CAS.
    captured = raw['review_input']
    inputs += [row for row in [*captured['context_files'], captured.get('prompt_file'),
                              captured.get('json_schema_file')] if row is not None]
    inputs += [{'path': str(packet/r['path']), 'sha256': r['sha256']} for r in manifest['subject_files']]
    for value in inputs:
        checked(value)
    return {'receipt': receipt, 'raw': raw, 'manifest': manifest, 'custody': custody,
            'job': job, 'authenticated_inputs': inputs, 'successor_authors': authors,
            'document': {'entry': binding['entry'], 'review': receipt_spec, 'successor': derivation}}


def seal(entry_path, history, packet, binding_path, snapshot, *, root=ROOT,
         source_author_packet=None, source_author_review=None, review_only_job_path=None):
    doc = successor_documents(entry_path, history, packet, root=root,
                              source_author_packet=source_author_packet,
                              source_author_review=source_author_review,
                              review_only_job_path=review_only_job_path)
    doc.update(live_snapshot=spec(snapshot), live_binding=read(binding_path))
    validate_documents(entry_path, doc, root=root)
    path = recovery_path(entry_path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(doc, stream, indent=2, sort_keys=True)
        stream.write('\n')
    return path


def authenticate_blocked_history(blocked_spec, entry_spec, original_spec, *, root, session_db=None):
    """Read retained predecessor/native custody; a stable BLOCKED hash is not history."""
    from datetime import datetime
    import hermes_sol_article_review as native
    import opus_review_loop as loop
    receipt_path = checked(blocked_spec)
    receipt = read(receipt_path)
    require(receipt.get('schema') == 'glp-opus-seo-round-receipt-v1'
            and receipt.get('status') == 'BLOCKED_OPUS_SEO_DRAFT'
            and receipt.get('receipt_sha256') == loop.payload_hash(receipt, 'receipt_sha256'),
            'review_only_blocked_history_receipt')
    history = receipt_path.parent
    require(history.name == 'history' and receipt_path.name == 'round-01-receipt.json'
            and history.is_relative_to((Path(root)/'reports').resolve()), 'review_only_blocked_history_path')
    prior = read(history.parent/'job.json')
    origin = read(checked(original_spec))
    binding = prior.get('postpublication_binding') or {}
    require(prior.get('schema') == 'glp-opus-seo-review-job-v1'
            and prior.get('artifact_role') == 'postpublication_review_only_job'
            and prior.get('max_rounds') == 1 and not prior.get('publish') and not prior.get('repair')
            and Path(prior.get('history_dir', '')).resolve() == history
            and binding.get('entry') == entry_spec and binding.get('original_job') == original_spec
            and prior.get('topic_id') == origin['topic_id'], 'review_only_blocked_history_predecessor')
    packet = Path(prior['packet_dir']).resolve(strict=True)
    manifest_path = checked({'path': receipt['subject_manifest_path'], 'sha256': receipt['subject_manifest_sha256']})
    raw_path = checked({'path': receipt['raw_review_path'], 'sha256': receipt['raw_review_sha256']})
    require(manifest_path == history/'round-01-subject-manifest.json'
            and raw_path == history/'round-01-sol-raw.json', 'review_only_blocked_history_artifacts')
    manifest, raw = read(manifest_path), read(raw_path)
    require(manifest.get('schema') == 'glp-opus-seo-subject-manifest-v1'
            and manifest.get('status') == 'SEALED_FOR_OPUS_REVIEW'
            and manifest.get('manifest_sha256') == loop.payload_hash(manifest, 'manifest_sha256')
            and all(receipt.get(k) == manifest.get(k) == prior.get(k) for k in ('job_id', 'topic_id'))
            and receipt.get('round') == manifest.get('round') == 1, 'review_only_blocked_history_manifest')
    rows = manifest['subject_files']
    require(sorted(row['path'] for row in rows) == sorted([*CORE, CANDIDATE])
            and sorted(prior['subject_files']) == sorted([*CORE, CANDIDATE]), 'review_only_blocked_history_subjects')
    for row in rows:
        path = packet/row['path']
        require(path.is_file() and not path.is_symlink() and sha(path) == row['sha256']
                and path.stat().st_size == row['bytes']
                and len(loop.physical_lines(path.read_bytes())) == row['lines'], 'review_only_blocked_history_sealed_bytes')
    require(all(sha(packet/n) == sha(Path(origin['packet_dir'])/n) for n in CORE[:6])
            and sha(packet/CANDIDATE) == binding['deployed_candidate']['sha256'], 'review_only_blocked_history_original_subject')
    # Existing validation authenticates the exact in-band packet and completed
    # native no-tools turn. No receipt hash or alleged model is authority.
    result = loop.parse_result(raw)
    loop.validate_review(result, raw, manifest, 1)
    custody = native.validate_result_custody(raw, manifest=manifest, packet=packet, session_db=session_db)
    require(receipt['status'] == result['verdict']
            and all(receipt.get(k) == result[k] for k in ('gates', 'findings'))
            and receipt.get('tools_disabled') is True
            and receipt.get('review_transport') == prior.get('review_transport') == raw['hermes_runtime']['transport_id']
            and all(receipt.get(k) == raw['hermes_runtime'][k] for k in ('model', 'provider', 'effort')),
            'review_only_blocked_history_native_result')
    intent = read(history/'native-review-intent.json')
    stamp = lambda value: datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    require(intent.get('schema') == 'glp-native-review-only-intent-v1'
            and intent.get('job_id') == prior['job_id'] and intent.get('subject_manifest_sha256') == sha(manifest_path)
            and intent.get('retry_allowed') is False and intent.get('publication_authorized') is False
            and stamp(manifest['sealed_at_utc']) <= stamp(intent['created_at_utc']) <= custody['started_at']
            <= custody['ended_at'] <= stamp(receipt['reviewed_at_utc']), 'review_only_blocked_history_intent')


def review_only_successor_inputs(origin, packet, candidate, candidate_sha256,
                                 draft_sha256, blocked_review, *, root):
    """Pin explicit corrected bytes; original aliases/verdict are history, not subjects."""
    packet = Path(packet).resolve(strict=True)
    require(packet.is_relative_to(root/'reports') and packet != origin,
            'review_only_successor_packet_path')
    sources = {}
    for name in CORE[:6]:
        path = packet/name
        require(path.is_file() and not path.is_symlink(), 'review_only_successor_source_path')
        sources[name] = spec(path)
    candidate = Path(candidate)
    require(candidate.is_absolute() and not candidate.is_symlink()
            and candidate.resolve(strict=True).is_relative_to(root/'reports'),
            'review_only_successor_candidate_path')
    require(sha(candidate) == candidate_sha256 and sources['04-draft.md']['sha256'] == draft_sha256,
            'review_only_successor_exact_bytes_required')
    require(all(sources[n]['sha256'] == sha(origin/n) for n in IDENTITY),
            'identity_subject_changed')
    blocked = spec(blocked_review)
    require(read(checked(blocked)).get('status') == 'BLOCKED_OPUS_SEO_DRAFT',
            'review_only_original_BLOCKED_required')
    return {'sources': sources, 'candidate': spec(candidate), 'blocked_review': blocked,
            'original_aliases': {n: spec(origin/n) for n in CORE[:6]
                                 if sources[n]['sha256'] == sha(origin/n)}}


def validate_review_only_successor(entry_path, original, packet, successor, *, root, clock):
    import compile_review_packet_article as compiler
    checked(successor['blocked_review'])
    origin = Path(original['packet_dir']).resolve(strict=True)
    aliases = {}
    for name in CORE[:6]:
        source = checked(successor['sources'][name])
        require(sha(packet/name) == sha(source), 'review_only_successor_source_drift')
        if sha(source) == sha(origin/name):
            aliases[name] = spec(origin/name)
    require(aliases == successor['original_aliases'], 'review_only_successor_alias_drift')
    require(sha(packet/CANDIDATE) == sha(checked(successor['candidate'])),
            'review_only_successor_candidate_drift')
    manifest = {'sealed_at_utc': clock, 'subject_files': [
        {'path': n, 'sha256': sha(packet/n)} for n in [*CORE, CANDIDATE]]}
    return successor_contract(entry_path.parent/'packet', packet, read(entry_path)['packet_files'],
                              manifest, compile_packet=compiler.compile_packet, root=root)


def review_only_job(original, packet, history, route, *, validator, binding):
    """Emit data for the existing native controller, never a review decision."""
    import copy
    import opus_review_loop as loop
    job = copy.deepcopy(original)
    for key in ('publish', 'repair', 'bridge_extension'):
        job.pop(key, None)
    job.update(job_id=history.parent.name + '-postpublication-review-only',
               packet_dir=str(packet), history_dir=str(history), max_rounds=1,
               artifact_role='postpublication_review_only_job',
               materialization_reason='exact_published_entry_review_only',
               materialized_at_utc=loop.now(), review_transport=route['transport_id'],
               review_wrapper_sha256=sha(route['wrapper']), rubric_sha256=sha(loop.RUBRIC),
               review_schema_sha256=sha(loop.REVIEW_SCHEMA),
               subject_files=[*CORE, CANDIDATE], validator=validator,
               postpublication_binding=binding)
    job['prompt_intro'] = (
        'Independently review all five gates for the exact sealed already-published article. '
        'The candidate is the byte-exact deployed page, not a new build. The source inputs '
        'are immutable historical inputs; 06/07 are current deterministic compiler/QA outputs. '
        'Judge discrepancies, health/source safety, independent-publisher identity and all '
        'commercial/delivery requirements without treating any historical verdict as approval. '
        'No repair, publication, deployment, research, send or model retry is authorized. '
        'A source-safety decision is not clinical acceptance.')
    if binding.get('review_subject') == 'corrected_successor':
        job['materialization_reason'] = 'corrected_successor_review_only'
        job['prompt_intro'] = (
            'Independently review all five gates for the exact sealed corrected successor. '
            'The candidate and draft are corrected bytes, NOT the deployed article. '
            'Original publication bindings, byte-identical source aliases and the original '
            'BLOCKED verdict remain history only, never approval or the review subject. '
            '06/07 are current deterministic compiler/QA outputs from these sources. '
            'Judge health/source safety, independent-publisher identity and commercial/delivery '
            'requirements. No repair, publication, deployment, research, send or model retry '
            'is authorized. A source-safety decision is not clinical acceptance.')
    loop.validate_topic_authority(job)
    return job


def validate_review_only_binding(job, route, *, root=ROOT, session_db=None, for_invocation=False):
    """Read-only native/immutable admission immediately before the controller call."""
    from datetime import datetime, timezone
    from article_health_acceptance import packet_authors, author_session, SESSION_DB
    from weekly_strategy_consumer import load_strategy
    import review_transports
    require(route['transport_id'] in review_transports.NATIVE_INVOCATION_TRANSPORT_IDS
            and route['provider'] == 'openai-codex'
            and route == review_transports.transport(route['transport_id']),
            'review_only_registered_native_route_required')
    binding = job.get('postpublication_binding') or {}
    entry = read(checked(binding['entry']))
    original = read(checked(binding['original_job']))
    pointer_snapshot = checked(binding['current_pointer'])
    if for_invocation:
        require(pointer_snapshot.read_bytes() == (Path(root)/'reports/glp-clinic-scout/seo-machine/current-content-base.json').read_bytes(),
                'review_only_current_pointer_drift')
    candidate = checked(binding['deployed_candidate'])
    require(binding.get('publication_authorized') is False
            and binding.get('recovery_acceptance') is False, 'review_only_binding_effects')
    packet = Path(job['packet_dir']).resolve(strict=True)
    origin = Path(original['packet_dir']).resolve(strict=True)
    require(job['subject_files'] == [*CORE, CANDIDATE], 'review_only_exact_subject_set')
    successor = binding.get('successor')
    require(binding.get('review_subject', 'deployed_original') in ('deployed_original', 'corrected_successor')
            and bool(successor) == (binding.get('review_subject') == 'corrected_successor'),
            'review_only_subject_mode')
    for row in entry['packet_files']:
        if row['path'] in CORE[:6]:
            require(row['sha256'] == sha(origin/row['path'])
                    and (successor or sha(packet/row['path']) == row['sha256']),
                    'review_only_source_changed_before_review')
    if not successor:
        require(sha(packet/CANDIDATE) == sha(candidate), 'review_only_candidate_changed_before_review')
    proposed = {'model': route['model'], 'runtime': {'native_session_id': 'NOT_YET_INVOKED'}}
    clock = datetime.now(timezone.utc).timestamp()
    authors = packet_authors(session_db or SESSION_DB, [origin/n for n in CORE[:6]], proposed, clock)
    require(authors == binding['original_source_authors'], 'review_only_native_author_drift')
    for sid in {row['session_id'] for row in authors}:
        author_session(session_db or SESSION_DB, sid, proposed, clock)
    if successor:
        validate_review_only_successor(checked(binding['entry']), original, packet, successor,
                                      root=Path(root), clock=datetime.now(timezone.utc).isoformat())
        authenticate_blocked_history(successor['blocked_review'], binding['entry'], binding['original_job'],
                                     root=root, session_db=session_db)
        # Aggregate writers retain alias exclusions, but cannot prove coverage
        # of an independently changed source. Recheck each path at invocation.
        for name in sorted(set(CORE[:6]) - set(successor['original_aliases'])):
            packet_authors(session_db or SESSION_DB,
                [checked(successor['sources'][name])], proposed, clock)
        successor_authors = packet_authors(session_db or SESSION_DB,
            [checked(successor['sources'][n]) for n in CORE[:6]], proposed, clock)
        require(successor_authors == successor['source_authors'], 'review_only_native_successor_author_drift')
        for sid in {row['session_id'] for row in successor_authors}:
            author_session(session_db or SESSION_DB, sid, proposed, clock)
    _, strategy = load_strategy(Path(root)/'reports/glp-clinic-scout/weekly-strategy/current-strategy.json')
    require(strategy.get('applied') is True and strategy == binding['weekly_strategy'],
            'review_only_current_weekly_strategy_drift')


def prepare_review_only(entry_path, original_job, output, transport_id, *, root=ROOT,
                        session_db=None, successor_packet=None, successor_candidate=None,
                        candidate_sha256=None, draft_sha256=None, blocked_review=None):
    """Prepare one effect-free job without materializing a publication or calling a model.

    This is preparation only: normal host/quota/source-lock admission is still required
    before the native controller, and the unchanged recovery validator remains fail closed.
    Corrected subjects require all five successor arguments, including explicit candidate
    and draft SHA256 pins. Registered Sol61 remains Fast; registered Astra normal/default
    is standard speed. Neither registration waives author independence.
    """
    import subprocess
    from datetime import datetime, timezone
    import article_source_carrier as carrier
    from article_health_acceptance import packet_authors, author_session, SESSION_DB
    import opus_review_loop as loop
    import review_transports
    from weekly_strategy_consumer import load_strategy
    root = Path(root).resolve(strict=True)
    entry_path = Path(entry_path).resolve(strict=True)
    expected = root / 'reports/glp-clinic-scout/seo-machine/article-buffer/entries'
    require(entry_path.name == 'entry.json' and entry_path.parent.parent == expected,
            'review_only_entry_location')
    entry = read(entry_path)
    require(entry['entry_id'] == entry_path.parent.name, 'review_only_entry_identity')
    require(len(entry['packet_files']) == len(CORE)
            and {r['path'] for r in entry['packet_files']} == set(CORE), 'review_only_original_subject_set')
    for row in entry['packet_files']:
        subject = entry_path.parent/'packet'/row['path']
        require(subject.is_file() and not subject.is_symlink() and sha(subject) == row['sha256'],
                'review_only_original_subject_drift')
    original = read(original_job)
    require(original.get('schema') == 'glp-opus-seo-review-job-v1'
            and original.get('operation') == 'new_article'
            and original.get('bridge_extension') is None, 'review_only_original_job_contract')
    receipt = read(checked(entry['pass_receipt']))
    require(original['job_id'] == receipt['job_id'], 'review_only_original_job_identity')
    origin = Path(original['packet_dir']).resolve(strict=True)
    for name in CORE[:6]:
        require((origin/name).is_file() and not (origin/name).is_symlink()
                and (origin/name).read_bytes() == (entry_path.parent/'packet'/name).read_bytes(),
                'review_only_original_source_identity')
    for key in ('topic_id', 'target_route'):
        require(original[key] == entry[key], 'review_only_topic_identity')
    require(original['target_query'] == receipt['authority']['selection_binding']['target_query']
            and checked(original['selection_receipt']) == checked(entry['selection_receipt']),
            'review_only_selection_identity')
    loop.validate_topic_authority(original)
    route = review_transports.transport(transport_id)
    require(transport_id in review_transports.NATIVE_INVOCATION_TRANSPORT_IDS
            and route['provider'] == 'openai-codex',
            'review_only_registered_native_route_required')
    proposed = {'model': route['model'], 'runtime': {'native_session_id': 'NOT_YET_INVOKED'}}
    clock = datetime.now(timezone.utc).timestamp()
    authors = packet_authors(session_db or SESSION_DB, [origin/n for n in CORE[:6]], proposed, clock)
    for sid in {row['session_id'] for row in authors}:
        author_session(session_db or SESSION_DB, sid, proposed, clock)
    supplied = (successor_packet, successor_candidate, candidate_sha256, draft_sha256, blocked_review)
    require(not any(supplied) or all(supplied), 'review_only_successor_inputs_required')
    successor = None
    if all(supplied):
        successor = review_only_successor_inputs(origin, *supplied, root=root)
        changed = sorted(set(CORE[:6]) - set(successor['original_aliases']))
        require(changed, 'review_only_successor_source_repair_required')
        for name in changed:
            packet_authors(session_db or SESSION_DB,
                [checked(successor['sources'][name])], proposed, clock)
        authenticate_blocked_history(successor['blocked_review'], spec(entry_path), spec(original_job),
                                     root=root, session_db=session_db)
        # A byte-identical alias does not erase genuine edits at its successor path.
        successor['source_authors'] = packet_authors(session_db or SESSION_DB,
            [checked(successor['sources'][n]) for n in CORE[:6]], proposed, clock)
        for sid in {row['session_id'] for row in successor['source_authors']}:
            author_session(session_db or SESSION_DB, sid, proposed, clock)
    _, strategy = load_strategy(root/'reports/glp-clinic-scout/weekly-strategy/current-strategy.json')
    require(strategy.get('applied') is True, 'review_only_current_weekly_strategy:'+str(strategy.get('reason')))
    pointer_path = root/'reports/glp-clinic-scout/seo-machine/current-content-base.json'
    pointer = read(pointer_path); release = Path(pointer['release_root']).resolve(strict=True)
    rows = [r for r in carrier.source_rows(release) if r.get('entry_id') == entry['entry_id']]
    require(len(rows) == 1 and rows[0]['route'] == entry['target_route'], 'review_only_published_source_identity')
    for key, name in (('article', '06-article.json'), ('qa', '07-qa-receipt.json')):
        require(rows[0][key]['sha256'] == sha(entry_path.parent/'packet'/name)
                == sha(release/rows[0][key]['path']), 'review_only_published_original_'+key)
    require(rows[0]['review']['sha256'] == entry['pass_receipt']['sha256']
            == sha(release/rows[0]['review']['path']), 'review_only_published_original_review')
    candidate = release/'public'/entry['target_route'].strip('/')/'index.html'
    require(candidate.is_file() and not candidate.is_symlink(), 'review_only_deployed_candidate')
    pointer_raw = pointer_path.read_bytes()
    output = Path(output).absolute()
    require(output.parent.resolve(strict=True).is_relative_to(root/'reports')
            and not output.exists() and not output.is_symlink(), 'review_only_unused_report_directory')
    output.mkdir(mode=0o700); packet = output/'packet'; packet.mkdir(mode=0o700)
    pointer_snapshot = output/'current-pointer.json'; pointer_snapshot.write_bytes(pointer_raw)
    binding = {'entry': spec(entry_path), 'original_job': spec(original_job),
               'original_source_authors': authors, 'current_pointer': spec(pointer_snapshot),
               'deployed_source': rows[0], 'deployed_candidate': spec(candidate), 'weekly_strategy': strategy,
               'publication_authorized': False, 'recovery_acceptance': False}
    if successor:
        binding.update(review_subject='corrected_successor', successor=successor)
    for name in CORE[:6]:
        source = checked(successor['sources'][name]) if successor else entry_path.parent/'packet'/name
        (packet/name).write_bytes(source.read_bytes())
    (packet/CANDIDATE).write_bytes((checked(successor['candidate']) if successor else candidate).read_bytes())
    controls = root/'systems/seo-blog-machine/scripts'
    argv = [sys.executable, str(controls/'validate_opus_article_packet.py'), '--packet', str(packet),
            '--expected-topic-id', entry['topic_id'], '--expected-query', original['target_query'],
            '--expected-route', entry['target_route'], '--selection-receipt', entry['selection_receipt']['path'],
            '--operation', 'new_article', '--config', str(root/'systems/seo-blog-machine/config/verticals/glp-clinic-scout.json')]
    validator = {'argv': argv, 'executable': sys.executable, 'executable_sha256': sha(sys.executable),
                 'control_files': {str(controls/n): sha(controls/n) for n in (
                     'validate_opus_article_packet.py', 'compile_review_packet_article.py',
                     'selection_receipt.py', 'r2_update_carrier.py')}, 'timeout': 300}
    loop.validate_executable(validator, 'validator')
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=300, cwd=root)
    (output/'validator.stdout.txt').write_text(proc.stdout)
    (output/'validator.stderr.txt').write_text(proc.stderr)
    require(proc.returncode == 0 and read(packet/'07-qa-receipt.json')['status'] == 'PASS',
            'review_only_current_deterministic_qa_failed')
    if successor:
        validate_review_only_successor(entry_path, original, packet, successor, root=root,
                                      clock=datetime.now(timezone.utc).isoformat())
    require(pointer_path.read_bytes() == pointer_raw and sha(candidate) == binding['deployed_candidate']['sha256'],
            'review_only_publication_changed_during_preparation')
    job = review_only_job(original, packet, output/'history', route, validator=validator, binding=binding)
    path = output/'job.json'; loop.atomic_json(path, job)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entry', type=Path, required=True)
    parser.add_argument('--history', type=Path)
    parser.add_argument('--packet', type=Path)
    parser.add_argument('--live-binding', type=Path)
    parser.add_argument('--live-snapshot', type=Path)
    parser.add_argument('--prepare-review-only', action='store_true',
                        help='emit one effect-free native-controller job; no review or publication')
    parser.add_argument('--original-job', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--review-transport')
    parser.add_argument('--successor-packet', type=Path)
    parser.add_argument('--successor-candidate', type=Path)
    parser.add_argument('--candidate-sha256')
    parser.add_argument('--draft-sha256')
    parser.add_argument('--blocked-review', type=Path)
    parser.add_argument('--review-only-job', type=Path,
                        help='seal a postpublication supplement; requires genuine native PASS and no source change')
    parser.add_argument('--source-author-packet', type=Path,
                        help='exact byte-identical source packet with native write/patch custody')
    parser.add_argument('--source-author-review', type=Path,
                        help='native origin review binding the copied source hashes (provenance only)')
    args = parser.parse_args()
    if args.prepare_review_only:
        if (not all((args.original_job, args.output, args.review_transport))
                or any((args.history, args.packet, args.live_binding, args.live_snapshot,
                        args.source_author_packet, args.source_author_review, args.review_only_job))):
            parser.error('prepare-review-only requires original-job/output/review-transport and forbids sealing inputs')
        from sync_from_repo import source_lock
        with source_lock(ROOT):
            path = prepare_review_only(args.entry, args.original_job, args.output, args.review_transport,
                successor_packet=args.successor_packet, successor_candidate=args.successor_candidate,
                candidate_sha256=args.candidate_sha256, draft_sha256=args.draft_sha256,
                blocked_review=args.blocked_review)
        print(json.dumps({'status': 'PREPARED_REVIEW_ONLY_NOT_INVOKED', 'job': spec(path),
                          'native_calls': 0, 'publication_authorized': False, 'health_accepted': False}))
        return
    if not all((args.history, args.packet, args.live_binding, args.live_snapshot)):
        parser.error('sealing requires history/packet/live-binding/live-snapshot')
    if any((args.original_job, args.output, args.review_transport)):
        parser.error('preparation inputs require prepare-review-only')
    from glp_release_main import RUNS
    RUNS.mkdir(parents=True, exist_ok=True)
    # Same nonblocking lock as hourly main release and the :17 sync job;
    # retain it through all input admission, full-byte validation and sealing.
    with (RUNS / 'run.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sealing = {'source_author_packet': args.source_author_packet,
                   'source_author_review': args.source_author_review}
        if args.review_only_job is not None:
            sealing['review_only_job_path'] = args.review_only_job
        path = seal(args.entry.resolve(strict=True), args.history.resolve(strict=True),
                    args.packet.resolve(strict=True), args.live_binding.resolve(strict=True),
                    args.live_snapshot.resolve(strict=True), **sealing)
    print(json.dumps({'status': 'PASS_POSTPUBLICATION_REVIEW', 'recovery': spec(path)}))


if __name__ == '__main__':
    main()
