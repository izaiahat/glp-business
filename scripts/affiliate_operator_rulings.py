#!/usr/bin/env python3
"""Immutable, newest-wins affiliate operator-rulings registry.

The registry is deliberately local and provider-free. Each job loads one
``RulingSnapshot`` at its boundary and passes it through every downstream
candidate decision. A malformed or ambiguous document is inert; it cannot
change an affiliate action.

Supported immutable document schema::

    {
      "schema": "affiliate-operator-rulings-v1",
      "schema_version": 1,
      "immutable": true,
      "ruled_at_utc": "2026-08-08T00:00:00Z",
      "source_message_id": "...",
      "rulings": [{
        "instruction_key": "program:example clinic",
        "program": "Example Clinic",
        "verdict": "HOLD",
        "reason": "..."
      }]
    }

Only exact, normalized program rules are executable. ``ALLOW`` is explicit
metadata that leaves the existing application gate in control; ``HOLD`` is a
terminal local policy hold. Legacy ``bridge-fit-rulings-v1`` documents are
loaded into the same frozen snapshot for compatibility, but their matching and
terminal semantics remain owned by ``affiliate_niche_geo_filter``.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

BUSINESS = Path("/home/ubuntu/business")
CANONICAL_RULES_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/operator-rulings"
SCHEMA = "affiliate-operator-rulings-v1"
SCHEMA_VERSION = 1
LEGACY_SCHEMA = "bridge-fit-rulings-v1"
VALID_VERDICTS = frozenset({"HOLD", "ALLOW"})
LEGACY_VERDICTS = frozenset({"FIT_APPLY", "NOT_FIT_PARKED"})
VALID_OPERATIONAL_VALUES = frozenset({
    "REQUIRE_EXACT_COMPLETE_DENOMINATOR",
    "NOT_ENABLED",
    "QUEUE_EXACT_CANONICAL_INVENTORY",
    "DURABLE_BLOCKER_INSTRUCTION",
})
VALID_TERMINAL_PROGRAM_STATUSES = frozenset({"terminal_provider_unavailable"})
_INGEST_RE = re.compile(
    r"^\s*RULING\s+(?P<verdict>HOLD|ALLOW)\s+(?:PROGRAM\s*:\s*)?"
    r"(?P<program>[A-Za-z0-9][A-Za-z0-9 .&'()/-]{0,120}?)(?:\s+-\s+(?P<reason>.{1,240}))?\s*$",
    re.I,
)


def normalize_program(value: object) -> str:
    """Stable conservative identity; an empty identity never matches."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def normalize_instruction_key(value: object) -> str:
    return re.sub(r"[^a-z0-9:]+", " ", str(value or "").lower()).strip()


def normalize_operational_key(value: object) -> str:
    """Conservative exact identity for standing, non-program instructions."""
    return str(value or "").strip().lower()


def valid_operational_key(value: object) -> bool:
    key = normalize_operational_key(value)
    return bool(
        key in {
            "blocker_cards.require_complete_program_denominator",
            "provider.awin.two_factor",
        }
        or re.fullmatch(r"apply_run\.message:[0-9]{6,24}:exact_inventory", key)
        or re.fullmatch(r"blocker:blk-[a-z0-9-]+:instruction(?::[0-9a-f]{16})?", key)
    )


def terminal_program_key(network: object, program_id: object) -> str:
    provider = str(network or "").strip().lower()
    identity = str(program_id or "").strip()
    if not re.fullmatch(r"[a-z0-9_-]{2,32}", provider) or not identity or len(identity) > 160:
        return ""
    return f"{provider}:{identity}"


def _parse_utc(value: object) -> datetime | None:
    text = str(value or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text):
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _safe_documents(root: Path, prefix: str) -> list[Path]:
    """Return only ordinary, direct-child JSON files under the supplied root."""
    try:
        canonical_root = root.resolve(strict=False)
    except OSError:
        return []
    if not canonical_root.is_dir():
        return []
    documents: list[Path] = []
    for path in sorted(canonical_root.glob(f"{prefix}*.json")):
        try:
            resolved = path.resolve(strict=True)
            mode = path.lstat().st_mode
            if resolved.parent != canonical_root or path.is_symlink() or not stat.S_ISREG(mode):
                continue
        except OSError:
            continue
        documents.append(resolved)
    return documents


def _read_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        raw = path.read_bytes()
        parsed = json.loads(raw)
    except (OSError, UnicodeDecodeError, ValueError):
        return None, ""
    return (parsed if isinstance(parsed, dict) else None,
            hashlib.sha256(raw).hexdigest())


