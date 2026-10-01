"""Offline reproduction of the two GLP-73 native retention run failures."""
from pathlib import Path
import sys
import types

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
# Curated checkout deliberately excludes the effectful offload implementation.
sys.modules.setdefault("r2_offload", types.ModuleType("r2_offload"))
import reports_archiver as archiver


@pytest.fixture
def flagship(tmp_path, monkeypatch):
    root = tmp_path / "flagship-v2"
    (root / "releases").mkdir(parents=True)
    monkeypatch.setattr(archiver, "FLAGSHIP", root)
    return root


def test_absent_optional_lanes_with_no_aliases_are_empty(flagship):
    assert archiver.release_symlink_target_references() == set()
    assert not (flagship / "staging").exists()


def test_missing_staging_does_not_hide_present_alias_target(flagship):
    target = flagship / "candidates" / "retained"
    target.mkdir(parents=True)
    (flagship / "review-cuts").mkdir()
    (flagship / "releases" / "alias").symlink_to(target, target_is_directory=True)
    assert archiver.release_symlink_target_references() == {target}
    assert not (flagship / "staging").exists()


@pytest.mark.parametrize("kind", ["file", "symlink", "dangling_symlink"])
def test_existing_invalid_optional_lane_stays_fail_closed(flagship, tmp_path, kind):
    lane = flagship / "staging"
    if kind == "file":
        lane.write_text("not a directory")
    else:
        target = tmp_path / "outside"
        if kind == "symlink":
            target.mkdir()
        lane.symlink_to(target, target_is_directory=True)
    with pytest.raises(archiver.SiteAuthorityError):
        archiver.release_symlink_target_references()


def test_dangling_release_alias_stays_fail_closed(flagship):
    (flagship / "releases" / "dangling").symlink_to(
        flagship / "staging" / "lost", target_is_directory=True
    )
    with pytest.raises(archiver.SiteAuthorityError, match="release_alias_target_unreadable"):
        archiver.release_symlink_target_references()


def test_alias_escape_stays_fail_closed(flagship, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (flagship / "releases" / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(archiver.SiteAuthorityError, match="release_alias_target_escapes_lanes"):
        archiver.release_symlink_target_references()


def test_missing_required_releases_root_stays_fail_closed(flagship):
    (flagship / "releases").rmdir()
    with pytest.raises(archiver.SiteAuthorityError, match="release_alias_root_unreadable"):
        archiver.release_symlink_target_references()
