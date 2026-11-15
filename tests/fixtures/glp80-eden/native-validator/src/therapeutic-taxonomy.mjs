import { createHash } from "node:crypto";
import { lstatSync, readFileSync, realpathSync } from "node:fs";
import path from "node:path";
import { validateProviderDynamicProfiles } from "./provider-dynamic-profiles.mjs";

export const THERAPEUTIC_FILTERS = Object.freeze([
  { value: "semaglutide", label: "Semaglutide", field: "molecule_tags" },
  { value: "tirzepatide", label: "Tirzepatide", field: "molecule_tags" },
  { value: "retatrutide", label: "Retatrutide", field: "molecule_tags" },
  { value: "wegovy", label: "Wegovy", field: "brand_name_tags" },
  { value: "zepbound", label: "Zepbound", field: "brand_name_tags" },
  { value: "mounjaro", label: "Mounjaro", field: "brand_name_tags" },
  { value: "oral", label: "Oral", field: "delivery_form_tags" },
  { value: "injectable", label: "Injectable", field: "delivery_form_tags" },
]);

export const TREATMENT_FILTERS = Object.freeze([
  { value: "compounded", label: "Compounded versions" },
  { value: "brand_name", label: "Brand-name medications" },
  { value: "either", label: "Either is fine" },
]);

export const MEDICATION_PREFERENCES = Object.freeze([
  "semaglutide", "tirzepatide", "retatrutide", "wegovy", "zepbound", "mounjaro", "oral", "injectable", "not_sure",
]);

const TAG_FIELDS = Object.freeze([
  "molecule_tags", "brand_name_tags", "delivery_form_tags", "treatment_type_tags",
]);
const ALLOWED = Object.freeze({
  molecule_tags: new Set(["semaglutide", "tirzepatide", "retatrutide"]),
  brand_name_tags: new Set(["wegovy", "zepbound", "mounjaro"]),
  delivery_form_tags: new Set(["oral", "injectable"]),
  treatment_type_tags: new Set(["compounded", "brand_name"]),
});
const LINK_STATES = new Set(["live", "editorial_link_dark"]);
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const SHA256 = /^[a-f0-9]{64}$/;
const CAPTURE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/;
const BUSINESS_ROOT = "/home/ubuntu/business";
const RETATRUTIDE_CAPTURE = Object.freeze({
  schema: "glp-retatrutide-local-capture-authority-v1",
  research: Object.freeze({
    path: "reports/ops/orchestration-restore-20260907/PLAN/D2/D2f/fresh-authority/research.md",
    sha256: "aca5d53141a5e516c94ef621d95d5ffeaa309f4b0a4551327d92c1a16ce2153c",
  }),
  audit_summary: Object.freeze({
    path: "reports/ops/orchestration-restore-20260907/PLAN/D2/D2f/fresh-authority/audit-summary.json",
    sha256: "77a54ee02361130524bb07b6940c3c75e96af634eaf7bbe2acb3e90bbf51f84a",
    status: "RESEARCH_COMPLETE_REQUIRES_EXACT_RECONCILIATION",
    audited_at: "2026-09-09T02:48:58.211798",
    firecrawl_job_id: "01a0840e-3dfc-76f6-a5e9-93c324ee574f",
  }),
  authorities: Object.freeze({
    fda: Object.freeze({
      host: "www.fda.gov",
      source_url: "https://www.fda.gov/drugs/drug-alerts-and-statements/fdas-concerns-unapproved-glp-1-drugs-used-weight-loss",
      authority_class: "official_federal_regulator",
      evidence_text: "Retatrutide cannot be used in compounding under federal law; it is not a component of an FDA-approved drug and has not been found safe and effective for any condition.",
      evidence_sha256: "2db4cda6e4047b3d1f288125c243e98a3345f25ab6fc427c3cfc4d607b3c4d3b",
      consumer_available: false,
    }),
    lilly: Object.freeze({
      host: "www.lilly.com",
      source_url: "https://www.lilly.com/news/stories/what-to-know-about-retatrutide",
      authority_class: "official_developer_first_party",
      evidence_text: "Retatrutide is investigational, not approved, and legally available only through Lilly-sponsored clinical trials.",
      evidence_sha256: "ee5df51e5fe1e65effc3b196c106eb6f2226104ecb4d3fff41e4b7033a273417",
      consumer_available: false,
    }),
  }),
});
const REQUIRED_COUNT_KEYS = Object.freeze([
  "provider_rows", "semaglutide", "tirzepatide", "retatrutide", "wegovy", "zepbound",
  "mounjaro", "oral", "injectable", "compounded", "brand_name", "treatment_type_unknown",
]);

function fail(code, detail = "") {
  throw new Error(`therapeutic_taxonomy_${code}${detail ? `:${detail}` : ""}`);
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  }
  return value;
}

export function stableSha256(value) {
  return createHash("sha256").update(JSON.stringify(canonical(value))).digest("hex");
}

export function taxonomyDigestBasis(row) {
  return {
    program_id: row.program_id,
    registry_program_id: row.registry_program_id,
    provider: row.provider,
    render_label: row.render_label || null,
    classification_state: row.classification_state,
    verified_as_of: row.verified_as_of,
    source_urls: row.source_urls,
    molecule_tags: row.molecule_tags,
    brand_name_tags: row.brand_name_tags,
    delivery_form_tags: row.delivery_form_tags,
    treatment_type_tags: row.treatment_type_tags,
    treatment_type_state: row.treatment_type_state,
    commercial_link_state: row.commercial_link_state,
    evidence_bindings: row.evidence_bindings,
    ...(Object.hasOwn(row, "claim_qualification") ? { claim_qualification: row.claim_qualification } : {}),
  };
}

