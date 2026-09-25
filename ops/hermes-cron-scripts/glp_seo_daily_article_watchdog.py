#!/usr/bin/env python3
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"GLP SEO Close Watchdog","output_class":"alert","composer_type":"deterministic_script","privacy_level":"internal","fixture":"⚠️ **GLP SEO Close Watchdog** — zero valid new-article closes for the PT day\n\n→ inspect mint owner state and carrier claim; no publication is inferred\n-# healthy days are silent","fixture_output_class":"alert","fixture_test":"systems/seo-blog-machine/tests/test_defect8_carrier.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":true}
"""Once-per-PT-day watchdog for valid new-article carrier closes."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROFILE_SCRIPTS = Path("/home/ubuntu/.hermes/scripts")
if str(PROFILE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PROFILE_SCRIPTS))

from glp_seo_daily_article_carrier import (
    DEFAULT_ROOT,
    read_json,
    seal,
    self_hash,
    sha,
    validate_claim,
    validate_close,
    write_exclusive,
    pt_day,
)

DEFAULT_JOBS = Path("/home/ubuntu/.hermes/cron/jobs.json")
DEFAULT_MINT_JOB = "b8948c536d31"


def job_state(path: Path, job_id: str) -> dict[str, Any]:
    if not path.is_file():
        return {"status": "jobs_registry_missing", "job_id": job_id}
    payload = read_json(path)
    candidates = [row for row in payload.get("jobs") or [] if str(row.get("id") or "").startswith(job_id)]
    if len(candidates) != 1:
        return {"status": "mint_job_not_unique", "job_id": job_id, "matches": len(candidates)}
    row = candidates[0]
    nested_state = row.get("state")
    state = nested_state if isinstance(nested_state, dict) else row
    return {
        "status": state.get("last_status") or "never_run",
        "job_id": row.get("id"),
        "name": row.get("name"),
        "last_run_at": state.get("last_run_at"),
        "last_error": state.get("last_error"),
    }


def count_closes(root: Path, date: str) -> tuple[list[Path], list[dict[str, str]]]:
    day_root = root.resolve() / date
    valid: list[Path] = []
    invalid: list[dict[str, str]] = []
    if not day_root.is_dir():
        return valid, invalid
    for path in sorted(day_root.glob("close-*.json")):
        try:
            close = validate_close(path)
            if close.get("pt_date") == date:
                valid.append(path)
            else:
                invalid.append({"path": str(path), "reason": "pt_date_mismatch"})
        except Exception as exc:
            invalid.append({"path": str(path), "reason": str(exc)})
    return valid, invalid


def claim_state(root: Path, date: str) -> dict[str, Any]:
    path = root.resolve() / date / "claim.json"
    if not path.is_file():
        stage = root.resolve() / date / "selection-stage.json"
        return {"status": "UNCLAIMED", "selection_staged": stage.is_file()}
    try:
        claim, _, _, _ = validate_claim(path)
        return {
            "status": claim["status"],
            "path": str(path),
            "sha256": sha(path),
            "owner_id": claim["owner_id"],
            "topic_id": claim["topic_id"],
        }
    except Exception as exc:
        return {"status": "INVALID_CLAIM", "path": str(path), "reason": str(exc)}


def run(root: Path, date: str, jobs: Path, mint_job_id: str) -> str:
    valid, invalid = count_closes(root, date)
    if valid:
        return ""
    alert_path = root.resolve() / date / "watchdog-alert.json"
    if alert_path.exists():
        alert = read_json(alert_path)
        if (alert.get("schema") != "glp-seo-daily-article-watchdog-alert-v1"
                or alert.get("alert_sha256") != self_hash(alert, "alert_sha256")):
            raise RuntimeError("watchdog_alert_marker_invalid")
        return ""
    claim = claim_state(root, date)
    blocker = "no_valid_new_article_close"
    if claim["status"] == "UNCLAIMED":
        blocker = "daily_article_unclaimed"
    elif claim["status"] == "INVALID_CLAIM":
        blocker = "daily_claim_invalid"
    alert = seal({
        "schema": "glp-seo-daily-article-watchdog-alert-v1",
        "status": "ALERT_ZERO_VALID_NEW_ARTICLE_CLOSES",
        "pt_date": date,
        "mint_job": job_state(jobs.resolve(), mint_job_id),
        "claim": claim,
        "valid_close_count": 0,
        "invalid_close_artifacts": invalid,
        "blocker": blocker,
        "artifact_root": str(root.resolve() / date),
    }, "alert_sha256")
    write_exclusive(alert_path, alert)
    return json.dumps(alert, sort_keys=True, separators=(",", ":"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--pt-date")
    parser.add_argument("--jobs", type=Path, default=DEFAULT_JOBS)
    parser.add_argument("--mint-job-id", default=DEFAULT_MINT_JOB)
    args = parser.parse_args()
    output = run(args.artifact_root, pt_day(args.pt_date), args.jobs, args.mint_job_id)
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
