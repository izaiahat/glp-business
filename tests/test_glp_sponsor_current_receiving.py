"""Current-coordinator receipt regression; synthetic, no live authority."""
import hashlib
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp_sponsor_campaign as c


def test_current_coordinator_receiving_is_not_a_historical_session(tmp_path, monkeypatch):
    import glp_sponsor_current_receiving as r
    root = tmp_path / 'installed'
    monkeypatch.setattr(c, 'ROOT', root)
    monkeypatch.setattr(c, 'STATE', root / 'runtime')
    monkeypatch.setattr(r, 'reviewed_source_head', lambda campaign: '1'*40, raising=False)
    monkeypatch.setattr(r, 'packet_binding', lambda campaign, packet, config: {'source': 'a'*64})
    monkeypatch.setattr(r, 'current_authority', lambda campaign, coordinator=None: {'plan_sha256': 'b'*64, 'decision_sha256': 'd'*64,
                                                                       'coordinator': {'session_id':'current-parent-session'}})
    packet = root / 'reports/ops/sponsor-machine-20261003/sponsor-review-packet.zip'
    packet.parent.mkdir(parents=True)
    packet.write_bytes(b'synthetic packet')
    sha = c.digest(packet)
    config = {'send_enabled': True, 'packet_sha256': sha}
    receipt = {'schema': r.SCHEMA, 'actor': 'coordinator', 'coordinator_authority': r.current_authority(c),
               'receiving_session_id': 'current-parent-session', 'packet_sha256': sha,
               'decision': 'RECEIVE CURRENT SPONSOR PACKET ' + sha,
               'reviewed_source_head': '1'*40, 'installed_bindings': {'source': 'a'*64}}
    c.save(c.STATE / r.RECEIPT, receipt)
    c.save(c.STATE / 'approval.json', receipt)
    assert c.approval_ready(config)
    # Both canonical receipt copies can agree and still be historical/forged.
    stale = dict(receipt, receiving_session_id='historical-parent-session')
    c.save(c.STATE / r.RECEIPT, stale)
    c.save(c.STATE / 'approval.json', stale)
    with pytest.raises(RuntimeError, match='current_coordinator_receiving_invalid'):
        c.approval_ready(config)
    # An arbitrary but syntactically valid commit cannot claim source review.
    unreviewed = dict(receipt, reviewed_source_head='2'*40)
    c.save(c.STATE / r.RECEIPT, unreviewed)
    c.save(c.STATE / 'approval.json', unreviewed)
    with pytest.raises(RuntimeError, match='current_coordinator_receiving_invalid'):
        c.approval_ready(config)
    c.save(c.STATE / r.RECEIPT, receipt)
    c.save(c.STATE / 'approval.json', receipt)
    original = (c.STATE / r.RECEIPT).read_text()
    for field, bad in [('actor', 'source-author'), ('coordinator_authority', {'plan_sha256':'0'*64}),
                       ('packet_sha256','0'*64), ('installed_bindings', {'source':'0'*64}),
                       ('decision','APPROVE SPONSOR PACKET '+sha), ('reviewed_source_head','not-head')]:
        c.save(c.STATE / r.RECEIPT, dict(receipt, **{field:bad}))
        with pytest.raises(RuntimeError, match='current_coordinator_receiving_invalid'):
            c.approval_ready(config)
    (c.STATE / r.RECEIPT).write_text(original.replace('"actor": "coordinator"', '"actor":"source-author", "actor":"coordinator"'))
    with pytest.raises(ValueError, match='duplicate_json_key:actor'):
        c.approval_ready(config)
    # The positive writer must refuse a leaf, even with plausible process metadata.
    monkeypatch.setenv('LINEAR_ROLE', 'worker')
    monkeypatch.setenv('HERMES_SESSION_ID', 'current-parent-session')
    with pytest.raises(RuntimeError, match='current_coordinator_execution_required'):
        r.require_coordinator_execution()


