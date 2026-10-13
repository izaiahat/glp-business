"""GLP-44 weekly keyword-gap plan: governs refill topics and updater targets.

Pure function over already-bound weekly inputs; no I/O, network or queue writes.
Follows reports/ops/seo-research-20261009/FINDINGS.md: 1/day publishing capacity
is not an obligation; aim for 3-5 evidence-complete new pages plus 2-4 meaningful
updates per week; existing relevant owner -> improve it, distinct decision -> new
URL; unknown demand is not zero and never qualifies a topic; no forced rewrites.
"""
from __future__ import annotations

import re

GENERIC = frozenset({
    'glp', 'glp1', '1', 'reviews', 'review', 'weight', 'loss', 'health', 'rx', 'meds',
    'the', 'a', 'of', 'for', 'and', 'vs', 'is', 'how', 'to', 'what', 'online', 'clinic',
    'tirzepatide', 'semaglutide', 'cost', 'price', 'pricing', 'guides', 'reviews', 'best',
    'compounded', 'med', 'md', 'care', 'program', 'pharmacy', 'login', 'legit',
})
POLICY = {
    'source': 'reports/ops/seo-research-20261009/FINDINGS.md',
    'publish_cadence': 'at most 1 new article per weekday; capacity, not an obligation',
    'new_pages_per_week': [3, 5],
    'meaningful_updates_per_week': [2, 4],
    'no_op_allowed': True,
    'semantic_materiality_required_for_updates': True,
    'no_date_only_or_paraphrase_updates': True,
    'spam_policy': ['No scaled near-duplicate pages; one canonical URL per consumer decision',
                    'Existing relevant owner -> improve that URL, never a parallel article',
                    'Affiliate links rel=sponsored with conspicuous disclosure',
                    'Descriptive internal links to the next consumer decision; no stuffed anchors'],
    'holds': ['Fella Health permanently excluded', 'Existing provider holds preserved',
              'Health/commercial claims require substantiation and independent review'],
}


def norm(value):
    return ' '.join(str(value or '').lower().split())


def tokens(value):
    text = re.sub(r'\bglp\s*-?\s*1\b', 'glp1', str(value or '').lower())
    return {t for t in re.findall(r'[a-z0-9]+', text) if t not in GENERIC and len(t) > 2}


def _positive(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else None


def link_targets(keyword, target_route, intent, live_routes, limit=3):
    """Live, crawlable next-decision links: same-entity pages then the intent hub."""
    wanted = tokens(keyword)
    scored = []
    for route in sorted(live_routes):
        if route == target_route or 'fella' in route:
            continue
        overlap = wanted & tokens(route.replace('/', ' ').replace('-', ' '))
        if overlap:
            scored.append((-len(overlap), route))
    picks = [r for _, r in sorted(scored)][:limit - 1]
    hub = {'commercial_review': '/reviews/', 'commercial_comparison': '/providers/',
           'commercial_cost': '/providers/'}.get(intent, '/providers/')
    if hub in live_routes and hub not in picks:
        picks.append(hub)
    return picks[:limit]


def plan_keyword_gaps(extended_supply, gap_volume, *, held_routes=(), live_routes=(), limit=100):
    """Return (keyword_gaps, exclusions). Only rows with current positive demand qualify."""
    volume_ok = isinstance(gap_volume, dict) and gap_volume.get('ok') is True
    volumes = {norm(k): v for k, v in (gap_volume or {}).get('rows', {}).items()
               if isinstance(v, dict) and v.get('ok')} if volume_ok else {}
    held = {str(r) for r in held_routes if r}
    live = {str(r) for r in live_routes if r}
    rows, exclusions, seen = [], [], set()
    for row in (extended_supply or {}).get('gaps') or []:
        if not isinstance(row, dict):
            continue
        keyword, route = row.get('keyword'), row.get('target_route')
        key = norm(keyword)
        reason = None
        if not key or not isinstance(route, str) or not route.startswith('/'):
            reason = 'missing_keyword_or_route'
        elif 'fella' in key or 'fella' in route.lower():
            reason = 'fella_excluded'
        elif route in held:
            reason = 'provider_hold_preserved'
        elif route in live:
            reason = 'existing_live_owner_refresh_not_new_article'
        elif row.get('queue_state') != 'READY_FOR_RESEARCH':
            reason = 'not_ready_for_research'
        elif (key, route) in seen:
            reason = 'duplicate_decision'
        if reason:
            exclusions.append({'keyword': keyword, 'target_route': route, 'reason': reason})
            continue
        quick = row.get('ws_s_quickwin') or {}
        competitor = quick.get('competitor') or {}
        own = quick.get('our_footprint') or {}
        current_volume = _positive((volumes.get(key) or {}).get('volume'))
        competitor_volume = _positive(competitor.get('search_volume'))
        basis = []
        if current_volume:
            basis.append('dataforseo_google_ads_search_volume_current_week')
        if competitor_volume:
            basis.append('dataforseo_competitor_ranked_keyword_current_week')
        if quick.get('gsc_query_observed_28d') is True:
            basis.append('gsc_query_observed_28d')
        if not (current_volume or competitor_volume):
            exclusions.append({'keyword': keyword, 'target_route': route,
                               'reason': 'unknown_or_no_current_positive_demand_not_zero'})
            continue
        seen.add((key, route))
        binding = row.get('strategy_binding') or {}
        intent = quick.get('intent') or 'navigational_or_informational_unknown'
        rows.append({
            'keyword': keyword, 'target_route': route, 'action': 'new_article',
            'opportunity_class': row.get('opportunity_class'),
            'source_opportunity_id': row.get('source_opportunity_id'),
            'intent': intent,
            'cluster': {'cluster_id': binding.get('cluster_id'), 'cluster_name': binding.get('cluster_name'),
                        'depth_stage': binding.get('depth_stage'),
                        'entity_tokens': sorted(tokens(keyword))},
            'search_volume_current': current_volume,
            'competitor_search_volume': competitor_volume,
            'competitor_position': competitor.get('position'),
            'competitor_url': competitor.get('url'),
            'our_position': own.get('position'),
            'keyword_difficulty': competitor.get('keyword_difficulty'),
            'demand_basis': basis,
            'evidence_class': 'CURRENT_MEASURED_DEMAND',
            'internal_link_targets': link_targets(keyword, route, intent, live),
            'consumer_review_required': True, 'publication_authorized': False,
        })
    rank = {'B': 0, 'C': 1}
    rows.sort(key=lambda r: (rank.get(r['opportunity_class'], 2),
                             -(r['search_volume_current'] or r['competitor_search_volume'] or 0),
                             norm(r['keyword'])))
    rows = rows[:limit]
    for index, row in enumerate(rows, 1):
        row['priority'] = index
        row['this_week_target'] = index <= POLICY['new_pages_per_week'][1]
    return rows, exclusions


def update_plan(refresh_list, limit=None):
    limit = limit or POLICY['meaningful_updates_per_week'][1]
    plan = []
    for entry in refresh_list or []:
        route = entry.get('route')
        if not isinstance(route, str) or 'fella' in route.lower():
            continue
        plan.append({'route': route, 'queries': entry.get('queries', []), 'priority': len(plan) + 1,
                     'action': 'refresh_existing_page', 'materiality_required': True,
                     'no_op_allowed': True, 'date_only_change_forbidden': True})
        if len(plan) >= limit:
            break
    return plan
