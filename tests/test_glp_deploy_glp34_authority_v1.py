#!/usr/bin/env python3
"""Adversarial provider-free tests for the Build 6 r3 authority carrier."""
from __future__ import annotations

import base64
import fcntl
import importlib.util
import json
import os
import shutil
import stat
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

CARRIER_PATH = Path(os.environ.get(
    "GLP_BUILD6_CARRIER_PATH",
    "/home/ubuntu/business/scripts/glp_deploy_glp34_authority_v1.py",
))
SPEC = importlib.util.spec_from_file_location("glp_deploy_build2_authority_v3", CARRIER_PATH)
carrier = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(carrier)


class CrashInjection(BaseException):
    pass


class CarrierV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pointer_before, cls.pointer_stat_before = carrier.capture_pointer(carrier.POINTER)

    @classmethod
    def tearDownClass(cls):
        after, after_stat = carrier.capture_pointer(carrier.POINTER)
        if after != cls.pointer_before:
            raise AssertionError("tests changed production pointer bytes")
        if (after_stat.st_dev, after_stat.st_ino) != (
            cls.pointer_stat_before.st_dev, cls.pointer_stat_before.st_ino
        ):
            raise AssertionError("tests changed production pointer inode")

    def setUp(self):
        carrier._CAS_FAULT_HOOK = None

    def tearDown(self):
        carrier._CAS_FAULT_HOOK = None

    @contextmanager
    def cas_fixture(self, *, artifacts: bool = True):
        with tempfile.TemporaryDirectory(prefix="glp-r13-cas-test-") as temp:
            root = Path(temp)
            pointer = root / "current.json"
            lock_path = root / "weekly.lock"
            predecessor = carrier.json_bytes({"schema": "test", "state": "predecessor"})
            successor = carrier.json_bytes({
                "schema": "test", "state": "successor", "release_root": "/release/r13",
                "deployment_id": "00000000-0000-0000-0000-000000000000",
                "manifest": {"path": "/manifest"}, "final_receipt": {"path": "/receipt"},
            })
            carrier.exclusive_file(pointer, predecessor, 0o600)
            lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            st = pointer.lstat()
            artifact_rows = []
            if artifacts:
                body = carrier.json_bytes({"artifact": "exact"})
                artifact_rows.append({
                    "path": str(root / "seo-report/manifest.json"), "mode": "0600",
                    "sha256": carrier.sha_bytes(body),
                    "bytes_base64": base64.b64encode(body).decode(),
                })
            intent = {
                "pointer_path": str(pointer), "lock_path": str(lock_path),
                "predecessor_bytes_base64": base64.b64encode(predecessor).decode(),
                "successor_bytes_base64": base64.b64encode(successor).decode(),
                "predecessor_stat": {
                    "st_dev": st.st_dev, "st_ino": st.st_ino, "uid": st.st_uid,
                    "gid": st.st_gid, "mode": "0600", "nlink": 1,
                },
                "final_mode": "0600", "final_uid": st.st_uid, "final_gid": st.st_gid,
                "temp_name": ".current.json.r13-test.tmp", "artifacts": artifact_rows,
            }
            try:
                yield root, pointer, lock_path, lock_fd, predecessor, successor, intent
            finally:
                os.close(lock_fd)

    def test_cli_accepts_only_two_fixed_modes(self):
        self.assertTrue(carrier.parse_args(["--verify-only"]).verify_only)
        self.assertTrue(carrier.parse_args(["--live"]).live)
        for argv in ([], ["--verify-only", "--live"], ["--live", "--release", "/tmp/x"],
                     ["--live", "--hash", "0" * 64], ["--live", "--predecessor", "x"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                carrier.parse_args(argv)

    def test_trusted_path_has_no_npx_npm_or_host_remint_execution(self):
        source = CARRIER_PATH.read_text(encoding="utf-8")
        self.assertNotIn('["npx"', source)
        self.assertNotIn('["npm"', source)
        self.assertNotIn("remint_content_base.py", source)
        self.assertIn("os.replace(temp_name, pointer.name", source)
        self.assertIn("os.O_EXCL", source)
        self.assertIn("os.fsync(parent_fd)", source)
        self.assertIn("str(NODE), str(WRANGLER)", source)

    def test_pre_effect_order_is_sensor_then_direct_build_then_provider(self):
        source = CARRIER_PATH.read_text(encoding="utf-8")
        body = source[source.index("def run_live("):source.index("def parse_args(")]
        sensor_at = body.index("run_actual_preflight_sensor")
        build_at = body.index("run_direct_wrangler_probe")
        provider_at = body.index("CloudflareClient(read_token())")
        self.assertLess(sensor_at, build_at)
        self.assertLess(build_at, provider_at)

    def test_terminal_pass_requires_post_cas_live_pass_zero_sensor(self):
        source = CARRIER_PATH.read_text(encoding="utf-8")
        body = source[source.index("def run_live("):source.index("def parse_args(")]
        pointer_at = body.index("atomic_pointer_cas")
        live_sensor_at = body.index("run_live_completeness_sensor", pointer_at)
        live_facet_at = body.index("run_facet_binding_sensor", live_sensor_at)
        terminal_at = body.index('receipt["status"] = "PASS_PRODUCTION_TERMINAL"')
        self.assertLess(pointer_at, live_sensor_at)
        self.assertLess(live_sensor_at, live_facet_at)
        self.assertLess(live_facet_at, terminal_at)

    def test_post_cas_validation_failure_terminalizes_blocked(self):
        source = CARRIER_PATH.read_text()
        body = source[source.index("def run_live("):]
        pointer_successor_at = body.index("if successor and current_pointer == successor:")
        blocked_receipt_at = body.index('receipt["status"] = "BLOCKED_POST_CAS_VALIDATION"', pointer_successor_at)
        blocked_state_at = body.index('state["status"] = "CONSUMED_BLOCKED_POST_CAS_VALIDATION"', blocked_receipt_at)
        raise_at = body.index("raise", blocked_state_at)
        self.assertLess(pointer_successor_at, blocked_receipt_at)
        self.assertLess(blocked_receipt_at, blocked_state_at)
        self.assertLess(blocked_state_at, raise_at)

    def test_crash_resume_reconciles_prior_provider_mutation_truthfully(self):
        source = CARRIER_PATH.read_text()
        self.assertIn("reconciled_prior_provider_mutation = True", source)
        self.assertIn('"provider_mutations": 1', source)
        self.assertIn('"provider_calls_prior_process": "unknown_nonzero"', source)
        self.assertIn("max(client.provider_mutations, 1 if reconciled_prior_provider_mutation else 0)", source)
        self.assertIn('"provider_calls": (None if reconciled_prior_provider_mutation else', source)

    def test_provider_head_uses_only_cloudflare_canonical_deployment(self):
        canonical = {
            "id": "11111111-1111-1111-1111-111111111111",
            "environment": "production",
            "latest_stage": {"status": "success"},
            "deployment_trigger": {"metadata": {"branch": "main"}},
        }
        project = {
            "name": carrier.PROJECT,
            "production_branch": carrier.PRODUCTION_BRANCH,
            "subdomain": f"{carrier.PROJECT}.pages.dev",
            "canonical_deployment": canonical,
            "latest_deployment": {"id": "22222222-2222-2222-2222-222222222222"},
        }
        self.assertIs(carrier.production_deployment(project), canonical)
        without_canonical = dict(project)
        without_canonical.pop("canonical_deployment")
        with self.assertRaisesRegex(carrier.GateError, "no_canonical_deployment"):
            carrier.production_deployment(without_canonical)

    def test_cloudflare_commit_identity_is_git_shaped_while_authority_keeps_sha256(self):
        source = CARRIER_PATH.read_text(encoding="utf-8")
        self.assertIn('["tree_sha256"][:40]', source)
        self.assertIn('payload["release"]["closure"]', source)

    def test_toolchain_entrypoints_accept_only_closure_bound_hardlinks(self):
        source = CARRIER_PATH.read_text(encoding="utf-8")
        self.assertIn('verify_immutable_tree(TOOLCHAIN, payload["toolchain"]["closure"], toolchain=True)', source)
        self.assertIn('verify_file_spec(WRANGLER, payload["toolchain"]["wrangler_js"], nlink_one=False)', source)
        self.assertIn('verify_file_spec(ESBUILD, payload["toolchain"]["esbuild"], nlink_one=False)', source)

    def test_bootstrap_runtimes_are_exact_regular_files_not_symlink_aliases(self):
        self.assertEqual(carrier.PYTHON, Path("/usr/bin/python3.12"))
        self.assertFalse(carrier.PYTHON.is_symlink())
        self.assertFalse(carrier.OPENSSL.is_symlink())
        carrier.require_regular(carrier.PYTHON, root_owned=True)
        carrier.require_regular(carrier.OPENSSL, root_owned=True)

    def test_wrangler_environment_declares_only_pinned_executable_path(self):
        with tempfile.TemporaryDirectory(prefix="glp-r13-env-") as temp:
            env = carrier.wrangler_env(Path(temp))
        self.assertEqual(env["PATH"], str(carrier.TOOLCHAIN / "runtime"))
        self.assertEqual(env["ESBUILD_BINARY_PATH"], str(carrier.ESBUILD))
        self.assertEqual(env["WRANGLER_HIDE_BANNER"], "true")
        self.assertEqual(env["WRANGLER_SEND_METRICS"], "false")
        self.assertNotIn("CLOUDFLARE_API_TOKEN", env)
        self.assertNotIn("NPM_CONFIG_REGISTRY", env)

    def preflight_fixture(self):
        return {"status": "PASS_PARTIAL", "failed_checks": [], "failure_count": 0,
                "skipped_checks": ["base_chain"], "effects": carrier.EXPECTED_ZERO_EFFECTS,
                "release_root": str(carrier.RELEASE),
                "checks": ([{"check": name, "status": "PASS"}
                            for name in carrier.EXPECTED_SENSOR_PASS_CHECKS]
                           + [{"check": "base_chain", "status": "SKIPPED"}])}

    def test_actual_pinned_sensor_receipt_passes_all_release_local_checks(self):
        with tempfile.TemporaryDirectory(prefix="glp-r13-sensor-test-") as temp:
            def sensor(command, **_kwargs):
                Path(command[command.index("--output") + 1]).write_text(json.dumps(self.preflight_fixture()))
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(carrier.subprocess, "run", side_effect=sensor):
                result = carrier.run_actual_preflight_sensor(Path(temp), {})
        self.assertEqual(result["status"], "PASS_PARTIAL")
        self.assertEqual(result["passed_checks"], carrier.EXPECTED_SENSOR_PASS_CHECKS)
        self.assertEqual(result["skipped_checks"], ["base_chain"])
        self.assertEqual(result["effects"], carrier.EXPECTED_ZERO_EFFECTS)

    def test_sensor_failure_cannot_be_hidden_or_weakened(self):
        broken = self.preflight_fixture()
        broken["checks"][0]["status"] = "FAIL"
        broken["failed_checks"] = ["coverage"]
        broken["failure_count"] = 1
        with tempfile.TemporaryDirectory(prefix="glp-r13-sensor-fail-") as temp:
            def sensor(command, **_kwargs):
                Path(command[command.index("--output") + 1]).write_text(json.dumps(broken))
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(carrier.subprocess, "run", side_effect=sensor):
                with self.assertRaises(carrier.GateError):
                    carrier.run_actual_preflight_sensor(Path(temp), {})

    def test_live_sensor_requires_exact_deployment_pass_zero_and_provider_read_count(self):
        deployment_id = "11111111-1111-1111-1111-111111111111"
        checks = [{"check": name, "status": "PASS", "failures": []}
                  for name in carrier.EXPECTED_LIVE_SENSOR_PASS_CHECKS]
        checks[-1].update({
            "base_deployment_id": deployment_id,
            "live_canonical_deployment_id": deployment_id,
            "provider_reads": 1,
        })
        strict = {
            "status": "PASS", "checks": checks, "failed_checks": [], "skipped_checks": [],
            "failure_count": 0, "effects": carrier.EXPECTED_ZERO_EFFECTS,
            "release_root": str(carrier.RELEASE),
        }
        with tempfile.TemporaryDirectory(prefix="glp-r3-live-sensor-") as temp:
            root = Path(temp)
            output = root / "live-completeness.json"
            def sensor(command, **_kwargs):
                Path(command[command.index("--output") + 1]).write_text(json.dumps(strict))
                return mock.Mock(returncode=0, stdout="", stderr="")
            with mock.patch.object(carrier.subprocess, "run", side_effect=sensor):
                accepted = carrier.run_live_completeness_sensor(root, deployment_id)
            self.assertEqual(accepted["status"], "PASS")
            self.assertEqual(accepted["failure_count"], 0)
            self.assertEqual(accepted["skipped_checks"], [])
            self.assertEqual(accepted["provider_reads"], 1)

    def test_live_sensor_rejects_partial_mismatch_and_unreceipted_provider_reads(self):
        deployment_id = "11111111-1111-1111-1111-111111111111"
        checks = [{"check": name, "status": "PASS", "failures": []}
                  for name in carrier.EXPECTED_LIVE_SENSOR_PASS_CHECKS]
        checks[-1].update({
            "base_deployment_id": deployment_id,
            "live_canonical_deployment_id": deployment_id,
            "provider_reads": 1,
        })
        strict = {
            "status": "PASS", "checks": checks, "failed_checks": [], "skipped_checks": [],
            "failure_count": 0, "effects": carrier.EXPECTED_ZERO_EFFECTS,
            "release_root": str(carrier.RELEASE),
        }
        variants = [
            ("partial", {**strict, "status": "PASS_PARTIAL", "skipped_checks": ["base_chain"]},
             "live_completeness_sensor_not_passing"),
            ("deployment", {**strict, "checks": [*checks[:-1], {**checks[-1], "base_deployment_id": "22222222-2222-2222-2222-222222222222"}]},
             "live_completeness_sensor_binding_invalid"),
            ("provider_reads", {**strict, "checks": [*checks[:-1], {**checks[-1], "provider_reads": 0}]},
             "live_completeness_sensor_provider_read_count_invalid"),
        ]
        for name, variant, error in variants:
            with self.subTest(name=name), tempfile.TemporaryDirectory(prefix="glp-r3-live-sensor-fail-") as temp:
                root = Path(temp)
                def sensor(command, **_kwargs):
                    Path(command[command.index("--output") + 1]).write_text(json.dumps(variant))
                    return mock.Mock(returncode=0, stdout="", stderr="")
                with mock.patch.object(carrier.subprocess, "run", side_effect=sensor):
                    with self.assertRaisesRegex(carrier.GateError, error):
                        carrier.run_live_completeness_sensor(root, deployment_id)

    def test_pinned_live_sensor_requests_are_intercepted(self):
        sensor_spec = importlib.util.spec_from_file_location("glp_r3_sensor", carrier.SENSOR)
        sensor = importlib.util.module_from_spec(sensor_spec)
        assert sensor_spec.loader is not None
        sensor_spec.loader.exec_module(sensor)
        deployment_id = "11111111-1111-1111-1111-111111111111"
        response = mock.Mock()
        response.json.return_value = {"success": True, "result": {
            "canonical_deployment": {"id": deployment_id}}}
        session = mock.Mock()
        session.get.return_value = response
        with tempfile.TemporaryDirectory(prefix="glp-r3-sensor-provider-") as temp:
            pointer = Path(temp) / "current-content-base.json"
            pointer.write_text(json.dumps({"deployment_id": deployment_id}))
            with (mock.patch.object(sensor, "CONTENT_BASE", pointer),
                  mock.patch.object(sensor, "validate_pointer_shape"),
                  mock.patch.object(sensor, "load_env_value", return_value="intercepted"),
                  mock.patch("requests.Session", return_value=session)):
                result = sensor.check_base_chain(False)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["base_deployment_id"], deployment_id)
        self.assertEqual(result["live_canonical_deployment_id"], deployment_id)
        self.assertEqual(session.get.call_count, 1)
        # Current sensor does not emit P39's required provider_reads field;
        # the carrier correctly refuses its live receipt until this is repaired.
        self.assertNotIn("provider_reads", result)

    def test_exact_toolchain_has_no_symlink_special_or_writable_member(self):
        expected = {
            "closure_sha256": "1af0afc423948202725ae9299b4803c46b68cbb053358943a17123c6dfbc4ad7",
            "file_count": 1662, "directory_count": 258, "total_bytes": 621665046,
        }
        self.assertEqual(carrier.toolchain_closure(carrier.TOOLCHAIN), expected)
        dirs, files = carrier._walk_plain(carrier.TOOLCHAIN)
        for path in [carrier.TOOLCHAIN, *dirs, *files]:
            st = path.lstat()
            self.assertEqual((st.st_uid, st.st_gid), (0, 0), path)
            self.assertFalse(stat.S_IMODE(st.st_mode) & 0o222, path)

    def test_toolchain_closure_canary_detects_mode_and_extra_file(self):
        with tempfile.TemporaryDirectory(prefix="glp-r13-toolchain-canary-") as temp:
            root = Path(temp)
            (root / "a").write_bytes(b"a")
            first = carrier.toolchain_closure(root)
            os.chmod(root / "a", 0o555)
            second = carrier.toolchain_closure(root)
            self.assertNotEqual(first["closure_sha256"], second["closure_sha256"])
            (root / "b").write_bytes(b"b")
            third = carrier.toolchain_closure(root)
            self.assertEqual(third["file_count"], first["file_count"] + 1)
            self.assertNotEqual(second["closure_sha256"], third["closure_sha256"])

    def test_tree_walker_rejects_symlink_canary(self):
        with tempfile.TemporaryDirectory(prefix="glp-r13-symlink-canary-") as temp:
            root = Path(temp)
            (root / "target").write_text("x")
            (root / "link").symlink_to("target")
            with self.assertRaisesRegex(carrier.GateError, "tree_special_file"):
                carrier._walk_plain(root)

    def test_atomic_cas_success_is_exact_durable_and_one_replace(self):
        with self.cas_fixture() as (root, pointer, _lock, lock_fd, _pre, successor, intent):
            replace_calls = []
            original_replace = carrier.os.replace
            def counted(*args, **kwargs):
                replace_calls.append((args, kwargs))
                return original_replace(*args, **kwargs)
            with mock.patch.object(carrier.os, "replace", side_effect=counted):
                result = carrier.atomic_pointer_cas(intent, lock_fd, production=False)
            self.assertEqual(result["status"], "PASS_ATOMIC_POINTER_CAS")
            self.assertEqual(pointer.read_bytes(), successor)
            self.assertEqual(stat.S_IMODE(pointer.stat().st_mode), 0o600)
            self.assertEqual(len(replace_calls), 1)
            self.assertFalse((root / intent["temp_name"]).exists())
            artifact = Path(intent["artifacts"][0]["path"])
            self.assertEqual(carrier.sha_file(artifact), intent["artifacts"][0]["sha256"])

    def test_atomic_cas_fsyncs_regular_files_and_parent_directory(self):
        with self.cas_fixture() as (_root, _pointer, _lock, lock_fd, _pre, _successor, intent):
            kinds = []
            original = carrier.os.fsync
            def recording(fd):
                kinds.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
                return original(fd)
            with mock.patch.object(carrier.os, "fsync", side_effect=recording):
                carrier.atomic_pointer_cas(intent, lock_fd, production=False)
            self.assertIn("file", kinds)
            self.assertIn("dir", kinds)
            self.assertGreaterEqual(kinds.count("file"), 4)
            self.assertGreaterEqual(kinds.count("dir"), 2)

    def test_atomic_cas_rejects_byte_drift_without_replace(self):
        with self.cas_fixture() as (_root, pointer, _lock, lock_fd, _pre, _successor, intent):
            pointer.write_bytes(carrier.json_bytes({"third": "state"}))
            with mock.patch.object(carrier.os, "replace", wraps=carrier.os.replace) as replace:
                with self.assertRaisesRegex(carrier.GateError, "neither_predecessor_nor_successor"):
                    carrier.atomic_pointer_cas(intent, lock_fd, production=False)
                replace.assert_not_called()

    def test_atomic_cas_rejects_identical_bytes_on_replaced_inode(self):
        with self.cas_fixture() as (root, pointer, _lock, lock_fd, predecessor, _successor, intent):
            replacement = root / "replacement"
            replacement.write_bytes(predecessor)
            os.chmod(replacement, 0o600)
            os.replace(replacement, pointer)
            self.assertNotEqual(pointer.stat().st_ino, intent["predecessor_stat"]["st_ino"])
            with self.assertRaisesRegex(carrier.GateError, "predecessor_inode_or_mode_drift"):
                carrier.atomic_pointer_cas(intent, lock_fd, production=False)

    def test_atomic_cas_rejects_wrong_existing_temp(self):
        with self.cas_fixture() as (root, pointer, _lock, lock_fd, predecessor, _successor, intent):
            (root / intent["temp_name"]).write_bytes(b"hostile")
            os.chmod(root / intent["temp_name"], 0o600)
            with self.assertRaisesRegex(carrier.GateError, "pointer_reconciliation_failed"):
                carrier.atomic_pointer_cas(intent, lock_fd, production=False)
            self.assertEqual(pointer.read_bytes(), predecessor)

    def test_atomic_cas_rejects_artifact_collision(self):
        with self.cas_fixture() as (_root, pointer, _lock, lock_fd, predecessor, _successor, intent):
            artifact = Path(intent["artifacts"][0]["path"])
            artifact.parent.mkdir(parents=True)
            artifact.write_bytes(b"hostile")
            os.chmod(artifact, 0o600)
            with self.assertRaisesRegex(carrier.GateError, "pointer_artifact_collision"):
                carrier.atomic_pointer_cas(intent, lock_fd, production=False)
            self.assertEqual(pointer.read_bytes(), predecessor)

    def test_atomic_cas_rejects_replaced_lock_inode(self):
        with self.cas_fixture() as (root, pointer, lock_path, lock_fd, predecessor, _successor, intent):
            replacement = root / "new-lock"
            replacement.write_text("")
            os.replace(replacement, lock_path)
            with self.assertRaisesRegex(carrier.GateError, "weekly_lock_inode_replaced"):
                carrier.atomic_pointer_cas(intent, lock_fd, production=False)
            self.assertEqual(pointer.read_bytes(), predecessor)

    def test_crash_after_temp_write_resumes_without_second_temp_creation(self):
        with self.cas_fixture() as (root, pointer, _lock, lock_fd, predecessor, successor, intent):
            fired = {"done": False}
            def hook(point):
                if point == "after_temp_write" and not fired["done"]:
                    fired["done"] = True
                    raise CrashInjection()
            carrier._CAS_FAULT_HOOK = hook
            with self.assertRaises(CrashInjection):
                carrier.atomic_pointer_cas(intent, lock_fd, production=False)
            self.assertEqual(pointer.read_bytes(), predecessor)
            self.assertTrue((root / intent["temp_name"]).exists())
            carrier._CAS_FAULT_HOOK = None
            result = carrier.atomic_pointer_cas(intent, lock_fd, production=False)
            self.assertEqual(result["status"], "PASS_ATOMIC_POINTER_CAS")
            self.assertEqual(pointer.read_bytes(), successor)

    def test_crash_after_replace_reconciles_successor_without_second_replace(self):
        with self.cas_fixture() as (_root, pointer, _lock, lock_fd, _pre, successor, intent):
            def hook(point):
                if point == "after_replace_before_parent_fsync":
                    raise CrashInjection()
            carrier._CAS_FAULT_HOOK = hook
            with self.assertRaises(CrashInjection):
                carrier.atomic_pointer_cas(intent, lock_fd, production=False)
            self.assertEqual(pointer.read_bytes(), successor)
            carrier._CAS_FAULT_HOOK = None
            with mock.patch.object(carrier.os, "replace", wraps=carrier.os.replace) as replace:
                result = carrier.atomic_pointer_cas(intent, lock_fd, production=False)
                replace.assert_not_called()
            self.assertEqual(result["status"], "PASS_CAS_RECONCILED_AFTER_CRASH")

    def test_crash_then_third_state_is_ambiguous_and_blocks(self):
        with self.cas_fixture() as (_root, pointer, _lock, lock_fd, _pre, _successor, intent):
            def hook(point):
                if point == "before_replace":
                    raise CrashInjection()
            carrier._CAS_FAULT_HOOK = hook
            with self.assertRaises(CrashInjection):
                carrier.atomic_pointer_cas(intent, lock_fd, production=False)
            pointer.write_bytes(b"third-state\n")
            carrier._CAS_FAULT_HOOK = None
            with self.assertRaisesRegex(carrier.GateError, "neither_predecessor_nor_successor"):
                carrier.atomic_pointer_cas(intent, lock_fd, production=False)

    def test_production_path_bypass_is_rejected(self):
        with self.cas_fixture(artifacts=False) as (_root, _pointer, _lock, lock_fd, _pre, _successor, intent):
            with self.assertRaisesRegex(carrier.GateError, "production_cas_path_bypass"):
                carrier.atomic_pointer_cas(intent, lock_fd, production=True)

    def test_one_time_state_resumes_active_and_refuses_consumed(self):
        with tempfile.TemporaryDirectory(prefix="glp-r13-one-time-") as temp:
            root = Path(temp)
            live_runs = root / "live-runs"
            state_path = root / "ONE-TIME.json"
            with mock.patch.object(carrier, "LIVE_RUNS", live_runs), mock.patch.object(carrier, "ONE_TIME_STATE", state_path):
                first = carrier.reserve_live("a" * 64)
                second = carrier.reserve_live("a" * 64)
                self.assertEqual(first[0], second[0])
                self.assertEqual(len([p for p in live_runs.iterdir() if p.is_dir()]), 1)
                state = carrier.load_json(state_path)
                state["status"] = "CONSUMED_PASS"
                carrier.checkpoint(state_path, state)
                with self.assertRaisesRegex(carrier.GateError, "one_time_authority_consumed"):
                    carrier.reserve_live("a" * 64)
                self.assertEqual(len([p for p in live_runs.iterdir() if p.is_dir()]), 1)

if __name__ == "__main__":
    unittest.main(verbosity=2)
