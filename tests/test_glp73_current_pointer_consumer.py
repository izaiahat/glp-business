"""Focused source regression for GLP-43's registered prepared pointer format."""
from __future__ import annotations

import ast
import base64
import hashlib
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(ROOT / "systems/seo-blog-machine/scripts"))
sys.path.insert(0, str(SCRIPTS))
# Portable fixture: isolate the weekly module's eager article-builder import.
# Legacy-path-size-sha256 rows never invoke BUILDER in these tests. A separate
# read-only actual-pointer probe exercises the unmodified candidate source.
source_path = SCRIPTS / "glp_price_index_weekly.py"
tree = ast.parse(source_path.read_text(), filename=str(source_path))
eager = [node for node in tree.body if isinstance(node, ast.Assign)
         and any(isinstance(t, ast.Name) and t.id == "BUILDER" for t in node.targets)]
assert len(eager) == 1 and isinstance(eager[0].value, ast.Call)
assert isinstance(eager[0].value.func, ast.Name) and eager[0].value.func.id == "load_builder"
tree.body.remove(eager[0])
module = types.ModuleType("glp_price_index_weekly")
module.__file__ = str(source_path)
sys.modules[module.__name__] = module
exec(compile(ast.fix_missing_locations(tree), str(source_path), "exec"), module.__dict__)
module.BUILDER = None

