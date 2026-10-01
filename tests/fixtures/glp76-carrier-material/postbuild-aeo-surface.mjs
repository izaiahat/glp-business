// Post-build AEO surface stage (BUILD-SPEC-PACK B-04, B-07, B-08).
//
// Runs immediately after `build-staging.mjs`, over the rendered tree:
//   1. codemod the five citation/schema defect classes (B-08)
//   2. resolve a TRUTHFUL `lastmod` for every indexable URL from content-hash
//      provenance — never build time (B-04 / SEO §3)
//   3. stamp visible dateline == JSON-LD dateModified == sitemap lastmod
//   4. rewrite `public/sitemap.xml` with 100% lastmod coverage
//   5. generate `public/llms.txt`, hard-failing on any dangling URL (B-07)
//
// Provenance rules (the anti-gaming spine):
//   * a page's hash is computed with dateline/dateModified normalized OUT, so a
//     date bump alone can never change the hash — a false bump is structurally
//     impossible, not merely discouraged;
//   * a route whose hash is unchanged KEEPS its recorded lastmod;
//   * a route whose hash changed takes the release date;
//   * a missing or stale provenance entry FAILS the build unless the change was
//     DECLARED (below) or GLP_LASTMOD_PROVENANCE_REFRESH=1 is set.
//
// DECLARED-ROUTE REFRESH (Blocker H, 2026-08-20)
// ----------------------------------------------
// A wire-in build's whole purpose is to change content: adding a provider
// changes that provider's own new pages and every machine-assembled surface
// that counts or lists providers. Before this lane existed the only way past
// the staleness assertion was `GLP_LASTMOD_PROVENANCE_REFRESH=1`, which does
// not refresh the changed route — it DISABLES the assertion for every route in
// the build, including the ones where a content change would be unintended and
// this gate is the only thing that would notice.
//
// So a build may now DECLARE, up front, the routes it is changing:
//
//   GLP_LASTMOD_DECLARED_ROUTES="/ /reviews/x/ …"     (whitespace/comma list)
//   GLP_LASTMOD_DECLARED_ROUTES_FILE=/abs/decl.json   (glp.lastmod_declared_routes.v1)
//
// For EXACTLY those routes a changed hash takes a truthful new lastmod (the
// release date — the date the content really moved) and its provenance record
// is rewritten. Every other route keeps the full assertion: an undeclared
// change still throws `lastmod_provenance_stale:<route>`. A declared route
// whose hash did NOT change is left completely alone — a declaration can never
// bump a date on its own, which is the property that keeps the whole file
// honest. The blanket refresh stays exactly as it was, for the full-release
// lanes that legitimately regenerate everything, and declaring routes while it
// is on is refused rather than silently ignored.

