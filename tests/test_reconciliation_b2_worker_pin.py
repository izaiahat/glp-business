"""Exact reviewed worker cohort; synthetic tamper must still stop the guard."""
import hashlib,re,subprocess
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
@pytest.mark.parametrize('tamper',[False,True])
def test_worker_pin_cohort(tmp_path,tamper):
    text=(ROOT/'ops/hermes-cron-scripts/glp_affiliate_reconciliation_honesty.sh').read_text()
    expected=re.search(r"scripts/glp_affiliate_wire_backlog_worker.py'\]='([a-f0-9]{64})'",text)[1]
    raw=(ROOT/'scripts/glp_affiliate_wire_backlog_worker.py').read_bytes()
    assert expected==hashlib.sha256(raw).hexdigest()
    worker=tmp_path/'worker.py';worker.write_bytes(raw+(b'\n' if tamper else b''))
    loop=text[text.index('for b2_path in'):text.index('\n# Resolve every approved')]
    script="declare -A b2_pins=(['"+str(worker)+"']='"+expected+"')\n"+loop
    run=subprocess.run(['bash','-c',script],capture_output=True,text=True)
    assert run.returncode==(1 if tamper else 0)
    assert ('b2_payload_hash_drift' in run.stdout)==tamper
