"""Offline regression for fleet-sentinel's authenticated read and durable alert lanes."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.cron_ports import fleet_sentinel as sentinel


SLOT = "2026-09-24T08:10:America/Los_Angeles"
NOW = datetime(2026, 9, 24, 15, 10, tzinfo=timezone.utc)


class FleetSentinelRegression(unittest.TestCase):
    def test_fixture_is_the_published_schedule_and_three_case_lanes(self):
        fixture = json.loads((sentinel.ROOT / "reports/ops/n8n-to-cron-20260924/preimage"
                              / (sentinel.WORKFLOW + ".json")).read_text())["active"]
        nodes = {node["name"]: node for node in fixture["nodes"]}
        self.assertEqual(nodes["Schedule"]["parameters"]["rule"]["interval"][0]["expression"],
                         sentinel.CRON)
        self.assertEqual(nodes["Bridge Health"]["parameters"]["genericAuthType"],
                         "httpHeaderAuth")
        self.assertEqual(nodes["Bridge Health"]["parameters"]["url"],
                         "https://bridge.allocant.ai/health")
        self.assertEqual(sentinel.HEALTH_URL, "https://bridge.allocant.ai/health")
        self.assertTrue({"Insert Miss Cases", "Bridge Down Case", "Insert Supply Case"} <= nodes.keys())

    def test_health_uses_bridge_global_loader_without_exposing_token(self):
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "token"
            token_file.write_text("fixture-only-global-token\n")
            token_file.chmod(0o600)
            requests = []

            class Response:
                status = 200
                def __enter__(self):
                    return self
                def __exit__(self, *_):
                    return False
                def read(self, size):
                    assert size == 8192
                    return b'{"status":"ok","uptime_s":17}'

            def urlopen(request, timeout):
                requests.append((request, timeout))
                return Response()

            output = io.StringIO()
            with patch.dict(os.environ, {"N8N_BRIDGE_TOKEN_FILE": str(token_file)}), \
                    patch.object(sentinel.urllib.request, "urlopen", side_effect=urlopen), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(sentinel._bridge_health()["uptime_s"], 17)
            self.assertEqual(len(requests), 1)
            request, timeout = requests[0]
            self.assertEqual(request.full_url, sentinel.HEALTH_URL)
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture-only-global-token")
            self.assertEqual(request.get_header("User-agent"), "axios/1.6.8")
            self.assertEqual(timeout, 10)
            self.assertNotIn("fixture-only-global-token", output.getvalue())

    def test_heartbeat_read_exhausts_more_than_one_cursor_window(self):
        from ops import n8n_dt
        pages = [
            {"data": {"rows": [{"machine": "old"}], "nextCursor": "next"}},
            {"data": {"rows": [{"machine": "new"}], "nextCursor": None}},
        ]
        with patch.object(n8n_dt, "_native_datatable_call", side_effect=pages) as call:
            rows = sentinel._read_table(sentinel.HEARTBEATS)
        self.assertEqual([r["machine"] for r in rows], ["old", "new"])
        self.assertEqual(call.call_args_list[1].args[0]["cursor"], "next")

    def test_manual_repair_is_one_corrected_identity_not_a_replay(self):
        original = "2026-09-24T18:10:America/Los_Angeles"
        case_id = "ec-original-bridge"
        prior = {"slot": original, "status": "PASS", "bridge": "down",
                 "deliveries": {"bridge_down": {"id": "old-alert"}},
                 "cases": {case_id: {"status": "verified", "row": {
                     "workflow_id": "bridge_health", "error_text": "GET /health failed: HTTPError"}}}}
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / (original.replace(":", "-") + ".json")
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps(prior))
            good = {"id": "correction", "channel_id": sentinel.CHANNEL,
                    "content": f"Correction old-alert / {case_id}: host HTTP 403 was User-Agent WAF"}
            with patch("scripts.post_discord_message.readback", return_value=good):
                one = sentinel.repair_slot(original, "correction", state_dir=directory)
                two = sentinel.repair_slot(original, "correction", state_dir=directory)
            self.assertEqual(one, two)
            self.assertEqual(one, "2026-09-24T18:11:America/Los_Angeles")
            with patch("scripts.post_discord_message.readback", return_value={**good, "content": "unrelated"}):
                with self.assertRaisesRegex(ValueError, "repair_correction_readback_mismatch"):
                    sentinel.repair_slot(original, "correction", state_dir=directory)

    def test_ambiguous_bridge_heartbeat_is_adopted_by_exact_key_without_reinsert(self):
        from ops import n8n_dt
        row = {"machine": "bridge_health", "last_success_utc": "2026-09-25T03:03:16Z",
               "output_count": 1, "receipt": sentinel.HEALTH_URL + " " + SLOT,
               "note": "uptime_s 17"}
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / (SLOT.replace(":", "-") + ".json")
            state_path.parent.mkdir(parents=True)
            state = {"slot": SLOT, "started_utc": row["last_success_utc"],
                     "lanes": {"compare": "done", "supply": "done"},
                     "errors": {"bridge": "NativeSemanticRefusal"}}
            state_path.write_text(json.dumps(state))
            with patch.object(n8n_dt, "rows", return_value=[dict(row, id=12711)]) as read:
                result = sentinel.run(SLOT, state_dir=Path(directory),
                    health=lambda: {"status": "ok", "uptime_s": 20},
                    insert=lambda *_: self.fail("ack-uncertain insert must not replay"))
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["bridge"], "healthy")
            self.assertEqual(read.call_count, 1)
            saved = json.loads(state_path.read_text())
            self.assertEqual(saved["bridge_heartbeat_status"], "verified_prior_effect")

    def test_manual_repair_suppresses_previously_verified_miss_effects(self):
        old = {"case_id": "old-case", "signature": "same", "error_text": "old — last success 5h ago"}
        new = {"case_id": "new-case", "signature": "new", "error_text": "new — last success 6h ago"}
        prior = {"slot": SLOT, "status": "PASS", "misses": 1,
                 "cases": {"old-case": {"status": "verified", "row": old}},
                 "deliveries": {"misses": {"status": "verified", "id": "old-message"}}}
        inserted, posted = [], []
        with tempfile.TemporaryDirectory() as directory:
            repair = "2026-09-24T08:11:America/Los_Angeles"
            state_path = Path(directory) / (repair.replace(":", "-") + ".json")
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"slot": repair, "lanes": {"bridge": "done", "supply": "done"}}))
            with patch.object(sentinel, "compare", return_value={
                    "misses": 2, "miss_rows": [old, new], "text": "old and new"}):
                result = sentinel.run(repair, now=NOW, state_dir=Path(directory),
                    run_type="manual_registered_repair", repair_prior=prior,
                    read_table=lambda _: [], insert=lambda table, row: inserted.append(row["case_id"]),
                    post=lambda key, body, *_: posted.append((key, body)))
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(inserted, ["new-case"])
            self.assertEqual(len(posted), 1)
            self.assertIn("new", posted[0][1])
            self.assertNotIn("old", posted[0][1])
            self.assertEqual(json.loads(state_path.read_text())["prior_misses_suppressed"], 1)


    def test_cases_survive_uncertain_discord_and_reentry_never_resends(self):
        registry = [{"machine": "stale_fixture", "cadence_hours": 1, "grace_hours": 0}]
        events = []
        sent = []
        def read_table(table):
            return registry if table == sentinel.REGISTRY else []
        def insert(table, row):
            events.append(("insert", table, row.get("case_id")))
        def uncertain_send(channel, content):
            sent.append((channel, content))
            events.append(("send", channel, None))
            raise RuntimeError("uncertain delivery")
        def buffer_job(slot):
            self.assertEqual(slot, SLOT)
            return {"status": "done", "rc": 0, "stdout_tail": json.dumps(
                {"pool": {"ready": 0, "minimum": 5}})}

        with tempfile.TemporaryDirectory() as directory, \
                patch("scripts.post_discord_message.send", side_effect=uncertain_send), \
                patch("scripts.post_discord_message.readback") as readback:
            for attempt in (1, 2):
                result = sentinel.run(SLOT, now=NOW, state_dir=Path(directory),
                                      read_table=read_table, insert=insert,
                                      health=lambda: (_ for _ in ()).throw(OSError("offline")),
                                      buffer_job=buffer_job)
                self.assertEqual(result["status"], "FAIL")
                self.assertEqual(set(result["errors"]), {"compare", "bridge", "supply"})
                cases = [event for event in events if event[0] == "insert" and event[1] == sentinel.CASES]
                self.assertEqual(len(cases), 3, (attempt, events))
                self.assertEqual(len({event[2] for event in cases}), 3)
                self.assertEqual(len(sent), 3, (attempt, events))
                readback.assert_not_called()
            first_send = next(i for i, event in enumerate(events) if event[0] == "send")
            self.assertTrue(all(event[0] == "insert" for event in events[:first_send]))
            state = json.loads(Path(result["receipt_path"]).read_text())
            self.assertEqual(len(state["cases"]), 3)
            self.assertTrue(all(item["status"] == "verified" for item in state["cases"].values()))

    def test_uncertain_case_insert_is_not_blindly_replayed(self):
        calls = []
        row = sentinel.case_row("stale_fixture", "sentinel_miss", sentinel.MISS_CLASS,
                                "late", NOW)
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "slot.json"
            state = {}
            def uncertain(table, value):
                calls.append((table, value["case_id"]))
                raise TimeoutError("case insert acknowledgement lost")
            with self.assertRaises(TimeoutError):
                sentinel._case_once(row, state, state_path, uncertain)
            resumed = json.loads(state_path.read_text())
            with self.assertRaisesRegex(ValueError, "datatable_insert_unverified"):
                sentinel._case_once(row, resumed, state_path, uncertain)
            self.assertEqual(calls, [(sentinel.CASES, row["case_id"])])

    def test_bridge_failure_card_names_cron_slot_not_n8n_execution(self):
        alert = sentinel.bridge_down("OSError", NOW, SLOT)
        self.assertIn("cron slot " + SLOT, alert["text"])
        self.assertNotIn("n8n execution", alert["text"])
        self.assertEqual(alert["case_row"]["execution_id"], "")


if __name__ == "__main__":
    unittest.main()
