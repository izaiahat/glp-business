"""Fixed shared bridge cohort in disposable maintained windows; no live effects."""
import copy
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from test_glp333_reporting_generation import ROOT, blob, generation_fixture, put, sha, payloads
import glp44_scoped_adoption as pair
import maintenance_inventory as inventory
import maintenance_window as mw
import sync_from_repo as syncer
from glp_ship_registry import REPORTING_PATHS as SHIP_REPORTING_PATHS

GENERATION = 'glp337-shared-bridge-20261001'
SOURCE = '898b45e903dffcce4046aa5548fc3e6f23b60b78'
PATHS = ('scripts/cron_bridge_action.py', 'scripts/glp_analytics_brain_cadence.py')
# Later reporting publication members also enter the shared inventory; the
# GLP337 generation's own PATHS, transaction delta and hashes remain unchanged.
REPORTING_INVENTORY_SUCCESSOR = (
    'scripts/affiliate_network_access.py', 'scripts/marketplace_adapters/katalys.py',
    'scripts/affiliate_reporting_admitad.py', 'scripts/affiliate_reporting_clickbank.py',
    'scripts/affiliate_reporting_everflow.py', 'scripts/affiliate_reporting_healthtrader.py',
    'scripts/affiliate_reporting_partnerstack.py', 'scripts/affiliate_reporting_refersion.py',
)
BASELINE = '4e8da708900178d32b2f165e1d18067750b45dabb5a115d3487b4d1a9d2d6bcb'
BEFORE = {PATHS[0]: 'f00f66759a9bdc3a0f3a4156c5fbe8a6acc73597f5b83732e818b9cc7dc1ec55',
          PATHS[1]: '61ca597689080f6f3e5de9b00c733009df258850f58bce7f592843e527f17c53'}
AFTER = {PATHS[0]: 'f1055dea298e8354bd61d9ccad084a2b33ab9c028ed7070b54e23d64ad023032',
         PATHS[1]: '4d51a627d7a5208238ab7ce7622772c118d9b141497942bf9ed8bd6d4587bcb2'}
CONTEXT = ('ops/scheduled-source-drift-manifest.json', 'ops/n8n-bridge/actions.json',
           'ops/n8n-bridge/bridge.py')


def fixture(tmp_path, monkeypatch, *, cadence_baseline=False):
    enter = mw.MaintenanceWindow.enter
    def enter_with_predecessors(window):
        root = window.backend.root
        original = blob('e2fa6efc6070708132b576e57182f648af0b0038', PATHS[0])
        assert sha(original) == BASELINE
        resident = original.replace(b'"gpt-6-sol"', b'"gpt-6.1-sol"')
        assert sha(resident) == BEFORE[PATHS[0]]
        put(root / PATHS[0], resident, 0o644)
        cadence = blob('84427444a4c84ff66380506d29fb9e0e1fc7d8aa', PATHS[1])
        assert sha(cadence) == BEFORE[PATHS[1]]
        put(root / PATHS[1], cadence, 0o644)
        put(root / CONTEXT[0], blob('375491444eca0dd5f159eaa09f5379a055e12e3d', CONTEXT[0]))
        put(root / CONTEXT[2], blob(SOURCE, CONTEXT[2]))
        state = json.loads((root / syncer.STATE).read_text())
        state['hashes'][PATHS[0]] = BASELINE
        if cadence_baseline:
            state['hashes'][PATHS[1]] = BEFORE[PATHS[1]]
        else:
            state['hashes'].pop(PATHS[1], None)
        put(root / syncer.STATE, (json.dumps(state) + '\n').encode())
        return enter(window)
    monkeypatch.setattr(mw.MaintenanceWindow, 'enter', enter_with_predecessors)
    return generation_fixture(tmp_path, monkeypatch, pr71_predecessor=True)


def preview(f):
    with mw.require_active_window(f.journal, f.root, f.hermes) as active:
        return pair.plan(ROOT, f.root, f.hermes, window=active, generation=GENERATION)[0]


def acceptance(proposal):
    # Disposable fixture labels are not independent acceptance or live authority.
    return {**copy.deepcopy(proposal), 'schema': 'glp44-scoped-adoption-review-v1',
            'decision': 'ACCEPT_EXACT_BYTES', 'prepared_by': 'fixture preparer',
            'reviewer': 'fixture independent source reviewer',
            'runtime_review': {'schema': 'glp337-shared-bridge-runtime-review-v1',
                              'decision': 'ACCEPT_EXACT_BYTES', 'reviewer': 'fixture receiver',
                              'proposal_sha256': sha(json.dumps(proposal, sort_keys=True).encode())}}


