"""Disposable no-effect refusal recovery; no installed intents or controls touched."""
import hashlib
import json
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path

import pytest
import glp_ship as ship
import maintenance_window as mw
from glp_ship_generation import json_sha, validate_review
from test_glp_ship_refusal_inspection import case


def put(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fixture(tmp_path, monkeypatch):
    c = case(tmp_path, monkeypatch)
    c.proposal['run_policy'] = {'entry': 'NONE_ENROLLMENT_ONLY', 'job_ids': list(c.spec.jobs),
                                'on_success': 'preserve', 'on_failure': 'reconcile_no_replay'}
    c.proposal_review = {'schema': 'glp-ship-review-v1', 'decision': 'ACCEPT_EXACT_PROPOSAL',
                         'prepared_by': 'preparer', 'reviewer': 'proposal-reviewer',
                         'proposal_sha256': json_sha(c.proposal)}
    put(c.attempt, {'state': 'started', 'proposal_sha256': json_sha(c.proposal)})
    c.journal = c.output / 'window.json'
    c.window = {'schema': mw.SCHEMA, 'state': 'closed', 'error': 'drain_timeout',
                'journal': str(c.journal), 'root': str(c.root), 'hermes': str(c.home),
                'created_at': 10, 'closed_at': 20, 'expires_at': 100,
                'keeper': {'pid': 2147483647, 'start_ticks': 1, 'boot_id': 'fixture'},
                'locks': [], 'source_sha256': {}, 'control_tools': {}, 'inventory_shape': [],
                'entries': []}
    # Exercise every surface's readback; native record and source preimages are real private files.
    for kind in ('hermes', 'crontab', 'systemd', 'n8n'):
        entry = {'kind': kind, 'id': c.spec.jobs[0] if kind == 'hermes' else kind, 'affected': True}
        job = c.proposal['jobs'][c.spec.jobs[0]]
        prior = {'fields': {}, 'configuration_sha256': mw.job_configuration(job), 'script_sha256': None}
        c.window['entries'].append({'entry': entry, 'prior': prior, 'intent': True,
                                    'paused': True, 'restore_intent': True, 'restored': True})
    c.window['inventory_shape'] = mw.inventory_shape({'entries': [r['entry'] for r in c.window['entries']]})
    put(c.journal, c.window)
    c.fresh_id = 'fresh-001'
    c.fresh_output = c.root / 'reports/ops/glp-ship/fresh-001'
    c.evidence = {'schema': 'glp-ship-drain-refusal-v1', 'source': c.source,
                  'root': str(c.root), 'hermes': str(c.home),
                  'attempt_sha256': digest(c.attempt), 'proposal_sha256': json_sha(c.proposal),
                  'window_journal': str(c.journal), 'window_sha256': digest(c.journal),
                  'recorded_by': 'observer', 'apply_exited': True, 'exit_code': 2,
                  'stage': 'MaintenanceWindow.enter', 'exception': 'drain_timeout',
                  'adoption_started': False, 'producer_started': False, 'effects_uncertain': False}
    c.review = {'schema': 'glp-ship-drain-supersession-review-v1',
                'decision': 'ACCEPT_EXACT_DRAIN_SUPERSESSION', 'reviewer': 'independent',
                'evidence_sha256': json_sha(c.evidence), 'proposal_sha256': json_sha(c.proposal),
                'fresh_attempt_id': c.fresh_id, 'fresh_output': str(c.fresh_output)}
    c.readbacks = []
    class ReadOnlyBackend:
        def __init__(self, *a): pass
        def job(self, jid):
            return next(j for j in json.loads((c.home / 'cron/jobs.json').read_text())['jobs'] if j['id'] == jid)
        def recovery_restored(self, entry, prior):
            c.readbacks.append(entry['kind'])
            return True
    monkeypatch.setattr(mw, 'LinuxBackend', ReadOnlyBackend)
    monkeypatch.setattr(ship, 'authenticate_installer', lambda *a: None)
    monkeypatch.setattr(ship, 'preclaim_plan', lambda *a: None)
    monkeypatch.setattr(ship, 'apply_generation', lambda *a: pytest.fail('transition invoked apply'))
    c.record = c.attempt.with_suffix('.supersession.json')
    c.fresh_intent = c.attempt.with_name(c.attempt.stem + '.retry-' + c.fresh_id + '.json')
    return c


def supersede(c):
    return ship.supersede_drain_refusal(c.root, c.root, c.home, c.spec, c.proposal,
                                       c.proposal_review, c.evidence, c.review,
                                       c.fresh_id, c.fresh_output)


def retry(c, **changes):
    args = dict(checkout=c.root, root=c.root, hermes=c.home, spec=c.spec,
                proposal=c.proposal, proposal_review=c.proposal_review,
                attempt_id=c.fresh_id, output=c.fresh_output)
    args.update(changes)
    return ship.claim_retry(**args)


def test_reviewed_closed_drain_refusal_supersedes_without_rewriting_intent(tmp_path, monkeypatch):
    c = fixture(tmp_path, monkeypatch)
    before = {p: p.read_bytes() for p in (c.attempt, c.journal, c.leaf, c.home / 'cron/jobs.json')}
    result = supersede(c)
    assert result['state'] == 'superseded'
    assert result['proposal_sha256'] == json_sha(c.proposal)
    assert result['fresh_attempt'] == {'id': c.fresh_id, 'intent': str(c.fresh_intent),
                                       'output': str(c.fresh_output), 'state': 'reserved'}
    assert json.loads(c.record.read_text()) == result
    assert result['original_intent_sha256'] == digest(c.attempt)
    assert not c.fresh_intent.exists() and not c.fresh_output.exists()
    assert set(c.readbacks) == {'crontab', 'systemd', 'n8n'}
    assert all(p.read_bytes() == b for p, b in before.items())
    with pytest.raises(RuntimeError):
        ship.claim_attempt(c.attempt, c.proposal, checkout=c.root)


def test_exact_binding_allows_only_one_new_exclusive_started_attempt(tmp_path, monkeypatch):
    c = fixture(tmp_path, monkeypatch)
    supersede(c)
    old = c.attempt.read_bytes(); record = c.record.read_bytes()
    assert retry(c) == c.fresh_intent
    claimed = json.loads(c.fresh_intent.read_text())
    assert claimed['state'] == 'started' and claimed['attempt_id'] == c.fresh_id
    assert claimed['proposal_sha256'] == json_sha(c.proposal)
    assert claimed['supersession_sha256'] == digest(c.record)
    with pytest.raises(RuntimeError):
        retry(c)
    assert c.attempt.read_bytes() == old and c.record.read_bytes() == record


@pytest.mark.parametrize('stage', ['supersession', 'fresh-claim'])
@pytest.mark.parametrize('bad', [
    'missing-hermes', 'missing-crontab', 'missing-systemd', 'missing-n8n', 'missing-inventory',
    'empty-inventory', 'non-list-inventory', 'malformed-member', 'duplicate-identity',
    'conflicting-identity', 'unknown-kind', 'non-string-id', 'non-string-command',
    'non-bool-affected', 'changed-id', 'changed-command', 'changed-affected',
    'non-object-entry', 'non-object-prior',
])
def test_incomplete_or_inconsistent_inventory_refuses_before_reservation_or_claim(tmp_path, monkeypatch, stage, bad):
    c = fixture(tmp_path, monkeypatch)
    if stage == 'fresh-claim':
        supersede(c)
    inventory = c.window['inventory_shape']
    if bad.startswith('missing-') and bad != 'missing-inventory':
        kind = bad.removeprefix('missing-')
        c.window['entries'] = [r for r in c.window['entries'] if r['entry']['kind'] != kind]
    elif bad == 'missing-inventory': c.window.pop('inventory_shape')
    elif bad == 'empty-inventory': c.window['inventory_shape'] = []
    elif bad == 'non-list-inventory': c.window['inventory_shape'] = {'unproven': True}
    elif bad == 'malformed-member': inventory[0].pop()
    elif bad == 'duplicate-identity': inventory.append(deepcopy(inventory[0]))
    elif bad == 'conflicting-identity':
        inventory.append([*inventory[0][:3], False])
    elif bad == 'unknown-kind': inventory[0][0] = 'unknown'
    elif bad == 'non-string-id': inventory[0][1] = []
    elif bad == 'non-string-command': inventory[0][2] = {}
    elif bad == 'non-bool-affected': inventory[0][3] = 1
    elif bad == 'changed-id': inventory[0][1] += '-other'
    elif bad == 'changed-command': inventory[0][2] = 'different command'
    elif bad == 'changed-affected': inventory[0][3] = False
    elif bad == 'non-object-entry': c.window['entries'][0]['entry'] = []
    elif bad == 'non-object-prior': c.window['entries'][0]['prior'] = []
    put(c.journal, c.window)
    c.evidence['window_sha256'] = digest(c.journal)
    c.review['evidence_sha256'] = json_sha(c.evidence)
    if stage == 'fresh-claim':
        # Even exact re-reviewed/rebound historical evidence must be internally complete.
        stored = json.loads(c.record.read_text())
        stored.update(evidence=c.evidence, review=c.review)
        put(c.record, stored)
    before = {p: p.read_bytes() for p in (c.attempt, c.journal, c.leaf, c.home / 'cron/jobs.json')}
    c.readbacks.clear()
    with pytest.raises(RuntimeError, match='ship_supersession_.*(inventory|restoration)'):
        retry(c) if stage == 'fresh-claim' else supersede(c)
    assert all(p.read_bytes() == b for p, b in before.items())
    assert not c.fresh_intent.exists() and not c.fresh_output.exists()
    assert c.record.exists() == (stage == 'fresh-claim')
    assert not c.readbacks  # Validate the entire cohort before any control readback.


def test_complete_inventory_with_unaffected_members_needs_only_affected_restoration_rows(tmp_path, monkeypatch):
    c = fixture(tmp_path, monkeypatch)
    c.window['inventory_shape'].append(['systemd', 'unrelated.timer', 'unchanged command', False])
    put(c.journal, c.window)
    c.evidence['window_sha256'] = digest(c.journal)
    c.review['evidence_sha256'] = json_sha(c.evidence)
    assert supersede(c)['state'] == 'superseded'
    assert retry(c) == c.fresh_intent
    assert set(c.readbacks) == {'crontab', 'systemd', 'n8n'}


@pytest.mark.parametrize('bad', [
    'missing-journal', 'legacy-prewindow', 'open', 'no-error', 'restore-failed', 'missing-row-proof',
    'unrestored', 'row-no-intent', 'empty-rows', 'duplicate-row', 'unknown-kind', 'unknown-field',
    'source_transaction', 'job_updates', 'job_outcomes', 'pre_adoption_recovery', 'locks', 'live-keeper',
    'live-controller', 'result', 'enrollment-receipt', 'default-result', 'fresh-output',
    'drift', 'mode-drift', 'job-drift', 'attempt-drift', 'proposal-drift', 'review-drift',
    'same-reviewer', 'same-preparer', 'apply-inflight', 'effects-uncertain', 'adoption-started',
    'producer-started', 'bad-stage', 'bad-exit', 'unreadable-owner', 'restoration-readback',
    'symlink-journal', 'wrong-journal-root', 'same-output', 'invalid-id', 'no-restoration-evidence',
    'intent-hardlink', 'journal-hardlink', 'unknown-evidence', 'source-mismatch',
])
def test_refuses_effects_ambiguity_missing_restoration_and_stale_preimages(tmp_path, monkeypatch, bad):
    c = fixture(tmp_path, monkeypatch)
    if bad == 'missing-journal': c.journal.unlink()
    elif bad == 'legacy-prewindow': c.evidence['schema'] = 'glp-ship-pre-window-refusal-v1'
    elif bad == 'open': c.window['state'] = 'draining'
    elif bad == 'no-error': c.window.pop('error')
    elif bad == 'restore-failed': c.window['restore_errors'] = [{'error': 'unknown'}]
    elif bad == 'missing-row-proof': c.window['entries'][0].pop('restored')
    elif bad == 'unrestored': c.window['entries'][0]['restored'] = False
    elif bad == 'row-no-intent': c.window['entries'][0]['intent'] = False
    elif bad == 'empty-rows': c.window['entries'] = []
    elif bad == 'duplicate-row': c.window['entries'].append(deepcopy(c.window['entries'][0]))
    elif bad == 'unknown-kind': c.window['entries'][0]['entry']['kind'] = 'unknown'
    elif bad == 'unknown-field': c.window['unaccounted_effect'] = False
    elif bad in ('source_transaction', 'job_updates', 'job_outcomes', 'pre_adoption_recovery'):
        c.window[bad] = {}
    elif bad == 'locks': c.window['locks'] = [{'fd': 5}]
    elif bad in ('live-keeper', 'live-controller'):
        c.window['keeper' if bad == 'live-keeper' else 'controller'] = mw.identity()
    elif bad in ('result', 'enrollment-receipt'):
        put(c.output / ('RESULT.json' if bad == 'result' else 'enrollment-receipt.json'), {})
    elif bad == 'default-result':
        # Output is already the default in this fixture.
        put(c.output / 'RESULT.json', {})
    elif bad == 'fresh-output': c.fresh_output.mkdir()
    elif bad == 'drift': c.leaf.write_text('changed')
    elif bad == 'mode-drift': c.leaf.chmod(0o700)
    elif bad == 'job-drift': put(c.home / 'cron/jobs.json', {'jobs': []})
    elif bad == 'attempt-drift': c.attempt.write_text(c.attempt.read_text() + ' ')
    elif bad == 'proposal-drift': c.proposal['prepared_by'] = 'different'
    elif bad == 'review-drift': c.proposal_review['proposal_sha256'] = '0' * 64
    elif bad == 'same-reviewer': c.review['reviewer'] = ' OBSERVER '
    elif bad == 'same-preparer': c.review['reviewer'] = 'PREPARER'
    elif bad == 'apply-inflight': c.evidence['apply_exited'] = False
    elif bad == 'effects-uncertain': c.evidence['effects_uncertain'] = True
    elif bad == 'adoption-started': c.evidence['adoption_started'] = True
    elif bad == 'producer-started': c.evidence['producer_started'] = True
    elif bad == 'bad-stage': c.evidence['stage'] = 'adopt'
    elif bad == 'bad-exit': c.evidence['exit_code'] = 0
    elif bad == 'unreadable-owner':
        monkeypatch.setattr(mw, 'identity', lambda *a: (_ for _ in ()).throw(PermissionError()))
    elif bad == 'restoration-readback':
        monkeypatch.setattr(mw.LinuxBackend, 'recovery_restored', lambda *a: False)
    elif bad == 'symlink-journal':
        target = c.journal.with_suffix('.real'); c.journal.rename(target); c.journal.symlink_to(target)
    elif bad == 'wrong-journal-root': c.window['root'] = '/other'
    elif bad == 'same-output': c.fresh_output = c.output
    elif bad == 'invalid-id': c.fresh_id = '../escape'
    elif bad == 'no-restoration-evidence': c.evidence.pop('window_sha256')
    elif bad in ('intent-hardlink', 'journal-hardlink'):
        import os
        path = c.attempt if bad == 'intent-hardlink' else c.journal
        os.link(path, tmp_path / 'alias')
    elif bad == 'unknown-evidence': c.evidence['possible_effect'] = True
    elif bad == 'source-mismatch': c.evidence['source'] = 'c' * 40
    if c.journal.exists() and not c.journal.is_symlink():
        put(c.journal, c.window)
        if bad != 'no-restoration-evidence':
            c.evidence['window_sha256'] = digest(c.journal)
    # Independent review of a bad document is NOT a bypass of proof checks.
    c.review.update(evidence_sha256=json_sha(c.evidence), fresh_attempt_id=c.fresh_id,
                    fresh_output=str(c.fresh_output))
    before = c.attempt.read_bytes()
    with pytest.raises((RuntimeError, OSError, ValueError)):
        supersede(c)
    assert c.attempt.read_bytes() == before
    assert not c.record.exists() and not c.fresh_intent.exists()


@pytest.mark.parametrize('bad', ['id', 'output', 'proposal', 'preimage', 'job', 'review', 'record', 'no-record', 'preclaim'])
def test_reserved_retry_rechecks_binding_and_target_before_claim(tmp_path, monkeypatch, bad):
    c = fixture(tmp_path, monkeypatch)
    supersede(c)
    changes = {}
    if bad == 'id': changes['attempt_id'] = 'another'
    elif bad == 'output': changes['output'] = c.fresh_output.with_name('another')
    elif bad == 'proposal': c.proposal['prepared_by'] = 'changed'
    elif bad == 'preimage': c.leaf.write_text('changed')
    elif bad == 'job': put(c.home / 'cron/jobs.json', {'jobs': []})
    elif bad == 'review': c.proposal_review = None
    elif bad == 'record': c.attempt.write_text(c.attempt.read_text() + ' ')
    elif bad == 'no-record': c.record.unlink()
    elif bad == 'preclaim':
        monkeypatch.setattr(ship, 'preclaim_plan', lambda *a: (_ for _ in ()).throw(RuntimeError('plan refused')))
    with pytest.raises((RuntimeError, OSError)):
        retry(c, **changes)
    assert not c.fresh_intent.exists()


@pytest.mark.parametrize('bad', [None, [], 'not a boundary document'])
def test_non_object_boundary_is_explicitly_refused_without_transition(tmp_path, monkeypatch, bad):
    c = fixture(tmp_path, monkeypatch)
    c.evidence = bad
    with pytest.raises(RuntimeError):
        supersede(c)
    assert not c.record.exists() and not c.fresh_intent.exists()


def test_supersession_is_exclusive_and_cannot_reserve_a_second_id(tmp_path, monkeypatch):
    c = fixture(tmp_path, monkeypatch)
    supersede(c)
    original = c.record.read_bytes()
    c.fresh_id = 'fresh-002'
    c.review['fresh_attempt_id'] = c.fresh_id
    with pytest.raises(RuntimeError): supersede(c)
    assert c.record.read_bytes() == original


def test_existing_fresh_intent_and_partial_supersession_fail_closed(tmp_path, monkeypatch):
    c = fixture(tmp_path, monkeypatch)
    c.record.write_text('{"state":')  # Crash before fsync: uncertainty is not retry permission.
    with pytest.raises((RuntimeError, ValueError)): retry(c)
    assert c.record.read_text() == '{"state":'
    c.record.unlink()
    put(c.fresh_intent, {'state': 'started'})
    with pytest.raises(RuntimeError): supersede(c)
    assert not c.record.exists()


def cli_fixture(c, monkeypatch):
    for name, value in [('ready', {'proposal': c.proposal}), ('accept', c.proposal_review),
                        ('evidence', c.evidence), ('reconcile-review', c.review)]:
        put(c.root / (name + '.json'), value)
    @contextmanager
    def committed(checkout, source):
        assert source == 'd' * 40  # Installer identity is distinct from the historical proposal.
        yield c.root, {}
    monkeypatch.setattr(ship, 'committed_checkout', committed)
    monkeypatch.setattr(ship, 'prepare', lambda *a: pytest.fail('must not regenerate accepted proposal'))
    return [c.spec.jobs[-1], '--merge-sha', 'd' * 40, '--root', str(c.root), '--hermes', str(c.home),
            '--checkout', str(c.root), '--proposal', str(c.root / 'ready.json'),
            '--review', str(c.root / 'accept.json'), '--attempt-id', c.fresh_id,
            '--output', str(c.fresh_output)]


def test_real_cli_transition_then_distinct_apply_reuses_exact_accepted_proposal(tmp_path, monkeypatch):
    c = fixture(tmp_path, monkeypatch)
    args = cli_fixture(c, monkeypatch)
    old = c.attempt.read_bytes()
    assert ship.main(args + ['--supersede-drain-refusal', '--refusal-evidence', str(c.root / 'evidence.json'),
                             '--refusal-review', str(c.root / 'reconcile-review.json')]) == 0
    assert not c.fresh_intent.exists()
    calls = []
    def apply(checkout, root, hermes, spec, generation, review, output):
        assert c.fresh_intent.exists()
        assert generation.proposal == c.proposal and generation.source == c.source
        validate_review(generation.proposal, review)
        calls.append(output)
        return {'success': True, 'runs': [], 'policy': 'ENROLLMENT_ONLY_NO_EXECUTION'}
    monkeypatch.setattr(ship, 'apply_generation', apply)
    assert ship.main(args + ['--apply']) == 0
    assert calls == [c.fresh_output]
    assert json.loads(c.fresh_intent.read_text())['state'] == 'succeeded'
    assert c.attempt.read_bytes() == old
    assert ship.main(args + ['--apply']) == 2
    assert len(calls) == 1


@pytest.mark.parametrize('mode', ['inspect-mixed', 'apply-and-supersede', 'no-id', 'no-accept', 'no-evidence',
                                 'no-refusal-review', 'no-output', 'no-proposal', 'plain-id', 'id-with-evidence'])
def test_real_cli_refuses_mixed_or_incomplete_recovery_inputs_before_claim(tmp_path, monkeypatch, mode):
    c = fixture(tmp_path, monkeypatch)
    args = cli_fixture(c, monkeypatch)
    args += ['--supersede-drain-refusal', '--refusal-evidence', str(c.root / 'evidence.json'),
             '--refusal-review', str(c.root / 'reconcile-review.json')]
    if mode == 'inspect-mixed': args += ['--inspect-attempt']
    elif mode == 'apply-and-supersede': args += ['--apply']
    elif mode in ('plain-id', 'id-with-evidence'):
        args.remove('--supersede-drain-refusal')
        if mode == 'id-with-evidence': args += ['--apply']
    else:
        flag = {'no-id': '--attempt-id', 'no-accept': '--review', 'no-evidence': '--refusal-evidence',
                'no-refusal-review': '--refusal-review', 'no-output': '--output', 'no-proposal': '--proposal'}[mode]
        at = args.index(flag); del args[at:at + 2]
    assert ship.main(args) == 2
    assert not c.record.exists() and not c.fresh_intent.exists()


def test_claimed_retry_still_hits_real_window_drain_refusal_before_adoption(tmp_path, monkeypatch):
    import glp44_scoped_adoption as pair
    import glp_ship_runtime as runtime
    import test_maintenance_window as windows
    c = fixture(tmp_path, monkeypatch)
    supersede(c); retry(c)
    original = c.attempt.read_bytes()
    backend = windows.FakeBackend()
    backend.root, backend.hermes = c.root, c.home
    backend.running = ['busy fixture']
    actual_window = mw.MaintenanceWindow
    def private_window(backend, journal, locks):
        return actual_window(backend, journal, [tmp_path / 'fixture-source.lock', tmp_path / 'fixture-jobs.lock'],
                             drain_timeout=0.01, poll_interval=0.005)
    monkeypatch.setattr(mw, 'MaintenanceWindow', private_window)
    monkeypatch.setattr(pair, 'adopt', lambda *a, **k: pytest.fail('drain refusal reached adoption'))
    monkeypatch.setattr(runtime, 'run_registered', lambda *a, **k: pytest.fail('enrollment ran producer'))
    generation = ship.GitGeneration(c.source, c.spec.kind, c.spec.paths, c.proposal['hashes'],
                                    c.proposal['merge_proof'], c.proposal)
    with pytest.raises(mw.WindowError, match='drain_timeout'):
        ship._apply_and_run(c.root, c.root, c.home, c.spec, generation, c.proposal_review,
                            c.fresh_output, c.fresh_output / 'window.json', backend, None)
    saved = json.loads((c.fresh_output / 'window.json').read_text())
    assert saved['state'] == 'closed' and saved['error'] == 'drain_timeout'
    assert all(r['restored'] is True for r in saved['entries'])
    assert backend.states == backend.original
    assert c.attempt.read_bytes() == original
    assert json.loads(c.fresh_intent.read_text())['state'] == 'started'
    with pytest.raises(RuntimeError): retry(c)


def test_two_concurrent_transition_owners_can_bind_only_one_reservation(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    c = fixture(tmp_path, monkeypatch)
    def enter():
        try:
            supersede(c)
            return 'reserved'
        except (RuntimeError, ValueError):
            return 'refused'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: enter(), range(2)))
    assert sorted(results) == ['refused', 'reserved']
    assert json.loads(c.record.read_text())['fresh_attempt']['id'] == c.fresh_id


def test_fsync_failure_retains_uncertain_exclusive_record_no_second_transition(tmp_path, monkeypatch):
    import glp_ship_attempts as attempts
    c = fixture(tmp_path, monkeypatch)
    original = c.attempt.read_bytes()
    monkeypatch.setattr(attempts.os, 'fsync', lambda *a: (_ for _ in ()).throw(OSError('lost fsync')))
    with pytest.raises(OSError): supersede(c)
    assert c.record.exists() and c.attempt.read_bytes() == original
    with pytest.raises(RuntimeError): supersede(c)
