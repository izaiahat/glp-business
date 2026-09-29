"""Offline archiver authority regression; synthetic evidence is not live approval."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
# The offload module is deliberately omitted from the curated source mirror.
sys.modules.setdefault("r2_offload", types.ModuleType("r2_offload"))
import reports_archiver as archiver


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, sort_keys=True).encode()
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest()


class PreparedConsumer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.reports = root / "reports"
        self.flagship = root / "site-system/flagship-v2"
        self.current = root / "reports/ops/glp43-prepared/release"
        self.previous = self.flagship / "releases/previous"
        self.rollback = self.flagship / "releases/rollback"
        for path in (self.current, self.previous, self.rollback):
            path.mkdir(parents=True)
        self.base_path = self.reports / "glp-clinic-scout/seo-machine/current-content-base.json"
        deployment = "11111111-1111-1111-1111-111111111111"
        prior = "22222222-2222-2222-2222-222222222222"
        evidence = self.base_path.parent / ("current-release-" + "a" * 40)
        manifest = evidence / "01-deployment-manifest.json"
        receipt = evidence / "10-final-publication-receipt.json"
        tree, public = "b" * 64, "c" * 64
        manifest_sha = put(manifest, {"schema": "glp-opus-release-deployment-manifest-v1",
            "release_root": str(self.current), "release_tree_sha256": tree})
        receipt_sha = put(receipt, {"schema": "glp-opus-production-terminal-receipt-v1",
            "status": "PASS_PRODUCTION_TERMINAL", "deployment": {"deployment_id": deployment},
            "evidence_sha256": {manifest.name: manifest_sha},
            "immutable_closure": {"release_tree_sha256": tree, "public_tree_sha256": public}})
        self.pointer = {"schema": "glp-opus-current-content-base-v1", "status": "PASS",
            "deployment_id": deployment, "release_root": str(self.current),
            "release_tree_sha256": tree, "public_tree_sha256": public,
            "site_head": "a" * 40, "deploy_directory_sha256": "d" * 64,
            "manifest": {"path": str(manifest), "sha256": manifest_sha},
            "final_receipt": {"path": str(receipt), "sha256": receipt_sha},
            "supersedes": {"deployment_id": prior, "release_root": str(self.previous)},
            "rollback_release_root": str(self.rollback)}
        # The real schema disallows a rollback outer key; use the receipt instead.
        del self.pointer["rollback_release_root"]
        receipt_payload = json.loads(receipt.read_bytes())
        receipt_payload["rollback_release_root"] = str(self.rollback)
        self.pointer["final_receipt"]["sha256"] = put(receipt, receipt_payload)
        old_manifest = evidence.parent / "base-remint-22222222-fixture/01-deployment-manifest.json"
        old_receipt = old_manifest.with_name("10-final-publication-receipt.json")
        old_sha = put(old_manifest, {"schema": "glp-opus-release-deployment-manifest-v1",
            "release_root": str(self.previous)})
        put(old_receipt, {"schema": "glp-opus-production-terminal-receipt-v1",
            "status": "PASS_PRODUCTION_TERMINAL", "deployment": {"deployment_id": prior},
            "evidence_sha256": {old_manifest.name: old_sha}})
        put(self.base_path, self.pointer)
        for name, value in (("REPORTS", self.reports), ("FLAGSHIP", self.flagship)):
            p = patch.object(archiver, name, value)
            p.start(); self.addCleanup(p.stop)
        self.calls = []
        self.bound_digest = hashlib.sha256(self.base_path.read_bytes()).hexdigest()
        weekly = types.ModuleType("glp_price_index_weekly")
        def synthetic_validator(pointer, release):
            self.calls.append(str(release))
            if (str(release) != str(self.current) or
                    hashlib.sha256(self.base_path.read_bytes()).hexdigest() != self.bound_digest):
                raise RuntimeError("synthetic_preparation_drift")
            return {"prepared_binding": {"files": {str(self.base_path): self.bound_digest}}}
        weekly._validate_base_evidence = synthetic_validator
        p = patch.dict(sys.modules, {"glp_price_index_weekly": weekly})
        p.start(); self.addCleanup(p.stop)
        # Explicit effect denial: this test only reads its temporary authority.
        p = patch.object(archiver, "r2_offload", types.SimpleNamespace(
            upload=lambda *a, **k: self.fail("offload forbidden")))
        p.start(); self.addCleanup(p.stop)

    def test_prepared_native_seam_protects_current_predecessor_and_rollback(self):
        result = archiver.load_site_tree_authority()
        self.assertEqual(result["status"], "VALID")
        self.assertEqual(len(self.calls), 2)  # native seam and post-consumption recheck
        self.assertEqual(result["refs"], {self.current, self.previous, self.rollback})
        for root in result["refs"]:
            for candidate in (root, root.parent, root / "nested"):
                self.assertTrue(archiver._reference_protects(candidate, result["refs"]))
        self.assertFalse(archiver._reference_protects(self.flagship / "staging/unrelated", result["refs"]))

    def test_refuses_missing_native_binding_and_drift(self):
        with patch.dict(sys.modules, {"glp_price_index_weekly": types.SimpleNamespace(
                _validate_base_evidence=lambda *a: {"prepared_binding": None})}):
            with self.assertRaisesRegex(archiver.SiteAuthorityError, "prepared_binding_missing"):
                archiver.load_site_tree_authority()
        self.bound_digest = "0" * 64
        with self.assertRaisesRegex(archiver.SiteAuthorityError, "synthetic_preparation_drift"):
            archiver.load_site_tree_authority()

        self.bound_digest = hashlib.sha256(self.base_path.read_bytes()).hexdigest()
        calls = []
        def changed_binding(*_):
            calls.append(1)
            return {"prepared_binding": {"files": {"preparation": str(len(calls))}}}
        with patch.object(archiver, "_registered_prepared_release_evidence", changed_binding):
            with self.assertRaisesRegex(archiver.SiteAuthorityError, "prepared_binding_changed"):
                archiver.load_site_tree_authority()
        self.assertEqual(len(calls), 2)

        self.previous.rmdir()
        with self.assertRaisesRegex(archiver.SiteAuthorityError, "supersedes_release_root_invalid"):
            archiver.load_site_tree_authority()

    def test_legacy_or_unproved_root_cannot_acquire_reports_lane(self):
        altered = dict(self.pointer)
        altered.pop("site_head")
        altered.pop("deploy_directory_sha256")
        put(self.base_path, altered)
        with self.assertRaisesRegex(archiver.SiteAuthorityError, "release_root_invalid"):
            archiver.load_site_tree_authority()
        altered["site_head"] = "a" * 40
        # Partial prepared identity cannot trigger the prepared branch.
        put(self.base_path, altered)
        with self.assertRaisesRegex(archiver.SiteAuthorityError, "fields_invalid"):
            archiver.load_site_tree_authority()


if __name__ == "__main__":
    unittest.main()
