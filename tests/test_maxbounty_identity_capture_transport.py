import importlib.util
from pathlib import Path
import sys
import pytest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))

def load_helper():
    spec=importlib.util.spec_from_file_location('identity_capture_candidate',ROOT/'scripts/glp_maxbounty_identity_capture.py')
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_official_transport_replaces_deleted_credentials(monkeypatch):
    from maxbounty_publisher_api import MaxBountyPublisherAPI
    calls=[]
    record={'success':True,'campaign_id':32854,'details':{'name':'offline transport fixture'}}
    class API:
        def authenticate(self):calls.append('authenticate')
        def campaign(self,pid):calls.append(('campaign',pid));return 200,record,'fixture-hash'
        def close(self):calls.append('close')
    monkeypatch.setattr(MaxBountyPublisherAPI,'from_environment',classmethod(lambda cls:API()))
    helper=load_helper()
    assert helper.fetch_campaign('32854')==record
    assert calls==['authenticate',('campaign','32854'),'close']


def test_official_identity_failures_close_transport(monkeypatch):
    from maxbounty_publisher_api import MaxBountyPublisherAPI
    cases=[(404,None,'maxbounty_campaign_http:404'),
           (200,{'success':False,'campaign_id':32854},'maxbounty_campaign_readback_refused'),
           (200,{'success':True,'campaign_id':True},'maxbounty_campaign_identity_missing_or_mismatch'),
           (200,{'success':True,'campaign_id':29207},'maxbounty_campaign_identity_missing_or_mismatch')]
    for status,record,reason in cases:
        calls=[]
        class API:
            def authenticate(self):calls.append('authenticate')
            def campaign(self,pid):return status,record,'fixture'
            def close(self):calls.append('close')
        monkeypatch.setattr(MaxBountyPublisherAPI,'from_environment',classmethod(lambda cls:API()))
        with pytest.raises(RuntimeError,match=reason):load_helper().fetch_campaign('32854')
        assert calls==['authenticate','close']


def test_invalid_id_never_opens_transport(monkeypatch):
    from maxbounty_publisher_api import MaxBountyPublisherAPI
    def forbidden(cls):raise AssertionError('transport opened before ID validation')
    monkeypatch.setattr(MaxBountyPublisherAPI,'from_environment',classmethod(forbidden))
    for pid in ('0','32854/x','abc',' 32854'):
        with pytest.raises(RuntimeError,match='maxbounty_campaign_id_invalid'):load_helper().fetch_campaign(pid)


def test_unavailable_credentials_fail_closed(monkeypatch):
    from maxbounty_publisher_api import MaxBountyPublisherAPI
    monkeypatch.setattr(MaxBountyPublisherAPI,'from_environment',classmethod(lambda cls:None))
    with pytest.raises(RuntimeError,match='maxbounty_api_credentials_unavailable'):load_helper().fetch_campaign('32854')


def test_authentication_failure_never_reads_campaign_and_closes(monkeypatch):
    from maxbounty_publisher_api import MaxBountyPublisherAPI
    calls=[]
    class API:
        def authenticate(self):raise RuntimeError('maxbounty_api_principal_unverified')
        def campaign(self,pid):raise AssertionError('campaign read after principal failure')
        def close(self):calls.append('close')
    monkeypatch.setattr(MaxBountyPublisherAPI,'from_environment',classmethod(lambda cls:API()))
    with pytest.raises(RuntimeError,match='maxbounty_api_principal_unverified'):load_helper().fetch_campaign('32854')
    assert calls==['close']
