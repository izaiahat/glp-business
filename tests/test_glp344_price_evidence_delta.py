"""GLP-344: dated first-party price evidence -> guarded registry delta (F04 monthly branch)."""
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/glp344-price-evidence'
spec = importlib.util.spec_from_file_location('price_evidence_delta', ROOT / 'tools/glp/price_evidence_delta.py')
DELTA = importlib.util.module_from_spec(spec)
spec.loader.exec_module(DELTA)

PID = 'example-telehealth'
MONTH, AS_OF = '2026-10', '2026-10-03T00:00:00+00:00'


@pytest.fixture
def work(tmp_path):
    shutil.copytree(FIXTURES, tmp_path / 'f')
    return tmp_path / 'f'


def claims(work, name='claims.json'):
    return json.loads((work / name).read_text())


def run(work, doc=None, pid=PID, month=MONTH, as_of=AS_OF, **kw):
    return DELTA.monthly_branch(pid, month, as_of, doc if doc is not None else claims(work),
                                work / 'provider-pricing-matrix.json', work, **kw)


def reasons(branch):
    acceptance = branch['acceptance']
    return acceptance['branch_errors'] + [r for c in acceptance['refused_claims'] for r in c['reasons']]


def assert_refused(branch):
    assert branch['acceptance']['disposition'] == 'REFUSED'
    assert branch['acceptance']['accepted_claims'] == []
    assert branch['registry_diff']['targets'][0]['operations'] == []
    assert branch['f06_publication_intent']['intent'] == 'NONE'
    assert branch['f06_publication_intent']['price_index_published'] is False


def test_accepted_branch_yields_exact_provenanced_diff_and_f06_intent(work):
    before = (work / 'provider-pricing-matrix.json').read_bytes()
    branch = run(work)
    assert branch['acceptance']['disposition'] == 'ACCEPTED'
    assert branch['acceptance']['accepted_claims'] == [0, 1, 2, 3]
    target = branch['registry_diff']['targets'][0]
    assert branch['registry_diff']['applied_to_candidate'] is False
    assert target['source_sha256'] == DELTA.sha(work / 'provider-pricing-matrix.json')
    assert target['operations'][0]['op'] == 'test'  # exact precondition on stored bytes
    adopted = DELTA.apply_patch(json.loads(before), target['operations'])
    assert DELTA.stable(adopted) == target['result_sha256']
    provider = adopted['providers'][PID]
    sema = provider['medications'][0]['facts']
    assert sema['amount_usd']['value'] == 249 and sema['period']['value'] == 'month'
    assert provider['cancellation_terms']['facts']['notice_days']['value'] == 7
    for fact in sema.values():
        assert fact['artifact_needed'] is None
        for evidence in fact['evidence']:
            assert set(evidence) == {'source_url', 'capture_sha256', 'capture_path', 'captured_at_utc', 'quote'}
            assert len(evidence['capture_sha256']) == 64
            assert evidence['capture_path'] == 'data/pricing-evidence/' + evidence['capture_sha256'] + '.md'
            assert evidence['source_url'].startswith('https://example-telehealth.com/')
            assert evidence['captured_at_utc'].startswith('2026-10-01T')
    for fact in provider['cancellation_terms']['facts'].values():
        for evidence in fact['evidence']:
            assert len(evidence['content_sha256']) == 64
            assert 'capture_key' in evidence
    # Unknown values are not invented by unrelated evidence.
    assert provider['medications'][1]['facts']['amount_usd']['value'] is None
    assert adopted['providers']['held-clinic'] == json.loads(before)['providers']['held-clinic']
    intent = branch['f06_publication_intent']
    assert intent['intent'] == 'REQUEST_F06_ACCEPTANCE'
    assert intent['price_index_published'] is False and intent['publication_claimed'] is False
    assert intent['registry_diff_sha256'] == DELTA.stable(branch['registry_diff'])
    assert (work / 'provider-pricing-matrix.json').read_bytes() == before
    assert run(work) == branch


def test_conflicting_claims_refuse_the_whole_branch(work):
    branch = run(work, claims(work, 'claims-conflict.json'))
    assert_refused(branch)
    assert any(r.startswith('CONFLICT') for r in reasons(branch))


@pytest.mark.parametrize('month,as_of', [('2027-01', '2027-01-15T00:00:00+00:00'),
                                         ('2026-09', '2026-09-30T00:00:00+00:00')])
def test_stale_or_future_captures_refuse(work, month, as_of):
    branch = run(work, month=month, as_of=as_of)
    assert_refused(branch)
    assert all(r.startswith('STALE') for r in reasons(branch))


