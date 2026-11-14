import { createHash } from "node:crypto";

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const MODES = new Set(["autofill", "manual"]);
const REQUIRED_SURFACES = Object.freeze(["compare", "quiz", "calculator", "counts", "suggestions"]);
const TAG_FIELDS = Object.freeze(["molecule_tags", "brand_name_tags", "delivery_form_tags", "treatment_type_tags"]);
const ALLOWED = Object.freeze({
  molecule_tags: new Set(["semaglutide", "tirzepatide", "retatrutide"]),
  brand_name_tags: new Set(["mounjaro", "wegovy", "zepbound"]),
  delivery_form_tags: new Set(["injectable", "oral"]),
  treatment_type_tags: new Set(["brand_name", "compounded"]),
});
export const REQUIRED_PROFILE_EVIDENCE_FIELDS = Object.freeze([
  "aliases", "calculator.checked_as_of", "calculator.evidence_gap_copy",
  "calculator.first_month_base_usd", "calculator.mode", "calculator.one_time_fee_usd",
  "calculator.price_context", "calculator.recurring_month_base_usd", "calculator.source_url",
  "program_id", "provider", "registry_program_ids", "source_urls",
  "taxonomy.brand_name_tags", "taxonomy.delivery_form_tags", "taxonomy.molecule_tags",
  "taxonomy.treatment_type_state", "taxonomy.treatment_type_tags", "verified_as_of",
].sort());
export const REQUIRED_RESEARCH_EVIDENCE_FIELDS = Object.freeze([
  "calculator.checked_as_of", "calculator.evidence_gap_copy", "calculator.first_month_base_usd",
  "calculator.mode", "calculator.one_time_fee_usd", "calculator.price_context",
  "calculator.recurring_month_base_usd", "calculator.source_url",
  "taxonomy.brand_name_tags", "taxonomy.delivery_form_tags", "taxonomy.molecule_tags",
  "taxonomy.treatment_type_tags", "verified_as_of",
].sort());
const IDENTITY_FIELDS = new Set(["aliases", "program_id", "provider", "registry_program_ids", "source_urls"]);

function fail(code, detail = "") {
  throw new Error(`provider_dynamic_profile_${code}${detail ? `:${detail}` : ""}`);
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  return value;
}

export function stableProfileSha256(value) {
  return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
}

function exactBytesSha256(value) {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

export function profileDigestBasis(profile) {
  return {
    program_id: profile.program_id,
    registry_program_ids: profile.registry_program_ids,
    provider: profile.provider,
    aliases: profile.aliases,
    version: profile.version,
    verified_as_of: profile.verified_as_of,
    source_urls: profile.source_urls,
    taxonomy: profile.taxonomy,
    calculator: profile.calculator,
    evidence_bindings: profile.evidence_bindings,
  };
}

export function normalizedProfileSha256(profile) {
  return stableProfileSha256(profileDigestBasis(profile));
}

function ageDays(date, asOf) {
  const start = Date.parse(`${date}T00:00:00Z`);
  const end = asOf instanceof Date ? asOf.getTime() : Date.parse(String(asOf));
  return Number.isFinite(start) && Number.isFinite(end) ? (end - start) / 86_400_000 : null;
}

function validHttps(url) {
  try { return new URL(String(url)).protocol === "https:"; } catch { return false; }
}

function renderId(program) {
  return String(program?.review_slug || program?.program_id || "");
}

function activePrograms(registry, profileId) {
  return (registry?.programs || []).filter((program) => {
    const profile = program.profiles?.[profileId];
    return program.commercial_state === "live" && profile?.vertical_fit === true
      && profile?.wired === true && profile?.surfaces?.review === true;
  });
}

function canonicalRegistry(registry, profileId) {
  const active = activePrograms(registry, profileId);
  const byRender = new Map(active.map((program) => [renderId(program), program]));
  const canonical = new Map();
  const members = new Map();
  for (const program of active) {
    const id = renderId(program);
    const declaration = program.profiles?.[profileId]?.canonical_identity;
    const canonicalId = declaration?.role === "alias" ? String(declaration.canonical_program_id || "") : id;
    if (!canonicalId || !byRender.has(canonicalId)) fail("alias_target_missing", id);
    if (!members.has(canonicalId)) members.set(canonicalId, []);
    members.get(canonicalId).push(program);
    if (canonicalId === id) canonical.set(canonicalId, program);
  }
  for (const id of members.keys()) if (!canonical.has(id)) fail("canonical_registry_row_missing", id);
  return { active, byRender, canonical, members };
}

function exactSortedStrings(value, field, id, allowed = null) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !item || (allowed && !allowed.has(item)))) fail("field_invalid", `${id}:${field}`);
  const sorted = [...new Set(value)].sort();
  if (sorted.length !== value.length || sorted.some((item, index) => item !== value[index])) fail("field_not_sorted_unique", `${id}:${field}`);
  return value;
}

