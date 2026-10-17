#!/usr/bin/env python3
"""GLP-47: one forward-only, retained-byte settings adoption in the sole publisher.

Called only by glp_release_main while it holds the registry/main run lock. Pages
bindings are deployment snapshots: changing the project alone is NOT adoption.
A distinct, journaled upload reuses the exact accepted deploy directory; it never
re-enters the consumed same-head publisher or rebuilds/recompiles accepted code.
"""
from __future__ import annotations

import base64
import copy
import os
from pathlib import Path
import subprocess

import glp_deploy_glp34_authority_v1 as c

DEPLOYMENT = '9dae1da7-cc07-40f5-b0a8-0719f9bb5142'
HEAD = '40a3faf58686ea7882cec26f9262a97946b6b99f'
PROJECT_ID = '81e5fd80-5162-4506-8b55-5349fc6a558e'
D1 = 'bcb91d2d-c3f1-4c55-93e3-070587e86d60'
POINTER_SHA = '6efdaa010a81d883fc7c1f9b52990ba01f3af14344068e9eaa9673d771016e8e'
PREPARED_SHA = '6c7d350ed57be149cc06809faea7408ba641704ed5b92ef43ffa6f7364b18e68'
AUTHORITY = 'ops/linear/approvals/OPERATOR-DECISION-20261016-grill-round2.md'
AUTHORITY_SHA = '3efb051e2de557bc0cdb45c4bf29eeac319364886bad7618b3d3578ca138ee0b'
KEY = 'NEWSLETTER_CAPTURE_MODE'
BASE = f'/accounts/{c.ACCOUNT_ID}/pages/projects/{c.PROJECT}'
DEPLOYED_KEYS = ('env_vars', 'd1_databases', 'compatibility_date', 'compatibility_flags',
                 'usage_model', 'build_image_major_version')
# Fixed-cohort deployed schema, independent of project deployment_configs. Keep
# stable identity/build/runtime settings; optional keys retain presence as well
# as values. Any provider field outside these two explicit classes must refuse.
DEPLOYED_SETTINGS = frozenset((*DEPLOYED_KEYS, 'build_config', 'project_id',
    'project_name', 'environment', 'production_branch', 'uses_functions',
    'kv_namespaces', 'r2_buckets', 'placement'))
# Only generated deployment identity/URLs, lifecycle/trigger/stage status and
# upload-file metadata may vary for a fresh retained-byte deployment. The native
# gates still authenticate ID, commit/marker, success and complete byte closure.
DEPLOYED_METADATA = frozenset(('id', 'short_id', 'url', 'aliases', 'created_on',
    'modified_on', 'deployment_trigger', 'latest_stage', 'stages', 'is_skipped', 'files'))


def digest(value):
    return c.sha_bytes(c.canonical(value))


def patch_body():
    # Documented Pages partial env-map PATCH. Never round-trip redacted secrets,
    # or submit the rest of production/preview settings as replacement values.
    return {'deployment_configs': {'production': {'env_vars': {
        KEY: {'type': 'plain_text', 'value': 'owned_test'}}}}}


