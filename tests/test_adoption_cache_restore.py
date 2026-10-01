"""Original PR109 R1 probes, with exact metadata and switch-custody assertions."""
import json
from pathlib import Path
import stat

import pytest

import test_glp44_scoped_adoption as glp44_fixtures
import test_glp73_paired_adoption as glp73_fixtures


def exact(path):
    st = path.stat()
    return path.read_bytes(), stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid


@pytest.mark.parametrize('variant', [
    'glp44',
    pytest.param('glp73', marks=pytest.mark.skipif(
        not (glp73_fixtures.LIVE / 'ops/.rebuild-sync-state.json').is_file()
        or not Path('/home/ubuntu/.hermes/hermes-agent/cron/scheduler.py').is_file(),
        reason='GLP73 original probe requires retained host preimages and native scheduler')),
])
def test_native_saver_failure_restores_exact_original_bytecode_cache(tmp_path, monkeypatch, variant):
    module = glp44_fixtures.pair if variant == 'glp44' else glp73_fixtures.pair
    if variant == 'glp44':
        checkout, root, hermes, registry, scheduler = glp44_fixtures.fixture(tmp_path, monkeypatch)
        proposal = module.plan(checkout, root, hermes, window=scheduler.window)[0]
        review = {**proposal, 'schema': 'glp44-scoped-adoption-review-v1',
                  'approved_source': module.APPROVED, 'decision': 'ACCEPT_EXACT_BYTES',
                  'reviewer': 'independent scratch reviewer'}
        original = {path: module.observed(path) for path in [*[root / rel for rel in module.PATHS],
                    root / module.syncer.STATE, hermes / 'scripts' / module.WRAPPER, registry]}
    else:
        root, hermes, registry, jobs, scheduler, checkout, window = glp73_fixtures.cohort(tmp_path, monkeypatch)
        review = glp73_fixtures.receipt(root, hermes, registry, jobs, checkout)
        original = module.plan(checkout, root, hermes)[0]
    original_metadata = {path: exact(path) for path in original}
    source = next(path for path in original if path.suffix == '.py' and path.is_relative_to(root))
    caches = [source.with_suffix('.pyc'), source.parent / '__pycache__' / (source.stem + '.cpython-311.pyc')]
    expected = {}
    for index, cache in enumerate(caches):
        cache.parent.mkdir(exist_ok=True)
        cache.write_bytes(b'EXACT retained original cache ' + str(index).encode())
        cache.chmod(0o640)
        expected[cache] = exact(cache)
    journal = root / 'window.json'
    forward = []
    def save_failure(_):
        forward.append(json.loads(journal.read_text())['source_switch'])
        assert forward[0]['state'] == 'published'
        assert all(not cache.exists() for cache in caches)
        raise OSError('injected native saver failure')
    monkeypatch.setattr(scheduler, '_save_jobs_unlocked', save_failure)
    with pytest.raises(OSError, match='injected native saver failure'):
        module.adopt(checkout, root, hermes, review, scheduler=scheduler, window=journal)
    assert {path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) for path in original} == original
    assert {path: exact(path) for path in original} == original_metadata
    actual = {cache: exact(cache) for cache in caches if cache.exists()}
    assert actual == expected, 'Ordinary native-save failure discarded original bytecode caches'
    assert len(forward) == 1
    restored = json.loads(journal.read_text())['source_switch']
    assert restored['state'] == 'rolled_back'
    assert restored['custody'] == forward[0]['custody']
    assert restored['files'].keys() == forward[0]['files'].keys()
    for name, item in forward[0]['files'].items():
        assert all(restored['files'][name][key] == value for key, value in item.items())
        digest = glp44_fixtures.pair.digest if variant == 'glp44' else glp73_fixtures.pair.sha
        assert digest(Path(item['original']).read_bytes()) == item['before']
    assert list(journal.parent.glob('.cohort-*')) == [Path(forward[0]['custody'])]
