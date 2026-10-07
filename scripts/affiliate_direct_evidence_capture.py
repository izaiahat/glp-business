#!/usr/bin/env python3
"""Persistently capture first-party evidence for discovered direct programs.

This lane performs only public Firecrawl reads. It never follows an application
redirect, submits a form, or fetches a tracking URL. Exact evidence bytes are
hash-bound into the site config so the existing application policy can make the
legality-only execution decision on every plan and execute pass.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

BUSINESS = Path("/home/ubuntu/business")
MACHINE = BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
DEFAULT_CONFIG = BUSINESS / "config/affiliate-sites/glp-clinic-scout.json"
DEFAULT_LOCK = Path("/home/ubuntu/.hermes/state/affiliate-direct-evidence.lock")
DIRECT_EVIDENCE_SCHEMA = "direct-affiliate-application-evidence-v1"
RUN_SCHEMA = "glp-direct-evidence-capture-run-v1"
BLOCKED_URL_RE = re.compile(
    r"(?:^|[/?#_.-])(?:track(?:ing)?|redirect|click|deeplink|hoplink|aff_c|affiliate-link)(?:$|[/?#_.=-])",
    re.I,
)
TERM_PATH_RE = re.compile(r"(?:terms?(?:[-_/ ](?:of[-_/ ](?:service|use)|conditions?))?|agreement|conditions?)", re.I)
AFFILIATE_PATH_RE = re.compile(r"(?:affiliate|ambassador|creator|partner|publisher|referral)", re.I)
ACCESS_CHALLENGE_RE = re.compile(
    r"(?:please wait while your request is being verified|checking your browser|"
    r"verify you are human|cloudflare ray id|enable javascript and cookies)", re.I,
)


def _load_policy():
    path = BUSINESS / "scripts/affiliate_contract_policy.py"
    spec = importlib.util.spec_from_file_location("affiliate_contract_policy_capture", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def canonical_host(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
    except ValueError:
        return ""
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def same_https_host(left: str, right: str) -> bool:
    return bool(canonical_host(left) and canonical_host(left) == canonical_host(right))


def normalize_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        return ""
    return urlunsplit(("https", parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))


def safe_terms_candidates(application_url: str, links: list[str]) -> list[str]:
    """Return first-party non-tracking terms URLs, strongest candidates first."""
    app = normalize_url(application_url)
    host = canonical_host(app)
    if not app or not host:
        return []
    scored: dict[str, int] = {}
    for raw in links:
        candidate = normalize_url(str(raw or ""))
        if not candidate or not same_https_host(app, candidate):
            continue
        parsed = urlsplit(candidate)
        probe = f"{parsed.path}?{parsed.query}".lower()
        if BLOCKED_URL_RE.search(probe) or not TERM_PATH_RE.search(parsed.path):
            continue
        score = 100 if AFFILIATE_PATH_RE.search(parsed.path) else 50
        scored[candidate] = max(score, scored.get(candidate, 0))

    # Public, same-host conventional terms paths are safe read-only probes. They
    # are attempted only after links actually published on the application page.
    root = f"https://{urlsplit(app).netloc}"
    for path, score in (
        ("/affiliate-terms", 80), ("/affiliate-program-terms", 80),
        ("/ambassador-program/terms-conditions", 80),
        ("/terms-and-conditions", 40), ("/terms-of-service", 40),
        ("/terms", 35), ("/policies/terms-of-service", 35),
    ):
        candidate = normalize_url(root + path)
        scored.setdefault(candidate, score)
    return [url for url, _ in sorted(scored.items(), key=lambda item: (-item[1], item[0]))]


def latest_direct_inventory(machine: Path = MACHINE) -> Path:
    candidates: list[Path] = []
    for path in machine.glob("backrun-*/marketplace/direct-supply-inventory.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if doc.get("schema") == "affiliate-supply-discovered-inventory-v1" and isinstance(doc.get("programs"), list):
            candidates.append(path)
    if not candidates:
        raise RuntimeError("direct_supply_inventory_missing")
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, str(path)))


def _firecrawl_key() -> str:
    value = os.environ.get("FIRECRAWL_API_KEY", "").strip()
    if value:
        return value
    env = Path.home() / ".hermes/.env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("FIRECRAWL_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("firecrawl_api_key_missing")


class FirecrawlScraper:
    def __init__(self, api_key: str | None = None, *, timeout: int = 90, attempts: int = 3):
        self.api_key = api_key or _firecrawl_key()
        self.timeout = max(1, int(timeout))
        self.attempts = max(1, int(attempts))

    def __call__(self, url: str) -> dict[str, Any]:
        last_error = ""
        for attempt in range(1, self.attempts + 1):
            payload: dict[str, Any] = {
                "url": url, "formats": ["markdown", "links"],
                "onlyMainContent": True, "maxAge": 0,
            }
            if attempt > 1:
                payload.update(proxy="auto", waitFor=1500)
            request = urllib.request.Request(
                "https://api.firecrawl.dev/v2/scrape",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    document = json.load(response)
                data = document.get("data") if isinstance(document, dict) else None
                if not isinstance(data, dict):
                    raise RuntimeError("firecrawl_data_missing")
                markdown = str(data.get("markdown") or "")
                metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
                status = int(metadata.get("statusCode") or 200)
                if status != 200 or not markdown.strip():
                    raise RuntimeError(f"firecrawl_capture_incomplete:{status}")
                if ACCESS_CHALLENGE_RE.search(markdown):
                    raise RuntimeError("firecrawl_access_challenge")
                return {
                    "url": url, "markdown": markdown,
                    "links": [str(value) for value in (data.get("links") or [])],
                    "status_code": status,
                    "scrape_id": str(metadata.get("scrapeId") or ""),
                    "title": str(metadata.get("title") or metadata.get("ogTitle") or ""),
                    "attempts": attempt,
                }
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
                    ValueError, RuntimeError) as exc:
                last_error = f"{type(exc).__name__}:{str(exc)[:180]}"
                if attempt < self.attempts:
                    time.sleep(min(2 ** (attempt - 1), 4))
        raise RuntimeError(f"firecrawl_scrape_failed:{last_error}")


def candidate_identity(candidate: dict[str, Any]) -> dict[str, str]:
    return {
        "network": "direct",
        "program_id": str(candidate.get("program_id") or ""),
        "name": str(candidate.get("name") or candidate.get("program_id") or ""),
    }


def build_artifact(candidate: dict[str, Any], application_text: str, terms_text: str) -> dict[str, Any]:
    identity = candidate_identity(candidate)
    return {
        "schema": DIRECT_EVIDENCE_SCHEMA,
        "captured_at_utc": utc(),
        "candidate": identity,
        "source": {
            "url": str(candidate.get("url") or ""),
            "first_party": True,
            "application_surface_text": application_text,
            "application_surface_text_sha256": sha256_text(application_text),
            "terms_text": terms_text,
            "terms_text_sha256": sha256_text(terms_text) if terms_text else None,
        },
        "effects": {"provider_calls": 0, "provider_mutations": 0, "applications_submitted": 0},
    }


def private_json(path: Path, payload: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    return sha256_bytes(raw)


def slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result or sha256_text(value)[:12]


def capture_candidate(candidate: dict[str, Any], fetcher: Callable[[str], dict[str, Any]],
                      output_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    application_url = str(candidate.get("url") or "")
    if not canonical_host(application_url):
        raise RuntimeError("direct_application_url_not_first_party_https")
    application = fetcher(application_url)
    terms: dict[str, Any] | None = None
    term_attempts: list[dict[str, Any]] = []
    for candidate_url in safe_terms_candidates(application_url, list(application.get("links") or [])):
        if candidate_url == normalize_url(application_url):
            continue
        try:
            result = fetcher(candidate_url)
            term_attempts.append({
                "url": candidate_url, "status_code": result.get("status_code"),
                "scrape_id": result.get("scrape_id"), "error": None,
            })
        except Exception as exc:  # per-candidate isolation; retain honest attempts
            term_attempts.append({"url": candidate_url, "status_code": None,
                                  "scrape_id": None, "error": f"{type(exc).__name__}:{str(exc)[:180]}"})
            continue
        text = str(result.get("markdown") or "").strip()
        # A successful guessed route that silently renders a generic 404 shell is
        # not terms evidence. Require terms language in the URL and bytes.
        if text and re.search(r"\b(?:terms?|agreement|conditions?)\b", text, re.I):
            terms = result
            break

    application_text = str(application.get("markdown") or "").strip()
    terms_text = str((terms or {}).get("markdown") or "").strip()
    artifact = build_artifact(candidate, application_text, terms_text)
    artifact_path = output_dir / f"{slug(str(candidate.get('program_id') or 'program'))}.json"
    artifact_sha = private_json(artifact_path, artifact)
    receipt = {
        "candidate": candidate_identity(candidate),
        "application": {
            "url": application_url, "status_code": application.get("status_code"),
            "scrape_id": application.get("scrape_id"), "attempts": application.get("attempts"),
        },
        "terms": {
            "url": (terms or {}).get("url"), "status_code": (terms or {}).get("status_code"),
            "scrape_id": (terms or {}).get("scrape_id"), "attempts": term_attempts,
            "present": bool(terms_text),
        },
        "artifact": {"path": str(artifact_path), "sha256": artifact_sha},
        "tracking_urls_fetched": 0,
        "application_submissions": 0,
    }
    return {"path": str(artifact_path), "sha256": artifact_sha}, receipt


def _binding_gate(policy, candidate: dict[str, Any], binding: dict[str, str], operation: str) -> dict[str, Any]:
    projection = dict(candidate)
    projection["network"] = "direct"
    projection["application_url"] = projection.get("url") or ""
    projection["direct_application_evidence"] = dict(binding)
    return policy.direct_application_eligibility(projection, operation=operation)


def _binding_age_hours(binding: dict[str, str]) -> float | None:
    try:
        document = json.loads(Path(binding["path"]).read_text(encoding="utf-8"))
        captured = dt.datetime.fromisoformat(str(document["captured_at_utc"]).replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - captured.astimezone(dt.timezone.utc)).total_seconds() / 3600
    except (KeyError, OSError, ValueError, json.JSONDecodeError):
        return None


def capture_run(inventory_path: Path, config_path: Path, output_dir: Path,
                fetcher: Callable[[str], dict[str, Any]], *, apply_config: bool,
                retry_held_hours: int = 24,
                force_program_ids: set[str] | None = None) -> dict[str, Any]:
    policy = _load_policy()
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    auto_apply = config.setdefault("auto_apply", {})
    bindings = auto_apply.setdefault("direct_application_evidence", {})
    programs = [row for row in (inventory.get("programs") or [])
                if isinstance(row, dict) and row.get("program_id")]
    rows: list[dict[str, Any]] = []
    captures = reused = applicable = 0
    changed_bindings: dict[str, dict[str, str]] = {}
    force_program_ids = force_program_ids or set()

    for candidate in programs:
        key = f"direct:{candidate['program_id']}"
        existing = bindings.get(key) if isinstance(bindings.get(key), dict) else None
        existing_gate = (_binding_gate(policy, candidate, existing, "direct_evidence_capture.preflight")
                         if existing else None)
        age = _binding_age_hours(existing) if existing else None
        recapture = (
            str(candidate["program_id"]) in force_program_ids
            or not existing or not existing_gate
            or existing_gate.get("decision") == "HOLD_DIRECT_EVIDENCE_MISSING_OR_INVALID"
            or (existing_gate.get("decision") in {
                "HOLD_FIRST_PARTY_TERMS_EVIDENCE_MISSING",
                "HOLD_AFFILIATE_APPLICATION_SURFACE_UNVERIFIED",
            } and (age is None or age >= retry_held_hours))
        )
        receipt: dict[str, Any] = {"candidate": candidate_identity(candidate), "binding_key": key}
        binding = existing
        if recapture:
            try:
                binding, capture_receipt = capture_candidate(candidate, fetcher, output_dir)
                changed_bindings[key] = binding
                captures += 1
                receipt.update(capture_receipt)
            except Exception as exc:
                receipt.update(status="CAPTURE_FAILED",
                               blocker=f"{type(exc).__name__}:{str(exc)[:240]}")
                rows.append(receipt)
                continue
        else:
            reused += 1
            receipt.update(status="REUSED_FRESH_BINDING", artifact=existing)

        gate = _binding_gate(policy, candidate, binding, "direct_evidence_capture.post_capture")
        if gate.get("operational"):
            applicable += 1
        receipt.update(status=("CAPTURED" if recapture else receipt["status"]),
                       gate=gate, applicable=bool(gate.get("operational")))
        rows.append(receipt)

    if apply_config and changed_bindings:
        bindings.update(changed_bindings)
        original_mode = config_path.stat().st_mode & 0o777
        private_json(config_path, config)
        os.chmod(config_path, original_mode)

    result = {
        "schema": RUN_SCHEMA,
        "captured_at_utc": utc(),
        "inventory": {"path": str(inventory_path), "sha256": sha256_bytes(inventory_path.read_bytes())},
        "config": {"path": str(config_path), "updated": bool(apply_config and changed_bindings),
                   "bindings_added_or_refreshed": len(changed_bindings)},
        "program_count": len(programs), "captured_count": captures, "reused_count": reused,
        "applicable_count": applicable,
        "blocked_count": sum(1 for row in rows if not row.get("applicable")),
        "capture_failure_count": sum(1 for row in rows if row.get("status") == "CAPTURE_FAILED"),
        "rows": rows,
        "effects": {"firecrawl_public_reads": sum(
            1 + len((row.get("terms") or {}).get("attempts") or []) for row in rows
            if row.get("application")),
            "provider_mutations": 0, "applications_submitted": 0,
            "tracking_urls_fetched": 0, "config_writes": int(bool(apply_config and changed_bindings))},
    }
    private_json(output_dir / "result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture hash-bound direct-program evidence.")
    parser.add_argument("--inventory", default="")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--apply-config", action="store_true")
    parser.add_argument("--retry-held-hours", type=int, default=24)
    parser.add_argument("--fetch-timeout", type=int, default=90)
    parser.add_argument("--force-program-id", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    inventory = Path(args.inventory) if args.inventory else latest_direct_inventory()
    output_dir = (Path(args.output_dir) if args.output_dir else
                  MACHINE / "direct-evidence-capture" / dt.datetime.now().astimezone().strftime("%Y-%m-%d"))
    DEFAULT_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with DEFAULT_LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = capture_run(
            inventory, Path(args.config), output_dir,
            FirecrawlScraper(timeout=args.fetch_timeout),
            apply_config=args.apply_config, retry_held_hours=max(0, args.retry_held_hours),
            force_program_ids={str(value) for value in args.force_program_id},
        )
    if args.json:
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        print(f"direct evidence: captured {result['captured_count']} · applicable {result['applicable_count']} · blocked {result['blocked_count']}")
    return 1 if result["capture_failure_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
