"""Four scoped offline groups; synthetic keys only, all external I/O denied."""
import base64
import asyncio
from copy import deepcopy
import importlib
import io
import json
from pathlib import Path
import socket
import sys
import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network denied'))
    monkeypatch.setattr(socket.socket, 'bind', lambda *a, **k: pytest.fail('listener bind denied'))
    import post_discord_message
    monkeypatch.setattr(post_discord_message, 'load_token', lambda: pytest.fail('credential read denied'))


def module():
    assert importlib.util.find_spec('rook_mcp_events') is not None, 'MCP server source missing'
    return importlib.import_module('rook_mcp_events')


def config(m):
    return m.Config('https://tenant.example/', 'https://rook.example/mcp', 'auth0|operator',
                    'https://rook.example/.well-known/oauth-protected-resource/mcp')


def rpc(m, method, params=None):
    return {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': {
        **(params or {}), '_meta': {'io.modelcontextprotocol/protocolVersion': m.VERSION,
                                  'io.modelcontextprotocol/clientCapabilities': {}}}}


class TestAuthDiscovery:
    def test_real_jwt_verification_and_discovery(self, tmp_path):
        m = module()
        cfg = config(m)
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = jwt.PyJWK.from_json(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()), algorithm='RS256')
        class Keys:
            def get_signing_key_from_jwt(self, token):
                return jwk
        auth = m.Authenticator(cfg, Keys())
        claims = {'iss': cfg.issuer, 'aud': cfg.audience, 'sub': cfg.subject, 'exp': int(time.time()) + 600,
                  'iat': int(time.time()), 'scope': 'rook.requests.read rook.replies.write',
                  'permissions': ['rook.requests.read', 'rook.replies.write']}
        token = jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'fixture'})
        principal = auth.authenticate('Bearer ' + token, {'rook.requests.read'})
        assert principal.subject == cfg.subject
        for delta in ({'iss': 'https://evil.example/'}, {'aud': 'other'}, {'sub': 'auth0|other'},
                      {'exp': 1}, {'scope': ''}, {'iat': int(time.time()) + 600},
                      {'permissions': None}, {'permissions': []}, {'permissions': 'rook.requests.read'}):
            with pytest.raises(m.AuthDenied):
                auth.authenticate('Bearer ' + jwt.encode({**claims, **delta}, key, algorithm='RS256'), {'rook.requests.read'})
        for bad in ('', 'Basic x', 'Bearer invalid', 'Bearer ' + token[:-5] + 'xxxxx',
                    'Bearer ' + jwt.encode(claims, 'synthetic-not-a-real-secret-' * 3, algorithm='HS256')):
            with pytest.raises(m.AuthDenied):
                auth.authenticate(bad, {'rook.requests.read'})
        app = m.Application(cfg, tmp_path / 'private', auth=auth)
        status, headers, body = app.handle('POST', '/mcp', {'MCP-Protocol-Version': m.VERSION}, rpc(m, 'server/discover'))
        assert status == 401 and 'resource_metadata' in headers['WWW-Authenticate']
        headers = {'Authorization': 'Bearer ' + token, 'MCP-Protocol-Version': m.VERSION}
        status, _, body = app.handle('POST', '/mcp', headers, rpc(m, 'server/discover'))
        assert status == 200
        assert body['result']['resultType'] == 'complete'
        assert body['result']['supportedVersions'] == [m.VERSION]
        assert body['result']['capabilities'] == {'tools': {}, 'events': {}}
        assert app.handle('POST', '/mcp', headers, rpc(m, 'tools/list'))[2]['result']['tools'][0]['name'] == 'reply_to_rook_request'
        assert app.handle('POST', '/mcp', headers, rpc(m, 'events/list'))[2]['result']['events'][0]['name'] == m.EVENT
        wrong = rpc(m, 'server/discover'); wrong['params']['_meta']['io.modelcontextprotocol/protocolVersion'] = '2025-03-26'
        assert app.handle('POST', '/mcp', headers, wrong)[0] == 400
        assert app.handle('POST', '/mcp', {**headers, 'MCP-Protocol-Version': '2025-03-26'}, wrong)[2]['error']['code'] == -32022
        assert app.handle('POST', '/mcp', headers, rpc(m, 'initialize'))[2]['error']['code'] == -32601
        metadata = app.handle('GET', '/.well-known/oauth-protected-resource/mcp', {}, None)[2]
        assert metadata['resource'] == cfg.audience
        assert metadata['authorization_servers'] == [cfg.issuer]
        assert app.handle('POST', '/mcp', {**headers, 'Origin': 'https://evil.example'}, rpc(m, 'server/discover'))[0] == 403
        app.close()

    def test_actual_http_parser_and_adapter_cleanup_without_socket(self, tmp_path):
        m = module(); cfg = config(m)
        app = m.Application(cfg, tmp_path / 'private')
        handler = m.make_handler(app, {'127.0.0.1:8765'})
        class MemorySocket:
            def __init__(self, request): self.input = io.BytesIO(request); self.output = bytearray()
            def makefile(self, *a): return self.input
            def settimeout(self, timeout): assert timeout == 10
            def sendall(self, data): self.output.extend(data)
        def request(raw):
            s = MemorySocket(raw)
            handler(s, ('127.0.0.1', 1), object())
            return bytes(s.output)
        response = request(b'GET /.well-known/oauth-protected-resource/mcp HTTP/1.1\r\nHost: 127.0.0.1:8765\r\n\r\n')
        assert response.startswith(b'HTTP/1.1 200') and cfg.issuer.encode() in response
        response = request(b'POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}')
        assert response.startswith(b'HTTP/1.1 401') and b'WWW-Authenticate:' in response
        for headers in (b'Host: evil.example', b'Host: 127.0.0.1:8765\r\nAuthorization: x\r\nAuthorization: y'):
            assert request(b'GET /mcp HTTP/1.1\r\n' + headers + b'\r\n\r\n').startswith(b'HTTP/1.1 403')
        assert request(b'POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nTransfer-Encoding: chunked\r\n\r\n').startswith(b'HTTP/1.1 400')
        events = []
        class FakeHTTP:
            def serve_forever(self, **kwargs): events.append('serve')
            def shutdown(self): events.append('shutdown')
            def server_close(self): events.append('server_close')
        class FakeClient:
            async def start(self, token, reconnect):
                assert token == 'synthetic' and reconnect is False
                raise RuntimeError('synthetic startup failure')
            async def close(self): events.append('client_close')
        with pytest.raises(RuntimeError):
            asyncio.run(m.run_adapter(app, FakeHTTP(), '100', 1, client_factory=lambda *a: FakeClient(), token_loader=lambda: 'synthetic'))
        assert events == ['serve', 'client_close', 'shutdown', 'server_close']
        app.close()


