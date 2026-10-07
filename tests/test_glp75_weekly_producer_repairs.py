"""Focused actual-producer regressions; all external effects are offline fixtures."""
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import types

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def module(monkeypatch, name, **values):
    value = types.ModuleType(name)
    value.__dict__.update(values)
    monkeypatch.setitem(sys.modules, name, value)
    return value


def dependencies(monkeypatch):
    module(monkeypatch, 'ops', __path__=[str(ROOT / 'ops')])
    module(monkeypatch, 'ops.glp', __path__=[str(ROOT / 'ops/glp')])
    module(monkeypatch, 'ops.glp.seo_business_context',
           load_business_context=lambda _: {}, load_seo_skills=lambda: {})


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


@pytest.mark.parametrize('day, expected', [
    ('2026-09-27', '2026-09-28'), ('2026-09-28', '2026-09-28'),
    ('2026-10-03', '2026-09-28'), ('2026-10-04', '2026-10-05'),
    ('2026-10-05', '2026-10-05'), ('2026-10-06', '2026-10-05'),
    ('2026-12-27', '2026-12-28'), ('2027-01-03', '2027-01-04'),
])
def test_actual_continuation_uses_sunday_workweek(tmp_path, monkeypatch, day, expected):
    dependencies(monkeypatch)
    continuation = load('actual_weekly_continuation', 'ops/glp/seo_weekly_continuation.py')
    release = tmp_path / 'release'
    copy = release / 'data/page-seo-copy.json'
    save(copy, {'schema': 'clinic-scout-page-seo-v1', 'routes': {}})
    manifest = tmp_path / 'inputs.json'
    save(manifest, {'source_bindings': {}, 'inputs': {
        'content-base': {'release_root': str(release), 'deployment_id': 'OFFLINE'},
        'seo-copy': {'sha256': sha(copy)},
        'gsc-28': {'captured_at_utc': '2026-10-05T01:35:00Z'},
    }, 'current_base_sha256': 'OFFLINE'})
    module(monkeypatch, 'ops.glp.seo_weekly_modules', read_bound=lambda x: x,
           validate_snapshot=lambda *_: None)
    module(monkeypatch, 'ops.glp.seo_weekly_strategy', build_strategy=lambda *_: None)
    reached = []

    class StopAtEmitter(Exception):
        pass

    def emit(*_, week_start, **__):
        reached.append(week_start)
        raise StopAtEmitter

    module(monkeypatch, 'ops.glp.seo_nonarticle_emitter', emit_bundle=emit, apply_operations=None)
    controller = tmp_path / 'controller.py'
    controller.write_text("import json\nfrom pathlib import Path\ndef main(argv):\n"
                          " p=Path(argv[-1]);p.mkdir();(p/'receipt.json').write_text(json.dumps({'patch_intakes':{'rows':[]}}));return 0\n")
    monkeypatch.setattr(continuation, 'CONTROLLER', controller)
    monkeypatch.setattr(continuation, 'BUSINESS', tmp_path / 'business')
    monkeypatch.setattr(continuation, 'bootstrap', lambda: None)
    with pytest.raises(StopAtEmitter):
        continuation.run_week(manifest, tmp_path / 'out', report_date=dt.date.fromisoformat(day))
    assert reached == [expected]


