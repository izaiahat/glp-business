"""Disposable Git successors preserve paired-wrapper admission."""
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest
import glp73_paired_adoption as pair
import sync_from_repo as syncer


def put(path, body, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    path.chmod(mode)


def fixture(tmp_path, monkeypatch):
    checkout, root, hermes = (tmp_path / name for name in ('checkout', 'root', 'hermes'))
    for path in (checkout, root, hermes):
        path.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(checkout), *args], stderr=subprocess.DEVNULL).decode().strip()
    git('init')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    rel = pair.RETENTION_PATHS[0]
    schema_rel = 'scripts/glp_current_content_base_schema.py'
    old, new, schema = b'old archive\n', b'new archive\n', b'retained schema\n'
    files = {rel: old, schema_rel: schema}
    repo = Path(__file__).resolve().parents[1]
    for name in ('glp73_paired_adoption.py', 'maintenance_inventory.py'):
        files['scripts/' + name] = (repo / 'scripts' / name).read_bytes()
    for _, name, *_ in pair.RETENTION_JOBS:
        files[pair.SNAPSHOTS + '/' + name] = (repo / pair.SNAPSHOTS / name).read_bytes()
    def commit():
        for name, body in files.items():
            put(checkout / name, body)
        manifest = {'schema': 'GLP_BUSINESS_SOURCE_V1', 'tracked_count': len(files) + 1,
                    'files': {name: {'sha256': pair.sha(body)} for name, body in files.items()}}
        put(checkout / syncer.MANIFEST, json.dumps(manifest).encode())
        git('add', '.')
        git('commit', '-m', 'fixture')
        return git('rev-parse', 'HEAD')
    commit()
    files[rel] = new
    source = commit()
    put(root / rel, old, 0o600)
    put(root / schema_rel, schema)
    put(root / syncer.STATE, json.dumps({'hashes': {rel: pair.sha(old)}}).encode())
    jobs = []
    for jid, name, _, _, mode in pair.RETENTION_JOBS:
        archive_line, schema_line = (16, 20) if name.startswith('f12_') else (19, 23)
        body = pair.replace_lines(files[pair.SNAPSHOTS + '/' + name], {
            archive_line: (pair.SOURCE_BEFORE[13][0], pair.sha(old)),
            schema_line: (pair.SOURCE_BEFORE[0][0], pair.sha(schema))})
        put(hermes / 'scripts' / name, body, mode)
        jobs.append({'id': jid, 'script': name, 'script_sha256': pair.sha(body),
                     'enabled': False, 'state': 'paused', 'fire_claim': None, 'run_claim': None})
    put(hermes / 'cron/jobs.json', json.dumps({'jobs': jobs}).encode())
    class GitGeneration:
        kind = 'retention'
        paths = pair.RETENTION_PATHS
        hashes = {rel: pair.sha(new)}
        def verify(self, checkout):
            assert git('rev-parse', 'HEAD') == self.source
            assert pair.sha((checkout / rel).read_bytes()) == self.hashes[rel]
    generation = GitGeneration()
    generation.source, generation.name = source, 'git-' + source
    monkeypatch.setitem(sys.modules, 'glp_ship_generation', SimpleNamespace(GitGeneration=GitGeneration))
    return SimpleNamespace(checkout=checkout, root=root, hermes=hermes, generation=generation, old=old, new=new, rel=rel)


def test_git_retention_plan_authenticates_predecessor_and_pairs_wrappers(tmp_path, monkeypatch):
    f = fixture(tmp_path, monkeypatch)
    planned = pair.plan(f.checkout, f.root, f.hermes, generation=f.generation)
    preimages, stages, sources, records = planned
    assert sources == {f.rel: pair.sha(f.new)}
    assert set(records) == {row[0] for row in pair.RETENTION_JOBS}
    assert len(stages) == 2
    for path, body in stages.items():
        assert body == preimages[path][0].replace(pair.sha(f.old).encode(), pair.sha(f.new).encode())
    proposal = pair.retention_proposal(f.checkout, f.root, f.hermes, planned, f.generation)
    assert proposal['generation'] == f.generation.name
    assert proposal['source'] == f.generation.source
    assert (f.root / f.rel).read_bytes() == f.old


@pytest.mark.parametrize('mutation,error', [('source', 'not_committed_predecessor'), ('baseline', 'baseline_moved'),
    ('mode', 'source_mode_moved'), ('wrapper', 'wrapper_not_committed_predecessor'), ('schema', 'preimage_or_mode_moved'), ('job', 'paused_job_claim_or_state')])
