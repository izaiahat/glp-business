import base64
import hashlib
import json
import stat
import tarfile
import os
from pathlib import Path
import sys
import types
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
sys.modules.setdefault('r2_offload', types.ModuleType('r2_offload'))
import reports_archiver as archiver


def identity(path):
    s = path.lstat()
    return (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_gid,
            s.st_nlink, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    root = tmp_path / 'prepared'; release = root / 'release'; release.mkdir(parents=True)
    member = release / 'site.bin'; member.write_bytes(b'approved')
    external = tmp_path / 'receipt.json'; external.write_bytes(b'accepted')
    calls = []
    module = types.ModuleType('glp_price_index_weekly')
    module.pointer = tmp_path / 'current.json'; module.pointer.write_text('{}')
    monkeypatch.setattr(archiver, '_canonical_base_path', lambda: module.pointer)
    def read(path, label):
        path = Path(path)
        return path, path.read_bytes(), identity(path)
    def validate(base, directory):
        calls.append(dict(base))
        assert directory == release
        if (member.is_symlink() or member.read_bytes() != b'approved'
                or set(release.iterdir()) != {member} or external.read_bytes() != b'accepted'):
            raise RuntimeError('native_authority_invalid')
        return {'prepared_binding': {'files': {str(external):
                (hashlib.sha256(external.read_bytes()).hexdigest(), identity(external))},
                'release': 'validated'}}
    module._read_bound_regular_file = read
    module._validate_base_evidence = validate
    monkeypatch.setitem(sys.modules, 'glp_price_index_weekly', module)
    monkeypatch.setattr(archiver, '_PREPARED_EVIDENCE_CACHE', None)
    return release, member, external, calls, module


def test_reuses_only_complete_unchanged_proof_and_returns_copy(prepared):
    release, member, external, calls, module = prepared
    module.pointer.write_text('{"deployment": "a"}')
    a = archiver._registered_prepared_release_evidence({'deployment': 'a'}, release)
    a['prepared_binding']['release'] = 'caller-mutated'
    b = archiver._registered_prepared_release_evidence({'deployment': 'a'}, release)
    assert len(calls) == 1 and b['prepared_binding']['release'] == 'validated'
    module.pointer.write_text('{"deployment": "b"}')
    archiver._registered_prepared_release_evidence({'deployment': 'b'}, release)
    assert len(calls) == 2


@pytest.mark.parametrize('change', ['rewrite_restore_mtime', 'new_member', 'symlink', 'external'])
def test_every_drift_reenters_native_validator_and_never_uses_stale_proof(prepared, change):
    release, member, external, calls, module = prepared
    archiver._registered_prepared_release_evidence({}, release)
    if change == 'rewrite_restore_mtime':
        before = member.stat(); member.write_bytes(b'corrupt!')
        os.utime(member, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert member.stat().st_size == before.st_size
        assert member.stat().st_ctime_ns != before.st_ctime_ns
    elif change == 'new_member': (release / 'new').write_text('extra')
    elif change == 'symlink': member.unlink(); member.symlink_to(external)
    else: external.write_bytes(b'altered!')
    with pytest.raises(RuntimeError, match='native_authority_invalid'):
        archiver._registered_prepared_release_evidence({}, release)
    assert len(calls) == 2 and archiver._PREPARED_EVIDENCE_CACHE is None


def test_mutation_during_full_validation_cannot_seed_cache(prepared):
    release, member, external, calls, module = prepared
    native = module._validate_base_evidence
    def changed(base, directory):
        proof = native(base, directory)
        member.write_bytes(b'corrupt!')
        return proof
    module._validate_base_evidence = changed
    archiver._registered_prepared_release_evidence({}, release)
    assert archiver._PREPARED_EVIDENCE_CACHE is None
    with pytest.raises(RuntimeError, match='native_authority_invalid'):
        archiver._registered_prepared_release_evidence({}, release)


def test_missing_optional_identity_reader_retains_native_uncached_path(prepared):
    release, member, external, calls, module = prepared
    del module._read_bound_regular_file
    archiver._registered_prepared_release_evidence({}, release)
    archiver._registered_prepared_release_evidence({}, release)
    assert len(calls) == 2 and archiver._PREPARED_EVIDENCE_CACHE is None


def test_semantically_identical_pointer_rewrite_rechecks_exact_native_binding(prepared):
    release, member, external, calls, module = prepared
    native = module._validate_base_evidence
    def exact_pointer(base, directory):
        proof = native(base, directory)
        if module.pointer.read_bytes() != b'{}':
            raise RuntimeError('native_pointer_binding_invalid')
        return proof
    module._validate_base_evidence = exact_pointer
    archiver._registered_prepared_release_evidence({}, release)
    module.pointer.write_bytes(b'{ }')
    with pytest.raises(RuntimeError, match='native_pointer_binding_invalid'):
        archiver._registered_prepared_release_evidence({}, release)
    assert len(calls) == 2 and archiver._PREPARED_EVIDENCE_CACHE is None


def test_pointer_changes_during_initial_proof_cannot_seed_cache(prepared):
    release, member, external, calls, module = prepared
    native = module._validate_base_evidence
    def changed(base, directory):
        proof = native(base, directory)
        module.pointer.write_bytes(b'{ }')
        return proof
    module._validate_base_evidence = changed
    archiver._registered_prepared_release_evidence({}, release)
    assert archiver._PREPARED_EVIDENCE_CACHE is None


def test_snapshot_limit_declines_cache_not_native_validation(prepared, monkeypatch):
    release, member, external, calls, module = prepared
    monkeypatch.setattr(archiver, '_PREPARED_CACHE_MAX_ENTRIES', 1)
    archiver._registered_prepared_release_evidence({}, release)
    archiver._registered_prepared_release_evidence({}, release)
    assert len(calls) == 2 and archiver._PREPARED_EVIDENCE_CACHE is None


@pytest.mark.parametrize('changed', ['build', 'ci', 'design', 'absence', 'authority'])
def test_transitive_affiliate_proof_drift_cannot_use_cached_authority(prepared, monkeypatch, changed):
    release, member, external, calls, module = prepared
    root=external.parent
    monkeypatch.setattr(archiver,'BUSINESS',root)
    paths={name:root/(name+'.json') for name in ('build','ci','design','absence','authority')}
    for name,path in paths.items():path.write_bytes(name.encode())
    def pin(path, relative=False):return {'path':path.name if relative else str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
    paths['design'].write_text(json.dumps({'checks':['affiliate-relationship-sync','raw-held-output','native-build-validators'],
        'unmapped_adverse_evidence':pin(paths['absence'])}))
    external.write_text(json.dumps({'schema':'glp-affiliate-sync-release-result-v1',
        'evidence':{name:pin(paths[name]) for name in ('build','ci','design')}}))
    review=root/'review.json';review.write_text(json.dumps({'schema':'glp-affiliate-sync-operator-release-v1',
        'operator_authority':pin(paths['authority'],True)}))
    expected={path:hashlib.sha256(path.read_bytes()).hexdigest() for path in [external,review,*paths.values()]}
    def native(base,directory):
        calls.append(dict(base))
        if any(hashlib.sha256(path.read_bytes()).hexdigest()!=digest for path,digest in expected.items()):
            raise RuntimeError('native_transitive_proof_drift')
        return {'prepared_binding':{'files':{str(path):(expected[path],identity(path)) for path in (external,review)},'release':'validated'}}
    module._validate_base_evidence=native
    archiver._registered_prepared_release_evidence({},release)
    archiver._registered_prepared_release_evidence({},release)
    assert len(calls)==1
    paths[changed].write_bytes(b'altered')
    with pytest.raises(RuntimeError,match='native_transitive_proof_drift'):
        archiver._registered_prepared_release_evidence({},release)
    assert len(calls)==2 and archiver._PREPARED_EVIDENCE_CACHE is None


def test_historical_preimage_is_not_a_live_dependency(prepared):
    release, member, external, calls, module=prepared
    external.write_text(json.dumps({'schema':'glp43-current-deploy-receipt-v1',
        'predecessor_pointer':{'path':str(module.pointer),'sha256':'0'*64}}))
    proof={'prepared_binding':{'files':{str(external):(hashlib.sha256(external.read_bytes()).hexdigest(),identity(external))}}}
    assert archiver._prepared_dependencies_unchanged(proof)


def test_unknown_gate_family_declines_cache(prepared):
    release, member, external, calls, module=prepared
    gate=external.with_name('gate.json');gate.write_text(json.dumps({'schema':'unregistered-future-gate'}))
    proof={'prepared_binding':{'files':{str(gate):(hashlib.sha256(gate.read_bytes()).hexdigest(),identity(gate))}}}
    assert not archiver._prepared_dependencies_unchanged(proof)


@pytest.fixture
def reuse(tmp_path, monkeypatch):
    home = tmp_path/'home'; business=home/'business'; root=business/'archive/f12-disk-archival'
    archive_dir=root/'20261003T003919Z'; archive_dir.mkdir(parents=True)
    source=home/'source/log.txt';source.parent.mkdir();source.write_bytes(b'original')
    heads=[]; puts=[]; remote={}
    class Client:
        def head_object(self, **kw): heads.append(kw);return remote.copy()
    def offload(*args, **kw): puts.append(args);raise AssertionError('unexpected PUT')
    monkeypatch.setattr(archiver,'HOME',home);monkeypatch.setattr(archiver,'BUSINESS',business)
    monkeypatch.setattr(archiver,'ARCHIVE_ROOT',root)
    monkeypatch.setattr(archiver,'r2_offload',types.SimpleNamespace(BUCKET='tilton-archives',_client=lambda:Client(),offload=offload))
    monkeypatch.setattr(archiver,'run_loaded_worker_bounded',lambda f,**kw:(f(-1,kw['operation_deadline']),[]))
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
    result=archiver._reuse_source_archive(relative,{'manifest_sha256':'a'*64},content,run_context=None,deadline=None)
    assert result[0]==archive and result[1]['source_matches']
    receipt=archiver._offload_archive(archive,digest)
    assert receipt['r2_status']=='EXISTING_VERIFIED' and not receipt['r2_put_attempted']
    assert archiver._offhost_verified(receipt,archive,digest) and len(heads)==1 and puts==[]
    assert source.read_bytes()==b'original'


def test_cron_archive_reuses_complete_current_member_manifest(reuse):
    source,build,heads,puts,remote=reuse;archive,relative,content,batch,digest=build(cron=True)
    assert archiver._reuse_cron_archive(batch,run_context=None,deadline=None)[0]==archive
    batch['members'][0]['sha256']='0'*64
    assert archiver._reuse_cron_archive(batch,run_context=None,deadline=None) is None
    assert puts==[] and source.exists()


@pytest.mark.parametrize('change',['content','mode'])
def test_changed_source_cannot_reuse_old_archive(reuse,change):
    source,build,heads,puts,remote=reuse;archive,relative,content,batch,digest=build()
    if change=='content':
        source.write_bytes(b'changed!');content['rows'][0]=('.', 'regular_file',hashlib.sha256(source.read_bytes()).hexdigest())
    else:source.chmod(0o600 if stat.S_IMODE(source.stat().st_mode)!=0o600 else 0o644)
    assert archiver._reuse_source_archive(relative,{'manifest_sha256':'a'*64},content,run_context=None,deadline=None) is None
    assert hashlib.sha256(archive.read_bytes()).hexdigest()==digest and puts==[]


def test_remote_checksum_mismatch_refuses_without_fallback_put(reuse):
    source,build,heads,puts,remote=reuse;archive,relative,content,batch,digest=build()
    remote['ChecksumSHA256']='wrong'
    receipt=archiver._offload_archive(archive,digest)
    assert receipt['r2_status']=='VERIFY_FAILED' and not archiver._offhost_verified(receipt,archive,digest)
    assert puts==[] and source.exists()
