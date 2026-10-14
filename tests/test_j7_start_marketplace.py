"""Actual job -> main -> run; synthetic selection/wire, never a provider read.
Roster-name projection and prior cutoff from authentic execution
c324745842e14e75871e6f8f0bd40a1c, report SHA256
 dea5625a4f04bc9a3050ece973e5a16be3bf8f76ed3ad6e904251ccfa272f0eb.
This projection is NOT the original canonical roster or new financial evidence.
"""
import copy
import hashlib
import json
from pathlib import Path
import socket
import sys
import time
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import glp_affiliate_weekly_reporting_j7 as j7
import glp_affiliate_weekly_reporting_j7_job as job
from marketplace_adapters import _base as base

NAMES = ('admitad advidi affiliati affise-reach amazon-associates archer-affiliates '
         'avantlink awin bixgrow brandchamp buygoods cj clickbank clickdealer daisycon '
         'digistore24 digital-media-solutions direct dr-cash ebay-partner-network '
         'everflow flexoffers giddyup goaffpro gurumedia health-nutrition-affiliates '
         'healthtrader howl impact katalys levanta linkconnector linktrust markethealth '
         'maxbounty maxweb moreniche partnerize partnerstack perform-cb rakuten '
         'refersion sellhealth shareasale skimlinks sovrn takeads target-partners '
         'terraleads trackdesk tradedoubler uppromote walmart-affiliate-program '
         'webgains xentraffic yepads').split()
WINDOW = {'week_start': '2026-10-05T00:00:00+00:00',
          'end_exclusive': '2026-10-12T00:00:00+00:00',
          'lookback_start': '2026-07-14T00:00:00+00:00',
          'last_closed_month_start': '2026-09-01T00:00:00+00:00',
          'last_closed_month_end': '2026-10-01T00:00:00+00:00', 'reporting_timezone': 'UTC'}

class EffectDenied(BaseException):
    pass

@pytest.mark.parametrize('selector', [None, ['clickbank'], ['not-in-roster'],
                                    ['clickbank', 'cj'], ['clickbank', 'clickbank']])
def test_actual_entry_rotation_preserves_roster_periods_and_single_budget(monkeypatch, tmp_path, selector):
    def denied(*args, **kwargs):
        raise EffectDenied('unexpected_network_or_collector')
    monkeypatch.setattr(socket.socket, 'connect', denied)
    monkeypatch.setattr(socket, 'getaddrinfo', denied)
    monkeypatch.setattr(j7, 'verify_pins', lambda *args: 'OFFLINE_PIN_BOUNDARY_ONLY')
    monkeypatch.setattr(j7, 'windows', lambda *args: copy.deepcopy(WINDOW))
    roster = {'marketplace_count': 56, 'marketplaces': [{'marketplace': n} for n in NAMES]}
    original = copy.deepcopy(roster)
    rp, ip, mp = (tmp_path/n for n in ('roster.json', 'registry.json', 'manifest.json'))
    rp.write_text(json.dumps(roster))
    roster_bytes = rp.read_bytes()
    ip.write_text(json.dumps({'schema': 'reporting-identity-registry-v1', 'identities': []}))
    # Job validates real owned dependencies; reporter's installed absolute pin
    # boundary is the explicit existing portable fixture seam, not acceptance.
    files = {str(ROOT/'scripts'/n): hashlib.sha256((ROOT/'scripts'/n).read_bytes()).hexdigest()
             for n in ('glp_affiliate_weekly_reporting_j7_job.py', 'glp_affiliate_weekly_reporting_j7.py',
                       'glp_network_revenue_v2.py', 'glp_network_revenue_import_v2.py')}
    mp.write_text(json.dumps({'files': files}))
    report_root = tmp_path/'reports'
    # Authentic prior cutoff projection retained as a sibling checkpoint. The
    # successor chooses explicitly, never resumes/relabels old observations.
    prior = report_root/'prior'
    prior.mkdir(parents=True)
    checkpoint = {'http_budget': {'attempted': 32, 'limit': 32, 'exhausted': True},
                  'first_unread': 'clickbank', 'window': WINDOW}
    (prior/'checkpoint.json').write_text(json.dumps(checkpoint))
    prior_bytes = (prior/'checkpoint.json').read_bytes()
    constructed, selected, wire, transports = [], [], [], []
    import requests
    class Session:
        max_redirects = 30
        def mount(self, prefix, adapter):
            assert adapter.max_retries.total == 0
        def request(self, method, url, **kwargs):
            wire.append((method, url))
            raise requests.exceptions.ConnectionError('SYNTHETIC_SELECTION_FAILURE')
    session = Session()
    monkeypatch.setattr(base.RequestsTransport, '_sess', lambda self: session)
    def collectors(names, transport, **kwargs):
        constructed.extend(names)
        transports.append(transport)
        assert transport.calls == 0 and transport.max_requests == 32
        assert 239 <= transport.deadline-time.monotonic() <= 240
        def bind(name):
            def collect(window, deadline, remaining, *, sink):
                selected.append(name)
                assert window == WINDOW and remaining == 32
                assert deadline == transport.deadline
                for i in range(32):
                    call = transport.auth_request if i == 0 else transport.request
                    with pytest.raises(requests.exceptions.ConnectionError):
                        call('POST' if i == 0 else 'GET', 'https://example.invalid/fixture')
                with pytest.raises(ValueError, match='run_request_budget_exhausted'):
                    transport.auth_request('POST', 'https://example.invalid/never-send')
                return []
            return collect
        return {n: bind(n) for n in names}
    monkeypatch.setitem(sys.modules, 'affiliate_network_access_j7', types.SimpleNamespace(weekly_collectors=collectors))
    argv = ['--source-manifest', str(mp), '--manifest-sha256', hashlib.sha256(mp.read_bytes()).hexdigest(),
            '--roster', str(rp), '--identity-registry', str(ip), '--report-root', str(report_root),
            '--lookback-days', '90', '--max-requests', '32', '--budget-seconds', '240', '--live-network-readonly']
    for value in selector or []:
        argv += ['--start-marketplace', value]
    if selector and (len(selector) != 1 or selector[0] not in NAMES):
        with pytest.raises(ValueError, match='start_marketplace_invalid|duplicate_start_marketplace'):
            job.main(argv)
        assert constructed == selected == wire == []
        assert list(report_root.iterdir()) == [prior]
    else:
        assert job.main(argv) == 0
        expected = selector[0] if selector else 'admitad'
        assert selected == [expected]
        output = next(p for p in report_root.iterdir() if p != prior and p.is_dir())
        result = json.loads((output/'RESULT.json').read_text())
        assert len(constructed) == len(set(constructed)) == result['marketplace_count'] == 56
        assert set(constructed) == set(NAMES)
        assert result['roster_sha256'] == j7.digest(original)
        assert result['window'] == WINDOW and result['coverage_complete'] is False
        assert result['http_budget']['attempted'] == result['http_budget']['limit'] == len(wire) == 32
        assert result['http_budget']['includes'] == 'REPORTING_AUTHENTICATION_FAILED_ATTEMPTS_AND_CALLER_RETRIES'
        assert session.max_redirects == 0
        assert result['observations'] == [] and result['payment_candidates'] == []
        assert all(result[k] is None for k in ('fleet_cash_minor', 'fleet_commission_minor', 'measured_epc'))
        assert {g['network'] for g in result['gaps']} == set(NAMES)
        assert result['account_identity_resolution']['matched_network_count'] == 0
        assert result.get('selection_start_marketplace') == (expected if selector else None)
        assert constructed == (NAMES[NAMES.index(expected):] + NAMES[:NAMES.index(expected)])
    assert rp.read_bytes() == roster_bytes
    assert (prior/'checkpoint.json').read_bytes() == prior_bytes


