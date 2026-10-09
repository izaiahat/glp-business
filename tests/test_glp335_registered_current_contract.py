"""Offline fixtures only: registered consumer must never revive the August overlay."""
import hashlib
import importlib.util
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load():
    sys.path.insert(0, str(ROOT / 'scripts'))
    spec = importlib.util.spec_from_file_location('glp335_shipper', ROOT / 'scripts/glp_affiliati_postback_ship.py')
    assert spec is not None and spec.loader is not None
    ship = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ship)
    assert ship.__file__ is not None
    assert Path(ship.__file__).resolve() == ROOT / 'scripts/glp_affiliati_postback_ship.py'
    return ship


def deny_effects(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('offline regression attempted an external effect')
    monkeypatch.setattr(socket, 'create_connection', denied)
    monkeypatch.setattr(subprocess, 'run', denied)
    monkeypatch.setattr(subprocess, 'Popen', denied)


def test_modern_registered_spec_not_rejected_as_obsolete(tmp_path, monkeypatch):
    ship = load()
    spec_path = tmp_path / 'affiliati.json'
    spec_path.write_text(json.dumps({
        'schema': 'glp.postback_network_spec.v1', 'network': 'Affiliati',
        'callback_method': 'GET', 'receiver_endpoint': 'https://glpclinicscout.com/api/postback/affiliati',
        'configuration_scope': 'per_offer', 'offers': ['40044', '42896', '43194'],
        'click_id_macro': '#s2#',
        'callback_query': {'subid': '#s2#', 'click_id': '#s2#', 'placement': '#s1#',
                           'conversion_id': '#leadid#', 'offer_id': '#oid#',
                           'payout': '#price#', 'event': 'conversion'},
        'support': {'supported': True},
        'authentication': {'capability': 'supported', 'mode': 'shared_secret',
                           'placement': 'query', 'parameter': 'key',
                           'secret_ref': '/home/ubuntu/.hermes/secrets/affiliati/postback-key.txt'},
    }))
    monkeypatch.setattr(ship, 'NETWORK_SPEC', spec_path)
    deny_effects(monkeypatch)
    result = ship.validate_predeploy_control_artifacts()
    assert result['network_spec']['sha256'] == hashlib.sha256(spec_path.read_bytes()).hexdigest()
    assert result['click_id_macro'] == '#s2#'
    spec = json.loads(spec_path.read_text())
    spec['callback_query']['subid'] = '#s1#'
    spec_path.write_text(json.dumps(spec))
    with pytest.raises(ship.ShipError, match='network_spec_identity_contract_invalid'):
        ship.validate_predeploy_control_artifacts()


def fixture_release(ship, tmp_path, monkeypatch):
    """Synthetic isolated fixture, never a production authority or runtime event."""
    release = tmp_path / 'release'
    source = release / 'functions/api/postback/affiliati.js'
    source.parent.mkdir(parents=True)
    source.write_text('// offline fixture receiver; no executable code\n')
    contract = release / 'data/affiliati-postback-contract.json'
    contract.parent.mkdir()
    contract.write_text(json.dumps({
        'schema': 'affiliati-receiver-contract-v4-registered-subid-owned-click-join',
        'affiliate_id': '15309', 'public_endpoint': ship.ENDPOINT,
        'parameter_map': {k: {'macro': v} for k, v in {'subid': '#s2#', 'click_id': '#s2#', 'placement': '#s1#'}.items()},
        'auth': {'secret_binding': 'AFFILIATI_POSTBACK_KEY'},
        'persistence': {'database_binding': 'GLP_DB', 'd1_database_id': ship.D1_ID},
    }))
    attribution = release / 'src/affiliati-attribution.mjs'
    attribution.parent.mkdir()
    attribution.write_text('// offline fixture imported module; no executable code\n')
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({
        'site_head': ship.ACCEPTED['site_head'], 'release_root': str(release),
        'release_tree_sha256': 'fixture-release-tree',
        'release_rows': [{'path': str(p.relative_to(release)), 'sha256': ship.sha_file(p)} for p in (source, attribution, contract)],
    }))
    terminal = tmp_path / 'terminal.json'
    terminal.write_text(json.dumps({
        'schema': 'glp-opus-production-terminal-receipt-v1', 'status': 'PASS_PRODUCTION_TERMINAL',
        'site_head': ship.ACCEPTED['site_head'], 'deployment': {'deployment_id': ship.ACCEPTED['deployment_id']},
        'evidence_sha256': {'01-deployment-manifest.json': ship.sha_file(manifest)},
        'immutable_closure': {'release_tree_sha256': 'fixture-release-tree', 'public_tree_sha256': 'fixture-public-tree'},
    }))
    pointer = tmp_path / 'pointer.json'
    pointer.write_text(json.dumps({
        'schema': 'glp-opus-current-content-base-v1', 'status': 'PASS',
        'site_head': ship.ACCEPTED['site_head'], 'deployment_id': ship.ACCEPTED['deployment_id'],
        'release_root': str(release), 'release_tree_sha256': 'fixture-release-tree', 'public_tree_sha256': 'fixture-public-tree',
        'manifest': ship.binding(manifest), 'final_receipt': ship.binding(terminal),
    }))
    monkeypatch.setattr(ship, 'CURRENT_BASE', pointer)
    monkeypatch.setattr(ship, 'ACCEPTED', {**ship.ACCEPTED, 'manifest_sha256': ship.sha_file(manifest),
        'terminal_sha256': ship.sha_file(terminal), 'receiver_sha256': ship.sha_file(source)})
    monkeypatch.setattr(ship, 'validate_predeploy_control_artifacts', lambda: {'network_spec': {'path': 'fixture', 'sha256': 'fixture'}})
    return pointer, source, terminal


