from test_firecrawl_integration import ROOT, fc
import importlib.util
import json
from pathlib import Path

def test_gov_onboarding_does_not_grant_clearance():
    source = (ROOT / 'scripts/research_glp_batch2_fda_warning_provenance.py').read_text()
    assert '/v2/search/gov' in source and 'NO_MATCH_BOUNDED' in source
    assert 'DISCOVERY_REQUIRES_JURISDICTION_REVIEW_NOT_CLEARANCE' in source
    assert "--spec" in (ROOT / 'scripts/g6_r1a_stage5_research_fetch.py').read_text()
