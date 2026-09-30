"""One focused communication acceptance group. No sockets or credentials."""
import base64
from copy import deepcopy
import hashlib
import json
import socket
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from test_rook_mcp_events import module, config, rpc


class TestRookCommunication:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        self.m = m = module()
        monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('socket denied'))
        monkeypatch.setattr(socket.socket, 'bind', lambda *a, **k: pytest.fail('bind denied'))
        monkeypatch.setattr(m, 'load_token', lambda: pytest.fail('credential denied'))
        import post_discord_message
        monkeypatch.setattr(post_discord_message, 'load_token', lambda: pytest.fail('credential denied'))
        self.cfg = config(m)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        key = jwt.PyJWK.from_json(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()), algorithm='RS256')
        class Keys:
            def get_signing_key_from_jwt(_, token): return key
        self.app = m.Application(self.cfg, tmp_path / 'private', auth=m.Authenticator(self.cfg, Keys()))
        self.messages = {'101': self.message('101')}
        self.thread = {'id': '201', 'guild_id': m.GUILD, 'parent_id': m.CHANNEL, 'type': 11,
                       'thread_metadata': {'archived': False, 'locked': False}}
        self.calls = []
        yield
        self.app.close()

    def message(self, identity, channel=None, content='original'):
        return {'id': identity, 'channel_id': channel or self.m.CHANNEL, 'author': {'id': self.m.BOT},
                'content': content, 'timestamp': '2026-09-30T00:00:00Z', 'attachments': [], 'reactions': []}

    def request(self, method, path, payload=None, file=None):
        self.calls.append((method, path, deepcopy(payload), file))
        channel = path.split('/')[2]
        if path == '/channels/201': return deepcopy(self.thread)
        if method == 'GET' and '/messages/' in path:
            return deepcopy(self.messages[path.split('/')[4]])
        if method == 'GET' and '/messages' in path: return list(deepcopy(self.messages).values())
        raise AssertionError((method, path))

    def token(self, **overrides):
        scope = overrides.get('scope', 'rook.channel.read rook.channel.write rook.requests.read rook.replies.write')
        claims = {'iss': self.cfg.issuer, 'aud': self.cfg.audience, 'sub': self.cfg.subject,
                  'iat': int(time.time()), 'exp': int(time.time()) + 600, 'scope': scope, 'permissions': scope.split()}
        claims.update(overrides)
        return jwt.encode(claims, self.key, algorithm='RS256')

    def call(self, name, arguments, scope=None, permissions=None, subject=None):
        scope = scope or ('rook.channel.read' if name.startswith('read_') else 'rook.channel.write')
        claims = {'iss': self.cfg.issuer, 'aud': self.cfg.audience, 'sub': subject or self.cfg.subject,
                  'iat': int(time.time()), 'exp': int(time.time()) + 600, 'scope': scope,
                  'permissions': permissions if permissions is not None else scope.split()}
        token = jwt.encode(claims, self.key, algorithm='RS256')
        return self.app.handle('POST', '/mcp', {'Authorization': 'Bearer ' + token,
                               'MCP-Protocol-Version': self.m.VERSION},
                               rpc(self.m, 'tools/call', {'name': name, 'arguments': arguments}))

    def value(self, name, arguments):
        status, _, body = self.call(name, arguments)
        assert status == 200 and 'error' not in body, body
        assert not body['result'].get('isError'), body
        return body['result']['structuredContent']

    def test_insufficient_scope_403_tool_challenge_without_effects(self, monkeypatch):
        """Authentic identity + RBAC can step up scope; RBAC denial cannot consent-loop."""
        m = self.m
        import io
        class MemorySocket:
            def __init__(self, raw): self.input = io.BytesIO(raw); self.output = bytearray()
            def makefile(self, *args): return self.input
            def settimeout(self, timeout): pass
            def sendall(self, data): self.output.extend(data)
        def denied(*args, **kwargs):
            pytest.fail('denied authorization attempted dispatch, persistence or external effect')
        def handle(body, authorization, extra=None):
            headers = {'MCP-Protocol-Version': m.VERSION, **(extra or {})}
            if authorization is not None:
                headers['Authorization'] = authorization
            return self.app.handle('POST', '/mcp', headers, body)
        legacy = 'rook.requests.read rook.replies.write'
        with monkeypatch.context() as guard:
            guard.setattr(self.app, 'dispatch', denied)
            guard.setattr(self.app.store, 'put', denied)
            guard.setattr(self.app.store, 'delete', denied)
            guard.setattr(m, 'discord_request', denied)
            for definition in m.COMMUNICATION_TOOLS:
                name = definition['name']
                required = definition['securitySchemes'][0]['scopes'][0]
                token = 'Bearer ' + self.token(scope=legacy, permissions=[required])
                body = rpc(m, 'tools/call', {'name': name, 'arguments': {}})
                status, headers, result = handle(body, token)
                assert status == 403
                challenge = headers['WWW-Authenticate']
                assert challenge.startswith('Bearer ')
                assert 'error="insufficient_scope"' in challenge
                assert 'error_description="' in challenge
                assert f'scope="{required}"' in challenge
                assert f'resource_metadata="{self.cfg.protected_metadata_url}"' in challenge
                assert result == {'jsonrpc': '2.0', 'id': body['id'], 'result': {
                    'resultType': 'complete', 'isError': True,
                    'content': [{'type': 'text', 'text': 'Additional channel scope required: ' + required}],
                    '_meta': {'mcp/www_authenticate': [challenge]}}}
                wire = m.canonical(body).encode()
                raw = (f'POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nAuthorization: {token}\r\n'
                       f'MCP-Protocol-Version: {m.VERSION}\r\nContent-Type: application/json\r\n'
                       f'Content-Length: {len(wire)}\r\n\r\n').encode() + wire
                sock = MemorySocket(raw)
                m.make_handler(self.app, {'127.0.0.1:8765'})(sock, ('127.0.0.1', 1), object())
                http_headers, http_body = bytes(sock.output).split(b'\r\n\r\n', 1)
                assert http_headers.startswith(b'HTTP/1.1 403 Forbidden\r\n')
                assert ('WWW-Authenticate: ' + challenge).encode() in http_headers
                assert json.loads(http_body) == result
                no_scope = jwt.decode(self.token(permissions=[required]), self.key.public_key(), algorithms=['RS256'],
                                      audience=self.cfg.audience, issuer=self.cfg.issuer)
                no_scope.pop('scope')
                assert handle(body, 'Bearer ' + jwt.encode(no_scope, self.key, algorithm='RS256')) == (status, headers, result)
                # A role assignment is not repaired by renewed user consent, even if scope is also missing.
                for scope in (legacy, required):
                    for permissions in ([], None, required, [123]):
                        status, headers, result = handle(body, 'Bearer ' + self.token(scope=scope, permissions=permissions))
                        assert status == 403 and result['error'] == 'forbidden'
                        assert 'WWW-Authenticate' not in headers and 'result' not in result
                for authorization in (None, 'Bearer invalid', 'Bearer ' + self.token(exp=1),
                                      'Bearer ' + self.token(iss='https://other.example/'),
                                      'Bearer ' + self.token(aud='other'), 'Bearer ' + self.token(sub='auth0|other'),
                                      'Bearer ' + jwt.encode({**no_scope, 'scope': None}, self.key, algorithm='RS256')):
                    status, headers, result = handle(body, authorization)
                    assert status == 401 and result == {'error': 'unauthorized'}
                    assert 'WWW-Authenticate' in headers
                malformed = []
                for delta in ({'id': True}, {'id': None}, {'id': []}, {'jsonrpc': '1.0'}, {'extra': 1}):
                    malformed.append({**deepcopy(body), **delta})
                no_id = deepcopy(body); no_id.pop('id'); malformed.append(no_id)
                for delta in ({'arguments': []}, {'arguments': None}, {'extra': 1}, {'_meta': None}):
                    invalid = deepcopy(body); invalid['params'].update(delta); malformed.append(invalid)
                for delta in ({'io.modelcontextprotocol/protocolVersion': '2025-03-26'},
                              {'io.modelcontextprotocol/clientCapabilities': None}):
                    invalid = deepcopy(body); invalid['params']['_meta'].update(delta); malformed.append(invalid)
                for invalid in malformed:
                    status, _, result = handle(invalid, token)
                    assert status == 403 and 'result' not in result
                for extra in ({'MCP-Protocol-Version': '2025-03-26'},
                              {'Mcp-Method': 'tools/list'}, {'Mcp-Name': 'other'}):
                    status, _, result = handle(body, token, extra)
                    assert status == 403 and 'result' not in result
                assert handle(body, token, {'Origin': 'https://other.example'}) == (403, {}, {'error': 'origin_refused'})
                metadata = m.Application(m.Config(self.cfg.issuer, self.cfg.audience, None,
                                                  self.cfg.protected_metadata_url, metadata_only=True))
                try:
                    assert metadata.store is metadata.auth is None
                    assert metadata.handle('POST', '/mcp', {'Authorization': token, 'MCP-Protocol-Version': m.VERSION}, body)[0] == 401
                finally:
                    metadata.close()
            # Unidentified tools and discovery never acquire communication-tool result metadata.
            for method, params, required in (('tools/call', {'name': 'unknown', 'arguments': {}}, 'rook.replies.write'),
                                             ('tools/call', {'name': [], 'arguments': {}}, 'rook.replies.write'),
                                             ('tools/list', {}, 'rook.requests.read')):
                status, _, result = handle(rpc(m, method, params), 'Bearer ' + self.token(scope='', permissions=[required]))
                assert status == 403 and 'result' not in result
        assert all(not self.app.store.all(table) for table in ('subscriptions', 'requests', 'deliveries', 'operations'))
        monkeypatch.setattr(m, 'discord_request', self.request)
        assert self.value('read_rook_messages', {'message_id': '101'})['messages'][0]['content'] == 'original'
        assert len(self.calls) == 1 and self.calls[0][0] == 'GET'

    def test_history_scope_context_and_bounded_original_data(self, monkeypatch):
        m = self.m
        monkeypatch.setattr(m, 'discord_request', self.request, raising=False)
        args = {'message_id': '101'}
        got = self.value('read_rook_messages', args)
        assert got['messages'][0]['content'] == 'original'
        assert got['messages'][0]['url'] == f'https://discord.com/channels/{m.GUILD}/{m.CHANNEL}/101'
        assert not self.app.store.all('requests')  # History is not replay/ingest.
        for scopes, perms, subject in [('rook.requests.read rook.replies.write', None, None),
                                      ('rook.channel.read', [], None), ('rook.channel.read', None, 'auth0|other')]:
            assert self.call('read_rook_messages', args, scopes, perms, subject)[0] == (401 if subject else 403)
        for bad in ({'channel_id': '999'}, {'message_id': '101', 'limit': 1}, {'limit': True},
                    {'limit': 26}, {'before_message_id': '0'}):
            assert 'error' in self.call('read_rook_messages', bad)[2]

        self.messages['101']['channel_id'] = '201'
        assert self.value('read_rook_messages', {'thread_id': '201', 'message_id': '101'})['channel_id'] == '201'
        for delta in ({'guild_id': '999'}, {'parent_id': '999'}, {'type': 12}):
            old = deepcopy(self.thread); self.thread.update(delta)
            assert 'error' in self.call('read_rook_messages', {'thread_id': '201'})[2]
            self.thread = old
        self.messages = {str(300 + i): self.message(str(300 + i), content='雪' * 2000) for i in range(25)}
        got = self.value('read_rook_messages', {'limit': 25, 'before_message_id': '999'})
        assert len(m.canonical(got).encode()) <= 65000
        assert got['truncated'] and len(got['messages']) < 25
        assert self.calls[-1][1].endswith('?limit=25&before=999')
        metadata = self.app.handle('GET', '/.well-known/oauth-protected-resource/mcp', {}, None)[2]
        assert metadata['scopes_supported'] == ['rook.requests.read', 'rook.replies.write', 'rook.channel.read', 'rook.channel.write', 'offline_access']

    def test_message_literal_links_custody_conflict_and_uncertainty(self, monkeypatch):
        m = self.m
        def request(method, path, payload=None, file=None):
            if method == 'POST':
                self.calls.append((method, path, payload, file))
                assert self.app.store.all('operations')[-1]['state'] == 'sending'
                assert payload['allowed_mentions'] == {'parse': [], 'replied_user': False}
                assert payload['flags'] == 4
                self.messages['102'] = self.message('102', content=payload['content'])
                self.messages['102']['message_reference'] = deepcopy(payload.get('message_reference', {}))
                return deepcopy(self.messages['102'])
            return self.request(method, path, payload, file)
        monkeypatch.setattr(m, 'discord_request', request, raising=False)
        args = {'operation_id': 'send_literal_link_001', 'text': '@everyone https://example.com/no-fetch', 'reply_to_message_id': '101'}
        got = self.value('send_rook_message', args)
        assert got['kind'] == 'rook.message' and got['message_id'] == '102'
        assert self.value('send_rook_message', args) == got
        assert len([c for c in self.calls if c[0] == 'POST']) == 1
        assert 'error' in self.call('send_rook_message', {**args, 'text': 'conflict'})[2]
        assert self.call('send_rook_message', args, 'rook.replies.write')[0] == 403
        for bad in ({**args, 'channel_id': '999'}, {**args, 'text': ''}, {**args, 'text': '😀' * 951},
                    {**args, 'operation_id': 'x'}, {**args, 'reply_to_message_id': '999'}):
            assert 'error' in self.call('send_rook_message', bad)[2]
        for field in ('archived', 'locked'):
            self.thread['thread_metadata'][field] = True
            assert 'error' in self.call('send_rook_message', {**args, 'thread_id': '201'})[2]
            self.thread['thread_metadata'][field] = False
        # Root parser must not promote our independent same-bot communication.
        self.app.callback = lambda *args: (204, b'')
        self.app.store.put('subscriptions', 'fixture', {'id': 'fixture', 'subject': self.cfg.subject,
            'policy': self.app.policy, 'expires': time.time() + 60, 'url': 'https://synthetic.example/events',
            'secret': 'whsec_' + base64.b64encode(b'x' * 32).decode()})
        event = {'t': 'MESSAGE_CREATE', 'd': {**self.messages['102'], 'guild_id': m.GUILD, 'content': 'Rook: echo'}}
        assert self.app.ingest(event, '100') == 0
        assert self.app.ingest({'t': event['t'], 'd': {**event['d'], 'id': '103'}}, '100') == 1
        count = []
        def uncertain(method, path, payload=None, file=None):
            if method == 'POST':
                count.append(1); raise TimeoutError('synthetic ambiguity')
            return self.request(method, path, payload, file)
        monkeypatch.setattr(m, 'discord_request', uncertain)
        bad = {**args, 'operation_id': 'send_uncertain_0001'}
        result = self.call('send_rook_message', bad)[2]['result']
        assert result['isError']
        self.app.close()
        self.app = m.Application(self.cfg, self.app.store.directory, auth=m.Authenticator(self.cfg, self.app.auth.keys))
        assert self.call('send_rook_message', bad)[2]['result'] == result and count == [1]
        assert self.call('send_rook_message', {**bad, 'operation_id': 'changed_uncertain_01'})[2]['result'] == result
        assert count == [1]
        receipts = m.canonical(self.app.store.all('operations'))
        assert args['text'] not in receipts
        mismatch_calls = []
        def mismatched_reference(method, path, payload=None, file=None):
            if method == 'POST':
                mismatch_calls.append(1)
                self.messages['104'] = self.message('104', content=payload['content'])
                self.messages['104']['message_reference'] = {'message_id': '999', 'channel_id': m.CHANNEL}
                return deepcopy(self.messages['104'])
            return self.request(method, path, payload, file)
        monkeypatch.setattr(m, 'discord_request', mismatched_reference)
        mismatch = {**args, 'operation_id': 'mismatch_reference_01', 'text': 'new content'}
        assert self.call('send_rook_message', mismatch)[2]['result']['isError']
        assert self.call('send_rook_message', mismatch)[2]['result']['isError'] and mismatch_calls == [1]

    def test_public_message_threads_duplicate_and_mismatched_readback(self, monkeypatch):
        m = self.m
        self.thread['name'] = 'Bounded discussion'
        def request(method, path, payload=None, file=None):
            if method == 'POST':
                self.calls.append((method, path, payload, file))
                assert path == f'/channels/{m.CHANNEL}/messages/101/threads'
                assert payload == {'name': 'Bounded discussion', 'auto_archive_duration': 60}
                self.messages['101']['thread'] = deepcopy(self.thread)
                return deepcopy(self.thread)
            return self.request(method, path, payload, file)
        monkeypatch.setattr(m, 'discord_request', request)
        args = {'operation_id': 'create_thread_0001', 'source_message_id': '101', 'name': 'Bounded discussion'}
        got = self.value('create_rook_thread', args)
        assert got['thread_id'] == '201' and got['parent_channel_id'] == m.CHANNEL
        assert self.value('create_rook_thread', args) == got
        assert len([c for c in self.calls if c[0] == 'POST']) == 1
        for bad in ({**args, 'thread_id': '201'}, {**args, 'name': '😀' * 51}, {**args, 'name': '  '},
                    {**args, 'source_message_id': '999'}, {**args, 'name': 'different'}):
            assert 'error' in self.call('create_rook_thread', bad)[2]
        assert self.call('create_rook_thread', args, 'rook.channel.read')[0] == 403
        duplicate = {**args, 'operation_id': 'create_thread_0002'}
        assert 'error' in self.call('create_rook_thread', duplicate)[2]
        assert len([c for c in self.calls if c[0] == 'POST']) == 1
        self.messages['101'].pop('thread')
        self.thread['parent_id'] = '999'
        result = self.call('create_rook_thread', {**args, 'operation_id': 'create_thread_0003'})[2]['result']
        assert result['isError']
        assert self.call('create_rook_thread', {**args, 'operation_id': 'create_thread_0003'})[2]['result'] == result

    def test_only_owned_reactions_unicode_custom_toggles_and_uncertainty(self, monkeypatch):
        m = self.m
        def request(method, path, payload=None, file=None):
            if '/reactions/' in path:
                self.calls.append((method, path, payload, file))
                assert path.endswith('/@me') and method in ('PUT', 'DELETE')
                self.messages['101']['reactions'] = ([{'emoji': {'id': None, 'name': '👍'}, 'count': 1, 'me': True}]
                                                       if method == 'PUT' else [])
                return {}
            if '/emojis/' in path:
                return {'id': '501', 'name': 'local', 'available': True}
            return self.request(method, path, payload, file)
        monkeypatch.setattr(m, 'discord_request', request)
        args = {'message_id': '101', 'emoji': '👍', 'present': True}
        got = self.value('set_rook_reaction', args)
        assert got['present'] is True
        assert self.value('set_rook_reaction', args) == got
        assert len([c for c in self.calls if '/reactions/' in c[1]]) == 1
        assert self.value('set_rook_reaction', {**args, 'present': False})['present'] is False
        assert self.value('set_rook_reaction', args)['present'] is True
        for bad in ({**args, 'user_id': '333'}, {**args, 'present': 1}, {**args, 'emoji': '@everyone'},
                    {**args, 'emoji': '../all'}, {**args, 'emoji': '<:remote:502>'}):
            assert 'error' in self.call('set_rook_reaction', bad)[2]
        assert self.call('set_rook_reaction', args, 'rook.channel.read')[0] == 403
        assert 'error' in self.call('set_rook_reaction', {'message_id': '101', 'emoji': '✅', 'present': False})[2]
        def custom(method, path, payload=None, file=None):
            if '/reactions/' in path:
                self.calls.append((method, path, payload, file))
                assert '/local%3A501/@me' in path
                self.messages['101']['reactions'] = [{'emoji': {'id': '501', 'name': 'local'}, 'me': True}]
                return {}
            return request(method, path, payload, file)
        monkeypatch.setattr(m, 'discord_request', custom)
        assert self.value('set_rook_reaction', {**args, 'emoji': '<:local:501>'})['present']
        effects = []
        def uncertain(method, path, payload=None, file=None):
            if '/reactions/' in path:
                effects.append(1); raise TimeoutError()
            return self.request(method, path, payload, file)
        monkeypatch.setattr(m, 'discord_request', uncertain)
        bad = {**args, 'emoji': '✅'}
        assert self.call('set_rook_reaction', bad)[2]['result']['isError']
        assert self.call('set_rook_reaction', bad)[2]['result']['isError']
        assert effects == [1]

    def test_removed_receipt_cannot_adopt_unrelated_same_bot_reaction(self, monkeypatch):
        # Reuse the independent ROOK-COMM-REVIEW-01 sequence with this group's fixtures.
        args = {'message_id': '101', 'emoji': '✅', 'present': True}
        def reaction_peer(method, path, payload=None, file=None):
            if method in {'PUT', 'DELETE'}:
                self.calls.append((method, path, None, file))
                assert path.endswith('/%E2%9C%85/@me')
                self.messages['101']['reactions'] = ([{'emoji': {'id': None, 'name': '✅'}, 'me': True, 'count': 1}]
                                                       if method == 'PUT' else [])
                return {}
            return self.request(method, path, payload, file)
        monkeypatch.setattr(self.m, 'discord_request', reaction_peer)
        assert self.value('set_rook_reaction', args)['present'] is True
        assert self.value('set_rook_reaction', {**args, 'present': False})['present'] is False
        removed_receipts = deepcopy(self.app.store.all('operations'))
        assert len(removed_receipts) == 1
        assert removed_receipts[0]['state'] == 'confirmed' and removed_receipts[0]['present'] is False
        # A distinct sender using the shared bot re-adds after our confirmed removal.
        unrelated = [{'emoji': {'id': None, 'name': '✅'}, 'me': True, 'count': 1}]
        self.messages['101']['reactions'] = deepcopy(unrelated)
        before = [c for c in self.calls if c[0] in {'PUT', 'DELETE'}]
        assert [c[0] for c in before] == ['PUT', 'DELETE']
        for present in (True, False):
            status, _, reply = self.call('set_rook_reaction', {**args, 'present': present})
            assert status == 200 and reply['result']['isError'] is True, reply
            assert [c for c in self.calls if c[0] in {'PUT', 'DELETE'}] == before
            assert self.messages['101']['reactions'] == unrelated
            assert self.app.store.all('operations') == removed_receipts

    def test_bounded_files_metadata_bytes_multipart_and_no_unsafe_sources(self, monkeypatch):
        m = self.m
        raw = b'bounded original notes\n'
        attachment = {'id': '401', 'filename': 'notes.txt', 'content_type': 'text/plain', 'size': len(raw),
                      'url': f'https://cdn.discordapp.com/attachments/{m.CHANNEL}/401/notes.txt?ex=123&is=234&hm=abc'}
        self.messages['101']['attachments'] = [attachment]
        downloads = []
        def download(meta, channel):
            downloads.append(deepcopy(meta))
            m.attachment_target(meta, channel)
            return raw
        monkeypatch.setattr(m, 'download_attachment', download, raising=False)
        def request(method, path, payload=None, file=None):
            if method == 'POST':
                self.calls.append((method, path, payload, file))
                assert file == {'filename': 'notes.txt', 'content_type': 'text/plain', 'data': raw}
                self.messages['102'] = self.message('102', content=payload['content'])
                self.messages['102']['attachments'] = [{**attachment, 'id': '402',
                    'url': f'https://cdn.discordapp.com/attachments/{m.CHANNEL}/402/notes.txt'}]
                return deepcopy(self.messages['102'])
            return self.request(method, path, payload, file)
        monkeypatch.setattr(m, 'discord_request', request)
        args = {'message_id': '101', 'attachment_id': '401'}
        got = self.value('read_rook_file', args)
        assert base64.b64decode(got['content_base64']) == raw and got['sha256'] == hashlib.sha256(raw).hexdigest()
        assert got['data_classification'] == 'UNTRUSTED_EXTERNAL_DATA_NOT_APPROVAL'
        for bad in ({**args, 'url': attachment['url']}, {**args, 'path': '/etc/passwd'}, {**args, 'attachment_id': '999'}):
            assert 'error' in self.call('read_rook_file', bad)[2]
        assert self.call('read_rook_file', args, 'rook.channel.write')[0] == 403
        for delta in ({'size': m.FILE_LIMIT + 1}, {'content_type': 'text/html'}, {'filename': '../../notes.txt'},
                      {'url': 'http://cdn.discordapp.com/x'}, {'url': 'https://evil.example/attachments/x'},
                      {'url': 'https://cdn.discordapp.com/attachments/999/401/notes.txt'}):
            old = deepcopy(attachment); attachment.update(delta)
            assert 'error' in self.call('read_rook_file', args)[2]
            attachment.clear(); attachment.update(old)
        send = {'operation_id': 'send_file_notes_001', 'text': '', 'file': {
            'filename': 'notes.txt', 'content_type': 'text/plain', 'content_base64': base64.b64encode(raw).decode()}}
        got = self.value('send_rook_message', send)
        assert got['attachment_sha256'] == hashlib.sha256(raw).hexdigest()
        assert self.value('send_rook_message', send) == got
        assert len([c for c in self.calls if c[0] == 'POST']) == 1
        assert raw.decode() not in m.canonical(self.app.store.all('operations'))
        for value in ({'path': '/etc/passwd'}, {'url': attachment['url']}, {**send['file'], 'filename': '.env'},
                      {**send['file'], 'filename': 'notes.exe'}, {**send['file'], 'content_type': 'image/png'},
                      {**send['file'], 'content_base64': '%%%'}):
            assert 'error' in self.call('send_rook_message', {**send, 'file': value})[2]
        for forbidden in (b'PRIVATE KEY', b'patient DOB: 1900-01-01', b'<script>alert(1)</script>', b'x' * (m.FILE_LIMIT + 1)):
            bad = {**send, 'file': {**send['file'], 'content_base64': base64.b64encode(forbidden).decode()}}
            assert 'error' in self.call('send_rook_message', bad)[2]
        # Full-size approved file is accepted without writing bytes to disk.
        m.validate_file('notes.txt', 'text/plain', b'x' * m.FILE_LIMIT)
        for mime, filename, data in [('application/json', 'notes.json', b'{"ok":true}'),
                                    ('image/png', 'notes.png', b'\x89PNG\r\n\x1a\nrest'),
                                    ('image/jpeg', 'notes.jpg', b'\xff\xd8\xffrest'),
                                    ('image/webp', 'notes.webp', b'RIFFxxxxWEBPrest'),
                                    ('application/pdf', 'notes.pdf', b'%PDF-1.4\n%%EOF')]:
            m.validate_file(filename, mime, data)

    def test_discord_utf8_mime_parameters_preserve_verified_file_custody(self, monkeypatch):
        """Live MIME spelling; synthetic bytes/transport, actual validation and send/read paths."""
        m = self.m
        raw = b'x' * 54  # Not the live attachment's bytes or hash.
        attachment = {'id': '401', 'filename': 'notes.txt', 'content_type': 'text/plain; charset=utf-8',
                      'size': len(raw), 'url': f'https://cdn.discordapp.com/attachments/{m.CHANNEL}/401/notes.txt'}
        self.messages['101']['attachments'] = [attachment]
        connections = []
        class Response:
            status = 200
            mime: object = 'text/plain; charset=utf-8'
            data = raw
            length = str(len(raw))
            def getheader(self, name, fallback=None):
                return {'Content-Type': self.mime, 'Content-Length': self.length}.get(name, fallback)
            def read(self, maximum): return self.data[:maximum]
        response = Response()
        class Connection:
            def __init__(self, host, address):
                assert host == 'cdn.discordapp.com' and address == '8.8.8.8'
            def request(self, method, path, body=None, headers=None):
                assert method == 'GET' and path.startswith(f'/attachments/{m.CHANNEL}/')
                assert headers is not None and 'Authorization' not in headers
                connections.append(path)
            def getresponse(self): return response
            def close(self): pass
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
        monkeypatch.setattr(m, 'PinnedHTTPSConnection', Connection)
        monkeypatch.setattr(m, 'discord_request', self.request)
        args = {'message_id': '101', 'attachment_id': '401'}
        def read_exact():
            got = self.value('read_rook_file', args)
            assert got['byte_length'] == len(raw) and base64.b64decode(got['content_base64']) == raw
            assert got['sha256'] == hashlib.sha256(raw).hexdigest()
            assert got['data_classification'] == 'UNTRUSTED_EXTERNAL_DATA_NOT_APPROVAL'
        # Metadata/CDN may independently omit the charset or vary token case/quoting/OWS.
        read_exact()
        for metadata_mime in ('text/plain', 'text/plain; charset=utf-8', ' TEXT/PLAIN ; CHARSET="UTF-8" '):
            for cdn_mime in ('text/plain', 'text/plain; charset=utf-8', 'Text/Plain; charset="UTF-8"'):
                attachment['content_type'], response.mime = metadata_mime, cdn_mime
                read_exact()
        valid_mime = 'text/plain; charset=utf-8'
        attachment['content_type'] = response.mime = valid_mime
        invalid_mimes = (None, 123, '', 'text/html; charset=utf-8', 'text/plain; charset=iso-8859-1',
                         'text/plain; charset=us-ascii', 'text/plain; charset=utf8',
                         'text/plain; charset=utf-8; CHARSET=utf-8',
                         'text/plain; charset=utf-8; charset=iso-8859-1',
                         'text/plain; foo=bar', 'text/plain; charset=utf-8; foo=bar',
                         'text/plain;', 'text/plain; charset', 'text/plain; charset=',
                         'text/plain; charset="utf-8', 'text/plain; charset="utf-8"junk',
                         'text/plain; charset=utf-8\r\nX-Test: x', 'text/plain, text/html')
        for bad in invalid_mimes:
            attachment['content_type'] = bad
            before = len(connections)
            assert 'error' in self.call('read_rook_file', args)[2], bad
            assert len(connections) == before  # Refuse unverified metadata before CDN.
            attachment['content_type'], response.mime = valid_mime, bad
            assert 'error' in self.call('read_rook_file', args)[2], bad
            response.mime = valid_mime
        for delta in ({'filename': 'notes.json'}, {'size': m.FILE_LIMIT + 1},
                      {'url': 'https://cdn.discordapp.com/attachments/999/401/notes.txt'}):
            original = deepcopy(attachment); attachment.update(delta)
            assert 'error' in self.call('read_rook_file', args)[2]
            attachment.clear(); attachment.update(original)
        for data in (b'\xff' * 54, b'\x00' * 54, b'<script>' + b'x' * 46, raw + b'x'):
            response.data = data
            assert 'error' in self.call('read_rook_file', args)[2]
        response.data = raw
        for filename, mime, data in (('notes.json', 'application/json; charset=utf-8', b'{"ok":true}'),
                                     ('notes.txt', valid_mime, 'snow: 雪\n'.encode())):
            assert m.validate_file(filename, mime, data)['data'] == data
        for filename, mime, data in (('notes.json', 'application/json; charset=utf-8', b'not-json'),
                                     ('notes.txt', valid_mime, b'\xff'),
                                     ('notes.png', 'image/png; charset=utf-8', b'\x89PNG\r\n\x1a\n'),
                                     ('notes.pdf', 'application/pdf; charset=utf-8', b'%PDF-1.4\n%%EOF')):
            with pytest.raises(m.RpcError): m.validate_file(filename, mime, data)
        def request(method, path, payload=None, file=None):
            if method == 'POST':
                self.calls.append((method, path, payload, file))
                assert file is not None and payload is not None
                assert file['data'] == raw and file['content_type'] == 'text/plain'
                self.messages['102'] = self.message('102', content=payload['content'])
                self.messages['102']['attachments'] = [deepcopy(attachment)]
                return deepcopy(self.messages['102'])
            return self.request(method, path, payload, file)
        monkeypatch.setattr(m, 'discord_request', request)
        send = {'operation_id': 'send_mime_fixture_001', 'text': '', 'file': {
            'filename': 'notes.txt', 'content_type': 'text/plain', 'content_base64': base64.b64encode(raw).decode()}}
        result = self.value('send_rook_message', send)
        assert result['attachment_sha256'] == hashlib.sha256(raw).hexdigest()
        assert self.value('send_rook_message', send) == result
        assert len([c for c in self.calls if c[0] == 'POST']) == 1
        assert self.app.store.all('operations')[0]['state'] == 'confirmed'
        # Failed CDN MIME or exact-byte readback remains uncertain and cannot resend, even after restart.
        for i, (mime, data) in enumerate((('text/plain; charset=iso-8859-1', raw), (valid_mime, b'y' * 54))):
            response.mime, response.data = mime, data
            bad = {**send, 'operation_id': f'send_mime_uncertain_{i:03}', 'text': str(i)}
            uncertain = self.call('send_rook_message', bad)[2]['result']
            assert uncertain['isError']
            count = len([c for c in self.calls if c[0] == 'POST'])
            self.app.close()
            self.app = m.Application(self.cfg, self.app.store.directory, auth=m.Authenticator(self.cfg, self.app.auth.keys))
            assert self.call('send_rook_message', bad)[2]['result'] == uncertain
            assert self.call('send_rook_message', {**bad, 'operation_id': f'changed_mime_uncertain_{i:03}'})[2]['result'] == uncertain
            assert len([c for c in self.calls if c[0] == 'POST']) == count
            assert self.app.store.all('operations')[-1]['state'] == 'uncertain'
        assert self.call('read_rook_file', args, 'rook.channel.write')[0] == 403
        assert self.call('send_rook_message', send, 'rook.channel.read')[0] == 403

    def test_http_file_exception_transport_pin_and_jwt_authority(self, monkeypatch):
        import io
        from email.parser import BytesParser
        from email.policy import default
        m = self.m
        keys = {t['name']: t for t in self.app.dispatch(m.Principal(self.cfg.subject, time.time() + 60, frozenset({'rook.requests.read'})), 'tools/list', {})['tools']}
        assert set(keys) == {'reply_to_rook_request', 'read_rook_messages', 'send_rook_message', 'create_rook_thread', 'read_rook_file', 'set_rook_reaction'}
        for name, tool in keys.items():
            assert tool['inputSchema']['additionalProperties'] is False
            if name != 'reply_to_rook_request':
                required = 'rook.channel.read' if name.startswith('read_') else 'rook.channel.write'
                assert tool['securitySchemes'] == [{'type': 'oauth2', 'scopes': [required]}]
                assert tool['_meta']['securitySchemes'] == tool['securitySchemes']
                assert tool['_meta']['rook/requiredRbacPermissions'] == [required]
                assert 'JWT scope AND RBAC permission ' + required in tool['description']
        calls = []
        class Response:
            status = 200
            data = b'notes'
            mime = 'text/plain'
            length = '5'
            def read(self, maximum): return self.data[:maximum]
            def getheader(self, name, fallback=None):
                return {'Content-Type': self.mime, 'Content-Length': self.length}.get(name, fallback)
        response = Response()
        class Connection:
            def __init__(self, host, address):
                assert host in {'discord.com', 'cdn.discordapp.com'} and address == '8.8.8.8'
            def request(self, method, path, body=None, headers=None): calls.append((method, path, body, headers))
            def getresponse(self): return response
            def close(self): calls.append(('close',))
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
        monkeypatch.setattr(m, 'PinnedHTTPSConnection', Connection)
        monkeypatch.setattr(m, 'load_token', lambda: 'synthetic-test-only')
        meta = {'id': '401', 'filename': 'notes.txt', 'content_type': 'text/plain', 'size': 5,
                'url': f'https://cdn.discordapp.com/attachments/{m.CHANNEL}/401/notes.txt'}
        assert m.download_attachment(meta, m.CHANNEL) == b'notes'
        assert 'Authorization' not in calls[-2][3]
        for status, mime, length, data in [(302, 'text/plain', '5', b'notes'), (200, 'text/html', '5', b'notes'),
                                          (200, 'text/plain', '6', b'notes'), (200, 'text/plain', None, b'notes!')]:
            response.status, response.mime, response.length, response.data = status, mime, length, data
            with pytest.raises(m.RpcError): m.download_attachment(meta, m.CHANNEL)
        response.status, response.mime, response.length, response.data = 200, 'application/json', None, b'{"id":"102"}'
        payload = {'content': 'literal https://example.com', 'allowed_mentions': {'parse': [], 'replied_user': False}, 'flags': 4,
                   'attachments': [{'id': 0, 'filename': 'notes.txt'}]}
        assert m.discord_request('POST', f'/channels/{m.CHANNEL}/messages', payload, {'filename': 'notes.txt', 'content_type': 'text/plain', 'data': b'notes'}) == {'id': '102'}
        sent = calls[-2]
        parsed = BytesParser(policy=default).parsebytes(('Content-Type: ' + sent[3]['Content-Type'] + '\r\n\r\n').encode() + sent[2])
        parts = list(parsed.iter_parts())
        assert json.loads(parts[0].get_payload(decode=True)) == payload
        assert parts[1].get_payload(decode=True) == b'notes' and parts[1].get_filename() == 'notes.txt'
        assert sent[3]['Authorization'] == 'Bot synthetic-test-only'
        for method, path in [('DELETE', f'/channels/{m.CHANNEL}/messages/101'), ('POST', '/users/@me/channels'),
                             ('DELETE', f'/channels/{m.CHANNEL}/messages/101/reactions/all'),
                             ('GET', '/guilds/999/emojis/501')]:
            with pytest.raises(m.RpcError): m.discord_request(method, path)
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))])
        with pytest.raises(m.RpcError): m.download_attachment(meta, m.CHANNEL)
        monkeypatch.setattr(m, 'discord_request', self.request)
        # Scope AND RBAC required on the same existing subject/issuer/resource, actual signed exp enforced.
        headers = {'Authorization': 'Bearer ' + self.token(scope='rook.channel.read', permissions=['rook.replies.write']), 'MCP-Protocol-Version': m.VERSION}
        body = rpc(m, 'tools/call', {'name': 'read_rook_messages', 'arguments': {'message_id': '101'}})
        assert self.app.handle('POST', '/mcp', headers, body)[0] == 403
        for claims in ({'exp': int(time.time()) - 120}, {'sub': 'auth0|wrong'}, {'aud': 'https://wrong.example/mcp'},
                       {'iss': 'https://wrong.example/'}):
            assert self.app.handle('POST', '/mcp', {**headers, 'Authorization': 'Bearer ' + self.token(**claims)}, body)[0] == 401
        # Actual in-memory HTTP parser: 1 MiB upload exception only; other tools keep 32 KiB request cap.
        class MemorySocket:
            def __init__(self, raw): self.input = io.BytesIO(raw); self.output = bytearray()
            def makefile(self, *args): return self.input
            def settimeout(self, timeout): pass
            def sendall(self, data): self.output.extend(data)
        handler = m.make_handler(self.app, {'127.0.0.1:8765'})
        def request(body):
            wire = m.canonical(body).encode()
            raw = (f'POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nAuthorization: Bearer {self.token()}\r\nMCP-Protocol-Version: {m.VERSION}\r\nContent-Type: application/json\r\nContent-Length: {len(wire)}\r\n\r\n'.encode() + wire)
            sock = MemorySocket(raw); handler(sock, ('127.0.0.1', 1), object()); return bytes(sock.output)
        blob = b'x' * m.FILE_LIMIT
        def upload(method, path, payload=None, file=None):
            if method == 'POST':
                assert file['data'] == blob
                self.messages['102'] = self.message('102', content=payload['content'])
                self.messages['102']['attachments'] = [{**meta, 'size': len(blob)}]
                return deepcopy(self.messages['102'])
            return self.request(method, path, payload, file)
        monkeypatch.setattr(m, 'discord_request', upload)
        monkeypatch.setattr(m, 'download_attachment', lambda *args: blob)
        send = {'operation_id': 'send_max_file_0001', 'text': '', 'file': {'filename': 'notes.txt', 'content_type': 'text/plain', 'content_base64': base64.b64encode(blob).decode()}}
        assert request(rpc(m, 'tools/call', {'name': 'send_rook_message', 'arguments': send})).startswith(b'HTTP/1.1 200')
        reply = rpc(m, 'tools/call', {'name': 'reply_to_rook_request', 'arguments': {'request_id': 'x', 'text': 'x' * 33000}})
        assert request(reply).startswith(b'HTTP/1.1 400')
        read = rpc(m, 'tools/call', {'name': 'read_rook_file', 'arguments': {'message_id': '102', 'attachment_id': '401'}})
        status, _, result = self.app.handle('POST', '/mcp', {**headers, 'Authorization': 'Bearer ' + self.token()}, read)
        assert status == 200 and base64.b64decode(result['result']['structuredContent']['content_base64']) == blob
        assert len(m.canonical(result).encode()) < m.FILE_HTTP_LIMIT
