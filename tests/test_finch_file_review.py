"""Synthetic-only reviewed-file custody. No root writes, real grants or network.

The HTTP fixture denies sockets and uses synthetic credentials. Successful
review tests emulate privileged filesystem ownership only inside tmp_path;
separate tests exercise the real trust-boundary predicate without that shim.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time
from types import ModuleType, SimpleNamespace

import pytest
from test_rook_communication import TestFinchCommunication as _Finch


class TestFinchReviewedFiles:
    setup = _Finch.setup
    callback = _Finch.callback
    secret = _Finch.secret
    http_call = _Finch.http_call
    call = _Finch.call
    subscribe = _Finch.subscribe
    event = _Finch.event
    args = _Finch.args
    ready = _Finch.ready
    reopen = _Finch.reopen

    @pytest.fixture(autouse=True)
    def local_review_fs(self, setup, monkeypatch):
        # Positive review tests model an independently accepted receiving
        # sandbox. Its real kernel predicate has a separate negative suite.
        monkeypatch.setattr(self.m, '_finch_confinement', lambda: None)
        self.review_root = self.root / 'synthetic-privileged-reviews'
        self.review_root.mkdir(mode=0o755)
        monkeypatch.setattr(self.m, 'FINCH_REVIEW_ROOT', self.review_root)
        self.real_review_stat = self.m._review_stat
        def synthetic_root_owned(value, *, directory=False):
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
            # Ordinary tmp dirs are intentionally writable. This is a fixture
            # ownership model, NOT a production bypass or permission change.
            fields['st_uid'] = 0
            if directory:
                fields['st_mode'] &= ~0o022
            return self.real_review_stat(SimpleNamespace(**fields), directory=directory)
        monkeypatch.setattr(self.m, '_review_stat', synthetic_root_owned)
        self.raw = self.cdn = b'Generic market glossary: patient navigation and diagnosis categories.\n'

    def record(self, *, identity='301', channel='901', ready=True):
        if ready:
            self.ready(identity, channel)
        row = self.app.store.get('requests', 'discord-' + identity)
        principal = self.m.Principal(self.cfg.subject, time.time() + 600, frozenset())
        candidate = self.m.upload_candidate(self.args(identity)['file'], finch=True)
        wire = self.args(identity)['text'] + '\n-# finch.reply · discord-' + identity
        binding = self.app.finch_review_binding(principal, row, self.m.file_review_binding(candidate),
                                              hashlib.sha256(wire.encode()).hexdigest())
        record = {'schema': 'finch-whole-file-review-v1', 'binding': binding,
                  'file_author': 'synthetic-author', 'reviewer': 'independent-synthetic-reviewer',
                  'verdict': 'nonsensitive', 'whole_file': True, 'exact_bytes': True,
                  'no_personal_data': True, 'no_secrets': True, 'no_active_content': True,
                  'no_unreviewed_encoding': True, 'review_evidence': 'synthetic-only-review-001',
                  'operator_approval': {'operator_id': self.m.OPERATOR, 'message_id': '777',
                                        'decision': 'approve_exact_reviewed_file'},
                  'issued_at': time.time(), 'expires': min(time.time() + 300, row['expires'])}
        self.write_record(record, identity)
        return record

    def write_record(self, record, identity='301'):
        path = self.review_root / ('discord-' + identity + '.json')
        path.write_text(self.m.canonical(record))
        path.chmod(0o644)
        return path

    def denied(self, args=None):
        status, result = self.call('reply_to_finch_request', args or self.args())
        assert status == 200 and 'error' in result, result
        assert not self.posts
        assert self.app.store.get('requests', 'discord-301')['state'] == 'ready'
        return result

    @pytest.mark.parametrize('channel', ['901', '902'])
    def test_reviewed_exact_file_crosses_http_multipart_and_both_cdn_checks(self, channel, monkeypatch):
        record = self.record(channel=channel)
        checks = []
        real_check = self.m.FinchFileReview.check
        def checked(review, **kw):
            checks.append((id(review), kw))
            return real_check(review, **kw)
        monkeypatch.setattr(self.m.FinchFileReview, 'check', checked)
        status, result = self.call('reply_to_finch_request', self.args())
        assert status == 200 and result['result']['isError'] is False, result
        assert len(self.posts) == 1 and self.posts[0][2] == self.raw
        assert len(checks) >= 9 and len({identity for identity, _ in checks}) == 1
        assert sum('file' in kw for _, kw in checks) == 7
        row = self.app.store.get('requests', 'discord-301')
        assert row['file_review_sha256'] == hashlib.sha256(self.m.canonical(record).encode()).hexdigest()
        assert row['state'] == 'confirmed'
        assert row['attachment']['sha256'] == hashlib.sha256(self.raw).hexdigest()
        assert 'reviewer' not in self.m.canonical(result) and self.raw.decode() not in self.m.canonical(row)
        # Expiration/deletion is not a reason to resend or lose a confirmed receipt.
        (self.review_root / 'discord-301.json').unlink()
        self.reopen()
        before = len(self.http)
        status, replay = self.call('reply_to_finch_request', self.args())
        assert replay == result and len(self.http) == before

    @pytest.mark.parametrize('data', [
        b'patient PRIVATE KEY', b'patient Authorization: x', b'patient Bearer synthetic',
        b'patient password=x', b'patient api_key=x', b'patient client_secret=x',
        b'patient <script>x</script>', b'patient <HTML>x', b'patient /JavaScript',
        b'patient /JS', b'patient /Launch', b'patient /OpenAction', b'patient /AA',
        b'patient /EmbeddedFile', b'patient DOB 2000-01-01', b'patient SSN 000-00-0000'])
    def test_hard_denials_win_before_request_or_review_lookup(self, data, monkeypatch):
        self.record()
        self.raw = data
        monkeypatch.setattr(self.app.store, 'get', lambda *a: pytest.fail('hard denial must precede lookup'))
        status, result = self.call('reply_to_finch_request', self.args())
        assert status == 200 and result['error']['message'] == 'SensitiveOrActiveFileRefused'
        assert not self.posts

    @pytest.mark.parametrize('data', [
        b'patient Jane Example has diagnosis X',
        b'Jane Example <jane@example.invalid>\n' + b'general context\n' * 2000 + b'diagnosis X',
        b'Generic market report\npatient Jane Example has diagnosis X',
        b'patient encoded section: SmFuZSBFeGFtcGxl',
        b'patient \xff', b'patient\x00hidden', b'\xff\xfep\x00a\x00t\x00i\x00e\x00n\x00t\x00'])
    def test_missing_review_person_linked_distant_mixed_and_encoding_denied(self, data):
        self.raw = self.cdn = data
        self.ready()
        self.denied()

    @pytest.mark.parametrize('field,value', [
        ('verdict', 'sensitive'), ('verdict', 'uncertain'), ('whole_file', False), ('exact_bytes', False),
        ('no_personal_data', False), ('no_secrets', False), ('no_active_content', False),
        ('no_unreviewed_encoding', False), ('review_evidence', ''), ('reviewer', 'synthetic-author'),
        ('reviewer', 'auth0|operator'), ('reviewer', '903'), ('schema', 'standing-hash-whitelist')])
    def test_incomplete_uncertain_sensitive_or_nonindependent_review_denied(self, field, value):
        record = self.record()
        record[field] = value
        self.write_record(record)
        self.denied()

    @pytest.mark.parametrize('field,value', [
        ('request_id', 'discord-302'), ('message_id', '302'), ('subject', 'other-subject'),
        ('request_subject', 'other-subject'), ('guild_id', '999'), ('channel_id', '902'),
        ('bot_id', '904'), ('policy_sha256', '0' * 64), ('request_policy_sha256', '0' * 64),
        ('current_policy_sha256', '0' * 64), ('source_sha256', '0' * 64), ('reply_sha256', '0' * 64),
        ('request_created', 1), ('request_expires', 9999999999)])
    def test_wrong_context_denied(self, field, value):
        record = self.record()
        record['binding'][field] = value
        self.write_record(record)
        self.denied()

    @pytest.mark.parametrize('field,value', [
        ('sha256', '0' * 64), ('byte_length', 1), ('filename', 'other.txt'),
        ('content_type', 'application/json')])
    def test_wrong_file_binding_denied(self, field, value):
        record = self.record()
        record['binding']['file'][field] = value
        self.write_record(record)
        self.denied()

    @pytest.mark.parametrize('kind', ['expired', 'future', 'before-request', 'beyond-request', 'nan', 'no-approval', 'wrong-operator'])
    def test_expiry_and_operator_approval_required(self, kind):
        record = self.record()
        if kind == 'expired': record['expires'] = time.time() - 1
        if kind == 'future': record['issued_at'] = time.time() + 5
        if kind == 'before-request': record['issued_at'] = record['binding']['request_created'] - 1
        if kind == 'beyond-request': record['expires'] = record['binding']['request_expires'] + 1
        if kind == 'nan': record['expires'] = float('nan')
        if kind == 'no-approval': record.pop('operator_approval')
        if kind == 'wrong-operator': record['operator_approval']['operator_id'] = '999'
        self.write_record(record)
        self.denied()

    @pytest.mark.parametrize('kind', ['file-bytes', 'filename', 'mime', 'reply-text', 'client-grant'])
    def test_changed_client_input_cannot_reuse_review(self, kind):
        self.record()
        if kind == 'file-bytes': self.raw += b' changed'
        if kind == 'filename': self.filename = 'other.txt'
        if kind == 'mime': self.filename, self.content_type, self.raw = 'notes.json', 'application/json', b'"patient"'
        args = self.args()
        if kind == 'reply-text': args['text'] = 'different'
        if kind == 'client-grant': args['file']['review'] = {'verdict': 'nonsensitive'}
        self.denied(args)

    @pytest.mark.parametrize('kind', ['symlink', 'directory-symlink', 'hardlink', 'writable', 'duplicate-field', 'app-owned'])
    def test_untrusted_local_record_refused(self, kind, monkeypatch):
        record = self.record()
        path = self.review_root / 'discord-301.json'
        if kind == 'symlink':
            target = self.review_root / 'target.json'; path.rename(target); path.symlink_to(target)
        if kind == 'directory-symlink':
            target = self.root / 'other'; self.review_root.rename(target); self.review_root.symlink_to(target)
        if kind == 'hardlink': os.link(path, self.review_root / 'second.json')
        if kind == 'writable': path.chmod(0o666)
        if kind == 'duplicate-field': path.write_text('{"schema":"x",' + self.m.canonical(record)[1:])
        if kind == 'app-owned': monkeypatch.setattr(self.m, '_review_stat', self.real_review_stat)
        self.denied()

    @pytest.mark.parametrize('kind', ['replace', 'rewrite', 'unlink', 'source', 'bootstrap', 'policy', 'request', 'expired'])
    def test_change_after_loading_refuses_post_and_never_retries(self, kind, monkeypatch):
        self.record()
        real_sender = self.app.sender
        def sender(channel, wire, file, *, file_review):
            path = self.review_root / 'discord-301.json'
            if kind == 'replace':
                replacement = self.review_root / 'new.json'; replacement.write_bytes(path.read_bytes()); replacement.replace(path)
            if kind == 'rewrite': path.write_bytes(path.read_bytes() + b' ')
            if kind == 'unlink': path.unlink()
            if kind == 'source': monkeypatch.setattr(self.m, 'FINCH_SOURCE_SHA256', '0' * 64)
            if kind == 'bootstrap': monkeypatch.setattr(self.m, '_FINCH_SOURCE_BOOTSTRAP_OK', False)
            if kind == 'policy': self.app.policy = '0' * 64
            if kind == 'request':
                row = self.app.store.get('requests', 'discord-301'); row['message_id'] = '302'; self.app.store.put('requests', row['id'], row)
            if kind == 'expired': monkeypatch.setattr(self.m.time, 'time', lambda: file_review.expires + 1)
            return real_sender(channel, wire, file, file_review=file_review)
        self.app.sender = sender
        status, result = self.call('reply_to_finch_request', self.args())
        assert status == 200 and result['result']['isError'] is True, result
        assert not self.posts
        assert self.app.store.get('requests', 'discord-301')['state'] == 'uncertain'

    @pytest.mark.parametrize('fault', ['timeout', 'attachment', 'author', 'channel', 'guild', 'cdn-bytes'])
    def test_uncertain_receipt_no_replay_after_restart(self, fault):
        self.record()
        if fault == 'cdn-bytes': self.cdn = b'patient changed\n'.ljust(len(self.raw), b' ')
        else: self.fault = fault
        status, result = self.call('reply_to_finch_request', self.args())
        assert status == 200 and result['result']['isError'] is True, result
        assert len(self.posts) == 1
        self.reopen()
        (self.review_root / 'discord-301.json').unlink()
        before = len(self.http)
        assert self.call('reply_to_finch_request', self.args())[1] == result
        assert len(self.http) == before

    def test_same_hash_review_for_another_request_does_not_authorize(self):
        self.record()
        self.ready('302')
        (self.review_root / 'discord-302.json').write_bytes((self.review_root / 'discord-301.json').read_bytes())
        status, result = self.call('reply_to_finch_request', self.args('302'))
        assert status == 200 and 'error' in result and not self.posts

    def test_no_context_or_nonfinch_context_cannot_bypass_validator(self):
        self.record()
        candidate = self.m.upload_candidate(self.args()['file'], finch=True)
        for finch in (False, True):
            with pytest.raises(self.m.RpcError):
                self.m.validate_file(candidate['filename'], candidate['content_type'], candidate['data'], finch=finch)
            with pytest.raises(self.m.RpcError):
                self.m.multipart_body({}, candidate, finch=finch)

    @pytest.mark.parametrize('mode,uid,nlink,directory', [
        (stat.S_IFREG | 0o644, 1000, 1, False), (stat.S_IFREG | 0o666, 0, 1, False),
        (stat.S_IFLNK | 0o777, 0, 1, False), (stat.S_IFREG | 0o644, 0, 2, False),
        (stat.S_IFDIR | 0o777, 0, 1, True), (stat.S_IFDIR | 0o755, 1000, 1, True)])
    def test_real_custody_predicate_rejects_untrusted_files_and_parents(self, mode, uid, nlink, directory):
        value = SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=nlink)
        with pytest.raises(ValueError): self.real_review_stat(value, directory=directory)

    def test_adapter_root_is_never_review_authority(self, monkeypatch):
        monkeypatch.setattr(self.m.os, 'geteuid', lambda: 0)
        with pytest.raises(ValueError): self.real_review_stat(SimpleNamespace())

    @pytest.mark.parametrize('stale', [False, True])
    def test_source_bootstrap_compares_actually_executing_code(self, stale):
        source = Path(self.m.__file__).read_bytes()
        path = self.root / 'synthetic_source.py'
        path.write_bytes(source + (b'\n_SYNTHETIC_NEW_CODE = True\n' if stale else b''))
        module = ModuleType('synthetic_finch_source'); module.__file__ = str(path)
        sys.modules[module.__name__] = module
        try:
            # Execute old compiled bytes, as with a stale resident module/pyc.
            exec(compile(source, str(path), 'exec', dont_inherit=True), module.__dict__)
            assert module._FINCH_SOURCE_BOOTSTRAP_OK is (not stale)
            assert module.FINCH_SOURCE_SHA256 == hashlib.sha256(path.read_bytes()).hexdigest()
            path.write_bytes(path.read_bytes() + b'\n_SYNTHETIC_DISK_DRIFT = True\n')
            def no_later_check(_): pytest.fail('disk source drift must refuse before context')
            review = module.FinchFileReview('{}', b'{}', (), no_later_check, time.time() + 60, time.time() + 60)
            with pytest.raises(module.RpcError, match='FileReviewContextMismatch'): review.check()
        finally:
            sys.modules.pop(module.__name__, None)
