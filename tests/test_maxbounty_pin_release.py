"""Only isolated files/registrations; no portal, scheduler or live adoption."""
import hashlib
import importlib.util
import json
import subprocess
import sys
import copy
import threading
from types import SimpleNamespace
import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUSINESS = '/home/ubuntu/business'
MONITOR = 'scripts/glp_maxbounty_capacity_monitor.py'
FRESH = 'scripts/glp_maxbounty_fresh_probe.py'
PINS = 'scripts/glp_maxbounty_capacity_monitor.pins.json'
WRAPPER = 'ops/hermes-cron-scripts/glp_maxbounty_capacity_resume.sh'


def sha(body):
    return hashlib.sha256(body).hexdigest()


def producer():
    path = ROOT / 'scripts/maxbounty_pin_release.py'
    assert path.exists(), 'missing narrow JSON/inline-wrapper pin producer'
    spec = importlib.util.spec_from_file_location('maxbounty_pin_release', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_real_offline_caller_accepts_complete_emitted_closure(tmp_path):
    p = producer()
    old = (ROOT / 'ops/maxbounty-pin-predecessor.json').read_bytes()
    wrapper = (ROOT / WRAPPER).read_bytes()
    sources = {name: sha((ROOT / name).read_bytes()) for name in (MONITOR, FRESH)}
    pins, shell = p.stage_pins(old, wrapper, sources)
    before, after = json.loads(old)['files'], json.loads(pins)['files']
    assert len(after) == 10
    assert {k:v for k,v in after.items() if k not in {BUSINESS+'/'+s for s in sources}} == {
        k:v for k,v in before.items() if k != BUSINESS+'/'+MONITOR}
    assert after[BUSINESS+'/'+FRESH] == sources[FRESH]
    assert sha(pins).encode() in shell and sources[MONITOR].encode() in shell
    # Execute the unchanged monitor and shell logic with ONLY paths relocated.
    # Dependencies are inert fixture bytes: this gate hashes, never imports them.
    mapped = {}
    for path, digest in after.items():
        relative = Path(path).relative_to(BUSINESS)
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        body = (ROOT / relative).read_bytes() if str(relative) in sources else b'fixture dependency\n'
        target.write_bytes(body)
        mapped[str(target)] = sha(body)
    manifest = tmp_path / PINS
    manifest.write_text(json.dumps({'schema': json.loads(pins)['schema'], 'files': mapped}))
    # Re-emit through the producer for fixture dependency and relocated pins hash.
    fixture_old = json.loads(old)
    fixture_old['files'] = {str(Path(BUSINESS)/Path(k).relative_to(tmp_path)):v for k,v in mapped.items()
                            if Path(k).name != Path(FRESH).name}
    fixture_old['files'][BUSINESS+'/'+MONITOR] = before[BUSINESS+'/'+MONITOR]
    original_shell = wrapper.replace(BUSINESS.encode(), str(tmp_path).encode())
    original_shell = original_shell.replace(b'/home/ubuntu/.hermes/venvs/glp-awin-runtime-r2/bin/python', sys.executable.encode())
    fixture_manifest = json.dumps({'schema': json.loads(old)['schema'], 'files': {
        str(tmp_path/Path(k).relative_to(BUSINESS)):v for k,v in fixture_old['files'].items()}}).encode()
    # Replace only the predecessor manifest hash to reflect path-only fixture setup.
    original_shell = original_shell.replace(sha(old).encode(), sha(fixture_manifest).encode())
    live_wrapper = tmp_path/'resume.sh'
    live_wrapper.write_bytes(original_shell)
    manifest.write_bytes(fixture_manifest)
    refused = subprocess.run(['bash', str(live_wrapper), '--check-offline'], capture_output=True, text=True)
    assert refused.returncode == 2 and 'reviewed source pin mismatch' in refused.stdout
    emitted, emitted_shell = p.stage_pins(fixture_manifest, original_shell, sources, business=tmp_path)
    manifest.write_bytes(emitted); live_wrapper.write_bytes(emitted_shell)
    accepted = subprocess.run(['bash', str(live_wrapper), '--check-offline'], capture_output=True, text=True)
    assert accepted.returncode == 0, accepted.stderr + accepted.stdout
    assert accepted.stdout.strip() == 'PASS_OFFLINE_SOURCE_PINS_NO_NETWORK'
    spec = importlib.util.spec_from_file_location('fixture_monitor', tmp_path/MONITOR)
    monitor = importlib.util.module_from_spec(spec); spec.loader.exec_module(monitor)
    assert len(monitor._fresh_source_identity(manifest)) == 64
    missing = json.loads(emitted); del missing['files'][str(tmp_path/FRESH)]
    manifest.write_text(json.dumps(missing))
    import pytest
    with pytest.raises(monitor.Refusal, match='FRESH_PRODUCER_SOURCE_PIN_MISSING'):
        monitor._fresh_source_identity(manifest)


def release_fixture(tmp_path):
    p = producer()
    assert hasattr(p, 'release'), 'missing reviewed adoption transaction'
    repo, business, hermes = [tmp_path/name for name in ('repo', 'business', 'hermes')]
    for root in (repo, business, hermes):
        root.mkdir()
    baseline = json.loads((ROOT/'ops/maxbounty-pin-predecessor.json').read_text())
    for name in p.DEPENDENCIES | p.SOURCES:
        body = (ROOT/name).read_bytes() if name in p.SOURCES else b'fixture dependency\n'
        target = business/name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(body)
        if name in p.DEPENDENCIES:
            baseline['files'][BUSINESS+'/'+name] = sha(body)
        if name in p.SOURCES:
            target = repo/name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(body)
    old = p.json_bytes(baseline)
    wrapper = (ROOT/WRAPPER).read_bytes().replace(
        sha((ROOT/'ops/maxbounty-pin-predecessor.json').read_bytes()).encode(), sha(old).encode())
    for name, body in [('ops/maxbounty-pin-predecessor.json', old), (WRAPPER, wrapper),
                       ('scripts/maxbounty_pin_release.py', (ROOT/'scripts/maxbounty_pin_release.py').read_bytes())]:
        target = repo/name; target.parent.mkdir(parents=True, exist_ok=True); target.write_bytes(body)
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=Fixture',
                                        '-c', 'user.email=fixture@example.invalid', *args], text=True).strip()
    git('init', '-q'); git('add', '.'); git('commit', '-qm', 'fixture reviewed source and predecessor')
    head = git('rev-parse', 'HEAD')
    mapped = p.relocate(old, business, hermes)
    manifest = business/PINS; manifest.write_bytes(mapped)
    shell = p.relocate(wrapper, business, hermes).replace(sha(old).encode(), sha(mapped).encode())
    installed = hermes/'scripts'/p.WRAPPER; installed.parent.mkdir(); installed.write_bytes(shell)
    installed.chmod(0o750)
    job = {'id': p.JOB_ID, 'script': p.WRAPPER, 'script_sha256': sha(shell), 'enabled': False,
           'state': 'paused', 'paused_reason': 'migration hold', 'paused_at': '2026-09-01',
           'schedule': {'expr': '5 3 * * *'}, 'next_run_at': '2026-09-28T03:05:00Z', 'last_status': 'error'}
    registry = hermes/'cron/jobs.json'; registry.parent.mkdir()
    registry.write_text(json.dumps({'jobs': [job, {'id': 'other', 'enabled': True}], 'version': 1}))
    scheduler = SimpleNamespace(
        _current_cron_store=lambda: SimpleNamespace(jobs_file=registry),
        _jobs_file_lock=threading.RLock(), _jobs_lock_file=lambda: registry.parent/'.jobs.lock',
        _record_load_stamp=lambda value: None)
    def save(jobs):
        doc = json.loads(registry.read_text()); doc['jobs'] = jobs
        registry.write_text(json.dumps(doc))
    scheduler._save_jobs_unlocked = save
    review = {'schema': 'maxbounty-pin-review-v1', 'decision': 'REVIEW_ONLY',
              'reviewer': 'isolated fixture only', 'reviewed_at_utc': '2026-01-01T00:00:00Z',
              'evidence': ['fixture, not live authority'], 'producer_head': head, 'source_head': head,
              'source_sha256': {n: sha((business/n).read_bytes()) for n in p.SOURCES},
              'predecessor_sha256': {'manifest': sha(mapped), 'wrapper': sha(shell)},
              'expected_job': copy.deepcopy(job), 'writer_exclusion': 'fixture has one owner'}
    receipt = tmp_path/'review.json'; receipt.write_text(json.dumps(review))
    return p, repo, business, hermes, scheduler, receipt, review


