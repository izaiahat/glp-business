"""GLP-44 collector publication successor in disposable maintained windows.

The reviewed hourly collector (PR142 + PR146) is a publication member, not only
a PR119 prerequisite. PR71 keeps staging its immutable old collector bytes.
"""
import copy
import json
from pathlib import Path
import subprocess

import pytest

from test_glp333_reporting_generation import ROOT, blob, generation_fixture, put, sha, payloads
import glp44_scoped_adoption as pair
import maintenance_window as mw
import sync_from_repo as syncer

GENERATION = 'glp44-collector-successor-20261002'
SOURCE = '488eeffd8a01f2fdb2fb19abf95636ad33829851'
COLLECTOR = 'systems/analytics-brain/collector.py'
REPORTING = 'scripts/glp_affiliate_weekly_reporting.py'
OLD_COLLECTOR = '24f3798f4e31c400a82fa7ae0dffaab27509636d0f4188265235247321b95b3c'
NEW_COLLECTOR = 'af1d8f9187384f025bcd042a17710581fabb1870fd5bf05c8dcff174f2a4203e'
MEMBERS = (*pair.PATHS, 'ops/glp/reporting-v2/identity-registry.json', REPORTING,
           'scripts/glp_affiliate_weekly_reporting_job.py')


def fixture(tmp_path, monkeypatch, *, advanced_reporting=False, host_collector=None):
    """Post-GLP73 host with pre-PR71 cohort; optionally main's reporting script.

    advanced_reporting reproduces the receiving refusal: the installed reporting
    script is already the merged main generation, so PR71's old pins cannot close.
    """
    enter = mw.MaintenanceWindow.enter
    def enter_with_host(window):
        root = window.backend.root
        state = json.loads((root / syncer.STATE).read_text())
        if advanced_reporting:
            body = blob(SOURCE, REPORTING)
            put(root / REPORTING, body, 0o640)
            state['hashes'][REPORTING] = sha(body)
        if host_collector is not None:
            put(root / COLLECTOR, host_collector, 0o640)
        put(root / syncer.STATE, (json.dumps(state) + '\n').encode())
        return enter(window)
    monkeypatch.setattr(mw.MaintenanceWindow, 'enter', enter_with_host)
    return generation_fixture(tmp_path, monkeypatch, pr71_predecessor=True)


def plan(f, generation=GENERATION, checkout=ROOT):
    with mw.require_active_window(f.journal, f.root, f.hermes) as active:
        return pair.plan(checkout, f.root, f.hermes, window=active, generation=generation)[0]


def acceptance(proposal):
    # Disposable fixture labels are not independent acceptance or live authority.
    return {**copy.deepcopy(proposal), 'schema': 'glp44-scoped-adoption-review-v1',
            'approved_source': SOURCE, 'decision': 'ACCEPT_EXACT_BYTES',
            'prepared_by': 'fixture preparer', 'reviewer': 'fixture independent source reviewer',
            'runtime_review': {'schema': 'glp44-collector-successor-runtime-review-v1',
                               'decision': 'ACCEPT_EXACT_BYTES', 'reviewer': 'fixture receiver',
                               'proposal_sha256': sha(json.dumps(proposal, sort_keys=True).encode())}}


def apply(f, review):
    return pair.adopt(ROOT, f.root, f.hermes, review, scheduler=f.scheduler,
                      window=f.journal, generation=GENERATION)


def test_successor_contract_is_fixed_and_pr71_history_unchanged():
    assert pair.generation_contract('pr71') == (pair.APPROVED, pair.PATHS)
    assert sha(blob(pair.APPROVED, COLLECTOR)) == OLD_COLLECTOR
    approved, paths = pair.generation_contract(GENERATION)
    assert approved == SOURCE and set(paths) == set(MEMBERS) and len(paths) == len(MEMBERS)
    # Every member is the exact committed PR146-merge byte generation.
    assert {rel: sha(blob(SOURCE, rel)) for rel in paths} == pair.COLLECTOR_HASHES
    assert pair.COLLECTOR_HASHES[COLLECTOR] == NEW_COLLECTOR == pair.PR119_PREREQUISITES[COLLECTOR]
    # The staged reporting pin manifest closes over the staged reporting members.
    pins = json.loads(blob(SOURCE, 'ops/glp/reporting-v2/source-manifest.json'))['files']
    for rel in (REPORTING, 'scripts/glp_affiliate_weekly_reporting_job.py',
                'systems/analytics-brain/analytics_brain.py'):
        assert pins[str(syncer.DEFAULT_ROOT / rel)] == pair.COLLECTOR_HASHES[rel]


