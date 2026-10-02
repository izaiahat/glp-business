#!/usr/bin/env python3
"""Capture, compare and stage evidence only; never apply to a candidate.

Containment is a mechanical observation, not semantic approval. A reviewed
ADJUDICATION.json is required to produce a ready patch. Missing decisions fail
closed and keep original dates. Cached reruns make no provider requests.
"""
import argparse
import copy
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urljoin, parse_qsl

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANDIDATE = ROOT / 'site-system/flagship-v2/candidates/glp-commercial-w3-20260905'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def stable(value):
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False))


def read(path):
    return json.loads(Path(path).read_text())


def put(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, indent=2, ensure_ascii=False) + '\n'
    if not path.exists() or path.read_text() != text:
        path.write_text(text)


def select_stale(document, as_of, within_days):
    if within_days < 0:
        raise ValueError('negative horizon')
    now = datetime.fromisoformat(as_of.replace('Z', '+00:00'))
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    selected = []
    for pid, row in document['programs'].items():
        date = datetime.fromisoformat(row['verified_as_of'].replace('Z', '+00:00'))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        age = (now - date).total_seconds() / 86400
        if age > document['freshness_max_days'] - within_days:
            selected.append({'program_id': pid, 'age_days': age, 'freshness_max_days': document['freshness_max_days']})
    return sorted(selected, key=lambda x: (-x['age_days'], x['program_id']))


def safe_url(url, hosts):
    if re.search(r'[<>\\\\\s]', url):
        return False
    u = urlsplit(url)
    if u.scheme != 'https' or u.username or u.password or u.port not in (None, 443):
        return False
    host = (u.hostname or '').lower().removeprefix('www.')
    hosts = {h.lower().removeprefix('www.') for h in hosts}
    if host not in hosts or host in {'localhost', '127.0.0.1'}:
        return False
    if re.search(r'/(go|click|track|redirect|checkout|login|signup|register)(/|$)', u.path, re.I):
        return False
    if re.search(r'\.(png|jpe?g|gif|webp|svg|ico|mp4|webm|woff2?|css|js)$', u.path, re.I):
        return False
    if any(re.search(r'aff|ref|click|subid|utm|token|code', k, re.I) for k, _ in parse_qsl(u.query)):
        return False
    return not re.search(r'fella', host, re.I)


def load_captures(captures):
    output = {}
    for key, cap in captures.items():
        if not cap.get('markdown_path'):
            continue
        if cap.get('usable_evidence') is False or cap.get('success') is False or cap.get('status_code', 200) != 200:
            continue
        path = Path(cap['markdown_path'])
        if sha(path) != cap['markdown_sha256']:
            raise ValueError('capture hash mismatch: ' + key)
        if not cap.get('captured_at_utc') or not cap.get('source_url'):
            raise ValueError('capture provenance missing: ' + key)
        output[key] = {**cap, 'text': path.read_text()}
    return output


def canonical_urls(profile, pricing):
    urls = list(profile.get('source_urls', []))
    urls += [x['source_url'] for x in pricing.get('medications', []) if x.get('source_url')]
    hosts = {urlsplit(u).hostname for u in profile.get('source_urls', []) if urlsplit(u).hostname}
    urls += [urlsplit(u)._replace(path='/', query='', fragment='').geturl() for u in urls]
    return sorted({u for u in urls if safe_url(u, hosts)}), hosts


