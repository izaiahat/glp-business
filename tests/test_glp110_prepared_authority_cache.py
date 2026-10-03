import hashlib
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
    a = archiver._registered_prepared_release_evidence({'deployment': 'a'}, release)
    a['prepared_binding']['release'] = 'caller-mutated'
    b = archiver._registered_prepared_release_evidence({'deployment': 'a'}, release)
    assert len(calls) == 1 and b['prepared_binding']['release'] == 'validated'
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


def test_snapshot_limit_declines_cache_not_native_validation(prepared, monkeypatch):
    release, member, external, calls, module = prepared
    monkeypatch.setattr(archiver, '_PREPARED_CACHE_MAX_ENTRIES', 1)
    archiver._registered_prepared_release_evidence({}, release)
    archiver._registered_prepared_release_evidence({}, release)
    assert len(calls) == 2 and archiver._PREPARED_EVIDENCE_CACHE is None
