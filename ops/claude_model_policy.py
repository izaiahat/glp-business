#!/usr/bin/env python3
"""Fail-closed reader for the central Claude model policy. Every Fable-seat
consumer imports resolve() instead of hard-coding a model. Missing/corrupt
policy => RuntimeError (fail closed) — callers must pause, never guess a model."""
from __future__ import annotations
import json, os
from pathlib import Path

POLICY = Path(os.environ.get("CLAUDE_MODEL_POLICY", "/home/ubuntu/business/ops/claude-model-policy.json"))
POLICY_SCHEMA = "claude_model_policy_v2"
FABLE_MODEL = "claude-fable-5-1"
# Operator ruling 2026-09-04: only the Discord bridge daemon uses Fable 5.1/high.
# Acceptance/resume/default seats remain Opus 5/medium. The interactive operator
# session and the bridge's top-stakes deferral/approval boundaries are unchanged.
FABLE_SEATS = frozenset({"bridge"})
# Operator ruling 2026-07-24: Opus 5 released; all Opus-4.8 seats move to Opus 5.
# Operator ruling 2026-07-29: Opus 5 effort = MEDIUM everywhere (xhigh overthinks/errors).
OPUS_MODEL = "claude-opus-5-5[1m]"
OPUS_EFFORT = "medium"


def _lane(value: object, label: str) -> tuple[str, str]:
    if not isinstance(value, dict):
        raise RuntimeError(f"model policy {label} invalid — FAIL CLOSED")
    model = str(value.get("model") or "")
    effort = str(value.get("effort") or "")
    if not model or not effort:
        raise RuntimeError(f"model policy {label} missing model/effort — FAIL CLOSED")
    return model, effort

def load() -> dict:
    try:
        d = json.loads(POLICY.read_text())
    except Exception as e:
        raise RuntimeError(f"model policy unreadable ({POLICY}): {e} — FAIL CLOSED, do not guess a model")
    if not isinstance(d, dict) or d.get("schema") != POLICY_SCHEMA:
        raise RuntimeError("model policy schema invalid — FAIL CLOSED")
    active_model, active_effort = _lane(d.get("active"), "active")
    rules = d.get("rules") or {}
    if not isinstance(rules, dict) or rules.get("no_paid_fable_fallback") is not True:
        raise RuntimeError("model policy missing no_paid_fable_fallback guard — FAIL CLOSED")
    if rules.get("included_fable_access") is not True:
        raise RuntimeError("included Fable access is not asserted — FAIL CLOSED, do not use paid fallback")
    if set(rules.get("fable_seats") or []) != FABLE_SEATS:
        raise RuntimeError("model policy sanctioned Fable seats changed — FAIL CLOSED")
    if rules.get("fable_model") != FABLE_MODEL:
        raise RuntimeError("model policy Fable model changed — FAIL CLOSED")
    if (active_model, active_effort) != (OPUS_MODEL, OPUS_EFFORT):
        raise RuntimeError("non-Fable default must remain Opus 5.5 medium — FAIL CLOSED")
    overrides = d.get("seat_overrides") or {}
    if not isinstance(overrides, dict):
        raise RuntimeError("model policy seat_overrides invalid — FAIL CLOSED")
    for seat in FABLE_SEATS:
        if _lane(overrides.get(seat), f"seat_overrides.{seat}") != (FABLE_MODEL, "xhigh"):
            raise RuntimeError(f"model policy {seat} must remain Fable xhigh — FAIL CLOSED")
    for seat, value in overrides.items():
        model, _effort = _lane(value, f"seat_overrides.{seat}")
        if model == FABLE_MODEL and seat not in FABLE_SEATS:
            raise RuntimeError("Fable assigned to an unsanctioned seat — FAIL CLOSED")
    return d

def resolve(seat: str | None = None) -> tuple[str, str]:
    """Return (model, effort) for the given seat's override, else the active seat.
    Fail closed on any problem. Unknown/absent seat falls back to active."""
    d = load()
    if seat in FABLE_SEATS:
        return _lane(d["seat_overrides"][seat], f"seat_overrides.{seat}")
    return _lane(d["active"], "active")

if __name__ == "__main__":
    m, e = resolve()
    print(f"{m} {e}")
