import datetime as dt
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/claude_cloud_relay.py'
spec = importlib.util.spec_from_file_location('claude_cloud_relay', SOURCE)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


class RelayTests(unittest.TestCase):
    def test_foreign_pr_refused(self):
        job = dict(job_id='job-123', repo='izaiahat/glp-site-candidate', base_branch='main')
        pr = dict(headRefName='claude-cloud/other-job', baseRefName='main',
                  headRepositoryOwner={'login': 'izaiahat'}, headRepository={'name': 'glp-site-candidate'},
                  isCrossRepository=False, state='OPEN', isDraft=False)
        with self.assertRaisesRegex(RuntimeError, 'FOREIGN_PR_BINDING'):
            relay.verify_pr(job, pr)

    def test_credit_exhausted_refused(self):
        snapshot = self.snapshot(0)
        with self.assertRaisesRegex(RuntimeError, 'CREDIT_EXHAUSTED_OR_EXPIRED'):
            relay.billing_gate(snapshot, self.auth(), 'account@example.com')

    def test_overage_refused(self):
        snapshot = self.snapshot(250)
        snapshot['overage_enabled'] = True
        with self.assertRaisesRegex(RuntimeError, 'OVERAGE_COULD_BILL'):
            relay.billing_gate(snapshot, self.auth(), 'account@example.com')

    def test_unknown_billing_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'BILLING_READBACK_UNVERIFIED'):
            relay.billing_gate({}, self.auth(), 'account@example.com')

    def test_forbidden_class_refused(self):
        with mock.patch.object(relay, 'cfg', return_value={'allowed_classes': ['test-fix', 'patch-draft', 'review']}):
            with self.assertRaisesRegex(RuntimeError, 'FORBIDDEN_JOB_CLASS'):
                relay.prompt_policy('do it', 'deploy')
            with self.assertRaisesRegex(RuntimeError, 'FORBIDDEN_PROMPT_ACTION'):
                relay.prompt_policy('Please deploy', 'review')

    def test_monitor_lock_prevents_overlap(self):
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / 'monitor.lock'
            first = relay.locked(lock)
            try:
                with self.assertRaisesRegex(RuntimeError, 'LOCK_BUSY'):
                    relay.locked(lock)
            finally:
                first.close()
            second = relay.locked(lock)
            second.close()

    def test_landing_refuses_unverified_session_message(self):
        job = dict(job_id='job-123', repo='izaiahat/glp-site-candidate', base_branch='main',
                   job_class='patch-draft', status='running')
        pr = dict(headRefName='claude-cloud/job-123', baseRefName='main',
                  headRepositoryOwner={'login': 'izaiahat'}, headRepository={'name': 'glp-site-candidate'},
                  isCrossRepository=False, state='OPEN', isDraft=False, url='https://github.com/izaiahat/glp-site-candidate/pull/2')
        with tempfile.TemporaryDirectory() as d:
            jf = Path(d) / 'JOB.json'
            jf.write_text(json.dumps(job))
            with mock.patch.object(relay, 'cfg', return_value={}), \
                 mock.patch.object(relay, 'job_file', return_value=jf), \
                 mock.patch.object(relay, 'gh_prs', return_value=[pr]), \
                 mock.patch.object(relay, 'append_jsonl') as emitted:
                with self.assertRaisesRegex(RuntimeError, 'SESSION_FINAL_MESSAGE_READBACK_UNAVAILABLE'):
                    relay.land_one('job-123')
                emitted.assert_not_called()

    def auth(self):
        return {'loggedIn': True, 'authMethod': 'claude.ai', 'email': 'account@example.com'}

    def snapshot(self, remaining):
        return {'account': 'account@example.com', 'source': 'claude.ai/settings/usage live readback',
                'observed_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'credit_remaining_usd': remaining, 'overage_enabled': False, 'auto_reload_enabled': False,
                'included_usage_available': True}


if __name__ == '__main__':
    unittest.main()
