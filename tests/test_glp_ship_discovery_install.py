"""Disposable F01 source/wrapper/native registry installation; no workflow runs."""
import copy
import importlib
import json
import subprocess
from pathlib import Path

import pytest

from test_discovery_predecessor_migration import repo, inputs, dump, sha, REL, BEFORE
from test_glp_ship_discovery import successor
from test_glp333_reporting_generation import PortableScheduler
from glp_ship_generation import GitGeneration, fingerprint, json_sha
from scripts import discovery_predecessor_migration as migration
from scripts import sync_from_repo as syncer

WRAPPER = 'affiliate_supply_discovery_daily.sh'
SNAPSHOT = 'ops/hermes-cron-scripts/' + WRAPPER
JID = '58202b06bdc0'


@pytest.fixture
def install_fixture(successor, inputs, tmp_path):
    candidate, _ = successor
    root, review_path, authority_path, _ = inputs
    hermes = root.parent / '.hermes'
    installed = hermes / 'scripts' / WRAPPER
    installed.parent.mkdir()
    old_wrapper = (candidate / SNAPSHOT).read_bytes()
    installed.write_bytes(old_wrapper)
    installed.chmod(0o700)
    state = json.loads((root / syncer.STATE).read_text())
    for path in migration.COHORT - {REL}:
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((candidate / path).read_bytes())
        state['hashes'][path] = sha(destination.read_bytes())
    dump(root / syncer.STATE, state)
    new_wrapper = old_wrapper.replace(migration.AFTER.encode(), sha((candidate / REL).read_bytes()).encode())
    assert new_wrapper != old_wrapper
    (candidate / SNAPSHOT).write_bytes(new_wrapper)
    manifest = json.loads((candidate / syncer.MANIFEST).read_text())
    manifest['files'][SNAPSHOT]['sha256'] = sha(new_wrapper)
    dump(candidate / syncer.MANIFEST, manifest)
    subprocess.run(['git', '-C', str(candidate), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-qam', 'Wrapper fixture'], check=True)
    source = subprocess.check_output(['git', '-C', str(candidate), 'rev-parse', 'HEAD'], text=True).strip()
    job = {'id': JID, 'script': WRAPPER, 'script_sha256': sha(old_wrapper), 'no_agent': True,
           'enabled': True, 'paused_at': None, 'schedule': {'kind': 'cron', 'expr': '0 7 * * 1'},
           'next_run_at': '2026-10-05T07:00:00+00:00', 'deliver': 'none'}
    other = {'id': 'unrelated', 'script_sha256': 'b' * 64, 'enabled': False}
    registry = hermes / 'cron/jobs.json'
    dump(registry, {'jobs': [job, other]})
    paths = tuple(sorted(migration.COHORT))
    pins = {p: sha((candidate / p).read_bytes()) for p in paths}
    proposal = {'source': source, 'kind': 'discovery', 'paths': list(paths), 'hashes': pins,
                'merge_proof': {'merge_sha': source}, 'tier': 'A', 'prepared_by': 'author',
                'root': str(root), 'hermes': str(hermes), 'jobs': {JID: copy.deepcopy(job)},
                'before': {str(p): fingerprint(p) for p in
                           [*(root / p for p in paths), root / syncer.STATE, root / syncer.MANIFEST, installed]}}
    generation = GitGeneration(source, 'discovery', paths, pins, proposal['merge_proof'], proposal)
    proposal['discovery_plan'] = migration.preview_generation(candidate, root, generation)
    authority = {'owner': 'owner', 'writer_exclusion': 'Disposable fixture, no consumers.',
                 'proposal_sha256': json_sha(proposal)}
    review = {'schema': 'glp-ship-review-v1', 'decision': 'ACCEPT_EXACT_PROPOSAL',
              'prepared_by': 'author', 'reviewer': 'independent',
              'proposal_sha256': json_sha(proposal), 'adoption_authority': authority}
    scheduler = PortableScheduler(registry)
    output = tmp_path / 'output'
    output.mkdir()
    return candidate, root, hermes, generation, review, output, scheduler


def installer():
    assert importlib.util.find_spec('glp_ship_discovery') is not None, 'F01 wrapper installer missing'
    return importlib.import_module('glp_ship_discovery')


def test_installs_exact_committed_wrapper_and_only_native_hash(install_fixture):
    candidate, root, hermes, generation, review, output, scheduler = install_fixture
    prior = copy.deepcopy(scheduler.load_jobs())
    result = installer().apply(*install_fixture)
    assert (root / REL).read_bytes() == (candidate / REL).read_bytes()
    assert (hermes / 'scripts' / WRAPPER).read_bytes() == (candidate / SNAPSHOT).read_bytes()
    expected = copy.deepcopy(prior)
    expected[0]['script_sha256'] = sha((candidate / SNAPSHOT).read_bytes())
    assert scheduler.load_jobs() == expected
    assert result['status'] == 'applied'


def test_native_save_lost_ack_keeps_wrapper_sealed_without_retry(install_fixture, monkeypatch):
    *_, scheduler = install_fixture
    save = scheduler._save_jobs_unlocked
    calls = []
    def lost_ack(rows):
        calls.append(copy.deepcopy(rows))
        save(rows)
        raise RuntimeError('lost acknowledgement')
    monkeypatch.setattr(scheduler, '_save_jobs_unlocked', lost_ack)
    with pytest.raises(RuntimeError, match='lost acknowledgement'):
        installer().apply(*install_fixture)
    _, root, hermes, generation, _, output, _ = install_fixture
    assert len(calls) == 1
    assert (hermes / 'scripts' / WRAPPER).stat().st_mode & 0o777 == 0
    journal = json.loads((output / 'discovery-install.json').read_text())
    assert journal['state'] == 'reconciliation_required'
    assert journal['source_switch']['state'] == 'commit_uncertain'
    assert journal['source_switch']['commit_started'] is True
    assert scheduler.load_jobs()[0]['script_sha256'] == generation.hashes[SNAPSHOT]


def test_partial_source_failure_seals_wrapper_before_fire_fence_release(install_fixture, monkeypatch):
    module = installer()
    actual = module.syncer._sync_locked
    def fail_after_sources(*args, **kwargs):
        value = actual(*args, **kwargs)
        if args[2]:
            raise RuntimeError('source acknowledgement lost')
        return value
    monkeypatch.setattr(module.syncer, '_sync_locked', fail_after_sources)
    with pytest.raises(RuntimeError, match='source acknowledgement lost'):
        module.apply(*install_fixture)
    _, root, hermes, _, _, output, scheduler = install_fixture
    assert (hermes / 'scripts' / WRAPPER).stat().st_mode & 0o777 == 0
    assert json.loads((output / 'discovery-install.json').read_text())['state'] == 'reconciliation_required'
    assert sha((root / REL).read_bytes()) != BEFORE
    assert scheduler.load_jobs()[0]['script_sha256'] != sha((install_fixture[0] / SNAPSHOT).read_bytes())


def test_wrapper_unknown_predecessor_is_not_blessed_by_review(install_fixture):
    candidate, root, hermes, generation, review, output, scheduler = install_fixture
    wrapper = hermes / 'scripts' / WRAPPER
    wrapper.write_bytes(b'unknown private wrapper edit\n')
    jobs = scheduler.load_jobs()
    jobs[0]['script_sha256'] = sha(wrapper.read_bytes())
    scheduler._save_jobs_unlocked(jobs)
    generation.proposal['jobs'][JID] = jobs[0]
    generation.proposal['before'][str(wrapper)] = fingerprint(wrapper)
    review['proposal_sha256'] = json_sha(generation.proposal)
    review['adoption_authority']['proposal_sha256'] = review['proposal_sha256']
    with pytest.raises(RuntimeError, match='unknown_wrapper_predecessor'):
        installer().apply(*install_fixture)
    assert sha((root / REL).read_bytes()) == BEFORE
    assert not (output / 'discovery-install.json').exists()


def test_native_pin_commit_retains_all_source_locks(install_fixture, inputs, monkeypatch):
    import fcntl
    scheduler = install_fixture[-1]
    save = scheduler._save_jobs_unlocked
    def checked_save(rows):
        for path in inputs[3]:
            with path.open('rb') as probe:
                with pytest.raises(BlockingIOError):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        save(rows)
    monkeypatch.setattr(scheduler, '_save_jobs_unlocked', checked_save)
    assert installer().apply(*install_fixture)['status'] == 'applied'


def test_existing_install_journal_refuses_replay(install_fixture):
    *_, output, scheduler = install_fixture
    (output / 'discovery-install.json').write_text('{}')
    with pytest.raises(RuntimeError, match='existing_journal_requires_reconciliation'):
        installer().apply(*install_fixture)
    assert sha((install_fixture[1] / REL).read_bytes()) == BEFORE


def test_missing_source_on_failure_is_held(install_fixture, monkeypatch):
    module = installer()
    actual = module.syncer._sync_locked
    def remove_then_fail(*args, **kwargs):
        if args[2]:
            (install_fixture[1] / REL).unlink()
            raise RuntimeError('source disappeared')
        return actual(*args, **kwargs)
    monkeypatch.setattr(module.syncer, '_sync_locked', remove_then_fail)
    with pytest.raises(RuntimeError, match='source disappeared'):
        module.apply(*install_fixture)
    assert (install_fixture[2] / 'scripts' / WRAPPER).stat().st_mode & 0o777 == 0


def test_wrapper_precommit_rollback_cannot_reopen_mismatched_sources(install_fixture, monkeypatch):
    module = installer()
    checks = 0
    def transient_reader(*args, **kwargs):
        nonlocal checks
        checks += 1
        return [{'pid': 99999, 'reason': 'fixture reader'}] if checks == 2 else []
    monkeypatch.setattr(module, 'process_readers', transient_reader)
    with pytest.raises(RuntimeError):
        module.apply(*install_fixture)
    assert (install_fixture[2] / 'scripts' / WRAPPER).stat().st_mode & 0o777 == 0


def test_committed_wrapper_must_pin_its_selected_source(install_fixture):
    from dataclasses import replace
    candidate, root, hermes, generation, review, output, scheduler = install_fixture
    body = (candidate / SNAPSHOT).read_bytes().replace(generation.hashes[REL].encode(), migration.AFTER.encode())
    (candidate / SNAPSHOT).write_bytes(body)
    manifest = json.loads((candidate / syncer.MANIFEST).read_text())
    manifest['files'][SNAPSHOT]['sha256'] = sha(body)
    dump(candidate / syncer.MANIFEST, manifest)
    subprocess.run(['git', '-C', str(candidate), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-qam', 'Stale committed wrapper'], check=True)
    source = subprocess.check_output(['git', '-C', str(candidate), 'rev-parse', 'HEAD'], text=True).strip()
    pins = dict(generation.hashes, **{SNAPSHOT: sha(body)})
    proof = {'merge_sha': source}
    generation = replace(generation, source=source, hashes=pins, proof=proof)
    generation.proposal.update(source=source, hashes=pins, merge_proof=proof)
    generation.proposal['discovery_plan'] = migration.preview_generation(candidate, root, generation)
    review['proposal_sha256'] = json_sha(generation.proposal)
    review['adoption_authority']['proposal_sha256'] = review['proposal_sha256']
    with pytest.raises(RuntimeError, match='wrapper_dependency_pin'):
        installer().apply(candidate, root, hermes, generation, review, output, scheduler)
    assert sha((root / REL).read_bytes()) == BEFORE


@pytest.mark.parametrize('known_predecessor', [True, False])
def test_existing_sibling_without_baseline_requires_git_history(install_fixture, known_predecessor):
    from dataclasses import replace
    candidate, root, hermes, generation, review, output, scheduler = install_fixture
    sibling = 'scripts/affiliate_monday_discovery_card.py'
    old_hash = generation.hashes[sibling]
    (candidate / sibling).write_bytes((candidate / sibling).read_bytes() + b'\n# Reviewed sibling successor\n')
    new_hash = sha((candidate / sibling).read_bytes())
    (candidate / SNAPSHOT).write_bytes((candidate / SNAPSHOT).read_bytes().replace(old_hash.encode(), new_hash.encode()))
    manifest = json.loads((candidate / syncer.MANIFEST).read_text())
    for rel in (sibling, SNAPSHOT):
        manifest['files'][rel]['sha256'] = sha((candidate / rel).read_bytes())
    dump(candidate / syncer.MANIFEST, manifest)
    subprocess.run(['git', '-C', str(candidate), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-qam', 'Sibling successor'], check=True)
    source = subprocess.check_output(['git', '-C', str(candidate), 'rev-parse', 'HEAD'], text=True).strip()
    state = json.loads((root / syncer.STATE).read_text())
    del state['hashes'][sibling]
    dump(root / syncer.STATE, state)
    if not known_predecessor:
        (root / sibling).write_bytes(b'unknown local sibling edit\n')
    pins = {path: sha((candidate / path).read_bytes()) for path in generation.paths}
    proof = {'merge_sha': source}
    generation = replace(generation, source=source, hashes=pins, proof=proof)
    generation.proposal.update(source=source, hashes=pins, merge_proof=proof)
    generation.proposal['before'] = {name: fingerprint(name) for name in generation.proposal['before']}
    generation.proposal['discovery_plan'] = migration.preview_generation(candidate, root, generation)
    review['proposal_sha256'] = json_sha(generation.proposal)
    review['adoption_authority']['proposal_sha256'] = review['proposal_sha256']
    if known_predecessor:
        assert installer().apply(candidate, root, hermes, generation, review, output, scheduler)['status'] == 'applied'
        assert (root / sibling).read_bytes() == (candidate / sibling).read_bytes()
        assert json.loads((root / syncer.STATE).read_text())['hashes'][sibling] == new_hash
    else:
        with pytest.raises(RuntimeError, match='source_preview_refused'):
            installer().apply(candidate, root, hermes, generation, review, output, scheduler)
        assert (root / sibling).read_bytes() == b'unknown local sibling edit\n'
        assert sha((root / REL).read_bytes()) == BEFORE
