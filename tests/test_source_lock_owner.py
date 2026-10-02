"""Held source-lock leases are local capabilities, never a bypass flag."""
from concurrent.futures import ThreadPoolExecutor
import pytest
from scripts import sync_from_repo as syncer


def test_owner_can_join_but_an_independent_descriptor_is_excluded(tmp_path):
    with syncer.source_lock(tmp_path) as owner:
        assert owner is not None
        with syncer.source_session(tmp_path, owner) as joined:
            assert joined is owner
        with pytest.raises(ValueError, match='source_import_busy'):
            with syncer.source_lock(tmp_path):
                pass
    with syncer.source_lock(tmp_path):
        pass


def test_expired_or_forged_owner_refuses(tmp_path):
    with syncer.source_lock(tmp_path) as owner:
        pass
    for invalid in (owner, object(), True):
        with pytest.raises(ValueError, match='source_import_owner_invalid'):
            with syncer.source_session(tmp_path, invalid):
                pass


def test_owner_cannot_cross_root_or_thread(tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    with syncer.source_lock(tmp_path) as owner:
        with pytest.raises(ValueError, match='source_import_owner_invalid'):
            with syncer.source_session(other, owner):
                pass
        def join():
            with syncer.source_session(tmp_path, owner):
                pass
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(ValueError, match='source_import_owner_invalid'):
                pool.submit(join).result()


def test_owner_detects_root_inode_replacement(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    with syncer.source_lock(root) as owner:
        root.rename(tmp_path / 'moved')
        root.mkdir()
        with pytest.raises(ValueError, match='source_import_root_moved'):
            with syncer.source_session(root, owner):
                pass
