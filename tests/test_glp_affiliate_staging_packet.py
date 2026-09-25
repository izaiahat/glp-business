from __future__ import annotations
import hashlib,importlib.util,json
from pathlib import Path
ROOT=Path('/home/ubuntu/business')
VALIDATOR=ROOT/'reports/backlog-staging-hardening-20260723/glp/validate_staging_packet.py'
PACKET=ROOT/'reports/backlog-staging-hardening-20260723/glp/staging-packet.json'

def load_validator():
    spec=importlib.util.spec_from_file_location('glp_stage_validator',VALIDATOR)
    module=importlib.util.module_from_spec(spec);assert spec and spec.loader;spec.loader.exec_module(module);return module

def packet(): return json.loads(PACKET.read_text())

def test_current_staging_packet_passes_exact_contract():
    validator=load_validator()
    historical=packet()
    historical_result=validator.validate(historical)
    assert historical_result['failures']==[
        'source_hash:reports/glp-pass2-20260730/private/91-affiliate-api-capabilities-live-apply-manifest.json',
        'source_hash:scripts/affiliate_approval_auto_reconciler.py',
        'source_hash:scripts/affiliate_marketplace_update_sheet.py',
        'source_hash:scripts/affiliate_sheet_batch_writer.py',
        'source_hash:scripts/affiliate_sheet_schema_guard.py',
        'source_hash:scripts/apply_affiliate_api_capabilities_migration.py',
    ]
    current=json.loads(json.dumps(historical))
    for relative in current['source_sha256']:
        current['source_sha256'][relative]=hashlib.sha256((ROOT/relative).read_bytes()).hexdigest()
    body=dict(current);body.pop('packet_sha256',None)
    current['packet_sha256']=hashlib.sha256(
        json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    result=validator.validate(current)
    assert result['status']=='PASS'
    assert result['staged_rows']==10
    assert result['access_gates']==15
    assert result['sheet_writes']==0

def test_checkbox_string_and_missing_strict_rule_fail_closed():
    value=packet();value['staged_candidate_rows'][0]['values'][1]='FALSE';value['sheet_contract']['checkbox_validation']['strict']=False
    result=load_validator().validate(value)
    assert result['status']=='FAIL'
    assert 'row_0_checkbox_1_not_bool' in result['failures']
    assert 'checkbox_validation_rule' in result['failures']

def test_live_effect_and_false_marketplace_completion_claim_fail_closed():
    value=packet();value['effects']['sheet_writes']=1;value['current_reconciliation']['all_marketplaces_complete']=True
    result=load_validator().validate(value)
    assert result['status']=='FAIL'
    assert 'forbidden_effect_sheet_writes' in result['failures']
    assert 'false_completion_claim' in result['failures']

def test_duplicate_identity_link_and_unheld_action_fail_closed():
    value=packet();value['staged_candidate_rows'][1]['identity']=value['staged_candidate_rows'][0]['identity'];value['staged_candidate_rows'][0]['values'][14]='https://tracking.invalid/';value['staged_candidate_rows'][0]['relationship_action']='APPLY'
    result=load_validator().validate(value)
    assert result['status']=='FAIL'
    assert 'staged_row_count_or_identity_uniqueness' in result['failures']
    assert 'row_0_link_or_wired' in result['failures']
    assert 'row_0_not_held' in result['failures']

def test_source_or_packet_hash_drift_fails_closed():
    value=packet();value['source_sha256'][next(iter(value['source_sha256']))]='0'*64
    result=load_validator().validate(value)
    assert result['status']=='FAIL'
    assert any(item.startswith('source_hash:') for item in result['failures'])
    assert 'packet_sha256' in result['failures']
