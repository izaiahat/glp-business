"""Synthetic fixtures; these tests are not live authentication proof."""
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from scripts.marketplace_adapters import xentraffic as xt
import xentraffic_fresh_catalog as xen
import maxbounty_fresh_catalog as mbread
import affiliate_fresh_browser as fresh


class Visible:
    fresh = True
    owned_session = 'synthetic-only'
    def __init__(self):
        self.url = xt.PORTAL_URL + '/offers'
        self.visits = []
    def goto(self, url):
        self.url = url
        self.visits.append(url)
    def body_text(self):
        return 'ops@glpclinicscout.com\nPartner ID: 4384\nOffer ID: 7\nExample\nDescription\nsemaglutide\nTerms\nUnknown'
    def visible_tables(self):
        return {'tables': [{'headers': ['Offer ID','Name','Status'],
                           'rows': [{'cells': ['7','Example','Active']}]}],
                'pagers': [{'text': 'Showing 1 to 1 of 1', 'links': []}]}
    def maxbounty_search_snapshot(self):
        return {'rows': [{'text': 'Example','cells': ['Example'],
                         'links': [{'name': 'Example','href': 'https://affiliates.maxbounty.com/campaign/7','program_id': '7'}]}],
                'category_label': 'Health (1)','buttons': [],'empty_marker_present': False}
    def maxbounty_campaign_sections(self):
        return {'title_headings': ['Example'],'sections': {'Campaign Description': {'text': 'semaglutide','links': []}}}
    def buttons(self):
        return ['Build Tracking Link']


def test_xen_visible_projection_is_normal_caller_contract():
    driver = Visible()
    reader = xen.FreshVisibleCatalogReader(driver).start()
    entries, total = xt.list_offers(reader.api)
    assert total == 1 and entries[0]['network_offer_id'] == 7
    assert entries[0]['relationship']['offer_affiliate_status'] == ''
    detail = xt.offer_detail(reader.api, 7)
    assert detail['terms_and_conditions'] == '' and detail['tracking_url'] == ''
    assert not any('/go/' in url for url in driver.visits)


def test_xen_unknown_total_never_becomes_zero():
    driver = Visible()
    driver.visible_tables = lambda: {'tables': [{'headers': ['ID','Name','Status'],'rows': []}], 'pagers': []}
    reader = xen.FreshVisibleCatalogReader(driver).start()
    with pytest.raises(xt.SessionError, match='published_total_or_pager_unknown'):
        xt.list_offers(reader.api)


def test_mb_visible_page_receipt_not_approved_api_evidence(tmp_path):
    reader = mbread.FreshVisibleListingTransport(Visible(), evidence_dir=tmp_path)
    page = reader({'key':'health','kind':'category','value':'58'}, None)
    assert page['total'] == 1 and page['terminal'] is True
    assert page['evidence']['query_response'] is None
    receipt = next(tmp_path.iterdir())
    assert json.loads(receipt.read_text())['query_response'] is None
    approved = reader({'key':'approved','kind':'approved','value':''}, None)
    assert approved['total'] is None


