"""Portable real-carrier package tests. All provider IO is intercepted."""
import ast
import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_carrier", ROOT / "scripts/glp_deploy_glp34_authority_v1.py")
assert spec is not None and spec.loader is not None
carrier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(carrier)
# Real historical heads, confirmed by read-only GitHub GET during R1 repair.
# Portable tests intercept GH; they do not claim these synthetic bytes were built there.
HEAD = "2927c1ea184d2ed23aa2dd448c8fc0d04df8c37d"
OTHER_HEAD = "30e4a8178101b87ef052e07a0f9f9e9f64e0c17f"
ROLLBACK = "ca0251a2-dd4e-469c-be3b-79ee8805c5f3"


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.public = self.root / "input"
        self.public.mkdir()
        (self.public / "index.html").write_bytes(b"<h1>fixture</h1>")
        rows = [{"file": "index.html", "sha256": hashlib.sha256(b"<h1>fixture</h1>").hexdigest()}]
        self.tree_hash = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
        self.output = self.root / "package"
        self.deployment = {"id": ROLLBACK, "environment": "production",
                           "deployment_trigger": {"metadata": {"branch": "main"}},
                           "latest_stage": {"status": "success"}}
        self.project = {"name": carrier.PROJECT, "production_branch": "main",
                        "subdomain": f"{carrier.PROJECT}.pages.dev",
                        "canonical_deployment": self.deployment}
        self.requests = []
        self.github_requests = []
        self.receipt = self.root / "gate.json"
        self.receipt_data = {"schema": "glp-d1-release-result-v1", "status": "PASS", "head": HEAD,
                             "output_before": {"sha256": self.tree_hash, "files": rows},
                             "output_after": {"sha256": self.tree_hash, "files": rows}}
        self.pin_receipt()

    def pin_receipt(self):
        self.receipt.write_text(json.dumps(self.receipt_data))
        self.receipt_hash = hashlib.sha256(self.receipt.read_bytes()).hexdigest()

    def github_transport(self, command, **kwargs):
        # Only the fixed, explicit GET is permitted; every other subprocess fails.
        self.assertIn(command, [["gh", "api", "--hostname", "github.com", "--method", "GET",
                                "repos/izaiahat/glp-site-candidate/commits/" + head]
                               for head in (HEAD, OTHER_HEAD)])
        self.assertEqual(kwargs, {"check": True, "capture_output": True, "text": True, "timeout": 45})
        self.github_requests.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps({"sha": command[-1].split("/")[-1]}), "")

    def transport(self, request, **kwargs):
        # A write at the actual HTTP boundary fails, rather than succeeding as a mock.
        self.assertEqual(request.get_method(), "GET")
        self.assertIsNone(request.data)
        prefix = carrier.API_BASE + f"/accounts/{carrier.ACCOUNT_ID}/pages/projects/{carrier.PROJECT}"
        self.assertIn(request.full_url, [prefix, prefix + "/deployments/" + ROLLBACK])
        self.requests.append(request.full_url)
        value = self.project if request.full_url == prefix else self.deployment
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"success": True, "result": value}).encode()
        return response

    def command(self, **changes):
        values = {"site-head": HEAD, "public-root": str(self.public),
                  "expected-tree-sha256": self.tree_hash, "rollback-deployment-id": ROLLBACK,
                  "build-receipt": str(self.receipt), "expected-build-receipt-sha256": self.receipt_hash,
                  "package-dir": str(self.output)}
        values.update(changes)
        return ["--package-only", *[part for k, v in values.items() for part in ("--" + k, v)]]

    def run_package(self, **changes):
        with contextlib.ExitStack() as stack:
            for name in ("run_live", "run_verify_only", "acquire_lock", "load_authority", "atomic_pointer_cas"):
                stack.enter_context(mock.patch.object(carrier, name, side_effect=AssertionError("forbidden: " + name)))
            stack.enter_context(mock.patch.object(carrier, "CloudflareClient", side_effect=AssertionError("write-capable client")))
            stack.enter_context(mock.patch.object(carrier.subprocess, "run", side_effect=self.github_transport))
            stack.enter_context(mock.patch.object(carrier.subprocess, "Popen", side_effect=AssertionError("unintercepted subprocess")))
            stack.enter_context(mock.patch.object(carrier, "read_token", return_value="synthetic-test-token"))
            stack.enter_context(mock.patch.object(carrier.urllib.request.OpenerDirector, "open", side_effect=self.transport))
            self.stdout, self.stderr = io.StringIO(), io.StringIO()
            stack.enter_context(contextlib.redirect_stdout(self.stdout))
            stack.enter_context(contextlib.redirect_stderr(self.stderr))
            return carrier.main(self.command(**changes))

    def test_matching_tree_produces_bound_package_without_write_capability(self):
        self.assertEqual(self.run_package(), 0)
        manifest = json.loads((self.output / "PACKAGE-MANIFEST.json").read_text())
        self.assertEqual(manifest["site_head"], HEAD)
        self.assertEqual(manifest["public_root"], str(self.public))
        self.assertEqual(manifest["public_tree_sha256"], self.tree_hash)
        self.assertEqual(manifest["rollback_deployment_id"], ROLLBACK)
        self.assertFalse(manifest["release_authorized"])
        self.assertEqual(manifest["provider_mutations"], 0)
        self.assertEqual(manifest["github_reads"], 1)
        self.assertEqual(manifest["site_repository"], "izaiahat/glp-site-candidate")
        self.assertEqual(manifest["build_receipt"], {"path": str(self.receipt), "sha256": self.receipt_hash,
                                                    "schema": "glp-d1-release-result-v1"})
        self.assertEqual(len(self.github_requests), 1)
        self.assertEqual((self.output / "public/index.html").read_bytes(), b"<h1>fixture</h1>")
        self.assertEqual(len(self.requests), 3)

    def test_zero_site_head_refuses_before_package_write(self):
        self.assertEqual(self.run_package(**{"site-head": "0" * 40}), 1)
        self.assertFalse(self.output.exists())

    def test_real_but_different_site_head_refuses_same_tree_and_receipt(self):
        self.assertEqual(self.run_package(**{"site-head": OTHER_HEAD}), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.requests, [])

    def test_missing_or_tampered_receipt_refuses_before_remote_or_local_writes(self):
        self.receipt.unlink()
        self.assertEqual(self.run_package(), 1)
        self.assertFalse(self.output.exists())
        self.pin_receipt()
        self.receipt.write_bytes(self.receipt.read_bytes() + b"\n")
        self.assertEqual(self.run_package(), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.github_requests, [])

    def test_pinned_receipt_requires_native_schema_pass_head_and_both_tree_inventories(self):
        original = copy.deepcopy(self.receipt_data)
        for changes in ({"schema": "other"}, {"status": "FAIL"}, {"head": OTHER_HEAD},
                        {"output_before": None}, {"output_after": None},
                        {"output_before": {"sha256": "0" * 64, "files": original["output_before"]["files"]}},
                        {"output_after": {"sha256": "0" * 64, "files": original["output_after"]["files"]}},
                        {"output_after": {"sha256": self.tree_hash, "files": []}}):
            with self.subTest(changes=changes):
                self.receipt_data = {**original, **changes}
                self.pin_receipt()
                self.assertEqual(self.run_package(), 1)
                self.assertFalse(self.output.exists())
        self.assertEqual(self.requests, [])
        self.assertEqual(self.github_requests, [])

    def test_malformed_receipt_bytes_or_hash_refuse(self):
        for raw in (b"not-json", b"null", b"[]"):
            self.receipt.write_bytes(raw)
            self.receipt_hash = hashlib.sha256(raw).hexdigest()
            self.assertEqual(self.run_package(), 1)
            self.assertFalse(self.output.exists())
        self.pin_receipt()
        for digest in ("wrong", "0" * 64):
            self.assertEqual(self.run_package(**{"expected-build-receipt-sha256": digest}), 1)
        self.assertEqual(self.github_requests, [])

    def test_receipt_symlink_refuses(self):
        link = self.root / "linked-gate.json"
        link.symlink_to(self.receipt)
        self.assertEqual(self.run_package(**{"build-receipt": str(link)}), 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.github_requests, [])

    def test_receipt_drift_during_copy_leaves_no_package(self):
        original = carrier.shutil.copyfileobj
        def copy_then_drift(*args, **kwargs):
            original(*args, **kwargs)
            self.receipt.write_bytes(self.receipt.read_bytes() + b"\n")
        with mock.patch.object(carrier.shutil, "copyfileobj", side_effect=copy_then_drift):
            self.assertEqual(self.run_package(), 1)
        self.assertFalse(self.output.exists())

    def test_missing_commit_and_github_errors_refuse_before_cloudflare_or_output(self):
        for error in (subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 404"),
                      subprocess.TimeoutExpired(["gh"], 45), FileNotFoundError("gh")):
            with self.subTest(error=type(error).__name__), mock.patch.object(self, "github_transport", side_effect=error):
                self.assertEqual(self.run_package(), 1)
                self.assertFalse(self.output.exists())
        self.assertEqual(self.requests, [])

    def test_github_wrong_commit_or_malformed_response_refuses(self):
        for raw in (json.dumps({"sha": OTHER_HEAD}), "null", "[]", "invalid"):
            with mock.patch.object(self, "github_transport", return_value=subprocess.CompletedProcess([], 0, raw, "")):
                self.assertEqual(self.run_package(), 1)
                self.assertFalse(self.output.exists())
        self.assertEqual(self.requests, [])

    def test_one_changed_byte_refuses_before_provider_read_or_package_write(self):
        (self.public / "index.html").write_bytes(b"<h1>Fixture</h1>")
        self.assertEqual(self.run_package(), 1)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.output.exists())

    def test_wrong_rollback_id_refuses(self):
        self.assertEqual(self.run_package(**{"rollback-deployment-id": "1" * 8 + "-1111-1111-1111-111111111111"}), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(self.output.exists())

    def test_rollback_metadata_refuses_preview_failed_or_wrong_branch(self):
        for changes in ({"environment": "preview"}, {"latest_stage": {"status": "failure"}},
                        {"deployment_trigger": {"metadata": {"branch": "feature"}}}):
            original = copy.deepcopy(self.deployment)
            self.deployment.update(changes)
            self.assertEqual(self.run_package(), 1)
            self.assertFalse(self.output.exists())
            self.deployment.clear()
            self.deployment.update(original)

    def test_detail_mismatch_refuses(self):
        self.project["canonical_deployment"] = copy.deepcopy(self.deployment)
        self.deployment["id"] = "1" * 8 + "-1111-1111-1111-111111111111"
        self.assertEqual(self.run_package(), 1)
        self.assertFalse(self.output.exists())

    def test_canonical_drift_during_copy_leaves_no_package(self):
        original = carrier.shutil.copyfileobj
        def copy_then_drift(*args, **kwargs):
            original(*args, **kwargs)
            self.deployment["id"] = "1" * 8 + "-1111-1111-1111-111111111111"
        with mock.patch.object(carrier.shutil, "copyfileobj", side_effect=copy_then_drift):
            self.assertEqual(self.run_package(), 1)
        self.assertFalse(self.output.exists())

    def test_target_drift_final_get_refuses_without_success_output(self):
        original = self.transport
        def final_get_then_drift(*args, **kwargs):
            response = original(*args, **kwargs)
            if len(self.requests) == 3:
                (self.output / "public/index.html").write_bytes(b"changed during final rollback read")
            return response
        with mock.patch.object(self, "transport", side_effect=final_get_then_drift):
            result = self.run_package()
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(self.github_requests), 1)
        self.assertEqual(result, 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(json.loads(self.stderr.getvalue()),
                         {"status": "BLOCK", "error": "package_final_tree_mismatch"})
        self.assertFalse(self.output.exists())

    def test_target_drift_at_manifest_write_is_still_checked(self):
        original = carrier.exclusive_file
        def write_then_drift(*args, **kwargs):
            original(*args, **kwargs)
            # New members must be found, not just rehashing the original rows.
            (self.output / "public/unlisted.html").write_bytes(b"late addition")
        with mock.patch.object(carrier, "exclusive_file", side_effect=write_then_drift):
            self.assertEqual(self.run_package(), 1)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertEqual(json.loads(self.stderr.getvalue()),
                         {"status": "BLOCK", "error": "package_final_tree_mismatch"})
        self.assertFalse(self.output.exists())

    def test_persisted_manifest_must_match_intended_bytes(self):
        original = carrier.exclusive_file
        source_tree = carrier.package_tree(self.public)
        receipt_bytes = self.receipt.read_bytes()
        for field in ("site_head", "build_receipt", "files", "rollback_deployment_id", None):
            with self.subTest(field=field):
                self.output = self.root / ("package-" + (field or "control"))
                self.requests.clear()
                self.github_requests.clear()
                intended = []

                def write_then_replace(path, data, **kwargs):
                    self.assertEqual(path, self.output / "PACKAGE-MANIFEST.json")
                    original(path, data, **kwargs)
                    self.assertEqual(path.read_bytes(), data)
                    intended.append(data)
                    if field is not None:
                        manifest = json.loads(data)
                        if field == "build_receipt":
                            manifest[field]["sha256"] = "0" * 64
                        else:
                            manifest[field] = {"site_head": OTHER_HEAD, "files": [],
                                               "rollback_deployment_id": "11111111-1111-1111-1111-111111111111"}[field]
                        # Mode 0444 does not prevent replacement in our directory.
                        path.unlink()
                        original(path, carrier.json_bytes(manifest), mode=0o444)
                        self.assertNotEqual(path.read_bytes(), data)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o444)
                    self.assertEqual(carrier.package_tree(self.output / "public"), source_tree)

                with mock.patch.object(carrier, "exclusive_file", side_effect=write_then_replace):
                    result = self.run_package()
                self.assertEqual(len(intended), 1)
                self.assertEqual(len(self.requests), 3)
                self.assertEqual(len(self.github_requests), 1)
                self.assertEqual(self.receipt.read_bytes(), receipt_bytes)
                self.assertEqual(carrier.package_tree(self.public), source_tree)
                if field is None:
                    self.assertEqual(result, 0)
                    self.assertEqual(self.stderr.getvalue(), "")
                    raw = (self.output / "PACKAGE-MANIFEST.json").read_bytes()
                    self.assertEqual(raw, intended[0])
                    success = json.loads(self.stdout.getvalue())
                    self.assertEqual(success["status"], "PASS_PACKAGE_ONLY")
                    self.assertEqual(success["manifest_sha256"], hashlib.sha256(raw).hexdigest())
                    self.assertEqual(carrier.package_tree(self.output / "public"), source_tree)
                else:
                    self.assertEqual(result, 1)
                    self.assertEqual(self.stdout.getvalue(), "")
                    self.assertEqual(json.loads(self.stderr.getvalue()),
                                     {"status": "BLOCK", "error": "package_final_manifest_mismatch"})
                    self.assertFalse(self.output.exists())

    def test_source_drift_during_copy_leaves_no_manifest(self):
        original = carrier.shutil.copyfileobj
        def copy_then_drift(*args, **kwargs):
            original(*args, **kwargs)
            (self.public / "index.html").write_bytes(b"changed")
        with mock.patch.object(carrier.shutil, "copyfileobj", side_effect=copy_then_drift):
            self.assertEqual(self.run_package(), 1)
        self.assertFalse(self.output.exists())

    def test_invalid_explicit_bindings_refuse(self):
        for changes in ({"site-head": "main"}, {"expected-tree-sha256": "wrong"},
                        {"rollback-deployment-id": "../rollback"}, {"public-root": "relative"},
                        {"package-dir": str(self.public / "nested")}):
            self.assertEqual(self.run_package(**changes), 1)
            self.assertFalse(self.output.exists())
        self.assertEqual(self.requests, [])

    def test_existing_output_is_never_replaced(self):
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_bytes(b"untouched")
        self.assertEqual(self.run_package(), 1)
        self.assertEqual(sentinel.read_bytes(), b"untouched")
        self.assertEqual(self.requests, [])

    def test_symlink_and_empty_tree_refuse(self):
        (self.public / "link").symlink_to(self.public / "index.html")
        self.assertEqual(self.run_package(), 1)
        (self.public / "link").unlink()
        (self.public / "index.html").unlink()
        self.assertEqual(self.run_package(), 1)
        self.assertFalse(self.output.exists())

    def test_no_redirect_or_arbitrary_read_endpoint(self):
        with self.assertRaises(carrier.GateError):
            carrier.PackageNoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid")
        with self.assertRaises(carrier.GateError):
            carrier.package_get("/arbitrary", "synthetic")

    def test_cli_requires_every_binding_and_forbids_mixed_modes(self):
        for missing in ("--site-head", "--public-root", "--expected-tree-sha256", "--build-receipt",
                        "--expected-build-receipt-sha256", "--rollback-deployment-id", "--package-dir"):
            args = self.command()
            index = args.index(missing)
            del args[index:index + 2]
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                carrier.main(args)
        for mode in ("--live", "--verify-only"):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                carrier.main(self.command() + [mode])
        self.assertFalse(self.output.exists())

    def test_tree_hash_matches_site_recursive_order_not_flat_path_sort(self):
        # JS orders 'a' before 'a.txt', then recurses before returning to a.txt.
        (self.public / "index.html").unlink()
        (self.public / "a").mkdir()
        (self.public / "a/z").write_bytes(b"z")
        (self.public / "a.txt").write_bytes(b"a")
        (self.public / "é.txt").write_bytes(b"e")
        rows = [{"file": name, "sha256": hashlib.sha256(data).hexdigest()}
                for name, data in [("a/z", b"z"), ("a.txt", b"a"), ("é.txt", b"e")]]
        expected = hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(carrier.package_tree(self.public), (expected, rows))


