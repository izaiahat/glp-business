from test_firecrawl_integration import ROOT, fc
import importlib.util
import json
from pathlib import Path

def test_page_two_us_weekly_top_three():
    assert fc._options({'location': 'United States', 'tbs': 'qdr:w'})['location'] == 'United States'
    assert fc._options({'location': 'United States', 'tbs': 'qdr:w'})['tbs'] == 'qdr:w'
    spec = importlib.util.spec_from_file_location('serp', ROOT / 'systems/seo-blog-machine/scripts/refresh_topic_supply_firecrawl.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert module.MAX_TARGETS == 40 and module.RESULT_LIMIT == 3 and module.REFRESH_AFTER_HOURS == 168
    _, rows = module.normalized_results({'success': True, 'data': {'web': [{'url': 'https://example.org', 'markdown': 'Captured source'}]}})
    assert rows[0]['markdown'] == 'Captured source'
    assert '"location": "United States"' in Path(module.__file__).read_text()
