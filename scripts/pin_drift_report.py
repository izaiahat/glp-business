#!/usr/bin/env python3
"""Fleet-wide, read-only hash-pin coverage and drift report.

Pin enforcement stays strict and re-pinning stays a reviewed release
transaction. This reporter only discovers and measures pins. It covers Python
path-to-hash tables, Python scalar Path/EXPECTED_*_SHA256 pairs in Hermes cron
carriers, every shell convention currently present under ``~/.hermes/scripts``,
and every enabled Hermes cron registration. A hash that looks like a pin but
cannot be resolved to a path is an explicit ``UNRESOLVED_PIN`` finding; it can
never disappear into a false clean report.

One-time transaction scripts are classified as historical records, not live
enforcement. Their original pins remain byte-preserved and any mismatch is
reported separately without making the current fleet fail.

Accepted bytes are content-addressed under ``reports/ops/pin-archive`` so a
future drift can produce a true diff instead of a provenance guess.

Exit codes: 0 clean, 1 drift/missing target, 2 unresolved/unreadable scan input.
"""
from __future__ import annotations

import argparse
import ast
import datetime as dt
import difflib
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator

BUSINESS = Path("/home/ubuntu/business")
HERMES_SCRIPTS = Path("/home/ubuntu/.hermes/scripts")
CRON_REGISTRY = Path("/home/ubuntu/.hermes/cron/jobs.json")
PIN_ARCHIVE = BUSINESS / "reports/ops/pin-archive/sha256"
PYTHON_PIN_ROOTS = (BUSINESS / "scripts", BUSINESS / "ops", BUSINESS / "systems")
PYTHON_SCALAR_PIN_ROOTS = (HERMES_SCRIPTS,)
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# These scripts are immutable records of one completed transaction. Updating
# their pins would falsify what that transaction verified at the time. This is
# deliberately explicit: a new source is live/blocking until reviewed and named
# here, so historical classification can never happen from a filename heuristic.
HISTORICAL_RECORD_SOURCES = frozenset({
    (BUSINESS / "scripts/apply_glp_go2_v6_certification.py").resolve(),
    (BUSINESS / "scripts/glp_item_b_worker_activation_20260810.py").resolve(),
    (BUSINESS / "scripts/session_db_offline_transaction_v2.py").resolve(),
    (BUSINESS / "scripts/correct_glp_successor_approved_denominator_20260802.py").resolve(),
})

#: Python modules carrying a pin table, and the dict name to read.
PYTHON_PIN_SOURCES: tuple[tuple[Path, str], ...] = (
    (BUSINESS / "scripts/glp_awin_portal_drain_cron.py", "EXECUTOR_SOURCE_PINS"),
    (BUSINESS / "scripts/glp_awin_portal_drain_cron.py", "STATIC_AUTHORITY_PINS"),
    (BUSINESS / "scripts/glp_awin_portal_drain_cron.py", "OPERATOR_RULING_PINS"),
)

# Retained for the deliberately narrow Awin release transaction. The reporter's
# default fleet scope scans every enabled registration.
AWIN_CRON_JOB_IDS = frozenset({"52e2014a9579", "7a269c665b12", "5a2cba16e057"})


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mtime(path: Path) -> str:
    stamp = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
    return stamp.isoformat(timespec="seconds").replace("+00:00", "Z")


def _literal_path(
    node: ast.expr,
    *,
    module: Path | None = None,
    environment: dict[str, Path] | None = None,
) -> Path | None:
    """Resolve static pathlib expressions without importing their module."""
    environment = environment or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return Path(node.value)
    if isinstance(node, ast.Name):
        if node.id == "BUSINESS":
            return environment.get(node.id, BUSINESS)
        if node.id == "__file__" and module is not None:
            return module
        return environment.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _literal_path(node.left, module=module, environment=environment)
        right = _literal_path(node.right, module=module, environment=environment)
        if left is None or right is None:
            return None
        return left / right
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "Path" and len(node.args) == 1:
            return _literal_path(node.args[0], module=module, environment=environment)
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "home"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "Path"
            and not node.args
        ):
            return Path.home()
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"resolve", "absolute"}:
            return _literal_path(node.func.value, module=module, environment=environment)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        value = _literal_path(node.value, module=module, environment=environment)
        return None if value is None else value.parent
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
    ):
        value = _literal_path(node.value.value, module=module, environment=environment)
        if value is not None and node.slice.value >= 0:
            try:
                return value.parents[node.slice.value]
            except IndexError:
                return None
    return None