import glp_deploy_glp34_authority_v1 as producer  # noqa: E402
import current_content_base as reader  # noqa: E402
import glp_price_index_weekly as evidence  # noqa: E402
from glp_current_content_base_schema import validate_pointer_shape  # noqa: E402


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def write(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return digest(raw)


def closure(root: Path) -> tuple[list[dict], dict]:
    rows = evidence._legacy_rows(evidence._complete_current_rows(root))
    return rows, {"tree_sha256": evidence._legacy_closure(rows), "file_count": len(rows),
                  "total_bytes": sum(row["size"] for row in rows)}


def fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prepared: bool) -> tuple[dict, Path, dict, Path]:
    business = tmp_path / "business"
    flagship = business / "site-system/flagship-v2"
    prior_root = flagship / "releases/r0"
    prior_root.mkdir(parents=True)
    (prior_root / "older").mkdir()
    release = business / "reports/ops/run/prepared/release" if prepared else flagship / "releases/r1"
    for rel, records, key in (("data/a.json", [{"canonical_path": "/a/", "title": "A"}], "articles"),
                              ("data/b.json", [{"canonical_path": "/b/", "title": "B"}], "articles"),
                              ("data/c.json", [{"canonical_path": "/c/", "title": "C"}], "money_pages"),
                              ("data/programs.json", [{"program_id": "x", "brand_name": "X"}], "programs")):
        write(release / rel, {key: records})
    (release / "public").mkdir()
    (release / "public/index.html").write_bytes(b"current public\n")
    rows, release_closure = closure(release)
    _, public_closure = closure(release / "public")
    prior = {"schema": "glp-opus-current-content-base-v1", "status": "PASS",
             "deployment_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "release_root": str(prior_root),
             "release_tree_sha256": "0" * 64, "public_tree_sha256": "1" * 64,
             "manifest": {"path": str(tmp_path / "old-manifest"), "sha256": "2" * 64},
             "final_receipt": {"path": str(tmp_path / "old-receipt"), "sha256": "3" * 64},
             "supersedes": {"deployment_id": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                            "release_root": str(prior_root / "older")}}
    # The predecessor is retained as a byte-bound preparation field, not a live pointer.
    prior_raw = json.dumps(prior, sort_keys=True, separators=(",", ":")).encode()
    head = "f" * 40
    pointer_path = business / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
    report = pointer_path.parent / f"current-release-{head}" if prepared else tmp_path / "legacy-evidence"
    manifest_path = report / "01-deployment-manifest.json"
    receipt_path = report / "10-final-publication-receipt.json"
    pointer = {"schema": "glp-opus-current-content-base-v1", "status": "PASS",
               "deployment_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb", "release_root": str(release),
               "release_tree_sha256": release_closure["tree_sha256"],
               "public_tree_sha256": public_closure["tree_sha256"],
               "supersedes": {"deployment_id": prior["deployment_id"], "release_root": str(prior_root)}}
    manifest = {"schema": "glp-opus-release-deployment-manifest-v1", "release_root": str(release),
                "release_rows": rows, "release_tree_sha256": release_closure["tree_sha256"]}
    receipt = {"schema": "glp-opus-production-terminal-receipt-v1", "status": "PASS_PRODUCTION_TERMINAL",
               "deployment": {"deployment_id": pointer["deployment_id"]}, "evidence_sha256": {},
               "immutable_closure": {"release_tree_sha256": release_closure["tree_sha256"],
                                     "public_tree_sha256": public_closure["tree_sha256"]}}
    if prepared:
        # Native GLP-43 preparation: all 22 producer fields, a copied build
        # gate, package manifest, full public+Worker tree and pointer intent.
        monkeypatch.setattr(producer, "CURRENT_ROLLBACK", prior["deployment_id"])
        monkeypatch.setattr(producer, "POINTER", pointer_path)
        deploy = release.parent / "deploy-public"
        deploy.mkdir(parents=True)
        (deploy / "index.html").write_bytes(b"current public\n")
        (deploy / "_worker.js").write_bytes(b"worker\n")
        names = ("providers/index.html", "tools/glp-1-cost-calculator/index.html",
                 "lp/quiz/index.html", "quiz/provider-fit/index.html",
                 "reviews/nu-image-medical-review/index.html", "reviews/mydrhank/index.html",
                 "privacy/index.html", "privacy/cookies/index.html", "sitemap.xml")
        for name in names:
            (release / "public" / name).parent.mkdir(parents=True, exist_ok=True)
            (release / "public" / name).write_bytes(b"native public\n")
            (deploy / name).parent.mkdir(parents=True, exist_ok=True)
            (deploy / name).write_bytes(b"native public\n")
        rows, release_closure = closure(release)
        _, public_closure = closure(release / "public")
        _, deploy_closure = closure(deploy)
        pointer["release_tree_sha256"] = release_closure["tree_sha256"]
        pointer["public_tree_sha256"] = public_closure["tree_sha256"]
        manifest.update({"site_head": head, "deploy_directory": deploy_closure,
                         "release_rows": rows, "release_tree_sha256": release_closure["tree_sha256"]})
        receipt["site_head"] = head
        pointer.update({"site_head": head, "deploy_directory_sha256": deploy_closure["tree_sha256"]})
        public_hash, public_rows = producer.package_tree(release / "public")
        build_path = report / "full-gate/gate.json"
        source = {"tracked_files": [], "authority_files": [], "runtime": {},
                  "built_source_sha256": "2" * 64, "generated_diff_sha256": "3" * 64,
                  "generated_diff_paths": [], "git_tree": "4" * 40,
                  "go_embedded_sha256": "5" * 64, "go_internal_fallback": False,
                  "go_manifest_sha256": "6" * 64, "wrangler_config_sha256": "7" * 64}
        holds = {"held": [{"id": "held"}]}
        gate = {"schema": "glp-d1-release-result-v1", "status": "PASS", "head": head,
                "output_before": {"sha256": public_hash, "files": public_rows},
                "output_after": {"sha256": public_hash, "files": public_rows},
                "built_source_sha256": source["built_source_sha256"],
                "generated_diff_sha256": source["generated_diff_sha256"],
                "generated_diff_paths": [], "authority_files": [],
                "steps": [{"id": "affiliate-census", "status": "PASS", "evidence": {"raw_held": holds}}]}
        gate_sha = write(build_path, gate)
        (release.parent / "gate.json").write_bytes(build_path.read_bytes())
        build_binding = {"path": str(build_path), "sha256": gate_sha,
                         "schema": "glp-d1-release-result-v1"}
        package = {"schema": "glp34-deploy-package-v1", "site_head": head,
                   "site_repository": producer.CURRENT_REPOSITORY, "project": producer.PROJECT,
                   "account_id": producer.ACCOUNT_ID, "rollback_branch": producer.PRODUCTION_BRANCH,
                   "rollback_deployment_id": prior["deployment_id"], "public_tree_sha256": public_hash,
                   "files": public_rows, "build_receipt": build_binding,
                   "authority_created": False, "file_count": len(public_rows),
                   "github_reads": 1, "mode": "package-only", "provider_mutations": 0,
                   "provider_reads": 1, "public_root": str(release / "public"),
                   "release_authorized": False, "rollback_checked_at_utc": "2026-09-01T00:00:00Z",
                   "rollback_environment": "production", "scope": "package-only",
                   "signature_created": False, "site_head_binding": {},
                   "tree_hash_algorithm": "glp34-public-tree-v1"}
        package_sha = write(release.parent / "PACKAGE-MANIFEST.json", package)
        prepared_doc = {"schema": "glp43-current-prepared-v1", "prepared_dir": str(release.parent),
                    "site_head": head, "site_repository": producer.CURRENT_REPOSITORY,
                    "project": producer.PROJECT, "branch": producer.PRODUCTION_BRANCH,
                    "account_id": producer.ACCOUNT_ID, "public_tree_sha256": public_hash,
                    "package_manifest_sha256": package_sha, "build_receipt": build_binding,
                    "source": source, "compiled": {
                        "worker_sha256": digest(b"worker\n"), "raw_worker_sha256": digest(b"worker\n"),
                        "argv": [], "pages_random_comment_replacements": 0, "returncode": 0,
                        "routes": [], "stderr_sha256": digest(b""), "stdout_sha256": digest(b"")},
                    "toolchain": producer.CURRENT_TOOLCHAIN,
                    "rollback_deployment_id": prior["deployment_id"],
                    "predecessor_pointer": {"path": str(pointer_path),
                        "bytes_base64": base64.b64encode(prior_raw).decode(),
                        "bytes": len(prior_raw), "sha256": digest(prior_raw),
                        "deployment_id": prior["deployment_id"], "release_root": str(prior_root),
                        "st_dev": 1, "st_ino": 2, "uid": 1000, "gid": 1000,
                        "mode": "0600", "nlink": 1},
                    "release": release_closure, "deploy_directory": deploy_closure,
                    "holds": holds, "route_specs": producer.current_route_specs(release / "public"),
                    "provider_reads": 3, "provider_mutations": 0, "release_authorized": False}
        prepared_sha = write(release.parent / "PREPARED.json", prepared_doc)
        assert producer.current_prepared(release.parent, prepared_sha) == prepared_doc
    else:
        manifest.update({"created_at_utc": "2026-09-01T00:00:00Z", "note": "legacy"})
        receipt["created_at_utc"] = "2026-09-01T00:00:00Z"
    manifest_sha = write(manifest_path, manifest)
    receipt["evidence_sha256"] = {manifest_path.name: manifest_sha}
    pointer["manifest"] = {"path": str(manifest_path), "sha256": manifest_sha}
    pointer["final_receipt"] = {"path": str(receipt_path), "sha256": write(receipt_path, receipt)}
    write(pointer_path, pointer)
    if prepared:
        intent = producer.current_pointer_intent(release.parent, prepared_doc, pointer["deployment_id"], report)
        for artifact in intent["artifacts"]:
            Path(artifact["path"]).write_bytes(base64.b64decode(artifact["bytes_base64"]))
        pointer_path.write_bytes(base64.b64decode(intent["successor_bytes_base64"]))
        pointer = json.loads(pointer_path.read_bytes())
        # Prior publication files are inert synthetic receipt stand-ins; only
        # the registered post-identity reader is substituted for this fixture.
        original = report / "CURRENT-RECEIPT.json"
        original_sha = write(original, {"schema": "glp43-current-promotion-receipt-v1"})
        prefix = "CURRENT-PROMOTION-" + pointer["deployment_id"]
        first_sha = write(report / (prefix + ".json"), {"status": "BLOCKED_ROLLED_BACK"})
        second_sha = write(report / (prefix + "-SUCCESSOR-" + first_sha + ".json"),
                           {"status": "BLOCKED_ROLLED_BACK"})
        terminal_sha = write(report / (prefix + "-SUCCESSOR-" + second_sha + ".json"),
                             {"status": "BLOCKED_ROLLED_BACK"})
        monkeypatch.setattr(producer, "CURRENT_PROMOTION_ROOT_SHA256", first_sha)
        monkeypatch.setattr(producer, "CURRENT_PROMOTION_ROLLED_BACK_SUCCESSOR_SHA256", second_sha)
        monkeypatch.setattr(producer, "CURRENT_PROMOTION_POST_IDENTITY_TERMINAL_SHA256", terminal_sha)
        monkeypatch.setattr(producer, "current_promotion_source", lambda args, prepared, original_path: prepared_doc)
        def post_identity(*args):
            return {"prepared_manifest_sha256": prepared_sha, "site_head": head,
                    "candidate_deployment_id": pointer["deployment_id"],
                    "predecessor_id": prior["deployment_id"],
                    "predecessor_pointer_sha256": digest(prior_raw),
                    "original_receipt_sha256": original_sha,
                    "terminal_promotion_sha256": terminal_sha,
                    "approval_sha256": "9" * 64}
        monkeypatch.setattr(producer, "current_promotion_post_identity_source", post_identity)
        terminal_path = report / (prefix + "-SUCCESSOR-" + terminal_sha + ".json")
        write(terminal_path, {"schema": "glp43-current-promotion-receipt-v1",
                              "status": "PASS_PRODUCTION_TERMINAL", "site_head": head,
                              "binding": post_identity(None), "original_receipt": str(original),
                              "stages": {"pointer_intent": intent,
                                         "pointer_cas": {"sha256": digest(pointer_path.read_bytes()), "status": "PASS"},
                                         "canonical_pointer_readback": {"pointer_sha256": digest(pointer_path.read_bytes()),
                                                                        "deployment_id": pointer["deployment_id"],
                                                                        "site_head": head}},
                              "effects": {"pointer_atomic_cas": 1}})

    monkeypatch.setattr(evidence, "BUSINESS", business)
    monkeypatch.setattr(evidence, "CURRENT_BASE", pointer_path)
    config = {"paths": {"current_content_base_state": str(pointer_path), "content_catalogs": [
        {"catalog_id": n, "kind": "article", "release_relative_path": f"data/{n}.json"}
        for n in "abc"]}, "program_set": {"release_relative_path": "data/programs.json"}}
    return config, flagship, pointer, pointer_path


