import importlib.util
from pathlib import Path
import pytest

P = Path(__file__).resolve().parents[1] / 'ops/hermes-cron-scripts/register_glp_seo_updater.py'
s = importlib.util.spec_from_file_location('register_updater', P)
assert s is not None and s.loader is not None
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)

def test_projection_keeps_peer_metadata_and_inputs_immutable():
    old = {'entries': [{'job_id': 'peer', 'route': 'old'}], 'entry_count': 1}
    generated = {'entries': [{'job_id': 'peer', 'route': 'unrelated-change'}, {'job_id': m.JOB, 'route': 'approved'}]}
    result = m.project(old, generated)
    assert result['entries'][0] == old['entries'][0]
    assert result['entry_count'] == 2
    assert old['entry_count'] == 1
    assert m.project(result, generated) == result

@pytest.mark.parametrize('rows', [[], [{'job_id': m.JOB}, {'job_id': m.JOB}]])
def test_projection_refuses_missing_or_duplicate_declaration(rows):
    with pytest.raises(ValueError, match='not_unique'):
        m.project({'entries': []}, {'entries': rows})
