"""GLP-73 regression: a rejected hero lineage steps down to stock, never vetoes the publish.

Source-only: the builder's tier functions are replaced with doubles that behave
like the real ones at their failure points (partial writes, then a raise). No
build, provider call, network or publication.
"""
import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "systems/seo-blog-machine/scripts"
ROUTE = "/guides/lineage-fallback/"
KEY = "guide-lineage-fallback"


def load_builder():
    # Host-only policy/pointer modules are stubbed at their import boundary, as
    # test_publication_date_binding does; none of them is image logic.
    stubs = {
        "decision_policy": ("consumer_policy", "require_allowed", "write_consumer_decision"),
        "glp_current_content_base_schema": ("COUNT_KEYS", "POINTER_REQUIRED_KEYS", "POINTER_SCHEMA",
                                            "POINTER_STATUS", "validate_pointer_shape", "validate_supersedes"),
        "glp_price_index_weekly": ("_validate_base_evidence",),
        "autonomous_release_gate": (),
    }
    for key, fields in stubs.items():
        stub = ModuleType(key)
        for field in fields:
            setattr(stub, field, set() if field in {"COUNT_KEYS", "POINTER_REQUIRED_KEYS"} else
                    (lambda *args, **kwargs: None))
        sys.modules.setdefault(key, stub)
    name = "glp73_image_lineage_builder"
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / "build_opus_article_release.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # stage_stock receives sys.modules[__name__]
    spec.loader.exec_module(module)
    return module


def generated_receipt(images, h1):
    return {
        "schema": images.SCHEMA, "status": images.STATUS_GENERATED, "reason": None,
        "provider": "gpt_image_2_native", "model": "gpt-image-2", "route": ROUTE, "h1": h1,
        "visual_key": KEY, "alt": "Editorial still life for the lineage fallback guide",
        "prompt_sha256": "a" * 64, "staged_dir": "/nonexistent/staging/family",
        "selected_candidate": {"index": 0, "sha256": "b" * 64, "source": {"sha256": "b" * 64}},
        "files": [{"name": f"{KEY}-1600.jpg", "suffix": "-1600.jpg", "width": 1600, "height": 900}],
        "credits_spent": 0, "cache_hit": False, "prepared_at_utc": "2026-10-03T00:00:00Z",
        "provider_chain_attempted": [{"provider": "gpt_image_2_native", "status": "GENERATED", "reason": None}],
    }


def candidate(tmp_path, builder, article):
    target = tmp_path / "candidate"
    catalogs = {"content_batch": {"articles": [article]}, "content_national_wow": {"articles": []}}
    catalog_paths = {"content_batch": target / "data/blog/content-batch.json",
                     "content_national_wow": target / "data/blog/content-national-wow-additions.json"}
    for name, path in catalog_paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(catalogs[name], indent=2, sort_keys=True) + "\n")
    manifest = target / "artifacts/ultracode/article-image-manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"schema": "glp-article-image-system-v3", "records": [],
                                    "article_count": 0, "route_master_count": 0, "output_count": 0}))
    state = target / builder.ROUTE_MASTER_STATE_RELATIVE
    state.write_text(json.dumps({"records": {}}))
    return target, catalogs, catalog_paths, manifest, state