export function normalizedTaxonomySha256(row) {
  return stableSha256(taxonomyDigestBasis(row));
}

function activeRegistryPrograms(registry, profileId) {
  const byRenderId = new Map();
  for (const program of registry?.programs || []) {
    const profile = program.profiles?.[profileId];
    if (program.commercial_state !== "live" || profile?.vertical_fit !== true
        || profile?.wired !== true || profile?.surfaces?.review !== true) continue;
    const pid = renderId(program);
    if (!pid || byRenderId.has(pid)) fail("wire_in_render_identity_duplicate", pid);
    byRenderId.set(pid, program);
  }
  return byRenderId;
}

function findProgramRow(pages, requiredPaths, pid) {
  for (const page of pages || []) {
    if (!requiredPaths.has(page.path)) continue;
    const row = (page.rows || []).find((item) => String(item.program_id) === pid);
    if (row) return row;
  }
  return null;
}

function wireInRow(program, pid, evidence, authorities, pages, quizConfig, requiredPaths) {
  if (!evidence || typeof evidence !== "object" || Array.isArray(evidence)) fail("wire_in_evidence_missing", pid);
  const authorityPath = String(evidence.authority_path || "");
  const authoritySha256 = String(evidence.authority_sha256 || "");
  if (!authorityPath.startsWith("reports/") || !SHA256.test(authoritySha256)) fail("wire_in_authority_invalid", pid);
  const sourceUrl = String(evidence.source_url || "");
  try {
    if (new URL(sourceUrl).protocol !== "https:") fail("wire_in_source_invalid", pid);
  } catch {
    fail("wire_in_source_invalid", pid);
  }
  const verifiedAsOf = String(evidence.verified_as_of || "");
  if (!ISO_DATE.test(verifiedAsOf)) fail("wire_in_verified_as_of_invalid", pid);
  const authority = `wire_in_${pid.replace(/[^a-z0-9]+/g, "_")}`;
  authorities[authority] = { path: authorityPath, sha256: authoritySha256 };
  const binding = {
    authority,
    authority_sha256: authoritySha256,
    source_url: sourceUrl,
    verified_as_of: verifiedAsOf,
    ...(evidence.evidence_sha256 ? { evidence_sha256: String(evidence.evidence_sha256) } : {}),
    ...(evidence.capture_id ? { capture_id: String(evidence.capture_id) } : {}),
  };
  if (!binding.evidence_sha256 && !binding.capture_id) fail("wire_in_evidence_identifier_missing", pid);
  const compareRow = findProgramRow(pages, requiredPaths, pid);
  if (!compareRow) fail("wire_in_compare_row_missing", pid);
  const quizCandidate = (quizConfig?.candidates || []).find((item) => String(item.program_id) === pid);
  if (!quizCandidate) fail("wire_in_quiz_candidate_missing", pid);
  const requestedLinkState = evidence.commercial_link_state || {};
  const derivedCompareState = commercialLinkState(compareRow, "compare");
  if (requestedLinkState.compare && requestedLinkState.compare !== derivedCompareState) {
    fail("wire_in_compare_link_state_mismatch", pid);
  }
  const row = {
    program_id: pid,
    registry_program_id: String(program.program_id),
    provider: String(program.display_name || evidence.provider || ""),
    render_label: evidence.render_label ? String(evidence.render_label) : null,
    classification_state: String(evidence.classification_state || "WIRE_IN_EXPLICIT"),
    verified_as_of: verifiedAsOf,
    source_urls: [sourceUrl],
    molecule_tags: sortedUniqueStrings(evidence.molecule_tags, ALLOWED.molecule_tags, "molecule_tags", pid),
    brand_name_tags: sortedUniqueStrings(evidence.brand_name_tags, ALLOWED.brand_name_tags, "brand_name_tags", pid),
    delivery_form_tags: sortedUniqueStrings(evidence.delivery_form_tags, ALLOWED.delivery_form_tags, "delivery_form_tags", pid),
    treatment_type_tags: sortedUniqueStrings(evidence.treatment_type_tags, ALLOWED.treatment_type_tags, "treatment_type_tags", pid),
    treatment_type_state: evidence.treatment_type_tags.length ? "verified" : "not_verified",
    commercial_link_state: {
      compare: derivedCompareState,
      quiz: requestedLinkState.quiz === "live" ? "live" : "editorial_link_dark",
    },
    evidence_bindings: [binding],
  };
  if (!row.provider) fail("wire_in_provider_missing", pid);
  row.normalized_sha256 = normalizedTaxonomySha256(row);
  return row;
}

export function reconcileTherapeuticTaxonomyWireIn({ registry, profileId, pages, quizConfig, asOf = new Date() }) {
  const document = registry?.therapeutic_taxonomy;
  if (!document || document.schema !== "glp-therapeutic-taxonomy-v1") fail("schema_invalid");
  const requiredPaths = new Set(document.required_surface_paths || []);
  const active = activeRegistryPrograms(registry, profileId);
  const existing = document.programs || {};
  const authorities = { ...(document.authorities || {}) };
  const programs = {};
  for (const [pid, program] of [...active.entries()].sort(([left], [right]) => left.localeCompare(right))) {
    const evidence = program.research_therapeutic_taxonomy;
    if (evidence && existing[pid]
        && ISO_DATE.test(String(existing[pid].verified_as_of || ""))
        && ISO_DATE.test(String(evidence.verified_as_of || ""))
        && existing[pid].verified_as_of > evidence.verified_as_of) {
      // Preserve a newer evidence-refresh row; validate the complete result below.
      programs[pid] = existing[pid];
    } else if (evidence) {
      programs[pid] = wireInRow(program, pid, evidence, authorities, pages, quizConfig, requiredPaths);
    } else if (existing[pid]) {
      programs[pid] = existing[pid];
    } else {
      fail("wire_in_evidence_missing", pid);
    }
  }
  document.authorities = authorities;
  document.programs = programs;
  document.expected_counts = deriveFacetCounts(programs);
  document.sealed_as_of = Object.values(programs).map((row) => row.verified_as_of).sort().at(-1) || document.sealed_as_of;
  validateTherapeuticTaxonomy(registry, profileId, { asOf });
  return Object.freeze({
    registry,
    activeProgramIds: Object.freeze([...active.keys()].sort()),
    counts: Object.freeze({ ...document.expected_counts }),
  });
}

