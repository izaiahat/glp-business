#!/usr/bin/env python3
"""Deterministic fleet house style for cron Discord messages.

Normative spec:
  /home/ubuntu/business/ops/CRON-DISCORD-OUTPUT-SOP.md
  /home/ubuntu/business/reports/claude-audit-20260707/overnight-20260708/discord-style-guide.md

The module is dependency-free and safe to import from no-agent cron scripts.
``render`` enforces the class line budget and 1900-character cap. ``lint`` is
the permanent creation/continuous-gate API.
"""
from __future__ import annotations

import re
from typing import Any

# Fixed fleet-wide emoji semantics — never repurpose.
STATUS = {
    "ok": "✅",
    "warn": "⚠️",
    "action": "🔴",
    "report": "📊",
    "money": "💰",
    "watch": "🕵️",
    "fable": "🔮",
    "infra": "⚙️",
}
EMOJI_TO_STATUS = {value: key for key, value in STATUS.items()}
OUTPUT_CLASS_BY_STATUS = {
    "ok": "report",
    "warn": "alert",
    "action": "alert",
    "report": "report",
    "money": "event",
    "watch": "event",
    "fable": "report",
    "infra": "report",
}
CLASS_LINE_BUDGET = {"silence": 0, "event": 6, "alert": 6, "report": 12, "actionable": 24}
MAX_LEN = 1900
STYLE_MARKER = "CRON_DISCORD_HOUSE_STYLE_V1"

_TITLE_RE = re.compile(
    r"^(✅|⚠️|🔴|📊|💰|🕵️|🔮|⚙️) \*\*([^*\n]{1,80})\*\* — ([^\n]{1,500})$"
)
_VAGUE_HEADLINE_RE = re.compile(
    r"^(?:run |cron |job )?(?:completed|complete|status update|update|done|finished|success|healthy|ok|report ready|alert)$",
    re.I,
)
_RAW_JSON_RE = re.compile(r"(?:^|\n)\s*[\[{]\s*(?:\n|\"[^\"]+\"\s*:)", re.M)
_TRACEBACK_RE = re.compile(
    r"Traceback \(most recent call last\)|(?:^|\n)\s*(?:File \"[^\"]+\", line \d+|[A-Za-z]+Error:)",
    re.M,
)
_SECRET_RE = re.compile(
    r"(?i)(?:api[_ -]?key|authorization|bearer|client[_ -]?secret|refresh[_ -]?token|password|private[_ -]?key)\s*[:=]\s*\S+"
)
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[-. (]*)?\d{3}[-. )]+\d{3}[-. ]+\d{4}(?!\d)")
_ISO_TIMESTAMP_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?\b")
_JOB_ID_RE = re.compile(r"(?i)(?:job(?:_id)?\s*[:=]\s*|`)[0-9a-f]{12}(?:`|\b)")
_DECORATIVE_EMOJI_RE = re.compile(r"[🚨🧪🎥👀📬🎉🔥✨🚀🟢🟡🟠❌❗‼️]")


def _single_line(value: Any, field: str) -> str:
    text = str(value if value is not None else "").strip()
    if not text or "\n" in text or "\r" in text:
        raise ValueError(f"{field} must be one non-empty line")
    return text


def title(status: str, name: Any, headline: Any) -> str:
    """Return the canonical title line; literal/decorative emoji are rejected."""
    if status not in STATUS:
        raise ValueError(f"unknown Discord status key: {status!r}")
    safe_name = _single_line(name, "short name")
    safe_headline = _single_line(headline, "headline")
    return f"{STATUS[status]} **{safe_name}** — {safe_headline}"


def kv(label: Any, value: Any) -> str:
    """One fact: label with its value bolded."""
    return f"{_single_line(label, 'label')}: **{_single_line(value, 'value')}**"


def kvline(*pairs: tuple[Any, Any]) -> str:
    """Join related key/value facts with the fleet middle-dot separator."""
    return " · ".join(kv(label, value) for label, value in pairs)


