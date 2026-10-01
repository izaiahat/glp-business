"""Candidate-boundary negatives; synthetic output is not whole-build proof."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from test_glp331_program_withdrawal import withdrawal as withdrawal, lane as lane, canonical_inbox as canonical_inbox, promote, run_fanout


def test_identity_crosswalk_missing_or_nonselected_refuses(withdrawal):
    f = withdrawal
    result = f.lane.run(promote(f))['results'][0]
    before = f.registry.read_bytes()
    for mappings in ([], [{'network': 'Awin', 'program_id': '3277', 'registry_program_id': f.provider['program_id']} ]):
        f.identities.write_text(json.dumps({'mappings': mappings}))
        with pytest.raises(ValueError):
            run_fanout(f, result)
        assert f.registry.read_bytes() == before


@pytest.mark.parametrize('status', ['hold_partner_email', 'hold_commercial_economics'])
def test_existing_provider_holds_survive(withdrawal, status):
    f = withdrawal
    f.lane.service.rows[758][10] = status
    result = f.lane.run(promote(f))['results'][0]
    assert result['status'] == 'refused'
    assert 'hold_preserved' in result['reason']
    assert not f.lane.service.writes


@pytest.mark.parametrize('surface', ['affiliate', 'review', 'quiz', 'redirect', 'lazy_json'])
def test_candidate_readback_rejects_stale_surfaces(tmp_path, surface):
    import affiliate_withdrawal_candidate as candidate
    public = tmp_path / 'public'
    quiz = public / 'quiz/provider-fit/index.html'
    quiz.parent.mkdir(parents=True)
    quiz.write_text('<script type="application/json" id="candidate-data">[]</script>')
    program = {'program_id': 'example', 'review_slug': 'example', 'review_path': '/reviews/example/',
               'go': {'route': '/go/example'}}
    assert candidate.assert_link_removal(tmp_path, program)['quiz_ineligible']
    if surface == 'review':
        review = public / 'reviews/example/index.html'
        review.parent.mkdir(parents=True)
        review.write_text('Old provider page')
    elif surface == 'affiliate':
        (public / 'comparison.html').write_text('<a href="/go/example?surface=compare">Old link</a>')
    elif surface == 'lazy_json':
        (public / 'lazy.json').write_text(json.dumps({'nested': [{'html': '<a href="/go/example">Old link</a>'}]}))
    elif surface == 'redirect':
        (public / '_redirects').write_text('/go/example https://example.invalid 302')
    else:
        quiz.write_text('<script id="candidate-data">[{"program_id":"example"}]</script>')
    with pytest.raises(ValueError):
        candidate.assert_link_removal(tmp_path, program)


def test_native_build_failure_remains_blocked_and_never_dispatches(withdrawal):
    import affiliate_withdrawal_candidate as candidate
    f = withdrawal
    source = f.root / 'detached-candidate'
    (source / 'data').mkdir(parents=True)
    (source / 'scripts').mkdir()
    (source / 'scripts/build-staging.mjs').write_text('// synthetic no-effect transport placeholder')
    target = source / 'data/program-registry.json'
    target.write_bytes(f.registry.read_bytes())
    f.registry = target
    request = run_fanout(f, f.lane.run(promote(f))['results'][0])
    calls = []
    def failed(argv, **kwargs):
        calls.append(argv)
        assert kwargs['cwd'] == source
        return SimpleNamespace(returncode=9, stdout=b'', stderr=b'synthetic native failure')
    outcome = candidate.build(Path(request['candidate_request']), source, runner=failed)
    assert outcome['status'] == 'BLOCKED_NATIVE_BUILD'
    assert outcome['release_authority'] is outcome['site_dispatched'] is False
    assert calls == [['node', str(source / 'scripts/build-staging.mjs')]]