function identityKey(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

function renderId(program) {
  return String(program?.review_slug || program?.program_id || "");
}

function sortedUniqueStrings(value, allowed, field, pid) {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string" || !allowed.has(item))) {
    fail("tag_value_invalid", `${pid}:${field}`);
  }
  const normalized = [...new Set(value)].sort();
  if (normalized.length !== value.length || normalized.some((item, index) => item !== value[index])) {
    fail("tags_not_sorted_unique", `${pid}:${field}`);
  }
  return normalized;
}

/** True when a raw taxonomy row's evidence is past its freshness window (claim must be held). */
export function taxonomyRowStale(row, freshnessMaxDays, asOf = new Date()) {
  const age = ageDays(String(row?.verified_as_of || ""), asOf);
  return age === null || age > freshnessMaxDays;
}

function ageDays(verifiedAsOf, asOf) {
  const start = Date.parse(`${verifiedAsOf}T00:00:00Z`);
  const end = asOf instanceof Date ? asOf.getTime() : Date.parse(String(asOf));
  if (!Number.isFinite(start) || !Number.isFinite(end)) return null;
  return (end - start) / 86_400_000;
}

function exactEvidenceSha256(text) {
  return createHash("sha256").update(text, "utf8").digest("hex");
}

function readExactLocalCapture(declaration, expected, label, businessRoot = BUSINESS_ROOT) {
  if (!declaration || declaration.path !== expected.path || declaration.sha256 !== expected.sha256) {
    fail("consumer_authority_capture_declaration_invalid", label);
  }
  const absolute = path.resolve(businessRoot, declaration.path);
  const relative = path.relative(businessRoot, absolute);
  if (!relative || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    fail("consumer_authority_capture_outside_workspace", label);
  }
  let status;
  try {
    status = lstatSync(absolute);
    if (!status.isFile() || status.isSymbolicLink() || realpathSync(absolute) !== absolute) {
      fail("consumer_authority_capture_not_plain_regular", label);
    }
  } catch (error) {
    if (String(error?.message || "").startsWith("therapeutic_taxonomy_")) throw error;
    fail("consumer_authority_capture_unreadable", label);
  }
  const bytes = readFileSync(absolute);
  if (createHash("sha256").update(bytes).digest("hex") !== expected.sha256) {
    fail("consumer_authority_capture_hash_mismatch", label);
  }
  return bytes;
}

export function testOnlyReadExactLocalCapture(businessRoot, declaration, expected) {
  return readExactLocalCapture(declaration, expected, "hostile_probe", path.resolve(businessRoot));
}

function captureAgeDays(timestamp, asOf) {
  const start = Date.parse(`${timestamp}Z`);
  const end = asOf instanceof Date ? asOf.getTime() : Date.parse(String(asOf));
  return Number.isFinite(start) && Number.isFinite(end) ? (end - start) / 86_400_000 : null;
}

function validateRetatrutideCapture(declaration, freshnessMaxDays, asOf, staleEvidence = "fail") {
  if (!declaration || declaration.schema !== RETATRUTIDE_CAPTURE.schema
      || declaration.research?.path !== RETATRUTIDE_CAPTURE.research.path
      || declaration.research?.sha256 !== RETATRUTIDE_CAPTURE.research.sha256
      || declaration.audit_summary?.path !== RETATRUTIDE_CAPTURE.audit_summary.path
      || declaration.audit_summary?.sha256 !== RETATRUTIDE_CAPTURE.audit_summary.sha256
      || declaration.audit_summary?.status !== RETATRUTIDE_CAPTURE.audit_summary.status
      || declaration.audit_summary?.audited_at !== RETATRUTIDE_CAPTURE.audit_summary.audited_at
      || declaration.audit_summary?.firecrawl_job_id !== RETATRUTIDE_CAPTURE.audit_summary.firecrawl_job_id) {
    fail("consumer_authority_capture_metadata_invalid", "retatrutide");
  }
  const declaredAuthorities = Object.fromEntries((declaration.authorities || []).map((row) => [row.authority, row]));
  if (Object.keys(declaredAuthorities).sort().join(",") !== "fda,lilly") {
    fail("consumer_authority_capture_authorities_invalid", "retatrutide");
  }
  for (const [authority, expected] of Object.entries(RETATRUTIDE_CAPTURE.authorities)) {
    const declared = declaredAuthorities[authority];
    if (!declared || declared.host !== expected.host || declared.source_url !== expected.source_url
        || declared.authority_class !== expected.authority_class) {
      fail("consumer_authority_capture_authority_invalid", authority);
    }
  }
  const researchBytes = readExactLocalCapture(declaration.research, RETATRUTIDE_CAPTURE.research, "research");
  const summaryBytes = readExactLocalCapture(declaration.audit_summary, RETATRUTIDE_CAPTURE.audit_summary, "audit_summary");
  const research = researchBytes.toString("utf8");
  let summary;
  try { summary = JSON.parse(summaryBytes.toString("utf8")); } catch { fail("consumer_authority_capture_json_invalid", "audit_summary"); }
  const capture = RETATRUTIDE_CAPTURE.audit_summary;
  const captureAge = captureAgeDays(summary.audited_at, asOf);
  if (summary.status !== capture.status || summary.audited_at !== capture.audited_at
      || summary.firecrawl_job_id !== capture.firecrawl_job_id || captureAge === null || captureAge < -1) {
    fail("consumer_authority_capture_status_time_invalid", "audit_summary");
  }
  // OPERATOR DECISION 2026-10-09 (workflow v3): stale evidence holds back only
  // the claim it supports. Identity, hash and semantic checks above still fail.
  const stale = captureAge > freshnessMaxDays;
  if (stale && staleEvidence !== "hold") fail("consumer_authority_capture_status_time_invalid", "audit_summary");
  if (!research.includes("Date: 2026-09-09") || !research.includes(`Firecrawl job: \`${capture.firecrawl_job_id}\``)) {
    fail("consumer_authority_capture_research_metadata_invalid", "research");
  }
  for (const [authority, expected] of Object.entries(RETATRUTIDE_CAPTURE.authorities)) {
    if (!research.includes(expected.source_url) || summary.retatrutide?.[`${authority}_source`] !== expected.source_url
        || summary.retatrutide?.[`${authority}_finding`] !== expected.evidence_text) {
      fail("consumer_authority_capture_semantics_invalid", authority);
    }
  }
  return Object.freeze({ summary, stale });
}

