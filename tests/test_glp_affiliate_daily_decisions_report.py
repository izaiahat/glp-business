from __future__ import annotations

import datetime as dt
import copy
import importlib.util
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PATH = Path("/home/ubuntu/business/scripts/glp_affiliate_daily_decisions_report.py")
SPEC = importlib.util.spec_from_file_location("glp_daily_report", PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules["glp_daily_report"] = mod
SPEC.loader.exec_module(mod)

RECON_PATH = Path("/home/ubuntu/business/scripts/affiliate_approval_auto_reconciler.py")


def ledger_row(name, *, status="queued_for_portal_execution", blocker="", step="",
               network="awin", lane="glp1-weightloss", submitted=None, blocked_at=""):
    row = {"name": name, "program_id": name, "network": network, "lane": lane,
           "status": status, "route": "portal", "planned_at_utc": "2026-08-06T15:30:00Z"}
    if blocker:
        row["last_blocker"] = blocker
        row["operator_step"] = step or f"finish {name} by hand"
        row["last_blocked_at_utc"] = blocked_at or "2026-08-06T16:00:00Z"
    if submitted:
        row["submitted_at_utc"] = submitted
    return row


class Sandbox(unittest.TestCase):
    def setUp(self):
        import contextlib
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.root = root
        for attr, rel in (
            ("LEDGER_PATH", "applications-ledger.json"),
            ("MINT_QUEUE_PATH", "api-key-minting-queue.json"),
            ("BLOCKER_STORE", "operator-blockers.json"),
            ("STATE_PATH", "daily-report-state.json"),
            ("SITE_CONFIG", "site-config.json"),
        ):
            self.stack.enter_context(_patched(mod, attr, root / rel))
        for attr, rel in (
            ("INTAKE_DIR", "intake"),
            ("REPORT_DIR", "daily-reports"),
            ("RESOLUTIONS_DIR", "resolutions"),
            ("OPERATOR_ACTIONS_DIR", "operator-actions"),
        ):
            self.stack.enter_context(_patched(mod, attr, root / rel))
        self.stack.enter_context(_patched(mod, "INBOX_DB", root / "no-such.sqlite3"))
        research_path = root / "awin-41-stage1-5-research.json"
        research_path.write_bytes(Path(
            "/home/ubuntu/business/reports/glp-clinic-scout/affiliate-machine/bridge-finish-1-20260812/awin-41-stage1-5-research.json"
        ).read_bytes())
        self.stack.enter_context(_patched(mod, "AWIN_TERMS_RESEARCH_PATH", research_path))
        self.write("applications-ledger.json", {"applications": []})

    def write(self, rel, payload):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path


import contextlib


@contextlib.contextmanager
def _patched(obj, attr, value):
    prior = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, prior)


class SnapshotTest(Sandbox):
    def test_pt_day_filter_and_buckets(self):
        now = dt.datetime(2026, 8, 7, 6, 0, tzinfo=dt.timezone.utc)  # Aug 6 PT
        self.write("applications-ledger.json", {"applications": [
            ledger_row("Today", status="SUBMITTED_VERIFIED", submitted="2026-08-06T20:00:00Z"),
            ledger_row("Yesterday", status="SUBMITTED_VERIFIED", submitted="2026-08-05T20:00:00Z"),
            ledger_row("Walled", blocker="AWIN_PORTAL_TOTP"),
            ledger_row("Broken", blocker="AttributeError:'NoneType' object has no attribute 'goto'"),
            ledger_row("Parked", blocker="lane_not_authorized:supplements-vitamins"),
        ]})
        snap = mod.collect_snapshot(now)
        self.assertEqual([r["name"] for r in snap["submitted_today"]], ["Today"])
        self.assertEqual([r["name"] for r in snap["in_review"]], ["Yesterday"])
        self.assertIn("AWIN_PORTAL_TOTP", snap["operator_rows"])
        self.assertEqual(len(snap["machine_defects"]), 1)
        self.assertEqual(len(snap["parked"]), 1)

    def test_routine_legal_and_copy_gaps_are_machine_research_not_operator_blockers(self):
        self.write("applications-ledger.json", {"applications": [
            ledger_row("Terms", blocker="terms_capture_incomplete_nonblocking:provider_copy_missing"),
            ledger_row("Copy", blocker="copy_gap:disclosure_copy_pending"),
            ledger_row("Hard", blocker="legal_terms_hard_trigger:exclusivity"),
        ]})
        snap = mod.collect_snapshot()
        self.assertEqual({row["name"] for row in snap["machine_research"]}, {"Terms", "Copy"})
        self.assertNotIn("terms_capture_incomplete_nonblocking", snap["operator_rows"])
        self.assertNotIn("copy_gap", snap["operator_rows"])
        self.assertIn("legal_terms_hard_trigger", snap["operator_rows"])

    def test_awin_portal_session_hold_is_machine_owned_not_operator_owned(self):
        self.write("applications-ledger.json", {"applications": [
            ledger_row("Power Life", blocker="AWIN_PORTAL_SESSION_REQUIRED"),
        ]})
        snap = mod.collect_snapshot()
        store = mod.sync_blockers(snap, {"blockers": {}})
        blocker = store["blockers"]["BLK-awin-portal-session-required"]
        self.assertEqual(blocker["status"], "machine_owned")
        self.assertFalse(blocker["operator_action_required"])
        self.assertEqual(blocker["ack"], None)
        self.assertNotIn(blocker, mod._open_blockers(store))

    def test_legacy_coreage_routine_terms_seed_is_migrated_off_operator_queue(self):
        legacy = {"blockers": {"BLK-coreage-katalys-terms": {
            "id": "BLK-coreage-katalys-terms", "status": "open", "cause": "legacy routine terms",
            "steps": ["review terms"],
        }}}
        store = mod.seed_manual_blockers(legacy)
        entry = store["blockers"]["BLK-coreage-katalys-terms"]
        self.assertEqual(entry["status"], "machine_research")
        self.assertFalse(entry["operator_action_required"])
        self.assertNotIn("BLK-coreage-katalys-terms", {row["id"] for row in mod._open_blockers(store)})

    def test_stale_telewellness_signature_card_is_superseded_by_adverse_reply(self):
        legacy = {"blockers": {"BLK-docusign-telewellnessmd": {
            "id": "BLK-docusign-telewellnessmd", "status": "open",
            "cause": "legacy signature request", "steps": ["sign"],
        }}}
        store = mod.seed_manual_blockers(legacy)
        entry = store["blockers"]["BLK-docusign-telewellnessmd"]
        self.assertEqual(entry["status"], "superseded_relationship_adverse")
        self.assertEqual(entry["superseded_by_event_id"], "evt_e7740747bf24c940900f895f7156")
        self.assertNotIn("BLK-docusign-telewellnessmd", {row["id"] for row in mod._open_blockers(store)})

    def test_terminal_provider_row_is_not_actionable_review_or_waiting_work(self):
        terminal = ledger_row("Med Weight Clinics", status="terminal_provider_unavailable",
                              blocker="affiliate_signup_transport_broken", network="direct")
        terminal.update({"exclude_from_application_execution": True,
                         "exclude_from_action_cards": True,
                         "exclude_from_waiting_footer": True})
        self.write("applications-ledger.json", {"applications": [terminal]})
        snap = mod.collect_snapshot()
        assert snap["operator_rows"] == {}
        assert snap["in_review"] == []
        store = {"blockers": {"BLK-terminal-med": {
            "id": "BLK-terminal-med", "status": "terminal_provider_unavailable",
            "exclude_from_waiting_footer": True, "source": "operator_terminal_ruling",
            "cause": "provider transport is terminal", "programs": [], "steps": []}}}
        synced = mod.sync_blockers(snap, store)
        assert synced["blockers"]["BLK-terminal-med"]["status"] == "terminal_provider_unavailable"
        rendered = "\n".join(mod.compose_report(snap, synced))
        assert "BLK-terminal-med" not in rendered

    def test_zero_applications_is_normal_and_never_renders_a_deficit(self):
        snap = mod.collect_snapshot()
        rendered = "\n".join(mod.compose_report(snap, {"blockers": {}})).casefold()
        for retired_phrase in ("quota", "shortfall", "applied today"):
            self.assertNotIn(retired_phrase, rendered)
        self.assertIn("awaiting network review: **0**", rendered)
        self.assertIn("approvals recorded today: **0**", rendered)


