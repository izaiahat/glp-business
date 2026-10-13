"""linear_close builds a typed ordinary RESULT from a worker RESULT; Linear and gh are offline fakes."""
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import linear_close as lc  # noqa: E402

lo = lc.lo


def test_reply_artifact_is_not_a_third_party_send():
    # Real L2b blocker: file evidence is not the effect of replying to a customer.
    for text in ('REPLY.md', 'dot ticket REPLY.md', 'reply.md',
                 'reports/REPLY-provider.json', 'REPLY-customer.md',
                 'Read scripts/send-provider.py and reports/spend.json.',
                 'Read reports/ops/dot-tickets/rook-ticket/REPLY.md into the fleet inbox.',
                 'Rook/Finch dispatch + reply consumer: ticket in, REPLY.md out',
                 'Read sponsor REPLY.md evidence; no action requested'):
        assert lo.protected_effect(text) is None


def test_negative_duplicate_spend_is_not_a_financial_write():
    # L6b's exact negative acceptance scope, not approval to spend.
    assert lo.protected_effect('avoid duplicate finding/publication/spend') is None


@pytest.mark.parametrize('text,effect', [
    ('Reply to the customer with the approved offer.', 'third-party-send'),
    ('Reply to jane@example.com with the offer.', 'third-party-send'),
    ('Sent a reply yesterday; evidence REPLY.md.', 'third-party-send'),
    ('Send a reply after approval.', 'third-party-send'),
    ('Record the sponsor reply in evidence.', 'third-party-send'),
    ('Send the approved message to providers; retain REPLY.md.', 'third-party-send'),
    ('`Send REPLY.md to the customer`', 'third-party-send'),
    ('Send reports/REPLY.md to subscribers.', 'third-party-send'),
    ('Email the onboarding sequence to subscribers.', 'third-party-send'),
    ('Submit the application to the partner network.', 'third-party-send'),
    ('Outreach pilot with a send.', 'third-party-send'),
    ('Send occurs after approval.', 'third-party-send'),
    ('avoid duplicate finding/publication/spend; spend $50 on ads.', 'financial-write'),
    ('Spend $50; avoid duplicate finding/publication/spend.', 'financial-write'),
    ('avoid duplicate finding/publication/spend and charge the invoice.', 'financial-write'),
    ('Make the payout to the network.', 'financial-write'),
    ('Record REAL earnings.', 'financial-write'),
    ('Publish the new health claims page; retain REPLY.md.', 'health-claim-publish'),
    ('avoid duplicate finding/publication/spend; publish new health claims.', 'health-claim-publish'),
])
def test_real_protected_effects_survive_artifacts_and_negative_guard(text, effect):
    assert lo.protected_effect(text) == effect


def test_genuine_send_refused_by_linear_close_even_with_reply_artifact(tmp_path):
    worker = tmp_path / 'RESULT.json'
    worker.write_text(json.dumps({'status': 'DONE_READY_TO_CLOSE', 'issue': 'GLP-355'}))
    issue = {'title': 'Partner outreach', 'description':
             '## Acceptance\nSend the approved reply to sponsors; keep REPLY.md evidence.'}
    with pytest.raises(lc.Refused, match='protected effect third-party-send'):
        lc.build('GLP-355', worker, issue)


@pytest.mark.parametrize('text', [
    'Send REPLY.md to jane@example.com',
    'Email reports/REPLY.md to jane.doe@example.com',
    'Send the reply after approval.',
])
def test_explicit_send_survives_reply_artifact_masking(tmp_path, text):
    assert lo.protected_effect(text) == 'third-party-send'
    worker = tmp_path / 'RESULT.json'
    worker.write_text(json.dumps({'status': 'DONE_READY_TO_CLOSE', 'issue': 'GLP-355'}))
    issue = {'title': 'Artifact handling', 'description': '## Acceptance\n' + text}
    with pytest.raises(lc.Refused, match='protected effect third-party-send'):
        lc.build('GLP-355', worker, issue)


def test_duplicate_constraint_cannot_hide_actual_financial_write(tmp_path):
    text = 'avoid duplicate finding/publication/spend; charge $50; keep REPLY.md'
    assert lo.protected_effect(text) == 'financial-write'
    worker = tmp_path / 'RESULT.json'
    worker.write_text(json.dumps({'status': 'DONE_READY_TO_CLOSE', 'issue': 'GLP-355'}))
    with pytest.raises(lc.Refused, match='protected effect financial-write'):
        lc.build('GLP-355', worker, {'title': 'Settlement', 'description': '## Acceptance\n' + text})


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