function providerRetatrutideEvidencePositive(profile) {
  if (!profile.taxonomy.molecule_tags.includes("retatrutide")) return false;
  const identityTokens = [profile.program_id, profile.provider]
    .map((value) => String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, ""))
    .filter(Boolean);
  const supported = profile.evidence_bindings.some((binding) => {
    if (!binding.fields.includes("taxonomy.molecule_tags")) return false;
    const raw = binding.evidence_text.toLowerCase();
    const compact = raw.replace(/[^a-z0-9]+/g, "");
    if (!raw.includes("retatrutide") || !identityTokens.some((token) => compact.includes(token))) return false;
    if (/\b(?:not|cannot|unavailable|unapproved|investigational|illegal|unlawful)\b/.test(raw)
        || /\b(?:does not|do not|trial(?:s)? only|only (?:in|through) .*trial)\b/.test(raw)) return false;
    return /\b(?:offers?|provides?|prescribes?|available)\b/.test(raw);
  });
  if (!supported) fail("consumer_authority_provider_evidence_unsupported", profile.program_id);
  return true;
}

export function validateConsumerTherapeuticAuthorities(registry, profileId, { asOf = new Date(), dynamic = null, staleEvidence = "fail" } = {}) {
  const document = registry?.consumer_therapeutic_authorities;
  if (!document || document.schema !== "glp-consumer-therapeutic-authority-v1" || document.version !== 1) fail("consumer_authority_schema_invalid");
  if (!ISO_DATE.test(String(document.sealed_as_of || "")) || !Number.isInteger(document.freshness_max_days)
      || document.freshness_max_days < 1 || document.freshness_max_days > 30) fail("consumer_authority_policy_invalid");
  const retatrutide = document.molecules?.retatrutide;
  if (!retatrutide || retatrutide.gate !== "positive_provider_evidence_and_all_current_lawful_regulatory_authorities") fail("consumer_authority_gate_invalid", "retatrutide");
  const capture = validateRetatrutideCapture(retatrutide.local_capture_authority, document.freshness_max_days, asOf, staleEvidence);
  let authorityStale = capture.stale;
  const records = retatrutide.official_evidence;
  if (!Array.isArray(records) || records.length !== 2
      || JSON.stringify(records.map((row) => row.authority).sort()) !== JSON.stringify(["fda", "lilly"])) fail("consumer_authority_dual_evidence_invalid", "retatrutide");
  for (const record of records) {
    const expected = RETATRUTIDE_CAPTURE.authorities[record.authority];
    if (!expected || record.source_url !== expected.source_url || record.source_host !== expected.host
        || record.authority_class !== expected.authority_class || record.evidence_text !== expected.evidence_text
        || record.evidence_sha256 !== expected.evidence_sha256
        || record.capture_path !== RETATRUTIDE_CAPTURE.audit_summary.path
        || record.capture_sha256 !== RETATRUTIDE_CAPTURE.audit_summary.sha256
        || record.capture_status !== RETATRUTIDE_CAPTURE.audit_summary.status
        || record.captured_at !== RETATRUTIDE_CAPTURE.audit_summary.audited_at) {
      fail("consumer_authority_exact_evidence_mismatch", record.authority);
    }
    if (!ISO_DATE.test(String(record.verified_as_of || "")) || !SHA256.test(String(record.evidence_sha256 || ""))
        || typeof record.evidence_text !== "string" || !record.evidence_text.length) fail("consumer_authority_evidence_invalid", record.authority);
    try {
      const source = new URL(record.source_url);
      if (source.protocol !== "https:" || source.hostname !== expected.host || source.href !== expected.source_url) {
        fail("consumer_authority_source_invalid", record.authority);
      }
    }
    catch { fail("consumer_authority_source_invalid", record.authority); }
    if (record.evidence_sha256 !== exactEvidenceSha256(record.evidence_text)) fail("consumer_authority_hash_mismatch", record.authority);
    const age = ageDays(record.verified_as_of, asOf);
    if (age === null || age < -1) fail("consumer_authority_stale", `${record.authority}:${age}`);
    if (age > document.freshness_max_days) {
      if (staleEvidence !== "hold") fail("consumer_authority_stale", `${record.authority}:${age}`);
      authorityStale = true;
    }
    if (record.consumer_availability !== expected.consumer_available) fail("consumer_authority_disposition_semantics_mismatch", record.authority);
  }
  const profiles = dynamic || validateProviderDynamicProfiles(registry, profileId, { asOf, staleEvidence });
  const providerEvidencePositive = [...profiles.profiles.values()].some(providerRetatrutideEvidencePositive);
  // A stale authority can never make the consumer claim positive: hold = unavailable.
  const lawfulAuthorityPositive = !authorityStale
    && records.every((record) => RETATRUTIDE_CAPTURE.authorities[record.authority].consumer_available === true);
  return Object.freeze({ retatrutide: Object.freeze({
    provider_evidence_positive: providerEvidencePositive,
    lawful_regulatory_authority_positive: lawfulAuthorityPositive,
    claim_held_stale: authorityStale,
    consumer_available: providerEvidencePositive && lawfulAuthorityPositive,
    source_urls: Object.freeze(records.map((record) => record.source_url)),
  }) });
}

