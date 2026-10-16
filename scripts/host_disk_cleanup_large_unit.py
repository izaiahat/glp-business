#!/usr/bin/env python3
"""Large-first scheduled controller; the original running source stays unchanged."""
import argparse
import collections
import datetime
import fcntl
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
BUS=Path('/home/ubuntu/business')
HOME=Path('/home/ubuntu/.hermes')
sys.path.insert(0,str(Path(__file__).resolve().parent))
from evidence_preserving_pruner import atomic_json
UNIT='host-disk-cleanup.service'
STATE=HOME/'runstate/host-disk-cleanup-last.json'

def disk():
    d=os.statvfs('/')
    return {'free_bytes':d.f_bavail*d.f_frsize,'total_bytes':d.f_blocks*d.f_frsize}

def free_space_pressure(d):
    return d['free_bytes']/d['total_bytes']<0.15

def phase_result(result, report, rows, *, accepted_returncodes=(0,), partial_returncodes=()):
    statuses=collections.Counter(r.get('status','UNSPECIFIED') for r in rows)
    partial=sum(bool(r.get('partial')) or r.get('status') in ('INCOMPLETE','UNKNOWN') or any(s in r.get('status','') for s in ('PARTIAL','PENDING')) for r in rows)
    gated=sum(r.get('status','').startswith(('GATED','REFUSED')) for r in rows)
    failed=sum(not r.get('ok',True) for r in rows)
    present=report.exists()
    if result.returncode not in (*accepted_returncodes,*partial_returncodes) or failed:
        status='failed'
    elif not present:
        status='missing_report'
    elif result.returncode in partial_returncodes or partial or gated:
        status='partial_or_gated'
    else:
        status='completed'
    return {'returncode':result.returncode,'status':status,'report':str(report),
            'report_present':present,'status_counts':dict(statuses),
            'partial_rows':partial,'gated_rows':gated,'failed_rows':failed}

