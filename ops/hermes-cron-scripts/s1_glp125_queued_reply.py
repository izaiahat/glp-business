#!/usr/bin/env python3
"""One-shot S1 direct-reply entry; fail closed before reviewed source installation."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path
ROOT=Path('/home/ubuntu/business')
LANE=ROOT/'reports/ops/fable-plan-2-20261009/lanes/S1'
SOURCE=ROOT/'scripts/glp_sponsor_nuimage_reply.py'
PIN='4a80776331b998ba1f327f92e93264194525d1dde4869009d3b087288fc73473'
REVIEW=LANE/'SOURCE-REVIEW.json'
def main():
    if not SOURCE.is_file() or hashlib.sha256(SOURCE.read_bytes()).hexdigest()!=PIN:
        print(json.dumps(dict(status='HELD_REVIEWED_SENDER_NOT_INSTALLED',sends=0)));return 2
    if not REVIEW.is_file():
        print(json.dumps(dict(status='HELD_INDEPENDENT_SOURCE_REVIEW_ABSENT',sends=0)));return 2
    review=json.loads(REVIEW.read_text())
    if (review.get('status')!='PASS' or not review.get('reviewer') or review.get('reviewer')=='S1'
        or review.get('nuimage_sender_sha256')!=PIN):
        print(json.dumps(dict(status='HELD_INDEPENDENT_SOURCE_REVIEW_BINDING_INVALID',sends=0)));return 2
    # Core sender owns time, exact copy, live stops/thread/draft and no-replay claim.
    completed=subprocess.run([sys.executable,str(SOURCE),'--send'],cwd=ROOT,check=False)
    return completed.returncode
if __name__=='__main__':sys.exit(main())
