"""Four scoped offline groups; synthetic keys only, all external I/O denied."""
import base64
import asyncio
from copy import deepcopy
import importlib
import importlib.util
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
def no_external_io(monkeypatch, tmp_path):
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network denied'))
    monkeypatch.setattr(socket.socket, 'bind', lambda *a, **k: pytest.fail('listener bind denied'))
    import post_discord_message
    monkeypatch.setattr(post_discord_message, 'load_token', lambda: pytest.fail('credential read denied'))
    monkeypatch.setattr(module(), 'ROOK_TOKEN_PATH', tmp_path / 'no-live-token')


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


class TestDiscordAPI:
    @pytest.fixture(autouse=True)
    def setup(self, tmp_path, monkeypatch):
        self.m = m = module()
        monkeypatch.setattr(m, 'load_rook_token', lambda: pytest.fail('credential read denied'))
        self.app = m.Application(config(m), tmp_path / 'api-private')
        self.principal = m.Principal(self.app.config.subject, time.time() + 600, frozenset({m.CHANNEL_WRITE}))
        self.calls = []
        self.status = 200
        self.response = {'id': '501', 'name': 'new channel'}
        self.authors = {}
        self.member_roles = {identity: ['701'] for identity in m.PROTECTED_MEMBERS}
        self.channels = {
            '201': {'id': '201', 'guild_id': m.GUILD, 'type': 0},
            '202': {'id': '202', 'guild_id': '999', 'type': 0},
            '203': {'id': '203', 'type': 1},
            '204': {'id': '204', 'guild_id': m.GUILD, 'type': 11, 'parent_id': '201'},
            m.CHANNEL: {'id': m.CHANNEL, 'guild_id': m.GUILD, 'type': 0, 'parent_id': None},
            '1487300132054765629': {'id': '1487300132054765629', 'guild_id': m.GUILD, 'type': 0, 'parent_id': None},
        }
        self.transport = m.discord_request
        monkeypatch.setattr(m, 'discord_request', self.peer)
        yield
        self.app.close()

    def peer(self, method, path, payload=None, file=None, **kwargs):
        self.calls.append((method, path, deepcopy(payload), kwargs))
        if not kwargs.get('guild_scoped'):
            assert method == 'GET'
            if path.startswith(f'/guilds/{self.m.GUILD}/members/'):
                identity = path.split('/')[4]
                return {'user': {'id': identity}, 'roles': deepcopy(self.member_roles[identity])}
            if path in ['/channels/' + identity for identity in self.channels]:
                return deepcopy(self.channels[path.split('/')[2]])
            if '/messages/' in path:
                identity = path.split('/')[4]
                return {'id': identity, 'channel_id': path.split('/')[2],
                        'author': {'id': self.authors.get(identity, self.m.ROOK_BOT)}}
            raise AssertionError(path)
        return self.status, deepcopy(self.response)

    def args(self, method='GET', path=None, **extra):
        return {'operation_id': 'discord_api_test_001', 'method': method,
                'path': path or f'/guilds/{self.m.GUILD}/channels', **extra}

    def call(self, arguments):
        return self.app.dispatch(self.principal, 'tools/call', {'name': 'discord_api', 'arguments': arguments})

    def effects(self):
        return [call for call in self.calls if call[3].get('guild_scoped') and call[0] != 'GET']

    def review3_http(self, monkeypatch, invalid_channel=None, invalid_value=None, fail=False):
        """Keep discord_request and its route validators real; fake only HTTP/credentials."""
        m = self.m
        sent = []
        class Connection:
            def __init__(self, host, address):
                assert (host, address) == ('discord.com', '8.8.8.8')
            def request(_, method, path, body=None, headers=None):
                assert headers['Authorization'] == 'Bot synthetic-test-only'
                sent.append((method, path, body))
                route = path.removeprefix('/api/v10')
                if method == 'GET' and route.startswith('/channels/'):
                    identity = route.split('/')[2]
                    if identity == invalid_channel:
                        if fail:
                            raise OSError('synthetic lookup failure')
                        _.value = invalid_value
                    else:
                        _.value = deepcopy(self.channels[identity])
                else:
                    _.value = deepcopy(self.response)
            def getresponse(_):
                data = json.dumps(_.value).encode()
                return type('Response', (), {'status': 200, 'read': lambda _, maximum: data[:maximum]})()
            def close(_): pass
        monkeypatch.setattr(m, 'callback_target', lambda *a: ('discord.com', '8.8.8.8', '/'))
        monkeypatch.setattr(m, 'PinnedHTTPSConnection', Connection)
        monkeypatch.setattr(m, 'load_rook_token', lambda: 'synthetic-test-only')
        monkeypatch.setattr(m, 'discord_request', self.transport)
        return sent

    @pytest.mark.parametrize('method,suffix,body', [
        ('DELETE', '/integrations/501', {}),
        ('DELETE', '/%69ntegrations/%35%30%31', {}),
        ('PATCH', '/integrations/501', {'expire_behavior': 1}),
        ('POST', '/integrations/501/sync', {}),
        ('POST', '/prune', {'days': 1, 'include_roles': ['701']}),
        ('POST', '/%70rune', {}),
    ])
    def test_review3_indirect_member_removal_real_transport(self, monkeypatch, method, suffix, body):
        sent = self.review3_http(monkeypatch)
        result = self.call(self.args(method, f'/guilds/{self.m.GUILD}' + suffix, json=body))
        assert result['isError'], result
        assert ('IntegrationMutationDenied' if 'prune' not in suffix and '%70rune' not in suffix
                else 'GuildPruneDenied') in result['content'][0]['text']
        assert not sent and not self.app.store.all('operations')

    @pytest.mark.parametrize('suffix', ['/integrations', '/prune?days=7&include_roles=701'])
    def test_review3_indirect_member_previews_remain_allowed(self, monkeypatch, suffix):
        sent = self.review3_http(monkeypatch)
        assert not self.call(self.args('GET', f'/guilds/{self.m.GUILD}' + suffix))['isError']
        assert len(sent) == 1 and sent[0][0] == 'GET'

    @pytest.mark.parametrize('path', [
        '/applications/501/role-connections/metadata',
        '/users/@me/applications/501/role-connection',
        '/applications/501/guilds/1486887661099815126/commands/601/permissions',
        '/applications/@me/guilds/1486887661099815126/commands/601/permissions',
    ])
    def test_review3_role_connection_and_command_permissions_scope(self, monkeypatch, path):
        sent = self.review3_http(monkeypatch)
        result = self.call(self.args('PUT', path, json={'permissions': []}))
        assert result['isError'] and 'DiscordScopeDenied' in result['content'][0]['text']
        assert not sent and not self.app.store.all('operations')

    @pytest.mark.parametrize('protected', ['1554668115239899196', '1487300132054765629'])
    @pytest.mark.parametrize('method,suffix,body', [
        ('PUT', '/permissions/555', {'allow': '8', 'deny': '0', 'type': 0}),
        ('DELETE', '/permissions/555', {}),
        ('PATCH', '', {'permission_overwrites': []}),
    ])
    def test_review3_parent_category_overwrites_real_transport(self, monkeypatch, protected, method, suffix, body):
        self.channels['201']['type'] = 4
        for identity in self.m.PROTECTED_CHANNELS:
            self.channels[identity]['parent_id'] = '201' if identity == protected else None
        sent = self.review3_http(monkeypatch)
        # Encoded target must compare against canonical parent IDs.
        result = self.call(self.args(method, '/channels/%32%30%31' + suffix, json=body))
        assert result['isError'] and 'ProtectedChannelParentActionDenied' in result['content'][0]['text']
        assert not [s for s in sent if s[0] != 'GET']
        assert ('GET', '/api/v10/channels/' + protected, None) in sent

    @pytest.mark.parametrize('protected', ['1554668115239899196', '1487300132054765629'])
    @pytest.mark.parametrize('invalid', ['failure', 'null', 'missing', 'id', 'guild', 'parent', 'leading_zero'])
    def test_review3_parent_lookup_fails_closed_real_transport(self, monkeypatch, protected, invalid):
        for identity in self.m.PROTECTED_CHANNELS:
            self.channels[identity]['parent_id'] = None
        value = deepcopy(self.channels[protected])
        if invalid == 'null': value = None
        elif invalid == 'missing': value.pop('parent_id')
        elif invalid == 'id': value['id'] = '999'
        elif invalid == 'guild': value['guild_id'] = '999'
        elif invalid == 'parent': value['parent_id'] = 201
        elif invalid == 'leading_zero': value['parent_id'] = '0201'
        sent = self.review3_http(monkeypatch, protected, value, fail=invalid == 'failure')
        result = self.call(self.args('PUT', '/channels/201/permissions/555', json={'deny': '1024', 'type': 0}))
        assert result['isError'] and 'ProtectedChannelParentUnverifiable' in result['content'][0]['text']
        assert not [s for s in sent if s[0] != 'GET']

    @pytest.mark.parametrize('parent', [None, '205'])
    @pytest.mark.parametrize('method,suffix,body', [
        ('PUT', '/permissions/555', {'allow': '8', 'type': 0}),
        ('DELETE', '/permissions/555', {}),
        ('PATCH', '', {'permission_overwrites': []}),
    ])
    def test_review3_unrelated_category_overwrites_real_transport(self, monkeypatch, parent, method, suffix, body):
        self.channels['201']['type'] = 4
        for identity in self.m.PROTECTED_CHANNELS:
            self.channels[identity]['parent_id'] = parent
        sent = self.review3_http(monkeypatch)
        assert not self.call(self.args(method, '/channels/201' + suffix, json=body))['isError']
        assert {s[1] for s in sent if s[0] == 'GET'} == {
            '/api/v10/channels/' + identity for identity in self.m.PROTECTED_CHANNELS | {'201'}}
        assert len([s for s in sent if s[0] != 'GET']) == 1

    def test_review3_parent_category_cosmetic_patch_real_transport(self, monkeypatch):
        self.channels['201']['type'] = 4
        for identity in self.m.PROTECTED_CHANNELS:
            self.channels[identity]['parent_id'] = '201'
        sent = self.review3_http(monkeypatch)
        assert not self.call(self.args('PATCH', '/channels/201', json={'name': 'renamed'}))['isError']
        assert [s[0] for s in sent] == ['GET', 'PATCH']

    @pytest.mark.parametrize('path', [
        '/channels/201/messages/101/crosspost',
        '/channels/%32%30%31/messages/%31%30%31/%63rosspost',
    ])
    def test_review_crosspost_fanout_denied_before_effect(self, path):
        self.channels['201']['type'] = 5  # Local source, unknowable following guilds.
        result = self.call(self.args('POST', path))
        assert not self.effects()
        assert result['isError'] and 'DiscordPublishDenied' in result['content'][0]['text']
        assert not self.calls and not self.app.store.all('operations')

    @pytest.mark.parametrize('action', ['PATCH', 'DELETE', 'bulk-delete'])
    @pytest.mark.parametrize('published', [True, False])
    def test_review_published_message_mutation_fanout(self, monkeypatch, action, published):
        # Published edits/deletes propagate too, even after a channel changes type.
        peer = self.peer
        def message_flags(method, path, payload=None, **kwargs):
            value = peer(method, path, payload, **kwargs)
            if method == 'GET' and '/messages/' in path and not kwargs.get('guild_scoped'):
                value['flags'] = 1 if published and path.endswith('/101') else 0
            return value
        monkeypatch.setattr(self.m, 'discord_request', message_flags)
        path = '/channels/201/messages/' + ('bulk-delete' if action == 'bulk-delete' else '101')
        body = {'messages': ['102', '101']} if action == 'bulk-delete' else {'content': 'edit'}
        result = self.call(self.args('POST' if action == 'bulk-delete' else action, path, json=body))
        assert len(self.effects()) == (0 if published else 1)
        assert result['isError'] == published
        if published:
            assert 'DiscordPublishDenied' in result['content'][0]['text']

    @pytest.mark.parametrize('method,body', [('DELETE', {}), ('PATCH', {'permissions': '8'})])
    @pytest.mark.parametrize('holder', [None, '392067741500309517', '1486880423568211968'])
    def test_review_protected_role_real_transport(self, monkeypatch, method, body, holder):
        """Mock HTTP only: both inspection and effects use the actual route validator."""
        m = self.m
        sent = []
        class Connection:
            def __init__(self, host, address):
                assert (host, address) == ('discord.com', '8.8.8.8')
            def request(self, verb, path, body=None, headers=None):
                sent.append((verb, path, body))
                assert headers['Authorization'] == 'Bot synthetic-test-only'
                if verb == 'GET':
                    identity = path.rsplit('/', 1)[1]
                    assert identity in m.PROTECTED_MEMBERS
                    self.value = {'user': {'id': identity}, 'roles': ['702'] if identity == holder else []}
                else:
                    assert (verb, path) == (method, f'/api/v10/guilds/{m.GUILD}/roles/702')
                    self.value = {}
            def getresponse(self):
                data = json.dumps(self.value).encode()
                return type('Response', (), {'status': 200, 'read': lambda _, maximum: data[:maximum]})()
            def close(self): pass
        monkeypatch.setattr(m, 'callback_target', lambda *a: ('discord.com', '8.8.8.8', '/'))
        monkeypatch.setattr(m, 'PinnedHTTPSConnection', Connection)
        monkeypatch.setattr(m, 'load_rook_token', lambda: 'synthetic-test-only')
        monkeypatch.setattr(m, 'discord_request', self.transport)
        result = self.call(self.args(method, f'/guilds/{m.GUILD}/roles/702', json=body))
        reads = [path for verb, path, _ in sent if verb == 'GET']
        writes = [verb for verb, _, _ in sent if verb != 'GET']
        assert reads  # A mocked allowlist must not hide InvalidDiscordRoute.
        if holder is None:
            assert not result['isError'], result
            assert set(reads) == {f'/api/v10/guilds/{m.GUILD}/members/{identity}' for identity in m.PROTECTED_MEMBERS}
            assert writes == [method]
        else:
            assert result['isError'] and 'ProtectedMemberRoleActionDenied' in result['content'][0]['text']
            assert not writes

    @pytest.mark.parametrize('method,path', [
        ('GET', '/guilds/999/members/392067741500309517'),
        ('GET', '/guilds/1486887661099815126/members/555'),
        ('GET', '/guilds/1486887661099815126/members/392067741500309517/roles'),
        ('GET', '/guilds/1486887661099815126/members/392067741500309517?limit=1'),
        ('PATCH', '/guilds/1486887661099815126/members/392067741500309517'),
        ('DELETE', '/guilds/1486887661099815126/members/1486880423568211968'),
    ])
    def test_review_inspection_transport_stays_narrow(self, monkeypatch, method, path):
        monkeypatch.setattr(self.m, 'callback_target', lambda *a: pytest.fail('HTTP must not be reached'))
        with pytest.raises(self.m.RpcError, match='InvalidDiscordRoute'):
            self.transport(method, path)

    @pytest.mark.parametrize('destination', ['202', '203', '999', '204'])
    @pytest.mark.parametrize('kind', ['follower', 'forward', 'parent', 'guild_channel', 'event', 'guild_setting'])
    def test_review_payload_scope(self, destination, kind):
        if destination == '204':
            self.channels['204']['parent_id'] = '202'
        method, path, body = {
            'follower': ('POST', '/channels/201/followers', {'webhook_channel_id': destination}),
            'forward': ('POST', '/channels/201/messages', {'message_reference': {'type': 1, 'channel_id': destination, 'message_id': '101'}}),
            'parent': ('PATCH', '/channels/201', {'parent_id': destination}),
            'guild_channel': ('POST', f'/guilds/{self.m.GUILD}/channels', {'name': 'child', 'parent_id': destination}),
            'event': ('POST', f'/guilds/{self.m.GUILD}/scheduled-events', {'channel_id': destination}),
            'guild_setting': ('PATCH', f'/guilds/{self.m.GUILD}', {'system_channel_id': destination}),
        }[kind]
        assert self.call(self.args(method, path, json=body))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('body', [
        {'message_reference': {'type': 1, 'message_id': '101'}},
        {'message_reference': {'type': 1, 'channel_id': '201', 'guild_id': '999', 'message_id': '101'}},
        {'message': {'message_reference': {'type': 1, 'channel_id': '202'}}},
        {'channel_ids': ['201', '202']},
    ])
    def test_review_nested_or_unverifiable_references(self, body):
        assert self.call(self.args('POST', '/channels/201/messages', json=body))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('delta', [
        {'audit_reason': 'changed metadata'},
        {'path_suffix': '?unused=1'},
        {'path_suffix': '?unused=2', 'audit_reason': 'another reason'},
    ])
    def test_review_uncertain_semantic_effect(self, monkeypatch, delta):
        peer = self.peer
        def timeout(method, path, payload=None, **kwargs):
            if kwargs.get('guild_scoped'):
                self.calls.append((method, path, payload, kwargs))
                raise TimeoutError('synthetic uncertain effect')
            return peer(method, path, payload, **kwargs)
        monkeypatch.setattr(self.m, 'discord_request', timeout)
        args = self.args('POST', json={'name': 'uncertain'}, audit_reason='first reason')
        assert self.call(args)['isError']
        self.app.close()
        self.app = self.m.Application(config(self.m), self.app.store.directory)
        suffix = delta.get('path_suffix', '')
        assert self.call({**args, 'operation_id': 'changed_review_id_001',
                          'path': args['path'] + suffix,
                          **{k: v for k, v in delta.items() if k != 'path_suffix'}})['isError']
        assert len(self.effects()) == 1

    @pytest.mark.parametrize('target', ['392067741500309517', '1486880423568211968'])
    @pytest.mark.parametrize('method,body', [('DELETE', {}), ('PATCH', {'permissions': '0'})])
    def test_review_protected_roles(self, target, method, body):
        self.member_roles = {identity: [] for identity in self.m.PROTECTED_MEMBERS}
        self.member_roles[target] = ['701']
        assert self.call(self.args(method, f'/guilds/{self.m.GUILD}/roles/701', json=body))['isError']
        assert not self.effects()

    def test_review_everyone_permissions(self):
        assert self.call(self.args('PATCH', f'/guilds/{self.m.GUILD}/roles/{self.m.GUILD}', json={'permissions': '0'}))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('method', ['POST', 'GET'])
    def test_review_query_token_never_persisted_or_replayed(self, method):
        token = 'synthetic-query-token-never-store'
        args = self.args(method, path=f'/guilds/{self.m.GUILD}/channels?token={token}')
        result = self.call(args)
        replay = self.call(args)
        assert token not in self.m.canonical(self.app.store.all('operations'))
        assert token not in self.m.canonical(replay)
        assert token.encode() not in self.app.store.path.read_bytes()
        assert result['isError'] and not self.calls

    @pytest.mark.parametrize('target', ['392067741500309517', '1486880423568211968'])
    @pytest.mark.parametrize('kind', ['member', 'bulk_ban'])
    def test_review_noncanonical_protected_ids(self, target, kind):
        path = f'/guilds/{self.m.GUILD}/members/0{target}' if kind == 'member' else f'/guilds/{self.m.GUILD}/bulk-ban'
        assert self.call(self.args('DELETE' if kind == 'member' else 'POST', path,
                                   json={} if kind == 'member' else {'user_ids': ['0' + target]}))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('channel', ['1554668115239899196', '1487300132054765629'])
    def test_review_protected_channel_permission_sync(self, channel):
        assert self.call(self.args('PATCH', '/channels/' + channel,
                                   json={'parent_id': '201', 'lock_permissions': True}))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('body', [{'owner_id': '555'}, {'owner_id': '392067741500309517'}])
    def test_review_ownership_transfer(self, body):
        assert self.call(self.args('PATCH', f'/guilds/{self.m.GUILD}', json=body))['isError']
        assert not self.effects()

    def test_review_mfa_change(self):
        assert self.call(self.args('POST', f'/guilds/{self.m.GUILD}/mfa', json={'level': 0}))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('channel', ['1554668115239899196', '1487300132054765629'])
    @pytest.mark.parametrize('body', [ {'lock_permissions': True}, {'parent_id': '201'} ])
    def test_review_protected_channel_position_array(self, channel, body):
        assert self.call(self.args('PATCH', f'/guilds/{self.m.GUILD}/channels',
                                   json=[{'id': channel, **body}]))['isError']
        assert not self.effects()

    def test_review_webhook_response_token_not_stored(self):
        token = 'synthetic-webhook-token-never-store'
        self.response = {'id': '501', 'token': token, 'channel_id': '201'}
        args = self.args('POST', '/channels/201/webhooks', json={'name': 'Rook hook'})
        assert not self.call(args)['isError']
        assert token not in self.m.canonical(self.app.store.all('operations'))
        assert token not in self.m.canonical(self.call(args))
        assert token.encode() not in self.app.store.path.read_bytes()
        assert len(self.effects()) == 1

    @pytest.mark.parametrize('method,body', [('DELETE', {}), ('PATCH', {'permissions': '8'})])
    def test_unheld_role_mutation_verifies_both_protected_members(self, method, body):
        self.member_roles = {identity: [] for identity in self.m.PROTECTED_MEMBERS}
        assert not self.call(self.args(method, f'/guilds/{self.m.GUILD}/roles/702', json=body))['isError']
        assert {c[1] for c in self.calls[:-1]} == {
            f'/guilds/{self.m.GUILD}/members/{identity}' for identity in self.m.PROTECTED_MEMBERS}
        assert len(self.effects()) == 1

    def test_protected_role_cosmetic_patch_remains_allowed(self):
        assert not self.call(self.args('PATCH', f'/guilds/{self.m.GUILD}/roles/701', json={'name': 'cosmetic'}))['isError']
        assert len(self.calls) == len(self.effects()) == 1

    @pytest.mark.parametrize('response', [None, {}, {'user': {'id': '999'}, 'roles': []},
        {'user': {'id': '1486880423568211968'}, 'roles': None},
        {'user': {'id': '1486880423568211968'}, 'roles': ['0701']}])
    def test_role_context_unverifiable_denied(self, monkeypatch, response):
        peer = self.peer
        def invalid(method, path, payload=None, **kwargs):
            if '/members/' in path and not kwargs.get('guild_scoped'):
                return deepcopy(response)
            return peer(method, path, payload, **kwargs)
        monkeypatch.setattr(self.m, 'discord_request', invalid)
        assert self.call(self.args('DELETE', f'/guilds/{self.m.GUILD}/roles/702'))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('body', [
        {'webhook_channel_id': '204'},
        {'message_reference': {'type': 1, 'channel_id': '204', 'guild_id': '1486887661099815126', 'message_id': '101'}},
        {'parent_id': '201'}, {'channel_id': '201'}, {'system_channel_id': '201'},
        {'message': {'message_reference': {'channel_id': '201', 'message_id': '101'}}},
    ])
    def test_in_guild_body_references_allowed(self, body):
        assert not self.call(self.args('POST', '/channels/201/messages', json=body))['isError']
        assert len(self.effects()) == 1
        assert all(c[0] == 'GET' for c in self.calls[:-1])

    @pytest.mark.parametrize('body', [
        [{'id': '201', 'parent_id': '204', 'lock_permissions': True}],
        [{'id': '1554668115239899196', 'position': 2}],
    ])
    def test_safe_channel_position_arrays_allowed(self, body):
        assert not self.call(self.args('PATCH', f'/guilds/{self.m.GUILD}/channels', json=body))['isError']
        assert self.effects()[0][2] == body

    @pytest.mark.parametrize('body', [[{'id': '202', 'position': 2}], [{'id': '201', 'parent_id': '202'}],
        [{'id': '01554668115239899196', 'lock_permissions': True}]])
    def test_foreign_or_noncanonical_channel_positions_denied(self, body):
        assert self.call(self.args('PATCH', f'/guilds/{self.m.GUILD}/channels', json=body))['isError']
        assert not self.effects()

    def test_thread_parent_cycle_denied(self):
        self.channels['204']['parent_id'] = '204'
        assert self.call(self.args('POST', '/channels/204/messages', json={'content': 'no'}))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('query', ['token=synthetic', 'unused=1', 'limit=25&limit=26',
        'before=0101', 'limit=synthetic', 'limit=', 'limit=1&token=synthetic', 'limit=%FF'])
    def test_unknown_duplicate_or_invalid_read_query_denied(self, query):
        assert self.call(self.args(path='/channels/201/messages?' + query))['isError']
        assert not self.calls and not self.app.store.all('operations')

    @pytest.mark.parametrize('body', [
        {'message_reference': {'type': '1', 'message_id': '101'}},
        {'message_reference': {'type': True, 'message_id': '101'}},
        {'components': [{'type': 8, 'default_values': [{'type': 'channel', 'id': '202'}]}]},
        {'thread_id': '202'}, {'source_guild_id': '999'}, {'guild_ids': ['1486887661099815126', '999']},
    ])
    def test_additional_body_channel_and_guild_references_denied(self, body):
        assert self.call(self.args('POST', '/channels/201/messages', json=body))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('path', [
        '/channels/201/users/@me/threads/archived/private?before=101&limit=25',
        '/channels/201/messages/pins?before=2026-09-30T00:00:00Z&limit=25',
    ])
    def test_documented_timestamp_and_snowflake_pagination(self, path):
        assert not self.call(self.args(path=path))['isError']

    def test_allowlisted_read_query_is_normalized_and_separate_from_route(self):
        args = self.args(path='/channels/%32%30%31/messages?limit=025&before=%31%30%31')
        got = self.call(args)['structuredContent']
        assert self.calls[-1][1] == '/channels/201/messages?before=101&limit=25'
        assert got['path'] == '/channels/201/messages'
        assert got['query'] == {'before': '101', 'limit': '25'}
        assert not self.app.store.all('operations')

    def test_confirmed_audit_reason_change_replays_without_rest(self):
        args = self.args('POST', '/channels/201/messages', json={'content': 'once'}, audit_reason='original')
        assert not self.call(args)['isError']
        before = len(self.calls)
        replay = self.call({**args, 'audit_reason': 'changed', 'path': '/channels/%32%30%31/messages'})
        assert not replay['isError'] and replay['structuredContent']['replayed']
        assert len(self.calls) == before

    def test_allowed_guild_path_and_direct_get(self):
        args = self.args(path=f'/guilds/{self.m.GUILD}/roles')
        got = self.call(args)
        assert not got['isError']
        assert got['structuredContent']['status'] == 200
        assert got['structuredContent']['response'] == self.response
        assert not self.app.store.all('operations')
        assert self.call(args) == got and len(self.calls) == 2

    @pytest.mark.parametrize('path', ['/guilds/999/channels', '/channels/202/messages',
        '/channels/203/messages', '/users/392067741500309517', '/users/@me/channels',
        '/users/@me/guilds/1486887661099815126', '/applications/123', '/gateway',
        '/webhooks/123/token', '/oauth2/@me', '/interactions/123/token/callback',
        'https://discord.com/api/v10/guilds/1486887661099815126',
        '/guilds/1486887661099815126/../999', '/guilds/1486887661099815126/%2e%2e/999',
        '/guilds/1486887661099815126/%2fusers', '/guilds/1486887661099815126/channels#fragment'])
    def test_foreign_dm_other_user_and_unsafe_paths_rejected(self, path):
        assert self.call(self.args(path=path))['isError']
        assert not self.effects()
        assert not any(c[3].get('guild_scoped') for c in self.calls)

    def test_thread_parent_must_be_in_guild(self):
        assert not self.call(self.args(path='/channels/204/messages'))['isError']
        assert [c[1] for c in self.calls[:2]] == ['/channels/204', '/channels/201']
        self.channels['204']['parent_id'] = '202'
        assert self.call(self.args('POST', '/channels/204/messages', json={'content': 'no'}))['isError']
        assert not self.effects()

    def test_guild_deletion_denied(self):
        assert self.call(self.args('DELETE', f'/guilds/{self.m.GUILD}'))['isError']
        assert not self.effects()

    def test_guild_leaving_denied(self):
        assert self.call(self.args('DELETE', f'/users/@me/guilds/{self.m.GUILD}'))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('target', ['392067741500309517', '1486880423568211968'])
    @pytest.mark.parametrize('action', ['kick', 'ban', 'timeout', 'roles', 'role_add', 'role_remove', 'nickname', 'bulk_ban'])
    def test_protected_member_actions_denied(self, target, action):
        base = f'/guilds/{self.m.GUILD}'
        method, path, body = {
            'kick': ('DELETE', f'{base}/members/{target}', {}),
            'ban': ('PUT', f'{base}/bans/{target}', {}),
            'timeout': ('PATCH', f'{base}/members/{target}', {'communication_disabled_until': None}),
            'roles': ('PATCH', f'{base}/members/{target}', {'roles': []}),
            'role_add': ('PUT', f'{base}/members/{target}/roles/555', {}),
            'role_remove': ('DELETE', f'{base}/members/{target}/roles/555', {}),
            'nickname': ('PATCH', f'{base}/members/{target}', {'nick': 'changed'}),
            'bulk_ban': ('POST', f'{base}/bulk-ban', {'user_ids': ['123', target]}),
        }[action]
        assert self.call(self.args(method, path, json=body))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('channel', ['1554668115239899196', '1487300132054765629'])
    @pytest.mark.parametrize('action', ['delete_channel', 'put_overwrite', 'delete_overwrite', 'patch_overwrites'])
    def test_protected_channel_delete_and_overwrites_denied(self, channel, action):
        method, suffix, body = {
            'delete_channel': ('DELETE', '', {}),
            'put_overwrite': ('PUT', '/permissions/555', {'allow': '8'}),
            'delete_overwrite': ('DELETE', '/permissions/555', {}),
            'patch_overwrites': ('PATCH', '', {'permission_overwrites': []}),
        }[action]
        assert self.call(self.args(method, '/channels/' + channel + suffix, json=body))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('method', ['PATCH', 'DELETE', 'bulk-delete'])
    def test_kael_message_mutation_denied(self, method):
        self.authors['101'] = self.m.BOT
        path = '/channels/201/messages/' + ('bulk-delete' if method == 'bulk-delete' else '101')
        body = {'messages': ['102', '101']} if method == 'bulk-delete' else {'content': 'changed'}
        assert self.call(self.args('POST' if method == 'bulk-delete' else method, path, json=body))['isError']
        assert not self.effects()

    @pytest.mark.parametrize('path,body', [('/users/@me', {'avatar': 'data:image/png;base64,c3ludGhldGlj', 'banner': None, 'username': 'Rook'}),
        ('/applications/@me', {'description': 'Rook bot bio'})])
    def test_own_profile_patch_allowed_and_receipt_has_no_body(self, path, body):
        self.response = {'id': self.m.ROOK_BOT, **body}
        got = self.call(self.args('PATCH', path, json=body))
        assert not got['isError'] and got['structuredContent']['response'] == self.response
        receipts = self.m.canonical(self.app.store.all('operations'))
        assert all(value not in receipts for value in body.values() if isinstance(value, str))
        assert len(self.effects()) == 1

    def test_non_get_replay_returns_stored_metadata_without_another_request(self):
        args = self.args('POST', json={'name': 'new channel', 'type': 0})
        got = self.call(args)
        assert got['structuredContent']['response'] == self.response
        old = deepcopy(self.app.store.all('operations')[0])
        before = len(self.calls)
        self.app.close()
        self.app = self.m.Application(config(self.m), self.app.store.directory)
        assert self.call(args) == old['result']
        assert len(self.calls) == before
        assert old['method'] == 'POST' and old['path'] == args['path'] and old['status'] == 200
        assert old['ids']['id'] == '501' and 'new channel' not in self.m.canonical(old)
        assert self.call({**args, 'json': {'name': 'conflict'}})['isError']
        assert len(self.calls) == before

    def test_uncertain_non_get_never_retried_even_with_changed_id(self, monkeypatch):
        peer = self.peer
        def timeout(method, path, payload=None, **kwargs):
            if kwargs.get('guild_scoped'):
                self.calls.append((method, path, payload, kwargs))
                raise TimeoutError('synthetic uncertain effect')
            return peer(method, path, payload, **kwargs)
        monkeypatch.setattr(self.m, 'discord_request', timeout)
        args = self.args('POST', json={'name': 'uncertain'})
        got = self.call(args)
        assert got['isError'] and len(self.effects()) == 1
        self.app.close()
        self.app = self.m.Application(config(self.m), self.app.store.directory)
        assert self.call(args) == got
        assert self.call({**args, 'operation_id': 'changed_api_id_001'}) == got
        assert len(self.effects()) == 1

    def test_default_mentions_and_explicit_override(self):
        args = self.args('POST', '/channels/201/messages', json={'content': '@everyone @here <@&123> <@456>'})
        assert not self.call(args)['isError']
        assert self.effects()[0][2]['allowed_mentions'] == {'parse': ['users'], 'replied_user': False}
        assert 'allowed_mentions' not in args['json']
        args['operation_id'] = 'explicit_mentions_001'
        args['json']['allowed_mentions'] = {'parse': []}
        assert not self.call(args)['isError']
        assert self.effects()[-1][2]['allowed_mentions'] == {'parse': []}

    def test_large_get_truncated_and_not_persisted(self):
        self.response = [{'id': str(i + 1), 'content': '雪' * 2000} for i in range(100)]
        got = self.call(self.args())['structuredContent']
        assert got['truncated'] and len(self.m.canonical(got)) < 65000
        assert not self.app.store.all('operations')

    def test_discord_4xx_is_tool_error_with_message_and_no_retry(self, monkeypatch):
        def denied(method, path, payload=None, **kwargs):
            self.calls.append((method, path, payload, kwargs))
            raise self.m.RpcError(-32030, 'Missing Permissions', {'status': 403, 'discord_code': 50013})
        monkeypatch.setattr(self.m, 'discord_request', denied)
        args = self.args('POST', json={'name': 'denied'})
        got = self.call(args)
        assert got['isError'] and 'Missing Permissions' in got['content'][0]['text']
        assert got['structuredContent']['status'] == 403
        assert self.call(args)['isError'] and len(self.effects()) == 1

    @pytest.mark.parametrize('delta', [{'method': 'TRACE'}, {'operation_id': 'bad'}, {'json': []},
        {'audit_reason': 123}, {'audit_reason': 'bad\r\nheader'}, {'path': 123}, {'file': {}}])
    def test_invalid_arguments_rejected(self, delta):
        assert self.call({**self.args('POST'), **delta})['isError']
        assert not self.calls and not self.app.store.all('operations')

    @pytest.mark.parametrize('method,path,body', [
        ('PATCH', '/channels/201', {'name': 'renamed'}),
        ('DELETE', '/channels/201', {}),
        ('PATCH', '/channels/201/messages/101', {'content': 'edit non-Kael'}),
        ('DELETE', '/channels/201/messages/101', {}),
        ('POST', '/channels/201/messages/bulk-delete', {'messages': ['101', '102']}),
        ('PUT', '/channels/201/messages/101/reactions/%F0%9F%91%8D/@me', {}),
        ('PUT', '/channels/201/pins/101', {}),
        ('POST', '/guilds/1486887661099815126/roles', {'name': 'team'}),
        ('PUT', '/guilds/1486887661099815126/members/555/roles/501', {}),
        ('PATCH', '/guilds/1486887661099815126/members/1486880423568211968', {'mute': False}),
        ('POST', '/guilds/1486887661099815126/emojis', {'name': 'rook'}),
        ('POST', '/guilds/1486887661099815126/scheduled-events', {'name': 'event'}),
    ])
    def test_general_administrator_routes_allowed(self, method, path, body):
        assert not self.call(self.args(method, path, json=body))['isError']
        assert len(self.effects()) == 1

    def test_forum_message_default_mentions(self):
        args = self.args('POST', '/channels/201/threads', json={'name': 'topic', 'message': {'content': '@everyone'}})
        assert not self.call(args)['isError']
        assert self.effects()[0][2]['message']['allowed_mentions'] == {'parse': ['users'], 'replied_user': False}
        assert 'allowed_mentions' not in args['json']['message']

    def test_encoded_denials_and_numeric_bulk_ban_target(self):
        assert self.call(self.args('DELETE', '/guilds/%31' + self.m.GUILD[1:]))['isError']
        assert self.call(self.args('PATCH', '/channels/201/messages/%31%30%31', json={'content': 'edit'}))['isError'] is False
        self.authors['101'] = self.m.BOT
        assert self.call(self.args('DELETE', '/channels/201/messages/%31%30%31'))['isError']
        assert self.call(self.args('POST', f'/guilds/{self.m.GUILD}/bulk-ban', json={'user_ids': [int(self.m.BOT)]}))['isError']
        assert len(self.effects()) == 1

    def test_oversized_object_and_bad_argument_types(self):
        self.response = {'content': '\\' * 100000}
        got = self.call(self.args())['structuredContent']
        assert got['truncated'] and len(self.m.canonical(got)) < 65000
        for delta in ({'method': []}, {'path': '/guilds/' + self.m.GUILD + '/%FF'}):
            assert self.call({**self.args(), **delta})['isError']

    def test_http_large_avatar_envelope_without_socket_or_credentials(self, monkeypatch):
        m = self.m
        monkeypatch.setattr(self.app.auth, 'authenticate', lambda *a: self.principal)
        class MemorySocket:
            def __init__(self, raw): self.input = io.BytesIO(raw); self.output = bytearray()
            def makefile(self, *args): return self.input
            def settimeout(self, timeout): pass
            def sendall(self, data): self.output.extend(data)
        handler = m.make_handler(self.app, {'127.0.0.1:8765'})
        def request(body):
            wire = m.canonical(body).encode()
            raw = (f'POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nAuthorization: Bearer synthetic-only\r\n'
                   f'MCP-Protocol-Version: {m.VERSION}\r\nContent-Type: application/json\r\nContent-Length: {len(wire)}\r\n\r\n').encode() + wire
            sock = MemorySocket(raw)
            handler(sock, ('127.0.0.1', 1), object())
            return bytes(sock.output)
        args = self.args('PATCH', '/users/@me', json={'avatar': 'data:image/png;base64,' + 'x' * 33000})
        result = request(rpc(m, 'tools/call', {'name': 'discord_api', 'arguments': args}))
        assert result.startswith(b'HTTP/1.1 200') and not json.loads(result.split(b'\r\n\r\n')[1])['result']['isError']
        assert len(self.effects()) == 1
        args['json']['avatar'] = 'x' * m.FILE_HTTP_LIMIT
        assert request(rpc(m, 'tools/call', {'name': 'discord_api', 'arguments': args})).startswith(b'HTTP/1.1 400')
        assert request(rpc(m, 'tools/call', {'name': 'send_rook_message', 'arguments': {'operation_id': 'large_legacy_0001', 'text': 'x' * 33000}})).startswith(b'HTTP/1.1 400')
        assert len(self.effects()) == 1

    def test_transport_status_audit_header_json_4xx_and_no_redirect(self, monkeypatch):
        m = self.m
        transport = self.transport
        sent = []
        class Response:
            status = 201
            data = b'{"id":"501"}'
            def read(self, maximum): return self.data[:maximum]
        response = Response()
        class Connection:
            def __init__(self, host, address): assert (host, address) == ('discord.com', '8.8.8.8')
            def request(self, method, path, body=None, headers=None): sent.append((method, path, body, headers))
            def getresponse(self): return response
            def close(self): sent.append(('close',))
        monkeypatch.setattr(m, 'callback_target', lambda *a: ('discord.com', '8.8.8.8', 443))
        monkeypatch.setattr(m, 'PinnedHTTPSConnection', Connection)
        monkeypatch.setattr(m, 'load_rook_token', lambda: 'synthetic-test-only')
        path = f'/guilds/{m.GUILD}/channels'
        assert transport('POST', path, {'name': 'room'}, guild_scoped=True, audit_reason='Rook: café / reason') == (201, {'id': '501'})
        assert sent[-2][1] == '/api/v10' + path
        assert sent[-2][3]['Authorization'] == 'Bot synthetic-test-only'
        assert sent[-2][3]['X-Audit-Log-Reason'] == 'Rook%3A%20caf%C3%A9%20%2F%20reason'
        response.status, response.data = 403, b'{"code":50013,"message":"Missing Permissions"}'
        with pytest.raises(m.RpcError) as error:
            transport('GET', path, guild_scoped=True)
        assert error.value.message == 'Missing Permissions' and error.value.data['status'] == 403
        for status in (302, 503):
            response.status = status
            before = len(sent)
            with pytest.raises(m.RpcError): transport('POST', path, {}, guild_scoped=True)
            assert len(sent) == before + 2 and sent[-1] == ('close',)
        response.status, response.data = 204, b''
        assert transport('DELETE', path + '/501', guild_scoped=True) == (204, {})

    def test_discovery_and_existing_write_scope(self):
        principal = self.m.Principal(self.principal.subject, self.principal.expires, frozenset({'rook.requests.read'}))
        tools = self.app.dispatch(principal, 'tools/list', {})['tools']
        definition = next(t for t in tools if t['name'] == 'discord_api')
        assert definition['securitySchemes'] == [{'type': 'oauth2', 'scopes': [self.m.CHANNEL_WRITE]}]
        assert definition['annotations']['destructiveHint'] is True
        assert definition['inputSchema']['required'] == ['operation_id', 'method', 'path']
        for detail in ('Administrator', self.m.GUILD, '/users/@me', 'data:image/png;base64', 'Kael'):
            assert detail in definition['description']
        for scope in ('rook.channel.read', 'rook.replies.write'):
            self.principal = self.m.Principal(principal.subject, principal.expires, frozenset({scope}))
            with pytest.raises(self.m.RpcError):
                self.call(self.args())
        assert not self.calls


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

    @pytest.mark.parametrize('token', ['<@1554962007059472445>', '<@!1554962007059472445>'])
    @pytest.mark.parametrize('sender_id', ['392067741500309517', '1486880423568211968'])
    @pytest.mark.parametrize('with_mentions', [True, False])
    @pytest.mark.parametrize('message_type', [0, 19])
    def test_rook_user_mention_creates_stripped_request(self, signed_subscription, token, sender_id, with_mentions, message_type):
        m, app, principal, params, deliveries = signed_subscription
        app.dispatch(principal, 'events/subscribe', params)
        event = {'t': 'MESSAGE_CREATE', 'd': {'id': '101', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                 'author': {'id': sender_id}, 'type': message_type,
                 'content': f' \t explain this {token} \n<@!{m.ROOK_BOT}>\t'}}
        if with_mentions:
            event['d']['mentions'] = [{'id': m.ROOK_BOT}]
        assert app.ingest(event, '100') == 1
        assert deliveries[-1][0]['data']['text'] == 'explain this'
        assert deliveries[-1][0]['data']['sender_id'] == sender_id
        assert app.store.get('requests', 'discord-101')['state'] == 'ready'
        assert app.ingest(event, '100') == 0
        assert len(app.store.all('requests')) == 1
        assert len(deliveries) == 2

    @pytest.mark.parametrize('delta', [
        {'author': {'id': '1554962007059472445', 'bot': True}},
        {'content': 'Rook: self echo', 'author': {'id': '1554962007059472445', 'bot': True}},
        {'content': '@everyone explain this', 'mentions': [], 'mention_everyone': True},
        {'content': '@everyone explain this', 'mention_everyone': True},
        {'content': '@here explain this', 'mentions': [], 'mention_everyone': True},
        {'content': '@here explain this', 'mention_everyone': True},
        {'content': '<@&1554962007059472445> explain this', 'mentions': [],
         'mention_roles': ['1554962007059472445']},
        {'content': '<@&1554962007059472445> explain this', 'mention_roles': ['1554962007059472445']},
        {'content': '<@777> explain this', 'mentions': [{'id': '777'}]},
        {'content': '<@777> explain this'},
        {'content': ' \t<@1554962007059472445>\n<@!1554962007059472445> \t'},
        {'content': ' \t<@1554962007059472445>\n<@!1554962007059472445> \t',
         'mentions': [{'id': '1554962007059472445'}]},
        {'guild_id': '1'}, {'channel_id': '2'}, {'type': 7}, {'type': 20}, {'webhook_id': '888'},
        {'author': {'id': '777', 'bot': True}}, {'author': {'id': '777'}}, {'id': '100'},
        {'content': 'explain this', 'mentions': []},
        {'content': 'explain this'},
        {'content': '<@1554962007059472445>' + 'x' * 2000},
        {'mentions': []},
        {'mentions': [{'id': '777'}]},
        {'mentions': None},
        {'mentions': {'id': '1554962007059472445'}},
        {'mentions': [None, '1554962007059472445', {'id': 1554962007059472445}]},
    ])
    @pytest.mark.parametrize('message_type', [0, 19])
    def test_rook_mention_rejects_echo_nonuser_and_empty_requests(self, signed_subscription, delta, message_type):
        m, app, principal, params, deliveries = signed_subscription
        app.dispatch(principal, 'events/subscribe', params)
        event = {'t': 'MESSAGE_CREATE', 'd': {'id': '101', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                 'author': {'id': m.OPERATOR}, 'type': message_type, 'content': f'<@{m.ROOK_BOT}> explain this'}}
        event['d'].update(delta)
        assert app.ingest(event, '100') == 0
        assert app.store.all('requests') == []
        assert app.store.all('deliveries') == []
        assert len(deliveries) == 1

    @pytest.mark.parametrize('message_type', [0, 19])
    @pytest.mark.parametrize('sender_id', ['392067741500309517', '1486880423568211968'])
    def test_rook_mention_metadata_without_body_token_is_rejected(self, signed_subscription, message_type, sender_id):
        m, app, principal, params, deliveries = signed_subscription
        app.dispatch(principal, 'events/subscribe', params)
        event = {'t': 'MESSAGE_CREATE', 'd': {'id': '101', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                 'author': {'id': sender_id}, 'type': message_type, 'content': '  explain this  ',
                 'mentions': [{'id': '777'}, {'id': m.ROOK_BOT}]}}
        assert app.ingest(event, '100') == 0
        assert app.store.all('requests') == []
        assert app.store.all('deliveries') == []
        assert len(deliveries) == 1

    def test_rook_legacy_prefix_keeps_existing_text_extraction(self, signed_subscription):
        m, app, principal, params, deliveries = signed_subscription
        app.dispatch(principal, 'events/subscribe', params)
        event = {'t': 'MESSAGE_CREATE', 'd': {'id': '101', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                 'author': {'id': m.OPERATOR}, 'type': 0, 'content': 'Rook: \t explain this \n', 'mentions': []}}
        assert app.ingest(event, '100') == 1
        assert deliveries[-1][0]['data']['text'] == 'explain this'
        event['d'].update(id='102', content='Rook: \t\n')
        assert app.ingest(event, '100') == 0
        assert app.store.get('requests', 'discord-102') is None
        assert len(deliveries) == 2

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
                      {'webhook_id': '888'}, {'content': 'not prefixed'}, {'type': 20}, {'id': '99'}):
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
    @pytest.mark.parametrize('text', ['# Heading\n**Bold**\n- item\n```python\nprint(1)\n```\n', 'x' * 1850])
    def test_markdown_verbatim_footer_and_digest(self, tmp_path, text):
        import hashlib
        m = module(); sent = []
        def sender(channel, content):
            sent.append(content)
            return {'id': '202', 'channel_id': channel, 'content': content, 'author': {'id': m.ROOK_BOT}}
        def reader(channel, identity):
            return {'id': identity, 'channel_id': channel, 'content': sent[-1], 'author': {'id': m.ROOK_BOT}}
        app, cfg, principal = ready_request(m, tmp_path, sender=sender, reader=reader)
        try:
            args = {'request_id': 'discord-101', 'text': text}
            first = app.reply(principal, args)
            assert first['isError'] is False
            assert sent == [text + '\n-# rook.reply · discord-101']
            assert len(sent[0].encode('utf-16-le')) // 2 <= 2000
            row = app.store.get('requests', 'discord-101')
            assert row['reply_sha256'] == hashlib.sha256(sent[0].encode()).hexdigest()
            assert row['reply_kind'] == 'rook.reply' and row['reply_id'] == '202'
            assert app.reply(principal, args) == first and len(sent) == 1
            schema = m.TOOL_DEFINITION['inputSchema']['properties']['text']
            assert schema['maxLength'] == 1850
            assert 'Discord markdown' in m.TOOL_DEFINITION['description']
        finally:
            app.close()

    @pytest.mark.parametrize('units_over', [0, 1])
    def test_final_utf16_limit(self, tmp_path, units_over):
        m = module(); sent = []
        def sender(channel, content):
            sent.append(content)
            return {'id': '202', 'channel_id': channel, 'content': content, 'author': {'id': m.ROOK_BOT}}
        def reader(channel, identity):
            return {'id': identity, 'channel_id': channel, 'content': sent[-1], 'author': {'id': m.ROOK_BOT}}
        app, cfg, principal = ready_request(m, tmp_path, sender=sender, reader=reader)
        footer = '\n-# rook.reply · discord-101'
        budget = 2000 - len(footer)
        text = '😀' * (budget // 2) + 'x' * (budget % 2 + units_over)
        try:
            if units_over:
                with pytest.raises(m.RpcError, match='ReplyTooLarge'):
                    app.reply(principal, {'request_id': 'discord-101', 'text': text})
                assert not sent and app.store.get('requests', 'discord-101')['state'] == 'ready'
            else:
                assert not app.reply(principal, {'request_id': 'discord-101', 'text': text})['isError']
                assert len(sent[0].encode('utf-16-le')) // 2 == 2000
        finally:
            app.close()

    @pytest.mark.parametrize('state', ['confirmed', 'sending', 'uncertain'])
    def test_legacy_reply_retry_returns_stored_result_without_resend(self, tmp_path, state):
        import hashlib
        m = module()
        def forbidden(*a): pytest.fail('legacy reply must never resend')
        app, cfg, principal = ready_request(m, tmp_path, sender=forbidden, reader=forbidden)
        args = {'request_id': 'discord-101', 'text': 'legacy\n**answer**'}
        legacy = json.dumps({'kind': m.REPLY_KIND, 'correlation': args['request_id'], 'text': args['text']},
                            ensure_ascii=False, separators=(',', ':'))
        row = app.store.get('requests', args['request_id'])
        result = {'isError': state != 'confirmed', 'content': [{'type': 'text', 'text': 'stored outcome'}]}
        row.update(state=state, reply_sha256=hashlib.sha256(legacy.encode()).hexdigest(), result=result)
        app.store.put('requests', args['request_id'], row)
        try:
            assert app.reply(principal, args) == result
            assert app.store.get('requests', args['request_id']) == row
            with pytest.raises(m.RpcError, match='ReplyConflict'):
                app.reply(principal, {**args, 'text': 'different'})
        finally:
            app.close()

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
            return {'id': '202', 'channel_id': channel, 'content': wire, 'author': {'id': m.ROOK_BOT}}
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
            return {'id': '202', 'channel_id': channel, 'content': content, 'author': {'id': m.ROOK_BOT}}
        def reader(channel, identity):
            return {'id': identity, 'channel_id': channel, 'content': sent[-1], 'author': {'id': m.ROOK_BOT}}
        app, cfg, principal = ready_request(m, tmp_path, sender=sender, reader=reader)
        params = {'name': 'reply_to_rook_request', 'arguments': {'request_id': 'discord-101', 'text': 'bounded @everyone reply'}}
        for delta in ({'channel_id': '3'}, {'request_id': 'discord-555'}, {'request_id': 'discord-101\nforged'},
                      {'request_id': 'discord-01'}, {'request_id': 'discord-' + '1' * 21},
                      {'text': ''}, {'text': 'x' * 1851}):
            with pytest.raises(m.RpcError):
                app.dispatch(principal, 'tools/call', {**params, 'arguments': {**params['arguments'], **delta}})
        with pytest.raises(m.RpcError):
            app.dispatch(m.Principal('other', principal.expires, principal.scopes), 'tools/call', params)
        first = app.dispatch(principal, 'tools/call', params)
        assert first['isError'] is False
        assert first['structuredContent']['message_id'] == '202'
        assert first['structuredContent']['url'].endswith('/202')
        assert sent[0] == 'bounded @everyone reply\n-# rook.reply · discord-101'
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
    def test_default_kael_receiver_accepts_custody_bound_rook_reply(self, tmp_path, monkeypatch, capsys):
        """Real CLI/default loader with synthetic ENV_PATH; observer != reply author."""
        m = module(); sent = []
        def sender(channel, content):
            sent.append(content)
            return {'id': '202', 'channel_id': channel, 'content': content, 'author': {'id': m.ROOK_BOT}}
        def readback(channel, identity):
            return {'id': identity, 'channel_id': channel, 'content': sent[-1], 'author': {'id': m.ROOK_BOT}}
        app, cfg, principal = ready_request(m, tmp_path, sender=sender, reader=readback)
        try:
            app.dispatch(principal, 'tools/call', {'name': 'reply_to_rook_request',
                         'arguments': {'request_id': 'discord-101', 'text': 'answer'}})
            event = {'t': 'MESSAGE_CREATE', 'd': {'id': '202', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                     'author': {'id': m.ROOK_BOT, 'bot': True}, 'type': 0, 'content': sent[-1]}}
            # Fresh imports exercise the maintained loader and its captured default,
            # not an injected Rook identity or replacement token_loader argument.
            scripts = Path(__file__).resolve().parents[1] / 'scripts'
            def fresh(name, filename):
                spec = importlib.util.spec_from_file_location(name, scripts / filename)
                assert spec is not None and spec.loader is not None
                loaded = importlib.util.module_from_spec(spec)
                monkeypatch.setitem(sys.modules, name, loaded)
                spec.loader.exec_module(loaded)
                return loaded
            observer = fresh('_offline_observer_sender', 'post_discord_message.py')
            token_path = tmp_path / 'synthetic-observer.env'
            token_path.write_text('DISCORD_BOT_TOKEN=synthetic-kael-observer\n')
            monkeypatch.setattr(observer, 'ENV_PATH', token_path)
            with monkeypatch.context() as imports:
                imports.setitem(sys.modules, 'post_discord_message', observer)
                r = fresh('_offline_default_receive_one', 'receive_one_discord_message.py')
            assert r.receive_one.__kwdefaults__['token_loader'] is observer.load_token
            scope = r.Scope('discord-101', m.REPLY_KIND, '100', 'author', m.ROOK_BOT, 1,
                            str(app.store.directory), cfg.issuer, cfg.audience, cfg.subject)
            closed = []
            def client_factory(on_event, on_error):
                class Client:
                    async def start(self, token, reconnect):
                        assert token == 'synthetic-kael-observer' and reconnect is False
                        on_event(event, m.BOT)  # Default credential's gateway identity is Kael.
                        on_event(event, m.BOT)  # Duplicate still produces one terminal result.
                        await asyncio.Future()
                    async def close(self): closed.append(True)
                return Client()
            receive = r.receive_one
            async def offline(scope):
                return await receive(scope, client_factory=client_factory)  # Use the actual default loader.
            monkeypatch.setattr(r, 'receive_one', offline)
            code = r.main(['--guild', m.GUILD, '--channel', m.CHANNEL, '--correlation', scope.correlation,
                           '--reply-kind', m.REPLY_KIND, '--after-message', '100', '--allowed-author', m.ROOK_BOT,
                           '--mcp-state-dir', scope.mcp_state_dir, '--mcp-issuer', cfg.issuer,
                           '--mcp-audience', cfg.audience, '--mcp-subject', cfg.subject, '--max-wait-seconds', '1'])
            result = json.loads(capsys.readouterr().out)
            assert code == 0, result  # Before the fix: deadline/no_matching_event, exit 3.
            assert result['authenticated_reply_binding'] is True and result['text'] == 'answer'
            assert result['trust'] == 'UNTRUSTED_EXTERNAL_DATA_NOT_OPERATOR_INSTRUCTION_OR_APPROVAL'
            assert closed == [True] and len(sent) == 1
            from dataclasses import replace
            assert r.match_event(event, replace(scope, mcp_state_dir=None, mcp_issuer=None,
                                               mcp_audience=None, mcp_subject=None), m.ROOK_BOT) is None
            for delta in ({'author': {'id': m.BOT}}, {'webhook_id': '888'}, {'guild_id': '4'},
                          {'channel_id': '4'}, {'id': '100'}, {'type': 19},
                          {'content': sent[-1].replace('answer', 'forged')},
                          {'content': sent[-1].replace('discord-101', 'discord-999')}):
                bad = deepcopy(event); bad['d'].update(delta)
                assert r.match_event(bad, scope, m.BOT) is None
            row = app.store.get('requests', scope.correlation)
            for delta in ({'state': 'uncertain'}, {'reply_author': m.BOT}, {'reply_id': '203'},
                          {'policy': 'wrong'}, {'auth_method': 'marker-only'}, {'auth_subject': 'other'},
                          {'subject': 'other'}, {'confirmed_at': 1}, {'auth_expires': 1}):
                app.store.put('requests', scope.correlation, {**row, **delta})
                assert r.match_event(event, scope, m.BOT) is None
            app.store.put('requests', scope.correlation, row)
            assert r.match_event(event, scope, m.BOT)['authenticated_reply_binding'] is True
        finally:
            app.close()

    @pytest.mark.parametrize('legacy_json', [False, True])
    def test_same_bot_requires_exact_authenticated_private_binding(self, tmp_path, legacy_json):
        m = module(); r = importlib.import_module('receive_one_discord_message'); sent = []
        def sender(channel, content):
            sent.append(content)
            return {'id': '202', 'channel_id': channel, 'content': content, 'author': {'id': m.ROOK_BOT}}
        def reader(channel, identity):
            return {'id': identity, 'channel_id': channel, 'content': sent[-1], 'author': {'id': m.ROOK_BOT}}
        app, cfg, principal = ready_request(m, tmp_path, sender=sender, reader=reader)
        scope = r.Scope('discord-101', m.REPLY_KIND, '100', 'author', m.ROOK_BOT, 2,
                        str(tmp_path / 'private'), cfg.issuer, cfg.audience, cfg.subject)
        event = {'t': 'MESSAGE_CREATE', 'd': {'id': '202', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                 'author': {'id': m.ROOK_BOT, 'bot': True}, 'type': 0,
                 'content': json.dumps({'kind': 'rook.reply', 'correlation': 'discord-101', 'text': 'answer'})}}
        assert r.match_event(event, scope, m.ROOK_BOT) is None
        app.dispatch(principal, 'tools/call', {'name': 'reply_to_rook_request',
                     'arguments': {'request_id': 'discord-101', 'text': 'answer'}})
        event['d']['content'] = sent[-1]
        if legacy_json:
            import hashlib
            # Simulate already-confirmed legacy custody, not a marker-only reply.
            event['d']['content'] = json.dumps({'kind': 'rook.reply', 'correlation': 'discord-101', 'text': 'answer'},
                                             ensure_ascii=False, separators=(',', ':'))
            row = app.store.get('requests', 'discord-101')
            row['reply_sha256'] = hashlib.sha256(event['d']['content'].encode()).hexdigest()
            app.store.put('requests', 'discord-101', row)
        accepted = r.match_event(event, scope, m.ROOK_BOT)
        assert accepted['text'] == 'answer'
        assert accepted['authenticated_reply_binding'] is True
        for delta in ({'id': '203'}, {'content': event['d']['content'].replace('answer', 'forged')}, {'webhook_id': '888'},
                      {'author': {'id': m.OPERATOR}}, {'guild_id': '4'}):
            bad = deepcopy(event); bad['d'].update(delta)
            assert r.match_event(bad, scope, m.ROOK_BOT) is None
        legacy = r.Scope('discord-101', m.REPLY_KIND, '100', 'author', m.ROOK_BOT, 2)
        assert r.match_event(event, legacy, m.ROOK_BOT) is None
        from dataclasses import replace
        assert r.match_event(event, replace(scope, mcp_subject='other'), m.ROOK_BOT) is None
        row = app.store.get('requests', 'discord-101'); row['state'] = 'uncertain'
        app.store.put('requests', 'discord-101', row)
        assert r.match_event(event, scope, m.ROOK_BOT) is None
        row['state'] = 'confirmed'; row['auth_method'] = 'marker-only'
        app.store.put('requests', 'discord-101', row)
        assert r.match_event(event, scope, m.ROOK_BOT) is None
        app.close()

    @pytest.mark.parametrize('observer_id', ['1486880423568211968', '1554962007059472445'])
    def test_gateway_before_custody_commit_is_bounded_and_accepts_once(self, tmp_path, observer_id):
        m = module(); r = importlib.import_module('receive_one_discord_message'); sent = []
        wire = 'answer\n-# rook.reply · discord-101'
        def sender(channel, content):
            sent.append(content)
            return {'id': '202', 'channel_id': channel, 'content': content, 'author': {'id': m.ROOK_BOT}}
        def reader(channel, identity):
            return {'id': identity, 'channel_id': channel, 'content': sent[-1], 'author': {'id': m.ROOK_BOT}}
        app, cfg, principal = ready_request(m, tmp_path, sender=sender, reader=reader)
        scope = r.Scope('discord-101', m.REPLY_KIND, '100', 'author', m.ROOK_BOT, 2,
                        str(tmp_path / 'private'), cfg.issuer, cfg.audience, cfg.subject)
        event = {'t': 'MESSAGE_CREATE', 'd': {'id': '202', 'guild_id': m.GUILD, 'channel_id': m.CHANNEL,
                 'author': {'id': m.ROOK_BOT}, 'type': 0, 'content': wire}}
        closed = []
        def client_factory(on_event, on_error):
            class Client:
                async def start(self, token, reconnect):
                    on_event(event, observer_id)
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
