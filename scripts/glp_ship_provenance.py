"""Read-only Git/GitHub provenance for maintained shipping generations."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path


def _git(checkout: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ['git', '-C', str(checkout), *args], check=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError('ship_git_read_failed') from exc


def _sha(value: str) -> None:
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value):
        raise RuntimeError('ship_full_commit_sha_required')


def derive_pins(checkout: Path, merge_sha: str, paths) -> dict[str, str]:
    """Hash regular Git blobs only after comparing the exact installed source bytes."""
    _sha(merge_sha)
    checkout = Path(checkout).absolute()
    if any(path.is_symlink() for path in (checkout, *checkout.parents)):
        raise RuntimeError('ship_checkout_symlink')
    if _git(checkout, 'cat-file', '-t', merge_sha).strip() != b'commit':
        raise RuntimeError('ship_commit_object_required')
    pins = {}
    for rel in paths:
        if (not isinstance(rel, str) or not rel or rel.startswith('/')
                or any(part in ('', '.', '..') for part in rel.split('/'))
                or any(ord(char) < 32 for char in rel) or '\\' in rel
                or rel in pins):
            raise RuntimeError('ship_unsafe_source_path')
        listing = _git(checkout, 'ls-tree', '-z', merge_sha, '--', rel)
        rows = listing.split(b'\0')
        if len(rows) != 2 or rows[1]:
            raise RuntimeError('ship_source_not_single_blob')
        metadata, _, name = rows[0].partition(b'\t')
        fields = metadata.split()
        if (len(fields) != 3 or fields[0] not in (b'100644', b'100755')
                or fields[1] != b'blob' or name != rel.encode()):
            raise RuntimeError('ship_source_not_regular_blob')
        blob = _git(checkout, 'cat-file', 'blob', fields[2].decode())
        current = Path(checkout)
        for part in rel.split('/'):
            current = current / part
            if current.is_symlink():
                raise RuntimeError('ship_source_symlink')
        try:
            fd = os.open(current, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise RuntimeError('ship_source_not_regular_file')
                if stream.read() != blob:
                    raise RuntimeError('ship_source_bytes_differ')
        except OSError as exc:
            raise RuntimeError('ship_source_read_failed') from exc
        pins[rel] = hashlib.sha256(blob).hexdigest()
    return pins


REPOSITORY = 'izaiahat/glp-business'
PREFIX = f'repos/{REPOSITORY}/'
WORKFLOW = '.github/workflows/business-source-ci.yml'


def github_json(endpoint: str):
    if (not isinstance(endpoint, str) or not endpoint.startswith(PREFIX)
            or not re.fullmatch(r'[A-Za-z0-9_./?=&-]+', endpoint)
            or '..' in endpoint or '//' in endpoint):
        raise RuntimeError('ship_github_endpoint_invalid')
    try:
        result = subprocess.run(
            ['gh', 'api', '--hostname', 'github.com', '--method', 'GET', endpoint],
            check=True, capture_output=True, text=True, timeout=45,
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        # Never include provider output, which may contain credential diagnostics.
        raise RuntimeError('ship_github_read_failed') from None


def _pages(api, endpoint: str, key=None) -> list:
    rows = []
    separator = '&' if '?' in endpoint else '?'
    for page in range(1, 101):
        payload = api(f'{endpoint}{separator}per_page=100&page={page}')
        batch = payload[key] if key else payload
        if not isinstance(batch, list) or len(batch) > 100:
            raise RuntimeError('ship_github_page_invalid')
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise RuntimeError('ship_github_pagination_limit')


def _positive(value) -> bool:
    return type(value) is int and value > 0


def _associated(row, pr: dict, head: str, corroborated) -> bool:
    """Bind a workflow run to the exact PR, treating omitted association as unknown.

    GitHub can return ``pull_requests: []`` for a same-repository PR run (seen on
    PR166 run 36973008150). Empty never grants association by itself: it binds
    only when the run's own canonical head identity matches the PR and native
    commit->PR evidence names exactly this PR; otherwise it refuses, never skips.
    """
    links = row['pull_requests']
    if not isinstance(links, list):
        raise RuntimeError('ship_ci_association_invalid')
    if links:
        return any(link['number'] == pr['number'] and link['head']['sha'] == head
                   for link in links)
    if (row['head_sha'] != head or row['event'] != 'pull_request'
            or row['repository']['full_name'] != REPOSITORY
            or row['head_repository']['full_name'] != REPOSITORY
            or row['head_branch'] != pr['head']['ref'] or not corroborated()):
        raise RuntimeError('ship_ci_association_uncorroborated')
    return True


def verify_merge(checkout: Path, merge_sha: str, api=github_json) -> dict:
    """Prove merged main ancestry and the latest exact PR-head CI success.

    The caller refreshes canonical origin/main before this read-only check.
    This proves provenance, never runtime approval.
    """
    _sha(merge_sha)
    _git(checkout, 'merge-base', '--is-ancestor', merge_sha, 'refs/remotes/origin/main')
    try:
        associated = _pages(api, PREFIX + f'commits/{merge_sha}/pulls')
        candidates = []
        for item in associated:
            number = item['number']
            if not _positive(number):
                raise RuntimeError('ship_pr_number_invalid')
            pr = api(PREFIX + f'pulls/{number}')
            if (pr['number'] == number and pr['merged'] is True and pr['merged_at']
                    and pr['merge_commit_sha'] == merge_sha
                    and pr['base']['ref'] == 'main'
                    and pr['base']['repo']['full_name'] == REPOSITORY):
                _sha(pr['head']['sha'])
                candidates.append(pr)
        if len(candidates) != 1:
            raise RuntimeError('ship_exact_merged_pr_required')
        pr = candidates[0]
        head = pr['head']['sha']
        proof = []

        def corroborated() -> bool:
            # Read once, only when a run omits association: the exact head must
            # natively belong to exactly this merged canonical PR and no other.
            if not proof:
                rows = _pages(api, PREFIX + f'commits/{head}/pulls')
                proof.append(
                    len(rows) == 1 and rows[0]['number'] == pr['number']
                    and rows[0]['head']['sha'] == head
                    and rows[0]['head']['ref'] == pr['head']['ref']
                    and rows[0]['head']['repo']['full_name'] == REPOSITORY
                    and pr['head']['repo']['full_name'] == REPOSITORY
                    and rows[0]['base']['ref'] == 'main'
                    and rows[0]['base']['repo']['full_name'] == REPOSITORY
                    and rows[0]['merge_commit_sha'] == merge_sha
                    and bool(rows[0]['merged_at']) and rows[0]['merged_at'] == pr['merged_at'])
            return proof[0]
        runs = _pages(api, PREFIX + 'actions/workflows/business-source-ci.yml/runs'
                      f'?head_sha={head}&event=pull_request', 'workflow_runs')
        if not runs or any(not _positive(row['id']) or not _positive(row['run_number'])
                           or not _positive(row['run_attempt']) for row in runs):
            raise RuntimeError('ship_ci_runs_missing_or_invalid')
        latest = max(runs, key=lambda row: (row['run_number'], row['run_attempt'], row['id']))
        run = api(PREFIX + f'actions/runs/{latest["id"]}')
        if (run['id'] != latest['id'] or run['run_attempt'] < latest['run_attempt']
                or not _positive(run['run_attempt']) or run['head_sha'] != head
                or run['event'] != 'pull_request' or run['status'] != 'completed'
                or run['conclusion'] != 'success' or run['path'] != WORKFLOW
                or run['repository']['full_name'] != REPOSITORY
                or not _associated(run, pr, head, corroborated)):
            raise RuntimeError('ship_exact_head_ci_success_required')
        # A passing main workflow cannot hide another failing/pending workflow
        # on this same PR head. Group reruns by workflow, preserving latest only.
        all_runs = _pages(api, PREFIX + 'actions/runs'
                          f'?head_sha={head}&event=pull_request', 'workflow_runs')
        grouped = {}
        for candidate in all_runs:
            if not _associated(candidate, pr, head, corroborated):
                continue
            if any(not _positive(candidate[field]) for field in
                   ('workflow_id', 'id', 'run_number', 'run_attempt')):
                raise RuntimeError('ship_ci_runs_missing_or_invalid')
            workflow = candidate['workflow_id']
            order = (candidate['run_number'], candidate['run_attempt'], candidate['id'])
            previous = grouped.get(workflow)
            if previous is None or order > previous[0]:
                grouped[workflow] = (order, candidate)
        if not grouped or run['workflow_id'] not in grouped:
            raise RuntimeError('ship_associated_ci_missing')
        for _, candidate in grouped.values():
            current = api(PREFIX + f'actions/runs/{candidate["id"]}')
            if (current['id'] != candidate['id']
                    or current['workflow_id'] != candidate['workflow_id']
                    or not _positive(current['run_attempt'])
                    or current['run_attempt'] < candidate['run_attempt']
                    or current['head_sha'] != head or current['event'] != 'pull_request'
                    or current['repository']['full_name'] != REPOSITORY
                    or current['status'] != 'completed' or current['conclusion'] != 'success'
                    or not _associated(current, pr, head, corroborated)):
                raise RuntimeError('ship_all_exact_head_ci_success_required')
            if current['workflow_id'] == run['workflow_id']:
                if (current['path'] != WORKFLOW
                        or current['run_number'] < run['run_number']
                        or (current['id'] == run['id']
                            and current['run_attempt'] < run['run_attempt'])):
                    raise RuntimeError('ship_ci_evidence_regressed')
                run = current
        return {'merge_sha': merge_sha, 'pr_number': pr['number'], 'pr_head': head,
                'ci_run_id': run['id'], 'ci_attempt': run['run_attempt']}
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise RuntimeError('ship_github_proof_invalid') from exc