def checkpoint(tmp_path, day, status, finding_status):
    target = tmp_path / day
    analysis = target / 'analysis'
    save(analysis / 'STRATEGY.json', {'week_start': '2026-10-05', 'markdown': 'OFFLINE'})
    save(analysis / 'NON-ARTICLE-INTAKE.json', {'OFFLINE': True})
    save(target / 'collection/MODULE-INPUTS.json', {'OFFLINE': True})
    manifest = target / 'collection/MODULE-INPUTS.json'
    save(target / 'collection/COLLECTION-RESULT.json', {'manifest': {'path': str(manifest), 'sha256': sha(manifest)}})
    save(target / 'INTENT.json', {'report_date': day})
    save(target / 'R2-AUTHORITY-REANCHOR.json', {'status': 'OFFLINE_RETAINED'})
    save(target / 'METERED-CLAIM.json', {'granted': False})
    files = [{'path': str(p), 'sha256': sha(p)} for p in sorted(analysis.iterdir())]
    save(analysis / 'WEEK-RECEIPT.json', {'status': 'PARTIAL_DRY_RUN', 'files': files,
        'week_start': '2026-10-05', 'module_statuses': {'page2': 'PASS_ANALYSIS_ONLY', 'keyword_gap': 'UNKNOWN_INPUT'},
        'source_operations': 0})
    files = [{'path': str(p), 'sha256': sha(p)} for p in sorted(target.rglob('*')) if p.is_file()]
    save(target / 'receipt.json', {'status': status, 'files': files,
         'finding': {'status': finding_status}})
    return target


@pytest.mark.parametrize('status, finding_status, recover', [
    ('PARTIAL', 'BLOCKED_FINDING_RECONCILE_NO_RECOLLECTION', True),
    ('BLOCKED_FINDING_RECONCILE_NO_RECOLLECTION', 'BLOCKED_FINDING_RECONCILE_NO_RECOLLECTION', True),
    ('PARTIAL', 'VERIFIED_EXACT_READBACK', False),
    ('PARTIAL', 'NOT_REQUESTED_ANALYSIS_ONLY', False),
])
def test_actual_stage_recovers_only_nested_finding_failure(tmp_path, monkeypatch, status, finding_status, recover):
    dependencies(monkeypatch)
    stage = load('actual_weekly_source_stage', 'scripts/glp_weekly_seo_source_stage.py')
    continuation = load('recovery_workweek_contract', 'ops/glp/seo_weekly_continuation.py')
    module(monkeypatch, 'ops.glp.seo_weekly_continuation', workweek_start=continuation.workweek_start,
           run_week=lambda *_a, **_k: pytest.fail('no analysis replay'))
    module(monkeypatch, 'ops.glp.seo_nonarticle_stage', admit=lambda *_a, **_k: {'status': 'OFFLINE_HELD'}, save=save)
    module(monkeypatch, 'ops.glp.cogny_client', connect=lambda **_: 'OFFLINE')
    findings = []
    def ensure(*args):
        findings.append(args[-1])
        return {'status': 'VERIFIED_EXACT_READBACK'}
    module(monkeypatch, 'ops.glp.seo_weekly_cogny', ensure_weekly_finding=ensure)
    monkeypatch.setattr(stage, 'load', lambda *_: types.SimpleNamespace(verify_pins=lambda: None))
    day = '2026-10-06'
    target = checkpoint(tmp_path, day, status, finding_status)
    originals = {p: p.read_bytes() for p in target.rglob('*') if p.is_file()}
    result = stage.run(dt.date.fromisoformat(day), root=tmp_path, registered=True)
    assert len(findings) == int(recover)
    for path, raw in originals.items():
        if path.name != 'receipt.json':
            assert path.read_bytes() == raw
    if recover:
        assert result['resumed_checkpoint'] is True
        assert result['status'] == 'PARTIAL'  # Unknown modules stay unknown, not PASS.
        assert result['incremental_metered_cost_usd'] == 0
        assert Path(result['resumes_prior_receipt']['path']).read_bytes() == originals[target / 'receipt.json']
        assert stage.run(dt.date.fromisoformat(day), root=tmp_path, registered=True)['idempotent_replay'] is True
        assert len(findings) == 1
    else:
        assert result['idempotent_replay'] is True
        assert (target / 'receipt.json').read_bytes() == originals[target / 'receipt.json']


def test_proposed_copy_does_not_import_promotional_display_name():
    common = load('actual_quickwin_common', 'ops/glp/seo_quickwin_common.py')
    restrained = {'title': 'SkinnyRx Review: Pricing & Terms | Clinic Scout',
                  'h1': 'SkinnyRx review: pricing and terms',
                  'meta': 'Considering SkinnyRx? Review reported pricing, program terms and documented evidence gaps.'}
    copy = common.proposed_copy('/reviews/skinnyrx/', restrained, 'SkinnyRx - #1 GLP Weight Loss Provider')
    assert copy == restrained
    assert common.proposed_copy('/reviews/skinnyrx/', {'h1': 'SkinnyRx #1 review'}, 'SkinnyRx #1') is None
    assert 'MadeMed' in common.proposed_copy('/reviews/mademed/', {'h1': 'MadeMed review'}, 'MadeMed')['title']


