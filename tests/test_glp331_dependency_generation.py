"""Source-only dependency switch in a disposable window; no live effects."""
import copy
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from test_glp333_reporting_generation import (
    ROOT, blob, generation_fixture, put, sha, payloads,
)
import glp44_scoped_adoption as pair
import maintenance_inventory as inventory
import maintenance_window as mw
import sync_from_repo as syncer

GENERATION = 'glp331-dependencies-20261001'
SOURCE = '0b61c25a7177896369f4fa76b4e84192de6bee70'
PATHS = ('scripts/glp_affiliate_wire_backlog_worker.py',
         'scripts/glp_go_runtime_manifest_refresh.mjs')
BEFORE = {'scripts/glp_affiliate_wire_backlog_worker.py':
          'b313063f54848dbf4e7e8ff8b4dbf2bf697ff1ccb7245837d950b61c3ed255a0',
          'scripts/glp_go_runtime_manifest_refresh.mjs':
          '35f99538dd648fe21b781766a0a81952ad222265e0aec552b42dda5563eaccbe'}
AFTER = {'scripts/glp_affiliate_wire_backlog_worker.py':
         '59e25a8c605f6c0c2ae284edfb3dae99eb7354c2679146fb787a923a4544ef05',
         'scripts/glp_go_runtime_manifest_refresh.mjs':
         'a2309d7e02b80f204b40011f0009cb7fcabaf046903dd09cfab085ccecd45e1e'}