@pytest.mark.parametrize('advanced_reporting', [False, True])
def test_successor_preview_closes_with_new_collector_and_applies(tmp_path, monkeypatch, advanced_reporting):
    f = fixture(tmp_path, monkeypatch, advanced_reporting=advanced_reporting)
    try:
        before = payloads(f)
        if advanced_reporting:
            # The reported receiving refusal: old PR71 pins cannot close.
            with pytest.raises(RuntimeError, match='reporting_pin_closure_refused:/home/ubuntu/business/'
                                                   'scripts/glp_affiliate_weekly_reporting.py'):
                plan(f, 'pr71')
        else:
            # PR71 remains immutable and still stages the old collector bytes.
            old = plan(f, 'pr71')
            assert old['source_sha256'][COLLECTOR] == OLD_COLLECTOR
            assert old['source_sha256'] == {rel: sha(blob(pair.APPROVED, rel)) for rel in pair.PATHS}
        proposal = plan(f)  # RED on main: unknown_reporting_generation.
        assert payloads(f) == before and not f.saves
        assert proposal['generation'] == GENERATION
        assert proposal['source_sha256'] == {rel: sha(blob(SOURCE, rel)) for rel in MEMBERS}
        assert proposal['source_sha256'][COLLECTOR] == NEW_COLLECTOR
        assert proposal['wrapper_sha256'] == sha(blob(SOURCE, pair.SNAPSHOT))
        pins = json.loads(blob(SOURCE, 'ops/glp/reporting-v2/source-manifest.json'))['files']
        retained = {str(Path(a).relative_to(syncer.DEFAULT_ROOT)) for a in pins} - set(MEMBERS)
        assert set(proposal['retained_source']) == retained
        review = acceptance(proposal)
        # A self-review or a review for the PR119 runtime schema is refused.
        for wrong in ({**review, 'reviewer': 'fixture preparer'},
                      {**review, 'runtime_review': {**review['runtime_review'],
                                                    'schema': 'glp333-reporting-runtime-review-v1'}}):
            with pytest.raises(RuntimeError, match='independent_matching_runtime_review_required'):
                apply(f, wrong)
        assert payloads(f) == before and not f.saves
        old_state = json.loads((f.root / syncer.STATE).read_text())
        old_jobs = json.loads(f.registry.read_text())['jobs']
        result = apply(f, review)
        assert result['applied'] and result['source'] == SOURCE and len(f.saves) == 1
        for rel in MEMBERS:
            assert (f.root / rel).read_bytes() == blob(SOURCE, rel)
            assert (f.root / rel).stat().st_mode & 0o777 == before[f.root / rel][1]
        assert sha((f.root / COLLECTOR).read_bytes()) == NEW_COLLECTOR
        assert (f.hermes / 'scripts' / pair.WRAPPER).read_bytes() == blob(SOURCE, pair.SNAPSHOT)
        expected_state = copy.deepcopy(old_state)
        expected_state['hashes'].update(proposal['source_sha256'])
        assert json.loads((f.root / syncer.STATE).read_text()) == expected_state
        expected_jobs = copy.deepcopy(old_jobs)
        expected_jobs[0]['script_sha256'] = proposal['wrapper_sha256']
        assert json.loads(f.registry.read_text())['jobs'] == expected_jobs
        assert mw.read_journal(f.journal)['source_transaction']['outcome'] == 'applied'
        custody = Path(result['custody'])
        for rel in MEMBERS:
            assert (custody / (sha(before[f.root / rel][0]) + '.bytes')).read_bytes() == before[f.root / rel][0]
        landed = payloads(f)
        # No replay: the job and source preimages have moved to the successor.
        with pytest.raises(RuntimeError, match='reporting_job_not_idle_or_preimage_moved'):
            apply(f, review)
        assert payloads(f) == landed and len(f.saves) == 1
        assert f.window.status()['verified']
    finally:
        f.window.release_locks()


def test_tampered_installed_collector_is_refused(tmp_path, monkeypatch):
    tampered = blob(SOURCE, COLLECTOR) + b'\n# tampered\n'
    f = fixture(tmp_path, monkeypatch, host_collector=tampered)
    try:
        before = payloads(f)
        with pytest.raises(RuntimeError, match='scoped_source_preflight_refused'):
            plan(f)
        assert payloads(f) == before and not f.saves
    finally:
        f.window.release_locks()


def test_tampered_candidate_collector_and_review_and_pinned_leaf_are_refused(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    checkout = tmp_path / 'checkout'
    head = subprocess.check_output(['git', '-C', str(ROOT), 'rev-parse', 'HEAD']).decode().strip()
    subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(ROOT), str(checkout)], check=True)
    subprocess.run(['git', '-C', str(checkout), 'update-ref', 'HEAD', head], check=True)
    subprocess.run(['git', '-C', str(checkout), 'read-tree', 'HEAD'], check=True)
    subprocess.run(['git', '-C', str(checkout), 'checkout-index', '--all'], check=True)
    try:
        before = payloads(f)
        (checkout / COLLECTOR).write_bytes((checkout / COLLECTOR).read_bytes() + b'\n# tampered\n')
        with pytest.raises(RuntimeError, match='approved_source_moved:' + COLLECTOR):
            plan(f, checkout=checkout)
        proposal = plan(f)
        tampered = acceptance(proposal)
        tampered['source_sha256'][COLLECTOR] = OLD_COLLECTOR
        with pytest.raises(RuntimeError, match='reviewed_preimage_or_candidate_moved:source_sha256'):
            apply(f, tampered)
        assert payloads(f) == before and not f.saves
        # Retained host-only pinned leaves still must match the staged manifest.
        leaf = f.root / 'scripts/affiliate_network_access.py'
        put(leaf, leaf.read_bytes() + b'\n# tampered\n', leaf.stat().st_mode & 0o777)
        with pytest.raises(RuntimeError, match='reporting_pin_closure_refused:' + str(syncer.DEFAULT_ROOT / 'scripts/affiliate_network_access.py')):
            plan(f)
        assert not f.saves
    finally:
        f.window.release_locks()
