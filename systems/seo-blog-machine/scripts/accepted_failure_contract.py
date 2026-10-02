"""Name-bound publication allowance; never a product-quality waiver."""
from __future__ import annotations
import hashlib
import re
import json
import os
import subprocess
from pathlib import Path

RUNTIME_ROOT = Path('/home/ubuntu/business/reports/glp-clinic-scout/seo-machine/release-runtime')


ACCEPTANCE_ROOT = Path('/home/ubuntu/business/reports/ops/orchestration-restore-20260907/PLAN')


def discover_acceptance(pointer):
    matches = []
    for acceptance_path in ACCEPTANCE_ROOT.glob('RELEASE-*/ACCEPTANCE.json'):
        review_path = acceptance_path.parent / 'REVIEW-MANIFEST.json'
        try:
            acceptance = json.loads(acceptance_path.read_text())
            review = json.loads(review_path.read_text())
            if (acceptance.get('status') == 'ACCEPT_VALIDATED'
                and Path(review['tuple']['sealed_root']).name == Path(pointer['release_root']).name
                and acceptance['evidence']['subject_id'] == review['tuple']['subject_id']):
                matches.append((acceptance_path, review_path))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if len(matches) != 1:
        raise RuntimeError('accepted_failures_exact_acceptance_missing_or_ambiguous')
    return matches[0]


def descriptor(path):
    path = Path(path).resolve(strict=True)
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def read_bound(spec):
    if not isinstance(spec, dict) or set(spec) != {'path', 'sha256'}:
        raise RuntimeError('accepted_failures_descriptor_invalid')
    if descriptor(spec['path']) != spec:
        raise RuntimeError('accepted_failures_evidence_hash_drift')
    return json.loads(Path(spec['path']).read_text())


def build_binding(pointer, acceptance_path, review_path):
    acceptance_spec, review_spec = descriptor(acceptance_path), descriptor(review_path)
    review = read_bound(review_spec)
    rows = [row for row in review['tuple']['inputs'] if Path(row['source_path']).name == 'SAFE-SUITE-DISCLOSURES.json']
    if len(rows) != 1:
        raise RuntimeError('accepted_failures_disclosure_not_unique')
    row = rows[0]
    disclosure = {'path': row['snapshot_path'], 'sha256': row['sha256']}
    data = read_bound(disclosure)
    binding = {'schema': 'glp-accepted-failures-binding-v1',
               'release_root': pointer['release_root'], 'release_tree_sha256': pointer['release_tree_sha256'],
               'release_manifest': pointer['manifest'],
               'acceptance': acceptance_spec, 'review': review_spec, 'disclosure': disclosure,
               'accepted_names': data['same_named_failures'], 'minimum_tests': data['counts']['tests'],
               'publication_allowance_only': True, 'raw_disclosure_status': data['status']}
    validate_binding(binding, pointer)
    return binding