def test_prepared_dependency_drift_after_consumption(tmp_path, monkeypatch):
    config, flagship, pointer, pointer_path = fixture(tmp_path, monkeypatch, True)
    resolution = reader.resolve_current_content_base(config, flagship_root=flagship)
    reader.verify_resolution(resolution)
    prepared_path = Path(pointer["release_root"]).parent / "PREPARED.json"
    prepared = json.loads(prepared_path.read_bytes())
    prepared["site_head"] = "e" * 40
    write(prepared_path, prepared)
    assert json.loads(pointer_path.read_bytes()) == pointer
    with pytest.raises(RuntimeError, match="prepared_dependency_drift|prepared_transitive_drift"):
        reader.verify_resolution(resolution)
    with pytest.raises(RuntimeError):
        reader.resolve_current_content_base(config, flagship_root=flagship)
    write(prepared_path, {**prepared, "site_head": "f" * 40})
    resolution = reader.resolve_current_content_base(config, flagship_root=flagship)
    # The compiled Worker is not a pointer/catalog or top-level PREPARED byte:
    # the native producer closure must be replayed after consumption.
    worker = prepared_path.parent / "deploy-public/_worker.js"
    worker.write_bytes(b"wORker\n")
    with pytest.raises(RuntimeError, match="prepared|registered"):
        reader.verify_resolution(resolution)


