from __future__ import annotations

import datetime as dt
import hashlib
import json
import struct
import sys
from pathlib import Path
from unittest import mock

import pytest
from googleapiclient.errors import HttpError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import glp_research_autowire as aw  # noqa: E402


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, prompt, model=None, timeout_s=None, callback_url=""):
        self.calls.append({
            "prompt": prompt, "model": model, "timeout_s": timeout_s,
            "callback_url": callback_url,
        })
        return {"job_id": "job-research-1", "status": "running"}


def row(*, status="ready_to_apply", slug="clinic-review", program="Clinic",
        official="https://clinic.example/", network="Test Network",
        vertical="glp1-weightloss"):
    values = [""] * len(aw.sheet_writer.HEADERS)
    cells = {
        "Program / Network": program,
        "Network / Route": network,
        "Official URL": official,
        "Fit Reason": "Provider offers clinician-guided GLP-1 care.",
        "Suggested GLP Page / Asset": "Provider review",
        "SEO Slug": slug,
        "Status": status,
        "Signup Complete?": True,
        "Wire-In Ready?": True,
        "Wired Live?": False,
        "Link QA Status": "not_clicked_preserve_attribution",
        "Vertical": vertical,
        "Approval Date": "2026-08-27",
    }
    for name, value in cells.items():
        values[aw.sheet_writer.HEADERS.index(name)] = value
    return values


def sheet(*rows):
    return [list(aw.sheet_writer.HEADERS), *rows]


class FakeGoogleResponse(dict):
    def __init__(self, status, **headers):
        super().__init__(headers)
        self.status = status
        self.reason = "test response"


class FakeGoogleRequest:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def execute(self):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def google_error(status, **headers):
    return HttpError(FakeGoogleResponse(status, **headers), b'{"error": {"message": "test"}}')


@pytest.fixture
def machine(tmp_path, monkeypatch):
    business = tmp_path / "business"
    business.mkdir()
    root = business / "reports/research-autowire"
    packet_root = business / "packets"
    gate = business / "affiliate-apply-gate.json"
    gate.write_text(json.dumps({"schema": "affiliate-apply-gate-v1", "paused": True}) + "\n")
    monkeypatch.setattr(aw, "BUSINESS", business)
    monkeypatch.setattr(aw, "ROOT", root)
    monkeypatch.setattr(aw, "STATE_PATH", root / "state.json")
    monkeypatch.setattr(aw, "LOCK_PATH", root / "state.lock")
    monkeypatch.setattr(aw, "AUTHORING_STATE_PATH", root / "authoring-ledger.json")
    monkeypatch.setattr(aw, "PACKET_ROOT", packet_root)
    monkeypatch.setattr(aw, "PLAN_ROOT", root / "plans")
    monkeypatch.setattr(aw, "EVENT_ROOT", root / "events")
    monkeypatch.setattr(aw, "BACKRUN_ROOT", business / "reports/affiliate-machine")
    monkeypatch.setattr(aw, "BACKRUN_SCRIPT", business / "scripts/affiliate_backrun.py")
    monkeypatch.setattr(aw, "GATE_PATH", gate)
    monkeypatch.setattr(aw.apply_gate, "BUSINESS", business)
    monkeypatch.setattr(aw.authoring, "ROOT", root / "authoring")
    monkeypatch.setattr(aw.authoring, "prior_fingerprints", lambda _provider: {})
    monkeypatch.setattr(aw, "network_program_identity", lambda _row: {
        "network": "testnetwork", "program_id": "42", "key": "testnetwork:42",
    })
    return {"business": business, "root": root, "packet_root": packet_root, "gate": gate}


def test_sheet_read_retries_429_then_succeeds():
    request = FakeGoogleRequest([google_error(429), {"values": [aw.sheet_writer.HEADERS]}])
    sleep = mock.Mock()

    result = aw.execute_sheet_read(request, sleep=sleep)

    assert result == {"values": [aw.sheet_writer.HEADERS]}
    assert request.calls == 2
    sleep.assert_called_once_with(aw.SHEET_READ_RETRY_DELAYS_SECONDS[0])


def test_sheet_read_does_not_retry_non_transient_error():
    request = FakeGoogleRequest([google_error(403)])
    sleep = mock.Mock()

    with pytest.raises(HttpError) as raised:
        aw.execute_sheet_read(request, sleep=sleep)

    assert raised.value.resp.status == 403
    assert request.calls == 1
    sleep.assert_not_called()


def test_sheet_read_retry_is_bounded():
    attempts = len(aw.SHEET_READ_RETRY_DELAYS_SECONDS) + 1
    request = FakeGoogleRequest([google_error(503) for _ in range(attempts)])
    sleep = mock.Mock()

    with pytest.raises(HttpError) as raised:
        aw.execute_sheet_read(request, sleep=sleep)

    assert raised.value.resp.status == 503
    assert request.calls == attempts
    assert [call.args[0] for call in sleep.call_args_list] == list(
        aw.SHEET_READ_RETRY_DELAYS_SECONDS)


def test_sheet_read_retry_outlasts_quota_window():
    assert sum(aw.SHEET_READ_RETRY_DELAYS_SECONDS) > 60


def test_sheet_read_honours_bounded_retry_after():
    request = FakeGoogleRequest([
        google_error(429, **{"retry-after": "30"}),
        {"values": [aw.sheet_writer.HEADERS]},
    ])
    sleep = mock.Mock()

    aw.execute_sheet_read(request, sleep=sleep)

    sleep.assert_called_once_with(30)


def approval_event(values, event_id="simulated-approval-20260827"):
    event = aw.event_from_row(2, values[1])
    event["approval_event_id"] = event_id
    return event


def write_operator_maxbounty_gate(machine, *, max_applications=1,
                                  attempts_consumed=0, queue_ids=None,
                                  networks=None, newly_authorized_attempts=None):
    asset = machine["business"] / "creative.png"
    asset.write_bytes(
        b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 1272, 1600))
    scope = {
        "networks_allowed": networks or ["maxbounty"],
        "max_applications": max_applications,
    }
    if attempts_consumed:
        scope["attempts_consumed_before_this_decision"] = attempts_consumed
    if queue_ids:
        scope["queue_ids_allowed"] = queue_ids
        scope["newly_authorized_attempts"] = (
            len(queue_ids) if newly_authorized_attempts is None
            else newly_authorized_attempts
        )
    machine["gate"].write_text(json.dumps({
        "schema": "affiliate-apply-gate-v1",
        "paused": False,
        "scope": scope,
        "approved_assets": [{
            "asset": "maxbounty_advertising_image",
            "path": "creative.png",
            "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
            "dimensions": "1272x1600",
            "approved_by": "operator (Izaiah Tilton), test fixture",
        }],
        "set_by": "Kael, recording the operator-authorized exact scope; authority is the operator",
    }))


