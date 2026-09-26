// Exact accepted-base function; only export removed for standalone Node replay.
// Source: /home/ubuntu/business/site-system/flagship-v2/releases/sealed-d2w-release2-current-20260911T152308Z/scripts/postbuild-aeo-surface.mjs
// Source SHA256: eccf925385bb48cb52c600794f3bdb889c69ab9e595481f3fdbc57e3319f52d1
function resolveMaterialDate(route, materialDate, resolved, releaseDate) {
  if (!materialDate) return resolved;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(materialDate) || materialDate > releaseDate) throw new Error(`lastmod_material_date_future_or_invalid:${route}`);
  if (materialDate <= resolved.lastmod) return resolved;
  return { lastmod: materialDate, action: "declared_material_page_edit" };
}