import { createHash } from "node:crypto";
import { readFile, readdir, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { newState, remediatePage, stampDateline } from "./codemod-aeo-citation-schema.mjs";
import { renderLlmsTxt } from "../src/llms-txt.mjs";
import { READY6_ROUTES } from "../src/ready6-editorial.mjs";
import { loadCarrierArticles, assertCarrierArticleOutput } from "../src/carrier-article-retention.mjs";
import { inheritedDisclosureDates } from "../src/inherited-disclosure-dates.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const PUBLIC = path.join(ROOT, "public");
const DATA = path.join(ROOT, "data");
const OPS = path.join(ROOT, "artifacts", "ops");
const PROVENANCE_FILE = path.join(DATA, "page-content-provenance.json");
const REFRESH = process.env.GLP_LASTMOD_PROVENANCE_REFRESH === "1";

export const DECLARED_ROUTES_SCHEMA = "glp.lastmod_declared_routes.v1";

/** How a route's lastmod was decided. Recorded per route on the stage receipt. */
export const LASTMOD_ACTIONS = Object.freeze({
  FROZEN: "frozen_route_declared_date",
  UNCHANGED: "unchanged_kept_recorded",
  RUNTIME_ONLY: "declared_runtime_only_kept_recorded",
  DECLARED: "declared_change_refreshed",
  BLANKET: "blanket_refresh",
});

/**
 * Parse a declared-route list. Refuses anything that is not an absolute site
 * route, so a stray token can never widen the refresh to something unintended.
 */
export function parseDeclaredRoutes(raw) {
  const routes = new Set();
  for (const token of String(raw || "").split(/[\s,]+/)) {
    if (!token) continue;
    if (!/^\/[A-Za-z0-9\-._~/]*$/.test(token)) throw new Error(`lastmod_declared_route_invalid:${token}`);
    routes.add(token);
  }
  return routes;
}

/** Read the declaration from env, or from an explicit declaration file. */
export async function readDeclaredRoutes(env = process.env, readTextFile = (file) => readFile(file, "utf8")) {
  const inline = env.GLP_LASTMOD_DECLARED_ROUTES;
  const file = env.GLP_LASTMOD_DECLARED_ROUTES_FILE;
  if (inline && file) throw new Error("lastmod_declared_routes_conflict");
  if (inline) return { routes: parseDeclaredRoutes(inline), source: "env:GLP_LASTMOD_DECLARED_ROUTES" };
  if (!file) return { routes: new Set(), source: null };
  let document;
  try { document = JSON.parse(await readTextFile(file)); }
  catch { throw new Error(`lastmod_declared_routes_file_unreadable:${file}`); }
  if (document?.schema !== DECLARED_ROUTES_SCHEMA) throw new Error(`lastmod_declared_routes_schema_invalid:${file}`);
  if (!Array.isArray(document.routes)) throw new Error(`lastmod_declared_routes_malformed:${file}`);
  const routes = parseDeclaredRoutes(document.routes.join(" "));
  const materialDates = new Map();
  if (document.material_dates !== undefined) {
    if (!document.material_dates || typeof document.material_dates !== "object" || Array.isArray(document.material_dates)) throw new Error("lastmod_material_dates_invalid");
    for (const [route, date] of Object.entries(document.material_dates)) {
      if (!routes.has(route) || !/^\d{4}-\d{2}-\d{2}$/.test(date) || new Date(date + "T00:00:00Z").toISOString().slice(0, 10) !== date) throw new Error(`lastmod_material_date_invalid:${route}`);
      materialDates.set(route, date);
    }
  }
  const editorialDates = new Map();
  if (document.editorial_dates !== undefined) {
    if (!document.editorial_dates || typeof document.editorial_dates !== 'object' || Array.isArray(document.editorial_dates)) throw new Error('editorial_dates_invalid');
    for (const [route, date] of Object.entries(document.editorial_dates)) {
      if (!routes.has(route) || !/^\d{4}-\d{2}-\d{2}$/.test(date) || new Date(date+'T00:00:00Z').toISOString().slice(0,10) !== date) throw new Error(`editorial_date_invalid:${route}`);
      editorialDates.set(route, date);
    }
  }
  return { routes, materialDates, editorialDates, source: `file:${file}` };
}

/**
 * THE gate, as one pure decision per route.
 *
 * `declared` only ever converts a THROW into a truthful release-dated refresh.
 * It cannot touch an unchanged route, it cannot freeze a date, and it is not
 * consulted at all when the hash still matches what is recorded.
 */
/**
 * Write a file that may still be a HARDLINK into an immutable sealed release.
 *
 * A machine candidate is materialized from its sealed predecessor with
 * hardlinks, so any file this build has not itself rewritten is still the
 * predecessor's inode — `root:root 0444`. `data/page-content-provenance.json` is
 * exactly such a file: until the declared-route lane existed nothing ever wrote
 * it outside the blanket-refresh lane, so the first machine build to refresh a
 * declared route died `EACCES` on it (observed live, 2026-08-20T21:15Z).
 *
 * Unlinking first is the whole fix, and it is the SAFE direction: the write then
 * lands on a brand-new inode belonging to this candidate, and the sealed
 * predecessor's bytes are untouched. Writing in place is what must never happen.
 */
async function writeDetached(file, contents) {
  await rm(file, { force: true });
  await writeFile(file, contents);
}

export function resolveRouteLastmod({ route, hash, recorded, frozenDate = null, refresh = false, declared = false, releaseDate, runtimeProof = null, allowUndeclaredRuntimeProof = false }) {
  if (frozenDate !== null) {
    if (!frozenDate) throw new Error(`frozen_route_declares_no_date_modified:${route}`);
    return { lastmod: frozenDate, action: LASTMOD_ACTIONS.FROZEN };
  }
  if (recorded && recorded.content_sha256 === hash) {
    return { lastmod: recorded.lastmod, action: LASTMOD_ACTIONS.UNCHANGED };
  }
  if (refresh) return { lastmod: releaseDate, action: LASTMOD_ACTIONS.BLANKET };
  if ((declared || allowUndeclaredRuntimeProof) && runtimeProof?.route === route && runtimeProof.current_content_sha256 === hash && runtimeProof.restored_content_sha256 === recorded?.content_sha256 && runtimeProof.recorded_lastmod === recorded?.lastmod) {
    return { lastmod: recorded.lastmod, action: LASTMOD_ACTIONS.RUNTIME_ONLY };
  }
  if (declared) return { lastmod: releaseDate, action: LASTMOD_ACTIONS.DECLARED };
  if (!recorded) throw new Error(`lastmod_provenance_missing:${route}`);
  throw new Error(`lastmod_provenance_stale:${route}`);
}

// Explicit source-witnessed page edits are distinct from catalog/evidence dates.
// A route declaration alone cannot advance a date; material_dates is a separate,
// report-derived list reviewed by the integrating owner, never a blanket refresh.
export function resolveMaterialDate(route, materialDate, resolved, releaseDate) {
  if (!materialDate) return resolved;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(materialDate) || materialDate > releaseDate) throw new Error(`lastmod_material_date_future_or_invalid:${route}`);
  if (materialDate <= resolved.lastmod) return resolved;
  return { lastmod: materialDate, action: "declared_material_page_edit" };
}