def capture(profile, pricing, out, offline=False):
    """Bounded first-party capture; explicit network invocation only."""
    out = Path(out)
    manifest = out / 'CAPTURES.json'
    if manifest.exists():
        saved = read(manifest)
        load_captures(saved)
        return saved
    if offline:
        return {}
    import os
    import requests
    key = os.environ.get('FIRECRAWL_API_KEY')
    if not key:
        # Explicit task authorization; consume only the named key, never serialize.
        for line in (Path.home() / '.hermes/.env').read_text().splitlines():
            if line.startswith('FIRECRAWL_API_KEY='):
                key = line.split('=', 1)[1].strip().strip('\"\'')
                break
    if not key:
        raise ValueError('FIRECRAWL_AUTH_UNAVAILABLE')
    queue, hosts = canonical_urls(profile, pricing)
    saved = {}
    seen = set()
    while queue and len(seen) < 12:
        url = queue.pop(0)
        if url in seen or not safe_url(url, hosts):
            continue
        seen.add(url)
        stamp = datetime.now(timezone.utc).isoformat()
        name = digest(url)[:16]
        try:
            response = requests.post('https://api.firecrawl.dev/v1/scrape', headers={'Authorization': 'Bearer ' + key}, json={'url': url, 'formats': ['markdown'], 'maxAge': 0, 'onlyMainContent': False}, timeout=75)
            data = response.json() if response.status_code == 200 else {}
            body = data.get('data', {}).get('markdown')
            metadata = data.get('data', {}).get('metadata', {})
            final_url = metadata.get('sourceURL', url)
            if not body or not safe_url(final_url, hosts) or metadata.get('statusCode', 200) != 200:
                saved[name] = {'source_url': url, 'captured_at_utc': stamp, 'status': 'BLOCKED', 'http_status': response.status_code, 'reason': 'NO_VALID_FIRST_PARTY_BODY'}
            else:
                path = out / 'captures' / (name + '.md')
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
                saved[name] = {'source_url': url, 'captured_at_utc': stamp, 'status': 'CAPTURED', 'forced_fresh': True, 'markdown_path': str(path.resolve()), 'markdown_sha256': sha(path)}
                for link in re.findall(r'\]\((https?://[^\s)]+)', body):
                    if safe_url(link, hosts) and re.search(r'pricing|terms|treatment|weight|semaglutide|tirzepatide', urlsplit(link).path, re.I):
                        if link not in seen and link not in queue:
                            queue.append(link)
        except (requests.RequestException, ValueError):
            saved[name] = {'source_url': url, 'captured_at_utc': stamp, 'status': 'BLOCKED', 'reason': 'CAPTURE_TRANSPORT_OR_PARSE_FAILURE'}
        put(manifest, saved)
    return saved


def leaves(value, prefix=''):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from leaves(child, prefix + '.' + key if prefix else key)
    elif isinstance(value, list) and value and isinstance(value[0], dict):
        for i, child in enumerate(value):
            yield from leaves(child, prefix + '.' + str(i))
    else:
        yield prefix, value


def fact_value(stored, path):
    value = stored
    for key in path.split('.'):
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def mechanical_compare(stored, captures):
    bodies = load_captures(captures)
    facts = []
    for section in ['profile', 'taxonomy', 'pricing']:
        for path, value in leaves(stored.get(section, {}), section):
            token = str(value) if value is not None and not isinstance(value, (list, dict, bool)) else None
            matches = [k for k, cap in bodies.items() if token and token in cap['text']]
            facts.append({'path': path, 'stored_value': value, 'mechanical': 'CONTAINED' if matches else 'NOT_CONTAINED', 'capture_keys': matches, 'status': 'NEEDS_ADJUDICATION', 'reason': 'Containment does not prove fact scope, schedule or clinical/commercial authority.'})
    return facts


def exclude_unsupported(rows, facts):
    excluded = set()
    for fact in facts:
        match = re.match(r'pricing\.medications\.(\d+)(?:\.|$)', fact['path'])
        if match and fact['status'] == 'UNSUPPORTED':
            excluded.add(int(match.group(1)))
    return [copy.deepcopy(row) for i, row in enumerate(rows) if i not in excluded]


def pointer(key):
    return key.replace('~', '~0').replace('/', '~1')


def diff(old, new, path=''):
    if old == new:
        return []
    if isinstance(old, dict) and isinstance(new, dict):
        operations = []
        for key in new:
            target = path + '/' + pointer(key)
            operations += [{'op': 'add', 'path': target, 'value': new[key]}] if key not in old else diff(old[key], new[key], target)
        for key in old:
            if key not in new:
                operations += [{'op': 'test', 'path': path + '/' + pointer(key), 'value': old[key]}, {'op': 'remove', 'path': path + '/' + pointer(key)}]
        return operations
    return [{'op': 'test', 'path': path, 'value': old}, {'op': 'replace', 'path': path, 'value': new}]


