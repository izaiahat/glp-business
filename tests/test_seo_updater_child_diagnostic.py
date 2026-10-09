"""Offline stdout-only review refusal regression; never imports live updater."""
import ast
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/glp_seo_updater_run.py'


def diagnostic_scope():
    names = {'child_failure_cause', 'last_json_line', 'run_child', 'run_effectful_child', 'ChildFailure'}
    tree = ast.parse(SOURCE.read_text())
    selected = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in names]
    scope = {'Path': Path, 'Any': object, 'subprocess': subprocess, 'json': json,
             'utc_now': lambda: '2026-10-09T00:00:00Z',
             'process_start_ticks': lambda pid: 1}
    scope['persist_bytes'] = lambda path, raw: path.write_bytes(raw) or {'path': str(path)}
    def save(path, value, **kwargs):
        path.write_text(json.dumps(value))
    scope['atomic_json'] = save
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), 'exec'), scope)
    return scope


class ReviewDiagnosticTests(unittest.TestCase):
    def test_stdout_only_review_exhaustion_survives_both_child_paths(self):
        scope = diagnostic_scope()
        payload = {'schema': 'glp-opus-seo-bridge-escalation-v1',
                   'status': 'BLOCKED_OPUS_NONCONVERGENCE', 'publication_invoked': False,
                   'rounds': [{'round': 5, 'verdict': 'BLOCKED_OPUS_SEO_DRAFT'}]}
        command = [sys.executable, '-c', 'import json; print(json.dumps(' + repr(payload) + ')); raise SystemExit(2)']
        for name in ('run_child', 'run_effectful_child'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                kwargs = {'cwd': root, 'output_dir': root}
                if name == 'run_child':
                    kwargs['timeout'] = 10
                with self.assertRaises(scope['ChildFailure']) as caught:
                    scope[name]('review-and-publish', command, **kwargs)
                self.assertEqual(caught.exception.cause, 'review_controller:BLOCKED_OPUS_NONCONVERGENCE')
                receipt = json.loads(caught.exception.receipt.read_text())
                self.assertEqual(receipt['failure_cause'], caught.exception.cause)
                self.assertFalse(receipt['parsed_last_json']['publication_invoked'])
                self.assertEqual(receipt['exit_code'], 2)

    def test_stderr_keeps_priority_and_unrecognized_stdout_is_not_authority(self):
        cause = diagnostic_scope()['child_failure_cause']
        self.assertEqual(cause(b'BLOCKED: exact_gate_failure\n', b'{"status":"PASS"}'), 'exact_gate_failure')
        self.assertEqual(cause(b'', b'{"status":"PASS", "publication_invoked":true}'), 'child_stderr_unclassified')


if __name__ == '__main__':
    unittest.main()
