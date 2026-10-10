"""Bounded retries only on an explicit Discord rejection."""
import io
import importlib.util
from pathlib import Path
import urllib.error
import pytest

ROOT = Path(__file__).resolve().parents[1]
def poster():
 spec=importlib.util.spec_from_file_location('poster_under_test',ROOT/'scripts/post_discord_message.py')
 m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

class Response:
 def __enter__(self): return self
 def __exit__(self,*args): pass
 def read(self): return b'{"id":"123"}'

def test_explicit_rate_rejection_retries_once_bounded(monkeypatch):
 m=poster();monkeypatch.setattr(m,'load_token',lambda:'fixture');calls=[];waits=[]
 def request(*args,**kwargs):
  calls.append(1)
  if len(calls)==1:
   raise urllib.error.HTTPError('https://discord.com',429,'rejected',{},io.BytesIO(b'{"retry_after":0.01}'))
  return Response()
 monkeypatch.setattr(m.urllib.request,'urlopen',request);monkeypatch.setattr(m.time,'sleep',waits.append)
 assert m.send('1','fixture')['id']=='123'
 assert len(calls)==2 and waits==[0.01]

@pytest.mark.parametrize('error',[urllib.error.HTTPError('https://discord.com',500,'ambiguous',{},io.BytesIO(b'{}')),TimeoutError('ambiguous')])
def test_uncertain_delivery_is_never_retried(monkeypatch,error):
 m=poster();monkeypatch.setattr(m,'load_token',lambda:'fixture');calls=[]
 def request(*args,**kwargs):calls.append(1);raise error
 monkeypatch.setattr(m.urllib.request,'urlopen',request)
 with pytest.raises(type(error)):m.send('1','fixture')
 assert len(calls)==1
