"""Persisted reporting identity only: no event, money, API or database fixture.

Portable CI tests the real unchanged normalizer and configuration. Retained
host evidence hashes are independently authenticated in the GLP-85 receipt;
the normalizer itself does not authenticate evidence hashes.
"""
import ast
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
CJ_PARENT_EVIDENCE = '/home/ubuntu/business/reports/ops/resume-20260929/reporting-denominator/RESULT.json'
CJ_PARENT_EVIDENCE_SHA = 'c5160f826dfc3055778a0f359e940242b595ff9bcd52b9bc087b4329ab628a9e'
CURRENT_PARENT_SHA = '93aa4fa83cba6ee24c81504fe0e58133c87014cb7bb618e1ff9140ab66b21271'
OLD_CJ_ACCOUNT_EVIDENCE = '/home/ubuntu/business/reports/ops/orchestration-restore-20260907/WS-F/NETWORK-REPORTING-ALL/api/cj-readback.json'
OLD_CJ_ACCOUNT_EVIDENCE_SHA = 'f1a52c653386a9dbb4f9a1e7b3b841c840a46b92afae393776470cc56c2b92a5'


def restore_cj_account_evidence(document):
    """Historical inverse only; no installed registry or wrapper update."""
    document['accounts'][0]['evidence_ref'] = OLD_CJ_ACCOUNT_EVIDENCE
    document['accounts'][0]['evidence_sha256'] = OLD_CJ_ACCOUNT_EVIDENCE_SHA
    return document


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


def test_cj_parent_resolution_maps_only_existing_account_evidence():
    document = registry()
    account = document['accounts'][0]
    assert account['evidence_ref'] == CJ_PARENT_EVIDENCE
    assert account['evidence_sha256'] == CJ_PARENT_EVIDENCE_SHA
    assert set(account) == {'account_id', 'evidence_ref', 'evidence_sha256', 'network',
                            'scope', 'status', 'tenant_id', 'tenant_kind'}
    assert account['scope'] == 'publisher_account_only_not_current_financial_or_program_coverage'
    assert len(document['accounts']) == 2 and len(document['identities']) == 2
    assert len(document['gaps']) == document['typed_identity_gap_count'] == 800
    gap = document['gaps'][326]
    assert (gap['sheet_row'], gap['source_pointer'], gap['status']) == (
        778, '/marketplaces/11/programs/56', 'UNRESOLVED_TYPED_IDENTITY')
    assert gap['account_id'] is None and gap['program_id'] is None
    # Execute the registered runner's existing account selector, without main,
    # credential access, transport, database, event or collector execution.
    tree = ast.parse((ROOT / 'scripts/glp_affiliate_weekly_reporting.py').read_text())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'main')
    selector = next(n.value for n in ast.walk(main) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'accounts' for t in n.targets))
    selected = eval(compile(ast.Expression(selector), '<registered-account-selector>', 'eval'),
                    {'registry': document, 'account': '8010186'})
    assert selected == [account]
    # Promotional-property ID is never a new advertiser/program authority.
    with pytest.raises(ValueError, match='^identity_unresolved_or_ambiguous$'):
        revenue.normalize(NoFinancialEvent(), dict(TUPLE, program_id='101822575'), document['identities'])
    # Reverse exactly the two evidence values; every other byte must match main,
    # including accepted Ro/current CJ tuples and all 800 dated gap entries.
    restored_bytes = CONFIG.read_bytes().replace(CJ_PARENT_EVIDENCE.encode(), OLD_CJ_ACCOUNT_EVIDENCE.encode())
    restored_bytes = restored_bytes.replace(CJ_PARENT_EVIDENCE_SHA.encode(), OLD_CJ_ACCOUNT_EVIDENCE_SHA.encode())
    assert hashlib.sha256(restored_bytes).hexdigest() == CURRENT_PARENT_SHA


def test_persisted_exact_tuple_resolves_without_financial_event():
    document = registry()
    assert len(document['identities']) == 2
    bound = next(row for row in document['identities'] if row['network'] == 'cj')
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


def test_ro_public_offer_reporting_identity_resolves_without_financial_event():
    document = registry()
    identity = {'network': 'roeverflowwhitelabeltenant4131', 'account_id': '14',
                'tenant_id': 'ro.everflowclient.io', 'program_id': '1'}
    bound = next(row for row in document['identities'] if row['network'] == identity['network'])
    assert {k: bound[k] for k in revenue.IDENTITY} == identity
    assert bound['tenant_kind'] == 'host' and bound['tenant_network_id'] == '4131'
    assert bound['provider_slug'] == 'ro-wegovy-glp1-quiz-us-cpl'
    assert bound['verification_scope'] == 'reporting_identity_only_public_offer_access_not_private_approval_or_financial_coverage'
    assert bound['evidence_sha256'] == '3f5c422aafb8967e8c3052f556fb93f4658b92051302f9168e53c7f6cb86b92a'
    assert len(bound['source_refs']) == 3
    assert {k: document['accounts'][1][k] for k in ('network', 'account_id', 'tenant_id')} == {
        k: identity[k] for k in ('network', 'account_id', 'tenant_id')}
    with pytest.raises(IdentityResolved, match='IDENTITY_RESOLVED_BEFORE_EVENT'):
        revenue.normalize(NoFinancialEvent(), identity, document['identities'])
    with pytest.raises(ValueError, match='^identity_unresolved_or_ambiguous$'):
        revenue.normalize(NoFinancialEvent(), dict(identity, program_id='3164'), document['identities'])


