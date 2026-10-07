"""Offline binding regressions: execute the real browser-use scripts against a strict fake CDP session."""
import ast
import importlib.util
import io
import json
import re
import select
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

RELAY_PATH = Path(__file__).resolve().parents[1] / "scripts/gpt6pro_relay.py"
spec = importlib.util.spec_from_file_location("relay_binding_test_subject", RELAY_PATH)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)
REAL_BU = relay.bu
REAL_CLEANUP_TABS = relay.cleanup_tabs
REAL_DRIVE_SUBFOLDER = relay.drive_subfolder
REAL_SUBPROCESS_RUN = subprocess.run
HOME = "https://chatgpt.com/"
SIBLING = HOME + "c/11111111-1111-1111-1111-111111111111"
WANT = HOME + "c/22222222-2222-2222-2222-222222222222"


class FakeCDP:
    def __init__(self):
        self.pages = {"sibling": {"url": SIBLING, "text": "sibling answer", "composer": "", "messages": True, "stop": False}}
        self.history = {WANT: "wanted answer", SIBLING: "sibling answer"}
        self.active = "sibling"  # wrapper JS page session
        self.sessions = {}
        self.wrong_page_session = False
        self.wrong_input_session = False
        self.browser_target = "browser-target"  # Target.* ignores page session
        self.created = []
        self.inserts = []
        self.sent = []
        self.reads = []
        self.navigations = []
        self.current_tab_calls = 0
        self.home_redirect = None
        self.reopen_redirect = None
        self.after_send_url = None
        self.redirect_on_slice = False
        self.wrong_switch = False
        self.defer_text = False
        self.omit_undefined_value = False
        self.pro_only_trigger = False
        self.medium_trigger = False
        self.trigger_label: str | None = None
        self.slider_works = True
        self.selected_model = '6 Pro'
        self.markdown_composer_only = False
        self.blank_paragraph = ''
        self.aria_send_only = False
        self.composer_available_at = 0
        self.composer_delay = 0
        self.first_composer_probe = None
        self.clock = 0
        self.app_ready_at = 0
        self.app_samples = []
        self.app_overrides = {}
        self.on_run = lambda: None

    def list_tabs(self):
        return [{"targetId": tid, "url": p["url"]} for tid, p in self.pages.items()]

    def switch_tab(self, tid):
        sid = 'session-' + tid
        self.sessions[sid] = 'sibling' if self.wrong_input_session else tid
        if not self.wrong_switch and not self.wrong_page_session:
            self.active = tid
        return sid

    def current_tab(self):
        self.current_tab_calls += 1
        return {"targetId": "sibling"}  # reproduce the stale daemon result, not the selected CDP target

    def goto_url(self, url):
        self.navigations.append((self.active, url))
        self.pages[self.active]["url"] = self.reopen_redirect or url

    def cdp(self, method, session_id=None, **params):
        if method == 'Target.attachToTarget':
            sid = 'witness-' + params['targetId']
            self.sessions[sid] = params['targetId']
            return {'sessionId': sid}
        if method == 'Target.detachFromTarget':
            self.sessions.pop(params['sessionId'])
            return {}
        if method == 'Runtime.evaluate':
            previous = self.active
            self.active = self.sessions[session_id] if session_id else self.active
            try:
                value = self.js(params['expression'])
                # CDP omits `value` for JavaScript undefined, including focus().
                if self.omit_undefined_value and value is None:
                    return {'result': {'type': 'undefined'}}
                return {'result': {'value': value}}
            finally:
                self.active = previous
        if method == 'Page.navigate':
            previous = self.active
            self.active = self.sessions[session_id]
            try:
                page = self.pages[self.active]
                url = params['url']
                page['url'] = (self.home_redirect if url == HOME else self.reopen_redirect) or url
                page['text'] = self.history.get(page['url'], '')
                page['messages'] = bool(page['text'])
                return {}
            finally:
                self.active = previous
        if method == "Target.createTarget":
            tid = f"new-{len(self.created) + 1}"
            requested = params["url"]
            url = (self.home_redirect if requested == HOME else self.reopen_redirect) or requested
            text = self.history.get(url, "")
            self.pages[tid] = {"url": url, "text": text, "composer": self.blank_paragraph, "placeholder": bool(self.blank_paragraph), "messages": bool(text), "stop": False}
            self.created.append((tid, requested))
            return {"targetId": tid}  # deliberately does not change self.active
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": params.get('targetId', self.browser_target)}}
        addressed = self.sessions[session_id] if session_id else self.active
        page = self.pages[addressed]
        if method == "Input.insertText":
            self.inserts.append((addressed, params["text"]))
            if page.pop('placeholder', False): page['composer'] = ''
            page["composer"] += params["text"]
            return {}
        if method == 'Input.dispatchMouseEvent':
            if params['x'] == 50 and params['type'] == 'mousePressed':
                page['picker_open'] = False
            elif params['x'] == 1100 and self.slider_works and params['type'] == 'mouseReleased':
                page['picker_power'] = 4
                page['selected_effort'] = 'maximum'
            return {}
        if method == "Input.dispatchKeyEvent":
            if params["key"] == "Backspace" and params["type"] == "keyDown":
                page["composer"] = ""
            return {}
        raise AssertionError(f"unexpected CDP command: {method}")

    def eval_retained_js(self, expression):
        page = self.pages[self.active]
        source = '''const p=JSON.parse(process.argv[1]);let clicks=0;
            const editor={innerText:p.composer,getClientRects:()=>[1],tagName:'DIV',
              getAttribute:k=>k==='contenteditable'?'true':null,
              classList:{contains:c=>c==='ProseMirror'},
              childNodes:p.composer.split('\\n').map(t=>({nodeType:1,tagName:'P',attributes:[],
                childNodes:t?[{nodeType:3,data:t}]:[{nodeType:1,tagName:'BR',className:'ProseMirror-trailingBreak',attributes:['class']}]}))};
            const button={disabled:false,click:()=>clicks++};
            const model={innerText:p.model||'6 Pro'};
            const trigger={innerText:p.model||'6 Pro',getClientRects:()=>[1],parentElement:null,
              getAttribute:k=>({'data-selected-reasoning-effort':p.selected_effort||'medium',
                'aria-expanded':'false','data-codex-intelligence-trigger':'true','data-composer-navigation-target':'reasoning'}[k]||null)};
            const document={readyState:'complete',title:p.challenge?'Just a moment...':'ChatGPT',
              body:{innerText:p.challenge?'Checking your browser':'ChatGPT'},
              querySelector:s=>s.includes('prompt-textarea')?editor:
                s.includes('send-button')?button:null,
              querySelectorAll:s=>s==='button'?[model]:s==='button[aria-label="Select ChatGPT model"]'?[trigger]:[],};
            const performance={timeOrigin:p.origin||12345.5};
            const getComputedStyle=()=>({display:'block',visibility:'visible'});
            ''' + MODEL_OWNER_FIXTURE + '''
            attachModelOwner(trigger,{slug:p.model&&p.model!=='6 Pro'?'other':'gpt-6-pro',
                thinkingEffort:p.selected_effort==='maximum'?null:'standard'});
            const result=''' + expression + ''';
            console.log(JSON.stringify({result,clicks}));'''
        run = REAL_SUBPROCESS_RUN(['node', '-e', source, json.dumps(page)],
                                 capture_output=True, text=True, check=True)
        outcome = json.loads(run.stdout)
        if outcome['clicks']:
            # Apply FakeCDP's transition only after the actual guard clicked.
            self.js("(()=>{const b=document.querySelector('button[data-testid=\\\"send-button\\\"]');if(!b||b.disabled)return false;b.click();return true;})()")
        return outcome

    def js(self, expression):
        page = self.pages[self.active]
        if self.redirect_on_slice and ".innerText.slice(" in expression:
            page.update(url=SIBLING, text=self.history[SIBLING])
            self.redirect_on_slice = False
        if expression.startswith("(()=>{const here=location.href;"):
            encoded = expression.split("!==", 1)[1].split(")return {binding_error", 1)[0]
            want = json.loads(encoded)
            inner = expression.split("return {url:here,value:(", 1)[1][:-len(")};})()")]
            here = page["url"]
            if re.split(r"[?#]", here)[0].rstrip("/") != want:
                return {"binding_error": True, "url": here}
            if self.omit_undefined_value and inner == "document.querySelector('#prompt-textarea').focus()":
                self.js(inner)
                return {"url": here}  # JSON serialization drops the undefined nested value
            return {"url": here, "value": self.js(inner)}
        if expression.startswith('window.__relay_session_nonce='):
            page['nonce'] = json.loads(expression.split('=', 1)[1])
            return page['nonce']
        if expression == 'window.__relay_session_nonce':
            return page.get('nonce')
        if expression == 'document.readyState':
            return 'complete'
        if expression == "location.href":
            return page["url"]
        if expression.startswith("location.href="):
            self.goto_url(json.loads(expression.split("=", 1)[1]))
            return page["url"]
        if expression.startswith('(()=>{const retainedOrigin='):
            return self.eval_retained_js(expression)['result']
        if expression.startswith(('(()=>{const prefix=', '(()=>{const expected=')):
            # Transport fixture only; NativeComposerTests executes the actual JS
            # and DOM selection/Input behavior independently.
            expected, _ = json.JSONDecoder().raw_decode(expression.split('const ', 1)[1].split('=', 1)[1])
            return ('' if page.get('placeholder') else page['composer']) == expected
        if "const mb=" in expression:
            model = None if self.pro_only_trigger or self.medium_trigger else '6 Pro'
            failure_location = page.get('failure_location', 'activity-header')
            failure_selector = ('main.querySelectorAll' in expression and
                                ('div[class*="group/activity-header"] span' in expression if failure_location == 'activity-header'
                                 else "span, button')" in expression or "main.querySelectorAll('button')" in expression))
            ready = self.clock >= self.app_ready_at
            self.app_samples.append((self.active, self.clock))
            return {"url": page["url"], "model": model if ready else None,
                    "title": "ChatGPT" if ready else "Just a moment...",
                    "ready_state": "complete", "composer_present": ready,
                    "model_trigger": ('Medium' if self.medium_trigger else 'Pro') if ready else None,
                    "security_interstitial": not ready, "human_challenge": False,
                    "logged_out": False, "rate_limited": False,
                    "drive_reconnect": False, "stop": page["stop"], "send": True,
                    "thinking_failed": bool(page.get("thinking_failed", False) and failure_selector),
                    "assistant_count": int(bool(page["text"])), "last_len": len(page["text"]),
                    **self.app_overrides}
        if "user_identity_matched" not in expression and ("div.text-size-chat.whitespace-pre-wrap" in expression or '[data-message-author-role=user]' in expression):
            identity_selector = ('[data-message-author-role=user]' in expression if page.get('identity_location') == 'user-role-only'
                                 else 'div.text-size-chat.whitespace-pre-wrap' in expression or '[data-message-author-role=user]' in expression)
            return bool(identity_selector and page.get("identity_matched", False))
        if 'function pickerActionable' in expression:
            # Transport state only; NativeComposerTests runs the actual DOM gates.
            committed_model = self.app_overrides.get('model', self.selected_model)
            committed = page.get('selected_effort') == 'maximum' and committed_model == '6 Pro'
            if 'r.right-13' in expression:
                return {'x': 1100, 'y': 470}
            if expression.endswith('return full6ProMaximum();})()'):
                return committed_model == '6 Pro' and page.get('picker_power') == 4
            if "const b=pickerTrigger();if(!b)return false;if(b.getAttribute('aria-expanded')!=='true')b.click();return true;" in expression:
                page['picker_open'] = True
                return True
            if 'const r=b.getBoundingClientRect(),x=' in expression:
                return {'x': 50, 'y': 15} if page.get('picker_open') and (not page['composer'] or page.get('placeholder')) and not page['messages'] else None
            if "return !!b&&b.getAttribute('aria-expanded')==='false'" in expression:
                return not page.get('picker_open', True) and committed
            if expression.endswith('return committed6ProMaximum();})()'):
                return committed
            if not committed:
                return False
        if 'return b?b.innerText.trim():null' in expression:
            if self.clock < self.app_ready_at:
                return None
            return self.trigger_label or ('Medium' if self.medium_trigger else ('Pro' if self.pro_only_trigger else None))
        if 'r.right-13' in expression:
            return {'x': 1100, 'y': 470} if self.medium_trigger else None
        if 'function pickerActionable' not in expression and 'data-model-picker-view-toggle' in expression and (self.pro_only_trigger or self.medium_trigger):
            return self.selected_model == '6 Pro' and (not self.medium_trigger or page.get('picker_power') == 4)
        if 'function pickerActionable' not in expression and 'Select ChatGPT model' in expression and (self.pro_only_trigger or self.medium_trigger):
            # React commits aria-expanded after the click, not synchronously.
            return "b.click();return true;" in expression
        if expression == "!!document.querySelector('[data-message-author-role]')":
            return page["messages"]
        if "dataset.relayId" in expression:
            if self.first_composer_probe is None:
                self.first_composer_probe = self.clock
            if self.clock < self.first_composer_probe + self.composer_delay:
                return None
            if self.markdown_composer_only and '[data-composer-markdown][contenteditable=true]' not in expression:
                return None
            return "composer-" + self.active
        if "#prompt-textarea" in expression and "innerText.trim().length" in expression:
            return bool(page['composer'].strip())
        if "#prompt-textarea" in expression and "innerText.length" in expression:
            if self.markdown_composer_only and '[data-composer-markdown][contenteditable=true]' not in expression:
                return 0
            return len(page["composer"])
        if expression == "document.querySelector('#prompt-textarea').focus()":
            return None
        if "e.focus();return document.activeElement===e;" in expression:
            return not self.markdown_composer_only or '[data-composer-markdown][contenteditable=true]' in expression
        if "b.click();return true;" in expression:
            if self.aria_send_only and 'button[type=' not in expression:
                return False
            self.sent.append((self.active, page["composer"]))
            if self.after_send_url:
                page["url"] = self.after_send_url
            elif page["url"] == HOME:
                page["url"] = HOME + f"c/00000000-0000-0000-0000-{len(self.sent):012x}"
            page["messages"] = True
            page["text"] = "" if self.defer_text else "reply: " + page["composer"]
            page["stop"] = self.defer_text
            self.history[page["url"]] = page["text"]
            page["composer"] = ""
            return True
        if "data-chatgpt-selection-message-id] [data-markdown-text-style" in expression:
            content = page.get('selection_text', '')
            if '.innerText.slice(' in expression:
                match = re.search(r"slice\((\d+),(\d+)\)", expression)
                return content[int(match[1]):int(match[2])]
            return len(content)
        if "data-message-author-role=assistant" in expression:
            if ".innerText.slice(" in expression:
                match = re.search(r"slice\((\d+),(\d+)\)", expression)
                if not match:
                    raise AssertionError(expression)
                self.reads.append(self.active)
                return page["text"][int(match[1]):int(match[2])]
            if ".innerText.length" in expression:
                return len(page["text"])
        raise AssertionError(f"unexpected JS expression: {expression}")

    def sleep(self, seconds):
        self.clock += seconds

    def run(self, script, timeout=300):
        self.on_run()
        clock = types.ModuleType("time")
        clock.sleep = self.sleep
        clock.time = lambda: self.clock
        clock.monotonic = lambda: self.clock
        env = {"cdp": self.cdp, "js": self.js, "list_tabs": self.list_tabs, "switch_tab": self.switch_tab,
               "current_tab": self.current_tab, "goto_url": self.goto_url, "wait_for_load": lambda: None,
               "activate_tab": lambda tid: None, "close_tab": lambda tid: self.pages.pop(tid)}
        out = io.StringIO()
        with mock.patch.dict(sys.modules, {"time": clock}), redirect_stdout(out):
            try:
                exec(script, env)
            except SystemExit as e:
                if e.code != 0:
                    raise
        return out.getvalue()


import pytest


@pytest.fixture(autouse=True)
def deny_network():
    with mock.patch('socket.socket.connect', side_effect=AssertionError('NETWORK FORBIDDEN IN RELAY TESTS')), mock.patch('socket.create_connection', side_effect=AssertionError('NETWORK FORBIDDEN IN RELAY TESTS')):
        yield


