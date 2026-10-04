"""One owning offline regression. No fixture is review or production authority."""
import argparse
import ast
import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
from unittest import mock

import pytest
import test_glp43_current_release as legacy

c = legacy.c

ROOT = Path(__file__).resolve().parents[1]


def test_genuine_fable_current_release_and_exact_produced_receiving_path():
    fixture = legacy.CurrentReleaseTests(methodName='runTest')
    fixture.setUp()
    try:
        fixture.prepare()
        fixture.approve_fixture()
        legacy_args = argparse.Namespace(approval=str(fixture.approval), expected_approval_sha256=c.sha_file(fixture.approval),
            expected_prepared_manifest_sha256=c.sha_file(fixture.prepared / 'PREPARED.json'), authorize_production=True)
        with mock.patch.object(c, 'CURRENT_LEGACY_ASTRA_REVIEWS', frozenset()):
            with pytest.raises(c.GateError, match='current_legacy_astra_review_not_retained'):
                c.current_approval(legacy_args, fixture.prepared_data)
        spec = importlib.util.spec_from_file_location('offline_fable_guard', ROOT / 'ventures/boring-ai/holistic-machine/scripts/bridge_authority_guard.py')
        assert spec is not None and spec.loader is not None
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        authority_name = 'ops/linear/approvals/OPERATOR-DEPLOY-COPY-FDA-QA-20261003.md'
        authority = fixture.root / authority_name
        authority.parent.mkdir(parents=True, exist_ok=True)
        authority.write_text('OFFLINE FIXTURE: designated Fable; not operator authority')
        packet = fixture.root / 'COMPILED-REVIEW-REQUEST.json'
        packet.write_bytes(c.json_bytes({'schema': 'glp44-current-release-review-request-v1',
            'subject': fixture.review['subject'],
            'reused_technical_acceptance': fixture.review['reused_technical_acceptance'],
            'authority': authority_name}))
        subject_id = 'glp:GLP-44:current-release:' + fixture.head + ':' + c.sha_file(fixture.prepared / 'PREPARED.json')
        request_id, acceptance_id, fable_id = '1556000000000000001', '1556000000000000002', '1556000000000000003'
        messages = {}
        issue_root = fixture.root / 'offline-guard-issued'
        with mock.patch.object(guard, 'BRIDGE_ISSUE_ROOT', issue_root), \
             mock.patch.object(guard, 'REVIEW_ARTIFACT_ROOT', fixture.root), \
             mock.patch.object(guard, '_configured_fable_author_id', return_value=fable_id), \
             mock.patch.object(guard, '_fetch_discord_message', side_effect=lambda channel, mid: messages[mid]), \
             mock.patch.object(c, 'current_fable_guard', return_value=guard, create=True), \
             mock.patch.object(c, 'CURRENT_FABLE_AUTHORITY', {authority_name: c.sha_file(authority)}, create=True):
            issue_path, issue = guard.issue_request(subject_id=subject_id, tuple_sha256=c.sha_file(packet), review_artifact_path=packet)
            messages[request_id] = {'id': request_id, 'channel_id': guard.FIXED_BRIDGE_CHANNEL_ID,
                'author': {'id': guard.KAEL_SHARED_BOT_AUTHOR_ID}, 'content': issue['request_content'],
                'timestamp': '2099-01-01T00:00:00Z', 'edited_timestamp': None}
            guard.bind_request_issue(issue_record_path=issue_path, request_message_id=request_id)
            messages[acceptance_id] = {'id': acceptance_id, 'channel_id': guard.FIXED_BRIDGE_CHANNEL_ID,
                'author': {'id': fable_id}, 'timestamp': '2099-01-01T00:05:00Z', 'edited_timestamp': None,
                'message_reference': {'message_id': request_id},
                'content': '\n'.join([subject_id, c.sha_file(packet), issue['challenge'], issue['issue_id'],
                    guard.EXACT_ACCEPTANCE_DECISION_LINE, *guard.REQUIRED_AUTHORITY_MARKERS,
                    guard.INSPECTION_NOTE_PREFIX + ' Offline fixture inspected exact compiled bytes, predecessor, runtime holds and release evidence; no live acceptance.'])}
            fixture.review.update(schema='glp44-designated-fable-release-review-v1',
                reviewer={'name': 'Fable', 'author_id': fable_id, 'authority': authority_name,
                          'original_target_authority': list(fixture.authorities)[0]},
                review_artifact={'path': str(packet), 'sha256': c.sha_file(packet)},
                bridge_acceptance={'request_message_id': request_id, 'acceptance_message_id': acceptance_id,
                                   'issue_record_path': str(issue_path)})
            fixture.save_review()
            args = argparse.Namespace(approval=str(fixture.approval), expected_approval_sha256=c.sha_file(fixture.approval),
                expected_prepared_manifest_sha256=c.sha_file(fixture.prepared / 'PREPARED.json'), authorize_production=True)
            # RED on the predecessor's Astra-only schema; GREEN uses real guard validation over offline readbacks.
            c.current_approval(args, fixture.prepared_data)
            baseline = copy.deepcopy(fixture.review)
            acceptance = copy.deepcopy(messages[acceptance_id])
            for mutation in ('author', 'head', 'artifact', 'verdict', 'missing_genuine_verdict', 'wrong_issue', 'edited', 'self_authored_json'):
                fixture.review = copy.deepcopy(baseline)
                messages[acceptance_id] = copy.deepcopy(acceptance)
                if mutation == 'author': messages[acceptance_id]['author']['id'] = guard.KAEL_SHARED_BOT_AUTHOR_ID
                elif mutation == 'head': fixture.review['subject']['head'] = 'f' * 40
                elif mutation == 'artifact': fixture.review['review_artifact']['sha256'] = 'f' * 64
                elif mutation == 'verdict': fixture.review['verdict'] = 'CHANGES_REQUIRED'
                elif mutation == 'missing_genuine_verdict': messages[acceptance_id]['content'] = 'APPROVE — offline invented local JSON is not Fable acceptance'
                elif mutation == 'wrong_issue': fixture.review['bridge_acceptance']['issue_record_path'] = str(fixture.root / 'invented.json')
                elif mutation == 'edited': messages[acceptance_id]['edited_timestamp'] = '2099-01-01T00:06:00Z'
                else: fixture.review.pop('bridge_acceptance')
                fixture.save_review(); args.expected_approval_sha256 = c.sha_file(fixture.approval)
                with pytest.raises((c.GateError, OSError, ValueError, KeyError)):
                    c.current_approval(args, fixture.prepared_data)
                assert fixture.deploy_calls == []
            fixture.review = baseline; messages[acceptance_id] = acceptance; fixture.save_review()
            assert fixture.publish() == 0
            deploy_count = len(fixture.deploy_calls)
            assert fixture.publish() == 1  # Consumed journal cannot authorize a replay.
            assert len(fixture.deploy_calls) == deploy_count == 1
            terminal = fixture.root / 'receipts' / ('current-release-' + fixture.head) / 'CURRENT-RECEIPT.json'
            receipt = json.loads(terminal.read_bytes())
            assert receipt['binding']['approval_path'] == str(fixture.approval)
            source_path = ROOT / 'scripts/glp_price_index_weekly.py'
            tree = ast.parse(source_path.read_text())
            names = {'_native_current_deploy_binding', '_read_bound_regular_file', '_stat_signature'}
            namespace = {'Path': Path, 'Any': object, 'argparse': argparse, 'base64': base64, 'json': json,
                         'os': os, 'stat': stat, 'sha_bytes': c.sha_bytes, 'CURRENT_BASE': fixture.pointer}
            exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names], type_ignores=[]), str(source_path), 'exec'), namespace)
            receiver = namespace['_native_current_deploy_binding']
            call = (json.loads(fixture.pointer.read_bytes()), fixture.prepared, fixture.prepared_data,
                    (fixture.prepared / 'PREPARED.json').read_bytes(), terminal.parent, c)
            assert not (fixture.prepared.parent / 'astra-final-review/RESPONSE.md').exists()
            before = {p: p.read_bytes() for p in (terminal, fixture.approval, fixture.pointer)}
            assert str(fixture.approval) in receiver(*call)
            assert before == {p: p.read_bytes() for p in before}
            # No guessed Fable filename fallback: the produced path is obligatory and exact.
            for mutation in ('missing_path', 'relative_path', 'wrong_hash', 'wrong_effect'):
                bad = copy.deepcopy(receipt)
                if mutation == 'missing_path': bad['binding'].pop('approval_path')
                elif mutation == 'relative_path': bad['binding']['approval_path'] = 'review.json'
                elif mutation == 'wrong_hash': bad['binding']['approval_sha256'] = 'f' * 64
                else: bad['effects']['pointer_atomic_cas'] = 0
                terminal.write_bytes(c.json_bytes(bad))
                with pytest.raises((RuntimeError, c.GateError)): receiver(*call)
            terminal.write_bytes(before[terminal])
    finally:
        fixture.doCleanups()
