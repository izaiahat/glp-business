"""Offline regression fixtures; synthetic captures are not provider evidence."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'ops/glp/evidence-refresh-k3'


def load(name):
    path = PACKAGE / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def put(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))
    return path


def test_saved_taxonomy_capture_becomes_hash_bound_packet_input(tmp_path):
    d = load('daily_evidence_refresh')
    source = tmp_path / 'taxonomy/run-20261002T120000Z'
    url = 'https://provider.example/medications'
    raw = put(source / 'provider-primary.json', {
        'request': {'url': url, 'maxAge': 0},
        'started_at': '2026-10-02T12:00:00+00:00',
        'received_at': '2026-10-02T12:00:01+00:00',
        'response': {'success': True, 'data': {'markdown': 'Fixture quote',
            'metadata': {'statusCode': 200, 'sourceURL': url}}}})
    put(source / 'RESULT.json', {'requested': ['provider'], 'written': [], 'status': 'BLOCKED'})
    er = d.load(ROOT / 'tools/glp/evidence_refresh.py', 'test_engine')
    profile = {'source_urls': [url]}
    caps = d.retained_captures(er, 'provider', profile, {}, '2026-10-02', tmp_path / 'out', source.parent)
    assert len(caps) == 1
    cap = next(iter(caps.values()))
    assert cap['captured_at_utc'] == '2026-10-02T12:00:01+00:00'
    assert cap['raw_capture_sha256'] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert er.load_captures(caps)[next(iter(caps))]['text'] == 'Fixture quote'
    assert d.retained_captures(er, 'provider', profile, {}, '2026-10-03', tmp_path / 'later', source.parent) == {}
    assert d.retained_captures(er, 'fella-health', profile, {}, '2026-10-02', tmp_path / 'excluded', source.parent) == {}


def test_stage_uses_retained_capture_and_resolves_declared_alias(tmp_path, monkeypatch):
    d = load('daily_evidence_refresh')
    er = d.load(ROOT / 'tools/glp/evidence_refresh.py', 'stage_engine')
    reg = {'programs': [{'program_id': 'member', 'review_slug': 'shed'}],
           'provider_dynamic_profiles': {'profiles': {'shedrx': {
               'aliases': [{'render_id': 'shed', 'program_id': 'member'}],
               'registry_program_ids': ['member'], 'source_urls': ['https://provider.example/']}}},
           'therapeutic_taxonomy': {'programs': {'shed': {'registry_program_id': 'member'}}}}
    put(tmp_path / 'release/data/program-registry.json', reg)
    put(tmp_path / 'release/data/provider-pricing-matrix.json', {'providers': {}})
    out = tmp_path / 'out'
    alias = d.stage_program(er, tmp_path / 'release', 'shed', out / 'shed', None, '2026-10-02')
    assert alias['status'] == 'DEPENDENCY_NEEDS_ADJUDICATION'
    assert alias['canonical_program_id'] == 'shedrx'
    assert alias['capture_outcome'] != 'UNRESOLVED_ALIAS_OR_PROFILE'
    monkeypatch.setattr(d, 'retained_captures', lambda *a, **kw: {'fixture': {'captured_at_utc': '2026-10-02T12:00:01Z'}})
    def engine_run(*a, **kw):
        put(out / 'shedrx/PACKET.json', {'status': 'NEEDS_ADJUDICATION', 'mechanical_facts': []})
        return type('Run', (), {'returncode': 0})()
    monkeypatch.setattr(d.subprocess, 'run', engine_run)
    row = d.stage_program(er, tmp_path / 'release', 'shedrx', out / 'shedrx', None, '2026-10-02')
    assert row['capture_count'] == 1
    assert row['capture_outcome'] == 'IMPORTED_RETAINED_TAXONOMY_CAPTURE'


def test_disjoint_composition_does_not_serially_apply_shared_counts(tmp_path):
    c = load('compose_current_base')
    d = load('daily_evidence_refresh')
    er = d.load(ROOT / 'tools/glp/evidence_refresh.py', 'compose_engine')
    base = {'therapeutic_taxonomy': {'programs': {}, 'authorities': {}, 'expected_counts': {'x': 2}},
            'provider_dynamic_profiles': {'profiles': {}, 'expected_counts': {'x': 2}}}
    for pid in ['alpha', 'beta']:
        base['therapeutic_taxonomy']['programs'][pid] = {'verified_as_of': '2026-09-01', 'commercial_link_state': 'held'}
        base['provider_dynamic_profiles']['profiles'][pid] = {'aliases': [], 'taxonomy': {'molecule_tags': ['x']}}
    matrix = {'providers': {'alpha': {'medications': []}, 'beta': {'medications': []}}, 'coverage': {}}
    reg_path = put(tmp_path / 'release/data/program-registry.json', base)
    mat_path = put(tmp_path / 'release/data/provider-pricing-matrix.json', matrix)
    rows = []
    for pid in ['alpha', 'beta']:
        changed = json.loads(json.dumps(base))
        changed['therapeutic_taxonomy']['programs'][pid]['verified_as_of'] = '2026-10-02'
        changed['provider_dynamic_profiles']['profiles'][pid]['taxonomy']['molecule_tags'] = []
        changed['provider_dynamic_profiles']['expected_counts'] = {'x': 1}
        folder = tmp_path / pid
        put(folder / 'PACKET.json', {'program_id': pid, 'status': 'PACKET_READY'})
        put(folder / 'DATA-PATCH.json', {'targets': [
            {'path': str(reg_path), 'source_sha256': er.sha(reg_path), 'operations': er.diff(base, changed)},
            {'path': str(mat_path), 'source_sha256': er.sha(mat_path), 'operations': []}]})
        rows.append({'program_id': pid, 'status': 'PACKET_READY', 'packet_path': str(folder / 'PACKET.json')})
    combined, _, consumed = c.merge_packets(er, tmp_path / 'release', rows)
    assert len(consumed) == 4
    assert all(v['verified_as_of'] == '2026-10-02' for v in combined['therapeutic_taxonomy']['programs'].values())
    assert all(v['taxonomy']['molecule_tags'] == [] for v in combined['provider_dynamic_profiles']['profiles'].values())
    assert combined['provider_dynamic_profiles']['expected_counts'] == {'x': 2}  # only native derivation may replace this
    assert er.read(reg_path) == base
    with pytest.raises(RuntimeError, match='DUPLICATE'):
        c.merge_packets(er, tmp_path / 'release', rows + rows[:1])
    patch = er.read(tmp_path / 'alpha/DATA-PATCH.json')
    patch['targets'][0]['operations'] += er.diff('held', 'active', '/therapeutic_taxonomy/programs/alpha/commercial_link_state')
    put(tmp_path / 'alpha/DATA-PATCH.json', patch)
    with pytest.raises(RuntimeError, match='AUTHORITY'):
        c.merge_packets(er, tmp_path / 'release', rows)


def test_successor_stage_preserves_immutable_original_and_reuses(tmp_path):
    d = load('daily_evidence_refresh')
    bound = {'fixture': 'same-base'}
    old = {'binding': bound, 'source_pins': {'runner': 'old'}, 'selector_only': False,
           'capture_root': None, 'artifacts': {}}
    p = put(tmp_path / '2026-10-02/RUN.json', old)
    before = p.read_bytes()
    pins = {'runner': 'new'}
    folder, parent = d.stage_directory(tmp_path, '2026-10-02', bound, pins, False, None, True)
    assert folder != p.parent and not folder.exists()
    assert parent == {'path': str(p), 'sha256': d.sha(p)}
    assert p.read_bytes() == before
    assert d.stage_directory(tmp_path, '2026-10-02', bound, pins, False, None, True) == (folder, parent)
    replacement = {'fixture': 'new-authenticated-base'}
    next_folder, next_parent = d.stage_directory(tmp_path, '2026-10-02', replacement, pins, False, None, True)
    assert next_folder != folder and next_parent == parent
    assert p.read_bytes() == before  # Fresh namespace, NEVER replay old packets.
    with pytest.raises(RuntimeError, match='BASE_CHANGED'):
        d.stage_directory(tmp_path, '2026-10-02', replacement, pins, False, None, False)
    with pytest.raises(RuntimeError, match='INPUT_MODE_CHANGED'):
        d.stage_directory(tmp_path, '2026-10-02', bound, pins, False, None, False)


def test_pallas_policy_matches_captured_wording_without_adding_tags():
    import ast
    source = ROOT / 'site-system/flagship-v2/candidates/glp34-provider-families-20260912/scripts/refresh-therapeutic-taxonomy.py'
    tree = ast.parse(source.read_text())
    policies = ast.literal_eval(next(n.value for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'POLICIES' for t in n.targets)))
    p = policies['pallas-health-review']
    # Verbatim excerpt, raw capture received 2026-10-02T12:01:28.411212Z.
    text = 'Compounded semaglutide injection is $139 for your first month, then $199 every 4 weeks, or $139/mo average forever on the annual plan; compounded tirzepatide injection is $179 for your first month, then $299 every 4 weeks, or $239/mo average forever on the annual plan.'
    assert set(p['quotes']['molecule_tags']) == {'semaglutide', 'tirzepatide'}
    assert all(q in text for q in p['quotes']['molecule_tags'].values())


def test_composition_requires_the_actual_pricing_receiver(tmp_path, monkeypatch):
    import types
    c = load('compose_current_base')
    d = load('daily_evidence_refresh')
    er = d.load(ROOT / 'tools/glp/evidence_refresh.py', 'pricing_engine')
    release = tmp_path / 'release'
    for name in ['program-registry.json', 'provider-pricing-matrix.json']:
        put(release / 'data' / name, {})
    monkeypatch.setattr(d, 'unchanged', lambda bound: None)
    monkeypatch.setattr(c, 'merge_packets', lambda *a: ({}, {}, {}))
    calls = []
    def native(argv, **kw):
        calls.append(argv)
        if argv[0] == 'node':
            return types.SimpleNamespace(returncode=0, stdout='{"registry":{},"matrix":{}}', stderr='')
        assert argv[0] == 'bwrap' and '--unshare-net' in argv
        assert argv[-1] == str(tmp_path / 'out/pricing-validation-root/scripts/validate-provider-pricing-matrix.mjs')
        # The accepted baseline stays visible at its real path; overlay only a
        # different candidate root, never the matrix the pointer authenticates.
        assert str(release / 'data/provider-pricing-matrix.json') not in argv
        assert str(release / 'data/program-registry.json') not in argv
        return types.SimpleNamespace(returncode=1, stdout='{"status":"FAIL","violations":["legacy_snapshot_drift"]}', stderr='')
    monkeypatch.setattr(c.subprocess, 'run', native)
    out = tmp_path / 'out'
    result = c.compose(d, er, {'pointer': {'release_root': str(release)}}, [{'program_id': 'fixture', 'status': 'PACKET_READY'}], out)
    assert result['status'] == 'BLOCKED_PRICING_VALIDATION'
    assert result['combined_patch'] is None
    assert not (out / 'COMBINED-PATCH.json').exists()
    assert len(calls) == 2
