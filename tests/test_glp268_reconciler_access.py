"""Exercise maintained named read functions, without host-only side effects."""
import ast
import asyncio
import json
from typing import Any
from pathlib import Path
from types import SimpleNamespace
import re
import urllib.parse
import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'scripts/affiliate_approval_auto_reconciler.py'

def load_function(name, **extra):
    tree = ast.parse(SOURCE.read_text())
    node = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    scope: dict[str, Any] = dict(re=re, urllib=urllib, asyncio=asyncio, json=json,
                 norm=lambda x: re.sub(r'[^a-z0-9]+', '', str(x).lower()),
                 KATALYS_ORG='098e5455-ca72-4099-9ff9-a9dd014148e7', **extra)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), scope)
    return scope[name]

class Driver:
    fresh = True
    def __init__(self): self.reads=[]
    def goto(self, url): self.url=url; self.reads.append(url)
    def body_text(self) -> str:
        return 'ops@glpclinicscout.com Izaiah' if self.url.endswith('/profile') else 'Dashboard Offers Reports Account Izaiah'
    def title(self) -> str: return 'Dashboard - HealthTrader'

class Token:
    def is_file(self): return True
    def read_text(self): return '{"access_token":"fixture-only-token","scope":"offers"}'

class HTTP:
    def __init__(self): self.calls=[]
    def get(self, url, **kw):
        self.calls.append(('GET',url))
        return SimpleNamespace(status_code=200, json=lambda:{'offers':[{'id':'fixture-only-offer'}]})
    def Session(self): raise AssertionError('legacy login HTTP session forbidden')

def test_healthtrader_api_first_then_fresh_principal_no_password_session():
    http=HTTP();driver=Driver()
    fn=load_function('healthtrader_snapshot',HEALTHTRADER_TOKEN=Token(),requests=http)
    result=asyncio.run(fn('network-account','HealthTrader',fresh_driver=driver))
    assert result['verified'] is True
    assert result['details']['principal_verified'] is True
    assert http.calls==[('GET','https://api.healthtrader.com/affiliates/v1/offers/list')]
    assert driver.reads==['https://affiliates.healthtrader.com/profile','https://affiliates.healthtrader.com/dashboard']


def test_healthtrader_api_success_is_not_approval_without_fresh_principal():
    fn=load_function('healthtrader_snapshot',HEALTHTRADER_TOKEN=Token(),requests=HTTP())
    with pytest.raises(RuntimeError,match='fresh_ai_browser_vault_stage_required'):
        asyncio.run(fn('network-account','HealthTrader'))


def test_katalys_relationship_only_api_never_attaches_cdp_or_generates_link():
    seen=[]
    def api(program,name):
        seen.append((program,name))
        return {'provider':'Katalys','program_id':program,'relationship':'approved','verified':True,'terms':'','link_generated':False}
    fn=load_function('katalys_snapshot',katalys_relationship_snapshot=api)
    result=asyncio.run(fn('123','Fixture-only partner','',allow_link_generation=False))
    assert seen==[('123','Fixture-only partner')]
    assert result['link_generated'] is False


def test_maxbounty_without_supported_api_or_fresh_account_is_explicit_hold():
    fn=load_function('maxbounty_snapshot')
    with pytest.raises(RuntimeError,match='maxbounty_supported_api_unavailable_fresh_ai_browser_vault_stage_required'):
        fn()


def test_healthtrader_wrong_protected_principal_refuses_before_dashboard():
    class Wrong(Driver):
        def body_text(self): return 'fixture-only-other@example.invalid'
    driver=Wrong()
    fn=load_function('healthtrader_snapshot',HEALTHTRADER_TOKEN=Token(),requests=HTTP())
    with pytest.raises(RuntimeError,match='healthtrader_protected_principal_unverified'):
        asyncio.run(fn('network-account','HealthTrader',fresh_driver=driver))
    assert len(driver.reads)==1


def test_maxbounty_fresh_account_access_does_not_infer_capacity_or_terms():
    class Max(Driver):
        def body_text(self): return 'ops@glpclinicscout.com' if self.url.endswith('/settings') else 'Dashboard Campaigns'
        def title(self): return 'MaxBounty Affiliate Portal'
    driver=Max();fn=load_function('maxbounty_snapshot')
    result=fn(fresh_driver=driver)
    assert result['verified'] is True and result['provider_writes']==0
    assert result['details']['capacity']=='UNKNOWN' and result['terms']==''
    assert [u.split('/')[-1] for u in driver.reads]==['settings','dashboard']


def test_registered_access_entry_never_initializes_effectful_state(monkeypatch,capsys):
    import argparse,sys
    def snapshot(**kwargs): return {'provider':'MaxBounty','program_id':'network-account','relationship':'approved','verified':True,'details':{'principal_verified':True},'read_method':'fresh_ai_browser_vault'}
    fn=load_function('main',argparse=argparse,maxbounty_snapshot=snapshot,
                     init_state=lambda:pytest.fail('state initialized'),google_service=lambda:pytest.fail('Sheet opened'),
                     gmail_service=lambda:pytest.fail('mail opened'),load_snapshot=lambda:pytest.fail('effect policy initialized'))
    monkeypatch.setattr(sys,'argv',['registered-reconciler','--access-only','maxbounty'])
    assert asyncio.run(fn(fresh_drivers={'maxbounty':Driver()}))==0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='PASS' and result['provider_writes']==0 and result['external_send'] is False


