"""Scratch-only discovery cost and unchanged mutation/resume boundaries."""
import json
import os
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import bounded_pruner as bp


class DiscoveryRegression(unittest.TestCase):
    def fixture(self):
        import tempfile
        base = Path(os.environ.get('TMPDIR', '/tmp'))
        base.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(prefix='glp349-discovery-', dir=base)

    def test_linear_sibling_discovery_and_fresh_mutations(self):
        real_scandir = os.scandir
        costs = []
        for count in (100, 400, 800):
            with self.fixture() as fixture:
                root = Path(fixture)
                for n in range(count):
                    (root / str(n)).mkdir()
                visited = []
                class Counted:
                    def __init__(self, path): self.inner = real_scandir(path)
                    def __iter__(self): return self
                    def __next__(self):
                        result = next(self.inner)
                        visited.append(result.name)
                        return result
                    def close(self): self.inner.close()
                    def __enter__(self): return self
                    def __exit__(self, *args): self.close()
                cursor = {}
                with patch.object(bp.os, 'scandir', Counted):
                    walker = bp.discovery(root, cursor, deadline=time.monotonic()+30, skip=lambda p: False)
                    yielded = []
                    try:
                        item = next(walker)
                        while True:
                            path, is_dir = item
                            yielded.append(path.name)
                            if len(yielded) == 1:
                                # Adding a sibling while a parent enumeration is
                                # suspended must still be discovered this call.
                                (root / 'late').mkdir()
                            if len(yielded) == 2:
                                # One caller deletion must not shift discovery;
                                # retain the other siblings to expose rescans.
                                path.rmdir()
                            item = walker.send(is_dir)
                    except StopIteration:
                        pass
                    finally:
                        walker.close()
                self.assertEqual(len(yielded), count+1)
                self.assertEqual(len(set(yielded)), count+1)
                self.assertEqual(cursor['frames'], [])
                costs.append(len(visited))
                self.assertLessEqual(len(visited), 4*(count+1), (count, len(visited)))
        print('GLP349 synthetic entries visited:', costs)

    def test_pending_effect_replayed_and_iterators_closed_on_budget_or_close(self):
        real_scandir = os.scandir
        live = set()
        class Tracked:
            def __init__(self, path):
                self.inner = real_scandir(path); live.add(self)
            def __iter__(self): return self
            def __next__(self): return next(self.inner)
            def close(self): self.inner.close(); live.discard(self)
            def __enter__(self): return self
            def __exit__(self, *args): self.close()
        with self.fixture() as fixture, patch.object(bp.os, 'scandir', Tracked):
            root = Path(fixture); (root/'pending').mkdir()
            cursor = {}
            walker = bp.discovery(root, cursor, deadline=time.monotonic()+30, skip=lambda p: False)
            pending, _ = next(walker)
            self.assertNotIn(pending.name, cursor['frames'][0][1])
            walker.close()
            self.assertEqual(live, set())
            cursor = json.loads(json.dumps(cursor))
            walker = bp.discovery(root, cursor, deadline=time.monotonic()+30, skip=lambda p: False)
            self.assertEqual(next(walker)[0], pending)
            with patch.object(bp.time, 'monotonic', return_value=float('inf')):
                with self.assertRaises(bp.ScanBudget): walker.send(True)
            self.assertEqual(live, set())
            self.assertIn(pending.name, cursor['frames'][0][1])

    def test_fresh_skip_and_symlink_refusal(self):
        with self.fixture() as fixture:
            root = Path(fixture)
            for name in ('a', 'b', 'c'):
                (root/name).mkdir()
            blocked = set()
            cursor = {}
            walker = bp.discovery(root, cursor, deadline=time.monotonic()+30, skip=lambda p: p in blocked)
            path, _ = next(walker)
            blocked.add(path)
            victim = next(p for p in root.iterdir() if p != path)
            victim.rmdir()
            victim.symlink_to(root, target_is_directory=True)
            (root/'link').symlink_to(root, target_is_directory=True)
            found = [path]
            try:
                item = walker.send(True)
                while True:
                    found.append(item[0]); item = walker.send(True)
            except StopIteration: pass
            finally: walker.close()
            self.assertEqual(len(found), 2)
            self.assertNotIn(root/'link', found)


if __name__ == '__main__':
    unittest.main()