def accept(f):
    p, repo, business, hermes, scheduler, receipt, review = f
    plan = p.release(receipt, repo, business=business, hermes=hermes)
    review.update(decision='ACCEPT_EXACT_CURRENT_BYTES', proposed_sha256=plan['proposed_sha256'])
    receipt.write_text(json.dumps(review))
    return plan


def test_release_transaction_preserves_paused_record_and_archives_preimages(tmp_path):
    f = release_fixture(tmp_path)
    p, repo, business, hermes, scheduler, receipt, review = f
    assert hasattr(p, 'release'), 'missing reviewed adoption transaction'
    paths = [business/PINS, hermes/'scripts'/p.WRAPPER, hermes/'cron/jobs.json']
    before = {path: path.read_bytes() for path in paths}
    preview = accept(f)
    assert len(preview['artifact_diffs']) == 2
    assert all(path.read_bytes() == body for path, body in before.items())
    result = p.release(receipt, repo, business=business, hermes=hermes, apply=True, scheduler=scheduler)
    assert result['applied'] and result['pause_state_preserved']
    expected = json.loads(before[paths[-1]])
    expected['jobs'][0]['script_sha256'] = preview['proposed_sha256']['wrapper']
    assert json.loads(paths[-1].read_bytes()) == expected
    assert paths[1].stat().st_mode & 0o777 == 0o750
    for path, body in before.items():
        assert (Path(result['custody'])/(sha(body)+'.bytes')).read_bytes() == body