def test_self_asserted_prepared_subset_is_not_registered(tmp_path, monkeypatch):
    config, flagship, pointer, pointer_path = fixture(tmp_path, monkeypatch, True)
    prepared_path = Path(pointer["release_root"]).parent / "PREPARED.json"
    prepared = json.loads(prepared_path.read_bytes())
    for key in ("account_id", "public_tree_sha256", "package_manifest_sha256", "build_receipt",
                "source", "toolchain", "holds", "route_specs", "provider_reads"):
        del prepared[key]
    write(prepared_path, prepared)
    assert json.loads(pointer_path.read_bytes()) == pointer
    with pytest.raises(RuntimeError, match="prepared_native_fields_invalid"):
        reader.resolve_current_content_base(config, flagship_root=flagship)


def test_publisher_prepared_pointer_resolves_exact_root_and_custody(tmp_path, monkeypatch):
    config, flagship, pointer, _ = fixture(tmp_path, monkeypatch, True)
    # This exact outer shape used to fail as fields_invalid before touching root/evidence.
    assert {"site_head", "deploy_directory_sha256"} <= set(pointer)
    resolved = reader.resolve_current_content_base(config, flagship_root=flagship)
    assert resolved["pointer"]["release_root"] == pointer["release_root"]
    assert resolved["pointer"]["deployment_id"] == pointer["deployment_id"]
    reader.verify_resolution(resolved)