@pytest.mark.parametrize('claim', ['SkinnyRx - #1 GLP Weight Loss Provider', 'SkinnyRx # 1',
                                 'SkinnyRx No. 1', 'SkinnyRx number one', 'SkinnyRx best provider'])
def test_emitter_holds_unsupported_promotional_copy(tmp_path, monkeypatch, claim):
    dependencies(monkeypatch)
    emitter = load('actual_nonarticle_emitter', 'ops/glp/seo_nonarticle_emitter.py')
    clean = {'title': 'SkinnyRx Review: Pricing & Terms | Clinic Scout',
             'h1': 'SkinnyRx review: pricing and terms', 'meta': 'Review published SkinnyRx program terms.'}
    path = tmp_path / 'data/page-seo-copy.json'
    save(path, {'schema': 'clinic-scout-page-seo-v1', 'routes': {'/reviews/skinnyrx/': clean}})
    raw = path.read_bytes()
    for field in ('title', 'h1', 'meta'):
        harmful = {**clean, field: claim}
        assert emitter.valid_copy(harmful) is False
        bundle = emitter.emit_bundle({'rows': [{'route': '/reviews/skinnyrx/', 'exact_new_copy': harmful,
                  'state': 'STAGED_SOURCE_OWNER_REVIEW_REQUIRED', 'evidence_module': 'OFFLINE'}]}, path,
                  week_start='2026-10-05', evidence_bindings={}, generation={'release_root': str(tmp_path)})
        assert bundle['operation_count'] == 0
        assert bundle['dispositions'][0]['state'] == 'HELD_UNSUPPORTED_COPY'
        assert path.read_bytes() == raw
    assert emitter.valid_copy(clean) is True


@pytest.mark.parametrize('defect, error', [
    ('old_sunday', 'weekly_checkpoint_workweek_collision_reconcile'),
    ('tampered_strategy', 'weekly_stage_receipt_drift'),
    ('missing_checkpoint', 'weekly_stage_receipt_drift'),
])
def test_recovery_refuses_unsafe_checkpoint_before_effects(tmp_path, monkeypatch, defect, error):
    dependencies(monkeypatch)
    stage = load('refusal_weekly_source_stage', 'scripts/glp_weekly_seo_source_stage.py')
    continuation = load('refusal_workweek_contract', 'ops/glp/seo_weekly_continuation.py')
    module(monkeypatch, 'ops.glp.seo_weekly_continuation', workweek_start=continuation.workweek_start,
           run_week=lambda *_a, **_k: pytest.fail('no analysis replay'))
    module(monkeypatch, 'ops.glp.seo_nonarticle_stage',
           admit=lambda *_a, **_k: pytest.fail('no stale intake admission'), save=save)
    monkeypatch.setattr(stage, 'load', lambda *_: types.SimpleNamespace(verify_pins=lambda: None))
    day = '2026-10-04' if defect == 'old_sunday' else '2026-10-06'
    target = checkpoint(tmp_path, day, 'PARTIAL', 'BLOCKED_FINDING_RECONCILE_NO_RECOLLECTION')
    if defect == 'old_sunday':
        # Authentic old-calendar fixture: coherent hashes but wrong planning week.
        strategy = target / 'analysis/STRATEGY.json'
        save(strategy, {'week_start': '2026-09-28', 'markdown': 'OFFLINE_OLD_SUNDAY'})
        week_path = target / 'analysis/WEEK-RECEIPT.json'
        week = json.loads(week_path.read_text())
        week['week_start'] = '2026-09-28'
        for row in week['files']:
            row['sha256'] = sha(Path(row['path']))
        save(week_path, week)
        receipt = json.loads((target / 'receipt.json').read_text())
        for row in receipt['files']:
            row['sha256'] = sha(Path(row['path']))
        save(target / 'receipt.json', receipt)
    elif defect == 'tampered_strategy':
        (target / 'analysis/STRATEGY.json').write_text('{}')
    else:
        (target / 'analysis/WEEK-RECEIPT.json').unlink()
    originals = {p: p.read_bytes() for p in target.rglob('*') if p.is_file()}
    with pytest.raises((ValueError, FileNotFoundError), match=error if defect != 'missing_checkpoint' else 'WEEK-RECEIPT'):
        stage.run(dt.date.fromisoformat(day), root=tmp_path, registered=True)
    assert originals == {p: p.read_bytes() for p in target.rglob('*') if p.is_file()}


