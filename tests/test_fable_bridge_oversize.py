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

    def test_oversize_accept_verbatim(self):
        self.assert_delivery(ACCEPT + "\ninspection_note: " + "a" * 1800 + "\n", ACCEPT)

    def test_oversize_reject_verbatim(self):
        self.assert_delivery(REJECT + "\n" + "défect" * 300 + "\n", REJECT)

    def test_oversize_no_verdict_does_not_manufacture_authority(self):
        self.assert_delivery("Quoted contract: " + ACCEPT + "\n" + "x" * 1800 + "\n",
                             "Reviewer response attached; no parsable verdict, no authority decision was issued.")

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
