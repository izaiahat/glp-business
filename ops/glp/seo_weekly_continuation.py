"""S3b analysis continuation of the existing weekly controller. No operational mode.

Q24 may install/call this stage from its existing weekly owner; scheduling and
non-article consumer admission are deliberately absent. All writes are confined
to an exclusive caller-selected analysis output; current source is read-only.
"""
import argparse
from contextlib import redirect_stdout
import datetime as dt
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys

BUSINESS=Path('/home/ubuntu/business')
CONTROLLER=BUSINESS/'scripts/glp_weekly_seo_control.py'

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def workweek_start(observation):
    # The registered Sunday planning run prepares the following Monday's work.
    return (observation+dt.timedelta(days=1) if observation.weekday()==6 else observation-dt.timedelta(days=observation.weekday())).isoformat()
def save(path,doc):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x') as stream:json.dump(doc,stream,indent=2,ensure_ascii=False);stream.write('\n')

def bootstrap():
    sys.dont_write_bytecode=True
    sys.path.insert(0,str(BUSINESS))
    import ops.glp
    staged=str(Path(__file__).resolve().parent)
    ops.glp.__path__=[staged]+[p for p in ops.glp.__path__ if p!=staged]

def run_week(input_path,output_root,*,report_date=None,registered=False):
    if str(BUSINESS) not in sys.path:
        sys.path.insert(0,str(BUSINESS))
    from ops.glp.seo_business_context import load_business_context, load_seo_skills
    business_context=load_business_context('weekly_continuation')
    seo_skills=load_seo_skills()
    output_root=Path(output_root).resolve();input_path=Path(input_path).resolve()
    if output_root.exists():raise ValueError('output_exists_no_replay')
    bootstrap()
    from ops.glp.seo_weekly_modules import read_bound,validate_snapshot
    from ops.glp.seo_nonarticle_emitter import emit_bundle,apply_operations
    from ops.glp.seo_weekly_strategy import build_strategy
    manifest=json.loads(input_path.read_text())
    for path,binding in manifest['source_bindings'].items():
        if sha(path)!=binding['sha256']:raise ValueError('source_generation_drift:'+path)
    base=read_bound(manifest['inputs']['content-base']);release=Path(base['release_root']).resolve()
    if output_root.is_relative_to(release) or any(output_root.is_relative_to(BUSINESS/p) for p in ['scripts','ops','systems','site-system']):raise ValueError('dry_run_output_is_live_source')
    copy_source=release/'data/page-seo-copy.json'
    if sha(copy_source)!=manifest['inputs']['seo-copy']['sha256']:raise ValueError('seo_source_snapshot_mismatch')
    gsc=read_bound(manifest['inputs']['gsc-28']);validate_snapshot(gsc,dt.datetime.now(dt.timezone.utc))
    prior=None
    if 'gsc-prior-28' in manifest['inputs']:
        prior=read_bound(manifest['inputs']['gsc-prior-28'])
        try:validate_snapshot(prior,dt.datetime.now(dt.timezone.utc))
        except (KeyError,ValueError,TypeError):prior=None
    observation=dt.datetime.fromisoformat(gsc['captured_at_utc'].replace('Z','+00:00')).date()
    observation=report_date or observation
    week_start=workweek_start(observation)
    output_root.mkdir(parents=True,exist_ok=False)
    # Execute the real existing controller's analysis path. It imports the staged
    # continuation modules via the explicitly prepared namespace, not a mock.
    from ops.glp import seo_weekly_modules
    spec=importlib.util.spec_from_file_location('s3b_actual_weekly_controller',CONTROLLER)
    controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
    captured=io.StringIO()
    with redirect_stdout(captured):
        controller_rc=controller.main(['--analysis-only','--module-input',str(input_path),'--analysis-output',str(output_root/'modules')])
    (output_root/'CONTROLLER-STDOUT.txt').write_text(captured.getvalue())
    module_receipt=output_root/'modules/receipt.json';report=json.loads(module_receipt.read_text())
    intakes=report.get('patch_intakes',{'rows':[]})
    generation={'release_root':str(release),'deployment_id':base['deployment_id'],'pointer_sha256':manifest['current_base_sha256'],'renderer_bindings':manifest.get('renderer_bindings',{})}
    bundle=emit_bundle(intakes,copy_source,week_start=week_start,evidence_bindings={'manifest':{'path':str(input_path),'sha256':sha(input_path)},'modules':{'path':str(module_receipt),'sha256':sha(module_receipt)}},generation=generation)
    save(output_root/'NON-ARTICLE-INTAKE.json',bundle)
    after=apply_operations(copy_source.read_bytes(),bundle)
    save(output_root/'dry-source/data/page-seo-copy.json',after)
    (output_root/'dry-source/src').mkdir(parents=True,exist_ok=False)
    renderer=release/'src/page-seo-copy.mjs'
    (output_root/'dry-source/src/page-seo-copy.mjs').write_bytes(renderer.read_bytes())
    strategy=build_strategy(report,bundle,gsc,prior,week_start=week_start)
    if 'cogny-weekly-evidence' in manifest['inputs']:
        from ops.glp.seo_weekly_cogny import enrich_strategy, read_binding
        binding=manifest['inputs']['cogny-weekly-evidence']
        evidence={**read_binding(binding),'binding':binding}
        strategy=enrich_strategy(strategy,report,evidence,business_context,seo_skills)
    elif registered:
        raise ValueError('registered_weekly_cogny_evidence_missing')
    save(output_root/'STRATEGY.json',strategy)
    (output_root/'WEEKLY-STRATEGY-NOTE.md').write_text(strategy['markdown'])
    for path,binding in manifest['source_bindings'].items():
        if sha(path)!=binding['sha256']:raise ValueError('source_drift_during_analysis:'+path)
    receipt={'schema':'glp-weekly-continuation-receipt-v1','status':'PASS_DRY_RUN' if controller_rc==0 else 'PARTIAL_DRY_RUN','effect_class':'MANUAL_ANALYSIS_ONLY','week_start':week_start,'controller':{'path':str(CONTROLLER),'sha256':sha(CONTROLLER),'exit_code':controller_rc,'argv':['--analysis-only','--module-input',str(input_path),'--analysis-output',str(output_root/'modules')]},'module_implementation':{'path':seo_weekly_modules.__file__,'sha256':sha(seo_weekly_modules.__file__)},'module_statuses':{k:v['status'] for k,v in report['modules'].items()},'intake_id':bundle['intake_id'],'source_operations':bundle['operation_count'],'source_input_sha256':sha(copy_source),'dry_source_sha256':sha(output_root/'dry-source/data/page-seo-copy.json'),'source_inputs_unchanged':True,'schedule_installed':False,'operational_queue_entry':False,'publication_authorized':False,'natural_weekly_run_observed':False,'unverified':['Kael Q24 installation and governed non-article admission','Source generation rebase/review, full build and independent acceptance','Deployment, indexing and natural weekly effect'],'files':[{'path':str(p),'sha256':sha(p)} for p in sorted(output_root.rglob('*')) if p.is_file()]}
    save(output_root/'WEEK-RECEIPT.json',receipt)
    return receipt

def main():
    parser=argparse.ArgumentParser(description='Evidence-only weekly continuation; no scheduler or publish mode')
    parser.add_argument('--module-input',type=Path,required=True);parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=run_week(args.module_input,args.output)
    print(json.dumps({k:result[k] for k in ['status','week_start','module_statuses','source_operations','operational_queue_entry','natural_weekly_run_observed']}))
    return 0 if result['status']=='PASS_DRY_RUN' else 2

if __name__=='__main__':raise SystemExit(main())
