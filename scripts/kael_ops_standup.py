#!/home/ubuntu/.hermes/hermes-agent/venv/bin/python
"""Tiny Kael ops standup / freshness check.

Purpose: prevent stale shared truth and overdue gates from surviving silently.
This is not the retired morning briefing. It is a bounded ops-health check:
STATE freshness, current hard-gate/readout presence, pending approvals, cron
health, revenue-watchdog status, and one next action. It does not mutate
external business systems.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/home/ubuntu/business/ops")
from discord_fmt import kvline, meaning, render, subtext, title

BASE = Path("/home/ubuntu/business")
HERMES = Path("/home/ubuntu/.hermes")
STATE = BASE / "STATE.md"
PNL = BASE / "daily_pnl.md"
REPORT_DIR = BASE / "reports" / "ops-standups"
USER_STORY_DIR = BASE / "data" / "hermes_user_story_daily"
CRON_JOBS = HERMES / "cron" / "jobs.json"
GOOGLE_TOKEN = HERMES / "google_token.json"

# Historical Path D receipts are checked only when STATE says Path D is active.
PATH_D_READOUT = BASE / "reports" / "path-d-120h-readout-2026-05-11.md"
PATH_D_EVIDENCE = BASE / "reports" / "path-d-readout-evidence-2026-05-11.json"

TLCC_ROOT = BASE / "ventures" / "tech-layoff-command-center"
VALIDATION_SENTINELS = BASE / "reports" / "validation-sprint"
SPREADSHEET_ID = "1jdnC3oMCxcFq7Clfv1PQTNABIYcAPXLTuBhwdfp_yZ0"


def now_pair() -> tuple[datetime, datetime]:
    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    return now_utc, now_utc.astimezone(ZoneInfo("America/Los_Angeles"))


def read_json_loose(path: Path):
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="ignore")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(text, strict=False)


def compact(value: str | None, limit: int = 260) -> str:
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def first_state_value(text: str, label: str, limit: int = 260) -> str:
    match = re.search(rf"^- {re.escape(label)}:\s*(.+)$", text, re.M)
    return compact(match.group(1), limit) if match else ""


def section_bullets(text: str, heading: str, max_lines: int = 5, limit: int = 260) -> list[str]:
    match = re.search(rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        return []
    bullets: list[str] = []
    for raw in match.group("body").splitlines():
        line = raw.strip()
        if line.startswith("- "):
            bullets.append(compact(line[2:], limit))
            if len(bullets) >= max_lines:
                break
    return bullets


def parse_state(now_utc: datetime) -> dict:
    text = STATE.read_text(encoding="utf-8") if STATE.exists() else ""
    last = None
    m = re.search(r"^Last updated:\s*(.+)$", text, re.M)
    if m:
        raw = m.group(1).strip()
        raw_clean = re.sub(r"\s*\(.+?\)\s*$", "", raw).strip()
        # STATE.md normally uses strings like `2026-05-11 16:54:56 PDT`,
        # `2026-06-03 06:55 UTC`, or ISO UTC strings. Python's strptime is
        # inconsistent for PDT/PST, so parse zone labels explicitly.
        pacific_match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)\s+(PDT|PST)$", raw_clean)
        if pacific_match:
            body = pacific_match.group(1)
            fmt = "%Y-%m-%d %H:%M:%S" if body.count(":") == 2 else "%Y-%m-%d %H:%M"
            dt = datetime.strptime(body, fmt)
            last = dt.replace(tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(timezone.utc)
        utc_match = re.match(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)\s+UTC$", raw_clean)
        if last is None and utc_match:
            body = utc_match.group(1)
            fmt = "%Y-%m-%d %H:%M:%S" if body.count(":") == 2 else "%Y-%m-%d %H:%M"
            last = datetime.strptime(body, fmt).replace(tzinfo=timezone.utc)
        if last is None:
            try:
                last = datetime.fromisoformat(raw_clean.replace("Z", "+00:00"))
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
            except Exception:
                last = None

    active_mission = first_state_value(text, "Active mission", 320)
    current_gate = first_state_value(text, "Current execution gate", 320)
    current_phase_lines = section_bullets(text, "Current phase", max_lines=3, limit=320)
    waiting_on_izaiah = section_bullets(text, "Waiting on Izaiah", max_lines=4, limit=300)

    old_phase = ""
    m2 = re.search(r"^- Phase:\s*\*\*(.+?)\*\*$", text, re.M)
    if m2:
        old_phase = compact(m2.group(1), 260)

    phase = active_mission or (current_phase_lines[0] if current_phase_lines else old_phase)
    current_truth = " ".join([active_mission, current_gate, *current_phase_lines])
    path_d_active = bool(re.search(r"\bPath D\b", current_truth, re.I)) and not bool(
        re.search(r"\b(F2|TLCC|Tech Layoff|Don.?t Sign Yet)\b", current_truth, re.I)
    )
    tlcc_active = bool(re.search(r"\b(F2|TLCC|Tech Layoff|Don.?t Sign Yet)\b", current_truth, re.I))

    age_hours = None
    stale = False
    if last:
        age_hours = round((now_utc - last.astimezone(timezone.utc)).total_seconds() / 3600, 2)
        stale = age_hours > 26
    return {
        "exists": STATE.exists(),
        "last_updated_age_hours": age_hours,
        "stale": stale,
        "phase": phase,
        "active_mission": active_mission,
        "current_gate": current_gate,
        "current_phase_lines": current_phase_lines,
        "waiting_on_izaiah": waiting_on_izaiah,
        "path_d_active": path_d_active,
        "tlcc_active": tlcc_active,
        "text": text,
    }


def latest_file(pattern: str, root: Path = BASE) -> str | None:
    files = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    return str(files[-1]) if files else None


def latest_from_patterns(patterns: list[str], root: Path = BASE) -> str | None:
    files: list[Path] = []
    for pattern in patterns:
        files.extend(root.glob(pattern))
    files = sorted(set(files), key=lambda p: p.stat().st_mtime if p.exists() else 0)
    return str(files[-1]) if files else None


def latest_user_story_json_path() -> Path | None:
    files = sorted(
        USER_STORY_DIR.glob("hermes-user-story-intel-*.json"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
    )
    return files[-1] if files else None


def proposal_status() -> dict:
    path = latest_user_story_json_path()
    data = read_json_loose(path) if path else {}
    props = (data or {}).get("proposals", [])
    pending = [p.get("id") for p in props if p.get("status") == "pending_operator_decision"]
    rejected = [p.get("id") for p in props if "rejected" in str(p.get("status", ""))]
    approved = [p.get("id") for p in props if str(p.get("status", "")).startswith("approved")]
    return {"pending": pending, "approved": approved, "rejected": rejected, "path": str(path) if path else None}


def cron_status(now_utc: datetime) -> dict:
    data = read_json_loose(CRON_JOBS) or {}
    jobs = data.get("jobs", data if isinstance(data, list) else [])
    enabled = [j for j in jobs if j.get("enabled", True)]
    issues = []
    for j in enabled:
        name = j.get("name") or j.get("id")
        if j.get("last_delivery_error"):
            issues.append(f"{name}: delivery_error={j.get('last_delivery_error')}")
        if j.get("last_status") not in (None, "ok"):
            issues.append(f"{name}: last_status={j.get('last_status')}")
        nxt = j.get("next_run_at")
        if nxt:
            try:
                dt = datetime.fromisoformat(nxt.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                lag = (now_utc - dt.astimezone(timezone.utc)).total_seconds() / 60
                if lag > 60:
                    issues.append(f"{name}: next_run_at is {round(lag,1)}m in the past")
            except Exception:
                issues.append(f"{name}: unparsable next_run_at={nxt}")
        if j.get("no_agent") and j.get("script"):
            script_path = HERMES / "scripts" / j["script"]
            if not script_path.exists():
                issues.append(f"{name}: missing script {j['script']}")
    return {"enabled_count": len(enabled), "issues": issues, "path": str(CRON_JOBS)}


def path_d_status(state: dict) -> dict:
    if not state.get("path_d_active"):
        return {"active": False, "summary": "inactive per STATE.md"}
    evidence = read_json_loose(PATH_D_EVIDENCE) or {}
    totals = evidence.get("totals") or {}
    return {
        "active": True,
        "readout_exists": PATH_D_READOUT.exists(),
        "readout_path": str(PATH_D_READOUT),
        "evidence_path": str(PATH_D_EVIDENCE) if PATH_D_EVIDENCE.exists() else None,
        "sent": totals.get("sent"),
        "responses": totals.get("responses"),
        "buys": totals.get("buys"),
        "google_sheet_status": (evidence.get("live_google_sheet_api_attempt") or {}).get("status"),
        "google_sheet_error": (evidence.get("live_google_sheet_api_attempt") or {}).get("error_summary"),
    }


def tlcc_status(state: dict) -> dict:
    text = state.get("text", "")
    active = bool(state.get("tlcc_active"))
    d3_pdf = TLCC_ROOT / "03-dont-sign-yet-pack-v1.pdf"
    d3_md = TLCC_ROOT / "03-dont-sign-yet-pack-v1.md"
    d4_final_report = BASE / "reports" / "tlcc-d4-d6-build-packet-final-2026-06-03.md"
    overnight_report = BASE / "reports" / "tlcc-d4-d6-overnight-morning-report-2026-06-03.md"
    latest_report = latest_from_patterns([
        "reports/tlcc-*.md",
        "ventures/tech-layoff-command-center/tlcc-*.md",
    ])
    content_batch = latest_from_patterns([
        "ventures/tech-layoff-command-center/f2-stream-b-content-batch-review-*.md",
    ])
    attorney_report = latest_from_patterns([
        "ventures/tech-layoff-command-center/d6-attorney-outreach-timing-first-wave-recommendation-*.md",
    ])

    missing = []
    if active and not d3_pdf.exists():
        missing.append("D3 paid-pack PDF missing")
    if active and not d3_md.exists():
        missing.append("D3 paid-pack source MD missing")
    if active and not (d4_final_report.exists() or overnight_report.exists() or latest_report):
        missing.append("TLCC D4/D6 build or staging report missing")
    if active and not content_batch:
        missing.append("F2 Stream B content batch artifact missing")
    if active and not attorney_report:
        missing.append("D6 attorney outreach recommendation artifact missing")

    approval_gates = []
    if re.search(r"Approve/edit latest D3|D3 .*pending operator/advisor approval", text, re.I):
        approval_gates.append("D3 Don’t Sign Yet Pack approval/edit")
    if re.search(r"review the final D4/D6|D4/D6 .*before any checkout|checkout/deploy/outreach gate", text, re.I):
        approval_gates.append("D4/D6 checkout/deploy/outreach packet review")
    if re.search(r"review D6 attorney|attorney outreach packet|manual contact", text, re.I):
        approval_gates.append("D6 attorney outreach review")
    if re.search(r"approve/edit the revised 88-post|content batch", text, re.I):
        approval_gates.append("Stream B X content batch review")
    if re.search(r"Stripe `sk_test` key|live payment link|MailerLite/free opt-in|custom domain/support mailbox|noindex removal", text, re.I):
        approval_gates.append("checkout/opt-in/domain/support activation inputs")

    # Deduplicate while preserving order.
    seen = set()
    approval_gates = [g for g in approval_gates if not (g in seen or seen.add(g))]
    return {
        "active": active,
        "d3_pdf": str(d3_pdf) if d3_pdf.exists() else None,
        "d4_final_report": str(d4_final_report) if d4_final_report.exists() else None,
        "overnight_report": str(overnight_report) if overnight_report.exists() else None,
        "latest_report": latest_report,
        "content_batch": content_batch,
        "attorney_report": attorney_report,
        "approval_gates": approval_gates,
        "missing": missing,
    }


def google_sheet_probe() -> dict:
    # Read-only probe. Prints no token/account data. If refresh succeeds, token may be refreshed in local credential store.
    if not GOOGLE_TOKEN.exists():
        return {"status": "missing_token", "ok": False, "detail": "google_token.json missing"}
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN), ["https://www.googleapis.com/auth/spreadsheets"])
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            GOOGLE_TOKEN.write_text(creds.to_json(), encoding="utf-8")
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID, fields="spreadsheetId").execute()
        return {"status": "ok", "ok": True, "detail": "read-only spreadsheet metadata probe succeeded"}
    except Exception as e:
        detail = str(e)
        if "invalid_grant" in detail:
            detail = "invalid_grant: token expired or revoked"
        return {"status": "blocked", "ok": False, "detail": detail[:240]}


def pnl_status() -> dict:
    text = PNL.read_text(encoding="utf-8") if PNL.exists() else ""
    today = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    return {"today_entry_present": today in text, "path": str(PNL)}


def watchdog_status(path: str | None) -> dict:
    if not path:
        return {"present": False, "severity": None, "issues": ["Stripe poller watchdog artifact missing"]}
    data = read_json_loose(Path(path)) or {}
    issues = data.get("issues") or data.get("warnings") or []
    severity = data.get("severity") or data.get("status")
    ok = str(severity).upper() in {"OK", "HEALTHY", "SUCCESS"} and not issues
    return {
        "present": True,
        "ok": ok,
        "severity": severity,
        "issues": issues,
        "job_enabled": data.get("job_enabled"),
        "job_last_status": data.get("job_last_status"),
        "age_minutes": data.get("latest_output_age_minutes") or data.get("job_age_minutes"),
    }


def build_report(cron_mode: bool = False) -> tuple[Path, str, dict]:
    now_utc, now_pt = now_pair()
    state = parse_state(now_utc)
    proposals = proposal_status()
    cron = cron_status(now_utc)
    pathd = path_d_status(state)
    tlcc = tlcc_status(state)
    google = google_sheet_probe()
    pnl = pnl_status()
    sentinel = latest_file("reports/validation-sprint/sentinel-*.md")
    watchdog = latest_file("reports/cron-health/stripe-poller-watchdog-*.json")
    watchdog_meta = watchdog_status(watchdog)

    blockers = []
    warnings = []
    if state.get("stale"):
        blockers.append(f"STATE stale: {state.get('last_updated_age_hours')}h old")
    if tlcc.get("active"):
        if tlcc.get("missing"):
            blockers.extend(["F2/TLCC artifact missing: " + x for x in tlcc["missing"]])
        if tlcc.get("approval_gates"):
            blockers.append("Current F2/TLCC approval gates: " + "; ".join(tlcc["approval_gates"]))
    elif pathd.get("active") and not pathd.get("readout_exists"):
        blockers.append("Path D 120h readout missing")
    elif not tlcc.get("active") and not pathd.get("active"):
        warnings.append("STATE mission detector found neither F2/TLCC nor Path D; inspect STATE.md manually")
    if proposals.get("pending"):
        blockers.append("Pending operator proposal decisions: " + ", ".join(proposals["pending"]))
    if cron.get("issues"):
        blockers.extend(["Cron: " + x for x in cron["issues"][:6]])
    if watchdog_meta.get("present") and not watchdog_meta.get("ok"):
        warnings.append("Stripe poller watchdog not OK: " + compact(str(watchdog_meta.get("issues") or watchdog_meta.get("severity")), 180))
    if not watchdog_meta.get("present"):
        warnings.append("Stripe poller watchdog artifact missing")
    if not google.get("ok"):
        warnings.append("Google Sheets API blocked: " + google.get("detail", "unknown"))
    if not pnl.get("today_entry_present"):
        warnings.append("daily_pnl has no current Pacific-day entry")

    if blockers:
        status = "blocked"
    elif warnings:
        status = "warning"
    else:
        status = "healthy"

    if blockers:
        next_action = blockers[0]
    elif warnings:
        next_action = warnings[0]
    else:
        next_action = "Nothing urgent. Continue only approved revenue work."

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"kael-ops-standup-{now_pt.date().isoformat()}.md"
    lines = [
        f"# Kael ops standup / freshness check — {now_pt.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        "",
        f"Status: **{status}**",
        "",
        "## What I checked",
        "- STATE.md freshness and current mission/gate truth",
        "- Current F2/TLCC artifact/readout presence when STATE says F2/TLCC is active",
        "- User-story intel proposal decision state from the latest daily JSON",
        "- Hermes cron job health from `~/.hermes/cron/jobs.json`",
        "- Stripe watchdog/sentinel artifact pointers",
        "- Google Sheets read-only API access",
        "- daily_pnl current-day entry",
        "",
        "## Current snapshot",
        f"- STATE stale: `{state.get('stale')}`; age_hours: `{state.get('last_updated_age_hours')}`",
        f"- Active mission: {state.get('active_mission') or 'unknown'}",
        f"- Current gate: {state.get('current_gate') or 'not detected'}",
    ]
    if tlcc.get("active"):
        lines.extend([
            f"- F2/TLCC latest report: `{tlcc.get('latest_report')}`",
            f"- F2/TLCC D3 PDF: `{tlcc.get('d3_pdf')}`",
            f"- F2/TLCC content batch: `{tlcc.get('content_batch')}`",
            f"- F2/TLCC attorney report: `{tlcc.get('attorney_report')}`",
            f"- F2/TLCC approval gates: `{tlcc.get('approval_gates')}`",
        ])
    if pathd.get("active"):
        lines.append(f"- Path D readout exists: `{pathd.get('readout_exists')}`; sent/responses/buys: `{pathd.get('sent')}/{pathd.get('responses')}/{pathd.get('buys')}`")
    lines.extend([
        f"- Proposal decisions: approved `{proposals.get('approved')}`, rejected `{proposals.get('rejected')}`, pending `{proposals.get('pending')}`; source `{proposals.get('path')}`",
        f"- Enabled Hermes cron jobs: `{cron.get('enabled_count')}`; cron issues: `{len(cron.get('issues') or [])}`",
        f"- Stripe watchdog: `{watchdog_meta.get('severity')}`; enabled `{watchdog_meta.get('job_enabled')}`; last_status `{watchdog_meta.get('job_last_status')}`; age_min `{watchdog_meta.get('age_minutes')}`",
        f"- Google Sheets probe: `{google.get('status')}` — {google.get('detail')}",
        f"- Latest sentinel: `{sentinel}`",
        f"- Latest Stripe watchdog: `{watchdog}`",
        "",
        "## Blockers",
    ])
    lines.extend([f"- {b}" for b in blockers] or ["- None"])
    lines.extend(["", "## Warnings"])
    lines.extend([f"- {w}" for w in warnings] or ["- None"])
    lines.extend([
        "",
        "## One next action",
        f"- {next_action}",
        "",
        "## Guardrails",
        "- This is not the retired morning briefing.",
        "- No Signal/Dialtone/kael55 routing.",
        "- No email by default.",
        "- No X sends, Sheet writes, Stripe changes, page/PDF edits, or Discord channel creation.",
    ])
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    gate_count = len(tlcc.get("approval_gates") or []) if tlcc.get("active") else 0
    worst = blockers[0] if blockers else (warnings[0] if warnings else "all checks healthy")
    more = max(0, len(blockers) + len(warnings) - 1)
    headline = compact(worst, 90) + (f" (+{more} more)" if more else "")
    status_key = {"blocked": "action", "warning": "warn"}.get(status, "ok")
    short = render(
        title(status_key, "Standup", headline),
        kvline(
            ("gates", gate_count),
            ("cron issues", len(cron.get("issues") or [])),
            ("stripe", watchdog_meta.get("severity")),
            ("sheets", google.get("status")),
        ),
        meaning(next_action),
        subtext(f"checked STATE · gates · proposals · cron · Stripe · Sheets · artifact: {out}"),
    )
    meta = {"status": status, "blockers": blockers, "warnings": warnings, "next_action": next_action, "report": str(out)}
    return out, short, meta


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cron", action="store_true", help="print Discord-ready output only when action/warning exists")
    parser.add_argument("--json", action="store_true", help="print machine JSON")
    args = parser.parse_args()
    out, short, meta = build_report(cron_mode=args.cron)
    if args.json:
        print(json.dumps(meta, indent=2, ensure_ascii=False))
    elif args.cron:
        if meta["status"] != "healthy":
            print(short)
    else:
        print(short)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
