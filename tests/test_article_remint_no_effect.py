import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts import cron_bridge_action as cron


class ArticleRemintNoEffectTests(unittest.TestCase):
    def test_remint_failure_requires_no_artifacts_sealed_base_and_live_head(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            operation = 'n8n-article-carrier-2026-10-08'
            digest = hashlib.sha256(operation.encode()).hexdigest()
            workspace = root / 'reports/glp-clinic-scout/seo-machine/article-buffer-work' / digest
            workspace.mkdir(parents=True)
            base = root / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
            base.write_text('{}')
            base_sha = hashlib.sha256(base.read_bytes()).hexdigest()
            report = workspace / 'publication-prior'
            report.mkdir()
            (report / 'release-job.json').write_text('{}')
            template = workspace / 'prior-template.json'
            template.write_text(json.dumps({'report_dir': str(report)}))
            materialization = {
                'operation_id': operation, 'generation': 1,
                'current_base_state': {'path': str(base), 'sha256': base_sha},
                'materialized': {'apply_template': {'path': str(template),
                    'sha256': hashlib.sha256(template.read_bytes()).hexdigest()}},
            }
            canonical = (json.dumps(materialization, sort_keys=True, separators=(',', ':'), ensure_ascii=False) + '\n').encode()
            materialization['materialization_sha256'] = hashlib.sha256(canonical).hexdigest()
            prior = workspace / 'materialization-r1.json'
            prior.write_text(json.dumps(materialization))
            run_id = f'buffer-{digest[:16]}-r2'
            candidate = root / 'site-system/flagship-v2/releases' / f'glp-buffer-2026-10-08-{digest[:16]}-r2'
            flags = {'--packet': str(workspace / 'packet'), '--topic-id': 'fixture',
                     '--route': '/guides/fixture/', '--run-id': run_id, '--release-root': str(candidate),
                     '--publication-date': '2026-10-08', '--selection-receipt': 'selection',
                     '--topic-authority-snapshot': 'authority', '--materialization-reason': 'remint',
                     '--pt-date': '2026-10-08'}
            child = {'argv': ['python', 'materialize_opus_run.py', *[x for pair in flags.items() for x in pair]], 'exit_code': 1}
            receipt = {'operation_id': operation, 'accepted_failures': {'materialization': str(prior)},
                       'head_reconciliation': {'aligned': True, 'pointer_head': {'deployment_id': 'live'},
                                               'live_head': {'deployment_id': 'live'}}}
            with patch.object(cron, 'ROOT', root), patch.object(cron, 'article_current_base', return_value={'deployment_id': 'live', 'pointer_sha256': base_sha}), patch.object(cron, 'article_production_head_id', return_value='live') as head:
                self.assertTrue(cron.article_materialization_failed_no_effect(receipt, child))
                for artifact in (candidate, workspace / f'apply-template-{run_id}.json', workspace / f'publication-{run_id}'):
                    artifact.parent.mkdir(parents=True, exist_ok=True)
                    artifact.write_text('possibly effectful')
                    self.assertFalse(cron.article_materialization_failed_no_effect(receipt, child))
                    artifact.unlink()
                head.return_value = 'drift'
                self.assertFalse(cron.article_materialization_failed_no_effect(receipt, child))
                head.return_value = 'live'
                base.write_text('{"drift":true}')
                self.assertFalse(cron.article_materialization_failed_no_effect(receipt, child))
                base.write_text('{}')
                materialization['generation'] = 9
                prior.write_text(json.dumps(materialization))
                self.assertFalse(cron.article_materialization_failed_no_effect(receipt, child))


if __name__ == '__main__':
    unittest.main()
