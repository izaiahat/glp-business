"""Receive successful registered taxonomy writes; never renew economic clocks."""
import ast
import copy
import json
from pathlib import Path
import re
import subprocess

FIELDS = ('molecule_tags', 'brand_name_tags', 'delivery_form_tags', 'treatment_type_tags')
METADATA = {'verified_as_of', 'source_urls', 'evidence_bindings', 'normalized_sha256'}


def project_verified_row(base, pid, refreshed, authority_name, authority):
    """Only source/freshness metadata changes; no fact or permission changes."""
    old = base['therapeutic_taxonomy']['programs'][pid]
    if {k: v for k, v in old.items() if k not in METADATA} != {k: v for k, v in refreshed.items() if k not in METADATA}:
        raise ValueError('REGISTERED_TAXONOMY_FACT_OR_AUTHORITY_CHANGE')
    result = copy.deepcopy(base)
    existing = result['therapeutic_taxonomy']['authorities'].get(authority_name)
    if existing is not None and existing != authority:
        raise ValueError('REGISTERED_TAXONOMY_AUTHORITY_COLLISION')
    result['therapeutic_taxonomy']['programs'][pid] = copy.deepcopy(refreshed)
    result['therapeutic_taxonomy']['authorities'][authority_name] = copy.deepcopy(authority)
    return result


def policy_functions(d):
    tree = ast.parse(d.TAXONOMY_PRODUCER.read_text())
    policies = ast.literal_eval(next(n.value for n in tree.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == 'POLICIES' for t in n.targets)))
    policies.update(d.read(d.TAXONOMY_POLICIES))
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {'normalized', 'support'}]
    if len(nodes) != 2:
        raise ValueError('REGISTERED_POLICY_FUNCTIONS_MISSING')
    namespace = {'re': re, 'FIELDS': FIELDS}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(d.TAXONOMY_PRODUCER), 'exec'), namespace)
    return policies, namespace['support']