def make_packet(machine, *, slug="clinic-review", quote="Exact evidence quote",
                capture_text=None, hash_drift=False):
    capture_text = capture_text or (
        "Exact evidence quote. Semaglutide oral treatment is described here. "
        "This is the calculator evidence passage with enough context for a manual entry."
    )
    capture = machine["business"] / "captures" / f"{slug}-captures.json"
    capture.parent.mkdir(parents=True, exist_ok=True)
    capture.write_text(json.dumps({
        "schema": "g6-r1a-stage5-capture-v1",
        "program_id": slug,
        "captures": [{
            "url": "https://clinic.example/",
            "status": "ok",
            "capture_route": "firecrawl",
            "markdown": capture_text,
        }],
        "tracking_clicks": 0,
    }, indent=2) + "\n")
    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    evidence_text = capture_text
    required = sorted({
        "verified_as_of", "taxonomy.molecule_tags", "taxonomy.brand_name_tags",
        "taxonomy.delivery_form_tags", "taxonomy.treatment_type_tags", "calculator.mode",
        "calculator.first_month_base_usd", "calculator.recurring_month_base_usd",
        "calculator.one_time_fee_usd", "calculator.price_context", "calculator.source_url",
        "calculator.checked_as_of", "calculator.evidence_gap_copy",
    })
    fields = {
        name: {
            "quote": quote,
            "source_url": "https://clinic.example/",
            "value": "UNKNOWN — public source does not establish this exact fact.",
            "verified_as_of": today,
        }
        for name in sorted(aw.EXPECTED_FIELDS)
    }
    packet = {
        "schema": "program-deep-research-v1",
        "status": "PASS_LIMITED",
        "program_id": slug,
        "display_name": "Clinic",
        "captured_at_utc": aw.utc_now(),
        "official_url": "https://clinic.example/",
        "effects": {"tracking_clicks": 0, "tracking_requests": 0,
                    "forms_submitted": 0, "accounts_created": 0},
        "fields": fields,
        "first_party_source_captures": {
            "path": str(capture), "bytes": capture.stat().st_size,
            "sha256": "0" * 64 if hash_drift else aw.sha_file(capture),
        },
        "provider_dynamic_profile": {
            "version": 1,
            "verified_as_of": today,
            "taxonomy": {
                "molecule_tags": ["semaglutide"],
                "brand_name_tags": [],
                "delivery_form_tags": ["oral"],
                "treatment_type_tags": [],
            },
            "calculator": {
                "mode": "manual", "first_month_base_usd": None,
                "recurring_month_base_usd": None, "one_time_fee_usd": None,
                "price_context": "The public page does not establish one comparable total.",
                "source_url": "https://clinic.example/", "checked_as_of": today,
                "evidence_gap_copy": "Enter exact first and recurring amounts manually after verification.",
            },
            "evidence_bindings": [{
                "fields": required,
                "evidence_text": evidence_text,
                "evidence_sha256": hashlib.sha256(evidence_text.encode()).hexdigest(),
                "source_url": "https://clinic.example/", "verified_as_of": today,
            }],
        },
    }
    path = machine["packet_root"] / f"{slug}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(packet, indent=2, sort_keys=True) + "\n")
    return path, capture


def make_legacy_packet(machine, *, slug="clinic-review"):
    capture = machine["business"] / "legacy-captures" / f"{slug}.json"
    capture.parent.mkdir(parents=True, exist_ok=True)
    capture.write_text(json.dumps({"markdown": "Legacy first-party capture"}) + "\n")
    capture_sha256 = aw.sha_file(capture)
    fields = {
        name: {
            "value": f"Legacy {name}", "quote": f"Legacy quote for {name}",
            "source_url": "https://clinic.example/", "verified_as_of": "2026-08-27",
            "source_capture_path": str(capture),
            "source_capture_sha256": capture_sha256,
        }
        for name in sorted(aw.EXPECTED_FIELDS)
    }
    packet = {
        "schema": "program-deep-research-v1", "program_name": "Clinic",
        "program_id": slug, "slug": slug, "title": "Clinic review",
        "researched_at": "2026-08-27T22:56:04Z", "field_count": len(fields),
        "fields": fields, "sources": ["https://clinic.example/"],
        "research_status": "complete",
        "effects": {"tracking_clicks": 0, "tracking_links_requested": 0,
                    "applications_submitted": 0},
    }
    path = machine["packet_root"] / f"{slug}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(packet, indent=2) + "\n")
    return path, capture


def test_baseline_is_read_only_and_gate_must_be_shut(machine):
    values = sheet(row())
    result = aw.baseline(values=values)
    assert result["status"] == "PASS_BASELINED"
    assert result["gate"]["paused"] is True
    assert result["effects"]["sheet_writes"] == 0
    machine["gate"].write_text(json.dumps({"paused": False, "open": True}))
    with pytest.raises(aw.AutowireError, match="apply_gate_not_shut"):
        aw.baseline(values=values)


def test_exact_operator_scoped_maxbounty_gate_is_safe_for_research(machine):
    write_operator_maxbounty_gate(machine)
    result = aw.baseline(values=sheet(row()))
    assert result["status"] == "PASS_BASELINED"
    assert result["gate"]["semantic_open"] is True
    assert result["gate"]["scope"] == {
        "networks_allowed": ["maxbounty"], "max_applications": 1,
        "attempts_consumed_before_this_decision": 0,
        "queue_ids_allowed_count": 0,
    }
    assert result["effects"]["provider_mutations"] == 0
    assert result["effects"]["sheet_writes"] == 0


def test_maxbounty_scope_without_operator_proof_still_fails_closed(machine):
    machine["gate"].write_text(json.dumps({
        "schema": "affiliate-apply-gate-v1",
        "paused": False,
        "scope": {"networks_allowed": ["maxbounty"], "max_applications": 1},
        "approved_assets": [],
        "set_by": "automation",
    }))
    with pytest.raises(aw.AutowireError, match="apply_gate_not_shut_or_safely_scoped"):
        aw.baseline(values=sheet(row()))


def test_exact_operator_scoped_maxbounty_backlog_gate_is_safe_for_research(machine):
    queue_ids = [f"maxbounty-{index}-staged-20260826" for index in range(17)]
    write_operator_maxbounty_gate(
        machine, max_applications=22, attempts_consumed=5, queue_ids=queue_ids)
    result = aw.baseline(values=sheet(row()))
    assert result["status"] == "PASS_BASELINED"
    assert result["gate"]["scope"] == {
        "networks_allowed": ["maxbounty"],
        "max_applications": 22,
        "attempts_consumed_before_this_decision": 5,
        "queue_ids_allowed_count": 17,
    }
    assert result["effects"]["provider_mutations"] == 0
    assert result["effects"]["sheet_writes"] == 0


def test_operator_added_candidate_does_not_expand_attempt_capacity(machine):
    queue_ids = [f"maxbounty-{index}-staged-20260826" for index in range(17)]
    queue_ids.append("maxbounty-24169-staged-20260901")
    write_operator_maxbounty_gate(
        machine, max_applications=22, attempts_consumed=5, queue_ids=queue_ids,
        newly_authorized_attempts=17)

    result = aw.baseline(values=sheet(row()))

    assert result["status"] == "PASS_BASELINED"
    assert result["gate"]["scope"] == {
        "networks_allowed": ["maxbounty"],
        "max_applications": 22,
        "attempts_consumed_before_this_decision": 5,
        "queue_ids_allowed_count": 18,
    }
    assert result["effects"]["provider_mutations"] == 0
    assert result["effects"]["sheet_writes"] == 0


def test_candidate_allowlist_cannot_expand_attempt_capacity(machine):
    queue_ids = [f"maxbounty-{index}-staged-20260826" for index in range(17)]
    queue_ids.append("maxbounty-24169-staged-20260901")
    write_operator_maxbounty_gate(
        machine, max_applications=22, attempts_consumed=5, queue_ids=queue_ids,
        newly_authorized_attempts=999)

    result = aw.baseline(values=sheet(row()))

    assert result["status"] == "PASS_BASELINED"
    assert result["gate"]["scope"]["max_applications"] == 22
    assert result["gate"]["scope"]["queue_ids_allowed_count"] == 18


def test_operator_cap_revision_ignores_stale_transition_metadata(machine):
    queue_ids = [f"maxbounty-{index}-staged-20260826" for index in range(17)]
    queue_ids.extend([
        "maxbounty-24169-staged-20260901",
        "maxbounty-33024-staged-20260902",
        "maxbounty-33066-staged-20260902",
    ])
    write_operator_maxbounty_gate(
        machine, max_applications=24, attempts_consumed=5, queue_ids=queue_ids,
        newly_authorized_attempts=17)

    result = aw.baseline(values=sheet(row()))

    assert result["status"] == "PASS_BASELINED"
    assert result["gate"]["scope"] == {
        "networks_allowed": ["maxbounty"],
        "max_applications": 24,
        "attempts_consumed_before_this_decision": 5,
        "queue_ids_allowed_count": 20,
    }
    assert result["effects"]["provider_mutations"] == 0
    assert result["effects"]["sheet_writes"] == 0


def test_invalid_canonical_attempt_cap_still_fails_closed(machine):
    queue_ids = [f"maxbounty-{index}-staged-20260826" for index in range(4)]
    write_operator_maxbounty_gate(
        machine, max_applications=4, attempts_consumed=5, queue_ids=queue_ids,
        newly_authorized_attempts=999)

    with pytest.raises(aw.AutowireError, match="apply_gate_not_shut_or_safely_scoped"):
        aw.baseline(values=sheet(row()))


