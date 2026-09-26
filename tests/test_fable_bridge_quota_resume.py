"""Offline exact-request resume regressions. No Discord or reviewer network calls."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bridge_quota_resume", ROOT / "scripts/fable_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
SOURCE = "1553266002706309203"
REFUSAL = "1553266023426293760"
ISSUE = "bridge-issue-5ea105281069a60941cc8c1c0eaadccb"
SIGNER = "1529571092371476591"
REFUSAL_BODY = "(Included Claude quota unavailable or unverified; no authority decision was issued.)\n[reviewer: fable-xhigh]"


class QuotaResumeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.artifact_root = self.root / "ops"
        self.artifact_root.mkdir()
        self.artifact = self.artifact_root / "manifest.json"
        self.artifact.write_text('{"artifact":"fixture"}\n')
        self.digest = hashlib.sha256(self.artifact.read_bytes()).hexdigest()
        self.issue_dir = self.root / "issues"
        self.issue_dir.mkdir()
        self.content = (f"<@{SIGNER}> TRUE-BRIDGE FABLE REVIEW REQUEST\nissue_id: {ISSUE}\n"
                        f"tuple_sha256: {self.digest}\nreview_artifact_sha256: {self.digest}\n"
                        "review_artifact_path: " + str(self.artifact))
        self.issue = dict(schema="boring_ai_bridge_request_issue_v2", status="BOUND", issue_id=ISSUE,
                          subject_id="glp:GLP-42:offline-fixture",
                          request_message_id=SOURCE, channel_id=bridge.CHANNEL,
                          request_author_id=bridge.KAEL_BOT_ID, tuple_sha256=self.digest,
                          review_artifact_sha256=self.digest, review_artifact_path=str(self.artifact),
                          request_content=self.content,
                          request_content_sha256=hashlib.sha256(self.content.encode()).hexdigest(),
                          request_timestamp="2026-09-26T04:44:33.332000+00:00")
        self.write_issue()
        self.state_path = self.root / "state.json"
        self.receipt = dict(source_message_id=SOURCE, kind="review_request", mode="model", posting="posted",
                            posted_message_id=REFUSAL, reply_chars=len(REFUSAL_BODY),
                            reply_sha256=hashlib.sha256(REFUSAL_BODY.encode()).hexdigest(),
                            reviewer_identity="fable-xhigh", backend="fable", model="claude-fable-5-1",
                            effort="xhigh", attempts=[dict(backend="fable", model="claude-fable-5-1",
                                                         effort="xhigh", invoked=False,
                                                         status="included_capacity_unverified_or_depleted")])
        self.save()
        self.message = dict(id=SOURCE, type=0, timestamp=self.issue["request_timestamp"],
                            author={"id": bridge.KAEL_BOT_ID, "bot": True},
                            mentions=[{"id": SIGNER}], content=self.content, edited_timestamp=None)
        self.refusal = dict(id=REFUSAL, content=REFUSAL_BODY, author={"id": SIGNER},
                            edited_timestamp=None, message_reference={"message_id": SOURCE})
        self.extra = []
        self.posts = []
        self.inferences = Mock(return_value=SimpleNamespace(returncode=0,
            stdout="TRUE-BRIDGE FABLE — REJECT\nInspected fixture; withheld acceptance.", stderr=""))
        for name, value in (("STATE", self.state_path), ("ISSUE_DIR", self.issue_dir),
                            ("ARTIFACT_ROOT", self.artifact_root), ("SELF_ID", SIGNER),
                            ("SESSION_ID", "00000000-0000-4000-8000-000000000001"),
                            ("expected_author_id", Mock(return_value=SIGNER)),
                            ("_native_session_id", Mock(return_value="20260922_120000_abcdef")),
                            ("_resolve_model", Mock(return_value=("claude-fable-5-1", "xhigh"))),
                            ("_routing_policy", Mock(return_value={"threshold_percent": 100})),
                            ("refresh_included_capacity", Mock(side_effect=self.capacity)),
                            ("api", Mock(side_effect=self.api)), ("log", Mock())):
            guard = patch.object(bridge, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.object(bridge.subprocess, "run", self.inferences)
        guard.start()
        self.addCleanup(guard.stop)
        self.five = 50
        self.week = 60

    def write_issue(self):
        body = {key: value for key, value in self.issue.items() if key != "record_sha256"}
        self.issue["record_sha256"] = hashlib.sha256(json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        (self.issue_dir / f"{ISSUE}.json").write_text(json.dumps(self.issue))

    def save(self):
        self.state_path.write_text(json.dumps({"last_id": REFUSAL,
                                              "reviewer_receipts": {SOURCE: self.receipt}}))

    def capacity(self):
        return {"collected_at_utc": datetime.now(timezone.utc).isoformat(), "records": [
            dict(provider="anthropic", freshness="live", capacity_status="available",
                 session_remaining_pct=self.five, weekly_remaining_pct=self.week)]}

    def api(self, method, path, body=None):
        if method == "GET" and path.endswith("/" + SOURCE):
            return self.message
        if method == "GET" and path.endswith("/" + REFUSAL):
            return self.refusal
        if method == "GET" and "?before=" in path:
            cursor = int(path.split("?before=")[1].split("&")[0])
            return [*self.extra, self.refusal, self.message] if cursor > int(REFUSAL) else []
        if method == "POST":
            self.posts.append(body)
            return {"id": "1553266999999999999"}
        raise AssertionError((method, path))

    def run_resume(self):
        return bridge.resume_preinvocation_quota(SOURCE, ISSUE, self.digest)

    def test_exact_preinvocation_resume_claim_precedes_inference_and_preserves_refusal(self):
        def inference(*args, **kwargs):
            state = bridge.load_state()
            original = state["reviewer_receipts"][SOURCE]
            self.assertEqual(original["reply_sha256"], self.receipt["reply_sha256"])
            self.assertEqual(original["posted_message_id"], REFUSAL)
            self.assertEqual(original["resume"]["status"], "invocation_reserved")
            self.assertEqual(original["resume"]["attempts"][0]["invoked"], True)
            return SimpleNamespace(returncode=0, stdout="TRUE-BRIDGE FABLE — REJECT\nInspected fixture", stderr="")
        self.inferences.side_effect = inference
        result = self.run_resume()
        self.assertEqual(result["status"], "posted")
        self.assertEqual(self.inferences.call_count, 1)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["nonce"], SOURCE + "-r1")
        self.assertEqual(self.posts[0]["message_reference"]["message_id"], SOURCE)
        self.assertEqual(bridge.load_state()["last_id"], REFUSAL)
        with self.assertRaisesRegex(RuntimeError, "resume_receipt_not_eligible"):
            self.run_resume()
        self.assertEqual(self.inferences.call_count, 1)
        self.assertEqual(len(self.posts), 1)

    def test_competing_decision_during_capacity_refresh_blocks_inference_and_post(self):
        decision = dict(id=str(int(REFUSAL) + 1), author={"id": SIGNER, "bot": True},
                        message_reference={"message_id": SOURCE},
                        content="TRUE-BRIDGE FABLE — ACCEPT\nOffline competing decision")
        def refresh():
            self.extra.append(decision)
            return self.capacity()
        bridge.refresh_included_capacity.side_effect = refresh
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            with self.assertRaisesRegex(RuntimeError, "additional_reviewer_reply_present"):
                self.run_resume()
        self.inferences.assert_not_called()
        self.assertFalse(self.posts)
        self.assertNotIn("resume", bridge.load_state()["reviewer_receipts"][SOURCE])
        self.assertEqual(bridge.load_state()["last_id"], REFUSAL)

    def test_competing_decision_during_inference_withholds_post_and_replay(self):
        decision = dict(id=str(int(REFUSAL) + 1), author={"id": SIGNER, "bot": True},
                        message_reference={"message_id": SOURCE},
                        content="TRUE-BRIDGE FABLE — ACCEPT\nOffline competing decision")
        def inference(*args, **kwargs):
            self.extra.append(decision)
            return SimpleNamespace(returncode=0,
                stdout="TRUE-BRIDGE FABLE — REJECT\nOffline resumed review", stderr="")
        self.inferences.side_effect = inference
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            with self.assertRaisesRegex(RuntimeError, "additional_reviewer_reply_present"):
                self.run_resume()
            with self.assertRaisesRegex(RuntimeError, "resume_receipt_not_eligible"):
                self.run_resume()
        self.assertEqual(self.inferences.call_count, 1)
        self.assertFalse(self.posts)
        state = bridge.load_state()
        original = state["reviewer_receipts"][SOURCE]
        self.assertEqual(original["posted_message_id"], REFUSAL)
        self.assertEqual(original["reply_sha256"], hashlib.sha256(REFUSAL_BODY.encode()).hexdigest())
        self.assertEqual(state["last_id"], REFUSAL)
        self.assertEqual(original["resume"]["status"], "reply_withheld_reconciliation")
        self.assertEqual(original["resume"]["posting"], "withheld")
        self.assertTrue(original["resume"]["attempts"][0]["invoked"])

    def test_duplicate_or_other_refusal_blocks_before_capacity_or_inference(self):
        self.extra.append(dict(id="1553266023426293761", author={"id": SIGNER},
                               message_reference={"message_id": SOURCE},
                               content="TRUE-BRIDGE FABLE — ACCEPT"))
        with self.assertRaisesRegex(RuntimeError, "additional_reviewer_reply_present"):
            self.run_resume()
        bridge.refresh_included_capacity.assert_not_called()
        self.inferences.assert_not_called()
        self.assertFalse(self.posts)

    def test_backward_pagination_detects_earlier_duplicate_reply(self):
        newer = [{"id": str(int(REFUSAL) + 100 + i), "author": {"id": "bystander"}}
                 for i in range(100)]
        extra = {"id": str(int(REFUSAL) + 1), "author": {"id": SIGNER},
                 "message_reference": {"message_id": SOURCE}}
        calls = []
        def pages(method, path, body=None):
            if "?before=" in path:
                cursor = int(path.split("?before=")[1].split("&")[0])
                calls.append(cursor)
                return newer if len(calls) == 1 else [extra, self.refusal, self.message]
            return self.api(method, path, body)
        with patch.object(bridge, "api", side_effect=pages):
            with self.assertRaisesRegex(RuntimeError, "additional_reviewer_reply_present"):
                self.run_resume()
        self.assertEqual(len(calls), 2)
        self.inferences.assert_not_called()

    def test_no_quota_and_invoked_refusal_are_never_resumed(self):
        self.five = 0
        with self.assertRaisesRegex(RuntimeError, "fresh_positive_included_capacity_required"):
            self.run_resume()
        self.assertNotIn("resume", bridge.load_state()["reviewer_receipts"][SOURCE])
        self.five = 50
        self.week = 0
        with self.assertRaisesRegex(RuntimeError, "fresh_positive_included_capacity_required"):
            self.run_resume()
        self.assertNotIn("resume", bridge.load_state()["reviewer_receipts"][SOURCE])
        self.week = 60
        self.receipt["attempts"][0]["invoked"] = True
        self.save()
        with self.assertRaisesRegex(RuntimeError, "resume_not_preinvocation_quota_refusal"):
            self.run_resume()
        self.inferences.assert_not_called()
        self.assertFalse(self.posts)

    def test_stale_or_unknown_capacity_is_not_an_invocation(self):
        bridge.refresh_included_capacity.side_effect = lambda: {
            "collected_at_utc": "2026-09-26T04:20:46Z", "records": []}
        with self.assertRaisesRegex(RuntimeError, "fresh_positive_included_capacity_required"):
            self.run_resume()
        self.assertNotIn("resume", bridge.load_state()["reviewer_receipts"][SOURCE])
        self.inferences.assert_not_called()

    def test_request_issue_artifact_refusal_and_signer_drift_fail_closed(self):
        mutations = [
            lambda: self.issue.update(status="DECIDED"),
            lambda: self.issue.update(request_author_id="wrong"),
            lambda: self.issue.update(tuple_sha256="0" * 64),
            lambda: self.message.update(content=self.content + "\nEDIT"),
            lambda: self.refusal.update(content=REFUSAL_BODY + " modified"),
            lambda: self.artifact.write_text("drift"),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                original_issue = dict(self.issue)
                original_message = dict(self.message)
                original_refusal = dict(self.refusal)
                artifact = self.artifact.read_bytes()
                mutate()
                self.write_issue()
                with self.assertRaises(RuntimeError):
                    self.run_resume()
                self.assertNotIn("resume", bridge.load_state()["reviewer_receipts"][SOURCE])
                self.issue = original_issue
                self.message = original_message
                self.refusal = original_refusal
                self.artifact.write_bytes(artifact)
        self.inferences.assert_not_called()
        self.assertFalse(self.posts)

    def test_uncertain_inference_and_post_are_not_retried(self):
        self.inferences.side_effect = TimeoutError("uncertain")
        with self.assertRaisesRegex(RuntimeError, "resume_no_safe_inference"):
            self.run_resume()
        claimed = bridge.load_state()["reviewer_receipts"][SOURCE]["resume"]
        self.assertEqual(claimed["status"], "no_inference")
        self.assertTrue(claimed["attempts"][0]["invoked"])
        self.assertFalse(self.posts)
        self.assertEqual(self.inferences.call_count, 1)
        with self.assertRaisesRegex(RuntimeError, "resume_receipt_not_eligible"):
            self.run_resume()
        self.assertEqual(self.inferences.call_count, 1)

    def test_successor_is_registered_once_after_terminal_no_decision(self):
        terminal_id = "1553281139517423701"
        terminal_body = ("(Reviewer response exceeded the single-message safety limit; "
                         "no authority decision was issued.)\n[reviewer: fable-xhigh]")
        terminal = dict(id=terminal_id, content=terminal_body, author={"id": SIGNER},
                        edited_timestamp=None, message_reference={"message_id": SOURCE}, attachments=[])
        self.extra.append(terminal)
        self.receipt["resume"] = dict(status="posted", posting="posted", issue_id=ISSUE,
            tuple_sha256=self.digest, posted_message_id=terminal_id,
            reviewer_identity="fable-xhigh", reply_chars=len(terminal_body),
            reply_sha256=hashlib.sha256(terminal_body.encode()).hexdigest(),
            attempts=[dict(backend="fable", invoked=True, status="ok")])
        self.save()
        original_api = self.api
        def api(method, path, body=None):
            return terminal if method == "GET" and path.endswith("/" + terminal_id) else original_api(method, path, body)
        def issue_request(*, subject_id, tuple_sha256, review_artifact_path):
            self.assertEqual(tuple_sha256, self.digest)
            path = self.issue_dir / ("bridge-issue-" + "f" * 32 + ".json")
            fresh = dict(issue_id=path.stem, status="ISSUED", subject_id=subject_id,
                         tuple_sha256=tuple_sha256, review_artifact_path=str(review_artifact_path),
                         request_content="new canonical request")
            path.write_text(json.dumps(fresh))
            return path, fresh
        fake_guard = SimpleNamespace(issue_request=issue_request,
            _record_hash=lambda value: hashlib.sha256(json.dumps(
                {key: item for key, item in value.items() if key != "record_sha256"},
                sort_keys=True, separators=(",", ":")).encode()).hexdigest())
        fake_spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
        with patch.object(bridge, "api", side_effect=api), \
                patch.object(bridge.importlib.util, "spec_from_file_location", return_value=fake_spec), \
                patch.object(bridge.importlib.util, "module_from_spec", return_value=fake_guard), \
                patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            result = bridge.issue_no_decision_successor(SOURCE, ISSUE, self.digest, terminal_id)
            self.assertEqual(result["status"], "ISSUED_NOT_SENT")
            registered = json.loads(Path(result["issue_record_path"]).read_text())
            self.assertEqual(registered["predecessor_request_message_id"], SOURCE)
            self.assertEqual(registered["predecessor_reply_message_id"], terminal_id)
            self.assertEqual(registered["predecessor_issue_id"], ISSUE)
            self.assertEqual(registered["tuple_sha256"], self.digest)
            with self.assertRaisesRegex(RuntimeError, "successor_terminal_receipt_not_eligible"):
                bridge.issue_no_decision_successor(SOURCE, ISSUE, self.digest, terminal_id)
        self.inferences.assert_not_called()
        self.assertFalse(self.posts)

    def test_successor_refuses_terminal_reply_with_decision(self):
        terminal_id = "1553281139517423701"
        terminal_body = "TRUE-BRIDGE FABLE — ACCEPT (this message is the SOLE valid acceptance)"
        self.receipt["resume"] = dict(status="posted", posting="posted", issue_id=ISSUE,
            tuple_sha256=self.digest, posted_message_id=terminal_id,
            reviewer_identity="fable-xhigh", reply_chars=len(terminal_body),
            reply_sha256=hashlib.sha256(terminal_body.encode()).hexdigest(),
            attempts=[dict(backend="fable", invoked=True, status="ok")])
        self.save()
        terminal = dict(id=terminal_id, content=terminal_body, author={"id": SIGNER},
                        edited_timestamp=None, message_reference={"message_id": SOURCE})
        original_api = self.api
        with patch.object(bridge, "api", side_effect=lambda method, path, body=None:
                terminal if method == "GET" and path.endswith("/" + terminal_id)
                else original_api(method, path, body)):
            with self.assertRaisesRegex(RuntimeError, "successor_predecessor_reply_not_no_decision"):
                bridge.issue_no_decision_successor(SOURCE, ISSUE, self.digest, terminal_id)
        self.assertNotIn("successor_issues", bridge.load_state())


if __name__ == "__main__":
    unittest.main()
