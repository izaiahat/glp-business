"""Ordinary completion uses real GLP-60 identifiers; all transports are offline fakes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/linear_orchestrator.py'
spec = importlib.util.spec_from_file_location('ordinary_completion_writer', SOURCE)
assert spec is not None and spec.loader is not None
lo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lo)

ISSUE_ID = '247e3c39-28dd-4abf-b0f6-dda3b15381e3'
HEAD = '1f587d2c73783c22e3b4acfad28b81fe8fb693cb'
MERGE = '3b9bb64c7c0c5002e19ce6566eceecb94943e7b6'
PR = 'https://github.com/izaiahat/glp-business/pull/13'
MESSAGE = '1553248300512321647'
CI = 'https://github.com/izaiahat/glp-business/actions/runs/36215157038/job/108329556269'
DESCRIPTION = '''One change, <=1 day.

## Acceptance

* TEST-labeled event through installed sender → exact card read back in #payments with identity, status, amount/currency or unknown.
* Replayed event → no duplicate card or real earning; transport failure remains queued and never redirects to #decisions.

PR: pending (link exact PR/head SHA when opened; non-code effect: link receipt).
Owner: lane:kael (one accountable execution lane).
Skills: affiliate-network-reporting, discord-delivery.
Source issue: GLP-60. Historical detail preserved in rebuild snapshot.'''
REVIEW = ('Coordinator acceptance verified: PR #13 merged at ' + MERGE + '; exact-head python-tests passed. '
          'Installed sender bytes match the merged source. Fresh parent run: tests/test_glp_earnings_notice.py 12/12 PASS. '
          'Read-only Discord GET verified TEST message ' + MESSAGE + ' in #payments matches the sent journal exactly. '
          'Prior replay returned ALREADY_SENT with same ID; transport-failure regressions pass. No new send or REAL earning claimed. '
          'Evidence: reports/ops/glp59-60-20260926/RESULT.json.')


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def completion(tmp_path, monkeypatch):
    # Synthetic local transport artifact, not a claim that this test sends a card.
    reports = tmp_path / 'reports'
    reports.mkdir()
    evidence = reports / 'existing-acceptance.json'
    evidence.write_text(json.dumps({'message_id': MESSAGE, 'replay': 'ALREADY_SENT',
                                    'failure': 'QUEUED_UNCERTAIN', 'tests': '12 passed'}))
    data = {'schema': 'linear-ordinary-completion-v1', 'issue': 'GLP-60', 'status': 'COMPLETE',
            'scope': 'ordinary-runtime', 'protected_effects': [],
            'description_sha256': digest(DESCRIPTION),
            'pr': {'url': PR, 'head_sha': HEAD, 'merge_sha': MERGE,
                   'checks': [{'name': 'python-tests', 'url': CI}]},
            'review': {'comment_id': 'a976e1cb-b265-4915-ab5c-157417f9ed1a', 'body_sha256': digest(REVIEW)},
            'evidence': [{'kind': 'runtime', 'path': str(evidence), 'sha256': lo.sha(evidence),
                          'observed': [MESSAGE, 'ALREADY_SENT', 'QUEUED_UNCERTAIN', '12 passed']}]}
    path = reports / 'RESULT.json'
    path.write_text(json.dumps(data))
    target = {'id': ISSUE_ID, 'identifier': 'GLP-60', 'title': 'Prove idempotent payments-only earnings notice',
              'description': DESCRIPTION, 'team': {'id': lo.TEAM, 'key': 'GLP'},
              'state': {'name': 'In Progress'}, 'labels': {'nodes': []}}
    review = {'id': data['review']['comment_id'], 'body': REVIEW, 'archivedAt': None,
              'user': {'id': lo.OPERATOR_USER_ID}, 'issue': {'id': ISSUE_ID}}
    remote = {'state': 'MERGED', 'headRefOid': HEAD, 'mergeCommit': {'oid': MERGE},
              'statusCheckRollup': [{'name': 'python-tests', 'detailsUrl': CI,
                                     'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}
    writes = []
    monkeypatch.setattr(lo, '__file__', str(tmp_path / 'scripts/linear_orchestrator.py'))
    monkeypatch.setattr(lo, 'issue', lambda ident: target)
    monkeypatch.setattr(lo, 'states', lambda: {'Done': 'done', 'In Progress': 'started'})
    monkeypatch.setattr(lo, 'gql', lambda *args: {'comment': review})
    import subprocess
    monkeypatch.setattr(subprocess, 'check_output', lambda *args, **kwargs: json.dumps(remote))
    monkeypatch.setattr(lo, 'comment', lambda iid, body: writes.append(('comment', iid, body)) or
                        {'success': True, 'comment': {'id': 'receipt'}})
    monkeypatch.setattr(lo, 'set_state', lambda iid, state: writes.append(('state', iid, state)) or
                        {'identifier': 'GLP-60', 'state': 'Done'})
    monkeypatch.setattr(lo, 'receipt_validates', lambda *args: True)
    monkeypatch.setattr(lo, 'sync_wrapper_pins', lambda *args: pytest.fail('ordinary closure must not repin runtime'))
    return target, path, data, writes, remote, review


def run_cli(monkeypatch, path, *extra):
    monkeypatch.setattr(sys, 'argv', ['linear_orchestrator.py', 'pass', 'GLP-60', str(path), '--ordinary', *extra])
    lo.main()


def test_real_glp60_mapping_closes_through_existing_writer_without_brief(completion, monkeypatch):
    target, path, data, writes, _, _ = completion
    run_cli(monkeypatch, path)
    assert [w[0] for w in writes] == ['comment', 'state']
    receipt = json.loads(writes[0][2].split('```json')[1].split('```')[0])
    assert receipt == {'issue_id': ISSUE_ID, 'outcome': 'PASS', 'result_path': str(path),
                       'result_sha256': lo.sha(path)}
    assert writes[1] == ('state', ISSUE_ID, 'done')


@pytest.mark.parametrize('defect', ['absent', 'check_only', 'missing_file', 'changed_bytes', 'wrong_message'])
def test_missing_live_acceptance_never_mutates(completion, monkeypatch, defect):
    _, path, data, writes, _, _ = completion
    if defect == 'absent':
        data['evidence'] = []
    elif defect == 'check_only':
        data['scope'] = 'ordinary-code'  # Cannot relabel the installed outcome as code-only.
        data['evidence'][0]['kind'] = 'check'
    elif defect == 'missing_file':
        Path(data['evidence'][0]['path']).unlink()
    elif defect == 'changed_bytes':
        Path(data['evidence'][0]['path']).write_text('changed')
    else:
        data['evidence'][0]['observed'] = ['1553253666172174388']  # Actual wrong/404 receipt ID.
    path.write_text(json.dumps(data))
    with pytest.raises(SystemExit, match='evidence|acceptance'):
        run_cli(monkeypatch, path)
    assert writes == []


@pytest.mark.parametrize('acceptance', [
    'Release the candidate to production after independent approval.',
    'Deploy the reviewed build and verify live readback.',
    'Accept outward design with independent Fable review.',
    'Mark positive REAL earnings only after independent financial acceptance.',
    'Send the approved third-party message to providers.',
])
def test_protected_acceptance_not_covered_by_ordinary_path(completion, monkeypatch, acceptance):
    target, path, data, writes, _, _ = completion
    target['description'] = '## Acceptance\n\n* ' + acceptance
    data['description_sha256'] = digest(target['description'])
    path.write_text(json.dumps(data))
    with pytest.raises(SystemExit, match='protected'):
        run_cli(monkeypatch, path)
    assert writes == []


@pytest.mark.parametrize('defect', ['stale_acceptance', 'no_acceptance', 'wrong_issue', 'unmerged',
                                    'wrong_head', 'wrong_merge', 'failed_ci', 'missing_ci', 'wrong_ci_url',
                                    'worker_review', 'wrong_review_issue', 'changed_review', 'protected_effect'])
def test_evidence_and_scope_binding_fail_closed(completion, monkeypatch, defect):
    target, path, data, writes, remote, review = completion
    if defect == 'stale_acceptance':
        target['description'] += '\nAdditional acceptance'
    elif defect == 'no_acceptance':
        target['description'] = ''
        data['description_sha256'] = digest('')
    elif defect == 'wrong_issue':
        data['issue'] = 'GLP-319'
    elif defect == 'unmerged':
        remote['state'] = 'OPEN'
    elif defect == 'wrong_head':
        remote['headRefOid'] = 'a' * 40
    elif defect == 'wrong_merge':
        remote['mergeCommit']['oid'] = 'b' * 40
    elif defect == 'failed_ci':
        remote['statusCheckRollup'][0]['conclusion'] = 'FAILURE'
    elif defect == 'missing_ci':
        remote['statusCheckRollup'] = []
    elif defect == 'wrong_ci_url':
        data['pr']['checks'][0]['url'] += '9'
    elif defect == 'worker_review':
        review['user']['id'] = '858c38f1-aa44-4ff2-b2b7-04ea63fcf1e7'
    elif defect == 'wrong_review_issue':
        review['issue']['id'] = 'different-issue'
    elif defect == 'changed_review':
        review['body'] += ' Not accepted.'
    else:
        data['protected_effects'] = ['release']
    path.write_text(json.dumps(data))
    with pytest.raises(SystemExit):
        run_cli(monkeypatch, path)
    assert writes == []


def test_read_only_check_exercises_contract_without_effect(completion, monkeypatch, capsys):
    _, path, _, writes, _, _ = completion
    run_cli(monkeypatch, path, '--check-only')
    assert json.loads(capsys.readouterr().out)['validated'] is True
    assert writes == []


def test_transition_done_uses_same_contract(completion, monkeypatch):
    _, path, _, writes, _, _ = completion
    monkeypatch.setattr(sys, 'argv', ['linear_orchestrator.py', 'transition', 'GLP-60', 'Done', str(path), '--ordinary'])
    lo.main()
    assert [w[0] for w in writes] == ['comment', 'state']


def test_legacy_brief_and_validator_still_required(completion, monkeypatch):
    target, path, _, writes, _, _ = completion
    with pytest.raises(SystemExit, match='brief'):
        lo.pass_issue(target, path, lo.states())
    brief = path.parent / 'existing.md'
    brief.write_text('existing legacy scope')
    def reject(*args, **kwargs):
        raise SystemExit('legacy protected validator refusal')
    monkeypatch.setattr(lo, '_lint_result', reject)
    with pytest.raises(SystemExit, match='legacy protected validator refusal'):
        lo.pass_issue(target, path, lo.states(), brief)
    assert writes == []


def test_uncertain_receipt_stops_before_done(completion, monkeypatch):
    _, path, _, writes, _, _ = completion
    monkeypatch.setattr(lo, 'comment', lambda *args: {'success': False})
    with pytest.raises(SystemExit, match='comment unconfirmed'):
        run_cli(monkeypatch, path)
    assert writes == []


def test_check_only_cannot_fall_through_to_other_mutations(completion, monkeypatch):
    _, path, _, writes, _, _ = completion
    monkeypatch.setattr(sys, 'argv', ['linear_orchestrator.py', 'transition', 'GLP-60', 'In Progress', str(path), '--check-only'])
    with pytest.raises(SystemExit, match='completion only'):
        lo.main()
    assert writes == []


def test_query_projects_scope_title_and_acceptance():
    assert {'title', 'description'} <= set(lo.ISSUE_FIELDS.split())


def test_ordinary_does_not_refresh_pins_even_with_changed_files(completion, monkeypatch):
    _, path, data, writes, _, _ = completion
    data['changed_files'] = ['scripts/glp_earnings_notice.py']
    path.write_text(json.dumps(data))
    run_cli(monkeypatch, path)
    assert [w[0] for w in writes] == ['comment', 'state']