def _assignment_name(node: ast.stmt) -> str | None:
    if isinstance(node, ast.AnnAssign):
        return getattr(node.target, "id", None)
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        return getattr(node.targets[0], "id", None)
    return None


def _path_environment(tree: ast.Module, module: Path) -> dict[str, Path]:
    environment: dict[str, Path] = {"BUSINESS": BUSINESS, "__file__": module}
    for _pass in range(8):
        changed = False
        for node in tree.body:
            name = _assignment_name(node)
            value = getattr(node, "value", None)
            if name is None or value is None:
                continue
            resolved = _literal_path(value, module=module, environment=environment)
            if resolved is not None and environment.get(name) != resolved:
                environment[name] = resolved
                changed = True
        if not changed:
            break
    return environment


def python_pins(module: Path, dict_name: str) -> Iterator[tuple[str, Path, str]]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    environment = _path_environment(tree, module)
    for node in tree.body:
        target = _assignment_name(node)
        if target != dict_name or not isinstance(node.value, ast.Dict):
            continue
        for key, value in zip(node.value.keys, node.value.values):
            path = (
                _literal_path(key, module=module, environment=environment)
                if key is not None else None
            )
            if path is None or not isinstance(value, ast.Constant):
                continue
            if isinstance(value.value, str) and HEX64.fullmatch(value.value):
                yield f"{module.name}:{dict_name}", path, value.value


def _annotation_is_path_dict(node: ast.stmt) -> bool:
    if not isinstance(node, ast.AnnAssign):
        return False
    rendered = ast.unparse(node.annotation).replace(" ", "")
    return "dict[Path,str]" in rendered or "Dict[Path,str]" in rendered


def _python_module_inventory(
    module: Path,
) -> tuple[list[tuple[str, Path, str]], list[dict[str, Any]]]:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    environment = _path_environment(tree, module)
    pins: list[tuple[str, Path, str]] = []
    issues: list[dict[str, Any]] = []
    for node in tree.body:
        name = _assignment_name(node)
        value = getattr(node, "value", None)
        if name is None or not isinstance(value, ast.Dict):
            continue
        rows: list[tuple[ast.expr | None, str]] = []
        for key, raw_digest in zip(value.keys, value.values):
            if (
                isinstance(raw_digest, ast.Constant)
                and isinstance(raw_digest.value, str)
                and HEX64.fullmatch(raw_digest.value)
            ):
                rows.append((key, raw_digest.value))
        if not rows:
            continue
        resolved_rows: list[tuple[Path, str]] = []
        unresolved_rows: list[tuple[ast.expr | None, str]] = []
        for key, digest in rows:
            path = (
                _literal_path(key, module=module, environment=environment)
                if key is not None else None
            )
            if path is not None and path.is_absolute():
                resolved_rows.append((path, digest))
            else:
                unresolved_rows.append((key, digest))
        pin_table = bool(resolved_rows) or _annotation_is_path_dict(node)
        if not pin_table:
            continue
        source = f"{module.name}:{name}"
        pins.extend((source, path, digest) for path, digest in resolved_rows)
        for key, digest in unresolved_rows:
            issues.append({
                "source": source,
                "path": str(module.resolve()),
                "state": "UNRESOLVED_PIN",
                "expected": digest,
                "unresolved_target": ast.unparse(key) if key is not None else "**mapping",
            })
    return pins, issues


def _scalar_digest_base(name: str) -> str | None:
    upper = name.upper()
    if upper.startswith("EXPECTED_") and upper.endswith("_SHA256"):
        return upper[len("EXPECTED_"):-len("_SHA256")]
    if upper.endswith("_EXPECTED_SHA256"):
        return upper[:-len("_EXPECTED_SHA256")]
    if upper.endswith("_SHA256"):
        return upper[:-len("_SHA256")]
    return None


