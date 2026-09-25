from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
from pathlib import Path


BUSINESS = Path("/home/ubuntu/business")
MATERIALIZER_PATH = BUSINESS / "scripts/site_tree_materializer.py"
AFFILIATE_PATH = BUSINESS / "scripts/build_glp_affiliate_wirein_incremental_v2.py"
PRICE_PATH = BUSINESS / "scripts/glp_price_index_weekly.py"
OPUS_PATH = BUSINESS / "systems/seo-blog-machine/scripts/build_opus_article_release.py"
REMINT_PATH = BUSINESS / "systems/seo-blog-machine/scripts/remint_content_base.py"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


materializer = load(MATERIALIZER_PATH, "site_tree_materializer_generator_gate_test")
affiliate = load(AFFILIATE_PATH, "affiliate_wirein_generator_gate_test")


def make_immutable(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o444)
    for path in sorted(
        (item for item in [root, *root.rglob("*")] if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        path.chmod(0o555)


def cleanup_writable(*roots: Path) -> None:
    for root in roots:
        if not root.exists() or root.is_symlink():
            continue
        for path in [root, *root.rglob("*")]:
            if path.is_dir() and not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o700)


def test_affiliate_declared_mutations_detach_before_any_write_and_preserve_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    required = affiliate.DECLARED_MUTATION_PATHS - affiliate.MAY_BE_ABSENT_BEFORE_WRITE
    for relative in sorted(required | {"src/unchanged.mjs"}):
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"accepted:{relative}\n", encoding="utf-8")
    (source / "node_modules/pkg").mkdir(parents=True)
    (source / "node_modules/pkg/index.js").write_text("ignored\n", encoding="utf-8")
    make_immutable(source)
    before = materializer.tree_content_closure(
        source,
        ignore=shutil.ignore_patterns(*affiliate.MATERIALIZATION_IGNORE_NAMES),
    )
    try:
        report = materializer.copytree_linked(
            source,
            target,
            ignore=shutil.ignore_patterns(*affiliate.MATERIALIZATION_IGNORE_NAMES),
        )
        detachment = affiliate.detach_declared_mutations(target)
        for relative in detachment["detached_paths"]:
            (target / relative).write_text(f"candidate:{relative}\n", encoding="utf-8")

        provenance = materializer.constructor_provenance(
            constructor=AFFILIATE_PATH,
            predecessor=source,
            output=target,
            mutation_paths=[Path(path) for path in sorted(affiliate.DECLARED_MUTATION_PATHS)],
            materializations=[report],
            predecessor_before=before,
            ignored_names=affiliate.MATERIALIZATION_IGNORE_NAMES,
        )
        assert provenance["predecessor"]["unchanged"] is True
        assert provenance["topology"]["violations"] == []
        assert provenance["topology"]["unchanged_shared_files"] == 1
        assert (source / "src/unchanged.mjs").stat().st_ino == (target / "src/unchanged.mjs").stat().st_ino
        assert all(
            (source / relative).stat().st_ino != (target / relative).stat().st_ino
            for relative in required
        )
        assert not (target / "node_modules").exists()
        assert report["copied_files"] == 0
        assert report["copied_bytes"] == 0
    finally:
        cleanup_writable(source, target)