def section(header: Any, *lines: Any) -> str:
    """Report mini-section with one logical fact per line."""
    out = [f"**{_single_line(header, 'section header')}**"]
    out.extend(str(line).strip() for line in lines if line is not None and str(line).strip())
    return "\n".join(out)


def meaning(text: Any) -> str:
    """Return the single so-what/action line. Mandatory on alerts."""
    return f"→ {_single_line(text, 'meaning')}"


def subtext(text: Any) -> str:
    """Return the one allowed final artifact/trace line."""
    return f"-# {_single_line(text, 'subtext')}"


def _parse_title(line: str) -> tuple[str | None, str | None, str | None, str | None]:
    match = _TITLE_RE.fullmatch(line)
    if not match:
        return None, None, None, None
    emoji, name, headline = match.groups()
    status = EMOJI_TO_STATUS.get(emoji)
    return status, name, headline, OUTPUT_CLASS_BY_STATUS.get(status or "")


def infer_output_class(message: str) -> str | None:
    if not message.strip():
        return "silence"
    first = message.strip().splitlines()[0]
    return _parse_title(first)[3]


def _safe_overflow_message() -> str:
    return (
        "🔴 **Output Guard** — protected house-style fields exceed 1900 characters\n\n"
        "→ inspect the local artifact; unsafe Discord output was suppressed\n"
        "-# source: discord_fmt protected-field overflow guard"
    )


def render(*parts: Any, max_len: int = MAX_LEN, output_class: str | None = None) -> str:
    """Render a message with deterministic line and character budgets.

    The title is separated by one blank line. When a body is over budget,
    trailing detail is dropped before the one meaning and one subtext line. A
    lone ellipsis records the collapse. Protected fields are never byte-sliced.
    """
    values = [str(part).strip() for part in parts if part is not None and str(part).strip()]
    if not values:
        return ""
    head = values[0]
    raw_lines: list[str] = []
    for value in values[1:]:
        raw_lines.extend(line.strip() for line in value.splitlines() if line.strip())

    meaning_lines = [line for line in raw_lines if line.startswith("→ ")]
    subtext_lines = [line for line in raw_lines if line.startswith("-# ")]
    # Preserve only one protected line of each type; lint catches duplicate input.
    keep_meaning = meaning_lines[:1]
    keep_subtext = subtext_lines[:1]
    body = [line for line in raw_lines if not line.startswith("→ ") and not line.startswith("-# ")]

    inferred = infer_output_class(head)
    cls = output_class or inferred or "report"
    budget = CLASS_LINE_BUDGET.get(cls, CLASS_LINE_BUDGET["report"])
    protected_count = 1 + len(keep_meaning) + len(keep_subtext)
    body_budget = max(0, budget - protected_count)
    collapsed = len(body) > body_budget
    if collapsed and body_budget:
        body = body[: max(0, body_budget - 1)] + ["…"]
    elif collapsed:
        body = []

    def assemble(lines: list[str]) -> str:
        tail = lines + keep_meaning + keep_subtext
        return head + ("\n\n" + "\n".join(tail) if tail else "")

    message = assemble(body)
    while len(message) > max_len and body:
        # Every pass must strictly SHRINK `body`. The earlier version popped the
        # ellipsis and then re-appended it on the next line, so a body that was
        # still over `max_len` after the pop looped forever — a composer (and the
        # cron holding its lock) would hang rather than emit a short card.
        body.pop()
        if collapsed and body:
            body[-1] = "…"
        message = assemble(body)
    if len(message) > max_len:
        return _safe_overflow_message()
    return message


