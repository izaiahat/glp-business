"""Offline synthetic CDP events; no live browser or historical payloads."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import gpt6pro_relay as relay

CID = '00000000-0000-0000-0000-000000000001'
DATA = b'original synthetic bytes'
DIGEST = hashlib.sha256(DATA).hexdigest()


def begin(guid='g', frame='frame', name='OUTPUT-MANIFEST.json'):
    return {'method': 'Browser.downloadWillBegin', 'params': {
        'guid': guid, 'frameId': frame, 'suggestedFilename': name,
        'url': 'https://example.invalid/synthetic'}}


def progress(state='completed', guid='g', **extra):
    return {'method': 'Browser.downloadProgress', 'params': {
        'guid': guid, 'state': state, 'totalBytes': len(DATA),
        'receivedBytes': len(DATA), **extra}}


class NativeReceiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.st = {'job': 'synthetic-job', 'account': 'synthetic', 'status': 'done', 'binding_version': 2,
                   'conversation_id': CID, 'conversation_url': 'https://chatgpt.com/c/' + CID,
                   'input_sha256': 'a' * 64, 'last_text_sha': 'b' * 64}
        self.dest = self.root / 'downloads'
        self.target = self.dest / 'OUTPUT-MANIFEST.json'
        self.jobs = self.root / 'jobs'
        self.calls = []
        self.clicks = 0
        self.journal = self.jobs / self.st['job'] / 'native-download-OUTPUT-MANIFEST.json.json'
        self.addCleanup(patch.stopall)
        patch.object(relay, 'JOBS', self.jobs).start()

    def harness(self, batches=(), *, stale=(), fail=None, restore_fail=False, data=DATA):
        batches = iter([list(stale), [], *batches])
        tick = [0]
        def monotonic():
            tick[0] += 1
            return tick[0]
        def drain():
            events = next(batches, [])
            if any(e['params'].get('state') == 'completed' for e in events) and data is not None:
                self.target.write_bytes(data)
            return events
        def cdp(method, **kwargs):
            self.calls.append((method, kwargs))
            if method == 'Page.getFrameTree':
                return {'frameTree': {'frame': {'id': 'frame'}}}
            if kwargs.get('behavior') == 'default' and restore_fail:
                raise RuntimeError('synthetic restoration failure')
            if kwargs.get('behavior') == 'allow' and fail == 'enable':
                raise RuntimeError('synthetic enable failure')
            return {}
        def bound(tab, url, expression):
            self.assertEqual(url, self.st['conversation_url'])
            if expression == 'location.href':
                return url
            if 'Open preview' in expression:
                return {'opened': True}
            receipt = json.loads(self.journal.read_text())
            self.assertTrue(receipt['click_attempted'])
            self.assertEqual(receipt['input_sha256'], 'a' * 64)
            self.clicks += 1
            if fail == 'click':
                raise RuntimeError('synthetic lost click response')
            return {'clicked': True}
        def bu(code, **kwargs):
            compile(code, '<emitted receiver>', 'exec')
            if 'receipt_path=' not in code:
                cdp('Browser.setDownloadBehavior', behavior='default', eventsEnabled=False)
                return {'restored': True}
            env = {'json': json, 'time': relay.time, 'cdp': cdp, 'drain_events': drain, '_bound_js': bound,
                   '_select': lambda tab: None, '_sessions': {'target': 'session'}}
            with patch.object(relay.time, 'monotonic', monotonic), patch.object(relay.time, 'sleep'):
                exec(code[code.index('receipt_path='):], env)
            return {'result': env['receipt']['result']}
        return bu

    def receive(self, bu):
        with patch.object(relay, 'bu_json', bu):
            return relay.receive_native_download(self.st, 'target', self.target.name,
                                                  self.dest, DIGEST)

    def assert_blocked(self):
        with patch.object(relay, 'bu_json') as bu:
            with self.assertRaisesRegex(ValueError, 'PRIOR_INTENT'):
                relay.receive_native_download(self.st, 'target', self.target.name, self.dest, DIGEST)
            bu.assert_not_called()

    def test_missing_capture_unknown_no_reclick(self):
        receipt = self.receive(self.harness())
        self.assertEqual(receipt['click_result'], {'clicked': True})
        self.assertEqual(receipt['result'], 'UNKNOWN')
        self.assertTrue(receipt['restoration']['restored'])
        self.assertEqual(self.clicks, 1)
        self.assert_blocked()

    def test_completed_guid_destination_hash(self):
        receipt = self.receive(self.harness([[begin(), progress(filePath=str(self.target))]]))
        self.assertEqual(receipt['result'], 'COMPLETED')
        self.assertEqual(receipt['guid'], 'g')
        self.assertEqual(receipt['sha256'], DIGEST)
        self.assertEqual(len(receipt['events']), 2)
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
        self.assertTrue(any(k.get('eventsEnabled') is True for _, k in self.calls))
        self.assert_blocked()

    def test_cross_job_frame_never_attributed(self):
        receipt = self.receive(self.harness([[begin(frame='other'), progress(filePath=str(self.target))]]))
        self.assertEqual(receipt['result'], 'UNKNOWN')
        self.assertNotIn('guid', receipt)

    def test_prior_guid_and_late_progress_not_accepted(self):
        receipt = self.receive(self.harness([[progress(filePath=str(self.target))],
                                            [begin(), progress(filePath=str(self.target))]],
                                           stale=[begin()]))
        self.assertEqual(receipt['result'], 'UNKNOWN')
        self.assertNotIn('guid', receipt)

    def test_destination_mismatch_is_error(self):
        receipt = self.receive(self.harness([[begin(), progress(filePath=str(self.root / 'wrong'))]]))
        self.assertEqual(receipt['result'], 'ERROR')
        self.assertEqual(receipt['reason'], 'NATIVE_DESTINATION_MISMATCH')

    def test_canceled_is_not_unknown_or_completed(self):
        receipt = self.receive(self.harness([[begin(), progress('canceled')]]))
        self.assertEqual(receipt['result'], 'CANCELED')
        self.assert_blocked()

    def test_payload_mismatch_not_completed(self):
        receipt = self.receive(self.harness([[begin(), progress(filePath=str(self.target))]], data=b'wrong'))
        self.assertEqual(receipt['result'], 'ERROR')
        self.assertEqual(receipt['reason'], 'NATIVE_FILE_HASH')

    def test_enable_and_uncertain_click_always_restore(self):
        for fail, result in [('enable', 'ERROR'), ('click', 'UNKNOWN')]:
            with self.subTest(fail=fail):
                self.st['job'] = 'synthetic-' + fail
                self.journal = self.jobs / self.st['job'] / 'native-download-OUTPUT-MANIFEST.json.json'
                receipt = self.receive(self.harness(fail=fail))
                self.assertEqual(receipt['result'], result)
                self.assertTrue(receipt['restoration']['restored'])

    def test_delayed_current_guid_is_collected_without_second_click(self):
        receipt = self.receive(self.harness([[], [], [begin()], [],
                                            [progress('inProgress')],
                                            [progress(filePath=str(self.target))]]))
        self.assertEqual(receipt['result'], 'COMPLETED')
        self.assertEqual(self.clicks, 1)

    def test_completed_without_native_destination_stays_unknown(self):
        receipt = self.receive(self.harness([[begin(), progress()]]))
        self.assertTrue(self.target.is_file())
        self.assertEqual(receipt['result'], 'UNKNOWN')
        self.assert_blocked()

    def test_multiple_matching_guids_never_choose_first(self):
        receipt = self.receive(self.harness([[begin(), begin('other'),
                                             progress(filePath=str(self.target))]]))
        self.assertEqual(receipt['result'], 'ERROR')
        self.assertEqual(receipt['reason'], 'AMBIGUOUS_NATIVE_GUID')

    def test_lost_wrapper_capture_still_restores_and_blocks_retry(self):
        def lost(code, **kwargs):
            if 'receipt_path=' in code:
                raise TimeoutError('synthetic IPC timeout')
            return {'restored': True}
        receipt = self.receive(lost)
        self.assertEqual(receipt['result'], 'UNKNOWN')
        self.assertTrue(receipt['restoration']['restored'])
        self.assertEqual(receipt['transport_error'], 'TimeoutError')
        self.assert_blocked()

    def test_maintained_fetch_entry_and_original_verifier(self):
        import types
        from test_gpt6pro_relay_binding import BindingTests, relay as subject
        case = BindingTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.prepare_bundle()
        directory = case.root / 'native'
        directory.mkdir()
        api = case.svc.files.return_value
        blobs = {n: api.get_media(fileId=i).execute() for n, i in
                 [('OUTPUT-MANIFEST.json', 'manifest'), ('answer.diff', 'artifact')]}
        job_before = subject.job_state('job')
        response_before = (subject.JOBS / 'job/RESPONSE.md').read_bytes()
        names = []
        def receive(st, tab, name, dest, digest):
            self.assertEqual(hashlib.sha256(blobs[name]).hexdigest(), digest)
            (dest / name).write_bytes(blobs[name])
            names.append(name)
            return {'result': 'COMPLETED', 'restoration': {'restored': True},
                    'preview_restoration': {'restored': True}}
        with patch.object(subject, 'receive_native_download', receive):
            self.assertEqual(subject.cmd_fetch_outbox(types.SimpleNamespace(
                job='job', download_dir=directory, receive_native=True)), 0)
        self.assertEqual(names, ['OUTPUT-MANIFEST.json', 'answer.diff'])
        self.assertEqual((subject.JOBS / 'job/RESPONSE.md').read_bytes(), response_before)
        self.assertEqual(subject.job_state('job')['input_sha256'], job_before['input_sha256'])
        self.assertEqual(subject.job_state('job')['fetch_verification']['state'], 'VERIFIED')
        subject.drive.assert_not_called()

    def test_restore_failure_does_not_mask_transfer(self):
        receipt = self.receive(self.harness([[begin(), progress('canceled')]], restore_fail=True))
        self.assertEqual(receipt['result'], 'CANCELED')
        self.assertFalse(receipt['restoration']['restored'])


if __name__ == '__main__':
    unittest.main()