def test_forged_environment_does_not_establish_coordinator(tmp_path, monkeypatch):
    import glp_sponsor_current_receiving as r
    active = tmp_path/'forged-active.json'
    active.write_text(json.dumps({'session_id':'forged-parent-session'}))
    monkeypatch.setenv('LINEAR_ROLE','orchestrator')
    monkeypatch.setenv('HERMES_SESSION_ID','forged-parent-session')
    monkeypatch.setenv('HERMES_TUI_ACTIVE_SESSION_FILE',str(active))
    with pytest.raises(RuntimeError, match='current_coordinator_execution_required'):
        r.require_coordinator_execution()


@pytest.fixture
def coordinator_runtime(tmp_path, monkeypatch):
    import sqlite3
    import glp_sponsor_current_receiving as r
    home, proc = tmp_path/'home', tmp_path/'proc'
    active = home/'cache/scratch/current.json'
    active.parent.mkdir(parents=True)
    active.write_text(json.dumps({'session_id':'current-parent-session'}))
    with sqlite3.connect(home/'state.db') as db:
        db.execute('CREATE TABLE sessions (id TEXT, parent_session_id TEXT, source TEXT)')
        db.executemany('INSERT INTO sessions VALUES (?,?,?)',
                       [('current-parent-session',None,'tui'),('leaf-session','current-parent-session','tui')])
    def put(pid, ppid, argv, env, start='1234'):
        p = proc/str(pid); p.mkdir(parents=True, exist_ok=True)
        p.joinpath('stat').write_text(f'{pid} (fixture) S {ppid} '+ '0 '*17 + start + ' 0')
        p.joinpath('cmdline').write_bytes(('\0'.join(argv)+'\0').encode())
        p.joinpath('environ').write_bytes(('\0'.join(k+'='+v for k,v in env.items())+'\0').encode())
    env = dict(HERMES_HOME=str(home),HERMES_TUI_ACTIVE_SESSION_FILE=str(active))
    put(100,1,[str(home/'hermes-agent/venv/bin/python'),'-m','tui_gateway.entry'],env)
    put(101,100,['/bin/bash','-c','receiver'],dict(env,HERMES_SESSION_ID='current-parent-session'))
    monkeypatch.setattr(r,'HOME',home); monkeypatch.setattr(r,'PROC',proc)
    monkeypatch.setattr(r.os,'getppid',lambda:101)
    for k,v in dict(env,LINEAR_ROLE='orchestrator',HERMES_SESSION_ID='current-parent-session').items():
        monkeypatch.setenv(k,v)
    return r, put, env, active


def test_current_root_coordinator_and_adverse_launch_metadata(coordinator_runtime, monkeypatch):
    r, put, env, active = coordinator_runtime
    assert r.require_coordinator_execution() == 'current-parent-session'
    original = r.live_coordinator()
    # A leaf exports the parent's genuine session and active-file path: refuse.
    put(101,100,['/bin/bash','-c','receiver'],dict(env,HERMES_SESSION_ID='leaf-session'))
    with pytest.raises(RuntimeError, match='current_coordinator_execution_required'):
        r.require_coordinator_execution()
    # A stale receipt cannot survive backend PID reuse or a session switch.
    put(101,100,['/bin/bash','-c','receiver'],dict(env,HERMES_SESSION_ID='current-parent-session'))
    put(100,1,[str(r.HOME/'hermes-agent/venv/bin/python'),'-m','tui_gateway.entry'],env,start='9999')
    assert r.live_coordinator() != original
    active.write_text(json.dumps({'session_id':'leaf-session'}))
    with pytest.raises(RuntimeError, match='current_coordinator_execution_required'):
        r.live_coordinator()
    active.write_text(json.dumps({'session_id':'unknown-historical-session'}))
    with pytest.raises(RuntimeError, match='current_coordinator_execution_required'):
        r.live_coordinator()


