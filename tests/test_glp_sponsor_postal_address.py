import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp_sponsor_campaign as c


def test_postal_address():
    cfg = json.loads((ROOT / 'ops/sponsor-machine/campaign.json').read_text())
    assert cfg['postal_address'] == '2108 N St, Ste N, Sacramento, California 95816, United States'
    assert cfg['postal_address'] in c.footer(cfg)
    assert 'Cheyenne' not in c.footer(cfg)