def lint(message: str, output_class: str | None = None) -> dict[str, Any]:
    """Deterministically lint one rendered Discord payload.

    Returns ``{"status":"pass|fail", "errors": [...], ...}`` and never raises
    for content errors. ``output_class`` must be silence/event/alert/report when
    supplied; otherwise it is inferred from the title emoji.
    """
    text = str(message or "")
    errors: list[str] = []
    warnings: list[str] = []
    if output_class not in {None, *CLASS_LINE_BUDGET}:
        errors.append("unknown_output_class")
    if not text.strip():
        cls = output_class or "silence"
        if cls != "silence":
            errors.append("non_silence_fixture_is_empty")
        return {
            "status": "pass" if not errors else "fail",
            "output_class": cls,
            "nonblank_lines": 0,
            "characters": 0,
            "errors": errors,
            "warnings": warnings,
        }
    if output_class == "silence":
        errors.append("healthy_no_news_must_be_silent")

    lines = text.splitlines()
    nonblank = [line.strip() for line in lines if line.strip()]
    first = nonblank[0] if nonblank else ""
    status_key, short_name, headline, inferred = _parse_title(first)
    if not status_key:
        errors.append("invalid_title_shape_or_status_emoji")
    else:
        if not short_name or len(short_name) > 80:
            errors.append("invalid_short_name")
        if not headline or _VAGUE_HEADLINE_RE.fullmatch(headline.strip()):
            errors.append("vague_title_headline")
    cls = output_class or inferred or "report"
    if output_class and inferred and output_class != inferred and not (
        output_class == "actionable" and inferred in {"event", "alert"}
    ):
        errors.append(f"title_semantics_mismatch:{inferred}_vs_{output_class}")

    if len(text) > MAX_LEN:
        errors.append("over_1900_character_cap")
    budget = CLASS_LINE_BUDGET.get(cls, CLASS_LINE_BUDGET["report"])
    if len(nonblank) > budget:
        errors.append(f"over_{cls}_line_budget:{len(nonblank)}>{budget}")

    meaning_lines = [line for line in nonblank if line.startswith("→ ")]
    subtext_lines = [line for line in nonblank if line.startswith("-# ")]
    if cls in {"alert", "actionable"} and len(meaning_lines) != 1:
        errors.append(f"{cls}s_require_exactly_one_meaning_line")
    elif len(meaning_lines) > 1:
        errors.append("at_most_one_meaning_line")
    if len(subtext_lines) > 1:
        errors.append("at_most_one_subtext_line")
    if subtext_lines and nonblank[-1] != subtext_lines[0]:
        errors.append("subtext_must_be_final")

    if _RAW_JSON_RE.search(text):
        errors.append("raw_json_dump_banned")
    if _TRACEBACK_RE.search(text):
        errors.append("traceback_or_exception_dump_banned")
    if _SECRET_RE.search(text):
        errors.append("secret_like_value_banned")
    if cls != "actionable" and _PHONE_RE.search(text):
        errors.append("phone_number_banned")

    body_without_subtext = "\n".join(
        line for line in nonblank[1:] if not line.startswith("-# ")
    )
    if cls != "actionable" and _ISO_TIMESTAMP_RE.search(body_without_subtext):
        errors.append("timestamp_in_body_banned")
    if _JOB_ID_RE.search(body_without_subtext):
        errors.append("job_id_in_body_banned")
    if _DECORATIVE_EMOJI_RE.search(body_without_subtext):
        errors.append("decorative_or_nonsemantic_emoji_banned")

    if len(lines) > 1 and lines[1] != "":
        errors.append("missing_blank_line_after_title")
    return {
        "status": "pass" if not errors else "fail",
        "output_class": cls,
        "status_key": status_key,
        "short_name": short_name,
        "headline": headline,
        "nonblank_lines": len(nonblank),
        "characters": len(text),
        "errors": errors,
        "warnings": warnings,
    }


