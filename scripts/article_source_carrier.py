#!/usr/bin/env python3
"""Publish sealed, independently accepted packets as reviewed site main source.

Never edits frozen catalogs/image manifests. The native main release retains
secrets, held-provider census, claim freshness, pointer CAS and rollback gates.
A deterministic source branch and immutable consumption permit crash resume.
"""
from __future__ import annotations
import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path('/home/ubuntu/business')
SCRIPTS = Path(__file__).resolve().parents[1] / 'systems/seo-blog-machine/scripts'
sys.path.insert(0, str(SCRIPTS))
import article_buffer as buffer
from carrier_lock import carrier_lock

SOURCE_REL = Path('data/blog/accepted-guides')
REPO = 'izaiahat/glp-site-candidate'
SITE = 'https://glpclinicscout.com'


def run(argv, *, cwd=None, timeout=900, lock_fd=None):
    proc = subprocess.run([str(x) for x in argv], cwd=cwd, capture_output=True,
                          text=True, timeout=timeout,
                          pass_fds=(lock_fd,) if lock_fd is not None else ())
    if proc.returncode:
        raise RuntimeError(f'source_child_failed:{argv}:rc={proc.returncode}\n{proc.stdout[-16000:]}\n{proc.stderr[-16000:]}')
    return proc.stdout


def gh(*args):
    return json.loads(run(['gh', *args]))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_rows(source):
    manifest = json.loads((source / SOURCE_REL / 'manifest.json').read_text())
    if manifest.get('schema') != 'glp-accepted-guides-source-v1':
        raise RuntimeError('source_manifest_invalid')
    return manifest['entries']


def held_source_replacement(source, packet, entry, predecessor_row):
    """Receive only admitted successor_of; a quarantine alone is not authority."""
    packet = packet.resolve()
    manifest = packet.parent / 'entry.json'
    root = manifest.parents[2]
    sealed = buffer.validate_entry(manifest,current_routes=set(),current_topic_ids=set())
    if (buffer.ENTRY_ID_PATTERN.fullmatch(str(entry.get('entry_id') or '')) is None
        or sealed != entry or packet != root / 'entries' / entry['entry_id'] / 'packet'
        or entry.get('publication_effects') != 0):
        raise RuntimeError('source_successor_entry_binding_invalid')
    lineage = entry.get('successor_of')
    if not isinstance(lineage,dict) or set(lineage) != {'entry_id','entry_manifest','hold'}:
        raise RuntimeError('source_identity_drift')
    old_id = predecessor_row['entry_id']
    if old_id != lineage['entry_id'] or old_id == entry['entry_id']:
        raise RuntimeError('source_successor_predecessor_identity_invalid')
    old_manifest = root / 'entries' / old_id / 'entry.json'
    if (buffer._verify_spec(lineage['entry_manifest'],'source_predecessor') != old_manifest
        or buffer._verify_spec(lineage['hold'],'source_predecessor_hold') != root / 'held' / (old_id+'.json')):
        raise RuntimeError('source_successor_lineage_location_invalid')
    # Binds all predecessor article/QA/review source bytes, not just the row ID.
    _, predecessor, _ = accepted_entry(root,source,predecessor_row)
    if (predecessor.get('publication_effects') != 0
        or any(predecessor[key] != entry[key] for key in ('topic_id','article_id','target_route'))):
        raise RuntimeError('source_successor_predecessor_identity_invalid')
    # Consumption is operation-keyed; entry identity lives in its sealed payload.
    for record_path in sorted((root / 'consumed').glob('*.json')):
        consumed = buffer._read_sealed(record_path,buffer.CONSUME_SCHEMA,'consumption_sha256')
        if consumed.get('entry_id') in (old_id,entry['entry_id']):
            raise RuntimeError('source_successor_already_consumed')
    if (entry['entry_id'] in buffer._held_entry_ids(root)
        or entry['entry_id'] in buffer._quarantined_entry_ids(root)):
        raise RuntimeError('source_successor_not_ready')
    controls = {raw:digest(Path(raw)) for raw in entry['control_files']}
    evidence = buffer.validate_admission_evidence(packet,Path(entry['pass_receipt']['path']),controls)
    if any(evidence[key] != entry[key] for key in ('topic_id','article_id','target_route')):
        raise RuntimeError('source_acceptance_identity_invalid')
    if buffer._held_successor_predecessor(root,old_id,[(old_manifest,predecessor)],evidence,
                                          entry['packet_closure_sha256']) != lineage:
        raise RuntimeError('source_successor_lineage_invalid')
    # Authenticate the registered review again; never admit or mint a review.
    receipt = buffer._verify_spec(entry['registered_review']['receipt'],'source_registered_review')
    reviewed_packet = receipt.parent.parent / 'packet'
    reviewed = buffer.validate_admission_evidence(reviewed_packet,receipt,controls)
    if (any(reviewed[key] != evidence[key] for key in ('topic_id','article_id','target_route'))
        or buffer._registered_successor_review(receipt,reviewed) != entry['registered_review']
        or digest(reviewed_packet / '06-article.json') != digest(packet / '06-article.json')
        or digest(reviewed_packet / '07-qa-receipt.json') != digest(packet / '07-qa-receipt.json')
        or digest(receipt) != digest(Path(entry['pass_receipt']['path']))):
        raise RuntimeError('source_successor_review_binding_invalid')


