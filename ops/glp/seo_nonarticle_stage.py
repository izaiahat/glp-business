"""Single-writer review-stage admission. Never an article or publication queue."""
import datetime as dt
import fcntl
import json
import os
from pathlib import Path

from ops.glp.seo_nonarticle_emitter import SOURCE, apply_operations, digest, emit_bundle
from ops.glp.seo_quickwin_common import permission_held
from ops.glp.seo_weekly_modules import read_bound, validate_snapshot

B = Path('/home/ubuntu/business')
POINTER = B / 'reports/glp-clinic-scout/seo-machine/current-content-base.json'
ROOT = B / 'reports/glp-clinic-scout/seo-machine/nonarticle-source-stage'


def save(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x') as stream:
        json.dump(doc, stream, indent=2, ensure_ascii=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def validate(bundle, pointer=POINTER):
    pointer = Path(pointer)
    raw_pointer = pointer.read_bytes()
    base = json.loads(raw_pointer)
    generation = bundle['generation']
    release = Path(base['release_root']).resolve()
    if (digest(raw_pointer) != generation['pointer_sha256']
            or str(release) != generation['release_root']
            or base['deployment_id'] != generation['deployment_id']):
        raise ValueError('current_generation_drift')
    source = release / SOURCE
    if (bundle['source']['path'] != str(source)
            or bundle['source']['relative_path'] != SOURCE
            or bundle['source']['owner'] != 'src/page-seo-copy.mjs:pageSeoCopy'):
        raise ValueError('source_owner_mismatch')
    manifest = read_bound(bundle['evidence_bindings']['manifest'])
    modules = read_bound(bundle['evidence_bindings']['modules'])
    # Reproduce producer intent from its bound module evidence, not caller prose.
    expected = emit_bundle(modules['patch_intakes'], source,
                           week_start=bundle['week_start'],
                           evidence_bindings=bundle['evidence_bindings'], generation=generation)
    if expected != bundle:
        raise ValueError('module_intent_mismatch')
    if manifest['current_base_sha256'] != digest(raw_pointer):
        raise ValueError('manifest_generation_drift')
    for path, binding in manifest['source_bindings'].items():
        if digest(Path(path).read_bytes()) != binding['sha256']:
            raise ValueError('source_binding_drift:' + path)
    validate_snapshot(read_bound(manifest['inputs']['gsc-28']), dt.datetime.now(dt.timezone.utc))
    registry = read_bound(manifest['inputs']['registry'])
    if digest((release / 'data/program-registry.json').read_bytes()) != manifest['inputs']['registry']['sha256']:
        raise ValueError('registry_generation_drift')
    pages = read_bound(manifest['inputs']['pages'])
    for op in bundle['operations']:
        route = op['route']
        matching = [row for row in registry['programs'] if row.get('review_path') == route]
        if matching and (len(matching) != 1 or any(permission_held(row) for row in matching)):
            raise ValueError('held_or_ambiguous_provider')
        if route.startswith('/reviews/') and route != '/reviews/' and not matching:
            raise ValueError('unknown_provider_identity')
        page = pages.get(route, {})
        capture = page.get('capture', {})
        url = 'https://glpclinicscout.com' + route
        if (page.get('canonical') != [url] or capture.get('final_url') != url
                or capture.get('status') != 200 or capture.get('path') != route):
            raise ValueError('noncanonical_route')
        if digest(Path(capture['file']).read_bytes()) != capture['sha256']:
            raise ValueError('page_evidence_drift')
        # Require the exact served route in the current source generation too.
        if not (release / 'public' / route.lstrip('/') / 'index.html').is_file():
            raise ValueError('route_not_in_current_generation')
    return apply_operations(source.read_bytes(), bundle), raw_pointer


def read_admission(receipt_path):
    """Authenticate immutable review-stage custody, not historical permission."""
    receipt_path = Path(receipt_path)
    raw = receipt_path.read_bytes()
    receipt = json.loads(raw)
    intake_path = receipt_path.parent / 'INTAKE.json'
    overlay_path = receipt_path.parent / 'source-overlay' / SOURCE
    files = receipt['files']
    if (len(files) != 2 or {item['path'] for item in files}
            != {str(intake_path), str(overlay_path)}):
        raise ValueError('admitted_stage_file_set_drift')
    for item in files:
        if digest(Path(item['path']).read_bytes()) != item['sha256']:
            raise ValueError('admitted_stage_drift')
    intake = json.loads(intake_path.read_bytes())
    ids = sorted(op['patch_id'] for op in intake['operations'])
    if (receipt.get('schema') != 'glp-seo-nonarticle-admission-v1'
            or receipt.get('status') != 'ADMITTED_SOURCE_REVIEW_REQUIRED'
            or receipt.get('intake_id') != intake['intake_id']
            or receipt_path.parent.name != intake['intake_id']
            or receipt.get('generation') != intake['generation']
            or receipt.get('operation_count') != intake['operation_count']
            or receipt.get('patch_ids', ids) != ids
            or receipt.get('publication_authorized') is not False
            or receipt.get('source_overlay_only') is not True
            or receipt.get('article_queue_writes') != 0
            or receipt.get('current_source_writes') != 0):
        raise ValueError('admission_identity_drift')
    # Historical freshness/permission is NOT replayed. Verify its own CAS and
    # overlay, then require fresh current-generation validation in admit().
    source = Path(intake['generation']['release_root']) / SOURCE
    if intake['source']['path'] != str(source):
        raise ValueError('admission_source_identity_drift')
    expected = apply_operations(source.read_bytes(), intake)
    if json.loads(overlay_path.read_bytes()) != expected:
        raise ValueError('admitted_overlay_intent_drift')
    return receipt, intake, {'path': str(receipt_path), 'sha256': digest(raw)}


def generation_key(generation):
    return tuple(generation[key] for key in ('release_root', 'deployment_id', 'pointer_sha256'))


def admit(intake_path, *, root=ROOT, pointer=POINTER):
    bundle = json.loads(Path(intake_path).read_text())
    # Pure validator also validates all identity/CAS fields before they form a path.
    after, raw_pointer = validate(bundle, pointer)
    root = Path(root).resolve()
    release = Path(bundle['generation']['release_root']).resolve()
    if root.is_relative_to(release):
        raise ValueError('stage_must_not_modify_release')
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.writer.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = root / bundle['intake_id']
        receipt_path = target / 'ADMISSION.json'
        patch_ids = {op['patch_id'] for op in bundle['operations']}
        reanchors = []
        replay = None
        current_ops = {op['patch_id']: op for op in bundle['operations']}
        for admitted_path in sorted(root.glob('*/ADMISSION.json')):
            if admitted_path == receipt_path:
                continue
            prior, prior_intake, prior_binding = read_admission(admitted_path)
            prior_ops = {op['patch_id']: op for op in prior_intake['operations']}
            overlap = patch_ids & prior_ops.keys()
            if not overlap:
                continue
            if any(current_ops[key] != prior_ops[key] for key in overlap):
                raise ValueError('overlapping_patch_intent_drift')
            if generation_key(prior_intake['generation']) != generation_key(bundle['generation']):
                # Owner-controlled reconciliation: re-admit the entire freshly
                # validated bundle, never return stale-generation authority.
                reanchors.append({
                    'schema': 'glp-seo-nonarticle-generation-reanchor-v1',
                    'owner': 'ops/glp/seo_nonarticle_stage.py:admit',
                    'scope': 'OVERLAPPING_PATCH_IDS_ONLY',
                    'prior_admission': prior_binding,
                    'prior_intake_id': prior_intake['intake_id'],
                    'prior_generation': prior_intake['generation'],
                    'current_generation': bundle['generation'],
                    'patch_ids': sorted(overlap),
                    'disposition': 'SUPERSEDED_FOR_CURRENT_GENERATION_REVIEW_ONLY',
                    'publication_authorized': False,
                })
                continue
            if overlap != patch_ids or overlap != prior_ops.keys():
                raise ValueError('partial_patch_overlap_requires_reconciliation')
            replay = {**prior, 'receipt': str(admitted_path),
                      'idempotent_replay': True, 'duplicate_patch_ids': sorted(overlap)}
        if target.exists():
            if not receipt_path.is_file():
                raise ValueError('ambiguous_stage_requires_reconciliation')
            receipt, admitted, _ = read_admission(receipt_path)
            if admitted != bundle or receipt.get('generation_reanchors', []) != reanchors:
                raise ValueError('admission_identity_drift')
            validate(bundle, pointer)
            return {**receipt, 'receipt': str(receipt_path), 'idempotent_replay': True}
        if replay is not None:
            if replay.get('generation_reanchors', []) != reanchors:
                raise ValueError('admission_reanchor_custody_drift')
            validate(bundle, pointer)
            return replay
        target.mkdir()
        save(target / 'INTAKE.json', bundle)
        save(target / 'source-overlay' / SOURCE, after)
        if Path(pointer).read_bytes() != raw_pointer:
            raise ValueError('generation_changed_during_admission')
        # Final validation detects a current hold/source change during the write.
        validate(bundle, pointer)
        for reanchor in reanchors:
            _, _, current_binding = read_admission(reanchor['prior_admission']['path'])
            if current_binding != reanchor['prior_admission']:
                raise ValueError('admission_reanchor_custody_drift')
        receipt = {
            'schema': 'glp-seo-nonarticle-admission-v1',
            'status': 'ADMITTED_SOURCE_REVIEW_REQUIRED',
            'intake_id': bundle['intake_id'], 'operation_count': bundle['operation_count'],
            'patch_ids': sorted(patch_ids), 'generation_reanchors': reanchors,
            'generation': bundle['generation'], 'at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'publication_authorized': False, 'article_queue_writes': 0,
            'current_source_writes': 0, 'source_overlay_only': True,
            'required_artifact': 'Design train-2 rebase/current-holds check and full candidate build, then independent acceptance and governed release; overlay is not a deployable candidate.',
            'files': [{'path': str(path), 'sha256': digest(path.read_bytes())}
                      for path in sorted(target.rglob('*')) if path.is_file()],
        }
        save(receipt_path, receipt)
        return {**receipt, 'receipt': str(receipt_path), 'idempotent_replay': False}
