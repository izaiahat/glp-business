"""Contract tests for the premium article hero image lane.

The lane's whole reason to exist defensively: it must add a page-specific hero
when Higgsfield answers, and it must be INVISIBLE when Higgsfield does not.
Every test below is written from that pair of obligations.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str, filename: str):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


images = load("article_images_test", "article_images.py")
builder = load("build_opus_article_release_images_test", "build_opus_article_release.py")
materializer_source = (ROOT / "scripts/materialize_opus_run.py").read_text(encoding="utf-8")

PIL = pytest.importorskip("PIL.Image")
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402


ARTICLE = {
    "h1": "GLP-1 cost sensitivity: what changes the monthly price",
    "title": "GLP-1 cost sensitivity | Clinic Scout",
    "primary_keyword": "glp-1 cost sensitivity",
    "canonical_path": "/guides/glp-1-cost-sensitivity/",
}
VISUAL_KEY = "guide-glp-1-cost-sensitivity"


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """No test may touch the production cache, ledger, CLI or account."""
    monkeypatch.setenv("GLP_ARTICLE_IMAGE_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("GLP_ARTICLE_IMAGE_CLI", str(tmp_path / "no-such-cli"))
    monkeypatch.setenv("GLP_ARTICLE_IMAGES", "on")
    # Legacy Higgsfield fixtures stay provider-free; native chain has its own oracle.
    monkeypatch.setenv("GLP_ARTICLE_IMAGE_PROVIDERS", "higgsfield")
    monkeypatch.setenv("GLP_ARTICLE_IMAGE_VARIANTS", "2")
    yield


def composition(path: Path, *, blur: float = 0.0, seed: int = 3) -> Path:
    """A realistic 4K 16:9 render stand-in: gradient, shapes, edges, grain.

    The film grain matters: a synthetic flat render compresses to a couple of
    kilobytes and would trip the lane's own degenerate-variant floor, which is
    exactly the check that catches a real blank render.
    """
    image = Image.new("RGB", (3840, 2160), (16, 35, 59))
    draw = ImageDraw.Draw(image)
    for index in range(0, 2160, 6):
        shade = 20 + (index * 90) // 2160
        draw.line((0, index, 3840, index), fill=(shade, shade + 18, shade + 40))
    for index in range(14):
        left = 180 + (index * 241 * seed) % 2600
        top = 140 + (index * 173 * seed) % 1500
        draw.rectangle((left, top, left + 520, top + 320), fill=(246, 238, 221), outline=(195, 96, 73), width=9)
        draw.ellipse((left + 60, top + 60, left + 190, top + 190), fill=(47, 115, 113))
    grain = Image.effect_noise((3840, 2160), 30).convert("RGB")
    image = Image.blend(image, grain, 0.22)
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="WEBP", quality=95, method=4)
    return path


def fake_generator(blur_by_index: dict[int, float] | None = None):
    calls: list[dict] = []

    def generate(prompt, reference, destination, *, cli, timeout):
        index = int(destination.stem.rsplit("-", 1)[-1])
        calls.append({"prompt": prompt, "destination": str(destination), "index": index})
        composition(destination, blur=(blur_by_index or {}).get(index, 0.0), seed=index + 3)
        return {"model": images.MODEL, "credits": images.CREDITS_PER_RENDER}

    generate.calls = calls  # type: ignore[attr-defined]
    return generate


# ---------------------------------------------------------------------------
# contracts that keep the lane wired to the site's own image system
# ---------------------------------------------------------------------------

def test_family_contract_matches_the_builder_and_the_materializer():
    assert images.IMAGE_SUFFIXES == builder.IMAGE_SUFFIXES
    assert {spec[3] for spec in images.VARIANT_SPECS} == set(images.IMAGE_SUFFIXES)
    assert set(images.PERF_BUDGET_BYTES) == set(images.IMAGE_SUFFIXES)
    assert set(images.MIN_VARIANT_BYTES) == set(images.IMAGE_SUFFIXES)
    for suffix in images.IMAGE_SUFFIXES:
        assert images.MIN_VARIANT_BYTES[suffix] < images.PERF_BUDGET_BYTES[suffix]
    # The materializer declares exactly this family as the allowed public
    # addition; a lane that emitted a different set would fail the delta gate.
    for suffix in images.IMAGE_SUFFIXES:
        assert f'"{suffix}"' in materializer_source


def test_materializer_pins_the_image_lane_as_control_code():
    assert 'SYSTEM / "scripts/article_images.py"' in materializer_source


def test_pillow_binding_stays_function_local_to_the_image_lane():
    assert not hasattr(images, "Image")


def test_image_lane_pin_is_verified_when_declared_and_absent_on_legacy_templates():
    pinned = {"path": str(builder.ARTICLE_IMAGES_PATH), "sha256": builder.sha(builder.ARTICLE_IMAGES_PATH)}
    drifted = {"path": str(builder.ARTICLE_IMAGES_PATH), "sha256": "b" * 64}
    # Declared -> verified against the live bytes; drift fails closed.
    assert builder.has_control_pin({"control_files": [pinned]}, builder.ARTICLE_IMAGES_PATH) is True
    assert builder.require_control_pin({"control_files": [pinned]}, builder.ARTICLE_IMAGES_PATH,
                                       "article_images") == builder.ARTICLE_IMAGES_PATH
    with pytest.raises(RuntimeError, match="article_images_control_pin"):
        builder.require_control_pin({"control_files": [drifted]}, builder.ARTICLE_IMAGES_PATH,
                                    "article_images")
    # Not declared -> a template minted before the lane existed builds exactly
    # the way it did before, instead of inventing a new publish blocker.
    assert builder.has_control_pin({"control_files": []}, builder.ARTICLE_IMAGES_PATH) is False


def test_builder_generates_before_the_catalogs_and_installs_after_the_build():
    """Ordering is the whole contract.

    build-staging.mjs reads the hero alt text out of the article row, so the
    hero must be generated BEFORE the catalogs are written; the family must be
    installed AFTER `npm run build`, because the build wipes public/.
    """
    import inspect

    source = inspect.getsource(builder._build_candidate_materialized)
    prepare = source.index("article_images.prepare_hero")
    catalogs_written = source.index("path.write_text(json.dumps(catalogs[name]")
    npm_build = source.index("build = run(")
    install = source.index("article_images.install_hero")
    assert prepare < catalogs_written < npm_build < install
    # The stock-hero copy loop now lives in the lane, behind the fallback.
    assert 'source_image = image_dir / f"editorial-evidence-hero{suffix}"' not in source
    assert 'fallback_key="editorial-evidence-hero"' in source


# ---------------------------------------------------------------------------
# subject -> prompt / alt text
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("h1,motif", [
    ("GLP-1 cost sensitivity: what changes the monthly price", "cost-evidence"),
    ("Ozempic vs Zepbound: what the labels actually say", "comparison"),
    ("How to verify an online GLP-1 pharmacy", "verification"),
    ("What the FDA says about semaglutide labeling", "evidence"),
])
def test_subject_classification_drives_the_art_direction(h1, motif):
    subject = images.article_subject({**ARTICLE, "h1": h1, "primary_keyword": ""})
    assert subject["motif"] == motif
    prompt = images.hero_prompt(subject)
    assert h1 in prompt
    for banned in ("no people", "no faces", "no logos", "no legible text", "no needles"):
        assert banned in prompt.lower()


def test_alt_text_is_subject_bound_bounded_and_screen_reader_first():
    alt = images.hero_alt_text(images.article_subject(ARTICLE))
    assert len(alt) <= 125
    assert alt.startswith("Editorial illustration for GLP-1 cost sensitivity")
    assert "photo" not in alt.lower()
    long_topic = images.hero_alt_text(images.article_subject(
        {**ARTICLE, "h1": "A deliberately very long guide headline about GLP-1 pricing, "
                          "insurance appeals, coverage exceptions and monthly cost drivers"}))
    assert len(long_topic) <= 125
    assert long_topic.startswith("Editorial illustration for A deliberately very long guide headline")


def test_article_without_a_subject_is_a_lane_error_not_a_crash():
    with pytest.raises(images.ImageLaneError):
        images.article_subject({"h1": "   "})


# ---------------------------------------------------------------------------
# encoding + perf budget
# ---------------------------------------------------------------------------

def test_render_family_writes_seven_variants_inside_the_perf_budget(tmp_path):
    source = composition(tmp_path / "source.webp")
    files = images.render_family(source, tmp_path / "family", VISUAL_KEY)
    assert [row["suffix"] for row in files] == [spec[3] for spec in images.VARIANT_SPECS]
    for row in files:
        path = tmp_path / "family" / row["name"]
        assert path.is_file()
        assert row["bytes"] == path.stat().st_size
        assert images.MIN_VARIANT_BYTES[row["suffix"]] <= row["bytes"] <= images.PERF_BUDGET_BYTES[row["suffix"]]
        with Image.open(path) as opened:
            assert opened.size == (row["width"], row["height"])
    assert {row["name"] for row in files} == {f"{VISUAL_KEY}{suffix}" for suffix in images.IMAGE_SUFFIXES}


def test_render_family_fails_closed_and_leaves_no_partial_family(tmp_path, monkeypatch):
    source = composition(tmp_path / "source.webp")
    monkeypatch.setitem(images.PERF_BUDGET_BYTES, "-1600.jpg", 900)
    with pytest.raises(images.ImageLaneError, match="perf_budget_exceeded"):
        images.render_family(source, tmp_path / "family", VISUAL_KEY)
    assert sorted(p.name for p in (tmp_path / "family").iterdir() if not p.name.startswith(".")) == []


def test_undersized_or_wrong_ratio_sources_are_rejected(tmp_path):
    small = tmp_path / "small.webp"
    Image.new("RGB", (800, 450), (30, 30, 30)).save(small, format="WEBP", quality=95)
    with pytest.raises(images.ImageLaneError):
        images.validate_source(small)


# ---------------------------------------------------------------------------
# prepare_hero: the generate path, the cache, and every degradation
# ---------------------------------------------------------------------------

def test_prepare_hero_generates_selects_the_sharper_candidate_and_caches(tmp_path):
    generator = fake_generator({0: 6.0})  # candidate 0 is the blurred render
    receipt = images.prepare_hero(article=ARTICLE, visual_key=VISUAL_KEY, generator=generator)
    assert receipt["status"] == images.STATUS_GENERATED
    assert receipt["reason"] is None
    assert len(generator.calls) == 2
    assert receipt["credits_spent"] == 2 * images.CREDITS_PER_RENDER
    assert receipt["cache_hit"] is False
    assert receipt["selected_candidate"]["index"] == 1
    scores = {row["index"]: row["metrics"]["score"] for row in receipt["candidates"]}
    assert scores[1] > scores[0]
    assert len(receipt["files"]) == len(images.IMAGE_SUFFIXES)
    staged = Path(receipt["staged_dir"])
    assert sorted(p.name for p in staged.iterdir()) == sorted(
        f"{VISUAL_KEY}{suffix}" for suffix in images.IMAGE_SUFFIXES)

    # A repair cycle rebuilds the same article: cached, no new credits spent.
    again = images.prepare_hero(article=ARTICLE, visual_key=VISUAL_KEY, generator=generator)
    assert again["status"] == images.STATUS_GENERATED
    assert len(generator.calls) == 2
    assert again["credits_spent"] == 0
    assert again["cache_hit"] is True
    assert again["selected_candidate"]["sha256"] == receipt["selected_candidate"]["sha256"]


@pytest.mark.parametrize("fault", [
    RuntimeError("cli_exit_1:auth expired"),
    TimeoutError("higgsfield timed out"),
    OSError("network unreachable"),
])
def test_generation_outage_degrades_instead_of_failing_the_build(fault):
    def broken(prompt, reference, destination, *, cli, timeout):
        raise fault

    receipt = images.prepare_hero(article=ARTICLE, visual_key=VISUAL_KEY, generator=broken)
    assert receipt["status"] == images.STATUS_FALLBACK
    assert receipt["alt"] is None
    assert receipt["files"] == []
    assert receipt["credits_spent"] == 0
    assert type(fault).__name__ in receipt["reason"]


def test_a_generator_that_writes_garbage_degrades(tmp_path):
    def garbage(prompt, reference, destination, *, cli, timeout):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"not an image" * 20)
        return {}

    receipt = images.prepare_hero(article=ARTICLE, visual_key=VISUAL_KEY, generator=garbage)
    assert receipt["status"] == images.STATUS_FALLBACK


def test_kill_switch_degrades_without_calling_the_generator(monkeypatch):
    monkeypatch.setenv("GLP_ARTICLE_IMAGES", "off")
    generator = fake_generator()
    receipt = images.prepare_hero(article=ARTICLE, visual_key=VISUAL_KEY, generator=generator)
    assert receipt["status"] == images.STATUS_FALLBACK
    assert "generation_disabled" in receipt["reason"]
    assert generator.calls == []


def test_a_hostile_visual_key_never_reaches_the_generator():
    generator = fake_generator()
    receipt = images.prepare_hero(article=ARTICLE, visual_key="../../etc/passwd", generator=generator)
    assert receipt["status"] == images.STATUS_FALLBACK
    assert generator.calls == []


# ---------------------------------------------------------------------------
# install_hero: what actually lands in the built tree
# ---------------------------------------------------------------------------

def stock_family(image_dir: Path) -> None:
    image_dir.mkdir(parents=True, exist_ok=True)
    for suffix in images.IMAGE_SUFFIXES:
        (image_dir / f"{images.FALLBACK_KEY}{suffix}").write_bytes(b"stock-hero-bytes" * 800)


def test_install_hero_prefers_the_generated_family(tmp_path):
    receipt = images.prepare_hero(article=ARTICLE, visual_key=VISUAL_KEY, generator=fake_generator())
    image_dir = tmp_path / "public/assets/images"
    stock_family(image_dir)
    install = images.install_hero(receipt, image_dir=image_dir, visual_key=VISUAL_KEY)
    assert install["mode"] == "generated"
    assert sorted(install["files"]) == sorted(f"{VISUAL_KEY}{suffix}" for suffix in images.IMAGE_SUFFIXES)
    for suffix in images.IMAGE_SUFFIXES:
        generated = (image_dir / f"{VISUAL_KEY}{suffix}").read_bytes()
        assert generated != (image_dir / f"{images.FALLBACK_KEY}{suffix}").read_bytes()


@pytest.mark.parametrize("receipt", [None, {"status": images.STATUS_FALLBACK}])
def test_install_hero_reproduces_todays_stock_copy_when_degraded(tmp_path, receipt):
    image_dir = tmp_path / "public/assets/images"
    stock_family(image_dir)
    install = images.install_hero(receipt, image_dir=image_dir, visual_key=VISUAL_KEY)
    assert install["mode"] == "fallback"
    for suffix in images.IMAGE_SUFFIXES:
        assert (image_dir / f"{VISUAL_KEY}{suffix}").read_bytes() == (
            image_dir / f"{images.FALLBACK_KEY}{suffix}").read_bytes()


def test_install_hero_falls_back_when_the_staged_family_vanished(tmp_path):
    receipt = images.prepare_hero(article=ARTICLE, visual_key=VISUAL_KEY, generator=fake_generator())
    staged = Path(receipt["staged_dir"])
    (staged / f"{VISUAL_KEY}-og.jpg").unlink()
    image_dir = tmp_path / "public/assets/images"
    stock_family(image_dir)
    install = images.install_hero(receipt, image_dir=image_dir, visual_key=VISUAL_KEY)
    assert install["mode"] == "fallback"
    assert len(install["files"]) == len(images.IMAGE_SUFFIXES)


def test_install_hero_keeps_the_pre_existing_broken_base_error(tmp_path):
    image_dir = tmp_path / "public/assets/images"
    image_dir.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="route_visual_family_invalid"):
        images.install_hero(None, image_dir=image_dir, visual_key=VISUAL_KEY)
    assert list(image_dir.iterdir()) == []


# ---------------------------------------------------------------------------
# article metadata: alt text reaches the renderer, and only when it is honest
# ---------------------------------------------------------------------------

def test_generated_hero_publishes_disclosed_subject_alt_text():
    receipt = images.prepare_hero(article=ARTICLE, visual_key=VISUAL_KEY, generator=fake_generator())
    article = dict(ARTICLE)
    assert images.apply_article_image_metadata(article, receipt) is True
    hero = article["images"][0]
    assert hero["alt"] == receipt["alt"]
    assert hero["ai_generated"] is True
    assert hero["position"] == "hero"
    assert hero["filename"] == f"{VISUAL_KEY}-1600.jpg"
    assert (hero["width"], hero["height"]) == (1600, 900)


@pytest.mark.parametrize("receipt", [None, {"status": images.STATUS_FALLBACK, "alt": None}])
def test_degraded_runs_leave_the_article_row_byte_identical(receipt):
    article = dict(ARTICLE)
    assert images.apply_article_image_metadata(article, receipt) is False
    assert article == ARTICLE
    assert "images" not in article


# ---------------------------------------------------------------------------
# telemetry: a permanently degraded lane is visible, not silent
# ---------------------------------------------------------------------------

def test_ledger_and_health_track_degradation_and_recovery():
    def broken(prompt, reference, destination, *, cli, timeout):
        raise RuntimeError("cli_exit_1")

    images.prepare_hero(article=ARTICLE, visual_key=VISUAL_KEY, generator=broken)
    images.prepare_hero(article=ARTICLE, visual_key=VISUAL_KEY, generator=broken)
    degraded = images.health()
    assert degraded["degraded"] is True
    assert degraded["consecutive_fallbacks"] == 2
    assert degraded["last_status"] == images.STATUS_FALLBACK

    images.prepare_hero(article={**ARTICLE, "h1": "Recovered subject for GLP-1 pricing"},
                        visual_key="guide-recovered-subject", generator=fake_generator())
    healthy = images.health()
    assert healthy["degraded"] is False
    assert healthy["consecutive_fallbacks"] == 0
    events = images.read_events(limit=10)
    assert [row["status"] for row in events][-3:] == [
        images.STATUS_FALLBACK, images.STATUS_FALLBACK, images.STATUS_GENERATED]
    assert json.loads(images.health_path().read_text())["last_visual_key"] == "guide-recovered-subject"


# --- GLP-211: the no-upscaling claim must hold for native sources -------------

def test_native_source_floor_covers_the_largest_variant_on_both_axes():
    """A source smaller than the biggest variant would be UPSCALED by
    render_family, which silently breaks the lane's no-upscaling claim.

    The floor is derived from VARIANT_SPECS rather than hardcoded, so adding a
    larger variant raises the bar automatically instead of leaving this stale.
    """
    assert images.MAX_VARIANT_WIDTH == max(spec[0] for spec in images.VARIANT_SPECS)
    assert images.MAX_VARIANT_HEIGHT == max(spec[1] for spec in images.VARIANT_SPECS)

    # 1536x864 is 16:9 and passes a bare long-edge >= 1536 test, but encoding it
    # to the 1600x900 variant is an upscale. It must be rejected.
    assert images.source_dimensions_valid(1536, 864, "gpt_image_2_native") is False
    # At or above the largest variant on both axes, no upscaling is possible.
    assert images.source_dimensions_valid(1600, 900, "gpt_image_2_native") is True
    assert images.source_dimensions_valid(1792, 1008, "gpt_image_2_native") is True


def test_render_family_refuses_to_upscale_rather_than_doing_it_quietly(tmp_path):
    """Enforced where the claim is made, not only at acceptance."""
    from PIL import Image

    undersized = tmp_path / "small.png"
    Image.new("RGB", (1280, 720), "white").save(undersized)
    with pytest.raises(images.ImageLaneError) as caught:
        images.render_family(undersized, tmp_path / "out", "guide-undersized")
    assert "source_smaller_than_variant" in str(caught.value)


# --- GLP-210: spend accounting survives a post-charge failure -----------------

def test_fallback_receipt_reports_credits_actually_spent():
    """A render can be charged and a LATER step still fail. Reporting 0 there
    loses real money from the accounting."""
    charged = images._fallback_receipt("guide-x", None, "EncodingError:boom", credits_spent=7)
    assert charged["credits_spent"] == 7
    assert charged["status"] == images.STATUS_FALLBACK
    # Nothing spent still reports zero, so the field stays truthful both ways.
    assert images._fallback_receipt("guide-x", None, "early:boom")["credits_spent"] == 0