def apply_patch(value, operations):
    value = copy.deepcopy(value)
    for operation in operations:
        keys = [k.replace('~1', '/').replace('~0', '~') for k in operation['path'].split('/')[1:]]
        parent = value
        for key in keys[:-1]:
            parent = parent[int(key)] if isinstance(parent, list) else parent[key]
        key = int(keys[-1]) if isinstance(parent, list) else keys[-1]
        if operation['op'] == 'test':
            if parent[key] != operation['value']:
                raise ValueError('stale patch precondition: ' + operation['path'])
        elif operation['op'] in ('replace', 'add'):
            parent[key] = copy.deepcopy(operation['value'])
        elif operation['op'] == 'remove':
            del parent[key]
        else:
            raise ValueError('unsupported patch operation')
    return value


def adjudicated_projection(pid, stored, decision, bodies, out):
    """Build registry bindings only after all consumer facts have decisions."""
    facts = {f['path']: f for f in decision['facts']}
    profile = copy.deepcopy(stored['profile'])
    tax = copy.deepcopy(stored['taxonomy'])
    required = ['profile.taxonomy.' + k for k in profile['taxonomy']]
    required += ['profile.calculator.' + k for k in profile['calculator'] if k not in {'checked_as_of', 'evidence_sha256'}]
    missing = [p for p in required if p not in facts]
    if missing:
        return {**decision, 'missing_decisions': missing}
    for path in required:
        f = facts[path]
        if f['status'] == 'UNSUPPORTED':
            # Excluding an unsupported price row does not resolve other fields.
            return {**decision, 'missing_decisions': [path + ': explicit corrected fact required']}
        _, section, field = path.split('.')
        profile[section][field] = copy.deepcopy(f['current_value'])
    date = min(c['captured_at_utc'][:10] for c in bodies.values())
    profile['verified_as_of'] = date
    profile['calculator']['checked_as_of'] = date
    profile['calculator']['evidence_sha256'] = stable({k: v for k, v in profile['calculator'].items() if k != 'evidence_sha256'})
    profile['source_urls'] = sorted({c['source_url'] for c in bodies.values()})
    identity = ['aliases', 'program_id', 'provider', 'registry_program_ids', 'source_urls']
    identity_text = json.dumps({k: profile[k] for k in identity}, sort_keys=True, separators=(',', ':'))
    text = '\n\n'.join(c['text'] for c in bodies.values())
    source = next(iter(bodies.values()))['source_url']
    profile['evidence_bindings'] = [
        {'fields': sorted(identity), 'source_url': source, 'verified_as_of': date, 'evidence_kind': 'registry_identity_projection', 'evidence_text': identity_text, 'evidence_sha256': digest(identity_text)},
        {'fields': sorted([p.removeprefix('profile.') for p in required] + ['calculator.checked_as_of', 'verified_as_of']), 'source_url': source, 'verified_as_of': date, 'evidence_text': text, 'evidence_sha256': digest(text), 'capture_paths': [c['markdown_path'] for c in bodies.values()]}
    ]
    authority_file = out / 'ADJUDICATED-EVIDENCE.json'
    put(authority_file, {'program_id': pid, 'facts': decision['facts'], 'captures': decision['captures'], 'provenance': 'Reported information'})
    name = 'evidence_refresh_' + pid.replace('-', '_')
    try:
        authority_path = str(authority_file.relative_to(ROOT))
    except ValueError:
        raise ValueError('reviewed authority output must be under workspace reports/')
    if not authority_path.startswith('reports/'):
        raise ValueError('authority output must be under reports/')
    authority = {'path': authority_path, 'sha256': sha(authority_file)}
    tax.update(copy.deepcopy(profile['taxonomy']))
    tax['verified_as_of'] = date
    tax['source_urls'] = profile['source_urls']
    tax['evidence_bindings'] = [{'authority': name, 'authority_sha256': authority['sha256'], 'source_url': c['source_url'], 'verified_as_of': date, 'evidence_sha256': c['markdown_sha256']} for c in bodies.values()]
    return {**decision, 'proposed_profile': profile, 'proposed_taxonomy': tax, 'authorities': {name: authority}}


def declared_aliases(reg, owner):
    """Resolve only the preserved render/network crosswalk, never a name match."""
    profile = reg['provider_dynamic_profiles']['profiles'][owner]
    aliases = {}
    for alias in profile.get('aliases', []):
        rid, member = alias['render_id'], alias['program_id']
        rows = [r for r in reg['programs'] if r['program_id'] == member]
        tax = reg['therapeutic_taxonomy']['programs'].get(rid)
        if (rid == owner or rid in aliases or rid in reg['provider_dynamic_profiles']['profiles']
                or member not in profile['registry_program_ids'] or len(rows) != 1
                or (rows[0].get('review_slug') or member) != rid
                or not tax or tax['registry_program_id'] != member):
            raise ValueError('invalid declared taxonomy alias: ' + rid)
        aliases[rid] = tax
    return aliases


