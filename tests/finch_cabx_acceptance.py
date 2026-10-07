"""Synthetic framing plus a pinned published assertion; no provenance trust."""
import base64
import hashlib
from pathlib import Path
import socket
import struct

import pytest
from finch_png_acceptance import png_chunk, png_fixture
from test_rook_mcp_events import module

UUID_SUFFIX = bytes.fromhex('00110010800000aa00389b71')
# c2pa-rs JUMBF_EMBEDDED_FILE_UUID; bfdb is a child box, not this content type.
EMBEDDED_FILE_UUID = bytes.fromhex('40cb0c32bb8a489da70b2ad6f47f4369')


def box(kind, data=b''):
    return struct.pack('>I', len(data) + 8) + kind + data


def description(kind, label, toggles=3, optional=b''):
    uuid = kind + UUID_SUFFIX if len(kind) == 4 else kind
    assert len(uuid) == 16
    return box(b'jumd', uuid + bytes([toggles]) + label + b'\0' + optional)


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


def svg_fixture(path='M0 0 L10 10 H20 V30 C0 0 1 1 2 2 Z', root='', path_attrs='fill="#000"'):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" {root}>'
            f'<path d="{path}" {path_attrs}/></svg>').encode('utf-8')


def embedded_svg(data, mime=b'image/svg+xml', filename=b''):
    fields = bytes([bool(filename)]) + mime + b'\0' + (filename + b'\0' if filename else b'')
    return manifest(superbox(EMBEDDED_FILE_UUID, b'example', [box(b'bfdb', fields), box(b'bidb', data)]))


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

    @pytest.mark.parametrize('filename', [b'', b'outline.svg'])
    def test_embedded_svg_synthetic_observed_shape_not_original(self, filename):
        # New artificial geometry matching REPORTED counts, not original artwork.
        # Eight synthetic closed, curved badge cells, not recovered artwork.
        commands = []
        for cell in range(8):
            x, y = 20 + (cell % 4) * 130, 20 + (cell // 4) * 260
            commands.append(f'M{x} {y}')
            for line in range(3 if cell < 4 else 2):
                commands.append(f'L{x + 20 + line * 20} {y + 10 + line * 15}')
            if cell < 2: commands.append(f'H{x + 100}')
            for vertical in range(2 if cell < 5 else 1):
                commands.append(f'V{y + 70 + vertical * 20}')
            for curve in range(5 if cell < 4 else 4):
                cx = x + 90 - curve * 15
                control = '540.717' if cell == 3 and curve == 0 else str(cx + 25)
                commands.append(f'C{control} {y + 100} {cx} {y + 120} {cx - 15} {y + 110}')
            commands.append('Z')
        path = ' '.join(commands)
        assert len(path) <= 2282
        path += ' ' * (2282 - len(path))
        data = svg_fixture(path, 'width="600px" height="600" viewBox="0 0 600 600"')
        data += b' ' * (2415 - len(data))
        assert len(data) == 2415 and len(path.encode()) == 2282
        assert sum(len(c[1:].split()) for c in commands) == 287
        assert sum(c[0] != 'M' for c in commands) == 79
        assert {c: sum(s[0] == c for s in commands) for c in 'MLHVCZ'} == {
            'M': 8, 'L': 20, 'H': 2, 'V': 13, 'C': 36, 'Z': 8}
        png = png_fixture(extra=[(b'caBX', embedded_svg(data, filename=filename))])
        value = {'filename': 'art.png', 'content_type': 'image/png', 'content_base64': base64.b64encode(png).decode()}
        file = self.m.upload_file(value, finch=True)
        assert file['data'] == png
        assert self.m.multipart_body({}, file, finch=True)[0].count(png) == 1

    @pytest.mark.parametrize('path', ['M0 0', 'M-1e6 +1e6 L.5 -.5 H1. V1e-2 C0,0,1,1,2,2 Z',
        'M0-1L2+3H4V5C0 0 1 1 2 2Z', 'M0 0 Z M1 1 L2 2'])
    def test_embedded_svg_supported_complete_path_grammar(self, path):
        self.validate(embedded_svg(svg_fixture(path)))

    @pytest.mark.parametrize('path', ['', ' ', 'L0 0', 'Z', 'M0', 'M0 0 1 1', 'M0 0 L1',
        'M0 0 L1 2 3 4', 'M0 0 H1 2', 'M0 0 V1 2', 'M0 0 C1 2 3 4 5',
        'M0 0 C1 2 3 4 5 6 7', 'M0 0 Z1', 'M0 0 garbage', 'M0 0 LNaN 1',
        'M0 0 LInf 1', 'M0 0 L1e999 1', 'M0 0 L1000000.1 1', 'M0 0 C0 0 -1000001 0 0 0',
        'M,0 0', 'M0,,0', 'M0 0,', 'M0 0,L1 2', 'M0 0 L1,', 'M0 0 L1e 2',
        'M0 0 L1e+ 2', 'M0 0 L1.2.3.4', 'M0 0 L1;2', 'M0 0\u00a0L1 2'])
    def test_embedded_svg_path_syntax_and_numeric_bounds(self, path):
        with pytest.raises(self.m.RpcError): self.validate(embedded_svg(svg_fixture(path)))

    @pytest.mark.parametrize('command', list('mlhvczASQTasqt'))
    def test_embedded_svg_unsupported_commands(self, command):
        with pytest.raises(self.m.RpcError):
            self.validate(embedded_svg(svg_fixture('M0 0 ' + command + '0 0 0 0 0 0 0')))

    @pytest.mark.parametrize('root', ['width="4096" height=".5px" viewBox="-1e6 1e6 4096 4096"',
        'width="1px" height="4096px" viewBox="0,0,1,1"', 'fill="none"'])
    def test_embedded_svg_root_dimension_and_viewbox_acceptance(self, root):
        self.validate(embedded_svg(svg_fixture(root=root)))

    @pytest.mark.parametrize('root', ['width="0"', 'height="-1"', 'width="4097"', 'height="1e999"',
        'width="100%"', 'width="1em"', 'width="NaN"', 'width="1 px"', 'height="+4096.01px"',
        'viewBox="0 0 1"', 'viewBox="0 0 1 1 1"', 'viewBox="0 0 0 1"', 'viewBox="0 0 1 -1"',
        'viewBox="1000001 0 1 1"', 'viewBox="0 0 1e999 1"', 'viewBox="0,,0,1,1"', 'viewBox="0 0 1 1,"',
        'viewBox="0-0 1 1"', 'viewBox=".0.0 1 1"', 'viewBox="0 0 1000001 1"'])
    def test_embedded_svg_bad_dimensions_and_viewbox(self, root):
        with pytest.raises(self.m.RpcError): self.validate(embedded_svg(svg_fixture(root=root)))

    @pytest.mark.parametrize('fill', ['none', 'black', 'white', '#123', '#ABCDEF'])
    @pytest.mark.parametrize('location', ['root', 'path'])
    def test_embedded_svg_fill_allowlist(self, fill, location):
        self.validate(embedded_svg(svg_fixture(root=f'fill="{fill}"' if location == 'root' else '',
            path_attrs=f'fill="{fill}"' if location == 'path' else '')))

    @pytest.mark.parametrize('fill', ['red', 'currentColor', 'inherit', 'url(#x)', 'url(https://example.invalid)',
        '#12', '#1234', '#12345678', '#xyz', 'rgb(0,0,0)', 'var(--x)', 'black;stroke:red'])
    @pytest.mark.parametrize('location', ['root', 'path'])
    def test_embedded_svg_unsupported_fill(self, fill, location):
        with pytest.raises(self.m.RpcError):
            self.validate(embedded_svg(svg_fixture(root=f'fill="{fill}"' if location == 'root' else '',
                path_attrs=f'fill="{fill}"' if location == 'path' else '')))

    @pytest.mark.parametrize('attr', ['onload="x"', 'style="fill:black"', 'class="x"', 'id="x"',
        'href="https://example.invalid"', 'xml:base="https://example.invalid"', 'transform="scale(2)"',
        'xmlns:x="https://example.invalid" x:fill="black"', 'xmlns:x="http://www.w3.org/2000/svg"'])
    @pytest.mark.parametrize('location', ['root', 'path'])
    def test_embedded_svg_unknown_attributes_and_namespaces(self, attr, location):
        with pytest.raises(self.m.RpcError):
            self.validate(embedded_svg(svg_fixture(root=attr if location == 'root' else '',
                path_attrs=attr if location == 'path' else '')))

    @pytest.mark.parametrize('data', [b'', b'not XML', b'\xff', b'<svg/>',
        b'<svg xmlns="https://example.invalid"><path d="M0 0"/></svg>',
        b'<svg xmlns="http://www.w3.org/2000/svg"/>',
        svg_fixture().replace(b'<path', b'<g><path').replace(b'/></svg>', b'/></g></svg>'),
        svg_fixture().replace(b'</svg>', b'<path d="M0 0"/></svg>'),
        svg_fixture().replace(b'/>', b'><path d="M0 0"/></path>'),
        svg_fixture().replace(b'<path', b'<script'), svg_fixture().replace(b'<path', b'<foreignObject'),
        svg_fixture().replace(b'<path', b'<animate'), svg_fixture().replace(b'<path', b'<style'),
        svg_fixture().replace(b'<path', b'<use'), svg_fixture().replace(b'<path', b'<image'),
        svg_fixture().replace(b'<path', b'text<path'), svg_fixture().replace(b'/>', b'>text</path>'),
        b'text' + svg_fixture(), svg_fixture() + b'text', svg_fixture() + svg_fixture(),
        svg_fixture().replace(b'fill="#000"', b'fill="#000" fill="black"'),
        svg_fixture().replace(b'd="', b'width="1" d="'),
        svg_fixture().replace(b'<svg ', b'<svg d="M0 0" '),
        b'<?xml version="1.0"?>' + svg_fixture(), b'<?probe x?>' + svg_fixture(),
        svg_fixture().replace(b'<path', b'<!--x--><path'),
        b'<!DOCTYPE svg [<!ENTITY x "x">]>' + svg_fixture(),
        b'<!DOCTYPE svg SYSTEM "https://example.invalid/dtd">' + svg_fixture(),
        svg_fixture().replace(b'<path', b'<![CDATA[ ]]><path'),
        svg_fixture().replace(b'#000', b'&#35;000'), svg_fixture().replace(b'#000', b'&amp;'),
        svg_fixture().replace(b'M0 0', b'M0&#32;0'),
        svg_fixture().replace(b'<path', b'&unknown;<path')])
    def test_embedded_svg_strict_xml_subset(self, data):
        with pytest.raises(self.m.RpcError): self.validate(embedded_svg(data))

    @pytest.mark.parametrize('encoding', ['utf-8', 'utf-16-le', 'utf-16-be'])
    @pytest.mark.parametrize('route', ['direct', 'finch'])
    def test_embedded_svg_utf8_only(self, encoding, route, monkeypatch):
        # BOM-less UTF-16 decodes as UTF-8 with NULs, but Expat autodetects it.
        data = svg_fixture('M0 0').decode('utf-8').encode(encoding)
        png = png_fixture(extra=[(b'caBX', embedded_svg(data))])
        if encoding == 'utf-8':
            if route == 'direct':
                self.m.png_embedded_svg(data)
            else:
                assert self.m.validate_file('art.png', 'image/png', png, finch=True)['data'] == png
        else:
            assert '\x00' in data.decode('utf-8')
            monkeypatch.setattr(self.m.expat, 'ParserCreate', lambda *a, **k: pytest.fail('XML parser entered'))
            if route == 'direct':
                with pytest.raises(ValueError, match='SVG XML NUL'): self.m.png_embedded_svg(data)
            else:
                with pytest.raises(self.m.RpcError, match='InvalidOrUnsupportedPNG'):
                    self.m.validate_file('art.png', 'image/png', png, finch=True)

    def test_embedded_svg_all_resource_boundaries(self):
        for length in (4096, 4097):
            path = 'M0 0' + ' ' * (length - 4)
            if length == 4096: self.validate(embedded_svg(svg_fixture(path)))
            else:
                with pytest.raises(self.m.RpcError): self.validate(embedded_svg(svg_fixture(path)))
        for operands in (1024, 1025):
            path = 'M0 0 ' + 'M0 0 ' * 511 + ('H0' if operands == 1025 else '')
            if operands == 1024: self.validate(embedded_svg(svg_fixture(path)))
            else:
                with pytest.raises(self.m.RpcError): self.validate(embedded_svg(svg_fixture(path)))
        for segments in (256, 257):
            path = 'M0 0 ' + 'H0 ' * segments
            if segments == 256: self.validate(embedded_svg(svg_fixture(path)))
            else:
                with pytest.raises(self.m.RpcError): self.validate(embedded_svg(svg_fixture(path)))
        for length in (8192, 8193):
            data = svg_fixture(); data += b' ' * (length - len(data))
            if length == 8192: self.validate(embedded_svg(data))
            else:
                with pytest.raises(self.m.RpcError): self.validate(embedded_svg(data))
        with pytest.raises(self.m.RpcError):
            self.validate(embedded_svg(b'<svg xmlns="http://www.w3.org/2000/svg">' + b'<g>' * 1000 + b'</g>' * 1000 + b'</svg>'))
        with pytest.raises(self.m.RpcError):
            self.validate(embedded_svg(svg_fixture().replace(b'<path', b'<path ' + b'x="x" ' * 1000)))

    def test_embedded_svg_nonpath_attribute_byte_budget(self):
        self.validate(embedded_svg(svg_fixture(root='width="' + '0' * 127 + '1"')))
        with pytest.raises(self.m.RpcError):
            self.validate(embedded_svg(svg_fixture(root='width="' + '0' * 128 + '1"')))

    def test_embedded_svg_pairing_raw_scans_caps_and_other_profiles(self):
        with pytest.raises(self.m.RpcError): self.validate(embedded_svg(png_fixture()))
        with pytest.raises(self.m.RpcError):
            self.validate(embedded_svg(svg_fixture(), mime=b'image/svg+xml;charset=utf-8'))
        # MIME is local to each embedded-file superbox, never a global hint.
        def assertion(mime, data):
            return superbox(EMBEDDED_FILE_UUID, b'example', [box(b'bfdb', b'\0' + mime + b'\0'), box(b'bidb', data)])
        siblings = assertion(b'image/svg+xml', svg_fixture()) + assertion(b'image/png', b'opaque')
        self.validate(superbox(b'c2pa', b'c2pa', [superbox(b'c2as', b'example', [siblings])]))
        bad = assertion(b'image/png', b'opaque') + assertion(b'image/svg+xml', b'not XML')
        with pytest.raises(self.m.RpcError):
            self.validate(superbox(b'c2pa', b'c2pa', [superbox(b'c2as', b'example', [bad])]))
        for children in ([box(b'bidb', svg_fixture())], [box(b'bfdb', b'\0image/svg+xml\0')],
                [box(b'bfdb', b'\0image/svg+xml\0'), box(b'json', svg_fixture())],
                [box(b'bfdb', b'\0image/svg+xml\0'), box(b'bidb', svg_fixture()), box(b'bidb', svg_fixture())]):
            with pytest.raises(self.m.RpcError):
                self.validate(manifest(superbox(EMBEDDED_FILE_UUID, b'example', children)))
        # PNG/JPEG remain opaque; SVG under other MIME does not gain XML validation.
        for mime in (b'image/png', b'image/jpeg'):
            self.validate(embedded_svg(b'opaque legacy bytes', mime=mime))
        with pytest.raises(self.m.RpcError, match='SensitiveOrActiveFileRefused'):
            self.validate(embedded_svg(svg_fixture() + b' patient '))
        data = svg_fixture()
        with pytest.raises(self.m.RpcError, match='FileNameOrTypeRefused'):
            self.m.validate_file('outline.svg', 'image/svg+xml', data, finch=True)
        with pytest.raises(self.m.RpcError, match='FileNameOrTypeRefused'):
            self.m.validate_file('outline.svg', 'image/svg+xml', data)
        payload = embedded_svg(data)
        budget = self.m.PNG_METADATA_LIMIT - len(payload) - 8
        payload = box(b'jumb', payload[8:] + box(b'free', b'x' * budget))
        assert len(payload) == self.m.PNG_METADATA_LIMIT
        self.validate(payload)
        with pytest.raises(self.m.RpcError): self.validate(payload, [(b'tEXt', b'Note\0safe')])

    @pytest.mark.parametrize('kind,children', [(b'cbor', [box(b'cbor', b'\xa0')]),
        (b'json', [box(b'json', b'{"note":"safe"}')]),
        (EMBEDDED_FILE_UUID, [box(b'bfdb', b'\x00image/png\0'), box(b'bidb', png_fixture())]),
        (EMBEDDED_FILE_UUID, [box(b'bfdb', b'\x01image/jpeg\0thumb.jpg\0'), box(b'bidb', b'\xff\xd8\xff\xd9')])])
    def test_supported_content_and_embedded_file_framing(self, kind, children):
        self.validate(manifest(superbox(kind, b'example', children + [box(b'free', bytes(16))])))

    @pytest.mark.parametrize('filename', [b'', b'thumb.png'])
    @pytest.mark.parametrize('salt', [b'', bytes(16), bytes(32)])
    def test_standard_embedded_file_uuid_preserves_bytes_with_filename_and_salt(self, filename, salt):
        fields = bytes([bool(filename)]) + b'image/png\0' + (filename + b'\0' if filename else b'')
        assertion = superbox(EMBEDDED_FILE_UUID, b'c2pa.thumbnail.claim',
            [box(b'bfdb', fields), box(b'bidb', png_fixture())],
            19 if salt else 3, box(b'c2sh', salt) if salt else b'')
        data = png_fixture(extra=[(b'caBX', manifest(assertion))])
        assert self.m.validate_file('art.png', 'image/png', data, finch=True)['data'] == data

    @pytest.mark.parametrize('uuid', [b'bfdb' + UUID_SUFFIX, b'bidb' + UUID_SUFFIX,
        EMBEDDED_FILE_UUID[:-1] + b'\x68', b'json' + UUID_SUFFIX[:-1] + b'\x70'])
    def test_substituted_or_near_match_content_uuid_is_refused(self, uuid):
        assertion = superbox(uuid, b'c2pa.thumbnail.claim',
            [box(b'bfdb', b'\0image/png\0'), box(b'bidb', png_fixture())])
        with pytest.raises(self.m.RpcError, match='InvalidOrUnsupportedPNG'):
            self.validate(manifest(assertion))

    @pytest.mark.parametrize('substituted', [False, True])
    def test_published_c2pa_rs_embedded_file_assertion(self, substituted):
        # Independently published bytes, NOT produced by description()/superbox().
        # Exact upstream URL, SHA-256 and extraction range are in the Rook README.
        assertion = (Path(__file__).parent / 'fixtures/finch_cabx_embedded_file.jumb').read_bytes()
        assert len(assertion) == 12628
        assert hashlib.sha256(assertion).hexdigest() == '9fdd17837a0aa7ba7ab60c974011a3cee9ecaae2a94d868504c184942204cd61'
        assert assertion[16:32] == bytes.fromhex('40cb0c32bb8a489da70b2ad6f47f4369')
        if substituted:
            # Change only the enclosing type; all published child bytes stay exact.
            assertion = assertion[:16] + b'bfdb' + UUID_SUFFIX + assertion[32:]
            with pytest.raises(self.m.RpcError, match='InvalidOrUnsupportedPNG'):
                self.validate(manifest(assertion))
        else:
            # Outer PNG and store/claim wrappers remain synthetic and unsigned.
            data = png_fixture(extra=[(b'caBX', manifest(assertion))])
            assert self.m.validate_file('art.png', 'image/png', data, finch=True)['data'] == data

    @pytest.mark.parametrize('marker', [b'PRIVATE KEY', b'Authorization: example', b'Bearer example',
        b'password=example', b'api_key=example', b'client_secret=example', b'patient', b'dob', b'ssn',
        b'diagnosis', b'medical record', b'<html>', b'<script>', b'/JavaScript', b'/JS', b'/Launch',
        b'/OpenAction', b'/AA', b'/EmbeddedFile'])
    @pytest.mark.parametrize('location', ['label', 'json', 'cbor', 'embedded', 'padding', 'salt'])
    def test_every_metadata_byte_keeps_existing_marker_detection(self, marker, location):
        if location == 'label':
            assertion = superbox(b'json', marker, [box(b'json', b'{}')])
        elif location == 'embedded':
            assertion = superbox(EMBEDDED_FILE_UUID, b'example', [box(b'bfdb', b'\0image/png\0'), box(b'bidb', png_fixture(extra=[(b'tEXt', b'Note\0 ' + marker + b' ')]))])
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
        with pytest.raises(self.m.RpcError): self.validate(manifest(superbox(EMBEDDED_FILE_UUID, b'example', children)))

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
        assertion = superbox(EMBEDDED_FILE_UUID, b'example', [box(b'bfdb', b'\0image/png\0'), box(b'bidb', thumbnail)])
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
