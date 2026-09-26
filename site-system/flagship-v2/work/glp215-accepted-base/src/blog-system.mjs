import { affiliateCta, renderComparisonTable } from "./money-page.mjs";
import { normalizeNetwork } from "./affiliate-subid.mjs";
import { loadAffiliateLinkMap } from "./affiliate-link-map.mjs";
import { readFileSync } from "node:fs";
import { addP1ContentUpgradeSurface } from "./wave2-p1-surfaces.mjs";
import { CITATION_REASONS, renderCitationAnchor } from "./citation-attribution.mjs";

function esc(value) { return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;"); }
const PRIMARY_HOSTS = ["fda.gov", "hhs.gov", "nih.gov", "ncbi.nlm.nih.gov", "pubmed.ncbi.nlm.nih.gov", "cms.gov", "clinicaltrials.gov", "ftc.gov", "uscourts.gov"];
const PUBLIC_INTERNAL_COPY = /held until operator go-live|no live route|link-dark|noindex in staging|release-held|Addendum A|Batch 6 ledger|substantiation worksheet cell|supporting material matrix cell|held gates|staging-only|release receipt/i;
export const editorialContract = Object.freeze({ version: "seo-phase1-v1", titleChars: [50, 60], metaDescriptionChars: [150, 160], directAnswerWords: [40, 60] });
const words = (value) => String(value || "").trim().split(/\s+/).filter(Boolean).length;

function publicHeading(value) {
  return String(value || "")
    .replace(/\s+and held status\b/gi, "")
    .replace(/\s+and release status\b/gi, "")
    .replace(/\brelease status\b/gi, "update notes");
}

const markdownLinks = (value) => [...String(value ?? "").matchAll(/\[([^\]]{1,80})\]\((\/[^)\s]+|https?:\/\/[^)\s]+)\)/g)].map((match) => match[2]);
// 2026-08-27 (launch route proof finding): an in-prose affiliate link used to
// ship rel="sponsored" and NOTHING else — a real RevOffers money link on
// /guides/zepbound-coupon/ was invisible to analytics because the click
// runtime keys on data-analytics-event. Prose affiliate links now carry the
// same CR-5 identity as CTA anchors (program, network, element code P for
// prose, page identity), built from the brief's own affiliate_links rows.
const mdLinks = (value, affiliateUrls = new Set(), affiliateMeta = new Map(), pageSlug = "", citationUrls = new Set()) => String(value ?? "").replace(/\[([^\]]{1,80})\]\((\/[^)\s]+|https?:\/\/[^)\s]+)\)/g, (m, label, href) => {
  const safeHref = href.replaceAll('"', "&quot;");
  const rel = /^https?:\/\//.test(href)
    ? (affiliateUrls.has(href) ? ' rel="sponsored noopener"' : ' rel="noopener"')
    : (/^\/go\//.test(href) ? ' rel="sponsored nofollow noopener"' : "");
  const meta = affiliateMeta.get(href);
  if (meta?.program_id) {
    // Surface authority binds prose exactly like CTAs (§4.5): a program the
    // guides link map holds (shedrx, serena-health pending the Awin
    // guides-surface re-mint) renders as its internal review link, never as a
    // live affiliate anchor. Before 2026-08-27 the raw href shipped with bare
    // rel="sponsored" and silently crossed that hold.
    const guideLink = loadAffiliateLinkMap("guides")[meta.program_id];
    if (!guideLink || guideLink.live !== true || !guideLink.affiliate_href) {
      const reviewPath = String(guideLink?.review_path || `/reviews/${meta.program_id}/`).replaceAll('"', "&quot;");
      return `<a href="${reviewPath}">${label}</a>`;
    }
    const markers = ` data-analytics-event="affiliate_click" data-link-mode="transparent-direct" data-program="${meta.program_id.replaceAll('"', "&quot;")}" data-provider-slug="${meta.program_id.replaceAll('"', "&quot;")}" data-network="${normalizeNetwork(null, href).replaceAll('"', "&quot;")}" data-element-code="P" data-rank-position="0" data-page-type="guide" data-sub1="0P" data-sub2="${String(pageSlug).replaceAll('"', "&quot;")}"`;
    return `<a href="${safeHref}"${rel}${markers}>${label}</a>`;
  }
  const citation = citationUrls.has(href) ? ` data-citation="${CITATION_REASONS.GUIDE_SOURCE_LEDGER}"` : "";
  return `<a href="${safeHref}"${rel}${citation}>${label}</a>`;
});
function publicParagraphs(paragraphs) {
  return (paragraphs || []).filter((text) => !PUBLIC_INTERNAL_COPY.test(String(text)));
}

function slugify(value) {
  return String(value || "section").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "section";
}

function humanTopic(brief) {
  const keyword = String(brief.primary_keyword || brief.target_query || "Research guide").toLowerCase();
  if (/cost|price|savings|assistance/.test(keyword)) return "Cost and access";
  if (/counterfeit|safety|verify|pharmacy|warning/.test(keyword)) return "Safety and verification";
  if (/coverage|insurance|appeal|medicare/.test(keyword)) return "Coverage and appeals";
  if (/\bvs\b|compare|comparison/.test(keyword)) return "Comparison guide";
  return "Source-checked research";
}

function guideSourceIndex(brief) {
  return new Map((brief.sources || []).map((source, index) => [source.source_id, index + 1]));
}

// Design pass 2026-08-26 (insights spec §4.1): the full claim-to-source map is
// evidence APPENDIX, not lede furniture. The section heading and its anchor
// stay visible (TOC and "Mapped claim" links keep a landing target); the long
// numbered list itself ships collapsed. Every claim statement is still in the
// DOM — nothing is deleted, it just stops costing ~2,400px before the article.
function renderClaimEvidence(brief) {
  const sourceIndex = guideSourceIndex(brief);
  const claims = brief.claims || [];
  const items = claims.map((claim) => {
    const refs = (claim.source_ids || []).map((id) => sourceIndex.get(id)).filter(Boolean).map((index) => `<a href="#guide-source-${index}" aria-label="Source ${index}">${index}</a>`).join("");
    return `<li id="${esc(claim.claim_id)}"><span>${esc(claim.statement)}</span>${refs ? `<sup class="inline-citation">${refs}</sup>` : ""}</li>`;
  }).join("");
  return items ? `<section class="claim-evidence"><h2 id="verified-claims">Verified claims</h2><p>Each statement below is bound to its numbered source.</p><details class="cs-details claim-evidence-fold"><summary>Open the claim-to-source map (${claims.length} claim${claims.length === 1 ? "" : "s"})</summary><ol>${items}</ol></details></section>` : "";
}

