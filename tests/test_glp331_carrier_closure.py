"""GLP331 exact seven-source/native hash-only closure: disposable, no live effects."""
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import fleet_pin_release as release
import wrapper_pin_sync as pins
import maxbounty_pin_release as p

NATIVE = Path(os.environ.get('GLP_NATIVE_CHECKOUT', '/home/ubuntu/.hermes/hermes-agent'))


def test_scope_matches_withheld_pr117_cohort_not_pin_dependencies():
    expected = {
        'scripts/affiliate_approval_auto_reconciler.py',
        'scripts/affiliate_program_withdrawal.py',
        'scripts/affiliate_sheet_batch_writer.py',
        'scripts/affiliate_terms_fanout.py',
        'scripts/affiliate_withdrawal_candidate.py',
        'scripts/glp_sheet_sync_drain.py',
        'ops/hermes-cron-scripts/glp_affiliate_reconciliation_honesty.sh',
    }
    assert release.GLP331_SOURCES == expected
    # GLP268 changes the current maintained reader, not the historical seven-
    # source adoption contract. Keep its original blob and exact candidate
    # successor bound separately; the producer must still stage the original.
    successor = {'scripts/affiliate_approval_auto_reconciler.py': (
        '26df9de611b1eeb02443abc60912cd784c5c0d49d41b1e8cfd2ba97b73220c46',
        # Keep original adoption immutable; bind the reviewed inbox-Triage successor.
        'b259c582c22a96ca3bc43933125a65a52d1b3353976b2187cf076757c3b6567b'),
        # GLP347 preserves the historical adoption producer and binds the current denial-only successors.
        'scripts/affiliate_sheet_batch_writer.py': (
            '0fd0453ed5ce2c696befb7687a15b35223fad97889a90f93e0b4286f7c563d66',
            'aef4747d1e9ca683df5bf0edc31c4be12d6769f28a9fe438f439f09e83bfefe3'),
        'scripts/glp_sheet_sync_drain.py': (
            'aa12fc53f878bb8c0505fca4681cd116eae1a81bacf99969b1b8ffab48a76abd',
            '10bcc641001f360d9d51046ed84e63bd9e451ad2c800e7e5d010b9e15840628e'),
        'ops/hermes-cron-scripts/glp_affiliate_reconciliation_honesty.sh': (
            '45ad2a9b2c435dc96725456721f688c37cdd73d8f58e57351960c242c4d6e4a9',
            '99bcc4cca7c63f41e3546bb0e470976907027557bf1e7b89d1a0dbb8aaebcfb4')}
    for name in expected:
        original = p.git(ROOT, 'show', release.GLP331_SOURCE_HEAD+':'+name)
        if name in successor:
            old, candidate = successor[name]
            assert p.sha_bytes(original) == old
            assert p.sha_bytes((ROOT/name).read_bytes()) == candidate
        else:
            assert (ROOT/name).read_bytes() == original
    assert "p.git(repo, 'show', source_head+':'+name)" in (ROOT/'scripts/fleet_pin_release.py').read_text()
    assert p.sha_bytes(p.git(ROOT, 'show', release.GLP331_SOURCE_HEAD+':'+release.GLP331_SNAPSHOT)) == (
        '45ad2a9b2c435dc96725456721f688c37cdd73d8f58e57351960c242c4d6e4a9')


