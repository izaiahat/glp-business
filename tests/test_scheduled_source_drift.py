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


def test_sanitized_real_host_cron_row_has_source_paths_not_native_path(tmp_path):
    root, hermes, installed, *_ = fixture(tmp_path)
    row = json.loads((ROOT/'tests/fixtures/scheduled_source_drift_crontab_row.json').read_text())
    # Copied from the real host inventory; change values, never add native fields.
    assert set(row) == {'id', 'surface', 'sources', 'payloads'}
    row['sources'][0].update(path=str(installed), source='ops/hermes-cron-scripts/job.py',
                             reviewed_sha256=sha(installed.read_bytes()))
    (root/'ops/scheduled-source-drift-manifest.json').write_text(json.dumps(
        {'schema':'scheduled-source-drift-v1', 'jobs':[row]}))
    (hermes/'cron/jobs.json').write_text('{"jobs":[]}')
    result = load().audit(root, hermes, crontab_ids=[row['id']])
    assert result['status'] == 'CLEAN', result
    assert result['checked'] == 1 and result['host_cron_jobs'] == 1


@pytest.mark.parametrize('sources', [None, [], {}, [{'source':'job.py'}],
                                      [{'path':None}], [{'path':''}], [{'path':17}],
                                      [{'path':'relative.py'}], [{'path':'\u0000bad'}]])
def test_unresolved_crontab_source_is_unknown_only_for_its_row(tmp_path, sources):
    root, hermes, installed, *_ = fixture(tmp_path)
    installed.write_bytes(b'other row drift\n')
    inventory_path = root/'ops/scheduled-source-drift-manifest.json'
    inventory = json.loads(inventory_path.read_text())
    inventory['jobs'].append({'id':'bad-cron', 'surface':'crontab',
                              'sources':sources, 'payloads':[]})
    inventory_path.write_text(json.dumps(inventory))
    result = load().audit(root, hermes, crontab_ids=['bad-cron'])
    assert result['status'] == 'UNKNOWN'
    assert result['counts']['script_drift'] == 1
    assert result['checked'] == 3  # unrelated native source/scheduler/payload survived
    assert [f['job_id'] for f in result['findings'] if f['kind']=='unknown'] == ['bad-cron']


def test_missing_native_binding_path_does_not_abort_sibling_rows(tmp_path):
    root, hermes, installed, _, jobs = fixture(tmp_path)
    inventory_path = root/'ops/scheduled-source-drift-manifest.json'
    inventory = json.loads(inventory_path.read_text())
    del inventory['jobs'][0]['path']
    jobs.append({'id':'second', 'script':'job.py', 'script_sha256':sha(installed.read_bytes())})
    second = dict(inventory['jobs'][0], id='second', path=str(installed))
    inventory['jobs'].append(second)
    inventory_path.write_text(json.dumps(inventory))
    (hermes/'cron/jobs.json').write_text(json.dumps({'jobs':jobs}))
    result = load().audit(root, hermes, crontab_ids=[])
    assert result['status']=='UNKNOWN'
    assert result['native_script_jobs']==2 and result['checked']==3
    assert [f['job_id'] for f in result['findings'] if f['kind']=='unknown'] == ['job']


@pytest.mark.parametrize('bad_script', ['', None, 0, False, []])
def test_reviewed_native_falsey_binding_is_row_local_unknown(tmp_path, bad_script):
    import copy
    root, hermes, _, _, jobs = fixture(tmp_path)
    inventory_path = root/'ops/scheduled-source-drift-manifest.json'
    inventory = json.loads(inventory_path.read_text())
    good = copy.deepcopy(inventory['jobs'][0]); good['id'] = 'healthy-sibling'
    inventory['jobs'].append(good)
    jobs.append(dict(jobs[0], id='healthy-sibling'))
    jobs[0]['script'] = bad_script
    (hermes/'cron/jobs.json').write_text(json.dumps({'jobs':jobs}))
    inventory_path.write_text(json.dumps(inventory))
    before = {str(p): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
              for p in tmp_path.rglob('*') if p.is_file()}
    result = load().audit(root, hermes, crontab_ids=[])
    assert result['status'] == 'UNKNOWN', result
    assert result['findings'] == [{'job_id':'job', 'kind':'unknown',
                                  'reason':'installed_source_path_unresolved', 'path':''}]
    assert result['counts'] == {'script_drift':0, 'scheduler_drift':0,
                                'payload_drift':0, 'unknown':1}
    assert result['native_script_jobs'] == 2 and result['checked'] == 3
    assert before == {str(p): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                      for p in tmp_path.rglob('*') if p.is_file()}


