"""Authentic deployment b18504e0 capture + adverse real-caller tests (offline).

Raw fixture hashes are from the retained REL-ACCEPT capture and sealed release.
No Cloudflare effects, credentials, release claims or review verdicts are made.
"""
import gzip
import hashlib
import importlib.util
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cf_share_carrier', ROOT/'scripts/glp_deploy_glp34_authority_v1.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)
FIXTURE = ROOT/'tests/fixtures/cf-editorial-share'
EXPECTED = gzip.decompress((FIXTURE/'expected.html.gz').read_bytes())
RAW = gzip.decompress((FIXTURE/'observed.html.gz').read_bytes())
DECODER = c.CURRENT_CF_EMAIL_DECODER_SCRIPT


def probe(tmp_path, raw=RAW, expected=EXPECTED):
    public = tmp_path/'public'
    public.mkdir(exist_ok=True)
    (public/'index.html').write_bytes(expected)
    item = {'url_path': '/', 'sha256': c.sha_bytes(expected), 'bytes': len(expected)}
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.headers = {'Content-Type': 'text/html; charset=utf-8'}
    response.read.return_value = raw
    receipt = {'stages': {}}
    path = tmp_path/'readback'/'receipt.json'
    with mock.patch.object(c.urllib.request.OpenerDirector, 'open', return_value=response):
        return c.current_route_probe(c.SITE, item, public, path, receipt)


def encode(payload, key=0x7e):
    return bytes([key, *[v ^ key for v in payload]]).hex().encode()


def change_share(raw, mutate):
    import re
    m = re.search(rb'/cdn-cgi/l/email-protection#([0-9a-f]+)', raw)
    value = bytes.fromhex(m[1].decode())
    decoded = bytes(v ^ value[0] for v in value[1:])
    assert decoded.startswith(b'?subject=')
    return raw[:m.start(1)] + encode(mutate(decoded)) + raw[m.end(1):]


def test_authentic_capture_restores_complete_sealed_page(tmp_path):
    assert hashlib.sha256(EXPECTED).hexdigest() == '04da922e7071077597ee397eb5da7ee5a147347d7a9d04848d8fec5fe5c3aca0'
    assert hashlib.sha256(RAW).hexdigest() == 'c96c4f9e36be33d86c4c803895544d5db72b540fa93cc80ab2932d67fa1de4ac'
    row = probe(tmp_path)
    assert row['comparison'] == 'exact_cf_email_reversal'
    assert row['compared_sha256'] == c.sha_bytes(EXPECTED)
    assert row['compared_bytes'] == len(EXPECTED)
    assert Path(row['body_path']).read_bytes() == RAW


@pytest.mark.parametrize('name,raw', [
    ('subject', change_share(RAW, lambda x: x.replace(b'How', b'Bad'))),
    ('canonical-url', change_share(RAW, lambda x: x.replace(b'glpclinicscout.com', b'example.com'))),
    ('recipient', change_share(RAW, lambda x: b'evil@example.com'+x)),
    ('extra-param', change_share(RAW, lambda x: x+b'&bcc=evil@example.com')),
    ('raw-entity-spelling', change_share(RAW, lambda x: x.replace(b'&body=', b'&amp;body='))),
    ('odd-hex', RAW.replace(b'7e410d0b', b'7e410d0', 1)),
    ('bad-hex', RAW.replace(b'7e410d0b', b'7g410d0b', 1)),
    ('missing-decoder', RAW.replace(DECODER, b'', 1)),
    ('extra-decoder', RAW.replace(DECODER, DECODER+DECODER, 1)),
    ('moved-decoder', RAW.replace(DECODER, b'', 1).replace(b'</head>', DECODER+b'</head>', 1)),
    ('nonexact-decoder', RAW.replace(b'data-cfasync="false"', b'data-cfasync="true"', 1)),
    ('script', RAW.replace(b'/assets/editorial-share.js', b'/assets/other.js', 1)),
    ('attribute', RAW.replace(b'class="cta secondary" href="/cdn-cgi', b'class="cta primary" href="/cdn-cgi', 1)),
    ('body', RAW.replace(b'<h1', b'<h2', 1)),
    ('support', RAW.replace(encode(b'support@glpclinicscout.com', 0x5f), encode(b'wrong@example.com', 0x5f))),
    ('support-span-mismatch', RAW.replace(b'data-cfemail="'+encode(b'support@glpclinicscout.com', 0xf7), b'data-cfemail="'+encode(b'privacy@glpclinicscout.com', 0xf7))),
], ids=lambda value: value if isinstance(value, str) else 'capture')
def test_adverse_real_caller_refuses(tmp_path, name, raw):
    # Every counterexample must really differ, not accidentally test baseline.
    assert raw != RAW, name
    with pytest.raises(c.GateError):
        probe(tmp_path, raw)


def test_recovery_uses_same_complete_byte_oracle():
    import sys
    sys.path.insert(0, str(ROOT/'scripts'))
    import article_review_recovery as recovery
    recovery.require_live_matches_sealed(RAW, EXPECTED)
    # Broad decoder stripping would have accepted this extra script.
    with pytest.raises(RuntimeError, match='cloudflare_email_decoder_script_count_invalid'):
        recovery.require_live_matches_sealed(RAW.replace(DECODER, DECODER+DECODER, 1), EXPECTED)


def test_wrong_expected_page_refused(tmp_path):
    with pytest.raises(c.GateError):
        probe(tmp_path, expected=EXPECTED.replace(b'<h1', b'<h2', 1))
