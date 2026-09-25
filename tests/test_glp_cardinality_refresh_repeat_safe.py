#!/usr/bin/env python3
"""The cardinality refresh must survive being run many times in one deploy.

DEFECT 2026-09-02 (round 24, provider_cardinality_contract_unknown):
glp_affiliate_wire_backlog_worker runs this refresh ONCE PER PREPARED ROW and
then again post-build - 19 invocations for an 18-row deploy. Each entry is
(file, old, new, marker), and replace_contract raises when it finds neither its
`old` nor any of its markers.

A new entry migrated the Option C block whose text was ALSO serving as the
already-migrated marker of a DIFFERENT entry. Invocation 1 migrated it and
destroyed that marker; invocation 2 could then find neither, and the whole
deploy failed on row 2 of 18. A single-invocation test passed clean, which is
exactly why this one exists.

This runs the real entry list over the real test files, repeatedly.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "scripts")]

import glp_provider_cardinality_test_refresh as refresher  # noqa: E402

SOURCE = ROOT / "scripts/glp_provider_cardinality_test_refresh.py"
RELEASES = ROOT / "site-system/flagship-v2/releases"


def _entries() -> list[tuple]:
    """The real `extra` entry list, evaluated out of the module source."""
    text = SOURCE.read_text()
    block = text[text.index("    extra += ["):text.index("    for name, old, new, marker in extra:")]
    namespace: dict = {}
    exec("extra = []\nif True:\n" + block, namespace)  # keeps the 4-space indent valid
    return namespace["extra"]


def _newest_release() -> Path | None:
    found = sorted(RELEASES.glob("glp-b2-wire-backlog-*/tests"))
    return found[-1].parent if found else None


def test_entry_list_is_stable_under_repeated_invocation():
    release = _newest_release()
    if release is None:
        pytest.skip("no release with tests/ on disk")
    entries = _entries()
    assert entries, "no extra entries found - has the block been renamed?"
    tmp = Path(tempfile.mkdtemp(prefix="cardinality-repeat-"))
    try:
        (tmp / "tests").mkdir()
        for name, *_ in entries:
            source = release / "tests" / name
            if source.is_file():
                shutil.copy2(source, tmp / "tests" / name)
        changed_per_pass = []
        for _ in range(4):
            changed = 0
            for name, old, new, marker in entries:
                path = tmp / "tests" / name
                if not path.is_file():
                    continue
                # Must never raise on any pass, including the ones after the
                # first has already migrated the file.
                changed += bool(refresher.replace_contract(path, old, new, marker))
            changed_per_pass.append(changed)
        assert changed_per_pass[0] >= 1, changed_per_pass
        assert all(count == 0 for count in changed_per_pass[1:]), (
            f"refresh is not idempotent across invocations: {changed_per_pass}")
        for name, *_ in entries:
            path = tmp / "tests" / name
            if path.is_file():
                done = subprocess.run(["node", "--check", str(path)],
                                      capture_output=True, text=True)
                assert done.returncode == 0, f"{name}: {done.stderr[:300]}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_entry_destroys_another_entrys_marker_without_replacing_it():
    """The precise shape of the round-24 defect, pinned structurally."""
    entries = _entries()
    for name, _old, new, marker in entries:
        markers = (marker,) if isinstance(marker, str) else marker
        for other_name, _o, other_new, other_marker in entries:
            if other_name != name or other_new is new:
                continue
            others = (other_marker,) if isinstance(other_marker, str) else other_marker
            # If this entry's `new` text would be written into the same file,
            # the other entry must still recognise a migrated state afterwards.
            assert any(token in new or token in other_new for token in others), (
                f"{name}: migrating one block leaves no surviving marker for a "
                f"sibling entry - that is the round-24 failure")
        assert any(token in new for token in markers), (
            f"{name}: entry does not self-recognise after its own migration")
