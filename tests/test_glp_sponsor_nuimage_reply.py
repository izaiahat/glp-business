import datetime as dt
import sys
from pathlib import Path
import pytest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'scripts')]
import glp_sponsor_nuimage_reply as n

@pytest.mark.parametrize('clock,allowed',[
 ('2026-10-09T16:00:00-07:00',False),
 ('2026-10-10T09:00:00-07:00',False),
 ('2026-10-12T05:59:59-07:00',False),
 ('2026-10-12T06:00:00-07:00',True),
 ('2026-10-12T16:59:59-07:00',True),
 ('2026-10-12T17:00:00-07:00',False),
 ('2026-10-17T09:00:00-07:00',False),
])
def test_nuimage_not_before_monday_and_installed_weekday_window(monkeypatch,clock,allowed):
 monkeypatch.setattr(n.c,'now',lambda:dt.datetime.fromisoformat(clock))
 monkeypatch.setattr(n.c,'service',lambda *a:pytest.fail('pre-window reached auth'))
 if allowed:n.require_schedule()
 else:
  with pytest.raises(RuntimeError):n.run(send=True)

def test_pinned_reply_is_dash_free(monkeypatch):
 from email.message import EmailMessage
 message=EmailMessage();message['From']='partners@glpclinicscout.com'
 message.set_content('A sponsorship follow up for GLP Clinic Scout.\n')
 n.assert_raw_rfc822_dash_free(message.as_bytes(),context='synthetic-approved-reply')
 message.set_content('A sponsorship follow-up.\n')
 with pytest.raises(Exception):n.assert_raw_rfc822_dash_free(message.as_bytes(),context='synthetic-rejected-reply')