class SyncTest(Sandbox):
    def test_operator_held_event_is_terminal_for_blocker_projection(self):
        connection = sqlite3.connect(mod.INBOX_DB)
        connection.execute("CREATE TABLE inbox_events(event_id TEXT PRIMARY KEY,state TEXT NOT NULL)")
        connection.execute("INSERT INTO inbox_events(event_id,state) VALUES(?,?)", ("evt_held", "held"))
        connection.commit()
        connection.close()

        self.assertTrue(mod._event_resolved("evt_held"))

    def test_blocker_projection_sources_are_pure_and_match_live_snapshot(self):
        ledger = {"applications": [
            ledger_row("Awin", blocker="AWIN_PUBLISHER_IDENTITY_UNVERIFIED"),
            ledger_row("Research", blocker="incomplete_awaiting_external"),
            ledger_row("Done", status="SUBMITTED_VERIFIED", submitted="2026-08-06T20:00:00Z"),
        ]}
        mint = {"tasks": [
            {"network": "katalys", "program_id": "1503"},
            {"network": "everflow", "program_id": "done", "resolved_at_utc": "2026-08-06T20:00:00Z"},
        ]}
        self.write("applications-ledger.json", ledger)
        self.write("api-key-minting-queue.json", mint)
        before = copy.deepcopy((ledger, mint))
        pure = mod.blocker_projection_sources(ledger, mint)
        live = mod.collect_snapshot()
        for key in ("operator_rows", "machine_defects", "machine_research", "parked", "mint_pending"):
            self.assertEqual(pure[key], live[key])
        self.assertEqual((ledger, mint), before)

    def test_deterministic_sync_replay_uses_supplied_local_evidence_and_fixed_timestamp(self):
        self.write("applications-ledger.json", {"applications": [
            ledger_row("Awin", blocker="AWIN_PUBLISHER_IDENTITY_UNVERIFIED"),
        ]})
        ledger = json.loads((self.root / "applications-ledger.json").read_text())
        mint = {"tasks": [{"network": "katalys", "program_id": "1503"}]}
        snapshot = mod.blocker_projection_sources(ledger, mint)
        opening = {"blockers": {}}
        stamp = "2026-08-12T02:02:15Z"
        kwargs = dict(
            synchronizer_now_utc=stamp, source_ledger=ledger,
            resolution_documents=[], intake_documents=[], resolved_event_ids=set(),
            manually_verified_ids=set(),
        )
        first = mod.sync_blockers(
            snapshot, mod.seed_manual_blockers(copy.deepcopy(opening), synchronizer_now_utc=stamp),
            **kwargs)
        second = mod.sync_blockers(
            snapshot, mod.seed_manual_blockers(copy.deepcopy(opening), synchronizer_now_utc=stamp),
            **kwargs)
        self.assertEqual(first, second)
        self.assertEqual(first["updated_at_utc"], stamp)
        self.assertEqual(first["blockers"]["BLK-awin-publisher-identity-unverified"]["status"],
                         "machine_owned")
        self.assertEqual(first["blockers"]["BLK-katalys-portal-access"]["status"],
                         "machine_owned")

    def test_current_machine_owned_and_waiting_provider_states_never_render_operator_cards(self):
        self.write("applications-ledger.json", {"applications": [
            ledger_row("Awin identity", blocker="AWIN_PUBLISHER_IDENTITY_UNVERIFIED"),
            ledger_row("Awin incomplete", blocker="incomplete_awaiting_external"),
        ]})
        self.write("api-key-minting-queue.json", {"pending_count": 3, "tasks": [
            {"network": "katalys", "program_id": "1503"},
            {"network": "sovrn", "program_id": ""},
            {"network": "direct", "program_id": "PlexusDx"},
            {"network": "awin", "program_id": "resolved", "resolved_at_utc": "2026-08-01T00:00:00Z"},
        ]})
        snapshot = mod.collect_snapshot()
        store = mod.sync_blockers(snapshot, {"blockers": {}})
        assert store["blockers"]["BLK-awin-publisher-identity-unverified"]["status"] == "machine_owned"
        assert store["blockers"]["BLK-incomplete-awaiting-external"]["status"] == "machine_research"
        assert store["blockers"]["BLK-katalys-portal-access"]["status"] == "machine_owned"
        assert store["blockers"]["BLK-sovrn-portal-access"]["status"] == "awaiting_external"
        assert store["blockers"]["BLK-direct-portal-access"]["status"] == "superseded_operator_declined"
        rendered = "\n".join(mod.compose_report(snapshot, store))
        assert "blocked on you: **0**" in rendered
        assert "authenticator app" not in rendered
        assert "sign in once" not in rendered

    def test_resolved_mint_task_is_not_reopened_by_stale_pending_count(self):
        self.write("api-key-minting-queue.json", {"pending_count": 1, "tasks": [
            {"network": "sovrn", "program_id": "", "resolved_at_utc": "2026-08-01T00:00:00Z"},
        ]})
        snapshot = mod.collect_snapshot()
        assert snapshot["mint_pending"] == []
        store = mod.sync_blockers(snapshot, {"blockers": {}})
        assert "BLK-sovrn-portal-access" not in store["blockers"]

    def test_derived_blocker_and_ack_preserved_across_syncs(self):
        self.write("applications-ledger.json", {"applications": [
            ledger_row("Walled", blocker="AWIN_PORTAL_TOTP")]})
        snap = mod.collect_snapshot()
        store = mod.sync_blockers(snap, {"blockers": {}})
        self.assertEqual(store["blockers"]["BLK-awin-totp"]["status"], "machine_owned")
        store["blockers"]["BLK-awin-totp"]["status"] = "open"
        store["blockers"]["BLK-awin-totp"]["ack"] = {"message_id": "1"}
        again = mod.sync_blockers(mod.collect_snapshot(), store)
        self.assertEqual(again["blockers"]["BLK-awin-totp"]["status"], "machine_owned")
        self.assertIsNone(again["blockers"]["BLK-awin-totp"]["ack"])
        self.assertFalse(again["blockers"]["BLK-awin-totp"]["operator_action_required"])

    def test_vanished_ground_truth_verifies_cleared(self):
        self.write("applications-ledger.json", {"applications": [
            ledger_row("Walled", blocker="AWIN_PORTAL_TOTP")]})
        store = mod.sync_blockers(mod.collect_snapshot(), {"blockers": {}})
        self.write("applications-ledger.json", {"applications": [
            ledger_row("Walled", status="SUBMITTED_VERIFIED", submitted="2026-08-06T20:00:00Z")]})
        store = mod.sync_blockers(mod.collect_snapshot(), store)
        self.assertEqual(store["blockers"]["BLK-awin-totp"]["status"], "verified_cleared")

    def test_access_resolution_suppresses_class_and_reports_machine_clear(self):
        self.write("applications-ledger.json", {"applications": [
            ledger_row("Walled", blocker="AWIN_PORTAL_TOTP", blocked_at="2026-08-06T16:00:00Z")]})
        self.write("resolutions/awin-evt_64df.json", {
            "network": "Awin", "case_id": "03120535", "event_id": "evt_64df",
            "resolution": "portal_login_wall_removed_by_network",
            "recorded_at_utc": "2026-08-07T00:30:00Z"})
        store = mod.sync_blockers(mod.collect_snapshot(), {"blockers": {}})
        self.assertNotIn("BLK-awin-totp", {k: v for k, v in store["blockers"].items()
                                           if v.get("status") == "open"})
        self.assertIn("AWIN_PORTAL_TOTP", store["suppressed_classes"])
        self.assertTrue(any(c["id"] == "BLK-awin-totp" for c in store["cleared_by_machine"]))
        self.assertEqual(store["machine_followups"], [])  # fresh policy: no TOTP/2SV mutation

    def test_newer_block_after_resolution_reopens(self):
        self.write("applications-ledger.json", {"applications": [
            ledger_row("Walled", blocker="AWIN_PORTAL_TOTP", blocked_at="2026-08-07T09:30:00Z")]})
        self.write("resolutions/awin-evt_64df.json", {
            "network": "Awin", "case_id": "03120535", "event_id": "evt_64df",
            "resolution": "portal_login_wall_removed_by_network",
            "recorded_at_utc": "2026-08-07T00:30:00Z"})
        store = mod.sync_blockers(mod.collect_snapshot(), {"blockers": {}})
        self.assertEqual(store["blockers"]["BLK-awin-totp"]["status"], "machine_owned")
        self.assertNotIn("AWIN_PORTAL_TOTP", store["suppressed_classes"])

    def test_intake_card_ingested_with_ack_wiring(self):
        self.write("intake/BLK-katalys-abc123.json", {
            "id": "BLK-katalys-abc123", "source": "exception_card",
            "event_id": "evt_x", "exception_type": "account_prerequisite",
            "cause": 'Katalys asks for payment forms in "Next Steps" before payouts can flow',
            "steps": ["open the email", "complete the forms"],
            "programs": ["Katalys"], "keywords": ["katalys"],
            "created_at_utc": "2026-08-07T00:00:00Z"})
        store = mod.sync_blockers(mod.collect_snapshot(), {"blockers": {}})
        entry = store["blockers"]["BLK-katalys-abc123"]
        self.assertEqual(entry["status"], "open")
        self.assertEqual(entry["on_ack"], {"kind": "resolve_event", "event_id": "evt_x"})


