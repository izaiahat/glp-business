"""One focused communication acceptance group. No sockets or credentials."""
from finch_png_acceptance import TestFinchPNGValidator, png_fixture
from finch_cabx_acceptance import TestFinchCaBX
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
        monkeypatch.setattr(m, 'ROOK_TOKEN_PATH', tmp_path / 'no-live-token')
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
                       'owner_id': m.ROOK_BOT,
                       'thread_metadata': {'archived': False, 'locked': False}}
        self.calls = []
        yield
        self.app.close()

    def message(self, identity, channel=None, content='original'):
        return {'id': identity, 'channel_id': channel or self.m.CHANNEL, 'author': {'id': self.m.ROOK_BOT},
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

    def test_dedicated_rook_token_for_all_discord_writes(self, tmp_path, monkeypatch):
        """Exercise real REST/default reply paths with synthetic files, never sockets."""
        import os
        m = self.m
        token_dir = tmp_path / 'secrets'; token_dir.mkdir(mode=0o700)
        token_file = token_dir / 'rook.token'
        token_file.write_text('synthetic-rook-token\n'); token_file.chmod(0o600)
        monkeypatch.setattr(m, 'ROOK_TOKEN_PATH', token_file, raising=False)
        monkeypatch.setattr(m, 'load_token', lambda: 'synthetic-kael-token')
        monkeypatch.setattr(m, 'callback_target', lambda url: ('discord.com', '93.184.216.34', '/'))
        headers_seen = []; payloads_seen = []
        thread = {**self.thread, 'name': 'discussion', 'owner_id': '1554962007059472445'}
        class Connection:
            def __init__(_, host, address):
                assert (host, address) == ('discord.com', '93.184.216.34')
            def request(_, method, path, body=None, headers=None):
                headers_seen.append((method, path, headers['Authorization']))
                payload = json.loads(body) if body else {}
                if method == 'POST': payloads_seen.append(deepcopy(payload))
                if method == 'POST' and path.endswith('/threads'):
                    self.messages['101']['thread'] = deepcopy(thread); _.value = thread
                elif method == 'POST':
                    self.messages['102'] = self.message('102', content=payload['content'])
                    self.messages['102']['author']['id'] = '1554962007059472445'
                    _.value = self.messages['102']
                elif method in {'PUT', 'DELETE'}:
                    self.messages['101']['reactions'] = ([{'emoji': {'id': None, 'name': '👍'}, 'me': True}]
                        if method == 'PUT' else [])
                    _.value = {}
                elif path == '/api/v10/channels/201': _.value = thread
                elif '/emojis/' in path: _.value = {'id': '501', 'name': 'local', 'available': True}
                else: _.value = self.messages[path.split('/')[6]]
            def getresponse(_):
                data = json.dumps(_.value).encode()
                return type('Response', (), {'status': 200, 'read': lambda *a: data})()
            def close(_): pass
        monkeypatch.setattr(m, 'PinnedHTTPSConnection', Connection)
        self.call('send_rook_message', {'operation_id': 'rook_identity_send_01', 'text': 'hello'})
        self.call('create_rook_thread', {'operation_id': 'rook_identity_thread01', 'source_message_id': '101', 'name': 'discussion'})
        self.call('set_rook_reaction', {'message_id': '101', 'emoji': '👍', 'present': True})
        self.call('set_rook_reaction', {'message_id': '101', 'emoji': '👍', 'present': False})
        now = time.time()
        self.app.store.put('requests', 'discord-301', {'id': 'discord-301', 'subject': self.cfg.subject,
            'policy': self.app.policy, 'state': 'ready', 'expires': now + 600})
        self.app.store.put('deliveries', 'delivery-301', {'id': 'delivery-301', 'request_id': 'discord-301'})
        text = '# Answer\n**Readable**\n- @everyone <@' + m.BOT + '>\n```\ncode\n```'
        result = self.call('reply_to_rook_request', {'request_id': 'discord-301', 'text': text}, 'rook.replies.write')
        assert result[2]['result']['isError'] is False
        assert payloads_seen[-1] == {'content': text + '\n-# rook.reply · discord-301',
                                    'allowed_mentions': {'parse': []}}
        writes = [(method, path) for method, path, _ in headers_seen if method != 'GET']
        assert writes == [('POST', f'/api/v10/channels/{m.CHANNEL}/messages'),
            ('POST', f'/api/v10/channels/{m.CHANNEL}/messages/101/threads'),
            ('PUT', f'/api/v10/channels/{m.CHANNEL}/messages/101/reactions/%F0%9F%91%8D/@me'),
            ('DELETE', f'/api/v10/channels/{m.CHANNEL}/messages/101/reactions/%F0%9F%91%8D/@me'),
            ('POST', f'/api/v10/channels/{m.CHANNEL}/messages')]
        assert all(auth == 'Bot synthetic-rook-token' for _, _, auth in headers_seen)
        # Reaction `me` is relative to the authenticated REST reader: GET must also use Rook.
        assert m.discord_request('GET', f'/guilds/{m.GUILD}/emojis/501')['id'] == '501'
        assert headers_seen[-1][2] == 'Bot synthetic-rook-token'
        before = len(headers_seen)
        with pytest.raises(RuntimeError, match='registered_bridge_sender_required'):
            m.send(m.CHANNEL, 'TRUE-BRIDGE FABLE REVIEW REQUEST')
        assert len(headers_seen) == before
        assert m.load_rook_token() == 'synthetic-rook-token'
        for mode in (0o640, 0o604):
            token_file.chmod(mode)
            with pytest.raises(ValueError): m.load_rook_token()
        token_file.chmod(0o600)
        with monkeypatch.context() as guard:
            guard.setattr(m.os, 'getuid', lambda: os.stat(token_file).st_uid + 1)
            with pytest.raises(ValueError): m.load_rook_token()
        token_file.unlink(); token_file.symlink_to(token_dir / 'missing')
        with pytest.raises(ValueError): m.load_rook_token()
        token_file.unlink(); token_file.mkdir(mode=0o700)
        with pytest.raises(ValueError): m.load_rook_token()
        token_file.rmdir(); token_file.write_text(''); token_file.chmod(0o600)
        with pytest.raises(ValueError): m.load_rook_token()
        token_file.write_text('synthetic-rook-token'); token_dir.chmod(0o750)
        with pytest.raises(ValueError): m.load_rook_token()

    def test_rook_author_readback_and_durable_receipts(self, monkeypatch):
        m = self.m; rook = '1554962007059472445'; effects = []
        self.thread.update(name='discussion', owner_id=rook)
        author = [rook]
        def peer(method, path, payload=None, file=None):
            if method == 'POST' and path.endswith('/threads'):
                effects.append(method); self.messages['101']['thread'] = deepcopy(self.thread)
                return deepcopy(self.thread)
            if method == 'POST':
                effects.append(method)
                self.messages['102'] = self.message('102', content=payload['content'])
                self.messages['102']['author']['id'] = author[0]
                return deepcopy(self.messages['102'])
            if method in {'PUT', 'DELETE'}:
                effects.append(method)
                self.messages['101']['reactions'] = [{'emoji': {'id': None, 'name': '👍'}, 'me': True}]
                return {}
            return self.request(method, path, payload, file)
        monkeypatch.setattr(m, 'discord_request', peer)
        assert self.value('send_rook_message', {'operation_id': 'rook_receipt_send_01', 'text': 'hello'})['message_id'] == '102'
        self.value('create_rook_thread', {'operation_id': 'rook_receipt_thread1', 'source_message_id': '101', 'name': 'discussion'})
        self.value('set_rook_reaction', {'message_id': '101', 'emoji': '👍', 'present': True})
        receipts = self.app.store.all('operations')
        assert len(receipts) == 3 and all(row['state'] == 'confirmed' and row['reply_author'] == rook for row in receipts)
        def reply(identity):
            self.app.store.put('requests', identity, {'id': identity, 'subject': self.cfg.subject,
                'policy': self.app.policy, 'state': 'ready', 'expires': time.time() + 600})
            self.app.store.put('deliveries', identity, {'id': identity, 'request_id': identity})
            return {'request_id': identity, 'text': 'answer'}
        args = reply('discord-301')
        assert not self.call('reply_to_rook_request', args, 'rook.replies.write')[2]['result']['isError']
        assert self.app.store.get('requests', 'discord-301')['reply_author'] == rook
        # Old Kael author cannot confirm either a general message or a bound reply.
        author[0] = m.BOT
        for name, bad, scope in (
            ('send_rook_message', {'operation_id': 'kael_receipt_send_01', 'text': 'wrong author'}, 'rook.channel.write'),
            ('reply_to_rook_request', reply('discord-302'), 'rook.replies.write')):
            before = len(effects)
            result = self.call(name, bad, scope)[2]['result']
            assert result['isError'] and self.call(name, bad, scope)[2]['result'] == result
            assert len(effects) == before + 1
        # Thread ownership is also checked, not merely its destination and name.
        self.messages['101'].pop('thread'); self.thread['owner_id'] = m.BOT
        bad_thread = {'operation_id': 'kael_receipt_thread1', 'source_message_id': '101', 'name': 'discussion'}
        before = len(effects)
        result = self.call('create_rook_thread', bad_thread)[2]['result']
        assert result['isError'] and self.call('create_rook_thread', bad_thread)[2]['result'] == result
        assert len(effects) == before + 1

    def test_rook_ingress_loop_guard_preserves_operator_and_kael(self, monkeypatch):
        m = self.m; delivered = []
        self.app.callback = lambda url, body, headers: (delivered.append(json.loads(body)) or (200, b'{}'))
        self.app.store.put('subscriptions', 'sub-test', {'id': 'sub-test', 'subject': self.cfg.subject,
            'policy': self.app.policy, 'expires': time.time() + 600, 'url': 'https://callback.example/events',
            'secret': 'whsec_' + base64.b64encode(b'synthetic-offline-secret-32-bytes!!').decode()})
        def event(identity, author, content='Rook: request'):
            return {'t': 'MESSAGE_CREATE', 'd': {'id': identity, 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                'author': {'id': author}, 'type': 0, 'content': content}}
        assert self.app.ingest(event('301', m.OPERATOR), '100') == 1
        assert self.app.ingest(event('302', m.BOT), '100') == 1
        assert self.app.ingest(event('303', m.OPERATOR, 'not prefixed'), '100') == 0
        before = deepcopy(self.app.store.all('requests'))
        with monkeypatch.context() as guard:
            guard.setattr(self.app.store, 'put', lambda *a: pytest.fail('Rook ingress must not reserve or deliver'))
            assert self.app.ingest(event('304', '1554962007059472445'), '100') == 0
            # Explicit exclusion must win even if the requester allowlist is accidentally aliased.
            guard.setattr(m, 'BOT', '1554962007059472445')
            assert self.app.ingest(event('305', '1554962007059472445'), '100') == 0
        assert len(delivered) == 2 and self.app.store.all('requests') == before

    def test_receive_one_authenticates_dedicated_rook_reply_binding(self, monkeypatch):
        import receive_one_discord_message as r
        m = self.m; rook = '1554962007059472445'; now = time.time()
        scope = r.Scope('discord-301', 'rook.reply', '100', 'author', rook, 2,
            str(self.app.store.directory), self.cfg.issuer, self.cfg.audience, self.cfg.subject)
        content = json.dumps({'kind': 'rook.reply', 'correlation': 'discord-301', 'text': 'answer'})
        event = {'t': 'MESSAGE_CREATE', 'd': {'id': '302', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
            'author': {'id': rook, 'bot': True}, 'type': 0, 'content': content}}
        binding = {'id': 'discord-301', 'state': 'confirmed', 'policy': self.app.policy,
            'auth_method': 'RS256-authenticated-tools/call', 'subject': self.cfg.subject,
            'auth_subject': self.cfg.subject, 'reply_id': '302', 'reply_author': rook,
            'guild_id': m.GUILD, 'channel_id': m.CHANNEL, 'reply_kind': 'rook.reply',
            'auth_expires': now + 600, 'confirmed_at': now, 'reply_sha256': hashlib.sha256(content.encode()).hexdigest()}
        assert r.match_event(event, scope, rook) is None
        self.app.store.put('requests', 'discord-301', binding)
        assert r.ROOK_BOT == m.ROOK_BOT == rook
        assert r.verified_reply_binding(event['d'], scope, rook)
        assert r.match_event(event, scope, rook)['authenticated_reply_binding'] is True
        for field, value in (('state', 'uncertain'), ('policy', 'wrong'), ('auth_method', 'unverified'),
                ('subject', 'other'), ('auth_subject', 'other'), ('reply_id', '303'), ('reply_author', m.BOT),
                ('guild_id', '1'), ('channel_id', '2'), ('reply_kind', 'wrong'), ('id', 'discord-303'),
                ('auth_expires', now - 1), ('confirmed_at', now - 20), ('reply_sha256', 'wrong')):
            self.app.store.put('requests', 'discord-301', {**binding, field: value})
            assert r.match_event(event, scope, rook) is None, field
        self.app.store.put('requests', 'discord-301', binding)
        assert not r.verified_reply_binding(event['d'], scope, m.BOT)
        for delta in ({'id': '303'}, {'content': content.replace('answer', 'forged')}, {'webhook_id': '777'},
                {'guild_id': '1'}, {'channel_id': '2'}, {'author': {'id': m.BOT}}):
            assert r.match_event({**event, 'd': {**event['d'], **delta}}, scope, rook) is None
        self.app.store.path.chmod(0o644)
        assert not r.verified_reply_binding(event['d'], scope, rook)
        self.app.store.path.chmod(0o600)
        with monkeypatch.context() as guard:
            guard.setattr(r.os, 'getuid', lambda: self.app.store.path.stat().st_uid + 1)
            assert not r.verified_reply_binding(event['d'], scope, rook)

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
        assert self.app.ingest({'t': event['t'], 'd': {**event['d'], 'id': '103', 'author': {'id': m.BOT}}}, '100') == 1
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

    def test_discord_attachment_backend_b2_query_is_narrowly_supported(self, monkeypatch):
        """b2 observed in exact authenticated metadata; signing fields/bytes are synthetic."""
        m = self.m
        raw = b'x' * 54
        base = f'https://cdn.discordapp.com/attachments/{m.CHANNEL}/401/notes.txt'
        signed = 'ex=12345678&is=23456789&hm=' + 'a' * 64
        attachment = {'id': '401', 'filename': 'notes.txt', 'content_type': 'text/plain; charset=utf-8',
                      'size': len(raw), 'url': base + '?' + signed + '&backend=b2'}
        # The original defect must fail here, before any transport is reached.
        assert m.attachment_target(attachment, m.CHANNEL) == attachment['url']
        for query in ('', signed, 'backend=b2&' + signed, signed + '&backend=b2'):
            url = base + ('?' + query if query else '')
            assert m.attachment_target({**attachment, 'url': url}, m.CHANNEL) == url
        invalid = ('backend=', 'backend=b3', 'backend=B2', 'backend=s3', 'backend=b2&backend=b2',
                   'backend=b2&backend=b3', 'backend=b2&ex=1&ex=1', 'backend=b2&is=1&is=2',
                   'backend=b2&hm=a&hm=b', 'backend=b2&unknown=a', 'backend=b2&ex=',
                   'backend=b2&ex=nothex', 'backend=b2&ex', 'backend=b2&&ex=1', 'backend=b2&&',
                   'backend=b2;ex=1', 'backend=%62%32', 'back%65nd=b2', 'backend=b2&hm=%zz',
                   'backend=b2&hm=' + 'a' * 257)
        for query in invalid:
            with pytest.raises(m.RpcError, match='UnverifiedAttachmentURL'):
                m.attachment_target({**attachment, 'url': base + '?' + query}, m.CHANNEL)
        for url in (attachment['url'].replace('https:', 'http:'),
                    attachment['url'].replace('cdn.discordapp.com', 'evil.example'),
                    attachment['url'].replace('cdn.discordapp.com', 'user@cdn.discordapp.com'),
                    attachment['url'].replace('/401/', '/402/'), attachment['url'] + '#fragment',
                    attachment['url'].replace('/notes.txt', '/other.txt')):
            with pytest.raises(m.RpcError, match='UnverifiedAttachmentURL'):
                m.attachment_target({**attachment, 'url': url}, m.CHANNEL)
        self.messages['101']['attachments'] = [attachment]
        connections = []
        class Response:
            status = 200
            def getheader(self, name, fallback=None):
                return {'Content-Type': 'text/plain', 'Content-Length': '54'}.get(name, fallback)
            def read(self, limit):
                assert limit == m.FILE_LIMIT + 1
                return raw
        class Connection:
            def __init__(self, host, address):
                connections.append((host, address))
            def request(self, method, path, headers):
                assert method == 'GET' and path == attachment['url'].split('cdn.discordapp.com')[1]
                assert 'Authorization' not in headers
            def getresponse(self): return Response()
            def close(self): pass
        monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))])
        monkeypatch.setattr(m, 'PinnedHTTPSConnection', Connection)
        monkeypatch.setattr(m, 'discord_request', self.request)
        args = {'message_id': '101', 'attachment_id': '401'}
        assert self.call('read_rook_file', args, 'rook.channel.write')[0] == 403
        assert not connections and not self.calls
        got = self.value('read_rook_file', args)
        assert base64.b64decode(got['content_base64']) == raw
        assert got['sha256'] == hashlib.sha256(raw).hexdigest() and got['byte_length'] == 54
        assert connections == [('cdn.discordapp.com', '8.8.8.8')]
        assert all(call[0] == 'GET' for call in self.calls)

    def test_discord_attachment_single_terminal_query_delimiter(self):
        """Exact live metadata shape; signing values redacted with synthetic hex."""
        m = self.m
        base = (f'https://cdn.discordapp.com/attachments/{m.CHANNEL}/'
                '1554939203538653364/rook-communication-test.txt')
        query = 'backend=b2&ex=' + 'a' * 8 + '&is=' + 'b' * 8 + '&hm=' + 'c' * 64
        attachment = {'id': '1554939203538653364', 'filename': 'rook-communication-test.txt',
                      'content_type': 'text/plain; charset=utf-8', 'size': 54,
                      'url': base + '?' + query + '&'}
        assert len(attachment['url']) == 210
        assert m.attachment_target(attachment, m.CHANNEL) == attachment['url']
        assert m.attachment_target({**attachment, 'url': base + '?' + query}, m.CHANNEL) == base + '?' + query
        for invalid in ('&', query + '&&', '&' + query + '&', query.replace('&ex=', '&&ex=') + '&',
                        query + '&backend=b2&', query + '&ex=aa&', query + '&unknown=aa&',
                        query + '&hm=&', query + '&bare&', query.replace('backend=b2', 'backend=b3') + '&',
                        query.replace('backend=b2', '%62ackend=b2') + '&'):
            with pytest.raises(m.RpcError, match='UnverifiedAttachmentURL'):
                m.attachment_target({**attachment, 'url': base + '?' + invalid}, m.CHANNEL)

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
        assert set(keys) == {'reply_to_rook_request', 'read_rook_messages', 'send_rook_message', 'create_rook_thread', 'read_rook_file', 'set_rook_reaction', 'discord_api', 'github_pr_comment'}
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
        monkeypatch.setattr(m, 'load_rook_token', lambda: 'synthetic-test-only')
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