class TestMetadataBootstrap:
    def test_first_login_metadata_only_without_subject_or_external_prmd(self, tmp_path, monkeypatch, capsys):
        """One real CLI/HTTP/lifespan path, with sockets and effect constructors denied."""
        from dataclasses import replace
        import threading
        m = module()
        local = 'http://127.0.0.1:8765/.well-known/oauth-protected-resource/mcp'
        cfg = m.Config('https://tenant.example/', 'https://rook.example/mcp', None, local,
                       metadata_only=True, local_tunnel_metadata=True)
        assert cfg.subject is None
        # Both reproduced startup preconditions are waived only explicitly, not globally.
        with pytest.raises(ValueError):
            replace(cfg, metadata_only=False)
        with pytest.raises(ValueError):
            replace(cfg, local_tunnel_metadata=False)
        assert replace(cfg, metadata_only=False, subject='auth0|fixture').subject == 'auth0|fixture'
        for bad in ('http://localhost:8765/.well-known/oauth-protected-resource/mcp',
                    'http://127.0.0.2:8765/.well-known/oauth-protected-resource/mcp',
                    'http://127.0.0.1:80/.well-known/oauth-protected-resource/mcp',
                    local + '?x=1', local + '#fragment', local.replace('/mcp', '/other'),
                    local.replace('127.0.0.1', 'user@127.0.0.1'),
                    local.replace('127.0.0.1', '127.0.0.1.example')):
            with pytest.raises(ValueError):
                replace(cfg, protected_metadata_url=bad)
        with pytest.raises(ValueError):
            replace(cfg, issuer='http://tenant.example/')
        with pytest.raises(ValueError):
            replace(cfg, audience='http://rook.example/mcp')

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = {'iss': cfg.issuer, 'aud': cfg.audience, 'sub': 'auth0|fixture',
                  'exp': int(time.time()) + 600, 'iat': int(time.time()),
                  'scope': 'rook.requests.read rook.replies.write',
                  'permissions': ['rook.requests.read', 'rook.replies.write']}
        token = jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'fixture'})
        assert jwt.decode(token, key.public_key(), algorithms=['RS256'],
                          audience=cfg.audience, issuer=cfg.issuer) == claims
        def denied(*args, **kwargs):
            pytest.fail('metadata bootstrap attempted effect constructor/credential/key/state I/O')
        for name in ('Authenticator', 'Store', 'create_client', 'load_token',
                     'send', 'readback', 'webhook_post'):
            monkeypatch.setattr(m, name, denied)
        monkeypatch.setattr(m.jwt, 'PyJWKClient', denied)
        monkeypatch.setattr(m.sqlite3, 'connect', denied)
        monkeypatch.setattr(socket, 'getaddrinfo', denied)

        events = []
        servers = []
        class MemorySocket:
            def __init__(self, raw): self.input = io.BytesIO(raw); self.output = bytearray()
            def makefile(self, *args): return self.input
            def settimeout(self, timeout): assert timeout == 10
            def sendall(self, data): self.output.extend(data)
        class FakeHTTP:
            def __init__(self, address, handler):
                assert address == ('127.0.0.1', 8765)
                self.handler = handler
                self.stopped = threading.Event()
                self.failure = None
                servers.append(self)
            def request(self, method, path, headers=b'', body=b''):
                raw = (method.encode() + b' ' + path.encode() + b' HTTP/1.1\r\n'
                       + b'Host: 127.0.0.1:8765\r\n' + headers + b'\r\n' + body)
                s = MemorySocket(raw)
                self.handler(s, ('127.0.0.1', 1), self)
                response = bytes(s.output)
                assert b'Cache-Control: no-store' in response and b'Connection: close' in response
                return response
            def serve_forever(self, **kwargs):
                events.append('serve')
                try:
                    response = self.request('GET', '/.well-known/oauth-protected-resource/mcp')
                    assert response.startswith(b'HTTP/1.1 200')
                    assert json.loads(response.split(b'\r\n\r\n', 1)[1]) == {
                        'resource': cfg.audience, 'authorization_servers': [cfg.issuer],
                        'scopes_supported': ['rook.requests.read', 'rook.replies.write', 'rook.channel.read', 'rook.channel.write', 'offline_access'],
                        'bearer_methods_supported': ['header']}
                    for verb in ('GET', 'POST'):
                        # Actual tunnel v0.0.15 probes /mcp with an empty POST then GET.
                        response = self.request(verb, '/mcp')
                        assert response.startswith(b'HTTP/1.1 401')
                        assert ('resource_metadata="' + local + '"').encode() in response
                    for method in ('server/discover', 'tools/list', 'events/list', 'tools/call',
                                   'events/subscribe', 'events/unsubscribe', 'initialize', 'unknown'):
                        for bearer in ('', 'Bearer invalid', 'Bearer ' + token):
                            data = json.dumps(rpc(m, method)).encode()
                            headers = (f'Authorization: {bearer}\r\nMCP-Protocol-Version: {m.VERSION}\r\n'
                                       f'Content-Type: application/json\r\nContent-Length: {len(data)}\r\n').encode()
                            response = self.request('POST', '/mcp', headers, data)
                            assert response.startswith(b'HTTP/1.1 401')
                            assert ('resource_metadata="' + local + '"').encode() in response
                            assert b'"result"' not in response
                    assert self.request('GET', '/.well-known/oauth-protected-resource/mcp',
                                        b'Origin: https://evil.example\r\n').startswith(b'HTTP/1.1 403')
                    assert self.request('GET', '/mcp', b'Host: evil.example\r\n').startswith(b'HTTP/1.1 403')
                    assert self.request('POST', '/mcp', b'Transfer-Encoding: chunked\r\n').startswith(b'HTTP/1.1 400')
                    for bad_headers, bad_body in ((b'Content-Length: 32769\r\nContent-Type: application/json\r\n', b''),
                                                  (b'Content-Length: 2\r\nContent-Type: text/plain\r\n', b'{}'),
                                                  (b'Content-Length: 1\r\nContent-Type: application/json\r\n', b'{')):
                        assert self.request('POST', '/mcp', bad_headers, bad_body).startswith(b'HTTP/1.1 400')
                except BaseException as exc:
                    self.failure = exc
                self.stopped.wait(5)
            def shutdown(self): events.append('shutdown'); self.stopped.set()
            def server_close(self): events.append('server_close')
        monkeypatch.setattr(m, 'HTTPServer', FakeHTTP)
        real_run = m.run_adapter
        async def bounded_run(app, server, cursor, seconds):
            assert seconds == 1 and cursor is None and app.config.metadata_only
            assert app.auth is None and app.store is None
            assert app.callback is None and app.sender is None and app.reader is None
            principal = m.Principal('auth0|fixture', claims['exp'], frozenset(claims['scope'].split()))
            with pytest.raises(m.RpcError):
                app.dispatch(principal, 'tools/call', {})
            with pytest.raises(m.RpcError):
                app.subscription(principal, {}, True)
            with pytest.raises(m.RpcError):
                app.reply(principal, {'request_id': 'discord-101', 'text': 'no send'})
            assert app.expire_subscriptions() is None
            assert app.ingest({'t': 'MESSAGE_CREATE', 'd': {}}, None) == 0
            started = time.monotonic()
            result = await real_run(app, server, cursor, seconds, client_factory=denied, token_loader=denied)
            assert 0.9 <= time.monotonic() - started < 3
            if server.failure:
                raise server.failure
            assert server.stopped.is_set()
            return result
        monkeypatch.setattr(m, 'run_adapter', bounded_run)
        monkeypatch.setattr(m.os, 'umask', lambda *args: None)  # No host process policy changes in this test.
        args = ['--issuer', cfg.issuer, '--audience', cfg.audience,
                '--local-tunnel-metadata', '--port', '8765', '--max-run-seconds', '1']
        assert m.main([*args, '--metadata-only']) == 0
        assert events == ['serve', 'shutdown', 'server_close'] and len(servers) == 1
        assert list(tmp_path.iterdir()) == []
        # Full mode still rejects absent identity/cursor/state BEFORE HTTP/state construction.
        for extra in ([], ['--state-dir', str(tmp_path / 'private'), '--after-message', '100'],
                      ['--operator-subject', 'auth0|fixture', '--state-dir', str(tmp_path / 'private')],
                      ['--operator-subject', 'auth0|fixture', '--after-message', '100']):
            with pytest.raises(SystemExit) as failure:
                m.main([*args, *extra])
            assert failure.value.code == 2
        for extra in (['--operator-subject', 'auth0|fixture'], ['--operator-subject', ''],
                      ['--state-dir', str(tmp_path / 'private')], ['--after-message', '100'],
                      ['--port', '80'], ['--max-run-seconds', '0'], ['--max-run-seconds', '3601'],
                      ['--protected-resource-metadata-url', local.replace(':8765/', ':8766/')]):
            with pytest.raises(SystemExit) as failure:
                m.main([*args, '--metadata-only', *extra])
            assert failure.value.code == 2
        assert len(servers) == 1 and list(tmp_path.iterdir()) == []
        assert token not in capsys.readouterr().out