def test_current_base_worker_successor_copin_and_historical_custody():
    name = 'scripts/glp_affiliate_wire_backlog_worker.py'
    legacy = '1f6988537a04f9b0813240a7c637cdd1bba840f61155b6af2717dbe016417c6a'
    predecessor = '59e25a8c605f6c0c2ae284edfb3dae99eb7354c2679146fb787a923a4544ef05'
    successor = '16cac080089a5b75da8ab3fba6d875bdd30a46bee545e0dd751dc97ed30e1108'
    assert release.GLP331_DEPENDENCY_BINDINGS[name] == (legacy, successor)
    assert p.sha_bytes((ROOT/name).read_bytes()) == successor
    manifest = json.loads((ROOT/'ops/rebuild-source-manifest.json').read_text())
    assert manifest['files'][name]['sha256'] == successor
    # Immutable predecessor producer/source remain reproducible, not relabeled
    # as approval of this new dependency. The accepted seven source blobs stay
    # at their historical source_head (also checked by the literal scope test).
    historical = 'ae6a761c53c035e5fd7a79b3a6f2af2ba85378b7'
    assert p.sha_bytes(p.git(ROOT, 'show', historical+':'+name)) == predecessor
    old_producer = p.git(ROOT, 'show', historical+':scripts/fleet_pin_release.py')
    assert predecessor.encode() in old_producer and successor.encode() not in old_producer
    old_manifest = json.loads(p.git(ROOT, 'show', historical+':ops/rebuild-source-manifest.json'))
    assert old_manifest['files'][name]['sha256'] == predecessor


def test_original_writer_two_copins_every_occurrence(tmp_path, monkeypatch, capsys):
    writer = tmp_path/'writer.py'
    writer.write_text('VALUE = 1\n')
    stale = '0'*64
    other = '1'*64
    wrapper = tmp_path/'wrapper.sh'
    original = (f"wire_writer='{writer}'\nwire_writer_expected_sha256='{stale}'\n"
                f"declare -A b2_pins=(\n  ['{writer}']='{other}'\n)\n"
                '# no business logic rewritten\n')
    wrapper.write_text(original)
    wrapper.chmod(0o700)
    monkeypatch.setattr(pins, 'RECEIPTS', tmp_path/'receipts')
    monkeypatch.setattr(pins, 'WRAPPER', wrapper)
    assert pins.main(['--check']) == 1
    assert json.loads(capsys.readouterr().out)['count'] == 2
    dry = pins.sync([writer], wrapper=wrapper, apply=False)
    assert len(dry['repinned']) == 2 and wrapper.read_text() == original
    done = pins.sync([writer], wrapper=wrapper)
    assert len(done['repinned']) == 2
    assert len({row['hash_offset'] for row in done['repinned']}) == 2
    assert wrapper.read_text() == original.replace(stale, p.sha_bytes(writer.read_bytes())).replace(other, p.sha_bytes(writer.read_bytes()))
    assert len(pins.covered(wrapper.read_text())) == 2
    assert wrapper.stat().st_mode & 0o777 == 0o700
    again = pins.sync([writer], wrapper=wrapper)
    assert not again['repinned'] and len(again['already_current']) == 2


