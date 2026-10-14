"""Offline fictional receipts only; no provider, credential or financial state."""
import copy
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('native_intake_consumer', ROOT / 'scripts/glp334_live_reconciliation.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_retained_custody_anchors_are_sha256_not_transcription_errors():
    anchors = [m.NATIVE_FINANCIAL_SOURCE, m.NATIVE_FINANCIAL_OWNERS,
               *m.NATIVE_FINANCIAL_ORIGINALS.values()]
    assert all(len(expected) == 64 and all(c in '0123456789abcdef' for c in expected)
               for _, expected in anchors)


@pytest.fixture
def intake(tmp_path, monkeypatch):
    fields = dict(network_affiliate_invoice_id=9, network_affiliate_id=8, network_id=7,
                  currency_id='USD', timezone_id=80, start_time='2026-08-01 00:00:00',
                  end_time='2026-08-31 23:59:59', status='paid', balance=0)
    invoice = dict(http_status=200, response_sha256='a'*64, captured_at='2026-10-14T00:00:00Z',
        endpoint='/api/affiliates/billings/affiliates/invoices/9?relationship=details&relationship=payments&relationship=extra_periods',
        fields=fields, relationships={k:dict(presence=False, returned_count=None, rows=None)
                                     for k in ('details','payments','extra_periods')})
    request = dict(from_='2026-08-01')
    request = {'from':'2026-08-01','to':'2026-08-31','timezone_id':80,
               'query':{'filters':[],'search_terms':[]}}
    financial = dict(http_status=200,response_sha256='b'*64,captured_at='2026-10-14T00:01:00Z',
        endpoint='/api/affiliates/reporting/entity/table',kind='financial',
        request={**request,'currency_id':'USD','columns':[{'column':'offer'},{'column':'event_name'}]},
        table_present=True, rows=[{'columns':[{'column_type':'offer','id':'synthetic-offer'},
          {'column_type':'event_name','id':'synthetic-event'}],
          'reporting':dict(revenue=0,event=1,cv=0,on_hold_conversion_count=0,on_hold_potential_revenue=0)}])
    onhold = dict(http_status=200,response_sha256='c'*64,captured_at='2026-10-14T00:01:01Z',
        endpoint='/api/affiliates/reporting/onhold?page=1&page_size=100',kind='onhold',
        request={**request,'show_conversions':True,'show_events':True},
        conversions_present=True,conversions=[],paging=dict(page=1,page_size=100,total_count=0))
    pay = dict(http_status=200,response_sha256='d'*64,captured_at='2026-10-14T00:07:00Z',
        endpoint='/api/affiliates/integrations/pay/payments?page=1&page_size=100',account=8,network=7,
        request=dict(from_timestamp=1785556800,to_timestamp=1791936209),cutoff_UTC='2026-10-14T00:03:29Z',
        payments_present=True,payments=[],returned_count=0,paging=dict(page=1,page_size=10,total_count=0))
    native = {'invoice-expanded-native':[invoice], 'august-native-financial':[financial,onhold],
              'native-payments-lifecycle-cutoff':pay,
              'conversion-detail-native':dict(endpoint='/api/affiliates/reporting/conversions/fictional-event',
                  http_status=200,response_sha256='e'*64,captured_at='2026-10-14T00:00:00Z',
                  fields=dict(conversion_id='fictional-event',transaction_id='fictional-tx',
                              currency_id='USD',revenue=0,is_event=True))}
    owned = {'objects':[dict(network='everflow',tenant='fictional.everflowclient.io',partner_id=8,
        fresh_statement_identity={**fields},owner='GLP334 fictional account owner')]}
    # Fixed paths/hash trust anchors are replaced only in offline tests.
    pins = {}
    for key in native:
        path = tmp_path / (key+'.json')
        path.write_text(json.dumps(native[key]))
        pins[key] = (path,m.sha(path.read_bytes()))
    np = tmp_path/'native.json'; op = tmp_path/'owned.json'
    def seal():
        np.write_text(json.dumps(native)); op.write_text(json.dumps(owned))
        for key,(path,_) in list(pins.items()):
            path.write_text(json.dumps(native[key])); pins[key]=(path,m.sha(path.read_bytes()))
        monkeypatch.setattr(m,'NATIVE_FINANCIAL_SOURCE',(np,m.sha(np.read_bytes())),raising=False)
        monkeypatch.setattr(m,'NATIVE_FINANCIAL_OWNERS',(op,m.sha(op.read_bytes())),raising=False)
        monkeypatch.setattr(m,'NATIVE_FINANCIAL_ORIGINALS',pins,raising=False)
    seal()
    return native, owned, seal, np, pins


def test_registered_consumer_keeps_native_fields_without_promoting_money(intake):
    native, _, _, _, _ = intake
    got = m.retained_native_financial_input()
    assert got['financial_rows'][0]['native_revenue'] == 0
    assert got['financial_rows'][0]['native_revenue_definition'] == 'Total revenue (affiliate payout amount).'
    assert got['financial_rows'][0]['base_conversion_count'] == 0
    assert got['financial_rows'][0]['event_count'] == 1
    assert got['financial_rows'][0]['on_hold_conversion_count'] == 0
    assert got['on_hold']['rows'] == [] and got['on_hold']['total_count'] == 0
    assert got['invoices'][0]['relationships']['details']['rows'] is None
    assert got['invoices'][0]['relationships']['payments']['rows'] is None
    assert got['payment_stream']['rows'] == [] and got['payment_stream']['cutoff_UTC'] == native['native-payments-lifecycle-cutoff']['cutoff_UTC']
    assert got['payment_stream']['rail'] == 'Everflow Pay'
    assert got['payment_stream']['time_filter'] == 'time_created'
    assert got['commission_minor'] is got['cash_minor'] is None
    assert got['closed_financial_population_complete'] is False
    assert got['classification'] == 'UNKNOWN' and got['invoice_membership'] is None
    assert 'retained_native_financial_input()' in (ROOT/'scripts/glp334_live_reconciliation.py').read_text().split('def main(')[1]


def test_omitted_invoice_arrays_do_not_become_explicit_empty(intake):
    native, _, seal, _, _ = intake
    native['invoice-expanded-native'][0]['relationships']['details'] = dict(presence=True,returned_count=0,rows=[])
    seal()
    got = m.retained_native_financial_input()['invoices'][0]['relationships']
    assert got['details']['rows'] == [] and got['details']['presence'] is True
    assert got['payments']['rows'] is None and got['payments']['presence'] is False


def test_existing_main_actually_receives_evidence_without_conversion_promotion(intake, tmp_path, monkeypatch):
    import sys
    old_umask = m.os.umask(0o077)
    m.os.umask(old_umask)
    old = {name: sys.modules.get(name) for name in m.DEPENDENCIES}
    monkeypatch.setattr(m, 'ROOT', ROOT)
    try:
        reporter, test_id = m.load_reconciler()
        monkeypatch.setattr(m, 'load_reconciler', lambda: (reporter, test_id))
        roster = {'marketplace_count':1, 'marketplaces':[{'marketplace':'everflow'}]}
        rp = tmp_path/'roster.json'; rp.write_text(json.dumps(roster))
        ip = tmp_path/'identity.json'; ip.write_text(json.dumps({'schema':'reporting-identity-registry-v1','identities':[]}))
        monkeypatch.setattr(m,'ROSTER',rp); monkeypatch.setattr(m,'REGISTRY',ip)
        monkeypatch.setattr(m,'REPORTS',tmp_path/'reports')
        monkeypatch.setattr(m,'ReceiverReader',lambda: object())
        monkeypatch.setattr(m,'capture',lambda *_: {'schema':'glp-f12-reconciliation-input-v1',
            'evidence_ref':'OFFLINE-FICTIONAL-RECEIVER', 'conversions':[
            {'network':'everflow','conversion_id':'fictional-test-'+str(i),'classification':'TEST'}
            for i in range(17)], 'financial_records':[], 'complete':False})
        monkeypatch.setattr(m,'weekly_input',lambda *_: (
            {'roster_sha256':reporter.digest(roster),'observations':[]},{'sha256':'f'*64}))
        assert m.main() == 0
        result = json.loads(next((tmp_path/'reports/conversion-reconciliation').glob('*/RESULT.json')).read_bytes())
        assert len(result['retained_native_financial']['financial_rows']) == 1
        assert result['retained_native_financial']['native_event_detail']['fields']['revenue'] == 0
        assert result['retained_native_financial']['native_event_detail']['fields']['payout'] is None
        assert result['conversion_reconciliation']['excluded_counts']['TEST'] == 17
        assert result['conversion_reconciliation']['rows'] == []
        assert result['commission_minor'] is result['cash_minor'] is None
        assert result['networks'][0]['financial_input_records'] == []
    finally:
        m.os.umask(old_umask)
        for name, value in old.items():
            if value is None: sys.modules.pop(name,None)
            else: sys.modules[name]=value


@pytest.mark.parametrize('corruption', ['native_hash','original_hash','account','request','cutoff','presence'])
def test_intake_refuses_unbound_evidence_not_arbitrary_local_json(intake, corruption):
    native, _, seal, np, pins = intake
    if corruption == 'native_hash':
        np.write_text('{}')
    elif corruption == 'original_hash':
        next(iter(pins.values()))[0].write_text('{}')
    else:
        if corruption == 'account': native['invoice-expanded-native'][0]['fields']['network_affiliate_id'] = 99
        if corruption == 'request': native['august-native-financial'][0]['request']['query']['filters'] = [{'offer':99}]
        if corruption == 'cutoff': native['native-payments-lifecycle-cutoff']['request']['to_timestamp'] += 1
        if corruption == 'presence': native['invoice-expanded-native'][0]['relationships']['payments']['rows'] = []
        seal()  # Reach semantic guard even with matching fixture byte pins.
    with pytest.raises(m.InputError,match='NATIVE_FINANCIAL'):
        m.retained_native_financial_input()
