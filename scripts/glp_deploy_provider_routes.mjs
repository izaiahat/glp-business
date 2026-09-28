// Candidate-only execution of the shipped /go handler. No network or production D1.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
const root = process.argv[2];
const { providers, configuration, configuration_error, source_error } = JSON.parse(fs.readFileSync(0, 'utf8'));
globalThis.fetch = async () => { throw new Error('sensor_network_forbidden'); };
const { GO_RUNTIME_MANIFEST: manifest } = await import(pathToFileURL(path.join(root, 'functions/go/program-manifest.js')));
const { onRequest } = await import(pathToFileURL(path.join(root, 'functions/go/[program].js')));
const entries = Object.values(manifest.programs);
const results = [];
for (const provider of providers) {
  const failures = [];
  if (configuration_error) failures.push(`runtime_configuration_unestablished:${configuration_error}`);
  if (source_error) failures.push(source_error);
  const matches = entries.filter(p => p.program_id === provider.program_id || p.route_slug === provider.slug || p.route === `/go/${provider.slug}`);
  const entry = matches.length === 1 ? matches[0] : null;
  if (!provider.registered) failures.push('provider_not_registered');
  if (provider.commercial_state !== 'live') failures.push('commercial_state_not_live');
  if (!entry) failures.push(matches.length ? 'route_ambiguous' : 'route_not_registered');
  const row = { slug: provider.slug, program_id: provider.program_id, failures };
  if (entry) {
    row.route = entry.route;
    if (entry.commercial_state !== 'live') failures.push('route_commercial_state_not_live');
    if (configuration.AFFILIATE_LIVE_GATE !== `operator-approved:${entry.operator_selection_receipt_sha256}`) failures.push('runtime_gate_mismatch');
    if (entry.live_link_allowed !== true || entry.promotion_authorized !== true) failures.push('route_not_live');
    let destination;
    try {
      destination = new URL(entry.tracking.base_url);
      if (destination.protocol !== 'https:' || /(^|\.)glpclinicscout\.com$/i.test(destination.hostname)) failures.push('destination_not_external_https');
    } catch { failures.push('destination_missing_or_invalid'); }
    // Exercise the candidate handler under its required production configuration.
    // Mapping is recorded in-memory; this proves build behavior, NOT live D1 or a conversion.
    if (!failures.length) {
      const mappings = [];
      const db = { prepare(sql) { return { bind(...args) { return { async run() {
        if (/^INSERT INTO affiliate_click_map\b/.test(sql)) mappings.push(args);
        else if (!/^UPDATE affiliate_click_map\b/.test(sql)) throw new Error('unexpected_sensor_sql');
        return { success: true };
      }, async first() {
        if (!/^INSERT INTO request_rate_limits\b/.test(sql)) throw new Error('unexpected_sensor_sql');
        return { request_count: 1 };
      } }; } }; } };
      try {
        const request = new Request(`https://glpclinicscout.com${entry.route}?s=reviews.rv2&v=a`, {
          headers: { 'User-Agent': 'GLP-Deploy-Sensor/1.0' },
        });
        const response = await onRequest({ request, params: { program: entry.route.split('/').pop() }, env: {
          AFFILIATE_RUNTIME_MODE: configuration.AFFILIATE_RUNTIME_MODE,
          AFFILIATE_LIVE_GATE: configuration.AFFILIATE_LIVE_GATE,
          GLP_DB: db,
        } });
        row.reason = response.headers.get('X-Redirect-Reason');
        row.attribution = response.headers.get('X-Attribution-Log');
        const location = new URL(response.headers.get('Location'));
        row.destination_host = location.hostname;
        if (response.status !== 307 || row.reason !== 'affiliate-live') failures.push('runtime_not_affiliate_live');
        if (row.attribution !== 'mapped' || mappings.length !== 1) failures.push('runtime_attribution_not_mapped');
        if (location.protocol !== 'https:' || location.hostname !== destination.hostname) failures.push('runtime_destination_mismatch');
        if (mappings.length === 1 && !decodeURIComponent(location.toString()).includes(mappings[0][0])) failures.push('destination_missing_mapped_click_id');
      } catch { failures.push('runtime_state_unestablished'); }
    }
  }
  results.push(row);
}
process.stdout.write(JSON.stringify(results));
