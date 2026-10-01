#!/usr/bin/env python3
"""Review-gated transitive release transaction for classified-live fleet pins.

This is not a self-healer. Exact current bytes must be accepted in a
``fleet-pin-review-v1`` receipt before any mismatching pin can move. The
transaction updates every discovered file co-pin, walks generated parent hashes
outward to convergence, updates every enabled scheduler registration through the
Hermes registry API, archives accepted non-secret bytes content-addressably, and
requires a clean live fleet scan before commit.

Historical-record pins are deliberately outside this transaction. It performs no
provider calls, applications, publishes, deploys, Sheet writes, sends, or spend.
"""
from __future__ import annotations

import argparse
import ast
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
CRON_REGISTRY = Path("/home/ubuntu/.hermes/cron/jobs.json")
ARCHIVE_ROOT = BUSINESS / "reports/ops/pin-archive"
SECRET_ROOT = Path("/home/ubuntu/.hermes/secrets")
LOCK = Path("/home/ubuntu/.hermes/state/fleet-pin-release.lock")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REVIEW_BASES = frozenset({
    "authorized_change_receipt",
    "full_current_baseline_review",
    "architecture_change_review",
    "current_wrapper_review",
})


@dataclass(frozen=True)
class PinSite:
    source: str
    source_path: Path
    target: Path
    kind: str
    key: str


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
    if document.get("schema") != "fleet-pin-review-v1":
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
        if (
            not candidate.is_absolute()
            or not candidate.is_file()
            or _is_secret(candidate)
            or not HEX64.fullmatch(digest)
            or basis not in REVIEW_BASES
            or not isinstance(evidence, list)
            or not evidence
            or any(not str(value).strip() for value in evidence)
        ):
            raise RuntimeError(f"review_entry_contract_invalid:{index}:{candidate}")
        candidate = candidate.resolve()
        if candidate in approved:
            raise RuntimeError(f"review_entry_duplicate:{candidate}")
        actual = sha_bytes(candidate.read_bytes())
        if actual != digest:
            raise RuntimeError(f"reviewed_bytes_moved:{candidate}:{digest}:{actual}")
        approved[candidate] = entry
    return document, approved


def _python_candidates(name: str) -> list[Path]:
    candidates: list[Path] = []
    for root in (*report.PYTHON_PIN_ROOTS, *report.PYTHON_SCALAR_PIN_ROOTS):
        if not root.is_dir():
            continue
        candidates.extend(path.resolve() for path in root.rglob(name) if path.is_file())
    return sorted(set(candidates), key=os.fspath)


def _source_site(source: str, target: Path, expected: str) -> PinSite:
    filename, key = source.split(":", 1)
    if filename.endswith((".sh", ".bash")):
        source_path = (report.HERMES_SCRIPTS / filename).resolve()
        kind = "shell_assoc" if key.startswith("associative_pin[") else "shell_scalar"
        return PinSite(source, source_path, target.resolve(), kind, key)
    if not filename.endswith(".py"):
        raise RuntimeError(f"pin_source_kind_unsupported:{source}")
    matches: list[tuple[Path, str]] = []
    for module in _python_candidates(filename):
        if module.parent == report.HERMES_SCRIPTS.resolve() or any(
            module.is_relative_to(root.resolve()) for root in report.PYTHON_SCALAR_PIN_ROOTS
            if root.exists()
        ):
            scalar, _issues = report._python_scalar_inventory(module)
            if (source, target, expected) in scalar:
                matches.append((module, "python_scalar"))
        if any(
            module.is_relative_to(root.resolve()) for root in report.PYTHON_PIN_ROOTS
            if root.exists()
        ):
            table, _issues = report._python_module_inventory(module)
            if (source, target, expected) in table:
                matches.append((module, "python_dict"))
    matches = sorted(set(matches), key=lambda row: (os.fspath(row[0]), row[1]))
    if len(matches) != 1:
        raise RuntimeError(f"pin_source_path_match_count:{source}:{target}:{len(matches)}")
    return PinSite(source, matches[0][0], target.resolve(), matches[0][1], key)