def test_cli_overnight_pilot_duration_bounds(tmp_path, monkeypatch, capsys):
    """Exercise real CLI validation; replace only effectful startup/lifespan."""
    m = module()
    accepted = []
    constructions = []
    closed = []
    def denied(*args, **kwargs):
        pytest.fail('duration CLI attempted external/credential/state I/O')
    for name in ('Authenticator', 'Store', 'create_client', 'load_token',
                 'send', 'readback', 'webhook_post'):
        monkeypatch.setattr(m, name, denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)
    class FakeApplication:
        def __init__(self, cfg, state_dir):
            self.config = cfg
            constructions.append((cfg, state_dir))
        def close(self):
            closed.append(self)
    monkeypatch.setattr(m, 'Application', FakeApplication)
    monkeypatch.setattr(m, 'HTTPServer', lambda *args: object())
    monkeypatch.setattr(m.os, 'umask', lambda *args: None)
    async def capture_run(app, server, cursor, seconds):
        accepted.append((app.config.metadata_only, cursor, seconds))
        return 0
    monkeypatch.setattr(m, 'run_adapter', capture_run)
    base = ['--issuer', 'https://tenant.example/', '--audience', 'https://rook.example/mcp',
            '--local-tunnel-metadata', '--port', '8765']
    full = ['--operator-subject', 'auth0|fixture', '--state-dir', str(tmp_path / 'private'),
            '--after-message', '100']
    for mode, flags, seconds in (
            (full, [], 1), (full, [], 3600),
            (full, ['--overnight-pilot'], 1), (full, ['--overnight-pilot'], 3600),
            (full, ['--overnight-pilot'], 3601), (full, ['--overnight-pilot'], 57600),
            (['--metadata-only'], [], 3600),
            (['--metadata-only'], ['--overnight-pilot'], 3600)):
        assert m.main([*base, *mode, *flags, '--max-run-seconds', str(seconds)]) == 0
        assert accepted[-1] == ('--metadata-only' in mode, None if '--metadata-only' in mode else '100', seconds)
    count = len(constructions)
    assert len(accepted) == len(closed) == count == 8
    for mode, flags, seconds in (
            (full, [], 0), (full, [], 3601), (full, [], 57600),
            (full, ['--overnight-pilot'], 0), (full, ['--overnight-pilot'], 57601),
            (['--metadata-only'], [], 3601),
            (['--metadata-only'], ['--overnight-pilot'], 3601),
            (['--metadata-only'], ['--overnight-pilot'], 57600)):
        with pytest.raises(SystemExit) as failure:
            m.main([*base, *mode, *flags, '--max-run-seconds', str(seconds)])
        assert failure.value.code == 2
        assert len(constructions) == len(accepted) == len(closed) == count
    assert list(tmp_path.iterdir()) == []
    capsys.readouterr()


