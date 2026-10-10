from types import SimpleNamespace
import pytest
from scripts import affiliate_catalog_batch as batch


def fixture(total=2, blockers=None):
    catalog = {'programs':[{'program_id':'a','group_slug':'a','name':'Current','relationship':'notjoined'}, {'program_id':'b','group_slug':'b','name':'Other','relationship':'notjoined'}], 'reported_total':total, 'blockers':[], 'endpoint':'marketplace'}
    joined = {'programs':[{'program_id':'a','group_slug':'a','name':'Current','relationship':'approved'}, {'program_id':'c','group_slug':'c','name':'Delisted joined','relationship':'pending'}], 'blockers':blockers or [], 'endpoint':'partnerships'}
    def payload(display, auth, programs, extra):
        return {'network':display,'auth_state':auth,'programs':programs,'enumerated':len(programs),**extra}
    return SimpleNamespace(partnerstack=SimpleNamespace(marketplace=lambda _:catalog,partnerships=lambda _:joined), b=SimpleNamespace(readback_payload=payload), verdict=lambda n,t,b: ('COMPLETE' if not b and t is not None and n>=t else 'INCOMPLETE','legacy'))


def test_partnerstack_joined_only_rows_do_not_corrupt_marketplace_denominator():
    legacy = fixture()
    inventory = batch.partnerstack_inventory(legacy, None)
    assert inventory['marketplace_reported_total'] == 2
    assert inventory['marketplace_enumerated'] == 2
    assert inventory['joined_only_enumerated'] == 1
    assert inventory['reported_total'] == 3
    assert inventory['enumerated'] == 3
    assert {p['program_id']:p['relationship'] for p in inventory['programs']} == {'a':'approved','b':'notjoined','c':'pending'}
    assert batch.catalog_verdict(legacy, inventory)['complete'] is True
    # Actual mismatch remains a mismatch, missing provider total stays unknown,
    # blocked partnerships stay incomplete. No >= oracle is broadened.
    for legacy in (fixture(3),fixture(None),fixture(blockers=[{'error_class':'auth'}])):
        actual = batch.partnerstack_inventory(legacy, None)
        assert batch.catalog_verdict(legacy,actual)['complete'] is False
    assert batch.partnerstack_inventory(fixture(None),None)['reported_total'] is None
