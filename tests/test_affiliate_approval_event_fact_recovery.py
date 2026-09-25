"""Regression for terminal MaxBounty approvals without classification facts."""
import asyncio
import importlib.util
from pathlib import Path
import sqlite3
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/affiliate_approval_auto_reconciler.py"
spec = importlib.util.spec_from_file_location("aar_event_fact_recovery", SCRIPT)
aar = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(aar)


def test_resolved_externally_missing_maxbounty_fact_reaches_recovery(tmp_path, monkeypatch):
    inbox_path = tmp_path / "inbox.sqlite3"
    event_id = "evt_missing_maxbounty_fact"
    with sqlite3.connect(inbox_path) as inbox:
        inbox.executescript("""
            CREATE TABLE inbox_events (
                event_id TEXT PRIMARY KEY, mailbox_id TEXT, provider_message_id TEXT,
                subject TEXT, raw_path TEXT, raw_sha256 TEXT, received_at TEXT, state TEXT
            );
            CREATE TABLE inbox_classifications (
                event_id TEXT, category TEXT, route TEXT, policy_version TEXT
            );
            CREATE TABLE mailbox_actions (event_id TEXT, action TEXT, status TEXT);
            CREATE TABLE affiliate_approval_event_facts (
                event_id TEXT, processing_completed_at_utc TEXT
            );
        """)
        inbox.execute(
            "INSERT INTO inbox_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, "glp_ops_gmail", "message-missing-fact",
             "Your MaxBounty campaign request has been approved",
             str(tmp_path / "approval.eml"), "fixture-digest",
             "2026-09-12T00:00:00Z", "resolved_externally"),
        )
        inbox.execute(
            "INSERT INTO inbox_classifications VALUES (?, ?, ?, ?)",
            (event_id, "affiliate_network_response", "affiliate_approval", "fixture-policy"),
        )
        assert inbox.execute(
            "SELECT COUNT(*) FROM affiliate_approval_event_facts WHERE event_id=?",
            (event_id,),
        ).fetchone()[0] == 0

    monkeypatch.setattr(aar, "INBOX_DB", inbox_path)
    monkeypatch.setattr(aar, "STATE_DB", tmp_path / "reconciler.sqlite3")
    state = aar.init_state()
    gmail = object()
    recovered = {"event_id": event_id, "classification_recovered": True}
    try:
        state.execute(
            "INSERT INTO events(event_id,status,updated_at) VALUES (?, 'blocked', ?)",
            (event_id, "2026-09-12T00:00:00Z"),
        )
        state.commit()
        with mock.patch.object(
            aar, "recover_maxbounty_approval_fact", create=True, return_value=recovered
        ) as recovery, mock.patch.object(
            aar, "reconcile_maxbounty_campaign_approval",
            side_effect=AssertionError("classification recovery must not replay the provider chain"),
        ) as provider_chain:
            events = aar.fetch_inbox_events([event_id])
            assert [event["event_id"] for event in events] == [event_id], (
                "resolved_externally MaxBounty approval with no facts row must be selected"
            )
            event = events[0]
            assert event["missing_approval_fact_recovery"] == 1
            result = asyncio.run(aar.process_event(event, object(), gmail, state, False, {}))
            assert result == recovered
            recovery.assert_called_once_with(event, gmail, state)
            provider_chain.assert_not_called()
    finally:
        state.close()
