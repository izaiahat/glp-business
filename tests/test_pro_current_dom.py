"""Replay redacted captured current DOM, never live browser/network."""
import copy
import json
from pathlib import Path
import subprocess
from unittest import mock
from tests import test_gpt6pro_relay_binding as binding
relay = binding.relay
FIXTURES = Path(__file__).parent / 'fixtures/pro_current_dom'

# Minimal DOM adapter uses the captured tree (not selector-specific return mocks).
# It supports only selectors emitted by these read-only contract functions.
DOM = r'''
const tree=JSON.parse(process.argv[1]),expression=JSON.parse(process.argv[2]);
function simple(e,s){
 let tag=s.match(/^[a-z0-9]+/i);if(tag&&e.tagName!==tag[0].toUpperCase())return false;
 let re=/\[([^\]=*$]+)(\$=|\*=|=)?(?:"([^"]*)"|'([^']*)'|([^\]]*))?\]/g,m;
 while((m=re.exec(s))){let v=e.getAttribute(m[1]),q=m[3]??m[4]??m[5];
  if(v===null)return false;
  if(m[2]==='='&&v!==q)return false;
  if(m[2]==='$='&&!v.endsWith(q))return false;
  if(m[2]==='*='&&!v.includes(q))return false;}
 return true;
}
function matches(e,s){const parts=s.trim().split(/\s+(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)/);let i=parts.length-1;
 if(!simple(e,parts[i--]))return false;
 for(let p=e.parentElement;i>=0;p=p?.parentElement){if(!p)return false;if(simple(p,parts[i]))i--;}
 return true;
}
function build(t,parent=null){
 if('text'in t)return {nodeType:3,data:t.text,parentElement:parent};
 let e={nodeType:1,tagName:t.tag.toUpperCase(),attrs:t.attrs||{},parentElement:parent,
 getAttribute(k){return Object.hasOwn(this.attrs,k)?this.attrs[k]:null},
 get hidden(){return Object.hasOwn(this.attrs,'hidden')},get inert(){return Object.hasOwn(this.attrs,'inert')},get disabled(){return Object.hasOwn(this.attrs,'disabled')},
 get textContent(){return this.childNodes.map(n=>n.data??n.textContent).join('')},
 get innerText(){return this.textContent},get children(){return this.childNodes.filter(n=>n.nodeType===1)},
 getClientRects(){return this.hidden?[]:[{}]},
 matches(s){return s.split(',').some(x=>matches(this,x))},
 closest(s){for(let n=this;n;n=n.parentElement)if(n.matches(s))return n;return null},
 querySelectorAll(s){let out=[];const visit=n=>{for(const c of n.children){if(c.matches(s))out.push(c);visit(c)}};visit(this);return out},
 querySelector(s){return this.querySelectorAll(s)[0]||null}};
 e.childNodes=(t.children||[]).map(c=>build(c,e));return e;
}
const root=build({tag:'document',children:[tree]});
global.document=root;global.getComputedStyle=()=>({display:'block',visibility:'visible'});
console.log(JSON.stringify(eval(expression)));
'''


def fixture(name):
    return json.loads((FIXTURES / (name + '.json')).read_text())


def walk(tree):
    yield tree
    for child in tree.get('children', []):
        yield from walk(child)


