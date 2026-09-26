import { selectedFamilyStyles } from "../src/selected-family-styles.mjs";
import { resetTrackingAllowedHostsCache } from "../src/tracking-allowed-hosts.mjs";
import { eligibilityCensus } from "../src/held-eligibility-census.mjs";
import { execFileSync } from "node:child_process";
import { cp, mkdir, readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { createHash } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {resolveDeployedPricingBaseline} from '../src/pricing-coverage-floor.mjs';
import {isComparableCashRow,PRICE_STATE_DISCLOSURE,priceStateText,stripPriceStateDisclosures} from '../src/price-state.mjs';
import { affiliateCta, renderMoneyPage, renderWordmark, scoreProvider } from "../src/money-page.mjs";
import { decorateProviderChips, wordmarkFitsFor } from "../src/provider-chip.mjs";
import { containTables } from "../src/table-containment.mjs";
import { WORDMARK_CSS } from "../src/provider-wordmark.mjs";
import { buildProviderReviewSpecs, normalizeFrozenScoreState, renderProviderReviewPage } from "../src/provider-review.mjs";
import { renderQuiz } from "../src/quiz.mjs";
import { renderPaidQuiz } from "../src/paid-quiz.mjs";
import { renderMatchesCustomized } from "../src/matches-customized.mjs";
import { renderAdsLandingPage, resolveHeadlineTokens } from "../src/ads-landing.mjs";
import { renderBlogPost, resolveBlogComparisonSpec, reviewBrief, validateKeywordOwnership } from "../src/blog-system.mjs";
import { renderDirectoryPage, evaluateDirectoryGate } from "../src/directory.mjs";
import { consentRuntimeSource } from "../src/consent-runtime.mjs";
import { candidateSharedRuntime, candidateExportPolicyMarkup } from "../src/candidate-export-policy.mjs";
import { buildSchemaGraph } from "../src/schema.mjs";
import { buildCostCalculatorData, renderCostCalculator } from "../src/cost-calculator.mjs";
import { costCalculatorDropdownProviders as costCalculatorDropdown } from "../src/cost-calculator.mjs";
import { costAffiliateEmailManifest, costCalculatorAffiliateLinks, decorateCostCalculatorAffiliateSurface } from "../src/cost-calculator-affiliate.mjs";
import { COST_AFFILIATE_EMAIL_MANIFEST } from "../src/cost-affiliate-email-manifest.generated.mjs";
import { assertLiveSurfaceParity, deriveGoEntriesFromRegistry, deriveReviewRoutesFromRegistry, projectMoneyPagesFromRegistry, projectQuizFromRegistry } from "../src/program-registry.mjs";
import { applyWave2Repairs, finalizeReviewIntakeRoute } from "./wave2-repairs.mjs";
import { applyWave2P1Surfaces } from "../src/wave2-p1-surfaces.mjs";
import { manifestJsonText, renderGoRuntimeManifestModule } from "../src/go-runtime-manifest.mjs";
import { buildPriceTrackerArtifacts } from "../src/price-tracker.mjs";
import { buildPriceIndexArtifacts, PRICE_INDEX_ROUTE, rebindPriceIndexShippedHtml } from "../src/price-index.mjs";
import { readPriceIndexPredecessor } from "../src/price-index-predecessor.mjs";
import { frozenPriceLedger } from "../src/price-index-nonprice-binding.mjs";
import { composePriceWatchIssue, validatePublishedPriceWatchIssue } from "../src/price-watch-email.mjs";
import { buildMysteryShopArtifacts, EXPECTED_LEDGER_FILE_SHA256 } from "../src/mystery-shop.mjs";
import { buildDealArtifacts } from "../src/deal-ingestion.mjs";
import { validatePromoRecord } from "../src/promo-code.mjs";
import { activeCouponMap } from "../src/coupon-offers.mjs";
import { buildStudyArtifacts } from "../src/original-data.mjs";
import { buildReady6EditorialPages } from "../src/ready6-editorial.mjs";
import { applyMeritScores, meritScore } from "../src/merit-ranking.mjs";
import { ALL_IN_MONTHS, applyPricingToCandidates } from "../src/quiz-ranking.mjs";
import { enforceReviewDepth, MIN_VISIBLE_REVIEW_WORDS } from "../src/review-depth.mjs";
import { assertDeclaredIdentityGroups, assertUniqueRenderIdentity } from "../src/provider-identity.mjs";
import { assertStampNotStale, deriveMoneyPageFreshness, deriveReviewFreshness, maxDate } from "../src/freshness.mjs";
import { projectTherapeuticSurfaces, reconcileTherapeuticTaxonomyWireIn, stableSha256 } from "../src/therapeutic-taxonomy.mjs";
import { comparablePriceProviderIds, providerSuggestions } from "../src/provider-dynamic-profiles.mjs";
import { normalizePriceIndexBuild4Semantics } from "../src/price-index-semantic-preservation.mjs";
import { assertPriceIndexLiveCount, projectPreservedPriceIndexLiveCount, substitutePriceIndexLiveCount } from "../src/price-index-live-count.mjs";
import { addArticleShareControls } from "../src/pass2-editorial-share.mjs";
import { upgradePhase3PageVisuals } from "../src/pass2-page-visual-upgrades.mjs";
import { renderReviewDirectoryPage, renderReviewIntakeForm, renderReviewSubmissionPage } from "../src/pass2-review-hub.mjs";
import { loadAffiliateLinkMap } from "../src/affiliate-link-map.mjs";
import { loadProviderPageContracts } from "../src/provider-cta-contract.mjs";
import { loadGoPromotionAuthority, restoreGoOnlyPriceBasis } from "../src/go-runtime-selection.mjs";

const MONTH_NAMES = Object.freeze(["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]);
import { assertTalliesDerived, isTallyGatedRoute, redriveTallies, TALLY_TOKENS } from "../src/surface-tallies.mjs";

import { leanLegalPages } from "../src/legal-pages.mjs";
import { buildRobotsTxt } from "../src/robots.mjs";
import { buildPhase3PageGraph, upgradeInsightsArticle , PHASE3_INSIGHTS_EXCLUSIONS } from "../src/phase3-page-graph.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DATA = path.join(ROOT, "data");
const PUBLIC = path.join(ROOT, "public");
const OPS = path.join(ROOT, "artifacts", "ops");
const BUILD4_PRESERVATION = path.join(ROOT, "artifacts", "build4-preservation");
const SITE_PROFILE = "glp-clinic-scout";
const WAVE4_PRICE_ROUTE_FILE = "tools/glp-1-price-tracker/index.html";
const PRICE_INDEX_ROUTE_FILE = "glp-1-price-index/index.html";
const WAVE4_OFFER_ROUTE_FILE = "tools/glp-1-savings-offer-monitor/index.html";
const WAVE4_EVIDENCE_ROUTE_FILE = "research/public-funnel-evidence/index.html";
const WAVE4_DATA_ROUTE_FILE = "research/original-data/public-glp-1-program-pricing-sample/index.html";
const WAVE4_CLEAN_ROOM_ROUTE_FILES = new Set([WAVE4_PRICE_ROUTE_FILE, WAVE4_OFFER_ROUTE_FILE, WAVE4_EVIDENCE_ROUTE_FILE, WAVE4_DATA_ROUTE_FILE]);
const SEO_NO_CTA_ROUTE_FILE = "guides/wegovy-pills-oral-semaglutide-cost/index.html";
// Operator-approved recovered article. Snapshot the approved predecessor bytes
// before the build clears public/ and skip the redesign renderer. Pass 2 may add
// governed, idempotent capture/share controls, but does not rewrite its evidence.
const MOUNJARO_COUPON_ROUTE_FILE = "guides/mounjaro-coupon/index.html";
const PASS2_MIGRATED_LEGACY_REVIEW_FILES = new Set([
  "reviews/coivas/index.html",
  "reviews/direct-meds/index.html",
  "reviews/mangorx/index.html",
  "reviews/sprout-health/index.html",
]);
// The public index is finalized explicitly before its manifest hash is minted.
// B-17: the paid landing family bypasses the P1 surface finalizer too. That
// finalizer injects the sitewide email-capture module, and SPEC-ADS §3.3 bans
// email capture on a paid LP outright — the LP's only job is the qualified
// outbound click, and a lead form on it is both a policy risk and a conversion
// tax on bought traffic.
const PHASE3_PAID_LP_ROUTE_FILES = Object.freeze([
  "lp/best-providers/compounded-semaglutide/index.html",
  "lp/best-providers/compounded-tirzepatide/index.html",
]);
const ADS_LP_ROUTE_FILES = new Set(["lp/r/orderlymeds/index.html", ...PHASE3_PAID_LP_ROUTE_FILES]);
const WAVE4_FINALIZER_BYPASS = new Set([WAVE4_PRICE_ROUTE_FILE, PRICE_INDEX_ROUTE_FILE, WAVE4_OFFER_ROUTE_FILE, WAVE4_DATA_ROUTE_FILE, SEO_NO_CTA_ROUTE_FILE, ...ADS_LP_ROUTE_FILES]);
const WAVE1_PUBLIC = path.resolve(ROOT, "../../releases/glp-wave1-20260725/public");
const STYLESHEET_ASSETS = ["site.css", "site-home.css", "site-money.css", "site-review.css", "site-editorial.css", "site-tools.css", "site-trust.css", "site-p3.css"];
const ANALYTICS_ASSET_SOURCE = await readFile(path.join(ROOT, "assets", "analytics.js"));
const ANALYTICS_ASSET_FILENAME = `analytics.${createHash("sha256").update(ANALYTICS_ASSET_SOURCE).digest("hex").slice(0, 16)}.js`;
const ASSET_VERSION = createHash("sha256").update(Buffer.concat([
  ...await Promise.all([
    ...STYLESHEET_ASSETS.map((file) => readFile(path.join(ROOT, "assets", file))),
    readFile(path.join(ROOT, "assets", "motion-loader.js")),
    readFile(path.join(ROOT, "assets", "motion.js")),
    readFile(path.join(ROOT, "assets", "site.js")),
    ANALYTICS_ASSET_SOURCE,
    readFile(path.join(ROOT, "assets", "review-hub.js")),
    readFile(path.join(ROOT, "assets", "editorial-share.js")),
    readFile(path.join(ROOT, "assets", "quiz-page.js")),
    readFile(path.join(ROOT, "assets", "paid-quiz.js")),
    readFile(path.join(ROOT, "assets", "paid-quiz.css")),
    readFile(path.join(ROOT, "assets", "meta-pixel.js")),
    readFile(path.join(ROOT, "assets", "meta-lead.js")),
    readFile(path.join(ROOT, "assets", "matches-customized.js")),
    readFile(path.join(ROOT, "assets", "review-intake.js")),
  ]),
  Buffer.from(consentRuntimeSource),
  Buffer.from(candidateExportPolicyMarkup),
])).digest("hex").slice(0, 16);
const readJson = async (relative) => relative === "provider-page-contracts.json"
  ? loadProviderPageContracts(path.dirname(DATA))
  : JSON.parse(await readFile(path.join(DATA, relative), "utf8"));
// PostHog restore (task #64 / CRO P0-4). The PROJECT api key is a publishable
// client-side key - it ships in the page by design, exactly like the GA4
// measurement id above. The privacy contract travels with it and is asserted by
// scripts/validate-instrumentation.mjs.
const postHogRuntimeSource = JSON.parse(await readFile(path.join(DATA, "posthog-config.json"), "utf8"));
if (postHogRuntimeSource.schema !== "glp-posthog-runtime-v1") throw new Error("posthog_runtime_schema_invalid");
if (!/^phc_[A-Za-z0-9]{10,}$/.test(postHogRuntimeSource.api_key || "")) throw new Error("posthog_runtime_key_invalid");
for (const [key, expected] of Object.entries({ autocapture: false, disable_session_recording: true, mask_all_text: true, mask_all_element_attributes: true })) {
  if (postHogRuntimeSource.privacy_contract?.[key] !== expected) throw new Error(`posthog_privacy_contract_invalid:${key}`);
}
const postHogRuntimeConfig = {
  enabled: postHogRuntimeSource.enabled === true,
  api_key: postHogRuntimeSource.api_key,
  host: postHogRuntimeSource.host,
  project_id: postHogRuntimeSource.project_id,
  operator_gate: postHogRuntimeSource.operator_gate,
};
const INDEXNOW_KEY_PATTERN = /^[A-Za-z0-9-]{8,128}$/;

function validateIndexNowConfig(config) {
  if (config?.schema !== "glp-indexnow-v1") throw new Error("indexnow_schema_invalid");
  if (config.host !== "glpclinicscout.com") throw new Error("indexnow_host_invalid");
  if (!INDEXNOW_KEY_PATTERN.test(config.key || "")) throw new Error("indexnow_key_invalid");
  if (config.key_location !== `https://${config.host}/${config.key}.txt`) throw new Error("indexnow_key_location_invalid");
  if (config.endpoint !== "https://api.indexnow.org/indexnow") throw new Error("indexnow_endpoint_invalid");
  if (!Array.isArray(config.notify_urls) || config.notify_urls.length === 0 || config.notify_urls.length > 10000) throw new Error("indexnow_notify_urls_invalid");
  if (new Set(config.notify_urls).size !== config.notify_urls.length || config.notify_urls.some((url) => {
    try { return new URL(url).hostname !== config.host || new URL(url).protocol !== "https:"; } catch { return true; }
  })) throw new Error("indexnow_notify_url_scope_invalid");
  return Object.freeze(config);
}

// 2026-08-06 surface unification, extended 2026-08-06 (cluster 1). No tally a
// reader can see is typed into a data file. Copy carries {{LIVE_PROVIDER_COUNT}},
// {{COMPARISON_PAGE_COUNT}} and {{RESEARCH_GUIDE_COUNT}}; every rendered byte is
// substituted here from counts derived off the data and the tree that is actually
// being written. Then assertTalliesDerived re-reads the rendered bytes and throws
// on ANY provider / comparison / guide total the shipped tree cannot reproduce -
// in prose or in a labelled metric tile - so a hand-typed number cannot survive a
// build. An unresolved token fails the build rather than shipping a brace.
const ANY_TOKEN = /\{\{[A-Z_]+\}\}/;
const derivedTallies = Object.fromEntries(TALLY_TOKENS.map((token) => [token, null]));
const buildDerivations = {
  live_provider_count: null, comparison_page_count: null, research_guide_count: null,
  token_substitutions: Object.fromEntries(TALLY_TOKENS.map((token) => [token, 0])),
  merit_scores_derived: [], identity_groups: null, freshness: {}, tally_gated_routes: 0,
  therapeutic_taxonomy: null,
  frozen_byte_tally_rewrites: [], frozen_byte_score_rewrites: [],
};

function resolveDerivedCounts(content, relative) {
  if (typeof content !== "string") return content;
  for (const token of TALLY_TOKENS) {
    const marker = `{{${token}}}`;
    if (!content.includes(marker)) continue;
    if (!Number.isFinite(derivedTallies[token])) throw new Error(`tally_not_derived_yet:${token}`);
    buildDerivations.token_substitutions[token] += content.split(marker).length - 1;
    content = content.replaceAll(marker, String(derivedTallies[token]));
  }
  if (relative.endsWith(".html") && ANY_TOKEN.test(content)) throw new Error(`unresolved_build_token:${relative}`);
  if (relative.endsWith(".html") && isTallyGatedRoute(relative)) {
    assertTalliesDerived(relative, content, derivedTallies);
    buildDerivations.tally_gated_routes += 1;
  }
  if (relative.endsWith(".html") && PROVIDER_SURFACE_ROUTE.test(relative)) {
    buildDerivations.freshness[relative] = assertStampNotStale(relative, content);
  }
  return content;
}

//: Routes whose "updated" stamp is a claim about provider data freshness.
const PROVIDER_SURFACE_ROUTE = /^(?:best|cost|compare|reviews)\//;

//: Every table the build writes, on every template, gets containment here.
//: 2026-08-18: doing it at the single write funnel (rather than in each
//: renderer) is what makes a FUTURE provider review inherit the fix - the
//: review template never has to remember to wrap anything.
const tableContainment = { pages: 0, wrapped: 0, prose: 0, data: 0, already_contained: 0 };

function containPageTables(relative, content) {
  if (typeof content !== "string" || !relative.endsWith(".html")) return content;
  const result = containTables(content, `/${relative.replace(/index\.html$/, "")}`);
  tableContainment.pages += 1;
  tableContainment.wrapped += result.wrapped;
  tableContainment.prose += result.prose;
  tableContainment.data += result.data;
  tableContainment.already_contained += result.alreadyContained;
  return result.html;
}

// REBUILD B-14 (LAY §3.3): a visible breadcrumb row on every programmatic
// template. glpwinner has neither the row nor the markup (§A4) — we already ship
// `BreadcrumbList` on all 111 programmatic pages, but the money and review
// templates never rendered the row a reader can see and click.
//
// It is derived FROM the page's own BreadcrumbList JSON-LD rather than composed
// beside it, so the visible row and the structured data can never disagree, and
// a template built next month inherits the row without remembering anything.
const breadcrumbRows = { added: 0, already_present: 0, no_graph: 0 };
const escapeText = (value) => String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

// Phase-4 gate remediation 2026-08-27 (GATES-PRELIM issue #7): these two
// routes carry BreadcrumbList JSON-LD (restored/added this pass) but their
// operator-approved layouts deliberately ship NO visible breadcrumb chrome —
// the quiz intro was compacted by operator order so the quiz is visible
// without scrolling (see the blessed removals in copy-equivalence-map.json),
// and the calculator's split workspace is pinned to a single laptop viewport.
// Schema-only breadcrumbs are the deliberate outcome here; deriving a visible
// row would reintroduce the removed chrome.
const SCHEMA_ONLY_BREADCRUMB_ROUTES = new Set(["quiz/provider-fit/index.html", "tools/glp-1-cost-calculator/index.html"]);

function addBreadcrumbRow(relative, content) {
  if (typeof content !== "string" || !relative.endsWith(".html")) return content;
  if (SCHEMA_ONLY_BREADCRUMB_ROUTES.has(relative)) { breadcrumbRows.already_present += 1; return content; }
  if (/aria-label="Breadcrumb"/i.test(content)) { breadcrumbRows.already_present += 1; return content; }
  const match = content.match(/"@type":\s*"BreadcrumbList"[\s\S]*?"itemListElement":\s*(\[[\s\S]*?\}\s*\])/);
  if (!match) { breadcrumbRows.no_graph += 1; return content; }
  let items;
  try { items = JSON.parse(match[1]); } catch { breadcrumbRows.no_graph += 1; return content; }
  if (!Array.isArray(items) || items.length < 2) { breadcrumbRows.no_graph += 1; return content; }
  const crumbs = items
    .slice()
    .sort((a, b) => Number(a.position || 0) - Number(b.position || 0))
    .map((item, index, all) => {
      const name = escapeText(item.name || "");
      const href = String(item.item || "").replace(/^https:\/\/glpclinicscout\.com/, "") || "/";
      return index === all.length - 1
        ? `<span aria-current="page">${name}</span>`
        : `<a href="${escapeText(href)}">${name}</a>`;
    })
    .join('<span aria-hidden="true">/</span>');
  const row = `<nav class="breadcrumbs" aria-label="Breadcrumb">${crumbs}</nav>`;
  const anchored = content.replace(
    /(<main\b[^>]*>(?:<div class="page-band" aria-hidden="true"><\/div>)?)/i,
    (whole) => `${whole}${row}`,
  );
  if (anchored === content) { breadcrumbRows.no_graph += 1; return content; }
  breadcrumbRows.added += 1;
  return anchored;
}

let retainedCarrierArticles;
const priceStateByRoute = new Map();
async function write(relative, content) {
  const destination = path.join(PUBLIC, relative);
  await mkdir(path.dirname(destination), { recursive: true });
  const retainedCarrierArticle = retainedCarrierArticles?.get(relative);
  if (relative === retainedCarrierArticle?.manifest.relative) {
    await writeFile(destination, retainedCarrierArticle.bytes);
    return;
  }
  if (typeof content === "string" && relative.endsWith(".html") && !WAVE4_FINALIZER_BYPASS.has(relative)) content = applyWave2P1Surfaces(content, relative);
  if (typeof content === 'string' && priceStateByRoute.has(relative) && !content.includes('data-current-price-state')) {
    const text=priceStateByRoute.get(relative).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const evidenceDate=maxDate([...text.matchAll(/\b(20\d{2}-\d{2}-\d{2})\b/g)].map(m=>m[1]));
    if (evidenceDate) content=content.replace(/"dateModified":"(\d{4}-\d{2}-\d{2})"/g,(full,date)=>`"dateModified":"${maxDate([date,evidenceDate])}"`);
    if (!content.includes('</main>')) throw new Error('price_state_review_main_missing:'+relative);
    content=content.replace('</main>',`<aside class="panel" data-current-price-state><h2>Reported pricing information</h2><p>${text}</p><p>Provider eligibility for this directory is separate from eligibility for cash-price arithmetic.</p></aside></main>`);
  }
  await writeFile(destination, containPageTables(relative, addBreadcrumbRow(relative, resolveDerivedCounts(content, relative))));
}

async function countPublicGoAnchors(directory) {
  let count = 0;
  const walk = async (current) => {
    for (const entry of await readdir(current, { withFileTypes: true })) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) { await walk(full); continue; }
      if (!entry.name.endsWith(".html")) continue;
      const html = await readFile(full, "utf8");
      count += (html.match(/href="\/go\/[a-z0-9][a-z0-9-]{1,79}(?:\?|\")/g) || []).length;
    }
  };
  await walk(directory);
  return count;
}

// Clean-room evidence routes bypass shared navigation, analytics, consent,
// provider, and other runtime finalizers by design.
async function writeCleanRoom(relative, content) {
  const destination = path.join(PUBLIC, relative);
  await mkdir(path.dirname(destination), { recursive: true });
  // Clean-room routes bypass the shared RUNTIME finalizers by design, but a
  // table that runs off the side of the screen is a layout defect, not a
  // runtime, so containment applies here too.
  await writeFile(destination, containPageTables(relative, resolveDerivedCounts(content, relative)));
}

function exactInlineStyleCspSource(html, relative) {
  const styles = [...html.matchAll(/<style(?:\s[^>]*)?>([\s\S]*?)<\/style>/gi)].map((match) => match[1]);
  if (styles.length !== 1) throw new Error(`standalone_style_count_invalid:${relative}:${styles.length}`);
  if (/\sstyle\s*=/i.test(html)) throw new Error(`standalone_inline_style_attribute_forbidden:${relative}`);
  return `'sha256-${createHash("sha256").update(styles[0]).digest("base64")}'`;
}

function upsertRouteCsp(headers, routePattern, csp) {
  const marker = `\n${routePattern}\n`;
  const markerIndex = headers.indexOf(marker);
  if (markerIndex < 0) throw new Error(`standalone_header_route_missing:${routePattern}`);
  const blockStart = markerIndex + 1;
  const nextBlock = headers.indexOf("\n\n", blockStart);
  const blockEnd = nextBlock < 0 ? headers.length : nextBlock;
  const block = headers.slice(blockStart, blockEnd);
  const header = `  Content-Security-Policy: ${csp}`;
  const hardened = /\n  Content-Security-Policy: [^\n]*/.test(block)
    ? block.replace(/\n  Content-Security-Policy: [^\n]*/, `\n${header}`)
    : `${block}\n${header}`;
  return `${headers.slice(0, blockStart)}${hardened}${headers.slice(blockEnd)}`;
}

async function writeOps(relative, content) {
  const destination = path.join(OPS, relative);
  await mkdir(path.dirname(destination), { recursive: true });
  await writeFile(destination, content);
}


/** Contain every table in the BUILT tree, whichever pass wrote it. */
async function sweepTableContainment(root) {
  const totals = { pages: 0, changed: 0, wrapped: 0, prose: 0, data: 0, already_contained: 0 };
  const walk = async (directory) => {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const full = path.join(directory, entry.name);
      if (entry.isDirectory()) { await walk(full); continue; }
      if (!entry.name.endsWith(".html")) continue;
      const before = await readFile(full, "utf8");
      totals.pages += 1;
      const result = containTables(before, `/${path.relative(root, full).replace(/index\.html$/, "")}`);
      totals.wrapped += result.wrapped;
      totals.prose += result.prose;
      totals.data += result.data;
      totals.already_contained += result.alreadyContained;
      if (result.html !== before) { await writeFile(full, result.html); totals.changed += 1; }
    }
  };
  await walk(root);
  return totals;
}


// SITE-BUILD-SOP gate 1 (dead control) + gate 20 (link destination), applied to
// the breadcrumb rows B-14 introduces. A crumb's parent route is whatever the
// page's BreadcrumbList names — and some of those parents (`/reviews/`) are hub
// surfaces the rebuild has not built YET. A link to a route the tree does not
// contain is a 404 in a reader's hands, so this sweep downgrades exactly those
// crumbs to plain text. When the hub ships, the crumb becomes a link again with
// no template edit: the sweep reads the tree, not a list.
async function sweepBreadcrumbLinkIntegrity(root) {
  const totals = { pages: 0, links: 0, downgraded: 0, targets: {} };
  const exists = new Map();
  const routeIsBuilt = async (href) => {
    if (!href.startsWith("/") || href.startsWith("//")) return true;
    const clean = href.split(/[?#]/)[0];
    if (exists.has(clean)) return exists.get(clean);
    const candidate = clean.endsWith("/") ? path.join(root, clean, "index.html") : path.join(root, clean);
    let ok = true;
    try { await stat(candidate); } catch { ok = false; }
    exists.set(clean, ok);
    return ok;
  };
  const walk = async (directory) => {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const full = path.join(directory, entry.name);
      if (entry.isDirectory()) { await walk(full); continue; }
      if (!entry.name.endsWith(".html")) continue;
      const before = await readFile(full, "utf8");
      const row = before.match(/<nav class="breadcrumbs"[^>]*>[\s\S]*?<\/nav>/i);
      if (!row) continue;
      totals.pages += 1;
      let changed = row[0];
      for (const link of row[0].matchAll(/<a href="([^"]+)">([\s\S]*?)<\/a>/g)) {
        totals.links += 1;
        if (await routeIsBuilt(link[1])) continue;
        totals.downgraded += 1;
        totals.targets[link[1]] = (totals.targets[link[1]] || 0) + 1;
        changed = changed.replace(link[0], `<span>${link[2]}</span>`);
      }
      if (changed === row[0]) continue;
      await writeFile(full, before.replace(row[0], changed));
    }
  };
  await walk(root);
  return totals;
}


const RETIRED_DRUG_INTENT_ROUTES = Object.freeze({
  "compounded-semaglutide-injection": "/lp/best-providers/compounded-semaglutide/",
  "compounded-tirzepatide-injection": "/lp/best-providers/compounded-tirzepatide/",
  "brand-ozempic": "/ozempic/",
  "brand-wegovy": "/wegovy/",
  "brand-mounjaro": "/mounjaro/",
  "brand-zepbound": "/zepbound/",
});

const MUSTFIX_INTERNAL_ROUTE_REPLACEMENTS = Object.freeze({
  "/guides/": "/insights/",
  "/guides/cheapest-glp1/": "/providers/",
  "/guides/healthrx-vs-agelessrx/": "/providers/",
  "/guides/wellorithm-vs-enhance-md/": "/providers/",
  "/guides/compare-online-glp-1-provider-fees/": "/providers/",
  "/guides/glp-1-program-total-cost-comparison/": "/providers/",
  "/reviews/shed/": "/reviews/shedrx/",
});

async function sweepMustFixMarkup(root, dataFragments = []) {
  const fragmentHashes = new Map(dataFragments.map(({ file, sha256 }) => [file, sha256]));
  const totals = { pages: 0, changed: 0, retired_links_repaired: 0, redirect_links_repaired: 0, checked_2026_repaired: 0, related_coverage_repaired: 0, affiliate_rel_repaired: 0 };
  const walk = async (directory) => {
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const full = path.join(directory, entry.name);
      if (entry.isDirectory()) { await walk(full); continue; }
      if (!entry.name.endsWith(".html")) continue;
      const relative = path.relative(root, full).split(path.sep).join("/");
      if (WAVE4_CLEAN_ROOM_ROUTE_FILES.has(relative)) continue;
      const before = await readFile(full, "utf8");
      const retainedCarrierArticle = retainedCarrierArticles?.get(relative);
      if (relative === retainedCarrierArticle?.manifest.relative) {
        assertCarrierArticleBytes(retainedCarrierArticle, before);
        continue;
      }
      if (fragmentHashes.has(relative)) {
        // Hash-bound JSON islands are data, not prose for legacy HTML rewrites.
        // Reject earlier corruption instead of reminting the loader's digest.
        if (createHash("sha256").update(before).digest("hex") !== fragmentHashes.get(relative)) throw new Error(`paid_quiz_fragment_pre_sweep_drift:${relative}`);
        continue;
      }
      totals.pages += 1;
      let after = before.replace(/\/best\/online-glp-1-providers\/\?drug=([^"'&\s#]+)(?:#compare-cards)?/g, (whole, drug) => {
        totals.retired_links_repaired += 1;
        return RETIRED_DRUG_INTENT_ROUTES[drug] || "/providers/";
      });
      const genericRetired = (after.match(/\/best\/online-glp-1-providers\//g) || []).length;
      totals.retired_links_repaired += genericRetired;
      after = after.replaceAll("/best/online-glp-1-providers/", "/providers/");
      for (const [from, to] of Object.entries(MUSTFIX_INTERNAL_ROUTE_REPLACEMENTS)) {
        const needle = `href="${from}"`;
        const count = after.split(needle).length - 1;
        totals.redirect_links_repaired += count;
        after = after.replaceAll(needle, `href="${to}"`);
      }
      const checked = (after.match(/\b(?:was )?checked (?:through )?2026-\d{2}-\d{2}(?!\d)/gi) || []).length;
      totals.checked_2026_repaired += checked;
      const relatedCoverage = (after.match(/\brelated coverage\b/gi) || []).length;
      totals.related_coverage_repaired += relatedCoverage;
      after = after
        .replace(/\bwas checked (2026-\d{2}-\d{2})(?!\d)/gi, "is current as of $1")
        .replace(/\bchecked through (2026-\d{2}-\d{2})(?!\d)/gi, "current through $1")
        .replace(/\bchecked (2026-\d{2}-\d{2})(?!\d)/gi, "current as of $1")
        .replace(/\brelated coverage\b/gi, "connected coverage")
        .replaceAll("Every price on this site shows the date we verified it and links to its source.", "Compare total monthly cost, renewal terms, and included services before you choose.")
        .replaceAll("Every price dated", "Compare total monthly cost")
        .replaceAll("Source-dated · Commission-independent rubric · Private fit guide", "Clear monthly costs · Independent rubric · Private fit guide")
        .replaceAll("Source-dated Â· Commission-independent rubric Â· Private fit guide", "Clear monthly costs · Independent rubric · Private fit guide");
      after = after.replace(/<a\b[^>]*>/gi, (anchor) => {
        const monetized = /data-analytics-event="affiliate_click"/i.test(anchor) || /\brel="[^"]*\bsponsored\b/i.test(anchor);
        if (!monetized) return anchor;
        const rel = anchor.match(/\brel="([^"]*)"/i);
        if (rel) {
          const tokens = new Set(rel[1].split(/\s+/).filter(Boolean));
          if (tokens.has("nofollow")) return anchor;
          tokens.add("nofollow");
          totals.affiliate_rel_repaired += 1;
          return anchor.replace(rel[0], `rel="${[...tokens].join(" ")}"`);
        }
        totals.affiliate_rel_repaired += 1;
        return anchor.replace(/>$/, ' rel="sponsored nofollow noopener noreferrer">');
      });
      if (after !== before) { await writeFile(full, after); totals.changed += 1; }
    }
  };
  await walk(root);
  return totals;
}

const MUSTFIX_SHADOWED_REDIRECT_ROUTES = Object.freeze([
  "/best/online-glp-1-providers/",
  "/guides/",
  "/guides/cheapest-glp1/",
  "/guides/healthrx-vs-agelessrx/",
  "/guides/wellorithm-vs-enhance-md/",
  "/guides/compare-online-glp-1-provider-fees/",
  "/guides/glp-1-program-total-cost-comparison/",
  "/reviews/shed/",
]);

async function removeShadowedRedirectFiles(root) {
  let removed = 0;
  for (const source of MUSTFIX_SHADOWED_REDIRECT_ROUTES) {
    const relative = source.replace(/^\//, "").replace(/\/$/, "");
    const file = path.join(root, relative, "index.html");
    try { await stat(file); await rm(file, { force: true }); removed += 1; } catch {}
  }
  return { status: "PASS", removed_shadowing_index_files: removed };
}

/** First allowed alias for a route, from data/email-alias-map.json. */
function allowedAliasFor(map, route) {
  if (map?.schema !== "glp-public-email-alias-map-v1") throw new Error("email_alias_map_invalid");
  for (const rule of map.route_purposes || []) {
    if (new RegExp(rule.match).test(route)) return rule.allowed[0];
  }
  throw new Error(`email_alias_route_unmapped:${route}`);
}

async function readTreeBytes(root) {
  const files = new Map();
  async function walk(directory, prefix = "") {
    const entries = await readdir(directory, { withFileTypes: true });
    for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      const absolute = path.join(directory, entry.name);
      const relative = path.join(prefix, entry.name);
      if (entry.isDirectory()) await walk(absolute, relative);
      else if (entry.isFile()) files.set(relative, await readFile(absolute));
    }
  }
  await walk(root);
  if (!files.size) throw new Error(`predecessor_tree_empty:${root}`);
  return files;
}

async function readDurablePredecessorFile(source, snapshotRelative) {
  const snapshot = path.join(BUILD4_PRESERVATION, snapshotRelative);
  try {
    const bytes = await readFile(source);
    await mkdir(path.dirname(snapshot), { recursive: true });
    await writeFile(snapshot, bytes);
    return bytes;
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
    return readFile(snapshot);
  }
}

async function readDurablePredecessorTree(source, snapshotRelative) {
  const snapshotRoot = path.join(BUILD4_PRESERVATION, snapshotRelative);
  try {
    const files = await readTreeBytes(source);
    for (const [relative, bytes] of files) {
      const snapshot = path.join(snapshotRoot, relative);
      await mkdir(path.dirname(snapshot), { recursive: true });
      await writeFile(snapshot, bytes);
    }
    return files;
  } catch (error) {
    if (error?.code !== "ENOENT") throw error;
    return readTreeBytes(snapshotRoot);
  }
}

function framePage(title, body) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow,noarchive,nosnippet,max-image-preview:large"><title>${title}</title></head><body><main>${body}</main></body></html>`;
}

