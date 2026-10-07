"""Observed effort-only UI must still prove GPT-6 family AND Pro."""
import json
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import gpt6pro_relay as relay


def test_effort_only_label_requires_exact_current_family_contract():
    assert 'function picker6ProLabel(' in relay.MODEL_PICKER_JS, 'Observed effort-only UI is unsupported'
    js = relay.MODEL_PICKER_JS
    harness = r'''
const attrs=o=>({getAttribute:k=>o[k]??null});
let label='Pro', family='GPT-6', checked='true', selected='true', explicit='false', effort='true', badge=true, count=1;
const control=attrs({'data-explicit-model':'false'});
const radio={innerText:'GPT-6',getAttribute:k=>k==='aria-checked'?checked:k==='data-model-selected'?selected:null};
const row={innerText:'Pro',closest:s=>({getAttribute:k=>explicit}),querySelector:s=>badge?{getAttribute:k=>effort}:null};
const view={querySelectorAll:s=>Array.from({length:count},()=>({...radio,innerText:family}))};
const check=()=>picker6ProLabel({...row,innerText:label},view);
const results=[check()];
family='GPT-5.6 Sol';results.push(check());family='GPT-6';checked='false';results.push(check());checked='true';
selected='false';results.push(check());selected='true';explicit='true';results.push(check());explicit='false';
effort='false';results.push(check());effort='true';count=2;results.push(check());count=1;badge=false;results.push(check());
label='Pro-ish';results.push(check());label='6 Pro';results.push(check());
console.log(JSON.stringify(results));
'''
    cp = subprocess.run(['node', '-e', js + harness], text=True, capture_output=True, check=True)
    assert json.loads(cp.stdout) == [True, False, False, False, False, False, False, False, False, True]