def evaluate(tree, expression):
    result = subprocess.run(['node', '-e', DOM, json.dumps(tree), json.dumps(expression)],
                            check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def current(tree, start=None, end=None):
    args = '' if start is None else f'{start},{end}'
    return evaluate(tree, '(()=>{' + relay.CURRENT_REPLY_JS + f'return currentFinalReply({args});' + '})()')


def mode(tree):
    return evaluate(tree, '(()=>{' + relay.PRO_CHAT_SURFACE_JS + 'return ensureProChatSurface();})()')


def test_captured_complete_final_assistant_and_wrapper_free_variant():
    tree = fixture('complete')
    result = current(tree)
    assert result['complete'] is True and result['n'] == len('Fixture assistant text')
    assert current(tree, 0, result['n']) == 'Fixture assistant text'
    active = copy.deepcopy(tree)
    active['children'].append({'tag':'button','attrs':{'aria-label':'Stop generating'},'children':[]})
    assert current(active)['reason'] == 'generation_active'
    # Counterexample mutation, NOT claimed as another live completed capture:
    # final assistant search-unit/heading survive while old wrappers are absent.
    for node in walk(tree):
        node.get('attrs', {}).pop('data-chatgpt-selection-message-id', None)
    assert current(tree)['complete'] is True
    assert current(tree, 0, 100) == 'Fixture assistant text'


def test_captured_in_progress_is_not_final_even_without_stop():
    pending = fixture('in_progress')
    assert current(pending)['complete'] is False
    assert current(pending)['reason'] == 'turn_incomplete'
    complete = fixture('complete')
    complete['children'][0]['children'].append(pending['children'][0]['children'][0])
    assert current(complete)['complete'] is False  # never select older complete turn
    for node in walk(pending):
        if 'data-talvt-turn-state' in node.get('attrs', {}):
            node['attrs']['data-talvt-turn-state'] = 'unrecognized'
    assert current(pending)['complete'] is False


def test_captured_chat_group_not_confused_with_other_buttons_and_work_refused():
    tree = fixture('chat_mode')
    assert mode(tree) == {'switched': False}
    tree['children'].append({'tag':'button','attrs':{},'children':[{'text':'Chat'}]})
    assert mode(tree) == {'switched': False}
    for node in walk(tree['children'][0]):
        if node.get('tag') == 'button':
            node['attrs']['aria-pressed'] = str(node['children'][0]['text'] == 'Work').lower()
    assert mode(tree)['error'] == 'CHAT_SURFACE_REFUSED'
    assert mode(tree)['reason'] == 'work_selected'
    hidden = fixture('chat_mode')
    hidden['children'][0]['attrs']['hidden']=''
    hidden['children'] += [{'tag':'button','attrs':{'aria-pressed':'true'},'children':[{'text':'Chat'}]},
                           {'tag':'button','attrs':{'aria-pressed':'false'},'children':[{'text':'Work'}]}]
    assert mode(hidden)['reason'] == 'mode_unverified'
    duplicate = fixture('chat_mode'); duplicate['children'] *= 2
    assert mode(duplicate)['reason'] == 'mode_ambiguous'


def test_pinned_renderer_never_substitutes_current_turn():
    tree = fixture('complete')
    expression = '(()=>{' + relay.CURRENT_REPLY_JS + 'return currentFinalReply(undefined,undefined,"selection_markdown");})()'
    assert evaluate(tree, expression)['complete'] is True
    for node in walk(tree):
        node.get('attrs', {}).pop('data-chatgpt-selection-message-id', None)
    assert current(tree)['complete'] is True
    assert evaluate(tree, expression)['reason'] == 'saved_renderer_unavailable'


class TestCurrentReaderTransport(binding.BindingTests):
    # Do not duplicate the inherited 200+ legacy tests in this focused file.
    __test__ = False


def test_real_reader_keeps_pending_reservation_and_never_reloads():
    case = TestCurrentReaderTransport(); case.setUp()
    try:
        pending = fixture('in_progress')
        original = case.browser.js
        def js(expression):
            if expression.startswith('(()=>{function currentFinalReply'):
                return evaluate(pending, expression)
            return original(expression)
        case.browser.js = js
        case.browser.pages['exact'] = {'url':binding.WANT, 'text':'interim commentary',
                                      'composer':'', 'messages':True, 'stop':False}
        case.job(tab='exact')
        for _ in range(3):
            case.clock += 150
            assert relay.poll('job', False, 150, 3600) == 3
            assert relay.job_state('job')['status'] == 'running'
        reply = relay.read_reply(binding.WANT, 'exact', reload=True)
        assert reply['text'] == '' and reply['state']['stop'] is True
        assert case.browser.navigations == [] and case.browser.created == []
        assert case.browser.sent == [] and case.browser.inserts == []
    finally:
        case.doCleanups()


def test_fresh_sender_waits_for_missing_mode_but_never_changes_work():
    case = TestCurrentReaderTransport(); case.setUp()
    try:
        original = case.browser.js; calls=[]
        mode_expr='(()=>{' + relay.PRO_CHAT_SURFACE_JS + 'return ensureProChatSurface();})()'
        def js(expression):
            if expression == mode_expr:
                calls.append(case.browser.clock)
                if len(calls) == 1:
                    return {'error':'CHAT_SURFACE_REFUSED','reason':'mode_missing'}
            return original(expression)
        case.browser.js=js
        result=relay.send_message('advisory')
        assert result.get('error') is None and result['sent'] is True
        assert len(calls) >= 2 and calls[1] > calls[0]
    finally:
        case.doCleanups()