export function consumerTherapeuticFilters(registry, profileId, options = {}) {
  const authorities = validateConsumerTherapeuticAuthorities(registry, profileId, options);
  return Object.freeze(THERAPEUTIC_FILTERS.filter((item) => item.value !== "retatrutide" || authorities.retatrutide.consumer_available));
}

export function deriveFacetCounts(programs) {
  const rows = programs instanceof Map ? [...programs.values()] : Object.values(programs || {});
  const count = (field, tag) => rows.filter((row) => (row[field] || []).includes(tag)).length;
  return {
    provider_rows: rows.length,
    semaglutide: count("molecule_tags", "semaglutide"),
    tirzepatide: count("molecule_tags", "tirzepatide"),
    retatrutide: count("molecule_tags", "retatrutide"),
    wegovy: count("brand_name_tags", "wegovy"),
    zepbound: count("brand_name_tags", "zepbound"),
    mounjaro: count("brand_name_tags", "mounjaro"),
    oral: count("delivery_form_tags", "oral"),
    injectable: count("delivery_form_tags", "injectable"),
    compounded: count("treatment_type_tags", "compounded"),
    brand_name: count("treatment_type_tags", "brand_name"),
    treatment_type_unknown: rows.filter((row) => (row.treatment_type_tags || []).length === 0).length,
  };
}

export function commercialLinkState(entity, surface = "compare") {
  const authorized = entity?.live_link_allowed === true && entity?.promotion_authorized === true;
  if (!authorized) return "editorial_link_dark";
  if (surface === "compare") {
    try {
      if (new URL(String(entity?.affiliate_href || "")).protocol !== "https:") return "editorial_link_dark";
    } catch {
      return "editorial_link_dark";
    }
  }
  return "live";
}

// A repeated visible name is safe only when both registry rows are explicitly
// the same canonical provider. Distinct offers must retain the uniqueness gate.
export function sameCanonicalRegistryIdentity(registryByRenderId, profileId, left, right) {
  const resolve = (id) => {
    const program = registryByRenderId.get(id);
    if (!program) return null;
    const identity = program.profiles?.[profileId]?.canonical_identity;
    if (identity?.role === "alias") {
      const target = String(identity.canonical_program_id || "");
      const canonicalProgram = registryByRenderId.get(target);
      if (!canonicalProgram || canonicalProgram.profiles?.[profileId]?.canonical_identity?.role === "alias") return null;
      return target;
    }
    return id;
  };
  const leftCanonical = resolve(left);
  return leftCanonical !== null && leftCanonical === resolve(right) && left !== right
    && [left, right].some((id) => registryByRenderId.get(id)?.profiles?.[profileId]?.canonical_identity?.role === "alias");
}

