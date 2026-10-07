"""Finite visible relationship read; fixtures never stand in for live access."""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from test_glp268_reconciler_access import load_function

ORG = '098e5455-ca72-4099-9ff9-a9dd014148e7'
TARGET = f'https://app.katalys.com/pub/{ORG}/explore/1325'
BODY = '''Marketing
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

class Driver:
    fresh = True
    owned_session = 'fixture-only-owned-runtime'
    def __init__(self, body=BODY): self.body = body; self.reads = []
    def goto(self, url): self.url = url; self.reads.append(url)
    def body_text(self): return self.body
    def title(self): return 'Explore: Willow'


def unavailable(*args):
    raise RuntimeError('katalys_graphql_org_permission_unavailable')


def test_normal_snapshot_consumes_ui_only_after_explicit_api_permission_failure():
    calls = []
    def ui(program, name, *, fresh_driver):
        calls.append((program, name, fresh_driver))
        return {'relationship':'pending', 'verified':True, 'provider_readback_verified':True,
                'terms':'', 'tracking_link':'', 'read_method':'fresh_ai_browser_vault_visible_relationship'}
    fn = load_function('katalys_snapshot', katalys_relationship_snapshot=unavailable,
                       katalys_fresh_relationship_snapshot=ui)
    driver = Driver()
    result = asyncio.run(fn('1325', 'Willow', '', allow_link_generation=False, fresh_driver=driver))
    assert calls == [('1325', 'Willow', driver)]
    assert result['api_readback'] == 'UNAVAILABLE_ORG_PERMISSION'
    assert result['relationship'] == 'pending'
    assert result['link_generated'] is False


@pytest.mark.parametrize('error', ['katalys_graphql_errors', 'katalys_graphql_session_expired:401',
                                   'katalys_graphql_provider_name_mismatch'])
def test_unrelated_api_failures_do_not_fall_back(error):
    def fail(*args): raise RuntimeError(error)
    fn = load_function('katalys_snapshot', katalys_relationship_snapshot=fail,
                       katalys_fresh_relationship_snapshot=lambda *a,**kw: pytest.fail('UI called'))
    with pytest.raises(RuntimeError, match=error):
        asyncio.run(fn('1325', 'Willow', '', fresh_driver=Driver()))


def test_permission_denied_without_owned_driver_is_not_success_or_empty():
    fn = load_function('katalys_snapshot', katalys_relationship_snapshot=unavailable)
    with pytest.raises(RuntimeError, match='org_permission_unavailable'):
        asyncio.run(fn('1325', 'Willow', ''))


def producer():
    from katalys_fresh_relationship import relationship_snapshot
    return relationship_snapshot


def test_pending_visible_source_relationship_not_program_active_or_description_approved():
    result = producer()('1325', 'Willow', fresh_driver=Driver())
    assert result['relationship'] == 'pending'
    assert result['provider_readback_verified'] is True
    assert result['details']['principal_verified'] is True
    assert result['details']['relationship_scope'] == 'GLP Clinic Scout Site'
    assert result['details']['inventory_complete'] is False
    assert result['terms'] == '' and result['terms_readback'] == 'UNKNOWN'
    assert result['provider_writes'] == 0 and result['tracking_link'] == ''
    assert 'body' not in result['details']


@pytest.mark.parametrize('status,expected', [('Approved','approved'),('Pending','pending'),('Rejected','rejected')])
def test_explicit_visible_status(status, expected):
    result = producer()('1325', 'Willow', fresh_driver=Driver(BODY.replace('\nPending\n', f'\n{status}\n')))
    assert result['relationship'] == expected


@pytest.mark.parametrize('body', [BODY.replace('ops@glpclinicscout.com','other@example.invalid'),
    BODY.replace('Tilton Group','Other Organization'), BODY.replace('Program ID: 1325','Program ID: 1326'),
    BODY.replace('\nWillow\n','\nOther\n'), BODY.replace('\nPending\n','\nActive\n'),
    BODY.replace('GLP Clinic Scout Site','Other Site'), BODY.replace('Applied\n',''),
    BODY.replace('GLP Clinic Scout Site\nPending','GLP Clinic Scout Site\nPending\nGLP Clinic Scout Site\nApproved'),
    BODY.replace('Default Landing Page','Missing Boundary')])
def test_wrong_missing_or_ambiguous_visible_fields_fail_closed(body):
    with pytest.raises(RuntimeError): producer()('1325', 'Willow', fresh_driver=Driver(body))


def test_exact_final_route_and_owned_runtime_are_required():
    class Wrong(Driver):
        def goto(self,url): super().goto(url); self.url = url + '?other=1'
    with pytest.raises(RuntimeError): producer()('1325', 'Willow', fresh_driver=Wrong())
    driver = Driver(); driver.owned_session = ''
    with pytest.raises(RuntimeError): producer()('1325', 'Willow', fresh_driver=driver)
    assert driver.reads == []


def test_graphql_forbidden_is_not_an_empty_success(monkeypatch):
    portal = SimpleNamespace(BEARER_PATH=SimpleNamespace(is_file=lambda:True,read_text=lambda:'fixture-only'),
                            GRAPHQL='https://fixture.invalid/graphql', MARKETPLACE_QUERY='fixture-only',
                            ORG_ID=ORG, PUB_ID='fixture-only', PAGE_SIZE=20)
    monkeypatch.setitem(sys.modules,'katalys_portal_session',portal)
    payload = {'errors':[{'extensions':{'code':'FORBIDDEN'}}]}
    requests = SimpleNamespace(post=lambda *a,**kw: SimpleNamespace(status_code=200,json=lambda:payload))
    fn = load_function('katalys_relationship_snapshot',requests=requests)
    with pytest.raises(RuntimeError, match='org_permission_unavailable'): fn('1325','Willow')
    payload['errors'].append({'extensions':{'code':'INTERNAL'}})
    with pytest.raises(RuntimeError, match='katalys_graphql_errors'): fn('1325','Willow')
