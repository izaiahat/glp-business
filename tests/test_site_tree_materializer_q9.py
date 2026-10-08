"""Literal Q9 boundaries on isolated trees; no installed helper or live effects."""
import importlib.util
import os
from pathlib import Path
import shutil
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / "scripts/site_tree_materializer.py"
SPEC = importlib.util.spec_from_file_location("q9_materializer", SOURCE)
materializer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(materializer)


def geometry(total=10000, free=1000, available=1000, unit=4096):
    return SimpleNamespace(f_blocks=total, f_bfree=free, f_bavail=available, f_frsize=unit)


class Q9Tests(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get("TMPDIR", str(Path.home() / ".cache/glp-tests")))
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="q9-offline-", dir=scratch)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        for relative in ("keep.txt", "change.txt", "node_modules/pkg/index.js",
                         "nested/node_modules/pkg/index.js", "nested/keep.txt",
                         "skip.txt", ".git/retained", "node_modules-not/keep.txt"):
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative + "\n")
            path.chmod(0o444)
        for path in [self.source, *self.source.rglob("*")]:
            if path.is_dir():
                path.chmod(0o555)
        self.business = self.root / "business"
        self.reports = self.business / "reports"
        self.reports.mkdir(parents=True)
        self.business_patch = mock.patch.object(materializer, "_BUSINESS_ROOT", self.business)
        self.business_patch.start()
        self.addCleanup(self.business_patch.stop)
        self.pressure = mock.patch.object(materializer.os, "fstatvfs", return_value=geometry())
        self.observation = self.pressure.start()
        self.addCleanup(self.pressure.stop)
        # Even accidental privileged/external calls must be denied in this suite.
        self.process = mock.patch.object(materializer.subprocess, "run", side_effect=AssertionError("external effect"))
        self.process.start()
        self.addCleanup(self.process.stop)

    def tearDown(self):
        for path in [self.root, *self.root.rglob("*")]:
            if path.is_dir() and not path.is_symlink():
                path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o700)
        self.temp.cleanup()

    def test_at_or_above_92_refuses_before_creation_on_destination_descriptor(self):
        # Reserved blocks count: 9000/(9000+500) is STOP despite only 90% total use.
        for index, info in enumerate((geometry(free=800, available=800),
                                      geometry(free=799, available=799),
                                      geometry(free=1000, available=500))):
            with self.subTest(info=info):
                observed = []
                def observe(fd):
                    observed.append(fd)
                    bound = os.fstat(fd)
                    parent = self.reports.stat()
                    self.assertEqual((bound.st_dev, bound.st_ino), (parent.st_dev, parent.st_ino))
                    return info
                self.observation.side_effect = observe
                destination = self.reports / f"stop-{index}"
                with mock.patch.object(materializer.os, "mkdir", side_effect=AssertionError("mkdir before refusal")), \
                     mock.patch.object(materializer.os, "link", side_effect=AssertionError("link before refusal")):
                    with self.assertRaisesRegex(RuntimeError, "candidate_copy_disk_pressure:STOP"):
                        materializer.copytree_linked(self.source, destination)
                self.assertFalse(destination.exists())
                self.assertEqual(len(observed), 1)
                with self.assertRaises(OSError):
                    os.fstat(observed[0])

    def test_unknown_observation_and_geometry_refuse_before_creation(self):
        invalid = (OSError("unavailable"), None, geometry(total=0, free=0, available=0),
                   geometry(unit=0), geometry(free=10001), geometry(available=1001),
                   geometry(available=-1), geometry(total=float("nan")))
        for index, info in enumerate(invalid):
            with self.subTest(info=info):
                self.observation.side_effect = info if isinstance(info, Exception) else None
                self.observation.return_value = info
                destination = self.reports / f"unknown-{index}"
                with mock.patch.object(materializer.os, "mkdir", side_effect=AssertionError("mkdir before refusal")):
                    with self.assertRaisesRegex(RuntimeError, "candidate_copy_disk_pressure:UNKNOWN"):
                        materializer.copytree_linked(self.source, destination)
                self.assertFalse(destination.exists())

    def test_below_92_preserves_cow_detach_and_selective_seal(self):
        self.observation.return_value = geometry(free=801, available=801)
        destination = self.root / "candidate"
        before = materializer.tree_content_closure(self.source)
        report = materializer.copytree_linked(self.source, destination)
        self.assertEqual(report["copied_files"], 0)
        self.assertEqual(report["copied_bytes"], 0)
        self.assertEqual(report["linked_files"], 8)
        self.assertEqual((destination / "keep.txt").stat().st_ino, (self.source / "keep.txt").stat().st_ino)
        materializer.detach_for_write(destination / "change.txt")
        (destination / "change.txt").write_text("changed\n")
        self.assertNotEqual((destination / "change.txt").stat().st_ino, (self.source / "change.txt").stat().st_ino)
        sealed = materializer.seal_tree_selective(destination, [self.source], owner=None)
        self.assertEqual(sealed["shared_files_preserved"], 7)
        self.assertEqual(sealed["private_files_sealed"], 1)
        self.assertEqual(materializer.tree_content_closure(self.source), before)

    def test_report_default_excludes_only_root_and_nested_node_modules(self):
        destination = self.reports / "snapshot"
        before = materializer.tree_content_closure(self.source)
        report = materializer.copytree_linked(self.source, destination)
        self.assertEqual(report["ignored_paths"], ["nested/node_modules", "node_modules"])
        self.assertFalse((destination / "node_modules").exists())
        self.assertFalse((destination / "nested/node_modules").exists())
        for relative in (".git/retained", "node_modules-not/keep.txt", "nested/keep.txt", "skip.txt"):
            self.assertTrue((destination / relative).is_file())
        self.assertEqual(materializer.tree_content_closure(self.source), before)

    def test_report_exclusion_composes_with_caller_ignore(self):
        report = materializer.copytree_linked(self.source, self.reports / "snapshot",
                                             ignore=shutil.ignore_patterns("skip.txt"))
        self.assertEqual(report["ignored_paths"], ["nested/node_modules", "node_modules", "skip.txt"])
        self.assertFalse((self.reports / "snapshot/skip.txt").exists())
        self.assertTrue((self.reports / "snapshot/keep.txt").exists())

    def test_callback_list_mutation_cannot_remove_report_default(self):
        visited = []

        def caller(directory, names):
            visited.append(directory)
            if "node_modules" in names:
                names.remove("node_modules")
            return [name for name in names if name == "skip.txt"]

        destination = self.reports / "callback-mutates-list"
        report = materializer.copytree_linked(self.source, destination, ignore=caller)
        self.assertFalse((destination / "node_modules").exists(), report)
        self.assertFalse((destination / "nested/node_modules").exists(), report)
        self.assertFalse((destination / "skip.txt").exists())
        self.assertEqual(report["ignored_paths"], ["nested/node_modules", "node_modules", "skip.txt"])
        self.assertEqual(report["linked_files"], 5)
        self.assertEqual(len(visited), 4)
        self.assertEqual(len(visited), len(set(visited)))
        self.assertTrue((destination / "node_modules-not/keep.txt").is_file())

    def test_nonreport_candidates_and_report_lookalikes_retain_node_modules(self):
        for parent in (self.root, self.business / "reports-other"):
            parent.mkdir(exist_ok=True)
            with self.subTest(parent=parent):
                destination = parent / "candidate"
                report = materializer.copytree_linked(self.source, destination)
                self.assertEqual(report["ignored_paths"], [])
                self.assertEqual(report["linked_files"], 8)
                self.assertTrue((destination / "node_modules/pkg/index.js").is_file())
                self.assertTrue((destination / "nested/node_modules/pkg/index.js").is_file())


if __name__ == "__main__":
    unittest.main()
