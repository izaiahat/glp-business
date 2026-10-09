"""Offline native-terminal custody regression; all authority here is synthetic."""
import argparse
import ast
import base64
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[1]

def test_affiliate_run_root_is_narrow_and_still_requires_publication_proof(tmp_path):
    import re
    source = ROOT/'scripts/glp_price_index_weekly.py'
    node = next(n for n in ast.parse(source.read_text()).body if isinstance(n,ast.FunctionDef) and n.name=='_prepared_base_links')
    business=tmp_path/'business';(business/'reports/ops').mkdir(parents=True)
    namespace={'Path':Path,'Any':object,'re':re,'BUSINESS':business,'CURRENT_BASE':business/'pointer.json'}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(source),'exec'),namespace)
    fn=namespace['_prepared_base_links'];pointer={'site_head':'a'*40}
    # glp_release_main (operator-approved-main-release) runs are main-<stamp>; other prefixes stay refused.
    for stamp,reason in [('20261003T030854Z','publication_path_invalid'),('main-20261009T045825Z','publication_path_invalid'),
                         ('arbitrary','prepared_root_invalid'),('other-20261009T045825Z','prepared_root_invalid')]:
        release=business/'reports/glp-clinic-scout/affiliate-machine/registry-site-sync'/stamp/'prepared/release'
        release.mkdir(parents=True)
        with pytest.raises(RuntimeError,match=reason):fn(pointer,release,tmp_path/'wrong-manifest',tmp_path/'wrong-receipt',{},[])


def fixture(tmp_path):
    source = (ROOT / 'scripts/glp_price_index_weekly.py').read_text()
    tree = ast.parse(source)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_native_current_deploy_binding')
    sha = lambda raw: hashlib.sha256(raw).hexdigest()
    prepared_root = tmp_path / 'prepared'; prepared_root.mkdir()
    run = tmp_path / 'run'; run.mkdir()
    approval = tmp_path / 'astra-final-review/RESPONSE.md'; approval.parent.mkdir()
    approval.write_text('{"verdict":"APPROVE","fixture":true}')
    pointer_raw = b'{"fixture":"current-pointer"}\n'
    pointer_path = tmp_path / 'pointer.json'; pointer_path.write_bytes(pointer_raw)
    pointer = {'deployment_id':'fixture-deployment','site_head':'fixture-head'}
    prepared = {'rollback_deployment_id':'fixture-predecessor','deploy_directory':{'tree_sha256':'fixture-tree'}}
    prepared_raw = b'{"fixture":"prepared"}'
    intent = {'successor_bytes_base64':base64.b64encode(pointer_raw).decode()}
    def approve(args, supplied):
        assert supplied == prepared
        assert args.authorize_production is True
        assert args.expected_prepared_manifest_sha256 == sha(prepared_raw)
        if json.loads(Path(args.approval).read_text()).get('verdict') != 'APPROVE':
            raise RuntimeError('unapproved fixture')
        assert args.expected_approval_sha256 == sha(approval.read_bytes())
    producer = SimpleNamespace(current_approval=approve, current_pointer_intent=lambda *args: intent)
    receipt = {
        'schema':'glp43-current-deploy-receipt-v1','status':'PASS_PRODUCTION_TERMINAL',
        'binding':{'prepared_manifest_sha256':sha(prepared_raw),'approval_sha256':sha(approval.read_bytes())},
        'site_head':'fixture-head','rollback_deployment_id':'fixture-predecessor',
        'deploy_directory':prepared['deploy_directory'],
        'effects':{'pages_deployments':1,'pages_rollbacks':0,'pointer_atomic_cas':1,
                   'worker_deploys':0,'d1_mutations':0,'cron_mutations':0,'email_sends':0},
        'stages':{'pages_deploy_cli':{'returncode':0},
                  'pages_deploy_intent':{'predecessor_id':'fixture-predecessor','commit_hash':'fixture-head'},
                  'pages_deployment':{'deployment_id':'fixture-deployment','commit_hash':'fixture-head'},
                  'route_probes':[{'fixture':True}], 'runtime_and_holds':[{'fixture':True}],
                  'pointer_intent':intent,'pointer_cas':{'sha256':sha(pointer_raw)},
                  'canonical_pointer_readback':{'deployment_id':'fixture-deployment','site_head':'fixture-head',
                                                'pointer_sha256':sha(pointer_raw)}}}
    receipt_path = run / 'CURRENT-RECEIPT.json'
    receipt_path.write_text(json.dumps(receipt))
    def reader(raw_path, label):
        path=Path(raw_path)
        if path.is_symlink() or not path.is_file(): raise RuntimeError('fixture dependency invalid')
        st=path.stat()
        return path,path.read_bytes(),(st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)
    namespace={'argparse':argparse,'base64':base64,'json':json,'sha_bytes':sha,
               '_read_bound_regular_file':reader,'CURRENT_BASE':pointer_path}
    exec(compile(ast.Module(body=[node],type_ignores=[]),str(ROOT/'scripts/glp_price_index_weekly.py'),'exec'),namespace)
    return namespace['_native_current_deploy_binding'], (pointer,prepared_root,prepared,prepared_raw,run,producer), receipt_path, receipt, approval, pointer_path