def discover_file_pin_sites() -> list[PinSite]:
    sites: list[PinSite] = []
    for source, target, expected in report.discover_pins(
        include_shell=True, include_registry=False, scope="fleet"
    ):
        sites.append(_source_site(source, target, expected))
    return sites


def _offset(text: str, line: int, column: int) -> int:
    rows = text.splitlines(keepends=True)
    return sum(len(row) for row in rows[: line - 1]) + column


def _replace_python_pin(text: str, site: PinSite, digest: str) -> str:
    tree = ast.parse(text)
    environment = report._path_environment(tree, site.source_path)
    assignment = next(
        (
            node for node in tree.body
            if report._assignment_name(node) == site.key
        ),
        None,
    )
    if assignment is None:
        raise RuntimeError(f"python_pin_assignment_missing:{site.source_path}:{site.key}")
    value = getattr(assignment, "value", None)
    constant: ast.Constant | None = None
    if site.kind == "python_scalar":
        if isinstance(value, ast.Constant) and isinstance(value.value, str) and HEX64.fullmatch(value.value):
            constant = value
    elif isinstance(value, ast.Dict):
        hits: list[ast.Constant] = []
        for raw_path, raw_digest in zip(value.keys, value.values):
            path = (
                report._literal_path(
                    raw_path, module=site.source_path, environment=environment
                )
                if raw_path is not None
                else None
            )
            if (
                path is not None
                and path.resolve() == site.target
                and isinstance(raw_digest, ast.Constant)
                and isinstance(raw_digest.value, str)
                and HEX64.fullmatch(raw_digest.value)
            ):
                hits.append(raw_digest)
        if len(hits) == 1:
            constant = hits[0]
    if constant is None or constant.lineno is None or constant.end_lineno is None:
        raise RuntimeError(f"python_pin_value_match_count:{site.source_path}:{site.key}")
    start = _offset(text, constant.lineno, constant.col_offset)
    end = _offset(text, constant.end_lineno, constant.end_col_offset)
    current = text[start:end]
    if not re.fullmatch(r"['\"][0-9a-f]{64}['\"]", current):
        raise RuntimeError(f"python_pin_literal_invalid:{site.source_path}:{site.key}")
    if current[1:-1] == digest:
        return text
    return text[:start] + current[0] + digest + current[-1] + text[end:]


def _replace_shell_pin(text: str, site: PinSite, digest: str) -> str:
    if site.kind == "shell_assoc":
        raw_target = site.key.removeprefix("associative_pin[").removesuffix("]")
        pattern = re.compile(
            rf"^(\s*\[['\"]{re.escape(raw_target)}['\"]\]\s*=\s*['\"])([0-9a-f]{{64}})(['\"]\s*)$",
            re.MULTILINE,
        )
    else:
        pattern = re.compile(
            rf"^(\s*(?:readonly\s+)?{re.escape(site.key)}\s*=\s*['\"]?)([0-9a-f]{{64}})(['\"]?\s*)$",
            re.MULTILINE,
        )
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise RuntimeError(f"shell_pin_match_count:{site.source_path}:{site.key}:{len(matches)}")
    if matches[0][1] == digest:
        return text
    return pattern.sub(rf"\g<1>{digest}\g<3>", text, count=1)


