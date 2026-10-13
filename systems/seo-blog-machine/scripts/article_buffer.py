#!/usr/bin/env python3
"""Immutable 3–5 article buffer with packet-bound Sol admission."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import review_transports
from carrier_lock import carrier_lock
from selection_receipt import load_selection_receipt

# Five weekday publications require a five-article independently accepted reserve.
READY_MIN = 5
READY_TARGET = 5
READY_MAX = 5
PT = ZoneInfo("America/Los_Angeles")
REQUIRED_SUBJECTS = {
    "00-authority.json",
    "01-evidence-ledger.json",
    "02-serp-brief.json",
    "03-editorial-brief.md",
    "04-draft.md",
    "05-claim-source-map.json",
    "06-article.json",
    "07-qa-receipt.json",
}
ENTRY_SCHEMA = "glp-seo-article-buffer-entry-v1"
CLAIM_SCHEMA = "glp-seo-article-buffer-claim-v1"
CLAIM_RETIREMENT_SCHEMA = "glp-seo-article-buffer-claim-retirement-v1"
QUARANTINE_SCHEMA = "glp-seo-article-buffer-quarantine-v1"
MATERIALIZATION_SCHEMA = "glp-seo-article-buffer-materialization-v1"
REMATERIALIZATION_SCHEMA = "glp-seo-article-buffer-rematerialization-v1"
CONSUME_SCHEMA = "glp-seo-article-buffer-consumption-v1"
HOLD_SCHEMA = "glp-seo-article-buffer-entry-hold-v1"
ENTRY_ID_PATTERN = re.compile(r"[0-9a-f]{24}")
MEDICAL_REFRAME_REASON = "high_risk_medical_claim_must_be_reframed"
PT_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
ROUTE_PATTERN = re.compile(r"^/(?:guides|best|reviews|cost|tools|compare)/[a-z0-9]+(?:-[a-z0-9]+)*/$")
RESUME_PLAN_CONTROLS = (
    Path(__file__).resolve().with_name("article_buffer_cli.py"),
)


class BufferEmptyError(RuntimeError):
    pass


class BufferAdmissionError(RuntimeError):
    pass


class PermanentArticleRejection(RuntimeError):
    """A sealed candidate failed a deterministic, candidate-local publish gate."""

    def __init__(
        self,
        reason: str,
        *,
        article_id: str = "",
        diagnostic: str = "",
    ) -> None:
        self.reason = str(reason or "permanent_article_gate_rejection")
        self.article_id = str(article_id or "")
        self.diagnostic = str(diagnostic or "")
        super().__init__(self.reason)


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _payload_hash(value: dict[str, Any], field: str) -> str:
    unsigned = dict(value)
    unsigned.pop(field, None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _opus_payload_hash(value: dict[str, Any], field: str) -> str:
    """Match opus_review_loop.payload_hash (which has no trailing newline)."""
    unsigned = dict(value)
    unsigned.pop(field, None)
    rendered = json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(rendered).hexdigest()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _spec(path: Path) -> dict[str, str]:
    source = path.resolve(strict=True)
    if not source.is_file() or source.is_symlink():
        raise BufferAdmissionError(f"buffer_file_invalid:{source}")
    return {"path": str(source), "sha256": _sha(source)}


def _verify_spec(spec: dict[str, Any], label: str) -> Path:
    if not isinstance(spec, dict) or set(spec) < {"path", "sha256"}:
        raise RuntimeError(f"{label}_spec_invalid")
    path = Path(str(spec.get("path") or "")).resolve()
    expected = str(spec.get("sha256") or "")
    if not path.is_file() or path.is_symlink() or len(expected) != 64 or _sha(path) != expected:
        raise RuntimeError(f"{label}_pin_invalid")
    return path


TOPIC_QUEUE_SNAPSHOT_GLOB = "topic-queue-snapshot-*.json"


def _resolve_topic_authority(spec: dict[str, Any], packet: Path) -> Path:
    """Resolve the pinned topic-queue bytes, from the live file or a run snapshot.

    The selection receipt pins ``topic-queues/latest.json`` by DIGEST. That file
    legitimately regenerates (daily feed, weekly learning), which is exactly why
    ``materialize_opus_run`` already pins a run-scoped SNAPSHOT instead of the
    live path for review jobs. Admission still resolved the live path, so once
    the queue regenerated, every reviewed packet older than the newest queue was
    permanently unadmittable with ``buffer_topic_authority_pin_invalid`` —
    observed 2026-08-18 on ``/guides/oral-glp-1-insurance-coverage/`` after the
    2026-08-17 17:47 regeneration, with the buffer already unpublishable.

    The DIGEST is the authority; the path is only where those bytes were found.
    So fall back to the run-scoped snapshots written beside the packet and accept
    exactly one whose bytes hash to the pinned digest. Nothing is weakened: a
    snapshot that does not hash to the pin is ignored, and if no source carries
    the pinned bytes this still fails closed.
    """
    if not isinstance(spec, dict) or set(spec) < {"path", "sha256"}:
        raise RuntimeError("buffer_topic_authority_spec_invalid")
    expected = str(spec.get("sha256") or "")
    if len(expected) != 64:
        raise RuntimeError("buffer_topic_authority_pin_invalid")
    live = Path(str(spec.get("path") or "")).resolve()
    if live.is_file() and not live.is_symlink() and _sha(live) == expected:
        return live
    # Admission retains the exact queue under the immutable entry evidence.
    # Carrier validation runs on that copied packet after the mutable live queue
    # may regenerate. Digest equality is still mandatory, never path authority.
    snapshots = list(packet.resolve().parent.glob(TOPIC_QUEUE_SNAPSHOT_GLOB))
    snapshots.append(packet.resolve().parent / 'evidence/topic_authority.json')
    for candidate in sorted(snapshots):
        if candidate.is_file() and not candidate.is_symlink() and _sha(candidate) == expected:
            return candidate.resolve()
    raise RuntimeError("buffer_topic_authority_pin_invalid")


def validate_topic_provenance(
    selection_receipt: dict[str, Any],
    selected: dict[str, Any],
    topic_authority_path: Path,
    *,
    error: type[Exception] = RuntimeError,
) -> dict[str, Any]:
    """Prove selection came from the measured keyword-gap/white-space supply."""

    def fail(reason: str) -> None:
        raise error(reason)

    provenance = selected.get("topic_provenance")
    if not isinstance(provenance, dict) or provenance.get("schema") != "glp-seo-topic-provenance-v1":
        fail("buffer_topic_provenance_missing")
    if (
        provenance.get("keyword") != selected.get("target_query")
        or not isinstance(provenance.get("selection_reason"), str)
        or not provenance["selection_reason"].strip()
        or provenance.get("selection_mode") != selection_receipt.get("selection_mode")
    ):
        fail("buffer_topic_provenance_reason_invalid")
    gap_source = provenance.get("gap_source")
    strategy_gap = isinstance(gap_source, dict) and (
        gap_source.get("type") == "strategy_competitor_gap"
        and gap_source.get("opportunity_type") == "competitor_gap_new_topic"
        and gap_source.get("opportunity_class") is None
        and gap_source.get("source_opportunity_id") is None
    )
    if (
        not isinstance(gap_source, dict)
        or (not strategy_gap and (
            gap_source.get("type") != "keyword_gap"
            or gap_source.get("opportunity_type") != "weekly_keyword_gap"
            or gap_source.get("opportunity_class") not in {"B", "C"}
            or not isinstance(gap_source.get("source_opportunity_id"), str)
            or not gap_source["source_opportunity_id"].strip()
        ))
        or gap_source.get("operator_directive_id") != selected.get("operator_directive_id")
    ):
        fail("buffer_topic_provenance_gap_source_invalid")
    evidence = provenance.get("evidence")
    keyword_evidence = provenance.get("keyword_evidence")
    analytics = provenance.get("analytics")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not isinstance(row, dict) or not row for row in evidence)
        or not isinstance(keyword_evidence, dict)
        or not keyword_evidence
        or not isinstance(analytics, dict)
        or not isinstance(analytics.get("gsc_preference"), dict)
    ):
        fail("buffer_topic_provenance_evidence_invalid")
    artifacts = provenance.get("source_artifacts")
    if strategy_gap:
        if not isinstance(artifacts, dict):
            fail("buffer_topic_provenance_source_artifacts_invalid")
        # A strategy rotation is not a weekly opportunity. Preserve its actual
        # authority, rather than inventing a weekly class or opportunity ID.
        sources = {}
        for key in ("strategy_map", "strategy_queue"):
            spec = (artifacts or {}).get(key)
            if (not isinstance(spec, dict)
                    or spec != (selection_receipt.get("inputs") or {}).get(key)
                    or not Path(str(spec.get("path") or "")).is_absolute()):
                fail("buffer_topic_provenance_strategy_input_invalid")
            try:
                path = _verify_spec(spec, "strategy")
                sources[key] = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(sources[key], dict):
                    fail("buffer_topic_provenance_strategy_document_invalid")
            except (RuntimeError, OSError, UnicodeError, json.JSONDecodeError):
                fail("buffer_topic_provenance_strategy_pin_invalid")
        rows = [row for row in (sources["strategy_queue"].get("queue") or [])
                if isinstance(row, dict)
                and row.get("target_query") == selected.get("target_query")
                and row.get("target_route") == selected.get("target_route")]
        if len(rows) != 1 or any(rows[0].get(key) != selected.get(key)
                                 for key in ("cluster_id", "queue_position", "depth_stage")):
            fail("buffer_topic_provenance_strategy_queue_invalid")
        clusters = [row.get("id") for row in (sources["strategy_map"].get("clusters") or [])
                    if isinstance(row, dict)]
        if not selected.get("cluster_id") or clusters.count(selected["cluster_id"]) != 1:
            fail("buffer_topic_provenance_strategy_cluster_invalid")
        volume = keyword_evidence.get("merged_search_volume")
        if (isinstance(volume, bool) or not isinstance(volume, (int, float))
                or not math.isfinite(volume) or volume <= 0):
            fail("buffer_topic_provenance_strategy_demand_invalid")
        if (not isinstance(selected.get("predecessor_proof"), dict)
                or selected["predecessor_proof"].get("satisfied") is not True):
            fail("buffer_topic_provenance_strategy_predecessor_invalid")
    else:
        overlay = (artifacts or {}).get("keyword_gap_strategy_overlay")
        opportunity = (artifacts or {}).get("keyword_gap_opportunity_artifact")
        receipt_overlay = (selection_receipt.get("inputs") or {}).get(
            "keyword_gap_strategy_overlay"
        )
        if (
            not isinstance(artifacts, dict)
            or not isinstance(overlay, dict)
            or overlay != receipt_overlay
            or not Path(str(overlay.get("path") or "")).is_absolute()
            or len(str(overlay.get("sha256") or "")) != 64
            or not isinstance(opportunity, dict)
            or not Path(str(opportunity.get("path") or "")).is_absolute()
            or len(str(opportunity.get("sha256") or "")) != 64
            or len(str(opportunity.get("artifact_sha256") or "")) != 64
        ):
            fail("buffer_topic_provenance_source_artifacts_invalid")
    try:
        authority = json.loads(topic_authority_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        fail("buffer_topic_provenance_authority_invalid")
    matches = [
        row
        for row in (authority.get("queue") or [])
        if isinstance(row, dict) and row.get("topic_id") == selected.get("topic_id")
    ]
    if len(matches) != 1:
        fail("buffer_topic_provenance_authority_topic_invalid")
    authority_row = matches[0]
    directive = authority_row.get("competitor_gap")
    if (
        authority_row.get("target_query") != selected.get("target_query")
        or authority_row.get("evidence_state") != "READY_FOR_RESEARCH"
        or not isinstance(directive, dict)
        or directive.get("operator_ready_for_research") is not True
        or directive.get("operator_directive_id") != selected.get("operator_directive_id")
        or directive.get("target_route") != selected.get("target_route")
        or directive.get("opportunity_type") != gap_source.get("opportunity_type")
        or directive.get("opportunity_class") != gap_source.get("opportunity_class")
        or directive.get("source_opportunity_id") != gap_source.get("source_opportunity_id")
        or directive.get("rationale") != provenance.get("selection_reason")
        or directive.get("evidence") != evidence
        or authority_row.get("keyword_evidence") != keyword_evidence
        or selected.get("gsc_preference") != analytics.get("gsc_preference")
    ):
        fail("buffer_topic_provenance_authority_binding_invalid")
    return dict(provenance)


def _write_once(path: Path, value: dict[str, Any], hash_field: str) -> Path:
    target = path.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(value)
    payload[hash_field] = _payload_hash(payload, hash_field)
    rendered = _canonical(payload)
    if target.exists():
        if target.read_bytes() != rendered:
            raise RuntimeError(f"buffer_cas_conflict:{target}")
        os.chmod(target, 0o444)
        return target
    descriptor, raw = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o444)
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != rendered:
                raise RuntimeError(f"buffer_cas_conflict:{target}")
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _read_sealed(path: Path, schema: str, field: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != schema or value.get(field) != _payload_hash(value, field):
        raise RuntimeError(f"buffer_artifact_invalid:{path}")
    return value


def pt_date_now() -> str:
    """Today in the machine's operating timezone (schedules are PT)."""
    return dt.datetime.now(PT).date().isoformat()


