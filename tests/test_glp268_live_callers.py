"""GLP-268 live callers: MaxBounty official API first, XenTraffic live visible table.

Offline only. The MaxBounty API is replaced by a recording opener; XenTraffic
DOM reads run in a Node recording VM. Fixtures are synthetic, not live proof.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT))

import affiliate_fresh_browser as fresh  # noqa: E402
import maxbounty_publisher_api as mbapi  # noqa: E402
import xentraffic_fresh_catalog as xen  # noqa: E402
from scripts.marketplace_adapters import maxbounty_discovery as discovery  # noqa: E402
from scripts.marketplace_adapters import xentraffic as xt  # noqa: E402

SECRET = 'SYNTHETIC_PASSWORD_NEVER_LOGGED'
TOKEN = 'SYNTHETIC_TOKEN_NEVER_LOGGED'


class Response(io.BytesIO):
    def __init__(self, payload, status=200):
        super().__init__(json.dumps(payload).encode())
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def campaign(pid, name, status='Approved'):
    return {'success': True, 'campaign_id': int(pid),
            'details': {'name': name, 'status': 'Active', 'affiliate_campaign_status': status,
                        'description': 'GLP-1 telehealth program', 'rules': '', 'coupon': ''},
            'commission': [{'rate': '$150.00 per lead', 'description': 'Sale', 'default_rate': 'Y'}],
            'landing_pages': [{'full': 'https://example.test/lp'}], 'search_restrictions': 'No brand bidding',
            'email_restrictions': {'rules': ''}}


class FakeMaxBounty:
    """Records every request; serves a two-page approved scope and fixed search scopes."""

    def __init__(self, *, campaigns, approved_pages, search=None, missing=None, principal=mbapi.EXPECTED_ACCOUNT):
        self.calls, self.campaigns, self.approved_pages = [], campaigns, approved_pages
        self.search_rows = search or {}
        self.missing = missing or {}
        self.principal = principal

    def __call__(self, request, timeout=None):
        url = urlsplit(request.full_url)
        self.calls.append((request.get_method(), url.path))
        assert url.scheme == 'https' and url.netloc == 'api.maxbounty.com'
        if url.path.endswith('/authentication'):
            assert request.get_method() == 'POST'
            body = json.loads(request.data)
            assert body == {'email': mbapi.EXPECTED_ACCOUNT, 'password': SECRET}
            return Response({'success': True, 'mb-api-token': TOKEN})
        assert request.get_method() == 'GET'
        assert request.headers.get('X-access-token') == TOKEN
        if url.path.endswith('/affiliate'):
            return Response({'success': True, 'publisher_id': 1,
                             'affiliate_personal_information': {'email': self.principal, 'status': 'A'}})
        if url.path.endswith('/search'):
            q = {k: v[0] for k, v in parse_qs(url.query, keep_blank_values=True).items()}
            assert q['as'] == 'true' and q['c'] == '0'
            if q['af'] == 'true':
                pages = self.approved_pages
            else:
                pages = self.search_rows.get((q['st'], q['m']), [[]])
            number = int(q['pn'])
            rows = pages[number - 1]
            return Response({'success': True, 'total_pages': len(pages) if any(pages) else 0,
                             'matchingCampaigns': [{'campaign_id': int(pid), 'name': self.campaigns[pid],
                                                    'rate_text': '$150.00 per lead'} for pid in rows]})
        pid = url.path.rsplit('/', 1)[1]
        if pid in self.missing:
            if self.missing[pid] == 404:
                raise HTTPError(request.full_url, 404, 'Not Found', {}, io.BytesIO(b'{"success":false}'))
            return Response(campaign(pid, 'Lost campaign', self.missing[pid]))
        return Response(campaign(pid, self.campaigns[pid]))


def credentials(tmp_path, email=mbapi.EXPECTED_ACCOUNT):
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / 'login.json'
    path.write_text(json.dumps({'email': email, 'password': SECRET}))
    return path


def fixture(tmp_path, *, missing=None):
    names = {str(i): f'Fixture GLP-1 {i} - CPS (US)' for i in range(1000, 1060)}
    approved = [[str(i) for i in range(1000, 1050)], [str(i) for i in range(1050, 1055)]]
    search = {('glp', '0'): [[str(i) for i in range(1000, 1003)]], ('', '58'): [[str(1055)]]}
    fake = FakeMaxBounty(campaigns=names, approved_pages=approved, search=search, missing=missing)
    api = mbapi.MaxBountyPublisherAPI(credentials(tmp_path), opener=fake)
    return api, fake


SCOPES = [{'key': 'search-glp', 'kind': 'search', 'value': 'glp'},
          {'key': 'category-health', 'kind': 'category', 'value': '58'},
          {'key': 'account-approved', 'kind': 'approved', 'value': ''}]


def run_collect(tmp_path, api, previous):
    from scripts.marketplace_adapters import maxbounty as mb
    return mb.login_and_enumerate('https://affiliates.maxbounty.com/', run_id='fixture', fresh_driver=api,
                                  evidence_dir=tmp_path / 'evidence', scopes=SCOPES,
                                  previous_approved_ids=previous,
                                  baseline_sources=[{'source': 'SYNTHETIC_ONLY'}])


def test_mb_api_enumeration_is_complete_read_only_and_secret_free(tmp_path):
    api, fake = fixture(tmp_path, missing={'2001': 404})
    result = run_collect(tmp_path, api, ['1000', '1054', '2001'])
    assert result['status'] == 'COMPLETE' and result['complete'] is True and not result['blockers']
    assert result['enumerated'] == 56 and result['read_method'] == mbapi.READ_METHOD
    assert {p['relationship'] for p in result['programs']} == {'approved'}
    assert [r['program_id'] for r in result['coverage']['retired_404']] == ['2001']
    discovery.require_search_coverage(result)
    assert {m for m, _ in fake.calls} == {'GET', 'POST'}
    assert [p for m, p in fake.calls if m == 'POST'] == ['/affiliates/api/authentication']
    assert not any(p.endswith(('/trackinglink', '/postback', '/addBookmark', '/removeBookmark', '/globalPostback'))
                   for _, p in fake.calls)
    persisted = '\n'.join(p.read_text() for p in tmp_path.rglob('*.json') if p.name != 'login.json')
    assert SECRET not in persisted and TOKEN not in persisted + json.dumps(result)


def test_mb_api_lost_approval_is_known_change_but_unknown_status_blocks(tmp_path):
    api, _ = fixture(tmp_path, missing={'2002': 'Request approval to run'})
    result = run_collect(tmp_path, api, ['1000', '2002'])
    assert result['complete'] is True
    assert [r['program_id'] for r in result['coverage']['approval_lost']] == ['2002']
    discovery.require_search_coverage(result)
    api, _ = fixture(tmp_path / 'b', missing={'2003': 'Something new'})
    result = run_collect(tmp_path / 'b', api, ['1000', '2003'])
    assert result['complete'] is False
    assert {'error': 'campaign_absent_from_current_enumeration:2003', 'program_id': '2003'} in result['blockers']


def test_validate_approval_lost_requires_exact_current_api_answer():
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    good = {'program_id': '7', 'disposition': 'unavailable',
            'observed_at_utc': datetime.now(timezone.utc).isoformat(),
            'evidence': {'source_class': 'official_publisher_api_response', 'status': 200,
                         'endpoint': 'https://api.maxbounty.com/affiliates/api/campaign/7',
                         'affiliate_campaign_status': 'Request approval to run', 'body_sha256': 'a' * 64}}
    assert discovery.validate_approval_lost(good, '7', started.isoformat())
    for change in ({'program_id': '8'}, {'evidence': {**good['evidence'], 'status': 500}},
                   {'evidence': {**good['evidence'], 'source_class': 'naturally_observed_UI_response'}},
                   {'evidence': {**good['evidence'], 'affiliate_campaign_status': 'Pending'}},
                   {'observed_at_utc': (started - timedelta(minutes=5)).isoformat()}):
        assert not discovery.validate_approval_lost({**good, **change}, '7', started.isoformat())


def test_mb_api_principal_and_credentials_refuse_without_leaking(tmp_path):
    api, fake = fixture(tmp_path)
    fake.principal = 'someone@else.test'
    with pytest.raises(mbapi.MaxBountyAPIError) as exc:
        api.authenticate()
    assert str(exc.value) == 'maxbounty_api_principal_unverified'
    other = mbapi.MaxBountyPublisherAPI(credentials(tmp_path / 'x', email='other@x.test'), opener=fake)
    with pytest.raises(mbapi.MaxBountyAPIError) as exc:
        other.authenticate()
    assert str(exc.value) == 'maxbounty_api_credentials_principal_mismatch'
    assert SECRET not in repr(exc.value) and TOKEN not in repr(exc.value)


def test_mb_lane_uses_api_first_and_blocks_when_unconfigured(monkeypatch, tmp_path):
    import glp_maxbounty_lane as lane
    monkeypatch.setenv(mbapi.ENV_NAME, '')
    assert mbapi.credentials_path() is None
    monkeypatch.setenv(mbapi.ENV_NAME, str(credentials(tmp_path)))
    assert mbapi.credentials_path() == credentials(tmp_path)
    api = mbapi.MaxBountyPublisherAPI.from_environment(opener=lambda *a, **k: pytest.fail('network'))
    assert api is not None and api.fresh is True and api.owned_session
    assert lane.main.__kwdefaults__ == {'fresh_driver': None, 'discovery_options': None}


def test_mb_visible_search_url_carries_native_as_flag():
    calls = []

    class Driver:
        fresh, owned_session = True, 'SYNTHETIC'
        url = ''

        def goto(self, target):
            calls.append(target)
            self.url = target

        def maxbounty_search_snapshot(self):
            return {'rows': [], 'buttons': [], 'empty_marker_present': True}
    import maxbounty_fresh_catalog as mbread
    transport = mbread.FreshVisibleListingTransport(Driver(), evidence_dir='/nonexistent-never-written')
    with pytest.raises(Exception):
        transport({'key': 's', 'kind': 'search', 'value': 'glp'}, None)
    assert parse_qs(urlsplit(calls[0]).query)['as'] == ['true']


def live_snapshot(start, count, total, active, payout='CPA\n$100.000'):
    rows = [{'cells': [str(5000 - start - i), '', f'Offer {start + i}', 'Public\nAvailable', 'Telehealth',
                       '-', payout, '$0.00', 'None', '']} for i in range(count)]
    return {'tables': [{'headers': ['ID', 'Thumbnail', 'Name', 'Visibility', 'Category', 'Channels',
                                    'Payout', "Today's Revenue", 'Rules', ''], 'rows': rows}],
            'totals': [f'{total} Total'], 'pages': [{'number': str(n), 'active': n == active}
                                                  for n in range(1, (total + 99) // 100 + 1)]}


def test_xt_live_projection_and_reader_complete_through_list_offers():
    snaps = [live_snapshot(1, 100, 205, 1), live_snapshot(101, 100, 205, 2, 'CPS\n70.00%'),
             live_snapshot(201, 5, 205, 3, 'View Payouts')]

    class Driver:
        fresh, owned_session = True, 'SYNTHETIC'

        def __init__(self):
            self.pages = []

        def goto(self, url):
            self.pages.append(url)

        def body_text(self):
            return 'My Account\nPartner ID\n4384\nName\nFixture'

        def xentraffic_catalog_snapshot(self):
            return snaps[0]

        def xentraffic_offers_page(self, number):
            return snaps[number - 1]
    driver = Driver()
    reader = xen.FreshVisibleCatalogReader(driver).start()
    entries, total = xt.list_offers(reader.api)
    assert total == 205 and len(entries) == 205
    assert driver.pages == [xt.PORTAL_URL + '/myaccount', xt.PORTAL_URL + '/offers']
    offers = [xt.normalize_offer(e) for e in entries]
    assert offers[0]['payout_display'] == 'CPA: $100.000' and offers[0]['payout_amount'] == 100.0
    assert offers[150]['payout_display'] == 'CPS 70.00%' and offers[204]['payout_type'] == 'MULTIPLE'
    assert offers[0]['status'] == '' and offers[0]['visibility'] == 'public'
    assert xt.completeness(len(offers), total)[0] == 'COMPLETE'


@pytest.mark.parametrize('mutate,reason', [
    (lambda s: s.update(totals=['205 Total', '206 Total']), 'xentraffic_visible_published_total_unknown'),
    (lambda s: s.update(totals=['2O5 Total']), 'xentraffic_visible_published_total_unknown'),
    (lambda s: s['tables'][0]['rows'].pop(), 'xentraffic_visible_page_row_count_mismatch'),
    (lambda s: s['tables'][0]['rows'][0]['cells'].__setitem__(0, 'x'), 'xentraffic_visible_offer_identity_unknown'),
    (lambda s: s['tables'][0]['rows'][0]['cells'].__setitem__(6, 'CPA\n$1.0.0'), 'xentraffic_visible_payout_unknown'),
    (lambda s: s.update(pages=[{'number': '1', 'active': False}, {'number': '2', 'active': True}]),
     'xentraffic_visible_pager_state_unknown'),
    (lambda s: s['tables'].append(s['tables'][0]), 'xentraffic_visible_offer_table_unknown_or_ambiguous'),
])
def test_xt_live_projection_refuses_unknown_layouts(mutate, reason):
    snap = live_snapshot(1, 100, 205, 1)
    mutate(snap)
    with pytest.raises(xt.SessionError) as exc:
        xen.project_live_catalog(snap, expected_start=1)
    assert str(exc.value) == reason
    with pytest.raises(xt.SessionError) as exc:
        xen.project_live_catalog(live_snapshot(1, 100, 205, 1), expected_start=1, expected_total=204)
    assert str(exc.value) == 'xentraffic_catalog_total_changed_mid_read'


def test_xt_live_principal_must_be_partner_4384():
    class Driver:
        fresh, owned_session = True, 'SYNTHETIC'

        def goto(self, url):
            pass

        def body_text(self):
            return 'My Account\nPartner ID\n9999'

        def xentraffic_catalog_snapshot(self):
            pytest.fail('catalogue read before principal')
    with pytest.raises(xt.SessionError):
        xen.FreshVisibleCatalogReader(Driver()).start()


def test_xt_diff_treats_unknown_status_and_tiered_payout_as_no_change():
    was = {'offer_id': 1, 'status': 'active', 'payout_type': 'CPS', 'payout_amount': 0, 'payout_display': 'CPS 70.00%'}
    now = {'offer_id': 1, 'status': '', 'payout_type': 'CPS', 'payout_amount': None, 'payout_display': 'CPS 70.00%'}
    assert xt.diff_snapshots([was], [now]) == []
    assert xt.diff_snapshots([was], [{**now, 'payout_type': 'MULTIPLE', 'payout_display': ''}]) == []
    changed = xt.diff_snapshots([was], [{**now, 'payout_display': 'CPS 60.00%'}])
    assert [d['kind'] for d in changed] == ['payout_change']
    assert [d['kind'] for d in xt.diff_snapshots([was], [{**now, 'status': 'paused'}])] == ['status_change']


def test_xt_catalog_read_in_recording_vm_never_reads_hidden_text_or_effects():
    program = r'''
const vm=require('node:vm'),fs=require('node:fs');
const expr=fs.readFileSync(0,'utf8');
function run(secret,kind){
 let hiddenReads=0,sensitive=0,effects=0;
 function node(tag,text,{boxed=true,area=true,overflow='visible',kids=[],attrs={},cls=[]}={}){
  const n={tagName:tag,children:kids,classList:{contains:c=>cls.includes(c)},
   getClientRects:()=>boxed?[1]:[],getBoundingClientRect:()=>({width:area?10:0,height:area?10:0}),
   getAttribute:k=>k in attrs?attrs[k]:null,hasChildNodes:()=>text!==''||kids.length>0,
   querySelectorAll:s=>s==='*'?kids.flatMap(k=>[k,...k.querySelectorAll('*')]):[],
   _css:{visibility:boxed?'visible':'hidden',overflow,overflowX:overflow,overflowY:overflow}};
  Object.defineProperty(n,'innerText',{get(){if(!boxed)hiddenReads++;return text;}});
  Object.defineProperty(n,'textContent',{get(){hiddenReads++;throw Error('textContent');}});
  Object.defineProperty(n,'value',{get(){sensitive++;throw Error('value');}});
  n.click=()=>{effects++;};return n;
 }
 const hidden=node('SPAN',secret,{boxed:false});
 const clip=node('DIV',secret,{area:false,overflow:'hidden',kids:[node('SPAN',secret)]});
 const inner=kind==='hidden'?[hidden]:kind==='clip'?[clip]:[];
 const cells=['3337','','GoXENBURN','Public\nAvailable','Nutra','-','CPA\n$100.000','$0.00','None',''].map((t,i)=>node('TD',t,{kids:i===2?inner:[]}));
 const ths=['ID','Thumbnail','Name','Visibility','Category','Channels','Payout',"Today's Revenue",'Rules',''].map(t=>node('TH',t,{attrs:{rowspan:'2'}}));
 const hr1=node('TR','',{kids:ths}),hr2=node('TR','',{kids:[]});
 const tr=node('TR','',{});tr.querySelectorAll=s=>s==='td'?cells:s==='*'?cells.flatMap(c=>[c,...c.querySelectorAll('*')]):[];
 const table=node('TABLE','',{});table.querySelectorAll=s=>s==='thead tr'?[hr1,hr2]:s==='tbody tr'?[tr]:s==='*'?[]:[];
 const total=node('SPAN','1 Total'),page=node('LI','1',{cls:['active']});
 const document={querySelectorAll:s=>s==='table'?[table]:s==='span,div'?[total]:s==='li.page-number'?[page]:[]};
 Object.defineProperty(document,'cookie',{get(){sensitive++;throw Error('cookie');}});
 const ctx={document,getComputedStyle:e=>e._css,fetch:()=>{effects++;throw Error('network');}};
 for(const k of ['localStorage','sessionStorage'])Object.defineProperty(ctx,k,{get(){sensitive++;throw Error('storage');}});
 let result;try{result={projection:vm.runInNewContext(expr,ctx)};}catch(e){result={refused:true};}
 return {result,hiddenReads,sensitive,effects};
}
const kind=process.argv[1];
console.log(JSON.stringify({a:run('SYNTHETIC_HIDDEN_A',kind),b:run('SYNTHETIC_HIDDEN_B',kind)}));
'''
    for kind in ('plain', 'hidden', 'clip'):
        result = subprocess.run(['node', '-e', program, kind], input=fresh._READS['xentraffic_catalog'],
                                text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        recorded = json.loads(result.stdout)
        assert recorded['a'] == recorded['b']
        assert (recorded['a']['hiddenReads'], recorded['a']['sensitive'], recorded['a']['effects']) == (0, 0, 0)
        if kind == 'clip':
            assert recorded['a']['result'] == {'refused': True}
        else:
            projection = recorded['a']['result']['projection']
            assert projection['totals'] == ['1 Total'] and projection['pages'] == [{'number': '1', 'active': True}]
            assert projection['tables'][0]['rows'][0]['cells'][2] == 'GoXENBURN'
            assert xen.project_live_catalog(projection, expected_start=1)['paging']['total_count'] == 1


def test_xt_driver_methods_are_scoped_to_exact_offers_page():
    url = ['https://xentraffic.everflowclient.io/myaccount']
    driver = fresh.FreshAIBrowserReadDriver(js=lambda e: url[0] if e == 'location.href' else pytest.fail('read'),
                                            goto_url=lambda u: url.__setitem__(0, u), wait_for_load=lambda: None,
                                            allowed_hosts=['xentraffic.everflowclient.io'], owned_session='SYNTHETIC')
    with pytest.raises(fresh.FreshAIBrowserRequired):
        driver.xentraffic_catalog_snapshot()
    with pytest.raises(fresh.FreshAIBrowserRequired):
        driver.xentraffic_offers_page(1)
