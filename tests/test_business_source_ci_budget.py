"""Keep the full exact-head CI gate within a measured bounded budget."""
from pathlib import Path
import yaml
ROOT=Path(__file__).resolve().parents[1]

def test_business_source_ci_keeps_full_gate_with_sufficient_bounded_time():
    workflow=yaml.safe_load((ROOT/'.github/workflows/business-source-ci.yml').read_text())
    job=workflow['jobs']['python-tests']
    assert 25 <= job['timeout-minutes'] <= 30
    names={step['name'] for step in job['steps'] if 'name' in step}
    assert {'Curated business control tests',
            'Publication-date binding regression (no deployment)',
            'Registered held-predecessor successor admission (isolated fixtures)',
            'Repair rollback and no-replay regressions (isolated fixtures)',
            'GLP-268 fresh per-job browser for affiliate callers (offline fakes; no browser launch)'}.issubset(names)
    assert workflow['concurrency']['cancel-in-progress'] is True