def fixture(tmp_path, monkeypatch):
    repo, business, home = (tmp_path/name for name in ('repo', 'business', 'home'))
    for base in (repo, business):
        (base/'scripts').mkdir(parents=True)
        (base/'ops/hermes-cron-scripts').mkdir(parents=True)
    for name in ('scripts', 'cron', 'state'):
        (home/name).mkdir(parents=True)
    # Real Git and all executed producer/dependency bytes, not a mocked planner.
    for name in ('fleet_pin_release.py', 'wrapper_pin_sync.py', 'maintenance_admission.py',
                 'maintenance_cohort.py', 'maintenance_window.py', 'sync_from_repo.py',
                 'maxbounty_pin_release.py', 'pin_drift_report.py'):
        (repo/'scripts'/name).write_bytes((ROOT/'scripts'/name).read_bytes())
    registered = json.loads((ROOT/'ops/registered-producers.json').read_text())
    (repo/'ops/registered-producers.json').write_text(json.dumps(registered))
    bodies = {name: ('# harmless successor '+name+'\n').encode() for name in release.GLP331_SOURCES}
    writer = business/'scripts/affiliate_sheet_batch_writer.py'
    scanner = business/'scripts/glp_wire_in_ready_queue.py'
    metadata = business/'scripts/affiliate_provider_metadata.py'
    scanner.write_text('# unchanged scanner dependency\n')
    metadata.write_text('# unchanged metadata dependency\n')
    dependency = business/'scripts/unchanged.py'
    dependency.write_text('# unchanged pinned dependency\n')
    # Four exact production dependency source bytes, never imported/executed.
    # Legacy carrier pins are intentionally stale; producer must derive only
    # these fixed substitutions, refusing a missing or drifted installed input.
    legacy_dependencies = ''
    for relative, (legacy, reviewed) in release.GLP331_DEPENDENCY_BINDINGS.items():
        body = (ROOT/relative).read_bytes()
        assert p.sha_bytes(body) == reviewed
        (repo/relative).write_bytes(body)
        (business/relative).write_bytes(body)
        legacy_dependencies += f" ['{business/relative}']='{legacy}'\n"
    source_manifest = {'files': {relative: {'sha256': reviewed} for relative, (_, reviewed)
                                in release.GLP331_DEPENDENCY_BINDINGS.items()}}
    (repo/'ops/rebuild-source-manifest.json').write_text(json.dumps(source_manifest))
    successor = ("#!/bin/bash\n"
        f"wire_writer='{writer}'\nwire_writer_expected_sha256='{p.sha_bytes(bodies[str(writer.relative_to(business))])}'\n"
        f"wire_scanner='{scanner}'\nwire_scanner_expected_sha256='{p.sha_bytes(scanner.read_bytes())}'\n"
        f"wire_metadata='{metadata}'\nwire_metadata_expected_sha256='{p.sha_bytes(metadata.read_bytes())}'\n"
        f"declare -A b2_pins=(\n ['{writer}']='{p.sha_bytes(bodies[str(writer.relative_to(business))])}'\n"
        f" ['{dependency}']='{p.sha_bytes(dependency.read_bytes())}'\n{legacy_dependencies})\n"
        "printf successor > \"$1\"\n").encode()
    old = b'#!/bin/bash\nprintf predecessor > "$1"\n'
    bodies[release.GLP331_SNAPSHOT] = successor
    for name, body in bodies.items():
        (repo/name).write_bytes(body)
        if name not in {'scripts/affiliate_program_withdrawal.py', 'scripts/affiliate_withdrawal_candidate.py'}:
            (business/name).write_bytes(b'# harmless predecessor\n')
    target = home/'scripts'/release.GLP331_CARRIER
    target.write_bytes(old); target.chmod(0o700)
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
                    'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture'], check=True)
    head = p.git(repo, 'rev-parse', 'HEAD').decode().strip()
    monkeypatch.setattr(release, 'GLP331_SOURCE_HEAD', head)
    monkeypatch.setattr(release, 'GLP331_PREDECESSOR', p.sha_bytes(old))
    jobs = [dict(id=release.GLP331_JOB, script=release.GLP331_CARRIER, script_sha256=p.sha_bytes(old),
                 enabled=True, state='scheduled', schedule={'kind':'cron','expr':'15 7 * * *','display':'daily'},
                 next_run_at='2999-01-01T00:00:00+00:00', run_claim=None, fire_claim=None,
                 failure_streak=2, routing={'keep':[1,2]}, last_error='fixture failure'),
            dict(id='3c79a0b5f7b2', script='inbox.py', enabled=True, state='scheduled',
                 schedule={'kind':'interval','minutes':5}),
            dict(id='b0c800751b88', script='approval.sh', enabled=True, state='scheduled',
                 schedule={'kind':'interval','minutes':10}),
            dict(id='held', script='other.sh', script_sha256='f'*64, enabled=False,
                 state='paused', pause_reason='Fella excluded', schedule={'kind':'interval','minutes':15})]
    registry = home/'cron/jobs.json'
    registry.write_text(json.dumps({'jobs': jobs})); registry.chmod(0o600)
    return repo, business, home, bodies, jobs, target, registry, dependency


def review_fixture(f, tmp_path):
    repo, business, home, *_ = f
    proposal, observed, staged = release.prepare_glp331(repo, release.GLP331_SOURCE_HEAD,
        'fixture-author', business=business, hermes=home)
    assert 'decision' not in proposal
    receipt = tmp_path/'fixture-review.json'
    receipt.write_text(json.dumps({'schema':'fleet-pin-glp331-review-v1',
        'decision':'ACCEPT_EXACT_CURRENT_BYTES', 'reviewer':'fixture-independent-reviewer',
        'reviewed_at_utc': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'evidence':['Synthetic test authority ONLY; never host acceptance'],
        'writer_exclusion':'Fixture has no other source writers or retained in-memory callers',
        'proposal':proposal}))
    return receipt, proposal, observed, staged


