"""Current cohort without changing historical cleanup/GLP331 oracles."""
import ast
import hashlib
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUCCESSOR = ROOT / 'ops/hermes-cron-scripts/glp_affiliate_reconciliation_current_base.sh'


def dependency_bindings():
    tree = ast.parse((ROOT / 'scripts/fleet_pin_release.py').read_text())
    node = next(n for n in tree.body if isinstance(n, ast.Assign) and
                any(getattr(t, 'id', '') == 'GLP331_DEPENDENCY_BINDINGS' for t in n.targets))
    return ast.literal_eval(node.value)


@pytest.mark.parametrize('tamper', [False, True])
@pytest.mark.parametrize('member', ['glp_affiliate_wire_backlog_worker.py', 'build_glp_affiliate_wirein_incremental_v2.py', 'glp_review_editorial_projection.py', 'glp_best_terms_current_bindings.py'])
def test_worker_pin_cohort(tmp_path, tamper, member):
    text = SUCCESSOR.read_text()
    match = re.search(r"scripts/" + re.escape(member) + r"'\]='([a-f0-9]{64})'", text)
    assert match is not None
    expected = match[1]
    raw = (ROOT / 'scripts' / member).read_bytes()
    assert expected == hashlib.sha256(raw).hexdigest()
    worker = tmp_path / 'worker.py'
    worker.write_bytes(raw + (b'\n' if tamper else b''))
    loop = text[text.index('for b2_path in'):text.index('\n# Resolve every approved')]
    script = "declare -A b2_pins=(['" + str(worker) + "']='" + expected + "')\n" + loop
    run = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
    assert run.returncode == (1 if tamper else 0)
    assert ('b2_payload_hash_drift' in run.stdout) == tamper


def test_successor_preserves_historical_wrapper_and_exact_reviewed_cohort():
    historical = (ROOT / 'ops/hermes-cron-scripts/glp_affiliate_reconciliation_honesty.sh').read_bytes()
    assert hashlib.sha256(historical).hexdigest() == '99bcc4cca7c63f41e3546bb0e470976907027557bf1e7b89d1a0dbb8aaebcfb4'
    expected = historical
    bindings = dependency_bindings()
    assert len(bindings) == 4
    import sys
    sys.path.insert(0, str(ROOT / 'scripts'))
    from glp93_current_base_binding import historical_dependency_bytes, current_carrier_bytes
    for rel, (old, new) in bindings.items():
        assert expected.count(old.encode()) == 1
        assert hashlib.sha256(historical_dependency_bytes(ROOT, rel)).hexdigest() == new
        expected = expected.replace(old.encode(), new.encode())
    assert SUCCESSOR.read_bytes() == current_carrier_bytes(expected)
    predecessor = expected.replace(bindings['scripts/glp_affiliate_wire_backlog_worker.py'][1].encode(),
                                   b'59e25a8c605f6c0c2ae284edfb3dae99eb7354c2679146fb787a923a4544ef05')
    assert hashlib.sha256(predecessor).hexdigest() == 'dc24f7c121d1272b0400f30140576a116a5b841ee74ec20d34ed2da75ba362a4'


def test_every_successor_b2_pin_equals_exact_maintained_dependency():
    pins = re.findall(r"\['/home/ubuntu/business/([^']+)'\]='([a-f0-9]{64})'", SUCCESSOR.read_text())
    assert len(pins) == 12  # historical ten plus two current transitive helpers
    uncurated = {
        'scripts/glp_price_index_source_rebind.mjs': '5342eabc650b351b5d3c7a2dd1329cf013a7445dbc30260f5240f6606cae021c',
        'scripts/glp_provider_dynamic_profile_activation.mjs': '598a7d4c9b1a261471569e9c69df6ae45d6fb6572807f801ccd958f6f0acd5a0',
    }
    for rel, pin in pins:
        if rel in uncurated:
            # Their exact unchanged literals are also bound by the full-byte
            # predecessor test. Actual installed bytes are checked at delivery.
            assert not (ROOT / rel).exists()
            assert pin == uncurated[rel]
        else:
            assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == pin


def paid_event_functions(root):
    """Execute the maintained pure gate without the worker's host-only builders."""
    import json
    from typing import Any, Iterable
    names = {'read_json', 'normalize_network', 'load_exact_paid_event_policy',
             'exact_paid_event_gate'}
    tree = ast.parse((ROOT / 'scripts/glp_affiliate_wire_backlog_worker.py').read_text())
    selected = ast.Module(body=[node for node in tree.body
                               if isinstance(node, ast.FunctionDef) and node.name in names],
                          type_ignores=[])
    namespace = {'Path': Path, 'Any': Any, 'Iterable': Iterable, 'json': json, 're': re,
                 'BUSINESS': root, 'EXACT_PAID_EVENT_POLICY': root / 'policy.json',
                 'B2Error': type('B2Error', (RuntimeError,), {})}
    exec(compile(selected, 'glp_affiliate_wire_backlog_worker.py', 'exec'), namespace)
    return namespace


def test_missing_other_rail_evidence_does_not_abort_current_rail(tmp_path):
    import json
    (tmp_path / 'valid.txt').write_text('Synthetic published paid-event policy evidence')
    policy = {'schema': 'glp.exact-paid-event-publication-policy.v1', 'networks': {
        'trackdesk': {'publication_mode': 'never_publishes_exact_paid_event',
                      'evidence': 'missing.txt'},
        'affiliati': {'publication_mode': 'publishes_exact_paid_event',
                     'evidence': 'valid.txt'}}}
    (tmp_path / 'policy.json').write_text(json.dumps(policy))
    functions = paid_event_functions(tmp_path)
    gate = functions['exact_paid_event_gate']
    with pytest.raises(RuntimeError, match='evidence_missing:trackdesk'):
        functions['load_exact_paid_event_policy']()
    assert gate('Affiliati', [{'exact_paid_event': 'Initial paid sale'}])['status'] == 'PASS_PUBLISHED'
    blocked = gate('Trackdesk', [])
    assert blocked['status'] == 'BLOCK'
    assert blocked['reason'] == 'exact_paid_event_policy_evidence_missing:trackdesk'
    assert blocked['risk_acceptance'] is None
    assert gate('Affiliati', [])['status'] == 'BLOCK'
    assert gate('Affiliati', [{'paid_event': 'sale'}, {'paid_event': 'lead'}])['status'] == 'BLOCK'
    # Missing field plus no policy, missing evidence, and invalid modes block.
    assert gate('Unclassified', [])['status'] == 'BLOCK'
    (tmp_path / 'valid.txt').unlink()
    assert gate('Affiliati', [{'paid_event': 'sale'}])['status'] == 'BLOCK'
    policy['networks']['trackdesk']['publication_mode'] = 'unknown'
    (tmp_path / 'policy.json').write_text(json.dumps(policy))
    with pytest.raises(RuntimeError, match='publication_mode_invalid'):
        gate('Affiliati', [{'paid_event': 'sale'}])
