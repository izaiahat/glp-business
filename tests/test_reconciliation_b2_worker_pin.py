"""Exact reviewed worker cohort; synthetic tamper must still stop the guard."""
import hashlib,re,subprocess
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
@pytest.mark.parametrize('tamper',[False,True])
def test_worker_pin_cohort(tmp_path,tamper):
    text=(ROOT/'ops/hermes-cron-scripts/glp_affiliate_reconciliation_current_base.sh').read_text()
    expected=re.search(r"scripts/glp_affiliate_wire_backlog_worker.py'\]='([a-f0-9]{64})'",text)[1]
    raw=(ROOT/'scripts/glp_affiliate_wire_backlog_worker.py').read_bytes()
    assert expected==hashlib.sha256(raw).hexdigest()
    worker=tmp_path/'worker.py';worker.write_bytes(raw+(b'\n' if tamper else b''))
    loop=text[text.index('for b2_path in'):text.index('\n# Resolve every approved')]
    script="declare -A b2_pins=(['"+str(worker)+"']='"+expected+"')\n"+loop
    run=subprocess.run(['bash','-c',script],capture_output=True,text=True)
    assert run.returncode==(1 if tamper else 0)
    assert ('b2_payload_hash_drift' in run.stdout)==tamper

def test_successor_preserves_entire_historical_wrapper_except_worker_literal():
    historical=(ROOT/'ops/hermes-cron-scripts/glp_affiliate_reconciliation_honesty.sh').read_bytes()
    successor=(ROOT/'ops/hermes-cron-scripts/glp_affiliate_reconciliation_current_base.sh').read_bytes()
    old=b'1f6988537a04f9b0813240a7c637cdd1bba840f61155b6af2717dbe016417c6a'
    new=b'16cac080089a5b75da8ab3fba6d875bdd30a46bee545e0dd751dc97ed30e1108'
    assert hashlib.sha256(historical).hexdigest()=='99bcc4cca7c63f41e3546bb0e470976907027557bf1e7b89d1a0dbb8aaebcfb4'
    assert historical.count(old)==1
    assert successor==historical.replace(old,new)
    assert hashlib.sha256(successor).hexdigest()=='cbc82eb6b6b018186ebf913ee06bb265d05f29262f031d922ea3c128da00c433'