class ComposeTest(Sandbox):
    def test_report_lints_chunks_bounded_and_carries_three_blocks(self):
        self.write("applications-ledger.json", {"applications": [
            ledger_row("Winner", status="SUBMITTED_VERIFIED", submitted=mod._now_utc()),
            ledger_row("Walled", blocker="account_password_required")]})
        snap = mod.collect_snapshot()
        store = mod.sync_blockers(snap, mod.seed_manual_blockers({"blockers": {}}))
        chunks = mod.compose_report(snap, store)
        self.assertGreaterEqual(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1900)
        blocker_text = "\n".join(chunks[1:])
        self.assertIn("BLK-account-password", blocker_text)
        self.assertIn("1. ", blocker_text)
        self.assertIn("DONE BLK-account-password", blocker_text)
        self.assertNotIn("planned application(s) held by", blocker_text)

    def test_report_is_lossless_when_blockers_require_more_than_three_chunks(self):
        snap = mod.collect_snapshot()
        blockers = {}
        for index in range(1, 31):
            blocker_id = f"BLK-lossless-{index:02d}"
            blockers[blocker_id] = {
                "id": blocker_id,
                "source": "manual",
                "cause": ("the operator must complete this blocker action before the lane can continue; " * 3).strip(),
                "steps": [(f"complete the numbered operator action for blocker {index} by hand; " * 3).strip()],
                "programs": [f"Program {index}"],
                "status": "open",
            }
        chunks = mod.compose_report(snap, {"blockers": blockers})
        self.assertGreater(len(chunks), 3)
        rendered = "\n".join(chunks)
        for blocker_id in blockers:
            self.assertIn(blocker_id, rendered)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1900)

    def test_one_large_blocker_adapts_chunk_width_without_dropping_programs(self):
        programs = [
            {
                "network": "awin",
                "program_id": str(120000 + index),
                "name": (
                    f"Long Health Marketplace Program {index} With Exact Operator Portal Identity "
                    "And Complete Verified Application State"
                ),
                "lane": "telehealth-general",
            }
            for index in range(35)
        ]
        proof = mod.completeness_proof(programs, expected_denominator=len(programs))
        entry = {
            "id": "BLK-large-lossless",
            "cause": "all planned applications remain behind one verified marketplace identity boundary",
            "steps": [
                "sign in to the verified publisher portal with the account owner session",
                "complete every exact provider program identity listed in all continuation cards",
                "do not substitute a similar merchant name or infer a provider program id",
                "return only after the portal shows one terminal provider state for every identity",
            ],
            "programs": programs,
            "program_count": len(programs),
            "program_completeness": proof,
        }
        chunks = mod._blocker_action_chunks(entry)
        self.assertGreaterEqual(len(chunks), 5)
        first_program_count = sum(line.startswith("• ") for line in chunks[0].splitlines())
        self.assertLess(first_program_count, 8)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 1880)
        mod.validate_rendered_chunks(programs, proof, chunks)

    def test_bare_reason_code_blocker_refuses_to_render(self):
        snap = mod.collect_snapshot()
        store = {"blockers": {"BLK-bare": {"id": "BLK-bare", "source": "manual",
                                          "cause": "short", "steps": [], "status": "open"}}}
        with self.assertRaises(RuntimeError):
            mod.compose_report(snap, store)

    def test_cleared_by_machine_line_appears(self):
        snap = mod.collect_snapshot()
        store = {"blockers": {}, "cleared_by_machine": [
            {"id": "BLK-awin-totp", "at_utc": mod._now_utc(),
             "note": "Awin removed the portal login wall (case 03120535)"}]}
        chunks = mod.compose_report(snap, store)
        self.assertIn("cleared by machine", chunks[0])
        self.assertIn("03120535", chunks[0])

    def test_post_report_uses_new_payload_identity_and_fails_closed_on_existing_drift(self):
        calls = []

        class FakeLedger:
            def __init__(self, _path):
                pass

            def queue_notification(self, event_id, purpose, channel, key, payload, **kwargs):
                calls.append((event_id, purpose, channel, key, payload, kwargs))
                return "out_exact"

            def close(self):
                pass

        class FakeControlPlane:
            DECISIONS_CHANNEL_ID = "decisions"
            Ledger = FakeLedger

        with _patched(mod, "load_control_plane", lambda: FakeControlPlane):
            self.assertEqual(mod.post_report(["exact card"], "2026-08-12", live=True), ["out_exact"])

        self.assertEqual(calls[0][3], "glp-affiliate-daily-report-v4:2026-08-12:p1")
        self.assertEqual(calls[0][4], "exact card")
        self.assertEqual(calls[0][5], {"verify_existing_payload": True})