def test_non_maxbounty_queue_identity_still_fails_closed(machine):
    queue_ids = [f"maxbounty-{index}-staged-20260826" for index in range(16)]
    queue_ids.append("direct-24169-staged-20260901")
    write_operator_maxbounty_gate(
        machine, max_applications=22, attempts_consumed=5, queue_ids=queue_ids,
        newly_authorized_attempts=17)

    with pytest.raises(aw.AutowireError, match="apply_gate_not_shut_or_safely_scoped"):
        aw.baseline(values=sheet(row()))


def test_broader_operator_scope_still_fails_closed(machine):
    write_operator_maxbounty_gate(
        machine, max_applications=2, queue_ids=["q-1", "q-2"],
        networks=["maxbounty", "direct"])
    with pytest.raises(aw.AutowireError, match="apply_gate_not_shut_or_safely_scoped"):
        aw.baseline(values=sheet(row()))


def test_exact_sheet_slug_dispatches_one_sol_research_job(machine):
    pending = sheet(row())
    aw.baseline(values=pending)
    approved = sheet(row(status="approved_active_tracking_ready"))
    recorder = Recorder()
    result = aw.dispatch(values=approved, callback_url="https://resume.example/1",
                         bridge_post=recorder)
    assert result["status"] == "DISPATCHED"
    assert result["seo_slug"] == "clinic-review"
    assert result["engine"] == "hermes_chat"
    assert result["reasoning"] == "xhigh"
    assert len(recorder.calls) == 1
    assert "slug: `clinic-review`" in recorder.calls[0]["prompt"]
    assert recorder.calls[0]["callback_url"] == "https://resume.example/1"


def test_simultaneous_approvals_are_queued_and_drained_without_loss(machine, monkeypatch):
    pending = sheet(
        row(slug="clinic-one-review", program="Clinic One"),
        row(slug="clinic-two-review", program="Clinic Two"),
    )
    aw.baseline(values=pending)
    approved = sheet(
        row(status="approved_active_tracking_ready", slug="clinic-one-review",
            program="Clinic One"),
        row(status="approved_active_tracking_ready", slug="clinic-two-review",
            program="Clinic Two"),
    )
    make_packet(machine, slug="clinic-one-review")
    monkeypatch.setattr(aw, "run_wire_plan", lambda _event_id, packet, _event: {
        "path": str(machine["root"] / "plans" / "plan.json"),
        "sha256": "a" * 64, "status": "PASS", "packet_sightings": [str(packet)],
        "packet_seen": True, "queue_count": 1, "effects": {"deployments": 0},
    })
    recorder = Recorder()

    first = aw.dispatch(values=approved, callback_url="https://resume.example/1",
                        bridge_post=recorder)
    second = aw.dispatch(values=approved, callback_url="https://resume.example/2",
                         bridge_post=recorder)

    assert first["status"] == "SKIP_EXISTING_VALID"
    assert first["seo_slug"] == "clinic-one-review"
    assert second["status"] == "DISPATCHED"
    assert second["seo_slug"] == "clinic-two-review"
    assert len(recorder.calls) == 1
    state = aw.load_state()
    assert len(state["events"]) == 2
    assert sorted(record["status"] for record in state["events"].values()) == [
        "COMPLETED", "IN_FLIGHT",
    ]


def test_invalid_approval_is_receipted_and_does_not_poison_pending_queue(machine):
    pending = sheet(
        row(slug="Invalid Slug", program="Broken Clinic"),
        row(slug="clinic-two-review", program="Clinic Two"),
    )
    aw.baseline(values=pending)
    approved = sheet(
        row(status="approved_active_tracking_ready", slug="Invalid Slug",
            program="Broken Clinic"),
        row(status="approved_active_tracking_ready", slug="clinic-two-review",
            program="Clinic Two"),
    )
    recorder = Recorder()

    failed = aw.dispatch(values=approved, callback_url="https://resume.example/1",
                         bridge_post=recorder)
    next_event = aw.dispatch(values=approved, callback_url="https://resume.example/2",
                             bridge_post=recorder)

    assert failed["status"] == "FAILED"
    assert failed["why"].startswith("approval_validation_failed:sheet_seo_slug_invalid")
    assert Path(failed["receipt"]).is_file()
    assert next_event["status"] == "DISPATCHED"
    assert next_event["seo_slug"] == "clinic-two-review"
    assert len(recorder.calls) == 1
    state = aw.load_state()
    assert sorted(record["status"] for record in state["events"].values()) == [
        "FAILED", "IN_FLIGHT",
    ]


@pytest.mark.parametrize("current_slug", ["", "Invalid Slug", "../unsafe"])
def test_exhausted_blank_slug_still_invalid_is_preserved(machine, current_slug):
    started = dt.datetime(2026, 9, 5, 19, 0, tzinfo=dt.timezone.utc)
    aw.baseline(values=sheet(row(slug="", program="Broken Clinic")))
    approved = sheet(row(
        status="approved_active_not_wired", slug="", program="Broken Clinic"))
    recorder = Recorder()
    failed = aw.dispatch(
        values=approved, callback_url="https://resume.example/1",
        bridge_post=recorder, now=started)
    state = aw.load_state()
    state["events"][failed["event_id"]].update({
        "attempts": aw.MAX_ATTEMPTS, "next_retry_utc": "",
    })
    before = aw.canonical(state)
    current = sheet(row(
        status="approved_active_not_wired", slug=current_slug,
        program="Broken Clinic"))

    recovered = aw._recover_exhausted_site_identity_record(state, current, started)

    assert recovered is None
    assert aw.canonical(state) == before
    assert recorder.calls == []


def test_exhausted_blank_slug_does_not_block_later_valid_approval(machine):
    started = dt.datetime(2026, 9, 5, 19, 0, tzinfo=dt.timezone.utc)
    aw.baseline(values=sheet(
        row(slug="", program="Broken Clinic"),
        row(slug="clinic-two-review", program="Clinic Two"),
    ))
    approved = sheet(
        row(status="approved_active_not_wired", slug="", program="Broken Clinic"),
        row(status="approved_active_not_wired", slug="clinic-two-review",
            program="Clinic Two"),
    )
    recorder = Recorder()
    failed = aw.dispatch(
        values=approved, callback_url="https://resume.example/1",
        bridge_post=recorder, now=started)
    state = aw.load_state()
    state["events"][failed["event_id"]].update({
        "attempts": aw.MAX_ATTEMPTS, "next_retry_utc": "",
    })
    frozen_failure = aw.canonical(state["events"][failed["event_id"]])
    aw.save_state(state)

    next_event = aw.dispatch(
        values=approved, callback_url="https://resume.example/2",
        bridge_post=recorder, now=started + dt.timedelta(hours=1))

    assert next_event["status"] == "DISPATCHED"
    assert next_event["seo_slug"] == "clinic-two-review"
    assert len(recorder.calls) == 1
    final = aw.load_state()
    assert len(final["events"]) == 2
    assert aw.canonical(final["events"][failed["event_id"]]) == frozen_failure


def test_failed_blank_slug_creates_successor_preserving_terminal_event(machine):
    started = dt.datetime(2026, 9, 1, 3, 0, tzinfo=dt.timezone.utc)
    pending = sheet(row(slug="", program="EOSHealth"))
    aw.baseline(values=pending)
    approved_blank = sheet(row(
        status="approved_active_not_wired", slug="", program="EOSHealth"))
    recorder = Recorder()

    failed = aw.dispatch(
        values=approved_blank, callback_url="https://resume.example/1",
        bridge_post=recorder, now=started)
    state = aw.load_state()
    state["events"][failed["event_id"]].update({
        "attempts": aw.MAX_ATTEMPTS,
        "next_retry_utc": "",
    })
    frozen_failure = aw.canonical(state["events"][failed["event_id"]])
    aw.save_state(state)
    corrected = sheet(row(
        status="approved_active_not_wired", slug="eoshealth-review",
        program="EOSHealth"))
    retried = aw.dispatch(
        values=corrected, callback_url="https://resume.example/2",
        bridge_post=recorder, now=started + dt.timedelta(hours=1))

    assert failed["status"] == "FAILED"
    assert retried["status"] == "DISPATCHED"
    assert retried["event_id"] != failed["event_id"]
    assert retried["event_id"].startswith("ra-slug-")
    assert retried["seo_slug"] == "eoshealth-review"
    state = aw.load_state()
    assert len(state["events"]) == 2
    assert aw.canonical(state["events"][failed["event_id"]]) == frozen_failure
    successor = state["events"][retried["event_id"]]
    assert successor["status"] == "IN_FLIGHT"
    assert successor["event"]["source"] == "governed_sheet_slug_backlog"
    assert len(recorder.calls) == 1