function validateCalculator(calculator, profile, document, asOf) {
  const id = profile.program_id;
  if (!calculator || !MODES.has(calculator.mode)) fail("calculator_mode_invalid", id);
  if (!ISO_DATE.test(String(calculator.checked_as_of || ""))) fail("calculator_date_invalid", id);
  const age = ageDays(calculator.checked_as_of, asOf);
  if (age === null || age < -1 || age > document.freshness_max_days) fail("calculator_evidence_stale", id);
  if (!profile.source_urls.includes(calculator.source_url) || !validHttps(calculator.source_url)) fail("calculator_source_unbound", id);
  if (typeof calculator.price_context !== "string" || calculator.price_context.trim().length < 20) fail("calculator_context_missing", id);
  const first = calculator.first_month_base_usd;
  const recurring = calculator.recurring_month_base_usd;
  const fee = calculator.one_time_fee_usd;
  if (calculator.mode === "autofill") {
    if (!Number.isFinite(first) || first < 0 || !Number.isFinite(recurring) || recurring < 0
        || !Number.isFinite(fee) || fee < 0) fail("calculator_autofill_values_invalid", id);
    if (typeof calculator.evidence_gap_copy === "string" && calculator.evidence_gap_copy.trim()) fail("calculator_autofill_conflict", id);
  } else {
    if (first !== null || recurring !== null || fee !== null) fail("calculator_manual_value_conflict", id);
    if (typeof calculator.evidence_gap_copy !== "string" || calculator.evidence_gap_copy.trim().length < 30) fail("calculator_manual_gap_missing", id);
  }
  const evidenceBasis = {
    mode: calculator.mode, first_month_base_usd: first, recurring_month_base_usd: recurring,
    one_time_fee_usd: fee, price_context: calculator.price_context, source_url: calculator.source_url,
    checked_as_of: calculator.checked_as_of, evidence_gap_copy: calculator.evidence_gap_copy || null,
  };
  if (calculator.evidence_sha256 !== stableProfileSha256(evidenceBasis)) fail("calculator_evidence_hash_mismatch", id);
}

function validateEvidenceBindings(profile, document, asOf, onStale = null) {
  const id = profile.program_id;
  if (!Array.isArray(profile.evidence_bindings) || !profile.evidence_bindings.length) fail("evidence_missing", id);
  const bound = new Map();
  for (const [index, binding] of profile.evidence_bindings.entries()) {
    const fields = exactSortedStrings(binding?.fields, `evidence_bindings.${index}.fields`, id);
    if (!fields.length || fields.some((field) => !REQUIRED_PROFILE_EVIDENCE_FIELDS.includes(field))) fail("evidence_field_invalid", `${id}:${index}`);
    const text = binding?.evidence_text;
    if (typeof text !== "string" || text.length === 0) fail("evidence_bytes_empty", `${id}:${index}`);
    if (!profile.source_urls.includes(binding.source_url) || !validHttps(binding.source_url)
        || !ISO_DATE.test(String(binding.verified_as_of || "")) || !SHA256.test(String(binding.evidence_sha256 || ""))) {
      fail("evidence_binding_invalid", `${id}:${index}`);
    }
    const bindingAge = ageDays(binding.verified_as_of, asOf);
    if (bindingAge === null || bindingAge < -1) fail("evidence_binding_stale", `${id}:${index}`);
    if (bindingAge > document.freshness_max_days) {
      if (!onStale) fail("evidence_binding_stale", `${id}:${index}`);
      onStale(id);
    }
    if (binding.evidence_sha256 !== exactBytesSha256(text)) fail("evidence_bytes_hash_mismatch", `${id}:${index}`);
    for (const field of fields) {
      const previous = bound.get(field);
      if (previous && previous !== binding.evidence_sha256) fail("evidence_conflict", `${id}:${field}`);
      bound.set(field, binding.evidence_sha256);
    }
  }
  const missing = REQUIRED_PROFILE_EVIDENCE_FIELDS.filter((field) => !bound.has(field));
  if (missing.length) fail("evidence_coverage_missing", `${id}:${missing.join(",")}`);
}