def alias_owner(reg, pid):
    owners = [owner for owner, profile in reg['provider_dynamic_profiles']['profiles'].items()
              if any(a.get('render_id') == pid for a in profile.get('aliases', []))]
    if len(owners) != 1 or pid not in declared_aliases(reg, owners[0]):
        raise ValueError('unique declared taxonomy alias owner required: ' + pid)
    return owners[0]


def project_alias_taxonomy(original, canonical):
    """Retain only supported existing alias facets; never copy permissions."""
    alias = copy.deepcopy(original)
    for field in ['molecule_tags', 'brand_name_tags', 'delivery_form_tags', 'treatment_type_tags']:
        alias[field] = [tag for tag in original[field] if tag in canonical[field]]
    alias['treatment_type_state'] = 'verified' if alias['treatment_type_tags'] else 'not_verified'
    for field in ['verified_as_of', 'source_urls', 'evidence_bindings']:
        alias[field] = copy.deepcopy(canonical[field])
    return alias


def generate_alias_receipt(pid, owner, reg, matrix, out, candidate):
    """A hash-bound companion to one canonical transaction, not a second writer."""
    parent = out.parent / owner
    packet_path, patch_path = parent / 'PACKET.json', parent / 'DATA-PATCH.json'
    packet, patch = read(packet_path), read(patch_path)
    if packet.get('status') != 'PACKET_READY' or packet.get('program_id') != owner:
        raise ValueError('canonical alias dependency not ready: ' + owner)
    expected = {str(candidate / 'data' / name) for name in ['program-registry.json', 'provider-pricing-matrix.json']}
    if {t['path'] for t in patch['targets']} != expected or len(patch['targets']) != len(expected):
        raise ValueError('canonical alias dependency target mismatch: ' + owner)
    for target in patch['targets']:
        if sha(target['path']) != target['source_sha256']:
            raise ValueError('canonical alias dependency candidate drift: ' + owner)
        projected = apply_patch(read(target['path']), target['operations'])
        put(out / 'offline/data' / Path(target['path']).name, projected)
    projected_reg = read(out / 'offline/data/program-registry.json')
    alias = projected_reg['therapeutic_taxonomy']['programs'][pid]
    expected_alias = project_alias_taxonomy(reg['therapeutic_taxonomy']['programs'][pid], projected_reg['therapeutic_taxonomy']['programs'][owner])
    if {k: v for k, v in alias.items() if k != 'normalized_sha256'} != {k: v for k, v in expected_alias.items() if k != 'normalized_sha256'}:
        raise ValueError('canonical transaction lacks exact alias projection: ' + pid)
    result = subprocess.run(['node', str(Path(__file__).with_name('evidence_refresh_verify.mjs')), str(candidate), str(out), owner], capture_output=True, text=True)
    validation = read(out / 'VALIDATION.json')
    if result.returncode != 0 or not all(v.get('status') == 'PASS' for v in validation['target_validators'].values()):
        raise ValueError('canonical alias dependency validation failed: ' + pid)
    dependency = {'program_id': owner, 'packet_path': str(packet_path), 'packet_sha256': sha(packet_path),
                  'patch_path': str(patch_path), 'patch_sha256': sha(patch_path),
                  'apply_rule': 'Apply this canonical transaction once; alias companion has no duplicate operations.'}
    put(out / 'STORED.json', {'taxonomy': reg['therapeutic_taxonomy']['programs'][pid], 'canonical_program_id': owner})
    put(out / 'CAPTURES.json', packet['captures'])
    put(out / 'DATA-PATCH.json', {**patch, 'depends_on': dependency, 'targets': [{**t, 'operations': []} for t in patch['targets']]})
    put(out / 'PACKET.json', {'program_id': pid, 'canonical_program_id': owner, 'status': 'PACKET_READY',
                            'provenance': 'Reported information', 'depends_on': dependency,
                            'captures': packet['captures'], 'facts_subject_program_id': owner,
                            'facts': packet['facts'], 'proposed_taxonomy': alias})
    (out / 'DIFF.md').write_text('# Evidence refresh: ' + pid + '\n\nPACKET_READY: declared taxonomy-only alias of ' + owner + '.\n'
                               'The canonical patch contains the alias projection; apply once, never duplicate the dynamic profile.\n'
                               'Alias identity, render label and commercial link states are preserved.\n'
                               'Source quotes and decisions: ' + str(parent / 'DIFF.md') + '\n')
    with (out / 'DIFF.md').open('a') as report:
        for fact in packet['facts']:
            if fact['path'].startswith('profile.taxonomy.'):
                report.write('\n- Canonical evidence `' + fact['path'] + '`: ' + fact.get('disposition', '')
                             + '\n  Capture: ' + fact['source_capture_path'] + '\n  Quote: ' + json.dumps(fact['quote']) + '\n')
    return 'PACKET_READY'