class TestSubscriptionsDelivery:
    @pytest.fixture
    def signed_subscription(self, tmp_path):
        m = module(); cfg = config(m)
        principal = m.Principal(cfg.subject, int(time.time()) + 7200, frozenset({'rook.requests.read'}))
        params = {'name': m.EVENT, 'arguments': dict(m.ARGUMENTS), 'cursor': None, 'delivery': {
            'mode': 'webhook', 'url': 'https://callback.example/events',
            'secret': 'whsec_' + base64.b64encode(b'k' * 32).decode()}}
        deliveries = []
        from standardwebhooks.webhooks import Webhook
        def callback(url, body, headers):
            decoded = Webhook(params['delivery']['secret']).verify(body, headers)
            deliveries.append((decoded, headers, body))
            return 200, json.dumps({'challenge': decoded['challenge']}).encode() if decoded.get('type') == 'verification' else b'{}'
        app = m.Application(cfg, tmp_path / 'private', callback=callback)
        try:
            yield m, app, principal, params, deliveries
        finally:
            app.close()

    def test_r96_1_subscription_returns_refresh_before(self, signed_subscription):
        from dataclasses import replace
        from datetime import datetime
        m, app, principal, params, _ = signed_subscription
        principal = replace(principal, expires=int(time.time()) + 60)
        result = app.dispatch(principal, 'events/subscribe', params)
        assert set(result) == {'id', 'refreshBefore', 'cursor', 'truncated'}
        assert datetime.fromisoformat(result['refreshBefore'].replace('Z', '+00:00')).timestamp() == principal.expires
        assert result['cursor'] is None and result['truncated'] is False
        assert app.dispatch(principal, 'events/subscribe', params) == result

    def test_r96_2_standard_webhooks_key_boundaries(self, signed_subscription):
        m, app, principal, params, deliveries = signed_subscription
        for size in (23, 24, 31, 32, 64, 65, 128):
            params['delivery']['secret'] = 'whsec_' + base64.b64encode(b'k' * size).decode()
            before = len(deliveries)
            if 24 <= size <= 64:
                result = app.dispatch(principal, 'events/subscribe', params)
                assert deliveries[-1][0]['type'] == 'verification'
                assert deliveries[-1][1]['X-MCP-Subscription-Id'] == result['id']
                assert len(deliveries) == before + 1
            else:
                with pytest.raises(m.RpcError) as failure:
                    app.dispatch(principal, 'events/subscribe', params)
                assert failure.value.code == -32602 and failure.value.message == 'InvalidSecret'
                assert len(deliveries) == before

    def test_r96_3_ttl_suggestions_receive_bounded_grants(self, signed_subscription):
        from dataclasses import replace
        from datetime import datetime
        m, app, principal, params, deliveries = signed_subscription
        now = int(time.time())
        with patch.object(m.time, 'time', return_value=now):
            for suggestion, seconds in ((None, 3600), (1, 1), (999, 1), (1000, 1),
                                        (1001, 1.001), (300000, 300), (3600000, 3600),
                                        (3600001, 3600), (10 ** 400, 3600)):
                params['ttlMs'] = suggestion
                result = app.dispatch(principal, 'events/subscribe', params)
                granted = datetime.fromisoformat(result['refreshBefore'].replace('Z', '+00:00')).timestamp()
                assert granted == pytest.approx(now + seconds, rel=0, abs=0.000001)
                capped = app.dispatch(replace(principal, expires=now + 1), 'events/subscribe', params)
                assert datetime.fromisoformat(capped['refreshBefore'].replace('Z', '+00:00')).timestamp() == now + 1
                assert result['cursor'] is None and result['truncated'] is False
            params.pop('ttlMs')
            default = app.dispatch(principal, 'events/subscribe', params)
            assert datetime.fromisoformat(default['refreshBefore'].replace('Z', '+00:00')).timestamp() == now + 3600
            before = len(deliveries)
            for invalid in (True, False, 0, -1, 1.5, '1000', [], {}, float('nan'), float('inf')):
                with pytest.raises(m.RpcError) as failure:
                    app.dispatch(principal, 'events/subscribe', {**params, 'ttlMs': invalid})
                assert failure.value.code == -32602
            with pytest.raises(m.RpcError):
                app.dispatch(principal, 'events/subscribe', {**params, 'ttlMs': None, 'cursor': 'replay'})
            assert len(deliveries) == before
            assert app.store.get('subscriptions', default['id'])['expires'] == now + 3600
            with patch.object(m.time, 'time', return_value=now + 3600):
                app.expire_subscriptions()
            assert app.store.all('subscriptions') == []

    def test_r96_4_signed_nonreplay_event_contains_null_cursor(self, signed_subscription):
        m, app, principal, params, deliveries = signed_subscription
        result = app.dispatch(principal, 'events/subscribe', params)
        event = {'t': 'MESSAGE_CREATE', 'd': {'id': '101', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                 'author': {'id': m.OPERATOR}, 'type': 0, 'content': 'Rook: no replay'}}
        assert app.ingest(event, '100') == 1
        decoded, headers, body = deliveries[-1]
        assert 'cursor' in decoded and decoded['cursor'] is None
        assert decoded['eventId'] == headers['webhook-id'] == 'discord-101'
        assert decoded['name'] == m.EVENT and decoded['data']['text'] == 'no replay'
        assert headers['X-MCP-Subscription-Id'] == result['id']
        from standardwebhooks.webhooks import Webhook
        from standardwebhooks.exceptions import WebhookVerificationError
        tampered = json.loads(body); tampered.pop('cursor')
        with pytest.raises(WebhookVerificationError):
            Webhook(params['delivery']['secret']).verify(m.canonical(tampered).encode(), headers)
        assert app.ingest(event, '100') == 0
        assert len(deliveries) == 2  # One verified challenge and one application event.

    def test_signed_verified_persistent_restricted_delivery(self, tmp_path):
        m = module(); cfg = config(m)
        principal = m.Principal(cfg.subject, int(time.time()) + 600, frozenset({'rook.requests.read', 'rook.replies.write'}))
        secret = 'whsec_' + base64.b64encode(b'offline-synthetic-key-material-1234').decode()
        calls = []
        from standardwebhooks.webhooks import Webhook
        def callback(url, body, headers):
            decoded = Webhook(secret).verify(body, headers)
            calls.append((url, decoded, headers))
            return 200, json.dumps({'challenge': decoded['challenge']}).encode() if decoded.get('type') == 'verification' else b'{}'
        app = m.Application(cfg, tmp_path / 'private', callback=callback)
        params = {'name': m.EVENT, 'arguments': m.ARGUMENTS, 'delivery': {
            'mode': 'webhook', 'url': 'https://callback.example/events', 'secret': secret}, 'cursor': None, 'ttlMs': 300000}
        result = app.dispatch(principal, 'events/subscribe', params)
        assert result['cursor'] is None and result['truncated'] is False
        assert app.dispatch(principal, 'events/subscribe', deepcopy(params))['id'] == result['id']
        assert len(calls) == 1
        event = {'t': 'MESSAGE_CREATE', 'd': {'id': '101', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                'author': {'id': m.BOT, 'bot': True}, 'type': 0, 'content': 'Rook: explain this'}}
        assert app.ingest(event, '100') == 1  # Same Kael bot is intentionally allowed.
        assert app.ingest(event, '100') == 0
        assert calls[-1][1]['eventId'] == 'discord-101'
        assert calls[-1][1]['data']['request_id'] == 'discord-101'
        assert calls[-1][2]['X-MCP-Subscription-Id'] == result['id']
        for delta in ({'guild_id': '1'}, {'channel_id': '2'}, {'author': {'id': '777', 'bot': True}},
                      {'webhook_id': '888'}, {'content': 'not prefixed'}, {'type': 19}, {'id': '99'}):
            bad = deepcopy(event); bad['d'].update(delta)
            assert app.ingest(bad, '100') == 0
        for delta in ({'arguments': {**m.ARGUMENTS, 'channel_id': '2'}}, {'cursor': 'replay'},
                      {'delivery': {**params['delivery'], 'secret': 'whsec_bad'}},
                      {'delivery': {**params['delivery'], 'mode': 'poll'}}, {'ttlMs': -1}):
            with pytest.raises(m.RpcError):
                app.dispatch(principal, 'events/subscribe', {**params, **delta})
        with pytest.raises(m.RpcError):
            app.dispatch(m.Principal('other', principal.expires, principal.scopes), 'events/subscribe', params)
        app.close()
        app = m.Application(cfg, tmp_path / 'private', callback=callback)
        assert app.ingest({**event, 'd': {**event['d'], 'id': '102'}}, '100') == 1
        stop = {k: v for k, v in params.items() if k not in ('cursor', 'ttlMs')}
        stop['delivery'] = {k: v for k, v in stop['delivery'].items() if k != 'secret'}
        assert app.dispatch(principal, 'events/unsubscribe', stop) == {}
        assert app.dispatch(principal, 'events/unsubscribe', stop) == {}
        assert app.ingest({**event, 'd': {**event['d'], 'id': '103'}}, '100') == 0
        app.close()
        assert (tmp_path / 'private').stat().st_mode & 0o777 == 0o700
        assert (tmp_path / 'private' / 'rook.sqlite3').stat().st_mode & 0o777 == 0o600
        bad = m.Application(cfg, tmp_path / 'bad', callback=lambda *a: (200, b'{"challenge":"wrong"}'))
        with pytest.raises(m.RpcError) as failure:
            bad.dispatch(principal, 'events/subscribe', params)
        assert failure.value.code == -32015
        assert bad.ingest(event, '100') == 0
        bad.close()

    def test_rotation_retry_gone_and_token_expiry(self, tmp_path):
        m = module(); cfg = config(m)
        principal = m.Principal(cfg.subject, int(time.time()) + 300, frozenset({'rook.requests.read'}))
        secret1 = 'whsec_' + base64.b64encode(b'synthetic-old-secret-32-characters').decode()
        secret2 = 'whsec_' + base64.b64encode(b'synthetic-new-secret-32-characters').decode()
        active_secret = [secret1]; signatures = []; outcomes = []
        from standardwebhooks.webhooks import Webhook
        def callback(url, body, headers):
            value = Webhook(active_secret[0]).verify(body, headers)
            if value.get('type') == 'verification':
                return 200, json.dumps({'challenge': value['challenge']}).encode()
            signatures.append(headers['webhook-signature'])
            if outcomes:
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception): raise outcome
                return outcome, b'{}'
            return 200, b'{}'
        app = m.Application(cfg, tmp_path / 'private', callback=callback)
        params = {'name': m.EVENT, 'arguments': m.ARGUMENTS, 'delivery': {
            'mode': 'webhook', 'url': 'https://callback.example/events', 'secret': secret1}}
        first = app.dispatch(principal, 'events/subscribe', params)
        active_secret[0] = secret2
        params['delivery']['secret'] = secret2
        assert app.dispatch(principal, 'events/subscribe', params)['id'] == first['id']
        event = {'t': 'MESSAGE_CREATE', 'd': {'id': '101', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                 'author': {'id': m.OPERATOR}, 'type': 0, 'content': 'Rook: request'}}
        outcomes.extend([OSError('no details in output'), 503, 200])
        with patch.object(m.time, 'sleep', return_value=None):
            assert app.ingest(event, '100') == 1
        assert len(signatures) == 3 and all(len(s.split()) == 2 for s in signatures)
        outcomes.append(410)
        assert app.ingest({**event, 'd': {**event['d'], 'id': '102'}}, '100') == 0
        assert app.store.all('subscriptions') == []
        app.dispatch(principal, 'events/subscribe', params)
        sub = app.store.all('subscriptions')[0]; sub['expires'] = time.time() - 1
        app.store.put('subscriptions', sub['id'], sub)
        assert app.ingest({**event, 'd': {**event['d'], 'id': '103'}}, '100') == 0
        assert app.store.all('subscriptions') == []
        app.close()

    def test_https_public_address_pinning_no_redirects(self):
        m = module()
        for url in ('http://callback.example/a', 'https://user:pass@callback.example/a',
                    'https://callback.example:444/a', 'https://127.0.0.1/a', 'https://callback.example/a#x'):
            with pytest.raises(m.RpcError):
                m.callback_target(url)
        answers = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        with patch('socket.getaddrinfo', answers):
            host, address, path = m.callback_target('https://callback.example/events?q=1')
        assert (host, address, path) == ('callback.example', '93.184.216.34', '/events?q=1')
        with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('169.254.169.254', 443))]):
            with pytest.raises(m.RpcError):
                m.callback_target('https://callback.example/events')
        sock = object(); captured = []
        class TLS:
            verify_mode = 2
            check_hostname = True
            def wrap_socket(self, s, *, server_hostname):
                assert s is sock
                captured.append(server_hostname)
                return sock
        with patch('socket.create_connection', side_effect=lambda addr, timeout: captured.append(addr) or sock):
            conn = m.PinnedHTTPSConnection('callback.example', '93.184.216.34', context=TLS())
            conn.connect()
        assert captured == [('93.184.216.34', 443), 'callback.example']
        class Redirect:
            def __init__(self, *a, **k): pass
            def request(self, *a, **k): pass
            def getresponse(self):
                return type('Response', (), {'status': 302, 'read': lambda *a: b''})()
            def close(self): pass
        with patch.object(m, 'PinnedHTTPSConnection', Redirect), patch('socket.getaddrinfo', answers):
            with pytest.raises(m.RpcError):
                m.webhook_post('https://callback.example/a', b'{}', {})


