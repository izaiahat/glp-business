"""Regression for the 2026-09-11..16 publication veto (GLP-73 / K10-023-B).

For six consecutive days the daily article carrier died inside the *stock-hero
fallback*: `article_image_release_contract.configure()` patched the candidate's
visual-QA test through exact-string anchors, the QA file's `receipt.model`
assertion gained the native provider on 2026-09-12, the anchor stopped matching
and `patch_once` raised `image_contract_anchor_invalid`.  Decoration vetoed
publication, which it must never do.

The red arm below replays the pre-fix code path verbatim against the real
candidate bytes; the green arm runs the current contract over a scratch copy of
the same candidate.  Both arms read the live candidate read-only.
"""
import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

MACHINE = Path(__file__).resolve().parents[1]
BUSINESS = MACHINE.parents[1]
CANDIDATE = BUSINESS / "site-system/flagship-v2/candidates/glp34-provider-families-20260912"
QA_RELATIVE = "tests/article-imagery-pipeline.test.mjs"
GENERATOR_RELATIVE = "scripts/generate-article-imagery.py"

# The exact anchor the pre-fix contract carried, and the exact error the carrier
# recorded on 2026-09-14 and 2026-09-16.
HISTORICAL_ANCHOR = 'assert.equal(receipt.model, "gpt_image_2", generated.route);'
HISTORICAL_SIGNATURE = "image_contract_anchor_invalid:" + HISTORICAL_ANCHOR

