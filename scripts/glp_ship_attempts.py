"""GLP334-only, reviewed no-effect drain supersession; history is never rewritten.

The sidecar is the terminal state plus ONE fresh reservation in one exclusive,
fsynced record. A partial record fails closed. A retry requires its own exclusive
claim, current preimages, original proposal acceptance and all normal apply gates.
Review documents are operator-authenticated attestations, not OS signatures.
"""
import hashlib
import json
import os
from pathlib import Path
import re

from glp_ship_generation import json_sha
from maxbounty_pin_release import read_regular


def require(condition, reason):
    if not condition:
        raise RuntimeError('ship_supersession_' + reason)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def absent(path):
    return not path.exists() and not path.is_symlink()


def checked_journal(root, name):
    journal = Path(name)
    require(journal.is_absolute() and journal == journal.resolve()
            and journal.is_relative_to(root) and journal.name == 'window.json', 'journal_path')
    read_regular(journal)  # Reject alias/absence before creating any operation lock.
    return journal


def exclusive_json(path, value):
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        raise RuntimeError('ship_supersession_already_claimed:' + str(path)) from None
    # Do not remove a possibly partial intent after uncertainty.
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write('\n'); handle.flush(); os.fsync(handle.fileno())
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def paths(root, proposal, attempt_id):
    require(isinstance(attempt_id, str) and re.fullmatch('[a-z0-9][a-z0-9-]{0,63}', attempt_id), 'attempt_id')
    source = proposal.get('source', '')
    require(isinstance(source, str) and re.fullmatch('[a-f0-9]{40}', source), 'source')
    original = root / 'reports/ops/glp-ship/attempts' / ('GLP334-' + source + '.json')
    return (original, original.with_suffix('.supersession.json'),
            original.with_name(original.stem + '.retry-' + attempt_id + '.json'))


def checked_generation(checkout, root, hermes, spec, proposal, proposal_review):
    import glp_ship as ship
    require(spec.name == 'GLP334' and spec.kind == 'reporting-enrollment' and spec.tier == 'A'
            and proposal.get('schema') == 'glp-ship-proposal-v1'
            and proposal.get('job_id') == spec.name and proposal.get('kind') == spec.kind
            and proposal.get('tier') == spec.tier and proposal.get('maintenance_window_required') is True
            and proposal.get('paths') == list(spec.paths)
            and set(proposal.get('jobs', {})) == set(spec.jobs)
            and proposal.get('run_policy') == {'entry': 'NONE_ENROLLMENT_ONLY', 'job_ids': list(spec.jobs),
                                              'on_success': 'preserve', 'on_failure': 'reconcile_no_replay'},
            'registered_proposal_binding')
    ship.authenticate_installer(checkout)
    ship.validate_review(proposal, proposal_review)
    proof = ship.verify_merge(checkout, proposal['source'])
    require(all(proposal['merge_proof'].get(k) == proof[k] for k in ('merge_sha', 'pr_number', 'pr_head')),
            'proposal_source_binding')
    generation = ship.GitGeneration(proposal['source'], spec.kind, spec.paths, proposal['hashes'],
                                    proposal['merge_proof'], proposal)
    generation.verify(checkout)
    generation.authorize(proposal_review, root, hermes, None)
    return generation


def dead_owner(owner):
    from maintenance_window import identity
    require(isinstance(owner, dict) and set(owner) == {'pid', 'start_ticks', 'boot_id'}
            and type(owner['pid']) is int and owner['pid'] > 0
            and type(owner['start_ticks']) is int and owner['start_ticks'] > 0
            and isinstance(owner['boot_id'], str) and bool(owner['boot_id']), 'owner_identity_required')
    try:
        current = identity(owner['pid'])
    except FileNotFoundError:
        return  # PID is absent; permission/unreadable identity is NOT absence.
    require(current != owner, 'apply_owner_alive')


