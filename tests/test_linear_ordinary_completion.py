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
        review['user']['id'] = 'worker-not-coordinator'
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


@pytest.mark.parametrize('control', [
    'ops', 'personal', 'unauthorized_author', 'missing_author', 'protected_scope',
    'protected_effect', 'wrong_issue', 'wrong_comment_id', 'wrong_comment_issue',
    'archived_comment', 'changed_body', 'stale_description', 'unmerged',
    'wrong_head', 'wrong_merge', 'failed_ci', 'wrong_ci_url', 'unbound_merge',
    'missing_evidence', 'independent_gate',
])
def test_glp329_ordinary_coordinator_identity_keeps_bindings_and_protected_gates(
        completion, monkeypatch, capsys, control):
    """GLP-329 custody identifiers with synthetic body/evidence and offline transports.

    This exercises check-only, never changes the genuine receipt/comment, and
    does not claim live acceptance or independent approval of this source repair.
    """
    target, path, data, writes, remote, review = completion
    target.update(id='381a8864-dd9e-4a11-86f3-2c85f9a892ff', identifier='GLP-329',
                  title='Repair source-only preview',
                  description='## Acceptance\n\n* Source-only preview preserves existing rows.')
    data.update(issue=target['identifier'], scope='ordinary-code',
                description_sha256=digest(target['description']))
    data['pr'].update(url='https://github.com/izaiahat/glp-business/pull/75',
                      head_sha='234bd75c69d25c1c2aec141d3cd828894e3396a0',
                      merge_sha='fc25cb61bbd78a96e5cb8dc128a703da9bcf97b7')
    remote.update(headRefOid=data['pr']['head_sha'],
                  mergeCommit={'oid': data['pr']['merge_sha']})
    data['review']['comment_id'] = 'ebd5b78b-7f97-4501-98fe-0342b4e5ef5d'
    review.update(id=data['review']['comment_id'], issue={'id': target['id']},
                  user={'id': '858c38f1-aa44-4ff2-b2b7-04ea63fcf1e7'},
                  body='Coordinator acceptance verified: source fixture at ' + data['pr']['merge_sha'])
    data['evidence'][0]['kind'] = 'check'
    data['review']['body_sha256'] = digest(review['body'])
    assert lo.OPERATOR_USER_ID == '86ceaadc-3c24-4810-ac3a-985cfc561a05'

    if control == 'personal':
        review['user']['id'] = lo.OPERATOR_USER_ID
    elif control == 'unauthorized_author':
        review['user']['id'] = 'worker-not-coordinator'
    elif control == 'missing_author':
        review['user'] = None
    elif control == 'protected_scope':
        target['description'] += '\n* Deploy after independent Fable review.'
        data['description_sha256'] = digest(target['description'])
    elif control == 'protected_effect':
        data['protected_effects'] = ['release']
    elif control == 'wrong_issue':
        data['issue'] = 'GLP-999'
    elif control == 'wrong_comment_id':
        review['id'] = 'another-comment'
    elif control == 'wrong_comment_issue':
        review['issue']['id'] = 'another-issue'
    elif control == 'archived_comment':
        review['archivedAt'] = '2026-09-30T00:00:00Z'
    elif control == 'changed_body':
        review['body'] += ' Changed.'
    elif control == 'stale_description':
        target['description'] += '\n* Additional acceptance.'
    elif control == 'unmerged':
        remote['state'] = 'OPEN'
    elif control == 'wrong_head':
        remote['headRefOid'] = 'a' * 40
    elif control == 'wrong_merge':
        remote['mergeCommit']['oid'] = 'b' * 40
    elif control == 'failed_ci':
        remote['statusCheckRollup'][0]['conclusion'] = 'FAILURE'
    elif control == 'wrong_ci_url':
        data['pr']['checks'][0]['url'] += '9'
    elif control == 'unbound_merge':
        review['body'] = 'Coordinator acceptance verified: source fixture'
        data['review']['body_sha256'] = digest(review['body'])
    elif control == 'missing_evidence':
        data['evidence'] = []
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    if control in ('ops', 'personal'):
        lo.pass_issue(target, path, {}, ordinary=True, check_only=True)
        assert json.loads(capsys.readouterr().out)['validated'] is True
    elif control == 'independent_gate':
        brief = path.parent / 'existing-brief.md'
        brief.write_text('existing protected scope')
        def reject(*args, **kwargs):
            raise SystemExit('independent protected acceptance still required')
        monkeypatch.setattr(lo, '_lint_result', reject)
        with pytest.raises(SystemExit, match='independent protected acceptance still required'):
            lo.pass_issue(target, path, {}, brief=brief, check_only=True)
    else:
        with pytest.raises(SystemExit, match='ordinary completion refused:'):
            lo.pass_issue(target, path, {}, ordinary=True, check_only=True)
    assert path.read_bytes() == before
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