def verified_row(d, er, pid, base_row, profile, captures, day):
    """Recheck the successful writer, current policy, and every underlying body."""
    from datetime import datetime
    from urllib.parse import urlsplit
    if 'fella' in pid or not re.fullmatch('[a-z0-9][a-z0-9-]*', pid):
        return None
    if not d.TAXONOMY_REGISTRY.is_file():
        return None
    registry_sha = d.sha(d.TAXONOMY_REGISTRY)
    registry = d.read(d.TAXONOMY_REGISTRY)
    row = registry['therapeutic_taxonomy']['programs'].get(pid)
    if not row or row.get('verified_as_of') != day:
        return None
    if {k: v for k, v in row.items() if k not in METADATA} != {k: v for k, v in base_row.items() if k not in METADATA}:
        return None  # Changes require full factual adjudication, not clock-only intake.
    names = {b['authority'] for b in row['evidence_bindings']}
    if len(names) != 1 or not next(iter(names)).startswith('taxonomy_refresh_'):
        return None
    name = next(iter(names));authority = registry['therapeutic_taxonomy']['authorities'][name]
    path = d.ROOT / authority['path']
    lane = d.ROOT / 'reports/ops/glp38-20260915/taxonomy-refresh'
    if not path.is_relative_to(lane) or path.name != pid + '-verification.json' or path.resolve() != path:
        raise ValueError('REGISTERED_TAXONOMY_AUTHORITY_PATH')
    if d.sha(path) != authority['sha256']:
        raise ValueError('REGISTERED_TAXONOMY_AUTHORITY_DRIFT')
    manifest = d.read(path);result_path = path.parent / 'RESULT.json';result = d.read(result_path)
    written = [r for r in result.get('written', []) if r.get('program_id') == pid]
    if result.get('status') not in {'PASS', 'REVIEW_REQUIRED'} or not written:
        return None
    if (result.get('producer') != str(d.TAXONOMY_PRODUCER) or len(written) != 1
            or written[0]['normalized_sha256'] != row['normalized_sha256']
            or written[0]['verified_as_of'] != day or manifest.get('program_id') != pid
            or manifest.get('verified_as_of') != day
            or manifest.get('tags_unchanged', manifest.get('verified_tags')) != {f: row[f] for f in FIELDS}):
        raise ValueError('REGISTERED_TAXONOMY_WRITER_BINDING')
    policies, support = policy_functions(d);policy = policies[pid]
    if policy.get('review_required'):
        return None
    specs = policy.get('sources', [{**policy, 'url': policy.get('linked_url', policy.get('url'))}])
    sources = manifest.get('sources', [manifest])
    if len(sources) != len(specs):
        raise ValueError('REGISTERED_TAXONOMY_SOURCE_SET')
    expected_bindings = [{'authority': name, 'authority_sha256': authority['sha256'],
        'source_url': s['source_url'], 'verified_as_of': s['verified_as_of'],
        'evidence_sha256': s['capture_sha256']} for s in sources]
    if row['evidence_bindings'] != expected_bindings:
        raise ValueError('REGISTERED_TAXONOMY_EVIDENCE_BINDING')
    hosts = {urlsplit(u).hostname for u in profile['source_urls']}
    observed = {f: [] for f in FIELDS}
    dependencies = {str(d.TAXONOMY_REGISTRY): registry_sha, str(path): d.sha(path), str(result_path): d.sha(result_path)}
    if policy.get('linked_url'):
        primary_path = path.parent / (pid + '-primary.json')
        primary = d.read(primary_path)
        if (primary.get('request', {}).get('url') != policy['url']
                or primary.get('request', {}).get('maxAge') != 0
                or primary.get('response', {}).get('success') is not True
                or primary['response']['data'].get('metadata', {}).get('statusCode') != 200
                or policy.get('intermediate_url', policy['linked_url']) not in primary['response']['data'].get('links', [])):
            return None
        dependencies[str(primary_path)] = d.sha(primary_path)
        if policy.get('intermediate_url'):
            middle_path = path.parent / (pid + '-intermediate.json')
            middle = d.read(middle_path)
            if (middle.get('request', {}).get('url') != policy['intermediate_url']
                    or middle.get('request', {}).get('maxAge') != 0
                    or middle.get('response', {}).get('success') is not True
                    or middle['response']['data'].get('metadata', {}).get('statusCode') != 200
                    or policy['linked_url'] not in middle['response']['data'].get('links', [])):
                return None
            dependencies[str(middle_path)] = d.sha(middle_path)
    for source, spec in zip(sources, specs):
        capture_path = d.ROOT / source['capture_path']
        if not capture_path.is_relative_to(lane) or capture_path.resolve() != capture_path or d.sha(capture_path) != source['capture_sha256']:
            raise ValueError('REGISTERED_TAXONOMY_CAPTURE_DRIFT')
        raw = d.read(capture_path);data = raw.get('response', {}).get('data', {});meta = data.get('metadata', {})
        url = spec['url'];received = raw['received_at'];started = raw['started_at']
        stamp = lambda v: datetime.fromisoformat(v.replace('Z', '+00:00'))
        if (source['source_url'] != url or raw['request'].get('url') != url
                or raw['request'].get('maxAge') != 0 or raw['response'].get('success') is not True
                or meta.get('statusCode') != 200 or not data.get('markdown')
                or source['captured_at'] != received or received[:10] != day or source['verified_as_of'] != day
                or stamp(started) > stamp(received) or not er.safe_url(url, hosts)
                or any(not er.safe_url(meta[k], hosts) for k in ('url', 'sourceURL') if meta.get(k))
                or str(meta.get('cacheState', '')).lower() in {'hit', 'cached'}
                or (meta.get('cachedAt') and stamp(meta['cachedAt']) < stamp(started))):
            raise ValueError('REGISTERED_TAXONOMY_CAPTURE_BINDING')
        matches = [c for c in captures.values() if c.get('raw_capture_path') == str(capture_path)
                   and c.get('raw_capture_sha256') == d.sha(capture_path)
                   and c.get('captured_at_utc') == received]
        if len(matches) != 1 or Path(matches[0]['markdown_path']).read_text() != data['markdown']:
            return None  # Newer/different body needs its own adjudication.
        if d.sha(matches[0]['markdown_path']) != matches[0]['markdown_sha256']:
            raise ValueError('REGISTERED_TAXONOMY_BODY_DRIFT')
        fragment = {f: list(spec.get('quotes', {}).get(f, {})) for f in FIELDS}
        found = support(fragment, spec, data['markdown'])
        for f in FIELDS: observed[f].extend(found[f])
        dependencies[str(capture_path)] = d.sha(capture_path)
        dependencies[matches[0]['markdown_path']] = matches[0]['markdown_sha256']
    if any(sorted(observed[f]) != sorted(row[f]) or len(observed[f]) != len(set(observed[f])) for f in FIELDS):
        raise ValueError('REGISTERED_TAXONOMY_ASSERTION_SET')
    for p, digest in dependencies.items():
        if d.sha(p) != digest:
            raise ValueError('REGISTERED_TAXONOMY_INPUT_DRIFT')
    return row, name, authority, dependencies