class AwinHeldTermsTest(Sandbox):
    EXPECTED = {
        "80963": ("ShedRX", "Company may immediately terminate and withhold any payments then due as liquidated damages; Distribution Partner is also responsible for refunding funds paid for previous periods in which Company determines a violation occurred."),
        "115383": ("Biltrx", "HTTP 500: Unable to download the Terms and Conditions PDF. Please try again later."),
        "126727": ("Measured", "AFFILIATE agrees to indemnify ADVERTISER for third-party liability connected with breach/fraud and intellectual-property claims associated with copy and images supplied by ADVERTISER and AFFILIATE'S usage thereof."),
    }

    def _store(self):
        return mod.seed_manual_blockers({"blockers": {}}, synchronizer_now_utc="2026-08-12T22:00:00Z")

    def test_exact_three_hash_pinned_cards_seed_with_exact_evidence_and_dual_replies(self):
        store = self._store()
        held = {key: value for key, value in store["blockers"].items()
                if key.startswith("BLK-awin-terms-")}
        self.assertEqual(set(held), {f"BLK-awin-terms-{pid}" for pid in self.EXPECTED})
        for program_id, (name, evidence) in self.EXPECTED.items():
            entry = held[f"BLK-awin-terms-{program_id}"]
            self.assertEqual(entry["terms_program_name"], name)
            self.assertEqual(entry["terms_evidence"], evidence)
            self.assertIn(evidence, entry["cause"])
            self.assertIn("Existing live links are preserved; only future material expansion is held.", entry["cause"])
            self.assertEqual(entry["reply_syntax"], [
                f"ACCEPT-TERMS {program_id}", f"REJECT-TERMS {program_id}"])
            self.assertEqual(entry["source_research_sha256"], mod.AWIN_TERMS_RESEARCH_SHA256)
            self.assertEqual(entry["status"], "open")
        rendered = "\n".join(mod.compose_report(mod.collect_snapshot(), store))
        for program_id in self.EXPECTED:
            self.assertIn(f"reply: `ACCEPT-TERMS {program_id}`", rendered)
            self.assertIn(f"reply: `REJECT-TERMS {program_id}`", rendered)
            self.assertNotIn(f"DONE BLK-awin-terms-{program_id}", rendered)
        self.assertIn("each blocker card's exact reply syntax", rendered)

    def test_source_hash_drift_fails_closed(self):
        self.root.joinpath("awin-41-stage1-5-research.json").write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "awin_terms_research_hash_mismatch"):
            self._store()

    def test_exact_parser_only_decides_matching_known_program(self):
        store = self._store()
        open_ids = {entry["id"]: {entry["id"].lower()} for entry in mod._open_blockers(store)}
        exact = mod.parse_acks("ACCEPT-TERMS 80963", open_ids, message_id="m80963")
        self.assertEqual(exact["acks"], {"BLK-awin-terms-80963": "terms_exact"})
        self.assertEqual(exact["terms_decision_lines"], {
            "BLK-awin-terms-80963": {"verb": "ACCEPT", "line": "ACCEPT-TERMS 80963"}})
        reject = mod.parse_acks("REJECT-TERMS 80963", open_ids, message_id="m80963r")
        self.assertEqual(reject["acks"], {"BLK-awin-terms-80963": "terms_exact"})
        self.assertEqual(reject["terms_decision_lines"], {
            "BLK-awin-terms-80963": {"verb": "REJECT", "line": "REJECT-TERMS 80963"}})
        for text in (
            "accept-terms 80963", "ACCEPT-TERMS 80963 ", "ACCEPT TERMS 80963",
            "reject-terms 80963", "REJECT-TERMS 80963 ", "REJECT TERMS 80963",
            "DONE BLK-awin-terms-80963", "done accepting ShedRX terms",
            "ACCEPT-TERMS 99999", "ACCEPT-TERMS 115383 now", "REJECT-TERMS 99999",
        ):
            parsed = mod.parse_acks(text, open_ids, message_id="near-miss")
            self.assertEqual(parsed["acks"], {}, text)
        only_other = {"BLK-awin-terms-115383": {"blk-awin-terms-115383"}}
        self.assertEqual(mod.parse_acks("ACCEPT-TERMS 80963", only_other)["acks"], {})

    def test_live_ack_prefilter_routes_exact_terms_reply_to_parser(self):
        class FakeLedger:
            def __init__(self, _path):
                pass

            def queue_notification(self, *_args, **_kwargs):
                return "out_confirmation"

            def close(self):
                pass

        class FakeControlPlane:
            OPERATOR_ID = "operator"
            DECISIONS_CHANNEL_ID = "decisions"
            Ledger = FakeLedger

        message = {
            "id": "1536000000000000042",
            "author": {"id": "operator"},
            "content": "ACCEPT-TERMS 80963",
        }
        result = mod._ack_sync_under_guard(
            cp=FakeControlPlane,
            messages=[message],
            live=True,
            now=dt.datetime(2026, 8, 12, 22, 30, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["applied_ids"], ["BLK-awin-terms-80963"])
        receipts = list(mod.OPERATOR_ACTIONS_DIR.glob("awin-terms-80963-*.json"))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0].stat().st_mode & 0o777, 0o600)
        receipt = json.loads(receipts[0].read_text())
        self.assertEqual(receipt["source_message_id"], message["id"])
        self.assertEqual(receipt["decision_text"], message["content"])
        self.assertEqual(receipt["operator_words"], message["content"])
        self.assertEqual(receipt["decision"], "operator_accepted_terms")
        self.assertFalse(receipt["provider_write"])

    def test_receipt_is_mode_0600_message_program_evidence_words_and_provenance_bound(self):
        store = self._store()
        entry = store["blockers"]["BLK-awin-terms-80963"]
        result = mod._write_terms_acceptance_receipt(
            entry, message_id="1536000000000000001", accepted_text="ACCEPT-TERMS 80963",
            recorded_at_utc="2026-08-12T22:05:00Z")
        path = Path(result["receipt"])
        receipt = json.loads(path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt["schema"], "glp-awin-terms-decision-v2")
        self.assertEqual(receipt["decision"], "operator_accepted_terms")
        self.assertEqual(receipt["program_id"], "80963")
        self.assertEqual(receipt["source_message_id"], "1536000000000000001")
        self.assertEqual(receipt["decision_text"], "ACCEPT-TERMS 80963")
        self.assertEqual(receipt["operator_words"], "ACCEPT-TERMS 80963")
        self.assertEqual(receipt["provenance"], "discord-message-1536000000000000001")
        self.assertEqual(receipt["terms_evidence_sha256"], entry["terms_evidence_sha256"])
        self.assertFalse(receipt["provider_write"])
        self.assertTrue(mod._manual_verified(entry))
        entry["status"] = "operator_says_done"
        synced = mod.sync_blockers(mod.collect_snapshot(), store)
        self.assertEqual(synced["blockers"][entry["id"]]["status"], "verified_cleared")

    def test_different_program_and_stale_evidence_never_verify(self):
        store = self._store()
        source = store["blockers"]["BLK-awin-terms-80963"]
        mod._write_terms_acceptance_receipt(
            source, message_id="1536000000000000002", accepted_text="ACCEPT-TERMS 80963",
            recorded_at_utc="2026-08-12T22:05:00Z")
        other = store["blockers"]["BLK-awin-terms-115383"]
        self.assertFalse(mod._manual_verified(other))
        stale = copy.deepcopy(source)
        stale["terms_evidence_sha256"] = "0" * 64
        self.assertFalse(mod._manual_verified(stale))
        with self.assertRaisesRegex(RuntimeError, "awin_terms_decision_stale_evidence"):
            mod._write_terms_acceptance_receipt(
                stale, message_id="1536000000000000003", accepted_text="ACCEPT-TERMS 80963")

    def test_v1_acceptance_receipts_remain_current_and_replay_idempotently(self):
        store = self._store()
        entry = store["blockers"]["BLK-awin-terms-80963"]
        source = Path(
            "/home/ubuntu/business/reports/glp-affiliate-auto-reconciler/operator-actions/"
            "awin-terms-80963-16f018b7b29ed64c.json")
        target = mod.OPERATOR_ACTIONS_DIR / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
        current = mod._current_terms_decision(entry)
        self.assertEqual(current["schema"], "glp-awin-terms-acceptance-v1")
        self.assertEqual(current["decision"], "operator_accepted_terms")
        self.assertTrue(mod._manual_verified(entry))
        before = target.read_bytes()
        replay = mod._write_terms_acceptance_receipt(
            entry, message_id="1537243566156546078", accepted_text="ACCEPT-TERMS 80963")
        self.assertFalse(replay["created"])
        self.assertEqual(target.read_bytes(), before)

    def test_newest_decision_wins_and_later_accept_and_reject_reverse_each_other(self):
        store = self._store()
        entry = store["blockers"]["BLK-awin-terms-126727"]
        mod._write_terms_decision_receipt(
            entry, decision="operator_accepted_terms", decision_text="ACCEPT-TERMS 126727",
            operator_words="ACCEPT-TERMS 126727", provenance="fixture-accept-1",
            recorded_at_utc="2026-08-12T20:00:00Z")
        self.assertEqual(mod._current_terms_decision(entry)["decision"], "operator_accepted_terms")
        mod._write_terms_decision_receipt(
            entry, decision="operator_rejected_terms", decision_text="REJECT-TERMS 126727",
            operator_words="measured has bad terms, and I wanna reject it",
            provenance="fixture-reject-2", recorded_at_utc="2026-08-12T21:00:00Z")
        self.assertEqual(mod._current_terms_decision(entry)["decision"], "operator_rejected_terms")
        self.assertFalse(mod._manual_verified(entry))
        mod._write_terms_decision_receipt(
            entry, decision="operator_accepted_terms", decision_text="ACCEPT-TERMS 126727",
            operator_words="ACCEPT-TERMS 126727", provenance="fixture-accept-3",
            recorded_at_utc="2026-08-12T22:00:00Z")
        self.assertEqual(mod._current_terms_decision(entry)["decision"], "operator_accepted_terms")
        self.assertTrue(mod._manual_verified(entry))

    def test_replay_conflict_and_cross_program_or_stale_evidence_fail_closed(self):
        store = self._store()
        entry = store["blockers"]["BLK-awin-terms-126727"]
        first = mod._write_terms_decision_receipt(
            entry, decision="operator_rejected_terms", decision_text="REJECT-TERMS 126727",
            operator_words="measured has bad terms, and I wanna reject it",
            provenance="bridge-session-20260812", recorded_at_utc="2026-08-13T00:00:00Z")
        before = Path(first["receipt"]).read_bytes()
        replay = mod._write_terms_decision_receipt(
            entry, decision="operator_rejected_terms", decision_text="REJECT-TERMS 126727",
            operator_words="measured has bad terms, and I wanna reject it",
            provenance="bridge-session-20260812", recorded_at_utc="2026-08-13T00:00:00Z")
        self.assertFalse(replay["created"])
        self.assertEqual(Path(first["receipt"]).read_bytes(), before)
        with self.assertRaisesRegex(RuntimeError, "awin_terms_decision_receipt_conflict"):
            mod._write_terms_decision_receipt(
                entry, decision="operator_accepted_terms", decision_text="ACCEPT-TERMS 126727",
                operator_words="ACCEPT-TERMS 126727", provenance="bridge-session-20260812",
                recorded_at_utc="2026-08-13T00:01:00Z")
        with self.assertRaisesRegex(RuntimeError, "awin_terms_decision_not_exact"):
            mod._write_terms_decision_receipt(
                entry, decision="operator_rejected_terms", decision_text="REJECT-TERMS 115383",
                operator_words="REJECT-TERMS 115383", provenance="cross-program")
        stale = copy.deepcopy(entry)
        stale["terms_evidence_sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "awin_terms_decision_stale_evidence"):
            mod._write_terms_decision_receipt(
                stale, decision="operator_rejected_terms", decision_text="REJECT-TERMS 126727",
                operator_words="REJECT-TERMS 126727", provenance="stale")

    def test_rejected_decision_remains_visible_reversible_and_not_open(self):
        store = self._store()
        entry = store["blockers"]["BLK-awin-terms-126727"]
        mod._write_terms_decision_receipt(
            entry, decision="operator_rejected_terms", decision_text="REJECT-TERMS 126727",
            operator_words="measured has bad terms, and I wanna reject it",
            provenance="bridge-session-20260812", recorded_at_utc="2026-08-13T00:00:00Z")
        projected = self._store()
        measured = projected["blockers"]["BLK-awin-terms-126727"]
        self.assertEqual(measured["status"], "operator_rejected_terms")
        self.assertFalse(measured["operator_action_required"])
        self.assertNotIn(measured["id"], {row["id"] for row in mod._open_blockers(projected)})
        rendered = "\n".join(mod.compose_report(mod.collect_snapshot(), projected))
        self.assertIn("operator_rejected_terms", rendered)
        self.assertIn("measured has bad terms, and I wanna reject it", rendered)
        self.assertIn("bridge-session-20260812", rendered)
        self.assertIn("ACCEPT-TERMS 126727", rendered)
        self.assertIn(self.EXPECTED["126727"][1], rendered)

    def test_live_reject_routes_to_v2_receipt_without_provider_effect(self):
        class FakeLedger:
            def __init__(self, _path): pass
            def queue_notification(self, *_args, **_kwargs): return "out_confirmation"
            def close(self): pass

        class FakeControlPlane:
            OPERATOR_ID = "operator"
            DECISIONS_CHANNEL_ID = "decisions"
            Ledger = FakeLedger

        message = {"id": "1536000000000000099", "author": {"id": "operator"},
                   "content": "REJECT-TERMS 126727"}
        result = mod._ack_sync_under_guard(
            cp=FakeControlPlane, messages=[message], live=True,
            now=dt.datetime(2026, 8, 13, 0, 30, tzinfo=dt.timezone.utc))
        self.assertEqual(result["applied_ids"], ["BLK-awin-terms-126727"])
        receipt_path = next(mod.OPERATOR_ACTIONS_DIR.glob("awin-terms-126727-*.json"))
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["decision"], "operator_rejected_terms")
        self.assertEqual(receipt["decision_text"], "REJECT-TERMS 126727")
        self.assertFalse(receipt["provider_write"])