def _python_scalar_inventory(
    module: Path,
) -> tuple[list[tuple[str, Path, str]], list[dict[str, Any]]]:
    """Read module-level scalar Path/hash pairs without importing the module."""
    tree = ast.parse(module.read_text(encoding="utf-8"))
    environment = _path_environment(tree, module)
    assignments = {
        name: getattr(node, "value")
        for node in tree.body
        if (name := _assignment_name(node)) is not None
        and getattr(node, "value", None) is not None
    }
    pins: list[tuple[str, Path, str]] = []
    issues: list[dict[str, Any]] = []
    for digest_name, value in assignments.items():
        if not (
            isinstance(value, ast.Constant)
            and isinstance(value.value, str)
            and HEX64.fullmatch(value.value)
        ):
            continue
        base = _scalar_digest_base(digest_name)
        if base is None:
            continue
        candidate_names = {
            base,
            f"{base}_PATH",
            f"{base}_TARGET",
            f"{base}_SOURCE",
            f"{base}_EXECUTABLE",
            f"{base}_FILE",
        }
        paired = [
            (name, node) for name, node in assignments.items()
            if name.upper() in candidate_names
        ]
        # A standalone digest can be a tree hash, receipt hash, or identifier.
        # The ninth convention is specifically a digest paired by name with a
        # module-level Path scalar, so unrelated digest constants are not pins.
        if not paired:
            continue
        resolved = [
            (name, path)
            for name, node in paired
            if (path := _literal_path(
                node, module=module, environment=environment)) is not None
            and path.is_absolute()
        ]
        source = f"{module.name}:{digest_name}"
        if len(resolved) == 1:
            pins.append((source, resolved[0][1], value.value))
            continue
        issues.append({
            "source": source,
            "path": str(module.resolve()),
            "state": "UNRESOLVED_PIN",
            "expected": value.value,
            "candidate_targets": sorted(
                f"{name}={ast.unparse(node)}" for name, node in paired),
            "reason": "path_not_found" if not resolved else "path_ambiguous",
        })
    return pins, issues


