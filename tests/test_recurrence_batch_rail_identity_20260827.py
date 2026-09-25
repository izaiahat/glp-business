#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

BUSINESS = Path("/home/ubuntu/business")


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


worker = load(
    "glp_affiliate_wire_backlog_worker_recurrence_tests",
    BUSINESS / "scripts/glp_affiliate_wire_backlog_worker.py",
)
stage = load(
    "glp_affiliate_deploy_stage_recurrence_tests",
    BUSINESS / "scripts/glp_affiliate_deploy_stage.py",
)


class RailIdentityTests(unittest.TestCase):
    def test_provider_alias_is_not_global_wire_identity(self):
        active = {
            "program_id": "shedrx",
            "network": "Katalys",
            "network_program_id": "1516",
            "tracking_link_sha256": "katalys-hash",
        }
        self.assertTrue(worker.activation_selection_proves_rail(
            active,
            network="Katalys",
            provider_program_id="1516",
            tracking_link_sha256="katalys-hash",
        ))
        self.assertFalse(worker.activation_selection_proves_rail(
            active,
            network="Awin",
            provider_program_id="80963",
            tracking_link_sha256="awin-hash",
        ))

    def test_current_sealed_shedrx_rail_and_retirement_are_exact(self):
        pointer = worker.read_json(worker.CURRENT_BASE)
        release, registry, _document = worker._registry_by_id(pointer)
        activation = worker.activation_selection_by_id(release)["shedrx"]
        retired = worker.retired_rail_rulings(release)
        self.assertTrue(worker.registry_row_proves_wired(registry["shedrx"]))
        self.assertEqual(worker.normalize_network(activation["network"]), "katalys")
        self.assertEqual(str(activation["network_program_id"]), "1516")
        self.assertEqual(int(activation["sheet_row"]), 323)
        self.assertIn(("shedrx", "awin", "80963"), retired)
        self.assertNotIn(("shedrx", "katalys", "1516"), retired)

    def test_current_plan_classifies_awin_194_as_expected_retired_not_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = worker.build_plan(
                backrun_dir=(BUSINESS / "reports/glp-clinic-scout/affiliate-machine/"
                             "nonready-remediation-20260826/backrun-final"),
                ledger_path=Path(temporary) / "ledger.json",
                date_pt="2026-08-27",
            )
        retired = {int(row["sheet_row"]): row
                   for row in plan.get("retired_sibling_rails") or []}
        already = {int(row["sheet_row"]): row
                   for row in plan.get("already_wired") or []}
        self.assertIn(194, retired)
        self.assertNotIn(194, already)
        self.assertEqual(retired[194]["disposition"], "expected_retired_sibling_rail")
        self.assertEqual(retired[194]["active_rail"]["network"], "Katalys")
        self.assertEqual(retired[194]["active_rail"]["provider_program_id"], "1516")
        self.assertEqual(retired[194]["active_rail"]["sheet_row"], 323)

    def test_morning_best_terms_stage_keeps_only_exact_winner_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            plan = worker.build_plan(
                backrun_dir=(BUSINESS / "reports/glp-clinic-scout/affiliate-machine/"
                             "nonready-remediation-20260826/backrun-final"),
                ledger_path=Path(temporary) / "ledger.json",
                date_pt="2026-08-27",
            )
        queue = {int(row["sheet_row"]): row for row in plan.get("queue") or []}
        excluded = {
            int(row["sheet_row"]): row
            for row in plan.get("best_terms_nonwinning_rails") or []
        }
        expected_winners = {
            104: ("katalys", "1518"),
            105: ("katalys", "1519"),
            664: ("xentraffic", "3162"),
            667: ("xentraffic", "3159"),
            749: ("xentraffic", "2862"),
        }
        for sheet_row, identity in expected_winners.items():
            self.assertIn(sheet_row, queue)
            self.assertEqual(
                (worker.normalize_network(queue[sheet_row]["network"]),
                 queue[sheet_row]["provider_program_id"]),
                identity,
            )
        expected_losers = {
            650: ("awin", "121970"),
            671: ("xentraffic", "3114"),
            673: ("xentraffic", "3163"),
            756: ("xentraffic", "3151"),
            757: ("xentraffic", "3152"),
            760: ("xentraffic", "3178"),
        }
        for sheet_row, identity in expected_losers.items():
            self.assertIn(sheet_row, excluded)
            self.assertNotIn(sheet_row, queue)
            self.assertEqual(
                (worker.normalize_network(excluded[sheet_row]["network"]),
                 excluded[sheet_row]["provider_program_id"]),
                identity,
            )
        self.assertNotIn(756, worker.best_terms_switches())
        self.assertNotIn(757, worker.best_terms_switches())

    def test_stage_partition_preserves_expected_retired_without_blocker(self):
        plan = {
            "denominator": {"staged_approved_linked_glp_total": 1},
            "queue": [],
            "already_wired": [],
            "retired_sibling_rails": [{"sheet_row": 194}],
            "fail_closed_unmapped": [],
        }
        indexed = stage.row_index(plan)
        self.assertEqual(indexed[194][0], "EXPECTED_RETIRED_SIBLING_RAIL")

    def test_stage_indexes_every_non_deploy_partition_without_ready_leakage(self):
        plan = {
            "denominator": {"staged_approved_linked_glp_total": 5},
            "queue": [{"sheet_row": 10}],
            "already_wired": [],
            "retired_sibling_rails": [{"sheet_row": 11}],
            "best_terms_nonwinning_rails": [{"sheet_row": 12}],
            "sister_dark": [{"sheet_row": 13}],
            "fail_closed_unmapped": [{"sheet_row": 14}],
        }
        indexed = stage.row_index(plan)
        self.assertEqual(len(indexed), 5)
        self.assertEqual(indexed[10][0], "FEED_CANDIDATE")
        self.assertEqual(indexed[11][0], "EXPECTED_RETIRED_SIBLING_RAIL")
        self.assertEqual(indexed[12][0], "BEST_TERMS_NONWINNER_NO_DEPLOY")
        self.assertEqual(indexed[13][0], "SISTER_DARK_NO_SITE_MUTATION")
        self.assertEqual(indexed[14][0], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