def test_weekly_wrapper_config_pin_and_all_other_bytes_preserved():
    wrapper = (ROOT / 'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh').read_bytes()
    # F12 adds only a pinned retained snapshot argument, never current coverage.
    retained = (b'# Retained D1 capture, NOT a fresh receiver export or closed-payout proof.\n'
        b'check_source reports/ops/f12-host-followthrough-20261002/reconciliation-input.json 5ae5aee441a9dbc6c9cb17dfb66fe0c7f28068f68d267ba6707b3c333deb95f1\n'
        b'args+=(--reconciliation-input /home/ubuntu/business/reports/ops/f12-host-followthrough-20261002/reconciliation-input.json)\n')
    assert wrapper.count(retained) == 1
    wrapper = wrapper.replace(retained, b'')
    # GLP333 successor: authenticate the three source/mode changes, then undo
    # only those changes before the unchanged GLP85 historical inverse oracle.
    source_pin = hashlib.sha256((ROOT/'ops/glp/reporting-v2/source-manifest.json').read_bytes()).hexdigest().encode()
    job_pin = hashlib.sha256((ROOT/'scripts/glp_affiliate_weekly_reporting_job.py').read_bytes()).hexdigest().encode()
    assert wrapper.count(source_pin) == 2 and wrapper.count(job_pin) == 1
    assert wrapper.count(b'args=(--live-network-readonly)') == 1
    wrapper = wrapper.replace(source_pin,b'34f39b915a78e9ce188777f9c33bd30e9edab147ceba7df26328bae75dba54bb')
    wrapper = wrapper.replace(job_pin,b'ae7cba876c14fd52594a4827d2c9289e264080bd9d72fb355b4dab64b48e57d8')
    wrapper = wrapper.replace(b'args=(--live-network-readonly)',b'args=(--live-cj-readonly)')
    prefix = b'check_source ops/glp/reporting-v2/identity-registry.json '
    lines = [line for line in wrapper.splitlines() if line.startswith(prefix)]
    assert len(lines) == 1
    pin = lines[0][len(prefix):]
    # F07 publishes the persisted registry pin; retain the exact historical
    # inverse below rather than blessing the formerly stale literal.
    assert pin.decode() == hashlib.sha256(CONFIG.read_bytes()).hexdigest()
    restored = wrapper.replace(lines[0], prefix + b'70f8139656cca7bcf834e447e142aa69bb2f8add7b5f28fd0d23a2f393688638')
    assert hashlib.sha256(restored).hexdigest() == '197d2f094203bb1f464ac558aac14376093b9311b816245fbf47d0a97b895d83'
    prior = restored.replace(prefix + b'70f8139656cca7bcf834e447e142aa69bb2f8add7b5f28fd0d23a2f393688638',
                             prefix + PREDECESSOR_SHA.encode())
    # Undo the other parent's two reviewed manifest-pin substitutions too;
    # the common predecessor must still match byte-for-byte.
    old_manifest = b'5c022f814c9d3160cd61185b24b952974347689b7dd2349e846bb34e75795da5'
    new_manifest = b'34f39b915a78e9ce188777f9c33bd30e9edab147ceba7df26328bae75dba54bb'
    assert prior.count(new_manifest) == 2
    prior = prior.replace(new_manifest, old_manifest)
    assert hashlib.sha256(prior).hexdigest() == 'd4b3a378c34013adcc65909b52b7973890b8b8523e8f5f887a5f3ea01fcf286e'


def test_only_identity_and_freshness_changed_from_exact_predecessor():
    document = restore_cj_account_evidence(registry())
    assert '2026-09-26/27' in document['freshness']
    assert len(document['gaps']) == document['typed_identity_gap_count'] == 800
    document['identities'] = [row for row in document['identities'] if row['network'] == 'cj']
    document['accounts'] = [row for row in document['accounts'] if row['network'] == 'cj']
    document['freshness'] = 'DATED_S2_GAPS_PRESERVED; one CJ/TRT Nation reporting identity bound to retained 2026-09-27 evidence; not full current network, program or financial coverage'
    cj_predecessor = (json.dumps(document, indent=2, sort_keys=True) + '\n').encode()
    assert hashlib.sha256(cj_predecessor).hexdigest() == '70f8139656cca7bcf834e447e142aa69bb2f8add7b5f28fd0d23a2f393688638'
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