@pytest.fixture
def readonly_completion(completion, monkeypatch):
    """Sanitized two-session portal proof; never contacts a portal or Linear."""
    target, path, data, writes, remote, review = completion
    target.update(title='Verify fresh portal login', description='''## Acceptance

* Fresh origin-bound vault login → exact publisher identity and read-only portal access read back.
* Second fresh session → authenticated access without fixed CDP/profile; device challenge respected.

PR: pending (non-code effect: link receipt).
Skills: credential-boundaries, headless-portal-apply-sessions.''')
    data.update(scope='ordinary-readonly-runtime', description_sha256=digest(target['description']))
    del data['pr']
    data['evidence'] = []
    for name in ('first-session', 'second-session'):
        artifact = path.parent / (name + '.json')
        artifact.write_text(json.dumps({'session': name, 'publisher': 'test-publisher-uuid',
                                        'assertions': 'PASS', 'effects': {'sends': 0}}))
        data['evidence'].append({'kind': 'runtime', 'path': str(artifact),
                                 'sha256': lo.sha(artifact), 'observed': [name, 'test-publisher-uuid']})
    review['body'] = ('Acceptance verified by parent: first fresh-session receipt in reports/first-session.json '
                      'plus second in reports/second-session.json. Exact publisher test-publisher-uuid; '
                      'both fresh sessions respected device verification. No applications/sends. '
                      'Non-code receipt satisfies both acceptance checks.')
    # Explicit version binding is synthetic fixture authority, not a live comment edit.
    review['body'] += '\nAccepted description SHA256: ' + data['description_sha256']
    data['review']['body_sha256'] = digest(review['body'])
    path.write_text(json.dumps(data))
    import subprocess
    monkeypatch.setattr(subprocess, 'check_output', lambda *a, **k: pytest.fail('non-code must not invent PR/CI'))
    return target, path, data, writes, remote, review


@pytest.mark.parametrize('check_only', [True, False])
def test_readonly_runtime_without_pr_or_brief_uses_single_writer(readonly_completion, monkeypatch, capsys, check_only):
    _, path, data, writes, _, _ = readonly_completion
    assert 'pr' not in data
    run_cli(monkeypatch, path, *(['--check-only'] if check_only else []))
    if check_only:
        assert json.loads(capsys.readouterr().out)['validated'] is True
        assert writes == []
    else:
        assert [w[0] for w in writes] == ['comment', 'state']
        receipt = json.loads(writes[0][2].split('```json')[1].split('```')[0])
        assert receipt == {'issue_id': ISSUE_ID, 'outcome': 'PASS', 'result_path': str(path),
                           'result_sha256': lo.sha(path)}
        assert writes[1] == ('state', ISSUE_ID, 'done')


@pytest.mark.parametrize('prefix', [
    '### Final check\n',
    '#### Final check\n',
    'Skills: credential-boundaries, headless-portal-apply-sessions.\n### Final check\n',
    'Owner: lane:worker\n### Final check\n',
    'PR: pending\n### Final check\n',
    'Source issue: GLP-60\n### Final check\n',
    'Skills: headless-portal-apply-sessions; ',
])
def test_protected_requirement_under_acceptance_subheading_must_refuse(
        readonly_completion, monkeypatch, capsys, prefix):
    target, path, data, writes, _, review = readonly_completion
    run_cli(monkeypatch, path, '--check-only')
    assert json.loads(capsys.readouterr().out)['validated'] is True
    prior_comment = dict(review)
    target['description'] = target['description'].replace(
        '\nPR:', '\n' + prefix + '* Submit application to network after read-only check.\n\nPR:')
    data['description_sha256'] = digest(target['description'])
    path.write_text(json.dumps(data))
    assert review == prior_comment
    try:
        with pytest.raises(SystemExit, match='protected send/application'):
            run_cli(monkeypatch, path, '--check-only')
    finally:
        assert writes == []


