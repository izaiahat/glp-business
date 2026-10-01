// Offline native snapshot SHA-256: 1c1d1078c90b8d528973141422137a6f92e73377c7c503bb136585a6aaa70a80
// One canonical render identity per merchant.
//
// WHY THIS EXISTS
// ---------------
// 2026-08-06: the cost calculator offered "Shed" and "SHED" as two separate
// providers, the comparison table listed the brand twice, and the live provider
// count said 29 when 28 distinct merchants were covered. Root cause: two wire-in
// waves registered the same merchant twice. The Awin lane registered it as
// `shedrx` ("Shed", shedrx.com) in July; the Katalys READY6 lane registered offer
// 1516 as `katalys-1516` ("SHED", shedrx.com) on 2026-08-06. The READY6 brand-slug
// scaffold even carries an `existing_release_alias` field for exactly this case
// and it was left null, so nothing compared the new brand against the ones already
// shipped. Every downstream surface then assembled its list from registry rows and
// faithfully rendered the merchant twice.
//
// WHAT THIS DOES
// --------------
// Derives a brand identity for every registry program from two independent
// signals - the case- and punctuation-folded display name, and the registrable
// domain of the first-party source URL the ledger cites - and unions them. A
// group with more than one member elects exactly ONE canonical program by a
// deterministic rule; the others are aliases. Aliases keep their own review page
// (the URL is live and indexed) but never enter an aggregate surface: not the
// comparison table, not the calculator, not the quiz, not a count.
//
// The derivation is the enforcement. A future wave that mints "SHEDRX" or a
// second offer on the same domain FAILS THE BUILD unless the registry declares
// which row is canonical and why - it cannot silently become a second provider.

/** Surfaces that assemble a LIST of providers. An alias may not appear in one. */
export const AGGREGATE_SURFACES = Object.freeze(["compare", "quiz", "calculator", "go"]);

/** Surfaces that render ONE provider's own page. An alias keeps these. */
export const PER_PROVIDER_SURFACES = Object.freeze(["review", "sitemap"]);

const SOURCE_KIND_RANK = Object.freeze({
  money: 0, fastpath: 1, approved53_ready6: 2, legacy_vertical: 3,
});