def test_rejected_generated_lineage_rolls_back_and_publishes_with_stock(tmp_path, monkeypatch):
    builder = load_builder()
    contract, images = builder.article_image_release_contract, builder.article_images
    article = {"canonical_path": ROUTE, "h1": "Lineage fallback", "batch_gate_passed": True}
    generated = generated_receipt(images, article["h1"])
    assert images.apply_article_image_metadata(article, generated) is True  # as the builder does
    target, catalogs, catalog_paths, manifest, state = candidate(tmp_path, builder, article)
    manifest_bytes, state_bytes = manifest.read_bytes(), state.read_bytes()
    stray_derivative = target / "assets/derivatives" / f"{KEY}-1600.jpg"
    stray_master = target / "assets/source/article-masters-pass2" / f"{KEY}.webp"
    stray_receipt = target / "artifacts/ultracode/article-release-image-receipts" / f"{KEY}.json"

    def late_rejection(target_, article_, receipt_, key_, catalogs_):
        # The real function copies the receipt, master and derivatives before a
        # late collision check can still raise; leave the same footprint.
        for path in (stray_receipt, stray_master, stray_derivative):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"stray")
        manifest.write_text(json.dumps({"schema": "glp-article-image-system-v3", "tampered": True}))
        state.write_text("{}")
        raise RuntimeError(f"article_route_master_derivative_collision:{key_}-og.jpg")

    stock_calls = []

    def stock(builder_, target_, article_, receipt_, key_, catalogs_):
        assert builder_ is builder and target_ == target and key_ == KEY
        stock_calls.append(copy.deepcopy(receipt_))
        return {"status": "PASS_ARTICLE_STOCK_ROUTE_STAGED", "provider": "stock", "route": ROUTE,
                "visual_key": key_, "family": [{"name": f"{key_}{s}"} for s in builder.IMAGE_SUFFIXES],
                "receipt": {"path": f"artifacts/ultracode/article-release-image-receipts/{key_}.json",
                            "sha256": "c" * 64}}

    events = []
    monkeypatch.setattr(builder, "stage_new_article_route_master", late_rejection)
    monkeypatch.setattr(contract, "stage_stock", stock)
    monkeypatch.setattr(images, "record_event", events.append)

    lineage, receipt, selection = builder.stage_article_image_lineage(
        target, article, generated, KEY, catalogs, catalog_paths)

    # The publication proceeds on the provenance-backed stock tier.
    assert lineage["status"] == "PASS_ARTICLE_STOCK_ROUTE_STAGED" and selection["tier"] == "stock_fallback"
    assert [(a["tier"], a["status"]) for a in selection["attempts"]] == [
        ("native_generated", "REJECTED"), ("stock_fallback", "STAGED")]
    assert receipt["status"] == images.STATUS_FALLBACK and receipt["provider"] == "stock"
    assert receipt["reason"].startswith(
        "generated_lineage_rejected:RuntimeError:article_route_master_derivative_collision:")
    assert receipt["files"] == [] and receipt["staged_dir"] is None and receipt["alt"] is None
    assert receipt["rejected_generation"]["model"] == "gpt-image-2"
    assert receipt["provider_chain_attempted"][-1]["provider"] == "release_lineage"
    assert stock_calls == [receipt] and events == [receipt]
    # The rejected tier left no trace: new files gone, shared files byte-identical.
    assert manifest.read_bytes() == manifest_bytes and state.read_bytes() == state_bytes
    assert not any(p.exists() for p in (stray_derivative, stray_master, stray_receipt))
    assert sorted(selection["attempts"][0]["rolled_back"]) == sorted(
        p.as_posix() for p in (manifest, state, stray_receipt, stray_master, stray_derivative))
    # The catalogs no longer claim an AI-generated hero that will not ship.
    assert "images" not in article
    for path in catalog_paths.values():
        assert "ai_generated" not in path.read_text()
    assert json.loads(catalog_paths["content_batch"].read_text())["articles"][0]["h1"] == article["h1"]

    # Control: an accepted native lineage is not demoted and keeps its metadata.
    accepted = {"status": "PASS_ARTICLE_ROUTE_MASTER_STAGED", "route": ROUTE}
    monkeypatch.setattr(builder, "stage_new_article_route_master", lambda *args: accepted)
    monkeypatch.setattr(contract, "configure", lambda *args: None)
    native = {"canonical_path": ROUTE, "h1": article["h1"]}
    fresh = generated_receipt(images, article["h1"])
    images.apply_article_image_metadata(native, fresh)
    lineage, kept, selection = builder.stage_article_image_lineage(
        target, native, fresh, KEY, catalogs, catalog_paths)
    assert lineage is accepted and kept is fresh and selection["tier"] == "native_generated"
    assert native["images"][0]["ai_generated"] is True and len(stock_calls) == 1

    # A legacy job that never ran the lane publishes on stock, saying so.
    lineage, legacy, selection = builder.stage_article_image_lineage(
        target, {"canonical_path": ROUTE, "h1": article["h1"]}, None, KEY, catalogs, catalog_paths)
    assert legacy["status"] == images.STATUS_FALLBACK and legacy["provider"] == "stock"
    assert legacy["reason"] == "legacy_template_without_control_pin"
    assert stock_calls[-1] == legacy and selection["attempts"][0]["status"] == "NOT_ATTEMPTED"

    # Stock unavailability still yields the text-only tier, and a stock-tier
    # integrity failure is still a failure: the selector does not swallow it.
    text_only = {"status": "PASS_ARTICLE_TEXT_ONLY_STAGED", "availability": "unavailable",
                 "reason": "stock_family_missing", "receipt": {"path": "x", "sha256": "d" * 64}}
    monkeypatch.setattr(contract, "stage_stock", lambda *args: text_only)
    lineage, _, selection = builder.stage_article_image_lineage(
        target, {"canonical_path": ROUTE, "h1": article["h1"]}, None, KEY, catalogs, catalog_paths)
    assert lineage is text_only and selection["tier"] == "text_only"

    def identity_failure(*args):
        raise RuntimeError("article_stock_route_identity_invalid")
    monkeypatch.setattr(contract, "stage_stock", identity_failure)
    with pytest.raises(RuntimeError, match="article_stock_route_identity_invalid"):
        builder.stage_article_image_lineage(
            target, {"canonical_path": ROUTE, "h1": article["h1"]}, None, KEY, catalogs, catalog_paths)