@pytest.mark.parametrize('binding,expand_scope', [
    ('original', True), ('absent', True), ('absent', False), ('duplicate', False),
])
def test_coordinator_acceptance_must_bind_description_version(
        readonly_completion, monkeypatch, capsys, binding, expand_scope):
    target, path, data, writes, _, review = readonly_completion
    run_cli(monkeypatch, path, '--check-only')
    assert json.loads(capsys.readouterr().out)['validated'] is True
    if binding == 'absent':
        review['body'] = review['body'].split('\nAccepted description SHA256:')[0]
    elif binding == 'duplicate':
        review['body'] += '\nAccepted description SHA256: ' + data['description_sha256']
    data['review']['body_sha256'] = digest(review['body'])
    prior_comment = dict(review)
    prior_evidence = json.dumps(data['evidence'])
    if expand_scope:
        target['description'] = target['description'].replace(
            '\nPR:', '\n* Third fresh job session required.\n\nPR:')
        # Refreshing a caller-controlled hash is not fresh coordinator acceptance.
        data['description_sha256'] = digest(target['description'])
    path.write_text(json.dumps(data))
    assert review == prior_comment
    assert json.dumps(data['evidence']) == prior_evidence
    try:
        with pytest.raises(SystemExit, match='coordinator accepted description binding'):
            run_cli(monkeypatch, path, '--check-only')
    finally:
        assert writes == []


@pytest.mark.parametrize('defect', [
    'stale_description', 'wrong_issue', 'wrong_team', 'worker_review', 'wrong_review_issue',
    'archived_review', 'changed_review', 'not_acceptance', 'missing_evidence', 'changed_evidence',
    'wrong_observed', 'unbound_observed', 'unbound_path', 'path_prefix', 'check_evidence', 'no_readonly_scope',
    'claimed_pr', 'changed_source', 'protected_effect', 'publish', 'published', 'publishing',
    'publication', 'publisher_release', 'deploy', 'design', 'financial', 'send', 'submit', 'apply',
])
def test_readonly_runtime_refuses_wrong_stale_or_protected_proof(readonly_completion, monkeypatch, defect):
    target, path, data, writes, _, review = readonly_completion
    if defect == 'stale_description':
        target['description'] += '\nNew acceptance'
    elif defect == 'wrong_issue':
        data['issue'] = 'GLP-999'
    elif defect == 'wrong_team':
        target['team']['id'] = 'wrong-team'
    elif defect == 'worker_review':
        review['user']['id'] = 'worker-not-coordinator'
    elif defect == 'wrong_review_issue':
        review['issue']['id'] = 'another-issue'
    elif defect == 'archived_review':
        review['archivedAt'] = '2026-09-27T02:00:00Z'
    elif defect == 'changed_review':
        review['body'] += ' Changed.'
    elif defect == 'not_acceptance':
        review['body'] = review['body'].replace('Acceptance verified by parent:', 'Work reported by worker:')
        data['review']['body_sha256'] = digest(review['body'])
    elif defect == 'missing_evidence':
        Path(data['evidence'][1]['path']).unlink()
    elif defect == 'changed_evidence':
        Path(data['evidence'][1]['path']).write_text('stale bytes')
    elif defect == 'wrong_observed':
        data['evidence'][1]['observed'] = ['different-publisher']
    elif defect == 'unbound_observed':
        data['evidence'][1]['observed'] = ['sends']  # In file but not acceptance? Remove from body too.
        review['body'] = review['body'].replace('applications/sends', 'effects')
        data['review']['body_sha256'] = digest(review['body'])
    elif defect == 'unbound_path':
        original = Path(data['evidence'][1]['path'])
        unrelated = path.parent / 'unreviewed-session.json'
        unrelated.write_bytes(original.read_bytes())
        data['evidence'][1]['path'] = str(unrelated)
    elif defect == 'path_prefix':
        review['body'] = review['body'].replace('reports/first-session.json', 'reports/first-session.json.backup')
        data['review']['body_sha256'] = digest(review['body'])
    elif defect == 'check_evidence':
        data['evidence'][1]['kind'] = 'check'
    elif defect == 'claimed_pr':
        data['pr'] = {'url': PR}
    elif defect == 'changed_source':
        data['changed_files'] = ['scripts/source.py']
    elif defect == 'protected_effect':
        data['protected_effects'] = ['application']
    else:
        acceptance = {
            'no_readonly_scope': 'Verify publisher identity in the portal.',
            'publish': 'Publish the comparison from the read-only publisher identity.',
            'published': 'Confirm published article from read-only publisher identity.',
            'publishing': 'Enable publishing after read-only identity check.',
            'publication': 'Authorize publication after read-only publisher check.',
            'publisher_release': 'Read-only publisher identity then release content.',
            'deploy': 'Deploy after read-only identity check.',
            'design': 'Accept outward design after read-only identity check.',
            'financial': 'Confirm REAL commissions with read-only portal access.',
            'send': 'Read-only publisher check then send application.',
            'submit': 'Read-only publisher check then submit application.',
            'apply': 'Read-only publisher check then apply to network.',
        }[defect]
        target['description'] = '## Acceptance\n\n* ' + acceptance
        data['description_sha256'] = digest(target['description'])
    path.write_text(json.dumps(data))
    with pytest.raises(SystemExit, match='ordinary completion'):
        run_cli(monkeypatch, path)
    assert writes == []