def test_direct_run_duplicate_roster_refuses_before_output(tmp_path):
    roster = {'marketplace_count': 2, 'marketplaces': [{'marketplace': 'clickbank'}]*2}
    with pytest.raises(ValueError, match='duplicate_marketplace'):
        j7.run(tmp_path/'absent', roster, [], start_marketplace='clickbank')
    assert not (tmp_path/'absent').exists()


@pytest.mark.parametrize('argv,expected,tamper', [
    ([], 0, False), (['--dry-run'], 0, False),
    (['--start-marketplace', 'clickbank'], 0, False),
    (['--dry-run', '--start-marketplace', 'clickbank'], 0, False),
    (['--start-marketplace', 'clickbank', '--start-marketplace', 'cj'], 64, False),
    (['--start-marketplace'], 64, False),
    (['--start-marketplace', 'ClickBank'], 64, False),
    (['--max-requests', '33'], 64, False),
    (['--start-marketplace', 'clickbank'], 66, True),
])
def test_registered_shell_portable_forwarding_and_source_drift(tmp_path, argv, expected, tamper):
    import re
    import subprocess
    text = (ROOT/'ops/hermes-cron-scripts/glp_affiliate_weekly_reporting_j7.sh').read_text()
    # Portable shell boundary only: retain parser/control bytes, relocate paths
    # and report-only pins, replace interpreter with inert argv capture. No job,
    # collector, native entry, provider or installed source executes here.
    for relative, pin in re.findall(r'^check_source (\S+) ([a-f0-9]{64})$', text, re.M):
        source = ROOT/relative
        body = source.read_bytes() if source.exists() else b'EXCLUDED_HOST_INPUT_FIXTURE_NOT_EVIDENCE\n'
        target = tmp_path/relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        text = text.replace(pin, digest)
    capture = tmp_path/'capture.sh'
    capture.write_text('printf "%s\\n" "$@"\n')
    text = text.replace('/home/ubuntu/.hermes/hermes-agent/venv/bin/python', '/bin/bash '+str(capture))
    text = text.replace('/home/ubuntu/business', str(tmp_path))
    wrapper = tmp_path/'portable-wrapper.sh'
    wrapper.write_text(text)
    if tamper:
        target = tmp_path/'scripts/glp_affiliate_weekly_reporting_j7_job.py'
        target.write_bytes(target.read_bytes()+b'\n# PRIVATE_DRIFT_FIXTURE\n')
    result = subprocess.run(['/bin/bash', str(wrapper), *argv], capture_output=True, text=True)
    assert result.returncode == expected
    if expected:
        assert not result.stdout
    else:
        forwarded = result.stdout.splitlines()
        if '--start-marketplace' in argv:
            assert forwarded.count('--start-marketplace') == 1
            assert forwarded[forwarded.index('--start-marketplace')+1] == 'clickbank'
        else:
            assert '--start-marketplace' not in forwarded
        assert ('--live-network-readonly' in forwarded) == ('--dry-run' not in argv)
        for flag, value in (('--max-requests', '32'), ('--budget-seconds', '240'), ('--lookback-days', '90')):
            assert forwarded.count(flag) == 1 and forwarded[forwarded.index(flag)+1] == value