def test_genuine_agent_only_jobs_remain_clean_with_healthy_native_sibling(tmp_path):
    root, hermes, _, _, jobs = fixture(tmp_path)
    jobs.extend([{'id':'agent-missing', 'prompt':'inert fixture'},
                 {'id':'agent-null', 'script':None, 'prompt':'inert fixture'},
                 {'id':'agent-empty', 'script':'', 'prompt':'inert fixture'}])
    (hermes/'cron/jobs.json').write_text(json.dumps({'jobs':jobs}))
    result = load().audit(root, hermes, crontab_ids=[])
    assert result['status'] == 'CLEAN' and result['findings'] == []
    assert result['native_script_jobs'] == 1 and result['checked'] == 3


@pytest.mark.parametrize('bad_script', [42, ['job.py'], '\u0000bad'])
def test_bad_native_script_binding_does_not_abort_sibling_rows(tmp_path, bad_script):
    root, hermes, installed, _, jobs = fixture(tmp_path)
    jobs.append({'id':'bad-native', 'script':bad_script})
    (hermes/'cron/jobs.json').write_text(json.dumps({'jobs':jobs}))
    result = load().audit(root, hermes, crontab_ids=[])
    assert result['status']=='UNKNOWN' and result['checked']==3
    assert [f['job_id'] for f in result['findings'] if f['kind']=='unknown']==['bad-native']


def test_all_committed_host_cron_source_row_shapes_are_resolved(tmp_path):
    import copy
    root, hermes, *_ = fixture(tmp_path)
    committed = json.loads((ROOT/'ops/scheduled-source-drift-manifest.json').read_text())
    rows = copy.deepcopy([r for r in committed['jobs'] if r['surface']=='crontab'])
    assert len(rows)==34 and all('path' not in row for row in rows)
    expected_checks = 0
    for number, row in enumerate(rows):
        # Source-schema regression only: replace binding values with inert files;
        # nested payload coverage has independent tests and is not asserted here.
        row['payloads'] = []
        for source_number, source in enumerate(row['sources']):
            path = root/f'row-{number}-source-{source_number}.py'
            path.write_bytes(b'fixture source; never execute\n')
            source['path'] = str(path)
            if 'reviewed_sha256' in source:
                source['reviewed_sha256'] = sha(path.read_bytes())
                expected_checks += 1
    (root/'ops/scheduled-source-drift-manifest.json').write_text(json.dumps(
        {'schema':'scheduled-source-drift-v1', 'jobs':rows}))
    (hermes/'cron/jobs.json').write_text('{"jobs":[]}')
    result = load().audit(root, hermes, crontab_ids=[row['id'] for row in rows])
    assert result['host_cron_jobs']==34 and result['checked']==54
    assert result['checked'] - result['counts']['unknown']==expected_checks==47  # GLP80 adds the reviewed existing price payload.
    assert result['counts']['unknown']==7  # Former bare price payload now has an exact reviewed binding.
    assert all(f['reason']=='invalid_or_absent_pin' for f in result['findings'])


