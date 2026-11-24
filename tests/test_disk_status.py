import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('disk_status', Path(__file__).parents[1] / 'scripts/disk_status.py')
assert SPEC is not None and SPEC.loader is not None
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class DiskStatusTests(unittest.TestCase):
    def test_thresholds_and_hourly_source(self):
        self.assertEqual([m.level(x) for x in (14.9, 15, 29.9, 30, 60)],
                         ['critical', 'low', 'low', 'ok', 'ok'])
        unit = (Path(__file__).parents[1] / 'ops/systemd/kael-disk-status.timer').read_text()
        self.assertIn('OnCalendar=*-*-* *:25:00', unit)
        self.assertIn('Persistent=true', unit)

    def test_real_allocation_delta_not_mtime_and_bounded_history(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / 'writer'
            writers = ((p, 'owner', 'protected'),)
            history = [{'at': i * 3600, 'bytes': {str(p): 1_000_000_000}} for i in range(60)]
            with patch.object(m, 'WRITERS', writers):
                result = m.observe({'history': history}, {str(p): 7_000_000_000}, 60 * 3600, (100e9, 75e9, 25e9))
            row = result['top_growers'][0]
            self.assertEqual(row['gb_per_day'], 6)
            self.assertEqual(row['observed_hours'], 24)
            self.assertTrue(row['over_5gb_per_day'])
            self.assertFalse(row['automatic_cap_applied'])
            self.assertLessEqual(len(result['history']), 49)
            self.assertEqual(result['state'], 'low')

    def test_status_mailbox_read_mode_and_unknown_first_growth(self):
        with tempfile.TemporaryDirectory() as d:
            box = Path(d)
            p = box / 'report'
            p.write_bytes(b'irreplaceable')
            with patch.object(m, 'WRITERS', ((p, 'owner', 'report'),)):
                result = m.run(box)
            self.assertEqual(p.read_bytes(), b'irreplaceable')
            self.assertIsNone(result['top_growers'][0]['gb_per_day'])
            self.assertEqual(result['freed_gb_this_run'], 0)
            self.assertFalse(result['cleanup_enabled'])
            for name in ('disk-status.json', 'disk-events.jsonl', '.disk-status.lock'):
                s = (box / name).stat()
                self.assertEqual(stat.S_IMODE(s.st_mode), 0o640)
                self.assertEqual(s.st_nlink, 1)
            self.assertEqual(json.loads((box / 'disk-status.json').read_text()), result)

    def test_daily_and_weekly_no_repeat_same_period(self):
        with tempfile.TemporaryDirectory() as d:
            box = Path(d)
            p = box / 'report'
            p.write_bytes(b'unique')
            with patch.object(m, 'WRITERS', ((p, 'owner', 'report'),)):
                m.run(box)
                m.run(box)
            events = [json.loads(line) for line in (box / 'disk-events.jsonl').read_text().splitlines()]
            self.assertEqual(sum(r['kind'] == 'daily' for r in events), 1)
            candidates = [r for r in events if r['kind'] == 'candidates for Izaiah']
            self.assertEqual(len(candidates), 1)
            self.assertEqual(candidates[0]['candidates'][0]['path'], str(p))

    def test_mailbox_alias_and_multilink_refused(self):
        with tempfile.TemporaryDirectory() as d:
            box = Path(d)
            target = box / 'original'
            target.write_bytes(b'preserve')
            original_mode = target.stat().st_mode
            alias = box / 'alias'
            alias.symlink_to(target)
            with self.assertRaises(OSError):
                m.checked_fd(alias, os.O_RDONLY)
            alias.unlink()
            os.link(target, alias)
            with self.assertRaises(ValueError):
                m.checked_fd(alias, os.O_RDONLY)
            self.assertEqual(target.stat().st_mode, original_mode)
            self.assertEqual(target.read_bytes(), b'preserve')
