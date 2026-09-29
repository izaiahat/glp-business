#!/usr/bin/env python3
"""Premium per-article hero imagery for the GLP article release pipeline.

Until now every published guide shipped the SAME stock hero: the builder copied
``editorial-evidence-hero{suffix}`` into ``guide-<slug>{suffix}``, so 40+ pages
shared one picture. This module generates a page-specific premium hero through
the Higgsfield CLI, picks the best of N candidates mechanically, and encodes the
exact seven-file responsive family the site contract expects, each variant
inside a measured byte budget derived from the images already on the site.

THE INVARIANT, and the reason this file is written defensively: **no image beats
a broken build.** Generation is decoration, never a gate. Every fault - CLI
missing, auth expired, credits exhausted, network timeout, malformed payload,
an encode that will not fit the perf budget - degrades to the deterministic
``editorial-evidence-hero`` copy the pipeline used before, writes the reason to
a degradation ledger, and lets the publish proceed. The only error this module
raises into the build is ``route_visual_family_invalid``: the PRE-EXISTING
failure for "not even the fallback family exists in the base tree", which has
always meant a genuinely broken base.

Cost discipline: a 4K ``gpt_image_2`` render costs 12 credits, so a two-variant
article costs 24. Candidates are cached by ``visual_key + prompt sha`` under
``reports/glp-clinic-scout/seo-machine/article-images/`` - the repair loop
rebuilds the same candidate tree several times per article and must never
re-spend. A pre-flight balance check refuses to start when the account is near
empty (degrade, don't drain).

Machine entry points (no hand-run steps):
  * ``prepare_hero()``  - called by build_opus_article_release before the
    catalogs are written, so the alt text lands in the article row that
    build-staging.mjs reads.
  * ``install_hero()``  - called after ``npm run build``, where the old
    seven-file copy loop lived.
  * ``python3 article_images.py --health`` - operator/sensor view of the
    degradation ledger; exits 2 while the lane is degraded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

BUSINESS = Path("/home/ubuntu/business")
DEFAULT_CACHE_ROOT = BUSINESS / "reports/glp-clinic-scout/seo-machine/article-images"
DEFAULT_CLI = Path("/usr/local/bin/higgsfield")
HIGGSFIELD_MODEL = "gpt_image_2"
HIGGSFIELD_CREDITS_PER_RENDER = 12
# GPT Image 2.5 through the Codex subscriptions is THE article-image path
# (operator 2026-09-17, receipt
# ops/linear/approvals/OPERATOR-DECISION-20260917-gpt-image-2-5-via-codex-primary.json).
# Codex's app-server reports the completed imageGeneration item's model id as
# "gpt-image-2"; that is the string the renderer actually returns and therefore
# the only string verification may bind to, and gpt_image_2_native.MODEL requests
# exactly it. Verification stays an exact single-string match: if Codex ever
# reports a different id the lane fails CLOSED to the receipted stock hero and
# the health alert fires - it never widens to accept an arbitrary model.
NATIVE_MODEL = "gpt-image-2"
NATIVE_CREDITS_PER_RENDER = 0
PROVIDER_MODELS = {"gpt_image_2_native": NATIVE_MODEL, "higgsfield": HIGGSFIELD_MODEL}
PROVIDER_CREDITS = {"gpt_image_2_native": NATIVE_CREDITS_PER_RENDER, "higgsfield": HIGGSFIELD_CREDITS_PER_RENDER}
# Compatibility names for pre-provider Higgsfield receipts and fixtures.
MODEL = HIGGSFIELD_MODEL
CREDITS_PER_RENDER = HIGGSFIELD_CREDITS_PER_RENDER
FALLBACK_KEY = "editorial-evidence-hero"

# The site's responsive family. Kept byte-identical to
# build_opus_article_release.IMAGE_SUFFIXES; test_article_images asserts the two
# never drift.
IMAGE_SUFFIXES = (
    "-800.avif", "-1600.avif", "-800.webp", "-1600.webp",
    "-800.jpg", "-1600.jpg", "-og.jpg",
)

# (width, height, encoder, suffix, quality ladder). The first quality is the
# house setting used by every image already on the site
# (scripts/prepare_glp_wave2_higgsfield_assets.py); the rest are the step-downs
# that keep an unusually busy render inside the perf budget.
VARIANT_SPECS: tuple[tuple[int, int, str, str, tuple[int, ...]], ...] = (
    (800, 450, "AVIF", "-800.avif", (70, 62, 54, 46)),
    (1600, 900, "AVIF", "-1600.avif", (70, 62, 54, 46)),
    (800, 450, "WEBP", "-800.webp", (82, 74, 66, 58)),
    (1600, 900, "WEBP", "-1600.webp", (82, 74, 66, 58)),
    (800, 450, "JPEG", "-800.jpg", (88, 82, 76, 70)),
    (1600, 900, "JPEG", "-1600.jpg", (88, 82, 76, 70)),
    (1200, 630, "JPEG", "-og.jpg", (88, 82, 76, 70)),
)

# Perf budget. Measured 2026-08-20 across the 101 hero families already live on
# glpclinicscout.com; each cap sits just above the observed maximum, so a
# generated hero can never be heavier than the heaviest page the site already
# ships. -1600.avif is the LCP asset for a guide hero.
PERF_BUDGET_BYTES = {
    "-800.avif": 55_000,     # live max 40,577
    "-1600.avif": 150_000,   # live max 115,697
    "-800.webp": 70_000,     # live max 56,542
    "-1600.webp": 200_000,   # live max 161,320
    "-800.jpg": 120_000,     # live max 101,861
    "-1600.jpg": 340_000,    # live max 302,258
    "-og.jpg": 220_000,      # live max 186,503
}
# Degenerate-variant floors. A variant under these is a blank, greybox or
# half-rendered frame rather than a composed hero. Each floor is ~1/3 of the
# smallest live example of that variant on the site (measured 2026-08-20), so a
# genuinely empty render (1-2 KB) trips it while an unusually clean but real
# composition does not.
MIN_VARIANT_BYTES = {
    "-800.avif": 5_500,     # live min 17,344
    "-1600.avif": 9_000,    # live min 28,427
    "-800.webp": 5_500,     # live min 20,000
    "-1600.webp": 18_000,   # live min 56,132
    "-800.jpg": 18_000,     # live min 56,962
    "-1600.jpg": 55_000,    # live min 170,266
    "-og.jpg": 16_000,      # live min 50,811
}
# The generated 4K source itself.
MIN_SOURCE_BYTES = 100_000
# GLP-211: the "no upscaling" claim is only true if a source is at least as
# large as the biggest variant we encode. Derive that floor from VARIANT_SPECS
# so it can never drift away from what render_family actually produces.
MAX_VARIANT_WIDTH = max(spec[0] for spec in VARIANT_SPECS)
MAX_VARIANT_HEIGHT = max(spec[1] for spec in VARIANT_SPECS)

SOURCE_MIN_WIDTH = 3800
SOURCE_MIN_HEIGHT = 2100

STATUS_GENERATED = "GENERATED"
STATUS_FALLBACK = "FALLBACK"
SCHEMA = "glp-article-hero-image-v1"

_WORD = re.compile(r"[a-z0-9]+")


class ImageLaneError(RuntimeError):
    """Internal, always caught: signals 'degrade to the stock hero'."""


# ---------------------------------------------------------------------------
# environment knobs (every one has a safe default; none is required)
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or default).strip()


def generation_enabled() -> bool:
    return _env("GLP_ARTICLE_IMAGES", "on").lower() not in {"off", "0", "false", "no"}


def cli_path() -> Path:
    return Path(_env("GLP_ARTICLE_IMAGE_CLI", str(DEFAULT_CLI)))


def cache_root() -> Path:
    return Path(_env("GLP_ARTICLE_IMAGE_CACHE", str(DEFAULT_CACHE_ROOT)))


def variant_count() -> int:
    try:
        value = int(_env("GLP_ARTICLE_IMAGE_VARIANTS", "2"))
    except ValueError:
        value = 2
    return max(1, min(3, value))


def lane_budget_seconds() -> int:
    try:
        value = int(_env("GLP_ARTICLE_IMAGE_BUDGET_S", "900"))
    except ValueError:
        value = 900
    return max(60, min(2400, value))


def credit_reserve() -> int:
    try:
        value = int(_env("GLP_ARTICLE_IMAGE_CREDIT_RESERVE", "120"))
    except ValueError:
        value = 120
    return max(0, value)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def warn(message: str) -> None:
    print(f"[article-images] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# subject -> prompt / alt text
# ---------------------------------------------------------------------------

COST_WORDS = ("cost", "price", "pricing", "insurance", "coverage", "appeal", "savings",
              "hsa", "fsa", "medicare", "medicaid", "subscription", "sensitivity", "budget")
PHARMACY_WORDS = ("pharmacy", "shipping", "fulfillment", "verification", "verify",
                  "checklist", "counterfeit", "compounded", "compounding", "sourcing")
COMPARISON_WORDS = (" vs ", "versus", "comparison", "compare", "alternative")


def article_subject(article: dict[str, Any]) -> dict[str, str]:
    """The exact editorial subject the hero must depict, from the article row."""
    if not isinstance(article, dict):
        raise ImageLaneError("article_invalid")
    h1 = str(article.get("h1") or article.get("title") or "").strip()
    if not h1:
        raise ImageLaneError("article_subject_missing")
    keyword = str(article.get("primary_keyword") or "").strip()
    route = str(article.get("canonical_path") or "").strip()
    lower = f" {h1.lower()} {keyword.lower()} "
    if any(word in lower for word in COMPARISON_WORDS):
        motif = "comparison"
        focus = ("two balanced evidence paths side by side, transparent tradeoffs laid out as "
                 "paper and glass layers, no winner declared")
    elif any(word in lower for word in PHARMACY_WORDS):
        motif = "verification"
        focus = ("sealed fulfillment evidence, a source checklist and a magnifying lens over "
                 "provenance documents, no brand marks")
    elif any(word in lower for word in COST_WORDS):
        motif = "cost-evidence"
        focus = ("layered cost and coverage evidence, dated source documents and a calm "
                 "comparison ledger, no currency figures and no legible numbers")
    else:
        motif = "evidence"
        focus = ("layered clinical-policy evidence, source boundaries and a magnifying lens, "
                 "no treatment recommendation")
    return {"h1": h1, "primary_keyword": keyword, "route": route, "motif": motif, "focus": focus}


def hero_prompt(subject: dict[str, str]) -> str:
    """House art direction, locked to the visual system already on the site."""
    return (
        f"Create a unique premium 16:9 editorial web hero for Clinic Scout about: {subject['h1']}. "
        f"Visual focus: {subject['focus']}. Award-quality paper-and-glass evidence system: deep navy, "
        "teal, muted coral, warm cream, restrained brass details. Tactile paper, glass and realistic "
        "materials, high-end documentary magazine art direction, soft directional studio light. "
        "No people, no faces, no logos, no product branding, no legible text, no letters, no numbers, "
        "no medical claims, no needles, no scattered pills, no before-and-after imagery, no stock-photo "
        "aesthetic. Keep a calm negative-space region suitable for adjacent web copy."
    )


def _clip_words(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    cut = value[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut or value[:limit]


# Longest phrasing first: hero_alt_text picks the longest one that still fits
# the 125-character budget, so a long headline shortens the scene instead of
# emitting a truncated fragment.
ALT_MOTIF = {
    "comparison": ("two balanced evidence paths in paper and glass", "balanced evidence paths", "paper evidence layers"),
    "verification": ("a source checklist and magnifying lens over sealed documents", "a checklist and magnifying lens", "a source checklist"),
    "cost-evidence": ("layered cost and coverage documents on an editorial desk", "layered cost documents", "cost evidence layers"),
    "evidence": ("layered research documents and evidence cards", "layered research documents", "research documents"),
}


def hero_alt_text(subject: dict[str, str]) -> str:
    """Bind the picture to the article subject, then describe it. <=125 chars.

    The subject comes first and is never sacrificed to the 125-char alt budget:
    a clipped scene description still reads correctly, a clipped topic loses the
    only part a screen-reader user needs. Never claims the image is a
    photograph, a chart, or evidence in itself.
    """
    scenes = ALT_MOTIF.get(subject["motif"], ALT_MOTIF["evidence"])
    topic = _clip_words(subject["h1"].rstrip(" .?!"), 84)
    head = f"Editorial illustration for {topic}"
    for scene in scenes:
        candidate = f"{head}: {scene}"
        if len(candidate) <= 125:
            return candidate
    return _clip_words(head, 125)


# ---------------------------------------------------------------------------
# candidate generation (Higgsfield CLI) - every failure raises ImageLaneError
# ---------------------------------------------------------------------------

def _url_candidates(value: Any, path: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _url_candidates(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _url_candidates(child, f"{path}[{index}]")
    elif isinstance(value, str) and value.startswith("http"):
        yield path, value


def result_urls(payload: Any) -> list[str]:
    """Downloadable renders, best first.

    ``result_url`` is the full-resolution render and ``min_result_url`` the
    downscaled preview - a hero needs the former, so preview URLs are only a
    last resort and are still dimension-checked before use.
    """
    full: list[str] = []
    preview: list[str] = []
    for path, value in _url_candidates(payload):
        if path.endswith("min_result_url"):
            preview.append(value)
        elif path.endswith("result_url"):
            full.append(value)
    ordered = [*full, *preview]
    if not ordered:
        raise ImageLaneError("result_url_missing")
    return ordered


def available_credits(cli: Path, timeout: int = 90) -> float:
    completed = subprocess.run(
        [str(cli), "account", "status", "--json"],
        text=True, capture_output=True, timeout=timeout,
    )
    if completed.returncode:
        raise ImageLaneError(f"account_status_failed:{completed.returncode}")
    try:
        return float(json.loads(completed.stdout).get("credits"))
    except (json.JSONDecodeError, TypeError, ValueError) as error:
        raise ImageLaneError("account_status_unreadable") from error


def higgsfield_candidate(prompt: str, reference: Path | None, destination: Path,
                         *, cli: Path, timeout: int) -> dict[str, Any]:
    """Render one 4K candidate to ``destination``. Default generator."""
    if not cli.is_file():
        raise ImageLaneError(f"cli_missing:{cli}")
    argv = [str(cli), "generate", "create", MODEL, "--prompt", prompt,
            "--aspect_ratio", "16:9", "--resolution", "4k", "--quality", "high",
            "--wait", "--wait-timeout", f"{max(60, timeout - 120)}s", "--json"]
    if reference is not None and reference.is_file():
        argv.extend(["--image", str(reference)])
    completed = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    if completed.returncode:
        raise ImageLaneError(f"cli_exit_{completed.returncode}:{completed.stderr.strip()[-300:]}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ImageLaneError("cli_payload_unreadable") from error
    destination.parent.mkdir(parents=True, exist_ok=True)
    last = "no_url_downloaded"
    for url in result_urls(payload):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(request, timeout=180) as response:  # noqa: S310 - fixed https host
                body = response.read()
                content_type = response.headers.get("content-type", "")
            if "image" not in content_type:
                raise ImageLaneError(f"unexpected_content_type:{content_type}")
            with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".part", delete=False) as handle:
                handle.write(body)
                temp = Path(handle.name)
            try:
                validate_source(temp)
            except Exception:
                temp.unlink(missing_ok=True)
                raise
            os.replace(temp, destination)
            return {"model": MODEL, "credits": CREDITS_PER_RENDER,
                    "content_type": content_type, "result_url": url}
        except Exception as error:
            last = f"{type(error).__name__}:{error}"
    raise ImageLaneError(f"render_download_failed:{last}")


def source_dimensions_valid(width: int, height: int, provider: str = "higgsfield") -> bool:
    """Article heroes: native maximum output >=1536 long edge; HF keeps 4K.

    Landscape/aspect and byte/provenance gates remain shared. Unknown providers
    cannot opt into the native exception.
    """
    if not isinstance(width, int) or not isinstance(height, int) or height <= 0:
        return False
    if not 1.7 <= width / height <= 1.8:
        return False
    if provider == "gpt_image_2_native":
        # Must cover the largest variant on BOTH axes, else render_family would
        # upscale it. A bare long-edge test let 1536-wide sources through to a
        # 1600-wide encode (GLP-211).
        return width >= MAX_VARIANT_WIDTH and height >= MAX_VARIANT_HEIGHT
    return provider == "higgsfield" and width >= SOURCE_MIN_WIDTH and height >= SOURCE_MIN_HEIGHT


def validate_source(path: Path, provider: str = "higgsfield") -> dict[str, Any]:
    from PIL import Image  # local import: a missing Pillow must degrade, not crash the import

    if not path.is_file() or path.stat().st_size < MIN_SOURCE_BYTES:
        raise ImageLaneError("source_too_small")
    with Image.open(path) as image:
        width, height = image.size
    if not source_dimensions_valid(width, height, provider):
        raise ImageLaneError(f"source_dimensions_invalid:{width}x{height}")
    return {"width": width, "height": height, "bytes": path.stat().st_size, "sha256": sha256_file(path)}


# ---------------------------------------------------------------------------
# mechanical variant selection
# ---------------------------------------------------------------------------

def candidate_score(path: Path) -> dict[str, float]:
    """Deterministic quality proxy for choosing between renders.

    ``detail`` is mean edge energy on a downscale: a flat, washed-out or
    half-empty render scores low, a crisp layered composition scores high.
    ``efficiency`` is how cheaply the render encodes at the LCP variant - a
    noisy render that blows the AVIF budget scores low. Score multiplies the
    two, so the winner is the sharpest image that still ships light.
    """
    from PIL import Image, ImageFilter, ImageStat

    with Image.open(path) as opened:
        image = opened.convert("RGB")
        probe = image.resize((640, 360), Image.Resampling.LANCZOS)
        detail = float(ImageStat.Stat(probe.convert("L").filter(ImageFilter.FIND_EDGES)).mean[0])
        with tempfile.NamedTemporaryFile(suffix=".avif", delete=False) as handle:
            temp = Path(handle.name)
        try:
            image.resize((1600, 900), Image.Resampling.LANCZOS).save(temp, format="AVIF", quality=70)
            lcp_bytes = temp.stat().st_size
        finally:
            temp.unlink(missing_ok=True)
    budget = PERF_BUDGET_BYTES["-1600.avif"]
    efficiency = min(1.0, budget / lcp_bytes) if lcp_bytes else 0.0
    return {"detail": round(detail, 4), "lcp_avif_bytes": float(lcp_bytes),
            "efficiency": round(efficiency, 4), "score": round(detail * efficiency, 4)}


def select_candidate(scored: list[dict[str, Any]]) -> dict[str, Any]:
    if not scored:
        raise ImageLaneError("no_candidate_rendered")
    return sorted(scored, key=lambda row: (-row["metrics"]["score"], row["sha256"]))[0]


# ---------------------------------------------------------------------------
# responsive family encoding, inside the perf budget
# ---------------------------------------------------------------------------

def render_family(source: Path, out_dir: Path, visual_key: str) -> list[dict[str, Any]]:
    """Encode the exact seven-file family, each variant inside its byte cap."""
    from PIL import Image, ImageOps

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[dict[str, Any]] = []
    try:
        with Image.open(source) as opened:
            base = opened.convert("RGB")
            if base.width < MAX_VARIANT_WIDTH or base.height < MAX_VARIANT_HEIGHT:
                # Fail loudly rather than silently upscaling; the caller degrades
                # to the stock hero, which is honest about what it is.
                raise ImageLaneError(
                    f"source_smaller_than_variant:{visual_key}:"
                    f"{base.width}x{base.height}<{MAX_VARIANT_WIDTH}x{MAX_VARIANT_HEIGHT}")
            for width, height, kind, suffix, ladder in VARIANT_SPECS:
                destination = out_dir / f"{visual_key}{suffix}"
                cap = PERF_BUDGET_BYTES[suffix]
                fitted = ImageOps.fit(base, (width, height), method=Image.Resampling.LANCZOS,
                                      centering=(0.5, 0.5))
                chosen: dict[str, Any] | None = None
                for quality in ladder:
                    temp = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
                    if kind == "AVIF":
                        fitted.save(temp, format="AVIF", quality=quality)
                    elif kind == "WEBP":
                        fitted.save(temp, format="WEBP", quality=quality, method=6)
                    elif kind == "JPEG":
                        fitted.save(temp, format="JPEG", quality=quality, optimize=True,
                                    progressive=True, subsampling=0)
                    else:  # pragma: no cover - VARIANT_SPECS is closed
                        raise ImageLaneError(f"encoder_invalid:{kind}")
                    size = temp.stat().st_size
                    if size <= cap:
                        os.replace(temp, destination)
                        chosen = {"quality": quality, "bytes": size}
                        break
                    temp.unlink(missing_ok=True)
                if chosen is None:
                    raise ImageLaneError(f"perf_budget_exceeded:{suffix}")
                if chosen["bytes"] < MIN_VARIANT_BYTES[suffix]:
                    raise ImageLaneError(f"variant_degenerate:{suffix}")
                with Image.open(destination) as check:
                    if check.size != (width, height):
                        raise ImageLaneError(f"variant_dimensions_invalid:{suffix}")
                written.append({"name": destination.name, "suffix": suffix, "width": width,
                                "height": height, "encoder": kind, "quality": chosen["quality"],
                                "bytes": chosen["bytes"], "budget_bytes": cap,
                                "floor_bytes": MIN_VARIANT_BYTES[suffix],
                                "sha256": sha256_file(destination)})
    except Exception:
        for row in written:
            (out_dir / row["name"]).unlink(missing_ok=True)
        raise
    if len(written) != len(IMAGE_SUFFIXES):
        raise ImageLaneError("family_incomplete")
    return written


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def ledger_path() -> Path:
    return cache_root() / "article-image-events.jsonl"


def health_path() -> Path:
    return cache_root() / "article-image-health.json"


def record_event(receipt: dict[str, Any]) -> None:
    """Append one lane event and refresh the health file (best effort)."""
    try:
        row = {"at_utc": receipt.get("prepared_at_utc") or utc_now(),
               "visual_key": receipt.get("visual_key"), "status": receipt.get("status"),
               "reason": receipt.get("reason"), "route": receipt.get("route"),
               "credits_spent": receipt.get("credits_spent", 0),
               "provider": receipt.get("provider"),
               "provider_chain_attempted": receipt.get("provider_chain_attempted", []),
               "cache_hit": receipt.get("cache_hit", False)}
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        history = read_events(limit=25)
        streak = 0
        for event in reversed(history):
            if event.get("status") == STATUS_FALLBACK:
                streak += 1
            else:
                break
        atomic_json(health_path(), {
            "schema": "glp-article-image-health-v1",
            "updated_at_utc": utc_now(),
            "last_status": row["status"],
            "last_reason": row["reason"],
            "last_visual_key": row["visual_key"],
            "provider": row["provider"],
            "provider_chain_attempted": row["provider_chain_attempted"],
            "consecutive_fallbacks": streak,
            "degraded": streak > 0,
        })
    except Exception as error:  # never let telemetry break a publish
        warn(f"ledger_write_failed:{type(error).__name__}:{error}")


def read_events(limit: int = 50) -> list[dict[str, Any]]:
    path = ledger_path()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


# ---------------------------------------------------------------------------
# the two build-path entry points
# ---------------------------------------------------------------------------

def _fallback_receipt(visual_key: str, subject: dict[str, str] | None, reason: str,
                      **extra: Any) -> dict[str, Any]:
    warn(f"degraded_to_stock_hero:{visual_key}:{reason}")
    return {"schema": SCHEMA, "status": STATUS_FALLBACK, "reason": reason,
            "visual_key": visual_key, "route": (subject or {}).get("route", ""),
            "h1": (subject or {}).get("h1", ""), "alt": None, "staged_dir": None,
            "files": [], "credits_spent": 0, "cache_hit": False,
            "prepared_at_utc": utc_now(), **extra}


def provider_order() -> list[str]:
    values = [p.strip() for p in _env("GLP_ARTICLE_IMAGE_PROVIDERS", "gpt_image_2_native").split(",") if p.strip()]
    # Historical Higgsfield receipts remain readable, but the production
    # environment cannot authorize a credit-spending render or fallback.
    if values != ["gpt_image_2_native"]:
        raise ImageLaneError("provider_chain_invalid")
    return values


def prepare_hero(*, article, visual_key, base_release_root=None, generator=None, cache=None, now=None):
    """GPT Image 2.5 via Codex → provenance-labelled stock hero; never raises.

    Higgsfield left the article lane on 2026-09-17 (operator receipt
    OPERATOR-DECISION-20260917-gpt-image-2-5-via-codex-primary.json); its credit
    balance can no longer gate an article publication.
    """
    attempts = []
    started = time.monotonic()
    try:
        providers = ["higgsfield"] if generator is not None else provider_order()
        if not generation_enabled():
            raise ImageLaneError("generation_disabled")
        result = None
        for provider in providers:
            remaining = lane_budget_seconds() - (time.monotonic() - started)
            if remaining <= 0:
                attempts.append({"provider": provider, "status": "NOT_ATTEMPTED", "reason": "lane_budget_exhausted"})
                break
            render = generator
            if provider == "gpt_image_2_native":
                import gpt_image_2_native
                render = gpt_image_2_native.candidate
            try:
                result = _prepare_hero(article=article, visual_key=visual_key,
                    base_release_root=base_release_root, generator=render, cache=cache,
                    now=now, provider=provider, budget_override=int(remaining))
            except ImageLaneError as error:
                # One provider's refusal steps the chain down to the next lane and
                # finally to the receipted stock hero. Before 2026-09-17 it escaped
                # the loop instead, so the attempt ledger lost the provider that
                # actually failed. Decoration never vetoes a publication.
                result = _fallback_receipt(
                    visual_key,
                    {"route": (article or {}).get("canonical_path", ""),
                     "h1": (article or {}).get("h1", "")},
                    f"{type(error).__name__}:{error}", provider="stock")
            attempts.append({"provider": provider, "status": result["status"], "reason": result.get("reason")})
            if result["status"] == STATUS_GENERATED:
                break
        if result is None:
            result = _fallback_receipt(visual_key, None, "provider_chain_exhausted", provider="stock")
        if result["status"] != STATUS_GENERATED:
            result.update(provider="stock", reason=";".join(str(a.get("reason")) for a in attempts))
        result["provider_chain_attempted"] = attempts
        result["elapsed_s"] = round(time.monotonic()-started, 1)
        if result["status"] == STATUS_GENERATED:
            atomic_json(Path(result["staged_dir"]).parent / "receipt.json", result)
        record_event(result)
        return result
    except Exception as error:
        result = _fallback_receipt(visual_key, None, f"{type(error).__name__}:{error}", provider="stock", provider_chain_attempted=attempts)
        record_event(result)
        return result


def _prepare_hero(
    *,
    article: dict[str, Any],
    visual_key: str,
    base_release_root: Path | None = None,
    generator: Callable[..., dict[str, Any]] | None = None,
    cache: Path | None = None,
    now: str | None = None,
    provider: str = "higgsfield",
    budget_override: int | None = None,
) -> dict[str, Any]:
    """Generate + stage the article's hero family. NEVER raises.

    Returns a receipt whose ``status`` is GENERATED (files staged, ``alt`` set)
    or FALLBACK (nothing staged; the caller copies the stock hero).
    """
    subject: dict[str, str] | None = None
    started = time.monotonic()
    # GLP-210: a render can be CHARGED and a later step (encoding, model
    # verification, selection) can still fail. Spend is tracked out here so the
    # fallback receipt reports what was actually spent instead of a flat 0.
    credits_spent = 0
    try:
        subject = article_subject(article)
        if not re.fullmatch(r"guide-[a-z0-9]+(?:-[a-z0-9]+)*", visual_key):
            raise ImageLaneError(f"visual_key_invalid:{visual_key}")
        if not generation_enabled():
            raise ImageLaneError("generation_disabled")
        prompt = hero_prompt(subject)
        prompt_sha = sha256_text(prompt)
        root = (cache or cache_root()) / visual_key / provider / prompt_sha[:16]
        root.mkdir(parents=True, exist_ok=True)
        render = generator or higgsfield_candidate
        cli = cli_path()
        # Style reference is OPT-IN. The CLI auto-uploads a local reference file
        # to S3 and that upload returned SignatureDoesNotMatch on 2026-08-20, so
        # a reference by default would degrade every article for an upload bug.
        # The house art direction is fully carried by the prompt; set
        # GLP_ARTICLE_IMAGE_REFERENCE=1 once the upload path is proven again.
        reference = None
        if base_release_root is not None and _env("GLP_ARTICLE_IMAGE_REFERENCE") == "1":
            candidate_reference = Path(base_release_root) / f"public/assets/images/{FALLBACK_KEY}-1600.jpg"
            reference = candidate_reference if candidate_reference.is_file() else None

        wanted = variant_count()
        budget_s = budget_override if budget_override is not None else lane_budget_seconds()
        scored: list[dict[str, Any]] = []
        cache_hit = True
        checked_balance = False
        for index in range(wanted):
            path = root / f"candidate-{index}.src"
            if not path.is_file():
                if time.monotonic() - started > budget_s:
                    warn(f"lane_budget_exhausted_after_{len(scored)}_candidates:{visual_key}")
                    break
                if provider == "higgsfield" and generator is None and not checked_balance:
                    checked_balance = True
                    balance = available_credits(cli)
                    if balance < credit_reserve() + wanted * CREDITS_PER_RENDER:
                        raise ImageLaneError(f"insufficient_credits:{balance:.0f}")
                remaining = int(budget_s - (time.monotonic() - started))
                provenance = render(prompt, reference, path, cli=cli, timeout=max(1, min(720, remaining)))
                atomic_json(path.with_suffix(".provenance.json"), {"provider": provider, "model": (provenance or {}).get("model") if provider == "gpt_image_2_native" else PROVIDER_MODELS[provider], "account_label": provenance.get("account_label") if provenance else None})
                credits_spent += PROVIDER_CREDITS[provider]
                cache_hit = False
            if provider == "gpt_image_2_native":
                provenance = json.loads(path.with_suffix(".provenance.json").read_text())
                if provenance.get("model") != NATIVE_MODEL:
                    raise ImageLaneError("native_model_unverified")
            try:
                metrics_source = validate_source(path, provider=provider)
                scored.append({"path": str(path), "index": index, "sha256": metrics_source["sha256"],
                               "source": metrics_source, "metrics": candidate_score(path)})
            except Exception as error:
                warn(f"candidate_rejected:{visual_key}:{index}:{type(error).__name__}:{error}")
                path.unlink(missing_ok=True)
        winner = select_candidate(scored)

        staged = root / "family"
        shutil.rmtree(staged, ignore_errors=True)
        files = render_family(Path(winner["path"]), staged, visual_key)
        alt = hero_alt_text(subject)
        receipt = {
            "schema": SCHEMA, "status": STATUS_GENERATED, "reason": None,
            "visual_key": visual_key, "route": subject["route"], "h1": subject["h1"],
            "motif": subject["motif"], "alt": alt, "model": PROVIDER_MODELS[provider], "provider": provider,
            "prompt_sha256": prompt_sha, "staged_dir": str(staged), "files": files,
            "selected_candidate": {"index": winner["index"], "sha256": winner["sha256"],
                                   "metrics": winner["metrics"], "source": winner["source"]},
            "candidates": [{"index": row["index"], "sha256": row["sha256"], "metrics": row["metrics"]}
                           for row in scored],
            "credits_spent": credits_spent, "cache_hit": cache_hit,
            "reference_used": str(reference) if reference else None,
            "elapsed_s": round(time.monotonic() - started, 1),
            "prepared_at_utc": now or utc_now(),
        }
        atomic_json(root / "receipt.json", receipt)
        return receipt
    except Exception as error:  # decoration must never gate a publish
        receipt = _fallback_receipt(visual_key, subject,
                                    f"{type(error).__name__}:{error}",
                                    credits_spent=credits_spent,
                                    elapsed_s=round(time.monotonic() - started, 1))
        return receipt


def install_hero(
    receipt: dict[str, Any] | None,
    *,
    image_dir: Path,
    visual_key: str,
    suffixes: Iterable[str] = IMAGE_SUFFIXES,
    fallback_key: str = FALLBACK_KEY,
) -> dict[str, Any]:
    """Place the article's family in the built tree.

    Generated staging first; the stock-hero copy is the deterministic fallback.
    Raises ``route_visual_family_invalid`` only when the base tree cannot even
    supply the stock family - the pre-existing broken-base failure.
    """
    suffixes = tuple(suffixes)
    installed: list[str] = []
    if receipt and receipt.get("status") == STATUS_GENERATED and receipt.get("staged_dir"):
        staged = Path(receipt["staged_dir"])
        try:
            sources = [staged / f"{visual_key}{suffix}" for suffix in suffixes]
            if any(not path.is_file() for path in sources):
                raise ImageLaneError("staged_family_incomplete")
            for source, suffix in zip(sources, suffixes):
                target = image_dir / f"{visual_key}{suffix}"
                if os.path.lexists(target):
                    raise ImageLaneError(f"target_exists:{suffix}")
                shutil.copy2(source, target)
                installed.append(target.name)
            return {"mode": "generated", "files": installed,
                    "bytes": sum((image_dir / name).stat().st_size for name in installed)}
        except Exception as error:
            for name in installed:
                (image_dir / name).unlink(missing_ok=True)
            installed = []
            warn(f"staged_install_failed:{visual_key}:{type(error).__name__}:{error}")

    for suffix in suffixes:
        source = image_dir / f"{fallback_key}{suffix}"
        target = image_dir / f"{visual_key}{suffix}"
        if not source.is_file() or os.path.lexists(target):
            for name in installed:
                (image_dir / name).unlink(missing_ok=True)
            raise RuntimeError("route_visual_family_invalid")
        shutil.copy2(source, target)
        installed.append(target.name)
    return {"mode": "fallback", "files": installed,
            "bytes": sum((image_dir / name).stat().st_size for name in installed)}


def apply_article_image_metadata(article: dict[str, Any], receipt: dict[str, Any] | None) -> bool:
    """Bind the generated hero's alt text to the article row build-staging reads.

    Only a GENERATED hero writes metadata: on fallback the article keeps the
    pre-existing behaviour (build-staging derives "Guide overview for <h1>"),
    so a degraded run is byte-identical to today's output.
    """
    if not receipt or receipt.get("status") != STATUS_GENERATED or not receipt.get("alt"):
        return False
    hero = [row for row in receipt.get("files") or [] if row.get("suffix") == "-1600.jpg"]
    width, height = (hero[0]["width"], hero[0]["height"]) if hero else (1600, 900)
    article["images"] = [{
        "ai_generated": True,
        "alt": receipt["alt"],
        "filename": f"{receipt['visual_key']}-1600.jpg",
        "generator": f"{receipt.get('provider') or 'higgsfield'}:{receipt.get('model') or MODEL}",
        "height": height,
        "loading": "eager",
        "position": "hero",
        "width": width,
    }]
    return True


# ---------------------------------------------------------------------------
# sensor entry point
# ---------------------------------------------------------------------------

def health() -> dict[str, Any]:
    path = health_path()
    if not path.is_file():
        return {"schema": "glp-article-image-health-v1", "degraded": False,
                "consecutive_fallbacks": 0, "last_status": None,
                "note": "no article has been built through the image lane yet"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema": "glp-article-image-health-v1", "degraded": True,
                "consecutive_fallbacks": -1, "last_status": None,
                "note": "health file unreadable"}


def main() -> int:
    parser = argparse.ArgumentParser(description="GLP article hero image lane")
    parser.add_argument("--health", action="store_true", help="print lane health; exit 2 when degraded")
    parser.add_argument("--events", type=int, default=0, help="print the last N lane events")
    args = parser.parse_args()
    if args.events:
        for row in read_events(limit=args.events):
            print(json.dumps(row, sort_keys=True))
    state = health()
    if args.health or not args.events:
        print(json.dumps(state, sort_keys=True))
    return 2 if state.get("degraded") else 0


if __name__ == "__main__":
    raise SystemExit(main())
