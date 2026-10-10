"""Disposition-only native non-chat emptiness; sending gates stay strict."""
import ast
import copy
import json
from unittest import mock
from tests.test_gpt6pro_relay_binding import relay


def witness(stop=False, users=0, assistants=0, empty=True):
    state = dict(url='https://chatgpt.com/', ready_state='complete',
                 composer_present=True, assistant_count=assistants, last_len=0,
                 security_interstitial=False, human_challenge=False,
                 logged_out=False, rate_limited=False, stop=stop,
                 drive_reconnect=False, thinking_failed=False)
    probe = dict(url='https://chatgpt.com/', time_origin=12345.5,
                 composer_empty=empty, user_count=users, transcript_count=users+assistants, stop=stop)
    return dict(tab='fixture', state=state, first=probe, last=copy.deepcopy(probe), same_tabs=True)


def test_nonchat_disposition_uses_separate_reader_without_widening_send():
    scripts=[]
    with mock.patch.object(relay, 'bu_json', side_effect=lambda s, **kw:scripts.append(s) or witness()):
        assert relay._retained_empty_witness('fixture', 12345.5, disposition_only=True)
    tree=ast.parse(scripts[0])
    expr=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
              and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='extra')
    assert 'Work with ChatGPT' in expr
    assert 'Work with ChatGPT' not in relay.COMPOSER_TEXT_JS
    top_calls = [n for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.Import, ast.ImportFrom))]
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id in ('_navigate', 'goto_url', 'new_tab')
                   for block in top_calls for n in ast.walk(block))


def test_sent_or_generating_renderer_refuses_even_when_composer_empty():
    for changes in (dict(stop=True), dict(users=1), dict(assistants=1), dict(empty=False)):
        with mock.patch.object(relay,'bu_json',return_value=witness(**changes)):
            assert not relay._retained_empty_witness('fixture',12345.5, disposition_only=True)


def test_nonchat_native_dom_is_empty_only_for_exact_known_shape():
    # Use the existing native DOM harness; it executes the browser expression.
    scripts=[]
    with mock.patch.object(relay,'bu_json',side_effect=lambda s,**kw:scripts.append(s) or witness()):
        relay._retained_empty_witness('fixture',12345.5, disposition_only=True)
    tree=ast.parse(scripts[0])
    expr=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
              and isinstance(n.targets[0],ast.Name) and n.targets[0].id=='extra')
    import subprocess, os, tempfile
    from tests.test_gpt6pro_relay_binding import NATIVE_COMPOSER_FIXTURE
    # fixture's initial cases assert the disposition's own composer_empty value.
    html=('<div contenteditable="true" data-composer-markdown="" class="ProseMirror">'
          '<p data-empty-paragraph="true" data-placeholder="Work with ChatGPT" class="placeholder">'
          '<br class="ProseMirror-trailingBreak"></p></div>')
    cases=[{'name':'nonchat','mutate':''},
           {'name':'draft','mutate':'p.append(document.createTextNode("draft"))'},
           {'name':'space','mutate':'p.append(document.createTextNode(" "))'},
           {'name':'unknown','mutate':'p.setAttribute("data-placeholder","Other")'},
           {'name':'extra','mutate':'e.append(p.cloneNode(true))'}]
    result=subprocess.run(['node','-e',NATIVE_COMPOSER_FIXTURE],capture_output=True,text=True,
        input=json.dumps({'expressions':[expr], 'initial':{'html':html,'attributes':[],
            'cases':cases,'exact':expr,'preflight':expr,'prepare':expr}}),timeout=30,
        env={**os.environ, 'TMPDIR': tempfile.gettempdir()})
    assert result.returncode==0,result.stderr
    actual=json.loads(result.stdout)['initial']['cases']
    assert actual[0]['preflight'] is True
    assert all(row['preflight'] is False for row in actual[1:])