def ready_request(m, tmp_path, *, sender, reader):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = config(m)
    principal = m.Principal(cfg.subject, int(time.time()) + 600, frozenset({'rook.requests.read', 'rook.replies.write'}))
    def callback(url, body, headers):
        value = json.loads(body)
        return 200, json.dumps({'challenge': value['challenge']}).encode() if value.get('type') == 'verification' else b'{}'
    app = m.Application(cfg, tmp_path / 'private', callback=callback, sender=sender, reader=reader)
    app.dispatch(principal, 'events/subscribe', {'name': m.EVENT, 'arguments': m.ARGUMENTS, 'delivery': {
        'mode': 'webhook', 'url': 'https://callback.example/events',
        'secret': 'whsec_' + base64.b64encode(b'synthetic-offline-secret-32-bytes!!').decode()}})
    event = {'t': 'MESSAGE_CREATE', 'd': {'id': '101', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
             'author': {'id': m.OPERATOR}, 'type': 0, 'content': 'Rook: a bounded request'}}
    assert app.ingest(event, '100') == 1
    return app, cfg, principal


class TestReplyBinding:
    def test_existing_sender_mention_suppression_and_bad_readback(self, tmp_path):
        m = module(); sender_module = importlib.import_module('post_discord_message'); captured = []
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b'{"id":"202"}'
        def urlopen(request, timeout):
            assert timeout == 20
            captured.append(json.loads(request.data))
            return Response()
        with patch.object(sender_module, 'load_token', return_value='synthetic-no-real-token'), patch.object(sender_module.urllib.request, 'urlopen', urlopen):
            assert sender_module.send(m.CHANNEL, '@everyone')['id'] == '202'
        assert captured == [{'content': '@everyone', 'allowed_mentions': {'parse': []}}]
        attempts = []
        def sender(channel, wire):
            attempts.append(wire)
            return {'id': '202', 'channel_id': channel, 'content': wire, 'author': {'id': m.BOT}}
        app, cfg, principal = ready_request(m, tmp_path, sender=sender, reader=lambda *a: {'id': '202', 'channel_id': 'wrong'})
        params = {'name': 'reply_to_rook_request', 'arguments': {'request_id': 'discord-101', 'text': 'answer'}}
        assert app.dispatch(principal, 'tools/call', params)['isError'] is True
        assert app.dispatch(principal, 'tools/call', params)['isError'] is True
        assert len(attempts) == 1 and app.store.get('requests', 'discord-101')['state'] == 'uncertain'
        with pytest.raises(BlockingIOError):
            m.Store(tmp_path / 'private')
        app.close()

    def test_fixed_destination_duplicate_conflict_and_uncertain_safety(self, tmp_path):
        m = module(); sent = []
        def sender(channel, content):
            assert channel == m.CHANNEL
            sent.append(content)
            return {'id': '202', 'channel_id': channel, 'content': content, 'author': {'id': m.BOT}}
        def reader(channel, identity):
            return {'id': identity, 'channel_id': channel, 'content': sent[-1], 'author': {'id': m.BOT}}
        app, cfg, principal = ready_request(m, tmp_path, sender=sender, reader=reader)
        params = {'name': 'reply_to_rook_request', 'arguments': {'request_id': 'discord-101', 'text': 'bounded @everyone reply'}}
        for delta in ({'channel_id': '3'}, {'request_id': 'discord-555'}, {'text': ''}, {'text': 'x' * 1201}):
            with pytest.raises(m.RpcError):
                app.dispatch(principal, 'tools/call', {**params, 'arguments': {**params['arguments'], **delta}})
        with pytest.raises(m.RpcError):
            app.dispatch(m.Principal('other', principal.expires, principal.scopes), 'tools/call', params)
        first = app.dispatch(principal, 'tools/call', params)
        assert first['isError'] is False
        assert first['structuredContent']['message_id'] == '202'
        assert first['structuredContent']['url'].endswith('/202')
        assert json.loads(sent[0]) == {'kind': 'rook.reply', 'correlation': 'discord-101', 'text': 'bounded @everyone reply'}
        assert app.dispatch(principal, 'tools/call', params) == first
        with pytest.raises(m.RpcError):
            app.dispatch(principal, 'tools/call', {**params, 'arguments': {**params['arguments'], 'text': 'different'}})
        assert len(sent) == 1
        # Stored reply IDs never become requests, even if Discord content changes.
        assert app.ingest({'t': 'MESSAGE_CREATE', 'd': {'id': '202', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                         'author': {'id': m.BOT}, 'type': 0, 'content': 'Rook: forged request'}}, '100') == 0
        app.close()
        app = m.Application(cfg, tmp_path / 'private', sender=sender, reader=reader)
        assert app.dispatch(principal, 'tools/call', params) == first
        assert len(sent) == 1
        app.close()
        def uncertain(*a):
            sent.append('uncertain-attempt'); raise OSError('synthetic secret must not escape')
        app, cfg, principal = ready_request(m, tmp_path / 'uncertain', sender=uncertain, reader=reader)
        result = app.dispatch(principal, 'tools/call', params)
        assert result['isError'] is True and 'synthetic secret' not in str(result)
        app.close()
        app = m.Application(cfg, tmp_path / 'uncertain' / 'private', sender=uncertain, reader=reader)
        assert app.dispatch(principal, 'tools/call', params)['isError'] is True
        assert sent.count('uncertain-attempt') == 1
        app.close()


