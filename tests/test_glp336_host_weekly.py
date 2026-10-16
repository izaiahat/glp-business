"""Offline regressions for host ownership, not provider task simulations."""
import datetime as dt
import json
from pathlib import Path
from unittest import mock

import pytest
from ops.glp import weekly_analytics_owner as owner
from ops.glp.weekly_strategy_sync import atomic

NOW = dt.datetime(2026, 10, 14, 18, tzinfo=dt.timezone.utc)


def producer(output):
    output.mkdir()
    atomic(output/'REPORT.json', {'gsc_rows': {'gsc-28': 12},
        'recommendations': 2, 'module_statuses': {'page2': 'PASS_ANALYSIS_ONLY'},
        'weekly_spend': {'within_cap': True, 'cap_usd': '25', 'committed_usd': '0'},
        'input_observations': {'ga4': {'ok': True, 'complete': True}}})
    atomic(output/'TOPIC-RECOMMENDATIONS.json', {'week_start': '2026-10-12',
        'recommendations': [{'route': '/reviews/example/', 'query': 'example reviews'}]})
    atomic(output/'STRATEGY-UPDATES.json', {'week_start': '2026-10-12', 'topic_priorities': [{'query': 'example reviews'}]})
    return {'report': str(output/'REPORT.json'), 'topic_recommendations': str(output/'TOPIC-RECOMMENDATIONS.json'),
        'strategy_update': {'status': 'HOST_DURABLE_STRATEGY_INPUT', 'updates': owner.binding(output/'STRATEGY-UPDATES.json')},
        'status': 'COMPLETE_WITH_EXPLICIT_UNKNOWNS'}


def test_weekly_completion_and_consumer_ack_are_once_only(tmp_path):
    calls = []
    def run(path):
        calls.append(path)
        return producer(path)
    first = owner.run_owned(tmp_path, run, now=NOW)
    before = Path(first['host_completion']['path']).read_bytes()
    second = owner.run_owned(tmp_path, lambda p: pytest.fail('must not recollect'), now=NOW)
    assert len(calls) == 1 and first == second
    assert Path(second['host_completion']['path']).read_bytes() == before
    ack = owner.consume(second, '020bcdd7f0e1')
    assert owner.consume(second, '020bcdd7f0e1') == ack
    assert ack['task_id'] == 'glp336-weekly-2026-10-12'
    assert ack['analysis_rows'] == 1
    assert Path(ack['path']).exists()


def test_empty_analysis_never_completes(tmp_path):
    def empty(path):
        result = producer(path)
        atomic(path/'TOPIC-RECOMMENDATIONS.json', {'week_start': '2026-10-12', 'recommendations': []})
        atomic(path/'REPORT.json', {'gsc_rows': {}, 'module_statuses': {}})
        return result
    with pytest.raises(ValueError, match='nonempty_weekly_analysis_required'):
        owner.run_owned(tmp_path, empty, now=NOW)
    assert not (tmp_path/'2026-10-12'/'COMPLETED.json').exists()


def test_crash_after_analysis_resumes_without_collection(tmp_path):
    real_atomic = owner.atomic
    def crash(path, doc):
        if Path(path).name == 'COMPLETED.json':
            raise RuntimeError('crash before completion')
        return real_atomic(path, doc)
    with mock.patch.object(owner, 'atomic', side_effect=crash), pytest.raises(RuntimeError):
        owner.run_owned(tmp_path, producer, now=NOW)
    result = owner.run_owned(tmp_path, lambda p: pytest.fail('must reuse analysis'), now=NOW)
    assert result['host_completion']['task_id'] == 'glp336-weekly-2026-10-12'


def test_tampered_analysis_refuses_instead_of_recollecting(tmp_path):
    result = owner.run_owned(tmp_path, producer, now=NOW)
    Path(result['topic_recommendations']).write_text('{}')
    with pytest.raises(ValueError, match='host_binding_changed'):
        owner.run_owned(tmp_path, lambda p: pytest.fail('must not recollect'), now=NOW)
    with pytest.raises(ValueError, match='host_binding_changed'):
        owner.consume(result, '020bcdd7f0e1')


def test_wrong_week_and_failed_budget_cannot_complete(tmp_path):
    def wrong(path):
        result = producer(path)
        atomic(path/'TOPIC-RECOMMENDATIONS.json', {'week_start': '2026-10-05', 'recommendations': [{}]})
        return result
    with pytest.raises(ValueError, match='host_analysis_week_mismatch'):
        owner.run_owned(tmp_path/'wrong', wrong, now=NOW)
    def over(path):
        result = producer(path)
        report = json.loads(Path(result['report']).read_text())
        report['weekly_spend']['within_cap'] = False
        atomic(Path(result['report']), report)
        return result
    with pytest.raises(ValueError, match='host_weekly_budget_unproved'):
        owner.run_owned(tmp_path/'over', over, now=NOW)


def test_free_analysis_completes_with_explicit_metered_hold(tmp_path):
    def held(path):
        result = producer(path)
        report = json.loads(Path(result['report']).read_text())
        report['module_statuses']['keyword_gap'] = 'UNKNOWN_INPUT'
        report['weekly_spend'] = {'within_cap': False, 'cap_usd': '25',
            'committed_usd': '1.308552', 'rail_journal_unknown': True,
            'open_reservations': [], 'unknown_cost_reservations': [],
            'refusals': [{'reason': 'unknown_prior_spend_this_week'}]}
        atomic(Path(result['report']), report)
        return result
    result = owner.run_owned(tmp_path, held, now=NOW)
    done = owner.verify_completion(result['host_completion'])
    assert done['analysis_rows'] == 1
    assert done['weekly_spend']['within_cap'] is False
    assert done['metered_collection_status'] == 'HELD_UNKNOWN_PRIOR_SPEND'
    assert owner.consume(result, '020bcdd7f0e1')['ack_count'] == 1


