#!/usr/bin/env python3
"""COGNY QUEUE DRAIN — the fleet executes Cogny's own scheduled skills, no hands.

WHY THIS EXISTS
---------------
Cogny schedules recurring analysis tasks for this workspace and each one carries
its own `skill_content` — a full audit playbook. Two of them (`bing-webmaster-audit`,
`seo-audit`) sat `in_progress` from 2026-08-18 to 2026-08-20 because nothing on
this box was watching the queue. A queue only a human drains is not a machine.

So the weekly telemetry lane, right after it posts its digest, asks Cogny whether
anything is pending and dispatches it on the bridge `hermes_chat` Sol xhigh
authoring contract — the same shape the onboarding machine uses for its authoring
engine: one job, a hard budget, async with an n8n resume callback, and a completion
step that writes the result back through `complete_task`.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
* It never SPENDS. `SPEND_SKILLS` are refused at dispatch time, before a brief is
  even built, and the brief itself carries a read-only mandate. Cogny's own skill
  contract already requires human approval before any mutation; this adds a second
  lock on our side so a queue row cannot become an ad buy.
* It never writes the governed Sheet. Cogny is not a Sheet writer and this lane
  does not make it one.
* It never invents findings. `complete_task` is called with what the agent
  actually returned, deduped against the open backlog by title so a weekly drain
  cannot refile the same ticket forever.

FAILURE POSTURE
---------------
An empty queue is a SUCCESS (`dispatched: false, reason: queue_empty`), not a
silent skip and not an error — the digest says so. A bridge that is down degrades
to an honest blocked receipt rather than taking the telemetry lane down with it.
A dispatched job that never returns is caught by the deadline in the state file
and reported on the next run instead of leaving a task claimed forever, which is
exactly the failure this lane was built to end.

MODES
-----
  --mode dispatch --callback <n8n $execution.resumeUrl>
      poll the queue; if a task is pending, claim it and fire ONE bridge hermes_chat
      job. Prints one compact JSON receipt on the LAST stdout line.
  --mode resume --job-id <id> [--task-id <id>]
      read the finished job's agent receipt, dedupe findings against the open
      backlog, and call complete_task.
  --mode status
      read-only queue + findings census. No claims, no dispatch, no writes.

TEST SAFETY
-----------
`GLP_COGNY_DRAIN_SINK=<path>` writes the dispatch to a file and NEVER calls the
bridge, so a test or a rehearsal cannot spawn a 45-minute agent by construction
rather than by somebody remembering to stub the right function. Same discipline
as `glp_onboarding_authoring_dispatch`.
"""
from __future__ import annotations

import argparse
import fcntl
import functools
import tempfile
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BUSINESS = Path(os.environ.get("GLP_COGNY_DRAIN_REPO", "/home/ubuntu/business"))
HOME = Path(os.path.expanduser("~"))
PT = ZoneInfo("America/Los_Angeles")

SCHEMA = "glp.cogny_queue_drain.v1"
MACHINE = "cogny_queue_drain"

ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/cogny-audits"
STATE_PATH = Path(os.environ.get("GLP_COGNY_DRAIN_STATE", str(ROOT / "queue-drain-state.json")))

BRIDGE_BASE = os.environ.get("GLP_COGNY_DRAIN_BRIDGE_BASE", "https://bridge.allocant.ai")
BRIDGE_TOKEN_FILE = HOME / ".hermes/secrets/n8n-bridge/token"
#: Cloudflare fronts bridge.allocant.ai and answers 403 ("error code: 1010") to the
#: default Python-urllib User-Agent. Proven by the tier-2 kill test 2026-08-19.
#: Without this header every dispatch dies at the POST and the lane degrades
#: silently back to "nobody drains the queue". Never remove it.
BRIDGE_UA = os.environ.get("GLP_COGNY_DRAIN_HTTP_UA",
                           "tilton-cogny-queue-drain/1.0 (+https://bridge.allocant.ai)")

