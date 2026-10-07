#!/usr/bin/env python3
"""GLP SEO data rails — one module, every measurement source the SEO machines use.

Why this exists: before it, each lane re-implemented DataForSEO basic auth, Bing
Webmaster query params, Google token refresh, and PostHog HogQL POSTs. Five
copies of the same auth dance is five places to break. Every SEO machine now
imports this and gets identical parsing, identical honest-failure shape, and
identical secret handling (paths only — no key is ever printed or written to a
receipt).

Rails carried here:
  * DataForSEO      — SERP ranks + keyword volumes (paid; balance-guarded)
  * Bing Webmaster  — first-party query/page stats, keyword research, URL +
                      content submission (fast indexing)
  * Google Search Console — direct user-OAuth rail; this is the ONLY rail that
                      can see ``sc-domain:glpclinicscout.com`` (Cogny's identity
                      sees the URL-prefix properties only)
  * GA4             — direct user-OAuth rail (Data API runReport)
  * PostHog         — HogQL over project 508172
  * Cogny           — via ``ops/glp/cogny_client.py`` (separate module)

Honest-failure contract: every ``fetch_*`` returns a dict that always carries
``ok`` and, on failure, ``error``. A rail that is down degrades the run's
coverage and says so in the receipt; it never fakes a number and never silently
drops a probe. ``unmeasured`` is never inferred from another rail.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

HOME = Path.home()
SECRETS = HOME / ".hermes/secrets"

DATAFORSEO_ENV = SECRETS / "boring-ai/dataforseo.env"
DATAFORSEO_OPS_ENV = SECRETS / "glpclinicscout/dataforseo.env"
DATAFORSEO_LEDGER = HOME / "business/reports/ops/orchestration-restore-20260907/WS-C/dataforseo-ledger.jsonl"
BING_ENV = SECRETS / "glpclinicscout/bing-webmaster-api-key.env"
POSTHOG_KEY = SECRETS / "posthog/personal-api-key.txt"
GOOGLE_TOKEN = SECRETS / "glp-scout-ops-google/google_token.json"

POSTHOG_PROJECT = "508172"
POSTHOG_QUERY_URL = f"https://us.posthog.com/api/projects/{POSTHOG_PROJECT}/query/"

GA4_GLP_PROPERTY = "541440529"
GSC_DOMAIN_PROPERTY = "sc-domain:glpclinicscout.com"
GSC_URL_PROPERTY = "https://glpclinicscout.com/"
BING_SITE = "https://glpclinicscout.com"

USER_AGENT = "glp-seo-machine/1.0"


class RailError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# small HTTP helper
# ---------------------------------------------------------------------------

def _http(url: str, *, method: str = "GET", headers: dict[str, str] | None = None,
          body: Any = None, timeout: int = 90, retries: int = 3) -> Any:
    data = None
    hdrs = {"User-Agent": USER_AGENT}
    hdrs.update(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:  # keep the body — it explains the 4xx
            detail = exc.read().decode("utf-8", "replace")[:400]
            last = RailError(f"http_{exc.code}: {detail}")
            if exc.code in (400, 401, 403, 404):
                break
        except Exception as exc:  # noqa: BLE001 — transport
            last = exc
        if attempt + 1 < retries:
            time.sleep(2 * (attempt + 1))
    raise RailError(str(last)[:400])


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


# ---------------------------------------------------------------------------
# DataForSEO
# ---------------------------------------------------------------------------

class DataForSEO:
    """Ordered, fail-closed rank rail: tryboringai first, then ops after exhaustion.

    Pass a stable business ``run_id`` when resuming a run. The journal refuses
    unresolved attempts and duplicate requests within that run. It stores no
    credentials, queries, payloads or raw API responses. No billable POST retries.
    ``balance(account=...)`` is an explicit read; it never selects a spend account.
    """

    BASE = "https://api.dataforseo.com/v3"

    def __init__(self, *, ledger_path: Path | None = None, run_id: str | None = None,
                 account_paths: tuple[Path, Path] | None = None) -> None:
        self.run_id = run_id or str(uuid.uuid4())
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", self.run_id):
            raise RailError("dataforseo_invalid_run_id")
        self.ledger_path = Path(ledger_path) if ledger_path is not None else DATAFORSEO_LEDGER
        paths = account_paths or (DATAFORSEO_ENV, DATAFORSEO_OPS_ENV)
        if len(paths) != 2 or Path(paths[0]).resolve() == Path(paths[1]).resolve():
            raise RailError("dataforseo_distinct_ordered_paths_required")
        self._accounts: dict[str, dict[str, Any]] = {}
        identities = set()
        for label, path in zip(("tryboringai", "ops"), paths):
            env = _read_env(Path(path))
            login = env.get("DATAFORSEO_LOGIN") or env.get("DATAFORSEO_USERNAME")
            password = env.get("DATAFORSEO_PASSWORD") or env.get("PASSWORD")
            state = "UNMEASURED" if login and password else "ABSENT"
            if login and password:
                if login.casefold() in identities:
                    raise RailError("dataforseo_duplicate_account_identity")
                identities.add(login.casefold())
            self._accounts[label] = {
                "auth": base64.b64encode(f"{login}:{password}".encode()).decode() if login and password else None,
                "state": state, "balance": None,
            }
        self._active = "tryboringai"
        self._halted = False
        self._lock = threading.RLock()

    def account_states(self) -> dict[str, str]:
        """Non-secret account readiness; absence is not exhaustion."""
        return {label: account["state"] for label, account in self._accounts.items()}

    def _record(self, event: str, account: str, **fields: Any) -> None:
        row = {"at": time.time(), "run_id": self.run_id, "event": event, "account": account, **fields}
        try:
            self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
            with self.ledger_path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                if event == "ATTEMPT":
                    handle.seek(0)
                    previous = [json.loads(line) for line in handle if line.strip()]
                    previous = [r for r in previous if r.get("run_id") == self.run_id]
                    settled = {r.get("attempt_id") for r in previous if r.get("event") == "RESULT"}
                    if any(r.get("event") == "ATTEMPT" and r.get("attempt_id") not in settled for r in previous):
                        raise RailError("dataforseo_unresolved_attempt_requires_reconciliation")
                    if any(r.get("event") == "RESULT" and r.get("request_hash") == fields.get("request_hash")
                           and r.get("state") != "EXHAUSTED" for r in previous):
                        raise RailError("dataforseo_request_already_recorded_for_run")
                handle.seek(0, os.SEEK_END)
                handle.write(json.dumps(row, sort_keys=True, allow_nan=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except RailError:
            raise
        except Exception:
            self._halted = True
            raise RailError("dataforseo_ledger_unavailable_or_invalid") from None

    @staticmethod
    def _number(value: Any) -> float | None:
        if isinstance(value, bool) or value is None:
            return None
        try:
            number = float(value)
            return number if math.isfinite(number) and number >= 0 else None
        except (ValueError, TypeError):
            return None

    def balance(self, account: str | None = None) -> float:
        with self._lock:
            if account is None:
                for label in ("tryboringai", "ops"):
                    value = self.balance(label)
                    if value > 0:
                        self._active = label
                        return value
                return 0.0
            label = account
            if label not in self._accounts:
                raise RailError("dataforseo_unknown_account")
            selected = self._accounts[label]
            if not selected["auth"]:
                self._record("ACCOUNT_STATE", label, state="ABSENT", balance=None, cost=None)
                raise RailError(f"dataforseo_{label}_ABSENT")
            self._record("BALANCE_ATTEMPT", label, balance=None, cost=None)
            try:
                doc = _http(self.BASE + "/appendix/user_data", timeout=60, retries=1,
                            headers={"Authorization": "Basic " + selected["auth"]})
                task = doc["tasks"][0]
                if doc.get("status_code") != 20000 or task.get("status_code") != 20000:
                    raise ValueError("unsuccessful balance envelope")
                value = self._number(task["result"][0]["money"]["balance"])
                if value is None:
                    raise ValueError("unknown balance")
            except Exception:
                selected.update(state="UNMEASURED", balance=None)
                self._record("BALANCE", label, state="UNMEASURED", balance=None, cost=None)
                raise RailError(f"dataforseo_{label}_balance_unmeasured") from None
            selected.update(state="EXHAUSTED" if value == 0 else "READY", balance=value)
            self._record("BALANCE", label, state=selected["state"], balance=value, cost=None)
            return value

    def _post(self, path: str, payload: list[dict[str, Any]], timeout: int = 180) -> dict[str, Any]:
        with self._lock:
            if self._halted:
                raise RailError("dataforseo_run_halted_requires_reconciliation")
            request_hash = hashlib.sha256(json.dumps([path, payload], sort_keys=True, allow_nan=False).encode()).hexdigest()
            for label in ("tryboringai", "ops"):
                selected = self._accounts[label]
                if not selected["auth"]:
                    self._record("ACCOUNT_STATE", label, state="ABSENT", cost=None)
                    raise RailError(f"dataforseo_{label}_ABSENT")
                if selected["state"] == "UNMEASURED":
                    self.balance(label)
                if selected["state"] == "EXHAUSTED":
                    continue
                if selected["state"] != "READY":
                    raise RailError(f"dataforseo_{label}_not_ready")
                self._active = label
                attempt_id = str(uuid.uuid4())
                self._record("ATTEMPT", label, attempt_id=attempt_id, request_hash=request_hash,
                             endpoint=path, cost=None, balance_estimate=selected["balance"])
                try:
                    doc = _http(self.BASE + path, method="POST", body=payload, timeout=timeout, retries=1,
                                headers={"Authorization": "Basic " + selected["auth"]})
                except Exception:
                    self._halted = True
                    self._record("AMBIGUOUS", label, attempt_id=attempt_id, request_hash=request_hash, cost=None)
                    raise RailError("dataforseo_transport_outcome_unknown_no_retry") from None
                cost = self._number(doc.get("cost")) if isinstance(doc, dict) else None
                tasks = doc.get("tasks") if isinstance(doc, dict) else None
                codes = [t.get("status_code") for t in tasks] if isinstance(tasks, list) and all(isinstance(t, dict) for t in tasks) else []
                # Only an explicit zero-cost all-rejected insufficient-funds response may fail over.
                exhausted = (cost == 0 and isinstance(tasks, list) and all(isinstance(t, dict) for t in tasks)
                             and (doc.get("status_code") == 40200 or (codes and all(c == 40200 for c in codes)))
                             and all(c == 40200 for c in codes)
                             and all(self._number(t.get("cost", 0)) == 0 and not t.get("result") for t in (tasks or [])))
                if cost is None or not isinstance(tasks, list):
                    self._halted = True
                    self._record("AMBIGUOUS", label, attempt_id=attempt_id, request_hash=request_hash, cost=cost)
                    raise RailError("dataforseo_cost_or_envelope_unmeasured_no_retry")
                if exhausted:
                    selected["state"] = "EXHAUSTED"
                    self._record("RESULT", label, attempt_id=attempt_id, request_hash=request_hash,
                                 state="EXHAUSTED", cost=cost, reason="provider_40200_insufficient_for_request")
                    continue
                if doc.get("status_code") != 20000 or not codes or any(c != 20000 for c in codes):
                    self._halted = True
                    self._record("RESULT", label, attempt_id=attempt_id, request_hash=request_hash,
                                 state="PROVIDER_ERROR", cost=cost, status_codes=codes)
                    raise RailError("dataforseo_provider_error_no_failover")
                selected["balance"] = max(0.0, selected["balance"] - cost)
                selected["state"] = "EXHAUSTED" if selected["balance"] == 0 else "READY"
                self._record("RESULT", label, attempt_id=attempt_id, request_hash=request_hash,
                             state="MEASURED", cost=cost, balance_estimate=selected["balance"])
                return doc
            self._record("ACCOUNT_STATE", "ops", state="EXHAUSTED", cost=None)
            raise RailError("dataforseo_all_accounts_EXHAUSTED")

    def serp_rank(self, keyword: str, target_domain: str, *, depth: int = 100,
                  location_code: int = 2840, language_code: str = "en") -> dict[str, Any]:
        """Our organic position for ``keyword``. ``position=None`` means genuinely absent
        from the measured depth — that is ``not_ranking``, never ``unmeasured``."""
        try:
            doc = self._post("/serp/google/organic/live/advanced", [{
                "keyword": keyword, "location_code": location_code,
                "language_code": language_code, "device": "desktop", "depth": depth,
            }])
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "keyword": keyword, "state": "unmeasured",
                    "error": str(exc)[:300]}
        task = (doc.get("tasks") or [{}])[0]
        if task.get("status_code") != 20000:
            return {"ok": False, "keyword": keyword, "state": "unmeasured",
                    "error": f"task_status_{task.get('status_code')}"}
        items = ((task.get("result") or [{}])[0] or {}).get("items") or []
        position: int | None = None
        our_url: str | None = None
        competitors: list[str] = []
        for item in items:
            if item.get("type") != "organic":
                continue
            domain = (item.get("domain") or "").lower()
            if len(competitors) < 3 and target_domain not in domain:
                competitors.append(domain)
            if target_domain in domain and position is None:
                position = item.get("rank_absolute") or item.get("rank_group")
                our_url = item.get("url")
        return {"ok": True, "keyword": keyword, "position": position, "url": our_url,
                "state": "measured" if position is not None else "not_ranking",
                "top_competitors": competitors,
                "cost": float(doc.get("cost") or 0.0)}

    def search_volume(self, keywords: list[str], *, location_code: int = 2840,
                      language_code: str = "en") -> dict[str, dict[str, Any]]:
        """Google Ads search volumes, keyed by keyword. Up to 700 keywords per task."""
        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(keywords), 700):
            batch = keywords[start:start + 700]
            try:
                doc = self._post("/keywords_data/google_ads/search_volume/live", [{
                    "keywords": batch, "location_code": location_code,
                    "language_code": language_code,
                }])
            except Exception as exc:  # noqa: BLE001
                for kw in batch:
                    out[kw] = {"ok": False, "error": str(exc)[:200]}
                continue
            task = (doc.get("tasks") or [{}])[0]
            for row in (task.get("result") or []):
                kw = row.get("keyword")
                if not kw:
                    continue
                out[kw] = {"ok": True, "volume": row.get("search_volume"),
                           "competition": row.get("competition"),
                           "cpc": row.get("cpc")}
        return out

    def ranked_keywords(self, targets: list[str], *, location_code: int = 2840,
                        language_code: str = "en", page_size: int = 1000,
                        max_pages_per_target: int = 10,
                        max_cost_usd: float = 5.0) -> dict[str, Any]:
        """Fetch complete Google-organic keyword footprints with bounded spend.

        DataForSEO Labs accepts only one ranked-keywords task per HTTP request,
        so batching here means the maximum 1,000 rows per paid task rather than
        multiple tasks in one POST. Offset zero determines the bounded remaining
        pages. Reserve a full requested page before EVERY POST, including each
        target's initial page; sparse/discounted pages never reduce reservations.
        Only measured task costs are reported as spend. Published-price ceiling:
        https://dataforseo.com/pricing/dataforseo-labs/dataforseo-google-api
        USD 0.012/task + 0.00012/item (2026-09-10); clickstream is disabled.
        """
        from decimal import Decimal

        cap = self._number(max_cost_usd)
        if cap is None or cap <= 0:
            raise RailError("dataforseo_ranked_keywords_cost_cap_invalid")
        max_cost_usd = Decimal(str(max_cost_usd))
        if type(page_size) is not int or not 1 <= page_size <= 1000:
            raise RailError("dataforseo_ranked_keywords_page_size_invalid")
        if type(max_pages_per_target) is not int or not 1 <= max_pages_per_target <= 100:
            raise RailError("dataforseo_ranked_keywords_page_cap_invalid")
        page_ceiling = Decimal("0.012") + Decimal("0.00012") * Decimal(str(page_size))
        reserved_cost = Decimal("0")

        def preflight() -> None:
            nonlocal reserved_cost
            projected = max(reserved_cost, Decimal(str(actual_cost))) + page_ceiling
            if projected > Decimal(str(max_cost_usd)):
                raise RailError(
                    f"dataforseo_ranked_keywords_cost_cap_preflight:{projected:.6f}:"
                    f"{max_cost_usd:.6f}")
            reserved_cost = projected
        clean_targets = list(dict.fromkeys(str(value).strip().lower() for value in targets
                                            if str(value).strip()))
        if not clean_targets:
            return {"ok": True, "domains": {}, "cost_usd": 0.0, "tasks": [],
                    "provider_calls": 0}


        def request(target: str, offset: int) -> dict[str, Any]:
            return {
                "target": target,
                "location_code": location_code,
                "language_code": language_code,
                "load_rank_absolute": True,
                "include_clickstream_data": False,
                "filters": [["ranked_serp_element.serp_item.type", "=", "organic"]],
                "order_by": ["ranked_serp_element.serp_item.etv,desc"],
                "limit": page_size,
                "offset": offset,
            }

        path = "/dataforseo_labs/google/ranked_keywords/live"
        domains = {
            target: {"target": target, "total_count": None, "items": [],
                     "complete": False, "pages": 0, "metrics": None,
                     "metrics_absolute": None}
            for target in clean_targets
        }
        task_receipts: list[dict[str, Any]] = []
        provider_calls = 0
        actual_cost = Decimal("0")

        def consume(doc: dict[str, Any], expected: list[dict[str, Any]]) -> None:
            nonlocal actual_cost
            tasks = doc.get("tasks") or []
            if len(tasks) != len(expected):
                raise RailError(
                    f"dataforseo_ranked_keywords_task_count_mismatch:{len(tasks)}:{len(expected)}")
            for task, wanted in zip(tasks, expected):
                target = wanted["target"]
                task_cost = float(task.get("cost") or 0.0)
                actual_cost += Decimal(str(task_cost))
                task_receipts.append({
                    "id": task.get("id"), "target": target,
                    "offset": wanted["offset"], "limit": wanted["limit"],
                    "status_code": task.get("status_code"),
                    "status_message": task.get("status_message"),
                    "cost_usd": task_cost,
                })
                if task.get("status_code") != 20000:
                    raise RailError(
                        f"dataforseo_ranked_keywords_task_failed:{target}:"
                        f"{task.get('status_code')}:{task.get('status_message')}")
                results = task.get("result") or []
                result = results[0] if results and isinstance(results[0], dict) else {}
                total = int(result.get("total_count") or 0)
                previous_total = domains[target]["total_count"]
                if previous_total is not None and previous_total != total:
                    raise RailError(
                        f"dataforseo_ranked_keywords_total_changed_mid_snapshot:"
                        f"{target}:{previous_total}:{total}")
                domains[target]["total_count"] = total
                if wanted["offset"] == 0:
                    domains[target]["metrics"] = result.get("metrics")
                    domains[target]["metrics_absolute"] = result.get("metrics_absolute")
                domains[target]["items"].extend(result.get("items") or [])
                domains[target]["pages"] += 1

        first = [request(target, 0) for target in clean_targets]
        try:
            for wanted in first:
                preflight()
                doc = self._post(path, [wanted], timeout=300)
                provider_calls += 1
                consume(doc, [wanted])
            remaining: list[dict[str, Any]] = []
            for target in clean_targets:
                total = int(domains[target]["total_count"] or 0)
                pages = max(1, math.ceil(total / page_size))
                if pages > max_pages_per_target:
                    raise RailError(
                        f"dataforseo_ranked_keywords_page_cap_exceeded:{target}:"
                        f"{pages}:{max_pages_per_target}")
                remaining.extend(request(target, offset)
                                 for offset in range(page_size, total, page_size))
            if remaining:
                for wanted in remaining:
                    preflight()
                    doc = self._post(path, [wanted], timeout=600)
                    provider_calls += 1
                    consume(doc, [wanted])
            if actual_cost > max_cost_usd:
                raise RailError(
                    f"dataforseo_ranked_keywords_actual_cost_cap_exceeded:"
                    f"{actual_cost:.6f}:{max_cost_usd:.6f}")
            for target, domain in domains.items():
                total = int(domain["total_count"] or 0)
                if len(domain["items"]) != total:
                    raise RailError(
                        f"dataforseo_ranked_keywords_incomplete:{target}:"
                        f"{len(domain['items'])}:{total}")
                domain["complete"] = True
            return {"ok": True, "domains": domains, "cost_usd": float(round(actual_cost, 6)),
                    "tasks": task_receipts, "provider_calls": provider_calls}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "domains": domains,
                    "cost_usd": float(round(actual_cost, 6)), "tasks": task_receipts,
                    "provider_calls": provider_calls, "error": str(exc)[:500]}

    def backlinks_summary(self, targets: list[str], *,
                          max_cost_usd: float = 1.0) -> dict[str, Any]:
        """Fetch live domain backlink summaries with bounded actual-cost receipts.

        The Backlinks Live API permits exactly one task per HTTP call. Domains are
        normalized to the provider's required no-scheme/no-www form. The complete
        result is retained because authority fields beyond backlinks and referring
        domains are useful for honest weekly movement reporting.
        """
        clean_targets = list(dict.fromkeys(
            str(value).strip().lower().removeprefix("https://").removeprefix("http://")
            .removeprefix("www.").rstrip("/")
            for value in targets if str(value).strip()
        ))
        if not clean_targets:
            return {"ok": True, "summaries": {}, "cost_usd": 0.0, "tasks": [],
                    "provider_calls": 0}
        if max_cost_usd <= 0:
            raise RailError("dataforseo_backlinks_summary_cost_cap_invalid")

        summaries: dict[str, dict[str, Any]] = {}
        task_receipts: list[dict[str, Any]] = []
        actual_cost = 0.0
        provider_calls = 0
        try:
            for target in clean_targets:
                observed = [row["cost_usd"] for row in task_receipts]
                projected = actual_cost + (max(observed) if observed else 0.0)
                if observed and projected > max_cost_usd:
                    raise RailError(
                        f"dataforseo_backlinks_summary_cost_cap_preflight:"
                        f"{projected:.6f}:{max_cost_usd:.6f}")
                doc = self._post("/backlinks/summary/live", [{
                    "target": target,
                    "include_subdomains": True,
                    "include_indirect_links": True,
                    "exclude_internal_backlinks": True,
                    "backlinks_status_type": "live",
                }], timeout=300)
                provider_calls += 1
                tasks = doc.get("tasks") or []
                if len(tasks) != 1:
                    raise RailError(
                        f"dataforseo_backlinks_summary_task_count_mismatch:{len(tasks)}:1")
                task = tasks[0]
                task_cost = float(task.get("cost") or 0.0)
                actual_cost += task_cost
                task_receipts.append({
                    "id": task.get("id"), "target": target,
                    "status_code": task.get("status_code"),
                    "status_message": task.get("status_message"),
                    "cost_usd": task_cost,
                })
                if task.get("status_code") != 20000:
                    raise RailError(
                        f"dataforseo_backlinks_summary_task_failed:{target}:"
                        f"{task.get('status_code')}:{task.get('status_message')}")
                results = task.get("result") or []
                if len(results) != 1 or not isinstance(results[0], dict):
                    raise RailError(f"dataforseo_backlinks_summary_result_missing:{target}")
                summaries[target] = results[0]
                if actual_cost > max_cost_usd:
                    raise RailError(
                        f"dataforseo_backlinks_summary_actual_cost_cap_exceeded:"
                        f"{actual_cost:.6f}:{max_cost_usd:.6f}")
            return {"ok": True, "summaries": summaries,
                    "cost_usd": round(actual_cost, 6), "tasks": task_receipts,
                    "provider_calls": provider_calls}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "summaries": summaries,
                    "cost_usd": round(actual_cost, 6), "tasks": task_receipts,
                    "provider_calls": provider_calls, "error": str(exc)[:500]}


# ---------------------------------------------------------------------------
# Bing Webmaster
# ---------------------------------------------------------------------------

class BingWebmaster:
    """First-party impressions/clicks/position + keyword research + fast indexing.

    Bing is ChatGPT's retrieval surface, so this rail carries double weight: it is
    both an SEO measurement and the closest thing we have to an AEO leading
    indicator. Submission quota is 100/day, 1200/mo and is SHARED with Cogny's
    Bing suite — single-submitter discipline: only the fast-index lane submits.
    """

    BASE = "https://ssl.bing.com/webmaster/api.svc/json"

    def __init__(self, site: str = BING_SITE) -> None:
        env = _read_env(BING_ENV)
        key = env.get("BING_WEBMASTER_API_KEY")
        if not key:
            raise RailError("bing_api_key_missing")
        self._key = key
        self.site = site

    def _url(self, method: str, params: dict[str, str] | None = None) -> str:
        query = {"apikey": self._key}
        query.update(params or {})
        return f"{self.BASE}/{method}?" + urllib.parse.urlencode(query)

    def _get(self, method: str, params: dict[str, str] | None = None) -> Any:
        doc = _http(self._url(method, params), timeout=90)
        return doc.get("d", doc)

    def _post(self, method: str, body: dict[str, Any]) -> Any:
        doc = _http(self._url(method), method="POST", body=body, timeout=90)
        return doc.get("d", doc)

    # -- measurement ------------------------------------------------------
    def query_stats(self) -> dict[str, Any]:
        try:
            rows = self._get("GetQueryStats", {"siteUrl": self.site}) or []
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:300], "rows": []}
        out = []
        for row in rows:
            out.append({
                "query": row.get("Query"),
                "impressions": row.get("Impressions"),
                "clicks": row.get("Clicks"),
                "avg_position": row.get("AvgImpressionPosition"),
                "avg_click_position": row.get("AvgClickPosition"),
            })
        return {"ok": True, "rows": out}

    def page_stats(self) -> dict[str, Any]:
        try:
            rows = self._get("GetPageStats", {"siteUrl": self.site}) or []
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:300], "rows": []}
        out = []
        for row in rows:
            out.append({
                "page": row.get("Query"),  # Bing returns the URL in the Query field here
                "impressions": row.get("Impressions"),
                "clicks": row.get("Clicks"),
                "avg_position": row.get("AvgImpressionPosition"),
            })
        return {"ok": True, "rows": out}

    def keyword(self, term: str, *, country: str = "us", language: str = "en-US") -> dict[str, Any]:
        try:
            row = self._get("GetKeyword", {
                "q": term, "country": country, "language": language,
                "startDate": _iso_days_ago(30), "endDate": _iso_days_ago(0),
            })
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "term": term, "error": str(exc)[:200]}
        if not isinstance(row, dict):
            return {"ok": False, "term": term, "error": "unexpected_shape"}
        return {"ok": True, "term": term, "impressions": row.get("Impressions"),
                "broad_impressions": row.get("BroadImpressions")}

    def related_keywords(self, term: str, *, country: str = "us",
                         language: str = "en-US") -> dict[str, Any]:
        try:
            rows = self._get("GetRelatedKeywords", {
                "q": term, "country": country, "language": language,
                "startDate": _iso_days_ago(30), "endDate": _iso_days_ago(0),
            }) or []
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "term": term, "error": str(exc)[:200], "rows": []}
        return {"ok": True, "term": term,
                "rows": [{"keyword": r.get("Query"), "impressions": r.get("Impressions")}
                         for r in rows if isinstance(r, dict)]}

    # -- submission (fast indexing) ---------------------------------------
    def submission_quota(self) -> dict[str, Any]:
        try:
            row = self._get("GetUrlSubmissionQuota", {"siteUrl": self.site}) or {}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:200]}
        return {"ok": True, "daily": row.get("DailyQuota"), "monthly": row.get("MonthlyQuota")}

    def submit_url_batch(self, urls: list[str]) -> dict[str, Any]:
        if not urls:
            return {"ok": True, "submitted": 0, "skipped": "empty_set"}
        try:
            self._post("SubmitUrlBatch", {"siteUrl": self.site, "urlList": urls})
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "submitted": 0, "error": str(exc)[:300]}
        return {"ok": True, "submitted": len(urls)}

    def submit_content(self, url: str, html: str, *, structured_markup: str = "") -> dict[str, Any]:
        """Push a page's CONTENT to Bing, not just its URL.

        ``httpMessage`` is not the HTML — it is a base64 **complete HTTP response
        message**: status line, headers, a bare CRLFCRLF separator, then the body.
        Sending naked HTML earns ``ErrorCode 2`` ("Couldnot find carriage return
        line feed separating http headers and content"), which is a confusing way
        for the API to say "you sent me a body with no envelope".
        """
        body = html.encode("utf-8")
        message = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "\r\n"
        ).encode("ascii") + body
        try:
            self._post("SubmitContent", {
                "siteUrl": self.site,
                "url": url,
                "httpMessage": base64.b64encode(message).decode("ascii"),
                "structuredMarkup": structured_markup,
            })
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "url": url, "error": str(exc)[:300]}
        return {"ok": True, "url": url, "bytes": len(body)}


def _iso_days_ago(days: int) -> str:
    import datetime as _dt
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Google (GSC + GA4) — direct user-OAuth rail
# ---------------------------------------------------------------------------

class GoogleRail:
    """Direct Google rail. Refreshes the stored user-OAuth token in-process.

    NEVER ``.with_subject()`` for analytics/GSC (see the google-workspace memory —
    impersonation breaks these two APIs). This rail is the only one that reaches
    ``sc-domain:glpclinicscout.com``.
    """

    def __init__(self, token_path: Path = GOOGLE_TOKEN) -> None:
        if not token_path.is_file():
            raise RailError("google_token_missing")
        self._path = token_path
        self._token: str | None = None

    def _access_token(self) -> str:
        if self._token:
            return self._token
        doc = json.loads(self._path.read_text(encoding="utf-8"))
        body = urllib.parse.urlencode({
            "client_id": doc["client_id"], "client_secret": doc["client_secret"],
            "refresh_token": doc["refresh_token"], "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(
            doc.get("token_uri", "https://oauth2.googleapis.com/token"), data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as resp:
            fresh = json.loads(resp.read().decode("utf-8"))
        self._token = fresh["access_token"]
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self._access_token()}

    # -- GSC ---------------------------------------------------------------
    def gsc_search_analytics(self, *, property_uri: str = GSC_DOMAIN_PROPERTY,
                             start_date: str, end_date: str,
                             dimensions: list[str] | None = None,
                             row_limit: int = 1000,
                             fetch_all: bool = False,
                             search_type: str | None = None,
                             data_state: str | None = None,
                             aggregation_type: str | None = None) -> dict[str, Any]:
        endpoint = ("https://searchconsole.googleapis.com/webmasters/v3/sites/"
                    + urllib.parse.quote(property_uri, safe="") + "/searchAnalytics/query")
        rows = []
        pages = 0
        start_row = 0
        row_limit = max(1, min(25000, int(row_limit)))
        try:
            while True:
                doc = _http(endpoint, method="POST", headers=self._headers(), body={
                    "startDate": start_date, "endDate": end_date,
                    "dimensions": ["query"] if dimensions is None else dimensions,
                    "rowLimit": row_limit, "startRow": start_row,
                    **({"type": search_type} if search_type is not None else {}),
                    **({"dataState": data_state} if data_state is not None else {}),
                    **({"aggregationType": aggregation_type} if aggregation_type is not None else {}),
                })
                page = doc.get("rows") or []
                pages += 1
                for row in page:
                    keys = row.get("keys", [])
                    if not isinstance(keys, list):
                        raise ValueError("invalid_gsc_keys_shape")
                    rows.append({
                        "keys": keys,
                        "clicks": row.get("clicks"), "impressions": row.get("impressions"),
                        "ctr": row.get("ctr"), "position": row.get("position"),
                    })
                if not fetch_all or len(page) < row_limit:
                    break
                start_row += len(page)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "property": property_uri, "error": str(exc)[:300],
                    "rows": rows, "pages": pages, "complete": False}
        return {"ok": True, "property": property_uri, "rows": rows,
                "pages": pages, "complete": True}

    def gsc_url_inspection(self, inspection_url: str, *,
                           property_uri: str = GSC_DOMAIN_PROPERTY,
                           language_code: str = "en-US") -> dict[str, Any]:
        """Read Google's canonical index verdict for one URL.

        Search Analytics proves impressions, not index coverage. The URL
        Inspection API is the authoritative per-URL read for whether Google has
        indexed a published route and, when it has not, why. This method is
        deliberately read-only; sitemap submission remains a separate write
        surface below.
        """
        endpoint = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
        try:
            doc = _http(endpoint, method="POST", headers=self._headers(), body={
                "inspectionUrl": inspection_url,
                "siteUrl": property_uri,
                "languageCode": language_code,
            })
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "property": property_uri, "url": inspection_url,
                    "error": str(exc)[:300]}
        result = doc.get("inspectionResult") or {}
        index = result.get("indexStatusResult") or {}
        return {
            "ok": True,
            "property": property_uri,
            "url": inspection_url,
            "inspection_result_link": result.get("inspectionResultLink"),
            "verdict": index.get("verdict"),
            "coverage_state": index.get("coverageState"),
            "robots_txt_state": index.get("robotsTxtState"),
            "indexing_state": index.get("indexingState"),
            "last_crawl_time": index.get("lastCrawlTime"),
            "page_fetch_state": index.get("pageFetchState"),
            "google_canonical": index.get("googleCanonical"),
            "user_canonical": index.get("userCanonical"),
            "crawled_as": index.get("crawledAs"),
            "referring_urls": index.get("referringUrls") or [],
        }

    def gsc_submit_sitemap(self, sitemap_url: str,
                           property_uri: str = GSC_DOMAIN_PROPERTY) -> dict[str, Any]:
        endpoint = ("https://searchconsole.googleapis.com/webmasters/v3/sites/"
                    + urllib.parse.quote(property_uri, safe="") + "/sitemaps/"
                    + urllib.parse.quote(sitemap_url, safe=""))
        try:
            _http(endpoint, method="PUT", headers=self._headers())
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "sitemap": sitemap_url, "error": str(exc)[:300]}
        return {"ok": True, "sitemap": sitemap_url}

    # -- GA4 ---------------------------------------------------------------
    #
    # ANALYTICS-TRUTH 2026-08-20. Property 541440529 carried 20,617 of 20,958
    # sessions and 103,013 of 104,807 events on hostName 127.0.0.1 — our own local
    # test runs firing the production tag. Any unfiltered read of this property is
    # wrong by roughly 60x, and it was: every CVR, engagement rate and Phase-8 ads
    # baseline computed from this rail was built on that number. Host filtering is
    # therefore the DEFAULT here, not an option a caller has to remember, because
    # the failure mode of forgetting is a plausible-looking wrong answer rather
    # than an error. Pass host_filter=None deliberately to read raw.
    GA4_PRODUCTION_HOSTS = ("glpclinicscout.com",)

    @staticmethod
    def ga4_host_filter(hosts: tuple[str, ...] = GA4_PRODUCTION_HOSTS) -> dict[str, Any]:
        """Exact production hosts only; local/subdomain events are never included."""
        expressions = [{"filter": {"fieldName": "hostName",
                                   "stringFilter": {"matchType": "EXACT", "value": host}}}
                       for host in hosts]
        if len(expressions) == 1:
            return expressions[0]
        return {"orGroup": {"expressions": expressions}}

    def ga4_run_report(self, *, property_id: str = GA4_GLP_PROPERTY,
                       start_date: str, end_date: str,
                       dimensions: list[str], metrics: list[str],
                       limit: int = 500,
                       host_filter: Any = "default",
                       dimension_filter: dict[str, Any] | None = None,
                       fetch_all: bool = False) -> dict[str, Any]:
        endpoint = f"https://analyticsdata.googleapis.com/v1beta/properties/{property_id}:runReport"
        if host_filter == "default":
            host_filter = self.ga4_host_filter()
        filters = [f for f in (host_filter, dimension_filter) if f]
        limit = max(1, min(250000, int(limit)))
        body: dict[str, Any] = {
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": d} for d in dimensions],
            "metrics": [{"name": m} for m in metrics],
            "limit": limit,
        }
        if len(filters) == 1:
            body["dimensionFilter"] = filters[0]
        elif filters:
            body["dimensionFilter"] = {"andGroup": {"expressions": filters}}
        rows = []
        pages = 0
        offset = 0
        expected_row_count = None
        try:
            while True:
                body["offset"] = offset
                doc = _http(endpoint, method="POST", headers=self._headers(), body=body)
                if not isinstance(doc, dict):
                    raise ValueError("invalid_ga4_report_shape")
                dimension_headers = doc.get("dimensionHeaders")
                metric_headers = doc.get("metricHeaders")
                if (not isinstance(dimension_headers, list)
                        or not isinstance(metric_headers, list)
                        or any(not isinstance(h, dict) for h in dimension_headers + metric_headers)
                        or [h.get("name") for h in dimension_headers] != dimensions
                        or [h.get("name") for h in metric_headers] != metrics):
                    raise ValueError("invalid_ga4_report_headers")
                # Protobuf JSON can omit zero rowCount and empty rows, but only
                # an authenticated response schema can establish an empty read.
                page = doc.get("rows", [])
                row_count = doc.get("rowCount", 0)
                if (not isinstance(page, list) or type(row_count) is not int
                        or row_count < 0 or len(page) > limit
                        or len(rows) + len(page) > row_count
                        or (expected_row_count is not None and row_count != expected_row_count)):
                    raise ValueError("invalid_ga4_report_count")
                expected_row_count = row_count
                pages += 1
                for row in page:
                    if not isinstance(row, dict):
                        raise ValueError("invalid_ga4_report_row")
                    dimension_values = row.get("dimensionValues")
                    metric_values = row.get("metricValues")
                    if (not isinstance(dimension_values, list) or not isinstance(metric_values, list)
                            or len(dimension_values) != len(dimensions) or len(metric_values) != len(metrics)
                            or any(not isinstance(v, dict) or not isinstance(v.get("value"), str)
                                   for v in dimension_values + metric_values)):
                        raise ValueError("invalid_ga4_report_row")
                    rows.append({"dimensions": [v["value"] for v in dimension_values],
                                 "metrics": [v["value"] for v in metric_values]})
                if not page and len(rows) < row_count:
                    raise ValueError("incomplete_ga4_report")
                if not fetch_all or len(rows) == row_count:
                    break
                offset += len(page)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:300], "rows": [],
                    "pages": pages, "complete": False, "host_filtered": bool(host_filter)}
        return {"ok": True, "dimension_names": [h["name"] for h in dimension_headers],
                "metric_names": [h["name"] for h in metric_headers],
                "rows": rows, "host_filtered": bool(host_filter),
                "host_filter": host_filter, "pages": pages, "complete": len(rows) == row_count}


# ---------------------------------------------------------------------------
# PostHog
# ---------------------------------------------------------------------------

class PostHog:
    """HogQL over project 508172. ``first``/``last`` are reserved words — alias them."""

    def __init__(self) -> None:
        if not POSTHOG_KEY.is_file():
            raise RailError("posthog_key_missing")
        self._key = POSTHOG_KEY.read_text(encoding="utf-8").strip()

    def hogql(self, query: str, timeout: int = 120) -> dict[str, Any]:
        try:
            doc = _http(POSTHOG_QUERY_URL, method="POST",
                        headers={"Authorization": "Bearer " + self._key},
                        body={"query": {"kind": "HogQLQuery", "query": query}},
                        timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)[:300], "rows": []}
        return {"ok": True, "columns": doc.get("columns") or [], "rows": doc.get("results") or []}

    def page_conversion(self, days: int = 30, host: str = "glpclinicscout.com") -> dict[str, Any]:
        """Per-path pageviews and conversion events for one host — the money metric per page.

        Two shapes are counted because the site emits both: ``affiliate_click`` (the
        revenue event, grammar shipped 2026-08-20) and the softer intent events
        (``cta_click``, ``scorecard_complete``). They are reported SEPARATELY —
        a soft click is not revenue and must never be summed into one number.

        The host filter matters: project 508172 carries Boring AI *and* GLP traffic,
        so an unfiltered query silently mixes two businesses' funnels.
        """
        query = f"""
        SELECT
          path(properties.$current_url) AS page_path,
          countIf(event IN ('page_view', '$pageview')) AS views,
          countIf(event = 'affiliate_click') AS affiliate_clicks,
          countIf(event IN ('cta_click', 'scorecard_complete', 'checkout_start')) AS intent_events
        FROM events
        WHERE timestamp > now() - INTERVAL {int(days)} DAY
          AND domain(properties.$current_url) = '{host}'
        GROUP BY page_path
        HAVING views > 0
        ORDER BY views DESC
        LIMIT 500
        """
        result = self.hogql(query)
        if not result.get("ok"):
            return result
        rows = []
        for page_path, views, affiliate_clicks, intent_events in result["rows"]:
            views_i = int(views or 0)
            rows.append({
                "path": page_path, "views": views_i,
                "clicks": int(affiliate_clicks or 0),
                "intent_events": int(intent_events or 0),
                "cvr": (float(affiliate_clicks or 0) / views_i) if views_i else 0.0,
            })
        return {"ok": True, "host": host, "window_days": days, "rows": rows,
                "total_views": sum(r["views"] for r in rows),
                "total_affiliate_clicks": sum(r["clicks"] for r in rows)}


# ---------------------------------------------------------------------------
# convenience
# ---------------------------------------------------------------------------

def rail_status() -> dict[str, Any]:
    """One-shot readiness probe of every rail. Used by lane pre-flight + the report."""
    status: dict[str, Any] = {}
    try:
        status["dataforseo"] = {"ok": True, "balance_usd": round(DataForSEO().balance(), 4)}
    except Exception as exc:  # noqa: BLE001
        status["dataforseo"] = {"ok": False, "error": str(exc)[:200]}
    try:
        status["bing"] = BingWebmaster().submission_quota()
    except Exception as exc:  # noqa: BLE001
        status["bing"] = {"ok": False, "error": str(exc)[:200]}
    try:
        rail = GoogleRail()
        probe = rail.gsc_search_analytics(start_date=_iso_days_ago(10),
                                          end_date=_iso_days_ago(3), row_limit=1)
        status["gsc_domain_property"] = {"ok": probe["ok"], "rows": len(probe.get("rows") or []),
                                          "error": probe.get("error")}
        ga4 = rail.ga4_run_report(start_date=_iso_days_ago(8), end_date=_iso_days_ago(1),
                                  dimensions=["date"], metrics=["sessions"], limit=1)
        status["ga4"] = {"ok": ga4["ok"], "rows": len(ga4.get("rows") or []),
                         "error": ga4.get("error")}
    except Exception as exc:  # noqa: BLE001
        status["google"] = {"ok": False, "error": str(exc)[:200]}
    try:
        ph = PostHog().hogql("SELECT count() FROM events WHERE timestamp > now() - INTERVAL 7 DAY")
        status["posthog"] = {"ok": ph["ok"], "error": ph.get("error")}
    except Exception as exc:  # noqa: BLE001
        status["posthog"] = {"ok": False, "error": str(exc)[:200]}
    return status


if __name__ == "__main__":
    print(json.dumps(rail_status(), indent=2, sort_keys=True))
