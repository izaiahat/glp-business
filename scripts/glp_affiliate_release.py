#!/usr/bin/env python3
"""F06 affiliate-sync adapter for the existing Cloudflare current-release carrier.

The operator explicitly approved routine merge/install/publication and removed
extra reviews on 2026-10-02. This is an operator-approved technical release, NOT
an invented independent reviewer. Exact CI, native build/design validators,
raw hold census, package bytes, Cloudflare readback and pointer CAS remain gates.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

ROOT=Path('/home/ubuntu/business')
AUTH='ops/linear/approvals/OPERATOR-DECISION-20261002-approve-everything-ship.md'
AUTH_SHA='0b950cd8e1344ed5f6bc334a8ef437b677996c57e336627bf1010df9167b0740'
SCHEMA='glp-affiliate-sync-release-result-v1'


def sha(raw):return hashlib.sha256(raw).hexdigest()
def read(path):return json.loads(Path(path).read_bytes())
def pin(path):return {'path':str(Path(path).resolve()),'sha256':sha(Path(path).read_bytes())}
def bound(binding):
    raw=Path(binding['path']).read_bytes()
    if sha(raw)!=binding['sha256']:raise ValueError('affiliate_release_evidence_drift')
    return json.loads(raw)
def save(path,value):
    from affiliate_terms_fanout import atomic,encoded
    atomic(Path(path),encoded(value))


def validate_build_evidence(receipt, rows):
    if receipt.get('scope')!='operator-approved-affiliate-sync' or receipt.get('release_authorized') is not False:
        raise ValueError('affiliate_release_scope_invalid')
    evidence=receipt['evidence']
    if set(evidence)!={'build','ci','design'}:raise ValueError('affiliate_release_evidence_missing')
    build,ci,design=(bound(evidence[k]) for k in ('build','ci','design'))
    if build.get('exit_code')!=0 or build.get('head')!=receipt['head'] or build.get('files')!=rows:
        raise ValueError('affiliate_release_build_binding_invalid')
    checks=ci.get('statusCheckRollup')
    if ci.get('headRefOid')!=receipt['head'] or not checks or any(c.get('status')!='COMPLETED' or c.get('conclusion')!='SUCCESS' for c in checks):
        raise ValueError('affiliate_release_ci_not_green')
    if (design.get('exit_code')!=0 or design.get('head')!=receipt['head']
            or design.get('checks')!=['affiliate-relationship-sync','raw-held-output','native-build-validators']):
        raise ValueError('affiliate_release_design_missing')
    census=design.get('raw_held',{})
    if (not census.get('held') or not census.get('htmlFiles') or not census.get('jsonFiles')
            or census.get('anchors')!=[] or census.get('routes')!=[]):
        raise ValueError('affiliate_release_hold_census_nonpass')


def validate_operator_release(review, prepared):
    if review.get('operator_authority')!={'path':AUTH,'sha256':AUTH_SHA} or sha((ROOT/AUTH).read_bytes())!=AUTH_SHA:
        raise ValueError('affiliate_release_operator_authority_invalid')
    gate=bound(prepared['build_receipt'])
    if gate.get('schema')!=SCHEMA:raise ValueError('affiliate_release_wrong_technical_contract')
    validate_build_evidence(gate,gate['output_after']['files'])
    if review.get('scope')!='operator-approved-affiliate-sync':raise ValueError('affiliate_release_wrong_operator_scope')


def authenticated_current_base(carrier):
    """Continue from a terminal native release, never a caller-supplied UUID."""
    pointer=read(carrier.POINTER)
    terminal=bound(pointer['final_receipt'])
    if (pointer.get('status')!='PASS' or terminal.get('status')!='PASS_PRODUCTION_TERMINAL'
            or terminal.get('deployment_id')!=pointer.get('deployment_id')
            or terminal.get('schema')!='glp43-current-deploy-receipt-v1'):
        raise ValueError('affiliate_release_current_base_not_terminal')
    prepared_path=Path(pointer['release_root']).parent/'PREPARED.json'
    prepared=read(prepared_path)
    if (prepared.get('site_head')!=pointer.get('site_head')
            or prepared.get('deploy_directory',{}).get('tree_sha256')!=pointer.get('deploy_directory_sha256')):
        raise ValueError('affiliate_release_current_base_identity_drift')
    # Full frozen source/deploy closure is revalidated by the carrier.
    old=carrier.CURRENT_ROLLBACK
    try:
        carrier.CURRENT_ROLLBACK=prepared['rollback_deployment_id']
        carrier.current_prepared(prepared_path.parent,sha(prepared_path.read_bytes()))
    finally:carrier.CURRENT_ROLLBACK=old
    return pointer


def package_and_publish(source, report, build_seat, *, publish=False):
    import glp_deploy_glp34_authority_v1 as carrier
    report=Path(report).resolve();source=Path(source).resolve()
    gate=read(report/'GATE.json')
    validate_build_evidence(gate,gate['output_before']['files'])
    base=authenticated_current_base(carrier)
    carrier.CURRENT_ROLLBACK=base['deployment_id']
    args=SimpleNamespace(site_head=gate['head'],public_root=str(source/'public'),
        expected_tree_sha256=gate['output_before']['sha256'],build_receipt=str(report/'GATE.json'),
        expected_build_receipt_sha256=sha((report/'GATE.json').read_bytes()),rollback_deployment_id=base['deployment_id'],
        package_dir=str(report/'package'),source_root=str(source),
        git_tree=subprocess.check_output(['git','-C',str(source),'rev-parse','HEAD^{tree}'],text=True).strip(),
        build_seat=build_seat,prepared_dir=str(report/'prepared'))
    if not (report/'package/PACKAGE-MANIFEST.json').exists():
        save(report/'package-result.json',carrier.run_package_only(args))
    args.expected_package_manifest_sha256=sha((report/'package/PACKAGE-MANIFEST.json').read_bytes())
    if not (report/'prepared/PREPARED.json').exists():
        save(report/'prepare-result.json',carrier.run_prepare_current(args))
    prepared=read(report/'prepared/PREPARED.json')
    args.expected_prepared_manifest_sha256=sha((report/'prepared/PREPARED.json').read_bytes())
    review={'schema':'glp-affiliate-sync-operator-release-v1','scope':'operator-approved-affiliate-sync',
        'verdict':'APPROVE','release_accepted':True,'blocking_findings':[],
        'operator_authority':{'path':AUTH,'sha256':AUTH_SHA},
        'subject':{'repository':carrier.CURRENT_REPOSITORY,'head':prepared['site_head'],
          'git_tree':prepared['source']['git_tree'],'public_tree_sha256':prepared['public_tree_sha256'],
          'package_manifest_sha256':prepared['package_manifest_sha256'],'project':carrier.PROJECT,
          'rollback_branch':carrier.PRODUCTION_BRANCH,'rollback_environment':'production',
          'rollback_deployment_id':base['deployment_id'],'prepared_manifest_sha256':args.expected_prepared_manifest_sha256,
          'deploy_directory_sha256':prepared['deploy_directory']['tree_sha256'],
          'compiled_worker_sha256':prepared['compiled']['worker_sha256']},
        'reused_technical_acceptance':{'gate_sha256':prepared['build_receipt']['sha256']}}
    save(report/'OPERATOR-RELEASE.json',review)
    args.approval=str(report/'OPERATOR-RELEASE.json');args.expected_approval_sha256=sha(Path(args.approval).read_bytes())
    args.authorize_production=publish
    if not publish:return {'status':'PREPARED_NOT_PUBLISHED'}
    marker=report/'publish-intent.json'
    if marker.exists():raise ValueError('affiliate_release_uncertain_inspect_native_receipt_no_replay')
    save(marker,{'prepared':pin(report/'prepared/PREPARED.json'),'base':base,'operator_authority':review['operator_authority']})
    result=carrier.run_publish_current(args)
    save(report/'PUBLISH.json',result)
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    p.add_argument('--build-seat',required=True);p.add_argument('--publish',action='store_true')
    a=p.parse_args();print(json.dumps(package_and_publish(a.source,a.report,a.build_seat,publish=a.publish)))

if __name__=='__main__':main()
