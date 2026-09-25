#!/usr/bin/env python3
"""The go-runtime refresh must pass the promotion authority into the derive.

It runs AFTER apply_go_runtime_promotion_authority. Omitting the argument
regenerated every entry unpromoted and silently reverted the promotion, so the
deploy failed its own gate with go_runtime_promotion_zero_active.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

BUSINESS = Path(__file__).resolve().parents[1]
REFRESH = BUSINESS / "scripts/glp_go_runtime_manifest_refresh.mjs"
sys.path.insert(0, str(BUSINESS / "scripts"))
import glp_affiliate_wire_backlog_worker as worker  # noqa: E402


def _source() -> str:
    return REFRESH.read_text(encoding="utf-8")


def test_derive_call_passes_the_promotion_authority():
    call = re.search(r"deriveGoEntriesFromRegistry\(([^;]*?)\),\s*\n\s*\};", _source(), re.S)
    assert call, "deriveGoEntriesFromRegistry call not found"
    assert "goPromotionAuthority" in call.group(1), (
        "the refresh must pass goPromotionAuthority; without it every entry "
        "regenerates unpromoted and go_runtime_promotion_zero_active fires")


def test_authority_is_read_from_the_release_and_defaults_to_empty():
    src = _source()
    assert "go-runtime-promotion-authority.json" in src
    # absent file must not throw - it has to degrade to the previous behaviour
    assert re.search(r"catch\s*\{\s*goPromotionAuthority = \{\};\s*\}", src)


def test_authority_path_matches_the_worker_constant():
    assert str(worker.GO_RUNTIME_PROMOTION_AUTHORITY_RELATIVE) == (
        "data/go-runtime-promotion-authority.json")
    assert "data" in _source() and "go-runtime-promotion-authority.json" in _source()


def test_contract_support_declares_the_fifth_parameter():
    """The derive signature the support installs must accept the argument."""
    src = (BUSINESS / "scripts/glp_affiliate_wire_backlog_worker.py").read_text()
    assert "affiliateMap = {}, goPromotionAuthority = {}" in src


def test_build_staging_derive_is_patched_to_pass_the_authority(tmp_path):
    """build-staging re-derives the manifest and fails closed on drift.

    It had the same four-argument omission as the refresh script, so once the
    runtime manifest is correctly promoted its unpromoted re-derivation threw
    frozen_build4_go_runtime_manifest_drift.
    """
    import shutil
    release = BUSINESS / "site-system/flagship-v2/releases/glp-r2-updater-20260901T202456Z-292e2ff9e0"
    if not release.is_dir():
        return
    root = tmp_path / "cand"
    for rel in ("src/program-registry.mjs", "scripts/regenerate-go-runtime-manifest.mjs",
                "scripts/build-staging.mjs", "tests/go-runtime-manifest.test.mjs",
                "tests/funnel-v2-instrumentation.test.mjs"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(release / rel, root / rel)
    result = worker._ensure_go_runtime_promotion_contract_support(root)
    assert "scripts/build-staging.mjs" in result["changed_files"]
    staging = (root / "scripts/build-staging.mjs").read_text()
    assert 'loadAffiliateLinkMap("compare"), goPromotionAuthority) };' in staging
    assert "go-runtime-promotion-authority.json" in staging
    # idempotent: a second pass changes nothing
    assert worker._ensure_go_runtime_promotion_contract_support(root)["changed_files"] == []
