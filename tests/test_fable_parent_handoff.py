"""EXPLICITLY SYNTHETIC operator intent; offline proof, never a real authorization.

Reuse the existing bridge fixtures and canonical guard. All network, credentials,
model execution and host-budget edits are denied or replaced at their I/O seams.
"""
import hashlib
import importlib.util
import json
import socket
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import test_fable_bridge_quota_resume as quota

SIGNER, bridge = quota.SIGNER, quota.bridge

ROOT = Path(__file__).resolve().parents[1]
REQUEST = "1555525422467387402"
ISSUE = "bridge-issue-7444094b1806654ef6b225f19e8f3f58"
DEFERRED = "1555525443363278849"
QUESTION = "1555539647889805455"
INTENT = "1555539647889805456"  # Synthetic ID, not an observed operator message.
DECISIONS = "1524254375453786294"
SUBJECT = "glp-clinic-scout:F06:lp-quiz-issued-version:20261002"


def denied(*args, **kwargs):
    raise AssertionError("Real network/credential/model/host effect forbidden")


class ParentHandoffTests(unittest.TestCase):
    def setUp(self):
        # Reuse only setup, not the predecessor test matrix.
        quota.QuotaResumeTests.setUp(self)
        for target in ("socket.socket.connect", "requests.Session.request", "subprocess.Popen"):
            p = patch(target, side_effect=denied)
            p.start()
            self.addCleanup(p.stop)
        self.artifact.rename(self.artifact_root / "design-contract.json")
        self.artifact = self.artifact_root / "design-contract.json"
        self.guard = bridge._authority_guard()
        for name, value in (("BRIDGE_ISSUE_ROOT", self.issue_dir),
                            ("REVIEW_ARTIFACT_ROOT", self.artifact_root),
                            ("_configured_fable_author_id", Mock(return_value=SIGNER)),
                            ("_discord_bot_token", Mock(side_effect=denied)),
                            ("_fetch_discord_message", Mock(side_effect=self.fetch))):
            p = patch.object(self.guard, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.issue = self.guard.build_issue_record(subject_id=SUBJECT, tuple_sha256=self.digest,
                                                   review_artifact_path=self.artifact)
        old_id = self.issue["issue_id"]
        self.issue.update(issue_id=ISSUE, status="BOUND", request_message_id=REQUEST,
                          request_timestamp="2026-10-02T10:00:00+00:00",
                          bound_at_utc="2026-10-02T10:00:01+00:00", request_edited_timestamp=None)
        self.issue["request_content"] = self.issue["request_content"].replace(old_id, ISSUE)
        self.issue["request_content_sha256"] = self.guard.sha256_text(self.issue["request_content"])
        self.issue["record_sha256"] = self.guard._record_hash(self.issue)
        (self.issue_dir / (ISSUE + ".json")).write_text(json.dumps(self.issue))
        self.message.update(id=REQUEST, channel_id=bridge.CHANNEL,
                            timestamp=self.issue["request_timestamp"], content=self.issue["request_content"])
        body = bridge.review_action("review_request", self.issue["request_content"],
                                   source_message_id=REQUEST)[1]
        self.refusal.update(id=DEFERRED, channel_id=bridge.CHANNEL, type=19,
                            content=body, author={"id": SIGNER, "bot": True},
                            message_reference={"message_id": REQUEST, "channel_id": bridge.CHANNEL})
        self.receipt = dict(source_message_id=REQUEST, kind="review_request", mode="deferred",
                            attempts=[], reviewer_identity="none (top_stakes_deferred)", posting="posted",
                            posted_message_id=DEFERRED, reply_chars=len(body),
                            reply_sha256=hashlib.sha256(body.encode()).hexdigest())
        self.initial = dict(last_id="1555579687424499905", unrelated_cursor="preserve",
                            reviewer_receipts={REQUEST: self.receipt, "other": {"posting": "posted"}})
        self.state_path.write_text(json.dumps(self.initial))
        self.question = dict(id=QUESTION, channel_id=DECISIONS, type=0,
            author={"id": bridge.KAEL_BOT_ID, "bot": True}, edited_timestamp=None,
            timestamp="2026-10-02T11:19:12+00:00", content="EXPLICITLY SYNTHETIC existing exact question")
        self.intent = dict(id=INTENT, channel_id=DECISIONS, type=19,
            author={"id": bridge.OPERATOR_ID, "bot": False}, edited_timestamp=None,
            timestamp="2026-10-02T11:20:00+00:00", attachments=[],
            content=f"HANDOFF FABLE REVIEW ONLY {REQUEST} {ISSUE} {self.digest}",
            message_reference={"message_id": QUESTION, "channel_id": DECISIONS})
        self.module = None
        self.host_events = []
        self.inferences.side_effect = self.infer
        p = patch.object(bridge, "api", side_effect=self.transport)
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(bridge, "_authority_guard", return_value=self.guard)
        p.start()
        self.addCleanup(p.stop)

    # Methods expected by the reused setup.
    write_issue = quota.QuotaResumeTests.write_issue
    save = quota.QuotaResumeTests.save
    capacity = quota.QuotaResumeTests.capacity
    api = quota.QuotaResumeTests.api

    def fetch(self, channel, message_id):
        self.assertEqual(channel, DECISIONS)
        return {QUESTION: self.question, INTENT: self.intent}[message_id]

    def transport(self, method, path, body=None):
        if method == "GET" and path.endswith("/" + REQUEST):
            return self.message
        if method == "GET" and path.endswith("/" + DEFERRED):
            return self.refusal
        if method == "GET" and "?before=" in path:
            return [*self.extra, self.refusal, self.message]
        if method == "POST":
            self.posts.append(body)
            self.posted = dict(id="1555580000000000000", channel_id=bridge.CHANNEL,
                author={"id": SIGNER, "bot": True}, edited_timestamp=None,
                message_reference=body["message_reference"], content=body["content"])
            return self.posted
        if method == "GET" and path.endswith("/1555580000000000000"):
            return self.posted
        raise AssertionError((method, path))

    @contextmanager
    def host(self):
        self.host_events.append("reserved")
        try:
            yield
        finally:
            self.host_events.append("released")

    def infer(self, command, **kwargs):
        self.assertEqual(self.host_events, ["reserved"])
        claim = bridge.load_state()["reviewer_receipts"][REQUEST]["parent_handoff"]
        self.assertEqual(claim["status"], "invocation_reserved")
        self.assertEqual(command[command.index("--resume") + 1], "20260922_214459_3be4d1")
        self.assertEqual(command[command.index("--toolsets") + 1], "fable-review-readonly")
        self.assertEqual(command[command.index("-m") + 1], "claude-fable-5-1")
        self.assertEqual(command[command.index("--reasoning") + 1], "xhigh")
        self.assertEqual(command[command.index("--provider") + 1], "claude-subscription-directsdk-experimental")
        prompt = Path(command[command.index("--query-file") + 1]).read_text()
        self.assertIn(self.issue["request_content"], prompt)
        return SimpleNamespace(returncode=0, stdout="TRUE-BRIDGE FABLE — REJECT\nSynthetic independent inspection", stderr="")

    def dispatch(self):
        self.assertTrue((ROOT / "scripts/fable_parent_handoff.py").is_file(),
                        "missing maintained operator-authenticated parent handoff")
        if self.module is None:
            spec = importlib.util.spec_from_file_location("synthetic_parent_handoff", ROOT / "scripts/fable_parent_handoff.py")
            self.module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.module)
        # Only the bound artifact/question hashes are synthetic, never live claims.
        with patch.object(self.module, "TUPLE_SHA256", self.digest), \
             patch.object(self.module, "QUESTION_SHA256", hashlib.sha256(self.question["content"].encode()).hexdigest()), \
             patch.object(self.module, "_host_admission", self.host), \
             patch.object(bridge, "SESSION_ID", "171df31c-619c-4432-8a32-1767a9c8edc2"), \
             patch.object(bridge, "_native_session_id", return_value="20260922_214459_3be4d1"):
            return self.module.dispatch(bridge, REQUEST, ISSUE, self.digest, INTENT)

    def test_synthetic_intent_routes_once_to_original_preserving_deferral_and_cursor(self):
        result = self.dispatch()
        self.assertEqual(result["status"], "posted")
        self.inferences.assert_called_once()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["content"], "TRUE-BRIDGE FABLE — REJECT\nSynthetic independent inspection")
        self.assertEqual(self.posts[0]["message_reference"]["message_id"], REQUEST)
        state = bridge.load_state()
        claim = state["reviewer_receipts"][REQUEST].pop("parent_handoff")
        self.assertEqual(state, self.initial)
        self.assertEqual(claim["operator_intent_message_id"], INTENT)
        self.assertEqual(self.host_events, ["reserved", "released"])
        with self.assertRaisesRegex(RuntimeError, "receipt_not_eligible"):
            self.dispatch()
        self.inferences.assert_called_once()


    def test_invalid_or_unbound_operator_intent_has_no_work(self):
        original = json.loads(json.dumps(self.intent))
        cases = [
            {"author": {"id": bridge.KAEL_BOT_ID, "bot": True}},
            {"author": {"id": bridge.OPERATOR_ID, "bot": True}},
            {"webhook_id": "1555539647889805499"},
            {"edited_timestamp": "2026-10-02T11:21:00+00:00"},
            {"message_reference": {"message_id": REQUEST, "channel_id": DECISIONS}},
            {"content": "yes"},
            {"content": "APPROVE DEPLOY " + ISSUE + " " + self.digest},
            {"content": original["content"].replace(self.digest, "0" * 64)},
        ]
        for mutation in cases:
            with self.subTest(mutation=mutation):
                self.intent = dict(original, **mutation)
                with self.assertRaises((RuntimeError, self.guard.BridgeAuthorityError)):
                    self.dispatch()
                self.assertEqual(bridge.load_state(), self.initial)
        self.inferences.assert_not_called()
        bridge.refresh_included_capacity.assert_not_called()
        self.assertFalse(self.posts)
        self.assertFalse(self.host_events)

    def test_peer_reply_refuses_before_work(self):
        self.extra.append(dict(id="1555580000000000000", author={"id": "independent-peer"},
                               message_reference={"message_id": REQUEST}))
        with self.assertRaisesRegex(RuntimeError, "additional_reviewer_reply_present"):
            self.dispatch()
        bridge.refresh_included_capacity.assert_not_called()
        self.inferences.assert_not_called()
        self.assertFalse(self.posts)

    def test_uncertain_claims_and_posts_are_terminal(self):
        for status in ("reserved", "invocation_reserved", "post_failed_or_ambiguous", "posted_unverified"):
            with self.subTest(status=status):
                self.receipt["parent_handoff"] = {"status": status}
                self.state_path.write_text(json.dumps(self.initial))
                with self.assertRaisesRegex(RuntimeError, "receipt_not_eligible"):
                    self.dispatch()
        self.receipt.pop("parent_handoff")
        self.state_path.write_text(json.dumps(self.initial))
        actual = self.transport
        def ambiguous(method, path, body=None):
            if method == "POST":
                raise TimeoutError("EXPLICITLY SYNTHETIC uncertain post")
            return actual(method, path, body)
        with patch.object(bridge, "api", side_effect=ambiguous):
            with self.assertRaisesRegex(TimeoutError, "uncertain post"):
                self.dispatch()
        self.inferences.assert_called_once()
        self.assertEqual(bridge.load_state()["reviewer_receipts"][REQUEST]["parent_handoff"]["status"],
                         "post_failed_or_ambiguous")
        with self.assertRaisesRegex(RuntimeError, "receipt_not_eligible"):
            self.dispatch()
        self.inferences.assert_called_once()

    def test_uncertain_inference_never_replays(self):
        self.inferences.side_effect = TimeoutError("EXPLICITLY SYNTHETIC uncertain inference")
        with self.assertRaisesRegex(RuntimeError, "no_safe_inference"):
            self.dispatch()
        with self.assertRaisesRegex(RuntimeError, "receipt_not_eligible"):
            self.dispatch()
        self.inferences.assert_called_once()
        self.assertFalse(self.posts)
        self.assertEqual(self.host_events, ["reserved", "released"])

    def test_verbatim_whitespace_output_uses_existing_attachment_transport(self):
        raw = "  EXPLICITLY SYNTHETIC reviewer stdout 🚀\n"
        infer = self.infer
        self.inferences.side_effect = lambda *args, **kwargs: (infer(*args, **kwargs),
                SimpleNamespace(returncode=0, stdout=raw, stderr=""))[1]
        def multipart(method, url, **kwargs):
            self.assertEqual(method, "POST")
            self.assertEqual(kwargs["files"]["files[0]"][1], raw.encode())
            payload = json.loads(kwargs["data"]["payload_json"])
            posted = self.transport("POST", "", payload)
            self.posted["attachments"] = [{"filename": "reviewer-response.txt",
                "size": len(raw.encode()), "url": "https://cdn.discordapp.com/attachments/synthetic"}]
            return SimpleNamespace(status_code=200, json=lambda: posted)
        with patch.object(bridge, "HTTP", SimpleNamespace(request=multipart)), \
                patch.object(bridge.requests, "get", return_value=SimpleNamespace(status_code=200, content=raw.encode(), raise_for_status=lambda: None)):
            self.dispatch()
        self.inferences.assert_called_once()
        claim = bridge.load_state()["reviewer_receipts"][REQUEST]["parent_handoff"]
        self.assertEqual(claim["output_sha256"], hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual(claim["attachment_verification"]["attachment_sha256"], claim["output_sha256"])
        self.assertEqual(self.posts[0]["message_reference"]["message_id"], REQUEST)

    def test_peer_reply_during_inference_withholds_post_and_replay(self):
        infer = self.infer
        def competing(*args, **kwargs):
            result = infer(*args, **kwargs)
            self.extra.append(dict(id="1555580000000000000", author={"id": "peer"},
                                   message_reference={"message_id": REQUEST}))
            return result
        self.inferences.side_effect = competing
        with self.assertRaisesRegex(RuntimeError, "additional_reviewer_reply_present"):
            self.dispatch()
        self.assertFalse(self.posts)
        self.assertEqual(bridge.load_state()["reviewer_receipts"][REQUEST]["parent_handoff"]["status"],
                         "reply_withheld_reconciliation")
        with self.assertRaisesRegex(RuntimeError, "receipt_not_eligible"):
            self.dispatch()
        self.inferences.assert_called_once()

    def test_native_entry_is_readonly_and_disables_reloaded_fallbacks(self):
        import runpy
        from types import ModuleType
        package = ModuleType("hermes_cli")
        package.__path__ = []
        config = ModuleType("hermes_cli.config")
        ambient = {"fallback_providers": [{"provider": "paid"}], "fallback_model": {"model": "wrong"}}
        original = Mock(return_value=ambient)
        config.load_config = original
        package.config = config
        local = json.loads(json.dumps(ambient))
        create = Mock()
        def native_main():
            self.assertEqual(config.load_config(), {"fallback_providers": []})
            self.assertEqual(local, {"fallback_providers": []})
        with patch.dict(sys.modules, {"hermes_cli": package, "hermes_cli.config": config,
                "hermes_cli.main": SimpleNamespace(main=native_main),
                "cli": SimpleNamespace(CLI_CONFIG=local),
                "toolsets": SimpleNamespace(create_custom_toolset=create)}), patch.object(sys, "argv", [
                "fable_bridge.py", "--native-reviewer", "chat", "--oneshot", "--provider",
                "claude-subscription-directsdk-experimental"]):
            runpy.run_path(str(ROOT / "scripts/fable_bridge.py"), run_name="__main__")
        self.assertIs(config.load_config, original)
        self.assertIn("fallback_model", ambient)
        self.assertEqual(create.call_args.args[2],
                         ["read_file", "search_files", "web_search", "web_extract", "vision_analyze"])

    def test_installed_host_admission_api_is_reserved_and_released(self):
        self.dispatch()
        released = Mock()
        reserve = Mock(return_value=([SimpleNamespace(release=released)], 2, 8))
        admission = SimpleNamespace(admission_problem=Mock(return_value=None),
                                    try_reserve_host_children=reserve)
        with patch.dict(sys.modules, {"tools": SimpleNamespace(delegation_admission=admission)}):
            with self.module._host_admission():
                released.assert_not_called()
            released.assert_called_once()
            reserve.assert_called_once_with(1)
            admission.admission_problem.assert_called_once_with()
            admission.admission_problem.return_value = "EXPLICITLY SYNTHETIC pressure"
            with self.assertRaisesRegex(RuntimeError, "host_pressure"):
                with self.module._host_admission():
                    denied()
            reserve.assert_called_once()

    def test_changed_artifact_and_unavailable_capacity_have_no_invocation(self):
        original = self.artifact.read_bytes()
        self.artifact.write_bytes(original + b" changed")
        with self.assertRaises(self.guard.BridgeAuthorityError):
            self.dispatch()
        self.artifact.write_bytes(original)
        with patch.object(bridge, "_included_quota_available", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "fresh_positive_included_capacity"):
                self.dispatch()
        self.inferences.assert_not_called()
        self.assertFalse(self.host_events)
        self.assertFalse(self.posts)
        self.assertEqual(bridge.load_state(), self.initial)

    def test_automatic_top_stakes_still_defers_without_intent(self):
        with patch.object(bridge, "ask_fable", side_effect=denied):
            mode, reply = bridge.review_action("review_request", self.issue["request_content"],
                                               source_message_id=REQUEST)
        self.assertEqual(mode, "deferred")
        self.assertEqual(reply, self.refusal["content"])
        self.inferences.assert_not_called()
        self.assertFalse(self.posts)


if __name__ == "__main__":
    unittest.main()