// The article catalog, not a shared-shell release, owns editorial freshness.
// Call AFTER resolveRouteLastmod: this must never waive a stale content hash.
// K6c: accepted inherited material dates, not change declarations.
const CARRIER_ACCEPTED_MATERIAL_DATES = Object.freeze({"/guides/found-vs-trimi-insurance-self-pay/": "2026-09-09", "/guides/gala-vs-shed/": "2026-09-09", "/guides/glp-1-insurance-support-vs-coverage/": "2026-09-09", "/guides/trimi-vs-yourera/": "2026-09-09"});
export function resolveCatalogDate(route, article, renderedDate, resolved, materialDate = null, releaseDate = null) {
  if (!article) return resolved;
  if (article.canonical_path !== route || !/^\d{4}-\d{2}-\d{2}$/.test(article.updated_at || "")) {
    throw new Error(`catalog_date_invalid:${route}`);
  }
  if (renderedDate !== article.updated_at && !(materialDate === renderedDate && materialDate > article.updated_at && /^\d{4}-\d{2}-\d{2}$/.test(materialDate) && releaseDate && materialDate <= releaseDate)) throw new Error(`catalog_render_date_mismatch:${route}`);
  return { lastmod: article.updated_at, action: "catalog_owned_editorial_date" };
}

// The Price Index is byte-frozen against the weekly observation chain and is
// re-asserted from preserved bytes by build-staging; its AEO fixes ship through
// `src/price-index.mjs` + GLP_PRICE_INDEX_REFRESH, never through this sweep
// (SPEC-EDITORIAL-MONETIZATION §3.8, DEPLOY-RUNBOOK freeze convention).
// Byte-frozen routes. Each is asserted byte-identical to its own renderer by a
// dedicated validator, so it cannot be post-processed here — the AEO contract is
// satisfied INSIDE those renderers instead (`src/price-index.mjs`,
// `src/ready6-editorial.mjs`). Their sitemap lastmod is taken from the date the
// page itself declares, and a content change without a matching declared-date
// move is a build failure (below), so the freeze buys no freshness dishonesty.
const CODEMOD_EXCLUDED = new Set([
  "glp-1-price-index/index.html",
  // Clean-room / link-dark routes: asserted byte-identical to their renderers and
  // required to carry NO script tag at all (that is their whole safety property),
  // so an injected JSON-LD block would break the contract it is meant to serve.
  // Verified defect-free by the sensor before exclusion.
  "research/public-funnel-evidence/index.html",
  "research/original-data/public-glp-1-program-pricing-sample/index.html",
  "tools/glp-1-price-tracker/index.html",
  "tools/glp-1-savings-offer-monitor/index.html",
  ...READY6_ROUTES.map((route) => `${route.replace(/^\//, "")}index.html`),
]);

function declaredDateModified(html) {
  const dates = [...html.matchAll(/"dateModified"\s*:\s*"(\d{4}-\d{2}-\d{2})"/g)].map((match) => match[1]);
  if (dates.length === 0) return null;
  return dates.sort().at(-1);
}

async function walk(directory, prefix = "") {
  const out = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const relative = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (entry.isDirectory()) out.push(...await walk(path.join(directory, entry.name), relative));
    else out.push(relative);
  }
  return out;
}

