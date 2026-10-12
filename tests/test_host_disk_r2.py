import io
import json
import time
from pathlib import Path
import pytest
import host_disk_cold_classes as m
import host_disk_r2 as r2


class API:
    def path_is_active_privileged(self, p):return False


class Remote:
    def store(self, archive, manifest, deadline):
        objects=[{'role':role,'key':role,'sha256':m.sha(p),'bytes':p.stat().st_size,'verified':True}
                 for role,p in [('archive',archive),('manifest',manifest)]]
        return {'bucket':r2.BUCKET,'private':True,'verified':True,'objects':objects}


def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('TMPDIR',str(tmp_path))
    import host_disk_custody
    monkeypatch.setattr(host_disk_custody,'collect',lambda:set())
    monkeypatch.setattr(m,'live_symlink_targets',lambda:set())
    root=tmp_path/'evidence';root.mkdir();(root/'proof').write_bytes(b'x'*65536)
    now=time.time();monkeypatch.setattr(m.time,'time',lambda:now+35*86400)
    return root,tmp_path/'out/a.tar.zst'


def test_remote_verified_pair_precedes_retirement_and_local_archive_removal(tmp_path,monkeypatch):
    root,dest=setup(tmp_path,monkeypatch)
    row=m.cold_archive(API(),root,dest,dry_run=False,cold_days=30,
        archive_destination=Remote(),deadline=time.monotonic()+60)
    assert row['status']=='ARCHIVED_R2_VERIFIED_RETIRED'
    assert not dest.exists() and not (root/'proof').exists()
    assert row['remote']['verified'] and row['bytes_freed']>0


def test_remote_failure_keeps_verified_local_fallback(tmp_path,monkeypatch):
    root,dest=setup(tmp_path,monkeypatch)
    class Failed(Remote):
        def store(self,*args):raise OSError('fixture upload failure')
    row=m.cold_archive(API(),root,dest,dry_run=False,cold_days=30,
        archive_destination=Failed(),deadline=time.monotonic()+60)
    assert dest.exists() and row['remote_fallback']['error_type']=='OSError'
    assert row['status']=='ARCHIVED_VERIFIED_RETIRED'


def test_source_changed_during_remote_attempt_is_not_retired(tmp_path,monkeypatch):
    root,dest=setup(tmp_path,monkeypatch)
    class Changed(Remote):
        def store(self,*args):
            result=super().store(*args)
            (root/'proof').write_bytes(b'new live content')
            return result
    with pytest.raises(ValueError,match='source drift'):
        m.cold_archive(API(),root,dest,dry_run=False,cold_days=30,
            archive_destination=Changed(),deadline=time.monotonic()+60)
    assert (root/'proof').read_bytes()==b'new live content'
    assert not dest.exists()


class Missing(Exception):
    response={'Error':{'Code':'NoSuchKey'}}


class FakeS3:
    def __init__(self):self.objects={}
    def get_object(self,**kw):
        if kw['Key'] not in self.objects:raise Missing()
        data=self.objects[kw['Key']]
        return {'Body':io.BytesIO(data),'ContentLength':len(data)}


class Transport(r2.Destination):
    def upload(self,path,key,deadline):self.client.objects[key]=Path(path).read_bytes()


def pair(tmp_path,days=31):
    archive=tmp_path/'a.tar.zst';archive.write_bytes(b'unit transport fixture')
    manifest=tmp_path/'a.manifest.json'
    ns=int((time.time()-days*86400)*1e9)
    manifest.write_text(json.dumps({'members':[{'signature':[0,0,0,0,ns,ns,1]}]}))
    return archive,manifest


def test_readback_size_sha256_and_immutable_reuse(tmp_path):
    client=FakeS3();destination=Transport(client,lambda:True)
    archive,manifest=pair(tmp_path)
    result=destination.store(archive,manifest,time.monotonic()+60)
    assert result['object_count']==2 and result['private']
    assert all(r['verified'] and r['created'] for r in result['objects'])
    result=destination.store(archive,manifest,time.monotonic()+60)
    assert not any(r['created'] for r in result['objects'])
    key=result['objects'][0]['key'];client.objects[key]=b'corrupt'
    with pytest.raises(ValueError,match='size/SHA256'):
        destination.store(archive,manifest,time.monotonic()+60)
    assert client.objects[key]==b'corrupt' # Never overwrite a conflicting object.


@pytest.mark.parametrize('days,private',[(7,True),(31,False)])
def test_young_or_public_evidence_is_never_uploaded(tmp_path,days,private):
    client=FakeS3();destination=Transport(client,lambda:private)
    archive,manifest=pair(tmp_path,days)
    with pytest.raises(ValueError):destination.store(archive,manifest,time.monotonic()+60)
    assert not client.objects


def test_incompressible_remote_evidence_does_not_require_local_compression_savings(tmp_path,monkeypatch):
    root,dest=setup(tmp_path,monkeypatch)
    import os
    (root/'proof').write_bytes(os.urandom(65536))
    row=m.cold_archive(API(),root,dest,dry_run=False,cold_days=30,
        archive_destination=Remote(),min_net_bytes=1024,deadline=time.monotonic()+60)
    assert row['status']=='ARCHIVED_R2_VERIFIED_RETIRED' and row['bytes_freed']>0
    assert not dest.exists()


