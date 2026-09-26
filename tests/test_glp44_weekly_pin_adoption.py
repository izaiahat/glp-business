"""Offline verification of the PR #49 pin closure; never invoke weekly main.

Uncurated host dependencies are not fabricated: the functional fixture scopes
pins to the three adopted sources. The separate binding test checks the real full
manifest hash against the unchanged script's literal PIN_SHA256.
"""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIVE = Path('/home/ubuntu/business')
SOURCES = ('ops/glp/seo_data_rails.py', 'scripts/glp_weekly_seo_ranking_report.py',
           'ops/glp/seo_business_context.py')


def load_analysis():
    spec = importlib.util.spec_from_file_location(
        'weekly_pin_analysis', ROOT / 'scripts/glp_weekly_seo_analysis.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_full_manifest_hash_and_adopted_source_bindings():
    module = load_analysis()
    raw = (ROOT / 'ops/glp/seo-weekly-installed-pins.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == module.PIN_SHA256
    pins = json.loads(raw)
    for rel in SOURCES:
        assert pins['paths'][str(LIVE / rel)] == hashlib.sha256(
            (ROOT / rel).read_bytes()).hexdigest()


@pytest.fixture
def adopted_fixture(tmp_path):
    module = load_analysis()
    pins = json.loads((ROOT / 'ops/glp/seo-weekly-installed-pins.json').read_bytes())
    scoped = {}
    for rel in SOURCES:
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / rel).read_bytes())
        scoped[str(target)] = pins['paths'][str(LIVE / rel)]
    pins['paths'] = scoped
    module.PIN_PATH = tmp_path / 'pins.json'
    module.PIN_PATH.write_text(json.dumps(pins))
    module.PIN_SHA256 = hashlib.sha256(module.PIN_PATH.read_bytes()).hexdigest()
    return module, pins, tmp_path


def test_verify_pins_accepts_new_source_bytes(adopted_fixture):
    module, pins, _ = adopted_fixture
    assert module.verify_pins() == pins


@pytest.mark.parametrize('rel', SOURCES)
def test_verify_pins_rejects_one_byte_source_change(adopted_fixture, rel):
    module, _, root = adopted_fixture
    target = root / rel
    original = target.read_bytes()
    target.write_bytes(bytes([original[0] ^ 1]) + original[1:])
    with pytest.raises(ValueError, match='^weekly_installed_source_drift:') as error:
        module.verify_pins()
    assert str(error.value) == 'weekly_installed_source_drift:' + str(target)


def test_verify_pins_rejects_manifest_change(adopted_fixture):
    module, _, _ = adopted_fixture
    module.PIN_PATH.write_bytes(module.PIN_PATH.read_bytes() + b' ')
    with pytest.raises(ValueError, match='^weekly_pin_manifest_drift$'):
        module.verify_pins()
