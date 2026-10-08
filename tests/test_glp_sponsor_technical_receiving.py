"""Synthetic coordinator custody only; no live receipt or transport."""
import hashlib
import json
import sys
import zipfile
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import glp_sponsor_campaign as c


def fixture(tmp_path, monkeypatch):
    root=tmp_path/'installed'; root.mkdir()
    state=tmp_path/'state'
    monkeypatch.setattr(c,'ROOT',root)
    monkeypatch.setattr(c,'STATE',state)
    original={'ops/sponsor-machine/campaign.json':b'{"send_enabled":false,"packet_sha256":null}',
              'ops/sponsor-machine/copy.json':b'unchanged synthetic copy',
              'scripts/glp_sponsor_campaign.py':b'# old sender',
              'scripts/firecrawl_client.py':b'# old shared client',
              'scripts/glp_sponsor_paypal.py':b'# old payment receiver',
              'scripts/glp_sponsor_readiness.py':b'# unchanged readiness',
              'inbox_control_plane/control_plane.py':b'# unchanged inbox',
              'inbox_control_plane/sponsor_replies.py':b'# unchanged reply',
              'assets/rate-card.pdf':b'unchanged synthetic pixels',
              'CAPTURE.json':b'unchanged synthetic capture'}
    def sealed(members):
        return {**members,'ops/sponsor-machine/assets.json':json.dumps({n:hashlib.sha256(b).hexdigest() for n,b in members.items()},sort_keys=True).encode()}
    original=sealed(original)
    predecessor=root/'reports/ops/sponsor-autonomous-20261007/review-packet/sponsor-review-packet.zip'
    def write_zip(path,members):
        path.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(path,'w') as z:
            for n,b in members.items():z.writestr(n,b)
    write_zip(predecessor,original)
    oldhash=c.digest(predecessor)
    monkeypatch.setattr(c,'TECHNICAL_PREDECESSOR_SHA256',oldhash,raising=False)
    updates={'scripts/firecrawl_client.py':b'# reviewed shared client',
             'scripts/glp328_readonly_evidence.py':b'# reviewed reader',
             'scripts/glp_sponsor_paypal.py':b'# reviewed payment receiver',
             'ops/linear/approvals/OPERATOR-DECISION-20261008-paypal-ownership-attestation.md':b'synthetic deployment authority'}
    pins={n:hashlib.sha256(b).hexdigest() for n,b in updates.items()}
    monkeypatch.setattr(c,'TECHNICAL_SOURCE_SHA256',pins,raising=False)
    admission={n:b'# synthetic fixed admission closure\n' for n in c.TECHNICAL_ADMISSION_SOURCE_SHA256}
    monkeypatch.setattr(c,'TECHNICAL_ADMISSION_SOURCE_SHA256',{n:hashlib.sha256(b).hexdigest() for n,b in admission.items()})
    new={**original,**updates,'scripts/glp_sponsor_campaign.py':b'# reviewed receiving guard'}
    new.pop('ops/sponsor-machine/assets.json')
    new=sealed(new)
    packet=root/'reports/ops/sponsor-machine-20261003/sponsor-review-packet.zip'
    def install():
        write_zip(packet,new)
        for n,b in new.items():
            p=root/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
    install()
    c.save(state/'approval.json',{'actor':'Izaiah Tilton','packet_sha256':oldhash,'decision':'APPROVE SPONSOR PACKET '+oldhash,'discord_message_id':'1557532710128324609'})
    receipt={'schema':'sponsor.coordinator_technical_receiving.v1','actor':'coordinator','coordinator':'Kael',
             'parent_session_id':'20260922_191215_fffbbb','packet_sha256':c.digest(packet),
             'predecessor_sha256':oldhash,'decision':'RECEIVE SPONSOR TECHNICAL SUCCESSOR '+c.digest(packet),
             'reviewed_source_head':'1'*40,'reviewed_sender_sha256':hashlib.sha256(new['scripts/glp_sponsor_campaign.py']).hexdigest(),
             'reviewed_technical_sources':pins}
    c.save(state/'technical-receiving.json',receipt)
    config={'send_enabled':True,'packet_sha256':c.digest(packet),'postal_address':'Synthetic only','require_full_source_binding':True}
    for n,b in admission.items():
        p=c.ROOT/n;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
    return config,receipt,new,install,predecessor


