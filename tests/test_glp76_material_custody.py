"""Portable accepted-current-producer custody regression; authority is synthetic."""
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]

def load():
    spec = importlib.util.spec_from_file_location('material_contract', ROOT/'systems/seo-blog-machine/scripts/carrier_fixture_contracts.py')
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    return m

def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}

def test_current_registered_producer_dates_supersede_stale_fixture_only_under_accepted_custody(tmp_path, monkeypatch):
    m = load(); monkeypatch.setattr(m, 'BUSINESS', tmp_path)
    base = tmp_path/'accepted'; route = '/guides/retained/'
    put(base/'tests/fixtures/d2j-material-lastmod.json', {'schema':'d2j-material-date-fixtures-v1','rows':[{'route':route,'old_date':'2026-07-23','required_date':'2026-09-09'}]})
    put(base/'data/blog/content-batch.json', {'articles':[{'canonical_path':route,'updated_at':'2026-07-23'}]})
    put(base/'data/blog/content-national-wow-additions.json', {'articles':[]})
    put(base/'data/page-content-provenance.json', {'schema':'glp-page-content-provenance-v1','generated_for_release':'2026-09-27','pages':{route:{'lastmod':'2026-09-27','content_sha256':'a'*64}}})
    page = base/'public/guides/retained/index.html'; page.parent.mkdir(parents=True)
    page.write_text('<script>{"dateModified":"2026-09-27"}</script>')
    producer = base/'scripts/build-lastmod-provenance.mjs'; producer.parent.mkdir()
    producer.write_bytes((ROOT/'tests/fixtures/glp76-carrier-material/build-lastmod-provenance.mjs').read_bytes())
    put(base/'package.json', {'scripts':{'build:validated':'node scripts/build-staging.mjs && node scripts/build-lastmod-provenance.mjs && npm run validate:coverage'}})
    manifest = tmp_path/'manifest.json'
    put(manifest, {'release_root':str(base),'release_rows':[{'path':str(p.relative_to(base)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in base.rglob('*') if p.is_file()]})
    final = put(tmp_path/'final.json', {'status':'PASS_PRODUCTION_TERMINAL','deployment':{'deployment_id':'fixture-deployment'}})
    pointer = tmp_path/'reports/glp-clinic-scout/seo-machine/current-content-base.json'
    ptr={'schema':'glp-opus-current-content-base-v1','status':'PASS','deployment_id':'fixture-deployment','release_root':str(base),'manifest':{'path':str(manifest),'sha256':hashlib.sha256(manifest.read_bytes()).hexdigest()},'final_receipt':final}
    put(pointer, ptr)
    before={str(p):p.read_bytes() for p in base.rglob('*') if p.is_file()}
    result=m.material_binding(base)
    assert result['material_dates']=={route:'2026-09-27'}
    assert result['fixture_baseline']=='2026-09-27'
    assert result['custody_mode']=='accepted_registered_render_comparison'
    # Neither a source filename, matching dates nor a copied PASS suffices.
    for file in (producer, page, base/'data/page-content-provenance.json', manifest, pointer, tmp_path/'final.json'):
        original=file.read_bytes(); file.write_bytes(original+b' ')
        try:
            if file==pointer: # JSON whitespace is not semantic drift: change deployment instead.
                bad=dict(ptr, deployment_id='wrong'); put(pointer,bad)
            with pytest.raises(RuntimeError): m.material_binding(base)
        finally: file.write_bytes(original)
    assert before=={str(p):p.read_bytes() for p in base.rglob('*') if p.is_file()}
    # Exercise the actual emitted caller as well as the custody gate: a stale
    # already-present constant must not strand the now-authenticated binding.
    import subprocess
    source=(ROOT/'tests/fixtures/glp76-carrier-material/postbuild-aeo-surface.mjs').read_text()
    patched=m.patch_material_date_resolver(source,result)
    program=r'''
const source=JSON.parse(process.argv[1]);
const constant=source.match(/^const CARRIER_ACCEPTED_MATERIAL_DATES = .*;$/m)[0];
const binding=source.match(/^    const materialDate = .*;$/m)[0];
const select=new Function('route','materialDates','articles','inheritedDisclosureDates',constant+'\n'+binding+'\nreturn materialDate;');
if(select('/guides/retained/',new Map(),new Map(),()=>({lastmod:'2026-09-09'}))!=='2026-09-27')throw Error('accepted_current_material_date_not_carried');
if(select('/guides/retained/',new Map([['/guides/retained/','2026-10-02']]),new Map(),()=>null)!=='2026-10-02')throw Error('explicit_material_priority_changed');
'''
    executed=subprocess.run(['node','--input-type=module','-e',program,json.dumps(patched)],capture_output=True,text=True)
    assert executed.returncode==0,executed.stderr
    assert m.patch_material_date_resolver(patched,result)==patched
    # An unrecognized producer may not waive the old fixture guard.
    producer.write_text('export function prepareLastmod() {}')
    with pytest.raises(RuntimeError): m.material_binding(base)
