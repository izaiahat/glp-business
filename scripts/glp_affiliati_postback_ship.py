#!/usr/bin/env python3
"""Ship the Affiliati CAKE postback edge through the canonical GLP release chain.

The machine is deliberately narrow: one Pages Function and three outbound-click
mapping files. It never opens a tracking URL, writes an affiliate portal, or
stores the postback key outside the existing mode-0600 secret file and the
Cloudflare Pages secret binding.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

BUSINESS = Path("/home/ubuntu/business")
SCRIPTS = BUSINESS / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import glp_price_index_weekly as release_chain  # noqa: E402
from glp_deploy_sensor_hook import attach_sensor_result  # noqa: E402
from site_tree_materializer import (  # noqa: E402
    constructor_provenance,
    copytree_linked,
    detach_for_write,
    make_directories_writable,
    remove_materialized_tree,
)

SOURCE_FUNCTION = (
    BUSINESS
    / "site-system/flagship-v2/candidates/glp-paid-acquisition-funnel-20260827"
    / "functions/api/postback/affiliati.js"
)
SOURCE_TEST = (
    BUSINESS
    / "site-system/flagship-v2/candidates/glp-paid-acquisition-funnel-20260827"
    / "tests/affiliati-postback-contract.test.mjs"
)
SECRET_PATH = Path("/home/ubuntu/.hermes/secrets/affiliati/postback-key.txt")
RELEASE_NAME = "glp-affiliati-postback-20260828"
CANDIDATE = release_chain.CANDIDATES / RELEASE_NAME
RELEASE = release_chain.RELEASES / RELEASE_NAME
RECEIPT_DIR = BUSINESS / "reports/ops/job-receipts"
RECEIPT_PATH = RECEIPT_DIR / "affiliati-postback-ship-20260828.json"
FAILED_PURGE_RECEIPT = RECEIPT_DIR / "affiliati-postback-ship-late-reconciliation-20260828.json"
LATE_RECONCILIATION_RECEIPT = RECEIPT_DIR / "affiliati-postback-ship-late-reconciliation-attempt-02-20260828.json"
PUBLIC_ENDPOINT = "https://glpclinicscout.com/api/postback/affiliati"
PUBLIC_ASSET = "https://glpclinicscout.com/assets/analytics.js"
VERSIONED_RELEASE_NAME = "glp-affiliati-postback-versioned-asset-20260828"
VERSIONED_CANDIDATE = release_chain.CANDIDATES / VERSIONED_RELEASE_NAME
VERSIONED_RELEASE = release_chain.RELEASES / VERSIONED_RELEASE_NAME
POSTBACK_ROOT = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/postback"
IMPLEMENTATION_ATTESTATION = RECEIPT_DIR / "affiliati-postback-pages-receiver-implementation-20260828.json"
LIVE_IMPLEMENTATION_ATTESTATION = RECEIPT_DIR / "affiliati-postback-pages-receiver-live-20260828.json"
RECEIVER_CONTRACT = POSTBACK_ROOT / "receiver-contracts/affiliati.json"
NETWORK_SPEC = POSTBACK_ROOT / "network-specs/affiliati.json"
MUTATION_PATHS = (
    Path("functions/api/postback/affiliati.js"),
    Path("src/affiliate-subid.mjs"),
    Path("assets/analytics.js"),
    Path("public/assets/analytics.js"),
    Path("data/affiliati-postback-contract.json"),
    Path("tests/affiliati-postback-contract.test.mjs"),
)
EXPECTED_CHANGED = {
    "added": ["functions/api/postback/affiliati.js", "tests/affiliati-postback-contract.test.mjs"],
    "modified": ["assets/analytics.js", "data/affiliati-postback-contract.json", "public/assets/analytics.js", "src/affiliate-subid.mjs"],
    "deleted": [],
}


class ShipError(RuntimeError):
    """Fail-closed release error."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def write_receipt(payload: dict[str, Any], path: Path = RECEIPT_PATH) -> None:
    release_chain.write_json_0600(path, payload)


def write_json_once(path: Path, payload: dict[str, Any]) -> str:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != data:
            raise ShipError(f"control_artifact_collision:{path}")
        return sha_bytes(data)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return sha_bytes(data)