class TestReceiveBoundReply:
    def test_same_bot_requires_exact_authenticated_private_binding(self, tmp_path):
        m = module(); r = importlib.import_module('receive_one_discord_message'); sent = []
        def sender(channel, content):
            sent.append(content)
            return {'id': '202', 'channel_id': channel, 'content': content, 'author': {'id': m.BOT}}
        def reader(channel, identity):
            return {'id': identity, 'channel_id': channel, 'content': sent[-1], 'author': {'id': m.BOT}}
        app, cfg, principal = ready_request(m, tmp_path, sender=sender, reader=reader)
        scope = r.Scope('discord-101', m.REPLY_KIND, '100', 'author', m.BOT, 2,
                        str(tmp_path / 'private'), cfg.issuer, cfg.audience, cfg.subject)
        event = {'t': 'MESSAGE_CREATE', 'd': {'id': '202', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                 'author': {'id': m.BOT, 'bot': True}, 'type': 0,
                 'content': json.dumps({'kind': 'rook.reply', 'correlation': 'discord-101', 'text': 'answer'})}}
        assert r.match_event(event, scope, m.BOT) is None
        app.dispatch(principal, 'tools/call', {'name': 'reply_to_rook_request',
                     'arguments': {'request_id': 'discord-101', 'text': 'answer'}})
        event['d']['content'] = sent[-1]
        accepted = r.match_event(event, scope, m.BOT)
        assert accepted['text'] == 'answer'
        assert accepted['authenticated_reply_binding'] is True
        for delta in ({'id': '203'}, {'content': event['d']['content'].replace('answer', 'forged')}, {'webhook_id': '888'},
                      {'author': {'id': m.OPERATOR}}, {'guild_id': '4'}):
            bad = deepcopy(event); bad['d'].update(delta)
            assert r.match_event(bad, scope, m.BOT) is None
        legacy = r.Scope('discord-101', m.REPLY_KIND, '100', 'author', m.BOT, 2)
        assert r.match_event(event, legacy, m.BOT) is None
        from dataclasses import replace
        assert r.match_event(event, replace(scope, mcp_subject='other'), m.BOT) is None
        row = app.store.get('requests', 'discord-101'); row['state'] = 'uncertain'
        app.store.put('requests', 'discord-101', row)
        assert r.match_event(event, scope, m.BOT) is None
        row['state'] = 'confirmed'; row['auth_method'] = 'marker-only'
        app.store.put('requests', 'discord-101', row)
        assert r.match_event(event, scope, m.BOT) is None
        app.close()

    def test_gateway_before_custody_commit_is_bounded_and_accepts_once(self, tmp_path):
        m = module(); r = importlib.import_module('receive_one_discord_message'); sent = []
        wire = json.dumps({'kind': m.REPLY_KIND, 'correlation': 'discord-101', 'text': 'answer'}, separators=(',', ':'))
        def sender(channel, content):
            sent.append(content)
            return {'id': '202', 'channel_id': channel, 'content': content, 'author': {'id': m.BOT}}
        def reader(channel, identity):
            return {'id': identity, 'channel_id': channel, 'content': sent[-1], 'author': {'id': m.BOT}}
        app, cfg, principal = ready_request(m, tmp_path, sender=sender, reader=reader)
        scope = r.Scope('discord-101', m.REPLY_KIND, '100', 'author', m.BOT, 2,
                        str(tmp_path / 'private'), cfg.issuer, cfg.audience, cfg.subject)
        event = {'t': 'MESSAGE_CREATE', 'd': {'id': '202', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                 'author': {'id': m.BOT}, 'type': 0, 'content': wire}}
        closed = []
        def client_factory(on_event, on_error):
            class Client:
                async def start(self, token, reconnect):
                    on_event(event, m.BOT)
                    await asyncio.sleep(0.07)
                    app.dispatch(principal, 'tools/call', {'name': 'reply_to_rook_request',
                                  'arguments': {'request_id': 'discord-101', 'text': 'answer'}})
                    await asyncio.Future()
                async def close(self): closed.append(True)
            return Client()
        result, code = asyncio.run(r.receive_one(scope, client_factory=client_factory, token_loader=lambda: 'synthetic'))
        assert code == 0 and result['authenticated_reply_binding'] is True and closed == [True]
        assert len(sent) == 1
        app.close()