def validate_binding(binding, pointer, *, planned_manifest=None):
    try:
        if binding['schema'] != 'glp-accepted-failures-binding-v1' or binding['publication_allowance_only'] is not True:
            raise RuntimeError('accepted_failures_schema_invalid')
        if 'parent_binding' in binding and 'price_index_parent_binding' in binding:
            raise RuntimeError('accepted_failures_mixed_lineage')
        for family in ({'parent_binding','descendant_manifest','descendant_final'},
                       {'price_index_parent_binding','price_index_classification','price_index_suite_witness'}):
            present=family.intersection(binding)
            if present and present!=family:
                raise RuntimeError('accepted_failures_orphan_lineage')
        for key in ('release_root', 'release_tree_sha256'):
            if binding[key] != pointer[key]:
                raise RuntimeError('accepted_failures_base_drift:' + key)
        if 'price_index_parent_binding' in binding:
            return validate_price_index_binding(binding, pointer, planned_manifest=planned_manifest)
        if 'parent_binding' in binding:
            parent = read_bound(binding['parent_binding'])
            validate_binding(parent, parent)
            if binding['parent_binding'] != descriptor(runtime_binding_path(parent)):
                raise RuntimeError('accepted_failures_parent_runtime_drift')
            manifest = read_bound(binding['descendant_manifest'])
            final = read_bound(binding['descendant_final'])
            for key in ('release_root', 'release_tree_sha256'):
                if manifest[key] != binding[key]:
                    raise RuntimeError('accepted_failures_descendant_identity_drift')
            # The real final-publication-v1 producer binds its root through the
            # manifest descriptor, not an invented top-level release_root.
            if (final.get('schema') != 'glp-opus-article-final-publication-v1'
                or final.get('manifest') != binding['descendant_manifest']
                or final.get('release_tree_sha256') != binding['release_tree_sha256']
                or final.get('public_tree_sha256') != manifest['public_tree_sha256']):
                raise RuntimeError('accepted_failures_descendant_identity_drift')
            if manifest['rollback_release_root'] != parent['release_root'] or final['status'] != 'PASS_LIVE_GSC_READBACK_PASS':
                raise RuntimeError('accepted_failures_descendant_predecessor_invalid')
            classification = verify_classification(Path(binding['descendant_final']['path']).parent)
            if final['accepted_failures'] != classification or classification['binding'] != binding['parent_binding']:
                raise RuntimeError('accepted_failures_descendant_classification_invalid')
            for key in ('acceptance', 'review', 'disclosure', 'accepted_names', 'minimum_tests', 'raw_disclosure_status'):
                if binding[key] != parent[key]:
                    raise RuntimeError('accepted_failures_descendant_allowance_drift')
            return binding
        if binding.get('acceptance_route') == 'designated-astra-current-v1':
            return validate_current_binding(binding, pointer)
        if 'acceptance_route' in binding:
            raise RuntimeError('accepted_failures_unknown_acceptance_route')
        acceptance, review, data = (read_bound(binding[key]) for key in ('acceptance', 'review', 'disclosure'))
        evidence, subject = acceptance['evidence'], review['tuple']
        digest = hashlib.sha256(json.dumps(subject, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        if (acceptance['status'] != 'ACCEPT_VALIDATED' or evidence['authority_class'] != 'bridge'
            or evidence['authority_origin'] != 'guard_owned_authenticated_discord_readback'
            or evidence['acceptance_author_id'] == evidence['request_author_id']
            or acceptance['acceptance_message_id'] != evidence['acceptance_message_id']
            or digest != review['tuple_sha256'] or digest != evidence['tuple_sha256']
            or evidence['subject_id'] != subject['subject_id']):
            raise RuntimeError('accepted_failures_acceptance_invalid')
        if Path(binding['release_root']).name != Path(subject['sealed_root']).name:
            raise RuntimeError('accepted_failures_seal_mismatch')
        manifest = read_bound(binding['release_manifest'])
        if any(manifest[key] != binding[key] for key in ('release_root', 'release_tree_sha256')):
            raise RuntimeError('accepted_failures_manifest_identity_drift')
        sealed_rows = [row for row in subject['inputs'] if row['sha256'] == subject['sealed_manifest_sha256']]
        if len(sealed_rows) != 1:
            raise RuntimeError('accepted_failures_sealed_manifest_ambiguous')
        sealed = read_bound({'path': sealed_rows[0]['snapshot_path'], 'sha256': sealed_rows[0]['sha256']})
        accepted_files = {row['path']: (row['sha256'], row['bytes']) for row in sealed}
        # The maintained current_pointer_intent producer emits size, unlike the
        # article manifest's bytes. Admit only its exact manifest/row shape;
        # independent acceptance and exact sealed-file comparison still apply.
        current_shape = {'schema', 'release_root', 'release_tree_sha256',
                         'release_rows', 'site_head', 'deploy_directory'}
        current_manifest = (set(manifest) == current_shape
                            and manifest['schema'] == 'glp-opus-release-deployment-manifest-v1')
        length_key = 'size' if current_manifest else 'bytes'
        if current_manifest and any(
                set(row) != {'path', 'sha256', 'size'} or type(row['size']) is not int
                or row['size'] < 0 for row in manifest['release_rows']):
            raise RuntimeError('accepted_failures_current_row_invalid')
        release_files = {row['path']: (row['sha256'], row[length_key]) for row in manifest['release_rows']}
        extras = release_files.keys() - accepted_files.keys()
        if ROOT_BINDING_NAME in extras:
            # K6d retention class is one exact, non-public, validated predecessor
            # receipt. It cannot excuse any modification to accepted files.
            root = Path(binding['release_root'])
            path = root / ROOT_BINDING_NAME
            value = json.loads(path.read_text())
            parent = read_bound(value['binding'])
            validate_root_binding(root, parent)
            if release_files[ROOT_BINDING_NAME] != (descriptor(path)['sha256'], path.stat().st_size):
                raise RuntimeError('accepted_failures_retained_receipt_hash_drift')
        if (len(accepted_files) != len(sealed) or len(release_files) != len(manifest['release_rows'])
            or any(release_files.get(path) != value for path, value in accepted_files.items())
            or any(not path.startswith('node_modules/') and path != ROOT_BINDING_NAME for path in extras)):
            raise RuntimeError('accepted_failures_release_not_accepted_seal')
        rows = [row for row in subject['inputs'] if row['snapshot_path'] == binding['disclosure']['path'] and row['sha256'] == binding['disclosure']['sha256']]
        if len(rows) != 1 or descriptor(rows[0]['source_path'])['sha256'] != binding['disclosure']['sha256']:
            raise RuntimeError('accepted_failures_disclosure_not_reviewed')
        names = data['same_named_failures']
        if (not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names)
            or len(set(names)) != len(names) or len(names) != data['retained_failure_count']
            or len(names) != data['counts']['fail'] or names != binding['accepted_names']
            or binding['minimum_tests'] != data['counts']['tests'] or type(binding['minimum_tests']) is not int
            or binding['minimum_tests'] <= 0 or data['new_failure_count'] != 0
            or data['newly_reported_failures'] or data['counts']['cancelled'] != 0
            or binding['raw_disclosure_status'] != data['status']):
            raise RuntimeError('accepted_failures_disclosure_invalid')
        return binding
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise RuntimeError('accepted_failures_binding_invalid:' + str(exc)) from exc


# Authentication anchor for the actual combined release, not a generic model-name
# bypass of bridge authority. Changing this anchor is a reviewed source successor.
CURRENT_ASTRA_REVIEW = {
    'path': '/home/ubuntu/business/reports/ops/glp42-final-combined-release-review-20260927/INDEPENDENT-REVIEW.json',
    'sha256': 'e316c8253f3d7fbab38c4e39fc007e0a646a3c11ea8b13b4a7d719dec47511ab'}
CURRENT_ASTRA_AUTHORITIES = {
    '/home/ubuntu/business/ops/linear/approvals/OPERATOR-DECISION-20260926-release-review-astra.md':
        '7d0d49def4254a19e088d4d6586c9963325311da17cfbd6054dd175876240fb0'}


def current_release_evidence(pointer):
    """Authenticate retained Astra approval and its exact prepared release seal."""
    for path, digest in CURRENT_ASTRA_AUTHORITIES.items():
        if descriptor(path) != {'path': path, 'sha256': digest}:
            raise RuntimeError('current_acceptance_operator_authority_drift')
    review = read_bound(CURRENT_ASTRA_REVIEW)
    reviewer = review['reviewer']
    if (review.get('schema') != 'glp42-designated-astra-release-review-v1'
        or review.get('verdict') != 'APPROVE' or review.get('release_accepted') is not True
        or review.get('full_combined_deployment_accepted') is not True
        or review.get('blocking_findings') != []
        or reviewer.get('model') != 'gpt-6-astra-900k'
        or reviewer.get('provider') != 'openai-codex'
        or 'not an author' not in reviewer.get('role', '')):
        raise RuntimeError('current_acceptance_designated_review_invalid')
    subject = review['subject']
    prepared_spec = {'path': str(Path(subject['prepared_dir']) / 'PREPARED.json'),
                     'sha256': subject['prepared_manifest_sha256']}
    prepared = read_bound(prepared_spec)
    required = {'repository': prepared['site_repository'], 'head': prepared['site_head'],
                'git_tree': prepared['source']['git_tree'],
                'public_tree_sha256': prepared['public_tree_sha256'],
                'package_manifest_sha256': prepared['package_manifest_sha256'],
                'deploy_directory_sha256': prepared['deploy_directory']['tree_sha256'],
                'compiled_worker_sha256': prepared['compiled']['worker_sha256'],
                'project': prepared['project']}
    if (any(subject.get(k) != v for k, v in required.items())
        or prepared['schema'] != 'glp43-current-prepared-v1'
        or prepared['site_repository'] != 'izaiahat/glp-site-candidate'
        or prepared['project'] != 'glp-clinic-scout'
        or prepared['prepared_dir'] != subject['prepared_dir']
        or review['reused_technical_acceptance']['gate_sha256'] != prepared['build_receipt']['sha256']):
        raise RuntimeError('current_acceptance_review_subject_drift')
    manifest = read_bound(pointer['manifest'])
    if (set(manifest) != {'schema', 'release_root', 'release_tree_sha256', 'release_rows', 'site_head', 'deploy_directory'}
        or manifest['schema'] != 'glp-opus-release-deployment-manifest-v1'
        or any(manifest[k] != pointer[k] for k in ('release_root', 'release_tree_sha256'))
        or manifest['release_root'] != str(Path(subject['prepared_dir']) / 'release')
        or manifest['site_head'] != subject['head']
        or manifest['deploy_directory'] != prepared['deploy_directory']):
        raise RuntimeError('current_acceptance_manifest_identity_drift')
    rows = manifest['release_rows']
    if not isinstance(rows, list) or not rows:
        raise RuntimeError('current_acceptance_release_rows_invalid')
    for row in rows:
        if (set(row) != {'path', 'sha256', 'size'} or type(row['size']) is not int or row['size'] < 0
            or not isinstance(row['path'], str) or not row['path'] or Path(row['path']).is_absolute()
            or any(part in ('', '.', '..') for part in row['path'].split('/'))
            or not re.fullmatch('[a-f0-9]{64}', row['sha256'])):
            raise RuntimeError('current_acceptance_release_rows_invalid')
    # Match the native current carrier's file_closure, not legacy row_closure.
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda r: r['path'].encode()):
        digest.update(f"{row['path']}\0{row['size']}\0{row['sha256']}\n".encode())
    if (len({r['path'] for r in rows}) != len(rows)
        or len(rows) != prepared['release']['file_count']
        or sum(r['size'] for r in rows) != prepared['release']['total_bytes']
        or digest.hexdigest() != pointer['release_tree_sha256']
        or pointer['release_tree_sha256'] != prepared['release']['tree_sha256']):
        raise RuntimeError('current_acceptance_release_not_reviewed_seal')
    return dict(CURRENT_ASTRA_REVIEW), prepared_spec


