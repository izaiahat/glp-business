"""Operator-approved GLP-327 economics must reach every sender render."""
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp_sponsor_campaign as c


def test_approved_prices_reach_all_variants(monkeypatch):
    config=json.loads((ROOT/'ops/sponsor-machine/campaign.json').read_text())
    copy=json.loads((ROOT/'ops/sponsor-machine/copy.json').read_text())
    approved={'currency':'USD','initial_total':750,'setup':250,'initial_placement':500,
              'term_hours':720,'optional_same_scope_renewal':500,'auto_charge':False}
    assert config['pricing']==approved
    assert copy['pricing']==approved
    monkeypatch.setattr(c,'ROOT',ROOT)
    # Relationship freshness is independent of frozen price rendering.
    import glp_sponsor_readiness as readiness
    monkeypatch.setattr(readiness,'relationship',lambda *args:{'variant':'established_affiliate','network':'Trackdesk'})
    for variant in ('cold','established_affiliate'):
        row={'email':'partnerships@example.com','host':'example.com','organization':'Example',
             'relationship':{'variant':variant,'network':'Trackdesk','evidence':{}}}
        for stage in range(3):
            rendered=c.render(row,stage,config)
            assert '$750' in rendered['body'],(variant,stage)
            assert '$500' in rendered['body'],(variant,stage)
            assert '$250 placement' not in rendered['body']
            assert '$250 for an optional renewal' not in rendered['body']
            assert 'Sacramento, California 95816' in rendered['body']
            assert 'no automatic' in rendered['body'].lower()
    assert '$750' in copy['interested_next_step']['body']
    assert 'renewal is $500' in copy['interested_next_step']['body']
    assert not config['send_enabled']  # prices alone never create send authority


def test_sponsor_weekday_business_hours_refuse_at_transport(monkeypatch):
    import datetime as dt
    import pytest
    # Boundaries in Pacific time, including the user's Friday-evening stop.
    cases=[('2026-10-09T05:59:59-07:00',False),
           ('2026-10-09T06:00:00-07:00',True),
           ('2026-10-09T16:59:59-07:00',True),
           ('2026-10-09T17:00:00-07:00',False),
           ('2026-10-09T18:00:00-07:00',False),
           ('2026-10-10T09:00:00-07:00',False),
           ('2026-10-11T09:00:00-07:00',False),
           ('2026-10-12T09:00:00-07:00',True),
           ('2026-11-02T06:00:00-08:00',True)]
    for value,eligible in cases:
        at=dt.datetime.fromisoformat(value)
        monkeypatch.setattr(c,'now',lambda:at)
        if eligible:
            c.require_sponsor_send_window()
        else:
            with pytest.raises(RuntimeError,match='outside_approved_weekday_06_17_PT_send_window'):
                c.send_message(None,{}, {}, [])
