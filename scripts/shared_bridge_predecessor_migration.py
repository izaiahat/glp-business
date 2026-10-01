"""GLP337 fixed shared-launcher migration; no CLI or receiving-host authority.

Only the authentic Sol6.1 one-literal predecessor and its fixed cadence pair
are eligible. Independent review and separate install authority bind the whole
proposal. A receiving-host owner must supply an externally established reader
and writer exclusion context; this module cannot establish that authority.
"""
import hashlib
import subprocess
from pathlib import Path

from scripts import sync_from_repo as importer
from scripts.maxbounty_pin_release import read_regular, strict_json, assert_unchanged, preserve_custody

LAUNCHER = 'scripts/cron_bridge_action.py'
CADENCE = 'scripts/glp_analytics_brain_cadence.py'
SELECTED = (LAUNCHER, CADENCE)
BASELINE = '4e8da708900178d32b2f165e1d18067750b45dabb5a115d3487b4d1a9d2d6bcb'
PREDECESSOR = 'f00f66759a9bdc3a0f3a4156c5fbe8a6acc73597f5b83732e818b9cc7dc1ec55'
SUCCESSOR = 'f1055dea298e8354bd61d9ccad084a2b33ab9c028ed7070b54e23d64ad023032'
CADENCE_BEFORE = '61ca597689080f6f3e5de9b00c733009df258850f58bce7f592843e527f17c53'
CADENCE_AFTER = '4d51a627d7a5208238ab7ce7622772c118d9b141497942bf9ed8bd6d4587bcb2'
CONTEXT = ('ops/scheduled-source-drift-manifest.json', 'ops/n8n-bridge/actions.json',
           'ops/n8n-bridge/bridge.py')


def digest(body):
    return hashlib.sha256(body).hexdigest()


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def checked(repo, root):
    repo, root = repo.resolve(strict=True), root.resolve(strict=True)
    head = git(repo, 'rev-parse', 'HEAD').decode().strip()
    observed = {}
    def retain(path):
        observed[path] = read_regular(path)
        return observed[path][0]
    state_path = root / importer.STATE
    state = strict_json(retain(state_path))
    baseline = state.get('hashes', {})
    if (baseline.get(LAUNCHER) != BASELINE or
            baseline.get(CADENCE) not in (None, CADENCE_BEFORE)):
        raise RuntimeError('shared_bridge_fixed_baseline_required')
    for rel, before, after in ((LAUNCHER, PREDECESSOR, SUCCESSOR),
                              (CADENCE, CADENCE_BEFORE, CADENCE_AFTER)):
        if digest(retain(root / rel)) != before:
            raise RuntimeError('shared_bridge_exact_predecessor_required:' + rel)
        body = retain(repo / rel)
        if digest(body) != after or body != git(repo, 'show', head + ':' + rel):
            raise RuntimeError('shared_bridge_fixed_exact_head_successor_required:' + rel)
    manifest = retain(repo / importer.MANIFEST)
    if manifest != git(repo, 'show', head + ':' + importer.MANIFEST):
        raise RuntimeError('shared_bridge_manifest_not_exact_head')
    if (root / importer.MANIFEST).exists():
        retain(root / importer.MANIFEST)
    for rel in CONTEXT:
        retain(root / rel)
    inventory = strict_json(observed[root / CONTEXT[0]][0])
    bindings = sorted(row['id'] for row in inventory['jobs']
                      if any(s.get('source') == LAUNCHER for s in row.get('sources', [])))
    if len(bindings) != 20 or len(set(bindings)) != 20:
        raise RuntimeError('shared_bridge_exact_twenty_bindings_required')
    proposal = {'schema': 'glp337-shared-bridge-proposal-v1', 'installer_head': head,
                'root': str(root), 'selected': list(SELECTED),
                'bindings_count': len(bindings), 'binding_ids': bindings,
                'inputs': {str(p): {'sha256': digest(b), 'mode': mode}
                           for p, (b, mode) in sorted(observed.items())}}
    return proposal, observed, (() if CADENCE in baseline else (CADENCE,))


def prepare(repo, root):
    """Read-only proposal; no baseline, inventory, lock or scheduler writes."""
    proposal, _, _ = checked(repo, root)
    return proposal


def apply(repo, root, review_path, authority_path, *, reader_exclusion=None):
    """Guarded paired import, never inventory repin or activation authority."""
    repo, root = repo.resolve(strict=True), root.resolve(strict=True)
    proposal, before, adopted = checked(repo, root)
    review_body, review_mode = read_regular(review_path)
    authority_body, authority_mode = read_regular(authority_path)
    review, authority = strict_json(review_body), strict_json(authority_body)
    if (review.get('schema') != 'glp337-shared-bridge-review-v1'
            or review.get('decision') != 'ACCEPT_EXACT_MIGRATION'
            or not str(review.get('prepared_by') or '').strip()
            or not str(review.get('reviewer') or '').strip()
            or review['prepared_by'].strip() == review['reviewer'].strip()
            or review.get('proposal') != proposal):
        raise RuntimeError('shared_bridge_independent_exact_review_required')
    if (review_path.resolve() == authority_path.resolve()
            or authority.get('schema') != 'glp337-shared-bridge-authority-v1'
            or authority.get('decision') != 'AUTHORIZE_EXACT_MIGRATION'
            or not str(authority.get('owner') or '').strip()
            or not str(authority.get('writer_exclusion') or '').strip()
            or authority.get('proposal') != proposal
            or authority.get('review_sha256') != digest(review_body)):
        raise RuntimeError('shared_bridge_separate_exact_authority_required')
    if reader_exclusion is None:
        raise RuntimeError('shared_bridge_external_exclusion_required')
    before[review_path] = review_body, review_mode
    before[authority_path] = authority_body, authority_mode
    with reader_exclusion() as established:
        if established is not True:
            raise RuntimeError('shared_bridge_external_exclusion_required')
        with importer.source_lock(root):
            assert_unchanged(before)
            fresh, fresh_before, fresh_adopted = checked(repo, root)
            if fresh != proposal or fresh_adopted != adopted:
                raise RuntimeError('shared_bridge_inputs_moved_under_exclusion')
            kwargs = {'_shared_bridge_preimage': PREDECESSOR}
            if importer._sync_locked(root, repo, False, adopted, SELECTED, **kwargs) != 0:
                raise RuntimeError('shared_bridge_guarded_preflight_refused')
            custody = root / 'reports/ops/pin-archive/shared-bridge' / digest(review_body)
            preserve_custody(custody, before, {root / p: before[repo / p][0] for p in SELECTED})
            assert_unchanged(before)
            if importer._sync_locked(root, repo, True, adopted, SELECTED, **kwargs) != 0:
                raise RuntimeError('shared_bridge_guarded_apply_refused_no_retry')
            for rel in SELECTED:
                if read_regular(root / rel) != (before[repo / rel][0], before[root / rel][1]):
                    raise RuntimeError('shared_bridge_readback_mismatch_no_retry')
            for rel in CONTEXT:
                if read_regular(root / rel) != before[root / rel]:
                    raise RuntimeError('shared_bridge_context_changed_no_retry')
            return {'applied': True, 'custody': str(custody), 'installer_head': proposal['installer_head']}
