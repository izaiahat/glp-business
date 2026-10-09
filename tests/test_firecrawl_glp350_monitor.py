from test_firecrawl_integration import ROOT, fc
import importlib.util
import json
from pathlib import Path

def test_monitor_changes_to_review_queue(tmp_path):
    import glp_firecrawl_monitors as monitor
    urls = monitor.targets({'providers': [{'display_name': 'Example', 'field_sources': {'pricing_and_inclusions': ['https://example.org/pricing']}}, {'display_name': 'Fella', 'field_sources': {'pricing_and_inclusions': ['https://fella.example']}}]})
    assert urls == ['https://example.org/pricing']
    detail = {'id': 'check1', 'monitorId': 'monitor1', 'pages': [{'id': 'p1', 'url': urls[0], 'status': 'changed', 'diff': {'text': 'old to new'}}]}
    assert monitor.intake(detail, tmp_path / 'queue') == 1
    assert monitor.intake(detail, tmp_path / 'queue') == 0
    row = json.loads((tmp_path / 'queue').read_text())
    assert row['status'] == 'REVIEW_REQUIRED_NOT_ADOPTED' and row['detected_status'] == 'changed'
