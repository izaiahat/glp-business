#!/usr/bin/env python3
"""Fable's /go anchor taxonomy, and the migration-ordering defect that hid it.

Two things are pinned here.

1. ORDERING. _ensure_go_runtime_promotion_contract_support runs on EVERY deploy,
   including a providers-only one that consumes no /go request, and it extends
   `deriveGoEntriesFromRegistry(... , affiliateMap = {})` to
   `(... , affiliateMap = {}, goPromotionAuthority = {})`. Two probes in
   _ensure_first_party_go_contract_support ended in the call's own closing
   paren, so once round 16 sealed a promotion-migrated release as the base the
   probes stopped matching, the stamp-only path was skipped, the whole-function
   replacement was attempted, and it failed closed with
   go_route_contract_source_drift. /go provisioning could never be applied
   again. The fix must keep working against a release that already carries the
   promotion migration - which is exactly what the fixture below is.

2. TAXONOMY (Fable, design + attribution authority, 2026-09-02). Page roles and
   positions are the TRUE page, never borrowed, because cs_click_id joins
   revenue by page_slug and review-page clicks must stay distinguishable from
   compare-grid clicks. The anchor form is the runtime's:
   ?s=<pageSlug>.<position>&v=<variant>. A bare /go/<slug> is invalid everywhere.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import glp_affiliate_wire_backlog_worker as worker  # noqa: E402

RELEASES = ROOT / "site-system/flagship-v2/releases"
WORKER_SRC = (ROOT / "scripts/glp_affiliate_wire_backlog_worker.py").read_text()

MIGRATED = (
    "src/program-registry.mjs",
    "src/ready6-editorial.mjs",
    "src/approved-program-coverage.mjs",
    "scripts/regenerate-go-runtime-manifest.mjs",
    "scripts/build-staging.mjs",
    "scripts/validate-ready6-editorial.mjs",
    "scripts/validate-ready6-static.py",
)


def _promotion_migrated_release() -> Path:
    """Newest release whose program-registry carries the promotion migration."""
    for release in sorted(RELEASES.glob("glp-b2-wire-backlog-*"), reverse=True):
        registry = release / "src/program-registry.mjs"
        if not registry.is_file():
            continue
        text = registry.read_text()
        if "goPromotionAuthority" in text and "GO_ROUTE_PROVISION_CONTRACT_V1" not in text:
            return release
    pytest.skip("no promotion-migrated, first-party-unmigrated release on disk")


@pytest.fixture(scope="module")
def migrated_tree():
    release = _promotion_migrated_release()
    tmp = Path(tempfile.mkdtemp(prefix="go-taxonomy-"))
    try:
        for relative in MIGRATED:
            source = release / relative
            if source.is_file():
                (tmp / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, tmp / relative)
        result = worker._ensure_first_party_go_contract_support(tmp)
        yield tmp, result
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_first_party_migration_applies_over_the_promotion_migration(migrated_tree):
    _, result = migrated_tree
    assert result["status"] == "PASS_SUPPORTED"
    assert "src/program-registry.mjs" in result["changed_files"]
    assert "scripts/build-staging.mjs" in result["changed_files"]


def test_every_migrated_file_still_parses(migrated_tree):
    tree, _ = migrated_tree
    for relative in MIGRATED:
        path = tree / relative
        if not path.is_file():
            continue
        cmd = (["node", "--check", str(path)] if path.suffix == ".mjs"
               else [sys.executable, "-m", "py_compile", str(path)])
        done = subprocess.run(cmd, capture_output=True, text=True)
        assert done.returncode == 0, f"{relative}: {done.stderr[:400]}"


def test_migration_is_idempotent(migrated_tree):
    tree, _ = migrated_tree
    again = worker._ensure_first_party_go_contract_support(tree)
    assert again["status"] == "PASS_ALREADY_SUPPORTED"
    assert again["changed_files"] == []


def test_probes_do_not_depend_on_the_calls_closing_paren():
    """The exact shape of the ordering defect, so it cannot be reintroduced."""
    assert 'affiliateMap = {})",' not in WORKER_SRC
    assert 'loadAffiliateLinkMap("compare"))\' in text' not in WORKER_SRC


def test_fable_taxonomy_page_roles_and_positions():
    assert '"providers", "reviews", "self-pay", "lp"' in WORKER_SRC
    # rv1/rv2 stay literals. sp1 and lp1 were literals under the first taxonomy
    # and are now produced by R4's indexed patterns instead - superseded, not
    # dropped, and asserted as patterns in the R4 tests below.
    for position in ("rv1", "rv2"):
        assert f'"{position}"' in WORKER_SRC, position
    for pattern in ("`lp${index + 1}`", "`sp${index + 1}`"):
        assert pattern in WORKER_SRC, pattern


def test_review_page_cta_renders_the_queried_reviews_anchor(migrated_tree):
    tree, _ = migrated_tree
    editorial = (tree / "src/ready6-editorial.mjs").read_text()
    assert "?s=reviews.rv1&v=" in editorial
    validator = (tree / "scripts/validate-ready6-static.py").read_text()
    assert "?s=reviews.rv1&v=" in validator


def test_a_bare_go_route_is_not_accepted_by_either_validator(migrated_tree):
    tree, _ = migrated_tree
    for relative in ("src/ready6-editorial.mjs", "scripts/validate-ready6-static.py"):
        text = (tree / relative).read_text()
        # No surviving expectation that a route may end at the slug.
        assert '`${firstPartyGoRoute}`' not in text, relative
        assert "first_party_go_route'] )" not in text, relative


@pytest.mark.parametrize("anchor", [
    "/go/coivas?s=reviews.rv1&v=a",
    "/go/coivas?s=reviews.rv2&v=a",
    "/go/coivas?s=self-pay.sp1&v=a",
    "/go/coivas?s=lp.lp1&v=b",
    "/go/coivas?s=providers.fv2s1&v=a",
    "/go/coivas?s=reviews.rv1&v=a&cs_click_id=0192f0a1-3b2c-7d4e-8f01-23456789abcd",
])
def test_runtime_parse_go_query_accepts_every_emitted_anchor(anchor):
    release = _promotion_migrated_release()
    worker_mjs = release / "src/worker.mjs"
    if not worker_mjs.is_file():
        pytest.skip("worker.mjs absent")
    script = (
        f'import {{ parseGoQuery }} from {str(worker_mjs)!r};'
        f'const r = parseGoQuery(new URL("https://glpclinicscout.com{anchor}"));'
        'if (!r.pageSlug || !r.linkPosition || !r.variant) throw new Error("incomplete");'
        'console.log("OK", r.pageSlug, r.linkPosition);'
    )
    done = subprocess.run(["node", "--input-type=module", "-e", script],
                          capture_output=True, text=True, cwd=str(release))
    assert done.returncode == 0, f"{anchor}: {done.stderr[:400]}"
    assert done.stdout.startswith("OK")


def test_runtime_parse_go_query_rejects_a_bare_route():
    release = _promotion_migrated_release()
    worker_mjs = release / "src/worker.mjs"
    if not worker_mjs.is_file():
        pytest.skip("worker.mjs absent")
    script = (
        f'import {{ parseGoQuery }} from {str(worker_mjs)!r};'
        'try { parseGoQuery(new URL("https://glpclinicscout.com/go/coivas")); }'
        'catch (error) { console.log("REJECTED", error.message); process.exit(0); }'
        'throw new Error("bare route was accepted");'
    )
    done = subprocess.run(["node", "--input-type=module", "-e", script],
                          capture_output=True, text=True, cwd=str(release))
    assert done.returncode == 0, done.stderr[:400]
    assert done.stdout.startswith("REJECTED")


# --- Fable's third required test -------------------------------------------
# "the runtime parseGoQuery accepts every emitted anchor in the candidate (walk
# the built tree)". A built tree only exists after a build, so this skips until
# one is on disk and then binds every emitted anchor to the real runtime.

def _newest_built_tree() -> Path | None:
    roots = []
    for base in (ROOT / "site-system/flagship-v2/candidates",
                 ROOT / "site-system/flagship-v2/releases"):
        if base.is_dir():
            roots += [p for p in base.glob("glp-b2-wire-backlog-*/public") if p.is_dir()]
    if not roots:
        return None
    return max(roots, key=lambda p: p.stat().st_mtime)


def _emitted_go_anchors(tree: Path) -> list[str]:
    """Anchors as a BROWSER sees them.

    `&` is entity-encoded to `&amp;` inside an HTML attribute, which is correct
    markup - the runtime receives the decoded URL, so the test must decode too
    or it fails every multi-parameter anchor for a defect that does not exist.
    """
    import html
    import re
    pattern = re.compile(r'href="(/go/[^"]*)"')
    found: set[str] = set()
    for page in tree.rglob("index.html"):
        text = page.read_text(encoding="utf-8", errors="replace")
        found.update(html.unescape(match) for match in pattern.findall(text))
    return sorted(found)


def test_every_emitted_go_anchor_in_the_built_tree_parses():
    tree = _newest_built_tree()
    if tree is None:
        pytest.skip("no built tree on disk yet")
    anchors = _emitted_go_anchors(tree)
    if not anchors:
        pytest.skip(f"no /go anchors emitted in {tree}")
    release = _promotion_migrated_release()
    worker_mjs = release / "src/worker.mjs"
    if not worker_mjs.is_file():
        pytest.skip("worker.mjs absent")
    # One node process for the whole tree; it reports the first anchor the
    # runtime refuses, which is the one a human has to look at.
    script = (
        f'import {{ parseGoQuery }} from {str(worker_mjs)!r};'
        'import { readFileSync } from "node:fs";'
        'const anchors = JSON.parse(readFileSync(process.argv[1], "utf8"));'
        'const bad = [];'
        'for (const anchor of anchors) {'
        '  try { parseGoQuery(new URL("https://glpclinicscout.com" + anchor)); }'
        '  catch (error) { bad.push(anchor + " :: " + error.message); }'
        '}'
        'if (bad.length) { console.error(bad.slice(0, 10).join("\\n")); process.exit(1); }'
        'console.log("OK " + anchors.length);'
    )
    import json as _json
    import tempfile as _tempfile
    with _tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
        _json.dump(anchors, handle)
        listing = handle.name
    try:
        done = subprocess.run(["node", "--input-type=module", "-e", script, "--", listing],
                              capture_output=True, text=True, cwd=str(release))
    finally:
        Path(listing).unlink(missing_ok=True)
    assert done.returncode == 0, (
        f"{tree}: the runtime refuses anchors it emitted:\n{done.stderr[:800]}")


# --- R4: indexed positions (Fable Pass 7 P13 / D-3, 2026-09-02) -------------
# Positions must be UNIQUE PER PROGRAM ON A PAGE, so listing surfaces use
# indexed positions the way the providers grid already uses pc<n>:
#   /lp/* and /lp/compare/  -> lp.lp<n>   (never providers.pc<n>)
#   /self-pay/ rows         -> self-pay.sp<n>
#   single-CTA /lp          -> lp.lp1
#   review pages            -> reviews.rv1 / rv2 (unchanged)

def test_r4_positions_are_indexed_patterns_not_bare_literals():
    assert "`lp${index + 1}`" in WORKER_SRC
    assert "`sp${index + 1}`" in WORKER_SRC
    # The old fixed literals are now produced BY the patterns, so they must not
    # also be hardcoded - a duplicate would mean two sources of truth.
    assert '"rv1", "rv2", "sp1", "lp1"' not in WORKER_SRC
    assert '"rv1", "rv2"' in WORKER_SRC


@pytest.mark.parametrize("anchor", [
    "/go/coivas?s=lp.lp1&v=a",
    "/go/coivas?s=lp.lp7&v=a",
    "/go/coivas?s=lp.lp60&v=a",
    "/go/coivas?s=self-pay.sp1&v=a",
    "/go/coivas?s=self-pay.sp12&v=a",
    "/go/coivas?s=reviews.rv1&v=a",
])
def test_r4_runtime_accepts_every_indexed_position(anchor):
    release = _promotion_migrated_release()
    worker_mjs = release / "src/worker.mjs"
    if not worker_mjs.is_file():
        pytest.skip("worker.mjs absent")
    script = (
        f'import {{ parseGoQuery }} from {str(worker_mjs)!r};'
        f'const r = parseGoQuery(new URL("https://glpclinicscout.com{anchor}"));'
        'console.log("OK", r.pageSlug, r.linkPosition);'
    )
    done = subprocess.run(["node", "--input-type=module", "-e", script],
                          capture_output=True, text=True, cwd=str(release))
    assert done.returncode == 0, f"{anchor}: {done.stderr[:300]}"


def test_r4_no_page_slug_position_tuple_maps_to_two_programs():
    """R4's required uniqueness test, run against the live built tree."""
    import html as html_mod
    import re as re_mod
    tree = _newest_built_tree()
    if tree is None:
        pytest.skip("no built tree on disk yet")
    seen: dict[tuple[str, str], set[str]] = {}
    anchor_re = re_mod.compile(r'href="(/go/([a-z0-9-]+)\?[^"]*)"')
    for page in tree.rglob("index.html"):
        text = page.read_text(encoding="utf-8", errors="replace")
        for raw, program in anchor_re.findall(text):
            url = html_mod.unescape(raw)
            match = re_mod.search(r"[?&]s=([^&]+)", url)
            if not match:
                continue
            surface = match.group(1)
            if "." not in surface:
                continue
            page_slug, _, position = surface.partition(".")
            seen.setdefault((page_slug, position), set()).add(program)
    collisions = {key: sorted(v) for key, v in seen.items() if len(v) > 1}
    assert not collisions, f"(page_slug, position) reused across programs: {collisions}"