function validatePositiveTaxonomySemantics(profile) {
  for (const field of TAG_FIELDS) {
    const bindingField = `taxonomy.${field}`;
    const evidenceTexts = profile.evidence_bindings
      .filter((binding) => binding.fields.includes(bindingField))
      .map((binding) => binding.evidence_text.toLowerCase());
    for (const value of profile.taxonomy[field]) {
      const token = value.replaceAll("_", "[_\\s-]+");
      const exactToken = new RegExp(`(^|[^a-z0-9])${token}([^a-z0-9]|$)`, "i");
      if (!evidenceTexts.some((text) => exactToken.test(text))) {
        fail("taxonomy_evidence_semantics_missing", `${profile.program_id}:${field}:${value}`);
      }
    }
  }
}

export function deriveDynamicFacetCounts(profiles) {
  const rows = profiles instanceof Map ? [...profiles.values()] : Object.values(profiles || {});
  const count = (field, tag) => rows.filter((profile) => profile.taxonomy[field].includes(tag)).length;
  return {
    provider_identities: rows.length,
    semaglutide: count("molecule_tags", "semaglutide"), tirzepatide: count("molecule_tags", "tirzepatide"),
    retatrutide: count("molecule_tags", "retatrutide"), wegovy: count("brand_name_tags", "wegovy"),
    zepbound: count("brand_name_tags", "zepbound"), mounjaro: count("brand_name_tags", "mounjaro"),
    oral: count("delivery_form_tags", "oral"), injectable: count("delivery_form_tags", "injectable"),
    compounded: count("treatment_type_tags", "compounded"), brand_name: count("treatment_type_tags", "brand_name"),
    treatment_type_unknown: rows.filter((profile) => profile.taxonomy.treatment_type_tags.length === 0).length,
    calculator_autofill: rows.filter((profile) => profile.calculator.mode === "autofill").length,
    calculator_manual: rows.filter((profile) => profile.calculator.mode === "manual").length,
  };
}

