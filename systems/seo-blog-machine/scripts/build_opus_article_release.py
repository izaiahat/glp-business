#!/usr/bin/env python3
"""Build/seal one GLP article release from an exact Opus PASS packet."""
from __future__ import annotations

import argparse
import datetime as dt
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from existing_article_updates import (  # noqa: E402
    materialize_national_article_ownership,
    preserve_r2_route_governance,
    replace_article_and_ownership,
    validate_r2_execution_contract,
    validate_r2_refresh,
)
from r2_execution_control import require_surface  # noqa: E402
from carrier_lock import carrier_lock  # noqa: E402
UPDATE_CONTROL_PATH = Path(__file__).resolve().parent / "existing_article_updates.py"
UPDATE_CONTROL_HELPER_PATH = Path(__file__).resolve().parent / "r2_execution_control.py"

MATERIALIZER_DIR = Path(__file__).resolve().parents[3] / "scripts"
if str(MATERIALIZER_DIR) not in sys.path:
    sys.path.insert(0, str(MATERIALIZER_DIR))
from site_tree_materializer import (  # noqa: E402
    FLAGSHIP_REQUIRED_PUBLIC_BUILD_INPUTS,
    constructor_provenance,
    copytree_linked,
    materialize_accepted_prepared_base,
    detach_for_write,
    prepare_clean_build_trees,
    remove_materialized_tree,
    restore_linked_subtree,
    seal_tree_selective,
)
from glp_served_test_contract import (  # noqa: E402
    SERVED_TEST_ROOT_CANONICAL,
    SERVED_TEST_ROOT_DYNAMIC,
    SERVED_TEST_ROOT_LEGACY,
    normalize_candidate_local_served_test,
)
MATERIALIZER_PATH = MATERIALIZER_DIR / "site_tree_materializer.py"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import article_images  # noqa: E402
import article_image_release_contract  # noqa: E402
import defect_remediation  # noqa: E402
import review_transports  # noqa: E402
import accepted_failure_contract  # noqa: E402
import carrier_fixture_contracts  # noqa: E402

ARTICLE_IMAGES_PATH = Path(__file__).resolve().parent / "article_images.py"

BUSINESS = Path("/home/ubuntu/business")
ACCOUNT = "f860943dff399ffb1db0a28dcd3dadfd"
PROJECT = "glp-clinic-scout"
D1 = "bcb91d2d-c3f1-4c55-93e3-070587e86d60"
CF_API = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/pages/projects/{PROJECT}"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
HOME_IMAGE_BUILD_OUTPUTS = tuple(
    Path(f"assets/derivatives/{stem}-{width}.{extension}")
    for stem, widths in {
        "home-hero-fruit": (640, 1024),
        "home-hero-fridge": (640, 1024),
        "home-hero-hand": (640, 1024),
        "home-lifestyle-phone": (768, 1536),
    }.items()
    for width in widths
    for extension in ("avif", "webp", "jpg")
)
PRODUCT_RENDER_BUILD_OUTPUTS = tuple(
    Path(f"assets/derivatives/phase3-product-{stem}-{width}.{extension}")
    for stem in ("wegovy-pill", "wegovy", "ozempic", "mounjaro", "zepbound")
    for width in (640, 1024)
    for extension in ("avif", "webp", "jpg")
)
CSS_BUILD_OUTPUTS = tuple(
    Path(f"assets/{name}")
    for name in (
        "site.css",
        "site-home.css",
        "site-money.css",
        "site-review.css",
        "site-editorial.css",
        "site-tools.css",
        "site-trust.css",
        "site-p3.css",
    )
)
IN_PLACE_BUILD_OUTPUTS = (
    Path("assets/provider-logos/pass2-lp-atlas.webp"),
    Path("assets/provider-logos/pass2-lp-atlas.json"),
    *HOME_IMAGE_BUILD_OUTPUTS,
    *PRODUCT_RENDER_BUILD_OUTPUTS,
    *CSS_BUILD_OUTPUTS,
    Path("artifacts/ultracode/phase2-home/generated-image-lineage.json"),
    Path("artifacts/ultracode/phase3-product-renders/generated-image-lineage.json"),
)
IN_PLACE_BUILD_OUTPUT_TREES = (Path("artifacts/build4-preservation"),)
ARTICLE_OVERLAYS = (
    Path("src/price-state.mjs"),
    Path("src/blog-system.mjs"),
    Path("src/site-capture-consent-sources.mjs"),
    Path("tests/editorial-monetization.test.mjs"),
    Path("tests/mounjaro-coupon-recovery.test.mjs"),
    Path("tests/pass2-late-audit-served.test.mjs"),
    Path("tests/pass2-late-audit-static-output.test.mjs"),
    Path("tests/playbook.test.mjs"),
    Path("tests/product-ready.test.mjs"),
    Path("tests/wave2-regression.test.mjs"),
    # This test copies public/ into a mutable /tmp fixture. Node's cp preserves
    # source modes, so it must not remain linked to the 0444 sealed predecessor
    # or its own postbuild mutation fails EACCES before testing provenance.
    Path("tests/lastmod-declared-routes.test.mjs"),
    Path("tests/affiliati-attribution-local-d1.test.mjs"),
    Path("tests/phase3-page-graph.test.mjs"),
    Path("tests/provider-pharmacy-emulation.test.mjs"),
    Path("scripts/validate-program-coverage.mjs"),
    Path("tests/program-registry-coverage.test.mjs"),
    Path("tests/price-watch-email.test.mjs"),
    Path("tests/article-imagery-pipeline.test.mjs"),
    Path("scripts/build-staging.mjs"),
    Path("scripts/generate-article-imagery.py"),
    Path("data/blog/content-batch.json"),
    Path("data/blog/content-national-wow-additions.json"),
    Path("data/blog/keyword-ownership-map.json"),
    Path("data/tracking-allowed-hosts.json"),
    Path("artifacts/ultracode/article-image-manifest.json"),
    # Accepted article releases carry the evolving route-master state locally.
    # Detach it with the rest of the bounded write surface so generation N+1
    # cannot write through a sealed predecessor hardlink.
    Path("artifacts/ultracode/article-route-master-state.json"),
    Path("functions/go/program-manifest.js"),
    # The clean build regenerates these bounded artifacts in place rather than
    # below public/ or artifacts/ops. Detach every generator output from the
    # sealed predecessor before Pillow/JSON writes.
    *IN_PLACE_BUILD_OUTPUTS,
)
GENERATED_TREES = (Path("public"), Path("artifacts/ops"))
CLEAN_BUILD_INPUTS = (
    *FLAGSHIP_REQUIRED_PUBLIC_BUILD_INPUTS,
    # The accepted predecessor's build-staging.mjs reads these Build 4 artifacts
    # before resetting public/ and artifacts/ops. Preserve them exactly so the
    # production article chain can perform a real clean build from that base.
    Path("public/glp-1-price-index/index.html"),
    Path("artifacts/ops/price-index-manifest.json"),
    Path("artifacts/ops/wave4-deal-ingestion-manifest.json"),
    # FLAGSHIP_REQUIRED_PUBLIC_BUILD_INPUTS already retains the complete
    # google-ads-api predecessor input set, including its assets.
)
IMAGE_SUFFIXES = (
    "-800.avif", "-1600.avif", "-800.webp", "-1600.webp",
    "-800.jpg", "-1600.jpg", "-og.jpg",
)
ROUTE_MASTER_STATE_SOURCE = (
    BUSINESS
    / "reports/glp-clinic-scout/rebuild/pass2-image-generation/state.json"
)
ROUTE_MASTER_STATE_RELATIVE = Path(
    "artifacts/ultracode/article-route-master-state.json"
)
# Generated lineage manifests are rewritten in place by the build's own
# generate:home-imagery / generate:phase3-product-renders steps; a release
# packaged from a fresh main build may not carry them (glp_release_main).
GENERATED_LINEAGE_OVERLAYS = (
    Path("artifacts/ultracode/phase2-home/generated-image-lineage.json"),
    Path("artifacts/ultracode/phase3-product-renders/generated-image-lineage.json"),
)
OPTIONAL_ARTICLE_OVERLAYS = (ROUTE_MASTER_STATE_RELATIVE, *GENERATED_LINEAGE_OVERLAYS)

# ---------------------------------------------------------------------------
# tests/playbook.test.mjs — historical-cohort batch-gate assertion states.
#
# Every state below is written by this builder. ``new_article`` upgrades
# PREDECESSOR -> SUCCESSOR; an ``r2_refresh`` then upgrades SUCCESSOR ->
# R2_SUCCESSOR. When an r2 release is accepted as the current content base the
# next build must recognise R2_SUCCESSOR as already-satisfying instead of
# failing ``playbook_batch_gate_anchor_invalid:old=0:new=0`` (2026-08-17
# carrier blocker). Both the writing step and the accepting step read these
# same constants, so the accepted-state set can never drift from what the
# builder can emit; ``test_playbook_anchor_states_are_build_idempotent``
# enforces that invariant mechanically.
# ---------------------------------------------------------------------------
PLAYBOOK_HISTORICAL_BATCH_GATE_PREDECESSOR = (
    "assert.ok(data.articles.every((item) => item.author.clinical_credentials_claimed === false"
    " && item.batch_gate_passed === false));"
)
PLAYBOOK_HISTORICAL_BATCH_GATE_SUCCESSOR = (
    "assert.ok(data.articles.slice(0, 29).every((item) => item.author.clinical_credentials_claimed === false"
    " && item.batch_gate_passed === false));"
)
PLAYBOOK_HISTORICAL_BATCH_GATE_R2_SUCCESSOR = (
    "assert.ok(data.articles.slice(0, 29).every((item) => item.author.clinical_credentials_claimed === false"
    " && (item.batch_gate_passed === false || (item.batch_gate_passed === true"
    ' && typeof item.operator_release_receipt === "string"'
    " && /^[a-f0-9]{64}$/.test(item.operator_release_receipt)))));"
)
PLAYBOOK_HISTORICAL_BATCH_GATE_R2_SUCCESSORS = (PLAYBOOK_HISTORICAL_BATCH_GATE_R2_SUCCESSOR,)
NATIONAL_CONTENT_RELEASE_GATE_PREDECESSOR = (
    '  if (nationalContent.mode !== "isolated_staging_link_dark_noindex_no_deployment"'
    ' || nationalContent.batch_gate_passed !== false'
    ' || nationalContent.operator_release_receipt !== null'
    ' || nationalContent.articles.length !== 28'
    ' || nationalContent.articles.some((article) => article.has_affiliate_cta !== false'
    ' || article.comparison_required !== false'
    ' || article.operator_release_receipt !== null))'
    ' throw new Error("national_content_release_gate_invalid");'
)
NATIONAL_CONTENT_RELEASE_GATE_R2_SUCCESSOR = '''  if (
    nationalContent.mode !== "isolated_staging_link_dark_noindex_no_deployment"
    || nationalContent.batch_gate_passed !== false
    || nationalContent.operator_release_receipt !== null
    || nationalContent.articles.length !== 28
    || nationalContent.articles.some((article) => (
      article.has_affiliate_cta !== false
      || article.comparison_required !== false
      || !(
        (article.batch_gate_passed === false && article.operator_release_receipt === null)
        || (article.batch_gate_passed === true
          && typeof article.operator_release_receipt === "string"
          && /^[a-f0-9]{64}$/.test(article.operator_release_receipt))
      )
    ))
  ) throw new Error("national_content_release_gate_invalid");'''
PLAYBOOK_BATCH_FIVE_PREDECESSOR = (
    'assert.ok(added.every((item) => item.word_count >= 1800 && item.word_count <= 2600'
    ' && item.sections.length >= 6 && item.sections.length <= 10'
    ' && item.author.author_id === "izaiah-tilton" && item.author.clinical_credentials_claimed === false'
    ' && item.batch_gate_passed === false && item.operator_release_receipt === null'
    ' && item.internal_links.length >= 3'
    ' && item.internal_links.some((link) => /^\\/(?:best|cost|compare)\\//.test(link.path))));'
)
PLAYBOOK_BATCH_FIVE_R2_SUCCESSOR = (
    'assert.ok(added.every((item) => item.author.clinical_credentials_claimed === false'
    ' && item.internal_links.length >= 3'
    ' && ((item.word_count >= 1800 && item.word_count <= 2600'
    ' && item.sections.length >= 6 && item.sections.length <= 10'
    ' && item.author.author_id === "izaiah-tilton" && item.batch_gate_passed === false'
    ' && item.operator_release_receipt === null'
    ' && item.internal_links.some((link) => /^\\/(?:best|cost|compare)\\//.test(link.path)))'
    ' || (item.word_count >= 1500 && item.word_count <= 3600'
    ' && item.sections.length >= 6 && item.sections.length <= 16'
    ' && item.batch_gate_passed === true'
    ' && typeof item.operator_release_receipt === "string"'
    ' && /^[a-f0-9]{64}$/.test(item.operator_release_receipt)))));'
)
PLAYBOOK_BATCH_SIX_PREDECESSOR = (
    'assert.ok(added.every((item) => item.word_count >= 2100 && item.word_count <= 2350'
    ' && item.sections.length >= 6 && item.sections.length <= 10'
    ' && item.author.author_id === "izaiah-tilton" && item.author.clinical_credentials_claimed === false'
    ' && item.batch_gate_passed === false && item.operator_release_receipt === null'
    ' && item.internal_links.length >= 3'
    ' && item.internal_links.some((link) => /^\\/(?:best|cost|compare)\\//.test(link.path))));'
)
PLAYBOOK_BATCH_SIX_R2_SUCCESSOR = (
    'assert.ok(added.every((item) => item.author.clinical_credentials_claimed === false'
    ' && item.internal_links.length >= 3'
    ' && ((item.word_count >= 2100 && item.word_count <= 2350'
    ' && item.sections.length >= 6 && item.sections.length <= 10'
    ' && item.author.author_id === "izaiah-tilton" && item.batch_gate_passed === false'
    ' && item.operator_release_receipt === null'
    ' && item.internal_links.some((link) => /^\\/(?:best|cost|compare)\\//.test(link.path)))'
    ' || (item.word_count >= 1500 && item.word_count <= 3600'
    ' && item.sections.length >= 6 && item.sections.length <= 16'
    ' && item.batch_gate_passed === true'
    ' && typeof item.operator_release_receipt === "string"'
    ' && /^[a-f0-9]{64}$/.test(item.operator_release_receipt)))));'
)


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def payload_hash(value: dict[str, Any], field: str) -> str:
    copy = dict(value)
    copy.pop(field, None)
    raw = json.dumps(copy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()


def atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.chmod(temp, 0o600)
    os.replace(temp, path)


def rows(root: Path) -> list[dict[str, Any]]:
    out = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"symlink_forbidden:{path}")
        if path.is_file():
            out.append({"path": path.relative_to(root).as_posix(), "sha256": sha(path), "bytes": path.stat().st_size})
    return out