def test_scoped_prepare_full_record_and_in_scope_drift_refuse(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    repo, business, home, bodies, jobs, target, registry, dependency = f
    receipt, proposal, observed, staged = review_fixture(f, tmp_path)
    preview = release.release_glp331(receipt, repo, business=business, hermes=home)
    assert not preview['applied'] and len(staged) == 8
    assert len(proposal['source_sha256']) == 7
    assert staged[business/release.GLP331_SNAPSHOT] == bodies[release.GLP331_SNAPSHOT]
    assert len(proposal['dependency_binding_changes']) == 4
    # Independently reconstruct the four substitutions and reverse equality.
    expected = bodies[release.GLP331_SNAPSHOT]
    for name, (legacy, reviewed) in release.GLP331_DEPENDENCY_BINDINGS.items():
        expected = expected.replace(legacy.encode(), reviewed.encode())
        path = business/name
        before = path.read_bytes()
        path.unlink()
        with pytest.raises(FileNotFoundError):
            release.prepare_glp331(repo, release.GLP331_SOURCE_HEAD, 'fixture-author', business=business, hermes=home)
        assert target.read_bytes() == observed[target][0]
        path.write_bytes(before+b' ')
        with pytest.raises(RuntimeError, match='in_scope_unreviewed_pin_drift'):
            release.prepare_glp331(repo, release.GLP331_SOURCE_HEAD, 'fixture-author', business=business, hermes=home)
        path.write_bytes(before)
    assert staged[target] == expected
    assert not (business/'scripts/affiliate_program_withdrawal.py').exists()
    dependency.write_text('# unreviewed drift\n')
    with pytest.raises(RuntimeError, match='in_scope_unreviewed_pin_drift'):
        release.prepare_glp331(repo, release.GLP331_SOURCE_HEAD, 'fixture-author', business=business, hermes=home)
    dependency.write_bytes(observed[dependency][0])
    changed = copy.deepcopy(jobs); changed[0]['routing'] = {'unreviewed': True}
    registry.write_text(json.dumps({'jobs':changed}))
    with pytest.raises(RuntimeError, match='full_record_cas'):
        release.release_glp331(receipt, repo, business=business, hermes=home)
    registry.write_bytes(observed[registry][0])
    changed = copy.deepcopy(jobs); changed.append(copy.deepcopy(jobs[0]))
    registry.write_text(json.dumps({'jobs':changed}))
    with pytest.raises(RuntimeError, match='duplicate_registration_refused'):
        release.prepare_glp331(repo, release.GLP331_SOURCE_HEAD, 'fixture-author', business=business, hermes=home)
    assert target.read_bytes() == observed[target][0]


@pytest.mark.parametrize('boundary', ['installed_predecessor', 'forged_source_and_manifest', 'historical_review'])
def test_worker_successor_does_not_admit_historical_dependency_or_review(tmp_path, monkeypatch, boundary):
    f = fixture(tmp_path, monkeypatch)
    repo, business, home, _, _, target, registry, _ = f
    receipt, proposal, observed, staged = review_fixture(f, tmp_path)
    name = 'scripts/glp_affiliate_wire_backlog_worker.py'
    historical = 'ae6a761c53c035e5fd7a79b3a6f2af2ba85378b7'
    before = p.git(ROOT, 'show', historical+':'+name)
    predecessor = p.sha_bytes(before)
    row = next(item for item in proposal['dependency_binding_changes']
               if item['path'] == str(business/name))
    assert row['to_sha256'] == '16cac080089a5b75da8ab3fba6d875bdd30a46bee545e0dd751dc97ed30e1108'
    if boundary == 'installed_predecessor':
        (business/name).write_bytes(before)
        reason = 'in_scope_unreviewed_pin_drift'
    elif boundary == 'forged_source_and_manifest':
        # Matching old Git blob + manifest + receiving bytes must not bypass
        # the literal successor, even in an otherwise clean committed checkout.
        (business/name).write_bytes(before)
        (repo/name).write_bytes(before)
        manifest = json.loads((repo/'ops/rebuild-source-manifest.json').read_text())
        manifest['files'][name]['sha256'] = predecessor
        (repo/'ops/rebuild-source-manifest.json').write_text(json.dumps(manifest))
        subprocess.run(['git', '-C', str(repo), 'add', '.'], check=True)
        subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Fixture', '-c',
                        'user.email=fixture@example.invalid', 'commit', '-qm', 'old dependency graph'], check=True)
        reason = 'glp331_reviewed_dependency_source_required'
    else:
        review = json.loads(receipt.read_text())
        old = review['proposal']
        old['producer_head'] = historical
        next(item for item in old['dependency_binding_changes']
             if item['path'] == str(business/name))['to_sha256'] = predecessor
        receipt.write_text(json.dumps(review))
        reason = 'review_proposal_moved_full_record_cas'
    with pytest.raises(RuntimeError, match=reason):
        release.release_glp331(receipt, repo, business=business, hermes=home, apply=True)
    assert target.read_bytes() == observed[target][0]
    assert registry.read_bytes() == observed[registry][0]
    assert not (business/'reports').exists()
    assert not (business/'scripts/affiliate_program_withdrawal.py').exists()