export function validateProviderDynamicProfiles(registry, profileId, { asOf = new Date(), staleEvidence = "fail" } = {}) {
  // OPERATOR DECISION 2026-10-09: in "hold" mode stale profile evidence holds
  // back that provider's therapeutic claim instead of failing the build.
  const heldStale = new Set();
  const onStale = staleEvidence === "hold" ? (id) => heldStale.add(id) : null;
  const document = registry?.provider_dynamic_profiles;
  if (!document || document.schema !== "glp-provider-dynamic-profiles-v1" || document.version !== 1) fail("schema_invalid");
  if (!ISO_DATE.test(String(document.sealed_as_of || ""))) fail("sealed_as_of_invalid");
  if (!Number.isInteger(document.freshness_max_days) || document.freshness_max_days < 1 || document.freshness_max_days > 90) fail("freshness_policy_invalid");
  if (JSON.stringify(document.required_surfaces) !== JSON.stringify(REQUIRED_SURFACES)) fail("required_surfaces_invalid");
  const registryState = canonicalRegistry(registry, profileId);
  const raw = document.profiles;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) fail("profile_map_invalid");
  const expectedIds = [...registryState.canonical.keys()].sort();
  const actualIds = Object.keys(raw).sort();
  if (JSON.stringify(actualIds) !== JSON.stringify(expectedIds)) fail("canonical_membership_mismatch", `expected=${expectedIds.join(",")}:actual=${actualIds.join(",")}`);
  const taxonomyRows = registry.therapeutic_taxonomy?.programs || {};
  const profiles = new Map();
  const labels = new Set();
  for (const id of actualIds) {
    const profile = raw[id];
    const members = registryState.members.get(id).sort((a, b) => renderId(a).localeCompare(renderId(b)));
    const memberIds = members.map((program) => String(program.program_id)).sort();
    const aliasRows = members.filter((program) => renderId(program) !== id).map((program) => ({
      program_id: String(program.program_id), render_id: renderId(program), label: String(program.display_name),
    })).sort((a, b) => a.program_id.localeCompare(b.program_id));
    if (profile?.program_id !== id || profile.version !== 1) fail("identity_invalid", id);
    if (JSON.stringify(profile.registry_program_ids) !== JSON.stringify(memberIds)) fail("registry_membership_mismatch", id);
    if (stableProfileSha256(profile.aliases) !== stableProfileSha256(aliasRows)) fail("alias_map_mismatch", id);
    if (profile.provider !== String(registryState.canonical.get(id).display_name || "")) fail("provider_identity_mismatch", id);
    const labelKey = profile.provider.toLowerCase().replace(/[^a-z0-9]+/g, "");
    if (!labelKey || labels.has(labelKey)) fail("provider_label_duplicate", id);
    labels.add(labelKey);
    if (!ISO_DATE.test(String(profile.verified_as_of || ""))) fail("verified_as_of_invalid", id);
    const age = ageDays(profile.verified_as_of, asOf);
    if (age === null || age < -1) fail("evidence_stale", id);
    if (age > document.freshness_max_days) {
      if (!onStale) fail("evidence_stale", id);
      onStale(id);
    }
    exactSortedStrings(profile.source_urls, "source_urls", id);
    if (!profile.source_urls.length || profile.source_urls.some((url) => !validHttps(url))) fail("source_urls_invalid", id);
    if (!profile.taxonomy || profile.taxonomy.treatment_type_state !== (profile.taxonomy.treatment_type_tags?.length ? "verified" : "not_verified")) fail("taxonomy_state_invalid", id);
    for (const field of TAG_FIELDS) exactSortedStrings(profile.taxonomy[field], `taxonomy.${field}`, id, ALLOWED[field]);
    const sourceTaxonomy = members.map((program) => taxonomyRows[renderId(program)]);
    if (sourceTaxonomy.some((row) => !row)) fail("taxonomy_source_missing", id);
    for (const field of TAG_FIELDS) {
      const merged = [...new Set(sourceTaxonomy.flatMap((row) => row[field] || []))].sort();
      if (JSON.stringify(profile.taxonomy[field]) !== JSON.stringify(merged)) fail("taxonomy_conflict", `${id}:${field}`);
    }
    validateEvidenceBindings(profile, document, asOf, onStale);
    validatePositiveTaxonomySemantics(profile);
    validateCalculator(profile.calculator, profile, document, asOf);
    if (profile.normalized_sha256 !== normalizedProfileSha256(profile)) fail("normalized_hash_mismatch", id);
    const canonicalProfile = registryState.canonical.get(id).profiles?.[profileId] || {};
    const surfaces = canonicalProfile.surfaces || {};
    // OPERATOR MODEL 2026-08-13: a dynamic-profile provider is on every list
    // surface UNLESS the registry row documents the restriction with a
    // surface_restriction_basis exception (trt-nation: editorial_link_dark,
    // scoped out of the quiz/calculator conversion pools while it keeps its
    // comparison row). An undocumented gap still fails the build.
    const documentedOff = (surface) => {
      const exception = canonicalProfile.exceptions?.[surface];
      return exception?.enabled === true && typeof exception.surface_restriction_basis === "string" && exception.surface_restriction_basis.trim();
    };
    const canonicalProgram=registryState.canonical.get(id);
    const reviewOnly=canonicalProfile.surface_membership==='review_only';
    if(reviewOnly && (canonicalProgram.affiliate_relationship_snapshot?.promotion_denied!==true
      || surfaces.review!==true || surfaces.sitemap!==true
      || !['compare','quiz','calculator','go'].every(s=>surfaces[s]===false && documentedOff(s)))) fail('review_only_surface_invalid',id);
    if (surfaces.compare !== true && !reviewOnly) fail("registry_surface_missing", id);
    for (const surface of ["quiz", "calculator"]) {
      if (surfaces[surface] !== true && !documentedOff(surface)) fail("registry_surface_missing", `${id}:${surface}`);
    }
    profiles.set(id, Object.freeze(profile));
  }
  const counts = deriveDynamicFacetCounts(profiles);
  if (JSON.stringify(document.expected_counts) !== JSON.stringify(counts)) fail("expected_counts_mismatch");
  return Object.freeze({ document, profiles, counts: Object.freeze(counts), canonicalIds: Object.freeze(actualIds),
    held_stale: Object.freeze([...heldStale].sort()) });
}

export function providerSuggestions(registry, profileId, options) {
  const validated = validateProviderDynamicProfiles(registry, profileId, options);
  const reviewOnlyIds=new Set((registry.programs||[]).filter(p=>p.profiles?.[profileId]?.surface_membership==='review_only').map(renderId));
  return Object.freeze([...validated.profiles.values()].filter(p=>!reviewOnlyIds.has(p.program_id)).map((profile) => Object.freeze({
    program_id: profile.program_id, provider: profile.provider,
  })).sort((a, b) => a.provider.localeCompare(b.provider) || a.program_id.localeCompare(b.program_id)));
}