def settings_witness(project, deployment, mode):
    """Authenticate BOTH surfaces; retain only hashes, never secret values."""
    canonical = c.production_deployment(project)
    if (project.get('id') != PROJECT_ID or project.get('uses_functions') is not True
            or deployment.get('project_id') != PROJECT_ID
            or deployment.get('project_name') != c.PROJECT
            or deployment.get('uses_functions') is not True
            or deployment.get('id') != canonical.get('id')
            or c.deployment_commit(deployment) != HEAD
            or not c.successful_production(deployment)
            or not c.successful_production(canonical)
            or c.deployment_commit(canonical) != HEAD):
        c.fail('owned_test_project_or_deployed_identity')
    configs = copy.deepcopy(project['deployment_configs'])
    prod = configs['production']
    env = prod['env_vars']
    expected = {'type': 'plain_text', 'value': mode}
    if env.get(KEY) != expected or deployment.get('env_vars', {}).get(KEY) != expected:
        c.fail('owned_test_capture_binding_not_' + mode)
    if prod.get('wrangler_config_hash'):
        c.fail('owned_test_wrangler_config_override')
    if prod.get('d1_databases') != {'GLP_DB': {'id': D1}}:
        c.fail('owned_test_project_d1_binding')
    for name in DEPLOYED_KEYS:
        # Pages currently reports project standard / deployment bundled. Keep
        # both exact values in independent witnesses, never normalize/bless a
        # changed deployed value merely because the project matches it.
        if (name not in prod or name not in deployment
                or name != 'usage_model' and prod[name] != deployment[name]):
            c.fail('owned_test_deployed_settings_mismatch:' + name)
    if set(deployment) - DEPLOYED_SETTINGS - DEPLOYED_METADATA:
        c.fail('owned_test_unknown_deployed_configuration')
    if not isinstance(deployment.get('build_config'), dict):
        c.fail('owned_test_deployed_build_config_missing_or_invalid')
    deployed_settings = {k: copy.deepcopy(v) for k, v in deployment.items()
                         if k not in DEPLOYED_METADATA}
    deployed_settings['env_vars'].pop(KEY)
    env.pop(KEY)
    return {'deployment_configs_except_capture_sha256': digest(configs),
            'project_settings_sha256': digest({k: project.get(k) for k in
                ('id', 'name', 'subdomain', 'production_branch', 'build_config', 'source',
                 'domains', 'production_script_name', 'preview_script_name')}),
            'deployed_settings_except_capture_sha256': digest(deployed_settings)}


def require_witness(project, deployment, mode, expected):
    if settings_witness(project, deployment, mode) != expected:
        c.fail('owned_test_other_settings_drift')


def get_current(client):
    project = client.project()
    current = c.production_deployment(project)
    deployment = client.request('GET', BASE + '/deployments/' + current['id'])
    return project, deployment


def retained_base():
    """Fixed current terminal cohort, not a caller UUID or self-recorded seal."""
    raw, st = c.capture_pointer(c.POINTER)
    if c.sha_bytes(raw) != POINTER_SHA:
        c.fail('owned_test_canonical_pointer_changed')
    pointer = c.load_json(c.POINTER)
    if (pointer.get('status') != 'PASS' or pointer.get('site_head') != HEAD
            or pointer.get('deployment_id') != DEPLOYMENT):
        c.fail('owned_test_terminal_pointer_identity')
    root = c.package_plain_path(Path(pointer['release_root']).parent)
    prepared = c.current_json(root / 'PREPARED.json', PREPARED_SHA)
    final = c.current_json(Path(pointer['final_receipt']['path']), pointer['final_receipt']['sha256'])
    manifest = c.current_json(Path(pointer['manifest']['path']), pointer['manifest']['sha256'])
    terminal = c.load_json(Path(pointer['final_receipt']['path']).parent / 'CURRENT-RECEIPT.json')
    if (final.get('status') != 'PASS_PRODUCTION_TERMINAL' or final.get('site_head') != HEAD
            or final.get('deployment') != {'deployment_id': DEPLOYMENT}
            or terminal.get('status') != 'PASS_PRODUCTION_TERMINAL'
            or terminal.get('binding', {}).get('prepared_manifest_sha256') != PREPARED_SHA
            or terminal.get('stages', {}).get('pages_deployment') !=
                {'deployment_id': DEPLOYMENT, 'commit_hash': HEAD}
            or terminal.get('stages', {}).get('canonical_pointer_readback', {}).get('pointer_sha256') != POINTER_SHA
            or terminal.get('effects', {}).get('pages_deployments') != 1
            or terminal.get('effects', {}).get('pointer_atomic_cas') != 1
            or prepared.get('site_head') != HEAD or prepared.get('project') != c.PROJECT
            or prepared.get('account_id') != c.ACCOUNT_ID
            or prepared.get('branch') != c.PRODUCTION_BRANCH
            or manifest.get('site_head') != HEAD
            or manifest.get('release_root') != pointer['release_root']):
        c.fail('owned_test_retained_terminal_binding')
    verify_bytes(root, prepared)
    if (manifest.get('release_rows') != c.file_rows(root / 'release')
            or manifest.get('release_tree_sha256') != prepared['release']['tree_sha256']
            or pointer['release_tree_sha256'] != prepared['release']['tree_sha256']
            or pointer['deploy_directory_sha256'] != prepared['deploy_directory']['tree_sha256']
            or pointer['public_tree_sha256'] != c.file_closure(root / 'release/public')['tree_sha256']):
        c.fail('owned_test_retained_manifest_binding')
    spec = {'path': str(c.POINTER), 'bytes_base64': base64.b64encode(raw).decode(),
            'sha256': POINTER_SHA, 'bytes': len(raw), 'st_dev': st.st_dev, 'st_ino': st.st_ino,
            'uid': st.st_uid, 'gid': st.st_gid, 'mode': f'{st.st_mode & 0o7777:04o}',
            'nlink': st.st_nlink, 'deployment_id': DEPLOYMENT, 'release_root': pointer['release_root']}
    return root, prepared, spec


