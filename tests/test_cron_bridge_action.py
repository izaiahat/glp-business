"""Cron entrypoint contracts; all actions and Discord calls are isolated."""
import fcntl
import hashlib
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from unittest.mock import patch

from scripts import cron_bridge_action as cron

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("cron_test_bridge", ROOT / "ops/n8n-bridge/bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class CronBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.actions = {"fixture": {"argv": ["/usr/bin/printf", "fixture-ok"], "cwd": str(self.root), "timeout_s": 5, "allowed_args": ["value"]}}
        self.calls = []

    def call(self, path, payload=None):
        self.calls.append((path, payload))
        try:
            bridge.authorize_scope(payload, self.actions, bridge.GLOBAL_AUTH_SCOPE)
            command = bridge.build_command(payload, self.actions)
        except bridge.BadRequest as exc:
            return exc.status, {"error": exc.message}
        value = bridge.run_command(command, "fixture-job")
        return 200, {"job_id": "fixture-job", "status": "done", "rc": value["rc"], "stdout_tail": value["stdout"].decode(), "timed_out": value["timed_out"]}

    def run_action(self, name="fixture", **kwargs):
        return cron.run_action(name, {}, state_dir=self.root, call=self.call, actions=self.actions, notify=lambda *a: {"id": "test", "exact_readback": True}, **kwargs)

    def test_allowlisted_action_runs_with_bridge_validation_timeout_and_receipt(self):
        result = self.run_action()
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["bridge"]["stdout_tail"], "fixture-ok")
        self.assertEqual(json.loads(Path(result["receipt_path"]).read_text())["bridge"]["job_id"], "fixture-job")
        # Argument checking belongs to the existing bridge, not a second allowlist.
        code, _ = self.call("/run", {"action": "script", "name": "fixture", "args": {"unknown": "bad"}})
        self.assertEqual(code, 400)

    def test_registered_repair_keeps_non_natural_receipt_identity(self):
        slot = "2026-09-24T18:11:America/Los_Angeles"
        result = self.run_action(due_slot=slot, invocation="manual_registered_repair")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["run_type"], "manual_registered_repair")
        self.assertEqual(json.loads(Path(result["receipt_path"]).read_text())["run_type"],
                         "manual_registered_repair")
        again = self.run_action(due_slot=slot, invocation="manual_registered_repair")
        self.assertEqual(again["status"], "SKIPPED_OBSERVED_SLOT")
        self.assertEqual(len([p for p, _ in self.calls if p == "/run"]), 1)

    def test_liveness_unsafe_completed_slot_gets_one_pinned_postrepair_run(self):
        action = "program_liveness_check"
        self.actions[action] = self.actions["fixture"]
        slot = "2026-09-21T07:00:America/Los_Angeles"
        digest = hashlib.sha256(json.dumps({"action": "script", "name": action, "args": {}},
                                           sort_keys=True).encode()).hexdigest()
        artifact = self.root / "unsafe-liveness.json"
        artifact.write_text(json.dumps({"tracking_probe_clicks": 52}))
        prior_path = self.root / "unsafe-receipt.json"
        prior_path.write_text(json.dumps({"status": "PASS", "rc": 0,
            "run_type": "manual_registered", "due_slot": slot,
            "payload_sha256": digest, "bridge": {"status": "done", "rc": 0},
            "effects": {"heartbeat": {"data": {"rows": [{"machine": "program_liveness",
                "note": "probe_clicks=52", "receipt": str(artifact)}]}}}}))
        (self.root / (action + ".state.json")).write_text(json.dumps({
            "slots": {slot: {"rc": 0, "receipt_path": str(prior_path)}},
            "last_receipt": str(prior_path)}))
        first = self.run_action(action, due_slot=slot, invocation="manual_registered",
                                finish=lambda *_: {})
        self.assertEqual((first["status"], first["run_type"]),
                         ("PASS", "manual_registered_repair"))
        self.assertEqual(first["due_slot"], slot + "|postrepair")
        second = self.run_action(action, due_slot=slot, invocation="manual_registered",
                                 finish=lambda *_: {})
        self.assertEqual(second["status"], "SKIPPED_OBSERVED_SLOT")
        self.assertEqual(len([p for p, _ in self.calls if p == "/run"]), 1)

    def test_unknown_action_refused_before_submission(self):
        result = self.run_action("not-allowlisted")
        self.assertNotEqual(result["rc"], 0)
        self.assertEqual(result["status"], "REFUSED_UNKNOWN_ACTION")
        self.assertEqual(self.calls, [])

    def test_onboarding_scheduled_scan_preserves_card_and_heartbeat(self):
        for candidates in ([], ["row1:fixture"]):
            with self.subTest(candidates=candidates):
                summary = {"machine": "onboarding_manifest", "mode": "scan", "status": "PASS",
                           "receipt": "fixture-scan", "output_count": len(candidates),
                           "onboarding_candidates": candidates, "removal_candidates": [],
                           "scan": {"transitions": 1, "sheet_rows": 8, "baseline": False},
                           "discord_text": "fixture scan card"}
                job = {"job_id": "fixture-scan", "finished_utc": "2026-09-24T05:00:00Z",
                       "stdout_tail": json.dumps(summary)}
                with patch.object(cron, "heartbeat", return_value={}) as hb, \
                     patch.object(cron, "post_once", return_value={}) as post:
                    effects = cron.finish_action("onboarding_manifest_run", {"mode": "scan"},
                                                 job, {}, self.root / "onboarding.state.json")
                self.assertEqual(hb.call_count, 1)
                self.assertEqual(effects["machine_status"], "PASS")
                self.assertEqual(post.call_count, bool(candidates))
                self.assertEqual(hb.call_args.args[:4], ("onboarding_manifest", len(candidates),
                    "fixture-scan", f"scan: {len(candidates)} candidate(s), 1 changed row(s) of 8"))
        with patch.object(cron, "heartbeat") as hb, patch.object(cron, "post_once") as post:
            job["stdout_tail"] = json.dumps({**summary, "authoring": {"dispatched": True}})
            with self.assertRaisesRegex(RuntimeError, "onboarding_schedule_unexpected_authoring"):
                cron.finish_action("onboarding_manifest_run", {"mode": "scan"}, job, {}, self.root / "s.json")
            hb.assert_not_called()
            post.assert_not_called()
        result = cron.run_action("onboarding_manifest_run", {"mode": "onboard", "provider": "fixture"},
            state_dir=self.root, actions={"onboarding_manifest_run": {}}, call=self.call)
        self.assertEqual(result["status"], "REFUSED_ONBOARDING_CRON_SCAN_ONLY")
        self.assertEqual(self.calls, [])

    def test_notification_trailing_newline_matches_sender_and_reconciles_existing_id(self):
        content = "x" * 1899 + "\nmore receipt text"
        job = {"job_id": "pricing-known"}
        key = hashlib.sha256((job["job_id"] + content).encode()).hexdigest()
        state = {"deliveries": {key: {"id": "existing", "status": "sending"}}}
        with patch("scripts.post_discord_message.send") as send, \
             patch("scripts.post_discord_message.readback", return_value={
                 "channel_id": cron.CHANNEL, "content": "x" * 1899}) as get:
            result = cron.post_once(content, job, state, self.root / "notification.json")
            self.assertEqual(result["status"], "verified")
            send.assert_not_called()
            get.assert_called_once_with(cron.CHANNEL, "existing")
        with patch("scripts.post_discord_message.send", return_value={"id": "new"}) as send, \
             patch("scripts.post_discord_message.readback", return_value={
                 "channel_id": cron.CHANNEL, "content": "x" * 1899}):
            cron.post_once(content, job, {}, self.root / "new-notification.json")
            send.assert_called_once_with(cron.CHANNEL, "x" * 1899)

    def test_topic_sequence_reconciles_score_without_reexecution(self):
        job = {"job_id": "mint-1", "finished_utc": "2026-09-24T01:00:00Z",
               "receipt_path": "mint-receipt", "stdout_tail": '{"gaps": 8, "status": "PASS"}'}
        score = {"job_id": "score-1", "finished_utc": job["finished_utc"],
                 "receipt_path": "score-receipt", "rc": 0,
                 "stdout_tail": '{"multipliers_written": 4, "top_pick": "test"}'}
        state_path = self.root / "topic_supply_mint.state.json"
        state = {}
        def score_run(action, args, **kwargs):
            directory = kwargs["state_dir"]
            receipt = directory / "score.json"
            cron.save(receipt, {"rc": 0, "bridge": score})
            cron.save(directory / "topic_data_score.state.json", {"last_receipt": str(receipt)})
            return {"rc": 0, "bridge": score}
        with patch.object(cron, "run_action", side_effect=score_run) as run, patch.object(cron, "heartbeat", return_value={}) as hb:
            first = cron.finish_action("topic_supply_mint", {}, job, state, state_path)
            second = cron.finish_action("topic_supply_mint", {}, job, state, state_path)
        self.assertEqual(first["scored"], 4)
        self.assertEqual(second["scored"], 4)
        self.assertEqual(run.call_count, 1)
        self.assertEqual([c.args[0] for c in hb.call_args_list], ["topic_supply", "topic_data_score"] * 2)

    def test_graph_terminal_semantics_are_not_rc_only_success(self):
        state_path = self.root / "graph.state.json"
        cases = [
            ("analytics_brain", {"status": "PRELAUNCH_INACTIVE", "output_count": 0}, "gsc_ga4_collector", "status", "FAIL_COLLECTION"),
            ("article_carrier_consume", {"schema": "n8n.article_carrier.v1", "status": "PASS_DRYRUN"}, "article_carrier", "status", "PASS_NOTHING_DUE"),
            ("glp_weekly_seo_control", {"machine": "glp_weekly_seo_control", "status": "PASS", "clicks": 2}, "glp_weekly_seo_control", "status", "FAIL"),
            ("pricing_refresh", {"schema": "glp.pricing_refresh.v1", "row_verdicts": {"unchanged": 2}}, "pricing_refresh", "row_verdicts", {}),
            ("error_fixer_daily_review", {"machine": "error_fixer_daily_review", "status": "PASS", "dispatches": 1}, "error_fixer_daily_review", "machine", "foreign_machine"),
        ]
        for action, receipt, machine, bad_key, bad_value in cases:
            with self.subTest(action=action), patch.object(cron, "heartbeat", return_value={}) as hb, patch.object(cron, "post_once", return_value={}):
                job = {"job_id": "test", "finished_utc": "2026-09-24T01:00:00Z", "receipt_path": "fixture", "stdout_tail": json.dumps(receipt)}
                cron.finish_action(action, {}, job, {}, state_path)
                self.assertEqual(hb.call_args.args[0], machine)
                hb.reset_mock()
                job["stdout_tail"] = json.dumps({**receipt, bad_key: bad_value})
                with self.assertRaises(RuntimeError):
                    cron.finish_action(action, {}, job, {}, state_path)
                hb.assert_not_called()

    def test_pending_readback_cannot_rebind_to_another_job(self):
        digest = hashlib.sha256(json.dumps({"action": "script", "name": "fixture", "args": {}}, sort_keys=True).encode()).hexdigest()
        cron.save(self.root / "fixture.state.json", {"pending": {"job_id": "original-job", "payload_sha256": digest, "receipt_path": "original-receipt"}})
        result = cron.run_action("fixture", {}, state_dir=self.root, actions=self.actions,
            call=lambda *a: (200, {"job_id": "foreign-job", "status": "done", "rc": 0}),
            notify=lambda *a: {"exact_readback": True})
        self.assertNotEqual(result["rc"], 0)
        self.assertEqual(json.loads((self.root / "fixture.state.json").read_text())["pending"]["job_id"], "original-job")

    def test_error_terminal_with_zero_rc_is_not_success(self):
        finished = []
        result = cron.run_action("fixture", {}, state_dir=self.root, actions=self.actions,
            call=lambda *a: (200, {"job_id": "error-job", "status": "error", "rc": 0}),
            finish=lambda *a: finished.append(True), notify=lambda *a: {"exact_readback": True})
        self.assertEqual(result["status"], "FAIL")
        self.assertNotEqual(result["rc"], 0)
        self.assertEqual(finished, [])
        self.assertEqual(result["bridge"]["status"], "error")

    def test_repeated_dst_slot_submits_only_once(self):
        action_path = self.root / "actions.json"
        action_path.write_text(json.dumps(self.actions))
        real_run = cron.run_action
        def isolated(action, args, **kwargs):
            return real_run(action, args, state_dir=self.root, actions=self.actions,
                call=self.call, notify=lambda *a: {}, **kwargs)
        with patch.object(cron, "run_action", side_effect=isolated), patch.object(cron, "datetime") as clock, patch("sys.argv", ["cron_bridge_action", "fixture", "--scheduled", "--cron", "0 1 * * *"]), patch("builtins.print"):
            sample = datetime(2026, 11, 1, 1, 0, tzinfo=ZoneInfo("America/Los_Angeles"), fold=0)
            clock.now.side_effect = lambda tz: sample if isinstance(tz, ZoneInfo) else datetime.now(tz)
            self.assertEqual(cron.main(), 0)
            sample = sample.replace(fold=1)
            self.assertEqual(cron.main(), 0)
        self.assertEqual(len(self.calls), 1)

    def test_phase_failures_do_not_block_or_clear_other_phases(self):
        alerts = []
        def call(path, payload=None):
            if payload["args"]["phase"] == "daily":
                return 202, {"job_id": "daily-job", "status": "running"}
            return 200, {"job_id": "hourly-job", "status": "done", "rc": 0}
        kwargs = dict(state_dir=self.root, actions=self.actions, call=call,
                      notify=lambda *a: alerts.append(a) or {"exact_readback": True})
        with patch.object(cron.time, "monotonic", side_effect=[0, 1000]):
            daily = cron.run_action("fixture", {"phase": "daily"}, **kwargs)
        hourly = cron.run_action("fixture", {"phase": "hourly"}, **kwargs)
        self.assertNotEqual(daily["rc"], 0)
        self.assertEqual(hourly["rc"], 0)
        self.assertEqual(len(alerts), 1)
        # A successful hourly result cannot erase an unresolved daily job.
        self.assertIn("daily-job", "".join(p.read_text() for p in self.root.rglob("*.state.json")))
        common = json.loads((self.root / "fixture.state.json").read_text())
        self.assertTrue(common["failed_branches"])
        self.assertTrue(common["alert"]["exact_readback"])

    def test_carrier_refusal_is_terminal_without_becoming_publication_success(self):
        calls = []
        def call(path, payload=None):
            calls.append(path)
            return 200, {"job_id": "carrier-" + str(len(calls)), "status": "done", "rc": 0,
                "finished_utc": "2026-09-24T18:00:00Z", "receipt_path": "fixture-receipt",
                "stdout_tail": json.dumps({"schema": "n8n.article_carrier.v1",
                    "status": "BLOCKED_BASE_COMPATIBILITY", "published": False})}
        with patch.object(cron, "heartbeat") as hb, patch.object(cron, "article_release_live", return_value=True):
            for slot in ("2026-09-24T11:00:America/Los_Angeles", "2026-09-24T11:00:America/Los_Angeles", "2026-09-25T11:00:America/Los_Angeles"):
                result = cron.run_action("article_carrier_consume", {}, state_dir=self.root,
                    actions={"article_carrier_consume": {"timeout_s": 1}}, call=call,
                    notify=lambda *a: {}, due_slot=slot)
                self.assertNotEqual(result["rc"], 0)
                self.assertIn(result["status"], ("FAIL", "SKIPPED_OBSERVED_SLOT"))
            hb.assert_not_called()
        self.assertEqual(calls, ["/run", "/run"])
        state = json.loads((self.root / "article_carrier_consume.state.json").read_text())
        self.assertNotIn("pending", state)
        self.assertEqual(result["machine_status"], "BLOCKED_BASE_COMPATIBILITY")
        uncertain = {"job_id": "uncertain", "status": "done", "rc": 0,
            "stdout_tail": json.dumps({"schema": "n8n.article_carrier.v1", "status": "BLOCKED_CONSUME_FAILED"})}
        with patch.object(cron, "article_release_live", return_value=True):
            cron.run_action("article_carrier_consume", {}, state_dir=self.root,
                actions={"article_carrier_consume": {"timeout_s": 1}},
                call=lambda *a: (200, uncertain), notify=lambda *a: {},
                due_slot="2026-09-28T11:00:America/Los_Angeles")
        state = json.loads((self.root / "article_carrier_consume.state.json").read_text())
        self.assertEqual(state["pending"]["job_id"], "uncertain")

    def test_carrier_cron_declines_before_glp40_release_without_bridge_post(self):
        calls = []
        slot = "2026-09-24T11:00:America/Los_Angeles"
        with patch.object(cron, "article_release_live", return_value=False):
            result = cron.run_action("article_carrier_consume", {"mode": "consume"},
                state_dir=self.root, actions={"article_carrier_consume": {}},
                call=lambda *a: calls.append(a), due_slot=slot, invocation="manual_registered")
        self.assertEqual(result["status"], "DECLINED_RELEASE_HOLD")
        self.assertEqual(result["rc"], 0)
        self.assertEqual(calls, [])
        state = json.loads(next(self.root.glob("article_carrier_consume-*.state.json")).read_text())
        self.assertEqual(state["slots"][slot]["receipt_path"], result["receipt_path"])

    def test_carrier_release_requires_completed_receipt_matching_live_head(self):
        from scripts import linear_client
        path = self.root / "reports/ops/orchestration-restore-20260907/PLAN/K10/EXECUTION/K10-009-C/RESULT.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"issue": "GLP-43", "status": "COMPLETE",
            "acceptance_clauses": [{"verdict": "PASS"}],
            "production_deployment_id": "approved-deployment"}))
        with patch.object(cron, "ROOT", self.root), \
                patch.object(linear_client, "gql", return_value={"issue": {"state": {"type": "completed"}}}), \
                patch.object(cron, "article_production_head_id", return_value="approved-deployment") as head:
            self.assertTrue(cron.article_release_live())
            head.return_value = "rolled-back-deployment"
            self.assertFalse(cron.article_release_live())
            path.write_text(json.dumps({"issue": "GLP-43", "status": "COMPLETE",
                "acceptance_clauses": [{"verdict": "PASS"}]}))
            self.assertFalse(cron.article_release_live())

    def test_overlapping_action_skipped_by_lock(self):
        lock = self.root / "fixture.lock"
        with lock.open("w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_action()
        self.assertEqual(result["status"], "SKIPPED_OVERLAP")
        self.assertEqual(self.calls, [])

    def test_heartbeat_uses_real_registered_key_and_reconciles_repeat(self):
        from ops import n8n_dt as dt
        row = {"machine": "fixture", "last_success_utc": "2026-09-24T01:00:00Z",
               "output_count": 0, "receipt": "fixture-receipt", "note": "idle"}
        job = {"finished_utc": row["last_success_utc"]}
        for native in (False, True):
            with self.subTest(native=native), patch.object(dt, "_datatable_native_first", return_value=native), \
                    patch.object(dt, "_preflight_rows", return_value=[row]), \
                    patch.object(dt, "native_rows_for_key_tuples", return_value={(row["receipt"],): row}), \
                    patch.object(dt, "call", side_effect=AssertionError("unexpected mutation")), \
                    patch.object(dt, "_native_datatable_call", side_effect=AssertionError("unexpected mutation")):
                for _ in range(2):
                    result = cron.heartbeat("fixture", 0, row["receipt"], "idle", job)
                    self.assertEqual(result["write_status"], "already_present")

    def test_terminal_semantic_refusal_does_not_capture_future_slots(self):
        cases = {
            "analytics_brain": {"status": "FAIL_COLLECTION"},
            "glp_weekly_seo_control": {"machine": "glp_weekly_seo_control", "status": "FAIL"},
            "pricing_refresh": {"schema": "glp.pricing_refresh.v1", "row_verdicts": {}},
        }
        for action, summary in cases.items():
            with self.subTest(action=action):
                calls = []
                def call(path, payload=None):
                    calls.append(path)
                    return 200, {"job_id": f"job-{len(calls)}", "status": "done", "rc": 0,
                                 "stdout_tail": json.dumps(summary)}
                for day in (24, 24, 25, 26):
                    result = cron.run_action(action, {}, state_dir=self.root, actions={action: {}},
                        call=call, notify=lambda *a: {}, due_slot=f"2026-09-{day}T09:17:America/Los_Angeles")
                    self.assertNotEqual(result["rc"], 0)
                self.assertEqual(calls, ["/run"] * 3)
                self.assertNotIn("pending", json.loads((self.root / f"{action}.state.json").read_text()))

    def test_reconciling_old_job_also_executes_current_due_slot(self):
        calls = []
        def call(path, payload=None):
            calls.append(path)
            if len(calls) == 1:
                return 202, {"job_id": "old", "status": "running"}
            return 200, {"job_id": "old" if path == "/job/old" else "new", "status": "done", "rc": 0}
        kwargs = dict(state_dir=self.root, actions=self.actions, call=call, notify=lambda *a: {})
        old = "2026-09-24T09:17:America/Los_Angeles"
        current = "2026-09-25T09:17:America/Los_Angeles"
        with patch.object(cron.time, "monotonic", side_effect=[0, 1000]):
            self.assertNotEqual(cron.run_action("fixture", {}, due_slot=old, **kwargs)["rc"], 0)
        result = cron.run_action("fixture", {}, due_slot=current, **kwargs)
        self.assertEqual(result["rc"], 0)
        self.assertEqual(result["due_slot"], current)
        self.assertEqual(calls, ["/run", "/job/old", "/run"])
        self.assertIn(current, json.loads((self.root / "fixture.state.json").read_text())["slots"])
        cron.run_action("fixture", {}, due_slot=current, **kwargs)
        self.assertEqual(calls, ["/run", "/job/old", "/run"])

    def test_failure_alert_recovers_known_ack_by_get_without_resend(self):
        from scripts import post_discord_message as discord
        def call(*a):
            return 200, {"job_id": "failed", "status": "done", "rc": 1}
        content = []
        def send(channel, text):
            content.append(text)
            return {"id": "acknowledged"}
        attempts = []
        def readback(channel, message_id):
            attempts.append(message_id)
            if len(attempts) == 1:
                raise TimeoutError("readback unavailable")
            return {"id": message_id, "channel_id": channel, "content": content[0]}
        with patch.object(discord, "send", side_effect=send) as sent, patch.object(discord, "readback", side_effect=readback):
            for _ in range(3):
                cron.run_action("fixture", {}, state_dir=self.root, actions=self.actions, call=call)
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(attempts, ["acknowledged", "acknowledged"])
        alert = json.loads((self.root / "fixture.state.json").read_text())["alert"]
        self.assertEqual(alert["id"], "acknowledged")
        self.assertTrue(alert["exact_readback"])
        with patch.object(discord, "send", side_effect=TimeoutError("POST ack unknown")) as sent, patch.object(discord, "readback") as fetched:
            for _ in range(2):
                cron.run_action("fixture", {}, state_dir=self.root / "unknown-ack", actions=self.actions, call=call)
        self.assertEqual(sent.call_count, 1)
        fetched.assert_not_called()

    def test_killed_terminal_branches_preserve_diagnostic_heartbeat(self):
        for action in ("glp_weekly_seo_control", "error_fixer_daily_review"):
            for rc, termination, expected in ((-15, {}, True), (-9, {"classification": "KILLED"}, True), (-9, {}, False), (1, {}, False)):
                with self.subTest(action=action, rc=rc, termination=termination), patch.object(cron, "heartbeat", return_value={}) as hb:
                    result = cron.run_action(action, {}, state_dir=self.root, actions={action: {}},
                        call=lambda *a: (200, {"job_id": "killed", "status": "error", "rc": rc,
                            "termination": termination, "finished_utc": "2026-09-24T01:00:00Z", "receipt_path": "killed-receipt"}),
                        notify=lambda *a: {})
                    self.assertNotEqual(result["rc"], 0)
                    self.assertEqual(hb.call_count, int(expected))
                    if expected:
                        self.assertEqual(hb.call_args.args[:3], (action, 0, "killed-receipt"))
                        self.assertEqual(result["effects"]["machine_status"], "killed_retryable")
                    self.assertNotIn("pending", json.loads((self.root / f"{action}.state.json").read_text()))

    def test_interrupted_or_incomplete_terminal_envelope_preserves_pending(self):
        envelopes = [
            {"status": "error", "rc": 125, "interrupted": True,
             "termination": {"classification": "BRIDGE_INTERRUPTED"}},
            {"status": "error", "rc": 125, "termination": {"classification": "BRIDGE_INTERRUPTED"}},
            {"status": "error", "interrupted": True},
            {"status": "done"},
        ]
        for action in ("analytics_brain", "article_carrier_consume"):
            for index, envelope in enumerate(envelopes):
                with self.subTest(action=action, envelope=envelope), patch.object(cron, "article_release_live", return_value=True):
                    directory = self.root / f"{action}-{index}"
                    calls = []
                    def call(path, payload=None):
                        calls.append(path)
                        if path == "/run" and len(calls) == 1:
                            return 202, {"job_id": "old", "status": "running"}
                        return 200, {"job_id": "old", **envelope}
                    kwargs = dict(state_dir=directory, actions={action: {}}, call=call, notify=lambda *a: {})
                    with patch.object(cron.time, "monotonic", side_effect=[0, 1000]):
                        cron.run_action(action, {}, due_slot="2026-09-24T09:17:America/Los_Angeles", **kwargs)
                    for day in (25, 26):
                        result = cron.run_action(action, {}, due_slot=f"2026-09-{day}T09:17:America/Los_Angeles", **kwargs)
                        self.assertEqual(result["rc"], 125)
                    self.assertEqual(calls, ["/run", "/job/old", "/job/old"])
                    state = json.loads((directory / f"{action}.state.json").read_text())
                    self.assertEqual(state["pending"]["job_id"], "old")
                    self.assertEqual(state.get("slots", {}), {})

    def test_unverified_alert_survives_action_recovery_and_new_streak(self):
        from scripts import post_discord_message as discord
        for failures_before_readback in (1, 3):
            directory = self.root / str(failures_before_readback)
            messages, reads = {}, []
            def send(channel, text):
                mid = str(len(messages) + 1)
                messages[mid] = text
                return {"id": mid}
            def readback(channel, mid):
                reads.append(mid)
                if mid == "1" and reads.count(mid) <= failures_before_readback:
                    raise TimeoutError("GET unavailable")
                return {"id": mid, "channel_id": channel, "content": messages[mid]}
            outcomes = iter([1, 0, 1, 0, 0])
            def call(*a):
                return 200, {"job_id": "job", "status": "done", "rc": next(outcomes)}
            with self.subTest(delayed_gets=failures_before_readback), patch.object(discord, "send", side_effect=send), patch.object(discord, "readback", side_effect=readback):
                for i in range(5):
                    cron.run_action("fixture", {}, state_dir=directory, actions=self.actions, call=call)
                    state = json.loads((directory / "fixture.state.json").read_text())
                    if i == 1 and failures_before_readback == 3:
                        self.assertEqual(state["unverified_alerts"][0]["id"], "1")
                self.assertEqual(len(messages), 2)
                self.assertEqual(reads.count("1"), failures_before_readback + 1)
                self.assertEqual(reads.count("2"), 1)
                self.assertFalse(state.get("unverified_alerts"))
                self.assertNotIn("alert", state)

    def test_failure_alert_deduplicated_until_success(self):
        calls = []
        def notify(action, receipt, *_):
            calls.append((action, receipt))
            return {"id": str(len(calls)), "exact_readback": True}
        outcomes = iter([1, 1, 0, 1])
        def failing(path, payload=None):
            return 200, {"job_id": "test-job", "status": "done", "rc": next(outcomes)}
        for _ in range(4):
            cron.run_action("fixture", {}, state_dir=self.root, call=failing, actions=self.actions, notify=notify)
        self.assertEqual(len(calls), 2)
        self.assertTrue(json.loads((self.root / "fixture.state.json").read_text())["alert"]["exact_readback"])


class SheetCronTests(unittest.TestCase):
    def test_drain_graph_quiet_contention_and_ack_recovery(self):
        from ops import n8n_dt
        for mode in ("quiet", "applied", "contended", "ack-recovery"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                original = {"update_id": "u1", "kind": "fixture", "status": "queued", "note": "original", "program_name": "Fixture"}
                rows = [] if mode == "quiet" else [original.copy()]
                posts = []
                updates = []
                ack_attempts = []
                summary = {"schema": "glp.sheet_sync_drain.v1", "queue_rows": 1,
                    "applied": 1, "noop": 0, "refused": 0, "escalated": 0, "deferred": 0,
                    "cells_written": 2, "receipt": "sheet-receipt", "results": [
                        {"update_id": "u1", "kind": "fixture", "status": "applied", "cells_written": 2}]}
                if mode == "contended":
                    summary.update(lock_contended=True, applied=0, cells_written=0, results=[])
                job = {"job_id": "sheet-job", "status": "done", "rc": 75 if mode == "contended" else 0,
                    "receipt_path": "bridge-receipt", "finished_utc": "2026-09-24T03:20:00Z", "stdout_tail": json.dumps(summary)}
                def call(path, payload=None):
                    if payload:
                        posts.append(payload)
                    return 200, job
                def read(table, filt=None, *a, **kw):
                    return [r.copy() for r in rows]
                def update(table, filt, data):
                    ack_attempts.append(data)
                    if mode == "ack-recovery" and len(ack_attempts) == 1:
                        raise TimeoutError("ack transport")
                    rows[0].update(data)
                    updates.append((filt, data))
                    return {"status": "confirmed"}
                actions = {"sheet_sync_drain": {"timeout_s": 5}}
                with patch.object(n8n_dt, "rows", side_effect=read), patch.object(n8n_dt, "update", side_effect=update), patch.object(cron, "heartbeat", return_value={}) as hb, patch.object(cron, "post_once", return_value={}) as card:
                    result = cron.run_action("sheet_sync_drain", {}, state_dir=directory, actions=actions, call=call, notify=lambda *a: {"exact_readback": True})
                    if mode == "ack-recovery":
                        self.assertNotEqual(result["rc"], 0)
                        result = cron.run_action("sheet_sync_drain", {}, state_dir=directory, actions=actions, call=call, notify=lambda *a: {"exact_readback": True})
                    self.assertEqual(result["rc"], 0)
                    self.assertEqual(len(posts), int(mode != "quiet"))
                    self.assertEqual(len(updates), int(mode in ("applied", "ack-recovery")))
                    self.assertEqual(hb.call_args.args[0], "sheet_sync")
                    self.assertEqual(card.call_count, int(mode in ("applied", "ack-recovery")))
                    if posts:
                        self.assertEqual(posts[0]["args"]["apply"], "true")
                        snapshot = json.loads(posts[0]["args"]["queue-json"])
                        self.assertEqual(snapshot[0]["update_id"], "u1")
                    if mode == "contended":
                        self.assertEqual(rows[0]["status"], "queued")
                        self.assertEqual(result["bridge"]["rc"], 75)
                    if updates:
                        self.assertEqual(updates[0][0], n8n_dt.eq("update_id", "u1"))
                        self.assertIn("original", rows[0]["note"])
                self.assertFalse(json.loads((directory / "sheet_sync_drain.state.json").read_text()).get("pending"))

    def test_reconciler_inserts_queue_before_report_and_heartbeat(self):
        from ops import n8n_dt
        with tempfile.TemporaryDirectory() as tmp:
            row = {"update_id": "q1", "kind": "fixture", "status": "queued"}
            summary = {"schema": "glp.sheet_vs_site_reconcile.v1", "queue_rows": [row],
                "rows_audited": 4, "class_counts": {"CONSISTENT": 3, "FIX": 1}, "receipt": "reconcile", "discord_text": "fixture report"}
            job = {"job_id": "reconcile-job", "rc": 0, "receipt_path": "bridge", "finished_utc": "2026-09-24T03:20:00Z", "stdout_tail": json.dumps(summary)}
            order = []
            def insert(table, value):
                order.append("queue")
                self.assertEqual(value["update_id"], "q1")
                return {}
            with patch.object(n8n_dt, "insert", side_effect=insert), patch.object(cron, "post_once", side_effect=lambda *a: order.append("card")), patch.object(cron, "heartbeat", side_effect=lambda *a: order.append("heartbeat")):
                effects = cron.finish_action("sheet_vs_site_reconcile", {}, job, {}, Path(tmp) / "state.json")
            self.assertEqual(order, ["queue", "card", "heartbeat"])
            self.assertEqual(effects["rows_audited"], 4)


class SheetContinuationRegressionTests(unittest.TestCase):
    """Real registered Data Table helpers; only native HTTP is synthetic."""

    def setUp(self):
        import copy
        import os
        from urllib.parse import urlsplit, parse_qs
        from ops import n8n_dt
        self.dt = n8n_dt
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.tables = {cron.SHEET_QUEUE: [], cron.HEARTBEATS: []}
        self.writes = []
        def http(method, path, body=None, **kwargs):
            url = urlsplit(path)
            table = url.path.split('/')[2]
            rows = self.tables[table]
            if method == 'GET':
                query = parse_qs(url.query)
                filt = json.loads(query.get('filter', ['{}'])[0])
                selected = [row for row in rows if n8n_dt._native_row_matches(row, filt)]
                start = int(query.get('cursor', ['0'])[0])
                limit = int(query.get('limit', ['100'])[0])
                return {'data': copy.deepcopy(selected[start:start+limit]),
                        'nextCursor': str(start+limit) if start+limit < len(selected) else None}
            self.writes.append((method, table))
            if method == 'POST':
                data = copy.deepcopy(body['data'])
                if table == cron.HEARTBEATS:
                    for row in data:
                        count = row.get('output_count')
                        if isinstance(count, float) and count.is_integer():
                            row['output_count'] = int(count)
                rows.extend(data)
                return {'data': copy.deepcopy(data)}
            if method == 'PATCH':
                matched = [row for row in rows if n8n_dt._native_row_matches(row, body['filter'])]
                for row in matched:
                    row.update(body['data'])
                return {'data': copy.deepcopy(matched)}
            raise AssertionError('unexpected transport')
        for context in (patch.dict(os.environ, {'N8N_DT_DATATABLE_TRANSPORT': 'native'}),
                        patch.object(n8n_dt, 'last_read_truncated', False),
                        patch.object(n8n_dt, '_native_http', side_effect=http),
                        patch.object(n8n_dt, 'call', side_effect=AssertionError('live MCP forbidden')),
                        patch.object(cron, 'post_once', return_value={})):
            context.start()
            self.addCleanup(context.stop)

    def row(self, uid='u1'):
        return dict(update_id=uid, kind='application_pending', network='Fixture',
                    program_id=uid, program_name='Synthetic', evidence='{}', status='queued',
                    queued_by='fixture', queued_at_utc='2026-09-24T00:00:00Z',
                    note='original', resolved_at_utc='')

    def run_action(self, action, call):
        return cron.run_action(action, {}, state_dir=self.directory,
                               actions={action: {'timeout_s': 1}}, call=call,
                               notify=lambda *a: {'exact_readback': True})

    def test_decisions_clean_cycles_real_heartbeat_and_lost_ack_recovery(self):
        calls = []
        jobs = {}
        clock = ['2026-09-24T12:00:00Z']
        def bridge(path, payload=None):
            calls.append(path)
            if path.startswith('/job/'):
                return 200, jobs[path.split('/')[-1]]
            number = len(jobs) + 1
            summary = dict(status='complete', mode='live', run_id='live-clean-100',
                sink='decisions', item_count=0, unroutable_count=0,
                source_message_ids=[], post_required=False, confirmation_card='')
            if number == 3:
                summary.update(run_id='live-new-message', item_count=1,
                    source_message_ids=['101'], confirmation_card='new card',
                    confirmation=dict(discord_message_id='101', sink='decisions',
                        channel_id=cron.DECISIONS_CHANNEL,
                        checks=dict(id=True, channel=True, content=True)))
            job = dict(job_id=f'live-{number}', status='done', rc=0,
                finished_utc=clock[0], stdout_tail=json.dumps(summary))
            jobs[job['job_id']] = job
            return 200, job
        original_insert = self.dt.insert
        def lost_ack(*args, **kwargs):
            original_insert(*args, **kwargs)
            raise TimeoutError('synthetic lost local acknowledgement')
        with patch.object(cron, 'utc_now', side_effect=lambda: clock[0]), patch(
                'scripts.post_discord_message.readback', return_value=dict(
                    id='101', channel_id=cron.DECISIONS_CHANNEL, content='new card')):
            first = self.run_action('decisions_router', bridge)
            clock[0] = '2026-09-24T12:15:00Z'
            with patch.object(self.dt, 'insert', side_effect=lost_ack):
                second = self.run_action('decisions_router', bridge)
            clock[0] = '2026-09-24T12:16:00Z'
            recovered = self.run_action('decisions_router', bridge)
            clock[0] = '2026-09-24T12:30:00Z'
            third = self.run_action('decisions_router', bridge)
        self.assertEqual([r['rc'] for r in (first, second, recovered, third)], [0, 125, 0, 0])
        self.assertEqual(calls, ['/run', '/run', '/job/live-2', '/run'])
        rows = self.tables[cron.HEARTBEATS]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r['receipt'] for r in rows}), 3)
        self.assertEqual(rows[1]['last_success_utc'], '2026-09-24T12:15:00Z')
        self.assertEqual(third['effects']['run_id'], 'live-new-message')

    def test_decisions_heartbeat_completion_time_persists_across_ack_retry(self):
        calls = []
        jobs = {}
        summary = dict(status='complete', mode='live', run_id='live-posted',
            sink='decisions', item_count=1, unroutable_count=0,
            source_message_ids=['101'], post_required=True, confirmation_card='card')
        def bridge(path, payload=None):
            calls.append(path)
            if path.startswith('/job/'):
                return 200, jobs[path.split('/')[-1]]
            mode = payload['args']['mode']
            result = dict(summary)
            if mode == 'confirm':
                result.update(post_required=False, confirmation=dict(
                    discord_message_id='101', sink='decisions',
                    channel_id=cron.DECISIONS_CHANNEL,
                    checks=dict(id=True, channel=True, content=True)))
            job = dict(job_id=mode, status='done', rc=0,
                finished_utc='2026-09-24T12:07:00Z' if mode == 'confirm' else '2026-09-24T12:00:00Z',
                stdout_tail=json.dumps(result))
            jobs[mode] = job
            return 200, job
        original_insert = self.dt.insert
        def lost_ack(*args, **kwargs):
            original_insert(*args, **kwargs)
            raise TimeoutError('synthetic lost local acknowledgement')
        with patch.object(cron, 'post_once', return_value={'id': '101'}), patch(
                'scripts.post_discord_message.readback', return_value=dict(
                    id='101', channel_id=cron.DECISIONS_CHANNEL, content='card')):
            with patch.object(cron, 'utc_now', return_value='2026-09-24T12:08:00Z'), patch.object(
                    self.dt, 'insert', side_effect=lost_ack):
                first = self.run_action('decisions_router', bridge)
            with patch.object(cron, 'utc_now', return_value='2026-09-24T12:20:00Z'):
                second = self.run_action('decisions_router', bridge)
        self.assertEqual([first['rc'], second['rc']], [125, 0])
        self.assertEqual(calls, ['/run', '/run', '/job/live', '/job/confirm'])
        self.assertEqual(len(self.tables[cron.HEARTBEATS]), 1)
        self.assertEqual(self.tables[cron.HEARTBEATS][0]['last_success_utc'], '2026-09-24T12:08:00Z')

    def test_c1_real_pagination_selects_150_and_never_false_empty(self):
        self.tables[cron.SHEET_QUEUE] = [self.row(str(i)) for i in range(151)]
        self.assertEqual(len(cron.queued_sheet_batch()), 150)
        self.tables[cron.SHEET_QUEUE] = [dict(update_id=str(i), status='queued') for i in range(100)] + [self.row()]
        self.assertEqual([row['update_id'] for row in cron.queued_sheet_batch()], ['u1'])
        self.tables[cron.SHEET_QUEUE] = [dict(update_id=str(i), status='queued') for i in range(5001)]
        with self.assertRaisesRegex(RuntimeError, 'sheet_queue_scan_incomplete'):
            cron.queued_sheet_batch()

    def test_natural_heartbeat_integer_readback_reconciles_float_count(self):
        job = {'finished_utc': '2026-09-24T03:27:12Z'}
        cron.heartbeat('gsc_ga4_collector', 4.0, 'natural-fixture', 'outputs=4.0', job)
        cron.heartbeat('gsc_ga4_collector', 4.0, 'natural-fixture', 'outputs=4.0', job)
        self.assertEqual(self.writes.count(('POST', cron.HEARTBEATS)), 1)

    def test_c2_terminal_producer_ack_settles_without_overwrite_or_new_drain(self):
        original = self.row()
        self.tables[cron.SHEET_QUEUE] = [original.copy()]
        summary = dict(schema='glp.sheet_sync_drain.v1', results=[dict(update_id='u1', kind=original['kind'], status='applied')], applied=1)
        calls = []
        def bridge(path, payload=None):
            calls.append(path)
            self.dt.update(cron.SHEET_QUEUE, self.dt.eq('update_id','u1'),
                           dict(status='applied', note='producer receipt', resolved_at_utc='2026-09-24T03:19:59Z'))
            return 200, dict(job_id='drain', status='done', rc=0, receipt_path='fixture',
                             finished_utc='2026-09-24T03:20:00Z', stdout_tail=json.dumps(summary))
        first = self.run_action('sheet_sync_drain', bridge)
        second = self.run_action('sheet_sync_drain', bridge)
        self.assertEqual([first['rc'], second['rc']], [0, 0])
        self.assertEqual(calls, ['/run'])
        self.assertEqual(self.writes.count(('PATCH', cron.SHEET_QUEUE)), 1)
        self.assertEqual(self.tables[cron.SHEET_QUEUE][0]['note'], 'producer receipt')
        changed = dict(self.tables[cron.SHEET_QUEUE][0], evidence='different operation')
        self.assertFalse(cron.same_sheet_operation(changed, original))
        self.assertFalse(cron.resolved_sheet_row(dict(original, status='applied')))

    def test_c3_reconciler_continues_after_insert_consumed_and_heartbeat_failure(self):
        summary = dict(schema='glp.sheet_vs_site_reconcile.v1', queue_rows=[self.row()], rows_audited=1)
        calls = []
        def bridge(path, payload=None):
            calls.append(path)
            return 200, dict(job_id='reconcile', status='done', rc=0, receipt_path='fixture',
                             finished_utc='2026-09-24T03:20:00Z', stdout_tail=json.dumps(summary))
        with patch.object(cron, 'heartbeat', side_effect=TimeoutError('heartbeat readback')):
            first = self.run_action('sheet_vs_site_reconcile', bridge)
        self.dt.update(cron.SHEET_QUEUE, self.dt.eq('update_id','u1'),
                       dict(status='applied', note='consumer receipt', resolved_at_utc='2026-09-24T03:21:00Z'))
        # Also exercise the crash-before-local-confirmation path: the real
        # insert helper rejects the advanced state; exact identity reconciles it.
        state_path = self.directory / 'sheet_vs_site_reconcile.state.json'
        state = json.loads(state_path.read_text())
        state.pop('queue_insertions', None)
        state_path.write_text(json.dumps(state))
        second = self.run_action('sheet_vs_site_reconcile', bridge)
        self.assertEqual([first['rc'], second['rc']], [125, 0])
        self.assertEqual(calls, ['/run', '/job/reconcile'])
        self.assertEqual(self.writes.count(('POST', cron.SHEET_QUEUE)), 1)
        self.assertEqual(self.tables[cron.SHEET_QUEUE][0]['status'], 'applied')


