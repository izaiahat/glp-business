import importlib.util
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock


MODULE_PATH = Path("/home/ubuntu/business/scripts/glp_provider_cardinality_test_refresh.py")
SPEC = importlib.util.spec_from_file_location("glp_provider_cardinality_test_refresh", MODULE_PATH)
assert SPEC and SPEC.loader
refresh = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = refresh
SPEC.loader.exec_module(refresh)


class ProviderCardinalityRefreshTests(TestCase):
    @staticmethod
    def _candidate_from(source_tests: Path, business: Path, name: str) -> Path:
        candidate = business / f"site-system/flagship-v2/candidates/{name}"
        shutil.copytree(source_tests, candidate / "tests")
        for copied in candidate.rglob("*"):
            copied.chmod(0o755 if copied.is_dir() else 0o644)
        return candidate

    @mock.patch.object(refresh, "detach_for_write")
    def test_replaces_stale_contract_even_when_marker_exists_elsewhere(self, detach):
        with TemporaryDirectory() as temporary_text:
            path = Path(temporary_text) / "fixture.mjs"
            path.write_text("marker();\nassert.equal(count, 117);\n", encoding="utf-8")
            changed = refresh.replace_contract(
                path,
                "assert.equal(count, 117);",
                "assert.equal(count, dynamic.length);",
                "marker();",
            )
            self.assertTrue(changed)
            self.assertIn("assert.equal(count, dynamic.length);", path.read_text(encoding="utf-8"))
            detach.assert_called_once_with(path)

    @mock.patch.object(refresh, "detach_for_write")
    def test_embedded_import_contract_remains_idempotent(self, detach):
        with TemporaryDirectory() as temporary_text:
            path = Path(temporary_text) / "fixture.mjs"
            old = 'import { one } from "./module.mjs";\n'
            marker = 'import { one, two } from "./module.mjs";\n'
            path.write_text(marker, encoding="utf-8")
            changed = refresh.replace_contract(path, old, marker, marker)
            self.assertFalse(changed)
            self.assertEqual(path.read_text(encoding="utf-8"), marker)
            detach.assert_not_called()

    @mock.patch.object(refresh, "detach_for_write")
    def test_newer_governed_dynamic_contract_alias_is_idempotent(self, detach):
        with TemporaryDirectory() as temporary_text:
            path = Path(temporary_text) / "price-watch-email.test.mjs"
            current = "assert.deepEqual(emitted, [...new Set([...approved, ...staged])].sort());\n"
            path.write_text(current, encoding="utf-8")
            changed = refresh.replace_contract(
                path,
                "assert.equal(emitted.length, 117);",
                "assert.equal(emitted.length, CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES.length);",
                (
                    "assert.equal(emitted.length, CONTENT_UPGRADE_TERMS_CAPTURE_SOURCES.length);",
                    "assert.deepEqual(emitted, [...new Set([...approved, ...staged])].sort());",
                ),
            )
            self.assertFalse(changed)
            self.assertEqual(path.read_text(encoding="utf-8"), current)
            detach.assert_not_called()

    @mock.patch.object(refresh, "detach_for_write")
    def test_prior_provider_dynamic_served_contract_normalizes_once(self, detach):
        with TemporaryDirectory() as temporary_text:
            path = Path(temporary_text) / "pass2-late-audit-served.test.mjs"
            path.write_text(refresh.SERVED_TEST_ROOT_DYNAMIC + "\n", encoding="utf-8")

            self.assertTrue(refresh.refresh_served_test_contract(path))
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                refresh.SERVED_TEST_ROOT_CANONICAL + "\n",
            )
            self.assertFalse(refresh.refresh_served_test_contract(path))
            detach.assert_called_once_with(path)

    def test_current_release_cardinality_contracts_are_all_recognized(self):
        source_tests = Path(
            "/home/ubuntu/business/site-system/flagship-v2/releases/"
            "glp-visual-prose-authority-20260827/tests"
        )
        with TemporaryDirectory() as temporary_text:
            business = Path(temporary_text)
            candidate = business / "site-system/flagship-v2/candidates/cardinality-contract-test"
            shutil.copytree(source_tests, candidate / "tests")
            for copied in candidate.rglob("*"):
                copied.chmod(0o755 if copied.is_dir() else 0o644)
            with mock.patch.object(refresh, "BUSINESS", business):
                result = refresh.refresh(candidate, "synergy-rx-review")
            self.assertEqual(result["status"], "PASS")
            self.assertGreater(result["changed_count"], 0)

            served = (candidate / "tests/pass2-late-audit-served.test.mjs").read_text(encoding="utf-8")
            static = (candidate / "tests/pass2-late-audit-static-output.test.mjs").read_text(encoding="utf-8")
            phase3 = (candidate / "tests/phase3-page-graph.test.mjs").read_text(encoding="utf-8")
            phase5 = (candidate / "tests/phase5-review-intake.test.mjs").read_text(encoding="utf-8")
            wave2 = (candidate / "tests/wave2-regression.test.mjs").read_text(encoding="utf-8")

            self.assertIn(refresh.SERVED_TEST_ROOT_CANONICAL, served)
            self.assertNotIn(refresh.SERVED_TEST_ROOT_DYNAMIC, served)
            self.assertNotIn("/glp-visual-ultracode-20260821/", served)
            self.assertIn('profile?.canonical_identity?.role !== "alias"', static)
            self.assertIn("const governedProviderIds = new Set(providerRows.map", static)
            self.assertNotIn("const canonicalReviewProviderCount = 34", static)
            self.assertNotIn("assert.equal(programs.length, 36)", static)
            self.assertIn("const governedProviderCount = new Set([", phase3)
            self.assertIn('built.report.family_counts["provider-detail"], governedProviderCount', phase3)
            self.assertIn("providerTags.length, governedProviderCount", phase3)
            self.assertIn("const expectedProviderCount = registry.programs.filter", phase5)
            self.assertNotIn("{ provider: 34, pharmacy: 90, total: 124 }", phase5)
            self.assertIn("selection.programs.length, selection.program_count", wave2)

            with mock.patch.object(refresh, "BUSINESS", business):
                second = refresh.refresh(candidate, "synergy-rx-review")
            self.assertEqual(second["status"], "PASS")
            self.assertEqual(second["changed_count"], 0)

    def test_newer_r2_dynamic_contract_is_accepted_without_rewriting_phase3(self):
        source_tests = Path(
            "/home/ubuntu/business/site-system/flagship-v2/releases/"
            "glp-r2-updater-20260830T025737Z-977a7c10fa/tests"
        )
        with TemporaryDirectory() as temporary_text:
            business = Path(temporary_text)
            candidate = self._candidate_from(
                source_tests, business, "newer-r2-cardinality-contract-test"
            )
            phase3_path = candidate / "tests/phase3-page-graph.test.mjs"
            before = phase3_path.read_bytes()
            with mock.patch.object(refresh, "BUSINESS", business):
                result = refresh.refresh(candidate, "synthetic-new-provider")
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(phase3_path.read_bytes(), before)

    def test_option_c_card_contract_is_accepted_without_inflating_default_grid(self):
        source_tests = Path(
            "/home/ubuntu/business/site-system/flagship-v2/releases/"
            "glp-funnel-v2-pass6-option-c-20260830-a4d60c24/tests"
        )
        with TemporaryDirectory() as temporary_text:
            business = Path(temporary_text)
            candidate = self._candidate_from(
                source_tests, business, "option-c-cardinality-contract-test"
            )
            phase3_path = candidate / "tests/phase3-page-graph.test.mjs"
            before = phase3_path.read_bytes()
            with mock.patch.object(refresh, "BUSINESS", business):
                result = refresh.refresh(candidate, "synthetic-new-provider")
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(phase3_path.read_bytes(), before)
            standalone = (candidate / "tests/pass5-standalone-theme.test.mjs").read_text(
                encoding="utf-8"
            )
            self.assertIn("const affiliatePositions = affiliateAnchors.map", standalone)
            self.assertNotIn("assert.equal(affiliateAnchors.length, 6)", standalone)