def test_git_retention_refuses_unrecognized_live_preimages(tmp_path, monkeypatch, mutation, error):
    f = fixture(tmp_path, monkeypatch)
    if mutation == 'source':
        put(f.root / f.rel, b'local changes', 0o600)
    elif mutation == 'mode':
        (f.root / f.rel).chmod(0o644)
    elif mutation == 'baseline':
        put(f.root / syncer.STATE, b'{"hashes":{}}')
    elif mutation == 'wrapper':
        target = f.hermes / 'scripts' / pair.RETENTION_JOBS[0][1]
        target.write_bytes(target.read_bytes() + b'\n# unrelated edit\n')
    elif mutation == 'schema':
        put(f.root / 'scripts/glp_current_content_base_schema.py', b'unknown')
    else:
        target = f.hermes / 'cron/jobs.json'
        document = json.loads(target.read_bytes())
        document['jobs'][0]['enabled'] = True
        target.write_text(json.dumps(document))
    with pytest.raises(RuntimeError, match=error):
        pair.plan(f.checkout, f.root, f.hermes, generation=f.generation)


def test_git_retention_reuses_transaction_and_rechecks_authority(tmp_path, monkeypatch):
    from contextlib import nullcontext
    f = fixture(tmp_path, monkeypatch)
    calls = []
    f.generation.authorize = lambda *args: calls.append('authorize')
    registry = f.hermes / 'cron/jobs.json'
    class Scheduler:
        _jobs_file_lock = nullcontext()
        def _current_cron_store(self):
            return SimpleNamespace(jobs_file=registry)
        def _jobs_lock_file(self):
            return registry.parent / '.jobs.lock'
        def _fire_job_lock(self, jid):
            return nullcontext(True)
        def _record_load_stamp(self, value):
            pass
        def _save_jobs_unlocked(self, jobs):
            calls.append('save')
            registry.write_text(json.dumps({'jobs': jobs}))
    class Window:
        def accept_file_updates(self, before, after):
            calls.append('files')
        def accept_job_update(self, jid, before, after):
            calls.append('job')
        def switch_files(self, stage, original, commit):
            calls.append('switch')
            for path, body in stage.items():
                assert pair.read_regular(path) == original[path]
                path.write_bytes(body)
            commit()
    result = pair._adopt_retention(f.checkout, f.root, f.hermes, None, scheduler=Scheduler(),
                                   window=Window(), generation=f.generation)
    assert result['applied'] and result['source'] == f.generation.source
    assert calls == ['authorize', 'authorize', 'files', 'job', 'job', 'switch', 'save']
    assert (f.root / f.rel).read_bytes() == f.new
    for job in json.loads(registry.read_bytes())['jobs']:
        assert job['script_sha256'] == pair.sha((f.hermes / 'scripts' / job['script']).read_bytes())
        assert job['enabled'] is False and job['state'] == 'paused'
    assert syncer.sync(f.root, f.checkout, False, (), pair.RETENTION_PATHS) == 0


def test_readonly_preview_accepts_scheduled_retention_but_apply_still_requires_pause(tmp_path, monkeypatch):
    import glp_ship
    f = fixture(tmp_path, monkeypatch)
    registry = f.hermes / 'cron/jobs.json'
    document = json.loads(registry.read_bytes())
    for job in document['jobs']:
        job.update(enabled=True, state='scheduled')
    registry.write_text(json.dumps(document))
    before = registry.read_bytes()
    planned = pair.plan(f.checkout, f.root, f.hermes, generation=f.generation,
                        allow_scheduled_preview=True)
    assert all(job['enabled'] and job['state'] == 'scheduled' for job in planned[3].values())
    assert registry.read_bytes() == before
    assert glp_ship.preview(f.checkout, f.root, f.hermes, f.generation)['job_before'] == planned[3]
    with pytest.raises(RuntimeError, match='paused_job_claim_or_state'):
        pair.plan(f.checkout, f.root, f.hermes, generation=f.generation)
    # The effectful transaction never inherits preview-only admission.
    with pytest.raises(RuntimeError, match='paused_job_claim_or_state'):
        pair._adopt_retention(f.checkout, f.root, f.hermes, None, scheduler=object(),
                              window=object(), generation=f.generation)
    assert registry.read_bytes() == before
    document['jobs'][0]['fire_claim'] = {'fixture': 'busy'}
    registry.write_text(json.dumps(document))
    with pytest.raises(RuntimeError, match='paused_job_claim_or_state'):
        pair.plan(f.checkout, f.root, f.hermes, generation=f.generation, allow_scheduled_preview=True)
