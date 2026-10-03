import base64
import hashlib
import json
from pathlib import Path
import stat
import sys
import tarfile
import types
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
sys.modules.setdefault('r2_offload', types.ModuleType('r2_offload'))
import reports_archiver as a

@pytest.fixture
def reuse(tmp_path, monkeypatch):
    home = tmp_path/'home'; business=home/'business'; root=business/'archive/f12-disk-archival'
    archive_dir=root/'20261003T003919Z'; archive_dir.mkdir(parents=True)
    source=home/'source/log.txt';source.parent.mkdir();source.write_bytes(b'original')
    heads=[]; puts=[]; remote={}
    class Client:
        def head_object(self, **kw): heads.append(kw);return remote.copy()
    def offload(*args, **kw): puts.append(args);raise AssertionError('unexpected PUT')
    monkeypatch.setattr(a,'HOME',home);monkeypatch.setattr(a,'BUSINESS',business)
    monkeypatch.setattr(a,'ARCHIVE_ROOT',root)
    monkeypatch.setattr(a,'r2_offload',types.SimpleNamespace(BUCKET='tilton-archives',_client=lambda:Client(),offload=offload))
    monkeypatch.setattr(a,'run_loaded_worker_bounded',lambda f,**kw:(f(-1,kw['operation_deadline']),[]))
    def build(cron=False):
        archive=archive_dir/(('cron-output__fixture' if cron else 'source__log.txt')+'-20261003T003919Z.tar.gz')
        relative=source.relative_to(home)
        with tarfile.open(archive,'w:gz') as tf:tf.add(source,arcname=str(relative),recursive=False)
        digest=hashlib.sha256(archive.read_bytes()).hexdigest();size=archive.stat().st_size
        row={'r2_bucket':'tilton-archives','r2_key':str(archive.relative_to(business)),
             'sha256':digest,'bytes':size,'get_sha256':digest,'verified':True}
        (root/'verified-reuse-index.json').write_text(json.dumps({'schema':'f12-r2-verified-reuse-v1','objects':[row]}))
        remote.update({'ResponseMetadata':{'HTTPStatusCode':200},'ContentLength':size,
            'Metadata':{'sha256':digest},'ChecksumSHA256':base64.b64encode(bytes.fromhex(digest)).decode()})
        content={'rows':[('.', 'regular_file',hashlib.sha256(source.read_bytes()).hexdigest())],'sha256':'b'*64}
        batch={'job_id':'fixture','members':[{'path':str(source),'archive_path':str(relative),
               'size':source.stat().st_size,'sha256':hashlib.sha256(source.read_bytes()).hexdigest()}]}
        return archive,relative,content,batch,digest
    return source,build,heads,puts,remote


def test_source_archive_reused_with_fresh_head_and_no_put(reuse):
    source,build,heads,puts,remote=reuse;archive,relative,content,batch,digest=build()
    result=a._reuse_source_archive(relative,{'manifest_sha256':'a'*64},content,run_context=None,deadline=None)
    assert result[0]==archive and result[1]['source_matches']
    receipt=a._offload_archive(archive,digest)
    assert receipt['r2_status']=='EXISTING_VERIFIED' and not receipt['r2_put_attempted']
    assert a._offhost_verified(receipt,archive,digest) and len(heads)==1 and puts==[]
    assert source.read_bytes()==b'original'


def test_cron_archive_reuses_complete_current_member_manifest(reuse):
    source,build,heads,puts,remote=reuse;archive,relative,content,batch,digest=build(cron=True)
    assert a._reuse_cron_archive(batch,run_context=None,deadline=None)[0]==archive
    batch['members'][0]['sha256']='0'*64
    assert a._reuse_cron_archive(batch,run_context=None,deadline=None) is None
    assert puts==[] and source.exists()


@pytest.mark.parametrize('change',['content','mode'])
def test_changed_source_cannot_reuse_old_archive(reuse,change):
    source,build,heads,puts,remote=reuse;archive,relative,content,batch,digest=build()
    if change=='content':
        source.write_bytes(b'changed!');content['rows'][0]=('.', 'regular_file',hashlib.sha256(source.read_bytes()).hexdigest())
    else:source.chmod(0o600 if stat.S_IMODE(source.stat().st_mode)!=0o600 else 0o644)
    assert a._reuse_source_archive(relative,{'manifest_sha256':'a'*64},content,run_context=None,deadline=None) is None
    assert hashlib.sha256(archive.read_bytes()).hexdigest()==digest and puts==[]


def test_remote_checksum_mismatch_refuses_without_fallback_put(reuse):
    source,build,heads,puts,remote=reuse;archive,relative,content,batch,digest=build()
    remote['ChecksumSHA256']='wrong'
    receipt=a._offload_archive(archive,digest)
    assert receipt['r2_status']=='VERIFY_FAILED' and not a._offhost_verified(receipt,archive,digest)
    assert puts==[] and source.exists()
