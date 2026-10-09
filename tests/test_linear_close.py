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


def test_worker_result_closes_check_only(tmp_path, monkeypatch):
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
    assert output and not lc.lo.is_bare_output(output[0][len('Output: '):])
