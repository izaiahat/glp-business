"""Current-coordinator receipt regression; synthetic, no live authority."""
import hashlib
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import glp_sponsor_campaign as c


def test_current_coordinator_receiving_is_not_a_historical_session(tmp_path, monkeypatch):
    import glp_sponsor_current_receiving as r
    root = tmp_path / 'installed'
    monkeypatch.setattr(c, 'ROOT', root)
    monkeypatch.setattr(c, 'STATE', root / 'runtime')
    monkeypatch.setattr(r, 'packet_binding', lambda campaign, packet, config: {'source': 'a'*64})
    monkeypatch.setattr(r, 'current_authority', lambda campaign: {'plan_sha256': 'b'*64, 'decision_sha256': 'd'*64})
    packet = root / 'reports/ops/sponsor-machine-20261003/sponsor-review-packet.zip'
    packet.parent.mkdir(parents=True)
    packet.write_bytes(b'synthetic packet')
    sha = c.digest(packet)
    config = {'send_enabled': True, 'packet_sha256': sha}
    receipt = {'schema': r.SCHEMA, 'actor': 'coordinator', 'coordinator_authority': r.current_authority(c),
               'receiving_session_id': 'current-parent-session', 'packet_sha256': sha,
               'decision': 'RECEIVE CURRENT SPONSOR PACKET ' + sha,
               'reviewed_source_head': '1'*40, 'installed_bindings': {'source': 'a'*64}}
    c.save(c.STATE / r.RECEIPT, receipt)
    c.save(c.STATE / 'approval.json', receipt)
    assert c.approval_ready(config)
    original = (c.STATE / r.RECEIPT).read_text()
    for field, bad in [('actor', 'source-author'), ('coordinator_authority', {'plan_sha256':'0'*64}),
                       ('packet_sha256','0'*64), ('installed_bindings', {'source':'0'*64}),
                       ('decision','APPROVE SPONSOR PACKET '+sha), ('reviewed_source_head','not-head')]:
        c.save(c.STATE / r.RECEIPT, dict(receipt, **{field:bad}))
        with pytest.raises(RuntimeError, match='current_coordinator_receiving_invalid'):
            c.approval_ready(config)
    (c.STATE / r.RECEIPT).write_text(original.replace('"actor": "coordinator"', '"actor":"source-author", "actor":"coordinator"'))
    with pytest.raises(ValueError, match='duplicate_json_key:actor'):
        c.approval_ready(config)
    # The positive writer must refuse a leaf, even with plausible process metadata.
    monkeypatch.setenv('LINEAR_ROLE', 'worker')
    monkeypatch.setenv('HERMES_SESSION_ID', 'current-parent-session')
    with pytest.raises(RuntimeError, match='current_coordinator_execution_required'):
        r.require_coordinator_execution()