def test_multiple_tuis_resolve_receiving_by_kernel_ancestry(coordinator_runtime):
    r, put, env, active = coordinator_runtime
    other = active.with_name('other.json')
    other.write_text(active.read_text())
    put(200,1,[str(r.HOME/'hermes-agent/venv/bin/python'),'-m','tui_gateway.entry'],
        dict(env,HERMES_TUI_ACTIVE_SESSION_FILE=str(other)))
    assert r.require_coordinator_execution() == 'current-parent-session'
    assert r.live_coordinator()['pid'] == 100


def test_scheduled_readback_selects_exact_received_tui(coordinator_runtime, monkeypatch):
    r, put, env, active = coordinator_runtime
    received = r.live_coordinator()
    other = active.with_name('other.json'); other.write_text(active.read_text())
    put(200,1,[str(r.HOME/'hermes-agent/venv/bin/python'),'-m','tui_gateway.entry'],
        dict(env,HERMES_TUI_ACTIVE_SESSION_FILE=str(other)))
    # A scheduled consumer has no TUI ancestry; the existing receipt selects,
    # but never creates, exact live custody. A PID/session restart still refuses.
    monkeypatch.setattr(r.os,'getppid',lambda:1)
    assert r.live_coordinator(received) == received
    put(100,1,[str(r.HOME/'hermes-agent/venv/bin/python'),'-m','tui_gateway.entry'],env,start='9999')
    with pytest.raises(RuntimeError,match='current_coordinator_execution_required'):
        r.live_coordinator(received)


def test_source_only_packet_refresh_preserves_outward_bytes(tmp_path, monkeypatch):
    import zipfile
    import glp_sponsor_current_receiving as r
    monkeypatch.setattr(c, 'ROOT', tmp_path)
    blobs = {name: name.encode() for name in set(r.SOURCE_PATHS)|set(r.ASSETS)|{r.REVIEW,r.SUPPLY,r.ASSET_REVIEW}}
    manifest = {name:hashlib.sha256(blob).hexdigest() for name,blob in blobs.items()}
    blobs[r.MANIFEST] = json.dumps(manifest).encode()
    packet = tmp_path/r.PACKET; packet.parent.mkdir(parents=True)
    with zipfile.ZipFile(packet,'w') as z:
        for name, blob in blobs.items(): z.writestr(name,blob)
    for name in r.SOURCE_PATHS:
        path=tmp_path/name; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(blobs[name])
    (tmp_path/'scripts/glp_sponsor_campaign.py').write_bytes(b'reviewed successor sender')
    readiness='scripts/glp_sponsor_readiness.py'
    reviewed=b'exact reviewed readiness successor'
    monkeypatch.setattr(r,'READINESS_RENEWAL',
        (hashlib.sha256(blobs[readiness]).hexdigest(),hashlib.sha256(reviewed).hexdigest()),raising=False)
    (tmp_path/readiness).write_bytes(reviewed)
    before=packet.read_bytes()
    out=tmp_path/'proposal.zip'
    r.refresh_source_packet(c,c.digest(packet),out)
    assert packet.read_bytes()==before
    with zipfile.ZipFile(out) as z:
        assert set(z.namelist())==set(blobs)
        for name,blob in blobs.items():
            if name not in (r.MANIFEST,'scripts/glp_sponsor_campaign.py',readiness):
                assert z.read(name)==blob
        assert z.read(readiness)==reviewed
    (tmp_path/readiness).write_bytes(b'unreviewed readiness')
    with pytest.raises(RuntimeError,match='source_packet_readiness_drift'):
        r.refresh_source_packet(c,c.digest(packet),tmp_path/'unreviewed.zip')
    assert not (tmp_path/'unreviewed.zip').exists()
    (tmp_path/readiness).write_bytes(reviewed)
    with pytest.raises(RuntimeError,match='source_packet_output_exists'):
        r.refresh_source_packet(c,c.digest(packet),out)
    (tmp_path/r.COPY).write_bytes(b'unauthorized changed copy')
    with pytest.raises(RuntimeError,match='source_packet_nontechnical_drift'):
        r.refresh_source_packet(c,c.digest(packet),tmp_path/'bad.zip')


