"""The child pin refusal must retain safe source provenance, never partial pins."""
import json
from pathlib import Path
import subprocess
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import host_disk_auto_classes as auto
import host_disk_authorized_retirement as retirement

SOURCES=('pr_pins','worktree_pins','live_release_pins','r8_pins','linear_pins')

@pytest.mark.parametrize('failed',SOURCES)
def test_child_refusal_names_source_without_partial_pins_or_secrets(monkeypatch,failed):
    calls=[]
    for source in SOURCES:
        def collect(source=source):
            calls.append(source)
            if source==failed:
                raise RuntimeError('secret-value-must-never-be-serialized')
            return {Path('/home/ubuntu/business/reports/'+source)}
        monkeypatch.setattr(auto,source,collect)
    result=auto.pin_inventory()
    assert result=={'schema':'host-disk-pins-v1','ok':False,'source':failed,'error_type':'RuntimeError'}
    assert 'secret-value' not in json.dumps(result)
    assert calls==list(SOURCES[:SOURCES.index(failed)+1])


def test_success_is_fresh_union_not_cached(monkeypatch):
    for source in SOURCES:
        monkeypatch.setattr(auto,source,lambda source=source:{Path('/home/ubuntu/business/reports/'+source)})
    first=auto.pin_inventory()
    assert first['ok'] is True and len(first['pins'])==5
    monkeypatch.setattr(auto,'r8_pins',lambda:{Path('/home/ubuntu/new-hold')})
    assert '/home/ubuntu/new-hold' in auto.pin_inventory()['pins']


def test_nonzero_child_error_is_refusal_with_source_not_lost_stderr(monkeypatch):
    reply={'schema':'host-disk-pins-v1','ok':False,'source':'linear_pins','error_type':'RuntimeError'}
    monkeypatch.setattr(retirement.subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess(a,1,json.dumps(reply),'secret-stderr'))
    with pytest.raises(RuntimeError,match=r'pin inventory refused: linear_pins \(RuntimeError; rc=1\)'):
        retirement.read_registry_pins()


@pytest.mark.parametrize('rc,reply',[(1,{'schema':'host-disk-pins-v1','ok':True,'pins':[]}),
 (0,{'schema':'host-disk-pins-v1','ok':False,'source':'linear_pins','error_type':'RuntimeError'}),
 (0,{'schema':'host-disk-pins-v1','ok':True,'pins':['relative/path']}),
 (0,{'schema':'host-disk-pins-v1','ok':True,'pins':'not a list'}),
 (0,{'ok':True,'pins':[]}), (1,{'schema':'host-disk-pins-v1','ok':False,'source':'secret','error_type':'secret'}),
 (1,'invalid-json'),
 (0,{'schema':'host-disk-pins-v1','ok':True,'pins':[],
     'source':'linear_pins','error_type':'TimeoutError'}),
 (0,'{"schema":"host-disk-pins-v1","ok":false,"source":"linear_pins","error_type":"TimeoutError","ok":true,"pins":[]}'),
 (0,'{"schema":"host-disk-pins-v1","ok":false,"ok":true,"pins":[]}'),
 (0,{'schema':'host-disk-pins-v1','ok':True,'pins':[],'unknown':True})])
def test_bad_or_conflicting_protocol_never_authorizes(monkeypatch,rc,reply):
    text=reply if isinstance(reply,str) else json.dumps(reply)
    monkeypatch.setattr(retirement.subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess(a,rc,text,'secret-stderr'))
    with pytest.raises(RuntimeError,match='pin inventory') as caught:
        retirement.read_registry_pins()
    assert 'secret-stderr' not in str(caught.value)


def test_success_parent_validates_all_absolute_pins(monkeypatch):
    reply={'schema':'host-disk-pins-v1','ok':True,'pins':['/home/ubuntu/one','/home/ubuntu/two']}
    monkeypatch.setattr(retirement.subprocess,'run',lambda *a,**kw:subprocess.CompletedProcess(a,0,json.dumps(reply),''))
    assert retirement.read_registry_pins()=={Path('/home/ubuntu/one'),Path('/home/ubuntu/two')}
