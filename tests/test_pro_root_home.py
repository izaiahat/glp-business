"""Offline tests for supported Home repair on new empty Codex root only."""
import json
from pathlib import Path
import subprocess
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import gpt6pro_relay as relay


def probe(**changes):
    config = dict(root=True, cloud=True, sol=True, picker=False, empty=True,
                  stop=False, messages=False, home_count=1, visible=True)
    config.update(changes)
    fixture = r'''
const c=CONFIG;
global.location={href:c.root?'https://chatgpt.com/':'https://chatgpt.com/c/live'};
const home={innerText:'Home',textContent:'Home',disabled:false,getClientRects:()=>c.visible?[1]:[],getBoundingClientRect:()=>({x:10,y:20,width:40,height:30})};
const editor={textContent:c.empty?'':'draft',innerText:c.empty?'':'draft'};
global.getComputedStyle=()=>({display:'block',visibility:'visible'});
global.document={title:'ChatGPT',body:{innerText:c.sol?'Cloud\nGPT-6.1 Sol':'ChatGPT'},
querySelector:(s)=>{
if(s.includes('Select ChatGPT model'))return c.picker?{}:null;
if(s.includes('Work in'))return c.cloud?{innerText:'Cloud'}:null;
if(s.includes('stop-button'))return c.stop?{}:null;
if(s.includes('data-message-author-role')||s.includes('data-chatgpt-selection-message-id'))return c.messages?{}:null;
if(s.includes('prompt-textarea'))return editor;
return null;},
querySelectorAll:(s)=>s==='button'?Array(c.home_count).fill(home):[]};
console.log(JSON.stringify(eval(EXPR)));
'''.replace('CONFIG', json.dumps(config)).replace('EXPR', json.dumps(relay.ROOT_CHAT_HOME_JS))
    return json.loads(subprocess.check_output(['node', '-e', fixture], text=True))


def test_observed_codex_root_gets_supported_home_geometry():
    assert probe() == {'x': 30, 'y': 35}


def test_never_normalize_conversation_generating_populated_or_ambiguous_surface():
    for change in ({'root':False}, {'cloud':False}, {'sol':False}, {'picker':True},
                   {'empty':False}, {'stop':True}, {'messages':True},
                   {'home_count':2}, {'home_count':0}, {'visible':False}):
        assert probe(**change) is None, change


def test_repair_is_single_new_target_action_before_unchanged_full_picker(monkeypatch):
    scripts=[]
    monkeypatch.setattr(relay,'bu_json',lambda s,**k:scripts.append(s) or {})
    relay.send_message('advisory')
    source=scripts[0]
    assert 'if not RETAINED and not home_repaired' in source
    assert source.index('home_repaired=True') < source.index('opened=_bound_js')
    assert "_binding_error(\"HUMAN_CHALLENGE\"" in source
    assert 'ROOT_CHAT_HOME' not in relay.BU_PRELUDE  # no generic tab navigation helper
    assert 'committed6ProMaximum' in source
    assert 'Network.setCookie' not in source