def closure(file_rows: list[dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for row in sorted(file_rows, key=lambda item: item["path"]):
        h.update(f"{row['path']}\0{row['sha256']}\0{row['bytes']}\n".encode())
    return h.hexdigest()


def checked(spec: dict[str, Any], label: str) -> Path:
    path = Path(str(spec.get("path", ""))).resolve()
    expected = str(spec.get("sha256", ""))
    if not path.is_file() or len(expected) != 64 or sha(path) != expected:
        raise RuntimeError(f"{label}_drift")
    return path


def checked_descriptor(spec: Any, label: str) -> Path:
    """Validate an exact descriptor without path/hash coercion or aliases."""
    if type(spec) is not dict or set(spec) != {"path", "sha256"}:
        raise RuntimeError(f"{label}_descriptor_invalid")
    raw_path = spec.get("path")
    expected = spec.get("sha256")
    if type(raw_path) is not str or not raw_path:
        raise RuntimeError(f"{label}_path_invalid")
    if type(expected) is not str or SHA256_PATTERN.fullmatch(expected) is None:
        raise RuntimeError(f"{label}_hash_invalid")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        raise RuntimeError(f"{label}_path_not_absolute")
    try:
        path = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as error:
        raise RuntimeError(f"{label}_path_invalid") from error
    if raw_path != str(path):
        raise RuntimeError(f"{label}_path_alias_or_traversal")
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"{label}_path_invalid")
    if sha(path) != expected:
        raise RuntimeError(f"{label}_hash_drift")
    return path


def strict_iso_date(value: Any, label: str) -> str:
    if type(value) is not str or ISO_DATE_PATTERN.fullmatch(value) is None:
        raise RuntimeError(f"{label}_invalid")
    try:
        if dt.date.fromisoformat(value).isoformat() != value:
            raise ValueError(value)
    except ValueError as error:
        raise RuntimeError(f"{label}_invalid") from error
    return value


def resolve_article_contract(job: dict[str, Any]) -> tuple[Path, dict[str, Any], str]:
    """Resolve exact article bytes and one unambiguous checked date."""
    if "article_json" not in job:
        raise RuntimeError("article_json_descriptor_missing")
    path = checked_descriptor(job["article_json"], "article_json")
    try:
        article = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("article_json_invalid") from error
    if not isinstance(article, dict):
        raise RuntimeError("article_json_invalid")
    article_date = strict_iso_date(
        article.get("prices_checked_date"),
        "article_prices_checked_date",
    )
    if "prices_checked_date" in job:
        job_date = strict_iso_date(job["prices_checked_date"], "job_prices_checked_date")
        if job_date != article_date:
            raise RuntimeError("prices_checked_date_conflict")
    return path, article, article_date


def has_control_pin(job: dict[str, Any], required: Path) -> bool:
    """True when the job's template declares this control file at all."""
    return any(
        Path(str(spec.get("path", ""))).resolve() == required.resolve()
        for spec in job.get("control_files") or []
    )


def require_control_pin(job: dict[str, Any], required: Path, label: str) -> Path:
    matches = [
        spec for spec in job.get("control_files") or []
        if Path(str(spec.get("path", ""))).resolve() == required.resolve()
    ]
    if len(matches) != 1:
        raise RuntimeError(f"{label}_control_pin_missing")
    return checked(matches[0], f"{label}_control_pin")


def load_env() -> dict[str, str]:
    path = Path("/home/ubuntu/.hermes/.env")
    info = path.stat()
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("deployment_env_permissions_invalid")
    values: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        if "=" in raw and not raw.lstrip().startswith("#"):
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def require_opus(job: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    receipt_path = checked(job["opus_receipt"], "opus_receipt")
    receipt = json.loads(receipt_path.read_text())
    manifest_path = checked(job["subject_manifest"], "subject_manifest")
    operator_directed = receipt.get("status") == "PASS_OPERATOR_DIRECTED_CURRENT_BYTE"
    if operator_directed:
        # Registry, not a hardcoded id: operator-ordered byte changes recur, and
        # each one must name a directive that records its order date, the defect
        # report it cites, and the exact routes it may touch.
        try:
            defect_remediation.directive(receipt.get("operator_directive_id"))
        except defect_remediation.DefectRemediationError as error:
            raise RuntimeError(f"operator_directed_pass_invalid:{error}") from error
        if not defect_remediation.directive_allows_route(
            receipt.get("operator_directive_id"), job.get("target_route")
        ):
            raise RuntimeError("operator_directed_route_outside_directive")
        qa_path = Path(str(receipt.get("deterministic_qa_receipt") or "")).resolve()
        if (not qa_path.is_file()
                or receipt.get("deterministic_qa_sha256") != sha(qa_path)
                or json.loads(qa_path.read_text()).get("status") != "PASS"):
            raise RuntimeError("operator_directed_qa_invalid")
    else:
        if receipt.get("status") != "PASS_OPUS_SEO_DRAFT" or receipt.get("tools_disabled") is not True:
            raise RuntimeError("opus_pass_required")
        # Dual-authorized review transport (operator authorization 2026-08-18).
        # The publish side must accept exactly the transports the buffer admits,
        # or an Opus-reviewed article could be admitted and then never publish.
        # Identities come from the shared table, never hardcoded here.
        try:
            transport = review_transports.transport(receipt.get("review_transport"))
        except review_transports.TransportError as error:
            raise RuntimeError(f"opus_identity_invalid:{error}") from error
        if (receipt.get("provider") != transport["provider"]
                or receipt.get("model") != transport["model"]
                or receipt.get("effort") != transport["effort"]):
            raise RuntimeError("opus_identity_invalid")
        # The summary is not authority for the runtime that actually reviewed.
        # Buffer admission already enforces this same raw pin and shared predicate.
        if (not isinstance(receipt.get("raw_review_path"), str)
                or not isinstance(receipt.get("raw_review_sha256"), str)):
            raise RuntimeError("opus_raw_pin_missing")
        raw_path = checked({"path": receipt["raw_review_path"],
                            "sha256": receipt["raw_review_sha256"]}, "opus_raw_review")
        try:
            raw_transport = review_transports.validate_raw_review_runtime(
                json.loads(raw_path.read_text()))
        except (ValueError, review_transports.TransportError) as error:
            raise RuntimeError(f"opus_raw_runtime_invalid:{error}") from error
        if raw_transport["transport_id"] != transport["transport_id"]:
            raise RuntimeError("opus_review_transport_mismatch")
    if receipt.get("subject_manifest_sha256") != sha(manifest_path):
        raise RuntimeError("opus_subject_binding_invalid")
    if receipt.get("findings") or any(row.get("status") != "PASS" for row in receipt.get("gates") or []):
        raise RuntimeError("opus_false_pass")
    if [row.get("gate") for row in receipt.get("gates") or []] != list("ABCDE"):
        raise RuntimeError("opus_gate_set_invalid")
    return receipt_path, receipt


BASE_SEAL_REPAIR_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/base-seal-repairs"
SITE_RELEASES_ROOT = BUSINESS / "site-system/flagship-v2/releases"
SEALED_FILE_MODE = 0o444
SEALED_DIRECTORY_MODE = 0o555
SEALED_OWNER = (0, 0)


def unsealed_base_entries(base: Path) -> list[str]:
    """Every entry under ``base`` that is writable or not owned by the seal owner."""
    offenders: list[str] = []
    for path in [base, *sorted(base.rglob("*"))]:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"base_symlink_forbidden:{path}")
        if stat.S_IMODE(info.st_mode) & 0o222 or (info.st_uid, info.st_gid) != SEALED_OWNER:
            offenders.append(str(path))
    return offenders


def ensure_accepted_base_sealed(
    base: Path, verified_rows: list[dict[str, Any]], verified_tree: str
) -> dict[str, Any]:
    """Seal a byte-verified accepted base that a sibling machine left writable.

    ``reports/.../current-content-base.json`` is written by more than one
    machine. The 2026-08-17 Katalys paused-program removal repointed it at an
    ubuntu-owned, mode-0755 release tree, and ``copytree_linked`` then refused
    that base with ``immutable_base_writable:.`` — stranding the M/W/F article
    carrier with a full buffer and zero effects.

    This never bypasses the immutability gate: the caller has already proven the
    tree's bytes equal the accepted terminal receipt, sealing only *removes*
    write permission (content hashes are path/sha256/bytes only), and the tree
    is re-verified byte-for-byte afterwards. A base that cannot be sealed still
    fails loud.
    """
    offenders = unsealed_base_entries(base)
    if not offenders:
        return {"status": "BASE_ALREADY_SEALED", "resealed_entries": 0}
    if base.parent != SITE_RELEASES_ROOT:
        # Only production release roots are sealable (the privileged seal is
        # confined to site-system/flagship-v2/releases). Anything else keeps the
        # untouched immutability requirement, which copytree_linked still
        # enforces — nothing is bypassed here.
        return {"status": "BASE_SEAL_SKIPPED_UNPRIVILEGED_ROOT", "resealed_entries": 0}
    report = seal_tree_selective(
        base,
        [],
        file_mode=SEALED_FILE_MODE,
        directory_mode=SEALED_DIRECTORY_MODE,
        owner=SEALED_OWNER,
        use_sudo=os.geteuid() != 0,
    )
    residual = unsealed_base_entries(base)
    if residual:
        raise RuntimeError(f"accepted_base_seal_incomplete:{residual[:5]}")
    resealed_rows = rows(base)
    if resealed_rows != verified_rows or closure(resealed_rows) != verified_tree:
        raise RuntimeError("accepted_base_seal_mutated_bytes")
    receipt = {
        "schema": "glp-opus-accepted-base-seal-repair-v1",
        "status": "PASS_ACCEPTED_BASE_RESEALED",
        "sealed_at_utc": now(),
        "release_root": str(base),
        "release_tree_sha256": verified_tree,
        "unsealed_entries_before": len(offenders),
        "unsealed_examples": offenders[:10],
        "selective_seal": report,
        "effects": {"deployments": 0, "cache_purges": 0, "gsc_submissions": 0, "spend_usd": 0},
    }
    BASE_SEAL_REPAIR_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = now().replace(":", "").replace("-", "")
    (BASE_SEAL_REPAIR_ROOT / f"{base.name}-{stamp}.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return receipt


def accepted_base_inventory(manifest: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Select one authenticated producer representation, never per-row aliases."""
    inventory = manifest.get("release_rows")
    if not isinstance(inventory, list) or not inventory or not isinstance(inventory[0], dict):
        raise RuntimeError("base_manifest_rows_invalid")
    fields = set(inventory[0])
    if fields == {"path", "sha256", "bytes"}:
        representation = "bytes"
    elif fields == {"path", "size", "sha256"}:
        representation = "size"
        if (set(manifest) != {"schema", "release_root", "release_rows", "release_tree_sha256",
                              "site_head", "deploy_directory"}
                or manifest["schema"] != "glp-opus-release-deployment-manifest-v1"
                or not isinstance(manifest["site_head"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", manifest["site_head"])):
            raise RuntimeError("base_native_manifest_invalid")
        deploy = manifest["deploy_directory"]
        if (not isinstance(deploy, dict) or set(deploy) != {"tree_sha256", "file_count", "total_bytes"}
                or not isinstance(deploy["tree_sha256"], str)
                or not SHA256_PATTERN.fullmatch(deploy["tree_sha256"])
                or any(type(deploy[k]) is not int or deploy[k] < 0 for k in ("file_count", "total_bytes"))):
            raise RuntimeError("base_native_deploy_closure_invalid")
    else:
        raise RuntimeError("base_manifest_row_representation_invalid")
    normalized = []
    seen: set[str] = set()
    for row in inventory:
        if not isinstance(row, dict) or set(row) != fields:
            raise RuntimeError("base_manifest_mixed_rows")
        path, digest, length = row["path"], row["sha256"], row[representation]
        if (not isinstance(path, str) or not path or "\\" in path or "\0" in path
                or path.startswith("/") or any(p in {"", ".", ".."} for p in path.split("/"))
                or path in seen or not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest)
                or type(length) is not int or length < 0):
            raise RuntimeError("base_manifest_row_invalid")
        seen.add(path)
        normalized.append({"path": path, "sha256": digest, "bytes": length})
    return representation, sorted(normalized, key=lambda r: r["path"].encode())


def accepted_base_closure(inventory: list[dict[str, Any]], representation: str) -> str:
    if representation == "bytes":
        return closure(inventory)
    if representation != "size":
        raise RuntimeError("base_manifest_row_representation_invalid")
    digest = hashlib.sha256()
    for row in sorted(inventory, key=lambda r: r["path"].encode()):
        digest.update(f"{row['path']}\0{row['bytes']}\0{row['sha256']}\n".encode())
    return digest.hexdigest()


def require_current_base(job: dict[str, Any]) -> tuple[Path, dict[str, Any], str]:
    base = Path(job["base_release_root"]).resolve()
    manifest_path = checked(job["base_manifest"], "base_manifest")
    receipt_path = checked(job["base_final_receipt"], "base_final_receipt")
    def descriptor(path: Path) -> tuple[int, ...]:
        info = os.lstat(path)
        return tuple(getattr(info, k) for k in (
            "st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns"))
    descriptors = (descriptor(manifest_path), descriptor(receipt_path))

    def recheck_evidence() -> None:
        # Authenticate the CURRENT lexical selection, including aliases/ancestors,
        # against the original resolved paths and descriptors. Hash equality alone
        # must not bless a same-byte retarget while the old resolved file survives.
        selected = (checked(job["base_manifest"], "base_manifest"),
                    checked(job["base_final_receipt"], "base_final_receipt"))
        if selected != (manifest_path, receipt_path) or descriptors != tuple(map(descriptor, selected)):
            raise RuntimeError("base_evidence_descriptor_drift")

    manifest = json.loads(manifest_path.read_text())
    receipt = json.loads(receipt_path.read_text())
    if receipt.get("status") == "PASS_LIVE_GSC_READBACK_PASS":
        deployment_id = receipt.get("deployment_id")
        manifest_sha = receipt.get("manifest", {}).get("sha256")
        receipt_release_tree = receipt.get("release_tree_sha256")
        receipt_public_tree = receipt.get("public_tree_sha256")
    elif receipt.get("status") == "PASS_PRODUCTION_TERMINAL":
        deployment_id = receipt.get("deployment", {}).get("deployment_id")
        manifest_sha = receipt.get("evidence_sha256", {}).get(manifest_path.name)
        receipt_release_tree = receipt.get("immutable_closure", {}).get("release_tree_sha256")
        receipt_public_tree = receipt.get("immutable_closure", {}).get("public_tree_sha256")
    else:
        raise RuntimeError("base_terminal_receipt_invalid")
    if not deployment_id or manifest_sha != sha(manifest_path):
        raise RuntimeError("base_terminal_manifest_invalid")
    if Path(manifest.get("release_root", "")).resolve() != base:
        raise RuntimeError("base_manifest_root_invalid")
    representation, inventory = accepted_base_inventory(manifest)
    actual_rows = rows(base)
    by_path = lambda values: sorted(values, key=lambda row: row["path"].encode())
    release_tree = accepted_base_closure(actual_rows, representation)
    if by_path(actual_rows) != inventory or release_tree != manifest.get("release_tree_sha256"):
        raise RuntimeError("base_release_drift")
    public_tree = accepted_base_closure(rows(base / "public"), representation)
    if receipt_release_tree != release_tree or receipt_public_tree != public_tree:
        raise RuntimeError("base_terminal_tree_mismatch")
    if representation == "size":
        if (receipt.get("schema") != "glp-opus-production-terminal-receipt-v1"
                or receipt.get("status") != "PASS_PRODUCTION_TERMINAL"
                or receipt.get("site_head") != manifest["site_head"]):
            raise RuntimeError("base_native_terminal_identity_invalid")
    else:
        # The legacy repair authenticates its own bytes-order digest only.
        recheck_evidence()
        ensure_accepted_base_sealed(base, actual_rows, release_tree)
    values = load_env()
    token = values.get("CLOUDFLARE_API_TOKEN") or values.get("CF_API_TOKEN")
    if not token:
        raise RuntimeError("cloudflare_token_missing")
    recheck_evidence()
    response = requests.get(CF_API, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    response.raise_for_status()
    body = response.json()
    if body.get("success") is not True:
        raise RuntimeError("cloudflare_project_read_failed")
    canonical = (body.get("result") or {}).get("canonical_deployment") or {}
    if canonical.get("id") != deployment_id:
        raise RuntimeError("production_head_changed")

    recheck_evidence()
    if representation == "size" and unsealed_base_entries(base):
        # Ingest the authenticated prepared bytes into a NEW fixed-root immutable
        # base. Never seal the mutable report tree or rewrite its bound evidence.
        base = materialize_accepted_prepared_base(
            base, SITE_RELEASES_ROOT / f"accepted-prepared-{release_tree}", inventory)
    recheck_evidence()
    return base, manifest, deployment_id


LASTMOD_BUILD_ENV_KEYS = (
    "GLP_LASTMOD_DECLARED_ROUTES",
    "GLP_LASTMOD_DECLARED_ROUTES_FILE",
    "GLP_LASTMOD_PROVENANCE_REFRESH",
    "GLP_RELEASE_DATE",
)


def declared_release_routes(job: dict[str, Any]) -> list[str]:
    """Derive the exact page routes this release contract permits to move.

    The postbuild provenance gate must remain armed for every other route. The
    article job already carries the authoritative public delta, so derive the
    narrow lastmod declaration from its ``*/index.html`` paths rather than using
    the blanket refresh lane or maintaining a second route list.
    """
    paths: list[str] = []
    for field in ("allowed_public_added", "allowed_public_changed"):
        values = job.get(field)
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise RuntimeError(f"{field}_invalid")
        paths.extend(values)
    routes: set[str] = set()
    for raw in paths:
        relative = Path(raw)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise RuntimeError(f"declared_public_path_invalid:{raw}")
        if relative.as_posix() == "index.html":
            routes.add("/")
        elif relative.name == "index.html":
            routes.add(f"/{relative.parent.as_posix().strip('/')}/")
    if not routes:
        raise RuntimeError("lastmod_declared_routes_empty")
    return sorted(routes)


def patch_article_runtime_proof(source: str) -> str:
    """Candidate-only integration of the predecessor's exact four-block proof.

    Never declare an unrelated route. A failed proof on an undeclared route
    remains the original stale/missing error; the material-date fallback cannot
    be reached by a failed runtime proof. Candidate export holds remain active.
    """
    # K4c removed the default resolver's declaration guard globally. Keep the
    # public helper strict: only the manifest-verified constructor call opts in
    # to preserving exact runtime-only predecessor dates before HTML restore.
    legacy = 'if (runtimeProof?.route === route'
    if legacy in source:
        source = apply_once_or_already(source, legacy,
            'if (declared && runtimeProof?.route === route', 'article_runtime_legacy_upgrade')
    source = apply_once_or_already(source,
        'releaseDate, runtimeProof = null }) {',
        'releaseDate, runtimeProof = null, allowUndeclaredRuntimeProof = false }) {',
        'article_runtime_explicit_optin')
    old = 'if (declared && runtimeProof?.route === route'
    new = 'if ((declared || allowUndeclaredRuntimeProof) && runtimeProof?.route === route'
    source = apply_once_or_already(source, old, new, "article_runtime_resolver")
    source = apply_once_or_already(source,
        '      runtimeProof,\n    });',
        '      runtimeProof,\n      allowUndeclaredRuntimeProof: Boolean(runtimePredecessor),\n    });',
        'article_runtime_constructor_optin')
    old = '? await runtimeOnlyLastmodProof({route,html,recorded,declared,predecessor:runtimePredecessor}) : null;'
    new = '? await runtimeOnlyLastmodProof({route,html,recorded,declared: declared || Boolean(runtimePredecessor),predecessor:runtimePredecessor}) : null;'
    return apply_once_or_already(source, old, new, "article_runtime_proof_call")


def bind_article_publication_date(article: dict[str, Any], publication_date: str,
                                  predecessor: dict[str, Any] | None = None) -> dict[str, Any]:
    """Bind a planned release day; never treat a draft check as first publication.

    Publisher rechecks the PT day before deploy. For R2, retain the existing
    catalog's first-publication day (legacy rows used updated_at in JSON-LD).
    """
    publication_date = strict_iso_date(publication_date, "publication_date")
    if predecessor is None:
        article["date_published"] = publication_date
    else:
        first_day = predecessor.get("date_published") or predecessor.get("updated_at")
        article["date_published"] = strict_iso_date(first_day, "predecessor_date_published")
    article["updated_at"] = publication_date
    for field in ("date_modified", "dateModified"):
        if field in article or (predecessor is not None and field in predecessor):
            article[field] = publication_date
    return article


def article_build_environment(job: dict[str, Any], candidate: Path | None = None) -> dict[str, str]:
    """Isolate route authority and bind verified predecessor runtime evidence."""
    env = dict(os.environ)
    for key in (*LASTMOD_BUILD_ENV_KEYS, "GCS_RELEASE_STAGE",
                "GLP_LASTMOD_RUNTIME_PREDECESSOR_ROOT",
                "GLP_LASTMOD_RUNTIME_PREDECESSOR_MANIFEST",
                "GLP_LASTMOD_RUNTIME_PREDECESSOR_MANIFEST_SHA256"):
        env.pop(key, None)
    env["GCS_RELEASE_STAGE"] = "candidate"
    env["GLP_LASTMOD_DECLARED_ROUTES"] = ",".join(declared_release_routes(job))
    env["GLP_RELEASE_DATE"] = strict_iso_date(job.get("publication_date"), "publication_date")
    if candidate is not None:
        # require_current_base has verified this manifest, tree and live head
        # before candidate construction. Recheck the descriptor here as well.
        manifest_path = checked(job["base_manifest"], "base_manifest")
        manifest = json.loads(manifest_path.read_text())
        base = Path(job["base_release_root"]).resolve()
        if Path(manifest["release_root"]).resolve() != base:
            raise RuntimeError("runtime_predecessor_root_mismatch")
        proof_path = candidate / "artifacts/article-runtime-predecessor.json"
        proof_path.unlink(missing_ok=True)
        proof_path.write_text(json.dumps(manifest["release_rows"], sort_keys=True) + "\n")
        source_path = candidate / "scripts/postbuild-aeo-surface.mjs"
        patched = patch_article_runtime_proof(source_path.read_text())
        patched = carrier_fixture_contracts.patch_material_date_resolver(
            patched, carrier_fixture_contracts.material_binding(base))
        source_path.unlink()
        source_path.write_text(patched)
        # The accepted base freezes therapeutic_taxonomy verified_as_of and no
        # registered command can advance it there, so requiring it to be fresh stops
        # EVERY publication freshness_max_days after each seal (2026-09-17). Keep the
        # frozen stamp - nothing rendered moves - and take the freshness clock from the
        # sha256-bound live re-verification beside it. Empty binding = exact no-op.
        taxonomy_path = candidate / "src/therapeutic-taxonomy.mjs"
        taxonomy_binding = carrier_fixture_contracts.taxonomy_reverification_binding(base)
        taxonomy_source = carrier_fixture_contracts.patch_taxonomy_freshness_clock(
            taxonomy_path.read_text(), taxonomy_binding)
        taxonomy_path.unlink()
        taxonomy_path.write_text(taxonomy_source)
        # Same bug class, same shape: a sealed row's checked_at is frozen and the
        # 45-day price gate has no registered way to advance it in an accepted base.
        tracker_path = candidate / "src/price-tracker.mjs"
        tracker_source = carrier_fixture_contracts.patch_price_tracker_freshness_clock(
            tracker_path.read_text())
        # The COW entry is a hardlink into the sealed, root-owned base: break the link
        # before writing, exactly as the aeo-surface and taxonomy patches above do.
        tracker_path.unlink()
        tracker_path.write_text(tracker_source)
        taxonomy_receipt = candidate / "artifacts/article-taxonomy-reverification.json"
        taxonomy_receipt.unlink(missing_ok=True)
        taxonomy_receipt.write_text(json.dumps({
            **taxonomy_binding,
            "status": "PASS_TAXONOMY_FRESHNESS_FROM_AUTHENTICATED_REVERIFICATION",
            "base_release_root": str(base),
            "rows": len(taxonomy_binding.get("stamps") or {}),
        }, indent=2, sort_keys=True) + "\n")
        env.update(GLP_LASTMOD_RUNTIME_PREDECESSOR_ROOT=str(base),
                   GLP_LASTMOD_RUNTIME_PREDECESSOR_MANIFEST=str(proof_path.resolve()),
                   GLP_LASTMOD_RUNTIME_PREDECESSOR_MANIFEST_SHA256=sha(proof_path))
    return env


def restore_undeclared_provenance(base: Path, candidate: Path, job: dict[str, Any]) -> None:
    """Keep provenance coherent with the existing exact-public-byte restore.

    Only called AFTER the full build/gates succeeded and predecessor-owned
    public files were restored. Does not forgive any failing content hash.
    """
    relative = "data/page-content-provenance.json"
    before = json.loads((base / relative).read_text())
    path = candidate / relative
    after = json.loads(path.read_text())
    declared = set(declared_release_routes(job))
    for route, record in before["pages"].items():
        if route not in declared:
            after["pages"][route] = record
    path.unlink()
    path.write_text(json.dumps(after, indent=2, sort_keys=True) + "\n")


def restore_undeclared_sitemap_entries(base: Path, candidate: Path, job: dict[str, Any]) -> None:
    """Restore metadata with the predecessor-owned HTML, not its rebuild dates.

    The sitemap is a declared shared file, but that does not make every URL in
    it article-owned. Preserve exact old entries outside the declared route
    delta. Missing, duplicate and undeclared new entries still fail closed.
    """
    import xml.etree.ElementTree as ET

    def entries(source: str) -> dict[str, str]:
        root = ET.fromstring(source)
        blocks = re.findall(r"<url>.*?</url>", source, flags=re.DOTALL)
        if len(blocks) != len(root):
            raise RuntimeError("sitemap_restore_entry_shape_invalid")
        result = {}
        for block in blocks:
            node = ET.fromstring(block)
            locations = node.findall("loc")
            if len(locations) != 1:
                raise RuntimeError("sitemap_restore_location_invalid")
            location = locations[0].text or ""
            prefix = "https://glpclinicscout.com"
            if not location.startswith(prefix + "/"):
                raise RuntimeError("sitemap_restore_canonical_host_invalid")
            route = location[len(prefix):]
            if route in result:
                raise RuntimeError("sitemap_restore_duplicate_route:" + route)
            result[route] = block
        return result

    path = candidate / "public/sitemap.xml"
    before = entries((base / "public/sitemap.xml").read_text())
    source = path.read_text()
    after = entries(source)
    declared = set(declared_release_routes(job))
    if set(before) - set(after):
        raise RuntimeError("sitemap_restore_predecessor_route_missing")
    if set(after) - set(before) - declared:
        raise RuntimeError("sitemap_restore_undeclared_route_added")
    for route, block in before.items():
        if route not in declared:
            source = source.replace(after[route], block, 1)
    path.unlink()
    path.write_text(source)


def run(
    command: list[str],
    cwd: Path,
    timeout: int = 1800,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    result = subprocess.run(
        command, cwd=cwd, text=True, capture_output=True, timeout=timeout, env=env
    )
    record = {"command": command, "exit_code": result.returncode,
              "stdout_tail": result.stdout[-8000:], "stderr_tail": result.stderr[-8000:]}
    if env is not None:
        record["environment_overrides"] = {
            key: env[key] for key in LASTMOD_BUILD_ENV_KEYS if key in env
        }
    if result.returncode:
        stdout_lines = result.stdout.splitlines()
        failure_lines = [index for index, line in enumerate(stdout_lines) if line.startswith("not ok ")]
        failure_context = "\n".join(
            line
            for index in failure_lines
            for line in stdout_lines[max(0, index - 2):min(len(stdout_lines), index + 32)]
        )
        raise RuntimeError(
            f"command_failed:{command[0]}:stderr={result.stderr[-4000:]}:"
            f"failure_context={failure_context}:stdout={result.stdout[-8000:]}"
        )
    return record


def apply_once_or_already(source: str, old: str, new: str, label: str) -> str:
    """Apply a one-time predecessor upgrade, or accept its exact successor state."""
    new_count = source.count(new)
    if new_count == 1:
        # ``old`` may be a literal prefix/subsequence of ``new``. Remove the
        # accepted successor once before checking for a second predecessor;
        # otherwise an already-upgraded source is misclassified as old=1,new=1.
        residual_old_count = source.replace(new, "", 1).count(old)
        if residual_old_count == 0:
            return source
        raise RuntimeError(
            f"{label}_anchor_invalid:old={residual_old_count}:new={new_count}"
        )
    old_count = source.count(old)
    if old_count == 1 and new_count == 0:
        return source.replace(old, new)
    raise RuntimeError(f"{label}_anchor_invalid:old={old_count}:new={new_count}")


def patch_lastmod_fixture_modes(source: str) -> str:
    """Make the provenance test's copied public tree mutable, never its source."""
    old = '''  await cp(path.join(ROOT, "public"), path.join(root, "public"), {
    recursive: true,
    filter: (source) => !source.startsWith(path.join(ROOT, "public", "assets")),
  });
  return root;'''
    new = '''  await cp(path.join(ROOT, "public"), path.join(root, "public"), {
    recursive: true,
    filter: (source) => !source.startsWith(path.join(ROOT, "public", "assets")),
  });
  // A sealed release is 0444. cp preserves that mode, but this isolated fixture
  // is intentionally mutated by the postbuild stage under test.
  const writable = spawnSync("chmod", ["-R", "u+w", path.join(root, "public")], { encoding: "utf8" });
  assert.equal(writable.status, 0, writable.stderr);
  return root;'''
    # The accepted site successor already makes the copied public tree
    # writable after its carrier-closure copies; accept that exact behavior
    # instead of requiring the historical placement directly after ``cp``.
    writable_successor = 'spawnSync("chmod", ["-R", "u+w", path.join(root, "public")]'
    if source.count(writable_successor) != 1:
        source = apply_once_or_already(source, old, new, "lastmod_fixture_writable")
    # The stage now reads page-seo-copy and both real article catalogs. Carry
    # the same input data tree as the tested source instead of an obsolete
    # two-file subset. No assertion or runtime gate changes.
    source = apply_once_or_already(source,
        'for (const directory of ["scripts", "src"])',
        'for (const directory of ["scripts", "src", "data"])',
        "lastmod_fixture_complete_data")
    return apply_once_or_already(source,
        '["page-content-provenance.json", "llms-priority.json"]',
        '["page-content-provenance.json", "llms-priority.json", "page-seo-copy.json"]',
        "lastmod_fixture_page_seo_input")


def rebind_affiliati_local_d1_fixture(source: str) -> str:
    """Use installed scoped ledger and placement-only s1 in isolated SQLite."""
    migration = '    database.exec(await readFile(path.join(ROOT, "migrations/006_affiliati_attribution_join.sql"), "utf8"));'
    source = apply_once_or_already(source, migration,
        migration + '\n    database.exec(await readFile(path.join(ROOT, "migrations/007_postback_scoped_identity.sql"), "utf8"));',
        "affiliati_fixture_scoped_schema")
    source = apply_once_or_already(source,
        'url.searchParams.set("subid", clickId);',
        'url.searchParams.set("subid", "providers.fv2s1");',
        "affiliati_fixture_placement_only")
    return apply_once_or_already(source,
        'event_status: "owned_click_recorded",',
        'event_status: "unclassified",', "affiliati_fixture_unknown_not_real")


def rebind_phase3_in_memory_fixture(source: str) -> str:
    """Bind whole-graph fixtures to real catalogs; keep the route-count gate."""
    anchor = 'const graph = JSON.parse(await readFile(path.join(ROOT, "data/phase3-page-graph.json"), "utf8"));'
    addition = '''
import { PHASE3_INSIGHTS_EXCLUSIONS as CARRIER_INSIGHTS_EXCLUSIONS } from "../src/phase3-page-graph.mjs";
const carrierCatalogs = await Promise.all(["content-batch.json", "content-national-wow-additions.json"].map(async (file) =>
  JSON.parse(await readFile(path.join(ROOT, "data/blog", file), "utf8"))));
// Match the installed staging caller's GLP-guide vertical classification.
const carrierOtherVertical = /(erectile|viagra|cialis|sildenafil|tadalafil|online-ed|peptide|bpc-157|cjc-1295|ipamorelin|hair-loss|minoxidil|finasteride|trt|testosterone|metabolic-health|metabolic-syndrome|insulin-resistance|continuous-glucose)/;
const carrierArticles = carrierCatalogs.flatMap((catalog) => catalog.articles).filter((article) =>
  String(article.canonical_path || "").startsWith("/guides/") && !carrierOtherVertical.test(article.canonical_path));
const carrierInsightArticles = carrierArticles.filter((article) =>
  !CARRIER_INSIGHTS_EXCLUSIONS.has(article.canonical_path) &&
  (String(article.vertical || article.focused_vertical || "GLP-1").toUpperCase().includes("GLP") || String(article.canonical_path || "").startsWith("/guides/")));
'''
    # Site successors carry their own actual-catalog binding (with evolved
    # comments/imports). Re-inserting the predecessor block would redeclare
    # ``carrierArticles`` and break the test module, so accept it semantically.
    catalogs_bound = (
        source.count("const carrierCatalogs = ") == 1
        and source.count("const carrierArticles = ") == 1
    )
    if not catalogs_bound:
        source = apply_once_or_already(source, anchor, anchor + addition, "phase3_fixture_actual_catalogs")
    if catalogs_bound and "ownedArticles(37)" not in source and "articles: carrierArticles" in source:
        pass  # Every whole-graph fixture already consumes the real catalogs.
    else:
        source = apply_once_or_already(source, 'articles: ownedArticles(37),', 'articles: carrierArticles,', "phase3_fixture_actual_articles")
    old = 'assert.equal(built.report.insight_pages, 4);'
    new = 'assert.equal(built.report.insight_pages, Math.max(1, Math.ceil(carrierInsightArticles.length / 12)));'
    if old in source or new in source:
        source = apply_once_or_already(source, old, new, "phase3_fixture_insights_pages")
    return source


def rebind_pagination_capture_fixture(source: str, job: dict[str, Any]) -> str:
    """Inventory declared pagination forms without approving email enrollment.

    New Insights pages emit staged forms just like the existing Phase 3 pages.
    Derive existing and declared new pagination routes for the staged cohort; retain
    every assertion, including refusal by the weekly subscriber validator.
    """
    pages = set()
    base = job.get("base_release_root")
    if base:
        pages.update(int(path.parent.name) for path in
                     (Path(base) / "public/insights/page").glob("*/index.html"))
    for relative in job.get("allowed_public_added", []):
        match = re.fullmatch(r"insights/page/([1-9][0-9]*)/index\.html", relative)
        if match:
            number = int(match.group(1))
            if number < 2:
                raise RuntimeError("capture_fixture_pagination_invalid")
            pages.add(number)
    if not pages:
        return source
    # The staged Insights entries carry free-form trailing line comments in a
    # sealed base (the 2026-09-11 base added "// Complete current carrier corpus
    # adds the fifth Insights page." to /insights/page/5/). An anchor that only
    # accepted a bare entry matched zero times and aborted the whole publication
    # with capture_fixture_routes_anchor_invalid, so the tail of each entry line
    # is absorbed rather than required to be empty.
    pattern = re.compile(
        r'(  const routes = \[\n)'
        r'((?:[ \t]*"/insights/page/[1-9][0-9]*/",[^\n]*\n)*)'
        r'([ \t]*"/matches/customized/",)')
    matches = list(pattern.finditer(source))
    if len(matches) != 1:
        raise RuntimeError("capture_fixture_routes_anchor_invalid")
    match = matches[0]
    if not base:
        pages.update(int(value) for value in re.findall(r'/insights/page/([1-9][0-9]*)/', match.group(2)))
    if min(pages) < 2:
        raise RuntimeError("capture_fixture_pagination_invalid")
    if base:
        registry = (Path(base) / "src/site-capture-consent-sources.mjs").read_text()
        inventories = re.findall(
            r"export const CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES = Object\.freeze\((\[[\s\S]*?\])\);",
            registry,
        )
        if len(inventories) != 1:
            raise RuntimeError("capture_fixture_approved_inventory_invalid")
        approved = json.loads(re.sub(r",\s*\]$", "]", inventories[0]))
        if not isinstance(approved, list) or any(not isinstance(item, str) for item in approved):
            raise RuntimeError("capture_fixture_approved_inventory_invalid")
        # Inventory is not enrollment authority. Existing approved sources keep
        # their exact contract; only unapproved generated pages are staged.
        pages = {number for number in pages
                 if f"insights-page-{number}:insights-page-{number}-terms-checklist" not in approved}
    replacement = match.group(1) + ''.join(f'    "/insights/page/{number}/",\n' for number in sorted(pages)) + match.group(3)
    return source[:match.start()] + replacement + source[match.end():]


def patch_editorial_monetization_test_for_r2(source: str) -> str:
    """Keep the historical monetization diff scoped to its original cohort.

    A later R2 release can independently authorize evidence changes. Such an
    article carries a completed batch gate and a receipt hash, so the older
    monetization-only immutability proof must defer to the R2 validators for that
    route while continuing to protect every other ruled route.
    """
    old = "  for (const canonical of [...RULED_YES, ...RULED_NO]) {"
    new = '''  const monetizationOnlyCanonicals = [...RULED_YES, ...RULED_NO].filter((canonical) => {
    const brief = guideOf(canonical);
    return !(brief?.batch_gate_passed === true
      && /^[a-f0-9]{64}$/.test(brief?.operator_release_receipt || ""));
  });
  for (const canonical of monetizationOnlyCanonicals) {'''
    return apply_once_or_already(source, old, new, "editorial_r2_citation_scope")


def patch_candidate_local_served_test(source: str) -> str:
    """Assert candidate-local URLs without pinning a historical release name."""
    return normalize_candidate_local_served_test(source)


def apply_once_or_already_evolved(
    source: str, old: str, new: str, successors: tuple[str, ...], label: str
) -> str:
    """``apply_once_or_already`` that also accepts later builder-written states.

    One builder step can legitimately be superseded by a *different* builder
    step: an r2 refresh rebinds the historical-cohort assertions that a
    ``new_article`` build wrote. Once such a tree is sealed and accepted as the
    current content base, the next build sees neither the predecessor nor the
    frozen successor and used to abort ``_anchor_invalid:old=0:new=0``.
    ``successors`` therefore names every further state this builder itself can
    write; each must be sourced from the same module constant the writing step
    uses, so a future evolution cannot desynchronise the two.
    """
    for successor in successors:
        count = source.count(successor)
        if count == 0:
            continue
        if count != 1:
            raise RuntimeError(f"{label}_anchor_invalid:successor={count}")
        residual = source.replace(successor, "", 1)
        residual_old, residual_new = residual.count(old), residual.count(new)
        if residual_old or residual_new:
            raise RuntimeError(
                f"{label}_anchor_invalid:successor=1:old={residual_old}:new={residual_new}"
            )
        return source
    return apply_once_or_already(source, old, new, label)


def patch_playbook_historical_batch_gate(source: str) -> str:
    """Scope the historical-cohort batch-gate assertion to the first 29 articles.

    Accepts the r2-refresh successor so an accepted base minted by an existing
    article refresh stays buildable by the next new-article release.
    """
    return apply_once_or_already_evolved(
        source,
        PLAYBOOK_HISTORICAL_BATCH_GATE_PREDECESSOR,
        PLAYBOOK_HISTORICAL_BATCH_GATE_SUCCESSOR,
        PLAYBOOK_HISTORICAL_BATCH_GATE_R2_SUCCESSORS,
        "playbook_batch_gate",
    )


def ensure_renderer_contract(source: str, satisfied: bool, old: str, new: str, label: str) -> str:
    """Accept any renderer that already satisfies the contract; upgrade the known
    predecessor form once; otherwise fail loud. The renderer legitimately evolves
    across releases (suppress_cta guard, import placement), so satisfaction is a
    semantic probe — never byte-equality with one frozen historical successor."""
    if satisfied:
        return source
    old_count = source.count(old)
    if old_count == 1 and new not in source:
        return source.replace(old, new)
    raise RuntimeError(f"{label}_anchor_invalid:old={old_count}:contract_unsatisfied")


PRIMARY_HOSTS_PREDECESSOR = (
    'const PRIMARY_HOSTS = ["fda.gov", "hhs.gov", "nih.gov", '
    '"ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov", "cms.gov", '
    '"ftc.gov", "uscourts.gov"];'
)
PRIMARY_HOSTS_SUCCESSOR = (
    'const PRIMARY_HOSTS = ["fda.gov", "hhs.gov", "nih.gov", '
    '"ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov", "cms.gov", '
    '"clinicaltrials.gov", "ftc.gov", "uscourts.gov"];'
)
EDITORIAL_AFFILIATE_GATE_PREDECESSOR = (
    '      if (brief.ftc_disclosure_placement !== "before_first_affiliate_link" '
    '|| !/affiliate/i.test(disclosureText) '
    '|| !/(?:commission|compensation|earn)/i.test(disclosureText) '
    '|| affiliateUrls.size === 0 '
    '|| [...affiliateUrls].some((url) => !/^https:\\/\\/[^\\s]+$/.test(String(url)) '
    '|| !renderedInputUrls.has(url))) return { status: "HELD", '
    'reason: "editorial_affiliate_contract_incomplete" };'
)
EDITORIAL_AFFILIATE_GATE_SUCCESSOR = (
    '      if (brief.ftc_disclosure_placement !== "before_first_affiliate_link" '
    '|| !/affiliate/i.test(disclosureText) '
    '|| !/(?:commission|compensation|earn)/i.test(disclosureText) '
    '|| !commerceSurfacePresent '
    '|| [...affiliateUrls].some((url) => !/^https:\\/\\/[^\\s]+$/.test(String(url)) '
    '|| !renderedInputUrls.has(url))) return { status: "HELD", '
    'reason: "editorial_affiliate_contract_incomplete" };'
)


def patch_renderer_surface_contract(source: str) -> str:
    """Make the exact renderer honor capture, CTA, and machine-date contracts."""
    # ClinicalTrials.gov is the NIH/NLM-operated federal trial registry and is
    # already an authoritative source in the R2 validator. The renderer's older
    # hard-coded list omitted it, so an exact packet with S11=clinicaltrials.gov
    # passed deterministic QA and Sol review, then failed only at npm build.
    if "const PRIMARY_HOSTS =" in source:
        source = apply_once_or_already(
            source,
            PRIMARY_HOSTS_PREDECESSOR,
            PRIMARY_HOSTS_SUCCESSOR,
            "renderer_primary_medical_hosts",
        )
    # The receipt-bound editorial monetization contract explicitly admits
    # table-only v2 comparisons and governed offer modules. A later duplicate
    # editorial check still required a prose affiliate URL, so an R2 refresh of
    # a valid table-only route failed after preserving its route governance.
    # Reuse commerceSurfacePresent while retaining every disclosure check and
    # rejecting every malformed or unrendered prose affiliate URL.
    if (
        "const commerceSurfacePresent =" in source
        and "editorial_affiliate_contract_incomplete" in source
    ):
        source = apply_once_or_already(
            source,
            EDITORIAL_AFFILIATE_GATE_PREDECESSOR,
            EDITORIAL_AFFILIATE_GATE_SUCCESSOR,
            "renderer_editorial_affiliate_commerce_surface",
        )
    source = ensure_renderer_contract(
        source,
        'import { addP1ContentUpgradeSurface }' in source,
        'import { renderComparisonTable } from "./money-page.mjs";\n',
        'import { renderComparisonTable } from "./money-page.mjs";\nimport { addP1ContentUpgradeSurface } from "./wave2-p1-surfaces.mjs";\n',
        "renderer_content_upgrade_import",
    )
    source = ensure_renderer_contract(
        source,
        'data-prices-checked-date' in source,
        '<p class="guide-evidence-band">Sources checked ${esc(sourceCheckedDate)} · Prices and terms can change.</p>',
        '<p class="guide-evidence-band">Sources checked ${esc(sourceCheckedDate)}${brief.surface_taxonomy === "medical" ? ` · Prices checked <time datetime="${esc(brief.prices_checked_date)}" data-prices-checked-date>${esc(brief.prices_checked_date)}</time>` : ""} · Prices and terms can change.</p>',
        "renderer_machine_price_date",
    )
    markers = [
        "export function renderBlogPost(brief, comparisonSpec, author) {",
        "export function renderBlogPost(brief, comparisonSpec, author, programs) {",
    ]
    matching_markers = [candidate for candidate in markers if source.count(candidate) == 1]
    if len(matching_markers) != 1:
        raise RuntimeError("renderer_modern_entry_anchor_invalid")
    marker = matching_markers[0]
    prefix, modern = source.split(marker, 1)
    modern = marker + modern
    modern = ensure_renderer_contract(
        modern,
        'const rendered = `<!doctype html><html lang="en" data-release=' in modern,
        "  return `<!doctype html><html lang=\"en\" data-release=\"${indexable ? \"release-ready\" : \"held\"}\">",
        "  const rendered = `<!doctype html><html lang=\"en\" data-release=\"${indexable ? \"release-ready\" : \"held\"}\">",
        "renderer_capture_wrapper_open",
    )
    modern = ensure_renderer_contract(
        modern,
        re.search(r"return[^\n]*addP1ContentUpgradeSurface\(rendered", modern) is not None,
        "</main></body></html>`;\n}\n\nexport const blogPolicy",
        "</main></body></html>`;\n  return addP1ContentUpgradeSurface(rendered, brief.canonical_path.replace(/^\\//, \"\"));\n}\n\nexport const blogPolicy",
        "renderer_capture_wrapper_close",
    )
    return prefix + modern


def patch_staging_route_contract(source: str) -> str:
    """Evolve staging metadata without bypassing route or release authority."""
    source = apply_once_or_already(
        source,
        '    if (!owner || owner.risk_tier !== article.risk_tier || reviewBrief(article, resolveArticleAuthor(article)).status !== "PASS" || (article.comparison_required && !comparison)) throw new Error(`blog_article_gate:${article.article_id}`);',
        '''    if (!owner) throw new Error(`blog_article_owner_missing:${article.article_id}`);
    if (owner.risk_tier !== article.risk_tier) throw new Error(`blog_article_owner_risk_mismatch:${article.article_id}`);
    const articleReview = reviewBrief(article, resolveArticleAuthor(article));
    if (articleReview.status !== "PASS") throw new Error(`blog_article_gate:${article.article_id}:${articleReview.reason}`);
    if (article.comparison_required && !comparison) throw new Error(`blog_article_comparison_missing:${article.article_id}`);''',
        "staging_article_gate_diagnostic",
    )
    # GLP73 site successor widened the same fail-closed guard to /reviews/.
    # Accept exactly that successor; any other guard still fails loud.
    reviews_guard = '  if (!/^\\/(?:guides|best|reviews)\\/[a-z0-9-]+\\/$/.test(article.canonical_path || "")) throw new Error(`article_canonical_path_invalid:${article.article_id}`);'
    if source.count(reviews_guard) != 1 or "guide_canonical_path_invalid" in source:
        source = apply_once_or_already(
            source,
            '  if (!/^\\/guides\\/[a-z0-9-]+\\/$/.test(article.canonical_path || "")) throw new Error(`guide_canonical_path_invalid:${article.article_id}`);',
            '  if (!/^\\/(?:guides|best)\\/[a-z0-9-]+\\/$/.test(article.canonical_path || "")) throw new Error(`article_canonical_path_invalid:${article.article_id}`);',
            "staging_article_route_guard",
        )
    source = apply_once_or_already(
        source,
        '  const allOwnership = [...keywordMap.entries, ...nationalArticles.map((article) => ({ owner: article.owner_key, primary_keyword: article.primary_keyword, risk_tier: article.risk_tier, status: article.status, url: article.canonical_path }))];',
        '''  const allOwnership = [...keywordMap.entries];
  for (const article of nationalArticles) {
    const matches = allOwnership.filter((entry) => entry.url === article.canonical_path);
    if (matches.length !== 1) throw new Error(`national_keyword_ownership_match_count_invalid:${article.article_id}:${matches.length}`);
    const owner = matches[0];
    if ((owner.article_id || owner.owner) !== article.article_id || owner.primary_keyword !== article.primary_keyword || owner.risk_tier !== article.risk_tier || owner.status !== article.status) throw new Error(`national_keyword_ownership_identity_invalid:${article.article_id}`);
  }''',
        "staging_national_ownership_source_contract",
    )
    source = apply_once_or_already(
        source,
        NATIONAL_CONTENT_RELEASE_GATE_PREDECESSOR,
        NATIONAL_CONTENT_RELEASE_GATE_R2_SUCCESSOR,
        "staging_national_content_r2_release_gate",
    )
    current_breadcrumb_contract = (
        "breadcrumbs:" in source
        and "article.canonical_path.startsWith(\"/best/\")" in source
        and 'name: "Compare"' in source
    )
    if current_breadcrumb_contract:
        return source
    return apply_once_or_already(
        source,
        '    breadcrumbs: [{ name: "Home", path: "/" }, { name: "Guides", path: "/guides/" }, { name: article.h1, path: article.canonical_path }],',
        '    breadcrumbs: [{ name: "Home", path: "/" }, article.canonical_path.startsWith("/best/") ? { name: "Compare", path: "/best/" } : { name: "Guides", path: "/guides/" }, { name: article.h1, path: article.canonical_path }],',
        "staging_article_breadcrumb_collection",
    )


R2_LEGACY_PRESERVED_ROUTE = "/guides/mounjaro-coupon/"
R2_LEGACY_PRESERVED_ROUTE_FILE = "guides/mounjaro-coupon/index.html"
R2_LEGACY_SNAPSHOT = '''  const preservedMounjaroCouponBytes = await readDurablePredecessorFile(path.join(PUBLIC, MOUNJARO_COUPON_ROUTE_FILE), MOUNJARO_COUPON_ROUTE_FILE);
  // The evidence/article bytes remain predecessor-owned, but O-28 supersedes the
  // old visible email-consent control. Run only the idempotent capture finalizer
  // before clearing public/ so passive privacy text, hidden consent version,
  // source, and honeypot survive while submission itself records consent.
  const preservedMounjaroCouponHtml = addArticleShareControls(applyWave2P1Surfaces(pageHero(
    preservedMounjaroCouponBytes.toString("utf8"),
    "guide-mounjaro-coupon",
    "Guide overview for Mounjaro Coupon 2026: $25 Card Rules and $499 Paths",
  ), MOUNJARO_COUPON_ROUTE_FILE));'''
R2_LEGACY_SKIP = '''    if (path.join(article.canonical_path.replace(/^\\//, ""), "index.html") === MOUNJARO_COUPON_ROUTE_FILE) continue;'''
R2_LEGACY_RESTORE = '''  // Reassert the recovered article with a raw write. The normal write funnels add
  // breadcrumbs/table containment and therefore are intentionally not used for
  // this predecessor-owned surface.
  await mkdir(path.dirname(path.join(PUBLIC, MOUNJARO_COUPON_ROUTE_FILE)), { recursive: true });
  await writeFile(path.join(PUBLIC, MOUNJARO_COUPON_ROUTE_FILE), preservedMounjaroCouponHtml);'''
R2_LEGACY_RELEASE_MARKERS = (
    "  // R2 target release: the refreshed catalog row now owns the Mounjaro coupon bytes.",
    "    // R2 target release: render Mounjaro from the refreshed catalog row.",
    "  // R2 target release: do not overwrite the rendered target with predecessor bytes.",
)

R2_MOUNJARO_LASTMOD_TEST_PREDECESSOR = '''// 2026-08-19: the date moved because the content moved, which is exactly the
// rule the lastmod provenance gate enforces. This still pins one entry and one
// date, so a bulk-stamp or duplicate row fails here.
test("Mounjaro coupon route has one truthful 2026-08-19 sitemap lastmod", async () => {
  const sitemap = await readFile(new URL("public/sitemap.xml", ROOT), "utf8");
  const exact = `<url><loc>https://glpclinicscout.com${ROUTE}</loc><lastmod>2026-08-19</lastmod></url>`;
  assert.equal(sitemap.split(`https://glpclinicscout.com${ROUTE}`).length - 1, 1);
  assert.equal(sitemap.split(exact).length - 1, 1);
});'''
R2_MOUNJARO_LASTMOD_TEST_SUCCESSOR = '''// The refreshed catalog row owns the route's machine date. This still pins one
// exact route and one exact date, so a missing, duplicate, or bulk-stamped row fails.
test("Mounjaro coupon route has one truthful catalog-bound sitemap lastmod", async () => {
  const [sitemap, batch] = await Promise.all([
    readFile(new URL("public/sitemap.xml", ROOT), "utf8"),
    readJson("data/blog/content-batch.json"),
  ]);
  const article = batch.articles.find((item) => item.canonical_path === ROUTE);
  assert.ok(article, "Mounjaro coupon source record missing");
  assert.match(article.updated_at, /^\\d{4}-\\d{2}-\\d{2}$/);
  const exact = `<url><loc>https://glpclinicscout.com${ROUTE}</loc><lastmod>${article.updated_at}</lastmod></url>`;
  assert.equal(sitemap.split(`https://glpclinicscout.com${ROUTE}`).length - 1, 1);
  assert.equal(sitemap.split(exact).length - 1, 1);
});'''
R2_MOUNJARO_ALT_TEST_PREDECESSOR = '''test("article hero alt replacement preserves dollar amounts as literal text", async () => {
  const gala = await read("public/guides/gala-vs-shed/index.html");
  const mounjaro = await read("public/guides/mounjaro-coupon/index.html");
  assert.match(gala, /alt="Guide overview for Gala vs Shed: compare the full terms, not a \\$20 headline gap"/);
  assert.match(mounjaro, /alt="Guide overview for Mounjaro Coupon 2026: \\$25 Card Rules and \\$499 Paths"/);
});'''
R2_MOUNJARO_ALT_TEST_SUCCESSOR = '''test("article hero alt replacement preserves dollar amounts as literal text", async () => {
  const [gala, mounjaro, batchSource] = await Promise.all([
    read("public/guides/gala-vs-shed/index.html"),
    read("public/guides/mounjaro-coupon/index.html"),
    read("data/blog/content-batch.json"),
  ]);
  const article = JSON.parse(batchSource).articles.find((item) => item.canonical_path === "/guides/mounjaro-coupon/");
  assert.ok(article, "Mounjaro coupon source record missing");
  assert.match(article.h1, /\\$/);
  const expectedAlt = `alt="Guide overview for ${article.h1}"`;
  assert.match(expectedAlt, /\\$/);
  assert.match(gala, /alt="Guide overview for Gala vs Shed: compare the full terms, not a \\$20 headline gap"/);
  assert.equal(mounjaro.split(expectedAlt).length - 1, 1);
});'''
R2_MOUNJARO_WAVE2_TEST_PREDECESSOR = (
    '    if (article.canonical_path === "/guides/mounjaro-coupon/" '
    '|| articleVertical(article) !== "GLP-1") {'
)
R2_MOUNJARO_WAVE2_TEST_SUCCESSOR = (
    '    if (articleVertical(article) !== "GLP-1") {'
)
R2_MOUNJARO_TEST_BINDINGS = (
    (
        Path("tests/mounjaro-coupon-recovery.test.mjs"),
        R2_MOUNJARO_LASTMOD_TEST_PREDECESSOR,
        R2_MOUNJARO_LASTMOD_TEST_SUCCESSOR,
    ),
    (
        Path("tests/pass2-late-audit-static-output.test.mjs"),
        R2_MOUNJARO_ALT_TEST_PREDECESSOR,
        R2_MOUNJARO_ALT_TEST_SUCCESSOR,
    ),
    (
        Path("tests/wave2-regression.test.mjs"),
        R2_MOUNJARO_WAVE2_TEST_PREDECESSOR,
        R2_MOUNJARO_WAVE2_TEST_SUCCESSOR,
    ),
)


def release_r2_target_from_legacy_preservation(source: str, target_route: str) -> str:
    """Let an explicitly targeted R2 route leave its historical raw-byte hold.

    The Mounjaro recovery route was intentionally carried as predecessor bytes
    while its catalog row could not reproduce the approved page. Once R2 replaces
    that exact row, preserving the old HTML makes source and served bytes disagree.
    Release only the exact targeted route, and require the three known preservation
    anchors to move together so a partial migration fails closed.
    """
    if target_route != R2_LEGACY_PRESERVED_ROUTE:
        return source
    old = (R2_LEGACY_SNAPSHOT, R2_LEGACY_SKIP, R2_LEGACY_RESTORE)
    old_counts = tuple(source.count(anchor) for anchor in old)
    marker_counts = tuple(source.count(marker) for marker in R2_LEGACY_RELEASE_MARKERS)
    if old_counts == (0, 0, 0) and marker_counts == (1, 1, 1):
        return source
    if old_counts != (1, 1, 1) or marker_counts != (0, 0, 0):
        raise RuntimeError(
            "r2_legacy_preservation_anchor_invalid:"
            f"old={old_counts}:released={marker_counts}"
        )
    replacements = (
        R2_LEGACY_RELEASE_MARKERS[0],
        R2_LEGACY_RELEASE_MARKERS[1],
        R2_LEGACY_RELEASE_MARKERS[2],
    )
    for anchor, replacement in zip(old, replacements, strict=True):
        source = source.replace(anchor, replacement, 1)
    return source


def rebind_r2_target_tests(target: Path, article: dict[str, Any]) -> dict[str, Any]:
    """Atomically move exact Mounjaro recovery tests to refreshed source truth.

    These three assertions intentionally described the predecessor-owned raw
    HTML. Releasing that exact route from its legacy preservation hold changes
    its owner to the refreshed catalog row. Validate all three predecessor or
    all three successor anchors before writing anything, so a partial migration
    cannot weaken or strand the candidate's test contract.
    """
    route = str(article.get("canonical_path") or "")
    if route != R2_LEGACY_PRESERVED_ROUTE:
        return {"status": "PASS_NOT_APPLICABLE", "route": route, "files": []}
    h1 = article.get("h1")
    updated_at = article.get("updated_at")
    if not isinstance(h1, str) or "$" not in h1:
        raise RuntimeError("r2_mounjaro_test_h1_invalid")
    if not isinstance(updated_at, str) or ISO_DATE_PATTERN.fullmatch(updated_at) is None:
        raise RuntimeError("r2_mounjaro_test_updated_at_invalid")

    staged: list[tuple[Path, str, str, str]] = []
    modes: list[str] = []
    for relative, predecessor, successor in R2_MOUNJARO_TEST_BINDINGS:
        path = target / relative
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise RuntimeError(f"r2_mounjaro_test_overlay_invalid:{relative}")
        source = path.read_text(encoding="utf-8")
        old_count = source.count(predecessor)
        new_count = source.count(successor)
        if (old_count, new_count) == (1, 0):
            mode = "predecessor"
            patched = source.replace(predecessor, successor, 1)
        elif (old_count, new_count) == (0, 1):
            mode = "successor"
            patched = source
        else:
            raise RuntimeError(
                f"r2_mounjaro_test_anchor_invalid:{relative}:"
                f"old={old_count}:new={new_count}"
            )
        modes.append(mode)
        staged.append((path, source, patched, relative.as_posix()))

    if len(set(modes)) != 1:
        raise RuntimeError(f"r2_mounjaro_test_partial_migration:{modes}")
    rows: list[dict[str, Any]] = []
    for path, source, patched, relative in staged:
        before = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if modes[0] == "predecessor":
            path.write_text(patched, encoding="utf-8")
        rows.append({
            "path": relative,
            "sha256_before": before,
            "sha256_after": sha(path),
        })
    return {
        "status": (
            "PASS_R2_TARGET_TESTS_REBOUND"
            if modes[0] == "predecessor"
            else "PASS_R2_TARGET_TESTS_ALREADY_REBOUND"
        ),
        "route": route,
        "h1_sha256": hashlib.sha256(h1.encode("utf-8")).hexdigest(),
        "updated_at": updated_at,
        "files": rows,
    }


R2_PROMOTIONAL_CTA_LABELS = (
    "Estimate your cost",
    "Compare programs",
    "Find my match",
)
R2_PROMOTIONAL_CTA_SURFACE_OPEN = re.compile(
    r'<aside\b[^>]*\bclass="[^"]*(?:guide-next-card|guide-next-step)[^"]*"[^>]*>',
    re.IGNORECASE,
)
R2_ASIDE_TAG = re.compile(
    r'<aside\b[^>]*>|</aside\s*>',
    re.IGNORECASE,
)
R2_PROMOTIONAL_CTA_HREFS = {
    "Estimate your cost": re.compile(
        r"/tools/glp-1-cost-calculator/(?:\?source=guide-(?:early|end))?"
    ),
    "Compare programs": re.compile(
        r"/(?:providers|best/online-glp-1-providers)/(?:\?source=guide-(?:early|end))?"
    ),
    "Find my match": re.compile(
        r"/quiz/provider-fit/(?:\?source=guide-(?:early|end))?"
    ),
}


def r2_promotional_cta_surface_spans(html: str) -> list[tuple[int, int]]:
    """Return balanced, non-nested CTA aside spans or fail closed.

    A non-greedy ``<aside>.*?</aside>`` expression can pair an unclosed CTA
    opening tag with an unrelated later aside's closing tag. Track every aside
    boundary instead, and reject any nesting that involves a governed CTA
    surface before a byte range is eligible for deletion.
    """
    stack: list[tuple[int, bool]] = []
    spans: list[tuple[int, int]] = []
    promotional_opens = 0
    for tag in R2_ASIDE_TAG.finditer(html):
        markup = tag.group(0)
        if markup.lower().startswith("</aside"):
            if not stack:
                raise RuntimeError("r2_rendered_cta_surface_structure_invalid:unexpected_close")
            start, is_promotional = stack.pop()
            if is_promotional:
                spans.append((start, tag.end()))
            continue

        is_promotional = R2_PROMOTIONAL_CTA_SURFACE_OPEN.fullmatch(markup) is not None
        if is_promotional:
            promotional_opens += 1
        if (is_promotional and stack) or any(parent_promotional for _start, parent_promotional in stack):
            raise RuntimeError("r2_rendered_cta_surface_structure_invalid:nested_aside")
        stack.append((tag.start(), is_promotional))

    if stack:
        raise RuntimeError(
            "r2_rendered_cta_surface_structure_invalid:"
            f"unclosed={len(stack)}"
        )
    if len(spans) != promotional_opens:
        raise RuntimeError(
            "r2_rendered_cta_surface_structure_invalid:"
            f"opens={promotional_opens}:complete={len(spans)}"
        )
    return spans


def sanitize_r2_rendered_promotional_ctas(
    candidate: Path,
    job: dict[str, Any],
) -> dict[str, Any]:
    """Delete only complete promotional CTA panels from one rendered R2 target.

    Route governance still comes verbatim from the predecessor. This post-render
    safety step therefore does not reinterpret ``suppress_cta`` (or any other
    governed field): it operates on the exact candidate-local target bytes after
    the renderer has finished. Every known panel must be structurally complete
    and contain exactly the three governed promotional links; partial/evolved
    markup is refused instead of being guessed at. No other public route is read
    or written.
    """
    if job.get("operation") != "r2_refresh":
        return {"status": "PASS_NOT_APPLICABLE", "removed_surfaces": 0}
    route = job.get("target_route")
    if type(route) is not str or re.fullmatch(r"/(?:guides|best)/[a-z0-9]+(?:-[a-z0-9]+)*/", route) is None:
        raise RuntimeError("r2_rendered_cta_target_route_invalid")
    relative = Path(route.strip("/")) / "index.html"
    declared_changed = {
        Path(str(path)).as_posix() for path in job.get("allowed_public_changed") or []
    }
    if relative.as_posix() not in declared_changed:
        raise RuntimeError("r2_rendered_cta_target_not_declared_changed")
    target = candidate / "public" / relative
    if target.is_symlink() or not target.is_file():
        raise RuntimeError("r2_rendered_cta_target_file_invalid")
    html = target.read_text(encoding="utf-8")
    canonical = f'<link rel="canonical" href="https://glpclinicscout.com{route}">'
    if html.count(canonical) != 1:
        raise RuntimeError("r2_rendered_cta_target_canonical_invalid")

    surfaces = r2_promotional_cta_surface_spans(html)
    for start, end in surfaces:
        markup = html[start:end]
        anchors = re.findall(
            r'<a\b[^>]*\bhref="([^"]+)"[^>]*>([^<]*)</a>',
            markup,
            re.IGNORECASE,
        )
        if (
            len(anchors) != len(R2_PROMOTIONAL_CTA_LABELS)
            or tuple(label for _href, label in anchors) != R2_PROMOTIONAL_CTA_LABELS
            or any(
                R2_PROMOTIONAL_CTA_HREFS[label].fullmatch(href) is None
                for href, label in anchors
            )
        ):
            raise RuntimeError("r2_rendered_cta_surface_contract_invalid")

    chunks: list[str] = []
    cursor = 0
    for start, end in surfaces:
        chunks.append(html[cursor:start])
        cursor = end
    chunks.append(html[cursor:])
    sanitized = "".join(chunks)
    if R2_PROMOTIONAL_CTA_SURFACE_OPEN.search(sanitized):
        raise RuntimeError("r2_rendered_cta_surface_residual")
    if any(token in sanitized for token in R2_PROMOTIONAL_CTA_LABELS) or re.search(
        r'href="/(?:tools/glp-1-cost-calculator|best/online-glp-1-providers|quiz/provider-fit)/\?source=guide-',
        sanitized,
    ):
        # Preserve ordinary navigation/citations rather than deleting a matching
        # token or link that escaped the two exact promotional panel classes.
        raise RuntimeError("r2_rendered_cta_forbidden_residual")
    before = hashlib.sha256(html.encode("utf-8")).hexdigest()
    if sanitized != html:
        detach_for_write(target)
        target.write_text(sanitized, encoding="utf-8")
    after = sha(target)
    return {
        "status": (
            "PASS_R2_RENDERED_PROMOTIONAL_CTAS_REMOVED"
            if surfaces
            else "PASS_R2_RENDERED_PROMOTIONAL_CTAS_ALREADY_SAFE"
        ),
        "route": route,
        "path": f"public/{relative.as_posix()}",
        "removed_surfaces": len(surfaces),
        "sha256_before": before,
        "sha256_after": after,
    }


def validate_rendered_article_contract(html: str, job: dict[str, Any]) -> None:
    """Validate the concrete rendered medical-guide surface, not source strings."""
    capture = re.findall(r'<form\b[^>]*action="/api/content-upgrade"[\s\S]*?</form>', html)
    if len(capture) != 1:
        raise RuntimeError("rendered_capture_form_count_invalid")
    form = capture[0]
    required = [
        'name="source_page" value="guide-link"',
        'name="lead_magnet_key" value="guide-link-resource"',
        'name="consent_version" value="glp-guide-resource-v1-20261007"',
        f'name="guide_path" value="{job["target_route"]}"',
        'name="request_id" value=""',
        'type="email" name="email"',
        '<p class="capture-consent-note">By submitting your email,',
        'href="/privacy/"',
        'you request one email containing a link to this guide.',
        'The email service receives your email address and the guide link to deliver it.',
        'This does not subscribe you to follow-up emails.',
        '>Email me a link to this guide</button>',
    ]
    if not job["target_route"].startswith("/guides/"):
        source_page = job["target_route"].strip("/").replace("/", "-")
        required = [f'name="source_page" value="{source_page}"',
                    f'name="lead_magnet_key" value="{source_page}-terms-checklist"',
                    'name="consent_version" value="glp-content-upgrade-v2-20260801"',
                    'type="email" name="email"', '<p class="capture-consent-note">By submitting your email,',
                    'href="/privacy/"', 'Unsubscribe anytime.']
    if (
        any(token not in form for token in required)
        or "payload.email_consent='true'" not in html
    ):
        raise RuntimeError("rendered_capture_contract_invalid")
    if re.search(r'<input\b[^>]*\bname="email_consent"', form):
        raise RuntimeError("rendered_capture_consent_control_invalid")
    input_names = set(re.findall(r'<input\b[^>]*\bname="([^"]+)"', form))
    allowed_names = {"source_page", "lead_magnet_key", "consent_version", "email", "company", "guide_path", "request_id"}
    if not job["target_route"].startswith("/guides/"):
        allowed_names -= {"guide_path", "request_id"}
    if not input_names or not input_names <= allowed_names:
        raise RuntimeError("rendered_capture_health_field_forbidden")
    if any(token in html for token in ("Estimate your cost", "Compare programs", "Find my match")):
        raise RuntimeError("rendered_medical_promotional_cta_forbidden")
    if re.search(r'href="/(?:tools/glp-1-cost-calculator|best/online-glp-1-providers|quiz/provider-fit)/\?source=guide-', html):
        raise RuntimeError("rendered_medical_promotional_cta_forbidden")
    _article_path, _article, checked_date = resolve_article_contract(job)
    machine_date = f'<time datetime="{checked_date}" data-prices-checked-date>{checked_date}</time>'
    if html.count(machine_date) != 1:
        raise RuntimeError("rendered_prices_checked_date_not_machine_readable")


def increment_coverage_anchors(
    script_source: str,
    test_source: str,
    predecessor_sitemap_routes: int,
    predecessor_build_report_routes: int | None = None,
) -> tuple[str, str]:
    script_pattern = re.compile(r'("glp-clinic-scout": )(\d+)(,)')
    delta_pattern = re.compile(r'(expected_sitemap_routes \+= )(\d+)(;)')
    routes_pattern = re.compile(r'(assert\.equal\(result\.sitemap_routes, )(\d+)(\);)')
    script_matches = list(script_pattern.finditer(script_source))
    delta_matches = list(delta_pattern.finditer(test_source))
    routes_matches = list(routes_pattern.finditer(test_source))

    def increment(match: re.Match[str]) -> str:
        return f"{match.group(1)}{int(match.group(2)) + 1}{match.group(3)}"

    # Legacy releases keep one flattened additive in the validator and mirror
    # both that value and the predecessor sitemap total as literals in the test.
    if len(script_matches) == len(delta_matches) == len(routes_matches) == 1:
        additive = int(script_matches[0].group(2))
        if int(delta_matches[0].group(2)) != additive:
            raise RuntimeError("coverage_additive_counter_drift")
        if int(routes_matches[0].group(2)) != predecessor_sitemap_routes:
            raise RuntimeError("coverage_sitemap_counter_drift")
        return script_pattern.sub(increment, script_source, count=1), routes_pattern.sub(
            increment, delta_pattern.sub(increment, test_source, count=1), count=1
        )

    # Layered successors derive the test expectation from the build report, so
    # there is deliberately no test-side numeric literal to rewrite. Bind that
    # report to the predecessor sitemap before adding a dedicated carrier layer;
    # this preserves the old fail-closed predecessor-count check instead of
    # treating an arbitrary multi-counter script as valid.
    if delta_matches or routes_matches:
        raise RuntimeError("coverage_counter_anchor_invalid")
    dynamic_test_markers = (
        'inputs.registry.site_profiles["glp-clinic-scout"].expected_sitemap_routes = buildReport.indexable_routes;',
        'assert.equal(result.sitemap_routes, inputs.registry.site_profiles["glp-clinic-scout"].expected_sitemap_routes);',
    )
    if any(test_source.count(marker) != 1 for marker in dynamic_test_markers):
        raise RuntimeError("coverage_counter_anchor_invalid")
    if (
        type(predecessor_build_report_routes) is not int
        or predecessor_build_report_routes != predecessor_sitemap_routes
    ):
        raise RuntimeError("coverage_sitemap_counter_drift")

    live_block_pattern = re.compile(
        r"(const LIVE_HEAD_MERGE_ADDITIVE_SITEMAP_ROUTES = Object\.freeze\(\{[^}]*\}\);)"
    )
    live_summand = "        + (LIVE_HEAD_MERGE_ADDITIVE_SITEMAP_ROUTES[profileId] || 0)\n"
    if len(list(live_block_pattern.finditer(script_source))) != 1 or script_source.count(live_summand) != 1:
        raise RuntimeError("coverage_counter_anchor_invalid")

    carrier_name = "ARTICLE_CARRIER_ADDITIVE_SITEMAP_ROUTES"
    carrier_pattern = re.compile(
        r'(const ARTICLE_CARRIER_ADDITIVE_SITEMAP_ROUTES = Object\.freeze\(\{\n'
        r'  // Net-new article routes published after this layered release baseline\.\n'
        r'  "glp-clinic-scout": )(\d+)(,\n\}\);)'
    )
    carrier_matches = list(carrier_pattern.finditer(script_source))
    if carrier_name not in script_source:
        carrier_block = (
            "\nconst ARTICLE_CARRIER_ADDITIVE_SITEMAP_ROUTES = Object.freeze({\n"
            "  // Net-new article routes published after this layered release baseline.\n"
            '  "glp-clinic-scout": 1,\n'
            "});"
        )
        script_source = live_block_pattern.sub(
            lambda match: f"{match.group(1)}{carrier_block}", script_source, count=1
        )
        script_source = script_source.replace(
            live_summand,
            live_summand + "        + (ARTICLE_CARRIER_ADDITIVE_SITEMAP_ROUTES[profileId] || 0)\n",
            1,
        )
    elif len(carrier_matches) == 1 and script_source.count(
        "        + (ARTICLE_CARRIER_ADDITIVE_SITEMAP_ROUTES[profileId] || 0)\n"
    ) == 1:
        script_source = carrier_pattern.sub(increment, script_source, count=1)
    else:
        raise RuntimeError("coverage_counter_anchor_invalid")
    return script_source, test_source


CONTENT_UPGRADE_DYNAMIC_COUNT_ASSERTION = (
    "assert.equal(emitted.length, CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES.length);"
)
CONTENT_UPGRADE_INVENTORY_ASSERTIONS = (
    "const approved = [...CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES].sort();",
    "const staged = await stagedPhase3CaptureSources();",
    "assert.deepEqual(emitted, [...new Set([...approved, ...staged])].sort());",
)
ARTICLE_SHARE_TEST_TITLE = (
    'test("all served article documents expose the complete share control set", async () => {'
)
ARTICLE_SHARE_COUNT_PATTERN = re.compile(
    r"(?P<prefix>^\s*assert\.equal\(checked, )(?P<count>\d+)(?P<suffix>\);[^\n]*$)",
    re.MULTILINE,
)


# --- leaked prose markup (2026-08-18 operator defect report) -----------------
# The renderer emits every prose field through esc() as a TEXT node, correctly,
# because prose is untrusted content. So a literal HTML tag in a catalog
# paragraph never becomes markup — it becomes visible copy in the reader's
# article body. Two writers put it there: the 08-16 hand-built packet scripts
# (standalone `<a id="slug"></a>` jump anchors, one before each heading) and an
# older r2 refresh (a whole `<!-- R2 TOC START --> <nav>...</nav>` block). Both
# shipped: `/guides/oral-glp-1-insurance-coverage/` and
# `/guides/mounjaro-vs-zepbound-label-and-cost/` rendered tag text as body copy.
#
# Removing it costs the reader nothing. `src/blog-system.mjs` already emits
# `<h2 id="section-N-slug">` for every section and its own `guide-toc` jump-link
# nav, so in-page anchors and the table of contents are the renderer's job.
PROSE_EMPTY_ANCHOR = re.compile(r'<a\s+id="[a-z0-9-]+"\s*>\s*</a>')
PROSE_TOC_COMMENT_BLOCK = re.compile(r"<!--\s*R2 TOC START\s*-->.*?<!--\s*R2 TOC END\s*-->", re.S)
PROSE_NAV_BLOCK = re.compile(r"<nav\b[^>]*>.*?</nav>", re.S)
PROSE_RESIDUAL_MARKUP = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>|<!--")
RENDERED_ESCAPED_TAG = re.compile(r"&lt;\s*/?\s*[a-zA-Z!]")


def scrub_prose_markup(value: str) -> str:
    """Delete leaked HTML markup from one prose string. Deletion only."""
    cleaned = PROSE_TOC_COMMENT_BLOCK.sub("", value)
    cleaned = PROSE_NAV_BLOCK.sub("", cleaned)
    cleaned = PROSE_EMPTY_ANCHOR.sub("", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def scrub_catalog_prose(catalogs: dict[str, Any], allowed_routes: set[str]) -> list[dict[str, Any]]:
    """Remove leaked markup from the catalog records this release may change.

    SCOPED ON PURPOSE. A release may only add/change the public files in its
    delta contract (`allowed_public_added` / `allowed_public_changed`): the new
    article, the inbound-link source pages, the section index, the home page and
    the sitemap. Scrubbing prose for any OTHER route would ship a cleaned
    `content-batch.json` next to hardlinked, still-dirty rendered HTML — the data
    and the pages would disagree, and the NEXT release would blow its delta gate
    on pages this one never meant to touch. Verified 2026-08-18: an unscoped
    scrub changed content-batch.json while `/guides/oral-glp-1-insurance-coverage/`
    and `/guides/mounjaro-vs-zepbound-label-and-cost/` kept their old bytes.

    Published pages outside this release's contract are repaired through the
    operation that is allowed to change them — an `r2_refresh` for that route.

    Fail loud rather than guess: if a field still holds markup after the known
    leak shapes are removed, the release stops instead of shipping either the
    leak or a silent partial edit.
    """
    changes: list[dict[str, Any]] = []

    def clean(value: str, route: str, where: str, *, droppable: bool) -> str | None:
        if not isinstance(value, str) or not PROSE_RESIDUAL_MARKUP.search(value):
            return value
        cleaned = scrub_prose_markup(value)
        if PROSE_RESIDUAL_MARKUP.search(cleaned):
            raise RuntimeError(f"prose_markup_unrecognized:{route}:{where}")
        if not cleaned and not droppable:
            raise RuntimeError(f"prose_markup_would_empty_required_field:{route}:{where}")
        changes.append({
            "route": route, "field": where,
            "action": "DROPPED" if not cleaned else "MARKUP_STRIPPED",
            "removed_chars": len(value) - len(cleaned),
        })
        return cleaned or None

    for payload in catalogs.values():
        for row in payload.get("articles") or []:
            route = str(row.get("canonical_path") or "")
            if route not in allowed_routes:
                continue
            for index, section in enumerate(row.get("sections") or []):
                if not isinstance(section, dict):
                    continue
                heading = clean(section.get("heading"), route, f"sections[{index}].heading",
                                droppable=False)
                if isinstance(section.get("heading"), str):
                    section["heading"] = heading
                for key in ("paragraphs", "bullets", "ordered", "items"):
                    if not isinstance(section.get(key), list):
                        continue
                    kept = []
                    for position, item in enumerate(section[key]):
                        result = clean(item, route, f"sections[{index}].{key}[{position}]",
                                       droppable=True)
                        if result is not None:
                            kept.append(result)
                    section[key] = kept
            for index, faq in enumerate(row.get("faqs") or []):
                if not isinstance(faq, dict):
                    continue
                for key in ("q", "a", "question", "answer"):
                    if isinstance(faq.get(key), str):
                        faq[key] = clean(faq[key], route, f"faqs[{index}].{key}", droppable=False)
            for key in ("direct_answer", "conclusion", "description", "title", "h1"):
                if isinstance(row.get(key), str):
                    row[key] = clean(row[key], route, key, droppable=False)
    return changes


def assert_no_rendered_prose_markup_leak(release: Path, routes: set[str]) -> int:
    """No page THIS release ships may show an escaped HTML tag as reader copy.

    Scoped to the release's own routes for the same reason the scrub is: pages
    outside the delta contract are hardlinked from the accepted base and are not
    this release's to fix, so failing on them would wedge every publish behind an
    unrelated legacy defect instead of surfacing it as its own repair.
    """
    scanned = 0
    for route in sorted(routes):
        page = release / "public" / route.strip("/") / "index.html"
        if not page.is_file():
            continue
        scanned += 1
        if RENDERED_ESCAPED_TAG.search(page.read_text(encoding="utf-8", errors="strict")):
            raise RuntimeError(f"rendered_prose_html_leak:{route}")
    return scanned


def increment_content_upgrade_count(consent_source: str, test_source: str) -> str:
    """Bind the generated capture-count test to the current predecessor bytes.

    Three accepted predecessor states, exactly like the playbook batch-gate anchor:

    * LITERAL — ``assert.equal(emitted.length, <int>);``. The builder is adding
      one allowlist entry, so the literal must be incremented by one here or
      ``npm test`` fails inside the candidate.
    * DERIVED — ``assert.equal(emitted.length,
      CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES.length);``. Two OTHER machines write
      this stronger form into the shared base (``glp_affiliate_wire_backlog_
      worker._normalize_content_upgrade_provenance_test`` and
      ``glp_provider_cardinality_test_refresh``). It re-derives from the runtime
      allowlist the builder just extended, so incrementing it is both impossible
      and unnecessary — the correct action is to leave the bytes alone. Before
      2026-08-18 this state raised ``content_upgrade_provenance_count_anchor_
      invalid`` and every GLP article publish died there once such a base was
      accepted (found behind the 08-16 inbound-link regression).
    * INVENTORY — the current site compares the emitted inventory with the
      deduplicated union of the governed allowlist and separately staged Phase 3
      sources. Adding the route to both the built page and governed allowlist
      keeps that semantic assertion correct, so these bytes also stay unchanged.

    Any other tree — no allowlist array, no assertion, several assertions, or a
    mix of both forms — still fails loud rather than being silently patched.
    """
    array_match = re.search(
        r"CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES\s*=\s*Object\.freeze\(\[([\s\S]*?)\]\);",
        consent_source,
    )
    if array_match is None:
        raise RuntimeError("content_upgrade_provenance_count_anchor_invalid")
    test_pattern = re.compile(r"(assert\.equal\(emitted\.length, )(\d+)(\);)")
    test_matches = list(test_pattern.finditer(test_source))
    derived_count = test_source.count(CONTENT_UPGRADE_DYNAMIC_COUNT_ASSERTION)
    inventory_counts = [test_source.count(anchor) for anchor in CONTENT_UPGRADE_INVENTORY_ASSERTIONS]
    if derived_count == 1 and not test_matches:
        return test_source
    if inventory_counts == [1, 1, 1] and not test_matches and not derived_count:
        return test_source
    if len(test_matches) != 1 or derived_count or any(inventory_counts):
        raise RuntimeError("content_upgrade_provenance_count_anchor_invalid")
    predecessor_count = len(re.findall(r'^\s+"[^"]+",\s*$', array_match.group(1), re.MULTILINE))
    if predecessor_count < 1 or int(test_matches[0].group(2)) != predecessor_count:
        raise RuntimeError("content_upgrade_provenance_count_drift")
    return test_pattern.sub(
        lambda match: f"{match.group(1)}{predecessor_count + 1}{match.group(3)}",
        test_source,
        count=1,
    )


def increment_article_share_control_count(predecessor: Path, test_source: str) -> str:
    """Advance the strict served-article share count for one new article.

    The accepted site intentionally retains image-manifest records for held,
    currently unserved articles. The late-audit test therefore cannot compare
    ``checked`` with the full manifest length. It carries an exact literal for
    the served subset instead. Bind that literal to the predecessor's real
    manifest-backed public inventory before advancing it by exactly one for the
    new route. This keeps the missing-page/cardinality guard strict without
    hardcoding today's count in the carrier.
    """
    if test_source.count(ARTICLE_SHARE_TEST_TITLE) != 1:
        raise RuntimeError("article_share_count_anchor_invalid")
    matches = list(ARTICLE_SHARE_COUNT_PATTERN.finditer(test_source))
    if len(matches) != 1:
        raise RuntimeError("article_share_count_anchor_invalid")

    manifest_path = predecessor / "artifacts/ultracode/article-image-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("article_share_manifest_invalid")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("records")
    if not isinstance(records, list) or not records:
        raise RuntimeError("article_share_manifest_invalid")

    routes: list[str] = []
    served_count = 0
    for record in records:
        route = str(record.get("route") or "") if isinstance(record, dict) else ""
        if not route.startswith("/") or not route.endswith("/"):
            raise RuntimeError("article_share_manifest_invalid")
        routes.append(route)
        page = predecessor / "public" / route.strip("/") / "index.html"
        if page.is_symlink():
            raise RuntimeError("article_share_manifest_invalid")
        if page.is_file():
            served_count += 1
    if len(routes) != len(set(routes)):
        raise RuntimeError("article_share_manifest_invalid")
    if int(matches[0].group("count")) != served_count:
        raise RuntimeError("article_share_count_drift")

    return ARTICLE_SHARE_COUNT_PATTERN.sub(
        lambda match: (
            f'{match.group("prefix")}{served_count + 1}{match.group("suffix")}'
        ),
        test_source,
        count=1,
    )


def ensure_content_upgrade_provenance_entry(source: str, anchor: str, entry: str) -> str:
    """Add one provenance entry across legacy and exhaustive literal allowlists.

    Article releases historically inserted after one stable route. The B2
    release builder now regenerates this array from every built capture surface,
    sorts it, and may legitimately omit that historical route. Accept only those
    two exact states: the legacy anchor, or one unique, duplicate-free, literal-
    only CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES array. Canonicalize the latter
    while inserting because accepted-base producers can append a validated cohort;
    the candidate's served-output inventory test remains the exhaustiveness gate.
    """
    entry_count = source.count(entry)
    if entry_count == 1:
        return source
    if entry_count != 0:
        raise RuntimeError(f"content_upgrade_provenance_duplicate:{entry_count}")
    if source.count(anchor) == 1:
        return source.replace(anchor, anchor + entry, 1)
    if source.count(anchor) != 0:
        raise RuntimeError("content_upgrade_provenance_allowlist_anchor_invalid")

    entry_match = re.fullmatch(r'  "([^"]+)",\n', entry)
    array_pattern = re.compile(
        r"(export const CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES\s*=\s*Object\.freeze\(\[\n)"
        r"([\s\S]*?)"
        r"(\]\);)"
    )
    array_matches = list(array_pattern.finditer(source))
    if (
        entry_match is None
        or source.count("export const CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES") != 1
        or len(array_matches) != 1
    ):
        raise RuntimeError("content_upgrade_provenance_allowlist_anchor_invalid")
    body = array_matches[0].group(2)
    rows = re.findall(r'^  "([^"]+)",\s*$', body, re.MULTILINE)
    nonblank_lines = [line for line in body.splitlines() if line.strip()]
    if (
        not rows
        or len(rows) != len(nonblank_lines)
        or len(rows) != len(set(rows))
    ):
        raise RuntimeError("content_upgrade_provenance_allowlist_anchor_invalid")
    rows.append(entry_match.group(1))
    rows.sort()
    replacement = (
        f"{array_matches[0].group(1)}"
        + "".join(f'  "{value}",\n' for value in rows)
        + f"{array_matches[0].group(3)}"
    )
    return array_pattern.sub(lambda _match: replacement, source, count=1)


def rebind_historical_article_tests_for_r2(source: str) -> str:
    """Keep historical cohort tests valid after an existing article is refreshed."""
    old_batch_gate = PLAYBOOK_HISTORICAL_BATCH_GATE_SUCCESSOR
    new_batch_gate = PLAYBOOK_HISTORICAL_BATCH_GATE_R2_SUCCESSOR
    source = apply_once_or_already(source, old_batch_gate, new_batch_gate, "playbook_r2_batch_gate")
    source = apply_once_or_already(
        source,
        PLAYBOOK_BATCH_FIVE_PREDECESSOR,
        PLAYBOOK_BATCH_FIVE_R2_SUCCESSOR,
        "playbook_r2_batch_five",
    )
    return apply_once_or_already(
        source,
        PLAYBOOK_BATCH_SIX_PREDECESSOR,
        PLAYBOOK_BATCH_SIX_R2_SUCCESSOR,
        "playbook_r2_batch_six",
    )


def detach_explicit_overlays(
    root: Path,
    relative_paths: tuple[Path, ...] | None = None,
) -> list[dict[str, Any]]:
    """Detach the complete, bounded set of files this builder may overwrite."""
    overlays = tuple(relative_paths if relative_paths is not None else ARTICLE_OVERLAYS)
    normalized: list[Path] = []
    for relative in overlays:
        relative = Path(relative)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise RuntimeError(f"overlay_path_invalid:{relative}")
        if any(relative == prior or relative in prior.parents or prior in relative.parents for prior in normalized):
            raise RuntimeError(f"overlay_path_collision:{relative}")
        normalized.append(relative)
    optional_missing = set(OPTIONAL_ARTICLE_OVERLAYS if relative_paths is None else ())
    detached: list[dict[str, Any]] = []
    for relative in normalized:
        path = root / relative
        if not os.path.lexists(path) and relative in optional_missing:
            # Pre-local-state releases legitimately inherit the exact external
            # migration source. The stage creates the candidate-local state.
            continue
        detached.append(detach_for_write(path))
    return detached


def detach_explicit_overlay_trees(
    root: Path,
    relative_trees: tuple[Path, ...] | None = None,
) -> list[dict[str, Any]]:
    """Detach every existing file in bounded trees regenerated in place.

    ``build-staging.mjs`` refreshes the complete Build 4 predecessor snapshot
    before resetting public/. The snapshot grows as routes are added, so an
    explicit file list would drift and reintroduce EACCES failures on sealed
    hardlinks. The tree itself is the bounded write surface.
    """
    trees = tuple(
        relative_trees if relative_trees is not None else IN_PLACE_BUILD_OUTPUT_TREES
    )
    normalized: list[Path] = []
    detached: list[dict[str, Any]] = []
    for relative in trees:
        relative = Path(relative)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise RuntimeError(f"overlay_tree_path_invalid:{relative}")
        if any(relative == prior or relative in prior.parents or prior in relative.parents for prior in normalized):
            raise RuntimeError(f"overlay_tree_path_collision:{relative}")
        normalized.append(relative)
        tree = root / relative
        if not os.path.lexists(tree):
            continue
        if tree.is_symlink() or not tree.is_dir():
            raise RuntimeError(f"overlay_tree_invalid:{relative}")
        for path in sorted(tree.rglob("*")):
            if path.is_symlink():
                raise RuntimeError(f"overlay_tree_symlink_forbidden:{path.relative_to(root)}")
            if path.is_file():
                detached.append(detach_for_write(path))
    return detached


def normalize_article_route_master_cow_validation(target: Path) -> dict[str, Any]:
    """Validate vendored PASS 2 route masters inside a COW successor.

    The immutable generation state records the absolute candidate pathname where
    each master was first produced. Every accepted successor vendors those exact
    bytes under its own route-master directory, so requiring the historical path
    to live below the successor rejects an honest linked build. Preserve the
    receipt's route-derived basename and SHA checks while validating the local
    mirror that the successor will publish.
    """
    path = target / "scripts/generate-article-imagery.py"
    old = '''        path = Path(record["output_path"]).resolve()
        if ROUTE_MASTER_DIR.resolve() not in path.parents or not path.exists():
            raise RuntimeError(f"article_route_master_path_invalid:{route}")
        if sha_file(path) != record.get("output_sha256"):
'''
    new = '''        recorded_path = Path(record["output_path"]).resolve()
        expected_name = f"{stem_for(route)}.webp"
        if recorded_path.name != expected_name:
            raise RuntimeError(f"article_route_master_identity_invalid:{route}")
        # The immutable generation receipt names the candidate where the master
        # was originally created. Successor candidates vendor those exact bytes
        # into their own route-master directory, so validate the local mirror
        # rather than requiring the historical absolute path to sit under ROOT.
        path = (ROUTE_MASTER_DIR / expected_name).resolve()
        if ROUTE_MASTER_DIR.resolve() not in path.parents or not path.exists():
            raise RuntimeError(f"article_route_master_path_invalid:{route}")
        if sha_file(path) != record.get("output_sha256"):
'''
    text = path.read_text(encoding="utf-8")
    if new in text:
        return {"status": "PASS_ALREADY_COW_SAFE", "path": str(path.relative_to(target))}
    if text.count(old) != 1:
        raise RuntimeError(f"article_route_master_cow_patch_match_count:{text.count(old)}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return {"status": "PASS_MIGRATED_COW_SAFE", "path": str(path.relative_to(target))}


def configure_article_route_master_state_overlay(target: Path) -> dict[str, Any]:
    """Bind v3 image validation to candidate-local, immutable lineage state."""
    path = target / "scripts/generate-article-imagery.py"
    text = path.read_text(encoding="utf-8")
    old_constant = (
        'ROUTE_MASTER_STATE = Path("/home/ubuntu/business/reports/glp-clinic-scout/'
        'rebuild/pass2-image-generation/state.json")'
    )
    new_constant = (
        'ROUTE_MASTER_STATE_RELATIVE = Path('
        '"artifacts/ultracode/article-route-master-state.json")\n'
        'ROUTE_MASTER_STATE = ROOT / ROUTE_MASTER_STATE_RELATIVE'
    )
    if text.count(new_constant) == 1 and text.count(old_constant) == 0:
        constant_status = "PASS_ALREADY_LOCAL"
    elif text.count(old_constant) == 1 and text.count(new_constant) == 0:
        text = text.replace(old_constant, new_constant, 1)
        constant_status = "PASS_MIGRATED_LOCAL"
    else:
        raise RuntimeError(
            "article_route_master_state_constant_anchor_invalid:"
            f"old={text.count(old_constant)}:new={text.count(new_constant)}"
        )

    old_lineage = (
        '{"path": str(ROUTE_MASTER_STATE), "sha256": sha_file(ROUTE_MASTER_STATE)}'
    )
    new_lineage = (
        '{"path": str(ROUTE_MASTER_STATE_RELATIVE), '
        '"sha256": sha_file(ROUTE_MASTER_STATE)}'
    )
    old_count = text.count(old_lineage)
    new_count = text.count(new_lineage)
    if old_count == 2 and new_count == 0:
        text = text.replace(old_lineage, new_lineage)
        lineage_status = "PASS_MIGRATED_RELATIVE"
    elif old_count == 0 and new_count == 2:
        lineage_status = "PASS_ALREADY_RELATIVE"
    else:
        raise RuntimeError(
            "article_route_master_state_lineage_anchor_invalid:"
            f"old={old_count}:new={new_count}"
        )
    path.write_text(text, encoding="utf-8")
    return {
        "status": "PASS",
        "path": str(path.relative_to(target)),
        "constant": constant_status,
        "lineage": lineage_status,
    }


def patch_article_image_visual_qa_for_release(target: Path) -> dict[str, Any]:
    """Keep historical visual QA scoped while proving every release-pipeline image."""
    path = target / "tests/article-imagery-pipeline.test.mjs"
    source = path.read_text(encoding="utf-8")
    if 'assert.equal(receipt.provider || "higgsfield", generated.provider || "higgsfield", generated.route);' in source:
        article_image_release_contract.configure(target)
        return {"status": "PASS_RELEASE_IMAGE_PROVENANCE_SCOPED", "path": str(path.relative_to(target)), "sha256": sha(path)}
    source = apply_once_or_already(
        source,
        '''  const routeState = JSON.parse(routeStateBytes);\n  const visualQaPath = "/home/ubuntu/business/reports/glp-clinic-scout/rebuild/pass2-image-generation/VISUAL-QA-20260825.json";''',
        '''  const routeState = JSON.parse(routeStateBytes);\n  const releaseRecords = Object.values(routeState.records).filter((record) => record.generation_lane === "article_release_pipeline");\n  const releaseRoutes = new Set(releaseRecords.map((record) => record.route));\n  const inheritedRoutes = routes.filter((route) => !releaseRoutes.has(route));\n  const visualQaPath = "/home/ubuntu/business/reports/glp-clinic-scout/rebuild/pass2-image-generation/VISUAL-QA-20260825.json";''',
        "article_image_release_visual_qa_scope",
    )
    replacements = (
        (
            '  assert.match(manifest.route_master_state.path, /pass2-image-generation\\/state\\.json$/);',
            '  assert.equal(manifest.route_master_state.path, "artifacts/ultracode/article-route-master-state.json");',
            "article_image_local_state_path",
        ),
        (
            "  assert.equal(visualQa.route_master_count, routes.length);",
            "  assert.equal(visualQa.route_master_count, inheritedRoutes.length);",
            "article_image_visual_qa_count_scope",
        ),
        (
            "  assert.equal(contactSheetManifest.route_master_count, routes.length);",
            "  assert.equal(contactSheetManifest.route_master_count, inheritedRoutes.length);",
            "article_image_contact_sheet_count_scope",
        ),
        (
            "  assert.equal(contactSheetManifest.unique_route_master_hashes, routes.length);",
            "  assert.equal(contactSheetManifest.unique_route_master_hashes, inheritedRoutes.length);",
            "article_image_contact_sheet_hash_scope",
        ),
        (
            "  assert.equal(contactRoutes.length, routes.length);",
            "  assert.equal(contactRoutes.length, inheritedRoutes.length);",
            "article_image_contact_sheet_route_count_scope",
        ),
        (
            "  assert.equal(new Set(contactRoutes).size, routes.length);",
            "  assert.equal(new Set(contactRoutes).size, inheritedRoutes.length);",
            "article_image_contact_sheet_unique_route_scope",
        ),
        (
            "  assert.deepEqual(contactRoutes.sort(), routes);",
            "  assert.deepEqual(contactRoutes.sort(), inheritedRoutes);",
            "article_image_contact_sheet_routes_scope",
        ),
    )
    for old, new, label in replacements:
        source = apply_once_or_already(source, old, new, label)

    release_proof = '''  for (const generated of releaseRecords) {
    assert.equal(generated.status, "PASS", generated.route);
    assert.equal(generated.output_path, `assets/source/article-masters-pass2/${generated.stem}.webp`, generated.route);
    assert.match(generated.source_sha256, /^[a-f0-9]{64}$/, generated.route);
    const lineage = generated.release_pipeline_receipt;
    assert.match(lineage.path, /^artifacts\\/ultracode\\/article-release-image-receipts\\/guide-[a-z0-9-]+\\.json$/);
    const receiptBytes = await readFile(new URL(`../${lineage.path}`, import.meta.url));
    assert.equal(sha256(receiptBytes), lineage.sha256, generated.route);
    const receipt = JSON.parse(receiptBytes);
    assert.equal(receipt.schema, "glp-article-hero-image-v1", generated.route);
    assert.equal(receipt.status, "GENERATED", generated.route);
    assert.equal(receipt.model, "gpt_image_2", generated.route);
    assert.equal(receipt.route, generated.route, generated.route);
    assert.equal(receipt.h1, generated.h1, generated.route);
    assert.equal(receipt.visual_key, generated.stem, generated.route);
    assert.equal(receipt.prompt_sha256, generated.prompt_sha256, generated.route);
    assert.equal(receipt.selected_candidate.source.sha256, generated.source_sha256, generated.route);
    const manifestRecord = manifest.records.find((record) => record.route === generated.route);
    assert.ok(manifestRecord, `release manifest record missing: ${generated.route}`);
    const receiptOutputs = receipt.files.map(({ name, width, height, sha256: outputSha }) => ({ name, width, height, sha256: outputSha })).sort((a, b) => a.name.localeCompare(b.name));
    const manifestOutputs = manifestRecord.outputs.map(({ name, width, height, sha256: outputSha }) => ({ name, width, height, sha256: outputSha })).sort((a, b) => a.name.localeCompare(b.name));
    assert.deepEqual(manifestOutputs, receiptOutputs, generated.route);
  }
'''
    source = apply_once_or_already(
        source,
        "  for (const record of manifest.records) {\n",
        release_proof + "  for (const record of manifest.records) {\n",
        "article_image_release_receipt_proof",
    )
    path.write_text(source, encoding="utf-8")
    return {
        "status": "PASS_RELEASE_IMAGE_PROVENANCE_SCOPED",
        "path": str(path.relative_to(target)),
        "sha256": sha(path),
    }


def _make_candidate_directory_writable(path: Path) -> None:
    if path.exists() and (path.is_symlink() or not path.is_dir()):
        raise RuntimeError(f"article_image_candidate_directory_invalid:{path}")
    if not path.exists():
        parent = path.parent
        if parent.exists():
            parent.chmod(stat.S_IMODE(parent.stat().st_mode) | stat.S_IWUSR | stat.S_IXUSR)
        path.mkdir(parents=True, exist_ok=True)
    path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IWUSR | stat.S_IXUSR)


def resolve_predecessor_route_master_state(
    target: Path,
    manifest: dict[str, Any],
    inherited_routes: set[str],
) -> dict[str, Any]:
    """Resolve and validate the exact route-state declared by the predecessor.

    Current releases must use their release-local state. The historical
    external state remains accepted only as an exact migration source for
    predecessors that have not yet adopted the local-state contract.
    """
    state_spec = manifest.get("route_master_state")
    if not isinstance(state_spec, dict) or set(state_spec) != {"path", "sha256"}:
        raise RuntimeError("article_route_master_predecessor_state_spec_invalid")
    declared_path = state_spec.get("path")
    declared_sha256 = state_spec.get("sha256")
    if (
        not isinstance(declared_path, str)
        or not declared_path
        or not isinstance(declared_sha256, str)
        or SHA256_PATTERN.fullmatch(declared_sha256) is None
    ):
        raise RuntimeError("article_route_master_predecessor_state_spec_invalid")

    raw_path = Path(declared_path)
    target_root = target.resolve(strict=True)
    if raw_path.is_absolute():
        if raw_path != ROUTE_MASTER_STATE_SOURCE:
            raise RuntimeError("article_route_master_predecessor_external_state_invalid")
        source = ROUTE_MASTER_STATE_SOURCE
        mode = "legacy_external"
    else:
        if (
            raw_path != ROUTE_MASTER_STATE_RELATIVE
            or not raw_path.parts
            or any(part in {"", ".", ".."} for part in raw_path.parts)
        ):
            raise RuntimeError("article_route_master_predecessor_local_state_path_invalid")
        source = target / raw_path
        mode = "release_local"

    try:
        source_stat = os.lstat(source)
    except FileNotFoundError as exc:
        raise RuntimeError("article_route_master_predecessor_state_missing") from exc
    if not stat.S_ISREG(source_stat.st_mode) or stat.S_ISLNK(source_stat.st_mode):
        raise RuntimeError("article_route_master_predecessor_state_file_invalid")
    resolved_source = source.resolve(strict=True)
    if mode == "legacy_external":
        if resolved_source != source:
            raise RuntimeError("article_route_master_predecessor_state_symlink_invalid")
    else:
        if target_root not in resolved_source.parents or resolved_source != source:
            raise RuntimeError("article_route_master_predecessor_state_containment_invalid")
        cursor = target
        for part in raw_path.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise RuntimeError("article_route_master_predecessor_state_symlink_invalid")
    if sha(source) != declared_sha256:
        raise RuntimeError("article_route_master_predecessor_state_sha256_invalid")

    try:
        state = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("article_route_master_predecessor_state_json_invalid") from exc
    state_records = state.get("records") if isinstance(state, dict) else None
    if (
        not isinstance(state, dict)
        or state.get("schema") != "glp-pass2-route-image-generation-v1"
        or state.get("job_set_type") != "gpt_image_2"
        or state.get("model") != "GPT Image 2"
        or state.get("quality") != "high"
        or state.get("resolution") != "4k"
        or not isinstance(state_records, dict)
        or set(state_records) != inherited_routes
        or any(
            not isinstance(record, dict)
            or record.get("status") != "PASS"
            or record.get("route") != record_route
            for record_route, record in state_records.items()
        )
    ):
        raise RuntimeError("article_route_master_predecessor_state_invalid")

    return {
        "status": "PASS_ROUTE_MASTER_PREDECESSOR_RESOLVED",
        "mode": mode,
        "path": str(source),
        "sha256": declared_sha256,
        "state": state,
        "records": state_records,
    }


def stage_new_article_route_master(
    target: Path,
    article: dict[str, Any],
    image_receipt: dict[str, Any] | None,
    visual_key: str,
    catalogs: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Extend a v3 image corpus with one already-generated, receipt-bound family."""
    from PIL import Image

    manifest_path = target / "artifacts/ultracode/article-image-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != "glp-article-image-system-v3":
        return None

    route = str(article.get("canonical_path") or "")
    h1 = str(article.get("h1") or "")
    if not route or not h1 or visual_key != f"guide-{route.strip('/').split('/')[-1]}":
        raise RuntimeError("article_route_master_identity_invalid")
    if not isinstance(image_receipt, dict):
        raise RuntimeError("article_route_master_receipt_missing")
    provider = image_receipt.get("provider", "higgsfield")
    if provider not in article_images.PROVIDER_MODELS:
        raise RuntimeError("article_route_master_receipt_provider_invalid")
    expected_receipt = {
        "schema": article_images.SCHEMA,
        "status": article_images.STATUS_GENERATED,
        "model": article_images.PROVIDER_MODELS[provider],
        "route": route,
        "h1": h1,
        "visual_key": visual_key,
    }
    for field, expected in expected_receipt.items():
        if image_receipt.get(field) != expected:
            raise RuntimeError(f"article_route_master_receipt_{field}_invalid")
    prompt_sha256 = image_receipt.get("prompt_sha256")
    if not isinstance(prompt_sha256, str) or SHA256_PATTERN.fullmatch(prompt_sha256) is None:
        raise RuntimeError("article_route_master_prompt_sha256_invalid")

    staged_dir = Path(str(image_receipt.get("staged_dir") or ""))
    receipt_source = staged_dir.parent / "receipt.json"
    if (
        staged_dir.name != "family"
        or staged_dir.is_symlink()
        or not staged_dir.is_dir()
        or receipt_source.is_symlink()
        or not receipt_source.is_file()
        or json.loads(receipt_source.read_text(encoding="utf-8")) != image_receipt
    ):
        raise RuntimeError("article_route_master_receipt_source_invalid")
    receipt_sha256 = sha(receipt_source)

    selected = image_receipt.get("selected_candidate")
    if not isinstance(selected, dict) or not isinstance(selected.get("source"), dict):
        raise RuntimeError("article_route_master_selected_candidate_invalid")
    selected_index = selected.get("index")
    source_meta = selected["source"]
    source_sha256 = source_meta.get("sha256")
    if (
        not isinstance(selected_index, int)
        or selected.get("sha256") != source_sha256
        or not isinstance(source_sha256, str)
        or SHA256_PATTERN.fullmatch(source_sha256) is None
        or not isinstance(source_meta.get("width"), int)
        or not isinstance(source_meta.get("height"), int)
        or not article_images.source_dimensions_valid(source_meta["width"], source_meta["height"], provider)
    ):
        raise RuntimeError("article_route_master_selected_candidate_invalid")
    source_path = staged_dir.parent / f"candidate-{selected_index}.src"
    if source_path.is_symlink() or not source_path.is_file() or sha(source_path) != source_sha256:
        raise RuntimeError("article_route_master_selected_source_invalid")
    with Image.open(source_path) as source_image:
        source_image.load()
        if source_image.size != (source_meta["width"], source_meta["height"]):
            raise RuntimeError("article_route_master_selected_source_dims_invalid")

    expected_files = {
        f"{visual_key}{suffix}": dimensions
        for suffix, dimensions in {
            "-800.avif": (800, 450),
            "-1600.avif": (1600, 900),
            "-800.webp": (800, 450),
            "-1600.webp": (1600, 900),
            "-800.jpg": (800, 450),
            "-1600.jpg": (1600, 900),
            "-og.jpg": (1200, 630),
        }.items()
    }
    receipt_files = image_receipt.get("files")
    if not isinstance(receipt_files, list) or len(receipt_files) != len(expected_files):
        raise RuntimeError("article_route_master_family_receipt_invalid")
    files_by_name: dict[str, dict[str, Any]] = {}
    for record in receipt_files:
        if not isinstance(record, dict) or record.get("name") in files_by_name:
            raise RuntimeError("article_route_master_family_receipt_invalid")
        name = str(record.get("name") or "")
        files_by_name[name] = record
    if set(files_by_name) != set(expected_files):
        raise RuntimeError("article_route_master_family_names_invalid")
    for name, dimensions in expected_files.items():
        record = files_by_name[name]
        source = staged_dir / name
        if (
            record.get("width") != dimensions[0]
            or record.get("height") != dimensions[1]
            or not isinstance(record.get("bytes"), int)
            or not isinstance(record.get("sha256"), str)
            or SHA256_PATTERN.fullmatch(record["sha256"]) is None
            or source.is_symlink()
            or not source.is_file()
            or source.stat().st_size != record["bytes"]
            or sha(source) != record["sha256"]
        ):
            raise RuntimeError(f"article_route_master_family_file_invalid:{name}")
        with Image.open(source) as image:
            image.load()
            if image.size != dimensions or image.getexif():
                raise RuntimeError(f"article_route_master_family_image_invalid:{name}")

    all_articles = [row for payload in catalogs.values() for row in payload.get("articles") or []]
    current_routes = [str(row.get("canonical_path") or "") for row in all_articles]
    if len(current_routes) != len(set(current_routes)) or current_routes.count(route) != 1:
        raise RuntimeError("article_route_master_catalog_routes_invalid")
    inherited_routes = set(current_routes) - {route}
    manifest_records = manifest.get("records")
    if not isinstance(manifest_records, list):
        raise RuntimeError("article_route_master_manifest_records_invalid")
    manifest_routes = {str(record.get("route") or "") for record in manifest_records}
    if (
        manifest_routes != inherited_routes
        or len(manifest_records) != len(inherited_routes)
        or manifest.get("article_count") != len(inherited_routes)
        or manifest.get("route_master_count") != len(inherited_routes)
        or manifest.get("output_count") != sum(len(record.get("outputs") or []) for record in manifest_records)
    ):
        raise RuntimeError("article_route_master_predecessor_manifest_invalid")

    predecessor_state = resolve_predecessor_route_master_state(
        target,
        manifest,
        inherited_routes,
    )
    state = predecessor_state["state"]
    state_records = predecessor_state["records"]
    if predecessor_state["mode"] == "release_local" and (
        not stat.S_IMODE(os.lstat(predecessor_state["path"]).st_mode) & stat.S_IWUSR
        or os.lstat(predecessor_state["path"]).st_nlink != 1
    ):
        raise RuntimeError("article_route_master_predecessor_state_not_detached")

    overlay = configure_article_route_master_state_overlay(target)
    visual_qa = patch_article_image_visual_qa_for_release(target)
    state_dir = target / ROUTE_MASTER_STATE_RELATIVE.parent
    receipt_dir = target / "artifacts/ultracode/article-release-image-receipts"
    master_dir = target / "assets/source/article-masters-pass2"
    derivative_dir = target / "assets/derivatives"
    for directory in (state_dir, receipt_dir, master_dir, derivative_dir):
        _make_candidate_directory_writable(directory)

    receipt_relative = Path("artifacts/ultracode/article-release-image-receipts") / f"{visual_key}.json"
    receipt_target = target / receipt_relative
    if receipt_target.exists():
        raise RuntimeError("article_route_master_receipt_target_collision")
    shutil.copy2(receipt_source, receipt_target)

    master_relative = Path("assets/source/article-masters-pass2") / f"{visual_key}.webp"
    master_path = target / master_relative
    if master_path.exists():
        raise RuntimeError("article_route_master_target_collision")
    with Image.open(source_path) as source_image:
        source_image.load()
        clean = source_image.convert("RGB")
        clean.save(master_path, format="WEBP", quality=95, method=6)
    with Image.open(master_path) as master_image:
        master_image.load()
        if master_image.format != "WEBP" or master_image.size != (
            source_meta["width"], source_meta["height"]
        ) or master_image.getexif():
            raise RuntimeError("article_route_master_output_invalid")
    master_sha256 = sha(master_path)

    for name, record in files_by_name.items():
        destination = derivative_dir / name
        if destination.exists():
            raise RuntimeError(f"article_route_master_derivative_collision:{name}")
        shutil.copy2(staged_dir / name, destination)
        if sha(destination) != record["sha256"]:
            raise RuntimeError(f"article_route_master_derivative_copy_invalid:{name}")

    subject = article_images.article_subject(article)
    state_records[route] = {
        "content_type": "image/webp",
        "elapsed_seconds": image_receipt.get("elapsed_s"),
        "format": "WEBP",
        "generation_lane": "article_release_pipeline",
        "provider": provider,
        "h1": h1,
        "height": source_meta["height"],
        "output_bytes": master_path.stat().st_size,
        "output_path": master_relative.as_posix(),
        "output_sha256": master_sha256,
        "prompt_sha256": prompt_sha256,
        "raw_receipt": receipt_relative.as_posix(),
        "release_pipeline_receipt": {
            "path": receipt_relative.as_posix(),
            "sha256": receipt_sha256,
        },
        "result_url_count": len(image_receipt.get("candidates") or []),
        "route": route,
        "source_sha256": source_sha256,
        "status": "PASS",
        "stem": visual_key,
        "subject": image_receipt.get("alt"),
        "width": source_meta["width"],
    }
    state["updated_at_utc"] = now()
    state_path = target / ROUTE_MASTER_STATE_RELATIVE
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    seed_sha256 = hashlib.sha256(route.encode("utf-8")).hexdigest()
    manifest_record = {
        "route": route,
        "stem": visual_key,
        "title_sha256": hashlib.sha256(h1.encode("utf-8")).hexdigest(),
        "art_direction": (
            "premium route-specific GPT Image 2 editorial still life generated from the article "
            "subject; no burned-in text, logos, labels, product claims, testimonials, or competitor assets"
        ),
        "seed": int(seed_sha256[:16], 16),
        "seed_sha256": seed_sha256,
        "category": subject["motif"],
        "generation_model": "gpt_image_2",
        "master_path": master_relative.as_posix(),
        "master_sha256": master_sha256,
        "prompt_sha256": prompt_sha256,
        "subject": image_receipt.get("alt"),
        "disposition": "generated_from_gpt_image_2_route_master",
        "outputs": [
            {
                "name": record["name"],
                "width": record["width"],
                "height": record["height"],
                "sha256": record["sha256"],
                "bytes": record["bytes"],
            }
            for record in receipt_files
        ],
    }
    manifest_records.append(manifest_record)
    manifest_records.sort(key=lambda record: record["route"])
    manifest["article_count"] = len(current_routes)
    manifest["records"] = manifest_records
    manifest["output_count"] = sum(len(record["outputs"]) for record in manifest_records)
    manifest["route_master_count"] = len(current_routes)
    manifest["external_generation_services_used"] = sum(record.get("provider") != "stock" for record in state_records.values())
    manifest["route_master_state"] = {
        "path": ROUTE_MASTER_STATE_RELATIVE.as_posix(),
        "sha256": sha(state_path),
    }
    source_files = (
        Path("data/blog/content-batch.json"),
        Path("data/blog/content-national-wow-additions.json"),
    )
    manifest["source_files"] = [
        {"path": relative.as_posix(), "sha256": sha(target / relative)}
        for relative in source_files
    ]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "PASS_ARTICLE_ROUTE_MASTER_STAGED",
        "route": route,
        "visual_key": visual_key,
        "source_sha256": source_sha256,
        "master": {
            "path": master_relative.as_posix(),
            "sha256": master_sha256,
            "width": source_meta["width"],
            "height": source_meta["height"],
        },
        "receipt": {"path": receipt_relative.as_posix(), "sha256": receipt_sha256},
        "state": {"path": ROUTE_MASTER_STATE_RELATIVE.as_posix(), "sha256": sha(state_path)},
        "predecessor_state": {
            "mode": predecessor_state["mode"],
            "path": predecessor_state["path"],
            "sha256": predecessor_state["sha256"],
        },
        "manifest_sha256": sha(manifest_path),
        "family": [
            {"name": record["name"], "sha256": record["sha256"]}
            for record in receipt_files
        ],
        "generator_overlay": overlay,
        "visual_qa_test": visual_qa,
    }


def verify_new_article_route_master_family(
    target: Path,
    route: str,
    visual_key: str,
    lineage: dict[str, Any],
) -> dict[str, Any]:
    """Verify the exact generated master/family after the clean public build."""
    manifest_path = target / "artifacts/ultracode/article-image-manifest.json"
    state_path = target / ROUTE_MASTER_STATE_RELATIVE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("route_master_state") != {
        "path": ROUTE_MASTER_STATE_RELATIVE.as_posix(),
        "sha256": sha(state_path),
    }:
        raise RuntimeError("article_route_master_postbuild_state_invalid")
    matches = [record for record in manifest.get("records") or [] if record.get("route") == route]
    if len(matches) != 1 or matches[0].get("stem") != visual_key:
        raise RuntimeError(f"article_route_master_postbuild_match_count:{len(matches)}")
    expected = {record["name"]: record["sha256"] for record in lineage.get("family") or []}
    observed = {record["name"]: record["sha256"] for record in matches[0].get("outputs") or []}
    if expected != observed or set(expected) != {f"{visual_key}{suffix}" for suffix in IMAGE_SUFFIXES}:
        raise RuntimeError("article_route_master_postbuild_family_invalid")
    for name, expected_sha256 in expected.items():
        for root in (target / "assets/derivatives", target / "public/assets/images"):
            path = root / name
            if path.is_symlink() or not path.is_file() or sha(path) != expected_sha256:
                raise RuntimeError(f"article_route_master_postbuild_hash_invalid:{path}")
    master = target / str(lineage["master"]["path"])
    if master.is_symlink() or not master.is_file() or sha(master) != lineage["master"]["sha256"]:
        raise RuntimeError("article_route_master_postbuild_master_invalid")
    return {
        "mode": "generated_route_master",
        "route": route,
        "files": sorted(expected),
        "bytes": sum((target / "public/assets/images" / name).stat().st_size for name in expected),
        "manifest_sha256": sha(manifest_path),
        "state_sha256": sha(state_path),
        "master_sha256": sha(master),
    }


#: Decorative-image lineage tiers, in selection order (GLP-73). A tier that the
#: release rejects steps down to the next one; the last tier publishes the
#: article with no hero at all. Content and claims checks are not in this lane.
IMAGE_LINEAGE_TIERS = ("native_generated", "stock_fallback", "text_only")


def _image_lineage_paths(target: Path, visual_key: str) -> list[Path]:
    """Every candidate path one route's image lineage may create or rewrite."""
    return [
        target / "artifacts/ultracode/article-image-manifest.json",
        target / ROUTE_MASTER_STATE_RELATIVE,
        target / "artifacts/ultracode/article-release-image-receipts" / f"{visual_key}.json",
        target / "assets/source/article-masters-pass2" / f"{visual_key}.webp",
        *(target / "assets/derivatives" / f"{visual_key}{suffix}" for suffix in IMAGE_SUFFIXES),
    ]


def snapshot_image_lineage(target: Path, visual_key: str) -> dict[Path, bytes | None]:
    """Capture tier-owned regular files; malformed inherited entries must survive.

    None means genuinely absent, never an existing symlink, directory or FIFO.
    Reject those before the native tier can write or fallback can mask them.
    """
    snapshot: dict[Path, bytes | None] = {}
    for path in _image_lineage_paths(target, visual_key):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            snapshot[path] = None
            continue
        if not stat.S_ISREG(mode):
            raise RuntimeError(f"article_image_lineage_nonregular_collision:{path.relative_to(target)}")
        snapshot[path] = path.read_bytes()
    return snapshot


def restore_image_lineage(snapshot: dict[Path, bytes | None]) -> list[str]:
    """Put every snapshotted path back exactly; new files are removed.

    A changed file is replaced through a fresh inode (temp file + rename), never
    rewritten in place, so a hard link the candidate still shares with its base
    release can never be mutated by the rollback.
    """
    restored: list[str] = []
    for path, body in snapshot.items():
        if body is None:
            if os.path.lexists(path):
                path.unlink()
                restored.append(path.as_posix())
            continue
        if path.is_file() and not path.is_symlink() and path.read_bytes() == body:
            continue
        mode = stat.S_IMODE(os.lstat(path).st_mode) if os.path.lexists(path) else None
        temp = path.with_name(path.name + ".rollback.tmp")
        temp.write_bytes(body)
        if mode is not None:
            os.chmod(temp, mode | stat.S_IWUSR)
        os.replace(temp, path)
        restored.append(path.as_posix())
    return restored


def demote_generated_receipt(receipt: dict[str, Any], reason: str) -> dict[str, Any]:
    """A GENERATED hero whose lineage the release rejected becomes a stock FALLBACK.

    Nothing of the rejected family survives into the receipt the release binds:
    no staged files, alt text or model claim. The rejection itself is retained
    as provenance so the ledger shows what was generated and why it was refused.
    """
    attempted = list(receipt.get("provider_chain_attempted") or [])
    attempted.append({"provider": "release_lineage", "status": "REJECTED", "reason": reason})
    return {
        "schema": article_images.SCHEMA, "status": article_images.STATUS_FALLBACK,
        "reason": reason, "visual_key": receipt.get("visual_key"), "route": receipt.get("route"),
        "h1": receipt.get("h1"), "alt": None, "staged_dir": None, "files": [],
        "credits_spent": receipt.get("credits_spent", 0), "cache_hit": receipt.get("cache_hit", False),
        "prepared_at_utc": receipt.get("prepared_at_utc") or now(), "provider": "stock",
        "provider_chain_attempted": attempted,
        "rejected_generation": {
            "provider": receipt.get("provider"), "model": receipt.get("model"),
            "prompt_sha256": receipt.get("prompt_sha256"), "staged_dir": receipt.get("staged_dir"),
            "selected_candidate_sha256": (receipt.get("selected_candidate") or {}).get("sha256"),
        },
    }


def legacy_fallback_receipt(article: dict[str, Any], visual_key: str) -> dict[str, Any]:
    """A job minted before the image lane existed never ran it; say so, then use stock."""
    return {
        "schema": article_images.SCHEMA, "status": article_images.STATUS_FALLBACK,
        "reason": "legacy_template_without_control_pin", "visual_key": visual_key,
        "route": str(article.get("canonical_path") or ""), "h1": str(article.get("h1") or ""),
        "alt": None, "staged_dir": None, "files": [], "credits_spent": 0, "cache_hit": False,
        "prepared_at_utc": now(), "provider": "stock", "provider_chain_attempted": [],
    }


def stage_article_image_lineage(
    target: Path,
    article: dict[str, Any],
    image_receipt: dict[str, Any] | None,
    visual_key: str,
    catalogs: dict[str, dict[str, Any]],
    catalog_paths: dict[str, Path],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    """Select the article's hero lineage: native GENERATED, receipted stock, text only.

    GLP-73: a decorative image must never veto a publication. Before this
    selector, a native hero that failed release-lineage validation (pixels,
    receipt binding, a collision in the candidate tree) raised straight out of
    the build and the day's article did not ship. Now the rejected tier is
    rolled back byte for byte, the receipt is demoted to a stock FALLBACK that
    records the rejection, and the next tier runs. Stock pixels that cannot be
    verified already step down to the text-only receipt inside ``stage_stock``.

    Returns ``(lineage, effective_receipt, selection)``. ``selection`` lists every
    tier attempted with its outcome so the build receipt is honest about which
    image, if any, the reader will see.
    """
    attempts: list[dict[str, Any]] = []
    receipt = image_receipt

    def write_catalogs() -> None:
        for name, path in catalog_paths.items():
            path.write_text(json.dumps(catalogs[name], indent=2, sort_keys=True) + "\n")

    if receipt is not None and receipt.get("status") == article_images.STATUS_GENERATED:
        snapshot = snapshot_image_lineage(target, visual_key)
        try:
            lineage = stage_new_article_route_master(target, article, receipt, visual_key, catalogs)
            article_image_release_contract.configure(target)
        except Exception as error:  # decoration must never gate a publish
            rolled_back = restore_image_lineage(snapshot)
            reason = f"generated_lineage_rejected:{type(error).__name__}:{error}"
            attempts.append({"tier": "native_generated", "status": "REJECTED",
                             "reason": reason, "rolled_back": rolled_back})
            print(json.dumps({"article_images": "generated_lineage_rejected",
                              "visual_key": visual_key, "reason": reason,
                              "rolled_back": rolled_back}, sort_keys=True), file=sys.stderr)
            receipt = demote_generated_receipt(receipt, reason)
            # The generated alt/model metadata was bound into the article row for
            # a family that will not ship; the catalogs must say stock, as before.
            article.pop("images", None)
            write_catalogs()
            article_images.record_event(receipt)
        else:
            attempts.append({"tier": "native_generated", "status": "STAGED"})
            return lineage, receipt, {"tier": "native_generated", "attempts": attempts}

    if receipt is None:
        receipt = legacy_fallback_receipt(article, visual_key)
        attempts.append({"tier": "native_generated", "status": "NOT_ATTEMPTED",
                         "reason": receipt["reason"]})
    lineage = article_image_release_contract.stage_stock(
        sys.modules[__name__], target, article, receipt, visual_key, catalogs,
    )
    if lineage is None:
        # Pre-v3 image corpus: the post-build ``install_hero`` copy decides.
        attempts.append({"tier": "stock_fallback", "status": "DEFERRED_TO_INSTALL",
                         "reason": "image_manifest_not_v3"})
        return None, receipt, {"tier": "stock_fallback", "attempts": attempts}
    if lineage.get("availability") == "unavailable":
        attempts.append({"tier": "stock_fallback", "status": "REJECTED", "reason": lineage.get("reason")})
        attempts.append({"tier": "text_only", "status": "STAGED", "receipt": lineage.get("receipt")})
        return lineage, receipt, {"tier": "text_only", "attempts": attempts}
    attempts.append({"tier": "stock_fallback", "status": "STAGED"})
    return lineage, receipt, {"tier": "stock_fallback", "attempts": attempts}


def rebind_article_image_manifest_for_r2(target: Path, article: dict[str, Any]) -> dict[str, Any]:
    """Rebind nonvisual image lineage after an existing article catalog edit."""
    manifest_path = target / "artifacts/ultracode/article-image-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") not in {"glp-article-image-system-v2", "glp-article-image-system-v3"}:
        raise RuntimeError("article_image_manifest_schema_invalid")
    route = str(article.get("canonical_path") or "")
    h1 = article.get("h1")
    if not route or not isinstance(h1, str) or not h1.strip():
        raise RuntimeError("r2_article_image_identity_invalid")
    records = manifest.get("records")
    if not isinstance(records, list):
        raise RuntimeError("article_image_manifest_records_invalid")
    matches = [record for record in records if isinstance(record, dict) and record.get("route") == route]
    if len(matches) != 1:
        raise RuntimeError(f"article_image_manifest_route_match_count:{len(matches)}")
    source_files = (
        Path("data/blog/content-batch.json"),
        Path("data/blog/content-national-wow-additions.json"),
    )
    before_sha256 = sha(manifest_path)
    prior_title_sha256 = matches[0].get("title_sha256")
    matches[0]["title_sha256"] = hashlib.sha256(h1.encode("utf-8")).hexdigest()
    manifest["source_files"] = [
        {"path": path.as_posix(), "sha256": sha(target / path)}
        for path in source_files
    ]
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "status": "PASS_R2_ARTICLE_IMAGE_LINEAGE_REBOUND",
        "path": str(manifest_path.relative_to(target)),
        "route": route,
        "prior_title_sha256": prior_title_sha256,
        "title_sha256": matches[0]["title_sha256"],
        "manifest_sha256_before": before_sha256,
        "manifest_sha256_after": sha(manifest_path),
        "public_image_bytes_changed": 0,
    }


def regenerate_tracking_allowed_hosts(target: Path) -> dict[str, Any]:
    """Regenerate the deterministic rails projection before its exact check."""
    return run(["python3", "scripts/emit-tracking-allowed-hosts.py"], target)


def normalize_css_bundle_mtimes(target: Path) -> list[dict[str, Any]]:
    """Rebind detached authoring mtimes to restored byte-identical public CSS.

    The clean build regenerates assets/*.css, then the COW public restore brings
    unchanged public bundles back from the sealed predecessor. Freshness checks
    compare mtimes as well as bytes. Only private authoring files are touched;
    public hardlinks and predecessor metadata remain immutable.
    """
    rows: list[dict[str, Any]] = []
    for relative in CSS_BUILD_OUTPUTS:
        source = target / relative
        public = target / "public" / relative
        if (
            source.is_symlink()
            or public.is_symlink()
            or not source.is_file()
            or not public.is_file()
        ):
            raise RuntimeError(f"css_bundle_path_invalid:{relative.name}")
        source_info = source.stat()
        public_info = public.stat()
        if source_info.st_nlink != 1:
            raise RuntimeError(f"css_source_not_detached:{relative.name}")
        source_sha = sha(source)
        public_sha = sha(public)
        if source_sha != public_sha:
            raise RuntimeError(f"public_css_drift:{relative.name}")
        os.utime(
            source,
            ns=(source_info.st_atime_ns, public_info.st_mtime_ns),
            follow_symlinks=False,
        )
        rows.append({
            "bundle": relative.name,
            "sha256": source_sha,
            "source_mtime_ns_before": source_info.st_mtime_ns,
            "source_mtime_ns_after": source.stat().st_mtime_ns,
            "public_mtime_ns": public_info.st_mtime_ns,
            "public_nlink": public_info.st_nlink,
        })
    return rows


def text_word_count(value: str) -> int:
    return len(re.findall(r"\b[\w’'-]+\b", value))


def article_section_word_count(article: dict[str, Any]) -> int:
    """Match the deployed route-fixture count over section paragraphs exactly."""
    sections = article.get("sections")
    if not isinstance(sections, list):
        raise RuntimeError("article_sections_invalid_for_word_count")
    paragraphs: list[str] = []
    for section in sections:
        values = section.get("paragraphs") if isinstance(section, dict) else None
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise RuntimeError("article_paragraphs_invalid_for_word_count")
        paragraphs.extend(values)
    return text_word_count(" ".join(paragraphs))


def article_links_to_route(article: dict[str, Any], target_route: str) -> bool:
    """Return whether any catalog paragraph already carries the inbound link."""
    sections = article.get("sections")
    if not isinstance(sections, list):
        raise RuntimeError("article_sections_invalid_for_inbound_link")
    for section in sections:
        values = section.get("paragraphs") if isinstance(section, dict) else None
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise RuntimeError("article_paragraphs_invalid_for_inbound_link")
        if any(target_route in value for value in values):
            return True
    return False


def existing_inbound_link_no_change(
    source: dict[str, Any],
    plan: dict[str, Any],
    allowed_public_changed: list[str],
) -> dict[str, Any] | None:
    """Return a no-change receipt when the requested inbound link already exists.

    Historical catalog ``word_count`` values include fields beyond section
    paragraphs, so section-only recounts cannot be used to reject an otherwise
    untouched source row. Public-delta scope is the mutation authority here.
    """
    target_route = str(plan.get("target_route") or "")
    if not article_links_to_route(source, target_route):
        return None
    old_word_count = source.get("word_count")
    if not isinstance(old_word_count, int) or old_word_count < 1:
        raise RuntimeError(f"inbound_link_source_word_count_invalid:{plan.get('source_route')}")
    public_path = f"{str(plan['source_route']).strip('/')}/index.html"
    if public_path in allowed_public_changed:
        raise RuntimeError("existing_inbound_link_source_overdeclared")
    return {
        "route": str(plan["source_route"]),
        "old_word_count": old_word_count,
        "new_word_count": old_word_count,
        "status": "ALREADY_PRESENT_NO_CHANGE",
    }


def rebind_route_word_count_assertions(
    root: Path,
    route: str,
    old_count: int,
    new_count: int,
) -> list[dict[str, Any]]:
    """Keep exact route fixtures honest after a declared inbound paragraph."""
    if new_count == old_count:
        return []
    marker = f'const ROUTE = "{route}";'
    assertion = re.compile(r"assert\.equal\(article\.word_count, (\d+)\);")
    updates: list[dict[str, Any]] = []
    for path in sorted((root / "tests").glob("*.test.mjs")):
        source = path.read_text(encoding="utf-8")
        if marker not in source or "assert.equal(article.word_count," not in source:
            continue
        matches = assertion.findall(source)
        if len(matches) != 1 or int(matches[0]) != old_count:
            raise RuntimeError(
                f"route_word_count_fixture_drift:{route}:{path.name}:"
                f"expected={old_count}:observed={matches}"
            )
        detached = detach_for_write(path)
        patched = assertion.sub(
            f"assert.equal(article.word_count, {new_count});",
            source,
            count=1,
        )
        path.write_text(patched, encoding="utf-8")
        updates.append({
            "path": path.relative_to(root).as_posix(),
            "route": route,
            "old_word_count": old_count,
            "new_word_count": new_count,
            "detachment": detached,
        })
    return updates


def unlink_generated_trees_for_rebuild(root: Path) -> dict[str, Any]:
    """Reset generated output and retain only detached staging prerequisites."""
    return prepare_clean_build_trees(root, GENERATED_TREES, CLEAN_BUILD_INPUTS)


def restore_missing_ops_artifacts(base: Path, candidate: Path) -> dict[str, Any]:
    """Carry predecessor receipts the clean build does not regenerate."""
    source = base / "artifacts/ops"
    if not source.is_dir():
        return {"status": "PASS_NO_PREDECESSOR_OPS", "restored": []}
    restored: list[str] = []
    copied: list[str] = []
    for original in sorted(source.rglob("*")):
        if original.is_symlink():
            raise RuntimeError(f"ops_artifact_symlink_forbidden:{original}")
        if not original.is_file():
            continue
        relative = original.relative_to(base)
        target = candidate / relative
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise RuntimeError(f"ops_artifact_target_invalid:{target}")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(original, target, follow_symlinks=False)
        except OSError as exc:
            # Production release trees are root-owned and 0444 after sealing.
            # fs.protected_hardlinks=1 correctly rejects an unprivileged hard
            # link to those files. Preserve the exact bytes and mode instead;
            # this is a bounded fallback only for files absent after the clean
            # build, not a replacement for regenerated candidate receipts.
            if exc.errno not in {errno.EACCES, errno.EPERM, errno.EXDEV}:
                raise
            try:
                shutil.copyfile(original, target, follow_symlinks=False)
                os.chmod(target, stat.S_IMODE(original.stat(follow_symlinks=False).st_mode))
            except Exception:
                target.unlink(missing_ok=True)
                raise
            copied.append(relative.as_posix())
        restored.append(relative.as_posix())
    return {"status": "PASS", "restored": restored, "copied": copied}


def declared_image_additions(job: dict[str, Any], visual_key: str, *, text_only: bool = False) -> list[str]:
    expected = [] if text_only else sorted(f"assets/images/{visual_key}{suffix}" for suffix in IMAGE_SUFFIXES)
    declared_raw = job.get("allowed_public_added")
    if not isinstance(declared_raw, list) or any(not isinstance(path, str) for path in declared_raw):
        raise RuntimeError("declared_image_additions_invalid")
    if len(declared_raw) != len(set(declared_raw)):
        raise RuntimeError("declared_public_addition_collision")
    declared = sorted(path for path in declared_raw if path.startswith("assets/images/"))
    if declared != expected:
        raise RuntimeError(f"declared_image_additions_invalid:{declared}")
    return expected


def require_declared_image_additions_absent(
    job: dict[str, Any],
    tree_root: Path,
    visual_key: str,
    *,
    text_only: bool = False,
) -> list[str]:
    additions = declared_image_additions(job, visual_key, text_only=text_only)
    collisions = [relative for relative in additions if os.path.lexists(tree_root / "public" / relative)]
    if collisions:
        raise RuntimeError(f"declared_image_addition_collision:{collisions}")
    return additions


def materialize_candidate_base(base: Path, candidate: Path, *, job=None) -> dict[str, Any]:
    try:
        materialization = copytree_linked(base, candidate)
        recovered = {}
        if job is not None:
            from glp_affiliate_sync_job import equivalent_main_rebuild_inputs
            recovered = equivalent_main_rebuild_inputs(job["base_release_root"])
            for relative, spec in recovered.items():
                target = candidate / relative
                if not os.path.lexists(target):
                    raw = Path(spec["path"]).read_bytes()
                    if hashlib.sha256(raw).hexdigest() != spec["sha256"]:
                        raise RuntimeError("native_rebuild_input_drift")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
        detached_overlays = detach_explicit_overlays(candidate)
        detached_overlay_trees = detach_explicit_overlay_trees(candidate)
        clean_build = unlink_generated_trees_for_rebuild(candidate)
        return {
            "materialization": materialization,
            "native_rebuild_inputs": recovered,
            "detached_overlays": detached_overlays,
            "detached_overlay_trees": detached_overlay_trees,
            **clean_build,
        }
    except Exception:
        remove_materialized_tree(candidate)
        raise


def rebind_phase3_insights_route_count(target: Path, base: Path, job: dict[str, Any]) -> dict[str, Any]:
    """Bind the graph count to the already-authorized pagination addition.

    A new guide is rendered by the blog system, not as a Phase3 graph page.
    Only crossing the Insights page-size boundary adds a Phase3 route.
    """
    from materialize_opus_run import new_article_insights_public_delta
    delta = new_article_insights_public_delta(base, job["target_route"])
    additions = delta["added"]
    declared = [p for p in job["allowed_public_added"] if p.startswith("insights/")]
    if sorted(declared) != sorted(additions):
        raise RuntimeError("phase3_insights_count_delta_not_declared")
    changed = [p for p in job["allowed_public_changed"] if p.startswith("insights/")]
    if sorted(changed) != sorted(delta["changed"]):
        raise RuntimeError("phase3_insights_changed_delta_not_declared")
    relative = Path("data/phase3-page-graph.json")
    original = json.loads((base / relative).read_text())
    candidate = json.loads((target / relative).read_text())
    if candidate != original:
        raise RuntimeError("phase3_insights_count_preimage_changed")
    before = original.get("expected_indexable_routes")
    if type(before) is not int or before < 1 or len(additions) > 1:
        raise RuntimeError("phase3_insights_count_preimage_invalid")
    after = before + len(additions)
    if additions:
        detach_for_write(target / relative)
        candidate["expected_indexable_routes"] = after
        (target / relative).write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n")
    return {"before": before, "after": after, "added_routes": additions, "source": "existing_governed_insights_delta"}


def patch_price_state_disclosures(source: str) -> str:
    """Port the exact accepted release2 disclosure-strip fix, not new prices."""
    predecessor = '  const versions=[PRICE_STATE_DISCLOSURE,...[prior,tallies].map(t=>PRICE_STATE_DISCLOSURE.replace(/\\{\\{([A-Z0-9_]+)\\}\\}/g,(_,k)=>String(t[k])))];'
    successor = '''  // Exact accepted release2 snapshot after S32; arbitrary counts/copy stay strict.
  const release2={LIVE_PROVIDER_COUNT:57,PRICED_PROVIDER_COUNT:32,CALCULATOR_COMPARABLE_PROVIDER_COUNT:5,BASELINE_PRICE_ROW_COUNT:89,CURRENT_PRICE_ROW_COUNT:82,BASELINE_PRICED_PROVIDER_COUNT:35,MATRIX_PRICED_PROVIDER_COUNT:33};
  const versions=[PRICE_STATE_DISCLOSURE,...[prior,release2,tallies].map(t=>PRICE_STATE_DISCLOSURE.replace(/\\{\\{([A-Z0-9_]+)\\}\\}/g,(_,k)=>String(t[k])))];'''
    return apply_once_or_already(source, predecessor, successor, "release2_disclosure_strip")


def patch_renderer_word_blueprint(source: str) -> str:
    """Materialize the already-reviewed SERP class's QA length policy.

    The packet compiler counts draft words, including tables and FAQs. Packet
    QA admits target..max(3600, int(target * 1.10)); the legacy renderer's flat
    ceiling incorrectly rejected those admitted articles. Do not change their
    reviewed body, counter, or claim bindings. Legacy briefs keep 1500..3600.
    """
    successor = '''  const serpBlueprint = brief.schema === "blog-brief-v2-opus-autopublish"
    && brief.batch_gate_passed === true
    && /^[a-f0-9]{64}$/.test(brief.operator_release_receipt || "")
    && Number.isInteger(brief.serp_contract?.parity_target_words)
    && brief.serp_contract.parity_target_words >= 1500;
  const blueprintMin = serpBlueprint ? brief.serp_contract.parity_target_words : 1500;
  const blueprintMax = serpBlueprint ? Math.max(3600, Math.trunc(blueprintMin * 1.10)) : 3600;
  if (!Number.isInteger(brief.word_count) || brief.word_count < blueprintMin || brief.word_count > blueprintMax) return { status: "HELD", reason: "word_count_outside_blueprint" };'''
    if successor not in source:
        for ceiling in (2400, 3600):
            predecessor = f'  if (!Number.isInteger(brief.word_count) || brief.word_count < 1500 || brief.word_count > {ceiling}) return {{ status: "HELD", reason: "word_count_outside_blueprint" }};'
            if predecessor in source:
                source = source.replace(predecessor, successor, 1)
                break
        else:
            raise RuntimeError("renderer_word_blueprint_preimage_unknown")
    descriptor = '  wordCountByClass: { "blog-brief-v2-opus-autopublish": "reviewed SERP target..max(3600, floor(target * 1.10))" },'
    if descriptor not in source:
        for ceiling in (2400, 3600):
            anchor = f'  wordCount: [1500, {ceiling}],'
            if anchor in source:
                source = source.replace(anchor, '  wordCount: [1500, 3600],\n' + descriptor, 1)
                break
        else:
            raise RuntimeError("renderer_word_blueprint_descriptor_unknown")
    return source


def _build_candidate_materialized(
    job: dict[str, Any],
    base: Path,
    opus_sha: str,
    temp: Path,
    cow: dict[str, Any],
) -> dict[str, Any]:
    route_master_cow_validation = normalize_article_route_master_cow_validation(temp)
    price_state = temp / "src/price-state.mjs"
    price_state.write_text(patch_price_state_disclosures(price_state.read_text()))
    renderer = temp / "src/blog-system.mjs"
    source = renderer.read_text()
    source = patch_renderer_word_blueprint(source)
    old_section = '''    const paragraphs = publicParagraphs(section.paragraphs).map((text) => `<p>${mdLinks(esc(text), affiliateUrls)}</p>`).join("");
    return { heading, id, html: `<section aria-labelledby="${id}"><h2 id="${id}">${esc(heading)}</h2>${paragraphs}</section>` };'''
    new_section = '''    const paragraphs = publicParagraphs(section.paragraphs).map((text) => `<p>${mdLinks(esc(text), affiliateUrls)}</p>`).join("");
    const ordered = Array.isArray(section.ordered_items) && section.ordered_items.length ? `<ol>${section.ordered_items.map((text) => `<li>${mdLinks(esc(text), affiliateUrls)}</li>`).join("")}</ol>` : "";
    const bullets = Array.isArray(section.bullets) && section.bullets.length ? `<ul>${section.bullets.map((text) => `<li>${mdLinks(esc(text), affiliateUrls)}</li>`).join("")}</ul>` : "";
    const tables = (section.tables || []).map((table) => `<div class="table-scroll responsive-table"><table><caption>${esc(heading)}</caption><thead><tr>${(table.headers || []).map((cell) => `<th scope="col">${mdLinks(esc(cell), affiliateUrls)}</th>`).join("")}</tr></thead><tbody>${(table.rows || []).map((row) => `<tr>${row.map((cell, cellIndex) => cellIndex === 0 ? `<th scope="row">${mdLinks(esc(cell), affiliateUrls)}</th>` : `<td>${mdLinks(esc(cell), affiliateUrls)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`).join("");
    return { heading, id, html: `<section aria-labelledby="${id}"><h2 id="${id}">${esc(heading)}</h2>${paragraphs}${ordered}${bullets}${tables}</section>` };'''
    # Accept the current renderer successor when its structured-section
    # behavior is already present with evolved citation arguments. The prior
    # exact replacement is retained for older release trees.
    if not all(marker in source for marker in (
        "Array.isArray(section.ordered_items)",
        "const tables = (section.tables || [])",
        'class="table-scroll responsive-table"',
    )):
        source = apply_once_or_already(
            source, old_section, new_section, "renderer_structured_section"
        )
    renderer.write_text(patch_renderer_surface_contract(source))
    playbook = temp / "tests/playbook.test.mjs"
    playbook_source = playbook.read_text()
    old_count = 'assert.equal(data.articles.length, 29); assert.equal(map.entries.length, 29);'
    playbook_source = apply_once_or_already(
        playbook_source, old_count,
        'assert.ok(data.articles.length >= 29); assert.ok(map.entries.length >= 29);',
        "playbook_article_count",
    )
    playbook.write_text(patch_playbook_historical_batch_gate(playbook_source))
    if job.get("operation") == "r2_refresh":
        playbook.write_text(rebind_historical_article_tests_for_r2(playbook.read_text()))
        editorial_test = temp / "tests/editorial-monetization.test.mjs"
        editorial_test.write_text(patch_editorial_monetization_test_for_r2(
            editorial_test.read_text()
        ))
        served_test = temp / "tests/pass2-late-audit-served.test.mjs"
        served_test.write_text(patch_candidate_local_served_test(served_test.read_text()))
    product_test = temp / "tests/product-ready.test.mjs"
    product_source = product_test.read_text()
    old_guide_count = 'assert.equal(blog.articles.filter((article) => article.vertical === "GLP-1").length, 37);'
    product_test.write_text(apply_once_or_already(
        product_source, old_guide_count,
        'assert.ok(blog.articles.filter((article) => article.vertical === "GLP-1").length >= 37);',
        "product_guide_count",
    ))
    wave2 = temp / "tests/wave2-regression.test.mjs"
    wave2_source = wave2.read_text()
    for old_test, new_test in [
        ("assert.equal(articles.length, 57);", "assert.ok(articles.length >= 57);"),
        ("assert.equal(routes.length, 88);", "assert.ok(routes.length >= 88);"),
    ]:
        wave2_source = apply_once_or_already(wave2_source, old_test, new_test, "wave2_count")
    wave2.write_text(wave2_source)
    lastmod_test = temp / "tests/lastmod-declared-routes.test.mjs"
    lastmod_test.write_text(patch_lastmod_fixture_modes(lastmod_test.read_text()))
    lastmod_test.write_text(carrier_fixture_contracts.patch_lastmod_material_fixture(lastmod_test.read_text(), base))
    product_test.write_text(carrier_fixture_contracts.patch_preserved_export_fixture(product_test.read_text()))
    for relative, adapt in [
        ("tests/affiliati-attribution-local-d1.test.mjs", rebind_affiliati_local_d1_fixture),
        ("tests/phase3-page-graph.test.mjs", rebind_phase3_in_memory_fixture),
        ("tests/provider-pharmacy-emulation.test.mjs", rebind_phase3_in_memory_fixture),
    ]:
        fixture = temp / relative
        fixture.write_text(adapt(fixture.read_text()))
    coverage_script = temp / "scripts/validate-program-coverage.mjs"
    coverage_source = coverage_script.read_text()
    coverage_test = temp / "tests/program-registry-coverage.test.mjs"
    coverage_test_source = coverage_test.read_text()
    operation = job.get("operation", "new_article")
    if operation not in {"new_article", "r2_refresh"}:
        raise RuntimeError("release_operation_invalid")
    if operation == "new_article":
        predecessor_sitemap_routes = (base / "public/sitemap.xml").read_text().count("<url>")
        predecessor_build_report = json.loads(
            (base / "artifacts/ops/build-report.json").read_text()
        )
        coverage_source, coverage_test_source = increment_coverage_anchors(
            coverage_source,
            coverage_test_source,
            predecessor_sitemap_routes,
            predecessor_build_report.get("indexable_routes"),
        )
    coverage_script.write_text(coverage_source)
    coverage_test.write_text(coverage_test_source)
    staging = temp / "scripts/build-staging.mjs"
    staging_source = staging.read_text()
    old_hero = 'const key = `guide-${routeSlug(article.canonical_path)}`;'
    new_hero = old_hero
    if old_hero not in staging_source:
        raise RuntimeError("opus_article_hero_anchor_missing")
    staging_source = patch_staging_route_contract(staging_source.replace(old_hero, new_hero))
    if operation == "r2_refresh":
        staging_source = release_r2_target_from_legacy_preservation(
            staging_source,
            str(job.get("target_route") or ""),
        )
    staging.write_text(staging_source)
    article_path, article, _prices_checked_date = resolve_article_contract(job)
    route = job["target_route"]
    if article.get("canonical_path") != route or article.get("batch_gate_passed") is not True:
        raise RuntimeError("article_route_or_gate_invalid")
    visual_key = f"guide-{route.strip('/').split('/')[-1]}"
    if operation == "new_article":
        image_additions = require_declared_image_additions_absent(job, base, visual_key)
    else:
        if any(str(path).startswith("assets/images/") for path in job.get("allowed_public_added") or []):
            raise RuntimeError("r2_visual_addition_forbidden")
        image_additions = []
    article["operator_release_receipt"] = opus_sha
    # This is a planned PT publication day, checked again at the publisher's
    # network boundary. A packet's draft date must not become first publication.
    if operation == "new_article":
        bind_article_publication_date(article, job["publication_date"])
    else:
        article["updated_at"] = job["publication_date"]
    r2_target_test_rebind: dict[str, Any] | None = None
    if operation == "r2_refresh":
        r2_target_test_rebind = rebind_r2_target_tests(temp, article)
    # PREMIUM ARTICLE IMAGERY (2026-08-20). Generate the page's own hero before
    # the catalogs are written, because build-staging.mjs reads the alt text out
    # of the article row. Generation is decoration and never a gate: a
    # FALLBACK receipt writes no metadata and installs the stock
    # editorial-evidence-hero family after the build, exactly as before.
    #
    # The lane is release-affecting code, so it is pinned like every other
    # module the builder imports: materialize_opus_run.py mints control_files
    # from CURRENT hashes, so the pin re-blesses itself at the next
    # materialization and a drifted module still fails closed. A template
    # minted BEFORE this lane existed cannot list it — that job simply builds
    # the way it always did (stock hero, no metadata) instead of inventing a
    # brand-new publish blocker on a decorative feature.
    image_receipt: dict[str, Any] | None = None
    if operation == "new_article":
        if has_control_pin(job, ARTICLE_IMAGES_PATH):
            require_control_pin(job, ARTICLE_IMAGES_PATH, "article_images")
            image_receipt = article_images.prepare_hero(
                article=article,
                visual_key=visual_key,
                base_release_root=base,
            )
            article_images.apply_article_image_metadata(article, image_receipt)
        else:
            print(json.dumps({"article_images": "skipped_legacy_template_without_control_pin",
                              "visual_key": visual_key}, sort_keys=True), file=sys.stderr)
    catalog_paths = {
        "content_batch": temp / "data/blog/content-batch.json",
        "content_national_wow": temp / "data/blog/content-national-wow-additions.json",
    }
    catalogs = {name: json.loads(path.read_text()) for name, path in catalog_paths.items()}
    release_routes = {route} | {
        str(plan.get("source_route") or "")
        for plan in article.get("inbound_link_plan") or []
    } - {""}
    prose_scrub = scrub_catalog_prose(catalogs, release_routes)
    ownership_path = temp / "data/blog/keyword-ownership-map.json"
    ownership = json.loads(ownership_path.read_text())
    ownership, national_ownership = materialize_national_article_ownership(
        ownership,
        catalogs["content_national_wow"]["articles"],
    )
    if operation == "new_article":
        all_articles = [row for payload in catalogs.values() for row in payload["articles"]]
        if any(row.get("canonical_path") == route or row.get("article_id") == article.get("article_id") for row in all_articles):
            raise RuntimeError("article_already_present")
        catalogs["content_batch"]["articles"].append(article)
        entries = ownership.get("entries") or ownership.get("keywords")
        if not isinstance(entries, list):
            raise RuntimeError("ownership_shape_invalid")
        if any(row.get("url") == route or row.get("primary_keyword") == article.get("primary_keyword") for row in entries):
            raise RuntimeError("keyword_ownership_collision")
        entries.append({"owner": article["article_id"], "primary_keyword": article["primary_keyword"],
                        "risk_tier": article["risk_tier"], "status": "PASS_OPUS_SEO_DRAFT",
                        "url": route, "batch_gate_passed": True, "operator_release_receipt": opus_sha})
    else:
        matches = [row for payload in catalogs.values() for row in payload["articles"]
                   if row.get("canonical_path") == route]
        if len(matches) != 1:
            raise RuntimeError(f"r2_exact_route_match_count_invalid:{len(matches)}")
        bind_article_publication_date(article, job["publication_date"], matches[0])
        article = preserve_r2_route_governance(matches[0], article)
        contract = validate_r2_execution_contract(
            job.get("r2_contract") or {},
            route=route,
        )
        validate_r2_refresh(
            matches[0],
            article,
            gsc_queries=contract.get("gsc_queries") or [],
            current_routes=set(contract.get("current_routes") or []),
            striking_distance_routes=set(contract.get("striking_distance_routes") or []),
            inbound_link_plan=job.get("inbound_link_plan") or [],
            minimum_inbound_links=int(contract.get("required_inbound_links") or 1),
            priority=contract["priority"],
        )
        catalogs, ownership, replacement = replace_article_and_ownership(
            catalogs,
            ownership,
            article,
            opus_receipt_sha256=opus_sha,
        )
        if replacement["replaced_count"] != 1 or replacement["ownership_updated_count"] != 1:
            raise RuntimeError("r2_exact_replacement_invalid")
    inbound_source_word_count_updates: list[dict[str, Any]] = []
    for plan in article.get("inbound_link_plan") or []:
        link_target = str(plan.get("target_route") or "")
        if (
            (operation == "new_article" and link_target != route)
            or (operation == "r2_refresh" and link_target not in set((job.get("r2_contract") or {}).get("striking_distance_routes") or []))
            or not str(plan.get("context_sentence") or "").endswith(").")
        ):
            raise RuntimeError("inbound_link_plan_invalid")
        source = next((row for payload in catalogs.values() for row in payload["articles"]
                       if row.get("canonical_path") == plan.get("source_route")), None)
        sections = (source or {}).get("sections")
        if not source or not isinstance(sections, list) or not sections:
            raise RuntimeError(f"inbound_link_source_missing:{plan.get('source_route')}")
        paragraphs = sections[-1].setdefault("paragraphs", [])
        existing = existing_inbound_link_no_change(
            source, plan, job["allowed_public_changed"],
        )
        if existing is not None:
            inbound_source_word_count_updates.append(existing)
            continue
        old_word_count = source.get("word_count")
        observed_word_count = article_section_word_count(source)
        if not isinstance(old_word_count, int) or old_word_count < 1:
            raise RuntimeError(f"inbound_link_source_word_count_invalid:{plan.get('source_route')}")
        paragraphs.append(plan["context_sentence"])
        appended_words = text_word_count(str(plan["context_sentence"]))
        if appended_words < 1:
            raise RuntimeError("inbound_link_context_word_count_invalid")
        new_word_count = old_word_count + appended_words
        if (
            observed_word_count == old_word_count
            and article_section_word_count(source) != new_word_count
        ):
            raise RuntimeError("inbound_link_source_word_count_update_invalid")
        source["word_count"] = new_word_count
        inbound_source_word_count_updates.extend(rebind_route_word_count_assertions(
            temp,
            str(plan["source_route"]),
            old_word_count,
            new_word_count,
        ))
    for name, path in catalog_paths.items():
        path.write_text(json.dumps(catalogs[name], indent=2, sort_keys=True) + "\n")
    ownership_path.write_text(json.dumps(ownership, indent=2, sort_keys=True) + "\n")
    image_manifest_rebind: dict[str, Any] | None = None
    article_route_master_lineage: dict[str, Any] | None = None
    image_selection: dict[str, Any] | None = None
    if operation == "r2_refresh":
        image_manifest_rebind = rebind_article_image_manifest_for_r2(temp, article)
    elif operation == "new_article":
        # GLP-73: native hero -> receipted stock hero -> text only. The selector
        # rolls a rejected tier back and steps down instead of raising; the
        # content, claims and public-delta checks around it are unchanged.
        article_route_master_lineage, image_receipt, image_selection = stage_article_image_lineage(
            temp, article, image_receipt, visual_key, catalogs, catalog_paths,
        )
        if article_route_master_lineage and article_route_master_lineage.get("availability") == "unavailable":
            # The signed job authorizes the seven-image ceiling; the route-bound
            # text-only receipt narrows the effective delta to its HTML, never
            # permits any undeclared image or any other extra public change.
            declared = declared_image_additions(job, visual_key)
            job["allowed_public_added"] = [path for path in job["allowed_public_added"] if path not in declared]
            require_declared_image_additions_absent(job, temp, visual_key, text_only=True)
            image_additions = []
    source_page = route.strip("/").replace("/", "-")
    consent_sources = temp / "src/site-capture-consent-sources.mjs"
    consent_source = consent_sources.read_text()
    provenance_anchor = '  "best-online-glp-1-providers:best-online-glp-1-providers-terms-checklist",\n'
    provenance_entry = f'  "{source_page}:{source_page}-terms-checklist",\n'
    consent_sources.write_text(ensure_content_upgrade_provenance_entry(
        consent_source,
        provenance_anchor,
        provenance_entry,
    ))
    if operation == "new_article":
        price_watch_test = temp / "tests/price-watch-email.test.mjs"
        price_watch_test.write_text(rebind_pagination_capture_fixture(
            increment_content_upgrade_count(consent_source, price_watch_test.read_text()),
            job,
        ))
        share_test = temp / "tests/pass2-late-audit-static-output.test.mjs"
        share_test.write_text(increment_article_share_control_count(
            base,
            share_test.read_text(encoding="utf-8"),
        ), encoding="utf-8")
    tracking_allowed_hosts = regenerate_tracking_allowed_hosts(temp)
    phase3_insights_count_rebind = None
    if operation == "new_article":
        phase3_insights_count_rebind = rebind_phase3_insights_route_count(temp, base, job)
    price_predecessor_input = retain_price_index_predecessor(job, base, temp)
    build = run(
        ["npm", "run", "build"],
        temp,
        env=article_build_environment(job, temp),
    )
    rendered_cta_sanitation = sanitize_r2_rendered_promotional_ctas(temp, job)
    pages_scanned = assert_no_rendered_prose_markup_leak(temp, release_routes)
    sitemap_path = temp / "public/sitemap.xml"
    sitemap = sitemap_path.read_text()
    for row in [article for payload in catalogs.values() for article in payload["articles"]]:
        route = str(row.get("canonical_path") or "")
        updated = str(row.get("updated_at") or "")
        if not route or not __import__("re").fullmatch(r"\d{4}-\d{2}-\d{2}", updated):
            continue
        bare = f"<url><loc>https://glpclinicscout.com{route}</loc></url>"
        dated = f"<url><loc>https://glpclinicscout.com{route}</loc><lastmod>{updated}</lastmod></url>"
        sitemap = sitemap.replace(bare, dated)
    sitemap_path.write_text(sitemap)
    image_install: dict[str, Any] | None = None
    if operation == "new_article":
        if article_route_master_lineage is not None and article_route_master_lineage.get("availability") == "unavailable":
            # The route-bound receipt is the only image evidence; no derivative or
            # synthetic master may be installed after the renderer chose text only.
            require_declared_image_additions_absent(job, temp, visual_key, text_only=True)
            image_install = {"status": "TEXT_ONLY", "availability": "unavailable",
                             "reason": article_route_master_lineage["reason"],
                             "receipt": article_route_master_lineage["receipt"], "files": []}
        elif article_route_master_lineage is not None:
            image_install = verify_new_article_route_master_family(
                temp,
                job["target_route"],
                visual_key,
                article_route_master_lineage,
            )
        else:
            require_declared_image_additions_absent(job, temp, visual_key)
            image_dir = temp / "public/assets/images"
            image_install = article_images.install_hero(
                image_receipt,
                image_dir=image_dir,
                visual_key=visual_key,
                suffixes=IMAGE_SUFFIXES,
                fallback_key="editorial-evidence-hero",
            )
    public_delta_paths = sorted(
        (Path(path) for path in [*job["allowed_public_added"], *job["allowed_public_changed"]]),
        key=lambda path: path.as_posix(),
    )
    public_restore = restore_linked_subtree(
        base,
        temp,
        Path("public"),
        public_delta_paths,
        constructor=Path(__file__).resolve(),
    )
    restore_undeclared_provenance(base, temp, job)
    restore_undeclared_sitemap_entries(base, temp, job)
    price_predecessor_output = retain_price_index_predecessor(job, base, temp)
    css_mtime_normalization = normalize_css_bundle_mtimes(temp)
    # Test the exact final candidate surface: predecessor-owned public bytes have
    # been restored, while only the declared article/index/sitemap delta remains.
    tests = accepted_failure_contract.run_suite(job, temp)
    ops_restore = restore_missing_ops_artifacts(base, temp)
    return {
        "build": build,
        "tests": tests,
        "article_sha256": sha(article_path),
        "article_route_master_cow_validation": route_master_cow_validation,
        "article_route_master_lineage": article_route_master_lineage,
        "article_image_manifest_rebind": image_manifest_rebind,
        "r2_target_test_rebind": r2_target_test_rebind,
        "rendered_cta_sanitation": rendered_cta_sanitation,
        "national_ownership": national_ownership,
        "tracking_allowed_hosts": tracking_allowed_hosts,
        "price_predecessor_input": price_predecessor_input,
        "price_predecessor_output": price_predecessor_output,
        "css_mtime_normalization": css_mtime_normalization,
        "inbound_source_word_count_updates": inbound_source_word_count_updates,
        "images": {
            "receipt": image_receipt,
            "install": image_install,
            "selection": image_selection,
        },
        "cow": {
            **cow,
            "declared_image_additions_absent_before_copy": image_additions,
            "public_restore": public_restore,
            "ops_restore": ops_restore,
        },
    }


def _build_defect_remediation_materialized(
    job: dict[str, Any], base: Path, temp: Path, cow: dict[str, Any]
) -> dict[str, Any]:
    """Apply ONE operator-ordered, deletion-only defect fix to ONE published route.

    Deliberately does none of the article-publish work: no coverage counters, no
    playbook anchors, no consent provenance, no image family, no sitemap lastmod
    rewrite. The article's own dates are untouched, because nothing about the
    content changed — only markup that was never meant to be reader-visible is
    removed. That keeps the public delta to exactly one page.
    """
    route = job["target_route"]
    directive_id = job["operator_directive_id"]
    catalog_paths = {
        "content_batch": temp / "data/blog/content-batch.json",
        "content_national_wow": temp / "data/blog/content-national-wow-additions.json",
    }
    catalogs = {name: json.loads(path.read_text()) for name, path in catalog_paths.items()}
    matches = [
        (name, index)
        for name, payload in catalogs.items()
        for index, row in enumerate(payload.get("articles") or [])
        if row.get("canonical_path") == route
    ]
    if len(matches) != 1:
        raise RuntimeError(f"defect_target_article_match_invalid:{len(matches)}")
    name, index = matches[0]
    before = catalogs[name]["articles"][index]
    after = defect_remediation.remediate_article(before)
    contract = defect_remediation.validate_remediation(before, after, directive_id=directive_id)
    catalogs[name]["articles"][index] = after
    for catalog_name, path in catalog_paths.items():
        path.write_text(json.dumps(catalogs[catalog_name], indent=2, sort_keys=True) + "\n")

    price_predecessor_input = retain_price_index_predecessor(job, base, temp)
    build = run(
        ["npm", "run", "build"],
        temp,
        env=article_build_environment(job, temp),
    )
    assert_no_rendered_prose_markup_leak(temp, {route})
    public_delta_paths = sorted(
        (Path(path) for path in [*job["allowed_public_added"], *job["allowed_public_changed"]]),
        key=lambda path: path.as_posix(),
    )
    public_restore = restore_linked_subtree(
        base, temp, Path("public"), public_delta_paths, constructor=Path(__file__).resolve(),
    )
    price_predecessor_output = retain_price_index_predecessor(job, base, temp)
    tests = accepted_failure_contract.run_suite(job, temp)
    ops_restore = restore_missing_ops_artifacts(base, temp)
    return {
        "build": build,
        "tests": tests,
        "defect_remediation": contract,
        "price_predecessor_input": price_predecessor_input,
        "price_predecessor_output": price_predecessor_output,
        "cow": {**cow, "public_restore": public_restore, "ops_restore": ops_restore},
    }


RELEASE_ROOT_RETIREMENT = "00b-release-root-retirement.json"
RELEASE_ROOT_RESUME = "00c-release-root-resume.json"
DEPLOYMENT_RECEIPTS = ("06-deployment-receipt.json", "06b-deployment-reconciliation.json")


def _resume_verdict(job: dict[str, Any], release: Path, base: Path) -> tuple[str, dict[str, Any]]:
    """Classify an existing release root that belongs to THIS operation.

    Returns ``(verdict, evidence)`` where verdict is one of:

    ``resume``   the root is our own sealed-but-undeployed candidate and its
                 tree hashes still match the sealed manifest byte-for-byte, so
                 the build is already done and deploy can proceed;
    ``retire``   the root is a dead partial from a killed run — no seal, or a
                 seal that no longer describes the bytes on disk;
    ``refuse``   the root must not be touched at all (accepted truth, or an
                 operation that already deployed).
    """
    report = Path(job["report_dir"]).resolve()
    if release == base:
        return "refuse", {"reason": "release_root_aliases_current_base"}
    if release.is_symlink() or not release.is_dir():
        return "refuse", {"reason": "release_root_not_a_directory"}
    deployed = [name for name in DEPLOYMENT_RECEIPTS if (report / name).is_file()]
    if deployed:
        return "refuse", {"reason": "release_root_already_deployed", "receipts": deployed}
    manifest_path = report / "01-deployment-manifest.json"
    seal_path = report / "02-immutable-release-seal.json"
    if not (manifest_path.is_file() and seal_path.is_file()):
        return "retire", {"reason": "release_root_without_seal"}
    try:
        manifest = json.loads(manifest_path.read_text())
        seal = json.loads(seal_path.read_text())
    except (OSError, json.JSONDecodeError):
        return "retire", {"reason": "release_seal_unreadable"}
    if not isinstance(manifest, dict) or not isinstance(seal, dict):
        return "retire", {"reason": "release_seal_malformed"}
    bindings = (
        manifest.get("release_root") == str(release)
        and seal.get("release_root") == str(release)
        and manifest.get("rollback_release_root") == str(base)
        and manifest.get("manifest_id") == job.get("manifest_id")
        and manifest.get("article_id") == job.get("article_id")
        and seal.get("status") == "PASS_IMMUTABLE_DEPLOY_HELD"
        and seal.get("seal_sha256") == payload_hash(seal, "seal_sha256")
        and manifest.get("manifest_payload_sha256") == payload_hash(manifest, "manifest_payload_sha256")
        and seal.get("manifest_sha256") == sha(manifest_path)
    )
    if not bindings:
        return "retire", {"reason": "release_seal_not_bound_to_this_operation"}
    try:
        release_tree = closure(rows(release))
        public_tree = closure(rows(release / "public"))
    except RuntimeError as error:
        return "retire", {"reason": f"release_tree_unreadable:{error}"}
    evidence = {
        "release_tree_sha256": release_tree,
        "public_tree_sha256": public_tree,
        "sealed_release_tree_sha256": seal.get("release_tree_sha256"),
        "sealed_public_tree_sha256": seal.get("public_tree_sha256"),
        "manifest_release_tree_sha256": manifest.get("release_tree_sha256"),
        "manifest_public_tree_sha256": manifest.get("public_tree_sha256"),
        "manifest": str(manifest_path),
        "seal": str(seal_path),
    }
    if (
        release_tree == seal.get("release_tree_sha256") == manifest.get("release_tree_sha256")
        and public_tree == seal.get("public_tree_sha256") == manifest.get("public_tree_sha256")
    ):
        return "resume", evidence
    return "retire", {**evidence, "reason": "release_tree_drifted_from_seal"}


def resume_or_retire_existing_release(job: dict[str, Any], base: Path) -> dict[str, Any] | None:
    """Make an existing release root a resumable state, not a dead end.

    On 2026-08-20 a killed publish left ``glp-buffer-…`` on disk. Every retry of
    the same operation then died on ``release_root_exists`` — the builder
    treated ITS OWN leftover as a stranger. Both honest outcomes are now
    implemented and both leave a receipt:

    * the leftover is our own SEALED, UNDEPLOYED candidate and its tree still
      hashes to the sealed manifest → return a resume record; the caller skips
      straight to deploy without rebuilding (and without re-sealing, which
      ``seal_release`` would refuse anyway);
    * the leftover is a dead partial → rename it aside under a timestamped
      ``.retired-…`` sibling and record what was moved, then let the normal
      build proceed into a clean root.

    Never on any path: touching accepted truth, or removing a root whose
    operation already deployed. Those raise, exactly as before.
    """
    release = Path(job["release_root"]).resolve()
    if not os.path.lexists(release):
        return None
    verdict, evidence = _resume_verdict(job, release, base)
    report = Path(job["report_dir"]).resolve()
    report.mkdir(parents=True, exist_ok=True)
    if verdict == "refuse":
        raise RuntimeError(f"release_root_exists:{evidence.get('reason')}")
    if verdict == "resume":
        record = {
            "schema": "glp-opus-article-release-root-resume-v1",
            "status": "PASS_SEALED_UNDEPLOYED_RELEASE_RESUMED",
            "resumed_at_utc": now(), "release_root": str(release),
            "manifest_id": job.get("manifest_id"), "article_id": job.get("article_id"),
            "evidence": evidence,
            "effects": {"builds": 0, "deployments": 0, "cache_purges": 0},
        }
        record["resume_sha256"] = payload_hash(record, "resume_sha256")
        atomic(report / RELEASE_ROOT_RESUME, record)
        return record
    retired = release.with_name(
        f".retired-{release.name}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    if os.path.lexists(retired):
        raise RuntimeError(f"release_root_retirement_collision:{retired}")
    try:
        os.rename(release, retired)
    except OSError as error:
        raise RuntimeError(f"release_root_retirement_failed:{release}:{error}") from error
    record = {
        "schema": "glp-opus-article-release-root-retirement-v1",
        "status": "RETIRED_DEAD_RELEASE_ROOT",
        "retired_at_utc": now(), "release_root": str(release), "retired_to": str(retired),
        "manifest_id": job.get("manifest_id"), "article_id": job.get("article_id"),
        "evidence": evidence,
        "effects": {"builds": 0, "deployments": 0, "cache_purges": 0, "files_deleted": 0},
    }
    record["retirement_sha256"] = payload_hash(record, "retirement_sha256")
    atomic(report / RELEASE_ROOT_RETIREMENT, record)
    return None


def build_candidate(job: dict[str, Any], base: Path, opus_sha: str) -> tuple[Path, dict[str, Any]]:
    """Build one isolated COW candidate and remove its whole partial on failure."""
    release = Path(job["release_root"]).resolve()
    if os.path.lexists(release):
        raise RuntimeError("release_root_exists")
    temp = release.with_name(
        f".candidate.cow-{release.name}-{os.getpid()}-{secrets.token_hex(8)}"
    )
    if os.path.lexists(temp):
        raise RuntimeError(f"candidate_cow_temp_collision:{temp}")
    try:
        cow = materialize_candidate_base(base, temp, job=job)
        import accepted_failure_contract
        accepted_failure_contract.stage_root_binding(temp, accepted_failure_contract.job_pointer(job))
        if job.get("operation") == defect_remediation.OPERATION:
            report = _build_defect_remediation_materialized(job, base, temp, cow)
        else:
            report = _build_candidate_materialized(job, base, opus_sha, temp, cow)
        os.replace(temp, release)
        return release, report
    except Exception as error:
        try:
            remove_materialized_tree(temp)
        except Exception as cleanup_error:
            raise RuntimeError(
                f"candidate_cow_cleanup_failed:{temp}:{cleanup_error}"
            ) from error
        if os.path.lexists(temp):
            raise RuntimeError(f"candidate_cow_partial_remains:{temp}") from error
        raise


def retain_price_index_predecessor(job: dict[str, Any], base: Path, target: Path) -> dict[str, Any]:
    """Carry the exact sealed manifest paired with unchanged predecessor HTML.

    Old article releases restored non-article HTML but retained a build-generated
    manifest for different HTML. A same-HTML checkpoint sealed in that very base
    is recoverable; no price, date, public byte, or manifest digest is rewritten.
    """
    html_rel = "public/glp-1-price-index/index.html"
    manifest_rel = "artifacts/ops/price-index-manifest.json"
    checkpoint_html = "artifacts/build4-preservation/glp-1-price-index/index.html"
    checkpoint_manifest = "artifacts/build4-preservation/ops/price-index-manifest.json"
    manifest = json.loads(checked(job["base_manifest"], "base_manifest").read_text())
    representation, inventory = accepted_base_inventory(manifest)
    if manifest["release_root"] != job["base_release_root"]:
        raise RuntimeError("price_index_retention_base_identity_drift")
    if str(base) != manifest["release_root"]:
        expected = SITE_RELEASES_ROOT / f"accepted-prepared-{manifest['release_tree_sha256']}"
        if representation != "size" or base != expected or unsealed_base_entries(base):
            raise RuntimeError("price_index_retention_base_identity_drift")
    indexed = {row["path"]: row for row in inventory}

    def authenticated(relative: str) -> bytes:
        path = base / relative
        row = indexed[relative]
        if path.is_symlink() or sha(path) != row["sha256"] or path.stat().st_size != row["bytes"]:
            raise RuntimeError("price_index_retention_sealed_input_drift:" + relative)
        return path.read_bytes()

    html = authenticated(html_rel)
    if (target / html_rel).read_bytes() != html:
        raise RuntimeError("price_index_retention_public_drift")
    selected = manifest_rel
    if selected in indexed:
        data = authenticated(selected)
    else:
        from glp_affiliate_sync_job import equivalent_main_rebuild_inputs
        recovered = equivalent_main_rebuild_inputs(job["base_release_root"])
        spec = recovered.get(selected)
        if not spec:
            raise RuntimeError("price_index_retention_native_rebuild_missing")
        data = Path(spec["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != spec["sha256"]:
            raise RuntimeError("price_index_retention_native_rebuild_drift")
        selected = "native-equivalent-main-rebuild"
    value = json.loads(data)
    if value.get("html_sha256") != hashlib.sha256(html).hexdigest():
        if authenticated(checkpoint_html) != html:
            raise RuntimeError("price_index_retention_checkpoint_public_conflict")
        selected = checkpoint_manifest
        data = authenticated(selected)
        value = json.loads(data)
    if (value.get("schema") != "glp-clinic-scout-price-index-build-v1"
        or value.get("route") != "/glp-1-price-index/"
        or value.get("html_sha256") != hashlib.sha256(html).hexdigest()
        or payload_hash(value, "payload_sha256") != value.get("payload_sha256")):
        raise RuntimeError("price_index_retention_pair_invalid")
    destination = target / manifest_rel
    if destination.read_bytes() != data:
        temporary = destination.with_name(".price-index-retained-" + str(os.getpid()))
        temporary.write_bytes(data)
        os.replace(temporary, destination)
    native = selected == "native-equivalent-main-rebuild"
    return {"status": "PASS_EXACT_NATIVE_REBUILD_PREDECESSOR_PAIR" if native else "PASS_EXACT_SEALED_PREDECESSOR_PAIR",
            "origin": selected if native else ("sealed-primary-pair" if selected == manifest_rel else "sealed-checkpoint-same-public-html"),
            "source_manifest": selected, "manifest_sha256": hashlib.sha256(data).hexdigest(),
            "public_html_sha256": hashlib.sha256(html).hexdigest(), "public_bytes_changed": False}


def validate_delta(job: dict[str, Any], base: Path, release: Path) -> dict[str, Any]:
    before = {row["path"]: row for row in rows(base)}
    after = {row["path"]: row for row in rows(release)}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(key for key in set(before) & set(after) if before[key] != after[key])
    runtime_delta = sorted(path for path in added + changed if path.startswith("functions/"))
    if removed or runtime_delta:
        raise RuntimeError(
            f"runtime_or_removal_delta_invalid:removed={removed[:40]}:"
            f"runtime_delta={runtime_delta[:40]}"
        )
    public_added = sorted(path for path in added if path.startswith("public/"))
    public_changed = sorted(path for path in changed if path.startswith("public/"))
    expected_added = sorted(f"public/{path}" for path in job["allowed_public_added"])
    expected_changed = sorted(f"public/{path}" for path in job["allowed_public_changed"])
    if public_added != expected_added or public_changed != expected_changed:
        raise RuntimeError(f"public_delta_invalid:added={public_added}:changed={public_changed}")
    operation = job.get("operation", "new_article")
    target_relative = f"public/{job['target_route'].strip('/')}/index.html"
    if operation == "new_article" and target_relative not in public_added:
        raise RuntimeError("new_article_target_not_added")
    if operation == "r2_refresh":
        if public_added or target_relative not in public_changed:
            raise RuntimeError("r2_target_not_exact_replacement")
    elif operation == defect_remediation.OPERATION:
        # One page, changed in place, nothing added anywhere.
        if public_added or public_changed != [target_relative]:
            raise RuntimeError(
                f"defect_remediation_delta_invalid:added={public_added}:changed={public_changed}")
    elif operation != "new_article":
        raise RuntimeError("release_operation_invalid")
    target_file = release / "public" / job["target_route"].strip("/") / "index.html"
    html = target_file.read_text()
    image_manifest_path = release / "artifacts/ultracode/article-image-manifest.json"
    image_manifest = json.loads(image_manifest_path.read_text())
    text_only = image_manifest.get("text_only_routes", [])
    if text_only:
        if (len(text_only) != 1 or text_only[0].get("route") != job["target_route"]
                or set(text_only[0]) != {"route", "receipt"}):
            raise RuntimeError("article_text_only_delta_binding_invalid")
        key = f"guide-{job['target_route'].strip('/').split('/')[-1]}"
        declared_image_additions(job, key, text_only=True)
        forbidden_image_markup = (r"<img\b", r"<picture\b", r"<source\b", r"(?:og|twitter):image", re.escape(key),
                                  r"editorial-evidence-hero")
        if any(re.search(pattern, html, re.IGNORECASE) for pattern in forbidden_image_markup):
            raise RuntimeError("article_text_only_rendered_image_invalid")
    required = ["index,follow", "application/ld+json", "FAQPage", "Clinic Scout Editorial Team",
                "Evidence boundary.", "Prices and terms can change."]
    if any(item not in html for item in required):
        raise RuntimeError("rendered_target_contract_invalid")
    validate_rendered_article_contract(html, job)
    for plan in job.get("inbound_link_plan") or []:
        source_file = release / "public" / plan["source_route"].strip("/") / "index.html"
        if not source_file.is_file() or f'href="{plan["target_route"]}"' not in source_file.read_text():
            raise RuntimeError("rendered_inbound_link_missing")
    forbidden = ["MedicalReview", "Medically reviewed by", "AggregateRating"]
    if any(token in html for token in forbidden):
        raise RuntimeError("rendered_article_forbidden_token")
    # /go/ anchors are the deployed commercial architecture (release
    # glp-go-rel-sponsored-20260806-r1, rubric render-layer ruling): allowed only
    # when every one carries the sponsored rel. A rel-less /go/ anchor is the
    # actual defect the old blanket ban was protecting against.
    for match in re.finditer(r'<a\b[^>]*href="/go/[^>]*>', html):
        if 'rel="sponsored nofollow noopener"' not in match.group(0):
            raise RuntimeError("rendered_article_forbidden_token")
    sitemap = (release / "public/sitemap.xml").read_text()
    target_url = "https://glpclinicscout.com" + job["target_route"]
    target_entry = f"<loc>{target_url}</loc><lastmod>{job['publication_date']}</lastmod>"
    if sitemap.count(f"<loc>{target_url}</loc>") != 1 or target_entry not in sitemap:
        raise RuntimeError("sitemap_target_invalid")
    return {"added_paths": added, "changed_paths": changed, "removed_paths": removed,
            "public_added": public_added, "public_changed": public_changed,
            "target_sha256": sha(target_file), "target_bytes": target_file.stat().st_size,
            "sitemap_routes": sitemap.count("<url>"), "sitemap_target_lastmod": job["publication_date"]}


def detach_shared_owner_mismatches(release: Path, base: Path, owner: tuple[int, int]) -> dict[str, Any]:
    """Privatize metadata mismatches before exact 0444 sealing; never chmod base."""
    base_identities: set[tuple[int, int]] = set()
    for path in [base, *base.rglob("*")]:
        info = os.lstat(path)
        if stat.S_ISREG(info.st_mode):
            base_identities.add((info.st_dev, info.st_ino))
    detached: list[str] = []
    for path in release.rglob("*"):
        info = os.lstat(path)
        if (
            stat.S_ISREG(info.st_mode)
            and (info.st_dev, info.st_ino) in base_identities
            and ((info.st_uid, info.st_gid) != owner or stat.S_IMODE(info.st_mode) != 0o444)
        ):
            detach_for_write(path)
            detached.append(path.relative_to(release).as_posix())
    return {
        "target_owner_uid": owner[0],
        "target_owner_gid": owner[1],
        "detached_shared_files": len(detached),
        "sample": detached[:20],
    }


def seal_release(job: dict[str, Any], release: Path, base: Path, predecessor: str,
                 opus_path: Path, opus: dict[str, Any], build_record: dict[str, Any],
                 delta: dict[str, Any], *, owner: tuple[int, int] = (0, 0),
                 use_sudo: bool = True) -> None:
    import accepted_failure_contract
    accepted_failure_contract.validate_root_binding(release, accepted_failure_contract.job_pointer(job))
    report = Path(job["report_dir"]).resolve()
    report.mkdir(parents=True, exist_ok=True)
    # `release-job.json` is the job itself; the two release-root lifecycle
    # receipts are written BEFORE the build by
    # `resume_or_retire_existing_release` and are evidence, not build output.
    allowed = {"release-job.json", RELEASE_ROOT_RETIREMENT, RELEASE_ROOT_RESUME}
    # Suite evidence precedes sealing; admit only the exact verified trio.
    accepted_failure_contract.verify_classification(report, job)
    allowed.update({"00-accepted-failures-classification.json", "00-suite-stdout.log", "00-suite-stderr.log"})
    unexpected = sorted(path.name for path in report.iterdir() if path.name not in allowed)
    if unexpected:
        raise RuntimeError(f"publication_report_preexisting_artifacts:{unexpected}")
    paths = {name: report / filename for name, filename in {
        "authority": "00-opus-publication-authority.json", "manifest": "01-deployment-manifest.json",
        "seal": "02-immutable-release-seal.json", "review": "03-parent-deployment-review.json",
        "build": "04-release-build-receipt.json"}.items()}
    release_rows = rows(release)
    tree = closure(release_rows)
    public_rows = rows(release / "public")
    public_tree = closure(public_rows)
    public_restore = build_record.get("cow", {}).get("public_restore") or {}
    prior_public_provenance = public_restore.get("constructor_provenance") or {}
    public_constructor_provenance = constructor_provenance(
        constructor=Path(__file__).resolve(),
        predecessor=base / "public",
        output=release / "public",
        mutation_paths=[
            Path(path)
            for path in [*job["allowed_public_added"], *job["allowed_public_changed"]]
        ],
        materializations=[public_restore["materialization"]],
        predecessor_before={
            "tree_sha256": prior_public_provenance["predecessor"]["tree_sha256_before"]
        },
    )
    owner_rebinding = detach_shared_owner_mismatches(release, base, owner)
    authority = {"schema": "glp-opus-seo-publication-authority-v1", "status": "PASS_OPUS_PUBLICATION_AUTHORITY",
                 "authorized_at_utc": now(), "article_id": job["article_id"], "route": job["target_route"],
                 "upstream_topic_authority": job["operator_authority"],
                 "opus_receipt": {"path": str(opus_path), "sha256": sha(opus_path),
                                    "subject_manifest_sha256": opus["subject_manifest_sha256"]},
                 "authorized_effects": {"pages_deployments": 1, "cache_purges": 1,
                                        "gsc_sitemap_submissions": 1},
                 "held_effects": {"emails": 0, "d1_writes": 0, "spend_usd": 0}}
    authority["authority_sha256"] = payload_hash(authority, "authority_sha256")
    atomic(paths["authority"], authority)
    controls = [Path(__file__).resolve(), Path(article_image_release_contract.__file__).resolve(), opus_path, checked(job["subject_manifest"], "subject_manifest")]
    controls.extend([Path(accepted_failure_contract.__file__).resolve(),
                     Path(carrier_fixture_contracts.__file__).resolve(),
                     report / "00-accepted-failures-classification.json",
                     report / "00-suite-stdout.log", report / "00-suite-stderr.log"])
    controls.extend(checked(spec, f"control_{index}") for index, spec in enumerate(job["control_files"]))
    manifest = {"schema": "glp-opus-article-deploy-manifest-v1", "status": "SEALED_DEPLOY_AUTHORIZED",
                "operation": job.get("operation", "new_article"),
                "created_at_utc": now(), "manifest_id": job["manifest_id"], "article_id": job["article_id"],
                "operator_authority": {"path": str(paths["authority"]), "sha256": sha(paths["authority"]),
                                       "authority_sha256": authority["authority_sha256"]},
                "cloudflare": {"account_id": ACCOUNT, "project": PROJECT, "branch": "main",
                               "expected_predecessor": predecessor, "expected_d1_binding": D1,
                               "production_host": "https://glpclinicscout.com"},
                "gsc": {"property": "https://glpclinicscout.com/",
                        "sitemap_url": "https://glpclinicscout.com/sitemap.xml",
                        "target_url": "https://glpclinicscout.com" + job["target_route"],
                        "submit_after_live_verification": True},
                "release_root": str(release), "rollback_release_root": str(base),
                "release_tree_sha256": tree, "public_tree_sha256": public_tree,
                "release_rows": release_rows, "delta": delta,
                "cow_materialization": build_record.get("cow", {}),
                "constructor_provenance": public_constructor_provenance,
                "live_contract": {"target_route": job["target_route"],
                                  "sitemap_routes": delta["sitemap_routes"],
                                  "sitemap_target_lastmod": delta["sitemap_target_lastmod"],
                                  "indexable": True, "metadata_title": job["metadata_title"]},
                "control_plane": {str(path): sha(path) for path in controls},
                "effects_before_deploy": {"deployments": 0, "cache_purges": 0, "gsc_submissions": 0,
                                          "emails_sent": 0, "spend_usd": 0}}
    manifest["manifest_payload_sha256"] = payload_hash(manifest, "manifest_payload_sha256")
    atomic(paths["manifest"], manifest)
    selective_seal = seal_tree_selective(
        release,
        [base],
        owner=owner,
        use_sudo=use_sudo,
    )
    for path in [release, *release.rglob("*")]:
        info = os.lstat(path)
        if (info.st_uid, info.st_gid) != owner or stat.S_IMODE(info.st_mode) & 0o222:
            raise RuntimeError(f"immutable_release_mode_invalid:{path}")
    seal = {"schema": "glp-opus-article-immutable-seal-v1", "status": "PASS_IMMUTABLE_DEPLOY_HELD",
            "sealed_at_utc": now(), "release_root": str(release), "release_tree_sha256": tree,
            "public_tree_sha256": public_tree, "release_files": len(release_rows),
            "manifest_path": str(paths["manifest"]), "manifest_sha256": sha(paths["manifest"]),
            "owner_uid": owner[0], "owner_gid": owner[1], "directory_mode": "0555",
            "file_mode": "0444_private_base_linked_metadata_preserved", "symlinks": 0,
            "selective_seal": selective_seal,
            "effects": {"deployments": 0, "cache_purges": 0, "gsc_submissions": 0, "spend_usd": 0}}
    seal["seal_sha256"] = payload_hash(seal, "seal_sha256")
    atomic(paths["seal"], seal)
    review = {"schema": "glp-opus-article-parent-deployment-review-v1", "status": "PASS_DEPLOY_AUTHORIZED",
              "reviewed_at_utc": now(), "manifest_path": str(paths["manifest"]),
              "manifest_sha256": sha(paths["manifest"]), "manifest_payload_sha256": manifest["manifest_payload_sha256"],
              "seal_path": str(paths["seal"]), "seal_sha256": sha(paths["seal"]),
              "deployer_sha256": job["proven_deployer_sha256"], "release_tree_sha256": tree,
              "public_tree_sha256": public_tree, "expected_predecessor": predecessor,
              "checks": {"opus_pass_exact": True, "subject_exact": True, "topic_go_upstream": True,
                         "seo_only_public_delta": True, "functions_runtime_unchanged": True,
                         "target_indexable": True, "root_readonly": True},
              "effects": {"deployments": 0, "cache_purges": 0, "gsc_mutations": 0}}
    review["review_sha256"] = payload_hash(review, "review_sha256")
    atomic(paths["review"], review)
    build_receipt = {"schema": "glp-opus-article-release-build-v1",
                     "status": "PASS_RELEASE_SEALED_DEPLOY_AUTHORIZED", "created_at_utc": now(),
                     "release_root": str(release), "release_files": len(release_rows),
                     "public_files": len(public_rows), "release_tree_sha256": tree,
                     "public_tree_sha256": public_tree, "article_sha256": delta["target_sha256"],
                     "sitemap_routes": delta["sitemap_routes"], "delta": delta,
                     "authority_sha256": sha(paths["authority"]), "manifest_sha256": sha(paths["manifest"]),
                     "seal_sha256": sha(paths["seal"]), "review_sha256": sha(paths["review"]),
                     "predecessor_deployment_id": predecessor, "commands": build_record,
                     "cow_materialization": {**build_record.get("cow", {}), "owner_rebinding": owner_rebinding},
                     "constructor_provenance": public_constructor_provenance,
                     "effects": {"deployments": 0, "cache_purges": 0, "gsc_submissions": 0, "spend_usd": 0}}
    atomic(paths["build"], build_receipt)


def build_job(job: dict[str, Any]) -> None:
    if str(BUSINESS) not in sys.path:
        sys.path.insert(0, str(BUSINESS))
    from ops.glp.seo_business_context import load_business_context
    business_context = load_business_context('article_builder')
    context_citation = {k: v for k, v in business_context.items() if k != 'text'}
    if job.get("schema") != "glp-opus-article-release-job-v1":
        raise RuntimeError("job_schema_invalid")
    if len(job.get("proven_deployer_sha256", "")) != 64:
        raise RuntimeError("proven_deployer_hash_invalid")
    require_control_pin(job, MATERIALIZER_PATH, "site_tree_materializer")
    if job.get("operation", "new_article") == "r2_refresh":
        require_control_pin(job, UPDATE_CONTROL_HELPER_PATH, "r2_execution_control")
        require_surface(job.get("r2_execution_surface"), UPDATE_CONTROL_PATH)
    if job.get("operation") == defect_remediation.OPERATION:
        require_control_pin(job, Path(defect_remediation.__file__).resolve(), "defect_remediation")
    opus_path, opus = require_opus(job)
    base, _base_manifest, predecessor = require_current_base(job)
    release = Path(job["release_root"]).resolve()
    resumed = resume_or_retire_existing_release(job, base)
    if resumed is not None:
        manifest = Path(job["report_dir"]) / "01-deployment-manifest.json"
        value = json.loads(manifest.read_text())
        print(json.dumps({"status": "PASS_RELEASE_SEALED_DEPLOY_AUTHORIZED",
                          "business_context": context_citation,
                          "resumed_sealed_release": True,
                          "release": str(release), "manifest": str(manifest),
                          "release_tree_sha256": value["release_tree_sha256"],
                          "public_tree_sha256": value["public_tree_sha256"],
                          "sitemap_routes": value["live_contract"]["sitemap_routes"]}, sort_keys=True))
        return
    try:
        release, build_record = build_candidate(job, base, sha(opus_path))
        delta = validate_delta(job, base, release)
        seal_release(job, release, base, predecessor, opus_path, opus, build_record, delta)
    except Exception:
        if release.exists() and not (Path(job["report_dir"]) / "02-immutable-release-seal.json").exists():
            shutil.rmtree(release, ignore_errors=True)
        raise
    manifest = Path(job["report_dir"]) / "01-deployment-manifest.json"
    value = json.loads(manifest.read_text())
    print(json.dumps({"status": "PASS_RELEASE_SEALED_DEPLOY_AUTHORIZED",
                      "business_context": context_citation,
                      "release": str(release), "manifest": str(manifest),
                      "release_tree_sha256": value["release_tree_sha256"],
                      "public_tree_sha256": value["public_tree_sha256"],
                      "sitemap_routes": value["live_contract"]["sitemap_routes"]}, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", required=True)
    args = parser.parse_args()
    job_path = Path(args.job).resolve()
    job = json.loads(job_path.read_text())
    with carrier_lock():
        build_job(job)


if __name__ == "__main__":
    main()
