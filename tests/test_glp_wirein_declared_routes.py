#!/usr/bin/env python3
"""BLOCKER H — provenance handling for provider-content wire-in builds.

THE DEFECT (2026-08-20). `scripts/postbuild-aeo-surface.mjs:165` throws
`lastmod_provenance_stale:<route>` for ANY content change it has no record of.
A wire-in build's whole purpose is to change content — adding a provider adds
that provider's pages and moves every machine-assembled surface that counts or
lists providers — so the wire-backlog lane died there on both of its 2026-08-20
runs, at `lastmod_provenance_stale:/`.

The current postbuild guard exposes an explicit provider-content refresh lane via
`GLP_LASTMOD_PROVENANCE_REFRESH=1`. The deployment worker must set it only on the
child build environment and record the postbuild readback. The derivation tests
below retain coverage of the route-enumeration evidence helper, while the worker
environment tests pin the live deployment contract:

  * the declaration is DERIVED, per run, from what the build actually wrote;
  * a build that wrote nothing declares nothing, leaving all 100 routes asserted;
  * a data change that does not move the program registry declares no
    machine-assembled surface, because no derived tally can have moved;
  * editorial author and policy routes are NEVER declared, so an unintended
    change to one still fails; the current renderer gates guide/index tallies;
  * provider-content builds carry the explicit refresh flag;
  * non-provider builds clear even an ambient flag, keeping the guard armed;
  * the release receipt records refresh use, build id, and any route names the
    postbuild stage actually emitted.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BUSINESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUSINESS / "scripts"))

import glp_affiliate_wire_backlog_worker as worker  # noqa: E402
import glp_affiliate_wire_backlog_daily as daily  # noqa: E402

DERIVER = BUSINESS / "scripts/derive_wirein_declared_routes.mjs"
#: The renderer's own machine-assembled-route predicate, taken from the release
#: the machine currently builds from, so the test exercises the real module.
CONTENT_BASE = BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
TALLIES = (Path(json.loads(CONTENT_BASE.read_text())["release_root"])
           / "src/surface-tallies.mjs")

ROUTES = [
    "/",                                         # machine-assembled (homepage tallies)
    "/best/online-glp-1-providers/",             # machine-assembled
    "/cost/online-glp-1-programs/",              # machine-assembled
    "/compare/found-vs-yourera/",                # machine-assembled
    "/reviews/orderlymeds/",                     # machine-assembled
    "/quiz/provider-fit/",                       # machine-assembled
    "/tools/glp-1-cost-calculator/",             # machine-assembled
    "/how-we-rate/",                             # machine-assembled
    "/guides/mounjaro-cost-without-insurance/",  # machine-assembled guide tallies
    "/authors/izaiah-tilton/",                   # EDITORIAL
    "/how-we-make-money/",                       # EDITORIAL
    "/glp-1-price-index/",                       # machine-assembled index tallies
]
EDITORIAL = ["/authors/izaiah-tilton/", "/how-we-make-money/"]

REGISTRY = {
    "schema": "glp-program-registry-v1",
    "programs": [
        {"program_id": "orderlymeds", "display_name": "Orderly Meds",
         "commercial_state": "live", "review_path": "/reviews/orderlymeds/",
         "go": {"route": "/go/orderlymeds"}},
    ],
}
NEW_PROGRAM = {
    "program_id": "nu-image-medical-review", "display_name": "Nu Image Medical",
    "commercial_state": "live", "review_path": "/reviews/nu-image-medical-review/",
    "go": {"route": "/go/nu-image-medical-review"},
}


def sitemap(routes) -> str:
    locations = "".join(
        f"<url><loc>https://glpclinicscout.com{route}</loc><lastmod>2026-08-19</lastmod></url>"
        for route in routes)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset>{locations}</urlset>\n'


class DeclaredRouteDerivationTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.pred = root / "predecessor"
        self.cand = root / "candidate"
        self.out = root / "declared.json"
        for tree in (self.pred, self.cand):
            (tree / "data").mkdir(parents=True)
            (tree / "public").mkdir(parents=True)
            (tree / "src").mkdir(parents=True)
            shutil.copy(TALLIES, tree / "src/surface-tallies.mjs")
            (tree / "public/sitemap.xml").write_text(sitemap(ROUTES))
            (tree / "data/program-registry.json").write_text(json.dumps(REGISTRY, indent=2))
            (tree / "data/deals.json").write_text(json.dumps({"deals": []}))

    def derive(self) -> dict:
        result = subprocess.run(
            ["node", str(DERIVER), "--predecessor", str(self.pred),
             "--candidate", str(self.cand), "--out", str(self.out)],
            capture_output=True, text=True, timeout=300, cwd=str(BUSINESS))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(self.out.read_text())

    def wire_in_a_provider(self):
        registry = json.loads((self.cand / "data/program-registry.json").read_text())
        registry["programs"].append(NEW_PROGRAM)
        (self.cand / "data/program-registry.json").write_text(json.dumps(registry, indent=2))

    def test_a_build_that_changed_no_data_declares_nothing(self):
        declaration = self.derive()
        self.assertEqual(declaration["schema"], "glp.lastmod_declared_routes.v1")
        self.assertEqual(declaration["routes"], [])
        self.assertEqual(declaration["derivation"]["changed_data_files"], [])
        self.assertEqual(len(declaration["derivation"]["still_asserted_routes"]), len(ROUTES))

    def test_the_new_providers_own_page_is_read_out_of_the_data_the_build_wrote(self):
        self.wire_in_a_provider()
        declaration = self.derive()
        self.assertEqual(declaration["derivation"]["new_routes_written_into_data"],
                         ["/reviews/nu-image-medical-review/"])
        self.assertIn("/reviews/nu-image-medical-review/", declaration["routes"])
        # The runtime redirector route is NOT a page and is never declared.
        self.assertNotIn("/go/nu-image-medical-review", declaration["routes"])

    def test_machine_assembled_surfaces_are_declared_and_editorial_ones_are_not(self):
        self.wire_in_a_provider()
        declaration = self.derive()
        for route in ["/", "/best/online-glp-1-providers/", "/cost/online-glp-1-programs/",
                      "/compare/found-vs-yourera/", "/reviews/orderlymeds/",
                      "/quiz/provider-fit/", "/tools/glp-1-cost-calculator/", "/how-we-rate/",
                      "/guides/mounjaro-cost-without-insurance/", "/glp-1-price-index/"]:
            self.assertIn(route, declaration["routes"], f"{route} should be declared")
        for route in EDITORIAL:
            self.assertNotIn(route, declaration["routes"], f"{route} must stay asserted")
            self.assertIn(route, declaration["derivation"]["still_asserted_routes"])

    def test_a_data_change_that_does_not_move_the_registry_declares_no_surface(self):
        # A tally is a function of the registry. If the registry did not move, no
        # machine-assembled page can have moved with it, and the gate stays fully
        # armed over every published route.
        (self.cand / "data/deals.json").write_text(json.dumps({"deals": [{"id": "x"}]}))
        declaration = self.derive()
        self.assertEqual(declaration["derivation"]["changed_data_files"], ["data/deals.json"])
        self.assertEqual(declaration["derivation"]["registry_files_changed"], [])
        self.assertEqual(declaration["derivation"]["machine_assembled_routes"], [])
        self.assertEqual(declaration["routes"], [])

    def test_the_declaration_is_deterministic_for_the_same_two_trees(self):
        self.wire_in_a_provider()
        first = self.derive()["routes"]
        second = self.derive()["routes"]
        self.assertEqual(first, second)
        self.assertEqual(first, sorted(set(first)))


class WireInBuildEnvironmentTest(unittest.TestCase):
    """The flag is child-build-only and provider-content-only."""

    def test_daily_provider_add_deploy_marks_the_build_as_provider_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            authority = root / "authority"
            authority.mkdir()
            (authority / "fixture-provider.json").write_text("{}")
            plan = {
                "queue": [{
                    "key": "network:fixture",
                    "site_program_id": "fixture-provider",
                    "evidence_hints": {"research": {}, "terms_classification": {}},
                }],
            }
            with (
                mock.patch.object(daily, "AUTHORITY_DIR", authority),
                mock.patch.object(daily, "binding_path", return_value=root / "bound.json"),
                mock.patch.object(daily.worker, "build_plan", return_value=plan),
                mock.patch.object(daily.worker, "atomic_json"),
                mock.patch.object(daily.worker, "begin_row"),
                mock.patch.object(daily.worker, "prepare_row"),
                mock.patch.object(
                    daily.worker, "release_prepared", return_value={"status": "PASS"},
                ) as release,
                mock.patch.object(
                    daily.worker, "status_receipt", return_value={"daily_card_line": "ok"},
                ),
            ):
                result = daily.run_day(
                    output_dir=root / "out",
                    ledger=root / "ledger.json",
                    backrun_dir=root / "backrun",
                    date_pt="2026-08-20",
                    deploy=True,
                )
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(release.call_args.kwargs["deploy"])
        self.assertTrue(release.call_args.kwargs["provider_content_change"])

    def test_registered_batch_no_deploy_stops_after_durable_row_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            authority_path = root / "batch-authority.json"
            authority_path.write_text("{}", encoding="utf-8")
            evidence = root / "evidence.json"
            evidence.write_text("{}", encoding="utf-8")
            key = "network:fixture"
            plan = {
                "pt_date": "2026-09-01",
                "queue": [{
                    "key": key,
                    "site_program_id": "fixture-provider",
                    "provider_program_id": "fixture",
                    "evidence_hints": {"research": {}, "terms_classification": {}},
                }],
                "batch_authority": {"allowed_queue_keys": [key]},
            }
            ledger_payload = {
                "rows": {key: {"status": "prepared", "pt_date": "2026-09-01"}}
            }
            with (
                mock.patch.object(daily, "authority_bound_plan", return_value=plan),
                mock.patch.object(daily.worker, "apply_batch_authority", return_value=plan),
                mock.patch.object(daily, "row_evidence_paths", return_value=(evidence, evidence, evidence)),
                mock.patch.object(daily.worker, "begin_row"),
                mock.patch.object(daily.worker, "prepare_row"),
                mock.patch.object(daily.worker, "plan_wired_limit", return_value=27),
                mock.patch.object(daily.worker, "load_ledger", return_value=ledger_payload),
                mock.patch.object(daily.worker, "path_binding", return_value={"path": "fixture"}),
                mock.patch.object(daily.worker, "release_prepared") as release,
                mock.patch.object(
                    daily.worker, "status_receipt", return_value={"daily_card_line": "ok"},
                ),
            ):
                result = daily.run_day(
                    output_dir=root / "out",
                    ledger=root / "ledger.json",
                    backrun_dir=None,
                    date_pt="2026-09-01",
                    deploy=False,
                    providers=["fixture-provider"],
                    batch_authority=authority_path,
                )
        self.assertEqual(
            result["status"], "PASS_BATCH_PREPARATION_ONLY_NO_EXTERNAL_WRITE"
        )
        self.assertIsNone(result["release"])
        self.assertEqual(result["preparation"]["prepared_target_count"], 1)
        self.assertEqual(result["preparation"]["globally_prepared_count"], 1)
        release.assert_not_called()

    def test_daily_pricing_consumes_separately_bound_same_provider_source(self):
        root = (BUSINESS / "reports/glp-clinic-scout/affiliate-machine/"
                "onboarding-manifest/authoring")
        latest = root / "authoring-20260829T222532Z-breeze-meds-review-c2.json"
        source = root / "authoring-20260829T075950Z-breeze-meds-review-c1.json"
        resolved = daily.pricing_by_provider([latest], [source])
        self.assertEqual(set(resolved), {"breeze-meds-review"})
        self.assertEqual(resolved["breeze-meds-review"].name, "pricing-spec.json")
        self.assertEqual(
            worker.verify_staged_pricing_spec(
                resolved["breeze-meds-review"], "breeze-meds-review"
            )["status"],
            "PASS",
        )

    def test_daily_pricing_refuses_provider_set_drift(self):
        root = (BUSINESS / "reports/glp-clinic-scout/affiliate-machine/"
                "onboarding-manifest/authoring")
        latest = root / "authoring-20260829T222532Z-breeze-meds-review-c2.json"
        other = root / "authoring-20260830T114104Z-care-bare-rx-glp1-review-c1.json"
        with self.assertRaisesRegex(worker.B2Error, "provider_set_mismatch"):
            daily.pricing_by_provider([latest], [other])

    def test_daily_pricing_allows_existing_provider_without_staged_pricing(self):
        root = (BUSINESS / "reports/glp-clinic-scout/affiliate-machine/"
                "onboarding-manifest/authoring")
        measured = root / "authoring-20260831T072400Z-measured-c1.json"
        self.assertEqual(daily.pricing_by_provider([measured], []), {})

    def test_provider_add_deploy_build_env_carries_provenance_refresh(self):
        env = worker._wire_in_build_env(
            provider_content_change=True, release_date="2026-08-20")
        self.assertEqual(env["GLP_LASTMOD_PROVENANCE_REFRESH"], "1")
        self.assertNotIn("GLP_LASTMOD_DECLARED_ROUTES", env)
        self.assertNotIn("GLP_LASTMOD_DECLARED_ROUTES_FILE", env)
        self.assertEqual(env["GLP_PRICE_INDEX_REFRESH"], "1")

    def test_non_provider_build_path_does_not_carry_provenance_refresh(self):
        os.environ["GLP_LASTMOD_PROVENANCE_REFRESH"] = "1"
        self.addCleanup(os.environ.pop, "GLP_LASTMOD_PROVENANCE_REFRESH", None)
        env = worker._wire_in_build_env(provider_content_change=False)
        self.assertNotIn("GLP_LASTMOD_PROVENANCE_REFRESH", env)
        # The helper copied the process environment; it did not export/unset it.
        self.assertEqual(os.environ["GLP_LASTMOD_PROVENANCE_REFRESH"], "1")

    def test_a_refreshed_lastmod_is_dated_the_day_it_actually_moved(self):
        # The stage's own fallback is a hard-coded 2026-08-19. A machine build
        # that refreshed a route today and published that date would be stating
        # a change happened on a day it did not.
        env = worker._wire_in_build_env(
            provider_content_change=True, release_date="2026-08-20")
        self.assertEqual(env["GLP_RELEASE_DATE"], "2026-08-20")
        with self.assertRaises(worker.B2Error):
            worker._wire_in_build_env(
                provider_content_change=True, release_date="20260820")

    def test_the_lane_the_build_ran_in_is_recorded_on_the_receipt(self):
        record = worker._run(
            ["true"], cwd=BUSINESS, timeout=60,
            env=worker._wire_in_build_env(provider_content_change=True))
        self.assertEqual(
            record["environment_overrides"]["GLP_LASTMOD_PROVENANCE_REFRESH"], "1")

    def test_deploy_receipt_records_refresh_use_build_id_and_named_routes(self):
        with tempfile.TemporaryDirectory() as temporary:
            candidate = Path(temporary) / "glp-b2-wire-backlog-test-build"
            stage = candidate / "artifacts/ops/aeo-surface-stage.json"
            stage.parent.mkdir(parents=True)
            stage.write_text(json.dumps({
                "schema": "glp-aeo-surface-stage-v1",
                "status": "PASS",
                "provenance_refresh": True,
                "declared_routes_refreshed": ["/", "/reviews/fixture/"],
            }))
            build_gate = worker._run(
                [
                    sys.executable,
                    "-c",
                    'print(\'{"provenance_refresh":true,'
                    '"declared_routes_refreshed":["/"]}\');print("x" * 3000)',
                ],
                cwd=BUSINESS,
                timeout=60,
            )
            self.assertNotIn("provenance_refresh", build_gate["stdout_tail"])
            evidence = worker._provenance_refresh_evidence(
                candidate,
                build_gate,
                expected=True,
            )
        self.assertTrue(evidence["provenance_refresh"])
        self.assertEqual(
            evidence["provenance_refresh_build_id"], "glp-b2-wire-backlog-test-build")
        self.assertEqual(
            evidence["provenance_routes_rewritten"], ["/", "/reviews/fixture/"])
        self.assertTrue(evidence["provenance_refresh_output_lines"])
        # The evidence object is expanded directly into both the durable day
        # release receipt and the successful deployment record.
        source = (BUSINESS / "scripts/glp_affiliate_wire_backlog_worker.py").read_text()
        self.assertGreaterEqual(source.count("**provenance_refresh,"), 2)


if __name__ == "__main__":
    unittest.main()