@dataclass(frozen=True)
class OperatorRuling:
    instruction_key: str
    program: str
    normalized_program: str
    verdict: str
    reason: str
    ruled_at_utc: str
    source_message_id: str
    source_file: str
    source_sha256: str


@dataclass(frozen=True)
class OperationalRuling:
    instruction_key: str
    value: str
    reason: str
    operator_words_sha256: str
    ruled_at_utc: str
    source_message_id: str
    source_file: str
    source_sha256: str


@dataclass(frozen=True)
class TerminalProgramRuling:
    canonical_key: str
    network: str
    program_id: str
    name: str
    status: str
    reason_code: str
    reason: str
    exclude_from_application_execution: bool
    exclude_from_action_cards: bool
    exclude_from_waiting_footer: bool
    preserve_historical_inventory_identity: bool
    reopen_requirements: tuple[str, ...]
    authority_payload_sha256: str
    ruled_at_utc: str
    source_message_id: str
    source_file: str
    source_sha256: str


@dataclass(frozen=True)
class PolicyDecision:
    verdict: str
    instruction_key: str
    reason: str
    snapshot_version: str
    snapshot_sha256: str


@dataclass(frozen=True)
class RulingSnapshot:
    """One immutable policy view for one job run."""
    schema: str
    schema_version: int
    captured_at_utc: str
    snapshot_sha256: str
    version: str
    rulings: Mapping[str, OperatorRuling]
    operational_rulings: Mapping[str, OperationalRuling]
    terminal_program_rulings: Mapping[str, TerminalProgramRuling]
    bridge_rulings: Mapping[str, tuple[str, str]]
    rejected_documents: tuple[str, ...]

    def evaluate_program(self, program: object) -> PolicyDecision | None:
        normalized = normalize_program(program)
        if not normalized:
            return None
        ruling = self.rulings.get(f"program:{normalized}")
        if ruling is None:
            return None
        return PolicyDecision(
            verdict=ruling.verdict,
            instruction_key=ruling.instruction_key,
            reason=ruling.reason,
            snapshot_version=self.version,
            snapshot_sha256=self.snapshot_sha256,
        )

    def evaluate_operational(self, instruction_key: object) -> OperationalRuling | None:
        """Exact-key only. Operational policy never participates in program matching."""
        key = normalize_operational_key(instruction_key)
        if not valid_operational_key(key):
            return None
        return self.operational_rulings.get(key)

    def evaluate_terminal_program(self, network: object, program_id: object) -> TerminalProgramRuling | None:
        """Return only an exact canonical provider-program terminal ruling."""
        key = terminal_program_key(network, program_id)
        return self.terminal_program_rulings.get(key) if key else None


