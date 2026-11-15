"""SYNTHETIC TEST ONLY: CBS scheduled research retry and Alexandria prefetch."""
import importlib.util
import contextlib
import io
import sys
import types
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'ops' / 'hermes-cron-scripts'


def load(name):
    spec = importlib.util.spec_from_file_location(name, SOURCE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


context = load('cbs_pool_research_context')
alexandria = load('cbs_pool_alexandria')
NOW = datetime(2026, 11, 16, 22, 45, tzinfo=timezone.utc)
DATA = {'season': 2026, 'week': 10, 'schedule_games': [{'game_id': 'a', 'teams': ['LAC', 'BAL']}]}
EVENT = {'key': '2026:10:prelock:test', 'kind': 'prelock', 'expires_at': (NOW + timedelta(minutes=75)).isoformat()}
PLAN = {'due_event': EVENT, 'expires_at': EVENT['expires_at']}


class CBSResearchTest(unittest.TestCase):
    def root(self, directory):
        root = Path(directory)
        (root / 'config.json').write_text(json.dumps({'research_enabled': True, 'mock_mode': True,
            'access_scope': 'read_only', 'auto_submit_enabled': False}))
        return root

    def test_identical_due_event_retries_after_failed_agent_but_not_before_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory)
            reader, collector = Mock(), Mock(return_value={'status': 'executed'})
            decide = Mock(return_value=PLAN)  # identical due output, no hash change
            first = context.prepare(root, NOW, lambda: DATA, reader, decide, collector)
            # Model now fails outside pre-run: event remains uncompleted.
            suppressed = context.prepare(root, NOW + timedelta(minutes=15), lambda: DATA, reader, decide, collector)
            retry = context.prepare(root, NOW + timedelta(minutes=30), lambda: DATA, reader, decide, collector)
            self.assertTrue(first['wakeAgent'])
            self.assertFalse(suppressed['wakeAgent'])
            self.assertTrue(retry['wakeAgent'])
            state = json.loads((root / 'experiment-state.json').read_text())
            self.assertEqual(state['attempts'][EVENT['key']]['count'], 2)
            self.assertNotIn('completed_events', state)
            self.assertEqual(reader.call_count, 2)

    def test_idle_disables_model_and_alexandria_while_stale_snapshot_can_refresh_rollover(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory)
            reader, collector = Mock(), Mock()
            decide = Mock(return_value={'due_event': None})
            idle = context.prepare(root, NOW, lambda: DATA, reader, decide, collector)
            self.assertFalse(idle['wakeAgent'])
            reader.assert_not_called()
            collector.assert_not_called()
            (root / 'snapshot-latest.json').write_text(json.dumps({'retrieved_at': (NOW - timedelta(hours=7)).isoformat()}))
            context.prepare(root, NOW, lambda: DATA, reader, decide, collector)
            reader.assert_called_once()
            collector.assert_not_called()

    def client(self, fail=False):
        def request(endpoint, payload, **kwargs):
            self.assertEqual(endpoint, 'scrape')  # REST /find-tools is a 404
            entry = payload['alexandria'][0]
            provider, capability = entry['provider'], entry['capability']
            if capability == 'find-tools':
                self.assertNotIn('includeCosts', entry['options'])  # MCP-only option is refused by REST
                provider = entry['options']['providers'][0]
                capability = entry['options']['capabilities'][0]
                body = {'items': [{'provider': provider, 'capability': capability, 'creditsCost': 5}]}
                return {'success': True, 'data': {'alexandria': [{'data': body}]}}
            if fail and capability.endswith('/projections'):
                return {'success': True, 'data': {'alexandria': [{'provider': provider, 'capability': capability, 'error': {'code': 'upstream'}}]}}
            if capability.endswith('/schedule'):
                body = {'season': 2026, 'week': 10, 'games': [{'away_team': {'abbreviation': 'LAC'}, 'home_team': {'abbreviation': 'BAL'}}]}
            elif capability == 'sports-league-data/injury_report':
                body = {'season': 2026, 'week': 10, 'reports': [{'player': 'TEST ONLY'}], 'report_date': '2026-11-14'}
            elif capability.endswith('/projections'):
                body = {'week': 10, 'players': [{'player': 'TEST ONLY'}]}
            else:
                body = {'injuries': []}
            return {'success': True, 'data': {'alexandria': [{'provider': provider, 'capability': capability, 'data': body, 'creditsCost': 5}]}}
        return Mock(request=Mock(side_effect=request))

    def test_due_prefetch_runs_four_contracts_and_calls_once_even_after_model_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.client()
            first = alexandria.collect(DATA, EVENT, directory, client)
            second = alexandria.collect(DATA, EVENT, directory, client)
            self.assertEqual(first, second)
            self.assertEqual(first['status'], 'executed')
            self.assertEqual(len(first['calls']), 4)
            self.assertEqual(client.request.call_count, 8)
            self.assertEqual(sum(r['creditsCost'] for r in first['calls']), 20)
            self.assertEqual(first['calls'][1]['count'], 1)  # official injuries use reports, not injuries

    def test_inner_provider_error_is_incomplete_and_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            client = self.client(fail=True)
            result = alexandria.collect(DATA, EVENT, directory, client)
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['calls'][-1]['status'], 'error_or_unknown')
            alexandria.collect(DATA, EVENT, directory, client)
            self.assertEqual(client.request.call_count, 8)
            with self.assertRaises(ValueError):
                alexandria.collect(DATA, dict(EVENT, key='different'), directory, client)

    def test_schedule_identity_and_official_report_shape_regression(self):
        payload = {'success': True, 'data': {'alexandria': [{'provider': 'nfl-com',
            'capability': 'sports-league-data/injury_report', 'data': {'reports': [], 'report_date': '2026-11-14'}}]}}
        receipt = alexandria.validate_response(payload, 'nfl-com', 'sports-league-data/injury_report', DATA)
        self.assertEqual(receipt['count'], 0)
        self.assertEqual(receipt['source_update'], '2026-11-14')
        payload['data']['alexandria'][0].update(capability='sports-league-data/schedule', data={'season': 2026, 'week': 9, 'games': []})
        with self.assertRaises(ValueError):
            alexandria.validate_response(payload, 'nfl-com', 'sports-league-data/schedule', DATA)

    @unittest.skipUnless(hasattr(os, 'memfd_create'), 'sealed invocation is Linux-specific')
    def test_sealed_cron_snapshot_imports_installed_helper_and_stays_idle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory)
            scripts = root / 'profile/scripts'
            scripts.mkdir(parents=True)
            shutil.copy2(SOURCE / 'cbs_pool_alexandria.py', scripts)
            (root / 'research_input.py').write_text('def refresh_local(): return {}\n')
            (root / 'experiment_schedule.py').write_text('def gate(*args): return {"status":"idle"}\n')
            (root / 'firecrawl_client.py').write_text('class FirecrawlClient: pass\n')
            text = (SOURCE / 'cbs_pool_research_context.py').read_text().replace(
                "ROOT = Path('/home/ubuntu/projects/cbs-confidence-pool')", 'ROOT = Path(' + repr(str(root)) + ')')
            fd = os.memfd_create('synthetic-cbs-cron')
            try:
                os.write(fd, text.encode())
                env = dict(os.environ, HERMES_HOME=str(root / 'profile'))
                result = subprocess.run([sys.executable, '/proc/self/fd/' + str(fd)],
                    pass_fds=(fd,), env=env, capture_output=True, text=True, timeout=20)
            finally:
                os.close(fd)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIs(json.loads(result.stdout)['wakeAgent'], False)
            self.assertEqual(json.loads(result.stdout)['status'], 'IDLE')

    def test_stdout_wake_gate_is_single_line_json_for_real_cron_parser(self):
        modules = {
            'research_input': types.SimpleNamespace(refresh_local=Mock()),
            'experiment_schedule': types.SimpleNamespace(gate=Mock()),
            'firecrawl_client': types.SimpleNamespace(FirecrawlClient=Mock()),
            'cbs_pool_alexandria': alexandria,
        }
        output = io.StringIO()
        with patch.dict(sys.modules, modules), patch.object(sys, 'argv', ['script']), \
             patch.object(context, 'prepare', return_value={'status': 'IDLE', 'wakeAgent': False}), \
             contextlib.redirect_stdout(output):
            context.main()
        lines = output.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIs(json.loads(lines[-1])['wakeAgent'], False)

    def test_manual_review_does_not_recreate_or_complete_expired_scheduled_event(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.root(directory)
            result = context.prepare(root, NOW, lambda: DATA, Mock(), lambda *a: {'due_event': None},
                                     lambda *a: {'status': 'executed'}, manual=True)
            self.assertEqual(result['event']['kind'], 'manual')
            self.assertTrue(result['wakeAgent'])
            self.assertFalse((root / 'experiment-state.json').exists())
            self.assertFalse((root / 'active-research-context.json').exists())


if __name__ == '__main__':
    unittest.main()