export function normalizedHash(html) {
  // Volatile-by-construction fragments are normalized out so the hash tracks
  // real content, not the release stamp.
  const canonical = html
    .replace(/\/assets\/analytics(?:\.[a-f0-9]{16})?\.js(?:\?v=[a-f0-9]{16})?/g, "/assets/analytics.js?v=ASSET_VERSION")
    .replace(/\?v=[a-f0-9]{16}/g, "?v=ASSET_VERSION")
    .replace(/<time datetime="\d{4}-\d{2}-\d{2}"([^>]*)>[^<]*<\/time>/g, '<time datetime="DATE"$1>DATE</time>')
    .replace(/"dateModified"\s*:\s*"\d{4}-\d{2}-\d{2}"/g, '"dateModified":"DATE"')
    .replace(/"datePublished"\s*:\s*"\d{4}-\d{2}-\d{2}"/g, '"datePublished":"DATE"');
  return createHash("sha256").update(canonical).digest("hex");
}

// D2i: keep the legacy normalizedHash algorithm, not a guessed v2 migration.
// A final predecessor page can contain downstream transforms absent at stage6.
// Borrow ONLY its exact runtime blocks and restore them into CURRENT stage6
// bytes. The resulting legacy hash must equal the recorded stage6 preimage.
const rawSha256 = (bytes) => createHash('sha256').update(bytes).digest('hex');
export async function loadRuntimePredecessor(env = process.env, readText = (file) => readFile(file, 'utf8')) {
  const root = env.GLP_LASTMOD_RUNTIME_PREDECESSOR_ROOT;
  const manifestPath = env.GLP_LASTMOD_RUNTIME_PREDECESSOR_MANIFEST;
  const manifestSha256 = env.GLP_LASTMOD_RUNTIME_PREDECESSOR_MANIFEST_SHA256;
  if (!root && !manifestPath && !manifestSha256) return null;
  if (!root || !path.isAbsolute(root) || !manifestPath || !path.isAbsolute(manifestPath) || !/^[a-f0-9]{64}$/.test(manifestSha256 || '')) throw new Error('runtime_predecessor_inputs');
  const manifestBytes = await readText(manifestPath);
  if (rawSha256(manifestBytes) !== manifestSha256) throw new Error('runtime_predecessor_manifest_hash');
  const entries = JSON.parse(manifestBytes), files = new Map();
  if (!Array.isArray(entries)) throw new Error('runtime_predecessor_manifest_schema');
  for (const entry of entries) {
    if (typeof entry.path !== 'string' || path.isAbsolute(entry.path) || entry.path.split('/').includes('..') || files.has(entry.path) || !/^[a-f0-9]{64}$/.test(entry.sha256 || '')) throw new Error('runtime_predecessor_manifest_entry');
    files.set(entry.path, entry.sha256);
  }
  async function verified(relative) {
    const text = await readText(path.join(root, relative));
    if (rawSha256(text) !== files.get(relative)) throw new Error(`runtime_predecessor_file_hash:${relative}`);
    return text;
  }
  const source = await verified('scripts/postbuild-aeo-surface.mjs');
  const oldNormalizer = source.match(/export (function normalizedHash\(html\) \{[\s\S]*?\n\})/)?.[1];
  if (oldNormalizer !== normalizedHash.toString()) throw new Error('runtime_predecessor_hash_algorithm_mismatch');
  const provenance = JSON.parse(await verified('data/page-content-provenance.json'));
  if (provenance.schema !== 'glp-page-content-provenance-v1' || !provenance.pages) throw new Error('runtime_predecessor_provenance_schema');
  return {manifestSha256, provenance, async readPage(route) {
    if (!/^\/(?:[A-Za-z0-9_-]+\/)*$/.test(route)) throw new Error('runtime_predecessor_route_invalid');
    return verified('public/' + route.slice(1) + 'index.html');
  }};
}

