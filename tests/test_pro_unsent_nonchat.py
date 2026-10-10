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
                 composer_empty=empty, user_count=users)
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
    import subprocess
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
            'cases':cases,'exact':expr,'preflight':expr,'prepare':expr}}),timeout=30)
    assert result.returncode==0,result.stderr
    actual=json.loads(result.stdout)['initial']['cases']
    assert actual[0]['preflight'] is True
    assert all(row['preflight'] is False for row in actual[1:])
