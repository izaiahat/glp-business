#!/usr/bin/env python3
"""Prepare the reviewed policy copy; never install source or modify Linear.

Input is the reconciler's current PLAN. Output is a candidate policy in this
checkout. Run with two explicit paths; retaining the original acceptance strings
is mandatory. This is preparation, not a completion receipt or a source install.
"""
import json
from pathlib import Path
import sys


def transform(plan):
    expected = {'GLP-125','GLP-320','GLP-327','GLP-328','GLP-349','GLP-47','GLP-334',
                'GLP-93','GLP-354','GLP-358','GLP-359','GLP-88','GLP-250','GLP-253'}
    assert {r['issue'] for r in plan['issues']} == expected and len(plan['issues']) == 14
    workers = {'GLP-125': 'ops/linear/proof-workers/GLP-125.json',
               'GLP-320': 'lanes/GLP320-LIVE-20261016/RESULT.json',
               'GLP-93': 'lanes/F01-OWNER/glp93-69-20261016/RESULT.json',
               'GLP-359': 'lanes/DOT-OWNER/RESULT.json',
               **{i:'ops/linear/proof-workers/'+i+'.json' for i in ('GLP-88','GLP-250','GLP-253')}}
    out = {'schema': 'glp-business-output-plan-v1',
           'authority': plan['authority'], 'written_at_utc': plan['written_at_utc'],
           'notes': ['Candidate policy; parent must receive this into the registered fleet plan after Astra review.',
                     'Proof PASS means observed business output, not independent acceptance or permission to close.',
                     'No generic completion preflight, worker status or reviewer ACCEPT substitutes for business output.'],
           'issues': [], 'untouched': plan['untouched']}
    for original in plan['issues']:
        issue = original['issue']
        row = {k:original[k] for k in ('issue','title','state','order')}
        row.update(acceptance_rewrite=original['acceptance_rewrite'].split(' Read-only proof:',1)[0].strip(),
                   proof_cmd=['python3','-B','scripts/linear_business_proof.py',issue],
                   proof_cwd='/home/ubuntu/business', proof_timeout_s=240,
                   proof_complete=True, worker_result=workers.get(issue,original['worker_result']),
                   proof_contract='Read-only issue predicate + live native reads; unchanged completion/provenance/independent protected-acceptance gates remain mandatory.')
        out['issues'].append(row)
    return out


if __name__ == '__main__':
    assert len(sys.argv) == 3
    source, target = map(Path, sys.argv[1:])
    assert source.resolve() != target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    result = transform(json.loads(source.read_text()))
    # Same-owner authorized GLP-93 acceptance changed after the snapshot. This
    # query writes nothing to Linear and binds the complete current clauses.
    import linear_business_proof as business
    row = next(r for r in result['issues'] if r['issue'] == 'GLP-93')
    row['acceptance_rewrite'] = business.acceptance(business.live_issue('GLP-93')['description'])
    target.write_text(json.dumps(result, indent=2)+'\n')
