"""F01 source migration plus exact committed wrapper/native pin publication.

The existing discovery migration owns source locks, preimage exception and
custody. Existing cohort publication owns wrapper sealing and uncertain saves.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path

import discovery_predecessor_migration as migration
import sync_from_repo as syncer
from glp_ship_generation import fingerprint, json_sha
from maintenance_admission import process_readers
from maintenance_cohort import switch_files
from maintenance_window import SCHEMA, atomic_json, native_registry_lock, read_journal
from maxbounty_pin_release import read_regular

JID = '58202b06bdc0'
WRAPPER = 'affiliate_supply_discovery_daily.sh'
SNAPSHOT = 'ops/hermes-cron-scripts/' + WRAPPER


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def apply(checkout, root, hermes, generation, review, output, scheduler):
    checkout, root, hermes, output = map(lambda p: Path(p).resolve(), (checkout, root, hermes, output))
    if generation.kind != 'discovery' or set(generation.paths) != migration.COHORT:
        raise RuntimeError('discovery_fixed_complete_cohort_required')
    generation.verify(checkout)
    generation.authorize(review, root, hermes, None)
    authority = review.get('adoption_authority') or {}
    if (not str(authority.get('owner') or '').strip()
            or not str(authority.get('writer_exclusion') or '').strip()
            or authority.get('proposal_sha256') != json_sha(generation.proposal)):
        raise RuntimeError('discovery_separate_adoption_authority_required')
    wrapper = hermes / 'scripts' / WRAPPER
    previous = read_regular(wrapper)
    candidate = read_regular(checkout / SNAPSHOT)[0]
    dependencies = {'discovery': migration.REL,
                    'expansion': 'scripts/affiliate_supply_expansion.py',
                    'coverage': 'scripts/affiliate_monday_marketplace_receipt.py',
                    'card': 'scripts/affiliate_monday_discovery_card.py'}
    for name, path in dependencies.items():
        values = re.findall(rb"^expected_" + name.encode() + rb"_sha256='([0-9a-f]{64})'$",
                            candidate, re.MULTILINE)
        if values != [generation.hashes[path].encode()]:
            raise RuntimeError('discovery_wrapper_dependency_pin_mismatch:' + name)
    known = syncer.reviewed_prior_hashes(checkout, SNAPSHOT) | {_sha(candidate)}
    if _sha(previous[0]) not in known:
        raise RuntimeError('discovery_unknown_wrapper_predecessor')
    registry = hermes / 'cron/jobs.json'
    if (Path(scheduler._current_cron_store().jobs_file) != registry
            or Path(scheduler._jobs_lock_file()) != registry.parent / '.jobs.lock'):
        raise RuntimeError('discovery_wrong_scheduler_store')
    expected = generation.proposal['jobs'].get(JID)
    if (set(generation.proposal['jobs']) != {JID} or not expected
            or expected.get('no_agent') is not True or expected.get('script') != WRAPPER
            or expected.get('script_sha256') != _sha(previous[0])
            or any(expected.get(key) for key in ('running', 'fire_claim', 'run_claim'))):
        raise RuntimeError('discovery_exact_idle_native_job_required')
    journal = output / 'discovery-install.json'
    if journal.exists():
        raise RuntimeError('discovery_existing_journal_requires_reconciliation')
    output.mkdir(parents=True, exist_ok=True)
    review_path, authority_path = output / 'discovery-review.json', output / 'discovery-authority.json'
    atomic_json(review_path, review)
    atomic_json(authority_path, authority)
    paths = [*(root / path for path in generation.paths), wrapper]
    def drain():
        return process_readers(paths, excluded={os.getpid()})
    def current_job():
        rows = scheduler.load_jobs()
        found = [row for row in rows if row.get('id') == JID]
        if len(found) != 1 or found[0] != expected:
            raise RuntimeError('discovery_native_job_preimage_moved')
        return rows
    with scheduler._fire_job_lock(JID) as acquired:
        if not acquired:
            raise RuntimeError('discovery_native_fire_fence_unavailable')
        with native_registry_lock(scheduler):
            generation.authorize(review, root, hermes, None)
            current_job()
            if read_regular(wrapper) != previous or drain():
                raise RuntimeError('discovery_wrapper_or_readers_moved')
            baselines = json.loads(read_regular(root / syncer.STATE)[0])['hashes']
            # The retained discovery predecessor has its separate fixed exception.
            # Existing siblings without a baseline must still pass the ordinary
            # importer's committed-history admission; a review cannot bless edits.
            adopted = tuple(path for path in sorted(generation.paths)
                            if path != migration.REL and path not in baselines
                            and (root / path).exists())
            kwargs = dict(only=generation.paths, discovery_review=review_path,
                          discovery_authority=authority_path, ship_generation=generation,
                          adopt_reviewed_baseline=adopted)
            if syncer.sync(root, checkout, False, **kwargs) != 0:
                raise RuntimeError('discovery_source_preview_refused')
            atomic_json(journal, {'schema': SCHEMA, 'state': 'source_install_intent',
                                 'kind': 'glp-ship-discovery-install-v1',
                                 'proposal_sha256': json_sha(generation.proposal), 'job_before': expected})
            def commit_pin():
                rows = current_job()
                after = copy.deepcopy(rows)
                next(row for row in after if row.get('id') == JID)['script_sha256'] = _sha(candidate)
                scheduler._record_load_stamp(None)
                try:
                    scheduler._save_jobs_unlocked(after)
                finally:
                    scheduler._record_load_stamp(None)
                if json.loads(read_regular(registry)[0])['jobs'] != after:
                    raise RuntimeError('discovery_native_save_uncertain_no_retry')
            def publish():
                switch_files(journal, {wrapper: candidate}, {wrapper: previous}, drain, commit=commit_pin)
            try:
                if syncer.sync(root, checkout, True, _ship_publish=publish, **kwargs) != 0:
                    raise RuntimeError('discovery_source_apply_refused')
                if read_regular(wrapper)[0] != candidate:
                    raise RuntimeError('discovery_wrapper_final_readback_failed')
                data = read_journal(journal)
                data['state'] = 'applied'
                atomic_json(journal, data)
            except BaseException:
                data = read_journal(journal)
                changed = False
                for name, before in generation.proposal['before'].items():
                    if Path(name) == wrapper:
                        continue
                    try:
                        changed = changed or fingerprint(name) != before
                    except (OSError, RuntimeError):
                        changed = True  # Missing/unreadable is changed, never evidence of an unchanged source.
                seal_error = None
                switch = data.get('source_switch', {})
                if changed and (not switch or switch.get('state') == 'rolled_back'):
                    if switch:
                        data['precommit_wrapper_custody'] = switch
                        atomic_json(journal, data)
                    def retain_seal():
                        raise RuntimeError('discovery_source_partial_requires_reconciliation')
                    try:
                        switch_files(journal, {wrapper: candidate}, {wrapper: previous}, drain, commit=retain_seal)
                    except BaseException as exc:
                        seal_error = type(exc).__name__
                data = read_journal(journal)
                data['state'] = 'reconciliation_required'
                if seal_error is not None:
                    data['seal_outcome'] = data.get('source_switch', {}).get('state', 'unverified')
                    data['seal_error_type'] = seal_error
                atomic_json(journal, data)
                raise
    return {'status': 'applied', 'journal': str(journal), 'job_id': JID,
            'wrapper_sha256': _sha(candidate)}
