#!/usr/bin/env python3
"""Publish one sealed Opus-approved GLP article through the proven carriers."""
from __future__ import annotations
import argparse, datetime as dt, hashlib, importlib.util, json, os, stat, subprocess, sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import requests

BUSINESS = Path("/home/ubuntu/business")
if str(BUSINESS / "scripts") not in sys.path:
    sys.path.insert(0, str(BUSINESS / "scripts"))
from glp_current_content_base_schema import (  # noqa: E402
    COUNT_KEYS,
    POINTER_REQUIRED_KEYS,
    POINTER_SCHEMA,
    POINTER_STATUS,
    validate_pointer_shape,
    validate_supersedes,
)
from glp_price_index_weekly import _validate_base_evidence  # noqa: E402

if str(BUSINESS / "systems/seo-blog-machine/scripts") not in sys.path:
    sys.path.insert(0, str(BUSINESS / "systems/seo-blog-machine/scripts"))
import autonomous_release_gate  # noqa: E402
import accepted_failure_contract  # noqa: E402
from carrier_lock import carrier_lock  # noqa: E402
ENGINE = BUSINESS / "systems/seo-blog-machine/scripts/deploy_opus_article_engine.py"
GSC_ENGINE = BUSINESS / "systems/seo-blog-machine/scripts/gsc_direct_principal_controller.py"
PRODUCTION_VERIFIER = BUSINESS / "systems/seo-blog-machine/scripts/verify_opus_article_production.py"
INDEXNOW_SUBMITTER = BUSINESS / "systems/seo-blog-machine/scripts/submit_target_indexnow_once.py"
BING_SUBMITTER = BUSINESS / "systems/seo-blog-machine/scripts/submit_target_bing_once.py"
ACCOUNT = "f860943dff399ffb1db0a28dcd3dadfd"
PROJECT = "glp-clinic-scout"
CF_API = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/pages/projects/{PROJECT}"
ENV_FILE = Path("/home/ubuntu/.hermes/.env")
SEO_MACHINE_REPORTS = BUSINESS / "reports/glp-clinic-scout/seo-machine"


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def publication_day_pt() -> str:
    return dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()


def require_current_publication_day(job: dict[str, Any]) -> None:
    """Refuse a stale planned day before any network deployment attempt."""
    planned = job.get("publication_date")
    try:
        if not isinstance(planned, str):
            raise ValueError("not_a_string")
        parsed = dt.date.fromisoformat(planned)
        if parsed.isoformat() != planned:
            raise ValueError("noncanonical")
    except (ValueError, TypeError):
        raise RuntimeError("publication_date_invalid") from None
    if planned != publication_day_pt():
        raise RuntimeError("publication_date_pt_day_mismatch:rematerialize_rebuild_review")


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def payload_hash(value: dict[str, Any], field: str) -> str:
    copy = dict(value); copy.pop(field, None)
    return hashlib.sha256(
        json.dumps(copy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        info = os.lstat(path)
        if (
            stat.S_ISREG(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o600
            and path.read_bytes() == encoded
        ):
            return
    except FileNotFoundError:
        pass
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temp.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o600); os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def encoded_json(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def evidence_spec(path: Path, data: bytes) -> dict[str, str]:
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def preflight_evidence(path: Path, data: bytes) -> None:
    """Reject an evidence collision without creating a directory or file."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or path.read_bytes() != data:
        raise RuntimeError(f"base_evidence_collision:{path}")


def write_evidence_once(path: Path, data: bytes) -> None:
    """Create exact evidence once after every pointer rejection gate has passed."""
    preflight_evidence(path, data)
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise RuntimeError("base_evidence_short_write")
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def canonical_predecessor_evidence_plan(
    pointer: dict[str, Any],
) -> tuple[tuple[Path, bytes], ...]:
    """Preserve exact outgoing identity evidence in F13's canonical namespace."""
    deployment_id = pointer.get("deployment_id")
    if not isinstance(deployment_id, str) or not deployment_id:
        raise RuntimeError("predecessor_evidence_deployment_invalid")
    rows: list[tuple[str, bytes]] = []
    for field, filename in (
        ("manifest", "01-deployment-manifest.json"),
        ("final_receipt", "10-final-publication-receipt.json"),
    ):
        source_spec = pointer.get(field)
        if not isinstance(source_spec, dict) or set(source_spec) != {"path", "sha256"}:
            raise RuntimeError(f"predecessor_evidence_{field}_spec_invalid")
        raw_path = source_spec.get("path")
        expected_sha = source_spec.get("sha256")
        try:
            source = Path(str(raw_path)).resolve(strict=True)
            info = os.lstat(source)
        except (OSError, RuntimeError):
            raise RuntimeError(f"predecessor_evidence_{field}_path_invalid") from None
        if (
            str(source) != raw_path
            or source.name != filename
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
        ):
            raise RuntimeError(f"predecessor_evidence_{field}_path_invalid")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected_sha:
            raise RuntimeError(f"predecessor_evidence_{field}_hash_mismatch")
        rows.append((filename, data))
    manifest_sha = pointer["manifest"]["sha256"]
    root = SEO_MACHINE_REPORTS / (
        f"base-remint-{deployment_id.split('-', 1)[0]}-preserved-{manifest_sha[:16]}"
    )
    return tuple((root / filename, data) for filename, data in rows)


def release_rows(root: Path, *, require_public: bool = False) -> list[dict[str, Any]]:
    """Return the complete canonical current-row projection for one sealed tree."""
    if (
        not root.is_dir()
        or root.is_symlink()
        or (require_public and not (root / "public").is_dir())
    ):
        raise RuntimeError("base_release_root_invalid")
    paths = list(root.rglob("*"))
    paths.sort(key=lambda path: path.relative_to(root).as_posix().encode("utf-8"))
    rows = []
    for path in paths:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"base_release_symlink_forbidden:{path}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"base_release_special_file_forbidden:{path}")
        rows.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": sha(path),
            "bytes": info.st_size,
        })
    if not rows:
        raise RuntimeError("base_release_rows_empty")
    return rows


def closure(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item["path"]):
        digest.update(f"{row['path']}\0{row['sha256']}\0{row['bytes']}\n".encode())
    return digest.hexdigest()


def canonical_base_evidence_plan(
    report: Path, manifest: dict[str, Any], final: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str], tuple[tuple[Path, bytes], ...]]:
    """Build canonical evidence and its exact specs entirely in memory."""
    if final.get("status") != "PASS_LIVE_GSC_READBACK_PASS":
        raise RuntimeError("article_final_not_terminal")
    deployment_id = final.get("deployment_id")
    if not isinstance(deployment_id, str) or not deployment_id:
        raise RuntimeError("article_final_deployment_invalid")
    release = Path(str(manifest.get("release_root", ""))).resolve()
    if manifest.get("release_root") != str(release):
        raise RuntimeError("article_release_root_noncanonical")
    rows = release_rows(release, require_public=True)
    public_rows = release_rows(release / "public")
    release_tree = closure(rows)
    public_tree = closure(public_rows)
    if (
        manifest.get("release_tree_sha256") != release_tree
        or manifest.get("public_tree_sha256") != public_tree
        or final.get("release_tree_sha256") != release_tree
        or final.get("public_tree_sha256") != public_tree
    ):
        raise RuntimeError("article_base_closure_mismatch")
    created = final.get("completed_at_utc") or manifest.get("created_at_utc")
    if not isinstance(created, str) or not created:
        raise RuntimeError("article_base_created_at_invalid")
    evidence_dir = report / "current-base-evidence"
    manifest_path = evidence_dir / "01-deployment-manifest.json"
    final_path = evidence_dir / "10-final-publication-receipt.json"
    base_manifest = {
        "schema": "glp-opus-release-deployment-manifest-v1",
        "created_at_utc": created,
        "release_root": str(release),
        "release_rows": rows,
        "release_tree_sha256": release_tree,
        "note": "consumer-compatible adapter minted from the sealed Opus article release",
    }
    manifest_data = encoded_json(base_manifest)
    manifest_spec = evidence_spec(manifest_path, manifest_data)
    base_final = {
        "schema": "glp-opus-production-terminal-receipt-v1",
        "status": "PASS_PRODUCTION_TERMINAL",
        "created_at_utc": created,
        "deployment": {"deployment_id": deployment_id},
        "evidence_sha256": {manifest_path.name: manifest_spec["sha256"]},
        "immutable_closure": {
            "release_tree_sha256": release_tree,
            "public_tree_sha256": public_tree,
        },
        "note": f"terminal moving-base adapter for Opus article deployment {deployment_id}",
    }
    final_data = encoded_json(base_final)
    final_spec = evidence_spec(final_path, final_data)
    return (
        manifest_spec,
        final_spec,
        ((manifest_path, manifest_data), (final_path, final_data)),
    )


