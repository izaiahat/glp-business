"""Guarded F09 adoption in fixture homes only; never run the health job."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def producer():
    path = ROOT/'scripts/scheduled_job_health_carrier_release.py'
    assert path.exists(), 'missing registered guarded F09 carrier producer'
    sys.path.insert(0, str(ROOT/'scripts'))
    spec = importlib.util.spec_from_file_location('health_carrier_release', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture(tmp_path):
    p = producer()
    repo, business, hermes = [tmp_path/n for n in ('repo', 'business', 'hermes')]
    for root in (repo, business, hermes):
        root.mkdir()
    for name in p.CODE | p.SOURCES | {'ops/registered-producers.json'}:
        target = repo/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT/name).read_bytes())
        if name in p.SOURCES:
            target = business/name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT/name).read_bytes())
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), '-c', 'user.name=Fixture',
            '-c', 'user.email=fixture@example.invalid', *args], text=True).strip()
    git('init', '-q'); git('add', '.'); git('commit', '-qm', 'fixture accepted source')
    head = git('rev-parse', 'HEAD')
    old = b'fixture predecessor carrier; never execute\n'
    # Path/known-preimage injection is fixture-only, not a CLI option.
    p.PREDECESSOR_SHA256 = p.sha_bytes(old)
    carrier = hermes/'scripts'/p.CARRIER
    carrier.parent.mkdir(); carrier.write_bytes(old); carrier.chmod(0o750)
    registry = hermes/'cron/jobs.json'; registry.parent.mkdir()
    job = {'id':p.JOB_ID, 'script':p.CARRIER, 'script_sha256':p.sha_bytes(old),
           'no_agent':True, 'enabled':True, 'state':'scheduled',
           'schedule':{'display':'every 15m', 'kind':'interval', 'minutes':15},
           'next_run_at':'2026-10-01T04:00:00Z', 'fire_claim':None}
    registry.write_text(json.dumps({'jobs':[job, {'id':'other', 'enabled':False}], 'version':1}))
    scheduler = SimpleNamespace(_current_cron_store=lambda:SimpleNamespace(jobs_file=registry),
        _jobs_file_lock=threading.RLock(), _jobs_lock_file=lambda:registry.parent/'.jobs.lock',
        _record_load_stamp=lambda _:None)
    def save(jobs):
        doc = json.loads(registry.read_text()); doc['jobs']=jobs
        registry.write_text(json.dumps(doc))
    scheduler._save_jobs_unlocked=save
    review = {'schema':'scheduled-job-health-carrier-review-v1', 'decision':'REVIEW_ONLY',
        'reviewer':'independent fixture reviewer', 'author':'fixture author',
        'reviewed_at_utc':'2026-01-01T00:00:00Z', 'evidence':['fixture only'],
        'producer_head':head, 'source_head':head,
        'source_sha256':{n:p.sha_bytes((business/n).read_bytes()) for n in p.SOURCES},
        'source_mode':{n:(business/n).stat().st_mode & 0o7777 for n in p.SOURCES},
        'predecessor_sha256':p.sha_bytes(old), 'predecessor_mode':0o750,
        'expected_job':copy.deepcopy(job),
        'writer_exclusion':'fixture single writer; no direct carrier callers'}
    receipt=tmp_path/'review.json'; receipt.write_text(json.dumps(review))
    return p, repo, business, hermes, scheduler, receipt, review


def accept(f):
    p, repo, business, hermes, scheduler, receipt, review = f
    plan=p.release(receipt, repo, business=business, hermes=hermes)
    review.update(decision='ACCEPT_EXACT_CURRENT_BYTES', proposed_sha256=plan['proposed_sha256'])
    receipt.write_text(json.dumps(review))
    return plan


def test_combined_dependency_preview_refuses_unbound_bytes(tmp_path, monkeypatch):
    """The real dependency_preview_probe.py counterexample, scratch-only.

    Keep genuine committed candidate bytes; never import/run the health manager,
    budget or transport. No live acceptance or apply is made by this regression.
    """
    f = fixture(tmp_path)
    p, repo, business, hermes, scheduler, receipt, review = f
    dependencies = ('ops/n8n_execution_budget.py', 'ops/n8n_dt.py')
    # Let the old three-source producer reach the genuine omission boundary.
    for name in dependencies:
        path = business/name
        if not path.exists():
            path.write_bytes((ROOT/name).read_bytes())
    targets = (hermes/'scripts'/p.CARRIER, hermes/'cron/jobs.json')
    before = {path:(path.read_bytes(), path.stat().st_mtime_ns,
                    path.stat().st_mode) for path in targets}
    monkeypatch.setattr(p, 'preserve_custody', lambda *a:pytest.fail('preview made custody'))
    monkeypatch.setattr(p, 'atomic_write', lambda *a:pytest.fail('preview wrote output'))
    monkeypatch.setattr(scheduler, '_save_jobs_unlocked', lambda *a:pytest.fail('preview saved'))
    healthy = p.release(receipt, repo, business=business, hermes=hermes)
    assert healthy['proposed_sha256'] == p.sha_bytes((ROOT/p.SNAPSHOT).read_bytes())
    assert healthy['applied'] is False and healthy['jobs_executed'] == 0
    for name in dependencies:
        path = business/name
        body, mode = path.read_bytes(), path.stat().st_mode & 0o7777
        for mutation in ('missing', 'incompatible', 'forged_hash', 'omitted_hash',
                         'mode', 'omitted_mode', 'extra_hash'):
            bad = copy.deepcopy(review)
            if mutation == 'missing':
                path.unlink()
            elif mutation in ('incompatible', 'forged_hash'):
                path.write_bytes(b'fixture deliberately incompatible; do not execute\n')
                if mutation == 'forged_hash':
                    bad['source_sha256'][name] = p.sha_bytes(path.read_bytes())
            elif mutation == 'omitted_hash':
                bad['source_sha256'].pop(name, None)
            elif mutation == 'mode':
                path.chmod(0o700)
            elif mutation == 'omitted_mode':
                bad['source_mode'].pop(name, None)
            elif mutation == 'extra_hash':
                bad['source_sha256']['ops/arbitrary.py'] = '0'*64
            receipt.write_text(json.dumps(bad))
            with pytest.raises((RuntimeError, OSError)):
                p.release(receipt, repo, business=business, hermes=hermes)
            assert before == {target:(target.read_bytes(), target.stat().st_mtime_ns,
                                      target.stat().st_mode) for target in targets}
            assert not (business/'reports').exists()
            path.write_bytes(body); path.chmod(mode)
        receipt.write_text(json.dumps(review))
        assert p.release(receipt, repo, business=business, hermes=hermes) == healthy
    assert p.SOURCES == {p.SNAPSHOT, 'scripts/scheduled_source_drift.py',
                         'ops/scheduled-source-drift-manifest.json', *dependencies}
    inventory = json.loads((ROOT/'ops/scheduled-source-drift-manifest.json').read_bytes())
    row = next(row for row in inventory['jobs'] if row['id'] == p.JOB_ID)
    assert row['reviewed_sha256'] == healthy['proposed_sha256']
    assert {payload['path']:payload['sha256'] for payload in row['payloads']} == {
        str(p.BUSINESS/name):p.sha_bytes((ROOT/name).read_bytes()) for name in dependencies}
    assert all(payload['owner'] == str(p.HERMES/'scripts'/p.CARRIER)
               for payload in row['payloads'])


def test_preview_and_apply_only_carrier_and_target_hash_with_custody(tmp_path):
    f=fixture(tmp_path)
    p, repo, business, hermes, scheduler, receipt, review=f
    carrier=hermes/'scripts'/p.CARRIER; registry=hermes/'cron/jobs.json'
    before={path:(path.read_bytes(), path.stat().st_mtime_ns) for path in (carrier, registry)}
    preview=accept(f)
    assert before=={path:(path.read_bytes(), path.stat().st_mtime_ns) for path in before}
    assert not (business/'reports').exists()
    result=p.release(receipt, repo, business=business, hermes=hermes, apply=True, scheduler=scheduler)
    assert result['applied']
    assert carrier.read_bytes()==(repo/p.SNAPSHOT).read_bytes()
    assert carrier.stat().st_mode & 0o777==0o750
    expected=json.loads(before[registry][0]); expected['jobs'][0]['script_sha256']=preview['proposed_sha256']
    assert json.loads(registry.read_bytes())==expected
    for path,(body,_) in before.items():
        assert (Path(result['custody'])/(p.sha_bytes(body)+'.bytes')).read_bytes()==body


@pytest.mark.parametrize('mutation', ['source', 'carrier', 'mode', 'symlink', 'head', 'emitted',
    'self_review', 'review_only', 'record', 'hash', 'binding', 'duplicate_job',
    'run_claim', 'fire_claim', 'running', 'no_agent', 'schedule'])
def test_refusals_write_nothing(tmp_path, mutation, monkeypatch):
    f=fixture(tmp_path)
    p, repo, business, hermes, scheduler, receipt, review=f
    accept(f)
    carrier=hermes/'scripts'/p.CARRIER; registry=hermes/'cron/jobs.json'
    doc=json.loads(registry.read_bytes()); job=doc['jobs'][0]
    if mutation=='source': (business/'scripts/scheduled_source_drift.py').write_bytes(b'changed')
    elif mutation=='carrier': carrier.write_bytes(b'changed')
    elif mutation=='mode': carrier.chmod(0o700)
    elif mutation=='symlink': carrier.rename(carrier.with_suffix('.old')); carrier.symlink_to(carrier.with_suffix('.old'))
    elif mutation=='head': review['producer_head']='0'*40
    elif mutation=='emitted': review['proposed_sha256']='0'*64
    elif mutation=='self_review': review['reviewer']=review['author']
    elif mutation=='review_only': review['decision']='REVIEW_ONLY'
    elif mutation=='record': job['next_run_at']='changed'
    elif mutation=='duplicate_job': doc['jobs'].append(copy.deepcopy(job))
    else:
        field={'hash':'script_sha256', 'binding':'script'}.get(mutation,mutation)
        job[field]={'by':'fixture'} if mutation in {'run_claim','fire_claim'} else (
            True if mutation=='running' else False if mutation=='no_agent' else 'changed')
        review['expected_job']=copy.deepcopy(job)  # matching receipt cannot bless unsafe identity/claim
    registry.write_text(json.dumps(doc)); receipt.write_text(json.dumps(review))
    before={path:(path.read_bytes(),path.stat().st_mtime_ns) for path in (carrier,registry)}
    monkeypatch.setattr(scheduler, '_save_jobs_unlocked', lambda _:pytest.fail('refusal called saver'))
    with pytest.raises((RuntimeError,OSError)):
        p.release(receipt,repo,business=business,hermes=hermes,apply=True,scheduler=scheduler)
    assert before=={path:(path.read_bytes(),path.stat().st_mtime_ns) for path in before}
    assert not (business/'reports').exists()


@pytest.mark.parametrize('race', ['source', 'mode', 'record', 'lock',
    'budget_bytes', 'budget_mode', 'transport_bytes', 'transport_mode'])
def test_intervening_changes_refuse_before_custody(tmp_path, race, monkeypatch):
    f=fixture(tmp_path)
    p, repo, business, hermes, scheduler, receipt, review=f
    accept(f)
    carrier=hermes/'scripts'/p.CARRIER; registry=hermes/'cron/jobs.json'
    before=carrier.read_bytes(); original=p.checked_plan
    def moved(*args):
        plan=original(*args)
        if race=='source': (business/'scripts/scheduled_source_drift.py').write_bytes(b'moved')
        elif race=='mode': carrier.chmod(0o700)
        elif race=='record': registry.write_bytes(b'{"jobs":[]}')
        elif race.startswith(('budget_', 'transport_')):
            dependency = business/('ops/n8n_execution_budget.py' if race.startswith('budget_')
                                   else 'ops/n8n_dt.py')
            if race.endswith('_bytes'): dependency.write_bytes(b'moved')
            else: dependency.chmod(0o700)
        return plan
    monkeypatch.setattr(p, 'checked_plan', moved)
    import contextlib, fcntl
    with contextlib.ExitStack() as stack:
        if race=='lock':
            lock=stack.enter_context(scheduler._jobs_lock_file().open('a+'))
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises((RuntimeError,BlockingIOError)):
            p.release(receipt,repo,business=business,hermes=hermes,apply=True,scheduler=scheduler)
    assert carrier.read_bytes()==before and not (business/'reports').exists()


@pytest.mark.parametrize('landed', [False,True])
def test_saver_failure_reconciles_without_replay(tmp_path, landed):
    f=fixture(tmp_path)
    p, repo, business, hermes, scheduler, receipt, review=f
    accept(f); carrier=hermes/'scripts'/p.CARRIER; old=carrier.read_bytes()
    save=scheduler._save_jobs_unlocked; calls=[]
    def fail(jobs):
        calls.append('save')
        if landed: save(jobs)
        raise RuntimeError('injected saver failure')
    scheduler._save_jobs_unlocked=fail
    with pytest.raises(RuntimeError,match='injected saver failure'):
        p.release(receipt,repo,business=business,hermes=hermes,apply=True,scheduler=scheduler)
    assert carrier.read_bytes()==((repo/p.SNAPSHOT).read_bytes() if landed else old)
    assert not list((business/'reports').rglob('acceptance.json'))
    if landed:
        with pytest.raises(RuntimeError):
            p.release(receipt,repo,business=business,hermes=hermes,apply=True,scheduler=scheduler)
        assert calls==['save']


def test_postrename_failure_restores_only_owned_bytes(tmp_path, monkeypatch):
    f=fixture(tmp_path)
    p, repo, business, hermes, scheduler, receipt, review=f
    accept(f); carrier=hermes/'scripts'/p.CARRIER; old=carrier.read_bytes()
    original=p.atomic_write; failed=False
    def fail(path,body,mode):
        nonlocal failed
        original(path,body,mode)
        if path==carrier and not failed:
            failed=True
            raise OSError('postrename failure')
    monkeypatch.setattr(p,'atomic_write',fail)
    with pytest.raises(OSError,match='postrename failure'):
        p.release(receipt,repo,business=business,hermes=hermes,apply=True,scheduler=scheduler)
    assert carrier.read_bytes()==old
