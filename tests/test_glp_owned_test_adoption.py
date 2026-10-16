"""Offline fixtures; never settings changes, deployment, enrollment or mail."""
import copy
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp_owned_test_adoption as adoption
import glp_deploy_glp34_authority_v1 as carrier


def objects(mode='held'):
    env = {'NEWSLETTER_CAPTURE_MODE': {'type': 'plain_text', 'value': mode},
           'RESEND_API_KEY': {'type': 'secret_text'},
           'EMAIL_RUNTIME_MODE': {'type': 'secret_text'}}
    config = {'env_vars': env, 'd1_databases': {'GLP_DB': {'id': adoption.D1}},
              'compatibility_date': '2026-07-01', 'compatibility_flags': [],
              'usage_model': 'standard', 'build_image_major_version': 3,
              'fail_open': False, 'always_use_latest_compatibility_date': False}
    dep = {'id': adoption.DEPLOYMENT, 'project_id': adoption.PROJECT_ID,
           'project_name': carrier.PROJECT, 'environment': 'production',
           'latest_stage': {'status': 'success'}, 'uses_functions': True,
           'deployment_trigger': {'metadata': {'branch': 'main', 'commit_hash': adoption.HEAD}},
           **{k: copy.deepcopy(config[k]) for k in adoption.DEPLOYED_KEYS}}
    project = {'id': adoption.PROJECT_ID, 'name': carrier.PROJECT, 'uses_functions': True,
               'subdomain': carrier.PROJECT + '.pages.dev', 'production_branch': 'main',
               'deployment_configs': {'production': config, 'preview': {'env_vars': {}}},
               'canonical_deployment': copy.deepcopy(dep), 'build_config': {}, 'source': None}
    return project, dep


def test_patch_is_one_plaintext_key_not_secret_roundtrip():
    assert adoption.patch_body() == {'deployment_configs': {'production': {'env_vars': {
        'NEWSLETTER_CAPTURE_MODE': {'type': 'plain_text', 'value': 'owned_test'}}}}}


def test_project_and_deployed_binding_both_required():
    project, dep = objects()
    original = copy.deepcopy((project, dep))
    before = adoption.settings_witness(project, dep, 'held')
    assert (project, dep) == original
    project['deployment_configs']['production']['env_vars']['NEWSLETTER_CAPTURE_MODE']['value'] = 'owned_test'
    with pytest.raises(carrier.GateError, match='capture_binding'):
        adoption.settings_witness(project, dep, 'owned_test')
    dep['env_vars']['NEWSLETTER_CAPTURE_MODE']['value'] = 'owned_test'
    after = adoption.settings_witness(project, dep, 'owned_test')
    assert before == after  # only the single authorized mode is excluded


@pytest.mark.parametrize('mutation', ['preview', 'secret', 'd1', 'flag', 'project', 'function', 'head'])
def test_settings_drift_refused(mutation):
    project, dep = objects()
    expected = adoption.settings_witness(project, dep, 'held')
    if mutation == 'preview':
        project['deployment_configs']['preview']['env_vars']['X'] = {'type': 'plain_text', 'value': 'oops'}
    elif mutation == 'secret':
        del project['deployment_configs']['production']['env_vars']['RESEND_API_KEY']
    elif mutation == 'd1':
        dep['d1_databases']['GLP_DB']['id'] = 'foreign-db'
    elif mutation == 'flag':
        dep['compatibility_flags'] = ['different']
    elif mutation == 'project':
        dep['project_id'] = 'foreign-project'
    elif mutation == 'function':
        dep['uses_functions'] = False
    else:
        dep['deployment_trigger']['metadata']['commit_hash'] = 'b' * 40
    with pytest.raises(carrier.GateError):
        adoption.require_witness(project, dep, 'held', expected)


def test_witness_does_not_serialize_secrets():
    project, dep = objects()
    for env in (project['deployment_configs']['production']['env_vars'], dep['env_vars']):
        env['RESEND_API_KEY']['value'] = 'synthetic-secret-never-retained'
    assert 'synthetic-secret-never-retained' not in json.dumps(adoption.settings_witness(project, dep, 'held'))