class FixtureClient:
    def __init__(self, ship):
        self.calls = []
        self.item = {'id': ship.ACCEPTED['deployment_id'], 'environment': 'production',
                     'latest_stage': {'status': 'success'},
                     'deployment_trigger': {'metadata': {'branch': 'main', 'commit_hash': ship.ACCEPTED['site_head']}},
                     'd1_databases': {'GLP_DB': {'id': ship.D1_ID}},
                     'env_vars': {'AFFILIATI_POSTBACK_KEY': {'type': 'secret_text', 'value': 'MUST_NOT_EXPORT'}}}
    def project(self):
        self.calls.append('project_GET')
        return {'name': 'glp-clinic-scout', 'production_branch': 'main', 'subdomain': 'glp-clinic-scout.pages.dev',
                'canonical_deployment': self.item, 'deployment_configs': {'production': self.item}}
    def request(self, method, path):
        assert method == 'GET'
        self.calls.append((method, path))
        return self.item


def test_modern_release_terminal_reconciliation_no_overlay_or_replay(tmp_path, monkeypatch):
    ship = load()
    pointer, source, terminal = fixture_release(ship, tmp_path, monkeypatch)
    deny_effects(monkeypatch)
    client = FixtureClient(ship)
    result = ship.reconcile(client)
    assert result['status'] == 'SKIP_ALREADY_INSTALLED'
    assert result['production_deployment_id'] == ship.ACCEPTED['deployment_id']
    assert result['inputs']['final_receipt'] == ship.binding(terminal)
    assert set(result['effects'].values()) == {0}
    assert 'MUST_NOT_EXPORT' not in json.dumps(result)
    assert len(client.calls) == 2
    before = {p: p.read_bytes() for p in (pointer, source, terminal)}
    assert ship.reconcile(client) == result
    assert all(p.read_bytes() == value for p, value in before.items())
    source.write_text('obsolete receiver')
    with pytest.raises(ship.ShipError, match='receiver_manifest_binding_invalid'):
        ship.reconcile(client)
    assert len(client.calls) == 4  # local refusal happens before any provider read


@pytest.mark.parametrize('mutation', ['canonical', 'branch', 'head', 'D1', 'secret'])
def test_wrong_live_canonical_or_binding_refuses_without_fallback(tmp_path, monkeypatch, mutation):
    ship = load()
    fixture_release(ship, tmp_path, monkeypatch)
    deny_effects(monkeypatch)
    client = FixtureClient(ship)
    if mutation == 'canonical': client.item['id'] = 'different-deployment'
    if mutation == 'branch': client.item['deployment_trigger']['metadata']['branch'] = 'preview'
    if mutation == 'head': client.item['deployment_trigger']['metadata']['commit_hash'] = 'other-head'
    if mutation == 'D1': client.item['d1_databases']['GLP_DB']['id'] = 'other-db'
    if mutation == 'secret': client.item['env_vars']['AFFILIATI_POSTBACK_KEY']['type'] = 'plain_text'
    with pytest.raises(ship.ShipError, match='canonical_receiver_readback_invalid'):
        ship.reconcile(client)


def test_registered_stdout_host_terminal_receipt_correlation(tmp_path, monkeypatch, capsys):
    ship = load()
    fixture_release(ship, tmp_path, monkeypatch)
    deny_effects(monkeypatch)
    monkeypatch.setattr(ship, 'CloudflareClient', lambda _: FixtureClient(ship))
    monkeypatch.setattr(ship, 'read_token', lambda: 'fixture-only')
    monkeypatch.setattr(ship, 'LOCK_PATH', tmp_path / 'release.lock')
    monkeypatch.setattr(ship, 'RECEIPT_DIR', tmp_path / 'receipts')
    spec = importlib.util.spec_from_file_location('glp335_ingress', ROOT / 'scripts/glp_host_ingress.py')
    assert spec is not None and spec.loader is not None
    ingress = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ingress)
    with ingress.connect(tmp_path / 'ingress.sqlite3') as db:
        body = {'intent_id': 'offline-fixture-release-335', 'kind': 'affiliati_release', 'payload': {}}
        assert ingress.accept(db, body)['status'] == 'acknowledged'
        def poster(action, args):
            assert (action, args) == ('/run', {'action': 'script', 'name': 'affiliati_postback_ship', 'args': {}})
            assert ship.main() == 0
            stdout = capsys.readouterr().out
            return {'job_id': 'fixture-job', 'status': 'done', 'rc': 0, 'stdout_tail': stdout}
        assert ingress.drain(db, poster)['unresolved'] == 0
        row = db.execute('select * from intents').fetchone()
        assert row['status'] == 'completed'
        output = json.loads(row['receipt'])
        native_receipt = json.loads(Path(output['receipt']).read_text())
        assert native_receipt['deployment_id'] == output['deployment_id'] == ship.ACCEPTED['deployment_id']
        assert native_receipt['inputs']['final_receipt']['sha256'] == ship.ACCEPTED['terminal_sha256']
        assert ingress.accept(db, body)['status'] == 'completed'
        assert ingress.drain(db, lambda *args: pytest.fail('completed intent replay'))['unresolved'] == 0
        assert len(list((tmp_path / 'receipts').iterdir())) == 1


