"""Read-only route-bound CTR inputs from measured GSC rows."""
import datetime as dt
import hashlib
import json
import math

WINDOW_DAYS = 28
MIN_IMPRESSIONS = 50
POSITION_TOLERANCE = 1.0

def route_handoff(snapshot, route, topic_id, consumer):
    if not route.startswith('/guides/') or not route.endswith('/') or not topic_id:
        raise ValueError('ctr_handoff_identity_invalid')
    page = next((p for p in snapshot['pages'] if p['path'] == route), {'path': route})
    ctr = page.get('ctr_test')
    proposals = consumer(page, dt.date.fromisoformat(snapshot['as_of']))
    return {
        'schema': 'glp-loop-c-native-route-handoff-v1',
        'topic_id': topic_id, 'route': route, 'as_of': snapshot['as_of'],
        'source_coverage': snapshot.get('source_coverage', {}),
        'observability_source_digest': snapshot.get('source_digest'),
        'status': 'MEASURED_CTR_CONSUMED' if ctr else 'AWAITING_PUBLICATION_AND_QUALIFIED_GSC_DATA',
        'ctr_test': ctr, 'proposals': proposals,
        'snapshot_sha256': hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest(),
        'producer': 'improvement_loops.build_loop_b_snapshot', 'consumer': 'improvement_loops.loop_c',
        'registration': 'NO_SEPARATE_REGISTRY; run-db automatically discovers every observed GSC route',
        'activation': 'Published route; measured GSC rows; >=50 impressions; fresh date; position-matched observed peer; run-db policy/feedback gates satisfied',
        'effects': {'title_changes': 0, 'publishes': 0, 'sends': 0},
    }


def measured_ctr_tests(rows, as_of, canonicalize):
    daily = {}
    for row in rows:
        try:
            date = dt.date.fromisoformat(str(row['metric_date']))
            age = (as_of - date).days
            route = canonicalize(str(row['page_path']))
            clicks = float(row['gsc_clicks'])
            impressions = float(row['gsc_impressions'])
            position = float(row['gsc_position'])
        except (KeyError, TypeError, ValueError):
            continue
        values = (clicks, impressions, position)
        if not all(math.isfinite(x) for x in values):
            continue
        if not (0 <= age < WINDOW_DAYS and 0 <= clicks <= impressions and impressions > 0 and position >= 1):
            continue
        key = (date.isoformat(), route)
        previous = daily.get(key)
        if previous is None or impressions > previous[1]:
            daily[key] = (clicks, impressions, position)
    totals = {}
    for (date, route), (clicks, impressions, position) in daily.items():
        item = totals.setdefault(route, [0.0, 0.0, 0.0, date])
        item[0] += clicks
        item[1] += impressions
        item[2] += position * impressions
        item[3] = max(item[3], date)
    eligible = {route: value for route, value in totals.items()
                if value[1] >= MIN_IMPRESSIONS
                and (as_of - dt.date.fromisoformat(value[3])).days <= 3}
    result = {}
    for route, (clicks, impressions, weighted_position, date) in eligible.items():
        position = weighted_position / impressions
        peers = {key: value for key, value in eligible.items() if key != route
                 and abs(value[2] / value[1] - position) <= POSITION_TOLERANCE}
        if not peers:
            continue
        peer_clicks = sum(value[0] for value in peers.values())
        peer_impressions = sum(value[1] for value in peers.values())
        binding = {'route': route, 'as_of': as_of.isoformat(), 'daily': sorted(
            (date, path, value) for (date, path), value in daily.items()
            if path == route or path in peers)}
        result[route] = {
            'actual_ctr': clicks / impressions,
            'expected_ctr': peer_clicks / peer_impressions,
            'position': position,
            'impressions': impressions,
            'measurement_state': 'MEASURED_GSC_POSITION_MATCHED_COHORT',
            'expected_ctr_method': 'other observed routes within 1.0 position; impression-weighted',
            'window_days': WINDOW_DAYS,
            'minimum_impressions': MIN_IMPRESSIONS,
            'source_digest': hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest(),
            'effects': {'title_changes': 0, 'publishes': 0},
        }
    return result
