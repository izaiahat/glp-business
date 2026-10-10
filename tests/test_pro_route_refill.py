"""One shortage regression: threshold, PLAN order, true round-two supply and no replay."""
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))


def test_dispatch_refill_threshold_second_round_and_ambiguous_note(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('p8_route', ROOT / 'scripts/pro_route.py')
    assert spec and spec.loader
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    monkeypatch.setattr(m, 'ROOT', tmp_path)
    monkeypatch.setattr(m.relay, 'job_state', lambda job: {})
    plan = tmp_path / 'plan.json'
    plan.write_text(json.dumps({'issues': [
        {'issue': 'GLP-334', 'order': 9}, {'issue': 'GLP-93', 'order': 5},
        {'issue': 'GLP-76', 'order': 4}, {'issue': 'GLP-358', 'order': -2}]}))
    control = m.directory() / 'CONTROL.json'; control.parent.mkdir(parents=True)
    control.write_text(json.dumps({'refill_plan': str(plan)}))
    monkeypatch.setattr(m, 'open_prep_issues', lambda: {'GLP-93', 'GLP-334', 'GLP-358'})
    posts = []
    def sender(body):
        # Receipt must exist BEFORE an outward effect, even if its reply is lost.
        assert json.loads((m.directory()/'REFILL-ALERT.json').read_text())['state']=='posting'
        posts.append(body)
        return {'message_id':'real-test-id','channel':'ops-alerts'}
    monkeypatch.setattr(m, 'send_refill_note', sender)
    for i in range(6):
        m.save({'job':str(i), 'issue':'GLP-93', 'state':'queued', 'attempts':[]})
    assert m.refill_alert()['state']=='stocked'
    assert posts==[]
    m.save({'job':'0','issue':'GLP-93','state':'running','attempts':[]})
    # A first-round success is NOT evidence of second-round prep.
    m.save({'job':'old','issue':'GLP-334','state':'complete','attempts':[]})
    body=m.refill_alert(); assert body['state']=='delivered'
    assert body['unsubmitted']==5
    assert body['issues_lacking_second_round']==['GLP-358','GLP-93','GLP-334']
    assert 'GLP-76' not in posts[0] and 'ops-alerts' in posts[0]
    m.refill_alert(); assert len(posts)==1
    # A restocked episode re-arms; explicit round-two marker excludes GLP-93.
    m.save({'job':'0','issue':'GLP-93','state':'queued','attempts':[]})
    m.refill_alert()
    prompt=m.directory()/'backlog'/'2001-GLP-93-auto.md'; prompt.parent.mkdir()
    prompt.write_text('# P8 SECOND-ROUND PREP — GLP-93\nUseful new question')
    m.save({'job':'0','issue':'GLP-93','state':'running','backlog_file':prompt.name,'attempts':[]})
    def uncertain(body):
        posts.append(body)
        raise TimeoutError('reply lost')
    monkeypatch.setattr(m,'send_refill_note',uncertain)
    assert m.refill_alert()['state']=='uncertain'
    assert json.loads((m.directory()/'REFILL-ALERT.json').read_text())['issues_lacking_second_round']==['GLP-358','GLP-334']
    m.refill_alert(); assert len(posts)==2
    m.save({'job':'0','issue':'GLP-93','state':'queued','attempts':[]})
    m.refill_alert()  # restocking cannot erase an ambiguous posting fence
    m.save({'job':'0','issue':'GLP-93','state':'running','attempts':[]})
    monkeypatch.setattr(m,'dispatch',lambda **kw: [])
    monkeypatch.setattr(m,'consume_outboxes',lambda: [])
    assert m.main(['dispatch','--max-actions','6'])==0
    assert json.loads(capsys.readouterr().out)['refill']['state']=='uncertain'
    assert len(posts)==2
    assert len(m.request_rows())==7  # no prompts minted, no job retries
