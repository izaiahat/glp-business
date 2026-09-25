import csv
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path("/home/ubuntu/business/scripts/loom_delivery_rails.py")
spec = importlib.util.spec_from_file_location("loom_delivery_rails_tested", SCRIPT)
loom = importlib.util.module_from_spec(spec)
assert spec.loader
sys.modules[spec.name] = loom
spec.loader.exec_module(loom)


class LoomRailsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sheet = self.root / "send_sheet.csv"
        with self.sheet.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["opportunity_id", "owner_name", "business_name"])
            writer.writeheader()
            writer.writerow({"opportunity_id": "opp_test123", "owner_name": "Test Owner", "business_name": "Test Co"})
        self.config = self.root / "rails.json"
        self.config.write_text(json.dumps({
            "default_alert_channel_id": "ops",
            "ventures": {
                "boring_ai": {
                    "armed": True,
                    "channel_id": "delivery",
                    "send_sheet": str(self.sheet),
                    "delivery_ledger": str(self.root / "delivery.jsonl"),
                    "max_duration_seconds": 90,
                    "guard_created_assignment": "active_recording_session",
                },
                "allocant": {"armed": True, "channel_id": "decisions", "send_sheet": "", "delivery_ledger": str(self.root / "allocant.jsonl"), "max_duration_seconds": 120},
                "glp": {"armed": False, "channel_id": "decisions", "send_sheet": "", "delivery_ledger": str(self.root / "glp.jsonl"), "max_duration_seconds": 90},
            },
        }))
        self.template = self.root / "hero.html"
        self.template.write_text("<section data-loom-hero>{{LOOM_TITLE}} {{LOOM_SHARE_URL}} {{LOOM_EMBED_URL}} {{LOOM_THUMBNAIL_URL}} {{LOOM_DURATION}} {{BOOKING_EMBED}}</section>")
        self.patchers = [
            mock.patch.object(loom, "CONFIG_PATH", self.config),
            mock.patch.object(loom, "REGISTRY_PATH", self.root / "registry.jsonl"),
            mock.patch.object(loom, "EVENTS_PATH", self.root / "events.jsonl"),
            mock.patch.object(loom, "GUARD_EVENTS_PATH", self.root / "guard-events.jsonl"),
            mock.patch.object(loom, "GUARD_STATE_PATH", self.root / "guard-state.json"),
            mock.patch.object(loom, "OUTCOMES_PATH", self.root / "outcomes.jsonl"),
            mock.patch.object(loom, "TRANSCRIPT_DIR", self.root / "transcripts"),
            mock.patch.object(loom, "REPORT_DIR", self.root / "reports"),
            mock.patch.object(loom, "TEMPLATE_PATH", self.template),
            mock.patch.object(loom, "BORING_DB_PATH", self.root / "boring.sqlite3"),
            mock.patch.object(loom, "BORING_EXPORT_PATH", self.root / "manual_delivery_queue.csv"),
        ]
        for patcher in self.patchers:
            patcher.start()
        self._seed_boring_queue()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def _seed_boring_queue(self):
        db = self.root / "boring.sqlite3"
        conn = sqlite3.connect(db)
        conn.executescript("""
        CREATE TABLE opportunities(id TEXT PRIMARY KEY,business_name TEXT,recommended_preview TEXT);
        CREATE TABLE delivery_log(id TEXT PRIMARY KEY,opportunity_id TEXT,status TEXT,channel TEXT,delivery_copy TEXT,operator_action_required TEXT,created_at TEXT);
        CREATE TABLE delivery_instructions(id TEXT PRIMARY KEY,opportunity_id TEXT,batch_id TEXT,batch_position INTEGER,created_at TEXT,status TEXT,instructions_json TEXT,approved_preview_url TEXT,updated_at TEXT);
        """)
        conn.execute("INSERT INTO opportunities VALUES (?,?,?)", ("opp_test123", "Test Co", "booking payments"))
        conn.execute("INSERT INTO delivery_log VALUES (?,?,?,?,?,?,?)", (
            "dl1", "opp_test123", "not_contacted", "manual_linkedin_dm",
            "Quick walkthrough: [PASTE LOOM URL]", "record", "2026-07-10T00:00:00Z",
        ))
        conn.execute("INSERT INTO delivery_log VALUES (?,?,?,?,?,?,?)", (
            "dl-old", "opp_test123", "not_contacted", "manual_meta_dm_or_best_route",
            "stale history", "do not use", "2026-07-09T00:00:00Z",
        ))
        conn.execute("INSERT INTO delivery_instructions VALUES (?,?,?,?,?,?,?,?,?)", (
            "di1", "opp_test123", "batch1", 1, "2026-07-10T00:00:00Z", "ready_manual_only_v2",
            json.dumps({
                "delivery_copy": "Quick walkthrough: [PASTE LOOM URL]",
                "manual_only": True,
                "primary_method": "manual_linkedin_dm",
                "source_url": "https://facebook.com/groups/example/posts/1",
                "preview_url": "https://tryboringai.com/preview/test",
            }),
            "https://tryboringai.com/preview/test", "2026-07-10T00:00:00Z"))
        conn.commit()
        conn.close()
        with (self.root / "manual_delivery_queue.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["opportunity_id", "business_name", "manual_delivery_copy"])
            writer.writeheader()
            writer.writerow({"opportunity_id": "opp_test123", "business_name": "Test Co", "manual_delivery_copy": "Quick walkthrough: [PASTE LOOM URL]"})

    @staticmethod
    def metadata(url):
        canonical, video_id = loom.canonical_loom_url(url)
        return {
            "video_id": video_id,
            "share_url": canonical,
            "title": "LOOM-1 real recording",
            "author_name": "Izaiah",
            "thumbnail_url": "https://cdn.loom.com/thumb.jpg",
            "duration_seconds": 72,
            "width": 1280,
            "height": 720,
            "embed_html": "<iframe></iframe>",
            "provider_name": "Loom",
        }

    def test_canonical_url_strips_tracking(self):
        url, video_id = loom.canonical_loom_url("see https://www.loom.com/share/abc_123?sid=secret")
        self.assertEqual("abc_123", video_id)
        self.assertEqual("https://www.loom.com/share/abc_123", url)

    def test_ingest_updates_sheet_ledger_and_hero_without_send(self):
        result = loom.ingest_url(
            "https://www.loom.com/share/abc123?sid=x",
            venture="boring_ai",
            opportunity_id="opp_test123",
            source="unit_test",
            no_discord=True,
            oembed_fetcher=self.metadata,
        )
        self.assertEqual("captured", result["status"])
        row = loom.send_sheet_rows(self.sheet)[0]
        self.assertEqual("abc123", row["loom_video_id"])
        self.assertEqual("LOOM-1 real recording", row["loom_title"])
        self.assertEqual("72", row["loom_duration_seconds"])
        self.assertIn("data-loom-hero", Path(result["hero_path"]).read_text())
        self.assertEqual([], result["discord_message_ids"])
        self.assertEqual(1, len(loom.read_jsonl(self.root / "registry.jsonl")))
        self.assertEqual(1, len(loom.read_jsonl(self.root / "delivery.jsonl")))
        self.assertEqual("attached", result["queue_attachment"]["status"])

    def test_auto_capture_updates_canonical_manual_queue_without_external_send(self):
        result = loom.ingest_url(
            "https://loom.com/share/queue1", venture="boring_ai", opportunity_id="opp_test123",
            source="guard_event:test", no_discord=True, oembed_fetcher=self.metadata,
        )
        self.assertFalse(result["queue_attachment"]["external_owner_send_executed"])
        conn = sqlite3.connect(self.root / "boring.sqlite3")
        payload = json.loads(conn.execute("SELECT instructions_json FROM delivery_instructions").fetchone()[0])
        delivery = conn.execute("SELECT status,delivery_copy FROM delivery_log").fetchone()
        conn.close()
        self.assertEqual("queue1", payload["loom_video_id"])
        self.assertNotIn("[PASTE LOOM URL]", payload["delivery_copy"])
        self.assertEqual("not_contacted", delivery[0])
        self.assertIn("https://www.loom.com/share/queue1", delivery[1])
        export = loom.send_sheet_rows(self.root / "manual_delivery_queue.csv")[0]
        self.assertEqual("queue1", export["loom_video_id"])
        card = loom.render_boring_capture_card(result["queue_attachment"]["operator_package"])
        self.assertIn("Test Co Loom ready", card)
        self.assertIn("https://facebook.com/groups/example/posts/1", card)
        self.assertIn("https://tryboringai.com/preview/test", card)
        self.assertIn("https://www.loom.com/share/queue1", card)
        self.assertIn("Quick walkthrough:", card)
        self.assertNotIn("[PASTE LOOM URL]", card)
        self.assertIn("Reply `SENT` or `BLOCKED`", card)
        self.assertIn("No prospect message was sent automatically", card)
        self.assertEqual("pass", loom.fmt_lint(card, output_class="actionable")["status"])

    def test_auto_capture_upserts_missing_v2_mirrors_and_renders_dm_copy(self):
        export_path = self.root / "manual_delivery_queue.csv"
        with export_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "opportunity_id", "business_name", "source_url", "original_ask",
                "preview_url_or_path", "manual_delivery_copy", "operator_decision",
            ])
            writer.writeheader()
        with self.sheet.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                "opportunity_id", "owner_name", "business_name", "verbatim_ask",
                "public_preview_url", "recommended_channel", "contact_route",
                "paste_ready_message", "loom_script_path", "package_pitch", "apollo_status",
            ])
            writer.writeheader()
        conn = sqlite3.connect(self.root / "boring.sqlite3")
        payload = json.loads(conn.execute("SELECT instructions_json FROM delivery_instructions").fetchone()[0])
        payload.pop("delivery_copy", None)
        payload.update({
            "dm_copy": "Quick walkthrough: {LOOM_URL}",
            "prospect_name": "Test Owner",
            "primary_destination": "https://linkedin.com/in/test-owner",
        })
        conn.execute("UPDATE delivery_instructions SET instructions_json=?", (json.dumps(payload),))
        conn.commit()
        conn.close()
        result = loom.ingest_url(
            "https://loom.com/share/upsert1", venture="boring_ai", opportunity_id="opp_test123",
            source="guard_event:test", no_discord=True, oembed_fetcher=self.metadata,
        )
        self.assertEqual("attached", result["queue_attachment"]["status"])
        rows = loom.send_sheet_rows(export_path)
        self.assertEqual(1, len(rows))
        self.assertEqual("opp_test123", rows[0]["opportunity_id"])
        self.assertEqual("upsert1", rows[0]["loom_video_id"])
        sheet_rows = loom.send_sheet_rows(self.sheet)
        self.assertEqual(1, len(sheet_rows))
        self.assertEqual("upsert1", sheet_rows[0]["loom_video_id"])
        self.assertNotIn("{LOOM_URL}", sheet_rows[0]["paste_ready_message"])
        card = loom.render_boring_capture_card(result["queue_attachment"]["operator_package"])
        self.assertIn("Manual LinkedIn DM", card)
        self.assertIn("https://linkedin.com/in/test-owner", card)

    def test_ready_manual_status_accepts_numeric_schema_versions_at_every_lookup(self):
        conn = sqlite3.connect(self.root / "boring.sqlite3")
        conn.execute("UPDATE delivery_instructions SET status='ready_manual_only_v3'")
        conn.commit()
        conn.close()

        result = loom.ingest_url(
            "https://loom.com/share/versioned1",
            venture="boring_ai",
            opportunity_id="opp_test123",
            source="guard_event:test",
            no_discord=True,
            oembed_fetcher=self.metadata,
        )
        self.assertEqual("attached", result["queue_attachment"]["status"])
        self.assertEqual(
            "opp_test123",
            loom.boring_delivery_for_video("versioned1", self.root / "boring.sqlite3"),
        )
        loom._persist_boring_capture_card_message("opp_test123", "versioned1", "123456")
        conn = sqlite3.connect(self.root / "boring.sqlite3")
        payload = json.loads(conn.execute("SELECT instructions_json FROM delivery_instructions").fetchone()[0])
        conn.close()
        self.assertEqual("123456", payload["loom_operator_card_message_id"])

    def test_ready_manual_status_rejects_non_numeric_or_superseded_suffixes(self):
        for status in ("ready_manual_only_v3_beta", "superseded_fresh_v6_card"):
            with self.subTest(status=status):
                conn = sqlite3.connect(self.root / "boring.sqlite3")
                conn.execute("UPDATE delivery_instructions SET status=?", (status,))
                conn.commit()
                conn.close()
                with self.assertRaisesRegex(RuntimeError, "expected one ready manual package.*found 0"):
                    loom.attach_loom_to_boring_queue(
                        "opp_test123",
                        self.metadata("https://loom.com/share/rejected1"),
                        self.root / "boring.sqlite3",
                    )

    def test_guard_assignment_rejects_when_no_recording_session_is_armed(self):
        with self.sheet.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["opportunity_id", "owner_name", "business_name"])
            writer.writeheader()
            writer.writerow({"opportunity_id": "opp_wrong", "owner_name": "Wrong", "business_name": "Wrong"})
            writer.writerow({"opportunity_id": "opp_test123", "owner_name": "Test", "business_name": "Test Co"})
        metadata = self.metadata("https://loom.com/share/sessionless1")
        metadata["title"] = "Generic Loom Recording"
        with self.assertRaisesRegex(RuntimeError, "exactly_one_armed_session:0"):
            loom.pending_guard_opportunity(
                metadata,
                event_id="evt-sessionless",
                event_time="2099-07-10T20:00:00Z",
            )

    def test_duplicate_capture_is_idempotent(self):
        kwargs = dict(venture="boring_ai", opportunity_id="opp_test123", source="unit_test", no_discord=True, oembed_fetcher=self.metadata)
        first = loom.ingest_url("https://loom.com/share/dup1", **kwargs)
        second = loom.ingest_url("https://loom.com/share/dup1", **kwargs)
        self.assertEqual("captured", first["status"])
        self.assertEqual("duplicate", second["status"])
        self.assertEqual(1, len(loom.read_jsonl(self.root / "registry.jsonl")))

    def test_duration_limit_fails_closed(self):
        def too_long(url):
            data = self.metadata(url)
            data["duration_seconds"] = 91
            return data
        with self.assertRaisesRegex(RuntimeError, "exceeds 90s"):
            loom.ingest_url("https://loom.com/share/long1", venture="boring_ai", opportunity_id="opp_test123", source="test", no_discord=True, oembed_fetcher=too_long)

    def test_view_email_has_who_which_and_optional_percent(self):
        loom.ingest_url("https://loom.com/share/view1", venture="boring_ai", opportunity_id="opp_test123", source="test", no_discord=True, oembed_fetcher=self.metadata)
        record = {
            "from_email": "notifications@loom.com",
            "subject": "Alex watched your video LOOM-1 real recording",
            "body": "Viewer: alex@example.com\nAlex watched your video. 87% watched.",
            "message_id": "m-1",
        }
        event = loom.parse_view_notification(record)
        self.assertIsNotNone(event)
        self.assertEqual("alex@example.com", event["viewer"])
        self.assertEqual("LOOM-1 real recording", event["title"])
        self.assertEqual(87, event["watch_percent"])
        handled = loom.handle_view_event(event, no_discord=True)
        self.assertTrue(handled["handled"])
        self.assertEqual(1, len(loom.read_jsonl(self.root / "outcomes.jsonl")))

    def test_non_loom_email_is_ignored(self):
        self.assertIsNone(loom.parse_view_notification({"from_email": "attacker@example.com", "subject": "watched", "body": "Viewer: x"}))

    def test_transcript_is_filed_and_sheet_updated(self):
        loom.ingest_url("https://loom.com/share/transcript1", venture="boring_ai", opportunity_id="opp_test123", source="test", no_discord=True, oembed_fetcher=self.metadata)
        source = self.root / "download.srt"
        source.write_text("1\n00:00:00,000 --> 00:00:02,000\nReal transcript.\n")
        result = loom.import_transcript(source, video_id="transcript1")
        self.assertEqual("filed", result["status"])
        self.assertTrue(Path(result["transcript_path"]).is_file())
        self.assertTrue(loom.send_sheet_rows(self.sheet)[0]["loom_transcript_path"].endswith("transcript1.srt"))

    def test_discord_link_requires_exactly_one_opportunity(self):
        self.assertEqual("opp_test123", loom.find_opportunity("opp_test123 https://loom.com/share/a"))
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            loom.find_opportunity("https://loom.com/share/a")
        with self.assertRaisesRegex(RuntimeError, "exactly one"):
            loom.find_opportunity("opp_one opp_two https://loom.com/share/a")

    def test_glp_rail_is_unarmed(self):
        cfg = loom.load_config()
        self.assertFalse(cfg["ventures"]["glp"]["armed"])

    def test_guard_poller_is_double_gated_and_disarmed(self):
        called = False

        def request(**kwargs):
            nonlocal called
            called = True
            return {"data": [], "meta": {}}

        result = loom.poll_guard_events(request_fn=request)
        self.assertEqual("disarmed", result["status"])
        self.assertEqual(0, result["requests"])
        self.assertFalse(called)

    def test_guard_view_event_writes_actor_time_and_location_outcome(self):
        loom.ingest_url("https://loom.com/share/guardview1", venture="boring_ai", opportunity_id="opp_test123", source="test", no_discord=True, oembed_fetcher=self.metadata)
        event = {
            "id": "evt-view-1",
            "attributes": {
                "time": "2026-07-09T20:00:00.000Z",
                "action": "loom_video_viewed",
                "actor": {"name": "Alex", "email": "alex@example.com"},
                "context": [{"id": "guardview1", "type": "loom_video", "attributes": {}}],
                "location": {"countryName": "United States", "regionName": "Oregon", "city": "Portland"},
            },
        }
        result = loom.process_guard_event(event, no_discord=True)
        self.assertEqual("outcome_captured", result["status"])
        outcome = loom.read_jsonl(self.root / "outcomes.jsonl")[-1]
        self.assertEqual("2026-07-09T20:00:00.000Z", outcome["ts_utc"])
        self.assertEqual("alex@example.com", outcome["payload"]["viewer"])
        self.assertEqual("Portland", outcome["payload"]["location"]["city"])

    def test_guard_poller_persists_each_action_cursor_under_request_ceiling(self):
        config = json.loads(self.config.read_text())
        config["guard_events"] = {
            "armed": True,
            "actions": ["loom_video_created", "loom_video_viewed"],
            "page_limit": 100,
            "max_requests_per_run": 8,
        }
        self.config.write_text(json.dumps(config))
        calls = []

        def request(**kwargs):
            calls.append(kwargs)
            return {"data": [], "meta": {"next": ""}}

        now = loom.parse_utc("2026-07-09T20:00:00Z")
        env = {
            loom.GUARD_ARM_ENV: "YES",
            "LOOM_ORG_ID": "org-test",
            "LOOM_ADMIN_API_KEY": "key-test",
        }
        with mock.patch.dict(loom.os.environ, env, clear=False):
            result = loom.poll_guard_events(no_discord=True, now=now, request_fn=request)
        self.assertEqual("pass", result["status"])
        self.assertEqual(2, result["requests"])
        self.assertLessEqual(result["request_ceiling"], 8)
        self.assertEqual(["loom_video_created", "loom_video_viewed"], [call["action"] for call in calls])
        state = json.loads((self.root / "guard-state.json").read_text())
        self.assertEqual("", state["actions"]["loom_video_created"]["cursor"])
        self.assertEqual("", state["actions"]["loom_video_viewed"]["cursor"])
        self.assertEqual("2026-07-09T19:55:00Z", state["actions"]["loom_video_created"]["watermark"])
        self.assertEqual("2026-07-09T19:55:00Z", state["actions"]["loom_video_viewed"]["watermark"])

    def test_guard_poller_sorts_created_events_oldest_first_before_processing(self):
        config = json.loads(self.config.read_text())
        config["guard_events"] = {
            "armed": True,
            "actions": ["loom_video_created"],
            "page_limit": 100,
            "max_requests_per_run": 8,
        }
        self.config.write_text(json.dumps(config))
        events = [
            {"id": "evt-late", "attributes": {"action": "loom_video_created", "time": "2026-07-09T20:02:00Z"}},
            {"id": "evt-early-b", "attributes": {"action": "loom_video_created", "time": "2026-07-09T20:01:00Z"}},
            {"id": "evt-early-a", "attributes": {"action": "loom_video_created", "time": "2026-07-09T20:01:00Z"}},
        ]
        processed = []

        def processor(event, *, no_discord=False):
            processed.append(event["id"])
            return {"event_id": event["id"], "no_discord": no_discord}

        env = {loom.GUARD_ARM_ENV: "YES", "LOOM_ORG_ID": "org-test", "LOOM_ADMIN_API_KEY": "key-test"}
        with mock.patch.dict(loom.os.environ, env, clear=False):
            result = loom.poll_guard_events(
                no_discord=True,
                now=loom.parse_utc("2026-07-09T20:05:00Z"),
                request_fn=lambda **kwargs: {"data": events, "meta": {"next": ""}},
                process_event_fn=processor,
            )
        self.assertEqual(["evt-early-a", "evt-early-b", "evt-late"], processed)
        self.assertEqual("event_time_ascending_after_complete_page_chain", result["created_event_ordering"])

    def test_guard_created_page_chain_fails_closed_before_processing_when_incomplete(self):
        config = json.loads(self.config.read_text())
        config["guard_events"] = {
            "armed": True,
            "actions": ["loom_video_created"],
            "page_limit": 1,
            "max_requests_per_run": 1,
        }
        self.config.write_text(json.dumps(config))
        processed = []
        event = {"id": "evt-page-1", "attributes": {"action": "loom_video_created", "time": "2026-07-09T20:01:00Z"}}
        env = {loom.GUARD_ARM_ENV: "YES", "LOOM_ORG_ID": "org-test", "LOOM_ADMIN_API_KEY": "key-test"}
        with mock.patch.dict(loom.os.environ, env, clear=False):
            with self.assertRaisesRegex(RuntimeError, "page chain exceeded request ceiling"):
                loom.poll_guard_events(
                    no_discord=True,
                    now=loom.parse_utc("2026-07-09T20:05:00Z"),
                    request_fn=lambda **kwargs: {"data": [event], "meta": {"next": "next-page"}},
                    process_event_fn=lambda event, **kwargs: processed.append(event["id"]),
                )
        self.assertEqual([], processed)
        self.assertFalse((self.root / "guard-state.json").exists())

    def test_atlassian_events_request_sends_from_as_epoch_milliseconds(self):
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            @staticmethod
            def read():
                return b'{"data": [], "meta": {}}'

        def opener(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return Response()

        result = loom.atlassian_events_request(
            org_id="org-test",
            api_key="key-test",
            action="loom_video_created",
            from_time="2026-07-09T00:00:00Z",
            limit=1,
            opener=opener,
        )
        self.assertEqual([], result["data"])
        self.assertIn("from=1783555200000", captured["url"])
        self.assertNotIn("2026-07-09", captured["url"])
        self.assertEqual(30, captured["timeout"])

    def test_guard_cursor_older_than_retention_fails_closed(self):
        config = json.loads(self.config.read_text())
        config["guard_events"] = {"armed": True, "actions": ["loom_video_created"]}
        self.config.write_text(json.dumps(config))
        (self.root / "guard-state.json").write_text(json.dumps({
            "actions": {"loom_video_created": {"cursor": "old", "watermark": "2025-01-01T00:00:00Z"}}
        }))
        env = {loom.GUARD_ARM_ENV: "YES", "LOOM_ORG_ID": "org-test", "LOOM_ADMIN_API_KEY": "key-test"}
        with mock.patch.dict(loom.os.environ, env, clear=False):
            with self.assertRaisesRegex(RuntimeError, "outside the 180-day retention window"):
                loom.poll_guard_events(now=loom.parse_utc("2026-07-09T20:00:00Z"), request_fn=lambda **kwargs: {})


if __name__ == "__main__":
    unittest.main()