@pytest.mark.parametrize('mutation', ['valid', 'drift', 'missing', 'symlink'])
def test_imported_attribution_module_bound_before_provider_read(tmp_path, monkeypatch, mutation):
    """One existing receiver import must remain in its accepted manifest cohort."""
    ship = load()
    pointer, _, _ = fixture_release(ship, tmp_path, monkeypatch)
    deny_effects(monkeypatch)
    module = Path(json.loads(pointer.read_text())['release_root']) / 'src/affiliati-attribution.mjs'
    original = module.read_bytes()
    if mutation == 'drift':
        module.write_bytes(original + b'// drift\n')
    elif mutation == 'missing':
        module.unlink()
    elif mutation == 'symlink':
        target = tmp_path / 'identical-module.mjs'
        target.write_bytes(original)
        module.unlink()
        module.symlink_to(target)
    client = FixtureClient(ship)
    if mutation == 'valid':
        result = ship.reconcile(client)
        assert result['status'] == 'SKIP_ALREADY_INSTALLED'
        assert result['inputs']['src/affiliati-attribution.mjs'] == ship.binding(module)
        assert set(result['effects'].values()) == {0}
        assert len(client.calls) == 2
    else:
        with pytest.raises(ship.ShipError, match='receiver_manifest_binding_invalid'):
            ship.reconcile(client)
        assert client.calls == []


ACCOUNT_REF_CASES = {
    # label: (env var metadata or None for absent, expected refusal)
    'default_absent': (None, False),
    'plain_text_15309': ({'type': 'plain_text', 'value': '15309'}, False),
    'plain_text_99999_false_acceptance': ({'type': 'plain_text', 'value': '99999'}, True),
    'secret_text_opaque': ({'type': 'secret_text'}, True),
    'malformed_value': ({'type': 'plain_text', 'value': '15309x'}, True),
    'malformed_padded': ({'type': 'plain_text', 'value': ' 15309'}, True),
    'malformed_empty': ({'type': 'plain_text', 'value': ''}, True),
    'malformed_number': ({'type': 'plain_text', 'value': 15309}, True),
    'malformed_shape': ('15309', True),
}


@pytest.mark.parametrize('readback', ['canonical', 'detail', 'production_config'])
@pytest.mark.parametrize('case', sorted(ACCOUNT_REF_CASES))
def test_account_ref_override_validated_in_every_readback(tmp_path, monkeypatch, readback, case):
    """Labeled offline fixture: the pinned receiver honours AFFILIATI_POSTBACK_ACCOUNT_REF
    (default 15309), so each authenticated readback must prove the effective account."""
    ship = load()
    fixture_release(ship, tmp_path, monkeypatch)
    deny_effects(monkeypatch)
    client = FixtureClient(ship)
    canonical, detail, config = (
        json.loads(json.dumps(client.item)) for _ in range(3))
    client.project = lambda: (client.calls.append('project_GET') or
        {'name': 'glp-clinic-scout', 'production_branch': 'main', 'subdomain': 'glp-clinic-scout.pages.dev',
         'canonical_deployment': canonical, 'deployment_configs': {'production': config}})
    client.request = lambda method, path: (client.calls.append((method, path)) or detail)
    env_var, refuses = ACCOUNT_REF_CASES[case]
    target = {'canonical': canonical, 'detail': detail, 'production_config': config}[readback]
    if env_var is not None:
        target['env_vars']['AFFILIATI_POSTBACK_ACCOUNT_REF'] = env_var
    if refuses:
        with pytest.raises(ship.ShipError, match='receiver_account_binding_invalid'):
            ship.reconcile(client)
    else:
        result = ship.reconcile(client)
        assert result['status'] == 'SKIP_ALREADY_INSTALLED'
        assert result['canonical_readback']['account_ref'] == '15309'
        assert set(result['effects'].values()) == {0}
        assert 'MUST_NOT_EXPORT' not in json.dumps(result)
    assert len(client.calls) == 2  # metadata reads only; no secret value retrieval