function renderPass5ComparisonHub(dateModified) {
  const medications = [
    [PASS5_MEDICATION_DESTINATIONS.compoundedSemaglutide, "Compounded semaglutide", "Published prices by provider"],
    [PASS5_MEDICATION_DESTINATIONS.compoundedTirzepatide, "Compounded tirzepatide", "Published prices by provider"],
    [PASS5_MEDICATION_DESTINATIONS.wegovyPill, "Wegovy pill", "Oral semaglutide paths"],
    [PASS5_MEDICATION_DESTINATIONS.ozempic, "Ozempic", "Brand-name provider paths"],
    [PASS5_MEDICATION_DESTINATIONS.wegovy, "Wegovy", "Brand-name provider paths"],
    [PASS5_MEDICATION_DESTINATIONS.mounjaro, "Mounjaro", "Brand-name provider paths"],
    [PASS5_MEDICATION_DESTINATIONS.zepbound, "Zepbound", "Brand-name provider paths"],
  ];
  const medicationLinks = medications.map(([href, label, note]) => `<a class="p5-compare-med" href="${href.replaceAll("&", "&amp;")}"><span>${label}</span><small>${note}</small><b aria-hidden="true">→</b></a>`).join("");
  const schema = JSON.stringify({
    "@context": "https://schema.org",
    "@graph": [
      {
        "@type": "BreadcrumbList",
        "@id": "https://glpclinicscout.com/compare/#breadcrumbs",
        itemListElement: [
          { "@type": "ListItem", position: 1, name: "Home", item: "https://glpclinicscout.com/" },
          { "@type": "ListItem", position: 2, name: "Compare", item: "https://glpclinicscout.com/compare/" },
        ],
      },
      {
        "@type": "WebPage",
        "@id": "https://glpclinicscout.com/compare/#page",
        name: "Choose a GLP-1 comparison path",
        url: "https://glpclinicscout.com/compare/",
        description: "Choose a medication-first or provider-first GLP-1 comparison path using source-dated Clinic Scout records.",
        dateModified,
      },
    ],
  });
  const body = `<nav class="breadcrumbs" aria-label="Breadcrumb"><a href="/">Home</a><span aria-hidden="true">/</span><span aria-current="page">Compare</span></nav><section class="p5-compare-hero"><p class="gcs-kicker">Comparison paths</p><h1>Start with the question you actually have.</h1><p class="lede">Choose a medication when you know what you want to research. Choose providers when you want to compare the market first. Both paths keep dated prices and evidence gaps visible.</p></section><section class="p5-compare-paths" aria-label="Choose a comparison path"><article class="p5-compare-path"><header><span class="p5-compare-number">01</span><div><p class="gcs-kicker">Medication first</p><h2>I know which medication I want to compare.</h2><p>Open the exact medication and form. Your choice stays attached to the destination instead of collapsing into a generic provider page.</p></div></header><div class="p5-compare-medications">${medicationLinks}</div></article><article class="p5-compare-path"><header><span class="p5-compare-number">02</span><div><p class="gcs-kicker">Provider first</p><h2>I want to compare programs before choosing a medication.</h2><p>Browse every live provider record, narrow the evidence with the fit guide, or estimate total cost before you decide what to research next.</p></div></header><div class="p5-compare-provider-actions"><a class="cs-btn cs-palette-action" href="/providers/">Compare all live providers</a><a class="cs-btn cs-btn--quiet" href="/quiz/provider-fit/">Find my provider fit</a><a class="cs-btn cs-btn--quiet" href="/tools/glp-1-cost-calculator/">Estimate total cost</a></div></article></section><aside class="p5-compare-boundary"><strong>What this comparison can do</strong><p>Clinic Scout organizes published program information. It does not choose a medication, diagnose a condition, or replace a licensed clinician.</p></aside>`;
  return framePage("Choose a GLP-1 comparison path · Clinic Scout", selectedFamilyStyles("/compare/") + body)
    .replace("<main>", '<main class="p3-page p5-compare-page" data-phase3-route="/compare/">')
    .replace("</head>", `<meta name="description" content="Choose a medication-first or provider-first GLP-1 comparison path using source-dated Clinic Scout records."><link rel="canonical" href="https://glpclinicscout.com/compare/"><script type="application/ld+json">${schema}</script></head>`);
}

const escHtml = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");

// Design pass 2026-08-20 (Fable edit 2): the 404 used to render as bare unstyled
// HTML - the one page a lost reader lands on looked like a server error. It is now
// built on the same cs-* design system as the rest of the site: gradient top strip,
// header wordmark, warm ground, the message in the display type, and the three
// paths that actually recover the visit.
//
// It stays DELIBERATELY static. The deploy verifier byte-compares the live 404
// response against this file (with cfemail normalization), so the page carries no
// script at all and exactly one subresource - the site's own stylesheet. The brand
// mark is set in type rather than as an <img> so the document makes no image
// request either. Without site.js nothing here depends on JS: the nav has no menu
// toggle to be stuck closed, and no element is hidden waiting for a reveal class.
function render404Page() {
  const path404 = (kicker, title, body, href, action) =>
    `<a class="cs-card cs-404-path" href="${href}"><p class="gcs-kicker">${kicker}</p><h3>${title}</h3><p>${body}</p><span class="cs-404-go">${action} →</span></a>`;
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex,nofollow"><title>Page not found · Clinic Scout</title><link rel="preload" href="/assets/fonts/manrope-latin-var.woff2" as="font" type="font/woff2" crossorigin><link rel="stylesheet" href="/assets/site.css?v=${ASSET_VERSION}"></head><body class="cs-404-body"><div class="cs-topstrip" aria-hidden="true"></div><nav class="gcs-nav" aria-label="Primary"><div class="gcs-nav-inner"><a class="gcs-brand" href="/"><span>Clinic Scout</span></a><div class="gcs-links"><a href="/providers/">Compare</a><a href="/tools/glp-1-cost-calculator/">Calculator</a><a href="/insights/">Research library</a></div><a class="gcs-nav-action" href="/quiz/provider-fit/">Find your fit</a></div></nav><main data-pages-not-found="true"><section class="cs-404-hero"><p class="gcs-kicker">Error 404</p><h1>That page isn't <span class="cs-emph">here</span>.</h1><p class="lede">The address is either outdated or mistyped. Nothing has been taken down — every comparison, price, and guide is still where it was. Pick one of these three and you will land where you meant to.</p></section><hr class="cs-tick" aria-hidden="true"><section aria-labelledby="cs-404-paths"><h2 id="cs-404-paths">Three ways back in</h2><div class="cs-cardgrid cs-404-grid">${[
    path404("Compare", "Compare all programs", "Every online GLP-1 provider we track, side by side — with the date we checked on every price.", "/providers/", "Open the comparison"),
    path404("Quiz", "Find your fit", "Seven questions, answered on your device, and an ungated shortlist at the end.", "/quiz/provider-fit/", "Start the quiz"),
    path404("Research", "Research library", "Source-bound guides on cost, coverage, verification, and cancellation terms.", "/guides/", "Browse the guides"),
  ].join("")}</div></section></main><footer class="gcs-footer cs-404-footer"><p class="gcs-footer-wordmark">Clinic Scout</p><p class="cs-footer-strength">Every price on this site shows the date we verified it and links to its source.</p><ul class="cs-404-footerlinks"><li><a href="/">Home</a></li><li><a href="/how-we-rate/">How we rate</a></li><li><a href="/how-we-make-money/">How we make money</a></li><li><a href="/privacy/">Privacy</a></li><li><a href="/terms/">Terms</a></li></ul><p class="gcs-entity">Entity: Tilton Group LLC · California entity B20260317552 · Principal and mailing address: 2108 N St, Ste N, Sacramento, CA 95816.</p></footer></body></html>`;
}

function renderDirectoryLanding(npi) {
  const cities = npi.cities.map((city) => {
    const slug = city.city_slug || `${city.city.toLowerCase().replaceAll(/[^a-z0-9]+/g, "-")}-${city.state.toLowerCase()}`;
    const direct = (city.records || []).filter((record) => record.glp_service_verified === true).length;
    return `<a class="clinic-card evidence-line" href="/clinics/${escHtml(slug)}/" data-city-card data-city-name="${escHtml(`${city.city} ${city.state}`.toLowerCase())}" data-city-tier="${direct ? "direct" : "registry"}"><h3>${escHtml(city.city)}, ${escHtml(city.state)}</h3><p class="clinic-card__tier">${direct ? `${direct} direct service source${direct === 1 ? "" : "s"}` : "Registry candidates only"}</p><p>${escHtml((city.records || []).length)} source-dated NPI-2 records</p></a>`;
  }).join("");
  return framePage("National clinic evidence directory · Clinic Scout", `<section class="task-hero hero" data-template="directory"><div><h1>Find local care evidence—not just a pin on a map.</h1><p class="gcs-kicker">National clinic evidence directory</p><p>Browse registry identity records and separately labeled direct organization-and-city service sources. Nothing here implies availability, quality, or a treatment recommendation.</p><label for="directory-search">Search by city or state</label><input id="directory-search" data-city-search type="search" autocomplete="postal-code" placeholder="City or state"></div><picture><source srcset="/assets/images/directory-hero-800.avif 800w, /assets/images/directory-hero-1600.avif 1600w" sizes="(max-width:768px) calc(100vw - 32px), 540px" type="image/avif"><source srcset="/assets/images/directory-hero-800.webp 800w, /assets/images/directory-hero-1600.webp 1600w" sizes="(max-width:768px) calc(100vw - 32px), 540px" type="image/webp"><img src="/assets/images/directory-hero-1600.jpg" width="1600" height="900" alt="Abstract directory concept with fictional map layers; not real clinic locations" fetchpriority="high"></picture></section><section class="notice evidence-line"><h2>Read the evidence label first</h2><p>Registry identity is not proof of current GLP-1 service. Direct service sources are labeled separately and dated.</p></section><section aria-labelledby="city-results-title"><h2 id="city-results-title">Browse ${npi.cities.length} city evidence pages</h2><div class="directory-filters" aria-label="City evidence filters"><button type="button" data-city-tier-filter="all" aria-pressed="true">All</button><button type="button" data-city-tier-filter="direct" aria-pressed="false">Direct service source</button><button type="button" data-city-tier-filter="registry" aria-pressed="false">Registry candidates</button></div><p class="results-count" data-city-count>${npi.cities.length} cities</p><div class="directory-results" data-city-results>${cities}</div></section>`);
}

function articleVertical(article) {
  const route = String(article.canonical_path || "");
  if (/(erectile|viagra|cialis|sildenafil|tadalafil|online-ed)/.test(route)) return "ED";
  if (/(peptide|bpc-157|cjc-1295|ipamorelin)/.test(route)) return "Peptides";
  if (/(hair-loss|minoxidil|finasteride)/.test(route)) return "Hair loss";
  if (/(trt|testosterone)/.test(route)) return "TRT";
  if (/(metabolic-health|metabolic-syndrome|insulin-resistance|continuous-glucose)/.test(route)) return "Wellness";
  return "GLP-1";
}

function renderGuideLibrary(articles) {
  const guideTopic = (article) => /cost|price|savings|assistance/i.test(article.primary_keyword || article.h1) ? "Cost and access" : /counterfeit|safety|verify|pharmacy|warning/i.test(article.primary_keyword || article.h1) ? "Safety and verification" : /coverage|insurance|appeal|medicare/i.test(article.primary_keyword || article.h1) ? "Coverage and appeals" : /\bvs\b|compare/i.test(article.primary_keyword || article.h1) ? "Comparison guide" : "GLP-1 research";
  const guideCard = (article, featured = false) => `<article class="panel evidence-line${featured ? " guide-card--featured" : ""}"><p class="gcs-kicker">${escHtml(guideTopic(article))}</p><h3><a href="${escHtml(article.canonical_path)}">${escHtml(article.h1)}</a></h3><p>${escHtml(article.description || article.direct_answer)}</p><p><small>${escHtml((article.sources || []).length)} cited sources · checked ${escHtml(article.source_checked_through)}</small></p></article>`;
  const order = ["GLP-1", "ED", "Peptides", "Hair loss", "TRT", "Wellness"].filter((vertical) => articles.some((article) => articleVertical(article) === vertical));
  const sections = order.map((vertical) => {
    const items = articles.filter((article) => articleVertical(article) === vertical);
    const id = `guides-${vertical.toLowerCase().replaceAll(/[^a-z0-9]+/g, "-")}`;
    return `<section aria-labelledby="${id}"><div class="gcs-section-head"><h2 id="${id}">${escHtml(vertical)}</h2><p>${items.length} evidence guide${items.length === 1 ? "" : "s"} with source dates and stated limitations.</p></div><div class="grid">${items.map((article) => guideCard(article)).join("")}</div></section>`;
  }).join("");
  const index = order.map((vertical) => {
    const id = `guides-${vertical.toLowerCase().replaceAll(/[^a-z0-9]+/g, "-")}`;
    const count = articles.filter((article) => articleVertical(article) === vertical).length;
    return `<a href="#${id}">${escHtml(vertical)} · ${count}</a>`;
  }).join("");
  const featured = articles.slice(0, 3).map((article) => guideCard(article, true)).join("");
  const groups = `<section class="featured-guides" aria-labelledby="featured-guides"><div class="gcs-section-head"><h2 id="featured-guides">Start here</h2><p>Three useful entry points for cost, verification, and comparison research.</p></div><div class="grid">${featured}</div></section><nav class="library-index" aria-label="Research library sections">${index}</nav>${sections}`;
  return framePage("Research library · Clinic Scout", `<section class="gcs-home-hero"><div><p class="gcs-kicker">Clinic Scout research library</p><h1>Research the claim before the checkout.</h1><p class="lede">Browse ${articles.length} source-bound GLP-1 guides. Each page states what its sources establish—and what they do not.</p></div><picture><source srcset="/assets/images/editorial-evidence-hero-800.avif 800w, /assets/images/editorial-evidence-hero-1600.avif 1600w" sizes="(max-width:900px) calc(100vw - 32px), 540px" type="image/avif"><source srcset="/assets/images/editorial-evidence-hero-800.webp 800w, /assets/images/editorial-evidence-hero-1600.webp 1600w" sizes="(max-width:900px) calc(100vw - 32px), 540px" type="image/webp"><img src="/assets/images/editorial-evidence-hero-1600.jpg" width="1600" height="900" alt="Editorial evidence papers and a magnifying glass" fetchpriority="high"></picture></section><aside class="notice evidence-line"><h2>Research guidance, not medical advice</h2><p>These guides do not diagnose, prescribe, rank treatment outcomes, or imply clinician review. Verify current treatment decisions with a licensed clinician.</p></aside>${groups}`).replace("</head>", `<meta name="description" content="Source-checked GLP-1 cost, coverage, safety, and provider comparison guides."><link rel="canonical" href="https://glpclinicscout.com/guides/"></head>`);
}

// PARITY REDESIGN 2026-08-20 — the homepage rebuilt to glpwinner's section order.
//
// The operator's verdict on the previous homepage was "super bland and ai made",
// with an explicit instruction to emulate glpwinner's layout and premium feel.
// The section order below is theirs, read off `competitors/screens/
// glpwinner-homepage-desktop.png` top to bottom (the full map is in
// reports/glp-clinic-scout/rebuild/PARITY-REDESIGN-REPORT-20260820.md §1):
//
//   hero on an aurora wash + chips + one animated CTA
//   -> trust bar  -> medication rail -> cash-pay/insurance duo
//   -> feature trio on a glow panel -> screen-out grid -> verdict/receipt wall
//   -> positioning pair -> article trio -> footer
//
// Three of their sections could not be copied honestly and were REPLACED, not
// faked (each is annotated at its call site below):
//   * their press-logo bar     -> our four checkable strengths
//   * their user-review wall   -> our editorial verdicts + price-check receipts
//   * their FDA-halo drug cards-> approval status stated per medication, including
//                                 "not FDA-approved" for compounded formulations
//
// Pass 5 operator map: medication entry points must preserve intent all the
// way into the destination page. Keep this one source of truth for home chips,
// rails, price-card CTAs, and shared navigation so none can collapse back to
// the generic /providers/ listing.
const PASS5_MEDICATION_DESTINATIONS = Object.freeze({
  allProviders: "/providers/?sort=cheapest",
  compoundedSemaglutide: "/lp/best-providers/compounded-semaglutide/?drug=SEMAGLUTIDE&form=COMPOUNDED&sort=cheapest",
  compoundedTirzepatide: "/lp/best-providers/compounded-tirzepatide/?drug=TIRZEPATIDE&form=COMPOUNDED&sort=cheapest",
  wegovyPill: "/wegovy-pill/?drug=WEGOVY_PILL&form=COMPOUNDED&sort=cheapest",
  ozempic: "/ozempic/?drug=OZEMPIC&form=BRAND_NAME&sort=cheapest",
  wegovy: "/wegovy/?drug=WEGOVY&form=BRAND_NAME&sort=cheapest",
  mounjaro: "/mounjaro/?drug=MOUNJARO&form=BRAND_NAME&sort=cheapest",
  zepbound: "/zepbound/?drug=ZEPBOUND&form=BRAND_NAME&sort=cheapest",
});

// Every number rendered here is still passed in derived from the build's own
// pricing capture — nothing on this page is a figure somebody typed.
function renderHomePage({ blogCount, comparisonCount, programCount, updatedLabel, topPicks = [], cheapest = null, railMeds = [], verdictCards = [], receiptCards = [], readerReviews = [], reviewPrograms = [], reviewPharmacies = [] }) {
  const money = (value) => Number.isFinite(Number(value)) ? `$${Number(value).toLocaleString("en-US")}` : "See options";
  const picture = (stem, alt, cssClass = "", widths = [640, 1024], loading = "lazy", intrinsicHeight = null) => {
    const [small, large] = widths;
    const srcset = (ext) => `/assets/images/${stem}-${small}.${ext} ${small}w, /assets/images/${stem}-${large}.${ext} ${large}w`;
    return `<picture class="${cssClass}"><source srcset="${srcset("avif")}" sizes="(max-width: 700px) 86vw, 42vw" type="image/avif"><source srcset="${srcset("webp")}" sizes="(max-width: 700px) 86vw, 42vw" type="image/webp"><img src="/assets/images/${stem}-${large}.jpg" srcset="${srcset("jpg")}" sizes="(max-width: 700px) 86vw, 42vw" width="${large}" height="${intrinsicHeight || large}" loading="${loading}" decoding="async" alt="${escHtml(alt)}"></picture>`;
  };
  const guidePicture = (stem, alt, cssClass = "") => `<picture class="${cssClass}"><source srcset="/assets/images/${stem}-800.avif 800w, /assets/images/${stem}-1600.avif 1600w" sizes="(max-width: 700px) 92vw, 32vw" type="image/avif"><source srcset="/assets/images/${stem}-800.webp 800w, /assets/images/${stem}-1600.webp 1600w" sizes="(max-width: 700px) 92vw, 32vw" type="image/webp"><img src="/assets/images/${stem}-1600.jpg" width="1600" height="900" loading="lazy" decoding="async" alt="${escHtml(alt)}"></picture>`;
  const medPicture = (stem, alt, widths = [640, 1280]) => {
    const [small, large] = widths;
    return `<picture class="cs-home-med__art"><source srcset="/assets/images/${stem}-${small}.avif ${small}w, /assets/images/${stem}-${large}.avif ${large}w" sizes="(max-width: 700px) 76vw, 420px" type="image/avif"><source srcset="/assets/images/${stem}-${small}.webp ${small}w, /assets/images/${stem}-${large}.webp ${large}w" sizes="(max-width: 700px) 76vw, 420px" type="image/webp"><img src="/assets/images/${stem}-${large}.jpg" width="${large}" height="${large}" loading="lazy" decoding="async" alt="${escHtml(alt)}"></picture>`;
  };
  const featurePicture = (stem, alt) => `<picture class="cs-home2-carecard__art"><source srcset="/assets/images/${stem}-720.avif 720w, /assets/images/${stem}-1440.avif 1440w" sizes="(max-width: 700px) 92vw, 31vw" type="image/avif"><source srcset="/assets/images/${stem}-720.webp 720w, /assets/images/${stem}-1440.webp 1440w" sizes="(max-width: 700px) 92vw, 31vw" type="image/webp"><img src="/assets/images/${stem}-1440.jpg" width="1440" height="960" loading="lazy" decoding="async" alt="${escHtml(alt)}"></picture>`;
  const providerMark = (card) => {
    if (!card.logo) return renderWordmark(card, "card");
    const logo = card.logo;
    const chip = logo.chip_class || (logo.treatment === "dark-chip" ? "dark" : "light");
    const scale = Number(logo.content_scale) > 1 ? ` style="--logo-content-scale:${Number(logo.content_scale).toFixed(3)}"` : "";
    return `<span class="provider-logo provider-logo--card provider-logo--${escHtml(chip)}"${scale}><img src="${escHtml(logo.path)}" alt="${escHtml(logo.alt)}" width="${Number(logo.width)}" height="${Number(logo.height)}" loading="lazy" decoding="async"></span>`;
  };

  // Design pass 2026-08-26 (home spec fix #7): all SEVEN medication shortcuts —
  // Wegovy included — as pill-scale chips (their 4+3 wrap; pill styling in
  // 10-home.css). Routes stay our owned medication routes.
  const quickLinks = [
    ["Compounded semaglutide", PASS5_MEDICATION_DESTINATIONS.compoundedSemaglutide],
    ["Compounded tirzepatide", PASS5_MEDICATION_DESTINATIONS.compoundedTirzepatide],
    ["Ozempic", PASS5_MEDICATION_DESTINATIONS.ozempic],
    ["Wegovy pill", PASS5_MEDICATION_DESTINATIONS.wegovyPill],
    ["Wegovy", PASS5_MEDICATION_DESTINATIONS.wegovy],
    ["Mounjaro", PASS5_MEDICATION_DESTINATIONS.mounjaro],
    ["Zepbound", PASS5_MEDICATION_DESTINATIONS.zepbound],
  ].map(([label, href]) => `<li><a class="cs-home-quick" href="${escHtml(href)}">${escHtml(label)}</a></li>`).join("");

  const hero = `<section class="cs-home2-hero" aria-labelledby="home-title" data-motion-loop>
    <div class="cs-home2-flow" aria-hidden="true"><span></span><span></span><span></span></div>
    <div class="cs-home2-hero__copy" data-motion="rise" data-motion-once="true">
      <p class="gcs-kicker">Independent GLP-1 comparison</p>
      <h1 id="home-title" data-split="lines">Find a GLP-1 plan that fits.</h1>
      <p class="cs-home2-cycle" aria-live="polite"><span data-home-value>Compare real monthly costs.</span></p>
      <ul class="cs-home2-quick" aria-label="Explore popular medications">${quickLinks}</ul>
      <div class="gcs-home-actions"><a class="cs-btn cs-btn--animated cs-palette-action cs-btn--orbit" href="/quiz/provider-fit/">See your matches<span class="cs-btn__arrow" aria-hidden="true">→</span><svg class="cs-btn__orbit" viewBox="0 0 220 60" preserveAspectRatio="none" aria-hidden="true"><rect class="cs-btn__orbit-track" x="2" y="2" width="216" height="56" rx="28" pathLength="100"/></svg></a><a class="cs-btn cs-btn--quiet" href="${PASS5_MEDICATION_DESTINATIONS.allProviders}">Compare all ${programCount} programs</a></div>
    </div>
    <div class="cs-home2-cluster" aria-label="Original unbranded wellness product photography" data-motion="scale" data-motion-once="true">
      ${picture("home-hero-fruit", "An unbranded amber bottle beside blood oranges under teal light", "cs-home2-cluster__main", [640, 1024], "eager")}
      ${picture("home-hero-fridge", "An unbranded amber vial stored inside a clean refrigerator", "cs-home2-cluster__side", [640, 1024], "eager")}
      ${picture("home-hero-hand", "A hand holding an unbranded amber bottle", "cs-home2-cluster__hand", [640, 1024], "eager")}
    </div>
  </section>`;

  const marqueeItems = verdictCards.slice(0, 6).map((card) => `<a href="${card.href}"><span>${escHtml(card.provider)}</span><small>Read our review</small></a>`).join("");
  const marquee = `<section class="cs-home2-marquee" aria-label="Programs compared by Clinic Scout" data-motion-loop data-motion="fade" data-motion-once="true"><p>Popular program reviews</p><div class="cs-home2-marquee__window"><div class="cs-home2-marquee__track">${marqueeItems}${marqueeItems}</div></div></section>`;

  const trust = `<section class="cs-home2-trust" data-motion="fade" data-motion-once="true"><dl class="cs-sr-only"><dt>Providers compared</dt><dd>${programCount}</dd></dl><div><strong data-counter data-counter-to="${programCount}">${programCount}</strong><span>programs tracked</span></div><div><strong>0</strong><span>paid placements</span></div><p>Nobody can buy a rank or a score.</p></section>`;

  // Design pass 2026-08-26 (home spec fix #1): one-viewport overlay pattern.
  // Text sits ON the art — title top-left over a dark scrim (AA on the pixels
  // underneath), honest qualifier bottom-left, price/CTA pill bottom-right —
  // and the WHOLE card is one link (the pill is visual affordance only), so the
  // band fits a laptop viewport with its heading. Prices remain derived from
  // our own pricing capture; a route without a captured price says so instead
  // of inventing one.
  const medCards = railMeds.map((med, index) => `<a class="cs-home-med" data-med-card data-index="${index}" href="${med.href}" aria-label="${escHtml(`${med.name} — ${med.status}`)}">
    ${medPicture(med.art, med.alt, med.widths)}
    <span class="cs-home-med__scrim" aria-hidden="true"></span><span class="cs-home-med__veil" aria-hidden="true"></span>
    <span class="cs-home-med__overlay"><h3>${escHtml(med.name)}</h3><span class="cs-home-med__foot"><em class="cs-home-med__qualifier">${index < 2 ? "Compounded option" : "Brand option"}</em><span class="cs-home-med__pill">${Number.isFinite(med.price) ? `Starts at ${money(med.price)}/mo` : "Find a provider"}</span></span></span>
  </a>`).join("");
  const medicationRail = `<section class="cs-home2-section cs-home2-meds" aria-labelledby="meds-title" data-motion="rise" data-motion-once="true">
    <div class="cs-home2-heading"><div><p class="gcs-kicker">Popular options</p><h2 id="meds-title">Start with the medication.</h2><p>See provider options, pricing paths, and the tradeoffs that matter.</p></div><div class="cs-home2-controls"><button type="button" data-med-prev aria-label="Previous medication">←</button><button type="button" data-med-next aria-label="Next medication">→</button><button type="button" class="cs-home2-med-toggle" data-med-toggle hidden aria-pressed="false" aria-label="Pause the medication carousel"><span class="cs-home2-med-toggle__icon" aria-hidden="true"></span><span data-med-toggle-label>Pause</span></button></div></div>
    <div class="cs-home2-med-viewport" data-med-viewport><div class="cs-home2-med-track" data-med-track>${medCards}</div></div>
    <div class="cs-home2-dots" data-med-dots aria-label="Medication slides"></div><p class="cs-home2-product-disclosure">Representative editorial renders, not retail-packaging photos; appearance may vary.</p>
  </section>`;

  const lifestyle = `<section class="cs-home2-lifestyle" data-motion-group="rise" data-motion-stagger="0.1" data-motion-once="true">
    <div class="cs-home2-lifestyle__media">${picture("home-lifestyle-phone", "A person using a phone at home", "", [768, 1536], "lazy", 1024)}</div>
    <div class="cs-home2-lifestyle__copy"><p class="gcs-kicker">Cash pay or insurance</p><h2>Choose the path that works for you.</h2><p>Compare self-pay programs, insurance support, and what is included before you commit.</p><a class="cs-btn cs-palette-action" href="${PASS5_MEDICATION_DESTINATIONS.allProviders}">Compare both paths</a></div>
  </section>`;

  const compoundedOptions = railMeds.filter((med) => med.name.startsWith("Compounded")).map((med) => `<button type="button" data-price-choice data-price-label="${escHtml(med.name.replace("Compounded ", ""))}" data-price-value="${Number.isFinite(med.price) ? med.price : ""}" data-price-href="${escHtml(med.href)}" aria-pressed="false">${escHtml(med.name.replace("Compounded ", ""))}</button>`).join("");
  const brandOptions = railMeds.filter((med) => ["Ozempic", "Wegovy pill", "Mounjaro", "Wegovy", "Zepbound"].includes(med.name)).map((med) => `<button type="button" data-price-choice data-price-label="${escHtml(med.name)}" data-price-value="${Number.isFinite(med.price) ? med.price : ""}" data-price-href="${escHtml(med.href)}" aria-pressed="false">${escHtml(med.name)}</button>`).join("");
  const firstCompound = railMeds.find((med) => med.name.startsWith("Compounded") && Number.isFinite(med.price));
  const firstBrand = railMeds.find((med) => ["Ozempic", "Wegovy", "Mounjaro", "Zepbound"].includes(med.name) && Number.isFinite(med.price));
  const priceCard = (kind, routeLabel, title, intro, options, first) => {
    const tooltipId = `cs-price-tooltip-${kind.toLowerCase()}`;
    return `<article class="cs-home2-price" data-price-card>
    <p class="cs-home2-price__route cs-grad-words">${routeLabel}</p><h3>${title}</h3><p>${intro}</p>
    <div class="cs-home2-price__choices" role="group" aria-label="Choose ${kind.toLowerCase()} medication">${options}</div>
    <div class="cs-home2-price__number"><span data-price-roll>${first ? money(first.price) : "See options"}</span><small>${first ? "/month starting price" : "published options"}</small><button class="cs-home2-info" type="button" aria-label="About these prices" aria-controls="${tooltipId}" aria-expanded="false" data-price-info>?</button><span class="cs-home2-info__tip" id="${tooltipId}" role="tooltip" hidden>Starting prices use a dated provider-published option or an official manufacturer self-pay offer. Eligibility, dose, and current terms can change the price.</span></div>
    <a class="cs-btn cs-palette-action" data-price-destination href="${escHtml(first?.href || "/providers/")}">Compare providers</a>
  </article>`;
  };
  // Design pass 2026-08-26 (home spec fix #11): the standalone calculator band
  // is folded into the pricing band as a slim follow-on row — same content,
  // same destination, one less full-height section in the arc.
  const pricing = `<section class="cs-home2-section cs-home2-pricing" aria-labelledby="pricing-title"><div class="cs-home2-heading" data-motion="rise" data-motion-once="true"><div><p class="gcs-kicker">Pricing</p><h2 id="pricing-title">See what your route may cost.</h2></div></div><div class="cs-home2-pricegrid" data-motion-group="rise" data-motion-stagger="0.12" data-motion-once="true">${priceCard("Compounded", "Compounded medication", "Lower starting prices", "Compare provider-published options for compounded medication.", compoundedOptions, firstCompound)}${priceCard("Brand", "Brand name medication", "Insurance can change the math", "Compare popular brands and the providers that support them.", brandOptions, firstBrand)}</div><aside class="cs-home2-pricing__calc" data-motion="fade" data-motion-once="true"><div><p class="gcs-kicker">Cost calculator</p><h3>Know your monthly cost before checkout.</h3><p>Add membership fees, medication, labs, and extras in one place.</p></div><a class="cs-btn cs-palette-action" href="/tools/glp-1-cost-calculator/" aria-label="Calculate your cost">Open the free cost calculator</a></aside></section>`;

  const care = `<section class="cs-home2-section cs-home2-care" aria-labelledby="care-title"><div class="cs-home2-heading" data-motion="rise" data-motion-once="true"><div><p class="gcs-kicker">Care you can trust</p><h2 id="care-title">Know who is behind your care.</h2></div></div><div class="cs-home2-caregrid" data-motion-group="rise" data-motion-stagger="0.1" data-motion-once="true">
    <a class="cs-home2-carecard" href="${PASS5_MEDICATION_DESTINATIONS.allProviders}">${featurePicture("feat-telehealth", "A calm telehealth setting")}<div><h3>Trusted providers</h3><p>Compare programs that use licensed clinicians and named pharmacy partners.</p><span>Compare providers →</span></div></a>
    <a class="cs-home2-carecard" href="/guides/verify-online-glp-1-pharmacy/">${featurePicture("feat-pharmacy", "A clean pharmacy work surface")}<div><h3>Transparent sourcing</h3><p>See the pharmacies programs name and learn how to verify a license.</p><span>Check a pharmacy →</span></div></a>
    <button class="cs-home2-carecard" type="button" data-review-open>${featurePicture("feat-receipts", "A review and research workspace")}<div><h3>Real patient reviews</h3><p>Share a real experience. We review every submission before anything appears publicly.</p><span>Share your experience →</span></div></button>
  </div></section>`;

  const risks = [
    ["No named pharmacy", "You cannot verify where the medication comes from.", "Look for a named, license-checkable pharmacy."],
    ["Research-use products", "Research peptides are not a patient treatment path.", "Choose care prescribed by a licensed clinician."],
    ["No clinician review", "A checkout form is not a medical evaluation.", "Use a program with a real clinical intake."],
    ["Hidden total cost", "Low teaser prices can leave out required fees.", "Compare the full monthly cost before paying."],
    ["Unclear cancellation", "Vague renewal terms make it harder to leave.", "Read the cancellation policy before subscribing."],
    ["Counterfeit signals", "Unverified packaging and sellers add avoidable risk.", "Use an authorized, traceable care pathway."],
  ];
  const riskIcons = [
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 21V8l8-5 8 5v13M8 21v-6h8v6M9 10h6M12 7v6"/></svg>',
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3M8 15h8"/></svg>',
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 3v6a6 6 0 0 0 12 0V3M6 5H4M18 5h2M12 15v2a4 4 0 0 0 4 4h1M17 18a2 2 0 1 0 0 4"/></svg>',
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3h10v18l-2-1.5L12 21l-3-1.5L7 21V3ZM10 8h4M10 12h4M10 16h3"/></svg>',
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 7h10v14H7zM9 3h6l2 4H7l2-4ZM9 11h6M9 15h4"/></svg>',
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 8 4v5c0 5-3.4 8-8 9-4.6-1-8-4-8-9V7l8-4ZM9 9l6 6M15 9l-6 6"/></svg>',
  ];
  const riskCards = risks.map(([title, danger, trust], index) => `<button type="button" class="cs-riskcard" data-risk-card aria-pressed="false" aria-label="${escHtml(title)}. ${escHtml(danger)} ${escHtml(trust)}"><span class="cs-riskcard__front"><span class="cs-riskcard__icon">${riskIcons[index]}</span><strong>${title}</strong></span><span class="cs-riskcard__back"><span>✕ ${danger}</span><span>✓ ${trust}</span></span></button>`).join("");
  const riskSection = `<section class="cs-home2-section cs-home2-risks" aria-labelledby="risks-title"><div class="cs-home2-heading" data-motion="rise" data-motion-once="true"><div><p class="gcs-kicker">Avoid sketchy providers</p><h2 id="risks-title">Six checks before you pay.</h2><p>Simple warning signs. Clear next steps.</p></div></div><div class="cs-riskgrid" data-motion-group="rise" data-motion-stagger="0.08" data-motion-once="true">${riskCards}</div></section>`;

  // Design pass 2026-08-26 (home spec fix #4): their real geometry — panel 1
  // TEXT LEFT / IMAGE RIGHT, panel 2 mirrored (their site is text-first; the
  // operator's from-memory mirror order is corrected per the logged decision
  // note). DOM keeps art first, so panel 1 carries --reverse (art order:2 →
  // right half) and motion.js v2's H17 contract (artFrom = reverse ? 84 : -84)
  // still slides each half in from its own outer edge. Centered gradient
  // divider bars sit between the panels on the bare page ground; the heading
  // underline bar and image glow ring live in 10-home.css.
  const logistics = `<section class="cs-home2-section cs-home2-logistics" aria-label="Care logistics"><div class="cs-home2-logistics__divider" aria-hidden="true"></div><article class="cs-home2-logistics__card cs-home2-logistics__card--reverse" data-h17-card="shipping" data-motion="rise" data-motion-once="true">${guidePicture("pass3-h17-shipping", "A temperature-controlled delivery kit arranged on a dark wood surface", "cs-home2-logistics__art")}<div class="cs-home2-logistics__copy"><p class="gcs-kicker">Delivery and support</p><h2>Responsive care, fast shipping.</h2><p>Compare published support channels, review timing, and shipping terms before you enroll.</p><a class="cs-btn cs-palette-action" href="${PASS5_MEDICATION_DESTINATIONS.allProviders}">Find a provider</a></div></article><div class="cs-home2-logistics__divider" aria-hidden="true"></div><article class="cs-home2-logistics__card" data-h17-card="schedule" data-motion="rise" data-motion-once="true">${guidePicture("pass3-h17-schedule", "A telehealth workspace with a tablet, clock, notebook, and plant", "cs-home2-logistics__art")}<div class="cs-home2-logistics__copy"><p class="gcs-kicker">Care on your schedule</p><h2>Care that works around your schedule.</h2><p>Compare how programs handle clinician review, follow-up, refills, and ongoing questions.</p><a class="cs-btn cs-palette-action" href="${PASS5_MEDICATION_DESTINATIONS.allProviders}">Find a provider</a></div></article></section>`;

  const verdictItems = verdictCards.map((card) => `<a class="cs-home2-verdict" href="${card.href}">${providerMark(card)}<span>${escHtml(card.score)}</span><h3>${escHtml(card.provider)}</h3><p>${escHtml(card.verdict)}</p><small>Read the verdict →</small></a>`).join("");
  const verdicts = `<section class="cs-home2-section cs-home2-verdicts" aria-labelledby="verdict-title" data-motion-loop><div class="cs-home2-heading" data-motion="rise" data-motion-once="true"><div><p class="gcs-kicker">Our verdicts</p><h2 id="verdict-title">The short answer on every program.</h2></div></div><div class="cs-home2-verdict-window"><div class="cs-home2-verdict-track">${verdictItems}${verdictItems}</div></div><p class="cs-home2-centered"><a class="cs-btn cs-palette-action" href="/reviews/">Read every verdict</a></p></section>`;

  const picks = topPicks.map((pick, index) => `<a class="cs-home2-pick" href="${pick.reviewPath}">${providerMark(pick)}<small>0${index + 1}</small><h3>${escHtml(pick.provider)}</h3><p>${Number.isFinite(pick.price) ? `${money(pick.price)}/month starting price` : "See current options"}</p><span>Read our review →</span></a>`).join("");
  const strongest = `<section class="cs-home2-section cs-home2-strongest" aria-labelledby="strongest-title"><div class="cs-home2-heading" data-motion="rise" data-motion-once="true"><div><p class="gcs-kicker">Strongest right now</p><h2 id="strongest-title">Three programs worth a closer look.</h2></div></div><div class="cs-home2-pickgrid" data-motion-group="rise" data-motion-stagger="0.1" data-motion-once="true">${picks}</div><p class="cs-home2-centered"><a class="cs-btn cs-palette-action" href="/providers/">See all ${programCount} programs</a></p></section>`;

  // Design pass 2026-08-26 (home spec fix #8): the review-wall module, fed ONLY
  // by approved first-party reader reviews (the same held-then-approved
  // moderation pipeline the /reviews/ hub reports — today that approved count
  // is 0, exactly as /reviews/ states). Zero approved records renders the
  // honest empty state: invitation + Browse Reviews + Share, never placeholder
  // cards. With approved records the band becomes three CSS-keyframe marquee
  // columns — duplicated content per column for the seamless loop (the
  // duplicate set is aria-hidden and unlinked), per-column speeds, edge fade
  // masks, hover pause, reduced-motion static (all in 10-home.css).
  const wallStars = (rating) => { const filled = Math.max(0, Math.min(5, Math.round(Number(rating) || 0))); return `<span class="cs-home2-wallstars" role="img" aria-label="${filled} out of 5 stars"><span aria-hidden="true">${"★".repeat(filled)}${"☆".repeat(5 - filled)}</span></span>`; };
  const wallCard = (review, linked) => `<article class="cs-home2-wallreview"><header>${linked ? `<a href="${escHtml(review.entity_path)}">${escHtml(review.entity)}</a>` : `<b>${escHtml(review.entity)}</b>`}<span>${escHtml(review.entity_type || "provider")}</span><time>${escHtml(review.approved_on || "")}</time></header>${wallStars(review.rating)}<p>${escHtml(review.body)}</p><footer><span>${escHtml(review.reviewer)}</span><span class="cs-home2-wallchip">✓ Verified email</span></footer></article>`;
  const wallColumns = readerReviews.length ? [0, 1, 2].map((column) => {
    const set = (linked) => readerReviews.filter((_, index) => index % 3 === column).map((review) => wallCard(review, linked)).join("");
    return `<div class="cs-home2-wall__col" style="--wall-col:${column}"><div class="cs-home2-wall__set">${set(true)}</div><div class="cs-home2-wall__set" aria-hidden="true">${set(false)}</div></div>`;
  }).join("") : "";
  const reviewWall = `<section class="cs-home2-section cs-home2-wall" aria-labelledby="review-wall-title"><div class="cs-home2-wall__intro" data-motion="rise" data-motion-once="true"><p class="gcs-kicker">Built from real experiences</p><h2 id="review-wall-title">Used a GLP-1 provider?</h2><p>Share what happened. Providers cannot pay to alter or remove reviews. We review every submission and contact you to verify it before publication.</p><div class="cs-home2-wall__actions"><button class="cs-btn cs-palette-action" type="button" data-review-open>Share your experience</button><a class="cs-btn" href="/reviews/">Browse reviews</a></div></div>${readerReviews.length ? `<div class="cs-home2-wall__cols">${wallColumns}</div>` : `<div class="cs-home2-review-steps" data-motion-group="rise" data-motion-stagger="0.1" data-motion-once="true"><span><b>1</b> You submit</span><span><b>2</b> We check your experience</span><span><b>3</b> Approved reviews appear</span></div><p class="cs-home2-wall__empty">Reader reviews appear here after verification.</p>`}</section>`;

  const positioning = `<section class="cs-home2-position" data-motion="fade" data-motion-once="true" data-motion-loop><div class="cs-home2-position__wash" aria-hidden="true"></div><p class="gcs-kicker">Independent by design</p><h2>We do not sell medication. We help you navigate the choices.</h2><p>Compare programs, understand costs, and know what to verify before you commit.</p><a class="cs-btn" href="/how-we-rate/">See how we compare</a></section>`;

  const editorialCards = [
    ["guide-cheapest-glp1", "/guides/cheapest-glp1/", "Find the lowest total cost", "Compare the price you see first with the cost you may actually pay."],
    ["guide-verify-online-glp-1-pharmacy", "/guides/verify-online-glp-1-pharmacy/", "Check the pharmacy", "A practical license and sourcing checklist before you order."],
    ["guide-glp-1-program-total-cost-comparison", "/guides/glp-1-program-total-cost-comparison/", "Compare every fee", "Medication, membership, labs, shipping, and the costs between."],
    ["guide-wegovy-pills-oral-semaglutide-cost", "/guides/wegovy-pills-oral-semaglutide-cost/", "Understand the Wegovy pill", "What the oral route changes about access and cost."],
    ["guide-glp-1-prior-authorization-appeals", "/guides/glp-1-prior-authorization-appeals/", "Get through prior authorization", "A clearer path when insurance asks for more."],
    ["guide-glp-1-shortage-availability-check", "/guides/glp-1-shortage-availability-check/", "Check current availability", "What to verify before choosing a medication or provider."],
  ];
  // Design pass 2026-08-26 (home spec fixes #5 + #9 + #11): the two editorial
  // bands (guide grid + news trio, which duplicated the first three guides)
  // merge into ONE rounded editorial panel — their S10 geometry — with the
  // newsletter capture pill in the same panel (it opens the existing sitewide
  // subscribe bottom sheet; transport and consent contract untouched) and a
  // single "Browse all guides" read-more. Guide cards flip to light
  // high-contrast cards in 10-home.css.
  const editorials = editorialCards.map(([image, href, title, copy]) => `<a class="cs-home2-article" href="${href}">${guidePicture(image, title)}<div><p class="gcs-kicker">Guide</p><h3>${title}</h3><p>${copy}</p><span>Read the guide →</span></div></a>`).join("");
  const editorial = `<section class="cs-home2-section cs-home2-editorial" aria-labelledby="editorial-title"><div class="cs-home2-editorial__head" data-motion="rise" data-motion-once="true"><p class="gcs-kicker">Clinic Scout guides</p><h2 id="editorial-title">Start with a clearer question.</h2><p>Fresh answers for expensive decisions — and a weekly price watch when you want the numbers to come to you.</p><button class="cs-btn cs-palette-action" type="button" data-newsletter-open>Get the weekly price watch <span aria-hidden="true">✉</span></button></div><div class="cs-home2-articlegrid" data-motion-group="rise" data-motion-stagger="0.08" data-motion-once="true">${editorials}</div><p class="cs-home2-editorial__more"><a class="cs-arrowlink" href="/insights/">Browse all ${blogCount} guides <span class="cs-arrow">→</span></a></p></section>`;

  // Fix #11: the help panel collapses to a pre-footer help line — every
  // destination preserved as an inline link (their S11 pattern).
  const help = `<section class="cs-home2-help" data-motion="rise" data-motion-once="true"><p class="cs-home2-help__line"><strong>Still have questions?</strong> Start with the route that matters most — <a href="/providers/">compare providers</a>, <a href="/tools/glp-1-cost-calculator/">estimate your cost</a>, or <a href="/quiz/provider-fit/">take the fit quiz</a>.</p></section>`;

  const reviewSheetV2 = `<div class="cs-review-sheet" data-review-sheet hidden><style data-preserve data-review-sheet-v2-style>.cs-review-sheet__panel.p5-review-intake{padding:clamp(18px,2.2vw,28px)}.cs-review-sheet__panel .p5-review-intake__card{margin-top:8px}.cs-review-sheet__panel .p5-review-intake__intro{padding-right:48px}.cs-review-sheet__panel .p5-review-form{margin:0}.cs-review-sheet__panel .cs-review-sheet__close{z-index:3}.cs-review-sheet__panel .p5-step{padding:clamp(16px,2.2vw,24px)}@media(max-width:720px){.cs-review-sheet__panel.p5-review-intake{padding:12px 10px calc(16px + env(safe-area-inset-bottom))}.cs-review-sheet__panel .p5-review-intake__intro{padding-right:42px}}</style><button class="cs-review-sheet__backdrop" type="button" data-review-close aria-label="Close review form"></button><section class="cs-review-sheet__panel p5-review-intake cs-intake" role="dialog" aria-modal="true" aria-labelledby="review-sheet-title" data-review-intake-root><button class="cs-review-sheet__close" type="button" data-review-close aria-label="Close">×</button><section class="p5-review-intake__card" aria-labelledby="review-sheet-title"><div class="p5-review-intake__intro"><p class="p3-kicker">Tell us what happened</p><h2 id="review-sheet-title">Share your experience</h2><p>One review, one honest confirmation, then manual verification before anything can be published.</p></div>${renderReviewIntakeForm({ programs: reviewPrograms, pharmacies: reviewPharmacies, compact: true })}</section></section><script type="module" src="/assets/review-intake.js" data-review-intake-runtime></script></div>`;

  // Design pass 2026-08-26 (home spec fix #11): the arc trims from 18 to 15
  // bands — calculator folded into pricing, review capture grown into the
  // review wall (after the proof cluster, before editorial), news merged into
  // the editorial panel, help collapsed to a pre-footer line.
  return framePage("Clinic Scout · Find a GLP-1 plan that fits",
    `${selectedFamilyStyles("/")}${hero}${pricing}${medicationRail}${trust}${marquee}${lifestyle}${care}${riskSection}${logistics}${verdicts}${strongest}${reviewWall}${positioning}${editorial}${help}${reviewSheetV2}`);
}

