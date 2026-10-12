"""Current GLP93 inputs cannot inherit the historical GLP331 acceptance."""
import hashlib
import importlib.util
from pathlib import Path
import shutil
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


def load():
    path = ROOT / 'scripts/glp93_current_base_binding.py'
    assert path.is_file(), 'explicit current-base builder/helper binding is missing'
    spec = importlib.util.spec_from_file_location('current_binding', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('tampered', [None, 'scripts/build_glp_affiliate_wirein_incremental_v2.py', 'scripts/glp_review_editorial_projection.py'])
def test_current_base_exact_pair_and_tamper_refusal(tmp_path, tampered):
    module = load()
    for name in module.CURRENT_BINDINGS:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    module.validate_current_base(tmp_path)
    if tampered:
        with (tmp_path / tampered).open('ab') as handle:
            handle.write(b'\n# tampered\n')
        with pytest.raises(RuntimeError, match='current_base_dependency_drift'):
            module.validate_current_base(tmp_path)


def test_historical_builder_bytes_remain_exact_and_current_gate_runs_first(tmp_path):
    module = load()
    body = module.historical_dependency_bytes(ROOT, module.BUILDER)
    assert hashlib.sha256(body).hexdigest() == 'a58ac5ecae59727c0106590305188e3db47af78fb399b043004a56fdf3cf8432'
    expected = subprocess.check_output(['git', '-C', str(ROOT), 'show', 'b455e4a9d99b7d393c719204489a59cb65cfc293:'+module.BUILDER])
    assert body == expected
    for name in module.CURRENT_BINDINGS:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    (tmp_path / module.BUILDER).write_bytes(body)
    with pytest.raises(RuntimeError, match='current_base_dependency_drift'):
        module.historical_dependency_bytes(tmp_path, module.BUILDER)


# Execute maintained pure constructor bodies without host-only eager imports.
import ast,json,re,sys,types
sys.path.insert(0,str(ROOT/'scripts'))
import glp_review_editorial_projection as projection
b=types.ModuleType('append_pure_bodies')
b.__dict__.update(Path=Path,re=re,json=json,hashlib=hashlib,
 file_sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest(),
 project_review_inputs=projection.project_review_inputs,
 captured_review_sources=projection.captured_review_sources,
 FIELD_BINDINGS={},HOLD_COPY='Held')
names={'require_zero_review_pending_invariant','append_deliberately_unscored_review_routes','write_provider_add_merit_binding_test','editorial_hold_reason','build_contract','build_integration','build_registry_entry','build_go_manifest_entry','quote','stated','canonical_field_sources','normalize_regulatory_banner','validate_checked_as_of'}
tree=ast.parse((ROOT/'scripts/build_glp_affiliate_wirein_incremental_v2.py').read_text())
exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names],type_ignores=[]),'maintained-append-pure-bodies','exec'),b.__dict__)
N=r'assert\.equal\(pending, (\d+)\);'
D=r'assert\.equal\(rated \+ pending, routes\.length\);'
MODERN="""const appendedRoutes=allRoutes.filter(route=>!legacyRoutes.has(route));
assert.deepEqual(appendedRoutes.sort(),['/reviews/prior/']);
assert.deepEqual(deliberatelyUnscored.sort(),appendedRoutes);
assert.equal(rated + pending + deliberatelyUnscored.length, routes.length);
assert.ok(rated > 0);
assert.equal(pending, 0);
"""

def test_modern_unscored_partition_preserves_zero_legacy_pending(tmp_path):
 p=tmp_path/'test.mjs';p.write_text(MODERN)
 assert b.require_zero_review_pending_invariant(p,N,D)==0
 for broken in [MODERN.replace('pending, 0','pending, 1'),MODERN.replace('assert.deepEqual(deliberatelyUnscored.sort(),appendedRoutes);',''),MODERN.replace('assert.ok(rated > 0);',''),MODERN+'assert.equal(rated + pending, routes.length);']:
  p.write_text(broken)
  with pytest.raises(RuntimeError):b.require_zero_review_pending_invariant(p,N,D)