def land_packet(source, packet, entry):
    route = entry['target_route']
    if not re.fullmatch(r'/guides/[a-z0-9-]+/', route):
        raise RuntimeError('source_route_invalid')
    rel = SOURCE_REL / route.strip('/').split('/')[-1]
    specs = {}
    inputs = {'article':packet / '06-article.json', 'qa':packet / '07-qa-receipt.json',
              'review':Path(entry['pass_receipt']['path'])}
    suffix = '-' + entry['entry_id'] if entry.get('successor_of') else ''
    for key, path in inputs.items():
        specs[key] = {'path':(rel / f'{key}{suffix}.json').as_posix(), 'sha256':digest(path)}
    row = {'entry_id':entry['entry_id'], 'route':route,
           'decorative_image_policy':'existing-approved-editorial-evidence-hero; no new generation or shared-manifest mutation', **specs}
    manifest_path = source / SOURCE_REL / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schema') != 'glp-accepted-guides-source-v1':
        raise RuntimeError('source_manifest_invalid')
    existing = [r for r in manifest['entries'] if r['route'] == route or r['entry_id'] == entry['entry_id']]
    predecessor = None
    if existing:
        if existing == [row] and all(digest(source / spec['path']) == spec['sha256'] for spec in specs.values()):
            return False
        if (len(existing) != 1 or existing[0]['route'] != route
            or existing[0]['entry_id'] == entry['entry_id'] or not entry.get('successor_of')):
            raise RuntimeError('source_identity_drift')
        predecessor = existing[0]
        held_source_replacement(source,packet,entry,predecessor)
        history = manifest.get('replacement_history',[])
        if not isinstance(history,list) or any(h.get('successor_entry_id') == entry['entry_id'] for h in history):
            raise RuntimeError('source_replacement_history_invalid')
        # Versioned successor paths leave original files and buffer holds intact.
        for spec in specs.values():
            target = source / spec['path']
            if target.is_symlink() or (target.exists() and digest(target) != spec['sha256']):
                raise RuntimeError('source_identity_drift')
    for key, path in inputs.items():
        target = source / specs[key]['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
    if predecessor is None:
        manifest['entries'].append(row)
    else:
        manifest['entries'][manifest['entries'].index(predecessor)] = row
        manifest.setdefault('replacement_history',[]).append({'predecessor':predecessor,
            'successor_entry_id':entry['entry_id'], 'successor_of':entry['successor_of']})
    manifest_path.write_text(json.dumps(manifest,indent=2) + '\n')
    return True


def accepted_entry(root, source, row):
    manifest = root / 'entries' / row['entry_id'] / 'entry.json'
    entry = buffer.validate_entry(manifest,current_routes=set(),current_topic_ids=set())
    packet = manifest.parent / 'packet'
    article = json.loads((packet / '06-article.json').read_text())
    for key, path in {'article':packet / '06-article.json', 'qa':packet / '07-qa-receipt.json',
                      'review':Path(entry['pass_receipt']['path'])}.items():
        spec = row[key]
        if digest(path) != spec['sha256'] or digest(source / spec['path']) != spec['sha256']:
            raise RuntimeError('source_packet_identity_drift')
    if entry['target_route'] != row['route'] or article['canonical_path'] != row['route']:
        raise RuntimeError('source_route_identity_drift')
    return manifest, entry, article


def fetch(url):
    # Exact publisher route only; never fetch tracking/affiliate links.
    out = run(['curl','--fail-with-body','--silent','--show-error','--max-time','60',
               '--write-out','\n%{http_code}',url],timeout=90)
    body, code = out.rsplit('\n',1)
    return int(code), body


def verified_live_html_hash(body, expected):
    """Reverse only Cloudflare's observed, reversible email protection.

    Do not strip prose, tags, whitespace, arbitrary scripts or attributes. Each
    replacement must occupy its exact position in the authenticated release.
    The final entire HTML must be byte-identical, not just the article title.
    """
    if body == expected:
        return hashlib.sha256(body.encode()).hexdigest()

    def decode(value):
        raw = bytes.fromhex(value)
        return bytes(b ^ raw[0] for b in raw[1:]).decode('utf-8')

    # CF inserts this literal immediately before the next real script after
    # protecting an email. Parse boundaries before any reversal: regex deletion
    # alone can erase data inside JSON-LD, JavaScript, or an attribute.
    decoder = '<script data-cfasync="false" src="/cdn-cgi/scripts/5c5dd728/cloudflare-static/email-decode.min.js"></script>'
    href_pattern = r'href="/cdn-cgi/l/email-protection#([a-fA-F0-9]{4,8192})"'
    span_pattern = r'<span class="__cf_email__" data-cfemail="([a-fA-F0-9]{4,8192})">\[email&#160;protected\]</span>'
    email_pattern = r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}'
    # The observed share links also encode a recipient-free subject/body query.
    share_pattern = r'\?subject=[^\s"<>]*(?:&|&amp;)body=[^\s"<>]+'
    attribute_pattern = re.compile(r'''\s+([^\s"'<>/=]+)(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?''')

    class Boundaries(HTMLParser):
        CDATA_CONTENT_ELEMENTS = ('script','style','textarea','title','xmp','iframe',
                                  'noembed','noframes','noscript')

        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.lines = [0] + [m.end() for m in re.finditer('\n', body)]
            self.scripts, self.emails, self.replacements, self.body_ends = [], [], [], []
            self.inert = []

        def handle_starttag(self, tag, attrs):
            # Include raw-text containers on Python versions that only handle
            # script/style, and self-closing spellings (not void HTML elements).
            if tag in self.CDATA_CONTENT_ELEMENTS:
                self.set_cdata_mode(tag)
            inert = (tag in ('textarea','title','xmp','iframe','noembed','noframes','plaintext',
                            'template','noscript','svg','math','select','option','optgroup')
                     or 'inert' in dict(attrs))
            if self.inert or inert:
                void = tag in ('area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr')
                if not void and (inert or tag == self.inert[-1]):
                    self.inert.append(tag)
                return
            line, column = self.getpos()
            offset = self.lines[line-1] + column
            raw = self.get_starttag_text() or ''
            if tag == 'script':
                self.scripts.append(offset)
            email_span = re.match(span_pattern, body[offset:]) if tag == 'span' else None
            if email_span:
                restored = decode(email_span[1])
                if re.fullmatch(email_pattern, restored):
                    self.emails.append(offset)
                    self.replacements.append((offset, offset+len(email_span[0]), restored))
            if tag == 'a':
                # Tokenize the entire start tag without gaps. A regex search
                # could match data-href, a quoted attribute's data, duplicate
                # hrefs, or malformed attributes tolerated by HTMLParser.
                opening = re.match(r'<[aA](?=\s|/?>)', raw)
                if not opening:
                    return
                pos, names, email_href = opening.end(), set(), None
                while raw[pos:].strip() not in ('>', '/>'):
                    attr = attribute_pattern.match(raw, pos)
                    if not attr or attr[1].lower() in names:
                        return
                    names.add(attr[1].lower())
                    match = re.fullmatch(href_pattern, raw[attr.start(1):attr.end()])
                    if match:
                        email_href = (attr.start(1), attr.end(), decode(match[1]))
                    pos = attr.end()
                if email_href:
                    start, end, restored = email_href
                    if re.fullmatch(email_pattern, restored) or re.fullmatch(share_pattern, restored):
                        self.emails.append(offset)
                        self.replacements.append((offset+start, offset+end, 'href="mailto:'+restored+'"'))

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)

        def handle_data(self, data):
            # HTMLParser does not model HTML's double-escaped script state.
            # Do not trust later apparent element boundaries in that shape.
            if self.cdata_elem == 'script' and re.search(r'<!--[\s\S]*?<script(?=[\s/>])', data, re.I):
                raise RuntimeError('live_article_content_drift')

        def handle_endtag(self, tag):
            if tag == 'body' and not self.inert:
                line, column = self.getpos()
                offset = self.lines[line-1] + column
                if body.startswith('</body>', offset):
                    self.body_ends.append(offset)
            if self.inert and 'plaintext' not in self.inert and tag == self.inert[-1]:
                self.inert.pop()

    try:
        boundary = Boundaries()
        boundary.feed(body)
        boundary.close()
        injections = [i for i, pos in enumerate(boundary.scripts) if body.startswith(decoder, pos)]
        if body != expected and injections:
            if len(injections) not in (1, 2):
                raise RuntimeError('live_article_content_drift')
            i = injections[0]
            pos = boundary.scripts[i]
            first_email = min(boundary.emails, default=len(body))
            first_script_after_email = next((s for s in boundary.scripts if s > first_email), None)
            if (pos != first_script_after_email
                    or i+1 == len(boundary.scripts)
                    or boundary.scripts[i+1] != pos+len(decoder)):
                raise RuntimeError('live_article_content_drift')
            boundary.replacements.append((pos, pos+len(decoder), ''))
            if len(injections) == 2:
                # The fresh real article also has a terminal decoder: a new
                # protected footer email follows the share script, with no
                # later original script to trigger decoding before </body>.
                # Admit only that exact second boundary, not arbitrary copies.
                tail = boundary.scripts[injections[1]]
                later_email = next((e for e in sorted(boundary.emails)
                                    if e > boundary.scripts[i+1]), None)
                next_script = next((s for s in boundary.scripts
                                    if later_email is not None and s > later_email), None)
                if (injections[1] != len(boundary.scripts)-1
                        or next_script != tail
                        or boundary.body_ends != [tail+len(decoder)]):
                    raise RuntimeError('live_article_content_drift')
                boundary.replacements.append((tail, tail+len(decoder), ''))
        # Preserve every byte outside admitted elements/attributes. Authenticate
        # each replacement at its position, never by substring membership.
        parts, cursor, expected_cursor = [], 0, 0
        for start, end, restored in sorted(boundary.replacements):
            if start < cursor:
                raise RuntimeError('live_article_content_drift')
            piece = body[cursor:start] + restored
            if not expected.startswith(piece, expected_cursor):
                raise RuntimeError('live_article_content_drift')
            parts.append(piece)
            expected_cursor += len(piece)
            cursor = end
        normalized = ''.join(parts) + body[cursor:]
    except (ValueError, UnicodeError, IndexError) as exc:
        raise RuntimeError('live_article_content_drift') from exc
    if normalized != expected:
        raise RuntimeError('live_article_content_drift')
    return hashlib.sha256(normalized.encode()).hexdigest()


