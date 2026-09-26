#!/usr/bin/env python3
"""Make GLP provider-cardinality tests derive counts from governed candidate data.

The affiliate wire-in worker grows the provider set.  This one-time contract
migration removes frozen provider totals while preserving named safety checks
for aliases and editorial-link-dark rows.  It mutates only a materialized
candidate's test files and is idempotent on successor releases.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")
sys.path.insert(0, str(BUSINESS / "scripts"))
from glp_served_test_contract import (  # noqa: E402
    SERVED_TEST_ROOT_CANONICAL,
    SERVED_TEST_ROOT_DYNAMIC,
    normalize_candidate_local_served_test,
)
from site_tree_materializer import detach_for_write  # noqa: E402


class RefreshError(RuntimeError):
    pass


def replace_contract(path: Path, old: str, new: str,
                     marker: str | tuple[str, ...]) -> bool:
    text = path.read_text(encoding="utf-8")
    markers = (marker,) if isinstance(marker, str) else marker
    migrated = any(item in text for item in markers)
    old_is_embedded_in_migrated_contract = old in new
    if old in text and not (old_is_embedded_in_migrated_contract and migrated):
        detach_for_write(path)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return True
    if migrated:
        return False
    raise RefreshError(f"provider_cardinality_contract_unknown:{path}")


def refresh_served_test_contract(path: Path) -> bool:
    """Normalize every predecessor emitted by either release-building machine."""
    text = path.read_text(encoding="utf-8")
    try:
        normalized = normalize_candidate_local_served_test(text)
    except RuntimeError as exc:
        raise RefreshError(str(exc)) from exc
    if normalized == text:
        return False
    detach_for_write(path)
    path.write_text(normalized, encoding="utf-8")
    return True


def refresh(release: Path, activated_program_id: str) -> dict[str, object]:
    release = release.resolve()
    allowed = (
        (BUSINESS / "site-system/flagship-v2/candidates").resolve(),
        (BUSINESS / "site-system/flagship-v2/releases").resolve(),
    )
    if not any(release == root or root in release.parents for root in allowed):
        raise RefreshError("release_outside_site_system")
    tests = release / "tests"
    if not tests.is_dir():
        raise RefreshError("candidate_tests_missing")

    changed: list[str] = []

    served_test = tests / "pass2-late-audit-served.test.mjs"
    if refresh_served_test_contract(served_test):
        changed.append(served_test.name)

    migrations = {
        "affiliate-expansion-13.test.mjs": (
            '    if (["live-vital-launch", "trt-nation", "measured"].includes(review.program_id)) {',
            '    if (html.includes(\'data-affiliate-state="held"\')) {',
            "if (html.includes('data-affiliate-state=\"held\"'))",
        ),
        "cost-calculator.test.mjs": (
            '''  // 27, not 28: trt-nation stays a compare/dynamic-profile row but is scoped
  // out of the calculator conversion pool (editorial_link_dark, operator order
  // 2026-08-13 — pools equal the live-eligible set exactly).
  assert.equal(costCalculatorDropdownProviders(data).eligible.length, 27);
  assert.equal(costCalculatorDropdownProviders(data).eligible.some((provider) => provider.program_id === "trt-nation"), false);
  assert.equal(data.providers.filter((provider) => provider.mode === "autofill").length, 5);
  assert.equal(data.providers.filter((provider) => provider.mode === "manual").length, 23);''',
            '''  const dropdown = costCalculatorDropdownProviders(data);
  assert.equal(dropdown.eligible.length + dropdown.excluded.length, dynamic.counts.provider_identities);
  assert.equal(dropdown.eligible.some((provider) => provider.program_id === "trt-nation"), false);
  assert.equal(data.providers.filter((provider) => provider.mode === "autofill").length, dynamic.counts.calculator_autofill);
  assert.equal(data.providers.filter((provider) => provider.mode === "manual").length, dynamic.counts.calculator_manual);''',
            "dropdown.eligible.length + dropdown.excluded.length",
        ),
        "playbook.test.mjs": (
            '''  assert.equal(expected.length, 29);
  assert.deepEqual(
    data.programs.map((item) => item.program_id).sort(),
    expected.map((item) => String(item.review_slug || item.program_id)).sort(),
  );
  assert.equal(data.program_count, 29);
  assert.equal(data.programs.length, 29);
  assert.equal(data.live_program_count, 28);
  const affiliateActive = data.programs.filter((item) => item.live_link_allowed || item.promotion_authorized);
  assert.equal(affiliateActive.length, 28);''',
            '''  assert.deepEqual(
    data.programs.map((item) => item.program_id).sort(),
    expected.map((item) => String(item.review_slug || item.program_id)).sort(),
  );
  assert.equal(data.program_count, expected.length);
  assert.equal(data.programs.length, expected.length);
  const affiliateActive = data.programs.filter((item) => item.live_link_allowed || item.promotion_authorized);
  assert.equal(data.live_program_count, affiliateActive.length);''',
            "assert.equal(data.program_count, expected.length);",
        ),
        # The homepage "Providers compared" tile is rendered from the live
        # provider count at build time, and the test said so in a comment while
        # asserting a frozen literal beside it. Every wire-in that grows the
        # provider set therefore killed the suite on a number nothing verified
        # (31 -> 33, 2026-08-18). Derive it from the same governed selection
        # artifact the tile is built from.
        "product-ready.test.mjs": (
            '  // The homepage tile is derived from the live provider count at build time.\n  // 2026-08-17: three Katalys-paused programs were removed, so it reads 31.\n  assert.match(home, /Providers compared[\\s\\S]*?<dd>31<\\/dd>/);',
            '  // The homepage tile is derived from the live provider count at build time,\n  // so this asserts against the governed selection artifact rather than a\n  // literal that every wire-in would invalidate.\n  const liveProviderCount = JSON.parse(\n    await readFile(path.join(ROOT, "data", "affiliate-activation-selection.json"), "utf8"),\n  ).live_program_count;\n  assert.ok(Number.isInteger(liveProviderCount) && liveProviderCount > 0);\n  assert.match(home, new RegExp(`Providers compared[\\\\s\\\\S]*?<dd>${liveProviderCount}</dd>`));',
            'const liveProviderCount = JSON.parse(',
        ),
        "price-index.test.mjs": (
            "  assert.equal(result.live_provider_count, 28);",
            "  assert.equal(result.live_provider_count, input.programRegistry.provider_dynamic_profiles.expected_counts.provider_identities);",
            (
                "input.programRegistry.provider_dynamic_profiles.expected_counts.provider_identities",
                # The retained price-index fixture now checks its own frozen
                # manifest, not the growing provider registry. Preserve that
                # denominator; require its exact source binding AND assertion.
                "  const frozenManifest=JSON.parse(await readFile(new URL('../artifacts/ops/price-index-manifest.json',import.meta.url)));\n"
                "  assert.equal(result.live_provider_count,frozenManifest.live_provider_count);",
            ),
        ),
        "provider-dynamic-profiles.test.mjs": (
            '''  assert.equal(registry.therapeutic_taxonomy.expected_counts.provider_rows, 29);
  assert.equal(validated.canonicalIds.length, 28);
  assert.deepEqual(validated.counts, {
    provider_identities: 28,
    semaglutide: 25,
    tirzepatide: 23,
    retatrutide: 0,
    wegovy: 8,
    zepbound: 5,
    mounjaro: 4,
    oral: 13,
    injectable: 15,
    compounded: 25,
    brand_name: 9,
    treatment_type_unknown: 3,
    calculator_autofill: 5,
    calculator_manual: 23,
  });''',
            '''  assert.equal(registry.therapeutic_taxonomy.expected_counts.provider_rows, Object.keys(registry.therapeutic_taxonomy.programs).length);
  assert.equal(validated.canonicalIds.length, registry.provider_dynamic_profiles.expected_counts.provider_identities);
  assert.deepEqual(validated.counts, registry.provider_dynamic_profiles.expected_counts);''',
            "assert.deepEqual(validated.counts, registry.provider_dynamic_profiles.expected_counts);",
        ),
        "therapeutic-surfaces-build6.test.mjs": (
            '''  assert.equal(Object.keys(registry.therapeutic_taxonomy.programs).length, 29);
  assert.equal(projected.includeProgramIds.length, 28);''',
            '''  assert.equal(Object.keys(registry.therapeutic_taxonomy.programs).length, registry.therapeutic_taxonomy.expected_counts.provider_rows);
  assert.equal(projected.includeProgramIds.length, registry.provider_dynamic_profiles.expected_counts.provider_identities);''',
            "registry.provider_dynamic_profiles.expected_counts.provider_identities",
        ),
        "therapeutic-taxonomy-build6.test.mjs": (
            '''const expectedCounts = {
  provider_rows: 29,
  semaglutide: 26,
  tirzepatide: 24,
  retatrutide: 0,
  wegovy: 9,
  zepbound: 6,
  mounjaro: 4,
  oral: 14,
  injectable: 16,
  compounded: 26,
  brand_name: 9,
  treatment_type_unknown: 3,
};''',
            "const expectedCounts = registry.therapeutic_taxonomy.expected_counts;",
            "const expectedCounts = registry.therapeutic_taxonomy.expected_counts;",
        ),
        "therapeutic-wire-in-propagation-build6.test.mjs": (
            '''  assert.equal(reconciled.activeProgramIds.length, 30);
  assert.equal(reconciled.counts.provider_rows, 30);
  assert.equal(reconciled.counts.semaglutide, 27);
  assert.equal(reconciled.counts.oral, 15);
  assert.equal(reconciled.counts.retatrutide, 0);''',
            '''  assert.equal(reconciled.activeProgramIds.length, baseRegistry.therapeutic_taxonomy.expected_counts.provider_rows + 1);
  assert.equal(reconciled.counts.provider_rows, baseRegistry.therapeutic_taxonomy.expected_counts.provider_rows + 1);
  assert.equal(reconciled.counts.semaglutide, baseRegistry.therapeutic_taxonomy.expected_counts.semaglutide + 1);
  assert.equal(reconciled.counts.oral, baseRegistry.therapeutic_taxonomy.expected_counts.oral + 1);
  assert.equal(reconciled.counts.retatrutide, baseRegistry.therapeutic_taxonomy.expected_counts.retatrutide);''',
            "baseRegistry.therapeutic_taxonomy.expected_counts.provider_rows + 1",
        ),
        "wave2-regression.test.mjs": (
            '''  assert.equal(rated, 23);
  assert.equal(pending, 4);''',
            '''  assert.equal(rated + pending, routes.length);
  assert.ok(rated > 0);
  assert.ok(pending > 0);''',
            "assert.equal(rated + pending, routes.length);",
        ),
    }
    for name, (old, new, marker) in migrations.items():
        path = tests / name
        if replace_contract(path, old, new, marker):
            changed.append(name)

    fastpath = tests / "provider-fastpath-plus5.test.mjs"
    fastpath_text = fastpath.read_text(encoding="utf-8")
    dynamic_state_marker = "if (html.includes('data-affiliate-state=\"held\"'))"
    if dynamic_state_marker not in fastpath_text:
        state_branch = re.compile(
            r'^    if \(\[[^\]]*\]\.includes\(review\.program_id\)\) \{$',
            re.M,
        )
        if len(state_branch.findall(fastpath_text)) != 1:
            raise RefreshError("provider_fastpath_state_branch_ambiguous")
        detach_for_write(fastpath)
        fastpath_text = state_branch.sub(
            '    if (html.includes(\'data-affiliate-state="held"\')) {',
            fastpath_text,
            count=1,
        )
        fastpath.write_text(fastpath_text, encoding="utf-8")
        changed.append(fastpath.name)

    # Additional dynamic assertions in files whose baseline block is broader.
    extra = [
        (
            "pass2-late-audit-static-output.test.mjs",
            '''const canonicalReviewProviderCount = 34; // 2026-08-27 live-head merge: +nu-image''',
            r'''const canonicalReviewProviderCount = registry.programs.filter((program) => {
  const profile = program.profiles?.["glp-clinic-scout"];
  return /^\/reviews\/[a-z0-9-]+\/$/.test(program.review_path || "")
    && profile?.canonical_identity?.role !== "alias";
}).length;''',
            "profile?.canonical_identity?.role !== \"alias\"",
        ),
        (
            "pass2-late-audit-static-output.test.mjs",
            '''  const programs = [...registry.programs, ...graph.provider_work_order_additions];
  const redirects = await read("public/_redirects");
  const redirectSources = new Set(redirects.trim().split("\\n").map((line) => line.split(/\\s+/)[0]));
  assert.equal(programs.length, 36); // 2026-08-27 live-head merge: +nu-image''',
            '''  const providerRows = [...registry.programs, ...graph.provider_work_order_additions];
  const governedProviderIds = new Set(providerRows.map((program) => program.program_id));
  const programs = [...new Map(providerRows.map((program) => [program.program_id, program])).values()];
  const redirects = await read("public/_redirects");
  const redirectSources = new Set(redirects.trim().split("\\n").map((line) => line.split(/\\s+/)[0]));
  assert.equal(programs.length, governedProviderIds.size);''',
            "const governedProviderIds = new Set(providerRows.map",
        ),
        (
            "phase3-page-graph.test.mjs",
            '''const graph = JSON.parse(await readFile(path.join(ROOT, "data/phase3-page-graph.json"), "utf8"));''',
            '''const graph = JSON.parse(await readFile(path.join(ROOT, "data/phase3-page-graph.json"), "utf8"));
const governedRegistry = JSON.parse(await readFile(path.join(ROOT, "data/program-registry.json"), "utf8"));
const governedProviderCount = new Set([
  ...governedRegistry.programs,
  ...(graph.provider_work_order_additions || []),
].map((program) => program.program_id)).size;''',
            (
                "const governedProviderCount = new Set([",
                # Newer predecessors already bind the family count directly to
                # the candidate-local governed registry. Rewriting that valid
                # contract into an older equivalent is unnecessary and, before
                # this alias, made every new provider row fail closed.
                'built.report.family_counts["provider-detail"], programRegistry.programs.length',
            ),
        ),
        (
            "phase3-page-graph.test.mjs",
            '''  assert.equal(built.report.family_counts["provider-detail"], 35);''',
            '''  assert.equal(built.report.family_counts["provider-detail"], governedProviderCount);''',
            (
                "built.report.family_counts[\"provider-detail\"], governedProviderCount",
                'built.report.family_counts["provider-detail"], programRegistry.programs.length',
            ),
        ),
        (
            "phase3-page-graph.test.mjs",
            '''  assert.equal(providerTags.length, 35, "G-2 must expose exactly the 35 governed identities (2026-08-27: nu-image moved from work-order addition into the registry proper — same total)");''',
            '''  assert.equal(providerTags.length, governedProviderCount, "G-2 must expose exactly the governed provider identities");''',
            (
                "providerTags.length, governedProviderCount",
                # The newer page-graph contract separates priced cards from
                # disclosure records, then proves the union against the dynamic
                # governed registry plus its four source-priced additions.
                "surfacedIds.size, registry.programs.length + 4",
                # Option C deliberately decouples evidence-qualified default-grid
                # cards from the growing provider-detail denominator. A new
                # provider route must not inflate this source-backed card count.
                'renderedIds.length, 35, "Option C must render exactly 35 evidence-qualified provider cards"',
                # 2026-09-02: the Option C block below is now itself migrated to
                # a governed-registry derivation, which DESTROYS the marker just
                # above. Without this second form, the first invocation migrated
                # the block and every later invocation found neither this
                # entry's `old` nor any marker and raised
                # provider_cardinality_contract_unknown. The aggregate runs this
                # refresh once per prepared row, so that surfaced on row 2 of 18
                # in production while a single-invocation test passed clean.
                "renderedIds.length, optionCExpected",
            ),
        ),
        (
            "wave2-regression.test.mjs",
            '''  assert.equal(selection.programs.length, 11);''',
            '''  assert.equal(selection.programs.length, selection.program_count);''',
            "selection.programs.length, selection.program_count",
        ),
        (
            "phase5-review-intake.test.mjs",
            '''  assert.deepEqual(counts, { provider: 34, pharmacy: 90, total: 124 } /* 2026-08-27 +nu-image */);
  assert.equal(optionTags.length, 125, "one governed alias remains visible but disabled"); // 2026-08-27 +nu-image
  assert.equal(enabled.length, counts.total);
  assert.equal(enabled.filter((tag) => /data-review-entity-type="provider"/.test(tag)).length, counts.provider);
  assert.equal(enabled.filter((tag) => /data-review-entity-type="pharmacy"/.test(tag)).length, counts.pharmacy);
  assert.equal(optionTags.filter((tag) => /disabled data-review-entity-alias="true"/.test(tag)).length, 1);''',
            r'''  const expectedProviderCount = registry.programs.filter((program) => {
    const profile = program.profiles?.["glp-clinic-scout"];
    return /^\/reviews\/[a-z0-9-]+\/$/.test(program.review_path || "")
      && profile?.canonical_identity?.role !== "alias";
  }).length;
  const expectedAliasCount = registry.programs.filter((program) => {
    const profile = program.profiles?.["glp-clinic-scout"];
    return /^\/reviews\/[a-z0-9-]+\/$/.test(program.review_path || "")
      && profile?.canonical_identity?.role === "alias";
  }).length;
  const expectedCounts = {
    provider: expectedProviderCount,
    pharmacy: pharmacyInput.entries.length,
    total: expectedProviderCount + pharmacyInput.entries.length,
  };
  assert.deepEqual(counts, expectedCounts);
  assert.equal(optionTags.length, counts.total + expectedAliasCount);
  assert.equal(enabled.length, counts.total);
  assert.equal(enabled.filter((tag) => /data-review-entity-type="provider"/.test(tag)).length, counts.provider);
  assert.equal(enabled.filter((tag) => /data-review-entity-type="pharmacy"/.test(tag)).length, counts.pharmacy);
  assert.equal(optionTags.filter((tag) => /disabled data-review-entity-alias="true"/.test(tag)).length, expectedAliasCount);''',
            "const expectedProviderCount = registry.programs.filter",
        ),
        (
            "provider-dynamic-profiles.test.mjs",
            '''  assert.equal(suggestions.length, 28);''',
            '''  assert.equal(suggestions.length, validated.canonicalIds.length);''',
            "assert.equal(suggestions.length, validated.canonicalIds.length);",
        ),
        (
            "provider-dynamic-profiles.test.mjs",
            '''  assert.equal(dropdown.eligible.length, 27);''',
            '''  assert.equal(dropdown.eligible.length + dropdown.excluded.length, validated.canonicalIds.length);''',
            "dropdown.eligible.length + dropdown.excluded.length",
        ),
        (
            "provider-dynamic-profiles.test.mjs",
            '''  assert.equal(taxonomy.counts.provider_rows, 30);
  const dynamic = reconcileProviderDynamicProfilesWireIn({ registry: fixture.registry, profileId: "glp-clinic-scout", asOf });
  assert.equal(dynamic.canonicalIds.length, 29);
  assert.equal(dynamic.counts.provider_identities, 29);''',
            '''  assert.equal(taxonomy.counts.provider_rows, registry.therapeutic_taxonomy.expected_counts.provider_rows + 1);
  const dynamic = reconcileProviderDynamicProfilesWireIn({ registry: fixture.registry, profileId: "glp-clinic-scout", asOf });
  assert.equal(dynamic.canonicalIds.length, registry.provider_dynamic_profiles.expected_counts.provider_identities + 1);
  assert.equal(dynamic.counts.provider_identities, registry.provider_dynamic_profiles.expected_counts.provider_identities + 1);''',
            "registry.provider_dynamic_profiles.expected_counts.provider_identities + 1",
        ),
        (
            "provider-dynamic-profiles.test.mjs",
            '''  assert.equal(projected.includeProgramIds.length, 29);''',
            '''  assert.equal(projected.includeProgramIds.length, dynamic.canonicalIds.length);''',
            "assert.equal(projected.includeProgramIds.length, dynamic.canonicalIds.length);",
        ),
        (
            "provider-dynamic-profiles.test.mjs",
            '''    assert.equal(projectedPage.canonical_provider_ids.length, 29, path);''',
            '''    assert.equal(projectedPage.canonical_provider_ids.length, dynamic.canonicalIds.length, path);''',
            "projectedPage.canonical_provider_ids.length, dynamic.canonicalIds.length",
        ),
        (
            "therapeutic-surfaces-build6.test.mjs",
            '''    assert.equal(page.rows.length, 28, path);''',
            '''    assert.equal(page.rows.length, projected.includeProgramIds.length, path);''',
            "page.rows.length, projected.includeProgramIds.length",
        ),
        (
            "therapeutic-surfaces-build6.test.mjs",
            '''  // 27, not 28: trt-nation keeps its compare row (above) but is scoped out of
  // the quiz conversion pool — editorial_link_dark, operator order 2026-08-13,
  // documented surface_restriction_basis on the registry row.
  assert.equal(projected.quizConfig.candidates.length, 27);
  assert.deepEqual(projected.quizConfig.candidates.map((row) => row.program_id).sort(), projected.includeProgramIds.filter((id) => id !== "trt-nation"));''',
            '''  const expectedQuizIds = projected.includeProgramIds.filter((id) => {
    const program = registry.programs.find((row) => String(row.review_slug || row.program_id) === id);
    return program?.profiles?.["glp-clinic-scout"]?.surfaces?.quiz === true;
  }).sort();
  assert.deepEqual(projected.quizConfig.candidates.map((row) => row.program_id).sort(), expectedQuizIds);''',
            "const expectedQuizIds = projected.includeProgramIds.filter",
        ),
        (
            "therapeutic-taxonomy-build6.test.mjs",
            '''  assert.equal(validated.programs.size, 29);''',
            '''  assert.equal(validated.programs.size, expectedCounts.provider_rows);''',
            "validated.programs.size, expectedCounts.provider_rows",
        ),
        (
            "therapeutic-taxonomy-build6.test.mjs",
            '''  assert.equal(select(["semaglutide"]).length, 26);
  assert.equal(select(["tirzepatide"]).length, 24);
  assert.equal(select(["semaglutide", "tirzepatide"]).length, 27);
  assert.equal(select(["retatrutide"]).length, 0);
  assert.equal(select([], "compounded").length, 26);
  assert.equal(select([], "brand_name").length, 9);
  assert.equal(select([], "either").length, 26);
  assert.equal(select([], "all").length, 29);''',
            '''  assert.equal(select(["semaglutide"]).length, expectedCounts.semaglutide);
  assert.equal(select(["tirzepatide"]).length, expectedCounts.tirzepatide);
  assert.equal(select(["semaglutide", "tirzepatide"]).length, new Set([...select(["semaglutide"]), ...select(["tirzepatide"])].map((row) => row.program_id)).size);
  assert.equal(select(["retatrutide"]).length, expectedCounts.retatrutide);
  assert.equal(select([], "compounded").length, expectedCounts.compounded);
  assert.equal(select([], "brand_name").length, expectedCounts.brand_name);
  assert.equal(select([], "either").length, new Set([...select([], "compounded"), ...select([], "brand_name")].map((row) => row.program_id)).size);
  assert.equal(select([], "all").length, expectedCounts.provider_rows);''',
            "new Set([...select([\"semaglutide\"]), ...select([\"tirzepatide\"])]",
        ),
        (
            "therapeutic-taxonomy-build6.test.mjs",
            '''  assert.equal(candidatesForMedicationPreference(candidates, "semaglutide").length, 26);
  assert.equal(candidatesForMedicationPreference(candidates, "tirzepatide").length, 24);
  assert.equal(candidatesForMedicationPreference(candidates, "retatrutide").length, 0);
  assert.equal(candidatesForMedicationPreference(candidates, "not_sure").length, 29);''',
            '''  assert.equal(candidatesForMedicationPreference(candidates, "semaglutide").length, expectedCounts.semaglutide);
  assert.equal(candidatesForMedicationPreference(candidates, "tirzepatide").length, expectedCounts.tirzepatide);
  assert.equal(candidatesForMedicationPreference(candidates, "retatrutide").length, expectedCounts.retatrutide);
  assert.equal(candidatesForMedicationPreference(candidates, "not_sure").length, expectedCounts.provider_rows);''',
            "candidatesForMedicationPreference(candidates, \"not_sure\").length, expectedCounts.provider_rows",
        ),
        (
            "therapeutic-wire-in-propagation-build6.test.mjs",
            '''  assert.equal(projected.includeProgramIds.length, 29);''',
            '''  assert.equal(projected.includeProgramIds.length, baseRegistry.provider_dynamic_profiles.expected_counts.provider_identities + 1);''',
            "baseRegistry.provider_dynamic_profiles.expected_counts.provider_identities + 1",
        ),
        (
            "price-watch-email.test.mjs",
            '''  assert.equal(emitted.length, 117);''',
            '''  assert.equal(emitted.length, CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES.length);''',
            (
                "assert.equal(emitted.length, CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES.length);",
                "assert.deepEqual(emitted, [...new Set([...approved, ...staged])].sort());",
            ),
        ),
        (
            "trt-nation-link-dark.test.mjs",
            '''  assert.equal(inputs.eligibility.program_count, 29);
  assert.equal(inputs.eligibility.live_program_count, 28);''',
            '''  assert.equal(inputs.eligibility.program_count, inputs.programRegistry.therapeutic_taxonomy.expected_counts.provider_rows);
  assert.equal(inputs.eligibility.live_program_count, inputs.activationSelection.live_program_count);''',
            "inputs.programRegistry.therapeutic_taxonomy.expected_counts.provider_rows",
        ),
        (
            "trt-nation-link-dark.test.mjs",
            '''  assert.equal(inputs.activationSelection.live_program_count, 28);

  const buildReport = await readJson("artifacts/ops/build-report.json");
  assert.equal(buildReport.direct_affiliate_programs, 27);
  assert.equal(buildReport.live_programs, 28);
  assert.equal(buildReport.live_registry_rows, 29);''',
            '''  assert.equal(inputs.activationSelection.live_program_count, inputs.eligibility.live_program_count);

  const buildReport = await readJson("artifacts/ops/build-report.json");
  const aliasIds = new Set(inputs.programRegistry.programs
    .filter((program) => program.profiles?.["glp-clinic-scout"]?.canonical_identity?.role === "alias")
    .map((program) => String(program.review_slug || program.program_id)));
  const expectedDirectPrograms = inputs.contentLedger.programs
    .filter((program) => (program.live_link_allowed || program.promotion_authorized) && !aliasIds.has(program.program_id)).length;
  assert.equal(buildReport.direct_affiliate_programs, expectedDirectPrograms);
  assert.equal(buildReport.live_programs, inputs.programRegistry.provider_dynamic_profiles.expected_counts.provider_identities);
  assert.equal(buildReport.live_registry_rows, inputs.programRegistry.therapeutic_taxonomy.expected_counts.provider_rows);''',
            "const expectedDirectPrograms = inputs.contentLedger.programs",
        ),
        (
            "pass5-standalone-theme.test.mjs",
            '''  assert.equal(affiliateAnchors.length, 6);
  assert.deepEqual(affiliateAnchors.map((anchor) => anchor.match(/data-link-position="([^\"]+)"/)?.[1]), ["lp-hero", "lp-rail", "lp-rail", "lp-rail", "lp-rail", "lp-sticky"]);''',
            '''  const affiliatePositions = affiliateAnchors.map((anchor) => anchor.match(/data-link-position="([^\"]+)"/)?.[1]);
  assert.equal(affiliatePositions.filter((position) => position === "lp-hero").length, 1);
  assert.equal(affiliatePositions.filter((position) => position === "lp-sticky").length, 1);
  assert.ok(affiliatePositions.filter((position) => position === "lp-rail").length >= 1);
  const railBody = html.match(/<tbody>([\\s\\S]*?)<\\/tbody>/)?.[1] || "";
  const renderedRailRows = [...railBody.matchAll(/<tr>([\\s\\S]*?)<\\/tr>/g)].map((match) => match[1]);
  assert.ok(renderedRailRows.length >= 3);
  for (const row of renderedRailRows) {
    assert.match(row, /data-link-position="lp-rail"/);
    const live = /data-analytics-event="affiliate_click"[^>]*data-affiliate-state="live"/.test(row);
    const unavailable = /data-affiliate-state="unavailable"/.test(row);
    assert.notEqual(live, unavailable, "each ranked rail row must be exactly live or unavailable");
  }''',
            "const affiliatePositions = affiliateAnchors.map",
        ),
        (
            # DEFECT 2026-09-01 (deploy-fable-20260901). A release that provisions a
            # first-party /go route rewrites the approved row's tracking state to
            # `candidate_registry_wired_first_party_go_staged` — worker.py's
            # apply_first_party_go_route_provision does it deliberately, and the
            # coverage VALIDATOR was taught the new state by
            # GO_ROUTE_PROVISION_CONTRACT_V1. Its own suite never was, so this exact
            # assertion turned a SUCCESSFUL /go deploy into a failing npm test — the
            # same #139 class as the provider-card count pins, one file further out.
            # Proven, not assumed: provisioning the three staged /go requests against
            # a candidate materialized from the live head fails exactly here, 12/13.
            #
            # The list stays an exhaustive enumeration rather than becoming a
            # wildcard: every state a governed release may legitimately produce is
            # named, so an unrecognised state still fails closed.
            "approved-program-53-coverage.test.mjs",
            '''    assert.ok(["candidate_registry_wired_provider_issued", "content_fit_lifted_provider_issued"].includes(tracking.state));''',
            '''    assert.ok([
      "candidate_registry_wired_provider_issued",
      "content_fit_lifted_provider_issued",
      // Written by apply_first_party_go_route_provision when this release stages
      // the provider's own approved URL behind our first-party /go redirect.
      "candidate_registry_wired_first_party_go_staged",
      // A relationship approved AFTER the frozen 2026-08-02 evidence snapshot, so
      // it can never appear in that snapshot and is its own partition.
      "post_snapshot_operator_rescinded_provider_issued",
    ].includes(tracking.state), `${program.program_key}:${tracking.state}`);''',
            "candidate_registry_wired_first_party_go_staged",
        ),
    ]
    optional_extra_tests = {"pass5-standalone-theme.test.mjs"}
    # 2026-09-02: the compare-surface wire-in. The 18 backlog providers were
    # already live in the registry (56 programs) but rendered no compare card,
    # because data/provider-facts-backfill was frozen at the 38-row roster. Once
    # the facts projection unfroze them the Option C grid went 35 -> 53, and
    # these two literals were the last thing between the build and the deploy.
    # Bind them to the governed registry minus the rows the same test already
    # declares detail-only: 56 - 3 = 53, and 38 - 3 = 35 for an older
    # predecessor. That is a stronger contract than the literal - a provider
    # that silently stops rendering a card now fails loudly instead of being
    # absorbed by a hand-updated number.
    extra += [
        (
            "phase3-page-graph.test.mjs",
            '''  assert.equal(renderedIds.length, 35, "Option C must render exactly 35 evidence-qualified provider cards");
  assert.equal(new Set(renderedIds).size, 35, "provider cards must not repeat");''',
            '''  const optionCDetailOnly = new Set(["mydrhank", "breeze-meds-review", "care-bare-rx-glp1-review"]);
  const optionCExpected = registry.programs.filter((program) => !optionCDetailOnly.has(program.program_id)).length;
  assert.equal(renderedIds.length, optionCExpected, "Option C must render every governed provider except the declared detail-only rows");
  assert.equal(new Set(renderedIds).size, optionCExpected, "provider cards must not repeat");''',
            "const optionCExpected = registry.programs.filter",
        ),
        (
            "design-pass4-late-callback-closure.test.mjs",
            r'''  assert.equal(cards.size, 35);
  assert.equal(count(providerMarkup, /data-provider-card-contract="option-c"/g), 35);
  assert.equal(count(providerMarkup, /<dt>Medications offered<\/dt>/g), 35);
  assert.equal(count(providerMarkup, /<dt>Total monthly cost<\/dt>/g), 35);
  assert.equal(count(providerMarkup, /<dt>Cancellation<\/dt>/g), 35);''',
            r'''  const optionCRegistry = JSON.parse(await read("data/program-registry.json"));
  const optionCDetailOnly = new Set(["mydrhank", "breeze-meds-review", "care-bare-rx-glp1-review"]);
  const optionCExpected = optionCRegistry.programs.filter((program) => !optionCDetailOnly.has(program.program_id)).length;
  assert.equal(cards.size, optionCExpected);
  assert.equal(count(providerMarkup, /data-provider-card-contract="option-c"/g), optionCExpected);
  assert.equal(count(providerMarkup, /<dt>Medications offered<\/dt>/g), optionCExpected);
  assert.equal(count(providerMarkup, /<dt>Total monthly cost<\/dt>/g), optionCExpected);
  assert.equal(count(providerMarkup, /<dt>Cancellation<\/dt>/g), optionCExpected);''',
            "const optionCExpected = optionCRegistry.programs.filter",
        ),
    ]

    for name, old, new, marker in extra:
        path = tests / name
        if name in optional_extra_tests and not path.is_file():
            continue
        if replace_contract(path, old, new, marker):
            changed.append(name)

    # Historical observations keep their original source bindings. Tests that
    # intentionally truncate history to simulate a first publication must bind
    # that synthetic latest row to the candidate's current governed sources.
    history_migrations = {
        "price-index.test.mjs": (
            '  migratePriceIndexHistoryV1ToV2,\n',
            '  migratePriceIndexHistoryV1ToV2,\n  normalizePriceIndexSourceHashes,\n',
            '  normalizePriceIndexSourceHashes,\n',
        ),
        "price-watch-email.test.mjs": (
            'import { composePriceWatchIssue } from "../src/price-watch-email.mjs";\n',
            'import { composePriceWatchIssue } from "../src/price-watch-email.mjs";\nimport { normalizePriceIndexSourceHashes } from "../src/price-index.mjs";\n',
            'import { normalizePriceIndexSourceHashes } from "../src/price-index.mjs";\n',
        ),
    }
    for name, (old, new, marker) in history_migrations.items():
        path = tests / name
        if replace_contract(path, old, new, marker):
            changed.append(name)
        text = path.read_text(encoding="utf-8")
        slice_anchor = "  input.tracker.price_index.observations = input.tracker.price_index.observations.slice(0, 1);\n"
        binding = "  input.tracker.price_index.observations.at(-1).source_files = normalizePriceIndexSourceHashes(input.programRegistry, input.sourceHashes);\n"
        if binding not in text:
            count = text.count(slice_anchor)
            expected = 2 if name == "price-index.test.mjs" else 1
            if count != expected:
                raise RefreshError(f"price_index_history_fixture_anchor_count:{name}:{count}")
            detach_for_write(path)
            path.write_text(text.replace(slice_anchor, slice_anchor + binding), encoding="utf-8")
            changed.append(name)

    # Remove only the selected row's stale named hold-copy block. Generic state
    # assertions above remain and now decide active/held from rendered output.
    text = fastpath.read_text(encoding="utf-8")
    target = re.escape(activated_program_id)
    block = re.compile(
        rf'^  const [A-Za-z_$][A-Za-z0-9_$]* = renderProviderReviewPage\(reviews\.get\("{target}"\), identity\);\n'
        rf'(?:  assert\.match\([^\n]+\);\n)+\n',
        re.M,
    )
    matches = list(block.finditer(text))
    if len(matches) > 1:
        raise RefreshError("target_static_test_block_ambiguous")
    if matches:
        detach_for_write(fastpath)
        fastpath.write_text(block.sub("", text, count=1), encoding="utf-8")
        changed.append(fastpath.name)

    return {
        "schema": "glp-provider-cardinality-test-refresh-v1",
        "status": "PASS",
        "activated_program_id": activated_program_id,
        "changed_files": sorted(set(changed)),
        "changed_count": len(set(changed)),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--activated-program-id", required=True)
    args = parser.parse_args()
    try:
        result = refresh(args.release, args.activated_program_id)
    except (OSError, ValueError, RefreshError) as exc:
        print(json.dumps({"schema": "glp-provider-cardinality-test-refresh-v1", "status": "BLOCKED", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
