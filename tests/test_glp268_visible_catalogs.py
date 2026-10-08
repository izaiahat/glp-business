"""Synthetic fixtures; these tests are not live authentication proof."""
import json
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from scripts.marketplace_adapters import xentraffic as xt
import xentraffic_fresh_catalog as xen
import maxbounty_fresh_catalog as mbread
import affiliate_fresh_browser as fresh


class Visible:
    fresh = True
    owned_session = 'synthetic-only'
    def __init__(self):
        self.url = xt.PORTAL_URL + '/offers'
        self.visits = []
    def goto(self, url):
        self.url = url
        self.visits.append(url)
    def body_text(self):
        return 'ops@glpclinicscout.com\nPartner ID: 4384\nOffer ID: 7\nExample\nDescription\nsemaglutide\nTerms\nUnknown'
    def visible_tables(self):
        return {'tables': [{'headers': ['Offer ID','Name','Status'],
                           'rows': [{'cells': ['7','Example','Active']}]}],
                'pagers': [{'text': 'Showing 1 to 1 of 1', 'links': []}]}
    def maxbounty_search_snapshot(self):
        return {'rows': [{'text': 'Example','cells': ['Example'],
                         'links': [{'name': 'Example','href': 'https://affiliates.maxbounty.com/campaign/7','program_id': '7'}]}],
                'category_label': 'Health (1)','buttons': [],'empty_marker_present': False}
    def maxbounty_campaign_sections(self):
        return {'title_headings': ['Example'],'sections': {'Campaign Description': {'text': 'semaglutide','links': []}}}
    def buttons(self):
        return ['Build Tracking Link']


def test_xen_visible_projection_is_normal_caller_contract():
    driver = Visible()
    reader = xen.FreshVisibleCatalogReader(driver).start()
    entries, total = xt.list_offers(reader.api)
    assert total == 1 and entries[0]['network_offer_id'] == 7
    assert entries[0]['relationship']['offer_affiliate_status'] == ''
    detail = xt.offer_detail(reader.api, 7)
    assert detail['terms_and_conditions'] == '' and detail['tracking_url'] == ''
    assert not any('/go/' in url for url in driver.visits)


def test_xen_unknown_total_never_becomes_zero():
    driver = Visible()
    driver.visible_tables = lambda: {'tables': [{'headers': ['ID','Name','Status'],'rows': []}], 'pagers': []}
    reader = xen.FreshVisibleCatalogReader(driver).start()
    with pytest.raises(xt.SessionError, match='published_total_or_pager_unknown'):
        xt.list_offers(reader.api)


def test_mb_visible_page_receipt_not_approved_api_evidence(tmp_path):
    reader = mbread.FreshVisibleListingTransport(Visible(), evidence_dir=tmp_path)
    page = reader({'key':'health','kind':'category','value':'58'}, None)
    assert page['total'] == 1 and page['terminal'] is True
    assert page['evidence']['query_response'] is None
    receipt = next(tmp_path.iterdir())
    assert json.loads(receipt.read_text())['query_response'] is None
    approved = reader({'key':'approved','kind':'approved','value':''}, None)
    assert approved['total'] is None


def test_new_browser_expressions_are_fixed_reads_and_compile():
    for key in ('visible_tables','maxbounty_search','maxbounty_sections'):
        expression = fresh._READS[key]
        assert not any(word in expression for word in ('fetch(', '.value', 'localStorage', 'cookie', '.click(', 'password'))
        result = subprocess.run(['node','-e','new Function(process.argv[1]);', expression], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_isolated_cli_entries_are_blocked_before_authentication():
    for name in ('glp_xentraffic_enumerate.py','glp_maxbounty_lane.py'):
        result = subprocess.run([sys.executable, str(ROOT/'scripts'/name), '--queue','off'], cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == 2
        payload = json.loads(result.stdout)
        assert payload['status'] == 'BLOCKED'
        assert payload['reason'] == 'fresh_ai_browser_vault_stage_required'
        assert all(value == 0 for value in payload['effects'].values())