def prepare_current_binding(pointer, suite_stdout, suite_exit_code, *, minimum_tests,
                            preparer_id, suite_result_path):
    """Produce an honest review input, never infer full-suite acceptance from GLP42."""
    if (type(minimum_tests) is not int or minimum_tests <= 0
        or type(suite_exit_code) is not int or not isinstance(preparer_id, str) or not preparer_id.strip()):
        raise RuntimeError('current_acceptance_proposal_arguments_invalid')
    review, prepared = current_release_evidence(pointer)
    stdout_spec, result_spec = descriptor(suite_stdout), descriptor(suite_result_path)
    stdout = Path(stdout_spec['path']).read_text()
    classification = classify_suite(stdout, suite_exit_code, [], minimum_tests=minimum_tests)
    if classification['status'] == 'FAIL_SUITE_INTEGRITY':
        raise RuntimeError('current_acceptance_full_suite_integrity:' + json.dumps(classification['errors']))
    result = read_bound(result_spec)
    if (result.get('command') != 'npm test' or result.get('invocations') != 1
        or result.get('exit_code') != suite_exit_code or result.get('counts') != classification['counts']
        or result.get('custody', {}).get('source_sha256') != pointer['release_tree_sha256']
        or result.get('custody', {}).get('source_unchanged') is not True):
        raise RuntimeError('current_acceptance_suite_source_custody_drift')
    # Re-read the log pin after parsing; a proposal binds the bytes actually read.
    if descriptor(suite_stdout) != stdout_spec:
        raise RuntimeError('current_acceptance_suite_log_drift')
    return {'schema': 'glp-current-release-suite-proposal-v1',
            'status': 'REQUIRES_INDEPENDENT_SUITE_ACCEPTANCE',
            'base': {k: pointer[k] for k in ('release_root', 'release_tree_sha256', 'manifest')},
            'review': review, 'prepared': prepared, 'suite_stdout': stdout_spec,
            'suite_result': result_spec, 'suite_exit_code': suite_exit_code,
            'classification': classification, 'minimum_tests': classification['counts']['tests'],
            'preparer_id': preparer_id, 'publication_allowance_only': True}