def predecessor_binding(manifest: dict[str, Any]) -> dict[str, str]:
    cloudflare = manifest.get("cloudflare") or {}
    deployment_id = cloudflare.get("expected_predecessor")
    release_root = manifest.get("rollback_release_root")
    if not isinstance(deployment_id, str) or not deployment_id:
        raise RuntimeError("base_predecessor_deployment_invalid")
    if not isinstance(release_root, str) or not release_root:
        raise RuntimeError("base_predecessor_release_invalid")
    release = Path(release_root)
    try:
        resolved = release.resolve(strict=True)
        info = os.lstat(release)
    except (OSError, RuntimeError):
        raise RuntimeError("base_predecessor_release_invalid") from None
    if str(resolved) != release_root or release.is_symlink() or not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("base_predecessor_release_invalid")
    return validate_supersedes(
        {"deployment_id": deployment_id, "release_root": release_root},
        require_exists=True,
        label="base_predecessor",
    )


def expected_base_plan(
    report: Path, manifest: dict[str, Any], final: dict[str, Any]
) -> tuple[dict[str, Any], tuple[tuple[Path, bytes], ...]]:
    manifest_spec, final_spec, evidence_plan = canonical_base_evidence_plan(
        report, manifest, final
    )
    expected = {
        "schema": POINTER_SCHEMA,
        "status": POINTER_STATUS,
        "deployment_id": final["deployment_id"],
        "release_root": manifest["release_root"],
        "release_tree_sha256": manifest["release_tree_sha256"],
        "public_tree_sha256": manifest["public_tree_sha256"],
        "manifest": manifest_spec,
        "final_receipt": final_spec,
        "supersedes": predecessor_binding(manifest),
    }
    validate_pointer_shape(expected, require_release_dirs=True, label="expected_base")
    return expected, evidence_plan


def expected_base_pointer(
    report: Path, manifest: dict[str, Any], final: dict[str, Any]
) -> dict[str, Any]:
    expected, _evidence_plan = expected_base_plan(report, manifest, final)
    return expected


def base_advance_plan(
    job: dict[str, Any],
    report: Path,
    manifest: dict[str, Any],
    final: dict[str, Any],
    opening: bytes,
) -> tuple[dict[str, Any], tuple[tuple[Path, bytes], ...], bool]:
    """Validate current authority and produce a zero-write transaction plan."""
    try:
        current = json.loads(opening)
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError("base_state_json_invalid") from None
    if not isinstance(current, dict):
        raise RuntimeError("base_state_not_object")
    current_sha = hashlib.sha256(opening).hexdigest()
    same_deployment = current.get("deployment_id") == final["deployment_id"]
    predecessor = predecessor_binding(manifest)
    predecessor_evidence_plan: tuple[tuple[Path, bytes], ...] = ()
    if same_deployment:
        validate_pointer_shape(
            current,
            require_release_dirs=True,
            allow_missing=POINTER_REQUIRED_KEYS - {"schema", "status", "deployment_id", "release_root"},
            label="same_deployment_base",
        )
    else:
        # A moving predecessor is authority only after its complete shared
        # schema and independently bound local evidence both validate.  The
        # job hash and article lineage are additional gates, not substitutes.
        validate_pointer_shape(
            current, require_release_dirs=True, label="changed_deployment_predecessor"
        )
        evidence = _validate_base_evidence(current, Path(current["release_root"]))
        if (
            evidence["release_tree_sha256"] != current["release_tree_sha256"]
            or evidence["public_tree_sha256"] != current["public_tree_sha256"]
        ):
            raise RuntimeError("changed_deployment_predecessor_evidence_mismatch")
        if (
            current_sha != job["base_state_sha256"]
            or current["deployment_id"] != predecessor["deployment_id"]
            or current["release_root"] != predecessor["release_root"]
        ):
            raise RuntimeError("base_state_cas_failed")
        predecessor_evidence_plan = canonical_predecessor_evidence_plan(current)

    expected, evidence_plan = expected_base_plan(report, manifest, final)
    evidence_plan = predecessor_evidence_plan + evidence_plan
    if same_deployment:
        # reminted_at_utc is the only versioned optional field.  It is not
        # publisher-owned, so preserve a valid populated value rather than
        # silently erasing another canonical writer's evidence.
        if "reminted_at_utc" in current:
            expected["reminted_at_utc"] = current["reminted_at_utc"]
        if COUNT_KEYS & set(current):
            release = Path(expected["release_root"])
            release_projection = release_rows(release, require_public=True)
            public_projection = release_rows(release / "public")
            exact_counts = {
                "release_files": len(release_projection),
                "release_bytes": sum(int(row["bytes"]) for row in release_projection),
                "public_files": len(public_projection),
                "public_bytes": sum(int(row["bytes"]) for row in public_projection),
            }
            for field in COUNT_KEYS & set(current):
                expected[field] = exact_counts[field]
        for field, value in current.items():
            if field in expected and value != expected[field]:
                raise RuntimeError(f"same_deployment_base_drift:{field}")
    for path, data in evidence_plan:
        preflight_evidence(path, data)
    evidence_complete = all(
        path.is_file() and not path.is_symlink() and path.read_bytes() == data
        for path, data in evidence_plan
    )
    return expected, evidence_plan, current == expected and evidence_complete


