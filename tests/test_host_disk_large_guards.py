import json
import subprocess
from pathlib import Path
import time
import host_disk_cold_classes as m
import host_disk_large_classes as large
import host_disk_large_sweep as sweep
from host_disk_reference_guard import ReferenceGuardAPI
import pytest


@pytest.fixture(autouse=True)
def isolated_scheduler_custody(monkeypatch):
    import host_disk_custody
    monkeypatch.setattr(host_disk_custody, 'collect', lambda: set())


class API:
    def path_is_active_privileged(self,p):return False
    def remove_disposable(self,p,deadline):
        import shutil
        shutil.rmtree(p);return True


def test_reference_metadata_drift_vetoes_effects(tmp_path):
    source=tmp_path/'consumer.json';source.write_text('{}')
    guarded=ReferenceGuardAPI(API(),{str(source):m.signature(source)})
    assert not guarded.path_is_active_privileged(tmp_path/'candidate')
    source.write_text('{"new_reference":"candidate"}')
    assert guarded.path_is_active_privileged(tmp_path/'candidate')
    assert guarded.last_reference_veto==str(source)


def test_absolute_scratch_reference_in_large_txt_is_protected(tmp_path,monkeypatch):
    bus=tmp_path/'business'
    root=bus/'reports/ops/glp347-pallas-20261007';root.mkdir(parents=True)
    (bus/'ops').mkdir()
    (bus/'ops/disk-retention-policy.json').write_text('{"protected_roots":[]}')
    path='/home/ubuntu/.hermes/cache/scratch/r8-test-custody-absent'
    (root/'ACK.txt').write_text('x'*3_100_000+'\n'+path+'\n')
    monkeypatch.setattr(sweep,'BUS',bus)
    report=tmp_path/'report';report.mkdir()
    pins=sweep.references(report)
    assert Path(path) in pins
    assert str(root/'ACK.txt') in json.loads((report/'references.json').read_text())['reference_source_sha256']


def test_sqlite_journals_and_duckdb_stay_in_place(tmp_path,monkeypatch):
    root=tmp_path/'evidence';root.mkdir()
    files={'database-wal.db-wal':b'wal','data':b'abcdefghDUCKxxxx','ordinary.txt':b'evidence'}
    for name,data in files.items():(root/name).write_bytes(data)
    now=time.time();monkeypatch.setattr(m.time,'time',lambda:now+10*86400)
    row=m.cold_archive(API(),root,tmp_path/'out.tar.zst',dry_run=False,deadline=time.monotonic()+60)
    assert row['status']=='ARCHIVED_VERIFIED_RETIRED'
    assert (root/'database-wal.db-wal').read_bytes()==b'wal'
    assert (root/'data').read_bytes()==files['data']
    assert not (root/'ordinary.txt').exists()


def test_extensionless_sqlite_vetoes_entire_node_tree(tmp_path,monkeypatch):
    p=tmp_path/'node_modules';p.mkdir();(p/'payload').write_bytes(b'SQLite format 3\0'+b'x'*100)
    now=time.time();monkeypatch.setattr(m.time,'time',lambda:now+4*86400)
    monkeypatch.setattr(large,'lsof_inactive',lambda p:True)
    row=m.node_modules(API(),[p],dry_run=False,deadline=time.monotonic()+30)[0]
    assert row['status']=='REFUSED' and row['reason']=='protected database payload'
    assert (p/'payload').exists()


def test_small_class_is_refused_before_content_hashing(tmp_path,monkeypatch):
    p=tmp_path/'report';p.mkdir();(p/'proof.txt').write_text('evidence')
    now=time.time();monkeypatch.setattr(m.time,'time',lambda:now+10*86400)
    def forbidden_hash(*args):raise AssertionError('tiny classes must not get full SHA reads')
    monkeypatch.setattr(m,'sha',forbidden_hash)
    row=m.cold_archive(API(),p,tmp_path/'out.tar.zst',dry_run=False,min_bytes=128*1024**2,deadline=time.monotonic()+30)
    assert row['status']=='BELOW_RECLAIM_FLOOR' and (p/'proof.txt').exists()


@pytest.mark.parametrize('error', [ValueError('relay/lane discovery changed during collection'),
                                  PermissionError('reference source unreadable'),
                                  subprocess.CalledProcessError(1,['stat','--','site-run']),
                                  subprocess.TimeoutExpired(['stat','--','site-run'],30)])
def test_reference_collection_refusal_has_terminal_zero_effect_receipt(tmp_path,monkeypatch,error):
    import host_disk_relay_custody
    bus=tmp_path/'business'
    root=bus/'reports/ops/glp347-pallas-20261007';root.mkdir(parents=True)
    evidence=root/'ACK.txt';evidence.write_text('retained evidence')
    (bus/'ops').mkdir()
    (bus/'ops/disk-retention-policy.json').write_text('{"protected_roots":[]}')
    changed=bus/'lanes/RELEASE/evidence'
    error.changed_source=str(changed)
    def refuse(**kwargs):raise error
    def forbidden_effect(*args,**kwargs):raise AssertionError('effect after refused inventory')
    monkeypatch.setattr(sweep,'BUS',bus)
    monkeypatch.setattr(host_disk_relay_custody,'collect',refuse)
    monkeypatch.setattr(m,'node_modules',forbidden_effect)
    report=tmp_path/'report'
    with pytest.raises(type(error)) as caught:
        sweep.run(report,budget=10)
    assert caught.value is error
    receipt=json.loads((report/'large-classes.json').read_text())
    assert receipt['stage']=='reference_inventory_refused'
    assert receipt['attributable_bytes_freed']==0
    assert receipt['reference_veto_source']==str(changed)
    assert receipt['rows']==[{'rule':'reference_inventory','status':'GATED_REFERENCE_INVENTORY',
        'ok':False,'partial':True,'bytes_freed':0,'reason':str(error),
        'error_type':type(error).__name__,'changed_source':str(changed)}]
    assert evidence.read_text()=='retained evidence'
    assert not (report/'references.json').exists()


def test_archive_headroom_veto_keeps_originals(tmp_path,monkeypatch):
    p=tmp_path/'report';p.mkdir();(p/'proof.txt').write_text('evidence')
    now=time.time();monkeypatch.setattr(m.time,'time',lambda:now+10*86400)
    row=m.cold_archive(API(),p,tmp_path/'out.tar.zst',dry_run=False,reserve_bytes=10**18,deadline=time.monotonic()+30)
    assert row['status']=='INSUFFICIENT_HEADROOM'
    assert not (tmp_path/'out.tar.zst').exists() and (p/'proof.txt').exists()
