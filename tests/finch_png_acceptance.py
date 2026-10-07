"""Offline bounded Finch PNG validation; synthetic images only."""
import base64
import socket
import pytest
from test_rook_mcp_events import module

# Synthetic PNG acceptance; no original avatar/banner bytes are available.
def png_chunk(kind, data):
    import struct, zlib
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))


def png_fixture(width=3, height=1, color=2, filters=None, extra=(), raw=None, compressed=None):
    import struct, zlib
    channels = {2: 3, 6: 4}.get(color, 3)
    raw = raw if raw is not None else b''.join(bytes([f]) + bytes(width * channels) for f in (filters or [0] * height))
    ihdr = struct.pack('>IIBBBBB', width, height, 8, color, 0, 0, 0)
    return b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', ihdr) + b''.join(png_chunk(k, d) for k, d in extra) + png_chunk(b'IDAT', compressed if compressed is not None else zlib.compress(raw)) + png_chunk(b'IEND', b'')


class TestFinchPNGValidator:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch):
        self.m = module()
        monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network denied'))
        monkeypatch.setattr(socket.socket, 'bind', lambda *a, **k: pytest.fail('bind denied'))
        monkeypatch.setattr(self.m, 'load_token', lambda: pytest.fail('credential denied'))

    def validate(self, data):
        return self.m.validate_file('art.png', 'image/png', data, finch=True)

    def test_idat_marker_false_positive_reproduced_only_finch_exempts_pixels(self):
        import zlib
        data = png_fixture(raw=b'\x00 patient ', compressed=zlib.compress(b'\x00 patient ', level=0))
        assert b'patient' in data
        with pytest.raises(self.m.RpcError, match='SensitiveOrActiveFileRefused'):
            self.m.validate_file('art.png', 'image/png', data)
        assert self.validate(data)['data'] == data

    @pytest.mark.parametrize('color', [2, 6])
    @pytest.mark.parametrize('filter_type', range(5))
    def test_real_decoder_reconstructs_rows(self, color, filter_type):
        channels = {2: 3, 6: 4}[color]
        encoded = bytes([filter_type]) + bytes(range(1, channels * 3 + 1))
        rows = list(self.m.png_decoded_rows([__import__('zlib').compress(encoded)], 3, 1, channels))
        expected = list(range(1, channels * 3 + 1))
        if filter_type in (1, 3, 4):
            for i in range(channels, len(expected)):
                left = expected[i - channels]
                expected[i] = (expected[i] + (left // 2 if filter_type == 3 else left)) & 255
        assert rows == [bytes(expected)]
        self.validate(png_fixture(color=color, raw=encoded))

    @pytest.mark.parametrize('case', ['crc', 'length', 'missing_header', 'missing_idat', 'missing_end', 'duplicate_header', 'trailing', 'nonconsecutive_idat', 'end_payload', 'chunk_type'])
    def test_structure_crc_and_order(self, case):
        import struct, zlib
        ihdr = struct.pack('>IIBBBBB', 3, 1, 8, 2, 0, 0, 0)
        head = b'\x89PNG\r\n\x1a\n' + png_chunk(b'IHDR', ihdr)
        idat = png_chunk(b'IDAT', zlib.compress(b'\x00' + bytes(9)))
        end = png_chunk(b'IEND', b'')
        valid = head + idat + end
        bad = {'crc': valid[:-1] + bytes([valid[-1] ^ 1]), 'length': valid[:8] + b'\x7f\xff\xff\xff' + valid[12:],
               'missing_header': valid[:8] + idat + end, 'missing_idat': head + end, 'missing_end': head + idat,
               'duplicate_header': head + head[8:] + idat + end, 'trailing': valid + b'hello',
               'nonconsecutive_idat': head + idat + png_chunk(b'tEXt', b'Note\x00safe') + idat + end,
               'end_payload': head + idat + png_chunk(b'IEND', b'x'),
               'chunk_type': head + png_chunk(b'abcd', b'') + idat + end}[case]
        with pytest.raises(self.m.RpcError): self.validate(bad)

    @pytest.mark.parametrize('raw', [b'\x05' + bytes(9), b'\x00' + bytes(8), b'\x00' + bytes(10)])
    def test_decode_rejects_invalid_filter_or_sample_count(self, raw):
        with pytest.raises(self.m.RpcError): self.validate(png_fixture(raw=raw))

    @pytest.mark.parametrize('case', ['invalid', 'truncated', 'extra_stream', 'extra_bytes', 'bomb'])
    def test_bounded_inflate_rejects_malformed_or_extra_stream(self, case):
        import zlib
        packed = zlib.compress(b'\x00' + bytes(9))
        bad = {'invalid': b'not deflate', 'truncated': packed[:-1], 'extra_stream': packed + packed,
               'extra_bytes': packed + b'x', 'bomb': zlib.compress(bytes(10 * 1024 * 1024))}[case]
        with pytest.raises(self.m.RpcError): self.validate(png_fixture(compressed=bad))

    @pytest.mark.parametrize('width,height', [(0, 1), (1, 0), (4097, 1), (1, 4097), (4096, 1025)])
    def test_dimensions_and_pixels_refused_before_decode(self, width, height, monkeypatch):
        monkeypatch.setattr(self.m, 'png_decoded_rows', lambda *a: pytest.fail('must reject before decode'))
        with pytest.raises(self.m.RpcError): self.validate(png_fixture(width, height, raw=b''))

    @pytest.mark.parametrize('kind', [b'tEXt', b'zTXt', b'iTXt'])
    @pytest.mark.parametrize('marker', [b'PRIVATE KEY', b'Authorization: placeholder', b'Bearer placeholder',
        b'password=placeholder', b'api_key=placeholder', b'api-key: placeholder', b'client_secret=placeholder',
        b'patient', b'dob', b'ssn', b'diagnosis', b'medical record', b'<html>', b'<script>',
        b'/JavaScript', b'/JS', b'/Launch', b'/OpenAction', b'/AA', b'/EmbeddedFile'])
    def test_metadata_keeps_original_sensitive_and_active_markers(self, kind, marker):
        import zlib
        payload = {b'tEXt': b'Note\x00' + marker, b'zTXt': b'Note\x00\x00' + zlib.compress(marker),
                   b'iTXt': b'Note\x00\x01\x00en\x00Title\x00' + zlib.compress(marker)}[kind]
        with pytest.raises(self.m.RpcError, match='SensitiveOrActiveFileRefused'):
            self.validate(png_fixture(extra=[(kind, payload)]))

    @pytest.mark.parametrize('kind', [b'tEXt', b'zTXt', b'iTXt'])
    def test_safe_metadata(self, kind):
        import zlib
        payload = {b'tEXt': b'Note\x00safe', b'zTXt': b'Note\x00\x00' + zlib.compress(b'safe'),
                   b'iTXt': b'Note\x00\x01\x00en\x00Title\x00' + zlib.compress('safe \u2603'.encode())}[kind]
        self.validate(png_fixture(extra=[(kind, payload)]))

    @pytest.mark.parametrize('case', ['compressed_bomb', 'aggregate', 'bad_method', 'extra_stream', 'bad_utf8', 'unknown', 'apng', 'palette', 'interlace'])
    def test_metadata_bounds_and_unsupported_variants(self, case):
        import zlib, struct
        extras = {'compressed_bomb': [(b'zTXt', b'Note\x00\x00' + zlib.compress(b'x' * (262144 + 1)))],
                  'aggregate': [(b'tEXt', b'Note\x00' + b'x' * 70000)] * 2,
                  'bad_method': [(b'zTXt', b'Note\x00\x01' + zlib.compress(b'safe'))],
                  'extra_stream': [(b'zTXt', b'Note\x00\x00' + zlib.compress(b'safe') * 2)],
                  'bad_utf8': [(b'iTXt', b'Note\x00\x00\x00\x00\x00\xff')],
                  'unknown': [(b'vpAg', b'safe')], 'apng': [(b'acTL', struct.pack('>II', 1, 0))]}
        data = png_fixture(color=3 if case == 'palette' else 2, extra=extras.get(case, []))
        if case == 'interlace':
            data = data[:8] + png_chunk(b'IHDR', struct.pack('>IIBBBBB', 3, 1, 8, 2, 0, 0, 1)) + data[33:]
        with pytest.raises(self.m.RpcError): self.validate(data)

    def test_http_ceiling_profile_scope_and_ordinary_body_limit(self):
        import io
        from types import SimpleNamespace
        m = self.m
        calls = []
        def handle(*args):
            calls.append(args)
            return 200, {}, {'ok': True}
        app = SimpleNamespace(config=SimpleNamespace(finch=True, metadata_only=False), handle=handle)
        class MemorySocket:
            def __init__(self, data): self.data, self.output = data, bytearray()
            def makefile(self, mode, *args): return io.BytesIO(self.data)
            def settimeout(self, timeout): pass
            def sendall(self, data): self.output.extend(data)
        def send(body, size):
            data = m.canonical(body).encode()
            assert len(data) <= size
            data += b' ' * (size - len(data))
            header = (f'POST /mcp HTTP/1.1\r\nHost: 127.0.0.1:8765\r\nContent-Type: application/json\r\nContent-Length: {size}\r\n\r\n').encode()
            sock = MemorySocket(header + data)
            m.make_handler(app, {'127.0.0.1:8765'})(sock, ('127.0.0.1', 1), object())
            return bytes(sock.output)
        png = {'method': 'tools/call', 'params': {'name': 'reply_to_finch_request',
               'arguments': {'file': {'content_type': 'image/png'}}}}
        assert send(png, m.FINCH_PNG_HTTP_LIMIT).startswith(b'HTTP/1.1 200')
        before = len(calls)
        assert send(png, m.FINCH_PNG_HTTP_LIMIT + 1).startswith(b'HTTP/1.1 400')
        assert send({'method': 'tools/list'}, 32769).startswith(b'HTTP/1.1 400')
        app.config.finch = False
        assert send(png, m.FILE_HTTP_LIMIT + 1).startswith(b'HTTP/1.1 400')
        assert len(calls) == before

    @pytest.mark.parametrize('width,height', [(1024, 4096), (4096, 1024)])
    def test_full_pixel_limit_streaming_memory_and_exact_expansion(self, width, height):
        import tracemalloc, zlib
        # Largest admitted RGBA scanline stream including 4096 filter bytes.
        packed = zlib.compress(bytes((width * 4 + 1) * height))
        tracemalloc.start()
        try:
            rows = 0
            for row in self.m.png_decoded_rows([packed], width, height, 4):
                assert len(row) == width * 4 and not any(row)
                rows += 1
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert rows == height
        assert peak < 524288, peak  # Streaming decoder, not a 16 MiB bitmap.
        self.validate(png_fixture(width, height, color=6, compressed=packed))

    def test_filters_use_previous_row_and_paeth_ties(self):
        import zlib
        first = bytes([10, 20, 30, 40, 50, 60])
        encoded = b'\x00' + first + b'\x02' + bytes(6) + b'\x04' + bytes(6)
        rows = list(self.m.png_decoded_rows([zlib.compress(encoded)], 2, 3, 3))
        assert rows == [first, first, first]
        encoded = b'\x00' + first + b'\x03' + bytes(6)
        assert list(self.m.png_decoded_rows([zlib.compress(encoded)], 2, 2, 3)) == [first, bytes([5, 10, 15, 22, 30, 37])]

    def test_split_idat_post_image_text_and_chunk_count(self):
        import zlib
        valid = png_fixture(); header, end = valid[:33], png_chunk(b'IEND', b'')
        packed = zlib.compress(b'\x00' + bytes(9))
        split = header + b''.join(png_chunk(b'IDAT', bytes([b])) for b in packed)
        self.validate(split + png_chunk(b'tEXt', b'Note\x00safe') + end)
        with pytest.raises(self.m.RpcError, match='SensitiveOrActiveFileRefused'):
            self.validate(split + png_chunk(b'tEXt', b'Note\x00patient') + end)
        for count in (1021, 1022):
            data = header + png_chunk(b'tEXt', b'Note\x00safe') * count + png_chunk(b'IDAT', packed) + end
            if count == 1021:
                self.validate(data)
            else:
                with pytest.raises(self.m.RpcError): self.validate(data)

    @pytest.mark.parametrize('kind,payload', [(b'gAMA', b'\x00\x00\xb1\x8f'), (b'cHRM', bytes(32)),
                                               (b'sRGB', b'\x00'), (b'pHYs', bytes(9))])
    def test_supported_fixed_metadata_lengths_uniqueness_order(self, kind, payload):
        self.validate(png_fixture(extra=[(kind, payload)]))
        for extras in ([(kind, payload)] * 2, [(kind, payload + b'x')]):
            with pytest.raises(self.m.RpcError): self.validate(png_fixture(extra=extras))
        data = png_fixture()
        with pytest.raises(self.m.RpcError): self.validate(data[:-12] + png_chunk(kind, payload) + data[-12:])

    @pytest.mark.parametrize('slot,value', [(8, 16), (9, 0), (9, 3), (9, 4), (10, 1), (11, 1), (12, 1)])
    def test_all_unsupported_ihdr_variants(self, slot, value):
        data = png_fixture()
        ihdr = bytearray(data[16:29]); ihdr[slot] = value
        with pytest.raises(self.m.RpcError): self.validate(data[:8] + png_chunk(b'IHDR', bytes(ihdr)) + data[33:])

    def test_scoped_caps_upload_and_other_formats_unchanged(self):
        import random, zlib
        m = self.m
        # Incompressible deterministic pixels exceed the former limit without large decoder allocation.
        raw = b''.join(b'\x00' + random.Random(n).randbytes(3072) for n in range(700))
        data = png_fixture(1024, 700, raw=raw)
        assert m.FILE_LIMIT < len(data) < m.FINCH_PNG_LIMIT
        encoded = {'filename': 'art.png', 'content_type': 'image/png', 'content_base64': base64.b64encode(data).decode()}
        assert m.upload_file(encoded, finch=True)['data'] == data
        with pytest.raises(m.RpcError, match='FileSizeRefused'): m.upload_file(encoded)
        assert m.multipart_body({}, m.upload_file(encoded, finch=True), finch=True)[0].count(data) == 1
        with pytest.raises(m.RpcError, match='FileSizeRefused'): m.multipart_body({}, {'filename': 'art.png', 'content_type': 'image/png', 'data': data})
        for mime, name, value in [('text/plain', 'notes.txt', b'x'), ('image/jpeg', 'art.jpg', b'\xff\xd8\xff'), ('image/webp', 'art.webp', b'RIFFxxxxWEBP'), ('application/pdf', 'notes.pdf', b'%PDF-%%EOF'), ('application/json', 'notes.json', b'{}')]:
            m.validate_file(name, mime, value, finch=True)
            with pytest.raises(m.RpcError, match='FileSizeRefused'): m.validate_file(name, mime, value + b'x' * m.FILE_LIMIT, finch=True)
        with pytest.raises(m.RpcError, match='FileSizeRefused'): self.validate(b'x' * (m.FINCH_PNG_LIMIT + 1))
        with pytest.raises(m.RpcError, match='FileSizeRefused'): m.upload_file({**encoded, 'content_base64': 'A' * (4194304 + 1)}, finch=True)
        # Legacy non-Finch PNG signature-only behavior is unchanged; Finch really decodes.
        fake = b'\x89PNG\r\n\x1a\n'
        m.validate_file('art.png', 'image/png', fake)
        with pytest.raises(m.RpcError): self.validate(fake)
        for value in (b'patient', b'PRIVATE KEY', b'<script>', b'/OpenAction'):
            for finch in (False, True):
                with pytest.raises(m.RpcError, match='SensitiveOrActiveFileRefused'):
                    m.validate_file('notes.txt', 'text/plain', value, finch=finch)
