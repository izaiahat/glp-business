"""Exact-note reconciliation for ambiguous application queue inserts."""
import pytest
from unittest.mock import patch

from ops import n8n_dt
from scripts.cron_ports_sheet import applications as app


ROW = {
    "update_id": "app-note-1", "kind": "application_pending", "network": "fixture",
    "program_id": "1", "program_name": "Fixture", "evidence": "fixture.json",
    "status": "queued", "queued_by": "applications",
    "queued_at_utc": "2026-09-24T12:00:00+00:00", "note": "original note",
}
JOB = {"status": "done", "rc": 0, "receipt_path": "fixture/bridge.json"}
SUMMARY = {"status": "COMPLETE", "queue_rows": [ROW], "submitted": 1,
           "discord_text": "queued"}


def finish_with_landing(tmp_path, landed):
    """Make the first read miss, then simulate a lost insert acknowledgment."""
    import scripts.cron_bridge_action as bridge

    with patch.object(bridge, "machine_receipt", return_value=SUMMARY), \
         patch.object(n8n_dt, "rows", side_effect=[[], landed]) as rows, \
         patch.object(n8n_dt, "insert", side_effect=n8n_dt.WriteReconciliationError("ambiguous")) as insert, \
         patch.object(bridge, "post_once", return_value={"id": "sent"}) as post, \
         patch.object(bridge, "heartbeat", return_value={"receipt": "sent"}) as heartbeat:
        try:
            result = app.finish(app.ACTION, {"mode": "drain"}, JOB, {}, tmp_path / "state.json")
        except Exception as exc:
            result = exc
    assert rows.call_count == 2
    assert insert.call_count == 1  # No replay after ambiguous acknowledgment.
    assert insert.call_args.args[1]["update_id"] == ROW["update_id"]
    return result, post, heartbeat


def test_ambiguous_queued_note_drift_cannot_confirm_other_operation(tmp_path):
    result, post, heartbeat = finish_with_landing(tmp_path, [{**ROW, "note": "other operation"}])
    assert isinstance(result, n8n_dt.WriteReconciliationError)
    post.assert_not_called()
    heartbeat.assert_not_called()


def test_ambiguous_matching_queued_note_confirms_once(tmp_path):
    result, post, heartbeat = finish_with_landing(tmp_path, [dict(ROW)])
    assert result["queue_rows_confirmed"] == 1
    post.assert_called_once()
    heartbeat.assert_called_once()


@pytest.mark.parametrize("status", ["applied", "noop", "refused", "escalated", "deferred"])
def test_ambiguous_resolved_note_can_be_writer_ack(tmp_path, status):
    result, post, heartbeat = finish_with_landing(tmp_path, [{
        **ROW, "status": status, "note": f"{status.upper()}: writer outcome | original note",
        "resolved_at_utc": "2026-09-24T12:01:00+00:00"}])
    assert result["queue_rows_confirmed"] == 1
    post.assert_called_once()
    heartbeat.assert_called_once()


@pytest.mark.parametrize("resolved_at", ["", "2026-09-24T11:59:00+00:00"])
def test_ambiguous_changed_note_without_valid_resolution_is_not_confirmed(tmp_path, resolved_at):
    result, post, heartbeat = finish_with_landing(tmp_path, [{
        **ROW, "status": "applied", "note": "other operation",
        "resolved_at_utc": resolved_at}])
    assert isinstance(result, n8n_dt.WriteReconciliationError)
    post.assert_not_called()
    heartbeat.assert_not_called()


def test_ambiguous_identity_drift_or_duplicate_not_confirmed(tmp_path):
    for landed in ([{**ROW, "evidence": "different.json"}], [ROW, ROW]):
        result, post, heartbeat = finish_with_landing(tmp_path, landed)
        assert isinstance(result, n8n_dt.WriteReconciliationError)
        post.assert_not_called()
        heartbeat.assert_not_called()


def test_existing_resolved_note_uses_same_reconciliation_rule(tmp_path):
    import scripts.cron_bridge_action as bridge

    current = {**ROW, "status": "applied", "note": "APPLIED: writer outcome | original note",
               "resolved_at_utc": "2026-09-24T12:01:00+00:00"}
    with patch.object(bridge, "machine_receipt", return_value=SUMMARY), \
         patch.object(n8n_dt, "rows", return_value=[current]), \
         patch.object(n8n_dt, "insert") as insert, \
         patch.object(bridge, "post_once", return_value={}), \
         patch.object(bridge, "heartbeat", return_value={}):
        result = app.finish(app.ACTION, {"mode": "drain"}, JOB, {}, tmp_path / "state.json")
    assert result["queue_rows_confirmed"] == 1
    insert.assert_not_called()


@pytest.mark.parametrize("current", [
    {**ROW, "note": "other operation"},
    {**ROW, "status": "applied", "note": "other operation", "resolved_at_utc": ""},
])
def test_existing_note_drift_stops_before_reporting(tmp_path, current):
    import scripts.cron_bridge_action as bridge

    with patch.object(bridge, "machine_receipt", return_value=SUMMARY), \
         patch.object(n8n_dt, "rows", return_value=[current]), \
         patch.object(n8n_dt, "insert") as insert, \
         patch.object(bridge, "post_once") as post, \
         patch.object(bridge, "heartbeat") as heartbeat:
        with pytest.raises(RuntimeError, match="applications_queue_existing_note_drift"):
            app.finish(app.ACTION, {"mode": "drain"}, JOB, {}, tmp_path / "state.json")
    insert.assert_not_called()
    post.assert_not_called()
    heartbeat.assert_not_called()