def test_coordinator_receives_new_technical_bytes_without_relabelling_personal_approval(tmp_path,monkeypatch):
    config,receipt,new,install,predecessor=fixture(tmp_path,monkeypatch)
    old_approval=(c.STATE/'approval.json').read_bytes();old_zip=predecessor.read_bytes()
    assert c.approval_ready(config) is True
    assert (c.STATE/'approval.json').read_bytes()==old_approval
    assert predecessor.read_bytes()==old_zip


@pytest.mark.parametrize('path',list(c.TECHNICAL_ADMISSION_SOURCE_SHA256))
def test_changed_discovery_or_classifier_dependency_refuses_receiving(tmp_path,monkeypatch,path):
    config,*_=fixture(tmp_path,monkeypatch)
    (c.ROOT/path).write_bytes(b'# tampered admission behavior')
    with pytest.raises(RuntimeError,match='technical_admission_source_changed'):
        c.approval_ready(config)


@pytest.mark.parametrize('case',['missing','impersonation','wrong_parent','wrong_coordinator','discord_transplant','source_head','sender_pin','source_pin','decision','predecessor','copy','config','asset','capture','extra_member','changed_source','installed_tamper','loose_rehash','old_receipt','forged_successor_personal_message','predecessor_tamper'])
def test_refuses_forged_or_expanded_technical_receiving(tmp_path,monkeypatch,case):
    config,r,new,install,predecessor=fixture(tmp_path,monkeypatch)
    if case=='missing':(c.STATE/'technical-receiving.json').unlink()
    elif case=='impersonation':r['actor']='Izaiah Tilton'
    elif case=='wrong_parent':r['parent_session_id']='child-author'
    elif case=='wrong_coordinator':r['coordinator']='source-author'
    elif case=='discord_transplant':r['discord_message_id']='1557532710128324609'
    elif case=='source_head':r['reviewed_source_head']='not-a-commit'
    elif case=='sender_pin':r['reviewed_sender_sha256']='0'*64
    elif case=='source_pin':r['reviewed_technical_sources']['scripts/firecrawl_client.py']='0'*64
    elif case=='decision':r['decision']='APPROVE SPONSOR PACKET '+config['packet_sha256']
    elif case=='predecessor':r['predecessor_sha256']='0'*64
    elif case in ('copy','config','asset','capture','extra_member','changed_source'):
        name={'copy':'ops/sponsor-machine/copy.json','config':'ops/sponsor-machine/campaign.json','asset':'assets/rate-card.pdf','capture':'CAPTURE.json','extra_member':'scripts/arbitrary.py','changed_source':'scripts/firecrawl_client.py'}[case]
        new[name]=b'FORGED EXPANSION'
        new['ops/sponsor-machine/assets.json']=json.dumps({n:hashlib.sha256(b).hexdigest() for n,b in new.items() if n!='ops/sponsor-machine/assets.json'}).encode()
        install();config['packet_sha256']=c.digest(c.ROOT/'reports/ops/sponsor-machine-20261003/sponsor-review-packet.zip')
        r.update(packet_sha256=config['packet_sha256'],decision='RECEIVE SPONSOR TECHNICAL SUCCESSOR '+config['packet_sha256'])
    elif case in ('installed_tamper','loose_rehash'):
        (c.ROOT/'scripts/firecrawl_client.py').write_bytes(b'forged installed source')
        if case=='loose_rehash':
            manifest=c.load(c.ROOT/'ops/sponsor-machine/assets.json');manifest['scripts/firecrawl_client.py']=c.digest(c.ROOT/'scripts/firecrawl_client.py');c.save(c.ROOT/'ops/sponsor-machine/assets.json',manifest)
    elif case=='old_receipt':
        r['packet_sha256']=r['predecessor_sha256']
    elif case=='forged_successor_personal_message':
        approval=c.load(c.STATE/'approval.json');approval.update(packet_sha256=config['packet_sha256'],decision='APPROVE SPONSOR PACKET '+config['packet_sha256']);c.save(c.STATE/'approval.json',approval)
        # The old personal-message ID cannot approve any successor, even through
        # the legacy exact-hash path.
    elif case=='predecessor_tamper':predecessor.write_bytes(b'changed original recovery archive')
    if case!='missing':c.save(c.STATE/'technical-receiving.json',r)
    with pytest.raises((RuntimeError,FileNotFoundError)):c.approval_ready(config)
