#!/usr/bin/env python3
"""Review-gated, all-source Awin pin release transaction.

This is intentionally not a self-healer. A test pass is not review authority and
no scheduled job calls this tool. Every drifted payload must appear at its exact
current SHA-256 in an explicit ``awin-pin-review-v1`` receipt. The transaction
then updates every Python/shell pin carrying those bytes, walks generated parent
and wrapper hashes outward, updates the three scheduler-enforced Awin
registrations through Hermes' registry API, archives accepted bytes, and proves
the complete release clean with ``pin_drift_report``.

It performs no provider calls, application submissions, browser actions, Sheet
writes, sends, spend, or secret reads. Paths below ``~/.hermes/secrets`` are
categorically refused and never archived.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pin_drift_report as report

BUSINESS = Path("/home/ubuntu/business")
PARENT = BUSINESS / "scripts/glp_awin_portal_drain_cron.py"
HERMES_SCRIPTS = Path("/home/ubuntu/.hermes/scripts")
CRON_REGISTRY = Path("/home/ubuntu/.hermes/cron/jobs.json")
ARCHIVE_ROOT = BUSINESS / "reports/ops/pin-archive"
SECRET_ROOT = Path("/home/ubuntu/.hermes/secrets")
LOCK = Path("/home/ubuntu/.hermes/state/awin-pin-release.lock")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REVIEW_BASES = frozenset({
    "authorized_change_receipt",
    "full_current_baseline_review",
    "architecture_change_review",
    "current_wrapper_review",
})


@dataclass(frozen=True)
class PinSite:
    source: Path
    kind: str
    key: str
    target: Path


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_secret(path: Path) -> bool:
    try:
        path.resolve().relative_to(SECRET_ROOT.resolve())
        return True
    except ValueError:
        return False


def _atomic_write(path: Path, value: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def load_review(path: Path) -> tuple[dict[str, Any], dict[Path, dict[str, Any]]]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != "awin-pin-review-v1":
        raise RuntimeError("review_schema_invalid")
    if document.get("decision") != "ACCEPT_EXACT_CURRENT_BYTES":
        raise RuntimeError("review_decision_invalid")
    if not str(document.get("reviewer") or "").strip():
        raise RuntimeError("reviewer_missing")
    try:
        reviewed_at = dt.datetime.strptime(
            str(document.get("reviewed_at_utc") or ""), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=dt.timezone.utc)
    except ValueError as exc:
        raise RuntimeError("review_timestamp_invalid") from exc
    if reviewed_at > dt.datetime.now(dt.timezone.utc):
        raise RuntimeError("review_timestamp_in_future")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("review_entries_missing")
    approved: dict[Path, dict[str, Any]] = {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RuntimeError(f"review_entry_invalid:{index}")
        candidate = Path(str(entry.get("path") or ""))
        digest = str(entry.get("reviewed_sha256") or "")
        basis = str(entry.get("review_basis") or "")
        evidence = entry.get("evidence")
        if (not candidate.is_absolute() or not candidate.is_file()
                or _is_secret(candidate) or not HEX64.fullmatch(digest)
                or basis not in REVIEW_BASES
                or not isinstance(evidence, list) or not evidence
                or any(not str(value).strip() for value in evidence)):
            raise RuntimeError(f"review_entry_contract_invalid:{index}:{candidate}")
        candidate = candidate.resolve()
        if candidate in approved:
            raise RuntimeError(f"review_entry_duplicate:{candidate}")
        actual = sha_bytes(candidate.read_bytes())
        if actual != digest:
            raise RuntimeError(f"reviewed_bytes_moved:{candidate}:{digest}:{actual}")
        approved[candidate] = entry
    return document, approved


def discover_file_pin_sites() -> list[PinSite]:
    sites: list[PinSite] = []
    for module, table in report.PYTHON_PIN_SOURCES:
        if not module.is_file():
            continue
        for _source, target, _expected in report.python_pins(module, table):
            sites.append(PinSite(module.resolve(), "python", table, target.resolve()))
    if report.HERMES_SCRIPTS.is_dir():
        for script in sorted(report.HERMES_SCRIPTS.glob("*.sh")):
            for source, target, _expected in report.shell_pins(script):
                # Keep the reporter's exact assignment name, including case/suffix.
                variable = source.rsplit(":", 1)[1]
                sites.append(PinSite(script.resolve(), "shell", variable, target.resolve()))
    return sites


def _replace_python_pin(text: str, site: PinSite, digest: str) -> str:
    relative = site.target.relative_to(report.BUSINESS).as_posix()
    pattern = re.compile(
        rf'^(\s*BUSINESS\s*/\s*"{re.escape(relative)}"\s*:\s*")([0-9a-f]{{64}})(",?\s*)$',
        re.MULTILINE,
    )
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise RuntimeError(f"python_pin_match_count:{site.source}:{relative}:{len(matches)}")
    return pattern.sub(rf"\g<1>{digest}\g<3>", text, count=1)


def _replace_shell_pin(text: str, site: PinSite, digest: str) -> str:
    pattern = re.compile(
        rf"^([ \t]*(?:readonly[ \t]+)?{re.escape(site.key)}=)([\"']?)"
        rf"[0-9a-f]{{64}}(\2[ \t]*)$",
        re.MULTILINE,
    )
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise RuntimeError(f"shell_pin_match_count:{site.source}:{site.key}:{len(matches)}")
    return pattern.sub(rf"\g<1>\g<2>{digest}\g<3>", text, count=1)


def build_staged(approved: set[Path], sites: list[PinSite]) -> tuple[dict[Path, bytes], list[dict[str, str]]]:
    staged: dict[Path, bytes] = {}
    updates: list[dict[str, str]] = []
    queue = sorted(approved, key=os.fspath)
    pending = set(queue)
    while queue:
        target = queue.pop(0)
        pending.discard(target)
        target_bytes = staged.get(target, target.read_bytes())
        digest = sha_bytes(target_bytes)
        for site in (candidate for candidate in sites if candidate.target == target):
            before = staged.get(site.source, site.source.read_bytes())
            text = before.decode("utf-8")
            after_text = (_replace_python_pin(text, site, digest)
                          if site.kind == "python" else _replace_shell_pin(text, site, digest))
            after = after_text.encode("utf-8")
            if after == before:
                continue
            staged[site.source] = after
            updates.append({
                "target": str(target), "pin_source": str(site.source),
                "kind": site.kind, "sha256": digest,
            })
            if site.source not in pending:
                queue.append(site.source)
                pending.add(site.source)
    for path, value in staged.items():
        if path.suffix == ".py":
            compile(value.decode("utf-8"), str(path), "exec")
        elif path.suffix in {".sh", ".bash"}:
            with tempfile.NamedTemporaryFile(suffix=path.suffix) as handle:
                handle.write(value)
                handle.flush()
                checked = subprocess.run(
                    ["/usr/bin/bash", "-n", handle.name], capture_output=True,
                    text=True, check=False, timeout=30)
            if checked.returncode:
                raise RuntimeError(f"staged_shell_invalid:{path}:{checked.stderr[-300:]}")
    return staged, updates


def archive_bytes(path: Path, value: bytes) -> Path:
    if _is_secret(path):
        raise RuntimeError(f"secret_archive_refused:{path}")
    digest = sha_bytes(value)
    destination = report.archive_path(digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if destination.read_bytes() != value:
            raise RuntimeError(f"pin_archive_collision_or_corruption:{destination}")
        return destination
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


class JsonRegistry:
    def __init__(self, path: Path):
        self.path = path

    def jobs(self) -> list[dict[str, Any]]:
        return list(json.loads(self.path.read_text(encoding="utf-8"))["jobs"])

    def paused_jobs(self) -> list[dict[str, Any]]:
        return self.jobs()

    def update_hash(self, job_id: str, digest: str) -> None:
        document = json.loads(self.path.read_text(encoding="utf-8"))
        hits = [job for job in document["jobs"] if str(job.get("id")) == job_id]
        if len(hits) != 1:
            raise RuntimeError(f"registry_job_match_count:{job_id}:{len(hits)}")
        hits[0]["script_sha256"] = digest
        _atomic_write(
            self.path, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(),
            self.path.stat().st_mode & 0o777)


    def update_paused_hashes(self, expected: list[dict[str, Any]], digests: dict[str, str]) -> None:
        # Fixture adapter only. Production uses the scheduler-owned lock and saver.
        if self.path.resolve() == CRON_REGISTRY.resolve():
            raise RuntimeError("fixture_adapter_live_registry_refused")
        import fcntl
        with self.path.with_suffix(".lock").open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            document = json.loads(self.path.read_text(encoding="utf-8"))
            document["jobs"] = _paused_hash_candidates(document["jobs"], expected, digests)
            _atomic_write(self.path, (json.dumps(document, indent=2) + "\n").encode(),
                          self.path.stat().st_mode & 0o777)


class HermesRegistry:
    def __init__(self):
        sys.path.insert(0, "/home/ubuntu/.hermes/hermes-agent")
        from cron.jobs import load_jobs, update_job
        self._load_jobs = load_jobs
        self._update_job = update_job

    def jobs(self) -> list[dict[str, Any]]:
        return list(self._load_jobs())

    def paused_jobs(self) -> list[dict[str, Any]]:
        from cron import jobs as scheduler
        if scheduler._current_cron_store().jobs_file.resolve() != CRON_REGISTRY.resolve():
            raise RuntimeError("paused_registration_wrong_profile")
        # Atomic-file snapshot only; no tolerant loader / auto-repair side effects.
        return list(json.loads(CRON_REGISTRY.read_text(encoding="utf-8"))["jobs"])

    def update_hash(self, job_id: str, digest: str) -> None:
        updated = self._update_job(job_id, {"script_sha256": digest})
        if updated is None:
            raise RuntimeError(f"registry_job_missing:{job_id}")


    def update_paused_hashes(self, expected: list[dict[str, Any]], digests: dict[str, str]) -> None:
        from cron import jobs as scheduler
        # update_job normalizes lifecycle fields; use its maintained locked saver
        # for an all-or-nothing hash-only transaction without that normalization.
        if scheduler._current_cron_store().jobs_file.resolve() != CRON_REGISTRY.resolve():
            raise RuntimeError("paused_registration_wrong_profile")
        import fcntl
        # The scheduler's generic context degrades on flock timeout. This
        # preparation must refuse instead, while sharing its exact lock path.
        with scheduler._jobs_file_lock:
            with scheduler._jobs_lock_file().open("a+") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                scheduler._record_load_stamp(None)
                try:
                    jobs = scheduler._peek_jobs_unlocked()
                    if jobs is None:
                        raise RuntimeError("paused_registration_store_invalid")
                    candidate = _paused_hash_candidates(jobs, expected, digests)
                    scheduler._save_jobs_unlocked(candidate)
                    if scheduler._peek_jobs_unlocked() != candidate:
                        raise RuntimeError("paused_registration_readback_mismatch_no_retry")
                finally:
                    scheduler._record_load_stamp(None)


# PR28 scope hold: 52e2014a9579 and 5a2cba16e057 are retired daily
# application jobs, never eligible for paused-registration proposals or CAS.
# This retained legacy preflight mapping is not adoption authority: the canonical
# fresh-preflight candidate's identity-resolution and migration holds remain.
PAUSED_WRAPPERS = {
    "7a269c665b12": "awin_auth_liveness_preflight.sh",
}


def _paused_hash_candidates(jobs: list[dict[str, Any]], expected: list[dict[str, Any]],
                            digests: dict[str, str]) -> list[dict[str, Any]]:
    """Validate the entire compare-and-swap before changing any record."""
    import copy
    selected = [job for job in jobs if str(job.get("id")) in PAUSED_WRAPPERS]
    if (len(selected) != len(PAUSED_WRAPPERS)
            or {str(job.get("id")) for job in selected} != set(PAUSED_WRAPPERS)
            or set(digests) != set(PAUSED_WRAPPERS)):
        raise RuntimeError("paused_registration_set_invalid")
    if sorted(selected, key=lambda j: j["id"]) != sorted(expected, key=lambda j: j["id"]):
        raise RuntimeError("paused_registration_preimage_changed")
    for job in selected:
        job_id = str(job["id"])
        wrapper = report.HERMES_SCRIPTS / PAUSED_WRAPPERS[job_id]
        raw = Path(str(job.get("script") or ""))
        registered = raw if raw.is_absolute() else report.HERMES_SCRIPTS / raw
        if (job.get("enabled") is not False or job.get("state") != "paused"
                or registered != wrapper or wrapper.is_symlink()
                or not HEX64.fullmatch(digests[job_id])
                or sha_bytes(wrapper.read_bytes()) != digests[job_id]):
            raise RuntimeError(f"paused_registration_contract_changed:{job_id}")
    result = copy.deepcopy(jobs)
    for job in result:
        if str(job.get("id")) in digests:
            job["script_sha256"] = digests[str(job["id"])]
    return result


def release_paused_registrations(review_path: Path, registry: Any, *,
                                 apply: bool) -> tuple[int, dict[str, Any]]:
    """Explicit exact-wrapper review; no source staging, activation or scheduling."""
    _review, approved = load_review(review_path)
    wrappers = {job_id: report.HERMES_SCRIPTS / name for job_id, name in PAUSED_WRAPPERS.items()}
    if (set(approved) != set(wrappers.values())
            or any(entry["review_basis"] != "current_wrapper_review" for entry in approved.values())):
        raise RuntimeError("paused_review_must_name_exact_eligible_wrappers")
    jobs = registry.paused_jobs()
    expected = [job for job in jobs if str(job.get("id")) in PAUSED_WRAPPERS]
    digests = {job_id: approved[path]["reviewed_sha256"] for job_id, path in wrappers.items()}
    _paused_hash_candidates(jobs, expected, digests)
    updates = [{"job_id": job["id"], "script": str(wrappers[job["id"]]),
                "script_sha256": digests[job["id"]], "previous_sha256": job.get("script_sha256")}
               for job in expected if job.get("script_sha256") != digests[job["id"]]]
    result = {"schema": "awin-paused-registration-release-v1", "applied": False,
              "review_receipt_sha256": sha_bytes(review_path.read_bytes()),
              "registry_updates": updates, "pause_state_preserved": True,
              "activation_ready": False, "fresh_browser_migration_hold": True}
    if apply and updates:
        registry.update_paused_hashes(expected, digests)
    result["applied"] = apply
    return 0, result


def _cron_paths(registry: Any) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for job in registry.jobs():
        job_id = str(job.get("id") or "")
        if job_id not in report.AWIN_CRON_JOB_IDS or not job.get("enabled"):
            continue
        raw = Path(str(job.get("script") or ""))
        out[job_id] = (raw if raw.is_absolute() else report.HERMES_SCRIPTS / raw.name).resolve()
    return out


def release(review_path: Path, registry: Any, *, registry_path: Path,
            apply: bool) -> tuple[int, dict[str, Any]]:
    review, approved = load_review(review_path)
    findings = report.scan(
        include_shell=True, include_registry=True, registry=registry_path, scope="awin")
    unreviewed = [item for item in findings if Path(item["path"]).resolve() not in approved]
    result: dict[str, Any] = {
        "schema": "awin-pin-release-v1",
        "captured_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "BLOCKED" if unreviewed else "PASS",
        "applied": False,
        "review_receipt": str(review_path),
        "review_receipt_sha256": sha_bytes(review_path.read_bytes()),
        "reviewed_paths": sorted(map(str, approved)),
        "initial_findings": findings,
        "unreviewed_findings": unreviewed,
        "pin_updates": [],
        "registry_updates": [],
        "effects": {
            "provider_calls": 0, "provider_writes": 0, "application_submissions": 0,
            "browser_actions": 0, "sheet_writes": 0, "outbound_sends": 0, "spend_usd": 0,
        },
    }
    if unreviewed:
        return 2, result

    sites = discover_file_pin_sites()
    staged, updates = build_staged(set(approved), sites)
    result["pin_updates"] = updates
    result["staged_files"] = sorted(map(str, staged))
    cron_paths = _cron_paths(registry)
    for job_id, wrapper in cron_paths.items():
        if wrapper not in approved and wrapper not in staged:
            current = next(job for job in registry.jobs() if str(job.get("id")) == job_id)
            if str(current.get("script_sha256") or "") != sha_bytes(wrapper.read_bytes()):
                result["status"] = "BLOCKED"
                result["unreviewed_findings"].append({
                    "source": f"jobs.json:{job_id}.script_sha256", "path": str(wrapper),
                    "state": "DRIFT", "reason": "wrapper_not_in_review_or_generated_closure",
                })
    if result["status"] == "BLOCKED":
        return 2, result
    if not apply:
        result["dry_run"] = True
        result["proposed_hashes"] = {
            str(path): sha_bytes(value) for path, value in sorted(staged.items(), key=lambda item: os.fspath(item[0]))
        }
        return 0, result

    originals = {path: path.read_bytes() for path in staged}
    modes = {path: path.stat().st_mode & 0o777 for path in staged}
    registry_before = {
        str(job.get("id")): str(job.get("script_sha256") or "")
        for job in registry.jobs() if str(job.get("id") or "") in report.AWIN_CRON_JOB_IDS
    }
    archived: dict[str, str] = {}
    try:
        for path in approved:
            archived[sha_bytes(path.read_bytes())] = str(archive_bytes(path, path.read_bytes()))
        for path, value in staged.items():
            archived[sha_bytes(value)] = str(archive_bytes(path, value))
        for path, value in staged.items():
            if path.read_bytes() != originals[path]:
                raise RuntimeError(f"preimage_changed_before_apply:{path}")
            _atomic_write(path, value, modes[path])
        for job_id, wrapper in cron_paths.items():
            digest = sha_bytes(wrapper.read_bytes())
            current = next(job for job in registry.jobs() if str(job.get("id")) == job_id)
            if str(current.get("script_sha256") or "") != digest:
                registry.update_hash(job_id, digest)
                result["registry_updates"].append({
                    "job_id": job_id, "script": str(wrapper), "script_sha256": digest,
                })
        remaining = report.scan(
            include_shell=True, include_registry=True, registry=registry_path, scope="awin")
        if remaining:
            raise RuntimeError(f"drift_after_release:{remaining}")
        for _source, path, digest in report.discover_pins(
                include_shell=True, include_registry=True, registry=registry_path,
                scope="awin"):
            if path.is_file() and not _is_secret(path):
                body = path.read_bytes()
                if sha_bytes(body) == digest:
                    archived[digest] = str(archive_bytes(path, body))
    except Exception:
        for path, value in originals.items():
            _atomic_write(path, value, modes[path])
        for job_id, digest in registry_before.items():
            current = next((job for job in registry.jobs()
                            if str(job.get("id")) == job_id), None)
            if current is not None and str(current.get("script_sha256") or "") != digest:
                registry.update_hash(job_id, digest)
        raise

    result["applied"] = True
    result["archives"] = archived
    result["final_hashes"] = {
        str(path): sha_bytes(path.read_bytes())
        for path in sorted(set(approved) | set(staged), key=os.fspath)
    }
    acceptance_root = ARCHIVE_ROOT / "acceptances"
    acceptance_root.mkdir(parents=True, exist_ok=True)
    stamp = result["captured_at_utc"].replace(":", "")
    acceptance = acceptance_root / f"awin-{stamp}-{result['review_receipt_sha256'][:16]}.json"
    body = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(acceptance, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    result["acceptance_receipt"] = str(acceptance)
    result["acceptance_receipt_sha256"] = sha_bytes(body)
    return 0, result


PACKAGE_BINARIES = {
    Path('/usr/bin/python3.12'): 'python3.12-minimal',
    **{Path('/usr/bin/' + name): 'coreutils'
       for name in ('sha256sum', 'readlink', 'date', 'chmod')},
    Path('/usr/bin/flock'): 'util-linux',
}


def release_package_binaries(review_path: Path, registry: Any, *,
                             registry_path: Path, apply: bool) -> tuple[int, dict[str, Any]]:
    """Narrow Awin release: verified OS binaries only; source drifts remain pinned.

    Requires a six-entry exact-byte review, checks installed dpkg manifests, and
    updates only the generated wrapper closure. It never admits application
    source changes under a package-upgrade receipt.
    """
    review, approved = load_review(review_path)
    if set(approved) != set(PACKAGE_BINARIES):
        raise RuntimeError('package_binary_review_must_name_exact_six')
    if any(item.get('review_basis') != 'full_current_baseline_review'
           for item in approved.values()):
        raise RuntimeError('package_binary_review_basis_invalid')
    verified = {}
    for target, package in PACKAGE_BINARIES.items():
        listed = subprocess.check_output(['dpkg-query', '-W', '-f=${Version}', package], text=True).strip()
        manifest = Path('/var/lib/dpkg/info') / (package + '.md5sums')
        entries = dict(line.split(None, 1)[::-1] for line in manifest.read_text().splitlines())
        actual_md5 = hashlib.md5(target.read_bytes()).hexdigest()
        if entries.get(target.as_posix().lstrip('/')) != actual_md5:
            raise RuntimeError(f'package_payload_mismatch:{target}')
        verified[str(target)] = {'package': package, 'version': listed,
                                 'dpkg_md5': actual_md5, 'sha256': sha_bytes(target.read_bytes())}
    before = report.scan(include_shell=True, include_registry=True,
                         registry=registry_path, scope='awin')
    binary_findings = [f for f in before if Path(f['path']).resolve() in approved]
    if {Path(f['path']).resolve() for f in binary_findings} != set(approved):
        raise RuntimeError('package_binary_finding_set_changed')
    staged, updates = build_staged(set(approved), discover_file_pin_sites())
    if not staged or set(staged) != {HERMES_SCRIPTS / 'affiliate_portal_apply_daily.sh'}:
        raise RuntimeError(f'unexpected_package_pin_closure:{list(map(str, staged))}')
    wrapper = next(iter(staged))
    affected = [j for j in registry.jobs()
                if str(j.get('id')) in report.AWIN_CRON_JOB_IDS
                and Path(str(j.get('script') or '')).name == wrapper.name]
    if len(affected) != 1 or affected[0].get('enabled') is not False:
        raise RuntimeError(f'paused_package_wrapper_registration_count:{len(affected)}')
    job_id = str(affected[0]['id'])
    previous = wrapper.read_bytes()
    prior_job = next(j for j in registry.jobs() if str(j.get('id')) == job_id)
    old_registration = str(prior_job.get('script_sha256') or '')
    result = {'schema': 'awin-package-binary-repin-v1', 'applied': False,
              'review_receipt': str(review_path), 'verified_packages': verified,
              'pin_updates': updates, 'wrapper_before_sha256': sha_bytes(previous),
              'wrapper_after_sha256': sha_bytes(staged[wrapper]), 'job_id': job_id,
              'registration_before': old_registration,
              'other_awin_findings_before': [f for f in before if f not in binary_findings]}
    if not apply:
        return 0, result
    mode = wrapper.stat().st_mode & 0o777
    backup = review_path.parent / ('affiliate_portal_apply_daily.sh.' + sha_bytes(previous)[:16] + '.bak')
    if backup.exists() and backup.read_bytes() != previous:
        raise RuntimeError('wrapper_backup_collision')
    if not backup.exists():
        _atomic_write(backup, previous, 0o600)
    archive_bytes(wrapper, previous)
    for target in approved:
        archive_bytes(target, target.read_bytes())
    archive_bytes(wrapper, staged[wrapper])
    try:
        if wrapper.read_bytes() != previous:
            raise RuntimeError('wrapper_moved_before_apply')
        _atomic_write(wrapper, staged[wrapper], mode)
        registry.update_hash(job_id, sha_bytes(staged[wrapper]))
        remaining = report.scan(include_shell=True, include_registry=True,
                                registry=registry_path, scope='awin')
        if any(Path(f['path']).resolve() in approved or f['path'] == str(wrapper)
               for f in remaining):
            raise RuntimeError('binary_or_wrapper_drift_after_repin')
        expected_other = {(f['source'], f['path'], f['state']) for f in before
                          if f not in binary_findings and f['path'] != str(wrapper)}
        actual_other = {(f['source'], f['path'], f['state']) for f in remaining
                        if f['path'] != str(wrapper)}
        if expected_other != actual_other:
            raise RuntimeError('unreviewed_awin_findings_changed')
    except Exception:
        _atomic_write(wrapper, previous, mode)
        registry.update_hash(job_id, old_registration)
        raise
    result.update(applied=True, wrapper_backup=str(backup),
                  registration_after=sha_bytes(staged[wrapper]),
                  other_awin_findings_after=remaining)
    return 0, result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-receipt", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--registry-json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--package-binaries-only", action="store_true")
    modes.add_argument("--paused-registrations-only", action="store_true",
                       help="Exact eligible-wrapper review; excludes retired daily jobs, never unpauses")
    args = parser.parse_args()
    registry_path = Path(args.registry_json) if args.registry_json else CRON_REGISTRY
    registry = JsonRegistry(registry_path) if args.registry_json else HermesRegistry()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        os.chmod(LOCK, 0o600)
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX)
        if args.paused_registrations_only:
            code, result = release_paused_registrations(
                Path(args.review_receipt), registry, apply=args.apply)
        else:
            operation = release_package_binaries if args.package_binaries_only else release
            code, result = operation(
                Path(args.review_receipt), registry, registry_path=registry_path,
                apply=args.apply)
    print(json.dumps(result, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