def advance_base_state(job: dict[str, Any], report: Path, manifest: dict[str, Any], final: dict[str, Any]) -> None:
    accepted_failure_contract.inherit_runtime_binding(job, report, manifest, final)
    state_path = Path(job["base_state_path"]).resolve()
    with carrier_lock():
        locked_opening = state_path.read_bytes()
        expected, evidence_plan, already_complete = base_advance_plan(
            job, report, manifest, final, locked_opening
        )
        if already_complete:
            return
        if state_path.read_bytes() != locked_opening:
            raise RuntimeError("base_state_cas_failed")
        for path, data in evidence_plan:
            write_evidence_once(path, data)
        if state_path.read_bytes() != locked_opening:
            raise RuntimeError("base_state_cas_failed")
        atomic(state_path, expected)


def advance_published_ledger(job: dict[str, Any], report: Path, final: dict[str, Any]) -> None:
    ledger_path = Path(job["published_ledger_path"]).resolve()
    with carrier_lock():
        ledger = json.loads(ledger_path.read_text())
        entries = ledger.get("entries") or []
        existing = next((row for row in entries if row.get("topic_id") == job["topic_id"]), None)
        if existing:
            if existing.get("deployment_id") == final["deployment_id"] and existing.get("target_route") == job["target_route"]:
                return
            raise RuntimeError("published_topic_replay_drift")
        if sha(ledger_path) != job["published_ledger_sha256"]:
            raise RuntimeError("published_ledger_cas_failed")
        entries.append({"topic_id": job["topic_id"], "target_query": final.get("target_query"),
                        "target_route": job["target_route"], "status": "PASS_LIVE",
                        "deployment_id": final["deployment_id"],
                        "terminal_receipt": str(report / "10-final-publication-receipt.json")})
        ledger["entries"] = entries
        atomic(ledger_path, ledger)


def advance_update_event_ledger(job: dict[str, Any], report: Path, final: dict[str, Any]) -> None:
    """Append one immutable R2 deployment event without touching net-new truth."""
    ledger_path = Path(job["update_event_ledger_path"]).resolve()
    with carrier_lock():
        ledger = json.loads(ledger_path.read_text())
        if ledger.get("schema") != "glp-seo-article-update-event-ledger-v1":
            raise RuntimeError("update_event_ledger_schema_invalid")
        entries = ledger.get("entries")
        if not isinstance(entries, list):
            raise RuntimeError("update_event_ledger_entries_invalid")
        event_id = job["manifest_id"]
        existing = next((row for row in entries if row.get("event_id") == event_id), None)
        if existing:
            if (
                existing.get("deployment_id") == final["deployment_id"]
                and existing.get("target_route") == job["target_route"]
                and existing.get("article_id") == job["article_id"]
            ):
                return
            raise RuntimeError("update_event_replay_drift")
        if sha(ledger_path) != job["update_event_ledger_sha256"]:
            raise RuntimeError("update_event_ledger_cas_failed")
        entries.append({
            "event_id": event_id,
            # The ledger records what actually happened to the live article, so a
            # deletion-only defect fix must never be filed as a content refresh.
            "operation": job.get("operation", "r2_refresh"),
            "article_id": job["article_id"],
            "target_route": job["target_route"],
            "deployment_id": final["deployment_id"],
            "terminal_receipt": str(report / "10-final-publication-receipt.json"),
        })
        atomic(ledger_path, ledger)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"module_load_failed:{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def load_production_html_normalizer():
    """Use the verifier's pinned, fail-closed Cloudflare email normalizer."""
    verifier = load_module("glp_opus_article_production_normalizer", PRODUCTION_VERIFIER)
    normalizer = getattr(getattr(verifier, "old", None), "normalize_cf_email_html", None)
    if not callable(normalizer):
        raise RuntimeError("production_html_normalizer_unavailable")
    return normalizer


