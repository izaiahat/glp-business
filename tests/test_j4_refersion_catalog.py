"""Refersion supply must not silently collapse to the membership surface."""
from types import SimpleNamespace
import pytest
from scripts import affiliate_catalog_batch as batch
def payload(display, auth, programs, extra):
    return {'network': display, 'auth_state': auth, 'programs': programs,
            'enumerated': len(programs), **extra}

refersion = SimpleNamespace(graphql=lambda *a: None, readback=lambda *a: None,
                            OFFERS_QUERY='{ offers(limit:%d) { id } }')
_base = SimpleNamespace(readback_payload=payload)


def offer(ident):
    return {'id': ident, 'type': 'PERCENT_OF_SALE', 'commission': '10',
            'cookie_days': '90', 'business_category': 'Health and personal care',
            'shop': {'name': 'Fixture ' + ident, 'url': 'https://example.invalid'}}


def test_marketplace_only_supply_and_rejected_membership_survive(monkeypatch):
    queries = []
    def read(_transport, query, _capability):
        queries.append(query)
        if 'offset:0' in query:
            return {'offers': [offer('001'), offer('002')]}
        if 'offset:2' in query:
            return {'offers': [offer('003')]}
        assert 'offset:3' in query
        return {'offers': []}
    monkeypatch.setattr(refersion, 'graphql', read)
    monkeypatch.setattr(refersion, 'readback', lambda _: {
        'programs': [{'program_id': '002', 'name': 'Fixture 002',
                      'relationship': 'rejected', 'status_raw': 'DENIED'}], 'blockers': []})
    legacy = SimpleNamespace(refersion=refersion, _generic=lambda *a: None,
                             b=_base, SCHEMA='fixture')
    result = batch.refersion_inventory(legacy, None, page_size=2)
    assert [r['program_id'] for r in result['programs']] == ['001', '002', '003']
    assert result['programs'][1]['relationship'] == 'rejected'
    assert result['programs'][0]['relationship'] == 'marketplace_offer'
    assert result['reported_total'] is None  # Exhaustion isn't an independent total.
    assert result['marketplace_exhausted'] is True
    assert result['marketplace_enumerated'] == 3
    assert len(queries) == 3


@pytest.mark.parametrize('rows', [[offer('001'), offer('001')], [{'id': ''}]])
def test_invalid_identity_refuses_complete_capture(monkeypatch, rows):
    monkeypatch.setattr(refersion, 'graphql', lambda *a: {'offers': rows})
    monkeypatch.setattr(refersion, 'readback', lambda _: {'programs': [], 'blockers': []})
    result = batch.refersion_inventory(SimpleNamespace(refersion=refersion, b=_base), None)
    assert result['reported_total'] is None
    assert result['marketplace_exhausted'] is False
    assert result['blockers']


def test_page_cap_never_becomes_an_exhaustion_claim(monkeypatch):
    monkeypatch.setattr(refersion, 'graphql', lambda *a: {'offers': [offer('001')]})
    monkeypatch.setattr(refersion, 'readback', lambda _: {'programs': [], 'blockers': []})
    result = batch.refersion_inventory(SimpleNamespace(refersion=refersion, b=_base), None, max_pages=1)
    assert result['marketplace_exhausted'] is False
    assert result['blockers'][-1]['error_class'] == 'refersion_catalog_page_cap'