def test_prewire_packet_binding_requires_exact_approved_dark_row(machine, tmp_path):
    backrun = tmp_path / "backrun"
    backrun.mkdir()
    headers = list(aw.sheet_writer.HEADERS)
    values = row(
        status="approved_active_not_wired", slug="eoshealth-review",
        program="EOSHealth")
    values[headers.index("Wire-In Ready?")] = False
    values[headers.index("Wired Live?")] = False
    (backrun / "sheet-full-row-export.json").write_text(json.dumps({
        "headers": headers,
        "rows": {"707": values},
    }))

    event = aw.event_from_row(707, values)
    proof = aw.prewire_packet_binding(backrun, event)

    assert proof["status"] == "PASS_PRE_WIRE_GATE"
    assert proof["sheet_row"] == 707
    assert proof["seo_slug"] == "eoshealth-review"
    assert proof["wire_in_ready"] is False
    assert proof["wired_live"] is False


@pytest.mark.parametrize(
    ("status", "wire_ready", "wired_live"),
    [
        ("ready_to_apply", False, False),
        ("approved_active_not_wired", True, False),
        ("approved_active_wired_live", True, True),
    ],
)
def test_prewire_packet_binding_refuses_nonapproved_or_wired_rows(
        machine, tmp_path, status, wire_ready, wired_live):
    backrun = tmp_path / f"backrun-{status}-{wire_ready}-{wired_live}"
    backrun.mkdir()
    headers = list(aw.sheet_writer.HEADERS)
    values = row(status=status, slug="eoshealth-review", program="EOSHealth")
    values[headers.index("Wire-In Ready?")] = wire_ready
    values[headers.index("Wired Live?")] = wired_live
    (backrun / "sheet-full-row-export.json").write_text(json.dumps({
        "headers": headers,
        "rows": {"707": values},
    }))

    event = aw.event_from_row(707, values)
    assert aw.prewire_packet_binding(backrun, event) is None


def test_failed_blank_slug_stays_terminal_after_governed_sheet_disposition(machine):
    started = dt.datetime(2026, 9, 1, 3, 0, tzinfo=dt.timezone.utc)
    pending = sheet(row(slug="", program="Goodness Lover"))
    aw.baseline(values=pending)
    approved_blank = sheet(row(
        status="approved_active_not_wired", slug="", program="Goodness Lover"))
    failed = aw.dispatch(
        values=approved_blank, callback_url="https://resume.example/1",
        bridge_post=lambda *_args, **_kwargs: pytest.fail("must not dispatch"),
        now=started)
    state = aw.load_state()
    state["events"][failed["event_id"]].update({
        "attempts": aw.MAX_ATTEMPTS,
        "next_retry_utc": "",
    })
    frozen_failure = aw.canonical(state["events"][failed["event_id"]])
    aw.save_state(state)
    dispositioned = sheet(row(
        status="approved_active_not_wired", slug="", program="Goodness Lover",
        vertical="out-of-scope"))

    result = aw.dispatch(
        values=dispositioned, callback_url="https://resume.example/2",
        bridge_post=lambda *_args, **_kwargs: pytest.fail("must not dispatch"),
        now=started + dt.timedelta(hours=1))

    assert failed["status"] == "FAILED"
    assert result["status"] == "PASS_NO_NEW_APPROVAL"
    record = aw.load_state()["events"][failed["event_id"]]
    assert aw.canonical(record) == frozen_failure
    assert record["status"] == "FAILED"
    assert record["event"]["seo_slug"] == ""
    with pytest.raises(aw.AutowireError, match="sheet_seo_slug_invalid"):
        aw.validate_slug("")


def test_new_out_of_scope_approval_never_enters_site_research_queue(machine):
    pending = sheet(row(slug="", program="Goodness Lover", vertical="out-of-scope"))
    aw.baseline(values=pending)
    approved = sheet(row(
        status="approved_active_not_wired", slug="", program="Goodness Lover",
        vertical="out-of-scope"))

    result = aw.dispatch(
        values=approved, callback_url="https://resume.example/1",
        bridge_post=lambda *_args, **_kwargs: pytest.fail("must not dispatch"))

    assert result["status"] == "PASS_NO_NEW_APPROVAL"
    assert aw.load_state()["events"] == {}
    with pytest.raises(aw.AutowireError, match="sheet_seo_slug_invalid"):
        aw.validate_slug("")


def test_tick_reconciles_inflight_without_redispatch(machine):
    approved = sheet(row(status="approved_active_tracking_ready"))
    aw.baseline(values=approved)
    recorder = Recorder()
    first = aw.dispatch(
        event=approval_event(approved), simulated=True, values=approved,
        callback_url="https://resume.example/1", bridge_post=recorder)
    calls = []

    result = aw.tick(
        callback_url="https://resume.example/2", values=approved,
        bridge_post=lambda *_args, **_kwargs: pytest.fail("must not redispatch"),
        job_status=lambda job_id: calls.append(job_id) or {
            "job_id": job_id, "status": "running", "rc": None,
        },
    )

    assert first["status"] == "DISPATCHED"
    assert result["status"] == "WAIT_IN_FLIGHT"
    assert result["event_id"] == first["event_id"]
    assert calls == [first["job_id"]]
    assert len(recorder.calls) == 1


def test_reconcile_without_inflight_is_noop(machine):
    aw.baseline(values=sheet(row()))
    result = aw.reconcile(job_status=lambda _job_id: pytest.fail("no job read expected"))
    assert result["status"] == "PASS_NO_IN_FLIGHT"
    assert result["effects"]["deployments"] == 0


def test_callback_free_tick_no_approval_is_no_effect_fixture(machine):
    current = sheet(row())
    aw.baseline(values=current)
    result = aw.tick(values=current,
                     bridge_post=lambda *_a, **_kw: pytest.fail("no dispatch"),
                     job_status=lambda _id: pytest.fail("no poll"))
    assert result["status"] == "PASS_NO_NEW_APPROVAL"
    assert result["dispatched"] is False
    assert all(result["effects"][key] == 0 for key in (
        "sheet_writes", "provider_mutations", "deployments",
        "tracking_requests", "tracking_clicks"))
    assert aw.load_state()["events"] == {}
    assert Path(result["receipt"]).is_file()


