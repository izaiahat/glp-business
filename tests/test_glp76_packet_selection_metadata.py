"""Execute only maintained packet rebinding on local fixtures; no updater startup."""
import ast
import hashlib
import json
from pathlib import Path
import re
import shutil
from typing import Any

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'scripts/glp_seo_updater_run.py'


def test_rebound_briefs_share_authority_identity_without_changing_content_dates(tmp_path):
    old={'operator_directive_id':'old','queue_sha256':'old-queue','selection_receipt_sha256':'old-receipt'}
    binding={'operator_directive_id':'current','queue_sha256':'current-queue','selection_receipt_sha256':'current-receipt','strategy_queue_sha256':'strategy-queue','strategy_map_sha256':'strategy-map','topic_id':'topic','target_query':'query','target_route':'/guides/exact/'}
    source=tmp_path/'source'; source.mkdir()
    data={'00-authority.json':{},'01-evidence-ledger.json':{'checked_date':'2026-09-03'},'02-serp-brief.json':{'selection_binding':old,'capture':{'attempted_at':'2026-09-03','sha256':'retained'}},'05-claim-source-map.json':{'claim':'approved'}}
    for name,value in data.items(): (source/name).write_text(json.dumps(value))
    brief='\n'.join(['# Approved brief','- Selection: `old-path`','- Selection SHA-256: `old-file`','- Selection receipt SHA-256: `old-receipt`','- Operator directive ID: `old`','- Queue SHA-256: `old-queue`','- Strategy queue SHA-256: `old-strategy-queue`','- Strategy map SHA-256: `old-strategy-map`','- Current content base: `old-base` SHA-256 `old-base-sha`','- PT as-of: `2026-09-03`','Approved content remains.'])+'\n'
    (source/'03-editorial-brief.md').write_text(brief)
    (source/'04-draft.md').write_text('Approved article, captured 2026-09-03.\n')
    selection_path=tmp_path/'selection.json'; selection_path.write_text('{"fresh":true}')
    authority_path=tmp_path/'authority.json'; authority_path.write_text('{"fresh":true}')
    selection={'receipt_sha256':'current-receipt','selected':[{'operator_directive_id':'current'}]}
    authority={'topic_id':'topic','target_route':'/guides/exact/','target_query':'query','article_id':'original','inputs':{'current_content_base_state':{'path':'/current/base.json','sha256':'current-base-sha'}}}
    def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    def atomic_json(path,value): Path(path).write_text(json.dumps(value,sort_keys=True))
    spec={'path':str(selection_path),'sha256':sha(selection_path),'receipt_sha256':'current-receipt'}
    scope={'Path':Path,'Any':Any,'shutil':shutil,'json':json,'re':re,'atomic_json':atomic_json,'sha':sha,'sha_bytes':lambda b:hashlib.sha256(b).hexdigest(),'utc_now':lambda:'2026-10-03T00:00:00Z','selection_binding':lambda s:binding,'selection_spec':lambda p:spec}
    tree=ast.parse(SOURCE.read_text())
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in {'rebind_packet','rebind_packet_selection_metadata'}]
    exec(compile(ast.Module(body=functions,type_ignores=[]),str(SOURCE),'exec'),scope)
    target=tmp_path/'target'; output=tmp_path/'output'; output.mkdir()
    scope['rebind_packet'](source,target,authority,authority_path,selection,selection_path,output)
    authored=json.loads((target/'00-authority.json').read_text())
    serp=json.loads((target/'02-serp-brief.json').read_text())
    for key,value in binding.items(): assert serp['selection_binding'][key]==authored['selection_binding'][key]==value
    assert serp['selection_binding']['selection_path']==spec['path']
    assert serp['selection_binding']['selection_sha256']==spec['sha256']
    assert serp['capture']==data['02-serp-brief.json']['capture']
    current_brief=(target/'03-editorial-brief.md').read_text()
    expected={'Selection':spec['path'],'Selection SHA-256':spec['sha256'],'Selection receipt SHA-256':'current-receipt','Operator directive ID':'current','Queue SHA-256':'current-queue','Strategy queue SHA-256':'strategy-queue','Strategy map SHA-256':'strategy-map'}
    for label,value in expected.items(): assert f'- {label}: `{value}`' in current_brief
    assert '- Current content base: `/current/base.json` SHA-256 `current-base-sha`' in current_brief
    assert '- PT as-of: `2026-09-03`' in current_brief and 'Approved content remains.' in current_brief
    for name in ['01-evidence-ledger.json','04-draft.md','05-claim-source-map.json']: assert (target/name).read_bytes()==(source/name).read_bytes()
    receipt=json.loads((output/'packet-authority-rebind.json').read_text())
    assert receipt['authored_subject_files_changed']==['00-authority.json','02-serp-brief.json','03-editorial-brief.md']
    assert receipt['prioritization_changed'] is False