def test_report_changed_between_validation_and_binding_must_refuse(tmp_path, monkeypatch):
    # Exact RV512-514 reproduction: mutate real bytes at the binding boundary.
    real_binding = owner.binding
    mutated = []
    def at_binding(path):
        path = Path(path)
        if path.name == 'REPORT.json' and not mutated:
            doc = json.loads(path.read_bytes())
            doc['gsc_rows'] = {}
            doc['module_statuses']['page2'] = 'UNKNOWN_INPUT'
            doc['weekly_spend'] = {'within_cap': False, 'cap_usd': '25', 'committed_usd': '30'}
            atomic(path, doc)
            mutated.append(True)
        return real_binding(path)
    monkeypatch.setattr(owner, 'binding', at_binding)
    with pytest.raises(ValueError):
        owner.run_owned(tmp_path, producer, now=NOW)
    assert not (tmp_path/'2026-10-12'/'COMPLETED.json').exists()
    assert not (tmp_path/'2026-10-12'/'CONSUMED-GLP44.json').exists()


def test_completion_and_ack_require_validated_report_hash(tmp_path):
    result = owner.run_owned(tmp_path, producer, now=NOW)
    spec = result['host_completion']
    done = owner.read_bound(spec)
    done.pop('validated_report_sha256', None)
    atomic(Path(spec['path']), done)
    result['host_completion'] = owner.binding(spec['path'])
    with pytest.raises(ValueError, match='host_completion_validated_report'):
        owner.verify_completion(result['host_completion'])
    with pytest.raises(ValueError, match='host_completion_validated_report'):
        owner.consume(result, owner.CONSUMER)
    assert not (tmp_path/'2026-10-12'/'CONSUMED-GLP44.json').exists()


def test_rebound_invalid_report_cannot_verify_or_ack(tmp_path):
    result = owner.run_owned(tmp_path, producer, now=NOW)
    spec = result['host_completion']
    done = owner.read_bound(spec)
    path = Path(result['report'])
    doc = json.loads(path.read_bytes())
    doc['gsc_rows'] = {}
    atomic(path, doc)
    done['analysis_bindings']['report'] = owner.binding(path)
    done['validated_report_sha256'] = done['analysis_bindings']['report']['sha256']
    atomic(Path(spec['path']), done)
    result['host_completion'] = owner.binding(spec['path'])
    with pytest.raises(ValueError, match='nonempty_weekly_analysis_required'):
        owner.verify_completion(result['host_completion'])
    with pytest.raises(ValueError, match='nonempty_weekly_analysis_required'):
        owner.consume(result, owner.CONSUMER)
    assert not (tmp_path/'2026-10-12'/'CONSUMED-GLP44.json').exists()


def test_report_drift_at_consumer_lock_refuses_ack(tmp_path, monkeypatch):
    result = owner.run_owned(tmp_path, producer, now=NOW)
    real_flock = owner.fcntl.flock
    def at_lock(fd, flags):
        real_flock(fd, flags)
        Path(result['report']).write_text('{}')
    monkeypatch.setattr(owner.fcntl, 'flock', at_lock)
    with pytest.raises(ValueError, match='host_binding_changed'):
        owner.consume(result, owner.CONSUMER)
    assert not (tmp_path/'2026-10-12'/'CONSUMED-GLP44.json').exists()


@pytest.mark.parametrize('change', ['missing_cost', 'true_with_unknown',
    'true_with_open', 'true_with_unknown_reservation'])
def test_malformed_budget_must_not_be_promoted(tmp_path, change):
    # RV512-514 final reproduction; open estimates alone are a positive control.
    def malformed(path):
        result = producer(path)
        doc = json.loads(Path(result['report']).read_bytes())
        spend = {'within_cap': False, 'cap_usd': '25', 'committed_usd': '1.308552',
            'rail_journal_unknown': True, 'open_reservations': [],
            'unknown_cost_reservations': [],
            'refusals': [{'reason': 'unknown_prior_spend_this_week'}]}
        if change == 'missing_cost':
            del spend['committed_usd']
        else:
            spend['within_cap'] = True
            if change == 'true_with_open':
                spend.update(rail_journal_unknown=False, open_reservations=['unsettled'])
            if change == 'true_with_unknown_reservation':
                spend.update(rail_journal_unknown=False, unknown_cost_reservations=['unknown'])
        doc['weekly_spend'] = spend
        doc['module_statuses']['keyword_gap'] = 'UNKNOWN_INPUT'
        atomic(Path(result['report']), doc)
        return result
    if change == 'true_with_open':
        result = owner.run_owned(tmp_path, malformed, now=NOW)
        assert owner.verify_completion(result['host_completion'])['metered_collection_status'] == 'WITHIN_CAP'
    else:
        with pytest.raises(ValueError, match='host_weekly_budget_unproved'):
            owner.run_owned(tmp_path, malformed, now=NOW)
        assert not (tmp_path/'2026-10-12'/'COMPLETED.json').exists()


@pytest.mark.parametrize('cost', [None, '', 'UNKNOWN', True, 'NaN', 'Infinity', '-1', '25.01'])
def test_unknown_or_invalid_committed_cost_refuses(cost):
    with pytest.raises(ValueError, match='host_weekly_budget_unproved'):
        owner.metered_status({'weekly_spend': {'cap_usd': '25',
            'committed_usd': cost, 'within_cap': True}})


def test_explicit_zero_cost_is_not_missing_cost():
    assert owner.metered_status({'weekly_spend': {'cap_usd': '25',
        'committed_usd': '0', 'within_cap': True}}) == 'WITHIN_CAP'