def test_source_refresh_archives_the_validated_readiness_buffer(tmp_path, monkeypatch):
    import zipfile
    import glp_sponsor_current_receiving as r
    monkeypatch.setattr(c, 'ROOT', tmp_path)
    blobs = {name: name.encode() for name in set(r.SOURCE_PATHS)|set(r.ASSETS)|{r.REVIEW,r.SUPPLY,r.ASSET_REVIEW}}
    blobs[r.MANIFEST] = json.dumps({name:hashlib.sha256(blob).hexdigest()
                                  for name,blob in blobs.items()}).encode()
    packet = tmp_path/r.PACKET; packet.parent.mkdir(parents=True)
    with zipfile.ZipFile(packet, 'w') as archive:
        for name,blob in blobs.items(): archive.writestr(name,blob)
    for name in r.SOURCE_PATHS:
        path = tmp_path/name; path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(blobs[name])
    target = tmp_path/r.READINESS_PATH
    reviewed = b'exact reviewed readiness successor'
    unreviewed = b'unreviewed successor introduced after first read'
    monkeypatch.setattr(r, 'READINESS_RENEWAL',
        (hashlib.sha256(blobs[r.READINESS_PATH]).hexdigest(),hashlib.sha256(reviewed).hexdigest()))
    target.write_bytes(reviewed)
    real_read = Path.read_bytes
    observed = []
    def change_after_read(path):
        data = real_read(path)
        if path == target:
            observed.append(data)
            if len(observed) == 1:
                target.write_bytes(unreviewed)
        return data
    monkeypatch.setattr(Path, 'read_bytes', change_after_read)
    output = tmp_path/'race-candidate.zip'
    result = r.refresh_source_packet(c, c.digest(packet), output)
    assert result['status'] == 'UNRECEIVED_SOURCE_ONLY_PACKET'
    with zipfile.ZipFile(output) as archive:
        consumed = archive.read(r.READINESS_PATH)
        assert consumed == reviewed  # Never archive unchecked replacement bytes.
        assert json.loads(archive.read(r.MANIFEST))[r.READINESS_PATH] == hashlib.sha256(reviewed).hexdigest()
    assert observed == [reviewed]  # The validated buffer is captured only once.
    assert real_read(target) == unreviewed  # A real scratch-path replacement occurred.