class AckParseTest(unittest.TestCase):
    OPEN = {"BLK-awin-totp": {"awin", "totp", "blk-awin-totp"},
            "BLK-katalys-w9": {"katalys", "deposit", "blk-katalys-w9"}}

    def test_exact(self):
        self.assertEqual(mod.parse_ack("DONE BLK-awin-totp", self.OPEN), ("BLK-awin-totp", "exact"))
        self.assertEqual(mod.parse_ack("done blk-katalys-w9", self.OPEN), ("BLK-katalys-w9", "exact"))

    def test_free_text_single_match(self):
        blocker, kind = mod.parse_ack("just finished the awin totp thing", self.OPEN)
        self.assertEqual((blocker, kind), ("BLK-awin-totp", "inferred"))

    def test_ambiguous_never_guesses(self):
        blocker, kind = mod.parse_ack(
            "done with the awin totp app and the katalys deposit forms", self.OPEN)
        self.assertIsNone(blocker)
        self.assertEqual(kind, "ambiguous")

    def test_single_stray_keyword_is_no_action(self):
        blocker, kind = mod.parse_ack("done — also check katalys later", self.OPEN)
        self.assertIsNone(blocker)
        self.assertEqual(kind, "none")

    def test_unrelated_ignored(self):
        self.assertEqual(mod.parse_ack("APPROVE haus-of-aesthetics", self.OPEN), (None, "none"))
        self.assertEqual(mod.parse_ack("DONE BLK-not-open", self.OPEN), (None, "ambiguous"))