@pytest.mark.parametrize('handoff', [False, True])
@pytest.mark.parametrize('network', ['maxbounty', 'healthtrader', 'katalys', None, 'unknown'])
def test_normal_main_passes_owned_driver_to_selected_caller(monkeypatch, handoff, network):
    import argparse, sys
    driver = Driver()
    event = {'event_id': 'fixture-only-event'}
    state = SimpleNamespace(execute=lambda *a: SimpleNamespace(fetchone=lambda: None), close=lambda: None)
    sheet = SimpleNamespace(spreadsheets=lambda: SimpleNamespace(values=lambda: SimpleNamespace(get=lambda **kw: object())))
    selected_calls = []
    async def consume(selected, svc, gmail, state, dry_run, cache, *rest):
        selected_calls.append(selected)
        if network in ('maxbounty', 'healthtrader', 'katalys'):
            assert cache.get('fresh_' + network + '_driver') is driver
            assert set(cache) == {'fresh_' + network + '_driver'}
        else:
            assert cache == {}
        assert dry_run is True
        return []
    fn = load_function('main', argparse=argparse, HEADERS=['fixture-header'], SHEET_ID='fixture-sheet', TAB='fixture-tab', pad=lambda x:x,
        configure_dry_run_output=lambda x: None, load_snapshot=lambda: {}, init_state=lambda: state,
        google_service=lambda: sheet, gmail_service=lambda: object(), execute_google_request=lambda *a: {'values':[['fixture-header']]},
        prepare_sheet_schema=lambda *a, **kw: (None, {}), pending_operator_handoffs=lambda: [],
        load_operator_handoff=lambda x: {'affiliate_subset':['fixture-only-event']},
        fetch_inbox_events=lambda *a, **kw: [event], process_selected_events=consume)
    argv = ['registered-reconciler', '--dry-run', '--dry-run-output', 'fixture-only-output', '--event-id', 'fixture-only-event']
    if handoff: argv += ['--operator-handoff', 'fixture-only-handoff']
    monkeypatch.setattr(sys, 'argv', argv)
    assert asyncio.run(fn(fresh_drivers={network: driver} if network is not None else None)) == 0
    assert selected_calls == [[(event, None)]]


# Visible Katalys relationships: these retained-layout fixtures are not live proof.
KATALYS_BODY = '''Marketing
Create Link
Tilton Group
ops@glpclinicscout.com
GLP Clinic Scout
Partner
Explore
Willow
Willow
Active
by Start Willow
Program ID: 1325
Requires brand approval.
Manage
Program Description
Approved is a word in the description, not a relationship.
Payout
Default
Default
$160
Applied
GLP Clinic Scout Site
Pending
Default Landing Page
Product Categories
GLP-1
'''


class KatalysDriver:
    fresh = True
    owned_session = 'fixture-only-owned-runtime'
    def __init__(self, body=KATALYS_BODY, title='Explore: Willow', redirect=None):
        self.body, self.page_title, self.redirect, self.reads = body, title, redirect, []
    def goto(self, url): self.url = self.redirect or url; self.reads.append(url)
    def title(self): return self.page_title
    def body_text(self): return self.body
    def __getattr__(self, name): raise AssertionError('unexpected operation: ' + name)


def permission_unavailable(*args):
    raise RuntimeError('katalys_graphql_org_permission_unavailable')


def test_normal_snapshot_consumes_ui_only_after_explicit_api_permission_failure():
    calls = []
    def ui(program, name, *, fresh_driver):
        calls.append((program, name, fresh_driver))
        return {'relationship':'pending', 'verified':True, 'provider_readback_verified':True,
                'terms':'', 'tracking_link':'', 'read_method':'fresh_ai_browser_vault_visible_relationship'}
    fn = load_function('katalys_snapshot', katalys_relationship_snapshot=permission_unavailable,
                       katalys_fresh_relationship_snapshot=ui)
    driver = KatalysDriver()
    result = asyncio.run(fn('1325', 'Willow', '', allow_link_generation=False, fresh_driver=driver))
    assert calls == [('1325', 'Willow', driver)]
    assert result['api_readback'] == 'UNAVAILABLE_ORG_PERMISSION'
    assert result['relationship'] == 'pending' and result['provider_readback_verified']
    assert result['tracking_link'] == '' and result['link_generated'] is False


@pytest.mark.parametrize('error', ['katalys_graphql_errors','katalys_graphql_session_expired:401',
    'katalys_graphql_provider_name_mismatch','katalys_graphql_program_identity_count:0'])
def test_other_api_errors_never_invoke_ui(error):
    def fail(*args): raise RuntimeError(error)
    fn = load_function('katalys_snapshot', katalys_relationship_snapshot=fail,
                       katalys_fresh_relationship_snapshot=lambda *a, **kw:pytest.fail('UI invoked'))
    with pytest.raises(RuntimeError, match=error):
        asyncio.run(fn('1325', 'Willow', '', allow_link_generation=False, fresh_driver=KatalysDriver()))


