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
"""
import argparse
import hashlib
import html as htmllib
import json
import re
import sys
from pathlib import Path

ROOT = Path('/home/ubuntu/business')
SCHEMA = 'glp-postpublication-review-recovery-v2'
SUBJECTS = {'00-authority.json', '01-evidence-ledger.json', '02-serp-brief.json',
            '03-editorial-brief.md', '04-draft.md', '05-claim-source-map.json',
            '06-article.json', '07-qa-receipt.json'}
CANDIDATE = '08-candidate-guide.html'
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


def successor_contract(original, fresh, rows, manifest, *, compile_packet):
    """Positively exercised derivation from the original packet to the successor."""
    expected = {row['path']: row['sha256'] for row in rows}
    actual = {row['path']: row['sha256'] for row in manifest['subject_files']}
    require(set(expected) == SUBJECTS and len(rows) == len(expected), 'original_subject_set')
    require(set(actual) == SUBJECTS | {CANDIDATE} and len(manifest['subject_files']) == len(actual),
            'recovery_subject_set')
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
    return {'identity': list(IDENTITY), 'source_repairs': changed,
            'article_regenerated_sha256': hashlib.sha256(regenerated.encode()).hexdigest(),
            'qa_sha256': actual['07-qa-receipt.json']}


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
    require(packet.resolve() == manifest_path.parent.parent / 'packet', 'copied_source_review_path')
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


def validate_documents(entry_path, doc, *, root=ROOT, session_db=None):
    import hermes_sol_article_review as transport
    sys.path.insert(0, str(root / 'systems/seo-blog-machine/scripts'))
    import opus_review_loop as loop
    import compile_review_packet_article as compiler
    entry = read(entry_path)
    require(doc['schema'] == SCHEMA and doc['status'] == 'PASS_POSTPUBLICATION_REVIEW', 'recovery_schema')
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
                                    compile_packet=compiler.compile_packet)
    require(doc['successor'] == derivation, 'successor_derivation_changed')
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
    author_seal = timestamp(manifest['sealed_at_utc'])
    origins = doc.get('source_authorship', {})
    if origins:
        origin_dirs = {Path(v['path']).parent for v in origins.values()}
        require(len(origin_dirs) == 1, 'copied_source_directory_set')
        author_seal = authenticate_source_origin(next(iter(origin_dirs)),
            doc['source_authorship_review'], derivation['source_repairs'], root, database, author_seal)
    successor_authors = copied_source_authors(fresh, derivation['source_repairs'],
                                       origins, root, database,
                                       {'model': runtime['model'], 'runtime': {'native_session_id': runtime['session_id']}},
                                       author_seal)
    pointer = read(root / 'reports/glp-clinic-scout/seo-machine/current-content-base.json')
    release = Path(pointer['release_root'])
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
    return {'receipt': receipt, 'raw': raw, 'manifest': manifest, 'custody': custody, 'document': doc,
            'successor_authors': successor_authors}


def validate_recovery(entry_path, *, root=ROOT, natural=False, session_db=None):
    require(not natural, 'postpublication_review_not_natural')
    entry_path = Path(entry_path).resolve(strict=True)
    path = recovery_path(entry_path, root)
    result = validate_documents(entry_path, read(path), root=root, session_db=session_db)
    result['recovery'] = spec(path)
    return result


def seal(entry_path, history, packet, binding_path, snapshot, *, root=ROOT,
         source_author_packet=None, source_author_review=None):
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
                                           compile_packet=compiler.compile_packet),
           'live_snapshot': spec(snapshot), 'live_binding': read(binding_path),
           'historical_custody_repaired': False, 'natural_acceptance': False}
    if source_author_packet is not None:
        origin = Path(source_author_packet)
        require(origin.is_absolute() and origin.is_dir() and not origin.is_symlink(), 'copied_source_directory')
        names = doc['successor']['source_repairs']
        require(all(not (origin / n).is_symlink() for n in names), 'copied_source_symlink')
        doc['source_authorship'] = {n: spec(origin / n) for n in names}
        require(source_author_review is not None, 'copied_source_native_review_required')
        origin_review = read(source_author_review)
        doc['source_authorship_review'] = {
            'manifest': spec(origin_review['subject_manifest_path']),
            'raw': spec(origin_review['raw_review_path'])}
    else:
        require(source_author_review is None, 'copied_source_review_without_packet')
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
    parser.add_argument('--live-snapshot', type=Path, required=True)
    parser.add_argument('--source-author-packet', type=Path,
                        help='exact byte-identical source packet with native write/patch custody')
    parser.add_argument('--source-author-review', type=Path,
                        help='native origin review binding the copied source hashes (provenance only)')
    args = parser.parse_args()
    from glp_release_main import lock_module
    with lock_module().acquire('main-site-release', owner='article-review-recovery', root=ROOT):
        path = seal(args.entry.resolve(strict=True), args.history.resolve(strict=True),
                    args.packet.resolve(strict=True), args.live_binding.resolve(strict=True),
                    args.live_snapshot.resolve(strict=True), source_author_packet=args.source_author_packet,
                    source_author_review=args.source_author_review)
    print(json.dumps({'status': 'PASS_POSTPUBLICATION_REVIEW', 'recovery': spec(path)}))


if __name__ == '__main__':
    main()