@pytest.mark.parametrize('mutation', ['self_review', 'stale', 'review_only', 'claim', 'schedule', 'source_mode'])
def test_scoped_admission_refusal(tmp_path, monkeypatch, mutation):
    f = fixture(tmp_path, monkeypatch)
    repo, business, home, _, jobs, _, registry, _ = f
    receipt, proposal, observed, staged = review_fixture(f, tmp_path)
    review = json.loads(receipt.read_text())
    if mutation == 'self_review': review['reviewer'] = 'fixture-author'
    if mutation == 'stale': review['reviewed_at_utc'] = '2020-01-01T00:00:00Z'
    if mutation == 'review_only': review['decision'] = 'REVIEW_ONLY'
    if mutation == 'claim':
        jobs[1]['run_claim'] = {'token':'fixture'}; registry.write_text(json.dumps({'jobs':jobs}))
    if mutation == 'schedule':
        jobs[0]['schedule']['expr'] = '* * * * *'; registry.write_text(json.dumps({'jobs':jobs}))
    if mutation == 'source_mode': (business/'scripts/affiliate_sheet_batch_writer.py').chmod(0o600)
    receipt.write_text(json.dumps(review))
    with pytest.raises(RuntimeError):
        release.release_glp331(receipt, repo, business=business, hermes=home, apply=True)
    assert not (business/'reports').exists()
    assert not (business/'scripts/affiliate_program_withdrawal.py').exists()


