#!/usr/bin/env python3
"""Capture bounded native selectors for offline source CI, never publication.

Run only against the read-only canonical source named in BUILD-SOURCE-OF-TRUTH.
Verbatim function bodies avoid a second implementation of site filtering.
"""
import argparse
import hashlib
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    source = args.source_root / 'src/program-registry.mjs'
    text = source.read_text()
    ranges = [
        ('export function deriveProfileSurfaceSets(', '\n// OPERATOR MODEL'),
        ('export function projectMoneyPagesFromRegistry(', '\nconst STATE_CODE_BY_NAME'),
        ('export function projectQuizFromRegistry(', '\n\n// Selection is not permission'),
        ('export function deriveReviewRoutesFromRegistry(', '\nfunction parseSitemapRoutes'),
    ]
    selected = '\n\n'.join(text[text.index(start):text.index(end, text.index(start))] for start, end in ranges)
    identity = args.source_root / 'src/provider-identity.mjs'
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'glp331_provider_identity.mjs').write_text(
        '// Offline native snapshot SHA-256: ' + hashlib.sha256(identity.read_bytes()).hexdigest() + '\n' + identity.read_text())
    header = '''// Bounded native projection fixture, NOT a whole build/publication receipt.
import { readFile, mkdir, writeFile } from 'node:fs/promises';
import { validateRegistryShape } from './affiliate_terms_registry_validator.mjs';
import { AGGREGATE_SURFACES, deriveIdentityGroups, assertUniqueRenderIdentity } from './glp331_provider_identity.mjs';
const SURFACES = ['compare', 'quiz', 'calculator', 'review', 'go', 'sitemap'];
function profileFor(program, profileId) { return program?.profiles?.[profileId] || null; }
'''
    footer = '''
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
'''
    (args.out / 'glp331_native_surface_build.mjs').write_text(
        '// program-registry.mjs SHA-256: ' + hashlib.sha256(source.read_bytes()).hexdigest() + '\n' + header + selected + footer)


if __name__ == '__main__':
    main()