class TestResearchCommunication:
    """Minimum pilot: reply/read only, synthetic identities and transport."""
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        from dataclasses import replace
        self.m = m = module()
        self.root = tmp_path
        self.dot = m.ResearchIdentity('901', '902', tmp_path / 'research.token',
                                     (m.OPERATOR, m.BOT, m.ROOK_BOT))
        self.cfg = replace(config(m), research=self.dot)
        monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network denied'))
        monkeypatch.setattr(socket.socket, 'bind', lambda *a, **k: pytest.fail('bind denied'))
        monkeypatch.setattr(m, 'load_token', lambda: pytest.fail('Kael credential denied'))
        monkeypatch.setattr(m, 'load_rook_token', lambda *a: pytest.fail('real credential denied'))
        self.calls = []
        self.app = m.Application(self.cfg, tmp_path / 'research-state', auth=object(), callback=self.callback,
                                 sender=self.send, reader=self.read)
        self.principal = m.Principal(self.cfg.subject, time.time() + 600,
            frozenset({'research.requests.read', 'research.replies.write', 'research.channel.read',
                       'research.channel.write', 'rook.channel.write'}))
        self.wire = None
        yield
        self.app.close()

    def callback(self, url, body, headers):
        self.calls.append(json.loads(body))
        return 200, b'{}'

    def send(self, channel, wire):
        assert channel == self.dot.channel
        row = self.app.store.get('requests', 'discord-301')
        assert row['state'] == 'sending' and row['policy'] == self.app.policy
        self.wire = wire
        return self.read(channel, '401')

    def read(self, channel, identity):
        return {'id': identity, 'channel_id': channel, 'author': {'id': self.dot.bot}, 'content': self.wire}

    def subscribe(self):
        self.app.store.put('subscriptions', 'sub-test', {'id': 'sub-test', 'subject': self.cfg.subject,
            'policy': self.app.policy, 'expires': time.time() + 600, 'url': 'https://callback.example/events',
            'secret': 'whsec_' + base64.b64encode(b'synthetic-offline-secret-32-bytes!!').decode()})

    def event(self, identity='301', author=None, content=None, **extra):
        return {'t': 'MESSAGE_CREATE', 'd': {'id': identity, 'guild_id': self.m.GUILD,
            'channel_id': self.dot.channel, 'author': {'id': author or self.m.OPERATOR}, 'type': 0,
            'content': content if content is not None else '<@902> bounded assignment', **extra}}

    def test_validated_exact_identity_and_sender_configuration(self):
        from dataclasses import replace
        for delta in ({'channel': self.m.CHANNEL}, {'channel': '01'}, {'bot': self.m.ROOK_BOT},
                      {'bot': 'bad'}, {'allowed_senders': (self.m.OPERATOR,)},
                      {'allowed_senders': (*self.dot.allowed_senders, '999')}, {'token_path': 'relative.token'}):
            with pytest.raises(ValueError): replace(self.dot, **delta)
        assert self.dot.guild == self.m.GUILD

    def test_only_two_tools_advertised_and_forbidden_even_with_write_scope(self, monkeypatch):
        tools = self.app.dispatch(self.principal, 'tools/list', {})['tools']
        assert {t['name'] for t in tools} == {'reply_to_research_request', 'read_research_messages'}
        assert 'thread_id' not in tools[1]['inputSchema']['properties']
        for name in ('discord_api', 'github_pr_comment', 'send_rook_message', 'read_rook_file',
                     'reply_to_rook_request', 'create_rook_thread', 'set_rook_reaction'):
            with pytest.raises(self.m.RpcError, match='ToolNotAllowed'):
                self.app.dispatch(self.principal, 'tools/call', {'name': name, 'arguments': {}})
        assert not self.calls
        events = self.app.dispatch(self.principal, 'events/list', {})['events']
        assert events[0]['name'] == 'discord.research.request.created'
        assert events[0]['inputSchema']['properties']['channel_id']['const'] == self.dot.channel

    def test_subject_and_research_scope_isolation(self):
        m = self.m
        wrong = m.Principal('auth0|other', time.time() + 600, self.principal.scopes)
        rook = m.Principal(self.cfg.subject, time.time() + 600, frozenset({'rook.requests.read', 'rook.replies.write'}))
        for principal in (wrong, rook):
            with pytest.raises(m.RpcError, match='Unauthorized'):
                self.app.dispatch(principal, 'tools/list', {})

    def test_literal_mentions_exact_allowlist_and_no_ack_self_webhook_prefix(self):
        self.subscribe()
        for index, sender in enumerate(self.dot.allowed_senders):
            assert self.app.ingest(self.event(str(301 + index), sender), '100') == 1
        rejected = [self.event('310', self.dot.bot), self.event('311', '999'),
            self.event('312', webhook_id='777'), self.event('313', content='Rook: old trigger'),
            self.event('314', content='Research: no literal mention'),
            self.event('315', content='ack', mentions=[{'id': self.dot.bot}]),
            self.event('316', content='<@902>'), self.event('317', mentions=[]),
            self.event('318', channel_id=self.m.CHANNEL), self.event('319', guild_id='999'),
            self.event('320', self.m.ROOK_BOT, '<@902> acknowledged'),
            self.event('321', self.m.BOT, '<@902> ack'),
            self.event('322', content='<@902> received')]
        for event in rejected: assert self.app.ingest(event, '100') == 0
        assert self.app.ingest(self.event('301'), '100') == 0
        assert [c['data']['sender_id'] for c in self.calls] == list(self.dot.allowed_senders)

    def test_private_state_cannot_repoint_between_dots_or_rook(self):
        from dataclasses import replace
        m = self.m
        self.app.close()
        for cfg in (config(m), replace(self.cfg, research=replace(self.dot, bot='903')),
                    replace(self.cfg, research=replace(self.dot, channel='904')),
                    replace(self.cfg, research=replace(self.dot, token_path=self.root / 'other.token')),
                    replace(self.cfg, audience='https://other.example/mcp')):
            with pytest.raises(ValueError, match='state identity'):
                m.Application(cfg, self.root / 'research-state', auth=object())
        self.app = m.Application(self.cfg, self.root / 'research-state', auth=object())
        rook = m.Application(config(m), self.root / 'rook-state', auth=object())
        rook.store.put('requests', 'old', {'id': 'old'})
        rook.close()
        with pytest.raises(ValueError, match='state identity'):
            m.Application(self.cfg, self.root / 'rook-state', auth=object())

    def test_exact_reply_custody_receiver_and_restart_uncertainty(self):
        from dataclasses import replace
        import receive_one_discord_message as receiver
        self.subscribe()
        assert self.app.ingest(self.event(), '100') == 1
        args = {'request_id': 'discord-301', 'text': 'answer'}
        result = self.app.dispatch(self.principal, 'tools/call', {'name': 'reply_to_research_request', 'arguments': args})
        assert not result['isError'] and self.wire == 'answer\n-# research.reply · discord-301'
        scope = receiver.Scope('discord-301', 'research.reply', '100', 'author', self.dot.bot, 1,
            str(self.root / 'research-state'), self.cfg.issuer, self.cfg.audience, self.cfg.subject,
            research=self.dot)
        event = self.event('401', self.dot.bot, self.wire)
        assert receiver.match_event(event, scope, self.dot.bot)['authenticated_reply_binding']
        assert receiver.match_event(event, scope, self.m.BOT) is None
        assert receiver.match_event(event, replace(scope, research=replace(self.dot, bot='903'), origin_id='903'), '903') is None
        assert self.app.dispatch(self.principal, 'tools/call', {'name': 'reply_to_research_request', 'arguments': args}) == result
        self.subscribe()
        assert self.app.ingest(self.event('302'), '100') == 1
        effects = []
        def fail(channel, wire):
            assert self.app.store.get('requests', 'discord-302')['state'] == 'sending'
            effects.append(wire)
            raise TimeoutError('synthetic uncertainty')
        self.app.sender = fail
        args['request_id'] = 'discord-302'
        uncertain = self.app.dispatch(self.principal, 'tools/call', {'name': 'reply_to_research_request', 'arguments': args})
        assert uncertain['isError'] and len(effects) == 1
        self.app.close()
        self.app = self.m.Application(self.cfg, self.root / 'research-state', auth=object(), sender=fail, reader=self.read)
        assert self.app.dispatch(self.principal, 'tools/call', {'name': 'reply_to_research_request', 'arguments': args}) == uncertain
        assert len(effects) == 1

    def test_receiver_uses_own_bot_and_exact_private_custody(self, monkeypatch):
        import asyncio
        import receive_one_discord_message as receiver
        self.subscribe()
        self.app.ingest(self.event(), '100')
        self.app.reply(self.principal, {'request_id': 'discord-301', 'text': 'answer'})
        scope = receiver.Scope('discord-301', 'research.reply', '100', 'author', self.dot.bot, 1,
            str(self.root / 'research-state'), self.cfg.issuer, self.cfg.audience, self.cfg.subject, research=self.dot)
        tokens = []
        monkeypatch.setattr(self.m, 'load_rook_token', lambda path=None:
            'synthetic-research' if path == self.dot.token_path else pytest.fail('wrong token'))
        def factory(on_event, on_error):
            class Client:
                async def start(_, token, reconnect):
                    tokens.append(token)
                    on_event(self.event('401', self.dot.bot, self.wire), self.dot.bot)
                    await asyncio.Future()
                async def close(_): pass
            return Client()
        result, status = asyncio.run(receiver.receive_one(scope, client_factory=factory))
        assert status == 0 and result['status'] == 'received' and result['authenticated_reply_binding']
        assert tokens == ['synthetic-research']

    def test_research_rest_read_and_reply_use_own_token(self, monkeypatch):
        m = self.m
        self.app.close()
        self.app = m.Application(self.cfg, self.root / 'research-state', auth=object(), callback=self.callback)
        seen = []
        monkeypatch.setattr(m, 'load_rook_token', lambda path=None: 'synthetic-research' if path == self.dot.token_path else pytest.fail('wrong token'))
        monkeypatch.setattr(m, 'callback_target', lambda url: ('discord.com', '93.184.216.34', '/'))
        class Connection:
            def __init__(_, *a): pass
            def request(_, method, path, body=None, headers=None):
                seen.append((method, path, headers['Authorization']))
                if body: self.wire = json.loads(body)['content']
                _.value = self.read(self.dot.channel, '401')
            def getresponse(_):
                data = json.dumps(_.value).encode()
                return type('Response', (), {'status': 200, 'read': lambda *a: data})()
            def close(_): pass
        monkeypatch.setattr(m, 'PinnedHTTPSConnection', Connection)
        read = self.app.dispatch(self.principal, 'tools/call', {'name': 'read_research_messages', 'arguments': {'message_id': '401'}})
        assert read['structuredContent']['channel_id'] == self.dot.channel
        assert f'/{m.GUILD}/{self.dot.channel}/401' in read['structuredContent']['messages'][0]['url']
        with pytest.raises(m.RpcError):
            self.app.dispatch(self.principal, 'tools/call', {'name': 'read_research_messages', 'arguments': {'thread_id': '999'}})
        self.subscribe(); self.app.ingest(self.event(), '100')
        assert not self.app.dispatch(self.principal, 'tools/call', {'name': 'reply_to_research_request',
            'arguments': {'request_id': 'discord-301', 'text': 'answer'}})['isError']
        assert all(auth == 'Bot synthetic-research' and f'/channels/{self.dot.channel}/messages' in path
                   for _, path, auth in seen)

    def test_research_gateway_own_observer_identity(self, monkeypatch):
        import asyncio
        m = self.m
        self.subscribe()
        tokens = []
        class Server:
            def serve_forever(_, **kw): pass
            def shutdown(_): pass
            def server_close(_): pass
        def factory(observer):
            def make(on_event, on_error):
                class Client:
                    async def start(_, token, reconnect):
                        tokens.append(token)
                        assert reconnect is False
                        on_event(self.event(), observer)
                        await asyncio.Future()
                    async def close(_): pass
                return Client()
            return make
        monkeypatch.setattr(m, 'load_rook_token', lambda path=None: 'synthetic-research' if path == self.dot.token_path else pytest.fail('wrong token'))
        assert asyncio.run(m.run_adapter(self.app, Server(), '100', .05, client_factory=factory(self.dot.bot))) == 0
        assert len(self.calls) == 1 and tokens == ['synthetic-research']
        assert asyncio.run(m.run_adapter(self.app, Server(), '100', .05, client_factory=factory(m.BOT))) == 2
        assert len(self.calls) == 1

    def test_rs256_research_http_auth_and_tool_denials(self):
        m = self.m
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jwt.PyJWK.from_json(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()), algorithm='RS256')
        class Keys:
            def get_signing_key_from_jwt(_, token): return jwk
        self.app.auth = m.Authenticator(self.cfg, Keys())
        scopes = ' '.join(self.principal.scopes)
        def request(name, **overrides):
            claims = {'iss': self.cfg.issuer, 'aud': self.cfg.audience, 'sub': self.cfg.subject,
                'iat': int(time.time()), 'exp': int(time.time()) + 600,
                'scope': scopes, 'permissions': scopes.split()}
            claims.update(overrides)
            return self.app.handle('POST', '/mcp', {'Authorization': 'Bearer ' + jwt.encode(claims, key, algorithm='RS256'),
                'MCP-Protocol-Version': m.VERSION}, rpc(m, 'tools/call', {'name': name, 'arguments': {}}))
        for name in ('discord_api', 'github_pr_comment'):
            status, _, result = request(name)
            assert status == 200 and result['error']['message'] == 'ToolNotAllowed'
        for delta in ({'aud': 'https://other.example/mcp'}, {'sub': 'auth0|other'},
                      {'iss': 'https://other.example/'}, {'exp': int(time.time()) - 300}):
            assert request('read_research_messages', **delta)[0] == 401
        for delta in ({'scope': 'rook.channel.read', 'permissions': ['rook.channel.read']},
                      {'permissions': []}):
            assert request('read_research_messages', **delta)[0] == 403
        metadata = self.app.handle('GET', '/.well-known/oauth-protected-resource/mcp', {}, None)[2]
        assert set(metadata['scopes_supported']) == {'research.requests.read', 'research.replies.write',
                                                    'research.channel.read', 'offline_access'}

    def test_research_cli_identity_inputs_are_required(self):
        import argparse
        m = self.m
        parser = argparse.ArgumentParser()
        m.add_research_arguments(parser)
        assert m.research_from_args(parser.parse_args([])) is None
        with pytest.raises(ValueError):
            m.research_from_args(parser.parse_args(['--profile', 'research']))
        args = ['--profile', 'research', '--research-channel', self.dot.channel,
                '--research-bot', self.dot.bot, '--research-token-path', str(self.dot.token_path)]
        for sender in self.dot.allowed_senders:
            args.extend(['--allowed-request-sender', sender])
        assert m.research_from_args(parser.parse_args(args)) == self.dot
        with pytest.raises(ValueError):
            m.research_from_args(parser.parse_args(args[2:]))
        with pytest.raises(ValueError):
            m.research_from_args(parser.parse_args(args + ['--allowed-request-sender', '999']))