def _python_fleet_inventory() -> tuple[
    list[tuple[str, Path, str]],
    list[dict[str, Any]],
    list[tuple[str, Path, str]],
    list[dict[str, Any]],
]:
    pins: list[tuple[str, Path, str]] = []
    issues: list[dict[str, Any]] = []
    historical_pins: list[tuple[str, Path, str]] = []
    historical_issues: list[dict[str, Any]] = []
    ignored_parts = {".git", ".venv", "node_modules", "__pycache__"}
    # Preserve explicitly registered tables as first-class fleet inputs. This
    # matters for library callers that override the source list (and for tables
    # that may live outside the broad roots); the final discovery layer dedupes
    # tables also found by the root walk.
    for module, dict_name in PYTHON_PIN_SOURCES:
        if not module.is_file():
            continue
        try:
            module_pins = list(python_pins(module, dict_name))
        except (OSError, SyntaxError) as exc:
            target_issues = (
                historical_issues
                if module.resolve() in HISTORICAL_RECORD_SOURCES
                else issues
            )
            target_issues.append({
                "source": f"python:{module.name}:{dict_name}",
                "path": str(module.resolve()),
                "state": "UNREADABLE",
                "expected": "registered-pin-table-scan",
                "error": f"{type(exc).__name__}:{exc}",
            })
            continue
        target_pins = (
            historical_pins
            if module.resolve() in HISTORICAL_RECORD_SOURCES
            else pins
        )
        target_pins.extend(module_pins)
    for root in PYTHON_PIN_ROOTS:
        if not root.is_dir():
            continue
        for module in sorted(root.rglob("*.py")):
            if any(part in ignored_parts for part in module.parts):
                continue
            try:
                text = module.read_text(encoding="utf-8")
                if not re.search(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", text):
                    continue
                module_pins, module_issues = _python_module_inventory(module)
            except OSError as exc:
                issues.append({
                    "source": f"python:{module.name}",
                    "path": str(module.resolve()),
                    "state": "UNREADABLE",
                    "expected": "pin-table-scan",
                    "error": f"{type(exc).__name__}:{exc}",
                })
                continue
            except SyntaxError as exc:
                pin_table_shape = bool(re.search(
                    r"dict\s*\[\s*Path\s*,\s*str\s*\]|"
                    r"(?m)^[ \t]*[A-Za-z_][A-Za-z0-9_]*(?:PIN|EXPECTED|SHA256)"
                    r"[A-Za-z0-9_]*[ \t]*(?::[^=]+)?=[ \t]*\{",
                    text,
                ))
                if pin_table_shape:
                    issues.append({
                        "source": f"python:{module.name}",
                        "path": str(module.resolve()),
                        "state": "UNREADABLE",
                        "expected": "pin-table-scan",
                        "error": f"SyntaxError:{exc}",
                    })
                continue
            if module.resolve() in HISTORICAL_RECORD_SOURCES:
                historical_pins.extend(module_pins)
                historical_issues.extend(module_issues)
            else:
                pins.extend(module_pins)
                issues.extend(module_issues)
    for root in PYTHON_SCALAR_PIN_ROOTS:
        if not root.is_dir():
            continue
        for module in sorted(root.rglob("*.py")):
            if any(part in ignored_parts for part in module.parts):
                continue
            try:
                text = module.read_text(encoding="utf-8")
                if not re.search(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", text):
                    continue
                module_pins, module_issues = _python_scalar_inventory(module)
            except OSError as exc:
                issues.append({
                    "source": f"python:{module.name}",
                    "path": str(module.resolve()),
                    "state": "UNREADABLE",
                    "expected": "scalar-pin-scan",
                    "error": f"{type(exc).__name__}:{exc}",
                })
                continue
            except SyntaxError as exc:
                if re.search(r"(?m)^\s*[A-Za-z_][A-Za-z0-9_]*SHA256\s*=", text):
                    issues.append({
                        "source": f"python:{module.name}",
                        "path": str(module.resolve()),
                        "state": "UNREADABLE",
                        "expected": "scalar-pin-scan",
                        "error": f"SyntaxError:{exc}",
                    })
                continue
            pins.extend(module_pins)
            issues.extend(module_issues)
    return pins, issues, historical_pins, historical_issues


SHELL_ASSIGN_RE = re.compile(
    r"^[ \t]*(?:readonly[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=([^\r\n]+?)[ \t]*$", re.M)
SHELL_HASH_ASSIGN_RE = re.compile(
    r"^[ \t]*(?:readonly[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)="
    r"[\"']?([0-9a-f]{64})[\"']?[ \t]*$", re.M)
SHELL_ASSOC_RE = re.compile(
    r"^[ \t]*\[[\"']([^\"']+)[\"']\][ \t]*=[\"']([0-9a-f]{64})[\"'][ \t]*$", re.M)
SHELL_HASH_CALL_RE = re.compile(
    r"(?:sha256sum|hash_file)[ \t]+(?:--[ \t]+)?"
    r"(?P<target>\"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?\"|"
    r"'\$\{?[A-Za-z_][A-Za-z0-9_]*\}?'|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?|"
    r"\"[^\"]+\"|'[^']+'|[^ \t|;&)]+)",
)
SHELL_VAR_COMPARE_RE = re.compile(
    r"[\"']?\$\{?(?P<left>[A-Za-z_][A-Za-z0-9_]*)\}?[\"']?[ \t]*"
    r"(?:==|!=)[ \t]*[\"']?\$\{?(?P<right>[A-Za-z_][A-Za-z0-9_]*)\}?[\"']?"
)
SHELL_EXPANSION_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _shell_value(raw: str, variables: dict[str, str]) -> str | None:
    value = raw.strip()
    if value.endswith(";"):
        value = value[:-1].rstrip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if "$(" in value or "`" in value or ":-" in value or ":+" in value:
        return None
    for _pass in range(12):
        unresolved = False

        def replace(match: re.Match[str]) -> str:
            nonlocal unresolved
            key = match.group(1) or match.group(2)
            if key not in variables:
                unresolved = True
                return match.group(0)
            return variables[key]

        next_value = SHELL_EXPANSION_RE.sub(replace, value)
        value = next_value
        if "$" not in value:
            return value
        if unresolved:
            return None
    return None


def _shell_variables(text: str) -> tuple[dict[str, str], dict[str, str]]:
    raw_variables = {name: raw for name, raw in SHELL_ASSIGN_RE.findall(text)}
    variables: dict[str, str] = {}
    for _pass in range(12):
        changed = False
        for name, raw in raw_variables.items():
            value = _shell_value(raw, variables)
            if value is not None and variables.get(name) != value:
                variables[name] = value
                changed = True
        if not changed:
            break
    return variables, raw_variables


def _shell_expected_base(name: str) -> str | None:
    lowered = name.lower()
    if lowered.startswith("actual_"):
        return None
    if lowered.endswith("_expected_sha256"):
        return lowered.removesuffix("_expected_sha256")
    if lowered == "expected_sha256":
        return ""
    if lowered.startswith("expected_") and lowered.endswith("_sha256"):
        return lowered[len("expected_"):-len("_sha256")]
    if lowered.endswith("_sha256"):
        return lowered.removesuffix("_sha256")
    return None


def _shell_target(
    token: str,
    *,
    variables: dict[str, str],
    cwd: Path | None,
) -> Path | None:
    value = token.strip().strip("\"").strip("'")
    if value.startswith("--") or value in {"$1", "${1}"}:
        return None
    if value.startswith("$"):
        key = value.removeprefix("${").removesuffix("}").removeprefix("$")
        value = variables.get(key, "")
    else:
        expanded = _shell_value(value, variables)
        if expanded is not None:
            value = expanded
    if not value or "$" in value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return None if cwd is None else cwd / path


def _shell_inventory(
    script: Path,
) -> tuple[list[tuple[str, Path, str]], list[dict[str, Any]]]:
    text = script.read_text(encoding="utf-8", errors="replace")
    variables, raw_variables = _shell_variables(text)
    cwd: Path | None = None
    for match in re.finditer(r"^[ \t]*cd[ \t]+([^ \t\r\n;&]+)", text, re.M):
        candidate = _shell_target(match.group(1), variables=variables, cwd=None)
        if candidate is not None:
            cwd = candidate

    actual_targets: dict[str, list[Path]] = {}
    for name, raw in raw_variables.items():
        targets = [
            target for match in SHELL_HASH_CALL_RE.finditer(raw)
            if (target := _shell_target(
                match.group("target"), variables=variables, cwd=cwd)) is not None
        ]
        if targets:
            actual_targets[name] = targets

    comparisons: dict[str, list[str]] = {}
    for match in SHELL_VAR_COMPARE_RE.finditer(text):
        left, right = match.group("left"), match.group("right")
        if _shell_expected_base(left) is not None:
            comparisons.setdefault(left, []).append(right)
        if _shell_expected_base(right) is not None:
            comparisons.setdefault(right, []).append(left)

    pins: list[tuple[str, Path, str]] = []
    issues: list[dict[str, Any]] = []
    covered_digests: set[str] = set()
    for name, digest in SHELL_HASH_ASSIGN_RE.findall(text):
        base = _shell_expected_base(name)
        if base is None:
            continue
        covered_digests.add(digest)
        candidates: set[Path] = set()
        alias_names = {
            f"expected_{base}_target",
        }
        variable_names = {
            base,
            f"{base}_path",
            f"{base}_target",
        }
        alias_candidates: set[Path] = set()
        for variable_name, value in variables.items():
            lowered_variable = variable_name.lower()
            if lowered_variable in alias_names | variable_names:
                target = _shell_target(value, variables=variables, cwd=cwd)
                if target is not None:
                    if lowered_variable in alias_names:
                        alias_candidates.add(target)
                    else:
                        candidates.add(target)
        if alias_candidates:
            candidates = alias_candidates
        for actual_name in comparisons.get(name, []):
            candidates.update(actual_targets.get(actual_name, []))
        for actual_name, targets in actual_targets.items():
            lowered = actual_name.lower()
            if (base and lowered.startswith(f"actual_{base}")) or (
                not base and lowered == "actual_sha256"
            ):
                candidates.update(targets)
        for line in text.splitlines():
            if not re.search(rf"\$\{{?{re.escape(name)}\}}?\b", line):
                continue
            for match in SHELL_HASH_CALL_RE.finditer(line):
                target = _shell_target(match.group("target"), variables=variables, cwd=cwd)
                if target is not None:
                    candidates.add(target)
        source = f"{script.name}:{name}"
        if len(candidates) == 1:
            pins.append((source, candidates.pop(), digest))
        else:
            issues.append({
                "source": source,
                "path": str(script.resolve()),
                "state": "UNRESOLVED_PIN",
                "expected": digest,
                "candidate_targets": sorted(map(str, candidates)),
                "reason": "path_not_found" if not candidates else "path_ambiguous",
            })

    for raw_path, digest in SHELL_ASSOC_RE.findall(text):
        covered_digests.add(digest)
        path = _shell_target(raw_path, variables=variables, cwd=cwd)
        source = f"{script.name}:associative_pin[{raw_path}]"
        if path is None:
            issues.append({
                "source": source,
                "path": str(script.resolve()),
                "state": "UNRESOLVED_PIN",
                "expected": digest,
                "unresolved_target": raw_path,
            })
        else:
            pins.append((source, path, digest))

    for line_number, line in enumerate(text.splitlines(), start=1):
        for digest in re.findall(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", line):
            if digest in covered_digests:
                continue
            if "sha256" not in line.lower() and "hash_file" not in line:
                continue
            issues.append({
                "source": f"{script.name}:line-{line_number}",
                "path": str(script.resolve()),
                "state": "UNRESOLVED_PIN",
                "expected": digest,
                "unresolved_target": line.strip()[:200],
            })
    return pins, issues


def shell_pins(script: Path) -> Iterator[tuple[str, Path, str]]:
    pins, _issues = _shell_inventory(script)
    yield from pins


def cron_registry_pins(
    registry: Path = CRON_REGISTRY,
    *,
    job_ids: frozenset[str] | None = None,
) -> Iterator[tuple[str, Path, str]]:
    document = json.loads(registry.read_text(encoding="utf-8"))
    for job in document.get("jobs", []):
        job_id = str(job.get("id") or "")
        if not job.get("enabled") or (job_ids is not None and job_id not in job_ids):
            continue
        raw = Path(str(job.get("script") or ""))
        path = raw if raw.is_absolute() else HERMES_SCRIPTS / raw.name
        expected = str(job.get("script_sha256") or "")
        if re.fullmatch(r"[0-9a-f]{64}", expected):
            yield f"jobs.json:{job_id}.script_sha256", path, expected


def archive_path(digest: str) -> Path:
    return PIN_ARCHIVE / digest[:2] / f"{digest}.bytes"


def _discover(
    *,
    include_shell: bool,
    include_registry: bool,
    registry: Path,
    scope: str,
) -> tuple[
    list[tuple[str, Path, str]],
    list[dict[str, Any]],
    list[tuple[str, Path, str]],
    list[dict[str, Any]],
]:
    if scope not in {"fleet", "awin"}:
        raise ValueError(f"unknown pin scope: {scope}")
    sources: list[tuple[str, Path, str]] = []
    issues: list[dict[str, Any]] = []
    historical_sources: list[tuple[str, Path, str]] = []
    historical_issues: list[dict[str, Any]] = []
    if scope == "fleet":
        (
            python_sources,
            python_issues,
            historical_sources,
            historical_issues,
        ) = _python_fleet_inventory()
        sources.extend(python_sources)
        issues.extend(python_issues)
    else:
        for module, dict_name in PYTHON_PIN_SOURCES:
            if module.is_file():
                sources.extend(python_pins(module, dict_name))
    if include_shell and HERMES_SCRIPTS.is_dir():
        for script in sorted(HERMES_SCRIPTS.glob("*.sh")):
            try:
                shell_sources, shell_issues = _shell_inventory(script)
            except OSError as exc:
                issues.append({
                    "source": f"shell:{script.name}", "path": str(script.resolve()),
                    "state": "UNREADABLE", "expected": "shell-pin-scan", "error": str(exc),
                })
                continue
            if scope == "awin":
                shell_sources = [
                    row for row in shell_sources
                    if row[0].rsplit(":", 1)[-1].endswith("_SHA256")
                    and row[0].rsplit(":", 1)[-1].upper() == row[0].rsplit(":", 1)[-1]
                ]
                shell_issues = []
            sources.extend(shell_sources)
            issues.extend(shell_issues)
    if include_registry and registry.is_file():
        sources.extend(cron_registry_pins(
            registry, job_ids=AWIN_CRON_JOB_IDS if scope == "awin" else None))
        if scope == "fleet":
            document = json.loads(registry.read_text(encoding="utf-8"))
            for job in document.get("jobs", []):
                if not job.get("enabled") or "script_sha256" not in job:
                    continue
                expected = str(job.get("script_sha256") or "")
                if expected and not HEX64.fullmatch(expected):
                    issues.append({
                        "source": f"jobs.json:{job.get('id')}.script_sha256",
                        "path": str(registry.resolve()),
                        "state": "UNRESOLVED_PIN",
                        "expected": expected or "<empty>",
                        "reason": "registration_hash_invalid",
                    })
    unique: list[tuple[str, Path, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for source, path, expected in sources:
        row = (source, str(path), expected)
        if row not in seen:
            seen.add(row)
            unique.append((source, path, expected))
    unique_historical: list[tuple[str, Path, str]] = []
    historical_seen: set[tuple[str, str, str]] = set()
    for source, path, expected in historical_sources:
        row = (source, str(path), expected)
        if row not in historical_seen:
            historical_seen.add(row)
            unique_historical.append((source, path, expected))
    return unique, issues, unique_historical, historical_issues


def discover_pins(
    *,
    include_shell: bool,
    include_registry: bool = True,
    registry: Path = CRON_REGISTRY,
    scope: str = "fleet",
) -> list[tuple[str, Path, str]]:
    sources, _issues, _historical_sources, _historical_issues = _discover(
        include_shell=include_shell, include_registry=include_registry,
        registry=registry, scope=scope)
    return sources


def discover_historical_pins(
    *,
    include_shell: bool = True,
    include_registry: bool = True,
    registry: Path = CRON_REGISTRY,
    scope: str = "fleet",
) -> list[tuple[str, Path, str]]:
    _sources, _issues, historical_sources, _historical_issues = _discover(
        include_shell=include_shell, include_registry=include_registry,
        registry=registry, scope=scope)
    return historical_sources


def _evaluate(
    sources: list[tuple[str, Path, str]],
    findings: list[dict[str, Any]],
    *,
    classification: str,
    blocking: bool,
) -> list[dict[str, Any]]:
    for item in findings:
        item.setdefault("classification", classification)
        item.setdefault("blocking", blocking)
    for source, path, expected in sources:
        common = {
            "source": source,
            "path": str(path),
            "expected": expected,
            "classification": classification,
            "blocking": blocking,
        }
        if not path.exists():
            findings.append({**common, "state": "MISSING"})
            continue
        if not path.is_file():
            continue
        try:
            actual = sha256_file(path)
        except OSError as exc:
            findings.append({**common, "state": "UNREADABLE", "error": str(exc)})
            continue
        if actual != expected:
            archived = archive_path(expected)
            archive_state = "MISSING"
            if archived.is_file():
                archive_state = (
                    "AVAILABLE" if sha256_file(archived) == expected else "CORRUPT")
            findings.append({
                **common,
                "state": "DRIFT",
                "actual": actual,
                "modified_utc": _mtime(path),
                "archive_state": archive_state,
                "archive_path": str(archived),
            })
    return findings


def scan(include_shell: bool, include_registry: bool = True,
         registry: Path = CRON_REGISTRY, scope: str = "fleet") -> list[dict[str, Any]]:
    sources, findings, _historical_sources, _historical_issues = _discover(
        include_shell=include_shell, include_registry=include_registry,
        registry=registry, scope=scope)
    return _evaluate(
        sources, findings, classification="LIVE_ENFORCEMENT", blocking=True)


def scan_historical_records(
    include_shell: bool = True,
    include_registry: bool = True,
    registry: Path = CRON_REGISTRY,
    scope: str = "fleet",
) -> list[dict[str, Any]]:
    _sources, _issues, historical_sources, historical_issues = _discover(
        include_shell=include_shell, include_registry=include_registry,
        registry=registry, scope=scope)
    return _evaluate(
        historical_sources,
        historical_issues,
        classification="HISTORICAL_RECORD",
        blocking=False,
    )


def text_diff(item: dict[str, Any]) -> str:
    """Return a true unified diff only when accepted preimage bytes exist."""
    if item.get("archive_state") != "AVAILABLE" or item.get("state") != "DRIFT":
        return ""
    archived = Path(str(item["archive_path"]))
    current = Path(str(item["path"]))
    try:
        before = archived.read_text(encoding="utf-8").splitlines(keepends=True)
        after = current.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return ""
    return "".join(difflib.unified_diff(
        before, after, fromfile=f"accepted:{item['expected']}",
        tofile=f"current:{item['actual']}", n=3))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable findings")
    parser.add_argument("--no-shell", action="store_true", help="scan Python pin tables only")
    parser.add_argument("--no-registry", action="store_true", help="omit cron registration pins")
    parser.add_argument("--scope", choices=("fleet", "awin"), default="fleet")
    parser.add_argument("--diff", action="store_true", help="print archived accepted bytes vs current bytes")
    args = parser.parse_args()

    try:
        findings = scan(
            include_shell=not args.no_shell, include_registry=not args.no_registry,
            scope=args.scope)
        historical = scan_historical_records(
            include_shell=not args.no_shell,
            include_registry=not args.no_registry,
            scope=args.scope,
        )
    except Exception as exc:  # a broken scan must never read as "clean"
        print(f"pin drift scan failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        unresolved = sum(
            item["state"] in {"UNRESOLVED_PIN", "UNREADABLE"} for item in findings)
        drifted = sum(item["state"] in {"DRIFT", "MISSING"} for item in findings)
        print(json.dumps({
            "drifted": drifted, "unresolved": unresolved,
            "finding_count": len(findings), "findings": findings,
            "historical_record_count": len(historical),
            "historical_records": historical,
            "historical_source_modules": sorted(
                str(path) for path in HISTORICAL_RECORD_SOURCES),
        }, indent=2, sort_keys=True))
        return 2 if unresolved else (1 if findings else 0)

    if not findings:
        print("all hash pins match")
        if historical:
            print(f"{len(historical)} non-blocking historical record mismatch(es) reported separately")
        return 0
    unresolved = sum(
        item["state"] in {"UNRESOLVED_PIN", "UNREADABLE"} for item in findings)
    drifted = sum(item["state"] in {"DRIFT", "MISSING"} for item in findings)
    print(f"{drifted} pin(s) drifted/missing; {len(findings)} total finding(s), "
          f"{unresolved} unresolved/unreadable\n")
    for item in findings:
        print(f"{item['state']}: {item['path']}")
        print(f"   pinned by  {item['source']}")
        print(f"   expected   {item['expected']}")
        if item.get("actual"):
            print(f"   on disk    {item['actual']}")
        if item.get("modified_utc"):
            print(f"   modified   {item['modified_utc']}")
        if item.get("archive_state"):
            print(f"   preimage   {item['archive_state']} {item['archive_path']}")
        if args.diff:
            diff = text_diff(item)
            print(diff if diff else "   diff       unavailable (no accepted text preimage)")
        print()
    print("Re-pinning is deliberate: establish what changed and why it was authorised "
          "before updating any pin.")
    if historical:
        print(f"{len(historical)} historical record mismatch(es) are non-blocking and were not re-pinned.")
    return 2 if unresolved else 1


if __name__ == "__main__":
    sys.exit(main())