class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cfg = {"accounts": [{"label": "account-a", "cdp_port": 9340, "weekly_messages": 100}],
                    "job_classes_allowed": ["review"], "ledger": "ledger.jsonl",
                    "envelope": {'max_concurrent_chats_per_ip': 6, 'max_concurrent_chats_per_account': 3,
                                 'min_seconds_between_submits': 60, 'poll_interval_s': 150, 'poll_stagger_s': 20},
                    "drive": {"inbox_id": "inbox", "outbox_id": "outbox"}}
        self.clock = 2000000000.0
        p = mock.patch.object(relay.time, 'time', side_effect=lambda: self.clock)
        p.start(); self.addCleanup(p.stop)
        self.browser = FakeCDP()
        self.svc = mock.Mock()
        self.svc.files.return_value.list.return_value.execute.return_value = {"files": []}
        for patch in (mock.patch.object(relay, "JOBS", self.root / "jobs"),
                      mock.patch.object(relay, "BUSINESS", self.root),
                      mock.patch.object(relay, "BU_ENV", dict(relay.BU_ENV)),
                      mock.patch.object(relay, "load_config", return_value=self.cfg),
                      mock.patch.object(relay, "bu", side_effect=self.browser.run),
                      mock.patch.object(relay, "drive", return_value=self.svc),
                      mock.patch.object(relay, "drive_subfolder", return_value="folder"),
                      mock.patch.object(relay, "drive_upload", return_value="uploaded"),
                      mock.patch.object(relay, "close_tab_quiet")):
            patch.start()
            self.addCleanup(patch.stop)
        relay.use_account(self.cfg["accounts"][0])

    def job(self, name="job", url=WANT, tab="sibling", **extra):
        relay.job_dir(name)
        st = {"job": name, "account": "account-a", "status": "running", "conversation_url": url, "tab": tab,
              "binding_version": 2, "conversation_id": url.rsplit('/', 1)[1] if url else None,
              "submitted": relay.now_iso(), **extra}
        relay.job_write(name, st)
        return st

    def test_postinsert_guard_terminal_preserves_unknown_and_draft(self):
        """Exact custody branch, never an error-string or empty-page waiver."""
        sha = lambda b: relay.hashlib.sha256(b).hexdigest()
        job = relay.CONSUMED_CANARY['job']; tab = 'held-draft'; origin = 12345.0
        d = relay.job_dir(job); (d / 'drive-input').mkdir()
        (d / 'PROMPT.md').write_bytes(b'fixture'); (d / 'drive-input/PROMPT.md').write_bytes(b'folded')
        (d / 'old-first.json').write_bytes(b'first-backup'); (d / 'old-second.json').write_bytes(b'second-backup')
        held = {'job':job, 'account':'account-a', 'job_class':'review', 'status':'failed',
            'reason':'RETAINED_RESUME_UNCERTAIN', 'typed':True, 'sent':'UNKNOWN', 'retry_blocked':True,
            'error':{'tab':tab}, 'resume_error':{'error':'retained_send_guard_failed', 'guard':'composer',
                'typed':True, 'sent':'UNKNOWN', 'tab':tab},
            **{k:'fixture-'+k for k in relay.PROVENANCE_FIELDS}}
        held['input_sha256'] = sha(json.dumps([('PROMPT.md',sha(b'fixture'))],separators=(',',':')).encode())
        message = relay._failed_generation_identity(held).encode(); (d/'MESSAGE.md').write_bytes(message)
        manifest = {k:held[k] for k in (*relay.PROVENANCE_FIELDS,'job','job_class')}
        manifest.update(attach=[],drive_attach=[]); (d/'MANIFEST.json').write_text(json.dumps(manifest))
        relay.job_write(job,held); original=(d/'JOB.json').read_bytes()
        for event in ('submitted','retained_resume','canary_forward_recovery'):
            relay.ledger_append(self.cfg,{'job':job,'account':'account-a','event':event})
        ledger=relay.ledger_path(self.cfg); lb=ledger.read_bytes()
        proof=self.root/'proof.txt'; proof.write_bytes(b'historical reviewed source and execution fixture')
        c={'job':job,'held':sha(original),'message':sha(message),
            'manifest':sha((d/'MANIFEST.json').read_bytes()),'drive_prompt':sha(b'folded'),
            'ledger_lines':tuple(map(sha,lb.splitlines())), 'tab':tab,'time_origin':origin,
            'postinsert_guard':True,'draft_sha256':sha(b'unchanged nonmatching draft'),
            'backups':{'old-first.json':sha(b'first-backup'),'old-second.json':sha(b'second-backup')},
            'proof_files':{'proof.txt':sha(proof.read_bytes())}}
        report='facts.json'; rp=self.root/report
        facts={'job':job,'disposition':'terminal_unsent_postinsert_guard','job_sha256':c['held'],
            'typed':True,'sent':'UNKNOWN','submission_bound':False,'draft_sha256':c['draft_sha256'],
            'ledger_preimage_sha256':sha(lb),'ledger_preimage_bytes':len(lb)}
        rp.write_text(json.dumps({'jobs':[facts]}))
        def call(*args):
            with mock.patch.object(sys,'argv',['relay','reconcile-unsent-model','--job',job,'--report',report,*args]), redirect_stdout(io.StringIO()):
                return relay.main()
        with mock.patch.object(relay,'TERMINAL_GUARD_REFUSED',c,create=True), \
             mock.patch.object(relay,'_retained_draft_witness',return_value=True,create=True) as witness, \
             mock.patch.object(relay,'_legacy_live_jobs',return_value=set()), \
             mock.patch.object(relay.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)), \
             mock.patch.object(relay,'bu',side_effect=AssertionError('NO OTHER BROWSER')), \
             mock.patch.object(relay,'cleanup_tabs',side_effect=AssertionError('NO CLEANUP')):
            self.assertEqual(call(),0)
            self.assertEqual((d/'JOB.json').read_bytes(),original); self.assertEqual(ledger.read_bytes(),lb)
            witness.return_value=False
            with self.assertRaises(SystemExit):call()
            witness.return_value=True
            proof.write_bytes(b'drift')
            with self.assertRaises(SystemExit):call()
            proof.write_bytes(b'historical reviewed source and execution fixture')
            # Refuse every independent custody mutation before any terminal write.
            changes = [(d/'PROMPT.md',b'packet drift'),(d/'old-first.json',b'backup drift'),
                       (rp,b'{"jobs":[]}'),(ledger,lb+b'not-json\n'),
                       (ledger,lb+json.dumps({'job':job,'event':'submission_bound'}).encode()+b'\n'),
                       (d/'JOB.json',json.dumps({**held,'resume_error':{'error':'timeout'}}).encode()),
                       (d/'JOB.json',json.dumps({**held,'sent':True}).encode())]
            for path, changed in changes:
                with self.subTest(custody=path.name):
                    old=path.read_bytes();path.write_bytes(changed)
                    with self.assertRaises(SystemExit):call()
                    self.assertFalse((d/('JOB.pre-terminal-unsent-'+c['held']+'.json')).exists())
                    path.write_bytes(old)
            with self.assertRaises(SystemExit):call('--apply','--authorize-preimage','0'*64)
            self.assertEqual(call('--apply','--authorize-preimage',c['held']),0)
            terminal=relay.job_state(job)
            self.assertEqual({k:v for k,v in terminal.items() if k!='terminal_unsent'},held)
            self.assertTrue(relay._verified_terminal_unsent(terminal))
            self.assertEqual(relay.active_registry(),[])
            self.assertIn(tab,relay.protected_tabs({'job':'different','account':'account-a'}))
            with self.assertRaisesRegex(SystemExit,'REFUSED_DUPLICATE'):relay.cmd_submit(self.args(job))
            with self.assertRaises(SystemExit):call('--apply','--authorize-preimage',c['held'])
            applied=ledger.read_bytes();ledger.write_bytes(b'\n'.join(applied.splitlines()[:-1])+b'\n')
            self.assertFalse(relay._verified_terminal_unsent(terminal))
            self.assertEqual(len(relay.active_registry()),1)
            ledger.write_bytes(applied);relay.job_write(job,{**terminal,'sent':False})
            self.assertFalse(relay._verified_terminal_unsent({**terminal,'sent':False}))

    def test_populated_witness_actual_emitted_fail_closed(self):
        """Execute emitted witness Python; browser payloads are inert boundary fixtures."""
        import copy
        sha = lambda b: relay.hashlib.sha256(b).hexdigest()
        logical = 'x' * 13323; digest = sha(logical.encode()); scripts = []
        with mock.patch.object(relay, 'bu_json', side_effect=lambda s: scripts.append(s) or {}):
            self.assertFalse(relay._retained_draft_witness('held',12345.0,digest))
        generated=scripts[0][len(relay.BU_PRELUDE):]
        compile(generated,'<actual-passive-witness>','exec')
        base={'url':HOME,'origin':12345.0,'logical':logical,'html':'unchanged','innerText':logical,
              'users':0,'assistants':0}
        state={'ready_state':'complete','composer_present':True,**{k:False for k in
               ('logged_out','stop','drive_reconnect','rate_limited','security_interstitial','human_challenge','thinking_failed')}}
        cases={'positive':None,'origin':('origin',0),'url':('url',WANT),'rich':('logical',None),
               'same_length_change':('logical','y'*13323),'users':('users',1),'assistants':('assistants',1)}
        cases.update({key:(key,True) for key in state if key not in ('ready_state','composer_present')})
        cases.update({'not_ready':('ready_state','loading'),'no_composer':('composer_present',False),
                      'torn':None,'inventory_drift':None,'missing_target':None})
        for name,change in cases.items():
            with self.subTest(case=name):
                a=copy.deepcopy(base);s=copy.deepcopy(state);calls=[]
                if change:
                    target=s if change[0] in s else a;target[change[0]]=change[1]
                def tabs():
                    calls.append('tabs')
                    return [{'targetId':'other' if name=='missing_target' else 'held'}]+(
                        [{'targetId':'new'}] if name=='inventory_drift' and calls.count('tabs')>1 else [])
                observations=iter([a,{**a,'html':'changed'} if name=='torn' else copy.deepcopy(a)])
                def bound(tab,url,expr):
                    self.assertEqual((tab,url),('held',HOME))
                    self.assertIn(relay.COMPOSER_TEXT_JS,expr)
                    return next(observations)
                env={'json':json,'list_tabs':tabs,'_select':lambda t:calls.append('select'),
                     '_bound_js':bound,'_state':lambda probe:s,
                     '_binding_error':lambda reason:(_ for _ in ()).throw(RuntimeError(reason))}
                if name=='missing_target':
                    with self.assertRaisesRegex(RuntimeError,'retained_target_missing'):exec(generated,env)
                else:
                    with redirect_stdout(io.StringIO()) as output:exec(generated,env)
                    self.assertEqual(json.loads(output.getvalue().split('RELAY_JSON:')[1]),{'valid':name=='positive'})
                self.assertTrue(all(x in ('tabs','select') for x in calls))

    def test_nested_custody_targets_protected_without_admission_waiver(self):
        """A failed proof may not make any custody-attributed draft selectable."""
        held = self.job('held', url=None, tab=None, status='failed',
            typed=True, sent='UNKNOWN', retry_blocked=True,
            error={'tab': 'initial-held'}, resume_error={'tab': 'resume-held'},
            terminal_unsent={'retained_tab': 'unverified-proof-held'})
        current = self.job('current', url=WANT, tab='current-tab')
        self.assertEqual(set(relay.protected_tabs(current)),
            {'initial-held', 'resume-held', 'unverified-proof-held'})
        self.assertIn('held', [r['job'] for r in relay.active_registry()])
        for tab in relay.protected_tabs(current):
            with self.subTest(tab=tab), self.assertRaisesRegex(SystemExit, 'TAB_COLLISION'):
                relay.require_binding(current, {'tab': tab, 'state': {'conversation_url_now': WANT}})
        self.assertEqual(relay.job_state('held'), held)
        with self.assertRaisesRegex(SystemExit, 'BINDING_UNVERIFIED'):
            relay.cmd_submit(self.args('fresh'))

    def test_drive_folder_timeout_registered_cli_one_shot(self):
        """Actual submit CLI timeout-after-create, then same-reservation recovery CLI."""
        import hashlib
        sha = lambda b: hashlib.sha256(b).hexdigest()
        job = relay.DRIVE_TIMEOUT_PR60['job']
        self.cfg['job_classes_allowed'].append('independent-review')
        d = relay.job_dir(job)
        prompt = d / 'PROMPT.md'
        prompt.write_text('# retained prompt\n')
        (d / 'drive-input').mkdir()
        files = [d / f'DRIVE-{i:02}.txt' for i in range(27)]
        for i, p in enumerate(files):
            p.write_text(f'retained attachment {i}\n')
        manifest = {'job': job, 'job_class': 'independent-review', 'kind': 'freeform', 'attach': [],
                    'drive_attach': [str(Path('/home/ubuntu/business/reports/ops/gpt6pro-relay/jobs') / job / p.name)
                                     for p in files],
                    **{k: 'fixture-' + k for k in relay.PROVENANCE_FIELDS}}
        (d / 'MANIFEST.json').write_text(json.dumps(manifest))
        folder_id = 'retained-folder-id'
        folder = {'id': folder_id, 'name': job, 'mimeType': 'application/vnd.google-apps.folder'}
        created = []
        def listing(**kw):
            q = kw['q']
            return types.SimpleNamespace(execute=lambda: {'files':
                ([folder] if created else []) if "mimeType = 'application/vnd.google-apps.folder'" in q else []})
        def create(**kw):
            created.append(kw)
            return types.SimpleNamespace(execute=lambda: (_ for _ in ()).throw(TimeoutError('after create')))
        self.svc.files.return_value.list.side_effect = listing
        self.svc.files.return_value.create.side_effect = create
        args = ['gpt6pro_relay.py', 'submit', '--job', job, '--account', 'account-a',
                '--job-class', 'independent-review', '--prompt', str(prompt), '--prompt-on-drive',
                '--drive-attach', *map(str, files)]
        cli = ['gpt6pro_relay.py', 'resume-drive-timeout', '--job', job]
        with mock.patch.object(relay, 'drive_subfolder', wraps=REAL_DRIVE_SUBFOLDER), \
             mock.patch.object(relay, 'cleanup_tabs', return_value=0), \
             mock.patch.object(sys, 'argv', args), self.assertRaisesRegex(TimeoutError, 'after create'):
            relay.main()
        self.assertEqual(len(created), 1)
        self.assertEqual(len(self.browser.sent), 0)
        held = (d / 'JOB.json').read_bytes()
        message = (d / 'MESSAGE.md').read_bytes()
        st = json.loads(held)
        self.assertEqual((st['status'], st['typed'], st['sent'], st['retry_blocked']),
                         ('submitting', 'UNKNOWN', 'UNKNOWN', True))
        self.assertEqual(len(relay.ledger_rows(self.cfg)), 1)
        incident = {'job_sha256': sha(held), 'message_sha256': sha(message),
                    'reservation_ledger_line': 1, 'submission_bound_count': 0,
                    'fully_paginated_exact_inbox_query': True,
                    'drive_inbox_folders': [{'folder_id_sha256': sha(folder_id.encode()),
                                             'file_count': 0, 'media': []}]}
        rp = self.root / 'reports/ops/glp96-current-integration-pro-review-20260928'
        rp.mkdir(parents=True)
        (rp / 'SUBMISSION-INCIDENT.json').write_text(json.dumps(incident))
        (rp / '20260928T011108Z-submit.json').write_text(json.dumps(
            {'returncode': 1, 'stderr': 'TimeoutError in _cmd_submit in drive_subfolder'}))
        c = {'job': job, 'job_sha256': sha(held), 'message_sha256': sha(message),
             'manifest_sha256': sha((d / 'MANIFEST.json').read_bytes()),
             'prompt_sha256': sha(prompt.read_bytes()),
             'incident_sha256': sha((rp / 'SUBMISSION-INCIDENT.json').read_bytes()),
             'submit_sha256': sha((rp / '20260928T011108Z-submit.json').read_bytes()),
             'folder_id_sha256': sha(folder_id.encode()), 'input_sha256': st['input_sha256'],
             'reservation_ledger_line': 1, 'account': 'account-a'}
        def call(*extra):
            with mock.patch.object(sys, 'argv', cli + list(extra)), redirect_stdout(io.StringIO()) as out:
                return relay.main(), out.getvalue()
        with mock.patch.object(relay, 'DRIVE_TIMEOUT_PR60', c), \
             mock.patch.object(relay, '_legacy_live_jobs', return_value=set()), \
             mock.patch.object(relay.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)), \
             mock.patch.object(relay, 'cleanup_tabs', side_effect=AssertionError('no cleanup')), \
             mock.patch.object(relay, 'drive_subfolder', side_effect=AssertionError('no second folder create')), \
             mock.patch.object(relay, 'drive_upload', return_value='uploaded') as upload:
            original_ledger = relay.ledger_path(self.cfg).read_bytes()
            self.assertIn('WOULD_RESUME_DRIVE_FOLDER', call()[1])
            self.assertEqual((d / 'JOB.json').read_bytes(), held)
            self.assertEqual(relay.ledger_path(self.cfg).read_bytes(), original_ledger)
            self.assertEqual(upload.call_count, 0)
            self.assertEqual(len(self.browser.sent), 0)
            self.clock += 61  # make pacing eligible before every mutation refusal
            backup = d / ('JOB.pre-drive-timeout-' + sha(held) + '.json')
            def refused(label, reason, modify, restore=lambda: None, default=False):
                with self.subTest(label=label):
                    modify()
                    try:
                        before = relay.ledger_path(self.cfg).read_bytes()
                        job_before = (d / 'JOB.json').read_bytes()
                        attempts = ((), ('--authorize-preimage', sha(held))) if default else (('--authorize-preimage', sha(held)),)
                        for extra in attempts:
                            with self.assertRaises(SystemExit) as cm:
                                call(*extra)
                            self.assertEqual(str(cm.exception), 'DRIVE_RESUME_REFUSED_' + reason)
                            self.assertEqual((d / 'JOB.json').read_bytes(), job_before)
                            self.assertEqual(relay.ledger_path(self.cfg).read_bytes(), before)
                            self.assertFalse(backup.exists())
                            self.assertEqual(upload.call_count, 0)
                            self.assertEqual(len(self.browser.sent), 0)
                    finally:
                        restore()
                    self.assertEqual(relay.ledger_path(self.cfg).read_bytes(), original_ledger)
                    self.assertEqual((d / 'JOB.json').read_bytes(), held)
            with self.assertRaisesRegex(SystemExit, 'DRIVE_RESUME_REFUSED_AUTHORITY'):
                call('--authorize-preimage', '0'*64)
            original_list = self.svc.files.return_value.list.side_effect
            def duplicate(**kw):
                if 'mimeType =' in kw['q']:
                    return types.SimpleNamespace(execute=lambda: {'files': [folder, folder]})
                return original_list(**kw)
            refused('duplicate folder', 'FOLDER',
                    lambda: setattr(self.svc.files.return_value.list, 'side_effect', duplicate),
                    lambda: setattr(self.svc.files.return_value.list, 'side_effect', original_list))
            def nonempty(**kw):
                if f"'{folder_id}' in parents" in kw['q']:
                    return types.SimpleNamespace(execute=lambda: {'files': [{'id': 'payload'}]})
                return original_list(**kw)
            refused('nonempty folder', 'FOLDER',
                    lambda: setattr(self.svc.files.return_value.list, 'side_effect', nonempty),
                    lambda: setattr(self.svc.files.return_value.list, 'side_effect', original_list))
            transported_prompt = d / 'drive-input/PROMPT.md'
            original_transport = transported_prompt.read_bytes()
            refused('preexisting transport prompt replacement', 'PACKET_OR_RESERVATION',
                    lambda: transported_prompt.write_bytes(b'# substituted review instructions\n'),
                    lambda: transported_prompt.write_bytes(original_transport), default=True)
            refused('packet drift', 'PACKET_OR_RESERVATION',
                    lambda: files[0].write_text('changed'),
                    lambda: files[0].write_text('retained attachment 0\n'))
            refused('send evidence', 'PACKET_OR_RESERVATION',
                    lambda: relay.ledger_append(self.cfg, {'job': job, 'event': 'submission_bound'}),
                    lambda: relay.ledger_path(self.cfg).write_bytes(original_ledger))
            refused('changed state', 'STATE_OR_PACKET',
                    lambda: (d / 'JOB.json').write_bytes(held+b' '),
                    lambda: (d / 'JOB.json').write_bytes(held))
            self.assertEqual(call('--authorize-preimage', sha(held))[0], 0)
            self.assertEqual(upload.call_count, 29)
            self.assertEqual(len(created), 1)
            self.assertEqual(len(self.browser.sent), 1)
            self.assertEqual(self.browser.sent[0][1], message.decode())
            self.assertEqual((d / 'MESSAGE.md').read_bytes(), message)
            self.assertEqual([r['event'] for r in relay.ledger_rows(self.cfg)],
                             ['submitted', 'drive_timeout_resume', 'submission_bound'])
            self.assertEqual((relay.job_state(job)['typed'], relay.job_state(job)['sent']), (True, True))
            with self.assertRaises(SystemExit):
                call('--authorize-preimage', sha(held))
            self.assertEqual(len(self.browser.sent), 1)

    def test_cleanup_retains_nested_attempt_and_history_targets(self):
        targets = {'resume-only', 'attempt-only', 'history-only'}
        for tab in targets:
            self.browser.pages[tab] = {'url': HOME, 'composer': 'retained draft', 'text': '',
                                       'messages': False, 'stop': False}
        self.job('held', url=None, tab=None, status='failed', typed=True,
            sent='UNKNOWN', retry_blocked=True, resume_error={'tab': 'resume-only'},
            resume_attempt={'retained_tab': 'attempt-only'},
            resume_history=[{'retained_tab': 'history-only'}])
        self.assertEqual(relay.cleanup_tabs(), 1)  # unrelated fixture sibling only
        self.assertEqual(set(self.browser.pages), targets)
        self.assertEqual(self.browser.navigations, [])
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])

    def test_cleanup_reclaims_terminal_chat_tabs_but_preserves_generating_binding(self):
        self.browser.pages['active'] = {"url": WANT, "text": "", "composer": "", "messages": True, "stop": True}
        self.browser.pages['done'] = {"url": SIBLING, "text": "reply", "composer": "", "messages": True, "stop": False}
        self.job(name='active-job', tab='active')
        self.job(name='done-job', url=SIBLING, tab='done', status='done')
        self.assertEqual(relay.cleanup_tabs(), 2)
        self.assertEqual(set(self.browser.pages), {'active'})
        self.assertEqual(self.browser.navigations, [])
        self.job(name='active-job', tab='active', status='done')
        self.assertEqual(relay.cleanup_tabs(), 1)
        self.assertEqual(len(self.browser.pages), 1)
        self.assertEqual(next(iter(self.browser.pages.values()))['url'], 'about:blank')

    def test_single_managed_wave_qualifies_three_generating_chats(self):
        """Actual wave + submit/poll code; fourth refuses before a browser send."""
        wave_spec = importlib.util.spec_from_file_location('qualified_wave', RELAY_PATH.with_name('gpt6pro_wave.py'))
        assert wave_spec is not None and wave_spec.loader is not None
        wave = importlib.util.module_from_spec(wave_spec)
        with mock.patch.dict(sys.modules, {'gpt6pro_relay': relay}):
            wave_spec.loader.exec_module(wave)
        jobs = ['qualified-one', 'qualified-two', 'qualified-three']
        for job in [*jobs, 'qualified-four']:
            d = relay.job_dir(job)
            (d / 'PROMPT.md').write_text('Distinct useful review fixture: ' + job)
            (d / 'MANIFEST.json').write_text(json.dumps({'job': job, 'job_class': 'review', 'attach': [],
                **{k: job + '-' + k for k in relay.PROVENANCE_FIELDS}}))
        wave_file = self.root / 'wave.json'
        wave_file.write_text(json.dumps({'jobs': [{'job': j} for j in jobs]}))
        peak = []
        self.browser.defer_text = True
        def cli(args):
            with mock.patch.object(sys, 'argv', ['gpt6pro_relay.py', *args]), redirect_stdout(io.StringIO()) as out:
                code = relay.main()
                return code, out.getvalue().strip().splitlines()[-1]
        def managed_run(args, timeout=900):
            if args[0] == 'submit':
                result = cli(args)
                peak.append(len(relay.active_registry()))
                if len(peak) == 3:
                    self.assertEqual(peak, [1, 2, 3])
                    rows = [relay.job_state(j) for j in jobs]
                    self.assertTrue(all(r['status'] == 'running' for r in rows))
                    self.assertEqual(len({r['conversation_id'] for r in rows}), 3)
                    self.assertEqual(len({r['tab'] for r in rows}), 3)
                    self.assertTrue(all(self.browser.pages[r['tab']]['stop'] for r in rows))
                    before = (len(self.browser.sent), len(self.browser.created))
                    fourth = [a.replace('qualified-three', 'qualified-four') for a in args]
                    with self.assertRaisesRegex(SystemExit, 'CAPACITY_ACCOUNT'):
                        cli(fourth)
                    self.assertEqual((len(self.browser.sent), len(self.browser.created)), before)
                    for r in rows:
                        page = self.browser.pages[r['tab']]
                        page.update(stop=False, text='Bound useful result for ' + r['job'])
                return result
            return cli(args)
        def sleep(seconds):
            self.clock += seconds
            self.browser.clock += seconds
        fake_clock = types.SimpleNamespace(monotonic=lambda: self.clock, sleep=sleep)
        with mock.patch.object(wave, 'JOBS', relay.JOBS), \
             mock.patch.object(wave, 'time', fake_clock), \
             mock.patch.object(wave, 'run', side_effect=managed_run), \
             mock.patch.object(wave.importlib, 'reload', side_effect=lambda module: module), \
             mock.patch.object(sys, 'argv', ['gpt6pro_wave.py', '--wave', str(wave_file), '--account', 'account-a']), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(wave.main(), 0)
        self.assertEqual(peak, [1, 2, 3])
        self.assertEqual(len(self.browser.sent), 3)
        self.assertTrue(all(relay.job_state(j)['status'] == 'done' for j in jobs))
        self.assertEqual(relay.active_registry(), [])
        self.assertIn('WAVE_DONE', out.getvalue())

    def test_account_tab_cap_refuses_before_creating_a_sixth_page(self):
        for i in range(4):
            self.browser.pages[f'old-{i}'] = {"url": SIBLING, "text": "", "composer": "", "messages": True, "stop": False}
        result = relay.send_message('packet', forbidden=[])
        self.assertEqual((result['error'], result['count'], result['cap']), ('TAB_CAP', 5, 5))
        self.assertEqual(self.browser.created, [])
        self.assertEqual(self.browser.inserts, [])

    def test_no_reply_reconciliation_keeps_ambiguous_send_bound(self):
        self.browser.pages["exact"] = {"url": WANT, "text": "", "composer": "", "messages": True, "stop": False}
        before = self.job(tab="exact", sent=True, typed=True)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(relay.reconcile_no_reply("job"), 3)
        self.assertIn("RECONCILED_NO_REPLY_UNRESOLVED", out.getvalue())
        self.assertEqual(relay.job_state("job"), before)
        self.assertEqual(self.browser.navigations, [])
        self.assertEqual(self.browser.sent, [])
        self.browser.pages["exact"]["url"] = SIBLING
        with self.assertRaises(SystemExit):
            relay.reconcile_no_reply("job")
        self.assertEqual(relay.job_state("job"), before)

    def test_ambiguous_sent_bound_polls_never_navigate_or_release_reservation(self):
        # Reproduce the incident: virtualized generation has neither assistant
        # text nor a legacy Stop button. Three polls used to trigger a reload.
        self.browser.pages['exact'] = {'url': WANT, 'text': '', 'composer': '',
                                       'messages': True, 'stop': False}
        original = self.job(tab='exact', sent=True, typed=True,
                            input_sha256='a'*64, reply_token='b'*64,
                            dispatcher_profile='default', hermes_session_id='offline',
                            linear_child='GLP-test', request_id='ambiguous')
        (relay.JOBS / 'job/MESSAGE.md').write_text(relay._failed_generation_identity(original))
        relay.ledger_append(self.cfg, {'event': 'submission_bound', 'job': 'job',
            'account': original['account'], 'conversation_url': WANT,
            **{key: original[key] for key in relay.PROVENANCE_FIELDS}})
        events = relay.ledger_rows(self.cfg)
        with mock.patch.object(self.browser, 'cdp', wraps=self.browser.cdp) as cdp, \
             mock.patch.object(relay, 'read_reply', wraps=relay.read_reply) as reads:
            for elapsed in (0, 150, 150, 86400):
                self.clock += elapsed
                self.assertEqual(relay.poll('job', False, 150, 3600), 3)
                self.assertEqual(self.browser.navigations, [], 'empty polls must not reload')
                self.assertEqual(relay.job_state('job')['status'], 'running')
                self.assertEqual([r['job'] for r in relay.active_registry()], ['job'])
            self.assertEqual(len(reads.call_args_list), 4)
            self.assertTrue(all(not c.kwargs.get('reload') for c in reads.call_args_list))
            self.assertEqual(relay.reconcile_no_reply('job'), 3)
            before = relay.job_state('job')
            # A lost/reused bound tab must not turn a read into a new-tab reopen,
            # including fetch-outbox's initial read of a nonterminal job.
            for tab_state in ('missing', 'reused'):
                if tab_state == 'missing':
                    self.browser.pages.pop('exact')
                else:
                    self.browser.pages['exact'] = {**self.browser.pages['sibling']}
                for entry in ('poll', 'fetch'):
                    with self.subTest(tab_state=tab_state, entry=entry):
                        self.clock += 150
                        with self.assertRaises(SystemExit):
                            if entry == 'poll':
                                relay.poll('job', False, 150, 3600)
                            else:
                                relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
                        self.assertEqual(relay.job_state('job'), before)
                        self.assertEqual(self.browser.created, [])
            self.assertFalse(any(c.args[0] in ('Page.navigate', 'Page.reload', 'Target.createTarget')
                                 for c in cdp.call_args_list))
        current = relay.job_state('job')
        self.assertEqual(current['tab'], original['tab'])
        self.assertEqual(current['conversation_url'], original['conversation_url'])
        self.assertTrue(current['sent'])
        self.assertFalse(current.get('reloaded_once', False))
        self.assertEqual([r['job'] for r in relay.active_registry()], ['job'])
        self.assertEqual(relay.ledger_rows(self.cfg), events)
        self.assertEqual(self.browser.navigations, [])
        self.assertEqual(self.browser.created, [])
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])
        relay.close_tab_quiet.assert_not_called()
        self.svc.files.assert_not_called()
        self.assertFalse((relay.JOBS / 'job/RESPONSE.md').exists())
        self.assertFalse((relay.JOBS / 'job/outbox').exists())

    def test_thinking_failed_bound_generation_is_terminal_but_ambiguous_controls_are_not(self):
        page = {"url": WANT, "text": "", "composer": "", "messages": True,
                "stop": False, "thinking_failed": True, "identity_matched": True}
        self.browser.pages["exact"] = page
        st = self.job(tab="exact", sent=True, typed=True, input_sha256='a'*64,
                      reply_token='b'*64, dispatcher_profile='default',
                      hermes_session_id='offline', linear_child='GLP-test', request_id='test')
        identity = relay._failed_generation_identity(st)
        (relay.JOBS / 'job' / 'MESSAGE.md').write_text('packet\n' + identity)
        relay.ledger_append(self.cfg, {'event': 'submission_bound', 'job': 'job',
            'account': st['account'], 'conversation_url': WANT,
            **{key: st[key] for key in relay.PROVENANCE_FIELDS}})
        before = (relay.JOBS / 'job/JOB.json').read_bytes()
        # The exact UI text is not enough without the identity in the user message.
        page['identity_matched'] = False
        self.assertEqual(relay.reconcile_no_reply('job'), 3)
        self.assertEqual((relay.JOBS / 'job/JOB.json').read_bytes(), before)
        page['identity_matched'] = True
        page['thinking_failed'] = False  # quoted error or empty idle page
        self.assertEqual(relay.reconcile_no_reply('job'), 3)
        self.assertEqual((relay.JOBS / 'job/JOB.json').read_bytes(), before)
        page['thinking_failed'] = True
        page['stop'] = True
        self.assertEqual(relay.reconcile_no_reply('job'), 3)
        page['stop'] = False
        page['text'] = 'assistant reply'
        self.assertEqual(relay.reconcile_no_reply('job'), 3)
        page['text'] = ''
        self.assertEqual(relay.reconcile_no_reply('job'), 5)
        failed = relay.job_state('job')
        self.assertEqual((failed['status'], failed['reason'], failed['retry_blocked']),
                         ('failed', 'THINKING_FAILED', True))
        self.assertEqual(failed['input_sha256'], st['input_sha256'])
        self.assertEqual(failed['reply_token'], st['reply_token'])
        self.assertEqual(failed['conversation_id'], st['conversation_id'])
        self.assertEqual(failed['tab'], 'exact')
        relay.close_tab_quiet.assert_called_with('exact', WANT)
        self.assertEqual(len(relay.active_registry()), 0)
        self.assertEqual(len([x for x in relay.ledger_rows(self.cfg) if x['event'] == 'generation_failed']), 2)
        self.assertEqual(relay.hashlib.sha256(Path(failed['failed_generation']['preimage']).read_bytes()).hexdigest(),
                         failed['failed_generation']['preimage_sha256'])
        self.assertFalse((relay.JOBS / 'job/RESPONSE.md').exists())
        self.assertFalse((relay.JOBS / 'job/outbox').exists())
        self.assertEqual(self.browser.navigations, [])
        self.assertEqual(self.browser.sent, [])
        with self.assertRaisesRegex(SystemExit, 'JOB_NOT_RUNNING'):
            relay.reconcile_no_reply('job')
        # The standing poll entry must make the same terminal decision without
        # refreshing a failed page, and --wait must stop rather than loop.
        other = self.job(name='polled', tab='exact2', url=SIBLING, sent=True, typed=True,
                         input_sha256='c'*64, reply_token='d'*64,
                         dispatcher_profile='default', hermes_session_id='offline',
                         linear_child='GLP-test', request_id='polled', finished_unrendered_polls=2)
        self.browser.pages['exact2'] = {**page, 'url': SIBLING}
        (relay.JOBS / 'polled/MESSAGE.md').write_text(relay._failed_generation_identity(other))
        relay.ledger_append(self.cfg, {'event': 'submission_bound', 'job': 'polled',
            'account': other['account'], 'conversation_url': SIBLING,
            **{key: other[key] for key in relay.PROVENANCE_FIELDS}})
        self.assertEqual(relay.poll('polled', True, 30, 3600), 5)
        self.assertEqual(relay.job_state('polled')['reason'], 'THINKING_FAILED')
        self.assertEqual(self.browser.navigations, [])

    def test_thinking_failed_button_and_user_role_only_variant(self):
        page = {"url": WANT, "text": "", "composer": "", "messages": True, "stop": False,
                "thinking_failed": True, "failure_location": "button",
                "identity_matched": False, "identity_location": "user-role-only"}
        self.browser.pages['exact'] = page
        st = self.job(tab='exact', sent=True, typed=True, input_sha256='a'*64,
                      reply_token='b'*64, dispatcher_profile='default',
                      hermes_session_id='offline', linear_child='GLP-test', request_id='test')
        (relay.JOBS / 'job/MESSAGE.md').write_text(relay._failed_generation_identity(st))
        relay.ledger_append(self.cfg, {'event': 'submission_bound', 'job': 'job',
            'account': st['account'], 'conversation_url': WANT,
            **{key: st[key] for key in relay.PROVENANCE_FIELDS}})
        before = (relay.JOBS / 'job/JOB.json').read_bytes()
        # Visible button alone, without the full marker in the user-role node, is UNKNOWN.
        self.assertEqual(relay.reconcile_no_reply('job'), 3)
        self.assertEqual((relay.JOBS / 'job/JOB.json').read_bytes(), before)
        page['identity_matched'] = True
        page['thinking_failed'] = False  # empty/ambiguous page remains UNKNOWN
        self.assertEqual(relay.reconcile_no_reply('job'), 3)
        self.assertEqual((relay.JOBS / 'job/JOB.json').read_bytes(), before)
        page['thinking_failed'] = True
        self.assertEqual(relay.reconcile_no_reply('job'), 5)
        self.assertEqual((relay.job_state('job')['status'], relay.job_state('job')['reason']),
                         ('failed', 'THINKING_FAILED'))
        self.assertEqual(self.browser.navigations, [])
        self.assertEqual(self.browser.sent, [])

    def args(self, name="job", wait=False):
        prompt = self.root / f"{name}.md"
        prompt.write_text("packet " + name)
        return types.SimpleNamespace(job=name, account="account-a", job_class="review", prompt=str(prompt),
                                     dispatcher_profile='fixture', hermes_session_id='offline', linear_child='GLP-test', request_id=name,
                                     attach=[], drive_attach=[], prompt_on_drive=False, dry_run=False,
                                     wait=wait, interval=30, max_wait=3600)

    def test_sibling_collision_never_typed(self):
        self.job("older", SIBLING, status="done")
        self.browser.home_redirect = SIBLING
        with self.assertRaisesRegex(SystemExit, "conversation_collision"):
            relay.cmd_submit(self.args())
        st = relay.job_state("job")
        self.assertEqual(st["status"], "failed")
        self.assertEqual(st["reason"], "conversation_collision")
        self.assertFalse(st["error"]["typed"])
        self.assertFalse(st["error"]["sent"])
        self.assertNotIn("conversation_url", st)
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])

    def test_passive_interstitial_clears_after_old_model_deadline_on_same_tab(self):
        # The real pacing prelude advances the fixture to t=60 before navigation.
        self.browser.app_ready_at = 100
        with mock.patch.object(self.browser, 'cdp', wraps=self.browser.cdp) as cdp:
            result = relay.send_message('packet', forbidden=[SIBLING])
        self.assertNotIn('error', result)
        self.assertEqual(self.browser.inserts, [('new-1', 'packet')])
        self.assertEqual(self.browser.sent, [('new-1', 'packet')])
        self.assertGreaterEqual(self.browser.first_composer_probe, self.browser.app_ready_at)
        self.assertEqual({tab for tab, _ in self.browser.app_samples}, {'new-1'})
        self.assertEqual(self.browser.created, [('new-1', 'about:blank')])
        navigation = [c for c in cdp.call_args_list if c.args[0] in ('Page.navigate', 'Page.reload')]
        self.assertEqual(len(navigation), 1)
        self.assertEqual(navigation[0].kwargs, {'session_id': 'session-new-1', 'url': HOME})
        self.assertEqual(self.browser.navigations, [])
        self.assertEqual(self.browser.current_tab_calls, 0)

    def test_human_challenge_preserves_target_without_waiting_or_interaction(self):
        self.browser.app_overrides = {'human_challenge': True}
        with mock.patch.object(self.browser, 'cdp', wraps=self.browser.cdp) as cdp, \
             mock.patch.object(self.browser, 'js', wraps=self.browser.js) as js:
            result = relay.send_message('packet')
        self.assertEqual(result['error'], 'HUMAN_CHALLENGE')
        self.assertEqual(result['readiness']['elapsed_s'], 0)
        self.assertEqual(result['readiness']['observations'], 1)
        self.assertFalse(result['typed'])
        self.assertFalse(result['sent'])
        self.assertIn(result['tab'], self.browser.pages)
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])
        self.assertFalse(any(c.args[0].startswith('Input.') for c in cdp.call_args_list))
        self.assertFalse(any('.click()' in c.args[0] for c in js.call_args_list))

    def test_readiness_timeout_has_bounded_evidence_and_never_retries(self):
        self.browser.app_ready_at = float('inf')
        with mock.patch.object(self.browser, 'cdp', wraps=self.browser.cdp) as cdp:
            result = relay.send_message('packet')
        self.assertEqual(result['error'], 'SECURITY_INTERSTITIAL')
        self.assertEqual(result['readiness'], {'elapsed_s': 90, 'observations': 46, 'limit_s': 90})
        self.assertFalse(result['typed'])
        self.assertFalse(result['sent'])
        self.assertIn(result['tab'], self.browser.pages)
        self.assertEqual(len(self.browser.created), 1)
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])
        self.assertEqual([c.args[0] for c in cdp.call_args_list if c.args[0].startswith('Page.')], ['Page.navigate'])

    def test_incomplete_app_is_not_wrong_model(self):
        self.browser.app_overrides = {'composer_present': False, 'model': '5 Pro'}
        result = relay.send_message('packet')
        self.assertEqual(result['error'], 'APP_NOT_READY')
        self.assertEqual(result['readiness']['elapsed_s'], 90)
        self.assertEqual(self.browser.inserts, [])

    def test_ready_wrong_model_still_refuses_before_input(self):
        self.browser.app_overrides = {'model': '5 Pro'}
        result = relay.send_message('packet')
        self.assertEqual(result['error'], 'WRONG_MODEL')
        self.assertFalse(result['typed'])
        self.assertFalse(result['sent'])
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])

    def test_readiness_rate_limit_refuses_immediately_even_with_ready_model(self):
        self.browser.app_overrides = {'rate_limited': True}
        result = relay.send_message('packet')
        self.assertEqual(result['error'], 'RATE_LIMITED')
        self.assertEqual(result['readiness']['observations'], 1)
        self.assertFalse(result['typed'])
        self.assertFalse(result['sent'])
        self.assertEqual(self.browser.inserts, [])

    def test_readiness_logged_out_preserves_unsent_flags(self):
        self.browser.app_overrides = {'logged_out': True}
        result = relay.send_message('packet')
        self.assertEqual(result['error'], 'LOGGED_OUT')
        self.assertEqual(result['readiness']['observations'], 1)
        self.assertFalse(result['typed'])
        self.assertFalse(result['sent'])
        self.assertEqual(self.browser.inserts, [])

    def test_readiness_refusal_keeps_registered_reservation_retry_blocked(self):
        self.browser.app_overrides = {'human_challenge': True}
        args = self.args()
        with self.assertRaisesRegex(SystemExit, 'SEND_FAILED HUMAN_CHALLENGE'):
            relay.cmd_submit(args)
        st = relay.job_state(args.job)
        self.assertEqual(st['reason'], 'HUMAN_CHALLENGE')
        self.assertTrue(st['retry_blocked'])
        self.assertFalse(st['typed'])
        self.assertFalse(st['sent'])
        self.assertIn(st['error']['tab'], self.browser.pages)
        self.assertNotIn('conversation_url', st)
        self.assertEqual([r['event'] for r in relay.ledger_rows(self.cfg)], ['submitted'])
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])
        created = list(self.browser.created)
        with self.assertRaises(SystemExit):
            relay.cmd_submit(args)
        self.assertEqual(self.browser.created, created)
        relay.cleanup_tabs()
        self.assertIn(st['error']['tab'], self.browser.pages, 'registered cleanup must preserve the challenge')

    def test_readiness_observation_refuses_reused_target_without_new_navigation(self):
        self.browser.app_ready_at = float('inf')
        original_sleep = self.browser.sleep
        def move_target(seconds):
            original_sleep(seconds)
            if self.browser.app_samples:
                self.browser.pages['new-1']['url'] = SIBLING
        with mock.patch.object(self.browser, 'sleep', side_effect=move_target):
            result = relay.send_message('packet')
        self.assertEqual(result['error'], 'conversation_collision')
        self.assertFalse(result['typed'])
        self.assertFalse(result['sent'])
        self.assertEqual(len(self.browser.created), 1)
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(self.browser.navigations, [])

    def test_readiness_observation_does_not_reopen_lost_target(self):
        self.browser.app_ready_at = float('inf')
        original_cdp = self.browser.cdp
        def lose_target(method, **params):
            if method == 'Target.getTargetInfo' and self.browser.app_samples:
                raise RuntimeError('No target with given id')
            return original_cdp(method, **params)
        with mock.patch.object(self.browser, 'cdp', side_effect=lose_target):
            with self.assertRaisesRegex(RuntimeError, 'No target with given id'):
                relay.send_message('packet')
        self.assertEqual(len(self.browser.created), 1)
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(self.browser.navigations, [])

    def test_current_thinking_effort_trigger_uses_normal_sender(self):
        self.browser.medium_trigger = True
        self.browser.trigger_label = 'Thinking effort'
        result = relay.send_message('packet')
        self.assertNotIn('error', result)
        self.assertTrue(result['typed'])
        self.assertTrue(result['sent'])
        self.assertEqual(len(self.browser.created), 1)
        self.assertEqual(len(self.browser.sent), 1)

    def test_current_thinking_effort_trigger_never_sends_lower_model(self):
        self.browser.medium_trigger = True
        self.browser.trigger_label = 'Thinking effort'
        self.browser.selected_model = '5 Pro'
        result = relay.send_message('packet')
        self.assertEqual(result['error'], 'WRONG_MODEL')
        self.assertFalse(result['typed'])
        self.assertFalse(result['sent'])
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])

    def test_late_app_readiness_still_requires_maximum_picker_gate(self):
        self.browser.app_ready_at = 100
        self.browser.medium_trigger = True
        self.browser.slider_works = False
        result = relay.send_message('packet')
        self.assertEqual(result['error'], 'WRONG_MODEL')
        self.assertGreaterEqual(self.browser.clock, 100)
        self.assertFalse(result['typed'])
        self.assertFalse(result['sent'])
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])

    def test_readiness_checks_deadline_before_accepting_slow_ready_observation(self):
        original_js = self.browser.js
        def slow_observation(expression):
            if expression.lstrip().startswith('(()=>{') and 'const mb=' in expression and 'const here=' not in expression:
                self.browser.clock += 91
            return original_js(expression)
        with mock.patch.object(self.browser, 'js', side_effect=slow_observation):
            result = relay.send_message('packet')
        self.assertEqual(result['error'], 'APP_NOT_READY')
        self.assertEqual(result['readiness']['elapsed_s'], 91)
        self.assertFalse(result['typed'])
        self.assertFalse(result['sent'])
        self.assertEqual(self.browser.inserts, [])

    def test_real_state_javascript_distinguishes_passive_and_human_security(self):
        # Execute the actual probe, not FakeCDP's state dictionary. No browser/network.
        env = {'js': lambda expression: expression}
        exec(relay.BU_PRELUDE, env)
        expression = env['_state']()
        fixtures = [
            {'title': 'Just a moment...', 'text': 'Checking your browser', 'human': False, 'passive': True},
            {'text': 'Verify you are human', 'human': True, 'passive': False},
            {'text': 'Please complete the security check', 'human': True, 'passive': False},
            {'frame': 'https://challenges.cloudflare.com/turnstile', 'human': True, 'passive': False},
            {'frame': 'https://www.google.com/recaptcha/api2/anchor', 'human': True, 'passive': False},
            {'frame': 'https://hcaptcha.com/challenge', 'human': True, 'passive': False},
            {'frame': 'https://challenges.cloudflare.com/turnstile', 'hidden': True, 'human': False, 'passive': False},
            {'frame': 'https://example.test/help', 'human': False, 'passive': False},
            {'app': True, 'human': False, 'passive': False},
        ]
        harness = r'''
const fixtures = FIXTURES;
console.log(JSON.stringify(fixtures.map(f => {
  const node = {innerText:'Medium',src:f.frame||'',title:'',getAttribute:k=>k==='src'?f.frame:null,
    getClientRects:()=>f.hidden?[]:[{}],style:{},hidden:!!f.hidden};
  global.getComputedStyle = ()=>({display:f.hidden?'none':'block',visibility:'visible'});
  global.location = {href:'https://chatgpt.com/'};
  global.document = {title:f.title||'ChatGPT',readyState:'complete',body:{innerText:f.text||''},
    querySelector:s=>f.app&&(s.includes('prompt-textarea')||s.includes('Select ChatGPT model'))?node:null,
    querySelectorAll:s=>s.includes('iframe')&&f.frame?[node]:[]};
  return PROBE;
})));
'''.replace('FIXTURES', json.dumps(fixtures)).replace('PROBE', expression)
        completed = subprocess.run(['node', '-e', harness], capture_output=True, text=True, check=True)
        for fixture, state in zip(fixtures, json.loads(completed.stdout)):
            with self.subTest(fixture=fixture):
                self.assertEqual(state['human_challenge'], fixture['human'])
                self.assertEqual(state['security_interstitial'], fixture['passive'])
                self.assertEqual(state['composer_present'], fixture.get('app', False))
                self.assertEqual(state['model_trigger'], 'Medium' if fixture.get('app') else None)

    def test_current_tab_is_not_the_created_target(self):
        result = relay.send_message("packet", forbidden=[SIBLING])
        self.assertNotIn("error", result)
        self.assertEqual(result["tab"], "new-1")
        self.assertEqual(self.browser.inserts, [("new-1", "packet")])
        self.assertEqual(self.browser.current_tab_calls, 0)

    def test_pro_only_fresh_tab_requires_exact_selected_six_pro_before_input(self):
        self.browser.pro_only_trigger = True
        result = relay.send_message('packet', forbidden=[SIBLING])
        self.assertNotIn('error', result)
        self.assertEqual(self.browser.inserts, [('new-1', 'packet')])
        self.browser.selected_model = '5 Pro'
        refused = relay.send_message('different packet', forbidden=[SIBLING])
        self.assertEqual(refused['error'], 'WRONG_MODEL')
        self.assertEqual(len(self.browser.inserts), 1)
        self.assertEqual(len(self.browser.sent), 1)

    def test_medium_fresh_tab_selects_max_and_verifies_six_pro_before_input(self):
        self.browser.medium_trigger = True
        result = relay.send_message('packet', forbidden=[SIBLING])
        self.assertNotIn('error', result)
        self.assertEqual(self.browser.inserts, [('new-1', 'packet')])
        self.assertEqual(len(self.browser.sent), 1)
        self.browser.slider_works = False
        refused = relay.send_message('different packet', forbidden=[SIBLING])
        self.assertEqual(refused['error'], 'WRONG_MODEL')
        self.assertEqual(len(self.browser.inserts), 1)
        self.assertEqual(len(self.browser.sent), 1)

    def test_new_markdown_composer_passes_preinsert_checks(self):
        self.browser.pro_only_trigger = True
        self.browser.markdown_composer_only = True
        result = relay.send_message('packet', forbidden=[SIBLING])
        self.assertNotIn('error', result)
        self.assertEqual(self.browser.inserts, [('new-1', 'packet')])
        self.assertEqual(len(self.browser.sent), 1)

    def test_blank_markdown_paragraph_is_not_a_nonempty_composer(self):
        self.browser.pro_only_trigger = True
        self.browser.markdown_composer_only = True
        self.browser.blank_paragraph = '\n'
        result = relay.send_message('packet', forbidden=[SIBLING])
        self.assertNotIn('error', result)
        self.assertEqual(len(self.browser.sent), 1)

    def test_late_markdown_composer_waits_for_stable_node_before_any_input(self):
        self.browser.pro_only_trigger = True
        self.browser.markdown_composer_only = True
        self.browser.composer_delay = 45
        result = relay.send_message('packet', forbidden=[SIBLING])
        self.assertNotIn('error', result)
        self.assertGreaterEqual(self.browser.clock - self.browser.first_composer_probe, 53)
        self.assertEqual(self.browser.inserts, [('new-1', 'packet')])
        self.assertEqual(len(self.browser.sent), 1)

    def test_composer_reconcile_requires_exact_unsent_tab_and_ledger_evidence(self):
        job = 'glp270-20260924d-taxonomy-all-expiring-rows'
        d = relay.job_dir(job)
        st = {'job': job, 'account': 'account-a', 'status': 'failed',
              'reason': 'NO_COMPOSER', 'retry_blocked': True, 'typed': 'UNKNOWN', 'sent': 'UNKNOWN',
              'error': {'error': 'NO_COMPOSER', 'state': {'url': HOME, 'stop': False,
                                                       'assistant_count': 0, 'last_len': 0}}}
        relay.job_write(job, st)
        (d / 'MESSAGE.md').write_text('packet')
        prior = [('submitted', None)]
        for i in range(2):
            digest = relay.hashlib.sha256(f'previous-{i}'.encode()).hexdigest()
            (d / ('JOB.pre-model-reconcile-' + digest + '.json')).write_bytes(f'previous-{i}'.encode())
            prior += [('model_unsent_reconcile', 'STARTED'), ('model_unsent_reconcile', 'APPLIED'),
                      ('submitted', None)]
        for event, phase in prior:
            relay.ledger_append(self.cfg, {'event': event, 'phase': phase, 'job': job,
                                           'preimage_sha256': digest if event == 'model_unsent_reconcile' else None})
        # Replace historical digests with their respective exact backups.
        ledger = self.root / 'ledger.jsonl'
        rows = [json.loads(x) for x in ledger.read_text().splitlines()]
        backups = sorted(d.glob('JOB.pre-model-reconcile-*.json'))
        for idx, backup in zip((2, 5), backups):
            rows[idx]['preimage_sha256'] = relay.hashlib.sha256(backup.read_bytes()).hexdigest()
        ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        empty = {'tabs': [{'url': HOME, 'composer_empty': True, 'messages_empty': True,
                           'job_in_user': False, 'job_in_composer': False}]}
        with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()), mock.patch.object(
            relay, 'bu_json', return_value=empty) as read, mock.patch.object(relay, 'cleanup_tabs', return_value=1):
            self.assertEqual(relay.reconcile_unsent_model(job), 0)
            self.assertEqual(relay.job_state(job)['status'], 'failed')
            read.return_value = {'tabs': [{**empty['tabs'][0], 'job_in_composer': True}]}
            with self.assertRaisesRegex(SystemExit, 'TAB_EVIDENCE'):
                relay.reconcile_unsent_model(job, apply=True)
            read.return_value = empty
            self.assertEqual(relay.reconcile_unsent_model(job, apply=True), 0)
        self.assertEqual(relay.job_state(job)['status'], 'unsent_reconciled')
        self.assertFalse(relay.job_state(job)['sent'])
        self.assertTrue(list(d.glob('JOB.pre-composer-reconcile-*.json')))
        self.assertEqual([x.get('phase') for x in relay.ledger_rows(self.cfg)[-2:]], ['STARTED', 'APPLIED'])

    def test_aria_only_submit_button_sends_exactly_once(self):
        self.browser.aria_send_only = True
        result = relay.send_message('packet', forbidden=[SIBLING])
        self.assertNotIn('error', result)
        self.assertEqual(self.browser.inserts, [('new-1', 'packet')])
        self.assertEqual(self.browser.sent, [('new-1', 'packet')])

    def test_nonempty_composer_reconcile_requires_exact_incident_tab_and_history(self):
        job = 'glp270-20260924d-taxonomy-all-expiring-rows'
        d = relay.job_dir(job)
        state = {'job': job, 'account': 'account-a', 'status': 'failed',
                 'reason': 'composer_not_empty', 'retry_blocked': True,
                 'typed': False, 'sent': False,
                 'error': {'error': 'composer_not_empty', 'typed': False, 'sent': False,
                           'tab': 'incident-tab'}}
        relay.job_write(job, state)
        (d / 'MESSAGE.md').write_text('packet')
        history = [('submitted', None)]
        for i in range(2):
            digest = relay.hashlib.sha256(f'prior-{i}'.encode()).hexdigest()
            (d / ('JOB.pre-model-reconcile-' + digest + '.json')).write_bytes(f'prior-{i}'.encode())
            history += [('model_unsent_reconcile', 'STARTED'),
                        ('model_unsent_reconcile', 'APPLIED'), ('submitted', None)]
        digest = relay.hashlib.sha256(b'pre-composer').hexdigest()
        (d / ('JOB.pre-composer-reconcile-' + digest + '.json')).write_bytes(b'pre-composer')
        history += [('composer_unsent_reconcile', 'STARTED'),
                    ('composer_unsent_reconcile', 'APPLIED'), ('submitted', None)]
        for event, phase in history:
            relay.ledger_append(self.cfg, {'event': event, 'phase': phase, 'job': job,
                                           'preimage_sha256': digest if event != 'submitted' else None})
        ledger = self.root / 'ledger.jsonl'
        rows = [json.loads(x) for x in ledger.read_text().splitlines()]
        for index in (2, 5):
            backup = sorted(d.glob('JOB.pre-model-reconcile-*.json'))[0 if index == 2 else 1]
            rows[index - 1]['preimage_sha256'] = rows[index]['preimage_sha256'] = relay.hashlib.sha256(backup.read_bytes()).hexdigest()
        ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        clean = {'tabs': [{'tab': 'incident-tab', 'url': HOME, 'composer_empty': True,
                           'messages_empty': True, 'job_in_user': False,
                           'job_in_composer': False}]}
        with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()), mock.patch.object(
            relay, 'bu_json', return_value=clean) as read, mock.patch.object(relay, 'cleanup_tabs', return_value=1):
            self.assertEqual(relay.reconcile_unsent_model(job), 0)
            self.assertEqual(relay.job_state(job), state)
            for bad in ({'tabs': []},
                        {'tabs': [{**clean['tabs'][0], 'composer_empty': False}]},
                        {'tabs': [{**clean['tabs'][0], 'tab': 'different-tab'}]},
                        {'tabs': [{**clean['tabs'][0], 'job_in_user': True}]}):
                read.return_value = bad
                with self.assertRaisesRegex(SystemExit, 'TAB_EVIDENCE'):
                    relay.reconcile_unsent_model(job, apply=True)
            read.return_value = clean
            self.assertEqual(relay.reconcile_unsent_model(job, apply=True), 0)
        self.assertEqual(relay.job_state(job)['status'], 'unsent_reconciled')
        self.assertFalse(relay.job_state(job)['retry_blocked'])
        self.assertEqual(relay.job_state(job)['typed'], False)
        self.assertEqual(relay.job_state(job)['sent'], False)
        self.assertEqual([x['phase'] for x in relay.ledger_rows(self.cfg)
                          if x.get('event') == 'nonempty_composer_unsent_reconcile'], ['STARTED', 'APPLIED'])
        self.assertTrue(list(d.glob('JOB.pre-nonempty-composer-reconcile-*.json')))

    def test_send_disabled_reconcile_requires_full_unsent_proof_and_clears_bound_composer(self):
        job = 'glp270-20260924d-taxonomy-all-expiring-rows'
        d = relay.job_dir(job)
        message = 'reply_token=fixture input_sha256=fixture\n' + job
        (d / 'MESSAGE.md').write_text(message)
        state = {'job': job, 'account': 'account-a', 'status': 'failed',
                 'reply_token': 'fixture', 'input_sha256': 'fixture',
                 'reason': 'SEND_DISABLED', 'retry_blocked': True,
                 'typed': 'UNKNOWN', 'sent': 'UNKNOWN',
                 'error': {'error': 'SEND_DISABLED'}}
        relay.job_write(job, state)
        history = [('submitted', None)]
        for i in range(2):
            blob = f'model-{i}'.encode()
            digest = relay.hashlib.sha256(blob).hexdigest()
            (d / ('JOB.pre-model-reconcile-' + digest + '.json')).write_bytes(blob)
            history += [('model_unsent_reconcile', 'STARTED', digest),
                        ('model_unsent_reconcile', 'APPLIED', digest), ('submitted', None)]
        blob = b'composer'; digest = relay.hashlib.sha256(blob).hexdigest()
        (d / ('JOB.pre-composer-reconcile-' + digest + '.json')).write_bytes(blob)
        history += [('composer_unsent_reconcile', 'STARTED', digest),
                    ('composer_unsent_reconcile', 'APPLIED', digest), ('submitted', None)]
        blob = b'nonempty'; digest = relay.hashlib.sha256(blob).hexdigest()
        (d / ('JOB.pre-nonempty-composer-reconcile-' + digest + '.json')).write_bytes(blob)
        history += [('nonempty_composer_unsent_reconcile', 'STARTED', digest),
                    ('nonempty_composer_unsent_reconcile', 'APPLIED', digest), ('submitted', None)]
        for item in history:
            relay.ledger_append(self.cfg, {'event': item[0], 'phase': item[1], 'job': job,
                                           'preimage_sha256': item[2] if len(item) == 3 else None})
        tabs = [{'tab': 'incident-tab', 'url': HOME, 'composer': message, 'user_count': 0,
                 'assistant_count': 0, 'job_in_user': False},
                {'tab': 'other-tab', 'url': SIBLING, 'composer': '', 'user_count': 1,
                 'assistant_count': 1, 'job_in_user': False}]
        read = {'tabs': tabs, 'target_count': 2}
        with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()), mock.patch.object(
            relay, 'bu_json', return_value=read) as browser:
            self.assertEqual(relay.reconcile_unsent_send_disabled(job, apply=False), 0)
            self.assertEqual(relay.job_state(job), state)
            for bad in ({'tabs': tabs[:1], 'target_count': 2},
                        {'tabs': [{**tabs[0], 'composer': ''}, tabs[1]], 'target_count': 2},
                        {'tabs': [{**tabs[0], 'job_in_user': True}, tabs[1]], 'target_count': 2},
                        {'tabs': [tabs[0], {**tabs[1], 'composer': message}], 'target_count': 2}):
                browser.return_value = bad
                with self.assertRaisesRegex(SystemExit, 'SEND_DISABLED_RECONCILE_REFUSED_TAB_EVIDENCE'):
                    relay.reconcile_unsent_send_disabled(job, apply=True)
            browser.side_effect = [read, {'cleared': True, 'tab': 'incident-tab',
                                           'url': HOME, 'user_count': 0, 'assistant_count': 0},
                                   {'closed': 2, 'remaining': 1}]
            self.assertEqual(relay.reconcile_unsent_send_disabled(job, apply=True), 0)
            self.assertEqual(browser.call_count, 8)
        result = relay.job_state(job)
        self.assertEqual(result['status'], 'unsent_reconciled')
        self.assertIs(result['sent'], False)
        self.assertFalse(result['retry_blocked'])
        self.assertEqual([r.get('phase') for r in relay.ledger_rows(self.cfg)
                          if r['event'] == 'send_disabled_unsent_reconcile'], ['STARTED', 'APPLIED'])
        self.assertEqual(next(d.glob('JOB.pre-send-disabled-reconcile-*.json')).read_bytes(),
                         json.dumps(state, indent=1).encode())

    def test_wrong_model_reconcile_requires_exact_unsent_evidence(self):
        job = 'glp270-20260924d-taxonomy-all-expiring-rows'
        d = relay.job_dir(job)
        state = {'job': job, 'account': 'account-a', 'status': 'failed',
                 'reason': 'WRONG_MODEL', 'retry_blocked': True, 'typed': False, 'sent': False,
                 'error': {'error': 'WRONG_MODEL', 'typed': False, 'sent': False,
                           'state': {'url': HOME, 'model': None, 'assistant_count': 0, 'last_len': 0}}}
        relay.job_write(job, state)
        (d / 'MESSAGE.md').write_text('packet')
        relay.ledger_append(self.cfg, {'event': 'submitted', 'job': job, 'account': 'account-a'})
        report = self.root / 'reports/ops/pro-wave-20260924d/RESULT.json'
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({'jobs': [{'job': job, 'submission_bound': False,
                                               'typed': False, 'sent': False}]}))
        with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()), mock.patch.object(
            relay, '_legacy_tabs', return_value=[{'type': 'page', 'id': 'idle', 'url': HOME}]):
            self.assertEqual(relay.reconcile_unsent_model(job), 0)
            self.assertEqual(relay.job_state(job)['status'], 'failed')
            changed = dict(state); changed['sent'] = True
            relay.job_write(job, changed)
            with self.assertRaisesRegex(SystemExit, 'REFUSED_STATE'):
                relay.reconcile_unsent_model(job, apply=True)
            relay.job_write(job, state)
            self.assertEqual(relay.reconcile_unsent_model(job, apply=True), 0)
        self.assertEqual(relay.job_state(job)['status'], 'unsent_reconciled')
        self.assertFalse(relay.job_state(job)['retry_blocked'])
        self.assertEqual(json.loads(next(d.glob('JOB.pre-model-reconcile-*.json')).read_text()), state)
        self.assertEqual([x['phase'] for x in relay.ledger_rows(self.cfg)
                          if x.get('event') == 'model_unsent_reconcile'], ['STARTED', 'APPLIED'])

    def test_postreset_model_reconcile_requires_no_chat_page_or_home(self):
        job = 'glp41-20260925-postreset-r3-browser-node-blockers'
        d = relay.job_dir(job)
        state = {'job': job, 'account': 'account-a', 'status': 'failed',
                 'reason': 'WRONG_MODEL', 'retry_blocked': True, 'typed': False, 'sent': False,
                 'error': {'error': 'WRONG_MODEL', 'typed': False, 'sent': False,
                           'state': {'url': HOME, 'model': None, 'assistant_count': 0, 'last_len': 0}}}
        relay.job_write(job, state)
        (d / 'MESSAGE.md').write_text('packet')
        relay.ledger_append(self.cfg, {'event': 'submitted', 'job': job, 'account': 'account-a'})
        report = self.root / 'reports/ops/pro-postreset-wave-20260925/RESULT.json'
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({'jobs': [{'job': job, 'submission_bound': False,
                                               'typed': False, 'sent': False}]}))
        with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()), mock.patch.object(
            relay, '_legacy_tabs', return_value=[{'type': 'page', 'id': 'blank', 'url': 'about:blank'}]) as tabs:
            self.assertEqual(relay.reconcile_unsent_model(job), 0)
            tabs.return_value = [{'type': 'page', 'id': 'chat', 'url': WANT}]
            with self.assertRaisesRegex(SystemExit, 'REFUSED_TAB_EVIDENCE'):
                relay.reconcile_unsent_model(job, apply=True)
            tabs.return_value = [{'type': 'page', 'id': 'blank', 'url': 'about:blank'}]
            self.assertEqual(relay.reconcile_unsent_model(job, apply=True), 0)
        self.assertEqual(relay.job_state(job)['status'], 'unsent_reconciled')
        self.assertFalse(relay.job_state(job)['retry_blocked'])

    def _unlisted_wrong_model(self, job, row):
        d = relay.job_dir(job)
        state = {'job': job, 'account': 'account-a', 'status': 'failed',
                 'reason': 'WRONG_MODEL', 'retry_blocked': True, 'typed': False, 'sent': False,
                 'error': {'error': 'WRONG_MODEL', 'typed': False, 'sent': False,
                           'state': {'url': HOME, 'model': None, 'assistant_count': 0, 'last_len': 0}}}
        relay.job_write(job, state)
        (d / 'MESSAGE.md').write_text('packet')
        relay.ledger_append(self.cfg, {'event': 'submitted', 'job': job, 'account': 'account-a'})
        report = 'reports/ops/pro-wave-unlisted/RESULT.json'
        (self.root / report).parent.mkdir(parents=True, exist_ok=True)
        (self.root / report).write_text(json.dumps({'jobs': [{'job_id': job, **row}]}))
        return d, state, report

    def test_security_readiness_reconciliation_retains_exact_unsent_tab(self):
        """Real registered caller + browser script; fixture-only apply, never live."""
        import copy
        import hashlib
        job = 'fixture-security-readiness'
        d = relay.job_dir(job)
        state = {'job': job, 'account': 'account-a', 'status': 'failed',
                 'reason': 'SECURITY_INTERSTITIAL', 'retry_blocked': True,
                 'typed': False, 'sent': False, 'input_sha256': 'a' * 64,
                 'reply_token': 'b' * 64,
                 **{key: 'fixture-' + key for key in relay.PROVENANCE_FIELDS},
                 'error': {'error': 'SECURITY_INTERSTITIAL', 'typed': False, 'sent': False,
                           'tab': 'retained', 'state': {'url': HOME, 'model': None,
                           'assistant_count': 0, 'last_len': 0, 'security_interstitial': True,
                           'human_challenge': False, 'logged_out': False, 'rate_limited': False,
                           'stop': False, 'drive_reconnect': False, 'thinking_failed': False,
                           'composer_present': False}}}
        relay.job_write(job, state)
        message = ('RELAY_IDENTITY job=' + job + ' input_sha256=' + state['input_sha256'] +
                   ' reply_token=' + state['reply_token']).encode()
        (d / 'MESSAGE.md').write_bytes(message)
        row = {'event': 'submitted', 'job': job, 'account': 'account-a',
               'kind': 'initial', 'certainty': 'reserved_may_send',
               **{key: state[key] for key in relay.PROVENANCE_FIELDS}}
        relay.ledger_append(self.cfg, row)
        ledger = relay.ledger_path(self.cfg)
        ledger_preimage = ledger.read_bytes()
        preimage = (d / 'JOB.json').read_bytes()
        digest = lambda raw: hashlib.sha256(raw).hexdigest()
        report = 'reports/readiness.json'
        receipt = {'job': job, 'reason': 'SECURITY_INTERSTITIAL', 'submission_bound': False,
                   'typed': False, 'sent': False, 'error_tab': 'retained',
                   'job_sha256': digest(preimage), 'message_sha256': digest(message),
                   'ledger_rows_sha256': digest(json.dumps(relay.ledger_rows(self.cfg),
                        sort_keys=True, separators=(',', ':')).encode()),
                   'time_origin': 12345.5}
        (self.root / report).parent.mkdir(parents=True, exist_ok=True)
        (self.root / report).write_text(json.dumps({'jobs': [receipt]}))
        self.browser.pages['retained'] = {'url': HOME, 'composer': '', 'text': '',
                                          'messages': False, 'stop': False}
        old_js = self.browser.js
        extra = {'url': HOME, 'time_origin': 12345.5, 'composer_empty': True,
                 'user_count': 0}
        expressions = set()
        def probe(expression):
            if expression.startswith('(()=>{const e=') and 'time_origin' in expression:
                expressions.add(expression)
                return dict(extra)
            return old_js(expression)
        self.browser.js = probe
        def snapshot():
            return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.root.rglob('*') if p.is_file() and '.lock' not in p.name}
        with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()) as live, \
             mock.patch.object(relay.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as admission, \
             mock.patch.object(relay, 'cleanup_tabs', side_effect=AssertionError('retained tab must survive')):
            before = snapshot()
            output = io.StringIO()
            with redirect_stdout(output), mock.patch.object(sys, 'argv',
                    ['gpt6pro_relay.py', 'reconcile-unsent-model', '--job', job, '--report', report]):
                self.assertEqual(relay.main(), 0)
            self.assertIn('WOULD_RECONCILE_UNSENT_READINESS', output.getvalue())
            self.assertEqual(snapshot(), before)
            self.assertIn('require-held', admission.call_args.args[0])
            # Each refusal is evaluated through the registered method in both modes.
            mutations = [
                ('typed', lambda s: s.update(typed=True)),
                ('sent', lambda s: s.update(sent=True)),
                ('unknown', lambda s: s.update(typed='UNKNOWN')),
                ('bound', lambda s: s.update(conversation_url=WANT)),
                ('error-typed', lambda s: s['error'].update(typed=True)),
                ('error-sent', lambda s: s['error'].update(sent=True)),
                ('human-original', lambda s: s['error']['state'].update(human_challenge=True)),
                ('login-original', lambda s: s['error']['state'].update(logged_out=True)),
                ('rate-original', lambda s: s['error']['state'].update(rate_limited=True)),
                ('wrong-reason', lambda s: s.update(reason='APP_NOT_READY')),
                ('error-reason', lambda s: s['error'].update(error='HUMAN_CHALLENGE')),
                ('target', lambda s: s['error'].update(tab='sibling')),
            ]
            for name, mutate in mutations:
                with self.subTest(guard=name):
                    changed = copy.deepcopy(state); mutate(changed)
                    (d / 'JOB.json').write_text(json.dumps(changed))
                    for apply in (False, True):
                        prior = snapshot()
                        with self.assertRaises(SystemExit):
                            relay.reconcile_unsent_model(job, apply=apply, report=report)
                        self.assertEqual(snapshot(), prior)
                    (d / 'JOB.json').write_bytes(preimage)
            before = snapshot()  # reset mtime baseline after our fixture mutations
            for key, value in [('security_interstitial', True), ('human_challenge', True),
                               ('logged_out', True), ('rate_limited', True), ('stop', True),
                               ('composer_present', False), ('assistant_count', 1),
                               ('ready_state', 'loading')]:
                with self.subTest(current_page=key):
                    self.browser.app_overrides[key] = value
                    for apply in (False, True):
                        with self.assertRaisesRegex(SystemExit, 'READINESS_RECONCILE_REFUSED_TAB_EVIDENCE'):
                            relay.reconcile_unsent_model(job, apply=apply, report=report)
                    self.browser.app_overrides.clear()
                    self.assertEqual(snapshot(), before)
            for key, value in [('composer_empty', False), ('user_count', 1),
                               ('time_origin', 999), ('url', WANT)]:
                saved = extra[key]; extra[key] = value
                with self.subTest(extra=key), self.assertRaisesRegex(SystemExit, 'REFUSED_TAB_EVIDENCE'):
                    relay.reconcile_unsent_model(job, report=report)
                extra[key] = saved
            self.browser.pages['retained']['url'] = WANT
            with self.assertRaisesRegex(SystemExit, 'REFUSED_TAB_EVIDENCE'):
                relay.reconcile_unsent_model(job, report=report)
            retained = self.browser.pages.pop('retained')
            with self.assertRaisesRegex(SystemExit, 'REFUSED_TAB_EVIDENCE'):
                relay.reconcile_unsent_model(job, report=report)
            retained['url'] = HOME; self.browser.pages['retained'] = retained
            self.browser.active = 'sibling'
            self.browser.wrong_switch = True
            with self.assertRaisesRegex(SystemExit, 'REFUSED_TAB_EVIDENCE'):
                relay.reconcile_unsent_model(job, report=report)
            self.browser.wrong_switch = False
            live.return_value = {job}
            with self.assertRaisesRegex(SystemExit, 'REFUSED_LIVE_RUNNER'):
                relay.reconcile_unsent_model(job, report=report)
            live.return_value = set()
            admission.return_value.returncode = 1
            with self.assertRaisesRegex(SystemExit, 'REFUSED_ADMISSION'):
                relay.reconcile_unsent_model(job, report=report)
            admission.return_value.returncode = 0
            for key, value in [('job_sha256', '0' * 64), ('message_sha256', '0' * 64),
                               ('ledger_rows_sha256', '0' * 64), ('error_tab', 'other'),
                               ('submission_bound', True), ('typed', True), ('sent', True)]:
                bad = {**receipt, key: value}
                (self.root / report).write_text(json.dumps({'jobs': [bad]}))
                with self.subTest(report=key), self.assertRaisesRegex(SystemExit, 'REFUSED_REPORT'):
                    relay.reconcile_unsent_model(job, report=report)
            (self.root / report).write_text(json.dumps({'jobs': [receipt]}))
            relay.ledger_append(self.cfg, {'job': job, 'event': 'submission_bound'})
            with self.assertRaisesRegex(SystemExit, 'REFUSED_LEDGER'):
                relay.reconcile_unsent_model(job, report=report)
            ledger.write_bytes(ledger_preimage)
            # Late drift in the real browser read refuses before any backup/write.
            self.browser.on_run = lambda: (d / 'MESSAGE.md').write_bytes(b'moved')
            with self.assertRaisesRegex(SystemExit, 'REFUSED_STATE_DRIFT'):
                relay.reconcile_unsent_model(job, apply=True, report=report)
            self.browser.on_run = lambda: None
            (d / 'MESSAGE.md').write_bytes(message)
            backup = d / ('JOB.pre-readiness-reconcile-' + digest(preimage) + '.json')
            backup.write_bytes(b'conflict')
            with self.assertRaisesRegex(SystemExit, 'REFUSED_BACKUP_CONFLICT'):
                relay.reconcile_unsent_model(job, apply=True, report=report)
            backup.unlink()
            self.assertEqual(relay.reconcile_unsent_model(job, apply=True, report=report), 0)
            self.assertEqual(backup.read_bytes(), preimage)
            updated = relay.job_state(job)
            self.assertEqual(updated['reason'], state['reason'])
            self.assertEqual(updated['error'], state['error'])
            self.assertIs(updated['retry_blocked'], True)  # reconciliation is not replay permission
            self.assertEqual((d / 'MESSAGE.md').read_bytes(), message)
            self.assertIn('retained', self.browser.pages)
            with self.assertRaisesRegex(SystemExit, 'REFUSED_STATE'):
                relay.reconcile_unsent_model(job, apply=True, report=report)
            events = relay.ledger_rows(self.cfg)
            self.assertEqual([(r['event'], r.get('phase')) for r in events],
                             [('submitted', None), ('readiness_unsent_reconcile', 'STARTED'),
                              ('readiness_unsent_reconcile', 'APPLIED')])
            self.assertFalse(self.browser.created or self.browser.inserts or self.browser.sent or self.browser.navigations)
        # Execute the actual new DOM expression, not a rewritten Python oracle.
        self.assertEqual(len(expressions), 1)
        expression = expressions.pop()
        for primary, fallback, text, users, empty in [
                (True, False, '', 0, True), (False, True, ' \n', 0, False),
                (True, False, 'draft', 0, False), (False, False, '', 0, False),
                (True, False, '', 1, True), (False, True, '', 0, True)]:
            # Exact logical DOM contract, not the obsolete innerText-only stub.
            setup = ('const location={href:' + json.dumps(HOME) + '};'
                     'const performance={timeOrigin:12345.5};const text=' + json.dumps(text) + ';'
                     'const attrs=text?{}:{"data-empty-paragraph":"true","data-placeholder":"Ask ChatGPT","class":"placeholder"};'
                     'const p={nodeType:1,tagName:"P",attributes:Object.keys(attrs),getAttribute:k=>attrs[k]??null,'
                     'hasAttribute:k=>k in attrs,childNodes:text?[{nodeType:3,data:text}]:[{nodeType:1,tagName:"BR",className:"ProseMirror-trailingBreak",attributes:["class"]}]};'
                     'const e={tagName:"DIV",getAttribute:k=>k==="contenteditable"?"true":null,'
                     'classList:{contains:k=>k==="ProseMirror"},childNodes:[p]};'
                     'const document={querySelector:s=>s==="#prompt-textarea"?'
                     + ('e' if primary else 'null') + ':' + ('e' if fallback else 'null') + ','
                     'querySelectorAll:s=>Array(' + str(users) + ').fill({})};')
            result = subprocess.run(['node', '-e', setup + 'console.log(JSON.stringify(' + expression + '))'],
                                    capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), {'url': HOME, 'time_origin': 12345.5,
                             'composer_empty': empty, 'user_count': users})

    def test_proof_bound_preinsert_terminal_actual_cli(self):
        """Reusable report-bound disposition; no replay or stale UNKNOWN release."""
        sha = lambda b: relay.hashlib.sha256(b).hexdigest()
        job, tab, origin = 'unlisted-preinsert-job', 'attributed-target', 1791334401239.7
        d = relay.job_dir(job)
        prompt = b'preserved original packet'
        (d / 'PROMPT.md').write_bytes(prompt)
        (d / 'drive-input').mkdir()
        (d / 'drive-input/PROMPT.md').write_bytes(prompt)
        held = {'job': job, 'account': 'account-a', 'job_class': 'review', 'status': 'failed',
            'reason': 'WRONG_MODEL', 'retry_blocked': True, 'typed': False, 'sent': False,
            'reply_token': 'b'*64, 'ts': '2026-10-07T00:53:31+00:00',
            'input_sha256': sha(json.dumps([('PROMPT.md', sha(prompt))], separators=(',', ':')).encode()),
            **{k: 'fixture-'+k for k in relay.PROVENANCE_FIELDS},
            'error': {'error': 'WRONG_MODEL', 'typed': False, 'sent': False,
                      'state': {'url': HOME, 'stop': False, 'assistant_count': 0, 'last_len': 0}}}
        relay.job_write(job, held)
        preimage = (d / 'JOB.json').read_bytes()
        message = relay._failed_generation_identity(held).encode()
        (d / 'MESSAGE.md').write_bytes(message)
        manifest = {k: held[k] for k in (*relay.PROVENANCE_FIELDS, 'job', 'job_class')}
        manifest.update(attach=[], drive_attach=[])
        (d / 'MANIFEST.json').write_text(json.dumps(manifest))
        ledger = relay.ledger_path(self.cfg)
        ledger.parent.mkdir(parents=True, exist_ok=True)
        row = {'ts': '2026-10-07T00:53:08+00:00', 'event': 'submitted', 'job': job,
               'account': 'account-a', 'kind': 'initial', 'certainty': 'reserved_may_send',
               **{k: held[k] for k in relay.PROVENANCE_FIELDS}}
        ledger.write_text(json.dumps(row)+'\n')
        before_ledger = ledger.read_bytes()
        report = 'reports/proof-bound-facts.json'
        rp = self.root / report
        rp.parent.mkdir(parents=True)
        evidence = {}
        for key, data in [('preflight', {'targets': [{'target_id': 'old-target'}]}),
                          ('observation', {'browser': {'targets_before': [{'target_id': tab, 'url': HOME}],
                              'metadata': [{'target_id': tab, 'url': HOME, 'time_origin': origin}]}})]:
            p = rp.with_name(key+'.json')
            p.write_text(json.dumps(data))
            evidence[key] = {'path': str(p.relative_to(self.root)), 'sha256': sha(p.read_bytes())}
        facts = {'job': job, 'disposition': 'terminal_unsent_preinsert', 'reason': 'WRONG_MODEL',
            'submission_bound': False, 'typed': False, 'sent': False, 'job_sha256': sha(preimage),
            'message_sha256': sha(message), 'manifest_sha256': sha((d/'MANIFEST.json').read_bytes()),
            'drive_prompt_sha256': sha(prompt), 'error_tab': tab, 'time_origin': origin,
            'ledger_preimage_sha256': sha(before_ledger), 'ledger_preimage_bytes': len(before_ledger),
            'ledger_line_sha256': [sha(before_ledger.rstrip(b'\n'))],
            'ledger_rows_sha256': sha(json.dumps([row], sort_keys=True, separators=(',', ':')).encode()),
            'target_evidence': evidence}
        rp.write_text(json.dumps({'jobs': [facts]}))
        def call(*args):
            with mock.patch.object(sys, 'argv', ['relay', 'reconcile-unsent-model', '--job', job,
                                                 '--report', report, *args]), redirect_stdout(io.StringIO()) as out:
                code = relay.main()
                return code, out.getvalue()
        with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()), \
             mock.patch.object(relay.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)), \
             mock.patch.object(relay, '_retained_empty_witness', return_value=True) as witness, \
             mock.patch.object(relay, '_legacy_tabs', return_value=[{'type': 'page', 'url': HOME}]), \
             mock.patch.object(relay, 'cleanup_tabs', side_effect=AssertionError('NO CLEANUP')), \
             mock.patch.object(relay, 'send_message', side_effect=AssertionError('NO SEND')), \
             mock.patch.object(relay, 'drive', side_effect=AssertionError('NO DRIVE')):
            code, text = call()
            self.assertEqual(code, 0)
            self.assertIn('WOULD_TERMINALIZE_UNSENT', text)
            self.assertEqual((d/'JOB.json').read_bytes(), preimage)
            self.assertEqual(ledger.read_bytes(), before_ledger)
            witness.assert_called_with(tab, origin)
            for key, value in [('typed', True), ('sent', 'UNKNOWN')]:
                relay.job_write(job, {**held, key: value})
                changed = sha((d/'JOB.json').read_bytes())
                rp.write_text(json.dumps({'jobs': [{**facts, 'job_sha256': changed}]}))
                with self.assertRaisesRegex(SystemExit, 'REFUSED_CUSTODY'):
                    call('--apply', '--authorize-preimage', changed)
                (d/'JOB.json').write_bytes(preimage)
                rp.write_text(json.dumps({'jobs': [facts]}))
            witness.return_value = False
            with self.assertRaisesRegex(SystemExit, 'TAB_EVIDENCE'): call()
            witness.return_value = True
            with self.assertRaises(SystemExit): call('--apply')
            self.assertEqual(call('--apply', '--authorize-preimage', sha(preimage))[0], 0)
            terminal = relay.job_state(job)
            self.assertEqual({k:v for k,v in terminal.items() if k != 'terminal_unsent'}, held)
            self.assertEqual((d/('JOB.pre-terminal-unsent-'+sha(preimage)+'.json')).read_bytes(), preimage)
            self.assertTrue(ledger.read_bytes().startswith(before_ledger))
            self.assertTrue(relay._verified_terminal_unsent(terminal))
            self.assertEqual([r['phase'] for r in relay.ledger_rows(self.cfg)[1:]], ['STARTED', 'APPLIED'])
            with self.assertRaisesRegex(SystemExit, 'REFUSED_DUPLICATE'): relay.cmd_submit(self.args(job))
            with self.assertRaises(SystemExit): call('--apply', '--authorize-preimage', sha(preimage))
            saved = ledger.read_bytes()
            ledger.write_bytes(b'\n'.join(saved.splitlines()[:-1])+b'\n')
            self.assertFalse(relay._verified_terminal_unsent(terminal))
            ledger.write_bytes(saved)
            # Exercise the real cleanup caller, stopping before its browser code.
            with mock.patch.object(relay, 'cleanup_tabs', REAL_CLEANUP_TABS):
                with mock.patch.object(relay, 'bu_json', side_effect=AssertionError('STOP BEFORE BROWSER')) as browser:
                    with self.assertRaisesRegex(AssertionError, 'STOP BEFORE BROWSER'): relay.cleanup_tabs()
                    script = browser.call_args.args[0]
                    assignment = next(n for n in ast.walk(ast.parse(script)) if isinstance(n, ast.Assign)
                                      and any(isinstance(t, ast.Name) and t.id == 'protected' for t in n.targets))
                    self.assertIn(tab, ast.literal_eval(assignment.value.args[0]))

            # Different-ID admission is checked through the actual submit caller,
            # then deliberately stopped before cleanup, upload or send.
            with self.assertRaisesRegex(AssertionError, 'NO CLEANUP'): relay.cmd_submit(self.args('fresh-distinct'))
            relay.job_write(job, {**terminal, 'job': 'drift'})
            self.assertFalse(relay._verified_terminal_unsent(terminal))

    def test_current_obsolete_unsent_disposition_actual_cli(self):
        """No replay: exact WRONG_MODEL/readiness pins, UNKNOWN stays held."""
        import copy
        sha = lambda b: relay.hashlib.sha256(b).hexdigest()
        jobs = [('glp44-canonical-sensor-partition-20261006', 'failed', 'WRONG_MODEL'),
                ('pro-glp96-intake-design-closer-20261003', 'unsent_reconciled', 'SECURITY_INTERSTITIAL')]
        pins, originals = {}, {}
        report = 'reports/current-terminal-facts.json'
        rp = self.root / report
        rp.parent.mkdir(parents=True)
        facts = []
        for job, status, reason in jobs:
            d = relay.job_dir(job)
            prompt = b'original obsolete packet'
            (d / 'PROMPT.md').write_bytes(prompt)
            (d / 'drive-input').mkdir()
            (d / 'drive-input/PROMPT.md').write_bytes(prompt)
            tab = 'retained-challenge' if reason == 'SECURITY_INTERSTITIAL' else None
            origin = 12345.5 if tab else None
            original = {'job': job, 'account': 'account-a', 'job_class': 'review',
                'status': 'failed', 'reason': reason, 'retry_blocked': True,
                'typed': False, 'sent': False, 'reply_token': 'b'*64,
                'input_sha256': sha(json.dumps([('PROMPT.md', sha(prompt))], separators=(',', ':')).encode()),
                **{k: 'fixture-'+k for k in relay.PROVENANCE_FIELDS},
                'error': {'error': reason, 'typed': False, 'sent': False,
                    'state': {'url': HOME, 'model': None, 'stop': False,
                              'assistant_count': 0, 'last_len': 0}}}
            if tab:
                original['error']['tab'] = tab
            old_bytes = json.dumps(original).encode()
            backup = sha(old_bytes) if tab else None
            held = dict(original)
            if tab:
                (d / ('JOB.pre-readiness-reconcile-'+backup+'.json')).write_bytes(old_bytes)
                held.update(status=status, reconciled_at='2026-10-03T00:59:49+00:00', preimage_sha256=backup)
            relay.job_write(job, held)
            originals[job] = (d / 'JOB.json').read_bytes()
            message = relay._failed_generation_identity(held).encode()
            (d / 'MESSAGE.md').write_bytes(message)
            manifest = {k: held[k] for k in (*relay.PROVENANCE_FIELDS, 'job', 'job_class')}
            manifest.update(attach=[], drive_attach=[])
            (d / 'MANIFEST.json').write_text(json.dumps(manifest))
            relay.ledger_append(self.cfg, {'event': 'submitted', 'job': job, 'account': 'account-a',
                'kind': 'initial', 'certainty': 'reserved_may_send',
                **{k: held[k] for k in relay.PROVENANCE_FIELDS}})
            if tab:
                event = {'event': 'readiness_unsent_reconcile', 'job': job, 'account': 'account-a',
                    'reason': reason, 'preimage_sha256': backup, 'message_sha256': sha(message),
                    'retained_tab': tab, 'time_origin': origin}
                for phase in ('STARTED', 'APPLIED'):
                    relay.ledger_append(self.cfg, {**event, 'phase': phase})
            lines = [line for line in relay.ledger_path(self.cfg).read_bytes().splitlines()
                     if json.loads(line)['job'] == job]
            c = {'job': job, 'held': sha(originals[job]), 'message': sha(message),
                 'manifest': sha((d / 'MANIFEST.json').read_bytes()),
                 'drive_prompt': sha(prompt), 'ledger_lines': tuple(map(sha, lines)),
                 'backup': backup, 'tab': tab, 'time_origin': origin,
                 'status': status, 'reason': reason}
            pins[job] = c
            facts.append({'job': job, 'disposition': 'terminal_unsent_obsolete', 'reason': reason,
                'typed': False, 'sent': False, 'submission_bound': False,
                'job_sha256': c['held'], 'message_sha256': c['message'], 'error_tab': tab,
                'time_origin': origin, 'ledger_rows_sha256': sha(json.dumps(
                    list(map(json.loads, lines)), sort_keys=True, separators=(',', ':')).encode())})
        rp.write_text(json.dumps({'jobs': facts}))
        unknown = self.job(relay.CONSUMED_CANARY['job'], url=None, tab=None, status='failed',
            typed=True, sent='UNKNOWN', retry_blocked=True, reason='RETAINED_RESUME_UNCERTAIN',
            error={'error': 'SECURITY_INTERSTITIAL', 'typed': False, 'sent': False})
        def call(job, *extra):
            with mock.patch.object(sys, 'argv', ['relay', 'reconcile-unsent-model', '--job', job,
                                               '--report', report, *extra]), redirect_stdout(io.StringIO()) as out:
                return relay.main(), out.getvalue()
        def snapshot():
            return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.root.rglob('*') if p.is_file()}
        with relay.account_lock(), relay.global_lock():
            pass
        with mock.patch.object(relay, 'TERMINAL_UNSENT_CURRENT', pins, create=True), \
             mock.patch.object(relay, '_legacy_live_jobs', return_value=set()) as live, \
             mock.patch.object(relay.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)), \
             mock.patch.object(relay, '_retained_empty_witness', return_value=True) as witness, \
             mock.patch.object(relay, '_legacy_tabs', return_value=[{'type': 'page', 'url': HOME}]), \
             mock.patch.object(relay, 'bu', side_effect=AssertionError('NO OTHER BROWSER')), \
             mock.patch.object(relay, 'drive', side_effect=AssertionError('NO DRIVE')), \
             mock.patch.object(relay, 'send_message', side_effect=AssertionError('NO SEND')), \
             mock.patch.object(relay, 'cleanup_tabs', side_effect=AssertionError('NO CLEANUP')):
            for job, _, _ in jobs:
                c = pins[job]
                before = snapshot()
                self.assertEqual(call(job)[0], 0)
                self.assertEqual(snapshot(), before)
                for label, mutate, restore in [
                    ('authority', lambda: None, lambda: None),
                    ('UNKNOWN overrides stale error', lambda: relay.job_write(job, {**json.loads(originals[job]), 'sent': 'UNKNOWN'}),
                        lambda: (relay.JOBS / job / 'JOB.json').write_bytes(originals[job])),
                    ('binding', lambda: relay.ledger_append(self.cfg, {'job': job, 'event': 'submission_bound'}),
                        lambda: relay.ledger_path(self.cfg).write_bytes(saved_ledger)),
                    ('live runner', lambda: setattr(live, 'return_value', {job}), lambda: setattr(live, 'return_value', set()))]:
                    saved_ledger = relay.ledger_path(self.cfg).read_bytes()
                    with self.subTest(job=job, guard=label):
                        mutate()
                        pre = snapshot()
                        with self.assertRaises(SystemExit):
                            call(job, '--apply', '--authorize-preimage', '0'*64 if label == 'authority' else c['held'])
                        self.assertEqual(snapshot(), pre)
                        restore()
                if c['tab']:
                    witness.return_value = False
                    with self.assertRaises(SystemExit): call(job)
                    witness.return_value = True
                pre_ledger = relay.ledger_path(self.cfg).read_bytes()
                self.assertEqual(call(job, '--apply', '--authorize-preimage', c['held'])[0], 0)
                terminal = relay.job_state(job)
                self.assertEqual({k:v for k,v in terminal.items() if k != 'terminal_unsent'}, json.loads(originals[job]))
                self.assertIs(terminal['retry_blocked'], True)
                self.assertEqual((relay.JOBS / job / ('JOB.pre-terminal-unsent-'+c['held']+'.json')).read_bytes(), originals[job])
                self.assertTrue(relay.ledger_path(self.cfg).read_bytes().startswith(pre_ledger))
                self.assertTrue(relay._verified_terminal_unsent(terminal))
                with self.assertRaisesRegex(SystemExit, 'REFUSED_DUPLICATE'): relay.cmd_submit(self.args(job))
                with self.assertRaises(SystemExit): call(job, '--apply', '--authorize-preimage', c['held'])
                # Both admission callers must reject projected/raw identity drift and interrupted apply.
                relay.job_write(job, {**terminal, 'job': 'drift'})
                self.assertFalse(relay._verified_terminal_unsent({**terminal, 'job': job}))
                with self.assertRaisesRegex(SystemExit, 'BINDING_UNVERIFIED'): relay.cmd_submit(self.args('future'))
                relay.job_write(job, terminal)
                applied = relay.ledger_path(self.cfg).read_bytes()
                relay.ledger_path(self.cfg).write_bytes(b'\n'.join(applied.splitlines()[:-1])+b'\n')
                self.assertFalse(relay._verified_terminal_unsent(terminal))
                relay.ledger_path(self.cfg).write_bytes(applied)
            self.assertEqual([r['job'] for r in relay.active_registry()], [unknown['job']])
            self.assertEqual(relay.job_state(unknown['job']), unknown)
            with self.assertRaisesRegex(SystemExit, 'BINDING_UNVERIFIED'): relay.cmd_submit(self.args('future'))
            # Different account can be admitted; old UNKNOWN cannot be retired by a forged receipt.
            self.cfg['accounts'].append({'label': 'account-b', 'cdp_port': 9341, 'weekly_messages': 100})
            relay.job_write(unknown['job'], {**unknown, 'account': 'account-b'})
            with self.assertRaisesRegex(AssertionError, 'NO CLEANUP'): relay.cmd_submit(self.args('future'))
            self.assertEqual(relay.job_state('future')['status'], 'submitting')

    def test_pr52_terminal_unsent_actual_cli(self):
        """Exact obsolete preinsert job: no replay; only different-ID admission."""
        import copy
        sha = lambda b: relay.hashlib.sha256(b).hexdigest()
        job = 'pro-glp318-pr52-44f52bba-integration-review-20260927'
        tab, origin = 'D3DD42837C1937487F27C4C6870EF9D9', 1790488400308.6
        d = relay.job_dir(job)
        prompt = b'original obsolete PR52 fixture'
        (d / 'PROMPT.md').write_bytes(prompt)
        (d / 'drive-input').mkdir()
        (d / 'drive-input/PROMPT.md').write_bytes(prompt)
        original = {'job': job, 'account': 'account-a', 'job_class': 'review',
                    'status': 'failed', 'reason': 'SECURITY_INTERSTITIAL', 'retry_blocked': True,
                    'typed': False, 'sent': False, 'reply_token': 'b'*64,
                    'input_sha256': sha(json.dumps([('PROMPT.md', sha(prompt))], separators=(',', ':')).encode()),
                    **{k: 'fixture-'+k for k in relay.PROVENANCE_FIELDS},
                    'error': {'error': 'SECURITY_INTERSTITIAL', 'tab': tab, 'typed': False, 'sent': False}}
        original_bytes = json.dumps(original).encode()
        old_backup = d / ('JOB.pre-retained-resume-' + sha(original_bytes) + '.json')
        old_backup.write_bytes(original_bytes)
        message = relay._failed_generation_identity(original).encode()
        (d / 'MESSAGE.md').write_bytes(message)
        manifest = {k: original[k] for k in (*relay.PROVENANCE_FIELDS, 'job', 'job_class')}
        manifest.update(attach=[], drive_attach=[])
        (d / 'MANIFEST.json').write_text(json.dumps(manifest))
        attempt = {'job': job, 'account': 'account-a', 'preimage_sha256': sha(original_bytes),
                   'message_sha256': sha(message), 'report_sha256': 'e'*64,
                   'retained_tab': tab, 'attempt_at': '2026-09-27T12:50:55+00:00'}
        held = {**original, 'reason': 'RETAINED_RESUME_UNCERTAIN', 'resume_attempt': attempt,
                'resume_error': {'error': 'composer_not_empty', 'typed': False, 'sent': False, 'tab': tab}}
        relay.job_write(job, held)
        held_bytes = (d / 'JOB.json').read_bytes()
        relay.ledger_append(self.cfg, {'event': 'submitted', 'job': job, 'account': 'account-a',
            'kind': 'initial', 'certainty': 'reserved_may_send',
            **{k: original[k] for k in relay.PROVENANCE_FIELDS}})
        relay.ledger_append(self.cfg, {'event': 'retained_resume', 'phase': 'STARTED', **attempt})
        ledger = relay.ledger_path(self.cfg)
        ledger_bytes = ledger.read_bytes()
        c = {'job': job, 'held': sha(held_bytes), 'backup': sha(original_bytes),
             'message': sha(message), 'manifest': sha((d / 'MANIFEST.json').read_bytes()),
             'drive_prompt': sha(prompt), 'ledger_lines': tuple(map(sha, ledger_bytes.splitlines())),
             'tab': tab, 'time_origin': origin}
        report = 'reports/terminal-facts.json'
        rp = self.root / report
        rp.parent.mkdir(parents=True)
        row = {'job': job, 'disposition': 'terminal_unsent_obsolete', 'reason': held['reason'],
               'submission_bound': False, 'typed': False, 'sent': False,
               'job_sha256': c['held'], 'message_sha256': c['message'], 'error_tab': tab,
               'time_origin': origin, 'ledger_rows_sha256': sha(json.dumps(relay.ledger_rows(self.cfg),
                    sort_keys=True, separators=(',', ':')).encode())}
        rp.write_text(json.dumps({'jobs': [row]}))
        report_bytes = rp.read_bytes()
        self.browser.pages[tab] = {'url': HOME, 'composer': '', 'text': '', 'messages': False,
                                   'stop': False, 'origin': origin}
        real_js = self.browser.js
        def probe(expr):
            if expr.startswith('(()=>{const e=') and 'time_origin' in expr:
                p = self.browser.pages[tab]
                return {'url': p['url'], 'time_origin': p['origin'],
                        'composer_empty': p['composer'] == '', 'user_count': int(p['messages'])}
            return real_js(expr)
        self.browser.js = probe
        def call(*extra):
            with mock.patch.object(sys, 'argv', ['gpt6pro_relay.py', 'reconcile-unsent-model',
                    '--job', job, '--report', report, *extra]), redirect_stdout(io.StringIO()) as out:
                return relay.main(), out.getvalue()
        def submit(identity):
            with mock.patch.object(sys, 'argv', ['gpt6pro_relay.py', 'submit', '--job', identity,
                    '--account', 'account-a', '--job-class', 'review', '--prompt', str(d / 'PROMPT.md'),
                    *[arg for k in relay.PROVENANCE_FIELDS for arg in ('--'+k.replace('_', '-'), 'fixture')]]):
                return relay.main()
        def snapshot():
            return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
                    for p in self.root.rglob('*') if p.is_file()}
        # Pre-existing lockfiles let default proof demonstrate zero writes including mtimes.
        with relay.account_lock(), relay.global_lock():
            pass
        with mock.patch.object(relay, 'TERMINAL_UNSENT_PR52', c, create=True), \
             mock.patch.object(relay, '_legacy_live_jobs', return_value=set()) as live, \
             mock.patch.object(relay.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as admission, \
             mock.patch.object(relay, 'send_message', side_effect=AssertionError('NO SEND')), \
             mock.patch.object(relay, 'drive', side_effect=AssertionError('NO DRIVE')), \
             mock.patch.object(relay, 'cleanup_tabs', side_effect=AssertionError('NO CLEANUP')):
            before = snapshot()
            code, text = call()
            self.assertEqual(code, 0)
            self.assertIn('WOULD_TERMINALIZE_UNSENT', text)
            self.assertEqual(snapshot(), before)
            self.assertIn('require-held', admission.call_args.args[0])
            self.assertEqual([r['job'] for r in relay.active_registry()], [job])
            with self.assertRaisesRegex(SystemExit, 'REFUSED_DUPLICATE'):
                submit(job)
            with self.assertRaisesRegex(SystemExit, 'BINDING_UNVERIFIED'):
                submit('distinct-future-review')
            self.cfg['envelope']['max_concurrent_chats_per_account'] = 1
            with self.assertRaisesRegex(SystemExit, 'CAPACITY_ACCOUNT'):
                relay.admit('distinct-future-review', self.cfg['accounts'][0], self.cfg, {})
            def refused(label, action=lambda: None, restore=lambda: None, args=None):
                with self.subTest(guard=label):
                    action()
                    pre = snapshot()
                    with self.assertRaises(SystemExit):
                        call(*(args if args is not None else ('--apply', '--authorize-preimage', c['held'])))
                    self.assertEqual(snapshot(), pre)
                    restore()
            refused('apply needs authority', args=['--apply'])
            refused('authority needs apply', args=['--authorize-preimage', c['held']])
            refused('old authority', args=['--apply', '--authorize-preimage', c['backup']])
            for key, value in [('typed', True), ('sent', 'UNKNOWN'), ('reason', 'OTHER'),
                               ('conversation_url', WANT), ('terminal_unsent', {'verified': True}),
                               ('preinsert_cancel', {'terminal': True, 'verified': True})]:
                refused(key, lambda k=key,v=value: relay.job_write(job, {**held,k:v}),
                        lambda: (d / 'JOB.json').write_bytes(held_bytes))
            for p in (old_backup, d / 'MESSAGE.md', d / 'PROMPT.md', d / 'MANIFEST.json'):
                saved = p.read_bytes()
                refused('custody '+p.name, lambda p=p: p.write_bytes(b'drift'),
                        lambda p=p,saved=saved: p.write_bytes(saved))
            refused('report', lambda: rp.write_text(json.dumps({'jobs': [{**row, 'sent': 'UNKNOWN'}]})),
                    lambda: rp.write_bytes(report_bytes))
            for name in ('RESPONSE.md', 'outbox'):
                p = d / name
                refused(name, lambda p=p: p.write_bytes(b'output'), lambda p=p: p.unlink())
            refused('bound ledger', lambda: ledger.write_bytes(ledger_bytes + json.dumps(
                {'job':job,'event':'submission_bound'}).encode()+b'\n'), lambda: ledger.write_bytes(ledger_bytes))
            refused('malformed ledger', lambda: ledger.write_bytes(ledger_bytes+b'{\n'),
                    lambda: ledger.write_bytes(ledger_bytes))
            refused('live runner', lambda: setattr(live,'return_value',{job}),
                    lambda: setattr(live,'return_value',set()))
            refused('heavy not held', lambda: setattr(admission.return_value,'returncode',1),
                    lambda: setattr(admission.return_value,'returncode',0))
            page = copy.deepcopy(self.browser.pages[tab])
            refused('absent target', lambda: self.browser.pages.pop(tab),
                    lambda: self.browser.pages.__setitem__(tab,dict(page)))
            for key,value in [('composer','draft'), ('composer',' \n'), ('url',WANT),
                              ('origin',999), ('text','output'), ('messages',True), ('stop',True)]:
                refused('target '+key, lambda k=key,v=value: self.browser.pages[tab].update({k:v}),
                        lambda: self.browser.pages.__setitem__(tab,dict(page)))
            refused('human challenge', lambda: self.browser.app_overrides.update(human_challenge=True),
                    self.browser.app_overrides.clear)
            # Recheck after the exact target observation: late output must not terminalize.
            self.browser.on_run = lambda: (d / 'RESPONSE.md').write_bytes(b'late')
            with self.assertRaises(SystemExit):
                call('--apply', '--authorize-preimage', c['held'])
            self.browser.on_run = lambda: None
            (d / 'RESPONSE.md').unlink()
            self.assertEqual((d / 'JOB.json').read_bytes(), held_bytes)
            self.assertEqual(ledger.read_bytes(), ledger_bytes)
            self.assertEqual(call('--apply', '--authorize-preimage', c['held'])[0], 0)
            terminal = relay.job_state(job)
            self.assertEqual({k:v for k,v in terminal.items() if k != 'terminal_unsent'}, held)
            self.assertIs(terminal['retry_blocked'], True)
            backup = d / ('JOB.pre-terminal-unsent-' + c['held'] + '.json')
            self.assertEqual(backup.read_bytes(), held_bytes)
            self.assertEqual(old_backup.read_bytes(), original_bytes)
            self.assertEqual((d / 'MESSAGE.md').read_bytes(), message)
            self.assertTrue(ledger.read_bytes().startswith(ledger_bytes))
            self.assertEqual([(r['event'],r.get('phase')) for r in relay.ledger_rows(self.cfg)],
                [('submitted',None),('retained_resume','STARTED'),
                 ('terminal_unsent','STARTED'),('terminal_unsent','APPLIED')])
            self.assertEqual(relay.active_registry(), [])
            for args in ((), ('--apply','--authorize-preimage',c['held'])):
                with self.assertRaises(SystemExit):
                    call(*args)
            with self.assertRaisesRegex(SystemExit, 'REFUSED_DUPLICATE'):
                submit(job)
            # A dict flag, interrupted journal, or changed proof can never release capacity.
            applied_ledger = ledger.read_bytes()
            ledger.write_bytes(b'\n'.join(applied_ledger.splitlines()[:-1])+b'\n')
            self.assertEqual(len(relay.active_registry()), 1)
            with self.assertRaisesRegex(SystemExit, 'BINDING_UNVERIFIED'):
                submit('distinct-future-review')
            ledger.write_bytes(applied_ledger)
            backup.write_bytes(b'drift')
            self.assertEqual(len(relay.active_registry()), 1)
            backup.write_bytes(held_bytes)
            other = {**held, 'job': relay.CONSUMED_CANARY['job'], 'typed': True, 'sent': 'UNKNOWN',
                     'terminal_unsent': terminal['terminal_unsent'], 'preinsert_cancel': {'verified': True}}
            relay.job_write(other['job'], other)
            self.assertEqual([r['job'] for r in relay.active_registry()], [other['job']])
            with self.assertRaisesRegex(SystemExit, 'BINDING_UNVERIFIED'):
                submit('distinct-future-review')
            (relay.JOBS / other['job'] / 'JOB.json').unlink()  # fixture-only unrelated row
            # Stop after real registered admission, before its cleanup/Drive/sender.
            class AdmissionReached(BaseException):
                pass
            held_snapshot = {str(p):p.read_bytes() for p in d.rglob('*') if p.is_file()}
            with mock.patch.object(relay, 'cleanup_tabs', side_effect=AdmissionReached):
                with self.assertRaises(AdmissionReached):
                    submit('distinct-future-review')
            self.assertEqual(relay.job_state('distinct-future-review')['status'], 'submitting')
            self.assertEqual([r['job'] for r in relay.active_registry()], ['distinct-future-review'])
            self.assertEqual({str(p):p.read_bytes() for p in d.rglob('*') if p.is_file()}, held_snapshot)
        # Real cleanup caller on FakeCDP still protects the original retained tab.
        self.assertEqual(relay.cleanup_tabs(), 1)
        self.assertIn(tab, self.browser.pages)
        self.assertEqual(self.browser.created + self.browser.navigations + self.browser.inserts + self.browser.sent, [])

    def test_terminal_admission_rejects_changed_current_job_identity(self):
        """Actual submit must refuse raw identity drift even with spare capacity."""
        # Reuse only the accepted synthetic setup, not its 29-negative matrix.
        # Keep that prior regression byte-identical, as in the reviewer's probe.
        source = Path(__file__).read_text()
        method = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)
                      and n.name == 'test_pr52_terminal_unsent_actual_cli')
        prefix = []
        for node in method.body:
            if isinstance(node, ast.With) and "'TERMINAL_UNSENT_PR52'" in (ast.get_source_segment(source, node) or ''):
                break
            prefix.append(node)
        else:
            self.fail('terminal fixture setup boundary missing')
        ns = {**globals(), 'self': self}
        exec(compile(ast.Module(body=prefix, type_ignores=[]), __file__, 'exec'), ns, ns)
        c, job, call, submit, snapshot = [ns[k] for k in ('c', 'job', 'call', 'submit', 'snapshot')]
        with mock.patch('socket.socket.connect', side_effect=AssertionError('NO NETWORK')), \
             mock.patch('socket.create_connection', side_effect=AssertionError('NO NETWORK')), \
             mock.patch.object(relay, 'TERMINAL_UNSENT_PR52', c), \
             mock.patch.object(relay, '_legacy_live_jobs', return_value=set()), \
             mock.patch.object(relay.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)), \
             mock.patch.object(relay, 'send_message', side_effect=AssertionError('NO SEND')), \
             mock.patch.object(relay, 'drive', side_effect=AssertionError('NO DRIVE')), \
             mock.patch.object(relay, 'cleanup_tabs', side_effect=AssertionError('NO CLEANUP')):
            self.assertEqual(call('--apply', '--authorize-preimage', c['held'])[0], 0)
            terminal = relay.job_state(job)
            self.assertTrue(relay._verified_terminal_unsent(terminal))
            self.assertEqual(relay.active_registry(), [])
            self.assertEqual(self.cfg['envelope']['max_concurrent_chats_per_account'], 3)
            # Mutate exactly the stored identity; all packet/proof/journal bytes survive.
            relay.job_write(job, {**terminal, 'job': 'changed-current-job-identity'})
            actual = relay.job_state(job)
            self.assertFalse(relay._verified_terminal_unsent(actual))
            self.assertEqual([r['job'] for r in relay.active_registry()], [job])
            before = snapshot()
            with mock.patch.object(relay, 'admit', wraps=relay.admit) as admit, \
                 mock.patch.object(relay, 'job_write', wraps=relay.job_write) as write, \
                 mock.patch.object(relay, 'ledger_append', wraps=relay.ledger_append) as append:
                with self.assertRaisesRegex(SystemExit, 'BINDING_UNVERIFIED'):
                    submit('distinct-future-review')
                admit.assert_not_called()
                write.assert_not_called()
                append.assert_not_called()
            self.assertEqual(snapshot(), before)  # includes MESSAGE/ledger/reservation clocks
            self.assertFalse((relay.JOBS / 'distinct-future-review' / 'JOB.json').exists())
            self.assertFalse((relay.JOBS / 'distinct-future-review' / 'MESSAGE.md').exists())
            self.assertFalse(relay._verified_terminal_unsent(
                relay.sibling_bindings('account-a', 'distinct-future-review')[0]))
            # Valid raw-record control reaches the real admission, before any effect.
            relay.job_write(job, terminal)
            with self.assertRaisesRegex(SystemExit, 'REFUSED_DUPLICATE'):
                submit(job)
            class AdmissionReached(BaseException):
                pass
            with mock.patch.object(relay, 'cleanup_tabs', side_effect=AdmissionReached):
                with self.assertRaises(AdmissionReached):
                    submit('distinct-future-review')
            self.assertEqual(relay.job_state('distinct-future-review')['status'], 'submitting')
            self.assertEqual([r['job'] for r in relay.active_registry()], ['distinct-future-review'])
            self.assertEqual(relay.job_state(job), terminal)
            self.assertEqual(self.browser.created + self.browser.navigations + self.browser.inserts + self.browser.sent, [])

    def test_consumed_canary_forward_recovery_actual_cli(self):
        """Source-only fixture: same registered CLI, real emitted sender, no live files."""
        import hashlib
        sha = lambda data: hashlib.sha256(data).hexdigest()
        job = relay.CONSUMED_CANARY['job']
        d = relay.job_dir(job)
        tab = relay.CONSUMED_CANARY['tab']
        origin = relay.CONSUMED_CANARY['time_origin']
        prompt = b'original six-PR prompt fixture'
        (d / 'PROMPT.md').write_bytes(prompt)
        (d / 'drive-input').mkdir()
        (d / 'drive-input/PROMPT.md').write_bytes(prompt)
        packet_sha = sha(json.dumps([('PROMPT.md', sha(prompt))], separators=(',', ':')).encode())
        original = {'job': job, 'account': 'account-a', 'job_class': 'review',
                    'status': 'failed', 'reason': 'SECURITY_INTERSTITIAL', 'retry_blocked': True,
                    'typed': False, 'sent': False, 'input_sha256': packet_sha,
                    'reply_token': 'b'*64, **{k: 'fixture-'+k for k in relay.PROVENANCE_FIELDS}}
        original['error'] = {'error': 'SECURITY_INTERSTITIAL', 'tab': tab,
                             'typed': False, 'sent': False}
        original_bytes = (json.dumps(original, sort_keys=True) + '\n').encode()
        (d / ('JOB.pre-retained-resume-' + sha(original_bytes) + '.json')).write_bytes(original_bytes)
        message = (relay._failed_generation_identity(original) + '\noriginal packet').encode()
        (d / 'MESSAGE.md').write_bytes(message)
        manifest = {k: original[k] for k in (*relay.PROVENANCE_FIELDS, 'job', 'job_class')}
        manifest.update(attach=[], drive_attach=[])
        manifest_bytes = json.dumps(manifest).encode()
        (d / 'MANIFEST.json').write_bytes(manifest_bytes)
        old_attempt = {'job': job, 'account': 'account-a', 'preimage_sha256': sha(original_bytes),
                       'message_sha256': sha(message), 'report_sha256': 'e'*64,
                       'retained_tab': tab, 'attempt_at': '2026-09-27T09:51:20+00:00'}
        held = {**original, 'status': 'failed', 'reason': 'RETAINED_RESUME_UNCERTAIN',
                'resume_attempt': old_attempt,
                'resume_error': {'error': 'page_evaluation_failed', 'typed': False,
                                 'sent': False, 'tab': tab}}
        relay.job_write(job, held)
        held_bytes = (d / 'JOB.json').read_bytes()
        relay.ledger_append(self.cfg, {'event': 'submitted', 'kind': 'initial',
            'certainty': 'reserved_may_send', 'job': job, 'account': 'account-a',
            **{k: original[k] for k in relay.PROVENANCE_FIELDS}})
        relay.ledger_append(self.cfg, {'event': 'retained_resume', 'phase': 'STARTED', **old_attempt})
        ledger = relay.ledger_path(self.cfg)
        ledger_bytes = ledger.read_bytes()
        lines = ledger_bytes.splitlines()
        rows = relay.ledger_rows(self.cfg)
        report = 'reports/current-canary.json'
        rp = self.root / report
        rp.parent.mkdir(parents=True)
        witness = {'url': HOME, 'time_origin': origin, 'composer_empty': True, 'user_count': 0}
        rp.write_text(json.dumps({'schema': 'pro-consumed-canary-existing-target-witness-v1',
            'status': 'EXACT_TARGET_PASSIVE_WITNESS_HELD_NO_RECOVERY', 'job': job,
            'target': tab, 'witness': {'first': witness, 'last': witness},
            'custody_after': {'files': {'JOB.json': {'sha256': sha(held_bytes)},
                'MESSAGE.md': {'sha256': sha(message)},
                'JOB.pre-retained-resume-' + sha(original_bytes) + '.json': {'sha256': sha(original_bytes)}},
                'ledger_rows': [{'sha256': sha(line)} for line in lines],
                'ordered_input_sha256': original['input_sha256']}}))
        c = dict(relay.CONSUMED_CANARY, held=sha(held_bytes), backup=sha(original_bytes),
                 message=sha(message), manifest=sha(manifest_bytes), drive_prompt=sha(prompt),
                 ledger_lines=tuple(map(sha, lines)), witness_sha256=sha(rp.read_bytes()))
        self.browser.pages[tab] = {'url': HOME, 'composer': '', 'text': '',
                                   'messages': False, 'stop': False, 'origin': origin}
        real_js = self.browser.js
        def probe(expr):
            if expr.startswith('(()=>{const e=') and 'time_origin' in expr:
                return {'url': self.browser.pages[tab]['url'], 'time_origin': origin,
                        'composer_empty': not self.browser.pages[tab]['composer'].strip(), 'user_count': 0}
            if expr.startswith('(()=>{const e=') and 'origin:performance.timeOrigin' in expr:
                p = self.browser.pages[tab]
                return {'origin': origin, 'users': 0, 'assistants': int(bool(p['text'])),
                        'empty': not p['composer'].strip(), 'challenge': bool(self.browser.app_overrides.get('human_challenge')),
                        'interstitial': False, 'login': False, 'rate': False, 'stop': p['stop'],
                        'reconnect': False}
            return real_js(expr)
        self.browser.js = probe
        cli = lambda *args: ['gpt6pro_relay.py', 'resume-unsent-readiness', '--job', job,
                              '--report', report, *args]
        with mock.patch.object(relay, 'CONSUMED_CANARY', c), \
             mock.patch.object(relay, '_legacy_live_jobs', return_value=set()), \
             mock.patch.object(relay.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as admission, \
             mock.patch.object(relay, 'drive', side_effect=AssertionError('NO UPLOAD')), \
             mock.patch.object(relay, 'cleanup_tabs', side_effect=AssertionError('NO CLEANUP')):
            def call(*args):
                with mock.patch.object(sys, 'argv', cli(*args)), redirect_stdout(io.StringIO()) as out:
                    return relay.main(), out.getvalue()
            self.assertEqual(call()[0], 0)
            self.assertEqual((d / 'JOB.json').read_bytes(), held_bytes)
            self.assertEqual(ledger.read_bytes(), ledger_bytes)
            self.assertEqual(list(d.glob('JOB.pre-canary-recovery-*')), [])
            self.assertEqual(self.browser.inserts + self.browser.sent, [])
            self.assertIn('require-held', admission.call_args.args[0])
            def refused(label, action, restore, *args):
                with self.subTest(label=label):
                    action()
                    with self.assertRaises(SystemExit):
                        call(*args)
                    restore()
                    self.assertEqual((d / 'JOB.json').read_bytes(), held_bytes)
                    self.assertEqual(ledger.read_bytes(), ledger_bytes)
                    self.assertEqual(self.browser.inserts + self.browser.sent, [])
            refused('wrong authority', lambda: None, lambda: None, '--authorize-preimage', '0'*64)
            refused('packet drift', lambda: (d / 'PROMPT.md').write_bytes(b'changed'),
                    lambda: (d / 'PROMPT.md').write_bytes(prompt), '--authorize-preimage', sha(held_bytes))
            refused('unexpected binding', lambda: ledger.write_bytes(ledger_bytes +
                    b'{"job":"' + job.encode() + b'","event":"submission_bound"}\n'),
                    lambda: ledger.write_bytes(ledger_bytes), '--authorize-preimage', sha(held_bytes))
            refused('used state', lambda: (d / 'JOB.json').write_bytes(held_bytes + b' '),
                    lambda: (d / 'JOB.json').write_bytes(held_bytes), '--authorize-preimage', sha(held_bytes))
            refused('generic failure', lambda: relay.job_write(job, {**held, 'reason': 'OTHER_FAILURE'}),
                    lambda: (d / 'JOB.json').write_bytes(held_bytes), '--authorize-preimage', sha(held_bytes))
            refused('missing target', lambda: self.browser.pages.pop(tab),
                    lambda: self.browser.pages.__setitem__(tab, {'url': HOME, 'composer': '',
                        'text': '', 'messages': False, 'stop': False, 'origin': origin}),
                    '--authorize-preimage', sha(held_bytes))
            refused('changed target', lambda: self.browser.pages[tab].update(url=WANT),
                    lambda: self.browser.pages[tab].update(url=HOME), '--authorize-preimage', sha(held_bytes))
            refused('nonempty draft', lambda: self.browser.pages[tab].update(composer='draft'),
                    lambda: self.browser.pages[tab].update(composer=''), '--authorize-preimage', sha(held_bytes))
            refused('challenge', lambda: self.browser.app_overrides.update(human_challenge=True),
                    lambda: self.browser.app_overrides.clear(), '--authorize-preimage', sha(held_bytes))
            self.assertEqual(call('--authorize-preimage', sha(held_bytes))[0], 0)
            self.assertEqual(self.browser.inserts, [(tab, message.decode())])
            self.assertEqual(self.browser.sent, [(tab, message.decode())])
            self.assertEqual(self.browser.created + self.browser.navigations, [])
            bound = relay.job_state(job)
            self.assertEqual((bound['status'], bound['retry_blocked']), ('running', False))
            self.assertEqual(bound['resume_history'], [old_attempt])
            self.assertEqual(bound['error'], original['error'])
            self.assertEqual(bound['resume_error'], held['resume_error'])
            self.assertEqual((d / ('JOB.pre-canary-recovery-' + sha(held_bytes) + '.json')).read_bytes(), held_bytes)
            self.assertEqual((d / ('JOB.pre-retained-resume-' + sha(original_bytes) + '.json')).read_bytes(), original_bytes)
            self.assertEqual([r['event'] for r in relay.ledger_rows(self.cfg)],
                             ['submitted', 'retained_resume', 'canary_forward_recovery', 'submission_bound'])
            with self.assertRaises(SystemExit):
                call('--authorize-preimage', sha(held_bytes))
            self.assertEqual(len(self.browser.sent), 1)

    def test_retained_resume_registered_one_shot_and_guards(self):
        """RED/GREEN: actual CLI, exact browser script, fixture JOB/report/ledger."""
        import hashlib
        job = 'fixture-retained-resume'
        d = relay.job_dir(job)
        st = {'job': job, 'account': 'account-a', 'status': 'failed',
              'reason': 'SECURITY_INTERSTITIAL', 'retry_blocked': True,
              'typed': False, 'sent': False, 'input_sha256': 'a' * 64, 'reply_token': 'b' * 64,
              **{k: 'fixture-' + k for k in relay.PROVENANCE_FIELDS}}
        st['error'] = {'error': st['reason'], 'typed': False, 'sent': False, 'tab': 'retained',
                       'state': {'url': HOME, 'model': None, 'assistant_count': 0, 'last_len': 0,
                                 'security_interstitial': True, 'composer_present': False,
                                 **{k: False for k in ('human_challenge', 'logged_out', 'rate_limited',
                                                       'stop', 'drive_reconnect', 'thinking_failed')}}}
        relay.job_write(job, st)
        packet = relay._failed_generation_identity(st) + '\noriginal packet'
        (d / 'MESSAGE.md').write_text(packet)
        relay.ledger_append(self.cfg, {'event': 'submitted', 'kind': 'initial',
            'certainty': 'reserved_may_send', 'job': job, 'account': 'account-a',
            **{k: st[k] for k in relay.PROVENANCE_FIELDS}})
        digest = lambda b: hashlib.sha256(b).hexdigest()
        original = (d / 'JOB.json').read_bytes()
        report = 'reports/fixture-readiness.json'
        rp = self.root / report
        rp.parent.mkdir(parents=True)
        row = {'job': job, 'reason': st['reason'], 'submission_bound': False,
               'typed': False, 'sent': False, 'error_tab': 'retained',
               'job_sha256': digest(original), 'message_sha256': digest(packet.encode()),
               'ledger_rows_sha256': digest(json.dumps(relay.ledger_rows(self.cfg),
                   sort_keys=True, separators=(',', ':')).encode()), 'time_origin': 12345.5}
        rp.write_text(json.dumps({'jobs': [row]}))
        self.browser.pages['retained'] = {'url': HOME, 'composer': '', 'text': '',
                                         'messages': False, 'stop': False}
        original_js = self.browser.js
        def probe(expr):
            if expr.startswith('(()=>{const e=') and 'time_origin' in expr:
                return {'url': self.browser.pages[self.browser.active]['url'],
                        'time_origin': 12345.5, 'composer_empty': True, 'user_count': 0}
            if expr.startswith('(()=>{const e=') and 'origin:performance.timeOrigin' in expr:
                p = self.browser.pages[self.browser.active]
                return {'origin': 12345.5, 'users': int(p.get('users', 0)),
                        'assistants': int(bool(p['text'])), 'empty': not p['composer'].strip(),
                        'challenge': bool(self.browser.app_overrides.get('human_challenge')),
                        'interstitial': bool(self.browser.app_overrides.get('security_interstitial')),
                        'login': bool(self.browser.app_overrides.get('logged_out')),
                        'rate': bool(self.browser.app_overrides.get('rate_limited')),
                        'stop': p['stop'], 'reconnect': bool(self.browser.app_overrides.get('drive_reconnect'))}
            return original_js(expr)
        self.browser.js = probe
        cli = lambda *extra: ['gpt6pro_relay.py', 'resume-unsent-readiness', '--job', job,
                              '--report', report, *extra]
        with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()) as live, \
             mock.patch.object(relay.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)) as admission, \
             mock.patch.object(relay, 'cleanup_tabs', side_effect=AssertionError('no cleanup')), \
             mock.patch.object(relay, 'drive', side_effect=AssertionError('no upload')):
            with mock.patch.object(sys, 'argv', cli()), redirect_stdout(io.StringIO()) as out:
                self.assertEqual(relay.main(), 0)
            self.assertIn('WOULD_RESUME_RETAINED', out.getvalue())
            self.assertEqual((d / 'JOB.json').read_bytes(), original)
            self.assertEqual(self.browser.created + self.browser.navigations + self.browser.sent, [])
            self.assertIn('require-held', admission.call_args.args[0])
            with mock.patch.object(sys, 'argv', cli('--authorize-preimage', '0'*64)), \
                 self.assertRaisesRegex(SystemExit, 'RESUME_REFUSED_AUTHORITY'):
                relay.main()
            live.return_value = {'another-runner'}
            with mock.patch.object(sys, 'argv', cli('--authorize-preimage', digest(original))), \
                 self.assertRaisesRegex(SystemExit, 'RESUME_REFUSED_LIVE_RUNNER'):
                relay.main()
            live.return_value = set()
            for key in ('human_challenge', 'logged_out', 'rate_limited', 'security_interstitial', 'stop'):
                self.browser.app_overrides[key] = True
                with self.subTest(key=key), mock.patch.object(sys, 'argv', cli('--authorize-preimage', digest(original))), \
                     self.assertRaises(SystemExit):
                    relay.main()
                self.browser.app_overrides.clear()
                self.assertEqual(self.browser.inserts, [])
            with mock.patch.object(sys, 'argv', cli('--authorize-preimage', digest(original))), redirect_stdout(io.StringIO()):
                self.assertEqual(relay.main(), 0)
            self.assertEqual(self.browser.created, [])
            self.assertEqual(self.browser.navigations, [])
            self.assertEqual(self.browser.inserts, [('retained', packet)])
            self.assertEqual(self.browser.sent, [('retained', packet)])
            bound = relay.job_state(job)
            self.assertEqual((bound['status'], bound['typed'], bound['sent'], bound['retry_blocked']),
                             ('running', True, True, False))
            self.assertEqual(bound['error'], st['error'])
            self.assertEqual((d / 'MESSAGE.md').read_text(), packet)
            self.assertEqual(next(d.glob('JOB.pre-retained-resume-*.json')).read_bytes(), original)
            self.assertEqual([r['event'] for r in relay.ledger_rows(self.cfg)],
                             ['submitted', 'retained_resume', 'submission_bound'])
            with mock.patch.object(sys, 'argv', cli('--authorize-preimage', digest(original))), \
                 self.assertRaises(SystemExit):
                relay.main()
            self.assertEqual(len(self.browser.sent), 1)
            # A post-insertion ambiguous result is journaled and never retried.
            held = 'fixture-retained-ambiguous'
            hd = relay.job_dir(held)
            hs = {**st, 'job': held, 'error': {**st['error'], 'tab': 'held'}}
            relay.job_write(held, hs)
            hm = relay._failed_generation_identity(hs) + '\noriginal packet'
            (hd / 'MESSAGE.md').write_text(hm)
            relay.ledger_append(self.cfg, {'event': 'submitted', 'kind': 'initial',
                'certainty': 'reserved_may_send', 'job': held, 'account': 'account-a',
                **{k: hs[k] for k in relay.PROVENANCE_FIELDS}})
            held_raw = (hd / 'JOB.json').read_bytes()
            hr = {'job': held, 'reason': hs['reason'], 'submission_bound': False,
                  'typed': False, 'sent': False, 'error_tab': 'held',
                  'job_sha256': digest(held_raw), 'message_sha256': digest(hm.encode()),
                  'ledger_rows_sha256': digest(json.dumps([r for r in relay.ledger_rows(self.cfg)
                      if r.get('job') == held], sort_keys=True, separators=(',', ':')).encode()),
                  'time_origin': 12345.5}
            rp.write_text(json.dumps({'jobs': [hr]}))
            self.browser.pages['held'] = {'url': HOME, 'composer': '', 'text': '',
                                          'messages': False, 'stop': False}
            hcli = ['gpt6pro_relay.py', 'resume-unsent-readiness', '--job', held,
                    '--report', report, '--authorize-preimage', digest(held_raw)]
            with mock.patch.object(sys, 'argv', hcli), \
                 mock.patch.object(relay, 'send_message', return_value={
                     'error': 'INSERT_STALLED', 'typed': True, 'sent': 'UNKNOWN'}) as sender, \
                 self.assertRaisesRegex(SystemExit, 'RESUME_HELD INSERT_STALLED'):
                relay.main()
            sender.assert_called_once()
            self.assertTrue(relay.job_state(held)['retry_blocked'])
            self.assertEqual(relay.job_state(held)['sent'], 'UNKNOWN')
            self.assertEqual((hd / 'MESSAGE.md').read_text(), hm)
            self.assertEqual(next(hd.glob('JOB.pre-retained-resume-*.json')).read_bytes(), held_raw)
            with mock.patch.object(sys, 'argv', hcli), self.assertRaises(SystemExit):
                relay.main()
            sender.assert_called_once()

    def test_retained_resume_paced_send_guard_actual_cli(self):
        for drift in ('document', 'draft', 'model', 'readiness', 'unchanged'):
            with self.subTest(drift=drift):
                case = type(self)()
                case.setUp()
                try:
                    case._run_retained_resume_paced_send_guard(drift)
                finally:
                    case.doCleanups()

    def _run_retained_resume_paced_send_guard(self, drift):
        """The real CLI must keep its consumed attempt held on a post-pacing drift."""
        import hashlib
        import socket
        job = 'fixture-retained-paced-' + drift
        d = relay.job_dir(job)
        st = {'job': job, 'account': 'account-a', 'status': 'failed',
              'reason': 'SECURITY_INTERSTITIAL', 'retry_blocked': True,
              'typed': False, 'sent': False, 'input_sha256': 'a' * 64, 'reply_token': 'b' * 64,
              **{k: 'fixture-' + k for k in relay.PROVENANCE_FIELDS}}
        st['error'] = {'error': st['reason'], 'typed': False, 'sent': False, 'tab': 'retained',
                       'state': {'url': HOME, 'model': None, 'assistant_count': 0, 'last_len': 0,
                                 'security_interstitial': True, 'composer_present': False,
                                 **{k: False for k in ('human_challenge', 'logged_out', 'rate_limited',
                                                      'stop', 'drive_reconnect', 'thinking_failed')}}}
        relay.job_write(job, st)
        original = (d / 'JOB.json').read_bytes()
        message = relay._failed_generation_identity(st) + '\noriginal packet'
        (d / 'MESSAGE.md').write_text(message)
        relay.ledger_append(self.cfg, {'event': 'submitted', 'kind': 'initial',
            'certainty': 'reserved_may_send', 'job': job, 'account': 'account-a',
            **{k: st[k] for k in relay.PROVENANCE_FIELDS}})
        sha = lambda value: hashlib.sha256(value).hexdigest()
        report = 'reports/fixture-paced-readiness.json'
        rp = self.root / report
        rp.parent.mkdir(parents=True)
        rp.write_text(json.dumps({'jobs': [{'job': job, 'reason': st['reason'],
            'submission_bound': False, 'typed': False, 'sent': False, 'error_tab': 'retained',
            'job_sha256': sha(original), 'message_sha256': sha(message.encode()),
            'ledger_rows_sha256': sha(json.dumps(relay.ledger_rows(self.cfg), sort_keys=True,
                separators=(',', ':')).encode()), 'time_origin': 12345.5}]}))
        page = {'url': HOME, 'composer': '', 'text': '', 'messages': False,
                'stop': False, 'origin': 12345.5, 'model': '6 Pro'}
        self.browser.pages['retained'] = page
        real_js = self.browser.js
        admission = []
        atomic = []
        def probe(expression):
            expr = expression
            if expr.startswith('(()=>{const e=') and 'time_origin' in expr:
                return {'url': page['url'], 'time_origin': page['origin'],
                        'composer_empty': not page['composer'].strip(), 'user_count': 0}
            if expr.startswith('(()=>{const e=') and 'origin:performance.timeOrigin' in expr:
                return {'origin': page['origin'], 'users': 0, 'assistants': 0,
                        'empty': not page['composer'].strip(), 'challenge': False,
                        'interstitial': bool(page.get('challenge')), 'login': False,
                        'rate': False, 'stop': False, 'reconnect': False}
            if expr.startswith('(()=>{const retainedOrigin='):
                outcome = self.browser.eval_retained_js(expr)
                atomic.append(outcome)
                return outcome['result']
            return real_js(expr)
        self.browser.js = probe
        real_sleep = self.browser.sleep
        def paced_sleep(seconds):
            if seconds > 20 and self.browser.inserts and not self.browser.sent:
                admission.append(seconds)
                if drift == 'document':
                    page.update(origin=99999.5, composer='unrelated replacement draft')
                elif drift == 'draft':
                    page['composer'] = 'unrelated replacement draft'
                elif drift == 'model':
                    page['model'] = '6 Sol'
                elif drift == 'readiness':
                    page['challenge'] = True
            real_sleep(seconds)
        self.browser.sleep = paced_sleep
        argv = ['gpt6pro_relay.py', 'resume-unsent-readiness', '--job', job,
                '--report', report, '--authorize-preimage', sha(original)]
        with mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('NO NETWORK')), \
             mock.patch.object(socket, 'create_connection', side_effect=AssertionError('NO NETWORK')), \
             mock.patch.object(relay, '_legacy_live_jobs', return_value=set()), \
             mock.patch.object(relay.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0)), \
             mock.patch.object(relay, 'drive', side_effect=AssertionError('NO UPLOAD')), \
             mock.patch.object(relay, 'cleanup_tabs', side_effect=AssertionError('NO CLEANUP')), \
             mock.patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()):
            if drift == 'unchanged':
                self.assertEqual(relay.main(), 0)
            else:
                with self.assertRaisesRegex(SystemExit, 'RESUME_HELD'):
                    relay.main()
        self.assertEqual(self.browser.inserts, [('retained', message)])
        self.assertEqual(len(admission), 1)
        self.assertEqual(self.browser.created + self.browser.navigations, [])
        self.assertEqual((d / 'MESSAGE.md').read_text(), message)
        self.assertEqual(next(d.glob('JOB.pre-retained-resume-*.json')).read_bytes(), original)
        final = relay.job_state(job)
        if drift == 'unchanged':
            self.assertEqual(self.browser.sent, [('retained', message)])
            self.assertEqual((final['status'], final['retry_blocked']), ('running', False))
            self.assertEqual([r['event'] for r in relay.ledger_rows(self.cfg)],
                             ['submitted', 'retained_resume', 'submission_bound'])
            self.assertEqual(atomic[0]['clicks'], 1)
        else:
            self.assertEqual(self.browser.sent, [])
            self.assertTrue(final['retry_blocked'])
            self.assertEqual([r['event'] for r in relay.ledger_rows(self.cfg)],
                             ['submitted', 'retained_resume'])
            self.assertTrue(not atomic or atomic[0]['clicks'] == 0)
            with mock.patch.object(sys, 'argv', argv), self.assertRaises(SystemExit):
                relay.main()
            self.assertEqual(self.browser.sent, [])

    def test_retained_sender_generated_javascript_survives_both_python_layers(self):
        """Capture the real sender's script, then parse and run its decoded _fresh JS."""
        scripts = []
        def capture(script, **kwargs):
            scripts.append(script)
            return {}
        with mock.patch.object(relay, 'bu_json', side_effect=capture):
            relay.send_message('original packet', retained_tab='retained', time_origin=12345.5)
        self.assertEqual(len(scripts), 1)
        generated = ast.parse(scripts[0])  # outer f-string has already been decoded
        expressions = [n.args[2].value for n in ast.walk(generated)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == '_bound_js' and len(n.args) > 2
            and isinstance(n.args[2], ast.Constant) and isinstance(n.args[2].value, str)]
        self.assertEqual(len(expressions), 15)  # Parent readback follows close, never the open preview.
        evidence = next(x for x in expressions if 'origin:performance.timeOrigin' in x)
        node = r'''const expressions=JSON.parse(process.argv[1]);
            // Compile every actual generated constant after the nested Python parse.
            for (const expression of expressions) new Function('return ('+expression+');');
            const evidence=expressions.find(x=>x.includes('origin:performance.timeOrigin'));
            function observe(title,body) {
                const editor={innerText:''};
                const document={title,body:{innerText:body},
                    querySelector:s=>s==='#prompt-textarea'?editor:null,
                    querySelectorAll:s=>[]};
                const performance={timeOrigin:12345.5};
                return new Function('document','performance','return ('+evidence+');')(document,performance);
            }
            console.log(JSON.stringify({clean:observe('ChatGPT','Ready'),
                challenge:observe('ChatGPT','Verify you are human'),count:expressions.length}));'''
        result = subprocess.run(['node', '-e', node, json.dumps(expressions)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed['count'], 15)
        self.assertEqual(observed['clean'], {'origin': 12345.5, 'users': 0,
            'assistants': 0, 'empty': True, 'challenge': False,
            'interstitial': False, 'login': False, 'rate': False,
            'stop': False, 'reconnect': False})
        self.assertTrue(observed['challenge']['challenge'])
        self.assertIn("document.title+'\\n'+body", evidence)

    def test_retained_sender_refuses_reused_missing_or_wrong_model_without_fresh_tab(self):
        self.browser.pages['retained'] = {'url': HOME, 'composer': '', 'text': '',
                                         'messages': False, 'stop': False}
        original_js = self.browser.js
        def probe(expression):
            if expression.startswith('(()=>{const e=') and 'origin:performance.timeOrigin' in expression:
                page = self.browser.pages[self.browser.active]
                return {'origin': 12345.5, 'users': page.get('users', 0),
                        'assistants': int(bool(page['text'])), 'empty': not page['composer'].strip(),
                        'challenge': False, 'interstitial': False, 'login': False,
                        'rate': False, 'stop': page['stop'], 'reconnect': False}
            return original_js(expression)
        self.browser.js = probe
        def send():
            return relay.send_message('original packet', retained_tab='retained', time_origin=12345.5)
        self.browser.pro_only_trigger = True
        self.browser.selected_model = '6 Sol'
        self.assertEqual(send()['error'], 'WRONG_MODEL')
        self.assertEqual(self.browser.inserts, [])
        self.browser.pro_only_trigger = False
        for change in ({'url': WANT}, {'composer': 'other draft'}, {'users': 1},
                       {'text': 'old assistant', 'messages': True}, {'stop': True}):
            page = self.browser.pages['retained']; before = page.copy(); page.update(change)
            with self.subTest(change=change):
                self.assertIn('error', send())
                self.assertEqual(self.browser.inserts, [])
            page.clear(); page.update(before)
        self.browser.pages.pop('retained')
        self.assertEqual(send()['error'], 'retained_target_missing')
        self.assertEqual(self.browser.created, [])
        self.assertEqual(self.browser.navigations, [])
        self.assertEqual(self.browser.sent, [])

    def test_unlisted_wrong_model_reconciles_with_explicit_report(self):
        job = 'glp999-unlisted-wrong-model'
        d, state, report = self._unlisted_wrong_model(
            job, {'submission_bound': False, 'typed': False, 'sent': False})
        out = io.StringIO()
        with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()), mock.patch.object(
            relay, '_legacy_tabs', return_value=[{'type': 'page', 'id': 'idle', 'url': HOME}]), \
                redirect_stdout(out):
            self.assertEqual(relay.reconcile_unsent_model(job, report=report), 0)
            self.assertEqual(relay.job_state(job), state)
            with self.assertRaisesRegex(SystemExit, 'MODEL_RECONCILE_REFUSED_REPORT_PATH'):
                relay.reconcile_unsent_model(job, apply=True, report='../outside.json')
            self.assertEqual(relay.reconcile_unsent_model(job, apply=True, report=report), 0)
        self.assertIn('WOULD_RECONCILE_UNSENT_MODEL job=' + job + ' account=account-a', out.getvalue())
        self.assertIn('RECONCILED_UNSENT_MODEL job=' + job + ' account=account-a', out.getvalue())
        self.assertEqual(relay.job_state(job)['status'], 'unsent_reconciled')
        self.assertFalse(relay.job_state(job)['retry_blocked'])
        self.assertEqual(json.loads(next(d.glob('JOB.pre-model-reconcile-*.json')).read_text()), state)
        self.assertEqual([x['phase'] for x in relay.ledger_rows(self.cfg)
                          if x.get('event') == 'model_unsent_reconcile'], ['STARTED', 'APPLIED'])

    def test_unlisted_wrong_model_report_must_prove_unsent(self):
        for row in ({'submission_bound': False, 'typed': False, 'sent': True},
                    {'submission_bound': True, 'typed': False, 'sent': False}):
            job = 'glp999-unlisted-' + ('sent' if row['sent'] else 'bound')
            _, state, report = self._unlisted_wrong_model(job, row)
            with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()), mock.patch.object(
                relay, '_legacy_tabs', return_value=[{'type': 'page', 'id': 'idle', 'url': HOME}]):
                for apply in (False, True):
                    with self.assertRaisesRegex(SystemExit, 'MODEL_RECONCILE_REFUSED_REPORT ' + job):
                        relay.reconcile_unsent_model(job, apply=apply, report=report)
            self.assertEqual(relay.job_state(job), state)
            self.assertFalse(list(relay.job_dir(job).glob('JOB.pre-model-reconcile-*.json')))

    def test_unlisted_wrong_model_without_report_refuses(self):
        job = 'glp999-unlisted-no-report'
        _, state, _ = self._unlisted_wrong_model(
            job, {'submission_bound': False, 'typed': False, 'sent': False})
        for apply in (False, True):
            with self.assertRaisesRegex(SystemExit, 'MODEL_RECONCILE_REFUSED_UNLISTED ' + job):
                relay.reconcile_unsent_model(job, apply=apply)
        self.assertEqual(relay.job_state(job), state)

    def test_undefined_focus_value_does_not_abort_before_typing(self):
        self.browser.omit_undefined_value = True
        result = relay.send_message("packet", forbidden=[SIBLING])
        self.assertNotIn("error", result)
        self.assertEqual(self.browser.inserts, [("new-1", "packet")])
        self.assertEqual(len(self.browser.sent), 1)

    def test_focus_abort_reconciles_only_exact_unsent_reservation(self):
        job = 'glp7-20260923-carrier-fix-draft'
        d = relay.job_dir(job)
        (d / 'MESSAGE.md').write_text('packet')
        original = {'job': job, 'account': 'account-a', 'status': 'submitting',
                    'retry_blocked': True, 'typed': 'UNKNOWN', 'sent': 'UNKNOWN'}
        relay.job_write(job, original)
        relay.ledger_append(self.cfg, {'event': 'submitted', 'job': job, 'account': 'account-a'})
        log = self.root / 'reports/ops/pro-wave-20260923/acct1.log'
        log.parent.mkdir(parents=True)
        log.write_text(f"submit {job}: rc=1 KeyError: 'value'\nSUBMIT_REFUSED KeyError: 'value'\n")
        with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()):
            self.assertEqual(relay.reconcile_unsent_focus(job, log), 0)
            self.assertEqual(relay.job_state(job), original)
            self.assertEqual(relay.reconcile_unsent_focus(job, log, apply=True), 0)
        st = relay.job_state(job)
        self.assertEqual(st['status'], 'unsent_reconciled')
        self.assertIs(st['typed'], False)
        self.assertIs(st['sent'], False)
        self.assertFalse(st['retry_blocked'])
        self.assertEqual(json.loads(list(d.glob('JOB.pre-focus-reconcile-*.json'))[0].read_text()), original)
        with self.assertRaisesRegex(SystemExit, 'FOCUS_RECONCILE_REFUSED_STATE'):
            relay.reconcile_unsent_focus(job, log)

    def test_focus_abort_refuses_without_exact_failure_signature(self):
        job = 'glp40-20260923-unresolved-disposition'
        relay.job_write(job, {'job': job, 'account': 'account-a', 'status': 'submitting',
                              'retry_blocked': True, 'typed': 'UNKNOWN', 'sent': 'UNKNOWN'})
        (relay.JOBS / job / 'MESSAGE.md').write_text('packet')
        relay.ledger_append(self.cfg, {'event': 'submitted', 'job': job, 'account': 'account-a'})
        log = self.root / 'reports/ops/pro-wave-20260923/acct2.log'
        log.parent.mkdir(parents=True)
        log.write_text('SUBMIT_REFUSED some-other-error\n')
        with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()):
            with self.assertRaisesRegex(SystemExit, 'FOCUS_RECONCILE_REFUSED_SIGNATURE'):
                relay.reconcile_unsent_focus(job, log, apply=True)
        self.assertTrue(relay.job_state(job)['retry_blocked'])

    def test_focus_abort_refuses_unlisted_job(self):
        with self.assertRaisesRegex(SystemExit, 'FOCUS_RECONCILE_REFUSED_UNLISTED'):
            relay.reconcile_unsent_focus('not-this-incident', self.root / 'reports/ops/pro-wave-20260923/acct1.log', apply=True)

    def test_wrapper_page_session_mismatch_refuses_all_bound_actions(self):
        self.browser.wrong_page_session = True
        for action in (lambda: relay.send_message('packet'),
                       lambda: relay.send_followup(WANT, 'missing', 'followup'),
                       lambda: relay.read_reply(WANT, 'missing')):
            with self.subTest(action=action):
                result = action()
                self.assertEqual(result.get('error'), 'tab_mismatch')
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])
        self.assertEqual(self.browser.reads, [])

    def test_addressed_input_session_mismatch_refuses_before_typing(self):
        self.browser.wrong_input_session = True
        result = relay.send_message('packet')
        self.assertEqual(result.get('error'), 'tab_mismatch')
        self.assertEqual(self.browser.inserts, [])

    def test_both_wrapper_and_input_drift_together_still_refuses(self):
        self.browser.wrong_input_session = True
        self.browser.wrong_page_session = True
        result = relay.send_message('packet')
        self.assertEqual(result.get('error'), 'tab_mismatch')
        self.assertEqual(self.browser.inserts, [])
        self.assertEqual(self.browser.sent, [])

    def test_broken_switch_refuses_input(self):
        self.browser.wrong_switch = True
        result = relay.send_message("packet")
        self.assertEqual(result.get("error"), "tab_mismatch")
        self.assertEqual(self.browser.inserts, [])

    def test_post_send_collision_is_not_reported_as_never_typed(self):
        self.job("older", SIBLING)
        self.browser.after_send_url = SIBLING
        with self.assertRaisesRegex(SystemExit, "conversation_collision"):
            relay.cmd_submit(self.args())
        st = relay.job_state("job")
        self.assertTrue(st["error"]["typed"])
        self.assertTrue(st["error"]["sent"])
        self.assertTrue(st["retry_blocked"])
        self.assertNotIn("conversation_url", st)
        self.assertEqual(relay.messages_used_7d(self.cfg, "account-a"), 1)
        with self.assertRaisesRegex(SystemExit, "REFUSED_DUPLICATE"):
            relay.cmd_submit(self.args())
        self.assertEqual(len(self.browser.sent), 1)

    def test_three_jobs_keep_distinct_bindings_and_replies(self):
        for name in ("one", "two", "three"):
            self.clock += 60
            self.assertEqual(relay.cmd_submit(self.args(name)), 0)
        states = [relay.job_state(n) for n in ("one", "two", "three")]
        self.assertEqual(len({s["conversation_url"] for s in states}), 3)
        self.assertEqual(len({s["tab"] for s in states}), 3)
        for name in ("one", "two", "three"):
            self.clock += 20
            self.assertEqual(relay.poll(name, False, 30, 3600), 3)
            self.assertFalse((relay.JOBS / name / "RESPONSE.md").exists())
            self.clock += 150
            self.assertEqual(relay.poll(name, False, 30, 3600), 0)
            self.assertEqual((relay.JOBS / name / "RESPONSE.md").read_text(), 'reply: ' + (relay.JOBS / name / 'MESSAGE.md').read_text())

    def test_three_thinking_jobs_release_the_submit_lock(self):
        self.browser.defer_text = True
        for name in ("one", "two", "three"):
            self.clock += 60
            self.assertEqual(relay.cmd_submit(self.args(name)), 0)
            self.assertFalse(relay._HELD_LOCKS)
        states = [relay.job_state(n) for n in ("one", "two", "three")]
        self.assertEqual(len({s["pending_conversation_url"] for s in states}), 3)
        self.assertTrue(all("conversation_url" not in s for s in states))
        self.assertTrue(all(self.browser.pages[s["tab"]]["stop"] for s in states))
        for name in ("three", "one", "two"):
            page = self.browser.pages[relay.job_state(name)["tab"]]
            page.update(text="finished " + name, stop=False)
            self.clock += 20
            self.assertEqual(relay.poll(name, False, 30, 3600), 3)
            self.clock += 150
            self.assertEqual(relay.poll(name, False, 30, 3600), 0)
            self.assertEqual((relay.JOBS / name / "RESPONSE.md").read_text(), f"finished {name}")

    def test_unbound_uncertain_sibling_blocks_submit(self):
        self.job("uncertain", None, status="failed", retry_blocked=True)
        with self.assertRaisesRegex(SystemExit, "BINDING_UNVERIFIED sibling"):
            relay.cmd_submit(self.args())
        self.assertEqual(self.browser.created, [])
        self.assertFalse(relay._HELD_LOCKS)

    def test_orphan_live_conversation_is_not_reused(self):
        self.browser.home_redirect = SIBLING
        result = relay.send_message("packet")
        self.assertEqual(result.get("error"), "conversation_collision")
        self.assertFalse(result["typed"])
        self.assertEqual(self.browser.inserts, [])

    def test_reload_does_not_interrupt_a_resumed_stream(self):
        self.browser.pages["sibling"].update(url=WANT, text="partial", stop=True)
        result = relay.read_reply(WANT, "sibling", reload=True)
        self.assertNotIn("error", result)
        self.assertTrue(result["state"]["stop"])
        self.assertEqual(self.browser.navigations, [])

    def test_job_write_failure_preserves_previous_json(self):
        self.job()
        path = relay.JOBS / "job/JOB.json"
        before = path.read_bytes()
        with mock.patch.object(relay.os, "replace", side_effect=OSError("fixture")):
            with self.assertRaisesRegex(OSError, "fixture"):
                relay.job_write("job", {"status": "done"})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob(".JOB-*")), [])

    def test_pending_id_is_reserved_then_promoted_after_text(self):
        self.browser.defer_text = True
        self.assertEqual(relay.cmd_submit(self.args("one")), 0)
        st = relay.job_state("one")
        self.assertNotIn("conversation_url", st)
        pending = st["pending_conversation_url"]
        self.assertEqual(relay.poll("one", False, 30, 3600), 3)
        self.assertNotIn("conversation_url", relay.job_state("one"))
        self.browser.home_redirect = pending
        self.clock += 60
        with self.assertRaisesRegex(SystemExit, "conversation_collision"):
            relay.cmd_submit(self.args("two"))
        page = self.browser.pages[st["tab"]]
        page.update(text="first token", stop=True)
        self.clock += 150
        self.assertEqual(relay.poll("one", False, 30, 3600), 3)
        st = relay.job_state("one")
        self.assertEqual(st["conversation_url"], pending)
        self.assertNotIn("pending_conversation_url", st)

    def test_read_wrong_tab_reopens_without_navigating_sibling(self):
        result = relay.read_reply(WANT, "sibling")
        self.assertNotIn("error", result)
        self.assertEqual(result["text"], "wanted answer")
        self.assertEqual(result["tab"], "new-1")
        self.assertEqual(self.browser.pages["sibling"]["url"], SIBLING)
        self.assertEqual(self.browser.navigations, [])
        self.assertEqual(self.browser.reads, ["new-1"])

    def test_missing_tab_does_not_reuse_a_matching_tab(self):
        self.browser.pages["sibling"]["url"] = WANT
        result = relay.read_reply(WANT, "missing")
        self.assertNotIn("error", result)
        self.assertEqual(result["tab"], "new-1")
        self.assertEqual(self.browser.reads, ["new-1"])

    def test_protected_tab_is_not_reused_even_when_its_url_matches(self):
        self.browser.pages["sibling"]["url"] = WANT
        result = relay.read_reply(WANT, "sibling", protected=["sibling"])
        self.assertNotIn("error", result)
        self.assertEqual(result["tab"], "new-1")

    def test_reopen_redirect_refuses_before_reading(self):
        self.browser.reopen_redirect = SIBLING
        result = relay.read_reply(WANT, "sibling")
        self.assertEqual(result.get("error"), "conversation_mismatch")
        self.assertNotIn("text", result)
        self.assertEqual(self.browser.reads, [])
        self.assertEqual(set(self.browser.pages), {'sibling'})

    def test_redirect_during_read_does_not_return_sibling_text(self):
        self.browser.redirect_on_slice = True
        result = relay.read_reply(WANT, "sibling")
        self.assertEqual(result.get("error"), "conversation_mismatch")
        self.assertNotIn("text", result)
        self.assertEqual(self.browser.reads, [])

    def test_followup_reopens_and_types_only_in_owned_tab(self):
        result = relay.send_followup(WANT, "sibling", "continue")
        self.assertNotIn("error", result)
        self.assertTrue(result["sent"])
        self.assertEqual(result["tab"], "new-1")
        self.assertEqual(self.browser.inserts, [("new-1", "continue")])
        self.assertEqual(self.browser.pages["sibling"]["url"], SIBLING)

    def test_followup_refuses_redirect_before_input(self):
        self.browser.reopen_redirect = SIBLING
        result = relay.send_followup(WANT, "sibling", "continue")
        self.assertEqual(result.get("error"), "conversation_mismatch")
        self.assertEqual(self.browser.inserts, [])

    def test_fetch_refuses_mismatch_before_drive_or_files(self):
        self.job()
        self.browser.reopen_redirect = SIBLING
        before = (relay.JOBS / "job" / "JOB.json").read_bytes()
        with self.assertRaisesRegex(SystemExit, "CONVERSATION_MISMATCH"):
            relay.cmd_fetch_outbox(types.SimpleNamespace(job="job"))
        relay.drive.assert_not_called()
        self.assertFalse((relay.JOBS / "job" / "outbox").exists())
        self.assertEqual((relay.JOBS / "job" / "JOB.json").read_bytes(), before)

    def test_fetch_rechecks_an_inconsistent_reader_result(self):
        self.job()
        result = {"tab": "sibling", "text": "poison", "state": {"url": SIBLING, "conversation_url_now": WANT}}
        with mock.patch.object(relay, "read_reply", return_value=result):
            with self.assertRaisesRegex(SystemExit, "CONVERSATION_MISMATCH"):
                relay.cmd_fetch_outbox(types.SimpleNamespace(job="job"))
        relay.drive.assert_not_called()
        self.assertFalse((relay.JOBS / "job" / "outbox").exists())

    def test_fetch_verified_binding_downloads_files(self):
        self.prepare_bundle()
        self.assertEqual(relay.cmd_fetch_outbox(types.SimpleNamespace(job='job')), 0)
        self.assertEqual((relay.JOBS / 'job/outbox/answer.diff').read_bytes(), b'verified fixture\n')
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'VERIFIED')

    def test_reply_writer_hash_and_legacy_exact_lf_reject_tamper(self):
        self.browser.pages['exact'] = {'url': WANT, 'text': 'wanted answer',
                                       'composer': '', 'messages': True, 'stop': False}
        self.job(tab='exact', last_text_sha=relay.hashlib.sha256(b'wanted answer').hexdigest())
        self.assertEqual(relay.poll('job', False, 30, 3600), 0)
        st = relay.job_state('job')
        local = (relay.JOBS / 'job/RESPONSE.md').read_bytes()
        self.assertEqual(st['reply_serialization'], 'raw_utf8')
        self.assertEqual(st['reply_render_mode'], 'role')
        self.assertEqual(relay.hashlib.sha256(local).hexdigest(), st['source_reply_sha256'])

        # The prior writer serialized a single additional LF. Its exact bytes
        # remain fetchable, but a changed payload cannot pass as that format.
        self.prepare_bundle()
        self.assertEqual(relay.cmd_fetch_outbox(types.SimpleNamespace(job='job')), 0)
        response = relay.JOBS / 'job/RESPONSE.md'
        response.write_bytes(response.read_bytes()[:-2] + b'!\n')
        with self.assertRaisesRegex(SystemExit, 'local reply differs'):
            relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'UNKNOWN')

    def prepare_bundle(self, mutate=None, extra=False, body='wanted answer\n'):
        artifact = b'verified fixture\n'
        manifest = {'job': 'job', 'conversation_id': WANT.rsplit('/', 1)[1], 'input_sha256': 'a'*64,
                    'source_reply_sha256': relay.hashlib.sha256(body.encode()).hexdigest(),
                    'files': {'answer.diff': relay.hashlib.sha256(artifact).hexdigest()}}
        if mutate: mutate(manifest)
        raw = json.dumps(manifest).encode()
        text = body + 'RELAY_OUTPUT_MANIFEST_SHA256=' + relay.hashlib.sha256(raw).hexdigest()
        self.browser.history[WANT] = text
        self.job(status='done', input_sha256='a'*64, last_text_sha=relay.hashlib.sha256(text.encode()).hexdigest(),
                 source_reply_sha256=relay.hashlib.sha256(text.encode()).hexdigest())
        (relay.JOBS / 'job/RESPONSE.md').write_text(text + '\n')
        entries = [{'id': 'manifest', 'name': 'OUTPUT-MANIFEST.json', 'mimeType': 'text/plain'},
                   {'id': 'artifact', 'name': 'answer.diff', 'mimeType': 'text/plain'}]
        if extra: entries.append({'id': 'foreign', 'name': 'other.diff', 'mimeType': 'text/plain'})
        api = self.svc.files.return_value
        api.list.return_value.execute.side_effect = [{'files': [{'id': 'folder'}]},
            {'files': entries[:1], 'nextPageToken': 'page2'}, {'files': entries[1:]}]
        blobs = {'manifest': raw, 'artifact': artifact}
        api.get_media.side_effect = lambda fileId: types.SimpleNamespace(execute=lambda: blobs[fileId])

    def test_verified_zero_outbox_is_distinct_from_unverifiable_bundle(self):
        self.prepare_bundle()
        self.svc.files.return_value.list.return_value.execute.side_effect = None
        self.svc.files.return_value.list.return_value.execute.return_value = {'files': []}
        self.assertEqual(relay.cmd_fetch_outbox(types.SimpleNamespace(job='job')), 10)
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'ABSENT')
        self.assertFalse((relay.JOBS / 'job/outbox').exists())
        self.svc.files.return_value.get_media.assert_not_called()

    def test_done_chat_empty_after_reopen_reload_recovers_exact_saved_reply(self):
        self.prepare_bundle()
        st = relay.job_state('job'); st['tab'] = 'sibling'; relay.job_write('job', st)
        original = relay.read_reply
        calls = []
        def delayed_reply(*args, **kwargs):
            calls.append(kwargs)
            result = original(*args, **kwargs)
            if len(calls) == 1:
                result['text'] = ''
                result['state']['stop'] = False
            return result
        with mock.patch.object(relay, 'read_reply', side_effect=delayed_reply):
            self.assertEqual(relay.cmd_fetch_outbox(types.SimpleNamespace(job='job')), 0)
        self.assertTrue(calls[1]['reload'])
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'VERIFIED')
        self.assertEqual((relay.JOBS / 'job/outbox/answer.diff').read_bytes(), b'verified fixture\n')

    def test_selection_capture_same_representation_verifies(self):
        self.prepare_bundle(body='first\nsecond\n')
        st = relay.job_state('job'); st.update(reply_serialization='raw_utf8', reply_render_mode='selection_markdown')
        relay.job_write('job', st)
        (relay.JOBS / 'job/RESPONSE.md').write_text(self.browser.history[WANT])
        self.browser.pages['sibling'].update(url=WANT, text='', selection_text=self.browser.history[WANT])
        with mock.patch.object(relay, 'cleanup_tabs', return_value=0):
            self.assertEqual(relay.cmd_fetch_outbox(types.SimpleNamespace(job='job')), 0)
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'VERIFIED')

    def test_selection_capture_missing_original_renderer_stays_unknown(self):
        self.prepare_bundle()
        st = relay.job_state('job'); st.update(reply_serialization='raw_utf8', reply_render_mode='role')
        relay.job_write('job', st)
        (relay.JOBS / 'job/RESPONSE.md').write_text(self.browser.history[WANT])
        self.browser.pages['sibling'].update(url=WANT, text='', selection_text=self.browser.history[WANT])
        with mock.patch.object(relay, 'cleanup_tabs', return_value=0):
            with self.assertRaisesRegex(SystemExit, 'final reply hash'):
                relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        relay.drive.assert_not_called()
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'UNKNOWN')

    def test_final_recheck_rejects_changed_reply_before_destination(self):
        self.prepare_bundle()
        original = relay.read_reply
        calls = 0
        def changing_reply(*args, **kwargs):
            nonlocal calls
            calls += 1
            result = original(*args, **kwargs)
            if calls == 2:
                result['text'] = result['text'].replace('wanted answer', 'wanted  answer')
                result['state']['render_mode'] = 'selection_markdown'
            return result
        with mock.patch.object(relay, 'read_reply', side_effect=changing_reply):
            with self.assertRaisesRegex(SystemExit, 'reply changed during fetch'):
                relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        self.assertEqual(calls, 2)
        self.assertFalse((relay.JOBS / 'job/outbox').exists())
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'UNKNOWN')

    def test_selection_renderer_linebreak_only_change_stays_unknown(self):
        self.prepare_bundle()
        st = relay.job_state('job'); st['reply_serialization'] = 'raw_utf8'; relay.job_write('job', st)
        (relay.JOBS / 'job/RESPONSE.md').write_text(self.browser.history[WANT])
        original = relay.read_reply
        def changed_render(*args, **kwargs):
            result = original(*args, **kwargs)
            result['text'] = result['text'].replace('\n', '\n\n', 1)
            result['state']['render_mode'] = 'selection_markdown'
            return result
        with mock.patch.object(relay, 'read_reply', side_effect=changed_render):
            with self.assertRaisesRegex(SystemExit, 'final reply hash'):
                relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        relay.drive.assert_not_called()
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'UNKNOWN')
        self.assertEqual(relay.close_tab_quiet.call_count, 1)

    def test_selection_renderer_relocated_indent_stays_unknown(self):
        self.prepare_bundle(body='    x = 1\n    +2\n')
        st = relay.job_state('job'); st['reply_serialization'] = 'raw_utf8'; relay.job_write('job', st)
        (relay.JOBS / 'job/RESPONSE.md').write_text(self.browser.history[WANT])
        original = relay.read_reply
        def moved_indent(*args, **kwargs):
            result = original(*args, **kwargs)
            result['text'] = result['text'].replace('    x = 1\n    +2\n', '    x = 1    \n+2\n')
            result['state']['render_mode'] = 'selection_markdown'
            return result
        with mock.patch.object(relay, 'read_reply', side_effect=moved_indent):
            with self.assertRaisesRegex(SystemExit, 'final reply hash'):
                relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        relay.drive.assert_not_called()
        self.assertFalse((relay.JOBS / 'job/outbox').exists())
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'UNKNOWN')

    def test_selection_renderer_relocated_hard_break_stays_unknown(self):
        self.prepare_bundle(body='first  \nsecond\n')
        st = relay.job_state('job'); st['reply_serialization'] = 'raw_utf8'; relay.job_write('job', st)
        (relay.JOBS / 'job/RESPONSE.md').write_text(self.browser.history[WANT])
        original = relay.read_reply
        def moved_hard_break(*args, **kwargs):
            result = original(*args, **kwargs)
            result['text'] = result['text'].replace('first  \nsecond', 'first\n  second')
            result['state']['render_mode'] = 'selection_markdown'
            return result
        with mock.patch.object(relay, 'read_reply', side_effect=moved_hard_break):
            with self.assertRaisesRegex(SystemExit, 'final reply hash'):
                relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        relay.drive.assert_not_called()
        self.assertFalse((relay.JOBS / 'job/outbox').exists())
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'UNKNOWN')

    def test_selection_renderer_joined_words_stay_unknown(self):
        self.prepare_bundle(body='not\nready\n')
        st = relay.job_state('job'); st['reply_serialization'] = 'raw_utf8'; relay.job_write('job', st)
        (relay.JOBS / 'job/RESPONSE.md').write_text(self.browser.history[WANT])
        original = relay.read_reply
        def joined_words(*args, **kwargs):
            result = original(*args, **kwargs)
            result['text'] = result['text'].replace('not\nready', 'notready')
            result['state']['render_mode'] = 'selection_markdown'
            return result
        with mock.patch.object(relay, 'read_reply', side_effect=joined_words):
            with self.assertRaisesRegex(SystemExit, 'final reply hash'):
                relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        relay.drive.assert_not_called()
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'UNKNOWN')
        self.assertEqual(relay.close_tab_quiet.call_count, 1)

    def test_selection_renderer_joined_fenced_code_stays_unknown(self):
        self.prepare_bundle(body='```python\nx = 1\n+2\n```\n')
        st = relay.job_state('job'); st['reply_serialization'] = 'raw_utf8'; relay.job_write('job', st)
        (relay.JOBS / 'job/RESPONSE.md').write_text(self.browser.history[WANT])
        original = relay.read_reply
        def joined_code(*args, **kwargs):
            result = original(*args, **kwargs)
            result['text'] = result['text'].replace('x = 1\n+2', 'x = 1+2')
            result['state']['render_mode'] = 'selection_markdown'
            return result
        with mock.patch.object(relay, 'read_reply', side_effect=joined_code):
            with self.assertRaisesRegex(SystemExit, 'final reply hash'):
                relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        relay.drive.assert_not_called()
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'UNKNOWN')

    def test_selection_renderer_non_linebreak_change_stays_unknown(self):
        self.prepare_bundle()
        original = relay.read_reply
        def changed_render(*args, **kwargs):
            result = original(*args, **kwargs)
            result['text'] = 'X' + result['text'][1:]
            result['state']['render_mode'] = 'selection_markdown'
            return result
        with mock.patch.object(relay, 'read_reply', side_effect=changed_render):
            with self.assertRaisesRegex(SystemExit, 'final reply hash'):
                relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'UNKNOWN')

    def test_missing_chat_marker_and_verified_zero_folder_is_absent(self):
        self.job(status='done', input_sha256='a'*64,
                 last_text_sha=relay.hashlib.sha256(b'wanted answer').hexdigest())
        (relay.JOBS / 'job/RESPONSE.md').write_text('wanted answer\n')
        self.svc.files.return_value.list.return_value.execute.return_value = {'files': []}
        self.assertEqual(relay.cmd_fetch_outbox(types.SimpleNamespace(job='job')), 10)
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'ABSENT')
        self.svc.files.return_value.get_media.assert_not_called()

    def test_missing_chat_marker_with_folder_is_unknown(self):
        self.job(status='done', input_sha256='a'*64,
                 last_text_sha=relay.hashlib.sha256(b'wanted answer').hexdigest())
        (relay.JOBS / 'job/RESPONSE.md').write_text('wanted answer\n')
        self.svc.files.return_value.list.return_value.execute.return_value = {'files': [{'id': 'folder'}]}
        with self.assertRaisesRegex(SystemExit, 'missing final-chat manifest receipt'):
            relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'UNKNOWN')

    def test_fetch_manifest_identity_and_hash_fail_closed(self):
        for key, value in [('job', 'sibling'), ('conversation_id', SIBLING.rsplit('/',1)[1]),
                           ('input_sha256', 'b'*64), ('source_reply_sha256', 'c'*64),
                           ('files', {'answer.diff': 'd'*64})]:
            with self.subTest(key=key):
                self.prepare_bundle(lambda m: m.update({key: value}))
                with self.assertRaisesRegex(SystemExit, 'OUTBOX_UNVERIFIED'):
                    relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
                self.assertFalse((relay.JOBS / 'job/outbox').exists())

    def test_v2_uses_pre_send_job_token_when_chat_uuid_is_unavailable(self):
        self.prepare_bundle(lambda m: (m.update(schema='gpt6pro-output-manifest-v2', reply_token='bound-token'),
                                      m.pop('conversation_id'), m.pop('source_reply_sha256')))
        st = relay.job_state('job'); st['reply_token'] = 'bound-token'; relay.job_write('job', st)
        self.assertEqual(relay.cmd_fetch_outbox(types.SimpleNamespace(job='job')), 0)
        self.assertEqual(relay.job_state('job')['fetch_verification']['conversation_id'], WANT.rsplit('/', 1)[1])

    def test_v2_rendered_reply_hash_is_relay_owned_and_tamper_refused(self):
        # Pro hashes its saved file, not its differently-rendered final chat.
        self.prepare_bundle(lambda m: (m.update(schema='gpt6pro-output-manifest-v2', reply_token='bound-token'),
                                      m.pop('conversation_id'), m.pop('source_reply_sha256')))
        st = relay.job_state('job'); st['reply_token'] = 'bound-token'; relay.job_write('job', st)
        self.assertEqual(relay.cmd_fetch_outbox(types.SimpleNamespace(job='job')), 0)
        self.browser.history[WANT] += ' changed'
        with self.assertRaisesRegex(SystemExit, 'final reply hash'):
            relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'UNKNOWN')

    def test_v2_wrong_token_and_claimed_wrong_conversation_refused(self):
        for change, reason in [(lambda m: m.update(reply_token='other'), 'reply token'),
                               (lambda m: m.update(conversation_id=SIBLING.rsplit('/', 1)[1]), 'job/conversation')]:
            with self.subTest(reason=reason):
                self.prepare_bundle(lambda m: (m.update(schema='gpt6pro-output-manifest-v2', reply_token='bound-token'),
                                              m.pop('conversation_id'), m.pop('source_reply_sha256'), change(m)))
                st = relay.job_state('job'); st['reply_token'] = 'bound-token'; relay.job_write('job', st)
                with self.assertRaisesRegex(SystemExit, reason):
                    relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
                self.assertEqual(relay.job_state('job')['fetch_verification']['state'], 'UNKNOWN')

    def test_submit_produces_v2_contract_and_persists_unique_token(self):
        self.assertEqual(relay.cmd_submit(self.args('one')), 0)
        st = relay.job_state('one')
        text = (relay.JOBS / 'one/MESSAGE.md').read_text()
        self.assertRegex(st['reply_token'], r'^[0-9a-f]{64}$')
        self.assertIn('reply_token=' + st['reply_token'], text)
        self.assertIn('schema="gpt6pro-output-manifest-v2"', text)
        self.assertIn('Do NOT include conversation_id', text)
        self.clock += 60
        self.assertEqual(relay.cmd_submit(self.args('two')), 0)
        self.assertNotEqual(st['reply_token'], relay.job_state('two')['reply_token'])

    def test_packet_unknown_rows_selects_only_unknown_and_source_files(self):
        spec = importlib.util.spec_from_file_location('packet_unknown', RELAY_PATH.parent / 'gpt6pro_packet.py')
        packet = importlib.util.module_from_spec(spec); spec.loader.exec_module(packet)
        candidate = self.root / 'candidate/tests/browser'; candidate.mkdir(parents=True)
        (candidate / 'unknown.spec.mjs').write_text('test unknown source')
        triage = self.root / 'triage.json'
        triage.write_text(json.dumps({'schema': 'triage', 'source_report': 'run', 'source_binding': 'frozen',
            'denominator': {'by_class': {'UNKNOWN': 1}}, 'tests': [
                {'class': 'UNKNOWN', 'spec': 'tests/browser/unknown.spec.mjs', 'title': 'uncertain'},
                {'class': 'c', 'spec': 'tests/browser/not-attached.spec.mjs', 'title': 'settled'}]}))
        wave = {'issue': 'GLP-test', 'acceptance_verbatim': 'classify', 'candidate_root': 'candidate',
                **{k: 'test-' + k for k in relay.PROVENANCE_FIELDS}}
        row = {'job': 'unknown', 'job_class': 'adjudication', 'kind': 'freeform', 'title': 'Classify',
               'situation': 'Frozen run', 'task': 'Classify every row', 'files': [
                   ['business', 'triage.json', 'unknown rows', 'unknown-rows'],
                   ['business', 'triage.json', 'test sources', 'unknown-test-sources']]}
        with mock.patch.object(packet, 'BUSINESS', self.root), mock.patch.object(packet, 'JOBS', self.root / 'jobs'):
            d = packet.build_freeform(row, wave)
        attached = json.loads(next(d.glob('DRIVE-UNKNOWN-*')).read_text())
        self.assertEqual([r['title'] for r in attached['tests']], ['uncertain'])
        self.assertEqual(len(list(d.glob('DRIVE-SOURCE-*'))), 1)
        self.assertEqual(next(d.glob('DRIVE-SOURCE-*')).read_text(), 'test unknown source')

    def test_failed_fetch_retry_reclaims_previous_tab(self):
        # A retry may reopen the completed chat, but may not accumulate tabs.
        self.prepare_bundle(extra=True)
        with self.assertRaisesRegex(SystemExit, 'OUTBOX_UNVERIFIED'):
            relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        tab = relay.job_state('job')['tab']
        self.prepare_bundle(extra=True)
        st = relay.job_state('job'); st['tab'] = tab; relay.job_write('job', st)
        with self.assertRaisesRegex(SystemExit, 'OUTBOX_UNVERIFIED'):
            relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        self.assertNotIn(tab, self.browser.pages)
        self.assertEqual(len(self.browser.pages), 2)
        self.assertEqual(relay.close_tab_quiet.call_count, 2)

    def test_fetch_foreign_extra_file_refused(self):
        self.prepare_bundle(extra=True)
        with self.assertRaisesRegex(SystemExit, 'OUTBOX_UNVERIFIED'):
            relay.cmd_fetch_outbox(types.SimpleNamespace(job='job'))
        self.assertFalse((relay.JOBS / 'job/outbox').exists())

    def test_poll_refuses_mismatch_before_response_write(self):
        self.job(last_text_sha=relay.hashlib.sha256(b"sibling answer").hexdigest())
        self.browser.reopen_redirect = SIBLING
        with self.assertRaisesRegex(SystemExit, "CONVERSATION_MISMATCH"):
            relay.poll("job", False, 30, 3600)
        self.assertFalse((relay.JOBS / "job/RESPONSE.md").exists())
        relay.drive.assert_not_called()

    def test_legacy_duplicate_bindings_are_quarantined(self):
        self.job("older", WANT, status="done")
        self.job()
        with self.assertRaisesRegex(SystemExit, "CONVERSATION_COLLISION"):
            relay.poll("job", False, 30, 3600)
        with self.assertRaisesRegex(SystemExit, "CONVERSATION_COLLISION"):
            relay.cmd_followup(types.SimpleNamespace(job="job", text_file=None))
        with self.assertRaisesRegex(SystemExit, "CONVERSATION_COLLISION"):
            relay.cmd_fetch_outbox(types.SimpleNamespace(job="job"))
        self.assertEqual(self.browser.created, [])
        self.assertEqual(self.browser.inserts, [])

    def test_url_identity_is_not_a_prefix_or_transient(self):
        for bad in (HOME, WANT + "suffix", WANT + "/other", WANT.replace("chatgpt.com", "chatgpt.com.evil"), HOME + "c/WEB:123"):
            with self.subTest(url=bad):
                self.assertIsNone(relay._conversation(bad))
                self.assertEqual(relay.read_reply(bad, "sibling").get("error"), "binding_unverified")
        self.assertEqual(relay._conversation(WANT + "/?x=1#frag"), WANT)
        self.assertEqual(self.browser.created, [])

    def test_global_capacity_includes_other_waves_and_uncertain_jobs(self):
        for i in range(3):
            self.job('foreign-wave-' + str(i), WANT[:-1] + str(i), status='running')
        with self.assertRaisesRegex(SystemExit, 'CAPACITY_ACCOUNT'):
            relay.cmd_submit(self.args())
        self.assertEqual(self.browser.inserts, [])
        relay.drive.assert_not_called()

    def test_six_ip_limit_counts_other_accounts(self):
        for i in range(6):
            self.job('foreign-' + str(i), WANT[:-1] + str(i), account='other-'+str(i))
        with self.assertRaisesRegex(SystemExit, 'CAPACITY_IP'):
            relay.cmd_submit(self.args())
        self.assertEqual(self.browser.created, [])

    def test_global_submit_pacing_and_quota_reservation_before_send(self):
        relay.cmd_submit(self.args('one'))
        with self.assertRaisesRegex(SystemExit, 'PACING_SUBMIT'):
            relay.cmd_submit(self.args('two'))
        self.assertEqual(len(self.browser.sent), 1)
        self.clock += 60
        relay.cmd_submit(self.args('two'))
        self.assertEqual(relay.messages_used_7d(self.cfg, 'account-a'), 2)
        self.assertEqual(len(relay.active_registry()), 2)

    def test_crash_after_send_keeps_reservation_no_retry(self):
        original = relay.send_message
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('lost result after send')
        with mock.patch.object(relay, 'send_message', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'lost result'):
                relay.cmd_submit(self.args())
        self.assertTrue(relay.job_state('job')['retry_blocked'])
        self.assertEqual(relay.messages_used_7d(self.cfg, 'account-a'), 1)
        with self.assertRaisesRegex(SystemExit, 'REFUSED_DUPLICATE'):
            relay.cmd_submit(self.args())
        self.assertEqual(len(self.browser.sent), 1)

    def test_provenance_required_before_any_external_effect(self):
        args = self.args(); args.request_id = None
        with self.assertRaisesRegex(SystemExit, 'PROVENANCE_REQUIRED'):
            relay.cmd_submit(args)
        self.assertEqual(self.browser.created, [])
        relay.drive.assert_not_called()

    def test_poll_clock_shared_across_jobs(self):
        self.assertTrue(relay.poll_slot('one', self.cfg))
        self.assertFalse(relay.poll_slot('two', self.cfg))
        self.clock += 20
        self.assertTrue(relay.poll_slot('two', self.cfg))
        self.assertFalse(relay.poll_slot('one', self.cfg))
        self.clock += 130
        self.assertTrue(relay.poll_slot('one', self.cfg))

    def test_two_submit_reservations_race_for_last_account_slot(self):
        self.job('seed1', WANT[:-1] + '1')
        self.job('seed2', WANT[:-1] + '2')
        code = '''import importlib.util, pathlib, sys, json
spec=importlib.util.spec_from_file_location('r',sys.argv[1]); r=importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
r.BUSINESS=pathlib.Path(sys.argv[2]); r.JOBS=r.BUSINESS/'jobs'
cfg=json.loads(sys.argv[3]); account=cfg['accounts'][0]; r.use_account(account)
name=sys.argv[4]; r.job_dir(name)
st={'job':name,'account':account['label'],'status':'submitting','retry_blocked':True,**{k:'fixture' for k in r.PROVENANCE_FIELDS}}
print('ready',flush=True); sys.stdin.readline()
try:
    with r.account_lock(): r.admit(name,account,cfg,st)
    print('ADMITTED',flush=True)
except SystemExit as e: print(str(e),flush=True)
'''
        procs = [subprocess.Popen([sys.executable, '-u', '-c', code, str(RELAY_PATH), str(self.root), json.dumps(self.cfg), name],
                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for name in ('racer1', 'racer2')]
        try:
            for proc in procs:
                self.assertTrue(select.select([proc.stdout], [], [], 5)[0])
                self.assertEqual(proc.stdout.readline().strip(), 'ready')
            for proc in procs: proc.stdin.write('go\n'); proc.stdin.flush()
            results = [p.communicate(timeout=10) for p in procs]
            self.assertEqual(sum('ADMITTED' in out for out, err in results), 1)
            self.assertEqual(sum('CAPACITY_ACCOUNT' in out for out, err in results), 1)
            self.assertEqual(len(relay.active_registry()), 3)
        finally:
            for proc in procs:
                if proc.poll() is None: proc.kill()
                proc.communicate()

    def test_config_pacing_applies_at_send_and_page_load(self):
        self.cfg['envelope']['min_seconds_between_submits'] = 95
        self.browser.sent_times = []
        original = self.browser.js
        def timed(expression):
            if expression.startswith('(()=>{function pickerActionable') and 'const b=document.querySelector' in expression and 'b.click();return true;' in expression:
                self.browser.sent_times.append(self.browser.clock)
            return original(expression)
        with mock.patch.object(self.browser, 'js', side_effect=timed):
            relay.send_message('one')
            relay.send_message('two')
        self.assertGreaterEqual(self.browser.sent_times[1] - self.browser.sent_times[0], 95)
        path = self.root / 'ops/gpt6pro-relay/locks/page-load-ip.clock'
        self.assertTrue(path.exists())
        self.assertEqual(len(self.browser.sent_times), 2)

    def test_wave_global_registry_and_packet_provenance(self):
        def load(name):
            spec = importlib.util.spec_from_file_location(name, RELAY_PATH.parent / (name + '.py'))
            module = importlib.util.module_from_spec(spec)
            with mock.patch.dict(sys.modules, {'gpt6pro_relay': relay}): spec.loader.exec_module(module)
            return module
        wave = load('gpt6pro_wave'); packet = load('gpt6pro_packet')
        with mock.patch.object(wave.importlib, 'reload', return_value=relay):
            self.assertTrue(wave.has_capacity('account-a', self.cfg['envelope']))
            for i in range(3): self.job('other-wave-' + str(i), WANT[:-1] + str(i))
            self.assertFalse(wave.has_capacity('account-a', self.cfg['envelope']))
        with mock.patch.object(sys, 'argv', ['wave', '--wave', 'fixture.json']), mock.patch.object(wave, 'run') as run:
            with self.assertRaises(SystemExit): wave.main()
            run.assert_not_called()
        with self.assertRaisesRegex(SystemExit, 'PROVENANCE_REQUIRED'): packet.packet_provenance({}, {})
        identity = {k:'test-'+k for k in relay.PROVENANCE_FIELDS}
        self.assertEqual(packet.packet_provenance(identity, {}), identity)

    def test_terminalize_legacy_exact_tab_dry_run_apply_and_idempotency(self):
        st = self.job('legacy', url=WANT, tab='wanted')
        st.pop('binding_version'); st.pop('conversation_id'); relay.job_write('legacy', st)
        self.browser.pages['wanted'] = {'url': WANT, 'text': 'final answer', 'composer': '',
                                        'messages': True, 'stop': False}
        outbox = relay.JOBS / 'legacy/outbox'
        outbox.mkdir(); (outbox / 'MANIFEST.json').write_text('legacy data')
        original = (relay.JOBS / 'legacy/JOB.json').read_bytes()
        tabs = lambda _: [{'id': t['targetId'], 'type': 'page', 'url': t['url']}
                          for t in self.browser.list_tabs()]
        close = lambda tab, url: self.browser.pages.pop(tab, None) is not None
        with mock.patch.object(relay.time, 'sleep'):
            with redirect_stdout(io.StringIO()) as output:
                relay.terminalize_legacy(live_jobs_reader=lambda: set(), tabs_reader=tabs)
            self.assertEqual(json.loads(output.getvalue())['results'][0]['decision'], 'WOULD_COMPLETE')
            self.assertEqual((relay.JOBS / 'legacy/JOB.json').read_bytes(), original)
            self.assertFalse((relay.JOBS / 'legacy/RESPONSE.md').exists())
            with redirect_stdout(io.StringIO()) as output:
                relay.terminalize_legacy(apply=True, live_jobs_reader=lambda: set(),
                                         tabs_reader=tabs, close_reader=close)
                relay.terminalize_legacy(apply=True, live_jobs_reader=lambda: set(),
                                         tabs_reader=tabs, close_reader=close)
        self.assertEqual(json.loads(output.getvalue().splitlines()[0])['results'][0]['decision'], 'COMPLETED')
        state = relay.job_state('legacy')
        self.assertEqual(state['status'], 'legacy_completed')
        self.assertEqual((relay.JOBS / 'legacy/RESPONSE.md').read_text(), 'final answer\n')
        self.assertEqual((outbox / 'MANIFEST.json').read_text(), 'legacy data')
        self.assertEqual(Path(state['legacy_reconciliation']['preimage']).read_bytes(), original)
        self.assertEqual([r['phase'] for r in relay.ledger_rows(self.cfg)
                          if r.get('event') == 'legacy_terminalize'], ['STARTED', 'APPLIED'])
        self.assertNotIn('wanted', self.browser.pages)

    def test_terminalize_legacy_allows_historical_reused_tab_not_active_owner(self):
        st = self.job('legacy', url=WANT, tab='wanted')
        st.pop('binding_version'); st.pop('conversation_id'); relay.job_write('legacy', st)
        self.browser.pages['wanted'] = {'url': WANT, 'text': 'final', 'composer': '',
                                        'messages': True, 'stop': False}
        self.job('historical', url=SIBLING, tab='wanted', status='done')
        tabs = lambda _: [{'id': t['targetId'], 'type': 'page', 'url': t['url']}
                          for t in self.browser.list_tabs()]
        with mock.patch.object(relay.time, 'sleep'), redirect_stdout(io.StringIO()) as output:
            relay.terminalize_legacy(live_jobs_reader=lambda: set(), tabs_reader=tabs)
        self.assertEqual(json.loads(output.getvalue())['results'][0]['decision'], 'WOULD_COMPLETE')
        historical = relay.job_state('historical'); historical['status'] = 'running'
        relay.job_write('historical', historical)
        with redirect_stdout(io.StringIO()) as output:
            relay.terminalize_legacy(live_jobs_reader=lambda: set(), tabs_reader=tabs)
        self.assertEqual(next(r for r in json.loads(output.getvalue())['results']
                              if r['job'] == 'legacy')['decision'], 'RUNNER_OR_TAB_AMBIGUOUS')

    def test_terminalize_legacy_still_generating_and_conflicting_response(self):
        st = self.job('legacy', url=WANT, tab='wanted')
        st.pop('binding_version'); st.pop('conversation_id'); relay.job_write('legacy', st)
        self.browser.pages['wanted'] = {'url': WANT, 'text': 'partial', 'composer': '',
                                        'messages': True, 'stop': True}
        original = (relay.JOBS / 'legacy/JOB.json').read_bytes()
        tabs = lambda _: [{'id': t['targetId'], 'type': 'page', 'url': t['url']}
                          for t in self.browser.list_tabs()]
        with redirect_stdout(io.StringIO()) as output:
            relay.terminalize_legacy(apply=True, live_jobs_reader=lambda: set(), tabs_reader=tabs)
        self.assertEqual(json.loads(output.getvalue())['results'][0]['decision'], 'STILL_GENERATING')
        self.browser.pages['wanted']['stop'] = False
        (relay.JOBS / 'legacy/RESPONSE.md').write_text('another reply\n')
        with mock.patch.object(relay.time, 'sleep'), redirect_stdout(io.StringIO()) as output:
            relay.terminalize_legacy(apply=True, live_jobs_reader=lambda: set(), tabs_reader=tabs)
        self.assertEqual(json.loads(output.getvalue())['results'][0]['decision'], 'RESPONSE_CONFLICT')
        self.assertEqual((relay.JOBS / 'legacy/JOB.json').read_bytes(), original)
        self.assertEqual(self.browser.created, [])
        self.assertEqual(self.browser.navigations, [])

    def test_terminalize_legacy_rejects_wrong_tab_and_unstable_reply(self):
        st = self.job('legacy', url=WANT, tab='missing')
        st.pop('binding_version'); st.pop('conversation_id'); relay.job_write('legacy', st)
        self.browser.pages['wanted'] = {'url': WANT, 'text': 'reply', 'composer': '',
                                        'messages': True, 'stop': False}
        tabs = lambda _: [{'id': t['targetId'], 'type': 'page', 'url': t['url']}
                          for t in self.browser.list_tabs()]
        with redirect_stdout(io.StringIO()) as output:
            relay.terminalize_legacy(live_jobs_reader=lambda: set(), tabs_reader=tabs)
        self.assertEqual(json.loads(output.getvalue())['results'][0]['decision'], 'WOULD_COMPLETE')
        self.job('active-sibling', url=SIBLING, tab='wanted')
        with redirect_stdout(io.StringIO()) as output:
            relay.terminalize_legacy(apply=True, live_jobs_reader=lambda: set(), tabs_reader=tabs)
        self.assertEqual(next(r for r in json.loads(output.getvalue())['results']
                              if r['job'] == 'legacy')['decision'], 'RUNNER_OR_TAB_AMBIGUOUS')
        sibling = relay.job_state('active-sibling'); sibling['status'] = 'done'
        relay.job_write('active-sibling', sibling)
        st['tab'] = 'wanted'; relay.job_write('legacy', st)
        reads = iter(['one', 'two'])
        def reply(url, tab, **kwargs):
            self.assertEqual(kwargs, {'reload': False, 'existing_only': True})
            text = next(reads)
            return {'tab': tab, 'text': text, 'state': {'stop': False,
                    'conversation_url_now': url, 'last_len': len(text)}}
        with mock.patch.object(relay.time, 'sleep'), redirect_stdout(io.StringIO()) as output:
            relay.terminalize_legacy(apply=True, live_jobs_reader=lambda: set(),
                                     tabs_reader=tabs, reply_reader=reply)
        self.assertEqual(json.loads(output.getvalue())['results'][0]['decision'], 'STILL_GENERATING_OR_UNSTABLE')
        self.assertFalse((relay.JOBS / 'legacy/RESPONSE.md').exists())
        self.assertFalse(list((relay.JOBS / 'legacy').glob('JOB.pre-reconcile-*')))

    def test_abandon_empty_ruling_dry_apply_exact_tabs_and_idempotency(self):
        names = ('glp30-final-npm-test-119-classification', 'glp38-final-dw-row-readback')
        urls = (WANT, SIBLING)
        for name, url in zip(names, urls):
            st = self.job(name, url=url, tab=name, submitted='2026-09-17T08:00:00+00:00')
            st.pop('binding_version'); st.pop('conversation_id'); relay.job_write(name, st)
            self.browser.pages[name] = {'url': url, 'text': '', 'composer': '', 'messages': False, 'stop': False}
        self.browser.pages.pop('sibling')
        original = {n: (relay.JOBS / n / 'JOB.json').read_bytes() for n in names}
        tabs = lambda _: [{'id': t['targetId'], 'type': 'page', 'url': t['url']}
                          for t in self.browser.list_tabs()]
        close = lambda tab, url: self.browser.pages.pop(tab, None) is not None
        with redirect_stdout(io.StringIO()) as output:
            rc = relay.abandon_empty_legacy(jobs=list(names), live_jobs_reader=lambda: set(), tabs_reader=tabs)
        self.assertEqual(rc, 0)
        self.assertEqual([r['decision'] for r in json.loads(output.getvalue())['results']], ['WOULD_ABANDON'] * 2)
        self.assertTrue(all((relay.JOBS / n / 'JOB.json').read_bytes() == original[n] for n in names))
        with redirect_stdout(io.StringIO()) as output:
            rc = relay.abandon_empty_legacy(jobs=list(names), apply=True, live_jobs_reader=lambda: set(),
                                            tabs_reader=tabs, close_reader=close)
        self.assertEqual(rc, 0)
        self.assertEqual([r['decision'] for r in json.loads(output.getvalue())['results']], ['ABANDONED'] * 2)
        self.assertEqual(relay.active_registry(), [])
        for name in names:
            state = relay.job_state(name)
            self.assertEqual(state['status'], 'legacy_abandoned')
            self.assertIn('Fable 2026-09-23', state['legacy_reconciliation']['ruling'])
            self.assertEqual(Path(state['legacy_reconciliation']['preimage']).read_bytes(), original[name])
            self.assertFalse((relay.JOBS / name / 'RESPONSE.md').exists())
            self.assertNotIn(name, self.browser.pages)
        self.assertEqual(len([r for r in relay.ledger_rows(self.cfg) if r.get('event') == 'legacy_abandon_empty']), 4)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(relay.abandon_empty_legacy(jobs=list(names), apply=True,
                             live_jobs_reader=lambda: set(), tabs_reader=tabs, close_reader=close), 0)
        self.assertEqual([r['decision'] for r in json.loads(output.getvalue())['results']], ['NOT_RUNNING_V1'] * 2)

    def test_abandon_empty_refuses_unruled_v2_young_runner_generation_and_reply(self):
        name = 'glp30-final-npm-test-119-classification'
        with self.assertRaisesRegex(SystemExit, 'EXPLICIT_RULING_JOBS'):
            relay.abandon_empty_legacy(jobs=[])
        with self.assertRaisesRegex(SystemExit, 'EXPLICIT_RULING_JOBS'):
            relay.abandon_empty_legacy(jobs=['unruled'])
        st = self.job(name, tab='wanted', submitted='2026-09-17T08:00:00+00:00')
        self.browser.pages['wanted'] = {'url': WANT, 'text': '', 'composer': '', 'messages': False, 'stop': False}
        tabs = lambda _: [{'id': t['targetId'], 'type': 'page', 'url': t['url']}
                          for t in self.browser.list_tabs()]
        def decision(live=lambda: set()):
            with redirect_stdout(io.StringIO()) as output:
                relay.abandon_empty_legacy(jobs=[name], apply=True, live_jobs_reader=live, tabs_reader=tabs,
                                           close_reader=lambda *_: self.fail('must not close'))
            return json.loads(output.getvalue())['results'][0]['decision']
        self.assertEqual(decision(), 'NOT_RUNNING_V1')
        st.pop('binding_version'); st.pop('conversation_id')
        st['submitted'] = relay.now_iso(); relay.job_write(name, st)
        self.assertEqual(decision(), 'TOO_NEW_OR_UNKNOWN_AGE')
        st['submitted'] = '2026-09-17T08:00:00+00:00'; relay.job_write(name, st)
        self.assertEqual(decision(lambda: {name}), 'RUNNER_OR_TAB_AMBIGUOUS')
        self.browser.pages['wanted']['stop'] = True
        self.assertEqual(decision(), 'NOT_VERIFIED_EMPTY_IDLE')
        self.browser.pages['wanted'].update(stop=False, text='answer', messages=True)
        self.assertEqual(decision(), 'NOT_VERIFIED_EMPTY_IDLE')
        self.browser.pages['wanted'].update(text='', messages=False)
        (relay.JOBS / name / 'RESPONSE.md').write_text('prior')
        self.assertEqual(decision(), 'RESPONSE_PRESENT')
        self.assertEqual(relay.job_state(name)['status'], 'running')
        self.assertFalse(list((relay.JOBS / name).glob('JOB.pre-reconcile-*')))
        self.assertEqual(self.browser.navigations, [])
        self.assertEqual(self.browser.created, [])
        self.assertEqual(self.browser.sent, [])

    def test_abandon_empty_refuses_second_read_change_and_sibling_claim(self):
        name = 'glp38-final-dw-row-readback'
        st = self.job(name, tab='wanted', submitted='2026-09-17T08:00:00+00:00')
        st.pop('binding_version'); st.pop('conversation_id'); relay.job_write(name, st)
        self.browser.pages['wanted'] = {'url': WANT, 'text': '', 'composer': '', 'messages': False, 'stop': False}
        tabs = lambda _: [{'id': t['targetId'], 'type': 'page', 'url': t['url']}
                          for t in self.browser.list_tabs()]
        self.job('other', SIBLING, tab='wanted')
        with redirect_stdout(io.StringIO()) as output:
            relay.abandon_empty_legacy(jobs=[name], apply=True, live_jobs_reader=lambda: set(), tabs_reader=tabs)
        self.assertEqual(json.loads(output.getvalue())['results'][0]['decision'], 'SIBLING_CLAIM')
        other = relay.job_state('other'); other['status'] = 'done'; relay.job_write('other', other)
        reads = iter(['', 'arrived'])
        def changed(url, tab, **kw):
            self.assertEqual(kw, {'reload': False, 'existing_only': True})
            text = next(reads)
            return {'tab': tab, 'text': text, 'state': {'conversation_url_now': url,
                    'stop': False, 'last_len': len(text)}}
        with redirect_stdout(io.StringIO()) as output:
            relay.abandon_empty_legacy(jobs=[name], apply=True, live_jobs_reader=lambda: set(),
                                       tabs_reader=tabs, reply_reader=changed)
        self.assertEqual(json.loads(output.getvalue())['results'][0]['decision'], 'TAB_OR_PAGE_CHANGED')
        self.assertEqual(relay.job_state(name)['status'], 'running')
        self.assertFalse(list((relay.JOBS / name).glob('JOB.pre-reconcile-*')))

    def test_reconcile_legacy_dry_run_apply_and_idempotent_preimage(self):
        old = self.job('old', status='running')
        old.pop('binding_version'); old.pop('conversation_id')
        relay.job_write('old', old)
        self.job('bound', status='running')
        raw = (relay.JOBS / 'old/JOB.json').read_bytes()
        def tabs(_account): return []
        with mock.patch.object(relay, '_legacy_live_jobs', return_value=set()):
            out = io.StringIO()
            with redirect_stdout(out):
                relay.reconcile_legacy(live_jobs_reader=lambda: set(), tabs_reader=tabs)
            self.assertEqual(json.loads(out.getvalue())['results'][0]['decision'], 'WOULD_ORPHAN')
            self.assertEqual((relay.JOBS / 'old/JOB.json').read_bytes(), raw)
            self.assertEqual(list((relay.JOBS / 'old').glob('JOB.pre-reconcile-*')), [])
            with redirect_stdout(io.StringIO()):
                relay.reconcile_legacy(apply=True, live_jobs_reader=lambda: set(), tabs_reader=tabs)
                relay.reconcile_legacy(apply=True, live_jobs_reader=lambda: set(), tabs_reader=tabs)
        state = relay.job_state('old')
        self.assertEqual(state['status'], 'legacy_orphaned')
        self.assertEqual(len(relay.active_registry()), 1)
        self.assertEqual(Path(state['legacy_reconciliation']['preimage']).read_bytes(), raw)
        rows = relay.ledger_rows(self.cfg)
        self.assertEqual([r['phase'] for r in rows if r.get('event') == 'legacy_reconcile'], ['STARTED', 'APPLIED'])
        self.assertRaisesRegex(SystemExit, 'BINDING_UNVERIFIED', relay.require_binding, state)

    def test_reconcile_legacy_keeps_live_runner_or_tab_and_unknown_evidence(self):
        for name in ('runner', 'tab', 'moved', 'unknown'):
            st = self.job(name)
            st.pop('binding_version'); st.pop('conversation_id')
            relay.job_write(name, st)
        def tabs(acct):
            return [{'id':'other', 'type':'page', 'url': WANT}]
        # A matching conversation is evidence even when the old target id changed.
        out = io.StringIO()
        with redirect_stdout(out):
            relay.reconcile_legacy(apply=True, live_jobs_reader=lambda: {'runner'}, tabs_reader=tabs)
        self.assertTrue(all(relay.job_state(n)['status'] == 'running' for n in ('runner','tab','moved','unknown')))
        self.assertEqual(list(relay.JOBS.glob('*/JOB.pre-reconcile-*')), [])
        with redirect_stdout(io.StringIO()):
            relay.reconcile_legacy(apply=True, live_jobs_reader=lambda: (_ for _ in ()).throw(OSError('ps failed')),
                                   tabs_reader=lambda _: [])
        self.assertEqual(len(relay.active_registry()), 4)
        self.assertEqual([r for r in relay.ledger_rows(self.cfg) if r.get('event') == 'legacy_reconcile'], [])

    def test_reconcile_legacy_keeps_exact_live_tab_and_runner(self):
        st = self.job('legacy')
        st.pop('binding_version'); st.pop('conversation_id'); relay.job_write('legacy', st)
        with redirect_stdout(io.StringIO()):
            relay.reconcile_legacy(apply=True, live_jobs_reader=lambda: set(),
                tabs_reader=lambda _: [{'id':'sibling','type':'page','url':SIBLING}])
            relay.reconcile_legacy(apply=True, live_jobs_reader=lambda: {'legacy'}, tabs_reader=lambda _: [])
        self.assertEqual(relay.job_state('legacy')['status'], 'running')

    def test_reconcile_detects_direct_poll_runner_without_network(self):
        from scripts import pro_relay_landing_monitor as monitor
        listing = '123 /usr/bin/python3 -B scripts/gpt6pro_relay.py poll --job legacy\n'
        with mock.patch.object(monitor, 'read_ps', return_value=listing):
            self.assertIn('legacy', relay._legacy_live_jobs())

    def test_live_jobs_skips_malformed_non_python_wrapper_but_fails_closed_for_python(self):
        from scripts import pro_relay_landing_monitor as monitor
        listing = "123 /usr/bin/bash -c 'unterminated\n124 /usr/bin/python3 scripts/gpt6pro_relay.py poll --job legacy\n"
        with mock.patch.object(monitor, 'read_ps', return_value=listing):
            self.assertIn('legacy', relay._legacy_live_jobs())
        with mock.patch.object(monitor, 'read_ps', return_value="123 /usr/bin/python3 -c 'unrelated prompt\n"):
            self.assertEqual(relay._legacy_live_jobs(), set())
        with mock.patch.object(monitor, 'read_ps', return_value="123 /usr/bin/python3 scripts/gpt6pro_relay.py 'unterminated\n"):
            with self.assertRaisesRegex(ValueError, 'unparseable possible Python relay process'):
                relay._legacy_live_jobs()

    def test_malformed_registry_fails_closed(self):
        relay.job_dir("broken")
        (relay.JOBS / "broken/JOB.json").write_text("{")
        with self.assertRaisesRegex(SystemExit, "BINDING_REGISTRY_UNREADABLE"):
            relay.cmd_submit(self.args())
        self.assertEqual(self.browser.created, [])

    def test_other_account_does_not_own_this_accounts_id(self):
        self.job("other", WANT, account="account-b")
        relay.require_binding(self.job())

    def test_submit_holds_lock_through_commit_but_not_wait(self):
        self.browser.on_run = lambda: self.assertTrue(relay._HELD_LOCKS)
        original = relay.job_write
        def write(job, st):
            self.assertTrue(relay._HELD_LOCKS)
            original(job, st)
        def poll(*args, **kwargs):
            self.assertFalse(relay._HELD_LOCKS)
            return 0
        with mock.patch.object(relay, "job_write", side_effect=write), mock.patch.object(relay, "poll", side_effect=poll):
            self.assertEqual(relay.cmd_submit(self.args(wait=True)), 0)

    def test_poll_releases_lock_before_sleep(self):
        self.job()
        calls = iter([(3, 30), (0, 0)])
        def once(*args):
            self.assertTrue(relay._HELD_LOCKS)
            return next(calls)
        def sleep(seconds):
            self.assertFalse(relay._HELD_LOCKS)
            self.assertEqual(seconds, 30)
        with mock.patch.object(relay, "_poll_once", side_effect=once), mock.patch.object(relay.time, "sleep", side_effect=sleep):
            self.assertEqual(relay.poll("job", True, 30, 3600), 0)

    def test_all_browser_scripts_take_the_same_lock(self):
        def run(*args, **kwargs):
            self.assertTrue(relay._HELD_LOCKS)
            return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")
        with mock.patch.object(relay.subprocess, "run", side_effect=run):
            self.assertEqual(REAL_BU("pass"), "ok")
        self.assertFalse(relay._HELD_LOCKS)

    def test_process_lock_serializes_same_account_not_other_accounts(self):
        code = '''import importlib.util, pathlib, sys
spec=importlib.util.spec_from_file_location("child_relay", sys.argv[1])
r=importlib.util.module_from_spec(spec); spec.loader.exec_module(r)
r.JOBS=pathlib.Path(sys.argv[2]); r.use_account({"cdp_port":int(sys.argv[3])})
print("ready", flush=True)
key=r.hashlib.sha256(r.BU_ENV["BU_CDP_URL"].encode()).hexdigest()
r.BUSINESS=r.JOBS.parent
locks=r.BUSINESS / "ops/gpt6pro-relay/locks"; locks.mkdir(parents=True, exist_ok=True)
with (locks / (key + ".lock")).open("a") as fh:
    try:
        r.fcntl.flock(fh, r.fcntl.LOCK_EX | r.fcntl.LOCK_NB)
    except BlockingIOError:
        print("blocked", flush=True)
    else:
        print("free", flush=True)
        r.fcntl.flock(fh, r.fcntl.LOCK_UN)
with r.account_lock():
    print("acquired", flush=True)
    sys.stdin.readline()
'''
        children = []
        def child(port):
            proc = subprocess.Popen([sys.executable, "-u", "-c", code, str(RELAY_PATH), str(relay.JOBS), str(port)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            children.append(proc)
            self.assertTrue(select.select([proc.stdout], [], [], 5)[0], "child did not start")
            self.assertEqual(proc.stdout.readline(), b"ready\n")
            self.assertTrue(select.select([proc.stdout], [], [], 5)[0], "child did not probe the lock")
            return proc, proc.stdout.readline()
        try:
            with relay.account_lock():
                same, status = child(9340)
                self.assertEqual(status, b"blocked\n")
                other, status = child(9341)
                self.assertEqual(status, b"free\n")
                self.assertTrue(select.select([other.stdout], [], [], 5)[0], "other account was unnecessarily blocked")
                self.assertEqual(other.stdout.readline(), b"acquired\n")
            self.assertTrue(select.select([same.stdout], [], [], 5)[0], "same account did not acquire after release")
            self.assertEqual(same.stdout.readline(), b"acquired\n")
            for proc in children:
                out, err = proc.communicate(b"release\n", timeout=5)
                self.assertEqual(proc.returncode, 0, out + err)
        finally:
            for proc in children:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate()


# Fixture owner contract; never invoked as application code. The actual captured
# callbacks are exercised below; the data/React tree and sinks remain synthetic.
MODEL_OWNER_FIXTURE = r'''
function attachModelOwner(trigger,selectedModel){
    function ownerType(e){const {selectedModel:eq,onModelChange:eF,onThinkingEffortChange:eH}=e;
        return {onCommitPower:null,onOpenChange:null,surface:'CHATGPT_MODEL_PICKER_SURFACE_COMPOSER'};}
    const props={selectedModel,onModelChange:()=>{},models:{},modelLimits:{},
        xHighExperimentResetContextKey:null,isLoading:false,isError:false,disabled:false};
    trigger.__reactFiber$fixture={type:ownerType,memoizedProps:props,return:null};
    return props;
}
'''


# Byte-exact source59 and source68 callback slices from retained capture
# 24cd58d14748ee1a62621b61841bba739a8e0260bb980368a8db46cf5fa96182.
# React/import composition is a fixture; HTMLElement.click has detail=0 and
# must NOT acquire an invented onclick toggle. Production never calls these.
MODEL_TRIGGER_FIXTURE = r'''
function installCloseTrigger(trigger,onOpenChange){
 const eB=false,Z=false,e6=true,eY={current:false},eG={current:null},e8=()=>{},te=onOpenChange;
 const pointer=e=>{!eB||e.defaultPrevented||0!==e.button||e.ctrlKey||(e8(),e.preventDefault())};
 const click=e=>{if(eB&&!Z&&!e.defaultPrevented&&0===e.button&&!e.ctrlKey&&0!==e.detail){let e=!e6||eY.current;te(e),e&&eG.current?.focus({preventScroll:!0})}};
 const state={triggerRef:null,triggerId:'trigger',contentId:'menu',get open(){return trigger.getAttribute('aria-expanded')==='true'},onOpenToggle:()=>onOpenChange(!state.open),onOpenChange};
 const N=()=>state,u=()=>({}),o={b:()=>null},T={jsx:(type,props)=>({type,props})},s={a:'Anchor'},E={a:{button:'Button'}},i={a:(user,internal)=>e=>{user?.(e);if(!e.defaultPrevented)internal(e)}};
 const render=function(e,t){let{__scopeDropdownMenu:n,disabled:r=!1,..._}=e,a=N("DropdownMenuTrigger",n),l=u(n),C=(0,o.b)(t,a.triggerRef);return(0,T.jsx)(s.a,{asChild:!0,...l,children:(0,T.jsx)(E.a.button,{type:"button",id:a.triggerId,"aria-haspopup":"menu","aria-expanded":a.open,"aria-controls":a.open?a.contentId:void 0,"data-state":a.open?"open":"closed","data-disabled":r?"":void 0,disabled:r,..._,ref:C,onPointerDown:(0,i.a)(e.onPointerDown,e=>{!r&&0===e.button&&!1===e.ctrlKey&&(a.onOpenToggle(),a.open||e.preventDefault())}),onKeyDown:(0,i.a)(e.onKeyDown,e=>{!r&&(["Enter"," "].includes(e.key)&&a.onOpenToggle(),"ArrowDown"===e.key&&a.onOpenChange(!0),["Enter"," ","ArrowDown"].includes(e.key)&&e.preventDefault())})})})};
 const handlers=render({onClick:click,onPointerDown:pointer},null).props.children.props;
 trigger.onclick=handlers.onClick;trigger.onpointerdown=handlers.onPointerDown;
}
function pointerClose(){document.querySelector('button').onpointerdown({button:0,ctrlKey:false,detail:1,defaultPrevented:false,preventDefault(){this.defaultPrevented=true}});}
'''


class NativeComposerTests(unittest.TestCase):
    def test_close_pointer_event_actual_emitted_caller(self):
        """Captured callbacks + real emitted selection caller, no browser/CDP.

        Mutation occurs after maximum proof, isolating close refusal. Event
        transport and DOM are fixtures, not evidence of live event delivery.
        """
        tree = ast.parse(Path(__file__).read_text())
        def literal(method, variable):
            fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == method)
            return next(ast.literal_eval(n.value) for n in ast.walk(fn) if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == variable for t in n.targets))
        fixture = literal('test_malformed_owner_cannot_hide_from_any_emitted_consumer', 'fixture')
        node = literal('test_malformed_owner_cannot_hide_from_any_emitted_consumer', 'node')
        pipeline = literal('test_committed_maximum_selection_native', 'pipeline')
        proc = subprocess.Popen(['node', '-e', node], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            def call(expr='true', setup=None):
                proc.stdin.write(json.dumps({'expr': expr, 'setup': setup})+'\n'); proc.stdin.flush()
                result = json.loads(proc.stdout.readline())
                self.assertNotIn('error', result)
                return result.get('value')
            cases = {
                'accept': '', 'reject_parent': '',
                'wrong_document': 'performance.timeOrigin=0',
                'nonempty': "txt.data='private draft'",
                'messages': "const query=document.querySelectorAll;document.querySelectorAll=s=>s==='[data-message-author-role]'?[{}]:query(s)",
                'duplicate_trigger': "const query=document.querySelectorAll;document.querySelectorAll=s=>s==='button[aria-label=\"Select ChatGPT model\"]'?[trigger,trigger]:query(s)",
                'unknown_expanded': "trigger.setAttribute('aria-expanded','unknown')",
                'unanchored': "trigger.setAttribute('aria-controls','other')",
                'zero_geometry': 'trigger.getBoundingClientRect=()=>({left:0,top:0,width:0,height:30})',
                'nonfinite_geometry': 'trigger.getBoundingClientRect=()=>({left:NaN,top:0,width:100,height:30})',
                'alternate_missing': 'delete owner.alternate.memoizedProps.models',
                'alternate_disagrees': "owner.alternate.memoizedProps.selectedModel={slug:'gpt-6',thinkingEffort:'standard'}",
                'malformed_owner': 'owner.return={type:owner.type,memoizedProps:{},return:null}',
                'no_delivery': '',
            }
            for retained in (False, True):
                scripts = []
                with mock.patch.object(relay, 'bu_json', side_effect=lambda s, **kw: scripts.append(s) or {}):
                    relay.send_message('fixture', **({'retained_tab': 'offline', 'time_origin': 12345.5} if retained else {}))
                generated = scripts[0]
                branch = compile(generated[generated.index('# All paths must commit'):generated.index('# Settle:')], '<actual-close-caller>', 'exec')
                for case, mutation in cases.items():
                    # Fresh _fresh is URL/message bound, not timeOrigin bound.
                    # Do not invent a stronger fresh-document oracle in fixtures.
                    if case == 'wrong_document' and not retained: continue
                    with self.subTest(retained=retained, case=case):
                        call(setup=fixture+MODEL_OWNER_FIXTURE+MODEL_TRIGGER_FIXTURE+
                             '(()=>{const accepts='+json.dumps(case != 'reject_parent')+';'+pipeline+'})();'+
                             "txt.data='';trigger.getBoundingClientRect=()=>({left:0,top:0,width:100,height:30});owner.alternate={type:owner.type,memoizedProps:{...props}};")
                        mouse_events, insertions = [], []
                        mutated = False
                        def refuse(reason, **kw): raise RuntimeError(reason)
                        def fresh():
                            # Test the real retained geometry's synchronous
                            # document guard, not a canned _fresh refusal.
                            return HOME
                        def bound(tid, url, expr):
                            nonlocal mutated
                            self.assertEqual((tid, url), ('offline', HOME))
                            result = call(expr)
                            if expr.endswith('return full6ProMaximum();})()') and not mutated:
                                mutated = True
                                if mutation: call(mutation+';true')
                            return result
                        def mouse(method, **kw):
                            self.assertEqual(method, 'Input.dispatchMouseEvent')
                            self.assertEqual((kw['session_id'], kw['button'], kw['clickCount']), ('offline', 'left', 1))
                            mouse_events.append(kw)
                            if kw['x'] == 187 and kw['type'] == 'mouseReleased':
                                call('selectMaximum();owner.alternate.memoizedProps.selectedModel=props.selectedModel;true')
                            elif kw['x'] == 50 and kw['type'] == 'mousePressed' and case != 'no_delivery':
                                call('pointerClose();'+('' if case == 'alternate_disagrees' else 'owner.alternate.memoizedProps.selectedModel=props.selectedModel;')+'true')
                        env = {'st': {'model': '6 Pro'}, 'RETAINED': retained, 'tab': {'targetId': 'offline'},
                               'fresh': HOME, 'json': json, 'time': types.SimpleNamespace(sleep=lambda _: None),
                               '_bound_js': bound, '_fresh': fresh, 'cdp': mouse,
                               '_sessions': {'offline': 'offline'}, '_binding_error': refuse}
                        if case == 'accept':
                            exec(branch, env)
                            insertions.append('admitted after real model branch')
                            self.assertEqual(call('events'), ['selected', 'parent', 'closed'])
                        else:
                            with self.assertRaisesRegex(RuntimeError, 'WRONG_MODEL|document_changed'):
                                exec(branch, env)
                            self.assertEqual(insertions, [])
                        close_events = [e for e in mouse_events if e['x'] == 50]
                        expected_close = case in ('accept', 'reject_parent', 'alternate_missing', 'alternate_disagrees', 'malformed_owner', 'no_delivery')
                        self.assertEqual([e['type'] for e in close_events], ['mousePressed', 'mouseReleased'] if expected_close else [])
                        self.assertEqual(call('sends'), 0)
                        self.assertEqual(call('getters'), 0)
                        self.assertEqual(len(mouse_events), 4 if expected_close else 2)
        finally:
            proc.stdin.write('{"stop":true}\n'); proc.stdin.flush(); proc.stdin.close()
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr.read())

    def test_malformed_owner_cannot_hide_from_any_emitted_consumer(self):
        """Reviewer's valid + malformed owner counterexample, pure Node VM.

        Execute initial selection, chunk preparation and both actual Send guards;
        only DOM/React and effect sinks are synthetic. No browser or live state.
        """
        scripts = []
        with mock.patch.object(relay, 'bu_json', side_effect=lambda s, **kw: scripts.append(s) or {}):
            relay.send_message('fixture')
            relay.send_message('fixture', retained_tab='offline', time_origin=12345.5)
        fresh, retained = scripts
        tree = ast.parse(fresh)
        probes = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                  and isinstance(n.value, str) and 'function committed6ProMaximum' in n.value]
        fresh_click = next(x for x in probes if 'const b=document.querySelector' in x
                           and 'b.click();return true;' in x)
        atomic = next(n.value for n in ast.walk(ast.parse(retained)) if isinstance(n, ast.Constant)
                      and isinstance(n.value, str) and n.value.startswith('(()=>{const retainedOrigin='))
        branch = compile(fresh[fresh.index('# All paths must commit'):fresh.index('# Settle:')],
                         '<actual-model-selection>', 'exec')
        prepare = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_prepare')
        # Reuse the existing byte-exact captured callback chain, not a new oracle.
        method = next(n for n in ast.walk(ast.parse(Path(__file__).read_text()))
                      if isinstance(n, ast.FunctionDef) and n.name == 'test_committed_maximum_selection_native')
        pipeline = next(ast.literal_eval(n.value) for n in ast.walk(method) if isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == 'pipeline' for t in n.targets))
        fixture = r'''
window=globalThis;getComputedStyle=()=>({display:'block',visibility:'visible'});
performance={timeOrigin:12345.5};
function el(attrs={},text=''){return {attrs,innerText:text,parentElement:null,hidden:false,inert:false,disabled:false,getClientRects:()=>[1],getBoundingClientRect:()=>({left:0,top:0,width:100,height:30}),getAttribute(k){return this.attrs[k]??null},setAttribute(k,v){this.attrs[k]=v},hasAttribute(k){return k in this.attrs},click(){this.onclick?.({button:0,ctrlKey:false,detail:0,defaultPrevented:false})},closest(){return view}}}
const trigger=el({'aria-label':'Select ChatGPT model','aria-expanded':'true','aria-controls':'menu','data-selected-reasoning-effort':'medium','data-codex-intelligence-trigger':'true','data-composer-navigation-target':'reasoning'},'Pro');
const view=el({'aria-hidden':'false'}),menu=el({role:'menu'});menu.id='menu';
const row=el({},'6 Pro'),badge=el(),power=el(),slider=el({'role':'slider','aria-valuemin':'0','aria-valuemax':'4','aria-valuenow':'4'}),rail=el();
rail.getBoundingClientRect=()=>({width:200,height:30,right:200,top:0});
row.querySelector=()=>badge;power.querySelectorAll=()=>[slider];
view.querySelectorAll=s=>s.includes('view-toggle')?[row]:s.includes('power-slider')?[rail]:[power];menu.querySelectorAll=()=>[view];
const txt={nodeType:3,data:'fixture'},p={nodeType:1,tagName:'P',attributes:[],childNodes:[txt],lastChild:txt};
const composer=el({contenteditable:'true'});composer.tagName='DIV';composer.classList={contains:()=>true};composer.childNodes=[p];composer.lastChild=p;
const send=el();send.onclick=()=>sends++;
let selection={isCollapsed:true};composer.focus=()=>document.activeElement=composer;
window.getSelection=()=>selection;selection.removeAllRanges=()=>{};selection.addRange=r=>{selection.anchorNode=r.node;selection.anchorOffset=r.offset};
document={readyState:'complete',title:'ChatGPT',body:{innerText:''},
 createRange:()=>({setStart(n,o){this.node=n;this.offset=o},collapse(){}}),
 querySelector:s=>s.includes('prompt-textarea')?composer:s.includes('send-button')?send:s==='button'?trigger:null,
 querySelectorAll:s=>s==='button[aria-label="Select ChatGPT model"]'?[trigger]:s==='[id]'?[menu]:s==='button'?[trigger,send]:[]};
'''
        node = r'''
const vm=require('vm'),rl=require('readline').createInterface({input:process.stdin,crlfDelay:Infinity});
let ctx;
(async()=>{for await(const line of rl){const r=JSON.parse(line);if(r.stop)break;
 try{if(r.setup){ctx=vm.createContext({setTimeout:()=>1,clearTimeout(){}});vm.runInContext(r.setup,ctx);}
 process.stdout.write(JSON.stringify({value:vm.runInContext(r.expr||'true',ctx)})+'\n');}
 catch(e){process.stdout.write(JSON.stringify({error:e.stack})+'\n');}
}})();
'''
        proc = subprocess.Popen(['node', '-e', node], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            def call(expr='true', setup=None):
                proc.stdin.write(json.dumps({'expr': expr, 'setup': setup})+'\n'); proc.stdin.flush()
                result = json.loads(proc.stdout.readline())
                self.assertNotIn('error', result)
                return result.get('value')
            def refuse(reason, **kwargs): raise RuntimeError(reason)
            def outcome(action):
                try: action()
                except RuntimeError as exc: return str(exc)
                return None
            cases = {
                'positive': '',
                'duplicate_accessor': 'const bad={...props};Object.defineProperty(bad,"selectedModel",{get(){getters++;return null}});owner.return={type:owner.type,memoizedProps:bad,return:null}',
                'duplicate_missing': 'const bad={...props};delete bad.models;owner.return={type:owner.type,memoizedProps:bad,return:null}',
                'duplicate_null_props': 'owner.return={type:owner.type,memoizedProps:null,return:null}',
                'duplicate_primitive_props': 'owner.return={type:owner.type,memoizedProps:7,return:null}',
                'alternate_missing': 'const bad={...props};delete bad.models;owner.alternate={type:owner.type,memoizedProps:bad}',
                'alternate_accessor': 'const bad={...props};Object.defineProperty(bad,"models",{get(){getters++;return {}}});owner.alternate={type:owner.type,memoizedProps:bad}',
            }
            for case, mutation in cases.items():
                with self.subTest(case=case):
                    call(setup=fixture+MODEL_OWNER_FIXTURE+MODEL_TRIGGER_FIXTURE+'(()=>{const accepts=true;'+pipeline+'})();')
                    call("txt.data='';true")
                    env = {'st': {'model': '6 Pro'}, 'RETAINED': False, 'tab': {'targetId': 'offline'},
                           'fresh': HOME, 'json': json, 'time': types.SimpleNamespace(sleep=lambda n: None),
                           '_bound_js': lambda tid, url, expr: call(expr), '_fresh': lambda: None,
                           'cdp': lambda method, **kw: call('selectMaximum();true' if kw['x']==187 and kw['type']=='mouseReleased' else 'pointerClose();true' if kw['x']==50 and kw['type']=='mousePressed' else 'true'),
                           '_sessions': {'offline': 'offline'}, '_binding_error': refuse}
                    # Establish a real accepted transition, then add the competing
                    # owner before re-running initial admission and every consumer.
                    exec(branch, env)
                    self.assertEqual(call('events'), ['selected', 'parent', 'closed'])
                    if mutation: call(mutation+';true')
                    # Begin the second close-only scenario open; opening behavior
                    # is separately scoped and is not invented by onclick here.
                    call("trigger.setAttribute('aria-expanded','true');true")
                    initial = outcome(lambda: exec(branch, env))
                    call("txt.data='fixture';true")
                    exec(compile(ast.Module(body=[prepare], type_ignores=[]), '<actual-prepare>', 'exec'), env)
                    chunk = outcome(lambda: env['_prepare']('fixture'))
                    actual = {'initial': initial, 'chunk': chunk, 'fresh': call(fresh_click),
                              'retained': call(atomic), 'clicks': call('sends'), 'getters': call('getters')}
                    expected = ({'initial': None, 'chunk': None, 'fresh': True,
                                 'retained': {'clicked': True}, 'clicks': 2, 'getters': 0} if case == 'positive' else
                                {'initial': 'WRONG_MODEL', 'chunk': 'WRONG_MODEL', 'fresh': False,
                                 'retained': {'clicked': False, 'reason': 'model'}, 'clicks': 0, 'getters': 0})
                    self.assertEqual(actual, expected)
        finally:
            proc.stdin.write('{"stop":true}\n'); proc.stdin.flush(); proc.stdin.close()
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr.read())

    def test_committed_maximum_selection_native(self):
        """Run actual generated model branch/JS; preview and model-text cannot commit."""
        import inspect
        import os
        import textwrap
        scripts = []
        with mock.patch.object(relay, 'bu_json', side_effect=lambda s, **kw: scripts.append(s) or {}):
            relay.send_message('fixture')
        generated = scripts[0]
        with mock.patch.object(relay, 'bu_json', side_effect=lambda s, **kw: scripts.append(s) or {}):
            relay.send_message('fixture', retained_tab='offline', time_origin=12345.5)
        retained = next(n.value for n in ast.walk(ast.parse(scripts[1]))
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and n.value.startswith('(()=>{const retainedOrigin='))
        fresh_click = next(n.value for n in ast.walk(ast.parse(generated))
                           if isinstance(n, ast.Constant) and isinstance(n.value, str)
                           and 'function committed6ProMaximum' in n.value
                           and 'b.click();return true;' in n.value
                           and 'const b=document.querySelector' in n.value)
        marker = '# All paths must commit' if '# All paths must commit' in generated else '# On a fresh chat'
        branch = compile(generated[generated.index(marker):generated.index('# Settle:')], '<actual-model-selection>', 'exec')
        tree = ast.parse(generated)
        prepare = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_prepare')
        method = ast.parse(textwrap.dedent(inspect.getsource(self.test_current_picker_visibility_contract_in_native_dom))).body[0]
        html = ast.literal_eval(next(n.value for n in method.body if isinstance(n, ast.Assign)
                               and any(isinstance(t, ast.Name) and t.id == 'html' for t in n.targets)))
        html = html.replace('aria-expanded="true"', 'aria-expanded="true" data-selected-reasoning-effort="medium" data-codex-intelligence-trigger="true" data-composer-navigation-target="reasoning"')
        html = html.replace('<div data-reasoning-slider=', '<div data-model-picker-power-slider style="width:200px;height:30px"></div><div data-reasoning-slider=')
        html += '<div id="prompt-textarea" contenteditable="true" class="ProseMirror"><p><br class="ProseMirror-trailingBreak"></p></div>'
        node = NATIVE_COMPOSER_FIXTURE.replace("const f=JSON.parse(fs.readFileSync(0,'utf8'));", 'const f={};')
        left, right = node.index('const output={};'), node.index('}finally{await cdp')
        node = node[:left] + r'''
const rl=require('readline').createInterface({input:process.stdin,crlfDelay:Infinity});
process.stdout.write(JSON.stringify({ready:true})+'\n');
for await(const line of rl){const r=JSON.parse(line);if(r.stop)break;
try{process.stdout.write(JSON.stringify({value:await js(r.expression)})+'\n');}
catch(e){process.stdout.write(JSON.stringify({error:e.message})+'\n');}}
''' + node[right:]
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.Popen(['node', '-e', node], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, env={**os.environ, 'TMPDIR': tmp})
            try:
                self.assertEqual(json.loads(proc.stdout.readline()), {'ready': True})
                def native(expression):
                    proc.stdin.write(json.dumps({'expression': expression})+'\n'); proc.stdin.flush()
                    result = json.loads(proc.stdout.readline())
                    self.assertNotIn('error', result)
                    return result.get('value')
                # Byte-exact callback slices from source70 in the bounded upstream
                # capture 24cd58d14748ee1a62621b61841bba739a8e0260bb980368a8db46cf5fa96182.
                # React/imported helpers, parent sinks and rerenders are fixtures.
                # The later read-only schema capture observed own-data
                # {slug:'gpt-6-pro', thinkingEffort:null}, not maximum/medium effort.
                pipeline = r'''
window.mouse=0;window.sends=0;window.getters=0;window.events=[];
const trigger=document.querySelector('button');
let eq={slug:'gpt-5-6-thinking',thinkingEffort:'standard'},ti=null,tl=eq;
const props=attachModelOwner(trigger,eq), owner=trigger.__reactFiber$fixture;
const eD={},eV=false,e4=undefined,eH=undefined,eX=true,tM=false,tQ=true,ta='simple';
const q={i:()=>undefined},s=()=>((a,b)=>JSON.stringify(a)===JSON.stringify(b));
const tH=[{modelSlug:'gpt-5-6-thinking',thinkingEffort:'standard'},{modelSlug:'gpt-6-pro',thinkingEffort:null}],tR=undefined;
const to=f=>{ti=typeof f==='function'?f(ti):f;},e3=()=>{},e2=()=>events.push('telemetry');
const eF=m=>{events.push('parent');if(accepts){eq=m;props.selectedModel=m;}};
const ty={current:null},tr=()=>{},te=()=>{},_={a:{prefetch(){}}};
const tI=v=>{trigger.setAttribute('aria-expanded',String(v));events.push(v?'open':'closed');};
const tY=function(e,t){let n=void 0===t?"model":t;to(t=>({model:e,base:eq,intent:"thinking-effort"!==n||e.slug!==eq.slug||(e.versionId??(0,q.i)(eD,e.slug))!==(eq.versionId??(0,q.i)(eD,eq.slug))||null!=t&&s()(t.base,eq)&&"thinking-effort"!==t.intent?"model":"thinking-effort"}))};
const tJ=function(e){let t=tH[e.powerSettingIndex];if(null!=t)return{slug:t.modelSlug,thinkingEffort:t.thinkingEffort,versionId:tR?.slugs.includes(t.modelSlug)?tR.id:(0,q.i)(eD,t.modelSlug)}};
const select=e=>{let t=tH[e.powerSettingIndex];null!=t&&t.modelSlug!==tl.slug&&e3(t.modelSlug);let n=tJ(e);null!=n&&tY(n,"thinking-effort")};
const commit=(e,t)=>{let n=tH[e.powerSettingIndex],a=tH[t.powerSettingIndex];n?.thinkingEffort!=null&&a?.thinkingEffort!=null&&n.thinkingEffort!==a.thinkingEffort&&e2("CHATGPT_MODEL_PICKER_UI_EVENT_TYPE_THINKING_EFFORT_SELECTED")};
const close=function(e){null!=ty.current&&(window.clearTimeout(ty.current),ty.current=null),e?(to(null),eV&&null==e4&&(0,b.c)(eQ,tU),_.a.prefetch(),eX||tM||(e2("CHATGPT_MODEL_PICKER_UI_EVENT_TYPE_SURFACE_OPENED"),!eV&&tQ&&"simple"===ta&&e2("CHATGPT_MODEL_PICKER_UI_EVENT_TYPE_THINKING_EFFORT_DROPDOWN_CLICKED"))):(s()(tl,eq)||(ti?.intent==="thinking-effort"&&null!=eH?eH(tl.thinkingEffort):eF(tl)),to(null),ty.current=window.setTimeout(()=>{ty.current=null,tr("simple"),te("")},350)),tI(e)};
function render(){tl=(null!=ti&&(s()(ti.base,eq)||eV&&s()((0,ei.b)(eD,ti.base,e4),eq))?ti.model:null)??eq;}
window.selectMaximum=()=>{select({powerSettingIndex:1});render();commit({powerSettingIndex:1},{powerSettingIndex:0});render();events.push('selected');};
installCloseTrigger(trigger,v=>{close(v);render();});
window.props=props;window.owner=owner;
'''
                for case in ['accept', 'reject', 'duplicate_trigger', 'duplicate_owner', 'getter', 'document_drift']:
                    with self.subTest(case=case):
                        native('document.body.innerHTML='+json.dumps(html)+';'+MODEL_OWNER_FIXTURE+MODEL_TRIGGER_FIXTURE+
                               '(()=>{const accepts='+json.dumps(case != 'reject')+';'+pipeline+'})();true')
                        if case == 'duplicate_trigger':
                            native('document.body.append(document.querySelector("button").cloneNode(true));true')
                        if case == 'duplicate_owner':
                            native('owner.return={type:owner.type,memoizedProps:props,return:null};true')
                        if case == 'getter':
                            native('Object.defineProperty(props,"selectedModel",{get(){getters++;return null;},set(){}});true')
                        def mouse(method, **kwargs):
                            self.assertEqual(method, 'Input.dispatchMouseEvent', 'no insertion in model admission')
                            center = native('(()=>{const r=document.querySelector("button").getBoundingClientRect();return r.left+r.width/2})()')
                            action = ('pointerClose();' if kwargs['x']==center and kwargs['type']=='mousePressed' else
                                      'selectMaximum();' if kwargs['x']!=center and kwargs['type']=='mouseReleased' else '')
                            native('window.mouse++;'+action+'true')
                        def refuse(reason, **kwargs): raise RuntimeError(reason)
                        def bound(tab, url, expr):
                            if case == 'document_drift' and native('window.mouse'):
                                raise RuntimeError('retained_document_changed')
                            return native(expr)
                        env = {'st': {'model': '6 Pro'}, 'RETAINED': False, 'tab': {'targetId':'offline'},
                               'fresh': HOME, 'json': json, 'time': types.SimpleNamespace(sleep=lambda n: None),
                               '_bound_js': bound, '_fresh': lambda: None,
                               'cdp': mouse, '_sessions': {'offline':'offline'}, '_binding_error': refuse,
                               '__import__': __import__}
                        if case != 'accept':
                            with self.assertRaisesRegex(RuntimeError, 'WRONG_MODEL|retained_document_changed'): exec(branch, env)
                        else:
                            exec(branch, env)
                            self.assertEqual(env['st']['model'], '6 Pro')
                            self.assertEqual(native('window.events'), ['selected', 'parent', 'closed'])
                            self.assertEqual(native('window.mouse'), 4)
                            self.assertEqual(native('document.querySelector("button").getAttribute("aria-expanded")'), 'false')
                            # Every real emitted consumer sees the same authoritative gate.
                            probes = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)
                                      and isinstance(n.value, str) and 'function committed6ProMaximum' in n.value]
                            committed = next(x for x in probes if x.endswith('return committed6ProMaximum();})()'))
                            self.assertTrue(native(committed))
                            native('document.querySelector("#prompt-textarea").remove();true')
                            native('document.body.insertAdjacentHTML("beforeend",\'<div id="prompt-textarea" contenteditable="true" class="ProseMirror"><p>fixture</p></div><button data-testid="send-button">Send</button>\');document.querySelector("[data-testid=send-button]").onclick=()=>sends++;true')
                            atomic = retained.replace('const retainedOrigin=12345.5;',
                                                      'const retainedOrigin='+str(native('performance.timeOrigin'))+';')
                            self.assertTrue(native(fresh_click))
                            self.assertEqual(native(atomic), {'clicked': True})
                            self.assertEqual(native('window.sends'), 2)
                            native('window.sends=0;true')
                            # Negative Send cases use the actual emitted synchronous guards.
                            for mutation, reason in [
                                ('owner.return={type:owner.type,memoizedProps:props,return:null}', 'model'),
                                ('props.isLoading=true', 'model'),
                                ('props.selectedModel={slug:"gpt-5-6-thinking",thinkingEffort:"standard"}', 'model'),
                                ('props.selectedModel={slug:"gpt-6-pro",thinkingEffort:"medium"}', 'model'),
                                ('document.querySelector("#prompt-textarea p").textContent="changed"', 'composer'),
                            ]:
                                native(mutation+';true')
                                self.assertEqual(native(atomic), {'clicked': False, 'reason': reason})
                                if reason == 'model': self.assertFalse(native(fresh_click))
                                native('owner.return=null;props.isLoading=false;props.selectedModel={slug:"gpt-6-pro",thinkingEffort:null};document.querySelector("#prompt-textarea p").textContent="fixture";true')
                            self.assertEqual(native(retained), {'clicked': False, 'reason': 'document'})
                            self.assertEqual(native('window.sends'), 0)
                            # Both alternate directions refuse stale props rather than guessing current fiber.
                            native('owner.alternate={type:owner.type,memoizedProps:{...props,selectedModel:{slug:"gpt-5-6-thinking",thinkingEffort:"standard"}}};true')
                            self.assertFalse(native(committed))
                            native('owner.alternate=null;props.selectedModel={slug:"gpt-6-pro",thinkingEffort:"medium"};true')
                            self.assertFalse(native(committed), 'medium is not maximum')
                            exec(compile(ast.Module(body=[prepare], type_ignores=[]), '<actual-prepare>', 'exec'), env)
                            with self.assertRaisesRegex(RuntimeError, 'WRONG_MODEL'): env['_prepare']('')
                        self.assertEqual(native('window.getters'), 0)
                        self.assertEqual(native('window.sends'), 0)
                        self.assertEqual(native('document.querySelector("button").getAttribute("data-selected-reasoning-effort")'), 'medium')
                proc.stdin.write(json.dumps({'stop':True})+'\n'); proc.stdin.flush(); proc.stdin.close()
                proc.wait(timeout=15)
                self.assertEqual(proc.returncode, 0, proc.stderr.read())
            finally:
                if proc.poll() is None: proc.kill(); proc.wait()

    def test_current_picker_visibility_contract_in_native_dom(self):
        """Actual emitted gate against captured inherited aria visibility.

        Source: pro-model-diagnosis-20261007/PICKER-DOM.json. The inactive
        inert list's unreferenced lock text is not the active Pro row's state.
        This exercises public DOM semantics, not a mocked gate return.
        """
        import os
        scripts = []
        with mock.patch.object(relay, 'bu_json', side_effect=lambda s, **kw: scripts.append(s) or {}):
            relay.send_message('fixture')
        tree = ast.parse(scripts[0])
        gates = [n.args[2] for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == '_bound_js' and len(n.args) > 2
            and 'return full6ProMaximum();})()' in (ast.get_source_segment(scripts[0], n.args[2]) or '')
            and 'retainedOrigin' not in (ast.get_source_segment(scripts[0], n.args[2]) or '')]
        gate = eval(compile(ast.Expression(gates[0]), '<actual-emitted-picker>', 'eval'),
                    {'json': json, 'trigger': "button[aria-label='Select ChatGPT model']"})
        html = ('<button aria-label="Select ChatGPT model" aria-expanded="true" aria-controls="picker">Thinking effort</button>'
            '<div role="menu" id="picker" data-state="open">'
            '<div data-active="true" aria-hidden="false">'
            '<div data-model-picker-view="simple">'
            '<div data-reasoning-slider="true" role="menuitem" aria-label="Power"><span role="slider" aria-hidden="true" tabindex="-1" aria-valuemin="0" aria-valuemax="4" aria-valuenow="4">power</span></div>'
            '<div role="menuitem" data-model-picker-view-toggle="true"><span>6</span> <span>Pro</span>'
            '<span data-maximum="true" style="display:inline-block;width:10px;height:10px"></span></div>'
            '</div></div><div data-active="false" aria-hidden="true" inert>'
            '<span id="unreferenced-lock">Locked, opens access options</span></div></div>')
        cases = [
            ('captured_active', '', True),
            ('hidden_ancestor', 'parent.setAttribute("aria-hidden","true")', False),
            ('inert_ancestor', 'parent.inert=true', False),
            ('inactive_ancestor', 'parent.dataset.active="false"', False),
            ('display_none', 'parent.style.display="none"', False),
            ('visibility_hidden', 'parent.style.visibility="hidden"', False),
            ('row_hidden', 'row.setAttribute("aria-hidden","true")', False),
            ('row_disabled', 'row.setAttribute("aria-disabled","true")', False),
            ('no_maximum', 'row.querySelector("[data-maximum]").remove()', False),
            ('wrong_model', 'row.firstChild.textContent="5"', False),
            ('lower_power', 'slider.setAttribute("aria-valuenow","3")', False),
            ('wrong_scale', 'slider.setAttribute("aria-valuemax","5")', False),
            ('slider_disabled', 'slider.setAttribute("aria-disabled","true")', False),
            ('power_hidden', 'slider.parentElement.setAttribute("aria-hidden","true")', False),
            ('power_inert', 'slider.parentElement.inert=true', False),
            ('power_disabled', 'slider.parentElement.setAttribute("aria-disabled","true")', False),
            ('maximum_hidden', 'row.querySelector("[data-maximum]").style.display="none"', False),
            ('ambiguous_row', 'row.parentElement.append(row.cloneNode(true))', False),
            ('closed_menu', 'document.querySelector("button").setAttribute("aria-expanded","false")', False),
            ('unbound_menu', 'document.querySelector("button").setAttribute("aria-controls","other")', False),
            ('duplicate_active_trigger', 'document.body.append(document.querySelector("button").cloneNode(true))', False),
            ('conflicting_active_trigger', 'const b=document.querySelector("button").cloneNode(true);b.setAttribute("aria-controls","other");document.body.append(b)', False),
            ('duplicate_bound_menu', 'document.body.append(document.querySelector("[role=menu]").cloneNode(true))', False),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = REAL_SUBPROCESS_RUN(['node', '-e', NATIVE_COMPOSER_FIXTURE], text=True,
                input=json.dumps({'expressions': [gate], 'picker': {'html': html, 'gate': gate,
                    'cases': [{'name': name, 'mutate': mutate} for name, mutate, _ in cases]}}),
                capture_output=True, timeout=30, env={**os.environ, 'TMPDIR': tmp})
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)['picker']
        for row, (name, _, expected) in zip(actual, cases, strict=True):
            with self.subTest(name=name):
                self.assertIs(row['accepted'], expected)
                self.assertTrue(row['unchanged'], 'model gate must be passive')

    def test_captured_initial_placeholder_uses_same_exact_preflight_reader(self):
        """PR52 passive empty capture: exact attribute presence and safe values.

        Root values omitted by the privacy filter use empty placeholders; these
        are not claimed as captured values. All reader-relevant values and every
        descendant attribute value were captured. No account/network or Send.
        """
        import os
        import hashlib
        scripts = []
        with mock.patch.object(relay, 'bu_json', side_effect=lambda s, **kw: scripts.append(s) or {}):
            relay.send_message('fixture', retained_tab='fixture', time_origin=12345.5)
            relay._retained_empty_witness('fixture', 12345.5)
        sender, preflight = map(ast.parse, scripts)
        functions = {n.name: n for n in sender.body if isinstance(n, ast.FunctionDef)}
        recorded = []
        ns = {'json': json, 'tab': {'targetId': 'fixture'}, 'fresh': HOME,
              '_fresh': lambda: HOME, '_bound_js': lambda *args: recorded.append(args[2]) or True,
              '_binding_error': lambda *a, **kw: self.fail('expression builder refused')}
        for name in ('_exact', '_prepare'):
            exec(compile(ast.Module(body=[functions[name]], type_ignores=[]), '<generated>', 'exec'), ns)
        ns['_exact'](''); exact = recorded[-1]
        ns['_prepare'](''); prepare = recorded[-1]
        extra = next(ast.literal_eval(n.value) for n in preflight.body
                     if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name)
                     and n.targets[0].id == 'extra')
        html = ('<div contenteditable="true" aria-multiline="" dir="" role="textbox" '
                'spellcheck="true" translate="no" class="ProseMirror" data-composer-markdown="" '
                'aria-label="" data-virtualkeyboard="true" data-relay-id="">'
                '<p data-empty-paragraph="true" data-placeholder="Ask ChatGPT" class="placeholder">'
                '<br class="ProseMirror-trailingBreak"></p></div>')
        attributes = [
            ['contenteditable', 'aria-multiline', 'dir', 'role', 'spellcheck', 'translate',
             'class', 'data-composer-markdown', 'aria-label', 'data-virtualkeyboard', 'data-relay-id'],
            ['data-empty-paragraph', 'data-placeholder', 'class'], ['class']]
        cases = [
            {'name': 'captured_empty', 'mutate': '', 'empty': True},
            {'name': 'unknown_attribute', 'mutate': 'p.setAttribute("data-unknown","")', 'empty': False},
            {'name': 'different_marker', 'mutate': 'p.setAttribute("data-empty-paragraph","false")', 'empty': False},
            {'name': 'different_placeholder', 'mutate': 'p.setAttribute("data-placeholder","Other")', 'empty': False},
            {'name': 'different_class', 'mutate': 'p.className="placeholder extra"', 'empty': False},
            {'name': 'nonempty_decoration', 'mutate': 'p.replaceChildren(document.createTextNode(" "))', 'empty': False},
            {'name': 'multiple_paragraphs', 'mutate': 'e.append(p.cloneNode(true))', 'empty': False},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            result = REAL_SUBPROCESS_RUN(['node', '-e', NATIVE_COMPOSER_FIXTURE], text=True,
                input=json.dumps({'expressions': [exact, prepare, extra], 'initial': {
                    'html': html, 'attributes': attributes, 'cases': cases,
                    'exact': exact, 'preflight': extra, 'prepare': prepare}}),
                capture_output=True, timeout=30, env={**os.environ, 'TMPDIR': tmp})
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)['initial']
        self.assertEqual(actual['attributes'], attributes)
        self.assertEqual(actual['tags'], ['DIV', 'P', 'BR'])
        self.assertEqual(hashlib.sha256(actual['inner'].encode()).hexdigest(),
                         '01ba4719c80b6fe911b091a7c05124b64eeece964e09c058ef8f9805daca546b')
        self.assertEqual(actual['text'], '')
        for row, case in zip(actual['cases'], cases, strict=True):
            with self.subTest(shape=case['name']):
                self.assertIs(row['exact'], case['empty'], 'initial exact-empty gate')
                self.assertIs(row['preflight'], case['empty'], 'read-only preflight must use exact reader')
                self.assertTrue(row['unchanged'], 'readers cannot mutate the composer')
        self.assertIs(actual['prepared'], True, 'captured empty placeholder supports end caret')

    def test_exact_paragraph_content_and_chunk_caret_in_native_dom(self):
        """Real generated JS + native DOM/Input, never a canned CDP result.

        The incident proved reordered tails at chunk boundaries, not which UI
        event moved the caret. Inject that displacement explicitly; do not claim
        to emulate the private editor or to reconstruct its historical events.
        """
        import os
        import shutil
        self.assertIsNotNone(shutil.which('google-chrome'), 'native regression requires Chrome')
        prefix = 'header\n\n\t spaces \u00a0 emoji \U0001f600\n' + 'a' * 5940 + 'FIRST-TAIL'
        suffix = 'SECOND'
        intended = prefix + suffix + '\n\n'
        def emitted(text):
            scripts = []
            with mock.patch.object(relay, 'bu_json', side_effect=lambda s, **kw: scripts.append(s) or {}):
                relay.send_message(text, retained_tab='fixture', time_origin=12345.5)
            return ast.parse(scripts[0])
        tree = emitted(intended)
        emitted_text = next(ast.literal_eval(n.value) for n in tree.body
            if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'TEXT' for t in n.targets))
        self.assertEqual(emitted_text, intended)  # No surrogate splitting in Python chunk boundaries.
        expressions = [n.args[2].value for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == '_bound_js' and len(n.args) > 2
            and isinstance(n.args[2], ast.Constant) and isinstance(n.args[2].value, str)]
        atomic = next(e for e in expressions if e.startswith('(()=>{const retainedOrigin='))
        functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        recorded = []
        ns = {'json': json, 'tab': {'targetId': 'fixture'}, 'fresh': HOME,
              '_fresh': lambda: HOME, '_bound_js': lambda *args: recorded.append(args[2]) or True,
              '_binding_error': lambda *a, **kw: None}
        # Execute the actual emitted expression builders (not substitute JS).
        for name in ('_prepare' if '_prepare' in functions else '_focus', '_exact' if '_exact' in functions else '_len'):
            exec(compile(ast.Module(body=[functions[name]], type_ignores=[]), '<generated>', 'exec'), ns)
        if '_prepare' in ns: ns['_prepare'](prefix)
        else: ns['_focus']()
        prepare = recorded[-1]
        if '_exact' in ns: ns['_exact'](prefix + suffix)
        else: ns['_len']()
        check = recorded[-1]
        cases = [{'name': 'correct', 'text': intended, 'expression': atomic},
                 {'name': 'reordered', 'text': intended[:-51] + intended[-49:] + intended[-51:-49], 'expression': atomic}]
        for name, changed in [('tab', intended.replace('\t', ' ')),
                              ('nbsp', intended.replace('\u00a0', ' ')),
                              ('blank_line', intended.replace('\n\n', '\n', 1)),
                              ('trailing_lf', intended[:-1]),
                              ('spaces', intended.replace(' spaces ', '  spaces ', 1))]:
            cases.append({'name': name, 'text': changed, 'expression': atomic})
        cases.append({'name': 'unknown_structure', 'text': intended, 'expression': atomic,
                      'mutate': 'const p=document.querySelector("p");const s=document.createElement("span");s.append(...p.childNodes);p.append(s);'})
        # The observed decoration is permitted only on the sole trailing-BR P.
        cases.extend([
            {'name': 'unknown_empty_attribute', 'text': intended, 'expression': atomic,
             'mutate': 'document.querySelector("p[data-empty-paragraph]").setAttribute("data-unknown","");'},
            {'name': 'nonempty_marker_misuse', 'text': intended, 'expression': atomic,
             'mutate': 'document.querySelector("p").setAttribute("data-empty-paragraph","");'},
        ])
        # A dedicated about:blank profile, blocked requests, pipe-only CDP.
        node = NATIVE_COMPOSER_FIXTURE
        with tempfile.TemporaryDirectory() as tmp:
            result = REAL_SUBPROCESS_RUN(['node', '-e', node], text=True,
                input=json.dumps({'expressions': expressions + [prepare, check], 'cases': cases,
                                  'caret': {'prefix': prefix, 'suffix': suffix, 'displace': 49,
                                            'prepare': prepare, 'check': check}}),
                capture_output=True, timeout=30, env={**os.environ, 'TMPDIR': tmp})
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        self.assertEqual(actual['caret']['actual'], prefix + suffix, 'chunk insertion must restore the exact end caret')
        self.assertIs(actual['caret']['checked'], True, 'prefix equality must be exact, not a length')
        for row in actual['cases']:
            self.assertEqual(row['result']['clicked'], row['name'] == 'correct', row['name'])
            self.assertEqual(row['clicks'], int(row['name'] == 'correct'), row['name'])