@pytest.mark.parametrize('mutation', [
    'monitor', 'fresh', 'dependency', 'manifest', 'wrapper', 'mode', 'symlink',
    'missing_fresh_review', 'source_hash', 'head', 'unreviewed_output', 'review_only',
    'enabled', 'scheduled', 'script_path', 'wrong_id', 'duplicate_job', 'schedule',
    'pause_reason', 'next_run', 'claim', 'registration_hash', 'duplicate_json'])
def test_conflicts_refuse_without_artifact_or_registry_writes(tmp_path, mutation):
    f = release_fixture(tmp_path)
    p, repo, business, hermes, scheduler, receipt, review = f
    accept(f)
    registry = hermes/'cron/jobs.json'
    doc = json.loads(registry.read_bytes())
    job = doc['jobs'][0]
    if mutation in {'monitor', 'fresh', 'dependency', 'manifest', 'wrapper'}:
        target = {'monitor': business/MONITOR, 'fresh': business/FRESH,
                  'dependency': business/'ops/n8n_dt.py', 'manifest': business/PINS,
                  'wrapper': hermes/'scripts'/p.WRAPPER}[mutation]
        target.write_bytes(target.read_bytes()+b'\n')
    elif mutation == 'symlink':
        target = business/FRESH; moved = business/'fresh-moved'; target.rename(moved); target.symlink_to(moved)
    elif mutation == 'mode':
        # Changed wrapper mode during planning is covered by the race test below.
        review['predecessor_sha256']['wrapper'] = '0'*64
    elif mutation == 'missing_fresh_review':
        del review['source_sha256'][FRESH]
    elif mutation == 'source_hash': review['source_sha256'][FRESH] = '0'*64
    elif mutation == 'head': review['producer_head'] = '0'*40
    elif mutation == 'unreviewed_output': review['proposed_sha256']['manifest'] = '0'*64
    elif mutation == 'review_only': review['decision'] = 'REVIEW_ONLY'
    elif mutation == 'duplicate_job': doc['jobs'].append(copy.deepcopy(job))
    elif mutation == 'enabled': job['enabled'] = True; review['expected_job'] = copy.deepcopy(job)
    elif mutation == 'scheduled': job['state'] = 'scheduled'; review['expected_job'] = copy.deepcopy(job)
    elif mutation == 'script_path': job['script'] = 'other/'+p.WRAPPER; review['expected_job'] = copy.deepcopy(job)
    elif mutation == 'wrong_id': job['id'] = 'not-the-target'
    elif mutation == 'schedule': job['schedule']['expr'] = '1 2 * * *'
    elif mutation == 'pause_reason': job['paused_reason'] = 'changed'
    elif mutation == 'next_run': job['next_run_at'] = None
    elif mutation == 'claim': job['fire_claim'] = {'by': 'someone'}; review['expected_job'] = copy.deepcopy(job)
    elif mutation == 'registration_hash': job['script_sha256'] = '0'*64; review['expected_job'] = copy.deepcopy(job)
    registry.write_text(json.dumps(doc))
    receipt.write_text(json.dumps(review))
    if mutation == 'duplicate_json':
        receipt.write_text(receipt.read_text().replace('{', '{"schema":"duplicate",', 1))
    paths = [business/PINS, hermes/'scripts'/p.WRAPPER, registry]
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
    with pytest.raises((RuntimeError, OSError)):
        p.release(receipt, repo, business=business, hermes=hermes, apply=True, scheduler=scheduler)
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths} == before
    assert not (business/'reports').exists()