def validate_refusal(root, hermes, spec, proposal, evidence, review, attempt_id, output):
    import maintenance_window as mw
    original, record, fresh = paths(root, proposal, attempt_id)
    body = read_regular(original)[0]
    require(original.stat().st_nlink == 1, 'aliased_intent')
    require(json.loads(body) == {'state': 'started', 'proposal_sha256': json_sha(proposal)}, 'exclusive_started_required')
    require(isinstance(evidence, dict) and isinstance(review, dict), 'reviewed_restoration_required')
    require(set(evidence) == {'schema', 'source', 'root', 'hermes', 'attempt_sha256', 'proposal_sha256',
                             'window_journal', 'window_sha256', 'recorded_by', 'apply_exited', 'exit_code',
                             'stage', 'exception', 'adoption_started', 'producer_started', 'effects_uncertain'}
            and set(review) == {'schema', 'decision', 'reviewer', 'evidence_sha256', 'proposal_sha256',
                                'fresh_attempt_id', 'fresh_output'}, 'unaccounted_boundary_fields')
    require(evidence.get('schema') == 'glp-ship-drain-refusal-v1'
            and evidence.get('source') == proposal['source']
            and evidence.get('root') == str(root) and evidence.get('hermes') == str(hermes)
            and evidence.get('attempt_sha256') == sha(body)
            and evidence.get('proposal_sha256') == json_sha(proposal)
            and evidence.get('apply_exited') is True and type(evidence.get('exit_code')) is int
            and evidence['exit_code'] == 2 and evidence.get('stage') == 'MaintenanceWindow.enter'
            and evidence.get('exception') == 'drain_timeout'
            and all(evidence.get(k) is False for k in ('adoption_started', 'producer_started', 'effects_uncertain')),
            'no_effect_boundary_required')
    journal = checked_journal(root, evidence.get('window_journal', ''))
    require(journal.stat().st_nlink == 1, 'aliased_journal')
    require(output.is_absolute() and output == output.resolve() and output.is_relative_to(root)
            and output != journal.parent and not any(p.is_symlink() for p in (output, *output.parents))
            and absent(output), 'fresh_output_not_unused')
    reviewer = str(review.get('reviewer') or '').strip().casefold()
    recorder = str(evidence.get('recorded_by') or '').strip().casefold()
    require(reviewer and recorder and reviewer not in {recorder, str(proposal['prepared_by']).strip().casefold()}
            and review.get('schema') == 'glp-ship-drain-supersession-review-v1'
            and review.get('decision') == 'ACCEPT_EXACT_DRAIN_SUPERSESSION'
            and review.get('evidence_sha256') == json_sha(evidence)
            and review.get('proposal_sha256') == json_sha(proposal)
            and review.get('fresh_attempt_id') == attempt_id and review.get('fresh_output') == str(output),
            'independent_exact_review_required')
    journal_body = read_regular(journal)[0]
    require(sha(journal_body) == evidence.get('window_sha256'), 'journal_digest')
    data = json.loads(journal_body)
    # Even an empty adoption/recovery field is evidence of a different boundary.
    allowed = {'schema', 'state', 'error', 'journal', 'root', 'hermes', 'created_at', 'closed_at',
               'expires_at', 'keeper', 'controller', 'locks', 'entries', 'source_sha256',
               'control_tools', 'inventory_shape', 'drain'}
    require(set(data) <= allowed and data.get('schema') == mw.SCHEMA
            and data.get('state') == 'closed' and data.get('error') == 'drain_timeout'
            and data.get('journal') == str(journal) and data.get('root') == str(root)
            and data.get('hermes') == str(hermes) and data.get('locks') == []
            and type(data.get('created_at')) in (int, float) and type(data.get('closed_at')) in (int, float)
            and data['closed_at'] >= data['created_at'], 'closed_pre_adoption_window_required')
    dead_owner(data.get('keeper'))
    if 'controller' in data:
        dead_owner(data['controller'])
    rows = data.get('entries')
    require(isinstance(rows, list) and bool(rows), 'complete_restoration_required')
    seen = set()
    backend = mw.LinuxBackend(root, hermes, journal)
    for row in rows:
        require(isinstance(row, dict) and set(row) == {'entry', 'prior', 'intent', 'paused', 'restore_intent', 'restored'}
                and all(row.get(k) is True for k in ('intent', 'paused', 'restore_intent', 'restored')),
                'complete_restoration_required')
        entry, prior = row['entry'], row['prior']
        key = (entry.get('kind'), entry.get('id'))
        require(key[0] in {'hermes', 'crontab', 'systemd', 'n8n'} and isinstance(key[1], str)
                and bool(key[1]) and key not in seen and entry.get('affected') is True, 'restoration_inventory')
        seen.add(key)
        if entry['kind'] == 'hermes':
            job = backend.job(entry['id'])
            require({k: job[k] for k in mw.CONTROL_FIELDS if k in job} == prior['fields']
                    and job.get('script_sha256') == prior['script_sha256']
                    and mw.job_configuration(job) == prior['configuration_sha256']
                    and all(not job.get(k) for k in ('running', 'fire_claim', 'run_claim', 'pending_slot')),
                    'native_restoration_readback')
        else:
            require(backend.recovery_restored(entry, prior), 'restoration_readback')
    default = root / 'reports/ops/glp-ship' / spec.name / proposal['source']
    for directory in {journal.parent, default}:
        require(all(absent(directory / name) for name in ('RESULT.json', 'enrollment-receipt.json')),
                'effect_receipt_present')
        if directory != journal.parent:
            require(absent(directory / 'window.json'), 'unbound_window_present')
    # Re-read exact evidence after slow control readbacks; no rewriting/synthesis.
    require(read_regular(original)[0] == body and read_regular(journal)[0] == journal_body,
            'evidence_changed_during_readback')
    return original, record, fresh, body