def test_affiliate_real_subprocess_chain_preserves_final_cow_topology(tmp_path: Path) -> None:
    pointer = json.loads(
        (BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json")
        .read_text(encoding="utf-8")
    )
    assert pointer["schema"] == "glp-opus-current-content-base-v1"
    assert pointer["status"] == "PASS"
    fixture = Path(pointer["release_root"])
    assert fixture.is_dir()
    source = tmp_path / "source"
    target = tmp_path / "target"
    required = affiliate.DECLARED_MUTATION_PATHS - affiliate.MAY_BE_ABSENT_BEFORE_WRITE
    supporting = {
        "data/program-content-ledger.json",
        "data/ready6-editorial-contract.json",
        "data/approved-program-registry.json",
        "public/assets/provider-logos/manifest.json",
        "scripts/reconcile-therapeutic-wire-in.mjs",
        "src/therapeutic-taxonomy.mjs",
        "src/provider-dynamic-profiles.mjs",
    }
    for relative in sorted(required | supporting):
        origin = fixture / relative
        assert origin.is_file(), relative
        destination = source / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, destination)
    (source / "src/unchanged.mjs").write_text("export const unchanged = true;\n", encoding="utf-8")
    make_immutable(source)
    ignore = shutil.ignore_patterns(*affiliate.MATERIALIZATION_IGNORE_NAMES)
    before = materializer.tree_content_closure(source, ignore=ignore)
    try:
        linked = materializer.copytree_linked(source, target, ignore=ignore)
        detached = affiliate.detach_declared_mutations(target)
        result = affiliate.run_surface_integration(target)
        assert result["status"] == "PASS"
        assert [row["step"] for row in result["steps"]] == [
            "research_merge", "surface_unification",
        ]
        assert all(row["returncode"] == 0 for row in result["steps"])
        assert affiliate.SURFACE_TRANSACTION_OUTPUTS <= set(detached["detached_paths"])
        assert not any(path.startswith("public/") for path in affiliate.DECLARED_MUTATION_PATHS)

        provenance = materializer.constructor_provenance(
            constructor=AFFILIATE_PATH,
            predecessor=source,
            output=target,
            mutation_paths=[Path(path) for path in sorted(affiliate.DECLARED_MUTATION_PATHS)],
            materializations=[linked],
            predecessor_before=before,
            ignored_names=affiliate.MATERIALIZATION_IGNORE_NAMES,
        )
        assert provenance["materializations"][0]["phase"] == "predecessor_to_output"
        assert provenance["topology"]["violations"] == []
        assert provenance["topology"]["unchanged_shared_files"] > 0
        assert materializer.tree_content_closure(source, ignore=ignore) == before
        for relative in affiliate.SURFACE_TRANSACTION_OUTPUTS:
            assert os.lstat(source / relative).st_ino != os.lstat(target / relative).st_ino
        assert os.lstat(source / "src/unchanged.mjs").st_ino == os.lstat(
            target / "src/unchanged.mjs"
        ).st_ino
    finally:
        cleanup_writable(source, target)


def test_affiliate_artifact_go_manifest_does_not_require_legacy_measured_template() -> None:
    manifest = {
        "programs": [{
            "program_id": "existing-live",
            "display_name": "Existing Live",
            "route": "/go/existing-live",
            "entry_state": "transparent_direct",
            "commercial_state": "live",
        }]
    }
    entry = affiliate.build_go_manifest_entry(
        "artifacts/ops/worker-manifest.staging.json",
        {"program_id": "new-live", "display_name": "New Live"},
        manifest,
    )
    assert entry == {
        "program_id": "new-live",
        "display_name": "New Live",
        "route": "/go/new-live",
        "entry_state": "transparent_direct",
        "commercial_state": "live",
    }


def test_affiliate_count_assertion_accepts_current_dynamic_contract(tmp_path: Path) -> None:
    target = tmp_path / "dynamic.test.mjs"
    target.write_text(
        "assert.equal(reviews.length, fastpath.expected_provider_review_count);\n",
        encoding="utf-8",
    )
    result = affiliate.bump_int_or_dynamic(
        target,
        r"assert\.equal\(reviews\.length, (\d+)\);",
        5,
        r"assert\.equal\(reviews\.length, fastpath\.expected_provider_review_count\);",
    )
    assert result is None
    assert target.read_text(encoding="utf-8") == (
        "assert.equal(reviews.length, fastpath.expected_provider_review_count);\n"
    )


def test_affiliate_field_sources_rebind_to_exact_dated_research_urls() -> None:
    spec = {
        "program_id": "fixture",
        "field_sources": {
            "pricing_eligibility_shipping": ["https://provider.test/current"],
            "program_terms": ["https://network.test/program/123"],
            "regulatory": ["https://fda.test/guidance"],
        },
    }
    research = {
        "fields": {
            "first_month_price": {"source_url": "https://provider.test/current"},
            "consultation_fee": {"source_url": "https://provider.test/current"},
            "shipping_wait": {"source_url": "https://provider.test/shipping"},
            "cancellation": {"source_url": "https://provider.test/terms"},
            "states_served": {"source_url": "https://network.test/program/123"},
            "pharmacy_type": {"source_url": "https://provider.test/current"},
        },
    }
    assert affiliate.canonical_field_sources(spec, research) == {
        "pricing_and_inclusions": [
            "https://provider.test/current", "https://provider.test/shipping",
        ],
        "cancellation_and_roles": [
            "https://provider.test/terms", "https://network.test/program/123",
            "https://provider.test/current",
        ],
        "regulatory": ["https://provider.test/current", "https://fda.test/guidance"],
    }
