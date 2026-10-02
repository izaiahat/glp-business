"""Exact held-predecessor successor admission regressions (isolated fixture only)."""
import json
from pathlib import Path

import pytest

from test_article_buffer import build_candidate, admit_candidate, sha, write_json, opus_payload_hash
import article_buffer as buffer
import article_buffer_cli as cli
from carrier_lock import carrier_lock as real_carrier_lock


@pytest.fixture(autouse=True)
def isolated_carrier_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(buffer, "carrier_lock", lambda: real_carrier_lock(tmp_path / "carrier.lock"))


def successor_fixture(tmp_path, *, registered=False):
    root = tmp_path / "pool"
    packet, receipt, control = build_candidate(tmp_path, "topic", "/guides/topic/", "topic-one")
    predecessor = admit_candidate(root, packet, receipt, control, "2026-09-25T01:00:00Z")
    note = tmp_path / "review.json"
    write_json(note, {"finding": "old packet"})
    entry = json.loads(predecessor.read_text())
    buffer.hold_entry(root=root, entry_id=entry["entry_id"], entry_sha256=sha(predecessor),
                      reason="independent_review_supersession", evidence={"path": str(note), "sha256": sha(note)},
                      current_routes=set(), current_topic_ids=set())
    # A different, exactly reviewed packet under the SAME selected authority.
    candidate = (tmp_path / "registered" / "runs" / "fixture-review" / "packet"
                 if registered else tmp_path / "successor" / "packet")
    candidate.mkdir(parents=True)
    for source in packet.iterdir():
        (candidate / source.name).write_bytes(source.read_bytes())
    (candidate / "04-draft.md").write_text((candidate / "04-draft.md").read_text() + "\nReviewed correction.\n")
    manifest = json.loads(Path(json.loads(receipt.read_text())["subject_manifest_path"]).read_text())
    for row in manifest["subject_files"]:
        source = candidate / row["path"]
        row.update(sha256=sha(source), bytes=source.stat().st_size, lines=len(source.read_text().splitlines()))
    manifest["manifest_sha256"] = opus_payload_hash(manifest, "manifest_sha256")
    history = (tmp_path / "registered" / "runs" / "fixture-review" / "opus-history-fixture-review"
               if registered else tmp_path / "successor" / "history")
    history.mkdir(parents=True)
    manifest_path = history / "round-01-subject-manifest.json"
    write_json(manifest_path, manifest)
    reviewed = json.loads(receipt.read_text())
    if registered:
        reviewed["job_id"] = "fixture-review"
        manifest["job_id"] = "fixture-review"
        manifest["manifest_sha256"] = opus_payload_hash(manifest, "manifest_sha256")
        write_json(manifest_path, manifest)
        raw_path = history / "round-01-sol-raw.json"
        raw = json.loads(Path(reviewed["raw_review_path"]).read_text())
        raw["session_id"] = "fixture-review-session"
        write_json(raw_path, raw)
        reviewed["review_transport"] = "hermes-openai-codex-sol"
        reviewed["raw_review_path"] = str(raw_path)
        reviewed["raw_review_sha256"] = sha(raw_path)
    reviewed["subject_manifest_path"] = str(manifest_path)
    reviewed["subject_manifest_sha256"] = sha(manifest_path)
    reviewed["receipt_sha256"] = opus_payload_hash(reviewed, "receipt_sha256")
    pass_path = history / "round-01-receipt.json"
    write_json(pass_path, reviewed)
    for name in ("stdout", "stderr"):
        (history / f"round-01-review.{name}.txt").write_text("")
    execution = {"argv": ["reviewer", "--disable-tools", "--receipt-file", reviewed["raw_review_path"]],
                 "exit_code": 0, "streams": {
                     name: {"path": str(history / f"round-01-review.{name}.txt"),
                            "sha256": sha(history / f"round-01-review.{name}.txt"), "bytes": 0}
                     for name in ("stdout", "stderr")}}
    if registered:
        transport = buffer.review_transports.transport("hermes-openai-codex-sol")
        execution.update(job_id="fixture-review", round=1,
                         subject_manifest_sha256=sha(manifest_path), raw_review_sha256=sha(raw_path))
        execution["argv"] = ["python3", str(transport["wrapper"]), "--disable-tools",
                              "--provider", transport["provider"], "--model", transport["model"],
                              "--effort", transport["effort"], "--receipt-file", str(raw_path)]
    write_json(history / "round-01-review-execution.json", execution)
    closeout = {"schema": "glp-opus-seo-loop-closeout-v1", "status": "PASS_OPUS_SEO_DRAFT",
                "publication_invoked": False, "topic_id": reviewed["topic_id"], "job_id": reviewed["job_id"],
                "rounds": [{"round": 1, "receipt": str(pass_path), "sha256": sha(pass_path),
                            "verdict": "PASS_OPUS_SEO_DRAFT"}]}
    if registered:
        closeout.update(review_execution_sha256=sha(history / "round-01-review-execution.json"),
                        raw_review_sha256=sha(raw_path), subject_manifest_sha256=sha(manifest_path),
                        packet_subject_sha256={row["path"]: row["sha256"] for row in manifest["subject_files"]})
        job = {"schema": "glp-opus-seo-review-job-v1", "job_id": "fixture-review",
               "packet_dir": str(candidate.resolve()), "history_dir": str(history.resolve()),
               "subject_files": [row["path"] for row in manifest["subject_files"]],
               "topic_id": reviewed["topic_id"], "target_route": "/guides/topic/",
               "review_transport": "hermes-openai-codex-sol"}
        write_json(history.parent / "review-job-fixture-review.json", job)
    closeout["closeout_sha256"] = opus_payload_hash(closeout, "closeout_sha256")
    write_json(history / "loop-closeout.json", closeout)
    return root, candidate, pass_path, control, predecessor, entry["entry_id"]


