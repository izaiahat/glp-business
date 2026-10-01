"""Offline real-adapter regression; no builder, job, or full JS module execution.

The source fixture is the byte-for-byte accepted base's postbuild script from
calendar-source-adoption/OWNER-INPUTS.json, authenticated by its release row.
Only the emitted date resolvers and existing material-date selection execute.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
BASE_SHA256 = "a034fd24091cf093637b20753708f2f3c2070c29f66176e5deabfeb10aee9ba9"
REVIEWED_RESOLVER_SHA256 = "04ccd89a23b92ab4416743a70ba6b9ba70c71d2cd8984716f3cf7c4bb79a19b4"
CANDIDATE_SHA256 = "313558350e761a244dd8fc1cc2621e9175e02925e844135f60864147f1b79b5b"


def test_real_carrier_propagates_reviewed_calendar_hunk_and_preserves_inherited_dates(tmp_path):
    fixture = ROOT / "tests/fixtures/glp76-carrier-material/postbuild-aeo-surface.mjs"
    base_bytes = fixture.read_bytes()
    assert len(base_bytes) == 29337
    assert hashlib.sha256(base_bytes).hexdigest() == BASE_SHA256
    base_copy = tmp_path / "accepted-base.mjs"
    base_copy.write_bytes(base_bytes)
    base_copy.chmod(0o444)
    before = base_copy.stat()

    helper = ROOT / "systems/seo-blog-machine/scripts/carrier_fixture_contracts.py"
    spec = importlib.util.spec_from_file_location("glp76_reviewed_carrier", helper)
    carrier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(carrier)
    source = base_copy.read_text()
    # This base already has the accepted binding and evolved inherited caller.
    # The production seam must retain them regardless of the supplied binding.
    candidate = carrier.patch_material_date_resolver(
        source, {"material_dates": {"/unrelated/": "2026-09-01"}})
    candidate_path = tmp_path / "candidate.mjs"
    candidate_path.write_text(candidate)

    program = r'''
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
const candidate = readFileSync(process.argv[1], "utf8");
const original = readFileSync(process.argv[2], "utf8");
const extract = (source, name) => {
  const matches = [...source.matchAll(new RegExp("export function " + name + "\\([^]*?\\n}\\n", "g"))];
  assert.equal(matches.length, 1);
  return matches[0][0];
};
const material = extract(candidate, "resolveMaterialDate");
const catalog = extract(candidate, "resolveCatalogDate");
const { resolveMaterialDate, resolveCatalogDate } = await import(
  "data:text/javascript;base64," + Buffer.from(material + catalog).toString("base64"));
const route = "/guides/carrier-calendar/";
const resolved = { lastmod: "2024-02-01", action: "retained_material" };
const release = "2026-09-30";
// Run the behavioral negative first so predecessor RED identifies the defect.
for (const invalid of ["2025-02-29", "2026-02-30", "2026-04-31", "1900-02-29",
                       "2026-00-01", "2026-13-01", "not-a-date", "2026-2-01", "2026-10-01"]) {
  assert.throws(() => resolveMaterialDate(route, invalid, resolved, release),
    { message: "lastmod_material_date_future_or_invalid:" + route }, invalid);
}
assert.deepEqual(resolveMaterialDate(route, "2024-02-29", resolved, release),
  { lastmod: "2024-02-29", action: "declared_material_page_edit" });
assert.deepEqual(resolveMaterialDate(route, release, resolved, release),
  { lastmod: release, action: "declared_material_page_edit" });
for (const retained of [null, "", "2024-01-31", "2024-02-01"]) {
  assert.equal(resolveMaterialDate(route, retained, resolved, release), resolved);
}
const bindingLine = candidate.match(/^    const materialDate = .*;$/m)[0];
const acceptedLine = candidate.match(/^const CARRIER_ACCEPTED_MATERIAL_DATES = .*;$/m)[0];
assert.ok(bindingLine.includes("inheritedDisclosureDates(route, articles.get(route))?.lastmod"));
assert.equal(bindingLine, original.match(/^    const materialDate = .*;$/m)[0]);
assert.equal(acceptedLine, original.match(/^const CARRIER_ACCEPTED_MATERIAL_DATES = .*;$/m)[0]);
const select = new Function("materialDates", "inheritedDisclosureDates", "articles", "route",
  acceptedLine + "\n" + bindingLine + "\nreturn materialDate;");
const articles = new Map([[route, { canonical_path: route, updated_at: "2024-02-01" }]]);
const inherited = () => ({ lastmod: "2024-02-29" }); // synthetic witness; real emitted selection
const inheritedDate = select(new Map(), inherited, articles, route);
assert.equal(inheritedDate, "2024-02-29");
assert.equal(select(new Map([[route, "2024-03-01"]]), inherited, articles, route), "2024-03-01");
const accepted = new Function(acceptedLine + "\nreturn CARRIER_ACCEPTED_MATERIAL_DATES;")();
const [acceptedRoute, acceptedDate] = Object.entries(accepted)[0];
assert.equal(select(new Map(), () => null, new Map(), acceptedRoute), acceptedDate);
const owned = resolveCatalogDate(route, articles.get(route), inheritedDate, resolved, inheritedDate, release);
assert.deepEqual(owned, { lastmod: "2024-02-01", action: "catalog_owned_editorial_date" });
assert.deepEqual(resolveMaterialDate(route, inheritedDate, owned, release),
  { lastmod: inheritedDate, action: "declared_material_page_edit" });
assert.throws(() => resolveCatalogDate(route, articles.get(route), inheritedDate, resolved, null, release),
  { message: "catalog_render_date_mismatch:" + route });
assert.equal(catalog, extract(original, "resolveCatalogDate"));
assert.equal(candidate.replace(material, ""), original.replace(extract(original, "resolveMaterialDate"), ""));
console.log(JSON.stringify({ resolver_sha256: createHash("sha256").update(material).digest("hex"),
  invalid_dates_refused: 9, inherited_selection_preserved: true, unrelated_bytes_preserved: true }));
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e", program, str(candidate_path), str(base_copy)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    proof = json.loads(result.stdout)
    assert proof["resolver_sha256"] == REVIEWED_RESOLVER_SHA256
    assert proof["invalid_dates_refused"] == 9
    assert proof["inherited_selection_preserved"] and proof["unrelated_bytes_preserved"]
    assert hashlib.sha256(candidate.encode()).hexdigest() == CANDIDATE_SHA256
    assert carrier.patch_material_date_resolver(candidate) == candidate
    # Anchor custody must refuse unknown or duplicated resolver bytes.
    start = candidate.index("export function resolveMaterialDate(")
    end = candidate.index("\n}\n", start) + len("\n}\n")
    hunk = candidate[start:end]
    old_start = source.index("export function resolveMaterialDate(")
    old_end = source.index("\n}\n", old_start) + len("\n}\n")
    for drifted in (candidate.replace(hunk, ""), candidate + hunk, candidate + source[old_start:old_end],
                    candidate.replace('const materialTimestamp = Date.parse(', 'const materialTimestamp = Date.now(')):
        with pytest.raises(RuntimeError, match="material_calendar_resolver_anchor_invalid"):
            carrier.patch_material_date_resolver(drifted)
    assert base_copy.read_bytes() == base_bytes == fixture.read_bytes()
    after = base_copy.stat()
    assert (after.st_ino, after.st_mtime_ns, after.st_mode) == (before.st_ino, before.st_mtime_ns, before.st_mode)