function renderHomePageLegacy({ blogCount, comparisonCount, cityCount }) {
  return framePage("Clinic Scout · Compare what matters before you commit", `<section class="gcs-home-hero"><div><p class="gcs-kicker">Independent GLP-1 program research</p><h1>Compare what matters before you commit.</h1><p class="lede">See published costs, service details, source gaps, and local clinic evidence—without pretending a comparison can choose treatment for you.</p><div class="gcs-home-actions"><a class="gcs-button" href="/providers/">Compare programs</a><a class="gcs-button secondary" href="/insights/">Find clinics near you</a></div><p><small>Source-dated · Commission-independent rubric · Private fit guide</small></p></div><div><picture><source srcset="/assets/images/editorial-evidence-hero-800.avif 800w, /assets/images/editorial-evidence-hero-1600.avif 1600w" sizes="(max-width:900px) calc(100vw - 32px), 540px" type="image/avif"><source srcset="/assets/images/editorial-evidence-hero-800.webp 800w, /assets/images/editorial-evidence-hero-1600.webp 1600w" sizes="(max-width:900px) calc(100vw - 32px), 540px" type="image/webp"><img src="/assets/images/editorial-evidence-hero-1600.jpg" srcset="/assets/images/editorial-evidence-hero-800.jpg 800w, /assets/images/editorial-evidence-hero-1600.jpg 1600w" sizes="(max-width:900px) calc(100vw - 32px), 540px" width="1600" height="900" fetchpriority="high" alt="Editorial still life of evidence papers and a magnifying glass"></picture><div class="trust-metrics evidence evidence-line"><div><dt>Published cost</dt><dd>Source dated</dd></div><div><dt>Support</dt><dd>Fact or gap</dd></div><div><dt>Source checked</dt><dd>Shown per page</dd></div><div><dt>Comparison basis</dt><dd>Source evidence</dd></div></div></div></section><section aria-labelledby="start-title"><div class="gcs-section-head"><h2 id="start-title">Start with your decision.</h2><p>Each path opens a concrete comparison, local evidence set, or verification workflow.</p></div><div class="grid"><a class="panel evidence-line" href="/providers/"><h3>Compare programs</h3><p>Price, product category, insurance support, and wait.</p></a><a class="panel evidence-line" href="/insights/"><h3>Find clinics</h3><p>Search registry candidates and direct service sources by place.</p></a><a class="panel evidence-line" href="/guides/verify-online-glp-1-pharmacy/"><h3>Check a provider</h3><p>Use a source checklist before you pay.</p></a></div></section><section class="verdict evidence-line"><div class="verdict-grid"><div><h2>Evidence-qualified comparisons, with the gaps left visible.</h2><p>Clinic Scout uses one published commercial rubric across each comparison. Missing price, support, or policy details remain “Not stated” instead of becoming confident estimates.</p><a href="/providers/">See all evidence-qualified options</a></div><dl class="trust-metrics"><div><dt>Comparisons</dt><dd>${comparisonCount}</dd></div><div><dt>Guides</dt><dd>${blogCount}</dd></div><div><dt>City pages</dt><dd>${cityCount}</dd></div><div><dt>Evidence model</dt><dd>Source-dated</dd></div></dl></div></section><section aria-labelledby="method-title"><div class="gcs-section-head"><h2 id="method-title">How the evidence works</h2><p>One visible sequence—from claim to limitation.</p></div><div class="grid"><article class="panel"><h3>1. We capture the published claim.</h3><p>Price, service, policy, registry identity, or location starts with a named source.</p></article><article class="panel"><h3>2. We check the source and date.</h3><p>Every decision fact keeps a verification date and evidence boundary.</p></article><article class="panel"><h3>3. We show the fact, limitation, or gap.</h3><p>Unsupported details stay missing. Registry taxonomy never becomes a service claim.</p></article></div></section><section class="gcs-directory-band"><h2>Look for local care evidence</h2><p>Browse city and state pages built from active NPI-2 location records with direct service sources separated from registry candidates.</p><a class="gcs-button" href="/insights/">Search locations</a><p><small>Registry identity is not proof of current GLP-1 service. Direct service sources are labeled separately.</small></p></section><section aria-labelledby="guides-title"><div class="gcs-section-head"><h2 id="guides-title">Research the parts providers may compress.</h2><p>Cost, access, product verification, and coverage each need a different source trail.</p></div><div class="grid"><a class="panel" href="/guides/zepbound-wegovy-cost-without-insurance/"><h3>Cost without insurance</h3><p>Separate medication, membership, follow-up, and shipping.</p></a><a class="panel" href="/guides/how-to-get-glp-1-prescription-online/"><h3>Online access</h3><p>Understand intake and prescribing boundaries.</p></a><a class="panel" href="/guides/verify-online-glp-1-pharmacy/"><h3>Product verification</h3><p>Check the source before paying.</p></a><a class="panel" href="/guides/glp-1-prior-authorization-appeals/"><h3>Coverage and appeals</h3><p>Map policy steps without implying an outcome.</p></a></div></section><section class="gcs-directory-band"><h2>Compare your priorities privately.</h2><p>Sensitive answers remain in the browser. The guide narrows commercial comparison priorities; it does not diagnose or decide eligibility.</p><a class="gcs-button" href="/quiz/provider-fit/">Start the six-step guide</a><p><small>No diagnosis. No eligibility decision.</small></p></section><section aria-labelledby="trust-close"><h2 id="trust-close">Verify the system, not a badge.</h2><div class="grid"><a class="panel" href="/how-we-rate/"><h3>Methodology</h3><p>Read the commercial evidence rubric.</p></a><a class="panel" href="/authors/clinic-scout-editorial-team/"><h3>Editorial identity</h3><p>See who is accountable and what is not claimed.</p></a><a class="panel" href="/how-we-make-money/"><h3>Money model</h3><p>Understand commission boundaries.</p></a></div></section>`);
}

const GLOBAL_WOW_CSS = String.raw`
:root{--navy:#0c2130;--navy-2:#173847;--ink:#17313d;--soft:#526871;--paper:#f4efe6;--surface:#fffdfa;--white:#fff;--sage:#4e7468;--mint:#dfe9e2;--coral:#b95f49;--sand:#e9dbc8;--gold:#d4ac70;--line:rgba(12,33,48,.14);--shadow:0 24px 70px rgba(16,38,51,.10);--shadow-sm:0 12px 34px rgba(16,38,51,.07);--serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;--sans:Inter,Avenir,"Segoe UI",ui-sans-serif,system-ui,sans-serif}
*{box-sizing:border-box}
html{scroll-behavior:smooth;background:var(--navy);overflow-x:clip}
body{margin:0;background:radial-gradient(circle at 100% 0,rgba(212,172,112,.15),transparent 28rem),linear-gradient(180deg,#faf7f0 0,var(--paper) 100%);color:var(--ink);font:17px/1.68 var(--sans);text-rendering:optimizeLegibility;-webkit-font-smoothing:antialiased;overflow-x:clip}
body>.top{display:none!important}
.gcs-nav{position:sticky;top:0;z-index:80;border-bottom:1px solid rgba(255,255,255,.12);background:rgba(12,33,48,.94);backdrop-filter:blur(18px);color:#fff}
.gcs-nav-inner{width:min(1180px,calc(100% - 32px));min-height:76px;margin:auto;display:flex;align-items:center;justify-content:space-between;gap:28px}
.gcs-brand{display:inline-flex;align-items:center;gap:12px;color:#fff!important;text-decoration:none;font:800 1.04rem/1 var(--sans);letter-spacing:.01em;white-space:nowrap}
.gcs-brand img{width:38px;height:38px;filter:drop-shadow(0 8px 14px rgba(0,0,0,.22))}
.gcs-links{display:flex;align-items:center;gap:clamp(14px,2vw,28px)}
.gcs-links a{display:inline-flex;align-items:center;min-height:44px;color:rgba(255,255,255,.84)!important;text-decoration:none;font-size:.88rem;font-weight:750;letter-spacing:.015em}
.gcs-links a:hover{color:#fff!important}
.gcs-preview{padding:8px 12px;border:1px solid rgba(255,255,255,.18);border-radius:999px;background:rgba(255,255,255,.08);color:#fff;font-size:.75rem;font-weight:800;white-space:nowrap}
main,.shell,header:not(.gcs-nav){width:min(1120px,calc(100% - 32px));max-width:1120px;margin-inline:auto;padding-inline:0}
main{padding-top:clamp(34px,5vw,72px);padding-bottom:110px}
.shell{padding-top:clamp(34px,5vw,72px)!important;padding-bottom:110px!important}
header:not(.gcs-nav){padding-top:clamp(34px,5vw,72px);padding-bottom:20px}
h1,h2,h3,legend{font-family:var(--serif)!important;color:var(--navy)!important;text-wrap:balance}
h1{max-width:18ch;font-size:clamp(2.7rem,6.4vw,6rem)!important;line-height:.94!important;letter-spacing:-.052em!important;margin:.35em 0 .32em}
h2{font-size:clamp(1.75rem,3.1vw,3rem);line-height:1.06;letter-spacing:-.028em;margin-top:2.1em}
h3{font-size:1.28rem;line-height:1.25}
p,li,dd{max-width:74ch}
a{color:#245e67;text-underline-offset:3px;text-decoration-thickness:1px}
a:hover{color:var(--coral)}
::selection{background:#cfe3da;color:var(--navy)}
:focus-visible{outline:3px solid var(--gold)!important;outline-offset:4px!important}
.eyebrow,.kicker,.stage{color:var(--sage)!important;font-family:var(--sans)!important;font-size:.76rem!important;font-weight:900!important;text-transform:uppercase!important;letter-spacing:.15em!important}
.stage,.badge{display:inline-flex!important;align-items:center;min-height:32px;padding:7px 12px!important;border:1px solid rgba(78,116,104,.2);border-radius:999px!important;background:var(--mint)!important;color:var(--navy)!important}
.hero{gap:clamp(28px,5vw,72px)!important;align-items:center!important;margin-bottom:clamp(40px,6vw,82px)}
.hero picture,.money-hero{position:relative;display:block;isolation:isolate}
.hero picture:before,.money-hero:before{content:"";position:absolute;inset:8% -4% -6% 8%;z-index:-1;border-radius:42px;background:linear-gradient(135deg,rgba(212,172,112,.38),rgba(78,116,104,.22));filter:blur(2px)}
.hero img,.money-hero img{display:block;width:100%;height:auto!important;aspect-ratio:16/9;object-fit:cover;border:1px solid rgba(255,255,255,.72);border-radius:clamp(22px,3vw,38px)!important;box-shadow:var(--shadow)!important}
.money-hero{margin:30px 0 48px!important}
.panel,.answer,.evidence,.disclosure,.provenance,.verdict,.winner,.notice,.hold,.privacy-gate,.curation{position:relative;padding:clamp(22px,3vw,34px)!important;border:1px solid var(--line)!important;border-radius:24px!important;background:rgba(255,253,250,.92)!important;box-shadow:var(--shadow-sm)!important;overflow:hidden}
.panel:before,.winner:before{content:"";position:absolute;inset:0 auto 0 0;width:4px;background:linear-gradient(var(--gold),var(--sage))}
.verdict,.answer{border-left:0!important;background:linear-gradient(135deg,#eef4ef,#fffdfa)!important}
.verdict:before,.answer:before,.notice:before{content:"";position:absolute;inset:0 auto 0 0;width:6px;background:var(--sage)}
.notice:before{background:var(--gold)}
.grid{display:grid!important;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px!important}
.grid>.panel,.grid>a.panel{margin:0!important;min-height:180px;text-decoration:none;color:var(--ink);transition:transform .25s ease,box-shadow .25s ease,border-color .25s ease}
.grid>a.panel:hover{transform:translateY(-5px);border-color:rgba(78,116,104,.45)!important;box-shadow:var(--shadow)!important}
.table-controls{display:flex;gap:10px;flex-wrap:wrap;margin:22px 0 12px}
.table-controls button,button,.begin,.option,.submit{min-height:48px;border:1px solid rgba(12,33,48,.16)!important;border-radius:14px!important;padding:11px 16px!important;background:#fff!important;color:var(--navy)!important;font:800 .92rem/1.2 var(--sans)!important;box-shadow:0 6px 18px rgba(12,33,48,.06);cursor:pointer}
.begin,.submit{background:var(--coral)!important;color:#fff!important;border-color:var(--coral)!important}
.option:hover,.table-controls button:hover{border-color:var(--sage)!important;background:#eef4ef!important}
.table-wrap{width:100%;overflow-x:auto;margin:14px 0 34px;border:1px solid var(--line)!important;border-radius:22px!important;background:var(--white);box-shadow:var(--shadow-sm)}
table{width:100%;border-collapse:separate!important;border-spacing:0!important;background:#fff!important}
th,td{padding:15px!important;border:0!important;border-bottom:1px solid var(--line)!important;vertical-align:top}
thead th{position:sticky;top:0;background:#e8f0eb!important;color:var(--navy)!important;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}
tbody tr:nth-child(even){background:#fbf8f2}
tbody tr:hover{background:#f1eee6}
.cta{border-radius:14px!important;box-shadow:none!important}
.cta.affiliate{background:var(--sage)!important}.cta.lead{background:var(--navy-2)!important}.cta.disabled{background:#68777d!important}
input:not([type="checkbox"]):not([type="radio"]),select,textarea{min-height:50px;border:1px solid rgba(12,33,48,.22)!important;border-radius:13px!important;background:#fff!important;color:var(--ink)!important;padding:12px 14px!important}
input[type="checkbox"],input[type="radio"]{display:inline-block!important;width:22px!important;height:22px!important;min-height:22px!important;padding:0!important;margin:2px 10px 0 0!important;vertical-align:top;accent-color:var(--sage)}
details{border-top:1px solid var(--line);padding-top:16px}summary{min-height:44px;display:flex;align-items:center;font-weight:800;cursor:pointer}
.gcs-home-hero{display:grid;grid-template-columns:1.05fr .95fr;gap:clamp(32px,6vw,84px);align-items:center;padding:clamp(12px,3vw,34px) 0 clamp(60px,9vw,118px)}
.gcs-home-hero h1{font-size:clamp(3.3rem,7.4vw,7rem)!important;max-width:11ch}
.gcs-home-hero .lede{font-size:clamp(1.06rem,1.7vw,1.3rem);color:var(--soft);max-width:54ch}
.gcs-home-actions{display:flex;gap:12px;flex-wrap:wrap;margin-top:26px}
.gcs-button{display:inline-flex;align-items:center;justify-content:center;min-height:50px;padding:12px 18px;border-radius:999px;background:var(--navy);color:#fff!important;text-decoration:none;font-weight:850}
.gcs-button.secondary{border:1px solid var(--line);background:transparent;color:var(--navy)!important}
.gcs-proof{display:grid;grid-template-columns:repeat(4,1fr);gap:1px;margin:0 0 96px;border:1px solid var(--line);border-radius:24px;overflow:hidden;background:var(--line)}
.gcs-proof div{padding:24px;background:#fffdfa}.gcs-proof strong{display:block;font:800 clamp(1.6rem,3vw,2.5rem)/1 var(--serif);color:var(--navy)}.gcs-proof span{display:block;margin-top:8px;color:var(--soft);font-size:.83rem}
.gcs-section-head{display:grid;grid-template-columns:.7fr 1.3fr;gap:32px;align-items:end;margin-bottom:32px}.gcs-section-head h2{margin:0}.gcs-section-head p{margin:0;color:var(--soft)}
.gcs-directory-band{margin:96px 0 16px;padding:clamp(30px,5vw,58px);border-radius:34px;background:linear-gradient(125deg,var(--navy),#214b55);color:#fff;box-shadow:var(--shadow)}
.gcs-directory-band h2{color:#fff!important;margin-top:0;max-width:16ch}.gcs-directory-band p{color:rgba(255,255,255,.78)}.gcs-directory-band .gcs-button{background:#fff;color:var(--navy)!important}
.gcs-legal-footer{max-width:none!important;margin:0!important;padding:42px max(24px,calc((100% - 1120px)/2))!important;display:flex;gap:18px 28px;flex-wrap:wrap;border:0!important;background:var(--navy);color:rgba(255,255,255,.68)}
.gcs-legal-footer:before{content:"Clinic Scout · Evidence before enrollment";flex-basis:100%;font:800 1.35rem/1.2 var(--serif);color:#fff}
.gcs-legal-footer a{min-height:44px;display:inline-flex;align-items:center;color:rgba(255,255,255,.82)!important}
.provider-logo{display:inline-flex;align-items:center;justify-content:center;min-width:88px;min-height:44px;padding:6px 10px;border:1px solid var(--line);border-radius:12px;background:#fff}.provider-logo img{display:block;width:auto;max-width:116px;height:auto;max-height:36px;object-fit:contain;transform:scale(var(--logo-content-scale,1))}.provider-logo--verdict{min-width:112px;min-height:54px}.provider-logo--verdict img{max-width:142px;max-height:44px}.provider-logo--light{background:#fff}.provider-logo--dark{background:#12243a;border-color:#2c4460}${WORDMARK_CSS}.provider-card-head{display:grid;grid-template-columns:auto 1fr auto;gap:16px;align-items:center}.verdict-top{display:flex;align-items:center;gap:18px;flex-wrap:wrap}.intent-card .provider-logo{margin-bottom:12px}.inline-citation{display:inline-flex;align-items:center;justify-content:center;min-width:22px;min-height:22px;margin-left:4px;border-radius:999px;background:#edf4f0;color:#174c39!important;font-size:.75rem;font-weight:800;text-decoration:none}.promo-status{margin:10px 0;padding:10px 12px;border-left:4px solid var(--sage);border-radius:8px;background:#f3f7f4}.promo-status strong{display:block}.promo-status code{display:inline-block;margin-top:6px;padding:4px 8px;border-radius:6px;background:#fff;border:1px dashed var(--sage);font-weight:800}.decision-matrix-row{position:relative}.decision-matrix-row .provider-logo{margin-bottom:12px}.decision-choose{font-size:1.04rem}.decision-choose span{display:block;color:var(--muted);font-size:.8rem;text-transform:uppercase;letter-spacing:.08em}.exit-capture{position:fixed;inset:0;z-index:5000;display:grid;place-items:center;padding:20px;background:rgba(8,23,40,.74);backdrop-filter:blur(5px)}.exit-capture[hidden]{display:none!important}.exit-capture-panel{position:relative;width:min(520px,100%);max-height:calc(100vh - 40px);overflow:auto;padding:28px;border:1px solid rgba(255,255,255,.35);border-radius:24px;background:#fff;box-shadow:0 30px 80px rgba(8,23,40,.35)}.exit-capture-close{position:absolute;right:14px;top:12px;display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;border:0;border-radius:999px;background:#edf4f0;color:#10233b;font-size:1.5rem}.exit-capture-form{display:grid;gap:12px}.exit-capture-form label{font-weight:800}.exit-capture-form input[type=email]{width:100%;min-height:48px;padding:11px 13px;border:1px solid #8fa49a;border-radius:10px;font:inherit}.consent-check{display:grid;grid-template-columns:22px 1fr;gap:10px;align-items:start;font-weight:500!important}.consent-check input{width:20px;height:20px;margin-top:2px}.exit-capture-form button[type=submit],.exit-capture-trigger{min-height:44px;padding:11px 18px;border:0;border-radius:10px;background:var(--coral);color:#fff;font-weight:800}.exit-capture-trigger{display:inline-flex;align-items:center;justify-content:center;margin-top:12px}.capture-status{min-height:24px;color:#174c39;font-weight:700}
@media(max-width:900px){.gcs-home-hero,.hero,.gcs-section-head{grid-template-columns:1fr!important}.gcs-home-hero picture,.hero picture{order:2}.gcs-proof{grid-template-columns:repeat(2,1fr)}.gcs-preview{display:none}.gcs-links{gap:12px}}
@media(max-width:620px){body{font-size:16px}.gcs-nav-inner{width:calc(100% - 24px);min-height:68px}.gcs-links{max-width:58vw;overflow-x:auto}.gcs-links a:nth-child(2){display:none}.gcs-brand span{display:none}main,.shell,header:not(.gcs-nav){width:min(100% - 24px,680px)!important}.grid{grid-template-columns:1fr!important}.gcs-proof{grid-template-columns:1fr 1fr;margin-bottom:64px}.gcs-proof div{padding:18px}.gcs-home-hero{padding-top:4px}.gcs-home-actions{display:grid}.gcs-button{width:100%}.sticky{padding-bottom:max(10px,env(safe-area-inset-bottom))!important}.table-wrap{margin-inline:0}h1{font-size:clamp(2.65rem,14vw,4.6rem)!important}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}*,*:before,*:after{transition:none!important;animation:none!important}}
`;

