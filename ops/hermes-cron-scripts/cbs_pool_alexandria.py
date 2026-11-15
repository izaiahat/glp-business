"""Due-event Alexandria execution, retained once per event before model startup."""
import json
from datetime import datetime, timezone
from pathlib import Path


def requests_for(data, event):
    options = {'season': int(data['season']), 'season_type': 'REG', 'week': int(data['week'])}
    calls = [('schedule', 'nfl-com', 'sports-league-data/schedule', options)]
    if not event['kind'].startswith('recap'):
        calls += [
            ('official-injuries', 'nfl-com', 'sports-league-data/injury_report', options),
            ('startwho-injuries', 'startwho-com', 'fantasy-sports-rankings/injury_report', {}),
            ('qb-projections', 'startwho-com', 'fantasy-sports-rankings/projections',
             {'position': 'QB', 'week': int(data['week']), 'limit': 40}),
        ]
    return calls


def validate_response(payload, provider, capability, data):
    if payload.get('success') is not True:
        raise ValueError('Firecrawl envelope did not succeed')
    entries = payload.get('data', {}).get('alexandria', [])
    matches = [e for e in entries if e.get('provider') == provider and e.get('capability') == capability]
    if len(matches) != 1:
        raise ValueError('Alexandria capability receipt missing or ambiguous')
    entry = matches[0]
    if entry.get('error') or entry.get('success') is False or not isinstance(entry.get('data'), dict):
        raise ValueError('Alexandria capability error or missing data')
    body = entry['data']
    if body.get('week') is not None and int(body['week']) != int(data['week']):
        raise ValueError('Alexandria week mismatch')
    if body.get('season') is not None and int(body['season']) != int(data['season']):
        raise ValueError('Alexandria season mismatch')
    if capability.endswith('/schedule'):
        norm = lambda team: {'AZ': 'ARI', 'JAC': 'JAX'}.get(team, team)
        games = body.get('games', [])
        expected = {frozenset(norm(t) for t in g['teams']) for g in data['schedule_games']}
        actual = [frozenset(norm(g[k]['abbreviation']) for k in ('away_team', 'home_team')) for g in games]
        if len(actual) != len(expected) or len(set(actual)) != len(actual) or set(actual) != expected:
            raise ValueError('Alexandria schedule does not match complete CBS slate')
    rows = next((body[k] for k in ('games', 'reports', 'injuries', 'projections', 'players') if isinstance(body.get(k), list)), None)
    if rows is None or (capability.endswith('/projections') and not rows):
        raise ValueError('Alexandria data rows missing')
    return {'status': 'executed', 'count': len(rows), 'creditsCost': entry.get('creditsCost'),
            'requestId': entry.get('requestId'), 'response_id': payload.get('scrape_id', payload.get('id')),
            'alexandria_id': entry.get('alexandriaId'), 'source_url': body.get('source_url'),
            'source_update': next((body[k] for k in ('updated_at', 'report_date', 'last_updated', 'updated_at_ms')
                                   if body.get(k) is not None), None)}


def collect(data, event, directory, client):
    """No automatic ambiguous replay: the per-call intent exists before transport."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    calls = requests_for(data, event)
    receipts = []
    for name, provider, capability, options in calls:
        receipt_path = directory / (name + '.receipt.json')
        raw_path = directory / (name + '.json')
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text())
            # Existing intent/error is not permission to repeat a possibly billed effect.
            if receipt.get('event_key') != event['key']:
                raise ValueError('Alexandria receipt belongs to another event')
            receipts.append(receipt)
            continue
        receipt = {'event_key': event['key'], 'provider': provider, 'capability': capability,
                   'options': options, 'observed_at': datetime.now(timezone.utc).isoformat(),
                   'status': 'unknown', 'raw_path': str(raw_path)}
        receipt_path.write_text(json.dumps(receipt, indent=2))
        try:
            # Free current-contract discovery is required before each intended execution.
            contract = client.request('scrape', {'alexandria': [{'provider': 'firecrawl',
                'capability': 'find-tools', 'options': {'providers': [provider], 'capabilities': [capability],
                    'level': 'tools', 'expand': ['options'], 'limit': 20}}]}, timeout=30)
            (directory / (name + '.contract.json')).write_text(json.dumps(contract, indent=2))
            items = [item for entry in contract.get('data', {}).get('alexandria', [])
                     if not entry.get('error') for item in entry.get('data', {}).get('items', [])]
            selected = [i for i in items if i.get('provider') == provider and i.get('capability') == capability]
            if contract.get('success') is not True or len(selected) != 1 or selected[0].get('creditsCost') != 5:
                raise ValueError('Alexandria discovery missing or approved five-credit price changed')
            payload = client.request('scrape', {'alexandria': [
                {'provider': provider, 'capability': capability, 'options': options}]}, timeout=45)
            raw_path.write_text(json.dumps(payload, indent=2))
            receipt.update(validate_response(payload, provider, capability, data))
        except Exception as exc:
            receipt.update(status='error_or_unknown', error_class=type(exc).__name__,
                           instruction='Do not replay automatically. Inspect retained response/operation ledger; use official web fallback and disclose this gap.')
        receipt_path.write_text(json.dumps(receipt, indent=2))
        receipts.append(receipt)
    manifest = {'event_key': event['key'], 'status': 'executed' if all(r['status'] == 'executed' for r in receipts) else 'incomplete',
                'calls': receipts, 'freshness': 'Inspect per-team source dates; retrieval is not source freshness. Official updates/inactives supersede older rows.'}
    (directory / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    return manifest
