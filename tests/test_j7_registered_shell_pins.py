import hashlib,re
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def test_registered_j7_shell_job_pin_matches_exact_imported_job_bytes():
    source=ROOT/'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_j7.sh'
    pins=dict(re.findall(r'^check_source (\S+) ([a-f0-9]{64})$',source.read_text(),re.M))
    target='scripts/glp_affiliate_weekly_reporting_j7_job.py'
    assert target in pins
    assert pins[target]==hashlib.sha256((ROOT/target).read_bytes()).hexdigest()
    manifest='ops/glp/reporting-v2/source-manifest-j7.json'
    assert pins[manifest]==hashlib.sha256((ROOT/manifest).read_bytes()).hexdigest()