def test_native_current_terminal_uses_real_bound_approval_and_preserves_predecessor(tmp_path):
    fn,args,path,receipt,approval,pointer=fixture(tmp_path)
    before={p:p.read_bytes() for p in (path,approval,pointer)}
    bound=fn(*args)
    assert set(bound)=={str(path),str(approval)}
    assert before=={p:p.read_bytes() for p in before}
    source=(ROOT/'scripts/glp_price_index_weekly.py').read_text()
    assert 'bound = _native_current_deploy_binding(pointer, root, prepared, raw, run_dir, producer, pointer_bytes=pointer_bytes)' in source

def test_native_terminal_snapshot_still_requires_exact_successor_bytes(tmp_path):
    fn,args,path,receipt,approval,pointer = fixture(tmp_path)
    raw = pointer.read_bytes()
    pointer.write_bytes(b'newer canonical pointer')
    with pytest.raises(RuntimeError):
        fn(*args)
    assert set(fn(*args, pointer_bytes=raw)) == {str(path), str(approval)}
    with pytest.raises(RuntimeError):
        fn(*args, pointer_bytes=b'foreign snapshot')
    assert pointer.read_bytes() == b'newer canonical pointer'


@pytest.mark.parametrize('change',['status','binding','head','deployment','rollback','cas','readback','intent','effects','route','foreign_pointer','approval'])
def test_native_terminal_refuses_tampering_or_unapproved_bytes(tmp_path,change):
    fn,args,path,receipt,approval,pointer=fixture(tmp_path)
    bad=copy.deepcopy(receipt)
    if change=='status': bad['status']='ACTIVE'
    elif change=='binding': bad['binding']['prepared_manifest_sha256']='f'*64
    elif change=='head': bad['site_head']='wrong-head'
    elif change=='deployment': bad['stages']['pages_deployment']['deployment_id']='foreign'
    elif change=='rollback': bad['stages']['rollback_intent']={'deployment_id':'fixture-predecessor'}
    elif change=='cas': bad['stages']['pointer_cas']['sha256']='f'*64
    elif change=='readback': bad['stages']['canonical_pointer_readback']['pointer_sha256']='f'*64
    elif change=='intent': bad['stages']['pointer_intent']['successor_bytes_base64']='eA=='
    elif change=='effects': bad['effects']['pointer_atomic_cas']=0
    elif change=='route': bad['stages']['route_probes']=[]
    elif change=='foreign_pointer': pointer.write_bytes(b'foreign-pointer')
    elif change=='approval': approval.write_text('{"verdict":"HOLD"}')
    path.write_text(json.dumps(bad))
    with pytest.raises(RuntimeError): fn(*args)