def test_headroom_batches_retire_only_manifest_members(tmp_path,monkeypatch):
    root,dest=setup(tmp_path,monkeypatch)
    (root/'second').write_bytes(b'y'*65536)
    row=m.cold_archive(API(),root,dest,dry_run=False,cold_days=30,
        archive_destination=Remote(),max_archive_input_bytes=140000,deadline=time.monotonic()+60)
    assert row['members']==1 and row['deferred_for_headroom_members']==1
    assert (root/'second').exists() and not (root/'proof').exists()


def test_r8_names_are_protected_without_external_inventory():
    assert m.protected(Path('/home/ubuntu/business/reports/ops/frozen-run/evidence-R8.txt'))
    assert m.protected(Path('/home/ubuntu/business/reports/ops/r8-evidence/proof.json'))


def test_scope_alias_created_during_upload_never_loses_originals(tmp_path,monkeypatch):
    root,dest=setup(tmp_path,monkeypatch);moved=tmp_path/'moved'
    class Alias(Remote):
        def store(self,*args):
            result=super().store(*args)
            root.rename(moved);root.symlink_to(moved,target_is_directory=True)
            return result
    with pytest.raises(ValueError,match='scope|symlink|alias'):
        m.cold_archive(API(),root,dest,dry_run=False,cold_days=30,
            archive_destination=Alias(),deadline=time.monotonic()+60)
    assert (moved/'proof').exists()


def test_site_archive_threads_r2_destination_before_local(tmp_path,monkeypatch):
    import host_disk_auto_classes as auto
    from types import SimpleNamespace
    site=tmp_path/'site';(site/'work/old').mkdir(parents=True)
    real=time.time();monkeypatch.setattr(time,'time',lambda:real+35*86400)
    calls=[];remote=object()
    def archive(*args,**kwargs):calls.append(kwargs);return {'bytes_freed':0}
    stub=SimpleNamespace(protected=lambda *args:False,cold_archive=archive)
    auto.site_cold_archive(API(),stub,tmp_path/'report',pins=(),site=site,
        archive_destination=remote,deadline=time.monotonic()+300,min_bytes=0)
    assert calls[0]['cold_days']==30 and calls[0]['archive_destination'] is remote
    assert calls[1]['cold_days']==7 and calls[1]['archive_destination'] is None


def test_verified_legacy_archive_migrates_after_full_pair_readback(tmp_path,monkeypatch):
    root,dest=setup(tmp_path,monkeypatch)
    m.cold_archive(API(),root,dest,dry_run=False,cold_days=30,deadline=time.monotonic()+60)
    assert dest.exists()
    rows=r2.migrate_archives(API(),m,tmp_path,Remote(),pins=(),deadline=time.monotonic()+60,dry_run=False)
    assert rows[0]['status']=='MIGRATED_R2_VERIFIED' and rows[0]['bytes_freed']>0
    assert not dest.exists() and Path(str(dest)+'.retirement.jsonl').exists()


def test_legacy_migration_upload_parent_alias_is_not_retired(tmp_path,monkeypatch):
    root,dest=setup(tmp_path,monkeypatch)
    m.cold_archive(API(),root,dest,dry_run=False,cold_days=30,deadline=time.monotonic()+60)
    held=tmp_path/'held'
    class Mutating(Remote):
        def store(self,a,b,d):
            receipt=super().store(a,b,d)
            a.parent.rename(held);a.parent.symlink_to(held,target_is_directory=True)
            return receipt
    rows=r2.migrate_archives(API(),m,tmp_path,Mutating(),pins=(),deadline=time.monotonic()+60,dry_run=False)
    assert rows[0]['status']=='REFUSED' and (held/dest.name).exists()


def test_singleton_lease_inode_and_exclusion(tmp_path):
    import os
    from host_disk_authorized_retirement import cleanup_lease
    lock=tmp_path/'lock';fd=cleanup_lease(lock)
    try:
        assert cleanup_lease(lock,fd)==fd
        with pytest.raises(BlockingIOError):cleanup_lease(lock)
        other=tmp_path/'other';other.write_text('')
        wrong=os.open(other,os.O_RDWR)
        try:
            with pytest.raises(ValueError):cleanup_lease(lock,wrong)
        finally:os.close(wrong)
    finally:os.close(fd)


def test_site_ranking_keeps_largest_direct_child_first(tmp_path,monkeypatch):
    import host_disk_authorized_retirement as a
    from types import SimpleNamespace
    text=f'1\t{tmp_path}/small\n900\t{tmp_path}/big\n901\t{tmp_path}\n99999\t/outside\n'
    monkeypatch.setattr(a.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(stdout=text))
    assert a.ranked_roots(tmp_path)==[tmp_path/'big',tmp_path/'small']