def apply(f, review):
    return pair.adopt(ROOT, f.root, f.hermes, review, scheduler=f.scheduler,
                      window=f.journal, generation=GENERATION)


def test_fixed_shared_bridge_generation_journals_only_pair_state_and_restores(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    try:
        before = payloads(f)
        old_state = json.loads((f.root / syncer.STATE).read_bytes())
        prior_controls = copy.deepcopy(mw.read_journal(f.journal)['entries'])
        with mw.require_active_window(f.journal, f.root, f.hermes) as active:
            premature = pair.plan(ROOT, f.root, f.hermes, window=active)[0]
            original_job = active.original_job(pair.JOB)
        proposal = preview(f)  # RED on main: unknown_reporting_generation.
        assert proposal['approved_source'] == SOURCE
        assert proposal['source_before_sha256'] == BEFORE
        assert proposal['source_sha256'] == proposal['state_delta'] == AFTER
        assert proposal['adopted_baselines'] == [PATHS[1]]
        assert set(proposal['retained_context']) == set(CONTEXT)
        assert payloads(f) == before and not f.saves
        old_dir = tmp_path / 'old-inventory'
        for name in ('maintenance_inventory.py', 'glp44_scoped_adoption.py',
                     'glp73_paired_adoption.py', 'glp331_carrier_adoption.py'):
            put(old_dir / name, blob('afd40c64005a52cd14dd82d59aa46f265ba3265a', 'scripts/' + name))
        put(old_dir / 'glp_ship_registry.py', b'# No historical ship declarations.\n')
        # Hold unrelated F08 paths constant in this historical bridge comparison.
        put(old_dir / 'glp345_a4_adoption.py', (ROOT / 'scripts/glp345_a4_adoption.py').read_bytes())
        with monkeypatch.context() as m:
            m.setattr(inventory, '__file__', str(old_dir / 'maintenance_inventory.py'))
            old_paths = inventory.affected_paths(f.root, f.hermes)
        assert inventory.affected_paths(f.root, f.hermes) - old_paths == (
            {f.root / rel for rel in (*PATHS, *REPORTING_INVENTORY_SUCCESSOR)}
            | ({f.root / rel for rel in SHIP_REPORTING_PATHS} - old_paths))
        with pytest.raises(RuntimeError, match='active_maintenance_window_required'):
            pair.plan(ROOT, f.root, f.hermes, generation=GENERATION)
        with mw.require_active_window(f.journal, f.root, f.hermes) as active:
            with pytest.raises(mw.WindowError, match='outside_adoption_scope'):
                path = str(f.root / 'scripts/unselected.py')
                active.accept_file_updates({path: sha(b'not selected\n')}, {path: sha(b'other\n')})
        review = acceptance(proposal)
        seen = []
        fire_lock = f.scheduler._fire_job_lock
        @contextmanager
        def tracked_fire(jid):
            with fire_lock(jid) as acquired:
                seen.append(jid)
                yield acquired
        with monkeypatch.context() as m:
            m.setattr(f.scheduler, '_fire_job_lock', tracked_fire)
            result = apply(f, review)
        assert seen == sorted(row['entry']['id'] for row in prior_controls if row['entry']['kind'] == 'hermes')
        assert result['applied'] and result['source'] == SOURCE and not f.saves
        expected = copy.deepcopy(old_state); expected['hashes'].update(AFTER)
        assert json.loads((f.root / syncer.STATE).read_bytes()) == expected
        transaction = mw.read_journal(f.journal)['source_transaction']
        assert transaction['outcome'] == 'applied' and transaction['switch_verified']
        assert transaction['job_ids'] == []
        assert set(transaction['after']) == {str(f.root / rel) for rel in (*PATHS, syncer.STATE)}
        for rel in PATHS:
            assert (f.root / rel).read_bytes() == blob(SOURCE, rel)
            assert (f.root / rel).stat().st_mode & 0o777 == 0o644
            assert (Path(result['custody']) / (BEFORE[rel] + '.bytes')).read_bytes() == before[f.root / rel][0]
        for path, value in before.items():
            if path not in {f.root / rel for rel in (*PATHS, syncer.STATE)}:
                assert (path.read_bytes(), path.stat().st_mode & 0o777) == value
        assert f.window.status()['verified']
        landed = payloads(f)
        with pytest.raises(RuntimeError):
            apply(f, review)
        assert payloads(f) == landed and not f.saves
        with mw.require_active_window(f.journal, f.root, f.hermes) as active:
            with pytest.raises(RuntimeError, match='reviewed_preimage_or_candidate_moved:state_before_sha256'):
                pair.plan(ROOT, f.root, f.hermes, premature, window=active)
        good = mw.read_journal(f.journal)
        bad = copy.deepcopy(good); bad['source_transaction']['outcome'] = 'uncertain'
        assert not f.window._verify(bad)
        put(f.root / PATHS[0], *before[f.root / PATHS[0]])
        assert not f.window._verify(good)
        put(f.root / PATHS[0], blob(SOURCE, PATHS[0]), 0o644)
        assert f.window._verify(good)
        f.window.exit()
        closed = mw.read_journal(f.journal)
        assert closed['state'] == 'closed'
        assert closed['entries'] == [{**row, 'restore_intent': True, 'restored': True} for row in prior_controls]
        assert f.scheduler.load_jobs()[0] == original_job
        assert f.scheduler.load_jobs()[1] == json.loads(before[f.registry][0])['jobs'][1]
        assert {rel: sha((f.root / rel).read_bytes()) for rel in PATHS} == AFTER
    finally:
        f.window.release_locks()


def test_shared_bridge_literal_inventory_adds_exactly_the_pair(tmp_path, monkeypatch):
    import maintenance_inventory as inventory
    root, hermes = tmp_path / 'root', tmp_path / 'hermes'
    root.mkdir(); hermes.mkdir()
    current = inventory.affected_paths(root, hermes)
    controls = tmp_path / 'controls'
    for filename in ('glp44_scoped_adoption.py', 'glp73_paired_adoption.py', 'glp331_carrier_adoption.py'):
        put(controls / filename, blob('afd40c64005a52cd14dd82d59aa46f265ba3265a', 'scripts/' + filename))
    put(controls / 'glp_ship_registry.py', b'# No historical ship declarations.\n')
    # Hold unrelated F08 paths constant; retain the exact original delta oracle.
    put(controls / 'glp345_a4_adoption.py', (ROOT / 'scripts/glp345_a4_adoption.py').read_bytes())
    monkeypatch.setattr(inventory, '__file__', str(controls / 'maintenance_inventory.py'))
    previous = inventory.affected_paths(root, hermes)
    assert current - previous == ({root / rel for rel in (*PATHS, *REPORTING_INVENTORY_SUCCESSOR)}
                                  | ({root / rel for rel in SHIP_REPORTING_PATHS} - previous))
    assert previous - current == set()
    prior = (controls / 'glp44_scoped_adoption.py').read_bytes()
    put(controls / 'glp44_scoped_adoption.py', prior + b'\nGLP337_PATHS = tuple(GLP337_HASHES)\n')
    with pytest.raises(ValueError):
        inventory.affected_paths(root, hermes)


def test_shared_bridge_authentic_tracked_cadence_baseline_acceptance(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch, cadence_baseline=True)
    try:
        proposal = preview(f)
        assert proposal['adopted_baselines'] == []
        old_state = json.loads((f.root / syncer.STATE).read_bytes())
        result = apply(f, acceptance(proposal))
        assert result['applied'] and not f.saves
        old_state['hashes'].update(AFTER)
        assert json.loads((f.root / syncer.STATE).read_bytes()) == old_state
        assert f.window._verify(mw.read_journal(f.journal))
        f.window.exit()
        assert mw.read_journal(f.journal)['state'] == 'closed'
    finally:
        f.window.release_locks()


@pytest.mark.parametrize('mutation', [
    'self_review', 'runtime_self_review', 'runtime_hash', 'runtime_schema',
    'extra_cohort', 'source', 'source_mode', 'baseline', 'state', 'state_mode',
    'registry', 'context', 'context_mode', 'manifest', 'missing_review',
])
def test_shared_bridge_exact_review_cas_refusals_before_payload_writes(tmp_path, monkeypatch, mutation):
    f = fixture(tmp_path, monkeypatch)
    try:
        review = acceptance(preview(f))
        if mutation == 'self_review':
            review['reviewer'] = review['prepared_by']
        elif mutation == 'runtime_self_review':
            review['runtime_review']['reviewer'] = review['prepared_by']
        elif mutation == 'runtime_hash':
            review['runtime_review']['proposal_sha256'] = '0' * 64
        elif mutation == 'runtime_schema':
            review['runtime_review']['schema'] = 'glp331-dependency-runtime-review-v1'
        elif mutation == 'extra_cohort':
            review['source_sha256']['scripts/unapproved.py'] = '0' * 64
        elif mutation in ('source', 'source_mode'):
            path = f.root / PATHS[0]
            path.chmod(0o600) if mutation == 'source_mode' else put(path, path.read_bytes() + b'\n')
        elif mutation in ('baseline', 'state', 'state_mode'):
            path = f.root / syncer.STATE
            if mutation == 'state_mode':
                path.chmod(0o640)
            else:
                data = json.loads(path.read_bytes())
                if mutation == 'baseline':
                    data['hashes'][PATHS[0]] = BEFORE[PATHS[0]]
                else:
                    data['unrelated_field'] = 'drift'
                put(path, json.dumps(data).encode())
        elif mutation == 'registry':
            data = json.loads(f.registry.read_bytes())
            data['jobs'][1]['unrelated_field'] = 'drift'
            put(f.registry, json.dumps(data).encode())
        elif mutation in ('context', 'context_mode'):
            path = f.root / CONTEXT[0]
            path.chmod(0o640) if mutation == 'context_mode' else put(path, path.read_bytes() + b'\n')
        elif mutation == 'manifest':
            path = f.root / syncer.MANIFEST
            put(path, path.read_bytes() + b'\n')
        else:
            review = None
        before = payloads(f)
        with pytest.raises((RuntimeError, ValueError, mw.WindowError)):
            apply(f, review)
        assert payloads(f) == before and not f.saves
    finally:
        f.window.release_locks()


def test_shared_bridge_candidate_private_mutation_refuses_before_switch(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    try:
        review = acceptance(preview(f))
        before = payloads(f)
        original_plan = pair._plan_dependencies
        calls = 0
        def mutate_after_locked_preflight(checkout, *args, **kwargs):
            nonlocal calls
            result = original_plan(checkout, *args, **kwargs)
            calls += 1
            if calls == 2:
                put(checkout / PATHS[1], b'poisoned private candidate\n')
            return result
        with monkeypatch.context() as context:
            context.setattr(pair, '_plan_dependencies', mutate_after_locked_preflight)
            with pytest.raises(RuntimeError, match='reviewed_candidate_moved_before_switch'):
                apply(f, review)
        assert payloads(f) == before and not f.saves
    finally:
        f.window.release_locks()


def test_shared_bridge_lost_source_ack_preserves_uncertainty_custody_and_no_replay(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    try:
        review = acceptance(preview(f))
        import maintenance_cohort as cohort
        original = cohort.switch_files
        def lost_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('injected shared bridge source acknowledgement loss')
        with monkeypatch.context() as context:
            context.setattr(cohort, 'switch_files', lost_ack)
            with pytest.raises(mw.WindowError, match='adoption_outcome_uncertain_reconcile_before_exit') as error:
                apply(f, review)
            assert isinstance(error.value.__context__, OSError)
            assert 'acknowledgement loss' in str(error.value.__context__)
        assert not f.saves
        journal = mw.read_journal(f.journal)
        assert journal['state'] == 'active'
        assert journal['source_transaction']['outcome'] == 'uncertain'
        switch = journal['source_switch']
        assert switch['state'] == 'published' and not switch.get('commit_started')
        before = payloads(f)
        assert {rel: sha((f.root / rel).read_bytes()) for rel in PATHS} == AFTER
        for item in switch['files'].values():
            assert Path(item['original']).is_file()
            assert Path(item['candidate']).is_file()
        custody = f.root / 'reports/ops/pin-archive/glp44' / sha(json.dumps(review, sort_keys=True).encode())
        assert (custody / (BEFORE[PATHS[0]] + '.bytes')).is_file()
        assert (custody / (AFTER[PATHS[1]] + '.bytes')).is_file()
        with pytest.raises(mw.WindowError):
            apply(f, review)
        assert payloads(f) == before and not f.saves
        # Exit needs independent exact-outcome review; no automatic retry/repair.
        with pytest.raises(mw.WindowError):
            f.window.exit()
        outcome = f.window.outcome_proposal(mw.read_journal(f.journal))
        assert outcome['side'] == 'after'
        f.window.exit({'schema': 'glp-maintenance-outcome-review-v1',
                       'decision': 'ACCEPT_EXACT_OUTCOME',
                       'prepared_by': 'fixture-recovery-preparer',
                       'reviewer': 'fixture-independent-outcome-reviewer',
                       'proposal': outcome})
        assert mw.read_journal(f.journal)['state'] == 'closed'
        assert {rel: sha((f.root / rel).read_bytes()) for rel in PATHS} == AFTER
    finally:
        f.window.release_locks()