@pytest.mark.parametrize('file_evidence', ['', ' Read dot ticket REPLY.md from the fleet inbox.'])
def test_check_only_runs_real_gate_without_linear_or_filesystem_writes(tmp_path, monkeypatch, capsys, file_evidence):
    reports = tmp_path / 'reports'
    receipt = reports / 'ops/collector/receipt.json'
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({'provider_rows': 58}))
    worker = receipt.parent / 'RESULT.json'
    worker.write_text(json.dumps({'status': 'DONE_READY_TO_CLOSE', 'issue': 'GLP-44',
        'live_run': 'Read-only snapshot provider_rows=58',
        'receipt': 'reports/ops/collector/receipt.json'}))
    target = {'id': ISSUE_ID, 'identifier': 'GLP-44', 'title': 'Collector snapshot',
        'description': '## Acceptance\n\nRead-only snapshot of provider rows.' + file_evidence + '\n',
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


@pytest.mark.parametrize('fault', [None, 'installed_bytes', 'unrelated_ancestry', 'ci_head_blob',
                                  'unreviewed_path', 'failed_ci'])
def test_zero_diff_source_carry_check_only(tmp_path, monkeypatch, capsys, fault):
    # GLP-350: PR478 changes no files, but carries merged PR460's installed source.
    # Its merge tree may also contain later main changes; the selected blobs must
    # still equal the original source and the exact head tested by hosted CI.
    rel = 'scripts/glp_price_freshness.py'
    original = 'https://github.com/izaiahat/glp-business/pull/460'
    carry = 'https://github.com/izaiahat/glp-business/pull/478'
    source_head, source_merge = 'c' * 40, 'd' * 40
    raw = b'reviewed price producer\n'
    blob = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
    source = tmp_path / rel
    source.parent.mkdir()
    source.write_bytes(b'tampered' if fault == 'installed_bytes' else raw)
    reports = tmp_path / 'reports'
    reports.mkdir()
    receipt = reports / 'receipt.json'
    receipt.write_text(json.dumps({'provider_rows': 54, 'cost': None}))
    worker = reports / 'RESULT.json'
    worker.write_text(json.dumps({'issue': 'GLP-350', 'status': 'DONE_READY_TO_CLOSE',
        'pr': carry, 'install': {'source_pr': original, 'paths': [rel]},
        'live_run': 'provider_rows=54', 'receipt': 'reports/receipt.json'}))
    target = {'id': ISSUE_ID, 'identifier': 'GLP-350', 'title': 'Price source receiving',
        'description': '## Acceptance\nInstalled price producer has a registered receipt.',
        'team': {'id': lo.TEAM, 'key': 'GLP'}, 'state': {'name': 'In Progress'}}
    remote = {'state': 'MERGED', 'headRefOid': HEAD, 'mergeCommit': {'oid': MERGE},
        'files': [], 'statusCheckRollup': [{'name': 'python-tests', 'detailsUrl': CI,
        'status': 'COMPLETED', 'conclusion': 'FAILURE' if fault == 'failed_ci' else 'SUCCESS'}]}
    original_remote = {'state': 'MERGED', 'headRefOid': source_head,
        'mergeCommit': {'oid': source_merge},
        'files': [{'path': 'scripts/other.py' if fault == 'unreviewed_path' else rel}]}
    def gh(url):
        return {carry: remote, original: original_remote}[url]
    def check_output(argv, **kwargs):
        if argv[:3] == ['gh', 'pr', 'view']:
            return json.dumps(gh(argv[3]))
        assert argv[:2] == ['gh', 'api']
        endpoint = argv[2]
        prefix = 'repos/izaiahat/glp-business/'
        if endpoint.startswith(prefix + 'compare/'):
            base, head = endpoint.split('/')[-1].split('...')
            assert (base, head) in ((source_merge, HEAD), (HEAD, MERGE))
            return json.dumps({'merge_base_commit': {'sha': 'e' * 40
                if fault == 'unrelated_ancestry' else base}})
        assert endpoint.startswith(prefix + 'contents/' + rel + '?ref=')
        ref = endpoint.split('=')[-1]
        assert ref in (source_head, HEAD, MERGE)
        return json.dumps({'type': 'file', 'sha': 'f' * 40
            if fault == 'ci_head_blob' and ref == HEAD else blob,
            'encoding': 'base64', 'content': base64.b64encode(raw).decode()})
    def gql(query, variables=None):
        assert query == 'query{viewer{id}}'
        return {'viewer': {'id': lo.OPERATOR_USER_ID}}
    def forbidden(*args, **kwargs):
        raise AssertionError('check-only attempted a write')
    monkeypatch.setattr(lc, 'ROOT', tmp_path)
    monkeypatch.setattr(lc, 'REPORTS', reports)
    monkeypatch.setattr(lo, '__file__', str(tmp_path / 'scripts/linear_orchestrator.py'))
    monkeypatch.setattr(lo, 'issue', lambda ident: target)
    monkeypatch.setattr(lo, 'gql', gql)
    monkeypatch.setattr(lo, 'comment', forbidden)
    monkeypatch.setattr(lc, 'write', forbidden)
    monkeypatch.setattr(lc, 'orchestrate', forbidden)
    monkeypatch.setattr(subprocess, 'check_output', check_output)
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert lc.main(['GLP-350', str(worker), '--check-only']) == (3 if fault else 0)
    output = capsys.readouterr().out
    expected = {None: 'GLP-350 PASS', 'installed_bytes': 'installed Git blob',
        'unrelated_ancestry': 'merged source ancestry', 'ci_head_blob': 'installed Git blob',
        'unreviewed_path': 'source PR did not change', 'failed_ci': 'exact-head CI'}
    assert expected[fault] in output
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before
