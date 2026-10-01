import fs from 'node:fs';
import crypto from 'node:crypto';
const [candidate, root, pid] = process.argv.slice(2);
const tax = await import(candidate + '/src/therapeutic-taxonomy.mjs');
const dyn = await import(candidate + '/src/provider-dynamic-profiles.mjs');
const read = p => JSON.parse(fs.readFileSync(p, 'utf8'));
const write = (p, value) => {
  const text = JSON.stringify(value, null, 2) + '\n';
  if (!fs.existsSync(p) || fs.readFileSync(p, 'utf8') !== text) fs.writeFileSync(p, text);
};
const sha = p => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');
const reg = read(root + '/offline/data/program-registry.json');
const matrix = read(root + '/offline/data/provider-pricing-matrix.json');
reg.therapeutic_taxonomy.expected_counts = tax.deriveFacetCounts(reg.therapeutic_taxonomy.programs);
reg.provider_dynamic_profiles.expected_counts = dyn.deriveDynamicFacetCounts(reg.provider_dynamic_profiles.profiles);
const taxonomyIds = [pid, ...reg.provider_dynamic_profiles.profiles[pid].aliases.map(alias => alias.render_id)];
for (const id of taxonomyIds) {
  reg.therapeutic_taxonomy.programs[id].normalized_sha256 = tax.normalizedTaxonomySha256(reg.therapeutic_taxonomy.programs[id]);
}
reg.provider_dynamic_profiles.profiles[pid].normalized_sha256 = dyn.normalizedProfileSha256(reg.provider_dynamic_profiles.profiles[pid]);
const rows = Object.values(matrix.providers).flatMap(p => p.medications || []);
matrix.coverage = {medication_rows: rows.length, providers_pricing_unavailable: Object.values(matrix.providers).filter(p => !p.medications?.length).length, providers_total: Object.keys(matrix.providers).length, providers_with_evidenced_pricing: Object.values(matrix.providers).filter(p => p.medications?.length).length, rows_with_intro_month_1: rows.filter(r => r.month_1_basis === 'intro').length};
write(root + '/offline/data/program-registry.json', reg);
write(root + '/offline/data/provider-pricing-matrix.json', matrix);
const isolated = structuredClone(reg);
const memberIds = reg.provider_dynamic_profiles.profiles[pid].registry_program_ids;
isolated.programs = reg.programs.filter(p => memberIds.includes(p.program_id));
const renderIds = isolated.programs.map(p => p.review_slug || p.program_id);
isolated.therapeutic_taxonomy.programs = Object.fromEntries(Object.entries(reg.therapeutic_taxonomy.programs).filter(([id]) => renderIds.includes(id)));
isolated.therapeutic_taxonomy.expected_counts = tax.deriveFacetCounts(isolated.therapeutic_taxonomy.programs);
isolated.provider_dynamic_profiles.profiles = {[pid]: reg.provider_dynamic_profiles.profiles[pid]};
isolated.provider_dynamic_profiles.expected_counts = dyn.deriveDynamicFacetCounts(isolated.provider_dynamic_profiles.profiles);
const target_validators = {};
for (const [name, fn] of [['taxonomy', tax.validateTherapeuticTaxonomy], ['dynamic', dyn.validateProviderDynamicProfiles]]) {
  try { fn(isolated, 'glp-clinic-scout', {asOf: new Date()}); target_validators[name] = {status: 'PASS'}; }
  catch (e) { target_validators[name] = {status: 'FAIL', error: e.message}; }
}
const targets = [];
for (const [name, next] of [['program-registry.json', reg], ['provider-pricing-matrix.json', matrix]]) {
  const source = candidate + '/data/' + name, original = read(source), operations = [];
  const escape = k => k.replaceAll('~', '~0').replaceAll('/', '~1');
  const walk = (a, b, p = '') => {
    if (JSON.stringify(a) === JSON.stringify(b)) return;
    if (a && b && typeof a === 'object' && typeof b === 'object' && !Array.isArray(a) && !Array.isArray(b)) {
      for (const k of Object.keys(b)) { const q = p + '/' + escape(k); if (!(k in a)) operations.push({op: 'add', path: q, value: b[k]}); else walk(a[k], b[k], q); }
      for (const k of Object.keys(a)) if (!(k in b)) operations.push({op: 'test', path: p + '/' + escape(k), value: a[k]}, {op: 'remove', path: p + '/' + escape(k)});
    } else operations.push({op: 'test', path: p, value: a}, {op: 'replace', path: p, value: b});
  };
  walk(original, next);
  targets.push({path: source, source_sha256: sha(source), operations});
}
write(root + '/DATA-PATCH.json', {schema: 'ws-r-json-patch-v1', format: 'RFC6902 per target; check source_sha256 first', applied_to_candidate: false, targets});
write(root + '/VALIDATION.json', {target_validators, thresholds_unchanged: reg.therapeutic_taxonomy.freshness_max_days === read(candidate + '/data/program-registry.json').therapeutic_taxonomy.freshness_max_days, applied_to_candidate: false});
if (Object.values(target_validators).some(v => v.status !== 'PASS')) process.exitCode = 1;
