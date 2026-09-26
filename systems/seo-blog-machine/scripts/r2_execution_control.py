#!/usr/bin/env python3
"""Pin only the R2 build/validation surface, not mutable prioritization code."""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

ASSIGNMENTS = frozenset({
    "WEEKLY_TIER_UNITS",
    "R2_MINIMUM_DELTA",
    "R2_ROUTE_GOVERNANCE_FIELDS",
    "META_FIELDS",
    "AUTHORITATIVE_HOSTS",
})
FUNCTIONS = frozenset({
    "canonical_route",
    "validate_r2_execution_contract",
    "_substantive_projection",
    "substantive_delta_fraction",
    "_section_key",
    "_section_text",
    "_query_answering_sections",
    "_source_identity",
    "_authoritative",
    "_routes_in",
    "validate_r2_refresh",
    "preserve_r2_route_governance",
    "replace_article_and_ownership",
})
IMPORTS = frozenset({
    ("import", "copy", None),
    ("import", "json", None),
    ("import", "re", None),
    ("from", "collections", "Counter"),
    ("from", "urllib.parse", "urlsplit"),
})


def _assignment_name(node: ast.Assign | ast.AnnAssign) -> str | None:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    if len(targets) != 1 or not isinstance(targets[0], ast.Name):
        return None
    return targets[0].id


def _selected_imports(node: ast.Import | ast.ImportFrom) -> list[ast.stmt]:
    selected: list[ast.stmt] = []
    if isinstance(node, ast.Import):
        for alias in node.names:
            if ("import", alias.name, alias.asname) in IMPORTS:
                selected.append(ast.Import(names=[alias]))
    else:
        for alias in node.names:
            if ("from", node.module, alias.name) in IMPORTS and alias.asname is None:
                selected.append(ast.ImportFrom(module=node.module, names=[alias], level=node.level))
    return selected


def execution_surface_sha256(path: Path) -> str:
    """Hash the exact AST that can affect R2 build validation/replacement.

    Selection ranking, cadence planning, CLI wiring, comments, and unrelated
    imports deliberately do not participate. Any executable R2 gate or mutation
    change still changes this digest and fails closed at publication.
    """
    source = path.resolve(strict=True)
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    body: list[ast.stmt] = []
    found_assignments: set[str] = set()
    found_functions: set[str] = set()
    found_imports: set[tuple[str, str, str | None]] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imports = _selected_imports(node)
            body.extend(imports)
            for selected in imports:
                if isinstance(selected, ast.Import):
                    alias = selected.names[0]
                    found_imports.add(("import", alias.name, alias.asname))
                else:
                    found_imports.add(("from", selected.module or "", selected.names[0].name))
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            name = _assignment_name(node)
            if name in ASSIGNMENTS:
                if name in found_assignments:
                    raise RuntimeError(f"r2_execution_surface_duplicate_assignment:{name}")
                found_assignments.add(name)
                body.append(node)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS:
            if node.name in found_functions:
                raise RuntimeError(f"r2_execution_surface_duplicate_function:{node.name}")
            found_functions.add(node.name)
            body.append(node)
    missing = {
        "assignments": sorted(ASSIGNMENTS - found_assignments),
        "functions": sorted(FUNCTIONS - found_functions),
        "imports": sorted(IMPORTS - found_imports),
    }
    if any(missing.values()):
        raise RuntimeError(f"r2_execution_surface_incomplete:{missing}")
    normalized = ast.dump(
        ast.Module(body=body, type_ignores=[]),
        annotate_fields=True,
        include_attributes=False,
    ).encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def surface_spec(path: Path) -> dict[str, str]:
    source = path.resolve(strict=True)
    return {"path": str(source), "surface_sha256": execution_surface_sha256(source)}


def require_surface(spec: Any, path: Path) -> Path:
    expected_path = path.resolve(strict=True)
    if not isinstance(spec, dict) or spec.get("path") != str(expected_path):
        raise RuntimeError("r2_execution_surface_spec_invalid")
    expected = spec.get("surface_sha256")
    if not isinstance(expected, str) or len(expected) != 64:
        raise RuntimeError("r2_execution_surface_spec_invalid")
    actual = execution_surface_sha256(expected_path)
    if actual != expected:
        raise RuntimeError(
            f"r2_execution_surface_drift:{expected_path}:expected={expected}:actual={actual}"
        )
    return expected_path
