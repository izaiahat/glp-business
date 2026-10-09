#!/usr/bin/env python3
"""Weekly GLP-1 Price Index release chain: candidate -> deploy -> remint -> sensor."""
from __future__ import annotations

import argparse
import base64
import csv
import io
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from site_tree_materializer import (  # noqa: E402
    FLAGSHIP_REQUIRED_PUBLIC_BUILD_INPUTS,
    constructor_provenance,
    copytree_linked,
    detach_for_write,
    make_directories_writable,
    prepare_clean_build_trees,
    remove_materialized_tree,
    restore_linked_subtree,
    seal_tree_selective,
    tree_content_closure,
    validate_constructor_provenance,
)
from glp_current_content_base_schema import (  # noqa: E402
    COUNT_KEYS as MOVING_BASE_COUNT_KEYS,
    POINTER_ALLOWED_KEYS as MOVING_BASE_ALLOWED_KEYS,
    POINTER_REQUIRED_KEYS as MOVING_BASE_REQUIRED_KEYS,
    is_prepared_pointer,
    validate_pointer_shape,
)

BUSINESS = Path("/home/ubuntu/business")
SITE_ROOT = BUSINESS / "site-system/flagship-v2"
CANDIDATES = SITE_ROOT / "candidates"
RELEASES = SITE_ROOT / "releases"
CURRENT_BASE = BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
GROWTH_ROOT = BUSINESS / "reports/glp-clinic-scout/growth/price-index"
REMINT = BUSINESS / "systems/seo-blog-machine/scripts/remint_content_base.py"
BUILDER_PATH = SCRIPT_DIR.parent / "systems/seo-blog-machine/scripts/build_opus_article_release.py"
OBSERVER_SOURCE = BUSINESS / "scripts/glp_price_index_weekly_sources/update-price-index.mjs"
SUPPORT_ROOT = OBSERVER_SOURCE.parent
SUITE_PATCHES_SHA256 = "58b767379939d91281ee57fa7e89d1363b7b3d2a9557a7e5b7636e0fdc1a60b8"
CSS_RETENTION_SHA256 = "ccb460ae1b6757e09d18ea1832b2bdfd51481e03c08e384c43f034630acf0866"
OBSERVER_BEFORE_SHA256 = "09339afce5df2e92d8b0a6bd682bf3fc718a3202e2533f38cf49065ff3b1cded"
OBSERVER_SHA256 = "07ba5c11c248c4c587ab163ac27efe6c86f2c4a4dc950a4804b2a3f9e7bad335"
NATIVE_RELEASE1_FIXTURE_SHA256 = "b073d42c833356ecfa5079d5ed42d3e9882b861f41e9eed414afe214dc43ada8"
NATIVE_RELEASE1_SOURCE_HASHES = {
    "scripts/build-staging.mjs": "277620a83a2dd0c6997487718d4dc4b8cb57b36c4609c4cc8730d807f1297e51",
    "data/d2w-s9-d2p-provenance-evidence.json": "9f44cbdd3bd4614405d8e125f358518ed2e9aa364e5df934a74c8490e94f7015",
    "scripts/postbuild-aeo-surface.mjs": "eccf925385bb48cb52c600794f3bdb889c69ab9e595481f3fdbc57e3319f52d1",
    "scripts/update-price-index.mjs": "07ba5c11c248c4c587ab163ac27efe6c86f2c4a4dc950a4804b2a3f9e7bad335",
    "src/inherited-disclosure-dates.mjs": "08716ffbf84c31bfcc892491556874e7af71298f2ccf9aa6c81a08a43b5dcdd3",
    "src/price-index-nonprice-binding.mjs": "ba1e543e00e3a78ca2f68fda10e8e542109fa79afc895df244be5cd30348e3de",
    "tests/fixtures/k9/postbuild-aeo-surface.preimage.mjs": None,
    "tests/k9-suite-repair.test.mjs": None,
    "tests/lastmod-declared-routes.test.mjs": ("e4c8d463161961f3d0c42a8b7f890a690e16a32e1a0275a293daed8a81d01cdc", NATIVE_RELEASE1_FIXTURE_SHA256),
    "tests/product-ready.test.mjs": "0c844afb42523b8d0a9427d10eca8ea377f114840418bf201e34c94b3531baa5",
}
ACCEPTED_FAILURE_CONTRACT = BUSINESS / "systems/seo-blog-machine/scripts/accepted_failure_contract.py"
ACCEPTED_FAILURE_CONTRACT_SHA256 = "9989f81c86de42b6be2aa547cc12ef48780399f828ff333cf9e36cce3f79255a"
LOCK_PATH = Path("/home/ubuntu/.hermes/locks/glp-price-index-weekly.lock")
PROJECT = "glp-clinic-scout"
# Verified 2026-08-08 against the live Pages project, current production head,
# and the established GLP deploy control libraries. These are public resource
# identities, not credentials.
ACCOUNT = "f860943dff399ffb1db0a28dcd3dadfd"
RUNTIME_D1 = "bcb91d2d-c3f1-4c55-93e3-070587e86d60"
WRANGLER = "4.114.0"
ROUTE = "/glp-1-price-index/"
PUBLIC_URL = f"https://glpclinicscout.com{ROUTE}"
SITEMAP_URL = "https://glpclinicscout.com/sitemap.xml"
INDEXNOW_ENDPOINT = "https://api.indexnow.org/indexnow"
INDEXNOW_HOST = "glpclinicscout.com"
INDEXNOW_ACCEPTED_STATUS = {200, 202}
WEEKLY_MUTABLE_FILES = (
    Path("data/price-tracker.json"),
    # The emitter may return PASS_UNCHANGED without replacing this inode.
    Path("data/tracking-allowed-hosts.json"),
    Path("data/page-content-provenance.json"),
    Path("functions/go/program-manifest.js"),
    Path("APPROVED-53-COVERAGE-GATE.md"),
    Path("SHED-IDENTITY-RECONCILIATION-20260806.md"),
    Path("calc.html"),
    Path("guide-mounjaro.html"),
    Path("guide-whatis.html"),
    Path("money-best.html"),
    Path("money-cost.html"),
    Path("quiz.html"),
    Path("rev-found.html"),
    Path("rev-shedrx.html"),
    Path("scripts/build-staging.mjs"),
    Path("scripts/postbuild-aeo-surface.mjs"),
    Path("scripts/update-price-index.mjs"),
)
# Bounded outputs of the accepted npm build graph. Inputs (source imagery,
# provider logos, CSS layers) remain shared/read-only. Do not chmod an asset
# directory recursively or derive mutation authority from a generated manifest.
GENERATED_MUTABLE_FILES = (
    Path("assets/provider-logos/pass2-lp-atlas.webp"),
    Path("assets/provider-logos/pass2-lp-atlas.json"),
    *(Path(f"assets/derivatives/{stem}-{width}.{extension}")
      for stem, widths in (
          ("home-hero-fruit", (640, 1024)),
          ("home-hero-fridge", (640, 1024)),
          ("home-hero-hand", (640, 1024)),
          ("home-lifestyle-phone", (768, 1536)),
          *((f"phase3-product-{slug}", (640, 1024))
            for slug in ("wegovy-pill", "wegovy", "ozempic", "mounjaro", "zepbound")),
      )
      for width in widths for extension in ("avif", "webp", "jpg")),
    Path("artifacts/ultracode/phase2-home/generated-image-lineage.json"),
    Path("artifacts/ultracode/phase3-product-renders/generated-image-lineage.json"),
    *(Path(f"assets/{name}.css") for name in (
        "site", "site-home", "site-money", "site-review", "site-editorial",
        "site-tools", "site-trust", "site-p3",
    )),
    # Builder checkpoint writers run before its public/ops reset. They must
    # retain readable bytes for the paired fallback, not be cleared as outputs.
    Path("artifacts/build4-preservation/glp-1-price-index/index.html"),
    Path("artifacts/build4-preservation/ops/price-index-manifest.json"),
    Path("artifacts/build4-preservation/ops/wave4-deal-ingestion-manifest.json"),
    Path("artifacts/build4-preservation/_headers"),
)
# These two checkpoint families are variable-width builder outputs. Only their
# existing regular files are detached; unknown siblings stay immutable/shared.
GENERATED_MUTABLE_SUBTREES = (
    Path("artifacts/build4-preservation/reviews"),
    Path("artifacts/build4-preservation/google-ads-api"),
)
GENERATED_TREES = (Path("public"), Path("artifacts/ops"))
CLEAN_BUILD_INPUTS = (
    *FLAGSHIP_REQUIRED_PUBLIC_BUILD_INPUTS,
    # The accepted predecessor's build-staging.mjs reads these Build 4 artifacts
    # before resetting public/ and artifacts/ops. Preserve them exactly so the
    # weekly chain can perform a real clean build from the current canonical base.
    Path("public/glp-1-price-index/index.html"),
    Path("artifacts/ops/price-index-manifest.json"),
    Path("artifacts/ops/wave4-deal-ingestion-manifest.json"),
)
PUBLIC_REFRESH_FILES = frozenset({
    Path("glp-1-price-index/index.html"),
    Path("glp-1-price-index/data/latest.json"),
    Path("glp-1-price-index/data/latest.csv"),
    Path("data/price-watch-issue.json"),
    Path("sitemap.xml"),
})
PRICE_INDEX_REFRESH_MARKER = 'const priceIndexRefresh = process.env.GLP_PRICE_INDEX_REFRESH === "1";'
PRICE_INDEX_PRESERVATION_BLOCK = '''  const normalizeRuntimeVersion = (html) => html.replace(/\\?v=[a-f0-9]{16}/g, "?v=ASSET_VERSION");
  if (normalizeRuntimeVersion(priceIndex.html) !== normalizeRuntimeVersion(preservedPriceIndexHtml.toString("utf8"))) {
    throw new Error("price_index_build4_semantic_preservation_mismatch");
  }
  const stripPriceIndexTransportHashes = (manifest) => {
    const clone = structuredClone(manifest);
    delete clone.html_sha256;
    delete clone.payload_sha256;
    return clone;
  };
  if (stableSha256(stripPriceIndexTransportHashes(priceIndex.manifest)) !==
      stableSha256(stripPriceIndexTransportHashes(preservedPriceIndexManifest))) {
    throw new Error("price_index_build4_manifest_semantic_preservation_mismatch");
  }
  await write(PRICE_INDEX_ROUTE_FILE, preservedPriceIndexHtml);
  await writeOps("price-index-manifest.json", preservedPriceIndexManifestBytes);'''
PRICE_INDEX_REFRESH_BLOCK = '''  const normalizeRuntimeVersion = (html) => html.replace(/\\?v=[a-f0-9]{16}/g, "?v=ASSET_VERSION");
  const priceIndexRefresh = process.env.GLP_PRICE_INDEX_REFRESH === "1";
  if (!priceIndexRefresh && normalizeRuntimeVersion(priceIndex.html) !== normalizeRuntimeVersion(preservedPriceIndexHtml.toString("utf8"))) {
    throw new Error("price_index_build4_semantic_preservation_mismatch");
  }
  const stripPriceIndexTransportHashes = (manifest) => {
    const clone = structuredClone(manifest);
    delete clone.html_sha256;
    delete clone.payload_sha256;
    return clone;
  };
  if (!priceIndexRefresh && stableSha256(stripPriceIndexTransportHashes(priceIndex.manifest)) !==
      stableSha256(stripPriceIndexTransportHashes(preservedPriceIndexManifest))) {
    throw new Error("price_index_build4_manifest_semantic_preservation_mismatch");
  }
  if (priceIndexRefresh) {
    await write(PRICE_INDEX_ROUTE_FILE, priceIndex.html);
    await writeOps("price-index-manifest.json", JSON.stringify(priceIndex.manifest, null, 2) + "\\n");
  } else {
    await write(PRICE_INDEX_ROUTE_FILE, preservedPriceIndexHtml);
    await writeOps("price-index-manifest.json", preservedPriceIndexManifestBytes);
  }'''

FROZEN_PROVENANCE_WRITE_MARKER = "const frozenRefreshed = entries"
FROZEN_PROVENANCE_WRITE_PREDICATE = "  const provenanceWritten = REFRESH || declaredRefreshed.length > 0;"
FROZEN_PROVENANCE_WRITE_BLOCK = '''  // Renderer-owned frozen routes may truthfully change both bytes and date.
  const frozenRefreshed = entries
    .filter(({ route, action }) => {
      if (action !== LASTMOD_ACTIONS.FROZEN) return false;
      const previous = provenance.pages?.[route];
      const next = nextProvenance.pages[route];
      return !previous || previous.content_sha256 !== next.content_sha256 || previous.lastmod !== next.lastmod;
    })
    .map(({ route }) => route);
  const provenanceWritten = REFRESH || declaredRefreshed.length > 0 || frozenRefreshed.length > 0;'''
FROZEN_PROVENANCE_RECEIPT_FIELD = "    provenance_written: provenanceWritten,"
FROZEN_PROVENANCE_RECEIPT_BLOCK = '''    provenance_written: provenanceWritten,
    frozen_routes_refreshed: [...frozenRefreshed].sort(),'''