/** Case- and punctuation-insensitive brand key. "Shed" and "SHED" are one key. */
export function identityKey(displayName) {
  return String(displayName ?? "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

/** Registrable domain of a first-party URL, or "" when there is not one. */
export function registrableHost(url) {
  const match = /^https?:\/\/([^/?#]+)/i.exec(String(url ?? ""));
  if (!match) return "";
  const labels = match[1].toLowerCase().replace(/:\d+$/, "").split(".").filter(Boolean);
  return labels.length >= 2 ? labels.slice(-2).join(".") : labels.join(".");
}

function link(parent, a, b) {
  const find = (node) => {
    while (parent.get(node) !== node) {
      parent.set(node, parent.get(parent.get(node)));
      node = parent.get(node);
    }
    return node;
  };
  const rootA = find(a);
  const rootB = find(b);
  if (rootA !== rootB) parent.set(rootA, rootB);
}

function resolve(parent, node) {
  while (parent.get(node) !== node) node = parent.get(node);
  return node;
}

function enabledAggregateSurfaces(program, profileId) {
  const surfaces = program?.profiles?.[profileId]?.surfaces || {};
  return AGGREGATE_SURFACES.filter((surface) => surfaces[surface] === true).length;
}

/**
 * Group registry programs by merchant identity and elect one canonical each.
 *
 * Pure: same registry + ledger in, same grouping out. No clock, no network.
 *
 * @returns {{groups: Map<string, {identity_key: string, canonical: string, members: string[], aliases: string[]}>,
 *            canonicalOf: Map<string, string>, aliasIds: Set<string>}}
 */
export function deriveIdentityGroups(registry, profileId, ledgerPrograms = []) {
  const programs = registry?.programs || [];
  const parent = new Map(programs.map((program) => [program.program_id, program.program_id]));
  const byNameKey = new Map();
  const byHost = new Map();
  const sourceUrlById = new Map();
  for (const entry of ledgerPrograms) {
    for (const key of [entry.program_id, entry.registry_id].filter(Boolean)) {
      if (entry.source_url) sourceUrlById.set(String(key), entry.source_url);
    }
  }
  for (const program of programs) {
    const nameKey = identityKey(program.display_name);
    if (nameKey) {
      if (byNameKey.has(nameKey)) link(parent, program.program_id, byNameKey.get(nameKey));
      else byNameKey.set(nameKey, program.program_id);
    }
    const host = registrableHost(sourceUrlById.get(program.program_id)
      || sourceUrlById.get(program.review_slug || ""));
    if (host) {
      if (byHost.has(host)) link(parent, program.program_id, byHost.get(host));
      else byHost.set(host, program.program_id);
    }
  }
  const membersByRoot = new Map();
  for (const program of programs) {
    const root = resolve(parent, program.program_id);
    if (!membersByRoot.has(root)) membersByRoot.set(root, []);
    membersByRoot.get(root).push(program);
  }
  const groups = new Map();
  const canonicalOf = new Map();
  const aliasIds = new Set();
  // Both spellings. A network program is `katalys-1516` in the registry and
  // `shed` everywhere a reader (or a side file keyed by render slug) names it;
  // suppressing only one spelling leaves the other alive on some surface.
  const aliasKeys = new Set();
  for (const members of membersByRoot.values()) {
    // Deterministic election: the row a reader already has the most of - most
    // aggregate surfaces, then the earliest-established lane, then id order.
    const ordered = [...members].sort((left, right) =>
      enabledAggregateSurfaces(right, profileId) - enabledAggregateSurfaces(left, profileId)
      || (SOURCE_KIND_RANK[left.source_kind] ?? 9) - (SOURCE_KIND_RANK[right.source_kind] ?? 9)
      || String(left.program_id).localeCompare(String(right.program_id)));
    const canonical = ordered[0];
    const key = identityKey(canonical.display_name) || canonical.program_id;
    const record = {
      identity_key: key,
      canonical: canonical.program_id,
      members: ordered.map((program) => program.program_id),
      aliases: ordered.slice(1).map((program) => program.program_id),
    };
    groups.set(key, record);
    for (const program of ordered) canonicalOf.set(program.program_id, canonical.program_id);
    for (const alias of ordered.slice(1)) {
      aliasIds.add(alias.program_id);
      aliasKeys.add(alias.program_id);
      if (alias.review_slug) aliasKeys.add(alias.review_slug);
    }
  }
  return { groups, canonicalOf, aliasIds, aliasKeys };
}

/**
 * The build gate. A duplicate merchant is only allowed to exist in the registry
 * when the registry SAYS SO, in writing, on the alias row itself. An undeclared
 * duplicate throws here rather than shipping a second dropdown option.
 */
export function assertDeclaredIdentityGroups(registry, profileId, ledgerPrograms = []) {
  const derived = deriveIdentityGroups(registry, profileId, ledgerPrograms);
  const byId = new Map((registry.programs || []).map((program) => [program.program_id, program]));
  for (const group of derived.groups.values()) {
    if (group.members.length < 2) continue;
    for (const memberId of group.members) {
      const declaration = byId.get(memberId)?.profiles?.[profileId]?.canonical_identity;
      const isAlias = group.aliases.includes(memberId);
      if (!declaration) {
        throw new Error(`provider_identity_duplicate_undeclared:${group.identity_key}:${memberId}:canonical_is:${group.canonical}`);
      }
      if (declaration.identity_key !== group.identity_key
        || declaration.canonical_program_id !== group.canonical
        || declaration.role !== (isAlias ? "alias" : "canonical")) {
        throw new Error(`provider_identity_declaration_mismatch:${memberId}`);
      }
      if (isAlias && String(declaration.reason || "").trim().length <= 40) {
        throw new Error(`provider_identity_alias_reason_missing:${memberId}`);
      }
    }
  }
  return derived;
}

/**
 * Case-insensitive duplicate guard for one assembled surface list. Called at
 * every point a list of providers is built, so a dedup bug anywhere fails the
 * build instead of reaching a reader.
 */
export function assertUniqueRenderIdentity(entries, surface) {
  const seen = new Map();
  for (const entry of entries) {
    const label = entry?.display_name ?? entry?.provider ?? entry?.label ?? "";
    const key = identityKey(label);
    if (!key) continue;
    if (seen.has(key)) {
      throw new Error(`surface_duplicate_render_identity:${surface}:${key}:${seen.get(key)}:${entry.program_id ?? "?"}`);
    }
    seen.set(key, entry.program_id ?? key);
  }
  return entries;
}