def _pt_date_of_epoch(epoch: float) -> str:
    return dt.datetime.fromtimestamp(epoch, PT).date().isoformat()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def claim_pt_date(claim: dict[str, Any], claim_path: Path) -> str:
    """The PT day a claim was taken.

    Claims sealed before 2026-08-20 carry no stamp, because nothing ever read
    one. Their file mtime is the honest fallback: ``_write_once`` creates the
    file once, immediately, and seals it 0444, so the mtime cannot be later
    than the claim itself.
    """
    value = claim.get("claimed_pt_date")
    if isinstance(value, str) and PT_DATE_PATTERN.fullmatch(value):
        return value
    return _pt_date_of_epoch(claim_path.stat().st_mtime)


def refill_base_status(work_root: Path, base_state: Path) -> dict[str, Any]:
    """Report whether the latest refill was sealed against the live base.

    Refill closeouts are the existing immutable checkpoint for the nightly
    refill lane. Comparing their recorded ``sha256_after`` with the current
    pointer makes a content-base change an explicit refill trigger without
    adding scheduler state or mutating the read-only status command.
    """
    current = _spec(base_state)
    root = work_root.resolve()
    candidates = {
        *root.glob("glp-seo-article-buffer-closeout-v1-*.json"),
        *root.glob("*/glp-seo-article-buffer-closeout-v1-*.json"),
    }
    valid: list[tuple[str, Path, str]] = []
    invalid = 0
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("schema") != "glp-seo-article-buffer-closeout-v1":
                invalid += 1
                continue
            base = value.get("current_content_base")
            observed = base.get("sha256_after") if isinstance(base, dict) else None
            completed = value.get("completed_at_utc")
            if not isinstance(observed, str) or not re.fullmatch(r"[0-9a-f]{64}", observed):
                invalid += 1
                continue
            valid.append((str(completed or ""), path, observed))
        except (OSError, UnicodeError, json.JSONDecodeError):
            invalid += 1
    if not valid:
        return {
            "schema": "glp-seo-article-buffer-refill-trigger-v1",
            "status": "REFILL_REQUIRED_BASELINE_MISSING",
            "changed": True,
            "current": current,
            "previous": None,
            "invalid_closeout_count": invalid,
        }
    _completed, closeout, observed = max(valid, key=lambda row: (row[0], row[1].stat().st_mtime_ns))
    changed = observed != current["sha256"]
    return {
        "schema": "glp-seo-article-buffer-refill-trigger-v1",
        "status": "REFILL_REQUIRED_CURRENT_CONTENT_BASE_CHANGED" if changed else "PASS_REFILL_BASE_ALIGNED",
        "changed": changed,
        "current": current,
        "previous": {"closeout": str(closeout), "sha256": observed},
        "invalid_closeout_count": invalid,
    }


MATERIALIZATION_GENERATION_PATTERN = re.compile(r"materialization-r(\d+)\.json")


def _materialization_candidates(
    workspace: Path,
) -> list[tuple[int, Path, dict[str, Any]]]:
    """Return every sealed generation in numeric order."""
    root = workspace.resolve()
    candidates: list[tuple[int, Path, dict[str, Any]]] = []
    base = root / "materialization.json"
    if base.is_file():
        candidates.append((
            0,
            base,
            _read_sealed(base, MATERIALIZATION_SCHEMA, "materialization_sha256"),
        ))
    for candidate in sorted(root.glob("materialization-r*.json")) if root.is_dir() else []:
        match = MATERIALIZATION_GENERATION_PATTERN.fullmatch(candidate.name)
        if match is None:
            continue
        candidates.append((
            int(match.group(1)),
            candidate,
            _read_sealed(candidate, MATERIALIZATION_SCHEMA, "materialization_sha256"),
        ))
    return sorted(candidates, key=lambda row: row[0])


def _latest_materialization(workspace: Path) -> tuple[int, Path, dict[str, Any] | None]:
    """The newest sealed materialization record for one operation workspace.

    Generation 0 is ``materialization.json``; a control-pin drift regeneration
    writes ``materialization-r<n>.json`` beside it (see
    ``apply_template_control_drift``). Every reader must follow the newest
    record, or a resume replays the exact plan that already failed.
    """
    root = workspace.resolve()
    candidates = _materialization_candidates(root)
    if not candidates:
        return 0, root / "materialization.json", None
    return candidates[-1]


