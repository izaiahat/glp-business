#!/usr/bin/env python3
"""One contract for candidate-local served-test root assertions.

Both provider wire-ins and R2 article releases evolve the same historical test.
Keeping every accepted predecessor and the one canonical successor here prevents
one machine from emitting a state the other machine cannot consume.
"""
from __future__ import annotations


SERVED_TEST_ROOT_LEGACY = (
    '  assert.ok(ROOT.pathname.endsWith("/glp-visual-ultracode-20260821/"));'
)
SERVED_TEST_ROOT_DYNAMIC = (
    r"  assert.match(ROOT.pathname, /\/site-system\/flagship-v2\/"
    r"(?:candidates|releases)\/[^/]+\/$/);"
)
SERVED_TEST_ROOT_CANONICAL = '''  assert.equal(new URL("../package.json", import.meta.url).pathname, new URL("package.json", ROOT).pathname);
  assert.equal(PUBLIC.pathname, new URL("public/", ROOT).pathname);'''


def normalize_candidate_local_served_test(
    source: str,
    *,
    label: str = "served_test_candidate_root",
) -> str:
    """Normalize exactly one known root assertion and reject mixed/unknown states."""
    counts = {
        "legacy": source.count(SERVED_TEST_ROOT_LEGACY),
        "dynamic": source.count(SERVED_TEST_ROOT_DYNAMIC),
        "new": source.count(SERVED_TEST_ROOT_CANONICAL),
    }
    if counts == {"legacy": 0, "dynamic": 0, "new": 1}:
        return source
    predecessors = (
        (SERVED_TEST_ROOT_LEGACY, counts["legacy"]),
        (SERVED_TEST_ROOT_DYNAMIC, counts["dynamic"]),
    )
    if counts["new"] == 0 and sum(count for _value, count in predecessors) == 1:
        predecessor = next(value for value, count in predecessors if count == 1)
        return source.replace(predecessor, SERVED_TEST_ROOT_CANONICAL, 1)
    raise RuntimeError(
        f"{label}_anchor_invalid:legacy={counts['legacy']}:"
        f"dynamic={counts['dynamic']}:new={counts['new']}"
    )