def test_private_bucket_object_expiry_is_refused():
    control=r2.Control({'CLOUDFLARE_ACCOUNT_ID':'fixture','CLOUDFLARE_API_TOKEN':'fixture'})
    def request(method,suffix,**kwargs):
        if suffix.endswith('/managed'):return {'enabled':False}
        if suffix.endswith('/custom'):return {'domains':[]}
        return {'rules':[{'enabled':True,'deleteObjectsTransition':{'condition':{'maxAge':30}}}]}
    control.request=request
    with pytest.raises(RuntimeError,match='expiry'):control.check_private()


def test_explicit_remote_authority_keeps_originals_on_failed_readback(tmp_path,monkeypatch):
    root,dest=setup(tmp_path,monkeypatch)
    class Authorized(Remote):
        def admit(self,root,days):return {'scope':'superseded_site'}
        def protect(self,p,pins):return m.protected(p,pins)
        def store(self,*args):raise OSError('fixture failed GET')
    row=m.cold_archive(API(),root,dest,dry_run=False,cold_days=0,
        remote_authority=Authorized(),deadline=time.monotonic()+60)
    assert row['status']=='REMOTE_REQUIRED_ORIGINALS_RETAINED'
    assert (root/'proof').exists() and not dest.exists()


def test_authorized_pair_store_reconciles_without_overwrite(tmp_path):
    from host_disk_authorized_retirement import store_pair
    transport=Transport(FakeS3(),lambda:True)
    archive,manifest=pair(tmp_path,days=0)
    proof=store_pair(transport,archive,manifest,time.monotonic()+60)
    assert all(o['created'] and o['verified'] for o in proof['objects'])
    again=store_pair(transport,archive,manifest,time.monotonic()+60)
    assert not any(o['created'] for o in again['objects'])
    transport.client.objects[proof['objects'][0]['key']]=b'conflicting bytes'
    with pytest.raises(ValueError,match='binding'):
        store_pair(transport,archive,manifest,time.monotonic()+60)


def test_site_protection_relaxes_only_seal_name_not_named_holds(tmp_path):
    from host_disk_authorized_retirement import site_protected
    root=tmp_path/'sealed-old'
    assert not site_protected(root/'ordinary.txt',(),root)
    assert site_protected(root/'ordinary.txt',(root,),root)
    for name in ('data.sqlite','backup.txt','R8.txt','.env','financial.json','secrets/value'):
        assert site_protected(root/name,(),root)


def test_real_site_authority_requires_disjoint_current_refs(tmp_path,monkeypatch):
    import host_disk_authorized_retirement as authorized
    site=tmp_path/'site';root=site/'candidates/sealed-old';root.mkdir(parents=True)
    monkeypatch.setattr(authorized,'SITE',site)
    monkeypatch.setattr(authorized,'fresh_pins',lambda:(site,site/'candidates'))
    authority=authorized.SiteAuthority(root,object())
    assert authority.admit(root,0)['scope']=='superseded_site'
    monkeypatch.setattr(authorized,'_CANONICAL_ROOTS',(site,))
    with pytest.raises(ValueError,match='custody'):authority.admit(root,0)
    monkeypatch.setattr(authorized,'_CANONICAL_ROOTS',())
    monkeypatch.setattr(authorized,'fresh_pins',lambda:(root,))
    with pytest.raises(ValueError,match='custody'):authority.admit(root,0)
    with pytest.raises(ValueError,match='scope'):authority.admit(root,30)


def test_completed_archive_authority_retains_manifest_and_partial_recovery(tmp_path,monkeypatch):
    import host_disk_authorized_retirement as authorized
    root,dest=setup(tmp_path,monkeypatch)
    m.cold_archive(API(),root,dest,dry_run=False,cold_days=7,deadline=time.monotonic()+60)
    monkeypatch.setattr(authorized,'fresh_pins',lambda:())
    ledger=Path(str(dest)+'.retirement.jsonl');original=ledger.read_text();ledger.write_text('')
    row=authorized.completed_archive(API(),dest,Remote(),tmp_path,time.monotonic()+60,False)
    assert row['status']=='RETAINED_PARTIAL_RETIREMENT_RECOVERY' and dest.exists()
    ledger.write_text(original)
    class Pair(Remote):
        def private_check(self):return True
        def readback(self,*args,**kwargs):return None
    monkeypatch.setattr(authorized,'store_pair',lambda d,a,b,end:Remote().store(a,b,end))
    row=authorized.completed_archive(API(),dest,Pair(),tmp_path,time.monotonic()+60,False)
    assert row['status']=='MIGRATED_R2_VERIFIED' and not dest.exists()
    assert Path(str(dest)+'.manifest.json').exists() and ledger.exists()


def test_remote_authority_becomes_current_before_retirement_refuses(tmp_path,monkeypatch):
    root,dest=setup(tmp_path,monkeypatch)
    class Authority(Remote):
        calls=0
        def protect(self,p,pins):return m.protected(p,pins)
        def admit(self,r,days):
            self.calls+=1
            if self.calls>1:raise ValueError('new current production custody')
            return {'scope':'superseded_site'}
    with pytest.raises(ValueError,match='production custody'):
        m.cold_archive(API(),root,dest,dry_run=False,cold_days=0,remote_authority=Authority(),deadline=time.monotonic()+60)
    assert (root/'proof').exists() and not dest.exists()