def test_repaired_source_pin_closure_and_actual_guard(tmp_path, monkeypatch):
    analysis = load('actual_weekly_pin_guard', 'scripts/glp_weekly_seo_analysis.py')
    raw = (ROOT / 'ops/glp/seo-weekly-installed-pins.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == analysis.PIN_SHA256
    pins = json.loads(raw)
    paths = ('ops/glp/seo_weekly_continuation.py', 'ops/glp/seo_quickwin_common.py',
             'ops/glp/seo_nonarticle_emitter.py', 'scripts/glp_weekly_seo_source_stage.py')
    scoped = {}
    for rel in paths:
        expected = pins['paths']['/home/ubuntu/business/' + rel]
        assert sha(ROOT / rel) == expected
        destination = tmp_path / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((ROOT / rel).read_bytes())
        scoped[str(destination)] = expected
    # Explicit offline four-member fixture; not an installed full-map readback.
    manifest = tmp_path / 'pins.json'
    save(manifest, {'paths': scoped})
    monkeypatch.setattr(analysis, 'PIN_PATH', manifest)
    monkeypatch.setattr(analysis, 'PIN_SHA256', sha(manifest))
    assert analysis.verify_pins()['paths'] == scoped
    for rel in paths:
        destination = tmp_path / rel
        original = destination.read_bytes()
        destination.write_bytes(original + b'\n')
        with pytest.raises(ValueError, match='weekly_installed_source_drift:'):
            analysis.verify_pins()
        destination.write_bytes(original)


def historical_stage_fixture(tmp_path, monkeypatch):
    dependencies(monkeypatch)
    common = load('stage_copy_policy', 'ops/glp/seo_quickwin_common.py')
    monkeypatch.setitem(sys.modules, 'ops.glp.seo_quickwin_common', common)
    emitter = load('stage_emitter', 'ops/glp/seo_nonarticle_emitter.py')
    monkeypatch.setitem(sys.modules, 'ops.glp.seo_nonarticle_emitter', emitter)
    module(monkeypatch, 'ops.glp.seo_weekly_modules', read_bound=lambda x: x, validate_snapshot=lambda *_: None)
    stage = load('stage_custody', 'ops/glp/seo_nonarticle_stage.py')
    release = tmp_path / 'release'
    source = release / emitter.SOURCE
    save(source, {'schema': 'clinic-scout-page-seo-v1', 'routes': {}})
    pointer_path = tmp_path / 'pointer.json'
    save(pointer_path, {})
    generation = {'release_root': str(release), 'deployment_id': 'OFFLINE', 'pointer_sha256': sha(pointer_path)}
    row = {'route': '/reviews/example/', 'state': 'STAGED_SOURCE_OWNER_REVIEW_REQUIRED',
           'exact_new_copy': {'title': 'Example Review', 'h1': 'Example review', 'meta': 'Review documented pricing and evidence gaps.'},
           'evidence_module': 'money_pages'}
    bundle = emitter.emit_bundle({'rows': [row]}, source, week_start='2026-10-05', evidence_bindings={}, generation=generation)
    current_overlay = emitter.apply_operations(source.read_bytes(), bundle)
    monkeypatch.setattr(stage, 'validate', lambda b, p: (emitter.apply_operations(source.read_bytes(), b), pointer_path.read_bytes()))
    # Historical producer accepted this promotional text; it is now forbidden.
    old = json.loads(json.dumps(bundle))
    old['week_start'] = '2026-09-28'
    old['operations'][0]['value']['title'] = 'Example: #1 Best Provider'
    op = old['operations'][0]
    op['patch_id'] = emitter.digest({'week_start': old['week_start'], 'source_sha256': old['source']['sha256'],
                                     'operation': {k: v for k, v in op.items() if k != 'patch_id'}})
    old['intake_id'] = emitter.digest({k: v for k, v in old.items() if k != 'intake_id'})
    root = tmp_path / 'admissions'
    target = root / old['intake_id']
    save(target / 'INTAKE.json', old)
    save(target / 'source-overlay' / emitter.SOURCE, {'schema': 'clinic-scout-page-seo-v1', 'routes': {op['route']: op['value']}})
    receipt = {'schema': 'glp-seo-nonarticle-admission-v1', 'status': 'ADMITTED_SOURCE_REVIEW_REQUIRED',
               'intake_id': old['intake_id'], 'generation': generation, 'operation_count': 1,
               'patch_ids': [op['patch_id']], 'publication_authorized': False,
               'source_overlay_only': True, 'article_queue_writes': 0, 'current_source_writes': 0,
               'files': [{'path': str(p), 'sha256': sha(p)} for p in sorted(target.rglob('*')) if p.is_file()]}
    receipt_path = target / 'ADMISSION.json'
    save(receipt_path, receipt)
    intake = tmp_path / 'current-intake.json'
    save(intake, bundle)
    return stage, emitter, root, pointer_path, intake, receipt_path, old, current_overlay


def test_disjoint_historical_copy_policy_does_not_block_fresh_admission(tmp_path, monkeypatch):
    stage, emitter, root, pointer_path, intake, prior, old, expected = historical_stage_fixture(tmp_path, monkeypatch)
    originals = {p: p.read_bytes() for p in prior.parent.rglob('*') if p.is_file()}
    # Never re-admit or promote the unsupported historical operation.
    with pytest.raises(ValueError, match='invalid_field_patch'):
        emitter.apply_operations((tmp_path / 'release' / emitter.SOURCE).read_bytes(), old)
    result = stage.admit(intake, root=root, pointer=pointer_path)
    assert result['status'] == 'ADMITTED_SOURCE_REVIEW_REQUIRED'
    assert result['publication_authorized'] is False
    assert result['generation_reanchors'] == []
    overlay = Path(result['receipt']).parent / 'source-overlay' / emitter.SOURCE
    assert json.loads(overlay.read_text()) == expected
    assert originals == {p: p.read_bytes() for p in prior.parent.rglob('*') if p.is_file()}


@pytest.mark.parametrize('damage', ['intake', 'overlay', 'index', 'source'])
def test_disjoint_custody_still_refuses_tamper(tmp_path, monkeypatch, damage):
    stage, emitter, root, pointer_path, intake, prior, old, _ = historical_stage_fixture(tmp_path, monkeypatch)
    target = prior.parent
    if damage == 'index':
        receipt = json.loads(prior.read_text())
        receipt['patch_ids'] = []
        prior.write_text(json.dumps(receipt))
    else:
        path = {'intake': target / 'INTAKE.json', 'overlay': target / 'source-overlay' / emitter.SOURCE,
                'source': tmp_path / 'release' / emitter.SOURCE}[damage]
        path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(ValueError):
        stage.admit(intake, root=root, pointer=pointer_path)
    assert len(list(root.glob('*/ADMISSION.json'))) == 1


def test_overlapping_historical_unsupported_copy_remains_refused(tmp_path, monkeypatch):
    stage, _, _, _, _, prior, old, _ = historical_stage_fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='invalid_field_patch'):
        stage.read_admission(prior, current_patch_ids={old['operations'][0]['patch_id']})
    with pytest.raises(ValueError, match='invalid_field_patch'):
        stage.read_admission(prior)