def current_binding_from_evidence(proposal_spec, acceptance_spec):
    proposal, acceptance = read_bound(proposal_spec), read_bound(acceptance_spec)
    expected = prepare_current_binding(proposal['base'], proposal['suite_stdout']['path'],
        proposal['suite_exit_code'], minimum_tests=proposal['minimum_tests'],
        preparer_id=proposal['preparer_id'], suite_result_path=proposal['suite_result']['path'])
    if proposal != expected:
        raise RuntimeError('current_acceptance_proposal_drift')
    reviewer = acceptance.get('reviewer', {})
    if (acceptance.get('schema') != 'glp-current-release-suite-acceptance-v1'
        or acceptance.get('verdict') != 'APPROVE' or acceptance.get('blocking_findings') != []
        or acceptance.get('proposal') != proposal_spec
        or acceptance.get('publication_allowance_only') is not True
        or acceptance.get('accepted_names') != proposal['classification']['new_failures']
        or reviewer.get('model') != 'gpt-6-astra-900k' or reviewer.get('provider') != 'openai-codex'
        or reviewer.get('independent') is not True or not isinstance(reviewer.get('id'), str)
        or not reviewer['id'].strip() or reviewer['id'].strip() == proposal['preparer_id'].strip()
        or acceptance.get('authority') not in [dict(path=p, sha256=h) for p, h in CURRENT_ASTRA_AUTHORITIES.items()]):
        raise RuntimeError('current_acceptance_independent_suite_acceptance_required')
    return {'schema': 'glp-accepted-failures-binding-v1', 'publication_allowance_only': True,
            'acceptance_route': 'designated-astra-current-v1',
            'release_root': proposal['base']['release_root'],
            'release_tree_sha256': proposal['base']['release_tree_sha256'],
            'release_manifest': proposal['base']['manifest'], 'acceptance': acceptance_spec,
            'review': proposal['review'], 'disclosure': proposal_spec,
            'accepted_names': acceptance['accepted_names'], 'minimum_tests': proposal['minimum_tests'],
            'raw_disclosure_status': proposal['classification']['status']}


def build_current_binding(proposal_path, suite_acceptance_path, expected_suite_acceptance_sha256):
    """The expected review hash must come from the coordinator's accepted review,
    not be recomputed from an untrusted file. Same trust boundary as current_approval.
    """
    proposal_spec = descriptor(proposal_path)
    acceptance_spec = {'path': str(Path(suite_acceptance_path).resolve(strict=True)),
                       'sha256': expected_suite_acceptance_sha256}
    return current_binding_from_evidence(proposal_spec, acceptance_spec)


def validate_current_binding(binding, pointer):
    expected = current_binding_from_evidence(binding['disclosure'], binding['acceptance'])
    if (binding != expected or any(binding[k] != pointer[k] for k in ('release_root', 'release_tree_sha256'))
        or ('manifest' in pointer and binding['release_manifest'] != pointer['manifest'])):
        raise RuntimeError('current_acceptance_binding_drift')
    return binding


PRICE_INDEX_INHERITED_KEYS = ('acceptance', 'review', 'disclosure', 'accepted_names',
                            'minimum_tests', 'raw_disclosure_status')
PRICE_INDEX_PUBLIC = frozenset(('public/glp-1-price-index/index.html',
    'public/glp-1-price-index/data/latest.json', 'public/glp-1-price-index/data/latest.csv',
    'public/sitemap.xml', 'public/data/price-watch-issue.json'))
PRICE_INDEX_SOURCES = frozenset(('data/price-tracker.json', 'data/page-content-provenance.json',
    'scripts/update-price-index.mjs', 'scripts/build-staging.mjs', 'scripts/postbuild-aeo-surface.mjs',
    'APPROVED-53-COVERAGE-GATE.md', 'SHED-IDENTITY-RECONCILIATION-20260806.md',
    'calc.html', 'guide-mounjaro.html', 'guide-whatis.html', 'money-best.html', 'money-cost.html',
    'quiz.html', 'rev-found.html', 'rev-shedrx.html',
    'tests/lastmod-declared-routes.test.mjs', 'tests/mounjaro-coupon-recovery.test.mjs',
    'tests/product-ready.test.mjs', 'tests/served-output-freshness.test.mjs'))
PRICE_INDEX_SOURCES = PRICE_INDEX_SOURCES | frozenset((
    'src/inherited-disclosure-dates.mjs', 'data/d2w-s9-d2p-provenance-evidence.json',
    'tests/k9-suite-repair.test.mjs', 'tests/fixtures/k9/postbuild-aeo-surface.preimage.mjs'))
# Exact reviewed repair transitions, not perpetual filename permission.
PRICE_INDEX_SOURCE_TRANSITIONS = {
    'scripts/update-price-index.mjs': ('09339afce5df2e92d8b0a6bd682bf3fc718a3202e2533f38cf49065ff3b1cded', '07ba5c11c248c4c587ab163ac27efe6c86f2c4a4dc950a4804b2a3f9e7bad335'),
    'scripts/postbuild-aeo-surface.mjs': ('9de33b85829e44e88af1352abf8543c02495a61bbd290f4df4b1df1d897cd225', '94a242df8a65e60f7f2a552fc961a98a8257f4fc3c423f194573a8c526ee1713'),
    'tests/lastmod-declared-routes.test.mjs': ('e4c8d463161961f3d0c42a8b7f890a690e16a32e1a0275a293daed8a81d01cdc', 'd6f27fa37b7f43e19f74fc52dab46347e63bb1daaa290e8679dc778c59df953b'),
    'tests/product-ready.test.mjs': ('0c844afb42523b8d0a9427d10eca8ea377f114840418bf201e34c94b3531baa5', '4adeaf375162794450362c2c1a033143ca1eaecd9309889f3258a0b3c2382a38'),
    'src/inherited-disclosure-dates.mjs': (None, '08716ffbf84c31bfcc892491556874e7af71298f2ccf9aa6c81a08a43b5dcdd3'),
    'data/d2w-s9-d2p-provenance-evidence.json': (None, '9f44cbdd3bd4614405d8e125f358518ed2e9aa364e5df934a74c8490e94f7015'),
    'tests/k9-suite-repair.test.mjs': (None, 'cc328085f3c3fac7758a1cdc83a358354c9fe8c7458b9bbb80f48fec2c919335'),
    'tests/fixtures/k9/postbuild-aeo-surface.preimage.mjs': (None, '406123d8e2162a9321b30bec51c36e6b345eb4917022135760721c61f5bc3741'),
}


