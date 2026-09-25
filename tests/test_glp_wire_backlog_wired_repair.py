import importlib.util
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, mock


MODULE_PATH = Path("/home/ubuntu/business/scripts/glp_affiliate_wire_backlog_worker.py")
SPEC = importlib.util.spec_from_file_location("glp_wire_backlog_wired_repair", MODULE_PATH)
assert SPEC and SPEC.loader
worker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = worker
SPEC.loader.exec_module(worker)


def write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return path


class WiredRepairTests(TestCase):
    def fixture(self, root: Path):
        pointer = write_json(root / "current-content-base.json", {
            "schema": "glp-opus-current-content-base-v1",
            "status": "PASS",
            "deployment_id": "deployment-before-repair",
            "release_root": "/absolute/release-before-repair",
            "release_tree_sha256": "a" * 64,
        })
        manifest = write_json(root / "onboard-receipt.json", {
            "machine": "onboarding_manifest",
            "mode": "onboard",
            "complete": False,
            "manifest": {"rows": [{
                "provider": "synergy-rx-review",
                "step": "assets",
                "status": "failed",
            }]},
        })
        candidate = {
            "key": "katalys:1520",
            "network": "Katalys",
            "provider_program_id": "1520",
            "site_program_id": "synergy-rx-review",
            "sheet_row": 103,
            "approval_ready": True,
            "tracking_ready": True,
            "existing_editorial_page": True,
            "evidence_hints": {},
            "friction": [0],
            "sheet": {},
        }
        plan = {
            "schema": worker.PLAN_SCHEMA,
            "status": "PASS",
            "pt_date": "2026-08-28",
            "source_bindings": {},
            "queue": [],
            "already_wired": [candidate],
            "denominator": {
                "already_wired": 1,
                "eligible_or_fail_closed_glp_backlog": 0,
            },
            "morning_two_phase_rollout": {"active": False},
        }
        repair_dir = root / "wired-repairs"
        authority = {
            "schema": worker.WIRED_REPAIR_SCHEMA,
            "status": "authorized",
            "provider": "synergy-rx-review",
            "key": "katalys:1520",
            "pt_date": "2026-08-28",
            "source_deployment_id": "deployment-before-repair",
            "source_release_root": "/absolute/release-before-repair",
            "source_release_tree_sha256": "a" * 64,
            "current_content_base": worker.path_binding(pointer),
            "source_onboarding_manifest": worker.path_binding(manifest),
            "allowed_machine_steps": ["assets"],
            "reason": "Replace missing editorial-scene references with a local logo fallback.",
            "created_at_utc": "2026-08-28T08:00:00Z",
        }
        return pointer, manifest, candidate, plan, repair_dir, authority

    def test_no_receipt_leaves_wired_row_terminal_and_out_of_queue(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pointer, _manifest, _candidate, plan, repair_dir, _authority = self.fixture(root)
            with mock.patch.object(worker, "CURRENT_BASE", pointer), mock.patch.object(
                worker, "WIRED_REPAIR_DIR", repair_dir
            ):
                result = worker.apply_explicit_wired_repairs(
                    plan, {"synergy-rx-review"}
                )
        self.assertEqual(result["queue"], [])
        self.assertEqual(len(result["already_wired"]), 1)
        self.assertEqual(result["wired_repairs"], [])

    def test_valid_receipt_requeues_exactly_one_explicit_wired_row(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pointer, _manifest, _candidate, plan, repair_dir, authority = self.fixture(root)
            write_json(repair_dir / "synergy-rx-review.json", authority)
            with mock.patch.object(worker, "CURRENT_BASE", pointer), mock.patch.object(
                worker, "WIRED_REPAIR_DIR", repair_dir
            ):
                result = worker.apply_explicit_wired_repairs(
                    plan, {"synergy-rx-review", "unrelated-provider"}
                )

        self.assertEqual([row["key"] for row in result["queue"]], ["katalys:1520"])
        self.assertEqual(result["already_wired"], [])
        self.assertEqual(len(result["wired_repairs"]), 1)
        self.assertEqual(result["denominator"]["already_wired"], 0)
        self.assertEqual(result["denominator"]["eligible_or_fail_closed_glp_backlog"], 1)

    def test_wrong_identity_head_or_manifest_binding_is_refused(self):
        mutations = {
            "provider": lambda authority: authority.update(provider="other-provider"),
            "deployment": lambda authority: authority.update(source_deployment_id="wrong"),
            "tree": lambda authority: authority.update(source_release_tree_sha256="b" * 64),
            "manifest_hash": lambda authority: authority["source_onboarding_manifest"].update(
                sha256="c" * 64
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), TemporaryDirectory() as temporary:
                root = Path(temporary)
                pointer, _manifest, _candidate, plan, repair_dir, authority = self.fixture(root)
                mutate(authority)
                write_json(repair_dir / "synergy-rx-review.json", authority)
                with mock.patch.object(worker, "CURRENT_BASE", pointer), mock.patch.object(
                    worker, "WIRED_REPAIR_DIR", repair_dir
                ), self.assertRaises((worker.B2Error, AttributeError)):
                    worker.apply_explicit_wired_repairs(plan, {"synergy-rx-review"})

    def test_begin_reopens_wired_row_only_with_valid_attached_authority(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            pointer, _manifest, candidate, plan, repair_dir, authority = self.fixture(root)
            authority_path = write_json(repair_dir / "synergy-rx-review.json", authority)
            with mock.patch.object(worker, "CURRENT_BASE", pointer), mock.patch.object(
                worker, "WIRED_REPAIR_DIR", repair_dir
            ):
                validation = worker.validate_wired_repair_authority(
                    candidate, plan, worker.path_binding(authority_path)
                )
                candidate["wired_repair_authority"] = validation
                plan["queue"] = [candidate]
                plan["already_wired"] = []
                plan_path = write_json(root / "plan.json", plan)
                ledger_path = root / "ledger.json"
                ledger = worker.fresh_ledger()
                ledger["rows"]["katalys:1520"] = {
                    "key": "katalys:1520",
                    "status": "wired",
                    "pt_date": "2026-08-28",
                    "first_attempted_at_utc": "2026-08-28T07:43:17Z",
                    "wired_at_utc": "2026-08-28T07:45:44Z",
                    "deployment_id": "deployment-before-repair",
                    "sensor": {"status": "PASS", "failure_count": 0},
                }
                worker.day_state(ledger, "2026-08-28")["wired_keys"] = ["katalys:1520"]
                worker.save_ledger(ledger_path, ledger)
                with mock.patch.object(worker, "verify_plan_sources", lambda value: None):
                    result = worker.begin_row(
                        plan_path=plan_path, ledger_path=ledger_path, key="katalys:1520"
                    )

        self.assertEqual(result["status"], "PASS_WIRED_REPAIR_ATTEMPT_STARTED")
        self.assertEqual(result["row"]["status"], "attempted")
        self.assertEqual(len(result["row"]["wired_repair_history"]), 1)

    def test_normal_wired_row_remains_terminal(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            _pointer, _manifest, candidate, plan, _repair_dir, _authority = self.fixture(root)
            plan["queue"] = [candidate]
            plan_path = write_json(root / "plan.json", plan)
            ledger_path = root / "ledger.json"
            ledger = worker.fresh_ledger()
            ledger["rows"]["katalys:1520"] = {
                "key": "katalys:1520", "status": "wired"
            }
            worker.save_ledger(ledger_path, ledger)
            with mock.patch.object(worker, "verify_plan_sources", lambda value: None):
                with self.assertRaisesRegex(worker.B2Error, "row_already_terminal:wired"):
                    worker.begin_row(
                        plan_path=plan_path, ledger_path=ledger_path, key="katalys:1520"
                    )
