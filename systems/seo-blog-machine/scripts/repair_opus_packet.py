#!/usr/bin/env python3
"""Hermes-managed bounded repair worker for one GLP SEO finding set."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
from pathlib import Path

MODEL = "gpt-5.6-sol"  # post-reset switchover 2026-08-07: Sol xhigh (config reasoning_overrides clamps effort)
PROVIDER = "openai-codex"
# Packet repairs routinely include fresh Firecrawl capture, line-level claim
# binding, draft edits, and a final reread. Same-day receipts on 2026-08-21 show
# useful completed edits being killed at the old 600-second ceiling. Give the
# authorized heavy Sol lane its full bounded turn budget instead of starving the
# article buffer; the parent controller has a separate 30-second reap margin.
REPAIR_TIMEOUT_SECONDS = 2400
REPAIR_MAX_TURNS = 60


def worker_command(prompt_text: str) -> list[str]:
    """Return the single authorized Hermes/OpenAI Codex Sol worker route."""
    return ["hermes", "chat", "--provider", PROVIDER, "--model", MODEL,
            "--reasoning", "xhigh", "--max-turns", str(REPAIR_MAX_TURNS),
            "--source", "tool", "-Q", "-q", prompt_text]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_worker(command: list[str], cwd: Path, timeout_seconds: int) -> tuple[subprocess.CompletedProcess[str], bool]:
    """Run one worker in its own process group and reap the full group on timeout.

    ``subprocess.run(..., timeout=...)`` kills only its direct child. Hermes chat
    starts MCP watchdogs and tool processes, so the old implementation leaked a
    live repair agent after the controller had already declared a timeout. That
    orphan could continue mutating the packet after review authority was gone.
    """
    process = subprocess.Popen(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
    return subprocess.CompletedProcess(
        command,
        124 if timed_out else process.returncode,
        stdout,
        stderr,
    ), timed_out


def main() -> int:
    plan_path = Path(os.environ.get("SEO_REPAIR_PLAN", "")).resolve()
    packet = Path(os.environ.get("SEO_PACKET_DIR", "")).resolve()
    if not plan_path.is_file() or not packet.is_dir():
        raise RuntimeError("repair_environment_invalid")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    expected = plan.get("plan_sha256")
    unsigned = dict(plan)
    unsigned.pop("plan_sha256", None)
    actual = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    if expected != actual or plan.get("schema") != "glp-opus-seo-repair-plan-v1":
        raise RuntimeError("repair_plan_hash_invalid")
    allowed = sorted(set(plan.get("allowed_subject_files") or []))
    if not allowed or any(Path(value).is_absolute() or ".." in Path(value).parts for value in allowed):
        raise RuntimeError("repair_allowed_files_invalid")
    validator_argv = plan.get("validator_argv")
    if (not isinstance(validator_argv, list) or not validator_argv
            or any(not isinstance(value, str) or not value for value in validator_argv)):
        raise RuntimeError("repair_validator_argv_invalid")
    for rel in allowed:
        if not (packet / rel).is_file():
            raise RuntimeError(f"repair_subject_missing:{rel}")
    prompt_path = plan_path.with_name(plan_path.stem + "-hermes-prompt.md")
    # Captures live beside the packet, not inside it; findings that require
    # persisting new evidence have no lawful destination without this path.
    research_dir = packet.parent / "research"
    prompt = f"""You are a bounded GLP SEO packet repair worker.

Working directory: {packet}
Repair plan: {plan_path}
Repair-plan SHA-256: {expected}
The wrapper has already verified this hash using the canonical JSON algorithm. Do not recompute or challenge it; execute the sealed findings.

Read the repair plan and edit ONLY these packet-relative files:
{chr(10).join('- ' + item for item in allowed)}

Implement exactly every `required_fix`; no extra rewrites, optimization, stylistic cleanup, or new claims.
The plan-listed files above are EXPLICITLY GRANTED for this round and always win over the general
prohibition that follows — if the plan grants `00-authority.json`, editing it is required, not forbidden.
Apart from those granted files: do not hand-edit derived files, manifests, receipts, scripts, or external systems.
Use Firecrawl first when a required finding needs missing live web evidence; record the exact URL, checked time, result, quote, or explicit not-obtainable reason. Never use competitor pages for medical or price claims.
When a finding requires PERSISTING a new source capture, write it as a NEW file under `{research_dir}`
using the existing `S<n>-<slug>-firecrawl.json` convention, then bind its path and SHA-256 exactly as the
finding instructs. Creating new capture files there is permitted; modifying or deleting an existing
capture is never permitted.
Do not send messages. Do not publish, deploy, purge caches, submit GSC, or mutate any external provider.
Preserve evidence binding: never invent a quote, citation, price, medical claim, reviewer, URL, or source.
If a finding requires evidence that is absent AND cannot be captured live, make no unsupported change and exit with a clear BLOCKED result.
After edits, run this exact hash-pinned deterministic validator command from the packet directory:
{shlex.join(validator_argv)}
The validator may regenerate only the plan-declared derived files. If it reports deterministic failures caused by
your repair, keep making the smallest honest changes within the granted source files and rerun that exact command
until it prints PASS. Never edit validators or gate/config files. If a failure cannot be fixed honestly inside the
granted files, stop and return BLOCKED instead of weakening a gate.
After validation, reread changed files and return a terse list of exact changes. The parent will independently hash-scope and revalidate.
"""
    prompt_path.write_text(prompt, encoding="utf-8")
    os.chmod(prompt_path, 0o600)
    command = worker_command(prompt_path.read_text())
    timeout_seconds = int(os.environ.get("SEO_REPAIR_TIMEOUT_SECONDS", REPAIR_TIMEOUT_SECONDS))
    if timeout_seconds < 1 or timeout_seconds > REPAIR_TIMEOUT_SECONDS:
        raise RuntimeError("repair_timeout_seconds_invalid")
    proc, timed_out = run_worker(command, packet, timeout_seconds)
    # hermes chat can die in post-turn teardown (exit 250) AFTER the worker has
    # finished its edits and written the change report — 2026-08-14 this
    # discarded a completed medical-accuracy repair. The parent loop hash-scopes
    # every changed byte and revalidates the packet, so a completed non-BLOCKED
    # change report is the real success signal for that code; the raw exit code
    # is still recorded.
    effective_rc = proc.returncode
    if (proc.returncode in {250} and proc.stdout.strip()
            and "BLOCKED" not in proc.stdout[-2000:]):
        effective_rc = 0
    receipt_path = plan_path.with_name(plan_path.stem + "-hermes-repair.json")
    receipt = {
        "schema": "glp-opus-seo-hermes-repair-execution-v1",
        "status": "PASS" if effective_rc == 0 else "BLOCKED",
        "effective_exit_code": effective_rc,
        "model": MODEL,
        "provider": PROVIDER,
        "plan_path": str(plan_path),
        "plan_sha256": sha(plan_path),
        "prompt_path": str(prompt_path),
        "prompt_sha256": sha(prompt_path),
        "allowed_files": allowed,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(receipt_path, 0o600)
    if proc.stdout:
        print(proc.stdout.rstrip())
    if proc.stderr:
        print(proc.stderr.rstrip(), file=sys.stderr)
    return effective_rc


if __name__ == "__main__":
    raise SystemExit(main())
