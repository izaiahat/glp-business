"""GLP43 registered CLI; real git/copy/hash/CAS, fixture compiler/provider only.

Every APPROVE in this file is explicitly synthetic and exists only in a temporary
fixture home. No real independent approval is authored or production call allowed.
"""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("current_carrier", ROOT / "scripts/glp_deploy_glp34_authority_v1.py")
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
REAL_RUN = subprocess.run
NEW_ID = "11111111-1111-4111-8111-111111111111"


class CurrentReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="glp43-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.package, self.prepared = [self.root / p for p in ("source", "package", "prepared")]
        self.source.mkdir(); self.package.mkdir()
        self.public = self.package / "public"
        self.public.mkdir()
        # The retired comparison hub is a compiled Function redirect, not a file.
        names = ["index.html", "providers/index.html",
                 "tools/glp-1-cost-calculator/index.html", "lp/quiz/index.html", "quiz/provider-fit/index.html",
                 "reviews/nu-image-medical-review/index.html", "reviews/mydrhank/index.html",
                 "privacy/index.html", "privacy/cookies/index.html", "sitemap.xml", "assets/consent.js"]
        for name in names:
            p = self.public / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("synthetic public: " + name)
        for name in ["functions/go/program-manifest.js", *[f"functions/f{i}.js" for i in range(16)],
                     "src/worker.mjs", "data/holds.json", "wrangler.toml"]:
            p = self.source / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("// synthetic " + name)
        self.git("init", "-q"); self.git("add", ".")
        self.git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "synthetic source")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.tree = self.git("rev-parse", "HEAD^{tree}").strip()
        rows = [[f, c.sha_file(self.source / f)] for f in self.git("ls-files").strip().splitlines()]
        public_hash, public_rows = c.package_tree(self.public)
        self.gate = self.root / "gate.json"
        self.gate_data = {"schema": "glp-d1-release-result-v1", "head": self.head, "status": "PASS",
                          "output_before": {"sha256": public_hash, "files": public_rows},
                          "output_after": {"sha256": public_hash, "files": public_rows},
                          "built_source_sha256": c.sha_bytes(json.dumps(rows, separators=(",", ":")).encode()),
                          "generated_diff_sha256": c.sha_bytes(b""), "generated_diff_paths": [], "authority_files": [],
                          "steps": [{"id": "affiliate-census", "status": "PASS", "evidence": {"raw_held": {
                              "anchors": [], "routes": [], "held": [{"id": "fella-health"}], "counts": {"fella-health": 0}}}}]}
        self.gate.write_bytes(c.json_bytes(self.gate_data))
        package = {"schema": "glp34-deploy-package-v1", "site_head": self.head, "site_repository": c.CURRENT_REPOSITORY,
                   "project": c.PROJECT, "account_id": c.ACCOUNT_ID, "rollback_branch": "main",
                   "rollback_deployment_id": c.CURRENT_ROLLBACK, "public_tree_sha256": public_hash, "files": public_rows,
                   "build_receipt": {"sha256": c.sha_file(self.gate)}}
        (self.package / "PACKAGE-MANIFEST.json").write_bytes(c.json_bytes(package))
        self.pointer = self.root / "current-content-base.json"
        self.pointer.write_bytes(c.json_bytes({"deployment_id": c.CURRENT_ROLLBACK, "release_root": "/synthetic/prior",
                                              "manifest": {}, "final_receipt": {}}))
        self.approval = self.root / "review.json"
        self.authorities = {}
        for name in c.CURRENT_AUTHORITIES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("SYNTHETIC FIXTURE AUTHORITY — not operator authorization")
            self.authorities[name] = c.sha_file(path)
        self.calls, self.deploy_calls, self.rollbacks, self.builds = [], [], [], []
        self.current = self.deployment(c.CURRENT_ROLLBACK, "0" * 40)
        self.deployments = [copy.deepcopy(self.current)]
        self.cli_mode = "land"
        self.readback_fail = False
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in {"BUSINESS": self.root, "POINTER": self.pointer, "SEO_REPORT_ROOT": self.root / "receipts",
                            "LOCK_PATH": self.root / "weekly.lock", "CURRENT_AUTHORITIES": self.authorities}.items():
            self.stack.enter_context(mock.patch.object(c, name, value))
        self.stack.enter_context(mock.patch.object(c, "verify_immutable_tree", return_value=c.CURRENT_TOOLCHAIN))
        self.stack.enter_context(mock.patch.object(c, "read_token", return_value="synthetic-fixture-not-a-credential"))
        self.stack.enter_context(mock.patch.object(c.subprocess, "run", side_effect=self.process))
        self.stack.enter_context(mock.patch.object(c.urllib.request, "urlopen", side_effect=self.transport))
        self.stack.enter_context(mock.patch.object(c.urllib.request.OpenerDirector, "open", side_effect=self.transport))
        # Historical sensor/authority execution is forbidden in this successor.
        for name in ("load_authority", "run_live", "run_verify_only", "run_live_completeness_sensor"):
            self.stack.enter_context(mock.patch.object(c, name, side_effect=AssertionError("historical path reached")))

    def git(self, *args):
        return REAL_RUN(["git", "-C", str(self.source), *args], check=True, capture_output=True, text=True).stdout

    def deployment(self, id, head):
        return {"id": id, "environment": "production", "latest_stage": {"status": "success"},
                "deployment_trigger": {"metadata": {"branch": "main", "commit_hash": head}},
                "url": "https://11111111.glp-clinic-scout.pages.dev"}

    def process(self, command, **kwargs):
        if command[:3] == ["git", "-C", str(self.source)]:
            return REAL_RUN(command, **kwargs)
        if command[:6] == ["gh", "api", "--hostname", "github.com", "--method", "GET"]:
            self.assertEqual(command[-1], "repos/izaiahat/glp-site-candidate/commits/" + self.head)
            return subprocess.CompletedProcess(command, 0, json.dumps({"sha": self.head}), "")
        if command[1:] == [str(self.root / "scripts/candidate_build_lock.py"), "require-held", "--seat", "fixture-seat"]:
            return subprocess.CompletedProcess(command, 0, b"HELD", b"")
        self.assertEqual(command[:2], [str(c.NODE), str(c.WRANGLER)])
        if command[2:6] == ["pages", "functions", "build", "functions"]:
            self.builds.append(command)
            out = Path(command[-1]); out.mkdir(parents=True)
            rows = [{"route": "/go/:program", "modules": "onGo", "middlewares": ""},
                    {"route": "/api/postback/affiliati", "modules": "onPostback", "middlewares": ""},
                    {"route": "/providers", "modules": "", "middlewares": "onMiddleware"}]
            body = '// ../.wrangler/tmp/pages-fixture/functionsRoutes-fixture.mjs\nvar routes = [\n'
            body += ',\n'.join('  {routePath: "' + r["route"] + '", mountPath: "/", method: "", middlewares: [' +
                                r["middlewares"] + '], modules: [' + r["modules"] + ']}' for r in rows)
            body += '\n];\n// fixture fallback env["ASSETS"].fetch(request)\n'
            (out / "index.js").write_text(body)
            return subprocess.CompletedProcess(command, 0, "Compiled Worker successfully", "")
        self.assertEqual(command[2:4], ["pages", "deploy"])
        self.assertIn("--no-bundle", command)
        self.assertEqual(command[command.index("--commit-hash") + 1], self.head)
        self.assertEqual(command[command.index("--project-name") + 1], c.PROJECT)
        self.assertEqual(command[command.index("--branch") + 1], "main")
        self.assertEqual(Path(command[4]), self.prepared / "deploy-public")
        self.deploy_calls.append(c.file_closure(Path(command[4])))
        self.assertEqual(self.deploy_calls[-1], self.prepared_data["deploy_directory"])
        if self.cli_mode != "unsettled":
            self.current = self.deployment(NEW_ID, self.head)
            self.deployments.append(copy.deepcopy(self.current))
        if self.cli_mode != "land":
            raise subprocess.TimeoutExpired(command, 1800)
        return subprocess.CompletedProcess(command, 0, "synthetic provider success", "")

    def transport(self, request, **kwargs):
        self.calls.append((request.get_method(), request.full_url))
        prefix = c.API_BASE + f"/accounts/{c.ACCOUNT_ID}/pages/projects/{c.PROJECT}"
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.code = response.status = 200
        response.headers = {}
        if request.full_url.startswith(prefix):
            suffix = request.full_url[len(prefix):]
            if request.get_method() == "POST":
                self.assertEqual(suffix, "/deployments/" + c.CURRENT_ROLLBACK + "/rollback")
                self.rollbacks.append(suffix)
                self.current = copy.deepcopy(self.deployments[0])
                body = {"success": True, "result": self.current}
            else:
                self.assertEqual(request.get_method(), "GET")
                self.assertIsNone(request.data)
                if suffix == "":
                    data = {"name": c.PROJECT, "production_branch": "main", "subdomain": c.PROJECT + ".pages.dev",
                            "canonical_deployment": self.current}
                    body = {"success": True, "result": data}
                elif suffix.startswith("/deployments?page="):
                    body = {"success": True, "result": self.deployments,
                            "result_info": {"page": 1, "total_pages": 1, "total_count": len(self.deployments)}}
                else:
                    self.assertEqual(suffix, "/deployments/" + c.CURRENT_ROLLBACK)
                    body = {"success": True, "result": self.deployments[0]}
            response.read.return_value = c.json_bytes(body)
        else:
            from urllib.parse import urlsplit
            url = urlsplit(request.full_url)
            self.assertIn(url.netloc, ("11111111.glp-clinic-scout.pages.dev", "glp-clinic-scout.pages.dev", "glpclinicscout.com"))
            if request.get_method() == "GET" and url.path == "/best/online-glp-1-providers/":
                response.code = 301
                response.headers = {"Location": "https://" + url.netloc + "/providers/", "Cache-Control": "public, max-age=300"}
            elif request.get_method() == "GET":
                path = self.public / url.path.lstrip("/")
                if path.is_dir(): path = path / "index.html"
                response.read.return_value = path.read_bytes()
            else:
                self.assertEqual(request.get_method(), "HEAD")
                if url.path.startswith("/go/"):
                    response.code = 307
                    response.headers = {"Location": c.SITE + "/best/online-glp-1-providers/", "Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
                elif url.path == "/api/postback/affiliati":
                    response.code = 200 if self.readback_fail else 405
                    response.headers = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}
                else:
                    self.assertEqual(url.path, "/providers/")
                    response.headers = {"X-Robots-Tag": "noindex, follow"}
        return response

    def cli(self, mode, values, *switches):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = c.main([mode, *[x for k, v in values.items() for x in ("--" + k, str(v))], *switches])
        raw = stdout.getvalue() if code == 0 else stderr.getvalue()
        self.last = json.loads(raw)
        return code

    def prep_args(self):
        return {"source-root": self.source, "site-head": self.head, "git-tree": self.tree,
                "build-seat": "fixture-seat", "package-dir": self.package,
                "expected-package-manifest-sha256": c.sha_file(self.package / "PACKAGE-MANIFEST.json"),
                "build-receipt": self.gate, "expected-build-receipt-sha256": c.sha_file(self.gate), "prepared-dir": self.prepared}

    def prepare(self):
        self.assertEqual(self.cli("--prepare-current", self.prep_args()), 0, self.last)
        self.prepared_data = json.loads((self.prepared / "PREPARED.json").read_text())
        self.assertFalse(self.prepared_data["release_authorized"])
        self.assertEqual(self.deploy_calls, [])
        self.assertEqual(len(self.builds), 1)
        self.assertFalse((self.prepared / "deploy-public/_routes.json").exists())
        self.assertIn(b"// r13-canonical-functions-routes.mjs", (self.prepared / "deploy-public/_worker.js").read_bytes())

    def approve_fixture(self):
        p = self.prepared_data
        self.review = {"schema": "glp42-designated-astra-release-review-v1", "verdict": "APPROVE", "release_accepted": True,
                       "blocking_findings": [], "fixture_only": True,
                       "reviewer": {"model": "gpt-6-astra-900k", "provider": "openai-codex", "role": "synthetic independent; not an author",
                                    "authority": list(self.authorities)[1], "original_target_authority": list(self.authorities)[0]},
                       "subject": {"repository": c.CURRENT_REPOSITORY, "head": self.head, "git_tree": self.tree,
                                   "public_tree_sha256": p["public_tree_sha256"], "package_manifest_sha256": p["package_manifest_sha256"],
                                   "project": c.PROJECT, "rollback_branch": "main", "rollback_environment": "production",
                                   "rollback_deployment_id": c.CURRENT_ROLLBACK,
                                   "prepared_manifest_sha256": c.sha_file(self.prepared / "PREPARED.json"),
                                   "deploy_directory_sha256": p["deploy_directory"]["tree_sha256"],
                                   "compiled_worker_sha256": p["compiled"]["worker_sha256"]},
                       "reused_technical_acceptance": {"gate_sha256": p["build_receipt"]["sha256"]}}
        self.save_review()

    def save_review(self):
        self.approval.write_bytes(c.json_bytes(self.review))

    def publish(self, authorize=True, approval_pin=None):
        args = {"prepared-dir": self.prepared, "expected-prepared-manifest-sha256": c.sha_file(self.prepared / "PREPARED.json"),
                "approval": self.approval, "expected-approval-sha256": approval_pin or c.sha_file(self.approval)}
        return self.cli("--publish-current", args, *(["--authorize-production"] if authorize else []))

    def test_preparation_and_drift_refusals_before_output_or_network(self):
        baseline = self.prep_args()
        source = self.source / "src/worker.mjs"
        original = source.read_bytes()
        for mutate, restore in [
            (lambda: source.write_bytes(original + b"drift"), lambda: source.write_bytes(original)),
            (lambda: (self.source / "functions/shadow.js").write_text("unlisted"), lambda: (self.source / "functions/shadow.js").unlink()),
            (lambda: source.unlink() or source.symlink_to(self.source / "wrangler.toml"), lambda: source.unlink() or source.write_bytes(original)),
        ]:
            mutate()
            self.assertEqual(self.cli("--prepare-current", baseline), 1)
            self.assertFalse(self.prepared.exists()); self.assertEqual(self.calls, []); self.assertEqual(self.builds, [])
            restore()
        for key, value in (("site-head", "a" * 40), ("git-tree", "b" * 40), ("expected-build-receipt-sha256", "c" * 64)):
            self.assertEqual(self.cli("--prepare-current", {**baseline, key: value}), 1)
            self.assertFalse(self.prepared.exists()); self.assertEqual(self.calls, [])
        self.prepare()

    def test_publish_refuses_missing_rejected_and_mismatched_exact_approval(self):
        self.prepare(); self.approve_fixture()
        before = list(self.calls)
        baseline = copy.deepcopy(self.review)
        self.approval.unlink()
        self.assertEqual(self.publish(approval_pin="0" * 64), 1)
        for key, value in (("head", "a" * 40), ("rollback_deployment_id", NEW_ID),
                           ("public_tree_sha256", "b" * 64), ("package_manifest_sha256", "c" * 64),
                           ("prepared_manifest_sha256", "d" * 64), ("deploy_directory_sha256", "e" * 64)):
            self.review = copy.deepcopy(baseline); self.review["subject"][key] = value; self.save_review()
            self.assertEqual(self.publish(), 1, key)
        self.review = copy.deepcopy(baseline)
        self.review.update(verdict="CHANGES_REQUIRED", release_accepted=False)
        self.save_review(); self.assertEqual(self.publish(), 1)
        self.review = baseline; self.save_review(); self.assertEqual(self.publish(authorize=False), 1)
        self.assertEqual(self.calls, before); self.assertEqual(self.deploy_calls, [])
        self.assertFalse((self.root / "receipts").exists())
        self.current = self.deployment(NEW_ID, "0" * 40)
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.last["error"], "current_predecessor_or_existing_candidate_mismatch")
        self.assertEqual(self.deploy_calls, [])
        before = list(self.calls)
        (self.prepared / "deploy-public/_worker.js").write_text("drift")
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.calls, before)

    def test_publish_consumes_once_reconciles_landed_timeout_and_records_target(self):
        self.prepare(); self.approve_fixture(); self.cli_mode = "landed-timeout"
        self.assertEqual(self.publish(), 0, self.last)
        receipt = json.loads(Path(self.last["receipt"]).read_text())
        self.assertEqual(receipt["stages"]["pages_deploy_cli"]["ambiguous"], "TimeoutExpired")
        self.assertEqual(receipt["stages"]["canonical_pointer_readback"]["site_head"], self.head)
        self.assertEqual(json.loads(self.pointer.read_text())["deployment_id"], NEW_ID)
        paths = {r["url"].split(".com")[-1] for r in receipt["stages"]["route_probes"]}
        self.assertTrue(any(p.endswith("/sitemap.xml") for p in paths))
        self.assertTrue(any(p.endswith("/assets/consent.js") for p in paths))
        self.assertEqual(len(receipt["stages"]["runtime_and_holds"]), 12)
        hubs = [r for r in receipt["stages"]["runtime_and_holds"] if r["path"].startswith("/best/")]
        self.assertEqual([r["status"] for r in hubs], [301, 301, 301])
        self.assertEqual(self.publish(), 1)
        self.assertEqual(len(self.deploy_calls), 1); self.assertEqual(len(self.builds), 1)

    def test_unsettled_attempt_resumes_reconciliation_only_without_retry(self):
        self.prepare(); self.approve_fixture(); self.cli_mode = "unsettled"
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.last["error"], "current_deploy_outcome_unsettled_reconcile_only")
        self.assertEqual(self.publish(), 1)
        self.assertEqual(len(self.deploy_calls), 1)
        self.current = self.deployment(NEW_ID, self.head); self.deployments.append(copy.deepcopy(self.current))
        self.assertEqual(self.publish(), 0, self.last)
        self.assertEqual(len(self.deploy_calls), 1)

    def test_postdeploy_route_failure_rolls_back_once_and_keeps_pointer(self):
        self.prepare(); self.approve_fixture(); before = self.pointer.read_bytes(); self.readback_fail = True
        self.assertEqual(self.publish(), 1)
        self.assertEqual(self.current["id"], c.CURRENT_ROLLBACK)
        self.assertEqual(self.pointer.read_bytes(), before)
        self.assertEqual(len(self.rollbacks), 1)
        self.assertEqual(self.publish(), 1)
        self.assertEqual(len(self.rollbacks), 1); self.assertEqual(len(self.deploy_calls), 1)


if __name__ == "__main__":
    unittest.main()