def row_closure(rows):
    digest = hashlib.sha256()
    for row in sorted(rows, key=lambda r:r['path']):
        digest.update(f"{row['path']}\0{row['sha256']}\0{row['bytes']}\n".encode())
    return digest.hexdigest()


def price_suite_subject(rows):
    # Disposable per-build ops reports may change between deterministic builds;
    # executable source, tests, data, checkpoints and ALL served bytes may not.
    lookup={r['path']:r for r in rows}
    pairs={
        'artifacts/build4-preservation/glp-1-price-index/index.html':'public/glp-1-price-index/index.html',
        'artifacts/build4-preservation/ops/price-index-manifest.json':'artifacts/ops/price-index-manifest.json',
    }
    # The second build advances its checkpoint to the already-tested output.
    # Compare the paired logical value; final custody is checked separately.
    return [dict(lookup[pairs[r['path']]],path=r['path']) if r['path'] in pairs else r
            for r in rows if not r['path'].startswith('artifacts/ops/')]


def price_index_scope(before, after):
    """Publication allowance inheritance, not a new acceptance or wider waiver."""
    def indexed(rows):
        result = {}
        for row in rows:
            path = row['path']
            if (not isinstance(path, str) or not path or Path(path).is_absolute()
                or any(p in ('', '.', '..') for p in path.split('/')) or path in result):
                raise RuntimeError('price_index_manifest_path_invalid')
            size = row.get('bytes', row.get('size'))
            if type(size) is not int or size < 0 or not re.fullmatch('[a-f0-9]{64}', row['sha256']):
                raise RuntimeError('price_index_manifest_row_invalid')
            result[path] = (row['sha256'], size)
        return result
    old, new = indexed(before), indexed(after)
    if old.keys() - new.keys():
        raise RuntimeError('price_index_inheritance_deletes_paths')
    changed = sorted(p for p in new if old.get(p) != new[p])
    for path in changed:
        generated = (path.startswith('artifacts/ops/')
            or path in ('assets/provider-logos/pass2-lp-atlas.webp', 'assets/provider-logos/pass2-lp-atlas.json',
                        'artifacts/ultracode/phase2-home/generated-image-lineage.json',
                        'artifacts/ultracode/phase3-product-renders/generated-image-lineage.json',
                        'artifacts/build4-preservation/glp-1-price-index/index.html',
                        'artifacts/build4-preservation/ops/price-index-manifest.json',
                        'artifacts/build4-preservation/ops/wave4-deal-ingestion-manifest.json',
                        'artifacts/build4-preservation/_headers')
            or path.startswith(('artifacts/build4-preservation/reviews/', 'artifacts/build4-preservation/google-ads-api/'))
            or re.fullmatch(r'assets/site(?:-home|-money|-review|-editorial|-tools|-trust|-p3)?\.css', path)
            or re.fullmatch(r'assets/derivatives/(?:home-hero-(?:fruit|fridge|hand)-(?:640|1024)|home-lifestyle-phone-(?:768|1536)|phase3-product-(?:wegovy-pill|wegovy|ozempic|mounjaro|zepbound)-(?:640|1024))\.(?:avif|webp|jpg)', path))
        if path not in PRICE_INDEX_PUBLIC and path not in PRICE_INDEX_SOURCES and not generated:
            raise RuntimeError('price_index_inheritance_scope:' + path)
        if path.startswith(('scripts/', 'src/', 'tests/')) or path=='data/d2w-s9-d2p-provenance-evidence.json':
            transition=PRICE_INDEX_SOURCE_TRANSITIONS.get(path)
            allowed=(() if transition is None else (transition,))
            if path == 'tests/lastmod-declared-routes.test.mjs':
                allowed += (('e4c8d463161961f3d0c42a8b7f890a690e16a32e1a0275a293daed8a81d01cdc',
                             'b073d42c833356ecfa5079d5ed42d3e9882b861f41e9eed414afe214dc43ada8'),)
            if (old.get(path,(None,))[0],new[path][0]) not in allowed:
                raise RuntimeError('price_index_unapproved_source_transition:' + path)
    return changed


