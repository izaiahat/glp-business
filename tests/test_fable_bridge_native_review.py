"""Offline native reviewer contract; never invoke Claude, Hermes inference or Discord."""
import importlib.util
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fable_native_bridge", ROOT / "scripts/fable_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class NativeReviewerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.legacy = "00000000-0000-4000-8000-000000000001"
        self.native = "20260922_120000_abcdef"
        self.transcript = self.root / (self.legacy + ".jsonl")
        self.transcript.write_text('{"type":"user"}\n')
        self.db = self.root / "state.db"
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE sessions (id TEXT, source TEXT, origin_json TEXT)")
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (self.native, "claude-code", json.dumps({
                "imported_from": {"tool": "claude-code", "foreign_session_id": self.legacy,
                                  "path": str(self.transcript)}
            })))
        self.policy = self.root / "policy.json"
        self.policy.write_bytes((ROOT / "ops/claude-model-policy.json").read_bytes())
        self.usage = self.root / "usage.json"
        self.usage.write_text(json.dumps({"rate_limits": {"seven_day": {"used_percentage": 0}}}))
        for name, value in (("SESSION_ID", self.legacy), ("HERMES_STATE", self.db),
                            ("MODEL_POLICY", self.policy), ("USAGE_FILE", self.usage),
                            ("_session_transcript", Mock(return_value=self.transcript)),
                            ("_resolve_model", Mock(return_value=("claude-fable-5-1", "xhigh"))),
                            ("_included_quota_available", Mock(return_value=True)),
                            ("log", Mock())):
            guard = patch.object(bridge, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.dict(os.environ, {"FABLE_HERMES_SESSION_ID": self.native})
        guard.start()
        self.addCleanup(guard.stop)

    def test_native_command_contract_and_exact_prompt(self):
        prompt = bridge._model_prompt("review_request", "Inspect artifact SHA-256 and challenge")
        def run(command, **kwargs):
            self.assertEqual(command[:3], [str(bridge.HERMES_PYTHON), str(ROOT / "scripts/fable_bridge.py"), "--native-reviewer"])
            self.assertEqual(command[3], "chat")
            self.assertEqual(command[command.index("--provider") + 1], "claude-subscription-directsdk-experimental")
            self.assertEqual(command[command.index("-m") + 1], "claude-fable-5-1")
            self.assertEqual(command[command.index("--reasoning") + 1], "xhigh")
            self.assertEqual(command[command.index("--toolsets") + 1], "fable-review-readonly")
            self.assertEqual(command[command.index("--resume") + 1], self.native)
            self.assertIn("--no-restore-cwd", command)
            self.assertNotIn("--yolo", command)
            self.assertEqual(Path(command[command.index("--query-file") + 1]).read_text(), prompt)
            self.assertEqual(kwargs["cwd"], "/home/ubuntu/business")
            return SimpleNamespace(returncode=0, stdout="TRUE-BRIDGE FABLE — REJECT\ninspected bytes", stderr="")
        with patch.object(bridge.subprocess, "run", side_effect=run) as mocked:
            reply = bridge.ask_fable("review_request", "Inspect artifact SHA-256 and challenge")
        mocked.assert_called_once()
        self.assertIn("REJECT", reply)
        self.assertTrue(reply.endswith("[reviewer: fable-xhigh]"))

    def test_design_capacity_refusal_never_calls_astra_or_accepts(self):
        with patch.object(bridge.subprocess, "run", return_value=SimpleNamespace(
            returncode=1, stdout="", stderr="rate_limit_error: weekly limit")) as run:
            receipt = {}
            reply = bridge.ask_fable("review_request", "Final outward-facing visual design review", receipt=receipt)
        run.assert_called_once()
        self.assertEqual([x["backend"] for x in receipt["attempts"]], ["fable"])
        self.assertEqual(receipt["attempts"][0]["status"], "capacity_unavailable")
        self.assertIn("no authority decision", reply)
        self.assertNotIn("ACCEPT", reply)

    def test_unclassified_review_and_depleted_included_quota_never_spend(self):
        self.assertTrue(bridge._design_review("review_request", "Approve these final pages"))
        self.assertFalse(bridge._design_review("review_request", "NON-DESIGN REVIEW: verify parser invariants"))
        with patch.object(bridge, "_included_quota_available", return_value=False), patch.object(bridge.subprocess, "run") as run:
            reply = bridge.ask_fable("review_request", "Approve these final pages")
        run.assert_not_called()
        self.assertIn("no authority decision", reply)

    def test_real_policy_resolver_matches_native_bridge_contract(self):
        spec = importlib.util.spec_from_file_location("policy_readback", ROOT / "ops/claude_model_policy.py")
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        # Exercise exact checkout policy bytes, not the receiving host's private
        # default path (which is absent on hosted CI). Keep the real loader and
        # all policy/seat validation unchanged.
        with patch.object(policy, "POLICY", ROOT / "ops/claude-model-policy.json"):
            self.assertEqual(policy.resolve("bridge"), ("claude-fable-5-1", "xhigh"))
            self.assertEqual(policy.resolve(), ("claude-opus-5-5[1m]", "medium"))
        with patch.object(policy, "POLICY", self.root / "missing-policy.json"):
            with self.assertRaisesRegex(RuntimeError, "FAIL CLOSED"):
                policy.resolve("bridge")

    def test_missing_or_mismatched_import_fails_closed(self):
        for value in ("", "20260922_120000_123456"):
            with self.subTest(value=value), patch.dict(os.environ, {"FABLE_HERMES_SESSION_ID": value}):
                with patch.object(bridge.subprocess, "run") as run:
                    reply = bridge.ask_fable("review_request", "Final visual design review")
                run.assert_not_called()
                self.assertIn("no authority decision", reply)
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE sessions SET source='codex-cli'")
        with self.assertRaisesRegex(RuntimeError, "provenance_mismatch"):
            bridge._native_session_id(self.legacy)

    def test_identity_marker_nonce_and_idempotency_boundary(self):
        with patch.object(bridge, "SELF_ID", "999999999999999999"):
            message = {"id": "42", "type": 0, "author": {"id": bridge.KAEL_BOT_ID, "bot": True},
                       "mentions": [{"id": bridge.SELF_ID}],
                       "content": f"<@{bridge.SELF_ID}> {bridge.REVIEW_REQUEST_MARKER} Inspect bytes"}
            self.assertEqual(bridge.classify_message(message)[0], "review_request")
            for mutation in ({"mentions": []}, {"type": 19}, {"content": "unmarked"},
                             {"author": {"id": "wrong", "bot": True}}):
                self.assertIsNone(bridge.classify_message({**message, **mutation}))
        with patch.object(bridge, "api", return_value={"id": "123"}) as api:
            self.assertEqual(bridge.post("no authority decision", reply_to_message_id="42"), "123")
        body = api.call_args.args[2]
        self.assertEqual(body["nonce"], "42")
        self.assertIs(body["enforce_nonce"], True)
        self.assertEqual(body["message_reference"]["message_id"], "42")
        self.assertEqual(body["allowed_mentions"], {"parse": [], "replied_user": False})


if __name__ == "__main__":
    unittest.main()