def demo_messages() -> list[tuple[str, str]]:
    alert = render(
        title("warn", "Spend Guard", "Google spend $33.40 crossed soft-pause $32"),
        kvline(("hard cap", "$40"), ("status", "PAUSED")),
        meaning("no action needed — guard auto-paused the campaign"),
        subtext("artifact: reports/glp-ads-control-plane/spend-guard-latest.json"),
    )
    event = render(
        title("watch", "Clinic Replies", "2 new clinic partner replies"),
        kvline(("warm", 1), ("vendor/system", 1)),
        "**#1** pricing question — human review",
        meaning("draft a reply for #1 — approval required before send"),
        subtext("artifact: reports/glp-cron-output-artifacts/reply-monitor.json"),
    )
    report = render(
        title("report", "Clinic Outreach Daily", "3 sends · 1 reply · 0 errors"),
        "**Pipeline** queued: **14** · day0-ready: **6**",
        "**Yesterday** sent: **3** · bounces: **0**",
        meaning("approve the follow-up batch in #decisions"),
        subtext("full report: reports/glp-cron-output-artifacts/health.json"),
    )
    return [("alert", alert), ("event", event), ("report", report)]


def _test() -> int:
    failures: list[str] = []

    def check(name: str, condition: bool) -> None:
        if not condition:
            failures.append(name)

    check("fixed title", title("warn", "Spend Guard", "1 issue") == "⚠️ **Spend Guard** — 1 issue")
    try:
        title("🧪", "X", "1 fixture")
        failures.append("literal emoji rejected")
    except ValueError:
        pass
    check("kv bold", kv("sent", 3) == "sent: **3**")
    check("empty render", render() == "")

    overflow = render(
        title("warn", "Budget Test", "20 facts exceeded the alert budget"),
        *[f"fact {index}: **{index}**" for index in range(20)],
        meaning("inspect the artifact"),
        subtext("artifact: reports/test.json"),
    )
    check("alert line budget", len([x for x in overflow.splitlines() if x.strip()]) <= 6)
    check("cap", len(overflow) <= MAX_LEN)
    check("protected meaning", "→ inspect the artifact" in overflow)
    check("protected subtext", overflow.rstrip().endswith("-# artifact: reports/test.json"))

    for cls, message in demo_messages():
        result = lint(message, cls)
        check(f"demo {cls} lint", result["status"] == "pass")
    check("silence pass", lint("", "silence")["status"] == "pass")
    check("healthy noise rejected", lint(title("ok", "Canary", "1 healthy check"), "silence")["status"] == "fail")

    bad_alert = "⚠️ **Cron Health** — 1 job needs attention\n\nstatus: **blocked**"
    check("alert meaning required", "alerts_require_exactly_one_meaning_line" in lint(bad_alert, "alert")["errors"])
    duplicate_subtext = render(title("report", "X", "1 report"), subtext("a"), subtext("b"))
    # render is fail-safe and keeps one; raw linter still rejects duplicates.
    raw_duplicate = title("report", "X", "1 report") + "\n\n-# a\n-# b"
    check("duplicate subtext rejected", "at_most_one_subtext_line" in lint(raw_duplicate, "report")["errors"])
    check("render keeps one subtext", len([x for x in duplicate_subtext.splitlines() if x.startswith("-# ")]) == 1)
    check("raw json rejected", "raw_json_dump_banned" in lint(title("report", "X", "1 report") + '\n\n{"status":"ok"}', "report")["errors"])
    check("traceback rejected", "traceback_or_exception_dump_banned" in lint(title("action", "X", "1 failure") + "\n\nTraceback (most recent call last):\n→ inspect artifact", "alert")["errors"])
    check("vague title rejected", "vague_title_headline" in lint("📊 **X** — run completed", "report")["errors"])
    check("wrong semantics rejected", any(x.startswith("title_semantics_mismatch") for x in lint(title("report", "X", "1 issue"), "alert")["errors"]))

    if failures:
        print("FAIL: " + ", ".join(failures))
        return 1
    print(f"discord_fmt self-test OK ({len(STATUS)} fixed statuses, lint + class budgets, cap {MAX_LEN})")
    return 0


if __name__ == "__main__":
    import sys
    if "--test" in sys.argv:
        raise SystemExit(_test())
    for class_name, message in demo_messages():
        print(f"--- {class_name} ({len(message)} chars) ---")
        print(message)
        print()
