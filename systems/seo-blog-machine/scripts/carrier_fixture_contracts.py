"""Acceptance-backed carrier fixture adapters; never rewrite catalog/public input."""
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse


def once(source, old, new, label):
    if source.count(new) == 1 and old not in source.replace(new, '', 1):
        return source
    if source.count(old) != 1:
        raise RuntimeError(label + '_anchor_invalid')
    return source.replace(old, new, 1)


def material_binding(base):
    base=Path(base)
    files=['tests/fixtures/d2j-material-lastmod.json','data/page-content-provenance.json',
           'data/blog/content-batch.json','data/blog/content-national-wow-additions.json']
    inputs={f:hashlib.sha256((base/f).read_bytes()).hexdigest() for f in files}
    declared=json.loads((base/files[0]).read_text())
    if declared['schema']!='d2j-material-date-fixtures-v1':
        raise RuntimeError('accepted_material_fixture_schema')
    rows={r['route']:r for r in declared['rows']}
    if len(rows)!=len(declared['rows']): raise RuntimeError('accepted_material_duplicate')
    provenance=json.loads((base/files[1]).read_text())['pages']
    material={}; seen=set(); dates=[]
    for file in files[2:]:
        for article in json.loads((base/file).read_text())['articles']:
            route=article['canonical_path']
            if route in seen: raise RuntimeError('accepted_material_catalog_duplicate:'+route)
            seen.add(route)
            editorial=article['updated_at']; dt.date.fromisoformat(editorial);dates.append(editorial)
            page=base/'public'/route.lstrip('/')/'index.html'
            if not page.exists(): continue
            html=page.read_text()
            found=re.findall(r'"dateModified"\s*:\s*"(\d{4}-\d{2}-\d{2})"',html)
            if not found: continue
            rendered=max(found)
            if rendered==editorial: continue
            row=rows.get(route,{}); recorded=provenance.get(route,{})
            if row.get('required_date')!=rendered or row.get('old_date')!=editorial or recorded.get('lastmod')!=rendered:
                raise RuntimeError('accepted_material_unexplained:'+route)
            material[route]=rendered; dates.append(rendered)
            inputs['public/'+route.lstrip('/')+'index.html']=hashlib.sha256(page.read_bytes()).hexdigest()
    dates.extend(p['lastmod'] for p in provenance.values())
    for date in dates: dt.date.fromisoformat(date)
    return {'material_dates':material,'input_sha256':inputs,'fixture_baseline':max(dates)}


def material_resolver_call_satisfied(source):
    """Accept a LATER evolution of this builder's own material-date wiring.

    ``once`` recognises exactly two states: the predecessor it replaces and the
    byte-exact successor it writes. The accepted base sealed on 2026-09-11 carries
    a third one - the same wiring plus an ``inheritedDisclosureDates(...)`` term in
    the ``materialDate`` binding - so the byte-exact check saw neither state and
    ``material_resolver_call_anchor_invalid`` aborted the entire publication on
    2026-09-17. Check the contract this patch exists to guarantee rather than its
    bytes: the accepted-material fallback is consulted, and both resolvers receive
    the resolved material date. An unpatched source satisfies none of the three.
    """
    binding = re.search(r'\n[ \t]*const materialDate = [^\n]*\n', source)
    return bool(
        binding
        and 'CARRIER_ACCEPTED_MATERIAL_DATES[route]' in binding.group(0)
        and 'resolveCatalogDate(route, articles.get(route), declaredDateModified(html), '
            'resolved, materialDate, releaseDate)' in source
        and 'resolveMaterialDate(route, materialDate, ownedDate, releaseDate)' in source
    )


