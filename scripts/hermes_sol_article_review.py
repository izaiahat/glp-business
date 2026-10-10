#!/usr/bin/env python3
"""Packet-only GLP article reviewer backed by native Hermes/OpenAI Astra.

This is the review transport used by the GLP article-review controller. It
resolves the provider through Hermes, creates a Hermes ``AIAgent`` with an empty
tool surface and no fallback provider, and writes a hash-bound execution
receipt. It never mutates the reviewed packet or external systems.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any

TARGET_PROVIDER = "openai-codex"
TARGET_MODEL = "gpt-6-astra-900k"
TARGET_EFFORT = "high"
TARGET_TRANSPORT = "hermes-openai-codex-astra-independent"
TARGET_SPEED = "normal"
TARGET_SERVICE_TIER = "default"
TARGET_API_MODE = "codex_responses"
HERMES_PYTHON = Path("/home/ubuntu/.hermes/hermes-agent/venv/bin/python")
DEFAULT_MAX_CONTEXT_CHARS = 2_100_000
DEFAULT_MAX_TOTAL_CHARS = 4_500_000

SYSTEM_BRIEF = """You are the packet-only GLP article review verifier.
Review only the complete, in-band packet supplied by the caller. You have no
tools and must not request or simulate tool use. Do not browse, mutate files,
run commands, publish, deploy, send messages, or claim external verification.
You are an instrument advisory, not the operator or Bridge authority. If the
review prompt requires an exact JSON shape or JSON Schema, output exactly one
matching JSON object and no prose, preamble, or Markdown fence.
"""

ADVISORY_HEADER = "INSTRUMENT ADVISORY — NOT BRIDGE"
FORBIDDEN_AUTHORITY_CLAIMS = ("bridge fable", "independent fable seat")


def sha_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def capture_file(raw: str, max_chars: int) -> tuple[Path, str, dict[str, object]]:
    supplied = Path(raw).expanduser()
    if supplied.is_symlink() or not supplied.is_file():
        raise ValueError(f"review_input_not_regular_file:{supplied}")
    path = supplied.resolve()
    content = path.read_bytes()
    text = content.decode("utf-8", errors="strict")
    if len(text) > max_chars:
        raise ValueError(f"context_file_exceeds_limit:{path}:{len(text)}:{max_chars}")
    return path, text, {
        "path": str(path),
        "bytes": len(content),
        "sha256": sha_bytes(content),
        "content": text,
    }


def public_capture(row: object) -> dict[str, object]:
    if not isinstance(row, dict):
        raise ValueError("captured_review_input_invalid")
    return {key: row[key] for key in ("path", "bytes", "sha256")}


def load_schema(args: argparse.Namespace, captured: dict[str, object]) -> dict[str, Any] | None:
    if not args.json_schema_file:
        return None
    _, schema_text, row = capture_file(args.json_schema_file, args.max_context_chars)
    schema = json.loads(schema_text)
    if not isinstance(schema, dict):
        raise ValueError("json_schema_root_invalid")
    captured["json_schema_file"] = row
    return schema


def _matches_json_type(value: object, expected: str) -> bool:
    checks = {
        "null": lambda item: item is None,
        "boolean": lambda item: isinstance(item, bool),
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
    }
    if expected not in checks:
        raise ValueError(f"json_schema_type_unsupported:{expected}")
    return checks[expected](value)


def json_schema_errors(value: object, schema: object, location: str = "$") -> list[str]:
    """Validate the Draft-7 subset used by the pinned GLP review schema.

    The managed Hermes venv intentionally stays lean and does not ship the
    third-party ``jsonschema`` package. Keeping this small validator in the
    transport avoids a second interpreter boundary while still failing closed
    for every keyword present in the hash-pinned production schema.
    """
    if not isinstance(schema, dict):
        return [f"{location}:schema_not_object"]
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type is not None:
        expected_types = expected_type if isinstance(expected_type, list) else [expected_type]
        if not all(isinstance(item, str) for item in expected_types):
            return [f"{location}:schema_type_invalid"]
        if not any(_matches_json_type(value, item) for item in expected_types):
            return [f"{location}:expected_type:{'|'.join(expected_types)}"]
    if "const" in schema and value != schema["const"]:
        errors.append(f"{location}:const_mismatch")
    if "enum" in schema and value not in (schema.get("enum") or []):
        errors.append(f"{location}:enum_mismatch")
    if "allOf" in schema:
        for index, child in enumerate(schema["allOf"]):
            errors.extend(json_schema_errors(value, child, f"{location}.allOf[{index}]"))
    if "anyOf" in schema and not any(not json_schema_errors(value, child, location) for child in schema["anyOf"]):
        errors.append(f"{location}:anyOf_mismatch")
    if "oneOf" in schema:
        matches = sum(not json_schema_errors(value, child, location) for child in schema["oneOf"])
        if matches != 1:
            errors.append(f"{location}:oneOf_mismatch:{matches}")

    if isinstance(value, dict):
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                errors.append(f"{location}.{key}:required")
        properties = schema.get("properties") or {}
        if not isinstance(properties, dict):
            errors.append(f"{location}:schema_properties_invalid")
        else:
            for key, child in properties.items():
                if key in value:
                    errors.extend(json_schema_errors(value[key], child, f"{location}.{key}"))
            if schema.get("additionalProperties") is False:
                for key in value:
                    if key not in properties:
                        errors.append(f"{location}.{key}:additional_property")
        if "minProperties" in schema and len(value) < int(schema["minProperties"]):
            errors.append(f"{location}:minProperties")
        if "maxProperties" in schema and len(value) > int(schema["maxProperties"]):
            errors.append(f"{location}:maxProperties")

    if isinstance(value, list):
        if "minItems" in schema and len(value) < int(schema["minItems"]):
            errors.append(f"{location}:minItems")
        if "maxItems" in schema and len(value) > int(schema["maxItems"]):
            errors.append(f"{location}:maxItems")
        if schema.get("uniqueItems") is True:
            rendered = [json.dumps(item, sort_keys=True, ensure_ascii=False) for item in value]
            if len(rendered) != len(set(rendered)):
                errors.append(f"{location}:uniqueItems")
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                errors.extend(json_schema_errors(item, item_schema, f"{location}[{index}]"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < int(schema["minLength"]):
            errors.append(f"{location}:minLength")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            errors.append(f"{location}:maxLength")
        if "pattern" in schema and re.search(str(schema["pattern"]), value) is None:
            errors.append(f"{location}:pattern")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{location}:minimum")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{location}:maximum")
    return errors


def build_payload(
    args: argparse.Namespace,
    captured: dict[str, object] | None = None,
    schema: dict[str, Any] | None = None,
) -> str:
    captured = captured if captured is not None else {}
    parts = ["# Packet-only GLP article review input"]
    if args.prompt:
        parts.extend(("## Prompt", args.prompt.strip()))
    if args.prompt_file:
        path, text, row = capture_file(args.prompt_file, args.max_context_chars)
        captured["prompt_file"] = row
        parts.extend((f"## Prompt file: {path}", text))
    context_rows: list[dict[str, object]] = []
    for raw in args.context_file or []:
        path, text, row = capture_file(raw, args.max_context_chars)
        context_rows.append(row)
        parts.extend((f"## Context file: {path}", text))
    captured["context_files"] = context_rows
    if schema is not None:
        parts.extend((
            "## Binding JSON Schema",
            "Return exactly one JSON object satisfying this schema:",
            json.dumps(schema, ensure_ascii=False, sort_keys=True),
        ))
    payload = "\n\n".join(parts).strip() + "\n"
    if len(payload) > args.max_total_chars:
        raise ValueError(f"review_payload_exceeds_limit:{len(payload)}:{args.max_total_chars}")
    return payload


def assert_captured_inputs_unchanged(captured: dict[str, object]) -> None:
    rows = [
        captured.get("prompt_file"),
        captured.get("json_schema_file"),
        *(captured.get("context_files") or []),
    ]
    for row in rows:
        if row is None:
            continue
        if not isinstance(row, dict):
            raise ValueError("captured_review_input_invalid")
        path = Path(str(row["path"]))
        content = path.read_bytes()
        if len(content) != row["bytes"] or sha_bytes(content) != row["sha256"]:
            raise ValueError(f"review_input_changed_during_review:{path}")


def review_input(
    args: argparse.Namespace,
    payload: str,
    captured: dict[str, object],
    runtime: dict[str, object] | None = None,
) -> dict[str, object]:
    runtime = runtime or {}
    return {
        # Transport identity for the dual-authorized review contract
        # (systems/seo-blog-machine/scripts/review_transports.py, operator
        # authorization 2026-08-18). This transport's isolation comes from
        # skip_context_files + skip_memory + an empty tool surface.
        "transport_id": TARGET_TRANSPORT,
        "context_isolated": True,
        "hermes_managed": True,
        "provider": args.provider,
        "model": args.model,
        "api_mode": runtime.get("api_mode", TARGET_API_MODE),
        "effort": args.effort,
        "speed": TARGET_SPEED,
        "service_tier": TARGET_SERVICE_TIER,
        "tools_disabled": args.disable_tools,
        "tool_count": runtime.get("tool_count", 0),
        "fallback_provider_count": runtime.get("fallback_provider_count", 0),
        "inline_prompt": args.prompt,
        "prompt_file": public_capture(captured.get("prompt_file")) if args.prompt_file else None,
        "context_files": [public_capture(row) for row in captured.get("context_files", [])],
        "json_schema_file": (
            public_capture(captured.get("json_schema_file")) if args.json_schema_file else None
        ),
        "payload_chars": len(payload),
        "payload_sha256": sha_bytes(payload.encode("utf-8")),
        "max_context_chars": args.max_context_chars,
        "max_total_chars": args.max_total_chars,
    }


def validate_target_args(args: argparse.Namespace) -> None:
    if args.provider != TARGET_PROVIDER:
        raise ValueError(f"provider_must_be_{TARGET_PROVIDER}:{args.provider}")
    if args.model != TARGET_MODEL:
        raise ValueError(f"model_must_be_{TARGET_MODEL}:{args.model}")
    if args.effort != TARGET_EFFORT:
        raise ValueError(f"effort_must_be_{TARGET_EFFORT}:{args.effort}")
    if not args.disable_tools:
        raise ValueError("disable_tools_required")


SESSION_DB = Path('/home/ubuntu/.hermes/state.db')


def native_system_prompt(db, session):
    inline = session['system_prompt'] or ''
    digest = session['system_prompt_hash'] if 'system_prompt_hash' in session.keys() else None
    if not digest:
        return inline
    row = db.execute('select prompt from system_prompts where hash=?', (digest,)).fetchone()
    if row is None or sha_bytes(row['prompt'].encode()) != digest:
        raise ValueError('native system prompt content hash differs')
    if inline and inline != row['prompt']:
        raise ValueError('native inline system prompt conflicts with content address')
    return row['prompt']


def authenticate_result(session_id: str, payload: str, response: str, *,
                        model: str, provider: str, effort: str, session_db=None) -> dict:
    """Read native runtime custody; receipt hashes are bindings, never authority.

    Only AIAgent writes the transcript/lifecycle/accounting. This producer must
    not create sessions or append an alleged model decision on its behalf.
    The DB is fixed by maintained code, never chosen by a raw review receipt.
    """
    try:
        with sqlite3.connect(Path(session_db or SESSION_DB).resolve(strict=True).as_uri() + '?mode=ro',
                             uri=True) as db:
            db.row_factory = sqlite3.Row
            session = db.execute('select * from sessions where id=?', (session_id,)).fetchone()
            rows = db.execute('select id,role,content,tool_calls,timestamp from messages '
                              'where session_id=? order by id', (session_id,)).fetchall()
            system_prompt = native_system_prompt(db, session) if session else ''
        if session is None:
            raise ValueError('reviewer session absent')
        config = json.loads(session['model_config'] or '{}')
        if not (session['model'] == model == TARGET_MODEL
                and session['billing_provider'] == provider == TARGET_PROVIDER
                and config.get('reasoning_config', {}).get('effort') == effort == TARGET_EFFORT
                and config.get('max_iterations') == 1
                and session['api_call_count'] == 1 and session['tool_call_count'] == 0
                and SYSTEM_BRIEF in system_prompt):
            raise ValueError('runtime reviewer identity/configuration differs')
        if [row['role'] for row in rows] != ['user', 'assistant'] or any(
                json.loads(row['tool_calls'] or 'null') for row in rows):
            raise ValueError('not an isolated single no-tools review')
        if not (session['ended_at'] is not None
                and session['started_at'] <= rows[0]['timestamp'] <= rows[1]['timestamp'] <= session['ended_at']):
            raise ValueError('reviewer execution not durably completed')
        if rows[0]['content'] != payload or advisory_output(rows[1]['content']) != advisory_output(response):
            raise ValueError('runtime input/response differs')
        return {'schema': 'glp-native-reviewer-result-custody-v1', 'session_id': session_id,
                'model': model, 'provider': provider, 'effort': effort,
                'started_at': session['started_at'], 'ended_at': session['ended_at'],
                'input_message_id': rows[0]['id'], 'response_message_id': rows[1]['id'],
                'system_prompt_sha256': sha_bytes(system_prompt.encode()),
                'payload_sha256': sha_bytes(payload.encode()),
                'response_sha256': sha_bytes(advisory_output(response).encode())}
    except (OSError, sqlite3.Error, KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(f'reviewer_result_custody_refused:{exc}') from exc


def validate_result_custody(raw: dict, *, manifest: dict, packet: Path, session_db=None) -> dict:
    """Authenticate the response and exact sealed packet the model actually saw."""
    try:
        inputs, runtime = raw['review_input'], raw['hermes_runtime']
        claimed = runtime['result_custody']
        if raw['session_id'] != runtime['session_id']:
            raise ValueError('reviewer session mismatch')
        args = argparse.Namespace(
            prompt=inputs.get('inline_prompt'), prompt_file=(inputs.get('prompt_file') or {}).get('path'),
            context_file=[row['path'] for row in inputs['context_files']],
            json_schema_file=(inputs.get('json_schema_file') or {}).get('path'),
            max_context_chars=inputs['max_context_chars'], max_total_chars=inputs['max_total_chars'],
            model=runtime['model'], provider=runtime['provider'], effort=runtime['effort'], disable_tools=True)
        captured = {}
        schema = load_schema(args, captured)
        payload = build_payload(args, captured, schema)
        observed = review_input(args, payload, captured, runtime)
        if observed != inputs:
            raise ValueError('captured review input changed')
        # Hashing an input is insufficient: bind the actual in-band subjects to
        # the accepted manifest/packet, not a fixture-supplied input hash.
        parts = ['SEALED ARTICLE SUBJECTS — READ ONLY',
                 f"manifest_sha256={manifest['manifest_sha256']}", '']
        for row in manifest['subject_files']:
            parts.append(f"===== {row['path']} =====")
            text = (packet / row['path']).read_text(encoding='utf-8')
            parts.extend(f'{i:04d}|{line}' for i, line in enumerate(text.splitlines(), 1))
            parts.append('')
        expected_context = '\n'.join(parts) + '\n'
        if len(captured['context_files']) != 1 or captured['context_files'][0]['content'] != expected_context:
            raise ValueError('runtime input is not this sealed packet')
        custody = authenticate_result(runtime['session_id'], payload, raw['result'], model=runtime['model'],
                                      provider=runtime['provider'], effort=runtime['effort'], session_db=session_db)
        if custody != claimed:
            raise ValueError('runtime custody binding differs')
        return custody
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f'reviewer_result_custody_refused:{exc}') from exc


def hermes_agent_kwargs(
    runtime: dict[str, Any], args: argparse.Namespace, session_id: str, session_db
) -> dict[str, Any]:
    """Return the fail-closed Hermes agent configuration for one review."""
    return {
        "api_key": runtime.get("api_key"),
        "base_url": runtime.get("base_url"),
        "provider": runtime.get("provider"),
        "requested_provider": runtime.get("requested_provider"),
        "api_mode": runtime.get("api_mode"),
        "model": args.model,
        "enabled_toolsets": [],
        "disabled_toolsets": [],
        "quiet_mode": True,
        "save_trajectories": False,
        "skip_context_files": True,
        "skip_memory": True,
        "load_soul_identity": False,
        "platform": "cli",
        "session_db": session_db,
        "session_id": session_id,
        "max_iterations": 1,
        "reasoning_config": {"enabled": True, "effort": args.effort},
        "request_overrides": {"timeout": float(args.timeout), "service_tier": TARGET_SERVICE_TIER},
        "fallback_model": None,
        "credential_pool": runtime.get("credential_pool"),
    }


def tool_call_count(messages: object) -> int:
    if not isinstance(messages, list):
        return 0
    total = 0
    for message in messages:
        if isinstance(message, dict) and isinstance(message.get("tool_calls"), list):
            total += len(message["tool_calls"])
    return total


def ensure_hermes_python() -> None:
    """Re-exec under the managed Hermes venv before provider initialization."""
    expected = HERMES_PYTHON.absolute()
    expected_prefix = expected.parent.parent.resolve()
    if Path(sys.prefix).resolve() == expected_prefix:
        return
    if os.environ.get("GLP_HERMES_SOL_REEXEC") == "1":
        raise RuntimeError(f"hermes_python_reexec_failed:{sys.executable}:{expected}")
    if not expected.is_file() or not os.access(expected, os.X_OK):
        raise RuntimeError(f"hermes_python_missing:{expected}")
    environment = dict(os.environ)
    environment["GLP_HERMES_SOL_REEXEC"] = "1"
    # Execute the venv symlink, not its resolved base interpreter. Resolving it
    # would bypass pyvenv.cfg and silently lose Hermes' installed dependencies.
    os.execve(str(expected), [str(expected), str(Path(__file__).resolve()), *sys.argv[1:]], environment)


def run_hermes(
    payload: str, args: argparse.Namespace
) -> tuple[str, dict[str, Any], dict[str, object]]:
    """Execute one no-tools review through Hermes' provider/runtime boundary."""
    validate_target_args(args)
    ensure_hermes_python()
    # System Python normally receives the Hermes checkout through its installed
    # .pth file. Keep the explicit path fallback so scheduler environments with
    # a minimal PYTHONPATH still resolve the same Hermes-managed runtime.
    hermes_checkout = Path("/home/ubuntu/.hermes/hermes-agent")
    if str(hermes_checkout) not in sys.path:
        sys.path.insert(0, str(hermes_checkout))

    from hermes_cli.runtime_provider import resolve_runtime_provider
    from agent.reasoning_effort import codex_supported_efforts
    from run_agent import AIAgent

    if args.effort not in codex_supported_efforts(args.model):
        raise RuntimeError(f"hermes_review_effort_unsupported:{args.model}:{args.effort}")
    runtime = resolve_runtime_provider(requested=args.provider, target_model=args.model)
    if (
        runtime.get("provider") != TARGET_PROVIDER
        or runtime.get("requested_provider") not in (None, TARGET_PROVIDER)
        or runtime.get("api_mode") != TARGET_API_MODE
    ):
        raise RuntimeError(
            "hermes_runtime_identity_invalid:"
            f"{runtime.get('provider')}:{runtime.get('requested_provider')}:{runtime.get('api_mode')}"
        )

    from hermes_state_registry import acquire, release_or_close
    session_id = f"glp_astra_review_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    session_db = acquire(SESSION_DB)
    agent = None
    started = time.monotonic()
    result: dict[str, Any] = {}
    try:
        agent = AIAgent(**hermes_agent_kwargs(runtime, args, session_id, session_db))
        if agent.tools or agent.valid_tool_names:
            raise RuntimeError("hermes_review_tool_surface_not_empty")
        # Open native custody before the first input timestamp; no wrapper DB writes.
        setattr(agent, '_cached_system_prompt', getattr(agent, '_build_system_prompt')(SYSTEM_BRIEF))
        getattr(agent, '_ensure_db_session')()
        result = agent.run_conversation(payload, system_message=SYSTEM_BRIEF)
    finally:
        try:
            if agent is not None:
                agent.close()
        finally:
            release_or_close(session_db)
    elapsed_ms = int((time.monotonic() - started) * 1000)

    calls = tool_call_count(result.get("messages"))
    if agent.tools or agent.valid_tool_names or calls:
        raise RuntimeError(f"hermes_review_tool_use_observed:{calls}")
    if (
        result.get("provider") != TARGET_PROVIDER
        or result.get("model") != TARGET_MODEL
        or result.get("failed") is True
        or result.get("completed") is not True
    ):
        raise RuntimeError(
            "hermes_review_execution_invalid:"
            f"{result.get('provider')}:{result.get('model')}:"
            f"{result.get('completed')}:{result.get('failed')}"
        )
    response = str(result.get("final_response") or "").strip()
    if not response or response == "(empty)":
        raise RuntimeError("hermes_review_empty_response")
    if int(result.get("api_calls") or 0) != 1:
        raise RuntimeError(f"hermes_review_turn_count_invalid:{result.get('api_calls')}")
    execution = {
        "transport_id": TARGET_TRANSPORT,
        "context_isolated": True,
        "model_turns": int(result.get("api_calls") or 0),
        "provider": result["provider"],
        "model": result["model"],
        "effort": args.effort,
        "speed": TARGET_SPEED,
        "service_tier": TARGET_SERVICE_TIER,
        "service_tier_evidence": "explicit_request_override_not_provider_confirmation",
        "api_mode": TARGET_API_MODE,
        "session_id": result.get("session_id") or session_id,
        "api_calls": int(result.get("api_calls") or 0),
        "tool_count": 0,
        "tool_calls": calls,
        "fallback_provider_count": 0,
        "duration_api_ms": elapsed_ms,
        "python_executable": str(Path(sys.executable).resolve()),
    }
    execution['result_custody'] = authenticate_result(
        str(execution['session_id']), payload, response, model=args.model, provider=args.provider, effort=args.effort)
    return response, result, execution