def _validate_operator_document(
    path: Path, doc: dict[str, Any], source_sha256: str,
) -> tuple[
    tuple[str, str, str], list[OperatorRuling], list[OperationalRuling], list[TerminalProgramRuling]
] | None:
    ruled_at = str(doc.get("ruled_at_utc") or "")
    source_message_id = str(doc.get("source_message_id") or "")
    rows = doc.get("rulings", [])
    operational_rows = doc.get("operational_rulings", [])
    terminal_rows = doc.get("terminal_program_rulings", [])
    payload_sha = doc.get("source_payload_sha256")
    if (
        doc.get("schema") != SCHEMA
        or doc.get("schema_version") != SCHEMA_VERSION
        or doc.get("immutable") is not True
        or _parse_utc(ruled_at) is None
        or not source_message_id
        or not isinstance(rows, list)
        or not isinstance(operational_rows, list)
        or not isinstance(terminal_rows, list)
        or (payload_sha is not None and not re.fullmatch(r"[0-9a-f]{64}", str(payload_sha)))
        or not (rows or operational_rows or terminal_rows)
    ):
        return None
    accepted: list[OperatorRuling] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        program = str(row.get("program") or "").strip()
        normalized = normalize_program(program)
        key = normalize_instruction_key(row.get("instruction_key"))
        verdict = str(row.get("verdict") or "").upper().strip()
        reason = str(row.get("reason") or "").strip()
        expected_key = f"program:{normalized}"
        if (not normalized or key != expected_key or verdict not in VALID_VERDICTS
                or not reason or key in seen):
            return None
        seen.add(key)
        accepted.append(OperatorRuling(
            instruction_key=key, program=program, normalized_program=normalized,
            verdict=verdict, reason=reason, ruled_at_utc=ruled_at,
            source_message_id=source_message_id, source_file=path.name,
            source_sha256=source_sha256,
        ))
    operational: list[OperationalRuling] = []
    operational_seen: set[str] = set()
    for row in operational_rows:
        if not isinstance(row, dict):
            return None
        key = normalize_operational_key(row.get("instruction_key"))
        value = str(row.get("value") or "").upper().strip()
        reason = str(row.get("reason") or "").strip()
        words_sha = str(row.get("operator_words_sha256") or "").lower().strip()
        if (not valid_operational_key(key) or value not in VALID_OPERATIONAL_VALUES
                or not reason or not re.fullmatch(r"[0-9a-f]{64}", words_sha)
                or key in operational_seen):
            return None
        operational_seen.add(key)
        operational.append(OperationalRuling(
            instruction_key=key, value=value, reason=reason,
            operator_words_sha256=words_sha, ruled_at_utc=ruled_at,
            source_message_id=source_message_id, source_file=path.name,
            source_sha256=source_sha256,
        ))
    terminal: list[TerminalProgramRuling] = []
    terminal_seen: set[str] = set()
    for row in terminal_rows:
        if not isinstance(row, dict):
            return None
        network = str(row.get("network") or "").strip().lower()
        program_id = str(row.get("program_id") or "").strip()
        key = terminal_program_key(network, program_id)
        status_value = str(row.get("status") or "").strip().lower()
        reopen = row.get("reopen_requirements")
        authority_sha = str(row.get("authority_payload_sha256") or "").strip().lower()
        if (
            not key or key in terminal_seen
            or status_value not in VALID_TERMINAL_PROGRAM_STATUSES
            or not str(row.get("name") or "").strip()
            or not str(row.get("reason_code") or "").strip()
            or not str(row.get("reason") or "").strip()
            or row.get("exclude_from_application_execution") is not True
            or row.get("exclude_from_action_cards") is not True
            or row.get("exclude_from_waiting_footer") is not True
            or row.get("preserve_historical_inventory_identity") is not True
            or not isinstance(reopen, list) or len(reopen) != 2
            or any(not str(value).strip() for value in reopen)
            or not re.fullmatch(r"[0-9a-f]{64}", authority_sha)
        ):
            return None
        terminal_seen.add(key)
        terminal.append(TerminalProgramRuling(
            canonical_key=key, network=network, program_id=program_id,
            name=str(row["name"]).strip(), status=status_value,
            reason_code=str(row["reason_code"]).strip(), reason=str(row["reason"]).strip(),
            exclude_from_application_execution=True, exclude_from_action_cards=True,
            exclude_from_waiting_footer=True, preserve_historical_inventory_identity=True,
            reopen_requirements=tuple(str(value).strip() for value in reopen),
            authority_payload_sha256=authority_sha, ruled_at_utc=ruled_at,
            source_message_id=source_message_id, source_file=path.name,
            source_sha256=source_sha256,
        ))
    return (ruled_at, source_message_id, path.name), accepted, operational, terminal


def _load_legacy_bridge_rulings(root: Path) -> tuple[dict[str, tuple[str, str]], list[dict[str, str]]]:
    """Mirror legacy timestamp/file newest-wins semantics without mutating them."""
    loaded: list[tuple[str, str, list[Any]]] = []
    accepted_docs: list[dict[str, str]] = []
    for path in _safe_documents(root, "bridge-fit-rulings-"):
        doc, source_sha256 = _read_json(path)
        if not doc or doc.get("schema") != LEGACY_SCHEMA or _parse_utc(doc.get("ruled_at_utc")) is None:
            continue
        rows = doc.get("rulings")
        if not isinstance(rows, list):
            continue
        loaded.append((str(doc["ruled_at_utc"]), path.name, rows))
        accepted_docs.append({"file": path.name, "sha256": source_sha256, "schema": LEGACY_SCHEMA})
    out: dict[str, tuple[str, str]] = {}
    for _stamp, _name, rows in sorted(loaded, key=lambda item: (item[0], item[1])):
        for row in rows:
            if not isinstance(row, dict):
                continue
            # Preserve the legacy loader's lowercase-but-not-normalized key.
            # `affiliate_niche_geo_filter.match_bridge_ruling()` intentionally
            # performs substring matching over the original program wording.
            program = str(row.get("program") or "").strip().lower()
            verdict = str(row.get("verdict") or "").strip()
            if program and verdict in LEGACY_VERDICTS:
                out[program] = (verdict, str(row.get("reason") or "").strip())
    return out, accepted_docs


