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
HEAD = "a" * 40
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
                  "package-dir": str(self.output)}
        values.update(changes)
        return ["--package-only", *[part for k, v in values.items() for part in ("--" + k, v)]]

    def run_package(self, **changes):
        with contextlib.ExitStack() as stack:
            for name in ("run_live", "run_verify_only", "acquire_lock", "load_authority", "atomic_pointer_cas"):
                stack.enter_context(mock.patch.object(carrier, name, side_effect=AssertionError("forbidden: " + name)))
            stack.enter_context(mock.patch.object(carrier, "CloudflareClient", side_effect=AssertionError("write-capable client")))
            stack.enter_context(mock.patch.object(carrier.subprocess, "run", side_effect=AssertionError("subprocess")))
            stack.enter_context(mock.patch.object(carrier, "read_token", return_value="synthetic-test-token"))
            stack.enter_context(mock.patch.object(carrier.urllib.request.OpenerDirector, "open", side_effect=self.transport))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
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
        self.assertEqual((self.output / "public/index.html").read_bytes(), b"<h1>fixture</h1>")
        self.assertEqual(len(self.requests), 3)

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
        for missing in ("--site-head", "--public-root", "--expected-tree-sha256", "--rollback-deployment-id", "--package-dir"):
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
