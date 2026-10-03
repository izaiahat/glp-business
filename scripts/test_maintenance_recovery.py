"""GLP-341 fixtures only: no host scheduler, subprocess, or source deployment."""
import copy
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import maintenance_window as window


class FixtureScheduler:
    def __init__(self, backend):
        self.backend = backend
        self._jobs_file_lock = threading.RLock()

    def _jobs_lock_file(self):
        return self.backend.hermes / 'cron/jobs.lock'

    @contextmanager
    def _fire_job_lock(self, jid):
        if self.backend.before_fire is not None:
            hook, self.backend.before_fire = self.backend.before_fire, None
            hook()
        fd = os.open(self.backend.hermes / ('cron/' + jid + '.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield True
        finally:
            os.close(fd)

    def load_jobs(self):
        return copy.deepcopy(list(self.backend.jobs.values()))

    def _save_jobs_unlocked(self, jobs):
        self.backend.assert_fences()
        self.backend.jobs = {job['id']: copy.deepcopy(job) for job in jobs}
        self.backend.effect('hermes')


class FixtureBridge:
    def __init__(self, backend):
        self.backend = backend

    def identity(self):
        return (71, 'fixture-resident')

    def verify_ingress(self):
        window.require(self.backend.gated, 'fixture_gate_absent')

    def open_ingress(self):
        self.backend.assert_fences()
        self.backend.gated = False
        self.backend.effect('n8n')


class FixtureBackend(window.LinuxBackend):
    def __init__(self, root, hermes, journal):
        super().__init__(root, hermes, journal)
        (hermes / 'cron').mkdir(parents=True)
        self.jobs, self.entries, self.effects = {}, [], []
        self.before_fire = self.after_effect = None
        self.busy, self.gated = False, False
        self.timer_state, self.crontab = 'active', '* * * * * fixture-command\r\n# unrelated\r\n'
        self.timer_configuration_ok = True
        self.native = FixtureScheduler(self)
        self.gateway = FixtureBridge(self)

    def inventory(self):
        entries = copy.deepcopy(self.entries)
        for entry in entries:
            entry['source_sha256'] = {path: window.source_digest(path)
                                      for path in entry['source_sha256']}
        self._report = {'complete': True, 'errors': [], 'entries': entries}
        return self._report

    def scheduler(self):
        return self.native

    def job(self, jid):
        return copy.deepcopy(self.jobs[jid])

    def bridge(self):
        return self.gateway

    def nft_table_absent(self):
        return not self.gated

    def timer_unchanged(self, entry, prior, refresh=False):
        window.require(self.timer_configuration_ok, 'timer_configuration_changed')

    def inflight(self, entries):
        return [{'kind': 'fixture-reader'}] if self.busy else []

    def assert_fences(self):
        data = window.read_journal(self.journal)
        window.require(window.locks_verified(data) and len(data['locks']) == 7,
                       'fixture_effect_without_all_source_fences')

    def effect(self, kind):
        self.effects.append(kind)
        if self.after_effect is not None:
            hook, self.after_effect = self.after_effect, None
            hook()

    def command(self, argv, payload=None):
        if argv == ['/usr/bin/crontab', '-l']:
            return self.crontab
        if argv == ['/usr/bin/crontab', '-']:
            self.assert_fences()
            self.crontab = payload
            self.effect('crontab')
            return ''
        prefix = ['/usr/bin/systemctl', '--user']
        if argv == prefix + ['show', 'fixture.timer', '-p', 'ActiveState', '--value']:
            return self.timer_state + '\n'
        if argv == prefix + ['start', 'fixture.timer']:
            self.assert_fences()
            self.timer_state = 'active'
            self.effect('systemd')
            return ''
        raise AssertionError('Fixture attempted unsupported host command: ' + repr(argv))


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.root, self.hermes = base / 'business', base / 'hermes'
        self.root.mkdir()
        (self.hermes / 'scripts').mkdir(parents=True)
        self.journal = self.root / 'reports/ops/fixture/WINDOW.json'
        self.backend = FixtureBackend(self.root, self.hermes, self.journal)
        self.a, self.b = self.hermes / 'scripts/a.sh', self.hermes / 'scripts/b.sh'
        self.state = self.root / 'ops/state.json'
        self.state.parent.mkdir()
        self.a.write_text('fixture-a-before\n')
        self.b.write_text('fixture-b-before\n')
        self.state.write_text('{"fixture": "before"}\n')
        shared = {str(self.state): window.source_digest(self.state)}
        self.backend.entries = [
            {'kind': 'n8n', 'id': 'fixture-gate', 'command': 'fixture-gate', 'affected': True,
             'prior': {}, 'source_sha256': shared},
            {'kind': 'systemd', 'id': 'fixture.timer', 'command': 'fixture-service', 'affected': True,
             'prior': {'timer_active': 'active'}, 'source_sha256': shared},
            {'kind': 'crontab', 'id': 'fixture-line', 'command': 'fixture-command', 'affected': True,
             'prior': {'index': 0, 'line': '* * * * * fixture-command'}, 'source_sha256': shared}]
        for jid, source in [('b', self.b), ('a', self.a)]:
            self.backend.jobs[jid] = {'id': jid, 'enabled': True, 'state': 'scheduled',
                                     'next_run_at': 'fixture-original-cursor',
                                     'script_sha256': window.source_digest(source), 'last_status': 'ok'}
            self.backend.entries.append({'kind': 'hermes', 'id': jid, 'command': str(source),
                                         'affected': True, 'trace': [str(source)], 'prior': {},
                                         'source_sha256': {**shared, str(source): window.source_digest(source)}})
        report = self.backend.inventory()
        rows = [{'entry': entry, 'prior': self.backend.snapshot(entry), 'intent': True,
                 'paused': True, 'restore_intent': False, 'restored': False}
                for entry in report['entries']]
        self.original = {'schema': window.SCHEMA, 'state': 'recovery_required',
                         'error': 'source_or_adoption_outcome_requires_reconciliation',
                         'journal': str(self.journal), 'root': str(self.root), 'hermes': str(self.hermes),
                         'entries': rows, 'inventory_shape': window.inventory_shape(report),
                         'control_tools': window.control_tools(), 'source_transaction': None,
                         'source_sha256': {path: digest for entry in report['entries']
                                           for path, digest in entry['source_sha256'].items()},
                         'created_at': 0, 'expires_at': 1, 'locks': [],
                         'keeper': {'pid': 0, 'start_ticks': 0, 'boot_id': 'fixture-dead-owner'}}
        window.atomic_json(self.journal, self.original)
        for row in rows:
            if row['entry']['kind'] == 'hermes':
                job = self.backend.jobs[row['entry']['id']]
                for key in window.CONTROL_FIELDS:
                    job.pop(key, None)
                job.update(copy.deepcopy(row['prior']['paused_fields']))
        self.backend.crontab = self.backend._crontab_candidate(rows[2]['prior'])
        self.backend.timer_state, self.backend.gated = 'inactive', True
        # Synthetic independently deployed source/pin changes, not recovery effects.
        self.a.write_text('fixture-a-approved-after\n')
        self.state.write_text('{"fixture": "approved-after"}\n')
        self.backend.jobs['a']['script_sha256'] = window.source_digest(self.a)
        self.window = window.MaintenanceWindow(self.backend, self.journal,
                                               window.default_locks(self.root, self.hermes))
        self.addCleanup(self.window.release_locks)

    def review(self):
        proposal = self.window.recover_pre_adoption()
        return {'schema': 'glp-maintenance-pre-adoption-review-v1',
                'decision': 'ACCEPT_EXACT_PRE_ADOPTION_EXIT',
                'prepared_by': 'fixture-preparer', 'reviewer': 'fixture-independent-reviewer',
                'proposal': proposal,
                'source_approvals': {path: 'fixture-source-approval' for path in proposal['changes']},
                'pin_approvals': {jid: 'fixture-pin-approval' for jid, job in proposal['jobs'].items()
                                  if job['pin_changed']},
                'external_restores': {jid: 'fixture-native-outcome' for jid, job in proposal['jobs'].items()
                                      if job['disposition'] == 'external_preimage'}}

    def external_preimage(self):
        row = self.original['entries'][-1]
        job = self.backend.jobs['a']
        for key in window.CONTROL_FIELDS:
            job.pop(key, None)
        job.update(copy.deepcopy(row['prior']['fields']))

    def assert_no_effects(self):
        self.assertEqual(self.backend.effects, [])
        self.assertEqual(self.window.handles, [])

    def test_new_checkout_preserves_old_tool_custody(self):
        self.original['control_tools'] = {
            str(self.root / 'old-checkout' / Path(path).name): digest
            for path, digest in self.original['control_tools'].items()}
        window.atomic_json(self.journal, self.original)
        self.assertEqual(self.window.recover_pre_adoption()['journal_sha256'], window.json_sha(self.original))

    def test_unchanged_pinless_job_keeps_absence_and_original_hold(self):
        row = self.original['entries'][-2]
        row['prior']['script_sha256'] = None
        self.backend.jobs['b'].pop('script_sha256')
        window.atomic_json(self.journal, self.original)
        self.window.recover_pre_adoption(self.review())
        self.assertNotIn('script_sha256', self.backend.job('b'))
        self.assertEqual(self.backend.job('b')['enabled'], row['prior']['fields']['enabled'])

    def test_native_discovery_coverage_without_complete_boolean(self):
        inventory = self.backend.inventory
        def discovered():
            report = inventory()
            report.pop('complete')
            report['coverage'] = {kind: True for kind in ('hermes', 'crontab', 'systemd', 'n8n')}
            return report
        self.backend.inventory = discovered
        self.assertEqual(self.window.recover_pre_adoption()['journal_sha256'], window.json_sha(self.original))

    def test_approved_sources_close_without_editing_baselines_or_pins(self):
        review = self.review()
        observed = {jid: self.backend.job(jid) for jid in self.backend.jobs}
        self.assertFalse(self.window._sources_valid(self.original, for_exit=True))
        result = self.window.recover_pre_adoption(review)
        self.assertEqual(result['state'], 'closed')
        current = window.read_journal(self.journal)
        self.assertEqual(current['source_sha256'], self.original['source_sha256'])
        self.assertEqual(current['control_tools'], self.original['control_tools'])
        self.assertEqual(current['pre_adoption_recovery']['origin'], self.original)
        self.assertEqual([row['prior'] for row in current['entries']],
                         [row['prior'] for row in self.original['entries']])
        self.assertEqual(self.backend.effects, ['hermes', 'hermes', 'crontab', 'systemd', 'n8n'])
        for row in current['entries']:
            if row['entry']['kind'] == 'hermes':
                jid = row['entry']['id']
                job = self.backend.job(jid)
                self.assertEqual({key: job[key] for key in window.CONTROL_FIELDS if key in job}, row['prior']['fields'])
                self.assertEqual({key: value for key, value in job.items() if key not in window.CONTROL_FIELDS},
                                 {key: value for key, value in observed[jid].items() if key not in window.CONTROL_FIELDS})
        self.assertFalse(window.locks_verified(current))
        current['state'] = 'active'
        self.assertFalse(self.window._verify(current))
        self.assertFalse(self.window._sources_valid(current, for_exit=True))

    def test_external_preimage_needs_separate_approval(self):
        self.external_preimage()
        review = self.review()
        review['external_restores'] = {}
        before = self.journal.read_bytes()
        with self.assertRaisesRegex(window.WindowError, 'external_restores'):
            self.window.recover_pre_adoption(review)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assert_no_effects()

    def test_external_preimage_is_observed_without_second_restore(self):
        self.external_preimage()
        before = self.backend.job('a')
        self.window.recover_pre_adoption(self.review())
        row = window.read_journal(self.journal)['entries'][-1]
        self.assertEqual(row['recovery_resolution'], 'external_preimage')
        self.assertFalse(row['restore_intent'])
        self.assertEqual(self.backend.job('a'), before)
        self.assertEqual(self.backend.effects.count('hermes'), 1)

    def test_native_resume_cursor_normalization_is_a_conflict(self):
        self.external_preimage()
        self.backend.jobs['a']['next_run_at'] = 'fixture-normalized-cursor'
        with self.assertRaisesRegex(window.WindowError, 'recovery_control_conflict'):
            self.review()
        self.assert_no_effects()

    def test_existing_source_without_valid_installed_pin_is_refused(self):
        self.backend.jobs['a']['script_sha256'] = self.original['entries'][-1]['prior']['script_sha256']
        with self.assertRaisesRegex(window.WindowError, 'recovery_pin_invalid'):
            self.review()
        self.assert_no_effects()

    def test_external_pin_change_needs_its_own_approval(self):
        review = self.review()
        review['pin_approvals'] = {}
        with self.assertRaisesRegex(window.WindowError, 'pin_approvals'):
            self.window.recover_pre_adoption(review)
        self.assert_no_effects()

    def test_approval_cannot_authorize_new_source_coverage(self):
        self.backend.entries[-1]['source_sha256'][str(self.b)] = window.source_digest(self.b)
        with self.assertRaisesRegex(window.WindowError, 'recovery_source_coverage_changed'):
            self.review()
        self.assert_no_effects()

    def test_stale_review_does_not_write_journal_or_restore(self):
        review = self.review()
        before = self.journal.read_bytes()
        self.state.write_text('{"fixture": "third-unapproved-version"}\n')
        with self.assertRaises(window.WindowError):
            self.window.recover_pre_adoption(review)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assert_no_effects()

    def test_post_lock_drift_is_rechecked_before_journaling(self):
        review = self.review()
        before = self.journal.read_bytes()
        take = self.window._take_locks
        def moved(data, **kwargs):
            take(data, **kwargs)
            self.state.write_text('{"fixture": "changed-after-admission"}\n')
        with patch.object(self.window, '_take_locks', side_effect=moved):
            with self.assertRaises(window.WindowError):
                self.window.recover_pre_adoption(review)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assert_no_effects()

    def test_native_race_refuses_before_save(self):
        review = self.review()
        self.backend.before_fire = lambda: self.backend.jobs['a'].update(last_status='fixture-concurrent-change')
        with self.assertRaises(window.WindowError):
            self.window.recover_pre_adoption(review)
        self.assert_no_effects()
        self.assertFalse(window.read_journal(self.journal)['entries'][-1]['restored'])

    def test_lost_native_ack_is_read_back_not_replayed(self):
        review = self.review()
        def lost_ack():
            raise OSError('fixture-native-save-landed-but-ack-lost')
        self.backend.after_effect = lost_ack
        with self.assertRaises(window.WindowError):
            self.window.recover_pre_adoption(review)
        self.assertEqual(self.backend.effects, ['hermes'])
        before = self.journal.read_bytes()
        with self.assertRaisesRegex(window.WindowError, 'use_recover_pre_adoption'):
            self.window.exit()
        self.assertEqual(self.journal.read_bytes(), before)
        with self.assertRaisesRegex(window.WindowError, 'recovery_is_exit_only'):
            self.window.enter()
        self.window.recover_pre_adoption(review)
        current = window.read_journal(self.journal)
        self.assertEqual(current['state'], 'closed')
        self.assertEqual(current['entries'][-1]['recovery_resolution'], 'landed_intent')
        self.assertEqual(self.backend.effects.count('hermes'), 2)

    def test_source_drift_stops_before_next_control(self):
        review = self.review()
        self.backend.after_effect = lambda: self.state.write_text('{"fixture": "uncooperative-writer"}\n')
        with self.assertRaises(window.WindowError):
            self.window.recover_pre_adoption(review)
        self.assertEqual(self.backend.effects, ['hermes'])
        self.assertNotEqual(window.read_journal(self.journal)['state'], 'closed')
        self.assertEqual(self.window.handles, [])

    def test_live_owner_without_verified_locks_is_not_dead(self):
        data = window.read_journal(self.journal)
        data['keeper'] = window.identity()
        window.atomic_json(self.journal, data)
        self.assertFalse(window.locks_verified(data))
        with self.assertRaisesRegex(window.WindowError, 'recovery_owner_alive'):
            self.review()
        self.assert_no_effects()

    def test_partial_lock_acquisition_releases_every_descriptor(self):
        review = self.review()
        path = self.window.lock_paths[1]
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                self.window.recover_pre_adoption(review)
            self.assert_no_effects()
            first = os.open(self.window.lock_paths[0], os.O_RDWR)
            try:
                fcntl.flock(first, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(first)
        finally:
            os.close(fd)
        self.assertEqual(window.read_journal(self.journal), self.original)

    def test_any_adopter_transaction_is_refused(self):
        data = window.read_journal(self.journal)
        data['source_transaction'] = {'outcome': 'pending'}
        window.atomic_json(self.journal, data)
        with self.assertRaisesRegex(window.WindowError, 'pre_adoption_window'):
            self.review()
        self.assert_no_effects()

    def test_source_symlink_is_not_approved(self):
        self.a.unlink()
        self.a.symlink_to(self.b)
        with self.assertRaises(window.WindowError):
            self.review()
        self.assert_no_effects()

    def test_nonhermes_lost_ack_readbacks_preserve_exact_state(self):
        for target in ('crontab', 'systemd', 'n8n'):
            with self.subTest(kind=target):
                # Each iteration gets its own complete fixture, never a live control.
                fixture = RecoveryTests(methodName='runTest')
                fixture.setUp()
                try:
                    review = fixture.review()
                    original_effect = fixture.backend.effect
                    failed = []
                    def effect(kind):
                        original_effect(kind)
                        if kind == target and not failed:
                            failed.append(True)
                            raise OSError('fixture-lost-native-ack')
                    fixture.backend.effect = effect
                    with fixture.assertRaises(window.WindowError):
                        fixture.window.recover_pre_adoption(review)
                    fixture.window.recover_pre_adoption(review)
                    fixture.assertEqual(fixture.backend.effects.count(target), 1)
                    fixture.assertEqual(fixture.backend.crontab, fixture.original['entries'][2]['prior']['text'])
                finally:
                    fixture.doCleanups()


    def test_optional_control_field_presence_is_not_normalized(self):
        self.backend.jobs['a']['pending_slot'] = None
        with self.assertRaisesRegex(window.WindowError, 'recovery_control_conflict'):
            self.review()
        self.assert_no_effects()

    def test_unbound_pin_is_unknown_not_permission(self):
        data = window.read_journal(self.journal)
        data['entries'][-1]['prior']['script_sha256'] = None
        self.backend.jobs['a']['script_sha256'] = None
        window.atomic_json(self.journal, data)
        with self.assertRaisesRegex(window.WindowError, 'recovery_pin_binding_unproven'):
            self.review()
        self.assert_no_effects()

    def test_drain_blocker_prevents_any_control_effect(self):
        review = self.review()
        before = self.journal.read_bytes()
        self.backend.busy = True
        with self.assertRaisesRegex(window.WindowError, 'recovery_not_drained'):
            self.window.recover_pre_adoption(review)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assert_no_effects()

    def test_journal_intent_failure_prevents_control_effects(self):
        review = self.review()
        before = self.journal.read_bytes()
        with patch.object(self.window, '_save', side_effect=OSError('fixture-journal-save-failed')):
            with self.assertRaises(OSError):
                self.window.recover_pre_adoption(review)
        self.assertEqual(self.journal.read_bytes(), before)
        self.assert_no_effects()

    def test_wrong_root_is_refused_before_creating_locks(self):
        review = self.review()
        self.backend.root = self.root / 'wrong-target'
        with self.assertRaisesRegex(window.WindowError, 'recovery_target_changed'):
            self.window.recover_pre_adoption(review)
        self.assertFalse(self.backend.root.exists())
        self.assert_no_effects()

    def test_ordinary_exit_still_refuses_unreviewed_sources(self):
        with self.assertRaisesRegex(window.WindowError, 'restore_refused_source'):
            self.window.exit()
        self.assert_no_effects()

    def test_deleted_source_is_not_an_approved_replacement(self):
        self.a.unlink()
        with self.assertRaisesRegex(window.WindowError, 'recovery_existing_sources_only'):
            self.review()
        self.assert_no_effects()


if __name__ == '__main__':
    unittest.main()
