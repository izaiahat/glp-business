import hashlib
import json
import re
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
CAPTURE=ROOT/'ops/unreviewed-local-edits/maxbounty-20261001'

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def test_capture_is_exact_without_replacing_immutable_predecessor():
    assert sha(CAPTURE/'glp_maxbounty_capacity_resume.sh')=='ba9fc895f657f0a1290522b10f2c024427d08dddfd3ec0e8579010092ced2d86'
    assert sha(CAPTURE/'glp_maxbounty_capacity_monitor.pins.json')=='114db70487e59a819e7913f12698176775d6d97a6340c801794c0f01cec3a920'
    assert sha(ROOT/'ops/hermes-cron-scripts/glp_maxbounty_capacity_resume.sh')=='b0e7d2770fcc5918aa5eaeef122bbede96caee8e68c28fa233bbcb7349215222'
    assert sha(ROOT/'ops/maxbounty-pin-predecessor.json')=='d4bdb54ffc4afa3ba3fbefd9a126688e3fff5cb1b6e5959ed802934408aedc59'

def test_capture_matches_reviewed_producer_emission():
    import importlib.util
    spec=importlib.util.spec_from_file_location('captured_maxbounty_producer',ROOT/'scripts/maxbounty_pin_release.py')
    assert spec is not None and spec.loader is not None
    producer=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(producer)
    sources={rel:sha(ROOT/rel) for rel in producer.SOURCES}
    manifest,wrapper=producer.stage_pins(
        (ROOT/'ops/maxbounty-pin-predecessor.json').read_bytes(),
        (ROOT/'ops/hermes-cron-scripts/glp_maxbounty_capacity_resume.sh').read_bytes(),sources)
    assert manifest==(CAPTURE/'glp_maxbounty_capacity_monitor.pins.json').read_bytes()
    assert wrapper==(CAPTURE/'glp_maxbounty_capacity_resume.sh').read_bytes()
    pins=json.loads(manifest)['files']
    assert '/home/ubuntu/business/scripts/glp_maxbounty_fresh_probe.py' in pins