def supersede_drain_refusal(checkout, root, hermes, spec, proposal, proposal_review,
                            evidence, review, attempt_id, output):
    import glp_ship as ship
    from maintenance_window import operation_lock
    original, record, fresh = paths(root, proposal, attempt_id)
    require(isinstance(evidence, dict) and isinstance(review, dict), 'reviewed_restoration_required')
    journal = checked_journal(root, evidence.get('window_journal', ''))
    # Source lock excludes an in-progress apply; original journal operation lock
    # excludes restore/resume. Neither grants a live keeper's authority.
    with ship.syncer.source_lock(root), operation_lock(journal):
        require(absent(record) and absent(fresh), 'already_claimed')
        generation = checked_generation(checkout, root, hermes, spec, proposal, proposal_review)
        original, record, fresh, body = validate_refusal(root, hermes, spec, proposal, evidence,
                                                        review, attempt_id, output)
        generation.verify_target(root, hermes)
        result = {'schema': 'glp-ship-drain-supersession-v1', 'state': 'superseded',
                  'original_intent': str(original), 'original_intent_sha256': sha(body),
                  'proposal_sha256': json_sha(proposal), 'source': proposal['source'],
                  'evidence': evidence, 'review': review, 'proposal_review_sha256': json_sha(proposal_review),
                  'fresh_attempt': {'id': attempt_id, 'intent': str(fresh), 'output': str(output), 'state': 'reserved'}}
        exclusive_json(record, result)  # One atomic binding; original remains byte-for-byte unchanged.
        return result


def claim_retry(*, checkout, root, hermes, spec, proposal, proposal_review, attempt_id, output):
    import glp_ship as ship
    from maintenance_window import operation_lock
    original, record, fresh = paths(root, proposal, attempt_id)
    with ship.syncer.source_lock(root):
        stored = read_regular(record)[0]
        document = json.loads(stored)
        require(document.get('schema') == 'glp-ship-drain-supersession-v1'
                and document.get('state') == 'superseded'
                and document.get('original_intent') == str(original)
                and document.get('original_intent_sha256') == sha(read_regular(original)[0])
                and document.get('source') == proposal['source']
                and document.get('proposal_sha256') == json_sha(proposal)
                and document.get('proposal_review_sha256') == json_sha(proposal_review)
                and document.get('fresh_attempt') == {'id': attempt_id, 'intent': str(fresh),
                                                      'output': str(output), 'state': 'reserved'}, 'retry_binding')
        journal = checked_journal(root, document['evidence']['window_journal'])
        with operation_lock(journal):
            require(absent(fresh), 'already_claimed')
            generation = checked_generation(checkout, root, hermes, spec, proposal, proposal_review)
            validate_refusal(root, hermes, spec, proposal, document['evidence'], document['review'], attempt_id, output)
            ship.preclaim_plan(root, hermes)  # Full original admission, immediately before the new claim.
            generation.verify_target(root, hermes)
            require(read_regular(record)[0] == stored, 'record_changed_during_claim')
            exclusive_json(fresh, {'state': 'started', 'attempt_id': attempt_id,
                                   'proposal_sha256': json_sha(proposal), 'output': str(output),
                                   'supersession': str(record), 'supersession_sha256': sha(stored)})
            return fresh