class TestFinchCommunication:
    """Approved two-binding reply/artifact lane; real HTTP/REST/CDN code, synthetic peers."""
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        from dataclasses import replace
        import socket
        self.m = m = module()
        self.root = tmp_path
        token_dir = tmp_path / 'tokens'; token_dir.mkdir(mode=0o700)
        token = token_dir / 'finch.token'; token.write_text('synthetic-finch-token'); token.chmod(0o600)
        self.dot = m.FinchIdentity('901', '902', '903', token, (m.OPERATOR, m.BOT, m.ROOK_BOT))
        self.cfg = replace(config(m), audience='https://finch.example/mcp', finch=self.dot)
        self.cursors = {'901': '100', '902': '200'}
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jwt.PyJWK.from_json(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()), algorithm='RS256')
        class Keys:
            def get_signing_key_from_jwt(_, token): return jwk
        self.keys = Keys()
        self.callbacks = []; self.http = []; self.posts = []; self.messages = {}
        self.raw = b'bounded source evidence\n'; self.cdn = self.raw
        self.filename, self.content_type = 'notes.txt', 'text/plain; charset=utf-8'
        self.fault = None
        monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network denied'))
        monkeypatch.setattr(socket.socket, 'bind', lambda *a, **k: pytest.fail('bind denied'))
        monkeypatch.setattr(m, 'load_token', lambda: pytest.fail('Kael credential denied'))
        monkeypatch.setattr(m, 'callback_target', lambda url:
            (url.split('/')[2], '8.8.8.8', '/' + url.split('/', 3)[3] if len(url.split('/', 3)) == 4 else '/'))
        outer = self
        class Response:
            status = 200
            def __init__(_, value, cdn=False): _.value, _.cdn = value, cdn
            def read(_, limit):
                data = outer.cdn if _.cdn else json.dumps(_.value).encode()
                return data[:limit]
            def getheader(_, name, fallback=None):
                return {'Content-Type': outer.content_type, 'Content-Length': str(len(outer.raw))}.get(name, fallback)
        class Connection:
            def __init__(_, host, address): _.host = host; assert address == '8.8.8.8'
            def request(_, method, path, body=None, headers=None):
                outer.http.append((_.host, method, path, headers))
                if _.host != 'discord.com':
                    assert _.host == 'cdn.discordapp.com' and method == 'GET' and 'Authorization' not in headers
                    _.response = Response(None, True); return
                assert headers['Authorization'] == 'Bot synthetic-finch-token'
                assert path.startswith('/api/v10/channels/')
                channel = path.split('/')[4]
                assert channel in outer.dot.channels
                if method == 'POST':
                    from email.parser import BytesParser
                    from email.policy import default
                    file = None
                    if headers['Content-Type'].startswith('multipart/'):
                        parsed = BytesParser(policy=default).parsebytes(
                            ('Content-Type: ' + headers['Content-Type'] + '\r\n\r\n').encode() + body)
                        parts = list(parsed.iter_parts()); assert len(parts) == 2
                        payload = json.loads(parts[0].get_payload(decode=True))
                        file = parts[1].get_payload(decode=True)
                        assert file == outer.raw and parts[1].get_filename() == outer.filename
                    else: payload = json.loads(body)
                    request_id = payload['content'].split(' · ')[-1]
                    row = outer.app.store.get('requests', request_id)
                    assert row['state'] == 'sending' and row['channel_id'] == channel
                    mentions = payload['allowed_mentions']
                    assert mentions['parse'] == [] and mentions['replied_user'] is False
                    assert set(mentions) <= {'parse', 'replied_user', 'users'}
                    assert mentions.get('users', []) in ([], [m.BOT], [m.ROOK_BOT], [m.BOT, m.ROOK_BOT])
                    assert payload['flags'] == 4
                    outer.posts.append((channel, payload, file))
                    if outer.fault == 'timeout': raise TimeoutError('synthetic ambiguity')
                    identity = str(500 + len(outer.posts))
                    value = {'id': identity, 'channel_id': channel, 'author': {'id': outer.dot.bot},
                             'content': payload['content'], 'attachments': []}
                    if file is not None:
                        value['attachments'] = [{'id': '601', 'filename': outer.filename, 'content_type': outer.content_type,
                            'size': len(file), 'url': f'https://cdn.discordapp.com/attachments/{channel}/601/{outer.filename}?backend=b2&ex=aa&is=bb&hm=cc&'}]
                    if outer.fault == 'author': value['author']['id'] = m.ROOK_BOT
                    if outer.fault == 'channel': value['channel_id'] = '902' if channel == '901' else '901'
                    if outer.fault == 'guild': value['guild_id'] = '999'
                    if outer.fault == 'attachment': value['attachments'][0]['filename'] = 'other.txt'
                    outer.messages[(channel, identity)] = deepcopy(value)
                elif '/messages/' in path:
                    identity = path.split('/')[6]
                    value = outer.messages.get((channel, identity), {'id': identity, 'channel_id': channel,
                        'author': {'id': m.OPERATOR}, 'content': 'original', 'attachments': []})
                else: value = [v for (c, _), v in outer.messages.items() if c == channel][:25]
                _.response = Response(value)
            def getresponse(_): return _.response
            def close(_): pass
        monkeypatch.setattr(m, 'PinnedHTTPSConnection', Connection)
        self.app = m.Application(self.cfg, tmp_path / 'finch-state', auth=m.Authenticator(self.cfg, self.keys), callback=self.callback)
        yield
        self.app.close()

    def callback(self, url, body, headers):
        from standardwebhooks.webhooks import Webhook
        Webhook(self.secret()).verify(body, headers)
        value = json.loads(body)
        self.callbacks.append((url, value))
        if value.get('name') == 'discord.finch.request.created':
            row = self.app.store.get('requests', value['data']['request_id'])
            sub = self.app.store.get('subscriptions', value['subscriptionId'])
            assert row['state'] == 'ready'
            assert (row['guild_id'], row['channel_id'], row['message_id']) == (
                value['data']['guild_id'], value['data']['channel_id'], value['data']['message_id'])
            assert sub['channel_id'] == row['channel_id']
            assert self.app.store.get('deliveries', sub['id'] + ':' + row['id'])['state'] == 'attempted'
        return (200, json.dumps({'challenge': value['challenge']}).encode()) if value.get('type') == 'verification' else (200, b'{}')

    def secret(self): return 'whsec_' + base64.b64encode(b'synthetic-offline-secret-32-bytes!!').decode()

    def http_call(self, method, params=None, **claims):
        import io
        m = self.m
        scopes = 'finch.requests.read finch.replies.write finch.channel.read rook.channel.write research.channel.write'
        claim = {'iss': self.cfg.issuer, 'aud': self.cfg.audience, 'sub': self.cfg.subject,
            'iat': int(time.time()), 'exp': int(time.time()) + 600, 'scope': scopes, 'permissions': scopes.split(), **claims}
        token = jwt.encode(claim, self.key, algorithm='RS256')
        body = m.canonical(rpc(m, method, params)).encode()
        class MemorySocket:
            def __init__(_): _.input = io.BytesIO(raw); _.output = bytearray()
            def makefile(_, *a): return _.input
            def settimeout(_, seconds): pass
            def sendall(_, data): _.output.extend(data)
        raw = (f'POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nAuthorization: Bearer {token}\r\n'
            f'MCP-Protocol-Version: {m.VERSION}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n').encode() + body
        sock = MemorySocket(); m.make_handler(self.app, {'127.0.0.1:8765'})(sock, ('127.0.0.1', 1), object())
        head, data = bytes(sock.output).split(b'\r\n\r\n', 1)
        return int(head.split()[1]), json.loads(data)

    def call(self, name, arguments, **claims): return self.http_call('tools/call', {'name': name, 'arguments': arguments}, **claims)

    def subscribe(self, channel, url='https://callback.example/events'):
        status, body = self.http_call('events/subscribe', {'name': 'discord.finch.request.created',
            'arguments': {'guild_id': self.m.GUILD, 'channel_id': channel},
            'delivery': {'mode': 'webhook', 'url': url, 'secret': self.secret()}})
        assert status == 200 and 'error' not in body, body
        return body['result']['id']

    def event(self, identity='301', channel='901', author=None, content='<@903> bounded assignment', **extra):
        return {'t': 'MESSAGE_CREATE', 'd': {'id': identity, 'guild_id': self.m.GUILD, 'channel_id': channel,
            'type': 0, 'author': {'id': author or self.m.OPERATOR}, 'content': content, **extra}}

    def args(self, identity='301', file=True):
        return {'request_id': 'discord-' + identity, 'text': 'answer', **({'file': {'filename': self.filename,
            'content_type': self.content_type, 'content_base64': base64.b64encode(self.raw).decode()}} if file else {})}

    def ready(self, identity='301', channel='901'):
        self.subscribe(channel)
        assert self.app.ingest(self.event(identity, channel), self.cursors) == 1

    def reopen(self):
        self.app.close()
        self.app = self.m.Application(self.cfg, self.root / 'finch-state', auth=self.m.Authenticator(self.cfg, self.keys), callback=self.callback)

    @pytest.mark.parametrize('branch_index,limit', [(0, 4194304), (1, 1398104)])
    def test_finch_file_union_branches_are_self_contained(self, branch_index, limit):
        # Inspect actual discovery output, not a hand-written replacement schema.
        status, result = self.http_call('tools/list')
        assert status == 200 and 'error' not in result, result
        tool = next(t for t in result['result']['tools'] if t['name'] == 'reply_to_finch_request')
        schema = tool['inputSchema']['properties']['file']
        branch = schema['oneOf'][branch_index]
        # A consumer projecting a union branch must not lose inherited filename,
        # required fields or closedness. This is not a connector implementation.
        assert set(branch['properties']) == set(schema['properties']) == {
            'filename', 'content_type', 'content_base64'}
        assert branch['type'] == schema['type'] == 'object'
        assert set(branch['required']) == set(schema['required']) == {
            'filename', 'content_type', 'content_base64'}
        assert branch['additionalProperties'] is schema['additionalProperties'] is False
        assert branch['properties']['filename'] == schema['properties']['filename']
        assert branch['properties']['content_base64'] == {'type': 'string', 'maxLength': limit}
        mime = branch['properties']['content_type']
        assert mime['type'] == 'string'
        if branch_index == 0:
            assert mime['const'] == 'image/png'
        else:
            assert set(mime['enum']) == set(self.m.MIME_EXTENSIONS) - {'image/png'}
        assert tool['inputSchema']['required'] == ['request_id', 'text']
        assert not self.posts

    @pytest.mark.parametrize('channel', ['901', '902'])
    def test_max_png_actual_http_multipart_cdn_custody_and_replay(self, channel):
        import random
        m = self.m
        pixels = b''.join(b'\x00' + random.Random(n).randbytes(3072) for n in range(1000))
        base = png_fixture(1024, 1000, raw=pixels)
        padding = b'Note\x00' + b'x' * (m.FINCH_PNG_LIMIT - len(base) - 17)
        self.raw = self.cdn = png_fixture(1024, 1000, raw=pixels, extra=[(b'tEXt', padding)])
        assert len(self.raw) == m.FINCH_PNG_LIMIT
        self.filename, self.content_type = 'art.png', 'image/png'
        self.ready(channel=channel)
        args = self.args()
        assert len(args['file']['content_base64']) == m.FINCH_PNG_ENCODED_LIMIT
        assert len(m.canonical(rpc(m, 'tools/call', {'name': 'reply_to_finch_request', 'arguments': args}))) > m.FILE_HTTP_LIMIT
        status, result = self.call('reply_to_finch_request', args)
        assert status == 200 and 'error' not in result, result
        assert result['result']['isError'] is False, result
        row = self.app.store.get('requests', 'discord-301')
        assert row['state'] == 'confirmed' and row['attachment']['byte_length'] == m.FINCH_PNG_LIMIT
        assert row['attachment']['sha256'] == hashlib.sha256(self.raw).hexdigest()
        assert self.posts[0][0] == channel and self.posts[0][2] == self.raw
        before = len(self.http)
        self.reopen()
        assert self.call('reply_to_finch_request', args)[1] == result
        assert len(self.http) == before
        assert args['file']['content_base64'] not in m.canonical(row)
        assert 'cdn.discordapp.com' not in m.canonical(row)
        schema = self.app.tools[0]['inputSchema']['properties']['file']
        assert schema['properties']['content_base64']['maxLength'] == 4194304
        assert schema['oneOf'][1]['properties']['content_base64']['maxLength'] == 1398104
        assert m.COMMUNICATION_TOOLS[1]['inputSchema']['properties']['file']['properties']['content_base64']['maxLength'] == 1398104

    @pytest.mark.parametrize('name,mime', [('reply_to_finch_request', 'text/plain'), ('send_rook_message', 'image/png'),
                                        ('read_finch_messages', 'image/png'), ('discord_api', 'image/png')])
    def test_large_http_exception_only_finch_png(self, name, mime):
        args = {'request_id': 'discord-301', 'text': 'answer', 'file': {'filename': 'art.png',
                'content_type': mime, 'content_base64': 'A' * 2000000}}
        status, result = self.call(name, args)
        assert status == 400 and result['error'] == 'bounded_json_body_required'
        assert not self.posts

    def test_invalid_png_refused_before_reservation_or_send(self):
        self.ready()
        args = self.args(); args['file'] = {'filename': 'art.png', 'content_type': 'image/png',
                                           'content_base64': base64.b64encode(b'\x89PNG\r\n\x1a\n').decode()}
        assert 'error' in self.call('reply_to_finch_request', args)[1]
        assert self.app.store.get('requests', 'discord-301')['state'] == 'ready'
        assert not self.posts

    def test_exact_two_identity_bindings_and_cli_inputs(self):
        from dataclasses import replace
        import argparse
        for delta in ({'private_channel': '01'}, {'shared_channel': '901'}, {'shared_channel': self.m.CHANNEL},
            {'bot': self.m.ROOK_BOT}, {'token_path': 'relative'}, {'allowed_senders': (self.m.OPERATOR,)},
            {'allowed_senders': (*self.dot.allowed_senders, '999')}, {'guild': '999'}):
            with pytest.raises(ValueError): replace(self.dot, **delta)
        parser = argparse.ArgumentParser(); self.m.add_research_arguments(parser)
        args = ['--profile', 'finch', '--finch-channel', '901', '--dots-shared-channel', '902', '--finch-bot', '903',
            '--finch-token-path', str(self.dot.token_path)]
        for sender in self.dot.allowed_senders: args.extend(['--allowed-request-sender', sender])
        assert self.m.finch_from_args(parser.parse_args(args)) == self.dot
        assert self.m.research_from_args(parser.parse_args(args)) is None
        for invalid in (['--profile', 'finch'], args[2:], args + ['--research-channel', '904']):
            with pytest.raises(ValueError): self.m.finch_from_args(parser.parse_args(invalid))
        with pytest.raises(ValueError): replace(self.cfg, research=self.m.ResearchIdentity('904', '905', self.root / 'other.token', self.dot.allowed_senders))

    def test_identity_bound_state_and_legacy_policy_hashes(self):
        from dataclasses import replace
        self.app.close()
        for cfg in (config(self.m), replace(self.cfg, finch=replace(self.dot, shared_channel='904')),
            replace(self.cfg, finch=replace(self.dot, private_channel='905')), replace(self.cfg, finch=replace(self.dot, bot='906')),
            replace(self.cfg, finch=replace(self.dot, token_path=self.root / 'other.token')),
            replace(self.cfg, issuer='https://other.example/'), replace(self.cfg, audience='https://other.example/mcp'),
            replace(self.cfg, subject='auth0|other')):
            with pytest.raises(ValueError, match='state identity'): self.m.Application(cfg, self.root / 'finch-state', auth=object())
        self.app = self.m.Application(self.cfg, self.root / 'finch-state', auth=self.m.Authenticator(self.cfg, self.keys), callback=self.callback)
        legacy = config(self.m)
        assert legacy.policy_fields() == {'issuer': legacy.issuer, 'audience': legacy.audience, 'subject': legacy.subject}
        research = self.m.ResearchIdentity('904', '905', self.root / 'research.token', self.dot.allowed_senders)
        assert replace(legacy, research=research).policy_fields() == {**legacy.policy_fields(), 'identity': research.policy_fields()}

    def test_discovery_only_two_tools_and_direct_forbidden_dispatch(self):
        _, body = self.http_call('tools/list')
        tools = body['result']['tools']
        assert [t['name'] for t in tools] == ['reply_to_finch_request', 'read_finch_messages']
        assert 'file' in tools[0]['inputSchema']['properties'] and 'channel_id' not in tools[0]['inputSchema']['properties']
        assert tools[1]['inputSchema']['required'] == ['channel_id']
        for name in ('discord_api', 'github_pr_comment', 'send_rook_message', 'read_rook_file', 'create_rook_thread',
            'set_rook_reaction', 'reply_to_research_request', 'send_finch_message', 'read_finch_file'):
            status, result = self.call(name, {})
            assert status == 200 and result['error']['message'] == 'ToolNotAllowed'
        assert not self.http and not self.posts
        _, body = self.http_call('events/list')
        assert body['result']['events'][0]['inputSchema']['properties']['channel_id']['enum'] == ['901', '902']

    @pytest.mark.parametrize('channel', ['901', '902'])
    def test_actual_mentions_survive_finch_reads_and_signed_events(self, channel):
        event = self.event(channel=channel, mentions=[
            {'id': self.dot.bot, 'username': 'not-projected', 'member': {'roles': ['777']}},
            {'id': self.m.ROOK_BOT}])
        self.messages[(channel, '301')] = deepcopy(event['d'])
        for arguments in ({'channel_id': channel, 'message_id': '301'}, {'channel_id': channel, 'limit': 1}):
            status, body = self.call('read_finch_messages', arguments)
            assert status == 200 and not body['result']['isError']
            value = body['result']['structuredContent']['messages'][0]
            assert value['mentions'] == [{'id': self.dot.bot}, {'id': self.m.ROOK_BOT}]
            assert value['mentions_truncated'] is False
        assert not self.app.store.all('requests')  # Reads never replay live requests.
        self.subscribe(channel)
        assert self.app.ingest(event, self.cursors) == 1
        payload = self.callbacks[-1][1]['data']
        assert payload['mentions'] == [{'id': self.dot.bot}, {'id': self.m.ROOK_BOT}]
        assert payload['mentions_truncated'] is False
        assert payload['text'] == 'bounded assignment'
        schema = self.app.event_definition['payloadSchema']
        assert schema['properties']['mentions']['items']['properties']['id'] == self.m.SNOWFLAKE
        assert set(payload) <= set(schema['properties'])
        assert not self.posts

    @pytest.mark.parametrize('metadata,expected,admitted', [
        ({}, None, True), ({'mentions': None}, None, False), ({'mentions': []}, [], False),
        ({'mentions': '<@903>'}, None, False), ({'mentions': {'id': '903'}}, None, False),
        ({'mentions': [None]}, None, False), ({'mentions': [{'id': 903}]}, None, False),
        ({'mentions': [{'id': '0903'}]}, None, False), ({'mentions': [{'id': '９０３'}]}, None, False),
        ({'mentions': [{'id': '9' * 21}]}, None, False),
        ({'mentions': [{'id': '903'}, {'id': 'bad'}]}, None, True),
        ({'mentions': [{'id': '904'}]}, [{'id': '904'}], False),
    ])
    def test_mention_metadata_unknown_empty_and_literal_are_not_fabricated(self, metadata, expected, admitted):
        event = self.event(**metadata)  # Literal <@903> is present in every body.
        self.messages[('901', '301')] = deepcopy(event['d'])
        _, body = self.call('read_finch_messages', {'channel_id': '901', 'message_id': '301'})
        value = body['result']['structuredContent']['messages'][0]
        assert value['mentions'] == expected
        assert value['mentions_truncated'] is False
        assert not self.app.store.all('requests')
        self.subscribe('901')
        assert self.app.ingest(event, self.cursors) == int(admitted)
        events = [v['data'] for _, v in self.callbacks if v.get('name') == self.app.event]
        if admitted:
            assert events[0]['mentions'] == expected
            assert events[0]['mentions_truncated'] is False
        else:
            assert not events and not self.app.store.all('requests')
        # Shared Rook/Research projection and original event schema remain unchanged.
        assert 'mentions' not in self.m.message_projection(event['d'], '901')
        assert 'mentions' not in self.m.EVENT_DEFINITION['payloadSchema']['properties']
        assert not self.posts

    @pytest.mark.parametrize('channel', ['901', '902'])
    def test_mentions_bounded_prefix_marks_read_and_event_truncation(self, channel):
        prefix = [{'id': str(1000 + i)} for i in range(100)]
        event = self.event(channel=channel, mentions=prefix + [{'id': self.dot.bot}])
        self.messages[(channel, '301')] = deepcopy(event['d'])
        _, body = self.call('read_finch_messages', {'channel_id': channel, 'message_id': '301'})
        result = body['result']['structuredContent']; value = result['messages'][0]
        assert value['mentions'] == prefix and value['mentions_truncated'] is True
        assert value['truncated'] is True and result['truncated'] is True
        self.subscribe(channel)
        assert self.app.ingest(event, self.cursors) == 1  # Existing ingress still sees actual bot metadata.
        payload = self.callbacks[-1][1]['data']
        assert payload['mentions'] == prefix and payload['mentions_truncated'] is True
        assert {'id': self.dot.bot} not in payload['mentions']  # No invented completeness.
        assert not self.posts

    def test_auth_http_subject_resource_scope_and_rbac_fail_closed(self):
        for delta in ({'aud': 'https://other.example/mcp'}, {'iss': 'https://other.example/'},
            {'sub': 'auth0|other'}, {'exp': 1}):
            assert self.call('read_finch_messages', {'channel_id': '901'}, **delta)[0] == 401
        for delta in ({'scope': 'rook.channel.read', 'permissions': ['rook.channel.read']},
            {'permissions': []}, {'scope': 'finch.requests.read', 'permissions': ['finch.channel.read']}):
            assert self.call('read_finch_messages', {'channel_id': '901'}, **delta)[0] == 403
        assert not self.http
        metadata = self.app.handle('GET', '/.well-known/oauth-protected-resource/mcp', {}, None)[2]
        assert set(metadata['scopes_supported']) == {'finch.requests.read', 'finch.replies.write', 'finch.channel.read', 'offline_access'}

    def test_subscriptions_are_per_channel_no_private_shared_forwarding(self):
        private = self.subscribe('901'); shared = self.subscribe('902')
        assert private != shared and len(self.app.store.all('subscriptions')) == 2
        for channel, sub in (('901', private), ('902', shared)):
            assert self.app.store.get('subscriptions', sub)['channel_id'] == channel
            assert self.app.ingest(self.event('301' if channel == '901' else '302', channel), self.cursors) == 1
        events = [v for _, v in self.callbacks if v.get('type') != 'verification']
        assert [(v['subscriptionId'], v['data']['channel_id']) for v in events] == [(private, '901'), (shared, '902')]
        for args in ({'guild_id': '999', 'channel_id': '901'}, {'guild_id': self.m.GUILD, 'channel_id': '904'}):
            _, bad = self.http_call('events/subscribe', {'name': self.app.event, 'arguments': args,
                'delivery': {'mode': 'webhook', 'url': 'https://callback.example/events', 'secret': self.secret()}})
            assert 'error' in bad
        self.http_call('events/unsubscribe', {'name': self.app.event, 'arguments': {'guild_id': self.m.GUILD, 'channel_id': '901'},
            'delivery': {'mode': 'webhook', 'url': 'https://callback.example/events'}})
        assert self.app.ingest(self.event('303'), self.cursors) == 0
        assert self.app.store.get('subscriptions', shared)

    def test_per_channel_fresh_cursors_durable_dedup_and_origin_reservation(self):
        self.subscribe('901'); self.subscribe('902')
        assert self.app.ingest(self.event('150', '901'), self.cursors) == 1
        assert self.app.ingest(self.event('150', '902'), self.cursors) == 0
        row = self.app.store.get('requests', 'discord-150')
        assert (row['guild_id'], row['channel_id'], row['message_id']) == (self.m.GUILD, '901', '150')
        self.reopen()
        assert self.app.ingest(self.event('150', '901'), self.cursors) == 0
        assert self.app.ingest(self.event('149', '901'), self.cursors) == 0
        assert self.app.ingest(self.event('201', '902'), self.cursors) == 1
        for cursors in ('100', {'901': '100'}, {'901': '100', '902': '200', '904': '1'}, {'901': '01', '902': '200'}):
            with pytest.raises(ValueError): self.app.ingest(self.event('399'), cursors)

    def test_literal_mentions_allowlist_self_webhook_ack_and_metadata_loops(self):
        self.subscribe('901')
        for i, author in enumerate(self.dot.allowed_senders):
            assert self.app.ingest(self.event(str(301 + i), author=author, content='<@!903> assignment'), self.cursors) == 1
        bad = [{'author': {'id': self.dot.bot}}, {'author': {'id': '999'}}, {'webhook_id': '777'},
            {'content': 'Finch: assignment'}, {'content': 'ack', 'mentions': [{'id': '903'}]},
            {'content': '<@903>'}, {'content': '<@903> assignment', 'mentions': []}, {'guild_id': '999'},
            *[{'content': '<@903> ' + t} for t in ('ack', 'acknowledged', 'received', 'reply', 'handoff', 'Finch reply', 'Finch handoff',
                                               'Research reply', 'Research handoff', 'Rook reply', 'Rook handoff')],
            {'author': {'id': self.m.ROOK_BOT}, 'content': '<@903> assignment\n-# rook.reply · discord-299'}]
        for i, delta in enumerate(bad):
            event = self.event(str(310 + i)); event['d'].update(delta)
            assert self.app.ingest(event, self.cursors) == 0
        assert len(self.app.store.all('requests')) == 3

    @pytest.mark.parametrize('channel', ['901', '902'])
    @pytest.mark.parametrize('mentions, admitted', [
        (None, True), ([{'id': '903'}], True), ([], False),
        ([{'id': '999'}], False), ('malformed', False),
    ])
    def test_suppressed_rook_tag_exception_does_not_expand_finch(self, channel, mentions, admitted):
        self.subscribe(channel)
        extra = {} if mentions is None else {'mentions': mentions}
        event = self.event(channel=channel, content='<@903> assignment', **extra)
        assert self.app.ingest(event, self.cursors) == int(admitted)
        assert len(self.app.store.all('requests')) == int(admitted)

    @pytest.mark.parametrize('channel', ['901', '902'])
    def test_bounded_history_real_rest_own_token_exact_selected_channel(self, channel):
        status, result = self.call('read_finch_messages', {'channel_id': channel, 'message_id': '701'})
        assert status == 200 and result['result']['structuredContent']['channel_id'] == channel
        assert result['result']['structuredContent']['messages'][0]['url'].endswith('/' + channel + '/701')
        self.call('read_finch_messages', {'channel_id': channel, 'before_message_id': '800', 'limit': 2})
        assert self.http[-1][2].endswith('/' + channel + '/messages?limit=2&before=800')
        before = len(self.http)
        for bad in ({}, {'channel_id': '904'}, {'channel_id': channel, 'thread_id': '999'},
            {'channel_id': channel, 'message_id': '701', 'limit': 2}, {'channel_id': channel, 'limit': True}):
            assert 'error' in self.call('read_finch_messages', bad)[1]
        assert len(self.http) == before and not self.app.store.all('requests')
        self.messages[(channel, '701')] = {'id': '701', 'channel_id': '904', 'content': 'foreign'}
        assert 'error' in self.call('read_finch_messages', {'channel_id': channel, 'message_id': '701'})[1]

    def test_transport_exact_routes_not_arbitrary_send_or_research_files(self):
        m = self.m; before = len(self.http)
        for method, path in [('GET', '/channels/904/messages'), ('GET', '/channels/901/messages?limit=1&limit=2'),
            ('POST', '/channels/901/messages/301/threads'), ('GET', '/guilds/' + m.GUILD),
            ('PATCH', '/users/@me'), ('DELETE', '/channels/901/messages/301')]:
            with pytest.raises(m.RpcError): m.discord_request(method, path, finch=self.dot)
        assert len(self.http) == before
        research = m.ResearchIdentity('904', '905', self.root / 'research.token', self.dot.allowed_senders)
        with pytest.raises(m.RpcError): m.discord_request('POST', '/channels/904/messages', {},
            {'filename': 'notes.txt', 'content_type': 'text/plain', 'data': self.raw}, research=research)

    @pytest.mark.parametrize('channel', ['901', '902'])
    def test_full_signed_assignment_http_artifact_multipart_cdn_receiver_and_replay(self, channel):
        import receive_one_discord_message as receiver
        self.ready(channel=channel)
        status, result = self.call('reply_to_finch_request', self.args())
        assert status == 200 and not result['result']['isError'], result
        assert len(self.posts) == 1 and self.posts[0][0] == channel
        row = self.app.store.get('requests', 'discord-301')
        assert row['state'] == 'confirmed' and row['attachment']['sha256'] == hashlib.sha256(self.raw).hexdigest()
        event = self.event(row['reply_id'], channel, self.dot.bot, self.posts[0][1]['content'])
        event['d']['attachments'] = deepcopy(self.messages[(channel, row['reply_id'])]['attachments'])
        scope = receiver.Scope('discord-301', 'finch.reply', '100', 'author', self.dot.bot, 2,
            str(self.root / 'finch-state'), self.cfg.issuer, self.cfg.audience, self.cfg.subject, finch=self.dot, finch_channel=channel)
        received = receiver.match_event(event, scope, self.dot.bot)
        assert received['authenticated_reply_binding'] and received['attachment']['sha256'] == row['attachment']['sha256']
        assert received['channel_id'] == channel
        self.reopen()
        assert self.call('reply_to_finch_request', self.args())[1] == result and len(self.posts) == 1
        for delta in ({'text': 'changed'}, {'file': {**self.args()['file'], 'content_base64': base64.b64encode(b'changed').decode()}},
            {'file': {**self.args()['file'], 'filename': 'other.txt'}}, {'channel_id': '902'}):
            assert 'error' in self.call('reply_to_finch_request', {**self.args(), **delta})[1]
        assert len(self.posts) == 1 and self.raw.decode() not in self.m.canonical(self.app.store.all('requests'))

    def test_receiver_rejects_wrong_origin_identity_and_file_custody(self):
        from dataclasses import replace
        import receive_one_discord_message as r
        self.ready(); self.call('reply_to_finch_request', self.args())
        row = self.app.store.get('requests', 'discord-301')
        event = self.event(row['reply_id'], author=self.dot.bot, content=self.posts[0][1]['content'])
        event['d']['attachments'] = deepcopy(self.messages[('901', row['reply_id'])]['attachments'])
        scope = r.Scope('discord-301', 'finch.reply', '100', 'author', self.dot.bot, 2, str(self.root / 'finch-state'),
            self.cfg.issuer, self.cfg.audience, self.cfg.subject, finch=self.dot, finch_channel='901')
        assert r.match_event(event, scope, self.dot.bot)
        for delta in ({'channel_id': '902'}, {'guild_id': '999'}, {'webhook_id': '777'}, {'attachments': []},
            {'attachments': event['d']['attachments'] * 2}, {'content': 'forged'}, {'author': {'id': self.m.ROOK_BOT}}):
            assert r.match_event({**event, 'd': {**event['d'], **delta}}, scope, self.dot.bot) is None
        for field, value in (('id', '602'), ('filename', 'other.txt'), ('content_type', 'text/html'), ('size', 999), ('url', 'https://evil.example/x')):
            forged = deepcopy(event); forged['d']['attachments'][0][field] = value
            assert r.match_event(forged, scope, self.dot.bot) is None
        for delta in ({'finch_channel': '902'}, {'mcp_subject': 'auth0|other'}, {'mcp_audience': 'https://other.example/mcp'},
            {'finch': replace(self.dot, token_path=self.root / 'other.token')}):
            assert r.match_event(event, replace(scope, **delta), self.dot.bot) is None
        assert r.match_event(event, scope, self.m.BOT) is None
        for delta in ({'channel_id': '902'}, {'guild_id': '999'}, {'message_id': '399'}, {'state': 'uncertain'}):
            self.app.store.put('requests', 'discord-301', {**row, **delta})
            assert r.match_event(event, scope, self.dot.bot) is None
        self.app.store.put('requests', 'discord-301', row)

    @pytest.mark.parametrize('fault', ['timeout', 'author', 'channel', 'guild', 'attachment', 'cdn'])
    def test_artifact_uncertainty_pre_effect_restart_no_resend(self, fault):
        self.ready()
        if fault == 'cdn': self.cdn = b'x' * len(self.raw)
        else: self.fault = fault
        _, result = self.call('reply_to_finch_request', self.args())
        assert result['result']['isError'] and len(self.posts) == 1
        assert self.app.store.get('requests', 'discord-301')['state'] == 'uncertain'
        self.reopen()
        assert self.call('reply_to_finch_request', self.args())[1] == result and len(self.posts) == 1
        assert 'error' in self.call('reply_to_finch_request', {**self.args(), 'text': 'changed'})[1]

    @pytest.mark.parametrize('file', [False, True], ids=['text', 'multipart'])
    @pytest.mark.parametrize('text,users', [
        ('<@1554962007059472445> answer', ['1554962007059472445']),
        ('<@1486880423568211968> answer', ['1486880423568211968']),
        ('<@!1554962007059472445> <@!1486880423568211968> answer',
         ['1486880423568211968', '1554962007059472445']),
        ('Kael 1486880423568211968 and Rook 1554962007059472445 answer', []),
        ('<@999> <@!998> <@392067741500309517> <@&1486880423568211968> '
         '<@&1554962007059472445> @everyone @here answer', []),
    ], ids=['rook-only', 'kael-only', 'both', 'neither', 'unrelated'])
    def test_reply_only_allows_literal_fixed_recipient_mentions(self, file, text, users):
        self.ready()
        arguments = {**self.args(file=file), 'text': text}
        status, result = self.call('reply_to_finch_request', arguments)
        assert status == 200 and not result['result']['isError'], result
        channel, payload, uploaded = self.posts[-1]
        assert channel == '901'
        assert payload['content'] == text + '\n-# finch.reply · discord-301'
        assert payload['allowed_mentions'] == {'parse': [], 'replied_user': False,
                                               **({'users': users} if users else {})}
        assert payload['flags'] == 4  # SUPPRESS_EMBEDS, not SUPPRESS_NOTIFICATIONS.
        assert uploaded == (self.raw if file else None)
        assert self.call('reply_to_finch_request', arguments)[1] == result
        assert len(self.posts) == 1

    def test_text_only_exact_origin_and_attachment_absence(self):
        self.ready(channel='902')
        _, result = self.call('reply_to_finch_request', self.args(file=False))
        assert not result['result']['isError'] and self.posts[0][0] == '902' and self.posts[0][2] is None
        assert self.app.store.get('requests', 'discord-301')['attachment'] is None
        assert 'error' in self.call('reply_to_finch_request', self.args())[1]
        assert len(self.posts) == 1

    def test_interrupted_sending_receipt_restarts_uncertain_without_post(self, monkeypatch):
        self.ready(); sender = self.app.sender
        def interrupted(channel, wire, file):
            sender(channel, wire, file)
            raise KeyboardInterrupt()
        monkeypatch.setattr(self.app, 'sender', interrupted)
        with pytest.raises(KeyboardInterrupt): self.call('reply_to_finch_request', self.args())
        assert self.app.store.get('requests', 'discord-301')['state'] == 'sending' and len(self.posts) == 1
        self.reopen()
        assert self.app.store.get('requests', 'discord-301')['state'] == 'uncertain'
        assert self.call('reply_to_finch_request', self.args())[1]['result']['isError']
        assert len(self.posts) == 1

    def test_undelivered_expired_wrong_origin_requests_cannot_send(self):
        self.ready(); row = self.app.store.get('requests', 'discord-301')
        delivery = self.app.store.all('deliveries')[0]
        self.app.store.put('deliveries', delivery['id'], {**delivery, 'state': 'attempted'})
        assert 'error' in self.call('reply_to_finch_request', self.args())[1]
        self.app.store.put('deliveries', delivery['id'], delivery)
        for delta in ({'expires': 1}, {'channel_id': '904'}, {'guild_id': '999'}, {'message_id': '399'}):
            self.app.store.put('requests', 'discord-301', {**row, **delta})
            assert 'error' in self.call('reply_to_finch_request', self.args())[1]
        assert not self.posts
        self.app.store.put('requests', 'discord-301', row)
        def ambiguous(*args): raise TimeoutError('callback uncertainty')
        self.app.callback = ambiguous
        assert self.app.ingest(self.event('302'), self.cursors) == 0
        assert all(d['state'] == 'attempted' for d in self.app.store.all('deliveries') if d['request_id'] == 'discord-302')
        assert 'error' in self.call('reply_to_finch_request', self.args('302'))[1]
        self.reopen()
        assert self.app.ingest(self.event('302'), self.cursors) == 0 and not self.posts

    def test_adapter_and_receiver_cli_exact_channel_inputs_offline(self, monkeypatch):
        import receive_one_discord_message as r
        args = ['--profile', 'finch', '--finch-channel', '901', '--dots-shared-channel', '902', '--finch-bot', '903',
                '--finch-token-path', str(self.dot.token_path)]
        for sender in self.dot.allowed_senders: args.extend(['--allowed-request-sender', sender])
        base = args + ['--issuer', self.cfg.issuer, '--audience', self.cfg.audience, '--operator-subject', self.cfg.subject,
            '--state-dir', str(self.root / 'new-state'), '--port', '9876', '--max-run-seconds', '1',
            '--protected-resource-metadata-url', self.cfg.protected_metadata_url]
        captured = []
        class App:
            def __init__(_, cfg, state): assert cfg.finch == self.dot; captured.append(cfg)
            def close(_): pass
        async def run(app, server, cursors, deadline):
            assert cursors == self.cursors and deadline == 1; return 0
        monkeypatch.setattr(self.m, 'Application', App)
        monkeypatch.setattr(self.m, 'HTTPServer', lambda *args: object())
        monkeypatch.setattr(self.m, 'run_adapter', run)
        monkeypatch.setattr(self.m, 'make_handler', lambda *args: object())
        for tail in ([], ['--finch-after-message', '100'], ['--finch-after-message', '100', '--dots-shared-after-message', '01'],
            ['--finch-after-message', '100', '--dots-shared-after-message', '200', '--after-message', '100']):
            with pytest.raises(SystemExit): self.m.main(base + tail)
        assert not captured
        assert self.m.main(base + ['--finch-after-message', '100', '--dots-shared-after-message', '200']) == 0
        async def receive(scope):
            assert scope.finch == self.dot and scope.channel == '902'; return {'status': 'synthetic'}, 0
        monkeypatch.setattr(r, 'receive_one', receive)
        receiver_args = args + ['--guild', self.m.GUILD, '--channel', '902', '--correlation', 'discord-301',
            '--reply-kind', 'finch.reply', '--after-message', '200', '--allowed-author', '903', '--max-wait-seconds', '1',
            '--mcp-state-dir', str(self.root / 'finch-state'), '--mcp-issuer', self.cfg.issuer,
            '--mcp-audience', self.cfg.audience, '--mcp-subject', self.cfg.subject]
        assert r.main(receiver_args) == 0
        with pytest.raises(SystemExit): r.main(receiver_args + ['--channel', '904'])

    def test_file_http_exception_and_unsafe_or_oversized_uploads(self):
        self.ready()
        args = self.args()
        for value in ({'path': '/etc/passwd'}, {'url': 'https://example.com/file'},
            {**args['file'], 'filename': 'notes.md'}, {**args['file'], 'content_type': 'text/html'},
            {**args['file'], 'filename': '../notes.txt'}, {**args['file'], 'content_base64': '%%%'}):
            assert 'error' in self.call('reply_to_finch_request', {**args, 'file': value})[1]
        for raw in (b'PRIVATE KEY', b'patient DOB: 1900-01-01', b'<script>bad</script>', b'x' * (self.m.FILE_LIMIT + 1)):
            assert 'error' in self.call('reply_to_finch_request', {**args, 'file': {**args['file'],
                'content_base64': base64.b64encode(raw).decode()}})[1]
        assert not self.posts
        assert self.call('reply_to_finch_request', {'request_id': 'discord-301', 'text': 'x' * 33000})[0] == 400
        assert self.call('reply_to_research_request', {**args, 'text': 'x' * 33000})[0] == 400
        self.raw = self.cdn = b'x' * self.m.FILE_LIMIT
        status, result = self.call('reply_to_finch_request', self.args())
        assert status == 200 and not result['result']['isError'] and len(self.posts) == 1
        assert result['result']['structuredContent']['attachment']['byte_length'] == self.m.FILE_LIMIT

    def test_gateway_and_receiver_use_finch_own_token_and_both_channel_cursors(self, monkeypatch):
        import asyncio
        import receive_one_discord_message as r
        self.subscribe('901'); self.subscribe('902'); tokens = []
        class Server:
            def serve_forever(_, **kw): pass
            def shutdown(_): pass
            def server_close(_): pass
        def factory(observer):
            def make(on_event, on_error):
                class Client:
                    async def start(_, token, reconnect):
                        tokens.append(token); assert reconnect is False
                        on_event(self.event('150', '901'), observer); on_event(self.event('201', '902'), observer)
                        await asyncio.Future()
                    async def close(_): pass
                return Client()
            return make
        assert asyncio.run(self.m.run_adapter(self.app, Server(), self.cursors, .05, client_factory=factory(self.dot.bot))) == 0
        assert tokens == ['synthetic-finch-token'] and len(self.app.store.all('requests')) == 2
        assert asyncio.run(self.m.run_adapter(self.app, Server(), self.cursors, .05, client_factory=factory(self.m.BOT))) == 2
        self.call('reply_to_finch_request', self.args('150', False))
        row = self.app.store.get('requests', 'discord-150')
        event = self.event(row['reply_id'], author=self.dot.bot, content=self.posts[0][1]['content'])
        event['d']['attachments'] = []
        scope = r.Scope('discord-150', 'finch.reply', '100', 'author', self.dot.bot, 1, str(self.root / 'finch-state'),
            self.cfg.issuer, self.cfg.audience, self.cfg.subject, finch=self.dot, finch_channel='901')
        def receiver_factory(on_event, on_error):
            class Client:
                async def start(_, token, reconnect):
                    tokens.append(token); on_event(event, self.dot.bot); await asyncio.Future()
                async def close(_): pass
            return Client()
        result, code = asyncio.run(r.receive_one(scope, client_factory=receiver_factory))
        assert code == 0 and result['authenticated_reply_binding'] and tokens[-1] == 'synthetic-finch-token'