def test_terminal_and_ambiguous_no_replay(tmp_path, monkeypatch):
    path = tmp_path / 'CURRENT-RECEIPT.json'
    for status in ('ACTIVE', 'PASS_PRODUCTION_TERMINAL', 'BLOCKED_POST_CAS_VALIDATION'):
        path.write_text(json.dumps({'status': status}))
        with pytest.raises(carrier.GateError, match='no_replay'):
            adoption.reserve(path, {'binding': {'plan_sha256': 'a' * 64}})


def test_cli_owned_test_modes_cannot_mix_with_ordinary_release(monkeypatch):
    import glp_release_main as main
    forbidden = Mock(side_effect=AssertionError('effect reached'))
    monkeypatch.setattr(main, 'site_main_sha', forbidden)
    for args in (['--prepare-owned-test', '--sha', 'a' * 40],
                 ['--prepare-owned-test', '--prepare-only'],
                 ['--apply-owned-test', 'plan', '--prepare-owned-test'],
                 ['--authorize-owned-test-adoption']):
        with pytest.raises(SystemExit):
            main.main(args)
    forbidden.assert_not_called()


def test_existing_usage_model_difference_is_preserved_not_normalized():
    project, dep = objects()
    dep['usage_model'] = 'bundled'
    before = adoption.settings_witness(project, dep, 'held')
    dep['usage_model'] = 'standard'
    with pytest.raises(carrier.GateError, match='other_settings_drift'):
        adoption.require_witness(project, dep, 'held', before)


@pytest.fixture
def effect_fixture(tmp_path, monkeypatch):
    import os
    import types
    import base64
    runs, root = tmp_path / 'runs', tmp_path / 'prepared'
    spec = {'deployment_id': adoption.DEPLOYMENT, 'sha256': adoption.POINTER_SHA}
    prepared = {'deploy_directory': {'tree_sha256': 'fixture'}, 'compiled': {'worker_sha256': 'fixture'},
                'site_head': adoption.HEAD, 'route_specs': [], 'holds': {'held': []}}
    project, dep = objects()
    state = {'project': project, 'dep': dep, 'patches': [], 'uploads': [], 'cas': []}
    monkeypatch.setattr(adoption, 'retained_base', lambda: (root, prepared, spec))
    monkeypatch.setattr(adoption, 'verify_bytes', lambda *a: None)
    monkeypatch.setattr(carrier, 'acquire_lock', lambda: os.open(tmp_path / 'lock', os.O_CREAT | os.O_RDWR, 0o600))
    monkeypatch.setattr(carrier, 'verify_signed_predecessor', lambda *a: None)
    monkeypatch.setattr(carrier, 'verify_immutable_tree', lambda *a, **kw: None)
    monkeypatch.setattr(carrier, 'read_token', lambda: 'fixture-token')
    monkeypatch.setattr(carrier, 'wrangler_env', lambda *a: {})
    native_sha = carrier.sha_file
    monkeypatch.setattr(carrier, 'sha_file', lambda p: adoption.AUTHORITY_SHA if str(p).endswith(adoption.AUTHORITY) else native_sha(p))
    class Client:
        def __init__(self, token): self.token = token
        def project(self): return copy.deepcopy(state['project'])
        def request(self, method, path, body=None, **kw):
            if method == 'GET': return copy.deepcopy(state['dep'])
            assert method == 'PATCH' and path == adoption.BASE and body == adoption.patch_body()
            journal = json.loads(adoption.journal_path(runs).read_text())
            assert 'settings_patch_intent' in journal['stages']
            state['patches'].append(copy.deepcopy(body))
            state['project']['deployment_configs']['production']['env_vars'][adoption.KEY]['value'] = 'owned_test'
            if state.get('drop_secret'):
                del state['project']['deployment_configs']['production']['env_vars']['RESEND_API_KEY']
    monkeypatch.setattr(carrier, 'CloudflareClient', Client)
    monkeypatch.setattr(carrier, 'current_deployments', lambda _: [copy.deepcopy(state['dep'])])
    def upload(argv, **kwargs):
        receipt = json.loads(adoption.journal_path(runs).read_text())
        assert 'pages_deploy_intent' in receipt['stages']
        assert '--no-bundle' in argv and str(root / 'deploy-public') in argv
        assert kwargs['cwd'] != str(root)  # no discovered Wrangler settings
        state['uploads'].append(argv)
        if state.get('ambiguous'):
            raise subprocess.TimeoutExpired(argv, 1800)
        new = copy.deepcopy(state['dep'])
        new['id'] = '11111111-2222-3333-4444-555555555555'
        new['env_vars'][adoption.KEY]['value'] = 'owned_test'
        new['deployment_trigger']['metadata']['commit_message'] = argv[argv.index('--commit-message') + 1]
        state['dep'] = new
        state['project']['canonical_deployment'] = copy.deepcopy(new)
        return types.SimpleNamespace(returncode=0, stdout='fixture success', stderr='')
    import subprocess
    monkeypatch.setattr(adoption.subprocess, 'run', upload)
    monkeypatch.setattr(carrier, 'current_route_matrix', lambda *a: ['fixture-static'])
    monkeypatch.setattr(carrier, 'current_runtime_readback', lambda *a: ['fixture-runtime-held'])
    raw = b'{"fixture":"successor"}'
    intent = {'successor_bytes_base64': base64.b64encode(raw).decode(), 'final_mode': '0600',
              'final_uid': 1000, 'final_gid': 1000}
    monkeypatch.setattr(carrier, 'current_pointer_intent', lambda *a: copy.deepcopy(intent))
    monkeypatch.setattr(carrier, 'atomic_pointer_cas', lambda *a, **kw: state['cas'].append(a) or {'fixture': True})
    monkeypatch.setattr(carrier, '_pointer_exact', lambda *a: None)
    plan = adoption.prepare(runs)
    return runs, state, plan