def patch_material_date_resolver(source, binding=None):
    # With no binding this exercises the resolver only; production constructor
    # supplies a binding derived from the already-manifest-verified base.
    binding=binding or {'material_dates':{}}
    marker='// K6c: accepted inherited material dates, not change declarations.\n'
    if marker not in source:
        anchor='export function resolveCatalogDate(route, article, renderedDate, resolved) {'
        header=marker+'const CARRIER_ACCEPTED_MATERIAL_DATES = Object.freeze('+json.dumps(binding['material_dates'],sort_keys=True)+');\n'
        source=once(source,anchor,header+'export function resolveCatalogDate(route, article, renderedDate, resolved, materialDate = null, releaseDate = null) {','material_resolver_signature')
    source=once(source,
        '  if (renderedDate !== article.updated_at) throw new Error(`catalog_render_date_mismatch:${route}`);',
        '  if (renderedDate !== article.updated_at && !(materialDate === renderedDate && materialDate > article.updated_at && /^\\d{4}-\\d{2}-\\d{2}$/.test(materialDate) && releaseDate && materialDate <= releaseDate)) throw new Error(`catalog_render_date_mismatch:${route}`);',
        'material_rendered_date')
    if not material_resolver_call_satisfied(source):
        source=once(source,
            '    const ownedDate = resolveCatalogDate(route, articles.get(route), declaredDateModified(html), resolved);\n    const { lastmod, action } = resolveMaterialDate(route, materialDates.get(route), ownedDate, releaseDate);',
            '    const materialDate = materialDates.get(route) || CARRIER_ACCEPTED_MATERIAL_DATES[route] || null;\n    const ownedDate = resolveCatalogDate(route, articles.get(route), declaredDateModified(html), resolved, materialDate, releaseDate);\n    const { lastmod, action } = resolveMaterialDate(route, materialDate, ownedDate, releaseDate);',
            'material_resolver_call')
    return source


def patch_lastmod_material_fixture(source, base):
    binding=material_binding(base)
    baseline=binding['fixture_baseline']
    release=(dt.date.fromisoformat(baseline)+dt.timedelta(days=1)).isoformat()
    block=f'// K6c fixture clocks from actual catalog and accepted declarations.\nconst CARRIER_FIXTURE_BASELINE = "{baseline}";\nconst RELEASE = "{release}";'
    pattern=r'// K6c fixture clocks from actual catalog and accepted declarations\.\nconst CARRIER_FIXTURE_BASELINE = "\d{4}-\d{2}-\d{2}";\nconst RELEASE = "\d{4}-\d{2}-\d{2}";'
    if re.search(pattern,source):
        if len(re.findall(pattern,source))!=1: raise RuntimeError('fixture_clock_duplicate')
        source=re.sub(pattern,lambda match:block,source)
    else:
        source=once(source,'const RELEASE = "2026-08-21";',block,'fixture_release_clock')
    # Three independently minted baselines, never the actual public tree.
    old='GLP_RELEASE_DATE: "2026-08-20"';new='GLP_RELEASE_DATE: CARRIER_FIXTURE_BASELINE'
    if old in source:
        if source.count(old)!=3: raise RuntimeError('fixture_baseline_clock_anchor_invalid')
        source=source.replace(old,new)
    elif source.count(new)!=3: raise RuntimeError('fixture_baseline_clock_successor_invalid')
    return source


def patch_preserved_export_fixture(source):
    old='  assert.deepEqual(candidateExportShellFindings(html), []);'
    new='''  // Test the accepted predecessor stage on this byte-preserved review.
  const acceptedReview = await readFile(path.join(CUMULATIVE, "reviews/trt-nation/index.html"), "utf8");
  assert.equal(html, acceptedReview, "unrelated review must remain byte-identical to the accepted base");
  const acceptedPolicy = acceptedReview.match(/window\\.GCS_EXPORT_POLICY=Object\\.freeze\\((\\{[^<]*\\})\\);/)?.[1];
  assert.ok(acceptedPolicy, "accepted base policy missing");
  const inheritedStage = JSON.parse(acceptedPolicy).stage;
  assert.deepEqual(candidateExportShellFindings(html, {stage: inheritedStage}), []);'''
    source=once(source,old,new,'preserved_review_stage')
    return once(source,
        'candidateExportShellFindings(wrongAnalyticsStage).includes("analytics_wrong_stage")',
        'candidateExportShellFindings(wrongAnalyticsStage, {stage: inheritedStage}).includes("analytics_wrong_stage")',
        'preserved_review_hostile_stage')