export function validateTherapeuticTaxonomy(registry, profileId, { asOf = new Date(), staleEvidence = "fail" } = {}) {
  const heldStale = new Set();
  const document = registry?.therapeutic_taxonomy;
  if (!document || document.schema !== "glp-therapeutic-taxonomy-v1") fail("schema_invalid");
  if (!ISO_DATE.test(String(document.sealed_as_of || ""))) fail("sealed_as_of_invalid");
  if (!Number.isInteger(document.freshness_max_days) || document.freshness_max_days < 1 || document.freshness_max_days > 90) {
    fail("freshness_policy_invalid");
  }
  if (!Array.isArray(document.required_surface_paths) || document.required_surface_paths.length === 0
      || new Set(document.required_surface_paths).size !== document.required_surface_paths.length
      || document.required_surface_paths.some((route) => !/^\/(?:best|cost)\/[a-z0-9-]+\/$/.test(route))) {
    fail("required_surface_paths_invalid");
  }
  const authorities = document.authorities;
  if (!authorities || typeof authorities !== "object" || Array.isArray(authorities) || Object.keys(authorities).length < 3) {
    fail("authorities_missing");
  }
  for (const [name, authority] of Object.entries(authorities)) {
    if (!SHA256.test(String(authority?.sha256 || "")) || !String(authority?.path || "").startsWith("reports/")) {
      fail("authority_invalid", name);
    }
  }
  const rawPrograms = document.programs;
  if (!rawPrograms || typeof rawPrograms !== "object" || Array.isArray(rawPrograms)) fail("program_map_invalid");
  const registryByRenderId = new Map((registry?.programs || []).map((program) => [renderId(program), program]));
  const programs = new Map();
  const renderIdentities = new Map();
  for (const pid of Object.keys(rawPrograms).sort()) {
    const row = rawPrograms[pid];
    if (!/^[a-z0-9][a-z0-9-]*$/.test(pid) || row?.program_id !== pid) fail("program_id_mismatch", pid);
    const registryProgram = registryByRenderId.get(pid);
    if (!registryProgram || registryProgram.program_id !== row.registry_program_id
        || registryProgram.profiles?.[profileId]?.vertical_fit !== true) {
      fail("registry_binding_mismatch", pid);
    }
    if (typeof row.provider !== "string" || !row.provider.trim()) fail("provider_missing", pid);
    if (Object.hasOwn(row, "claim_qualification") && (typeof row.claim_qualification !== "string"
        || row.claim_qualification !== row.claim_qualification.trim() || !row.claim_qualification
        || row.claim_qualification.length > 2000 || /[<>\u0000-\u001f\u007f]/.test(row.claim_qualification))) {
      fail("claim_qualification_invalid", pid);
    }
    for (const field of TAG_FIELDS) sortedUniqueStrings(row[field], ALLOWED[field], field, pid);
    if (row.treatment_type_state !== ((row.treatment_type_tags || []).length ? "verified" : "not_verified")) {
      fail("treatment_type_state_mismatch", pid);
    }
    if (!row.commercial_link_state || !LINK_STATES.has(row.commercial_link_state.compare) || !LINK_STATES.has(row.commercial_link_state.quiz)) {
      fail("commercial_link_state_invalid", pid);
    }
    if (!ISO_DATE.test(String(row.verified_as_of || ""))) fail("verified_as_of_invalid", pid);
    const age = ageDays(row.verified_as_of, asOf);
    if (age === null || age < -1) fail("evidence_stale", `${pid}:${age}`);
    if (age > document.freshness_max_days) {
      // OPERATOR DECISION 2026-10-09: hold back only this row's therapeutic claim.
      if (staleEvidence !== "hold") fail("evidence_stale", `${pid}:${age}`);
      heldStale.add(pid);
    }
    if (!Array.isArray(row.source_urls) || row.source_urls.length === 0
        || row.source_urls.some((url) => {
          try { return new URL(url).protocol !== "https:"; } catch { return true; }
        })
        || [...new Set(row.source_urls)].sort().some((url, index) => url !== row.source_urls[index])) {
      fail("source_urls_invalid", pid);
    }
    if (!Array.isArray(row.evidence_bindings) || row.evidence_bindings.length === 0) fail("evidence_missing", pid);
    for (const binding of row.evidence_bindings) {
      const authority = authorities[binding?.authority];
      if (!authority || binding.authority_sha256 !== authority.sha256) fail("evidence_authority_mismatch", pid);
      if (!row.source_urls.includes(binding.source_url) || !ISO_DATE.test(String(binding.verified_as_of || ""))) {
        fail("evidence_source_mismatch", pid);
      }
      const hashValid = SHA256.test(String(binding.evidence_sha256 || ""));
      const captureValid = CAPTURE_ID.test(String(binding.capture_id || ""));
      if (!hashValid && !captureValid) fail("evidence_identifier_missing", pid);
    }
    if (!SHA256.test(String(row.normalized_sha256 || "")) || row.normalized_sha256 !== normalizedTaxonomySha256(row)) {
      fail("normalized_hash_mismatch", pid);
    }
    const rendered = row.render_label || row.provider;
    const key = identityKey(rendered);
    const previous = renderIdentities.get(key);
    if (!key || (previous && !sameCanonicalRegistryIdentity(registryByRenderId, profileId, pid, previous))) {
      fail("render_identity_duplicate", `${pid}:${previous || ""}`);
    }
    renderIdentities.set(key, pid);
    programs.set(pid, Object.freeze({ ...row }));
  }
  const counts = deriveFacetCounts(programs);
  if (!document.expected_counts || REQUIRED_COUNT_KEYS.some((key) => document.expected_counts[key] !== counts[key])) {
    fail("expected_counts_mismatch", stableSha256({ expected: document.expected_counts, actual: counts }));
  }
  if (document.expected_counts.provider_rows !== programs.size) fail("provider_count_mismatch");
  return Object.freeze({ document, programs, counts: Object.freeze(counts),
    held_stale: Object.freeze([...heldStale].sort()) });
}

function canonicalTherapeuticPrograms(registry, profileId, validatedPrograms) {
  const registryByRenderId = new Map((registry?.programs || []).map((program) => [renderId(program), program]));
  const grouped = new Map();
  for (const [sourceId, taxonomy] of validatedPrograms.entries()) {
    const registryProgram = registryByRenderId.get(sourceId);
    if (!registryProgram) fail("canonical_projection_registry_missing", sourceId);
    const identity = registryProgram.profiles?.[profileId]?.canonical_identity;
    const canonicalId = identity?.role === "alias" ? String(identity.canonical_program_id || "") : sourceId;
    if (!canonicalId || !registryByRenderId.has(canonicalId)) fail("canonical_projection_target_missing", sourceId);
    const rows = grouped.get(canonicalId) || [];
    rows.push(taxonomy);
    grouped.set(canonicalId, rows);
  }
  return new Map([...grouped.entries()].sort(([left], [right]) => left.localeCompare(right)).map(([canonicalId, rows]) => {
    const canonicalTaxonomy = validatedPrograms.get(canonicalId);
    if (!canonicalTaxonomy) fail("canonical_projection_taxonomy_missing", canonicalId);
    const merge = (field) => [...new Set(rows.flatMap((row) => row[field] || []))].sort();
    const qualifications = [...new Set(rows.map(row => row.claim_qualification).filter(Boolean))];
    if (qualifications.length > 1) fail("canonical_qualification_conflict", canonicalId);
    const projected = {
      ...canonicalTaxonomy,
      ...(qualifications.length ? {claim_qualification: qualifications[0], source_urls: merge("source_urls")} : {}),
      molecule_tags: merge("molecule_tags"),
      brand_name_tags: merge("brand_name_tags"),
      delivery_form_tags: merge("delivery_form_tags"),
      treatment_type_tags: merge("treatment_type_tags"),
    };
    projected.treatment_type_state = projected.treatment_type_tags.length ? "verified" : "not_verified";
    projected.normalized_sha256 = rows.length === 1
      ? canonicalTaxonomy.normalized_sha256
      : stableSha256({
        canonical_program_id: canonicalId,
        source_taxonomy_sha256s: rows.map((row) => row.normalized_sha256).sort(),
      });
    return [canonicalId, Object.freeze(projected)];
  }));
}

