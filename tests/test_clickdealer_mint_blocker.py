"""Synthetic unsupported portal task; no provider access is claimed."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp_affiliate_daily_decisions_report as daily

def test_unsupported_mint_is_not_a_login_blocker():
    task = {'network': 'clickdealer', 'portal_supported': False, 'program_id': '178482:252017'}
    snapshot = daily.blocker_projection_sources({'applications': []}, {'tasks': [task]})
    store = daily.sync_blockers(snapshot, {'blockers': {}}, resolution_documents=[], intake_documents=[])
    row = store['blockers']['BLK-clickdealer-portal-access']
    assert row['operator_action_required'] is False
    assert row['status'] == 'machine_owned'
    assert row['programs'] == ['178482:252017']
    assert 'login' not in ' '.join(row['steps'])