def test_claim_not_newer_than_stored_evidence_refuses(work):
    matrix = json.loads((work / 'provider-pricing-matrix.json').read_text())
    matrix['providers'][PID]['medications'][0]['facts']['amount_usd']['evidence'][0]['captured_at_utc'] = '2026-10-02T00:00:00Z'
    (work / 'provider-pricing-matrix.json').write_text(json.dumps(matrix))
    branch = run(work)
    assert_refused(branch)
    assert 'STALE: not newer than stored evidence' in reasons(branch)


def test_as_of_outside_month_is_rejected(work):
    with pytest.raises(ValueError, match='outside branch month'):
        run(work, month='2026-11')


@pytest.mark.parametrize('mutation,expected', [
    (lambda doc, work: (work / 'captures/pricing.md').write_text('tampered'), 'capture hash mismatch: pricing'),
    (lambda doc, work: doc['claims'][0].pop('content_sha256'), 'missing content_sha256'),
    (lambda doc, work: doc['claims'][2].pop('captured_at_utc'), 'missing captured_at_utc'),
    (lambda doc, work: doc['claims'][0].update(source_url='https://example-telehealth.com/other'),
     'source_url does not match capture manifest'),
    (lambda doc, work: doc['claims'][0].update(quote='$199/month'), 'exact quote absent from cited capture'),
    (lambda doc, work: doc['claims'][0].update(value=199), 'claimed value absent from quote'),
    (lambda doc, work: doc['claims'][1].update(value=None), 'unknown value is not a delta'),
    (lambda doc, work: doc['claims'][1].update(value='UNKNOWN'), 'unknown value is not a delta'),
    (lambda doc, work: doc['captures']['pricing'].update(forced_fresh=False), 'capture not a fresh first-party body: pricing'),
    (lambda doc, work: doc.update(first_party_hosts=['other.example']), 'source is not an allowed first-party URL'),
    (lambda doc, work: doc['claims'][0].update(row_id='missing-row'), 'price row not uniquely present: missing-row'),
    (lambda doc, work: doc['claims'][2].update(field='surprise'), 'unsupported cancellation term: surprise'),
])
def test_unprovenanced_or_unverifiable_claims_refuse(work, mutation, expected):
    doc = claims(work)
    mutation(doc, work)
    branch = run(work, doc)
    assert_refused(branch)
    assert expected in reasons(branch)


def test_observations_never_claim_a_published_price_index(work):
    doc = claims(work)
    doc['price_index_published'] = True
    branch = run(work, doc)
    assert_refused(branch)
    assert any(r.startswith('PUBLICATION_CLAIM_WITHOUT_F06') for r in reasons(branch))


@pytest.mark.parametrize('pid,held', [('held-clinic', ()), (PID, (PID,))])
def test_held_providers_stay_held(work, pid, held):
    doc = claims(work)
    doc['program_id'] = pid
    branch = run(work, doc, pid=pid, held_providers=held)
    assert_refused(branch)
    assert any(r.startswith('HELD') for r in reasons(branch))


def test_fella_is_permanently_excluded(work):
    matrix = json.loads((work / 'provider-pricing-matrix.json').read_text())
    matrix['providers']['fella-health'] = matrix['providers'][PID]
    (work / 'provider-pricing-matrix.json').write_text(json.dumps(matrix))
    doc = claims(work)
    doc['program_id'] = 'fella-health'
    assert_refused(run(work, doc, pid='fella-health'))


def test_adoption_rechecks_freshness_drift_and_disposition(work):
    doc = claims(work)
    branch = run(work, doc)
    matrix_path = work / 'provider-pricing-matrix.json'
    adopted = DELTA.adopt(branch, doc, matrix_path, work, '2026-10-20T00:00:00+00:00')
    assert adopted['providers'][PID]['medications'][0]['facts']['amount_usd']['value'] == 249
    with pytest.raises(ValueError, match='outside branch month'):
        DELTA.adopt(branch, doc, matrix_path, work, '2027-01-15T00:00:00+00:00')
    late = dict(branch, month='2027-01')
    with pytest.raises(ValueError, match='adoption refused: .*STALE'):
        DELTA.adopt(late, doc, matrix_path, work, '2027-01-15T00:00:00+00:00')
    refused = run(work, claims(work, 'claims-conflict.json'))
    with pytest.raises(ValueError, match='only an ACCEPTED branch'):
        DELTA.adopt(refused, doc, matrix_path, work, AS_OF)
    matrix_path.write_text(matrix_path.read_text() + ' ')
    with pytest.raises(ValueError, match='stale delta'):
        DELTA.adopt(branch, doc, matrix_path, work, AS_OF)
