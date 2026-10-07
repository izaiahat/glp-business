#!/usr/bin/env python3
"""S3b stage invoked by the existing Sunday control owner, not a scheduler."""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

B = Path('/home/ubuntu/business')
sys.path.insert(0, str(B))
ROOT = B / 'reports/glp-clinic-scout/seo-machine/weekly-source-stage'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(report_date, *, module_input=None, root=ROOT, admission_root=None, registered=False):
    from ops.glp.seo_business_context import load_business_context
    business_context = load_business_context('weekly_source_stage')
    pins = load('weekly_analysis_pin_guard', B / 'scripts/glp_weekly_seo_analysis.py')
    pins.verify_pins()
    from ops.glp.seo_weekly_continuation import run_week
    from ops.glp.seo_nonarticle_stage import admit, save
    target = Path(root) / report_date.isoformat()
    receipt_path = target / 'receipt.json'
    resumed = target.exists() and not receipt_path.is_file()
    orphan_intent = False
    if resumed and registered and module_input is None and not (target / 'analysis/WEEK-RECEIPT.json').exists():
        # The old Sunday carrier could stop after writing only INTENT. Admit that
        # exact zero-effect state, not an arbitrary partial collection/journal.
        intent = target / 'INTENT.json'
        if {p.name for p in target.iterdir()} == {'INTENT.json'} and not intent.is_symlink():
            recorded = json.loads(intent.read_text())
            if (recorded.get('report_date') == report_date.isoformat()
                    and recorded.get('metered_collection_authorized') is False
                    and set(recorded) == {'report_date', 'at_utc', 'metered_collection_authorized'}):
                from ops.glp.seo_weekly_spend import CLAIMS
                if any(json.loads(p.read_text()).get('report_date') == report_date.isoformat()
                       for p in Path(CLAIMS).glob('*.json')):
                    raise ValueError('weekly_orphan_metered_claim_reconcile_no_recollection')
                orphan_intent = True
                resumed = False
    prior_receipt_raw = None
    if receipt_path.is_file():
        prior = json.loads(receipt_path.read_text())
        for item in prior['files']:
            if hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('weekly_stage_receipt_drift')
        if registered and prior['status'] == 'BLOCKED_FINDING_RECONCILE_NO_RECOLLECTION':
            prior_receipt_raw = receipt_path.read_bytes()
            resumed = True
        else:
            return {**prior, 'idempotent_replay': True, 'receipt': str(receipt_path), 'sha256': hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
    if resumed:
        checkpoint = target / 'analysis/WEEK-RECEIPT.json'
        if not registered or module_input is not None or not checkpoint.is_file():
            raise ValueError('weekly_stage_incomplete_reconcile_no_recollection')
        week = json.loads(checkpoint.read_text())
        if week.get('status') != 'PASS_DRY_RUN':
            raise ValueError('weekly_stage_incomplete_reconcile_no_recollection')
        required = {str(target / 'analysis/STRATEGY.json'), str(target / 'analysis/NON-ARTICLE-INTAKE.json')}
        if not required.issubset({item['path'] for item in week['files']}):
            raise ValueError('weekly_checkpoint_bindings_missing')
        for item in week['files']:
            if hashlib.sha256(Path(item['path']).read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('weekly_checkpoint_file_drift')
        module_input = target / 'collection/MODULE-INPUTS.json'
        collection = json.loads((target / 'collection/COLLECTION-RESULT.json').read_text())
        bound = collection['manifest']
        if bound['path'] != str(module_input) or hashlib.sha256(module_input.read_bytes()).hexdigest() != bound['sha256']:
            raise ValueError('weekly_checkpoint_collection_drift')
        if json.loads((target / 'INTENT.json').read_text())['report_date'] != report_date.isoformat():
            raise ValueError('weekly_checkpoint_date_drift')
        reanchor = json.loads((target / 'R2-AUTHORITY-REANCHOR.json').read_text())
        spend = json.loads((target / 'METERED-CLAIM.json').read_text())
        if spend.get('granted') and json.loads(Path(spend['claim_path']).read_text()) != spend:
            raise ValueError('weekly_checkpoint_claim_drift')
    else:
        if not orphan_intent:
            target.mkdir(parents=True)
            save(target / 'INTENT.json', {'report_date': report_date.isoformat(), 'at_utc': dt.datetime.now(dt.timezone.utc).isoformat(), 'metered_collection_authorized': False})
        from ops.glp.seo_weekly_r2_stage import reanchor_pending
        reanchor = reanchor_pending() if registered else {'status': 'NOT_REQUESTED_ANALYSIS_ONLY'}
        save(target / 'R2-AUTHORITY-REANCHOR.json', reanchor)
        spend = {'granted': False, 'reason': 'NOT_REGISTERED_OR_INPUT_ALREADY_BOUND'}
        if module_input is None:
            if registered:
                from ops.glp.seo_weekly_spend import claim
                spend = claim(report_date)
            save(target / 'METERED-CLAIM.json', spend)
            collector = load('weekly_current_input_collector', B / 'scripts/glp_weekly_seo_collect.py')
            collector.run(target / 'collection', allow_metered=spend['granted'], report_date=report_date)
            module_input = target / 'collection/MODULE-INPUTS.json'
        week = run_week(module_input, target / 'analysis', report_date=report_date, registered=registered)
    admission = admit(target / 'analysis/NON-ARTICLE-INTAKE.json', **({'root': admission_root} if admission_root else {}))
    finding = {'status': 'NOT_REQUESTED_ANALYSIS_ONLY'}
    if registered:
        from ops.glp.cogny_client import connect
        from ops.glp.seo_weekly_cogny import ensure_weekly_finding
        strategy = json.loads((target / 'analysis/STRATEGY.json').read_text())
        try:
            finding = ensure_weekly_finding(connect(retries=1, timeout=90), B / 'reports/glp-clinic-scout/seo-machine/weekly-control/cogny-findings', strategy)
        except Exception as exc:
            finding = {'status': 'BLOCKED_FINDING_RECONCILE_NO_RECOLLECTION', 'error': type(exc).__name__ + ':' + str(exc)[:220]}
    result = {
        'schema': 'glp-weekly-source-stage-v1',
        'status': ('PASS_WEEKLY_SOURCE_STAGED' if finding['status'] == 'VERIFIED_EXACT_READBACK' else 'BLOCKED_FINDING_RECONCILE_NO_RECOLLECTION') if registered and week['status'] == 'PASS_DRY_RUN' else ('PASS_ANALYSIS_ONLY' if week['status'] == 'PASS_DRY_RUN' else 'PARTIAL'),
        'report_date': report_date.isoformat(),
        'resumed_checkpoint': resumed,
        'reconciled_intent_only': orphan_intent,
        'incremental_metered_cost_usd': 0 if resumed else None,
        'business_context': {k: v for k, v in business_context.items() if k != 'text'},
        'r2_reanchor': reanchor,
        'module_statuses': week['module_statuses'], 'source_operations': week['source_operations'],
        'admission': admission, 'strategy_note': str(target / 'analysis/WEEKLY-STRATEGY-NOTE.md'),
        'metered_collection_authorized': spend['granted'], 'metered_claim': spend,
        'finding': finding, 'module_input': str(module_input),
        'registered_entry_stage': registered, 'publication_authorized': False,
        'natural_execution_proven': False,
        'required_artifact': 'n8n trigger-mode due execution and this source-stage receipt; recurring DFS authority for fresh keyword-gap data; design build/acceptance for overlay',
        'files': [{'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(target.rglob('*')) if p.is_file() and p != receipt_path],
    }
    if prior_receipt_raw is None:
        save(receipt_path, result)
    else:
        import os, tempfile
        if receipt_path.read_bytes() != prior_receipt_raw:
            raise ValueError('weekly_resume_receipt_changed')
        old_sha = hashlib.sha256(prior_receipt_raw).hexdigest()
        archive = Path(root) / '.receipt-history' / report_date.isoformat() / (old_sha + '.json')
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists():
            if archive.read_bytes() != prior_receipt_raw:
                raise ValueError('weekly_resume_history_drift')
        else:
            with archive.open('xb') as handle:
                handle.write(prior_receipt_raw)
        result['resumes_prior_receipt'] = {'path': str(archive), 'sha256': old_sha}
        fd, temp = tempfile.mkstemp(dir=target, prefix='.receipt-resume-')
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump(result, handle, indent=2); handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, receipt_path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
    return {**result, 'receipt': str(receipt_path), 'sha256': hashlib.sha256(receipt_path.read_bytes()).hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--report-date', required=True, type=dt.date.fromisoformat)
    parser.add_argument('--module-input', type=Path)
    args = parser.parse_args()
    result = run(args.report_date, module_input=args.module_input)
    print(json.dumps(result))
    raise SystemExit(0 if result['status'] == 'PASS_ANALYSIS_ONLY' else 2)
