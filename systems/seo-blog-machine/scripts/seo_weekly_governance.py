"""Current week's hash-validated review governs topic/update eligibility.

This is a supply filter, never clinical, provider or publication authority.
Targets are not mandatory date bumps: no evidence-backed material change is a no-op.
"""
CADENCE = {'new_target_per_day': 1, 'new_ceiling_per_day': 1,
           'research_ceiling_per_day': 3, 'publication_weekdays': [0, 1, 2, 3, 4],
           'meaningful_updates_min_per_week': 2, 'meaningful_updates_max_per_week': 4,
           'no_op_allowed': True}


def refresh_candidates(rows, strategy):
    if strategy is None:
        return []
    priorities = {r['route']: r.get('priority', i + 1)
                  for i, r in enumerate(strategy.get('refresh_list') or [])
                  if isinstance(r.get('route'), str) and 'fella' not in r['route'].lower()}
    return sorted((r for r in rows if r.get('route') in priorities),
                  key=lambda r: (priorities[r['route']], r['route']))


def new_candidates(rows, strategy):
    if strategy is None:
        return []
    normal = lambda value: ' '.join(str(value or '').lower().split())
    gaps = strategy.get('keyword_gaps') or []
    return [row for row in rows if 'fella' not in normal(row.get('target_query'))
            and any(normal(gap.get('keyword')) == normal(row.get('target_query'))
                    and gap.get('target_route', row.get('target_route')) == row.get('target_route')
                    for gap in gaps)]
