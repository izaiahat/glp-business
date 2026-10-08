"""Offline caller regressions; synthetic transport, never authentication proof."""
import contextlib
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(monkeypatch, name, relative):
    for package, directory in (("scripts", "scripts"), ("scripts.marketplace_adapters", "scripts/marketplace_adapters"), ("marketplace_adapters", "scripts/marketplace_adapters")):
        module = types.ModuleType(package)
        module.__path__ = [str(ROOT / directory)]
        monkeypatch.setitem(sys.modules, package, module)
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_xt_shell_blocks_without_reading_profiles_or_state(monkeypatch, capsys):
    lane = load(monkeypatch, "xt_lane_candidate", "scripts/glp_xentraffic_enumerate.py")
    monkeypatch.setattr(lane, "load_state", lambda: pytest.fail("state read before admission"))
    assert lane.main(["--queue", "off"]) == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "BLOCKED"
    assert receipt["summary"]["offers"] is None
    assert receipt["effects"]["provider_mutations"] == 0


def test_xt_legacy_constructor_never_reads_access(monkeypatch):
    xt = load(monkeypatch, "xt_candidate", "scripts/marketplace_adapters/xentraffic.py")
    monkeypatch.setattr(xt, "load_access", lambda *a: pytest.fail("profile file read"))
    with pytest.raises(xt.SessionError, match="fresh_ai_browser_vault_stage_required"):
        xt.PortalSession()


@pytest.mark.parametrize("payload", [
    {"paging": {"total_count": 2}, "offers": [{"network_offer_id": 1}, {"network_offer_id": 1}]},
    {"paging": {}, "offers": []},
    {"paging": {"total_count": 1}, "offers": []},
])
def test_xt_catalog_cannot_complete_missing_duplicate_or_truncated_rows(monkeypatch, payload):
    xt = load(monkeypatch, "xt_candidate", "scripts/marketplace_adapters/xentraffic.py")
    with pytest.raises(xt.SessionError):
        xt.list_offers(lambda *a, **kw: payload)