def test_callback_free_tick_polls_exact_job_and_drains_queued_approvals(
        machine, monkeypatch):
    pending = sheet(row(slug="clinic-one-review", program="Clinic One"),
                    row(slug="clinic-two-review", program="Clinic Two"))
    aw.baseline(values=pending)
    approved = sheet(row(status="approved_active_tracking_ready",
                         slug="clinic-one-review", program="Clinic One"),
                     row(status="approved_active_tracking_ready",
                         slug="clinic-two-review", program="Clinic Two"))
    recorder = Recorder()
    first = aw.tick(values=approved, bridge_post=recorder)
    assert first["status"] == "DISPATCHED"
    assert recorder.calls[0]["callback_url"] == ""
    assert len(aw.load_state()["events"]) == 2
    partial = aw.tick(values=approved, bridge_post=recorder,
                      job_status=lambda job_id: {"job_id": job_id,
                                                 "status": "done", "rc": 0})
    assert partial["status"] == "WAIT_IN_FLIGHT"
    wrong = aw.tick(values=approved, bridge_post=recorder,
                    job_status=lambda _job_id: {"job_id": "sibling", "status": "done",
                                                "rc": 0, "finished_utc": "2026-09-24T00:00:00Z"})
    assert wrong["status"] == "WAIT_IN_FLIGHT"
    assert len(recorder.calls) == 1
    monkeypatch.setattr(aw.authoring, "ingest", lambda *_a, **_kw: {
        "ingested": True, "agent_verdict": "authored"})
    monkeypatch.setattr(aw, "validate_packet", lambda *_a, **_kw: {
        "packet_sha256": "a" * 64, "capture_binding": {"sha256": "b" * 64}})
    monkeypatch.setattr(aw, "run_wire_plan", lambda *_a, **_kw: {
        "path": "fixture-plan", "sha256": "c" * 64, "packet_bound": True})
    completed = aw.tick(values=approved, bridge_post=recorder,
                        job_status=lambda job_id: {"job_id": job_id, "status": "done",
                                                   "rc": 0, "finished_utc": "2026-09-24T00:00:00Z"})
    assert completed["status"] == "COMPLETED"
    assert Path(completed["receipt"]).is_file()
    # The mocked ingest's real counterpart also settles the authoring ledger.
    ledger = aw.authoring.load_state(aw.AUTHORING_STATE_PATH)
    ledger["providers"]["clinic-one-review"]["cycles"][-1]["status"] = "RESOLVED"
    aw.authoring.save_state(ledger, aw.AUTHORING_STATE_PATH)
    second = aw.tick(values=approved, bridge_post=recorder)
    assert second["status"] == "DISPATCHED", second
    assert second["event_id"] != first["event_id"]
    assert len(recorder.calls) == 2
    assert all(call["callback_url"] == "" for call in recorder.calls)
    assert all(completed["effects"][key] == 0 for key in (
        "sheet_writes", "provider_mutations", "deployments",
        "tracking_requests", "tracking_clicks"))


def test_callback_free_tick_recovers_claim_from_authoring_ledger_without_repost(machine):
    pending = sheet(row())
    approved = sheet(row(status="approved_active_tracking_ready"))
    aw.baseline(values=pending)
    recorder = Recorder()
    first = aw.tick(values=approved, bridge_post=recorder)
    assert first["status"] == "DISPATCHED"
    state = aw.load_state()
    record = state["events"][first["event_id"]]
    record.update(status="CLAIMED", job_id="")
    aw.save_state(state)
    waiting = aw.tick(values=approved, bridge_post=recorder,
                      job_status=lambda job_id: {"job_id": job_id, "status": "running"})
    assert waiting["status"] == "WAIT_IN_FLIGHT"
    assert waiting["job_id"] == first["job_id"]
    assert aw.load_state()["events"][first["event_id"]]["status"] == "IN_FLIGHT"
    assert len(recorder.calls) == 1


def test_callback_free_tick_retains_ambiguous_claim_without_dispatch(machine):
    pending = sheet(row())
    approved = sheet(row(status="approved_active_tracking_ready"))
    aw.baseline(values=pending)
    first = aw.tick(values=approved, bridge_post=Recorder())
    state = aw.load_state()
    state["events"][first["event_id"]].update(status="CLAIMED", job_id="")
    aw.save_state(state)
    aw.AUTHORING_STATE_PATH.unlink()
    result = aw.tick(values=approved,
                     bridge_post=lambda *_a, **_kw: pytest.fail("ambiguous POST"))
    assert result["status"] == "WAIT_IN_FLIGHT"
    assert result["job_status"] == "claim_ambiguous"
    assert aw.load_state()["events"][first["event_id"]]["status"] == "CLAIMED"


def test_callback_free_tick_finishes_after_ingest_commit_before_autowire_commit(
        machine, monkeypatch):
    pending = sheet(row())
    approved = sheet(row(status="approved_active_tracking_ready"))
    aw.baseline(values=pending)
    recorder = Recorder()
    first = aw.tick(values=approved, bridge_post=recorder)
    ledger = aw.authoring.load_state(aw.AUTHORING_STATE_PATH)
    cycle = ledger["providers"]["clinic-review"]["cycles"][-1]
    cycle.update(status="RESOLVED", agent_verdict="authored")
    aw.authoring.save_state(ledger, aw.AUTHORING_STATE_PATH)
    monkeypatch.setattr(aw.authoring, "ingest", lambda *_a, **_kw: {
        "ingested": False, "reason_code": "not_in_flight"})
    monkeypatch.setattr(aw, "validate_packet", lambda *_a, **_kw: {
        "packet_sha256": "a" * 64, "capture_binding": {"sha256": "b" * 64}})
    monkeypatch.setattr(aw, "run_wire_plan", lambda *_a, **_kw: {
        "path": "fixture-plan", "sha256": "c" * 64, "packet_bound": True})
    result = aw.tick(values=approved, bridge_post=recorder,
                     job_status=lambda job_id: {"job_id": job_id, "status": "done",
                                                "rc": 0, "finished_utc": "2026-09-24T00:00:00Z"})
    assert result["status"] == "COMPLETED"
    assert result["job_id"] == first["job_id"]
    assert len(recorder.calls) == 1


def test_direct_dispatch_still_requires_callback_and_webhook_retains_it(machine):
    pending = sheet(row())
    approved = sheet(row(status="approved_active_tracking_ready"))
    aw.baseline(values=pending)
    with pytest.raises(aw.AutowireError, match="callback_url_required"):
        aw.dispatch(values=approved, bridge_post=Recorder())
    recorder = Recorder()
    result = aw.tick(values=approved, callback_url="https://resume.example/1",
                     bridge_post=recorder)
    assert result["status"] == "DISPATCHED"
    assert recorder.calls[0]["callback_url"] == "https://resume.example/1"


def test_same_event_while_in_flight_is_durable_skip(machine):
    approved = sheet(row(status="approved_active_tracking_ready"))
    aw.baseline(values=approved)
    event = approval_event(approved)
    recorder = Recorder()
    first = aw.dispatch(event=event, simulated=True, values=approved,
                        callback_url="https://resume.example/1", bridge_post=recorder)
    second = aw.dispatch(event=event, simulated=True, values=approved,
                         callback_url="https://resume.example/2", bridge_post=recorder)
    assert first["status"] == "DISPATCHED"
    assert second["status"] == "SKIP_IN_FLIGHT"
    assert second["job_id"] == first["job_id"]
    assert len(recorder.calls) == 1


def test_different_event_waits_for_existing_inflight_authoring(machine):
    approved = sheet(
        row(status="approved_active_tracking_ready", slug="clinic-one-review",
            program="Clinic One"),
        row(status="approved_active_tracking_ready", slug="clinic-two-review",
            program="Clinic Two"),
    )
    aw.baseline(values=approved)
    recorder = Recorder()
    first_event = approval_event(approved, "event-one")
    first_event["seo_slug"] = "clinic-one-review"
    first = aw.dispatch(event=first_event, simulated=True, values=approved,
                        callback_url="https://resume.example/1", bridge_post=recorder)
    second_event = aw.event_from_row(3, approved[2])
    second_event["approval_event_id"] = "event-two"
    second = aw.dispatch(event=second_event, simulated=True, values=approved,
                         callback_url="https://resume.example/2", bridge_post=recorder)

    assert first["status"] == "DISPATCHED"
    assert second["status"] == "SKIP_IN_FLIGHT"
    assert second["event_id"] == first["event_id"]
    assert second["requested_event_id"] == "event-two"
    assert len(recorder.calls) == 1


