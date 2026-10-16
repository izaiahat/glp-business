"""Review-only counterexamples; failing assertions specify safe behavior."""
import base64, json
from pathlib import Path
import pytest
import glp_release_inputs as inputs
import glp_release_main as release
import glp_affiliate_sync_job as job
import glp_deploy_sensor_hook as hook
import glp_deploy_glp34_authority_v1 as carrier


def test_effective_writable_bind_is_part_of_input_key(tmp_path, monkeypatch):
    compiler=tmp_path/'compiler'; compiler.mkdir(); (compiler/'x').write_text('compiler')
    monkeypatch.setattr(carrier,'TOOLCHAIN',compiler)
    source=tmp_path/'source'; source.mkdir()
    seed=tmp_path/'seed'; seed.mkdir(); (seed/'governed.json').write_text('old')
    report=tmp_path/'report'; report.mkdir()
    config={'namespace':'/site','base_argv':['/bin/true','--clearenv','--unshare-all','--cap-drop','ALL','--bind',str(source),'/site','--bind',str(seed),'/site/artifacts']}
    effective=job.namespace(config,source,report)
    bind=Path(effective[effective.index('/site/artifacts')-1])
    before=inputs.capture(config,source_tree='TREE',source_revision='HEAD',sheet_sha256='SHEET')
    (bind/'governed.json').write_text('changed effective build input')
    after=inputs.capture(config,source_tree='TREE',source_revision='HEAD',sheet_sha256='SHEET')
    print('EFFECTIVE_BIND_CHANGED',inputs.path_digest(bind)!=inputs.path_digest(seed),'KEY_UNCHANGED',before==after)
    assert before!=after, 'actual namespace input changed but snapshot still hashes template seed'


def test_skip_rejects_sensor_for_other_canonical_deployment(tmp_path,monkeypatch):
    import glp_deploy_completeness_sensor as sensor
    old=tmp_path/'old'; root=old/'prepared/release'; root.mkdir(parents=True)
    (root/'content').write_text('retained deployed bytes')
    source=old/'candidate'; source.mkdir(); (source/'source.js').write_text('source')
    report=tmp_path/'new'; report.mkdir()
    pointer={'site_head':'HEAD','release_root':str(root),'deployment_id':'OLD'}
    key={'sha256':'INPUTS','source_tree':'TREE'}
    baseline={'status':'PASS_PRODUCTION_TERMINAL','pointer':pointer,'inputs':key,'release_tree_sha256':inputs.path_digest(root),'validation_source':str(source),'validation_source_sha256':inputs.path_digest(source)}
    job.save(old/'SUCCESSFUL-INPUTS.json',baseline)
    monkeypatch.setattr(release,'fresh_release_inputs',lambda *a:key)
    monkeypatch.setattr(release,'validate_cached_release',lambda *a:None)
    checks=[{'check':n,'status':'PASS'} for n in hook.LIVE_CHECKS]
    # Real base-chain check reads the new current pointer, independently of
    # the retained release supplied to run_sensor. Transport alone is fake.
    import sys
    from types import SimpleNamespace
    current=tmp_path/'current'; current.mkdir()
    prior=tmp_path/'prior'; prior.mkdir()
    new_id='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
    current_pointer={'schema':'glp-opus-current-content-base-v1','status':'PASS','deployment_id':new_id,'release_root':str(current),'release_tree_sha256':'a'*64,'public_tree_sha256':'b'*64,'manifest':{'path':str(tmp_path/'manifest.json'),'sha256':'c'*64},'final_receipt':{'path':str(tmp_path/'terminal.json'),'sha256':'d'*64},'supersedes':{'deployment_id':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','release_root':str(prior)}}
    pointer_path=tmp_path/'pointer.json'; pointer_path.write_text(json.dumps(current_pointer))
    monkeypatch.setattr(sensor,'CONTENT_BASE',pointer_path)
    monkeypatch.setattr(sensor,'RELEASES',tmp_path)
    monkeypatch.setattr(sensor,'load_env_value',lambda *a:'fixture-not-sent')
    class Session:
        def __init__(self): self.headers={}
        def get(self,*a,**k): return SimpleNamespace(json=lambda:{'success':True,'result':{'canonical_deployment':{'id':new_id}}})
    monkeypatch.setitem(sys.modules,'requests',SimpleNamespace(Session=Session))
    checks[-1]=sensor.check_base_chain(offline=False)
    assert checks[-1]['status']=='PASS'
    print('REAL_BASE_CHAIN',checks[-1])
    raw={'schema':'glp-deploy-completeness-sensor-v1','status':'PASS','release':root.name,'release_root':str(root),'live_programs':1,'checks':checks,'failed_checks':[],'skipped_checks':[],'failure_count':0,'failures':[],'captured_at_utc':hook.dt.datetime.now(hook.dt.timezone.utc).isoformat()}
    monkeypatch.setattr(sensor,'run_sensor',lambda *a,**k:raw)
    monkeypatch.setattr(hook,'MACHINE_DIR',tmp_path/'sensors')
    out=release.try_identical_inputs({},report,pointer,{'headRefOid':'HEAD','url':'PR'})
    strict=hook.receipt_is_sensor_verified(out)
    print('SKIP',out['status'],'STRICT_EXISTING_CONSUMER',strict)
    assert strict[0] is False and 'canonical deployment differs' in strict[1]
    assert out['status']!='SKIPPED_IDENTICAL_INPUTS', 'new consumer accepts wrong deployment rejected by maintained strict consumer'


def test_acceptance_requires_all_required_exact_head_checks(monkeypatch):
    main='M'; head='H'; api='repos/izaiahat/glp-site-candidate'
    marker=json.dumps({'schema':'glp-main-integration-acceptance-v1','source_main_sha':main,'source_main_tree':'TREE','previous_pr':'OLDPR','previous_head':'OLDHEAD'},sort_keys=True,indent=2)+'\n'
    merged=[False]; effects=[]
    def gh(*args):
        if args[:2]==('pr','list'):
            if '--head' in args:
                return [{'number':99,'url':'NEWPR','headRefOid':head,'state':'OPEN','statusCheckRollup':[{'name':'unrelated-success-only','status':'COMPLETED','conclusion':'SUCCESS'}]}]
            if merged[0]: return [{'url':'NEWPR','headRefOid':head,'mergeCommit':{'oid':'NEWMAIN'}}]
            return [{'url':'OLDPR','headRefOid':'OLDHEAD','mergeCommit':{'oid':main}}]
        path=args[1]
        if '/pulls/99/merge' in path:
            effects.append('MERGE');merged[0]=True;return {'merged':True}
        if path.endswith('/commits/main'):return {'sha':'NEWMAIN' if merged[0] else main}
        if '/git/commits/' in path:return {'parents':[{'sha':main}]}
        if '/contents/' in path:return {'content':base64.b64encode(marker.encode()).decode()}
        if '/compare/' in path:return {'files':[{'filename':'data/main-release-acceptance.json'}]}
        if '/commits/' in path:return {'commit':{'tree':{'sha':'OLDTREE' if path.endswith('/OLDHEAD') else 'TREE'}}}
        raise AssertionError(args)
    out=release.ensure_main_acceptance(main,gh=gh)
    print('RECOVERY',out,'MOCK_EFFECTS',effects)
    assert not effects, 'one unrelated successful job permits merge while both required exact-head workflows are absent'