def test_receive_renewal_requires_exact_predecessor_and_archives(tmp_path, monkeypatch):
    import contextlib
    import zipfile
    import glp_sponsor_current_receiving as r
    import sync_from_repo as sync
    monkeypatch.setattr(c,'ROOT',tmp_path)
    monkeypatch.setattr(c,'STATE',tmp_path/'runtime');c.STATE.mkdir()
    monkeypatch.setattr(c,'CONFIG',tmp_path/r.CONFIG)
    c.save(c.CONFIG,{'send_enabled':False,'packet_sha256':None})
    monkeypatch.setattr(r,'require_coordinator_execution',lambda:'root-session')
    monkeypatch.setattr(r,'current_authority',lambda *a,**kw:{'coordinator':{'session_id':'root-session'}})
    monkeypatch.setattr(r,'reviewed_source_head',lambda _: 'a'*40)
    monkeypatch.setattr(r,'packet_binding',lambda *a,**kw:{'source':'reviewed'})
    monkeypatch.setattr(c,'approval_ready',lambda _:True)
    monkeypatch.setattr(sync,'source_lock',lambda _:contextlib.nullcontext())
    def packet(path,sender=b'old',copy=b'unchanged'):
        path.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('scripts/glp_sponsor_campaign.py',sender)
            z.writestr(r.COPY,copy)
            z.writestr(r.MANIFEST,b'{}')
    packet(tmp_path/r.PACKET)
    old=dict(packet_sha256=c.digest(tmp_path/r.PACKET),schema=r.SCHEMA)
    c.save(c.STATE/r.RECEIPT,old);c.save(c.STATE/'approval.json',old)
    c.save(c.STATE/'activation.json',dict(send_enabled=True,packet_sha256=old['packet_sha256']))
    old_receipt=(c.STATE/r.RECEIPT).read_bytes();old_packet=(tmp_path/r.PACKET).read_bytes()
    old_sha=c.digest(c.STATE/r.RECEIPT)
    candidate=tmp_path/'candidate.zip';packet(candidate,b'new')
    result=tmp_path/'result.json'
    with pytest.raises(RuntimeError,match='current_renewal_predecessor_changed'):
        r.receive(c,c.digest(candidate),'a'*40,result,successor_packet=candidate,replace_receipt_sha256='0'*64)
    packet(candidate,b'new',b'changed commercial copy')
    with pytest.raises(RuntimeError,match='current_renewal_outward_bytes_changed'):
        r.receive(c,c.digest(candidate),'a'*40,result,successor_packet=candidate,replace_receipt_sha256=old_sha)
    assert (c.STATE/r.RECEIPT).read_bytes()==old_receipt
    assert (tmp_path/r.PACKET).read_bytes()==old_packet
    packet(candidate,b'new')
    outcome=r.receive(c,c.digest(candidate),'a'*40,result,successor_packet=candidate,replace_receipt_sha256=old_sha)
    assert outcome['status']=='CURRENT_PACKET_RECEIVED'
    archived=list((tmp_path/'canonical-preimages').iterdir())
    assert any(p.read_bytes()==old_receipt for p in archived)
    assert any(p.read_bytes()==old_packet for p in archived)
    assert (tmp_path/r.PACKET).read_bytes()==candidate.read_bytes()
    assert c.load(c.STATE/r.RECEIPT)==c.load(c.STATE/'approval.json')


@pytest.mark.parametrize('organization',['altRx - GLP1 - US - CPS','ShedRX - GLP-1 Microdosing - CPS (US)','Brand‐Name','Brand‑Name','Brand‒Name','Brand–Name','Brand—Name','Brand―Name'])
def test_organization_display_dash_removal_preserves_words(monkeypatch, organization):
    import re
    from inbox_control_plane.outbound_style_guard import assert_dash_free, DASH_CHARS
    root=Path(__file__).resolve().parents[1]
    monkeypatch.setattr(c,'ROOT',root)
    config=c.load(root/'ops/sponsor-machine/campaign.json')
    copy=c.load(root/'ops/sponsor-machine/copy.json')
    row=dict(organization=organization,email='fixture@example.test',host='example.test')
    for stage in range(3):
        out=c.render(row,stage,config)
        assert_dash_free(out['subject']+'\n'+out['body'])
        expected=re.sub(' *['+re.escape(DASH_CHARS)+']+ *',' ',organization)
        item=copy['variants']['cold'][stage]
        assert out['body']==item['body'].replace('{organization}',expected)+c.footer(config)
        assert out['subject']==item['subject'].replace('{organization}',expected)
        assert row['organization']==organization  # provenance not rewritten


def test_interested_copy_is_dash_free():
    from inbox_control_plane.outbound_style_guard import assert_dash_free
    copy=c.load(Path(__file__).resolve().parents[1]/'ops/sponsor-machine/copy.json')
    item=copy['interested_next_step']
    assert_dash_free(item['subject']+'\n'+item['body'])