function exactIds(entries, label) {
  const ids = entries.map((entry) => String(entry?.program_id || ""));
  if (ids.some((id) => !id) || new Set(ids).size !== ids.length) fail("surface_ids_invalid", label);
  return new Set(ids);
}

function surfaceScopedIds(actual, scopedOff) {
  return new Set([...actual].filter((id) => !scopedOff.has(id)));
}

function assertSameSet(actual, expected, label) {
  const missing = [...expected].filter((id) => !actual.has(id));
  const extra = [...actual].filter((id) => !expected.has(id));
  if (missing.length || extra.length) fail("surface_membership_mismatch", `${label}:missing=${missing.join(",")}:extra=${extra.join(",")}`);
}

function publicTaxonomy(row) {
  return Object.freeze({
    molecule_tags: row.molecule_tags,
    brand_name_tags: row.brand_name_tags,
    delivery_form_tags: row.delivery_form_tags,
    treatment_type_tags: row.treatment_type_tags,
    treatment_type_state: row.treatment_type_state,
    verified_as_of: row.verified_as_of,
    ...(row.claim_qualification ? { claim_qualification: row.claim_qualification, source_urls: row.source_urls } : {}),
    normalized_sha256: row.normalized_sha256,
  });
}

export function therapeuticTokens(row) {
  return [...new Set([
    ...(row?.molecule_tags || []), ...(row?.brand_name_tags || []), ...(row?.delivery_form_tags || []),
  ])].sort();
}