def verify_bytes(root, prepared):
    if (c.sha_file(root / 'PREPARED.json') != PREPARED_SHA
            or c.file_closure(root / 'release') != prepared['release']
            or c.file_closure(root / 'deploy-public') != prepared['deploy_directory']
            or c.sha_file(root / 'deploy-public/_worker.js') != prepared['compiled']['worker_sha256']):
        c.fail('owned_test_retained_bytes_drift')
    package = c.current_json(root / 'PACKAGE-MANIFEST.json', prepared['package_manifest_sha256'])
    c.current_deploy_public(root / 'deploy-public', package['files'], prepared['compiled']['worker_sha256'])
    if c.current_route_specs(root / 'release/public') != prepared['route_specs']:
        c.fail('owned_test_route_specs_drift')


def reserve(path, receipt):
    # Never overwrite/reset the historical publisher or this one-shot journal.
    if path.exists():
        c.fail('owned_test_prior_intent_no_replay_inspect_receipt')
    c.exclusive_file(path, c.json_bytes(receipt))


def journal_path(runs):
    return runs / ('owned-test-' + DEPLOYMENT) / 'CURRENT-RECEIPT.json'


def assert_no_pending(runs):
    for path in runs.glob('owned-test-*/CURRENT-RECEIPT.json'):
        if c.load_json(path).get('status') != 'PASS_PRODUCTION_TERMINAL':
            c.fail('owned_test_unsettled_blocks_release:' + str(path))


def prepare(runs):
    lock = c.acquire_lock()
    try:
        root, prepared, spec = retained_base()
        client = c.CloudflareClient(c.read_token())
        project, deployment = get_current(client)
        if deployment['id'] != DEPLOYMENT:
            c.fail('owned_test_canonical_deployment_changed')
        witness = settings_witness(project, deployment, 'held')
        c.verify_signed_predecessor(spec)
        plan = {'schema': 'glp47-owned-test-adoption-plan-v1', 'issue': 'GLP-47',
                'effect': 'project_capture_setting_then_distinct_retained_byte_deployment',
                'created_at_utc': c.utc(), 'predecessor_pointer': spec,
                'prepared_manifest_sha256': PREPARED_SHA, 'prepared_dir': str(root),
                'site_head': HEAD, 'deployment_id': DEPLOYMENT, 'settings_witness': witness,
                'deploy_directory': prepared['deploy_directory'],
                'worker_sha256': prepared['compiled']['worker_sha256'],
                'authority': {'path': AUTHORITY, 'sha256': AUTHORITY_SHA},
                'from': 'held', 'to': 'owned_test', 'email_sends': 0, 'enrollments': 0}
        path = journal_path(runs).parent / 'PLAN.json'
        c.exclusive_file(path, c.json_bytes(plan))
        return {'status': 'PREPARED_OWNED_TEST_NOT_APPLIED', 'plan': str(path),
                'plan_sha256': c.sha_file(path), 'deployment_id': DEPLOYMENT,
                'provider_mutations': 0, 'email_sends': 0, 'enrollments': 0}
    finally:
        os.close(lock)


