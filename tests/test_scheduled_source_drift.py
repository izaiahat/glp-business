import hashlib
import json
import importlib.util
import pytest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def load():
    spec = importlib.util.spec_from_file_location('scheduled_source_drift', ROOT/'scripts/scheduled_source_drift.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def sha(body):
    return hashlib.sha256(body).hexdigest()

def fixture(tmp_path):
    root = tmp_path/'business'; hermes = tmp_path/'hermes'
    (root/'ops').mkdir(parents=True); (root/'ops/hermes-cron-scripts').mkdir()
    (hermes/'scripts').mkdir(parents=True); (hermes/'cron').mkdir()
    source = root/'ops/hermes-cron-scripts/job.py'; source.write_bytes(b'pass\n')
    installed = hermes/'scripts/job.py'; installed.write_bytes(source.read_bytes())
    payload = root/'payload.py'; payload.write_bytes(b'pass\n')
    files = {'ops/hermes-cron-scripts/job.py': {'sha256':sha(source.read_bytes())}}
    (root/'ops/rebuild-source-manifest.json').write_text(json.dumps({'files':files}))
    inventory = {'schema':'scheduled-source-drift-v1', 'jobs':[{'id':'job', 'surface':'native',
        'path':str(installed), 'source':'ops/hermes-cron-scripts/job.py',
        'payloads':[{'path':str(payload), 'sha256':sha(payload.read_bytes()), 'owner':str(installed)}]}]}
    (root/'ops/scheduled-source-drift-manifest.json').write_text(json.dumps(inventory))
    jobs = [{'id':'job', 'script':'job.py', 'script_sha256':sha(installed.read_bytes()), 'enabled':False}]
    (hermes/'cron/jobs.json').write_text(json.dumps({'jobs':jobs}))
    return root, hermes, installed, payload, jobs

def test_clean_audit_is_read_only(tmp_path):
    root, hermes, *_ = fixture(tmp_path)
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}
    result = load().audit(root, hermes, crontab_ids=[])
    assert result['status']=='CLEAN'
    assert result['counts']=={'script_drift':0, 'scheduler_drift':0, 'payload_drift':0, 'unknown':0}
    assert before == {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob('*') if p.is_file()}

def test_paused_job_script_and_scheduler_drift_remain_visible(tmp_path):
    root, hermes, installed, *_ = fixture(tmp_path)
    installed.write_bytes(b'changed\n')
    result = load().audit(root, hermes, crontab_ids=[])
    assert result['counts']['script_drift']==1
    assert result['counts']['scheduler_drift']==1
    assert result['status']=='DRIFT'

def test_transitive_payload_drift_and_missing_target(tmp_path):
    root, hermes, installed, payload, _ = fixture(tmp_path)
    payload.write_bytes(b'changed\n')
    assert load().audit(root, hermes, crontab_ids=[])['counts']['payload_drift']==1
    payload.unlink()
    result = load().audit(root, hermes, crontab_ids=[])
    assert result['counts']['unknown']>=1
    assert result['status']=='UNKNOWN'

def test_new_untracked_job_and_changed_binding_fail_closed(tmp_path):
    root, hermes, installed, payload, jobs = fixture(tmp_path)
    jobs[0]['script']='elsewhere.py'
    jobs.append({'id':'new','script':'not-curated.sh'})
    (hermes/'cron/jobs.json').write_text(json.dumps({'jobs':jobs}))
    result = load().audit(root, hermes, crontab_ids=['crontab:new'])
    assert result['status']=='UNKNOWN'
    assert result['counts']['unknown']>=3

def test_missing_inventory_and_invalid_registry_never_clean(tmp_path):
    root, hermes, *_ = fixture(tmp_path)
    (root/'ops/scheduled-source-drift-manifest.json').unlink()
    assert load().audit(root, hermes, crontab_ids=[])['status']=='UNKNOWN'
    (hermes/'cron/jobs.json').write_text('{')
    assert load().audit(root, hermes, crontab_ids=[])['status']=='UNKNOWN'

def test_symlink_target_is_not_read_as_source(tmp_path):
    root, hermes, installed, payload, _ = fixture(tmp_path)
    installed.unlink(); installed.symlink_to(payload)
    result = load().audit(root, hermes, crontab_ids=[])
    assert result['status']=='UNKNOWN'

def test_daily_due_first_health_tick_after_eight_pacific(tmp_path):
    m=load(); state={}
    assert not m.daily_due(state,datetime(2026,10,1,14,59,tzinfo=timezone.utc))
    assert m.daily_due(state,datetime(2026,10,1,15,0,tzinfo=timezone.utc))
    state['last_source_drift_day']='2026-10-01'
    assert not m.daily_due(state,datetime(2026,10,2,0,0,tzinfo=timezone.utc))
    assert m.daily_due(state,datetime(2026,10,2,15,0,tzinfo=timezone.utc))

