#!/usr/bin/env python3
"""Registered discovery entry: reuse fresh proven API catalogs, not public guesses."""
from __future__ import annotations
import datetime as dt
import json
from pathlib import Path
import sys

BUSINESS = Path('/home/ubuntu/business')
STATE = BUSINESS / 'reports/glp-clinic-scout/affiliate-machine/catalog-batch/checkpoint.json'


def adapters(core, *, state=STATE, now=None):
    """Expose only fresh, complete catalogs; preserve all negative row metadata.

    Off-vertical rows aren't provider seeds. Passing an entire 20k retail
    catalog to the public search fallback would starve every later source.
    Unknown/incomplete catalogs retain the core's honest blocked disposition.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if not state.exists():
        return {}
    if state.is_symlink():
        raise ValueError('unsafe_catalog_checkpoint')
    doc = json.loads(state.read_text())
    if doc.get('schema') != 'affiliate-catalog-batch-v1':
        raise ValueError('unsupported_catalog_checkpoint')
    result = {}
    for network, row in doc['networks'].items():
        age = (now - dt.datetime.fromisoformat(row['attempted_at'])).total_seconds()
        if network == 'xentraffic' or not row.get('complete') or not 0 <= age <= 86400:
            continue
        programs = []
        for program in row['inventory'].get('programs') or []:
            name = str(program.get('name') or '')
            description = ' '.join(str(program.get(field) or '') for field in
                                   ('description', 'category', 'categories', 'vertical'))
            url = str(program.get('official_url') or program.get('url') or '')
            if core.glp_evidence(name, name + ' ' + description, url)['fit']:
                programs.append(dict(program))
        result[network] = {'status': 'AUTHENTICATED_CATALOG_COMPLETE',
                           'programs': programs, 'catalog_observed_at_utc': row['attempted_at'],
                           'reported_total': row['inventory'].get('reported_total')}
    return result


def main(argv=None):
    sys.path.insert(0, str(BUSINESS / 'scripts'))
    import affiliate_supply_discovery as core
    original = core.live_platform_discovery_adapters
    def api_first():
        # Official Partnerize discovery remains available where no complete
        # catalog checkpoint exists. Never construct a portal login session.
        return {**original(), **adapters(core)}
    core.live_platform_discovery_adapters = api_first
    return core.main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
