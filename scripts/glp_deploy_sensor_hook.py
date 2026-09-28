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

sys.path.insert(0, str(BUSINESS / "scripts"))
sys.path.insert(0, str(BUSINESS / "ops"))


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


def receipt_is_sensor_verified(receipt: dict[str, Any]) -> tuple[bool, str]:
    """Contract for validators: a deploy receipt must carry a passing sensor_result."""
    sensor = receipt.get("sensor_result")
    if not isinstance(sensor, dict):
        return False, "deploy receipt carries no sensor_result (deploy is unverified)"
    status = sensor.get("status")
    if status not in ("PASS", "PASS_PARTIAL"):
        return False, (f"deploy receipt sensor_result status is {status!r} with "
                       f"{sensor.get('failure_count')} failure(s)")
    return True, "sensor_result present and passing"


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