@pytest.mark.parametrize('race', ['source', 'wrapper_mode', 'record', 'lock'])
def test_intervening_mutations_and_lock_contention_fail_closed(tmp_path, race, monkeypatch):
    f = release_fixture(tmp_path)
    p, repo, business, hermes, scheduler, receipt, review = f
    accept(f)
    paths = [business/PINS, hermes/'scripts'/p.WRAPPER, hermes/'cron/jobs.json']
    baseline = {path: path.read_bytes() for path in paths}
    original = p.checked_plan
    def moved(*args):
        plan = original(*args)
        if race == 'source': (business/FRESH).write_bytes(b'changed')
        elif race == 'wrapper_mode': paths[1].chmod(0o700)
        elif race == 'record': paths[-1].write_bytes(b'{"jobs":[]}')
        return plan
    monkeypatch.setattr(p, 'checked_plan', moved)
    import contextlib, fcntl
    with contextlib.ExitStack() as stack:
        if race == 'lock':
            lock = stack.enter_context(scheduler._jobs_lock_file().open('a+'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises((RuntimeError, BlockingIOError)):
            p.release(receipt, repo, business=business, hermes=hermes, apply=True, scheduler=scheduler)
    for path in paths[:-1]: assert path.read_bytes() == baseline[path]
    assert paths[-1].read_bytes() == (b'{"jobs":[]}' if race == 'record' else baseline[paths[-1]])
    assert not (business/'reports').exists()


@pytest.mark.parametrize('landed', [False, True])
def test_saver_failure_rolls_back_only_when_registration_did_not_land(tmp_path, landed):
    f = release_fixture(tmp_path)
    p, repo, business, hermes, scheduler, receipt, review = f
    preview = accept(f)
    before = {path: path.read_bytes() for path in [business/PINS, hermes/'scripts'/p.WRAPPER]}
    save = scheduler._save_jobs_unlocked
    def fail(jobs):
        if landed: save(jobs)
        raise RuntimeError('injected saver failure')
    scheduler._save_jobs_unlocked = fail
    with pytest.raises(RuntimeError, match='injected saver failure'):
        p.release(receipt, repo, business=business, hermes=hermes, apply=True, scheduler=scheduler)
    for path, body in before.items():
        if not landed: assert path.read_bytes() == body
        else: assert sha(path.read_bytes()) in preview['proposed_sha256'].values()
    assert not list((business/'reports').rglob('acceptance.json'))


@pytest.mark.parametrize('mutation', ['helper', 'extra', 'duplicate', 'shell_duplicate', 'shell_malformed'])
def test_emitter_refuses_incomplete_or_ambiguous_pin_conventions(mutation):
    p = producer()
    before = (ROOT/'ops/maxbounty-pin-predecessor.json').read_bytes()
    wrapper = (ROOT/WRAPPER).read_bytes()
    sources = {name: sha((ROOT/name).read_bytes()) for name in (MONITOR, FRESH)}
    if mutation == 'helper': del sources[FRESH]
    if mutation == 'extra':
        doc = json.loads(before); doc['files']['/unknown'] = '0'*64; before = json.dumps(doc).encode()
    if mutation == 'duplicate': before = before.replace(b'"schema":', b'"schema":"duplicate", "schema":')
    if mutation == 'shell_duplicate': wrapper += wrapper.splitlines()[10]+b'\n'
    if mutation == 'shell_malformed': wrapper = wrapper.replace(b'== "441e', b"== '441e")
    with pytest.raises(RuntimeError): p.stage_pins(before, wrapper, sources)


def test_real_installed_scheduler_saver_in_isolated_home(tmp_path):
    scheduler_source = Path('/home/ubuntu/.hermes/hermes-agent/cron/jobs.py')
    if not scheduler_source.exists():
        pytest.skip('host-only installed scheduler integration; portable cases run above')
    f = release_fixture(tmp_path)
    p, repo, business, hermes, scheduler, receipt, review = f
    accept(f)
    # New interpreter with HOME store selected before any cron imports.
    code = '''import sys, json
from pathlib import Path
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
from cron import jobs
import maxbounty_pin_release as p
home=Path(sys.argv[3]); business=Path(sys.argv[4])
assert jobs._current_cron_store().jobs_file == home/'cron/jobs.json'
before=json.loads((home/'cron/jobs.json').read_text())['jobs']
r=p.release(Path(sys.argv[5]), Path(sys.argv[6]), business=business, hermes=home, apply=True, scheduler=jobs)
after=json.loads((home/'cron/jobs.json').read_text())['jobs']
before[0]['script_sha256']=r['proposed_sha256']['wrapper']
assert after == before
print('PASS_REAL_SCHEDULER_SAVER_HASH_ONLY')
'''
    import os
    env = dict(os.environ, HERMES_HOME=str(hermes), PYTHONDONTWRITEBYTECODE='1')
    run = subprocess.run([sys.executable, '-B', '-c', code, str(scheduler_source.parents[1]),
        str(ROOT/'scripts'), str(hermes), str(business), str(receipt), str(repo)],
        env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr + run.stdout
    assert 'PASS_REAL_SCHEDULER_SAVER_HASH_ONLY' in run.stdout


def test_postrename_failure_restores_all_written_artifacts(tmp_path, monkeypatch):
    f = release_fixture(tmp_path)
    p, repo, business, hermes, scheduler, receipt, review = f
    accept(f)
    target = business/PINS
    original = p.atomic_write
    before = target.read_bytes()
    failed = False
    def fail_after_rename(path, body, mode):
        nonlocal failed
        original(path, body, mode)
        if path == target and not failed:
            failed = True
            raise OSError('injected post-rename fsync failure')
    monkeypatch.setattr(p, 'atomic_write', fail_after_rename)
    with pytest.raises(OSError, match='post-rename'):
        p.release(receipt, repo, business=business, hermes=hermes, apply=True, scheduler=scheduler)
    assert target.read_bytes() == before


def test_custody_metadata_is_stable_across_iteration_order(tmp_path):
    p = producer()
    observed = {tmp_path/'b': (b'b', 0o600), tmp_path/'a': (b'a', 0o644)}
    root = tmp_path/'archive'
    p.preserve_custody(root, observed, {})
    p.preserve_custody(root, dict(reversed(list(observed.items()))), {})
