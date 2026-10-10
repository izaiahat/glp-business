"""Source-only Q6 corrections and strict candidate renderer migration.

No registry, acceptance, queue, tracking requests or release effects.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re


def captured_review_sources(research):
    """A URL/date is not evidence; authenticate stored raw captures first."""
    binding = research.get('first_party_source_captures') or {}
    if not binding:
        return {}
    bundle = Path(binding['path']).read_bytes()
    if hashlib.sha256(bundle).hexdigest() != binding.get('sha256'):
        raise RuntimeError('review_capture_bundle_hash_mismatch')
    captures = {}
    for capture in json.loads(bundle).get('captures', []):
        path = Path(capture['raw_capture_path'])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != capture.get('raw_capture_sha256'):
            raise RuntimeError('review_raw_capture_hash_mismatch')
        response = json.loads(raw)
        if response.get('data', response).get('markdown') != capture.get('markdown'):
            raise RuntimeError('review_capture_markdown_mismatch')
        if capture.get('status') != 200 or not capture.get('markdown'):
            continue
        captures[capture['url']] = {
            'markdown': capture['markdown'],
            'binding': {
                'bundle_path': binding['path'], 'bundle_sha256': binding['sha256'],
                'raw_capture_path': str(path), 'raw_capture_sha256': capture['raw_capture_sha256'],
                'captured_at_utc': research['captured_at_utc'],
            },
        }
    return captures


def project_review_inputs(spec, research):
    """Correct only named consumed inputs, without mutating historical packets."""
    spec, research = deepcopy(spec), deepcopy(research)
    pid = spec['program_id']
    if pid not in {'rightwell-review', 'willow-review', 'ivim-health-review'}:
        return spec, research
    captures = captured_review_sources(research)
    copy, row, fields = spec['page_copy'], spec['row_contract'], research['fields']
    if pid == 'rightwell-review':
        copy['bottom_line'] = copy['bottom_line'].replace(
            'RightWell is strongest for adults comparing', 'RightWell describes')
    elif pid == 'willow-review':
        for key in ('description', 'bottom_line'):
            if 'compounded' not in copy[key].lower():
                copy[key] = copy[key].replace('oral and injectable GLP-1', 'compounded oral and injectable GLP-1').replace(
                    'offers semaglutide and tirzepatide', 'offers compounded semaglutide and compounded tirzepatide')
        if 'compounded' not in row['best_for'].lower():
            row['best_for'] = row['best_for'].replace('oral and injectable semaglutide and tirzepatide', 'compounded oral and injectable semaglutide and tirzepatide')
        row['medications'] = row['medications'].replace('Semaglutide and tirzepatide', 'Compounded semaglutide and compounded tirzepatide')
        row['starting_price'] = row['starting_price'].replace('Semaglutide from', 'Compounded semaglutide from').replace('; tirzepatide from', '; compounded tirzepatide from')
        row['pros'] = [text.replace('Oral and injectable options', 'Compounded oral and injectable options') for text in row['pros']]
        monthly, pharmacy = fields['monthly_price'], fields['pharmacy_type']
        monthly['value'] = monthly['value'].replace('Injectable semaglutide', 'Compounded injectable semaglutide')
        if not pharmacy['value'].startswith('Willow states:'):
            pharmacy['value'] = 'Willow states: “' + pharmacy['quote'] + '”'
        copy['pricing'], copy['regulatory'] = monthly['value'], pharmacy['value']
    else:
        terms_url = 'https://www.ivimhealth.com/terms-and-conditions/'
        plan_url = 'https://www.ivimhealth.com/glp1idwt/'
        conflict_quote = 'Because GLP-1 medications are prescribed and shipped based on your selected program length, cancellation is not available once your program has started.'
        shipping_quote = 'GLP-1 ID Program medications are not auto-shipped. Each shipment requires clinical review, titration assessment, and individual provider approval prior to dispensing.'
        for url, quote in ((terms_url, fields['cancellation']['quote']), (plan_url, conflict_quote), (terms_url, shipping_quote)):
            if quote not in captures.get(url, {}).get('markdown', ''):
                raise RuntimeError('review_correction_official_quote_missing:' + url)
        cancellation = fields['cancellation']
        cancellation['value'] = (
            "Reported information: Ivim's official cancellation statements conflict. "
            'Its Terms & Conditions describes refunds for unshipped medication, less a $100 fee per undeveloped three-month portion or $50 per undeveloped two-month portion, with a medical-ineligibility fee waiver. '
            'Its GLP-1 ID program page states: “' + conflict_quote + '” '
            'Controlling plan-specific cancellation and refund eligibility remain UNKNOWN. Confirm the applicable terms with Ivim before enrolling; no refund is promised.'
        )
        cancellation['conflicting_sources'] = [{'source_url': plan_url, 'quote': conflict_quote, 'verified_as_of': cancellation['verified_as_of']}]
        fields['shipping_wait']['value'] = 'UNKNOWN delivery estimate. Ivim states: “' + shipping_quote + '”'
        copy['cancellation'], copy['shipping'] = cancellation['value'], fields['shipping_wait']['value']
        profile = research['provider_dynamic_profile']
        offer_url, offer_quote = 'https://www.ivimhealth.com/wegovypill/', '# The Wegovy® Pill is Here'
        if offer_quote not in captures.get(offer_url, {}).get('markdown', ''):
            raise RuntimeError('review_affirmative_brand_offer_missing')
        profile['taxonomy']['brand_name_tags'] = ['wegovy']
        profile['brand_offer_unknown'] = ['mounjaro', 'zepbound']
        for binding in profile['evidence_bindings']:
            if 'taxonomy.brand_name_tags' in binding['fields']:
                binding.update(source_url=offer_url, evidence_text=offer_quote,
                               evidence_sha256=hashlib.sha256(offer_quote.encode()).hexdigest())
        spec['provider_dynamic_profile'] = deepcopy(profile)
    return spec, research


OLD_SCORE = '''function scoreBlock(row, provider) {
  const overall = score(row);
  if (overall !== null) {
    return `<p class="score">${esc((overall / 10).toFixed(1))}/10</p>`
      + `<p class="score-basis">Evidence-completeness score across the five published criteria below. <a href="/how-we-rate/">How we rate</a>.</p>`;
  }
  return `<p class="score score-unscored">Not scored</p>`
    + `<p class="score-basis">${esc(provider)} has not published enough of the five criteria below for a comparable score. `
    + `We leave it unscored rather than fill the gaps with estimates. <a href="/how-we-rate/">How we rate</a>.</p>`;
}'''
NEW_SCORE = '''function scoreBlock(row, provider) {
  const overall = score(row);
  const count = assessedCriteria(row).length;
  if (overall !== null && count > 0) {
    return `<p class="score">${esc((overall / 10).toFixed(1))}/10</p>`
      + `<p class="score-basis">Evidence-completeness score across the ${count} assessed criteria below. <a href="/how-we-rate/">How we rate</a>.</p>`;
  }
  return `<p class="score score-unscored">Not scored</p>`
    + `<p class="score-basis">No comparable editorial score is assigned to ${esc(provider)}. `
    + `This is not a claim that provider information is unpublished. <a href="/how-we-rate/">How we rate</a>.</p>`;
}'''
OLD_CRITERIA = '''function criteriaList(row) {
  const scored = Object.entries(WEIGHTS).filter(([key]) => key !== "public_reviews" || hasComparableReviewEvidence(row));
  return scored.map(([key, weight]) => {
    const value = row.criteria_scores?.[key];
    const result = !Number.isFinite(value) ? "Not published"
      : value === 0 ? `Nothing published · 0 of ${weight}%`
        : `${esc((value / 10).toFixed(1))}/10`;
    return `<li><span>${esc(LABELS[key])} · ${weight}%</span><strong>${result}</strong></li>`;
  }).join("");
}'''
NEW_CRITERIA = '''function assessedCriteria(row) {
  return Object.entries(WEIGHTS).filter(([key]) => Number.isFinite(row.criteria_scores?.[key])
    && (key !== "public_reviews" || hasComparableReviewEvidence(row)));
}

function criteriaList(row) {
  return assessedCriteria(row).map(([key, weight]) => {
    const value = row.criteria_scores[key];
    const result = value === 0 ? `Nothing published · 0 of ${weight}%`
      : `${esc((value / 10).toFixed(1))}/10`;
    return `<li><span>${esc(LABELS[key])} · ${weight}%</span><strong>${result}</strong></li>`;
  }).join("");
}'''
OLD_HOLD = 'const holdCopy = endedNotice || integration.hold_copy;'
NEW_HOLD = '''const holdCopy = endedNotice || (linkDark
    ? "No active affiliate link is available on this page."
    : "This page contains one active affiliate link. Clinic Scout may earn a commission at no extra cost to the reader; the link does not change the page evidence or score.");'''
SECTION = '${criteria ? `<section class="criteria" id="criteria"><h2>Published scoring criteria</h2><p>Scores use the published weights, normalize across assessed criteria only, and exclude commission. Unassessed criteria are omitted, not treated as unpublished or zero.</p><ul>${criteria}</ul></section>` : ""}'


def normalize_provider_review_editorial_source(target):
    """Fail-closed/idempotent migration on isolated candidate source, not HTML."""
    path = Path(target) / 'src/provider-review.mjs'
    source = path.read_text()
    for old, new in ((OLD_SCORE, NEW_SCORE), (OLD_CRITERIA, NEW_CRITERIA), (OLD_HOLD, NEW_HOLD)):
        if new in source:
            continue
        if source.count(old) != 1:
            raise RuntimeError('review_renderer_editorial_anchor_drift')
        source = source.replace(old, new)
    pattern = r'<section class="criteria" id="criteria"><h2>(?:Evidence criteria|Published scoring inputs)</h2><p>[^<]+</p><ul>\$\{criteria\}</ul></section>'
    matches = re.findall(pattern, source)
    if matches:
        if len(matches) != 2:
            raise RuntimeError('review_renderer_criteria_section_drift')
        source = re.sub(pattern, lambda _: SECTION, source)
        source = source.replace('<a href="#criteria">Scoring</a>', '${criteria ? `<a href="#criteria">Scoring</a>` : ""}')
    elif source.count(SECTION) != 2:
        raise RuntimeError('review_renderer_criteria_section_missing')
    old_observed = 'const observed = evidence?.observed_at?.slice(0, 10) || checkedAsOf;'
    new_observed = 'const observed = evidence?.observed_at?.slice(0, 10) || contract.source_capture_bindings?.[url]?.captured_at_utc?.slice(0, 10) || checkedAsOf;'
    if old_observed in source:
        source = source.replace(old_observed, new_observed)
    path.write_text(source)
    return {'status': 'PASS_SOURCE_ONLY_MIGRATION', 'renderer_sha256': hashlib.sha256(source.encode()).hexdigest()}