def run_apply(fixture, tmp_path):
    runs, state, plan = fixture
    return adoption.apply(runs, Path(plan['plan']), plan['plan_sha256'], True, tmp_path)


def test_registered_retained_byte_apply_then_terminal_no_replay(effect_fixture, tmp_path):
    runs, state, plan = effect_fixture
    assert not state['patches'] and not state['uploads'] and not state['cas']
    out = run_apply(effect_fixture, tmp_path)
    assert out['status'] == 'PASS_PRODUCTION_TERMINAL' and out['capture'] == 'owned_test'
    assert len(state['patches']) == len(state['uploads']) == len(state['cas']) == 1
    receipt = json.loads(adoption.journal_path(runs).read_text())
    assert receipt['effects']['email_sends'] == receipt['effects']['enrollments'] == 0
    assert receipt['stages']['capture_binding_readback']['deployment'] == 'owned_test'
    with pytest.raises(carrier.GateError):
        run_apply(effect_fixture, tmp_path)
    assert len(state['patches']) == len(state['uploads']) == len(state['cas']) == 1


@pytest.mark.parametrize('failure', ['drop_secret', 'ambiguous'])
def test_uncertain_or_other_setting_loss_blocks_next_release_and_replay(effect_fixture, tmp_path, failure):
    runs, state, plan = effect_fixture
    state[failure] = True
    with pytest.raises(carrier.GateError):
        run_apply(effect_fixture, tmp_path)
    assert len(state['patches']) == 1 and not state['cas']
    before = len(state['uploads'])
    with pytest.raises(carrier.GateError):
        run_apply(effect_fixture, tmp_path)
    assert len(state['patches']) == 1 and len(state['uploads']) == before
    with pytest.raises(carrier.GateError, match='unsettled_blocks_release'):
        adoption.assert_no_pending(runs)


def test_changed_plan_requires_no_credentials_or_effects(effect_fixture, tmp_path, monkeypatch):
    runs, state, plan = effect_fixture
    Path(plan['plan']).write_text('{}')
    monkeypatch.setattr(carrier, 'read_token', Mock(side_effect=AssertionError('credential reached')))
    with pytest.raises(carrier.GateError):
        run_apply(effect_fixture, tmp_path)
    assert not state['patches'] and not state['uploads']
