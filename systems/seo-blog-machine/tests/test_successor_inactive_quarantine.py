import json
import pytest
from test_article_successor import successor_fixture
from test_article_buffer import sha, write_json
import article_buffer as b

def setup(tmp_path,mode='safe'):
 root,packet,receipt,control,old,oldid=successor_fixture(tmp_path)
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


def test_requested_predecessor_quarantine_is_never_filtered(tmp_path):
 root,oldid,dups,ev,q,other=setup(tmp_path)
 original=dups[0][0];material=tmp_path/'original-materialization.json';write_json(material,{'fixture_only':True})
 payload={'schema':b.QUARANTINE_SCHEMA,'status':'QUARANTINED_PERMANENT_ARTICLE_GATE_REJECTION','entry_id':oldid,'reason':'inbound_link_source_missing:/guides/fixture/','publication_effects':0,'entry_manifest':{'path':str(original),'sha256':sha(original)},'materialization':{'path':str(material),'sha256':sha(material)}}
 b._write_once(root/'quarantined'/f'{oldid}.json',payload,'quarantine_sha256')
 with pytest.raises(b.BufferAdmissionError,match='buffer_successor_predecessor_quarantined_recorded'):
  b._held_successor_predecessor(root,oldid,dups,ev,'0'*64)