BUSINESS = Path('/home/ubuntu/business')
CANDIDATE_REGISTRY_GLOB = 'site-system/flagship-v2/candidates/*/data/program-registry.json'
TAXONOMY_TAG_FIELDS = ('molecule_tags', 'brand_name_tags', 'delivery_form_tags', 'treatment_type_tags')
TAXONOMY_ISO_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
TAXONOMY_REVERIFIED_CONST = 'CARRIER_TAXONOMY_REVERIFIED'


def _taxonomy_capture_proven(source, business):
    """Authenticate the producer's capture bytes and its per-source assertions."""
    relative = source.get('capture_path')
    digest = source.get('capture_sha256')
    url = source.get('source_url')
    if (not isinstance(relative, str) or not relative.startswith('reports/')
            or not isinstance(digest, str) or re.fullmatch(r'[0-9a-f]{64}', digest) is None
            or not isinstance(url, str) or urlparse(url).scheme != 'https'
            or not urlparse(url).hostname):
        return False
    root = business.resolve()
    capture_path = business / relative
    if (capture_path.is_symlink() or not capture_path.is_file()
            or root not in capture_path.resolve().parents):
        return False
    try:
        raw = capture_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            return False
        receipt = json.loads(raw)
        start = dt.datetime.fromisoformat(receipt['started_at'].replace('Z', '+00:00'))
        received = dt.datetime.fromisoformat(receipt['received_at'].replace('Z', '+00:00'))
        if (start.tzinfo is None or received.tzinfo is None or received < start
                or receipt['request']['url'] != url
                or receipt['request'].get('maxAge') != 0
                or receipt['request'].get('formats') != ['markdown', 'links']
                or source.get('captured_at') != receipt['received_at']
                or source.get('verified_as_of') != received.date().isoformat()):
            return False
        response = receipt['response']
        data = response['data']
        meta = data['metadata']
        markdown = data['markdown']
        if (response.get('success') is not True or meta.get('statusCode') != 200
                or not isinstance(markdown, str) or not markdown
                or str(meta.get('cacheState', '')).lower() in ('hit', 'cached')):
            return False
        for key in ('sourceURL', 'url'):
            if meta.get(key) and urlparse(meta[key]).hostname != urlparse(url).hostname:
                return False
        if meta.get('cachedAt') and dt.datetime.fromisoformat(
                meta['cachedAt'].replace('Z', '+00:00')) < start:
            return False
        assertions = source.get('assertions')
        if not isinstance(assertions, dict):
            return False
        plain = re.sub(r'\s+', ' ', markdown.replace('*', '').replace('#', '')).strip().casefold()
        for field in TAXONOMY_TAG_FIELDS:
            rows = assertions.get(field)
            if not isinstance(rows, dict):
                return False
            for proof in rows.values():
                if not isinstance(proof, dict) or not isinstance(proof.get('quote'), str):
                    return False
                quote = re.sub(r'\s+', ' ', proof['quote'].replace('*', '').replace('#', '')).strip().casefold()
                if not quote or quote not in plain:
                    return False
                pos = plain.find(quote)
                context = plain[max(0, pos - 80):pos + len(quote) + 100]
                if proof.get('context') != context:
                    return False
                if re.search(r"\b(unavailable|discontinued|not (?:available|offered|prescribed|provided)|(?:do(?:es)? not|don['’]t|doesn['’]t|never|no longer)\s+(?:offer|prescrib|provid|supply|sell|carry|avail)\w*)\b", context):
                    return False
        return True
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False


