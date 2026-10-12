"""Offline expiry/reminder proof; never substitute for live key renewal."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import codex_cloud_key_renewal as renewal


class Client:
    def __init__(self): self.messages = []; self.fail = False
    def send(self, channel, content):
        self.messages.append({'id': str(len(self.messages)+1), 'channel_id': channel,
                              'content': content, 'author': {'bot': True}})
        if self.fail: raise TimeoutError('ambiguous after POST')
        return self.messages[-1]
    def readback(self, channel, ident): return next(x for x in self.messages if x['id']==ident)
    def recent(self, channel): return [x for x in self.messages if x['channel_id']==channel]


def env(now, hours=72):
    return {'name': 'hermes-evidence-account1', 'account': 'izaiahtilton1@gmail.com',
            'environment_id': 'test-env', 'key_id': 'old-key-id',
            'expires': (now+timedelta(hours=hours)).isoformat()}


def test_renewal_monitor_deadline_and_exact_readback(tmp_path):
    now = datetime(2026,10,11,tzinfo=timezone.utc)
    client = Client(); state = tmp_path/'state.json'
    assert renewal.run({'environments':[env(now,74)]},state,now,client)['status']=='NOT_DUE'
    assert not client.messages
    config = {'environments':[env(now,73)]}
    result = renewal.run(config,state,now,client)
    assert result['status']=='MANUAL_RENEWAL_REQUIRED'
    assert len(client.messages)==2
    assert client.messages[0]['channel_id']==renewal.DECISIONS
    assert client.messages[1]['channel_id']==renewal.OPS_ALERTS
    assert result['environments'][0]['notification']['status']=='VERIFIED'
    assert not result['secret_generated'] and not result['cloud_secret_updated']
    renewal.run(config,state,now,client)
    assert len(client.messages)==2
    assert 'No new key has been generated' in client.messages[0]['content']
    assert 'tag:codex-cloud' in client.messages[0]['content']
    assert 'Only after that proof' in client.messages[0]['content']


def test_renewal_monitor_uncertain_post_reconciles_without_repost(tmp_path):
    client = Client(); client.fail = True
    state = {}; path = tmp_path/'state.json'
    with pytest.raises(TimeoutError):
        renewal.notify(state,path,client,renewal.DECISIONS,'same-key','same-content')
    client.fail = False
    stored = json.loads(path.read_text())
    assert renewal.notify(stored,path,client,renewal.DECISIONS,'same-key','same-content')['status']=='VERIFIED'
    assert len(client.messages)==1
    # Missing readback is an unresolved effect, never permission to resend.
    path2 = tmp_path/'lost.json'; state2 = {'notifications':{'x':{'status':'INTENT'}}}
    with pytest.raises(RuntimeError, match='uncertain_manual_reconcile'):
        renewal.notify(state2,path2,client,renewal.DECISIONS,'x','different')
    assert len(client.messages)==1


def test_renewal_registry_preserves_existing_scope():
    root = Path(__file__).resolve().parents[1]
    config = json.loads((root/'ops/codex-cloud/tailscale-renewal.json').read_text())
    assert len(config['environments'])==2
    for row in config['environments']:
        assert row['tags']==['tag:codex-cloud']
        assert len((renewal.operator_steps(row)+'\n-# renewal-ref: '+'a'*20).encode('utf-16-le'))//2 <=1900
    assert config['mode'] in ('manual_fallback','automatic')
