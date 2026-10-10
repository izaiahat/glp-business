"""Proof commands fail closed; retained evidence is never fabricated."""
import importlib.util
import json
from pathlib import Path
import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/fleet_plan_proof.py'


def load():
    spec = importlib.util.spec_from_file_location('fleet_proof', SOURCE)
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_missing_receipt_and_empty_ledger_refuse_without_writes(tmp_path, monkeypatch, capsys):
    m = load()
    monkeypatch.setattr(m, 'ROOT', tmp_path)
    assert m.main(['GLP-356']) == 3
    assert 'missing evidence' in capsys.readouterr().out
    p = tmp_path / 'data/firecrawl/operations.jsonl'
    p.parent.mkdir(parents=True)
    p.write_text('')
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    assert m.main(['GLP-350']) == 3
    assert 'ledger empty' in capsys.readouterr().out
    assert m.main(['GLP-68']) == 3
    assert 'no complete executable proof contract' in capsys.readouterr().out
    assert {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()} == before


def test_current_output_requires_natural_proof_and_does_not_claim_closure(tmp_path, monkeypatch):
    m = load()
    monkeypatch.setattr(m, 'ROOT', tmp_path)
    p = tmp_path / 'reports/ops/fleet-inbox/LAST-RUN.json'
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({'status':'COMPLETE','sources':{'dot':2,'native':4,'pro':24},
        'linear_mutations':0,'row_count':30}))
    with pytest.raises(ValueError, match='NATURAL-RUN.json'):
        m.fleet_ingestion()
    natural = tmp_path / m.PLAN / 'lanes/L2/NATURAL-RUN.json'
    natural.parent.mkdir(parents=True)
    natural.write_text(json.dumps({'job_id':'bbc82551321b','last_status':'ok',
        'sources':{'dot':2,'native':4,'pro':24},'linear_mutations':0,'rows':30}))
    result = m.fleet_ingestion()
    assert result['row_count'] == 30
    assert result['closure_postcondition'].startswith('pending GLP-355')
    assert 'closed_issue' not in result
    p.write_text(json.dumps({'status':'COMPLETE','sources':{'dot':2}, 'linear_mutations':0}))
    with pytest.raises(ValueError, match='three production sources'):
        m.fleet_ingestion()
