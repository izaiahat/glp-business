"""Offline future-writer contract; never main, network, locks or live adoption."""
import copy
import json

import pytest
import os
from pathlib import Path
from test_glp80_eden_policy import (replay, collect, FIXTURES, PID, URLS,
                                  QUALIFICATION, FIELDS)


def test_future_writer_stages_exact_qualified_closure_without_clock_or_hold_change(replay):
    m, receipts, _, run, index = replay
    registry = json.loads((FIXTURES / 'native-input.json').read_text())
    original = copy.deepcopy(registry['therapeutic_taxonomy']['programs'][PID])
    staged = m.prepare_qualified_source_row(original, PID)
    assert registry['therapeutic_taxonomy']['programs'][PID] == original
    assert staged['source_urls'] == sorted(URLS)
    assert staged['claim_qualification'] == QUALIFICATION
    for key in original:
        if key != 'source_urls':
            assert staged[key] == original[key]
    manifest = collect(replay, row=staged)
    manifest['previous_row'] = original
    assert manifest['claim_qualification'] == QUALIFICATION
    target = copy.deepcopy(original)
    m.apply_qualified_source_row(target, PID, manifest)
    assert target['source_urls'] == sorted(URLS)
    assert target['claim_qualification'] == QUALIFICATION
    assert target['verified_as_of'] == original['verified_as_of']
    assert target['evidence_bindings'] == original['evidence_bindings']
    assert target['commercial_link_state'] == original['commercial_link_state']
    assert {f: target[f] for f in FIELDS} == {f: original[f] for f in FIELDS}
    assert m.prepare_qualified_source_row(target, PID) == target


def test_closure_drift_refuses_without_mutation(replay):
    m, _, _, _, _ = replay
    original = json.loads((FIXTURES / 'native-input.json').read_text())['therapeutic_taxonomy']['programs'][PID]
    for field, value in [('source_urls', URLS + ['https://example.org/unreviewed']),
                         ('claim_qualification', 'Clean Zepbound supply verified.'),
                         ('brand_name_tags', ['mounjaro'])]:
        wrong = copy.deepcopy(original)
        wrong[field] = value
        before = copy.deepcopy(wrong)
        with pytest.raises(ValueError, match='qualified_source'):
            m.prepare_qualified_source_row(wrong, PID)
        assert wrong == before
    assert m.prepare_qualified_source_row(original, 'unrelated') == original


def test_paired_native_writer_validation_and_public_fixture_match(replay):
    root = os.environ.get('GLP80_RENDERER_ROOT')
    if not root:
        pytest.skip('paired-source integration runs on host at both exact heads')
    m, _, _, _, _ = replay
    m.ROOT = Path(root)
    registry = json.loads((FIXTURES / 'native-input.json').read_text())
    original = copy.deepcopy(registry['therapeutic_taxonomy']['programs'][PID])
    staged = m.prepare_qualified_source_row(original, PID)
    manifest = collect((m, replay[1], staged, replay[3], replay[4]))
    m.apply_qualified_source_row(registry['therapeutic_taxonomy']['programs'][PID], PID, manifest)
    row = registry['therapeutic_taxonomy']['programs'][PID]
    row['verified_as_of'] = manifest['verified_as_of']  # simulated future adoption only
    authority = 'eden_fixture_manifest'
    authority_sha = __import__('hashlib').sha256(m.encoded(manifest)).hexdigest()
    registry['therapeutic_taxonomy']['authorities'][authority] = {'path':'reports/eden-fixture/manifest.json','sha256':authority_sha}
    row['evidence_bindings'] = [{'authority':authority,'authority_sha256':authority_sha,
        'source_url':s['source_url'],'verified_as_of':s['verified_as_of'],
        'evidence_sha256':s['capture_sha256']} for s in manifest['sources']]
    m.validate_qualified_renderer(registry, [PID])
    accepted = m.validate(registry, [PID])
    actual = accepted['therapeutic_taxonomy']['programs'][PID]
    assert actual['claim_qualification'] == QUALIFICATION
    assert actual['source_urls'] == sorted(URLS)
    assert actual['commercial_link_state'] == original['commercial_link_state']
    partner = json.loads((Path(root) / 'tests/fixtures/eden-qualified-closure.json').read_text())
    for key in ['source_urls', 'claim_qualification', *FIELDS]:
        assert actual[key] == partner['row'][key]
    assert [(s['source_url'],s['evidence_sha256']) for s in actual['evidence_bindings']] == [(s['source_url'],s['evidence_sha256']) for s in partner['row']['evidence_bindings']]
    stale = copy.deepcopy(accepted)
    stale['therapeutic_taxonomy']['programs'][PID]['verified_as_of'] = original['verified_as_of']
    with pytest.raises(ValueError, match='evidence_stale'):
        m.validate(stale, [PID])
    assert m.prepare_qualified_source_row(original, 'unrelated') == original


def test_unqualified_or_incomplete_manifest_cannot_reach_writer(replay):
    m, _, _, _, _ = replay
    original = json.loads((FIXTURES / 'native-input.json').read_text())['therapeutic_taxonomy']['programs'][PID]
    staged = m.prepare_qualified_source_row(original, PID)
    manifest = collect(replay, row=staged)
    for mutation in [lambda x: x.pop('claim_qualification'),
                     lambda x: x['sources'].pop(),
                     lambda x: x['sources'][0].update(source_url='https://example.org/')]:
        wrong = copy.deepcopy(manifest)
        mutation(wrong)
        target = copy.deepcopy(original)
        with pytest.raises(ValueError, match='qualified_source'):
            m.apply_qualified_source_row(target, PID, wrong)
        assert target == original