def strict_json_result(response: str, schema: dict[str, Any] | None) -> dict[str, Any]:
    text = response.strip()
    if text.startswith(ADVISORY_HEADER):
        text = text[len(ADVISORY_HEADER):].strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"review_result_not_strict_json:{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("review_result_root_invalid")
    if schema is not None:
        errors = json_schema_errors(value, schema)
        if errors:
            raise ValueError(f"review_result_schema_invalid:{errors[0]}")
    return value


def advisory_output(value: str) -> str:
    text = value.strip()
    lowered = text.casefold()
    if any(claim in lowered for claim in FORBIDDEN_AUTHORITY_CLAIMS):
        raise ValueError("forbidden Bridge authority claim in instrument output")
    if text.startswith(ADVISORY_HEADER):
        return text
    return f"{ADVISORY_HEADER}\n\n{text}"


def secure_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content.rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run a packet-only independent GLP review through native Hermes/OpenAI Astra."
    )
    parser.add_argument("--prompt", help="Inline review prompt.")
    parser.add_argument("--prompt-file", help="Review prompt file.")
    parser.add_argument("--context-file", action="append", help="In-band context file; repeatable.")
    parser.add_argument("--provider", default=TARGET_PROVIDER)
    parser.add_argument("--model", default=TARGET_MODEL)
    parser.add_argument("--effort", default=TARGET_EFFORT)
    parser.add_argument("--max-context-chars", type=int, default=DEFAULT_MAX_CONTEXT_CHARS)
    parser.add_argument("--max-total-chars", type=int, default=DEFAULT_MAX_TOTAL_CHARS)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--output-format", choices=("text", "json"), default="text")
    parser.add_argument("--receipt-file", help="Secure raw execution receipt; requires JSON output.")
    parser.add_argument("--disable-tools", action="store_true")
    parser.add_argument("--json-schema-file", help="Require output matching this local JSON Schema.")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.prompt and not args.prompt_file:
        parser.error("provide --prompt or --prompt-file")
    if args.receipt_file and args.output_format != "json":
        parser.error("--receipt-file requires --output-format json")
    try:
        validate_target_args(args)
        captured: dict[str, object] = {}
        schema = load_schema(args, captured)
        payload = build_payload(args, captured, schema)
        if args.dry_run:
            print(json.dumps({
                "status": "PASS_HERMES_SOL_REVIEW_DRY_RUN",
                "provider": args.provider,
                "model": args.model,
                "effort": args.effort,
                "tools_disabled": True,
                "payload_chars": len(payload),
                "payload_sha256": sha_bytes(payload.encode("utf-8")),
            }, sort_keys=True))
            return 0

        response, result, execution = run_hermes(payload, args)
        assert_captured_inputs_unchanged(captured)
        if args.output_format == "json":
            strict_json_result(response, schema)
        rendered_result = advisory_output(response)
        receipt = {
            "result": rendered_result,
            "is_error": False,
            "authority_class": "instrument_advisory",
            "bridge_authority": False,
            "session_id": execution["session_id"],
            "duration_api_ms": execution["duration_api_ms"],
            "num_turns": execution["api_calls"],
            "permission_denials": [],
            "modelUsage": {
                TARGET_MODEL: {
                    "canonicalModel": TARGET_MODEL,
                    "provider": TARGET_PROVIDER,
                    "inputTokens": int(result.get("input_tokens") or 0),
                    "outputTokens": int(result.get("output_tokens") or 0),
                    "reasoningTokens": int(result.get("reasoning_tokens") or 0),
                }
            },
            "usage": {
                "server_tool_use": {
                    "web_search_requests": 0,
                    "web_fetch_requests": 0,
                },
                "tool_calls": execution["tool_calls"],
            },
            "review_input": review_input(args, payload, captured, execution),
            "hermes_runtime": execution,
        }
        receipt_body = dict(receipt)
        receipt["record_sha256"] = sha_bytes(
            json.dumps(
                receipt_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        )
        rendered_receipt = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
        if args.receipt_file:
            secure_write(Path(args.receipt_file).expanduser().resolve(), rendered_receipt)
        print(rendered_result)
        return 0
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        print(f"BLOCKED: Hermes Sol review failed: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
