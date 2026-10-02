// program-registry.mjs SHA-256: 05724dc3d63f6424c772261ebf8f8798421efc7542a3c5d8c5ce9e4c31341bd8
// Bounded native projection fixture, NOT a whole build/publication receipt.
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { validateRegistryShape } from './affiliate_terms_registry_validator.mjs';
import { AGGREGATE_SURFACES, deriveIdentityGroups, assertUniqueRenderIdentity } from './glp331_provider_identity.mjs';
const SURFACES = ['compare', 'quiz', 'calculator', 'review', 'go', 'sitemap'];
function profileFor(program, profileId) { return program?.profiles?.[profileId] || null; }
export function deriveProfileSurfaceSets(registry, profileId, ledgerPrograms = []) {
  validateRegistryShape(registry, profileId);
  // One canonical render identity per merchant. An alias row keeps its own
  // review page and sitemap entry, but is never a second entry in a LIST -
  // that is how "Shed" and "SHED" both reached the calculator dropdown.
  const { aliasIds } = deriveIdentityGroups(registry, profileId, ledgerPrograms);
  const sets = Object.fromEntries(SURFACES.map((surface) => [surface, new Set()]));
  for (const program of registry.programs) {
    const profile = profileFor(program, profileId);
    if (!profile) continue;
    const isAlias = aliasIds.has(program.program_id);
    // A network program is keyed by its network id (awin-128761) in the registry
    // and by its render slug (coivas) everywhere a reader can see it. Carrying
    // BOTH keys is not belt-and-braces: matching on the network id alone silently
    // filtered all six READY6 providers back off the comparison page after they
    // had been added to it (2026-08-06 surface unification).
    for (const surface of SURFACES) {
      if (!profile.surfaces[surface]) continue;
      if (isAlias && AGGREGATE_SURFACES.includes(surface)) continue;
      sets[surface].add(program.program_id);
      if (program.review_slug) sets[surface].add(program.review_slug);
    }
  }
  return sets;
}


export function projectMoneyPagesFromRegistry(registry, profileId, pages, ledgerPrograms = [], options = {}) {
  const allowed = new Set(deriveProfileSurfaceSets(registry, profileId, ledgerPrograms).compare);
  const forced = new Set(options.includeProgramIds || []);
  const registryRenderIds = new Set((registry.programs || []).map((program) => String(program.review_slug || program.program_id)));
  for (const pid of forced) {
    if (!registryRenderIds.has(pid)) throw new Error(`coverage_forced_compare_program_missing:${profileId}:${pid}`);
    allowed.add(pid);
  }
  return pages.map((page) => {
    const rows = (page.rows || []).filter((row) => allowed.has(row.program_id));
    assertUniqueRenderIdentity(rows.map((row) => ({ ...row, provider: row.render_provider || row.provider })), `money:${page.page_id}`);
    return { ...page, rows };
  });
}


export function projectQuizFromRegistry(registry, profileId, quiz, contentLedger = null, options = {}) {
  const allowed = new Set(deriveProfileSurfaceSets(registry, profileId, contentLedger?.programs || []).quiz);
  const forced = new Set(options.includeProgramIds || []);
  const registryRenderIds = new Set((registry.programs || []).map((program) => String(program.review_slug || program.program_id)));
  for (const pid of forced) {
    if (!registryRenderIds.has(pid)) throw new Error(`coverage_forced_quiz_program_missing:${profileId}:${pid}`);
    allowed.add(pid);
  }
  const ledgerById = new Map(((contentLedger && contentLedger.programs) || []).map((entry) => [entry.program_id, entry]));
  const research = (entry, field) => {
    const item = entry && entry[`research_${field}`];
    // Never let a "provider does not publish this" finding become a quiz
    // price line or a states-served note.
    return item && item.value && item.state !== "not_publicly_stated" ? item : null;
  };
  const candidates = (quiz.candidates || []).filter((candidate) => allowed.has(candidate.program_id)).map((candidate) => {
    const rawEntry = ledgerById.get(candidate.program_id);
    const entry = rawEntry ? projectProviderDisplayLedger(rawEntry) : rawEntry;
    if (!entry) return candidate;
    const firstMonth = research(entry, "first_month_price");
    const priceValue = (firstMonth && firstMonth.value) || entry.starting_price || "";
    const priceDate = (firstMonth && firstMonth.verified_as_of) || null;
    const statesItem = research(entry, "states_served");
    return {
      ...candidate,
      priority_score: typeof entry.score === "number" ? entry.score : candidate.priority_score,
      best_for: entry.best_for || candidate.best_for || "",
      price_line: priceValue ? `${String(priceValue).split(";")[0].trim()}${priceDate ? ` · checked ${priceDate}` : ""}` : "",
      excluded_states: statesItem ? excludedStatesFromResearch(statesItem.value) : [],
      states_note: statesItem ? String(statesItem.value).split(";")[0].trim() : "",
    };
  });
  assertUniqueRenderIdentity(candidates.map((candidate) => ({
    ...candidate,
    display_name: candidate.render_display_name || candidate.display_name,
  })), "quiz");
  return { ...quiz, candidates };
}


export function deriveReviewRoutesFromRegistry(registry, profileId, ledgerPrograms = []) {
  const allowed = deriveProfileSurfaceSets(registry, profileId, ledgerPrograms).review;
  return registry.programs.filter((program) => allowed.has(program.program_id)).map((program) => program.review_path);
}

// No content ledger is supplied: display research/pricing is not under test.
const registry = JSON.parse(await readFile(process.argv[2], 'utf8'));
const profile = 'glp-clinic-scout';
const rows = registry.programs.map(p => ({program_id: p.review_slug || p.program_id, provider: p.display_name}));
const projection = {
  sets: Object.fromEntries(Object.entries(deriveProfileSurfaceSets(registry, profile)).map(([s, ids]) => [s, [...ids]])),
  compare: projectMoneyPagesFromRegistry(registry, profile, [{page_id: 'offline-comparison', rows}]),
  quiz: projectQuizFromRegistry(registry, profile, {candidates: rows}),
  reviews: deriveReviewRoutesFromRegistry(registry, profile),
};
await mkdir(process.argv[3], {recursive: false});
await writeFile(`${process.argv[3]}/surface-projection.json`, JSON.stringify(projection, null, 2));
console.log(JSON.stringify({status: 'PASS_BOUNDED_NATIVE_PROJECTION', surfaces: Object.keys(projection.sets)}));
