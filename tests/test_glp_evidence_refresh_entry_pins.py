"""F04 entry custody: offline fixture promotion, not live scheduler proof."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import socket
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'ops/glp/evidence-refresh-k3'
SOURCE = ROOT / 'ops/hermes-cron-scripts/glp_evidence_refresh_daily.py'
OLD_MANIFEST_PIN = '341865c3ed9a2063f1c8a80711059ab7acae2e50ad12e03a26c98896eb8f3a05'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def declared_pins():
    tree = ast.parse(SOURCE.read_text())
    rows = [node for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == 'PINS' for t in node.targets)]
    assert len(rows) == 1
    return ast.literal_eval(rows[0].value)


def load_entry(path, package, monkeypatch):
    spec = importlib.util.spec_from_file_location('f04_entry_fixture', path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    entry.BASE = package
    exec_calls, network_calls = [], []
    entry.os = SimpleNamespace(environ={}, execv=lambda *args: exec_calls.append(args))
    entry.sys = SimpleNamespace(argv=[str(path)])

    def deny_network(*args, **kwargs):
        network_calls.append((args, kwargs))
        pytest.fail('entry gate must not use network')

    monkeypatch.setattr(socket, 'socket', deny_network)
    monkeypatch.setattr(socket, 'create_connection', deny_network)
    return entry, exec_calls, network_calls


@pytest.fixture
def promotion(tmp_path):
    package = tmp_path / 'package'
    package.mkdir()
    for name in declared_pins():
        shutil.copyfile(PACKAGE / name, package / name)
    installed = tmp_path / 'allowed-scripts/glp_evidence_refresh_daily.py'
    installed.parent.mkdir()
    current = declared_pins()['SOURCE-PINS.json']
    assert current != OLD_MANIFEST_PIN
    installed.write_text(SOURCE.read_text().replace(current, OLD_MANIFEST_PIN))
    registration = {'id': '1ef582b6c7a0', 'script': installed.name,
                    'script_sha256': digest(installed)}
    return package, installed, registration


def test_declared_cohort_and_every_nested_source_match_reviewed_bytes():
    pins = declared_pins()
    assert set(pins) == {'daily_evidence_refresh.py', 'review_evidence.py',
                         'SOURCE-PINS.json', 'ASTRA-LOW-PROMPT.md',
                         'compose_current_base.py', 'registered_taxonomy.py'}
    for name, expected in pins.items():
        assert digest(PACKAGE / name) == expected, name
    manifest = json.loads((PACKAGE / 'SOURCE-PINS.json').read_text())
    assert len(manifest['sources']) == 11
    for absolute, expected in manifest['sources'].items():
        rel = Path(absolute).relative_to('/home/ubuntu/business')
        assert digest(ROOT / rel) == expected, rel
    for key, name in [('runner_sha256', 'daily_evidence_refresh.py'),
                      ('composer_sha256', 'compose_current_base.py'),
                      ('registered_taxonomy_sha256', 'registered_taxonomy.py')]:
        assert manifest[key] == pins[name]


def test_fixture_promotion_enrolls_exact_maintained_bytes(promotion, monkeypatch, capsys):
    package, installed, registration = promotion
    stale, calls, network = load_entry(installed, package, monkeypatch)
    assert stale.main() == 2
    assert json.loads(capsys.readouterr().out)['reason'] == 'ENTRY_PIN_DRIFT:SOURCE-PINS.json'
    assert calls == network == []
    # Fixture-only promotion and enrollment; real guarded native save is a separate acceptance step.
    shutil.copyfile(SOURCE, installed)
    registration['script_sha256'] = digest(installed)
    assert digest(SOURCE) == digest(installed) == registration['script_sha256']
    promoted, calls, network = load_entry(installed, package, monkeypatch)
    assert promoted.PINS == declared_pins()
    assert promoted.main() is None  # execv spy returned, unlike the real process replacement.
    assert calls == [(promoted.PYTHON, [promoted.PYTHON, '-B',
                                      str(package / 'daily_evidence_refresh.py'), '--continue-day'])]
    assert network == []


@pytest.mark.parametrize('name', sorted(declared_pins()))
def test_mutated_declared_file_refuses_before_exec_or_network(name, promotion, monkeypatch, capsys):
    package, installed, _ = promotion
    shutil.copyfile(SOURCE, installed)
    target = package / name
    original = target.read_bytes()
    target.write_bytes(original + b'\n')
    entry, calls, network = load_entry(installed, package, monkeypatch)
    before = dict(entry.PINS)
    assert entry.main() == 2
    assert json.loads(capsys.readouterr().out) == {
        'status': 'BLOCKED', 'reason': 'ENTRY_PIN_DRIFT:' + name}
    assert calls == network == []
    assert entry.PINS == before == declared_pins()
    assert target.read_bytes() == original + b'\n'