#: 45 minutes — the operator's number, the same budget the onboarding authoring
#: lane and the tier-2 fixer ladder run on.
BUDGET_S = int(os.environ.get("GLP_COGNY_DRAIN_BUDGET_S", "2700"))
#: One dispatch per lane run. A weekly lane that fans out N agents is a weekly
#: lane that can spend N budgets in one night.
MAX_DISPATCH_PER_RUN = 1
# Operator routing order, 2026-08-20 ~2:30pm PT: recurring research and
# authoring run on Sol. Opus is reserved for orchestration and independent
# review, never this worker seat. Historical `claude_p` records remain valid.
MODEL = "gpt-6.1-sol"
REASONING = "xhigh"
TOOLSET_PROFILE = "authoring"
# The bridge code on disk permits 60 turns, but the RUNNING process still
# rejects >20. Raise this pin only in the separately sequenced bridge-restart
# step; today's worker must never send a payload the running bridge rejects.
BRIDGE_MAX_TURNS = int(os.environ.get("GLP_BRIDGE_MAX_TURNS", "60"))

#: Skills whose playbooks mutate a paid account. Refused at dispatch, before a
#: brief exists. Adding a skill here is the only supported way to keep a Cogny
#: task type out of the automated lane.
SPEND_SKILLS = {
    "google-ads-audit", "search-campaign-audit", "pmax-audit", "pmax-launch-kit",
    "demand-gen-audit", "shopping-campaign-audit", "google-ads-monitor",
    "linkedin-ads-audit", "linkedin-micro-campaigns", "linkedin-launch-video",
    "tiktok-launch-video", "reddit-launch-video",
}
#: Task types that are mutations by definition in Cogny's own skill contract.
SPEND_TASK_TYPES = {"action", "strategize"}