def test_source_acceptance_binds_installed_bytes(tmp_path, monkeypatch):
    import glp_sponsor_current_receiving as r
    monkeypatch.setattr(c,'ROOT',tmp_path)
    pins={'campaign_sender_sha256':'scripts/glp_sponsor_campaign.py',
          'current_receiver_sha256':'scripts/glp_sponsor_current_receiving.py',
          'nuimage_sender_sha256':'scripts/glp_sponsor_nuimage_reply.py',
          'readiness_sha256':'scripts/glp_sponsor_readiness.py'}
    accepted=dict(status='PASS',reviewer='synthetic independent reviewer',reviewed_source_head='a'*40)
    for key,name in pins.items():
        path=tmp_path/name;path.parent.mkdir(exist_ok=True);path.write_text(name)
        accepted[key]=c.digest(path)
    c.save(tmp_path/r.SOURCE_REVIEW,accepted)
    assert r.reviewed_source_head(c)=='a'*40
    for change in ({'status':'PENDING'},{'reviewer':'S1'},{'nuimage_sender_sha256':'b'*64},
                   {'readiness_sha256':'b'*64}):
        c.save(tmp_path/r.SOURCE_REVIEW,dict(accepted,**change))
        with pytest.raises(RuntimeError,match='independent_installed_source_review_required'):
            r.reviewed_source_head(c)