NATIVE_COMPOSER_FIXTURE = r'''
const fs=require('fs'),cp=require('child_process'),os=require('os'),path=require('path');
const f=JSON.parse(fs.readFileSync(0,'utf8'));
const dir=fs.mkdtempSync(path.join(process.env.TMPDIR,'pro-composer-native-'));
const chrome=cp.spawn('google-chrome',['--headless=new','--no-sandbox','--disable-gpu','--disable-background-networking','--disable-component-update','--disable-sync','--no-first-run','--no-default-browser-check','--disable-extensions','--host-resolver-rules=MAP * ~NOTFOUND','--remote-debugging-pipe','--user-data-dir='+dir,'about:blank'],{stdio:['ignore','ignore','ignore','pipe','pipe']});
let next=0,buf='',pending=new Map();
chrome.stdio[4].on('data',b=>{buf+=b;let pos;while((pos=buf.indexOf('\0'))>=0){const msg=JSON.parse(buf.slice(0,pos));buf=buf.slice(pos+1);if(msg.id&&pending.has(msg.id)){const [yes,no]=pending.get(msg.id);pending.delete(msg.id);msg.error?no(new Error(msg.error.message)):yes(msg.result);}}});
function cdp(method,params={},sessionId){return new Promise((yes,no)=>{const id=++next;pending.set(id,[yes,no]);chrome.stdio[3].write(JSON.stringify({id,method,params,sessionId})+'\0');});}
let sid;
async function js(expression){const r=await cdp('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true},sid);if(r.exceptionDetails)throw new Error('FIXTURE_JS_EXCEPTION');return r.result.value;}
(async()=>{try{
const target=(await cdp('Target.createTarget',{url:'about:blank'})).targetId;
sid=(await cdp('Target.attachToTarget',{targetId:target,flatten:true})).sessionId;
await cdp('Network.enable',{},sid);await cdp('Network.setBlockedURLs',{urls:['*']},sid);
await js(`document.body.innerHTML='<div data-composer-markdown contenteditable="true" class="ProseMirror" style="white-space:break-spaces"></div><button data-testid="send-button">Send</button><button aria-label="Select ChatGPT model" data-selected-reasoning-effort="maximum" data-codex-intelligence-trigger="true" data-composer-navigation-target="reasoning">6 Pro</button>';window.clicks=0;document.querySelector('[data-testid=send-button]').onclick=()=>window.clicks++;window.render=(text)=>{const e=document.querySelector('[contenteditable]');e.replaceChildren(...text.split('\\n').map(t=>{const p=document.createElement('p');if(t)p.append(document.createTextNode(t));else{p.setAttribute('data-empty-paragraph','');const b=document.createElement('br');b.className='ProseMirror-trailingBreak';p.append(b);}return p;}));};`);
const output={};
await js(__MODEL_OWNER_FIXTURE__);
for(const expr of f.expressions)new Function('return ('+expr+')');
if(f.picker){
const i=f.picker;output.picker=[];
for(const c of i.cases){await js('document.body.innerHTML='+JSON.stringify(i.html)+';(()=>{const row=document.querySelector("[data-model-picker-view-toggle]"),parent=document.querySelector("[data-active=true]"),slider=document.querySelector("[role=slider]");'+c.mutate+';})()');const before=await js('document.body.innerHTML');output.picker.push({name:c.name,accepted:await js(i.gate),unchanged:before===await js('document.body.innerHTML')});}
process.stdout.write(JSON.stringify(output));return;
}
if(f.initial){
const i=f.initial;
await js('document.head.innerHTML="<style>[data-composer-markdown]{white-space:break-spaces}</style>";window.initial=()=>{document.body.innerHTML='+JSON.stringify(i.html)+';};window.initial();');
output.initial=await js('(()=>{const e=document.querySelector("[contenteditable]");const all=[e,...e.querySelectorAll("*")];return {attributes:all.map(n=>n.getAttributeNames()),tags:all.map(n=>n.tagName),inner:e.innerText,text:e.textContent,cases:[]};})()');
for(const c of i.cases){await js('window.initial();(()=>{const e=document.querySelector("[contenteditable]"),p=e.firstChild;'+c.mutate+';})()');const before=await js('document.body.innerHTML');output.initial.cases.push({name:c.name,exact:await js(i.exact),preflight:(await js(i.preflight)).composer_empty,unchanged:before===await js('document.body.innerHTML')});}
await js('window.initial()');output.initial.prepared=await js(i.prepare);
process.stdout.write(JSON.stringify(output));return;
}
output.cases=[];for(const c of f.cases){await js('window.render('+JSON.stringify(c.text)+');window.clicks=0;');if(c.mutate)await js(c.mutate);const expr=c.expression.replace('const retainedOrigin=12345.5;', 'const retainedOrigin=performance.timeOrigin;');output.cases.push({name:c.name,result:await js(expr),clicks:await js('window.clicks')});}
const c=f.caret;await js('window.render('+JSON.stringify(c.prefix)+');const e=document.querySelector("[contenteditable]");e.focus();const n=e.lastChild.firstChild;getSelection().collapse(n,n.length-'+c.displace+');');output.caret={prepared:await js(c.prepare)};await cdp('Input.insertText',{text:c.suffix},sid);output.caret.actual=await js('[...document.querySelector("[contenteditable]").children].map(p=>p.textContent).join("\\n")');output.caret.checked=await js(c.check);
process.stdout.write(JSON.stringify(output));
}finally{await cdp('Browser.close').catch(()=>{});}})().catch(e=>{process.stderr.write(e.message);process.exitCode=1;}).finally(()=>{chrome.on('exit',()=>fs.rmSync(dir,{recursive:true,force:true}));setTimeout(()=>{chrome.kill('SIGKILL');},2000).unref();});
'''.replace('__MODEL_OWNER_FIXTURE__', json.dumps(MODEL_OWNER_FIXTURE + '''
const trigger=document.querySelector('button[aria-label="Select ChatGPT model"]');
trigger.setAttribute('aria-expanded','false');
attachModelOwner(trigger,{slug:'gpt-6-pro',thinkingEffort:null});
'''))


if __name__ == "__main__":
    unittest.main()