def test_dependency_generation_journals_exact_cohort_and_restores_window(tmp_path, monkeypatch):
    enter = mw.MaintenanceWindow.enter
    def enter_with_predecessors(window):
        root = window.backend.root
        # Authenticate real preimages before the actual inventory/snapshot.
        # Refresher intentionally has no baseline, exercising Git provenance.
        for rel in PATHS:
            body = blob('8f81f3665a0eff5339779444fc73dd9262911057', rel)
            assert sha(body) == BEFORE[rel]
            put(root / rel, body, 0o644)
        state = json.loads((root / syncer.STATE).read_text())
        state['hashes'][PATHS[0]] = BEFORE[PATHS[0]]
        state['hashes'].pop(PATHS[1], None)
        put(root / syncer.STATE, (json.dumps(state) + '\n').encode())
        return enter(window)
    monkeypatch.setattr(mw.MaintenanceWindow, 'enter', enter_with_predecessors)
    f = generation_fixture(tmp_path, monkeypatch, pr71_predecessor=True)
    try:
        state = json.loads((f.root / syncer.STATE).read_text())
        data = mw.read_journal(f.journal)
        before = payloads(f)
        old_jobs = f.registry.read_bytes()
        prior_controls = copy.deepcopy(data['entries'])
        old_inventory = blob('d6c193eeff4489c2605fded809cb54e4ba09b22d', 'scripts/maintenance_inventory.py')
        old_dir = tmp_path / 'old-inventory'
        put(old_dir / 'maintenance_inventory.py', old_inventory)
        for name in ('glp44_scoped_adoption.py', 'glp73_paired_adoption.py'):
            put(old_dir / name, blob('d6c193eeff4489c2605fded809cb54e4ba09b22d', 'scripts/' + name))
        put(old_dir / 'glp331_carrier_adoption.py', (ROOT / 'scripts/glp331_carrier_adoption.py').read_bytes())
        with monkeypatch.context() as m:
            m.setattr(inventory, '__file__', str(old_dir / 'maintenance_inventory.py'))
            old_paths = inventory.affected_paths(f.root, f.hermes)
        with mw.require_active_window(f.journal, f.root, f.hermes) as active:
            proposal = pair.plan(ROOT, f.root, f.hermes, window=active, generation=GENERATION)[0]
        shared_bridge = ('scripts/cron_bridge_action.py', 'scripts/glp_analytics_brain_cadence.py')
        reporting_leaves = (
            'scripts/affiliate_network_access.py', 'scripts/marketplace_adapters/katalys.py',
            'scripts/affiliate_reporting_admitad.py', 'scripts/affiliate_reporting_clickbank.py',
            'scripts/affiliate_reporting_everflow.py', 'scripts/affiliate_reporting_healthtrader.py',
            'scripts/affiliate_reporting_partnerstack.py', 'scripts/affiliate_reporting_refersion.py',
        )
        assert inventory.affected_paths(f.root, f.hermes) - old_paths == {f.root / rel for rel in (*PATHS, *shared_bridge, *reporting_leaves)}
        assert proposal['source_sha256'] == AFTER
        assert proposal['source_before_sha256'] == BEFORE
        assert proposal['state_delta'] == AFTER
        with mw.require_active_window(f.journal, f.root, f.hermes) as active:
            premature_pr71 = pair.plan(ROOT, f.root, f.hermes, window=active)[0]
        review = {**copy.deepcopy(proposal), 'schema': 'glp44-scoped-adoption-review-v1',
                  'approved_source': SOURCE, 'decision': 'ACCEPT_EXACT_BYTES',
                  'prepared_by': 'fixture preparer', 'reviewer': 'fixture independent reviewer',
                  'runtime_review': {'schema': 'glp331-dependency-runtime-review-v1',
                                     'decision': 'ACCEPT_EXACT_BYTES', 'reviewer': 'fixture receiver',
                                     'proposal_sha256': sha(json.dumps(proposal, sort_keys=True).encode())}}
        def apply(receipt=review):
            return pair.adopt(ROOT, f.root, f.hermes, receipt, scheduler=f.scheduler,
                              window=f.journal, generation=GENERATION)
        with pytest.raises(RuntimeError, match='active_maintenance_window_required'):
            pair.plan(ROOT, f.root, f.hermes, generation=GENERATION)
        for mutate in (
            lambda r: r.update(reviewer=r['prepared_by']),
            lambda r: r['runtime_review'].update(proposal_sha256='stale'),
            lambda r: r['source_sha256'].update({'scripts/unselected.py': sha(b'not selected\n')}),
            lambda r: r.update(state_before_sha256='stale'),
        ):
            bad = copy.deepcopy(review); mutate(bad)
            with pytest.raises(RuntimeError):
                apply(bad)
        # Source and mode CAS, including a half-cohort, refuse without mutation.
        for body, mode in ((b'moved\n', 0o644), (blob(SOURCE, PATHS[0]), 0o644),
                           (before[f.root / PATHS[0]][0], 0o600)):
            put(f.root / PATHS[0], body, mode)
            with pytest.raises(RuntimeError):
                apply()
            put(f.root / PATHS[0], *before[f.root / PATHS[0]])
        assert payloads(f) == before and not f.saves
        with mw.require_active_window(f.journal, f.root, f.hermes) as active:
            with pytest.raises(mw.WindowError, match='outside_adoption_scope'):
                p = str(f.root / 'scripts/unselected.py')
                active.accept_file_updates({p: sha(b'not selected\n')}, {p: sha(b'other\n')})
        @contextmanager
        def blocked_fire(jid):
            yield False
        with monkeypatch.context() as m:
            m.setattr(f.scheduler, '_fire_job_lock', blocked_fire)
            with pytest.raises(RuntimeError, match='dependency_fire_fence_unavailable'):
                apply()
        assert payloads(f) == before and not f.saves
        seen = []
        fire_lock = f.scheduler._fire_job_lock
        @contextmanager
        def tracked_fire(jid):
            with fire_lock(jid) as acquired:
                seen.append(jid)
                yield acquired
        with monkeypatch.context() as m:
            m.setattr(f.scheduler, '_fire_job_lock', tracked_fire)
            result = apply()
        assert seen == sorted(row['entry']['id'] for row in prior_controls if row['entry']['kind'] == 'hermes')
        assert result['applied'] and result['source'] == SOURCE and not f.saves
        assert f.registry.read_bytes() == old_jobs
        expected = copy.deepcopy(state); expected['hashes'].update(AFTER)
        assert json.loads((f.root / syncer.STATE).read_text()) == expected
        transaction = mw.read_journal(f.journal)['source_transaction']
        assert transaction['outcome'] == 'applied' and transaction['job_ids'] == []
        assert transaction['switch_verified']
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
            apply()
        assert payloads(f) == landed and not f.saves
        # A pre-dependency PR71 review cannot survive the state delta. Only now
        # obtain the GLP44 preview, in the same journal, with the new preimage.
        with mw.require_active_window(f.journal, f.root, f.hermes) as active:
            with pytest.raises(RuntimeError, match='reviewed_preimage_or_candidate_moved:state_before_sha256'):
                pair.plan(ROOT, f.root, f.hermes, premature_pr71, window=active)
            next_proposal = pair.plan(ROOT, f.root, f.hermes, window=active)[0]
        assert next_proposal['state_before_sha256'] == sha((f.root / syncer.STATE).read_bytes())
        # Existing uncertainty/half-cohort oracles remain fail closed.
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
        restored = f.scheduler.load_jobs()
        assert restored[0]['enabled'] is True and restored[0]['state'] == 'scheduled'
        assert restored[0]['script_sha256'] == json.loads(old_jobs)['jobs'][0]['script_sha256']
        assert restored[1] == json.loads(old_jobs)['jobs'][1]
        assert {rel: sha((f.root / rel).read_bytes()) for rel in PATHS} == AFTER
    finally:
        f.window.release_locks()