class DecisionsGraphTests(unittest.TestCase):
    """Exercise the registered runner, mocking only bridge/Discord/DT transports."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.calls = []
        self.posts = []
        self.messages = {}
        self.heartbeats = []
        self.dispatch = dict(status='complete', mode='live', run_id='live-a1',
                             sink='decisions', item_count=0, unroutable_count=0,
                             source_message_ids=[], post_required=False,
                             confirmation_card='', confirmation=None)
        self.agent = dict(job_id='sol-1', status='done', rc=0,
                          finished_utc='2026-09-24T12:01:00Z', stdout_tail='agent complete')
        self.readback_fails = 0
        self.confirmation = None
        self.router_resumes = 0
        self.hb_patch = patch.object(cron, 'heartbeat', side_effect=self._heartbeat)
        self.hb_patch.start(); self.addCleanup(self.hb_patch.stop)
        self.send_patch = patch('scripts.post_discord_message.send', side_effect=self._send)
        self.send_patch.start(); self.addCleanup(self.send_patch.stop)
        self.get_patch = patch('scripts.post_discord_message.readback', side_effect=self._readback)
        self.get_patch.start(); self.addCleanup(self.get_patch.stop)

    def _heartbeat(self, *args):
        self.heartbeats.append(args)
        return {'write_status': 'confirmed'}

    def _send(self, channel, content):
        mid = str(len(self.posts) + 1)
        self.posts.append((channel, content))
        self.messages[mid] = dict(id=mid, channel_id=channel, content=content)
        return {'id': mid}

    def _readback(self, channel, mid):
        if self.readback_fails:
            self.readback_fails -= 1
            raise TimeoutError('synthetic GET unavailable')
        return self.messages[mid]

    def call(self, path, payload=None):
        self.calls.append((path, payload))
        if path.startswith('/job/'):
            job_id = path.split('/')[-1]
            return 200, self.jobs[job_id]
        if path != '/run':
            raise AssertionError(path)
        if payload['action'] == 'hermes_chat':
            assert payload['model'] == 'gpt-6-sol' and payload['reasoning'] == 'xhigh'
            assert payload['toolset_profile'] == 'authoring' and payload['max_turns'] == 60
            self.jobs['sol-1'] = self.agent
            return 200, self.agent
        args = payload['args']
        mode = args.get('mode', 'live')
        if mode == 'live':
            assert args == {'mode': 'live', 'sink': 'decisions'}
            result = self.dispatch
            job_id = 'dispatch-1'
        elif mode == 'resume':
            self.router_resumes += 1
            assert args == {'mode': 'resume', 'run-id': self.dispatch['run_id'], 'job-id': 'sol-1'}
            result = self.resumed
            job_id = 'resume-1'
        elif mode == 'confirm':
            assert args == {'mode': 'confirm', 'run-id': self.dispatch['run_id'],
                            'sink': self.dispatch['sink'], 'discord-message-id': '1'}
            result = self.confirmation or dict(getattr(self, 'resumed', self.dispatch), post_required=False,
                confirmation=dict(discord_message_id='1', sink=self.dispatch['sink'],
                    channel_id=cron.DECISIONS_CHANNEL if self.dispatch['sink'] == 'decisions' else cron.CHANNEL,
                    checks=dict(id=True, channel=True, content=True)))
            job_id = 'confirm-1'
        else:
            raise AssertionError(mode)
        job = dict(job_id=job_id, status='done', rc=0,
                   finished_utc='2026-09-24T12:00:00Z', stdout_tail=json.dumps(result))
        self.jobs[job_id] = job
        return 200, job

    def run_graph(self, args=None):
        return cron.run_action('decisions_router', args or {}, state_dir=self.root,
            actions={'decisions_router': {'timeout_s': 3300}}, call=self.call,
            notify=lambda *a: {'exact_readback': True})

    def test_clean_graph_heartbeat_and_no_card_or_model(self):
        self.jobs = {}
        result = self.run_graph()
        self.assertEqual(result['rc'], 0)
        self.assertEqual(self.posts, [])
        self.assertEqual([path for path, _ in self.calls], ['/run'])
        self.assertEqual(self.heartbeats[0][:4], ('decisions_ack', 0, 'live-a1:clean:dispatch-1',
                                                 'decisions router clean: 0 new item(s)'))

    def test_post_both_sinks_exact_confirm_then_heartbeat_and_get_only_recovery(self):
        for sink, channel in [('decisions', cron.DECISIONS_CHANNEL), ('ops-alerts', cron.CHANNEL)]:
            with self.subTest(sink=sink):
                self.jobs = {}
                self.dispatch.update(sink=sink, item_count=2, unroutable_count=1,
                                     source_message_ids=['100'], post_required=True,
                                     confirmation_card='exact operator card')
                self.readback_fails = 1
                first = self.run_graph()
                self.assertEqual(first['rc'], 125)
                self.assertEqual(self.posts[-1], (channel, 'exact operator card'))
                self.assertEqual(self.heartbeats, [])
                second = self.run_graph()
                self.assertEqual(second['rc'], 0)
                self.assertEqual(len(self.posts), 1)
                self.assertEqual(len([p for p, v in self.calls if p == '/run' and v['args'].get('mode') == 'confirm']), 1)
                self.assertEqual(self.heartbeats[-1][2], 'live-a1:discord_verified')
                self.posts.clear(); self.messages.clear(); self.calls.clear(); self.heartbeats.clear()
                self.root = self.root / sink; self.root.mkdir()

    def test_awaiting_agent_resumes_same_run_only_after_bound_terminal_job(self):
        self.jobs = {}
        agent_path = self.root / 'agent.json'
        agent_path.write_text(json.dumps(dict(schema='decisions-router-agent-v1',
            run_id='live-a1', items=[dict(item='1', line='fixture',
                status='unroutable', destination='unroutable')])))
        self.dispatch = dict(status='awaiting_agent', mode='live', run_id='live-a1',
            sink='decisions', free_text_prompt='route residue',
            agent_receipt_path=str(agent_path), agent_expected=[dict(item='1', line='fixture')],
            post_required=False)
        self.resumed = dict(status='complete', mode='live', run_id='live-a1',
            sink='decisions', item_count=1, unroutable_count=0,
            source_message_ids=['100'], post_required=True, confirmation_card='residue card')
        with patch.object(cron, 'decisions_host_sol', return_value={'job_id': 'sol-1', 'status': 'done', 'rc': 0}) as sol:
            result = self.run_graph()
        self.assertEqual(result['rc'], 0, result)
        sol.assert_called_once()
        self.assertEqual(self.router_resumes, 1)
        self.assertEqual([v['action'] for p, v in self.calls if p == '/run'],
                         ['script', 'script', 'script'])
        self.assertEqual(self.heartbeats[-1][2], 'live-a1:discord_verified')

    def test_malformed_scope_truncated_and_noncomplete_hold_no_false_heartbeat(self):
        for change in ({'sink': 'forged'}, {'status': 'blocked'},
                       {'post_required': True, 'confirmation_card': ''}):
            with self.subTest(change=change):
                self.jobs = {}; self.dispatch.update(change)
                result = self.run_graph()
                self.assertEqual(result['rc'], 125)
                self.assertEqual(self.posts, [])
                self.assertEqual(self.heartbeats, [])
                self.assertIn('pending', json.loads((self.root / 'decisions_router.state.json').read_text()))
                self.root = self.root / 'next'; self.root.mkdir()
                self.dispatch = dict(status='complete', mode='live', run_id='live-a1',
                    sink='decisions', item_count=0, unroutable_count=0,
                    source_message_ids=[], post_required=False, confirmation_card='')
        self.jobs = {}
        original = self.call
        def truncated(path, payload=None):
            code, job = original(path, payload)
            if path == '/run': job['stdout_truncated'] = True
            return code, job
        self.call = truncated
        result = self.run_graph()
        self.assertEqual(result['rc'], 125)
        self.assertEqual(self.heartbeats, [])

    def test_unknown_post_ack_never_replays_card_or_confirms(self):
        self.jobs = {}
        self.dispatch.update(item_count=1, source_message_ids=['100'],
                             post_required=True, confirmation_card='card')
        with patch('scripts.post_discord_message.send', side_effect=TimeoutError('unknown POST')) as send:
            first, second = self.run_graph(), self.run_graph()
        self.assertEqual([first['rc'], second['rc']], [125, 125])
        self.assertEqual(send.call_count, 1)
        self.assertEqual(self.heartbeats, [])
        self.assertEqual([v['args']['mode'] for p,v in self.calls if p == '/run'], ['live'])


    def test_successful_decisions_cycle_releases_continuation_binding_for_next_run(self):
        self.jobs = {}
        self.dispatch.update(item_count=1, source_message_ids=['100'],
                             post_required=True, confirmation_card='first card')
        first = self.run_graph()
        self.assertEqual(first['rc'], 0, first)
        self.dispatch.update(run_id='live-a2', confirmation_card='second card')
        original = self.call
        def next_job(path, payload=None):
            if path == '/run' and payload['args'].get('mode') == 'live':
                code, job = original(path, payload)
                job = dict(job, job_id='dispatch-2')
                self.jobs['dispatch-2'] = job
                return code, job
            if path == '/run' and payload['args'].get('mode') == 'confirm':
                self.calls.append((path, payload))
                assert payload['args']['discord-message-id'] == '2'
                result = dict(self.dispatch, post_required=False,
                    confirmation=dict(discord_message_id='2', sink='decisions',
                        channel_id=cron.DECISIONS_CHANNEL,
                        checks=dict(id=True, channel=True, content=True)))
                job = dict(job_id='confirm-2', status='done', rc=0,
                           finished_utc='2026-09-24T12:00:00Z', stdout_tail=json.dumps(result))
                self.jobs['confirm-2'] = job
                return 200, job
            return original(path, payload)
        self.call = next_job
        second = self.run_graph()
        self.assertEqual(second['rc'], 0, second)
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(self.heartbeats[-1][2], 'live-a2:discord_verified')

    def test_preconfirmed_run_reconciles_get_without_card_post_or_confirm_replay(self):
        self.jobs = {}
        self.messages['known'] = dict(id='known', channel_id=cron.DECISIONS_CHANNEL, content='prior card')
        self.dispatch.update(item_count=1, source_message_ids=['100'],
            confirmation_card='prior card', post_required=False,
            confirmation=dict(discord_message_id='known', sink='decisions',
                channel_id=cron.DECISIONS_CHANNEL,
                checks=dict(id=True, channel=True, content=True)))
        result = self.run_graph()
        self.assertEqual(result['rc'], 0, result)
        self.assertEqual(self.posts, [])
        self.assertEqual([p for p,v in self.calls], ['/run'])
        self.assertEqual(self.heartbeats[0][2], 'live-a1:discord_verified')

    def test_host_sol_subscription_route_and_exactly_once_stage(self):
        from ops import decisions_router as router
        agent_dir = self.root / 'agent'
        agent_dir.mkdir()
        path = str(agent_dir / 'live-a1.json')
        state_path = self.root / 'state.json'
        state = {}
        class FakeProcess:
            pid = 42
            returncode = 0
            def __init__(self, argv, **kwargs):
                self.argv = argv
                self.kwargs = kwargs
            def communicate(self, timeout):
                self.timeout = timeout
                return b'ok', b''
        with patch.object(router, 'AGENT_DIR', agent_dir), patch('subprocess.Popen', side_effect=FakeProcess) as popen:
            first = cron.decisions_host_sol('bounded prompt', path, 'live-a1', state, state_path)
            second = cron.decisions_host_sol('bounded prompt', path, 'live-a1', state, state_path)
        self.assertEqual(first, second)
        self.assertEqual(popen.call_count, 1)
        argv = popen.call_args.args[0]
        for flag, expected in (('--provider', 'openai-codex'), ('--model', 'gpt-6-sol'),
                               ('--reasoning', 'xhigh'), ('--toolsets', 'web,file,terminal,firecrawl')):
            self.assertEqual(argv[argv.index(flag) + 1], expected)
        self.assertNotIn('bounded prompt', argv)
        self.assertFalse((self.root / 'decisions-sol-live-a1.prompt').exists())
        self.assertEqual(json.loads(state_path.read_text())['decisions_jobs']['sol']['status'], 'complete')
        with patch.object(router, 'AGENT_DIR', agent_dir), patch('subprocess.Popen') as popen:
            with self.assertRaisesRegex(RuntimeError, 'binding_changed'):
                cron.decisions_host_sol('changed prompt', path, 'live-a1', state, state_path)
            popen.assert_not_called()

    def test_host_sol_uncertain_launch_never_replayed(self):
        from ops import decisions_router as router
        agent_dir = self.root / 'agent'
        agent_dir.mkdir()
        path = str(agent_dir / 'live-a1.json')
        state_path = self.root / 'state.json'
        state = {}
        with patch.object(router, 'AGENT_DIR', agent_dir), patch('subprocess.Popen', side_effect=OSError('unavailable')) as popen:
            with self.assertRaises(OSError):
                cron.decisions_host_sol('prompt', path, 'live-a1', state, state_path)
            with self.assertRaisesRegex(RuntimeError, 'unconfirmed_no_replay'):
                cron.decisions_host_sol('prompt', path, 'live-a1', state, state_path)
            self.assertEqual(popen.call_count, 1)
        self.assertEqual(json.loads(state_path.read_text())['decisions_jobs']['sol']['status'], 'launch_unknown')

    def test_mismatched_confirmation_holds_without_heartbeat_or_card_repost(self):
        self.jobs = {}
        self.dispatch.update(item_count=1, source_message_ids=['100'],
                             post_required=True, confirmation_card='card')
        self.confirmation = dict(self.dispatch, post_required=False,
            confirmation=dict(discord_message_id='1', sink='decisions',
                channel_id=cron.CHANNEL, checks=dict(id=True, channel=True, content=True)))
        first = self.run_graph()
        second = self.run_graph()
        self.assertEqual([first['rc'], second['rc']], [125, 125])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.heartbeats, [])
        self.assertEqual([v['args']['mode'] for p,v in self.calls if p == '/run'], ['live', 'confirm'])

    def test_scheduled_scope_rejects_replay_and_message_selection(self):
        for args in ({'mode': 'replay'}, {'sink': 'ops-alerts'}, {'message-id': '123'}):
            with self.subTest(args=args):
                result = self.run_graph(args)
                self.assertEqual(result['status'], 'REFUSED_DECISIONS_CRON_LIVE_ONLY')
                self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
