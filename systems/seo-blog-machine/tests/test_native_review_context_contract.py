import ast
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]

def test_real_controller_bundle_matches_native_custodian_exact_contract(tmp_path):
    source=(ROOT/'systems/seo-blog-machine/scripts/opus_review_loop.py').read_text()
    tree=ast.parse(source)
    fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='build_context_bundle')
    import os
    ns={'Path':Path,'Any':object,'os':os,'subject_paths':lambda job:[Path(job['packet_dir'])/'04-draft.md']}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'<controller>','exec'),ns)
    packet=tmp_path/'packet';packet.mkdir();(packet/'04-draft.md').write_text('# Exact draft\nNo clinic implication.\n')
    manifest={'manifest_sha256':'a'*64,'subject_files':[{'path':'04-draft.md'}]}
    output=ns['build_context_bundle']({'packet_dir':str(packet)},manifest,1,tmp_path)
    assert output.read_text()==('SEALED ARTICLE SUBJECTS — READ ONLY\nmanifest_sha256='+'a'*64+'\n\n===== 04-draft.md =====\n0001|# Exact draft\n0002|No clinic implication.\n\n')