export function projectTherapeuticSurfaces({ registry, profileId, pages, quizConfig, programs = [], asOf = new Date(), staleEvidence = "fail" }) {
  const validated = validateTherapeuticTaxonomy(registry, profileId, { asOf, staleEvidence });
  const rawCanonical = canonicalTherapeuticPrograms(registry, profileId, validated.programs);
  const dynamic = validateProviderDynamicProfiles(registry, profileId, { asOf, staleEvidence });
  const consumerAuthorities = validateConsumerTherapeuticAuthorities(registry, profileId, { asOf, dynamic, staleEvidence });
  const effectiveFilters = consumerTherapeuticFilters(registry, profileId, { asOf, dynamic, staleEvidence });
  // A canonical provider with any stale source row renders no therapeutic tags
  // (claim held back, treatment unknown) while its row, links and holds stay intact.
  const heldStaleSources = new Set(validated.held_stale);
  const registryByRenderId = new Map((registry?.programs || []).map((program) => [renderId(program), program]));
  const heldCanonical = new Set([...heldStaleSources].map((sourceId) => {
    const identity = registryByRenderId.get(sourceId)?.profiles?.[profileId]?.canonical_identity;
    return identity?.role === "alias" ? String(identity.canonical_program_id || "") : sourceId;
  }).concat(dynamic.held_stale || []));
  const withheld = () => ({ molecule_tags: [], brand_name_tags: [], delivery_form_tags: [],
    treatment_type_tags: [], treatment_type_state: "not_verified", claim_qualification: "", claim_held_stale: true });
  const allowedPreferenceValues = new Set([...effectiveFilters.map((item) => item.value), "not_sure"]);
  const projectedPrograms = new Map([...dynamic.profiles.entries()].map(([programId, profile]) => {
    const raw = rawCanonical.get(programId);
    if (!raw) fail("dynamic_profile_taxonomy_missing", programId);
    return [programId, Object.freeze({
      ...raw,
      ...profile.taxonomy,
      ...(heldCanonical.has(programId) ? withheld() : {}),
      provider: profile.provider,
      render_label: null,
      verified_as_of: profile.verified_as_of,
      normalized_sha256: raw.claim_qualification
        ? stableSha256({taxonomy_sha256:raw.normalized_sha256, dynamic_profile_sha256:profile.normalized_sha256})
        : profile.normalized_sha256,
    })];
  }));
  const reviewOnlyIds=new Set((registry.programs||[]).filter(p=>p.profiles?.[profileId]?.surface_membership==='review_only').map(p=>String(p.review_slug||p.program_id)));
  const expectedIds = new Set([...projectedPrograms.keys()].filter(id=>!reviewOnlyIds.has(id)));
  // S9-2: a receipted hold stays a DARK ROW in the raw money/quiz source
  // documents - the installed held-aware census requires the row to be present
  // and projects it off, and a missing held row is itself a fail-closed failure.
  // So the money-page membership expectation is surface-scoped the same way the
  // quiz pool already is: a source id the canonical set no longer carries is
  // dropped from the comparison ONLY when its registry row proves it is held,
  // unwired and scoped off every conversion surface. Anything else still fails.
  const scopedOffIds = new Set((registry.programs || [])
    .filter((item) => {
      const profile = item.profiles?.[profileId];
      const scopedOff = ["compare", "quiz", "calculator", "go"]
        .every((surface) => profile?.surfaces?.[surface] === false);
      if (!scopedOff) return false;
      // Two provable non-members: a receipted, unwired hold, and a declared
      // alias identity that consolidates onto its canonical record.
      if(reviewOnlyIds.has(String(item.review_slug||item.program_id)))return true;
      return item.commercial_state === "held"
        ? profile.wired === false
        : profile.canonical_identity?.role === "alias";
    })
    .map((item) => String(item.review_slug || item.program_id)));
  const requiredPaths = new Set(validated.document.required_surface_paths);
  const projectedPages = pages.map((page) => {
    const rows = (page.rows || []).filter(row=>!reviewOnlyIds.has(String(row.program_id))).map((row) => {
      const taxonomy = projectedPrograms.get(String(row.program_id));
      if (!taxonomy) return { ...row };
      const linkState = commercialLinkState(row, "compare");
      if (linkState !== taxonomy.commercial_link_state.compare) fail("compare_link_state_mismatch", `${page.path}:${row.program_id}`);
      return {
        ...row,
        render_provider: taxonomy.render_label || row.provider,
        therapeutic_taxonomy: publicTaxonomy(taxonomy),
        therapeutic_tokens: therapeuticTokens(taxonomy),
        commercial_link_state: linkState,
      };
    });
    if (requiredPaths.has(page.path)) assertSameSet(surfaceScopedIds(exactIds(rows, page.path), scopedOffIds), expectedIds, page.path);
    return {
      ...page,
      therapeutic_facets_enabled: requiredPaths.has(page.path),
      consumer_therapeutic_filters: effectiveFilters.map((item) => item.value),
      consumer_therapeutic_authority_schema: registry.consumer_therapeutic_authorities.schema,
      canonical_provider_ids: Object.freeze([...expectedIds].sort()),
      provider_dynamic_profile_schema: dynamic.document.schema,
      rows,
    };
  });
  for (const route of requiredPaths) {
    if (!projectedPages.some((page) => page.path === route)) fail("required_surface_missing", route);
  }
  const candidates = (quizConfig.candidates || []).filter(row=>!reviewOnlyIds.has(String(row.program_id))).map((candidate) => {
    const taxonomy = projectedPrograms.get(String(candidate.program_id));
    if (!taxonomy) return { ...candidate };
    // quiz-config is deliberately link-dark at rest. renderQuiz resolves the
    // approved affiliate map and checks the effective state against this value;
    // a raw config may never escalate a taxonomy row that is editorial-only.
    const rawLinkState = commercialLinkState(candidate, "quiz");
    if (rawLinkState === "live" && taxonomy.commercial_link_state.quiz !== "live") {
      fail("quiz_link_state_escalation", candidate.program_id);
    }
    return {
      ...candidate,
      render_display_name: taxonomy.render_label || candidate.display_name,
      therapeutic_taxonomy: publicTaxonomy(taxonomy),
      therapeutic_tags: therapeuticTokens(taxonomy),
      commercial_link_state: taxonomy.commercial_link_state.quiz,
    };
  });
  // OPERATOR MODEL 2026-08-13: the quiz pool is the canonical provider set
  // MINUS programs whose registry row scopes the quiz surface off with a
  // documented surface_restriction_basis exception (validated by the registry
  // projection). trt-nation is editorial_link_dark and no longer a quiz
  // candidate while it keeps its comparison row, so the quiz expectation is
  // surface-scoped instead of identical to the compare expectation.
  const quizScopedIds = new Set([...expectedIds].filter((programId) => {
    const program = (registry.programs || []).find((item) => String(item.review_slug || item.program_id) === String(programId));
    return !program || program.profiles?.[profileId]?.surfaces?.quiz === true;
  }));
  assertSameSet(surfaceScopedIds(exactIds(candidates, "quiz"), scopedOffIds), quizScopedIds, "quiz");
  return Object.freeze({
    // Blog ledger records share the SAME validated canonical public taxonomy as
    // comparison rows; ledger identity, prose, clocks and link flags stay intact.
    programs: programs.map((program) => {
      const taxonomy = projectedPrograms.get(String(program.program_id));
      return taxonomy ? { ...program, therapeutic_taxonomy: publicTaxonomy(taxonomy) } : { ...program };
    }),
    pages: projectedPages,
    quizConfig: {
      ...quizConfig,
      questions: (quizConfig.questions || []).map((question) => question.id === "medication_preference"
        ? { ...question, options: (question.options || []).filter((option) => allowedPreferenceValues.has(option.value)) }
        : question),
      candidates,
      therapeutic_taxonomy_schema: validated.document.schema,
      provider_dynamic_profile_schema: dynamic.document.schema,
    },
    projection: validated,
    validated,
    dynamic,
    consumerAuthorities,
    consumerTherapeuticFilters: effectiveFilters,
    claimsHeldStale: Object.freeze([...heldCanonical].sort()),
    projectedPrograms,
    includeProgramIds: Object.freeze([...expectedIds].sort()),
  });
}

export function candidatesForMedicationPreference(candidates, preference) {
  if (preference === undefined || preference === null || preference === "" || preference === "not_sure") return [...candidates];
  if (!MEDICATION_PREFERENCES.includes(preference)) return [];
  return candidates.filter((candidate) => (candidate.therapeutic_tags || therapeuticTokens(candidate.therapeutic_taxonomy)).includes(preference));
}

export function matchesTherapeuticFacets(taxonomy, selectedTherapeutics = [], treatment = "all") {
  const requested = [...new Set(selectedTherapeutics)];
  if (requested.some((value) => !THERAPEUTIC_FILTERS.some((item) => item.value === value))) return false;
  const selected = requested;
  const tokens = therapeuticTokens(taxonomy);
  const therapeuticMatch = selected.length === 0 || selected.some((value) => tokens.includes(value));
  if (!therapeuticMatch) return false;
  if (treatment === "all") return true;
  const types = taxonomy?.treatment_type_tags || [];
  if (treatment === "either") return types.length > 0;
  if (!TREATMENT_FILTERS.some((item) => item.value === treatment)) return false;
  return types.includes(treatment);
}