@pytest.mark.parametrize('job_id, filename, accepted_hash', [
    ('3c79a0b5f7b2', 'inbox_control_plane_cron.py',
     '2cba7ec77ee5cb3261d5d8c8566b567bef5b27e9731d07e25444bd3821fde05a'),
    ('58202b06bdc0', 'affiliate_supply_discovery_daily.sh',
     'bd9288957cfb26d385541751e791b580007bed960482e611eb450d0e77de124a'),
    ('dc3649d3c84c', 'glp_relationship_hygiene_daily.sh',
     'f64276e4c789527cfa7b23deb6d4de64c905a482c5af74397134757dc8a5e4c7'),
])
def test_f09_reviewed_active_native_inventory_matches_exact_source(job_id, filename, accepted_hash):
    # These three reviewed adoptions are not a fleet-wide baseline refresh.
    inventory = json.loads((ROOT/'ops/scheduled-source-drift-manifest.json').read_text())
    rows = [row for row in inventory['jobs'] if row['id'] == job_id]
    assert len(rows) == 1
    row = rows[0]
    assert row['surface'] == 'native'
    assert row['path'] == '/home/ubuntu/.hermes/scripts/' + filename
    assert row['source'] == 'ops/hermes-cron-scripts/' + filename
    source = (ROOT/row['source']).read_bytes()
    if filename == 'affiliate_supply_discovery_daily.sh':
        # Candidate v2 producer pin is not a live adoption. Preserve the exact
        # reviewed inventory/predecessor oracle, allowing only this pin edit.
        assert sha(source) == '8f4eccb6e34daede6d0950617460a853e6b61edbaff7a648a4f7c8482258c01f'
        successor = b'88fca064dad7474a2e1af55402f9999e362f1d84cf077dbbdceee92d1169879e'
        assert source.count(successor) == 1
        assert sha((ROOT/'scripts/affiliate_supply_discovery.py').read_bytes()).encode() == successor
        source = source.replace(successor, b'8f882b3fc6ae1d6fc81590c94c84b1e28c1f094e273b7b0643c013366682ca7f')
    assert sha(source) == accepted_hash
    assert row['reviewed_sha256'] == accepted_hash
    assert row['payloads'] == []


def test_disk_monitor_inventory_is_exact_maintained_source_and_mutations_drift():
    inventory = json.loads((ROOT/'ops/scheduled-source-drift-manifest.json').read_text())
    rows = [row for row in inventory['jobs'] if row['id'] == 'crontab:d1449753b8c4b011']
    assert len(rows) == 1 and rows[0]['payloads'] == []
    assert rows[0]['surface'] == 'crontab' and len(rows[0]['sources']) == 1
    source = rows[0]['sources'][0]
    assert source['path'] == '/home/ubuntu/.hermes/scripts/disk_monitor.sh'
    assert source['source'] == 'ops/hermes-cron-scripts/disk_monitor.sh'
    accepted = '2bc27ff0fdfd02a2de2f94a1678b1ddadb04def5cbe5ddadc8cc75cdb438fe7d'
    raw = (ROOT/source['source']).read_bytes()
    assert source['reviewed_sha256'] == sha(raw) == accepted
    assert sha(raw + b'\n# unreviewed drift\n') != source['reviewed_sha256']