def test_existing_valid_packet_skips_without_authoring(machine, monkeypatch):
    approved = sheet(row(status="approved_active_tracking_ready"))
    aw.baseline(values=approved)
    packet, capture = make_packet(machine)
    before = (aw.sha_file(packet), aw.sha_file(capture))
    calls = []
    monkeypatch.setattr(aw, "run_wire_plan", lambda event_id, packet_path, _event: (
        calls.append((event_id, packet_path)) or {
            "path": str(machine["root"] / "plans" / "plan.json"),
            "sha256": "b" * 64, "status": "PASS",
            "packet_sightings": [str(packet_path)], "packet_seen": True,
            "queue_count": 1, "effects": {"deployments": 0},
        }
    ))
    recorder = Recorder()
    result = aw.dispatch(event=approval_event(approved), simulated=True, values=approved,
                         callback_url="https://resume.example/1", bridge_post=recorder)
    after = (aw.sha_file(packet), aw.sha_file(capture))
    assert result["status"] == "SKIP_EXISTING_VALID"
    assert recorder.calls == []
    assert calls == [(result["event_id"], packet)]
    assert result["wire_plan"]["packet_seen"] is True
    assert before == after
    state = aw.load_state()["events"][result["event_id"]]
    assert state["packet_path"] == str(packet)
    assert state["failure"] == ""
    assert state["next_retry_utc"] == ""


def test_backrun_refresh_reuses_current_slug_without_subprocess(machine, monkeypatch):
    backrun = aw.BACKRUN_ROOT / "backrun-current"
    backrun.mkdir(parents=True)
    values = row(status="approved_active_tracking_ready", slug="willow-review",
                 program="Willow")
    (backrun / "sheet-full-row-export.json").write_text(json.dumps({
        "headers": list(aw.sheet_writer.HEADERS),
        "rows": {"731": values},
    }))
    (backrun / "coverage-receipt.json").write_text(json.dumps({"status": "PASS"}))
    monkeypatch.setattr(aw.wire, "latest_complete_backrun", lambda _root: backrun)
    monkeypatch.setattr(aw.subprocess, "run", lambda *_args, **_kwargs: pytest.fail(
        "current backrun must not launch a refresh"))

    result = aw.ensure_current_backrun_for_event(
        "evt-willow", aw.event_from_row(731, values))

    assert result["refreshed"] is False
    assert result["backrun_dir"] == str(backrun)


def test_backrun_refresh_runs_existing_read_only_backrun_and_requires_slug(machine, monkeypatch):
    selected = {"path": None}
    values = row(status="approved_active_tracking_ready", slug="willow-review",
                 program="Willow")
    event = aw.event_from_row(731, values)

    def latest(_root):
        if selected["path"] is None:
            raise RuntimeError("complete_backrun_missing")
        return selected["path"]

    def run(command, **_kwargs):
        assert command[:2] == [sys.executable, str(aw.BACKRUN_SCRIPT)]
        out = Path(command[command.index("--output-dir") + 1])
        out.mkdir(parents=True)
        (out / "sheet-full-row-export.json").write_text(json.dumps({
            "headers": list(aw.sheet_writer.HEADERS),
            "rows": {"731": values},
        }))
        (out / "sheet-row-dispositions.json").write_text("{}")
        (out / "coverage-receipt.json").write_text(json.dumps({
            "status": "PASS",
            "effects": {"provider_mutations": 0, "sheet_writes": 0,
                        "tracking_clicks": 0, "emails_sent": 0, "deploys": 0},
        }))
        selected["path"] = out
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(aw.wire, "latest_complete_backrun", latest)
    monkeypatch.setattr(aw.subprocess, "run", run)

    result = aw.ensure_current_backrun_for_event("evt-willow", event)

    assert result["refreshed"] is True
    assert result["seo_slug"] == "willow-review"
    assert result["effects"]["deploys"] == 0


def test_backrun_refresh_accepts_same_row_approved_lifecycle_progression(
        machine, monkeypatch):
    selected = {"path": None}
    observed = row(
        status="approved_active_not_wired", slug="rightwell-review",
        program="RightWell", official="https://rightwell.example/",
        network="MaxBounty")
    current = list(observed)
    current[aw.sheet_writer.HEADERS.index("Status")] = "approved_active_tracking_ready"
    event = aw.event_from_row(723, observed)

    def latest(_root):
        if selected["path"] is None:
            raise RuntimeError("complete_backrun_missing")
        return selected["path"]

    def run(command, **_kwargs):
        out = Path(command[command.index("--output-dir") + 1])
        out.mkdir(parents=True)
        (out / "sheet-full-row-export.json").write_text(json.dumps({
            "headers": list(aw.sheet_writer.HEADERS),
            "rows": {"723": current},
        }))
        (out / "sheet-row-dispositions.json").write_text("{}")
        (out / "coverage-receipt.json").write_text(json.dumps({
            "status": "PASS",
            "effects": {"provider_mutations": 0, "sheet_writes": 0,
                        "tracking_clicks": 0, "emails_sent": 0, "deploys": 0},
        }))
        selected["path"] = out
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(aw.wire, "latest_complete_backrun", latest)
    monkeypatch.setattr(aw.subprocess, "run", run)

    result = aw.ensure_current_backrun_for_event("evt-rightwell", event)

    assert result["refreshed"] is True
    assert result["event_binding"]["sheet_row"] == 723
    assert result["event_binding"]["sheet_status"] == "approved_active_tracking_ready"


def test_exact_sheet_event_accepts_same_approved_lifecycle_progression(
        machine, monkeypatch):
    observed = sheet(row(
        status="approved_active_not_wired", slug="rightwell-review",
        program="RightWell", official="https://rightwell.example/",
        network="MaxBounty"))
    event = aw.event_from_row(2, observed[1])
    current = sheet(row(
        status="approved_active_tracking_ready", slug="rightwell-review",
        program="RightWell", official="https://rightwell.example/",
        network="MaxBounty"))
    monkeypatch.setattr(aw, "network_program_identity", lambda _row: {
        "network": "maxbounty", "program_id": "32122", "key": "maxbounty:32122",
    })

    live = aw.exact_sheet_event(event, current)

    assert live["status"] == "approved_active_tracking_ready"
    assert live["seo_slug"] == "rightwell-review"
    assert live["sheet_row"] == 2


def test_backrun_binding_accepts_pending_sibling_rail_on_same_site(machine, tmp_path):
    backrun = tmp_path / "backrun-pending-sibling"
    backrun.mkdir()
    target = row(
        status="approved_active_tracking_ready", slug="rightwell-review",
        program="RightWell MaxBounty", official="https://rightwell.example/",
        network="MaxBounty")
    sibling = row(
        status="applied_pending_brand_approval", slug="rightwell-review",
        program="RightWell Katalys", official="https://www.rightwell.example/apply",
        network="Katalys")
    (backrun / "sheet-full-row-export.json").write_text(json.dumps({
        "headers": list(aw.sheet_writer.HEADERS),
        "rows": {"713": target, "120": sibling},
    }))

    binding = aw.backrun_event_binding(backrun, aw.event_from_row(713, target))

    assert binding is not None
    assert binding["sheet_row"] == 713
    assert binding["sibling_site_slug_rows"] == [{
        "sheet_row": 120,
        "sheet_status": "applied_pending_brand_approval",
        "wire_in_ready": True,
        "wired_live": False,
    }]


def test_backrun_binding_still_refuses_cross_site_sibling_slug(machine, tmp_path):
    backrun = tmp_path / "backrun-conflicting-sibling"
    backrun.mkdir()
    target = row(
        status="approved_active_tracking_ready", slug="rightwell-review",
        program="RightWell MaxBounty", official="https://rightwell.example/",
        network="MaxBounty")
    sibling = row(
        status="applied_pending_brand_approval", slug="rightwell-review",
        program="Unrelated Katalys", official="https://unrelated.example/",
        network="Katalys")
    (backrun / "sheet-full-row-export.json").write_text(json.dumps({
        "headers": list(aw.sheet_writer.HEADERS),
        "rows": {"713": target, "120": sibling},
    }))

    assert aw.backrun_event_binding(backrun, aw.event_from_row(713, target)) is None


