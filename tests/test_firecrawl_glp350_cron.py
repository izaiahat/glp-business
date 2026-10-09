from test_firecrawl_integration import ROOT, fc

def test_covered_cron_declarations_and_request_cap():
    import subprocess
    for name in ('glp_reddit_daily_scout_wrapper.sh', 'affiliate_site_evidence_daily.sh'):
        path = ROOT / 'ops/hermes-cron-scripts' / name
        subprocess.run(['bash', '-n', str(path)], check=True)
        source = path.read_text()
        assert 'CRON_DISCORD_HOUSE_STYLE_V1' in source and 'CRON_DISCORD_OUTPUT_DECLARATION_V1=' in source
    affiliate = (ROOT / 'ops/hermes-cron-scripts/affiliate_site_evidence_daily.sh').read_text()
    assert '--max-requests 40' in affiliate
    assert 'affiliate_site_evidence_card.py' in affiliate