def cloudflare_api_token() -> str:
    descriptor = os.open(ENV_FILE, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise RuntimeError("deployment_env_permissions_invalid")
        text = os.read(descriptor, info.st_size + 1).decode("utf-8")
    finally:
        os.close(descriptor)
    values: dict[str, str] = {}
    for raw in text.splitlines():
        if "=" not in raw or raw.lstrip().startswith("#"):
            continue
        key, value = raw.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    token = values.get("CLOUDFLARE_API_TOKEN") or values.get("CF_API_TOKEN")
    if not token:
        raise RuntimeError("cloudflare_token_missing")
    return token


def require_current_production_deployment(deployment_id: str) -> dict[str, Any]:
    """Prove a post-deploy continuation still owns the canonical live head."""
    response = requests.get(
        CF_API + "/deployments",
        params={"env": "production", "per_page": 25},
        headers={
            "Authorization": "Bearer " + cloudflare_api_token(),
            "User-Agent": "Kael-GLP-Article-Publisher/1.0",
        },
        timeout=60,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("success") is not True:
        raise RuntimeError("cloudflare_project_read_failed")
    deployments = sorted(
        [
            row for row in (body.get("result") or [])
            if row.get("environment") == "production" and not row.get("is_skipped")
        ],
        key=lambda row: row.get("created_on") or "",
        reverse=True,
    )
    if not deployments:
        raise RuntimeError("cloudflare_production_head_missing")
    head = deployments[0]
    if (
        head.get("id") != deployment_id
        or (head.get("latest_stage") or {}).get("status") != "success"
    ):
        raise RuntimeError("post_deploy_production_head_changed")
    return {
        "deployment_id": deployment_id,
        "created_on": head.get("created_on"),
        "status": "PASS_CURRENT_PRODUCTION_HEAD",
    }


def reconcile_partial_deployment(
    report: Path, manifest: dict[str, Any]
) -> dict[str, Any] | None:
    """Prove and resume one landed deployment without issuing another deploy.

    The original 06/05/08 journals remain immutable. A distinct 06b receipt is
    created only after the provider still reports the exact deployment as the
    current production head and the full sealed-release verifier has produced
    07 against immutable, apex, and www.
    """
    reconciliation_path = report / "06b-deployment-reconciliation.json"
    original_path = report / "06-deployment-receipt.json"
    lease_path = report / "05-deployment-lease.json"
    live_path = report / "07-live-byte-verification.json"
    purge_path = report / "08-cache-purge-receipt.json"
    manifest_path = report / "01-deployment-manifest.json"
    if reconciliation_path.is_symlink():
        raise RuntimeError("partial_deployment_reconciliation_symlink_forbidden")
    if reconciliation_path.exists():
        return None
    if not original_path.is_file():
        return None
    try:
        original = json.loads(original_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("partial_deployment_receipt_unreadable") from exc
    if not isinstance(original, dict):
        raise RuntimeError("partial_deployment_receipt_malformed")
    if original.get("status") != "FAIL_RECONCILIATION_REQUIRED":
        return None
    if live_path.exists():
        raise RuntimeError("partial_deployment_live_receipt_collision")
    if not all(path.is_file() for path in (lease_path, purge_path, manifest_path)):
        raise RuntimeError("partial_deployment_evidence_incomplete")
    try:
        lease = json.loads(lease_path.read_text())
        purge = json.loads(purge_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("partial_deployment_evidence_unreadable") from exc
    if not isinstance(lease, dict) or not isinstance(purge, dict):
        raise RuntimeError("partial_deployment_evidence_malformed")

    deployment_id = original.get("deployment_id")
    deployment_url = original.get("deployment_url")
    manifest_sha = sha(manifest_path)
    release_tree = manifest.get("release_tree_sha256")
    public_tree = manifest.get("public_tree_sha256")
    release_root = manifest.get("release_root")
    historical_deployer_sha = original.get("deployer_sha256")
    effects = original.get("effects") or {}
    runtime = original.get("runtime") or {}
    controls = manifest.get("control_plane") or {}
    if (
        not isinstance(deployment_id, str) or not deployment_id
        or not isinstance(deployment_url, str) or not deployment_url.startswith("https://")
        or original.get("manifest_path") != str(manifest_path)
        or original.get("manifest_sha256") != manifest_sha
        or original.get("manifest_payload_sha256") != manifest.get("manifest_payload_sha256")
        or original.get("release_root") != release_root
        or original.get("release_tree_sha256") != release_tree
        or original.get("public_tree_sha256") != public_tree
        or effects.get("deployments") != 1
        or effects.get("cache_purges") != 1
        or effects.get("gsc_submissions") != 0
        or effects.get("secret_writes") != 0
        or runtime.get("url") != deployment_url
        or runtime.get("uses_functions") is not True
        or controls.get(str(ENGINE)) != historical_deployer_sha
    ):
        raise RuntimeError("partial_deployment_receipt_binding_invalid")
    if (
        lease.get("status") != "FAIL_RECONCILIATION_REQUIRED"
        or lease.get("provider_write_started") is not True
        or lease.get("deployment_id") != deployment_id
        or lease.get("attempt_nonce") != original.get("attempt_nonce")
        or lease.get("commit_message") != original.get("commit_message")
        or lease.get("expected_predecessor") != (manifest.get("cloudflare") or {}).get("expected_predecessor")
        or lease.get("release_tree_sha256") != release_tree
    ):
        raise RuntimeError("partial_deployment_lease_binding_invalid")
    purge_effects = purge.get("effects") or {}
    if (
        purge.get("status") != "PASS_PURGED_TOKEN_REVOKED"
        or purge.get("deployment_id") != deployment_id
        or purge.get("manifest_sha256") != manifest_sha
        or purge_effects.get("cache_purges") != 1
        or purge_effects.get("deployments") != 0
        or purge.get("secret_values_printed") is not False
    ):
        raise RuntimeError("partial_deployment_purge_binding_invalid")

    before = require_current_production_deployment(deployment_id)
    release = Path(str(release_root))
    if (
        not release.is_absolute()
        or release.is_symlink()
        or not release.is_dir()
        or str(release.resolve(strict=True)) != release_root
    ):
        raise RuntimeError("partial_deployment_release_root_invalid")
    result = subprocess.run(
        [
            sys.executable,
            str(PRODUCTION_VERIFIER),
            "--deployment-id", deployment_id,
            "--manifest", str(manifest_path),
            "--release", str(release),
            "--output", str(live_path),
        ],
        cwd=BUSINESS,
        text=True,
        capture_output=True,
        timeout=3600,
    )
    if result.returncode != 0 or not live_path.is_file():
        raise RuntimeError(
            "partial_deployment_full_live_verification_failed:"
            + result.stderr[-1000:].replace("\n", " ")
        )
    try:
        live = json.loads(live_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("partial_deployment_live_receipt_unreadable") from exc
    if (
        not isinstance(live, dict)
        or live.get("status") != "PASS_LIVE"
        or live.get("deployment_id") != deployment_id
        or live.get("release_tree_sha256") != release_tree
        or live.get("public_tree_sha256") != public_tree
    ):
        raise RuntimeError("partial_deployment_live_receipt_invalid")
    after = require_current_production_deployment(deployment_id)
    reconciliation = {
        "schema": "glp-opus-article-deployment-reconciliation-v1",
        "status": "PASS_LIVE",
        "completed_at_utc": now(),
        "deployment_id": deployment_id,
        "deployment_url": deployment_url,
        "manifest_sha256": manifest_sha,
        "manifest_payload_sha256": manifest.get("manifest_payload_sha256"),
        "release_root": release_root,
        "release_tree_sha256": release_tree,
        "public_tree_sha256": public_tree,
        "original_deployment_receipt": {"path": str(original_path), "sha256": sha(original_path)},
        "deployment_lease": {"path": str(lease_path), "sha256": sha(lease_path)},
        "live_verification": {"path": str(live_path), "sha256": sha(live_path)},
        "cache_purge": {"path": str(purge_path), "sha256": sha(purge_path)},
        "production_head_before_verification": before,
        "production_head_after_verification": after,
        "reconciliation_control_plane": {
            str(Path(__file__).resolve()): sha(Path(__file__).resolve()),
            str(PRODUCTION_VERIFIER): sha(PRODUCTION_VERIFIER),
        },
        "historical_effects": effects,
        "reconciliation_effects": {"deployments": 0, "cache_purges": 0, "gsc_submissions": 0},
    }
    atomic(reconciliation_path, reconciliation)
    return reconciliation


def assert_controls(manifest: dict[str, Any]) -> None:
    controls = manifest.get("control_plane") or {}
    for raw, expected in controls.items():
        path = Path(raw)
        if not path.is_file() or sha(path) != expected:
            raise RuntimeError(f"control_plane_drift:{raw}")
    for path in (Path(__file__).resolve(), ENGINE, GSC_ENGINE, INDEXNOW_SUBMITTER, BING_SUBMITTER):
        if controls.get(str(path)) != sha(path):
            raise RuntimeError(f"publication_control_unpinned:{path}")


def deployment_receipt_path(report: Path) -> Path:
    reconciliation = report / "06b-deployment-reconciliation.json"
    return reconciliation if reconciliation.is_file() else report / "06-deployment-receipt.json"


def validated_post_deploy_receipts(report: Path, manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Return the exact completed deployment chain, or fail closed on partial/malformed bytes."""
    deployment_path = deployment_receipt_path(report)
    live_path = report / "07-live-byte-verification.json"
    purge_path = report / "08-cache-purge-receipt.json"
    if not any(path.exists() for path in (deployment_path, live_path, purge_path)):
        return None
    if not all(path.is_file() for path in (deployment_path, live_path, purge_path)):
        raise RuntimeError("post_deploy_receipts_incomplete")
    try:
        deployment = json.loads(deployment_path.read_text())
        live = json.loads(live_path.read_text())
        purge = json.loads(purge_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("post_deploy_receipts_unreadable") from exc
    if not all(isinstance(value, dict) for value in (deployment, live, purge)):
        raise RuntimeError("post_deploy_receipts_malformed")
    deployment_id = deployment.get("deployment_id")
    release_tree = manifest.get("release_tree_sha256")
    manifest_sha = sha(report / "01-deployment-manifest.json")
    if (deployment.get("status") != "PASS_LIVE"
            or not isinstance(deployment_id, str) or not deployment_id
            or deployment.get("release_tree_sha256") != release_tree):
        raise RuntimeError("post_deploy_deployment_receipt_invalid")
    if deployment.get("manifest_sha256") not in (None, manifest_sha):
        raise RuntimeError("post_deploy_deployment_manifest_binding_invalid")
    if (live.get("status") != "PASS_LIVE"
            or live.get("deployment_id") != deployment_id
            or live.get("release_tree_sha256") != release_tree):
        raise RuntimeError("post_deploy_live_receipt_invalid")
    if (purge.get("status") != "PASS_PURGED_TOKEN_REVOKED"
            or purge.get("deployment_id") != deployment_id
            or purge.get("manifest_sha256") != manifest_sha):
        raise RuntimeError("post_deploy_purge_receipt_invalid")
    return {
        "deployment_id": deployment_id,
        "deployment_path": str(deployment_path),
        "deployment_sha256": sha(deployment_path),
        "live_sha256": sha(live_path),
        "purge_sha256": sha(purge_path),
        "manifest_sha256": manifest_sha,
    }


def run_deploy(job: dict[str, Any], report: Path, manifest: dict[str, Any]) -> None:
    if validated_post_deploy_receipts(report, manifest) is not None:
        return
    require_current_publication_day(job)
    engine = load_module("glp_opus_article_deploy_engine", ENGINE)
    engine.REPORT = report
    engine.RELEASE = Path(manifest["release_root"])
    engine.MANIFEST = report / "01-deployment-manifest.json"
    engine.SEAL = report / "02-immutable-release-seal.json"
    engine.REVIEW = report / "03-parent-deployment-review.json"
    engine.LEASE = report / "05-deployment-lease.json"
    engine.RECEIPT = report / "06-deployment-receipt.json"
    engine.LIVE = report / "07-live-byte-verification.json"
    engine.PURGE = report / "08-cache-purge-receipt.json"
    engine.LOCK = Path("/tmp") / f"glp-opus-{job['manifest_id']}.lock"
    engine.PREDECESSOR = manifest["cloudflare"]["expected_predecessor"]
    engine.EXPECTED_TREE = manifest["release_tree_sha256"]
    engine.EXPECTED_PUBLIC = manifest["public_tree_sha256"]
    engine.main()


def run_gsc(job: dict[str, Any], report: Path, manifest: dict[str, Any]) -> None:
    output = report / "09-gsc-sitemap-submit-readback.json"
    deployment_path = deployment_receipt_path(report)
    controller = load_module("glp_opus_article_gsc_direct_controller", GSC_ENGINE)
    arguments = {
        "report": report,
        "manifest_path": report / "01-deployment-manifest.json",
        "deployment_path": deployment_path,
        "manifest_id": job["manifest_id"],
        "target_route": job["target_route"],
        "lastmod": job["publication_date"],
        "output": output,
    }
    controller.run_gsc_control(**arguments, mode="preflight")
    controller.run_gsc_control(**arguments, mode="apply")


def run_indexnow(job: dict[str, Any], report: Path, final: dict[str, Any]) -> dict[str, Any]:
    """Notify IndexNow for both new publishes and same-route R2 updates."""
    submitter = load_module("glp_indexnow_persistent_submitter", INDEXNOW_SUBMITTER)
    result = submitter.submit(
        release=Path(job["release_root"]).resolve(),
        route=job["target_route"],
        deployment_receipt=deployment_receipt_path(report),
        expected_deployment_id=final["deployment_id"],
        final_receipt=report / "10-final-publication-receipt.json",
        intent_path=report / "11-indexnow-intent.json",
        output_path=report / "12-indexnow-result.json",
        retries=3,
        backoff_seconds=1.0,
    )
    if not str(result.get("status") or "").startswith("PASS_ACCEPTED"):
        raise RuntimeError(f"indexnow_submission_failed_after_live_publish:{result.get('status')}")
    return result


def _bing_content_push(job: dict[str, Any], report: Path) -> dict[str, Any]:
    """Push the new article's HTML to Bing on top of the URL submission.

    ``SubmitUrlBatch`` asks Bing to come and crawl; ``SubmitContent`` hands Bing
    the bytes so indexing does not wait on a crawl at all. Bing is ChatGPT's
    retrieval surface and this site already holds positions 5-9 there, so hours
    saved here are hours of answer-engine visibility gained.

    The bytes come from the RELEASE on disk, never from the edge — submitting
    what a CDN happens to be serving would let a stale cache define what Bing
    indexes. NON-FATAL like every other post-publish leg: the article is already
    live and byte-verified by the time this runs.
    """
    output_path = report / "14-bing-content-push.json"
    record: dict[str, Any] = {"schema": "glp-bing-content-push-v1", "completed_at_utc": now()}
    try:
        import sys as _sys  # noqa: PLC0415
        _sys.path.insert(0, "/home/ubuntu/business/ops")
        _sys.path.insert(0, "/home/ubuntu/business/scripts")
        from glp.seo_data_rails import BingWebmaster  # noqa: PLC0415
        import glp_bing_fast_index as fast_index  # noqa: PLC0415

        route = str(job.get("target_route") or "")
        url = "https://glpclinicscout.com" + route
        pointer = json.loads((BUSINESS / "reports/glp-clinic-scout/seo-machine"
                              / "current-content-base.json").read_text())
        release = Path(str(pointer.get("release_root") or ""))
        html = fast_index.local_html_for(url, release if release.is_dir() else None)
        if not html:
            record.update({"status": "SKIPPED", "reason": "no_local_html", "url": url})
        else:
            result = BingWebmaster().submit_content(url, html)
            record.update({"status": "PASS" if result.get("ok") else "ERROR_NONFATAL",
                           "url": url, "bytes": result.get("bytes"),
                           "error": result.get("error")})
    except Exception as exc:  # noqa: BLE001 - never fail a live publish on Bing
        record.update({"status": "ERROR_NONFATAL", "error_class": type(exc).__name__,
                       "message": str(exc)[:300]})
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        pass
    return record


def run_bing_submission(job: dict[str, Any], report: Path, final: dict[str, Any]) -> dict[str, Any]:
    """Submit the exact live URL to Bing's own URL-submission queue.

    IndexNow is a change *notification*; Bing's SubmitUrlBatch is a direct
    submission against a hard daily quota. Both run on every publish.

    This leg is deliberately NON-FATAL: the article is already live and
    byte-verified by the time it runs, so a Bing outage, throttle, or exhausted
    quota must never fail an otherwise-successful publication. Every outcome is
    still written to 13-bing-submission-result.json for the audit.
    """
    output_path = report / "13-bing-submission-result.json"
    try:
        submitter = load_module("glp_bing_persistent_submitter", BING_SUBMITTER)
        result = submitter.submit(
            route=job["target_route"],
            deployment_receipt=deployment_receipt_path(report),
            expected_deployment_id=final["deployment_id"],
            final_receipt=report / "10-final-publication-receipt.json",
            output_path=output_path,
            retries=3,
            backoff_seconds=1.0,
        )
        content = _bing_content_push(job, report)
        return {"status": result.get("status"), "fatal": False, "output": str(output_path),
                "content_push": content}
    except Exception as exc:  # noqa: BLE001 - never fail a live publish on Bing
        record = {
            "schema": "glp-target-bing-result-v1",
            "status": "ERROR_NONFATAL",
            "error_class": type(exc).__name__,
            "message": str(exc)[:300],
            "completed_at_utc": now(),
            "target_url": "https://glpclinicscout.com" + str(job.get("target_route") or ""),
            "api_key_value_recorded": False,
        }
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        except OSError:
            pass
        return {"status": "ERROR_NONFATAL", "fatal": False, "output": str(output_path)}


def finalize(
    job: dict[str, Any],
    report: Path,
    manifest: dict[str, Any],
    production_head: dict[str, Any] | None = None,
) -> dict[str, Any]:
    accepted_failures = accepted_failure_contract.verify_classification(report, job)
    final = report / "10-final-publication-receipt.json"
    if final.exists():
        existing = json.loads(final.read_text())
        if existing.get("status") == "PASS_LIVE_GSC_READBACK_PASS":
            return existing
        raise RuntimeError("existing_final_receipt_invalid")
    deployment_path = deployment_receipt_path(report)
    deployment = json.loads(deployment_path.read_text())
    live = json.loads((report / "07-live-byte-verification.json").read_text())
    purge = json.loads((report / "08-cache-purge-receipt.json").read_text())
    gsc = json.loads((report / "09-gsc-sitemap-submit-readback.json").read_text())
    deployment_id = deployment.get("deployment_id")
    if deployment.get("status") != "PASS_LIVE" or live.get("status") != "PASS_LIVE":
        raise RuntimeError("deployment_or_live_not_pass")
    if purge.get("status") != "PASS_PURGED_TOKEN_REVOKED" or gsc.get("status") != "PASS":
        raise RuntimeError("purge_or_gsc_not_pass")
    if production_head is None:
        production_head = require_current_production_deployment(str(deployment_id or ""))
    if production_head.get("deployment_id") != deployment_id:
        raise RuntimeError("final_production_head_binding_invalid")
    expected_sha = manifest["delta"]["target_sha256"]
    expected_bytes = manifest["delta"]["target_bytes"]
    immutable = str(deployment.get("deployment_url") or "").rstrip("/")
    route = job["target_route"]
    expected_content = (
        Path(manifest["release_root"]).resolve() / "public" / route.strip("/") / "index.html"
    ).read_bytes()
    if len(expected_content) != expected_bytes or hashlib.sha256(expected_content).hexdigest() != expected_sha:
        raise RuntimeError("manifest_target_bytes_invalid")
    normalize_html = load_production_html_normalizer()
    normalized_expected = normalize_html(expected_content)
    normalized_expected_sha = hashlib.sha256(normalized_expected).hexdigest()
    checks = []
    for host in (immutable, "https://glpclinicscout.com", "https://www.glpclinicscout.com"):
        response = requests.get(host + route, params={"release": manifest["manifest_payload_sha256"][:16]}, timeout=60)
        actual_sha = hashlib.sha256(response.content).hexdigest()
        normalized_actual = normalize_html(response.content)
        normalized_actual_sha = hashlib.sha256(normalized_actual).hexdigest()
        passed = (
            response.status_code == 200
            and len(normalized_actual) == len(normalized_expected)
            and normalized_actual_sha == normalized_expected_sha
        )
        checks.append({"host": host, "status": response.status_code, "bytes": len(response.content),
                       "sha256": actual_sha, "normalized_bytes": len(normalized_actual),
                       "normalized_sha256": normalized_actual_sha,
                       "cloudflare_email_normalized": response.content != normalized_actual,
                       "passed": passed})
    if not all(row["passed"] for row in checks):
        # A second authorized lane can advance the production pointer between
        # the pre-finalization head check and these three byte reads. Preserve
        # the byte refusal, but classify a proven head move so the generation
        # coordinator rematerializes against the new head instead of treating
        # the collision as a content defect.
        try:
            require_current_production_deployment(str(deployment_id or ""))
        except RuntimeError as error:
            if "post_deploy_production_head_changed" in str(error):
                raise RuntimeError(
                    "production_head_changed_during_final_target_byte_readback"
                ) from error
            raise
        raise RuntimeError("final_target_byte_readback_failed")
    value = {"schema": "glp-opus-article-final-publication-v1",
             "accepted_failures": accepted_failures,
             "status": "PASS_LIVE_GSC_READBACK_PASS", "completed_at_utc": now(),
             "operation": job.get("operation", "new_article"),
             "job_id": job["manifest_id"], "topic_id": job.get("topic_id"),
             "target_query": job.get("target_query"), "article_id": job["article_id"],
             "live_url": "https://glpclinicscout.com" + route,
             "deployment_id": deployment_id, "canonical_deployment_id": deployment_id,
             "release_tree_sha256": manifest["release_tree_sha256"],
             "public_tree_sha256": manifest["public_tree_sha256"],
             "target_sha256": expected_sha, "target_bytes": expected_bytes,
             "manifest": {"path": str(report / "01-deployment-manifest.json"),
                          "sha256": sha(report / "01-deployment-manifest.json")},
             "canonical_production_head": production_head,
             "opus_receipt": manifest["operator_authority"], "live_readback": checks,
             "control_plane": {str(Path(__file__).resolve()): sha(Path(__file__).resolve()),
                               str(PRODUCTION_VERIFIER): sha(PRODUCTION_VERIFIER)},
             "receipts": {name: {"path": str(report / filename), "sha256": sha(report / filename)}
                          for name, filename in {"deployment": deployment_path.name,
                                                "live": "07-live-byte-verification.json",
                                                "purge": "08-cache-purge-receipt.json",
                                                "gsc": "09-gsc-sitemap-submit-readback.json"}.items()},
             "effects": {"pages_deployments": 1, "cache_purges": 1,
                         "gsc_sitemap_submissions": 1, "emails": 0,
                         "d1_writes": 0, "spend_usd": 0}}
    value["receipt_sha256"] = payload_hash(value, "receipt_sha256")
    atomic(final, value)
    return value


def reconcile_final_job(job: dict[str, Any], *, verify_only: bool = False) -> dict[str, Any]:
    """Adopt a proven completed publication; never deploy or call submitters."""
    report = Path(job["report_dir"]).resolve()
    final_path = report / "10-final-publication-receipt.json"
    if not final_path.is_file() or final_path.is_symlink():
        raise RuntimeError("completed_publication_final_missing")
    final_bytes = final_path.read_bytes()
    final = json.loads(final_bytes)
    manifest_path = report / "01-deployment-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if (job.get("schema") != "glp-opus-article-release-job-v1"
        or job.get("operation", "new_article") != "new_article"
        or final.get("schema") != "glp-opus-article-final-publication-v1"
        or final.get("status") != "PASS_LIVE_GSC_READBACK_PASS"
        or payload_hash(final, "receipt_sha256") != final.get("receipt_sha256")
        or payload_hash(manifest, "manifest_payload_sha256") != manifest.get("manifest_payload_sha256")
        or final.get("manifest") != {"path": str(manifest_path), "sha256": sha(manifest_path)}
        or final.get("job_id") != job.get("manifest_id")
        or final.get("topic_id") != job.get("topic_id")
        or final.get("article_id") != job.get("article_id")
        or final.get("live_url") != "https://glpclinicscout.com" + job["target_route"]
        or manifest.get("release_root") != job.get("release_root")
        or manifest.get("rollback_release_root") != job.get("base_release_root")):
        raise RuntimeError("completed_publication_identity_invalid")
    for name, spec in final["receipts"].items():
        path = Path(spec["path"])
        if path.parent != report or path.is_symlink() or sha(path) != spec["sha256"]:
            raise RuntimeError("completed_publication_receipt_drift:" + name)
    classification = accepted_failure_contract.verify_classification(report, job)
    if final.get("accepted_failures") != classification:
        raise RuntimeError("completed_publication_classification_drift")
    state_path = Path(job["base_state_path"])
    with carrier_lock():
        before = state_path.read_bytes()
        expected, _, _ = base_advance_plan(job, report, manifest, final, before)
        ledger_path = Path(job["published_ledger_path"])
        ledger = json.loads(ledger_path.read_text())
        existing = [row for row in ledger.get("entries", []) if row.get("topic_id") == job["topic_id"]]
        if existing:
            if len(existing) != 1 or any(existing[0].get(k) != v for k, v in {
                "deployment_id": final["deployment_id"], "target_route": job["target_route"], "status": "PASS_LIVE"}.items()):
                raise RuntimeError("published_topic_replay_drift")
        elif sha(ledger_path) != job["published_ledger_sha256"]:
            raise RuntimeError("published_ledger_cas_failed")
        require_current_production_deployment(final["deployment_id"])
        normalizer = load_production_html_normalizer()
        content = (Path(manifest["release_root"]) / "public" / job["target_route"].strip("/") / "index.html").read_bytes()
        reads = []
        for host in ("https://glpclinicscout.com", "https://www.glpclinicscout.com"):
            response = requests.get(host + job["target_route"], timeout=60)
            passed = response.status_code == 200 and normalizer(response.content) == normalizer(content)
            reads.append({"url": host + job["target_route"], "status": response.status_code,
                          "normalized_sha256": hashlib.sha256(normalizer(response.content)).hexdigest(), "passed": passed})
        if not all(row["passed"] for row in reads):
            raise RuntimeError("completed_publication_live_bytes_drift")
        require_current_production_deployment(final["deployment_id"])
        if not verify_only:
            advance_base_state(job, report, manifest, final)
            advance_published_ledger(job, report, final)
        if final_path.read_bytes() != final_bytes:
            raise RuntimeError("completed_publication_final_mutated")
    return {"status": "PASS_RECONCILIATION_PREFLIGHT" if verify_only else "PASS_COMPLETED_PUBLICATION_RECONCILED",
            "deployment_id": final["deployment_id"], "final_receipt": str(final_path),
            "final_receipt_sha256": hashlib.sha256(final_bytes).hexdigest(),
            "pointer_before": json.loads(before), "pointer_expected": expected,
            "live_readback": reads, "external_writes": 0}


def publish_job(job: dict[str, Any]) -> None:
    if job.get("schema") != "glp-opus-article-release-job-v1":
        raise RuntimeError("job_schema_invalid")
    operation = job.get("operation", "new_article")
    if operation not in {"new_article", "r2_refresh", "defect_remediation"}:
        raise RuntimeError("release_operation_invalid")
    report = Path(job["report_dir"]).resolve()
    accepted_failures = accepted_failure_contract.verify_classification(report, job)
    final = report / "10-final-publication-receipt.json"
    if final.exists():
        existing = json.loads(final.read_text())
        if existing.get("status") == "PASS_LIVE_GSC_READBACK_PASS":
            manifest = json.loads((report / "01-deployment-manifest.json").read_text())
            advance_base_state(job, report, manifest, existing)
            if operation == "new_article" and job.get("published_ledger_path"):
                advance_published_ledger(job, report, existing)
            elif operation in {"r2_refresh", "defect_remediation"} and job.get("update_event_ledger_path"):
                advance_update_event_ledger(job, report, existing)
            indexnow = run_indexnow(job, report, existing)
            bing = run_bing_submission(job, report, existing)
            print(json.dumps({"status": "IDEMPOTENT_ALREADY_PASS", "deployment_id": existing["deployment_id"],
                              "live_url": existing["live_url"], "receipt": str(final), "accepted_failures": accepted_failures,
                              "indexnow_status": indexnow["status"],
                              "bing_status": bing["status"]}, sort_keys=True))
            return
        raise RuntimeError("existing_final_receipt_invalid")
    manifest_path = report / "01-deployment-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if payload_hash(manifest, "manifest_payload_sha256") != manifest.get("manifest_payload_sha256"):
        raise RuntimeError("manifest_payload_hash_invalid")
    reconcile_partial_deployment(report, manifest)
    post_deploy = validated_post_deploy_receipts(report, manifest)
    if post_deploy is None:
        require_current_publication_day(job)
        assert_controls(manifest)
    # ZERO-APPROVAL AUTONOMOUS RELEASE (operator authority:
    # reports/glp-clinic-scout/seo-machine/OPERATOR-AUTHORITY-AUTONOMOUS-
    # RELEASE-20260813.md). The operator gate is REPLACED by this machine-gate
    # chain: STOP-SEO kill-switch, 3/day hard cadence ceiling, editorial/
    # honesty contract, no-medical-reviewer, coverage invariant, completeness
    # sensor, and predecessor CAS must ALL pass in code before any deploy.
    gate_path = report / "00-autonomous-release-gate.json"
    if post_deploy is None:
        packet_dir = Path(job["article_json"]["path"]).resolve().parent
        target_relative = "public" + job["target_route"] + "index.html"
        gate_receipt = autonomous_release_gate.evaluate_release(
            packet=packet_dir,
            release_root=Path(job["release_root"]).resolve(),
            predecessor_root=Path(job["base_release_root"]).resolve(),
            base_state_path=Path(job["base_state_path"]).resolve(),
            base_state_sha256=job["base_state_sha256"],
            expected_deployment_id=(manifest.get("cloudflare") or {}).get("expected_predecessor"),
            release_target=Path(job["release_root"]).resolve() / target_relative,
            operation=job.get("operation", "new_article"),
            article_path=Path(job["article_json"]["path"]).resolve(),
        )
        atomic(gate_path, gate_receipt)
    else:
        if not gate_path.is_file():
            raise RuntimeError("post_deploy_autonomous_gate_missing")
        gate_receipt = json.loads(gate_path.read_text())
    autonomous_release_gate.require_pass(gate_receipt)
    if post_deploy is None:
        run_deploy(job, report, manifest)
    post_deploy = validated_post_deploy_receipts(report, manifest)
    if post_deploy is None:
        raise RuntimeError("post_deploy_receipts_missing_after_deploy")
    require_current_production_deployment(post_deploy["deployment_id"])
    run_gsc(job, report, manifest)
    production_head = require_current_production_deployment(post_deploy["deployment_id"])
    result = finalize(job, report, manifest, production_head)
    advance_base_state(job, report, manifest, result)
    if operation == "new_article":
        advance_published_ledger(job, report, result)
    else:
        advance_update_event_ledger(job, report, result)
    indexnow = run_indexnow(job, report, result)
    bing = run_bing_submission(job, report, result)
    print(json.dumps({"status": result["status"], "deployment_id": result["deployment_id"],
                      "live_url": result["live_url"], "receipt": str(final), "accepted_failures": accepted_failures,
                      "indexnow_status": indexnow["status"],
                      "bing_status": bing["status"]}, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("--job", required=True)
    args = parser.parse_args()
    job = json.loads(Path(args.job).resolve().read_text())
    with carrier_lock():
        publish_job(job)


if __name__ == "__main__":
    main()