@pytest.mark.parametrize("stock_available", [True, False])
def test_real_rejected_receipt_selects_real_stock_or_text_only(tmp_path, monkeypatch, stock_available):
    """Exercise the actual native receipt rejection and actual fallback staging.

    Only the unrelated renderer-source overlay is stubbed. Pixels and receipts
    are synthetic test inputs, not production provenance or acceptance output.
    """
    builder = load_builder()
    article = {"canonical_path": ROUTE, "h1": "Lineage fallback", "batch_gate_passed": True}
    generated = generated_receipt(builder.article_images, article["h1"])
    builder.article_images.apply_article_image_metadata(article, generated)
    target, catalogs, catalog_paths, manifest_path, state_path = candidate(tmp_path, builder, article)
    state = {"schema": "glp-pass2-route-image-generation-v1", "job_set_type": "gpt_image_2",
             "model": "GPT Image 2", "quality": "high", "resolution": "4k", "records": {}}
    state_path.write_text(json.dumps(state))
    manifest = json.loads(manifest_path.read_text())
    manifest["route_master_state"] = {"path": builder.ROUTE_MASTER_STATE_RELATIVE.as_posix(),
                                      "sha256": builder.sha(state_path)}
    manifest_path.write_text(json.dumps(manifest))
    monkeypatch.setattr(builder.article_image_release_contract, "configure", lambda *args: None)
    monkeypatch.setattr(builder, "configure_article_route_master_state_overlay", lambda *args: None)
    monkeypatch.setattr(builder, "patch_article_image_visual_qa_for_release", lambda *args: None)
    events = []
    monkeypatch.setattr(builder.article_images, "record_event", events.append)
    derivative = target / "assets/derivatives"
    derivative.mkdir(parents=True)
    if stock_available:
        for suffix in builder.IMAGE_SUFFIXES:
            dimensions = (1200, 630) if suffix == "-og.jpg" else (
                (800, 450) if suffix.startswith("-800") else (1600, 900))
            Image.new("RGB", dimensions, "#485367").save(derivative / ("editorial-evidence-hero" + suffix))

    # The pre-selector build called this exact tier directly and aborted here.
    with pytest.raises(RuntimeError, match="article_route_master_receipt_source_invalid"):
        builder.stage_new_article_route_master(target, article, generated, KEY, catalogs)
    lineage, effective, selection = builder.stage_article_image_lineage(
        target, article, generated, KEY, catalogs, catalog_paths)
    assert selection["attempts"][0]["status"] == "REJECTED"
    assert "article_route_master_receipt_source_invalid" in effective["reason"]
    assert "images" not in article and article["batch_gate_passed"] is True
    assert events == [effective]
    receipt_path = target / lineage["receipt"]["path"]
    assert builder.sha(receipt_path) == lineage["receipt"]["sha256"]
    receipt = json.loads(receipt_path.read_text())
    manifest = json.loads(manifest_path.read_text())
    if stock_available:
        assert selection["tier"] == "stock_fallback" and len(lineage["family"]) == 7
        assert receipt["status"] == "FALLBACK" and receipt["provider"] == "stock"
        assert receipt["fallback_key"] == "editorial-evidence-hero"
        assert receipt["stock_source_sha256"] == builder.sha(derivative / "editorial-evidence-hero-1600.webp")
        assert manifest["records"][0]["generation_model"] == "stock"
        assert manifest["external_generation_services_used"] == 0
        assert json.loads(state_path.read_text())["records"][ROUTE]["provider"] == "stock"
        for output in lineage["family"]:
            assert builder.sha(derivative / output["name"]) == output["sha256"]
    else:
        assert selection["tier"] == "text_only" and lineage["family"] == []
        assert receipt["status"] == "TEXT_ONLY" and receipt["reason"] == "stock_family_missing"
        assert manifest["article_count"] == 1 and manifest["route_master_count"] == 0
        assert article["image_availability"] == "text_only"
        assert not list(derivative.glob(KEY + "*"))
    assert json.loads(catalog_paths["content_batch"].read_text())["articles"][0] == article


