#!/usr/bin/env python3
"""The authorized transports for the GLP five-gate article review.

Operator authorization 2026-08-18 (verbatim intent): "we can have independent
opus reviewers for things like the seo article reviews." That supersedes the
Sol-only transport pin recorded in OPUS-REVIEW-PATH-20260802.md (execution
succession 2026-08-08) — it does NOT change the rubric, the gate set, the PASS
bar, or any packet contract.

This module is the ONE place the transport identities live. The review
controller, the buffer's admission check, and the migration verifier all read
this table, so the set of runtimes the machine will ACCEPT can never drift from
the set of runtimes the machine can EMIT — the same discipline the release
builder's byte anchors use.

Both transports must satisfy identical mechanical safety properties, and every
one of them is verifiable from the raw receipt alone:

  * empty tool surface — no file, shell, web, or MCP access of any kind
  * exactly one model turn
  * no fallback provider or fallback model
  * fresh, packet-bound context: the reviewer sees only the sealed subject files
    handed to it in-band, never the drafting context, repo, or prior session
  * a verdict that satisfies the hash-pinned review JSON Schema
  * zero effects: no publish, deploy, purge, submit, send, or spend

"Independent" means the reviewing CONTEXT never saw the drafting. For the Opus
transport that is enforced by a fresh `claude -p` process per round with no
settings sources, no MCP servers, no session persistence, no CLAUDE.md
discovery, and an empty tool surface.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

BUSINESS = Path("/home/ubuntu/business")

OPERATOR_AUTHORIZATION = {
    "authority": "operator",
    "date": "2026-08-18",
    "ruling": ("we can have independent opus reviewers for things like the seo article reviews"),
    "supersedes": ("OPUS-REVIEW-PATH-20260802.md execution succession 2026-08-08 "
                   "(Sol-only transport pin)"),
    "scope": "REVIEW TRANSPORT ONLY — rubric, gate set, PASS bar and packet contracts unchanged",
}

SOL_TRANSPORT: dict[str, Any] = {
    "transport_id": "hermes-openai-codex-sol",
    "wrapper": BUSINESS / "scripts/hermes_sol_article_review.py",
    "provider": "openai-codex",
    "model": "gpt-5.6-sol",
    "effort": "xhigh",
    "api_mode": "codex_responses",
    # Models that may appear in modelUsage without producing any part of the
    # verdict. The Hermes transport runs exactly one model.
    "auxiliary_models": (),
    "authorized_since": "2026-08-08",
}

OPUS_TRANSPORT: dict[str, Any] = {
    "transport_id": "claude-code-opus-independent",
    "wrapper": BUSINESS / "scripts/claude_opus_article_review.py",
    "provider": "anthropic-claude-code",
    "model": "claude-opus-5",
    "effort": "default",
    "api_mode": "claude_code_print",
    # The Claude Code harness bills a small auxiliary model for session
    # bookkeeping. It emits no part of the verdict; the reviewer-model output
    # dominance check below is what binds the verdict to the reviewer.
    "auxiliary_models": ("claude-haiku-4-5",),
    "authorized_since": "2026-08-18",
}

POLICY_OPUS_TRANSPORT: dict[str, Any] = {
    **OPUS_TRANSPORT,
    "transport_id": "claude-code-opus-policy-independent",
    "effort": "medium",
    "authority": BUSINESS / "reports/ops/orchestration-restore-20260907/briefs/plan/K6-ARTICLE-CARRIER-TO-LIVE.md",
}


# Retain both Opus 5 rows for receipt replay. Invocation follows the explicitly
# sanctioned revision; a later policy change still requires a reviewed row.
POLICY_OPUS55_TRANSPORT: dict[str, Any] = {
    **POLICY_OPUS_TRANSPORT,
    "transport_id": "claude-code-opus55-policy-independent",
    "model": "claude-opus-5-5[1m]",
    # Claude CLI 2.1.281 accepts [1m] as context selection, but modelUsage's
    # canonicalModel is the pricing/model identity without that suffix.
    "canonical_model": "claude-opus-5-5",
    "authorized_since": "2026-09-22",
    "authority": BUSINESS / "ops/linear/approvals/OPERATOR-DECISION-20260922-hermes-opus55-maintenance.json",
}


def policy_opus_transport() -> dict[str, Any]:
    """Resolve the invocation policy without rewriting historical receipts."""
    import sys
    if str(BUSINESS) not in sys.path:
        sys.path.insert(0, str(BUSINESS))
    from ops.claude_model_policy import resolve
    model, effort = resolve()
    row = POLICY_OPUS55_TRANSPORT
    if (model, effort) != (row["model"], row["effort"]):
        raise TransportError("review_policy_transport_mismatch")
    return dict(row)


AUTHORIZED_TRANSPORTS: dict[str, dict[str, Any]] = {
    SOL_TRANSPORT["transport_id"]: SOL_TRANSPORT,
    OPUS_TRANSPORT["transport_id"]: OPUS_TRANSPORT,
    POLICY_OPUS_TRANSPORT["transport_id"]: POLICY_OPUS_TRANSPORT,
    POLICY_OPUS55_TRANSPORT["transport_id"]: POLICY_OPUS55_TRANSPORT,
}
DEFAULT_TRANSPORT_ID = SOL_TRANSPORT["transport_id"]

# The retired Bridge-review path. It was never packet-bound, never tool-free and
# never schema-pinned, so it stays forbidden in every active source regardless of
# which authorized transport is in use.
FORBIDDEN_TRANSPORT_TOKENS = ("claude_" + "design_review.py",)


class TransportError(RuntimeError):
    pass


def transport(transport_id: str | None) -> dict[str, Any]:
    resolved = transport_id or DEFAULT_TRANSPORT_ID
    if resolved not in AUTHORIZED_TRANSPORTS:
        raise TransportError(f"review_transport_unauthorized:{resolved}")
    return AUTHORIZED_TRANSPORTS[resolved]


def runtime_block(raw: dict[str, Any]) -> dict[str, Any]:
    """Return the transport runtime block.

    ``review_runtime`` is canonical. ``hermes_runtime`` is the Sol transport's
    original key and is still emitted by that wrapper, so receipts written
    before 2026-08-18 keep validating byte-for-byte.
    """
    block = raw.get("review_runtime")
    if not isinstance(block, dict):
        block = raw.get("hermes_runtime")
    return block if isinstance(block, dict) else {}


def identify_transport(raw: dict[str, Any]) -> dict[str, Any]:
    """Resolve which authorized transport produced this raw receipt."""
    review_input = raw.get("review_input") or {}
    declared = review_input.get("transport_id")
    if isinstance(declared, str) and declared:
        return transport(declared)
    runtime = runtime_block(raw)
    provider, model = review_input.get("provider"), review_input.get("model")
    for row in AUTHORIZED_TRANSPORTS.values():
        if provider == row["provider"] and model == row["model"]:
            return row
        if runtime.get("provider") == row["provider"] and runtime.get("model") == row["model"]:
            return row
    raise TransportError("review_transport_unauthorized:unidentified")


def validate_raw_review_runtime(raw: dict[str, Any], *, error: type[Exception] = TransportError,
                                prefix: str = "review") -> dict[str, Any]:
    """Fail closed unless the raw receipt proves one authorized transport ran clean.

    Identical safety bar for every transport. Returns the matched transport.
    """
    row = identify_transport(raw)
    review_input = raw.get("review_input") or {}
    runtime = runtime_block(raw)

    if (review_input.get("provider") != row["provider"]
            or review_input.get("model") != row["model"]
            or review_input.get("effort") != row["effort"]
            or review_input.get("api_mode") != row["api_mode"]
            or review_input.get("tools_disabled") is not True
            or review_input.get("tool_count") != 0
            or review_input.get("fallback_provider_count") != 0
            or review_input.get("context_isolated") is not True):
        raise error(f"{prefix}_execution_pin_invalid:{row['transport_id']}")

    if (runtime.get("provider") != row["provider"]
            or runtime.get("model") != row["model"]
            or runtime.get("api_mode") != row["api_mode"]
            or runtime.get("tool_count") != 0
            or runtime.get("tool_calls") != 0
            or runtime.get("fallback_provider_count") != 0
            or int(runtime.get("model_turns") or 0) != 1):
        raise error(f"{prefix}_runtime_unproven:{row['transport_id']}")

    if raw.get("permission_denials") not in (None, []):
        raise error(f"{prefix}_permission_denial_observed")
    usage = raw.get("usage") or {}
    if int(usage.get("tool_calls") or 0):
        raise error(f"{prefix}_tool_use_observed")
    if any(int(value or 0) for value in (usage.get("server_tool_use") or {}).values()):
        raise error(f"{prefix}_server_tool_use_observed")

    model_usage = raw.get("modelUsage") or {}
    if not isinstance(model_usage, dict) or not model_usage:
        raise error(f"{prefix}_model_unproven")
    canonical = {str((value or {}).get("canonicalModel") or key): (value or {})
                 for key, value in model_usage.items()}
    reviewer_model = row.get("canonical_model", row["model"])
    if reviewer_model not in canonical:
        raise error(f"{prefix}_model_unproven")
    allowed = {reviewer_model, *row["auxiliary_models"]}
    unexpected = sorted(set(canonical) - allowed)
    if unexpected:
        raise error(f"{prefix}_unauthorized_model_observed:{','.join(unexpected)}")
    reviewer_output = int(canonical[reviewer_model].get("outputTokens") or 0)
    if reviewer_output <= 0:
        raise error(f"{prefix}_model_unproven")
    # The verdict text must come from the reviewer, not an auxiliary model.
    if any(int(value.get("outputTokens") or 0) >= reviewer_output
           for name, value in canonical.items() if name != reviewer_model):
        raise error(f"{prefix}_auxiliary_model_output_dominant")
    return row