function addGuideMetadata(html, article, author) {
  if (!/^\/(?:guides|best)\/[a-z0-9-]+\/$/.test(article.canonical_path || "")) throw new Error(`article_canonical_path_invalid:${article.article_id}`);
  const canonicalUrl = `https://glpclinicscout.com${article.canonical_path}`;
  const rawDescription = String(article.description || article.direct_answer || article.h1).replace(/\s+/g, " ").trim();
  const description = rawDescription.length <= 160 ? rawDescription : `${rawDescription.slice(0, 157).replace(/\s+\S*$/, "")}…`;
  // Phase-4 gate remediation 2026-08-27 (GATES-PRELIM issues #5/#6/#9):
  // - The guides crumb routed through the RETIRED /guides/ hub (301 -> /insights/)
  //   on all 63 guides; it now points at the live /insights/ hub, matching the
  //   visible breadcrumb these pages already render.
  // - /best/* crumbed to a nonexistent /best/ hub (404, no redirect); it now
  //   points at the live /providers/ comparison hub.
  // - Article.image: the guide hero derivative already shipped and preloaded on
  //   these pages (guide-{slug}-1600.jpg, the largest JPG for compatibility) is
  //   attached only when the asset actually exists — never fabricated.
  const heroSlug = article.canonical_path.split("/").filter(Boolean).at(-1);
  const heroJpg = `/assets/images/guide-${heroSlug}-1600.jpg`;
  const schema = buildSchemaGraph({
    url: canonicalUrl,
    title: article.title,
    description,
    datePublished: article.updated_at,
    dateModified: article.date_modified || article.updated_at,
    author,
    image: existsSync(path.join(ROOT, "assets/derivatives", `guide-${heroSlug}-1600.jpg`)) ? `https://glpclinicscout.com${heroJpg}` : null,
    evidenceReview: { status: article.evidence_review?.status, source_urls: (article.sources || []).map((item) => item.url) },
    breadcrumbs: [{ name: "Home", path: "/" }, article.canonical_path.startsWith("/best/") ? { name: "Compare", path: "/providers/" } : { name: "Guides", path: "/insights/" }, { name: article.h1, path: article.canonical_path }],
  });
  const hasCanonical = /<link\s+rel="canonical"/i.test(html);
  const meta = `<meta name="description" content="${escHtml(description)}">`;
  const withDescription = /<meta\s+name="description"[^>]*>/i.test(html) ? html.replace(/<meta\s+name="description"[^>]*>/i, meta) : html.replace("</head>", `${meta}</head>`);
  const metadata = `${hasCanonical ? "" : `<link rel="canonical" href="${canonicalUrl}">`}<script type="application/ld+json">${JSON.stringify(schema.jsonLd)}</script>`;
  return withDescription.replace("</head>", `${metadata}</head>`);
}

// Phase-4 gate remediation 2026-08-27 (GATES-PRELIM issue #7): the quiz page
// dropped the BreadcrumbList it had on live during the rebuild. Restore it at
// the write site (src/quiz.mjs is owned by the conversion-attribution lane in
// this pass; a head JSON-LD block is placement-equivalent). The WebApplication
// node the renderer emits is untouched.
function addQuizBreadcrumbSchema(html) {
  const schema = JSON.stringify({
    "@context": "https://schema.org",
    "@type": "BreadcrumbList",
    "@id": "https://glpclinicscout.com/quiz/provider-fit/#breadcrumbs",
    itemListElement: [
      { "@type": "ListItem", position: 1, name: "Home", item: "https://glpclinicscout.com/" },
      { "@type": "ListItem", position: 2, name: "Provider fit quiz", item: "https://glpclinicscout.com/quiz/provider-fit/" },
    ],
  });
  return html.replace("</head>", `<script type="application/ld+json">${schema}</script></head>`);
}

function addCanonical(html, relative) {
  if (/<link\s+rel="canonical"/i.test(html)) return html;
  const route = `/${relative.replace(/index\.html$/, "")}`;
  return html.replace("</head>", `<link rel="canonical" href="https://glpclinicscout.com${route}"></head>`);
}

function addTrustMetadata(html, relative) {
  const descriptions = {
    "how-we-rate/index.html": "See the exact evidence criteria, weights, missing-data treatment, and accountability behind Clinic Scout program scores.",
    "how-we-make-money/index.html": "See how Clinic Scout affiliate relationships work and which ordering methods our comparisons use.",
    "editorial-guidelines/index.html": "Read Clinic Scout's source, risk-tier, correction, disclosure, and publishing standards.",
    "review-standards/index.html": "See how Clinic Scout checks commercial and health-information pages without inventing clinical authority.",
    "authors/izaiah-tilton/index.html": "Meet Izaiah Tilton, the named publisher accountable for Clinic Scout's research and commercial comparison systems.",
    "authors/clinic-scout-editorial-team/index.html": "Learn what the Clinic Scout Editorial Team organizational byline means and how the research process works.",
  };
  const description = descriptions[relative];
  if (!description) return html;
  const route = `/${relative.replace(/index\.html$/, "")}`;
  const schema = { "@context": "https://schema.org", "@type": "WebPage", url: `https://glpclinicscout.com${route}`, description, dateModified: "2026-07-30", publisher: { "@type": "Organization", name: "GLP Clinic Scout" } };
  return html.replace("</head>", `<meta name="description" content="${escHtml(description)}"><script type="application/ld+json">${JSON.stringify(schema)}</script></head>`);
}

function setRobots(html, content) {
  const meta = `<meta name="robots" content="${content}">`;
  return /<meta\s+name="robots"[^>]*>/i.test(html)
    ? html.replace(/<meta\s+name="robots"[^>]*>/i, meta)
    : html.replace("</head>", `${meta}</head>`);
}

function markReleaseReady(html) {
  return setRobots(html, "index,follow,max-image-preview:large")
    .replace(/<html([^>]*?)\sdata-release="[^"]+"([^>]*)>/i, '<html$1 data-release="release-ready"$2>')
    .replace(/<html(?![^>]*\sdata-release=)([^>]*)>/i, '<html data-release="release-ready"$1>');
}

