"""Static candidate pin closure; never execute the reporting wrapper or job."""
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
LIVE = Path('/home/ubuntu/business')
MANIFEST = ROOT / 'ops/glp/reporting-v2/source-manifest.json'
WRAPPER = ROOT / 'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting.sh'


def test_reporting_source_pin_pair_is_coherent():
    raw = MANIFEST.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    manifest = json.loads(raw)
    assert manifest['schema'] == 'reporting-source-pins-v1'
    host_only = {
        'scripts/glp_network_revenue_import_v2.py': '0fc7e127a2e68fc3f283983b334bd320d0d3beec320d3dabb4089ee702b0386c',
        'scripts/marketplace_adapters/cj.py': '16eb39ee1d04c3d3e08e38547080273c45ea74f98ee5e916bd1d63cd5a9478db',
    }
    assert len(manifest['files']) == 8
    for absolute, expected in manifest['files'].items():
        rel = Path(absolute).relative_to(LIVE)
        if str(rel) in host_only:
            assert expected == host_only[str(rel)]  # excluded from curated checkout
        else:
            assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == expected
    text = WRAPPER.read_text()
    shell_pin = re.findall(r'^check_source ops/glp/reporting-v2/source-manifest\.json ([0-9a-f]{64})$', text, re.M)
    job_pin = re.findall(r'^  --manifest-sha256 ([0-9a-f]{64}) \\$', text, re.M)
    assert shell_pin == job_pin == [digest]
    assert 'check_source ops/glp/reporting-v2/identity-registry.json 0f46cfb248f1d20a7c976ed7baec0c6d816a3371da95bdbc0bca8e21c96cf1f8' in text
