#!/usr/bin/env python3
"""G6-R1a S6 — generalized incremental affiliate wire-in builder for the GLP release.

Successor to build_glp_affiliate_wirein_incremental.py, which hardcoded a single
program (Measured, Awin 126727) and a set of replace_once() literals that are all
ALREADY APPLIED in the production head. Re-running it raises replace_count:...:0.

What changed, and why it generalizes:

  * Programs come from a --spec file (g6-r1a-wirein-program-specs-v1) listing N
    programs, so a future wave adds rows to JSON instead of editing Python.
  * Every count assertion is bumped by an ANCHORED REGEX that reads the current
    number out of the file and writes current+N. The old builder hardcoded both
    sides ("18" -> "19"), which is why it could only ever run once. Each patch
    still asserts it matched exactly once, so a moved or duplicated assertion is
    a hard failure rather than a silent no-op.
  * Set-membership assertions (activeIds, the calculator exclusion list) get the
    new program ids appended, again with an exactly-once match assertion.

Nothing here is relaxed relative to the Measured build: the same tracking
validation, the same cross-file approval corroboration, and the same
unexpected-source-change guard. A net-new provider may supply one exact,
hash-bound local WebP logo with first-party source provenance; only that file and
the logo manifest may change. The guide renderer may receive the one idempotent
source-marker migration below so a provider becoming approved cannot turn an
existing evidence citation into an unmarked commercial link. No tracking URL is
ever fetched.
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
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BUSINESS = Path("/home/ubuntu/business")
sys.path.insert(0, str(BUSINESS / "scripts"))
from build_glp_affiliate_expansion_13 import validate_tracking  # noqa: E402
import glp_registry_evidence as registry_evidence  # noqa: E402
from site_tree_materializer import (  # noqa: E402
    constructor_provenance,
    copytree_linked,
    detach_for_write,
    remove_materialized_tree,
    tree_content_closure,
)

MISSION = BUSINESS / "reports/glp-clinic-scout/affiliate-machine/g6-r1a-wirein-20260806"
RESEARCH = MISSION / "research"
AWIN_PUBLISHER_ID = "2979387"
KATALYS_PUBLISHER_ID = "13031"
# The publisher identity we expect to find INSIDE the tracking link, per network,
# and the query parameter(s) that carry it. A network absent from this table is
# not wireable by this builder — that is the point of the else-branch below.
#
# 2026-08-20: the table is now DERIVED from `glp_affiliate_link_rails.RAILS`, so
# a rail added there (with its first-party link evidence) is wireable here with
# no code change. The three entries below are asserted unchanged at import time —
# a rails edit can extend this contract but can never silently rewrite the
# publisher identity of a network that is already earning.
from glp_affiliate_link_rails import RAILS as _RAILS  # noqa: E402
from glp_affiliate_link_rails import rail_for as _rail_for  # noqa: E402
from glp_affiliate_link_rails import RailError as _RailError  # noqa: E402
from glp_affiliate_link_rails import spec_allowed_host as _spec_allowed_host  # noqa: E402

PUBLISHER_ID_BY_NETWORK = {
    rail.network: (rail.publisher_id, rail.publisher_params) for rail in _RAILS
}
# Captured MaxBounty links (missed-maxbounty-approval-20260831/tracking-link-29207.json)
# carry the affiliate identity in `a`; do not invent a network-wide constant.
PUBLISHER_ID_BY_NETWORK["MaxBounty"] = (None, ("a",))
for _alias_net, _expected in (
    ("Awin", (AWIN_PUBLISHER_ID, ("awinaffid", "id"))),
    ("Katalys", (KATALYS_PUBLISHER_ID, ("aff_id",))),
    ("Direct affiliate", (None, ("affid",))),
):
    if PUBLISHER_ID_BY_NETWORK.get(_alias_net) != _expected:
        raise RuntimeError(f"publisher_contract_drift:{_alias_net}")

ALLOWED_SOURCE_CHANGES = {
    "data/affiliate-wire-in-approvals.json",
    "data/phase3-page-graph.json",
    "data/program-registry.json",
    "data/provider-fastpath-contract.json",
    "data/provider-logo-sources.json",
    "data/provider-page-contracts.json",
    "data/worker-manifest.staging.json",
    "artifacts/ops/worker-manifest.staging.json",
    "scripts/build-staging.mjs",
    "scripts/generate-article-imagery.py",
    "scripts/validate-provider-pricing-matrix.mjs",
    "scripts/validate-provider-fastpath-plus5.mjs",
    "src/blog-system.mjs",
    "src/cost-calculator.mjs",
    "tests/affiliate-expansion-13.test.mjs",
    "tests/cost-calculator.test.mjs",
    "tests/provider-surfaces-repair.test.mjs",
    "tests/program-registry-coverage.test.mjs",
    "tests/provider-add-merit-binding.test.mjs",
    "tests/provider-fastpath-plus5.test.mjs",
    "tests/wave2-regression.test.mjs",
}
SURFACE_TRANSACTION_OUTPUTS = {
    "data/program-registry.json",
    "data/money-page-specs.json",
    "data/quiz-config.json",
    "data/program-page-eligibility.json",
    "data/affiliate-activation-selection.json",
    "data/cost-calculator.json",
}
SURFACE_INTEGRATION_CHANGES = {"data/program-content-ledger.json"} | SURFACE_TRANSACTION_OUTPUTS
DECLARED_MUTATION_PATHS = ALLOWED_SOURCE_CHANGES | SURFACE_INTEGRATION_CHANGES
MAY_BE_ABSENT_BEFORE_WRITE = {
    "data/worker-manifest.staging.json",
    "artifacts/ops/worker-manifest.staging.json",
    "data/program-content-ledger.json",
    "tests/provider-add-merit-binding.test.mjs",
}
MATERIALIZATION_IGNORE_NAMES = (".wrangler", "node_modules")

FDA_COMPOUNDED_URL = ("https://www.fda.gov/drugs/drug-alerts-and-statements/"
                      "fdas-concerns-unapproved-glp-1-drugs-used-weight-loss")

HOLD_COPY = ("This page contains one active affiliate link. Clinic Scout may earn a "
             "commission at no extra cost to the reader; the link does not change the "
             "page evidence or score.")

FIELD_BINDINGS = {
    "bottom_line": ["pricing_and_inclusions"],
    "cancellation": ["cancellation_and_roles"],
    "eligibility": ["cancellation_and_roles"],
    "pricing": ["pricing_and_inclusions"],
    "regulatory": ["regulatory"],
    "shipping": ["pricing_and_inclusions"],
}

COMPOUNDED_TAIL = ("Compounded drugs are not FDA approved and are not reviewed by FDA for "
                   "safety, effectiveness, or quality before marketing.")


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_sha(path: Path) -> str:
    return sha_bytes(path.read_bytes())


def tree_hashes(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "node_modules" in path.parts:
            continue
        rel = path.relative_to(root)
        if rel.parts and rel.parts[0] in {"public", ".wrangler"}:
            continue
        out[str(rel)] = file_sha(path)
    return out


def detach_declared_mutations(target: Path, declared_paths: set[str] | None = None) -> dict:
    """Make every existing declared output private before any writer runs."""
    declared_paths = declared_paths or DECLARED_MUTATION_PATHS
    detachments = []
    absent = []
    for relative in sorted(declared_paths):
        path = target / relative
        if not os.path.lexists(path):
            if relative not in MAY_BE_ABSENT_BEFORE_WRITE and not relative.startswith("assets/provider-logos/"):
                raise RuntimeError(f"declared_mutation_path_missing:{relative}")
            absent.append(relative)
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"declared_mutation_path_not_regular:{relative}")
        row = detach_for_write(path)
        row["relative_path"] = relative
        detachments.append(row)
    return {
        "status": "PASS_DECLARED_PATHS_DETACHED",
        "declared_paths": sorted(declared_paths),
        "detached_paths": sorted(row["relative_path"] for row in detachments),
        "absent_before_write": absent,
        "detachments": detachments,
    }


def read_json(path: Path):
    return json.loads(path.read_text())


def _bound_json(binding: object, *, label: str) -> dict:
    """Read one exact local JSON binding without touching any remote URL."""
    if not isinstance(binding, dict):
        raise RuntimeError(f"affiliati_network_identity_{label}_binding_missing")
    path = Path(str(binding.get("path") or "")).resolve()
    try:
        path.relative_to(BUSINESS.resolve())
    except ValueError as exc:
        raise RuntimeError(f"affiliati_network_identity_{label}_outside_business") from exc
    if not path.is_file():
        raise RuntimeError(f"affiliati_network_identity_{label}_missing")
    data = path.read_bytes()
    if sha_bytes(data) != str(binding.get("sha256") or ""):
        raise RuntimeError(f"affiliati_network_identity_{label}_hash_drift")
    if len(data) != int(binding.get("bytes") or -1):
        raise RuntimeError(f"affiliati_network_identity_{label}_size_drift")
    try:
        value = json.loads(data)
    except ValueError as exc:
        raise RuntimeError(f"affiliati_network_identity_{label}_json_invalid") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"affiliati_network_identity_{label}_document_invalid")
    return value


def _tracking_validation_id(spec: dict) -> str:
    """Return the rail-side immutable id while preserving the queue offer id.

    Affiliati has three distinct identities: the governed provider offer id,
    the offer-contract id, and the Link creative id carried by ``c=``. The
    queue/registry must retain the provider offer id. Link-shape validation must
    use the independently captured creative id. The two local captures below
    prove the mapping; no tracking URL is requested, opened, or resolved.
    """
    advertiser_id = str(spec.get("advertiser_id") or "").strip()
    if not advertiser_id:
        raise RuntimeError("spec_advertiser_id_missing")
    rail = _rail_for(str(spec.get("network") or ""))
    if rail is None or rail.network != "Affiliati":
        return advertiser_id

    identity = spec.get("network_identity")
    if not isinstance(identity, dict):
        raise RuntimeError("affiliati_network_identity_missing")
    if (
        identity.get("schema") != "glp.wire_in_identity_evidence.v1"
        or str(identity.get("network") or "").casefold() != "affiliati"
        or str(identity.get("provider_offer_id") or "").strip() != advertiser_id
    ):
        raise RuntimeError("affiliati_network_identity_offer_drift")
    immutable_id = str(identity.get("immutable_id") or "").strip()
    tracking_host = str(identity.get("tracking_host") or "").strip().casefold()
    if not immutable_id.isdigit() or not tracking_host:
        raise RuntimeError("affiliati_network_identity_shape_invalid")

    inventory = _bound_json(identity.get("identity_capture"), label="inventory")
    mapping_rows = []
    for row in inventory.get("rows") or []:
        if not isinstance(row, dict):
            continue
        ids = ((row.get("immutable_ids") or {}).get("value") or {})
        if str(ids.get("program_id") or "").strip() == advertiser_id:
            mapping_rows.append(ids)
    if len(mapping_rows) != 1:
        raise RuntimeError(f"affiliati_network_identity_inventory_match_count:{len(mapping_rows)}")
    offer_contract_id = str(mapping_rows[0].get("offer_contract_id") or "").strip()
    if not offer_contract_id:
        raise RuntimeError("affiliati_network_identity_offer_contract_missing")

    campaign = _bound_json(identity.get("source_capture"), label="campaign")
    offer = campaign.get("offer") or {}
    if (
        campaign.get("schema") != "glp.affiliati-campaign-configuration-capture.v1"
        or str(campaign.get("network") or "").casefold() != "affiliati"
        or str(offer.get("program_id") or "").strip() != offer_contract_id
    ):
        raise RuntimeError("affiliati_network_identity_campaign_drift")
    creatives = [
        row for row in offer.get("creative_configuration") or []
        if isinstance(row, dict) and str(row.get("creative_id") or "").strip() == immutable_id
    ]
    if len(creatives) != 1:
        raise RuntimeError(f"affiliati_network_identity_creative_match_count:{len(creatives)}")
    issued = str(creatives[0].get("unique_link") or "").strip()
    try:
        issued_host = (urlparse(issued).hostname or "").casefold()
    except ValueError as exc:
        raise RuntimeError("affiliati_network_identity_creative_link_invalid") from exc
    if issued_host != tracking_host:
        raise RuntimeError("affiliati_network_identity_tracking_host_drift")
    return immutable_id


def normalize_decimal_price_quote_validation(target: Path) -> dict:
    """Preserve evidenced cents when checking matrix amounts against quotes.

    The predecessor rounded every amount before checking the quote, so an exact
    published value such as ``$84.99`` was tested as ``85`` and rejected. This
    migration keeps the fail-closed quote-membership rule but checks the amount's
    actual decimal/grouped forms. It changes candidate bytes only; the sealed
    predecessor remains untouched.
    """
    source_path = target / "src/cost-calculator.mjs"
    source = source_path.read_text()
    marker = "const quoteTokens = new Set(["
    if marker not in source:
        old = """        const plain = String(Math.round(amount));
        const grouped = Math.round(amount).toLocaleString(\"en-US\");
        if (!row.quote.includes(plain) && !row.quote.includes(grouped)) {
"""
        new = """        const quoteTokens = new Set([
          String(amount),
          amount.toLocaleString(\"en-US\", { useGrouping: true, maximumFractionDigits: 2 }),
          Number.isInteger(amount) ? amount.toFixed(0) : amount.toFixed(2),
        ]);
        if (![...quoteTokens].some((token) => row.quote.includes(token))) {
"""
        if source.count(old) != 1:
            raise RuntimeError("decimal_price_quote_validator_anchor_drift")
        source_path.write_text(source.replace(old, new, 1))

    validator_path = target / "scripts/validate-provider-pricing-matrix.mjs"
    validator = validator_path.read_text()
    validator_marker = "const tokens = new Set(["
    if validator_marker not in validator:
        validator_old = """function amountInQuote(amount, quote) {
  const whole = Math.round(amount);
  return quote.includes(String(whole)) || quote.includes(whole.toLocaleString(\"en-US\"));
}
"""
        validator_new = """function amountInQuote(amount, quote) {
  const tokens = new Set([
    String(amount),
    amount.toLocaleString(\"en-US\", { useGrouping: true, maximumFractionDigits: 2 }),
    Number.isInteger(amount) ? amount.toFixed(0) : amount.toFixed(2),
  ]);
  return [...tokens].some((token) => quote.includes(token));
}
"""
        if validator.count(validator_old) != 1:
            raise RuntimeError("decimal_price_matrix_validator_anchor_drift")
        validator_path.write_text(validator.replace(validator_old, validator_new, 1))

    test_path = target / "tests/provider-surfaces-repair.test.mjs"
    test_text = test_path.read_text()
    test_quote_marker = "String(row[field]),"
    if test_quote_marker not in test_text:
        test_quote_old = """        const plain = String(Math.round(row[field]));
        const grouped = Math.round(row[field]).toLocaleString("en-US");
        assert.ok(row.quote.includes(plain) || row.quote.includes(grouped),
          `${provider.program_id}/${row.medication_id}: ${field} is not in its quote`);
"""
        test_quote_new = """        const quoteTokens = new Set([
          String(row[field]),
          row[field].toLocaleString("en-US", { useGrouping: true, maximumFractionDigits: 2 }),
          Number.isInteger(row[field]) ? row[field].toFixed(0) : row[field].toFixed(2),
        ]);
        assert.ok([...quoteTokens].some((token) => row.quote.includes(token)),
          `${provider.program_id}/${row.medication_id}: ${field} is not in its quote`);
"""
        if test_text.count(test_quote_old) != 1:
            raise RuntimeError("decimal_price_surface_test_anchor_drift")
        test_text = test_text.replace(test_quote_old, test_quote_new, 1)
    test_marker = "an exact cents price inside its quote passes the build"
    if test_marker not in test_text:
        test_text += """

test(\"an exact cents price inside its quote passes the build\", () => {
  const matrix = {
    schema: \"glp-provider-pricing-matrix-v1\",
    providers: {
      decimal: {
        medications: [{
          medication_id: \"compounded-semaglutide-injection\",
          label: \"Compounded semaglutide (injection)\",
          cadence: \"month\",
          month_1_basis: \"same_as_ongoing\",
          month_1_usd: 84.99,
          ongoing_usd: 84.99,
          quote: \"Published price: $84.99 per month.\",
          source_url: \"https://provider.example/pricing\",
          capture_sha256: \"a\".repeat(64),
          captured_on: \"2026-09-01\",
        }],
      },
    },
  };
  assert.doesNotThrow(() => attachPricingMatrix([{ program_id: \"decimal\" }], matrix));
});
"""
    test_path.write_text(test_text)
    return {
        "status": "PASS_DECIMAL_PRICE_QUOTE_VALIDATION_MIGRATED",
        "source": str(source_path.relative_to(target)),
        "validator": str(validator_path.relative_to(target)),
        "test": str(test_path.relative_to(target)),
    }


def logo_mutation_paths(specs: list[dict]) -> set[str]:
    paths = {"assets/provider-logos/manifest.json"}
    for spec in specs:
        output = str(spec.get("logo_output_file") or "")
        if not output or Path(output).name != output or not output.endswith(".webp"):
            raise RuntimeError(f"logo_output_invalid:{spec.get('program_id')}")
        paths.add(f"assets/provider-logos/{output}")
    return paths


def measured_chip(target: Path, asset: Path, program_id: str) -> dict:
    """The four MEASURED chip fields for one staged logo, or a named refusal.

    Delegated to `glp_provider_surface_writers.measure_logo_chip`, which runs the
    CANDIDATE'S OWN `scripts/analyze-provider-logo-luminance.py`. One measurement
    in the codebase, used by both writers — the alternative is what BLOCKER E was:
    two lanes writing the same manifest, one of them measuring and one of them
    not, and the difference only visible as a build crash 40 seconds later.
    """
    import glp_provider_surface_writers as surface_writers  # noqa: PLC0415
    try:
        return surface_writers.measure_logo_chip(target, asset, program_id)
    except surface_writers.SurfaceWriteError as exc:
        raise RuntimeError(str(exc)) from None


def stage_bound_logos(target: Path, specs: list[dict]) -> dict:
    manifest_path = target / "assets/provider-logos/manifest.json"
    manifest = read_json(manifest_path)
    logos = manifest.get("logos")
    if not isinstance(logos, list):
        raise RuntimeError("logo_manifest_schema_invalid")
    sources_path = target / "data/provider-logo-sources.json"
    sources = read_json(sources_path)
    source_rows = sources.get("programs")
    if (sources.get("schema") != "glp-provider-logo-sources-v1"
            or not isinstance(source_rows, list)):
        raise RuntimeError("logo_sources_catalog_schema_invalid")
    staged_rows = []
    staged_sources = []
    for spec in specs:
        pid = spec["program_id"]
        if any(item.get("program_id") == pid for item in logos):
            raise RuntimeError(f"logo_manifest_entry_exists:{pid}")
        if any(item.get("program_id") == pid for item in source_rows):
            raise RuntimeError(f"logo_source_catalog_entry_exists:{pid}")
        binding = spec.get("logo_source")
        if not isinstance(binding, dict) or binding.get("program_id") != pid:
            raise RuntimeError(f"logo_source_binding_invalid:{pid}")
        source = Path(str(binding.get("path") or "")).resolve()
        expected_sha = str(binding.get("sha256") or "")
        expected_bytes = int(binding.get("bytes") or -1)
        if not source.is_file() or file_sha(source) != expected_sha or source.stat().st_size != expected_bytes:
            raise RuntimeError(f"logo_source_missing_or_drifted:{pid}")
        if binding.get("media_type") != "image/webp":
            raise RuntimeError(f"logo_source_media_type_invalid:{pid}")
        width = int(binding.get("width") or 0)
        height = int(binding.get("height") or 0)
        source_url = str(binding.get("source_asset_url") or "")
        if width < 1 or height < 1 or not source_url.startswith("https://"):
            raise RuntimeError(f"logo_source_provenance_invalid:{pid}")
        output = spec["logo_output_file"]
        destination = target / "assets/provider-logos" / output
        shutil.copy2(source, destination)
        if file_sha(destination) != expected_sha:
            raise RuntimeError(f"logo_stage_hash_drift:{pid}")
        entry = {
            "program_id": pid,
            "provider": spec["display_name"],
            "source_page_url": spec["official_url"],
            "source_kind": "remote_image",
            "source_asset_url": source_url,
            "resolved_url": source_url,
            "raw_sha256": str(binding.get("raw_sha256") or expected_sha),
            "public_path": f"/assets/provider-logos/{output}",
            "output_file": output,
            "output_sha256": expected_sha,
            "bytes": expected_bytes,
            "width": width,
            "height": height,
            "media_type": "image/webp",
            "surface": str(binding.get("surface") or "light"),
            "verified_as_of": str(binding.get("verified_as_of") or ""),
            "runtime_remote_load": False,
            # BLOCKER E, 2026-08-20 — MEASURED, never declared. This lane used to
            # emit a manifest row without `logo_treatment`; every one of the 32
            # rows written by `build-provider-logos.py` and by the backfill writer
            # carries it, and `src/provider-chip.mjs :: indexLogoManifest` throws
            # `provider_logo_treatment_missing:<program_id>` on any row that does
            # not. So the net-new path staged a correct asset and then took the
            # whole build down in `decorateProviderChips` — silently, for every
            # net-new provider. The measurement is delegated to the one writer
            # that already owns it, run against the CANDIDATE'S OWN analyzer, and
            # it refuses by name rather than writing a row the renderer rejects.
            **measured_chip(target, destination, pid),
        }
        logos.append(entry)
        staged_rows.append(entry)
        source_row = {
            "program_id": pid,
            "provider": spec["display_name"],
            "source_kind": "remote_image",
            "source_page_url": spec["official_url"],
            "asset_url": source_url,
            "output_file": output,
            "surface": str(binding.get("surface") or "light"),
        }
        source_rows.append(source_row)
        staged_sources.append(source_row)
    manifest["count"] = len(logos)
    manifest["runtime_remote_assets"] = 0
    write_json(manifest_path, manifest)
    verified_dates = [str(sources.get("verified_as_of") or "")]
    verified_dates.extend(str(row.get("verified_as_of") or "") for row in staged_rows)
    sources["verified_as_of"] = max((value for value in verified_dates if value),
                                    default="")
    write_json(sources_path, sources)
    # BLOCKER I, 2026-08-20T21:30:23Z — `npm run validate:logo-treatment` compares
    # this file BYTE FOR BYTE against a full re-measurement, and the derived
    # `logo_treatment_counts` above is maintained by `analyze()`, not by the two
    # fields this function sets. Staging a light-chip logo therefore left the
    # tally one behind and killed the build with `logo_treatment_manifest_stale`.
    # Nothing here maintains a derived field any more: the analyzer the validator
    # runs re-derives the whole document, so the two cannot disagree.
    import glp_provider_surface_writers as surface_writers  # noqa: PLC0415
    try:
        rederived = surface_writers.rederive_logo_manifest(target)
    except surface_writers.SurfaceWriteError as exc:
        raise RuntimeError(str(exc)) from None
    return {"status": "PASS_BOUND_LOGOS_STAGED", "logos": staged_rows,
            "logo_sources": staged_sources,
            "manifest_rederivation": rederived}


def write_json(path: Path, value, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    if private:
        os.chmod(path, 0o600)


def sub_once(path: Path, pattern: str, repl, flags: int = 0) -> str:
    """Apply a regex substitution that MUST match exactly once."""
    text = path.read_text()
    matches = list(re.finditer(pattern, text, flags))
    if len(matches) != 1:
        raise RuntimeError(f"patch_match_count:{path.name}:{len(matches)}:{pattern[:80]!r}")
    new_text = re.sub(pattern, repl, text, count=1, flags=flags)
    if new_text == text:
        raise RuntimeError(f"patch_noop:{path.name}:{pattern[:80]!r}")
    path.write_text(new_text)
    return matches[0].group(0)


def normalize_inline_guide_source_markers(target: Path) -> dict[str, str]:
    """Mark in-prose guide citations before an approved domain can trip the gate.

    Guide source-ledger URLs may also appear inline in prose or bullets. They are
    evidence links, not affiliate inventory, and must retain the exact bare source
    URL while carrying the same explicit marker as the numbered source ledger.
    The migration is idempotent so the first successor repairs the shared renderer
    and every later provider simply inherits the fixed source bytes.
    """
    path = target / "src/blog-system.mjs"
    text = path.read_text()
    patched_signature = (
        'const mdLinks = (value, affiliateUrls = new Set(), affiliateMeta = new Map(), '
        'pageSlug = "", citationUrls = new Set()) =>'
    )
    patched_return = (
        '  const citation = citationUrls.has(href) '
        '? ` data-citation="${CITATION_REASONS.GUIDE_SOURCE_LEDGER}"` : "";\n'
        '  return `<a href="${safeHref}"${rel}${citation}>${label}</a>`;'
    )
    patched_source_urls = (
        '  const sourceUrls = new Set((brief.sources || []).map((item) => esc(item?.url)));\n'
    )
    if (
        patched_signature in text
        and patched_return in text
        and text.count(patched_source_urls) == 2
        and text.count("affiliateMeta, briefPageSlug, sourceUrls)") == 7
    ):
        return {"status": "PASS_ALREADY_SOURCE_MARKED", "path": str(path)}

    replacements = [
        (
            'const mdLinks = (value, affiliateUrls = new Set(), affiliateMeta = new Map(), '
            'pageSlug = "") =>',
            patched_signature,
            1,
        ),
        (
            '  return `<a href="${safeHref}"${rel}>${label}</a>`;',
            patched_return,
            1,
        ),
        (
            '  const affiliateMeta = new Map((brief.affiliate_links || []).filter((item) => '
            'item?.url && item?.program_id).map((item) => [esc(item.url), item]));\n',
            '  const affiliateMeta = new Map((brief.affiliate_links || []).filter((item) => '
            'item?.url && item?.program_id).map((item) => [esc(item.url), item]));\n'
            + patched_source_urls,
            2,
        ),
        (
            "mdLinks(esc(text), affiliateUrls, affiliateMeta, briefPageSlug)",
            "mdLinks(esc(text), affiliateUrls, affiliateMeta, briefPageSlug, sourceUrls)",
            4,
        ),
        (
            "mdLinks(esc(cell), affiliateUrls, affiliateMeta, briefPageSlug)",
            "mdLinks(esc(cell), affiliateUrls, affiliateMeta, briefPageSlug, sourceUrls)",
            3,
        ),
    ]
    for old, new, expected_count in replacements:
        count = text.count(old)
        if count != expected_count:
            raise RuntimeError(
                f"inline_guide_source_marker_match_count:{path.name}:{count}:{expected_count}"
            )
        text = text.replace(old, new)
    path.write_text(text)
    return {"status": "PASS_MIGRATED_SOURCE_MARKERS", "path": str(path)}


def normalize_paid_landing_evidenced_rail_selection(target: Path) -> dict[str, str]:
    """Select the paid-LP comparison rail from priced rows before truncation.

    A newly approved high-scoring row can legitimately lack captured pricing.
    Truncating the ranked list first let such rows consume the five display slots,
    after which the renderer removed them and silently collapsed the comparison
    table. Filtering for evidenced price first preserves rubric order while keeping
    the paid landing's independently tested comparison depth.
    """
    path = target / "scripts/build-staging.mjs"
    text = path.read_text()
    old = '''      const rail = lpRanked.filter((row) => row !== lpRow).slice(0, 5).map((row) => {
        const med = homeCheapestOf(row);
        const score = scoreProvider(row);
        return {
          name: row.provider,
          score: Number.isFinite(score) ? score / 10 : null,
          differentiator: String(row.best_for || "").replace(/^Stronger published /, "Strongest on ").replace(/ evidence in this comparison$/, ""),
          price: med?.ongoing_usd ?? null,
          checked: med?.captured_on ?? null,
          cta: affiliateCta(lpSpec, row, "lp-rail", "visit", "a", 'class="cta cta--row" data-element-code="R"'),
        };
      });'''
    new = '''      const rail = lpRanked
        .filter((row) => row !== lpRow)
        .map((row) => ({ row, med: homeCheapestOf(row) }))
        .filter(({ med }) => Number.isFinite(med?.ongoing_usd))
        .slice(0, 5)
        .map(({ row, med }) => {
          const score = scoreProvider(row);
          return {
            name: row.provider,
            score: Number.isFinite(score) ? score / 10 : null,
            differentiator: String(row.best_for || "").replace(/^Stronger published /, "Strongest on ").replace(/ evidence in this comparison$/, ""),
            price: med.ongoing_usd,
            checked: med.captured_on,
            cta: affiliateCta(lpSpec, row, "lp-rail", "visit", "a", 'class="cta cta--row" data-element-code="R"'),
          };
        });'''
    typed = new.replace(
        '.map((row) => ({ row, med: homeCheapestOf(row) }))',
        '.map((row) => ({ row, med: homeCheapestOf(row), typedPrices: (providerPricingMatrix.providers?.[homeMatrixKey(row)]?.medications || []).filter(item => Object.hasOwn(item, "price_kind")) }))',
    ).replace(
        '.filter(({ med }) => Number.isFinite(med?.ongoing_usd))',
        '.filter(({ med, typedPrices }) => Number.isFinite(med?.ongoing_usd) || typedPrices.length)',
    ).replace('.map(({ row, med }) => {', '.map(({ row, med, typedPrices }) => {').replace(
        'price: med.ongoing_usd,', 'price: med?.ongoing_usd ?? null,',
    ).replace('checked: med.captured_on,', 'checked: med?.captured_on ?? null,\n            typedPrices,')
    counts = {"old": text.count(old), "new": text.count(new), "typed": text.count(typed)}
    print(json.dumps({"paid_landing_rail_path": str(path.resolve()),
                      "snippet_counts": counts}), flush=True)
    if sum(counts.values()) != 1 or text.count("const rail = lpRanked") != 1:
        raise RuntimeError(f"paid_landing_rail_variant_unknown:{path.name}")
    if counts["new"] == 1 or counts["typed"] == 1:
        block = typed if counts["typed"] else new
        print(json.dumps({"rail_status": "PASS_ALREADY_EVIDENCED_RAIL",
                          "before": block, "after": block}), flush=True)
        return {"status": "PASS_ALREADY_EVIDENCED_RAIL", "path": str(path)}
    path.write_text(text.replace(old, new, 1))
    print(json.dumps({"rail_status": "PASS_MIGRATED_EVIDENCED_RAIL",
                      "before": old, "after": new}), flush=True)
    return {"status": "PASS_MIGRATED_EVIDENCED_RAIL", "path": str(path)}


def bump_int(path: Path, pattern: str, delta: int) -> tuple[int, int]:
    """Bump the single capture group in `pattern` by delta. Group 1 is the number."""
    text = path.read_text()
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise RuntimeError(f"bump_match_count:{path.name}:{len(matches)}:{pattern[:80]!r}")
    m = matches[0]
    before = int(m.group(1))
    after = before + delta
    start, end = m.span(1)
    path.write_text(text[:start] + str(after) + text[end:])
    return before, after


def bump_fastpath_validator(path: Path, delta: int) -> None:
    """Advance literals while preserving the release's exact live/held identities."""
    text = path.read_text()
    patterns = {
        name: rf"assert\.equal\({name}\.providers\.length, (\d+)\);"
        for name in ("contracts", "fastpath")
    }
    matches = {name: list(re.finditer(pattern, text)) for name, pattern in patterns.items()}
    print(json.dumps({"validator_path": str(path.resolve()),
                      "assertion_matches": {name: len(found) for name, found in matches.items()} }), flush=True)
    for name, found in matches.items():
        if len(found) != 1:
            raise RuntimeError(f"fastpath_assertion_missing_or_ambiguous:{path.name}:{name}.providers.length:{len(found)}")
    for name, pattern in patterns.items():
        before = matches[name][0].group(0)
        bump_int(path, pattern, delta)
        after = re.search(pattern, path.read_text()).group(0)
        print(json.dumps({"assertion": name, "before": before, "after": after}), flush=True)
    bump_int(path, r"assert\.equal\(logoManifest\.count, (\d+)\);", delta)
    live_pattern = r'source_kind === "fastpath" && item\.commercial_state === "live"\)\.length, (\d+)\);'
    if re.search(live_pattern, text):
        bump_int(path, live_pattern, delta)
    else:
        identities = (
            'const fastpathRegistryPrograms = programRegistry.programs.filter((item) => item.source_kind === "fastpath");',
            'assert.deepEqual(fastpathRegistryPrograms.map((item) => item.program_id).sort(), fastpath.providers.map((item) => item.program_id).sort());',
            'assert.deepEqual(fastpathRegistryPrograms.filter((item) => item.commercial_state !== "live").map((item) => item.program_id).sort(), ["telos-rx"]);',
            'assert.deepEqual(fastpathRegistryPrograms.filter((item) => item.commercial_state === "live").map((item) => item.program_id).sort(), fastpath.providers.map((item) => item.program_id).filter((id) => id !== "telos-rx").sort());',
        )
        if any(text.count(assertion) != 1 for assertion in identities):
            raise RuntimeError(f"fastpath_live_identity_assertion_missing:{path.name}")


def bump_int_or_dynamic(
    path: Path,
    numeric_pattern: str,
    delta: int,
    dynamic_pattern: str,
) -> tuple[int, int] | None:
    """Maintain a literal cardinality, tolerating its dynamic replacement beside it."""
    text = path.read_text()
    numeric = list(re.finditer(numeric_pattern, text))
    dynamic = list(re.finditer(dynamic_pattern, text))
    if len(numeric) == 1:
        # The literal binds whether or not the identity sits beside it.
        return bump_int(path, numeric_pattern, delta)
    if not numeric and len(dynamic) == 1:
        return None
    raise RuntimeError(
        f"bump_or_dynamic_match_count:{path.name}:numeric={len(numeric)}:dynamic={len(dynamic)}"
    )


def advance_phase3_expected_routes(target: Path, delta: int) -> tuple[int, int]:
    """Advance the Phase 3 render cardinality by one route per new provider.

    `buildPhase3PageGraph` emits one provider-detail page for every canonical
    provider. The incremental builder already grows that provider roster, so the
    graph's fail-closed route expectation is part of the same transaction rather
    than a downstream constant for an operator to patch after every addition.
    """
    path = target / "data/phase3-page-graph.json"
    graph = read_json(path)
    before = graph.get("expected_indexable_routes")
    if not isinstance(before, int) or isinstance(before, bool) or before < 1:
        raise RuntimeError("phase3_expected_indexable_routes_invalid")
    if not isinstance(delta, int) or isinstance(delta, bool) or delta < 1:
        raise RuntimeError("phase3_route_delta_invalid")
    after = before + delta
    graph["expected_indexable_routes"] = after
    write_json(path, graph)
    return before, after


def normalize_article_route_master_cow_validation(target: Path) -> dict:
    """Make PASS 2 image validation portable across linked successor trees.

    The resumable generation state intentionally records the absolute path where
    each immutable master was first produced. A successor vendors those bytes
    under its own ``assets/source/article-masters-pass2`` directory. Requiring the
    historical path itself to be inside the successor therefore rejects every
    honest copy-on-write build. Preserve the receipt's basename and hash checks,
    but validate the successor-local mirror that will actually be published.
    """
    path = target / "scripts/generate-article-imagery.py"
    old = '''        path = Path(record["output_path"]).resolve()
        if ROUTE_MASTER_DIR.resolve() not in path.parents or not path.exists():
            raise RuntimeError(f"article_route_master_path_invalid:{route}")
        if sha_file(path) != record.get("output_sha256"):
'''
    new = '''        recorded_path = Path(record["output_path"]).resolve()
        expected_name = f"{stem_for(route)}.webp"
        if recorded_path.name != expected_name:
            raise RuntimeError(f"article_route_master_identity_invalid:{route}")
        # The immutable generation receipt names the candidate where the master
        # was originally created. Successor candidates vendor those exact bytes
        # into their own route-master directory, so validate the local mirror
        # rather than requiring the historical absolute path to sit under ROOT.
        path = (ROUTE_MASTER_DIR / expected_name).resolve()
        if ROUTE_MASTER_DIR.resolve() not in path.parents or not path.exists():
            raise RuntimeError(f"article_route_master_path_invalid:{route}")
        if sha_file(path) != record.get("output_sha256"):
'''
    text = path.read_text(encoding="utf-8")
    if new in text:
        return {"status": "PASS_ALREADY_COW_SAFE", "path": str(path.relative_to(target))}
    if text.count(old) != 1:
        raise RuntimeError(f"article_route_master_cow_patch_match_count:{text.count(old)}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return {"status": "PASS_MIGRATED_COW_SAFE", "path": str(path.relative_to(target))}


def require_zero_review_pending_invariant(
    path: Path,
    numeric_pattern: str,
    dynamic_pattern: str,
) -> int:
    """Pin the review-rating spine: provider adds may grow routes, never pending.

    The 2026-08-06 renderer repair binds the build-derived content-ledger score
    to fast-path review rows. Therefore `pending` is not a cardinality maintained
    by this constructor. It must remain exactly zero while `routes.length` grows.
    A missing, duplicated, or already-weakened assertion is a named machine
    refusal rather than permission to ship an unscored review.
    """
    text = path.read_text()
    numeric = list(re.finditer(numeric_pattern, text))
    dynamic = list(re.finditer(dynamic_pattern, text))
    if len(numeric) != 1 or len(dynamic) != 1:
        raise RuntimeError(
            f"review_rating_invariant_match_count:{path.name}:"
            f"numeric={len(numeric)}:dynamic={len(dynamic)}"
        )
    pending = int(numeric[0].group(1))
    if pending != 0:
        raise RuntimeError(
            f"review_rating_pending_invariant_nonzero:{path.name}:{pending}"
        )
    return pending


def write_provider_add_merit_binding_test(target: Path) -> Path:
    """Install a data-driven regression for every fast-path/provider-add row."""
    path = target / "tests/provider-add-merit-binding.test.mjs"
    body = '''import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { applyMeritScores, meritScore } from "../src/merit-ranking.mjs";
import { scoreProvider } from "../src/money-page.mjs";
import { comparablePriceProviderIds } from "../src/provider-dynamic-profiles.mjs";
import { buildProviderReviewSpecs, renderProviderReviewPage } from "../src/provider-review.mjs";

const readJson = async (relative) => JSON.parse(await readFile(new URL(`../data/${relative}`, import.meta.url), "utf8"));

test("provider-add reviews bind the exact merit score their comparison rows publish", async () => {
  const [ledger, registry, money, contracts, fastpath, identity] = await Promise.all([
    readJson("program-content-ledger.json"),
    readJson("program-registry.json"),
    readJson("money-page-specs.json"),
    readJson("provider-page-contracts.json"),
    readJson("provider-fastpath-contract.json"),
    readJson("identity-registry.json"),
  ]);
  const scoredPrograms = structuredClone(ledger.programs);
  const comparablePriceIds = comparablePriceProviderIds(registry, "glp-clinic-scout", {
    asOf: new Date(`${registry.provider_dynamic_profiles.sealed_as_of}T12:00:00Z`),
  });
  applyMeritScores(scoredPrograms, { comparablePriceIds });
  const ledgerById = new Map(scoredPrograms.map((entry) => [entry.program_id, entry]));
  const reviews = new Map(buildProviderReviewSpecs(
    money.pages, contracts, fastpath, registry, "glp-clinic-scout", scoredPrograms,
  ).map((review) => [review.program_id, review]));

  for (const contract of contracts.providers) {
    const id = contract.program_id;
    const entry = ledgerById.get(id);
    assert.ok(entry, `provider_add_merit_input_missing:${id}:program-content-ledger`);
    const expected = meritScore(entry);
    assert.ok(Number.isFinite(expected), `provider_add_merit_input_missing:${id}:criteria_scores`);
    const review = reviews.get(id);
    assert.ok(review, `provider_add_review_missing:${id}`);
    assert.equal(scoreProvider(review.row), expected, `provider_add_review_score_drift:${id}`);

    const comparisonRows = money.pages.flatMap((page) => page.rows || [])
      .filter((row) => row.program_id === id);
    assert.ok(comparisonRows.length > 0, `provider_add_comparison_row_missing:${id}`);
    for (const row of comparisonRows) {
      assert.equal(
        scoreProvider({ ...row, criteria_scores: entry.criteria_scores }),
        expected,
        `provider_add_comparison_score_drift:${id}`,
      );
    }

    const html = renderProviderReviewPage(review, identity);
    assert.match(html, new RegExp(`class="score">${(expected / 10).toFixed(1).replace(".", "\\\\.")}\\\\/10<\\\\/p>`), id);
    assert.doesNotMatch(html, /Not scored|Score pending|Pending evidence/i, id);
  }
});
'''
    path.write_text(body)
    return path


def quote(research: dict, field: str) -> dict:
    item = (research.get("fields") or {}).get(field)
    if item is None:
        raise RuntimeError(f"research_field_missing:{field}")
    if item.get("value") and (not item.get("source_url") or not item.get("verified_as_of")):
        raise RuntimeError(f"research_field_unsourced:{field}")
    return item


def stated(item: dict, fallback: str) -> str:
    """Render a research field honestly: a real value, or an explicit not-published line."""
    value = item.get("value")
    if value:
        return str(value)
    if item.get("state") != "not_publicly_stated":
        raise RuntimeError("research_field_empty_without_state")
    return fallback


def canonical_field_sources(spec: dict, research: dict) -> dict[str, list[str]]:
    """Bind renderer citation groups to exact URLs already present in dated evidence."""
    supplied = spec.get("field_sources") or {}
    fields = research.get("fields") or {}

    def urls(*names: str) -> list[str]:
        values: list[str] = []
        for name in names:
            item = fields.get(name) or {}
            value = item.get("source_url")
            if isinstance(value, str) and value.startswith("https://") and value not in values:
                values.append(value)
        return values

    def supplied_urls(*names: str) -> list[str]:
        values: list[str] = []
        for name in names:
            for value in supplied.get(name) or []:
                if isinstance(value, str) and value.startswith("https://") and value not in values:
                    values.append(value)
        return values

    groups = {
        "pricing_and_inclusions": urls("first_month_price", "consultation_fee", "shipping_wait")
        + supplied_urls("pricing_and_inclusions", "pricing_eligibility_shipping"),
        "cancellation_and_roles": urls("cancellation", "states_served", "consultation_fee")
        + supplied_urls("cancellation_and_roles", "program_terms"),
        "regulatory": urls("pharmacy_type") + supplied_urls("regulatory"),
    }
    for key, values in groups.items():
        groups[key] = list(dict.fromkeys(values))
        if not groups[key]:
            raise RuntimeError(f"field_source_group_missing:{spec['program_id']}:{key}")
    return groups


def normalize_regulatory_banner(value: object) -> str:
    """Normalize legacy boolean authoring output to the governed banner code."""
    if value is True:
        return "compounded_glp_general"
    if value is False:
        return "no_compounded_classification_published"
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise RuntimeError("regulatory_banner_required_invalid")


def build_contract(spec: dict, research: dict, tracking_url: str, logo: dict,
                   publisher_id: str) -> dict:
    pid = spec["program_id"]
    price = quote(research, "first_month_price")
    states = quote(research, "states_served")
    consult = quote(research, "consultation_fee")
    cancel = quote(research, "cancellation")
    pharmacy = quote(research, "pharmacy_type")
    shipping = quote(research, "shipping_wait")

    eligibility = " ".join(x for x in (
        stated(states, "The provider does not publish a list of the states it serves."),
        stated(consult, "The provider does not publish a consultation price."),
    ) if x)

    regulatory_banner = normalize_regulatory_banner(spec.get("regulatory_banner_required"))
    regulatory = stated(pharmacy, "The provider does not publish who fills its prescriptions.")
    if regulatory_banner.endswith("compounded_glp_general"):
        regulatory = f"{regulatory} {COMPOUNDED_TAIL}"

    row = spec["row_contract"]
    copy = spec["page_copy"]
    return {
        "display_name": spec["display_name"],
        "program_id": pid,
        "official_url": spec["official_url"],
        # A `tracking_domain` rail (XenTraffic and friends) rotates its domain per
        # tenant, so its host can ONLY be declared. Carrying the declaration onto
        # the contract is what lets the renderer's allowlist derivation
        # (`scripts/emit-tracking-allowed-hosts.py` -> `program_allowed_hosts`)
        # resolve that rail at build time instead of refusing it — the Blocker F
        # defect, one rail forward. Absent when the spec declares nothing.
        **({"tracking_host": spec["tracking_host"]} if spec.get("tracking_host") else {}),
        "review_path": f"/reviews/{pid}/",
        "field_sources": canonical_field_sources(spec, research),
        "logo_contract": {
            "asset_ready": True,
            "intended_public_path": logo["path"],
            "official_source": logo["source_asset_url"],
            "report_asset": f"assets/provider-logos/{spec['logo_output_file']}",
            "sha256": logo["sha256"],
        },
        "network_contract": {
            "activation_blocker": None,
            "immutable_offer_id": spec["advertiser_id"],
            "local_evidence_checked_through": spec["checked_as_of"],
            "network": spec["network"],
            "promotion_authorized_in_this_artifact": True,
            "publisher_id": publisher_id,
            "relationship_state": "approved_or_active",
            "sheet_status": "approved_active_tracking_ready",
            "tracking_url": tracking_url,
            "transparent_direct_supported_by_current_renderer": True,
        },
        "page_copy": {
            "title": copy["title"],
            "description": copy["description"],
            "bottom_line": copy["bottom_line"],
            "pricing": price["value"],
            "eligibility": eligibility,
            "shipping": stated(shipping, "The provider does not publish a delivery timeframe."),
            "cancellation": stated(cancel, "The provider does not publish a cancellation policy."),
            "regulatory": regulatory,
        },
        "publication_state": {
            "affiliate_activation_candidate_after_gate": True,
            "affiliate_activation_state": "G6_R1A_WIRE_IN_20260806",
            "calculator_eligible": False,
            "editorial_content_ready": True,
            "regulatory_banner_required": regulatory_banner,
        },
        "row_contract": {
            "affiliate_href": tracking_url,
            "affiliate_link_mode": "transparent_direct",
            "affiliate_network": spec["network"],
            "best_for": row["best_for"],
            "cons": row["cons"],
            "criteria_scores": {},
            "insurance": row["insurance"],
            "live_link_allowed": True,
            "logo": logo["path"],
            "medications": row["medications"],
            "program_id": pid,
            "promotion_authorized": True,
            "pros": row["pros"],
            "provider": spec["display_name"],
            "review_path": f"/reviews/{pid}/",
            "score": None,
            "source_url": price["source_url"],
            "starting_price": row["starting_price"],
            # Deliberately null until the governed dynamic-pricing projection
            # decides whether one monthly comparator exists. Rating is separate:
            # the build derives it from this provider's seeded/enriched ledger row.
            "starting_price_sort": None,
        },
    }


def build_integration(spec: dict, logo: dict, activation_at_utc: str) -> dict:
    return {
        "affiliate_cta_state": "ACTIVE_PROVIDER_ISSUED_DIRECT",
        "activation_at_utc": activation_at_utc,
        "checked_as_of": spec["checked_as_of"],
        "field_bindings": dict(FIELD_BINDINGS),
        "hold_copy": HOLD_COPY,
        "logo": {
            "height": logo["height"],
            "media_type": logo["media_type"],
            "path": logo["path"],
            "sha256": logo["sha256"],
            "source_path": f"assets/provider-logos/{spec['logo_output_file']}",
            "surface": logo["surface"],
            "width": logo["width"],
        },
        "program_id": spec["program_id"],
        "route_state": "LIVE_EDITORIAL_AFFILIATE_ACTIVE",
    }


def build_registry_entry(spec: dict) -> dict:
    """The default surface set for a newly wired provider.

    2026-08-06 surface unification. This function used to ship every new program
    with compare, quiz AND calculator switched OFF behind one canned "ships
    unrated on the fastpath review lane" sentence. That single default is what
    built a two-tier site: 16 live providers reachable only as review pages while
    the comparison page said "13 providers". The default is now ON. A provider's
    evidence score is derived from its own dated research by
    src/merit-ranking.mjs, so a ranked row never has to invent anything.

    Calculator is the one surface that still starts off, because it autofills a
    single monthly number and most new providers publish a range, a bundle or an
    intro rate. Its exception names the specific ambiguity, and
    scripts/glp_surface_unification.py re-adjudicates it against the research and
    turns it on the moment one comparable figure exists.
    """
    pid = spec["program_id"]
    evidence_projection = (
        registry_evidence.load_registry_evidence(spec)
        if spec.get("registry_evidence_file") else None
    )
    evidence_record = evidence_projection["record"] if evidence_projection else None
    vertical_fit = (
        (evidence_record.get("vertical_fit_evidence") or {}).get("asserted") is True
        if evidence_record else True
    )
    calculator_reason = (
        f"{spec['display_name']} was wired in without a single comparable published monthly "
        "figure to autofill. Re-adjudicated against the dated research by "
        "scripts/glp_surface_unification.py on every run; this exception is void the moment one "
        "unambiguous monthly price exists.")
    entry = {
        "commercial_state": "live",
        "display_name": spec["display_name"],
        "go": {"entry_state": "transparent_direct", "route": f"/go/{pid}"},
        "profiles": {
            "glp-clinic-scout": {
                "data_profile": {"has_price": False, "has_review_facts": True},
                "exceptions": {
                    "calculator": {"enabled": True, "reason": calculator_reason},
                },
                "surfaces": {
                    "calculator": False, "compare": True, "go": True,
                    "quiz": True, "review": True, "sitemap": True,
                },
                "vertical_fit": vertical_fit,
                "wired": True,
            }
        },
        "program_id": pid,
        "review_path": f"/reviews/{pid}/",
        "source_kind": "fastpath",
    }
    if evidence_projection:
        entry["research_dynamic_profile"] = json.loads(json.dumps(spec["provider_dynamic_profile"]))
        entry["profiles"]["glp-clinic-scout"]["vertical_fit_evidence"] = json.loads(
            json.dumps(evidence_record["vertical_fit_evidence"])
        )
        entry["tracking_link_verification"] = json.loads(
            json.dumps(evidence_record["tracking_link_verification"])
        )
        embedded = json.loads(json.dumps(evidence_record))
        embedded["source"] = evidence_projection["source"]
        entry["registry_evidence"] = embedded
    return entry


def build_go_manifest_entry(rel: str, spec: dict, manifest: dict) -> dict:
    pid = spec["program_id"]
    if rel == "artifacts/ops/worker-manifest.staging.json":
        return {
            "program_id": pid,
            "display_name": spec["display_name"],
            "route": f"/go/{pid}",
            "entry_state": "transparent_direct",
            "commercial_state": "live",
        }
    template = next(
        (item for item in manifest["programs"] if item["program_id"] == "measured"),
        None,
    )
    if template is None:
        raise RuntimeError(f"go_manifest_template_missing:{rel}:measured")
    entry = dict(template)
    entry.update({
        "program_id": pid,
        "display_name": spec["display_name"],
        "route": f"/go/{pid}",
    })
    entry.pop("route_slug", None)
    return entry


def apply_changes(target: Path, specs: list[dict], tracking: dict[str, str],
                  publishers: dict[str, str], activation_at_utc: str,
                  sheet_snapshot: Path, research_dir: Path) -> dict:
    notes: dict = {}
    n = len(specs)
    ids = [s["program_id"] for s in specs]

    logo_manifest = read_json(target / "assets/provider-logos/manifest.json")
    logos: dict[str, dict] = {}
    for spec in specs:
        pid = spec["program_id"]
        entry = next((item for item in logo_manifest["logos"] if item["program_id"] == pid), None)
        if entry is None:
            raise RuntimeError(f"logo_manifest_entry_missing:{pid}")
        staged = target / "assets/provider-logos" / spec["logo_output_file"]
        if not staged.is_file() or file_sha(staged) != entry["output_sha256"]:
            raise RuntimeError(f"logo_asset_missing_or_drifted:{pid}")
        logos[pid] = {
            "path": entry["public_path"], "sha256": entry["output_sha256"],
            "width": entry["width"], "height": entry["height"],
            "media_type": entry["media_type"], "surface": entry["surface"],
            "source_asset_url": entry["source_asset_url"],
        }
    notes["logos"] = {pid: logos[pid]["path"] for pid in ids}

    research: dict[str, dict] = {}
    for spec in specs:
        doc = read_json(research_dir / spec["research_file"])
        if doc.get("schema") != "program-deep-research-v1":
            raise RuntimeError(f"research_schema_invalid:{spec['program_id']}")
        research[spec["program_id"]] = doc

    # 1. provider-page-contracts.json
    contracts_path = target / "data/provider-page-contracts.json"
    contracts = read_json(contracts_path)
    present = {p["program_id"] for p in contracts["providers"]}
    for spec in specs:
        if spec["program_id"] in present:
            raise RuntimeError(f"contract_already_present:{spec['program_id']}")
        contracts["providers"].append(
            build_contract(spec, research[spec["program_id"]], tracking[spec["program_id"]],
                           logos[spec["program_id"]], publishers[spec["program_id"]]))
    contracts["target_release"]["requested_provider_review_count"] += n
    write_json(contracts_path, contracts)
    notes["requested_provider_review_count"] = contracts["target_release"]["requested_provider_review_count"]

    # 2. provider-fastpath-contract.json — integrations, counts, self-hash.
    fastpath_path = target / "data/provider-fastpath-contract.json"
    fastpath = read_json(fastpath_path)
    for spec in specs:
        fastpath["providers"].append(
            build_integration(spec, logos[spec["program_id"]], activation_at_utc))
    fastpath["fastpath_provider_count"] = len(fastpath["providers"])
    # `base_provider_review_count + fastpath_provider_count` stopped being the
    # review count on 2026-08-17: the Katalys pause removal deleted three
    # programs from the registry and re-derived expected_provider_review_count
    # from the surviving review ids, without touching either counter. Rebuilding
    # from that dead identity re-inflated the number by the removed programs and
    # killed the build gate (`provider_review_count_invalid:28:expected:30`,
    # 2026-08-18). Advance the count that is actually there by exactly the rows
    # this run adds; step 3 then re-derives it from the registry and fails loudly
    # on any drift, so the two can never silently disagree again.
    fastpath["expected_provider_review_count"] += n
    fastpath["current_contract_sha256"] = file_sha(contracts_path)
    write_json(fastpath_path, fastpath)
    notes["fastpath_provider_count"] = fastpath["fastpath_provider_count"]
    notes["expected_provider_review_count"] = fastpath["expected_provider_review_count"]

    # 3. program-registry.json — entries + sitemap routes.
    registry_path = target / "data/program-registry.json"
    registry = read_json(registry_path)
    present = {p["program_id"] for p in registry["programs"]}
    for spec in specs:
        if spec["program_id"] in present:
            raise RuntimeError(f"registry_entry_already_present:{spec['program_id']}")
        registry["programs"].append(build_registry_entry(spec))
    profile = registry["site_profiles"]["glp-clinic-scout"]
    profile["expected_sitemap_routes"] += n
    write_json(registry_path, registry)
    notes["expected_sitemap_routes"] = profile["expected_sitemap_routes"]

    # The Phase 3 page graph emits one provider-detail route for every provider
    # the registry adds. Keep its fail-closed count in the same constructor
    # transaction; otherwise the renderer sees N+1 real pages against a stale N
    # expectation and refuses every future net-new provider build.
    phase3_before, phase3_after = advance_phase3_expected_routes(target, n)
    notes["expected_phase3_routes_before"] = phase3_before
    notes["expected_phase3_routes"] = phase3_after

    # The live predecessor predates the PASS 2 route-master COW repair. Apply the
    # exact fail-closed migration inside the candidate constructor so its build
    # validates the vendored bytes rather than an obsolete predecessor pathname.
    notes["article_route_master_cow_validation"] = (
        normalize_article_route_master_cow_validation(target)
    )

    # The registry projection is what src/provider-review.mjs actually enforces.
    # Reconcile the fastpath claim against it here so a stale counter surfaces in
    # this builder with an exact number instead of inside the build gate.
    registry_review_ids = {
        program["program_id"] for program in registry["programs"]
        if program.get("source_kind") in ("money", "fastpath")
        and ((program.get("profiles") or {}).get("glp-clinic-scout") or {})
        .get("surfaces", {}).get("review") is True
    }
    if len(registry_review_ids) != fastpath["expected_provider_review_count"]:
        raise RuntimeError(
            "expected_provider_review_count_registry_drift:"
            f"{fastpath['expected_provider_review_count']}:registry:{len(registry_review_ids)}")
    notes["registry_derived_provider_review_count"] = len(registry_review_ids)

    # 4. go manifests.
    for rel in ("artifacts/ops/worker-manifest.staging.json", "data/worker-manifest.staging.json"):
        path = target / rel
        if not path.is_file():
            continue
        manifest = read_json(path)
        present = {p["program_id"] for p in manifest["programs"]}
        for spec in specs:
            pid = spec["program_id"]
            if pid in present:
                raise RuntimeError(f"go_manifest_already_present:{rel}:{pid}")
            manifest["programs"].append(build_go_manifest_entry(rel, spec, manifest))
        if "program_count" in manifest:
            manifest["program_count"] = len(manifest["programs"])
        write_json(path, manifest)
        notes.setdefault("go_manifests", []).append(rel)

    # 5. Corroborating approval record — same strictness as the frozen
    #    approved-program-registry, appended rather than rewritten.
    approvals_path = target / "data/affiliate-wire-in-approvals.json"
    approvals = read_json(approvals_path)
    if approvals.get("schema") != "glp-affiliate-wire-in-approvals-v1":
        raise RuntimeError("approvals_schema_invalid")
    present = {p["existing_release_alias"] for p in approvals["programs"]}
    for spec in specs:
        pid = spec["program_id"]
        if pid in present:
            raise RuntimeError(f"approvals_already_present:{pid}")
        approvals["programs"].append({
            "existing_release_alias": pid,
            "display_name": spec["display_name"],
            "network": spec["network"],
            "provider_program_id": spec["advertiser_id"],
            "publisher_id": publishers[pid],
            "canonical_sheet_row": spec["sheet_row"],
            "sheet_status": "approved_active_tracking_ready",
            "signup_complete": True,
            "wire_in_ready": True,
            "approval_state": "approved_active",
            "historical_activation_authority": {
                "approval_state": "approved_active",
                "classification_path": spec["terms_classification_path"],
                "rubric_verdict_at_activation": "PASS",
                "bridge_ruling_considered": "terms-document-hierarchy-20260806",
            },
            "current_terms_reassessment": {
                "as_of_utc": None,
                "evidence_path": None,
                "future_change_terms_gate_open": False,
                "hold_trigger": "current_terms_reassessment_required",
                "live_link_disposition": "preserve_existing_live",
                "may_authorize_new_activation": False,
                "may_authorize_material_expansion": False,
                "verdict": "HOLD_FUTURE_CHANGE_EXISTING_LIVE_PRESERVED",
            },
            "tracking": {
                "provider_issued_url": tracking[pid],
                "link_qa_status": "not_clicked_preserve_attribution",
                "clicks": 0,
            },
        })
    approvals["source_authority"]["sheet_snapshot_sha256"] = file_sha(sheet_snapshot)
    write_json(approvals_path, approvals)

    # 6. Count assertions. Every one is read-then-bumped, so this builder is
    #    re-runnable against a moved head instead of frozen to one wave.
    validator = target / "scripts/validate-provider-fastpath-plus5.mjs"
    bump_fastpath_validator(validator, n)

    fp_test = target / "tests/provider-fastpath-plus5.test.mjs"
    bump_int(fp_test,
             r"assert\.equal\(new Set\(fastpath\.providers\.map\(\(provider\) => provider\.program_id\)\)\.size, (\d+)\);", n)
    bump_int_or_dynamic(
        fp_test,
        r"assert\.equal\(reviews\.length, (\d+)\);",
        n,
        r"assert\.equal\(reviews\.length, fastpath\.expected_provider_review_count\);",
    )
    bump_int_or_dynamic(
        fp_test,
        r"assert\.equal\(new Set\(reviews\.map\(\(review\) => review\.path\)\)\.size, (\d+)\);",
        n,
        r"assert\.equal\(new Set\(reviews\.map\(\(review\) => review\.path\)\)\.size, reviews\.length\);",
    )
    bump_int_or_dynamic(
        fp_test,
        r"assert\.equal\(data\.providers\.length, (\d+)\);",
        n,
        r"assert\.equal\(data\.providers\.length, canonicalIds\.size\);",
    )
    bump_int_or_dynamic(
        fp_test,
        r"assert\.equal\(new Set\(data\.providers\.map\(\(provider\) => provider\.program_id\)\)\.size, (\d+)\);",
        n,
        r"assert\.deepEqual\(new Set\(data\.providers\.map\(\(provider\) => provider\.program_id\)\), canonicalIds\);",
    )

    quoted = "".join(f', "{pid}"' for pid in ids)
    sub_once(fp_test, r'(const activeIds = new Set\(\[[^\]]*)\]\)',
             lambda m: m.group(1) + quoted + "])")
    sub_once(
        fp_test,
        r'(for \(const id of \[[^\]]*)\]\) assert\.ok\(eligibleIds\.has\(id\), id\)',
        lambda m: m.group(1) + quoted + "]) assert.ok(eligibleIds.has(id), id)",
    )
    sub_once(
        fp_test,
        r'(assert\.deepEqual\(new Set\(added\.map\(\(review\) => review\.program_id\)\), new Set\(\[[^\]]*)\]\)\);',
        lambda m: m.group(1) + quoted + "]));",
    )

    calculator_test = target / "tests/cost-calculator.test.mjs"
    bump_int_or_dynamic(
        calculator_test,
        r"assert\.equal\(data\.providers\.length, (\d+)\);",
        n,
        r"assert\.equal\(data\.providers\.length, expected\.size\);",
    )
    bump_int_or_dynamic(
        calculator_test,
        r"assert\.equal\(new Set\(data\.providers\.map\(\(provider\) => provider\.program_id\)\)\.size, (\d+)\);",
        n,
        r"assert\.deepEqual\(new Set\(data\.providers\.map\(\(provider\) => provider\.program_id\)\), expected\);",
    )

    bump_int_or_dynamic(
        target / "tests/affiliate-expansion-13.test.mjs",
        r"assert\.equal\(reviews\.length, (\d+)\);",
        n,
        r"assert\.equal\(reviews\.length, providerFastpath\.expected_provider_review_count\);",
    )

    coverage = target / "tests/program-registry-coverage.test.mjs"
    bump_int_or_dynamic(
        coverage,
        r"assert\.equal\(result\.sitemap_routes, (\d+)\);",
        n,
        r"assert\.equal\(result\.sitemap_routes, inputs\.registry\.site_profiles\[\"glp-clinic-scout\"\]\.expected_sitemap_routes\);",
    )
    bump_int(coverage, r"assert\.equal\(result\.review_pages, (\d+)\);", n)
    bump_int_or_dynamic(
        coverage, r"assert\.equal\(result\.live_fastpath_programs, (\d+)\);", n,
        r"assert\.equal\(result\.live_fastpath_programs, liveContracts\.length\);",
    )

    # Route cardinality grows, but the 2026-08-06 rating spine remains zero-pending:
    # build-staging derives merit from the seeded/enriched ledger, then
    # buildProviderReviewSpecs binds that exact row onto the fast-path review.
    wave2 = target / "tests/wave2-regression.test.mjs"
    bump_int(wave2, r"assert\.equal\(routes\.length, (\d+)\);", n)
    notes["review_rating_pending"] = require_zero_review_pending_invariant(
        wave2,
        r"assert\.equal\(pending, (\d+)\);",
        r"assert\.equal\(rated \+ pending, routes\.length\);",
    )
    routes_after = read_json(target / "data/program-registry.json")  # noqa: F841 (readability)
    sub_once(wave2, r'test\("all (\d+) legacy review pages',
             lambda m: m.group(0).replace(m.group(1), str(int(m.group(1)) + n)))
    merit_test = write_provider_add_merit_binding_test(target)
    notes["provider_add_merit_binding_test"] = str(merit_test.relative_to(target))

    return notes


def run_surface_integration(target: Path) -> dict:
    """Merge the research, then put every live provider on every surface it earns."""
    steps = []
    for name, command in (
        ("research_merge", [sys.executable, str(BUSINESS / "scripts/affiliate_merge_deep_research.py"),
                            "--release", str(target), "--seed-missing",
                            "--output", str(target / "data/program-content-ledger.json")]),
        ("surface_unification", [sys.executable, str(BUSINESS / "scripts/glp_surface_unification.py"),
                                 "--release", str(target), "--candidate-in-place"]),
    ):
        proc = subprocess.run(command, capture_output=True, text=True, timeout=900)
        steps.append({"step": name, "returncode": proc.returncode,
                      "stderr_tail": proc.stderr[-500:] if proc.returncode else ""})
        if proc.returncode != 0:
            raise RuntimeError(f"surface_integration_failed:{name}:{proc.stderr[-300:]}")
    return {"status": "PASS", "steps": steps}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--sheet", required=True, help="sheet snapshot minted in MISSION_DIR")
    parser.add_argument("--authority", required=True, help="per-run selection authority receipt")
    parser.add_argument("--spec", required=True, help="g6-r1a-wirein-program-specs-v1")
    parser.add_argument(
        "--research-dir",
        default=str(RESEARCH),
        help="directory containing the spec's hash-bound program research files",
    )
    parser.add_argument("--report-dir", required=True)
    parser.add_argument(
        "--defer-surface-integration",
        action="store_true",
        help=(
            "apply the base registry/review changes only; the B2 batch worker will attach "
            "the hash-bound provider dynamic profile and run the shared integration once"
        ),
    )
    args = parser.parse_args()

    source = Path(args.source).resolve()
    target = Path(args.target).resolve()
    report = Path(args.report_dir).resolve()

    authority_path = Path(args.authority).resolve()
    authority = read_json(authority_path)
    if authority.get("status") != "APPROVED":
        raise RuntimeError("authority_not_approved")
    scope = authority.get("approved_scope") or {}
    authority_sha = file_sha(authority_path)

    spec_path = Path(args.spec).resolve()
    research_dir = Path(args.research_dir).resolve()
    if not research_dir.is_dir():
        raise RuntimeError(f"research_dir_missing:{research_dir}")
    spec_doc = read_json(spec_path)
    if spec_doc.get("schema") != "g6-r1a-wirein-program-specs-v1":
        raise RuntimeError("spec_schema_invalid")
    specs = spec_doc["programs"]
    spec_ids = sorted(s["program_id"] for s in specs)
    if spec_ids != sorted(scope.get("newly_wired_program_ids") or []):
        raise RuntimeError(f"spec_authority_drift:{spec_ids}")
    logo_paths = logo_mutation_paths(specs)
    declared_mutation_paths = DECLARED_MUTATION_PATHS | logo_paths

    sheet_path = Path(args.sheet).resolve()
    sheet = read_json(sheet_path)
    if sheet.get("status") != "PASS" or (sheet.get("schema_audit") or {}).get("issues"):
        raise RuntimeError("sheet_snapshot_invalid")
    rows = {int(row["row"]): row for row in sheet["selected_rows"]}

    tracking: dict[str, str] = {}
    hosts: dict[str, str] = {}
    publishers: dict[str, str] = {}
    for spec in specs:
        row = rows.get(spec["sheet_row"])
        if not row:
            raise RuntimeError(f"sheet_row_missing:{spec['sheet_row']}")
        if row.get("Status") != "approved_active_tracking_ready":
            raise RuntimeError(f"sheet_selection_gate_status:{spec['sheet_row']}:{row.get('Status')}")
        # `Apply?` is an application-queue instruction, not deployment authority.
        # Approved rows may correctly set it FALSE after the relationship exists;
        # requiring it here rejected MEDVI even though Status, signup, tracking and
        # Wire-In Ready all proved the row deployable. Keep the two completion
        # gates fail-closed and let Status carry the approval decision.
        for gate in ("Signup Complete?", "Wire-In Ready?"):
            if str(row.get(gate)).strip().upper() != "TRUE":
                raise RuntimeError(f"sheet_selection_gate:{spec['sheet_row']}:{gate}")
        if str(row.get("Network / Route") or "").strip().casefold() != spec["network"].casefold():
            raise RuntimeError(f"sheet_network_drift:{spec['sheet_row']}")
        url = str(row.get("Tracking Link / Deep Link") or "").strip()
        tracking_form = spec.get("tracking_form", "query_offer_publisher")
        # A merchant-hosted rail's allowed host is resolved by the SAME rule the
        # wire-in-ready gate applied before this row was ever queued: the spec's
        # declared `tracking_host` if it has one, else its own `official_url`.
        # Reading only `tracking_host` is what killed the Trackdesk build eight
        # attempts running while the gate had already passed the row.
        try:
            allowed_host = _spec_allowed_host(spec["network"], spec) or None
        except _RailError as exc:
            raise RuntimeError(str(exc)) from None
        host, query = validate_tracking(spec["network"], _tracking_validation_id(spec), url,
                                        allowed_host=allowed_host,
                                        tracking_form=tracking_form)
        if spec["network"].startswith("Direct"):
            expected_publisher, publisher_params = None, ("affid",)
        elif spec["network"] not in PUBLISHER_ID_BY_NETWORK:
            raise RuntimeError(f"network_publisher_contract_missing:{spec['network']}")
        else:
            expected_publisher, publisher_params = PUBLISHER_ID_BY_NETWORK[spec["network"]]
        if spec["network"] == "MaxBounty":
            # Keep empty/duplicate attribution values visible so they cannot
            # silently fall back to a declared identity.
            query = parse_qs(urlparse(url).query, keep_blank_values=True)
        if expected_publisher is None:
            # A direct-affiliate program issues its own publisher id, so there is
            # no network-wide constant to pin. The spec must declare the id the
            # provider issued us and the link must carry exactly that.
            expected_publisher = str(spec.get("publisher_id") or "")
            if spec["network"] == "MaxBounty":
                expected_publisher = expected_publisher.strip()
                if "a" in query:
                    affiliate_ids = query["a"]
                    if len(affiliate_ids) != 1 or not affiliate_ids[0].strip():
                        raise RuntimeError(f"publisher_id_drift:{spec['sheet_row']}:{spec['network']}")
                    if expected_publisher and expected_publisher != affiliate_ids[0]:
                        raise RuntimeError(f"publisher_id_drift:{spec['sheet_row']}:{spec['network']}")
                    if not expected_publisher:
                        expected_publisher = affiliate_ids[0]
            if not expected_publisher:
                raise RuntimeError(f"spec_publisher_id_missing:{spec['sheet_row']}")
        if tracking_form == "path_referral_code":
            # The issued code IS the publisher identity; validate_tracking has
            # already asserted it is the link's last path segment.
            if expected_publisher != spec["advertiser_id"]:
                raise RuntimeError(f"publisher_id_drift:{spec['sheet_row']}:{spec['network']}")
        elif _rail_for(spec["network"]) is not None and \
                _rail_for(spec["network"]).surfaces_publisher_id_in_path:
            # CJ (`/click-<publisher>-<link>`) and the Everflow-client rail
            # (`/<affiliate_token>/<offer_token>/`) carry our identity in the
            # PATH, not the query. `validate_link` has already pinned it there
            # against the rail's own constant, so demanding a query param would
            # reject every genuine link on those rails.
            if expected_publisher and f"/{expected_publisher}" not in url \
                    and f"-{expected_publisher}-" not in url:
                raise RuntimeError(f"publisher_id_drift:{spec['sheet_row']}:{spec['network']}")
        elif spec["network"] == "MaxBounty" and "a" not in query:
            # Opaque provider-issued links may rely on the declared spec id.
            # Absence of BOTH sources already failed above; path checks stay first.
            pass
        elif not any(query.get(param) == [expected_publisher] for param in publisher_params):
            raise RuntimeError(f"publisher_id_drift:{spec['sheet_row']}:{spec['network']}")
        publishers[spec["program_id"]] = expected_publisher
        tracking[spec["program_id"]] = url
        hosts[spec["program_id"]] = host

    if os.path.lexists(target):
        raise RuntimeError(f"target_must_not_exist:{target}")
    ignore = shutil.ignore_patterns(*MATERIALIZATION_IGNORE_NAMES)
    source_before = tree_content_closure(source, ignore=ignore)
    try:
        # public/ MUST come along: scripts/build-staging.mjs reads already-published
        # pages back out of it (the SEO machine's guide articles). Unchanged files
        # remain predecessor hardlinks; only declared writers are detached.
        materialization = copytree_linked(source, target, ignore=ignore)
        opening = tree_content_closure(target, ignore=ignore)
        if opening["tree_sha256"] != source_before["tree_sha256"]:
            raise RuntimeError("linked_materialization_opening_closure_mismatch")
        before = tree_hashes(target)
        detachment = detach_declared_mutations(target, declared_mutation_paths)
        logo_staging = stage_bound_logos(target, specs)

        print(json.dumps({"build_source": str(source.resolve()),
                          "build_target": str(target.resolve()),
                          "sheet_rows": [spec["sheet_row"] for spec in specs]}), flush=True)
        notes = apply_changes(target, specs, tracking, publishers,
                              scope.get("activation_at_utc") or now(), sheet_path,
                              research_dir)
        notes["decimal_price_quote_validation"] = (
            normalize_decimal_price_quote_validation(target)
        )
        notes["inline_guide_source_markers"] = normalize_inline_guide_source_markers(target)
        notes["paid_landing_evidenced_rail"] = normalize_paid_landing_evidenced_rail_selection(target)
        # SAME RUN, not a follow-up mission: research merge -> surface integration.
        # A provider that reaches the registry in this build reaches the comparison
        # page, the quiz and (where a comparable price exists) the calculator in this
        # build too. Skipping this step is what produced the review-only tier.
        surface_integration = (
            {
                "status": "DEFERRED_TO_CALLER",
                "reason": "provider_dynamic_profile_must_precede_shared_surface_integration",
                "external_effects": {"deployments": 0, "provider_calls": 0, "tracking_clicks": 0},
            }
            if args.defer_surface_integration
            else run_surface_integration(target)
        )

        after = tree_hashes(target)
        changed_nonpublic = sorted(
            path for path in set(before) | set(after) if before.get(path) != after.get(path)
        )
        unexpected_nonpublic = sorted(set(changed_nonpublic) - declared_mutation_paths)
        if unexpected_nonpublic:
            raise RuntimeError(f"unexpected_source_change:{unexpected_nonpublic}")

        provenance = constructor_provenance(
            constructor=Path(__file__).resolve(),
            predecessor=source,
            output=target,
            mutation_paths=[Path(path) for path in sorted(declared_mutation_paths)],
            materializations=[materialization],
            predecessor_before=source_before,
            ignored_names=MATERIALIZATION_IGNORE_NAMES,
        )
        topology = provenance["topology"]
        changed = sorted(set(topology["added_paths"]) | set(topology["modified_paths"]))
        unexpected = sorted(set(changed) - declared_mutation_paths)
        if unexpected:
            raise RuntimeError(f"unexpected_source_change:{unexpected}")
        asset_changes = [path for path in changed if path.startswith("assets/")]
        unexpected_assets = sorted(set(asset_changes) - logo_paths)
        if unexpected_assets:
            raise RuntimeError(f"unexpected_asset_change:{unexpected_assets}")
    except Exception:
        remove_materialized_tree(target)
        raise

    receipt = {
        "schema": "g6-r1a-incremental-wirein-build-v1",
        "status": "PASS_CANDIDATE_SOURCE",
        "built_at_utc": now(),
        "source": str(source),
        "source_tree_sha256": source_before["tree_sha256"],
        "source_files": source_before["files"],
        "source_bytes": source_before["bytes"],
        "target": str(target),
        "authority_path": str(authority_path),
        "authority_sha256": authority_sha,
        "mission_authority_sha256": authority.get("mission_authority_sha256"),
        "spec_path": str(spec_path),
        "spec_sha256": file_sha(spec_path),
        "research_dir": str(research_dir),
        "research_sha256": {
            spec["program_id"]: file_sha(research_dir / spec["research_file"])
            for spec in specs
        },
        "sheet_snapshot_sha256": file_sha(sheet_path),
        "bridge_ruling_considered": "terms-document-hierarchy-20260806",
        "newly_wired_programs": [{
            "program_id": s["program_id"],
            "display_name": s["display_name"],
            "sheet_row": s["sheet_row"],
            "network": s["network"],
            "network_program_id": s["advertiser_id"],
            "lane": "fastpath_review_transparent_direct",
            "tracking_host": hosts[s["program_id"]],
            "tracking_link_sha256": sha_bytes(tracking[s["program_id"]].encode()),
            "tracking_clicks": 0,
        } for s in specs],
        "changed_files": changed,
        "changed_nonpublic_files": changed_nonpublic,
        "unexpected_changed_files": unexpected,
        "asset_changes": asset_changes,
        "logo_staging": logo_staging,
        "materialization": materialization,
        "detachment": detachment,
        "predecessor_unchanged": provenance["predecessor"],
        "output_closure": provenance["output"],
        "constructor_provenance": provenance,
        "counts": notes,
        "held_effects": {"new_money_pages": 0, "template_changes": 0, "renderer_source_patches": 0,
                         "higgsfield_asset_changes": 0, "email_sends": 0, "lead_sharing": 0,
                         "spend_usd": 0, "tracking_clicks": 0},
    }
    receipt["surface_integration"] = surface_integration
    write_json(report / "06-incremental-wirein-build-receipt-r2.json", receipt)
    print(json.dumps({"status": receipt["status"], "changed_files": len(changed),
                      "target": str(target), "counts": notes}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