def validate_price_index_binding(binding, pointer, *, planned_manifest=None):
    parent = read_bound(binding['price_index_parent_binding'])
    validate_binding(parent, parent)
    if binding['price_index_parent_binding'] != descriptor(runtime_binding_path(parent)):
        raise RuntimeError('price_index_parent_runtime_drift')
    if any(binding[k] != parent[k] for k in PRICE_INDEX_INHERITED_KEYS):
        raise RuntimeError('price_index_allowance_widened')
    old = read_bound(parent['release_manifest'])
    new = read_bound(binding['release_manifest'])
    if any(new[k] != pointer[k] for k in ('release_root', 'release_tree_sha256')):
        raise RuntimeError('price_index_release_identity_drift')
    if new.get('schema') != 'glp-opus-release-deployment-manifest-v1':
        raise RuntimeError('price_index_release_manifest_schema')
    if new.get('rollback_release_root') != parent['release_root']:
        raise RuntimeError('price_index_predecessor_manifest_drift')
    if pointer.get('manifest') is not None and pointer['manifest'] != binding['release_manifest']:
        # Remint produces its own equivalent, exact public/whole-tree manifest.
        supplied=read_bound(pointer['manifest']) if planned_manifest is None else planned_manifest
        if any(supplied[k]!=new[k] for k in ('release_root','release_tree_sha256','release_rows')):
            raise RuntimeError('price_index_pointer_manifest_drift')
        # Legacy remint v1 carries the public closure in its terminal receipt,
        # not as a manifest field. Derive it from the exact bound public rows;
        # do not add a field the executed constructor's v1 reader rejects.
        supplied_public=row_closure([dict(r,path=r['path'][len('public/'):])
            for r in supplied['release_rows'] if r['path'].startswith('public/')])
        if (supplied.get('schema') != new['schema']
            or supplied_public != new['public_tree_sha256']
            or supplied.get('public_tree_sha256', supplied_public) != supplied_public
            or pointer.get('public_tree_sha256', supplied_public) != supplied_public):
            raise RuntimeError('price_index_pointer_public_closure_drift')
    price_index_scope(old['release_rows'], new['release_rows'])
    root = Path(pointer['release_root'])
    actual = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise RuntimeError('price_index_release_symlink')
        if path.is_file():
            actual.append({'path':path.relative_to(root).as_posix(),
                'sha256':descriptor(path)['sha256'], 'bytes':path.stat().st_size})
    digest = hashlib.sha256()
    for row in sorted(actual, key=lambda item: item['path']):
        digest.update(f"{row['path']}\0{row['sha256']}\0{row['bytes']}\n".encode())
    if actual != new['release_rows'] or digest.hexdigest() != pointer['release_tree_sha256']:
        raise RuntimeError('price_index_release_bytes_drift')
    public_rows=[dict(r,path=r['path'][len('public/'):]) for r in actual if r['path'].startswith('public/')]
    if row_closure(public_rows)!=new.get('public_tree_sha256'):
        raise RuntimeError('price_index_public_closure_drift')
    actual_by_path={r['path']:r for r in actual}
    for checkpoint,produced in (
        ('artifacts/build4-preservation/glp-1-price-index/index.html','public/glp-1-price-index/index.html'),
        ('artifacts/build4-preservation/ops/price-index-manifest.json','artifacts/ops/price-index-manifest.json')):
        if checkpoint in actual_by_path and any(actual_by_path[checkpoint][k]!=actual_by_path[produced][k] for k in ('sha256','bytes')):
            raise RuntimeError('price_index_checkpoint_pair_drift')
    classification = read_bound(binding['price_index_classification'])
    verified = verify_classification(Path(binding['price_index_classification']['path']).parent)
    if classification != verified or verified['binding'] != binding['price_index_parent_binding']:
        raise RuntimeError('price_index_classification_drift')
    # Keep every non-index sitemap entry at the predecessor's exact date.
    import xml.etree.ElementTree as ET
    def sitemap(base):
        rows = ET.parse(Path(base)/'public/sitemap.xml').getroot()
        values = [tuple((child.tag,child.text) for child in row) for row in rows]
        urls = [dict(v).get('{http://www.sitemaps.org/schemas/sitemap/0.9}loc') for v in values]
        if len(set(urls)) != len(urls): raise RuntimeError('price_index_sitemap_duplicate')
        return dict(zip(urls,values))
    old_map,new_map = sitemap(parent['release_root']),sitemap(root)
    index_url='https://glpclinicscout.com/glp-1-price-index/'
    if old_map.keys()!=new_map.keys() or any(old_map[k]!=new_map[k] for k in old_map if k!=index_url):
        raise RuntimeError('price_index_unrelated_sitemap_drift')
    witness=read_bound(binding['price_index_suite_witness'])
    if (witness.get('schema')!='glp-price-index-suite-witness-v1'
        or witness.get('expected_stage')!='production-seal'
        or witness.get('classification')!=binding['price_index_classification']
        or witness.get('parent_binding')!=binding['price_index_parent_binding']
        or witness.get('tested_public_tree_sha256')!=new['public_tree_sha256']
        or witness.get('candidate_root')==str(root)
        or price_suite_subject(witness['before_rows'])!=price_suite_subject(witness['after_rows'])
        or price_suite_subject(witness['before_rows'])!=price_suite_subject(actual)):
        raise RuntimeError('price_index_suite_subject_drift')
    constructor=witness['constructor']
    if descriptor(constructor['path'])!=constructor or witness.get('constructor_sha256')!=constructor['sha256']:
        raise RuntimeError('price_index_suite_constructor_drift')
    return binding


def install_price_index_binding(parent_pointer, manifest_path, report):
    parent,spec = load_binding(parent_pointer)
    manifest = read_bound(descriptor(manifest_path))
    binding = {k:parent[k] for k in PRICE_INDEX_INHERITED_KEYS}
    binding.update(schema='glp-accepted-failures-binding-v1', publication_allowance_only=True,
        release_root=manifest['release_root'],release_tree_sha256=manifest['release_tree_sha256'],
        release_manifest=descriptor(manifest_path),price_index_parent_binding=spec,
        price_index_suite_witness=descriptor(Path(report)/'price-index-suite-witness.json'),
        price_index_classification=descriptor(Path(report)/'00-accepted-failures-classification.json'))
    return install_binding(binding, manifest)


def runtime_binding_path(pointer):
    tree = pointer.get('release_tree_sha256', '')
    if not re.fullmatch('[0-9a-f]{64}', tree):
        raise RuntimeError('accepted_failures_tree_invalid')
    return RUNTIME_ROOT / tree / 'accepted-failures.json'


def load_binding(pointer):
    spec = descriptor(runtime_binding_path(pointer))
    return validate_binding(read_bound(spec), pointer), spec