function buildSitemap(routes, lastmodByRoute = new Map()) {
  const locations = [...new Set(routes)].sort().map((route) => {
    const lastmod = lastmodByRoute.get(route);
    if (lastmod !== undefined && !/^\d{4}-\d{2}-\d{2}$/.test(String(lastmod))) throw new Error(`sitemap_lastmod_invalid:${route}`);
    return `<url><loc>https://glpclinicscout.com${route}</loc>${lastmod ? `<lastmod>${lastmod}</lastmod>` : ""}</url>`;
  }).join("");
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${locations}</urlset>\n`;
}

const READY6_LEGACY_REDIRECTS = Object.freeze([
  ["/reviews/awin-128761/", "/reviews/coivas/"],
  ["/reviews/katalys-1286/", "/reviews/sprout-health/"],
  ["/reviews/katalys-647/", "/reviews/mangorx/"],
  ["/reviews/katalys-1304/", "/reviews/direct-meds/"],
  ["/reviews/katalys-1516/", "/reviews/shed/"],
  ["/reviews/katalys-1518/", "/reviews/breezemeds/"],
]);

// 2026-08-17 network pause (Katalys offers 1518/1519/1520). A paused program has
// no joinable offer, so its review page - which carries a transparent-direct
// affiliate CTA - and the comparison page written entirely about two paused
// programs are removed from the build. Their URLs 301 to the live comparison
// instead of 404ing, so inbound links and accumulated search equity land on a
// page that can still answer the reader's question. They leave the sitemap by
// virtue of no longer being built.
const RETIRED_PROGRAM_ROUTES = [
  ["/reviews/synergy-rx/", "/providers/"],
  ["/reviews/care-bare-rx-glp1/", "/providers/"],
  ["/reviews/breezemeds/", "/providers/"],
  ["/compare/synergy-rx-vs-carebarerx/", "/providers/"],
  // 2026-08-18 niche-fit removal (operator ruling): three non-GLP programs left
  // this site for their own verticals. Their review routes must 301 rather than
  // 404 - a 404 lets Cloudflare Always Online serve an archived copy of the very
  // page we removed, which is exactly what a live check found after the deploy.
  ["/reviews/paternity-lab/", "/providers/"],
  ["/reviews/advanced-trichology/", "/providers/"],
  ["/reviews/lyfefuel-us/", "/providers/"],
];

const CONSOLIDATED_DUPLICATE_ROUTES = [
  ["/guides/healthrx-vs-agelessrx/", "/compare/healthrx-vs-agelessrx/"],
  ["/guides/wellorithm-vs-enhance-md/", "/compare/wellorithm-vs-enhance-md/"],
  ["/guides/cheapest-glp1/", "/cost/online-glp-1-programs/"],
  ["/guides/compare-online-glp-1-provider-fees/", "/cost/online-glp-1-programs/"],
  ["/guides/glp-1-program-total-cost-comparison/", "/cost/online-glp-1-programs/"],
];
const CONSOLIDATED_SOURCE_ROUTES = new Set(CONSOLIDATED_DUPLICATE_ROUTES.map(([from]) => from));
// Design pass 2026-08-20 (Fable edit 1): SHORT NAV ALIASES. The memorable route a
// reader types or a partner links ("/quiz/") is not the canonical route we publish
// ("/quiz/provider-fit/"), so it used to hit the 404. These are additive nav aliases,
// declared separately from the frozen READY6/consolidation contracts above so that
// contract stays exactly what it was; scripts/validate-g9-retirement.mjs mirrors this
// list in the same order. Never a chain: each target is a built, canonical route.
const SHORT_NAV_ALIASES = [
  ["/quiz/", "/quiz/provider-fit/"],
];

// MUSTFIX 2026-08-22: four provider-detail aliases were linked by the Phase 3
// medication/category graph but had no static page or redirect. Keep these outside
// the Phase 3 indexable-route denominator because none is a canonical page.
const MUSTFIX_PROVIDER_DETAIL_REDIRECTS = Object.freeze([
  ["/providers/coivas/", "/reviews/coivas/"],
  ["/providers/sprout-health/", "/reviews/sprout-health/"],
  ["/providers/direct-meds/", "/reviews/direct-meds/"],
  ["/providers/mangorx/", "/reviews/mangorx/"],
]);

function aliasReviewRoute(program) {
  return program.review_path || `/reviews/${program.program_id}/`;
}

function buildReady6Redirects(programs = []) {
  // SHED-301 (2026-08-06): a merchant alias's review route consolidates onto its
  // canonical record. Both the network-id route and the brand route 301 straight
  // to the canonical review — derived from the registry's canonical_identity
  // declarations, so the next alias gets the same treatment without editing this
  // file, and no redirect ever chains through a second redirect.
  const aliasTarget = new Map();
  for (const program of programs) {
    const declaration = program.profiles?.[SITE_PROFILE]?.canonical_identity;
    if (declaration?.role !== "alias") continue;
    const canonical = programs.find((row) => row.program_id === declaration.canonical_program_id);
    if (!canonical?.review_path) throw new Error(`alias_canonical_unresolved:${program.program_id}`);
    // S9-7: an identity-only alias declares no review surface of its own, so its
    // legacy review route is derived from its id. The 301 still ships.
    aliasTarget.set(aliasReviewRoute(program), canonical.review_path);
  }
  // A legacy network-id route whose brand route has since been retired must point
  // at the retirement target directly - never at a URL that itself redirects.
  const retiredTarget = new Map(RETIRED_PROGRAM_ROUTES);
  const resolveTarget = (to) => {
    const aliased = aliasTarget.get(to) || to;
    return retiredTarget.get(aliased) || aliased;
  };
  const lines = READY6_LEGACY_REDIRECTS.map(([from, to]) => `${from} ${resolveTarget(to)} 301`);
  const emitted = new Set(READY6_LEGACY_REDIRECTS.map(([from]) => from));
  for (const [from, to] of RETIRED_PROGRAM_ROUTES) if (!emitted.has(from)) { lines.push(`${from} ${to} 301`); emitted.add(from); }
  // Consolidation 2026-08-15 (strategy rule R1): duplicate-intent URLs 301 to
  // their canonical — kills live keyword cannibalization. Pages stay built
  // (redirects fire before static lookup); they are dropped from the sitemap.
  for (const [from, to] of CONSOLIDATED_DUPLICATE_ROUTES) lines.push(`${from} ${to} 301`);

  for (const [from, to] of aliasTarget) lines.push(`${from} ${to} 301`);
  // Short nav aliases last: additive, and never a source that is already emitted.
  for (const [from, to] of SHORT_NAV_ALIASES) if (!lines.some((line) => line.startsWith(`${from} `))) lines.push(`${from} ${to} 301`);
  return `${lines.join("\n")}\n`;
}

function addGuideFunnel(html) {
  const panel = `<aside class="panel evidence-line guide-next-step" aria-label="Next steps"><p class="gcs-kicker">Next steps</p><h2>Turn this research into a decision</h2><div class="gcs-actions"><a class="gcs-cta gcs-cta-primary" href="/tools/glp-1-cost-calculator/">Estimate your cost</a><a class="gcs-cta gcs-cta-secondary" href="${PASS5_MEDICATION_DESTINATIONS.allProviders}">Compare programs</a><a class="gcs-text-link" href="/quiz/provider-fit/">Find my match</a></div></aside>`;
  const mainStart = html.search(/<main\b[^>]*>/i);
  const answerStart = mainStart < 0 ? -1 : html.indexOf('<section class="answer"', mainStart);
  const firstSectionEnd = answerStart < 0 ? -1 : html.indexOf("</section>", answerStart);
  if (firstSectionEnd < 0 || !html.includes("</main>")) throw new Error("guide_funnel_insertion_point_missing");
  const early = `${html.slice(0, firstSectionEnd + 10)}${panel}${html.slice(firstSectionEnd + 10)}`;
  return early.replace("</main>", `${panel}</main>`);
}

function upgradeReviewScore(html) {
  return html.replace(/<p class="score">(\d+(?:\.\d+)?)\/10<\/p>/g, (_match, outOfTen) => {
    const score = Number(outOfTen) * 10;
    const outOfFive = (score / 20).toFixed(1);
    return `<p class="score"><span class="score-lockup"><strong class="score-ten">${Number(outOfTen).toFixed(1)}<span>/10</span></strong><span class="stars" role="img" aria-label="${Number(outOfTen).toFixed(1)} out of 10; visualized as ${outOfFive} out of 5 stars"><span aria-hidden="true">★★★★★</span><span class="stars-fill" style="width:${score}%" aria-hidden="true">★★★★★</span></span></span></p>`;
  });
}

// ULTRACODE renderer:shell 2026-08-21. The shared chrome now emits the
// 02-shell.css pattern: brand lockup (aurora-aperture mark + webfont wordmark,
// per the lockup source's own note in assets/brand/logo-horizontal-dark.svg),
// grouped dropdown navigation derived from the REAL route families, the
// expandable local search (runtime in assets/site.js, index minted by this
// build as /assets/search-index.json), and the drawer/scrim mobile pattern.
// The drawer and search panel ship with `hidden` as their resting state —
// [hidden]{display:none} keeps the SSR'd drawer invisible on desktop, out of
// the tab order when closed, and honestly hidden to headless visibility
// checks; the site.js controllers lift the attribute around their transitions.
// Nav menus and search link ONLY currently-linked, indexable surfaces: the
// wave-4 link-dark routes (price tracker, savings monitor, /research/*) stay
// unlinked — redesign is not activation authority.
const NAV_GROUPS = [
  ["Bulk", [
    ["/3-month/?drug=SEMAGLUTIDE&form=COMPOUNDED&plan=3&sort=cheapest", "3-month semaglutide", "Published three-month semaglutide terms and price evidence"],
    ["/3-month/?drug=TIRZEPATIDE&form=COMPOUNDED&plan=3&sort=cheapest", "3-month tirzepatide", "Published three-month tirzepatide terms and price evidence"],
    ["/6-month/?drug=SEMAGLUTIDE&form=COMPOUNDED&duration=6&sort=cheapest", "6+ month semaglutide", "Published longer-term semaglutide options and qualifiers"],
    ["/6-month/?drug=TIRZEPATIDE&form=COMPOUNDED&duration=6&sort=cheapest", "6+ month tirzepatide", "Published longer-term tirzepatide options and qualifiers"],
  ]],
  ["Telehealth Providers", [
    [PASS5_MEDICATION_DESTINATIONS.compoundedSemaglutide, "Compounded semaglutide", "Filter source-dated compounded semaglutide provider rows"],
    [PASS5_MEDICATION_DESTINATIONS.compoundedTirzepatide, "Compounded tirzepatide", "Filter source-dated compounded tirzepatide provider rows"],
    [PASS5_MEDICATION_DESTINATIONS.wegovyPill, "Wegovy pill", "Compare bound oral semaglutide provider pathways"],
    [PASS5_MEDICATION_DESTINATIONS.wegovy, "Wegovy", "Compare provider pathways with published Wegovy rows"],
    [PASS5_MEDICATION_DESTINATIONS.ozempic, "Ozempic", "Compare provider pathways with published Ozempic rows"],
    [PASS5_MEDICATION_DESTINATIONS.mounjaro, "Mounjaro", "Compare provider pathways with published Mounjaro rows"],
    [PASS5_MEDICATION_DESTINATIONS.zepbound, "Zepbound", "Compare provider pathways with published Zepbound rows"],
  ]],
];

// The Price Index is an existing byte-frozen surface. Rebuild its semantic
// comparison with the exact predecessor chrome instead of sending it through the
// redesign shell; preservation mode then re-emits the snapshotted bytes below.
function addByteFrozenSharedRuntime(html) {
  const config = `<link rel="icon" type="image/svg+xml" href="/assets/brand/favicon.svg"><link rel="apple-touch-icon" href="/assets/brand/favicon.svg"><link rel="preload" href="/assets/fonts/manrope-latin-var.woff2" as="font" type="font/woff2" crossorigin><link rel="preload" href="/assets/fonts/source-sans-3-latin-var.woff2" as="font" type="font/woff2" crossorigin><link rel="stylesheet" href="/assets/site.css?v=${ASSET_VERSION}"><script>window.GCS_ANALYTICS={enabled:true,consent:'granted',measurement_id:'G-2WQ42JY0WK',debug_measurement_id:'G-PY9P5CW2EM',operator_gate:'operator-approved:analytics-default-on-20260801'};window.GCS_CLARITY={enabled:false,project_id:null,operator_gate:'held'};window.GCS_POSTHOG=${JSON.stringify(postHogRuntimeConfig)};</script><script>if(navigator.webdriver===true||/HeadlessChrome|Headless|Playwright|Puppeteer|Lighthouse|Google-InspectionTool|synthetic|bot|crawler|spider/i.test(navigator.userAgent||'')||['localhost','127.0.0.1','::1'].includes(location.hostname)){window['ga-disable-G-2WQ42JY0WK']=true;window['ga-disable-G-PY9P5CW2EM']=true;window.GCS_ANALYTICS_SUPPRESSED=true}</script><script>if(!window.GCS_ANALYTICS_SUPPRESSED)(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src='https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);})(window,document,'script','dataLayer','GTM-PWR49VVK');</script><script src="/assets/site.js?v=${ASSET_VERSION}" defer></script><script src="/assets/consent.js?v=${ASSET_VERSION}" defer></script><script src="/assets/${ANALYTICS_ASSET_FILENAME}" defer></script>`;  const route = html.match(/<link rel="canonical" href="https:\/\/glpclinicscout\.com([^"#?]*)/i)?.[1] || "/";
  const navLink = (href, label, active) => `<a href="${href}"${active ? ' aria-current="page"' : ""}>${label}</a>`;
  // Frozen price facts do not exempt the shared shell from the export hold.
  const byteFrozenConfig = candidateSharedRuntime(config);
  const compareActive = /^\/(best|compare|cost|reviews)\//.test(route);
  const nav = `<nav class="gcs-nav" aria-label="Primary"><div class="gcs-nav-inner"><a class="gcs-brand" href="/"${route === "/" ? ' aria-current="page"' : ""}><img src="/assets/brand/favicon.svg" alt="" width="36" height="36"><span>Clinic Scout</span></a><button class="gcs-menu-button" type="button" aria-expanded="false" aria-controls="gcs-menu" data-menu-toggle>Menu</button><div id="gcs-menu" class="gcs-links" data-menu>${navLink("/providers/", "Compare", compareActive)}${navLink("/tools/glp-1-cost-calculator/", "Calculator", route.startsWith("/tools/"))}${navLink("/insights/", "Research library", route.startsWith("/guides/"))}${navLink("/how-we-rate/", "How we rate", route === "/how-we-rate/")}</div><a class="gcs-nav-action" href="/quiz/provider-fit/"${route.startsWith("/quiz/") ? ' aria-current="page"' : ""}>Find your fit</a></div></nav>`;
  const preFooter = `<div class="cs-prefooter">Still have questions? <a href="/insights/">Read the research library</a> or email us at <a href="mailto:support@glpclinicscout.com">support@glpclinicscout.com</a>.</div>`;
  const footer = `<footer class="gcs-footer"><div class="gcs-footer-grid"><section class="gcs-footer-brand"><p class="gcs-footer-wordmark">Clinic Scout</p><p>Evidence before enrollment. Source gaps stay visible.</p><p class="cs-footer-strength">Compare total monthly cost, renewal terms, and included services before you choose.</p><a class="cs-footer-badge" href="/glp-1-price-index/"><b>Price Index</b> Compare total monthly cost</a></section><section><p class="gcs-footer-heading">Compare</p><ul><li><a href="/providers/">Best programs</a></li><li><a href="/cost/online-glp-1-programs/">Cost comparison</a></li><li><a href="/tools/glp-1-cost-calculator/">Cost calculator</a></li><li><a href="/quiz/provider-fit/">Provider fit</a></li></ul></section><section><p class="gcs-footer-heading">Evidence</p><ul><li><a href="/how-we-rate/">How we rate</a></li><li><a href="/editorial-guidelines/">Editorial guidelines</a></li><li><a href="/review-standards/">Review standards</a></li></ul></section><section><p class="gcs-footer-heading">Company</p><ul><li><a href="/authors/izaiah-tilton/">Publisher</a></li><li><a href="/how-we-make-money/">How we make money</a></li><li><a href="/privacy/lead-capture/">Contact and corrections</a></li></ul></section><section><p class="gcs-footer-heading">Legal</p><ul><li><a href="/privacy/">Privacy</a></li><li><a href="/terms/">Terms</a></li><li><a href="/privacy/cookies/">Cookie choices</a></li><li><a href="/privacy/#consumer-health-data">Consumer health data</a></li><li><a href="/how-we-make-money/">Affiliate disclosure</a></li></ul></section></div><p class="gcs-entity">Entity: Tilton Group LLC · California entity B20260317552 · Principal and mailing address: 2108 N St, Ste N, Sacramento, CA 95816.</p></footer>`;
  return upgradeReviewScore(html)
    .replace(/<style\b(?![^>]*data-preserve)[^>]*>[\s\S]*?<\/style>/gi, "")
    .replaceAll("The next five steps may ask a broad BMI range, medication status, goal, insurance expectation, and state.", "The next six steps may ask a broad BMI range, medication status, goal, insurance expectation, state, and timing.")
    .replaceAll(">Send the checklist</button>", ">Save request</button>")
    .replaceAll('class="eyebrow"', 'class="gcs-kicker"')
    .replaceAll('class="stage"', 'class="gcs-kicker"')
    .replace(/<main(?![^>]*\bid=)([^>]*)>/i, '<main id="main" tabindex="-1"$1>')
    .replace("</head>", `${byteFrozenConfig}</head>`)
    .replace(/<body([^>]*)>/i, `<body$1><a class="skip-link" href="#main">Skip to main content</a><div class="cs-topstrip" aria-hidden="true"></div>${nav}`)
    .replace("</body>", `${preFooter}${footer}</body>`);
}

function addSharedRuntime(html) {
  const route = html.match(/<link rel="canonical" href="https:\/\/glpclinicscout\.com([^"#?]*)/i)?.[1] || "/";
  const stylesheet = route === "/" ? "site-home.css"
    : route === "/compare/" ? "site-p3.css"
    // 2026-08-27 compare-search fix 4: this /best/ route is an upgraded guide
    // document, not a money-table template; load the bundle that owns its
    // guide-shell container, cinema hero, and article furniture.
    : route === "/best/ozempic-alternatives/" ? "site-editorial.css"
    : /^\/(?:best|compare|cost)\//.test(route) ? "site-money.css"
    : route.startsWith("/reviews/") ? "site-review.css"
    : /^\/(?:guides|authors)\//.test(route) ? "site-editorial.css"
    : /^\/(?:quiz|tools|matches)\//.test(route) ? "site-tools.css"
    : /^\/(?:how-we-rate|how-we-make-money|review-standards|editorial-guidelines|privacy|terms|security)(?:\/|$)/.test(route) ? "site-trust.css"
    // Phase-4 gate remediation 2026-08-27 (GATES-PRELIM issue #3): the p3
    // revenue family (brand pages, providers, pharmacies, bulk, self-pay) was
    // the only template family still loading the 638KB render-blocking site.css
    // monolith. It now gets its own site-p3.css bundle (COMMON + 11-phase3 +
    // 12-phase3-closure + 30-provider-detail + 90-compat, verified by class-use
    // grep: the capture/panel/footer generics those pages rely on live in
    // 90-compat). /lp/ routes are deliberately excluded — they keep the default
    // and are rewritten to site-editorial.css by optimizePaidLandingPage.
    // Insights index pages are 40-editorial-styled (cs-ins-*, p3-pagination-*)
    // so they route to the existing editorial bundle instead.
    : /^\/(?:providers|pharmacies)\//.test(route) || ["/self-pay/", "/bulk/", "/3-month/", "/6-month/", "/ozempic/", "/wegovy/", "/wegovy-pill/", "/mounjaro/", "/zepbound/"].includes(route) ? "site-p3.css"
    : route === "/insights/" || /^\/insights\/page\//.test(route) ? "site-editorial.css"
    : "site.css";
  const template = route === "/" ? "home"
    : route === "/guides/" ? "library"
    : route.startsWith("/guides/") ? "guide"
    : route.startsWith("/authors/") ? "author"
    : route === "/reviews/submit/" ? "review-intake"
    : route.startsWith("/reviews/") ? "review"
    : route === "/matches/customized/" ? "matcher"
    : route === "/quiz/provider-fit/" ? "quiz"
    : route === "/tools/glp-1-cost-calculator/" ? "calculator"
    : route === "/glp-1-price-index/" ? "price-index"
    : stylesheet === "site-trust.css" ? "trust"
    : "default";
  const editorialHero = /^\/guides\//.test(route)
    ? html.match(/<source\s+srcset="([^"]+)"\s+sizes="([^"]+)"\s+type="image\/avif">/i)
    : null;
  const editorialHeroPreload = editorialHero
    ? `<link rel="preload" as="image" imagesrcset="${editorialHero[1]}" imagesizes="${editorialHero[2]}" type="image/avif" fetchpriority="high">`
    : "";
  const shellTighten = `<style data-preserve data-pass2-shell>body[data-cs-template]:not([data-cs-template="home"]):not([data-cs-template="calculator"])>main:not(.p3-page){padding-top:clamp(20px,3vw,42px)!important}</style>`;
  // Phase-4 gate remediation 2026-08-27 (GATES-PRELIM issue #8): the SplitText
  // and Flip vendor plugins were loaded on ~249 pages and referenced by nothing
  // in the shipped runtime (12.7KB gz dead weight). Their script tags are
  // dropped from the shared shell; the vendored files stay on disk for future use.
  const config = `${shellTighten}<link rel="icon" type="image/svg+xml" href="/assets/brand/favicon.svg"><link rel="icon" type="image/png" sizes="32x32" href="/assets/brand/favicon-32.png"><link rel="icon" type="image/png" sizes="512x512" href="/assets/brand/favicon-512.png"><link rel="apple-touch-icon" sizes="180x180" href="/assets/brand/apple-touch-icon.png"><meta name="theme-color" content="#07100d"><link rel="preload" href="/assets/fonts/manrope-latin-var.woff2" as="font" type="font/woff2" crossorigin><link rel="preload" href="/assets/fonts/source-sans-3-latin-var.woff2" as="font" type="font/woff2" crossorigin>${editorialHeroPreload}<link rel="stylesheet" href="/assets/${stylesheet}?v=${ASSET_VERSION}"><script>window.GCS_ANALYTICS={enabled:true,consent:'granted',measurement_id:'G-2WQ42JY0WK',debug_measurement_id:'G-PY9P5CW2EM',operator_gate:'operator-approved:analytics-default-on-20260801'};window.GCS_CLARITY={enabled:false,project_id:null,operator_gate:'held'};window.GCS_POSTHOG=${JSON.stringify(postHogRuntimeConfig)};</script><script>if(navigator.webdriver===true||/HeadlessChrome|Headless|Playwright|Puppeteer|Lighthouse|Google-InspectionTool|synthetic|bot|crawler|spider/i.test(navigator.userAgent||'')||['localhost','127.0.0.1','::1'].includes(location.hostname)){window['ga-disable-G-2WQ42JY0WK']=true;window['ga-disable-G-PY9P5CW2EM']=true;window.GCS_ANALYTICS_SUPPRESSED=true}</script><script>if(!window.GCS_ANALYTICS_SUPPRESSED)(function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start':new Date().getTime(),event:'gtm.js'});var f=d.getElementsByTagName(s)[0],j=d.createElement(s),dl=l!='dataLayer'?'&l='+l:'';j.async=true;j.src='https://www.googletagmanager.com/gtm.js?id='+i+dl;f.parentNode.insertBefore(j,f);})(window,document,'script','dataLayer','GTM-PWR49VVK');</script><script src="/assets/vendor/gsap/gsap.min.js" defer></script><script src="/assets/vendor/gsap/ScrollTrigger.min.js" defer></script><script src="/assets/vendor/lenis/lenis.min.js" defer></script><script src="/assets/motion.js?v=${ASSET_VERSION}" defer></script><script src="/assets/site.js?v=${ASSET_VERSION}" defer></script><script src="/assets/consent.js?v=${ASSET_VERSION}" defer></script><script src="/assets/${ANALYTICS_ASSET_FILENAME}" defer></script>`;
  // META_PIXEL_RESTORE_V1: reference the pixel on the homepage only.
  // meta-pixel.js's own PAGEVIEW_PATHS already lists "/"; the file simply
  // stopped being written and stopped being referenced.
  const heldConfig = candidateSharedRuntime(config);
  const withMetaPixel = template === "home"
    ? heldConfig.replace("</script>", `</script><script defer src="/meta-pixel.js?v=${ASSET_VERSION}" data-glp-meta-pixel="pageview-viewcontent"></script>`)
    : heldConfig;
  const prePaintConfig = template === "quiz"
    ? withMetaPixel.replace('<link rel="icon"', '<script>document.documentElement.classList.add("js")</script><link rel="icon"')
    : withMetaPixel;
  const performanceConfig = prePaintConfig.replace(
    '<script src="/assets/vendor/gsap/gsap.min.js" defer></script><script src="/assets/vendor/gsap/ScrollTrigger.min.js" defer></script><script src="/assets/vendor/lenis/lenis.min.js" defer></script><script src="/assets/motion.js?v=',
    '<script src="/assets/motion-loader.js?v=',
  );
  if (performanceConfig === prePaintConfig) throw new Error("motion_loader_binding_rewrite_failed");
  const utility = "";
  const current = (href) => { const target = href.split(/[?#]/)[0]; return (target === "/" ? route === "/" : route.startsWith(target)) ? ' aria-current="page"' : ""; };
  const menuLink = ([href, label, note]) => `<a href="${href}"${current(href)}>${label}<small>${note}</small></a>`;
  const navGroups = NAV_GROUPS.map(([label, items]) => `<li class="cs-navgroup"><button type="button" aria-haspopup="true" aria-expanded="false">${label}</button><div class="cs-navmenu">${items.map(menuLink).join("")}</div></li>`).join("");
  const brand = `<a class="gcs-brand" href="/"${current("/")}><span>Clinic <span class="cs-grad-words">Scout</span></span></a>`;
  const searchPanel = `<div class="cs-search-panel" id="cs-header-search" role="search" aria-label="Search Clinic Scout" hidden><div class="cs-search-box"><button type="button" class="cs-search-back" data-search-close aria-label="Close search and clear"><svg width="18" height="18" viewBox="0 0 20 20" fill="none" aria-hidden="true"><path d="m12.5 4.5-5.5 5.5 5.5 5.5M7.5 10h9" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></button><input type="search" placeholder="Search providers or pharmacies" autocomplete="off" aria-label="Search providers or pharmacies"><span class="cs-search-status" aria-hidden="true"><span class="cs-dot"></span><span class="cs-dot"></span><span class="cs-dot"></span></span></div><div class="cs-search-results"></div></div>`;
  const searchControl = `<div class="cs-search"><button class="cs-search-toggle" type="button" aria-controls="cs-header-search" aria-expanded="false"><svg width="15" height="15" viewBox="0 0 15 15" fill="none" aria-hidden="true"><circle cx="6.5" cy="6.5" r="5" stroke="currentColor" stroke-width="1.6"/><path d="m10.5 10.5 3.5 3.5" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg><span>Search providers</span></button>${searchPanel}</div>`;
  const menuToggle = `<button class="cs-menu-toggle" type="button" aria-expanded="false" aria-controls="cs-drawer" aria-label="Menu" data-menu-toggle><svg width="18" height="14" viewBox="0 0 18 14" fill="none" aria-hidden="true"><path d="M1 1h16M1 7h16M1 13h16" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg><span>Menu</span></button>`;
  const nav = `<nav class="gcs-nav" aria-label="Primary"><div class="gcs-nav-inner">${brand}${searchControl}<ul class="gcs-links"><li><a href="/insights/"${current("/insights/")}>Insights</a></li>${navGroups}<li><a href="/pharmacies/"${current("/pharmacies/")}>Compounding Pharmacies</a></li><li><a href="/reviews/"${current("/reviews/")}>Reviews</a></li><li><a href="/tools/glp-1-cost-calculator/"${current("/tools/glp-1-cost-calculator/")}>Calculator</a></li><li><a class="cta primary cs-palette-action" href="/quiz/provider-fit/">Find your fit</a></li></ul>${menuToggle}</div></nav>`;
  // The drawer remains a sibling of the sticky header so its fixed panel is not
  // trapped by the header's backdrop-filter containing block.
  const drawerSection = (label, items) => {
    const id = `cs-drawer-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
    return `<section class="cs-drawer__section"><button class="cs-drawer__toggle" type="button" data-drawer-section-toggle aria-expanded="false" aria-controls="${id}"><span>${label}</span><svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true"><path d="m4 6 4 4 4-4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg></button><div class="cs-drawer__panel" id="${id}" data-drawer-section-panel hidden>${items.map(([href, text]) => `<a href="${href}"${current(href)}>${text}</a>`).join("")}</div></section>`;
  };
  const drawerLinks = `<a href="/insights/"${current("/insights/")}>Insights</a>${NAV_GROUPS.map(([label, items]) => drawerSection(label, items.map(([href, text]) => [href, text]))).join("")}<a href="/pharmacies/"${current("/pharmacies/")}>Compounding Pharmacies</a><a href="/reviews/"${current("/reviews/")}>Reviews</a><a href="/tools/glp-1-cost-calculator/"${current("/tools/glp-1-cost-calculator/")}>Calculator</a>`;
  const drawer = `<div class="cs-scrim" aria-hidden="true"></div><nav class="cs-drawer" id="cs-drawer" aria-label="Menu" tabindex="-1" data-menu hidden><button class="cs-drawer__close" type="button" data-menu-close aria-label="Close menu"><span>Close</span><svg width="18" height="18" viewBox="0 0 18 18" fill="none" aria-hidden="true"><path d="m3 3 12 12M15 3 3 15" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></button>${drawerLinks}<a class="cta primary cs-palette-action" href="/quiz/provider-fit/">Find your fit</a></nav>`;
  // B-14 (LAY §2.3): pre-footer band. Cheap trust + support signal, their
  // pattern, our addresses. `support@` is the alias whose PURPOSE is reader
  // contact (data/email-alias-map.json); ops@ is never public.
  // The alias is a property of the PAGE'S PURPOSE (data/email-alias-map.json):
  // a band shipped sitewide must not publish support@ on a privacy route, or
  // a reader's data-subject request lands on the wrong desk.
  const preFooterAlias = route.startsWith("/privacy/") ? "privacy"
    : route.startsWith("/security") ? "security"
    : route.startsWith("/google-ads-api/privacy/") ? "privacy"
    : "support";
  const preFooter = `<div class="cs-prefooter"><div class="cs-prefooter__inner"><p>Still have questions? <a href="/insights/">Read the research library</a> or email us at <a href="mailto:${preFooterAlias}@glpclinicscout.com">${preFooterAlias}@glpclinicscout.com</a>.</p><button class="cs-btn cs-palette-action cs-subscribe-open" type="button" data-subscribe-open>Get price updates <span aria-hidden="true">✉</span></button></div></div>`;
  const subscribeSheet = `<div class="cs-subscribe-layer" data-subscribe-layer hidden><button class="cs-subscribe-backdrop" type="button" aria-label="Close subscribe form" data-subscribe-close></button><section class="cs-subscribe-sheet" role="dialog" aria-modal="true" aria-labelledby="cs-subscribe-title" tabindex="-1"><button class="cs-subscribe-close" type="button" aria-label="Close subscribe form" data-subscribe-close>×</button><div class="cs-subscribe-copy"><p class="gcs-kicker">Weekly price watch</p><h2 id="cs-subscribe-title">Know when GLP-1 prices move.</h2><p>One useful comparison each week. No spam, no sold lists, and you can unsubscribe anytime.</p></div><form class="cs-subscribe-form" data-subscribe-form novalidate><label for="cs-subscribe-email">Email address</label><div class="cs-subscribe-fields"><input id="cs-subscribe-email" name="email" type="email" autocomplete="email" inputmode="email" required placeholder="you@example.com"><button class="cs-btn cs-palette-action" type="submit">Sign up</button></div><p class="cs-subscribe-consent-note">By signing up, you agree to receive Clinic Scout price and comparison updates under our <a href="/privacy/">Privacy Policy</a>. Unsubscribe anytime.</p><input name="company" type="text" tabindex="-1" autocomplete="off" aria-hidden="true"><input name="consent_version" type="hidden" value="glp-newsletter-v1-20260822"><input name="consent_source" type="hidden" value="/subscribe-bottom-sheet/"><p class="cs-subscribe-status" role="status" aria-live="polite"></p></form></section></div>`;
  // Footer: 02-shell's .cs-footer-grid grammar. Every destination and every
  // legal/disclosure string from the previous footer is preserved verbatim —
  // the entity stamp is the exact SITE-BUILD-SOP:64 string, character for
  // character.
  const footer = `<footer class="gcs-footer"><div class="cs-container"><div class="cs-footer-grid"><section class="gcs-footer-brand"><p class="gcs-footer-wordmark">Clinic <span class="cs-grad-words">Scout</span></p><p>Evidence before enrollment. Source gaps stay visible.</p><p class="cs-footer-strength">Every price on this site shows the date we verified it and links to its source.</p><a class="cs-footer-badge" href="/glp-1-price-index/"><b>Price Index</b> Every price dated</a></section><section><h4>Compare</h4><ul><li><a href="/providers/">Provider directory</a></li><li><a href="/self-pay/">Self-pay comparison</a></li><li><a href="/tools/glp-1-cost-calculator/">Cost calculator</a></li><li><a href="/quiz/provider-fit/">Provider fit</a></li><li><button class="cs-footer-linkbtn" type="button" data-subscribe-open>Price alerts newsletter</button></li></ul></section><section><h4>Evidence</h4><ul><li><a href="/how-we-rate/">How we rate</a></li><li><a href="/editorial-guidelines/">Editorial guidelines</a></li><li><a href="/review-standards/">Review standards</a></li></ul></section><section><h4>Company</h4><ul><li><a href="/authors/izaiah-tilton/">Publisher</a></li><li><a href="/how-we-make-money/">How we make money</a></li><li><a href="/privacy/lead-capture/">Contact and corrections</a></li></ul></section><section><h4>Legal</h4><ul><li><a href="/privacy/">Privacy</a></li><li><a href="/terms/">Terms</a></li><li><a href="/privacy/cookies/">Cookie choices</a></li><li><a href="/privacy/#consumer-health-data">Consumer health data</a></li><li><a href="/how-we-make-money/">Affiliate disclosure</a></li></ul></section></div><div class="cs-footer-legal"><p class="cs-footer-disclosure">Clinic Scout is an independent research and comparison tool, not a medical provider. Nothing on this site is medical advice, and no comparison can determine whether a medication is right for you — that decision belongs with you and a licensed clinician. Compounded GLP-1 medications are not FDA approved. Information is checked against providers' published pages on the dates shown and may change after we record it.</p><p class="cs-footer-disclosure">Clinic Scout may earn commissions from provider links. Check the ordering method stated for each comparison. Payment is not evidence of quality, safety or medical suitability.</p><p>Entity: Tilton Group LLC · California entity B20260317552 · Principal and mailing address: 2108 N St, Ste N, Sacramento, CA 95816.</p></div></div></footer>`;
  return upgradeReviewScore(html)
    .replace(/<style\b(?![^>]*data-preserve)[^>]*>[\s\S]*?<\/style>/gi, "")
    .replaceAll("The next five steps may ask a broad BMI range, medication status, goal, insurance expectation, and state.", "The next six steps may ask a broad BMI range, medication status, goal, insurance expectation, state, and timing.")
    .replaceAll(">Send the checklist</button>", ">Save request</button>")
    .replaceAll('class="eyebrow"', 'class="gcs-kicker"')
    .replaceAll('class="stage"', 'class="gcs-kicker"')
    .replaceAll('/assets/quiz-page.js"', `/assets/quiz-page.js?v=${ASSET_VERSION}"`)
    .replaceAll('/assets/matches-customized.js"', `/assets/matches-customized.js?v=${ASSET_VERSION}"`)
    .replaceAll('/assets/review-intake.js"', `/assets/review-intake.js?v=${ASSET_VERSION}"`)
    .replace(/<html([^>]*)>/i, `<html$1 data-cs-template="${template}">`)
    .replace(/<main(?![^>]*\bid=)([^>]*)>/i, '<main id="main" tabindex="-1"$1>')
    .replace(/<main([^>]*)>/i, `<main$1 data-cs-template="${template}">`)
    .replace("</head>", `${performanceConfig}</head>`)
    .replace(/<body([^>]*)>/i, `<body$1 data-cs-template="${template}"><a class="skip-link" href="#main">Skip to main content</a>${utility}${nav}${drawer}`)
    .replace("</body>", `${preFooter}${footer}${subscribeSheet}</body>`);
}

function optimizePaidLandingPage(html) {
  const withoutNonessentialChrome = html
    .replace(/<link rel="icon" type="image\/png" sizes="32x32"[^>]*>\n?/i, "")
    .replace(/<link rel="icon" type="image\/png" sizes="512x512"[^>]*>\n?/i, "")
    .replace(/<link rel="apple-touch-icon"[^>]*>\n?/i, "")
    .replace(/<script src="\/assets\/vendor\/gsap\/(?:gsap|ScrollTrigger|SplitText|Flip)\.min\.js" defer><\/script>/gi, "")
    .replace(/<script src="\/assets\/vendor\/lenis\/lenis\.min\.js" defer><\/script>/gi, "")
    .replace(/<script src="\/assets\/motion\.js\?v=[^"]+" defer><\/script>/gi, "")
    .replace(/<button\b[^>]*data-subscribe-open[^>]*>[\s\S]*?<\/button>/gi, "")
    .replace(/<div class="cs-subscribe-layer"[\s\S]*?<\/div><\/body>/i, "</body>");

  // Paid comparison routes have a strict 15-request / 300 KB transfer budget.
  // Keep the route-specific original artwork visible, but request only its 640px
  // AVIF on this noindex surface. The 1280px original remains declared as image
  // lineage without becoming another browser request.
  let optimized = withoutNonessentialChrome.replace(
    /<picture class="p3-product-visual" data-product-visual="([^"]+)">[\s\S]*?<img\b[^>]*\balt="([^"]+)"[^>]*><\/picture>/i,
    (_match, stem, alt) => `<picture class="p3-product-visual" data-product-visual="${stem}" data-full-src="/assets/images/product-comparison-pass2/${stem}-1280.avif"><img src="/assets/images/product-comparison-pass2/${stem}-640.avif" width="640" height="360" loading="eager" fetchpriority="high" decoding="async" alt="${alt}"></picture>`,
  );

  if (/\bdata-phase3-route="\/lp\/best-providers\//i.test(optimized)) {
    // Phase 3's paid comparisons need the shared shell plus the Phase 3 layer,
    // not the 12-layer fallback bundle. The editorial bundle already contains
    // that exact combination. Related reading still keeps visible original art,
    // but one 800px AVIF per lazy card is enough on this noindex surface; its
    // 1600px lineage remains declared without inflating the request budget.
    optimized = optimized
      .replace("/assets/site.css?v=", "/assets/site-editorial.css?v=")
      .replace(
        /<picture class="p3-story-picture">[\s\S]*?<img src="([^"]+)-1600\.jpg"[^>]*\balt="([^"]+)"[^>]*><\/picture>/gi,
        (_match, stem, alt) => `<picture class="p3-story-picture" data-full-src="${stem}-1600.avif"><img src="${stem}-800.avif" width="800" height="450" loading="lazy" decoding="async" alt="${alt}"></picture>`,
      );
  }

  return optimized;
}

function directCalculatorWorkspace(html) {
  // DESIGN PASS 2026-08-26 (calculator-tools spec fix #2, supersedes the O-26
  // "strip the hero" transform): the renderer now emits the competitor's
  // split tool-page geometry itself — visible H1 + published-price anchor on
  // the left, the form card on the right (our palette, light-card language).
  // The pass2 job here is no longer hero removal; it is keeping that whole
  // split inside one laptop viewport (their single-viewport hero) and leaving
  // mobile room below the form so the consent toast cannot sit on Calculate.
  if (!html.includes('<div class="calc-layout"')) throw new Error("calculator_workspace_missing");
  const compact = `<style data-preserve data-pass2-calculator>body[data-cs-template="calculator"]>main{padding-top:0!important}.cost-workspace{display:grid;align-items:center;min-height:calc(100svh - 76px);padding-block:16px!important}.cost-form{gap:12px!important;padding:clamp(18px,2.4vw,28px)!important}.cost-form :is(input,select){min-height:44px!important}.cost-grid{gap:10px!important}.cost-hint{font-size:.74rem!important;line-height:1.25!important}@media(max-width:860px){.cost-workspace{display:block;min-height:0;padding-block:12px 110px!important}}</style>`;
  return html.replace("</head>", `${compact}</head>`);
}

function addMoneyHero(html) {
  return html.replace(/<main([^>]*)>/, `<main$1><div class="page-band" aria-hidden="true"></div>`);
}

function addPriceIndexMoneyLink(html) {
  const panel = `<aside class="panel evidence-line price-index-entry" aria-label="Weekly GLP-1 price index"><p class="gcs-kicker">Weekly price benchmark</p><h2>See the market before you compare monthly costs.</h2><p>The GLP-1 Price Index publishes the current median and range for eligible compounded semaglutide and tirzepatide monthly prices, with first-party sources and no backfilled history.</p><a class="gcs-text-link" href="${PRICE_INDEX_ROUTE}">Open the weekly GLP-1 Price Index</a></aside>`;
  if (!html.includes("</main>")) throw new Error("price_index_money_link_insertion_point_missing");
  return html.replace("</main>", `${panel}</main>`);
}

function routeSlug(route) {
  return String(route).replace(/^\//, "").replace(/\/$/, "").split("/").at(-1);
}

function pageHero(html, key, alt) {
  return html
    .replaceAll("editorial-evidence-hero", key)
    .replaceAll("money-page-hero", key)
    .replace(
      /(<picture[^>]*>[\s\S]*?<img[^>]*\balt=")[^"]*(")/i,
      (_match, prefix, suffix) => `${prefix}${escHtml(alt)}${suffix}`,
    );
}

function standaloneHero(html) {
  return html.replace(/<main([^>]*)>/i, `<main$1><div class="page-band" aria-hidden="true"></div>`);
}

const TRUST_VISUALS = {
  "authors/izaiah-tilton/index.html": { key: "author-izaiah-tilton", title: "Izaiah Tilton · Clinic Scout publisher", alt: "A tidy research desk" },
  "authors/clinic-scout-editorial-team/index.html": { key: "author-editorial-team", title: "Clinic Scout editorial team", alt: "The Clinic Scout research team desk" },
  "how-we-rate/index.html": { key: "trust-how-we-rate", title: "How Clinic Scout rates programs", alt: "How our scoring works" },
  "review-standards/index.html": { key: "trust-review-standards", title: "Clinic Scout review standards", alt: "Checking sources before publishing" },
  "how-we-make-money/index.html": { key: "trust-how-we-make-money", title: "How Clinic Scout makes money", alt: "How Clinic Scout stays independent" },
  "editorial-guidelines/index.html": { key: "trust-editorial-guidelines", title: "Clinic Scout editorial guidelines", alt: "Fact-checking notes" },
};

function responsiveHero(html, slug, sizes = "(max-width: 768px) calc(100vw - 24px), 540px") {
  return html
    .replace(`srcset="/assets/images/${slug}-1600.avif"`, `srcset="/assets/images/${slug}-800.avif 800w, /assets/images/${slug}-1600.avif 1600w" sizes="${sizes}"`)
    .replace(`srcset="/assets/images/${slug}-1600.webp"`, `srcset="/assets/images/${slug}-800.webp 800w, /assets/images/${slug}-1600.webp 1600w" sizes="${sizes}"`)
    .replace(`src="/assets/images/${slug}-1600.jpg"`, `src="/assets/images/${slug}-1600.jpg" srcset="/assets/images/${slug}-800.jpg 800w, /assets/images/${slug}-1600.jpg 1600w" sizes="${sizes}"`);
}

function addOg(html, slug, title) {
  const image = `https://glpclinicscout.com/assets/images/${slug}-og.jpg`;
  const homeDescription = "Independent, source-bound research for comparing GLP-1 programs, prices, policies, and evidence gaps.";
  const homeSchema = { "@context": "https://schema.org", "@graph": [
    { "@type": "Organization", "@id": "https://glpclinicscout.com/#organization", name: "Clinic Scout", url: "https://glpclinicscout.com/" },
    { "@type": "WebSite", "@id": "https://glpclinicscout.com/#website", url: "https://glpclinicscout.com/", name: "Clinic Scout", publisher: { "@id": "https://glpclinicscout.com/#organization" } },
  ] };
  const homepage = title.startsWith("Clinic Scout ·")
    ? `<meta name="description" content="${homeDescription}"><link rel="canonical" href="https://glpclinicscout.com/"><meta property="og:description" content="${homeDescription}"><script type="application/ld+json">${JSON.stringify(homeSchema)}</script>`
    : "";
  const meta = `${homepage}<meta property="og:type" content="website"><meta property="og:title" content="${title.replaceAll('"', '&quot;')}"><meta property="og:image" content="${image}"><meta name="twitter:card" content="summary_large_image"><meta name="twitter:image" content="${image}">`;
  return html.replace("</head>", `${meta}</head>`);
}

function addCuration(html, curation) {
  const esc = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
  const featured = curation.featured.map((item) => `<li><strong>${esc(item.program.display_name)}</strong> · economics/EPC score ${esc(item.score)}</li>`).join("");
  const seeAll = curation.see_all.map((item) => `<li>${esc(item.program.display_name)} — ${item.eligible ? "eligible but below top-K" : "held until economics, EPC sample, recurring terms, compliance, deposit, and live gates pass"}</li>`).join("");
  const section = `<section class="panel curation" data-curation-state="${esc(curation.status)}"><p class="eyebrow">Economics × EPC × recurring flag</p><h2>Featured top-K: ${curation.featured.length ? `${curation.featured.length} evidence-qualified program(s)` : "held — no program has a valid sample"}</h2><p>Formula: ${esc(curation.formula)}. A minimum of ${esc(curation.minimum_clicks)} valid clicks and actual-deposit reconciliation are required; single-digit-click noise cannot change placement.</p>${featured ? `<ol>${featured}</ol>` : "<p>No verdict-box or primary-table placement is economically authorized.</p>"}<details><summary>See all ${curation.see_all.length} staged options</summary><ul>${seeAll}</ul></details></section>`;
  return html.replace("</section><p>Clinic Scout compares", `</section>${section}<p>Clinic Scout compares`);
}

const analytics = `(()=>{const allowed={affiliate_click:['program','merchant','page_slug','link_position','variant'],comparison_table_view:['page_slug','table_id'],quiz_start:['page_slug'],quiz_step_complete:['step_number'],quiz_complete:['result_count'],provider_finder_complete:['page_slug','result_count'],scroll_75:['page_slug']};function emit(name,params){const c=window.GCS_ANALYTICS||{};if(!c.enabled||c.consent!=='granted'||!allowed[name])return false;const clean={};for(const k of allowed[name])if(params[k]!==undefined)clean[k]=params[k];window.gtag?.('event',name,clean);return true}window.GCSAnalytics={event:emit};window.addEventListener('gcs:analytics',e=>emit(e.detail?.name,e.detail?.params||{}));document.addEventListener('click',e=>{const a=e.target.closest('[data-analytics-event="affiliate_click"]');if(!a)return;emit('affiliate_click',{program:a.dataset.program,merchant:a.dataset.merchant,page_slug:a.dataset.pageSlug,link_position:a.dataset.linkPosition,variant:a.dataset.variant||'a'})});let sent=false;addEventListener('scroll',()=>{if(!sent&&(scrollY+innerHeight)/document.documentElement.scrollHeight>=.75){sent=true;emit('scroll_75',{page_slug:location.pathname})}},{passive:true});const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){emit('comparison_table_view',{page_slug:location.pathname,table_id:e.target.dataset.tableId});io.unobserve(e.target)}}),{threshold:.25});document.querySelectorAll('[data-analytics-event="comparison_table_view"]').forEach(x=>io.observe(x));})();`;

// brandSvg() removed 2026-08-21 (renderer:shell): the generated navy mark is
// superseded by the authored aurora-aperture brand set in assets/brand/,
// mirrored into public/assets/brand/ by the copy step below.

function trustPages(identity, articles = [], authorRegistry = { authors: [] }) {
  const authoredPages = (authorId) => {
    const pages = articles.filter((article) => (article.author?.author_id || article.author?.id) === authorId);
    return `<section class="authored-pages" data-authored-pages="${escHtml(authorId)}"><h2>Published pages</h2><p>This index is generated from the byline recorded in Clinic Scout's article registry.</p><ol>${pages.map((article) => `<li><a href="${escHtml(article.canonical_path)}">${escHtml(article.title || article.h1)}</a></li>`).join("")}</ol></section>`;
  };
  const authorJsonLd = (authorId) => {
    const author = authorRegistry.authors.find((item) => item.author_id === authorId);
    if (!author || !["Person", "Organization"].includes(author.entity_type)) throw new Error(`author_schema_registry_missing:${authorId}`);
    const schema = {
      "@context": "https://schema.org",
      "@type": author.entity_type,
      "@id": `https://glpclinicscout.com${author.bio_path}#author`,
      name: author.display_name,
      url: `https://glpclinicscout.com${author.bio_path}`,
      description: author.transparency_note,
      ...(author.entity_type === "Person" && author.role ? { jobTitle: author.role } : {}),
      ...(Array.isArray(author.same_as) && author.same_as.length ? { sameAs: author.same_as } : {}),
    };
    return `<script type="application/ld+json">${JSON.stringify(schema)}</script>`;
  };
  return {
    "how-we-rate/index.html": framePage("How Clinic Scout rates programs", `<p class="gcs-kicker">Our methodology</p><h1>Rankings come from evidence, never commission.</h1><div class="grid"><section class="panel"><h2>30% · Price clarity</h2><p>Does the provider publish the real monthly cost — including what it covers and when it renews?</p></section><section class="panel"><h2>25% · Care process</h2><p>How clearly the provider explains its visits, prescriptions, follow-ups, and support.</p></section><section class="panel"><h2>15% · Insurance help</h2><p>Whether the provider helps you use insurance or get reimbursed.</p></section><section class="panel"><h2>15% · Customer reviews</h2><p>What real customers report on independent review sites.</p></section><section class="panel"><h2>15% · Shipping and delivery</h2><p>Where the provider ships, how fast, and from which pharmacy.</p></section></div><p>When something is not published, we say so instead of guessing. Commissions are never part of the score.</p><aside class="trust-block"><div><h2>Methodology status</h2><p>Last updated July 30, 2026 · five published criteria · unsupported dimensions omitted rather than zeroed.</p></div><div><h2>Accountability</h2><p>Izaiah Tilton is the named publisher responsible for maintaining this commercial scoring method. No clinical credential is claimed.</p></div></aside>`),
    "how-we-make-money/index.html": framePage("How Clinic Scout makes money", `<h1>Clear incentives. Explained ordering.</h1><p>Clinic Scout may earn affiliate commission when a visitor voluntarily follows an eligible partner link. Some future provider-matching agreements may pay per qualified lead after explicit sharing consent. Clinic Scout may earn commissions from provider links. Check the ordering method stated for each comparison. Payment is not evidence of quality, safety or medical suitability. Quiz matches use goal-weighted evidence scores and price tie-breaks. Scored comparisons use their published rubric; provider directories and treatment lists use their stated price, name or selected sort. Affiliate earnings must not be used to promote a provider within these comparisons.</p><p>Affiliate links, email sends, provider sharing, and production measurement are enabled only when their separate consent, policy, and production requirements are met.</p>`),
    "editorial-guidelines/index.html": framePage("Clinic Scout editorial guidelines", `<h1>Evidence review without fake authority.</h1><p>Articles use a verified named publisher or the accountable ${identity.author.display_name} organizational byline. Neither claims clinical credentials.</p><h2>Risk tiers</h2><ul><li><strong>LOW:</strong> commercial and comparison content requires source checking.</li><li><strong>MEDIUM:</strong> health information requires inline primary citations and a recorded evidence review against FDA labeling, PubMed, or equivalent sources.</li><li><strong>HIGH:</strong> individualized treatment, diagnosis, dosing, or outcome claims are blocked or reframed.</li></ul><p>We use software to help organize research and catch errors, but every claim must trace to a real, dated source. We never invent authors, clinicians, credentials, patients, testimonials, or results.</p>`),
    "authors/clinic-scout-editorial-team/index.html": framePage("Clinic Scout Editorial Team", `<section class="gcs-home-hero"><div><p class="gcs-kicker">Organizational byline</p><h1>Clinic Scout Editorial Team</h1><p class="lede">The Clinic Scout Editorial Team is the research desk responsible for source maintenance, commercial comparisons, corrections, and consumer verification guides published by Tilton Group LLC.</p></div><div class="panel evidence-line author-identity-visual"><img src="/assets/brand/favicon.svg" alt="Clinic Scout editorial mark" width="96" height="96"><h2>What this byline means</h2><p>This is an organizational identity, not a fictional person. It is used when an article reflects Clinic Scout's shared research process rather than one writer's personal experience.</p></div></section><section><h2>What the team is responsible for</h2><p>The team checks whether a page's claims match the sources listed on that page. For provider comparisons, that means reviewing published prices, subscription terms, insurance-support language, shipping information, cancellation terms, and other details a reader may need before enrolling. For health-information guides, the team gives priority to FDA labeling, federal agency material, peer-reviewed literature, and other primary sources already mapped to the article.</p><p>Sources have dates because provider terms and regulatory information change. A page should say when the evidence was checked, distinguish a published fact from an editorial interpretation, and leave a gap visible when the source does not answer the question.</p></section><section class="grid"><article class="panel evidence-line"><h2>How review works</h2><p>Every guide has a risk tier. Commercial comparisons receive a current-source check. Health-information pages require primary-source mapping and a recorded evidence review. Individualized diagnosis, dosing, treatment selection, and outcome predictions are outside the publication's scope.</p><p>Software helps us organize sources and catch broken links — but people decide what publishes, and every page passes the same source and accuracy checks.</p></article><article class="panel evidence-line"><h2>What the team does not claim</h2><p>The team is not a physician, pharmacy, telehealth clinic, insurer, or medical-review board. This byline does not imply prescribing authority, diagnosis, individualized medical advice, or a guarantee about eligibility, price, coverage, availability, quality, or results.</p><p>Readers should use Clinic Scout to inspect published evidence and prepare questions. Treatment decisions belong with a licensed clinician who knows the reader's medical history.</p></article></section><section><h2>Commercial independence and corrections</h2><p>Clinic Scout may earn a commission from eligible partner links. Commission is excluded from the scoring rubric and cannot change a source, score, winner, or medical statement. The same published criteria apply to partners and non-partners.</p><p>If a provider changes a term or a cited source no longer supports a statement, the page should be corrected rather than defended. Read the <a href="/editorial-guidelines/">editorial guidelines</a>, the <a href="/review-standards/">review standards</a>, <a href="/how-we-rate/">how Clinic Scout rates programs</a>, and <a href="/how-we-make-money/">how the publication makes money</a>.</p></section>${authoredPages("clinic-scout-editorial-team")}${authorJsonLd("clinic-scout-editorial-team")}`),
    "authors/izaiah-tilton/index.html": framePage("Izaiah Tilton, Publisher", `<section class="gcs-home-hero"><div><p class="gcs-kicker">Publisher profile</p><h1>Izaiah Tilton</h1><p class="lede">Izaiah Tilton is the named publisher accountable for Clinic Scout's source comparison, commercial-term analysis, and consumer verification systems through Tilton Group LLC.</p></div><div class="panel evidence-line author-identity-visual"><img src="/assets/authors/izaiah-tilton-publisher-mark.png" alt="Non-photographic publisher identity mark for Izaiah Tilton" width="640" height="640"><p class="image-caption">Publisher identity mark · non-photographic · based on public profile provenance</p><h2>Accountability</h2><p>Izaiah is responsible for the publication's methodology, commercial disclosures, correction process, and the decision to publish or remove a page. That responsibility does not make him a clinician.</p></div></section><section><h2>Why Clinic Scout exists</h2><p>Health-program shopping pages often compress a complicated decision into one price, one badge, or one ranking. Clinic Scout takes the opposite approach. It records what a provider publishes, dates the source, shows how a score was calculated, and leaves missing evidence visible. The goal is not to choose treatment for the reader. It is to make the commercial and verification questions easier to see before money or personal information changes hands.</p><p>Izaiah's work on the site focuses on research systems: comparing subscription terms, tracing claims back to first-party or primary sources, separating registry identity from proof of current service, and checking that editorial pages do not overstate what their evidence establishes.</p></section><section class="grid"><article class="panel evidence-line"><h2>Publishing responsibilities</h2><p>Izaiah maintains the scoring rules used across provider pages and is accountable for applying the same criteria to every program. He also oversees source freshness, affiliate disclosures, privacy boundaries, and corrections when published terms change.</p><p>Provider commission is not a scoring input. An eligible affiliate relationship may determine whether a transparent partner link appears, but it may not change the wording of a claim, the published criteria, or the score produced by those criteria.</p></article><article class="panel evidence-line"><h2>Limits of authority</h2><p>Izaiah does not claim a medical degree, prescribing authority, diagnostic expertise, or the ability to recommend an individual treatment. Clinic Scout does not know a reader's medical history and does not replace a licensed clinician, pharmacist, insurer, or emergency service.</p><p>Health-information pages therefore stay within the boundaries of their cited evidence. When a source cannot support a clinical conclusion, the page should say so instead of filling the gap with an estimate.</p></article></section><section><h2>How the work is reviewed</h2><p>Each article is assigned a risk tier. Commercial pages receive current-source checks. Health-information pages require primary-source mapping and an evidence-review record. High-risk individualized claims are blocked or rewritten as general verification guidance. Automated tools may assist with structure and quality checks, but they do not count as clinical review and do not replace source verification.</p><p>Readers can inspect the rules in the <a href="/editorial-guidelines/">editorial guidelines</a>, <a href="/review-standards/">review standards</a>, and <a href="/how-we-rate/">rating methodology</a>. Commercial relationships are explained on <a href="/how-we-make-money/">How Clinic Scout makes money</a>. Izaiah's public identity reference is also available on <a href="https://github.com/izaiahat" rel="me noopener">GitHub</a>.</p></section>${authoredPages("izaiah-tilton")}${authorJsonLd("izaiah-tilton")}`),
    "review-standards/index.html": framePage("Clinic Scout review standards", `<h1>How Clinic Scout reviews evidence.</h1><p>Clinic Scout reviews consumer research for source accuracy, commercial clarity, and scope before publication. The process is designed to show what a source establishes, what remains unknown, and who is accountable for the page. It is not a clinical-review badge and does not turn a publisher, editor, or automated check into a healthcare professional.</p><h2>1. Start with the exact reader question</h2><p>Each page is assigned one primary question and a clear route. A provider comparison may examine published price, included services, insurance-support language, shipping, renewal, and cancellation terms. A health-information guide may summarize a product label, regulator warning, coverage rule, or verification workflow. The page must not quietly expand into individualized diagnosis, prescribing, dosing, treatment selection, or outcome prediction.</p><h2>2. Map factual claims to dated sources</h2><p>Claims are bound to first-party provider terms, FDA or other government material, peer-reviewed literature, official plan documents, or another source appropriate to the statement. Dates remain visible because prices, eligibility, warnings, and policies change. A source that identifies a business or product does not automatically prove quality, availability, coverage, suitability, or results. Unsupported facts stay missing rather than becoming editorial estimates.</p><h2>3. Apply the risk tier</h2><p>LOW-risk commercial pages receive current-source and consistency checks. MEDIUM-risk health-information pages require primary-source mapping and a recorded evidence review against the cited material. HIGH-risk individualized claims are blocked or rewritten as general verification questions. A page may publish without clinician review only when it does not claim that such review occurred and remains inside the evidence and scope boundary.</p><h2>4. Check the complete reader-facing page</h2><p>Review includes the headline, quick answer, headings, tables, labels, scoring inputs, disclosures, calls to action, related links, source list, metadata, structured data, mobile layout, and keyboard-visible controls. Repeated caveats, machine-facing rubric language, empty score fields, and unsupported promotional claims are defects even when the underlying data file is technically valid. Generated pages are inspected after the build because a safe source record can still render confusingly.</p><h2>5. Keep automation subordinate to evidence</h2><p>Automated tools may organize source records, run consistency checks, detect broken links, compare generated routes, or flag likely copy drift. They do not create clinical authority, approve treatment claims, or substitute for source reading. The named publisher remains responsible for deciding whether the page's evidence, wording, disclosures, and correction path are fit to publish.</p><h2>6. Correct rather than defend stale work</h2><p>When a cited term changes, a heading no longer matches its body, or a claim outgrows its source, Clinic Scout should correct, qualify, or remove it. Material corrections preserve the evidence trail and update the visible date. Readers and providers may submit a documented correction through the published contact route. Commercial relationships cannot veto a correction or change the scoring method.</p><aside class="trust-block"><div><h2>Named accountability</h2><p>Izaiah Tilton is the publisher responsible for this process through Tilton Group LLC. He does not claim medical credentials.</p></div><div><h2>Clinical review label</h2><p>Independent clinician review is shown only after a named qualified reviewer has actually reviewed the page. No unnamed or automated reviewer is presented as a clinician.</p></div></aside><p>Last updated July 30, 2026. Related standards: <a href="/editorial-guidelines/">editorial guidelines</a>, <a href="/how-we-rate/">rating methodology</a>, and <a href="/how-we-make-money/">commercial disclosures</a>.</p>`),
    "privacy/lead-capture/index.html": framePage("Clinic Scout lead-capture privacy notice", `<h1>Minimal data, explicit choices.</h1><p>The content-upgrade form accepts email, source page, requested checklist, consent version, and separate privacy and email consent. It rejects phone, state, BMI, weight, medications, diagnosis, medical history, and quiz-answer history.</p><p>After both required choices, Clinic Scout may email the requested checklist only when its separate transactional-email production gate is enabled. Provider sharing and lead sale are not active.</p>`),
    "privacy/index.html": framePage("Clinic Scout Privacy Policy", `<p>LEGAL DRAFT · COUNSEL REVIEW REQUIRED BEFORE LIVE QUIZ OR LEAD GENERATION</p><h1>Privacy Policy</h1><p><strong>Effective draft:</strong> July 23, 2026. Clinic Scout minimizes collection by default. Necessary browser storage supports site function. Optional analytics and Microsoft Clarity remain off until affirmative cookie consent.</p><h2>Health-related quiz information</h2><p>The quiz may ask broad BMI range, medication status, goals, insurance expectations, and state. Opt-in consent is required before those questions appear. Those answers remain in the browser, are not persisted, are not sent to analytics, and are not included in the lead record.</p><h2>Content-upgrade information</h2><p>If a visitor requests a checklist, the content-upgrade contract accepts email, source page, requested checklist, consent version, and separate privacy and email consent. No phone number, state, diagnosis, weight, medication history, or quiz-answer history is accepted. Clinic Scout may email the requested checklist only when the separate transactional-email production gate is enabled. No provider sharing, sale, call, or text is active.</p><h2>Purpose, retention, access, deletion, and correction</h2><p>Data may be used only to fulfill the requested editorial guide and, if separately chosen and legally cleared, future matching. Production retention periods, consumer-health-data rights, deletion workflow, processor list, and state notices require counsel approval before activation. Requests will be routed through the published correction/privacy contact once production identity is approved.</p><h2>Changes and contact</h2><p>Material changes require versioning, notice, and renewed consent where required.</p>`),
    "terms/index.html": framePage("Clinic Scout Terms of Service", `<p>LEGAL DRAFT · COUNSEL REVIEW REQUIRED BEFORE GO-LIVE</p><h1>Terms of Service</h1><p>Clinic Scout provides consumer research and comparison tools, not medical advice, diagnosis, treatment, prescribing, or a guarantee of eligibility, price, coverage, availability, quality, or outcome.</p><h2>Provider and NPI information</h2><p>Public NPI data confirms registry identity, taxonomy, and listed location facts only. It does not prove current GLP-1 services or endorsement. Visitors must verify services directly.</p><h2>Commercial relationships</h2><p>Some future links may generate affiliate commission and some future matching arrangements may generate compensation. Commission may not determine claims, evidence scoring, or clinical framing. Every redirect, lead share, email send, and payment arrangement remains separately gated.</p><h2>Acceptable use and limitations</h2><p>Do not misuse forms, automate fraudulent clicks, bypass consent, submit another person's health data, or treat this site as emergency or clinical care. Production dispute, governing-law, warranty, liability, and arbitration language requires named counsel approval before release.</p>`),
    "privacy/cookies/index.html": framePage("Clinic Scout Cookie and Tracking Choices", `<h1>Cookie and tracking choices</h1><p>Necessary local storage records the consent choice. Optional GA4 and Microsoft Clarity stay disabled until affirmative analytics consent and separate production requirements are met. Health quiz answers and lead-form values are prohibited analytics parameters.</p><h2>Controls</h2><p>Rejecting optional tracking does not block comparison content or the quiz. The choice can be reset by clearing site storage and cookies. No advertising pixel is configured in this staging build.</p>`),
  };
}

import { loadCarrierArticles, assertCarrierArticleBytes } from "../src/carrier-article-retention.mjs";
async function main() {
  // Required source derivation, not a carried-forward artifact. The former
  // package build chain omitted this emitter despite its documented contract.
  execFileSync('python3', ['-B', path.join(ROOT, 'scripts/emit-tracking-allowed-hosts.py'), '--root', ROOT], { stdio: 'inherit' });
  resetTrackingAllowedHostsCache();
  retainedCarrierArticles = loadCarrierArticles(ROOT);
  const programRegistry = await readJson("program-registry.json");
  const indexNow = validateIndexNowConfig(await readJson("indexnow.json"));
  // guides/tirzepatide-cost/index.html was carried here as preserved bytes because the
  // page had no content-batch entry to rebuild it from. Its entry was restored on
  // 2026-08-06, so the route is now rendered from data/blog/content-batch.json like
  // every other guide and the publish chain can edit it again.
  const preservedReviewBytes = new Map();
  for (const program of programRegistry.programs.filter((item) => item.source_kind === "legacy_vertical")) {
    const relative = path.join(program.review_path.replace(/^\//, ""), "index.html");
    if (PASS2_MIGRATED_LEGACY_REVIEW_FILES.has(relative)) continue;
    const bytes = await readDurablePredecessorFile(path.join(PUBLIC, relative), path.join("reviews", relative));
    preservedReviewBytes.set(relative, bytes);
  }
  // Build 4 private/index artifacts are predecessor-owned. Build 6 changes the
  // shared asset version for facet controls, but it must not remint or rewrite the
  // price-index or deal-evaluation records. Preserve those bytes and prove the
  // newly evaluated semantics still match before carrying them forward.
  const priceIndexPredecessor = await readPriceIndexPredecessor({
    htmlPath: path.join(PUBLIC, PRICE_INDEX_ROUTE_FILE),
    manifestPath: path.join(OPS, "price-index-manifest.json"),
    snapshotRoot: BUILD4_PRESERVATION,
  });
  const preservedPriceIndexHtml = priceIndexPredecessor.html;
  const preservedPriceIndexManifestBytes = priceIndexPredecessor.manifestBytes;
  const preservedPriceIndexManifest = priceIndexPredecessor.manifest;
  const preservedDealManifestBytes = await readDurablePredecessorFile(path.join(OPS, "wave4-deal-ingestion-manifest.json"), "ops/wave4-deal-ingestion-manifest.json");
  const preservedDealManifest = JSON.parse(preservedDealManifestBytes.toString("utf8"));
  // R2 target release: the refreshed catalog row now owns the Mounjaro coupon bytes.
  // OAuth verification files and their security headers are immutable carrier
  // bytes. Snapshot them from the actual predecessor tree before clearing the
  // candidate, rather than reaching back to a retired historical release.
  let preservedHeaders = (await readDurablePredecessorFile(path.join(PUBLIC, "_headers"), "_headers")).toString("utf8").split("\n# Wave 4 price tracker:")[0].trimEnd();
  // The predecessor header is carried forward as source truth, but analytics
  // transport origins are data-driven and must not remain stale forever. Reconcile
  // both CSP directives before the clear so the rebuilt public/_headers is wired.
  const posthogTransport = JSON.parse(await readFile(path.join(DATA, "posthog-config.json"), "utf8"));
  for (const origin of posthogTransport.transport_origins?.origins || []) {
    for (const directive of ["script-src", "connect-src"]) {
      const directivePattern = new RegExp(`(${directive} [^;]+)`);
      const current = preservedHeaders.match(directivePattern)?.[1] || "";
      if (!current) throw new Error(`csp_directive_missing:${directive}`);
      if (!current.split(/\s+/).includes(origin)) preservedHeaders = preservedHeaders.replace(directivePattern, `$1 ${origin}`);
    }
  }
  const preservedGoogleAdsApiBytes = await readDurablePredecessorTree(path.join(PUBLIC, "google-ads-api"), "google-ads-api");
  await Promise.all([
    rm(PUBLIC, { recursive: true, force: true }),
    rm(OPS, { recursive: true, force: true }),
  ]);
  const [identity, eligibility, contentLedger, moneySpecs, quizConfig, npi, visual, emailSequences, resendMachine, keywordMap, blogBatch, nationalContent, authorRegistry, activationSelection, costCalculatorConfig, providerPageContracts, providerFastpath, providerPricingMatrix, medicationDirectPricing, couponFeed] = await Promise.all([
    readJson("identity-registry.json"), readJson("program-page-eligibility.json"), readJson("program-content-ledger.json"), readJson("money-page-specs.json"), readJson("quiz-config.json"), readJson("directory/npi-city-batch.json"), readJson("visual-system.json"), readJson("email-system.json"), readJson("resend-email-machine.json"), readJson("blog/keyword-ownership-map.json"), readJson("blog/content-batch.json"), readJson("blog/content-national-wow-additions.json"), readJson("blog/author-registry.json"), readJson("affiliate-activation-selection.json"), readJson("cost-calculator.json"), readJson("provider-page-contracts.json"), readJson("provider-fastpath-contract.json"), readJson("provider-pricing-matrix.json"), readJson("medication-direct-pricing.json"), readJson("coupon-offers.json"),
  ]);
  const [phase3GraphConfig, phase3PharmacyInput] = await Promise.all([readJson("phase3-page-graph.json"), readJson("phase3-pharmacy-render-input.json")]);
  reconcileTherapeuticTaxonomyWireIn({
    registry: programRegistry,
    profileId: SITE_PROFILE,
    pages: moneySpecs.pages,
    quizConfig,
  });
  // OPERATOR MODEL 2026-08-13: fail the build loudly when any live-eligible
  // provider is missing from a surface without a documented
  // surface_restriction_basis, or when a conversion pool (quiz/calculator)
  // offers a program that is not live-eligible. This is the machine wiring for
  // "every new provider deploys to ALL surfaces" — not a one-time audit.
  assertLiveSurfaceParity(programRegistry, SITE_PROFILE, eligibility, contentLedger.programs);
  const [readyRegistry, ready6Contract] = await Promise.all([readJson("approved-program-registry.json"), readJson("ready6-editorial-contract.json")]);
  // 2026-08-06 coverage repair (G8): the live-program count is DERIVED, not carried
  // forward from the Katalys Sheet selection. `activationSelection.program_count` (13)
  // is the direct-activation Sheet subset and stays that. `liveProgramCount` is every
  // wired, vertical-fit, live registry program represented on the editorial surfaces.
  // Commercial/editorial coverage and affiliate-active coverage are deliberately
  // separate: a link-dark content-fit hold remains a live provider identity.
  const ledgerLiveIds = new Set(contentLedger.programs
    .filter((program) => program.live_link_allowed || program.promotion_authorized)
    .map((program) => String(program.program_id)));
  // 2026-08-06 cluster 1: the count is over MERCHANTS, not registry rows. Two
  // wire-in waves registered Shed twice (Awin `shedrx`, Katalys `katalys-1516`),
  // and every surface counted it twice. `assertDeclaredIdentityGroups` derives the
  // grouping from the display name AND the first-party domain, and throws unless
  // the registry says in writing which row is canonical - so the NEXT duplicate
  // fails the build instead of quietly becoming a 30th provider.
  const providerIdentity = assertDeclaredIdentityGroups(programRegistry, SITE_PROFILE, contentLedger.programs);
  buildDerivations.identity_groups = {
    merchants: providerIdentity.groups.size,
    aliases: Object.fromEntries([...providerIdentity.aliasIds].map((id) => [id, providerIdentity.canonicalOf.get(id)])),
  };
  const liveRegistryRows = programRegistry.programs.filter((program) => {
    const profile = program.profiles?.[SITE_PROFILE];
    return program.commercial_state === "live" && profile?.vertical_fit === true
      && profile?.wired === true && profile?.surfaces?.review === true;
  });
  const liveAliasRows = liveRegistryRows.filter((program) => providerIdentity.aliasIds.has(program.program_id));
  const liveProgramCount = liveRegistryRows.length - liveAliasRows.length;
  const liveRowCount = liveRegistryRows.length;
  const directAffiliateRegistryRows = liveRegistryRows
    .filter((program) => ledgerLiveIds.has(String(program.review_slug || program.program_id)));
  const directAffiliateAliasRows = directAffiliateRegistryRows
    .filter((program) => providerIdentity.aliasIds.has(program.program_id));
  const directAffiliateProgramCount = directAffiliateRegistryRows.length - directAffiliateAliasRows.length;
  const directAffiliateRowCount = directAffiliateRegistryRows.length;
  buildDerivations.live_provider_count = liveProgramCount;
  derivedTallies.LIVE_PROVIDER_COUNT = liveProgramCount;
  const livePriceIds=new Set(liveRegistryRows.filter(p=>!providerIdentity.aliasIds.has(p.program_id)).map(p=>String(p.review_slug || p.program_id)));
  const matrixEntries=Object.entries(providerPricingMatrix.providers);
  const pricingBaseline=resolveDeployedPricingBaseline(await readJson('pricing-coverage-baseline.json'));
  derivedTallies.PRICED_PROVIDER_COUNT=matrixEntries.filter(([id,e])=>livePriceIds.has(id)&&e.medications.length).length;
  derivedTallies.CALCULATOR_COMPARABLE_PROVIDER_COUNT=matrixEntries.filter(([id,e])=>livePriceIds.has(id)&&e.medications.some(isComparableCashRow)).length;
  derivedTallies.CURRENT_PRICE_ROW_COUNT=matrixEntries.reduce((n,[,e])=>n+e.medications.length,0);
  derivedTallies.MATRIX_PRICED_PROVIDER_COUNT=matrixEntries.filter(([,e])=>e.medications.length).length;
  derivedTallies.BASELINE_PRICE_ROW_COUNT=pricingBaseline.coverage.medication_rows;
  derivedTallies.BASELINE_PRICED_PROVIDER_COUNT=pricingBaseline.coverage.providers_with_evidenced_pricing;
  buildDerivations.price_state_tallies={...derivedTallies};
  for (const program of liveRegistryRows) {
    if(providerIdentity.aliasIds.has(program.program_id)) continue;
    const id=String(program.review_slug || program.program_id),entry=providerPricingMatrix.providers[id];
    if (!entry || entry.medications.length) continue;
    for (const route of [program.review_path || `/reviews/${id}/`,`/providers/${program.program_id}/`]) priceStateByRoute.set(route.replace(/^\//,'')+'index.html',priceStateText(entry));
  }
  // The wire-in side files count registry ROWS (one per affiliate relationship);
  // reader-facing copy counts merchants. Both are checked, against the right one.
  if (activationSelection.live_program_count !== directAffiliateRowCount) throw new Error(`affiliate_activation_live_count_desynced:${activationSelection.live_program_count}:${directAffiliateRowCount}`);
  const eligibilityPartition = eligibilityCensus(ROOT, programRegistry, providerPricingMatrix, eligibility, liveRegistryRows, directAffiliateRowCount);
  const directEligibility = eligibilityPartition.liveRows.filter((program) => program.live_link_allowed || program.promotion_authorized || program.operator_selection_receipt_sha256);
  if (directEligibility.length !== directAffiliateRowCount || directEligibility.some((program) => program.affiliate_link_mode !== "transparent_direct" || program.go_route_wired === true || program.redirector_tos_verified === true)) throw new Error("direct_live_program_registry_invalid");
  const directContentPrograms = contentLedger.programs.filter((program) => program.live_link_allowed || program.promotion_authorized);
  if (contentLedger.live_link_allowed !== true || contentLedger.promotion_authorized !== true || directContentPrograms.length !== directAffiliateRowCount || directContentPrograms.some((program) => program.affiliate_link_mode !== "transparent_direct" || program.go_route_wired === true || program.redirector_tos_verified === true)) throw new Error("direct_live_content_registry_invalid");
  if (moneySpecs.template_revision !== "money-page-p1-v2-wave0" || moneySpecs.pages.some((page) => page.template_revision !== "money-page-p1-v2-wave0")) throw new Error("money_template_revision_invalid");
  // 2026-08-06 enrichment: the deep-research fields live on the content
  // ledger; spec rows are the render surface. Merge research_* keys onto every
  // matching row so reviews and money pages can render them with citations.
  // 2026-08-06 surface unification: merit scores are DERIVED here, at build, from
  // the ledger - never carried in a hand-maintained rank array. A provider wired
  // in tonight is scored by the same function as one wired in a month ago, so if
  // it publishes more it outranks the incumbent on the next build with no edit.
  {
    const comparablePriceIds = comparablePriceProviderIds(programRegistry, SITE_PROFILE, {
      asOf: new Date(`${programRegistry.provider_dynamic_profiles.sealed_as_of}T12:00:00Z`),
    });
    const derivedFor = applyMeritScores(contentLedger.programs, { comparablePriceIds });
    for (const program of contentLedger.programs) {
      const score = meritScore(program);
      if (score === null) throw new Error(`merit_score_underivable:${program.program_id}`);
      program.score = score;
    }
    const ledgerById = new Map(contentLedger.programs.map((program) => [program.program_id, program]));
    for (const page of moneySpecs.pages) {
      for (const row of page.rows || []) {
        const ledgerEntry = ledgerById.get(row.program_id);
        if (!ledgerEntry) continue;
        for (const [key, value] of Object.entries(ledgerEntry)) {
          if (key.startsWith("research_") && value && row[key] === undefined) row[key] = value;
        }
        // Rows carry the operator-reviewed criteria when they have them; the rest
        // take the derived ones, and every row records which basis it used.
        if (!row.criteria_scores || !Number.isFinite(row.criteria_scores.price_transparency)) {
          row.criteria_scores = ledgerEntry.criteria_scores;
        }
        row.score_basis = ledgerEntry.score_basis;
        row.score = meritScore(row);
      }
    }
    buildDerivations.merit_scores_derived = derivedFor;
  }
  if (
    nationalContent.mode !== "isolated_staging_link_dark_noindex_no_deployment"
    || nationalContent.batch_gate_passed !== false
    || nationalContent.operator_release_receipt !== null
    || nationalContent.articles.length !== 28
    || nationalContent.articles.some((article) => (
      article.has_affiliate_cta !== false
      || article.comparison_required !== false
      || !(
        (article.batch_gate_passed === false && article.operator_release_receipt === null)
        || (article.batch_gate_passed === true
          && typeof article.operator_release_receipt === "string"
          && /^[a-f0-9]{64}$/.test(article.operator_release_receipt))
      )
    ))
  ) throw new Error("national_content_release_gate_invalid");
  const nationalArticles = nationalContent.articles.map((article) => ({ ...article, sections: article.sections.map((section) => ({ ...section, heading: section.heading || section.h2 })) }));
  const commercialGuideRoutes = new Set(["/guides/cheapest-glp1/", "/guides/zepbound-coupon/"]);
  const allArticles = [...blogBatch.articles, ...nationalArticles].map((article) => ({
    ...article,
    surface_taxonomy: commercialGuideRoutes.has(article.canonical_path) ? "commercial" : (article.surface_taxonomy || "medical"),
  }));
  const glpArticles = allArticles.filter((article) => articleVertical(article) === "GLP-1");
  const glpGuideArticles = glpArticles.filter((article) => article.canonical_path.startsWith("/guides/"));
  const allOwnership = [...keywordMap.entries];
  for (const article of nationalArticles) {
    const matches = allOwnership.filter((entry) => entry.url === article.canonical_path);
    if (matches.length !== 1) throw new Error(`national_keyword_ownership_match_count_invalid:${article.article_id}:${matches.length}`);
    const owner = matches[0];
    if ((owner.article_id || owner.owner) !== article.article_id || owner.primary_keyword !== article.primary_keyword || owner.risk_tier !== article.risk_tier || owner.status !== article.status) throw new Error(`national_keyword_ownership_identity_invalid:${article.article_id}`);
  }
  validateKeywordOwnership(allOwnership);
  const authorMap = new Map(authorRegistry.authors.map((author) => [author.author_id, { id: author.author_id, ...author }]));
  const publisherAuthor = authorMap.get("izaiah-tilton");
  if (!publisherAuthor || publisherAuthor.status !== "verified_human" || publisherAuthor.clinical_credentials_claimed !== false) throw new Error("verified_human_publisher_missing");
  // Operator directive 2026-07-30: bylines use the organizational Editorial Team identity;
  // the named publisher appears only on his own author page (linked from the footer).
  const editorialAuthor = authorMap.get("clinic-scout-editorial-team");
  if (!editorialAuthor) throw new Error("editorial_team_author_missing");
  const resolveArticleAuthor = () => editorialAuthor;
  const programMap = new Map(contentLedger.programs.map((item) => [item.program_id, item]));
  for (const article of allArticles) {
    const owner = allOwnership.find((item) => item.primary_keyword === article.primary_keyword && item.url === article.canonical_path);
    const comparison = resolveBlogComparisonSpec(article, contentLedger.programs);
    if (!owner) throw new Error(`blog_article_owner_missing:${article.article_id}`);
    if (owner.risk_tier !== article.risk_tier) throw new Error(`blog_article_owner_risk_mismatch:${article.article_id}`);
    const articleReview = reviewBrief(article, resolveArticleAuthor(article));
    if (articleReview.status !== "PASS") throw new Error(`blog_article_gate:${article.article_id}:${articleReview.reason}`);
    if (article.comparison_required && !comparison) throw new Error(`blog_article_comparison_missing:${article.article_id}`);
  }
  if (activationSelection.status !== "APPROVED_FOR_TRANSPARENT_DIRECT_BUILD" || activationSelection.go_route_program_count !== 0 || !Array.isArray(activationSelection.programs) || activationSelection.program_count !== activationSelection.programs.length) throw new Error("affiliate_activation_selection_invalid");
  const sheetAuthorizedIds = new Set(activationSelection.programs.map((program) => program.program_id));
  if (sheetAuthorizedIds.size !== activationSelection.program_count) throw new Error("affiliate_activation_selection_duplicate");
  // 2026-08-06 surface unification: the Katalys Sheet selection is ONE wire-in
  // authority, not the only one. A fast-path provider is authorized by its own
  // active contract row and a READY6 provider by its provider-issued tracking
  // record in the approved-program registry. Treating the Sheet as the sole
  // authority is what forced 16 live, approved providers off the money pages.
  const fastpathActiveIds = new Set((providerFastpath.providers || [])
    .filter((provider) => provider.affiliate_cta_state === "ACTIVE_PROVIDER_ISSUED_DIRECT"
      && provider.route_state === "LIVE_EDITORIAL_AFFILIATE_ACTIVE")
    .map((provider) => provider.program_id));
  const contractAuthorizedIds = new Set((providerPageContracts.providers || [])
    .filter((contract) => contract.row_contract?.promotion_authorized === true
      && contract.network_contract?.activation_blocker === null
      && typeof contract.network_contract?.tracking_url === "string"
      && fastpathActiveIds.has(contract.program_id))
    .map((contract) => contract.program_id));
  const ready6ById = new Map((readyRegistry.programs || []).map((program) => [program.registry_id, program]));
  const ready6AuthorizedIds = new Set((ready6Contract.programs || [])
    .filter((program) => {
      const tracking = ready6ById.get(program.registry_id)?.tracking;
      return tracking?.candidate_registry_wired === true
        && (readyRegistry.allowed_provider_tracking_hosts?.[ready6ById.get(program.registry_id)?.network] || []).includes(tracking.tracking_host);
    })
    .map((program) => String(program.review_path || "").replace(/^\/reviews\/|\/$/g, "")));
  const authorizedLiveProgramIds = new Set([...sheetAuthorizedIds, ...contractAuthorizedIds, ...ready6AuthorizedIds]);
  const fullGoLiveReceipt = activationSelection.operator_gate_receipt_sha256;
  if (!/^[a-f0-9]{64}$/.test(fullGoLiveReceipt)) throw new Error("affiliate_activation_receipt_invalid");
  const canonicalAugmented = projectMoneyPagesFromRegistry(programRegistry, SITE_PROFILE, moneySpecs.pages, contentLedger.programs);
  const canonicalQuiz = projectQuizFromRegistry(programRegistry, SITE_PROFILE, quizConfig, contentLedger);
  const therapeuticSurfaces = projectTherapeuticSurfaces({
    registry: programRegistry,
    profileId: SITE_PROFILE,
    pages: canonicalAugmented,
    quizConfig: canonicalQuiz,
  });
  const therapeuticProgramIds = [...therapeuticSurfaces.validated.programs.keys()];
  const suggestions = providerSuggestions(programRegistry, SITE_PROFILE, {
    asOf: new Date(`${programRegistry.provider_dynamic_profiles.sealed_as_of}T12:00:00Z`),
  });
  const suggestionIds = suggestions.map((suggestion) => suggestion.program_id).sort();
  if (JSON.stringify(suggestionIds) !== JSON.stringify(therapeuticSurfaces.includeProgramIds)) {
    throw new Error("provider_suggestions_dynamic_membership_mismatch");
  }
  buildDerivations.therapeutic_taxonomy = {
    schema: programRegistry.therapeutic_taxonomy.schema,
    program_rows: therapeuticProgramIds.length,
    counts: therapeuticSurfaces.validated.counts,
    trt_nation_link_state: therapeuticSurfaces.validated.programs.get("trt-nation")?.commercial_link_state,
    source_authorities: Object.fromEntries(Object.entries(programRegistry.therapeutic_taxonomy.authorities).map(([key, value]) => [key, value.sha256])),
  };
  buildDerivations.provider_dynamic_profiles = {
    schema: therapeuticSurfaces.dynamic.document.schema,
    canonical_provider_count: suggestions.length,
    counts: therapeuticSurfaces.dynamic.counts,
    suggestion_ids: suggestionIds,
  };
  const augmented = therapeuticSurfaces.pages.map((spec) => {
    const liveRows = spec.rows.filter((row) => row.live_link_allowed || row.promotion_authorized);
    if (liveRows.some((row) => !authorizedLiveProgramIds.has(row.program_id) || !programMap.has(row.program_id))) throw new Error(`money_page_unauthorized_live_row:${spec.page_id}`);
    const shouldBeLive = liveRows.length > 0;
    if (Boolean(spec.live_link_allowed) !== shouldBeLive || (shouldBeLive && spec.operator_gate_receipt !== fullGoLiveReceipt) || (!shouldBeLive && spec.operator_gate_receipt !== null)) throw new Error(`money_page_full_go_gate:${spec.page_id}`);
    // REBUILD B-15: the ProviderCompareCard grid reads the SAME machine-written
    // capture the calculator and the price index read. Handing the whole matrix
    // to the renderer (rather than flattening it into the spec) keeps the
    // automation write-point exactly where the refresh machine expects it.
    spec.pricing_matrix = providerPricingMatrix;
    return spec;
  });
  // 2026-08-06 cluster 1: freshness is derived, never stamped. Each comparison
  // page takes the newest dated evidence it actually carries, floored at its
  // published date and its previous stamp so a date never travels backwards.
  // 2026-08-18 chip repair: the shipped logo asset is the authority for BOTH
  // which providers get a mark and what surface that mark needs. Joining here
  // (once, before anything renders) is what stops a row from disagreeing with
  // the asset the same release ships - the root cause of both chip defects.
  const couponOffersByProgram = activeCouponMap(couponFeed);
  for (const spec of augmented) spec.coupon_offers_by_program = couponOffersByProgram;
  buildDerivations.coupon_offers = { schema: couponFeed.schema, total: couponFeed.offers.length, active: Object.keys(couponOffersByProgram).length };
  await write("data/coupon-offers.json", JSON.stringify(couponFeed, null, 2) + "\n");
  const providerLogoManifest = JSON.parse(await readFile(path.join(ROOT, "assets/provider-logos/manifest.json"), "utf8"));
  const wordmarkFontMetrics = await readJson("wordmark-font-metrics.json");
  buildDerivations.provider_chip_treatment = decorateProviderChips([...canonicalAugmented, ...therapeuticSurfaces.pages], providerLogoManifest, wordmarkFontMetrics);
  for (const spec of augmented) Object.assign(spec, deriveMoneyPageFreshness(spec));
  derivedTallies.COMPARISON_PAGE_COUNT = augmented.length;
  // 2026-08-27: the reader-facing guide tally counts what the Insights library
  // actually lists — the same governed exclusion set (retired routes + the
  // byte-pinned coupon page) that shapes the library shapes this number.
  derivedTallies.RESEARCH_GUIDE_COUNT = glpGuideArticles.filter((article) => !PHASE3_INSIGHTS_EXCLUSIONS.has(article.canonical_path)).length;
  buildDerivations.comparison_page_count = augmented.length;
  buildDerivations.table_containment = tableContainment;
  buildDerivations.research_guide_count = glpGuideArticles.filter((article) => !PHASE3_INSIGHTS_EXCLUSIONS.has(article.canonical_path)).length;
  // 2026-08-18 quiz ranking honesty: join the governed pricing matrix and the
  // evidence-derived criteria onto every candidate, and re-derive the price lane
  // from published prices rather than from the pre-matrix `comparable_price`
  // flag. Doing it HERE (not in the checked-in quiz-config) is what makes the
  // monthly pricing-refresh machine self-correcting: when a provider's pricing
  // goes unavailable, it leaves the price lane on the next build with no edit.
  const projectedQuiz = (() => {
    const base = therapeuticSurfaces.quizConfig;
    const ledgerScores = new Map(contentLedger.programs.map((program) => [program.program_id, program]));
    const withCriteria = (base.candidates || []).map((candidate) => {
      const entry = ledgerScores.get(candidate.program_id);
      if (!entry?.criteria_scores) throw new Error(`quiz_candidate_criteria_scores_missing:${candidate.program_id}`);
      const priced = providerPricingMatrix.providers?.[candidate.program_id] || null;
      return {
        ...candidate,
        criteria_scores: entry.criteria_scores,
        pricing_checked_on: (priced?.medications || []).map((row) => row.captured_on).sort().at(-1) || null,
      };
    });
    const joined = applyPricingToCandidates(withCriteria, providerPricingMatrix);
    buildDerivations.quiz_ranking = {
      priced_candidates: joined.priced_candidates,
      price_lane_added: joined.price_lane_added,
      price_lane_removed: joined.price_lane_removed,
      cohort_min_6mo_usd: joined.cohort_min_6mo_usd,
    };
    return {
      ...base,
      candidates: joined.candidates,
      ranking: {
        schema: "glp-quiz-goal-weighted-ranking-v1",
        cohort_min_6mo_usd: joined.cohort_min_6mo_usd,
        all_in_months: ALL_IN_MONTHS,
        all_in_label: `Lowest published ${ALL_IN_MONTHS}-month all-in`,
        pricing_matrix_captured_on: providerPricingMatrix.captured_on,
      },
    };
  })();
  const providerReviews = buildProviderReviewSpecs(canonicalAugmented, providerPageContracts, providerFastpath, programRegistry, SITE_PROFILE, contentLedger.programs);
  // 2026-08-18: a review spec rebuilds its own row (a fast-path provider's row
  // comes from the contract, not from a comparison page), so the chip join has
  // to reach it too - otherwise BiltRx's white mark keeps rendering on a white
  // chip on its own review page after the compare chips were fixed.
  buildDerivations.provider_chip_treatment_reviews = decorateProviderChips(
    [{ rows: providerReviews.map((review) => review.row).filter(Boolean) }],
    providerLogoManifest, wordmarkFontMetrics);
  for (const review of providerReviews) {
    validatePromoRecord(review.row?.promo);
    review.date_modified = deriveReviewFreshness(review);
    review.row.review_date_modified = review.date_modified;
  }
  assertUniqueRenderIdentity(providerReviews.filter((review) => !providerIdentity.aliasIds.has(review.program_id)), "provider-reviews");
  const providerLogoByProgramId = new Map((providerLogoManifest.logos || []).map((logo) => [logo.program_id, logo]));
  const ready6CanonicalLogoIdByRegistryId = new Map(Object.values(programRegistry.provider_dynamic_profiles?.profiles || {}).flatMap((profile) =>
    (profile.registry_program_ids || []).map((registryId) => [registryId, profile.program_id])));
  const ready6LogoByRegistryId = Object.fromEntries(programRegistry.programs.flatMap((program) => {
    const canonicalId = ready6CanonicalLogoIdByRegistryId.get(program.program_id)
      || providerIdentity.canonicalOf.get(program.program_id)
      || program.program_id;
    const logo = providerLogoByProgramId.get(canonicalId);
    if (!logo) return [];
    return [[program.program_id, {
      path: logo.public_path,
      alt: `${logo.provider} logo`,
      width: logo.width,
      height: logo.height,
      treatment: logo.logo_treatment,
      content_scale: logo.logo_fit?.content_scale,
      runtime_remote_load: logo.runtime_remote_load,
    }]];
  }));
  const ready6Editorial = await buildReady6EditorialPages(ROOT, {
    // The READY6 editorial contract is a sealed 2026-08-02 record and still
    // validates in full; the site registry decides what actually publishes, so a
    // network-paused program drops out here without falsifying that record.
    activeRegistryIds: programRegistry.programs.map((program) => program.program_id),
    ledgerPrograms: contentLedger.programs,
    aliases: Object.fromEntries([...providerIdentity.aliasIds].map((aliasId) => {
      const canonical = programRegistry.programs.find((program) => program.program_id === providerIdentity.canonicalOf.get(aliasId));
      return [aliasId, { brand: canonical.display_name, route: canonical.review_path }];
    })),
    logoByRegistryId: ready6LogoByRegistryId,
    shell: "shared-interactive",
  });
  const costCalculator = buildCostCalculatorData(costCalculatorConfig, canonicalAugmented, providerPageContracts, providerFastpath, programRegistry, SITE_PROFILE, providerIdentity.aliasKeys, providerPricingMatrix);
  buildDerivations.cost_calculator_pricing = costCalculator.pricing_coverage;
  assertUniqueRenderIdentity(costCalculatorDropdown(costCalculator).eligible, "cost-calculator-dropdown");
  const {governedEmailAffiliateLinks}=await import('../src/cost-calculator-affiliate.mjs');
  const expectedCostAffiliateEmailManifest = costAffiliateEmailManifest(governedEmailAffiliateLinks(augmented, programRegistry, activationSelection, SITE_PROFILE));
  if (JSON.stringify(COST_AFFILIATE_EMAIL_MANIFEST) !== JSON.stringify(expectedCostAffiliateEmailManifest)) {
    throw new Error("frozen_cost_affiliate_email_manifest_drift");
  }
  await write("data/therapeutic-taxonomy.json", JSON.stringify(programRegistry.therapeutic_taxonomy, null, 2) + "\n");
  for (const spec of augmented) { const key = `money-${routeSlug(spec.path)}`; await write(path.join(spec.path.replace(/^\//, ""), "index.html"), addSharedRuntime(addOg(pageHero(addPriceIndexMoneyLink(addMoneyHero(setRobots(renderMoneyPage(spec, { ...identity, author: editorialAuthor }), "index,follow,max-image-preview:large"))), key, `Comparison overview for ${spec.h1}`), key, spec.h1))); }
  for (const review of providerReviews) { const key = review.fastpath_contract ? "editorial-evidence-hero" : `review-${routeSlug(review.path)}`; await write(path.join(review.path.replace(/^\//, ""), "index.html"), markReleaseReady(addSharedRuntime(addOg(standaloneHero(renderProviderReviewPage(review, { ...identity, author: editorialAuthor }), key, `About ${review.provider}`), key, `${review.provider} review`)))); }
  for (const page of ready6Editorial.pages) await write(path.join(page.route.replace(/^\//, ""), "index.html"), markReleaseReady(addSharedRuntime(page.html)));
  for (const [relative, bytes] of preservedReviewBytes) {
    const { html, rewrites } = redriveTallies(relative, bytes.toString("utf8"), derivedTallies);
    buildDerivations.frozen_byte_tally_rewrites.push(...rewrites);
    const normalized = normalizeFrozenScoreState(relative, html);
    if (normalized.rewritten) buildDerivations.frozen_byte_score_rewrites.push(relative);
    await write(relative, normalized.html);
  }
  // DESIGN WAVE 2026-08-06: the quiz result cards reuse the compare-tab logo rows,
  // so a logo can never diverge between the two surfaces.
  const quizProviderLogos = {};
  for (const spec of augmented) for (const row of spec.rows) if (row.logo && !quizProviderLogos[row.program_id]) quizProviderLogos[row.program_id] = row.logo;
  const quizProviderWordmarks = wordmarkFitsFor(projectedQuiz.candidates, providerLogoManifest, wordmarkFontMetrics);
  await write("matches/customized/index.html", addSharedRuntime(addOg(renderMatchesCustomized(projectedQuiz, programRegistry, SITE_PROFILE, quizProviderLogos), "quiz-hero", "Customized provider matches · Clinic Scout")));
  await write("quiz/provider-fit/index.html", addQuizBreadcrumbSchema(addSharedRuntime(addOg(responsiveHero(renderQuiz({ ...projectedQuiz, states: ["AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA","WV","WI","WY"] }, programRegistry, SITE_PROFILE, quizProviderLogos, quizProviderWordmarks), "quiz-hero"), "quiz-hero", projectedQuiz.h1))));
  // Bind the entrance receipt to the organic document just emitted above,
  // including its existing all-state projection, not a second raw quiz render.
  const paidQuiz = renderPaidQuiz(await readFile(path.join(PUBLIC, "quiz/provider-fit/index.html"), "utf8"), ASSET_VERSION);
  await writeCleanRoom("lp/quiz/index.html", paidQuiz.html);
  for (const asset of paidQuiz.assets) await writeCleanRoom(asset.file, asset.body);
  for (const fragment of paidQuiz.fragments) await writeCleanRoom(fragment.file, fragment.html);

  const costCalculatorHtml = directCalculatorWorkspace(decorateCostCalculatorAffiliateSurface(renderCostCalculator(costCalculator), costCalculator, augmented, programRegistry, activationSelection, SITE_PROFILE));
  await write("tools/glp-1-cost-calculator/index.html", addSharedRuntime(addOg(costCalculatorHtml, "money-online-glp-1-programs", costCalculator.h1)));
  for (const article of allArticles) {
    // R2 target release: render Mounjaro from the refreshed catalog row.
    const key = `guide-${routeSlug(article.canonical_path)}`;
    const articleBase = addOg(responsiveHero(pageHero(addGuideMetadata(renderBlogPost(article, resolveBlogComparisonSpec(article, contentLedger.programs), resolveArticleAuthor(article), contentLedger.programs), article, resolveArticleAuthor(article)), key, article.images?.[0]?.alt || `Guide overview for ${article.h1}`), key), key, article.h1);
    const articleGraph = addArticleShareControls(articleVertical(article) === "GLP-1" ? upgradeInsightsArticle(articleBase, article, glpGuideArticles.filter((item) => item.canonical_path !== article.canonical_path && path.join(item.canonical_path.replace(/^\//, ""), "index.html") !== MOUNJARO_COUPON_ROUTE_FILE)) : articleBase);
    const rendered = addSharedRuntime(articleGraph);
    await write(path.join(article.canonical_path.replace(/^\//, ""), "index.html"), articleVertical(article) === "GLP-1" ? markReleaseReady(article.suppress_cta ? rendered : addGuideFunnel(rendered)) : rendered);
  }
  const directoryStates = npi.cities.map((city) => {
    const slug = city.city_slug || `${city.city.toLowerCase().replaceAll(/[^a-z0-9]+/g, "-")}-${city.state.toLowerCase()}`;
    const gate = evaluateDirectoryGate(city);
    return { slug, status: "RETIRED", reason: "doorway_surface_retired_no_publishable_source_unique_value", prior_gate_status: gate.status, records: city.records.length, population_2025: city.population_2025 || null, evidence_tier: city.direct_service_evidence_tier || "LEGACY" };
  });
  const trustPageMap = trustPages(identity, glpArticles, authorRegistry);
  const legalPageMap = leanLegalPages(framePage);
  const indexableTrustPages = new Set(["how-we-rate/index.html", "how-we-make-money/index.html", "editorial-guidelines/index.html", "authors/clinic-scout-editorial-team/index.html", "authors/izaiah-tilton/index.html", "review-standards/index.html"]);
  for (const [relative, html] of Object.entries({ ...trustPageMap, ...legalPageMap })) {
    const visual = TRUST_VISUALS[relative];
    const canonical = addTrustMetadata(addCanonical(html, relative), relative);
    const rendered = visual ? addOg(standaloneHero(canonical, visual.key, visual.alt), visual.key, visual.title) : canonical;
    await write(relative, indexableTrustPages.has(relative) ? markReleaseReady(addSharedRuntime(rendered)) : addSharedRuntime(rendered));
  }
  await write("index.html", addSharedRuntime(addOg(framePage("Clinic Scout · Evidence before enrollment", `<section class="gcs-home-hero"><div><p class="eyebrow">Evidence before enrollment</p><h1>Find the facts behind the price.</h1><p class="lede">Independent, source-bound research for people comparing GLP-1 programs, prices, policies, and registry-listed clinic organizations—without fake rankings or hidden live promotions.</p><div class="gcs-home-actions"><a class="gcs-button" href="/providers/">Compare programs</a><a class="gcs-button secondary" href="/insights/">Explore the directory</a></div></div><picture><source srcset="/assets/images/editorial-evidence-hero-800.avif 800w, /assets/images/editorial-evidence-hero-1600.avif 1600w" sizes="(max-width:900px) calc(100vw - 24px), 540px" type="image/avif"><source srcset="/assets/images/editorial-evidence-hero-800.webp 800w, /assets/images/editorial-evidence-hero-1600.webp 1600w" sizes="(max-width:900px) calc(100vw - 24px), 540px" type="image/webp"><img src="/assets/images/editorial-evidence-hero-1600.jpg" srcset="/assets/images/editorial-evidence-hero-800.jpg 800w, /assets/images/editorial-evidence-hero-1600.jpg 1600w" sizes="(max-width:900px) calc(100vw - 24px), 540px" width="1600" height="900" fetchpriority="high" alt="Clinic Scout research"></picture></section><section class="gcs-proof" aria-label="Research coverage"><div><strong>${blogBatch.articles.length}</strong><span>full evidence guides</span></div><div><strong>${augmented.length}</strong><span>structured comparison pages</span></div><div><strong>${npi.cities.length}</strong><span>city directory pages</span></div><div><strong>0</strong><span>live promotional links</span></div></section><section><div class="gcs-section-head"><h2>A calmer way to compare.</h2><p>Every page tells you what the source proves, what it does not prove, and where the evidence stops. Missing facts stay pending instead of becoming confident estimates.</p></div><div class="grid"><a class="panel" href="/cost/online-glp-1-programs/"><p class="eyebrow">Cost clarity</p><h3>See recurring price terms in context.</h3><p>Compare documented inclusions, renewal cadence, insurance support, and source freshness.</p></a><a class="panel" href="/guides/semaglutide-online-safety-verification/"><p class="eyebrow">Verification</p><h3>Check an online program before paying.</h3><p>Use a practical source checklist without turning general information into medical advice.</p></a><a class="panel" href="/quiz/provider-fit/"><p class="eyebrow">Private tool</p><h3>Narrow choices by commercial priorities.</h3><p>Broad health answers remain in the browser and are not used for program ranking.</p></a><a class="panel" href="/editorial-guidelines/"><p class="eyebrow">Accountability</p><h3>Read the evidence standard.</h3><p>See risk tiers, source requirements, correction rules, and what Clinic Scout refuses to claim.</p></a></div></section><section class="gcs-directory-band"><p class="eyebrow">National registry research</p><h2>Local context without pretending every listing offers GLP-1 care.</h2><p>${npi.cities.length} city pages pair current Census population context with active NPI-2 organization records. NPPES supports identity, taxonomy, and location—not service, quality, price, availability, or outcomes.</p><a class="gcs-button" href="/insights/">See a city research page</a></section>`), "editorial-evidence-hero", "Clinic Scout · Evidence before enrollment")));
  await write("guides/index.html", markReleaseReady(addSharedRuntime(addOg(renderGuideLibrary(glpGuideArticles), "editorial-evidence-hero", "Clinic Scout GLP-1 research library"))));
  const providerDataAsOf = maxDate(augmented.map((spec) => spec.date_modified)) || maxDate(Object.values(buildDerivations.freshness));
  const [asOfYear, asOfMonth] = String(providerDataAsOf).split("-");
  const updatedLabel = `${MONTH_NAMES[Number(asOfMonth) - 1]} ${asOfYear}`;
  // REBUILD homepage (LAY T1): the ranked strip and the price band are DERIVED
  // from the same ranked comparison and the same machine-written pricing capture
  // the hub renders. Nothing on the homepage is a number somebody typed.
  const homeSpec = augmented.find((spec) => spec.path === "/best/online-glp-1-providers/") || augmented[0];
  const homeMatrixKey = (row) => String(row.review_path || "").replace(/^\/reviews\//, "").replace(/\/$/, "") || row.program_id;
  const homeCheapestOf = (row) => {
    const entry = providerPricingMatrix.providers?.[homeMatrixKey(row)];
    const priced = (entry?.medications || []).filter((med) => Number.isFinite(med.ongoing_usd) && !String(med.medication_id).startsWith("brand-") && med.medication_id !== "membership-only");
    return priced.sort((left, right) => left.ongoing_usd - right.ongoing_usd)[0] || null;
  };
  const homeRanked = [...homeSpec.rows]
    .map((row) => ({ row, score: Number(row.score) || 0 }))
    .sort((left, right) => right.score - left.score || String(left.row.provider).localeCompare(String(right.row.provider)))
    .slice(0, 3);
  const topPicks = homeRanked.map(({ row }) => {
    const med = homeCheapestOf(row);
    return {
      provider: row.provider,
      reviewPath: row.review_path || "/providers/",
      logo: row.logo,
      wordmark: row.wordmark,
      price: med?.ongoing_usd ?? null,
      checked: med?.captured_on ?? null,
      basis: med ? `${med.label}, captured from ${row.provider}'s own page.` : "",
    };
  });
  const cheapestAll = homeSpec.rows
    .map((row) => ({ row, med: homeCheapestOf(row) }))
    .filter((item) => item.med)
    .sort((left, right) => left.med.ongoing_usd - right.med.ongoing_usd)[0] || null;
  const cheapest = cheapestAll
    ? { provider: cheapestAll.row.provider, price: cheapestAll.med.ongoing_usd, checked: cheapestAll.med.captured_on, label: cheapestAll.med.label }
    : null;
  // ---- PARITY REDESIGN 2026-08-20: the three new homepage data feeds --------
  // All three are DERIVED from machine-written pricing captures and the same
  // ranked comparison the hub renders. Direct manufacturer self-pay prices are
  // allowed only through medication-direct-pricing.json with dated source proof.

  // (a) The medication rail. One card per treatment route, each priced from the
  //     cheapest published capture across all providers for that route, and each
  //     carrying its true regulatory status. Compounded formulations say plainly
  //     that they are not FDA approved — glpwinner's equivalent cards put an "FDA
  //     approved for ..." line on everything, which we will not copy.
  const cheapestForMedication = (predicate) => {
    let best = null;
    for (const row of homeSpec.rows) {
      const entry = providerPricingMatrix.providers?.[homeMatrixKey(row)];
      for (const med of entry?.medications || []) {
        if (!Number.isFinite(med.ongoing_usd) || !predicate(String(med.medication_id))) continue;
        if (!best || med.ongoing_usd < best.price) best = { price: med.ongoing_usd, checked: med.captured_on, provider: row.provider };
      }
    }
    return best;
  };
  const railSpec = [
    { key: "semaglutide", name: "Compounded semaglutide", tone: "semaglutide", art: "med-rail-semaglutide",
      alt: "An unbranded frosted glass vial lit by a teal and emerald aurora",
      status: `Compare injectable semaglutide programs and what each one includes.`,
      href: PASS5_MEDICATION_DESTINATIONS.compoundedSemaglutide,
      match: (id) => id === "compounded-semaglutide-injection" },
    { key: "tirzepatide", name: "Compounded tirzepatide", tone: "tirzepatide", art: "med-rail-tirzepatide",
      alt: "Two unbranded injector pens crossing, lit by an emerald aurora",
      status: `Compare injectable tirzepatide programs and their monthly costs.`,
      href: PASS5_MEDICATION_DESTINATIONS.compoundedTirzepatide,
      match: (id) => id === "compounded-tirzepatide-injection" },
    { key: "wegovy-pill", name: "Wegovy pill", tone: "oral", art: "phase3-product-wegovy-pill", widths: [640, 1024],
      alt: "An editorial Wegovy pill product render on an aurora-lit surface",
      status: `Understand the oral route, provider pathways, and cost questions.`,
      href: PASS5_MEDICATION_DESTINATIONS.wegovyPill,
      match: () => false, directPriceKey: "wegovy-pill" },
    { key: "ozempic", name: "Ozempic", tone: "brand", art: "phase3-product-ozempic", widths: [640, 1024],
      alt: "An editorial Ozempic injector product render under a teal aurora",
      status: `Compare providers with published Ozempic pathways.`,
      href: PASS5_MEDICATION_DESTINATIONS.ozempic,
      match: (id) => id === "brand-ozempic" },
    { key: "wegovy", name: "Wegovy", tone: "brand", art: "phase3-product-wegovy", widths: [640, 1024],
      alt: "An editorial Wegovy injector product render under a coral aurora",
      status: `Compare providers with published Wegovy pathways.`,
      href: PASS5_MEDICATION_DESTINATIONS.wegovy,
      match: (id) => id === "brand-wegovy" },
    { key: "mounjaro", name: "Mounjaro", tone: "brand", art: "phase3-product-mounjaro", widths: [640, 1024],
      alt: "An editorial Mounjaro injector product render under an emerald aurora",
      status: `Compare providers with published Mounjaro pathways.`,
      href: PASS5_MEDICATION_DESTINATIONS.mounjaro,
      match: (id) => id === "brand-mounjaro" },
    { key: "zepbound", name: "Zepbound", tone: "brand", art: "phase3-product-zepbound", widths: [640, 1024],
      alt: "An editorial Zepbound injector product render under a violet aurora",
      status: `Compare providers with published Zepbound pathways.`,
      href: PASS5_MEDICATION_DESTINATIONS.zepbound,
      match: (id) => id === "brand-zepbound" },
  ];
  const railMeds = railSpec.map((spec) => {
    const direct = medicationDirectPricing.medications?.[spec.directPriceKey];
    const priced = cheapestForMedication(spec.match) || (Number.isFinite(direct?.monthly_price_usd)
      ? { price: direct.monthly_price_usd, checked: direct.observed_on, provider: direct.source_publisher, priceLabel: direct.price_label }
      : null);
    return { name: spec.name, tone: spec.tone, art: spec.art, widths: spec.widths, alt: spec.alt, status: spec.status, href: spec.href,
      price: priced?.price ?? null, checked: priced?.checked ?? null, priceLabel: priced?.priceLabel ?? "Provider-published starting price" };
  });

  // (b) Verdict cards for the wall — our own editorial bottom line per provider,
  //     with the rubric score that produced it. These are OUR words about OUR
  //     research, never a patient's.
  const verdictCards = homeSpec.rows
    .filter((row) => String(row.bottom_line || "").trim() && row.review_path)
    .sort((left, right) => (Number(right.score) || 0) - (Number(left.score) || 0))
    .slice(0, 6)
    .map((row) => ({ provider: row.provider, href: row.review_path, logo: row.logo, wordmark: row.wordmark, score: `${row.score}/100`,
      verdict: String(row.bottom_line).replace(/\s+/g, " ").trim().slice(0, 210) }));

  // (c) Price-check receipts — the newest dated captures in the build. This is the
  //     content glpwinner structurally cannot show: they publish no dates at all.
  const receiptCards = homeSpec.rows
    .map((row) => {
      const entry = providerPricingMatrix.providers?.[homeMatrixKey(row)];
      const priced = (entry?.medications || [])
        .filter((med) => Number.isFinite(med.ongoing_usd) && med.captured_on)
        .sort((left, right) => String(right.captured_on).localeCompare(String(left.captured_on)))[0];
      if (!priced || !row.review_path) return null;
      return { provider: row.provider, href: row.review_path, checked: priced.captured_on,
        line: `${priced.label} listed at $${Number(priced.ongoing_usd).toLocaleString("en-US")}/month.` };
    })
    .filter(Boolean)
    .sort((left, right) => String(right.checked).localeCompare(String(left.checked)))
    .slice(0, 6);

  // (d) Approved first-party reader reviews for the home review wall (design
  //     pass 2026-08-26, home spec fix #8). The optional data file is the
  //     moderation pipeline's APPROVED output — a submission only lands here
  //     after the manual hold-and-verify flow the /reviews/ hub describes. No
  //     file (today's truth: 0 approved) → the wall renders its honest empty
  //     state. Expected record shape: { status: "approved", entity,
  //     entity_type, entity_path, rating, body, reviewer, approved_on }.
  let approvedReaderReviews = [];
  try {
    const wallData = await readJson("approved-reader-reviews.json");
    approvedReaderReviews = (wallData.reviews || []).filter((review) => review.status === "approved" && review.entity && review.body);
  } catch {}
  const homepage = renderHomePage({ blogCount: glpGuideArticles.filter((article) => !PHASE3_INSIGHTS_EXCLUSIONS.has(article.canonical_path)).length, comparisonCount: augmented.length, programCount: liveProgramCount, updatedLabel, topPicks, cheapest, railMeds, verdictCards, receiptCards, readerReviews: approvedReaderReviews, reviewPrograms: programRegistry.programs.filter((program) => !providerIdentity.aliasIds.has(program.program_id)), reviewPharmacies: phase3PharmacyInput });
  await write("index.html", markReleaseReady(addSharedRuntime(addOg(homepage, "home-evidence", "Clinic Scout · Compare what matters before you commit"))));
  const phase3Build = buildPhase3PageGraph({
    graph: phase3GraphConfig,
    pharmacyInput: phase3PharmacyInput,
    pricingMatrix: providerPricingMatrix,
    programRegistry,
    homeSpec,
    articles: glpGuideArticles,
    couponOffersByProgram,
  });
  for (const page of phase3Build.pages) {
    let phase3Html = page.html;
    if (["providers/index.html", "wegovy/index.html"].includes(page.file)) {
      const payloadPattern = /<script type="application\/json" data-provider-card-payloads>([\s\S]*?)<\/script>/;
      const payloadMatch = phase3Html.match(payloadPattern);
      if (!payloadMatch) throw new Error("provider_card_payload_missing");
      const payload = JSON.parse(payloadMatch[1]);
      const lazyCardCount = (phase3Html.match(/data-provider-lazy-index=/g) || []).length;
      if (!Array.isArray(payload) || lazyCardCount === 0 || payload.length !== lazyCardCount) throw new Error("provider_card_payload_invalid");
      const payloadText = `${JSON.stringify(payload)}\n`;
      const payloadFilename = `provider-card-payloads.${createHash("sha256").update(payloadText).digest("hex").slice(0, 16)}.json`;
      await write(`assets/${payloadFilename}`, payloadText);
      phase3Html = phase3Html.replace(payloadPattern, `<script type="application/json" data-provider-card-payloads data-src="/assets/${payloadFilename}">[]</script>`);
    }
    const visualPage = upgradePhase3PageVisuals(phase3Html, { family: page.family, route: page.route, graph: phase3GraphConfig, registry: programRegistry });
    let html = addSharedRuntime(visualPage);
    if (ADS_LP_ROUTE_FILES.has(page.file)) {
      html = optimizePaidLandingPage(html);
      html = setRobots(html, "noindex,nofollow,noarchive,nosnippet,max-image-preview:large");
    } else {
      html = markReleaseReady(html);
    }
    await write(page.file, html);
  }
  const {renderPriceStateComparison}=await import('../src/price-state.mjs');
  const comparisonStates=renderPriceStateComparison(liveRegistryRows.filter(p=>!providerIdentity.aliasIds.has(p.program_id)).map(p=>{const id=String(p.review_slug||p.program_id),entry=providerPricingMatrix.providers[id];return {id,label:entry?.provider||p.name||p.display_name||id,review_path:p.review_path||`/reviews/${id}/`,entry};}));
  await write("compare/index.html", markReleaseReady(addSharedRuntime(renderPass5ComparisonHub(providerDataAsOf).replace('</main>',comparisonStates+'</main>'))));
  const reviewLogoByProgram = Object.fromEntries((providerLogoManifest.logos || []).map((logo) => [logo.program_id, {
    path: logo.public_path,
    alt: `${logo.provider} logo`,
    width: logo.width,
    height: logo.height,
    // A4: light/white wordmarks need the dark tile ground or they vanish on
    // the white logo tile (measured logo_luminance in the manifest).
    treatment: logo.logo_treatment || "light-chip",
  }]));
  const reviewHubPrograms = programRegistry.programs.filter((program) => !providerIdentity.aliasIds.has(program.program_id));
  const reviewHubEvidenceDates = [...JSON.stringify({ programs: reviewHubPrograms, pharmacies: phase3PharmacyInput }).matchAll(/\d{4}-\d{2}-\d{2}/g)].map((match) => match[0]);
  const reviewHubDateModified = maxDate([...reviewHubEvidenceDates, providerDataAsOf]);
  await write("reviews/index.html", markReleaseReady(addSharedRuntime(renderReviewDirectoryPage({ programs: reviewHubPrograms, pharmacies: phase3PharmacyInput, logoByProgram: reviewLogoByProgram, dateModified: reviewHubDateModified }))));
  await write("reviews/submit/index.html", addSharedRuntime(renderReviewSubmissionPage({ programs: reviewHubPrograms, pharmacies: phase3PharmacyInput, dateModified: reviewHubDateModified })));
  await writeOps("phase3-page-graph.json", `${JSON.stringify(phase3Build.report, null, 2)}\n`);
  // ---- B-17: LP-A paid landing page, first instance (SPEC-ADS §3.1) ---------
  // Built PAUSED-ready: noindex, out of the sitemap, AdsBot allowed by robots,
  // and rendered through the SAME affiliate anchor path as every organic surface
  // so the link-monetization gate and deploy sensor #16 travel with it.
  {
    const lpSpec = homeSpec;
    const lpRanked = [...lpSpec.rows].sort((left, right) => (scoreProvider(right) ?? 0) - (scoreProvider(left) ?? 0) || String(left.provider).localeCompare(String(right.provider)));
    const lpRow = lpRanked.find((row) => (row.review_path || "").includes("orderlymeds"));
    if (lpRow) {
      const lpPrice = homeCheapestOf(lpRow);
      const lpScore = scoreProvider(lpRow);
      const lpMonth = MONTH_NAMES[Number(String(providerDataAsOf).split("-")[1]) - 1];
      const lpYear = String(providerDataAsOf).split("-")[0];
      const defaultHeadline = `${lpRow.provider} Review (${lpMonth} ${lpYear}): Prices, Fees, and How It Compares`;
      const headlines = {
        omr: resolveHeadlineTokens(`${lpRow.provider} Review ([month] [year]): Prices, Fees, and How It Compares`, providerDataAsOf),
        omp: resolveHeadlineTokens(`${lpRow.provider} Pricing ([month] [year]): What It Publishes, Checked and Dated`, providerDataAsOf),
        cmp: resolveHeadlineTokens(`Compare the Best Online GLP-1 Programs of [year]`, providerDataAsOf),
      };
      const rail = lpRanked
        .filter((row) => row !== lpRow)
        .map((row) => ({ row, med: homeCheapestOf(row), typedPrices: (providerPricingMatrix.providers?.[homeMatrixKey(row)]?.medications || []).filter(item => Object.hasOwn(item, "price_kind")) }))
        .filter(({ med, typedPrices }) => Number.isFinite(med?.ongoing_usd) || typedPrices.length)
        .slice(0, 5)
        .map(({ row, med, typedPrices }) => {
          const score = scoreProvider(row);
          return {
            name: row.provider,
            score: Number.isFinite(score) ? score / 10 : null,
            differentiator: String(row.best_for || "").replace(/^Stronger published /, "Strongest on ").replace(/ evidence in this comparison$/, ""),
            price: med?.ongoing_usd ?? null,
            checked: med?.captured_on ?? null,
            typedPrices,
            cta: affiliateCta(lpSpec, row, "lp-rail", "visit", "a", 'class="cta cta--row" data-element-code="R"'),
          };
        });
      const lpHtml = renderAdsLandingPage({
        analyticsAssetUrl: `/assets/${ANALYTICS_ASSET_FILENAME}`,
        provider: { slug: "orderlymeds", name: lpRow.provider, score: Number.isFinite(lpScore) ? lpScore / 10 : null, reviewPath: lpRow.review_path },
        price: lpPrice,
        typedPrices: (providerPricingMatrix.providers?.[homeMatrixKey(lpRow)]?.medications || []).filter(row => Object.hasOwn(row, "price_kind")),
        bullets: [
          lpPrice ? `${lpPrice.label} published at $${lpPrice.ongoing_usd}/month, checked ${lpPrice.captured_on}.` : "See the product/plan evidence below. No comparable monthly cash charge is inferred.",
          String(lpRow.meds_offered || "Medications offered are listed on the provider's own page."),
          String(lpRow.insurance || "Payment terms are listed on the provider's own page."),
          "Every figure on this page links to the provider page we read it from — not to a redirect.",
        ],
        caveat: "Compounded semaglutide and compounded tirzepatide are not FDA-approved and have not been evaluated by the FDA for safety or effectiveness. Introductory pricing can rise after the promotional period — check the current terms on the provider's own page before you enrol.",
        rail,
        sources: (lpSpec.sources || []).filter((source) => String(source.url || "").includes("orderlymeds")).slice(0, 4),
        cta: affiliateCta(lpSpec, lpRow, "lp-hero", "qualify", "a", 'class="cta" data-element-code="L"'),
        stickyCta: affiliateCta(lpSpec, lpRow, "lp-sticky", "qualify", "a", 'class="cta" data-element-code="F"'),
        headlines,
        defaultHeadline,
        checkedThrough: lpPrice?.captured_on || providerDataAsOf,
        disclosure: "Clinic Scout is an independent publisher. Some programs on this page pay us a commission when a reader signs up, at no extra cost to the reader. This page reviews its named program. The alternatives use the published evidence rubric and source-price eligibility; they are not an exhaustive market ranking or a medical recommendation.",
        fdaFootnotes: [
          "Wegovy and Zepbound are FDA-approved for chronic weight management. Ozempic and Mounjaro are FDA-approved for type 2 diabetes and are not FDA-approved for weight loss.",
          "Compounded medications are not FDA-approved and are not reviewed by the FDA for safety, effectiveness, or quality.",
        ],
      });
      await write("lp/r/orderlymeds/index.html", lpHtml);
    }
  }

  await write("404.html", render404Page());
  await write("assets/analytics.js", ANALYTICS_ASSET_SOURCE);
  await write(`assets/${ANALYTICS_ASSET_FILENAME}`, ANALYTICS_ASSET_SOURCE);
  await write("assets/consent.js", consentRuntimeSource);
  for (const stylesheet of STYLESHEET_ASSETS) await write(`assets/${stylesheet}`, await readFile(path.join(ROOT, "assets", stylesheet), "utf8"));
  await write("assets/motion-loader.js", await readFile(path.join(ROOT, "assets/motion-loader.js"), "utf8"));
  await write("assets/motion.js", await readFile(path.join(ROOT, "assets/motion.js"), "utf8"));
  await write("assets/site.js", await readFile(path.join(ROOT, "assets/site.js"), "utf8"));
  await write("assets/review-hub.js", await readFile(path.join(ROOT, "assets/review-hub.js"), "utf8"));
  await write("assets/editorial-share.js", await readFile(path.join(ROOT, "assets/editorial-share.js"), "utf8"));
  await write("assets/quiz-page.js", await readFile(path.join(ROOT, "assets/quiz-page.js"), "utf8"));
  for (const asset of ["paid-quiz.js", "paid-quiz.css"]) await write(`assets/${asset}`, await readFile(path.join(ROOT, "assets", asset), "utf8"));
  await write("assets/matches-customized.js", await readFile(path.join(ROOT, "assets/matches-customized.js"), "utf8"));
  await write("assets/review-intake.js", await readFile(path.join(ROOT, "assets/review-intake.js"), "utf8"));
  await write("assets/content-capture-resilient.js", await readFile(path.join(ROOT, "assets/content-capture-resilient.js"), "utf8"));
  // Ship the default-held A6 helper without importing or invoking it on pages.
  await write("assets/platform-click-handoff.mjs", await readFile(path.join(ROOT, "assets/platform-click-handoff.mjs"), "utf8"));
  await mkdir(path.join(PUBLIC, "assets/vendor"), { recursive: true });
  await cp(path.join(ROOT, "assets/vendor"), path.join(PUBLIC, "assets/vendor"), { recursive: true });
  await mkdir(path.join(PUBLIC, "assets/fonts"), { recursive: true });
  await cp(path.join(ROOT, "assets/fonts"), path.join(PUBLIC, "assets/fonts"), { recursive: true });
  await cp(path.join(ROOT, "assets/provider-logos"), path.join(PUBLIC, "assets/provider-logos"), { recursive: true });
  await cp(path.join(ROOT, "assets/authors"), path.join(PUBLIC, "assets/authors"), { recursive: true });
  // META_PIXEL_RESTORE_V1: root-level static passthrough files. They had no
  // writer at all, so every full rebuild's rm(PUBLIC) dropped them. They are
  // ordinary source-controlled assets, not predecessor-owned generated bytes,
  // so they are copied fresh on every build.
  await cp(path.join(ROOT, "assets/meta-pixel.js"), path.join(PUBLIC, "meta-pixel.js"));
  await cp(path.join(ROOT, "assets/meta-lead.js"), path.join(PUBLIC, "meta-lead.js"));
  // ULTRACODE renderer:shell 2026-08-21: the brand system is now authored in
  // assets/brand/ (aurora-aperture mark, horizontal lockup, favicon SVG, and
  // the PNG fallbacks minted by scripts/build-brand-favicons.mjs). The public
  // tree mirrors that directory exactly — the old generated navy mark and the
  // never-referenced wordmark.svg are gone with the directory replace.
  await rm(path.join(PUBLIC, "assets/brand"), { recursive: true, force: true });
  await cp(path.join(ROOT, "assets/brand"), path.join(PUBLIC, "assets/brand"), { recursive: true });
  // (The shell search's /assets/search-index.json is minted ONCE, later in
  // main() by buildSearchIndex — after every route above has been written.)
  const derivatives = path.join(ROOT, "assets/derivatives");
  try { await cp(derivatives, path.join(PUBLIC, "assets/images"), { recursive: true }); } catch { /* final validator fails missing derivatives */ }
  const [priceTrackerBytes, originalDataStudyBytes, programRegistryBytes, contentLedgerBytes] = await Promise.all([
    readFile(path.join(DATA, "price-tracker.json")),
    readFile(path.join(DATA, "original-data-study.json")),
    readFile(path.join(DATA, "program-registry.json")),
    readFile(path.join(DATA, "program-content-ledger.json")),
  ]);
  const priceTrackerConfig = JSON.parse(priceTrackerBytes);
  const originalDataStudy = JSON.parse(originalDataStudyBytes);
  const wave4PriceTracker = buildPriceTrackerArtifacts(priceTrackerConfig);
  await write(WAVE4_PRICE_ROUTE_FILE, wave4PriceTracker.html);
  await writeOps("wave4-price-tracker-manifest.json", JSON.stringify(wave4PriceTracker.manifest, null, 2) + "\n");
  let priceIndexRegistrySourceSha256 = createHash("sha256").update(programRegistryBytes).digest("hex");
  if (programRegistry.therapeutic_taxonomy) {
    const priceIndexRegistryBasis = structuredClone(programRegistry);
    delete priceIndexRegistryBasis.therapeutic_taxonomy;
    delete priceIndexRegistryBasis.provider_dynamic_profiles;
    delete priceIndexRegistryBasis.consumer_therapeutic_authorities;
    const semanticBasisSha256 = stableSha256(restoreGoOnlyPriceBasis(priceIndexRegistryBasis, ROOT));
    if (semanticBasisSha256 !== programRegistry.therapeutic_taxonomy.price_index_registry_programs_semantic_basis_sha256) {
      throw new Error("price_index_registry_semantic_basis_drift");
    }
    const frozenSourceSha256 = programRegistry.therapeutic_taxonomy.price_index_registry_frozen_source_sha256;
    const trackerSourceSha256 = priceTrackerConfig.price_index.observations.at(-1).source_files.program_registry_sha256;
    if (frozenSourceSha256 !== trackerSourceSha256) throw new Error("price_index_registry_frozen_source_lineage_mismatch");
    priceIndexRegistrySourceSha256 = frozenSourceSha256;
  }
  const priceLedger = frozenPriceLedger(contentLedgerBytes,
    await readFile(path.join(DATA, "price-index-frozen-content-ledger.json")),
    JSON.parse(await readFile(path.join(DATA, "price-index-nonprice-binding.json"), "utf8")));
  const priceIndexInput = {
    tracker: priceTrackerConfig,
    study: originalDataStudy,
    programRegistry,
    contentLedger: priceLedger.ledger,
    sourceHashes: {
      original_data_study_sha256: createHash("sha256").update(originalDataStudyBytes).digest("hex"),
      program_content_ledger_sha256: createHash("sha256").update(priceLedger.bytes).digest("hex"),
      program_registry_sha256: priceIndexRegistrySourceSha256,
    },
  };
  const priceIndex = buildPriceIndexArtifacts(priceIndexInput, {
    decorate: (html) => applyWave2P1Surfaces(markReleaseReady(addByteFrozenSharedRuntime(html)), PRICE_INDEX_ROUTE_FILE),
  });
  const priceIndexRefresh = process.env.GLP_PRICE_INDEX_REFRESH === "1";
  // PASS 2 explicitly migrates the sitewide navigation and opt-in surface. The
  // semantic comparator strips only those marked shell regions, while the
  // manifest comparison below remains strict for every price/data field. This
  // remints transport hashes for the local candidate without refreshing data.
  const pass2ShellRefresh = true;
  // S9 item 11: the page's provider totals are now derived. The preserved Build 4
  // document is projected into the same copy shape before comparison so the
  // migration is explicit and every other preserved byte stays strict.
  assertPriceIndexLiveCount(priceIndex.html, liveProgramCount);
  // Only the exact new disclosure is additive. Historical amounts, sample,
  // sources, dates, manifest and original captured document remain immutable.
  if (priceIndex.html.split(PRICE_STATE_DISCLOSURE).length!==2) throw new Error('price_state_disclosure_missing_or_duplicate');
  const normalizedPriceIndex = normalizePriceIndexBuild4Semantics(substitutePriceIndexLiveCount(priceIndex.html.replace(PRICE_STATE_DISCLOSURE,''), liveProgramCount));
  const stripCurrentPriceDisclosure = html => stripPriceStateDisclosures(html,derivedTallies);
  const normalizedPreservedPriceIndex = normalizePriceIndexBuild4Semantics(
    projectPreservedPriceIndexLiveCount(stripCurrentPriceDisclosure(preservedPriceIndexHtml.toString("utf8")), priceIndexInput.tracker.price_index.observations.at(-1).observed_on, liveProgramCount));
  if (!priceIndexRefresh && normalizedPriceIndex !== normalizedPreservedPriceIndex) {
    let firstDifference = 0;
    while (firstDifference < normalizedPriceIndex.length && normalizedPriceIndex[firstDifference] === normalizedPreservedPriceIndex[firstDifference]) firstDifference += 1;
    const contextStart = Math.max(0, firstDifference - 80);
    const contextEnd = firstDifference + 160;
    throw new Error(`price_index_build4_semantic_preservation_mismatch at=${firstDifference} generated=${JSON.stringify(normalizedPriceIndex.slice(contextStart, contextEnd))} preserved=${JSON.stringify(normalizedPreservedPriceIndex.slice(contextStart, contextEnd))}`);
  }
  const stripPriceIndexTransportHashes = (manifest) => {
    const clone = structuredClone(manifest);
    delete clone.html_sha256;
    delete clone.payload_sha256;
    // validatePriceIndexInputs has already bound the latest observation to the
    // exact current source tuple. Non-price provider evidence can therefore
    // remint these lineage fields without changing any preserved price fact.
    // Keep record_set_sha256, dates, metrics, movers, and artifact facts strict.
    delete clone.source_hashes;
    delete clone.tracker_payload_sha256;
    return clone;
  };
  if (!priceIndexRefresh && stableSha256(stripPriceIndexTransportHashes(priceIndex.manifest)) !==
      stableSha256(stripPriceIndexTransportHashes(preservedPriceIndexManifest))) {
    throw new Error("price_index_build4_manifest_semantic_preservation_mismatch");
  }
  if (priceIndexRefresh || pass2ShellRefresh) {
    await write(PRICE_INDEX_ROUTE_FILE, priceIndex.html);
    // S9 item 11: the page now carries the derived live-count token, so the bytes
    // on disk are the substituted ones. Bind the manifest to what actually
    // shipped and prove the shipped bytes carry the derived count.
    const shippedPriceIndexHtml = await readFile(path.join(PUBLIC, PRICE_INDEX_ROUTE_FILE), "utf8");
    assertPriceIndexLiveCount(shippedPriceIndexHtml, liveProgramCount, { substituted: true });
    await writeOps("price-index-manifest.json", JSON.stringify(rebindPriceIndexShippedHtml(priceIndex.manifest, shippedPriceIndexHtml), null, 2) + "\n");
  } else {
    await write(PRICE_INDEX_ROUTE_FILE, preservedPriceIndexHtml);
    await writeOps("price-index-manifest.json", preservedPriceIndexManifestBytes);
  }
  // AEO P1-G / ED §3.8.3: the Dataset's two `distribution` files are minted from
  // the same validated observation rows the table renders, every build, so the
  // page and the downloads can never drift. They are NOT gated on the refresh
  // flag: in preservation mode the freshly built HTML is byte-identical to the
  // preserved bytes, so the rows behind these files are the rows on the page.
  await write("glp-1-price-index/data/latest.csv", priceIndex.distribution.csv);
  await write("glp-1-price-index/data/latest.json", priceIndex.distribution.json);
  const priceWatchIssue = validatePublishedPriceWatchIssue(
    composePriceWatchIssue({ config: resendMachine, priceIndexInput, eligiblePrograms: [], now: `${priceIndex.manifest.latest_observed_on}T23:59:59Z` }),
    { expectedObservedOn: priceIndex.manifest.latest_observed_on },
  );
  const priceWatchIssueText = JSON.stringify(priceWatchIssue);
  await write("data/price-watch-issue.json", `${JSON.stringify({
    schema: "glp-clinic-scout-published-price-watch-envelope-v1",
    issue_sha256: createHash("sha256").update(priceWatchIssueText).digest("hex"),
    issue: priceWatchIssue,
  }, null, 2)}\n`);

  const wave4LedgerBytes = await readFile(path.join(DATA, "wave4-source-capture-ledger.json"));
  if (createHash("sha256").update(wave4LedgerBytes).digest("hex") !== EXPECTED_LEDGER_FILE_SHA256) throw new Error("wave4_capture_ledger_file_hash_invalid");
  const wave4MysteryShop = buildMysteryShopArtifacts(await readJson("mystery-shop.json"), JSON.parse(wave4LedgerBytes));
  await writeCleanRoom(WAVE4_EVIDENCE_ROUTE_FILE, wave4MysteryShop.html);
  await writeOps("wave4-mystery-shop-manifest.json", JSON.stringify(wave4MysteryShop.manifest, null, 2) + "\n");

  const wave4Deals = buildDealArtifacts(await readJson("deals.json"), { now: preservedDealManifest.evaluated_at });
  await write(WAVE4_OFFER_ROUTE_FILE, wave4Deals.html);
  const rebuiltDealManifestBytes = Buffer.from(JSON.stringify(wave4Deals.manifest, null, 2) + "\n");
  if (!rebuiltDealManifestBytes.equals(preservedDealManifestBytes)) {
    throw new Error("wave4_deal_build4_preservation_mismatch");
  }
  await writeOps("wave4-deal-ingestion-manifest.json", preservedDealManifestBytes);

  const wave4Study = buildStudyArtifacts(originalDataStudy);
  await write(WAVE4_DATA_ROUTE_FILE, wave4Study.html);
  await write("data/public-glp-1-program-pricing-sample.csv", wave4Study.csv);
  await write("data/public-glp-1-program-pricing-sample.json", wave4Study.datasetText);
  await write("data/public-glp-1-program-pricing-sample-methodology.json", wave4Study.methodologyText);
  await writeOps("wave4-original-data-manifest.json", JSON.stringify(wave4Study.manifest, null, 2) + "\n");
  for (const route of phase3Build.retiredRoutes) await rm(path.join(PUBLIC, route.replace(/^\//, "")), { recursive: true, force: true });
  const phase3RedirectSources = new Set(phase3Build.redirects.map(([from]) => from));
  const indexableRoutes = [...new Set([
    "/",
    "/compare/",
    "/matches/customized/",
    "/quiz/provider-fit/",
    "/reviews/",
    PRICE_INDEX_ROUTE,
    costCalculator.path,
    ...augmented.map((spec) => spec.path),
    ...programRegistry.programs.filter((program) => program.profiles?.[SITE_PROFILE]?.surfaces?.sitemap === true).map((program) => program.review_path),
    ...glpArticles.map((article) => article.canonical_path),
    ...[...indexableTrustPages].map((relative) => `/${relative.replace(/index\.html$/, "")}`),
    ...phase3Build.indexableRoutes,
  ])].filter((route) => !phase3RedirectSources.has(route) && !CONSOLIDATED_SOURCE_ROUTES.has(route));
  const articleLastmodByRoute = new Map(glpArticles.map((article) => [article.canonical_path, article.updated_at]));
  articleLastmodByRoute.set(PRICE_INDEX_ROUTE, priceIndex.manifest.latest_observed_on);
  articleLastmodByRoute.set("/reviews/", reviewHubDateModified);
  await write("sitemap.xml", buildSitemap(indexableRoutes, articleLastmodByRoute));
  const redirectMap = new Map(buildReady6Redirects(programRegistry.programs).trim().split("\n").filter(Boolean).map((line) => {
    const [from, to, status = "301"] = line.trim().split(/\s+/);
    return [from, { to, status }];
  }));
  for (const [from, to] of phase3Build.redirects) redirectMap.set(from, { to, status: "301" });
  for (const [from, to] of MUSTFIX_PROVIDER_DETAIL_REDIRECTS) redirectMap.set(from, { to, status: "301" });
  // G08a/S14: the organic medication owners are substantive indexable pages.
  // Do not let inherited aliases silently shadow their generated documents.
  for (const { route } of phase3GraphConfig.organic_compounded_pages) {
    if (redirectMap.has(route)) throw new Error(`organic_category_redirect_collision:${route}`);
  }
  // The OAuth support page preserves its compliance-reviewed consumer support
  // link. Resolve that legacy path to the canonical first-party contact page.
  redirectMap.set("/support/", { to: "/privacy/lead-capture/", status: "301" });
  // This retired comparison route needs a Pages Function so the legacy drug query can map to a live intent route.
  redirectMap.delete("/best/online-glp-1-providers/");
  // W11: resolve every inherited target through the final Phase 3 preserve map.
  // Cloudflare must never send a reader or crawler through a redirect chain.
  const resolveRedirectTarget = (source) => {
    const seen = new Set([source]);
    let target = redirectMap.get(source)?.to;
    while (redirectMap.has(target)) {
      if (seen.has(target)) throw new Error(`redirect_cycle:${[...seen, target].join("->")}`);
      seen.add(target);
      target = redirectMap.get(target).to;
    }
    return target;
  };
  for (const [from, target] of redirectMap) redirectMap.set(from, { ...target, to: resolveRedirectTarget(from) });
  await write("_redirects", `${[...redirectMap].map(([from, target]) => `${from} ${target.to} ${target.status}`).join("\n")}\n`);
  const ready6RouteSet = new Set(ready6Editorial.pages.map((page) => page.route));
  // SHED-301: an alias merchant's review route is a permanent redirect, so it is
  // never a link target — an internal link to a 301 sends every reader and
  // crawler through a hop the canonical link avoids.
  const aliasReviewRoutes = new Set(programRegistry.programs
    .filter((program) => program.profiles?.[SITE_PROFILE]?.canonical_identity?.role === "alias")
    .map((program) => aliasReviewRoute(program)));
  await applyWave2Repairs({
    publicDir: PUBLIC,
    indexableRoutes: indexableRoutes.filter((route) => !ready6RouteSet.has(route)),
    providerReviews,
    // READY6 pages are reasserted from clean-room bytes below, so they are link
    // targets only - but they must BE targets, or the class ships orphaned (G5).
    linkTargetRoutes: [...ready6RouteSet].filter((route) => !aliasReviewRoutes.has(route)),
  });
  // 2026-08-18 (operator: "some of the text is truncated and falls off the side
  // of the screen"). applyWave2Repairs INJECTS the review evidence tables after
  // every page has been written, with its own writeFile - so containing tables
  // only at the write() funnel missed every review page on the site. A final
  // sweep over the built tree is the only placement that is true regardless of
  // which pass emitted the markup, and containTables is idempotent, so a table
  // already contained on the way in is left exactly as it was.
  tableContainment.final_sweep = await sweepTableContainment(PUBLIC);
  breadcrumbRows.link_integrity = await sweepBreadcrumbLinkIntegrity(PUBLIC);
  const reviewDepth = await enforceReviewDepth(PUBLIC, MIN_VISIBLE_REVIEW_WORDS);
  const reviewIntakeFinalization = await finalizeReviewIntakeRoute({ publicDir: PUBLIC });
  await writeOps("review-depth-manifest.json", JSON.stringify(reviewDepth, null, 2) + "\n");
  await writeOps("review-intake-finalization.json", JSON.stringify(reviewIntakeFinalization, null, 2) + "\n");
  // Reassert the governed READY6 editorial body through the current shared review
  // shell after site-wide transforms. Evidence/copy remain owned by READY6; only
  // navigation, search, drawer, subscription, site.js, and motion.js are shared.
  for (const page of ready6Editorial.pages) await write(path.join(page.route.replace(/^\//, ""), "index.html"), markReleaseReady(addSharedRuntime(page.html)));
  // Reassert exact standalone descriptive-data bytes after site-wide transforms.
  await write(WAVE4_DATA_ROUTE_FILE, wave4Study.html);
  // R2 target release: do not overwrite the rendered target with predecessor bytes.
  await write("robots.txt", buildRobotsTxt());
  await write(`${indexNow.key}.txt`, `${indexNow.key}\n`);
  // Affiliate-network site-ownership verification files (LinkConnector and any future
  // network that uses the same fetch-and-byte-compare mechanism). Emitted from
  // data/network-verification-pages.json for exactly the reason the IndexNow key file
  // above is: a verification file dropped into public/ by hand disappears on the next
  // clean rebuild, and the network's crawler then reads a 404 and revokes the account.
  // The payload is byte-exact by contract, so it is stored, asserted, and written - not
  // templated. These are machine endpoints: never in the sitemap, always noindex.
  const networkVerification = await readJson("network-verification-pages.json");
  if (networkVerification.schema !== "glp-network-verification-pages-v1") throw new Error("network_verification_schema_invalid");
  const networkVerificationEmitted = [];
  for (const page of networkVerification.pages || []) {
    if (!/^[a-z0-9][a-z0-9._-]*\.(?:htm|html|txt)$/.test(page.path || "")) throw new Error(`network_verification_path_invalid:${page.path}`);
    if (typeof page.content !== "string" || page.content.length === 0) throw new Error(`network_verification_content_invalid:${page.path}`);
    if (page.final_url !== `https://glpclinicscout.com/${page.path}`) throw new Error(`network_verification_url_mismatch:${page.path}`);
    await write(page.path, page.content);
    networkVerificationEmitted.push({ network: page.network, path: page.path, final_url: page.final_url, bytes: Buffer.byteLength(page.content, "utf8"), content_sha256: createHash("sha256").update(page.content).digest("hex") });
  }
  await writeOps("network-verification-manifest.json", JSON.stringify({ schema: networkVerification.schema, status: "PASS_VERIFICATION_FILES_EMITTED", pages: networkVerificationEmitted }, null, 2) + "\n");
  await writeOps("indexnow-manifest.json", JSON.stringify({
    schema: indexNow.schema,
    status: "PASS_KEY_FILE_EMITTED",
    host: indexNow.host,
    key_location: indexNow.key_location,
    key_sha256: createHash("sha256").update(indexNow.key).digest("hex"),
    endpoint: indexNow.endpoint,
    notify_urls: indexNow.notify_urls,
    key_value_recorded_in_manifest: false,
  }, null, 2) + "\n");
  const standaloneArtifacts = [
    { relative: WAVE4_PRICE_ROUTE_FILE, html: wave4PriceTracker.html, recorded_sha256: wave4PriceTracker.manifest.html_sha256 },
    { relative: WAVE4_EVIDENCE_ROUTE_FILE, html: wave4MysteryShop.html, recorded_sha256: wave4MysteryShop.manifest.rendered_html_sha256 },
    { relative: WAVE4_OFFER_ROUTE_FILE, html: wave4Deals.html, recorded_sha256: null },
    { relative: WAVE4_DATA_ROUTE_FILE, html: wave4Study.html, recorded_sha256: wave4Study.manifest.route_sha256 },
  ];
  const standaloneCspRows = [];
  for (const artifact of standaloneArtifacts) {
    const finalHtml = await readFile(path.join(PUBLIC, artifact.relative), "utf8");
    if (finalHtml !== artifact.html) throw new Error(`standalone_final_byte_parity_mismatch:${artifact.relative}`);
    const htmlSha256 = createHash("sha256").update(finalHtml).digest("hex");
    if (artifact.recorded_sha256 !== null && artifact.recorded_sha256 !== htmlSha256) throw new Error(`standalone_manifest_html_hash_mismatch:${artifact.relative}`);
    standaloneCspRows.push({
      relative: artifact.relative,
      route_pattern: `/${artifact.relative.replace(/index\.html$/, "*")}`,
      html_sha256: htmlSha256,
      style_csp_source: exactInlineStyleCspSource(finalHtml, artifact.relative),
      manifest_hash_bound: artifact.recorded_sha256 !== null,
    });
  }
  buildDerivations.standalone_clean_room = {
    status: "PASS_FINAL_BYTE_PARITY_AND_EXACT_STYLE_CSP",
    routes: standaloneCspRows,
  };
  const assetCacheHeaders = `\n# Wave 4 price tracker: link-dark, non-interactive, zero runtime network\n/tools/glp-1-price-tracker/*\n  X-Robots-Tag: noindex, nofollow, noarchive, nosnippet\n  Referrer-Policy: no-referrer\n  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()\n  Content-Security-Policy: default-src 'none'; script-src 'none'; connect-src 'none'; img-src 'none'; style-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'\n  Cache-Control: no-store\n\n# Weekly Price Watch feed: machine-readable, same-release, never cached\n/data/price-watch-issue.json\n  Cache-Control: no-store\n  X-Robots-Tag: noindex, nofollow, noarchive, nosnippet\n  Content-Type: application/json; charset=utf-8\n\n# Wave 4 manufacturer-offer route: link-dark, non-interactive, zero runtime network\n/tools/glp-1-savings-offer-monitor/*\n  X-Robots-Tag: noindex, nofollow, noarchive, nosnippet\n  Referrer-Policy: no-referrer\n  Permissions-Policy: camera=(), microphone=(), geolocation=(), payment=(), usb=()\n  Content-Security-Policy: default-src 'none'; script-src 'none'; connect-src 'none'; img-src 'none'; style-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'\n  Cache-Control: no-store\n\n# Round-2 versioned static assets\n/assets/site.css\n  Cache-Control: public, max-age=31536000, immutable\n/assets/site-*.css\n  Cache-Control: public, max-age=31536000, immutable\n/assets/site.js\n  Cache-Control: public, max-age=31536000, immutable\n/assets/consent.js\n  Cache-Control: public, max-age=31536000, immutable\n/assets/analytics.js\n  Cache-Control: public, max-age=31536000, immutable\n/assets/fonts/*\n  Cache-Control: public, max-age=31536000, immutable\n/assets/images/*\n  Cache-Control: public, max-age=604800, stale-while-revalidate=86400\n/assets/provider-logos/*\n  Cache-Control: public, max-age=2592000, stale-while-revalidate=86400\n\n# Wave 4 clean-room public evidence route\n/research/public-funnel-evidence/*\n  X-Robots-Tag: noindex, nofollow, noarchive, nosnippet\n  Cache-Control: no-store\n\n# Held Wave 4 descriptive dataset: static and network-dark\n/research/original-data/public-glp-1-program-pricing-sample/*\n  X-Robots-Tag: noindex, nofollow, noarchive, nosnippet\n  Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; script-src 'none'; connect-src 'none'; img-src 'none'; font-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'\n`;
  const networkVerificationHeaders = `\n# Affiliate-network ownership verification endpoints: machine-fetched, never indexed\n${networkVerificationEmitted.map((page) => `/${page.path}\n  X-Robots-Tag: noindex, nofollow, noarchive, nosnippet`).join("\n")}\n`;
  const strictNetworkDarkCsp = (styleSource) => `default-src 'none'; script-src 'none'; connect-src 'none'; img-src 'none'; font-src 'none'; style-src ${styleSource}; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'`;
  const hardenedAssetCacheHeaders = standaloneCspRows.reduce(
    (headers, row) => upsertRouteCsp(headers, row.route_pattern, strictNetworkDarkCsp(row.style_csp_source)),
    `${assetCacheHeaders.trimEnd()}\n${networkVerificationHeaders}`,
  );
  await write("_headers", `${preservedHeaders.trimEnd()}\n${hardenedAssetCacheHeaders}`);
  // These Google Ads API compliance pages are carried as predecessor BYTES, so
  // the ops@ contact address they shipped could not be fixed by editing a
  // template. 2026-08-18: rewrite the alias on the way through, from the same
  // governed map every rendered page uses, so no public surface publishes the
  // internal catch-all.
  const emailAliasMap = await readJson("email-alias-map.json");
  buildDerivations.public_email_aliases = { rewritten_preserved_bytes: [] };
  for (const [relative, bytes] of preservedGoogleAdsApiBytes) {
    const route = `/google-ads-api/${relative.replace(/index\.html$/, "")}`;
    let content = bytes;
    if (relative.endsWith(".html")) {
      const alias = allowedAliasFor(emailAliasMap, route);
      const text = bytes.toString("utf8");
      const rewritten = text
        .replaceAll(`ops@${emailAliasMap.domain}`, `${alias}@${emailAliasMap.domain}`)
        .replace(
          "<div>A first-party application operated by Tilton Group LLC.</div><div>2108 N St, Ste N, Sacramento, CA 95816</div>",
          "<div>Entity: Tilton Group LLC · California entity B20260317552 · Principal and mailing address: 2108 N St, Ste N, Sacramento, CA 95816.</div>",
        );
      if (rewritten !== text) {
        buildDerivations.public_email_aliases.rewritten_preserved_bytes.push({ route, alias });
        content = Buffer.from(rewritten, "utf8");
      }
    }
    await write(path.join("google-ads-api", relative), content);
  }
  // ULTRACODE 2026-08-21 (shell search): the header search in assets/site.js
  // consumes a LOCAL JSON index — no search endpoint, no query leaves the page.
  // Built here, after every route above has been written, from the same
  // canonical specs those routes rendered, so the index can only ever offer
  // routes this build actually produced. It adds no HTML anchors and changes
  // no page's index/noindex intent.
  await write("assets/search-index.json", buildSearchIndex({
    augmented,
    providerReviews,
    programRegistry,
    aliasReviewRoutes,
    glpArticles,
    projectedQuiz,
    costCalculator,
    priceObservedOn: priceIndex.manifest.latest_observed_on,
    trustRelatives: [...indexableTrustPages],
    legalRelatives: Object.keys(legalPageMap),
    phase3Pages: phase3Build.pages,
  }));
  const activeGoAnchors = await countPublicGoAnchors(PUBLIC);
  // GO_RUNTIME_PROMOTION_AUTHORITY_V1: missing authority means every route stays held.
  const goPromotionAuthority = loadGoPromotionAuthority(ROOT);
  const goRuntimeManifest = { schema: "affiliate-worker-staging-v3-registry", program_registry_schema: programRegistry.schema, site_profile: SITE_PROFILE, internal_fallback: "https://glpclinicscout.com/providers/", programs: deriveGoEntriesFromRegistry(programRegistry, SITE_PROFILE, contentLedger.programs, loadAffiliateLinkMap("compare"), goPromotionAuthority) };
  await writeOps("worker-manifest.staging.json", manifestJsonText(goRuntimeManifest));
  const frozenGoRuntimePath = path.join(ROOT, "functions", "go", "program-manifest.js");
  const expectedGoRuntime = renderGoRuntimeManifestModule(goRuntimeManifest);
  if (await readFile(frozenGoRuntimePath, "utf8") !== expectedGoRuntime) {
    throw new Error("frozen_build4_go_runtime_manifest_drift");
  }
  await writeOps("email-system.json", JSON.stringify(emailSequences, null, 2) + "\n");
  await writeOps("resend-email-machine.json", JSON.stringify(resendMachine, null, 2) + "\n");
  await writeOps("blog-system.json", JSON.stringify({ schema: "glp-clinic-scout-merged-blog-system-v2", focused_vertical: "GLP-1", source_schemas: [blogBatch.schema, nationalContent.schema], articles: allArticles.map((item) => ({ article_id: item.article_id, canonical_path: item.canonical_path, vertical: articleVertical(item), risk_tier: item.risk_tier, author_id: resolveArticleAuthor(item).id, review_status: reviewBrief(item, resolveArticleAuthor(item)).status, noindex: articleVertical(item) !== "GLP-1" })) }, null, 2) + "\n");
  const batchGate = {
    status: "PRODUCT_READY_PROVIDER_DEPLOY_HELD",
    money_template_revision: moneySpecs.template_revision,
    authority_addenda: ["A", "B", "C", "D", "E", "F", "G"],
    affiliate_links_live: directAffiliateProgramCount, direct_activation_sheet_programs: activationSelection.program_count, affiliate_link_mode: "transparent_direct_plus_instrumented_go_candidate", go_routes_live: 0, active_go_anchors: activeGoAnchors, lead_capture_runtime_live: false, email_sends_authorized: resendMachine.send_authorized === true && resendMachine.runtime_enabled === true, provider_sharing_authorized: false,
    clinician_required: false, honest_author_status: identity.author.status, blog_risk_model: "LOW_MEDIUM_HIGH",
    money_pages: augmented.length, quiz_pages: 1, calculator_pages: 1, quiz_candidates: projectedQuiz.candidates.length, blog_articles: allArticles.length, content_verticals: Object.fromEntries(["GLP-1", "ED", "Peptides", "Hair loss", "TRT", "Wellness"].map((vertical) => [vertical, allArticles.filter((article) => articleVertical(article) === vertical).length])), type5_pages: 0, retired_directory_states: directoryStates.length, directory_states: directoryStates,
    provider_review_pages: reviewDepth.review_page_count,
    review_depth_minimum_visible_words: reviewDepth.minimum_visible_words,
    ready6_editorial_candidate_routes: ready6Editorial.pages.length,
    ready6_candidate_state: "READY6_PRODUCTION_REVIEW_INDEXABLE",
    visual_system: visual.schema, fable_design_pass_required: true, fable_design_pass: "PASS_FABLE_FINAL_ACCEPTED_P1_REPAIRED", cross_device_gate: "PASS_LOCAL_CHROMIUM_WEBKIT",
    cwv_targets: { lcp_ms_lt: 2500, inp_ms_lt: 200, cls_lt: 0.1, field_percentile: 75 },
    resend_machine: { status: resendMachine.status, runtime_enabled: resendMachine.runtime_enabled, send_authorized: resendMachine.send_authorized, sending_subdomain: resendMachine.domain.sending_subdomain },
  };
  await writeOps("batch-gate.json", JSON.stringify(batchGate, null, 2) + "\n");
  const mustFixMarkup = await sweepMustFixMarkup(PUBLIC, paidQuiz.fragments);
  const mustFixRedirectFiles = await removeShadowedRedirectFiles(PUBLIC);
  // Final hashes belong after existing markup/route/copy normalization. Retain
  // producer-stage hashes separately; tests also pin the unchanged organic bytes.
  const finalPaidQuizContract = renderPaidQuiz(await readFile(path.join(PUBLIC, "quiz/provider-fit/index.html"), "utf8"), ASSET_VERSION);
  await writeOps("paid-quiz-build.json", JSON.stringify({ schema: "w7-paid-entrance-build-v1", phase: "after-markup-finalization", architecture: paidQuiz.architecture, candidate_count: paidQuiz.candidateCount, producer_organic_data_sha256: paidQuiz.organicDataHashes, organic_region_sha256: finalPaidQuizContract.organicRegionSha256, organic_data_sha256: finalPaidQuizContract.organicDataHashes, fragment_sha256: paidQuiz.fragmentHashes, tracking_stack: "not-included", release_ready: false }, null, 2) + "\n");
  const trustAndLegalPages = Object.keys({ ...trustPages(identity, allArticles, authorRegistry), ...leanLegalPages(framePage) }).length;
  await writeOps("build-report.json", JSON.stringify({ status: "PASS_PRODUCT_READY_PROVIDER_DEPLOY_HELD", focused_vertical: "GLP-1", indexable_routes: indexableRoutes.length, held_non_glp_articles: allArticles.length - glpArticles.length, authority_addenda: ["A", "B", "C", "D", "E", "F", "G"], money_template_revision: moneySpecs.template_revision, pages: augmented.length + 1 + allArticles.length + trustAndLegalPages + 6, money_pages: augmented.length, directory_pages: 0, retired_directory_pages: directoryStates.length, directory_landing_pages: 0, guide_library_pages: 1, blog_articles: allArticles.length, content_verticals: Object.fromEntries(["GLP-1", "ED", "Peptides", "Hair loss", "TRT", "Wellness"].map((vertical) => [vertical, allArticles.filter((article) => articleVertical(article) === vertical).length])), quiz_candidates: projectedQuiz.candidates.length, lead_api_built: true, lead_capture_runtime_live: false, email_sequence_steps: emailSequences.welcome_sequence.length, email_sends_authorized: resendMachine.send_authorized === true && resendMachine.runtime_enabled === true, active_go_anchors: activeGoAnchors, direct_affiliate_programs: directAffiliateProgramCount, live_programs: liveProgramCount, live_registry_rows: liveRowCount, direct_activation_sheet_programs: activationSelection.program_count, oauth_surface_preserved: true, price_index: { route: PRICE_INDEX_ROUTE, observation_count: priceIndex.manifest.observation_count, latest_observed_on: priceIndex.manifest.latest_observed_on, metrics: priceIndex.manifest.metrics }, surface_derivations: buildDerivations }, null, 2) + "\n");
  const buildReport = JSON.parse(await readFile(path.join(OPS, "build-report.json"), "utf8"));
  buildReport.provider_review_pages = reviewDepth.review_page_count;
  buildReport.review_depth_minimum_visible_words = reviewDepth.minimum_visible_words;
  buildReport.ready6_editorial_candidate_routes = ready6Editorial.pages.length;
  buildReport.ready6_candidate_state = "READY6_PRODUCTION_REVIEW_INDEXABLE";
  buildReport.pages += providerReviews.length + ready6Editorial.pages.length + phase3Build.pages.length;
  buildReport.phase3 = phase3Build.report;
  buildReport.mustfix_opus_final = { markup: mustFixMarkup, redirects: mustFixRedirectFiles };
  // D2j: every generated data-* value is checked before this stage can pass.
  execFileSync('python3', ['-B', path.join(ROOT, 'scripts/validate-data-attributes.py'), '--root', PUBLIC, '--receipt', path.join(OPS, 'data-attribute-invariants.json')], { stdio: 'inherit' });
  buildReport.data_attribute_invariants = { status: 'PASS', receipt: 'data-attribute-invariants.json' };
  execFileSync('python3', ['-B', path.join(ROOT, 'scripts/emit-tracking-allowed-hosts.py'), '--root', ROOT, '--check'], { stdio: 'inherit' });
  buildReport.tracking_allowed_hosts = { status: 'PASS', source: 'data/tracking-allowed-hosts.json', derived_and_checked_in_this_stage: true };
  await writeOps("build-report.json", JSON.stringify(buildReport, null, 2) + "\n");
  console.log(JSON.stringify({ status: "PASS_PRODUCT_READY_PROVIDER_DEPLOY_HELD", indexable_routes: indexableRoutes.length, focused_glp_articles: glpArticles.length, held_non_glp_articles: allArticles.length - glpArticles.length, money_pages: augmented.length, provider_review_pages: deriveReviewRoutesFromRegistry(programRegistry, SITE_PROFILE, contentLedger.programs).length, calculator_pages: 1, directory_pages_held: directoryStates.length, quiz_candidates: projectedQuiz.candidates.length, direct_affiliate_programs: directAffiliateProgramCount, live_programs: liveProgramCount, live_registry_rows: liveRowCount, active_go_anchors: activeGoAnchors, email_sends_authorized: resendMachine.send_authorized === true && resendMachine.runtime_enabled === true, provider_deploy_attempts: 0, phase3_routes: phase3Build.pages.length, phase3_deploys: 0 }));
}

// ULTRACODE 2026-08-21 — local search index for the shell search (call site
// near the end of main()). One entry per route: url/title/description/family/
// date. Indexable routes come straight from the canonical specs main() just
// rendered (money specs, registry review routes minus alias 301s, GLP article
// batch, quiz/calculator/price-index configs, the indexable trust set); the
// noindex utility pages a reader still needs (legal/policy) are included but
// carry `noindex: true` so the search UI labels them. Titles for trust/legal
// routes come from an explicit map that THROWS on an unknown route — a future
// page cannot ship an unlabeled search hit. Tally tokens inside spec copy are
// substituted by the write() funnel like every other surface.
function buildSearchIndex({ augmented, providerReviews, programRegistry, aliasReviewRoutes, glpArticles, projectedQuiz, costCalculator, priceObservedOn, trustRelatives, legalRelatives, phase3Pages = [] }) {
  const clip = (value, max = 160) => {
    const text = String(value || "").replace(/\s+/g, " ").trim();
    return text.length <= max ? text : `${text.slice(0, max - 1).replace(/\s+\S*$/, "")}…`;
  };
  const routeOf = (relative) => `/${relative.replace(/index\.html$/, "")}`;
  const TRUST_LABELS = {
    "how-we-rate/index.html": ["How Clinic Scout rates programs", "The five published criteria behind every score — commission is never one of them."],
    "how-we-make-money/index.html": ["How Clinic Scout makes money", "Affiliate relationships, and what they can and cannot change."],
    "editorial-guidelines/index.html": ["Editorial guidelines", "Risk tiers, source requirements, and what Clinic Scout refuses to claim."],
    "review-standards/index.html": ["Review standards", "How evidence is reviewed before a page publishes."],
    "authors/clinic-scout-editorial-team/index.html": ["Clinic Scout Editorial Team", "The organizational byline responsible for research and corrections."],
    "authors/izaiah-tilton/index.html": ["Izaiah Tilton, publisher", "The named publisher accountable for Clinic Scout."],
  };
  const LEGAL_LABELS = {
    "privacy/index.html": ["Privacy Policy", "What Clinic Scout collects, why, and the choices you have."],
    "terms/index.html": ["Terms of Service", "The terms that govern using Clinic Scout."],
    "privacy/cookies/index.html": ["Cookie and tracking choices", "Analytics choices and how to opt out."],
    "privacy/lead-capture/index.html": ["Contact and corrections", "How to reach Clinic Scout or ask for a page to be fixed."],
  };
  const reviewDates = new Map(providerReviews.map((review) => [review.path, review.date_modified]));
  const candidates = [
    { url: "/", title: "Compare online GLP-1 programs", description: "Dated, source-linked prices across every program Clinic Scout tracks.", family: "Start here", date: null },
    { url: "/compare/", title: "Choose a GLP-1 comparison path", description: "Start with a medication or compare providers first, with source-dated evidence kept attached.", family: "Comparisons", date: null },
    { url: "/matches/customized/", title: "Customized provider matches", description: "A compact state- and treatment-scoped shortlist built from Clinic Scout's governed provider registry.", family: "Tools", date: null },
    { url: "/quiz/provider-fit/", title: clip(projectedQuiz.h1, 110), description: clip(projectedQuiz.description), family: "Tools", date: null },
    { url: costCalculator.path, title: clip(costCalculator.h1, 110), description: clip(costCalculator.description), family: "Tools", date: null },
    { url: PRICE_INDEX_ROUTE, title: "GLP-1 Price Index", description: "A weekly index of published GLP-1 program prices with downloadable data.", family: "Tools", date: priceObservedOn || null },
    { url: "/insights/", title: "GLP-1 insights", description: "Every published source-checked GLP-1 guide in one place.", family: "Insights", date: null },
    ...augmented.map((spec) => ({ url: spec.path, title: clip(spec.h1, 110), description: clip(spec.description), family: "Comparisons", date: spec.date_modified || null })),
    ...programRegistry.programs
      .filter((program) => program.profiles?.[SITE_PROFILE]?.surfaces?.sitemap === true && !aliasReviewRoutes.has(program.review_path))
      .map((program) => ({ url: program.review_path, title: `${program.display_name} review`, description: "Independent provider review built on dated, linked sources.", family: "Reviews", date: reviewDates.get(program.review_path) || null })),
    ...glpArticles.map((article) => ({ url: article.canonical_path, title: clip(article.h1 || article.title, 110), description: clip(article.description || article.direct_answer || article.title), family: "Guides", date: article.updated_at || null })),
    ...phase3Pages.map((page) => ({
      url: page.route,
      title: clip(page.html.match(/<title>([^<]+)<\/title>/i)?.[1] || page.route, 110),
      description: clip(page.html.match(/<meta name="description" content="([^"]+)"/i)?.[1] || "Source-bound Clinic Scout page."),
      family: page.family.startsWith("pharmacy") ? "Pharmacies" : page.family.startsWith("insights") ? "Insights" : "Providers",
      date: null,
    })),
    ...trustRelatives.map((relative) => {
      const label = TRUST_LABELS[relative];
      if (!label) throw new Error(`search_index_trust_label_missing:${relative}`);
      return { url: routeOf(relative), title: label[0], description: label[1], family: "About Clinic Scout", date: null };
    }),
    ...legalRelatives.map((relative) => {
      const label = LEGAL_LABELS[relative];
      if (!label) throw new Error(`search_index_legal_label_missing:${relative}`);
      return { url: routeOf(relative), title: label[0], description: label[1], family: "Site & legal", date: null, noindex: true };
    }),
  ];
  const seen = new Set();
  const entries = [];
  for (const entry of candidates) {
    if (!/^\/(?:[a-z0-9-]+\/)*$/.test(entry.url)) throw new Error(`search_index_url_invalid:${entry.url}`);
    if (!entry.title || !entry.description) throw new Error(`search_index_entry_incomplete:${entry.url}`);
    if (seen.has(entry.url)) continue; // first spec wins; a route appears once
    seen.add(entry.url);
    entries.push(entry);
  }
  return `${JSON.stringify({ schema: "glp-clinic-scout-search-index-v1", entry_count: entries.length, entries }, null, 2)}\n`;
}

await main();