sys.path.insert(0, str(MACHINE / "scripts"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract = _load("glp73_contract", MACHINE / "scripts/article_image_release_contract.py")
images = _load("glp73_images", MACHINE / "scripts/article_images.py")


def _scratch_target(tmp_path):
    """A writable copy of exactly the two candidate files configure() patches."""
    target = tmp_path / "candidate"
    for relative in (QA_RELATIVE, GENERATOR_RELATIVE):
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CANDIDATE / relative, destination)
    return target


@pytest.fixture(scope="module")
def qa_text():
    if not (CANDIDATE / QA_RELATIVE).is_file():
        pytest.skip(f"candidate visual-QA file absent: {CANDIDATE / QA_RELATIVE}")
    return (CANDIDATE / QA_RELATIVE).read_text(encoding="utf-8")


def test_image_contract_red_arm_reproduces_the_publication_veto(qa_text):
    """The pre-fix literal anchor still raises the production signature."""
    assert HISTORICAL_ANCHOR not in qa_text, (
        "the historical anchor reappeared; this regression no longer reproduces"
    )
    with pytest.raises(RuntimeError) as failure:
        contract.patch_once(qa_text, HISTORICAL_ANCHOR, "unused-replacement")
    assert str(failure.value) == HISTORICAL_SIGNATURE


def test_image_contract_green_arm_configures_a_stock_hero_release(tmp_path):
    """configure() completes over the CURRENT candidate; no anchor is invalid."""
    target = _scratch_target(tmp_path)
    result = contract.configure(target)
    assert result["status"] == "PASS_MIXED_PROVIDER_CONTRACT"

    patched = (target / QA_RELATIVE).read_text(encoding="utf-8")
    assert 'generated.provider === "stock" ? "FALLBACK"' in patched
    assert 'assert.equal(receipt.model, generated.provider === "stock" ? "stock"' in patched
    assert '"stock"].includes(generated.provider' in patched
    # The provider assertion must appear exactly once, never duplicated.
    assert patched.count(
        'assert.equal(receipt.provider || "higgsfield", '
        'generated.provider || "higgsfield", generated.route);'
    ) == 1


def test_image_contract_configure_is_idempotent(tmp_path):
    """A second pass must not raise and must not duplicate the release helper."""
    target = _scratch_target(tmp_path)
    contract.configure(target)
    first = (target / QA_RELATIVE).read_text(encoding="utf-8")
    generator_first = (target / GENERATOR_RELATIVE).read_text(encoding="utf-8")
    contract.configure(target)
    assert (target / QA_RELATIVE).read_text(encoding="utf-8") == first
    assert (target / GENERATOR_RELATIVE).read_text(encoding="utf-8") == generator_first
    assert generator_first.count("def validate_release_record(") == 1


def test_image_contract_native_model_id_is_one_truthful_string(qa_text):
    """Generator-returned id == verified id == QA expectation == contract map."""
    expected = re.search(
        r'assert\.equal\(receipt\.model, [^\n]*?generated\.provider === "gpt_image_2_native" '
        r'\? "([^"]+)"',
        qa_text,
    )
    assert expected, "candidate QA no longer states a native model expectation"
    assert expected.group(1) == images.NATIVE_MODEL == contract.NATIVE_MODEL == "gpt-image-2"
    assert images.PROVIDER_MODELS["gpt_image_2_native"] == images.NATIVE_MODEL
    # The renderer must REQUEST exactly the id we verify. gpt_image_2_native.MODEL
    # also seeds the per-render work-directory identity, so a mismatch orphans every
    # prior attempt directory and the reconciliation guard that protects it.
    native = _load("glp73_native", MACHINE / "scripts/gpt_image_2_native.py")
    if not native.codex_binaries():
        pytest.skip("codex is not installed on this host")
    assert native.resolve_model() == images.NATIVE_MODEL


def test_image_contract_model_change_reaches_an_already_patched_target(tmp_path):
    """A future NATIVE_MODEL change must not be silently dropped.

    The first idempotency marker stopped at the "stock" branch, so a second
    configure() over an already-patched tree returned early and left a STALE model
    id in the visual-QA assertion while every other consumer moved on - re-opening
    the veto class this repair closed.
    """
    target = _scratch_target(tmp_path)
    contract.configure(target)
    original = contract.NATIVE_MODEL
    try:
        contract.NATIVE_MODEL = "gpt-image-9-future"
        contract.configure(target)
    finally:
        contract.NATIVE_MODEL = original
    patched = (target / QA_RELATIVE).read_text(encoding="utf-8")
    model_assertion = next(
        line for line in patched.splitlines() if "assert.equal(receipt.model," in line
    )
    assert "gpt-image-9-future" in model_assertion


def test_image_contract_article_lane_is_codex_only(monkeypatch):
    """Operator 2026-09-17: GPT Image 2.5 via Codex is THE article-image path."""
    monkeypatch.delenv("GLP_ARTICLE_IMAGE_PROVIDERS", raising=False)
    assert images.provider_order() == ["gpt_image_2_native"]
    balance_gate = re.search(
        r"if provider == \"higgsfield\" and generator is None and not checked_balance:",
        (MACHINE / "scripts/article_images.py").read_text(encoding="utf-8"),
    )
    assert balance_gate, "a Higgsfield credit balance must never gate an article publish"


# ---------------------------------------------------------------------------
# Same defect class, second occurrence: the builder's pagination-capture anchor.
# With the image-contract veto cleared, the 2026-09-17T02:53Z carrier run reached
# build_opus_article_release.rebind_pagination_capture_fixture and aborted with
# `capture_fixture_routes_anchor_invalid` because the sealed base's
# tests/price-watch-email.test.mjs had gained a trailing line comment on its
# "/insights/page/5/" entry. Receipt:
# reports/glp-clinic-scout/seo-machine/article-carrier-failures/20260917T025329.221187Z-c0c84014815987b8.json
# ---------------------------------------------------------------------------

CURRENT_BASE_POINTER = (
    BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
)
HISTORICAL_CAPTURE_PATTERN = (
    r'(  const routes = \[\n)((?:    "/insights/page/[1-9][0-9]*/",\n)*)'
    r'(    "/matches/customized/",)'
)

builder = _load("glp73_builder", MACHINE / "scripts/build_opus_article_release.py")


def _current_base_release():
    import json as _json

    if not CURRENT_BASE_POINTER.is_file():
        pytest.skip("no accepted content base pointer")
    release = Path(_json.loads(CURRENT_BASE_POINTER.read_text())["release_root"])
    fixture = release / "tests/price-watch-email.test.mjs"
    if not fixture.is_file():
        pytest.skip(f"accepted base has no price-watch fixture: {fixture}")
    return release, fixture


def test_carrier_fixture_anchor_red_arm_reproduces_the_capture_veto():
    """The pre-fix comment-intolerant anchor matches zero times on the live base."""
    _release, fixture = _current_base_release()
    assert len(re.findall(HISTORICAL_CAPTURE_PATTERN, fixture.read_text())) == 0


def test_carrier_fixture_anchor_green_arm_rebinds_against_the_live_base():
    """rebind_pagination_capture_fixture completes over the accepted base."""
    release, fixture = _current_base_release()
    job = {
        "base_release_root": str(release),
        "allowed_public_added": ["insights/page/6/index.html"],
    }
    rebound = builder.rebind_pagination_capture_fixture(fixture.read_text(), job)
    assert '"/matches/customized/",' in rebound
    assert re.search(r'"/insights/page/[1-9][0-9]*/",', rebound)
    # Idempotent: rebinding the rebound source must not raise or drift.
    assert builder.rebind_pagination_capture_fixture(rebound, job) == rebound


def test_carrier_fixture_anchor_tolerates_trailing_comments_and_indent():
    """A trailing line comment on a staged entry must never veto publication."""
    source = (
        '  const routes = [\n'
        '    "/insights/page/5/", // the exact comment that broke 2026-09-17\n'
        '    "/matches/customized/",\n'
        '  ];\n'
    )
    job = {"base_release_root": None, "allowed_public_added": ["insights/page/6/index.html"]}
    rebound = builder.rebind_pagination_capture_fixture(source, job)
    assert '"/insights/page/5/",\n' in rebound
    assert '"/insights/page/6/",\n' in rebound


# ---------------------------------------------------------------------------
# Same defect class, third occurrence: carrier_fixture_contracts.once() knows only
# the predecessor and its own byte-exact successor, so the accepted base's EVOLVED
# material-date wiring (an extra inheritedDisclosureDates term) raised
# `material_resolver_call_anchor_invalid` on the 2026-09-17T02:56Z run. Receipt:
# .../article-buffer-work/3a580aa2…/child-failures/buffer-3a580aa2b147e241-r2-build.stderr.txt
# ---------------------------------------------------------------------------

fixtures = _load("glp73_fixtures", MACHINE / "scripts/carrier_fixture_contracts.py")

UNEVOLVED_CALL = (
    '    const ownedDate = resolveCatalogDate(route, articles.get(route), '
    'declaredDateModified(html), resolved);\n'
    '    const { lastmod, action } = resolveMaterialDate(route, materialDates.get(route), '
    'ownedDate, releaseDate);'
)


def _accepted_resolver():
    release, _fixture = _current_base_release()
    resolver = release / "scripts/postbuild-aeo-surface.mjs"
    if not resolver.is_file():
        pytest.skip(f"accepted base has no aeo surface script: {resolver}")
    return release, resolver


def test_carrier_fixture_anchor_accepts_the_evolved_material_resolver():
    """The accepted base's evolved wiring is recognised, not re-patched."""
    release, resolver = _accepted_resolver()
    source = resolver.read_text(encoding="utf-8")
    assert fixtures.material_resolver_call_satisfied(source)
    binding = fixtures.material_binding(release)
    assert fixtures.patch_material_date_resolver(source, binding) == source


def test_carrier_fixture_anchor_still_patches_an_unwired_resolver():
    """Negative control: an unwired source is patched, never silently accepted."""
    release, resolver = _accepted_resolver()
    source = resolver.read_text(encoding="utf-8")
    evolved = re.search(
        r'    const materialDate = [^\n]*\n'
        r'    const ownedDate = resolveCatalogDate\([^\n]*\n'
        r'    const \{ lastmod, action \} = resolveMaterialDate\([^\n]*',
        source,
    )
    assert evolved, "accepted base no longer carries the material-date wiring"
    unwired = source.replace(evolved.group(0), UNEVOLVED_CALL, 1)
    assert unwired != source
    assert not fixtures.material_resolver_call_satisfied(unwired)
    rewired = fixtures.patch_material_date_resolver(unwired, fixtures.material_binding(release))
    assert "resolveMaterialDate(route, materialDate, ownedDate, releaseDate)" in rewired
    assert fixtures.material_resolver_call_satisfied(rewired)


# ---------------------------------------------------------------------------
# The frozen-value-required-fresh bug class (memory:
# frozen-value-required-fresh-bug-class). therapeutic_taxonomy.verified_as_of is
# frozen into every sealed base and NOTHING registered can advance it there, so the
# 31-day guard stopped every publication on 2026-09-17. Fix shape: keep the frozen
# value, take the freshness clock from the authenticated live re-verification beside
# it. Witness: .../child-failures/buffer-3a580aa2b147e241-r2-build.stderr.txt
# (therapeutic_taxonomy_evidence_stale:bodybuilding-com-glp-1-longevity-review:32.12)
# ---------------------------------------------------------------------------

TAXONOMY_FIVE = (
    "bodybuilding-com-glp-1-longevity-review",
    "coreage-rx-glp-1-1632",
    "pallas-health-review",
    "rxspan-md-telehealth-program-review",
    "skinnyrx-1-glp-weight-loss-provider-review",
)


def test_taxonomy_freshness_binding_authenticates_the_five_frozen_rows():
    """Every admitted stamp is bound to a sha256-verified manifest under reports/."""
    import hashlib as _hashlib

    release, _fixture = _current_base_release()
    binding = fixtures.taxonomy_reverification_binding(release)
    assert sorted(binding["stamps"]) == sorted(TAXONOMY_FIVE)
    frozen = json.loads(
        (release / "data/program-registry.json").read_text()
    )["therapeutic_taxonomy"]["programs"]
    for row in binding["evidence"]:
        manifest = BUSINESS / row["manifest"]
        assert manifest.is_file() and not manifest.is_symlink()
        assert _hashlib.sha256(manifest.read_bytes()).hexdigest() == row["manifest_sha256"]
        payload = json.loads(manifest.read_text())
        assert payload["program_id"] == row["program_id"]
        assert payload["verified_as_of"] == row["reverified_as_of"]
        # The re-verification must cover THIS release's own frozen claims.
        for field in fixtures.TAXONOMY_TAG_FIELDS:
            assert sorted(payload["tags_unchanged"].get(field) or []) == sorted(
                frozen[row["program_id"]].get(field) or []
            )
        assert row["reverified_as_of"] > row["frozen_verified_as_of"]


def test_taxonomy_freshness_binding_refuses_unauthenticated_evidence(tmp_path):
    """Negative controls: a tampered manifest, a wrong hash and mismatched tags."""
    release, _fixture = _current_base_release()
    business = tmp_path / "business"
    registry_dir = business / "site-system/flagship-v2/candidates/probe/data"
    registry_dir.mkdir(parents=True)
    manifest_dir = business / "reports/probe"
    manifest_dir.mkdir(parents=True)
    frozen = json.loads(
        (release / "data/program-registry.json").read_text()
    )["therapeutic_taxonomy"]["programs"]
    pid = TAXONOMY_FIVE[0]
    manifest = {
        "program_id": pid,
        "verified_as_of": "2026-09-16",
        "tags_unchanged": {f: frozen[pid].get(f) or [] for f in fixtures.TAXONOMY_TAG_FIELDS},
    }
    manifest_path = manifest_dir / "probe.json"
    manifest_path.write_text(json.dumps(manifest))
    import hashlib as _hashlib

    good = _hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    def write_registry(sha, tags=None):
        payload = dict(manifest)
        if tags is not None:
            payload["tags_unchanged"] = tags
            manifest_path.write_text(json.dumps(payload))
        (registry_dir / "program-registry.json").write_text(json.dumps({
            "therapeutic_taxonomy": {
                "authorities": {"probe": {"path": "reports/probe/probe.json", "sha256": sha}},
                "programs": {pid: {"evidence_bindings": [
                    {"authority": "probe", "verified_as_of": "2026-09-16"}]}},
            }
        }))

    write_registry("0" * 64)
    assert fixtures.taxonomy_reverification_binding(release, business)["stamps"] == {}

    write_registry(good)
    assert fixtures.taxonomy_reverification_binding(release, business)["stamps"] == {pid: "2026-09-16"}

    mismatched = {f: [] for f in fixtures.TAXONOMY_TAG_FIELDS}
    write_registry(_hashlib.sha256(json.dumps({**manifest, "tags_unchanged": mismatched}).encode()).hexdigest(), mismatched)
    assert fixtures.taxonomy_reverification_binding(release, business)["stamps"] == {}


def test_taxonomy_freshness_clock_patch_is_a_noop_without_evidence():
    """An empty binding must leave the guard behaviourally unchanged, and the patch
    must be idempotent."""
    release, _fixture = _current_base_release()
    source = (release / "src/therapeutic-taxonomy.mjs").read_text()
    noop = fixtures.patch_taxonomy_freshness_clock(source, {"stamps": {}})
    assert "Object.freeze({})" in noop
    binding = fixtures.taxonomy_reverification_binding(release)
    patched = fixtures.patch_taxonomy_freshness_clock(source, binding)
    assert fixtures.patch_taxonomy_freshness_clock(patched, binding) == patched
    for pid in TAXONOMY_FIVE:
        assert f'"{pid}": "2026-09-16"' in patched
    # freshness_max_days is never widened and the frozen stamp still renders.
    assert "freshness_max_days" not in patched.split("CARRIER_TAXONOMY_REVERIFIED")[1][:400]
    assert "const age = ageDays(freshnessAsOf, asOf);" in patched


def test_price_tracker_freshness_clock_patch_is_anchored_and_idempotent():
    """Fourth instance of the frozen-value-required-fresh class: a sealed row's
    checked_at cannot be advanced in an accepted base, so the 45-day price gate
    stopped every publication. The displayed checked_at and quote stay sealed."""
    release, _fixture = _current_base_release()
    tracker = release / "src/price-tracker.mjs"
    if not tracker.is_file():
        pytest.skip(f"accepted base has no price tracker: {tracker}")
    source = tracker.read_text(encoding="utf-8")
    patched = fixtures.patch_price_tracker_freshness_clock(source)
    assert fixtures.patch_price_tracker_freshness_clock(patched) == patched
    # MAX_AGE_MS is never widened and the sealed row is never rewritten.
    assert "const MAX_AGE_MS = 45 * 86_400_000;" in patched
    assert "record.checked_at" in patched
    # The clock may only be borrowed from a re-capture of the SAME quote and url.
    assert 'evidenceClock.quoteSha === record.quote_sha256' in patched
    assert 'evidenceClock.url === record.source_url' in patched
    assert 'if (nowMs - freshnessMs > MAX_AGE_MS) throw new Error("tracker_freshness_invalid");' in patched


# ---------------------------------------------------------------------------
# Operator 2026-09-17 (addendum_0645Z): "make sure our codex is up to date always
# and that we use the latest image model available." The model id is resolved from
# the installed Codex binary instead of pinned, so a Codex upgrade is picked up with
# no code change. Neither 0.153.4 nor 0.154.0 ships a gpt-image-2.5 id.
# ---------------------------------------------------------------------------


def _native():
    return _load("glp73_native_resolver", MACHINE / "scripts/gpt_image_2_native.py")


def test_codex_image_model_resolver_picks_the_latest_available(tmp_path):
    """Fake binary string sets: 2.5 wins when present, 2 otherwise; aliases lose."""
    native = _native()
    without = tmp_path / "codex-without-25"
    without.write_bytes(
        b"\x00noise gpt-image-1 gpt-image-1.5 gpt-image-1-mini gpt-image-2 "
        b"gpt-image-2-high noise\x00"
    )
    assert native.image_model_ids(str(without)) == ["gpt-image-1", "gpt-image-1.5", "gpt-image-2"]
    assert native.resolve_model(str(without)) == "gpt-image-2"

    with_25 = tmp_path / "codex-with-25"
    with_25.write_bytes(
        b"gpt-image-1 gpt-image-2 gpt-image-2.5 gpt-image-2.5-preview gpt-image-3-mini"
    )
    assert native.resolve_model(str(with_25)) == "gpt-image-2.5"
    # -mini, -high, -preview and every non-versioned alias are excluded, never guessed at.
    assert "gpt-image-2.5-preview" not in native.image_model_ids(str(with_25))
    assert "gpt-image-3-mini" not in native.image_model_ids(str(with_25))

    barren = tmp_path / "codex-barren"
    barren.write_bytes(b"no model ids here")
    with pytest.raises(native.NativeImageError, match="native_image_model_unresolved"):
        native.resolve_model(str(barren))


def test_codex_image_model_resolver_matches_the_installed_binary():
    """The resolved id is what the INSTALLED Codex exposes, and what we verify."""
    native = _native()
    binaries = native.codex_binaries()
    if not binaries:
        pytest.skip("codex is not installed on this host")
    resolved = native.resolve_model()
    assert native.IMAGE_MODEL_EXACT.match(resolved)
    assert resolved == images.NATIVE_MODEL, (
        "the installed Codex now exposes a newer image model than article_images "
        "verifies against; update NATIVE_MODEL and the visual-QA expectation together"
    )
