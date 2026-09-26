"""Offline oversize delivery contract; no Discord or reviewer network access."""
import hashlib
import importlib.util
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fable_oversize", ROOT / "scripts/fable_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
SOURCE = "1553266002706309203"
POSTED = "1553281139517423701"
SIGNER = "1529571092371476591"
ACCEPT = "TRUE-BRIDGE FABLE — ACCEPT (this message is the SOLE valid acceptance)"
REJECT = "TRUE-BRIDGE FABLE — REJECT"


class OversizeTests(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.api = Mock()
        self.http = Mock()
        self.http.request.side_effect = self.send
        for name, value in (("HTTP", self.http), ("api", self.api), ("SELF_ID", SIGNER)):
            guard = patch.object(bridge, name, value)
            guard.start()
            self.addCleanup(guard.stop)

    def send(self, method, url, **kwargs):
        self.sent.append((method, url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"id": POSTED})

    def assert_delivery(self, output, expected):
        reply = bridge._tag_reply("review_request", output, "fable-xhigh")
        wrapper, raw, digest = bridge._oversize_reply(reply, "review_request")
        self.assertEqual(raw, output.encode("utf-8"))
        self.assertTrue(wrapper.startswith(expected + "\n"))
        self.assertIn(digest, wrapper)
        self.assertEqual(bridge.post(reply, reply_to_message_id=SOURCE, nonce=SOURCE + "-r1"), POSTED)
        self.assertEqual(len(self.sent), 1)
        self.api.assert_not_called()
        method, url, kwargs = self.sent[0]
        self.assertEqual(method, "POST")
        self.assertEqual(url, bridge.BASE + f"/channels/{bridge.CHANNEL}/messages")
        payload = json.loads(kwargs["data"]["payload_json"])
        self.assertEqual(payload["content"], wrapper)
        self.assertEqual(payload["nonce"], SOURCE + "-r1")
        self.assertTrue(payload["enforce_nonce"])
        self.assertEqual(payload["allowed_mentions"], {"parse": [], "replied_user": False})
        self.assertEqual(payload["message_reference"]["message_id"], SOURCE)
        self.assertEqual(payload["attachments"], [{"id": 0, "filename": "reviewer-response.txt"}])
        self.assertEqual(kwargs["files"]["files[0]"][1], output.encode("utf-8"))
        message = {"id": POSTED, "content": wrapper, "edited_timestamp": None,
                   "author": {"id": SIGNER}, "message_reference": {"message_id": SOURCE},
                   "attachments": [{"filename": "reviewer-response.txt", "size": len(raw),
                                    "url": "https://cdn.discordapp.com/attachments/fixture/reviewer-response.txt"}]}
        self.api.return_value = message
        with patch.object(bridge.requests, "get", return_value=SimpleNamespace(
                content=raw, raise_for_status=lambda: None)) as get:
            self.assertEqual(bridge.verify_oversize_post(POSTED, SOURCE, reply, "review_request")
                             ["attachment_sha256"], hashlib.sha256(raw).hexdigest())
            get.assert_called_once_with(message["attachments"][0]["url"], timeout=30)
        return reply

    def test_oversize_accept_never_manufactures_acceptance(self):
        self.assert_delivery(ACCEPT + "\ninspection_note: " + "a" * 1800 + "\n",
                             "Reviewer response attached; no parsable verdict, no authority decision was issued.")

    def test_oversize_leading_bom_or_newline_is_not_acceptance(self):
        for prefix in ("\ufeff", "\n", "\r\n"):
            with self.subTest(prefix=repr(prefix)):
                self.sent.clear()
                self.api.reset_mock()
                self.assert_delivery(prefix + ACCEPT + "\n" + "a" * 1800,
                                     "Reviewer response attached; no parsable verdict, no authority decision was issued.")

    def test_oversize_withdrawn_or_negated_acceptance_is_not_authority(self):
        for contradiction in ("I withdraw this approval. No release authority is granted.",
                              "Do not deploy; the review fails the criteria.",
                              "This is not approved and must not be accepted.",
                              "Approval is rescinded; do not release."):
            with self.subTest(contradiction=contradiction):
                self.sent.clear()
                self.api.reset_mock()
                self.assert_delivery(ACCEPT + "\n" + contradiction + "\n" + "a" * 1800,
                                     "Reviewer response attached; no parsable verdict, no authority decision was issued.")

    def test_oversize_conditional_pending_void_never_accept(self):
        for qualification in ("Acceptance is conditional on legal sign-off; no unconditional go-live yet.",
                              "Approval is pending independent verification; hold rollout until reviewed.",
                              "This approval is void; hold deployment."):
            with self.subTest(qualification=qualification):
                self.sent.clear()
                self.api.reset_mock()
                self.assert_delivery(ACCEPT + "\n" + qualification + "\n" + "a" * 1800,
                                     "Reviewer response attached; no parsable verdict, no authority decision was issued.")

    def test_oversize_reject_verbatim(self):
        self.assert_delivery(REJECT + "\n" + "défect" * 300 + "\n", REJECT)

    def test_oversize_no_verdict_does_not_manufacture_authority(self):
        self.assert_delivery("Quoted contract: " + ACCEPT + "\n" + "x" * 1800 + "\n",
                             "Reviewer response attached; no parsable verdict, no authority decision was issued.")

    def test_followup_fits_uses_short_decision_and_original_attachment(self):
        original = ACCEPT + "\n" + "analysis " * 230 + "\n"
        compact = ACCEPT + "\nsubject: fixture; tuple SHA-256: abc; challenge: xyz; inspected fixture."
        record = {}
        seen = []
        def invoke(backend, prompt, receipt, deadline, **kwargs):
            seen.append((backend, prompt, kwargs.get("before_invoke")))
            receipt.update(reviewer_identity="fable-xhigh", backend="fable",
                           model="claude-fable-5-1", effort="xhigh")
            receipt.setdefault("attempts", []).append(dict(invoked=True, status="ok"))
            return (original if len(seen) == 1 else compact), False
        with patch.object(bridge, "select_backend", return_value=("fable", "claude-fable-5-1", "xhigh")), \
             patch.object(bridge, "_invoke_reviewer", side_effect=invoke):
            reply = bridge.ask_fable("review_request", "design fixture", receipt=record)
        self.assertEqual(len(seen), 2)
        self.assertEqual(seen[0][0], seen[1][0])
        self.assertIn("SAME review session", seen[1][1])
        self.assertEqual(record.pop("original_stdout"), original)
        self.assertEqual(record["original_stdout_sha256"], hashlib.sha256(original.encode()).hexdigest())
        self.assertTrue(reply.startswith(compact + "\n"))
        self.assertIsNone(bridge._oversize_reply(reply, "review_request"))
        bridge.post(reply, reply_to_message_id=SOURCE, original_stdout=original)
        payload = json.loads(self.sent[0][2]["data"]["payload_json"])
        self.assertEqual(payload["content"], reply)
        self.assertEqual(self.sent[0][2]["files"]["files[0]"][1], original.encode())
        self.api.return_value = {"id": POSTED, "content": reply, "edited_timestamp": None,
                                 "author": {"id": SIGNER}, "message_reference": {"message_id": SOURCE},
                                 "attachments": [{"filename": "reviewer-response.txt", "size": len(original.encode()),
                                                  "url": "https://cdn.discordapp.com/attachments/fixture/reviewer-response.txt"}]}
        with patch.object(bridge.requests, "get", return_value=SimpleNamespace(
                content=original.encode(), raise_for_status=lambda: None)):
            self.assertEqual(bridge.verify_oversize_post(POSTED, SOURCE, reply, "review_request",
                                                          original_stdout=original)["attachment_sha256"],
                             hashlib.sha256(original.encode()).hexdigest())

    def test_followup_oversize_or_failure_is_no_decision(self):
        original = ACCEPT + "\n" + "a" * 1800
        for second, status in ((ACCEPT + "\n" + "b" * 1800, "ok"),
                               ("(Reviewer CLI failed)", "cli_error")):
            with self.subTest(status=status):
                record = {}
                calls = []
                def invoke(backend, prompt, receipt, deadline, **kwargs):
                    calls.append(prompt)
                    receipt.update(reviewer_identity="fable-xhigh", backend="fable",
                                   model="claude-fable-5-1", effort="xhigh")
                    receipt.setdefault("attempts", []).append(dict(invoked=True, status="ok" if len(calls) == 1 else status))
                    return (original if len(calls) == 1 else second), False
                with patch.object(bridge, "select_backend", return_value=("fable", "claude-fable-5-1", "xhigh")), \
                     patch.object(bridge, "_invoke_reviewer", side_effect=invoke):
                    reply = bridge.ask_fable("review_request", "design fixture", receipt=record)
                self.assertEqual(len(calls), 2)
                self.assertNotIn(ACCEPT, reply)
                self.assertIn("no authority decision was issued", reply)
                self.assertIsNone(bridge._oversize_reply(reply, "review_request"))
                self.assertEqual(record["original_stdout"], original)

    def test_short_reply_unchanged_json_path(self):
        reply = bridge._tag_reply("review_request", REJECT + "\nSubstantive note.", "fable-xhigh")
        self.assertEqual(reply, REJECT + "\nSubstantive note.\n[reviewer: fable-xhigh]")
        self.api.return_value = {"id": POSTED}
        self.assertEqual(bridge.post(reply, reply_to_message_id=SOURCE), POSTED)
        self.assertEqual(self.api.call_args.args[:2], ("POST", f"/channels/{bridge.CHANNEL}/messages"))
        self.assertEqual(self.api.call_args.args[2]["content"], reply)
        self.assertIsNone(bridge.verify_oversize_post(POSTED, SOURCE, reply, "review_request"))
        self.assertFalse(self.sent)

    def test_attachment_readback_mismatch_is_terminal_not_a_second_post(self):
        reply = bridge._tag_reply("review_request", REJECT + "\n" + "a" * 1600, "fable-xhigh")
        bridge.post(reply, reply_to_message_id=SOURCE)
        wrapper, raw, _ = bridge._oversize_reply(reply, "review_request")
        self.api.return_value = {"id": POSTED, "content": wrapper, "edited_timestamp": None,
                                 "author": {"id": SIGNER}, "message_reference": {"message_id": SOURCE},
                                 "attachments": [{"filename": "reviewer-response.txt", "size": len(raw),
                                                  "url": "https://cdn.discordapp.com/attachments/fixture/reviewer-response.txt"}]}
        with patch.object(bridge.requests, "get", return_value=SimpleNamespace(
                content=b"drift", raise_for_status=lambda: None)):
            with self.assertRaisesRegex(RuntimeError, "oversize_attachment_bytes_mismatch"):
                bridge.verify_oversize_post(POSTED, SOURCE, reply, "review_request")
        self.assertEqual(len(self.sent), 1)


if __name__ == "__main__":
    unittest.main()