class LineageTests(unittest.TestCase):
    def test_all_legacy_code_unchanged_except_main_name(self):
        tree = ast.parse((ROOT / "scripts/glp_deploy_glp34_authority_v1.py").read_text())
        boundary = next(i for i, node in enumerate(tree.body) if isinstance(node, ast.ClassDef) and node.name == "PackageNoRedirect")
        tree.body = tree.body[1:boundary]  # omit module docstring; only legacy code
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "legacy_main":
                node.name = "main"
        for node in ast.walk(tree):
            # Python 3.12 added empty type_params fields; no legacy generics exist.
            if hasattr(node, "type_params"):
                self.assertEqual(node.type_params, [])
                node._fields = tuple(field for field in node._fields if field != "type_params")
        self.assertEqual(len(tree.body), 115)
        # Authenticated original e8473902..., curated unchanged in 583f6fe.
        self.assertEqual(hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest(),
                         "879a22829ffbf39459e9dd75fef1c64e9fabb1b69e96d9f83828087b8b85c106")

    def test_legacy_cli_dispatch_uses_original_gates_without_package(self):
        # Both modes exercised ONLY with terminal gates stubbed: no real live execution.
        for mode, target in [("--verify-only", "run_verify_only"), ("--live", "run_live")]:
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(carrier, "acquire_lock", return_value=-1))
                authority = stack.enter_context(mock.patch.object(carrier, "load_authority", return_value=({"legacy": True}, "hash")))
                chosen = stack.enter_context(mock.patch.object(carrier, target, return_value={"fixture": True}))
                stack.enter_context(mock.patch.object(carrier, "run_package_only", side_effect=AssertionError("package reached")))
                stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                self.assertEqual(carrier.main([mode]), 0)
                authority.assert_called_once_with()
                chosen.assert_called_once_with(*(({"legacy": True}, "hash") if mode == "--verify-only" else ({"legacy": True}, "hash", -1)))

    def test_generator_reproduces_committed_carrier_and_historical_test(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "generated"
            subprocess.run([sys.executable, str(ROOT / "scripts/generate_glp34_deploy_carrier.py"),
                            "--prepare", "--emit-dir", str(output)], check=True, capture_output=True)
            for folder, name in [("scripts", "glp_deploy_glp34_authority_v1.py"), ("tests", "test_glp_deploy_glp34_authority_v1.py")]:
                self.assertEqual((output / name).read_bytes(), (ROOT / folder / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