def _generate(pid, out, candidate=DEFAULT_CANDIDATE, adjudication=None, dry_run=True):
    candidate, out = Path(candidate).resolve(), Path(out).resolve()
    if out == candidate or candidate in out.parents or out in candidate.parents:
        raise ValueError('output must not overlap candidate')
    reg_path, mat_path = candidate / 'data/program-registry.json', candidate / 'data/provider-pricing-matrix.json'
    reg, matrix = read(reg_path), read(mat_path)
    if pid not in reg['provider_dynamic_profiles']['profiles']:
        return generate_alias_receipt(pid, alias_owner(reg, pid), reg, matrix, out, candidate)
    profile = reg['provider_dynamic_profiles']['profiles'][pid]
    if 'fella' in pid.lower() or 'fella' in profile.get('provider', '').lower():
        raise ValueError('permanently excluded provider')
    stored = {'profile': profile, 'taxonomy': reg['therapeutic_taxonomy']['programs'][pid], 'pricing': matrix['providers'].get(pid, {}), 'sources': [{'path': str(p), 'sha256': sha(p)} for p in [reg_path, mat_path]]}
    put(out / 'STORED.json', stored)
    decision = read(adjudication) if adjudication else {}
    caps = decision.get('captures') or capture(profile, stored['pricing'], out, offline=dry_run)
    put(out / 'CAPTURES.json', caps)
    bodies = load_captures(caps)
    mechanical = mechanical_compare(stored, caps)
    facts = decision.get('facts', [])
    valid = bool(facts and bodies and decision.get('review_complete'))
    review_errors = []
    if any(c.get('forced_fresh') is False for c in bodies.values()):
        valid = False
        review_errors.append('Captures explicitly used provider default cache; fresh-fetch provenance required before advancing dates.')
    capture_paths = {str(Path(c['markdown_path']).resolve()): c for c in bodies.values()}
    for fact in facts:
        if fact.get('status') not in {'SUPPORTED', 'CHANGED', 'UNSUPPORTED'}:
            valid = False
            review_errors.append(fact['path'] + ': unresolved semantic disposition')
            continue
        p = fact.get('source_capture_path')
        quote = fact.get('quote')
        if not p or not quote or not Path(p).exists() or quote not in Path(p).read_text():
            valid = False
            review_errors.append(fact['path'] + ': exact quote absent from cited capture')
        elif fact.get('source_capture_sha256') != sha(p):
            valid = False
            review_errors.append(fact['path'] + ': capture hash mismatch')
        if p and str(Path(p).resolve()) not in capture_paths:
            valid = False
            review_errors.append(fact['path'] + ': source not in capture manifest')
        elif p:
            cap = capture_paths[str(Path(p).resolve())]
            if fact.get('source_url') != cap['source_url'] or fact.get('captured_at_utc') != cap['captured_at_utc']:
                valid = False
                review_errors.append(fact['path'] + ': provenance mismatch')
        try:
            prior = fact_value(stored, fact['path'])
            if prior != fact.get('stored_value'):
                valid = False
                review_errors.append(fact['path'] + ': stored value drift')
        except (KeyError, IndexError, TypeError):
            # Explicit supplemental observations do not overwrite stored facts.
            if not fact['path'].endswith('.published_headline'):
                valid = False
                review_errors.append(fact['path'] + ': unknown fact path')
    if valid and not decision.get('proposed_profile'):
        decision = adjudicated_projection(pid, stored, {**decision, 'captures': caps}, bodies, out)
    packet = {'program_id': pid, 'provenance': 'Reported information', 'stored': stored, 'captures': caps, 'mechanical_facts': mechanical, 'facts': facts, 'status': 'NEEDS_ADJUDICATION', 'review_errors': review_errors, 'missing_decisions': decision.get('missing_decisions', []), 'limits': ['No clinical, legal, network approval or release acceptance implied.']}
    next_reg, next_matrix = copy.deepcopy(reg), copy.deepcopy(matrix)
    if valid and decision.get('proposed_profile') and decision.get('proposed_taxonomy'):
        proposed = decision['proposed_profile']
        taxonomy = decision['proposed_taxonomy']
        fact_map = {f['path']: f for f in facts}
        required = ['profile.taxonomy.' + k for k in profile['taxonomy']]
        required += ['profile.calculator.' + k for k in profile['calculator'] if k not in {'checked_as_of', 'evidence_sha256'}]
        for path in required:
            fact = fact_map.get(path)
            if not fact or fact['status'] == 'UNSUPPORTED' or fact.get('current_value') != fact_value({'profile': proposed}, path):
                raise ValueError('proposed fact lacks matching reviewed disposition: ' + path)
        if proposed['taxonomy'] != {k: taxonomy[k] for k in proposed['taxonomy']}:
            raise ValueError('proposed taxonomy/profile conflict')
        captured_dates = {c['captured_at_utc'][:10] for c in bodies.values()}
        if proposed['verified_as_of'] not in captured_dates or taxonomy['verified_as_of'] not in captured_dates:
            raise ValueError('freshness date not backed by capture')
        if not set(proposed['source_urls']).issubset({c['source_url'] for c in bodies.values()}):
            raise ValueError('uncaptured proposed source URL')
        # Immutable identity and permission boundary: evidence cannot change these.
        for field in ['program_id', 'registry_program_ids', 'provider', 'aliases', 'version']:
            if proposed.get(field) != profile.get(field):
                raise ValueError('identity mutation refused: ' + field)
        for field in ['program_id', 'registry_program_id', 'classification_state', 'commercial_link_state', 'render_label']:
            if taxonomy.get(field) != stored['taxonomy'].get(field):
                raise ValueError('authority mutation refused: ' + field)
        next_reg['provider_dynamic_profiles']['profiles'][pid] = proposed
        next_reg['therapeutic_taxonomy']['programs'][pid] = taxonomy
        packet['alias_projections'] = {}
        for alias_id, original in declared_aliases(reg, pid).items():
            alias_taxonomy = project_alias_taxonomy(original, taxonomy)
            next_reg['therapeutic_taxonomy']['programs'][alias_id] = alias_taxonomy
            packet['alias_projections'][alias_id] = {'stored': original, 'proposed': alias_taxonomy,
                                                   'basis': 'Preserved declared alias; supported existing facets only; own permissions unchanged.'}
        for name, authority in decision.get('authorities', {}).items():
            source = ROOT / authority['path']
            if sha(source) != authority['sha256']:
                raise ValueError('authority hash mismatch')
            next_reg['therapeutic_taxonomy']['authorities'][name] = authority
        kept = exclude_unsupported(stored['pricing'].get('medications', []), facts)
        retained = []
        for i, row in enumerate(stored['pricing'].get('medications', [])):
            if row not in kept:
                continue
            review = decision.get('price_schedules', {}).get(str(i))
            if not review or review.get('schedule_verified') is not True:
                raise ValueError('retained comparable price row requires explicit schedule adjudication: ' + str(i))
            cap = bodies[review['capture_key']]
            if not review.get('quote') or review['quote'] not in cap['text']:
                raise ValueError('price schedule quote missing from source')
            next_row = copy.deepcopy(row)
            next_row['captured_on'] = cap['captured_at_utc'][:10]
            next_row['capture_sha256'] = cap['markdown_sha256']
            retained.append(next_row)
        kept = retained
        if len(kept) != len(stored['pricing'].get('medications', [])):
            next_matrix['providers'][pid]['medications'] = kept
            next_matrix['providers'][pid]['unavailable_reason'] = decision.get('unavailable_reason') if not kept else None
        elif kept:
            next_matrix['providers'][pid]['medications'] = kept
        packet['status'] = 'PROPOSED_PENDING_VALIDATION'
    put(out / 'PACKET.json', packet)
    put(out / 'offline/data/program-registry.json', next_reg)
    put(out / 'offline/data/provider-pricing-matrix.json', next_matrix)
    result = subprocess.run(['node', str(Path(__file__).with_name('evidence_refresh_verify.mjs')), str(candidate), str(out), pid], capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise ValueError('validator runtime failure: ' + result.stderr[:500])
    validation = read(out / 'VALIDATION.json')
    for source in stored['sources']:
        if sha(source['path']) != source['sha256']:
            raise ValueError('candidate source changed during generation: ' + source['path'])
    # Never create an apparently actionable partial patch for unresolved facts.
    if packet['status'] == 'NEEDS_ADJUDICATION':
        put(out / 'DATA-PATCH.json', {'schema': 'ws-r-json-patch-v1', 'applied_to_candidate': False, 'targets': [{'path': str(p), 'source_sha256': sha(p), 'operations': []} for p in [reg_path, mat_path]]})
    elif all(v.get('status') == 'PASS' for v in validation['target_validators'].values()):
        packet['status'] = 'PACKET_READY'
    else:
        packet['status'] = 'BLOCKED'
    put(out / 'PACKET.json', packet)
    lines = ['# Evidence refresh: ' + pid, '', 'Status: ' + packet['status'], 'Proposed only; original authority, holds and freshness thresholds unchanged.', '', '## Per-fact comparison']
    for fact in facts or mechanical:
        lines.append('- `' + fact['path'] + '` ' + fact['status'] + ': ' + fact.get('disposition', fact.get('reason', '')) + ' Quote: ' + json.dumps(fact.get('quote')) + ' Capture: ' + str(fact.get('source_capture_path', 'UNKNOWN')))
    diff_text = '\n'.join(lines) + '\n'
    if not (out / 'DIFF.md').exists() or (out / 'DIFF.md').read_text() != diff_text:
        (out / 'DIFF.md').write_text(diff_text)
    return packet['status']


def generate(pid, out, candidate=DEFAULT_CANDIDATE, adjudication=None, dry_run=True):
    out, candidate = Path(out).resolve(), Path(candidate).resolve()
    if out == candidate or candidate in out.parents or out in candidate.parents:
        raise ValueError('output must not overlap candidate')
    try:
        return _generate(pid, out, candidate, adjudication, dry_run)
    except (ValueError, KeyError, FileNotFoundError) as exc:
        reason = type(exc).__name__ + ': ' + str(exc)
        put(out / 'PACKET.json', {'program_id': pid, 'status': 'BLOCKED', 'reason': reason, 'provenance': 'Reported information; no changes authorized'})
        put(out / 'VALIDATION.json', {'status': 'BLOCKED', 'reason': reason, 'applied_to_candidate': False})
        targets = []
        for name in ['program-registry.json', 'provider-pricing-matrix.json']:
            path = candidate / 'data' / name
            if path.exists():
                targets.append({'path': str(path), 'source_sha256': sha(path), 'operations': []})
        put(out / 'DATA-PATCH.json', {'schema': 'ws-r-json-patch-v1', 'applied_to_candidate': False, 'targets': targets})
        (out / 'DIFF.md').write_text('# ' + pid + '\n\nBLOCKED: ' + reason + '\nNo fact or date is changed. Resolve the named input/adjudication gap before regeneration.\n')
        return 'BLOCKED'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program')
    parser.add_argument('--stale-within-days', type=float)
    parser.add_argument('--dry-run', action='store_true', help='offline only; never calls provider')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, default=DEFAULT_CANDIDATE)
    parser.add_argument('--adjudication', type=Path)
    args = parser.parse_args()
    if args.stale_within_days is not None:
        document = read(args.candidate / 'data/program-registry.json')['therapeutic_taxonomy']
        print(json.dumps(select_stale(document, datetime.now(timezone.utc).isoformat(), args.stale_within_days), indent=2))
    if args.program:
        print(json.dumps({'program_id': args.program, 'status': generate(args.program, args.out, args.candidate, args.adjudication, args.dry_run)}))
    elif args.stale_within_days is None:
        parser.error('--program or --stale-within-days required')


if __name__ == '__main__':
    main()
