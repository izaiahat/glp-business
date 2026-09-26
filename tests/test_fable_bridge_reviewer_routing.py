"""Offline routing/protocol regressions; no real reviewer or Discord call is allowed."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("p58_bridge", ROOT / "scripts/fable_bridge.py")
assert SPEC is not None and SPEC.loader is not None
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)
ACCEPT = "TRUE-BRIDGE FABLE — ACCEPT (this message is the SOLE valid acceptance)"
ANSWER = ACCEPT + "\nsubject=test; tuple=" + "a" * 64 + "; challenge=test; inspected fixture bytes."
FABLE = "fable-xhigh"
ASTRA = "astra-standin (claude_exhausted)"


class ReviewerRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.policy = self.root / "policy.json"
        self.policy.write_bytes((ROOT / "ops/claude-model-policy.json").read_bytes())
        self.usage = self.root / "usage.json"
        self.write_usage(0, 0)
        self.state = self.root / "state.json"
        self.sid = self.root / "sid.txt"
        self.sid.write_text("00000000-0000-4000-8000-000000000001", encoding="utf-8")
        self.run = Mock(return_value=self.result())
        self.log = Mock()
        for name, value in (
            ("MODEL_POLICY", self.policy), ("USAGE_FILE", self.usage), ("STATE", self.state),
            ("SID_FILE", self.sid), ("SESSION_ID", self.sid.read_text()), ("STOP_REQUESTED", False),
            ("SELF_ID", "999999999999999999"), ("_resolve_model", self.resolve_model),
            ("_native_session_id", Mock(return_value="20260922_120000_abcdef")), ("log", self.log),
            ("_included_quota_available", Mock(return_value=True)),
        ):
            guard = patch.object(bridge, name, value)
            guard.start()
            self.addCleanup(guard.stop)
        guard = patch.object(bridge.subprocess, "run", self.run)
        guard.start()
        self.addCleanup(guard.stop)

    @staticmethod
    def result(code: int = 0, stdout: str = ANSWER, stderr: str = "") -> SimpleNamespace:
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

    def resolve_model(self) -> tuple[str, str]:
        seat = json.loads(self.policy.read_text())["seat_overrides"]["bridge"]
        return seat["model"], seat["effort"]

    def write_usage(self, five: float, week: float) -> None:
        self.usage.write_text(json.dumps({"rate_limits": {
            "five_hour": {"used_percentage": five}, "seven_day": {"used_percentage": week},
        }}), encoding="utf-8")

    def review(self, kind: str = "review_request", text: str = "NON-DESIGN REVIEW: Inspect bound candidate bytes.") -> tuple[str, dict]:
        receipt: dict = {}
        output = bridge.ask_fable(kind, text, receipt=receipt)
        return output, receipt

    def test_policy_and_below_threshold_use_fable_xhigh(self) -> None:
        self.write_usage(94.999, 94.999)
        self.assertEqual(bridge.select_backend(), ("fable", "claude-fable-5-1", "xhigh"))
        output, receipt = self.review()
        command = self.run.call_args.args[0]
        self.assertEqual(command[0], str(bridge.HERMES_PYTHON))
        self.assertEqual(command[command.index("-m") + 1], "claude-fable-5-1")
        self.assertEqual(command[command.index("--reasoning") + 1], "xhigh")
        self.assertEqual(command[command.index("--toolsets") + 1], "fable-review-readonly")
        self.assertIn("--resume", command)
        self.assertTrue(output.endswith(f"[reviewer: {FABLE}]"))
        self.assertEqual(receipt["reviewer_identity"], FABLE)
        self.assertEqual(receipt["attempts"][0]["status"], "ok")

    def test_either_window_at_threshold_routes_astra(self) -> None:
        for five, week in ((100, 0), (0, 100), (100, 100)):
            with self.subTest(five=five, week=week):
                self.write_usage(five, week)
                self.assertEqual(bridge.select_backend(), ("astra-standin", "gpt-6-astra-900k", "xhigh"))

    def test_policy_and_usage_are_reread_without_restart(self) -> None:
        value = json.loads(self.policy.read_text())
        value["reviewer_routing"]["threshold_percent"] = 98
        self.policy.write_text(json.dumps(value))
        self.write_usage(97, 97)
        self.assertEqual(bridge.select_backend()[0], "fable")
        value["reviewer_routing"].pop("threshold_percent")
        self.policy.write_text(json.dumps(value))
        self.assertEqual(bridge.select_backend()[0], "astra-standin")
        self.write_usage(1, 1)
        self.assertEqual(bridge.select_backend()[0], "fable")

    def test_percent_fraction_and_invalid_windows(self) -> None:
        for window, expected in (
            ({"used_percentage": 1}, "fable"), ({"utilization": 1.0}, "astra-standin"),
            ({"used_percentage": "100"}, "astra-standin"),
            ({"used_percentage": 1, "utilization": 1}, "fable"),
            ({"used_percentage": True}, "fable"), ({"used_percentage": "NaN"}, "fable"),
            ({"used_percentage": "Infinity"}, "fable"), ({"used_percentage": -1}, "fable"),
            ({"used_percentage": 101}, "fable"), ({"used_percentage": "bad"}, "fable"),
            ({"used_percentage": 100, "resets_at": "bad"}, "astra-standin"),
        ):
            with self.subTest(window=window):
                self.usage.write_text(json.dumps({"rate_limits": {"seven_day": window}}))
                self.assertEqual(bridge.select_backend()[0], expected)

    def test_unknown_usage_probes_fable_not_an_arbitrary_model(self) -> None:
        for data in ("not json", "[]", '{"rate_limits": null}'):
            self.usage.write_text(data)
            self.assertEqual(bridge.select_backend()[0], "fable")
        self.usage.unlink()
        self.assertEqual(bridge.select_backend()[0], "fable")

    def test_stale_prereset_usage_reprobes_but_fresh_high_still_routes_astra(self) -> None:
        for reset in (1700000000, 1700000000000):
            self.usage.write_text(json.dumps({"rate_limits": {
                "seven_day": {"used_percentage": 100, "resets_at": reset},
            }}))
            os.utime(self.usage, (1699999900, 1699999900))
            with patch.object(bridge.time, "time", return_value=1700000100):
                self.assertEqual(bridge.select_backend()[0], "fable")
                os.utime(self.usage, (1700000050, 1700000050))
                self.assertEqual(bridge.select_backend()[0], "astra-standin")

    def test_corrupt_policy_and_wrong_fable_effort_fail_closed(self) -> None:
        original = self.policy.read_text()
        bad_values = ["{}", "not json"]
        for threshold in (0, 101, True, "95", float("nan")):
            value = json.loads(original)
            value["reviewer_routing"]["threshold_percent"] = threshold
            bad_values.append(json.dumps(value))
        for model in (None, "", "-unexpected-option"):
            value = json.loads(original)
            value["reviewer_routing"]["hermes_model"] = model
            bad_values.append(json.dumps(value))
        value = json.loads(original)
        value["seat_overrides"]["bridge"]["effort"] = "high"
        bad_values.append(json.dumps(value))
        for bad in bad_values:
            with self.subTest(policy=bad):
                self.policy.write_text(bad)
                output, _ = self.review()
                self.assertIn("no authority decision", output)
                self.assertNotIn(ACCEPT, output)
        self.run.assert_not_called()

    def test_astra_uses_real_prompt_file_max_and_fresh_namespace(self) -> None:
        self.write_usage(100, 0)
        paths, names = [], []
        prompt = bridge._model_prompt("review_request", "NON-DESIGN REVIEW: Inspect bound candidate bytes.")

        def run(command: list[str], **kwargs) -> SimpleNamespace:
            self.assertEqual(command[:2], ["hermes", "chat"])
            self.assertEqual(command[command.index("-m") + 1], "gpt-6-astra-900k")
            self.assertEqual(command[command.index("--reasoning") + 1], "xhigh")
            for flag in ("-Q", "--oneshot"):
                self.assertIn(flag, command)
            self.assertEqual(command[command.index("--provider") + 1], "openai-codex")
            self.assertIn("agent-evaluation-review", command[command.index("--skills") + 1].split(","))
            for flag in ("--ignore-user-config", "--ignore-rules", "--toolsets", "--safe-mode", "--yolo"):
                self.assertNotIn(flag, command)
            for flag in ("--resume", "--continue", "-c", "-r"):
                self.assertNotIn(flag, command)
            path = Path(command[command.index("--query-file") + 1])
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(), prompt)
            self.assertEqual(path.parent.stat().st_mode & 0o077, 0)
            paths.append(path)
            names.append(command[command.index("--source") + 1])
            self.assertEqual(kwargs["cwd"], "/home/ubuntu/business")
            self.assertTrue(0 < kwargs["timeout"] <= 1200)
            self.assertNotIn("shell", kwargs)
            return self.result()

        self.run.side_effect = run
        for _ in range(2):
            output, receipt = self.review()
            self.assertIn("--provider", self.run.call_args.args[0])
            self.assertEqual(receipt["reviewer_identity"], ASTRA)
            self.assertEqual(receipt["attempts"][0]["session_name"], names[-1])
            self.assertEqual(output.splitlines()[0], ACCEPT)
        self.assertNotEqual(names[0], names[1])
        self.assertTrue(all(name.startswith("fable-bridge-astra-standin-") for name in names))
        self.assertTrue(all(not path.exists() for path in paths))

    def test_rate_limit_fallback_keeps_prompt_and_does_not_latch(self) -> None:
        prompts = []

        def run(command: list[str], **kwargs) -> SimpleNamespace:
            if command[0] == str(bridge.HERMES_PYTHON):
                prompts.append(Path(command[command.index("--query-file") + 1]).read_text())
                return self.result(1, "", "rate_limit_error: exhausted")
            prompts.append(Path(command[command.index("--query-file") + 1]).read_text())
            return self.result()

        self.run.side_effect = run
        output, receipt = self.review()
        self.assertEqual(prompts[0], prompts[1])
        self.assertEqual(receipt["reviewer_identity"], ASTRA)
        self.assertEqual([a["backend"] for a in receipt["attempts"]], ["fable", "astra-standin"])
        self.assertEqual(receipt["attempts"][0]["status"], "capacity_unavailable")
        self.assertTrue(output.endswith(f"[reviewer: {ASTRA}]"))
        self.run.side_effect = None
        self.assertEqual(self.review()[1]["reviewer_identity"], FABLE)

    def test_refusal_variants_and_exit_zero_banner(self) -> None:
        for code, text in (
            (1, "weekly limit"), (1, "usage limit"), (1, "rate-limit reached"),
            (0, "You've hit your limit · resets later"), (0, "Error: rate limit exceeded"),
        ):
            with self.subTest(code=code, text=text):
                self.run.side_effect = [self.result(code, text), self.result()]
                self.assertEqual(self.review()[1]["reviewer_identity"], ASTRA)
        self.run.side_effect = [self.result(0, "", "weekly limit"), self.result()]
        self.assertEqual(self.review()[1]["reviewer_identity"], ASTRA)
        self.run.side_effect = None
        self.run.return_value = self.result(0, ANSWER + " Rate limit handling inspected.")
        self.assertEqual(self.review()[1]["reviewer_identity"], FABLE)

    def test_non_capacity_errors_do_not_fallback_and_both_fail_closed(self) -> None:
        for failure in (self.result(1, "", "not logged in"), self.result(1, "", "invalid model")):
            self.run.reset_mock()
            self.run.side_effect = None
            self.run.return_value = failure
            output, receipt = self.review()
            self.assertIn("no authority decision", output)
            self.assertEqual(receipt["reviewer_identity"], FABLE)
            self.assertEqual(self.run.call_count, 1)
        self.run.side_effect = [self.result(1, "weekly limit"), self.result(1, "failed")]
        output, receipt = self.review()
        self.assertIn("no authority decision", output)
        self.assertNotIn(ACCEPT, output)
        self.assertEqual(receipt["reviewer_identity"], ASTRA)
        self.assertEqual(len(receipt["attempts"]), 2)

    def test_empty_missing_and_timeout_are_tagged_and_clean_up(self) -> None:
        for usage in (0, 100):
            self.write_usage(usage, 0)
            for failure in (self.result(0, ""), FileNotFoundError(), subprocess.TimeoutExpired("fake", 1)):
                paths = []

                def run(command: list[str], **kwargs) -> SimpleNamespace:
                    if "--query-file" in command:
                        paths.append(Path(command[command.index("--query-file") + 1]))
                    if isinstance(failure, Exception):
                        raise failure
                    return failure

                self.run.side_effect = run
                output, receipt = self.review()
                self.assertIn("no authority decision", output)
                self.assertNotIn(ACCEPT, output)
                self.assertTrue(output.endswith(f"[reviewer: {receipt['reviewer_identity']}]"))
                self.assertTrue(all(not path.exists() for path in paths))

    def test_fallback_shares_the_original_wall_clock_budget(self) -> None:
        self.run.side_effect = [self.result(1, "usage limit"), self.result()]
        with patch.object(bridge.time, "monotonic", side_effect=[10, 20, 310]):
            self.review()
        self.assertEqual([c.kwargs["timeout"] for c in self.run.call_args_list], [1190, 900])
        self.run.reset_mock()
        self.run.side_effect = [self.result(1, "usage limit")]
        with patch.object(bridge.time, "monotonic", side_effect=[10, 20, 1210]):
            output, _ = self.review()
        self.assertEqual(self.run.call_count, 1)
        self.assertIn("no authority decision", output)

    def test_tags_preserve_first_line_and_recount_both_limits(self) -> None:
        for kind, limit in (("review_request", 1399), ("operator", 1900), ("transport_test", 1900)):
            for identity in (FABLE, ASTRA):
                suffix = f"\n[reviewer: {identity}]"
                body = ACCEPT + "\n" + "x" * (limit - len(suffix) - len(ACCEPT) - 1)
                output = bridge._tag_reply(kind, body, identity)
                self.assertEqual(len(output), limit)
                self.assertEqual(output.splitlines()[0], ACCEPT)
                self.assertTrue(output.endswith(suffix))
                over = bridge._tag_reply(kind, body + "x", identity)
                self.assertLessEqual(len(over), limit)
                self.assertNotIn(ACCEPT, over)
                self.assertIn("no authority decision", over)
                self.assertTrue(over.endswith(suffix))
        self.assertEqual(bridge._tag_reply("transport_test", "FABLE_BRIDGE_READY nonce", FABLE).splitlines()[0],
                         "FABLE_BRIDGE_READY nonce")
        self.assertNotIn(ACCEPT, bridge._tag_reply("review_request", ANSWER + "\n[reviewer: fake]", FABLE))

    def test_top_stakes_remains_deferred_without_policy_or_model(self) -> None:
        self.policy.unlink()
        for text in ("production deploy", "pricing", "cloudflare", "evidence-sensitive"):
            receipt = {}
            mode, output = bridge.review_action("review_request", text, source_message_id="42", receipt=receipt)
            self.assertEqual(mode, "deferred")
            self.assertTrue(output.startswith("TOP-STAKES DEFERRED — no authority decision was issued"))
            self.assertEqual(receipt["reviewer_identity"], "none (top_stakes_deferred)")
            self.assertEqual(receipt["attempts"], [])
        self.run.assert_not_called()
        for kind in ("operator", "transport_test"):
            self.assertIsNone(bridge.top_stakes_category(kind, "production deploy"))

    def message(self, source: str = "42") -> dict:
        return {"id": source, "author": {"id": bridge.KAEL_BOT_ID, "bot": True},
                "mentions": [{"id": bridge.SELF_ID}],
                "content": f"<@{bridge.SELF_ID}> {bridge.REVIEW_REQUEST_MARKER} NON-DESIGN REVIEW: inspect bound candidate bytes."}

    def test_classification_guards_unchanged(self) -> None:
        message = self.message()
        self.assertEqual(bridge.classify_message(message)[0], "review_request")
        for update in (
            {"mentions": []}, {"webhook_id": "1"}, {"edited_timestamp": "now"}, {"type": 1},
            {"author": {"id": bridge.SELF_ID, "bot": True}},
            {"author": {"id": "untrusted", "bot": True}}, {"content": "unmarked text"},
        ):
            self.assertIsNone(bridge.classify_message({**message, **update}))

    def run_main(
        self, *, ambiguous: bool = False, drain: bool = False, switch: bool = False,
    ) -> list[dict]:
        self.state.write_text('{"last_id": "1"}')
        bodies = []
        messages = [self.message(), self.message("43")] if drain or switch else [self.message()]

        def api(method: str, path: str, body=None):
            if method == "GET":
                return messages
            source = body["nonce"]
            identity = FABLE if switch and source == "43" else ASTRA
            receipt = bridge.load_state()["reviewer_receipts"][source]
            self.assertEqual(receipt["posting"], "prepared")
            self.assertEqual(receipt["reviewer_identity"], identity)
            self.assertEqual(receipt["reply_chars"], len(body["content"]))
            self.assertEqual(receipt["reply_sha256"], hashlib.sha256(body["content"].encode()).hexdigest())
            self.assertEqual(body["message_reference"], {
                "message_id": source, "channel_id": bridge.CHANNEL, "fail_if_not_exists": True,
            })
            self.assertEqual(body["allowed_mentions"], {"parse": [], "replied_user": False})
            self.assertEqual(body["nonce"], source)
            self.assertIs(body["enforce_nonce"], True)
            self.assertTrue(body["content"].endswith(f"[reviewer: {identity}]"))
            bodies.append(body)
            if switch:
                self.write_usage(0, 0)
            if drain:
                bridge._request_stop(bridge.signal.SIGTERM, None)
            return None if ambiguous else {"id": "12345"}

        self.write_usage(100, 0)
        with patch.object(bridge, "api", side_effect=api), patch.object(bridge, "token", return_value="fake"), \
             patch.object(bridge, "validate_startup_identity", return_value=bridge.SELF_ID), \
             patch.object(bridge.requests, "Session"), patch.object(bridge.signal, "signal") as install, \
             patch.object(bridge.time, "sleep", side_effect=StopIteration):
            if ambiguous:
                with self.assertRaisesRegex(RuntimeError, "post_failed_or_ambiguous"):
                    bridge.main()
            elif drain:
                bridge.main()
            else:
                with self.assertRaises(StopIteration):
                    bridge.main()
            install.assert_called_once_with(bridge.signal.SIGTERM, bridge._request_stop)
        return bodies

    def test_receipt_precedes_post_and_records_posted_identity(self) -> None:
        self.assertEqual(len(self.run_main()), 1)
        receipt = bridge.load_state()["reviewer_receipts"]["42"]
        self.assertEqual(receipt["posting"], "posted")
        self.assertEqual(receipt["posted_message_id"], "12345")
        self.assertEqual(receipt["reviewer_identity"], ASTRA)

    def test_each_source_keeps_its_identity_when_usage_resets(self) -> None:
        self.assertEqual(len(self.run_main(switch=True)), 2)
        receipts = bridge.load_state()["reviewer_receipts"]
        self.assertEqual(receipts["42"]["reviewer_identity"], ASTRA)
        self.assertEqual(receipts["43"]["reviewer_identity"], FABLE)
        self.assertEqual(receipts["42"]["attempts"][0]["backend"], "astra-standin")
        self.assertEqual(receipts["43"]["attempts"][0]["backend"], "fable")

    def test_existing_fable_session_is_still_resumed(self) -> None:
        _, receipt = self.review()
        command = self.run.call_args.args[0]
        self.assertIn("--resume", command)
        self.assertNotIn("--session-id", command)
        self.assertEqual(command[command.index("--resume") + 1], "20260922_120000_abcdef")
        self.assertEqual(receipt["attempts"][0]["session_mode"], "native_import_resume")

    def test_ambiguous_post_is_receipted_without_retry(self) -> None:
        self.assertEqual(len(self.run_main(ambiguous=True)), 1)
        receipt = bridge.load_state()["reviewer_receipts"]["42"]
        self.assertEqual(receipt["posting"], "failed_or_ambiguous")
        self.assertIsNone(receipt["posted_message_id"])

    def test_sigterm_drains_current_message_without_advancing_next(self) -> None:
        self.assertEqual(len(self.run_main(drain=True)), 1)
        state = bridge.load_state()
        self.assertEqual(state["last_id"], "42")
        self.assertEqual(set(state["reviewer_receipts"]), {"42"})
        self.assertEqual(state["reviewer_receipts"]["42"]["posting"], "posted")


def mutation_receipt() -> None:
    """Prove mutants in disposable copies, never in the running bridge's tree."""
    script = "scripts/fable_bridge.py"
    policy = "ops/claude-model-policy.json"
    tests = "tests/test_fable_bridge_reviewer_routing.py"
    mutants = (
        (script, "if used >= threshold:", "if used > threshold:",
         "test_either_window_at_threshold_routes_astra"),
        (policy, '"threshold_percent": 95', '"threshold_percent": 96',
         "test_either_window_at_threshold_routes_astra"),
        (script, 'return "astra-standin", routing["hermes_model"], "max"',
         'return "astra-standin", routing["hermes_model"], "low"',
         "test_either_window_at_threshold_routes_astra"),
        (script, 'used = float(raw) * (100 if fraction else 1)', 'used = float(raw)',
         "test_percent_fraction_and_invalid_windows"),
        (script, 'if observed_at < reset <= time.time():', 'if False:',
         "test_stale_prereset_usage_reprobes_but_fresh_high_still_routes_astra"),
        (script, 'if exhausted:', 'if False:',
         "test_rate_limit_fallback_keeps_prompt_and_does_not_latch"),
        (script, 'prompt_path.write_text(prompt, encoding="utf-8")',
         'prompt_path.write_text("changed prompt", encoding="utf-8")',
         "test_astra_uses_real_prompt_file_max_and_fresh_namespace"),
        (script, '"--source", session_name,', '"--source", session_name, "--resume", "builder-seat",',
         "test_astra_uses_real_prompt_file_max_and_fresh_namespace"),
        (script, 'remaining = deadline - time.monotonic()', 'remaining = 1200',
         "test_fallback_shares_the_original_wall_clock_budget"),
        (script, 'if len(body) + len(suffix) > limit:', 'if len(body) > limit:',
         "test_tags_preserve_first_line_and_recount_both_limits"),
        (script, 'return body + suffix', 'return body',
         "test_tags_preserve_first_line_and_recount_both_limits"),
        (script, 'receipts[source_id] = receipt', 'receipts[source_id] = {}',
         "test_receipt_precedes_post_and_records_posted_identity"),
        (script, 'STOP_REQUESTED = True', 'STOP_REQUESTED = False',
         "test_sigterm_drains_current_message_without_advancing_next"),
        (policy, '"effort": "xhigh"', '"effort": "high"',
         "test_policy_and_below_threshold_use_fable_xhigh"),
    )
    with tempfile.TemporaryDirectory(prefix="p58-mutations-") as directory:
        root = Path(directory)
        for relative in (script, policy, tests):
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, destination)
        subprocess.run([sys.executable, "-B", str(root / tests), "-v"], cwd=root, check=True)
        for number, (relative, before, after, test) in enumerate(mutants, 1):
            path = root / relative
            original = path.read_bytes()
            source = original.decode("utf-8")
            if source.count(before) != 1:
                raise RuntimeError(f"mutation_{number}_anchor_not_unique")
            print(f"MUTATION {number}: {relative}: {before} -> {after}; expect {test} to fail", flush=True)
            try:
                changed = source.replace(before, after, 1)
                if relative.endswith(".py"):
                    compile(changed, str(path), "exec")
                else:
                    json.loads(changed)
                path.write_text(changed, encoding="utf-8")
                result = subprocess.run(
                    [sys.executable, "-B", str(root / tests), "ReviewerRoutingTests." + test, "-v"], cwd=root,
                )
                if result.returncode != 1:
                    raise RuntimeError(f"mutation_{number}_not_killed_by_test: exit={result.returncode}")
            finally:
                path.write_bytes(original)
                if path.read_bytes() != original:
                    raise RuntimeError(f"mutation_{number}_restore_failed")
            print(f"MUTATION {number}: KILLED; bytes RESTORED", flush=True)
        subprocess.run([sys.executable, "-B", str(root / tests), "-v"], cwd=root, check=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--mutation-receipt"]:
        mutation_receipt()
    else:
        unittest.main()
