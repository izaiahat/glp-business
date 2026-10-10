from types import SimpleNamespace
from scripts import affiliate_catalog_batch as batch
import pytest


def test_current_scoped_maxbounty_capture_reaches_supply_without_global_upgrade(monkeypatch):
    from scripts.marketplace_adapters import maxbounty_discovery as discovery
    import datetime as dt
    captured = dt.datetime.now(dt.timezone.utc).isoformat()
    data = {'status': 'COMPLETE', 'complete': True, 'captured_at_utc': captured,
            'coverage': {'schema': 'glp.maxbounty.account-catalog-coverage.v2'},
            'programs': [{'program_id': '001', 'relationship': 'pending', 'name': 'Fixture GLP clinic'}],
            'enumerated': 1}
    checked = []
    def prove(doc):
        assert doc is data
        checked.append('unchanged current coverage validator')
        return {'scope': 'declared-search+categories', 'global_complete': False}
    monkeypatch.setattr(discovery, 'require_search_coverage', prove)
    denominator = SimpleNamespace(freshest_capture=lambda: ('fixture-capture.json', data, ''),
                                  MAX_CAPTURE_AGE_HOURS=30)
    legacy = SimpleNamespace(maxbounty_denominator=denominator, SCHEMA='fixture')
    result = batch.maxbounty_inventory(legacy, None)
    assert checked == ['unchanged current coverage validator']
    assert result['programs'][0]['program_id'] == '001'
    assert result['programs'][0]['relationship'] == 'pending'
    assert result['enumerated'] == 1
    assert result['reported_total'] is None
    assert result['global_complete'] is False
    assert result['blockers'][0]['error_class'] == 'maxbounty_global_catalog_denominator_unproven'
    def refuse(_):
        raise ValueError('search_category_proof_disagreement')
    monkeypatch.setattr(discovery, 'require_search_coverage', refuse)
    with pytest.raises(ValueError, match='proof_disagreement'):
        batch.maxbounty_inventory(legacy, None)


def test_catalog_count_overflow_is_not_complete_even_if_legacy_uses_greater_equal():
    legacy = SimpleNamespace(verdict=lambda count, total, blockers: ('COMPLETE', 'legacy >= comparison'))
    actual = batch.catalog_verdict(legacy, {'enumerated': 419, 'reported_total': 418, 'blockers': []})
    assert actual['complete'] is False
    assert actual['completeness'] == 'INCOMPLETE'
    assert actual['enumerated'] == 419
    assert actual['reported_total'] == 418
    assert 'count_mismatch' in actual['detail']