export function comparablePriceProviderIds(registry, profileId, options) {
  const validated = validateProviderDynamicProfiles(registry, profileId, options);
  return new Set([...validated.profiles.values()].filter((profile) => profile.calculator.mode === "autofill").map((profile) => profile.program_id));
}

function identityBinding({ id, memberIds, provider, aliases, verifiedAsOf, sourceUrls }) {
  const evidenceText = JSON.stringify(canonical({
    program_id: id, registry_program_ids: memberIds, provider, aliases,
    verified_as_of: verifiedAsOf, source_urls: sourceUrls,
  }));
  return {
    fields: [...IDENTITY_FIELDS].sort(), source_url: sourceUrls[0], verified_as_of: verifiedAsOf,
    evidence_kind: "registry_identity_projection", evidence_text: evidenceText,
    evidence_sha256: exactBytesSha256(evidenceText),
  };
}

function normalizeIncomingBinding(binding, treatmentState) {
  const fields = [...(binding?.fields || [])];
  if (fields.includes("taxonomy.treatment_type_tags") && !fields.includes("taxonomy.treatment_type_state")) {
    fields.push("taxonomy.treatment_type_state");
  }
  return { ...binding, fields: [...new Set(fields)].sort(), treatment_type_state: treatmentState };
}

function buildProfileFromResearch({ id, program, members, taxonomy, evidence }) {
  if (!evidence || typeof evidence !== "object" || Array.isArray(evidence) || evidence.version !== 1) fail("wire_in_evidence_invalid", id);
  const expectedTaxonomy = {
    molecule_tags: [...taxonomy.molecule_tags], brand_name_tags: [...taxonomy.brand_name_tags],
    delivery_form_tags: [...taxonomy.delivery_form_tags], treatment_type_tags: [...taxonomy.treatment_type_tags],
    treatment_type_state: taxonomy.treatment_type_state,
  };
  const incomingTaxonomy = evidence.taxonomy || {};
  for (const field of TAG_FIELDS) exactSortedStrings(incomingTaxonomy[field], `research.taxonomy.${field}`, id, ALLOWED[field]);
  if (TAG_FIELDS.some((field) => JSON.stringify(incomingTaxonomy[field]) !== JSON.stringify(expectedTaxonomy[field]))) fail("wire_in_taxonomy_conflict", id);
  const verifiedAsOf = String(evidence.verified_as_of || "");
  const calculator = { ...(evidence.calculator || {}) };
  const evidenceBasis = {
    mode: calculator.mode, first_month_base_usd: calculator.first_month_base_usd,
    recurring_month_base_usd: calculator.recurring_month_base_usd, one_time_fee_usd: calculator.one_time_fee_usd,
    price_context: calculator.price_context, source_url: calculator.source_url,
    checked_as_of: calculator.checked_as_of, evidence_gap_copy: calculator.evidence_gap_copy || null,
  };
  calculator.evidence_sha256 = stableProfileSha256(evidenceBasis);
  const memberIds = members.map((item) => String(item.program_id)).sort();
  const aliases = members.filter((item) => renderId(item) !== id).map((item) => ({
    program_id: String(item.program_id), render_id: renderId(item), label: String(item.display_name),
  })).sort((a, b) => a.program_id.localeCompare(b.program_id));
  const researchBindings = (evidence.evidence_bindings || []).map((binding) => normalizeIncomingBinding(binding, expectedTaxonomy.treatment_type_state));
  const sourceUrls = [...new Set([
    ...(taxonomy.source_urls || []), calculator.source_url,
    ...researchBindings.map((binding) => binding.source_url),
  ])].sort();
  const profile = {
    program_id: id, registry_program_ids: memberIds, provider: String(program.display_name), aliases,
    version: 1, verified_as_of: verifiedAsOf, source_urls: sourceUrls,
    taxonomy: expectedTaxonomy, calculator,
    evidence_bindings: [identityBinding({ id, memberIds, provider: String(program.display_name), aliases, verifiedAsOf, sourceUrls }), ...researchBindings],
  };
  profile.normalized_sha256 = normalizedProfileSha256(profile);
  return profile;
}