def test_permission_refusal_without_fresh_driver_stays_blocked():
    fn = load_function('katalys_snapshot', katalys_relationship_snapshot=permission_unavailable)
    with pytest.raises(RuntimeError, match='org_permission_unavailable'):
        asyncio.run(fn('1325', 'Willow', '', allow_link_generation=False))


def katalys_producer():
    import sys
    sys.path.insert(0, str(ROOT / 'scripts'))
    from katalys_fresh_relationship import relationship_snapshot
    return relationship_snapshot


@pytest.mark.parametrize('status', ['Pending','Approved','Rejected'])
def test_finite_producer_reads_exact_visible_site_relationship(status):
    driver = KatalysDriver(KATALYS_BODY.replace('\nPending\n','\n'+status+'\n'))
    result = katalys_producer()('1325', 'Willow', fresh_driver=driver)
    assert result['relationship'] == status.lower() and result['verified'] is True
    assert driver.reads == ['https://app.katalys.com/pub/098e5455-ca72-4099-9ff9-a9dd014148e7/explore/1325']
    assert result['details']['principal_verified'] is True
    assert result['details']['relationship_scope'] == 'GLP Clinic Scout Site'
    assert result['details']['inventory_complete'] is False
    assert result['terms_readback'] == 'UNKNOWN' and result['terms'] == ''
    assert result['link_generated'] is False and result['provider_writes'] == 0


@pytest.mark.parametrize('body', [KATALYS_BODY.replace('ops@glpclinicscout.com','other@example.invalid'),
    KATALYS_BODY.replace('Tilton Group','Other Organization'), KATALYS_BODY.replace('Program ID: 1325','Program ID: 1326'),
    KATALYS_BODY.replace('\nWillow\n','\nOther\n'), KATALYS_BODY.replace('\nPending\n','\nActive\n'),
    KATALYS_BODY.replace('GLP Clinic Scout Site','Other Site'), KATALYS_BODY.replace('Applied\n',''),
    KATALYS_BODY.replace('GLP Clinic Scout Site\nPending','GLP Clinic Scout Site\nPending\nGLP Clinic Scout Site\nApproved'),
    KATALYS_BODY.replace('Default Landing Page','Missing Boundary')])
def test_wrong_missing_or_ambiguous_visible_fields_fail_closed(body):
    with pytest.raises(RuntimeError): katalys_producer()('1325', 'Willow', fresh_driver=KatalysDriver(body))


def test_wrong_route_title_or_missing_owned_driver_fail_closed():
    unowned = KatalysDriver(); unowned.owned_session = ''
    for driver in (None, unowned, KatalysDriver(title='Explore: Other'), KatalysDriver(redirect='https://app.katalys.com/pub/other/explore/1325')):
        with pytest.raises(RuntimeError): katalys_producer()('1325', 'Willow', fresh_driver=driver)
    assert unowned.reads == []


def test_ui_approved_does_not_authorize_link_generation():
    fn = load_function('katalys_snapshot', katalys_relationship_snapshot=permission_unavailable,
                       katalys_fresh_relationship_snapshot=katalys_producer())
    driver = KatalysDriver(KATALYS_BODY.replace('\nPending\n', '\nApproved\n'))
    with pytest.raises(RuntimeError, match='katalys_link_generation_held'):
        asyncio.run(fn('1325', 'Willow', '', allow_link_generation=True, fresh_driver=driver))
    result = asyncio.run(fn('1325', 'Willow', 'fixture-only-existing-link', allow_link_generation=False, fresh_driver=driver))
    assert result['tracking_link'] == 'fixture-only-existing-link' and result['link_generated'] is False
    assert result['terms'] == '' and result['terms_readback'] == 'UNKNOWN'


def test_graphql_forbidden_is_permission_hold_not_empty_relationship(monkeypatch):
    import sys
    class Token:
        def is_file(self): return True
        def read_text(self): return 'fixture-only-not-secret'
    portal = SimpleNamespace(BEARER_PATH=Token(), GRAPHQL='https://fixture.invalid/graphql',
        MARKETPLACE_QUERY='fixture-only-query', ORG_ID='fixture-org', PUB_ID='fixture-pub', PAGE_SIZE=10)
    monkeypatch.setitem(sys.modules, 'katalys_portal_session', portal)
    for errors, stage in [([{'extensions':{'code':'FORBIDDEN'}}], 'katalys_graphql_org_permission_unavailable'),
        ([{'extensions':{'code':'FORBIDDEN'}}, {'extensions':{'code':'OTHER'}}], 'katalys_graphql_errors'),
        ([{'message':'FORBIDDEN'}], 'katalys_graphql_errors')]:
        http = SimpleNamespace(post=lambda *a, **kw:SimpleNamespace(status_code=200,json=lambda:{'errors':errors,'data':None}))
        fn = load_function('katalys_relationship_snapshot', requests=http)
        with pytest.raises(RuntimeError, match=stage): fn('1325','Willow')