def test_new_browser_expressions_are_fixed_reads_and_compile():
    for key in ('visible_tables','maxbounty_search','maxbounty_sections'):
        expression = fresh._READS[key]
        assert not any(word in expression for word in ('fetch(', '.value', 'localStorage', 'cookie', '.click(', 'password'))
        result = subprocess.run(['node','-e','new Function(process.argv[1]);', expression], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_fixed_visible_reads_in_recording_vm_never_touch_credentials_or_effects():
    program = r'''
const vm=require('node:vm'),fs=require('node:fs');
const exprs=JSON.parse(fs.readFileSync(0,'utf8'));
function run(secret){let sensitive=0,effects=0;
  function node(text,extra={}){
    const n={innerText:text,rel:'',href:'',disabled:false,
      getBoundingClientRect:()=>({width:10,height:10}),
      getAttribute:k=>k==='aria-disabled'?'false':null,
      querySelectorAll:()=>[],matches:s=>s==='h1'&&text==='Example',
      closest:()=>null,...extra};
    Object.defineProperty(n,'value',{get(){sensitive++;return secret;}});
    for(const key of ['click','fill','submit','requestSubmit','dispatchEvent'])n[key]=()=>{effects++;throw Error('effect');};
    return n;
  }
  const a=node('Example',{href:'https://affiliates.maxbounty.com/campaign/7'});
  const cell=node('Example'),header=node('Name');
  const row=node('Example',{querySelectorAll:s=>s==='td'?[cell]:s==='a[href]'?[a]:[]});
  const table=node('',{querySelectorAll:s=>s==='thead th'?[header]:s==='tbody tr'?[row]:[]});
  const pager=node('1',{querySelectorAll:s=>s==='a[href]'?[]:[]});
  const button=node('1',{parentElement:{classList:{contains:()=>true}}});
  const box=node('Rates\n$40',{querySelectorAll:()=>[]});
  const heading=node('Rates',{closest:()=>box,parentElement:box});
  const title=node('Example');
  const option=node('Health (1)');
  const document={body:node('Dashboard Campaigns'),querySelectorAll:s=>{
    if(s==='table')return [table];
    if(s==='nav,[role=navigation],.pagination')return [pager];
    if(s==='button.page-link')return [button];
    if(s==='select[name=m] option:checked')return [option];
    if(s==='h1,h2,h3,h4,h5,h6,[role=heading],dt,strong,[class*=card-header]')return [heading,title];
    sensitive++;throw Error('unexpected selector '+s);
  }};
  Object.defineProperty(document,'cookie',{get(){sensitive++;throw Error('cookie');}});
  const context={document,getComputedStyle:()=>({display:'block',visibility:'visible'}),
    fetch:()=>{effects++;throw Error('network');}};
  Object.defineProperty(context,'localStorage',{get(){sensitive++;throw Error('storage');}});
  Object.defineProperty(context,'sessionStorage',{get(){sensitive++;throw Error('storage');}});
  const result={};
  for(const [name,expr]of Object.entries(exprs))result[name]=vm.runInNewContext(expr,context);
  return {result,sensitive,effects};
}
const a=run('SYNTHETIC_A'),b=run('SYNTHETIC_B');
if(JSON.stringify(a)!==JSON.stringify(b)||a.sensitive!==0||a.effects!==0)process.exit(2);
console.log(JSON.stringify({fixture_only:true,operations:Object.keys(exprs).length,sensitive:a.sensitive,effects:a.effects}));
'''
    expressions = {name:fresh._READS[name] for name in ('visible_tables','maxbounty_search','maxbounty_sections')}
    result = subprocess.run(['node','-e',program], input=json.dumps(expressions), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {'fixture_only':True,'operations':3,'sensitive':0,'effects':0}



@pytest.mark.parametrize('hidden_kind', ['cell', 'descendant', 'spacer'])
def test_hidden_dom_recording_vm_and_real_mb_main_persistence(monkeypatch, tmp_path, capsys, hidden_kind):
    # Same counterexample as the independent review, plus a nested hidden span.
    # The VM reports every hidden getter and rejects all credential/effect reads.
    program = r'''
const fs=require('node:fs'),vm=require('node:vm');
const expressions=JSON.parse(fs.readFileSync(0,'utf8'));
function run(secret,kind){
 let hiddenReads=0,sensitive=0,effects=0;
 function node(text,visible=true,extra={}){
  const n={getBoundingClientRect:()=>({width:visible?10:0,height:visible?10:0}),
   _visible:visible,querySelectorAll:()=>[],getAttribute:()=>null,
   // Structural only: a text-bearing node has a child; it never reveals the text.
   hasChildNodes:()=>text!=='',...extra};
  Object.defineProperty(n,'innerText',{get(){if(!visible)hiddenReads++;return text;}});
  Object.defineProperty(n,'textContent',{get(){hiddenReads++;throw Error('textContent');}});
  Object.defineProperty(n,'value',{get(){sensitive++;throw Error('input value');}});
  n.click=n.submit=()=>{effects++;throw Error('mutation');};return n;
 }
 const a=node('Fixture wellness',true,{href:'https://affiliates.maxbounty.com/campaign/707'});
 const hidden=node(secret,false);
 // Live MaxBounty rows carry an empty hidden <div> spacer inside the name cell.
 const spacer=node('',false);
 const nested=kind==='descendant'?[hidden]:kind==='spacer'?[spacer]:[];
 const cell=node('Fixture wellness',true,{querySelectorAll:s=>s==='*'?nested:[]});
 const cells=kind==='cell'?[cell,hidden]:[cell];
 const row=node('Fixture wellness',true,{querySelectorAll:s=>s==='td'?cells:s==='a[href]'?[a]:s==='*'?[...cells,a,...nested]:[]});
 const headers=kind==='cell'?[node('Name'),node('Notes',false)]:[node('Name')];
 const table=node('',true,{querySelectorAll:s=>s==='tbody tr'?[row]:s==='thead th'?headers:[]});
 const option=node('Health (1)');
 const document={body:node('Dashboard Campaigns'),querySelectorAll:s=>s==='table'?[table]:s==='select[name=m] option:checked'?[option]:[]};
 Object.defineProperty(document,'cookie',{get(){sensitive++;throw Error('cookie');}});
 const ctx={document,getComputedStyle:e=>({visibility:e._visible?'visible':'hidden',display:e._visible?'block':'none'}),fetch:()=>{effects++;throw Error('network');}};
 for(const key of ['localStorage','sessionStorage'])Object.defineProperty(ctx,key,{get(){sensitive++;throw Error('storage');}});
 const result={};for(const [k,expr] of Object.entries(expressions)){
  try{result[k]={projection:vm.runInNewContext(expr,ctx)};}catch(e){result[k]={refused:true};}
 }
 return {result,hiddenReads,sensitive,effects};
}
const a=run('SYNTHETIC_HIDDEN_SECRET_A',process.argv[1]),b=run('SYNTHETIC_HIDDEN_SECRET_B',process.argv[1]);
console.log(JSON.stringify({a,b}));
'''
    expressions = {name: fresh._READS[name] for name in ('visible_tables', 'maxbounty_search')}
    result = subprocess.run(['node', '-e', program, hidden_kind], input=json.dumps(expressions), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    recorded = json.loads(result.stdout)
    assert recorded['a'] == recorded['b']
    assert {k: recorded['a'][k] for k in ('hiddenReads', 'sensitive', 'effects')} == {
        'hiddenReads': 0, 'sensitive': 0, 'effects': 0}
    projections = recorded['a']['result']
    if hidden_kind == 'cell':
        assert projections['visible_tables']['projection']['tables'][0]['headers'] == ['Name', '']
        assert projections['visible_tables']['projection']['tables'][0]['rows'][0]['cells'] == ['Fixture wellness', '']
    elif hidden_kind == 'spacer':
        # A childless hidden spacer is accepted structurally without any hidden read.
        assert projections['visible_tables']['projection']['tables'][0]['rows'][0]['cells'] == ['Fixture wellness']
        assert projections['maxbounty_search']['projection']['rows'][0]['cells'] == ['Fixture wellness']
    else:
        assert all(value == {'refused': True} for value in projections.values())

    import glp_maxbounty_lane as lane
    assert Path(lane.__file__).resolve() == ROOT / 'scripts/glp_maxbounty_lane.py'
    assert Path(lane.mb.__file__).resolve() == ROOT / 'scripts/marketplace_adapters/maxbounty.py'
    url = ['https://affiliates.maxbounty.com/dashboard']
    def js(expression):
        if expression == 'location.href': return url[0]
        if expression == fresh._READS['body_text']: return 'ops@glpclinicscout.com\nDashboard Campaigns'
        if expression == fresh._READS['title']: return 'MaxBounty'
        if expression == fresh._READS['maxbounty_sections']: return {'title_headings': ['Fixture wellness'], 'sections': {}}
        if expression == fresh._READS['buttons']: return ['Build tracking link']
        if expression == fresh._READS['maxbounty_search']:
            operation = projections['maxbounty_search']
            if operation.get('refused'): raise RuntimeError('SYNTHETIC_HIDDEN_SECRET_A')
            return operation['projection']
        pytest.fail('unexpected browser operation')
    driver = fresh.FreshAIBrowserReadDriver(js=js, goto_url=lambda target: url.__setitem__(0, target),
        wait_for_load=lambda: None, allowed_hosts=['affiliates.maxbounty.com'], owned_session='SYNTHETIC_ONLY')
    monkeypatch.setattr(lane, 'RUN_ROOT', tmp_path)
    monkeypatch.setattr(lane, 'repo_rel', str)
    monkeypatch.setattr(lane, 'read_sheet_snapshot', lambda: {'login_url': 'https://affiliates.maxbounty.com/login'})
    monkeypatch.setattr(lane, 'previous_approval_baseline', lambda *a: {
        'previous_approved_ids': [], 'baseline_sources': [{'source': 'SYNTHETIC_ONLY'}]})
    for name in ('bind_required_site_identities', 'update_coupon_model', 'draft_applications', 'queue_all', 'heartbeat', 'route_error'):
        monkeypatch.setattr(lane, name, lambda *a: pytest.fail('downstream effect'))
    assert lane.main(['--mode', 'read-only', '--queue', 'off', '--run-id', 'hidden'], fresh_driver=driver) == 2
    files = list(tmp_path.rglob('*.json'))
    assert files
    persisted = '\n'.join(p.read_text() for p in files)
    assert 'SYNTHETIC_HIDDEN_SECRET' not in persisted + capsys.readouterr().out
    receipt = json.loads((tmp_path / 'run-hidden/run-receipt.json').read_text())
    assert all(value == 0 for value in receipt['effects'].values())
    if hidden_kind == 'cell':
        assert list((tmp_path / 'run-hidden/search-category-pages').glob('*.json'))
        assert 'approved_account_scope_unproven' in persisted
        assert lane.main(['--mode', 'read-only', '--queue', 'off', '--run-id', 'positive'],
                         fresh_driver=driver, discovery_options={'scopes': [
                             {'key': 's', 'kind': 'search', 'value': 'wellness'},
                             {'key': 'c', 'kind': 'category', 'value': '58'}]}) == 0
        positive = json.loads((tmp_path / 'run-positive/run-receipt.json').read_text())
        assert positive['status'] == 'PASS_READ_ONLY'
        assert all(value == 0 for value in positive['effects'].values())
        assert not any('SYNTHETIC_HIDDEN_SECRET' in p.read_text() for p in tmp_path.rglob('*.json'))
    driver.close()


def test_isolated_cli_entries_are_blocked_before_authentication():
    for name in ('glp_xentraffic_enumerate.py','glp_maxbounty_lane.py'):
        env = {**os.environ, 'MAXBOUNTY_API_CREDENTIALS_FILE': ''}  # API path disabled
        result = subprocess.run([sys.executable, str(ROOT/'scripts'/name), '--queue','off'], cwd=ROOT, capture_output=True, text=True, env=env)
        assert result.returncode == 2
        payload = json.loads(result.stdout)
        assert payload['status'] == 'BLOCKED'
        assert payload['reason'] == 'fresh_ai_browser_vault_stage_required'
        assert all(value == 0 for value in payload['effects'].values())