def build_staged(sites: list[PinSite]) -> tuple[dict[Path, bytes], list[dict[str, str]]]:
    staged: dict[Path, bytes] = {}
    updates: list[dict[str, str]] = []
    for _pass in range(max(8, len(sites) + 2)):
        changed = False
        for site in sites:
            target_bytes = staged.get(site.target, site.target.read_bytes())
            digest = sha_bytes(target_bytes)
            before = staged.get(site.source_path, site.source_path.read_bytes())
            text = before.decode("utf-8")
            after_text = (
                _replace_python_pin(text, site, digest)
                if site.kind.startswith("python_")
                else _replace_shell_pin(text, site, digest)
            )
            after = after_text.encode("utf-8")
            if after == before:
                continue
            staged[site.source_path] = after
            updates.append({
                "target": str(site.target),
                "pin_source": str(site.source_path),
                "source": site.source,
                "kind": site.kind,
                "sha256": digest,
            })
            changed = True
        if not changed:
            break
    else:
        raise RuntimeError("pin_graph_did_not_converge")
    for path, value in staged.items():
        if path.suffix == ".py":
            compile(value.decode("utf-8"), str(path), "exec")
        elif path.suffix in {".sh", ".bash"}:
            with tempfile.NamedTemporaryFile(suffix=path.suffix) as handle:
                handle.write(value)
                handle.flush()
                checked = subprocess.run(
                    ["/usr/bin/bash", "-n", handle.name],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
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

    def update_hash(self, job_id: str, digest: str) -> None:
        document = json.loads(self.path.read_text(encoding="utf-8"))
        hits = [job for job in document["jobs"] if str(job.get("id")) == job_id]
        if len(hits) != 1:
            raise RuntimeError(f"registry_job_match_count:{job_id}:{len(hits)}")
        hits[0]["script_sha256"] = digest
        _atomic_write(
            self.path,
            (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(),
            self.path.stat().st_mode & 0o777,
        )


class HermesRegistry:
    def __init__(self):
        sys.path.insert(0, "/home/ubuntu/.hermes/hermes-agent")
        from cron.jobs import load_jobs, update_job

        self._load_jobs = load_jobs
        self._update_job = update_job

    def jobs(self) -> list[dict[str, Any]]:
        return list(self._load_jobs())

    def update_hash(self, job_id: str, digest: str) -> None:
        updated = self._update_job(job_id, {"script_sha256": digest})
        if updated is None:
            raise RuntimeError(f"registry_job_missing:{job_id}")


def _cron_path(job: dict[str, Any]) -> Path:
    raw = Path(str(job.get("script") or ""))
    return (raw if raw.is_absolute() else report.HERMES_SCRIPTS / raw.name).resolve()


def release(
    review_path: Path,
    registry: Any,
    *,
    registry_path: Path,
    apply: bool,
) -> tuple[int, dict[str, Any]]:
    _review, approved = load_review(review_path)
    findings = report.scan(
        include_shell=True,
        include_registry=True,
        registry=registry_path,
        scope="fleet",
    )
    unreviewed = [
        item
        for item in findings
        if not Path(str(item["path"])).is_file()
        or Path(str(item["path"])).resolve() not in approved
    ]
    result: dict[str, Any] = {
        "schema": "fleet-pin-release-v1",
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
        "historical_records_excluded": report.scan_historical_records(
            include_shell=True,
            include_registry=True,
            registry=registry_path,
            scope="fleet",
        ),
        "effects": {
            "provider_calls": 0,
            "provider_writes": 0,
            "application_submissions": 0,
            "publishes": 0,
            "deployments": 0,
            "sheet_writes": 0,
            "outbound_sends": 0,
            "spend_usd": 0,
        },
    }
    if unreviewed:
        return 2, result

    sites = discover_file_pin_sites()
    staged, updates = build_staged(sites)
    result["pin_updates"] = updates
    result["staged_files"] = sorted(map(str, staged))
    # Re-register only jobs that already carry a live enforcement pin. An
    # enabled script without ``script_sha256`` is not a pin source, and this
    # transaction must not silently introduce a new scheduler gate.
    jobs = [
        job for job in registry.jobs()
        if job.get("enabled")
        and HEX64.fullmatch(str(job.get("script_sha256") or ""))
    ]
    proposed_registry = []
    for job in jobs:
        path = _cron_path(job)
        if not path.is_file():
            continue
        digest = sha_bytes(staged.get(path, path.read_bytes()))
        if str(job.get("script_sha256") or "") != digest:
            proposed_registry.append({
                "job_id": str(job.get("id")),
                "script": str(path),
                "script_sha256": digest,
            })
    result["proposed_registry_updates"] = proposed_registry
    if not apply:
        result["dry_run"] = True
        result["proposed_hashes"] = {
            str(path): sha_bytes(value)
            for path, value in sorted(staged.items(), key=lambda item: os.fspath(item[0]))
        }
        return 0, result

    originals = {path: path.read_bytes() for path in staged}
    modes = {path: path.stat().st_mode & 0o777 for path in staged}
    registry_before = {
        str(job.get("id")): str(job.get("script_sha256") or "")
        for job in jobs
        if any(str(job.get("id")) == row["job_id"] for row in proposed_registry)
    }
    archived: dict[str, str] = {}
    try:
        for path in approved:
            body = path.read_bytes()
            archived[sha_bytes(body)] = str(archive_bytes(path, body))
        for path, value in staged.items():
            archived[sha_bytes(value)] = str(archive_bytes(path, value))
        for path, value in staged.items():
            if path.read_bytes() != originals[path]:
                raise RuntimeError(f"preimage_changed_before_apply:{path}")
            _atomic_write(path, value, modes[path])
        for row in proposed_registry:
            registry.update_hash(row["job_id"], row["script_sha256"])
            result["registry_updates"].append(row)
        remaining = report.scan(
            include_shell=True,
            include_registry=True,
            registry=registry_path,
            scope="fleet",
        )
        if remaining:
            raise RuntimeError(f"drift_after_release:{remaining}")
        for _source, path, digest in report.discover_pins(
            include_shell=True,
            include_registry=True,
            registry=registry_path,
            scope="fleet",
        ):
            if path.is_file() and not _is_secret(path):
                body = path.read_bytes()
                if sha_bytes(body) == digest:
                    archived[digest] = str(archive_bytes(path, body))
    except Exception:
        for path, value in originals.items():
            _atomic_write(path, value, modes[path])
        for job_id, digest in registry_before.items():
            current = next(
                (job for job in registry.jobs() if str(job.get("id")) == job_id),
                None,
            )
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
    acceptance = acceptance_root / f"fleet-{stamp}-{result['review_receipt_sha256'][:16]}.json"
    body = (json.dumps(result, indent=2, sort_keys=True) + "\n").encode()
    fd = os.open(acceptance, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    result["acceptance_receipt"] = str(acceptance)
    result["acceptance_receipt_sha256"] = sha_bytes(body)
    return 0, result


# Deliberately literal scope, NOT an arbitrary job/path allowlist. Full-fleet
# behavior above is unchanged. This closure installs exact Git source bytes,
# rather than silently blessing today's drift through a fleet sweep.
GLP331_JOB = 'f0c6a95a371c'
GLP331_CARRIER = 'glp_affiliate_reconciliation_honesty.sh'
GLP331_SNAPSHOT = 'ops/hermes-cron-scripts/' + GLP331_CARRIER
GLP331_SOURCES = frozenset((
    'scripts/affiliate_program_withdrawal.py',
    'scripts/affiliate_sheet_batch_writer.py',
    'scripts/affiliate_provider_metadata.py',
    'scripts/affiliate_approval_auto_reconciler.py',
    'scripts/glp_wire_in_ready_queue.py',
    'scripts/network_approval_email_ingest.py', GLP331_SNAPSHOT,
))
GLP331_CONSUMERS = (GLP331_JOB, '3c79a0b5f7b2', 'b0c800751b88')
GLP331_LOCKS = ('glp-affiliate-approval-reconciler.lock',
                'glp-sheet-sync-drain.lock', 'email-monitor-cron.lock')
GLP331_PREDECESSOR = '119fdf2c23d748888cf29e6449e4e7a149826141213e944e38ea8a1e122b0e6d'
GLP331_SOURCE_HEAD = '727ad1f6af8e1000f04b96f1c267e5b0de2a832f'


def _scoped_modules():
    # Reuse maintained primitives; never import an effectful carrier/payload.
    import maintenance_admission as admission
    import maintenance_cohort as cohort
    import maintenance_window as window
    import sync_from_repo as importer
    import maxbounty_pin_release as primitives
    import wrapper_pin_sync as pins
    return admission, cohort, window, importer, primitives, pins


def prepare_glp331(repo, source_head, prepared_by, *, business=BUSINESS,
                   hermes=Path('/home/ubuntu/.hermes')):
    """Read-only, byte-bound review proposal. Never emit an ACCEPT decision.

    Owner-controlled retirement of old/nonparticipating importers and bounded
    direct callers is still required; flock/census is not hostile-UID isolation.
    """
    admission, cohort, window, importer, p, pins = _scoped_modules()
    if not isinstance(prepared_by, str) or not prepared_by.strip():
        raise RuntimeError('prepared_by_required')
    if source_head != GLP331_SOURCE_HEAD:
        raise RuntimeError('glp331_exact_accepted_source_head_required')
    repo, business, hermes = map(lambda v: Path(v).absolute(), (repo, business, hermes))
    head = p.git(repo, 'rev-parse', 'HEAD').decode().strip()
    p.git(repo, 'merge-base', '--is-ancestor', source_head, head)
    if p.git(repo, 'status', '--porcelain', '--untracked-files=no').strip():
        raise RuntimeError('dirty_producer_checkout')
    dependencies = [Path(__file__), *(Path(m.__file__) for m in
                    (admission, cohort, window, importer, p, pins, report))]
    for path in dependencies:
        if (path.resolve().parent != Path(__file__).resolve().parent
                or p.git(repo, 'show', head+':scripts/'+path.name) != p.read_regular(path)[0]):
            raise RuntimeError('executing_producer_dependency_not_reviewed:' + path.name)
    registered = p.strict_json(p.git(repo, 'show', head+':ops/registered-producers.json'))
    for target in ('/home/ubuntu/.hermes/scripts/'+GLP331_CARRIER,
                   '/home/ubuntu/.hermes/cron/jobs.json'):
        if not any(row['glob'] == target and 'scripts/fleet_pin_release.py' in row['producers']
                   for row in registered['artifacts']):
            raise RuntimeError('producer_not_registered:' + target)
    observed = {path: p.read_regular(path) for path in dependencies}
    staged, modes, preimages = {}, {}, {}
    for name in sorted(GLP331_SOURCES):
        body = p.git(repo, 'show', source_head+':'+name)
        path = business/name
        # No author-selected source digests; exact PR117 business bytes only.
        git_mode = p.git(repo, 'ls-tree', source_head, '--', name).split()[0]
        mode = 0o755 if git_mode == b'100755' else 0o644
        if path.is_symlink():
            raise RuntimeError('symlink_source_refused:' + name)
        try:
            before = p.read_regular(path)
        except FileNotFoundError:
            before = None
            # No orphan bytecode shortcut for a newly introduced source. The
            # unchanged cohort handles existing members' bytecode itself.
            if path.suffix == '.py' and (path.with_suffix('.pyc').exists() or
                    list((path.parent/'__pycache__').glob(path.stem + '.*.pyc'))):
                raise RuntimeError('absent_source_has_unreviewed_bytecode:' + name)
        observed[path] = before
        if before is not None:
            mode = before[1]  # Existing installed privacy modes survive.
        modes[str(path)] = mode
        staged[path] = body
    carrier = hermes/'scripts'/GLP331_CARRIER
    observed[carrier] = p.read_regular(carrier)
    if sha_bytes(observed[carrier][0]) != GLP331_PREDECESSOR or observed[carrier][1] != 0o700:
        raise RuntimeError('glp331_unknown_carrier_preimage_or_mode')
    staged[carrier] = staged[business/GLP331_SNAPSHOT]
    modes[str(carrier)] = 0o700
    # Authenticate ALL shell conventions, including FIT authority and the two
    # writer occurrences. Unknown/malformed pins cannot become a false clean.
    text = staged[carrier].decode()
    class StagedShell:
        # Feed exact in-memory successor bytes to the unchanged read-only
        # reporter without exposing a temporary candidate or altering globals.
        name = carrier.name
        def read_text(self, **kwargs):
            return text
        def resolve(self):
            return carrier.resolve()
    discovered, issues = report._shell_inventory(StagedShell())
    if issues:
        raise RuntimeError('in_scope_unresolved_pins:' + repr(issues))
    writer = str(business/'scripts/affiliate_sheet_batch_writer.py')
    if sum(path == writer for path, *_ in pins.covered(text)) != 2:
        raise RuntimeError('glp331_both_writer_copins_required')
    for _source, target, expected in discovered:
        if target in staged:
            actual = sha_bytes(staged[target])
        else:
            observed[target] = p.read_regular(target)
            actual = sha_bytes(observed[target][0])
        if actual != expected:
            raise RuntimeError('in_scope_unreviewed_pin_drift:' + str(target))
    registry = hermes/'cron/jobs.json'
    observed[registry] = p.read_regular(registry)
    jobs = p.strict_json(observed[registry][0])['jobs']
    if not isinstance(jobs, list) or any(not isinstance(j, dict) for j in jobs):
        raise RuntimeError('registry_jobs_invalid')
    ids = [j.get('id') for j in jobs]
    if len(set(ids)) != len(ids):
        raise RuntimeError('duplicate_registration_refused')
    for jid in GLP331_CONSUMERS:
        if ids.count(jid) != 1:
            raise RuntimeError('consumer_registration_required:' + jid)
        job = next(j for j in jobs if j['id'] == jid)
        if job.get('run_claim') or job.get('fire_claim') or job.get('running'):
            raise RuntimeError('consumer_claim_or_run_refused:' + jid)
    job = next(j for j in jobs if j['id'] == GLP331_JOB)
    if (job.get('script') not in {GLP331_CARRIER, str(carrier)}
            or job.get('script_sha256') != GLP331_PREDECESSOR
            or job.get('state') != 'scheduled' or job.get('enabled') is not True
            or job.get('schedule', {}).get('kind') != 'cron'
            or job['schedule'].get('expr') != '15 7 * * *'):
        raise RuntimeError('target_registration_identity_schedule_or_hold')
    for path, before in observed.items():
        preimages[str(path)] = None if before is None else {
            'sha256': sha_bytes(before[0]), 'mode': before[1]}
    proposal = {'schema': 'fleet-pin-glp331-proposal-v1', 'scope': 'glp331',
                'prepared_by': prepared_by, 'producer_head': head, 'source_head': source_head,
                'repo': str(repo), 'business': str(business), 'hermes': str(hermes),
                'preimages': preimages, 'source_sha256': {
                    name: sha_bytes(staged[business/name]) for name in sorted(GLP331_SOURCES)},
                'proposed_sha256': {str(path): sha_bytes(body) for path, body in sorted(staged.items())},
                'source_modes': modes, 'expected_job': job, 'registration_delta': ['script_sha256'],
                'consumer_ids': list(GLP331_CONSUMERS), 'consumer_locks': list(GLP331_LOCKS)}
    return proposal, observed, staged


def release_glp331(review_path, repo, *, business=BUSINESS,
                   hermes=Path('/home/ubuntu/.hermes'), apply=False, scheduler=None):
    """Scoped native full-record CAS, coherent DAC cohort, hash-only save/readback.

    Preview independently authenticates emitted bytes; apply requires a separate
    fresh reviewer and owner exclusion, not an author-authored acceptance.
    """
    import copy
    import fcntl
    from contextlib import ExitStack
    admission, cohort, window, importer, p, pins = _scoped_modules()
    review_before = p.read_regular(Path(review_path))
    review = p.strict_json(review_before[0])
    if (review.get('schema') != 'fleet-pin-glp331-review-v1'
            or review.get('decision') not in {'REVIEW_ONLY', 'ACCEPT_EXACT_CURRENT_BYTES'}
            or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()
            or not isinstance(review.get('proposal'), dict)
            or review['reviewer'].strip() == review['proposal'].get('prepared_by', '').strip()
            or not isinstance(review.get('evidence'), list) or not review['evidence']
            or any(not isinstance(s, str) or not s.strip() for s in review['evidence'])
            or not isinstance(review.get('writer_exclusion'), str) or not review['writer_exclusion'].strip()):
        raise RuntimeError('independent_exact_review_contract')
    when = dt.datetime.strptime(review['reviewed_at_utc'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc)
    age = (dt.datetime.now(dt.timezone.utc) - when).total_seconds()
    if not 0 <= age <= 86400:
        raise RuntimeError('fresh_review_timestamp_required')
    proposal = review['proposal']
    # Durable intent refuses replay even when native outcome is unreadable.
    custody = Path(business)/'reports/ops/pin-archive/glp331'/sha_bytes(p.json_bytes(proposal))
    journal = custody/'cutover.json'
    if apply and journal.exists():
        raise RuntimeError('cutover_already_attempted_reconcile_no_replay')
    with ExitStack() as stack:
        if apply:
            stack.enter_context(importer.source_lock(Path(business)))
        actual, observed, staged = prepare_glp331(repo, proposal.get('source_head'),
            proposal.get('prepared_by'), business=business, hermes=hermes)
        if actual != proposal:
            raise RuntimeError('review_proposal_moved_full_record_cas')
        observed[Path(review_path)] = review_before
        result = {'schema': 'fleet-pin-glp331-release-v1', 'scope': 'glp331', 'applied': False,
                  'proposal': actual, 'review_sha256': sha_bytes(review_before[0]),
                  'registration_delta': ['script_sha256'], 'jobs_executed': 0,
                  'lifecycle_mutations': 0}
        if not apply:
            return result
        if review['decision'] != 'ACCEPT_EXACT_CURRENT_BYTES':
            raise RuntimeError('apply_requires_independent_exact_review')
        if scheduler is None:
            os.environ['HERMES_HOME'] = str(hermes)
            sys.path.insert(0, str(Path(hermes)/'hermes-agent'))
            from cron import jobs as scheduler
        registry = Path(hermes)/'cron/jobs.json'
        if (scheduler._current_cron_store().jobs_file != registry
                or scheduler._jobs_lock_file() != registry.parent/'.jobs.lock'):
            raise RuntimeError('wrong_scheduler_store_or_lock')
        for jid in sorted(GLP331_CONSUMERS):
            window.require(stack.enter_context(scheduler._fire_job_lock(jid)), 'native_fire_fence_unavailable')
        stack.enter_context(window.native_registry_lock(scheduler))
        for name in GLP331_LOCKS:
            fd = os.open(Path(hermes)/'state'/name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            stack.callback(os.close, fd)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        def unchanged(values):
            for path, before in values.items():
                if before is None:
                    if path.exists() or path.is_symlink():
                        raise RuntimeError('absent_source_moved:' + str(path))
                elif p.read_regular(path) != before:
                    raise RuntimeError('preimage_changed_before_apply:' + str(path))
        def drain():
            jobs = p.strict_json(p.read_regular(registry)[0])['jobs']
            def lookup(jid):
                hits = [j for j in jobs if j.get('id') == jid]
                window.require(len(hits) == 1, 'consumer_registration_moved')
                return hits[0]
            busy, owners = admission.native_runs(scheduler,
                [{'kind': 'hermes', 'id': jid} for jid in GLP331_CONSUMERS], lookup)
            busy += admission.process_readers(staged, staged, owners, {os.getpid()})
            return busy
        unchanged(observed)
        window.require(not drain(), 'glp331_consumers_not_drained')
        p.preserve_custody(custody, {k:v for k,v in observed.items() if v is not None}, staged)
        unchanged(observed)
        window.atomic_json(journal, {'schema': window.SCHEMA, 'kind': 'glp331-scoped-cutover-v1',
                                    'proposal': actual, 'absent': [str(k) for k,v in observed.items() if v is None]})
        jobs = p.strict_json(observed[registry][0])['jobs']
        candidate = copy.deepcopy(jobs)
        carrier = Path(hermes)/'scripts'/GLP331_CARRIER
        next(j for j in candidate if j['id'] == GLP331_JOB)['script_sha256'] = sha_bytes(staged[carrier])
        remaining = {k:v for k,v in observed.items() if k not in staged}
        before = {k:v for k,v in observed.items() if k in staged and v is not None}
        cohort_stage = {k: staged[k] for k in before}
        aliases = {}
        try:
            # Missing public paths stay absent until ALL existing members are
            # sealed. Private same-directory aliases let the unchanged cohort
            # own their descriptors; commit links their already-sealed inodes.
            for path in staged:
                if observed[path] is None:
                    fd, name = tempfile.mkstemp(prefix='.glp331-new-', dir=path.parent)
                    alias = Path(name)
                    aliases[path] = alias
                    mode = actual['source_modes'][str(path)]
                    with os.fdopen(fd, 'wb') as out:
                        out.write(staged[path]); out.flush(); os.fsync(out.fileno())
                        os.fchmod(out.fileno(), mode)
                    before[alias] = (staged[path], mode)
                    cohort_stage[alias] = staged[path]
                    cohort.sync_dir(path.parent)
            data = window.read_journal(journal)
            data['new_source_aliases'] = {str(k):str(v) for k,v in aliases.items()}
            window.atomic_json(journal, data)
            def commit():
                unchanged(remaining)
                window.require(not drain(), 'glp331_consumers_not_drained')
                for path, alias in aliases.items():
                    window.require(alias.stat().st_mode & 0o777 == 0, 'new_source_not_sealed')
                    # link() is atomic absent-path CAS, never an overwrite.
                    os.link(alias, path, follow_symlinks=False)
                    cohort.sync_dir(path.parent)
                    window.require(cohort.metadata(path.stat()) == cohort.metadata(alias.stat())
                                   and path.stat().st_mode & 0o777 == 0, 'new_source_link_readback')
                scheduler._record_load_stamp(None)
                try:
                    scheduler._save_jobs_unlocked(copy.deepcopy(candidate))
                finally:
                    scheduler._record_load_stamp(None)
                current = p.read_regular(registry)
                if p.strict_json(current[0])['jobs'] != candidate:
                    raise RuntimeError('registration_readback_mismatch_no_retry')
                remaining[registry] = current
                unchanged(remaining)
            cohort.switch_files(journal, cohort_stage, before, drain, commit=commit)
            for alias in aliases.values():
                os.unlink(alias); cohort.sync_dir(alias.parent)
            unchanged({**remaining, **{path: (body, actual['source_modes'][str(path)])
                                       for path, body in staged.items()}})
        except BaseException:
            state = window.read_journal(journal).get('source_switch', {})
            if state.get('commit_started'):
                # Cohort keeps the seal across every uncertain native outcome;
                # even a post-publication verification error must fail closed.
                for path in set(staged) | set(aliases.values()):
                    if path.exists():
                        os.chmod(path, 0)
                state['state'] = 'commit_uncertain'
                data = window.read_journal(journal); data['source_switch'] = state
                window.atomic_json(journal, data)
            elif state.get('state') not in {'recovery_required', 'recovering'}:
                for alias in aliases.values():
                    os.unlink(alias); cohort.sync_dir(alias.parent)
            raise
        result.update(applied=True, custody=str(custody), cutover_journal=str(journal))
        p.atomic_write(custody/'acceptance.json', p.json_bytes(result), 0o600)
        return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=('fleet', 'glp331'), default='fleet')
    parser.add_argument('--prepare', action='store_true')
    parser.add_argument('--repo', type=Path)
    parser.add_argument('--source-head')
    parser.add_argument('--prepared-by')
    parser.add_argument("--review-receipt")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--registry-json")
    args = parser.parse_args()
    if args.scope == 'glp331':
        if not args.repo or args.registry_json:
            parser.error('glp331 requires --repo and the native registry, never --registry-json')
        if args.prepare:
            if args.apply or args.review_receipt:
                parser.error('--prepare emits only a read-only proposal, not acceptance')
            result, _, _ = prepare_glp331(args.repo, args.source_head, args.prepared_by)
        else:
            if not args.review_receipt or args.source_head or args.prepared_by:
                parser.error('reviewed glp331 requires only --repo and --review-receipt')
            result = release_glp331(Path(args.review_receipt), args.repo, apply=args.apply)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if args.prepare or args.repo or args.source_head or args.prepared_by or not args.review_receipt:
        parser.error('fleet requires --review-receipt; scoped preparation options are not fleet authority')
    registry_path = Path(args.registry_json) if args.registry_json else CRON_REGISTRY
    registry = JsonRegistry(registry_path) if args.registry_json else HermesRegistry()
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as handle:
        os.chmod(LOCK, 0o600)
        import fcntl

        fcntl.flock(handle, fcntl.LOCK_EX)
        code, result = release(
            Path(args.review_receipt),
            registry,
            registry_path=registry_path,
            apply=args.apply,
        )
    print(json.dumps(result, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