def test_health_digest_executes_checker_without_importing_host_effects(tmp_path):
    import ast
    from types import SimpleNamespace
    tree=ast.parse((ROOT/'ops/hermes-cron-scripts/scheduled_job_health_manager.py').read_text())
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='collect_daily_source_drift']
    assert len(functions)==1
    calls=[]
    result={'status':'CLEAN','counts':{'script_drift':0,'scheduler_drift':0,'payload_drift':0,'unknown':0}}
    m=load()
    dependency=SimpleNamespace(daily_due=m.daily_due,live_audit=lambda: calls.append('audit') or result)
    scope={'scheduled_source_drift':dependency, 'ZoneInfo':__import__('zoneinfo').ZoneInfo}
    exec(compile(ast.Module(body=functions,type_ignores=[]),'<real health integration>','exec'),scope)
    state={}; now=datetime(2026,10,1,15,0,tzinfo=timezone.utc)
    assert scope['collect_daily_source_drift'](state,now)==result
    assert calls==['audit'] and state['last_source_drift_day']=='2026-10-01'
    assert scope['collect_daily_source_drift'](state,now) is None
    assert calls==['audit']
    assert state['last_source_drift']==result


@pytest.mark.parametrize('surface', ['native', 'crontab'])
@pytest.mark.parametrize('coverage', ['missing', None, {}, 'unsupported'])
def test_missing_payload_coverage_must_be_unknown(tmp_path, surface, coverage):
    root, hermes, installed, payload, _ = fixture(tmp_path)
    payload.write_bytes(b'changed payload that is no longer covered\n')
    inventory_path = root/'ops/scheduled-source-drift-manifest.json'
    inventory = json.loads(inventory_path.read_text())
    spec = inventory['jobs'][0]
    crontab_ids = []
    if surface == 'crontab':
        spec['surface'] = surface
        spec['sources'] = [{'path':str(installed), 'source':spec['source']}]
        (hermes/'cron/jobs.json').write_text('{"jobs":[]}')
        crontab_ids = ['job']
    if coverage == 'missing':
        del spec['payloads']
    else:
        spec['payloads'] = coverage
    inventory_path.write_text(json.dumps(inventory))
    result = load().audit(root, hermes, crontab_ids=crontab_ids)
    assert result['status'] == 'UNKNOWN', result
    assert any(f['job_id'] == 'job' and f['reason'] == 'transitive_pin_inventory_missing'
               for f in result['findings'])


def test_reviewed_explicit_no_payload_coverage_remains_clean(tmp_path):
    root, hermes, *_ = fixture(tmp_path)
    inventory_path = root/'ops/scheduled-source-drift-manifest.json'
    inventory = json.loads(inventory_path.read_text())
    inventory['jobs'][0]['payloads'] = []
    inventory_path.write_text(json.dumps(inventory))
    assert load().audit(root, hermes, crontab_ids=[])['status'] == 'CLEAN'


@pytest.mark.parametrize('files', [{}, 'missing', None, []])
def test_empty_nested_pin_coverage_must_be_unknown(tmp_path, files):
    root, hermes, _, _, _ = fixture(tmp_path)
    pinmanifest = root/'payload-pins.json'
    value = {'schema':'payload-pins-v1'}
    if files != 'missing':
        value['files'] = files
    pinmanifest.write_text(json.dumps(value))
    inventory_path = root/'ops/scheduled-source-drift-manifest.json'
    inventory = json.loads(inventory_path.read_text())
    inventory['jobs'][0]['payloads'] = [{'path':str(pinmanifest),
        'sha256':sha(pinmanifest.read_bytes()), 'owner':'job.py'}]
    inventory_path.write_text(json.dumps(inventory))
    result = load().audit(root, hermes, crontab_ids=[])
    assert result['status'] == 'UNKNOWN', result
    assert any(f['job_id'] == 'job' and f['reason'] == 'transitive_manifest_coverage_missing'
               for f in result['findings'])
    assert result['counts']['payload_drift'] == 0  # matching outer bytes are still checked


def test_nested_pin_member_and_outer_hash_drift_remain_visible(tmp_path):
    root, hermes, _, payload, _ = fixture(tmp_path)
    pinmanifest = root/'payload-pins.json'
    value = {'files': {str(payload):sha(payload.read_bytes())}}
    pinmanifest.write_text(json.dumps(value))
    inventory_path = root/'ops/scheduled-source-drift-manifest.json'
    inventory = json.loads(inventory_path.read_text())
    inventory['jobs'][0]['payloads'] = [{'path':str(pinmanifest),
        'sha256':sha(pinmanifest.read_bytes()), 'owner':'job.py'}]
    inventory_path.write_text(json.dumps(inventory))
    assert load().audit(root, hermes, crontab_ids=[])['status'] == 'CLEAN'
    payload.write_bytes(b'child drift\n')
    pinmanifest.write_text(json.dumps(value) + '\n')
    result = load().audit(root, hermes, crontab_ids=[])
    assert result['status'] == 'DRIFT'
    assert result['counts']['payload_drift'] == 2


def test_report_is_hash_only_and_not_job_payload_text(tmp_path):
    root, hermes, installed, *_ = fixture(tmp_path)
    installed.write_bytes(b'password="do-not-print-this-value"\n')
    result=load().audit(root,hermes,crontab_ids=[])
    assert 'do-not-print-this-value' not in json.dumps(result)
    assert 'do-not-print-this-value' not in load().render_summary(result)
