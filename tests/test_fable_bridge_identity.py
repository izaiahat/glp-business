from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "fable_bridge.py"
SPEC = importlib.util.spec_from_file_location("fable_bridge", MODULE_PATH)
assert SPEC and SPEC.loader
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


class FableBridgeIdentityTests(unittest.TestCase):
    fable_id = "1529571092371476591"

    def setUp(self) -> None:
        BRIDGE.SELF_ID = self.fable_id

    def message(
        self,
        author_id: str,
        *,
        bot: bool,
        content: str,
        mention: bool = True,
        **updates: object,
    ) -> dict:
        value = {
            "id": "1529999999999999999",
            "type": 0,
            "author": {"id": author_id, "bot": bot},
            "content": content,
            "mentions": [{"id": self.fable_id}] if mention else [],
            "edited_timestamp": None,
        }
        value.update(updates)
        return value

    def authority_config(self, path: Path, acceptance_id: str | None = None) -> None:
        path.write_text(json.dumps({
            "schema": "boring_ai_bridge_authority_v1",
            "channel_id": BRIDGE.CHANNEL,
            "request_author_id": BRIDGE.KAEL_BOT_ID,
            "acceptance_author_id": acceptance_id or self.fable_id,
        }), encoding="utf-8")

    def test_operator_requires_structured_mention(self) -> None:
        unmentioned = self.message(BRIDGE.OPERATOR_ID, bot=False, content="status", mention=False)
        self.assertIsNone(BRIDGE.classify_message(unmentioned))
        mentioned = self.message(BRIDGE.OPERATOR_ID, bot=False, content=f"<@{self.fable_id}> status")
        self.assertEqual(BRIDGE.classify_message(mentioned), ("operator", "status"))

    def test_kael_requires_anchored_marker(self) -> None:
        chatter = self.message(BRIDGE.KAEL_BOT_ID, bot=True, content=f"<@{self.fable_id}> normal chatter")
        embedded = self.message(BRIDGE.KAEL_BOT_ID, bot=True, content=f"<@{self.fable_id}> x {BRIDGE.BRIDGE_TEST_MARKER}")
        self.assertIsNone(BRIDGE.classify_message(chatter))
        self.assertIsNone(BRIDGE.classify_message(embedded))
        transport = self.message(BRIDGE.KAEL_BOT_ID, bot=True, content=f"<@{self.fable_id}> {BRIDGE.BRIDGE_TEST_MARKER} nonce-123")
        self.assertEqual(BRIDGE.classify_message(transport), ("transport_test", f"{BRIDGE.BRIDGE_TEST_MARKER} nonce-123"))
        review = self.message(BRIDGE.KAEL_BOT_ID, bot=True, content=f"<@{self.fable_id}> {BRIDGE.REVIEW_REQUEST_MARKER} subject tuple challenge")
        self.assertEqual(BRIDGE.classify_message(review)[0], "review_request")
        canonical = self.message(BRIDGE.KAEL_BOT_ID, bot=True, content=f"<@{self.fable_id}> BRIDGE REQUEST subject tuple challenge")
        self.assertEqual(BRIDGE.classify_message(canonical)[0], "review_request")

    def test_self_other_authors_bot_flag_mismatch_are_ignored(self) -> None:
        cases = [
            self.message(self.fable_id, bot=True, content=f"<@{self.fable_id}> loop"),
            self.message("1599999999999999999", bot=True, content=f"<@{self.fable_id}> {BRIDGE.BRIDGE_TEST_MARKER} x"),
            self.message(BRIDGE.KAEL_BOT_ID, bot=False, content=f"<@{self.fable_id}> {BRIDGE.BRIDGE_TEST_MARKER} x"),
            self.message(BRIDGE.OPERATOR_ID, bot=True, content=f"<@{self.fable_id}> status"),
        ]
        self.assertTrue(all(BRIDGE.classify_message(value) is None for value in cases))

    def test_webhook_system_and_edited_messages_are_ignored(self) -> None:
        base = dict(author_id=BRIDGE.OPERATOR_ID, bot=False, content=f"<@{self.fable_id}> status")
        cases = [
            self.message(**base, webhook_id="1529999999999999999"),
            self.message(**base, type=6),
            self.message(**base, edited_timestamp="2026-07-22T00:00:00Z"),
        ]
        self.assertTrue(all(BRIDGE.classify_message(value) is None for value in cases))

    def test_startup_identity_matches_runtime_file_and_checked_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            author_file = root / "author_id"
            author_file.write_text(self.fable_id + "\n", encoding="utf-8")
            author_file.chmod(0o600)
            config = root / "authority.json"
            self.authority_config(config)
            with mock.patch.object(BRIDGE, "AUTHOR_ID_FILE", author_file), mock.patch.object(
                BRIDGE, "AUTHORITY_CONFIG", config
            ), mock.patch.object(
                BRIDGE, "api", side_effect=[{"id": self.fable_id, "bot": True}, {"id": BRIDGE.CHANNEL}]
            ):
                self.assertEqual(BRIDGE.validate_startup_identity(), self.fable_id)

    def test_restricted_token_rejects_symlink_permissions_and_wrong_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            real = root / "real"
            real.write_text("sentinel", encoding="utf-8")
            real.chmod(0o600)
            link = root / "link"
            link.symlink_to(real)
            with mock.patch.object(BRIDGE, "TOKEN_FILE", link), self.assertRaises(RuntimeError):
                BRIDGE.token()
            real.chmod(0o644)
            with mock.patch.object(BRIDGE, "TOKEN_FILE", real), self.assertRaises(RuntimeError):
                BRIDGE.token()
            real.chmod(0o600)
            with mock.patch.object(BRIDGE, "TOKEN_FILE", real), mock.patch.object(
                BRIDGE.os, "getuid", return_value=os.getuid() + 1
            ), self.assertRaises(RuntimeError):
                BRIDGE.token()

    def test_one_exact_nonce_protected_referenced_reply(self) -> None:
        calls = []
        def fake_api(method: str, path: str, body: dict | None = None) -> dict:
            calls.append((method, path, body))
            return {"id": "1529999999999999998"}
        with mock.patch.object(BRIDGE, "api", side_effect=fake_api):
            result = BRIDGE.post("FABLE_BRIDGE_READY nonce-123", reply_to_message_id="1529999999999999997")
        self.assertEqual(result, "1529999999999999998")
        self.assertEqual(len(calls), 1)
        body = calls[0][2]
        self.assertEqual(body["content"], "FABLE_BRIDGE_READY nonce-123")
        self.assertEqual(body["message_reference"]["message_id"], "1529999999999999997")
        self.assertEqual(body["allowed_mentions"]["parse"], [])
        self.assertEqual(body["nonce"], "1529999999999999997")
        self.assertTrue(body["enforce_nonce"])

    def test_oversized_output_never_emits_partial_acceptance(self) -> None:
        calls = []
        with mock.patch.object(BRIDGE, "api", side_effect=lambda method, path, body=None: calls.append(body) or {"id": "1529999999999999998"}):
            BRIDGE.post("TRUE-BRIDGE FABLE — ACCEPT " + ("x" * 3000), reply_to_message_id="1529999999999999997")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("ACCEPT", calls[0]["content"])
        self.assertIn("no authority decision", calls[0]["content"])

    def test_ambiguous_post_transport_failure_is_not_retried(self) -> None:
        session = mock.Mock()
        session.request.side_effect = requests.Timeout("sentinel")
        with mock.patch.object(BRIDGE, "HTTP", session), mock.patch.object(BRIDGE, "log"):
            self.assertIsNone(BRIDGE.api("POST", "/channels/x/messages", {"content": "sentinel"}))
        self.assertEqual(session.request.call_count, 1)

    def test_bridge_source_has_no_shared_token_or_sensitive_logging_patterns(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn("fable-discord/bot_token", source)
        self.assertNotIn("/home/ubuntu/.hermes/.env", source)
        self.assertNotIn("DISCORD_BOT_TOKEN", source)
        self.assertNotIn("_thinking", source)
        self.assertNotIn("r.stderr", source)
        self.assertNotIn("text[:", source)

    def test_cursor_is_saved_before_model_execution(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        cursor = source.index('state["last_id"] = message["id"]')
        saved = source.index("save_state(state)", cursor)
        model = source.index("action_mode, reply = review_action", saved)
        self.assertLess(cursor, saved)
        self.assertLess(saved, model)


if __name__ == "__main__":
    unittest.main(verbosity=2)
