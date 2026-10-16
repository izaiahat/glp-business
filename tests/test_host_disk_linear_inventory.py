"""Bounded fresh Linear inventory retains every active-description hold."""
from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import host_disk_auto_classes as auto

class Reader:
    def __init__(self, rows):self.rows=rows;self.calls=[]
    def pages(self, query, variables, path):
        assert 'description' in query and 'pageInfo' in query
        assert variables=={'filter':{'team':{'key':{'eq':'GLP'}}}}
        assert path==('issues',)
        self.calls.append((query,variables,path));return self.rows

def row(identifier, description=None, state='started', team='GLP'):
    return {'identifier':identifier,'description':description,'state':{'type':state},'team':{'key':team}}

def test_one_complete_live_inventory_not_one_get_per_issue():
    reader=Reader([row('GLP-1','hold /home/ubuntu/business/reports/old-a'),
                   row('GLP-2','hold /home/ubuntu/business/reports/old-b','backlog'),
                   row('GLP-3','/home/ubuntu/business/reports/retired','completed'),
                   row('GLP-4',None),row('GLP-5','/home/ubuntu/business/reports/canceled','canceled')])
    assert auto.linear_pins(reader=reader)=={Path('/home/ubuntu/business/reports/old-a'),Path('/home/ubuntu/business/reports/old-b')}
    assert len(reader.calls)==1
    reader.rows=[row('GLP-6','/home/ubuntu/business/reports/new-live')]
    assert auto.linear_pins(reader=reader)=={Path('/home/ubuntu/business/reports/new-live')}
    assert len(reader.calls)==2  # Every custody refresh still reads; never cache across effects.

@pytest.mark.parametrize('bad',[
    {'identifier':'GLP-1','state':{'type':'started'},'team':{'key':'GLP'}},
    row('GLP-1',description=7),row('GLP-1',team='OTHER'),
    {'identifier':'GLP-1','description':'/home/ubuntu/business/reports/pin','state':None,'team':{'key':'GLP'}},
    {'identifier':'GLP-1','description':None,'state':{'type':None},'team':{'key':'GLP'}},
    'not a node',
])
def test_incomplete_or_wrong_team_inventory_cannot_authorize_effects(bad):
    with pytest.raises(RuntimeError,match='Linear inventory'):
        auto.linear_pins(reader=Reader([row('GLP-2','/home/ubuntu/business/reports/good'),bad]))

def test_rate_limit_never_returns_partial_pins():
    class Refused:
        def pages(self,*args):raise RuntimeError('rate limit wait exceeds bound')
    with pytest.raises(RuntimeError,match='Linear inventory unavailable'):
        auto.linear_pins(reader=Refused())

def test_default_transport_reuses_registered_pagination_without_issue_gets(tmp_path):
    # The reduced mirror does not carry the shared Linear client. Exercise the
    # exact subprocess protocol with a credential-free registered-client stand-in.
    client=tmp_path/'linear_client.py'
    client.write_text('''import json
class Client:
    def pages(self, query, variables, path):
        assert 'description' in query and 'pageInfo' in query
        assert path == ('issues',)
        assert variables == {'filter': {'team': {'key': {'eq': 'GLP'}}}}
        return [{'identifier':'GLP-1','description':'/home/ubuntu/business/reports/first',
                 'state':{'type':'started'},'team':{'key':'GLP'}}]
''')
    assert auto.linear_pins(client=client)=={Path('/home/ubuntu/business/reports/first')}
    client.write_text('''class Client:
    def pages(self, *args):
        raise RuntimeError('Missing or repeated pagination cursor')
''')
    with pytest.raises(RuntimeError,match='Linear inventory unavailable'):
        auto.linear_pins(client=client)
