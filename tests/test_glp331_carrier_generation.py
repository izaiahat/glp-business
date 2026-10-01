"""Disposable exact-window controls; synthetic reviews are not runtime authority."""
import copy
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from test_glp331_carrier_closure import fixture, review_fixture, ROOT
from test_glp333_reporting_generation import PortableScheduler, put
import fleet_pin_release as fleet
import maxbounty_pin_release as p
import maintenance_inventory as inventory
import maintenance_window as mw


@pytest.mark.parametrize('outcome', ['ok', 'lost_ack', 'bad_readback', 'second_link', 'alias_unlink'])
def test_unrelated_registry_churn_in_window(tmp_path, monkeypatch, outcome):
    f = fixture(tmp_path, monkeypatch)
    repo, root, home, bodies, jobs, carrier, registry, dependency = f
    # Main's actual whole-registry-CAS counterexample, not a missing-import RED.
    old_review, _, _, _ = review_fixture(f, tmp_path)
    changed = copy.deepcopy(jobs); changed[-1]['last_status'] = 'unrelated natural tick'
    put(registry, json.dumps({'jobs': changed, 'updated_at': 'churn'}).encode())
    if importlib.util.find_spec('glp331_carrier_adoption') is None:
        fleet.release_glp331(old_review, repo, business=root, hermes=home)
        pytest.fail('main unexpectedly tolerated unrelated registry churn')
    import glp331_carrier_adoption as generation
    for name in ('glp331_carrier_adoption.py', 'maintenance_inventory.py'):
        put(repo / 'scripts' / name, (ROOT / 'scripts' / name).read_bytes())
    import subprocess
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-qm', 'generation fixture'], check=True)
    for job in changed[1:]:
        put(home / 'scripts' / job['script'], b'# harmless unrelated entry\n')
    put(root / 'ops/n8n-bridge/actions.json', b'{}\n')
    native = os.environ.get('GLP331_NATIVE_CRON_SOURCE')
    if native:
        assert not any(k == 'cron' or k.startswith('cron.') for k in sys.modules)
        monkeypatch.setenv('HERMES_HOME', str(home))
        monkeypatch.setenv('HERMES_SKIP_ENV_FILE', '1')
        monkeypatch.syspath_prepend(native)
        from cron import jobs as scheduler
        assert scheduler._current_cron_store().jobs_file == registry
    else:
        scheduler = PortableScheduler(registry)
    bridge = {'paused': False}
    def probe(argv):
        if argv == ['crontab', '-l'] or 'list-unit-files' in argv or 'list-units' in argv:
            return ''
        return ('LoadState=loaded\nActiveState=active\nUnitFileState=enabled\nMainPID=123\n'
                'ExecStart={ path=/usr/bin/true ; argv[]=true ; ignore_errors=no ; }\n')
    class Backend(mw.LinuxBackend):
        def scheduler(self): return scheduler
        def inventory(self): return inventory.discover(root, home, probe=probe)
        def snapshot(self, entry):
            return {'fixture_paused': bridge['paused']} if entry['kind'] == 'n8n' else super().snapshot(entry)
        def pause(self, entry, prior):
            if entry['kind'] == 'n8n': bridge['paused'] = True
            else: super().pause(entry, prior)
        def paused(self, entry, prior):
            return bridge['paused'] if entry['kind'] == 'n8n' else super().paused(entry, prior)
        def restore(self, entry, prior):
            if entry['kind'] == 'n8n': bridge['paused'] = prior['fixture_paused']
            else: super().restore(entry, prior)
        def cron_tool(self, action, jid):
            assert action == 'pause'
            current = scheduler.load_jobs()
            next(j for j in current if j['id'] == jid).update(enabled=False, state='paused')
            scheduler._save_jobs_unlocked(current)
        def inflight(self, entries): return []  # no launched work in these roots
    journal = tmp_path / 'window.json'
    backend = Backend(root, home, journal)
    locks = [tmp_path / 'reader.lock', tmp_path / 'writer.lock']
    window = mw.MaintenanceWindow(backend, journal, locks)
    monkeypatch.setattr(mw, 'LinuxBackend', lambda *_: backend)
    monkeypatch.setattr(mw, 'default_locks', lambda *_: locks)
    window.enter()
    calls, fences = [], []
    save = scheduler._save_jobs_unlocked
    fire = scheduler._fire_job_lock
    @contextmanager
    def tracked_fire(jid):
        with fire(jid) as acquired:
            fences.append(jid)
            yield acquired
    monkeypatch.setattr(scheduler, '_fire_job_lock', tracked_fire)
    try:
        assert window.status()['verified']
        with mw.require_active_window(journal, root, home) as active:
            proposal, observed, staged = generation.plan(repo, root, home, 'fixture-author', window=active)
        literal = {
            root / 'scripts/affiliate_program_withdrawal.py',
            root / 'scripts/affiliate_sheet_batch_writer.py',
            root / 'scripts/affiliate_terms_fanout.py',
            root / 'scripts/affiliate_approval_auto_reconciler.py',
            root / 'scripts/affiliate_withdrawal_candidate.py',
            root / 'scripts/glp_sheet_sync_drain.py',
            root / 'ops/hermes-cron-scripts/glp_affiliate_reconciliation_honesty.sh',
            home / 'scripts/glp_affiliate_reconciliation_honesty.sh',
        }
        assert set(staged) == literal
        assert str(root / 'ops/.rebuild-sync-state.json') not in proposal['proposed_sha256']
        assert len([v for v in observed.values() if v is None]) == 2
        assert set(staged) <= inventory.affected_paths(root, home)
        assert str(registry) not in proposal['preimages']
        review = {'schema': 'glp331-carrier-generation-review-v1', 'decision': 'ACCEPT_EXACT_BYTES',
                  'reviewer': 'fixture-independent', 'reviewed_at_utc': generation.dt.datetime.now(generation.dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                  'proposal': copy.deepcopy(proposal),
                  'runtime_review': {'schema': 'glp331-carrier-runtime-review-v1',
                                    'decision': 'ACCEPT_EXACT_BYTES', 'reviewer': 'fixture-receiver',
                                    'proposal_sha256': p.sha_bytes(p.json_bytes(proposal))},
                  'writer_exclusion': 'fixture only: no nonparticipating callers or writers',
                  'evidence': ['synthetic fixture, not live approval']}
        def apply(receipt=review):
            return generation.adopt(repo, root, home, receipt, scheduler=scheduler, window=journal)
        for mutate in (lambda r: r.update(reviewer='fixture-author'),
                       lambda r: r['runtime_review'].update(proposal_sha256='moved'),
                       lambda r: r['proposal']['proposed_sha256'].update({'outside': '0'*64})):
            bad = copy.deepcopy(review); mutate(bad)
            with pytest.raises(RuntimeError): apply(bad)
        original_jobs = scheduler.load_jobs()
        assert set(proposal['consumer_records']) == set(fleet.GLP331_CONSUMERS)
        # Scoped field presence changes refuse; unrelated records may advance.
        moved = copy.deepcopy(original_jobs); moved[1]['run_claim'] = None
        put(registry, json.dumps({'jobs': moved}).encode())
        with pytest.raises(RuntimeError): apply()
        put(registry, json.dumps({'jobs': original_jobs}).encode())
        current = copy.deepcopy(original_jobs); current[-1]['last_status'] = 'later unrelated tick'
        put(registry, json.dumps({'jobs': current, 'updated_at': 'later'}).encode())
        def saved(candidate, **kwargs):
            calls.append(copy.deepcopy(candidate))
            assert fences[-3:] == sorted(fleet.GLP331_CONSUMERS)
            data = mw.read_journal(journal)
            assert data['source_transaction']['job_ids'] == [fleet.GLP331_JOB]
            assert data['source_switch']['state'] == 'commit_uncertain'
            assert data['source_switch']['commit_started']
            assert set(data['source_transaction']['after']) == {str(path) for path in staged}
            for path in staged:
                assert path.stat().st_mode & 0o777 == 0
                with pytest.raises(PermissionError): path.read_bytes()
            save(candidate, **kwargs)
            if outcome == 'lost_ack':
                raise OSError('synthetic lost native acknowledgement')
            if outcome == 'bad_readback':
                value = json.loads(registry.read_bytes())
                value['jobs'][-1]['last_status'] = 'readback drift'
                put(registry, json.dumps(value).encode())
        monkeypatch.setattr(scheduler, '_save_jobs_unlocked', saved)
        if outcome == 'second_link':
            real_link = mw.os.link
            def link(source, destination, **kwargs):
                if Path(source).name.startswith('.glp331-new-') and 'candidate' in str(destination):
                    raise OSError('synthetic second public link failure')
                return real_link(source, destination, **kwargs)
            monkeypatch.setattr(mw.os, 'link', link)
        if outcome == 'alias_unlink':
            real_unlink = mw.os.unlink
            def unlink(path, *args, **kwargs):
                if Path(path).name.startswith('.glp331-new-'):
                    raise OSError('synthetic alias unlink failure')
                return real_unlink(path, *args, **kwargs)
            monkeypatch.setattr(mw.os, 'unlink', unlink)
        if outcome != 'ok':
            with pytest.raises(Exception, match='adoption_outcome_uncertain'):
                apply()
            data = mw.read_journal(journal)
            assert data['source_transaction']['outcome'] == 'uncertain'
            assert data['source_switch']['state'] == 'commit_uncertain'
            assert set(data['job_updates']) == {fleet.GLP331_JOB}
            for path in staged:
                if path.exists():
                    assert path.stat().st_mode & 0o777 == 0
            assert all(Path(alias).exists() and Path(alias).stat().st_mode & 0o777 == 0
                       for alias in data['new_source_aliases'].values())
            assert not window.status()['verified']
            with pytest.raises(RuntimeError, match='no_replay'):
                apply()
            assert len(calls) == (0 if outcome == 'second_link' else 1)
            with pytest.raises(Exception, match='restore_refused_source_or_adoption_outcome'):
                window.exit()
            return
        result = apply()
        assert result['applied'] and len(calls) == 1
        expected = copy.deepcopy(current); expected[0]['script_sha256'] = p.sha_bytes(staged[carrier])
        assert scheduler.load_jobs() == expected
        assert mw.read_journal(journal)['source_transaction']['outcome'] == 'applied'
        for path, body in staged.items():
            assert p.read_regular(path) == (body, proposal['source_modes'][str(path)])
            assert path.stat().st_nlink == 1
        assert not list(root.rglob('.glp331-new-*'))
        assert window.status()['verified']
        with pytest.raises(RuntimeError): apply()
        assert len(calls) == 1
        # Restoration uses native exact controls and only the declared pin.
        monkeypatch.setattr(scheduler, '_save_jobs_unlocked', save)
        window.exit()
        restored = scheduler.load_jobs()
        for job in restored:
            original = next(j for j in changed if j['id'] == job['id'])
            if job['id'] in fleet.GLP331_CONSUMERS:
                assert job['enabled'] == original['enabled'] and job['state'] == original['state']
                assert job.get('next_run_at') == original.get('next_run_at')
        assert restored[-1] == expected[-1]
    finally:
        window.release_locks()
