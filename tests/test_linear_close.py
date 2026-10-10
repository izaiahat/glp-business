"""linear_close builds a typed ordinary RESULT from a worker RESULT; Linear and gh are offline fakes."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import linear_close as lc  # noqa: E402

lo = lc.lo
ISSUE_ID = 'issue-uuid'
HEAD = 'a' * 40
MERGE = 'b' * 40
PR = 'https://github.com/izaiahat/glp-business/pull/281'
CI = 'https://github.com/izaiahat/glp-business/actions/runs/1/job/2'
DESCRIPTION = '## Acceptance\n\n- [ ] Installed shipper drain completes with a receipt.\n'
SHIPPER = b'merged shipper bytes'


def test_legacy_dry_run_posts_acceptance(tmp_path, monkeypatch):
    reports = tmp_path / 'reports'
    (reports / 'ops/job-receipts').mkdir(parents=True)
    receipt = reports / 'ops/job-receipts/ship-1.json'
    receipt.write_text(json.dumps({'status': 'SKIP_ALREADY_INSTALLED', 'deployment_id': '8bb93c91'}))
    worker = reports / 'ops/run/RESULT.json'
    worker.parent.mkdir(parents=True)
    (tmp_path / 'scripts').mkdir()
    (tmp_path / 'scripts/glp_affiliati_postback_ship.py').write_bytes(SHIPPER)
    worker.write_text(json.dumps({
        'status': 'DONE_READY_TO_CLOSE', 'issues': ['GLP-335'], 'pr': 'PR281 MERGED as ' + MERGE[:8],
        'install': 'sync_from_repo.py --only scripts/glp_affiliati_postback_ship.py apply',
        'live_run': 'drain rc=0 SKIP_ALREADY_INSTALLED deployment 8bb93c91',
        'receipt': 'reports/ops/job-receipts/ship-1.json'}))
    target = {'id': ISSUE_ID, 'identifier': 'GLP-335', 'title': 'Host shipper drain', 'description': DESCRIPTION,
              'team': {'id': lo.TEAM, 'key': 'GLP'}, 'state': {'name': 'In Progress'}, 'labels': {'nodes': []}}
    remote = {'state': 'MERGED', 'headRefOid': HEAD, 'mergeCommit': {'oid': MERGE},
              'statusCheckRollup': [{'name': 'python-tests', 'detailsUrl': CI,
                                     'status': 'COMPLETED', 'conclusion': 'SUCCESS'}],
              'files': [{'path': 'scripts/glp_affiliati_postback_ship.py'}, {'path': 'tests/x.py'}]}
    posted = {}

    def gql(query, variables=None):
        if 'viewer' in query:
            return {'viewer': {'id': lo.OPERATOR_USER_ID}}
        return {'comment': {'id': 'c1', 'body': posted['body'], 'archivedAt': None,
                            'createdAt': datetime.now(timezone.utc).isoformat(),
                            'user': {'id': lo.OPERATOR_USER_ID}, 'issue': {'id': ISSUE_ID}}}

    def comment(iid, body):
        assert iid == ISSUE_ID
        posted['body'] = body
        return {'success': True, 'comment': {'id': 'c1'}}

    def orchestrate(ident, typed, *extra):
        assert extra == ('--check-only',)
        lo.pass_issue(lo.issue(ident), str(typed), {}, ordinary=True, check_only=True)

    monkeypatch.setattr(lc, 'ROOT', tmp_path)
    monkeypatch.setattr(lc, 'REPORTS', reports)
    monkeypatch.setattr(lo, '__file__', str(tmp_path / 'scripts/linear_orchestrator.py'))
    monkeypatch.setattr(lo, 'issue', lambda ident: target)
    monkeypatch.setattr(lo, 'gql', gql)
    monkeypatch.setattr(lo, 'comment', comment)
    monkeypatch.setattr(lc, 'gh_pr', lambda url: remote)
    monkeypatch.setattr(lc, 'orchestrate', orchestrate)
    def check_output(argv, **kwargs):
        if argv[:2] == ['gh', 'api']:
            assert argv[2] == 'repos/izaiahat/glp-business/contents/scripts/glp_affiliati_postback_ship.py?ref=' + MERGE
            return json.dumps({'encoding': 'base64', 'content': base64.b64encode(SHIPPER).decode()})
        return json.dumps(remote)
    monkeypatch.setattr(subprocess, 'check_output', check_output)

    state, typed = lc.close('GLP-335', worker)
    data = json.loads(typed.read_text())
    assert state == 'In Progress' and typed == worker.parent / 'GLP-335' / 'RESULT.json'
    assert data['scope'] == 'ordinary-runtime' and data['pr']['merge_sha'] == MERGE
    assert data['pr']['checks'] == [{'name': 'python-tests', 'url': CI}]
    assert data['description_sha256'] == hashlib.sha256(DESCRIPTION.encode()).hexdigest()
    assert data['evidence'][0]['sha256'] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert data['review'] == {'comment_id': 'c1', 'body_sha256': hashlib.sha256(posted['body'].encode()).hexdigest()}
    shipper_sha = hashlib.sha256(SHIPPER).hexdigest()
    assert data['installed_files'] == [{'path': str((tmp_path / 'scripts/glp_affiliati_postback_ship.py').resolve()),
                                        'sha256': shipper_sha}]
    assert 'Installed: scripts/glp_affiliati_postback_ship.py ' + shipper_sha in posted['body'].splitlines()
    output = [line for line in posted['body'].splitlines() if line.startswith('Output: ')]
    assert len(output) == 1 and 'sha256' not in output[0].split(', ') and '8bb93c91' in output[0]


def test_check_only_runs_real_gate_without_linear_or_filesystem_writes(tmp_path, monkeypatch, capsys):
    reports = tmp_path / 'reports'
    receipt = reports / 'ops/collector/receipt.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({'provider_rows': 58}))
    worker = receipt.parent / 'RESULT.json'
    worker.write_text(json.dumps({'status': 'DONE_READY_TO_CLOSE', 'issue': 'GLP-44',
        'live_run': 'Read-only snapshot provider_rows=58',
        'receipt': 'reports/ops/collector/receipt.json'}))
    target = {'id': ISSUE_ID, 'identifier': 'GLP-44', 'title': 'Collector snapshot',
        'description': '## Acceptance\n\nRead-only snapshot of provider rows.\n',
        'team': {'id': lo.TEAM, 'key': 'GLP'}, 'state': {'name': 'In Progress'}}
    writes = []
    def gql(query, variables=None):
        if 'mutation' in query:
            writes.append(query)
            raise AssertionError('check-only attempted a Linear mutation')
        assert query == 'query{viewer{id}}'
        return {'viewer': {'id': lo.OPERATOR_USER_ID}}
    def forbidden(*args, **kwargs):
        writes.append(args)
        raise AssertionError('check-only attempted a write')
    monkeypatch.setattr(lc, 'ROOT', tmp_path)
    monkeypatch.setattr(lc, 'REPORTS', reports)
    monkeypatch.setattr(lo, '__file__', str(tmp_path / 'scripts/linear_orchestrator.py'))
    monkeypatch.setattr(lo, 'issue', lambda ident: target)
    monkeypatch.setattr(lo, 'gql', gql)
    monkeypatch.setattr(lo, 'comment', forbidden)
    monkeypatch.setattr(lc, 'write', forbidden)
    monkeypatch.setattr(lc, 'orchestrate', forbidden)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert lc.main(['GLP-44', str(worker), '--check-only']) == 0
    assert 'GLP-44 PASS' in capsys.readouterr().out
    # Same gate must refuse missing current acceptance, rather than trusting status.
    target['description'] = 'No acceptance section'
    assert lc.main(['GLP-44', str(worker), '--check-only']) == 3
    assert 'REFUSED' in capsys.readouterr().out
    assert writes == []
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
    assert not (worker.parent / 'GLP-44').exists()



def test_letter_leading_hex_identifier_is_output():
    # Regression: a merge sha starting with a-f matched the field-name branch and was called bare.
    assert not lo.is_bare_output('da8bbf7322f1bc20b059e0f8a33ec7d1037c42a1')
    assert not lo.is_bare_output('2f816b36')
    assert lo.is_bare_output('rows_reverified') and lo.is_bare_output('PASS') and lo.is_bare_output('sha256')


def test_evidence_business_counts_are_observed(tmp_path, monkeypatch):
    # Regression: evidence whose only worker-cited token was a sha refused as "no business output".
    reports = tmp_path / 'reports'
    receipt = reports / 'ops/w3/main-taxonomy-check.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({'status': 'PASS', 'main_sha': 'da8bbf73', 'provider_rows': 58,
                                   'stale_20260908_rows': 0}, indent=2))
    monkeypatch.setattr(lc, 'REPORTS', reports)
    monkeypatch.setattr(lc, 'ROOT', tmp_path)
    worker = {'status': 'DONE_READY_TO_CLOSE', 'live_run': 'taxonomy PASS on main da8bbf73',
              'receipt': 'reports/ops/w3/main-taxonomy-check.json'}
    evidence = lc.collect_evidence(worker, str(reports / 'ops/w3/RESULT.json'))
    observed = evidence[0]['observed']
    assert '"provider_rows": 58' in observed and not any('stale_20260908_rows' in v for v in observed)
    assert lc.first_output({'evidence': evidence})
