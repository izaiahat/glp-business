"""Offline regressions for archive verification after a hardlink lookup."""
from __future__ import annotations

import io
import os
from pathlib import Path
import sys
import tarfile
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
# Curated source omits effectful offload code; this suite never calls it.
sys.modules.setdefault("r2_offload", types.ModuleType("r2_offload"))
import reports_archiver as ra


def archive_fixture(tmp_path: Path, *, suffix: str = "valid"):
    source = tmp_path / "source"
    source.mkdir()
    (source / "original").write_bytes(b"retained bytes")
    os.link(source / "original", source / "linked")
    (source / "after-link").write_bytes(b"trailing content must be verified")
    before = ra.tree_stats(source)
    content = ra.source_content_snapshot(source)
    archive = tmp_path / "fixture.tar.gz"
    with tarfile.open(archive, "w:gz", dereference=False) as tf:
        for name in (".", "original", "linked"):
            tf.add(source / name, arcname="source" if name == "." else f"source/{name}", recursive=False)
        if suffix != "missing":
            body = b"corrupt" if suffix == "corrupt" else (source / "after-link").read_bytes()
            info = tarfile.TarInfo("source/after-link")
            info.size = len(body)
            tf.addfile(info, io.BytesIO(body))
        if suffix in {"extra", "duplicate"}:
            info = tarfile.TarInfo("source/unexpected" if suffix == "extra" else "source/after-link")
            tf.addfile(info, io.BytesIO(b""))
    with tarfile.open(archive) as tf:
        members = tf.getmembers()
    assert members[2].islnk(), "The regression requires a real tar hardlink header"
    assert members[2].linkname == "source/original"
    return archive, before, content


def test_verifier_checks_members_after_hardlink_lookup(tmp_path):
    archive, before, content = archive_fixture(tmp_path)
    assert ra._verify_source_tar(archive, Path("source"), before, content) == 4


@pytest.mark.parametrize("suffix,reason", [
    ("missing", "archive_member_manifest_mismatch:expected=4:actual=3"),
    ("corrupt", "archive_member_content_mismatch:source/after-link"),
    ("extra", "archive_member_manifest_extra_member"),
    ("duplicate", "archive_member_manifest_extra_member"),
])
def test_trailing_bad_members_still_fail_closed(tmp_path, suffix, reason):
    archive, before, content = archive_fixture(tmp_path, suffix=suffix)
    with pytest.raises(RuntimeError, match=reason):
        ra._verify_source_tar(archive, Path("source"), before, content)