@pytest.mark.parametrize("drift", ["none", "wrong_predecessor", "unheld", "selection", "packet",
                                   "closeout", "execution", "day_drift", "unbound_publisher", "old_receipt", "extra_duplicate"])
def test_exact_registered_successor(tmp_path, monkeypatch, drift):
    root, packet, receipt, control, old, old_id = successor_fixture(tmp_path, registered=True)
    monkeypatch.setattr(buffer, "REGISTERED_REVIEW_ROOT", tmp_path / "registered")
    monkeypatch.setattr(buffer, "pt_date_now", lambda: "2026-09-26")
    controls = {str(control.resolve()): sha(control)}
    for name in ("build_opus_article_release.py", "publish_opus_article_release.py"):
        path = Path(buffer.__file__).with_name(name)
        controls[str(path)] = sha(path)
    request = dict(root=root, packet=packet, pass_receipt=receipt,
                   control_files=controls, current_routes=set(),
                   current_topic_ids=set(), mutable_article_routes=set(),
                   admitted_at_utc="2026-09-26T12:00:00Z", predecessor_entry_id=old_id,
                   today_pt="2026-09-26")
    if drift == "wrong_predecessor":
        request["predecessor_entry_id"] = "a" * 24
    elif drift == "unheld":
        (root / "held" / f"{old_id}.json").unlink()
    elif drift == "selection":
        # Valid alternate selection for another topic may never borrow the held route.
        request["packet"] = build_candidate(tmp_path, "other", "/guides/topic/", "other-topic")[0]
    elif drift == "packet":
        (packet / "04-draft.md").write_text("tampered")
    elif drift == "closeout":
        path = receipt.parent / "loop-closeout.json"
        closeout = json.loads(path.read_text()); closeout["publication_invoked"] = True
        write_json(path, closeout)
    elif drift == "execution":
        path = receipt.parent / "round-01-review-execution.json"
        execution = json.loads(path.read_text()); execution["exit_code"] = 1
        write_json(path, execution)
    elif drift == "day_drift":
        request["today_pt"] = "2026-09-25"
    elif drift == "unbound_publisher":
        request["control_files"] = {str(control.resolve()): sha(control)}
    elif drift == "old_receipt":
        request["pass_receipt"] = Path(json.loads(old.read_text())["pass_receipt"]["path"])
    elif drift == "extra_duplicate":
        (root / "entries" / ("f" * 24)).mkdir()
        (root / "entries" / ("f" * 24) / "entry.json").write_bytes(old.read_bytes())
    if drift != "none":
        with pytest.raises((buffer.BufferAdmissionError, RuntimeError, ValueError, KeyError)):
            buffer.admit(**request)
        assert len(list((root / "entries").glob("*/entry.json"))) <= (2 if drift == "extra_duplicate" else 1)
        return
    parsed = cli.build_parser().parse_args(["admit-successor", "--packet", str(packet),
        "--pass-receipt", str(receipt), "--predecessor-entry-id", old_id])
    assert parsed.predecessor_entry_id == old_id
    admitted = buffer.admit(**request)
    assert json.loads(admitted.read_text())["successor_of"]["entry_id"] == old_id
    assert (root / "held" / f"{old_id}.json").exists()
    assert buffer.admit(**request) == admitted
    assert len(buffer.ready_entries(root, current_routes=set(), current_topic_ids=set())) == 1
    assert old.read_bytes() == Path(json.loads(admitted.read_text())["successor_of"]["entry_manifest"]["path"]).read_bytes()


