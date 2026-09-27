"""Persisted reporting identity only: no event, money, API or database fixture.

Portable CI tests the real unchanged normalizer and configuration. Retained
host evidence hashes are independently authenticated in the GLP-85 receipt;
the normalizer itself does not authenticate evidence hashes.
"""
import copy
import hashlib
import json
from pathlib import Path

import pytest
import glp_network_revenue_v2 as revenue

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'ops/glp/reporting-v2/identity-registry.json'
PREDECESSOR_SHA = '0f46cfb248f1d20a7c976ed7baec0c6d816a3371da95bdbc0bca8e21c96cf1f8'
OLD_FRESHNESS = 'DATED_S2_SNAPSHOT; full identities intentionally unresolved; not a replacement live registry'
TUPLE = {'network': 'cj', 'account_id': '8010186',
         'tenant_id': 'not_applicable', 'program_id': '7538517'}


class IdentityResolved(BaseException):
    """Exit at the first event read, after the real identity gate."""


class NoFinancialEvent:
    def get(self, key, *args):
        assert key == 'provider_event_id'
        raise IdentityResolved('IDENTITY_RESOLVED_BEFORE_EVENT')


def registry():
    document = json.loads(CONFIG.read_text())
    assert document['schema'] == 'reporting-identity-registry-v1'
    return document


def test_persisted_exact_tuple_resolves_without_financial_event():
    document = registry()
    assert len(document['identities']) == 1
    bound = document['identities'][0]
    assert {k: bound[k] for k in revenue.IDENTITY} == TUPLE
    assert bound['provider_slug'] == 'trt-nation'
    assert bound['tenant_kind'] == 'not_applicable'
    assert bound['status'] == 'verified'
    assert bound['verification_scope'] == 'reporting_identity_only_not_activation_or_financial_coverage'
    assert bound['evidence_pointer'] == '/validated_mapping'
    assert len(bound['evidence_sha256']) == 64
    assert bound['source_refs'][0]['pointer'] == '/rows/0/publisherId'
    assert bound['source_refs'][1]['pointer'] == '/rows/4'
    assert {k: document['accounts'][0][k] for k in ('network', 'account_id', 'tenant_id')} == {
        k: TUPLE[k] for k in ('network', 'account_id', 'tenant_id')}
    with pytest.raises(IdentityResolved, match='IDENTITY_RESOLVED_BEFORE_EVENT'):
        revenue.normalize(NoFinancialEvent(), TUPLE, document['identities'])


def test_only_identity_and_freshness_changed_from_exact_predecessor():
    document = registry()
    assert '2026-09-27' in document['freshness']
    assert len(document['gaps']) == document['typed_identity_gap_count'] == 800
    document['identities'] = []
    document['freshness'] = OLD_FRESHNESS
    restored = (json.dumps(document, indent=2, sort_keys=True) + '\n').encode()
    assert hashlib.sha256(restored).hexdigest() == PREDECESSOR_SHA


@pytest.mark.parametrize('key,value', [
    ('network', 'awin'), ('account_id', '101822575'),
    ('account_id', '8010187'), ('tenant_id', '101822575'),
    ('tenant_id', 'example.invalid'), ('program_id', '101822575'),
    ('program_id', '6105366'),
])
def test_wrong_tuple_and_property_substitution_refuse(key, value):
    identity = dict(TUPLE, **{key: value})
    with pytest.raises(ValueError, match='^identity_unresolved_or_ambiguous$'):
        revenue.normalize(NoFinancialEvent(), identity, registry()['identities'])


@pytest.mark.parametrize('case,error', [
    ('empty', 'identity_unresolved_or_ambiguous'),
    ('duplicate', 'identity_unresolved_or_ambiguous'),
    ('unverified', 'identity_unresolved_or_ambiguous'),
    ('missing_evidence', 'identity_registry_incomplete'),
    ('wrong_tenant_kind', 'tenant_kind_mismatch'),
    ('fella', 'excluded_fella'),
    ('missing_provider', 'provider_exclusion_identity_unresolved'),
])
def test_unchanged_consumer_safety_guards(case, error):
    rows = copy.deepcopy(registry()['identities'])
    if case == 'empty':
        rows = []
    elif case == 'duplicate':
        rows += copy.deepcopy(rows)
    elif case == 'unverified':
        rows[0]['status'] = 'unknown'
    elif case == 'missing_evidence':
        rows[0]['evidence_ref'] = ''
    elif case == 'wrong_tenant_kind':
        rows[0]['tenant_kind'] = 'host'
    elif case == 'fella':
        rows[0]['provider_slug'] = 'fella-health'
    elif case == 'missing_provider':
        rows[0]['provider_slug'] = None
    with pytest.raises(ValueError, match='^' + error + '$'):
        revenue.normalize(NoFinancialEvent(), TUPLE, rows)