def load_snapshot(root: Path | str | None = None, *, captured_at_utc: str | None = None) -> RulingSnapshot:
    """Read a fresh immutable snapshot. Call exactly once at a job boundary."""
    rules_root = Path(root) if root is not None else CANONICAL_RULES_DIR
    selected: dict[str, tuple[tuple[str, str, str], OperatorRuling]] = {}
    selected_operational: dict[str, tuple[tuple[str, str, str], OperationalRuling]] = {}
    selected_terminal: dict[str, tuple[tuple[str, str, str], TerminalProgramRuling]] = {}
    accepted_docs: list[dict[str, str]] = []
    rejected: list[str] = []
    for path in _safe_documents(rules_root, "operator-rulings-"):
        doc, source_sha256 = _read_json(path)
        if doc is None:
            rejected.append(path.name)
            continue
        result = _validate_operator_document(path, doc, source_sha256)
        if result is None:
            rejected.append(path.name)
            continue
        precedence, rows, operational_rows, terminal_rows = result
        accepted_docs.append({"file": path.name, "sha256": source_sha256, "schema": SCHEMA})
        for row in rows:
            prior = selected.get(row.instruction_key)
            if prior is None or precedence > prior[0]:
                selected[row.instruction_key] = (precedence, row)
        for row in operational_rows:
            prior = selected_operational.get(row.instruction_key)
            if prior is None or precedence > prior[0]:
                selected_operational[row.instruction_key] = (precedence, row)
        for row in terminal_rows:
            prior = selected_terminal.get(row.canonical_key)
            if prior is None or precedence > prior[0]:
                selected_terminal[row.canonical_key] = (precedence, row)
    bridge_root = rules_root.parent if rules_root.name == "operator-rulings" else rules_root
    bridge_rulings, legacy_docs = _load_legacy_bridge_rulings(bridge_root)
    manifest = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "operator_documents": sorted(accepted_docs, key=lambda row: row["file"]),
        "legacy_documents": sorted(legacy_docs, key=lambda row: row["file"]),
        "rulings": [
            {
                "key": key,
                "verdict": ruling.verdict,
                "ruled_at_utc": ruling.ruled_at_utc,
                "source_message_id": ruling.source_message_id,
                "source_file": ruling.source_file,
                "source_sha256": ruling.source_sha256,
            }
            for key, (_precedence, ruling) in sorted(selected.items())
        ],
        "operational_rulings": [
            {
                "key": key, "value": ruling.value,
                "ruled_at_utc": ruling.ruled_at_utc,
                "source_message_id": ruling.source_message_id,
                "source_file": ruling.source_file,
                "source_sha256": ruling.source_sha256,
            }
            for key, (_precedence, ruling) in sorted(selected_operational.items())
        ],
        "terminal_program_rulings": [
            {
                "key": key, "status": ruling.status,
                "ruled_at_utc": ruling.ruled_at_utc,
                "source_message_id": ruling.source_message_id,
                "source_file": ruling.source_file,
                "source_sha256": ruling.source_sha256,
                "authority_payload_sha256": ruling.authority_payload_sha256,
            }
            for key, (_precedence, ruling) in sorted(selected_terminal.items())
        ],
        "bridge_rulings": sorted((key, verdict, reason) for key, (verdict, reason) in bridge_rulings.items()),
    }
    snapshot_sha256 = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    captured = captured_at_utc or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return RulingSnapshot(
        schema=SCHEMA,
        schema_version=SCHEMA_VERSION,
        captured_at_utc=captured,
        snapshot_sha256=snapshot_sha256,
        version=f"{SCHEMA}@{snapshot_sha256[:16]}",
        rulings=MappingProxyType({key: ruling for key, (_precedence, ruling) in selected.items()}),
        operational_rulings=MappingProxyType({
            key: ruling for key, (_precedence, ruling) in selected_operational.items()
        }),
        terminal_program_rulings=MappingProxyType({
            key: ruling for key, (_precedence, ruling) in selected_terminal.items()
        }),
        bridge_rulings=MappingProxyType(dict(bridge_rulings)),
        rejected_documents=tuple(sorted(rejected)),
    )


def policy_receipt(snapshot: RulingSnapshot) -> dict[str, Any]:
    """Non-secret metadata suitable for local receipts."""
    return {
        "schema": snapshot.schema,
        "schema_version": snapshot.schema_version,
        "version": snapshot.version,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "captured_at_utc": snapshot.captured_at_utc,
        "ruling_count": len(snapshot.rulings),
        "operational_ruling_count": len(snapshot.operational_rulings),
        "operational_ruling_keys": sorted(snapshot.operational_rulings),
        "terminal_program_ruling_count": len(snapshot.terminal_program_rulings),
        "terminal_program_ruling_keys": sorted(snapshot.terminal_program_rulings),
        "legacy_bridge_ruling_count": len(snapshot.bridge_rulings),
        "rejected_document_count": len(snapshot.rejected_documents),
    }


