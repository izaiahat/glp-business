"""Offline real-classifier / fake-existing-queue integration and crash regressions."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import affiliate_auto_apply as admission


class Queue:
    last_read_truncated = False

    def __init__(self):
        self.data = []
        self.inserts = 0
        self.lost_ack = False
        self.drop_write = False

    def eq(self, field, value):
        return field, value

    def rows(self, table, where, limit):
        assert table == admission.APPLICATION_QUEUE_TABLE
        return [r for r in self.data if r.get(where[0]) == where[1]]

    def insert(self, table, row):
        self.inserts += 1
        if not self.drop_write:
            self.data.append(copy.deepcopy(row))
        if self.lost_ack or self.drop_write:
            raise TimeoutError('fixture uncertain enqueue')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.syspath_prepend(str(root / 'scripts'))
    from scripts import affiliate_operator_rulings as rulings
    snapshot = rulings.load_snapshot(tmp_path / 'rulings')
    monkeypatch.setattr(rulings, 'load_snapshot', lambda: snapshot)
    queue = Queue()
    dedupe = SimpleNamespace(verdict=lambda *a, **k: None)
    path = tmp_path / 'impact-readback.json'
    candidate = {'program_id': '123', 'name': 'Fixture Clinic',
                 'description': 'US telehealth clinic prescribes semaglutide medication for weight loss.',
                 'region': 'US', 'network_status': 'active', 'relationship': 'notjoined',
                 'application_url': 'https://fixture.example/affiliate/apply',
                 'official_url': 'https://fixture.example/'}
    def inventory(rows=None):
        path.write_text(json.dumps({'network': 'impact', 'programs': rows or [candidate]}))
        return path
    def run(**kwargs):
        return admission.admit_discovery([path], queue_client=queue, dedupe_index=dedupe,
            journal_dir=tmp_path / 'journal', lock_path=tmp_path / 'lock', **kwargs)
    inventory()
    return SimpleNamespace(queue=queue, dedupe=dedupe, candidate=candidate,
                           inventory=inventory, path=path, run=run, root=tmp_path)


def test_real_classifier_admits_existing_queue_once_and_preserves_unknown(setup):
    first = setup.run(apply=True)
    assert first['outcomes'][0]['status'] == 'admitted'
    assert setup.run(apply=True)['outcomes'][0]['status'] == 'already_admitted'
    setup.candidate['name'] = 'Fixture Clinic New Label'
    setup.inventory()
    assert setup.run(apply=True)['outcomes'][0]['status'] == 'already_admitted'
    assert setup.queue.inserts == 1
    row = setup.queue.data[0]
    candidate = admission.validate_discovery_row(row, journal_dir=setup.root / 'journal')
    assert candidate['payout'] is None and candidate['currency'] is None and candidate['terms'] is None
    assert first['effects'] == {'applications_submitted': 0, 'sheet_writes': 0}


@pytest.mark.parametrize('change,reason', [
    ({'name': 'Fella Health'}, 'provider_excluded'),
    ({'name': 'Telos Health'}, 'provider_excluded'),
    ({'name': 'HealthRX'}, 'provider_excluded'),
    ({'held': True}, 'source_held'),
    ({'suppressed': True}, 'source_held'),
    ({'region': ''}, 'explicit_us'),
    ({'region': 'GB'}, 'explicit_us'),
    ({'description': 'Telehealth prescribes semaglutide medication. Not available in the United States.'}, 'contradictory_us'),
    ({'network_status': 'paused'}, 'candidate_gate'),
    ({'relationship': 'pending'}, 'relationship_not_fresh'),
    ({'program_id': ''}, 'missing_exact'),
])
def test_admission_respects_exclusions_and_unknowns(setup, change, reason):
    setup.inventory([{**setup.candidate, **change}])
    result = setup.run(apply=True)['outcomes'][0]
    assert result['status'] == 'refused'
    assert reason in result['reason']
    assert setup.queue.inserts == 0


def test_shared_suppression_index_is_terminal(setup):
    setup.dedupe.verdict = lambda *a, **k: SimpleNamespace(reason='relationship_suppressed')
    assert setup.run(apply=True)['outcomes'][0]['reason'].startswith('suppressed_or_engaged')
    assert setup.queue.inserts == 0


def test_dry_run_never_writes_queue_journal_or_lock(setup):
    assert setup.run()['outcomes'][0]['status'] == 'would_admit'
    assert setup.queue.inserts == 0
    assert not (setup.root / 'journal').exists()
    assert not (setup.root / 'lock').exists()


def test_lost_ack_reads_back_and_never_reinserts(setup):
    setup.queue.lost_ack = True
    assert setup.run(apply=True)['outcomes'][0]['status'] == 'admitted'
    assert setup.run(apply=True)['outcomes'][0]['status'] == 'already_admitted'
    assert setup.queue.inserts == 1


def test_uncertain_missing_write_requires_reconciliation_not_retry(setup):
    setup.queue.drop_write = True
    assert setup.run(apply=True)['status'] == 'INCOMPLETE'
    assert setup.run(apply=True)['outcomes'][0]['status'] == 'reconcile_required'
    assert setup.queue.inserts == 1


def test_existing_operator_row_prevents_duplicate(setup):
    setup.queue.data.append({'queue_id': 'operator-123', 'network': 'impact', 'program_id': '123'})
    assert setup.run(apply=True)['outcomes'][0]['status'] == 'already_queued'
    assert setup.queue.inserts == 0


def test_truncated_queue_fails_closed(setup):
    setup.queue.last_read_truncated = True
    with pytest.raises(RuntimeError, match='read_truncated'):
        setup.run(apply=True)
    assert setup.queue.inserts == 0


def test_actual_consumer_accepts_only_untampered_confirmed_admission(setup, monkeypatch):
    from scripts import glp_application_machine as machine
    setup.run(apply=True)
    original = admission.validate_discovery_row
    monkeypatch.setattr(admission, 'validate_discovery_row',
                        lambda row: original(row, journal_dir=setup.root / 'journal'))
    row = setup.queue.data[0]
    candidate = machine.queued_candidate(row)
    assert candidate['source'] == 'weekly_discovery_application_queue'
    assert candidate['network_status'] == 'active'
    # An authority string alone, or edited candidate, cannot bypass custody.
    tampered = {**row, 'program_name': 'Other Clinic'}
    with pytest.raises(ValueError, match='queue_drift'):
        machine.queued_candidate(tampered)
    payload = json.loads(row['payload'])
    assert 'submission_packet' not in payload  # executor, not discovery, owns real form mapping
    with pytest.raises(ValueError, match='queue_drift'):
        machine.queued_candidate({**row, 'payload': '{}'})
    with pytest.raises(ValueError, match='submission_packet_missing'):
        machine.queued_candidate({**row, 'queue_id': 'operator-123',
                                  'operator_authority': 'fixture_operator', 'payload': '{}'})


def test_corrupted_frozen_source_blocks_enqueue_recovery(setup):
    setup.queue.drop_write = True
    setup.run(apply=True)
    frozen = next((setup.root / 'journal/sources').glob('*.json'))
    frozen.write_bytes(b'{interrupted')
    with pytest.raises(ValueError, match='source_hash_mismatch'):
        setup.run(apply=True)
    assert setup.queue.inserts == 1


def test_consumer_rechecks_network_status_without_dropping_it(setup, monkeypatch):
    from scripts import glp_application_machine as machine
    from scripts import affiliate_niche_geo_filter as nfilter
    monkeypatch.setattr(machine, 'AUTHORIZED_LANES', {'glp1-weightloss'})
    rows, _ = admission.read_inventory(setup.path)
    result = machine.gate_and_dedupe(rows, {}, {}, nfilter, dedupe_index=setup.dedupe)
    assert len(result['accepted']) == 1
    rows[0]['network_status'] = 'paused'
    assert not machine.gate_and_dedupe(rows, {}, {}, nfilter, dedupe_index=setup.dedupe)['accepted']


def test_budget_deferred_tail_is_reported(setup):
    report = setup.run(apply=True, deadline_seconds=-1)
    assert report['status'] == 'INCOMPLETE'
    assert report['candidate_count'] == 1
    assert report['outcomes'][0]['status'] == 'deferred'
    assert setup.queue.inserts == 0


@pytest.mark.parametrize('slow_step', ['classification', 'queue_read', 'insert'])
def test_review_r3_no_new_queue_operation_after_admission_deadline(setup, monkeypatch, slow_step):
    import time
    operations = []
    deadline = time.monotonic() + .04
    original_rows, original_insert = setup.queue.rows, setup.queue.insert
    original_refusal = admission.candidate_gate
    def rows(*args, **kwargs):
        operations.append(('read', time.monotonic()))
        if slow_step == 'queue_read':
            time.sleep(.12)
        return original_rows(*args, **kwargs)
    def insert(*args, **kwargs):
        operations.append(('insert', time.monotonic()))
        if slow_step == 'insert':
            time.sleep(.12)  # Never interrupt an in-flight effect.
        return original_insert(*args, **kwargs)
    def refusal(*args, **kwargs):
        result = original_refusal(*args, **kwargs)
        if slow_step == 'classification':
            time.sleep(.12)
        return result
    monkeypatch.setattr(setup.queue, 'rows', rows)
    monkeypatch.setattr(setup.queue, 'insert', insert)
    monkeypatch.setattr(admission, 'candidate_gate', refusal)
    setup.inventory([setup.candidate, {**setup.candidate, 'program_id': '456'}])
    result = setup.run(apply=True, deadline_seconds=.04)
    assert all(start < deadline for _, start in operations), operations
    assert result['status'] == 'INCOMPLETE'
    assert setup.queue.inserts == (1 if slow_step == 'insert' else 0)
    assert list((setup.root / 'journal/deferred').glob('*.json'))
    # An expired dispatch readback must stay uncertain until a later run observes it.
    monkeypatch.setattr(setup.queue, 'rows', original_rows)
    monkeypatch.setattr(setup.queue, 'insert', original_insert)
    monkeypatch.setattr(admission, 'candidate_gate', original_refusal)
    resumed = setup.run(apply=True)
    assert resumed['status'] == 'COMPLETE'
    assert setup.queue.inserts == 2


def test_deadline_during_dispatch_intent_does_not_make_unstarted_effect_uncertain(setup, monkeypatch):
    import time
    original = admission.write_json
    def slow_intent(path, record):
        original(path, record)
        if record.get('state') == 'dispatching':
            time.sleep(.12)
    monkeypatch.setattr(admission, 'write_json', slow_intent)
    result = setup.run(apply=True, deadline_seconds=.05)
    assert result['outcomes'][0]['status'] == 'deferred'
    assert setup.queue.inserts == 0
    key = admission.stable_key('impact', '123')
    assert json.loads((setup.root / 'journal' / (key + '.json')).read_text())['state'] == 'prepared'
    monkeypatch.setattr(admission, 'write_json', original)
    assert setup.run(apply=True)['status'] == 'COMPLETE'
    assert setup.queue.inserts == 1


def test_executor_gate_still_requires_complete_terms(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'scripts'))
    from scripts import affiliate_application_executor_gate as gate
    authority = tmp_path / 'authority.json'
    authority.write_text(json.dumps({'schema': gate.EXPECTED_AUTHORITY_SCHEMA,
        'status': 'ACTIVE', 'authority': 'advertiser_program_application_submission_only',
        'lanes': ['glp1-weightloss']}))
    target = {'network': 'impact', 'program_id': '123', 'decision': 'AUTO_APPLY',
              'fit_lane': 'glp1-weightloss', 'relationship': 'available',
              'application_url': 'https://fixture.example/apply'}
    preflight = tmp_path / 'preflight.json'
    preflight.write_text(json.dumps({'schema': gate.EXPECTED_PREFLIGHT_SCHEMA,
        'authority_manifest_sha256': gate.digest(authority), 'targets': [target]}))
    receipt = gate.validate(authority, preflight, [])
    assert receipt['status'] == 'BLOCKED'
    assert 'terms_not_complete_compatible:impact:123' in receipt['failures']
