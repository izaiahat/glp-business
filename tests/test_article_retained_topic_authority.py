"""Retained queue custody survives mutable live queue regeneration."""
import hashlib
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'systems/seo-blog-machine/scripts'))
import article_buffer as buffer


def test_retained_entry_authority_digest_survives_live_queue_change(tmp_path):
    packet = tmp_path / 'entry/packet'
    packet.mkdir(parents=True)
    evidence = packet.parent / 'evidence/topic_authority.json'
    evidence.parent.mkdir()
    evidence.write_text('{"queue": ["accepted-topic"]}')
    live = tmp_path / 'latest.json'
    live.write_text('{"queue": ["different-topic"]}')
    pin = {'path': str(live), 'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}
    assert buffer._resolve_topic_authority(pin, packet) == evidence.resolve()
    evidence.write_text('{"queue": ["tampered-topic"]}')
    with pytest.raises(RuntimeError, match='buffer_topic_authority_pin_invalid'):
        buffer._resolve_topic_authority(pin, packet)
