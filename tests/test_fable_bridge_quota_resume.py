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
        # The guard is site-owned and excluded from the curated CI checkout.
        # On the host, execute its actual module against temp-root fixtures.
        guard_source = Path("/home/ubuntu/business/ventures/boring-ai/holistic-machine/scripts/bridge_authority_guard.py")
        if not guard_source.is_file():
            self.skipTest("host-only bridge authority guard source unavailable")
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
        self.assert_successor_registered(terminal)

    def assert_successor_registered(self, terminal):
        terminal_id = terminal["id"]
        original_api = self.api
        def api(method, path, body=None):
            return terminal if method == "GET" and path.endswith("/" + terminal_id) else original_api(method, path, body)
        # Load the actual guard source, replacing only its filesystem roots and
        # authenticated GET seam after execution. The issue/hash/bind paths stay real.
        original_spec = bridge.importlib.util.spec_from_file_location
        loaded = []
        def fixture_spec(name, path):
            spec = original_spec(name, path)
            loader = spec.loader
            class FixtureLoader:
                def create_module(self, spec):
                    return None
                def exec_module(self, module):
                    loader.exec_module(module)
                    module.BRIDGE_ISSUE_ROOT = self_root.issue_dir
                    module.REVIEW_ARTIFACT_ROOT = self_root.artifact_root
                    module._fetch_discord_message = lambda channel, message_id: {
                        "id": message_id, "channel_id": channel,
                        "author": {"id": module.KAEL_SHARED_BOT_AUTHOR_ID},
                        "content": loaded_request["content"],
                        "timestamp": module._utc_now(), "edited_timestamp": None,
                    }
                    loaded.append(module)
            spec.loader = FixtureLoader()
            return spec
        self_root = self
        loaded_request = {}
        with patch.object(bridge, "api", side_effect=api), \
                patch.object(bridge.importlib.util, "spec_from_file_location", side_effect=fixture_spec), \
                patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            result = bridge.issue_no_decision_successor(SOURCE, ISSUE, self.digest, terminal_id)
            self.assertEqual(len(loaded), 1)
            guard = loaded[0]
            registered_path = Path(result["issue_record_path"])
            registered = json.loads(registered_path.read_text())
            self.assertEqual(result["status"], "ISSUED_NOT_SENT")
            self.assertEqual(registered["predecessor_request_message_id"], SOURCE)
            self.assertEqual(registered["predecessor_reply_message_id"], terminal_id)
            self.assertEqual(registered["predecessor_issue_id"], ISSUE)
            self.assertEqual(registered["tuple_sha256"], self.digest)
            self.assertEqual(registered["record_sha256"], guard._record_hash(registered))
            self.assertEqual(registered["status"], "ISSUED")
            guard.load_issue_record(registered_path, expected_subject_id=registered["subject_id"],
                                    expected_tuple_sha256=self.digest, require_bound=False)
            loaded_request["content"] = result["request_content"]
            bound_path, bound = guard.bind_request_issue(
                issue_record_path=registered_path, request_message_id="1553281999999999999")
            self.assertEqual(bound_path, registered_path)
            self.assertEqual(bound["status"], "BOUND")
            self.assertEqual(guard.load_issue_record(registered_path,
                expected_subject_id=registered["subject_id"], expected_tuple_sha256=self.digest)[1], bound)
            with self.assertRaisesRegex(RuntimeError, "successor_terminal_receipt_not_eligible"):
                bridge.issue_no_decision_successor(SOURCE, ISSUE, self.digest, terminal_id)
        self.inferences.assert_not_called()
        self.assertFalse(self.posts)

    def test_oversize_terminal_successor_requires_exact_verified_two_attempt_shape(self):
        terminal_id = "1553281139517423701"
        raw = ("TRUE-BRIDGE FABLE — ACCEPT (initial oversized output)" * 40).encode()
        digest = hashlib.sha256(raw).hexdigest()
        body = ("(Compact reviewer decision unavailable; no authority decision was issued.)\n"
                f"Full verbatim initial reviewer response: attached as reviewer-response.txt "
                f"({len(raw)} UTF-8 bytes; SHA-256: {digest}).\n[reviewer: fable-xhigh]")
        url = "https://cdn.discordapp.com/attachments/offline/reviewer-response.txt"
        attachment = dict(url=url, filename="reviewer-response.txt", size=len(raw))
        terminal = dict(id=terminal_id, content=body, author={"id": SIGNER},
                        edited_timestamp=None, message_reference={"message_id": SOURCE},
                        attachments=[attachment])
        attempt = dict(backend="fable", model="claude-fable-5-1", effort="xhigh",
                       session_id="20260922_120000_abcdef", session_mode="native_import_resume",
                       legacy_session_id="00000000-0000-4000-8000-000000000001",
                       invoked=True, status="ok")
        self.receipt["resume"] = dict(status="posted", posting="posted", issue_id=ISSUE,
            tuple_sha256=self.digest, posted_message_id=terminal_id,
            reviewer_identity="fable-xhigh", backend="fable", model="claude-fable-5-1",
            effort="xhigh", reply_chars=len(body),
            reply_sha256=hashlib.sha256(body.encode()).hexdigest(),
            original_stdout_sha256=digest, original_stdout_bytes=len(raw),
            attachment_verification=dict(attachment_sha256=digest, attachment_bytes=len(raw),
                posted_content_sha256=hashlib.sha256(body.encode()).hexdigest()),
            attempts=[dict(attempt), dict(attempt, status="timeout")])
        original_api = self.api
        def api(method, path, data=None):
            return terminal if method == "GET" and path.endswith("/" + terminal_id) else original_api(method, path, data)
        fetched = Mock(content=raw)
        with patch.object(bridge, "api", side_effect=api), \
                patch.object(bridge.requests, "get", return_value=fetched) as download, \
                patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            def refused(mutate, expected):
                mutate()
                self.save()
                with self.assertRaisesRegex(RuntimeError, expected):
                    bridge.issue_no_decision_successor(SOURCE, ISSUE, self.digest, terminal_id)
                self.assertNotIn("successor_issues", bridge.load_state())
            def verdict(text):
                altered = body + "\nTRUE-BRIDGE FABLE — " + text
                terminal["content"] = altered
                self.receipt["resume"].update(reply_chars=len(altered),
                    reply_sha256=hashlib.sha256(altered.encode()).hexdigest())
            baseline = json.loads(json.dumps(self.receipt["resume"]))
            # Reviewer probe regression: deleting both bindings used to register.
            # Exercise the actual successor admission before its host-only guard.
            binding_cases = [
                ("both_missing", lambda r: (r.pop("original_stdout_sha256"), r.pop("original_stdout_bytes"))),
            ]
            for field, empty, wrong_type, malformed, mismatch in (
                ("original_stdout_sha256", "", 7, "g" * 64, "0" * 64),
                ("original_stdout_bytes", 0, True, -1, len(raw) + 1),
            ):
                binding_cases.extend([
                    (field + "_missing", lambda r, field=field: r.pop(field)),
                    (field + "_empty", lambda r, field=field, value=empty: r.__setitem__(field, value)),
                    (field + "_wrong_type", lambda r, field=field, value=wrong_type: r.__setitem__(field, value)),
                    (field + "_malformed", lambda r, field=field, value=malformed: r.__setitem__(field, value)),
                    (field + "_mismatch", lambda r, field=field, value=mismatch: r.__setitem__(field, value)),
                ])
            for case, mutate in binding_cases:
                with self.subTest(binding=case):
                    self.receipt["resume"] = json.loads(json.dumps(baseline))
                    mutate(self.receipt["resume"])
                    self.assertFalse(bridge._successor_verified_attachment(terminal, self.receipt["resume"], body))
                    refused(lambda: None, "successor_predecessor_reply_not_no_decision")
            # All attachment receipt fields are mandatory; no receipt default may
            # stand in for an independently verified byte digest or size.
            verification_cases = [
                ("attachment_sha256", "", 7, "g" * 64, "0" * 64),
                ("attachment_bytes", 0, True, -1, len(raw) + 1),
                ("posted_content_sha256", "", 7, "g" * 64, "0" * 64),
            ]
            for field, empty, wrong_type, malformed, mismatch in verification_cases:
                for label, value in (("missing", None), ("empty", empty),
                                     ("wrong_type", wrong_type), ("malformed", malformed),
                                     ("mismatch", mismatch)):
                    with self.subTest(verified_field=field, defect=label):
                        self.receipt["resume"] = json.loads(json.dumps(baseline))
                        verified = self.receipt["resume"]["attachment_verification"]
                        if label == "missing":
                            verified.pop(field)
                        else:
                            verified[field] = value
                        self.assertFalse(bridge._successor_verified_attachment(terminal, self.receipt["resume"], body))
                        refused(lambda: None, "successor_predecessor_reply_not_no_decision")
            for mutate, expected in [
                (lambda: self.receipt["resume"]["attempts"].append(dict(attempt)), "receipt_not_eligible"),
                (lambda: self.receipt["resume"]["attempts"].pop(), "reply_not_no_decision"),
                (lambda: self.receipt["resume"]["attempts"][1].update(session_id="other"), "receipt_not_eligible"),
                (lambda: self.receipt["resume"]["attachment_verification"].update(attachment_sha256="0" * 64), "reply_not_no_decision"),
                (lambda: terminal["attachments"][0].update(size=len(raw) + 1), "reply_not_no_decision"),
                (lambda: terminal["attachments"].append(dict(attachment)), "reply_not_no_decision"),
                (lambda: setattr(fetched, "content", raw + b"drift"), "reply_not_no_decision"),
                (lambda: verdict("REJECT"), "reply_not_no_decision"),
                (lambda: verdict("ACCEPT"), "reply_not_no_decision"),
            ]:
                with self.subTest(mutate=expected):
                    self.receipt["resume"] = json.loads(json.dumps(baseline))
                    terminal.update(content=body, attachments=[dict(attachment)])
                    terminal["attachments"][0]["size"] = len(raw)
                    fetched.content = raw
                    refused(mutate, "successor_predecessor_" + expected if expected.startswith("reply") else "successor_terminal_" + expected)
            self.receipt["resume"] = baseline
            terminal.update(content=body, attachments=[dict(attachment)])
            terminal["attachments"][0]["size"] = len(raw)
            fetched.content = raw
            self.save()
            self.extra.append(terminal)
            if Path("/home/ubuntu/business/ventures/boring-ai/holistic-machine/scripts/bridge_authority_guard.py").is_file():
                self.assert_successor_registered(terminal)
            else:
                # CI excludes the site-owned guard; prove admission reaches its gate.
                with patch.object(bridge.importlib.util, "spec_from_file_location", return_value=None):
                    with self.assertRaisesRegex(RuntimeError, "registered_issue_guard_unavailable"):
                        bridge.issue_no_decision_successor(SOURCE, ISSUE, self.digest, terminal_id)
                self.assertEqual(bridge.load_state()["successor_issues"][SOURCE]["status"], "reserved")
            download.assert_called_with(url, timeout=30)

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