class MultiAckParseTest(unittest.TestCase):
    """The operator's preferred reply shape (2026-08-07): one big message, many
    DONE lines, narrative feedback interleaved — every ack lands, feedback and
    ambiguity are surfaced, quoting instructions never acks."""
    OPEN = {"BLK-awin-totp": {"awin", "totp", "blk-awin-totp"},
            "BLK-katalys-w9": {"katalys", "deposit", "blk-katalys-w9"},
            "BLK-docusign-x": {"docusign", "agreement", "blk-docusign-x"}}

    BIG = (
        "ok knocked most of these out tonight:\n"
        "DONE BLK-awin-totp\n"
        "done blk-katalys-w9\n"
        "also finished the BLK-docusign-x one\n"
        "btw the report should group portal blockers by network next time\n"
        "and the reply line should sit at the top of each card imo\n"
    )

    def test_big_message_acks_every_blocker(self):
        parsed = mod.parse_acks(self.BIG, self.OPEN)
        self.assertEqual(parsed["acks"], {"BLK-awin-totp": "exact",
                                          "BLK-katalys-w9": "exact",
                                          "BLK-docusign-x": "inferred"})
        self.assertEqual(parsed["ambiguous_lines"], [])
        self.assertEqual(len(parsed["narrative_lines"]), 3)
        self.assertIn("group portal blockers", parsed["narrative_lines"][1])

    def test_two_ids_on_one_done_line_both_ack(self):
        parsed = mod.parse_acks("done with BLK-awin-totp and BLK-katalys-w9",
                                self.OPEN)
        self.assertEqual(set(parsed["acks"]), {"BLK-awin-totp", "BLK-katalys-w9"})

    def test_quoted_instructions_without_verb_never_ack(self):
        parsed = mod.parse_acks("reply: DONE-ish? the card said BLK-awin-totp somewhere",
                                self.OPEN)
        self.assertEqual(parsed["acks"], {})

    def test_keyword_line_straddling_blockers_is_ambiguous_not_guessed(self):
        parsed = mod.parse_acks("done with the awin totp app and the katalys deposit forms",
                                self.OPEN)
        self.assertEqual(parsed["acks"], {})
        self.assertEqual(len(parsed["ambiguous_lines"]), 1)

    def test_unknown_done_id_is_ambiguous(self):
        parsed = mod.parse_acks("DONE BLK-not-open", self.OPEN)
        self.assertEqual(parsed["acks"], {})
        self.assertEqual(len(parsed["ambiguous_lines"]), 1)


