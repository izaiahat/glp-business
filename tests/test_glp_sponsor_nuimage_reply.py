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
    ('2026-10-13T06:00:00-07:00',False),
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


@pytest.fixture
def offline_draft(tmp_path, monkeypatch):
 from email.message import EmailMessage
 from unittest.mock import MagicMock
 import hashlib, base64
 monkeypatch.setattr(n.c,'INBOX_POLL_LOCK',tmp_path/'poll.lock')
 body='Hi Andreas,\n\nOur proposed sponsor pilot is $750.\n'
 def row(message, identifier, labels, timestamp='1'):
  return dict(id=identifier,threadId=n.THREAD,labelIds=labels,internalDate=timestamp,
              raw=base64.urlsafe_b64encode(message.as_bytes()).decode())
 inbound=EmailMessage();inbound['From']=n.TO;inbound['Message-ID']='<original@example.test>'
 inbound.set_content('Please send the proposal.\n')
 source=row(inbound,n.THREAD,['INBOX'])
 draft=EmailMessage();draft['From']=n.c.SENDER;draft['To']=n.TO
 draft['Subject']='Approved sponsor reply';draft['In-Reply-To']='<original@example.test>'
 draft.set_content(body)
 monkeypatch.setattr(n,'LANE',tmp_path);monkeypatch.setattr(n,'QUEUE',tmp_path/'queue.json')
 monkeypatch.setattr(n,'JOURNAL',tmp_path/'send.json');monkeypatch.setattr(n.c,'STATE',tmp_path)
 artifact=tmp_path/'GLP125-NuImage-reply.txt';artifact.write_text(body)
 monkeypatch.setattr(n,'ARTIFACT_SHA',n.c.digest(artifact))
 monkeypatch.setattr(n,'BODY_SHA',hashlib.sha256(body.encode()).hexdigest())
 n.c.save(n.QUEUE,dict(artifact_sha256=n.ARTIFACT_SHA,body_sha256=n.BODY_SHA,
  thread_id=n.THREAD,to=n.TO,source_rfc_message_id='<original@example.test>',
  subject='Approved sponsor reply',draft_id='fixture-draft'))
 monkeypatch.setattr(n.c,'now',lambda:dt.datetime.fromisoformat('2026-10-12T09:00:00-07:00'))
 monkeypatch.setattr(n.c,'read_local',lambda:dict(suppression=dict(complete=True,entries=[]),faults=[]))
 client=MagicMock();users=client.service.users.return_value
 users.settings.return_value.sendAs.return_value.list.return_value.execute.return_value={
  'sendAs':[dict(sendAsEmail=n.c.SENDER,verificationStatus='accepted')]}
 rows={n.THREAD:source}
 client.get_message.side_effect=lambda identifier,**kw:rows[identifier]
 client.get_thread.side_effect=lambda _:dict(messages=[dict(id=k) for k in rows])
 users.drafts.return_value.get.return_value.execute.side_effect=lambda **kw:dict(message=row(draft,'fixture-draft',['DRAFT']))
 monkeypatch.setattr(n.c,'service',lambda _:client)
 return draft, rows, row, users


def test_exact_draft_preflight_is_effect_free(offline_draft):
 _,_,_,users=offline_draft
 assert n.run(send=False)['status']=='PREFLIGHT_ONLY'
 users.drafts.return_value.send.assert_not_called()
 assert not n.JOURNAL.exists()

@pytest.mark.parametrize('change',['cc','bcc','extra_to','duplicate_to','html','later_stop','later_reply'])
def test_changed_envelope_or_new_reply_refuses_before_claim(offline_draft,change):
 from email.message import EmailMessage
 draft, rows, row, users=offline_draft
 if change in ('cc','bcc'):draft[change]='other@example.test'
 elif change=='extra_to':draft.replace_header('To',n.TO+', other@example.test')
 elif change=='duplicate_to':draft._headers.append(('To','other@example.test'))
 elif change=='html':draft.add_alternative('<p>Unapproved commercial terms.</p>',subtype='html')
 else:
  reply=EmailMessage();reply['From']=n.TO
  reply.set_content('STOP' if change=='later_stop' else 'Please wait until next month.')
  rows['later']=row(reply,'later',['INBOX'],'2')
 users.drafts.return_value.send.side_effect=lambda **kw:pytest.fail('unsafe draft reached fake transport')
 with pytest.raises(RuntimeError):n.run(send=True)
 users.drafts.return_value.send.assert_not_called()
 assert not n.JOURNAL.exists()