def test_current_inventory_adds_only_three_exact_reviewed_native_pins():
    import subprocess
    path = 'ops/scheduled-source-drift-manifest.json'
    expected = json.loads(subprocess.check_output([
        'git', '-C', str(ROOT), 'show',
        '898b45e903dffcce4046aa5548fc3e6f23b60b78:' + path]))
    allowed = {
        '3c79a0b5f7b2': '2cba7ec77ee5cb3261d5d8c8566b567bef5b27e9731d07e25444bd3821fde05a',
        '58202b06bdc0': 'bd9288957cfb26d385541751e791b580007bed960482e611eb450d0e77de124a',
        'ecbe6fdc0163': '77f12f10a9a38a228c3e8033ca8b538be37a1c799aa2c9d6ceef7b8b792d7fbb',
        'd5d5b954f303': '8d3cca1cb70d4fca01a5d70c6e050205c20d8fc637402a1e87711fb7bf929d07',
        'dc3649d3c84c': 'f64276e4c789527cfa7b23deb6d4de64c905a482c5af74397134757dc8a5e4c7',
    }
    seen = []
    for row in expected['jobs']:
        if row['id'] in allowed:
            row['reviewed_sha256'] = allowed[row['id']]
            seen.append(row['id'])
    assert sorted(seen) == sorted(allowed)
    launcher = 'scripts/cron_bridge_action.py'
    # GLP-320 successor adds only GET lineage proof propagation; retain literal
    # cohort equality and all historical inventory deltas below.
    launcher_pin = 'ad0398af92b07a67878eb5b0f3cd8456d8c306954eddddc711685e047c78bd72'
    assert sha((ROOT/launcher).read_bytes()) == launcher_pin
    shared = 0
    for row in expected['jobs']:
        for source in row.get('sources', []):
            if source.get('source') == launcher:
                assert source['reviewed_sha256'] == 'f1055dea298e8354bd61d9ccad084a2b33ab9c028ed7070b54e23d64ad023032'
                source['reviewed_sha256'] = launcher_pin
                shared += 1
    assert shared == 20
    # GLP-338: exact budget payload repin (digest headroom), F09 job only.
    budget = 'ops/n8n_execution_budget.py'
    budget_pin = 'f817de02a7c6659cd28767099a67edfab8411c20e095757142cd80ba66f42c10'
    assert sha((ROOT/budget).read_bytes()) == budget_pin
    repinned = 0
    for row in expected['jobs']:
        for payload in row.get('payloads', []):
            if payload['path'] == '/home/ubuntu/business/' + budget:
                assert row['id'] == 'b86ec8937c20'
                assert payload['sha256'] == '988b05a8b9c9b8c0906d6c678b1d5ce10d87b9640fc2ead568bda7aaa102ee23'
                payload['sha256'] = budget_pin
                repinned += 1
    assert repinned == 1
    # GLP-320: exact isolated TEST entry and repaired native Python payload.
    sensor = 'ops/hermes-cron-scripts/glp_seo_publish_failure_alert.py'
    sensor_pin = 'b51af360d690ed6fc9039bcfcdf5a31fc7080e25270051a07c78c2e0ab81d9d0'
    assert sha((ROOT/sensor).read_bytes()) == sensor_pin
    sensor_rows = [row for row in expected['jobs'] if row['id'] == '76de44046a9b']
    assert len(sensor_rows) == 1 and sensor_rows[0]['payloads'] == []
    assert sensor_rows[0]['reviewed_sha256'] == 'bc11df966517025e109519d2840ea24ca4eadfdb9f7c5c339ccd455afe6751a2'
    wrapper_pin = 'd42a5735e4b417437e83b976dbcb8f33cbbf611fb9808c2594e3b7c117189afc'
    assert sha((ROOT/'ops/hermes-cron-scripts/glp_seo_publish_failure_alert_cron.sh').read_bytes()) == wrapper_pin
    sensor_rows[0]['reviewed_sha256'] = wrapper_pin
    sensor_rows[0]['payloads'] = [{
        'path': '/home/ubuntu/.hermes/scripts/glp_seo_publish_failure_alert.py',
        'sha256': sensor_pin,
        'owner': '/home/ubuntu/.hermes/scripts/glp_seo_publish_failure_alert_cron.sh'}]
    # Sunday review now labels the upcoming governed week (GLP-76).
    strategy_pin = '4aee8957471ab8869bb4acf86f7c24780aebeb184a4450ea537ada7321b58fdb'
    assert sha((ROOT/'ops/hermes-cron-scripts/weekly_strategy_review_gate.py').read_bytes()) == strategy_pin
    strategy_rows = [row for row in expected['jobs'] if row['id'] == '020bcdd7f0e1']
    assert len(strategy_rows) == 1 and strategy_rows[0]['payloads'] == []
    assert strategy_rows[0]['reviewed_sha256'] == '484d54ce051eba4507742e824d93c2e60e1806d7624fd3caadc3bb29acc5053e'
    strategy_rows[0]['reviewed_sha256'] = strategy_pin
    # GLP-80: only the existing pricing action's payload registration changes.
    pricing_rows = [r for r in expected['jobs'] if r['id'] == 'crontab:daf0c212391b0319']
    assert len(pricing_rows) == 1
    assert pricing_rows[0]['sources'][1] == {'path': '/home/ubuntu/business/scripts/glp_pricing_refresh.py'}
    pricing_source = 'scripts/glp_price_freshness.py'
    pricing_rows[0]['id'] = 'crontab:d58cea74745f59c1'
    # GLP-350: exact maintained caller successor; transport no longer forces enhanced.
    pricing_pin = 'c114803b4cd8b544f99815aedc0f04b1d78abac1f1cde593b96c8046f32dabfe'
    assert sha((ROOT/pricing_source).read_bytes()) == pricing_pin
    pricing_rows[0]['sources'][1] = {'path': '/home/ubuntu/business/'+pricing_source,
        'source': pricing_source, 'reviewed_sha256': pricing_pin}
    # D1/PR449: exact maintained monitor successor includes the user-service bus.
    # Cron identity and all other source/payload metadata remain historical.
    monitor = 'ops/hermes-cron-scripts/disk_monitor.sh'
    monitor_pin = '2bc27ff0fdfd02a2de2f94a1678b1ddadb04def5cbe5ddadc8cc75cdb438fe7d'
    assert sha((ROOT/monitor).read_bytes()) == monitor_pin
    monitor_rows = [r for r in expected['jobs'] if r['id'] == 'crontab:d1449753b8c4b011']
    assert len(monitor_rows) == 1 and len(monitor_rows[0]['sources']) == 1
    assert monitor_rows[0]['sources'][0]['reviewed_sha256'] == '1f9c64eb1f8957c34aac6bd96f48ff42c79cafc4068c96da3fe3747b959f3447'
    monitor_rows[0]['sources'][0]['reviewed_sha256'] = monitor_pin
    # GLP349 source-only successor: exactly the existing host-storage row.
    # All historical transitions above and full-document equality remain intact.
    host = [r for r in expected['jobs'] if r['id'] == '075ed81c6b43']
    assert len(host) == 1 and host[0]['payloads'] == []
    assert host[0]['reviewed_sha256'] == '7d469fb2305cdb5bfc742b25e8681df73b3a7dbe4b385b18b54e10de8fd379d2'
    wrapper = 'ops/hermes-cron-scripts/host_disk_pruner.py'
    host_pin = '35a23b99209c58a1d43d237228f3fce8af3fea405b0dddcf7b7cbf26dd4d5f45'
    assert sha((ROOT/wrapper).read_bytes()) == host_pin
    host[0]['reviewed_sha256'] = host_pin
    payloads = [
        ('scripts/host_disk_pruner_trigger.py', 'e3e01d65d345943a4fd880930b9c42eeffadaa2fd3fee1b6a63135c6d549a896'),
        ('scripts/host_disk_cleanup_large_unit.py', '68a6abb5d6b229f1182aaf2afadd9171561a3c1d80a4efa9df5970ae38b587da'),
        ('scripts/host_disk_large_sweep.py', '04dae0459281e8e5192ddd965ceb86f00e1983916e335b29ee7f7c5bfe01bce7'),
    ]
    for rel, pin in payloads:
        assert sha((ROOT/rel).read_bytes()) == pin
    host[0]['payloads'] = [{'path': '/home/ubuntu/business/' + rel, 'sha256': pin,
        'owner': '/home/ubuntu/.hermes/scripts/host_disk_pruner.py'} for rel, pin in payloads]
    # Retain full-document equality and the historical fixed-cohort pins above.
    assert json.loads((ROOT/path).read_text()) == expected


def test_report_is_hash_only_and_not_job_payload_text(tmp_path):
    root, hermes, installed, *_ = fixture(tmp_path)
    installed.write_bytes(b'password="do-not-print-this-value"\n')
    result=load().audit(root,hermes,crontab_ids=[])
    assert 'do-not-print-this-value' not in json.dumps(result)
    assert 'do-not-print-this-value' not in load().render_summary(result)
