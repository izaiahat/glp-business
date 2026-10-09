from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import article_buffer as buffer  # noqa: E402
import selection_receipt as selection  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def opus_payload_hash(value: dict, field: str) -> str:
    unsigned = dict(value)
    unsigned.pop(field, None)
    rendered = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_candidate(
    tmp_path: Path,
    name: str,
    route: str,
    topic_id: str,
    inbound_source: str | None = None,
    risk_tier: str = "MEDIUM",
    include_provenance: bool = True,
    capture_surface: dict | None = None,
) -> tuple[Path, Path, Path]:
    packet = tmp_path / name / "packet"
    packet.mkdir(parents=True)
    rationale = f"Measured competitor coverage gap for {name}"
    evidence = [{"kind": "competitor_position", "position": 42}]
    keyword_evidence = {"source": "gsc", "query": name, "impressions": 10}
    source_opportunity_id = f"weekly-gap-{topic_id}"
    opportunity_artifact = tmp_path / name / "opportunities.json"
    write_json(opportunity_artifact, {
        "schema": "fixture-keyword-gap-opportunities-v1",
        "artifact_sha256": "a" * 64,
        "opportunities": [{"opportunity_id": source_opportunity_id}],
    })
    opportunity_spec = {
        "path": str(opportunity_artifact.resolve()),
        "sha256": sha(opportunity_artifact),
        "artifact_sha256": "a" * 64,
    }
    overlay_path = tmp_path / name / "keyword-gap-overlay.json"
    write_json(overlay_path, {"schema": "fixture-overlay-v1", "queue": [topic_id]})
    overlay_spec = {"path": str(overlay_path.resolve()), "sha256": sha(overlay_path)}
    topic_authority = tmp_path / name / "topic-authority.json"
    write_json(topic_authority, {
        "schema": "fixture-topic-queue-v1",
        "queue": [{
            "topic_id": topic_id,
            "target_query": name,
            "evidence_state": "READY_FOR_RESEARCH",
            "keyword_evidence": keyword_evidence,
            "competitor_gap": {
                "target_route": route,
                "operator_ready_for_research": True,
                "operator_directive_id": "fixture-authority",
                "opportunity_type": "weekly_keyword_gap",
                "opportunity_class": "B",
                "source_opportunity_id": source_opportunity_id,
                "rationale": rationale,
                "evidence": evidence,
            },
        }],
    })
    authority_spec = {"path": str(topic_authority.resolve()), "sha256": sha(topic_authority)}
    selected = {
        "topic_id": topic_id,
        "target_query": name,
        "target_route": route,
        "operator_directive_id": "fixture-authority",
        "queue_position": 1,
        "cluster_id": "fixture-cluster",
        "depth_stage": "spoke",
        "queue_sha256": authority_spec["sha256"],
        "strategy_safe": True,
        "predecessor_proof": {"satisfied": True},
        "gsc_preference": {},
    }
    if include_provenance:
        selected["topic_provenance"] = {
            "schema": "glp-seo-topic-provenance-v1",
            "keyword": name,
            "selection_reason": rationale,
            "selection_mode": "keyword_gap_class_b_first",
            "gap_source": {
                "type": "keyword_gap",
                "opportunity_type": "weekly_keyword_gap",
                "opportunity_class": "B",
                "source_opportunity_id": source_opportunity_id,
                "operator_directive_id": "fixture-authority",
            },
            "evidence": evidence,
            "keyword_evidence": keyword_evidence,
            "analytics": {"gsc_preference": {}},
            "source_artifacts": {
                "keyword_gap_strategy_overlay": overlay_spec,
                "keyword_gap_opportunity_artifact": opportunity_spec,
            },
        }
    receipt_value = {
        "schema": selection.SCHEMA,
        "status": selection.STATUS,
        "selected": [selected],
        "eligible": [selected],
        "eligible_count": 1,
        "selection_mode": "keyword_gap_class_b_first",
        "inputs": {
            "topic_queue": authority_spec,
            "keyword_gap_strategy_overlay": overlay_spec,
            "strategy_queue": authority_spec,
            "strategy_map": authority_spec,
            "published_ledger": authority_spec,
            "hold_ledger": authority_spec,
            "current_content_base_state": authority_spec,
            "sitemap_readback": authority_spec,
            "current_content_base_resolution": {
                "catalogs": [{"sha256": authority_spec["sha256"]} for _ in range(3)]
            },
        },
    }
    selection_path = tmp_path / name / "selection-receipt.json"
    selection.write_immutable_receipt(selection_path, receipt_value)
    values = {
        "00-authority.json": {
            "topic_id": topic_id,
            "selection_receipt": selection.selection_spec(selection_path),
            "selection_binding": selection.selection_binding(json.loads(selection_path.read_text())),
        },
        "01-evidence-ledger.json": {"sources": ["FDA"]},
        "02-serp-brief.json": {"query": name},
        "03-editorial-brief.md": f"# Brief {name}\n",
        "04-draft.md": (
            f"---\nriskTier: \"{risk_tier}\"\n---\n# Draft {name}\nSourced article.\n"
        ),
        "05-claim-source-map.json": {"claims": [{"source": "FDA"}]},
        "06-article.json": {
            "article_id": f"article-{topic_id}",
            **({"capture_surface": capture_surface} if capture_surface is not None else {}),
            "canonical_path": route,
            "title": name,
            "risk_tier": risk_tier,
            "inbound_link_plan": (
                [{"source_route": inbound_source, "anchor_text": name}]
                if inbound_source else []
            ),
        },
        "07-qa-receipt.json": {"status": "PASS"},
    }
    for filename, value in values.items():
        path = packet / filename
        if isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            write_json(path, value)
    subjects = []
    for filename in values:
        path = packet / filename
        data = path.read_bytes()
        subjects.append({
            "path": filename,
            "bytes": len(data),
            "lines": len(data.decode().splitlines()),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    manifest = {
        "schema": "glp-opus-seo-subject-manifest-v1",
        "status": "SEALED_FOR_OPUS_REVIEW",
        "sealed_at_utc": "2026-08-14T12:00:00Z",
        "job_id": f"job-{topic_id}",
        "topic_id": topic_id,
        "round": 1,
        "subject_files": subjects,
    }
    manifest["manifest_sha256"] = opus_payload_hash(manifest, "manifest_sha256")
    manifest_path = tmp_path / name / "subject-manifest.json"
    write_json(manifest_path, manifest)
    raw = {
        "review_input": {
            "transport_id": "hermes-openai-codex-sol",
            "context_isolated": True,
            "hermes_managed": True,
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "api_mode": "codex_responses",
            "effort": "xhigh",
            "tools_disabled": True,
            "tool_count": 0,
            "fallback_provider_count": 0,
        },
        "hermes_runtime": {
            "transport_id": "hermes-openai-codex-sol",
            "context_isolated": True,
            "model_turns": 1,
            "provider": "openai-codex",
            "model": "gpt-5.6-sol",
            "api_mode": "codex_responses",
            "tool_count": 0,
            "tool_calls": 0,
            "fallback_provider_count": 0,
        },
        "modelUsage": {"gpt-5.6-sol": {"canonicalModel": "gpt-5.6-sol", "outputTokens": 4096}},
        "usage": {"server_tool_use": {}, "tool_calls": 0},
    }
    raw_path = tmp_path / name / "sol-raw.json"
    write_json(raw_path, raw)
    receipt = {
        "schema": "glp-opus-seo-round-receipt-v1",
        "status": "PASS_OPUS_SEO_DRAFT",
        "reviewed_at_utc": "2026-08-14T12:01:00Z",
        "job_id": f"job-{topic_id}",
        "topic_id": topic_id,
        "round": 1,
        "provider": "openai-codex",
        "model": "gpt-5.6-sol",
        "effort": "xhigh",
        "tools_disabled": True,
        "subject_manifest_path": str(manifest_path.resolve()),
        "subject_manifest_sha256": sha(manifest_path),
        "raw_review_path": str(raw_path.resolve()),
        "raw_review_sha256": sha(raw_path),
        "gates": [{"gate": gate, "status": "PASS", "findings": []} for gate in "ABCDE"],
        "findings": [],
        "effects": {"publishes": 0, "deployments": 0, "cache_purges": 0, "gsc_submissions": 0},
    }
    receipt["receipt_sha256"] = opus_payload_hash(receipt, "receipt_sha256")
    receipt_path = tmp_path / name / "pass-receipt.json"
    write_json(receipt_path, receipt)
    control = tmp_path / "buffer-control.py"
    if not control.exists():
        control.write_text("CONTROL = True\n", encoding="utf-8")
    return packet, receipt_path, control


def admit_candidate(
    root: Path,
    packet: Path,
    receipt: Path,
    control: Path,
    admitted: str,
    mutable_article_routes: set[str] | None = None,
) -> Path:
    return buffer.admit(
        root=root,
        packet=packet,
        pass_receipt=receipt,
        control_files={str(control.resolve()): sha(control)},
        current_routes=set(),
        current_topic_ids=set(),
        mutable_article_routes=mutable_article_routes or set(),
        admitted_at_utc=admitted,
    )


def _check_registered_exact_entry_hold(tmp_path, unsafe):
    root = tmp_path / "pool"
    packet, receipt, control = build_candidate(
        tmp_path, "obsolete", "/guides/obsolete/", "topic-obsolete"
    )
    manifest = admit_candidate(root, packet, receipt, control, "2026-08-14T01:00:00Z")
    entry = json.loads(manifest.read_text())
    evidence_path = tmp_path / "review.json"
    write_json(evidence_path, {"status": "reviewed_obsolete"})
    evidence = {"path": str(evidence_path.resolve()), "sha256": sha(evidence_path)}
    base = tmp_path / "base.json"
    write_json(base, {"deployment_id": "fixture"})
    original = {p: p.read_bytes() for p in manifest.parent.rglob("*") if p.is_file()}
    request = dict(
        root=root, entry_id=entry["entry_id"], entry_sha256=sha(manifest),
        reason="superseded_after_exact_packet_review", evidence=evidence,
        current_routes=set(), current_topic_ids=set(), dry_run=True,
    )
    import article_buffer_cli as cli
    parsed = cli.build_parser().parse_args([
        "hold", "--root", str(root), "--entry-id", entry["entry_id"],
        "--entry-sha256", sha(manifest), "--reason", request["reason"],
        "--evidence", str(evidence_path), "--evidence-sha256", evidence["sha256"],
        "--dry-run",
    ])
    assert parsed.command == "hold" and parsed.dry_run is True
    assert buffer.hold_entry(**request)["status"] == "DRY_RUN_BUFFER_ENTRY_HOLD"
    assert not (root / "held").exists()
    request["dry_run"] = False
    first = buffer.hold_entry(**request)
    assert first["status"] == "PASS_BUFFER_ENTRY_HELD"
    hold_path = Path(first["hold"]["path"])
    held_bytes = hold_path.read_bytes()
    assert buffer._read_sealed(hold_path, buffer.HOLD_SCHEMA, "hold_sha256")["entry_manifest"] == buffer._spec(manifest)
    assert buffer.hold_entry(**request) == {
        "status": "IDEMPOTENT_BUFFER_ENTRY_HELD", "hold": first["hold"]
    }
    assert hold_path.read_bytes() == held_bytes
    assert buffer.ready_entries(root, current_routes=set(), current_topic_ids=set()) == []
    assert buffer.pool_status(root, current_routes=set(), current_topic_ids=set())["ready"] == 0
    with pytest.raises(buffer.BufferEmptyError):
        buffer.claim_oldest(root=root, operation_id="cannot-consume-held",
                            current_routes=set(), current_topic_ids=set(), base_state=base)
    assert buffer.recycle_stale_claims(root=root, work_root=tmp_path / "work",
                                       base_state=base, today_pt="2026-08-15")["retired"] == []
    assert buffer.ready_entries(root, current_routes=set(), current_topic_ids=set()) == []
    with pytest.raises(buffer.BufferAdmissionError, match="buffer_candidate_duplicate"):
        admit_candidate(root, packet, receipt, control, "2026-08-14T03:00:00Z")
    assert {p: p.read_bytes() for p in manifest.parent.rglob("*") if p.is_file()} == original

    # Every unsafe target is a separate isolated pool; the held receipt above
    # must neither be re-used to conceal an unsafe state nor admit a successor.
    other_packet, other_receipt, _ = build_candidate(
        tmp_path, "unsafe", "/guides/unsafe/", "topic-unsafe"
    )
    other = admit_candidate(root, other_packet, other_receipt, control, "2026-08-14T02:00:00Z")
    other_entry = json.loads(other.read_text())
    unsafe_request = {**request, "entry_id": other_entry["entry_id"], "entry_sha256": sha(other)}
    expected = {
        "claimed": "buffer_hold_entry_claimed",
        "consumed": "buffer_hold_entry_consumed",
        "published": "buffer_entry_now_published",
        "unknown": "buffer_hold_entry_unknown",
        "preimage_drift": "buffer_hold_entry_preimage_drift",
        "evidence_drift": "buffer_hold_evidence_pin_invalid",
    }[unsafe]
    if unsafe == "claimed":
        buffer.claim_oldest(root=root, operation_id="in-flight",
                            current_routes=set(), current_topic_ids=set(), base_state=base)
        expected = "buffer_hold_entry_claimed"
    elif unsafe == "consumed":
        buffer._write_once(root / "consumed" / "fixture.json", {
            "schema": buffer.CONSUME_SCHEMA, "entry_id": other_entry["entry_id"]
        }, "consumption_sha256")
        expected = "buffer_hold_entry_consumed"
    elif unsafe == "published":
        unsafe_request["current_routes"] = {other_entry["target_route"]}
        expected = "buffer_entry_now_published"
    elif unsafe == "unknown":
        unsafe_request["entry_id"] = "f" * 24
        expected = "buffer_hold_entry_unknown"
    elif unsafe == "preimage_drift":
        unsafe_request["entry_sha256"] = "0" * 64
        expected = "buffer_hold_entry_preimage_drift"
    elif unsafe == "evidence_drift":
        unsafe_request["evidence"] = {**evidence, "sha256": "0" * 64}
        expected = "buffer_hold_evidence_pin_invalid"
    with pytest.raises(RuntimeError, match=expected):
        buffer.hold_entry(**unsafe_request)
    assert not (root / "held" / f"{other_entry['entry_id']}.json").exists()
    with pytest.raises(RuntimeError, match="buffer_hold_conflict"):
        buffer.hold_entry(**{**request, "reason": "unreviewed_new_reason"})


@pytest.mark.parametrize("unsafe", [
    "claimed", "consumed", "published", "unknown", "preimage_drift", "evidence_drift"
])
def test_registered_exact_entry_hold_states(tmp_path, unsafe, monkeypatch):
    from carrier_lock import carrier_lock as real_carrier_lock
    monkeypatch.setattr(buffer, "carrier_lock", lambda: real_carrier_lock(tmp_path / "carrier.lock"))
    _check_registered_exact_entry_hold(tmp_path, unsafe)


def test_admission_rejects_packet_pin_drift(tmp_path):
    packet, receipt, control = build_candidate(tmp_path, "one", "/guides/one/", "topic-one")
    (packet / "04-draft.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(buffer.BufferAdmissionError, match="buffer_subject_pin_invalid"):
        admit_candidate(tmp_path / "pool", packet, receipt, control, "2026-08-14T01:00:00Z")


def test_admission_rejects_missing_topic_provenance(tmp_path):
    packet, receipt, control = build_candidate(
        tmp_path,
        "no-provenance",
        "/guides/no-provenance/",
        "topic-no-provenance",
        include_provenance=False,
    )
    with pytest.raises(buffer.BufferAdmissionError, match="buffer_topic_provenance_missing"):
        admit_candidate(tmp_path / "pool", packet, receipt, control, "2026-08-14T01:00:00Z")


def test_admission_records_bound_topic_provenance(tmp_path):
    packet, receipt, control = build_candidate(
        tmp_path, "provenance", "/guides/provenance/", "topic-provenance"
    )
    manifest = admit_candidate(
        tmp_path / "pool", packet, receipt, control, "2026-08-14T01:00:00Z"
    )
    entry = json.loads(manifest.read_text())
    provenance = entry["topic_provenance"]
    assert provenance["keyword"] == "provenance"
    assert provenance["gap_source"]["opportunity_class"] == "B"
    assert provenance["evidence"]


def test_admission_rejects_nonzero_publication_effects(tmp_path):
    packet, receipt_path, control = build_candidate(tmp_path, "effects", "/guides/effects/", "topic-effects")
    receipt = json.loads(receipt_path.read_text())
    receipt["effects"]["publishes"] = 1
    receipt["receipt_sha256"] = opus_payload_hash(receipt, "receipt_sha256")
    write_json(receipt_path, receipt)
    with pytest.raises(buffer.BufferAdmissionError, match="buffer_opus_pass_invalid"):
        admit_candidate(tmp_path / "pool", packet, receipt_path, control, "2026-08-14T01:00:00Z")


def test_admission_rejects_inbound_source_absent_from_mutable_catalogs(tmp_path):
    packet, receipt, control = build_candidate(
        tmp_path,
        "dead-source",
        "/guides/dead-source/",
        "topic-dead-source",
        inbound_source="/best/not-in-article-catalogs/",
    )
    root = tmp_path / "pool"
    with pytest.raises(
        buffer.BufferAdmissionError,
        match=r"inbound_link_source_missing:/best/not-in-article-catalogs/",
    ):
        admit_candidate(
            root,
            packet,
            receipt,
            control,
            "2026-08-14T01:00:00Z",
            mutable_article_routes={"/guides/mutable-source/"},
        )
    assert list((root / "entries").glob("*/entry.json")) == []


def test_admission_accepts_inbound_source_present_in_mutable_catalogs(tmp_path):
    source = "/guides/mutable-source/"
    packet, receipt, control = build_candidate(
        tmp_path,
        "live-source",
        "/guides/live-source/",
        "topic-live-source",
        inbound_source=source,
    )
    manifest = admit_candidate(
        tmp_path / "pool",
        packet,
        receipt,
        control,
        "2026-08-14T01:00:00Z",
        mutable_article_routes={source},
    )
    assert manifest.is_file()


def test_capacity_precheck_excludes_entry_after_inbound_source_leaves_catalogs(tmp_path):
    source = "/guides/removed-source/"
    packet, receipt, control = build_candidate(
        tmp_path,
        "stale-source",
        "/guides/stale-source/",
        "topic-stale-source",
        inbound_source=source,
    )
    root = tmp_path / "pool"
    admit_candidate(
        root,
        packet,
        receipt,
        control,
        "2026-08-14T01:00:00Z",
        mutable_article_routes={source},
    )
    assert buffer.pool_status(
        root,
        current_routes=set(),
        current_topic_ids=set(),
    )["ready"] == 1
    assert buffer.pool_status(
        root,
        current_routes=set(),
        current_topic_ids=set(),
        mutable_article_routes=set(),
    )["ready"] == 0


def test_admission_rejects_duplicate_and_live_route(tmp_path):
    packet, receipt, control = build_candidate(tmp_path, "one", "/guides/one/", "topic-one")
    admit_candidate(tmp_path / "pool", packet, receipt, control, "2026-08-14T01:00:00Z")
    packet2, receipt2, _ = build_candidate(tmp_path, "two", "/guides/one/", "topic-two")
    with pytest.raises(buffer.BufferAdmissionError, match="buffer_candidate_duplicate"):
        admit_candidate(tmp_path / "pool", packet2, receipt2, control, "2026-08-14T02:00:00Z")
    packet3, receipt3, _ = build_candidate(tmp_path, "three", "/guides/live/", "topic-live")
    with pytest.raises(buffer.BufferAdmissionError, match="buffer_candidate_already_live"):
        buffer.admit(
            root=tmp_path / "other-pool",
            packet=packet3,
            pass_receipt=receipt3,
            control_files={str(control.resolve()): sha(control)},
            current_routes={"/guides/live/"},
            current_topic_ids=set(),
            mutable_article_routes=set(),
            admitted_at_utc="2026-08-14T03:00:00Z",
        )


@pytest.mark.parametrize('reason,risk,on_page,allowed', [
    ('inbound_link_source_missing:/guides/retired/', 'MEDIUM', True, True),
    ('clinical_claim_invalid', 'MEDIUM', True, False),
    ('inbound_link_source_missing:/guides/retired/', 'HIGH', True, False),
    ('inbound_link_source_missing:/guides/retired/', 'MEDIUM', False, False),
])
def test_on_page_link_source_successor_is_narrow_and_keeps_history(tmp_path, reason, risk, on_page, allowed):
    root = tmp_path / 'pool'
    route, topic = '/guides/source-repaired/', 'topic-source-repaired'
    legacy = {'status': 'WORKING_BASE_TEMPLATE_CONTRACT', 'action': '/api/content-upgrade', 'email_field': 'email'}
    old_packet, old_receipt, control = build_candidate(tmp_path, 'legacy', route, topic,
                                                      risk_tier=risk, capture_surface=legacy)
    old_manifest = admit_candidate(root, old_packet, old_receipt, control, '2026-08-14T01:00:00Z')
    old = buffer._read_sealed(old_manifest, buffer.ENTRY_SCHEMA, 'entry_sha256')
    qpath = root / 'quarantined' / f"{old['entry_id']}.json"
    buffer._write_once(qpath, {'schema': buffer.QUARANTINE_SCHEMA, 'reason': reason,
        'entry_id': old['entry_id'], 'topic_id': topic, 'article_id': old['article_id'],
        'target_route': route, 'publication_effects': 0}, 'quarantine_sha256')
    old_hashes = (sha(old_manifest), sha(qpath))
    capture = {'status': 'ON_PAGE_ONLY', 'email_field': None, 'health_data_fields': [],
               'follow_up_consent': False} if on_page else legacy
    packet, receipt, _ = build_candidate(tmp_path, 'fresh', route, topic,
                                        risk_tier='MEDIUM', capture_surface=capture)
    if allowed:
        successor = admit_candidate(root, packet, receipt, control, '2026-08-14T02:00:00Z')
        entry = buffer._read_sealed(successor, buffer.ENTRY_SCHEMA, 'entry_sha256')
        assert entry['on_page_source_repair_of']['entry_manifest']['path'] == str(old_manifest)
        assert buffer.pool_status(root, current_routes=set(), current_topic_ids=set())['ready'] == 1
        with pytest.raises(buffer.BufferAdmissionError, match='buffer_candidate_duplicate'):
            admit_candidate(root, packet, receipt, control, '2026-08-14T03:00:00Z')
    else:
        with pytest.raises(buffer.BufferAdmissionError, match='buffer_candidate_duplicate'):
            admit_candidate(root, packet, receipt, control, '2026-08-14T02:00:00Z')
    assert old_hashes == (sha(old_manifest), sha(qpath))


def test_admission_accepts_only_fresh_reviewed_successor_to_exact_medical_quarantine(tmp_path):
    root = tmp_path / "pool"
    route = "/guides/reframed/"
    topic = "topic-reframed"
    old_packet, old_receipt, control = build_candidate(
        tmp_path, "medical-old", route, topic, risk_tier="HIGH"
    )
    old_manifest = admit_candidate(
        root, old_packet, old_receipt, control, "2026-08-14T01:00:00Z"
    )
    old_entry = buffer._read_sealed(old_manifest, buffer.ENTRY_SCHEMA, "entry_sha256")
    quarantine = {
        "schema": buffer.QUARANTINE_SCHEMA,
        "status": "QUARANTINED_PERMANENT_ARTICLE_GATE_REJECTION",
        "reason": buffer.MEDICAL_REFRAME_REASON,
        "entry_id": old_entry["entry_id"],
        "topic_id": topic,
        "article_id": old_entry["article_id"],
        "target_route": route,
    }
    quarantine_path = root / "quarantined" / f"{old_entry['entry_id']}.json"
    buffer._write_once(quarantine_path, quarantine, "quarantine_sha256")

    packet, receipt, _ = build_candidate(
        tmp_path, "medical-new", route, topic, risk_tier="MEDIUM"
    )
    successor = admit_candidate(
        root, packet, receipt, control, "2026-08-14T02:00:00Z"
    )
    successor_entry = buffer._read_sealed(successor, buffer.ENTRY_SCHEMA, "entry_sha256")
    assert successor_entry["reframed_from"]["reason"] == buffer.MEDICAL_REFRAME_REASON
    assert successor_entry["reframed_from"]["entry_manifest"]["path"] == str(old_manifest)
    assert buffer.pool_status(
        root,
        current_routes=set(),
        current_topic_ids=set(),
    )["ready"] == 1


def test_medical_reframe_duplicate_cannot_be_reentered_without_lower_risk(tmp_path):
    root = tmp_path / "pool"
    route = "/guides/still-high/"
    topic = "topic-still-high"
    old_packet, old_receipt, control = build_candidate(
        tmp_path, "still-high-old", route, topic, risk_tier="HIGH"
    )
    old_manifest = admit_candidate(
        root, old_packet, old_receipt, control, "2026-08-14T01:00:00Z"
    )
    old_entry = buffer._read_sealed(old_manifest, buffer.ENTRY_SCHEMA, "entry_sha256")
    buffer._write_once(
        root / "quarantined" / f"{old_entry['entry_id']}.json",
        {
            "schema": buffer.QUARANTINE_SCHEMA,
            "reason": buffer.MEDICAL_REFRAME_REASON,
            "entry_id": old_entry["entry_id"],
            "topic_id": topic,
            "article_id": old_entry["article_id"],
            "target_route": route,
        },
        "quarantine_sha256",
    )
    packet, receipt, _ = build_candidate(
        tmp_path, "still-high-new", route, topic, risk_tier="HIGH"
    )
    with pytest.raises(
        buffer.BufferAdmissionError,
        match="buffer_medical_reframe_successor_invalid",
    ):
        admit_candidate(root, packet, receipt, control, "2026-08-14T02:00:00Z")


def test_oldest_valid_consumption_and_replay_safety(tmp_path):
    root = tmp_path / "pool"
    packet1, receipt1, control = build_candidate(tmp_path, "old", "/guides/old/", "topic-old")
    packet2, receipt2, _ = build_candidate(tmp_path, "new", "/guides/new/", "topic-new")
    admit_candidate(root, packet1, receipt1, control, "2026-08-14T01:00:00Z")
    admit_candidate(root, packet2, receipt2, control, "2026-08-14T02:00:00Z")
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-current"})
    rematerializations: list[str] = []
    publications: list[str] = []

    def rematerialize(packet, entry, base_spec, operation_id, generation=0):
        assert packet.is_dir()
        authority_temporary = packet / "00-authority.tmp"
        authority_temporary.write_text("{}\n", encoding="utf-8")
        authority_temporary.unlink()
        rematerializations.append(entry["entry_id"])
        return {
            "operation": "new_article",
            "base_state_sha256": base_spec["sha256"],
            "entry_id": entry["entry_id"],
            "operation_id": operation_id,
        }

    def publish(materialized, operation_id, lock_fd):
        assert isinstance(lock_fd, int)
        publications.append(materialized["entry_id"])
        return {"status": "PASS_LIVE_GSC_READBACK_PASS", "operation_id": operation_id}

    first = buffer.consume_oldest(
        root=root,
        operation_id="forced-fallback-1",
        current_routes=set(),
        current_topic_ids=set(),
        base_state=base,
        work_root=tmp_path / "work",
        rematerialize=rematerialize,
        publish=publish,
    )
    assert first["status"] == "PASS_BUFFER_PUBLISHED"
    assert first["target_route"] == "/guides/old/"
    replay = buffer.consume_oldest(
        root=root,
        operation_id="forced-fallback-1",
        current_routes=set(),
        current_topic_ids=set(),
        base_state=base,
        work_root=tmp_path / "work",
        rematerialize=lambda *_args: pytest.fail("replay rematerialized"),
        publish=lambda *_args: pytest.fail("replay published"),
    )
    assert replay == first
    assert len(rematerializations) == len(publications) == 1


def test_publish_failure_replays_sealed_materialization_without_remint(tmp_path):
    root = tmp_path / "pool"
    packet, receipt, control = build_candidate(
        tmp_path, "publish-replay", "/guides/publish-replay/", "topic-publish-replay"
    )
    admit_candidate(root, packet, receipt, control, "2026-08-14T01:00:00Z")
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-current"})
    rematerializations = 0

    def rematerialize(_packet, _entry, base_spec, _operation_id, _generation=0):
        nonlocal rematerializations
        rematerializations += 1
        return {"operation": "new_article", "base_state_sha256": base_spec["sha256"]}

    with pytest.raises(RuntimeError, match="publish failed"):
        buffer.consume_oldest(
            root=root,
            operation_id="publish-replay-operation",
            current_routes=set(),
            current_topic_ids=set(),
            base_state=base,
            work_root=tmp_path / "work",
            rematerialize=rematerialize,
            publish=lambda *_args: (_ for _ in ()).throw(RuntimeError("publish failed")),
        )

    replay = buffer.consume_oldest(
        root=root,
        operation_id="publish-replay-operation",
        current_routes=set(),
        current_topic_ids=set(),
        base_state=base,
        work_root=tmp_path / "work",
        rematerialize=lambda *_args: pytest.fail("replay rematerialized"),
        publish=lambda _materialized, operation_id, _lock_fd: {
            "status": "PASS_LIVE_GSC_READBACK_PASS",
            "operation_id": operation_id,
        },
    )
    assert replay["status"] == "PASS_BUFFER_PUBLISHED"
    assert rematerializations == 1


def test_head_change_mid_publish_rematerializes_current_generation(tmp_path):
    root = tmp_path / "pool"
    packet, receipt, control = build_candidate(
        tmp_path, "head-change", "/guides/head-change/", "topic-head-change"
    )
    admit_candidate(root, packet, receipt, control, "2026-08-14T01:00:00Z")
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-a", "release_tree_sha256": "a" * 64})
    rematerializations: list[tuple[int, str]] = []
    publish_attempts: list[str] = []

    def rematerialize(_packet, _entry, base_spec, _operation_id, generation=0):
        rematerializations.append((generation, base_spec["sha256"]))
        return {
            "operation": "new_article",
            "base_state_sha256": base_spec["sha256"],
            "release_root": str(tmp_path / f"release-r{generation}"),
        }

    def publish(materialized, _operation_id, _lock_fd):
        publish_attempts.append(materialized["base_state_sha256"])
        if len(publish_attempts) == 1:
            write_json(
                base,
                {"deployment_id": "dep-b", "release_tree_sha256": "b" * 64},
            )
            raise RuntimeError(
                "production_head_changed_during_final_target_byte_readback"
            )
        return {"status": "PASS_LIVE_GSC_READBACK_PASS"}

    result = buffer.consume_oldest(
        root=root,
        operation_id="head-change-operation",
        current_routes=set(),
        current_topic_ids=set(),
        base_state=base,
        work_root=tmp_path / "work",
        rematerialize=rematerialize,
        publish=publish,
    )

    assert result["status"] == "PASS_BUFFER_PUBLISHED"
    assert [generation for generation, _digest in rematerializations] == [0, 1]
    assert len(set(publish_attempts)) == 2
    assert result["current_base_state"]["sha256"] == sha(base)
    workspace = (
        tmp_path / "work" / hashlib.sha256(b"head-change-operation").hexdigest()
    )
    rebound = json.loads((workspace / "materialization-r1.json").read_text())
    assert rebound["status"] == "PASS_BUFFER_REMATERIALIZED_AFTER_HEAD_CHANGE"
    assert rebound["base_drift"]["stale_publish_permitted"] is False
    assert rebound["supersedes"]["generation"] == 0


def test_claimed_before_base_change_rematerializes_against_current_base(tmp_path):
    root = tmp_path / "pool"
    packet, receipt, control = build_candidate(
        tmp_path, "claim-base-drift", "/guides/claim-base-drift/", "topic-claim-base-drift"
    )
    admit_candidate(root, packet, receipt, control, "2026-08-14T01:00:00Z")
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-before"})
    claim_path = buffer.claim_oldest(
        root=root,
        operation_id="claim-base-drift-operation",
        current_routes=set(),
        current_topic_ids=set(),
        base_state=base,
    )
    original_claim = claim_path.read_bytes()
    old_base = json.loads(original_claim)["current_base_state"]
    write_json(base, {"deployment_id": "dep-current"})
    rematerializations = []

    def rematerialize(_packet, _entry, base_spec, _operation_id, generation=0):
        rematerializations.append((generation, base_spec["sha256"]))
        return {"operation": "new_article", "base_state_sha256": base_spec["sha256"]}

    result = buffer.consume_oldest(
        root=root,
        operation_id="claim-base-drift-operation",
        current_routes=set(),
        current_topic_ids=set(),
        base_state=base,
        work_root=tmp_path / "work",
        rematerialize=rematerialize,
        publish=lambda *_args: {"status": "PASS_LIVE_GSC_READBACK_PASS"},
    )

    assert result["status"] == "PASS_BUFFER_PUBLISHED"
    assert rematerializations == [(0, sha(base))]
    assert result["current_base_state"]["sha256"] == sha(base)
    assert claim_path.read_bytes() == original_claim
    workspace = tmp_path / "work" / hashlib.sha256(
        b"claim-base-drift-operation"
    ).hexdigest()
    materialization = json.loads((workspace / "materialization.json").read_text())
    assert materialization["status"] == "PASS_BUFFER_REMATERIALIZED_AFTER_HEAD_CHANGE"
    assert materialization["base_drift"]["previous"] == old_base
    assert materialization["base_drift"]["current"]["sha256"] == sha(base)
    assert materialization["base_drift"]["stale_publish_permitted"] is False


def test_refill_base_status_detects_pointer_advance_since_latest_closeout(tmp_path):
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-before"})
    old_sha = sha(base)
    closeout = tmp_path / "work" / "run-1" / "glp-seo-article-buffer-closeout-v1-1.json"
    write_json(closeout, {
        "schema": "glp-seo-article-buffer-closeout-v1",
        "completed_at_utc": "2026-08-14T01:00:00Z",
        "current_content_base": {"sha256_after": old_sha},
    })
    write_json(base, {"deployment_id": "dep-current"})

    result = buffer.refill_base_status(tmp_path / "work", base)

    assert result["status"] == "REFILL_REQUIRED_CURRENT_CONTENT_BASE_CHANGED"
    assert result["changed"] is True
    assert result["previous"]["sha256"] == old_sha
    assert result["current"]["sha256"] == sha(base)


def test_refill_base_status_passes_when_latest_closeout_binds_the_live_base(tmp_path):
    """No base change means NO refill trigger.

    check_supply() turns ``changed`` into a live refill dispatch, so a
    permanently-true trigger would fire a real Hermes job on every carrier run.
    This is the guard against that regression.
    """
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-current"})
    closeout = tmp_path / "work" / "run-1" / "glp-seo-article-buffer-closeout-v1-1.json"
    write_json(closeout, {
        "schema": "glp-seo-article-buffer-closeout-v1",
        "completed_at_utc": "2026-08-14T01:00:00Z",
        "current_content_base": {"sha256_after": sha(base)},
    })

    result = buffer.refill_base_status(tmp_path / "work", base)

    assert result["status"] == "PASS_REFILL_BASE_ALIGNED"
    assert result["changed"] is False
    assert result["invalid_closeout_count"] == 0


def test_refill_base_status_skips_legacy_closeouts_without_a_recorded_base(tmp_path):
    """Closeouts sealed before ``sha256_after`` existed must not become the baseline.

    Production still holds pre-2026-08-27 closeouts whose
    ``current_content_base`` has no ``sha256_after``. Treating one as the
    baseline would compare None to the pointer and pin the trigger to True
    forever.
    """
    work = tmp_path / "work"
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-current"})
    # Newest by completion stamp, but legacy-shaped: must be counted invalid.
    write_json(work / "glp-seo-article-buffer-closeout-v1-legacy.json", {
        "schema": "glp-seo-article-buffer-closeout-v1",
        "completed_at_utc": "2026-09-01T00:00:00Z",
        "current_content_base": {"path": "x", "unchanged": True},
    })
    write_json(work / "run-1" / "glp-seo-article-buffer-closeout-v1-1.json", {
        "schema": "glp-seo-article-buffer-closeout-v1",
        "completed_at_utc": "2026-08-31T00:00:00Z",
        "current_content_base": {"sha256_after": sha(base)},
    })

    result = buffer.refill_base_status(work, base)

    assert result["status"] == "PASS_REFILL_BASE_ALIGNED"
    assert result["changed"] is False
    assert result["invalid_closeout_count"] == 1
    assert result["previous"]["closeout"].endswith("run-1/glp-seo-article-buffer-closeout-v1-1.json")


def test_refill_base_status_requires_refill_when_no_closeout_baseline_exists(tmp_path):
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-current"})

    result = buffer.refill_base_status(tmp_path / "work", base)

    assert result["status"] == "REFILL_REQUIRED_BASELINE_MISSING"
    assert result["changed"] is True
    assert result["previous"] is None


def test_permanent_article_gate_rejection_quarantines_and_publishes_next(tmp_path):
    root = tmp_path / "pool"
    bad_packet, bad_receipt, control = build_candidate(
        tmp_path, "bad", "/guides/bad/", "topic-bad"
    )
    good_packet, good_receipt, _ = build_candidate(
        tmp_path, "good", "/guides/good/", "topic-good"
    )
    admit_candidate(root, bad_packet, bad_receipt, control, "2026-08-14T01:00:00Z")
    admit_candidate(root, good_packet, good_receipt, control, "2026-08-14T02:00:00Z")
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-current"})
    attempted: list[str] = []

    def rematerialize(_packet, entry, base_spec, _operation_id, _generation=0):
        return {
            "operation": "new_article",
            "base_state_sha256": base_spec["sha256"],
            "entry_id": entry["entry_id"],
            "article_id": entry["article_id"],
        }

    def publish(materialized, operation_id, _lock_fd):
        attempted.append(materialized["article_id"])
        if materialized["article_id"] == "article-topic-bad":
            raise buffer.PermanentArticleRejection(
                "primary_medical_source_missing",
                article_id="article-topic-bad",
                diagnostic="blog_article_gate:article-topic-bad:primary_medical_source_missing",
            )
        return {"status": "PASS_LIVE_GSC_READBACK_PASS", "operation_id": operation_id}

    result = buffer.consume_oldest(
        root=root,
        operation_id="skip-bad-and-publish-good",
        current_routes=set(),
        current_topic_ids=set(),
        base_state=base,
        work_root=tmp_path / "work",
        rematerialize=rematerialize,
        publish=publish,
    )

    assert result["status"] == "PASS_BUFFER_PUBLISHED"
    assert result["target_route"] == "/guides/good/"
    assert attempted == ["article-topic-bad", "article-topic-good"]
    assert [row["reason"] for row in result["quarantined_before_publication"]] == [
        "primary_medical_source_missing"
    ]
    quarantine_spec = result["quarantined_before_publication"][0]["receipt"]
    quarantine = buffer._read_sealed(
        Path(quarantine_spec["path"]),
        buffer.QUARANTINE_SCHEMA,
        "quarantine_sha256",
    )
    assert quarantine["publication_effects"] == 0
    assert quarantine["disposition"] == "excluded_from_ready_pool_claim_recycled_continue_next"
    assert buffer.pool_status(
        root,
        current_routes=set(),
        current_topic_ids=set(),
    )["ready"] == 0


def test_permanent_materialization_rejection_quarantines_before_publish(tmp_path):
    root = tmp_path / "pool"
    bad_packet, bad_receipt, control = build_candidate(
        tmp_path, "bad-source", "/guides/bad-source/", "topic-bad-source"
    )
    good_packet, good_receipt, _ = build_candidate(
        tmp_path, "good-source", "/guides/good-source/", "topic-good-source"
    )
    admit_candidate(root, bad_packet, bad_receipt, control, "2026-08-14T01:00:00Z")
    admit_candidate(root, good_packet, good_receipt, control, "2026-08-14T02:00:00Z")
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-current"})
    published: list[str] = []

    def rematerialize(_packet, entry, base_spec, _operation_id, _generation=0):
        result = {
            "operation": "new_article",
            "base_state_sha256": base_spec["sha256"],
            "entry_id": entry["entry_id"],
        }
        if entry["article_id"] == "article-topic-bad-source":
            result["permanent_rejection"] = {
                "reason": "inbound_link_source_missing:/guides/not-live/",
                "article_id": entry["article_id"],
                "diagnostic": "source absent from current article catalogs",
            }
        return result

    def publish(materialized, operation_id, _lock_fd):
        published.append(materialized["entry_id"])
        return {"status": "PASS_LIVE_GSC_READBACK_PASS", "operation_id": operation_id}

    result = buffer.consume_oldest(
        root=root,
        operation_id="skip-bad-materialization-and-publish-good",
        current_routes=set(),
        current_topic_ids=set(),
        base_state=base,
        work_root=tmp_path / "work",
        rematerialize=rematerialize,
        publish=publish,
    )

    assert result["status"] == "PASS_BUFFER_PUBLISHED"
    assert result["target_route"] == "/guides/good-source/"
    assert len(published) == 1
    assert result["quarantined_before_publication"][0]["reason"] == (
        "inbound_link_source_missing:/guides/not-live/"
    )
    quarantine_spec = result["quarantined_before_publication"][0]["receipt"]
    quarantine = buffer._read_sealed(
        Path(quarantine_spec["path"]),
        buffer.QUARANTINE_SCHEMA,
        "quarantine_sha256",
    )
    assert quarantine["disposition"] == (
        "excluded_from_ready_pool_claim_recycled_continue_next"
    )


def test_one_ready_article_publishes_even_below_supply_floor(tmp_path):
    root = tmp_path / "pool"
    packet, receipt, control = build_candidate(
        tmp_path, "below-floor", "/guides/below-floor/", "topic-below-floor"
    )
    admit_candidate(root, packet, receipt, control, "2026-08-14T01:00:00Z")
    assert buffer.pool_status(
        root,
        current_routes=set(),
        current_topic_ids=set(),
    )["at_or_above_minimum"] is False
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-current"})

    result = buffer.consume_oldest(
        root=root,
        operation_id="publish-below-floor",
        current_routes=set(),
        current_topic_ids=set(),
        base_state=base,
        work_root=tmp_path / "work",
        rematerialize=lambda _packet, entry, base_spec, _operation_id, _generation=0: {
            "operation": "new_article",
            "base_state_sha256": base_spec["sha256"],
            "entry_id": entry["entry_id"],
        },
        publish=lambda _materialized, operation_id, _lock_fd: {
            "status": "PASS_LIVE_GSC_READBACK_PASS",
            "operation_id": operation_id,
        },
    )
    assert result["status"] == "PASS_BUFFER_PUBLISHED"
    assert result["target_route"] == "/guides/below-floor/"


def test_terminal_publication_crash_recovers_after_pointer_and_ledger_advance(tmp_path):
    root = tmp_path / "pool"
    packet, receipt, control = build_candidate(
        tmp_path,
        "terminal-crash",
        "/guides/terminal-crash/",
        "topic-terminal-crash",
    )
    admit_candidate(root, packet, receipt, control, "2026-08-14T01:00:00Z")
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-current"})
    ledger = tmp_path / "published-ledger.json"
    write_json(ledger, {"entries": []})
    report = tmp_path / "publication"
    template = tmp_path / "apply-template.json"

    def rematerialize(_packet, entry, base_spec, _operation_id, _generation=0):
        write_json(
            template,
            {
                "schema": "glp-opus-article-apply-template-v1",
                "operation": "new_article",
                "topic_id": entry["topic_id"],
                "article_id": entry["article_id"],
                "target_route": entry["target_route"],
                "base_state_sha256": base_spec["sha256"],
                "report_dir": str(report.resolve()),
                "release_root": str((tmp_path / "release").resolve()),
                "published_ledger_path": str(ledger.resolve()),
            },
        )
        return {
            "operation": "new_article",
            "base_state_sha256": base_spec["sha256"],
            "apply_template": {
                "path": str(template.resolve()),
                "sha256": sha(template),
            },
        }

    def publish_then_crash(_materialized, _operation_id, _lock_fd):
        final = {
            "schema": "glp-opus-article-final-publication-v1",
            "status": "PASS_LIVE_GSC_READBACK_PASS",
            "operation": "new_article",
            "topic_id": "topic-terminal-crash",
            "article_id": "article-topic-terminal-crash",
            "live_url": "https://glpclinicscout.com/guides/terminal-crash/",
            "deployment_id": "dep-terminal",
            "public_tree_sha256": "c" * 64,
        }
        final["receipt_sha256"] = opus_payload_hash(final, "receipt_sha256")
        write_json(report / "10-final-publication-receipt.json", final)
        write_json(
            base,
            {
                "deployment_id": "dep-terminal",
                "release_root": str((tmp_path / "release").resolve()),
                "public_tree_sha256": "c" * 64,
            },
        )
        write_json(
            ledger,
            {
                "entries": [
                    {
                        "topic_id": "topic-terminal-crash",
                        "target_route": "/guides/terminal-crash/",
                        "deployment_id": "dep-terminal",
                        "status": "PASS_LIVE",
                    }
                ]
            },
        )
        raise RuntimeError("crash after terminal publication")

    with pytest.raises(RuntimeError, match="crash after terminal publication"):
        buffer.consume_oldest(
            root=root,
            operation_id="terminal-publication-crash",
            current_routes=set(),
            current_topic_ids=set(),
            base_state=base,
            work_root=tmp_path / "work",
            rematerialize=rematerialize,
            publish=publish_then_crash,
        )

    replay = buffer.consume_oldest(
        root=root,
        operation_id="terminal-publication-crash",
        current_routes=set(),
        current_topic_ids=set(),
        base_state=base,
        work_root=tmp_path / "work",
        rematerialize=lambda *_args: pytest.fail("terminal replay rematerialized"),
        publish=lambda *_args: pytest.fail("terminal replay published"),
        current_truth=lambda: (
            {"/guides/terminal-crash/"},
            {"topic-terminal-crash"},
        ),
    )
    assert replay["status"] == "PASS_BUFFER_PUBLISHED"
    assert replay["recovered_after_terminal_publication"] is True
    assert replay["publication"]["deployment_id"] == "dep-terminal"


def test_empty_or_invalid_buffer_fails_loudly(tmp_path):
    base = tmp_path / "base.json"
    write_json(base, {"deployment_id": "dep"})
    with pytest.raises(buffer.BufferEmptyError, match="article_buffer_empty_or_invalid"):
        buffer.consume_oldest(
            root=tmp_path / "empty",
            operation_id="empty-fallback",
            current_routes=set(),
            current_topic_ids=set(),
            base_state=base,
            work_root=tmp_path / "work",
            rematerialize=lambda *_args: {},
            publish=lambda *_args: {},
        )


def test_consumption_rereads_current_nonpublication_truth_under_lock(tmp_path):
    root = tmp_path / "pool"
    packet, receipt, control = build_candidate(
        tmp_path,
        "became-live",
        "/guides/became-live/",
        "topic-live",
    )
    admit_candidate(root, packet, receipt, control, "2026-08-14T01:00:00Z")
    base = tmp_path / "base.json"
    write_json(base, {"deployment_id": "dep-current"})
    with pytest.raises(buffer.BufferEmptyError, match="article_buffer_empty_or_invalid"):
        buffer.consume_oldest(
            root=root,
            operation_id="live-race-fallback",
            current_routes=set(),
            current_topic_ids=set(),
            base_state=base,
            work_root=tmp_path / "work",
            rematerialize=lambda *_args: pytest.fail("live entry rematerialized"),
            publish=lambda *_args: pytest.fail("live entry published"),
            current_truth=lambda: ({"/guides/became-live/"}, {"topic-live"}),
        )


def test_pool_contract_min_target_max(tmp_path):
    status = buffer.pool_status(tmp_path / "pool", current_routes=set(), current_topic_ids=set())
    assert (status["minimum"], status["target"], status["maximum"]) == (3, 4, 5)
    assert status["deficit_to_minimum"] == 3


def test_claimed_history_does_not_permanently_block_refill_to_max(tmp_path):
    root = tmp_path / "pool"
    candidates = [
        build_candidate(tmp_path, f"candidate-{index}", f"/guides/candidate-{index}/", f"topic-{index}")
        for index in range(6)
    ]
    for index, (packet, receipt, control) in enumerate(candidates[:5]):
        admit_candidate(root, packet, receipt, control, f"2026-08-14T0{index}:00:00Z")
    base = tmp_path / "current-content-base.json"
    write_json(base, {"deployment_id": "dep-current"})
    buffer.claim_oldest(
        root=root,
        operation_id="claimed-before-refill",
        current_routes=set(),
        current_topic_ids=set(),
        base_state=base,
    )
    packet, receipt, control = candidates[5]
    admit_candidate(root, packet, receipt, control, "2026-08-14T06:00:00Z")
    assert buffer.pool_status(root, current_routes=set(), current_topic_ids=set())["ready"] == 5


def test_nightly_refill_uses_standard_review_loop_with_apply_false(tmp_path):
    packet, receipt, control = build_candidate(tmp_path, "nightly", "/guides/nightly/", "topic-nightly")
    review_job = tmp_path / "nightly" / "review-job.json"
    write_json(review_job, {"schema": "glp-opus-seo-review-job-v1"})
    calls = []

    def review_runner(job, apply):
        calls.append((job, apply))
        return {
            "status": "PASS_OPUS_SEO_DRAFT",
            "publication_invoked": False,
            "rounds": [{"receipt": str(receipt)}],
        }

    result = buffer.refill_review_only(
        [{"packet": str(packet), "review_job": str(review_job)}],
        root=tmp_path / "pool",
        current_routes=set(),
        current_topic_ids=set(),
        mutable_article_routes=set(),
        review_runner=review_runner,
        control_files={str(control.resolve()): sha(control)},
        admitted_at_utc="2026-08-14T04:00:00Z",
    )
    assert calls == [(review_job, False)]
    assert result["review_apply"] is False
    assert result["pool"]["ready"] == 1
    assert result["status"] == "BUFFER_CAPACITY_DEFICIT"


def test_refill_candidate_that_fails_gates_does_not_count_as_supply(tmp_path):
    packet, _receipt, control = build_candidate(
        tmp_path, "blocked", "/guides/blocked/", "topic-blocked"
    )
    review_job = tmp_path / "blocked" / "review-job.json"
    write_json(review_job, {"schema": "glp-opus-seo-review-job-v1"})

    result = buffer.refill_review_only(
        [{"packet": str(packet), "review_job": str(review_job)}],
        root=tmp_path / "pool",
        current_routes=set(),
        current_topic_ids=set(),
        mutable_article_routes=set(),
        review_runner=lambda _job, _apply: {
            "status": "BLOCKED_OPUS_SEO_DRAFT",
            "publication_invoked": False,
            "rounds": [],
        },
        control_files={str(control.resolve()): sha(control)},
        admitted_at_utc="2026-08-14T04:00:00Z",
    )

    assert result["status"] == "BUFFER_CAPACITY_DEFICIT"
    assert result["pool"]["ready"] == 0
    assert result["admitted"] == []
    assert result["failures"]


def test_admission_copies_topic_authority_for_future_remint(tmp_path):
    root = tmp_path / "pool"
    packet, receipt, control = build_candidate(
        tmp_path, "snapshot", "/guides/snapshot/", "topic-snapshot"
    )
    manifest = admit_candidate(
        root, packet, receipt, control, "2026-08-14T01:00:00Z"
    )
    entry = json.loads(manifest.read_text())
    copied = Path(entry["topic_authority"]["path"])
    assert copied.is_file()
    assert copied.parent.name == "evidence"
    original = tmp_path / "snapshot" / "topic-authority.json"
    original.write_text("{\"drifted\":true}\n", encoding="utf-8")
    validated = buffer.validate_entry(
        manifest,
        current_routes=set(),
        current_topic_ids=set(),
    )
    assert validated["entry_id"] == entry["entry_id"]
    assert sha(copied) == entry["topic_authority"]["sha256"]


def test_admission_control_pins_are_provenance_not_live_tree_readiness(tmp_path):
    root = tmp_path / "pool"
    packet, receipt, control = build_candidate(
        tmp_path, "control-provenance", "/guides/control-provenance/", "topic-control-provenance"
    )
    manifest = admit_candidate(
        root, packet, receipt, control, "2026-08-14T01:00:00Z"
    )
    entry = json.loads(manifest.read_text())
    admitted_hash = entry["control_files"][str(control.resolve())]

    control.write_text("CONTROL = 'upgraded'\n", encoding="utf-8")

    validated = buffer.validate_entry(
        manifest,
        current_routes=set(),
        current_topic_ids=set(),
    )
    assert validated["control_files"][str(control.resolve())] == admitted_hash
    assert sha(control) != admitted_hash


def test_materializer_requires_snapshot_only_for_remint():
    source = (SCRIPTS / "materialize_opus_run.py").read_text(encoding="utf-8")
    assert 'parser.add_argument("--topic-authority-snapshot", type=Path)' in source
    assert 'remint_topic_authority_snapshot_required' in source
    assert 'topic_authority_snapshot_only_allowed_for_remint' in source
    assert 'args.topic_authority_snapshot.resolve(strict=True)' in source


def test_every_article_materialization_declares_author_profile_delta():
    source = (SCRIPTS / "materialize_opus_run.py").read_text(encoding="utf-8")
    new_article_branch = source.split("else:\n        update_ledger = None", 1)[1]
    new_article_branch = new_article_branch.split("apply_template =", 1)[0]
    assert "author_profile_public_path(article)" in new_article_branch
    assert "new_article_insights_public_delta(" in new_article_branch
    assert '*insights_delta["added"]' in new_article_branch
    assert '*insights_delta["changed"]' in new_article_branch
    r2_branch = source.split('if args.operation == "r2_refresh":', 1)[1]
    r2_branch = r2_branch.split("else:\n        update_ledger = None", 1)[0]
    assert "author_profile_public_changes(existing[0], article)" in r2_branch
    assert '            "index.html",' not in r2_branch
    assert '            "index.html",' in new_article_branch
