"""One current reporting/collector cohort; offline no-apply previews only."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp44_scoped_adoption as pair
import sync_from_repo as syncer

GENERATION = 'f07-identity-publication-20261002'
SOURCE = '87f681fa7e011a3f6af132ee7a4e27d6778ebbdf'
NEW_LEAVES = tuple('scripts/affiliate_reporting_' + name + '.py' for name in (
    'admitad', 'clickbank', 'everflow', 'healthtrader', 'partnerstack', 'refersion'))
EXTRA = ('scripts/affiliate_network_access.py', 'scripts/marketplace_adapters/katalys.py', *NEW_LEAVES)
MEMBERS = (*pair.COLLECTOR_PATHS, *EXTRA)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def blob(ref, rel):
    return subprocess.check_output(['git', '-C', str(ROOT), 'show', ref + ':' + rel])


def put(path, body, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(mode)


def snapshot(root, hermes):
    return {str(p): (p.read_bytes(), p.stat().st_mode & 0o777)
            for base in (root, hermes) for p in base.rglob('*') if p.is_file()}


def fixture(tmp_path):
    root, hermes = tmp_path / 'business', tmp_path / 'hermes'
    root.mkdir(); hermes.mkdir()
    old_pins = json.loads(blob(pair.COLLECTOR_SOURCE, 'ops/glp/reporting-v2/source-manifest.json'))['files']
    for absolute in old_pins:
        rel = str(Path(absolute).relative_to(syncer.DEFAULT_ROOT))
        put(root / rel, blob(pair.COLLECTOR_SOURCE, rel))
    for rel in pair.COLLECTOR_PATHS:
        put(root / rel, blob(pair.COLLECTOR_SOURCE, rel))
    put(root / syncer.MANIFEST, (ROOT / syncer.MANIFEST).read_bytes())
    hashes = {str(p.relative_to(root)): sha(p.read_bytes()) for p in root.rglob('*') if p.is_file()}
    put(root / syncer.STATE, (json.dumps({'hashes': hashes}) + '\n').encode())
    wrapper = blob(pair.COLLECTOR_SOURCE, pair.SNAPSHOT)
    put(hermes / 'scripts' / pair.WRAPPER, wrapper, 0o750)
    job = {'id': pair.JOB, 'script': pair.WRAPPER, 'script_sha256': sha(wrapper),
           'enabled': True, 'state': 'scheduled', 'no_agent': True,
           'schedule': {'kind': 'cron', 'expr': '0 4 * * 1'},
           'fire_claim': None, 'run_claim': None}
    put(hermes / 'cron/jobs.json', json.dumps({'jobs': [job]}).encode(), 0o600)
    return root, hermes


@pytest.fixture
def approved_checkout(tmp_path):
    # Retain the immutable PR164 payload; commit only the maintained producer
    # into this disposable tree so its producer equality guard remains exact.
    from test_glp333_reporting_generation import commit_fixture_tree
    checkout = tmp_path / 'approved-pr164'
    subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout', str(ROOT), str(checkout)], check=True)
    subprocess.run(['git', '-C', str(checkout), 'checkout', '--quiet', '--detach',
                    '2265d3539691fa221a49936da23cd206c3fe0bc8'], check=True)
    put(checkout / 'scripts/glp44_scoped_adoption.py', Path(pair.__file__).read_bytes(), 0o755)
    commit_fixture_tree(checkout)
    assert {rel: sha((checkout / rel).read_bytes()) for rel in MEMBERS} == pair.F07_HASHES
    assert (checkout / 'scripts/glp44_scoped_adoption.py').read_bytes() == Path(pair.__file__).read_bytes()
    return checkout


@pytest.mark.parametrize('old', [pair.COLLECTOR_GENERATION, pair.PR119_GENERATION, pair.REPORTING_SUCCESSOR_GENERATION])
def test_both_old_previews_refuse_and_one_successor_proposes(tmp_path, old, approved_checkout):
    root, hermes = fixture(tmp_path)
    before = snapshot(root, hermes)
    with pytest.raises(RuntimeError, match='approved_source_moved:'):
        pair.plan(ROOT, root, hermes, generation=old)
    with pytest.raises(RuntimeError, match='approved_source_moved:ops/glp/analytics-brain-control.json'):
        pair.plan(ROOT, root, hermes, generation=GENERATION)
    proposal = pair.plan(approved_checkout, root, hermes, generation=GENERATION)[0]
    assert snapshot(root, hermes) == before
    assert proposal['generation'] == GENERATION
    assert proposal['source_sha256'] == {rel: sha(blob(SOURCE, rel)) for rel in MEMBERS}
    assert proposal['producer_head'] == subprocess.check_output(
        ['git', '-C', str(approved_checkout), 'rev-parse', 'HEAD'], text=True).strip()
    assert {rel for rel, value in proposal['source_before_sha256'].items() if value is None} == set(NEW_LEAVES)
    assert proposal['target']['journal'] is None
    # No-apply preview is not authority to bypass maintenance or predecessor custody.
    with pytest.raises(RuntimeError, match='active_maintenance_window_required'):
        pair.adopt(approved_checkout, root, hermes, None, scheduler=None, generation=GENERATION)
    assert snapshot(root, hermes) == before


def test_fixed_successor_closes_all_reporting_pins_and_keeps_old_contracts():
    approved, members = pair.generation_contract(GENERATION)
    assert approved == SOURCE and tuple(members) == MEMBERS
    assert {rel: sha(blob(SOURCE, rel)) for rel in members} == pair.F07_HASHES
    for generation, source, paths, hashes in (
        (pair.REPORTING_SUCCESSOR_GENERATION, pair.REPORTING_SUCCESSOR_SOURCE, pair.REPORTING_SUCCESSOR_PATHS, pair.REPORTING_SUCCESSOR_HASHES),
        (pair.PR119_GENERATION, pair.PR119_SOURCE, pair.PR119_PATHS, pair.PR119_HASHES),
        (pair.COLLECTOR_GENERATION, pair.COLLECTOR_SOURCE, pair.COLLECTOR_PATHS, pair.COLLECTOR_HASHES),
    ):
        assert pair.generation_contract(generation) == (source, paths)
        assert {rel: sha(blob(source, rel)) for rel in paths} == hashes
    pins = json.loads(blob(SOURCE, 'ops/glp/reporting-v2/source-manifest.json'))['files']
    for rel in EXTRA:
        assert pins[str(syncer.DEFAULT_ROOT / rel)] == pair.F07_HASHES[rel]


def test_successor_inventory_includes_new_publication_members():
    import maintenance_inventory
    root, hermes = Path('/fixture/business'), Path('/fixture/hermes')
    assert {root / rel for rel in MEMBERS} <= maintenance_inventory.affected_paths(root, hermes)


def test_fixed_successor_collector_drift_still_refuses_without_mutation(tmp_path, approved_checkout):
    root, hermes = fixture(tmp_path)
    before = snapshot(root, hermes)
    collector = approved_checkout / 'systems/analytics-brain/collector.py'
    collector.write_bytes(collector.read_bytes() + b'\n# tampered\n')
    with pytest.raises(RuntimeError, match='approved_source_moved:systems/analytics-brain/collector.py'):
        pair.plan(approved_checkout, root, hermes, generation=GENERATION)
    assert snapshot(root, hermes) == before


def test_successor_retained_leaf_and_baseline_guards_still_refuse(tmp_path, approved_checkout):
    root, hermes = fixture(tmp_path)
    pair.plan(approved_checkout, root, hermes, generation=GENERATION)
    leaf = root / 'scripts/marketplace_adapters/impact.py'
    original = leaf.read_bytes()
    leaf.write_bytes(original + b'\n# tampered\n')
    with pytest.raises(RuntimeError, match='reporting_pin_closure_refused:'):
        pair.plan(approved_checkout, root, hermes, generation=GENERATION)
    leaf.write_bytes(original)
    selected = root / 'scripts/affiliate_network_access.py'
    selected.write_bytes(selected.read_bytes() + b'\n# tampered\n')
    with pytest.raises(RuntimeError, match='scoped_source_preflight_refused'):
        pair.plan(approved_checkout, root, hermes, generation=GENERATION)