def test_append_review_partition_names_only_exact_new_routes(tmp_path):
 p=tmp_path/'test.mjs';p.write_text(MODERN)
 b.append_deliberately_unscored_review_routes(p,['new'])
 assert "['/reviews/new/', '/reviews/prior/']" in p.read_text()
 assert 'assert.equal(pending, 0);' in p.read_text()
 with pytest.raises(RuntimeError):b.append_deliberately_unscored_review_routes(p,['new'])

def test_editorial_only_contract_never_grants_tracking(tmp_path):
 from datetime import datetime
 b.datetime=datetime
 url='https://provider.example/'
 raw=tmp_path/'raw.json';raw.write_text(json.dumps({'data':{'markdown':'Provider evidence'}}))
 bundle=tmp_path/'bundle.json';bundle.write_text(json.dumps({'captures':[{'url':url,'markdown':'Provider evidence','raw_capture_path':str(raw),'raw_capture_sha256':hashlib.sha256(raw.read_bytes()).hexdigest(),'status':200}]}))
 research={'captured_at_utc':'2026-10-10T00:00:00Z','first_party_source_captures':{'path':str(bundle),'sha256':hashlib.sha256(bundle.read_bytes()).hexdigest()},'fields':{k:{'value':'Reported information','source_url':url,'verified_as_of':'2026-10-10'} for k in ('first_month_price','states_served','consultation_fee','cancellation','pharmacy_type','shipping_wait')}}
 spec={'program_id':'fixture','display_name':'Fixture','official_url':url,'checked_as_of':'2026-10-10','advertiser_id':'1','network':'MaxBounty','logo_output_file':'fixture.webp','page_copy':{'title':'Fixture','description':'Reported information','bottom_line':'Reported information'},'row_contract':{'best_for':'Research','cons':[],'insurance':'UNKNOWN','medications':'UNKNOWN','pros':[],'starting_price':'UNKNOWN'},'editorial_hold':'campaign landing includes investigational product','regulatory_banner_required':'compounded_medication_fda_disclosure_required'}
 logo={'path':'/assets/fixture.webp','source_asset_url':url+'logo','sha256':'0'*64,'width':1,'height':1,'media_type':'image/webp','surface':'light'}
 c=b.build_contract(spec,research,None,logo,'803003');i=b.build_integration(spec,logo,'2026-10-10T00:00:00Z');r=b.build_registry_entry(spec)
 assert c['row_contract']['promotion_authorized'] is False
 assert c['row_contract']['live_link_allowed'] is False
 assert c['row_contract']['affiliate_href'] is None and c['network_contract']['tracking_url'] is None
 assert c['network_contract']['promotion_authorized_in_this_artifact'] is False
 assert i['route_state']=='READY_EDITORIAL_LINK_DARK' and i['affiliate_cta_state'].startswith('HOLD_')
 assert r['go']['entry_state']=='held' and r['profiles']['glp-clinic-scout']['surfaces']['go'] is False
 assert r['affiliate_relationship_snapshot']['promotion_denied'] is True
 surfaces=r['profiles']['glp-clinic-scout']['surfaces']
 assert surfaces['review'] is True and surfaces['sitemap'] is True
 assert all(surfaces[k] is False for k in ['compare','quiz','calculator','go','guides'])
 with pytest.raises(RuntimeError):b.build_contract(spec,research,'https://tracker.example/',logo,'803003')
 with pytest.raises(RuntimeError):b.editorial_hold_reason({**spec,'editorial_hold':False})

def test_legacy_partition_still_requires_zero(tmp_path):
 p=tmp_path/'test.mjs';p.write_text('assert.equal(pending, 0);\nassert.equal(rated + pending, routes.length);')
 assert b.require_zero_review_pending_invariant(p,N,D)==0
 p.write_text(p.read_text().replace('pending, 0','pending, 2'))
 with pytest.raises(RuntimeError):b.require_zero_review_pending_invariant(p,N,D)