def live_readback(route,title, *, expected_html=None):
    url = SITE + route
    code, body = fetch(url)
    match = re.search(r'<title>(.*?)</title>',body,re.S|re.I)
    actual = html.unescape(match[1]).strip() if match else None
    if code != 200 or actual != title:
        raise RuntimeError(f'live_title_mismatch:{code}:{actual!r}:{title!r}')
    robots = re.findall(r'<meta\b[^>]*name=[\"\']robots[\"\'][^>]*>',body,re.I)
    if not robots or any(re.search(r'noindex|nofollow',tag,re.I) for tag in robots):
        raise RuntimeError('live_indexability_invalid')
    if f'href="{url}"' not in body:
        raise RuntimeError('live_canonical_missing')
    result = {'url':url,'http':code,'title':actual,'html_sha256':hashlib.sha256(body.encode()).hexdigest()}
    if expected_html is not None:
        result['deployed_html_sha256'] = verified_live_html_hash(body,Path(expected_html).read_bytes().decode('utf-8'))
    return result


def consumed_path(root,operation_id):
    return root / 'consumed' / (hashlib.sha256(operation_id.encode()).hexdigest() + '.json')


def record_consumption(root, entry, operation_id, publication, claim=None):
    target = consumed_path(root,operation_id)
    if target.exists():
        return buffer._read_sealed(target,buffer.CONSUME_SCHEMA,'consumption_sha256')
    receipt = publication.get('receipt')
    if not receipt and (publication.get('release') or {}).get('report'):
        receipt = str(Path(publication['release']['report']) / 'RESULT.json')
    if not receipt:
        pointer = json.loads((ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json').read_text())
        receipt = pointer['final_receipt']['path']
    publication = {**publication, 'receipt': receipt}
    payload = {'schema':buffer.CONSUME_SCHEMA,'status':'PASS_BUFFER_PUBLISHED',
               'operation_id':operation_id,'entry_id':entry['entry_id'],
               'topic_id':entry['topic_id'],'target_route':entry['target_route'],
               'publication':publication,'publication_lane':'accepted-site-main-source'}
    if claim:
        payload['claim'] = buffer._spec(claim)
    buffer._write_once(target,payload,'consumption_sha256')
    return buffer._read_sealed(target,buffer.CONSUME_SCHEMA,'consumption_sha256')


def successor_publication_lineage(publication, pointer):
    """Authenticate content containment, not merely a surviving title or route.

    The immutable consumption pins the original native terminal via its search
    binding. That terminal pins the original deployment manifest. The validated
    CURRENT pointer pins the successor manifest and deployed closure. Require
    the identical accepted article/QA/review AND rendered HTML in both closures.
    No ancestry guess, mutable receipt retrofit, publishing or new consumption.
    Recovery-snapshot records deliberately retain their exact-pointer contract.
    """
    main = publication['main_release']
    binding = main['search_submission']['binding']
    original_id = publication['deployment_id']
    if (binding['deployment_id'] != original_id
            or main['deployment_id'] != original_id
            or pointer['deployment_id'] == original_id):
        raise RuntimeError('source_lineage_original_deployment_invalid')
    terminal_spec = binding['terminal_receipt']
    terminal_path = buffer._verify_spec(terminal_spec, 'source_lineage_terminal')
    if publication['receipt'] != str(terminal_path):
        raise RuntimeError('source_lineage_terminal_binding_invalid')
    terminal = json.loads(terminal_path.read_text())
    if (terminal['schema'] != 'glp-opus-production-terminal-receipt-v1'
            or terminal['status'] != 'PASS_PRODUCTION_TERMINAL'
            or terminal['deployment']['deployment_id'] != original_id
            or terminal['site_head'] != binding['site_head']):
        raise RuntimeError('source_lineage_terminal_identity_invalid')
    original_manifest_spec = {'path':str(terminal_path.parent / '01-deployment-manifest.json'),
        'sha256':terminal['evidence_sha256']['01-deployment-manifest.json']}
    original_manifest_path = buffer._verify_spec(original_manifest_spec, 'source_lineage_manifest')
    current_manifest_path = buffer._verify_spec(pointer['manifest'], 'source_lineage_current_manifest')
    original_manifest = json.loads(original_manifest_path.read_text())
    current_manifest = json.loads(current_manifest_path.read_text())
    row = publication['source_entry']
    route = row['route']
    if not re.fullmatch(r'/guides/[a-z0-9-]+/', route):
        raise RuntimeError('source_lineage_route_invalid')
    specs = [row[key] for key in ('article','qa','review')]
    public_rel = 'public/' + route.strip('/') + '/index.html'
    public_hashes = []
    for manifest, release_root, site_head in (
            (original_manifest,binding['release_root'],binding['site_head']),
            (current_manifest,pointer['release_root'],pointer['site_head'])):
        if (manifest['schema'] != 'glp-opus-release-deployment-manifest-v1'
                or manifest['release_root'] != release_root or manifest['site_head'] != site_head):
            raise RuntimeError('source_lineage_manifest_identity_invalid')
        release = Path(release_root)
        if not release.is_absolute() or release.is_symlink():
            raise RuntimeError('source_lineage_release_invalid')
        for rel, expected in [(s['path'],s['sha256']) for s in specs] + [(public_rel,None)]:
            relative = Path(rel)
            if relative.is_absolute() or '..' in relative.parts:
                raise RuntimeError('source_lineage_path_invalid')
            matches = [r for r in manifest['release_rows'] if r['path'] == rel]
            if len(matches) != 1:
                raise RuntimeError('source_lineage_manifest_row_invalid')
            item = matches[0]
            target = release / rel
            if (target.is_symlink() or not target.is_file()
                    or not target.resolve().is_relative_to(release.resolve())
                    or digest(target) != item['sha256']
                    or target.stat().st_size != item['size']
                    or (expected is not None and item['sha256'] != expected)):
                raise RuntimeError('source_lineage_content_drift')
            if rel == public_rel:
                public_hashes.append(item['sha256'])
    if public_hashes[0] != public_hashes[1]:
        raise RuntimeError('source_lineage_rendered_article_drift')
    # Recheck the historical pins after reading their bound leaf bytes.
    buffer._verify_spec(terminal_spec, 'source_lineage_terminal')
    buffer._verify_spec(original_manifest_spec, 'source_lineage_manifest')
    return ({'deployment_id':original_id, 'site_head':binding['site_head'],
             'final_receipt':terminal_spec},
            {'mode':'sealed-deployed-article-content', 'original_deployment_id':original_id,
             'current_deployment_id':pointer['deployment_id'], 'route':route,
             'article_html_sha256':public_hashes[0], 'original_manifest':original_manifest_spec,
             'current_manifest':pointer['manifest']})


def published_operation_readback(operation_id):
    """GET-only recovery of a failed acknowledgement after native reconciliation.

    Require the exact sealed operation, claim, accepted source packet, current
    pointer binding, production identity and live title. Never create a record
    or invoke publication here; missing/stale evidence keeps the caller pending.
    """
    import article_buffer_cli as cli
    from scripts.cron_bridge_action import article_current_base, article_production_head_id
    if not operation_id:
        return None
    try:
        path = consumed_path(cli.DEFAULT_ROOT, operation_id)
        record = buffer._read_sealed(path,buffer.CONSUME_SCHEMA,'consumption_sha256')
        if (record.get('operation_id') != operation_id or record.get('status') != 'PASS_BUFFER_PUBLISHED'
                or record.get('publication_lane') != 'accepted-site-main-source'):
            return None
        publication = record['publication']
        current = article_current_base()
        if publication.get('status') != 'PASS_BUFFER_PUBLISHED':
            return None
        lineage = None
        reconciled = publication.get('source_reconciled') is True
        if reconciled:
            if (publication.get('deployment_id') != current['deployment_id']
                    or publication.get('current_base') != buffer._spec(cli.POINTER)):
                return None
        else:
            # Normal consume seals the native main result, not a recovery
            # snapshot. Authenticate its terminal receipt against the validated
            # CURRENT pointer; never retrofit fields into immutable consumption.
            pointer = json.loads(cli.POINTER.read_text())
            if (digest(cli.POINTER) != current['pointer_sha256']
                    or pointer['deployment_id'] != current['deployment_id']):
                return None
            publication_base = pointer
            if publication.get('deployment_id') != current['deployment_id']:
                publication_base, lineage = successor_publication_lineage(publication,pointer)
            pr, main = publication['source_pr'], publication['main_release']
            if (pr['state'] != 'MERGED'
                    or not re.fullmatch(r'https://github\.com/izaiahat/glp-site-candidate/pull/[1-9][0-9]*', pr['url'])
                    or any(not re.fullmatch('[a-f0-9]{40}', value)
                           for value in (pr['headRefOid'], pr['mergeCommit']['oid']))
                    or publication_base['site_head'] not in (pr['headRefOid'], pr['mergeCommit']['oid'])
                    or main['status'] not in ('PASS_PRODUCTION_TERMINAL', 'PASS_ALREADY_CURRENT')
                    or main.get('prepare_only') is True
                    or main['scope'] != 'operator-approved-main-release'
                    or main['site_main'] != pr['mergeCommit']['oid']
                    or main['deployment_id'] != publication_base['deployment_id']
                    or (main['status'] == 'PASS_PRODUCTION_TERMINAL' and main.get('source_pr') != pr['url'])):
                return None
            terminal_path = buffer._verify_spec(publication_base['final_receipt'], 'source_readback_terminal')
            terminal = json.loads(terminal_path.read_text())
            if (publication['receipt'] != str(terminal_path)
                    or terminal['schema'] != 'glp-opus-production-terminal-receipt-v1'
                    or terminal['status'] != 'PASS_PRODUCTION_TERMINAL'
                    or terminal['deployment']['deployment_id'] != publication_base['deployment_id']
                    or terminal['site_head'] != publication_base['site_head']):
                return None
        claim_path = buffer._verify_spec(record['claim'],'source_readback_claim')
        claim = buffer._read_sealed(claim_path,buffer.CLAIM_SCHEMA,'claim_sha256')
        if claim.get('operation_id') != operation_id or claim.get('entry_id') != record['entry_id']:
            return None
        _, release, routes, _ = cli.current_context()
        row = next(r for r in source_rows(release) if r['entry_id'] == record['entry_id'])
        _, entry, article = accepted_entry(cli.DEFAULT_ROOT,release,row)
        if (record['target_route'] != entry['target_route'] or record['topic_id'] != entry['topic_id']
                or entry['target_route'] not in routes
                or (not reconciled and publication.get('source_entry') != row)):
            return None
        live_kwargs = {}
        if lineage:
            live_kwargs['expected_html'] = release / 'public' / entry['target_route'].strip('/') / 'index.html'
        live = live_readback(entry['target_route'],article['title'],**live_kwargs)
        if lineage:
            if live.get('deployed_html_sha256') != lineage['article_html_sha256']:
                return None
            lineage = {**lineage, 'live_readback':live}
        if article_production_head_id() != current['deployment_id'] or article_current_base() != current:
            return None
        return {'status':'PASS_PUBLISHED','published':True,'entry_id':entry['entry_id'],
                'topic_id':entry['topic_id'],'route':entry['target_route'],'url':live['url'],
                'receipt_path':str(path),'reconciled_live':live,
                **({'publication_lineage':lineage} if lineage else {})}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, StopIteration):
        return None


def reconcile_published(root, release):
    """Seal only source-bound, live exact titles; never publish during recovery."""
    reconciled = []
    for row in source_rows(release):
        if not (root / 'entries' / row['entry_id'] / 'entry.json').is_file():
            continue
        manifest, entry, article = accepted_entry(root,release,row)
        live = live_readback(entry['target_route'],article['title'])
        pointer = json.loads((ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json').read_text())
        publication = {'status':'PASS_BUFFER_PUBLISHED','source_reconciled':True,
                       'source_entry':row,'current_base':buffer._spec(ROOT / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'),
                       'deployment_id':pointer.get('deployment_id'),'live':live}
        record_consumption(root,entry,'source-reconciled-' + entry['entry_id'],publication)
        for claim_path, claim in buffer._claim_rows(root):
            if claim['entry_id'] == entry['entry_id']:
                record_consumption(root,entry,claim['operation_id'],publication,claim_path)
        reconciled.append({'entry_id':entry['entry_id'],**live})
    return {'status':'PASS_SOURCE_RECONCILED','entries':reconciled}


def validate_next(root, release, routes, topics):
    rows = buffer.ready_entries(root,current_routes=routes,current_topic_ids=topics)
    if not rows:
        raise buffer.BufferEmptyError('article_buffer_empty_or_invalid')
    manifest, entry = rows[0]
    # Admission pins are historical; authenticate the complete accepted subject
    # against the currently installed validation chain before the source effect.
    controls = {p:digest(Path(p)) for p in entry['control_files']}
    evidence = buffer.validate_admission_evidence(manifest.parent / 'packet',Path(entry['pass_receipt']['path']),controls)
    if any(evidence[k] != entry[k] for k in ('topic_id','article_id','target_route')):
        raise RuntimeError('source_acceptance_identity_invalid')
    return manifest, entry


def preflight():
    import article_buffer_cli as cli
    _, release, routes, topics = cli.current_context()
    manifest, entry = validate_next(cli.DEFAULT_ROOT,release,routes,topics)
    return {'status':'PASS_SOURCE_MAIN_COMPATIBILITY','entry_id':entry['entry_id'],
            'route':entry['target_route'],'manifest':str(manifest),'release_root':str(release)}


def ensure_source_main(workspace, packet, entry):
    branch = 'carrier/accepted-' + entry['entry_id']
    if not workspace.exists():
        run(['git','clone','--quiet','--branch','main','--single-branch',f'git@github.com:{REPO}.git',workspace])
        run(['git','checkout','-b',branch],cwd=workspace)
    run(['git','fetch','origin','main'],cwd=workspace)
    # Read exact published branch first after any uncertain GitHub operation.
    prs = gh('pr','list','--repo',REPO,'--head',branch,'--state','all','--json','number,state,headRefOid,mergeCommit,url')
    if len(prs) > 1:
        raise RuntimeError('source_pr_identity_ambiguous')
    if prs and prs[0]['state'] == 'MERGED':
        merge = prs[0]['mergeCommit']['oid']
        run(['git','checkout','--detach',merge],cwd=workspace)
        land_packet(workspace,packet,entry)  # exact identity only, no second commit
        return prs[0]
    if prs and prs[0]['state'] == 'CLOSED':
        raise RuntimeError('source_pr_closed_without_merge')
    if not prs:
        run(['git','merge','--ff-only','origin/main'],cwd=workspace)
    changed = land_packet(workspace,packet,entry)
    if changed:
        run(['git','add',SOURCE_REL],cwd=workspace)
        run(['git','commit','-m',f'Publish accepted guide {entry["target_route"]}'],cwd=workspace)
    run(['git','push','origin',f'HEAD:refs/heads/{branch}'],cwd=workspace)
    if not prs:
        run(['gh','pr','create','--repo',REPO,'--base','main','--head',branch,
             '--title',f'Carrier: accepted guide {entry["target_route"]}',
             '--body',f'Registered host carrier. Exact independently accepted packet {entry["entry_id"]}; prose/QA/five-gate bytes unchanged. Native main release preserves secrets, Fella/provider holds, claim freshness, idempotency and rollback. No sends or n8n activation.'],timeout=90)
    pr = gh('pr','view',branch,'--repo',REPO,'--json','url,headRefOid')
    # GitHub may not have registered any checks when a new PR is returned.
    # Empty is UNKNOWN, not failure or green; wait boundedly on the exact head
    # before asking --watch (which exits immediately on an empty check set).
    deadline = time.monotonic() + 120
    while True:
        registered = gh('pr','view',pr['url'],'--json','headRefOid,statusCheckRollup')
        if registered.get('headRefOid') != pr['headRefOid']:
            raise RuntimeError('source_exact_head_ci_not_green')
        if registered.get('statusCheckRollup'):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('source_ci_registration_timeout')
        time.sleep(5)
    # Required existing site CI includes accepted-source contracts. The native
    # build and release safety gates run separately in glp_release_main.py.
    run(['gh','pr','checks',pr['url'],'--watch','--interval','10'],timeout=1800)
    readback = gh('pr','view',pr['url'],'--json','headRefOid,statusCheckRollup')
    checks = readback.get('statusCheckRollup') or []
    if readback['headRefOid'] != pr['headRefOid'] or not checks or any(c.get('conclusion') != 'SUCCESS' for c in checks):
        raise RuntimeError('source_exact_head_ci_not_green')
    run(['gh','pr','merge',pr['url'],'--merge','--match-head-commit',pr['headRefOid']],timeout=90)
    return gh('pr','view',pr['url'],'--json','url,state,headRefOid,mergeCommit')


def publication_receipt(pr, result, live, row):
    """Retain the exact accepted source/deployment for automatic health closure."""
    if not result.get('deployment_id'):
        raise RuntimeError('source_release_deployment_identity_missing')
    return {'status': 'PASS_BUFFER_PUBLISHED', 'source_pr': pr, 'main_release': result,
            'live': live, 'source_entry': row, 'deployment_id': result['deployment_id']}


def consume(args):
    import article_buffer_cli as cli
    with carrier_lock() as lock_fd:
        path = consumed_path(args.root,args.operation_id)
        if path.exists():
            return buffer._read_sealed(path,buffer.CONSUME_SCHEMA,'consumption_sha256')
        _, release, routes, topics = cli.current_context()
        reconcile_published(args.root,release)
        if path.exists():
            return buffer._read_sealed(path,buffer.CONSUME_SCHEMA,'consumption_sha256')
        claim_name = hashlib.sha256(args.operation_id.encode()).hexdigest() + '.json'
        claim = args.root / 'claims' / claim_name
        if claim.exists():
            claimed = buffer._read_sealed(claim,buffer.CLAIM_SCHEMA,'claim_sha256')
            manifest = buffer._verify_spec(claimed['entry_manifest'],'source_claim_entry')
            entry = buffer.validate_entry(manifest,current_routes=routes,current_topic_ids=topics)
        else:
            manifest, entry = validate_next(args.root,release,routes,topics)
            claim = buffer._claim_oldest_locked(root=args.root,operation_id=args.operation_id,
                         current_routes=routes,current_topic_ids=topics,base_state=cli.POINTER)
        controls = {p:digest(Path(p)) for p in entry['control_files']}
        evidence = buffer.validate_admission_evidence(manifest.parent / 'packet',Path(entry['pass_receipt']['path']),controls)
        if any(evidence[k] != entry[k] for k in ('topic_id','article_id','target_route')):
            raise RuntimeError('source_acceptance_identity_invalid')
        workspace = args.work_root / ('source-' + entry['entry_id'])
        workspace.parent.mkdir(parents=True,exist_ok=True)
        pr = ensure_source_main(workspace,manifest.parent / 'packet',entry)
        if pr.get('state') != 'MERGED':
            raise RuntimeError('source_merge_not_observed')
        output = run([sys.executable,ROOT / 'scripts/glp_release_main.py','--sha',pr['mergeCommit']['oid']],timeout=3600,lock_fd=lock_fd)
        result = cli.json_from_output(output)
        if result.get('status') not in {'PASS_PRODUCTION_TERMINAL','PASS_ALREADY_CURRENT'}:
            raise RuntimeError('source_release_not_terminal:' + json.dumps(result))
        article = json.loads((manifest.parent / 'packet/06-article.json').read_text())
        live = live_readback(entry['target_route'],article['title'])
        _, current, current_routes, _ = cli.current_context()
        if entry['target_route'] not in current_routes:
            raise RuntimeError('source_release_sitemap_missing')
        row = next((r for r in source_rows(current) if r['entry_id'] == entry['entry_id']),None)
        if row is None:
            raise RuntimeError('source_release_identity_missing')
        accepted_entry(args.root,current,row)
        publication = publication_receipt(pr, result, live, row)
        return record_consumption(args.root,entry,args.operation_id,publication,claim)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode',choices=['preflight','reconcile'])
    args = p.parse_args()
    if args.mode == 'preflight':
        result = preflight()
    else:
        import article_buffer_cli as cli
        with carrier_lock():
            _,release,_,_ = cli.current_context()
            result = reconcile_published(cli.DEFAULT_ROOT,release)
    print(json.dumps(result,sort_keys=True))

if __name__ == '__main__':
    main()