class ReconcilerRegressionTest(unittest.TestCase):
    """detect() must classify access-resolution mail as account_access_resolved,
    and the exception card composer must refuse bare reason codes."""

    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("recon_under_test", RECON_PATH)
        assert spec and spec.loader
        cls.recon = importlib.util.module_from_spec(spec)
        sys.modules["recon_under_test"] = cls.recon
        spec.loader.exec_module(cls.recon)

    def test_awin_2sv_disabled_is_access_resolved_not_tax_kyc(self):
        body = ("Hello Izaiah, We have now disabled two-step verification (2SV) for your user "
                "as requested. You can log in using only your username and password. Case 03120535.")
        candidate = self.recon.detect(
            "Your Awin Support Case 03120535", "Awin Partner Success <success@awin.com>", body)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["action"], "account_access_resolved")
        self.assertEqual(candidate.get("case_id"), "03120535")

    def test_katalys_payment_prereq_still_prerequisite(self):
        body = ("Next steps for Application ID 46734157436: please complete the W-9 form and the "
                "Direct Deposit form so we can process your payments.")
        self.assertTrue(self.recon.OPERATOR_PREREQUISITE_RE.search(body))
        self.assertFalse(
            self.recon.detect("Katalys Onboarding | Next Steps - Payment Information",
                              "ap@katalys.com", body)
            and self.recon.detect("Katalys Onboarding | Next Steps - Payment Information",
                                  "ap@katalys.com", body)["action"] == "account_access_resolved")

    def test_exception_card_carries_three_blocks(self):
        class Exc:
            exception_type = "account_prerequisite"
            detail = "tax_kyc_identity_or_payout_action_required"
        context = {"subject": "Katalys Onboarding | Next Steps - Payment Information",
                   "sender": "ap@katalys.com", "date": "2026-07-14",
                   "network": "Katalys", "program": "Katalys"}
        blocker_id, message = self.recon.compose_affiliate_exception_card(
            {"event_id": "evt_075fc289cce733d083c5afc9726a"}, {"network": "Katalys"}, Exc(), context)
        self.assertTrue(blocker_id.startswith("BLK-katalys-"))
        self.assertIn("WHAT'S WRONG:", message)
        self.assertIn("W-9", message)
        self.assertIn("1. ", message)
        self.assertIn(f"DONE {blocker_id}", message)
        self.assertNotIn("Exception: `", message)

    def test_exception_card_refuses_empty_evidence(self):
        class Exc:
            exception_type = "mystery_exception"
            detail = "code"
        context = {"subject": "", "sender": "", "date": "", "network": "", "program": ""}
        with self.assertRaises(RuntimeError):
            self.recon.compose_affiliate_exception_card({"event_id": "evt_1"}, {"network": ""}, Exc(), context)