def _witness_script():
    scripts=[]
    with mock.patch.object(relay,'bu_json',side_effect=lambda s,**kw:scripts.append(s) or {}):
        relay._retained_empty_witness('fixture',12345.5,disposition_only=True)
    tree=ast.parse(scripts[0])
    state=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_state')
    start=next(i for i,n in enumerate(tree.body) if isinstance(n,ast.Assign) and
               isinstance(n.targets[0],ast.Name) and n.targets[0].id=='tab' and
               isinstance(n.value,ast.Constant) and n.value.value=='fixture')
    return state,ast.Module(body=tree.body[start:],type_ignores=[])


def _execute_witness_script(evaluator):
    import io
    from contextlib import redirect_stdout
    state,tail=_witness_script()
    ns={'json':json,'js':None,'list_tabs':lambda:[{'targetId':'fixture'}],
        '_select':lambda tab:None,'_bound_js':lambda tab,url,expr:evaluator(expr),
        '_binding_error':lambda *a,**kw:(_ for _ in ()).throw(AssertionError(a))}
    exec(compile(ast.Module(body=[state],type_ignores=[]),'<actual-state>','exec'),ns)
    with redirect_stdout(io.StringIO()) as out:
        exec(compile(tail,'<actual-witness-caller>','exec'),ns)
    return json.loads(out.getvalue().split('RELAY_JSON:',1)[1])


def test_native_temporal_stop_and_alternate_transcript_cannot_pass():
    """Execute actual emitted Python/JS, not a canned final witness answer."""
    import subprocess,os,tempfile
    from tests.test_gpt6pro_relay_binding import NATIVE_COMPOSER_FIXTURE
    expressions=[]
    _execute_witness_script(lambda expr:expressions.append(expr) or {})
    native=NATIVE_COMPOSER_FIXTURE.replace('if(f.picker){',r'''
if(f.snapshots){
 await js('document.body.innerHTML='+JSON.stringify(f.snapshots.html));
 output.snapshots=[];
 for(const p of f.snapshots.probes){if(p.mutate)await js(p.mutate);output.snapshots.push(await js(p.expression));}
 process.stdout.write(JSON.stringify(output));return;
}
if(f.picker){''')
    html=('<div contenteditable="true" data-composer-markdown="" class="ProseMirror">'
          '<p data-empty-paragraph="true" data-placeholder="Work with ChatGPT" class="placeholder">'
          '<br class="ProseMirror-trailingBreak"></p></div>')
    for kind in ('idle','late_stop','late_assistant','stable_selection'):
        probes=[];state_seen=False
        for expr in expressions:
            mutate=''
            if kind=='late_stop' and state_seen:
                mutate="document.body.insertAdjacentHTML('beforeend','<button data-testid=stop-button>Stop</button>')"
            if kind=='late_assistant' and state_seen:
                mutate="document.body.insertAdjacentHTML('beforeend','<div data-message-author-role=assistant>answer</div>')"
            if kind=='stable_selection' and not probes:
                mutate="document.body.insertAdjacentHTML('beforeend','<div data-chatgpt-selection-message-id=alternate>answer</div>')"
            probes.append({'expression':expr,'mutate':mutate})
            state_seen=state_seen or 'const stop=' in expr
        cp=subprocess.run(['node','-e',native],capture_output=True,text=True,timeout=30,
            input=json.dumps({'expressions':expressions,'snapshots':{'html':html,'probes':probes}}),
            env={**os.environ,'TMPDIR':tempfile.gettempdir()})
        assert cp.returncode==0,cp.stderr
        observed=json.loads(cp.stdout)['snapshots']
        for obj in observed:
            obj['url']='https://chatgpt.com/'
            if 'time_origin' in obj:obj['time_origin']=12345.5
        values=iter(observed)
        result=_execute_witness_script(lambda expr:next(values))
        with mock.patch.object(relay,'bu_json',return_value=result):
            accepted=relay._retained_empty_witness('fixture',12345.5,disposition_only=True)
        assert accepted is (kind=='idle'),kind