export async function runtimeOnlyLastmodProof({route, html, recorded, declared, predecessor}) {
  if (!declared || !predecessor || !recorded) return null;
  const oldRecord = predecessor.provenance.pages[route];
  if (!oldRecord || oldRecord.content_sha256 !== recorded.content_sha256 || oldRecord.lastmod !== recorded.lastmod) return null;
  const before = await predecessor.readPage(route);
  let restored = html;
  // Reject ambiguous/missing blocks. Never strip arbitrary scripts, markup,
  // copy, schema or metadata to manufacture equivalence.
  const patterns = [
    /<script data-candidate-export-policy>window\.GCS_EXPORT_POLICY=Object\.freeze\([^<]*\);<\/script>/g,
    ...['ANALYTICS','POSTHOG','CLARITY'].map(name => new RegExp(`window\\.GCS_${name}=\\{[^<]*?\\};`, 'g')),
  ];
  for (const pattern of patterns) {
    const old = [...before.matchAll(pattern)], next = [...restored.matchAll(pattern)];
    if (old.length !== 1 || next.length !== 1) return null;
    restored = restored.replace(pattern, () => old[0][0]);
  }
  if (restored === html || normalizedHash(restored) !== recorded.content_sha256) return null;
  return {route, predecessor_manifest_sha256:predecessor.manifestSha256,
    predecessor_html_sha256:rawSha256(before), current_content_sha256:normalizedHash(html),
    restored_content_sha256:normalizedHash(restored), recorded_lastmod:recorded.lastmod};
}

function parseRobotsDisallows(robots) {
  const disallows = [];
  let inWildcard = false;
  for (const raw of robots.split("\n")) {
    const line = raw.trim();
    if (/^user-agent:/i.test(line)) inWildcard = line.split(":")[1].trim() === "*";
    else if (inWildcard && /^disallow:/i.test(line)) {
      const value = line.slice(line.indexOf(":") + 1).trim();
      if (value) disallows.push(value);
    }
  }
  return disallows;
}

function routesFromSitemap(xml) {
  return [...xml.matchAll(/<loc>https:\/\/glpclinicscout\.com([^<]*)<\/loc>/g)].map((match) => match[1]);
}