// Owned, source-mapped data table ("information gain"). Restored 2026-08-06: this
// block used to be injected into the built HTML by a one-off phase23 script, which
// is why /guides/tirzepatide-cost/ shipped with a table its content-batch entry
// could not describe. It renders from the article row so the batch stays authority.
function renderInformationGain(brief) {
  const gain = brief.information_gain;
  const rows = gain?.rows;
  if (!gain || !Array.isArray(rows) || !rows.length) return "";
  const sourceIndex = guideSourceIndex(brief);
  const body = rows.map((row) => {
    const number = sourceIndex.get(row.source_id);
    if (!number) throw new Error("information_gain_source_unmapped");
    return `<tr><th scope="row">${esc(row.label)}</th><td data-label="Published amount"><strong>${esc(row.value)}</strong></td><td data-label="Evidence"><a href="#guide-source-${number}">Source ${number}</a></td></tr>`;
  }).join("");
  const headers = gain.headers || ["Product and payment path", "Published amount", "Evidence"];
  return `<section class="guide-data-table information-gain-table" data-information-gain="${esc(gain.type)}" aria-labelledby="${esc(gain.anchor_id)}"><h2 id="${esc(gain.anchor_id)}">${esc(gain.heading)}</h2><p>${esc(gain.note)}</p><div class="table-scroll responsive-table"><table><caption>${esc(gain.caption)}</caption><thead><tr>${headers.map((cell) => `<th scope="col">${esc(cell)}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div></section>`;
}

// Design pass 2026-08-26: appendix material (spec §4.1) — the fact recap ships
// collapsed next to the claim map. Content unchanged, ~1,200px reclaimed.
function renderGuideFacts(brief) {
  const rows = (brief.claims || []).filter((claim) => /\$|price|cost|coverage|eligib|label|approved|warning|percent|%/i.test(claim.statement || "")).slice(0, 8);
  if (!rows.length) return "";
  return `<section class="guide-data-table"><h2 id="facts-to-compare">Facts to compare</h2><details class="cs-details guide-facts-fold"><summary>Open the source-bound fact recap (${rows.length} row${rows.length === 1 ? "" : "s"})</summary><div class="table-scroll responsive-table"><table><caption>Source-bound facts from this guide</caption><thead><tr><th scope="col">Question</th><th scope="col">Published fact</th><th scope="col">Evidence</th></tr></thead><tbody>${rows.map((claim) => `<tr><th scope="row">${/\$|price|cost/i.test(claim.statement) ? "Cost" : /label|approved/i.test(claim.statement) ? "Product status" : /warning|risk/i.test(claim.statement) ? "Safety boundary" : "Eligibility or terms"}</th><td data-label="Published fact">${esc(claim.statement)}</td><td data-label="Evidence"><a href="#${esc(claim.claim_id)}">Mapped claim</a></td></tr>`).join("")}</tbody></table></div></details></section>`;
}

function renderGuideChecklist(brief) {
  const positives = (brief.pros || []).slice(0, 5);
  const cautions = (brief.cons || []).slice(0, 5);
  if (!positives.length && !cautions.length) return "";
  return `<section class="guide-checklist"><h2 id="verification-checklist">What to verify</h2><div class="guide-checklist-grid"><div><h3>Confirm</h3><ul>${positives.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div><div><h3>Do not assume</h3><ul>${cautions.map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div></div></section>`;
}

function renderGuideSources(brief) {
  const claims = new Map((brief.claims || []).map((claim) => [claim.claim_id, claim.statement]));
  return (brief.sources || []).map((source, index) => {
    const supported = (source.claim_ids || []).map((id) => claims.get(id)).filter(Boolean);
    const unavailable = /(?:_404|NOT_OBTAINABLE|EXACT_QUERY_SERP)/.test(String(source.source_class || ""));
    const sourceControl = unavailable
      ? `<span class="source-nolink" data-citation="${CITATION_REASONS.GUIDE_SOURCE_LEDGER}" data-source-access="recorded_unavailable">Recorded unavailable at check time</span>`
      : renderCitationAnchor({ url: source.url, label: "Open source", reason: CITATION_REASONS.GUIDE_SOURCE_LEDGER });
    return `<li id="guide-source-${index + 1}"><strong>${esc(source.publisher)}</strong><span>${supported.length ? `Supports: ${supported.map(esc).join(" · ")}` : "Background source for the guide boundary."}</span>${sourceControl}<small>Checked ${esc(source.verified_as_of)}</small></li>`;
  }).join("");
}

export function validateKeywordOwnership(entries) {
  const seenKeyword = new Set(); const seenUrl = new Set();
  for (const item of entries) {
    const keyword = String(item.primary_keyword || "").trim().toLowerCase();
    if (!keyword || seenKeyword.has(keyword) || !item.url || seenUrl.has(item.url)) throw new Error("keyword_ownership_collision");
    seenKeyword.add(keyword); seenUrl.add(item.url);
  }
  return true;
}

// ED §4.1 — the task-#61 gate flip.
//
// The v1 shape (`live_link_allowed === false && operator_gate_receipt === null`) was
// correct governance while the ruling was pending: it held every machine-minted
// comparison guide link-dark and threw if anything tried to claim otherwise. The
// ruling now EXISTS, as an operator gate receipt on disk, so v2 is accepted -
// and v1 stays valid, unchanged, so the SEO machine can still mint held guides that
// have not been ruled on. Nothing was weakened: v2 demands MORE than v1 did.
//
// v2 creates no second link authority. Rows resolve through exactly the same
// `buildAffiliateLinkMap` chain the money pages use, on surface "guides". A program
// the map does not return live - surface authority missing, href hash mismatch,
// poisoned row, operator-rejected terms - keeps the held shape and renders the
// internal fallback CTA, exactly as it does on /best/ and /cost/.
export const EDITORIAL_MONETIZATION_GATE_RECEIPT_SHA256 = "386dc029a3cd681c352bee33037ee150ed7da795191cd8df1a0010aeeb4e74b6";

function blogComparisonSpecVersion(spec) {
  if (spec?.schema === "blog-comparison-spec-v1" && spec.live_link_allowed === false && spec.operator_gate_receipt === null) return "v1";
  if (spec?.schema === "blog-comparison-spec-v2" && spec.live_link_allowed === true
    && spec.operator_gate_receipt === EDITORIAL_MONETIZATION_GATE_RECEIPT_SHA256
    && typeof spec.page_slug === "string" && spec.page_slug.length > 0
    && /^\d{4}-\d{2}-\d{2}$/.test(String(spec.source_checked_through || ""))) return "v2";
  return null;
}

function heldComparisonRow(program, reviewPath) {
  const { affiliate_href: _affiliateHref, ...heldProgram } = program;
  return { ...heldProgram, provider: program.display_name, merchant: program.display_name, review_path: reviewPath, live_link_allowed: false, promotion_authorized: false, operator_selection_receipt_sha256: null, affiliate_link_mode: "held_blog_comparison" };
}

export function resolveBlogComparisonSpec(brief, programs, linkMap) {
  const spec = brief?.comparison_spec;
  if (!brief?.comparison_required) return null;
  const version = blogComparisonSpecVersion(spec);
  if (!version) throw new Error("blog_comparison_spec_gate_invalid");
  const ids = spec.row_program_ids || [];
  if (ids.length < 4 || ids.length > 8 || new Set(ids).size !== ids.length) throw new Error("blog_comparison_row_ids_invalid");
  const programMap = new Map((programs || []).map((program) => [program.program_id, program]));
  const links = version === "v2" ? (linkMap || loadAffiliateLinkMap("guides")) : {};
  const rows = ids.map((programId) => {
    const program = programMap.get(programId);
    if (!program) throw new Error(`blog_comparison_program_invalid:${programId}`);
    const link = links[programId];
    const reviewPath = link?.review_path || program.review_path || `/reviews/${programId}/`;
    if (version !== "v2" || !link?.live || !link.affiliate_href) return heldComparisonRow(program, reviewPath);
    return {
      ...program,
      provider: program.display_name,
      merchant: program.display_name,
      review_path: reviewPath,
      affiliate_href: link.affiliate_href,
      live_link_allowed: true,
      promotion_authorized: true,
      affiliate_link_mode: "transparent_direct",
    };
  });
  // `path` drives pageTypeOf() so a guide click is attributed as a guide, and
  // `date_modified` feeds the subid month contract in affiliateCta.
  return { ...spec, path: brief.canonical_path, date_modified: spec.source_checked_through || brief.source_checked_through, rows };
}

// ED §4.3 — the offer module for the two guides that have no comparison table.
// It is deliberately NOT a general-purpose renderer: every row must carry captured
// first-party wording and the date it was captured, because the §3.6 boundary
// ("these pages publish wording you can verify", never "these providers are
// HSA/FSA-eligible") is only true if the wording is actually on the page. A row
// without it throws rather than rendering a bare CTA next to an evidence claim.
// The offer module needs the ledger row (display name, network, review path) for a
// program the brief names. Loaded the same way loadAffiliateLinkMap loads its own
// authorities, so no caller has to thread the ledger through every render path.
let ledgerProgramsCache = null;
function ledgerPrograms() {
  if (ledgerProgramsCache) return ledgerProgramsCache;
  try {
    ledgerProgramsCache = JSON.parse(readFileSync(new URL("../data/program-content-ledger.json", import.meta.url), "utf8")).programs || [];
  } catch { ledgerProgramsCache = []; }
  return ledgerProgramsCache;
}

export const GUIDE_TABLE_DISCLOSURE = "Clinic Scout may earn commissions from provider links. Check the ordering method stated for each comparison. Payment is not evidence of quality, safety or medical suitability.";

export function renderGuideOfferModule(module, programs, spec, linkMap) {
  if (!module || !Array.isArray(module.rows) || module.rows.length === 0) return "";
  if (!module.heading) throw new Error("guide_offer_module_heading_missing");
  if (module.inside_sources_block) throw new Error("guide_offer_module_inside_evidence_layer");
  const version = blogComparisonSpecVersion(spec);
  if (version !== "v2") throw new Error("guide_offer_module_gate_invalid");
  const programMap = new Map((programs || []).map((program) => [program.program_id, program]));
  const links = linkMap || loadAffiliateLinkMap("guides");
  const rows = module.rows.map((entry) => {
    const program = programMap.get(entry.program_id);
    if (!program) throw new Error(`guide_offer_module_program_invalid:${entry.program_id}`);
    if (!entry.captured_wording || !/^\d{4}-\d{2}-\d{2}$/.test(String(entry.checked_date || ""))) throw new Error(`guide_offer_module_row_evidence_missing:${entry.program_id}`);
    const link = links[entry.program_id];
    const reviewPath = link?.review_path || program.review_path || `/reviews/${entry.program_id}/`;
    const row = link?.live && link.affiliate_href
      ? { ...program, provider: program.display_name, merchant: program.display_name, review_path: reviewPath, affiliate_href: link.affiliate_href, live_link_allowed: true, promotion_authorized: true, affiliate_link_mode: "transparent_direct" }
      : heldComparisonRow(program, reviewPath);
    const cta = affiliateCta({ ...spec, rows: module.rows.map((item) => ({ program_id: item.program_id })) }, row, "pricing", "terms");
    return `<li class="guide-offer-row"><p class="guide-offer-provider"><strong>${esc(program.display_name)}</strong></p><p class="guide-offer-wording">${esc(entry.captured_wording)} <span class="guide-offer-checked">Captured <time datetime="${esc(entry.checked_date)}">${esc(entry.checked_date)}</time></span></p><div class="dual-cta">${cta}<a class="cta internal" href="${esc(reviewPath)}">Read ${esc(program.display_name)} review</a></div></li>`;
  }).join("");
  return `<section class="guide-offer-module" id="${esc(module.id || "current-offers")}" aria-labelledby="guide-offer-heading"><h2 id="guide-offer-heading">${esc(module.heading)}</h2>${module.standfirst ? `<p>${esc(module.standfirst)}</p>` : ""}<p class="table-disclosure">${esc(GUIDE_TABLE_DISCLOSURE)}</p><ul class="guide-offer-rows">${rows}</ul></section>`;
}

export function validateHeldDraft(draft, ownershipEntries) {
  if (!draft?.draft_id || !/^\/[a-z0-9/_-]+\/$/.test(draft.canonical_path || "")) throw new Error("held_draft_identity_invalid");
  if (!["LOW", "MEDIUM", "HIGH"].includes(draft.risk_tier)) throw new Error("held_draft_risk_invalid");
  if (!/^HELD_|^BLOCKED_/.test(draft.status || "") || String(draft.hold_reason || "").length < 30) throw new Error("held_draft_gate_invalid");
  const owner = (ownershipEntries || []).find((entry) => entry.primary_keyword === draft.primary_keyword && entry.url === draft.canonical_path);
  if (!owner || owner.risk_tier !== draft.risk_tier || owner.status !== draft.status) throw new Error("held_draft_keyword_owner_mismatch");
  return true;
}

export function renderHeldDraft(draft, author) {
  return `<!doctype html><html lang="en" data-release="held"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow,noarchive,nosnippet"><title>${esc(draft.h1)} · Clinic Scout</title><style>:root{--ink:#10233b;--paper:#f7f5ef;--card:#fff;--line:#ccd7df;--green:#0b644d}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.65 system-ui,sans-serif}main{width:min(760px,calc(100% - 32px));margin:auto;padding:56px 0 96px}.eyebrow{color:var(--green);font-weight:900;text-transform:uppercase;letter-spacing:.1em}h1{font-size:clamp(2.4rem,7vw,4.8rem);line-height:1}.hold{background:var(--card);border:1px solid var(--line);border-left:7px solid #b47a00;border-radius:18px;padding:22px;margin:28px 0}a{min-height:44px;display:inline-flex;align-items:center}</style></head><body><main><p class="eyebrow">${esc(draft.risk_tier)}-risk held evidence draft</p><h1>${esc(draft.h1)}</h1><p>By <a href="${esc(author.bio_path)}">${esc(author.display_name)}</a> · Independent research · every claim source-cited</p><section class="hold"><h2>Not ready for publication</h2><p>${esc(draft.hold_reason)}</p><p><strong>Gate:</strong> ${esc(draft.status)}. No claim, provider verdict, affiliate link, or coverage conclusion is published from this placeholder.</p></section><p><a href="/editorial-guidelines/">Read the evidence and risk-tier policy</a></p></main></body></html>`;
}

export function isValidInternalLinkPath(value) {
  return /^\/[a-z0-9/_-]+\/(?:\?[a-zA-Z0-9._~%=&+-]+)?$/.test(String(value || ""));
}

export function reviewBrief(brief, author) {
  const risk = brief.risk_tier;
  if (!["LOW", "MEDIUM", "HIGH"].includes(risk)) return { status: "BLOCKED", reason: "risk_tier_invalid" };
  if (risk === "HIGH") return { status: "BLOCKED", reason: "high_risk_medical_claim_must_be_reframed" };
  const taxonomy = brief.surface_taxonomy || "medical";
  if (!["commercial", "medical"].includes(taxonomy)) return { status: "BLOCKED", reason: "surface_taxonomy_invalid" };
  if (!["verified_organization", "verified_human"].includes(author?.status) || author?.clinical_credentials_claimed !== false) return { status: "HELD", reason: "honest_persistent_author_missing" };
  const sources = brief.sources || [];
  const minSources = risk === "MEDIUM" ? 3 : 2;
  if (sources.length < minSources || sources.some((item) => !/^https:\/\//.test(item.url || "") || !item.source_id || !Array.isArray(item.claim_ids) || !item.verified_as_of)) return { status: "HELD", reason: "claim_source_map_incomplete" };
  if (new Set(sources.map((item) => item.source_id)).size !== sources.length) return { status: "HELD", reason: "source_id_duplicate" };
  if (risk === "MEDIUM" && !sources.some((item) => PRIMARY_HOSTS.some((host) => new URL(item.url).hostname.endsWith(host)))) return { status: "HELD", reason: "primary_medical_source_missing" };
  if (brief.evidence_review?.status !== "PASS" || !brief.evidence_review?.reviewed_at || !brief.evidence_review?.content_sha256) return { status: "HELD", reason: "evidence_review_receipt_missing" };
  if (!Number.isInteger(brief.word_count) || brief.word_count < 1500 || brief.word_count > 3600) return { status: "HELD", reason: "word_count_outside_blueprint" };
  if (brief.internal_links && (!Array.isArray(brief.internal_links) || brief.internal_links.some((item) => !item?.label || !isValidInternalLinkPath(item.path)))) return { status: "BLOCKED", reason: "internal_link_invalid" };
  const affiliateUrls = new Set((brief.affiliate_links || []).map((item) => item?.url).filter(Boolean));
  const sourceUrls = new Set(sources.map((item) => item.url));
  const renderedInputUrls = new Set((brief.sections || []).flatMap((section) => publicParagraphs(section.paragraphs).flatMap(markdownLinks)).filter((href) => /^https?:\/\//.test(href)));
  const unboundExternalUrls = [...renderedInputUrls].filter((url) => !sourceUrls.has(url) && !affiliateUrls.has(url));
  if (taxonomy === "medical" && (affiliateUrls.size || unboundExternalUrls.length)) return { status: "BLOCKED", reason: "medical_surface_must_be_tracked_link_clean" };
  if (taxonomy === "commercial" && brief.has_affiliate_cta && !brief.inline_ftc_disclosure) return { status: "BLOCKED", reason: "inline_ftc_disclosure_missing" };
  // ED §4.2: a commercial guide can carry its commerce in the comparison table or an
  // offer module instead of prose links. Counting only prose `affiliate_links` HELDs
  // exactly the table-only pages this ruling flips. The disclosure requirement is
  // unchanged and the medical-surface link-clean rule above is untouched.
  const liveComparison = blogComparisonSpecVersion(brief.comparison_spec) === "v2";
  const hasOfferModule = Array.isArray(brief.offer_module?.rows) && brief.offer_module.rows.length > 0;
  const commerceSurfacePresent = Boolean(affiliateUrls.size || liveComparison || hasOfferModule);
  if (taxonomy === "commercial" && brief.has_affiliate_cta && (!brief.ftc_disclosure_placement || !commerceSurfacePresent)) return { status: "HELD", reason: "commercial_taxonomy_affiliate_contract_incomplete" };
  // ED §4.4(5): once a page is actually monetized its disclosure must describe the
  // present, not a held future. "if a held link is later approved" is now a lie.
  if (taxonomy === "commercial" && brief.has_affiliate_cta && (liveComparison || hasOfferModule)
    && /held link|future eligible|no outbound affiliate link is active|later approved/i.test(String(brief.inline_ftc_disclosure_text || ""))) return { status: "HELD", reason: "ftc_disclosure_describes_held_future_on_live_page" };
  // ED §6.1: an affiliate href must never be reachable from the evidence layer.
  if ((brief.sources || []).some((source) => affiliateUrls.has(source.url))) return { status: "BLOCKED", reason: "citation_position_carries_affiliate_href" };
  if (brief.editorial_contract_version === editorialContract.version) {
    const titleLength = String(brief.title || "").length;
    const metaLength = String(brief.meta_description || brief.description || "").length;
    const answerWords = words(brief.direct_answer);
    if (titleLength < editorialContract.titleChars[0] || titleLength > editorialContract.titleChars[1]) return { status: "HELD", reason: "editorial_title_length" };
    if (metaLength < editorialContract.metaDescriptionChars[0] || metaLength > editorialContract.metaDescriptionChars[1]) return { status: "HELD", reason: "editorial_meta_length" };
    if (answerWords < editorialContract.directAnswerWords[0] || answerWords > editorialContract.directAnswerWords[1]) return { status: "HELD", reason: "editorial_direct_answer_length" };
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(brief.source_checked_through || ""))) return { status: "HELD", reason: "editorial_source_date_missing" };
    if (brief.has_affiliate_cta) {
      const disclosureText = String(brief.inline_ftc_disclosure_text || "").trim();
      if (brief.ftc_disclosure_placement !== "before_first_affiliate_link" || !/affiliate/i.test(disclosureText) || !/(?:commission|compensation|earn)/i.test(disclosureText) || !commerceSurfacePresent || [...affiliateUrls].some((url) => !/^https:\/\/[^\s]+$/.test(String(url)) || !renderedInputUrls.has(url))) return { status: "HELD", reason: "editorial_affiliate_contract_incomplete" };
    }
  }
  if (brief.comparison_required) {
    const spec = brief.comparison_spec;
    const ids = spec?.row_program_ids || [];
    // Both shapes are valid: v1 (held, un-ruled) and v2 (ruled, receipt-bound).
    // Anything else - including a v2 carrying the wrong receipt - still HELDs.
    if (!blogComparisonSpecVersion(spec) || ids.length < 4 || ids.length > 8 || new Set(ids).size !== ids.length) return { status: "HELD", reason: "comparison_spec_incomplete" };
  }
  return { status: "PASS", reason: "risk_tier_and_evidence_clear" };
}

function renderBlogPostLegacy(brief, comparisonSpec, author) {
  const gate = reviewBrief(brief, author);
  if (brief.comparison_required && !comparisonSpec) throw new Error("comparison_spec_required");
  const indexable = gate.status === "PASS" && brief.batch_gate_passed === true && brief.operator_release_receipt;
  const affiliateUrls = new Set((brief.affiliate_links || []).map((item) => esc(item?.url)));
  const affiliateMeta = new Map((brief.affiliate_links || []).filter((item) => item?.url && item?.program_id).map((item) => [esc(item.url), item]));
  const sourceUrls = new Set((brief.sources || []).map((item) => esc(item?.url)));
  const briefPageSlug = String(brief.page_slug || brief.slug || String(brief.canonical_path || "").replaceAll("/", " ").trim().split(" ").pop() || "");
  const sources = (brief.sources || []).map((item, index) => {
    const relationship = item.claim_ids.length ? `supports ${esc(item.claim_ids.join(", "))}` : "background source";
    return `<li id="source-${esc(item.source_id)}-${index + 1}">${renderCitationAnchor({ url: item.url, label: item.publisher, reason: CITATION_REASONS.GUIDE_SOURCE_LEDGER })} · ${relationship} · checked ${esc(item.verified_as_of)}</li>`;
  }).join("");
  const internalLinks = (brief.internal_links || []).map((item) => `<li><a href="${esc(item.path)}">${esc(item.label)}</a></li>`).join("");
  const body = (brief.sections || []).map((section) => {
    const rawHeading = publicHeading(section.heading || section.h2 || "");
    const heading = rawHeading ? `<h2>${esc(rawHeading.replace(/\bverification verification\b/gi, "verification"))}</h2>` : "";
    const paragraphs = publicParagraphs(section.paragraphs).map((text) => `<p>${mdLinks(esc(text), affiliateUrls, affiliateMeta, briefPageSlug, sourceUrls)}</p>`).join("");
    return heading || paragraphs ? `<section>${heading}${paragraphs}</section>` : "";
  }).join("") + (internalLinks ? `<nav class="evidence" aria-label="Related Clinic Scout guides"><h2>Keep comparing</h2><ul>${internalLinks}</ul></nav>` : "");
  return `<!doctype html><html lang="en" data-release="${indexable ? "release-ready" : "held"}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="${indexable ? "index,follow,max-image-preview:large" : "noindex,nofollow,noarchive,nosnippet,max-image-preview:large"}"><title>${esc(brief.title)}</title><style>:root{--ink:#10233b;--paper:#f7f5ef;--surface:#fff;--line:#ccd7df;--green:#0b644d;--coral:#d94f35}*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:17px/1.65 ui-sans-serif,system-ui,sans-serif}main{width:min(1100px,calc(100% - 32px));margin:auto;padding:32px 0 96px}.hero{display:grid;grid-template-columns:1fr 1fr;gap:clamp(24px,5vw,64px);align-items:center}.hero img{width:100%;height:auto;aspect-ratio:16/9;object-fit:cover;border-radius:28px}h1{font-size:clamp(2.6rem,7vw,5.4rem);line-height:.98;letter-spacing:-.045em;text-wrap:balance}.answer,.evidence,.disclosure{padding:20px;border:1px solid var(--line);border-radius:18px;background:var(--surface);margin:24px 0}.answer{border-left:7px solid var(--green)}.table-wrap{overflow-x:auto;width:100%}table{min-width:980px}.table-controls{display:flex;gap:8px;flex-wrap:wrap}.table-controls button{min-height:44px;padding:10px 14px;font:inherit}a,.cta{min-height:44px}.cta{display:inline-flex;align-items:center;padding-block:10px}.hero .kicker{color:var(--green);font-weight:900;text-transform:uppercase;letter-spacing:.1em}@media(max-width:768px){.hero{grid-template-columns:1fr}.hero picture{order:2}main{width:min(100% - 24px,680px)}}@media(prefers-reduced-motion:reduce){*{transition:none!important}}</style></head><body><main><section class="hero"><div><p class="kicker">${esc(brief.risk_tier)}-risk evidence brief</p><h1>${esc(brief.h1)}</h1><p>By <a href="${esc(author.bio_path)}">${esc(author.display_name)}</a> · Independent research · every claim source-cited · updated ${esc(brief.updated_at)}</p></div><picture><source srcset="/assets/images/editorial-evidence-hero-1600.avif" type="image/avif"><source srcset="/assets/images/editorial-evidence-hero-1600.webp" type="image/webp"><img src="/assets/images/editorial-evidence-hero-1600.jpg" width="1600" height="900" fetchpriority="high" alt="Layered research documents and evidence cards representing source-reviewed health comparisons"></picture></section><div class="evidence">Reviewed against FDA prescribing information and peer-reviewed studies where applicable · Sources checked ${esc(brief.source_checked_through)} · General information only, not medical advice.</div><section class="answer"><h2>Direct answer</h2><p>${esc(brief.direct_answer)}</p></section>${brief.inline_ftc_disclosure ? `<aside class="disclosure"><strong>Affiliate disclosure:</strong> We may earn a commission from some provider links. Check the ordering method stated for each comparison; payment is not evidence of quality, safety or medical suitability.</aside>` : ""}${comparisonSpec ? renderComparisonTable(comparisonSpec) : ""}${body}<section><h2>Sources</h2><ol>${sources}</ol></section><section><h2>Next step</h2><p>${esc(brief.conclusion)}</p><a href="/quiz/provider-fit/">Compare your priorities privately</a></section></main></body></html>`;
}

// Scoped, build-preserved styling for the guide template (assets/site.css is owned elsewhere).
// Placed inside <main> so it wins over site.css at equal specificity, and marked data-preserve
// so the build's <style> stripper keeps it.
const GUIDE_STYLE = `<style data-preserve>
.guide-shell section.answer{background:none;border:0;box-shadow:none;padding:0;margin:18px auto;overflow:visible}
.guide-shell section.answer::before{content:none}
.guide-shell .quick-answer{margin:0}
.guide-shell .quick-answer .gcs-kicker{margin:0 0 6px;color:var(--color-evidence)}
.guide-shell .quick-answer p:last-child{margin-bottom:0}
.guide-shell .guide-byline{color:var(--text-secondary)}
.guide-shell .guide-evidence-band{margin:14px auto 6px;padding-left:12px;border-left:3px solid var(--color-evidence);color:var(--text-secondary);font-size:.95rem}
.guide-shell .guide-next-card{display:flex;flex-wrap:wrap;align-items:center;gap:12px 18px;padding:16px 20px;margin:22px auto}
.guide-shell .guide-next-card:hover{transform:none}
.guide-shell .guide-next-card .gcs-kicker{margin:0;color:var(--color-evidence);flex:0 0 auto}
.guide-shell .guide-next-links{display:flex;flex-wrap:wrap;gap:10px;margin:0}
/* The build's legacy addGuideFunnel injects duplicate next-step panels (.guide-next-step) into
   GLP guides; this template now owns the compact next-step cards, so hide the legacy pair.
   No-op once addGuideFunnel is retired in scripts/build-staging.mjs. */
.guide-shell .guide-next-step{display:none}
.guide-shell .guide-next-card .gcs-button{min-height:44px;padding:9px 16px;font-size:.95rem}
.guide-shell .guide-faq{margin:30px auto}
.guide-shell .faq-item{border:1px solid var(--color-line);border-radius:12px;background:var(--surface-card);margin:10px 0}
.guide-shell .faq-item>summary{cursor:pointer;list-style:none;padding:14px 18px;font-weight:700;font-family:var(--font-display);display:flex;justify-content:space-between;align-items:center;gap:12px}
.guide-shell .faq-item>summary::-webkit-details-marker{display:none}
.guide-shell .faq-item>summary::after{content:"+";color:var(--color-evidence);font-weight:800;flex:0 0 auto}
.guide-shell .faq-item[open]>summary::after{content:"–"}
.guide-shell .faq-item>div{padding:0 18px 16px;color:var(--text-secondary)}
.guide-shell .faq-item>div p{margin:0 0 10px}
.guide-shell .faq-item>div p:last-child{margin-bottom:0}
@media(max-width:640px){.guide-shell .guide-next-links{width:100%}.guide-shell .guide-next-card .gcs-button{flex:1 1 auto}}

/* ED B-13 guide monetization surfaces. Visually distinct from the evidence layer by
   design: the ruling's core invariant is that a reader can always tell a commercial
   element from a citation. */
.table-disclosure{margin:0 0 12px;padding:10px 14px;border-left:4px solid #b47a00;background:#fffaf0;border-radius:0 10px 10px 0;font-size:.94rem;color:#5b4a20}
.guide-offer-module{margin:34px 0;padding:22px;border:1px solid #ccd7df;border-left:7px solid #0b644d;border-radius:18px;background:#fff}
.guide-offer-module h2{margin-top:0}
.guide-offer-rows{list-style:none;margin:0;padding:0;display:grid;gap:16px}
.guide-offer-row{padding:16px;border:1px solid #e2e9ef;border-radius:14px;background:#fbfcfd}
.guide-offer-provider{margin:0 0 6px}
.guide-offer-wording{margin:0 0 12px;color:#43566b;font-size:.96rem}
.guide-offer-checked{display:inline-block;margin-left:6px;padding:2px 8px;border-radius:999px;background:#eef3f8;color:#31465c;font-size:.82rem;white-space:nowrap}
.guide-offer-row .dual-cta{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
@media(max-width:640px){.guide-offer-row .dual-cta{grid-template-columns:1fr}}
</style>`;

// Next-step funnel: one compact 3-link card, reused early and at the close of every guide.
const GUIDE_NEXT_STEPS = [
  { href: "/tools/glp-1-cost-calculator/", label: "Estimate your cost", cls: "gcs-button" },
  { href: "/providers/?sort=cheapest", label: "Compare programs", cls: "gcs-button secondary" },
  { href: "/quiz/provider-fit/", label: "Find my match", cls: "gcs-button secondary" },
];

function renderNextStepCard(source) {
  const links = GUIDE_NEXT_STEPS
    .map((step) => `<a class="${step.cls}" href="${step.href}${step.href.includes("?") ? "&" : "?"}source=${esc(source)}">${step.label}</a>`)
    .join("");
  return `<aside class="panel evidence-line guide-next-card" aria-label="Next steps"><p class="gcs-kicker">Use this research</p><div class="guide-next-links">${links}</div></aside>`;
}

function normalizeGuideFaqs(brief) {
  const raw = Array.isArray(brief.faqs) ? brief.faqs : Array.isArray(brief.faq) ? brief.faq : [];
  return raw
    .map((item) => ({ q: String(item.q ?? item.question ?? "").trim(), a: String(item.a ?? item.answer ?? "").trim() }))
    .filter((item) => item.q && item.a);
}

function renderGuideFaq(faqs) {
  if (!faqs.length) return "";
  // Repair D (monotony): the first Q&A keeps the plain faq-item skin as the
  // section's visual anchor; every repeat beyond it additionally carries the
  // shared cs-details accordion primitive so a long FAQ list doesn't read as
  // N identical cards in a row. faq-item is kept on every item (it owns the
  // +/- marker, border, and inter-item spacing) — cs-details is additive, not
  // a replacement, so no existing FAQ styling/spacing regresses.
  const items = faqs
    .map((item, index) => `<details class="faq-item${index > 0 ? " cs-details" : ""}"><summary>${esc(item.q)}</summary><div><p>${esc(item.a)}</p></div></details>`)
    .join("");
  return `<section class="guide-faq" aria-labelledby="faq"><h2 id="faq">Frequently asked questions</h2>${items}</section>`;
}

// ED §3.5/§3.6 placement: the ruling names the section the module follows
// ("after the final content section and before Keep comparing", "after The
// prepayment checklist"). Encoding it as a section id keeps the decision in the
// brief where an editor can read it, and fails loudly if that section is renamed
// away rather than silently dropping the module at the end.
function placeSections(sectionRows, offerModule, afterSectionId) {
  const body = sectionRows.map((section) => section.html);
  if (!offerModule) return body.join("");
  if (!afterSectionId) return `${body.join("")}${offerModule}`;
  const index = sectionRows.findIndex((section) => section.id === afterSectionId);
  if (index < 0) throw new Error(`guide_offer_module_anchor_section_missing:${afterSectionId}`);
  return [...body.slice(0, index + 1), offerModule, ...body.slice(index + 1)].join("");
}

export function renderBlogPost(brief, comparisonSpec, author, programs) {
  if (!Array.isArray(brief.sections) || brief.sections.length === 0) {
    const rendered = renderBlogPostLegacy(brief, comparisonSpec, author);
    const canonicalPath = String(brief.canonical_path || "/guides/preview/");
    return addP1ContentUpgradeSurface(rendered.replace("</head>", `<link rel="canonical" href="https://glpclinicscout.com${esc(canonicalPath)}"></head>`), canonicalPath.replace(/^\//, ""));
  }
  const gate = reviewBrief(brief, author);
  if (brief.comparison_required && !comparisonSpec) throw new Error("comparison_spec_required");
  const indexable = gate.status === "PASS" && brief.batch_gate_passed === true && brief.operator_release_receipt;
  const canonical = `https://glpclinicscout.com${brief.canonical_path}`;
  const affiliateUrls = new Set((brief.affiliate_links || []).map((item) => esc(item?.url)));
  const affiliateMeta = new Map((brief.affiliate_links || []).filter((item) => item?.url && item?.program_id).map((item) => [esc(item.url), item]));
  const sourceUrls = new Set((brief.sources || []).map((item) => esc(item?.url)));
  const briefPageSlug = String(brief.page_slug || brief.slug || String(brief.canonical_path || "").replaceAll("/", " ").trim().split(" ").pop() || "");
  const description = String(brief.meta_description || brief.description || brief.direct_answer || brief.conclusion || brief.h1 || "Source-checked Clinic Scout guide").replace(/\s+/g, " ").trim();
  const sourceCheckedDate = brief.source_checked_through || brief.updated_at;
  const faqs = normalizeGuideFaqs(brief);
  const sectionRows = brief.sections.map((section, index) => {
    const heading = publicHeading(section.heading || section.h2 || "").replace(/\bverification verification\b/gi, "verification");
    const id = `section-${index + 1}-${slugify(heading)}`;
    const paragraphs = publicParagraphs(section.paragraphs).map((text) => `<p>${mdLinks(esc(text), affiliateUrls, affiliateMeta, briefPageSlug, sourceUrls)}</p>`).join("");
    const ordered = Array.isArray(section.ordered_items) && section.ordered_items.length ? `<ol>${section.ordered_items.map((text) => `<li>${mdLinks(esc(text), affiliateUrls, affiliateMeta, briefPageSlug, sourceUrls)}</li>`).join("")}</ol>` : "";
    const bullets = Array.isArray(section.bullets) && section.bullets.length ? `<ul>${section.bullets.map((text) => `<li>${mdLinks(esc(text), affiliateUrls, affiliateMeta, briefPageSlug, sourceUrls)}</li>`).join("")}</ul>` : "";
    const tables = (section.tables || []).map((table) => `<div class="table-scroll responsive-table"><table><caption>${esc(heading)}</caption><thead><tr>${(table.headers || []).map((cell) => `<th scope="col">${mdLinks(esc(cell), affiliateUrls, affiliateMeta, briefPageSlug, sourceUrls)}</th>`).join("")}</tr></thead><tbody>${(table.rows || []).map((row) => `<tr>${row.map((cell, cellIndex) => cellIndex === 0 ? `<th scope="row">${mdLinks(esc(cell), affiliateUrls, affiliateMeta, briefPageSlug, sourceUrls)}</th>` : `<td>${mdLinks(esc(cell), affiliateUrls, affiliateMeta, briefPageSlug, sourceUrls)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`).join("");
    return { heading, id, html: `<section aria-labelledby="${id}"><h2 id="${id}">${esc(heading)}</h2>${paragraphs}${ordered}${bullets}${tables}</section>` };
  });
  const toc = sectionRows.map((section) => `<li><a href="#${section.id}">${esc(section.heading)}</a></li>`).join("");
  const relatedItems = Array.isArray(brief.internal_links) && brief.internal_links.length
    ? brief.internal_links
    : [{ label: "Browse all research guides", path: "/guides/" }, { label: "Compare your priorities privately", path: "/quiz/provider-fit/?source=guide-related" }];
  const related = relatedItems.map((item) => `<li><a href="${esc(item.path)}">${esc(item.label || item.anchor)}</a></li>`).join("");
  // Repair D (monotony): renderGuideChecklist above already renders this exact
  // brief.pros/brief.cons pair as "What to verify" (Confirm / Do not assume).
  // This second pass over the SAME arrays ("Quick evidence check") is a real
  // content repeat, not new information — every word stays (copy-preservation
  // gate: nothing is deleted), but beyond the first ("What to verify")
  // occurrence it collapses into the shared cs-details accordion instead of a
  // second fully-expanded card, so the page stops reading as the same block
  // twice in a row. <summary> replaces the <h2> as the disclosure label; the
  // heading text itself is unchanged.
  const prosCons = (brief.pros || []).length || (brief.cons || []).length ? `<details class="cs-details"><summary>Quick evidence check</summary><div class="guide-checklist-grid"><div><h3>What the sources establish</h3><ul>${(brief.pros || []).map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div><div><h3>What still needs verification</h3><ul>${(brief.cons || []).map((item) => `<li>${esc(item)}</li>`).join("")}</ul></div></div></details>` : "";
  // ED §3 shared elements: the proximate disclosure sits directly above the table /
  // module. The top-of-page `.affiliate-disclosure` aside already precedes it; a
  // single top-of-page line on a ~1,600-word page is the FTC-prominence weakness
  // this closes (TD D5/421).
  const monetizedComparison = blogComparisonSpecVersion(comparisonSpec) === "v2";
  const proximateDisclosure = monetizedComparison ? `<p class="table-disclosure">${esc(GUIDE_TABLE_DISCLOSURE)}</p>` : "";
  const comparison = comparisonSpec ? `<section class="guide-comparison"><h2>Provider facts side by side</h2>${proximateDisclosure}${renderComparisonTable(comparisonSpec)}</section>` : "";
  // ED §4.3: the offer module rides the same v2 gate + link map as the table. It is
  // placed by the brief (`offer_module.after_section_id`) so the ruling's §3.5/§3.6
  // placement decisions are data, not a hard-coded index.
  const offerModuleSpec = brief.offer_module && brief.offer_module.rows?.length
    ? { schema: "blog-comparison-spec-v2", live_link_allowed: true, operator_gate_receipt: EDITORIAL_MONETIZATION_GATE_RECEIPT_SHA256, page_slug: brief.offer_module.page_slug, source_checked_through: brief.offer_module.source_checked_through || brief.source_checked_through, date_modified: brief.offer_module.source_checked_through || brief.source_checked_through, path: brief.canonical_path }
    : null;
  const offerModule = offerModuleSpec ? renderGuideOfferModule(brief.offer_module, programs || ledgerPrograms(), offerModuleSpec) : "";
  const sources = renderGuideSources(brief);
  const schema = {
    "@context": "https://schema.org",
    "@type": "Article",
    headline: brief.h1,
    description,
    url: canonical,
    datePublished: brief.updated_at,
    dateModified: brief.updated_at,
    author: { "@type": author.entity_type === "Organization" ? "Organization" : "Person", name: author.display_name, url: `https://glpclinicscout.com${author.bio_path}` },
    publisher: { "@type": "Organization", name: "GLP Clinic Scout", url: "https://glpclinicscout.com/" },
    citation: (brief.sources || []).map((source) => source.url),
  };
  const faqSchema = faqs.length
    ? { "@context": "https://schema.org", "@type": "FAQPage", "@id": `${canonical}#faq`, mainEntity: faqs.map((item) => ({ "@type": "Question", name: item.q, acceptedAnswer: { "@type": "Answer", text: item.a } })) }
    : null;
  // Design pass 2026-08-26 (PAGE-SPECS-20260826/insights spec §2 + §4.1):
  // content-first article order. Cinematic photo hero (H1 + byline on a scrim,
  // CSS: guide-hero--cinema in 40-editorial.css) → one-line freshness band →
  // the direct answer styled as the article lede (not a boxed card) → compact
  // disclosure → body sections immediately. The evidence machinery (claim map,
  // owned data tables, checklist) moves BEHIND the article as an appendix next
  // to the sources it cites — nothing is removed, it stops being pre-content
  // furniture. addGuideFunnel still finds `<section class="answer">`, pageHero/
  // responsiveHero still find the editorial-evidence-hero picture stems.
  const rendered = `<!doctype html><html lang="en" data-release="${indexable ? "release-ready" : "held"}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="${indexable ? "index,follow,max-image-preview:large" : "noindex,nofollow,noarchive,nosnippet,max-image-preview:large"}"><title>${esc(brief.title)}</title><meta name="description" content="${esc(description)}"><link rel="canonical" href="${esc(canonical)}"><script type="application/ld+json">${JSON.stringify(schema).replaceAll("<", "\\u003c")}</script>${faqSchema ? `<script type="application/ld+json">${JSON.stringify(faqSchema).replaceAll("<", "\\u003c")}</script>` : ""}</head><body><main class="guide-shell">${GUIDE_STYLE}<nav class="breadcrumbs" aria-label="Breadcrumb"><a href="/">Home</a><span aria-hidden="true">/</span><a href="/insights/">Guides</a><span aria-hidden="true">/</span><span aria-current="page">${esc(brief.h1)}</span></nav><section class="guide-hero guide-hero--cinema"><div class="guide-hero-copy"><p class="kicker">${esc(humanTopic(brief) === "Source-checked research" ? "Source-checked guide" : `${humanTopic(brief)} · source-checked guide`)}</p><h1>${esc(brief.h1)}</h1><p class="guide-byline">By <a href="${esc(author.bio_path)}">the ${esc(author.display_name)}</a> · Updated ${esc(sourceCheckedDate)}</p></div><picture><source srcset="/assets/images/editorial-evidence-hero-1600.avif" type="image/avif"><source srcset="/assets/images/editorial-evidence-hero-1600.webp" type="image/webp"><img src="/assets/images/editorial-evidence-hero-1600.jpg" alt="Generated editorial evidence map for ${esc(brief.h1)}" width="1600" height="900" loading="eager" fetchpriority="high"></picture></section><p class="guide-evidence-band">Sources checked ${esc(sourceCheckedDate)}${brief.surface_taxonomy === "medical" ? ` · Prices checked <time datetime="${esc(brief.prices_checked_date)}" data-prices-checked-date>${esc(brief.prices_checked_date)}</time>` : ""} · Prices and terms can change.</p><section class="answer"><div class="panel evidence-line quick-answer"><p class="guide-lede">${esc(brief.direct_answer)}</p></div></section><aside class="disclosure"><strong>Evidence boundary.</strong> ${esc(brief.medical_advice_disclaimer || "This guide does not diagnose, prescribe, or replace a licensed professional.")} ${brief.surface_taxonomy === "commercial" && brief.inline_ftc_disclosure ? `<span class="affiliate-disclosure"><strong>Affiliate disclosure:</strong> ${esc(brief.inline_ftc_disclosure_text)}</span>` : ""}</aside><nav class="guide-toc" aria-label="In this guide"><p class="eyebrow">In this guide</p><ol>${toc}<li><a href="#verified-claims">Verified claims</a></li>${faqs.length ? '<li><a href="#faq">Frequently asked questions</a></li>' : ""}<li><a href="#sources">Sources</a></li></ol></nav>${comparison}${placeSections(sectionRows, offerModule, brief.offer_module?.after_section_id)}${renderInformationGain(brief)}${brief.suppress_cta ? "" : renderNextStepCard("guide-early")}${renderGuideFaq(faqs)}<nav class="related-guides" aria-label="Related Clinic Scout guides"><h2>Keep comparing</h2><ul>${related}</ul></nav><section class="guide-appendix" aria-label="Evidence appendix">${renderGuideChecklist(brief)}${renderClaimEvidence(brief)}${renderGuideFacts(brief)}${prosCons}</section><section class="guide-sources" id="sources"><h2>Sources and what they support</h2><ol>${sources}</ol></section>${brief.suppress_cta ? "" : renderNextStepCard("guide-end")}<aside><strong>Scope boundary:</strong> This guide does not diagnose, prescribe, authenticate a product, determine individual eligibility, or replace a licensed professional, regulator, insurer, pharmacist, or program administrator.</aside></main></body></html>`;
  // The capture is a site-owned, explicit-consent content upgrade, not an
  // affiliate CTA. Every article/guide template gets the same capture surface;
  // suppress_cta continues to suppress only the commercial next-step cards.
  return addP1ContentUpgradeSurface(rendered, brief.canonical_path.replace(/^\//, ""));
}

export const blogPolicy = Object.freeze({
  pacePerDay: [1, 3],
  requiredBrief: true,
  riskTiers: { LOW: "commercial/comparison; source check", MEDIUM: "primary medical citations plus evidence-review receipt", HIGH: "blocked or reframed" },
  clinicianRequired: false,
  honestAttribution: true,
  wordCount: [1500, 3600],
  publishStates: ["HELD", "PASS"],
});