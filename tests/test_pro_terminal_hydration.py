"""Exact terminal reply hydration never changes the stored acceptance hash."""
import hashlib
from pathlib import Path
import sys
import pytest
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import gpt6pro_relay as relay


def setup(monkeypatch, texts, stop=False):
    st={'job':'j','status':'done','tab':'t','conversation_id':'00000000-0000-0000-0000-000000000001',
        'last_text_sha':hashlib.sha256(b'complete receipt').hexdigest(),'reply_render_mode':'selection_markdown'}
    calls=[]
    def read(*args,**kwargs):
        calls.append(kwargs)
        text=texts.pop(0) if len(texts)>1 else texts[0]
        return {'state':{'stop':stop},'text':text,'tab':'t'}
    monkeypatch.setattr(relay,'read_reply',read)
    monkeypatch.setattr(relay,'require_binding',lambda *a:None)
    monkeypatch.setattr(relay,'protected_tabs',lambda *a:[])
    monkeypatch.setattr(relay.time,'sleep',lambda n:None)
    return st,calls


def test_waits_for_original_exact_renderer_bytes_without_reload(monkeypatch):
    st,calls=setup(monkeypatch,['','partial','complete receipt'])
    assert relay.read_terminal_reply(st)['text']=='complete receipt'
    assert len(calls)==3
    assert all(c['capture_mode']=='selection_markdown' and not c.get('reload') for c in calls)
    assert st['last_text_sha']==hashlib.sha256(b'complete receipt').hexdigest()


def test_changed_reply_is_not_rehashed_accepted_or_reloaded(monkeypatch):
    st,calls=setup(monkeypatch,['different final'])
    reply=relay.read_terminal_reply(st)
    assert reply['text']=='different final'
    assert len(calls)==11
    assert st['last_text_sha']!=hashlib.sha256(reply['text'].encode()).hexdigest()
    assert all(not c.get('reload') for c in calls)


def test_generation_never_retried_or_reloaded(monkeypatch):
    st,calls=setup(monkeypatch,['streaming'],stop=True)
    assert relay.read_terminal_reply(st)['state']['stop'] is True
    assert len(calls)==1


def test_nonterminal_rejected_before_any_browser_read(monkeypatch):
    st,calls=setup(monkeypatch,['complete receipt'])
    st['status']='running'
    with pytest.raises(ValueError,match='terminal'):
        relay.read_terminal_reply(st)
    assert not calls