function buildSitemap(entries) {
  const locations = entries.map(({ route, lastmod }) =>
    `<url><loc>https://glpclinicscout.com${route}</loc><lastmod>${lastmod}</lastmod></url>`).join("");
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${locations}</urlset>\n`;
}

async function main() {
  const retainedCarrierArticles = loadCarrierArticles(ROOT);
  const releaseDate = process.env.GLP_RELEASE_DATE || "2026-08-19";
  if (!/^\d{4}-\d{2}-\d{2}$/.test(releaseDate)) throw new Error("release_date_invalid");
  const { routes: declaredRoutes, source: declaredSource, materialDates = new Map(), editorialDates = new Map() } = await readDeclaredRoutes();
  // The two lanes are mutually exclusive by construction: the blanket refresh
  // means "everything moved", a declaration means "exactly these moved". A build
  // asserting both is a build that does not know what it changed.
  if (REFRESH && declaredRoutes.size) throw new Error("lastmod_declared_routes_with_blanket_refresh");

  const sitemapXml = await readFile(path.join(PUBLIC, "sitemap.xml"), "utf8");
  const routes = routesFromSitemap(sitemapXml);
  if (routes.length === 0) throw new Error("sitemap_routes_empty");

  // Scope: EVERY page in the tree, not just the indexable ones. Deploy sensor
  // check #17 scans the whole `public/` tree, and it is right to: a held page
  // today is an indexable page after one flag change, and a duplicate Article
  // node that survives on 25 held guides is 25 defects waiting to be published.
  // Only the byte-frozen routes are skipped, and their contract is satisfied
  // inside their own renderers instead.
  const state = newState();
  const htmlByFile = new Map();
  // `/google-ads-api/**` is carried through as PRESERVED BYTES from the
  // predecessor release (build-staging reads it out of `public/` and writes it
  // back). Rewriting it here would bake the edit into every future release's
  // "preserved" bytes and break the compliance-page byte contract its own test
  // asserts. It is noindex, has no citations, and no answer engine reads it.
  const allPages = (await walk(PUBLIC))
    .filter((relative) => relative.endsWith("index.html") && !relative.startsWith("google-ads-api/"));
  for (const relative of allPages) {
    const before = await readFile(path.join(PUBLIC, relative), "utf8");
    state.pages += 1;
    const retainedCarrierArticle = retainedCarrierArticles.get(relative);
    if (retainedCarrierArticle) assertCarrierArticleOutput(retainedCarrierArticle, before);
    const after = CODEMOD_EXCLUDED.has(relative) || retainedCarrierArticle ? before : remediatePage(before, state);
    if (after !== before) state.changed += 1;
    htmlByFile.set(relative, after);
  }

  let provenance = { schema: "glp-page-content-provenance-v1", pages: {} };
  try { provenance = JSON.parse(await readFile(PROVENANCE_FILE, "utf8")); } catch { if (!REFRESH) throw new Error("lastmod_provenance_file_missing"); }
  if (provenance.schema !== "glp-page-content-provenance-v1") throw new Error("lastmod_provenance_schema_invalid");
  const runtimePredecessor = await loadRuntimePredecessor();
  const runtimeOnlyPreserved = [];

  const nextProvenance = { schema: provenance.schema, generated_for_release: releaseDate, pages: {} };
  const catalogs = await Promise.all(["blog/content-batch.json", "blog/content-national-wow-additions.json"]
    .map(async (file) => JSON.parse(await readFile(path.join(DATA, file), "utf8"))));
  const articles = new Map();
  for (const article of catalogs.flatMap((catalog) => catalog.articles)) {
    if (articles.has(article.canonical_path)) throw new Error(`catalog_duplicate_route:${article.canonical_path}`);
    articles.set(article.canonical_path, article);
  }
  const catalogReconciled = [];
  const materialReconciled = [];
  const entries = [];
  const declaredRefreshed = [];
  const declaredUnchanged = [];
  for (const route of [...routes].sort()) {
    const relative = `${route.replace(/^\//, "")}index.html`;
    const html = htmlByFile.get(relative);
    if (html === undefined) throw new Error(`sitemap_route_absent_from_tree:${route}`);
    const hash = normalizedHash(html);
    const recorded = provenance.pages?.[route];
    const retainedCarrierArticle = retainedCarrierArticles.get(relative);
    if (retainedCarrierArticle) {
      // Preserve the manifest-bound article and editorial date; only the
      // consent shell varies with the release stage.
      assertCarrierArticleOutput(retainedCarrierArticle, html);
      if (recorded?.lastmod !== declaredDateModified(html) || articles.get(route)?.updated_at !== recorded.lastmod) throw new Error('carrier_article_date_drift');
      nextProvenance.pages[route] = recorded;
      entries.push({ route, relative, lastmod: recorded.lastmod, action: 'carrier_exact_byte_and_provenance_retention' });
      continue;
    }
    const frozen = CODEMOD_EXCLUDED.has(relative);
    // A byte-frozen route's date is owned by the renderer that owns its bytes
    // (`src/price-index.mjs` from the weekly observation chain,
    // `src/ready6-editorial.mjs` from its adjudication record). Taking the date
    // the page itself publishes keeps visible date == schema date == lastmod
    // without this stage reaching into a freeze it is not allowed to touch.
    const declared = declaredRoutes.has(route);
    const runtimeProof = !frozen && hash !== recorded?.content_sha256
      ? await runtimeOnlyLastmodProof({route,html,recorded,declared: declared || Boolean(runtimePredecessor),predecessor:runtimePredecessor}) : null;
    const resolved = resolveRouteLastmod({
      route,
      hash,
      recorded,
      frozenDate: frozen ? declaredDateModified(html) : null,
      refresh: REFRESH,
      declared,
      releaseDate,
      runtimeProof,
      allowUndeclaredRuntimeProof: Boolean(runtimePredecessor),
    });
    const materialDate = materialDates.get(route) || inheritedDisclosureDates(route, articles.get(route))?.lastmod || CARRIER_ACCEPTED_MATERIAL_DATES[route] || null;
    const ownedDate = resolveCatalogDate(route, articles.get(route), declaredDateModified(html), resolved, materialDate, releaseDate);
    const { lastmod, action } = frozen ? resolved : resolveMaterialDate(route, materialDate, ownedDate, releaseDate);
    if (action === "declared_material_page_edit") materialReconciled.push(route);
    if (articles.has(route) && recorded?.lastmod !== lastmod) catalogReconciled.push(route);
    if (resolved.action === LASTMOD_ACTIONS.RUNTIME_ONLY) runtimeOnlyPreserved.push(runtimeProof);
    if (declared && action === LASTMOD_ACTIONS.DECLARED) declaredRefreshed.push(route);
    else if (declared) declaredUnchanged.push(route);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(lastmod)) throw new Error(`lastmod_invalid:${route}`);
    nextProvenance.pages[route] = { content_sha256: hash, lastmod };
    entries.push({ route, relative, lastmod, action });
  }
  // A declaration naming a route the tree does not publish authorizes nothing —
  // it can only ever be a no-op — but it IS reported, because a declaration that
  // silently matched nothing is how the next lane discovers its deriver drifted.
  const declaredAbsent = [...declaredRoutes].filter((route) => !routes.includes(route)).sort();

  const lastmodByFile = new Map(entries.map((entry) => [entry.relative, entry.lastmod]));
  for (const relative of allPages) {
    if (CODEMOD_EXCLUDED.has(relative)) continue;
    if (retainedCarrierArticles.has(relative)) continue;
    // Indexable pages take the provenance lastmod. A held page has no sitemap
    // date to be honest about, so it keeps the date it already publishes — the
    // stamp only makes the visible dateline agree with the schema date.
    const lastmod = lastmodByFile.get(relative) || declaredDateModified(htmlByFile.get(relative));
    if (!lastmod) continue;
    // Body editorial freshness is independent of disclosure/runtime sitemap freshness.
    const route = '/'+relative.replace(/index\.html$/, '');
    const editorialDate = editorialDates.get(route) || inheritedDisclosureDates(route, articles.get(route))?.editorialDate;
    if (editorialDate && (!articles.has(route) || editorialDate > releaseDate)) throw new Error(`editorial_date_unowned_or_future:${route}`);
    htmlByFile.set(relative, stampDateline(htmlByFile.get(relative), editorialDate || lastmod, state));
  }
  for (const [relative, html] of htmlByFile) await writeFile(path.join(PUBLIC, relative), html);
  await writeFile(path.join(PUBLIC, "sitemap.xml"), buildSitemap(entries));
  // Persist actual declared/blanket changes or an exact catalog-date repair.
  // A repeated catalog reconciliation is a no-op; a declaration on its own
  // still cannot rewrite provenance or advance a source-owned editorial date.
  const provenanceWritten = REFRESH || declaredRefreshed.length > 0 || catalogReconciled.length > 0 || runtimeOnlyPreserved.length > 0 || materialReconciled.length > 0;
  if (provenanceWritten) await writeDetached(PROVENANCE_FILE, `${JSON.stringify(nextProvenance, null, 2)}\n`);

  const robots = await readFile(path.join(PUBLIC, "robots.txt"), "utf8");
  const disallows = parseRobotsDisallows(robots);
  const indexableRoutes = new Set(entries.map((entry) => entry.route));
  const treeFiles = new Set(await walk(PUBLIC));
  const llmsConfig = JSON.parse(await readFile(path.join(DATA, "llms-priority.json"), "utf8"));
  const llms = renderLlmsTxt(llmsConfig, {
    indexableRoutes,
    isDisallowed: (route) => disallows.some((rule) => route.startsWith(rule)),
    hasFile: (relative) => treeFiles.has(relative),
  });
  for (const route of indexableRoutes) {
    const html = htmlByFile.get(`${route.replace(/^\//, "")}index.html`);
    if (/<meta name="robots" content="[^"]*noindex/i.test(html)) throw new Error(`sitemap_route_noindex:${route}`);
  }
  await writeFile(path.join(PUBLIC, "llms.txt"), llms);

  const receipt = {
    schema: "glp-aeo-surface-stage-v1",
    status: "PASS",
    release_date: releaseDate,
    provenance_refresh: REFRESH,
    provenance_written: provenanceWritten,
    catalog_dates_reconciled: catalogReconciled,
    material_page_dates_reconciled: materialReconciled,
    runtime_only_preserved: runtimeOnlyPreserved,
    runtime_predecessor_manifest_sha256: runtimePredecessor?.manifestSha256 || null,
    declared_routes_source: declaredSource,
    declared_routes: [...declaredRoutes].sort(),
    declared_routes_refreshed: [...declaredRefreshed].sort(),
    declared_routes_unchanged: [...declaredUnchanged].sort(),
    declared_routes_absent_from_sitemap: declaredAbsent,
    sitemap_urls: entries.length,
    sitemap_lastmod_coverage: entries.length,
    lastmod_at_release_date: entries.filter((entry) => entry.lastmod === releaseDate).length,
    llms_txt_bytes: Buffer.byteLength(llms),
    codemod: state,
  };
  await writeFile(path.join(OPS, "aeo-surface-stage.json"), `${JSON.stringify(receipt, null, 2)}\n`);
  console.log(JSON.stringify(receipt));
}

// Entry-point guard: the decision functions above are unit-testable only if a
// test may import this module without running a whole build over `public/`.
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) await main();
