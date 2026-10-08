"""Offline technical-only packet successor; never activation authority."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import zipfile
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
MANIFEST = 'ops/sponsor-machine/assets.json'
CLIENT = 'scripts/firecrawl_client.py'
READER = 'scripts/glp328_readonly_evidence.py'


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True).strip()


def fixture(tmp_path):
    source = tmp_path/'source'; source.mkdir()
    git(source, 'init', '-q')
    git(source, 'config', 'user.email', 'fixture@example.invalid')
    git(source, 'config', 'user.name', 'Fixture')
    members = {
        'ops/sponsor-machine/campaign.json': b'{"send_enabled":false,"packet_sha256":null}',
        'ops/sponsor-machine/copy.json': b'{"copy":"unchanged fixture"}',
        'scripts/glp_sponsor_campaign.py': b'# unchanged sender fixture\n',
        'scripts/glp_sponsor_paypal.py': b'# unchanged payment fixture\n',
        'scripts/glp_sponsor_readiness.py': b'# unchanged readiness fixture\n',
        'scripts/glp_sponsor_alexandria.py': b'# unchanged enrichment fixture\n',
        'scripts/bouncer_shared_account_guard.py': b'# unchanged guard fixture\n',
        'inbox_control_plane/control_plane.py': b'# unchanged inbox fixture\n',
        'inbox_control_plane/sponsor_replies.py': b'# unchanged reply fixture\n',
        CLIENT: b'# predecessor client fixture\n',
        'reports/ops/sponsor-machine-20261003/assets/rate-card.pdf': b'fictional pixels fixture',
        'reports/ops/sponsor-machine-20261003/REVIEW.md': b'unchanged cadence and copy',
        'CAPTURE.json': b'{"fixture_only":true}',
    }
    for name, data in members.items():
        if name.endswith('.py') or name.startswith('ops/sponsor-machine/'):
            p = source/name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
    p=source/READER; p.write_bytes(b'# accepted reader fixture\n')
    git(source, 'add', '.'); git(source, 'commit', '-qm', 'source')
    source_head=git(source, 'rev-parse', 'HEAD'); git(source, 'update-ref', 'refs/remotes/origin/main', source_head)
    packet=tmp_path/'approved.zip'
    sealed={n:hashlib.sha256(b).hexdigest() for n,b in members.items() if n!='CAPTURE.json'}
    members[MANIFEST]=json.dumps(sealed, sort_keys=True).encode()
    with zipfile.ZipFile(packet, 'w') as z:
        for n,b in members.items(): z.writestr(n,b)
    dependency=tmp_path/'dependency'
    subprocess.run(['git','clone','-q',str(source),str(dependency)], check=True)
    git(dependency, 'config', 'user.email', 'fixture@example.invalid')
    git(dependency, 'config', 'user.name', 'Fixture')
    git(dependency, 'update-ref', 'refs/remotes/origin/main', source_head)
    (dependency/CLIENT).write_bytes(b'# successor client fixture\n')
    git(dependency, 'commit', '-qam', 'dependency')
    dep_head=git(dependency, 'rev-parse', 'HEAD')
    return source,dependency,dep_head,packet,members


def producer():
    assert importlib.util.find_spec('glp_sponsor_packet_successor') is not None, 'maintained technical successor producer missing'
    import glp_sponsor_packet_successor
    return glp_sponsor_packet_successor


def build(tmp_path):
    source,dep,head,packet,members=fixture(tmp_path)
    old=packet.read_bytes(); out=tmp_path/'successor'
    result=producer().build_successor(packet,hashlib.sha256(old).hexdigest(),source,dep,head,out)
    return source,dep,head,packet,members,out,result


def test_successor_preserves_all_outward_bytes_and_original_packet(tmp_path):
    source,dep,head,packet,members,out,result=build(tmp_path)
    assert result['status']=='UNAPPROVED_TECHNICAL_SUCCESSOR'
    assert result['source_merged'] is True and result['dependency_merged'] is False
    assert result['dependency_head']==head
    assert hashlib.sha256(packet.read_bytes()).hexdigest()==result['predecessor_sha256']
    with zipfile.ZipFile(out/'sponsor-review-packet.zip') as z:
        new={n:z.read(n) for n in z.namelist()}
    assert set(new)==set(members)|{READER}
    for n,b in members.items():
        if n not in (CLIENT,MANIFEST): assert new[n]==b
    assert new[CLIENT]==(dep/CLIENT).read_bytes()
    assert new[READER]==(source/READER).read_bytes()
    manifest=json.loads(new[MANIFEST])
    assert all(hashlib.sha256(new[n]).hexdigest()==h for n,h in manifest.items())
    assert result['approval_required']=='APPROVE SPONSOR PACKET '+result['packet_sha256']
    assert not list(out.rglob('approval.json')) and not list(out.rglob('activation.json'))


@pytest.mark.parametrize('case,reason', [
    ('hash', 'predecessor_packet_hash_mismatch'),
    ('head', 'dependency_head_mismatch'),
    ('output', 'output_already_exists'),
    ('source', 'nontechnical_source_drift'),
    ('unmerged_source', 'sponsor_source_not_merged'),
])
def test_refuses_without_creating_proposal(tmp_path,case,reason):
    source,dep,head,packet,members=fixture(tmp_path)
    expected=hashlib.sha256(packet.read_bytes()).hexdigest()
    out=tmp_path/'successor'
    if case=='hash': expected='0'*64
    if case=='head': head='0'*40
    if case=='output': out.mkdir(); (out/'owned-by-other').write_text('retain')
    if case in ('source','unmerged_source'):
        (source/'ops/sponsor-machine/copy.json').write_text('{"copy":"changed"}')
        git(source,'commit','-qam','semantic change')
        if case=='source': git(source,'update-ref','refs/remotes/origin/main',git(source,'rev-parse','HEAD'))
    with pytest.raises(ValueError,match=reason):
        producer().build_successor(packet,expected,source,dep,head,out)
    assert not (out/'sponsor-review-packet.zip').exists()
    if case=='output': assert (out/'owned-by-other').read_text()=='retain'


def test_successor_reuses_intact_guard_but_never_old_approval(tmp_path,monkeypatch):
    import socket
    def deny(*a,**kw): raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket,'connect',deny)
    source,dep,head,packet,members,out,result=build(tmp_path)
    import glp_sponsor_campaign as campaign
    root=tmp_path/'guard-receiver'; root.mkdir()
    with zipfile.ZipFile(out/'sponsor-review-packet.zip') as z: z.extractall(root)
    canonical=root/'reports/ops/sponsor-machine-20261003/sponsor-review-packet.zip'
    canonical.write_bytes((out/'sponsor-review-packet.zip').read_bytes())
    monkeypatch.setattr(campaign,'ROOT',root)
    monkeypatch.setattr(campaign,'STATE',tmp_path/'fixture-state')
    config=campaign.load(root/'ops/sponsor-machine/campaign.json')
    config.update(send_enabled=True,packet_sha256=result['packet_sha256'],postal_address='Fixture only',require_full_source_binding=True)
    def receipt(h):
        campaign.save(campaign.STATE/'approval.json',{'actor':'Izaiah Tilton','packet_sha256':h,
            'decision':'APPROVE SPONSOR PACKET '+h,'discord_message_id':'fixture-not-a-live-message'})
    receipt(result['predecessor_sha256'])
    with pytest.raises(RuntimeError,match='exact_operator_approval_missing'): campaign.approval_ready(config)
    receipt(result['packet_sha256'])
    assert campaign.approval_ready(config) is True  # explicit synthetic authority, no transport
    (root/CLIENT).write_bytes(b'# tampered fixture')
    with pytest.raises(RuntimeError,match='approved_asset_changed'): campaign.approval_ready(config)
    loose=campaign.load(root/MANIFEST); loose[CLIENT]=hashlib.sha256((root/CLIENT).read_bytes()).hexdigest()
    campaign.save(root/MANIFEST,loose)
    with pytest.raises(RuntimeError,match='approved_manifest_not_in_packet'): campaign.approval_ready(config)


def test_snapshot_uses_committed_source_not_uncommitted_owner_edits(tmp_path):
    source,dep,head,packet,members=fixture(tmp_path)
    expected=hashlib.sha256(packet.read_bytes()).hexdigest()
    (dep/CLIENT).write_text('# uncommitted other-owner edit')
    out=tmp_path/'successor'
    result=producer().build_successor(packet,expected,source,dep,head,out)
    with zipfile.ZipFile(out/'sponsor-review-packet.zip') as z:
        assert z.read(CLIENT)==b'# successor client fixture\n'
    assert (dep/CLIENT).read_text()=='# uncommitted other-owner edit'
    assert result['dependency_head']==head


@pytest.mark.parametrize('case',['accepted','authority_tamper','sender_drift'])
def test_attestation_successor_seals_only_exact_authority_and_reviewed_receiver(tmp_path,monkeypatch,case):
    source,dep,head,packet,members=fixture(tmp_path)
    p=producer()
    import inspect
    assert 'operator_attestation' in inspect.signature(p.build_successor).parameters,'technical producer cannot seal the accepted operator-attestation receiver'
    authority=source/p.ATTESTATION;authority.parent.mkdir(parents=True,exist_ok=True)
    authority.write_bytes(b'SYNTHETIC OPERATOR DEPLOYMENT RECORD - NOT LIVE')
    monkeypatch.setattr(p,'ATTESTATION_SHA256',hashlib.sha256(authority.read_bytes()).hexdigest())
    (source/p.PAYPAL).write_bytes(b'# synthetic operator attestation receiver\n')
    if case=='authority_tamper':authority.write_bytes(b'TAMPERED AUTHORITY')
    if case=='sender_drift':(source/'scripts/glp_sponsor_campaign.py').write_bytes(b'# changed sender\n')
    git(source,'add',str(authority.relative_to(source)),p.PAYPAL,'scripts/glp_sponsor_campaign.py')
    git(source,'commit','-qm','synthetic reviewed technical source')
    git(source,'update-ref','refs/remotes/origin/main',git(source,'rev-parse','HEAD'))
    out=tmp_path/'attestation-successor'
    if case!='accepted':
        with pytest.raises(ValueError,match='authority_mismatch' if case=='authority_tamper' else 'nontechnical_source_drift'):
            p.build_successor(packet,hashlib.sha256(packet.read_bytes()).hexdigest(),source,dep,head,out,operator_attestation=True)
        assert not out.exists()
        return
    result=p.build_successor(packet,hashlib.sha256(packet.read_bytes()).hexdigest(),source,dep,head,out,operator_attestation=True)
    assert result['status']=='UNAPPROVED_TECHNICAL_SUCCESSOR' and result['operator_approved'] is False
    assert result['operator_attestation_receiver'] is True
    with zipfile.ZipFile(out/'sponsor-review-packet.zip') as z:
        new={name:z.read(name) for name in z.namelist()}
    assert new[p.ATTESTATION]==authority.read_bytes()
    assert new[p.PAYPAL]==(source/p.PAYPAL).read_bytes()
    for name,data in members.items():
        if name not in {p.PAYPAL,CLIENT,MANIFEST}:assert new[name]==data
    sealed=json.loads(new[MANIFEST])
    assert all(hashlib.sha256(new[name]).hexdigest()==sha for name,sha in sealed.items())
    assert set(result['changed_members'])=={p.PAYPAL,p.ATTESTATION,CLIENT,READER,MANIFEST}
    assert not list(out.rglob('approval.json')) and not list(out.rglob('activation.json'))