export function reconcileProviderDynamicProfilesWireIn({ registry, profileId, asOf = new Date() }) {
  const document = registry?.provider_dynamic_profiles;
  if (!document || document.schema !== "glp-provider-dynamic-profiles-v1") fail("schema_invalid");
  const stagedRegistry = structuredClone(registry);
  const stagedDocument = stagedRegistry.provider_dynamic_profiles;
  const state = canonicalRegistry(stagedRegistry, profileId);
  const next = {};
  for (const [id, program] of [...state.canonical.entries()].sort(([a], [b]) => a.localeCompare(b))) {
    const members = state.members.get(id);
    const incoming = members.map((member) => member.research_dynamic_profile).filter((value) => value !== undefined && value !== null);
    if (incoming.length > 1) {
      const first = JSON.stringify(canonical(incoming[0]));
      if (incoming.slice(1).some((value) => JSON.stringify(canonical(value)) !== first)) fail("wire_in_evidence_conflict", id);
    }
    if (!incoming.length) {
      if (!stagedDocument.profiles?.[id]) fail("wire_in_evidence_missing", id);
      next[id] = stagedDocument.profiles[id];
      continue;
    }
    const taxonomyRows = members.map((member) => stagedRegistry.therapeutic_taxonomy?.programs?.[renderId(member)]);
    if (taxonomyRows.some((row) => !row)) fail("wire_in_taxonomy_missing", id);
    const merge = (field) => [...new Set(taxonomyRows.flatMap((row) => row[field] || []))].sort();
    const taxonomy = {
      molecule_tags: merge("molecule_tags"), brand_name_tags: merge("brand_name_tags"),
      delivery_form_tags: merge("delivery_form_tags"), treatment_type_tags: merge("treatment_type_tags"),
      treatment_type_state: merge("treatment_type_tags").length ? "verified" : "not_verified",
      source_urls: [...new Set(taxonomyRows.flatMap((row) => row.source_urls || []))].sort(),
    };
    // An evidence refresh can supersede embedded wire-in research. Replaying
    // that older input must not erase the newer profile. Full membership,
    // evidence hashes and freshness are still validated below before commit.
    const current = stagedDocument.profiles?.[id];
    const retainCurrent = current && ISO_DATE.test(String(current.verified_as_of || ""))
      && ISO_DATE.test(String(incoming[0].verified_as_of || ""))
      && current.verified_as_of > incoming[0].verified_as_of;
    let researchTaxonomy = taxonomy;
    if (retainCurrent) {
      // Historical input is checked against its own evidenced taxonomy, not
      // today's tags or source URLs. It cannot become the selected profile.
      const suppliedTaxonomy = incoming[0].taxonomy || {};
      for (const field of TAG_FIELDS) exactSortedStrings(suppliedTaxonomy[field], `research.taxonomy.${field}`, id, ALLOWED[field]);
      researchTaxonomy = {
        ...suppliedTaxonomy,
        treatment_type_state: suppliedTaxonomy.treatment_type_tags.length ? "verified" : "not_verified",
        source_urls: [],
      };
    }
    const incomingProfile = buildProfileFromResearch({ id, program, members, taxonomy: researchTaxonomy, evidence: incoming[0] });
    if (retainCurrent) {
      // This is validation of superseded evidence, not permission to publish it.
      // The retained current profile still passes the caller's clock below.
      const historicalAsOf = new Date(`${incomingProfile.verified_as_of}T12:00:00Z`);
      exactSortedStrings(incomingProfile.source_urls, "source_urls", id);
      if (!incomingProfile.source_urls.length || incomingProfile.source_urls.some((url) => !validHttps(url))) fail("source_urls_invalid", id);
      validateEvidenceBindings(incomingProfile, stagedDocument, historicalAsOf);
      validatePositiveTaxonomySemantics(incomingProfile);
      validateCalculator(incomingProfile.calculator, incomingProfile, stagedDocument, historicalAsOf);
    }
    next[id] = retainCurrent ? current : incomingProfile;
  }
  stagedDocument.profiles = next;
  stagedDocument.expected_counts = deriveDynamicFacetCounts(next);
  stagedDocument.sealed_as_of = Object.values(next).map((profile) => profile.verified_as_of).sort().at(-1) || stagedDocument.sealed_as_of;
  const validated = validateProviderDynamicProfiles(stagedRegistry, profileId, { asOf });
  registry.provider_dynamic_profiles = stagedDocument;
  return validateProviderDynamicProfiles(registry, profileId, { asOf });
}
