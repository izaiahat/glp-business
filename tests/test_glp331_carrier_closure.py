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
    for name in expected:
        assert (ROOT/name).read_bytes() == p.git(ROOT, 'show', release.GLP331_SOURCE_HEAD+':'+name)
    assert p.sha_bytes((ROOT/release.GLP331_SNAPSHOT).read_bytes()) == (
        '45ad2a9b2c435dc96725456721f688c37cdd73d8f58e57351960c242c4d6e4a9')


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
    successor = ("#!/bin/bash\n"
        f"wire_writer='{writer}'\nwire_writer_expected_sha256='{p.sha_bytes(bodies[str(writer.relative_to(business))])}'\n"
        f"wire_scanner='{scanner}'\nwire_scanner_expected_sha256='{p.sha_bytes(scanner.read_bytes())}'\n"
        f"wire_metadata='{metadata}'\nwire_metadata_expected_sha256='{p.sha_bytes(metadata.read_bytes())}'\n"
        f"declare -A b2_pins=(\n ['{writer}']='{p.sha_bytes(bodies[str(writer.relative_to(business))])}'\n"
        f" ['{dependency}']='{p.sha_bytes(dependency.read_bytes())}'\n)\n"
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
    assert len(pins.covered(target.read_text())) == 5
    subprocess.run(['bash',str(target),str(marker)],check=True)
    assert marker.read_text() == 'successor'
    with pytest.raises(RuntimeError, match='no_replay'):
        release.release_glp331(receipt, repo, business=business, hermes=home, apply=True, scheduler=native)
    assert calls == ['save']