def load_planned_binding(pointer, manifest_path, manifest_data):
    """Validate exact planned bytes; every existing authority stays disk-bound.

    This is preflight only. Call load_binding again after persisting evidence,
    before advancing the current-base pointer.
    """
    manifest_spec = {'path': str(Path(manifest_path).resolve()),
                     'sha256': hashlib.sha256(manifest_data).hexdigest()}
    if pointer.get('manifest') != manifest_spec:
        raise RuntimeError('planned_manifest_descriptor_drift')
    planned_manifest = json.loads(manifest_data)
    spec = descriptor(runtime_binding_path(pointer))
    return validate_binding(read_bound(spec), pointer, planned_manifest=planned_manifest), spec


def write_json(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + '\n')
    os.replace(temporary, path)


def install_binding(binding, pointer):
    validate_binding(binding, pointer)
    path = runtime_binding_path(pointer)
    if path.exists():
        if json.loads(path.read_text()) != binding:
            raise RuntimeError('accepted_failures_runtime_collision')
    else:
        write_json(path, binding)
    return load_binding(pointer)[1]


def inherit_runtime_binding(job, report, manifest, final):
    report = Path(report)
    classification = verify_classification(report, job)
    if final.get('accepted_failures') != classification:
        raise RuntimeError('accepted_failures_final_classification_drift')
    parent = read_bound(classification['binding'])
    binding = {key: value for key, value in parent.items()
               if key not in ('parent_binding', 'descendant_manifest', 'descendant_final',
                              'price_index_parent_binding', 'price_index_classification', 'price_index_suite_witness')}
    binding.update(release_root=manifest['release_root'], release_tree_sha256=manifest['release_tree_sha256'],
                   release_manifest=descriptor(report / '01-deployment-manifest.json'),
                   parent_binding=classification['binding'],
                   descendant_manifest=descriptor(report / '01-deployment-manifest.json'),
                   descendant_final=descriptor(report / '10-final-publication-receipt.json'))
    return install_binding(binding, manifest)


def job_pointer(job):
    manifest = read_bound(job['base_manifest'])
    if manifest['release_root'] != job['base_release_root']:
        raise RuntimeError('accepted_failures_job_base_drift')
    return {key: manifest[key] for key in ('release_root', 'release_tree_sha256')}


ROOT_BINDING_NAME = 'accepted-failures.json'


def root_binding_value(pointer):
    """Acyclic receipt: the new root carries its validated predecessor allowance."""
    _binding, spec = load_binding(pointer)
    return {'schema': 'glp-accepted-failures-root-v1',
            'release_root': pointer['release_root'],
            'release_tree_sha256': pointer['release_tree_sha256'],
            'binding': spec, 'publication_allowance_only': True}


def validate_root_binding(root, pointer):
    path = Path(root) / ROOT_BINDING_NAME
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("accepted_failures_binding_invalid:'release_root'")
    value = json.loads(path.read_text())
    if value != root_binding_value(pointer):
        raise RuntimeError('accepted_failures_root_binding_drift')
    read_bound(value['binding'])
    return descriptor(path)


def stage_root_binding(root, pointer):
    root = Path(root)
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("accepted_failures_binding_invalid:'release_root'")
    # Atomic replace detaches any inherited hardlink; never edit predecessor bytes.
    write_json(root / ROOT_BINDING_NAME, root_binding_value(pointer))
    return validate_root_binding(root, pointer)


def run_suite(job, cwd, *, runner=None):
    report = Path(job['report_dir']); report.mkdir(parents=True, exist_ok=True)
    classification_path = report / '00-accepted-failures-classification.json'
    try:
        pointer = job_pointer(job)
        binding, spec = load_binding(pointer)
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        write_json(classification_path, {'status': 'FAIL_ACCEPTANCE_BINDING', 'errors': [str(exc)]})
        raise RuntimeError('accepted_failures_binding_refused:' + str(exc)) from exc
    try:
        result = (runner or subprocess.run)(['npm', 'test'], cwd=cwd, text=True, capture_output=True, timeout=1800)
        stdout, stderr, code = result.stdout, result.stderr, result.returncode
    except (subprocess.TimeoutExpired, OSError) as exc:
        stdout = getattr(exc, 'stdout', '') or ''
        stderr = getattr(exc, 'stderr', '') or str(exc)
        if isinstance(stdout, bytes): stdout = stdout.decode(errors='replace')
        if isinstance(stderr, bytes): stderr = stderr.decode(errors='replace')
        code = -1
    stdout_path, stderr_path = report / '00-suite-stdout.log', report / '00-suite-stderr.log'
    stdout_path.write_text(stdout); stderr_path.write_text(stderr)
    classification = classify_suite(stdout, code, binding['accepted_names'], minimum_tests=binding['minimum_tests'])
    classification.update(binding=spec, base=pointer, suite_stdout=descriptor(stdout_path), suite_stderr=descriptor(stderr_path))
    write_json(classification_path, classification)
    verified = verify_classification(report, job)
    return {'command': ['npm', 'test'], 'exit_code': code, 'stdout_tail': stdout[-8000:],
            'stderr_tail': stderr[-8000:], 'accepted_failures': verified}


