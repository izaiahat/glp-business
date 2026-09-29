"""Offline IMAGE-1 caller-boundary regression; no provider, credit or ledger effect."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "systems/seo-blog-machine/scripts/article_images.py"
SPEC = importlib.util.spec_from_file_location("glp73_native_only_images", SOURCE)
assert SPEC and SPEC.loader
images = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(images)

ARTICLE = {
    "h1": "GLP-1 cost sensitivity: what changes the monthly price",
    "title": "GLP-1 cost sensitivity | Clinic Scout",
    "primary_keyword": "glp-1 cost sensitivity",
    "canonical_path": "/guides/glp-1-cost-sensitivity/",
}


@pytest.mark.parametrize("configured,native_calls", [
    ("higgsfield", 0),
    ("gpt_image_2_native,higgsfield", 0),
    ("gpt_image_2_native", 2),
])
def test_environment_never_admits_paid_fallback_after_native_uncertainty(
    configured, native_calls, tmp_path, monkeypatch
):
    monkeypatch.setenv("GLP_ARTICLE_IMAGE_PROVIDERS", configured)
    monkeypatch.setenv("GLP_ARTICLE_IMAGES", "on")
    monkeypatch.setenv("GLP_ARTICLE_IMAGE_VARIANTS", "1")
    monkeypatch.setenv("GLP_ARTICLE_IMAGE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("GLP_ARTICLE_IMAGE_CLI", str(tmp_path / "no-such-cli"))
    effects = []

    def forbidden(*args, **kwargs):
        effects.append("paid_or_credit")
        raise AssertionError("paid provider boundary reached")

    def uncertain_native(*args, **kwargs):
        effects.append("native")
        raise images.ImageLaneError("prior_attempt_requires_reconciliation")

    monkeypatch.setitem(sys.modules, "gpt_image_2_native", SimpleNamespace(candidate=uncertain_native))
    monkeypatch.setattr(images, "available_credits", forbidden)
    monkeypatch.setattr(images, "higgsfield_candidate", forbidden)
    monkeypatch.setattr(images, "record_event", lambda receipt: None)
    for _ in range(2):  # An identical later caller cannot re-enable a paid dispatch.
        receipt = images.prepare_hero(article=ARTICLE, visual_key="guide-glp-1-cost-sensitivity")
        assert receipt["status"] == images.STATUS_FALLBACK
        assert receipt["provider"] == "stock"
        assert receipt["credits_spent"] == 0
        if native_calls:
            assert "prior_attempt_requires_reconciliation" in receipt["reason"]
            assert [a["provider"] for a in receipt["provider_chain_attempted"]] == ["gpt_image_2_native"]
        else:
            assert "provider_chain_invalid" in receipt["reason"]
            assert receipt["provider_chain_attempted"] == []
    assert effects == ["native"] * native_calls