def test_legacy_pointer_still_resolves_under_flagship(tmp_path, monkeypatch):
    config, flagship, pointer, _ = fixture(tmp_path, monkeypatch, False)
    resolved = reader.resolve_current_content_base(config, flagship_root=flagship)
    assert resolved["pointer"]["release_root"] == pointer["release_root"]


def test_prepared_pointer_cannot_escape_verified_root_or_change_receipt(tmp_path, monkeypatch):
    config, flagship, pointer, path = fixture(tmp_path, monkeypatch, True)
    escaped = tmp_path / "unregistered/release"
    escaped.mkdir(parents=True)
    pointer["release_root"] = str(escaped)
    write(path, pointer)
    with pytest.raises(RuntimeError, match="prepared_root_invalid|release_root_mismatch"):
        reader.resolve_current_content_base(config, flagship_root=flagship)
    pointer["release_root"] = str((tmp_path / "business/reports/ops/run/prepared/release"))
    pointer["deployment_id"] = "dddddddd-dddd-dddd-dddd-dddddddddddd"
    write(path, pointer)
    with pytest.raises(RuntimeError, match="promotion_path_invalid|final_receipt_deployment_mismatch"):
        reader.resolve_current_content_base(config, flagship_root=flagship)
    pointer["deployment_id"] = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    original_deploy_hash = pointer["deploy_directory_sha256"]
    pointer["deploy_directory_sha256"] = "0" * 64
    write(path, pointer)
    with pytest.raises(RuntimeError, match="prepared_closure_mismatch"):
        reader.resolve_current_content_base(config, flagship_root=flagship)
    pointer["deploy_directory_sha256"] = original_deploy_hash
    pointer["site_head"] = "wrong"
    write(path, pointer)
    with pytest.raises(RuntimeError, match="site_head_invalid"):
        reader.resolve_current_content_base(config, flagship_root=flagship)
    pointer["site_head"] = "f" * 40
    pointer["unexpected"] = "PASS"
    write(path, pointer)
    with pytest.raises(RuntimeError, match="fields_invalid"):
        reader.resolve_current_content_base(config, flagship_root=flagship)