def _post_deploy_recovery_materialization(
    workspace: Path,
    *,
    operation_id: str,
    entry: dict[str, Any],
    base_spec: dict[str, str],
) -> tuple[int, Path, dict[str, Any]] | None:
    """Find an older generation that deployed but did not finish publication.

    A control fix normally mints a new generation. If the prior generation
    already became the live deployment, rebuilding from the stale pointer can
    never be correct: the only safe continuation is its existing post-deploy
    receipt chain. This selector performs no effect and grants no acceptance;
    the CLI adapter and publisher still validate the complete chain, current
    Cloudflare head, live bytes, and pointer/ledger CAS before writing truth.
    """
    for generation, path, record in reversed(_materialization_candidates(workspace)):
        if (
            record.get("operation_id") != operation_id
            or record.get("entry_id") != entry.get("entry_id")
        ):
            raise RuntimeError("buffer_materialization_replay_mismatch")
        # Older immutable generations are expected after an authorized lane
        # advances current-content-base. They remain provenance, but cannot be
        # resumed against a different head.
        if record.get("current_base_state") != base_spec:
            continue
        materialized = record.get("materialized")
        if not isinstance(materialized, dict) or not isinstance(materialized.get("apply_template"), dict):
            continue
        template_path = _verify_spec(
            materialized["apply_template"], "buffer_post_deploy_apply_template"
        )
        try:
            template = json.loads(template_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RuntimeError("buffer_post_deploy_apply_template_invalid") from error
        if (
            template.get("schema") != "glp-opus-article-apply-template-v1"
            or template.get("operation", "new_article") != "new_article"
            or template.get("topic_id") != entry.get("topic_id")
            or template.get("article_id") != entry.get("article_id")
            or template.get("target_route") != entry.get("target_route")
            or template.get("base_state_sha256") != base_spec.get("sha256")
        ):
            raise RuntimeError("buffer_post_deploy_apply_template_binding_invalid")
        raw_report = template.get("report_dir")
        raw_release = template.get("release_root")
        if not isinstance(raw_report, str) or not isinstance(raw_release, str):
            raise RuntimeError("buffer_post_deploy_paths_invalid")
        report = Path(raw_report).resolve()
        release = Path(raw_release).resolve()
        deployment_paths = [
            report / name
            for name in ("06-deployment-receipt.json", "06b-deployment-reconciliation.json")
            if (report / name).exists()
        ]
        effect_markers = [
            *deployment_paths,
            report / "07-live-byte-verification.json",
            report / "08-cache-purge-receipt.json",
        ]
        if not any(candidate.exists() for candidate in effect_markers):
            continue
        if (
            raw_report != str(report)
            or raw_release != str(release)
            or not report.is_dir()
            or report.is_symlink()
            or not release.is_dir()
            or release.is_symlink()
            or materialized.get("release_root") != raw_release
        ):
            raise RuntimeError("buffer_post_deploy_paths_invalid")
        if (report / "10-final-publication-receipt.json").exists():
            continue
        required = [
            report / "01-deployment-manifest.json",
            report / "02-immutable-release-seal.json",
            report / "07-live-byte-verification.json",
            report / "08-cache-purge-receipt.json",
            report / "release-job.json",
        ]
        if len(deployment_paths) not in {1, 2} or any(
            not candidate.is_file() or candidate.is_symlink()
            for candidate in [*deployment_paths, *required]
        ):
            raise RuntimeError("buffer_post_deploy_receipts_incomplete")
        return generation, path, record
    return None


def apply_template_control_drift(materialized: Any) -> list[dict[str, str]]:
    """Re-check a CACHED apply template's control pins against current bytes.

    The buffer deliberately keeps admission-time ``control_files`` as historical
    provenance, and the consume path rematerializes against the CURRENT chain
    before any effect. What the resume path did not do was re-check the plan it
    had already cached: on 2026-08-20 a legitimate carrier fix changed a pinned
    control after the first materialization, and every retry replayed the stale
    template straight into ``apply_opus_article_release``'s ``control_drift``
    guard — a permanent dead end for that operation id.

    The guard is right and stays exactly as strict. The plan is what is stale,
    so this reports the drift and the caller REGENERATES. Returns [] when the
    cached template still binds the current tree byte-for-byte, or when the
    materialization carries no apply template at all.
    """
    if not isinstance(materialized, dict):
        return [{"control": "<materialization>", "reason": "materialization_not_a_mapping"}]
    spec = materialized.get("apply_template")
    if not isinstance(spec, dict):
        return []
    path = Path(str(spec.get("path") or ""))
    expected = str(spec.get("sha256") or "")
    if not path.is_file() or path.is_symlink():
        return [{"control": str(path), "reason": "apply_template_missing"}]
    if len(expected) != 64 or _sha(path) != expected:
        return [{"control": str(path), "reason": "apply_template_bytes_changed"}]
    try:
        template = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [{"control": str(path), "reason": "apply_template_unreadable"}]
    controls = (template or {}).get("control_files")
    if not isinstance(controls, dict) or not controls:
        return [{"control": str(path), "reason": "apply_template_control_pins_missing"}]
    drift: list[dict[str, str]] = []
    for required in RESUME_PLAN_CONTROLS:
        resolved = str(required.resolve())
        if resolved not in controls:
            drift.append({"control": resolved, "reason": "resume_control_pin_missing"})
    for raw, pinned in sorted(controls.items()):
        control = Path(str(raw))
        if not control.is_file() or control.is_symlink():
            drift.append({"control": str(control), "reason": "control_missing"})
            continue
        current = _sha(control)
        if current != str(pinned):
            drift.append({
                "control": str(control),
                "reason": "control_bytes_changed",
                "pinned_sha256": str(pinned),
                "current_sha256": current,
            })
    return drift


def validate_completed_generation_lineage(workspace: Path, claim: dict[str, Any]) -> list[dict[str, Any]]:
    """Authenticate a rebased claim through every immutable materialization."""
    candidates = _materialization_candidates(workspace)
    if not candidates or [row[0] for row in candidates] != list(range(candidates[-1][0] + 1)):
        raise RuntimeError("buffer_completed_generation_chain_invalid")
    previous = None
    for generation, path, record in candidates:
        if (record.get("operation_id") != claim.get("operation_id")
            or record.get("entry_id") != claim.get("entry_id")
            or record.get("generation", 0) != generation):
            raise RuntimeError("buffer_completed_generation_identity_drift")
        materialized = record.get("materialized") or {}
        template = json.loads(_verify_spec(materialized.get("apply_template"), "buffer_completed_generation_template").read_text())
        if (template.get("topic_id") != claim.get("topic_id")
            or template.get("target_route") != claim.get("target_route")
            or template.get("base_state_sha256") != record["current_base_state"]["sha256"]
            or template.get("release_root") != materialized.get("release_root")):
            raise RuntimeError("buffer_completed_generation_identity_drift")
        if previous is None:
            if record.get("current_base_state") != claim.get("current_base_state"):
                raise RuntimeError("buffer_completed_generation_claim_base_drift")
        else:
            pg, pp, pr = previous
            expected = {"generation": pg, "materialization": str(pp),
                        "apply_template": pr["materialized"]["apply_template"],
                        "release_root": pr["materialized"]["release_root"]}
            if record.get("supersedes") != expected:
                raise RuntimeError("buffer_completed_generation_chain_invalid")
            if (record["current_base_state"] != pr["current_base_state"]
                and record.get("status") != "PASS_BUFFER_REMATERIALIZED_AFTER_HEAD_CHANGE"):
                raise RuntimeError("buffer_completed_generation_unreceipted_rebase")
        previous = (generation, path, record)
    return [record for _, _, record in candidates]


def _recover_completed_consumption(
    *,
    root: Path,
    operation_id: str,
    base_state: Path,
    work_root: Path,
    consumed_path: Path,
) -> dict[str, Any] | None:
    """Seal closeout after publication advanced truth before consumption wrote."""
    operation_hash = hashlib.sha256(operation_id.encode()).hexdigest()
    claim_path = root.resolve() / "claims" / f"{operation_hash}.json"
    workspace = work_root.resolve() / operation_hash
    _generation, materialization_path, materialization_record = _latest_materialization(workspace)
    if not claim_path.is_file() or materialization_record is None:
        return None
    claim = _read_sealed(claim_path, CLAIM_SCHEMA, "claim_sha256")
    if claim.get("operation_id") != operation_id:
        raise RuntimeError("buffer_claim_operation_mismatch")
    manifest_path = _verify_spec(claim["entry_manifest"], "buffer_claim_entry")
    entry = validate_entry(
        manifest_path,
        current_routes=set(),
        current_topic_ids=set(),
    )
    materialization = materialization_record
    if (
        materialization.get("operation_id") != operation_id
        or materialization.get("entry_id") != entry["entry_id"]
    ):
        raise RuntimeError("buffer_completed_materialization_mismatch")
    materialized = materialization.get("materialized")
    if (
        not isinstance(materialized, dict)
        or materialized.get("operation", "new_article") != "new_article"
    ):
        raise RuntimeError("buffer_completed_materialization_invalid")
    if not isinstance(materialized.get("apply_template"), dict):
        return None
    template_path = _verify_spec(
        materialized.get("apply_template"),
        "buffer_completed_apply_template",
    )
    template = json.loads(template_path.read_text(encoding="utf-8"))
    if (
        template.get("schema") != "glp-opus-article-apply-template-v1"
        or template.get("operation", "new_article") != "new_article"
        or template.get("topic_id") != entry["topic_id"]
        or template.get("article_id") != entry["article_id"]
        or template.get("target_route") != entry["target_route"]
        or template.get("base_state_sha256")
        != materialization["current_base_state"]["sha256"]
    ):
        raise RuntimeError("buffer_completed_apply_template_invalid")
    report = Path(str(template.get("report_dir") or "")).resolve()
    final_path = report / "10-final-publication-receipt.json"
    if not final_path.is_file():
        return None
    # A rebased admission is recoverable only through its complete immutable
    # generation chain. The final receipt, advanced pointer and ledger below
    # remain mandatory; this never replays a publish or edits the original claim.
    if materialization.get("current_base_state") != claim.get("current_base_state"):
        validate_completed_generation_lineage(workspace, claim)
    final = json.loads(final_path.read_text(encoding="utf-8"))
    if (
        final.get("schema") != "glp-opus-article-final-publication-v1"
        or final.get("status") != "PASS_LIVE_GSC_READBACK_PASS"
        or final.get("receipt_sha256")
        != _opus_payload_hash(final, "receipt_sha256")
        or final.get("operation", "new_article") != "new_article"
        or final.get("topic_id") != entry["topic_id"]
        or final.get("article_id") != entry["article_id"]
        or final.get("live_url")
        != "https://glpclinicscout.com" + entry["target_route"]
        or not isinstance(final.get("deployment_id"), str)
        or not final.get("deployment_id")
    ):
        raise RuntimeError("buffer_completed_final_receipt_invalid")
    pointer = json.loads(base_state.read_text(encoding="utf-8"))
    if (
        pointer.get("deployment_id") != final["deployment_id"]
        or pointer.get("release_root") != template.get("release_root")
        or pointer.get("public_tree_sha256") != final.get("public_tree_sha256")
    ):
        raise RuntimeError("buffer_completed_pointer_not_advanced")
    ledger_path = Path(str(template.get("published_ledger_path") or "")).resolve()
    if not ledger_path.is_file():
        raise RuntimeError("buffer_completed_published_ledger_missing")
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    matching = [
        row
        for row in (ledger.get("entries") or [])
        if row.get("topic_id") == entry["topic_id"]
        and row.get("target_route") == entry["target_route"]
        and row.get("deployment_id") == final["deployment_id"]
        and row.get("status") == "PASS_LIVE"
    ]
    if len(matching) != 1:
        raise RuntimeError("buffer_completed_published_ledger_invalid")
    publication = {
        "status": "PASS_BUFFER_PUBLISHED",
        "deployment_id": final["deployment_id"],
        "live_url": final["live_url"],
        "receipt": str(final_path),
        "receipt_sha256": _sha(final_path),
    }
    consumption = {
        "schema": CONSUME_SCHEMA,
        "status": "PASS_BUFFER_PUBLISHED",
        "operation_id": operation_id,
        "entry_id": entry["entry_id"],
        "topic_id": entry["topic_id"],
        "target_route": entry["target_route"],
        "claim": _spec(claim_path),
        "current_base_state": claim["current_base_state"],
        "rematerialization": materialized,
        "publication": publication,
        "recovered_after_terminal_publication": True,
    }
    _write_once(consumed_path, consumption, "consumption_sha256")
    return _read_sealed(consumed_path, CONSUME_SCHEMA, "consumption_sha256")


def _subject_rows(packet: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    rows = manifest.get("subject_files")
    if not isinstance(rows, list) or not rows:
        raise BufferAdmissionError("buffer_subject_manifest_rows_invalid")
    found: set[str] = set()
    verified: list[dict[str, Any]] = []
    for row in rows:
        relative = str((row or {}).get("path") or "")
        rel = Path(relative)
        if not relative or rel.is_absolute() or ".." in rel.parts or relative in found:
            raise BufferAdmissionError("buffer_subject_path_invalid")
        path = (packet / rel).resolve()
        if packet.resolve() not in path.parents or not path.is_file() or path.is_symlink():
            raise BufferAdmissionError(f"buffer_subject_missing:{relative}")
        digest = _sha(path)
        if row.get("sha256") != digest or row.get("bytes") != path.stat().st_size:
            raise BufferAdmissionError(f"buffer_subject_pin_invalid:{relative}")
        found.add(relative)
        verified.append({"path": relative, "sha256": digest, "bytes": path.stat().st_size})
    if found != REQUIRED_SUBJECTS:
        raise BufferAdmissionError("buffer_subject_manifest_not_exact")
    return sorted(verified, key=lambda row: row["path"])


def _validate_runtime(raw: dict[str, Any]) -> dict[str, Any]:
    """Prove one AUTHORIZED review transport produced this raw receipt, clean.

    Dual-authorized since the 2026-08-18 operator ruling ("we can have
    independent opus reviewers for things like the seo article reviews"): the
    Hermes/OpenAI-Codex Sol path and the independent Claude Opus path are both
    admissible, and both must clear one identical mechanical bar — empty tool
    surface, exactly one model turn, no fallback, isolated context, no
    unauthorized model in the usage table. The bar lives in ``review_transports``
    so what the buffer ACCEPTS can never drift from what the machine EMITS.

    This admits a second transport; it relaxes nothing about the gates, the
    rubric, the PASS bar, or any packet contract.
    """
    return review_transports.validate_raw_review_runtime(
        raw, error=BufferAdmissionError, prefix="buffer_opus"
    )


def validate_admission_evidence(
    packet: Path,
    pass_receipt: Path,
    control_files: dict[str, str],
) -> dict[str, Any]:
    packet = packet.resolve(strict=True)
    if not packet.is_dir() or packet.is_symlink():
        raise BufferAdmissionError("buffer_packet_invalid")
    qa_path = packet / "07-qa-receipt.json"
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    if qa.get("status") != "PASS":
        raise BufferAdmissionError("buffer_deterministic_qa_not_pass")
    receipt_path = pass_receipt.resolve(strict=True)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    gates = receipt.get("gates")
    effects = receipt.get("effects")
    receipt_transport = review_transports.transport(receipt.get("review_transport"))
    if (
        receipt.get("schema") != "glp-opus-seo-round-receipt-v1"
        or receipt.get("status") != "PASS_OPUS_SEO_DRAFT"
        or receipt.get("provider") != receipt_transport["provider"]
        or receipt.get("model") != receipt_transport["model"]
        or receipt.get("effort") != receipt_transport["effort"]
        or receipt.get("tools_disabled") is not True
        or not isinstance(gates, list)
        or [row.get("gate") for row in gates] != list("ABCDE")
        or any(row.get("status") != "PASS" or row.get("findings") for row in gates)
        or receipt.get("findings") not in (None, [])
        or not isinstance(effects, dict)
        or set(effects) != {"publishes", "deployments", "cache_purges", "gsc_submissions"}
        or any(type(value) is not int or value != 0 for value in effects.values())
    ):
        raise BufferAdmissionError("buffer_opus_pass_invalid")
    if receipt.get("receipt_sha256") != _opus_payload_hash(receipt, "receipt_sha256"):
        raise BufferAdmissionError("buffer_opus_receipt_self_hash_invalid")
    manifest_path = Path(str(receipt.get("subject_manifest_path") or "")).resolve()
    if not manifest_path.is_file() or _sha(manifest_path) != receipt.get("subject_manifest_sha256"):
        raise BufferAdmissionError("buffer_subject_manifest_pin_invalid")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("schema") != "glp-opus-seo-subject-manifest-v1"
        or manifest.get("manifest_sha256") != _opus_payload_hash(manifest, "manifest_sha256")
    ):
        raise BufferAdmissionError("buffer_subject_manifest_invalid")
    subjects = _subject_rows(packet, manifest)
    raw_path = Path(str(receipt.get("raw_review_path") or "")).resolve()
    if not raw_path.is_file() or _sha(raw_path) != receipt.get("raw_review_sha256"):
        raise BufferAdmissionError("buffer_opus_raw_pin_invalid")
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    raw_transport = _validate_runtime(raw)
    if raw_transport["transport_id"] != receipt_transport["transport_id"]:
        raise BufferAdmissionError(
            f"buffer_review_transport_mismatch:{receipt_transport['transport_id']}:"
            f"{raw_transport['transport_id']}")
    if not isinstance(control_files, dict) or not control_files:
        raise BufferAdmissionError("buffer_control_pins_missing")
    verified_controls: dict[str, str] = {}
    for raw_control, expected in sorted(control_files.items()):
        path = Path(raw_control).resolve()
        if not path.is_file() or path.is_symlink() or len(str(expected)) != 64 or _sha(path) != expected:
            raise BufferAdmissionError(f"buffer_control_pin_invalid:{path}")
        verified_controls[str(path)] = str(expected)
    authority = json.loads((packet / "00-authority.json").read_text(encoding="utf-8"))
    selection_receipt_spec = authority.get("selection_receipt") or {}
    selection_receipt_path = _verify_spec(selection_receipt_spec, "buffer_selection_receipt")
    selection_receipt, selected = load_selection_receipt(selection_receipt_path)
    topic_authority_spec = ((selection_receipt.get("inputs") or {}).get("topic_queue") or {})
    topic_authority_path = _resolve_topic_authority(topic_authority_spec, packet)
    topic_provenance = validate_topic_provenance(
        selection_receipt,
        selected,
        topic_authority_path,
        error=BufferAdmissionError,
    )
    article = json.loads((packet / "06-article.json").read_text(encoding="utf-8"))
    receipt_topic = str(receipt.get("topic_id") or "")
    manifest_topic = str(manifest.get("topic_id") or "")
    if (
        not receipt_topic
        or receipt_topic != manifest_topic
        or receipt.get("job_id") != manifest.get("job_id")
        or receipt.get("round") != manifest.get("round")
    ):
        raise BufferAdmissionError("buffer_opus_subject_identity_mismatch")
    topic_id = receipt_topic
    article_id = str(article.get("article_id") or "")
    route = str(article.get("canonical_path") or "")
    if not topic_id or not article_id or ROUTE_PATTERN.fullmatch(route) is None:
        raise BufferAdmissionError("buffer_subject_identity_invalid")
    return {
        "topic_id": topic_id,
        "article_id": article_id,
        "target_route": route,
        "subjects": subjects,
        "qa": _spec(qa_path),
        "pass_receipt": _spec(receipt_path),
        "subject_manifest": _spec(manifest_path),
        "raw_review": _spec(raw_path),
        "selection_receipt": _spec(selection_receipt_path),
        "topic_authority": _spec(topic_authority_path),
        "topic_provenance": topic_provenance,
        "review_transport": raw_transport["transport_id"],
        "control_files": verified_controls,
    }


def missing_inbound_source_routes(
    packet: Path,
    mutable_article_routes: set[str],
    *,
    expected_article_id: str | None = None,
) -> list[str]:
    """Return planned inbound sources absent from the mutable article catalogs.

    A sitemap/live-tree route is not enough: the release builder can only add an
    inbound link by editing one of the configured article catalogs. Admission
    therefore binds the packet to that narrower current source set instead of
    trusting a QA receipt minted against older release bytes.
    """
    article_path = packet.resolve() / "06-article.json"
    try:
        article = json.loads(article_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise BufferAdmissionError("buffer_article_packet_invalid") from error
    if not isinstance(article, dict):
        raise BufferAdmissionError("buffer_article_packet_invalid")
    article_id = str(article.get("article_id") or "")
    if expected_article_id is not None and article_id != expected_article_id:
        raise BufferAdmissionError("buffer_packet_article_identity_mismatch")
    plans = article.get("inbound_link_plan") or []
    if not isinstance(plans, list):
        raise BufferAdmissionError("buffer_inbound_link_plan_invalid")
    sources: set[str] = set()
    for plan in plans:
        if not isinstance(plan, dict):
            raise BufferAdmissionError("buffer_inbound_link_plan_invalid")
        source = str(plan.get("source_route") or "")
        if not source or ROUTE_PATTERN.fullmatch(source) is None:
            raise BufferAdmissionError("buffer_inbound_link_plan_invalid")
        sources.add(source)
    return sorted(sources - mutable_article_routes)


def _entry_manifests(root: Path) -> Iterable[Path]:
    entries = root.resolve() / "entries"
    return sorted(entries.glob("*/entry.json")) if entries.is_dir() else []


def _claimed_entry_ids(root: Path) -> set[str]:
    claims = root.resolve() / "claims"
    result: set[str] = set()
    for path in sorted(claims.glob("*.json")) if claims.is_dir() else []:
        try:
            result.add(str(_read_sealed(path, CLAIM_SCHEMA, "claim_sha256")["entry_id"]))
        except Exception:
            raise RuntimeError(f"buffer_claim_invalid:{path}") from None
    return result


def _hold_path(root: Path, entry_id: str) -> Path:
    return root.resolve() / "held" / f"{entry_id}.json"


def _held_entry_ids(root: Path) -> set[str]:
    held_root = root.resolve() / "held"
    result: set[str] = set()
    for path in sorted(held_root.glob("*.json")) if held_root.is_dir() else []:
        try:
            receipt = _read_sealed(path, HOLD_SCHEMA, "hold_sha256")
            if path.stem != receipt.get("entry_id") or ENTRY_ID_PATTERN.fullmatch(path.stem) is None:
                raise RuntimeError("buffer_hold_identity_invalid")
            manifest = root.resolve() / "entries" / path.stem / "entry.json"
            pinned = receipt.get("entry_manifest")
            if (receipt.get("status") != "HELD_READY_ENTRY"
                or receipt.get("publication_effects") != 0
                or receipt.get("disposition") != "excluded_from_ready_selection_no_successor_admission"
                or not isinstance(pinned, dict)
                or pinned.get("path") != str(manifest)
                or not manifest.is_file() or manifest.is_symlink()
                or pinned.get("sha256") != _sha(manifest)):
                raise RuntimeError("buffer_hold_manifest_binding_invalid")
            entry = _read_sealed(manifest, ENTRY_SCHEMA, "entry_sha256")
            if (entry.get("entry_id") != path.stem
                or any(receipt.get(key) != entry.get(key) for key in
                       ("topic_id", "article_id", "target_route", "packet_closure_sha256"))
                or not isinstance(receipt.get("reason"), str)
                or not receipt["reason"]
                or not isinstance(receipt.get("evidence"), dict)
                or set(receipt["evidence"]) != {"path", "sha256"}):
                raise RuntimeError("buffer_hold_entry_binding_invalid")
            result.add(path.stem)
        except Exception:
            raise RuntimeError(f"buffer_hold_invalid:{path}") from None
    return result


def hold_entry(
    *,
    root: Path,
    entry_id: str,
    entry_sha256: str,
    reason: str,
    evidence: dict[str, str],
    current_routes: set[str],
    current_topic_ids: set[str],
    mutable_article_routes: set[str] | None = None,
    dry_run: bool = False,
    allow_stale_claim: bool = False,
) -> dict[str, Any]:
    """Register a reversible exact-entry exclusion; never rewrite the packet.

    A later release/unhold requires a separately reviewed operation. Neither
    stale-claim recycling nor refill has authority to remove this receipt.
    """
    if ENTRY_ID_PATTERN.fullmatch(entry_id) is None:
        raise RuntimeError("buffer_hold_entry_id_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", entry_sha256):
        raise RuntimeError("buffer_hold_entry_preimage_invalid")
    if not isinstance(reason, str) or not reason.strip() or reason != reason.strip():
        raise RuntimeError("buffer_hold_reason_required")
    if not isinstance(evidence, dict) or set(evidence) != {"path", "sha256"}:
        raise RuntimeError("buffer_hold_evidence_invalid")
    with carrier_lock():
        manifest = root.resolve() / "entries" / entry_id / "entry.json"
        if not manifest.is_file() or manifest.is_symlink():
            raise RuntimeError("buffer_hold_entry_unknown")
        if _sha(manifest) != entry_sha256:
            raise RuntimeError("buffer_hold_entry_preimage_drift")
        entry = validate_entry(
            manifest, current_routes=current_routes, current_topic_ids=current_topic_ids
        )
        if entry.get("entry_id") != entry_id:
            raise RuntimeError("buffer_hold_entry_identity_invalid")
        if mutable_article_routes is not None and missing_inbound_source_routes(
            manifest.parent / "packet", mutable_article_routes,
            expected_article_id=entry["article_id"],
        ):
            raise RuntimeError("buffer_hold_entry_not_ready")
        evidence_path = _verify_spec(evidence, "buffer_hold_evidence")
        if str(evidence_path) != evidence["path"]:
            raise RuntimeError("buffer_hold_evidence_path_alias")
        claimed = entry_id in _claimed_entry_ids(root)
        stale: list[dict[str, Any]] = []
        if claimed:
            stale = [row for row in stale_claims(root) if row["entry_id"] == entry_id]
            all_claims = [claim for _, claim in _claim_rows(root) if claim.get("entry_id") == entry_id]
            if not allow_stale_claim or len(stale) != 1 or len(all_claims) != 1:
                raise RuntimeError("buffer_hold_entry_claimed")
        consumed_root = root.resolve() / "consumed"
        for path in sorted(consumed_root.glob("*.json")) if consumed_root.is_dir() else []:
            consumed = _read_sealed(path, CONSUME_SCHEMA, "consumption_sha256")
            if consumed.get("entry_id") == entry_id:
                raise RuntimeError("buffer_hold_entry_consumed")
        if entry_id in _quarantined_entry_ids(root):
            raise RuntimeError("buffer_hold_entry_quarantined")
        receipt_path = _hold_path(root, entry_id)
        held = _held_entry_ids(root)
        if entry_id in held:
            receipt = _read_sealed(receipt_path, HOLD_SCHEMA, "hold_sha256")
            if (receipt.get("entry_id") != entry_id
                or receipt.get("entry_manifest") != _spec(manifest)
                or receipt.get("reason") != reason
                or receipt.get("evidence") != evidence):
                raise RuntimeError("buffer_hold_conflict")
            return {"status": "IDEMPOTENT_BUFFER_ENTRY_HELD", "hold": _spec(receipt_path)}
        receipt = {
            "schema": HOLD_SCHEMA,
            "status": "HELD_READY_ENTRY",
            "held_at_utc": _utc_now(),
            "entry_id": entry_id,
            "topic_id": entry["topic_id"],
            "article_id": entry["article_id"],
            "target_route": entry["target_route"],
            "packet_closure_sha256": entry["packet_closure_sha256"],
            "entry_manifest": _spec(manifest),
            "reason": reason,
            "evidence": evidence,
            "publication_effects": 0,
            "disposition": "excluded_from_ready_selection_no_successor_admission",
        }
        if claimed:
            receipt["stale_claim"] = _spec(Path(stale[0]["claim"]))
        if dry_run:
            return {"status": "DRY_RUN_BUFFER_ENTRY_HOLD", "would_hold": receipt}
        _write_once(receipt_path, receipt, "hold_sha256")
        return {"status": "PASS_BUFFER_ENTRY_HELD", "hold": _spec(receipt_path)}


def _quarantine_path(root: Path, entry_id: str) -> Path:
    return root.resolve() / "quarantined" / f"{entry_id}.json"


def _quarantined_entry_ids(root: Path) -> set[str]:
    quarantine_root = root.resolve() / "quarantined"
    result: set[str] = set()
    for path in sorted(quarantine_root.glob("*.json")) if quarantine_root.is_dir() else []:
        try:
            receipt = _read_sealed(path, QUARANTINE_SCHEMA, "quarantine_sha256")
            entry_id = str(receipt.get("entry_id") or "")
            if not entry_id or path.stem != entry_id:
                raise RuntimeError("buffer_quarantine_identity_invalid")
            result.add(entry_id)
        except Exception:
            raise RuntimeError(f"buffer_quarantine_invalid:{path}") from None
    return result


def _quarantine_summary(path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    return {
        "entry_id": receipt["entry_id"],
        "topic_id": receipt["topic_id"],
        "article_id": receipt["article_id"],
        "target_route": receipt["target_route"],
        "reason": receipt["reason"],
        "receipt": _spec(path),
    }


def _on_page_source_repair_predecessor(*, root, packet, evidence, duplicate_manifests, successor_closure):
    """Only a fresh reviewed on-page successor to an unpublished link-source quarantine."""
    if len(duplicate_manifests) != 1:
        raise BufferAdmissionError("buffer_candidate_duplicate")
    manifest_path, old = duplicate_manifests[0]
    if any(old.get(k) != evidence[k] for k in ('topic_id', 'target_route', 'article_id')):
        raise BufferAdmissionError("buffer_candidate_duplicate")
    entry_id = old['entry_id']
    quarantine_path = _quarantine_path(root, entry_id)
    if not quarantine_path.is_file():
        raise BufferAdmissionError("buffer_candidate_duplicate")
    q = _read_sealed(quarantine_path, QUARANTINE_SCHEMA, 'quarantine_sha256')
    if (not str(q.get('reason', '')).startswith('inbound_link_source_missing:/guides/')
            or any(q.get(k) != old.get(k) for k in ('entry_id', 'topic_id', 'target_route', 'article_id'))
            or q.get('publication_effects') != 0 or old.get('publication_effects') != 0
            or entry_id in _claimed_entry_ids(root) or entry_id in _held_entry_ids(root)
            or (root / 'consumed' / f'{entry_id}.json').exists()):
        raise BufferAdmissionError("buffer_candidate_duplicate")
    before = json.loads((manifest_path.parent / 'packet/06-article.json').read_text())
    after = json.loads((packet / '06-article.json').read_text())
    old_capture = before.get('capture_surface', {})
    levels = {'LOW': 1, 'MEDIUM': 2}
    if (old_capture.get('status') != 'WORKING_BASE_TEMPLATE_CONTRACT'
            or old_capture.get('action') != '/api/content-upgrade' or old_capture.get('email_field') != 'email'
            or after.get('capture_surface') != {'status': 'ON_PAGE_ONLY', 'email_field': None,
                                               'health_data_fields': [], 'follow_up_consent': False}
            or not evidence['target_route'].startswith('/guides/')
            or before.get('risk_tier') not in levels or after.get('risk_tier') not in levels
            or levels[after['risk_tier']] > levels[before['risk_tier']]
            or old.get('packet_closure_sha256') == successor_closure):
        raise BufferAdmissionError("buffer_candidate_duplicate")
    return {'entry_manifest': _spec(manifest_path), 'quarantine': _spec(quarantine_path),
            'reason': q['reason'], 'successor_gate': 'fresh_deterministic_qa_and_independent_five_gate_review'}


def _medical_reframe_predecessor(
    *,
    root: Path,
    packet: Path,
    evidence: dict[str, Any],
    duplicate_manifests: list[tuple[Path, dict[str, Any]]],
    successor_closure: str,
) -> dict[str, Any]:
    """Authorize a reviewed successor only for the exact medical quarantine."""
    if len(duplicate_manifests) != 1:
        raise BufferAdmissionError("buffer_candidate_duplicate")
    manifest_path, predecessor = duplicate_manifests[0]
    if (
        predecessor.get("topic_id") != evidence["topic_id"]
        or predecessor.get("target_route") != evidence["target_route"]
        or predecessor.get("article_id") != evidence["article_id"]
    ):
        raise BufferAdmissionError("buffer_candidate_duplicate")
    quarantine_path = _quarantine_path(root, str(predecessor.get("entry_id") or ""))
    if not quarantine_path.is_file():
        raise BufferAdmissionError("buffer_candidate_duplicate")
    quarantine = _read_sealed(quarantine_path, QUARANTINE_SCHEMA, "quarantine_sha256")
    if (
        quarantine.get("reason") != MEDICAL_REFRAME_REASON
        or quarantine.get("entry_id") != predecessor.get("entry_id")
        or quarantine.get("topic_id") != evidence["topic_id"]
        or quarantine.get("target_route") != evidence["target_route"]
        or quarantine.get("article_id") != evidence["article_id"]
    ):
        raise BufferAdmissionError("buffer_candidate_duplicate")

    old_article = json.loads(
        (manifest_path.parent / "packet/06-article.json").read_text(encoding="utf-8")
    )
    new_article = json.loads((packet.resolve() / "06-article.json").read_text(encoding="utf-8"))
    old_draft = (manifest_path.parent / "packet/04-draft.md").read_text(encoding="utf-8")
    new_draft = (packet.resolve() / "04-draft.md").read_text(encoding="utf-8")
    if (
        str(old_article.get("risk_tier") or "").upper() != "HIGH"
        or 'riskTier: "HIGH"' not in old_draft
        or str(new_article.get("risk_tier") or "").upper() not in {"LOW", "MEDIUM"}
        or not any(f'riskTier: "{tier}"' in new_draft for tier in ("LOW", "MEDIUM"))
        or predecessor.get("packet_closure_sha256") == successor_closure
    ):
        raise BufferAdmissionError("buffer_medical_reframe_successor_invalid")
    return {
        "entry_manifest": _spec(manifest_path),
        "quarantine": _spec(quarantine_path),
        "reason": MEDICAL_REFRAME_REASON,
        "successor_gate": "fresh_deterministic_qa_and_independent_five_gate_review",
    }


def _finalize_declared_quarantine(
    *,
    root: Path,
    work_root: Path,
    operation_id: str,
) -> dict[str, Any] | None:
    """Finish the local claim/workspace retirement declared by a sealed receipt.

    The receipt is written first. If the process dies between that declaration,
    the workspace rename, and claim unlink, the next consume finishes these
    local-only steps before it claims another candidate with the same operation
    id. Publication truth is never changed here.
    """
    operation_hash = hashlib.sha256(operation_id.encode()).hexdigest()
    claim_path = root.resolve() / "claims" / f"{operation_hash}.json"
    if not claim_path.is_file():
        return None
    claim = _read_sealed(claim_path, CLAIM_SCHEMA, "claim_sha256")
    if claim.get("operation_id") != operation_id:
        raise RuntimeError("buffer_quarantine_claim_operation_mismatch")
    receipt_path = _quarantine_path(root, str(claim.get("entry_id") or ""))
    if not receipt_path.is_file():
        return None
    receipt = _read_sealed(receipt_path, QUARANTINE_SCHEMA, "quarantine_sha256")
    if (
        receipt.get("operation_id") != operation_id
        or receipt.get("entry_id") != claim.get("entry_id")
        or receipt.get("claim_sha256") != claim.get("claim_sha256")
    ):
        raise RuntimeError("buffer_quarantine_claim_binding_invalid")
    workspace = work_root.resolve() / operation_hash
    archive = work_root.resolve() / "quarantined" / f"{operation_hash}-{claim['entry_id']}"
    if workspace.exists():
        if archive.exists():
            raise RuntimeError("buffer_quarantine_workspace_archive_conflict")
        archive.parent.mkdir(parents=True, exist_ok=True)
        os.rename(workspace, archive)
    os.chmod(claim_path, 0o600)
    claim_path.unlink()
    return _quarantine_summary(receipt_path, receipt)


def _declare_permanent_quarantine(
    *,
    root: Path,
    work_root: Path,
    operation_id: str,
    claim_path: Path,
    claim: dict[str, Any],
    entry: dict[str, Any],
    materialization_path: Path,
    error: PermanentArticleRejection,
) -> dict[str, Any]:
    if error.article_id and error.article_id != entry.get("article_id"):
        raise RuntimeError("buffer_quarantine_article_identity_mismatch")
    operation_hash = hashlib.sha256(operation_id.encode()).hexdigest()
    archive = work_root.resolve() / "quarantined" / f"{operation_hash}-{entry['entry_id']}"
    receipt = {
        "schema": QUARANTINE_SCHEMA,
        "status": "QUARANTINED_PERMANENT_ARTICLE_GATE_REJECTION",
        "quarantined_at_utc": _utc_now(),
        "reason": error.reason,
        "diagnostic": error.diagnostic[:4000],
        "operation_id": operation_id,
        "entry_id": entry["entry_id"],
        "topic_id": entry["topic_id"],
        "article_id": entry["article_id"],
        "target_route": entry["target_route"],
        "claim_sha256": claim["claim_sha256"],
        "claim": _spec(claim_path),
        "entry_manifest": claim["entry_manifest"],
        "materialization": _spec(materialization_path),
        "workspace_archive": str(archive),
        "publication_effects": 0,
        "disposition": "excluded_from_ready_pool_claim_recycled_continue_next",
    }
    receipt_path = _quarantine_path(root, entry["entry_id"])
    _write_once(receipt_path, receipt, "quarantine_sha256")
    finalized = _finalize_declared_quarantine(
        root=root,
        work_root=work_root,
        operation_id=operation_id,
    )
    if finalized is None:
        raise RuntimeError("buffer_quarantine_finalization_missing")
    return finalized


def validate_entry(
    manifest_path: Path,
    *,
    current_routes: set[str],
    current_topic_ids: set[str],
) -> dict[str, Any]:
    entry = _read_sealed(manifest_path.resolve(), ENTRY_SCHEMA, "entry_sha256")
    if entry.get("status") != "READY_UNPUBLISHED" or not isinstance(entry.get("packet_files"), list):
        raise RuntimeError("buffer_entry_state_invalid")
    if entry["target_route"] in current_routes or entry["topic_id"] in current_topic_ids:
        raise RuntimeError("buffer_entry_now_published")
    packet = manifest_path.parent / "packet"
    for row in entry["packet_files"]:
        path = packet / row["path"]
        if not path.is_file() or path.is_symlink() or _sha(path) != row["sha256"] or path.stat().st_size != row["bytes"]:
            raise RuntimeError(f"buffer_packet_pin_invalid:{row['path']}")
    for label in ("qa", "pass_receipt", "subject_manifest", "raw_review", "selection_receipt", "topic_authority"):
        _verify_spec(entry[label], f"buffer_{label}")
    selection_receipt, selected = load_selection_receipt(Path(entry["selection_receipt"]["path"]))
    if ((selection_receipt.get("inputs") or {}).get("topic_queue") or {}).get("sha256") != entry["topic_authority"]["sha256"]:
        raise RuntimeError("buffer_topic_authority_selection_binding_invalid")
    topic_provenance = validate_topic_provenance(
        selection_receipt,
        selected,
        Path(entry["topic_authority"]["path"]),
    )
    if entry.get("topic_provenance") != topic_provenance:
        raise RuntimeError("buffer_topic_provenance_entry_binding_invalid")
    # ``control_files`` is immutable admission provenance, not a live-code
    # pin. Requiring the current tree to retain every admission-time hash
    # silently drains the standing buffer whenever the guarded carrier is
    # upgraded. The consume path rematerializes and revalidates the sealed
    # packet through the current standard chain before any effect. Preserve
    # the historical pins here, while validating their exact manifest shape.
    controls = entry.get("control_files")
    if not isinstance(controls, dict) or not controls:
        raise RuntimeError("buffer_control_provenance_invalid")
    for raw, expected in controls.items():
        path = Path(str(raw))
        if not path.is_absolute() or len(str(expected)) != 64:
            raise RuntimeError("buffer_control_provenance_invalid")
    successor = entry.get("successor_of")
    if successor is not None:
        if not isinstance(successor, dict) or set(successor) != {"entry_id", "entry_manifest", "hold"}:
            raise RuntimeError("buffer_successor_lineage_invalid")
        root = manifest_path.resolve().parents[2]
        if successor["entry_id"] not in _held_entry_ids(root):
            raise RuntimeError("buffer_successor_predecessor_hold_drift")
        for label in ("entry_manifest", "hold"):
            _verify_spec(successor[label], f"buffer_successor_{label}")
        registered = entry.get("registered_review")
        if not isinstance(registered, dict) or set(registered) != {"receipt", "closeout", "execution"}:
            raise RuntimeError("buffer_successor_review_binding_invalid")
        for label, spec in registered.items():
            _verify_spec(spec, f"buffer_successor_{label}")
    return entry


REGISTERED_REVIEW_ROOT = Path("/home/ubuntu/business/reports/ops")


def _registered_successor_review(receipt_path: Path, evidence: dict[str, Any]) -> dict[str, Any]:
    """Require the controller's registered run and its fully bound review chain.

    Self-hashes alone are not registration: the run's job and history must be
    located in the registered reports tree and agree on this exact packet.
    """
    receipt_path = receipt_path.resolve(strict=True)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    job_id, round_number = receipt.get("job_id"), receipt.get("round")
    if (not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", job_id)
        or type(round_number) is not int or round_number != 1):
        raise BufferAdmissionError("buffer_successor_review_identity_invalid")
    history = receipt_path.parent
    run = history.parent
    root = REGISTERED_REVIEW_ROOT.resolve()
    if (history.name != f"opus-history-{job_id}" or run.name != job_id
        or run.parent.name != "runs" or root not in run.parents
        or receipt_path != history / "round-01-receipt.json"):
        raise BufferAdmissionError("buffer_successor_review_unregistered_location")
    job_path = run / f"review-job-{job_id}.json"
    job = json.loads(job_path.read_text(encoding="utf-8"))
    packet = Path(evidence["qa"]["path"]).parent.resolve()
    subject_names = [row["path"] for row in evidence["subjects"]]
    if (job.get("schema") != "glp-opus-seo-review-job-v1"
        or job.get("job_id") != job_id
        or Path(str(job.get("packet_dir") or "")).resolve() != packet
        or packet != run / "packet"
        or Path(str(job.get("history_dir") or "")).resolve() != history
        or job.get("subject_files") != subject_names
        or job.get("topic_id") != evidence["topic_id"]
        or job.get("target_route") != evidence["target_route"]
        or job.get("review_transport") != evidence["review_transport"]):
        raise BufferAdmissionError("buffer_successor_review_job_mismatch")
    closeout_path = history / "loop-closeout.json"
    execution_path = history / "round-01-review-execution.json"
    manifest_path = history / "round-01-subject-manifest.json"
    raw_path = history / "round-01-sol-raw.json"
    closeout = json.loads(closeout_path.read_text(encoding="utf-8"))
    execution = json.loads(execution_path.read_text(encoding="utf-8"))
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    transport = review_transports.transport(evidence["review_transport"])
    argv = execution.get("argv")
    if (not isinstance(argv, list) or execution.get("exit_code") != 0
        or execution.get("job_id") != job_id or execution.get("round") != 1
        or execution.get("subject_manifest_sha256") != _sha(manifest_path)
        or execution.get("raw_review_sha256") != _sha(raw_path)
        or len(argv) < 3 or Path(argv[1]).resolve() != Path(transport["wrapper"]).resolve()
        or not all(flag in argv for flag in ("--disable-tools", "--receipt-file", "--model", "--effort", "--provider"))
        or any(argv[argv.index(flag) + 1] != value for flag, value in (
            ("--receipt-file", str(raw_path)), ("--model", transport["model"]),
            ("--effort", transport["effort"]), ("--provider", transport["provider"])))
        or set(execution.get("streams") or {}) != {"stdout", "stderr"}
        or not isinstance(raw.get("session_id"), str) or not raw["session_id"].strip()
        or raw.get("review_input", {}).get("model") != transport["model"]
        or raw.get("review_input", {}).get("effort") != transport["effort"]):
        raise BufferAdmissionError("buffer_successor_review_execution_invalid")
    for stream in execution["streams"].values():
        path = _verify_spec(stream, "buffer_successor_execution_stream")
        if path.parent != history or path.stat().st_size != stream.get("bytes"):
            raise BufferAdmissionError("buffer_successor_execution_stream_invalid")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = {row["path"]: (row["sha256"], row["bytes"]) for row in evidence["subjects"]}
    if (Path(evidence["subject_manifest"]["path"]) != manifest_path
        or Path(evidence["raw_review"]["path"]) != raw_path
        or {row["path"]: (row["sha256"], row["bytes"]) for row in manifest["subject_files"]} != expected
        or len(manifest["subject_files"]) != len(expected)):
        raise BufferAdmissionError("buffer_successor_subject_tuple_drift")
    if (closeout.get("schema") != "glp-opus-seo-loop-closeout-v1"
        or closeout.get("closeout_sha256") != _opus_payload_hash(closeout, "closeout_sha256")
        or closeout.get("status") != "PASS_OPUS_SEO_DRAFT"
        or closeout.get("publication_invoked") is not False
        or closeout.get("topic_id") != evidence["topic_id"]
        or closeout.get("job_id") != job_id
        or closeout.get("rounds") != [{"round": 1, "receipt": str(receipt_path),
                                      "sha256": _sha(receipt_path), "verdict": "PASS_OPUS_SEO_DRAFT"}]
        or closeout.get("review_execution_sha256") != _sha(execution_path)
        or closeout.get("raw_review_sha256") != _sha(raw_path)
        or closeout.get("subject_manifest_sha256") != _sha(manifest_path)
        or closeout.get("packet_subject_sha256") != {row["path"]: row["sha256"] for row in evidence["subjects"]}):
        raise BufferAdmissionError("buffer_successor_registered_review_invalid")
    return {"receipt": _spec(receipt_path), "closeout": _spec(closeout_path),
            "execution": _spec(execution_path)}


def _inactive_inbound_quarantine(root: Path, path: Path, entry: dict[str, Any]) -> bool:
    """Ignore only authenticated zero-effect historical link-source rejection.

    No record is edited; uncertain, active, published or unrelated history stays
    a duplicate blocker. The actual requested predecessor is never filtered.
    """
    try:
        q = _read_sealed(_quarantine_path(root, entry['entry_id']), QUARANTINE_SCHEMA, 'quarantine_sha256')
        if (q.get('entry_id') != entry['entry_id']
                or q.get('status') != 'QUARANTINED_PERMANENT_ARTICLE_GATE_REJECTION'
                or not str(q.get('reason', '')).startswith('inbound_link_source_missing:/guides/')
                or type(q.get('publication_effects')) is not int or q['publication_effects'] != 0
                or _verify_spec(q['entry_manifest'], 'inactive_quarantine_entry') != path.resolve()):
            return False
        try:
            _verify_spec(q['materialization'], 'inactive_quarantine_materialization')
        except RuntimeError:
            # The producer retains the exact pre-refusal bytes when archiving
            # the workspace; a reused live path is not evidence authority.
            archive = Path(str(q.get('workspace_archive') or ''))
            if not archive.is_absolute():
                return False
            spec = q['materialization']
            _verify_spec({'path': str(archive / Path(spec['path']).name),
                          'sha256': spec['sha256']}, 'inactive_quarantine_archive')
        for directory in ('claims', 'consumed', 'published', 'publications'):
            for record in (root.resolve() / directory).glob('*.json'):
                if json.loads(record.read_text()).get('entry_id') == entry['entry_id']:
                    return False
        return True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        return False


def _held_successor_predecessor(root: Path, predecessor_id: str,
                                duplicates: list[tuple[Path, dict[str, Any]]],
                                evidence: dict[str, Any], closure: str) -> dict[str, Any]:
    duplicates = [(p, e) for p, e in duplicates if e.get('entry_id') == predecessor_id
                  or not _inactive_inbound_quarantine(root, p, e)]
    if ENTRY_ID_PATTERN.fullmatch(predecessor_id) is None or len(duplicates) not in (1, 2):
        raise BufferAdmissionError("buffer_successor_predecessor_not_exact")
    path, entry = duplicates[0] if len(duplicates) == 1 else next(
        ((p, e) for p, e in duplicates if e.get("entry_id") == predecessor_id), (None, None))
    if (path is None or entry is None or len(duplicates) not in (1, 2)
        or any(e.get("entry_id") != predecessor_id and
               "successor_of" not in e for _, e in duplicates)):
        raise BufferAdmissionError("buffer_successor_predecessor_not_exact")
    if path.parent.name != predecessor_id or entry.get("entry_id") != predecessor_id:
        raise BufferAdmissionError("buffer_successor_predecessor_not_exact")
    if predecessor_id not in _held_entry_ids(root):
        raise BufferAdmissionError("buffer_successor_predecessor_not_held")
    # A HELD marker is not a proof of exclusive state. Even a stale claim or
    # an orphaned terminal record makes this predecessor ineligible.
    for directory in ("claims", "consumed", "quarantined", "published", "publications"):
        records = root.resolve() / directory
        for record in records.glob("*.json") if records.is_dir() else ():
            payload = json.loads(record.read_text(encoding="utf-8"))
            if payload.get("entry_id") == predecessor_id:
                raise BufferAdmissionError(f"buffer_successor_predecessor_{directory}_recorded")
    if predecessor_id in _claimed_entry_ids(root) or predecessor_id in _quarantined_entry_ids(root):
        raise BufferAdmissionError("buffer_successor_predecessor_not_held_only")
    validate_entry(path, current_routes=set(), current_topic_ids=set())
    if (any(entry.get(key) != evidence[key] for key in ("topic_id", "target_route"))
        or entry.get("packet_closure_sha256") == closure
        or entry.get("topic_provenance") != evidence["topic_provenance"]):
        raise BufferAdmissionError("buffer_successor_authority_or_content_drift")
    old_selection, old_selected = load_selection_receipt(Path(entry["selection_receipt"]["path"]))
    new_selection, new_selected = load_selection_receipt(Path(evidence["selection_receipt"]["path"]))
    fields = ("topic_id", "target_query", "target_route", "operator_directive_id",
              "queue_position", "queue_sha256", "selection_mode", "topic_provenance")
    if (any(old_selected.get(k) != new_selected.get(k) for k in fields)
        or old_selection.get("selection_mode") != new_selection.get("selection_mode")
        or entry.get("topic_authority", {}).get("sha256") != evidence["topic_authority"]["sha256"]):
        raise BufferAdmissionError("buffer_successor_selection_drift")
    return {"entry_id": predecessor_id, "entry_manifest": _spec(path),
            "hold": _spec(_hold_path(root, predecessor_id))}


def _successor_publication_date(today_pt: str, control_files: dict[str, str]) -> None:
    """PR #19 binds the planned PT day at release, not historical draft dates."""
    if today_pt != pt_date_now():
        raise BufferAdmissionError("buffer_successor_pt_day_drift")
    for name in ("build_opus_article_release.py", "publish_opus_article_release.py"):
        path = Path(__file__).resolve().with_name(name)
        if control_files.get(str(path)) != _sha(path):
            raise BufferAdmissionError("buffer_successor_publication_controls_unpinned")


def admit(
    *,
    root: Path,
    packet: Path,
    pass_receipt: Path,
    control_files: dict[str, str],
    current_routes: set[str],
    current_topic_ids: set[str],
    mutable_article_routes: set[str],
    admitted_at_utc: str,
    predecessor_entry_id: str | None = None,
    today_pt: str | None = None,
) -> Path:
    evidence = validate_admission_evidence(packet, pass_receipt, control_files)
    registered_review = None
    if predecessor_entry_id is not None:
        _successor_publication_date(today_pt or pt_date_now(), control_files)
        registered_review = _registered_successor_review(pass_receipt, evidence)
    missing_sources = missing_inbound_source_routes(
        packet,
        mutable_article_routes,
        expected_article_id=evidence["article_id"],
    )
    if missing_sources:
        raise BufferAdmissionError(f"inbound_link_source_missing:{missing_sources[0]}")
    with carrier_lock():
        if evidence["target_route"] in current_routes or evidence["topic_id"] in current_topic_ids:
            raise BufferAdmissionError("buffer_candidate_already_live")
        closure = hashlib.sha256(_canonical(evidence["subjects"])).hexdigest()
        duplicates: list[tuple[Path, dict[str, Any]]] = []
        for path in _entry_manifests(root):
            entry = _read_sealed(path, ENTRY_SCHEMA, "entry_sha256")
            if path.parent.name != entry.get("entry_id"):
                raise BufferAdmissionError("buffer_entry_manifest_identity_drift")
            if entry["target_route"] == evidence["target_route"] or entry["topic_id"] == evidence["topic_id"]:
                duplicates.append((path, entry))
        reframed_from = None
        on_page_successor_of = None
        predecessor = None
        if predecessor_entry_id is not None:
            predecessor = _held_successor_predecessor(
                root, predecessor_entry_id, duplicates, evidence, closure)
            _successor_publication_date(today_pt or pt_date_now(), control_files)
        elif duplicates:
            predecessor_quarantine = _quarantine_path(root, str(duplicates[0][1].get('entry_id', '')))
            reason = (_read_sealed(predecessor_quarantine, QUARANTINE_SCHEMA, 'quarantine_sha256').get('reason', '')
                      if predecessor_quarantine.is_file() else '')
            if str(reason).startswith('inbound_link_source_missing:/guides/'):
                on_page_successor_of = _on_page_source_repair_predecessor(
                    root=root, packet=packet, evidence=evidence,
                    duplicate_manifests=duplicates, successor_closure=closure,
                )
            else:
                reframed_from = _medical_reframe_predecessor(
                    root=root, packet=packet, evidence=evidence,
                    duplicate_manifests=duplicates, successor_closure=closure,
                )
        entry_id = hashlib.sha256(
            f"{evidence['topic_id']}\0{evidence['target_route']}\0{closure}".encode()
        ).hexdigest()[:24]
        entries_root = root.resolve() / "entries"
        final = entries_root / entry_id
        if predecessor is not None and any(
            e.get("entry_id") not in (predecessor_entry_id, entry_id) for _, e in duplicates
        ):
            raise BufferAdmissionError("buffer_successor_existing_conflict")
        if predecessor is not None and final.exists():
            existing = validate_entry(final / "entry.json", current_routes=current_routes,
                                      current_topic_ids=current_topic_ids)
            if (existing.get("successor_of") != predecessor
                or existing.get("registered_review") != registered_review
                or existing.get("packet_files") != evidence["subjects"]
                or existing.get("pass_receipt", {}).get("sha256") != evidence["pass_receipt"]["sha256"]):
                raise BufferAdmissionError("buffer_successor_idempotency_drift")
            return final / "entry.json"
        ready_count = len(ready_entries(
            root,
            current_routes=current_routes,
            current_topic_ids=current_topic_ids,
            mutable_article_routes=mutable_article_routes,
        ))
        if ready_count >= READY_MAX:
            raise BufferAdmissionError("buffer_ready_pool_at_max")

        entry_id = hashlib.sha256(
            f"{evidence['topic_id']}\0{evidence['target_route']}\0{closure}".encode()
        ).hexdigest()[:24]
        entries_root = root.resolve() / "entries"
        entries_root.mkdir(parents=True, exist_ok=True)
        final = entries_root / entry_id
        if final.exists():
            raise BufferAdmissionError("buffer_candidate_duplicate")
        temporary = entries_root / f".{entry_id}.tmp-{os.getpid()}"
        if temporary.exists():
            shutil.rmtree(temporary)
        (temporary / "packet").mkdir(parents=True)
        (temporary / "evidence").mkdir()
        source_packet = packet.resolve()
        packet_rows: list[dict[str, Any]] = []
        for row in evidence["subjects"]:
            source = source_packet / row["path"]
            target = temporary / "packet" / row["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            os.chmod(target, 0o444)
            packet_rows.append(dict(row))
        stored: dict[str, dict[str, str]] = {}
        for label in ("qa", "pass_receipt", "subject_manifest", "raw_review", "topic_authority"):
            source = Path(evidence[label]["path"])
            target = temporary / "evidence" / f"{label}{source.suffix}"
            shutil.copyfile(source, target)
            os.chmod(target, 0o444)
            stored[label] = {
                "path": str((final / "evidence" / target.name).resolve()),
                "sha256": _sha(target),
            }
        entry = {
            "schema": ENTRY_SCHEMA,
            "status": "READY_UNPUBLISHED",
            "entry_id": entry_id,
            "admitted_at_utc": admitted_at_utc,
            "topic_id": evidence["topic_id"],
            "article_id": evidence["article_id"],
            "target_route": evidence["target_route"],
            "packet_closure_sha256": closure,
            "packet_files": packet_rows,
            **stored,
            "selection_receipt": evidence["selection_receipt"],
            "topic_provenance": evidence["topic_provenance"],
            "review_transport": evidence["review_transport"],
            "control_files": evidence["control_files"],
            "publication_effects": 0,
        }
        if on_page_successor_of is not None:
            entry["on_page_source_repair_of"] = on_page_successor_of
        if reframed_from is not None:
            entry["reframed_from"] = reframed_from
        if predecessor is not None:
            entry["successor_of"] = predecessor
            entry["registered_review"] = registered_review
        _write_once(temporary / "entry.json", entry, "entry_sha256")
        for directory in (temporary / "packet", temporary / "evidence", temporary):
            os.chmod(directory, 0o555)
        os.rename(temporary, final)
        return final / "entry.json"


def ready_entries(
    root: Path,
    *,
    current_routes: set[str],
    current_topic_ids: set[str],
    mutable_article_routes: set[str] | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    claimed = _claimed_entry_ids(root)
    quarantined = _quarantined_entry_ids(root)
    held = _held_entry_ids(root)
    valid: list[tuple[Path, dict[str, Any]]] = []
    for path in _entry_manifests(root):
        try:
            entry = validate_entry(
                path,
                current_routes=current_routes,
                current_topic_ids=current_topic_ids,
            )
            if mutable_article_routes is not None:
                missing = missing_inbound_source_routes(
                    path.parent / "packet",
                    mutable_article_routes,
                    expected_article_id=str(entry["article_id"]),
                )
                if missing:
                    continue
        except RuntimeError:
            continue
        if entry["entry_id"] not in claimed and entry["entry_id"] not in quarantined and entry["entry_id"] not in held:
            valid.append((path, entry))
    valid.sort(key=lambda item: (item[1]["admitted_at_utc"], item[1]["entry_id"]))
    return valid


def claim_oldest(
    *,
    root: Path,
    operation_id: str,
    current_routes: set[str],
    current_topic_ids: set[str],
    base_state: Path,
) -> Path:
    with carrier_lock():
        return _claim_oldest_locked(
            root=root,
            operation_id=operation_id,
            current_routes=current_routes,
            current_topic_ids=current_topic_ids,
            base_state=base_state,
        )


def _claim_oldest_locked(
    *,
    root: Path,
    operation_id: str,
    current_routes: set[str],
    current_topic_ids: set[str],
    base_state: Path,
) -> Path:
    if not operation_id:
        raise RuntimeError("buffer_operation_id_required")
    claim_name = hashlib.sha256(operation_id.encode()).hexdigest() + ".json"
    claim_path = root.resolve() / "claims" / claim_name
    if claim_path.exists():
        claim = _read_sealed(claim_path, CLAIM_SCHEMA, "claim_sha256")
        manifest = Path(claim["entry_manifest"]["path"])
        validate_entry(manifest, current_routes=current_routes, current_topic_ids=current_topic_ids)
        # The claim is immutable provenance. A deployment may advance the
        # pointer after this operation claimed the entry; consume_oldest() must
        # receive the claim so it can rematerialize and validate the article
        # against the live base under the same lock. Rewriting or rejecting the
        # claim here strands the operation before that safety path runs.
        return claim_path
    rows = ready_entries(root, current_routes=current_routes, current_topic_ids=current_topic_ids)
    if not rows:
        raise BufferEmptyError("article_buffer_empty_or_invalid")
    manifest_path, entry = rows[0]
    claim = {
        "schema": CLAIM_SCHEMA,
        "status": "CLAIMED_OLDEST_VALID_READY_ENTRY",
        "operation_id": operation_id,
        "entry_id": entry["entry_id"],
        "topic_id": entry["topic_id"],
        "target_route": entry["target_route"],
        "entry_manifest": _spec(manifest_path),
        "current_base_state": _spec(base_state),
        # Stamped since 2026-08-20 so a claim whose run died can be proven
        # stale by ``recycle_stale_claims`` instead of holding the entry until
        # a human deletes the file.
        "claimed_at_utc": _utc_now(),
        "claimed_pt_date": pt_date_now(),
    }
    return _write_once(claim_path, claim, "claim_sha256")


def _claim_rows(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    claims = root.resolve() / "claims"
    rows: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(claims.glob("*.json")) if claims.is_dir() else []:
        try:
            rows.append((path, _read_sealed(path, CLAIM_SCHEMA, "claim_sha256")))
        except Exception:
            raise RuntimeError(f"buffer_claim_invalid:{path}") from None
    return rows


def stale_claims(root: Path, *, today_pt: str | None = None) -> list[dict[str, Any]]:
    """Claims from a PRIOR PT day whose operation produced no consumption.

    Read-only: the dry-run status lane reports these without mutating anything.
    A claim taken TODAY is never stale — the run that owns it may still be in
    flight, and the standard lock (not a clock) is what serialises live runs.
    """
    today = today_pt or pt_date_now()
    if PT_DATE_PATTERN.fullmatch(today) is None:
        raise RuntimeError("buffer_recycle_pt_date_invalid")
    consumed_dir = root.resolve() / "consumed"
    rows: list[dict[str, Any]] = []
    for path, claim in _claim_rows(root):
        if (consumed_dir / path.name).is_file():
            continue
        claimed = claim_pt_date(claim, path)
        if claimed >= today:
            continue
        rows.append({
            "claim": str(path),
            "operation_id": str(claim.get("operation_id") or ""),
            "entry_id": str(claim.get("entry_id") or ""),
            "topic_id": str(claim.get("topic_id") or ""),
            "target_route": str(claim.get("target_route") or ""),
            "claimed_pt_date": claimed,
            "claimed_at_utc": claim.get("claimed_at_utc"),
            "pt_date_source": "claim" if claim.get("claimed_pt_date") else "file_mtime",
        })
    return rows


def recycle_stale_claims(
    *,
    root: Path,
    work_root: Path,
    base_state: Path,
    today_pt: str | None = None,
) -> dict[str, Any]:
    """Auto-retire claims whose operation produced no consumption in its PT day.

    Tonight (2026-08-20) a killed carrier run left a sealed claim behind. The
    claim is what ``ready_entries`` subtracts, so the last ready article became
    invisible to every later run — ``BLOCKED_BUFFER_EMPTY`` with an article
    sitting right there — until the claim file was deleted by hand.

    Retirement is deliberately narrow and provable:

    * The standard carrier lock is held, so no live run can own any claim here.
    * Only claims from a STRICTLY EARLIER PT day are eligible; a same-day claim
      belongs to the run that is executing right now.
    * A claim whose operation actually finished is SEALED first, never dropped:
      ``_recover_completed_consumption`` runs before every retirement, exactly
      as the resume path does, so a publish that advanced truth before its
      consumption record was written still closes out properly.
    * Retirement writes a write-once receipt carrying the whole original claim
      before the claim file is removed. Nothing is silently discarded.

    Nothing is weakened: the entry itself is untouched, and it only becomes
    claimable again through ``ready_entries``, which still re-validates the
    sealed packet and still excludes anything already live.
    """
    with carrier_lock():
        today = today_pt or pt_date_now()
        rows = stale_claims(root, today_pt=today)
        retired: list[dict[str, Any]] = []
        recovered: list[dict[str, Any]] = []
        retire_root = root.resolve() / "claims-retired"
        for row in rows:
            claim_path = Path(row["claim"])
            operation_id = row["operation_id"]
            consumed_path = root.resolve() / "consumed" / claim_path.name
            note = None
            if operation_id:
                try:
                    sealed = _recover_completed_consumption(
                        root=root,
                        operation_id=operation_id,
                        base_state=base_state,
                        work_root=work_root,
                        consumed_path=consumed_path,
                    )
                except Exception as error:  # noqa: BLE001 - recorded, never hidden
                    sealed = None
                    note = f"{type(error).__name__}:{error}"
                if sealed is not None:
                    recovered.append({**row, "consumption": str(consumed_path)})
                    continue
            receipt = {
                "schema": CLAIM_RETIREMENT_SCHEMA,
                "status": "RETIRED_STALE_CLAIM_NO_CONSUMPTION",
                "retired_at_utc": _utc_now(),
                "retired_on_pt_date": today,
                "reason": "claim_pt_day_closed_without_consumption",
                "claim_pt_date": row["claimed_pt_date"],
                "pt_date_source": row["pt_date_source"],
                "operation_id": operation_id,
                "entry_id": row["entry_id"],
                "topic_id": row["topic_id"],
                "target_route": row["target_route"],
                "recovery_attempt": note or "no_completed_publication_found",
                "claim": json.loads(claim_path.read_text(encoding="utf-8")),
            }
            # One receipt per (claim, retirement day): a later re-claim of the
            # same operation id that dies again gets its own record instead of
            # colliding with this one under write-once.
            receipt_path = retire_root / f"{claim_path.stem}-{today}.json"
            _write_once(receipt_path, receipt, "retirement_sha256")
            os.chmod(claim_path, 0o600)
            claim_path.unlink()
            retired.append({**row, "retirement_receipt": str(receipt_path)})
        return {
            "status": "PASS_BUFFER_CLAIM_RECYCLE",
            "pt_date": today,
            "examined": len(rows),
            "retired": retired,
            "recovered": recovered,
        }


def consume_oldest(
    *,
    root: Path,
    operation_id: str,
    current_routes: set[str],
    current_topic_ids: set[str],
    base_state: Path,
    work_root: Path,
    # (packet_work, entry, base_state_spec, operation_id, generation) -> plan.
    # ``generation`` is 0 for the first plan and increments when a cached plan
    # is regenerated after control-pin drift, so the caller can mint a fresh
    # run id and release root instead of colliding with the superseded one.
    rematerialize: Callable[[Path, dict[str, Any], dict[str, str], str, int], dict[str, Any]],
    publish: Callable[[dict[str, Any], str, int], dict[str, Any]],
    current_truth: Callable[[], tuple[set[str], set[str]]] | None = None,
) -> dict[str, Any]:
    with carrier_lock() as lock_fd:
        consumed_path = root.resolve() / "consumed" / (hashlib.sha256(operation_id.encode()).hexdigest() + ".json")
        if consumed_path.exists():
            return _read_sealed(consumed_path, CONSUME_SCHEMA, "consumption_sha256")
        recovered = _recover_completed_consumption(
            root=root,
            operation_id=operation_id,
            base_state=base_state,
            work_root=work_root,
            consumed_path=consumed_path,
        )
        if recovered is not None:
            return recovered
        if current_truth is not None:
            live_truth = current_truth()
            if (
                not isinstance(live_truth, tuple)
                or len(live_truth) != 2
                or not all(isinstance(value, set) for value in live_truth)
            ):
                raise RuntimeError("buffer_current_publication_truth_invalid")
            current_routes, current_topic_ids = live_truth
        # A dead run must not hold an entry hostage. Same lock, prior PT days
        # only, completed work sealed before anything is retired.
        recycle_stale_claims(root=root, work_root=work_root, base_state=base_state)
        quarantined: list[dict[str, Any]] = []
        while True:
            reconciled = _finalize_declared_quarantine(
                root=root,
                work_root=work_root,
                operation_id=operation_id,
            )
            if reconciled is not None and reconciled not in quarantined:
                quarantined.append(reconciled)
            try:
                claim_path = claim_oldest(
                    root=root,
                    operation_id=operation_id,
                    current_routes=current_routes,
                    current_topic_ids=current_topic_ids,
                    base_state=base_state,
                )
            except BufferEmptyError as error:
                if quarantined:
                    exhausted = BufferEmptyError(
                        "article_buffer_no_publishable_entries_after_quarantine"
                    )
                    exhausted.quarantined = quarantined
                    raise exhausted from error
                raise
            claim = _read_sealed(claim_path, CLAIM_SCHEMA, "claim_sha256")
            manifest_path = _verify_spec(claim["entry_manifest"], "buffer_claim_entry")
            entry = validate_entry(
                manifest_path,
                current_routes=current_routes,
                current_topic_ids=current_topic_ids,
            )
            # This is the live current-content-base pointer reread, performed while
            # holding the same lock as every fresh/update deployment.
            base_spec = _spec(base_state)
            claim_base_drift = base_spec != claim["current_base_state"]

            workspace = work_root.resolve() / hashlib.sha256(operation_id.encode()).hexdigest()
            packet_work = workspace / "packet"
            if workspace.exists():
                if not packet_work.is_dir():
                    raise RuntimeError("buffer_workspace_replay_invalid")
            else:
                workspace.mkdir(parents=True)
                shutil.copytree(
                    manifest_path.parent / "packet",
                    packet_work,
                    copy_function=shutil.copyfile,
                )
            # Admission seals packet directories read-only. copytree preserves those
            # directory modes, but the guarded remint writes temporary authority
            # files beside the immutable subjects. Restore write access only inside
            # this operation-scoped workspace, including on a failed-run replay.
            os.chmod(packet_work, 0o700)
            for path in packet_work.rglob("*"):
                if path.is_dir():
                    os.chmod(path, 0o700)
                elif path.is_file():
                    os.chmod(path, 0o600)
            generation, materialization_path, materialization = _latest_materialization(workspace)
            post_deploy_recovery = _post_deploy_recovery_materialization(
                workspace,
                operation_id=operation_id,
                entry=entry,
                base_spec=base_spec,
            )
            if post_deploy_recovery is not None:
                generation, materialization_path, materialization = post_deploy_recovery
            control_drift: list[dict[str, str]] = []
            base_drift = False
            superseded: dict[str, Any] | None = None
            if materialization is not None:
                if (
                    materialization.get("operation_id") != operation_id
                    or materialization.get("entry_id") != entry["entry_id"]
                ):
                    raise RuntimeError("buffer_materialization_replay_mismatch")
                materialized = materialization.get("materialized")
                base_drift = materialization.get("current_base_state") != base_spec
                # The cached plan is only resumable while it still binds the CURRENT
                # control chain. On drift, regenerate rather than replay a template
                # the apply guard will reject forever.
                control_drift = apply_template_control_drift(materialized)
                if (control_drift or base_drift) and post_deploy_recovery is None:
                    superseded = {
                        "generation": generation,
                        "materialization": str(materialization_path),
                        "apply_template": materialized.get("apply_template")
                        if isinstance(materialized, dict) else None,
                        "release_root": materialized.get("release_root")
                        if isinstance(materialized, dict) else None,
                    }
                    generation += 1
                    materialization_path = workspace / f"materialization-r{generation}.json"
                    materialized = rematerialize(
                        packet_work, entry, base_spec, operation_id, generation
                    )
            else:
                generation, materialization_path = 0, workspace / "materialization.json"
                base_drift = claim_base_drift
                if base_drift:
                    superseded = {
                        "claim": str(claim_path),
                        "claim_current_base_state": claim["current_base_state"],
                    }
                materialized = rematerialize(
                    packet_work, entry, base_spec, operation_id, generation
                )
            if not isinstance(materialized, dict):
                raise RuntimeError("buffer_rematerialization_result_invalid")
            if materialized.get("operation", "new_article") != "new_article":
                raise RuntimeError("buffer_release_operation_invalid")
            if materialized.get("base_state_sha256") != base_spec["sha256"]:
                raise RuntimeError("buffer_release_not_rematerialized_on_current_base")
            for label in ("apply_template", "review_job"):
                if label in materialized:
                    _verify_spec(materialized[label], f"buffer_materialization_{label}")
            if not materialization_path.exists():
                record = {
                    "schema": MATERIALIZATION_SCHEMA,
                    "status": "PASS_BUFFER_REMATERIALIZED_CURRENT_BASE",
                    "operation_id": operation_id,
                    "entry_id": entry["entry_id"],
                    "current_base_state": base_spec,
                    "materialized": materialized,
                    "generation": generation,
                }
                if superseded is not None:
                    record["status"] = (
                        "PASS_BUFFER_REMATERIALIZED_AFTER_HEAD_CHANGE"
                        if base_drift
                        else "PASS_BUFFER_REMATERIALIZED_AFTER_CONTROL_DRIFT"
                    )
                    if control_drift:
                        record["control_drift"] = control_drift
                    if base_drift:
                        record["base_drift"] = {
                            "previous": superseded.get("claim_current_base_state")
                            or materialization.get("current_base_state"),
                            "current": base_spec,
                            "stale_publish_permitted": False,
                        }
                    record["supersedes"] = superseded
                _write_once(materialization_path, record, "materialization_sha256")
            # The adapter must pass lock_fd to the standard apply subprocess so the
            # same flock remains held without a self-deadlock in the child chain.
            try:
                rejection = materialized.get("permanent_rejection")
                if rejection is not None:
                    if (
                        not isinstance(rejection, dict)
                        or not str(rejection.get("reason") or "").startswith(
                            "inbound_link_source_missing:/"
                        )
                    ):
                        raise RuntimeError("buffer_permanent_rejection_marker_invalid")
                    raise PermanentArticleRejection(
                        str(rejection["reason"]),
                        article_id=str(rejection.get("article_id") or ""),
                        diagnostic=str(rejection.get("diagnostic") or ""),
                    )
                head_change_recoveries = 0
                while True:
                    try:
                        result = publish(materialized, operation_id, lock_fd)
                        break
                    except Exception as error:
                        if "production_head_changed" not in str(error):
                            raise
                        if head_change_recoveries >= 8:
                            raise RuntimeError(
                                "buffer_head_change_remint_budget_exhausted"
                            ) from error
                        next_base_spec = _spec(base_state)
                        if next_base_spec == base_spec:
                            raise RuntimeError(
                                "buffer_head_change_without_current_base_advance"
                            ) from error
                        previous = {
                            "generation": generation,
                            "materialization": str(materialization_path),
                            "apply_template": materialized.get("apply_template"),
                            "release_root": materialized.get("release_root"),
                            "current_base_state": base_spec,
                        }
                        base_spec = next_base_spec
                        generation += 1
                        head_change_recoveries += 1
                        materialization_path = workspace / f"materialization-r{generation}.json"
                        materialized = rematerialize(
                            packet_work, entry, base_spec, operation_id, generation
                        )
                        if not isinstance(materialized, dict):
                            raise RuntimeError("buffer_rematerialization_result_invalid")
                        if materialized.get("operation", "new_article") != "new_article":
                            raise RuntimeError("buffer_release_operation_invalid")
                        if materialized.get("base_state_sha256") != base_spec["sha256"]:
                            raise RuntimeError(
                                "buffer_release_not_rematerialized_on_current_base"
                            )
                        for label in ("apply_template", "review_job"):
                            if label in materialized:
                                _verify_spec(
                                    materialized[label],
                                    f"buffer_materialization_{label}",
                                )
                        record = {
                            "schema": MATERIALIZATION_SCHEMA,
                            "status": "PASS_BUFFER_REMATERIALIZED_AFTER_HEAD_CHANGE",
                            "operation_id": operation_id,
                            "entry_id": entry["entry_id"],
                            "current_base_state": base_spec,
                            "materialized": materialized,
                            "generation": generation,
                            "base_drift": {
                                "previous": previous["current_base_state"],
                                "current": base_spec,
                                "refusal": f"{type(error).__name__}:{error}",
                                "stale_publish_permitted": False,
                            },
                            "supersedes": previous,
                        }
                        _write_once(
                            materialization_path,
                            record,
                            "materialization_sha256",
                        )
            except PermanentArticleRejection as error:
                quarantine = _declare_permanent_quarantine(
                    root=root,
                    work_root=work_root,
                    operation_id=operation_id,
                    claim_path=claim_path,
                    claim=claim,
                    entry=entry,
                    materialization_path=materialization_path,
                    error=error,
                )
                quarantined.append(quarantine)
                continue
            if not isinstance(result, dict) or result.get("status") not in {
                "PASS_LIVE_GSC_READBACK_PASS",
                "PASS_BUFFER_PUBLISHED",
                "IDEMPOTENT_ALREADY_PASS",
            }:
                raise RuntimeError("buffer_publish_failed")
            consumption = {
                "schema": CONSUME_SCHEMA,
                "status": "PASS_BUFFER_PUBLISHED",
                "operation_id": operation_id,
                "entry_id": entry["entry_id"],
                "topic_id": entry["topic_id"],
                "target_route": entry["target_route"],
                "claim": _spec(claim_path),
                "current_base_state": base_spec,
                "rematerialization": materialized,
                "publication": result,
                "quarantined_before_publication": quarantined,
            }
            _write_once(consumed_path, consumption, "consumption_sha256")
            return _read_sealed(consumed_path, CONSUME_SCHEMA, "consumption_sha256")


def pool_status(
    root: Path,
    *,
    current_routes: set[str],
    current_topic_ids: set[str],
    mutable_article_routes: set[str] | None = None,
) -> dict[str, Any]:
    count = len(ready_entries(
        root,
        current_routes=current_routes,
        current_topic_ids=current_topic_ids,
        mutable_article_routes=mutable_article_routes,
    ))
    return {
        "ready": count,
        "minimum": READY_MIN,
        "target": READY_TARGET,
        "maximum": READY_MAX,
        "deficit_to_minimum": max(0, READY_MIN - count),
        "deficit_to_target": max(0, READY_TARGET - count),
        "at_or_above_minimum": count >= READY_MIN,
    }


def refill_review_only(
    candidates: Iterable[dict[str, Any]],
    *,
    root: Path,
    current_routes: set[str],
    current_topic_ids: set[str],
    mutable_article_routes: set[str],
    review_runner: Callable[[Path, bool], dict[str, Any]],
    control_files: dict[str, str],
    admitted_at_utc: str,
) -> dict[str, Any]:
    """Refill to five by invoking the standard Opus loop with apply=False."""
    admitted: list[str] = []
    failures: list[dict[str, str]] = []
    for candidate in candidates:
        if len(ready_entries(
            root,
            current_routes=current_routes,
            current_topic_ids=current_topic_ids,
            mutable_article_routes=mutable_article_routes,
        )) >= READY_TARGET:
            break
        try:
            closeout = review_runner(Path(candidate["review_job"]), False)
            if closeout.get("status") != "PASS_OPUS_SEO_DRAFT" or closeout.get("publication_invoked") is not False:
                raise BufferAdmissionError("buffer_review_only_not_pass")
            round_receipt = Path(closeout["rounds"][-1]["receipt"])
            manifest = admit(
                root=root,
                packet=Path(candidate["packet"]),
                pass_receipt=round_receipt,
                control_files=control_files,
                current_routes=current_routes,
                current_topic_ids=current_topic_ids,
                mutable_article_routes=mutable_article_routes,
                admitted_at_utc=admitted_at_utc,
            )
            admitted.append(str(manifest))
        except Exception as error:
            failures.append({"candidate": str(candidate.get("review_job")), "reason": f"{type(error).__name__}:{error}"})
    status = pool_status(
        root,
        current_routes=current_routes,
        current_topic_ids=current_topic_ids,
        mutable_article_routes=mutable_article_routes,
    )
    return {"status": "PASS_BUFFER_REFILL" if status["at_or_above_minimum"] else "BUFFER_CAPACITY_DEFICIT",
            "pool": status, "admitted": admitted, "failures": failures, "review_apply": False}


def current_publication_truth(
    *,
    release_root: Path,
    catalog_relpaths: Iterable[str],
    published_ledger: Path | None = None,
) -> tuple[set[str], set[str]]:
    routes: set[str] = set()
    topics: set[str] = set()
    root = release_root.resolve(strict=True)
    for relative in catalog_relpaths:
        payload = json.loads((root / relative).read_text(encoding="utf-8"))
        for row in payload.get("articles") or []:
            route = row.get("canonical_path")
            if route:
                routes.add(str(route))
            if row.get("topic_id"):
                topics.add(str(row["topic_id"]))
    sitemap = (root / "public/sitemap.xml").read_text(encoding="utf-8")
    for location in __import__("re").findall(r"<loc>\s*([^<]+?)\s*</loc>", sitemap):
        parsed = urlsplit(location)
        if parsed.path:
            routes.add("/" + parsed.path.strip("/") + "/")
    if published_ledger and published_ledger.is_file():
        ledger = json.loads(published_ledger.read_text(encoding="utf-8"))
        for row in ledger.get("entries") or []:
            if row.get("status") == "PASS_LIVE":
                if row.get("topic_id"):
                    topics.add(str(row["topic_id"]))
                if row.get("target_route") or row.get("route"):
                    routes.add(str(row.get("target_route") or row.get("route")))
    return routes, topics