# K9: exact accepted train4 source, not a marker-only relaxation. Keep the
# legacy adapter for its original generation; authenticate the entire modern
# postbuild preimage (including every catalog/runtime/material guard).
FROZEN_PROVENANCE_CURRENT_SOURCE_SHA256 = "9de33b85829e44e88af1352abf8543c02495a61bbd290f4df4b1df1d897cd225"
FROZEN_PROVENANCE_CURRENT_PREDICATE = (
    "  const provenanceWritten = REFRESH || declaredRefreshed.length > 0"
    " || catalogReconciled.length > 0 || runtimeOnlyPreserved.length > 0"
    " || materialReconciled.length > 0;"
)
FROZEN_PROVENANCE_CURRENT_WRITE_BLOCK = FROZEN_PROVENANCE_WRITE_BLOCK.replace(
    "  const provenanceWritten = REFRESH || declaredRefreshed.length > 0 || frozenRefreshed.length > 0;",
    FROZEN_PROVENANCE_CURRENT_PREDICATE[:-1] + " || frozenRefreshed.length > 0;",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def iso_now() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_file(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def _file_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): sha_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def preserve_predecessor_public_outputs(candidate: Path, predecessor: Path) -> dict[str, Any]:
    """Restore public as predecessor hardlinks plus private weekly outputs."""
    result = restore_linked_subtree(
        predecessor,
        candidate,
        Path("public"),
        PUBLIC_REFRESH_FILES,
        constructor=Path(__file__).resolve(),
    )
    topology = result["constructor_provenance"]["topology"]
    return {
        "status": "PASS_EXACT_WEEKLY_SCOPE",
        "allowed_paths": topology["declared_mutation_paths"],
        "modified_paths": topology["modified_paths"],
        "added_paths": topology["added_paths"],
        "deleted_paths": topology["deleted_paths"],
        "unchanged_shared_files": topology["unchanged_shared_files"],
        "private_mutation_files": topology["private_mutation_files"],
        "materialization": result["materialization"],
        "detachments": result["detachments"],
        "constructor_provenance": result["constructor_provenance"],
    }


def load_suite_patches():
    manifest = SUPPORT_ROOT / "suite-patches.json"
    if manifest.is_symlink() or sha_file(manifest) != SUITE_PATCHES_SHA256:
        raise RuntimeError("suite_patches_pin_drift")
    records = json.loads(manifest.read_text())
    for row in records:
        relative = Path(row["path"])
        if relative.is_absolute() or any(p in ("", ".", "..") for p in row["path"].split("/")):
            raise RuntimeError("suite_patch_path_invalid")
        source = SUPPORT_ROOT / relative
        if source.is_symlink() or not source.is_file() or sha_file(source) != row["after_sha256"]:
            raise RuntimeError("suite_patch_source_drift:" + row["path"])
    return records


def native_release1_sources(candidate: Path) -> bool:
    """Exact approved native fixes; never overwrite with legacy overlays."""
    for relative, expected in NATIVE_RELEASE1_SOURCE_HASHES.items():
        path = candidate / relative
        if path.is_symlink() or (path.exists() and not path.is_file()):
            return False
        actual = sha_file(path) if path.is_file() else None
        if actual not in (expected if isinstance(expected, tuple) else (expected,)):
            return False
    return True


def preserve_nonindex_sitemap(candidate: Path, predecessor: Path) -> dict[str, Any]:
    """Advance only the index entry, retaining every other predecessor entry."""
    import xml.etree.ElementTree as ET
    namespace = 'http://www.sitemaps.org/schemas/sitemap/0.9'
    index_url = 'https://glpclinicscout.com/glp-1-price-index/'
    old_path, new_path = predecessor / 'public/sitemap.xml', candidate / 'public/sitemap.xml'
    old_root, new_root = ET.parse(old_path).getroot(), ET.parse(new_path).getroot()
    def entries(root):
        if root.tag != '{' + namespace + '}urlset':
            raise RuntimeError('price_sitemap_namespace')
        result = {}
        for entry in root:
            loc = entry.find('{' + namespace + '}loc')
            if entry.tag != '{' + namespace + '}url' or loc is None or not loc.text or loc.text in result:
                raise RuntimeError('price_sitemap_invalid_or_duplicate_url')
            result[loc.text] = entry
        return result
    old, new = entries(old_root), entries(new_root)
    if old.keys() != new.keys() or index_url not in old:
        raise RuntimeError('price_sitemap_inventory_drift')
    index = list(old_root).index(old[index_url])
    old_root.remove(old[index_url])
    old_root.insert(index, new[index_url])
    ET.register_namespace('', namespace)
    before = sha_file(new_path)
    detach_for_write(new_path)
    ET.ElementTree(old_root).write(new_path, encoding='utf-8', xml_declaration=True)
    return {'status': 'PASS_INDEX_ENTRY_ONLY', 'preserved_url_count': len(old) - 1,
            'predecessor_sha256': sha_file(old_path), 'generated_sha256': before,
            'output_sha256': sha_file(new_path)}


def suite_mutation_paths(root: Path, repairs) -> set[Path]:
    """Declare only suite files actually replaced on the executed branch."""
    if isinstance(repairs, list):
        return {Path(row["path"]).relative_to(root) for row in repairs
                if row["before_sha256"] != row["sha256"]}
    return {Path(path) for path in repairs["changed"]}


def apply_suite_repairs(root: Path):
    if native_release1_sources(root):
        source = SUPPORT_ROOT / "tests/lastmod-declared-routes.release1.mjs"
        if source.is_symlink() or sha_file(source) != NATIVE_RELEASE1_FIXTURE_SHA256:
            raise RuntimeError("native_release1_fixture_source_drift")
        target = root / "tests/lastmod-declared-routes.test.mjs"
        before = sha_file(target)
        if before != NATIVE_RELEASE1_FIXTURE_SHA256:
            detach_for_write(target)
            target.write_bytes(source.read_bytes())
        return [{"mode": "NATIVE_RELEASE1_WITH_COMPLETE_TEST_FIXTURE", "path": str(target),
                 "before_sha256": before, "sha256": sha_file(target)}]
    records = load_suite_patches()
    pending = []
    for row in records:
        target = root / row["path"]
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            raise RuntimeError("suite_patch_target_not_confined")
        before = sha_file(target) if target.is_file() else None
        if before == row["after_sha256"]:
            continue
        if before != row["before_sha256"]:
            raise RuntimeError("suite_patch_preimage_drift:" + row["path"])
        pending.append(row)
    for row in pending:
        target = root / row["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
        with temporary.open("xb") as stream:
            stream.write((SUPPORT_ROOT / row["path"]).read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    return {"changed": [r["path"] for r in pending], "patches": records}


def retain_weekly_css_authoring(root: Path, predecessor: Path):
    source = SUPPORT_ROOT / "css_retention.py"
    if source.is_symlink() or sha_file(source) != CSS_RETENTION_SHA256:
        raise RuntimeError("css_retention_source_drift")
    spec = importlib.util.spec_from_file_location("price_index_css_retention", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.retain_weekly_css_authoring(root, predecessor)


def ensure_price_index_observer_mode(root: Path) -> dict[str, Any]:
    """Apply the exact incoming Release-1 frozen-ledger observer, candidate-only."""
    target = root / "scripts/update-price-index.mjs"
    if target.is_symlink() or not target.is_file():
        raise RuntimeError("observer_source_drift")
    before = sha_file(target)
    if before == OBSERVER_SHA256:
        return {"changed": False, "sha256": before}
    if before != OBSERVER_BEFORE_SHA256:
        raise RuntimeError("observer_source_drift")
    if OBSERVER_SOURCE.is_symlink() or sha_file(OBSERVER_SOURCE) != OBSERVER_SHA256:
        raise RuntimeError("observer_patch_pin_drift")
    temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
    with temporary.open("xb") as stream:
        stream.write(OBSERVER_SOURCE.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    if sha_file(target) != OBSERVER_SHA256:
        raise RuntimeError("observer_installed_hash_mismatch")
    return {"changed": True, "before_sha256": before, "sha256": OBSERVER_SHA256}


def load_accepted_failure_contract():
    if ACCEPTED_FAILURE_CONTRACT.is_symlink() or sha_file(ACCEPTED_FAILURE_CONTRACT) != ACCEPTED_FAILURE_CONTRACT_SHA256:
        raise RuntimeError("accepted_failure_contract_source_drift")
    spec = importlib.util.spec_from_file_location("price_index_accepted_failure_contract", ACCEPTED_FAILURE_CONTRACT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ensure_price_index_refresh_mode(root: Path) -> dict[str, Any]:
    """Install the bounded weekly-refresh branch in the candidate build only."""
    target = root / "scripts/build-staging.mjs"
    if native_release1_sources(root):
        return {"status": "native_release1_unchanged", "path": str(target), "sha256": sha_file(target)}
    before = target.read_text(encoding="utf-8")
    before_sha256 = sha_bytes(before.encode())
    # Exact accepted train4 builder already has the bounded refresh branch.
    # Authenticate ALL source bytes: comparator, source-ledger/registry guards,
    # pass2 shell migration and both Dataset writers remain unmodified. Unknown
    # successors still fail closed below; this is not marker-only acceptance.
    if before_sha256 == "d1189ba5715e2d0869454c9138dd7d5632bf0ea138fd2b553a2a67b8865582e2":
        return {"status": "already_installed", "contract": "train4-price-index-refresh-v1", "path": str(target), "sha256": before_sha256}
    if "normalizePriceIndexBuild4Semantics" in before:
        raise RuntimeError("price_index_current_refresh_source_drift")
    if PRICE_INDEX_REFRESH_MARKER in before:
        if PRICE_INDEX_REFRESH_BLOCK not in before:
            raise RuntimeError("price_index_refresh_contract_drift")
        return {"status": "already_installed", "path": str(target), "sha256": before_sha256}
    if before.count(PRICE_INDEX_PRESERVATION_BLOCK) != 1:
        raise RuntimeError("price_index_preservation_contract_not_exact")
    after = before.replace(PRICE_INDEX_PRESERVATION_BLOCK, PRICE_INDEX_REFRESH_BLOCK)
    target.write_text(after, encoding="utf-8")
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    return {
        "status": "installed",
        "path": str(target),
        "before_sha256": before_sha256,
        "sha256": sha_file(target),
    }


def ensure_price_index_frozen_provenance_mode(root: Path) -> dict[str, Any]:
    target = root / "scripts" / "postbuild-aeo-surface.mjs"
    if native_release1_sources(root):
        return {"status": "native_release1_unchanged", "path": str(target), "sha256": sha_file(target)}
    before = target.read_text(encoding="utf-8")
    before_sha256 = sha_file(target)

    if before_sha256 == "94a242df8a65e60f7f2a552fc961a98a8257f4fc3c423f194573a8c526ee1713":
        return {"status": "already_installed", "path": str(target), "sha256": before_sha256}
    if FROZEN_PROVENANCE_CURRENT_PREDICATE in before or FROZEN_PROVENANCE_CURRENT_WRITE_BLOCK in before:
        installed = FROZEN_PROVENANCE_CURRENT_WRITE_BLOCK in before
        original = before
        if installed:
            if before.count(FROZEN_PROVENANCE_CURRENT_WRITE_BLOCK) != 1 or before.count(FROZEN_PROVENANCE_RECEIPT_BLOCK) != 1:
                raise RuntimeError("postbuild_current_frozen_provenance_contract_drift")
            original = before.replace(FROZEN_PROVENANCE_CURRENT_WRITE_BLOCK, FROZEN_PROVENANCE_CURRENT_PREDICATE, 1).replace(
                FROZEN_PROVENANCE_RECEIPT_BLOCK, FROZEN_PROVENANCE_RECEIPT_FIELD, 1
            )
        if sha_bytes(original.encode("utf-8")) != FROZEN_PROVENANCE_CURRENT_SOURCE_SHA256:
            raise RuntimeError("postbuild_current_frozen_provenance_source_drift")
        if installed:
            return {"status": "already_installed", "path": str(target), "sha256": before_sha256}
        after = before.replace(FROZEN_PROVENANCE_CURRENT_PREDICATE, FROZEN_PROVENANCE_CURRENT_WRITE_BLOCK, 1).replace(
            FROZEN_PROVENANCE_RECEIPT_FIELD, FROZEN_PROVENANCE_RECEIPT_BLOCK, 1
        )
        target.write_text(after, encoding="utf-8")
        target.chmod(0o644)
        return {"status": "installed", "path": str(target), "before_sha256": before_sha256, "sha256": sha_file(target)}

    if FROZEN_PROVENANCE_WRITE_MARKER in before:
        if FROZEN_PROVENANCE_WRITE_BLOCK not in before or FROZEN_PROVENANCE_RECEIPT_BLOCK not in before:
            raise RuntimeError(
                "postbuild frozen-provenance transform marker exists without the exact expected contract"
            )
        return {
            "status": "already_installed",
            "path": str(target),
            "sha256": before_sha256,
        }

    if before.count(FROZEN_PROVENANCE_WRITE_PREDICATE) != 1:
        raise RuntimeError(
            "postbuild frozen-provenance write predicate was not found exactly once"
        )
    if before.count(FROZEN_PROVENANCE_RECEIPT_FIELD) != 1:
        raise RuntimeError(
            "postbuild frozen-provenance receipt field was not found exactly once"
        )

    after = before.replace(
        FROZEN_PROVENANCE_WRITE_PREDICATE,
        FROZEN_PROVENANCE_WRITE_BLOCK,
        1,
    ).replace(
        FROZEN_PROVENANCE_RECEIPT_FIELD,
        FROZEN_PROVENANCE_RECEIPT_BLOCK,
        1,
    )
    target.write_text(after, encoding="utf-8")
    target.chmod(0o644)
    return {
        "status": "installed",
        "path": str(target),
        "before_sha256": before_sha256,
        "sha256": sha_file(target),
    }


def load_builder():
    spec = importlib.util.spec_from_file_location("glp_price_index_builder", BUILDER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("release_builder_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = load_builder()


def closure(root: Path) -> tuple[str, list[dict[str, Any]]]:
    rows = BUILDER.rows(root)
    return BUILDER.closure(rows), rows


def no_symlinks(root: Path) -> None:
    for current, dirs, files in os.walk(root, followlinks=False):
        base = Path(current)
        for name in [*dirs, *files]:
            if (base / name).is_symlink():
                raise RuntimeError(f"release_symlink_forbidden:{base / name}")


def write_json_0600(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def write_text_0600(path: Path, text: str) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = text.encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(encoded)
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(temporary, path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return {
        "path": str(path),
        "sha256": sha_bytes(encoded),
        "bytes": len(encoded),
    }


def safe_tail(text: str, limit: int = 4000) -> str:
    text = text.replace("\x1b", "")
    return text[-limit:]


def run_command(
    receipt: dict[str, Any],
    command: list[str],
    *,
    cwd: Path,
    timeout: int = 1800,
    env: dict[str, str] | None = None,
    output_log_dir: Path | None = None,
    output_log_stem: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if (output_log_dir is None) != (output_log_stem is None):
        raise ValueError("output_log_dir and output_log_stem must be provided together")
    if output_log_stem is not None and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", output_log_stem) is None:
        raise ValueError("output_log_stem contains unsafe characters")
    started = iso_now()
    # The shared Pages command boundary (also used by sealed successors and
    # Affiliati ship) must run the EXISTING sensor before publication.
    if "pages" in command and "deploy" in command:
        from glp_deploy_sensor_hook import attach_sensor_result

        if ("--project-name" not in command or command[command.index("--project-name") + 1] != PROJECT
                or "--branch" not in command or command[command.index("--branch") + 1] != "main"):
            raise RuntimeError("predeploy_sensor_target_unestablished")
        public = Path(command[command.index("deploy") + 1])
        if not public.is_absolute():
            public = cwd / public
        receipt_path = Path(receipt.get("receipt_path") or (BUSINESS / "reports/glp-clinic-scout/affiliate-machine" / f"predeploy-{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}.json"))
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        sensor = attach_sensor_result(receipt_path, public.parent, offline=True, post_alert=False,
                                      live_configuration=True, runtime_root=cwd)
        receipt["sensor_result"] = sensor
        if sensor.get("status") not in {"PASS", "PASS_PARTIAL"} or sensor.get("failure_count") != 0:
            raise RuntimeError(f"predeploy_sensor_failed:{sensor.get('receipt_path')}")
    proc = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout)
    command_record: dict[str, Any] = {
        "command": " ".join(command),
        "cwd": str(cwd),
        "started_at_utc": started,
        "exit_code": proc.returncode,
        "stdout_tail": safe_tail(proc.stdout),
        "stderr_tail": safe_tail(proc.stderr),
    }
    if output_log_dir is not None and output_log_stem is not None:
        command_record["full_output_logs"] = {
            "stdout": write_text_0600(output_log_dir / f"{output_log_stem}.stdout.log", proc.stdout),
            "stderr": write_text_0600(output_log_dir / f"{output_log_stem}.stderr.log", proc.stderr),
        }
    receipt.setdefault("commands", []).append(command_record)
    if proc.returncode != 0:
        raise RuntimeError(f"command_failed:{command[0]}:{proc.returncode}:{safe_tail(proc.stderr, 500)}")
    return proc


def credential(env_name: str, file_path: Path) -> str:
    value = os.getenv(env_name, "").strip()
    if not value and file_path.is_file():
        value = file_path.read_text(encoding="utf-8").strip()
    if not value or any(char.isspace() for char in value):
        raise RuntimeError(f"credential_missing_or_invalid:{env_name}")
    return value


def cf_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def cf_json(token: str, path: str) -> dict[str, Any]:
    response = requests.get(f"https://api.cloudflare.com/client/v4{path}", headers=cf_headers(token), timeout=45)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"cloudflare_api_failed:{path}")
    return payload


def production_head(token: str) -> dict[str, Any]:
    payload = cf_json(token, f"/accounts/{ACCOUNT}/pages/projects/{PROJECT}/deployments?env=production&per_page=25")
    rows = [row for row in payload.get("result", []) if row.get("environment") == "production" and row.get("latest_stage", {}).get("status") == "success"]
    if not rows:
        raise RuntimeError("cloudflare_production_head_missing")
    return sorted(rows, key=lambda row: str(row.get("created_on", "")), reverse=True)[0]


def wait_for_production_head(token: str, deployment_id: str, *, attempts: int = 24, delay_seconds: int = 5) -> dict[str, Any]:
    """Boundedly absorb Cloudflare's post-deploy list/read-after-write lag."""
    last: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        last = production_head(token)
        if last.get("id") == deployment_id:
            return last
        if attempt < attempts:
            time.sleep(delay_seconds)
    raise RuntimeError(f"new_production_head_invalid:{last.get('id')}:{deployment_id}")


def deployment_detail(token: str, deployment_id: str) -> dict[str, Any]:
    return cf_json(token, f"/accounts/{ACCOUNT}/pages/projects/{PROJECT}/deployments/{deployment_id}").get("result", {})


def project_detail(token: str) -> dict[str, Any]:
    return cf_json(token, f"/accounts/{ACCOUNT}/pages/projects/{PROJECT}").get("result", {})


def d1_ids(payload: dict[str, Any]) -> list[str]:
    """Read current and legacy Pages API D1 binding shapes without guessing.

    2026-08-28: `latest_deployment` is whatever deployment landed most recently
    on the PROJECT — including any PREVIEW from any branch. A funnel-preview
    deployment (branch paid-acquisition-funnel-20260827, carrying the stale
    candidate D1 6f0331db) therefore poisoned this set for the production gate
    and refused a production deploy whose own canonical binding was correct and
    unanimous. A preview's binding is not evidence about production.

    So `latest_deployment` is consulted ONLY when it is itself production. The
    check stays exact — canonical production, production deployment config, and
    the resolved production deployment must all still equal RUNTIME_D1, and a
    genuinely wrong PRODUCTION binding fails exactly as before. Deployment-detail
    payloads (already resolved to one specific deployment by the caller) are
    unaffected: they carry no `latest_deployment` key.
    """
    latest = payload.get("latest_deployment") or {}
    latest_is_production = str(latest.get("environment", "")).lower() == "production"
    maps = [
        payload.get("d1_databases", {}),
        payload.get("deployment_configs", {}).get("production", {}).get("d1_databases", {}),
        latest.get("d1_databases", {}) if latest_is_production else {},
        payload.get("canonical_deployment", {}).get("d1_databases", {}),
    ]
    ids = {
        str(binding.get("id"))
        for bindings in maps if isinstance(bindings, dict)
        for binding in bindings.values() if isinstance(binding, dict) and binding.get("id")
    }
    legacy = payload.get("functions_config", {}).get("bindings", {})
    ids.update(str(binding.get("id")) for binding in legacy.values() if isinstance(binding, dict) and binding.get("type") == "d1" and binding.get("id"))
    return sorted(ids)


def canonical_deployment_id(detail: dict[str, Any], pages_hostname_id: str) -> str:
    """Promote the eight-character Pages hostname ID to the full API UUID."""
    deployment_id = str(detail.get("id", "")).lower()
    hostname_id = pages_hostname_id.lower()
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", deployment_id):
        raise RuntimeError("cloudflare_deployment_full_id_invalid")
    if not deployment_id.startswith(f"{hostname_id}-"):
        raise RuntimeError("cloudflare_deployment_hostname_id_mismatch")
    return deployment_id


def _valid_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


_BASE_COUNT_KEYS_V1 = MOVING_BASE_COUNT_KEYS
_POINTER_REQUIRED_KEYS_V1 = MOVING_BASE_REQUIRED_KEYS
_POINTER_ALLOWED_KEYS_V1 = MOVING_BASE_ALLOWED_KEYS
_MANIFEST_REQUIRED_KEYS_V1 = frozenset({
    "schema", "created_at_utc", "release_root", "release_rows", "release_tree_sha256", "note",
})
_MANIFEST_ALLOWED_KEYS_V1 = (
    _MANIFEST_REQUIRED_KEYS_V1 | _BASE_COUNT_KEYS_V1
)
_FINAL_RECEIPT_REQUIRED_KEYS_V1 = frozenset({
    "schema", "status", "created_at_utc", "deployment", "evidence_sha256", "immutable_closure",
})
_FINAL_RECEIPT_ALLOWED_KEYS_V1 = (
    _FINAL_RECEIPT_REQUIRED_KEYS_V1
    | frozenset({"note"})
    | _BASE_COUNT_KEYS_V1
)
_EVIDENCE_SPEC_KEYS_V1 = frozenset({"path", "sha256"})
_ROW_SCHEMAS_V1 = {
    frozenset({"path", "sha256", "bytes"}): ("current-path-sha256-bytes-v1", "bytes"),
    frozenset({"path", "sha256", "size"}): ("legacy-path-size-sha256-v1", "size"),
}
_PATH_NORMALIZATION_FORMS = ("NFC", "NFD", "NFKC", "NFKD")


def _require_outer_keys_v1(
    payload: Any,
    required: frozenset[str],
    allowed: frozenset[str],
    error: str,
) -> None:
    if not isinstance(payload, dict) or not required.issubset(payload) or not set(payload).issubset(allowed):
        raise RuntimeError(error)


def _stat_signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_uid, info.st_gid,
        info.st_size, info.st_mtime_ns, info.st_ctime_ns,
    )


def _read_bound_regular_file(raw_path: str, field: str) -> tuple[Path, bytes, tuple[int, ...]]:
    """Read one exactly spelled canonical regular file and prove stable identity."""
    try:
        path = Path(raw_path)
        resolved_before = path.resolve(strict=True)
        if str(resolved_before) != raw_path:
            raise RuntimeError
        before_path = os.lstat(raw_path)
    except (OSError, RuntimeError):
        raise RuntimeError(f"current_content_base_{field}_path_invalid") from None
    if not stat.S_ISREG(before_path.st_mode):
        raise RuntimeError(f"current_content_base_{field}_path_invalid")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(raw_path, flags)
    except OSError:
        raise RuntimeError(f"current_content_base_{field}_path_invalid") from None
    try:
        before_fd = os.fstat(fd)
        if not stat.S_ISREG(before_fd.st_mode) or _stat_signature(before_fd) != _stat_signature(before_path):
            raise RuntimeError(f"current_content_base_{field}_identity_changed")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after_fd = os.fstat(fd)
        try:
            after_path = os.lstat(raw_path)
            resolved_after = path.resolve(strict=True)
        except (OSError, RuntimeError):
            raise RuntimeError(f"current_content_base_{field}_identity_changed") from None
    except OSError:
        raise RuntimeError(f"current_content_base_{field}_identity_changed") from None
    finally:
        os.close(fd)
    signature = _stat_signature(before_fd)
    if (
        str(resolved_after) != raw_path
        or signature != _stat_signature(after_fd)
        or signature != _stat_signature(after_path)
    ):
        raise RuntimeError(f"current_content_base_{field}_identity_changed")
    return path, b"".join(chunks), signature


def _checked_base_evidence(
    pointer: dict[str, Any], field: str, expected_name: str
) -> dict[str, Any]:
    spec = pointer.get(field)
    if not isinstance(spec, dict) or set(spec) != _EVIDENCE_SPEC_KEYS_V1:
        raise RuntimeError(f"current_content_base_{field}_spec_invalid")
    raw_path = spec.get("path")
    expected_sha = spec.get("sha256")
    if not _valid_sha256(expected_sha):
        raise RuntimeError(f"current_content_base_{field}_hash_invalid")
    if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
        raise RuntimeError(f"current_content_base_{field}_path_invalid")
    if (
        not os.path.isabs(raw_path)
        or raw_path.rsplit("/", 1)[-1] != expected_name
    ):
        raise RuntimeError(f"current_content_base_{field}_path_invalid")
    path, data, identity = _read_bound_regular_file(raw_path, field)
    if sha_bytes(data) != expected_sha:
        raise RuntimeError(f"current_content_base_{field}_hash_mismatch")
    return {
        "raw_path": raw_path,
        "path": path,
        "sha256": expected_sha,
        "bytes": data,
        "identity": identity,
    }


def _recheck_base_evidence(bound: dict[str, Any], field: str) -> None:
    path, data, identity = _read_bound_regular_file(bound["raw_path"], field)
    if path != bound["path"]:
        raise RuntimeError(f"current_content_base_{field}_identity_changed")
    if identity != bound["identity"]:
        raise RuntimeError(f"current_content_base_{field}_identity_changed")
    if data != bound["bytes"] or sha_bytes(data) != bound["sha256"]:
        raise RuntimeError(f"current_content_base_{field}_bytes_changed")


def _normalized_release_path(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) <= 0x1F or ord(character) == 0x7F for character in value)
    ):
        raise RuntimeError("manifest_release_row_path_invalid")
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or relative.as_posix() != value
        or value in {".", ".."}
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise RuntimeError("manifest_release_row_path_invalid")
    return value


def _normalized_manifest_rows(value: Any) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(value, list) or not value:
        raise RuntimeError("manifest_release_rows_invalid")
    selected: tuple[str, str] | None = None
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    normalized_seen: dict[str, dict[str, str]] = {
        form: {} for form in _PATH_NORMALIZATION_FORMS
    }
    for row in value:
        if not isinstance(row, dict):
            raise RuntimeError("manifest_release_row_not_object")
        row_schema = _ROW_SCHEMAS_V1.get(frozenset(row))
        if row_schema is None or (selected is not None and row_schema != selected):
            raise RuntimeError("manifest_release_row_schema_invalid")
        selected = row_schema
        path = _normalized_release_path(row.get("path"))
        if path in seen:
            raise RuntimeError("manifest_release_row_duplicate")
        seen.add(path)
        for form, paths in normalized_seen.items():
            normalized_path = unicodedata.normalize(form, path)
            prior = paths.get(normalized_path)
            if prior is not None and prior != path:
                raise RuntimeError("manifest_release_row_normalization_collision")
            paths[normalized_path] = path
        item_sha = row.get("sha256")
        if not _valid_sha256(item_sha):
            raise RuntimeError("manifest_release_row_sha256_invalid")
        size_key = row_schema[1]
        size = row.get(size_key)
        if type(size) is not int or size < 0:
            raise RuntimeError("manifest_release_row_size_invalid")
        normalized.append({"path": path, "sha256": item_sha, size_key: size})
    assert selected is not None
    normalized.sort(key=lambda row: row["path"].encode("utf-8"))
    return selected[0], normalized


def _complete_current_rows(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        paths = list(root.rglob("*"))
    except OSError:
        raise RuntimeError("current_content_base_release_walk_failed") from None
    paths.sort(key=lambda path: path.relative_to(root).as_posix().encode("utf-8"))
    for path in paths:
        try:
            info = os.lstat(path)
        except OSError:
            raise RuntimeError("current_content_base_release_walk_failed") from None
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeError(f"release_symlink_forbidden:{path}")
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError(f"release_special_file_forbidden:{path}")
        out.append({
            "path": path.relative_to(root).as_posix(),
            "sha256": sha_file(path),
            "bytes": info.st_size,
        })
    return out


def _legacy_rows(current_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"path": row["path"], "sha256": row["sha256"], "size": row["bytes"]}
        for row in current_rows
    ]


def _legacy_closure(rows: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda item: item["path"].encode("utf-8")):
        digest.update(f"{row['path']}\0{row['size']}\0{row['sha256']}\n".encode("utf-8"))
    return digest.hexdigest()


def _computed_base_counts(
    current_rows: list[dict[str, Any]], public_rows: list[dict[str, Any]]
) -> dict[str, int]:
    return {
        "release_files": len(current_rows),
        "release_bytes": sum(row["bytes"] for row in current_rows),
        "public_files": len(public_rows),
        "public_bytes": sum(row["bytes"] for row in public_rows),
    }


def _validate_count_fields(payload: dict[str, Any], counts: dict[str, int], label: str) -> None:
    for field, expected in counts.items():
        if field in payload and (type(payload[field]) is not int or payload[field] != expected):
            raise RuntimeError(f"{label}_{field}_count_mismatch")


def _load_base_document(data: bytes, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(data)
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError(f"legacy_{label}_json_invalid") from None
    if not isinstance(payload, dict):
        raise RuntimeError(f"legacy_{label}_json_invalid")
    return payload


def _prepared_base_links(
    pointer: dict[str, Any], release: Path, manifest_path: Path, receipt_path: Path,
    manifest: dict[str, Any], release_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind the GLP-43 publisher's preparation to its public terminal evidence."""
    head = pointer["site_head"]
    prepared_dir = release.parent
    ops_root = BUSINESS / "reports/ops"
    affiliate_runs = BUSINESS / 'reports/glp-clinic-scout/affiliate-machine/registry-site-sync'
    affiliate_root = (prepared_dir.parent.parent == affiliate_runs
                      and prepared_dir.name == 'prepared'
                      and re.fullmatch(r'(?:main-)?20\d{6}T\d{6}Z', prepared_dir.parent.name) is not None)
    if (release.name != "release" or (ops_root.resolve(strict=True) not in prepared_dir.parents and not affiliate_root)
            or prepared_dir.resolve(strict=True) != prepared_dir
            or release.resolve(strict=True) != release
            or prepared_dir.is_symlink()):
        raise RuntimeError("current_content_base_prepared_root_invalid")
    if (manifest_path != CURRENT_BASE.parent / f"current-release-{head}/01-deployment-manifest.json"
            or receipt_path != manifest_path.parent / "10-final-publication-receipt.json"):
        raise RuntimeError("current_content_base_prepared_publication_path_invalid")
    prepared_path, raw, identity = _read_bound_regular_file(str(prepared_dir / "PREPARED.json"), "prepared")
    prepared = _load_base_document(raw, "prepared")
    if (prepared.get("schema") != "glp43-current-prepared-v1"
            or prepared.get("prepared_dir") != str(prepared_dir)
            or prepared.get("site_head") != head
            or prepared.get("site_repository") != "izaiahat/glp-site-candidate"
            or prepared.get("project") != "glp-clinic-scout"
            or prepared.get("branch") != "main"
            or prepared.get("release_authorized") is not False
            or prepared.get("provider_mutations") != 0):
        raise RuntimeError("current_content_base_prepared_identity_mismatch")
    previous = prepared.get("predecessor_pointer")
    if not isinstance(previous, dict):
        raise RuntimeError("current_content_base_predecessor_invalid")
    try:
        prior_bytes = base64.b64decode(previous["bytes_base64"], validate=True)
        prior = json.loads(prior_bytes)
        validate_pointer_shape(prior, require_release_dirs=True)
    except (KeyError, TypeError, ValueError, RuntimeError, json.JSONDecodeError):
        raise RuntimeError("current_content_base_predecessor_invalid") from None
    if (previous.get("path") != str(CURRENT_BASE)
            or previous.get("sha256") != sha_bytes(prior_bytes)
            or previous.get("bytes") != len(prior_bytes)
            or previous.get("deployment_id") != prior["deployment_id"]
            or previous.get("release_root") != prior["release_root"]
            or pointer["supersedes"] != {
                "deployment_id": prior["deployment_id"], "release_root": prior["release_root"]
            }
            or prepared.get("rollback_deployment_id") != prior["deployment_id"]):
        raise RuntimeError("current_content_base_predecessor_mismatch")
    release_closure = {
        "tree_sha256": _legacy_closure(release_rows), "file_count": len(release_rows),
        "total_bytes": sum(row["size"] for row in release_rows),
    }
    deploy = prepared_dir / "deploy-public"
    if deploy.resolve(strict=True) != deploy or deploy.is_symlink() or not deploy.is_dir():
        raise RuntimeError("current_content_base_deploy_directory_invalid")
    compiled = prepared.get("compiled")
    if (not isinstance(compiled, dict) or not _valid_sha256(compiled.get("worker_sha256"))
            or not (deploy / "_worker.js").is_file()
            or (deploy / "_worker.js").is_symlink()
            or sha_file(deploy / "_worker.js") != compiled["worker_sha256"]):
        raise RuntimeError("current_content_base_prepared_worker_mismatch")
    deploy_rows = _legacy_rows(_complete_current_rows(deploy))
    deploy_closure = {
        "tree_sha256": _legacy_closure(deploy_rows), "file_count": len(deploy_rows),
        "total_bytes": sum(row["size"] for row in deploy_rows),
    }
    if (prepared.get("release") != release_closure
            or prepared.get("deploy_directory") != deploy_closure
            or manifest.get("deploy_directory") != deploy_closure
            or pointer["deploy_directory_sha256"] != deploy_closure["tree_sha256"]):
        raise RuntimeError("current_content_base_prepared_closure_mismatch")
    binding = _registered_prepared_binding(pointer, prepared_dir, prepared, raw, manifest_path)
    _, final_bytes, final_identity = _read_bound_regular_file(str(prepared_path), "prepared")
    if final_identity != identity or final_bytes != raw:
        raise RuntimeError("current_content_base_prepared_identity_changed")
    binding["files"][str(prepared_path)] = (sha_bytes(raw), identity)
    return binding


def _native_current_deploy_binding(pointer, root, prepared, raw, run_dir, producer):
    """Accept the registered direct terminal path as well as historical promotions."""
    original_path = run_dir / "CURRENT-RECEIPT.json"
    _path, receipt_raw, receipt_identity = _read_bound_regular_file(str(original_path), "current_deploy")
    terminal = json.loads(receipt_raw)
    approval_path = root.parent / "astra-final-review/RESPONSE.md"
    gate_path = root / 'gate.json'
    if (gate_path.is_file() and sha_bytes(gate_path.read_bytes()) == prepared['build_receipt']['sha256']
            and json.loads(gate_path.read_bytes()).get('schema') == 'glp-affiliate-sync-release-result-v1'):
        approval_path = root.parent / 'OPERATOR-RELEASE.json'
    _path, approval_raw, approval_identity = _read_bound_regular_file(str(approval_path), "current_approval")
    binding = {"prepared_manifest_sha256": sha_bytes(raw), "approval_sha256": sha_bytes(approval_raw)}
    producer.current_approval(argparse.Namespace(approval=str(approval_path),
        expected_approval_sha256=binding["approval_sha256"],
        expected_prepared_manifest_sha256=binding["prepared_manifest_sha256"],
        authorize_production=True), prepared)
    deployment_id = pointer["deployment_id"]
    intent = producer.current_pointer_intent(root, prepared, deployment_id, run_dir)
    successor = base64.b64decode(intent["successor_bytes_base64"], validate=True)
    stages = terminal.get("stages", {})
    if (set(terminal) != {"binding", "deploy_directory", "effects", "rollback_deployment_id",
                         "schema", "site_head", "stages", "status"}
            or terminal.get("schema") != "glp43-current-deploy-receipt-v1"
            or terminal.get("status") != "PASS_PRODUCTION_TERMINAL"
            or terminal.get("binding") != binding
            or terminal.get("site_head") != pointer["site_head"]
            or terminal.get("rollback_deployment_id") != prepared["rollback_deployment_id"]
            or terminal.get("deploy_directory") != prepared["deploy_directory"]
            or terminal.get("effects") != {"pages_deployments": 1, "pages_rollbacks": 0,
                 "pointer_atomic_cas": 1, "worker_deploys": 0, "d1_mutations": 0,
                 "cron_mutations": 0, "email_sends": 0}
            or stages.get("pages_deploy_cli", {}).get("returncode") != 0
            or stages.get("pages_deploy_intent", {}).get("predecessor_id") != prepared["rollback_deployment_id"]
            or stages.get("pages_deploy_intent", {}).get("commit_hash") != pointer["site_head"]
            or stages.get("pages_deployment") != {"deployment_id": deployment_id, "commit_hash": pointer["site_head"]}
            or "rollback_intent" in stages
            or not stages.get("route_probes") or not stages.get("runtime_and_holds")
            or stages.get("pointer_intent") != intent
            or stages.get("pointer_cas", {}).get("sha256") != sha_bytes(successor)
            or stages.get("canonical_pointer_readback") != {"deployment_id": deployment_id,
                 "site_head": pointer["site_head"], "pointer_sha256": sha_bytes(successor)}
            or successor != CURRENT_BASE.read_bytes()):
        raise RuntimeError("current_content_base_native_terminal_binding_invalid")
    for path, expected_raw, expected_identity in ((original_path, receipt_raw, receipt_identity),
                                                (approval_path, approval_raw, approval_identity)):
        _path, after, identity = _read_bound_regular_file(str(path), "native_dependency")
        if after != expected_raw or identity != expected_identity:
            raise RuntimeError("current_content_base_native_dependency_changed")
    return {str(original_path): (sha_bytes(receipt_raw), receipt_identity),
            str(approval_path): (sha_bytes(approval_raw), approval_identity)}


def _registered_prepared_binding(pointer, root, prepared, raw, manifest_path):
    """Keep later authorized native affiliate releases consumable by F04/F06."""
    import glp_deploy_glp34_authority_v1 as producer
    gate_path = root / 'gate.json'
    if (gate_path.is_file() and sha_bytes(gate_path.read_bytes()) == prepared.get('build_receipt', {}).get('sha256')
            and json.loads(gate_path.read_bytes()).get('schema') == 'glp-affiliate-sync-release-result-v1'):
        previous = producer.CURRENT_ROLLBACK
        producer.CURRENT_ROLLBACK = prepared['rollback_deployment_id']
        try:
            return _registered_prepared_binding_impl(pointer, root, prepared, raw, manifest_path)
        finally:
            producer.CURRENT_ROLLBACK = previous
    return _registered_prepared_binding_impl(pointer, root, prepared, raw, manifest_path)


def _registered_prepared_binding_impl(
    pointer: dict[str, Any], root: Path, prepared: dict[str, Any], raw: bytes, manifest_path: Path,
) -> dict[str, Any]:
    """Consume the registered producer's read-only preparation and publication validators."""
    # The carrier's guarded __main__ is not entered on import: no prepare,
    # publish, provider request, or publisher write is performed here.
    import glp_deploy_glp34_authority_v1 as producer

    native_keys = {
        "schema", "prepared_dir", "site_head", "site_repository", "project", "branch",
        "account_id", "public_tree_sha256", "package_manifest_sha256", "build_receipt",
        "source", "compiled", "toolchain", "rollback_deployment_id", "predecessor_pointer",
        "release", "deploy_directory", "holds", "route_specs", "provider_reads",
        "provider_mutations", "release_authorized",
    }
    if (set(prepared) != native_keys
            or set(prepared.get("source", {})) != {
                "authority_files", "built_source_sha256", "generated_diff_paths",
                "generated_diff_sha256", "git_tree", "go_embedded_sha256",
                "go_internal_fallback", "go_manifest_sha256", "runtime", "tracked_files",
                "wrangler_config_sha256"}
            or set(prepared.get("compiled", {})) != {
                "argv", "pages_random_comment_replacements", "raw_worker_sha256",
                "returncode", "routes", "stderr_sha256", "stdout_sha256", "worker_sha256"}):
        raise RuntimeError("current_content_base_prepared_native_fields_invalid")
    try:
        if producer.current_prepared(root, sha_bytes(raw)) != prepared:
            raise RuntimeError("current_content_base_prepared_native_mismatch")
        package_path, gate_path = root / "PACKAGE-MANIFEST.json", root / "gate.json"
        package = json.loads(_read_bound_regular_file(str(package_path), "package")[1])
        gate = json.loads(_read_bound_regular_file(str(gate_path), "gate")[1])
        rows = producer.package_tree(root / "release/public")[1]
        gate_binding = producer.package_build_receipt(argparse.Namespace(
            build_receipt=prepared["build_receipt"]["path"], expected_build_receipt_sha256=prepared["build_receipt"]["sha256"],
            site_head=prepared["site_head"], expected_tree_sha256=prepared["public_tree_sha256"]), rows)
        source = prepared["source"]
        if (prepared["build_receipt"] != gate_binding
                or package.get("schema") != "glp34-deploy-package-v1"
                or package.get("site_head") != pointer["site_head"]
                or package.get("site_repository") != prepared["site_repository"]
                or package.get("account_id") != prepared["account_id"]
                or package.get("project") != prepared["project"]
                or package.get("rollback_branch") != prepared["branch"]
                or package.get("rollback_deployment_id") != prepared["rollback_deployment_id"]
                or package.get("public_tree_sha256") != prepared["public_tree_sha256"]
                or package.get("files") != rows or package.get("build_receipt") != gate_binding
                or gate.get("built_source_sha256") != source["built_source_sha256"]
                or gate.get("generated_diff_sha256") != source["generated_diff_sha256"]
                or gate.get("generated_diff_paths") != source["generated_diff_paths"]
                or gate.get("authority_files") != source["authority_files"]
                or prepared["holds"] != next((step["evidence"]["raw_held"] for step in gate["steps"]
                    if step["id"] == "affiliate-census" and step["status"] == "PASS"), None)
                or prepared["release_authorized"] is not False
                or prepared["provider_mutations"] != 0 or prepared["provider_reads"] != 3):
            raise RuntimeError("current_content_base_prepared_producer_binding_invalid")
        for name, digest in source["tracked_files"]:
            if producer.sha_file(root / "release" / name) != digest:
                raise RuntimeError("current_content_base_prepared_source_drift")
        for row in source["authority_files"]:
            if producer.sha_file(root / "release" / row["file"]) != row["sha256"]:
                raise RuntimeError("current_content_base_prepared_authority_drift")
        for name, closure in source["runtime"].items():
            if producer.file_closure(root / "release" / name) != closure:
                raise RuntimeError("current_content_base_prepared_runtime_drift")
        run_dir = manifest_path.parent
        original_path = run_dir / "CURRENT-RECEIPT.json"
        original = json.loads(_read_bound_regular_file(str(original_path), "current_deploy")[1])
        if (original.get("schema") == "glp43-current-deploy-receipt-v1"
                and original.get("status") == "PASS_PRODUCTION_TERMINAL"):
            bound = _native_current_deploy_binding(pointer, root, prepared, raw, run_dir, producer)
            dependencies = (package_path, gate_path, Path(prepared["build_receipt"]["path"]))
            bound.update({str(path): (sha_bytes(data), identity)
                for path in dependencies
                for _path, data, identity in [_read_bound_regular_file(str(path), "prepared_dependency")]})
            return {"files": bound,
                "source_sha256": sha_bytes(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()),
                "compiled_sha256": sha_bytes(json.dumps(prepared["compiled"], sort_keys=True, separators=(",", ":")).encode()),
                "release": prepared["release"], "deploy": prepared["deploy_directory"]}
        deployment_id = pointer["deployment_id"]
        prefix = "CURRENT-PROMOTION-" + deployment_id
        root_path = run_dir / (prefix + ".json")
        first_path = run_dir / (prefix + "-SUCCESSOR-" + producer.CURRENT_PROMOTION_ROOT_SHA256 + ".json")
        second_path = run_dir / (prefix + "-SUCCESSOR-" + producer.CURRENT_PROMOTION_ROLLED_BACK_SUCCESSOR_SHA256 + ".json")
        terminal_path = run_dir / (prefix + "-SUCCESSOR-" + producer.CURRENT_PROMOTION_POST_IDENTITY_TERMINAL_SHA256 + ".json")
        terminal = json.loads(_read_bound_regular_file(str(terminal_path), "promotion")[1])
        if (set(package) != {
                "account_id", "authority_created", "build_receipt", "file_count", "files",
                "github_reads", "mode", "project", "provider_mutations", "provider_reads",
                "public_root", "public_tree_sha256", "release_authorized", "rollback_branch",
                "rollback_checked_at_utc", "rollback_deployment_id", "rollback_environment",
                "schema", "scope", "signature_created", "site_head", "site_head_binding",
                "site_repository", "tree_hash_algorithm"}
                or set(terminal) != {"schema", "status", "binding", "effects", "original_receipt", "site_head", "stages"}):
            raise RuntimeError("current_content_base_registered_fields_invalid")
        binding = terminal["binding"]
        args = argparse.Namespace(
            authorize_existing_promotion=True, authorize_renewed_promotion=True,
            authorize_rolled_back_successor_renewal=True, authorize_post_identity_promotion=True,
            expected_prepared_manifest_sha256=sha_bytes(raw),
            expected_approval_sha256=binding["approval_sha256"],
            expected_rolled_back_receipt_sha256=binding["original_receipt_sha256"],
            expected_terminal_promotion_sha256=producer.CURRENT_PROMOTION_POST_IDENTITY_TERMINAL_SHA256,
            candidate_deployment_id=deployment_id, site_head=pointer["site_head"],
        )
        registered = producer.current_promotion_post_identity_source(args,
            producer.current_promotion_source(args, prepared, original_path),
            original_path, root_path, first_path, second_path)
        intent = producer.current_pointer_intent(root, prepared, deployment_id, run_dir)
        successor = base64.b64decode(intent["successor_bytes_base64"], validate=True)
        stages = terminal["stages"]
        if (terminal.get("schema") != "glp43-current-promotion-receipt-v1"
                or terminal.get("status") != "PASS_PRODUCTION_TERMINAL"
                or terminal.get("original_receipt") != str(original_path)
                or terminal.get("site_head") != pointer["site_head"] or binding != registered
                or stages.get("pointer_intent") != intent
                or stages.get("pointer_cas", {}).get("sha256") != sha_bytes(successor)
                or stages.get("canonical_pointer_readback") != {
                    "deployment_id": deployment_id, "site_head": pointer["site_head"],
                    "pointer_sha256": sha_bytes(successor)}
                or successor != CURRENT_BASE.read_bytes()
                or terminal.get("effects", {}).get("pointer_atomic_cas") != 1):
            raise RuntimeError("current_content_base_promotion_binding_invalid")
        dependencies = (package_path, gate_path, Path(prepared["build_receipt"]["path"]),
                        original_path, root_path, first_path, second_path, terminal_path)
        bound = {str(path): (sha_bytes(data), identity)
                 for path in dependencies
                 for _path, data, identity in [_read_bound_regular_file(str(path), "prepared_dependency")]}
    except (producer.GateError, KeyError, TypeError, ValueError, OSError, IndexError) as exc:
        raise RuntimeError(f"current_content_base_registered_preparation_invalid:{exc}") from exc
    return {"files": bound,
            "source_sha256": sha_bytes(json.dumps(source, sort_keys=True, separators=(",", ":")).encode()),
            "compiled_sha256": sha_bytes(json.dumps(prepared["compiled"], sort_keys=True, separators=(",", ":")).encode()),
            "release": prepared["release"], "deploy": prepared["deploy_directory"]}


def _validate_base_evidence(pointer: dict[str, Any], release: Path) -> dict[str, Any]:
    manifest_bound = _checked_base_evidence(
        pointer, "manifest", "01-deployment-manifest.json"
    )
    receipt_bound = _checked_base_evidence(
        pointer, "final_receipt", "10-final-publication-receipt.json"
    )
    manifest_path = manifest_bound["path"]
    receipt_path = receipt_bound["path"]
    if manifest_path.parent != receipt_path.parent:
        raise RuntimeError("current_content_base_evidence_directory_mismatch")

    manifest = _load_base_document(manifest_bound["bytes"], "manifest")
    _recheck_base_evidence(manifest_bound, "manifest")
    prepared_format = is_prepared_pointer(pointer)
    if prepared_format:
        _require_outer_keys_v1(
            manifest,
            frozenset({"schema", "release_root", "release_rows", "release_tree_sha256",
                       "site_head", "deploy_directory"}),
            frozenset({"schema", "release_root", "release_rows", "release_tree_sha256",
                       "site_head", "deploy_directory"}),
            "prepared_manifest_fields_invalid",
        )
        if manifest["site_head"] != pointer["site_head"]:
            raise RuntimeError("prepared_manifest_site_head_mismatch")
    else:
        _require_outer_keys_v1(
            manifest,
            _MANIFEST_REQUIRED_KEYS_V1,
            _MANIFEST_ALLOWED_KEYS_V1,
            "legacy_manifest_fields_invalid",
        )
    if manifest.get("schema") != "glp-opus-release-deployment-manifest-v1":
        raise RuntimeError("legacy_manifest_schema_invalid")
    if manifest.get("release_root") != str(release):
        raise RuntimeError("legacy_manifest_release_root_mismatch")
    closure_schema, manifest_rows = _normalized_manifest_rows(manifest.get("release_rows"))

    actual_current_rows = _complete_current_rows(release)
    public_current_rows = _complete_current_rows(release / "public")
    counts = _computed_base_counts(actual_current_rows, public_current_rows)
    _validate_count_fields(manifest, counts, "legacy_manifest")
    _validate_count_fields(pointer, counts, "current_content_base_pointer")
    if closure_schema == "legacy-path-size-sha256-v1":
        actual_rows = _legacy_rows(actual_current_rows)
        release_tree = _legacy_closure(actual_rows)
        public_tree = _legacy_closure(_legacy_rows(public_current_rows))
        if actual_rows != manifest_rows:
            raise RuntimeError("legacy_manifest_release_rows_mismatch")
        if manifest.get("release_tree_sha256") != release_tree:
            raise RuntimeError("legacy_manifest_release_tree_mismatch")
    else:
        actual_rows = actual_current_rows
        release_tree = BUILDER.closure(actual_rows)
        public_tree = BUILDER.closure(public_current_rows)
        if actual_rows != manifest_rows or manifest.get("release_tree_sha256") != release_tree:
            raise RuntimeError("current_content_base_closure_mismatch")

    prepared_binding = None
    if prepared_format:
        if closure_schema != "legacy-path-size-sha256-v1":
            raise RuntimeError("prepared_manifest_row_schema_invalid")
        prepared_binding = _prepared_base_links(pointer, release, manifest_path, receipt_path, manifest, actual_rows)

    pointer_release_tree = pointer.get("release_tree_sha256")
    pointer_public_tree = pointer.get("public_tree_sha256")
    if not _valid_sha256(pointer_release_tree) or not _valid_sha256(pointer_public_tree):
        raise RuntimeError("current_content_base_closure_invalid")
    if pointer_release_tree != release_tree or pointer_public_tree != public_tree:
        raise RuntimeError("current_content_base_closure_mismatch")

    receipt = _load_base_document(receipt_bound["bytes"], "final_receipt")
    _recheck_base_evidence(receipt_bound, "final_receipt")
    if prepared_format:
        _require_outer_keys_v1(
            receipt,
            frozenset({"schema", "status", "deployment", "evidence_sha256",
                       "immutable_closure", "site_head"}),
            frozenset({"schema", "status", "deployment", "evidence_sha256",
                       "immutable_closure", "site_head"}),
            "prepared_final_receipt_fields_invalid",
        )
        if receipt["site_head"] != pointer["site_head"]:
            raise RuntimeError("prepared_final_receipt_site_head_mismatch")
    else:
        _require_outer_keys_v1(
            receipt,
            _FINAL_RECEIPT_REQUIRED_KEYS_V1,
            _FINAL_RECEIPT_ALLOWED_KEYS_V1,
            "legacy_final_receipt_fields_invalid",
        )
    if receipt.get("schema") != "glp-opus-production-terminal-receipt-v1":
        raise RuntimeError("legacy_final_receipt_schema_invalid")
    if receipt.get("status") != "PASS_PRODUCTION_TERMINAL":
        raise RuntimeError("legacy_final_receipt_status_invalid")
    deployment = receipt.get("deployment")
    if not isinstance(deployment, dict) or set(deployment) != {"deployment_id"}:
        raise RuntimeError("legacy_final_receipt_deployment_fields_invalid")
    if deployment.get("deployment_id") != pointer.get("deployment_id"):
        raise RuntimeError("legacy_final_receipt_deployment_mismatch")
    if receipt.get("evidence_sha256") != {manifest_path.name: manifest_bound["sha256"]}:
        raise RuntimeError("legacy_final_receipt_manifest_evidence_mismatch")
    immutable_closure = receipt.get("immutable_closure")
    if not isinstance(immutable_closure, dict) or set(immutable_closure) != {
        "release_tree_sha256", "public_tree_sha256"
    }:
        raise RuntimeError("legacy_final_receipt_closure_fields_invalid")
    if immutable_closure != {
        "release_tree_sha256": release_tree,
        "public_tree_sha256": public_tree,
    }:
        raise RuntimeError("legacy_final_receipt_closure_mismatch")
    _validate_count_fields(receipt, counts, "legacy_final_receipt")
    _recheck_base_evidence(manifest_bound, "manifest")
    _recheck_base_evidence(receipt_bound, "final_receipt")
    return {
        "release_tree_sha256": release_tree,
        "public_tree_sha256": public_tree,
        "closure_schema": closure_schema,
        "row_count": len(actual_rows),
        "manifest": {"path": manifest_path, "sha256": manifest_bound["sha256"]},
        "final_receipt": {"path": receipt_path, "sha256": receipt_bound["sha256"]},
        "prepared_binding": prepared_binding,
    }


def validate_base(pointer: dict[str, Any], token: str) -> dict[str, Any]:
    if is_prepared_pointer(pointer):
        validate_pointer_shape(pointer, require_release_dirs=True)
    _require_outer_keys_v1(
        pointer,
        _POINTER_REQUIRED_KEYS_V1,
        _POINTER_ALLOWED_KEYS_V1 | (frozenset({"site_head", "deploy_directory_sha256"})
                                    if is_prepared_pointer(pointer) else frozenset()),
        "current_content_base_fields_invalid",
    )
    if pointer.get("status") != "PASS":
        raise RuntimeError("current_content_base_not_pass")
    if pointer.get("schema") != "glp-opus-current-content-base-v1":
        raise RuntimeError("current_content_base_schema_invalid")
    deployment_id = pointer.get("deployment_id")
    if not isinstance(deployment_id, str) or re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", deployment_id
    ) is None:
        raise RuntimeError("current_content_base_deployment_id_invalid")
    release_root = pointer.get("release_root")
    if not isinstance(release_root, str) or not release_root or "\x00" in release_root:
        raise RuntimeError("current_content_base_release_missing")
    release = Path(release_root)
    try:
        resolved_release = release.resolve(strict=True)
        release_info = os.lstat(release)
    except (OSError, RuntimeError):
        raise RuntimeError("current_content_base_release_missing") from None
    if (
        not release.is_absolute()
        or str(resolved_release) != release_root
        or release.is_symlink()
        or not stat.S_ISDIR(release_info.st_mode)
        or not (release / "public").is_dir()
    ):
        raise RuntimeError("current_content_base_release_missing")

    # The predecessor identity is authority, not commentary: both UUID and
    # release root are mandatory, exact, and validated before evidence opens or
    # provider reads.  The shared helper also keeps every pointer writer and
    # consumer on the same versioned outer-key contract.
    validate_pointer_shape(pointer, require_release_dirs=True)

    evidence = _validate_base_evidence(pointer, release)
    release_tree = evidence["release_tree_sha256"]
    public_tree = evidence["public_tree_sha256"]
    project = project_detail(token)
    if project.get("name") != PROJECT:
        raise RuntimeError("cloudflare_project_identity_mismatch")
    if d1_ids(project) != [RUNTIME_D1]:
        raise RuntimeError("cloudflare_project_d1_binding_invalid")
    head = production_head(token)
    if head.get("id") != deployment_id:
        raise RuntimeError(f"current_content_base_cloudflare_drift:{deployment_id}:{head.get('id')}")
    detail = deployment_detail(token, head["id"])
    if d1_ids(detail) != [RUNTIME_D1]:
        raise RuntimeError("current_content_base_d1_binding_invalid")
    return {
        "release": release,
        "release_tree_sha256": release_tree,
        "public_tree_sha256": public_tree,
        "head": head,
        "detail": detail,
        "project": project,
        "closure_schema": evidence["closure_schema"],
        "row_count": evidence["row_count"],
        "manifest": evidence["manifest"],
        "final_receipt": evidence["final_receipt"],
    }


def make_writable(root: Path) -> dict[str, Any]:
    """Make private directories writable and detach declared writable outputs."""
    no_symlinks(root)
    writable_directories = make_directories_writable(root)
    detached = [detach_for_write(root / relative) for relative in WEEKLY_MUTABLE_FILES]
    # Older authenticated predecessors may lack newer generated families.
    # Absent outputs will be created privately by the writer; never invent inputs.
    generated_paths = set(GENERATED_MUTABLE_FILES)
    for relative in GENERATED_MUTABLE_SUBTREES:
        subtree = root / relative
        if subtree.exists():
            if not subtree.is_dir():
                raise RuntimeError(f"generated_subtree_not_directory:{relative}")
            generated_paths.update(path.relative_to(root) for path in subtree.rglob("*")
                                   if path.is_file())
    generated_detached = [detach_for_write(root / relative)
                          for relative in sorted(generated_paths)
                          if (root / relative).exists()]
    return {
        "writable_directories": writable_directories,
        "detached_files": detached,
        "generated_detached_files": generated_detached,
    }


def unlink_generated_trees_for_rebuild(root: Path) -> dict[str, Any]:
    """Reset generated output and retain only detached staging prerequisites."""
    return prepare_clean_build_trees(root, GENERATED_TREES, CLEAN_BUILD_INPUTS)


def prepare_weekly_candidate(candidate: Path) -> dict[str, Any]:
    writable = make_writable(candidate)
    # Keep each original input inode alive through reset. On repeat builds
    # some inputs are already private; unlink/recreate can otherwise reuse a
    # freed inode and trip the shared materializer's predecessor-identity check.
    # This lease changes no bytes/modes and is removed before sealing.
    import tempfile
    with tempfile.TemporaryDirectory(prefix=".price-input-lease-", dir=candidate.parent) as directory:
        lease = Path(directory)
        for index, relative in enumerate(CLEAN_BUILD_INPUTS):
            source = candidate / relative
            if source.is_symlink() or not source.is_file():
                raise RuntimeError(f"price_input_lease_not_regular:{relative}")
            # Root-owned sealed inputs cannot be hardlinked by an unprivileged
            # process under protected_hardlinks. Lease an owned private copy.
            detach_for_write(source)
            os.link(source, lease / str(index), follow_symlinks=False)
        clean_build = unlink_generated_trees_for_rebuild(candidate)
    return {"writable": writable, **clean_build}


def copy_weekly_candidate(source: Path, candidate: Path) -> dict[str, Any]:
    if os.path.lexists(candidate):
        raise RuntimeError(f"candidate_already_exists:{candidate}")
    try:
        predecessor_before = tree_content_closure(source)
        materialization = copytree_linked(source, candidate)
        no_symlinks(candidate)
        return {
            "materialization": materialization,
            "predecessor_before": predecessor_before,
            "preparation": prepare_weekly_candidate(candidate),
        }
    except Exception:
        remove_materialized_tree(candidate)
        raise


def seal_release(
    candidate: Path,
    release: Path,
    predecessor: Path,
    *,
    owner: tuple[int, int] = (0, 0),
    use_sudo: bool = True,
) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
    if release.exists():
        raise RuntimeError(f"release_already_exists:{release}")
    temporary = release.with_name(f".{release.name}.tmp-{os.getpid()}")
    if temporary.exists():
        shutil.rmtree(temporary)
    candidate_seal = seal_tree_selective(
        candidate,
        [predecessor],
        owner=owner,
        use_sudo=use_sudo,
    )
    materialization = copytree_linked(candidate, temporary)
    no_symlinks(temporary)
    release_seal = seal_tree_selective(
        temporary,
        [candidate],
        owner=owner,
        use_sudo=use_sudo,
    )
    os.replace(temporary, release)
    release_tree, release_rows = closure(release)
    public_tree, _ = closure(release / "public")
    for path in [release, *release.rglob("*")]:
        info = os.lstat(path)
        if info.st_uid != owner[0] or info.st_gid != owner[1] or info.st_mode & 0o222:
            raise RuntimeError(f"immutable_release_mode_invalid:{path}")
    return release_tree, public_tree, release_rows, {
        "candidate_seal": candidate_seal,
        "candidate_to_release": materialization,
        "release_seal": release_seal,
    }


def deploy_pages(receipt: dict[str, Any], release: Path, public_tree: str, token: str, account_id: str) -> tuple[str, dict[str, Any], str]:
    carrier = Path(tempfile.mkdtemp(prefix="glp-price-index-deploy-"))
    try:
        shutil.copytree(release / "functions", carrier / "functions")
        shutil.copytree(release / "src", carrier / "src")
        # 2026-08-27: Pages Functions may import build-generated JSON from data/
        # (review-capture.mjs -> phase3-pharmacy-render-input.json), so the
        # scratch carrier stages data/ alongside src/.
        shutil.copytree(release / "data", carrier / "data")
        env = os.environ.copy()
        env.update({"CLOUDFLARE_API_TOKEN": token, "CLOUDFLARE_ACCOUNT_ID": account_id, "WRANGLER_SEND_METRICS": "false"})
        run_command(receipt, ["npx", f"wrangler@{WRANGLER}", "pages", "functions", "build", "functions", "--outdir", ".wrangler/tmp/pages-bundle"], cwd=carrier, timeout=600, env=env)
        deploy = run_command(receipt, [
            "npx", f"wrangler@{WRANGLER}", "pages", "deploy", str(release / "public"),
            "--project-name", PROJECT, "--branch", "main", "--commit-dirty=true",
            "--commit-message", f"GLP-1 Price Index weekly observation {utc_now().date().isoformat()}",
            "--commit-hash", public_tree[:40],
        ], cwd=carrier, timeout=1200, env=env)
        match = re.search(r"https://([0-9a-f-]+)\.glp-clinic-scout\.pages\.dev", deploy.stdout + deploy.stderr)
        if not match:
            raise RuntimeError("cloudflare_deployment_id_parse_failed")
        pages_hostname_id = match.group(1)
        detail = deployment_detail(token, pages_hostname_id)
        deployment_id = canonical_deployment_id(detail, pages_hostname_id)
        if detail.get("environment") != "production" or detail.get("latest_stage", {}).get("status") != "success":
            raise RuntimeError("cloudflare_new_deployment_not_success")
        if d1_ids(detail) != [RUNTIME_D1]:
            raise RuntimeError("cloudflare_new_deployment_d1_invalid")
        return deployment_id, detail, safe_tail(deploy.stdout + deploy.stderr)
    finally:
        shutil.rmtree(carrier, ignore_errors=True)


def fetch_until(url: str, predicate, *, attempts: int = 12, delay_seconds: int = 5) -> tuple[bytes, dict[str, Any]]:
    last: dict[str, Any] = {}
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, headers={"Cache-Control": "no-cache", "Pragma": "no-cache", "User-Agent": "ClinicScoutPriceIndexValidator/1.0"}, timeout=45)
            body = response.content
            last = {"attempt": attempt, "http_status": response.status_code, "sha256": sha_bytes(body), "bytes": len(body), "final_url": response.url}
            if response.status_code == 200 and predicate(body):
                return body, last
        except requests.RequestException as exc:
            last = {"attempt": attempt, "error": type(exc).__name__}
        if attempt < attempts:
            time.sleep(delay_seconds)
    raise RuntimeError(f"live_readback_failed:{url}:{last}")


def validate_price_index_outputs(root: Path, *, expected_observed_on: str | None = None) -> dict[str, Any]:
    """Bind retained HTML and both downloads to the same validated observation.

    The source renderer remains the price/evidence authority. This additional
    boundary catches stale predecessor downloads or mixed output generations
    after public-scope restoration, before any deployment and at live readback.
    """
    manifest = json.loads((root / "artifacts/ops/price-index-manifest.json").read_text())
    config = json.loads((root / "data/price-tracker.json").read_text())["price_index"]
    observations = config["observations"]
    latest = observations[-1]
    json_path = root / "public/glp-1-price-index/data/latest.json"
    csv_path = root / "public/glp-1-price-index/data/latest.csv"
    distribution = json.loads(json_path.read_text())
    html = (root / "public/glp-1-price-index/index.html").read_text()
    observed_on = latest["observed_on"]
    coverage = f"{observations[0]['observed_on']}/{observed_on}"
    if (
        manifest.get("status") != "PASS" or manifest.get("route") != ROUTE
        or distribution.get("schema") != "glp-clinic-scout-price-index-distribution-v1"
        or distribution.get("url") != PUBLIC_URL
        or (expected_observed_on is not None and observed_on != expected_observed_on)
        or manifest.get("latest_observed_on") != observed_on
        or distribution.get("observed_on") != observed_on
        or manifest.get("data_as_of") != latest["data_as_of"]
        or distribution.get("data_as_of") != latest["data_as_of"]
        or distribution.get("temporal_coverage") != coverage
        or manifest.get("history_started_on") != observations[0]["observed_on"]
        or manifest.get("observation_count") != len(observations)
        or manifest.get("source_hashes") != latest["source_files"]
        or manifest.get("record_set_sha256") != latest["record_set_sha256"]
        or distribution.get("metrics") != manifest.get("metrics")
    ):
        raise RuntimeError("price_index_distribution_observation_mismatch")
    columns = ["record_id", "provider_id", "provider", "medication", "formulation",
               "advertised_monthly_price_usd", "billing_basis", "price_phase",
               "selection_basis", "first_party_source_url", "captured_on"]
    rows = distribution.get("rows")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) or set(row) != set(columns) for row in rows):
        raise RuntimeError("price_index_distribution_rows_invalid")
    ids = [row["record_id"] for row in rows]
    projected = [{"id": row["record_id"], "amount_usd": row["advertised_monthly_price_usd"],
                  **{key: row[key] for key in ("provider_id", "medication", "formulation", "billing_basis", "price_phase", "selection_basis")}}
                 for row in rows]
    if (len(set(ids)) != len(ids) or manifest.get("record_count") != len(rows)
        or distribution.get("row_count") != len(rows)
        or sorted(projected, key=lambda row: row["id"]) != sorted(latest["records"], key=lambda row: row["id"])
        or sorted(config["records"], key=lambda row: row["id"]) != sorted(latest["records"], key=lambda row: row["id"])):
        raise RuntimeError("price_index_distribution_record_mismatch")
    csv_reader = csv.DictReader(io.StringIO(csv_path.read_text(), newline=""), strict=True)
    csv_rows = list(csv_reader)
    expected_csv_rows = [{key: str(int(row[key])) if isinstance(row[key], float) and row[key].is_integer() else str(row[key])
                          for key in columns} for row in rows]
    if csv_reader.fieldnames != columns or csv_rows != expected_csv_rows:
        raise RuntimeError("price_index_distribution_csv_mismatch")
    datasets = []
    for script in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        value = json.loads(script)
        datasets.extend(node for node in value.get("@graph", [value]) if node.get("@type") == "Dataset")
    expected_downloads = {PUBLIC_URL + "data/latest.json", PUBLIC_URL + "data/latest.csv"}
    if (len(datasets) != 1 or datasets[0].get("dateModified") != observed_on
        or datasets[0].get("temporalCoverage") != coverage
        or {item.get("contentUrl") for item in datasets[0].get("distribution", [])} != expected_downloads
        or sorted(re.findall(r'<tr data-index-record-id="([^"]+)">', html)) != sorted(ids)
        or manifest.get("html_sha256") != sha_file(root / "public/glp-1-price-index/index.html")):
        raise RuntimeError("price_index_distribution_html_mismatch")
    return {"status": "PASS_SAME_OBSERVATION", "observed_on": observed_on,
            "data_as_of": latest["data_as_of"], "record_count": len(rows),
            "json_sha256": sha_file(json_path), "csv_sha256": sha_file(csv_path)}


def readback(release: Path, detail: dict[str, Any]) -> dict[str, Any]:
    dataset_validation = validate_price_index_outputs(release)
    local_page = (release / "public/glp-1-price-index/index.html").read_bytes()
    local_sitemap = (release / "public/sitemap.xml").read_bytes()
    local_page_sha = sha_bytes(local_page)
    local_sitemap_sha = sha_bytes(local_sitemap)

    def normalize_cf_email(body: bytes) -> bytes:
        """Cloudflare email-protection injects a per-request nonce (2026-08-27:
        the redesigned shared footer put support@ on this page, so raw byte
        equality became impossible — the exact hermes-01 cfemail gotcha). Both
        the local file and the served body collapse to one canonical form:
        obfuscated spans/hrefs and the raw address become a fixed token, and the
        injected decoder script disappears. Everything else stays byte-strict."""
        body = re.sub(rb'<script[^>]*email-decode\.min\.js[^>]*></script>', b"", body)
        body = re.sub(rb'/cdn-cgi/l/email-protection#[0-9a-f]+', b"EMAIL", body)
        body = re.sub(rb'<a[^>]*href="/cdn-cgi/l/email-protection"[^>]*>.*?</a>', b"EMAIL", body)
        body = re.sub(rb'<span[^>]*__cf_email__[^>]*data-cfemail="[0-9a-f]+"[^>]*>.*?</span>', b"EMAIL", body)
        body = re.sub(rb'mailto:[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', b"EMAIL", body)
        body = re.sub(rb'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', b"EMAIL", body)
        return body

    local_page_norm_sha = sha_bytes(normalize_cf_email(local_page))
    immutable_base = str(detail.get("url", "")).rstrip("/")
    if not immutable_base.startswith("https://"):
        raise RuntimeError("immutable_deployment_url_missing")
    page_predicate = lambda body: sha_bytes(normalize_cf_email(body)) == local_page_norm_sha and b'href="https://glpclinicscout.com/glp-1-price-index/"' in body and body.count(b"data-index-record-id=") == dataset_validation["record_count"]
    sitemap_predicate = lambda body: sha_bytes(body) == local_sitemap_sha and body.count(b"https://glpclinicscout.com/glp-1-price-index/") == 1
    immutable_page, immutable_fact = fetch_until(f"{immutable_base}{ROUTE}", page_predicate, attempts=3, delay_seconds=2)
    public_page, public_fact = fetch_until(PUBLIC_URL, page_predicate)
    public_sitemap, sitemap_fact = fetch_until(SITEMAP_URL, sitemap_predicate)
    if sha_bytes(normalize_cf_email(immutable_page)) != sha_bytes(normalize_cf_email(public_page)) or sha_bytes(public_sitemap) != local_sitemap_sha:
        raise RuntimeError("live_readback_byte_mismatch")
    downloads = {}
    for name in ("latest.json", "latest.csv"):
        expected = (release / "public/glp-1-price-index/data" / name).read_bytes()
        predicate = lambda body, expected=expected: body == expected
        immutable_url = f"{immutable_base}{ROUTE}data/{name}"
        public_url = f"{PUBLIC_URL}data/{name}"
        _, immutable = fetch_until(immutable_url, predicate, attempts=3, delay_seconds=2)
        _, production = fetch_until(public_url, predicate)
        downloads[name] = {"local_sha256": sha_bytes(expected), "immutable_url": immutable_url,
                           "url": public_url, "immutable": immutable, "production": production}
    return {
        "dataset": {"validation": dataset_validation, "downloads": downloads},
        "route": ROUTE,
        "live_url": PUBLIC_URL,
        "immutable_url": f"{immutable_base}{ROUTE}",
        "page": {"local_sha256": local_page_sha, "immutable": immutable_fact, "production": public_fact},
        "sitemap": {"url": SITEMAP_URL, "local_sha256": local_sitemap_sha, "production": sitemap_fact, "route_occurrences": public_sitemap.count(b"https://glpclinicscout.com/glp-1-price-index/")},
        "canonical_occurrences": public_page.count(b'href="https://glpclinicscout.com/glp-1-price-index/"'),
        "record_rows": public_page.count(b"data-index-record-id="),
        "medical_reviewer_claim_count": len(re.findall(rb"MedicalReview|reviewedBy|Medically reviewed by", public_page, re.I)),
    }


def changed_paths(base_root: Path, release: Path) -> dict[str, list[str]]:
    def files(root: Path) -> dict[str, str]:
        rows = BUILDER.rows(root)
        return {row["path"]: row["sha256"] for row in rows}
    before, after = files(base_root), files(release)
    return {
        "added": sorted(set(after) - set(before)),
        "modified": sorted(path for path in set(before) & set(after) if before[path] != after[path]),
        "deleted": sorted(set(before) - set(after)),
    }


def validate_indexnow_config(release: Path) -> dict[str, Any]:
    path = release / "data/indexnow.json"
    if not path.is_file():
        raise RuntimeError("indexnow_config_missing")
    config = json.loads(path.read_text(encoding="utf-8"))
    key = str(config.get("key", ""))
    if config.get("schema") != "glp-indexnow-v1":
        raise RuntimeError("indexnow_config_schema_invalid")
    if config.get("host") != INDEXNOW_HOST:
        raise RuntimeError("indexnow_config_host_invalid")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,128}", key):
        raise RuntimeError("indexnow_config_key_invalid")
    expected_location = f"https://{INDEXNOW_HOST}/{key}.txt"
    if config.get("key_location") != expected_location:
        raise RuntimeError("indexnow_config_key_location_invalid")
    if config.get("endpoint") != INDEXNOW_ENDPOINT:
        raise RuntimeError("indexnow_config_endpoint_invalid")
    urls = config.get("notify_urls")
    if urls != [PUBLIC_URL]:
        raise RuntimeError("indexnow_notify_urls_invalid")
    key_file = release / "public" / f"{key}.txt"
    if not key_file.is_file() or key_file.read_text(encoding="utf-8") != f"{key}\n":
        raise RuntimeError("indexnow_local_key_file_invalid")
    return config


def submit_indexnow(release: Path, predecessor: Path | None = None) -> dict[str, Any]:
    """Announce this deploy to IndexNow.

    Historically this posted exactly ONE url — `/glp-1-price-index/` — so every
    newly deployed review page announced itself to nobody (DEPLOY-RUNBOOK "Known
    defects", task 37). It now posts the pinned url PLUS the content-hash changed
    set versus the predecessor release, filtered to sitemap-present, indexable,
    robots-allowed pages only, with retry x3. Unchanged urls are never
    resubmitted (IndexNow anti-spam guidance, SEO §5.1).

    Passing no predecessor degrades to the historical single-url behaviour rather
    than failing the deploy: the announcement is the last step of a chain that has
    already succeeded, and a diff we cannot compute must not strand a live deploy.
    That degradation is recorded in the receipt as `changed_set_reason`.
    """
    config = validate_indexnow_config(release)
    key = config["key"]
    key_bytes = f"{key}\n".encode("utf-8")
    _, key_readback = fetch_until(
        config["key_location"],
        lambda body: body == key_bytes,
        attempts=12,
        delay_seconds=5,
    )
    sys.path.insert(0, str(BUSINESS / "scripts"))
    import glp_indexnow_changed_set as changed_set_module  # type: ignore

    composed = changed_set_module.compose_notify_urls(
        list(config["notify_urls"]), release, predecessor)
    receipt = changed_set_module.submit(config, composed["urls"])
    run_id = stamp()
    http_status = receipt["posts"][0]["http_status"] if receipt["posts"] else None
    receipt.update({
        "key_file_readback": key_readback,
        "notify_urls": config["notify_urls"],
        "changed_set_reason": composed["reason"],
        "changed_counts": {k: len(v) for k, v in composed["changed"].items()},
        "deleted_routes_not_submitted": composed["changed"]["deleted"],
        "rejected_changed_routes": composed["rejected"],
        "predecessor_release": composed["predecessor"],
        "ledger_rows_written": changed_set_module.append_ledger(
            composed["urls"], release.name, run_id, http_status),
    })
    return receipt


def verify_functions_transition(before: str, after: str, expected_after: str | None) -> dict[str, Any]:
    if expected_after is not None:
        if len(expected_after) != 64 or any(ch not in "0123456789abcdef" for ch in expected_after):
            raise RuntimeError("expected_functions_tree_after_invalid")
        if after != expected_after:
            raise RuntimeError("expected_functions_tree_after_mismatch")
    if before != after and expected_after is None:
        raise RuntimeError("functions_closure_changed")
    return {
        "before_sha256": before,
        "after_sha256": after,
        "changed": before != after,
        "explicit_expected_after_sha256": expected_after,
        "status": "PASS_EXACT_DECLARED_SUCCESSOR" if before != after else "PASS_UNCHANGED",
    }


def price_index_build_env(predecessor: Path, run_dir: Path, observed_on: str) -> dict[str, str]:
    """Bind the existing runtime-only proof to the validated predecessor.

    No route declaration or blanket date refresh is granted. Material content
    drift still fails the existing postbuild proof.
    """
    rows = _complete_current_rows(predecessor)
    manifest = run_dir / "runtime-predecessor-files.json"
    write_json_0600(manifest, rows)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("GLP_LASTMOD_")}
    env.update({
        "GLP_PRICE_INDEX_REFRESH": "1",
        "GLP_RELEASE_DATE": observed_on,
        "GLP_LASTMOD_RUNTIME_PREDECESSOR_ROOT": str(predecessor),
        "GLP_LASTMOD_RUNTIME_PREDECESSOR_MANIFEST": str(manifest),
        "GLP_LASTMOD_RUNTIME_PREDECESSOR_MANIFEST_SHA256": sha_file(manifest),
    })
    return env


def run_chain(
    prepared_candidate: Path | None,
    release_name: str | None,
    expected_functions_tree_after: str | None = None,
    prepared_constructor_receipt: Path | None = None,
) -> tuple[int, dict[str, Any], Path]:
    run_id = stamp()
    run_dir = GROWTH_ROOT / "runs" / run_id
    receipt_path = run_dir / "chain-receipt.json"
    receipt: dict[str, Any] = {
        "schema": "glp-price-index-release-chain-v1",
        "status": "RUNNING",
        "run_id": run_id,
        "started_at_utc": iso_now(),
        "chain": ["candidate", "deploy", "remint", "sensor", "indexnow"],
        "route": ROUTE,
        "commands": [],
        "external_effects": {"cloudflare_deployments": 0, "current_content_base_remints": 0, "indexnow_pings": 0, "discord_messages": 0, "affiliate_mutations": 0, "outreach_sends": 0, "spend_usd": 0},
    }
    write_json_0600(receipt_path, receipt)
    release: Path | None = None
    try:
        token = credential("CLOUDFLARE_API_TOKEN", Path("/home/ubuntu/.config/cloudflare/api_token"))
        configured_account_id = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()
        if configured_account_id and configured_account_id != ACCOUNT:
            raise RuntimeError("cloudflare_account_identity_mismatch")
        account_id = ACCOUNT
        pointer = json.loads(CURRENT_BASE.read_text(encoding="utf-8"))
        base = validate_base(pointer, token)
        acceptance = load_accepted_failure_contract()
        _binding, binding_spec = acceptance.load_binding(pointer)
        receipt["accepted_failure_binding"] = binding_spec
        receipt["predecessor"] = {
            "deployment_id": pointer["deployment_id"],
            "release_root": str(base["release"]),
            "release_tree_sha256": base["release_tree_sha256"],
            "public_tree_sha256": base["public_tree_sha256"],
            "cloudflare_account_id": ACCOUNT,
            "cloudflare_project": base["project"].get("name"),
            "d1_database_ids": d1_ids(base["detail"]),
        }
        if prepared_candidate:
            candidate = prepared_candidate.resolve()
            if candidate.parent != CANDIDATES or not candidate.is_dir():
                raise RuntimeError("prepared_candidate_scope_invalid")
            no_symlinks(candidate)
            if prepared_constructor_receipt is None:
                raise RuntimeError("prepared_candidate_constructor_provenance_required")
            prepared_provenance = json.loads(
                prepared_constructor_receipt.resolve().read_text(encoding="utf-8")
            )
            validate_constructor_provenance(
                prepared_provenance,
                predecessor=base["release"],
                output=candidate,
                constructor=Path(__file__).resolve(),
            )
            candidate_cow = {
                "materialization": prepared_provenance["materializations"][0],
                "materializations": prepared_provenance["materializations"],
                "predecessor_before": {
                    "tree_sha256": prepared_provenance["predecessor"]["tree_sha256_before"]
                },
                "constructor_provenance": prepared_provenance,
                "preparation": prepare_weekly_candidate(candidate),
            }
            release_label = release_name or candidate.name
        else:
            if not (base["release"] / "src/price-index.mjs").is_file() or not (base["release"] / "scripts/update-price-index.mjs").is_file():
                raise RuntimeError("price_index_source_missing_from_current_content_base")
            release_label = release_name or f"glp-price-index-weekly-{run_id.lower()}"
            candidate = CANDIDATES / release_label
            candidate_cow = copy_weekly_candidate(base["release"], candidate)
        release = RELEASES / release_label
        observed_on = utc_now().date().isoformat()
        receipt["candidate"] = {
            "path": str(candidate),
            "source_predecessor_deployment_id": pointer["deployment_id"],
            "observed_on": observed_on,
            "cow": candidate_cow,
        }
        receipt["candidate"]["frozen_provenance_contract"] = (
            ensure_price_index_frozen_provenance_mode(candidate)
        )
        receipt["candidate"]["price_index_refresh_contract"] = ensure_price_index_refresh_mode(candidate)
        receipt["candidate"]["observer_migration"] = ensure_price_index_observer_mode(candidate)
        receipt["candidate"]["suite_repairs"] = apply_suite_repairs(candidate)
        run_command(receipt, ["node", "scripts/update-price-index.mjs", "--observed-on", observed_on], cwd=candidate, timeout=300)
        refresh_env = price_index_build_env(base["release"], run_dir, observed_on)
        run_command(receipt, ["npm", "run", "build"], cwd=candidate, timeout=1800, env=refresh_env)
        receipt["candidate"]["first_sitemap_merge"] = preserve_nonindex_sitemap(candidate, base["release"])
        receipt["candidate"]["first_public_scope"] = preserve_predecessor_public_outputs(candidate, base["release"])
        receipt["candidate"]["first_css_retention"] = retain_weekly_css_authoring(candidate, base["release"])
        receipt["candidate"]["first_dataset_validation"] = validate_price_index_outputs(candidate, expected_observed_on=observed_on)
        first_public_tree, _ = closure(candidate / "public")
        suite_job = {"base_release_root": str(base["release"]),
                     "base_manifest": pointer["manifest"], "report_dir": str(run_dir)}
        suite_env = dict(os.environ, GCS_RELEASE_STAGE="production-seal")
        _tested_tree, tested_rows = closure(candidate)
        suite = acceptance.run_suite(suite_job, candidate,
            runner=lambda command, **kwargs: subprocess.run(command, env=suite_env, **kwargs))
        _after_test_tree, after_test_rows = closure(candidate)
        constructor_snapshot = run_dir / "price-index-constructor.py"
        shutil.copyfile(Path(__file__).resolve(), constructor_snapshot)
        write_json_0600(run_dir / "price-index-suite-witness.json", {
            "schema": "glp-price-index-suite-witness-v1", "candidate_root": str(candidate),
            "expected_stage": "production-seal", "parent_binding": binding_spec,
            "before_rows": tested_rows, "after_rows": after_test_rows,
            "tested_public_tree_sha256": first_public_tree,
            "classification": acceptance.descriptor(run_dir / "00-accepted-failures-classification.json"),
            "constructor": acceptance.descriptor(constructor_snapshot),
            "constructor_sha256": sha_file(Path(__file__).resolve()),
        })
        receipt["commands"].append(suite)
        receipt["accepted_failures"] = suite["accepted_failures"]
        # Public restoration reintroduces predecessor-owned hardlinks. Every
        # full build needs its own clean, detached writer inputs, not only #1.
        receipt["candidate"]["second_preparation"] = prepare_weekly_candidate(candidate)
        run_command(receipt, ["npm", "run", "build"], cwd=candidate, timeout=1800, env=refresh_env)
        receipt["candidate"]["second_sitemap_merge"] = preserve_nonindex_sitemap(candidate, base["release"])
        receipt["candidate"]["second_public_scope"] = preserve_predecessor_public_outputs(candidate, base["release"])
        receipt["candidate"]["second_css_retention"] = retain_weekly_css_authoring(candidate, base["release"])
        receipt["candidate"]["dataset_validation"] = validate_price_index_outputs(candidate, expected_observed_on=observed_on)
        second_public_tree, _ = closure(candidate / "public")
        if first_public_tree != second_public_tree:
            raise RuntimeError("candidate_build_nondeterministic")
        manifest = json.loads((candidate / "artifacts/ops/price-index-manifest.json").read_text(encoding="utf-8"))
        if manifest.get("status") != "PASS" or manifest.get("route") != ROUTE:
            raise RuntimeError("price_index_manifest_invalid")
        sitemap = (candidate / "public/sitemap.xml").read_text(encoding="utf-8")
        if sitemap.count(f"https://glpclinicscout.com{ROUTE}") != 1:
            raise RuntimeError("price_index_sitemap_invalid")
        release_tree, public_tree, release_rows, release_cow = seal_release(
            candidate,
            release,
            base["release"],
        )
        functions_tree_before, _ = closure(base["release"] / "functions")
        functions_tree_after, _ = closure(release / "functions")
        functions_transition = verify_functions_transition(
            functions_tree_before,
            functions_tree_after,
            expected_functions_tree_after,
        )
        receipt["candidate"].update({"deterministic_public_tree_sha256": second_public_tree, "price_index_manifest": manifest})
        receipt["release"] = {
            "path": str(release), "release_tree_sha256": release_tree, "public_tree_sha256": public_tree,
            "row_count": len(release_rows), "owner": "root:root", "immutable": True,
            "changed_paths": changed_paths(base["release"], release), "cow": release_cow,
            "functions_transition": functions_transition,
        }
        if receipt["release"]["changed_paths"]["deleted"]:
            raise RuntimeError("release_deletes_predecessor_paths")
        declared_mutations = set(WEEKLY_MUTABLE_FILES)
        declared_mutations.update(GENERATED_MUTABLE_FILES)
        declared_mutations.update(suite_mutation_paths(candidate, receipt["candidate"]["suite_repairs"]))
        declared_mutations.update(Path("public") / path for path in PUBLIC_REFRESH_FILES)
        for generated_tree in (Path("artifacts/ops"), *GENERATED_MUTABLE_SUBTREES):
            for root in (base["release"], release):
                tree_root = root / generated_tree
                if tree_root.is_dir():
                    declared_mutations.update(
                        path.relative_to(root)
                        for path in tree_root.rglob("*")
                        if path.is_file() and not path.is_symlink()
                    )
        materializations = list(
            candidate_cow.get("materializations") or [candidate_cow["materialization"]]
        )
        materializations.append(release_cow["candidate_to_release"])
        release_provenance = constructor_provenance(
            constructor=Path(__file__).resolve(),
            predecessor=base["release"],
            output=release,
            mutation_paths=sorted(declared_mutations, key=lambda path: path.as_posix()),
            materializations=materializations,
            predecessor_before=candidate_cow["predecessor_before"],
        )
        receipt["release"]["constructor_provenance"] = release_provenance
        constructor_receipt = run_dir / "site-tree-constructor-receipt.json"
        write_json_0600(constructor_receipt, release_provenance)
        os.chmod(constructor_receipt, 0o444)
        receipt["release"]["constructor_receipt"] = {
            "path": str(constructor_receipt),
            "sha256": sha_file(constructor_receipt),
        }
        # Prove exact remint admission before the first provider write. This
        # carries the unchanged prior publication allowance, not new approval.
        allowance_manifest = run_dir / "price-index-allowance-manifest.json"
        write_json_0600(allowance_manifest, {
            "schema": "glp-opus-release-deployment-manifest-v1",
            "release_root": str(release), "release_tree_sha256": release_tree,
            "rollback_release_root": str(base["release"]),
            "public_tree_sha256": public_tree, "release_rows": release_rows,
        })
        receipt["release"]["publication_allowance"] = acceptance.install_price_index_binding(
            pointer, allowance_manifest, run_dir)
        pointer_now = json.loads(CURRENT_BASE.read_text(encoding="utf-8"))
        if pointer_now.get("deployment_id") != pointer.get("deployment_id") or production_head(token).get("id") != pointer.get("deployment_id"):
            raise RuntimeError("predecessor_moved_before_deploy")
        deployment_id, detail, deploy_output = deploy_pages(receipt, release, public_tree, token, account_id)
        receipt["external_effects"]["cloudflare_deployments"] = 1
        receipt["deployment"] = {
            "id": deployment_id, "deployment_id": deployment_id, "environment": detail.get("environment"),
            "status": detail.get("latest_stage", {}).get("status"), "url": detail.get("url"),
            "aliases": detail.get("aliases", []), "d1_database_ids": d1_ids(detail), "wrangler_output_tail": deploy_output,
        }
        receipt["status"] = "DEPLOYED_REMINT_PENDING"
        write_json_0600(receipt_path, receipt)
        if deployment_id == pointer["deployment_id"]:
            raise RuntimeError("new_deployment_reused_predecessor")
        wait_for_production_head(token, deployment_id)
        remint = run_command(receipt, [
            "sudo", "-n", "python3", str(REMINT),
            "--release", str(release), "--deployment-id", deployment_id,
            "--supersedes-deployment-id", pointer["deployment_id"],
            "--supersedes-release", pointer["release_root"],
        ], cwd=BUSINESS, timeout=600)
        receipt["external_effects"]["current_content_base_remints"] = 1
        remint_payload = json.loads(remint.stdout)
        pointer_after = json.loads(CURRENT_BASE.read_text(encoding="utf-8"))
        if pointer_after.get("deployment_id") != deployment_id or pointer_after.get("release_root") != str(release) or pointer_after.get("release_tree_sha256") != release_tree or pointer_after.get("public_tree_sha256") != public_tree:
            raise RuntimeError("content_base_remint_readback_invalid")
        receipt["remint"] = {"status": remint_payload.get("status"), "current_content_base": str(CURRENT_BASE), "readback": pointer_after}
        receipt["live_readback"] = readback(release, detail)
        receipt["status"] = "REMINTED_SENSOR_PENDING"
        write_json_0600(receipt_path, receipt)
        sys.path.insert(0, str(BUSINESS / "scripts"))
        import glp_deploy_sensor_hook as sensor_hook  # type: ignore
        sensor_hook.MACHINE_DIR = run_dir
        sensor = sensor_hook.attach_sensor_result(receipt_path, release, offline=False, post_alert=False)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["sensor"] = sensor
        if sensor.get("status") not in {"PASS", "PASS_PARTIAL"} or sensor.get("failure_count") != 0:
            raise RuntimeError(f"post_deploy_sensor_failed:{sensor.get('status')}:{sensor.get('failure_count')}")
        receipt["status"] = "SENSOR_PASSED_INDEXNOW_PENDING"
        write_json_0600(receipt_path, receipt)
        receipt["external_effects"]["indexnow_pings"] = 1
        receipt["indexnow"] = submit_indexnow(release, base["release"])
        receipt["status"] = "PASS"
        receipt["completed_at_utc"] = iso_now()
        receipt["blockers"] = []
        write_json_0600(receipt_path, receipt)
        return 0, receipt, receipt_path
    except Exception as exc:
        # Keep the in-memory command/effect evidence. Reloading the initial on-disk
        # checkpoint here used to erase every pre-deploy command when a later local
        # gate failed before the next checkpoint.
        effectful = receipt.get("external_effects", {}).get("cloudflare_deployments", 0) > 0
        receipt["status"] = "FAIL" if effectful else "BLOCKED"
        receipt["completed_at_utc"] = iso_now()
        receipt["blockers"] = [f"{type(exc).__name__}: {exc}"]
        if release:
            receipt.setdefault("release", {}).setdefault("path", str(release))
        write_json_0600(receipt_path, receipt)
        return 1, receipt, receipt_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepared-candidate", type=Path)
    parser.add_argument("--prepared-candidate-constructor-receipt", type=Path)
    parser.add_argument("--release-name")
    parser.add_argument(
        "--expected-functions-tree-after",
        help="Exact SHA-256 closure required for an explicitly declared successor functions/ transition",
    )
    args = parser.parse_args()
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "BLOCKED", "blocker": "price_index_release_chain_already_running"}, sort_keys=True))
            return 1
        code, receipt, receipt_path = run_chain(
            args.prepared_candidate,
            args.release_name,
            args.expected_functions_tree_after,
            args.prepared_candidate_constructor_receipt,
        )
    summary = {
        "status": receipt.get("status"), "receipt": str(receipt_path), "route": ROUTE,
        "observed_on": receipt.get("candidate", {}).get("observed_on"),
        "deployment_id": receipt.get("deployment", {}).get("id"),
        "sensor_status": receipt.get("sensor", {}).get("status") or receipt.get("sensor_result", {}).get("status"),
        "indexnow_status": receipt.get("indexnow", {}).get("status"),
        "blockers": receipt.get("blockers", []), "external_effects": receipt.get("external_effects", {}),
    }
    print(json.dumps(summary, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
