"""W6: builder accepts merged site-main successors without weakening gates.

Each excerpt below is copied byte-for-byte from izaiahat/glp-site-candidate
main (7de97fe). The article builder used to require the predecessor anchors
exactly, so any release built on current site main died before npm build
(``staging_article_route_guard_anchor_invalid`` and three fixture anchors).
Each test proves: (1) the merged successor is accepted unchanged, (2) the
predecessor is still upgraded exactly once, (3) an unknown/weakened form still
fails loud.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
# existing_article_updates.py hard-codes the host analytics-brain path; give a
# clean CI checkout its own copy of the same module (test import path only).
ANALYTICS = Path(__file__).resolve().parents[3] / "systems/analytics-brain"


def load(name):
    for extra in (ANALYTICS, SCRIPTS):
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


builder = load("build_opus_article_release")
fixtures = load("carrier_fixture_contracts")

GUIDE_GUARD = '  if (!/^\\/guides\\/[a-z0-9-]+\\/$/.test(article.canonical_path || "")) throw new Error(`guide_canonical_path_invalid:${article.article_id}`);'
BEST_GUARD = '  if (!/^\\/(?:guides|best)\\/[a-z0-9-]+\\/$/.test(article.canonical_path || "")) throw new Error(`article_canonical_path_invalid:${article.article_id}`);'
REVIEWS_GUARD = '  if (!/^\\/(?:guides|best|reviews)\\/[a-z0-9-]+\\/$/.test(article.canonical_path || "")) throw new Error(`article_canonical_path_invalid:${article.article_id}`);'


def test_staging_route_guard_accepts_reviews_successor_and_still_refuses_unknown(monkeypatch):
    seen = []
    real = builder.apply_once_or_already

    def isolated(source, old, new, label):
        seen.append(label)
        if label == "staging_article_route_guard":
            return real(source, old, new, label)
        return source  # Only the route guard is under test here.

    monkeypatch.setattr(builder, "apply_once_or_already", isolated)
    monkeypatch.setattr(builder, "NATIONAL_CONTENT_RELEASE_GATE_PREDECESSOR", "x", raising=False)
    tail = '\n    breadcrumbs: [{ name: "Home", path: "/" }, article.canonical_path.startsWith("/best/") ? { name: "Compare", path: "/best/" } : { name: "Guides", path: "/guides/" }, { name: article.h1, path: article.canonical_path }],\n'
    # Current merged site main: unchanged, not double-patched.
    assert builder.patch_staging_route_contract(REVIEWS_GUARD + tail) == REVIEWS_GUARD + tail
    # Predecessor: upgraded exactly once to the guides|best guard.
    assert builder.patch_staging_route_contract(GUIDE_GUARD + tail).count(BEST_GUARD) == 1
    # Unknown guard (e.g. any-namespace) still fails loud.
    loose = REVIEWS_GUARD.replace("(?:guides|best|reviews)", "[a-z]+")
    with pytest.raises(RuntimeError, match="staging_article_route_guard_anchor_invalid"):
        builder.patch_staging_route_contract(loose + tail)


LASTMOD_SUCCESSOR = '''  for (const directory of ["scripts", "src", "data"]) {
    await cp(path.join(ROOT, directory), path.join(root, directory), { recursive: true });
  }
  for (const file of ["page-content-provenance.json", "llms-priority.json", "page-seo-copy.json"]) {
    await cp(path.join(ROOT, "data", file), path.join(root, "data", file));
  }
  await cp(path.join(ROOT, "public"), path.join(root, "public"), {
    recursive: true,
    filter: (source) => !source.startsWith(path.join(ROOT, "public", "assets")),
  });
  for (const article of loadCarrierArticles(ROOT).values()) {
    await writeFile(path.join(root, 'public', manifest.relative), renderedCarrierArticle(article, 'production-seal'));
  }
  // A sealed release is 0444. cp preserves that mode, but this isolated fixture
  // is intentionally mutated by the postbuild stage under test.
  const writable = spawnSync("chmod", ["-R", "u+w", path.join(root, "public")], { encoding: "utf8" });
  assert.equal(writable.status, 0, writable.stderr);
  return root;
'''


def test_lastmod_fixture_accepts_writable_successor_and_still_refuses_missing_chmod():
    assert builder.patch_lastmod_fixture_modes(LASTMOD_SUCCESSOR) == LASTMOD_SUCCESSOR
    # Without the chmod the read-only sealed copy would be mutated: refuse.
    stripped = LASTMOD_SUCCESSOR.replace(
        '  const writable = spawnSync("chmod", ["-R", "u+w", path.join(root, "public")], { encoding: "utf8" });\n', "")
    with pytest.raises(RuntimeError, match="lastmod_fixture_writable_anchor_invalid"):
        builder.patch_lastmod_fixture_modes(stripped)


PRODUCT_SUCCESSOR = '''  const report = JSON.parse(reportBytes);
  assert.equal(report.release_stage, expectedStage, `build-report ${reportSha256}: declared run stage mismatch`);
  assert.deepEqual(candidateExportShellFindings(html, {stage: expectedStage}), []);
  assert.ok(candidateExportShellFindings(wrongAnalyticsStage, {stage: expectedStage}).includes("analytics_wrong_stage"));
'''
PRODUCT_PREDECESSOR = '''  assert.deepEqual(candidateExportShellFindings(html), []);
  assert.ok(candidateExportShellFindings(wrongAnalyticsStage).includes("analytics_wrong_stage"));
'''


def test_preserved_export_fixture_accepts_declared_stage_successor_only():
    assert fixtures.patch_preserved_export_fixture(PRODUCT_SUCCESSOR) == PRODUCT_SUCCESSOR
    upgraded = fixtures.patch_preserved_export_fixture(PRODUCT_PREDECESSOR)
    assert "{stage: inheritedStage}" in upgraded
    # Stage-less findings without the build-report stage binding still refuse.
    unbound = PRODUCT_SUCCESSOR.replace("assert.equal(report.release_stage, expectedStage,", "")
    with pytest.raises(RuntimeError, match="preserved_review_stage_anchor_invalid"):
        fixtures.patch_preserved_export_fixture(unbound.replace("{stage: expectedStage}", ""))


PHASE3_SUCCESSOR = '''const graph = JSON.parse(await readFile(path.join(ROOT, "data/phase3-page-graph.json"), "utf8"));
const carrierCatalogs = await Promise.all(["content-batch.json", "content-national-wow-additions.json"].map(async (file) =>
  JSON.parse(await readFile(path.join(ROOT, "data/blog", file), "utf8"))));
const carrierArticles = carrierCatalogs.flatMap((catalog) => catalog.articles).filter((article) =>
  String(article.canonical_path || "").startsWith("/guides/"));
const carrierInsightArticles = carrierArticles.filter((article) =>
  !PHASE3_INSIGHTS_EXCLUSIONS.has(article.canonical_path));
const a = buildPhase3PageGraph({ articles: carrierArticles, couponOffersByProgram: {} });
const b = buildPhase3PageGraph({ articles: carrierArticles, couponOffersByProgram: {} });
  assert.equal(built.report.insight_pages, Math.max(1, Math.ceil(carrierInsightArticles.length / 12)));
'''


def test_phase3_fixture_accepts_bound_catalog_successor_without_redeclaring():
    out = builder.rebind_phase3_in_memory_fixture(PHASE3_SUCCESSOR)
    assert out == PHASE3_SUCCESSOR
    assert out.count("const carrierArticles = ") == 1
    # A successor that still feeds synthetic owned articles is NOT accepted.
    synthetic = PHASE3_SUCCESSOR.replace(
        "const a = buildPhase3PageGraph({ articles: carrierArticles,",
        "const a = buildPhase3PageGraph({ articles: ownedArticles(37),\n  articles: ownedArticles(37),")
    with pytest.raises(RuntimeError, match="phase3_fixture_actual_articles_anchor_invalid"):
        builder.rebind_phase3_in_memory_fixture(synthetic)
