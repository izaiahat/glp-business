"""No retrospective terminal state may be inferred from absence/equality."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
import glp_ship as ship
from glp_ship_generation import json_sha
from glp_ship_registry import registered_job


def case(tmp_path, monkeypatch):
    root, home = tmp_path / 'root', tmp_path / 'home'
    root.mkdir(); home.mkdir()
    spec = registered_job('aadf6677c7f0')
    source = 'a' * 40
    output = root / 'reports/ops/glp-ship' / spec.name / source
    (home / 'cron').mkdir()
    leaf = root / 'preimage'
    leaf.write_text('original')
    proposal = {'schema': 'glp-ship-proposal-v1', 'job_id': spec.name,
                'source': source, 'kind': spec.kind, 'tier': spec.tier,
                'root': str(root), 'hermes': str(home), 'prepared_by': 'preparer',
                'maintenance_window_required': True, 'jobs': {j: {'id': j} for j in spec.jobs},
                'paths': list(spec.paths), 'hashes': {},
                'merge_proof': {'merge_sha': source, 'pr_head': 'b' * 40, 'pr_number': 1,
                                'ci_run_id': 1, 'ci_attempt': 1},
                'before': {str(leaf): ship.fingerprint(leaf)}}
    (home / 'cron/jobs.json').write_text(json.dumps({'jobs': list(proposal['jobs'].values())}))
    attempt = root / 'reports/ops/glp-ship/attempts' / (spec.name + '-' + source + '.json')
    attempt.parent.mkdir(parents=True)
    attempt.write_text(json.dumps({'state': 'started', 'proposal_sha256': json_sha(proposal)}))
    digest = hashlib.sha256(attempt.read_bytes()).hexdigest()
    evidence = {'schema': 'glp-ship-pre-window-refusal-v1', 'source': source,
                'attempt_sha256': digest, 'proposal_sha256': json_sha(proposal),
                'root': str(root), 'hermes': str(home), 'recorded_by': 'observer',
                'window_journal': str(output / 'window.json'),
                'stage': 'MaintenanceWindow.plan', 'exception': 'FileNotFoundError',
                'missing_path': str(root / 'scripts/glp331_carrier_adoption.py'),
                'snapshot_started': False, 'pause_started': False, 'adoption_started': False,
                'traceback_sha256': 'b' * 64}
    review = {'schema': 'glp-ship-refusal-review-v1', 'reviewer': 'independent',
              'decision': 'ACCEPT_EXACT_PRE_WINDOW_REFUSAL', 'source': source,
              'attempt_sha256': digest, 'proposal_sha256': json_sha(proposal),
              'evidence_sha256': json_sha(evidence)}
    monkeypatch.setattr(ship.GitGeneration, 'verify', lambda *a: None)
    monkeypatch.setattr(ship, 'verify_merge', lambda *a: deepcopy(proposal['merge_proof']))
    return SimpleNamespace(**locals())


def inspect(c, evidence=True):
    return ship.inspect_attempt(c.root, c.root, c.home, c.spec, c.proposal,
                                c.evidence if evidence else None, c.review if evidence else None)


def test_cold_documented_inspection_cli_has_zero_filesystem_writes(tmp_path, monkeypatch):
    c = case(tmp_path, monkeypatch)
    cold = tmp_path / 'cold/scripts'
    shutil.copytree(Path(ship.__file__).parent, cold,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*.pyo'))
    assert not list(cold.rglob('__pycache__'))
    assert not list(cold.rglob('*.pyc'))
    ready = tmp_path / 'ready.json'
    ready.write_text(json.dumps({'proposal': c.proposal}))
    # These are private synthetic old-attempt/native records, never live data.
    for path in (c.attempt, c.home / 'cron/jobs.json', c.leaf, ready):
        path.chmod(0o444)
    argv = [sys.executable, str(cold / 'glp_ship.py'), 'aadf6677c7f0',
            '--merge-sha', c.source, '--root', str(c.root), '--hermes', str(c.home),
            '--checkout', str(cold.parent), '--inspect-attempt', '--proposal', str(ready)]
    env = {k: v for k, v in os.environ.items()
           if k not in {'PYTHONDONTWRITEBYTECODE', 'PYTHONPYCACHEPREFIX', 'PYTHONPATH'}}
    env['HOME'] = str(c.home)

    def snapshot():
        # Include directory mtimes too: create-then-delete is not read-only.
        return {str(p.relative_to(tmp_path)):
                (p.stat().st_mode, p.stat().st_mtime_ns,
                 hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                for p in (tmp_path, *tmp_path.rglob('*'))}

    before = snapshot()
    child = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True,
                           text=True, timeout=30)
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    assert result['assessment'] == 'UNRESOLVED_NO_REVIEWED_BOUNDARY_EVIDENCE'
    assert result['state'] == 'started'
    assert result['transition_performed'] is False and result['replay_permitted'] is False
    after = snapshot()
    assert after == before, 'READ_ONLY_CLI_WRITES: ' + repr(sorted(
        path for path in before.keys() | after.keys() if before.get(path) != after.get(path)))


def test_started_attempt_inspection_preserves_original_and_never_enables_replay(tmp_path, monkeypatch):
    c = case(tmp_path, monkeypatch)
    before = c.attempt.read_bytes()
    unresolved = inspect(c, False)
    assert unresolved['assessment'] == 'UNRESOLVED_NO_REVIEWED_BOUNDARY_EVIDENCE'
    checked = inspect(c)
    assert checked['assessment'] == 'REVIEWED_REFUSAL_CURRENTLY_CONSISTENT'
    assert checked['state'] == 'started' and checked['replay_permitted'] is False
    assert checked['transition_performed'] is False
    assert c.attempt.read_bytes() == before
    with pytest.raises(RuntimeError, match='ship_attempt_exists_reconcile'):
        ship.claim_attempt(c.attempt, c.proposal)
    assert c.attempt.read_bytes() == before


@pytest.mark.parametrize('bad', ['ambiguous', 'postwindow', 'drift', 'job-drift', 'attempt', 'proposal', 'source', 'same-reviewer', 'window', 'symlink-window'])
def test_inspection_refuses_ambiguous_postwindow_drift_and_binding_changes(tmp_path, monkeypatch, bad):
    c = case(tmp_path, monkeypatch)
    if bad == 'ambiguous':
        c.evidence.pop('snapshot_started')
    elif bad == 'postwindow':
        c.evidence['pause_started'] = True
    elif bad == 'drift':
        c.leaf.write_text('moved')
    elif bad == 'job-drift':
        (c.home / 'cron/jobs.json').write_text('{"jobs": []}')
    elif bad == 'attempt':
        c.attempt.write_text(c.attempt.read_text() + ' ')
    elif bad == 'proposal':
        c.proposal['prepared_by'] = 'changed'
    elif bad == 'source':
        c.evidence['source'] = 'c' * 40
    elif bad == 'same-reviewer':
        c.review['reviewer'] = 'observer'
    else:
        c.output.mkdir(parents=True)
        window = c.output / 'window.json'
        window.write_text('{}') if bad == 'window' else window.symlink_to(c.output / 'absent')
    # Review the altered record too: review alone cannot rescue bad boundaries.
    c.review['evidence_sha256'] = json_sha(c.evidence)
    before = c.attempt.read_bytes()
    with pytest.raises(RuntimeError):
        inspect(c)
    assert c.attempt.read_bytes() == before
