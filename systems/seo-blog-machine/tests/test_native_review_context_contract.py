import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest


def controller():
    source = ROOT / 'systems/seo-blog-machine/scripts/opus_review_loop.py'
    spec = importlib.util.spec_from_file_location('physical_context_controller', source)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


@pytest.mark.parametrize('separator', [' ', '\u2028', '\u2029', '\x85', '\x0b', '\x0c'])
def test_rv459_long_json_remains_one_lossless_physical_record(tmp_path, separator):
    # RV-459's 200017-byte U+2028 probe, extended to all Unicode boundaries.
    # JSON escapes VT/FF; a raw VT/FF is not valid JSON string content.
    packet = tmp_path / 'packet'
    packet.mkdir()
    source = json.dumps({'quote': 'a' * 100000 + separator + 'b' * 100000}, ensure_ascii=False) + '\n'
    (packet / '01-evidence-ledger.json').write_bytes(source.encode())
    extra = ['00-authority.json', '02-serp-brief.json', '03-editorial-brief.md', '04-draft.md']
    for name in extra:
        (packet / name).write_bytes(b'{}\n')
    job = {'packet_dir': str(packet), 'subject_files': ['01-evidence-ledger.json'] + extra,
           'job_id': 'offline-rv459', 'topic_id': 'offline-fixture'}
    producer = controller()
    manifest, _ = producer.seal_subjects(job, 1, tmp_path)
    bundle = producer.build_context_bundle(job, manifest, 1, tmp_path).read_bytes().decode()
    record = bundle.split('===== 01-evidence-ledger.json =====\n', 1)[1].split('\n\n===== ', 1)[0]
    assert len(record.split('\n')) == 1
    reconstructed = re.sub(r'^0001\|', '', record)
    assert reconstructed == source[:-1]
    assert json.loads(reconstructed) == json.loads(source)
    assert manifest['subject_files'][0]['lines'] == 1


@pytest.mark.parametrize('source,lines', [
    (b'', []), (b'\n', ['']), (b'{}', ['{}']), (b'{}\n', ['{}']),
    (b'{}\r\n\t\r\n', ['{}', '\t']), (b'{}\r', ['{}\r']),
    (b'{\r"a": 1}\n', ['{\r"a": 1}']),
    ('a\u2028b\u2029c\x85d\x0be\x0cf\n'.encode(), ['a\u2028b\u2029c\x85d\x0be\x0cf']),
    (b'a\r\nb\nc\r', ['a', 'b', 'c\r']),
])
def test_manifest_and_bundle_preserve_raw_physical_lines(tmp_path, source, lines):
    packet = tmp_path / 'packet'
    packet.mkdir()
    files = ['04-draft.md'] + [f'{i}.json' for i in range(4)]
    for name in files:
        (packet / name).write_bytes(source)
    job = {'packet_dir': str(packet), 'subject_files': files, 'job_id': 'offline', 'topic_id': 'fixture'}
    producer = controller()
    manifest, _ = producer.seal_subjects(job, 1, tmp_path)
    bundle = producer.build_context_bundle(job, manifest, 1, tmp_path).read_bytes().decode()
    expected = '===== 04-draft.md =====\n' + ''.join(f'{n:04d}|{line}\n' for n, line in enumerate(lines, 1)) + '\n'
    assert expected in bundle
    assert manifest['subject_files'][0]['lines'] == len(lines)

@pytest.mark.parametrize('producer,name', [
    ('article_buffer_cli.py', 'CONTROL_FILES'), ('materialize_opus_run.py', 'controls')])
def test_registered_controls_pin_shared_physical_line_dependency(producer, name):
    source = ROOT / 'systems/seo-blog-machine/scripts' / producer
    tree = ast.parse(source.read_text())
    assignment = next(node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == name for target in node.targets))
    namespace = {node.id: Path('/unused') / node.id for node in ast.walk(assignment.value)
                 if isinstance(node, ast.Name)}
    namespace.update(SYSTEM=ROOT / 'systems/seo-blog-machine', BUSINESS=ROOT)
    controls = eval(compile(ast.Expression(assignment.value), str(source), 'eval'), namespace)
    assert ROOT / 'scripts/article_review_physical_lines.py' in controls


ROOT=Path(__file__).resolve().parents[3]

def test_real_controller_bundle_matches_native_custodian_exact_contract(tmp_path):
    source=(ROOT/'systems/seo-blog-machine/scripts/opus_review_loop.py').read_text()
    tree=ast.parse(source)
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='build_context_bundle')
    import os
    from article_review_physical_lines import physical_lines
    ns={'Path':Path,'Any':object,'os':os,'physical_lines':physical_lines,'subject_paths':lambda job:[Path(job['packet_dir'])/'04-draft.md']}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'<controller>','exec'),ns)
    packet=tmp_path/'packet';packet.mkdir();(packet/'04-draft.md').write_text('# Exact draft\nNo clinic implication.\n')
    manifest={'manifest_sha256':'a'*64,'subject_files':[{'path':'04-draft.md'}]}
    output=ns['build_context_bundle']({'packet_dir':str(packet)},manifest,1,tmp_path)
    assert output.read_text()==('SEALED ARTICLE SUBJECTS — READ ONLY\nmanifest_sha256='+'a'*64+'\n\n===== 04-draft.md =====\n0001|# Exact draft\n0002|No clinic implication.\n\n')
