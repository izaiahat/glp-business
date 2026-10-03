"""Synthetic spec-shaped JUMBF framing; not signed provenance or original art."""
import base64
import hashlib
import socket
import struct

import pytest
from finch_png_acceptance import png_chunk, png_fixture
from test_rook_mcp_events import module

UUID_SUFFIX = bytes.fromhex('00110010800000aa00389b71')


def box(kind, data=b''):
    return struct.pack('>I', len(data) + 8) + kind + data


def description(kind, label, toggles=3, optional=b''):
    return box(b'jumd', kind + UUID_SUFFIX + bytes([toggles]) + label + b'\0' + optional)


def superbox(kind, label, children, toggles=3, optional=b''):
    return box(b'jumb', description(kind, label, toggles, optional) + b''.join(children))


def manifest(assertion=None, optional=b'', toggles=3):
    assertion = assertion or superbox(b'json', b'c2pa.actions', [box(b'json', b'{"actions":[]}')])
    return superbox(b'c2pa', b'c2pa', [superbox(b'c2ma', b'urn:c2pa:00000000-0000-0000-0000-000000000001', [
        superbox(b'c2as', b'c2pa.assertions', [assertion]),
        superbox(b'c2cl', b'c2pa.claim', [box(b'cbor', b'\xa0')]),
        superbox(b'c2cs', b'c2pa.signature', [box(b'cbor', b'\xd2\x84\x40\xa0\x40\x40')]),
    ], toggles, optional)])


def reported_shape_fixture(width, height, color, idat_count, last_idat):
    """Match reported framing/byte counts with new deterministic synthetic pixels.

    Nonfinal stored DEFLATE blocks followed by final compressed zero blocks form
    ONE stream. This tunes exact size without trailing data or decoder stubs.
    """
    import random
    import zlib
    row = width * {2: 3, 6: 4}[color] + 1
    total = row * height
    target = (idat_count - 1) * 65536 + last_idat
    low, high = target - 8192, target
    while low <= high:
        prefix = (low + high) // 2
        final = zlib.compress(bytes(total - prefix), level=9)[2:-4]
        length = prefix + 5 * ((prefix + 65534) // 65535) + len(final) + 6
        if length == target: break
        if length < target: low = prefix + 1
        else: high = prefix - 1
    else:
        raise AssertionError('cannot tune synthetic stream size')
    raw = bytearray(random.Random(17).randbytes(prefix))
    raw[::row] = bytes(len(raw[::row]))  # Every PNG filter byte is zero.
    stream = bytearray(b'\x78\x01')
    for pos in range(0, prefix, 65535):
        block = raw[pos:pos + 65535]
        stream.extend(b'\0' + struct.pack('<HH', len(block), 65535 - len(block)) + block)
    checksum = zlib.adler32(bytes(total - prefix), zlib.adler32(raw))
    stream.extend(final + struct.pack('>I', checksum))
    assert len(stream) == target
    payload = manifest()
    # Add final root padding while keeping the true declared parent length.
    payload = box(b'jumb', payload[8:] + box(b'free', b'x' * (21881 - len(payload) - 8)))
    chunks = [(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, color, 0, 0, 0)), (b'caBX', payload)]
    chunks += [(b'IDAT', bytes(stream[pos:pos + 65536])) for pos in range(0, len(stream), 65536)]
    chunks.append((b'IEND', b''))
    return b'\x89PNG\r\n\x1a\n' + b''.join(png_chunk(k, d) for k, d in chunks), [(k.decode(), len(d)) for k, d in chunks]