@pytest.mark.parametrize('read_only', ['0', '1'])
def test_xt_normal_main_consumes_fresh_reader_and_preserves_no_effect_hold(monkeypatch, capsys, tmp_path, read_only):
    lane = load(monkeypatch, "xt_lane_candidate", "scripts/glp_xentraffic_enumerate.py")
    calls = []
    class Reader:
        fresh = True
        owned_session = "OFFLINE_FIXTURE"
        def api(self, path, **kwargs):
            calls.append(path)
            return {"paging": {"total_count": 1}, "offers": [{"network_offer_id": 10, "name": "Fixture wellness", "offer_status": "active"}]}
        def start(self): calls.append("principal")
    monkeypatch.setattr(lane, "load_state", lambda: {"offers": []})
    monkeypatch.setattr(lane, "RUN_ROOT", tmp_path)
    monkeypatch.setattr(lane, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(lane, "repo_rel", lambda p: str(p))
    assert lane.main(["--queue", "off", "--heartbeat", "0", '--read-only', read_only], fresh_reader=Reader()) == 0
    assert (tmp_path / 'state.json').exists() is (read_only == '0')
    receipt = json.loads(capsys.readouterr().out)
    assert calls[0] == "principal" and len(calls) == 2
    assert receipt["summary"]["completeness"] == "COMPLETE"
    assert receipt["read_method"] == "fresh_ai_browser_vault"
    assert receipt["effects"]["provider_mutations"] == 0


def test_mb_default_enumeration_never_instantiates_legacy_session(monkeypatch):
    legacy = types.ModuleType("marketplace_adapters.portal_lane")
    legacy.PortalSession = lambda *a, **kw: pytest.fail("legacy session created")
    legacy.PORTALS = {"maxbounty": object()}
    monkeypatch.setitem(sys.modules, "marketplace_adapters.portal_lane", legacy)
    mb = load(monkeypatch, "marketplace_adapters.maxbounty", "scripts/marketplace_adapters/maxbounty.py")
    result = mb.login_and_enumerate("https://affiliates.maxbounty.com/login", run_id="fixture")
    assert result["status"] == "BLOCKED_LOGIN"
    assert result["enumerated"] is None
    assert result["login"]["reason"] == "fresh_ai_browser_vault_stage_required"


def test_mb_fresh_enumeration_verifies_principal_then_real_discovery(monkeypatch):
    legacy = types.ModuleType("marketplace_adapters.portal_lane")
    legacy.PortalSession = lambda *a, **kw: pytest.fail("legacy session created")
    monkeypatch.setitem(sys.modules, "marketplace_adapters.portal_lane", legacy)
    mb = load(monkeypatch, "marketplace_adapters.maxbounty", "scripts/marketplace_adapters/maxbounty.py")
    calls = []
    class Driver:
        fresh = True
        owned_session = "OFFLINE_FIXTURE"
        url = ""
        def goto(self, url): self.url = url; calls.append(url)
        def body_text(self): return "ops@glpclinicscout.com\nDashboard Campaigns"
        def title(self): return "MaxBounty"
    driver = Driver()
    monkeypatch.setattr(mb, "enumerate_search_categories", lambda current, **kw: (calls.append("discovery") or {"complete": False, "coverage": {}, "programs": [], "enumerated": None}))
    result = mb.login_and_enumerate("https://affiliates.maxbounty.com/login", run_id="fixture", fresh_driver=driver)
    assert calls == ["https://affiliates.maxbounty.com/settings", "https://affiliates.maxbounty.com/dashboard", "discovery"]
    assert result["status"] == "INCOMPLETE"
    assert result["read_method"] == "fresh_ai_browser_vault"


def test_mb_normal_entry_refuses_before_sheet_or_output_io(monkeypatch, capsys):
    # Real module, external-only dependency stub, no host-module fallback.
    fake = types.ModuleType("marketplace_adapters.maxbounty")
    fake.portal_lane = types.SimpleNamespace(PortalSession=lambda: pytest.fail("legacy"))
    monkeypatch.setitem(sys.modules, "marketplace_adapters.maxbounty", fake)
    gate = types.ModuleType("ops.affiliate_apply_gate")
    package = types.ModuleType("ops"); package.__path__ = [str(ROOT / "ops")]
    monkeypatch.setitem(sys.modules, "ops", package)
    monkeypatch.setitem(sys.modules, "ops.affiliate_apply_gate", gate)
    lane = load(monkeypatch, "mb_lane_candidate", "scripts/glp_maxbounty_lane.py")
    monkeypatch.setattr(lane, "read_sheet_snapshot", lambda: pytest.fail("Sheet read before fresh stage"))
    assert lane.main(["--mode", "enumerate", "--queue", "off"]) == 2
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "BLOCKED"
    assert receipt["reason"] == "fresh_ai_browser_vault_stage_required"
    assert receipt["effects"]["applications_submitted"] == 0


def test_mb_normal_main_consumes_driver_and_preserves_unknown_counts(monkeypatch, tmp_path, capsys):
    lane = load(monkeypatch, 'mb_fresh_main_candidate', 'scripts/glp_maxbounty_lane.py')
    driver = types.SimpleNamespace(fresh=True, owned_session='SYNTHETIC_ONLY')
    seen = []
    def enumerate_once(url, **kwargs):
        seen.append(kwargs['fresh_driver'])
        return {'status':'INCOMPLETE', 'complete':False, 'programs':[],
                'enumerated':None, 'discovered_links':None, 'coverage':{},
                'blockers':[{'error':'approved_account_scope_unproven'}]}
    monkeypatch.setattr(lane.mb, 'login_and_enumerate', enumerate_once)
    monkeypatch.setattr(lane, 'RUN_ROOT', tmp_path)
    monkeypatch.setattr(lane, 'repo_rel', lambda p: str(p))
    monkeypatch.setattr(lane, 'read_sheet_snapshot', lambda: {'login_url':'https://affiliates.maxbounty.com/login'})
    monkeypatch.setattr(lane, 'previous_approval_baseline', lambda *a: {})
    monkeypatch.setattr(lane, 'update_coupon_model', lambda *a: pytest.fail('effect after incomplete read'))
    assert lane.main(['--mode','enumerate','--queue','off','--run-id','fixture'], fresh_driver=driver) == 2
    receipt = json.loads((tmp_path/'run-fixture'/'run-receipt.json').read_text())
    assert seen == [driver]
    assert receipt['enumeration']['approved'] is None
    assert receipt['effects']['applications_submitted'] == 0


def test_xt_raw_driver_exception_does_not_enter_receipt(monkeypatch, capsys):
    lane = load(monkeypatch, 'xt_safe_exception_candidate', 'scripts/glp_xentraffic_enumerate.py')
    def fail(): raise RuntimeError('SYNTHETIC_PRIVATE_DIAGNOSTIC')
    driver = types.SimpleNamespace(fresh=True, owned_session='SYNTHETIC_ONLY', start=fail)
    monkeypatch.setattr(lane, 'load_state', lambda: {})
    assert lane.main(['--queue','off'], fresh_reader=driver) == 2
    output = capsys.readouterr().out
    assert 'SYNTHETIC_PRIVATE_DIAGNOSTIC' not in output
    assert json.loads(output)['summary']['offers'] is None


def test_mb_read_only_entry_stops_before_downstream_effects(monkeypatch, tmp_path, capsys):
    lane = load(monkeypatch, 'mb_read_only_candidate', 'scripts/glp_maxbounty_lane.py')
    driver = types.SimpleNamespace(fresh=True, owned_session='SYNTHETIC_ONLY')
    seen = []
    def enumerate_once(url, **kwargs):
        assert kwargs['fresh_driver'] is driver
        return {'status':'COMPLETE', 'complete':True, 'programs':[], 'enumerated':0,
                'discovered_links':0, 'coverage':{'schema':'SYNTHETIC'}, 'read_method':'SYNTHETIC'}
    monkeypatch.setattr(lane.mb, 'login_and_enumerate', enumerate_once)
    monkeypatch.setattr(lane.mb.discovery, 'require_search_coverage', lambda data: seen.append('oracle'))
    monkeypatch.setattr(lane, 'RUN_ROOT', tmp_path)
    monkeypatch.setattr(lane, 'repo_rel', lambda p: str(p))
    monkeypatch.setattr(lane, 'read_sheet_snapshot', lambda: {'login_url':'https://affiliates.maxbounty.com/login'})
    monkeypatch.setattr(lane, 'previous_approval_baseline', lambda *a: {})
    for name in ('bind_required_site_identities', 'update_coupon_model', 'draft_applications', 'queue_all', 'heartbeat'):
        monkeypatch.setattr(lane, name, lambda *a: pytest.fail('downstream effect in read-only mode'))
    assert lane.main(['--mode','read-only','--queue','off','--run-id','fixture'], fresh_driver=driver) == 0
    receipt = json.loads((tmp_path/'run-fixture'/'run-receipt.json').read_text())
    assert seen == ['oracle']
    assert receipt['status'] == 'PASS_READ_ONLY'
    assert all(value == 0 for value in receipt['effects'].values())