def _taxonomy_manifest_sources_proven(manifest, business, frozen_row, stamp):
    sources = manifest.get('sources')
    if sources is None:
        sources = [manifest]  # producer's single-source format
    if not isinstance(sources, list) or not sources:
        return False
    merged = {field: {} for field in TAXONOMY_TAG_FIELDS}
    for source in sources:
        if (not isinstance(source, dict) or not isinstance(source.get('verified_as_of'), str)
                or source['verified_as_of'] < stamp
                or (frozen_row.get('source_urls') and source.get('source_url') not in frozen_row['source_urls'])
                or not _taxonomy_capture_proven(source, business)):
            return False
        for field in TAXONOMY_TAG_FIELDS:
            for tag, proof in source['assertions'][field].items():
                if tag in merged[field]:
                    return False
                merged[field][tag] = proof
    if merged != manifest.get('assertions'):
        return False
    return all(set(merged[field]) == set(frozen_row.get(field) or [])
               for field in TAXONOMY_TAG_FIELDS)


def taxonomy_reverification_binding(base, business=BUSINESS):
    """Freshness clock for a FROZEN taxonomy row, from the authenticated evidence beside it.

    ``therapeutic_taxonomy.programs[pid].verified_as_of`` is frozen into whatever tree
    gets sealed, and NOTHING in the carrier's registered path can advance it in an
    accepted base: ``reconcileTherapeuticTaxonomyWireIn`` can only raise it from
    ``program.research_therapeutic_taxonomy``, which no registry row carries (0 of 60),
    and the one registered producer that does refresh these rows
    (``candidates/*/scripts/refresh-therapeutic-taxonomy.py``, registered in
    ops/registered-producers.json) writes the CANDIDATE registry, which no publish path
    reads. Requiring that frozen value to be fresh therefore guarantees a total
    publication stop ``freshness_max_days`` after every seal - the 2026-09-17 outage, and
    the same bug class as the 2026-09-16 pinned-capture and price-tracker stops.

    The fix keeps the frozen value - the sealed tree's rendered date does not move, so no
    published byte changes - and takes the freshness clock from the authenticated live
    re-verification recorded beside it. A stamp is admitted ONLY when:

    * a candidate registry's evidence binding names an authority resolving to a
      ``reports/...`` manifest, and that file's sha256 equals the recorded one;
    * the manifest's own ``program_id`` and ``verified_as_of`` agree with the binding;
    * the manifest's ``tags_unchanged`` equals THIS release's frozen row field for field,
      so a re-verification of different claims can never lend its clock;
    * every producer source has a hash-verified successful fresh response, matching
      source/time and captured quote assertions, and the live evidence binding pins
      the matching capture and authority hashes;
    * and the stamp is strictly newer than the frozen value.

    Anything else is ignored, so an unrefreshed row still fails the gate loudly.
    ``freshness_max_days`` is never widened and no sealed byte is ever edited.
    """
    base = Path(base)
    business = Path(business)
    frozen = json.loads((base / 'data/program-registry.json').read_text())
    frozen_rows = (frozen.get('therapeutic_taxonomy') or {}).get('programs') or {}
    stamps, evidence = {}, []
    for registry_path in sorted(business.glob(CANDIDATE_REGISTRY_GLOB)):
        if registry_path.is_symlink():
            continue
        try:
            document = json.loads(registry_path.read_text())['therapeutic_taxonomy']
        except (OSError, ValueError, KeyError):
            continue
        authorities = document.get('authorities') or {}
        for pid, row in (document.get('programs') or {}).items():
            frozen_row = frozen_rows.get(pid)
            if not isinstance(frozen_row, dict) or not isinstance(row, dict):
                continue
            frozen_stamp = str(frozen_row.get('verified_as_of') or '')
            for binding in row.get('evidence_bindings') or []:
                if not isinstance(binding, dict):
                    continue
                stamp = str(binding.get('verified_as_of') or '')
                authority = authorities.get(binding.get('authority'))
                if not TAXONOMY_ISO_DATE.match(stamp) or stamp <= frozen_stamp:
                    continue
                if not isinstance(authority, dict):
                    continue
                relative = str(authority.get('path') or '')
                if not relative.startswith('reports/') or '..' in relative:
                    continue
                manifest_path = business / relative
                if manifest_path.is_symlink() or not manifest_path.is_file():
                    continue
                if hashlib.sha256(manifest_path.read_bytes()).hexdigest() != authority.get('sha256'):
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text())
                except ValueError:
                    continue
                if manifest.get('program_id') != pid or str(manifest.get('verified_as_of') or '') != stamp:
                    continue
                tags = manifest.get('tags_unchanged')
                if not isinstance(tags, dict):
                    continue
                if any(sorted(tags.get(field) or []) != sorted(frozen_row.get(field) or [])
                       for field in TAXONOMY_TAG_FIELDS):
                    continue
                if (binding.get('authority_sha256') != authority.get('sha256')
                        or not _taxonomy_manifest_sources_proven(manifest, business, frozen_row, stamp)):
                    continue
                sources = manifest.get('sources', [manifest])
                if not any(source.get('source_url') == binding.get('source_url')
                           and source.get('verified_as_of') == stamp
                           and source.get('capture_sha256') == binding.get('evidence_sha256')
                           for source in sources):
                    continue
                if stamp <= stamps.get(pid, ''):
                    continue
                stamps[pid] = stamp
                evidence.append({
                    'program_id': pid,
                    'frozen_verified_as_of': frozen_stamp,
                    'reverified_as_of': stamp,
                    'authority': binding.get('authority'),
                    'manifest': relative,
                    'manifest_sha256': authority.get('sha256'),
                    'captured_at': manifest.get('captured_at'),
                    'source_url': manifest.get('source_url'),
                    'index': str(registry_path.relative_to(business)),
                })
    return {'schema': 'glp-carrier-taxonomy-reverification-v1', 'stamps': stamps,
            'evidence': sorted(evidence, key=lambda entry: entry['program_id'])}