class TestFinchCaBX:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.m = module()
        monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network denied'))
        monkeypatch.setattr(socket.socket, 'bind', lambda *a, **k: pytest.fail('bind denied'))
        monkeypatch.setattr(self.m, 'load_token', lambda: pytest.fail('credential denied'))
        monkeypatch.setattr(self.m, 'load_rook_token', lambda: pytest.fail('credential denied'))

    def validate(self, payload, extras=()):
        return self.m.validate_file('art.png', 'image/png', png_fixture(extra=[(b'caBX', payload), *extras]), finch=True)

    @pytest.mark.parametrize('width,height,color,count,last,size,original_sha', [
        (1254, 1254, 6, 11, 15010, 692440, '4c748d919c916f3b6831d5dc1a84366f6bf5cd76bf280e95a50b2e37e313ff0b'),
        (1983, 793, 2, 32, 61997, 2115935, '118d8bde38273c2c1e91fdae29b8d4bf12ee96e278c83b5fcc3ce27ff56d22da')])
    def test_reported_avatar_banner_shapes_not_original_bytes(self, width, height, color, count, last, size, original_sha):
        data, chunks = reported_shape_fixture(width, height, color, count, last)
        assert chunks == [('IHDR', 13), ('caBX', 21881)] + [('IDAT', 65536)] * (count - 1) + [('IDAT', last), ('IEND', 0)]
        assert len(data) == size
        assert hashlib.sha256(data).hexdigest() != original_sha
        file = self.m.validate_file('art.png', 'image/png', data, finch=True)
        assert file['data'] == data
        if size > self.m.FILE_LIMIT:
            with pytest.raises(self.m.RpcError, match='FileSizeRefused'):
                self.m.validate_file('art.png', 'image/png', data)

    def test_spec_shaped_manifest_preserves_bytes_sha_and_multipart(self):
        payload = manifest()
        data = png_fixture(extra=[(b'caBX', payload)])
        value = {'filename': 'art.png', 'content_type': 'image/png', 'content_base64': base64.b64encode(data).decode()}
        file = self.m.upload_file(value, finch=True)
        assert file['data'] == data
        assert hashlib.sha256(file['data']).hexdigest() == hashlib.sha256(data).hexdigest()
        assert self.m.multipart_body({}, file, finch=True)[0].count(data) == 1
        # Preserve the existing non-Finch raw/signature-only validator, not a JUMBF gate.
        self.m.validate_file('art.png', 'image/png', png_fixture(extra=[(b'caBX', b'opaque')]))

    @pytest.mark.parametrize('optional,toggles', [(b'', 3), (struct.pack('>I', 42), 7),
        (bytes(32), 11), (box(b'c2sh', bytes(16)), 19),
        (struct.pack('>I', 42) + bytes(32) + box(b'c2sh', bytes(32)), 31)])
    def test_supported_description_fields(self, optional, toggles):
        self.validate(manifest(optional=optional, toggles=toggles))

    @pytest.mark.parametrize('kind,children', [(b'cbor', [box(b'cbor', b'\xa0')]),
        (b'json', [box(b'json', b'{"note":"safe"}')]),
        (b'bfdb', [box(b'bfdb', b'\x00image/png\0'), box(b'bidb', png_fixture())]),
        (b'bfdb', [box(b'bfdb', b'\x01image/jpeg\0thumb.jpg\0'), box(b'bidb', b'\xff\xd8\xff\xd9')])])
    def test_supported_content_and_embedded_file_framing(self, kind, children):
        self.validate(manifest(superbox(kind, b'example', children + [box(b'free', bytes(16))])))

    @pytest.mark.parametrize('marker', [b'PRIVATE KEY', b'Authorization: example', b'Bearer example',
        b'password=example', b'api_key=example', b'client_secret=example', b'patient', b'dob', b'ssn',
        b'diagnosis', b'medical record', b'<html>', b'<script>', b'/JavaScript', b'/JS', b'/Launch',
        b'/OpenAction', b'/AA', b'/EmbeddedFile'])
    @pytest.mark.parametrize('location', ['label', 'json', 'cbor', 'embedded', 'padding', 'salt'])
    def test_every_metadata_byte_keeps_existing_marker_detection(self, marker, location):
        if location == 'label':
            assertion = superbox(b'json', marker, [box(b'json', b'{}')])
        elif location == 'embedded':
            assertion = superbox(b'bfdb', b'example', [box(b'bfdb', b'\0image/png\0'), box(b'bidb', png_fixture(extra=[(b'tEXt', b'Note\0 ' + marker + b' ')]))])
        else:
            children = [box(b'json', b'{}')]
            if location == 'padding': children.append(box(b'free', marker))
            if location in ('json', 'cbor'): children = [box(location.encode(), marker)]
            assertion = superbox(b'cbor' if location == 'cbor' else b'json', b'example', children,
                19 if location == 'salt' else 3,
                box(b'c2sh', marker.ljust(32, b' ')) if location == 'salt' else b'')
        with pytest.raises(self.m.RpcError, match='SensitiveOrActiveFileRefused'):
            self.validate(manifest(assertion))

    @pytest.mark.parametrize('case', ['empty', 'short_header', 'zero_size', 'extended_size', 'small_size',
        'oversized', 'trailing', 'two_roots', 'missing_description', 'duplicate_description',
        'description_after_content', 'child_over_parent', 'empty_content', 'unknown_box',
        'compressed', 'unknown_uuid', 'wrong_content', 'padding_first', 'padding_not_final'])
    def test_lengths_nesting_and_unsupported_boxes_fail_closed(self, case):
        good = manifest()
        desc = description(b'c2pa', b'c2pa')
        assertion = superbox(b'json', b'example', [box(b'json', b'{}')])
        bad = {'empty': b'', 'short_header': b'1234567', 'zero_size': bytes(4) + good[4:],
            'extended_size': struct.pack('>I', 1) + good[4:], 'small_size': struct.pack('>I', 7) + good[4:],
            'oversized': struct.pack('>I', len(good) + 1) + good[4:], 'trailing': good + b'x', 'two_roots': good * 2,
            'missing_description': box(b'jumb', assertion), 'duplicate_description': box(b'jumb', desc * 2 + assertion),
            'description_after_content': box(b'jumb', assertion + desc),
            'child_over_parent': box(b'jumb', desc + struct.pack('>I', 65536) + b'jumb'),
            'empty_content': superbox(b'c2pa', b'c2pa', []),
            'unknown_box': manifest(superbox(b'json', b'example', [box(b'xml ', b'<x/>')])),
            'compressed': manifest(superbox(b'c2cm', b'example', [box(b'brob', b'compressed')])),
            'unknown_uuid': manifest(superbox(b'xxxx', b'example', [box(b'json', b'{}')])),
            'wrong_content': manifest(superbox(b'json', b'example', [box(b'cbor', b'\xa0')])),
            'padding_first': box(b'jumb', box(b'free') + desc + assertion),
            'padding_not_final': box(b'jumb', desc + box(b'free') + assertion)}[case]
        with pytest.raises(self.m.RpcError): self.validate(bad)

    @pytest.mark.parametrize('desc', [b'', b'x' * 16, b'json' + UUID_SUFFIX + b'\x03no-null',
        b'json' + UUID_SUFFIX + b'\x03\xff\0', b'json' + UUID_SUFFIX + b'\x03bad/name\0',
        b'json' + UUID_SUFFIX + b'\x03\0', b'json' + UUID_SUFFIX + b'\x23example\0',
        b'json' + UUID_SUFFIX + b'\x01example\0', b'json' + UUID_SUFFIX + b'\x07example\0x',
        b'json' + UUID_SUFFIX + b'\x0bexample\0x', b'json' + UUID_SUFFIX + b'\x13example\0' + box(b'c2sh', b'x'),
        b'json' + UUID_SUFFIX + b'\x13example\0' + box(b'xxxx', bytes(16)),
        b'json' + UUID_SUFFIX + b'\x03example\0extra'])
    def test_malformed_descriptions_and_optional_fields(self, desc):
        with pytest.raises(self.m.RpcError):
            self.validate(manifest(box(b'jumb', box(b'jumd', desc) + box(b'json', b'{}'))))

    @pytest.mark.parametrize('children', [[box(b'bidb', b'raw')], [box(b'bfdb', b'\0image/png\0')],
        [box(b'bfdb', b'\x02image/png\0'), box(b'bidb', b'raw')],
        [box(b'bfdb', b'\x01image/png\0'), box(b'bidb', b'raw')],
        [box(b'bfdb', b'\0image/png'), box(b'bidb', b'raw')],
        [box(b'bfdb', b'\0application/zip\0'), box(b'bidb', b'raw')],
        [box(b'bfdb', b'\0image/png\0extra'), box(b'bidb', b'raw')]])
    def test_embedded_framing_external_and_unsupported_media(self, children):
        with pytest.raises(self.m.RpcError): self.validate(manifest(superbox(b'bfdb', b'example', children)))

    def test_single_cabx_crc_and_existing_aggregate_metadata_budget(self):
        payload = manifest()
        with pytest.raises(self.m.RpcError): self.validate(payload, [(b'caBX', payload)])
        data = bytearray(png_fixture(extra=[(b'caBX', payload)])); data[41] ^= 1
        with pytest.raises(self.m.RpcError): self.m.validate_file('art.png', 'image/png', bytes(data), finch=True)
        # Raw caBX counts once, existing uncompressed text counts twice.
        padding = self.m.PNG_METADATA_LIMIT - len(payload) - 8
        payload = superbox(b'c2pa', b'c2pa', [manifest()[8 + len(description(b'c2pa', b'c2pa')):], box(b'free', b'x' * padding)])
        assert len(payload) == self.m.PNG_METADATA_LIMIT
        self.validate(payload)
        with pytest.raises(self.m.RpcError): self.validate(payload, [(b'tEXt', b'Note\0safe')])
        with pytest.raises(self.m.RpcError): self.validate(payload + b'x')

    def test_cabx_position_no_url_fetch_and_embedded_pixels_are_metadata(self):
        payload = manifest(superbox(b'json', b'example', [box(b'json', b'{"url":"https://example.invalid/manifest"}')]))
        data = png_fixture()
        # The spec recommends pre-IDAT, but does not require it.
        self.m.validate_file('art.png', 'image/png', data[:-12] + png_chunk(b'caBX', payload) + data[-12:], finch=True)
        import zlib
        packed = zlib.compress(b'\x00' + bytes(9))
        between = data[:33] + png_chunk(b'IDAT', packed[:3]) + png_chunk(b'caBX', payload) + png_chunk(b'IDAT', packed[3:]) + data[-12:]
        with pytest.raises(self.m.RpcError): self.m.validate_file('art.png', 'image/png', between, finch=True)
        thumbnail = png_fixture(raw=b'\x00 patient ', compressed=zlib.compress(b'\x00 patient ', level=0))
        assertion = superbox(b'bfdb', b'example', [box(b'bfdb', b'\0image/png\0'), box(b'bidb', thumbnail)])
        with pytest.raises(self.m.RpcError, match='SensitiveOrActiveFileRefused'): self.validate(manifest(assertion))

    def test_depth_box_count_and_label_limits_at_boundary(self):
        # Structural container profile, not semantic C2PA claim validation.
        for depth in (8, 9):
            child = superbox(b'json', b'example', [box(b'json', b'{}')])
            for _ in range(depth - 2): child = superbox(b'c2ma', b'example', [child])
            payload = superbox(b'c2pa', b'c2pa', [child])
            if depth == 8: self.validate(payload)
            else:
                with pytest.raises(self.m.RpcError): self.validate(payload)
        for count in (1024, 1025):
            # root+description, leaf+description+content, plus padding = six boxes.
            leaf = superbox(b'cbor', b'example', [box(b'cbor', b'\xa0')] * (count - 5) + [box(b'free')])
            payload = superbox(b'c2pa', b'c2pa', [leaf])
            if count == 1024: self.validate(payload)
            else:
                with pytest.raises(self.m.RpcError): self.validate(payload)
        self.validate(manifest(superbox(b'json', b'x' * 1024, [box(b'json', b'{}')])))
        with pytest.raises(self.m.RpcError):
            self.validate(manifest(superbox(b'json', b'x' * 1025, [box(b'json', b'{}')])))
