#!/usr/bin/env python3
"""Populate ops/linear/pins.json: name -> UUID pins for team, states, projects, labels.

Read-only against Linear (queries only). The pin file is an optimization and a
rename guard, never an authority: linear_client.select_one honours a pin only
when it resolves to exactly one row in the freshly fetched connection, and falls
back to the live name match otherwise. Re-run after any Linear rename.

  python3 scripts/linear_pins_refresh.py [--team GLP] [--out ops/linear/pins.json] [--print]
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from linear_client import Client, LinearError, ROOT, PINS_PATH, save_journal
from id_grammar import assert_linear_uuid


def collect(client, team_key='GLP'):
    team = client.team(team_key)
    assert_linear_uuid(team['id'], field='team')

    def bucket(rows, label):
        pins, seen = {}, {}
        for row in rows:
            name, identity = row.get('name'), row.get('id')
            if not isinstance(name, str) or not name or not isinstance(identity, str):
                continue
            assert_linear_uuid(identity, field=label + '.' + name[:40])
            seen[name] = seen.get(name, 0) + 1
            pins[name] = identity
        # A duplicated display name cannot be pinned; leave it to the live lookup
        # to fail loudly rather than pin one of two rows behind the caller's back.
        return {name: value for name, value in pins.items() if seen[name] == 1}

    labels = [r for r in client.connection('labels')
              if not r.get('team') or r['team']['id'] == team['id']]
    return {'schema': 'linear-pins-v1',
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'team_key': team_key,
            'note': 'Advisory UUID pins; select_one falls back to the live name match.',
            'teams': {team['key']: team['id'], team['name']: team['id']} if team.get('name') else {team['key']: team['id']},
            'states': bucket(client.connection('states', {'id': team['id']}), 'states'),
            'projects': bucket(client.connection('projects'), 'projects'),
            'labels': bucket(labels, 'labels')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--team', default='GLP')
    parser.add_argument('--out', type=Path, default=PINS_PATH)
    parser.add_argument('--print', dest='show', action='store_true', help='Print the pins instead of only the summary')
    args = parser.parse_args(argv)
    pins = collect(Client(), args.team)
    save_journal(args.out, pins)
    summary = {'written': str(Path(args.out).resolve()),
               'counts': {k: len(pins[k]) for k in ('teams', 'states', 'projects', 'labels')}}
    print(json.dumps(pins if args.show else summary, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (LinearError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