TAXONOMY_CLOCK_ANCHOR = '    const age = ageDays(row.verified_as_of, asOf);'
TAXONOMY_CLOCK_REPLACEMENT = '''    // The frozen stamp still renders; only the freshness clock may advance, and only
    // on evidence that re-confirmed this row's own tags.
    const reverifiedAsOf = CARRIER_TAXONOMY_REVERIFIED[pid] || null;
    const freshnessAsOf = (ISO_DATE.test(String(reverifiedAsOf || "")) && reverifiedAsOf > row.verified_as_of)
      ? reverifiedAsOf
      : row.verified_as_of;
    const age = ageDays(freshnessAsOf, asOf);'''
TAXONOMY_DECLARATION_ANCHOR = 'export function validateTherapeuticTaxonomy('


def patch_taxonomy_freshness_clock(source, binding=None):
    """Measure taxonomy freshness against the authenticated re-verification rather than
    the frozen stamp. With an empty binding this is a behavioural no-op."""
    literal = json.dumps(dict((binding or {}).get('stamps') or {}), sort_keys=True)
    constant = 'const ' + TAXONOMY_REVERIFIED_CONST + ' = Object.freeze(' + literal + ');\n'
    existing = re.search(
        r'const ' + TAXONOMY_REVERIFIED_CONST + r' = Object\.freeze\(\{[^\n]*\}\);\n', source)
    if existing:
        source = source[:existing.start()] + constant + source[existing.end():]
    else:
        header = ('// K10-023-B carrier binding: {program_id: reverified_as_of}. Every entry is\n'
                  '// proven by a sha256-bound live re-verification manifest under reports/ whose\n'
                  '// tags equal this release\'s own frozen row. Empty outside a carrier build.\n')
        source = once(source, TAXONOMY_DECLARATION_ANCHOR,
                      header + constant + TAXONOMY_DECLARATION_ANCHOR,
                      'taxonomy_reverified_declaration')
    return once(source, TAXONOMY_CLOCK_ANCHOR, TAXONOMY_CLOCK_REPLACEMENT, 'taxonomy_freshness_clock')


