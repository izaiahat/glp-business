// Registered caller of the unchanged AEO provenance producer/detector.
// Ruling36: compare each emitted page with its verified predecessor, never a build claim.
import { createHash, randomUUID } from 'node:crypto';
import { readFile, mkdir, writeFile, rm, open } from 'node:fs/promises';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { normalizedHash, loadRuntimePredecessor } from './postbuild-aeo-surface.mjs';
import { loadHistoricalCarrierArticles, assertCarrierArticleOutput } from '../src/carrier-article-retention.mjs';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const BASE_DIR = path.join(ROOT, 'data/predecessors/content-base');
const BASE_POINTER = path.join(BASE_DIR, 'current-content-base.json');
const BASE_POINTER_SHA256 = 'f2ff608d5c1d885cd48f861ed636c64e9324cdf91e9131448388575026499aee';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const SOURCES = ['scripts/build-staging.mjs', 'src/phase3-page-graph.mjs',
  'src/provider-review.mjs', 'src/money-page.mjs', 'scripts/wave2-repairs.mjs',
  'src/pass2-review-hub.mjs', 'src/wave2-p1-surfaces.mjs',
  'src/matches-customized.mjs', 'src/compounded-category.mjs', 'src/paid-landing/index.html'];

export async function captureLastmodEmission(root, routes) {
  // Retained for build-staging's diagnostic report; prepareLastmod never reads it.
  const sources = Object.fromEntries(await Promise.all([...SOURCES, 'data/blog/content-batch.json'].map(async source =>
    [source, sha(await readFile(path.join(root, source)))])));
  const pages = Object.fromEntries(await Promise.all(routes.map(async route =>
    [route, sha(await readFile(path.join(root, 'public', route.slice(1), 'index.html')))])));
  return { sources, pages };
}

export async function prepareLastmod(root = ROOT, { readPredecessor = readFile } = {}) {
  const pointerBytes = await readFile(BASE_POINTER);
  if (sha(pointerBytes) !== BASE_POINTER_SHA256) throw new Error('lastmod_base_pointer_drift');
  const pointer = JSON.parse(pointerBytes);
  const releaseRoot = path.join(root, 'data/predecessors/content-base/release');
  // The historical pointer and deployment manifest are retained byte-for-byte.
  // Only their access paths are local; their release identity and SHA pins stay intact.
  const receiptBytes = await readFile(path.join(BASE_DIR, 'final-receipt.json'));
  if (sha(receiptBytes) !== pointer.final_receipt.sha256) throw new Error('lastmod_base_receipt_drift');
  const receipt = JSON.parse(receiptBytes);
  if (receipt.status !== 'PASS_PRODUCTION_TERMINAL') throw new Error(`lastmod_base_receipt_not_terminal:${receipt.status}`);
  // The id is nested under `deployment`, not top-level; a top-level read is undefined and would
  // refuse every build.
  if (receipt.deployment?.deployment_id !== pointer.deployment_id) throw new Error('lastmod_base_receipt_deployment_mismatch');
  const manifestBytes = await readFile(path.join(BASE_DIR, 'deployment-manifest.json'));
  if (sha(manifestBytes) !== pointer.manifest.sha256) throw new Error('lastmod_source_manifest_drift');
  const manifest = JSON.parse(manifestBytes);
  if (manifest.release_root !== pointer.release_root) throw new Error('lastmod_source_root_mismatch');
  const entries = manifest.release_rows;
  // Reuse the installed verified predecessor loader and its algorithm/pair checks.
  // This projection carries the pinned manifest, not a material-change witness.
  const ops = path.join(root, 'artifacts/ops');
  await mkdir(ops, { recursive: true });
  const runtimeFile = path.join(ops, 'lastmod-runtime-predecessor.json');
  const runtimeBytes = JSON.stringify(entries) + '\n';
  await writeFile(runtimeFile, runtimeBytes);
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
    !key.startsWith('GLP_LASTMOD_') && key !== 'GLP_RELEASE_DATE'));
  Object.assign(env, {
    GLP_RELEASE_DATE: new Date().toISOString().slice(0, 10),
    GLP_LASTMOD_RUNTIME_PREDECESSOR_ROOT: releaseRoot,
    GLP_LASTMOD_RUNTIME_PREDECESSOR_MANIFEST: runtimeFile,
    GLP_LASTMOD_RUNTIME_PREDECESSOR_MANIFEST_SHA256: sha(runtimeBytes),
  });
  const predecessor = await loadRuntimePredecessor(env, async file =>
    (await (file.startsWith(releaseRoot + path.sep) ? readPredecessor(file) : readFile(file))).toString('utf8'));
  const files = new Set(entries.map(row => row.path));
  // The verified predecessor predates the six-field rebind. Keep its original
  // whole-record contract; current emission still requires authenticated inputs.
  const carriers = loadHistoricalCarrierArticles(releaseRoot);
  // Reconstruct the detector's cache from verified final predecessor bytes.
  // Neither the candidate cache nor a legacy stage6 hash is rendered-output evidence.
  const provenance = { ...predecessor.provenance, pages: { ...predecessor.provenance.pages } };
  const sitemap = await readFile(path.join(root, 'public/sitemap.xml'), 'utf8');
  const routes = [...sitemap.matchAll(/<loc>https:\/\/glpclinicscout\.com([^<]*)<\/loc>/g)].map(match => match[1]);
  if (!routes.length) throw new Error('sitemap_routes_empty');
  const comparisons = [], changed = [], firstPublications = [], blocked = [];
  for (const route of routes) {
    if (!/^\/(?:[A-Za-z0-9_-]+\/)*$/.test(route)) throw new Error(`lastmod_route_invalid:${route}`);
    const relative = `public/${route.slice(1)}index.html`;
    if (!files.has(relative)) {
      // Absence from both authenticated predecessor inventories is first
      // publication, not a comparison against fabricated predecessor bytes.
      if (Object.hasOwn(predecessor.provenance.pages, route)) {
        blocked.push(`lastmod_predecessor_unavailable:${relative}:not_in_manifest`);
        continue;
      }
      const html = await readFile(path.join(root, relative));
      comparisons.push({ route, before_sha256: null, after_sha256: sha(html), differs: true, first_publication: true });
      changed.push(route);
      firstPublications.push(route);
      continue;
    }
    let previousHtml;
    try { previousHtml = await predecessor.readPage(route); }
    catch (error) {
      blocked.push(error.code ? `lastmod_predecessor_unavailable:${relative}:${error.code}` : `lastmod_predecessor_file_drift:${relative}:${error.message}`);
      continue;
    }
    const recorded = predecessor.provenance.pages[route];
    if (!/^\d{4}-\d{2}-\d{2}$/.test(recorded?.lastmod || '') || recorded.lastmod > env.GLP_RELEASE_DATE) {
      blocked.push(`lastmod_predecessor_date_unavailable:${relative}`);
      continue;
    }
    const html = await readFile(path.join(root, relative));
    const differs = !html.equals(Buffer.from(previousHtml));
    const carrier = carriers.get(relative.slice('public/'.length));
    if (carrier) assertCarrierArticleOutput(carrier, html);
    else provenance.pages[route] = { content_sha256: normalizedHash(previousHtml), lastmod: recorded.lastmod };
    comparisons.push({ route, before_sha256: sha(previousHtml), after_sha256: sha(html), differs });
    if (differs) changed.push(route);
  }
  const comparison = { total: routes.length, changed: changed.length, unchanged: comparisons.length - changed.length,
    first_publications: firstPublications.length, blocked, predecessor_manifest_sha256: pointer.manifest.sha256 };
  if (blocked.length) throw Object.assign(new Error(blocked.join('; ')), { comparison });
  // The existing detector receives ONLY this process's comparison via its private
  // stdin pipe. No declared-source file, journal, build report or inherited env is read.
  env.GLP_LASTMOD_DECLARED_ROUTES_FILE = '/dev/stdin';
  const input = JSON.stringify({ schema: 'glp.lastmod_declared_routes.v1', routes: changed,
    material_dates: Object.fromEntries(changed.map(route => [route, env.GLP_RELEASE_DATE])) });
  return { env, input, comparison, comparisons, provenance };
}

