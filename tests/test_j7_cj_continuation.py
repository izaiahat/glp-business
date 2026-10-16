"""Synthetic CJ partition continuation; never contacts a provider."""
import json
import time
from pathlib import Path
import pytest
import glp_affiliate_weekly_reporting_j7 as reporting


def test_continuation_and_dispatch_are_pinned_by_current_registered_manifest():
    import hashlib
    root=Path(__file__).resolve().parents[1]
    pins=json.loads((root/'ops/glp/reporting-v2/source-manifest-j7.json').read_text())['files']
    for name in ['affiliate_cj_checkpoint.py','affiliate_network_access_j7.py','glp_affiliate_weekly_reporting_j7.py']:
        assert pins['/home/ubuntu/business/scripts/'+name] == hashlib.sha256((root/'scripts'/name).read_bytes()).hexdigest()


class Wire:
    def __init__(self, status=200, complete=True):
        self.calls = 0
        self.status = status
        self.complete = complete
    def request(self, method, url, **kwargs):
        from marketplace_adapters._base import json_response
        self.calls += 1
        assert method == 'POST' and url == 'https://commissions.api.cj.com/query'
        return json_response({'data':{'publisherCommissions':{
            'count':0,'payloadComplete':self.complete,'records':[]}}}, status=self.status)


def read(wire, path, **kwargs):
    return reporting.collect_cj(wire, 'SYNTHETIC_NOT_SECRET', kwargs.pop('account', '334000'),
        '2026-09-01T00:00:00+00:00', '2026-09-22T00:00:00+00:00',
        deadline=kwargs.pop('deadline', time.monotonic()+60), max_requests=2,
        resume_path=path, **kwargs)


@pytest.mark.parametrize('case', ['checksum', 'account', 'stale', 'permissions', 'symlink'])
def test_untrusted_or_wrong_scope_checkpoint_denied_without_io(tmp_path, case):
    from affiliate_cj_checkpoint import digest
    from datetime import datetime, timezone, timedelta
    path = tmp_path/'cursor.json'
    wire = Wire()
    read(wire, path)
    kwargs = {}
    if case in {'checksum','stale'}:
        doc = json.loads(path.read_text())
        if case == 'checksum':
            doc['envelope']['state']['accepted_count'] += 1
        else:
            doc['envelope']['created_at'] = (datetime.now(timezone.utc)-timedelta(days=9)).isoformat()
            doc['sha256'] = digest(doc['envelope'])
        path.write_text(json.dumps(doc))
    elif case == 'account':
        kwargs['account'] = '334999'
    elif case == 'permissions':
        path.chmod(0o644)
    elif case == 'symlink':
        target=tmp_path/'target.json'
        path.rename(target)
        path.symlink_to(target)
    with pytest.raises((ValueError,OSError)):
        read(wire, path, **kwargs)
    assert wire.calls == 2


@pytest.mark.parametrize('status', [401,403,429,500])
def test_error_never_retries_or_certifies_on_resume(tmp_path, status):
    from affiliate_network_access_j6 import ReadTransport
    wire = Wire(status=status)
    transport = ReadTransport(wire, max_requests=2)
    path = tmp_path/'cursor.json'
    first = read(transport,path)
    assert wire.calls == 1 and not first[-1]['payload_complete']
    assert first[0]['http_status'] == status
    wire.status = 200
    second = read(transport,path)
    assert wire.calls == 1 and not second[-1]['payload_complete']
    assert second[-1]['record_count'] is None


def test_incomplete_response_split_survives_budget(tmp_path):
    wire = Wire(complete=False)
    path = tmp_path/'cursor.json'
    first = read(wire,path)
    assert first[-1]['payload_complete'] is False
    state=json.loads(path.read_text())['envelope']['state']
    assert state['queue'] and state['incomplete_hashes']
    assert first[0]['reason'] == 'SUPERSEDED_BY_BOUNDED_TIME_PARTITIONS'


def test_expired_deadline_makes_no_call_or_complete_claim(tmp_path):
    wire = Wire()
    pages=read(wire,tmp_path/'cursor.json',deadline=0)
    assert wire.calls == 0 and not pages[-1]['payload_complete']


def test_registered_dispatch_uses_saved_month_before_moving_windows(tmp_path, monkeypatch):
    from affiliate_network_access_j7 import weekly_collectors
    from marketplace_adapters import cj
    monkeypatch.setattr(cj,'_secret',lambda: ('SYNTHETIC_NOT_SECRET','334000','TEST_WEBSITE'))
    wire=Wire()
    collect=weekly_collectors(['cj'],wire,cj_resume_root=tmp_path)['cj']
    window={'last_closed_month_start':'2026-09-01T00:00:00+00:00',
            'last_closed_month_end':'2026-09-22T00:00:00+00:00',
            'week_start':'2026-10-05T00:00:00+00:00',
            'end_exclusive':'2026-10-12T00:00:00+00:00',
            'lookback_start':'2026-07-14T00:00:00+00:00'}
    first=collect(window,time.monotonic()+60,2,sink=lambda row:None)
    assert wire.calls == 2 and first[0]['coverage'] == 'PARTIAL'
    assert first[0]['period_purpose'] == 'closed_month'
    second=collect(window,time.monotonic()+60,1,sink=lambda row:None)
    assert wire.calls == 3 and second[0]['coverage'] == 'COMPLETE'
    assert second[0]['account_scope_complete'] is False
    assert second[0]['financial_classification'] == 'UNKNOWN'
    third=collect(window,time.monotonic()+60,0,sink=lambda row:None)
    assert wire.calls == 3 and third[0]['request_count'] == 0
    assert third[0]['coverage'] == 'COMPLETE'


def test_three_pages_two_runs_and_completed_replay_noop(tmp_path):
    class Wire:
        calls = 0
        queries = []
        def request(self, method, url, **kwargs):
            self.calls += 1
            self.queries.append(kwargs['json_body']['query'])
            box = {'count': 0, 'payloadComplete': True, 'records': []}
            class Response:
                status = 200
                body = json.dumps({'data': {'publisherCommissions': box}}).encode()
                def json(self):
                    return json.loads(self.body)
            return Response()
    wire = Wire()
    cursor = tmp_path / 'cursor.json'
    def run():
        return reporting.collect_cj(wire, 'SYNTHETIC_NOT_SECRET', '334000',
            '2026-09-01T00:00:00+00:00', '2026-09-22T00:00:00+00:00',
            deadline=time.monotonic()+60, max_requests=2, resume_path=cursor)
    first = run()
    assert wire.calls == 2 and cursor.exists()
    assert first[-1]['payload_complete'] is False
    assert first[-1]['record_count'] is None
    state = cursor.read_bytes()
    assert b'SYNTHETIC_NOT_SECRET' not in state
    second = run()
    assert wire.calls == 3 and len(set(wire.queries)) == 3
    assert second[-1]['payload_complete'] is True
    assert second[-1]['request_count'] == 1
    assert second[-1]['record_count'] == 0
    assert second[-1]['account_scope_complete'] is False
    assert second[-1]['commission_minor'] is None and second[-1]['cash_minor'] is None
    completed = cursor.read_bytes()
    replay = run()
    assert wire.calls == 3
    assert replay[-1]['request_count'] == 0
    assert replay[-1]['payload_complete'] is True
    assert cursor.read_bytes() == completed
    assert cursor.stat().st_mode & 0o777 == 0o600
