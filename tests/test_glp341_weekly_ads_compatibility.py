"""Offline Q25 compatibility regression; retained receipt is NOT live acceptance."""
import ast
import hashlib
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'reports/ops/orchestration-restore-20260907/PLAN/A4/weekly_ads_loop.py'
FIXTURE = ROOT / 'tests/fixtures/glp341-a3-result.json'


def load(path=SOURCE):
    spec = importlib.util.spec_from_file_location('glp341_weekly', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WeeklyCompatibilityTests(unittest.TestCase):
    def test_historical_blocked_a3_does_not_veto_scoped_report(self):
        weekly = load()
        calls = []
        monitor = SimpleNamespace()
        for name in ('collect_google', 'collect_meta', 'collect_microsoft', 'collect_posthog'):
            setattr(monitor, name, lambda n=name: {'status': 'UNKNOWN', 'reason': n})

        def observe(platform):
            def collector(m, window):
                self.assertIs(m, monitor)
                calls.append((platform, window))
                # No explicit performance rows: UNKNOWN must survive, never zero.
                return weekly.unknown('NO_EXPLICIT_ROWS') | {'window': window}
            return collector

        fixed = datetime(2026, 10, 5, 16, 30, tzinfo=timezone.utc)
        with patch.object(weekly, 'A3', FIXTURE), \
             patch.object(weekly, 'google_week', observe('google')), \
             patch.object(weekly, 'meta_week', observe('meta')), \
             patch.object(weekly, 'microsoft_week', observe('microsoft')), \
             patch.object(weekly, 'posthog_week', observe('posthog')), \
             patch('socket.socket', side_effect=AssertionError('network forbidden')), \
             patch('subprocess.Popen', side_effect=AssertionError('child forbidden')):
            result = weekly.collect(fixed, {'monitor': monitor})
        self.assertEqual(result['windows']['current']['start'], '2026-09-28')
        self.assertEqual(result['windows']['previous']['end_exclusive'], '2026-09-28')
        self.assertEqual(len(calls), 8)
        for platform in ('google', 'meta', 'microsoft'):
            for label in ('current', 'previous'):
                self.assertEqual(result['weekly'][platform][label]['status'], 'UNKNOWN')
                self.assertEqual(result['weekly'][platform][label]['rows'], [])
                self.assertEqual(result['weekly'][platform][label]['window'], result['windows'][label])
        self.assertEqual(result['a3_evidence']['sha256'], hashlib.sha256(FIXTURE.read_bytes()).hexdigest())
        self.assertFalse(result['a3_evidence']['spend_authorized'])
        self.assertEqual(result['a3_evidence']['captured_at_utc'], '2026-09-08T21:42:11.823803+00:00')
        self.assertEqual(result['a3_evidence']['evidence_kind'], 'PINNED_HISTORICAL_OBSERVATION_NOT_AUTHORITY')
        self.assertEqual(result['effects'], {'ad_object_writes': 0, 'sends': 0, 'events_emitted': 0, 'scheduler_changes': 0})

    def test_altered_predecessor_refuses_before_any_provider_observation(self):
        weekly = load()
        original = Path.read_bytes

        def tampered(path):
            if path == FIXTURE:
                return original(path) + b'\n'
            return original(path)

        with patch.object(weekly, 'A3', FIXTURE), patch.object(Path, 'read_bytes', tampered):
            with self.assertRaisesRegex(ValueError, 'a3_historical_receipt_drift'):
                # No monitor exists: drift must fail before looking up a client.
                weekly.collect(datetime.now(timezone.utc), {})

    def test_launcher_pins_the_consumed_successor_and_historical_input(self):
        launcher = load(ROOT / 'ops/hermes-cron-scripts/glp_weekly_ads_analytics.py')
        base = '/home/ubuntu/business/'
        files = {
            base + str(SOURCE.relative_to(ROOT)): SOURCE,
            base + str(SOURCE.with_name('SOURCE-PINS.json').relative_to(ROOT)):
                SOURCE.with_name('SOURCE-PINS.json'),
            base + 'reports/ops/orchestration-restore-20260907/PLAN/A3/RESULT.json': FIXTURE,
        }
        for runtime_path, candidate_path in files.items():
            self.assertEqual(launcher.PINS[runtime_path],
                             hashlib.sha256(candidate_path.read_bytes()).hexdigest())
        self.assertFalse(any('result_contract' in p or 'briefs/' in p for p in launcher.PINS))
        # Any source drift still stops the subprocess; no scheduler invocation.
        with patch.object(Path, 'read_bytes', return_value=b'drift'), \
                patch.object(launcher.subprocess, 'run') as invoke:
            with self.assertRaisesRegex(RuntimeError, 'Q25_SOURCE_PIN_DRIFT'):
                launcher.main()
            invoke.assert_not_called()

    def test_schedule_unknown_and_campaign_scopes_stay_held(self):
        weekly = load()
        self.assertEqual(weekly.CAMPAIGNS, {'google': '24204737927',
                         'meta': '120248533692790046', 'microsoft': '571604457'})
        self.assertTrue(weekly.due(datetime(2026, 10, 5, 16, 30, tzinfo=timezone.utc)))
        self.assertFalse(weekly.due(datetime(2026, 10, 5, 16, 31, tzinfo=timezone.utc)))
        self.assertFalse(weekly.due(datetime(2026, 10, 6, 16, 30, tzinfo=timezone.utc)))
        # DST changes UTC binding, not Monday 09:30 PT.
        self.assertTrue(weekly.due(datetime(2026, 11, 9, 17, 30, tzinfo=timezone.utc)))
        self.assertIsNone(weekly.summarize(weekly.unknown('missing'), None)['spend'])
        tree = ast.parse(SOURCE.read_text())
        calls = {node.func.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)}
        self.assertFalse(calls & {'mutate', 'post_discord', 'run', 'update_campaign',
                                'create_campaign', 'collect_n8n_postbacks'})


if __name__ == '__main__':
    unittest.main()
