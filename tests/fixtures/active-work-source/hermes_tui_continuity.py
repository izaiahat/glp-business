READY_MARKERS = (" ready │", "─ ready", "│ ready ", " ready ─")

ACTIVE_MARKERS = ("esc to interrupt", "Running tool", "Thinking…", "Working…")

BAD_MARKERS = (
    "error: session not found",
    "[Hermes TUI exited rc=",
    "Traceback (most recent call last)",
    "SyntaxError:",
    "missing state DB",
)

def activity_from_capture(capture: str) -> dict[str, Any]:
    lines = [line.rstrip() for line in capture.splitlines() if line.strip()]
    tail = "\n".join(lines[-8:])
    bad = [marker for marker in BAD_MARKERS if marker.lower() in tail.lower()]
    ready_pos = max((tail.rfind(marker) for marker in READY_MARKERS), default=-1)
    active_pos = max((tail.lower().rfind(marker.lower()) for marker in ACTIVE_MARKERS), default=-1)
    pending = False
    placeholders = {
        'Ask me anything…', 'Try "explain this codebase"', 'Try "write a test for…"',
        'Try "refactor the auth module"', 'Try "/help" for commands',
        'Try "fix the lint errors"', 'Try "how does the config loader work?"',
    }
    prompts = [line.strip()[1:].strip() for line in lines[-8:] if line.strip().startswith(('❯', '›'))]
    if prompts:
        pending = bool(prompts[-1] and prompts[-1] not in placeholders)
    if bad:
        state = "error"
    elif pending:
        state = "pending_input"
    elif ready_pos >= 0 and ready_pos > active_pos:
        state = "idle_ready"
    elif active_pos >= 0:
        state = "active"
    else:
        state = "unknown"
    return {
        "state": state,
        "evidence": {"ready_marker": ready_pos >= 0, "active_marker": active_pos >= 0, "bad_markers": bad},
    }
