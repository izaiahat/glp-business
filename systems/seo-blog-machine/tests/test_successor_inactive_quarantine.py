import json
import pytest
from pathlib import Path
from test_article_successor import successor_fixture, isolated_carrier_lock
from test_article_buffer import sha, write_json
import article_buffer as b

def setup(tmp_path,mode='safe', *, registered=False):
 root,packet,receipt,control,old,oldid=successor_fixture(tmp_path, registered=registered)
 otherid='f'*24;other=root/'entries'/otherid/'entry.json';other.parent.mkdir(parents=True)
 entry=json.loads(old.read_text());entry['entry_id']=otherid;entry.pop('entry_sha256',None);b._write_once(other,entry,'entry_sha256');entry=b._read_sealed(other,b.ENTRY_SCHEMA,'entry_sha256')
 material=tmp_path/'materialization.json';write_json(material,{'fixture_only':True})
 payload={'schema':b.QUARANTINE_SCHEMA,'status':'QUARANTINED_PERMANENT_ARTICLE_GATE_REJECTION','entry_id':otherid,'reason':'inbound_link_source_missing:/guides/fixture/','publication_effects':0,'entry_manifest':{'path':str(other),'sha256':sha(other)},'materialization':{'path':str(material),'sha256':sha(material)}}
 if mode=='unknown':payload.pop('publication_effects')
 if mode=='effects':payload['publication_effects']=1
 if mode=='bool_zero':payload['publication_effects']=False
 if mode=='entry_drift':payload['entry_manifest']['sha256']='0'*64
 if mode=='wrong_reason':payload['reason']='unrelated_refusal'
 if mode in ['archive','archive_bad']:
  archive=tmp_path/'archive';archive.mkdir();(archive/material.name).write_bytes(material.read_bytes());payload['workspace_archive']=str(archive);material.write_text('new live bytes')
  if mode=='archive_bad':(archive/material.name).write_text('wrong archived bytes')
 q=root/'quarantined'/f'{otherid}.json';q.parent.mkdir(exist_ok=True);b._write_once(q,payload,'quarantine_sha256')
 if mode in ['active','consumed','published','publications']:
  d=root/('claims' if mode=='active' else mode);d.mkdir();write_json(d/'record.json',{'entry_id':otherid})
 if mode=='pin_drift':material.write_text('changed')
 duplicates=[(old,json.loads(old.read_text())),(other,entry)];ev={k:json.loads(old.read_text())[k] for k in ['topic_id','target_route','topic_provenance']};ev['selection_receipt']=json.loads(old.read_text())['selection_receipt'];ev['topic_authority']=json.loads(old.read_text())['topic_authority']
 return root,oldid,duplicates,ev,q,other

@pytest.mark.parametrize('mode',['safe','archive'])
def test_authenticated_zero_effect_inbound_quarantine_does_not_block_held_successor(tmp_path,mode):
 root,oldid,dups,ev,q,other=setup(tmp_path,mode);before=(q.read_bytes(),other.read_bytes());result=b._held_successor_predecessor(root,oldid,dups,ev,'0'*64);assert result['entry_id']==oldid;assert before==(q.read_bytes(),other.read_bytes())

@pytest.mark.parametrize('mode',['unknown','effects','bool_zero','entry_drift','wrong_reason','active','consumed','published','publications','pin_drift','archive_bad'])
def test_unknown_effect_or_active_history_remains_duplicate_block(tmp_path,mode):
 root,oldid,dups,ev,q,other=setup(tmp_path,mode)
 with pytest.raises(b.BufferAdmissionError,match='buffer_successor_predecessor_not_exact'):
  b._held_successor_predecessor(root,oldid,dups,ev,'0'*64)


@pytest.mark.parametrize('mode', ['safe', 'archive', 'unknown', 'effects', 'bool_zero',
                                 'entry_drift', 'wrong_reason', 'active', 'consumed',
                                 'published', 'publications', 'pin_drift', 'archive_bad'])
def test_real_successor_admission_excludes_history_without_readmitting_it(tmp_path, monkeypatch, mode):
    root, oldid, duplicates, _, quarantine, other = setup(tmp_path, mode, registered=True)
    packet = tmp_path / 'registered/runs/fixture-review/packet'
    receipt = packet.parent / 'opus-history-fixture-review/round-01-receipt.json'
    monkeypatch.setattr(b, 'REGISTERED_REVIEW_ROOT', tmp_path / 'registered')
    monkeypatch.setattr(b, 'pt_date_now', lambda: '2026-09-26')
    # Reuse the exact fixture's pinned controls, not live source authority.
    controls = dict(json.loads(duplicates[0][0].read_text())['control_files'])
    for name in ('build_opus_article_release.py', 'publish_opus_article_release.py'):
        path = Path(b.__file__).with_name(name)
        controls[str(path)] = sha(path)
    request = dict(root=root, packet=packet, pass_receipt=receipt, control_files=controls,
                   current_routes=set(), current_topic_ids=set(), mutable_article_routes=set(),
                   admitted_at_utc='2026-09-26T12:00:00Z', predecessor_entry_id=oldid,
                   today_pt='2026-09-26')
    before = {p: p.read_bytes() for p in root.rglob('*.json')}
    if mode not in ('safe', 'archive'):
        with pytest.raises(b.BufferAdmissionError, match='buffer_successor_predecessor_not_exact'):
            b.admit(**request)
        assert all(p.read_bytes() == content for p, content in before.items())
        assert len(list((root / 'entries').glob('*/entry.json'))) == 2
        return
    admitted = b.admit(**request)
    assert admitted != other
    assert json.loads(admitted.read_text())['successor_of']['entry_id'] == oldid
    assert b.admit(**request) == admitted
    ready = b.ready_entries(root, current_routes=set(), current_topic_ids=set())
    assert len(ready) == 1
    assert len(list((root / 'entries').glob('*/entry.json'))) == 3
    assert all(p.read_bytes() == content for p, content in before.items())
    assert quarantine.exists()


def test_requested_predecessor_quarantine_is_never_filtered(tmp_path):
 root,oldid,dups,ev,q,other=setup(tmp_path)
 original=dups[0][0];material=tmp_path/'original-materialization.json';write_json(material,{'fixture_only':True})
 payload={'schema':b.QUARANTINE_SCHEMA,'status':'QUARANTINED_PERMANENT_ARTICLE_GATE_REJECTION','entry_id':oldid,'reason':'inbound_link_source_missing:/guides/fixture/','publication_effects':0,'entry_manifest':{'path':str(original),'sha256':sha(original)},'materialization':{'path':str(material),'sha256':sha(material)}}
 b._write_once(root/'quarantined'/f'{oldid}.json',payload,'quarantine_sha256')
 with pytest.raises(b.BufferAdmissionError,match='buffer_successor_predecessor_quarantined_recorded'):
  b._held_successor_predecessor(root,oldid,dups,ev,'0'*64)