ENABLED = os.environ.get("GLP_COGNY_DRAIN_ENABLED", "1").lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def utc_now(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pt_day(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(PT).strftime("%Y-%m-%d")


def load_state(path: str | Path | None = None) -> dict:
    p = Path(path or STATE_PATH)
    if not p.is_file():
        return {"schema": SCHEMA, "dispatches": []}
    doc = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get('dispatches'), list):
        raise ValueError('cogny_state_invalid_no_reset')
    return doc


def save_state(state: dict, path: str | Path | None = None) -> None:
    p = Path(path or STATE_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    state["schema"] = SCHEMA
    # Keep every uncertain identity; pruning can reopen a once-only mutation.
    fd, temp = tempfile.mkstemp(prefix='.cogny-', dir=p.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(state, out, sort_keys=True)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, p)
        directory = os.open(p.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def custody(fn):
    """One owner across native calls, bridge continuations and host callbacks."""
    import inspect
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def locked(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        path = Path(bound.arguments.get('state_path') or STATE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix('.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            return fn(*args, **kwargs)
    return locked


def sink() -> str:
    return os.environ.get("GLP_COGNY_DRAIN_SINK", "").strip()


# ---------------------------------------------------------------------------
# bridge I/O
# ---------------------------------------------------------------------------

def _bridge_headers() -> dict:
    token = BRIDGE_TOKEN_FILE.read_text().strip()
    return {"Content-Type": "application/json",
            "Authorization": "Bearer " + token,
            "User-Agent": BRIDGE_UA}


def _hermes_model(model: str = "") -> str:
    selected = str(model or MODEL).strip() or MODEL
    if any(marker in selected.casefold()
           for marker in ("anthropic", "claude", "opus", "sonnet", "haiku")):
        raise ValueError("Cogny authoring requires a Hermes model, not a Claude model")
    return selected


def bridge_hermes_chat(prompt: str, timeout_s: int, callback_url: str = "",
                       model: str = "gpt-6.1-sol", http_timeout: int = 45) -> dict:
    """Fire ONE bridge Hermes-managed Sol xhigh job and DO NOT WAIT.

    200 with the finished record if it beats the bridge's sync window, else 202
    {job_id, status: running}. Either way only the job id is kept: a 45-minute
    agent must never be held inside a weekly lane whose n8n Wait is finite and
    whose bridge slot is 1 of only 3.
    """
    body: dict[str, Any] = {
        "action": "hermes_chat",
        "prompt": prompt,
        "model": _hermes_model(model),
        "reasoning": REASONING,
        "max_turns": max(1, min(BRIDGE_MAX_TURNS, 20)),
        "toolset_profile": TOOLSET_PROFILE,
        "timeout_s": int(timeout_s),
    }
    if callback_url:
        body["callback_url"] = callback_url
    req = urllib.request.Request(BRIDGE_BASE + "/run", data=json.dumps(body).encode("utf-8"),
                                 method="POST", headers=_bridge_headers())
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return {"error": "http %s" % exc.code, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}


def bridge_job_status(job_id: str, http_timeout: int = 30) -> dict:
    req = urllib.request.Request(BRIDGE_BASE + "/job/" + str(job_id), headers=_bridge_headers())
    try:
        with urllib.request.urlopen(req, timeout=http_timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)[:300]}


def _sink_write(prompt: str, timeout_s: int, callback_url: str, task_id: str,
                model: str = MODEL) -> dict:
    path = Path(sink())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"written_utc": utc_now(), "task_id": task_id,
                             "engine": "hermes_chat", "model": _hermes_model(model),
                             "reasoning": REASONING,
                             "max_turns": max(1, min(BRIDGE_MAX_TURNS, 20)),
                             "toolset_profile": TOOLSET_PROFILE,
                             "timeout_s": timeout_s,
                             "callback_url": callback_url, "prompt_chars": len(prompt),
                             "prompt": prompt}, ensure_ascii=False) + "\n")
    return {"job_id": "sink-%s" % task_id[:8], "status": "running",
            "sink": str(path), "posted_to_bridge": False}


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

def may_dispatch(task: dict, state: dict, now: dt.datetime | None = None) -> dict:
    """Decide whether ONE task may be dispatched. Never raises, always explains."""
    if not ENABLED:
        return {"ok": False, "why": "GLP_COGNY_DRAIN_ENABLED is off"}
    task_id = str(task.get("id") or "")
    skill = str(task.get("skill_id") or "")
    ttype = str(task.get("task_type") or "")
    if not task_id:
        return {"ok": False, "why": "task carries no id"}
    if skill in SPEND_SKILLS:
        return {"ok": False, "why": f"skill '{skill}' is on the spend denylist — operator decision, never automated",
                "refused_class": "spend"}
    if ttype in SPEND_TASK_TYPES:
        return {"ok": False, "why": f"task_type '{ttype}' mutates by definition in Cogny's own contract — operator decision",
                "refused_class": "spend"}
    day = pt_day(now)
    for rec in state.get("dispatches", []):
        if rec.get("task_id") == task_id and rec.get("pt_day") == day:
            return {"ok": False, "why": f"task {task_id} already dispatched today ({day})",
                    "refused_class": "already_dispatched"}
    return {"ok": True, "why": "", "skill_id": skill, "task_type": ttype}


# ---------------------------------------------------------------------------
# brief
# ---------------------------------------------------------------------------

def receipt_path_for(task_id: str, stamp: str) -> str:
    return str(ROOT / stamp[:8] / f"agent-receipt-{task_id[:8]}.json")


def build_brief(task: dict, receipt_file: str, report_dir: str) -> str:
    """The agent brief. Carries the skill playbook VERBATIM plus our guardrails."""
    skill_content = str(task.get("skill_content") or "").strip()
    context = task.get("context")
    ctx_block = ("\n## Workspace context supplied by Cogny\n\n```json\n"
                 + json.dumps(context, indent=1)[:6000] + "\n```\n") if context else ""
    playbook = (f"\n## The skill playbook (VERBATIM from Cogny — follow it step by step)\n\n"
                f"{skill_content[:40000]}\n") if skill_content else (
        "\n## The skill playbook\n\nCogny did not return `skill_content` for this task. Reconstruct the "
        "methodology from the task description and the matching skill under `.claude/skills/`, and SAY SO "
        "in your summary — never present a reconstruction as a verbatim skill run.\n")

    return f"""You are executing ONE scheduled Cogny analysis task for GLP Clinic Scout, dispatched
automatically by the `glp/three-engine-telemetry` machine. No human is watching this run.

## Task
- id: {task.get('id')}
- skill: {task.get('skill_id')}
- type: {task.get('task_type')}
- description: {task.get('description')}
{ctx_block}{playbook}
## Rails you have (read `reports/ops/TOOL-REGISTRY.md` first)
- Cogny MCP via `ops/glp/cogny_client.py` — use its `TOOL` name map. Integration tools are
  namespaced `mcp__<Integration>__tool_<name>`; the bare name returns JSON-RPC -32601 "Unknown
  tool", which reads exactly like "not connected". Do not misdiagnose that.
- Direct rails via `ops/glp/seo_data_rails.py`: DataForSEO (SERP + volumes), BingWebmaster,
  GoogleRail (GSC domain property + GA4), PostHog (HogQL).
- Known live facts, so you do not rediscover them: GA4 property 541440529 is ~98% localhost
  traffic — ALWAYS filter `hostName CONTAINS glpclinicscout.com`. PostHog has no GLP events.
  Cogny's GSC identity sees only the URL-prefix property, never `sc-domain:`. Cogny's Google Ads
  identity cannot reach AW-18245668620.

## HARD GUARDRAILS — these are not suggestions
1. READ-ONLY. Do not create, pause, update or delete any campaign, budget, bid, keyword, audience
   or conversion action. Do not spend a cent. If the playbook asks for a mutation, record it as a
   FINDING describing the change and stop there.
2. Do not write the governed Google Sheet. There is exactly one Sheet writer and it is not you.
3. Do not deploy, do not touch production, do not modify any n8n workflow.
4. Never print a key, token, password or EIN. Reference secret paths, never values.
5. Never weaken a gate, a floor or a validator.
6. Evidence or silence. Every claim gets a number and the call that produced it. If a rail is
   blocked, report BLOCKED with the exact error — an honest gap beats a confident guess.
7. Cross-check against our own weekly telemetry detectors in
   `scripts/glp_three_engine_telemetry.py`. Where the two systems disagree, EXPLAIN the
   disagreement rather than picking a winner.

## Deliverables
1. A human-readable report under `{report_dir}/`.
2. A machine receipt at EXACTLY this path, valid JSON, nothing else in the file:

   {receipt_file}

   ```json
   {{
     "task_id": "{task.get('id')}",
     "ok": true,
     "result_summary": "<what you found, with the numbers — this becomes the Cogny task summary>",
     "report_path": "<path to the report you wrote>",
     "findings": [
       {{
         "title": "<short, actionable, specific>",
         "body": "<the evidence: exact figures, the call that produced them, the recommended action>",
         "action_type": "<seo_fix|content|technical_seo|instrumentation|integration|tooling|...>",
         "expected_outcome": "<what improves if this is acted on>",
         "estimated_impact_usd": 0,
         "execution_target": "manual|agent"
       }}
     ],
     "blocked": [{{"what": "...", "error": "..."}}]
   }}
   ```

Do NOT call `complete_task` yourself. The machine reads this receipt, dedupes your findings
against the open backlog, and completes the task. Writing the receipt IS finishing the job.
"""


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------

def mode_status(client: Any) -> dict:
    queue = client.call("get_queue_status", {}, allow_error=True)
    findings = client.call("list_findings", {"limit": 100}, allow_error=True)
    rows = findings.get("findings") if isinstance(findings, dict) else findings
    stats = (queue or {}).get("stats") or {}
    return {"schema": SCHEMA, "mode": "status", "ok": True,
            "pending": int(stats.get("pending") or 0),
            "in_progress": int(stats.get("in_progress") or 0),
            "completed": int(stats.get("completed") or 0),
            "open_findings": len(rows or []),
            "output_count": int(stats.get("pending") or 0)}


@custody
def mode_dispatch(client: Any, callback_url: str, budget_s: int, model: str,
                  poster: Callable | None = None, now: dt.datetime | None = None,
                  state_path: str | Path | None = None) -> dict:
    state = load_state(state_path)
    if state.get('claim_uncertain') or any(r.get('status') not in {'completed', 'completed_no_receipt'}
                                         for r in state['dispatches']):
        return {'schema': SCHEMA, 'mode': 'dispatch', 'ok': False, 'dispatched': False,
                'reason': 'prior_custody_unresolved', 'output_count': 0}
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y%m%dT%H%M%SZ")

    client.call("heartbeat", {"agent_harness": "n8n-bridge", "harness_version": "1.0",
                              "skill_version": "1.0.0", "model_name": model or MODEL},
                allow_error=True)

    queue = client.call("get_queue_status", {}, allow_error=True)
    if isinstance(queue, dict) and queue.get("cogny_error"):
        return {"schema": SCHEMA, "mode": "dispatch", "ok": False, "dispatched": False,
                "reason": "queue_read_failed", "error": str(queue.get("cogny_error"))[:300],
                "output_count": 0}
    stats = (queue or {}).get("stats") or {}
    if type(stats.get('pending')) is not int or stats['pending'] < 0:
        raise ValueError('queue_denominator_unknown')
    pending = stats['pending']
    if pending <= 0:
        # An empty queue is a SUCCESS. The never-zero doctrine applies to
        # telemetry ROWS, not to a work queue that is legitimately drained.
        return {"schema": SCHEMA, "mode": "dispatch", "ok": True, "dispatched": False,
                "reason": "queue_empty", "pending": 0,
                "in_progress": int(stats.get("in_progress") or 0),
                "completed": int(stats.get("completed") or 0), "output_count": 0}

    if not isinstance(stats.get('pending'), int) or stats.get('pending') < 0:
        raise ValueError('queue_denominator_unknown')
    state['claim_uncertain'] = {'at': utc_now(), 'pending_before': pending}
    save_state(state, state_path)
    task = client.call("get_next_task", {}, allow_error=True)
    if not isinstance(task, dict) or not task.get("hasTask"):
        return {"schema": SCHEMA, "mode": "dispatch", "ok": False, "dispatched": False,
                "reason": "get_next_task_returned_none", "pending": pending, "output_count": 0}
    row = task.get("task") if isinstance(task.get("task"), dict) else task
    task_id = str(row.get("id") or "")
    if not task_id:
        raise ValueError('claimed_task_identity_missing')
    claim = {'task_id': task_id, 'status': 'claimed', 'claimed_at': utc_now()}
    state['dispatches'].append(claim)
    state.pop('claim_uncertain')
    save_state(state, state_path)

    verdict = may_dispatch(row, {'dispatches': state['dispatches'][:-1]}, now)
    if not verdict.get("ok"):
        rec = {"task_id": task_id, "pt_day": pt_day(now), "dispatched_utc": utc_now(now),
               "status": "refused", "why": verdict.get("why"),
               "refused_class": verdict.get("refused_class", "gate")}
        claim.update(rec)
        save_state(state, state_path)
        return {"schema": SCHEMA, "mode": "dispatch", "ok": True, "dispatched": False,
                "reason": "refused", "why": verdict.get("why"), "task_id": task_id,
                "skill_id": row.get("skill_id"), "output_count": 0}

    report_dir = f"reports/glp-clinic-scout/seo-machine/cogny-audits/{stamp[:8]}"
    receipt_file = receipt_path_for(task_id, stamp)
    Path(receipt_file).parent.mkdir(parents=True, exist_ok=True)
    brief = build_brief(row, receipt_file, report_dir)

    if poster is None:
        poster = ((lambda p, t, cb, m: _sink_write(p, t, cb, task_id, m))
                  if sink() else bridge_hermes_chat)
    claim.update({'status': 'dispatch_uncertain', 'agent_receipt': receipt_file,
                  'report_dir': report_dir, 'pt_day': pt_day(now)})
    save_state(state, state_path)
    try:
        res = poster(brief, budget_s, callback_url, model) or {}
    except Exception as exc:  # noqa: BLE001 — a dead bridge is a blocked receipt, not a dead lane
        res = {"error": str(exc)[:300]}

    job_id = res.get("job_id")
    rec = {
        "task_id": task_id, "skill_id": row.get("skill_id"), "task_type": row.get("task_type"),
        "pt_day": pt_day(now), "dispatched_utc": utc_now(now),
        "deadline_utc": utc_now((now or dt.datetime.now(dt.timezone.utc))
                                + dt.timedelta(seconds=budget_s)),
        "budget_s": budget_s, "job_id": job_id, "engine": "hermes_chat",
        "model": model or MODEL, "reasoning": REASONING,
        "bridge_status": res.get("status") or ("error" if res.get("error") else ""),
        "callback_url_given": bool(callback_url),
        "agent_receipt": receipt_file, "report_dir": report_dir,
        "brief_chars": len(brief),
        "status": "in_flight" if job_id else "dispatch_failed",
        "error": res.get("error"),
    }
    claim.update(rec)
    save_state(state, state_path)

    return {"schema": SCHEMA, "mode": "dispatch", "ok": bool(job_id),
            "dispatched": bool(job_id), "reason": "dispatched" if job_id else "bridge_error",
            "task_id": task_id, "skill_id": row.get("skill_id"), "job_id": job_id,
            "agent_receipt": receipt_file, "budget_s": budget_s,
            "brief_chars": len(brief), "error": res.get("error"),
            "pending_before": pending, "output_count": 1 if job_id else 0}


def read_agent_receipt(path: str | Path) -> dict:
    """Read the agent's receipt.

    ``_unreadable`` distinguishes "the agent never wrote a receipt" from "the
    agent wrote a receipt saying it failed". The second still carries a real
    summary and real findings and must not be thrown away, which is why the
    marker is a private key rather than a reuse of ``ok``.
    """
    p = Path(path)
    if not p.is_file():
        return {"_unreadable": "agent_receipt_missing: %s" % p}
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"_unreadable": "agent_receipt_unparseable: %s" % str(exc)[:200]}
    if not isinstance(doc, dict):
        return {"_unreadable": "agent_receipt_not_an_object"}
    return doc


def dedupe_findings(client: Any, findings: list) -> tuple[list, list]:
    """Never refile a ticket that is already open. Title match, same rule the
    telemetry lane uses, so the two writers cannot fight each other."""
    open_titles: set = set()
    try:
        cur = client.call("list_findings", {"limit": 100})
        if not isinstance(cur, dict) or not isinstance(cur.get('findings'), list):
            raise ValueError('findings_shape_unknown')
        rows = cur.get("findings") if isinstance(cur, dict) else cur
        for r in rows or []:
            if isinstance(r, dict) and str(r.get("status") or "open") not in ("done", "dismissed"):
                open_titles.add(str(r.get("title") or "").strip())
    except Exception as exc:
        raise RuntimeError('cogny_findings_read_failed') from exc
    fresh, skipped = [], []
    for f in findings or []:
        if not isinstance(f, dict) or not f.get("title") or not f.get("body"):
            continue
        title = str(f["title"]).strip()[:200]
        if title in open_titles:
            skipped.append(title)
            continue
        item = {"title": title, "body": str(f["body"])}
        for k in ("action_type", "expected_outcome", "execution_target"):
            if f.get(k):
                item[k] = str(f[k])
        if isinstance(f.get("estimated_impact_usd"), (int, float)):
            item["estimated_impact_usd"] = float(f["estimated_impact_usd"])
        fresh.append(item)
        open_titles.add(title)
    return fresh, skipped


@custody
def mode_resume(client: Any, job_id: str = "", task_id: str = "",
                job_status: Callable | None = None,
                state_path: str | Path | None = None) -> dict:
    state = load_state(state_path)
    matches = [r for r in state['dispatches'] if
               (job_id or task_id) and (not job_id or r.get('job_id') == job_id)
               and (not task_id or r.get('task_id') == task_id)]
    out = {'schema': SCHEMA, 'mode': 'resume', 'ok': False, 'output_count': 0,
           'task_id': task_id, 'job_id': job_id}
    if len(matches) != 1:
        return {**out, 'reason': 'exact_dispatch_not_found'}
    rec = matches[0]
    task_id, job_id = rec['task_id'], rec.get('job_id')
    out.update(task_id=task_id, job_id=job_id)
    if rec.get('status') == 'completed' and rec.get('completion_ack'):
        return {**out, 'ok': True, 'reason': 'already_acknowledged',
                'findings_created': rec.get('findings_created', 0),
                'report_path': rec.get('report_path')}
    if rec.get('status') in {'completion_uncertain', 'completion_awaiting_readback'}:
        queue = client.call('get_queue_status', {})
        remote = [r for r in queue.get('tasks', []) if r.get('id') == task_id]
        if len(remote) == 1 and remote[0].get('status') == 'completed':
            rec.update(status='completed', completion_ack={'readback': remote[0]}, resolved_utc=utc_now())
            save_state(state, state_path)
            return {**out, 'ok': True, 'reason': 'completion_reconciled',
                    'findings_created': rec.get('findings_created', 0),
                    'report_path': rec.get('report_path')}
        return {**out, 'reason': 'completion_ack_unknown_no_replay'}
    if not job_id or rec.get('status') != 'in_flight':
        return {**out, 'reason': 'dispatch_unresolved'}
    job = (job_status or bridge_job_status)(job_id)
    if job.get('job_id') != job_id or job.get('status') not in {'done', 'error'}:
        return {**out, 'reason': 'job_not_terminal'}
    receipt = read_agent_receipt(rec.get('agent_receipt') or '')
    if (job.get('rc') != 0 or receipt.get('_unreadable') or receipt.get('task_id') != task_id
            or receipt.get('ok') is not True or not receipt.get('result_summary')
            or not receipt.get('report_path')):
        return {**out, 'reason': 'analysis_not_successful', 'rc': job.get('rc')}
    fresh, skipped = dedupe_findings(client, receipt.get('findings') or [])
    summary = str(receipt['result_summary']) + '\n\nFull report: ' + str(receipt['report_path'])
    rec.update(status='completion_uncertain', report_path=receipt['report_path'],
               findings_created=len(fresh), findings_skipped_open=len(skipped))
    save_state(state, state_path)
    res = client.call('complete_task', {'taskId': task_id, 'resultSummary': summary[:20000],
                                      'findings': fresh}, allow_error=True)
    if not isinstance(res, dict) or res.get('success') is not True:
        return {**out, 'reason': 'completion_ack_unknown_no_replay'}
    rec.update(status='completion_awaiting_readback', completion_response=res)
    save_state(state, state_path)
    queue = client.call('get_queue_status', {})
    remote = [r for r in queue.get('tasks', []) if r.get('id') == task_id]
    if len(remote) != 1 or remote[0].get('status') != 'completed':
        return {**out, 'reason': 'completion_readback_missing'}
    rec.update(status='completed', completion_ack={'response': res, 'readback': remote[0]},
               resolved_utc=utc_now())
    save_state(state, state_path)
    return {**out, 'ok': True, 'reason': 'completed', 'output_count': max(1, len(fresh)),
            'findings_created': len(fresh), 'findings_skipped_open': len(skipped),
            'report_path': receipt['report_path']}


def mode_tick(client):
    state = load_state()
    pending = [r for r in state['dispatches'] if r.get('status') != 'completed']
    if pending:
        row = pending[0]
        return mode_resume(client, row.get('job_id', ''), row.get('task_id', ''))
    return mode_dispatch(client, '', BUDGET_S, MODEL)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Drain Cogny's scheduled-task queue, no hands.")
    ap.add_argument("--mode", choices=("dispatch", "resume", "status", "tick"), default="status")
    ap.add_argument("--callback", default="", help="n8n $execution.resumeUrl for the hermes_chat job")
    ap.add_argument("--budget-s", type=int, default=BUDGET_S)
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--job-id", default="")
    ap.add_argument("--task-id", default="")
    ap.add_argument("--output", default="", help="also write the receipt to this path")
    ns = ap.parse_args(argv)

    try:
        from ops.glp.cogny_client import connect
        # One transport attempt: claims/completions cannot be blindly retried.
        client = connect(retries=1)
    except Exception as exc:  # noqa: BLE001
        out = {"schema": SCHEMA, "mode": ns.mode, "ok": False,
               "reason": "cogny_unreachable", "error": str(exc)[:300], "output_count": 0}
        print(json.dumps(out))
        return 1

    if ns.mode == "status":
        out = mode_status(client)
    elif ns.mode == 'tick':
        out = mode_tick(client)
    elif ns.mode == "dispatch":
        out = mode_dispatch(client, ns.callback, max(1, min(ns.budget_s, 3600)), ns.model)
    else:
        out = mode_resume(client, ns.job_id, ns.task_id)

    out["utc"] = utc_now()
    if ns.output:
        Path(ns.output).parent.mkdir(parents=True, exist_ok=True)
        Path(ns.output).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
    # ONE compact JSON receipt on the LAST stdout line — the n8n parser scans
    # upward for the newest line carrying output_count.
    print(json.dumps(out))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
