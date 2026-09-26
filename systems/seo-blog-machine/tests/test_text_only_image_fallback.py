"""Focused image-only release cases; no build, browser or publication."""
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import article_image_release_contract as images


def _builder_module():
    spec = importlib.util.spec_from_file_location("glp73_text_only_builder", SCRIPTS / "build_opus_article_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def release(tmp_path, monkeypatch):
    route = "/insights/fallback-contract-test/"
    key = "guide-fallback-contract-test"
    article = {"canonical_path": route, "h1": "Fallback contract test"}
    catalog = {"articles": [article]}
    catalogs = {"content_batch": catalog, "content_national_wow": {"articles": []}}
    for name in ("content-batch", "content-national-wow-additions"):
        path = tmp_path / "data/blog" / (name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(catalog if name == "content-batch" else catalogs["content_national_wow"]))
    manifest = {"schema": "glp-article-image-system-v3", "records": [], "route_master_count": 0,
                "article_count": 0, "output_count": 0}
    path = tmp_path / "artifacts/ultracode/article-image-manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(manifest))
    state = {"records": {}}
    builder = SimpleNamespace(
        IMAGE_SUFFIXES=("-800.avif", "-1600.avif", "-800.webp", "-1600.webp", "-800.jpg", "-1600.jpg", "-og.jpg"),
        ROUTE_MASTER_STATE_RELATIVE=Path("artifacts/ultracode/article-route-master-state.json"),
        resolve_predecessor_route_master_state=lambda *args: {"state": state},
        configure_article_route_master_state_overlay=lambda *args: None,
        patch_article_image_visual_qa_for_release=lambda *args: None,
        _make_candidate_directory_writable=lambda path: path.mkdir(parents=True, exist_ok=True),
    )
    monkeypatch.setattr(images, "configure", lambda *args: None)
    return tmp_path, article, catalogs, builder, key


def _stock_images(target, suffixes):
    derivative = target / "assets/derivatives"
    derivative.mkdir(parents=True)
    for suffix in suffixes:
        size = (1200, 630) if suffix == "-og.jpg" else ((800, 450) if suffix.startswith("-800") else (1600, 900))
        Image.new("RGB", size, "#485367").save(derivative / ("editorial-evidence-hero" + suffix))
    return derivative


@pytest.mark.parametrize("failure,reason", [("missing", "stock_family_missing"), ("corrupt", "stock_family_invalid_pixels")])
def test_unavailable_stock_stages_text_only_receipt_without_pixels(release, failure, reason):
    target, article, catalogs, builder, key = release
    derivative = _stock_images(target, builder.IMAGE_SUFFIXES)
    source = derivative / "editorial-evidence-hero-1600.jpg"
    if failure == "missing":
        source.unlink()
    else:
        source.write_bytes(b"not-an-image")
    result = images.stage_stock(builder, target, article, {"status": "FALLBACK", "provider": "stock", "reason": "generation_failed"}, key, catalogs)
    assert result["availability"] == "unavailable" and result["reason"] == reason
    assert result["family"] == [] and result["master"] is None
    assert not list(derivative.glob(key + "*"))
    assert not (target / "assets/source/article-masters-pass2" / (key + ".webp")).exists()
    manifest = json.loads((target / "artifacts/ultracode/article-image-manifest.json").read_text())
    assert manifest["article_count"] == 1 and manifest["route_master_count"] == 0
    assert manifest["text_only_routes"] == [{"route": article["canonical_path"], "receipt": result["receipt"]}]
    receipt_path = target / result["receipt"]["path"]
    assert images.sha(receipt_path) == result["receipt"]["sha256"]
    assert json.loads(receipt_path.read_text())["status"] == "TEXT_ONLY"
    assert json.loads((target / "data/blog/content-batch.json").read_text())["articles"][0]["image_availability"] == "text_only"


def test_valid_stock_keeps_seven_images_and_route_master(release):
    target, article, catalogs, builder, key = release
    derivative = _stock_images(target, builder.IMAGE_SUFFIXES)
    result = images.stage_stock(builder, target, article, {"status": "FALLBACK", "provider": "stock", "reason": "generation_failed"}, key, catalogs)
    assert result["status"] == "PASS_ARTICLE_STOCK_ROUTE_STAGED" and len(result["family"]) == 7
    assert "image_availability" not in article
    for record in result["family"]:
        assert images.sha(derivative / record["name"]) == record["sha256"]
    manifest = json.loads((target / "artifacts/ultracode/article-image-manifest.json").read_text())
    assert manifest["article_count"] == manifest["route_master_count"] == 1
    assert "text_only_routes" not in manifest


def test_declared_public_images_narrow_only_for_text_only_receipt(tmp_path):
    builder = _builder_module()
    key = "guide-fallback-contract-test"
    images_paths = [f"assets/images/{key}{suffix}" for suffix in builder.IMAGE_SUFFIXES]
    job = {"allowed_public_added": ["insights/fallback-contract-test/index.html", *images_paths]}
    assert builder.require_declared_image_additions_absent(job, tmp_path, key) == sorted(images_paths)
    job["allowed_public_added"] = ["insights/fallback-contract-test/index.html"]
    assert builder.require_declared_image_additions_absent(job, tmp_path, key, text_only=True) == []
    with pytest.raises(RuntimeError, match="declared_image_additions_invalid"):
        builder.declared_image_additions(job, key)
    job["allowed_public_added"].append(images_paths[0])
    with pytest.raises(RuntimeError, match="declared_image_additions_invalid"):
        builder.declared_image_additions(job, key, text_only=True)
