"""TEST ONLY: synthetic CJ responses; never run the job or contact a provider.

Run with PYTHONPATH=/home/ubuntu/business/scripts to check installed modules,
otherwise with PYTHONPATH=scripts to check the identical curated source in CI.
Requested window metadata is not evidence of an observed complete period.
"""
import builtins
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess

import pytest
import glp_affiliate_weekly_reporting as reporting
import glp_network_revenue_v2 as revenue


@pytest.mark.parametrize("case", [
    "complete_unknown_money", "incomplete_payload", "missing_payload_complete",
    "missing_posting_date", "missing_period_end",
])
def test_labelled_period_fixture_through_real_reporter(tmp_path, monkeypatch, case):
    """One end-to-end regression: response -> collector -> persisted report."""
    class ForbiddenEffect(BaseException):
        pass

    effects = []

    def forbidden(*args, **kwargs):
        effects.append("forbidden_effect")
        raise ForbiddenEffect("fixture must not use network, processes or a database")

    # BaseException deliberately escapes the reporter's safe Exception handlers.
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(os, "system", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(revenue, "Store", forbidden)

    def check_write(path):
        # os.fdopen passes an integer to io.open; bind that FD to its real path.
        if isinstance(path, int):
            path = os.readlink(f"/proc/self/fd/{path}")
        if not Path(path).resolve().is_relative_to(tmp_path.resolve()):
            raise ForbiddenEffect("write outside isolated TEST fixture directory")

    original_os_open = os.open
    original_open = builtins.open
    original_io_open = io.open

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            check_write(path)
        return original_os_open(path, flags, *args, **kwargs)

    def guarded_open(original):
        def call(path, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                check_write(path)
            return original(path, mode, *args, **kwargs)
        return call

    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(builtins, "open", guarded_open(original_open))
    monkeypatch.setattr(io, "open", guarded_open(original_io_open))

    # All identifiers and response fields are synthetic, not account evidence.
    row = {"commissionId": "319001", "advertiserId": "319002",
           "publisherId": "319000", "advertiserName": "TEST fixture only",
           "postingDate": "2026-09-15T12:00:00Z", "actionStatus": "unknown",
           "pubCommissionAmountUsd": None}
    box = {"count": 1, "payloadComplete": True, "records": [row]}
    if case == "incomplete_payload":
        box["payloadComplete"] = False
    elif case == "missing_payload_complete":
        del box["payloadComplete"]
    elif case == "missing_posting_date":
        del row["postingDate"]
    payload = {"data": {"publisherCommissions": box}}
    fixture = {"label": "TEST_ONLY_SYNTHETIC_NO_REAL_EARNINGS", "case": case,
               "as_of": "2026-09-26T00:00:00+00:00", "response": payload,
               "requested_start": "2026-09-14T00:00:00+00:00",
               "requested_end": "" if case == "missing_period_end" else "2026-09-21T00:00:00+00:00"}
    (tmp_path / "INPUT-TEST-ONLY.json").write_text(json.dumps(fixture, indent=2) + "\n")

    class Response:
        status = 200
        body = json.dumps(payload).encode()

        def json(self):
            return payload

    class FixtureTransport:
        calls = 0

        def request(self, method, url, **kwargs):
            self.calls += 1
            assert method == "POST"
            assert url == "https://commissions.api.cj.com/query"
            assert 'sincePostingDate: "2026-09-14T00:00:00Z"' in kwargs["json_body"]["query"]
            assert 'beforePostingDate: "2026-09-21T00:00:00Z"' in kwargs["json_body"]["query"]
            return Response()

    transport = FixtureTransport()

    def collector(window, deadline, max_requests, *, sink):
        assert window["week_start"] == fixture["requested_start"]
        assert window["end_exclusive"] == "2026-09-21T00:00:00+00:00"
        return reporting.collect_cj(
            transport, "TEST_ONLY_NOT_A_CREDENTIAL", "319000",
            fixture["requested_start"], fixture["requested_end"],
            deadline=deadline, max_requests=max_requests, sink=sink)

    # Reproduce the genuine period parser ValueError, not a pin mismatch.
    if case == "missing_period_end":
        with pytest.raises(ValueError, match="Invalid isoformat string"):
            reporting.collect_cj(transport, "TEST_ONLY_NOT_A_CREDENTIAL", "319000",
                                 fixture["requested_start"], fixture["requested_end"],
                                 deadline=0, max_requests=1)
        assert transport.calls == 0

    output = tmp_path / "TEST-ONLY-report"
    result = reporting.run(
        output, {"marketplace_count": 1, "marketplaces": [{"marketplace": "cj"}]}, [],
        as_of=datetime(2026, 9, 26, tzinfo=timezone.utc),
        collectors={"cj": collector}, max_requests=1)
    assert json.loads((output / "RESULT.json").read_text()) == result
    assert result["status"] == "PARTIAL"  # Account read completeness != fleet coverage.
    assert result["coverage_complete"] is False
    assert result["fleet_commission_minor"] is None
    assert result["fleet_cash_minor"] is None
    assert result["measured_epc"] is None
    assert result["payment_candidates"] == []
    assert result["window"]["end_exclusive"] == "2026-09-21T00:00:00+00:00"
    assert effects == []

    if case == "missing_period_end":
        assert result["observations"] == []  # No fabricated observed current period.
        assert result["gaps"] == [{"network": "cj", "state": "SAFE_COLLECTOR_ERROR",
                                   "exception_class": "ValueError"}]
        assert not list(output.glob("cj-page-*.json"))
        assert transport.calls == 0
    else:
        assert transport.calls == 1
        page, summary = result["observations"]
        for index, observation in enumerate(result["observations"], 1):
            assert json.loads((output / f"cj-page-{index:04d}.json").read_text()) == observation
            assert observation["commission_minor"] is None
            assert observation["cash_minor"] is None
            assert observation["account_id"] == "319000"
            assert observation["start_inclusive"] == fixture["requested_start"]
            assert observation["end_exclusive"] == fixture["requested_end"]
        complete = case == "complete_unknown_money"
        assert summary["report_complete_period"] is complete
        assert summary["payload_complete"] is complete
        assert summary["record_count"] == (1 if complete else None)
        assert summary["status"] == ("READ_COMPLETE_ACCOUNT_SCOPE_ONLY" if complete else "PARTIAL")
        gap_states = {gap["state"] for gap in result["gaps"]}
        assert ("PARTIAL_ACCOUNT_REPORT" in gap_states) is not complete
        if case == "missing_posting_date":
            assert page["reason"] == "SAFE_REPORT_ERROR"
            assert page["exception_class"] == "ValueError"
            assert "accounting_projection" not in page
            assert page["record_count"] is None
        else:
            projected = page["accounting_projection"][0]
            assert projected["commission_minor"] is None
            assert projected["classification"] == "UNKNOWN"
            assert projected["attribution"] == "UNKNOWN"
            assert projected["posting_date"] == "2026-09-15T12:00:00.000000+00:00"
        if case in {"incomplete_payload", "missing_payload_complete"}:
            assert page["reason"] == "SUPERSEDED_BY_BOUNDED_TIME_PARTITIONS"
            assert summary["unread_partition_count"] == 2
    assert "TEST_ONLY_NOT_A_CREDENTIAL" not in (output / "RESULT.json").read_text()