@pytest.mark.parametrize('damage',['none','asset','source','pricing','address','send_guard','art_pending','extra_member','supply_count'])
def test_real_packet_binding_closed_members_and_guard_refusals(tmp_path,monkeypatch,damage,recorded_operator=False):
    import zipfile
    import glp_sponsor_current_receiving as r
    source=Path(__file__).resolve().parents[1]
    config=c.load(source/r.CONFIG);copy=c.load(source/r.COPY)
    monkeypatch.setattr(c,'ROOT',tmp_path)
    blobs={name:b'synthetic offline source or asset' for name in set(r.SOURCE_PATHS)|set(r.ASSETS)|{r.REVIEW}}
    if damage=='pricing':config['pricing']['initial_total']=1
    if damage=='address':config['postal_address']='obsolete address'
    if damage=='send_guard':config['require_bouncer']=False
    blobs[r.CONFIG]=json.dumps(config).encode();blobs[r.COPY]=json.dumps(copy).encode()
    art=dict(schema='sponsor.current_native_assets_review.v1',status='PASS',reviewer='synthetic test only',
             pricing=r.PRICING,postal_address=config['postal_address'],
             asset_sha256={name:hashlib.sha256(blobs[name]).hexdigest() for name in r.ASSETS})
    if damage=='art_pending':art['status']='PENDING'
    blobs[r.ASSET_REVIEW]=json.dumps(art).encode()
    blobs[r.SUPPLY]=json.dumps(dict(sends=0,organizations=[dict(host=f'org{i}.test') for i in range(11 if damage=='supply_count' else 12)])).encode()
    manifest={name:hashlib.sha256(blob).hexdigest() for name,blob in blobs.items()}
    blobs[r.MANIFEST]=json.dumps(manifest).encode()
    for name,blob in blobs.items():
        path=tmp_path/name;path.parent.mkdir(exist_ok=True,parents=True);path.write_bytes(blob)
    if damage=='asset':(tmp_path/r.ASSETS[0]).write_bytes(b'changed installed asset')
    if damage=='source':(tmp_path/'scripts/glp_sponsor_campaign.py').write_bytes(b'changed installed source')
    if damage=='extra_member':blobs['../unexpected']=b'bad'
    packet=tmp_path/'fixture.zip'
    with zipfile.ZipFile(packet,'w') as z:
        for name,blob in blobs.items():z.writestr(name,blob)
    if damage=='none':
        assert r.packet_binding(c,packet,config)==manifest
        # Real closed-packet, installed-review, renewal writer and approval reader
        # together. Only coordinator process identity/source lock are offline seams.
        import contextlib
        import sync_from_repo as sync
        monkeypatch.setattr(c,'STATE',tmp_path/'runtime'); c.STATE.mkdir()
        monkeypatch.setattr(c,'CONFIG',tmp_path/r.CONFIG)
        monkeypatch.setattr(r,'require_coordinator_execution',lambda:'test-root')
        monkeypatch.setattr(r,'current_authority',lambda *a,**kw:{'coordinator':{'session_id':'test-root'}})
        monkeypatch.setattr(sync,'source_lock',lambda _:contextlib.nullcontext())
        nuimage=tmp_path/'scripts/glp_sponsor_nuimage_reply.py'; nuimage.write_bytes(b'unchanged reviewed nuimage')
        accepted=dict(status='PASS',reviewer='Independent fixture reviewer',reviewed_source_head='a'*40)
        for key,name in {'campaign_sender_sha256':'scripts/glp_sponsor_campaign.py',
                         'current_receiver_sha256':'scripts/glp_sponsor_current_receiving.py',
                         'nuimage_sender_sha256':'scripts/glp_sponsor_nuimage_reply.py',
                         'sponsor_replies_sha256':'inbox_control_plane/sponsor_replies.py',
                         'readiness_sha256':'scripts/glp_sponsor_readiness.py'}.items():
            accepted[key]=c.digest(tmp_path/name)
        review_path=r.SUCCESSOR_SOURCE_REVIEW
        if recorded_operator:
            from test_glp_sponsor_unattended import decisions
            decisions(tmp_path,monkeypatch)
            accepted['canonical_inbox_sha256']=c.digest(tmp_path/'inbox_control_plane/control_plane.py')
            accepted['firecrawl_client_sha256']=c.digest(tmp_path/'scripts/firecrawl_client.py')
            review_path=r.OPERATOR_SOURCE_REVIEW
            monkeypatch.setattr(r,'require_coordinator_execution',lambda:pytest.fail('NO TUI CUSTODY FOR AUTOMATION'))
            monkeypatch.setattr(r,'current_authority',lambda *a,**kw:pytest.fail('NO TUI CUSTODY FOR AUTOMATION'))
        c.save(tmp_path/review_path,accepted)
        installed=tmp_path/r.PACKET; installed.parent.mkdir(parents=True,exist_ok=True)
        old=dict(blobs); old['scripts/glp_sponsor_campaign.py']=b'predecessor source'
        old[r.READINESS_PATH]=b'predecessor readiness'
        monkeypatch.setattr(r,'READINESS_RENEWAL',
            (hashlib.sha256(old[r.READINESS_PATH]).hexdigest(),manifest[r.READINESS_PATH]),raising=False)
        old_manifest={name:hashlib.sha256(blob).hexdigest() for name,blob in old.items() if name!=r.MANIFEST}
        old[r.MANIFEST]=json.dumps(old_manifest).encode()
        (tmp_path/r.MANIFEST).write_bytes(old[r.MANIFEST])
        with zipfile.ZipFile(installed,'w') as z:
            for name,blob in old.items(): z.writestr(name,blob)
        previous=dict(schema=r.SCHEMA,packet_sha256=c.digest(installed))
        c.save(c.STATE/r.RECEIPT,previous); c.save(c.STATE/'approval.json',previous)
        c.save(c.STATE/'activation.json',dict(send_enabled=True,packet_sha256=c.digest(installed)))
        packet=tmp_path/'supported-refresh.zip'
        r.refresh_source_packet(c,c.digest(installed),packet)
        result=r.receive(c,c.digest(packet),'a'*40,tmp_path/'receiving.json',successor_packet=packet,
                         replace_receipt_sha256=c.digest(c.STATE/r.RECEIPT),recorded_operator=recorded_operator)
        assert result['status']=='CURRENT_PACKET_RECEIVED'
        assert c.approval_ready(c.campaign_config()) is True
        accepted['reviewer']='RV-S2'; c.save(tmp_path/review_path,accepted)
        with pytest.raises(RuntimeError,match='independent_installed_source_review_required'):
            c.approval_ready(c.campaign_config())
    else:
        with pytest.raises(RuntimeError):r.packet_binding(c,packet,config)