def test_handwritten_self_hashed_review_is_not_registered(tmp_path, monkeypatch):
    root, packet, receipt, control, old, old_id = successor_fixture(tmp_path)
    monkeypatch.setattr(buffer, "pt_date_now", lambda: "2026-09-26")
    controls = {str(control.resolve()): sha(control)}
    for name in ("build_opus_article_release.py", "publish_opus_article_release.py"):
        path = Path(buffer.__file__).with_name(name)
        controls[str(path)] = sha(path)
    with pytest.raises(buffer.BufferAdmissionError, match="unregistered_location"):
        buffer.admit(root=root, packet=packet, pass_receipt=receipt, control_files=controls,
                     current_routes=set(), current_topic_ids=set(), mutable_article_routes=set(),
                     admitted_at_utc="2026-09-26T12:00:00Z", predecessor_entry_id=old_id,
                     today_pt="2026-09-26")


@pytest.mark.parametrize("kind", ["claims", "consumed", "quarantined", "published"])
def test_held_predecessor_with_other_record_is_ineligible(tmp_path, monkeypatch, kind):
    root, packet, receipt, control, old, old_id = successor_fixture(tmp_path, registered=True)
    monkeypatch.setattr(buffer, "REGISTERED_REVIEW_ROOT", tmp_path / "registered")
    monkeypatch.setattr(buffer, "pt_date_now", lambda: "2026-09-26")
    controls = {str(control.resolve()): sha(control)}
    for name in ("build_opus_article_release.py", "publish_opus_article_release.py"):
        path = Path(buffer.__file__).with_name(name)
        controls[str(path)] = sha(path)
    directory = root / kind
    directory.mkdir(exist_ok=True)
    (directory / "stale.json").write_text(json.dumps({"entry_id": old_id}))
    with pytest.raises(buffer.BufferAdmissionError, match="predecessor_.*_recorded"):
        buffer.admit(root=root, packet=packet, pass_receipt=receipt, control_files=controls,
                     current_routes=set(), current_topic_ids=set(), mutable_article_routes=set(),
                     admitted_at_utc="2026-09-26T12:00:00Z", predecessor_entry_id=old_id,
                     today_pt="2026-09-26")


def test_stale_claim_hold_before_recycle_never_restores_ready(tmp_path, monkeypatch):
    root, packet, receipt, control, old, old_id = successor_fixture(tmp_path)
    (root / "held" / f"{old_id}.json").unlink()
    entry = json.loads(old.read_text())
    claim_path = root / "claims" / "fixture.json"
    buffer._write_once(claim_path, {"schema": buffer.CLAIM_SCHEMA, "status": "CLAIMED_OLDEST_VALID_READY_ENTRY",
                                    "operation_id": "retired-owner", "entry_id": old_id,
                                    "topic_id": entry["topic_id"], "target_route": entry["target_route"],
                                    "claimed_pt_date": "2026-09-18", "claimed_at_utc": "2026-09-18T18:00:25Z",
                                    "entry_manifest": buffer._spec(old)}, "claim_sha256")
    evidence_path = tmp_path / "review.json"; write_json(evidence_path, {"finding": "dated claim requires rereview"})
    request = dict(root=root, entry_id=old_id, entry_sha256=sha(old), reason="stale_claim_requires_review",
                   evidence=buffer._spec(evidence_path), current_routes=set(), current_topic_ids=set(),
                   allow_stale_claim=True)
    monkeypatch.setattr(buffer, "pt_date_now", lambda: "2026-09-26")
    assert buffer.hold_entry(**{**request, "dry_run": True})["status"] == "DRY_RUN_BUFFER_ENTRY_HOLD"
    assert buffer.hold_entry(**request)["status"] == "PASS_BUFFER_ENTRY_HELD"
    base = tmp_path / "base.json"; write_json(base, {"deployment_id": "fixture"})
    assert buffer.recycle_stale_claims(root=root, work_root=tmp_path / "work", base_state=base,
                                       today_pt="2026-09-26")["retired"]
    assert buffer.ready_entries(root, current_routes=set(), current_topic_ids=set()) == []


def test_stale_claim_hold_rejects_current_day(tmp_path, monkeypatch):
    root, packet, receipt, control, old, old_id = successor_fixture(tmp_path)
    (root / "held" / f"{old_id}.json").unlink()
    entry = json.loads(old.read_text())
    claim_path = root / "claims" / "fixture.json"
    buffer._write_once(claim_path, {"schema": buffer.CLAIM_SCHEMA, "entry_id": old_id,
                                    "claimed_pt_date": "2026-09-26", "topic_id": entry["topic_id"],
                                    "target_route": entry["target_route"]}, "claim_sha256")
    evidence_path = tmp_path / "review.json"; write_json(evidence_path, {"review": "required"})
    monkeypatch.setattr(buffer, "pt_date_now", lambda: "2026-09-26")
    with pytest.raises(RuntimeError, match="buffer_hold_entry_claimed"):
        buffer.hold_entry(root=root, entry_id=old_id, entry_sha256=sha(old),
                          reason="stale_claim_requires_review", evidence=buffer._spec(evidence_path),
                          current_routes=set(), current_topic_ids=set(), allow_stale_claim=True)