@pytest.mark.parametrize("collision_type", ["symlink", "dangling_symlink", "directory", "fifo"])
def test_fallback_preserves_preexisting_nonregular_lineage_collision(tmp_path, monkeypatch, collision_type):
    """A decorative fallback cannot erase inherited malformed-tree evidence."""
    import os

    builder = load_builder()
    article = {"canonical_path": ROUTE, "h1": "Lineage fallback", "batch_gate_passed": True}
    generated = generated_receipt(builder.article_images, article["h1"])
    target, catalogs, catalog_paths, manifest, state = candidate(tmp_path, builder, article)
    derivative = target / "assets/derivatives"
    derivative.mkdir(parents=True)
    for suffix in builder.IMAGE_SUFFIXES:
        dimensions = (1200, 630) if suffix == "-og.jpg" else (
            (800, 450) if suffix.startswith("-800") else (1600, 900))
        Image.new("RGB", dimensions, "#485367").save(derivative / ("editorial-evidence-hero" + suffix))
    state.write_text(json.dumps({"schema": "glp-pass2-route-image-generation-v1",
                               "job_set_type": "gpt_image_2", "model": "GPT Image 2",
                               "quality": "high", "resolution": "4k", "records": {}}))
    doc = json.loads(manifest.read_text())
    doc["route_master_state"] = {"path": builder.ROUTE_MASTER_STATE_RELATIVE.as_posix(),
                                 "sha256": builder.sha(state)}
    manifest.write_text(json.dumps(doc))
    collision = derivative / f"{KEY}-og.jpg"
    if collision_type == "symlink":
        collision.symlink_to(derivative / "editorial-evidence-hero-og.jpg")
    elif collision_type == "dangling_symlink":
        collision.symlink_to(derivative / "missing.jpg")
    elif collision_type == "directory":
        collision.mkdir()
    else:
        os.mkfifo(collision)
    before = {p: p.read_bytes() for p in (manifest, state, *catalog_paths.values())}
    identity = collision.lstat()
    link = os.readlink(collision) if collision.is_symlink() else None
    events = []
    monkeypatch.setattr(builder.article_images, "record_event", events.append)
    monkeypatch.setattr(builder.article_image_release_contract, "configure", lambda *args: None)
    monkeypatch.setattr(builder, "configure_article_route_master_state_overlay", lambda *args: None)
    monkeypatch.setattr(builder, "patch_article_image_visual_qa_for_release", lambda *args: None)

    with pytest.raises(RuntimeError, match="article_image_lineage_nonregular_collision"):
        builder.stage_article_image_lineage(target, article, generated, KEY, catalogs, catalog_paths)

    assert collision.lstat().st_ino == identity.st_ino
    assert collision.lstat().st_mode == identity.st_mode
    if link is not None:
        assert os.readlink(collision) == link
    assert all(p.read_bytes() == value for p, value in before.items())
    assert events == []
    assert not (target / "artifacts/ultracode/article-release-image-receipts" / f"{KEY}.json").exists()