def apply(runs, plan_path, plan_sha, authorized, workspace):
    if not authorized or not plan_sha:
        c.fail('owned_test_explicit_apply_authority_required')
    # Pin the exact operator authority before credentials or output.
    if c.sha_file(workspace / AUTHORITY) != AUTHORITY_SHA:
        c.fail('owned_test_operator_authority_drift')
    path = journal_path(runs)
    if Path(plan_path) != path.parent / 'PLAN.json':
        c.fail('owned_test_registered_plan_path_required')
    plan = c.current_json(Path(plan_path), plan_sha)
    lock = c.acquire_lock()
    try:
        root, prepared, spec = retained_base()
        if (plan.get('schema') != 'glp47-owned-test-adoption-plan-v1'
                or plan.get('predecessor_pointer') != spec or plan.get('prepared_dir') != str(root)
                or plan.get('prepared_manifest_sha256') != PREPARED_SHA
                or plan.get('site_head') != HEAD or plan.get('deployment_id') != DEPLOYMENT
                or plan.get('deploy_directory') != prepared['deploy_directory']
                or plan.get('worker_sha256') != prepared['compiled']['worker_sha256']
                or plan.get('authority') != {'path': AUTHORITY, 'sha256': AUTHORITY_SHA}
                or plan.get('from') != 'held' or plan.get('to') != 'owned_test'):
            c.fail('owned_test_plan_binding_drift')
        c.verify_immutable_tree(c.TOOLCHAIN, c.CURRENT_TOOLCHAIN, toolchain=True)
        client = c.CloudflareClient(c.read_token())
        project, deployment = get_current(client)
        if deployment['id'] != DEPLOYMENT:
            c.fail('owned_test_canonical_deployment_changed')
        require_witness(project, deployment, 'held', plan['settings_witness'])
        marker = 'GLP47 owned_test adoption ' + plan_sha
        if any(c.deployment_commit(d) == HEAD and
               d.get('deployment_trigger', {}).get('metadata', {}).get('commit_message') == marker
               for d in c.current_deployments(client)):
            c.fail('owned_test_existing_operation_no_replay')
        receipt = {'schema': 'glp43-current-deploy-receipt-v1', 'operation': 'GLP-47-owned-test-adoption',
                   'status': 'ACTIVE', 'site_head': HEAD, 'rollback_deployment_id': DEPLOYMENT,
                   'binding': {'prepared_manifest_sha256': PREPARED_SHA, 'plan_sha256': plan_sha},
                   'deploy_directory': prepared['deploy_directory'], 'stages': {}}
        reserve(path, receipt)
        stages = receipt['stages']
        # Every external effect has a durable intent; re-entry NEVER mutates.
        stages['settings_patch_intent'] = {'key': KEY, 'from': 'held', 'to': 'owned_test',
                                           'created_at_utc': c.utc()}
        c.checkpoint(path, receipt)
        c.verify_signed_predecessor(spec)
        project, deployment = get_current(client)
        require_witness(project, deployment, 'held', plan['settings_witness'])
        client.request('PATCH', BASE, patch_body())
        project, old = get_current(client)
        if old['id'] != DEPLOYMENT:
            c.fail('owned_test_canonical_changed_after_patch')
        # Deployed binding must still be held until a new deployment exists.
        project_held = copy.deepcopy(project)
        project_held['deployment_configs']['production']['env_vars'][KEY] = {'type': 'plain_text', 'value': 'held'}
        if project['deployment_configs']['production']['env_vars'].get(KEY) != {'type': 'plain_text', 'value': 'owned_test'}:
            c.fail('owned_test_project_patch_not_observed')
        require_witness(project_held, old, 'held', plan['settings_witness'])
        stages['settings_patch_readback'] = {'project_capture': 'owned_test', 'deployed_capture': 'held',
                                            'other_settings_unchanged': True}
        verify_bytes(root, prepared)
        c.verify_signed_predecessor(spec)
        stages['pages_deploy_intent'] = {'predecessor_id': DEPLOYMENT, 'commit_hash': HEAD,
                                         'commit_message': marker, 'created_at_utc': c.utc()}
        c.checkpoint(path, receipt)
        cwd = path.parent / 'deploy-cwd'
        cwd.mkdir(exist_ok=False)
        command = [str(c.NODE), str(c.WRANGLER), 'pages', 'deploy', str(root / 'deploy-public'),
                   '--project-name', c.PROJECT, '--branch', c.PRODUCTION_BRANCH, '--commit-hash', HEAD,
                   '--commit-message', marker, '--no-bundle']
        try:
            proc = subprocess.run(command, cwd=str(cwd), env=c.wrangler_env(path.parent, client.token),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=1800)
            stages['pages_deploy_cli'] = {'returncode': proc.returncode,
                'stdout_sha256': c.sha_bytes(proc.stdout.encode()), 'stderr_sha256': c.sha_bytes(proc.stderr.encode())}
        except (OSError, subprocess.SubprocessError) as exc:
            stages['pages_deploy_cli'] = {'ambiguous': type(exc).__name__}
        c.checkpoint(path, receipt)
        project, current = get_current(client)
        matches = [d for d in c.current_deployments(client) if c.deployment_commit(d) == HEAD
                   and d.get('environment') == 'production'
                   and d.get('deployment_trigger', {}).get('metadata', {}).get('commit_message') == marker]
        if (len(matches) != 1 or matches[0]['id'] != current['id'] or current['id'] == DEPLOYMENT
                or current.get('deployment_trigger', {}).get('metadata', {}).get('commit_message') != marker):
            c.fail('owned_test_upload_unsettled_no_replay')
        require_witness(project, current, 'owned_test', plan['settings_witness'])
        stages['pages_deployment'] = {'deployment_id': current['id'], 'commit_hash': HEAD}
        stages['capture_binding_readback'] = {'project': 'owned_test', 'deployment': 'owned_test',
                                              'other_settings_unchanged': True, 'd1_id': D1}
        c.checkpoint(path, receipt)
        # Retain the accepted native route/runtime oracles and atomic pointer CAS.
        # No changed content means no purge, search submission or rebuilding.
        verify_bytes(root, prepared)
        stages['route_probes'] = c.current_route_matrix(client, current, prepared['route_specs'],
                                                        root / 'release/public', path, receipt)
        stages['runtime_and_holds'] = c.current_runtime_readback(current, prepared)
        project, current_again = get_current(client)
        if current_again['id'] != current['id']:
            c.fail('owned_test_foreign_head_before_cas')
        require_witness(project, current_again, 'owned_test', plan['settings_witness'])
        verify_bytes(root, prepared)
        # A new in-memory predecessor, NOT an edit to the accepted PREPARED file.
        next_prepared = {**prepared, 'predecessor_pointer': spec, 'rollback_deployment_id': DEPLOYMENT}
        intent = c.current_pointer_intent(root, next_prepared, current['id'], path.parent)
        stages['pointer_intent'] = intent
        c.checkpoint(path, receipt)
        stages['pointer_cas'] = c.atomic_pointer_cas(intent, lock, production=True)
        project, final = get_current(client)
        if final['id'] != current['id']:
            c.fail('owned_test_foreign_head_after_cas')
        require_witness(project, final, 'owned_test', plan['settings_witness'])
        raw = base64.b64decode(intent['successor_bytes_base64'], validate=True)
        c._pointer_exact(c.POINTER, raw, int(intent['final_mode'], 8), intent['final_uid'], intent['final_gid'])
        stages['canonical_pointer_readback'] = {'deployment_id': final['id'], 'site_head': HEAD,
                                                'pointer_sha256': c.sha_bytes(raw)}
        receipt['status'] = 'PASS_PRODUCTION_TERMINAL'
        receipt['effects'] = {'pages_deployments': 1, 'project_settings_patches': 1, 'pages_rollbacks': 0,
                             'pointer_atomic_cas': 1, 'worker_deploys': 0, 'd1_mutations': 0,
                             'cron_mutations': 0, 'email_sends': 0, 'enrollments': 0}
        c.checkpoint(path, receipt)
        return {'status': receipt['status'], 'receipt': str(path), 'receipt_sha256': c.sha_file(path),
                'deployment_id': final['id'], 'site_head': HEAD, 'capture': 'owned_test'}
    finally:
        # Failures remain durable ACTIVE/inspect-only, including after PATCH or
        # CAS. Do not rollback behind a pointer or reset project settings blindly.
        os.close(lock)