def main(*,local_only=False):
    from contextlib import ExitStack
    # Same protocol/inode as pro_route.singleton(): dispatcher -> heavy/account.
    # Acquire before cleanup/source leases; never wait for or disturb its owner.
    # ExitStack closes both descriptors on return AND exceptions. Children do not
    # inherit the route fd; this controller retains it through every final check.
    with ExitStack() as custody:
        if local_only:
            path=BUS/'ops/gpt6pro-relay/route/dispatcher.lock'
            try:
                fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
                route=custody.enter_context(os.fdopen(fd,'rb'))
                opened=os.fstat(route.fileno());named=path.lstat()
                if not stat.S_ISREG(opened.st_mode) or (opened.st_dev,opened.st_ino)!=(named.st_dev,named.st_ino):
                    raise ValueError('dispatcher lock identity unavailable')
                fcntl.flock(route,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except (OSError,ValueError) as error:
                print(f'Local-only cleanup deferred: existing Pro dispatcher exclusion unavailable ({type(error).__name__}); no cleanup started.',flush=True)
                return 75
        lock=custody.enter_context(open(HOME/'runstate/host-disk-cleanup.lock','a+'))
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('Existing cleanup owner retained; duplicate skipped.');return 0
        return cleanup(local_only=local_only)


def cleanup(*,local_only=False):
    started=time.monotonic();stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    report=BUS/'reports/host-disk-prune'/('large-full-'+stamp);report.mkdir(parents=True)
    before=disk();rows=[];phases={};pressure=free_space_pressure(before)
    state={'unit':UNIT,'pid':os.getpid(),'report_dir':str(report),'root_before':before,
           'status':'running','stage':'large_classes','pressure_trigger':pressure,
           'local_only':local_only}
    atomic_json(STATE,state)
    command=['sudo','-n','/usr/bin/python3','-I',str(BUS/'scripts/host_disk_large_sweep.py'),
             '--report',str(report/'large'),'--budget','2700' if pressure else '900']
    if local_only:
        command.append('--local-only')
    # Open PR / Linear / live release / R8 / worktree / PREPARED references need
    # the unprivileged gh/Linear identity; collected here, fail-closed in the sweep.
    try:
        import host_disk_auto_classes as auto
        atomic_json(report/'external-pins.json',{'schema':'host-disk-external-pins-v1',
            'collected_at':time.time(),'pins':sorted(map(str,auto.external_pins()))})
        command+=['--external-pins',str(report/'external-pins.json')]
    except Exception as e:
        atomic_json(report/'external-pins-error.json',{'error':str(e)})
    large_result=subprocess.run(command)
    if (report/'large/large-classes.json').exists():
        rows.extend(json.loads((report/'large/large-classes.json').read_text())['rows'])
    phases['large_classes']=phase_result(large_result,report/'large/large-classes.json',rows)
    atomic_json(report/'large-phase-completion.json',{'root_before':before,'root_after':disk(),
                'rows':rows,'returncode':large_result.returncode})
    proof_dir=BUS/'reports/ops/disk-reclaim-20261009'
    intent=proof_dir/'scheduled-entrypoint-intent.json'
    if intent.exists():
        proof=json.loads(intent.read_text());after_large=disk()
        proof.update(stage='large_phase_completed',unit_pid=os.getpid(),report_dir=str(report),
            after_free_bytes=after_large['free_bytes'],
            df_delta_bytes=after_large['free_bytes']-proof['before_free_bytes'],
            attributable_bytes_freed=sum(r.get('bytes_freed',0) for r in rows),
            large_worker_returncode=large_result.returncode)
        proof['real_gb_reclaim_verified']=proof['df_delta_bytes']>=1_000_000_000 and proof['attributable_bytes_freed']>=1_000_000_000
        target=proof_dir/'RESULT2.json'
        receipt=json.loads(target.read_text()) if target.exists() else {}
        receipt['scheduled_entrypoint_proof']=proof;atomic_json(target,receipt)
        atomic_json(proof_dir/'scheduled-entrypoint-proof.json',proof)
    state.update(stage='legacy_safe_rules');atomic_json(STATE,state)
    safe_result=subprocess.run([sys.executable,str(BUS/'scripts/host_disk_pruner.py'),
        '--safe-rules-only','--emit-noop','--report-dir',str(report/'safe')],timeout=300)
    safe_rows=[]
    if (report/'safe/receipt.json').exists():
        safe=json.loads((report/'safe/receipt.json').read_text())
        safe_rows=[{'rule':'legacy_safe_rules','bytes_freed':safe['reclaimed_bytes'],'ok':safe_result.returncode in (0,2)}]
        rows.extend(safe_rows)
    phases['legacy_safe_rules']=phase_result(safe_result,report/'safe/receipt.json',safe_rows,accepted_returncodes=(0,2))
    state.update(stage='D_A_B_C',progress_path=str(report/'progress.json'));atomic_json(STATE,state)
    # Preserve every legacy rule/guard and its persisted cursors, but bound a
    # scheduled pass. Infinite --full-pass previously monopolized the singleton
    # for hours while B skipped mutable copies; later cron fires did no work.
    evidence_result=subprocess.run(['sudo','-n','/usr/bin/python3','-I',str(BUS/'scripts/evidence_preserving_pruner.py'),
        '--max-seconds','1050','--report',str(report/'evidence-preserving.json')])
    evidence_rows=[]
    if (report/'evidence-preserving.json').exists():
        evidence_rows=json.loads((report/'evidence-preserving.json').read_text())['rows']
        rows.extend(evidence_rows)
    phases['D_A_B_C']=phase_result(evidence_result,report/'evidence-preserving.json',evidence_rows,partial_returncodes=(2,))
    after=disk();totals=collections.Counter()
    for row in rows:totals[row['rule']]+=int(row.get('bytes_freed',0))
    receipt={**state,'status':'completed' if all(p['status']=='completed' for p in phases.values()) else 'completed_with_errors',
        'root_after':after,'root_free_delta_bytes':after['free_bytes']-before['free_bytes'],
        'rows':rows,'bytes_freed_by_rule':dict(totals),'reclaimed_bytes':sum(totals.values()),
        'runtime_seconds':time.monotonic()-started,'phase_results':phases}
    atomic_json(report/'completion.json',receipt)
    state.update(status=receipt['status'],stage='finished',completion_receipt=str(report/'completion.json'))
    atomic_json(STATE,state)
    line=f"⚙️ **Host Disk** — reclaimed **{sum(totals.values())/1024**3:.2f} GiB** · root **{after['free_bytes']/1024**3:.1f} GiB free** · receipt {report/'completion.json'}"
    print(line,flush=True)
    from post_discord_message import send,readback
    delivery={'channel_id':'1487300132054765629','content':line,'status':'attempting'}
    atomic_json(report/'delivery.json',delivery)
    try:
        message=send(delivery['channel_id'],line);delivery['message_id']=str(message['id'])
        atomic_json(report/'delivery.json',delivery)
        fetched=readback(delivery['channel_id'],delivery['message_id'])
        delivery.update(status='delivered',exact_readback=fetched.get('content')==line and str(fetched.get('channel_id'))==delivery['channel_id'])
    except Exception as exc:
        delivery.update(status='failed_or_unknown_no_automatic_retry',error_type=type(exc).__name__)
    atomic_json(report/'delivery.json',delivery)
    return 0 if receipt['status']=='completed' else 1

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--local-only',action='store_true',
                        help='disable all R2 destination/control work; retain guarded local classes')
    args=parser.parse_args()
    raise SystemExit(main(local_only=args.local_only))
