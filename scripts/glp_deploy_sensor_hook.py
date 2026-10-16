#!/usr/bin/env python3
"""Post-deploy hook: run the completeness sensor, stamp the receipt, alert on failure.

Every GLP Pages deploy script calls `attach_sensor_result(RECEIPT, CANDIDATE)`
immediately after the deployment is confirmed successful. The hook:

  1. runs `scripts/glp_deploy_completeness_sensor.py` against the deployed tree,
  2. writes the sensor's own receipt under the affiliate-machine report dir,
  3. merges a `sensor_result` block into the deploy receipt — a deploy receipt
     without `sensor_result` is by definition unverified,
  4. posts one house-style ops-alerts card when the sensor fails.

It never raises: a hook failure must not corrupt the record of a deployment that
really did ship. A crash is recorded as `sensor_result.status == "ERROR"`, which
is not a pass, so downstream validators still see an unverified deploy.

No Sheet writes, no outreach, no tracking-link clicks, no secrets printed.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any

BUSINESS = Path("/home/ubuntu/business")
MACHINE_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
OPS_ALERTS = "1487300132054765629"
ENV_FILE = Path("/home/ubuntu/.hermes/.env")

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ops"))


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _detail_line(text: str) -> str:
    """One failure, safe for a Discord card: no JSON braces, no timestamps."""
    import re as _re

    line = str(text).replace("\n", " ").replace("\r", " ")
    line = _re.sub(r"[{}\[\]]", "", line)
    line = _re.sub(r"\b20\d{2}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?\S*)?", "", line)
    line = _re.sub(r"\s{2,}", " ", line).strip(" ,;")
    return line[:170]


def build_alert(result: dict[str, Any], receipt_path: str | None) -> str:
    """One house-style alert card (CRON_DISCORD_HOUSE_STYLE_V1 shape).

    Alerts get a 6 nonblank-line budget: title, facts, two details, the single
    meaning line, and the artifact subtext.
    """
    import discord_fmt as fmt

    failed = result.get("failed_checks") or []
    count = int(result.get("failure_count") or 0)
    headline = (f"{len(failed)} completeness check(s) failed after deploy"
                if failed else "post-deploy sensor could not complete")
    lines = [
        fmt.title("action", "GLP Deploy Sensor", headline),
        fmt.kvline(("release", result.get("release") or "unknown"),
                   ("live programs", result.get("live_programs", "unknown")),
                   ("failures", count)),
    ]
    for item in (result.get("failures") or [])[:2]:
        detail = _detail_line(item)
        if detail:
            lines.append(detail)
    lines.append(fmt.meaning("repair the site data named in the sensor receipt before "
                             "treating this deploy as complete"))
    lines.append(fmt.subtext(f"receipt: {receipt_path or 'sensor produced no receipt'}"))
    return fmt.render(*lines, output_class="alert")


def post_ops_alert(message: str) -> dict[str, Any]:
    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "requests_unavailable"}
    token = None
    try:
        for raw in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            if raw.strip().startswith("DISCORD_BOT_TOKEN="):
                token = raw.split("=", 1)[1].strip().strip('"').strip("'")
                break
    except OSError:
        pass
    if not token:
        return {"ok": False, "error": "missing_DISCORD_BOT_TOKEN"}
    try:
        resp = requests.post(
            f"https://discord.com/api/v10/channels/{OPS_ALERTS}/messages",
            headers={"Authorization": f"Bot {token}"},
            json={"content": message[:1900]}, timeout=30)
        return {"ok": resp.status_code < 300, "http_status": resp.status_code,
                "route": OPS_ALERTS}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__, "route": OPS_ALERTS}


def attach_sensor_result(receipt_path: Path | str, release_root: Path | str | None = None,
                         *, offline: bool = False, post_alert: bool = True,
                         live_configuration: bool = False, runtime_root: Path | None = None) -> dict[str, Any]:
    """Run the sensor, stamp the deploy receipt, alert on failure. Never raises."""
    receipt_path = Path(receipt_path)
    summary: dict[str, Any]
    sensor_receipt: str | None = None
    try:
        from glp_deploy_completeness_sensor import run_sensor, write_receipt

        release = Path(release_root) if release_root else None
        options = {"offline": offline}
        if live_configuration or runtime_root is not None:
            options.update(live_configuration=live_configuration, runtime_root=runtime_root)
        result = run_sensor(release, **options)
        sensor_receipt, digest = write_receipt(
            result, MACHINE_DIR / f"deploy-completeness-{_stamp()}.json")
        summary = {
            "schema": result.get("schema"),
            "status": result.get("status"),
            "offline": offline,
            "release_root": result.get("release_root"),
            "release": result.get("release"),
            "live_programs": result.get("live_programs"),
            "failed_checks": result.get("failed_checks"),
            "skipped_checks": result.get("skipped_checks"),
            "failure_count": result.get("failure_count"),
            "failures": (result.get("failures") or [])[:40],
            "receipt_path": sensor_receipt,
            "receipt_sha256": digest,
            "captured_at_utc": result.get("captured_at_utc"),
        }
    except Exception as exc:
        result = {}
        summary = {
            "schema": "glp-deploy-completeness-sensor-v1",
            "status": "ERROR",
            "error": f"{type(exc).__name__}: {exc}",
            "failed_checks": ["sensor_did_not_run"],
            "failure_count": 1,
            "failures": [f"post-deploy sensor did not run: {type(exc).__name__}"],
            "captured_at_utc": dt.datetime.now(dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    # Stamp the deploy receipt. An unstamped receipt is an unverified deploy.
    try:
        if receipt_path.is_file():
            deploy_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if isinstance(deploy_receipt, dict):
                deploy_receipt["sensor_result"] = summary
                body = json.dumps(deploy_receipt, indent=2, sort_keys=True) + "\n"
                receipt_path.write_text(body)
                os.chmod(receipt_path, stat.S_IRUSR | stat.S_IWUSR)
                summary["deploy_receipt_stamped"] = str(receipt_path)
    except Exception as exc:
        summary["deploy_receipt_stamp_error"] = f"{type(exc).__name__}: {exc}"

    if post_alert and summary["status"] not in ("PASS", "PASS_PARTIAL"):
        try:
            summary["ops_alert"] = post_ops_alert(
                build_alert({**result, **summary}, sensor_receipt))
        except Exception as exc:
            summary["ops_alert"] = {"ok": False, "error": type(exc).__name__}
    return summary


# The required run_sensor checks, in source order. A truncated receipt is not a
# completed live audit, even if its top-level status claims PASS.
LIVE_CHECKS = (
    "coverage", "surface_completeness", "publishable_provider_routes",
    "affiliate_surface_tracking", "provider_link_monetization",
    "therapeutic_facet_bindings", "count_copy", "derived_tallies",
    "research_binding", "freshness_stamps", "page_fact_consistency", "identity_agreement",
    "identity_dedup", "selectable_option_dedup", "link_graph_floor",
    "score_binding", "aeo_surface", "base_chain",
)
# Existing business output-health contract for this same completeness receipt.
MAX_SENSOR_AGE = dt.timedelta(hours=36)


def _utc_time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(dt.timezone.utc) if parsed.tzinfo else None


def _remint_target_args(command: str, deployment_id: str, release_root: str) -> bool:
    """Parse the native space-joined argv; ambiguous whitespace cannot prove a target."""
    parts = command.split()
    if (release_root.split() != [release_root] or deployment_id.split() != [deployment_id]
            or len(parts) < 8 or parts[:4] != [
                "sudo", "-n", "python3",
                str(BUSINESS / "systems/seo-blog-machine/scripts/remint_content_base.py")]
            or len(parts[4:]) % 2):
        return False
    options: dict[str, str] = {}
    allowed = {"--release", "--deployment-id", "--supersedes-deployment-id", "--supersedes-release"}
    for flag, value in zip(parts[4::2], parts[5::2]):
        if flag not in allowed or flag in options or value.startswith("--"):
            return False
        options[flag] = value
    return options.get("--release") == release_root and options.get("--deployment-id") == deployment_id


def _bound_event_time(receipt: dict[str, Any], deployment_id: str,
                      release_root: str) -> dt.datetime | None:
    """Find the successful target event in the two registered receipt schemas.

    Legacy completion precedes the hook; weekly chain completion follows it.
    The weekly remint readback needs its successful matching command, not just
    an optional timestamp string on an unrelated pointer.
    """
    deploy = receipt.get("deployment")
    if not isinstance(deploy, dict) or deploy.get("id") != deployment_id:
        return None
    if receipt.get("deployment_id") not in (None, deployment_id):
        return None
    if receipt.get("schema") == "glp-main-identical-inputs-receipt-v1":
        pointer = receipt.get("terminal_pointer")
        if not isinstance(pointer, dict):
            return None
        try:
            spec = pointer['final_receipt']
            raw = Path(spec['path']).read_bytes()
            terminal = json.loads(raw)
            if (hashlib.sha256(raw).hexdigest() != spec['sha256']
                    or pointer.get('schema') != 'glp-opus-current-content-base-v1'
                    or pointer.get('status') != 'PASS'
                    or pointer.get('deployment_id') != deployment_id
                    or pointer.get('release_root') != release_root
                    or not pointer.get('site_head')
                    or terminal.get('status') != 'PASS_PRODUCTION_TERMINAL'
                    or terminal.get('site_head') != pointer['site_head']
                    or terminal.get('deployment', {}).get('deployment_id') != deployment_id):
                return None
            return _utc_time(pointer.get('reminted_at_utc'))
        except (OSError, ValueError, KeyError, TypeError):
            return None
    if receipt.get("schema") == "glp-enrichment-production-deployment-receipt-v1":
        stage = deploy.get("latest_stage")
        apply = receipt.get("apply")
        created = _utc_time(deploy.get("created_on"))
        completed = _utc_time(receipt.get("completed_at_utc"))
        if (receipt.get("status") != "PASS_DEPLOYED_READBACK_REQUIRED"
                or not isinstance(stage, dict) or stage.get("status") != "success"
                or not isinstance(apply, dict) or apply.get("exit_code") != 0
                or not created or not completed or created > completed):
            return None
        if "ended_on" in stage:
            ended = _utc_time(stage["ended_on"])
            if not ended or not created <= ended <= completed:
                return None
        return completed

    if receipt.get("schema") == "glp-price-index-release-chain-v1":
        remint = receipt.get("remint")
        effects = receipt.get("external_effects")
        if not isinstance(remint, dict) or not isinstance(effects, dict):
            return None
        pointer = remint.get("readback")
        if (receipt.get("status") not in ("REMINTED_SENSOR_PENDING",
                                            "SENSOR_PASSED_INDEXNOW_PENDING", "PASS")
                or remint.get("status") != "PASS" or not isinstance(pointer, dict)
                or pointer.get("schema") != "glp-opus-current-content-base-v1"
                or pointer.get("status") != "PASS"
                or pointer.get("deployment_id") != deployment_id
                or pointer.get("release_root") != release_root
                or effects.get("cloudflare_deployments") != 1
                or effects.get("current_content_base_remints") != 1
                or deploy.get("status") != "success"
                or deploy.get("deployment_id") != deployment_id):
            return None
        minted = _utc_time(pointer.get("reminted_at_utc"))
        commands = receipt.get("commands")
        if not minted or not isinstance(commands, list):
            return None
        deploy_commands = [(index, command) for index, command in enumerate(commands)
                           if isinstance(command, dict) and isinstance(command.get("command"), str)
                           and command["command"].split()[2:4] == ["pages", "deploy"]]
        remint_commands = [(index, command) for index, command in enumerate(commands)
                           if isinstance(command, dict) and isinstance(command.get("command"), str)
                           and "remint_content_base.py" in command["command"]]
        if len(deploy_commands) != 1 or len(remint_commands) != 1:
            return None
        deploy_index, pages = deploy_commands[0]
        remint_index, command = remint_commands[0]
        pages_parts = pages["command"].split()
        deployed_at = _utc_time(pages.get("started_at_utc"))
        started = _utc_time(command.get("started_at_utc"))
        # The producer rounds reminted_at_utc to whole seconds. Reject a stale
        # replay, while allowing the command start to carry subsecond precision.
        # Its Pages command is recorded before the remint, not after capture.
        if (pages.get("exit_code") != 0 or not deployed_at
                or pages_parts[0] != "npx" or not pages_parts[1].startswith("wrangler@")
                or pages_parts[4:5] != [str(Path(release_root) / "public")]
                or deploy_index >= remint_index
                or command.get("exit_code") != 0 or not started
                or deployed_at > started
                or minted < started.replace(microsecond=0)
                or not _remint_target_args(command["command"], deployment_id, release_root)):
            return None
        return minted
    return None


def receipt_is_sensor_verified(receipt: dict[str, Any]) -> tuple[bool, str]:
    """Verify an exact-target, fresh, complete *live* raw sensor receipt.

    Offline PASS_PARTIAL remains a useful pre-deploy report, never live proof.
    Do not trust the small mutable summary as a substitute for its hashed raw
    receipt or infer canonical identity from deployment creation order.
    """
    sensor = receipt.get("sensor_result")
    if not isinstance(sensor, dict):
        return False, "deploy receipt carries no sensor_result (deploy is unverified)"
    if sensor.get("status") != "PASS":
        return False, "sensor_result is not a complete live PASS"
    # GLP-73 R2: only evidence that STATES it was a live run counts. A summary
    # whose mode is offline, unstated or not a boolean is unknown, not live.
    if sensor.get("offline") is not False:
        return False, "sensor_result mode is offline or unstated (not live evidence)"
    path, digest = sensor.get("receipt_path"), sensor.get("receipt_sha256")
    if not isinstance(path, str) or not path or not isinstance(digest, str) or len(digest) != 64:
        return False, "sensor raw receipt path/hash missing"
    try:
        raw_bytes = Path(path).read_bytes()
        if hashlib.sha256(raw_bytes).hexdigest() != digest:
            return False, "sensor raw receipt hash mismatch"
        raw = json.loads(raw_bytes)
    except (OSError, ValueError, TypeError):
        return False, "sensor raw receipt unreadable"
    if not isinstance(raw, dict) or raw.get("schema") != "glp-deploy-completeness-sensor-v1":
        return False, "sensor raw receipt schema invalid"
    if any(sensor.get(key) != raw.get(key) for key in (
            "schema", "status", "release", "release_root", "captured_at_utc",
            "failure_count", "failed_checks", "skipped_checks")):
        return False, "sensor summary differs from raw receipt"
    if (raw.get("status") != "PASS" or type(raw.get("failure_count")) is not int
            or raw["failure_count"] != 0 or raw.get("failed_checks") != []
            or raw.get("skipped_checks") != [] or raw.get("failures") != []):
        return False, "sensor raw receipt is partial or failed"
    checks = raw.get("checks")
    if (not isinstance(checks, list) or len(checks) != len(LIVE_CHECKS)
            or [c.get("check") for c in checks if isinstance(c, dict)] != list(LIVE_CHECKS)
            or any(not isinstance(c, dict) or c.get("status") != "PASS" for c in checks)):
        return False, "required live checks missing, skipped or failed"
    # Both forms are produced by existing deploy callers; never use a
    # predecessor or a sensor-supplied ID as the expected deployment target.
    deploy = receipt.get("deployment")
    deployment_id = receipt.get("deployment_id") or (deploy.get("id") if isinstance(deploy, dict) else None)
    release = receipt.get("release")
    release_root = (receipt.get("release_root") or receipt.get("candidate_root")
                    or (release.get("path") if isinstance(release, dict) else None))
    if (not isinstance(deployment_id, str) or not deployment_id
            or not isinstance(release_root, str) or not Path(release_root).is_absolute()
            or raw.get("release_root") != release_root
            or raw.get("release") != Path(release_root).name):
        return False, "sensor release/deployment target unestablished or mismatched"
    base = checks[-1]
    if (base.get("base_deployment_id") != deployment_id
            or base.get("live_canonical_deployment_id") != deployment_id):
        return False, "sensor canonical deployment differs from deploy receipt"
    try:
        captured = dt.datetime.fromisoformat(raw["captured_at_utc"].replace("Z", "+00:00"))
        age = dt.datetime.now(dt.timezone.utc) - captured
    except (KeyError, AttributeError, TypeError, ValueError):
        return False, "sensor capture time invalid"
    if captured.tzinfo is None or not dt.timedelta(0) <= age <= MAX_SENSOR_AGE:
        return False, "sensor receipt stale or in the future"
    event = _bound_event_time(receipt, deployment_id, release_root)
    if event is None or captured.astimezone(dt.timezone.utc) < event:
        return False, "successful deployment/remint event missing, invalid or after sensor capture"
    return True, "fresh complete live sensor receipt matches deployment and raw hash"


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("receipt", help="deploy receipt to stamp")
    ap.add_argument("--release", help="deployed tree root")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-alert", action="store_true")
    ns = ap.parse_args()
    out = attach_sensor_result(ns.receipt, ns.release, offline=ns.offline,
                               post_alert=not ns.no_alert)
    print(json.dumps(out, indent=2, sort_keys=True))
    sys.exit(0 if out.get("status") in ("PASS", "PASS_PARTIAL") else 1)
