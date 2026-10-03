"""Coherent PR115-after-PR165 adoption; no live writes or provider calls."""
import ast
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp345_a4_adoption as a4
from test_glp345_a4_adoption import isolated, put
AUTHENTICATE = a4.authenticate


@pytest.mark.parametrize('outcome', ['ok', 'lost_ack', 'bad_readback'])
def test_weekly_exact_retained_cohort_and_native_switch(isolated, tmp_path, monkeypatch, outcome):
    """Host-only exact retained inputs; disposable store, never live acceptance."""
    import os
    retained = os.environ.get('GLP341_RETAINED_ROOT')
    if not retained:
        pytest.skip('exact uncurated retained prerequisites require explicit read-only host root')
    from test_glp345_a4_adoption import test_guarded_switch_native_pin_and_no_replay
    root, home, window, target = isolated
    before, wrapper = a4.stage_pins((ROOT / a4.MAP_SNAPSHOT).read_bytes(),
                                   (ROOT / a4.WRAPPER_SNAPSHOT).read_bytes())
    emitted, _ = a4.stage_weekly_pins(before, wrapper)
    requirements = dict(a4.wrapper_pins(wrapper))
    requirements.update({x['path']: x['sha256'] for x in json.loads(emitted).values()})
    requirements[str(a4.BUSINESS / a4.A3)] = a4.A3_HASH
    for absolute in requirements:
        relative = Path(absolute).relative_to(a4.BUSINESS)
        put(root / relative, (Path(retained) / relative).read_bytes())
    put(root / a4.MAP, before)
    put(home / 'scripts' / a4.WRAPPER, wrapper)
    put(root / 'ops/.rebuild-sync-state.json', json.dumps({'schema': 1, 'hashes': {
        a4.MONITOR: a4.AFTER, 'untouched': 'a'*64}}).encode())
    registry = home / 'cron/jobs.json'
    doc = json.loads(registry.read_bytes())
    doc['jobs'][0]['script_sha256'] = a4.WEEKLY_WRAPPER_BEFORE
    put(registry, json.dumps(doc).encode())
    monkeypatch.setattr(a4, 'authenticate', AUTHENTICATE)
    real_plan = a4.plan
    monkeypatch.setattr(a4, 'plan', lambda *args, **kw: real_plan(*args, **(kw | {
        'generation': a4.WEEKLY_GENERATION})))
    test_guarded_switch_native_pin_and_no_replay((root, home, window, doc['jobs'][0]),
                                                tmp_path, monkeypatch, outcome)


def test_weekly_scope_is_in_real_maintained_inventory(tmp_path):
    import maintenance_inventory as inv
    root, home = tmp_path / 'business', tmp_path / 'hermes'
    paths = inv.affected_paths(root, home)
    assert root / 'reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_ads_loop.py' in paths


def test_weekly_plan_refuses_unresolved_dependencies_before_writes(isolated, monkeypatch):
    root, home, window, target = isolated
    before, wrapper = a4.stage_pins((ROOT / a4.MAP_SNAPSHOT).read_bytes(),
                                   (ROOT / a4.WRAPPER_SNAPSHOT).read_bytes())
    put(root / a4.MAP, before)
    put(home / 'scripts' / a4.WRAPPER, wrapper)
    put(root / 'reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_ads_loop.py',
        (ROOT / 'ops/glp345-a4/weekly.predecessor.py').read_bytes())
    put(root / 'reports/ops/orchestration-restore-20260907/PLAN/A3/RESULT.json',
        (ROOT / 'tests/fixtures/glp341-a3-result.json').read_bytes())
    registry = home / 'cron/jobs.json'
    doc = json.loads(registry.read_bytes())
    doc['jobs'][0]['script_sha256'] = a4.sha(wrapper)
    put(registry, json.dumps(doc).encode())
    monkeypatch.setattr(a4, 'authenticate', lambda checkout, **kw:
                        ('f'*40, (ROOT / 'reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_ads_loop.py').read_bytes()))
    baseline = {p: p.read_bytes() for r in (root, home) for p in r.rglob('*') if p.is_file()}
    # Explicit stand-in prerequisites from the legacy fixture cannot be blessed.
    with pytest.raises(RuntimeError, match='weekly_dependency_drift'):
        a4.plan(ROOT, root, home, 'fixture author', window=window,
                generation='glp341-weekly-pr115-after-pr165-20261002')
    assert all(p.read_bytes() == raw for p, raw in baseline.items())


def test_reviewed_weekly_successor_closes_three_incompatibilities():
    # Genuine PR165 outputs, reconstructed from its authenticated predecessors.
    before, wrapper = a4.stage_pins((ROOT / a4.MAP_SNAPSHOT).read_bytes(),
                                   (ROOT / a4.WRAPPER_SNAPSHOT).read_bytes())
    assert callable(getattr(a4, 'stage_weekly_pins', None)), 'missing coherent PR115-after-PR165 route'
    emitted, carrier = a4.stage_weekly_pins(before, wrapper)
    original, candidate = json.loads(before), json.loads(emitted)
    assert set(candidate) == set(original)
    for key in set(original) - {'posthog'}:
        assert candidate[key] == original[key]  # PR161 must not be rolled back.
    assert candidate['posthog']['sha256'] == a4.sha((ROOT / 'ops/glp/seo_data_rails.py').read_bytes())
    pins = a4.wrapper_pins(carrier)
    old = a4.wrapper_pins(wrapper)
    assert not any('result_contract' in p or 'briefs/' in p for p in pins)
    assert pins[str(a4.BUSINESS / a4.WEEKLY)] == a4.sha((ROOT / a4.WEEKLY).read_bytes())
    assert pins[str(a4.BUSINESS / a4.MAP)] == a4.sha(emitted)
    assert pins[str(a4.BUSINESS / a4.A3)] == a4.sha((ROOT / 'tests/fixtures/glp341-a3-result.json').read_bytes())
    entry = str(a4.BUSINESS / 'reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_cron_entry.py')
    assert pins[entry] == old[entry]
    # All code/declarations outside the PINS assignment remain exact live bytes.
    def strip_pins(body):
        node = next(n for n in ast.parse(body).body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'PINS' for t in n.targets))
        rows = body.splitlines(keepends=True)
        return b''.join(rows[:node.lineno-1] + rows[node.end_lineno:])
    assert strip_pins(carrier) == strip_pins(wrapper)
    for m, w in ((before+b'\n', wrapper), (before, wrapper+b'\n')):
        with pytest.raises(RuntimeError, match='preimage'):
            a4.stage_weekly_pins(m, w)
