"""Monthly command/policy regression only; never a registered live run."""
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class StopBeforeCollector(BaseException):
    pass


class MonthlyProfitabilityTests(unittest.TestCase):
    def setUp(self):
        self.cadence = load("monthly_cadence", "scripts/glp_analytics_brain_cadence.py")
        self.brain = load("monthly_brain", "systems/analytics-brain/analytics_brain.py")

    def capture(self, phase, **options):
        captured = []

        def runner(argv, **kwargs):
            captured.append((argv, kwargs))
            raise StopBeforeCollector

        # The sentinel stops the actual maintained wrapper before provider,
        # receipt, alert or database effects. Neither collector nor CLI runs.
        with patch.object(self.cadence, "deploy_watch_active", return_value=False), \
                patch.object(self.cadence, "trigger_deploy", return_value={}), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("no process")), \
                patch.object(self.cadence, "send_alert", side_effect=AssertionError("no send")):
            with self.assertRaises(StopBeforeCollector):
                self.cadence._run_admitted(phase, runner=runner, **options)
        self.assertEqual(len(captured), 1)
        argv, kwargs = captured[0]
        self.assertEqual(kwargs, dict(cwd=ROOT, text=True, capture_output=True, timeout=1200))
        return argv

    def test_monthly_actual_argv_selects_profitability(self):
        control = json.loads((ROOT / "ops/glp/analytics-brain-control.json").read_text())
        self.assertEqual(control["cadences"]["monthly"],
                         "network revenue import and provider or placement profitability")
        argv = self.capture("monthly")
        self.assertEqual(argv, [sys.executable, str(self.cadence.COLLECTOR),
                                "--phase", "monthly", "--decision-class", "profitability"])

    def test_other_collector_phases_keep_existing_argv(self):
        for phase in ("hourly", "daily", "route-census", "weekly", "deploy", "deploy-trigger"):
            with self.subTest(phase=phase):
                # Fixture is an argv-only placeholder, never read or launched.
                argv = self.capture(phase, fixture=Path("offline-unused.json"))
                mapped = "deploy" if phase == "deploy-trigger" else phase
                self.assertEqual(argv, [sys.executable, str(self.cadence.COLLECTOR),
                                       "--phase", mapped, "--fixture", "offline-unused.json"])

    def test_monthly_unavailable_network_is_hold_with_null_revenue(self):
        # Labelled synthetic contract inputs, no commercial events or money.
        sources = {name: {"state": "healthy", "actual_rows": 1,
                          "host_scope": self.brain.PRODUCTION_HOST if name in {"ga4", "posthog"} else None,
                          "host_filter": {"exact": self.brain.PRODUCTION_HOST} if name in {"ga4", "posthog"} else None}
                   for name in self.brain.SEARCH_SOURCES}
        sources["network_revenue"] = {"state": "unavailable", "reason": "not_imported"}
        for source in sources.values():
            if source["state"] == "healthy":
                source["watermark"] = "2026-10-08T00:00:00Z"
        static = {"path": "/", "http_status": 200, "ga4_static_present": True,
                  "gtm_static_present": True, "posthog_static_present": True,
                  "csp_present": True, "csp_gtm_allowed": True, "csp_posthog_allowed": True,
                  "consent_default_denied_present": True, "affiliate_route_expected": False}
        data = {"sources": sources, "sitemap_complete": True, "sitemap_routes": ["/"],
                "static_routes": [static], "revenue_events": []}
        argv = self.capture("monthly")
        selected = argv[argv.index("--decision-class") + 1] if "--decision-class" in argv else "search"
        with patch.object(self.brain.sqlite3, "connect", side_effect=AssertionError("no database")):
            outputs = self.brain.build_outputs(data, decision_class=selected)
            search = self.brain.build_outputs(data, decision_class="search")
        health = outputs["analytics-source-health.json"]
        self.assertEqual(search["analytics-source-health.json"]["mode"], "PASS_SEARCH_ONLY")
        self.assertEqual(health["mode"], "HOLD_REVENUE_OPTIMIZATION")
        self.assertFalse(health["allowed"])
        self.assertEqual(outputs["decision-receipt.json"]["status"], "HOLD_NO_DECISIONS_REQUESTED")
        network = next(row for row in health["sources"] if row["source"] == "network_revenue")
        self.assertEqual(network["state"], "unavailable")
        self.assertIsNone(network["actual_rows"])
        self.assertIsNone(network["expected_rows"])
        self.brain.assert_null_revenue(outputs)


if __name__ == "__main__":
    unittest.main()