@pytest.mark.skipif(not (NATIVE/'cron/jobs.py').exists(), reason='unchanged native scheduler checkout required')
@pytest.mark.skipif(os.geteuid() == 0, reason='real unprivileged DAC required')
@pytest.mark.parametrize('outcome', ['ok', 'raise_before', 'raise_after', 'bad_readback'])
def test_target_specific_native_hash_only_release(tmp_path, monkeypatch, outcome):
    f = fixture(tmp_path, monkeypatch)
    repo, business, home, bodies, jobs, target, registry, dependency = f
    monkeypatch.setenv('HERMES_HOME', str(home)); monkeypatch.setenv('HERMES_SKIP_ENV_FILE','1')
    monkeypatch.syspath_prepend(str(NATIVE))
    from cron import jobs as native
    assert Path(native.__file__).resolve() == (NATIVE/'cron/jobs.py').resolve()
    assert native._current_cron_store().jobs_file == registry
    receipt, proposal, observed, staged = review_fixture(f, tmp_path)
    marker = tmp_path/'execution-marker'
    calls = []
    readbacks = []
    read_regular = p.read_regular
    def observed_readback(path):
        if path == registry and calls and not readbacks:
            for member in staged:
                assert member.stat().st_mode & 0o777 == 0
                with pytest.raises(PermissionError): member.read_bytes()
            readbacks.append('sealed-native-readback')
        return read_regular(path)
    monkeypatch.setattr(p, 'read_regular', observed_readback)
    save = native._save_jobs_unlocked
    def native_save(candidate, **kw):
        calls.append('save')
        # ALL public outputs, including newly linked sources, stay sealed through
        # the real native saver AND its readback; no carrier may execute.
        for path in staged:
            assert path.exists() and path.stat().st_mode & 0o777 == 0
            with pytest.raises(PermissionError): path.read_bytes()
        probe = subprocess.run(['bash', str(target), str(marker)], capture_output=True)
        assert probe.returncode != 0 and not marker.exists()
        assert json.loads(registry.read_text())['jobs'] == jobs
        for jid in release.GLP331_CONSUMERS:
            probe = subprocess.run([sys.executable, '-c',
                'import sys;sys.path.insert(0,sys.argv[1]);from cron import jobs;'
                'jobs._JOBS_LOCK_TIMEOUT_SECONDS=0.05;print(jobs.claim_job_for_fire(sys.argv[2],manual=True))',
                str(NATIVE), jid], capture_output=True, text=True, timeout=15)
            assert probe.returncode == 0 and probe.stdout.strip() == 'False'
        importer = subprocess.run([sys.executable, str(ROOT/'scripts/sync_from_repo.py'),
            '--root',str(business),'--checkout',str(repo),'--apply'],capture_output=True,text=True,timeout=15)
        assert importer.returncode == 2 and 'source_import_busy' in importer.stderr
        if outcome == 'raise_before': raise RuntimeError('injected native save failure')
        if outcome == 'bad_readback': candidate[0]['routing'] = {'unexpected':True}
        save(candidate, **kw)
        for path in staged: assert path.stat().st_mode & 0o777 == 0
        if outcome == 'raise_after': raise RuntimeError('injected native save failure')
    monkeypatch.setattr(native, '_save_jobs_unlocked', native_save)
    if outcome != 'ok':
        reason = 'readback_mismatch' if outcome == 'bad_readback' else 'injected native save failure'
        with pytest.raises(RuntimeError, match=reason):
            release.release_glp331(receipt, repo, business=business, hermes=home, apply=True, scheduler=native)
        assert calls == ['save']
        assert readbacks == (['sealed-native-readback'] if outcome == 'bad_readback' else [])
        for path in staged: assert path.stat().st_mode & 0o777 == 0
        with pytest.raises(RuntimeError, match='no_replay'):
            release.release_glp331(receipt, repo, business=business, hermes=home, apply=True, scheduler=native)
        assert calls == ['save'] and not list((business/'reports').rglob('acceptance.json'))
        current = json.loads(registry.read_text())['jobs']
        assert current[1:] == jobs[1:]
        assert current[0]['script_sha256'] == (jobs[0]['script_sha256'] if outcome == 'raise_before' else p.sha_bytes(staged[target]))
        return
    result = release.release_glp331(receipt, repo, business=business, hermes=home, apply=True, scheduler=native)
    assert result['applied'] and calls == ['save']
    assert readbacks == ['sealed-native-readback']
    for path, before in observed.items():
        if path not in staged and path != registry:
            assert p.read_regular(path) == before
    expected = copy.deepcopy(jobs); expected[0]['script_sha256'] = p.sha_bytes(staged[target])
    assert json.loads(registry.read_text())['jobs'] == expected
    for path, body in staged.items():
        assert path.read_bytes() == body
        assert path.stat().st_mode & 0o777 == proposal['source_modes'][str(path)]
        assert path.stat().st_nlink == 1
    assert dependency.read_bytes() == observed[dependency][0]
    assert not list(business.rglob('.glp331-new-*'))
    assert len(pins.covered(target.read_text())) == 9
    subprocess.run(['bash',str(target),str(marker)],check=True)
    assert marker.read_text() == 'successor'
    with pytest.raises(RuntimeError, match='no_replay'):
        release.release_glp331(receipt, repo, business=business, hermes=home, apply=True, scheduler=native)
    assert calls == ['save']