def test_authored_pass_limited_packet_refreshes_stale_sibling_slug_backrun(
        machine, monkeypatch):
    stale = aw.BACKRUN_ROOT / "backrun-stale"
    stale.mkdir(parents=True)
    sibling = row(
        status="live_wired_verified", slug="clinic-review", program="Clinic Katalys",
        official="https://clinic.example/campaign-7", network="Katalys")
    sibling[aw.sheet_writer.HEADERS.index("Wired Live?")] = True
    target_stale = row(
        status="provider_capacity_blocked_no_attempt", slug="", program="Clinic MaxBounty",
        official="https://clinic.example/campaign-3", network="MaxBounty")
    (stale / "sheet-full-row-export.json").write_text(json.dumps({
        "headers": list(aw.sheet_writer.HEADERS),
        "rows": {"97": sibling, "730": target_stale},
    }))
    (stale / "coverage-receipt.json").write_text(json.dumps({"status": "PASS"}))

    target = row(
        status="approved_active_tracking_ready", slug="clinic-review",
        program="Clinic MaxBounty", official="https://clinic.example/campaign-3",
        network="MaxBounty")
    target[aw.sheet_writer.HEADERS.index("Wire-In Ready?")] = False
    target[aw.sheet_writer.HEADERS.index("Wired Live?")] = False
    event = aw.event_from_row(730, target)
    packet, _capture = make_packet(machine)
    assert json.loads(packet.read_text())["status"] == "PASS_LIMITED"
    selected = {"path": stale}

    def latest(_root):
        return selected["path"]

    def run(command, **_kwargs):
        if command[:2] == [sys.executable, str(aw.BACKRUN_SCRIPT)]:
            out = Path(command[command.index("--output-dir") + 1])
            out.mkdir(parents=True)
            (out / "sheet-full-row-export.json").write_text(json.dumps({
                "headers": list(aw.sheet_writer.HEADERS),
                "rows": {"97": sibling, "730": target},
            }))
            (out / "sheet-row-dispositions.json").write_text("{}")
            (out / "coverage-receipt.json").write_text(json.dumps({
                "status": "PASS",
                "effects": {"provider_mutations": 0, "sheet_writes": 0,
                            "tracking_clicks": 0, "emails_sent": 0, "deploys": 0},
            }))
            selected["path"] = out
        else:
            assert command[:3] == [
                "python3", "scripts/glp_affiliate_wire_backlog_worker.py", "plan"]
            plan = Path(command[command.index("--output") + 1])
            plan.parent.mkdir(parents=True, exist_ok=True)
            plan.write_text(json.dumps({"status": "PASS", "queue": [], "effects": {}}))
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(aw.wire, "latest_complete_backrun", latest)
    monkeypatch.setattr(aw.subprocess, "run", run)

    result = aw.run_wire_plan("evt-clinic-maxbounty", packet, event)

    assert result["packet_bound"] is True
    assert result["packet_binding_state"] == "PRE_WIRE_GATE"
    assert result["backrun"]["refreshed"] is True
    assert result["packet_binding"]["sheet_row"] == 730
    assert result["packet_binding"]["sibling_site_slug_rows"] == [{
        "sheet_row": 97, "sheet_status": "live_wired_verified",
        "wire_in_ready": True, "wired_live": True,
    }]
    binding = json.loads(Path(result["packet_binding_receipt"]).read_text())
    assert binding["packet_sha256"] == aw.sha_file(packet)
    assert binding["wire_plan_sha256"] == result["sha256"]
    assert binding["prewire"]["sheet_row"] == 730


def test_wire_plan_binds_exact_fail_closed_target_disposition(machine, monkeypatch):
    target = row(
        status="approved_active_tracking_ready", program="Clinic MaxBounty",
        network="MaxBounty")
    event = aw.event_from_row(713, target)
    event.update({
        "network": "maxbounty",
        "network_program_id": "31359",
    })
    packet, _capture = make_packet(machine)
    backrun = aw.BACKRUN_ROOT / "backrun-exact-target-disposition"
    backrun.mkdir(parents=True)
    binding = aw.backrun_event_binding
    (backrun / "sheet-full-row-export.json").write_text(json.dumps({
        "headers": list(aw.sheet_writer.HEADERS),
        "rows": {"713": target},
    }))
    event_binding = binding(backrun, aw.event_from_row(713, target))
    monkeypatch.setattr(aw, "ensure_current_backrun_for_event", lambda *_args: {
        "refreshed": False,
        "backrun_dir": str(backrun),
        "coverage_receipt": str(backrun / "coverage-receipt.json"),
        "coverage_sha256": "e" * 64,
        "seo_slug": "clinic-review",
        "event_binding": event_binding,
        "effects": {},
    })

    def run(command, **_kwargs):
        plan = Path(command[command.index("--output") + 1])
        plan.parent.mkdir(parents=True, exist_ok=True)
        plan.write_text(json.dumps({
            "status": "PASS",
            "queue": [],
            "fail_closed_unmapped": [{
                "display_name": "Clinic MaxBounty",
                "seo_slug": "clinic-review",
                "sheet_row": 713,
                "sheet_status": "approved_active_tracking_ready",
                "sheet_wired_live": False,
                "vertical": "glp1-weightloss",
                "wire_ready_bookkeeping": True,
                "reason": "marketplace_relationship_not_joined",
                "detail": {
                    "network": "maxbounty",
                    "program_id": "31359",
                    "relationship": "pending",
                },
            }],
            "effects": {"deployments": 0, "provider_calls": 0, "site_writes": 0},
        }))
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(aw.subprocess, "run", run)

    result = aw.run_wire_plan("evt-clinic-maxbounty", packet, event)

    assert result["packet_bound"] is True
    assert result["packet_seen"] is False
    assert result["packet_binding_state"] == "PLAN_TARGET_DISPOSITIONED"
    assert result["packet_binding"]["plan_bucket"] == "fail_closed_unmapped"
    assert result["packet_binding"]["reason"] == "marketplace_relationship_not_joined"
    receipt = json.loads(Path(result["packet_binding_receipt"]).read_text())
    assert receipt["status"] == "PASS_PLAN_TARGET_DISPOSITION"
    assert receipt["plan_target_disposition"]["sheet_row"] == 713
    assert receipt["effects"] == {
        "sheet_writes": 0, "provider_mutations": 0, "deployments": 0,
        "tracking_requests": 0, "tracking_clicks": 0,
    }


def test_existing_invalid_packet_fails_closed_and_is_not_overwritten(machine):
    approved = sheet(row(status="approved_active_tracking_ready"))
    aw.baseline(values=approved)
    packet, _capture = make_packet(machine, hash_drift=True)
    before = packet.read_bytes()
    recorder = Recorder()
    result = aw.dispatch(event=approval_event(approved), simulated=True, values=approved,
                         callback_url="https://resume.example/1", bridge_post=recorder)
    assert result["status"] == "FAILED"
    assert result["attempts"] == 1
    assert result["why"].startswith("existing_packet_invalid_refusing_overwrite:")
    assert Path(result["receipt"]).is_file()
    assert recorder.calls == []
    assert packet.read_bytes() == before
    assert not (machine["root"] / "legacy-packet-archive").exists()


def test_strict_legacy_packet_is_archived_then_machine_reauthors(machine):
    approved = sheet(row(status="approved_active_tracking_ready"))
    aw.baseline(values=approved)
    packet, _capture = make_legacy_packet(machine)
    before = packet.read_bytes()
    recorder = Recorder()

    result = aw.dispatch(event=approval_event(approved), simulated=True, values=approved,
                         callback_url="https://resume.example/1", bridge_post=recorder)

    assert result["status"] == "DISPATCHED"
    assert result["attempts"] == 1
    assert packet.read_bytes() == before
    assert len(recorder.calls) == 1
    record = aw.load_state()["events"][result["event_id"]]
    migration = record["legacy_reauthoring"]
    assert migration["status"] == "QUEUED"
    assert migration["previous_attempts"] == 0
    archive = Path(migration["archive"]["path"])
    assert archive.read_bytes() == before
    assert aw.sha_file(archive) == aw.sha_file(packet)