def ingest_operator_message_rulings(message_id: str, lines: list[str], *,
                                    ruled_at_utc: str, root: Path | str | None = None) -> list[dict[str, str]]:
    """Persist only explicit ``RULING HOLD|ALLOW PROGRAM: ...`` operator lines.

    The narrow grammar prevents a narrative, acknowledgement, or vague request
    from silently becoming durable policy. Existing documents are never edited;
    a replay returns no new rulings and performs no rewrite.
    """
    if _parse_utc(ruled_at_utc) is None or not str(message_id).strip():
        return []
    parsed: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in lines:
        match = _INGEST_RE.fullmatch(str(line))
        if not match:
            continue
        program = match.group("program").strip()
        key = f"program:{normalize_program(program)}"
        reason = (match.group("reason") or "explicit operator ruling").strip()
        if not normalize_program(program) or key in seen or re.search(r"\b(?:api[ _-]?key|token|password|secret)\b", reason, re.I):
            return []
        seen.add(key)
        parsed.append({"instruction_key": key, "program": program,
                       "verdict": match.group("verdict").upper(), "reason": reason})
    if not parsed:
        return []
    rules_root = Path(root) if root is not None else CANONICAL_RULES_DIR
    rules_root.mkdir(parents=True, exist_ok=True)
    path = rules_root / f"operator-rulings-{str(message_id).strip()}.json"
    document = {"schema": SCHEMA, "schema_version": SCHEMA_VERSION, "immutable": True,
                "ruled_at_utc": ruled_at_utc, "source_message_id": str(message_id).strip(),
                "rulings": parsed}
    body = json.dumps(document, indent=2, sort_keys=True) + "\n"
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(body)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except FileExistsError:
        return []
    return [{"instruction_key": row["instruction_key"], "program": row["program"],
             "verdict": row["verdict"], "source_file": path.name} for row in parsed]


def write_immutable_ruling_document(
    message_id: str, *, ruled_at_utc: str, root: Path | str,
    program_rulings: list[dict[str, str]] | None = None,
    operational_rulings: list[dict[str, str]] | None = None,
    terminal_program_rulings: list[dict[str, Any]] | None = None,
    source_payload_sha256: str | None = None,
) -> dict[str, Any]:
    """Exclusive, byte-idempotent persistence for parsed operator instructions.

    ``source_payload_sha256`` binds a ruling document to the exact captured
    operator payload.  Terminal program rulings are separate from ordinary
    HOLD/ALLOW policy and require the explicit, two-part reopen contract
    validated by :func:`_validate_operator_document`.
    """
    message_id = str(message_id).strip()
    if not message_id or _parse_utc(ruled_at_utc) is None:
        raise ValueError("invalid_ruling_document_identity_or_timestamp")
    payload_sha = str(source_payload_sha256 or "").strip().lower()
    if payload_sha and not re.fullmatch(r"[0-9a-f]{64}", payload_sha):
        raise ValueError("invalid_source_payload_sha256")
    document: dict[str, Any] = {
        "schema": SCHEMA, "schema_version": SCHEMA_VERSION, "immutable": True,
        "ruled_at_utc": ruled_at_utc, "source_message_id": message_id,
        "rulings": list(program_rulings or []),
        "operational_rulings": list(operational_rulings or []),
        "terminal_program_rulings": list(terminal_program_rulings or []),
    }
    if payload_sha:
        document["source_payload_sha256"] = payload_sha
    root_path = Path(root)
    path = root_path / f"operator-rulings-{message_id}.json"
    body = (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    body_sha = hashlib.sha256(body).hexdigest()
    if _validate_operator_document(path, document, body_sha) is None:
        raise ValueError("invalid_ruling_document")
    root_path.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(body)
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        created = True
    except FileExistsError:
        if path.read_bytes() != body:
            raise RuntimeError(f"immutable_ruling_conflict:{path.name}")
        created = False
    return {
        "path": str(path), "source_file": path.name, "created": created,
        "sha256": hashlib.sha256(body).hexdigest(),
        "source_payload_sha256": payload_sha or None,
        "program_ruling_count": len(document["rulings"]),
        "operational_ruling_count": len(document["operational_rulings"]),
        "terminal_program_ruling_count": len(document["terminal_program_rulings"]),
    }