def test_uncertain_submission_is_never_replayed(offline_draft):
 _,_,_,users=offline_draft
 users.drafts.return_value.send.return_value.execute.side_effect=TimeoutError('unknown')
 with pytest.raises(TimeoutError):n.run(send=True)
 assert n.c.load(n.JOURNAL)['status']=='SUBMISSION_STARTED'
 with pytest.raises(RuntimeError,match='existing_or_uncertain_send_claim'):n.run(send=True)
 assert users.drafts.return_value.send.call_count==1


def test_send_consumes_validated_raw_not_mutable_draft_id(offline_draft):
 import base64
 draft,_,_,users=offline_draft
 expected=draft.as_bytes()
 def consume(**kwargs):
  assert kwargs['body']['message']['threadId']==n.THREAD
  assert base64.urlsafe_b64decode(kwargs['body']['message']['raw'])==expected
  raise TimeoutError('synthetic uncertain submission')
 users.drafts.return_value.send.side_effect=consume
 with pytest.raises(TimeoutError):n.run(send=True)
 assert n.c.load(n.JOURNAL)['status']=='SUBMISSION_STARTED'


@pytest.mark.parametrize('stop',['incomplete','fault','suppressed','clock_crossing','replay','wrong_inbound'])
def test_send_time_guards_refuse_without_submission(offline_draft,monkeypatch,stop):
 draft,rows,make_row,users=offline_draft
 if stop in ('incomplete','fault','suppressed'):
  monkeypatch.setattr(n.c,'read_local',lambda:dict(suppression=dict(complete=stop!='incomplete',
    entries=[{'email':n.TO}] if stop=='suppressed' else []),faults=['unknown'] if stop=='fault' else []))
 elif stop=='clock_crossing':
  clocks=iter(['2026-10-12T16:59:59-07:00','2026-10-12T16:59:59-07:00','2026-10-12T17:00:00-07:00'])
  monkeypatch.setattr(n.c,'now',lambda:dt.datetime.fromisoformat(next(clocks)))
 elif stop=='wrong_inbound':
  from email.message import EmailMessage
  wrong=EmailMessage();wrong['From']='other@example.test';wrong['Message-ID']='<original@example.test>'
  wrong.set_content('other inbound')
  rows[n.THREAD]=make_row(wrong,n.THREAD,['INBOX'])
 else:
  rows['sent']=make_row(draft,'sent',['SENT'],'2')
 if stop=='replay':assert n.run(send=True)['status']=='ALREADY_SENT_DO_NOT_RESEND'
 else:
  with pytest.raises(RuntimeError):n.run(send=True)
 users.drafts.return_value.send.assert_not_called()
 assert not n.JOURNAL.exists()


def test_overlapping_poll_finishes_before_nuimage_queue_read(offline_draft,monkeypatch):
 import fcntl,threading
 _,_,_,users=offline_draft
 released=threading.Event()
 load=n.c.load
 def stable_queue(path):
  assert released.is_set(),'Nu Image eligibility read raced canonical poll'
  with n.c.INBOX_POLL_LOCK.open('a') as other:
   with pytest.raises(BlockingIOError):fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
  return load(path)
 monkeypatch.setattr(n.c,'load',stable_queue)
 with n.c.INBOX_POLL_LOCK.open('a') as poll:
  fcntl.flock(poll,fcntl.LOCK_EX|fcntl.LOCK_NB)
  def finish():
   threading.Event().wait(0.3)
   released.set();fcntl.flock(poll,fcntl.LOCK_UN)
  worker=threading.Thread(target=finish);worker.start()
  try:result=n.run(send=False)
  finally:worker.join(3)
 assert result['status']=='PREFLIGHT_ONLY' and result['sends']==0
 users.drafts.return_value.send.assert_not_called()
 with n.c.INBOX_POLL_LOCK.open('a') as other:
  fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
