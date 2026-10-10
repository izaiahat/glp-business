#!/usr/bin/env python3
"""Repair only the Reddit scout's missing retired skill binding."""
import json
from pathlib import Path
import sys

JOB_ID = '11f21051d2e8'


def repair(job, update):
    if job.get('id') != JOB_ID or job.get('fire_claim'):
        raise ValueError('wrong_or_active_job')
    if job.get('enabled_toolsets') != ['code_execution', 'firecrawl']:
        raise ValueError('capability_drift')
    if job.get('skills') == ['reddit-community-research']:
        return {'status':'ALREADY_CURRENT','changed_fields':[]}
    if job.get('skills') != ['web-research-automation']:
        raise ValueError('unknown_skill_preimage')
    updated = update(JOB_ID, {'skills':['reddit-community-research']})
    fields = sorted(k for k in set(job)|set(updated) if job.get(k) != updated.get(k))
    if fields != ['skill', 'skills']:
        raise ValueError('unexpected_job_delta')
    return {'status':'PASS','job_id':JOB_ID,'changed_fields':fields,
            'skills':updated['skills'],'enabled_toolsets':updated['enabled_toolsets']}


def main():
    sys.path.insert(0,str(Path.home()/'.hermes/hermes-agent'))
    from cron import jobs
    with jobs._fire_job_lock(JOB_ID) as owned:
        if not owned:
            raise ValueError('native_fire_lock_busy')
        print(json.dumps(repair(jobs.get_job(JOB_ID),jobs.update_job)))


if __name__ == '__main__':
    main()