class Defect2And7Regression(Sandbox):
    def test_pending_awaiting_and_no_response_create_awaiting_external_and_are_excluded_from_action_cards(self):
        self.write("applications-ledger.json", {"applications": [ledger_row("Everflow", blocker="EVERFLOW_PORTAL_PENDING")]})
        snap = mod.collect_snapshot()
        store = mod.sync_blockers(snap, {"blockers": {}})
        blocker_id = next(iter(store["blockers"]))
        parsed = mod.parse_acks(
            f"{blocker_id} is still pending and awaiting a network response; no response yet", 
            {blocker_id: set(store["blockers"][blocker_id]["keywords"])})
        mod.apply_awaiting_external_updates(store, snap, message_id="fixture-msg", 
                                            state_updates=parsed["state_update_lines"],
                                            captured_at_utc="2026-08-08T10:00:00Z")
        entry = store["blockers"][blocker_id]
        self.assertEqual(entry["status"], "awaiting_external")
        self.assertEqual(entry["awaiting_external"]["source_message_id"], "fixture-msg")
        rendered = "\n".join(mod.compose_report(snap, store))
        self.assertIn("waiting on networks/system", rendered)
        self.assertNotIn(f"reply: `DONE {blocker_id}`", rendered)

    def test_awaiting_external_clears_or_reopens_only_from_a_bound_machine_observation(self):
        self.write("applications-ledger.json", {"applications": [ledger_row("Everflow", blocker="EVERFLOW_PORTAL_PENDING", blocked_at="2026-08-08T09:00:00Z")]})
        store = mod.sync_blockers(mod.collect_snapshot(), {"blockers": {}})
        blocker_id = next(iter(store["blockers"]))
        update = [{"blocker_id": blocker_id, "state_kind": "awaiting_external", "operator_words": "still pending"}]
        mod.apply_awaiting_external_updates(store, mod.collect_snapshot(), message_id="m1", state_updates=update, captured_at_utc="2026-08-08T10:00:00Z")
        unchanged = mod.sync_blockers(mod.collect_snapshot(), store)
        self.assertEqual(unchanged["blockers"][blocker_id]["status"], "awaiting_external")
        self.write("applications-ledger.json", {"applications": [ledger_row("Everflow", blocker="EVERFLOW_PORTAL_PENDING", blocked_at="2026-08-08T11:00:00Z")]})
        reopened = mod.sync_blockers(mod.collect_snapshot(), unchanged)
        self.assertEqual(reopened["blockers"][blocker_id]["status"], "open")
        mod.apply_awaiting_external_updates(reopened, mod.collect_snapshot(), message_id="m2", state_updates=update, captured_at_utc="2026-08-08T12:00:00Z")
        self.write("applications-ledger.json", {"applications": [ledger_row("Everflow", status="SUBMITTED_VERIFIED", submitted="2026-08-08T13:00:00Z")]})
        self.assertEqual(mod.sync_blockers(mod.collect_snapshot(), reopened)["blockers"][blocker_id]["status"], "verified_cleared")

    def test_negated_ack_verbs_never_ack_in_parse_ack_or_parse_acks(self):
        open_ids = {"BLK-awin-totp": {"awin", "totp", "blk-awin-totp"}}
        for line in ("not done BLK-awin-totp", "didn't finish BLK-awin-totp", "never done BLK-awin-totp", "still pending BLK-awin-totp", "not yet done BLK-awin-totp"):
            self.assertEqual(mod.parse_ack(line, open_ids)[0], None)
            parsed = mod.parse_acks(line, open_ids)
            self.assertEqual(parsed["acks"], {})
            self.assertTrue(parsed["narrative_lines"] or parsed["state_update_lines"]
                            or parsed["operational_rulings"])

    def test_replay_1535423795576311848_returns_zero_acks_and_two_non_ack_outputs(self):
        payload = json.loads((Path("/home/ubuntu/business/tests/fixtures/glp_affiliate_ack_replay_1535423795576311848.json")).read_text())
        open_ids = {"BLK-everflow-portal-access": {"everflow", "portal", "blk-everflow-portal-access"},
                    "BLK-no-application-url-on-row": {"url", "row", "blk-no-application-url-on-row"}}
        parsed = mod.replay_operator_message_source(payload, "1535423795576311848", open_ids)
        self.assertEqual(parsed["acks"], {})
        self.assertEqual(len(parsed["state_update_lines"]), 2)
        self.assertEqual(len(parsed["narrative_lines"]), 2)

    def test_operator_feedback_capture_preserves_exact_1540120879093125140_blk_and_all_source_lines(self):
        payload = json.loads(Path(
            "/home/ubuntu/business/tests/fixtures/"
            "glp_affiliate_operator_feedback_1540120879093125140.json"
        ).read_text())
        lines = mod.operator_feedback_capture_lines(payload["content"])
        self.assertEqual(len(lines), payload["expected_nonempty_line_count"])
        self.assertIn(payload["expected_blk_line"], lines)
        self.assertEqual(len(lines), len([
            line for line in payload["content"].splitlines() if line.strip()
        ]))

    def test_allowlisted_concise_and_substantive_exact_one_blk_instructions_persist_once(self):
        known = {"BLK-x": {"blk-x", "x"}}
        concise = mod.parse_acks("BLK-x - skip", known, message_id="200001")
        self.assertEqual([row["state_kind"] for row in concise["state_update_lines"]],
                         ["operator_declined"])
        substantive = mod.parse_acks("BLK-x - use the exact API workflow", known,
                                     message_id="200002")
        self.assertEqual(len(substantive["operational_rulings"]), 1)
        self.assertEqual(substantive["narrative_lines"], [])
        duplicate = mod.parse_acks(
            "BLK-x - use the exact API workflow\nBLK-x - use the exact API workflow",
            known, message_id="200002")
        self.assertEqual(len(duplicate["operational_rulings"]), 1)
        self.assertEqual(duplicate["duplicate_instruction_line_count"], 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = mod.load_operator_rulings().write_immutable_ruling_document(
                "200002", ruled_at_utc="2026-08-11T00:00:00Z", root=root,
                operational_rulings=duplicate["operational_rulings"])
            before = Path(first["path"]).read_bytes()
            second = mod.load_operator_rulings().write_immutable_ruling_document(
                "200002", ruled_at_utc="2026-08-11T00:00:00Z", root=root,
                operational_rulings=duplicate["operational_rulings"])
            self.assertFalse(second["created"])
            self.assertEqual(Path(second["path"]).read_bytes(), before)

    def test_negated_decline_and_reopen_are_durable_but_cannot_reverse_state(self):
        known = {"BLK-x": {"blk-x", "x"}}
        for text in ("BLK-x do not skip this", "BLK-x do not reopen this"):
            parsed = mod.parse_acks(text, known, message_id="200003")
            self.assertEqual(parsed["state_update_lines"], [])
            self.assertEqual(len(parsed["operational_rulings"]), 1)
            self.assertEqual(parsed["narrative_lines"], [])

    def test_same_line_and_cross_line_semantic_conflicts_fail_closed_before_effects(self):
        known = {"BLK-x": {"blk-x", "x"}}
        cases = (
            "BLK-x skip this and reopen it now",
            "BLK-x do not skip this; skip this",
            "BLK-x - skip\nBLK-x - reopen and resume this now",
            "BLK-x do not reopen this\nBLK-x reopen this now",
        )
        for text in cases:
            parsed = mod.parse_acks(text, known, message_id="200004")
            self.assertEqual(parsed["state_update_lines"], [], text)
            self.assertEqual(parsed["operational_rulings"], [], text)
            self.assertEqual(parsed["queue_requests"], [], text)
            self.assertEqual(parsed["acks"], {}, text)
            self.assertTrue(parsed["ambiguous_lines"], text)

    def test_cross_line_exact_47_positive_negative_conflict_emits_no_queue_or_ruling(self):
        blocker = "BLK-incomplete-awaiting-external"
        known = {blocker: {blocker.lower(), "incomplete"}}
        parsed = mod.parse_acks(
            f"{blocker} confirm and join all 47 pending programs\n"
            f"{blocker} do not join the 47 yet",
            known, message_id="200005")
        self.assertEqual(parsed["queue_requests"], [])
        self.assertEqual(parsed["operational_rulings"], [])
        self.assertTrue(parsed["ambiguous_lines"])


if __name__ == "__main__":
    unittest.main()