def make_packet(d, er, release, pid, out, captures, day, decision=None):
    """Transfer already-written unchanged taxonomy facts, never broad research."""
    for fact in (decision or {}).get('facts', []):
        if fact['path'].startswith('profile.taxonomy.') and (fact.get('status') != 'SUPPORTED'
                or fact.get('current_value') != fact.get('stored_value')):
            return None
    base = d.read(release / 'data/program-registry.json')
    profile = base['provider_dynamic_profiles']['profiles'].get(pid)
    if profile is None or 'fella' in str(profile.get('provider', '')).lower():
        return None
    proof = verified_row(d, er, pid, base['therapeutic_taxonomy']['programs'][pid], profile, captures, day)
    if proof is None:
        return None
    row, name, authority, dependencies = proof
    projected = project_verified_row(base, pid, row, name, authority)
    for alias_id, original in er.declared_aliases(base, pid).items():
        alias = er.project_alias_taxonomy(original, row)
        if any(alias[f] != original[f] for f in FIELDS):
            return None  # An alias claim change needs its own reviewed correction.
        projected['therapeutic_taxonomy']['programs'][alias_id] = alias
    matrix = d.read(release / 'data/provider-pricing-matrix.json')
    d.put(out / 'CAPTURES.json', captures)
    d.put(out / 'offline/data/program-registry.json', projected)
    d.put(out / 'offline/data/provider-pricing-matrix.json', matrix)
    run = subprocess.run(['node', str(d.ENGINE.with_name('evidence_refresh_verify.mjs')),
                          str(release), str(out), pid], capture_output=True, text=True, timeout=90)
    validation = d.read(out / 'VALIDATION.json')
    if (run.returncode or set(validation.get('target_validators', {})) != {'taxonomy', 'dynamic'}
            or not all(v.get('status') == 'PASS' for v in validation['target_validators'].values())):
        raise ValueError('REGISTERED_TAXONOMY_TARGET_VALIDATION:' + run.stderr[-1000:])
    after = d.read(out / 'offline/data/program-registry.json')
    if (after['provider_dynamic_profiles'] != base['provider_dynamic_profiles']
            or after['programs'] != base['programs']
            or d.read(out / 'offline/data/provider-pricing-matrix.json') != matrix):
        raise ValueError('REGISTERED_TAXONOMY_NON_TAXONOMY_CHANGE')
    for p, digest in dependencies.items():
        if d.sha(p) != digest:
            raise ValueError('REGISTERED_TAXONOMY_INPUT_DRIFT')
    packet = {'schema': 'k3-registered-taxonomy-packet-v1', 'program_id': pid,
              'status': 'PACKET_READY', 'adjudication_scope': 'taxonomy_only',
              'provenance': 'Existing registered writer, current policy replay and native validation; reported information',
              'registered_inputs': dependencies, 'captures': captures, 'facts': [],
              'research_clocks_advanced': False, 'price_clocks_advanced': False,
              'non_taxonomy_changes_deferred': [f['path'] for f in (decision or {}).get('facts', [])
                                               if not f['path'].startswith('profile.taxonomy.') and f.get('status') == 'CHANGED']}
    d.put(out / 'PACKET.json', packet)
    (out / 'DIFF.md').write_text('# Taxonomy-only registered intake\n\nPACKET_READY: every retained taxonomy assertion rechecked against the written source receipt.\nNo calculator, matrix, identity, commercial permission or deep-research clock changed.\n')
    return {'program_id': pid, 'status': 'PACKET_READY', 'adjudication_scope': 'taxonomy_only',
            'capture_outcome': 'REGISTERED_TAXONOMY_VERIFIED', 'packet_path': str(out / 'PACKET.json'),
            'capture_count': len(captures), 'registered_inputs': dependencies}