export async function openComparisonInput(root, input) {
  // /dev/stdin cannot reopen Node's socket-backed spawn pipe on this host.
  // Unlink an empty regular file BEFORE writing: no named witness can be supplied.
  const file = path.join(root, 'artifacts/ops', `.lastmod-input-${randomUUID()}`);
  const handle = await open(file, 'wx+', 0o600);
  try { await rm(file); await handle.writeFile(input); return handle; }
  catch (error) { await handle.close(); throw error; }
}

async function main() {
  const inputs = await prepareLastmod();
  console.log(JSON.stringify({ lastmod_render_comparison: inputs.comparison }));
  // Same hardlink-detach write used by postbuild-aeo-surface's local writer.
  const provenanceFile = path.join(ROOT, 'data/page-content-provenance.json');
  await rm(provenanceFile, { force: true });
  await writeFile(provenanceFile, JSON.stringify(inputs.provenance) + '\n');
  const input = await openComparisonInput(ROOT, inputs.input);
  try {
    const result = spawnSync(process.execPath, ['scripts/postbuild-aeo-surface.mjs'], {
      cwd: ROOT, env: inputs.env, stdio: [input.fd, 'inherit', 'inherit'],
    });
    if (result.error) throw result.error;
    process.exitCode = result.status ?? 1;
    if (result.status === 0) {
      const reportPath = path.join(ROOT, 'artifacts/ops/build-report.json');
      const report = JSON.parse(await readFile(reportPath, 'utf8'));
      report.export_policy_surface_sha256 = sha(await readFile(path.join(ROOT, 'public/providers/index.html')));
      await writeFile(reportPath, JSON.stringify(report, null, 2) + '\n');
    }
  } finally { await input.close(); }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => { console.error(JSON.stringify({ status: 'REFUSED', error: error.message, comparison: error.comparison })); process.exitCode = 1; });
}
