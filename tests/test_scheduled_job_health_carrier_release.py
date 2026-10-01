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


@pytest.mark.parametrize('race', ['source', 'mode', 'record', 'lock'])
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