TRACKER_IMPORT_ANCHOR = 'import { createHash } from "node:crypto";\n'
TRACKER_IMPORT_ADDITION = (
    'import { fileURLToPath as trackerFileURLToPath } from "node:url";\n'
    'import { loadEvidenceInputs as trackerLoadEvidenceInputs,'
    ' materialQualificationsFor as trackerMaterialQualificationsFor }'
    ' from "/home/ubuntu/business/scripts/glp_evidence_ledger.mjs";\n'
    '''// K10-023-B: a sealed row's checked_at is FROZEN and nothing in the carrier's
// registered path can advance it in an accepted base, so requiring it to be fresh
// stops every publication 45 days after each seal. The displayed checked_at stays
// exactly as sealed - that IS when this price was checked, and nothing rendered
// moves - while the AGE is measured against the authenticated re-capture of the
// same quote at the same url, which the registered evidence refresh cycle writes.
function trackerEvidenceClocks(root) {
  const { ledger } = trackerLoadEvidenceInputs(root);
  const clocks = new Map();
  const remember = (captureId, at, url, quoteSha) => {
    if (!captureId || typeof at !== "string" || !url || !quoteSha) return;
    const previous = clocks.get(captureId);
    if (!previous || Date.parse(at) > Date.parse(previous.at)) clocks.set(captureId, { at, url, quoteSha });
  };
  for (const record of ledger.records || []) {
    if (record.disposition !== "ACCEPTED") continue;
    remember(record.capture_id, record.cached_at, record.requested_url, record.exact_quote_sha256);
  }
  for (const qualification of trackerMaterialQualificationsFor(ledger)) {
    const material = qualification.material || {};
    const source = (ledger.records || []).find((row) => row.capture_id === material.capture_id);
    if (!source || source.disposition !== "ACCEPTED") continue;
    remember(material.capture_id, qualification.fetched_at, source.requested_url, source.exact_quote_sha256);
  }
  return clocks;
}
'''
)
TRACKER_CLOCK_ANCHOR = (
    '    const checkedMs = Date.parse(record.checked_at);\n'
    '    if (!Number.isFinite(checkedMs) || checkedMs > nowMs || nowMs - checkedMs > MAX_AGE_MS)'
    ' throw new Error("tracker_freshness_invalid");\n'
)
TRACKER_CLOCK_REPLACEMENT = '''    const checkedMs = Date.parse(record.checked_at);
    if (!Number.isFinite(checkedMs) || checkedMs > nowMs) throw new Error("tracker_timestamp_invalid");
    // Only a re-capture that proves the SAME quote at the SAME url may lend its clock.
    const evidenceClock = TRACKER_EVIDENCE_CLOCKS.get(record.source_capture_id);
    const reverifiedMs = evidenceClock && evidenceClock.quoteSha === record.quote_sha256
      && evidenceClock.url === record.source_url ? Date.parse(evidenceClock.at) : NaN;
    const freshnessMs = Number.isFinite(reverifiedMs) && reverifiedMs >= checkedMs ? reverifiedMs : checkedMs;
    if (freshnessMs > nowMs) throw new Error("tracker_after_qualification_invalid");
    if (nowMs - freshnessMs > MAX_AGE_MS) throw new Error("tracker_freshness_invalid");
'''
TRACKER_CLOCKS_ANCHOR = '  const ids = new Set();\n  const scrapeIds = new Set();\n'
TRACKER_CLOCKS_BINDING = (
    '  const TRACKER_EVIDENCE_CLOCKS = trackerEvidenceClocks('
    'trackerFileURLToPath(new URL("../", import.meta.url)));\n'
)


def patch_price_tracker_freshness_clock(source):
    """Measure price freshness against the authenticated re-capture, never against the
    frozen checked_at alone. Identical shape to the taxonomy clock repair; the sealed
    quote, its sha256 and its displayed checked_at are untouched, so no published byte
    moves and MAX_AGE_MS is never widened."""
    source = once(source, TRACKER_IMPORT_ANCHOR,
                  TRACKER_IMPORT_ANCHOR + TRACKER_IMPORT_ADDITION, 'tracker_evidence_clock_import')
    source = once(source, TRACKER_CLOCKS_ANCHOR,
                  TRACKER_CLOCKS_ANCHOR + TRACKER_CLOCKS_BINDING, 'tracker_evidence_clock_binding')
    return once(source, TRACKER_CLOCK_ANCHOR, TRACKER_CLOCK_REPLACEMENT, 'tracker_evidence_clock')
