// Offline shape-only fixture from deployed src/program-registry.mjs.
// Source SHA-256: 05724dc3d63f6424c772261ebf8f8798421efc7542a3c5d8c5ce9e4c31341bd8
// Captured 2026-10-01. No whole-build or publication authority is implied.
const SURFACES = Object.freeze(["compare", "quiz", "calculator", "review", "go", "sitemap"]);
const NETWORK_ID_SLUG = /^(?:awin|katalys|cj-affiliate|partnerize)-\d+$/;
const ROUTE_SLUG = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

function profileFor(program, profileId) {
  return program?.profiles?.[profileId] || null;
}

export function validateRegistryShape(registry, profileId) {
  if (registry?.schema !== "site-program-registry-v1" || !Array.isArray(registry.programs)) throw new Error("program_registry_invalid");
  const siteProfile = registry.site_profiles?.[profileId];
  if (!siteProfile) throw new Error(`site_profile_unknown:${profileId}`);
  if (siteProfile.auto_generate !== false || siteProfile.auto_publish !== false) throw new Error(`site_profile_automation_must_be_held:${profileId}`);
  const ids = new Set();
  const reviewSlugs = new Set();
  const goRoutes = new Set();
  for (const program of registry.programs) {
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(program.program_id || "") || ids.has(program.program_id)) throw new Error(`program_registry_identity_invalid:${program.program_id || "missing"}`);
    ids.add(program.program_id);
    if (!program.display_name || !["live", "held"].includes(program.commercial_state)) throw new Error(`program_registry_state_invalid:${program.program_id}`);
    const routeMatch = String(program.review_path || "").match(/^\/reviews\/([a-z0-9]+(?:-[a-z0-9]+)*)\/$/);
    const reviewSlug = routeMatch?.[1];
    const expectedSlug = program.review_slug || program.program_id;
    const identityProfile = profileFor(program, profileId);
    const identityOnlyAlias = identityProfile?.canonical_identity?.role === "alias"
      && program.commercial_state === "held" && identityProfile.wired === false
      && SURFACES.every(surface => identityProfile.surfaces?.[surface] === false)
      && registry.programs.some(target => target.program_id === identityProfile.canonical_identity.canonical_program_id
        && target.program_id !== program.program_id && target.profiles?.[profileId]?.canonical_identity?.role === "canonical");
    if ((!identityOnlyAlias || Object.hasOwn(program, "review_path"))
      && (!reviewSlug || !ROUTE_SLUG.test(expectedSlug) || reviewSlug !== expectedSlug || reviewSlugs.has(reviewSlug))) throw new Error(`program_registry_review_path_invalid:${program.program_id}`);
    if (NETWORK_ID_SLUG.test(reviewSlug)) throw new Error(`coverage_network_id_review_slug_forbidden:${profileId}:${program.program_id}:${reviewSlug}`);
    if (reviewSlug) reviewSlugs.add(reviewSlug);
    if (program.go?.route !== `/go/${expectedSlug}` || !program.go.entry_state || goRoutes.has(program.go.route)) throw new Error(`program_registry_go_entry_invalid:${program.program_id}`);
    goRoutes.add(program.go.route);
    const profile = profileFor(program, profileId);
    if (!profile) continue;
    if (typeof profile.vertical_fit !== "boolean" || typeof profile.wired !== "boolean") throw new Error(`program_registry_profile_invalid:${profileId}:${program.program_id}`);
    if (typeof profile.data_profile?.has_price !== "boolean" || typeof profile.data_profile?.has_review_facts !== "boolean") throw new Error(`program_registry_data_profile_invalid:${profileId}:${program.program_id}`);
    for (const surface of SURFACES) {
      if (typeof profile.surfaces?.[surface] !== "boolean") throw new Error(`program_registry_surface_flag_invalid:${profileId}:${program.program_id}:${surface}`);
    }
    for (const [surface, exception] of Object.entries(profile.exceptions || {})) {
      if (!SURFACES.includes(surface) || exception?.enabled !== true) throw new Error(`coverage_exception_flag_invalid:${profileId}:${program.program_id}:${surface}`);
      if (typeof exception.reason !== "string" || !exception.reason.trim()) throw new Error(`coverage_exception_reason_missing:${profileId}:${program.program_id}:${surface}`);
      // OPERATOR MODEL 2026-08-13: any surface scoping must name its documented
      // restriction basis on the row itself; prose alone does not scope.
      if (typeof exception.surface_restriction_basis !== "string" || !exception.surface_restriction_basis.trim()) throw new Error(`coverage_exception_restriction_basis_missing:${profileId}:${program.program_id}:${surface}`);
    }
  }
  return siteProfile;
}