def verify_classification(report, job=None):
    report = Path(report)
    value = json.loads((report / '00-accepted-failures-classification.json').read_text())
    pointer = job_pointer(job) if job is not None else value['base']
    if value['base'] != pointer or descriptor(runtime_binding_path(pointer)) != value['binding']:
        raise RuntimeError('accepted_failures_classification_base_drift')
    binding = validate_binding(read_bound(value['binding']), pointer)
    for field, filename in [('suite_stdout', '00-suite-stdout.log'), ('suite_stderr', '00-suite-stderr.log')]:
        if descriptor(report / filename) != value[field]:
            raise RuntimeError('accepted_failures_suite_log_drift')
    stdout = (report / '00-suite-stdout.log').read_text()
    expected = classify_suite(stdout, value['suite_exit_code'], binding['accepted_names'], minimum_tests=binding['minimum_tests'])
    expected.update(binding=value['binding'], base=pointer, suite_stdout=value['suite_stdout'], suite_stderr=value['suite_stderr'])
    if expected != value or value['status'] != 'PASS_ACCEPTED_FAILURES':
        raise RuntimeError('accepted_failures_gate_refused:' + value['status'] + ':' + json.dumps(value.get('new_failures', [])))
    return value


def classify_suite(stdout, exit_code, accepted_names, *, minimum_tests):
    errors = []
    counts = {}
    for key in ('tests', 'suites', 'pass', 'fail', 'cancelled', 'skipped', 'todo'):
        values = re.findall(r'^# ' + key + r' (\d+)$', stdout, re.M)
        if len(values) != 1:
            errors.append('summary_invalid:' + key)
        else:
            counts[key] = int(values[0])
    records = re.findall(r'^([ \t]*)(not ok|ok) (\d+) - (.+)$', stdout, re.M)
    # Node's suite summaries are not test cases; refuse them until explicitly supported.
    failures = [name for _, outcome, _, name in records if outcome == 'not ok']
    if len(failures) != len(set(failures)) or len(accepted_names) != len(set(accepted_names)):
        errors.append('duplicate_failure_identity')
    skipped = [name for _, _, _, name in records if re.search(r' # SKIP(?: |$)', name)]
    top = [int(num) for indent, _, num, _ in records if not indent]
    plans = re.findall(r'^1\.\.(\d+)$', stdout, re.M)
    # Node emits file ordinals for executable test files without test() calls,
    # interleaved with case ordinals. Cardinality, not unique numbering, binds the plan.
    if len(plans) != 1 or len(top) != (int(plans[0]) if plans else 0):
        errors.append('plan_or_sequence_invalid')
    if stdout.count('TAP version 13') != 1 or not re.search(r'^# duration_ms [\d.]+\s*\Z', stdout, re.M):
        errors.append('tap_envelope_invalid')
    if re.search(r'Bail out!|ERR_TEST_FAILURE.*cancelled|SIG(?:KILL|TERM|SEGV)', stdout):
        errors.append('suite_crashed')
    if counts:
        if counts.get('tests', 0) < minimum_tests or counts.get('tests') != len(records):
            errors.append('test_population_invalid')
        if counts.get('fail') != len(failures) or counts.get('skipped') != len(skipped):
            errors.append('outcome_count_mismatch')
        if counts.get('tests') != sum(counts.get(k, 0) for k in ('pass','fail','cancelled','skipped','todo')):
            errors.append('summary_count_mismatch')
        if any(counts.get(k, 0) for k in ('suites', 'cancelled', 'todo')):
            errors.append('unsupported_or_incomplete_suite')
    if exit_code != (1 if failures else 0):
        errors.append('exit_code_mismatch')
    accepted = [name for name in failures if name in accepted_names]
    new = [name for name in failures if name not in accepted_names]
    return {'schema': 'glp-accepted-failures-classification-v1',
            'status': 'FAIL_SUITE_INTEGRITY' if errors else ('FAIL_NEW_FAILURES' if new else 'PASS_ACCEPTED_FAILURES'),
            'accepted_inherited': accepted, 'new_failures': new, 'errors': errors,
            'counts': counts, 'suite_exit_code': exit_code,
            'suite_stdout_sha256': hashlib.sha256(stdout.encode()).hexdigest(),
            'accepted_inherited_count': len(accepted), 'new_failure_count': len(new)}


def current_binding_cli(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Current Astra release acceptance producer; no publication effects')
    sub = parser.add_subparsers(dest='action', required=True)
    prepare = sub.add_parser('prepare-current')
    prepare.add_argument('--pointer', type=Path, required=True)
    prepare.add_argument('--suite-stdout', type=Path, required=True)
    prepare.add_argument('--suite-result', type=Path, required=True)
    prepare.add_argument('--suite-exit-code', type=int, required=True)
    prepare.add_argument('--minimum-tests', type=int, required=True)
    prepare.add_argument('--preparer-id', required=True)
    prepare.add_argument('--out', type=Path, required=True)
    install = sub.add_parser('install-current')
    install.add_argument('--pointer', type=Path, required=True)
    install.add_argument('--proposal', type=Path, required=True)
    install.add_argument('--suite-acceptance', type=Path, required=True)
    install.add_argument('--expected-suite-acceptance-sha256', required=True)
    args = parser.parse_args(argv)
    pointer_spec = descriptor(args.pointer)
    pointer = read_bound(pointer_spec)
    if args.action == 'prepare-current':
        proposal = prepare_current_binding(pointer, args.suite_stdout, args.suite_exit_code,
            minimum_tests=args.minimum_tests, preparer_id=args.preparer_id,
            suite_result_path=args.suite_result)
        # A reviewed proposal is immutable: never overwrite it on rerun.
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open('x') as handle:
            handle.write(json.dumps(proposal, sort_keys=True, indent=2) + '\n')
        print(json.dumps({'status': proposal['status'], 'proposal': descriptor(args.out),
                          'counts': proposal['classification']['counts']}))
    else:
        binding = build_current_binding(args.proposal, args.suite_acceptance,
                                         args.expected_suite_acceptance_sha256)
        if descriptor(args.pointer) != pointer_spec:
            raise RuntimeError('current_acceptance_pointer_changed')
        print(json.dumps({'binding': install_binding(binding, pointer)}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(current_binding_cli())
    except (RuntimeError, OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit('BLOCKED: ' + str(exc)) from exc