def read_json_object(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise ShipError(f"control_artifact_missing:{path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ShipError(f"control_artifact_invalid:{path}") from exc
    if not isinstance(value, dict):
        raise ShipError(f"control_artifact_not_object:{path}")
    return value


def reconciliation_source() -> dict[str, Any] | None:
    """Return the frozen landed-deploy failure eligible for no-redeploy recovery."""
    if not RECEIPT_PATH.is_file() or RECEIPT_PATH.is_symlink():
        return None
    prior = read_json_object(RECEIPT_PATH)
    blockers = prior.get("blockers") or []
    if (
        prior.get("status") == "FAIL"
        and (prior.get("effects") or {}).get("cloudflare_deployments") == 1
        and (prior.get("deployment") or {}).get("id")
        and any("live_asset_readback_invalid:production:200" in str(value) for value in blockers)
    ):
        return prior
    return None


def validate_predeploy_control_artifacts() -> dict[str, Any]:
    contract = read_json_object(RECEIVER_CONTRACT)
    spec = read_json_object(NETWORK_SPEC)
    attestation = read_json_object(IMPLEMENTATION_ATTESTATION)
    if contract.get("schema") != "affiliati-receiver-contract-v1" or contract.get("status") != "READY_FOR_REGISTERED_SHIP":
        raise ShipError("receiver_contract_not_ready")
    if (spec.get("schema") != "postback-network-spec-v1" or spec.get("status") != "READY_FOR_REGISTERED_SHIP"
            or spec.get("click_id_macro") != "#s1#"
            or spec.get("compatibility_click_id_macro") != "#s2#"):
        raise ShipError("network_spec_not_ready")
    if attestation.get("schema") != "glp.affiliati_pages_receiver.predeploy.v1" or attestation.get("status") != "PASS_REVIEWED_FOR_REGISTERED_SHIP":
        raise ShipError("implementation_attestation_not_ready")
    expected = attestation.get("source_files") or {}
    source_paths = {
        "function": SOURCE_FUNCTION,
        "subid": SOURCE_FUNCTION.parents[3] / "src/affiliate-subid.mjs",
        "analytics": SOURCE_FUNCTION.parents[3] / "assets/analytics.js",
        "public_analytics": SOURCE_FUNCTION.parents[3] / "public/assets/analytics.js",
        "test": SOURCE_TEST,
    }
    for label, path in source_paths.items():
        if expected.get(label, {}).get("sha256") != sha_file(path):
            raise ShipError(f"implementation_attestation_source_mismatch:{label}")
    if attestation.get("shipper_sha256") != sha_file(Path(__file__).resolve()):
        raise ShipError("implementation_attestation_shipper_mismatch")
    return {
        "implementation_attestation": {"path": str(IMPLEMENTATION_ATTESTATION), "sha256": sha_file(IMPLEMENTATION_ATTESTATION)},
        "receiver_contract": {"path": str(RECEIVER_CONTRACT), "sha256": sha_file(RECEIVER_CONTRACT)},
        "network_spec": {"path": str(NETWORK_SPEC), "sha256": sha_file(NETWORK_SPEC)},
    }


def ensure_control_artifacts(*, deployment_id: str, deployment_url: str, release: Path) -> dict[str, Any]:
    predeploy = validate_predeploy_control_artifacts()
    function_path = release / MUTATION_PATHS[0]
    live = {
        "schema": "glp.affiliati_pages_receiver.live.v1",
        "status": "PASS",
        "deployment_id": deployment_id,
        "deployment_url": deployment_url,
        "release_root": str(release),
        "endpoint": PUBLIC_ENDPOINT,
        "d1_database_id": release_chain.RUNTIME_D1,
        "function": {"path": str(function_path), "sha256": sha_file(function_path)},
        "predeploy_attestation_sha256": predeploy["implementation_attestation"]["sha256"],
        "authentication": {"mode": "shared_secret", "placement": "query", "parameter": "key", "secret_binding": "AFFILIATI_POSTBACK_KEY", "missing_or_wrong_key_http_status": 403},
        "secrets_printed": False,
    }
    live_sha = write_json_once(LIVE_IMPLEMENTATION_ATTESTATION, live)
    return {**predeploy, "live_implementation_attestation": {"path": str(LIVE_IMPLEMENTATION_ATTESTATION), "sha256": live_sha}}


def read_secret() -> str:
    info = SECRET_PATH.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ShipError("affiliati_postback_secret_mode_invalid")
    value = SECRET_PATH.read_text(encoding="utf-8").strip()
    if len(value) < 48 or any(char.isspace() for char in value):
        raise ShipError("affiliati_postback_secret_entropy_invalid")
    return value


def replace_exact(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ShipError(f"overlay_contract_drift:{label}:{count}")
    return text.replace(old, new, 1)


def transform_subid(text: str) -> str:
    text = replace_exact(
        text,
        '  RevOffers: ["sub1", "sub2", "sub3", "sub4"],\n  Awin:',
        '  RevOffers: ["sub1", "sub2", "sub3", "sub4"],\n'
        '  // Affiliati uses s1 as the primary owned click id and duplicates it into s2 for compatibility.\n'
        '  Affiliati: ["s1", "s2"],\n  Awin:',
        "subid_params",
    )
    text = replace_exact(
        text,
        'const NETWORK_BY_TRACKING_HOST = Object.freeze({\n  "www.awin1.com": "Awin",',
        'const NETWORK_BY_TRACKING_HOST = Object.freeze({\n'
        '  "epsilonmkt.com": "Affiliati",\n'
        '  "maanantaispc.com": "Affiliati",\n'
        '  "www.awin1.com": "Awin",',
        "tracking_hosts",
    )
    text = replace_exact(
        text,
        'const NETWORK_LABEL_PATTERNS = [\n  [/awin/i, "Awin"],',
        'const NETWORK_LABEL_PATTERNS = [\n  [/affiliati/i, "Affiliati"],\n  [/awin/i, "Awin"],',
        "network_label",
    )
    return text


def transform_analytics(text: str) -> str:
    text = replace_exact(
        text,
        '    RevOffers: ["sub1", "sub2", "sub3", "sub4"],\n    Awin:',
        '    RevOffers: ["sub1", "sub2", "sub3", "sub4"],\n'
        '    Affiliati: ["s1", "s2"],\n    Awin:',
        "analytics_params",
    )
    text = replace_exact(
        text,
        '    const values = [anchor.dataset.sub1, anchor.dataset.sub2, clickId, gclid];',
        '    // Affiliati uses s1 as the primary owned click id and duplicates it into s2.\n'
        '    const values = anchor.dataset.network === "Affiliati"\n'
        '      ? [clickId, clickId]\n'
        '      : [anchor.dataset.sub1, anchor.dataset.sub2, clickId, gclid];',
        "analytics_values",
    )
    text = replace_exact(
        text,
        '      if (index < 2 && url.searchParams.get(name)) return;',
        '      if (index < 2 && anchor.dataset.network !== "Affiliati" && url.searchParams.get(name)) return;',
        "analytics_affiliati_override",
    )
    return text


def expected_overlays(predecessor: Path) -> dict[Path, bytes]:
    if not SOURCE_FUNCTION.is_file() or not SOURCE_TEST.is_file():
        raise ShipError("affiliati_source_missing")
    source = (predecessor / "src/affiliate-subid.mjs").read_text(encoding="utf-8")
    analytics = (predecessor / "assets/analytics.js").read_text(encoding="utf-8")
    public_analytics = (predecessor / "public/assets/analytics.js").read_text(encoding="utf-8")
    return {
        MUTATION_PATHS[0]: SOURCE_FUNCTION.read_bytes(),
        MUTATION_PATHS[1]: transform_subid(source).encode("utf-8"),
        MUTATION_PATHS[2]: transform_analytics(analytics).encode("utf-8"),
        MUTATION_PATHS[3]: transform_analytics(public_analytics).encode("utf-8"),
        MUTATION_PATHS[4]: RECEIVER_CONTRACT.read_bytes(),
        MUTATION_PATHS[5]: SOURCE_TEST.read_bytes(),
    }


def installed(root: Path) -> bool:
    try:
        source = SOURCE_FUNCTION.read_bytes()
        return (
            (root / MUTATION_PATHS[0]).read_bytes() == source
            and 'Affiliati: ["s1", "s2"]' in (root / MUTATION_PATHS[1]).read_text(encoding="utf-8")
            and '"epsilonmkt.com": "Affiliati"' in (root / MUTATION_PATHS[1]).read_text(encoding="utf-8")
            and 'anchor.dataset.network === "Affiliati"' in (root / MUTATION_PATHS[2]).read_text(encoding="utf-8")
            and '? [clickId, clickId]' in (root / MUTATION_PATHS[2]).read_text(encoding="utf-8")
            and (root / MUTATION_PATHS[2]).read_bytes() == (root / MUTATION_PATHS[3]).read_bytes()
            and (root / MUTATION_PATHS[4]).read_bytes() == RECEIVER_CONTRACT.read_bytes()
            and (root / MUTATION_PATHS[5]).read_bytes() == SOURCE_TEST.read_bytes()
        )
    except OSError:
        return False


def endpoint_auth_gate(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=45, headers={"Cache-Control": "no-cache"})
    try:
        body = response.json()
    except ValueError as exc:
        raise ShipError("affiliati_endpoint_non_json") from exc
    if response.status_code != 403 or body != {"status": "FORBIDDEN"}:
        raise ShipError(f"affiliati_endpoint_auth_gate_invalid:{response.status_code}")
    return {"url": url, "http_status": 403, "body_sha256": sha_bytes(response.content)}


def asset_readback(*, deployment_url: str, deployment_id: str, release: Path) -> dict[str, Any]:
    expected_sha = sha_file(release / "public/assets/analytics.js")
    urls = {
        "immutable": deployment_url.rstrip("/") + "/assets/analytics.js",
        "production_cache_busted": PUBLIC_ASSET + "?reconcile=" + deployment_id[:8],
        "production_unbusted": PUBLIC_ASSET,
    }
    reads: dict[str, Any] = {}
    for label, url in urls.items():
        response = requests.get(url, timeout=45, headers={"Cache-Control": "no-cache"})
        body_sha = sha_bytes(response.content)
        reads[label] = {
            "url": url,
            "http_status": response.status_code,
            "sha256": body_sha,
            "matches_expected": response.status_code == 200 and body_sha == expected_sha,
            "cf_cache_status": response.headers.get("CF-Cache-Status"),
            "age": response.headers.get("Age"),
            "cache_control": response.headers.get("Cache-Control"),
            "cf_ray_present": bool(response.headers.get("CF-Ray")),
        }
    return {"expected_sha256": expected_sha, "reads": reads}


ANALYTICS_REFERENCE = re.compile(r"/assets/analytics\.js(?:\?v=[A-Za-z0-9._-]+)?")


def transform_versioned_builder(text: str) -> str:
    stylesheet_marker = (
        'const STYLESHEET_ASSETS = ["site.css", "site-home.css", "site-money.css", '
        '"site-review.css", "site-editorial.css", "site-tools.css", "site-trust.css", "site-p3.css"];\n'
    )
    if text.count(stylesheet_marker) != 1:
        raise ShipError("versioned_builder_stylesheet_marker_drift")
    text = text.replace(
        stylesheet_marker,
        stylesheet_marker
        + 'const ANALYTICS_ASSET_SOURCE = await readFile(path.join(ROOT, "assets", "analytics.js"));\n'
        + 'const ANALYTICS_ASSET_FILENAME = `analytics.${createHash("sha256").update(ANALYTICS_ASSET_SOURCE).digest("hex").slice(0, 16)}.js`;\n',
        1,
    )
    source_read = '    readFile(path.join(ROOT, "assets", "analytics.js")),\n'
    if text.count(source_read) != 1:
        raise ShipError("versioned_builder_analytics_source_drift")
    text = text.replace(source_read, "    ANALYTICS_ASSET_SOURCE,\n", 1)
    text = text.replace("/assets/analytics.js?v=${ASSET_VERSION}", "/assets/${ANALYTICS_ASSET_FILENAME}")
    write_line = '  await write("assets/analytics.js", await readFile(path.join(ROOT, "assets/analytics.js"), "utf8"));\n'
    if text.count(write_line) != 1:
        raise ShipError("versioned_builder_analytics_write_drift")
    text = text.replace(
        write_line,
        '  await write("assets/analytics.js", ANALYTICS_ASSET_SOURCE);\n'
        '  await write(`assets/${ANALYTICS_ASSET_FILENAME}`, ANALYTICS_ASSET_SOURCE);\n',
        1,
    )
    call_marker = "      const lpHtml = renderAdsLandingPage({\n"
    if text.count(call_marker) != 1:
        raise ShipError("versioned_builder_ads_call_drift")
    return text.replace(
        call_marker,
        call_marker + "        analyticsAssetUrl: `/assets/${ANALYTICS_ASSET_FILENAME}`,\n",
        1,
    )


def transform_versioned_ads_landing(text: str) -> str:
    signature = (
        "  cta, stickyCta, headlines, defaultHeadline, checkedThrough, disclosure, "
        "fdaFootnotes = [], stickyEnabled = true,\n"
    )
    if text.count(signature) != 1:
        raise ShipError("versioned_ads_signature_drift")
    text = text.replace(
        signature,
        "  cta, stickyCta, headlines, defaultHeadline, checkedThrough, disclosure, "
        "fdaFootnotes = [], stickyEnabled = true, analyticsAssetUrl = \"/assets/analytics.js\",\n",
        1,
    )
    script = '<script src="/assets/consent.js" defer></script><script src="/assets/analytics.js" defer></script>'
    if text.count(script) != 1:
        raise ShipError("versioned_ads_script_drift")
    return text.replace(
        script,
        '<script src="/assets/consent.js" defer></script><script src="${analyticsAssetUrl}" defer></script>',
        1,
    )


def prepare_versioned_candidate(predecessor: Path) -> dict[str, Any]:
    if VERSIONED_RELEASE.exists():
        raise ShipError("versioned_asset_release_already_exists")
    if os.path.lexists(VERSIONED_CANDIDATE):
        remove_materialized_tree(VERSIONED_CANDIDATE)
    predecessor_before = release_chain.tree_content_closure(predecessor)
    materialization = copytree_linked(predecessor, VERSIONED_CANDIDATE)
    release_chain.no_symlinks(VERSIONED_CANDIDATE)
    writable_directories = make_directories_writable(VERSIONED_CANDIDATE)
    analytics_bytes = (predecessor / "public/assets/analytics.js").read_bytes()
    analytics_sha = sha_bytes(analytics_bytes)
    filename = f"analytics.{analytics_sha[:16]}.js"
    asset_url = f"/assets/{filename}"
    detached: list[dict[str, Any]] = []
    modified: list[str] = []
    replacement_count = 0
    for source in sorted((predecessor / "public").rglob("*.html")):
        text = source.read_text(encoding="utf-8")
        updated, count = ANALYTICS_REFERENCE.subn(asset_url, text)
        if count == 0:
            continue
        target = VERSIONED_CANDIDATE / source.relative_to(predecessor)
        detached.append(detach_for_write(target))
        target.write_text(updated, encoding="utf-8")
        replacement_count += count
        modified.append(source.relative_to(predecessor).as_posix())
    if replacement_count < 250:
        raise ShipError(f"versioned_asset_reference_coverage_too_small:{replacement_count}")
    hashed_asset = VERSIONED_CANDIDATE / "public/assets" / filename
    if hashed_asset.exists():
        raise ShipError("versioned_asset_collision")
    hashed_asset.write_bytes(analytics_bytes)
    builder = VERSIONED_CANDIDATE / "scripts/build-staging.mjs"
    ads = VERSIONED_CANDIDATE / "src/ads-landing.mjs"
    for target, transform in ((builder, transform_versioned_builder), (ads, transform_versioned_ads_landing)):
        detached.append(detach_for_write(target))
        target.write_text(transform(target.read_text(encoding="utf-8")), encoding="utf-8")
        modified.append(target.relative_to(VERSIONED_CANDIDATE).as_posix())
    remaining = 0
    for path in (VERSIONED_CANDIDATE / "public").rglob("*.html"):
        remaining += len(ANALYTICS_REFERENCE.findall(path.read_text(encoding="utf-8")))
    if remaining:
        raise ShipError(f"versioned_asset_old_public_references_remain:{remaining}")
    return {
        "predecessor_before": predecessor_before,
        "materialization": materialization,
        "writable_directories": writable_directories,
        "detached_files": detached,
        "analytics_sha256": analytics_sha,
        "analytics_filename": filename,
        "analytics_asset_url": asset_url,
        "public_reference_replacements": replacement_count,
        "mutation_paths": sorted(modified + [f"public/assets/{filename}"]),
        "expected_delta": {
            "added": [f"public/assets/{filename}"],
            "modified": sorted(modified),
            "deleted": [],
        },
    }


def verify_versioned_asset(*, deployment_url: str, release: Path, asset_url: str) -> dict[str, Any]:
    expected_asset = (release / "public" / asset_url.lstrip("/")).read_bytes()
    expected_sha = sha_bytes(expected_asset)
    surfaces: dict[str, Any] = {}
    for label, base in {
        "immutable": deployment_url.rstrip("/"),
        "production": "https://glpclinicscout.com",
    }.items():
        root = requests.get(base + "/", timeout=45, headers={"Cache-Control": "no-cache"})
        asset = requests.get(base + asset_url, timeout=45, headers={"Cache-Control": "no-cache"})
        surfaces[label] = {
            "root_http_status": root.status_code,
            "root_references_versioned_asset": asset_url in root.text,
            "asset_url": base + asset_url,
            "asset_http_status": asset.status_code,
            "asset_sha256": sha_bytes(asset.content),
            "asset_matches_expected": asset.status_code == 200 and asset.content == expected_asset,
            "cf_cache_status": asset.headers.get("CF-Cache-Status"),
            "age": asset.headers.get("Age"),
            "cf_ray_present": bool(asset.headers.get("CF-Ray")),
        }
    if not all(
        row["root_http_status"] == 200
        and row["root_references_versioned_asset"]
        and row["asset_matches_expected"]
        for row in surfaces.values()
    ):
        raise ShipError("versioned_asset_live_readback_invalid")
    return {"asset_url": asset_url, "expected_sha256": expected_sha, "surfaces": surfaces}


def prepare_candidate(predecessor: Path) -> dict[str, Any]:
    if RELEASE.exists():
        raise ShipError("affiliati_release_exists_but_is_not_current")
    if os.path.lexists(CANDIDATE):
        remove_materialized_tree(CANDIDATE)
    predecessor_before = release_chain.tree_content_closure(predecessor)
    materialization = copytree_linked(predecessor, CANDIDATE)
    release_chain.no_symlinks(CANDIDATE)
    writable_directories = make_directories_writable(CANDIDATE)
    detached = []
    for relative in MUTATION_PATHS[1:]:
        if (CANDIDATE / relative).exists():
            detached.append(detach_for_write(CANDIDATE / relative))
    overlays = expected_overlays(predecessor)
    for relative, data in overlays.items():
        target = CANDIDATE / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o644)
    return {
        "predecessor_before": predecessor_before,
        "materialization": materialization,
        "writable_directories": writable_directories,
        "detached_files": detached,
        "overlay_sha256": {path.as_posix(): sha_bytes(data) for path, data in overlays.items()},
    }


def run_local_validation(receipt: dict[str, Any], candidate: Path = CANDIDATE) -> None:
    release_chain.run_command(
        receipt,
        ["npm", "test"],
        cwd=candidate,
        timeout=1800,
        output_log_dir=RECEIPT_DIR / "affiliati-postback-command-logs",
        output_log_stem="npm-test",
    )
    node_check = r"""
import {normalizeNetwork, subidParamsFor} from './src/affiliate-subid.mjs';
const hosts = ['https://epsilonmkt.com/a', 'https://maanantaispc.com/b'];
if (hosts.some((u) => normalizeNetwork('', u) !== 'Affiliati')) process.exit(11);
if (JSON.stringify(subidParamsFor('Affiliati')) !== JSON.stringify(['s1','s2'])) process.exit(12);
const js = await import('node:fs').then((m) => m.readFileSync('./assets/analytics.js','utf8'));
if (!js.includes('anchor.dataset.network === "Affiliati"')) process.exit(13);
if (!js.includes('? [clickId, clickId]')) process.exit(14);
const contract = JSON.parse(await import('node:fs').then((m) => m.readFileSync('./data/affiliati-postback-contract.json','utf8')));
if (contract.offers?.length !== 3 || contract.identity_contract?.s1 !== 'cs_click_id primary owned conversion join key') process.exit(15);
"""
    release_chain.run_command(
        receipt,
        ["node", "--input-type=module", "-e", node_check],
        cwd=candidate,
        timeout=120,
    )


def pages_secret_put(receipt: dict[str, Any], secret: str, token: str) -> None:
    env = os.environ.copy()
    env.update({
        "CLOUDFLARE_API_TOKEN": token,
        "CLOUDFLARE_ACCOUNT_ID": release_chain.ACCOUNT,
        "WRANGLER_SEND_METRICS": "false",
    })
    command = [
        "npx", f"wrangler@{release_chain.WRANGLER}", "pages", "secret", "put",
        "AFFILIATI_POSTBACK_KEY", "--project-name", release_chain.PROJECT,
    ]
    completed = subprocess.run(
        command,
        cwd=BUSINESS,
        env=env,
        input=secret + "\n",
        text=True,
        capture_output=True,
        timeout=300,
    )
    receipt.setdefault("commands", []).append({
        "command": "npx wrangler pages secret put AFFILIATI_POSTBACK_KEY --project-name glp-clinic-scout [stdin redacted]",
        "exit_code": completed.returncode,
        "stdout_sha256": sha_bytes(completed.stdout.encode()),
        "stderr_sha256": sha_bytes(completed.stderr.encode()),
        "secret_value_printed": False,
    })
    if completed.returncode != 0:
        raise ShipError("cloudflare_pages_secret_put_failed")
    listed = subprocess.run(
        ["npx", f"wrangler@{release_chain.WRANGLER}", "pages", "secret", "list",
         "--project-name", release_chain.PROJECT],
        cwd=BUSINESS,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    if listed.returncode != 0 or "AFFILIATI_POSTBACK_KEY" not in (listed.stdout + listed.stderr):
        raise ShipError("cloudflare_pages_secret_readback_missing")
    receipt["cloudflare_secret"] = {
        "name": "AFFILIATI_POSTBACK_KEY",
        "type": "secret_text",
        "registered": True,
        "value_printed": False,
    }


def deploy(receipt: dict[str, Any], release: Path, public_tree: str, token: str) -> tuple[str, dict[str, Any]]:
    carrier = Path(tempfile.mkdtemp(prefix="glp-affiliati-postback-deploy-"))
    try:
        for directory in ("functions", "src", "data"):
            shutil.copytree(release / directory, carrier / directory)
        env = os.environ.copy()
        env.update({
            "CLOUDFLARE_API_TOKEN": token,
            "CLOUDFLARE_ACCOUNT_ID": release_chain.ACCOUNT,
            "WRANGLER_SEND_METRICS": "false",
        })
        release_chain.run_command(
            receipt,
            ["npx", f"wrangler@{release_chain.WRANGLER}", "pages", "functions", "build",
             "functions", "--outdir", ".wrangler/tmp/pages-bundle"],
            cwd=carrier,
            timeout=600,
            env=env,
        )
        result = release_chain.run_command(
            receipt,
            ["npx", f"wrangler@{release_chain.WRANGLER}", "pages", "deploy", str(release / "public"),
             "--project-name", release_chain.PROJECT, "--branch", "main", "--commit-dirty=true",
             "--commit-message", "GLP Affiliati authenticated postback edge",
             "--commit-hash", public_tree[:40]],
            cwd=carrier,
            timeout=1200,
            env=env,
        )
        match = re.search(
            r"https://([0-9a-f-]+)\.glp-clinic-scout\.pages\.dev",
            result.stdout + result.stderr,
        )
        if not match:
            raise ShipError("cloudflare_deployment_id_parse_failed")
        detail = release_chain.deployment_detail(token, match.group(1))
        deployment_id = release_chain.canonical_deployment_id(detail, match.group(1))
        if detail.get("environment") != "production" or detail.get("latest_stage", {}).get("status") != "success":
            raise ShipError("cloudflare_new_deployment_not_success")
        if release_chain.d1_ids(detail) != [release_chain.RUNTIME_D1]:
            raise ShipError("cloudflare_new_deployment_d1_invalid")
        return deployment_id, detail
    finally:
        shutil.rmtree(carrier, ignore_errors=True)


def run() -> tuple[int, dict[str, Any]]:
    prior = reconciliation_source()
    receipt_path = LATE_RECONCILIATION_RECEIPT if prior else RECEIPT_PATH
    receipt: dict[str, Any] = {
        "schema": "glp-affiliati-postback-ship-late-reconciliation-v1" if prior else "glp-affiliati-postback-ship-v1",
        "status": "RUNNING",
        "started_at_utc": now(),
        "receipt_path": str(receipt_path),
        "release_name": RELEASE_NAME,
        "declared_mutation_paths": [path.as_posix() for path in MUTATION_PATHS],
        "commands": [],
        "effects": {
            "cloudflare_secret_writes": 0,
            "cloudflare_deployments": 0,
            "content_base_remints": 0,
            "cache_purge_requests": 0,
            "cache_purges": 0,
            "affiliate_portal_writes": 0,
            "tracking_links_requested": 0,
            "tracking_links_clicked": 0,
            "outbound_sends": 0,
            "spend_usd": 0,
        },
        "secrets_printed": False,
    }
    write_receipt(receipt, receipt_path)
    try:
        token = release_chain.credential(
            "CLOUDFLARE_API_TOKEN", Path("/home/ubuntu/.config/cloudflare/api_token")
        )
        configured_account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
        if configured_account and configured_account != release_chain.ACCOUNT:
            raise ShipError("cloudflare_account_identity_mismatch")
        pointer = json.loads(release_chain.CURRENT_BASE.read_text(encoding="utf-8"))
        base = release_chain.validate_base(pointer, token)
        receipt["predecessor"] = {
            "deployment_id": pointer["deployment_id"],
            "release_root": pointer["release_root"],
            "release_tree_sha256": base["release_tree_sha256"],
            "public_tree_sha256": base["public_tree_sha256"],
            "project": base["project"].get("name"),
            "d1_database_ids": release_chain.d1_ids(base["detail"]),
        }
        receipt["predeploy_control_artifacts"] = validate_predeploy_control_artifacts()
        if installed(base["release"]):
            if prior:
                failed_purge = read_json_object(FAILED_PURGE_RECEIPT)
                if (
                    failed_purge.get("status") != "BLOCKED"
                    or (failed_purge.get("cache_purge") or {}).get("http_status") != 401
                    or (failed_purge.get("cache_purge") or {}).get("error_codes") != [10000]
                    or (failed_purge.get("effects") or {}).get("cache_purge_requests") != 1
                ):
                    raise ShipError("failed_purge_receipt_not_exact")
                receipt["prior_failures"] = {
                    "landed_deploy": {"path": str(RECEIPT_PATH), "sha256": sha_file(RECEIPT_PATH)},
                    "cache_purge_401": {"path": str(FAILED_PURGE_RECEIPT), "sha256": sha_file(FAILED_PURGE_RECEIPT)},
                }
                receipt["release_name"] = VERSIONED_RELEASE_NAME
                candidate_info = prepare_versioned_candidate(base["release"])
                receipt["declared_mutation_paths"] = candidate_info["mutation_paths"]
                receipt["candidate"] = {"path": str(VERSIONED_CANDIDATE), **candidate_info}
                run_local_validation(receipt, VERSIONED_CANDIDATE)
                remaining = sum(
                    len(ANALYTICS_REFERENCE.findall(path.read_text(encoding="utf-8")))
                    for path in (VERSIONED_CANDIDATE / "public").rglob("*.html")
                )
                if remaining:
                    raise ShipError(f"versioned_asset_regressed_after_tests:{remaining}")
                release_tree, public_tree, release_rows, release_cow = release_chain.seal_release(
                    VERSIONED_CANDIDATE, VERSIONED_RELEASE, base["release"]
                )
                delta = release_chain.changed_paths(base["release"], VERSIONED_RELEASE)
                if delta != candidate_info["expected_delta"]:
                    raise ShipError(f"versioned_release_scope_invalid:{json.dumps(delta, sort_keys=True)}")
                provenance = constructor_provenance(
                    constructor=Path(__file__).resolve(),
                    predecessor=base["release"],
                    output=VERSIONED_RELEASE,
                    mutation_paths=[Path(value) for value in candidate_info["mutation_paths"]],
                    materializations=[candidate_info["materialization"], release_cow["candidate_to_release"]],
                    predecessor_before=candidate_info["predecessor_before"],
                )
                receipt["release"] = {
                    "path": str(VERSIONED_RELEASE),
                    "release_tree_sha256": release_tree,
                    "public_tree_sha256": public_tree,
                    "row_count": len(release_rows),
                    "changed_paths": delta,
                    "immutable": True,
                    "constructor_provenance": provenance,
                    "cow": release_cow,
                }
                pointer_now = json.loads(release_chain.CURRENT_BASE.read_text(encoding="utf-8"))
                if pointer_now.get("deployment_id") != pointer["deployment_id"]:
                    raise ShipError("predecessor_moved_before_versioned_deploy")
                if release_chain.production_head(token).get("id") != pointer["deployment_id"]:
                    raise ShipError("production_head_moved_before_versioned_deploy")
                deployment_id, detail = deploy(receipt, VERSIONED_RELEASE, public_tree, token)
                receipt["effects"]["cloudflare_deployments"] = 1
                receipt["deployment"] = {
                    "id": deployment_id,
                    "url": detail.get("url"),
                    "environment": detail.get("environment"),
                    "status": detail.get("latest_stage", {}).get("status"),
                    "d1_database_ids": release_chain.d1_ids(detail),
                }
                write_receipt(receipt, receipt_path)
                release_chain.wait_for_production_head(token, deployment_id)
                receipt["sensor_result"] = attach_sensor_result(receipt_path, VERSIONED_RELEASE)
                if receipt["sensor_result"].get("status") not in ("PASS", "PASS_PARTIAL"):
                    raise ShipError("deploy_completeness_sensor_failed")
                remint = release_chain.run_command(
                    receipt,
                    ["sudo", "-n", "python3", str(release_chain.REMINT),
                     "--release", str(VERSIONED_RELEASE), "--deployment-id", deployment_id,
                     "--supersedes-deployment-id", pointer["deployment_id"],
                     "--supersedes-release", pointer["release_root"]],
                    cwd=BUSINESS,
                    timeout=600,
                )
                receipt["effects"]["content_base_remints"] = 1
                receipt["remint"] = json.loads(remint.stdout)
                pointer_after = json.loads(release_chain.CURRENT_BASE.read_text(encoding="utf-8"))
                if (
                    pointer_after.get("deployment_id") != deployment_id
                    or pointer_after.get("release_root") != str(VERSIONED_RELEASE)
                ):
                    raise ShipError("versioned_content_base_remint_readback_invalid")
                immutable_base = str(detail.get("url") or "").rstrip("/")
                receipt["live_auth_gate"] = {
                    "immutable": endpoint_auth_gate(immutable_base + "/api/postback/affiliati"),
                    "production": endpoint_auth_gate(PUBLIC_ENDPOINT),
                }
                receipt["versioned_asset_readback"] = verify_versioned_asset(
                    deployment_url=immutable_base,
                    release=VERSIONED_RELEASE,
                    asset_url=candidate_info["analytics_asset_url"],
                )
                receipt["postback_control_artifacts"] = ensure_control_artifacts(
                    deployment_id=deployment_id,
                    deployment_url=immutable_base,
                    release=VERSIONED_RELEASE,
                )
                receipt["status"] = "PASS_VERSIONED_ASSET_LIVE"
            else:
                receipt["status"] = "SKIP_ALREADY_INSTALLED"
                receipt["live_auth_gate"] = endpoint_auth_gate(PUBLIC_ENDPOINT)
                receipt["postback_control_artifacts"] = ensure_control_artifacts(
                    deployment_id=pointer["deployment_id"],
                    deployment_url=str(base["detail"].get("url") or ""),
                    release=base["release"],
                )
            receipt["completed_at_utc"] = now()
            receipt["blockers"] = []
            write_receipt(receipt, receipt_path)
            return 0, receipt

        candidate_info = prepare_candidate(base["release"])
        receipt["candidate"] = {"path": str(CANDIDATE), **candidate_info}
        run_local_validation(receipt)
        release_tree, public_tree, release_rows, release_cow = release_chain.seal_release(
            CANDIDATE, RELEASE, base["release"]
        )
        delta = release_chain.changed_paths(base["release"], RELEASE)
        if delta != EXPECTED_CHANGED:
            raise ShipError(f"release_scope_invalid:{json.dumps(delta, sort_keys=True)}")
        provenance = constructor_provenance(
            constructor=Path(__file__).resolve(),
            predecessor=base["release"],
            output=RELEASE,
            mutation_paths=MUTATION_PATHS,
            materializations=[candidate_info["materialization"], release_cow["candidate_to_release"]],
            predecessor_before=candidate_info["predecessor_before"],
        )
        receipt["release"] = {
            "path": str(RELEASE),
            "release_tree_sha256": release_tree,
            "public_tree_sha256": public_tree,
            "row_count": len(release_rows),
            "changed_paths": delta,
            "immutable": True,
            "constructor_provenance": provenance,
            "cow": release_cow,
        }
        pointer_now = json.loads(release_chain.CURRENT_BASE.read_text(encoding="utf-8"))
        if pointer_now.get("deployment_id") != pointer["deployment_id"]:
            raise ShipError("predecessor_moved_before_deploy")
        if release_chain.production_head(token).get("id") != pointer["deployment_id"]:
            raise ShipError("production_head_moved_before_deploy")

        pages_secret_put(receipt, read_secret(), token)
        receipt["effects"]["cloudflare_secret_writes"] = 1
        deployment_id, detail = deploy(receipt, RELEASE, public_tree, token)
        receipt["effects"]["cloudflare_deployments"] = 1
        receipt["deployment"] = {
            "id": deployment_id,
            "url": detail.get("url"),
            "environment": detail.get("environment"),
            "status": detail.get("latest_stage", {}).get("status"),
            "d1_database_ids": release_chain.d1_ids(detail),
        }
        write_receipt(receipt, receipt_path)
        release_chain.wait_for_production_head(token, deployment_id)
        receipt["sensor_result"] = attach_sensor_result(receipt_path, RELEASE)
        if receipt["sensor_result"].get("status") not in ("PASS", "PASS_PARTIAL"):
            raise ShipError("deploy_completeness_sensor_failed")
        remint = release_chain.run_command(
            receipt,
            ["sudo", "-n", "python3", str(release_chain.REMINT),
             "--release", str(RELEASE), "--deployment-id", deployment_id,
             "--supersedes-deployment-id", pointer["deployment_id"],
             "--supersedes-release", pointer["release_root"]],
            cwd=BUSINESS,
            timeout=600,
        )
        receipt["effects"]["content_base_remints"] = 1
        receipt["remint"] = json.loads(remint.stdout)
        pointer_after = json.loads(release_chain.CURRENT_BASE.read_text(encoding="utf-8"))
        if pointer_after.get("deployment_id") != deployment_id or pointer_after.get("release_root") != str(RELEASE):
            raise ShipError("content_base_remint_readback_invalid")

        immutable_base = str(detail.get("url") or "").rstrip("/")
        receipt["live_auth_gate"] = {
            "immutable": endpoint_auth_gate(immutable_base + "/api/postback/affiliati"),
            "production": endpoint_auth_gate(PUBLIC_ENDPOINT),
        }
        local_asset = RELEASE / "public/assets/analytics.js"
        local_asset_sha = sha_file(local_asset)
        asset_reads = {}
        for label, url in {
            "immutable": immutable_base + "/assets/analytics.js",
            "production": "https://glpclinicscout.com/assets/analytics.js",
        }.items():
            response = requests.get(url, timeout=45, headers={"Cache-Control": "no-cache"})
            if response.status_code != 200 or sha_bytes(response.content) != local_asset_sha:
                raise ShipError(f"live_asset_readback_invalid:{label}:{response.status_code}")
            asset_reads[label] = {"url": url, "http_status": 200, "sha256": sha_bytes(response.content)}
        receipt["live_asset_readback"] = {"local_sha256": local_asset_sha, **asset_reads}
        receipt["postback_control_artifacts"] = ensure_control_artifacts(
            deployment_id=deployment_id,
            deployment_url=immutable_base,
            release=RELEASE,
        )
        receipt["status"] = "PASS"
        receipt["completed_at_utc"] = now()
        receipt["blockers"] = []
        write_receipt(receipt, receipt_path)
        return 0, receipt
    except Exception as exc:  # noqa: BLE001
        receipt["status"] = "FAIL" if receipt["effects"]["cloudflare_deployments"] else "BLOCKED"
        receipt["completed_at_utc"] = now()
        receipt["blockers"] = [f"{type(exc).__name__}: {exc}"]
        write_receipt(receipt, receipt_path)
        return 1, receipt


def main() -> int:
    import re

    release_chain.LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with release_chain.LOCK_PATH.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "BLOCKED", "blocker": "glp_release_chain_already_running", "reason": "glp_release_chain_already_running"}))
            return 1
        code, receipt = run()
    # Real run() blockers use "ExcName: message". Keep the named cause,
    # stripping URLs and credential-bearing diagnostics before normalization.
    reason = None
    if code:
        reason = "ship_blocked_details_in_receipt"
        for value in receipt.get("blockers") or []:
            if not isinstance(value, str):
                continue
            safe = re.sub(r"https?://\S+", "", value)
            if re.search(r"(?i)\b(?:authorization|bearer|password|secret|token|api[_-]?key)\b", safe):
                continue
            safe = re.sub(r"[^A-Za-z0-9_.:-]+", "_", safe).strip("_")[:160]
            if safe:
                reason = safe
                break
    print(json.dumps({
        "reason": reason,
        "status": receipt.get("status"),
        "receipt": receipt.get("receipt_path", str(RECEIPT_PATH)),
        "deployment_id": (receipt.get("deployment") or {}).get("id"),
        "blockers": receipt.get("blockers") or [],
        "effects": receipt.get("effects") or {},
    }, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
