"""Offline binding regressions: execute the real browser-use scripts against a strict fake CDP session."""
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
            self.pages[tid] = {"url": url, "text": text, "composer": self.blank_paragraph, "messages": bool(text), "stop": False}
            self.created.append((tid, requested))
            return {"targetId": tid}  # deliberately does not change self.active
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": params.get('targetId', self.browser_target)}}
        addressed = self.sessions[session_id] if session_id else self.active
        page = self.pages[addressed]
        if method == "Input.insertText":
            self.inserts.append((addressed, params["text"]))
            page["composer"] += params["text"]
            return {}
        if method == 'Input.dispatchMouseEvent':
            if self.medium_trigger and self.slider_works and params['type'] == 'mouseReleased':
                page['picker_power'] = 4
            return {}
        if method == "Input.dispatchKeyEvent":
            if params["key"] == "Backspace" and params["type"] == "keyDown":
                page["composer"] = ""
            return {}
        raise AssertionError(f"unexpected CDP command: {method}")

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
        if 'return b?b.innerText.trim():null' in expression:
            if self.clock < self.app_ready_at:
                return None
            return 'Medium' if self.medium_trigger else ('Pro' if self.pro_only_trigger else None)
        if 'r.right-13' in expression:
            return {'x': 1100, 'y': 470} if self.medium_trigger else None
        if 'data-model-picker-view-toggle' in expression and (self.pro_only_trigger or self.medium_trigger):
            return self.selected_model == '6 Pro' and (not self.medium_trigger or page.get('picker_power') == 4)
        if 'Select ChatGPT model' in expression and (self.pro_only_trigger or self.medium_trigger):
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
            if expression.startswith('(()=>{const b=') and 'b.click();return true;' in expression:
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


if __name__ == "__main__":
    unittest.main()