def test_exhausted_strict_legacy_failure_is_requeued_once(machine):
    approved = sheet(row(status="approved_active_tracking_ready"))
    aw.baseline(values=approved)
    event = approval_event(approved)
    event_id = event["approval_event_id"]
    state = aw.load_state()
    state["events"][event_id] = {
        "event_id": event_id, "event": event, "seo_slug": "clinic-review",
        "status": "FAILED", "attempts": aw.MAX_ATTEMPTS,
        "first_seen_utc": "2026-08-27T00:00:00Z",
        "failure": aw.LEGACY_REAUTHOR_FAILURE, "next_retry_utc": "",
    }
    aw.save_state(state)
    packet, _capture = make_legacy_packet(machine)
    before = packet.read_bytes()
    recorder = Recorder()

    result = aw.dispatch(values=approved, callback_url="https://resume.example/1",
                         bridge_post=recorder)

    assert result["status"] == "DISPATCHED"
    assert result["attempts"] == 1
    assert len(recorder.calls) == 1
    saved = aw.load_state()["events"][event_id]
    assert saved["legacy_reauthoring"]["previous_attempts"] == aw.MAX_ATTEMPTS
    assert Path(saved["legacy_reauthoring"]["archive"]["path"]).read_bytes() == before


def test_exhausted_predecessor_failure_recovers_when_packet_is_now_current(
        machine, monkeypatch):
    approved = sheet(row(status="approved_active_tracking_ready"))
    aw.baseline(values=approved)
    event = approval_event(approved)
    event_id = event["approval_event_id"]
    state = aw.load_state()
    state["events"][event_id] = {
        "event_id": event_id, "event": event, "seo_slug": "clinic-review",
        "status": "FAILED", "attempts": aw.MAX_ATTEMPTS,
        "first_seen_utc": "2026-08-27T00:00:00Z",
        "failure": aw.LEGACY_REAUTHOR_FAILURE, "next_retry_utc": "",
    }
    aw.save_state(state)
    packet, _capture = make_packet(machine)
    monkeypatch.setattr(aw, "run_wire_plan", lambda _event_id, packet_path, _event: {
        "path": str(machine["root"] / "plans" / "plan.json"),
        "sha256": "c" * 64, "status": "PASS", "packet_seen": True,
        "packet_sightings": [str(packet_path)], "queue_count": 1, "effects": {},
    })

    result = aw.dispatch(values=approved, callback_url="https://resume.example/1",
                         bridge_post=Recorder())

    assert result["status"] == "SKIP_EXISTING_VALID"
    saved = aw.load_state()["events"][event_id]
    assert saved["status"] == "COMPLETED"
    assert saved["legacy_reauthoring"]["basis"] == "packet_now_valid_under_current_contract"


def test_exhausted_backrun_mismatch_recovers_once_from_current_exact_binding(
        machine, monkeypatch):
    target = row(
        status="approved_active_tracking_ready", program="Clinic MaxBounty",
        network="MaxBounty")
    sibling = row(
        status="applied_pending_brand_approval", program="Clinic Katalys",
        network="Katalys")
    current = sheet(target, sibling)
    aw.baseline(values=current)
    event = aw.event_from_row(2, target)
    event_id = aw.stable_event_id(event)
    event["approval_event_id"] = event_id
    state = aw.load_state()
    state["events"][event_id] = {
        "event_id": event_id, "event": event, "seo_slug": "clinic-review",
        "status": "FAILED", "attempts": aw.MAX_ATTEMPTS,
        "first_seen_utc": "2026-09-02T13:40:03Z",
        "failure": "backrun_refresh_event_row_mismatch", "next_retry_utc": "",
    }
    aw.save_state(state)
    packet, _capture = make_packet(machine)
    backrun = aw.BACKRUN_ROOT / "backrun-current-pending-sibling"
    backrun.mkdir(parents=True)
    (backrun / "sheet-full-row-export.json").write_text(json.dumps({
        "headers": list(aw.sheet_writer.HEADERS),
        "rows": {"2": target, "3": sibling},
    }))
    (backrun / "coverage-receipt.json").write_text(json.dumps({"status": "PASS"}))
    monkeypatch.setattr(aw, "network_program_identity", lambda _row: {
        "network": "maxbounty", "program_id": "32122", "key": "maxbounty:32122",
    })
    monkeypatch.setattr(aw.wire, "latest_complete_backrun", lambda _root: backrun)
    monkeypatch.setattr(aw, "run_wire_plan", lambda _event_id, packet_path, _event: {
        "path": str(machine["root"] / "plans" / "plan.json"),
        "sha256": "d" * 64, "status": "PASS", "packet_seen": True,
        "packet_sightings": [str(packet_path)], "queue_count": 1, "effects": {},
    })

    result = aw.dispatch(
        values=current, callback_url="https://resume.example/1", bridge_post=Recorder(),
        now=dt.datetime(2026, 9, 2, 22, 0, tzinfo=dt.timezone.utc))

    assert result["status"] == "SKIP_EXISTING_VALID"
    saved = aw.load_state()["events"][event_id]
    assert saved["status"] == "COMPLETED"
    recovery = saved["backrun_binding_recovery"]
    assert recovery["schema"] == aw.BACKRUN_BINDING_RECOVERY_SCHEMA
    assert recovery["previous_attempts"] == aw.MAX_ATTEMPTS
    assert recovery["previous_failure"] == "backrun_refresh_event_row_mismatch"
    assert recovery["packet_path"] == str(packet)
    assert recovery["event_binding"]["sibling_site_slug_rows"][0]["sheet_status"] == (
        "applied_pending_brand_approval")


def test_existing_invalid_packet_does_not_poison_pending_queue(machine):
    pending = sheet(
        row(slug="clinic-one-review", program="Clinic One"),
        row(slug="clinic-two-review", program="Clinic Two"),
    )
    aw.baseline(values=pending)
    approved = sheet(
        row(status="approved_active_tracking_ready", slug="clinic-one-review",
            program="Clinic One"),
        row(status="approved_active_tracking_ready", slug="clinic-two-review",
            program="Clinic Two"),
    )
    packet, _capture = make_packet(machine, slug="clinic-one-review", hash_drift=True)
    before = packet.read_bytes()
    recorder = Recorder()

    failed = aw.dispatch(values=approved, callback_url="https://resume.example/1",
                         bridge_post=recorder)
    next_event = aw.dispatch(values=approved, callback_url="https://resume.example/2",
                             bridge_post=recorder)

    assert failed["status"] == "FAILED"
    assert failed["attempts"] == 1
    assert failed["why"].startswith("existing_packet_invalid_refusing_overwrite:")
    assert packet.read_bytes() == before
    assert next_event["status"] == "DISPATCHED"
    assert next_event["seo_slug"] == "clinic-two-review"
    assert len(recorder.calls) == 1
    state = aw.load_state()
    assert sorted(record["status"] for record in state["events"].values()) == [
        "FAILED", "IN_FLIGHT",
    ]


def test_packet_validation_rejects_hash_drift(machine):
    packet, _capture = make_packet(machine, hash_drift=True)
    with pytest.raises(aw.AutowireError, match="capture_binding_hash_drift"):
        aw.validate_packet(packet, "clinic-review")


def test_packet_validation_rejects_quote_not_in_same_source(machine):
    packet, _capture = make_packet(machine, quote="Words never captured")
    with pytest.raises(aw.AutowireError, match="packet_quote_not_in_same_source_capture"):
        aw.validate_packet(packet, "clinic-review")


def test_sheet_slug_is_not_invented_or_normalized(machine):
    approved = sheet(row(status="approved_active_tracking_ready", slug="Clinic Review"))
    aw.baseline(values=approved)
    with pytest.raises(aw.AutowireError, match="sheet_seo_slug_invalid"):
        aw.dispatch(event=approval_event(approved), simulated=True, values=approved,
                    callback_url="https://resume.example/1", bridge_post=Recorder())


def test_dispatch_failure_is_receipted_and_retryable(machine):
    approved = sheet(row(status="approved_active_tracking_ready"))
    aw.baseline(values=approved)

    def failed(*_args, **_kwargs):
        return {"error": "bridge down"}

    result = aw.dispatch(event=approval_event(approved), simulated=True, values=approved,
                         callback_url="https://resume.example/1", bridge_post=failed)
    assert result["status"] == "FAILED"
    assert result["attempts"] == 1
    assert result["next_retry_utc"]
    receipt = Path(result["receipt"])
    assert receipt.is_file()
    saved = json.loads(receipt.read_text())
    assert saved["effects"]["tracking_requests"] == 0
    assert saved["effects"]["sheet_writes"] == 0
