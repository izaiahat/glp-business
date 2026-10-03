#!/usr/bin/env python3
"""Portal APPLICATION executors — the last edge of the application pipeline.

`affiliate_auto_apply.py` plans applications and executes the ones whose network
publishes a join API. Everything else lands in the portal queue with status
`queued_for_portal_execution` and, until this module existed, stayed there.
This is the executor that actually SUBMITS those applications.

Contract (operator 2026-08-06):
  * Per APPLICATION, not per batch: every submit is preceded by its own exact
    program-id preflight validated by `affiliate_application_executor_gate.py`.
    One bad row can no longer void a whole network's batch, and a submit can
    only ever fire for the exact identity the gate approved.
  * Fail-closed: no gate PASS, no capture, no unambiguous single submit control
    -> BLOCKED with the operator step. The lane never improvises a form.
  * ONE submit per program per run, ONE login attempt per portal per run
    (`portal_lane.AttemptBudget`). Executors that reuse a live session take no
    login attempt at all.
  * Never clicks affiliate TRACKING links. Application forms only.
  * Every application writes a receipt: program, network, submitted_at, masked
    screenshot evidence, confirmation state.
  * The applications ledger is the double-application guard; it is updated the
    same way the API path updates it (in place, keyed network:program_id).

Providers are reached ONLY through an injected driver, which is what keeps the
test suite provider-free.
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
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import _base as b
from . import portal_lane as pl
from scripts.affiliate_contract_policy import (
    MECHANICAL_TRIGGER_CODES,
    blocked_account_action_hits,
    genuine_legal_prohibition_hits,
    mechanical_hard_trigger_hits,
)
from scripts import affiliate_operator_rulings as operator_rulings
from scripts.affiliate_blocker_completeness import completeness_proof
from scripts.affiliate_niche_geo_filter import choke_candidate
from scripts.affiliate_fresh_browser import (
    TRANSPORT as FRESH_BROWSER_TRANSPORT, fresh_context, login_wall, refuse_persistent,
)
from scripts.awin_api_first_application import (
    AuthenticatedAwinCDPAdapter, AwinApiFirstJoin, OfficialAwinReadClient,
)
from scripts.glp_item_e_application_queue import validate_execution_authority_manifest

BUSINESS = b.BUSINESS
MACHINE_DIR = BUSINESS / "reports/glp-clinic-scout/affiliate-machine"
LEDGER_PATH = MACHINE_DIR / "applications-ledger.json"
APPLY_ROOT = MACHINE_DIR / "portal-apply"
AUTHORITY_PATH = BUSINESS / "reports/glp-affiliate-expansion-20260731/02-application-authority.json"
GATE_SCRIPT = BUSINESS / "scripts/affiliate_application_executor_gate.py"
KATALYS_ORG = "098e5455-ca72-4099-9ff9-a9dd014148e7"
DECISIONS_CHANNEL = "1524254375453786294"

QUEUED_STATUS = "queued_for_portal_execution"
TERMINAL_STATUSES = {
    "SUBMITTED_VERIFIED", "SUBMISSION_UNVERIFIED", "ALREADY_RELATIONSHIP",
    "APPROVED_VERIFIED", "JOINED_VERIFIED", "AUTO_APPROVED_VERIFIED", "PENDING_VERIFIED",
    "terminal_provider_unavailable",
}

# The 11 gate flags. An executor sets them from what it OBSERVED; anything it
# could not observe stays True (i.e. blocking), never optimistically False.
GATE_FLAGS = (
    "requires_account_creation", "requires_signature", "requires_kyc",
    "requires_tax_action", "requires_banking_or_payment", "requires_security_change",
    "requires_provider_outreach", "has_unresolved_custom_terms",
    "has_unsupported_medical_claims", "creates_phi_role", "brand_sensitive",
)
AWIN_TRACKING_RE = re.compile(r"(?:click|track|redirect|deeplink|linkbuilder)", re.I)
AWIN_PUBLISHER_ID = "2979387"
AWIN_TERMS_PDF_TOOL = Path("/usr/bin/mutool")
AWIN_TERMS_PDF_TOOL_SHA256 = "9203440040cc38ee412aeedbfe57f452d6b85709c5b1600ca6ab2aa05478ab73"
AWIN_TERMS_PDF_MAX_BYTES = 5 * 1024 * 1024
AWIN_TERMS_TEXT_MAX_BYTES = 1024 * 1024
AWIN_ACCOUNT_NAME = "Tilton Group"
AWIN_PROFILE_TEMPLATE = (
    "https://ui.awin.com/awin/affiliate/2979387/merchant-profile/{program_id}"
)
AWIN_DIRECTORY_PATH = "/awin/affiliate/2979387/merchant-directory/index/tab/active/page/1"
AWIN_SITE_INFORMATION_URL = (
    "https://ui.awin.com/awin/affiliate/2979387/settings/site-information"
)
AWIN_PROFILE_PATH_RE = re.compile(
    rf"^/awin/affiliate/(?P<publisher_id>{AWIN_PUBLISHER_ID})/merchant-profile/(?P<program_id>[0-9]+)$"
)
AWIN_PUBLISHER_SCOPE_RE = re.compile(r"^/awin/affiliate/(?P<publisher_id>[^/]+)(?:/|$)")
AWIN_AUTH_TITLE_RE = re.compile(
    r"(?:\bsign[\s-]*in\b|\blog[\s-]*in\b|\blogin\b|\boauth\b|"
    r"\bauthentication\b|\bauthorization\b|\bsession\b.*\bexpired\b|"
    r"\baccess\s+denied\b|\bunauthorized\b|\bforbidden\b|\berror\b)",
    re.I,
)


def extract_awin_terms_pdf_text(raw: bytes) -> str:
    """Extract stable contract text from Awin's volatile PDF container."""
    if (not isinstance(raw, bytes) or not raw.startswith(b"%PDF-")
            or len(raw) > AWIN_TERMS_PDF_MAX_BYTES):
        return ""
    try:
        tool_stat = AWIN_TERMS_PDF_TOOL.lstat()
        if (not stat.S_ISREG(tool_stat.st_mode) or tool_stat.st_uid != 0
                or tool_stat.st_nlink != 1 or tool_stat.st_mode & 0o022):
            return ""
        if hashlib.sha256(AWIN_TERMS_PDF_TOOL.read_bytes()).hexdigest() != AWIN_TERMS_PDF_TOOL_SHA256:
            return ""
        with tempfile.TemporaryDirectory(prefix="awin-terms-") as directory:
            root = Path(directory)
            source = root / "terms.pdf"
            output = root / "terms.txt"
            descriptor = os.open(
                source, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            completed = subprocess.run(
                (str(AWIN_TERMS_PDF_TOOL), "draw", "-F", "txt", "-o", str(output), str(source)),
                cwd=root,
                env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                return ""
            output_stat = output.lstat()
            if (not stat.S_ISREG(output_stat.st_mode) or output_stat.st_nlink != 1
                    or output_stat.st_size <= 0 or output_stat.st_size > AWIN_TERMS_TEXT_MAX_BYTES):
                return ""
            text = output.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError, subprocess.SubprocessError):
        return ""
    return " ".join(text.split())


def _awin_identity_route(url: str) -> dict[str, Any]:
    """Classify exact raw Awin UI URL bytes without parser normalization gaps.

    ``publisher_scoped`` marks every parsed publisher route before authority
    allowlisting, including alternate or malformed hosts, ports, foreign
    accounts and non-allowlisted routes. Those forms are terminal identity
    failures rather than fallback cues.
    ``allowed`` additionally requires an exact raw HTTPS authority spelling,
    no raw query/fragment delimiter (including bare ``?``/``#``), and one of
    the two exact publisher route grammars.
    """
    raw_url = str(url)
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
    except (TypeError, ValueError):
        return {"publisher_scoped": False, "allowed": False, "publisher_id": ""}
    scope = AWIN_PUBLISHER_SCOPE_RE.match(parsed.path)
    # Route scope is established before authority allowlisting. An alternate,
    # encoded or malformed authority on an Awin publisher path is a terminal
    # hostile observation, not evidence that no publisher surface exists.
    publisher_scoped = scope is not None
    if not publisher_scoped:
        return {"publisher_scoped": False, "allowed": False, "publisher_id": ""}
    publisher_id = str(scope.group("publisher_id"))
    try:
        port = parsed.port
        port_valid = True
    except ValueError:
        # A malformed official scoped authority must remain terminal and may
        # never be reclassified as an absent publisher surface for fallback.
        port = None
        port_valid = False
    raw_authority = ""
    if raw_url.startswith("https://"):
        remainder = raw_url[len("https://"):]
        delimiters = [index for index in (
            remainder.find("/"), remainder.find("?"), remainder.find("#"),
        ) if index >= 0]
        end = min(delimiters) if delimiters else len(remainder)
        raw_authority = remainder[:end]
    exact_origin = (
        parsed.scheme == "https"
        and raw_url.startswith("https://")
        and raw_authority in ("ui.awin.com", "ui.awin.com:443")
        and parsed.netloc == raw_authority
        and hostname == "ui.awin.com"
        and parsed.username is None
        and parsed.password is None
        and port_valid
        and port in (None, 443)
        and "?" not in raw_url
        and "#" not in raw_url
    )
    allowed_path = parsed.path == AWIN_DIRECTORY_PATH or AWIN_PROFILE_PATH_RE.fullmatch(parsed.path) is not None
    return {
        "publisher_scoped": True,
        "allowed": bool(exact_origin and publisher_id == AWIN_PUBLISHER_ID and allowed_path),
        "publisher_id": publisher_id,
    }


def _plain_json(value: Any) -> Any:
    """Copy a deeply frozen authority value into JSON-native containers."""
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        _plain_json(value), sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


class _AuthenticatedAwinQueueRow(dict):
    """In-process result of exact manifest validation and queue-row binding.

    Generic JSON/application-plan rows cannot manufacture this type.  The seal
    also makes post-bind mutation fail closed when ``_row_from_queue`` consumes
    it.  Executor-facing rows remain ordinary dictionaries after that check.
    """

    __slots__ = (
        "_authority_row_sha256", "_projection_sha256", "_choke_identity", "_authorized_lane",
    )

    def __init__(self, value: dict[str, Any], *, authority_row_sha256: str,
                 choke_identity: str, authorized_lane: str):
        dict.__init__(self, value)
        self._authority_row_sha256 = str(authority_row_sha256)
        self._choke_identity = str(choke_identity)
        self._authorized_lane = str(authorized_lane)
        self._projection_sha256 = _sha256_json(value)

    def verify_projection(self) -> None:
        if (_sha256_json(self) != self._projection_sha256
                or not re.fullmatch(r"[0-9a-f]{64}", self._authority_row_sha256)
                or self._choke_identity != f"awin:{str(self.get('program_id') or '')}"
                or not authority_lane(self._authorized_lane)):
            raise ValueError("awin_authenticated_queue_projection_drift")

# The auto-apply lane's vertical lanes are finer-grained than the authorization
# manifest's. This is a MAPPING, not a widening: every target lane below is one
# the operator authorized on 2026-07-31. An unmapped lane is not authorized and
# the row is blocked rather than relabelled.
LANE_MAP = {
    "glp1-weightloss": "glp1-weightloss",
    "mens-health-trt": "mens-health-trt",
    "mens-health-ed": "mens-health-ed",
    "mens-health-hair": "mens-health-hair",
    "hair-loss": "mens-health-hair",
    "peptides": "peptides-longevity",
    "peptides-longevity": "peptides-longevity",
    "diagnostics-testing": "diagnostics-testing",
    "supplements-nutrition": "supplements-nutrition",
    "supplements": "supplements-nutrition",
    "general-wellness": "general-wellness",
    # The exact 2026-08-13 niche-classifier breadth-tier aliases
    # (telehealth_clinical, compounding_pharmacy, glp_adjacent_labs) were
    # REMOVED on 2026-09-17: those tiers are no longer application fits
    # (receipt ops/linear/approvals/OPERATOR-DECISION-20260917-apply-scope-
    # glp1-telehealth-only-clickdealer.json), so nothing may relabel them into
    # an authorized parent lane.  Unmapped means unauthorized, which is why
    # `apply-scope-not-authorized` (affiliate_niche_geo_filter.
    # UNAUTHORIZED_APPLY_LANE) is deliberately absent here too.
    "telehealth-general": "other",
    "beauty-skincare": "other",
    "other": "other",
}

CREDENTIAL_FIELD_RE = re.compile(
    r"(?i)pass|secret|token|api[_-]?key|otp|2fa|mfa|cvv|card|ssn|social security|"
    r"routing|account number|tax|w-?9|iban|swift")


# --------------------------------------------------------------------- utils

def now() -> str:
    return b.utc()


def compact(text: str, limit: int = 30000) -> str:
    return "\n".join(line.rstrip() for line in str(text or "").splitlines())[:limit]


def normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def write_private(path: Path, payload: Any) -> str:
    """0600 JSON write, returning the sha256 the gate will bind against."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path.parent, pl.DIR_MODE)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, pl.FILE_MODE)
    temporary.replace(path)
    os.chmod(path, pl.FILE_MODE)
    return b.sha256_file(path)


def identity(network: str, program_id: str) -> str:
    return f"{network}:{program_id}"


def authority_lane(lane: str) -> str:
    return LANE_MAP.get(str(lane or "").strip().lower(), "")


def classify_terms(text: str) -> dict:
    """Terms completeness plus the one canonical consent policy.

    The mechanical vocabulary is exactly the seven centrally defined trigger
    codes. Legality and operator-owned account actions are separate gates and
    can never silently widen that mechanical list.
    """
    body = str(text or "")
    lowered = body.lower()
    markers = ("commission", "payout", "terms", "agreement", "affiliate program",
               "program description", "cookie", "payment")
    hits = [marker for marker in markers if marker in lowered]
    complete = len(body) >= 400 and len(hits) >= 2
    mechanical = mechanical_hard_trigger_hits(body)
    legal = genuine_legal_prohibition_hits(body)
    account = blocked_account_action_hits(body)
    risks = [row["trigger"] for row in mechanical]
    assert all(code in MECHANICAL_TRIGGER_CODES for code in risks)
    blocked = bool(mechanical or legal or account)
    return {
        "captured_complete": bool(complete),
        "compatible": bool(complete and not blocked),
        "has_special_terms": blocked,
        "risk_classes": risks,
        "mechanical_trigger_hits": mechanical,
        "legal_prohibition_hits": legal,
        "blocked_account_action_hits": account,
        "marker_hits": hits,
        "text_chars": len(body),
    }


# -------------------------------------------------------------------- driver

FORM_SCAN_JS = """
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const labelFor = el => {
    let t = '';
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) t += ' ' + l.innerText;
    }
    const p = el.closest('label');
    if (p) t += ' ' + p.innerText;
    return t.replace(/\\s+/g, ' ').trim().slice(0, 160);
  };
  const forms = [];
  document.querySelectorAll('form').forEach((form, i) => {
    if (!vis(form)) return;
    form.setAttribute('data-glp-form', String(i));
    const fields = [];
    form.querySelectorAll('input, textarea, select').forEach((el, j) => {
      const type = (el.type || el.tagName).toLowerCase();
      if (type === 'hidden') return;
      el.setAttribute('data-glp-field', i + '-' + j);
      fields.push({
        key: i + '-' + j,
        tag: el.tagName.toLowerCase(),
        type: type,
        name: el.name || '',
        id: el.id || '',
        placeholder: el.placeholder || '',
        aria: el.getAttribute('aria-label') || '',
        label: labelFor(el),
        required: !!(el.required || el.getAttribute('aria-required') === 'true'),
        visible: vis(el),
        options: el.tagName === 'SELECT'
          ? Array.from(el.options).map(o => o.value).slice(0, 60) : [],
      });
    });
    const submits = [];
    form.querySelectorAll("button, input[type='submit'], input[type='button']").forEach((el, k) => {
      if (!vis(el)) return;
      el.setAttribute('data-glp-submit', i + '-' + k);
      const t = (el.innerText || el.value || '').replace(/\\s+/g, ' ').trim();
      const type = (el.getAttribute('type') || '').toLowerCase();
      if (type === 'reset') return;
      submits.push({key: i + '-' + k, label: t.slice(0, 120)});
    });
    forms.push({
      index: i,
      action: form.getAttribute('action') || '',
      text: (form.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 4000),
      fields: fields,
      submits: submits,
      file_inputs: form.querySelectorAll("input[type='file']").length,
    });
  });
  return forms;
}
"""


class PlaywrightApplyDriver(pl.PlaywrightDriver):
    """Legacy compatibility surface; start refuses portal Playwright transport.

    Dialog/form helpers remain for older importers, but no persistent or empty
    Playwright browser is launched. Fresh vault reads use an explicitly injected
    first-class AI browser driver; this name does not establish access.
    """

    def __init__(self, profile_dir: Path | str = "", *, fresh: bool = False,
                 cdp_url: str = "", **kwargs):
        refuse_persistent(cdp_url)
        super().__init__(Path(profile_dir or "/home/ubuntu/.hermes/cache/scratch/portal-apply-unused"), **kwargs)
        self.fresh = fresh
        self._scopes: dict[str, Any] = {}
        self._fresh_stack: contextlib.ExitStack | None = None

    def start(self):
        # Empty Playwright contexts are still forbidden affiliate portal access.
        # Only an injected first-class AI browser can consume a vault login;
        # starting another browser would lose that login and its ownership proof.
        from scripts.affiliate_fresh_browser import FreshAIBrowserRequired
        raise FreshAIBrowserRequired("fresh_ai_browser_vault_stage_required")

    def close(self) -> None:
        # No legacy superclass cleanup: this compatibility driver never starts
        # a persistent context or listener and must not touch another consumer.
        return None

    # -- scopes --------------------------------------------------------------
    def _scope(self, scope: str = ""):
        return self._scopes[scope] if scope else self.page

    def buttons(self, scope: str = "") -> list[str]:
        target = self._scope(scope).locator("button:visible")
        names: list[str] = []
        for index in range(target.count()):
            try:
                names.append(" ".join(target.nth(index).inner_text().split())[:160])
            except Exception:  # noqa: BLE001 - a button that vanished is not a name
                continue
        return names

    def click_button(self, label: str, scope: str = "") -> None:
        target = self._scope(scope).locator("button:visible")
        for index in range(target.count()):
            button = target.nth(index)
            with contextlib.suppress(Exception):
                if " ".join(button.inner_text().split()) == label:
                    button.click()
                    return
        raise RuntimeError(f"button_not_found:{label}")

    def button_enabled(self, label: str, scope: str = "") -> bool:
        target = self._scope(scope).locator("button:visible")
        for index in range(target.count()):
            button = target.nth(index)
            with contextlib.suppress(Exception):
                if " ".join(button.inner_text().split()) == label:
                    return bool(button.is_enabled())
        return False

    def open_dialog(self, contains: str, scope_id: str = "dialog") -> str:
        dialogs = self.page.locator('[role="dialog"]:visible')
        for index in range(dialogs.count()):
            candidate = dialogs.nth(index)
            with contextlib.suppress(Exception):
                if contains.lower() in candidate.inner_text().lower():
                    self._scopes[scope_id] = candidate
                    return scope_id
        return ""

    def scope_text(self, scope: str = "", limit: int | None = None) -> str:
        if not scope:
            return self.body_text(limit)
        try:
            text = self._scopes[scope].inner_text(timeout=15000)
        except Exception:  # noqa: BLE001
            text = ""
        return text[:limit] if limit else text

    def scope_count(self, selector: str, scope: str = "") -> int:
        try:
            return self._scope(scope).locator(selector).count()
        except Exception:  # noqa: BLE001
            return 0

    def scope_fill(self, selector: str, value: str, scope: str = "", index: int = 0) -> None:
        self._scope(scope).locator(selector).nth(index).fill(value)

    def scope_check(self, selector: str, index: int, scope: str = "") -> None:
        self._scope(scope).locator(selector).nth(index).check()

    def press_escape(self) -> None:
        with contextlib.suppress(Exception):
            self.page.keyboard.press("Escape")

    # -- generic form introspection -----------------------------------------
    def scan_forms(self) -> list[dict]:
        try:
            return list(self.page.evaluate(FORM_SCAN_JS) or [])
        except Exception:  # noqa: BLE001
            return []

    def fill_field(self, key: str, value: str) -> None:
        self.page.locator(f"[data-glp-field='{key}']").first.fill(value)

    def select_field(self, key: str, value: str) -> None:
        self.page.locator(f"[data-glp-field='{key}']").first.select_option(value)

    def check_field(self, key: str) -> None:
        self.page.locator(f"[data-glp-field='{key}']").first.check()

    def click_submit(self, key: str) -> None:
        self.page.locator(f"[data-glp-submit='{key}']").first.click()

    def links(self, limit: int = 400) -> list[dict]:
        try:
            return list(self.page.evaluate(
                """(n) => Array.from(document.querySelectorAll('a[href]')).slice(0, n)
                       .map(a => ({href: a.href,
                                   text: (a.innerText||'').replace(/\\s+/g,' ').trim().slice(0,120)}))""",
                limit) or [])
        except Exception:  # noqa: BLE001
            return []


class AwinAuthFailure(RuntimeError):
    """One deterministic, non-secret run-level Awin authentication failure."""

    def __init__(self, failed_step: str, detail: str):
        if (not re.fullmatch(r"[a-z0-9_]{3,80}", str(failed_step))
                or not re.fullmatch(r"[a-zA-Z0-9_.:-]{3,160}", str(detail))):
            failed_step, detail = "sanitize_auth_failure", "unsafe_failure_detail_rejected"
        super().__init__(f"{failed_step}:{detail}")
        self.failed_step = str(failed_step)
        self.detail = str(detail)


class AwinFreshBrowserTransport:
    """API-only identity or an injected fresh AI/vault driver (GLP-268).

    API identity creates no browser and grants no portal-write authority. An
    injected driver consumes an already-owned fresh vault stage; no password,
    CAPTCHA solver, saved profile or CDP endpoint is accepted here. Legacy
    shell callers without an API identity stop before portal I/O.
    """

    PROFILE = AWIN_PROFILE_TEMPLATE
    PROGRAMS = "https://ui.awin.com" + AWIN_DIRECTORY_PATH

    def __init__(self, endpoint: str = "", *, driver: Any | None = None,
                 api_identity: dict[str, Any] | None = None):
        if endpoint:
            raise AwinAuthFailure("launch_fresh_browser", "persistent_cdp_endpoint_refused")
        self.endpoint = ""
        self.driver = driver
        self.api_identity = dict(api_identity or {})
        self._auth_report: dict[str, Any] | None = None
        # Official API identity is checked before any portal driver is consulted.
        # It cannot authorize terms or UI writes (call() still requires a driver).
        if self.driver is None:
            self._auth_report = self._api_verified_report(captcha_used=False)
        if driver is None and self._auth_report is None:
            raise AwinAuthFailure("vault_login_required", "fresh_ai_browser_vault_stage_required")

    @staticmethod
    def _visible(page: Any, selector: str) -> Any | None:
        matches = page.locator(selector)
        for index in range(matches.count()):
            candidate = matches.nth(index)
            if candidate.is_visible():
                return candidate
        return None

    def _api_verified_report(self, *, captcha_used: bool) -> dict[str, Any] | None:
        """Exact official identity for read-only fallback; never UI-write authority."""
        api_id = str(self.api_identity.get("publisher_id") or "")
        api_account_id = str(self.api_identity.get("account_id") or "")
        api_name = str(self.api_identity.get("legal_name") or "")
        if (self.api_identity.get("verified") is not True
                or api_id != AWIN_PUBLISHER_ID or api_account_id != AWIN_PUBLISHER_ID
                or normalized(AWIN_ACCOUNT_NAME) not in normalized(api_name)):
            return None
        self._auth_report = {
            "status": "API_VERIFIED", "reauthenticated": False,
            "publisher_id": api_id, "publisher_title": api_name,
            "account_name": api_name, "cdp_endpoint": self.endpoint,
            "profile": "", "browser": "not_used" if self.driver is None else FRESH_BROWSER_TRANSPORT,
            "captcha_used": bool(captcha_used),
            "identity_source": "api",
        }
        return dict(self._auth_report)

    def _ensure_authenticated_session(self) -> dict[str, Any]:
        """Prove exact publisher id/title in the UI, else fall back to the API."""
        auth_report = getattr(self, "_auth_report", None)
        if auth_report is not None:
            return dict(auth_report)
        if self.driver is None:
            raise AwinAuthFailure("vault_login_required", "fresh_ai_browser_vault_stage_required")
        page = self.driver.page
        navigation_failure = ""
        try:
            self.driver.goto(self.PROGRAMS)
            self.driver.wait(1200)
        except Exception as exc:  # noqa: BLE001
            navigation_failure = type(exc).__name__
        if navigation_failure:
            # Leave the handler before raising: neither the raw cause nor its
            # implicit context may enter a public authentication traceback.
            raise AwinAuthFailure("navigate_publisher_directory", f"navigation_failed:{navigation_failure}")
        observed = self._identity_observation([page])
        if observed is not None and observed.get("authenticated") is True:
            self._auth_report = {
                "status": "HEALTHY", "reauthenticated": False,
                "publisher_id": AWIN_PUBLISHER_ID, "publisher_title": str(observed.get("title") or ""),
                "account_name": AWIN_ACCOUNT_NAME, "cdp_endpoint": self.endpoint,
                "profile": "", "browser": FRESH_BROWSER_TRANSPORT,
                "captcha_used": False, "identity_source": "ui",
            }
            return dict(self._auth_report)
        route = _awin_identity_route(self.driver.current_url())
        if route.get("publisher_scoped") and str(route.get("publisher_id") or "") != AWIN_PUBLISHER_ID:
            raise AwinAuthFailure("verify_publisher_identity", "publisher_id_mismatch")
        if route.get("publisher_scoped") and not route.get("allowed"):
            raise AwinAuthFailure("verify_publisher_identity", "publisher_route_not_exact")

        email = self._visible(
            page, "input[type='email'], input[name='email'], input[name='username'], input[autocomplete='username']")
        password = self._visible(page, "input[type='password'], input[autocomplete='current-password']")
        if email is None and password is None:
            raise AwinAuthFailure("locate_login_email", "exact_visible_email_selector_required")
        # GLP-268: the fresh browser is never signed in from a credential file
        # or a paid CAPTCHA solver. Login belongs to the origin-bound vault stage
        # (scripts/glp_awin_portal_login.py) in an AI-browser session; here the
        # verified official API identity is the read-only result, never UI-write
        # authority, and without it the lane stops for that vault stage.
        api_report = self._api_verified_report(captcha_used=False)
        if api_report is not None:
            return api_report
        raise AwinAuthFailure("vault_login_required", "fresh_vault_login_stage_not_integrated")

    def authenticate_and_verify(self) -> dict[str, Any]:
        return self._ensure_authenticated_session()

    def close(self) -> None:
        if self.driver is not None:
            self.driver.close()

    @staticmethod
    def failure_payload(exc: AwinAuthFailure) -> dict[str, str]:
        return {
            "schema": "awin-auth-failure-v1", "code": "AWIN_AUTH_REPAIR_FAILED",
            "failed_step": exc.failed_step, "detail": exc.detail,
        }

    @staticmethod
    def _observed_website(page: Any) -> dict[str, Any]:
        """Extract one publisher website from labelled authenticated UI DOM.

        The expected website is deliberately not an input.  Only a value shown
        by a website-labelled control/field in the live authenticated page can
        satisfy this observation.  Missing or conflicting evidence fails
        closed rather than borrowing a configured constant.
        """
        script = r"""
        () => {
          const norm = value => (value || '').replace(/\s+/g, ' ').trim();
          const websiteLabel = value => /^(website|publisher website|primary (website|site url|url)|site url)$/i.test(norm(value));
          const candidates = [];
          const add = (value, kind, label) => {
            const website = norm(value);
            if (/^https?:\/\/[^\s]+$/i.test(website)) {
              candidates.push({website, evidence_kind: kind, evidence_label: norm(label)});
            }
          };
          for (const input of document.querySelectorAll('input, textarea')) {
            const id = input.id || '';
            const labels = [
              input.getAttribute('aria-label'), input.getAttribute('data-testid'),
              input.getAttribute('name'), id,
              id ? document.querySelector(`label[for="${CSS.escape(id)}"]`)?.textContent : '',
              input.closest('label')?.textContent,
            ].filter(Boolean);
            const label = labels.find(websiteLabel);
            if (label) add(input.value, 'labelled_control', label);
          }
          for (const term of document.querySelectorAll('dt, th')) {
            if (!websiteLabel(term.textContent)) continue;
            const value = term.tagName === 'DT' ? term.nextElementSibling : term.nextElementSibling;
            if (value) {
              const anchor = value.querySelector?.('a[href]');
              add(anchor?.href || value.textContent, 'labelled_definition', term.textContent);
            }
          }
          for (const row of document.querySelectorAll('tr')) {
            const cells = row.querySelectorAll('th, td');
            if (cells.length < 2 || !websiteLabel(cells[0].textContent)) continue;
            const anchor = cells[1].querySelector('a[href]');
            add(anchor?.href || cells[1].textContent, 'labelled_table_row', cells[0].textContent);
          }
          return candidates;
        }
        """
        try:
            raw = page.evaluate(script)
        except Exception:  # noqa: BLE001 - absence/DOM uncertainty fails closed
            raw = []
        candidates = [value for value in (raw or []) if isinstance(value, dict)]
        by_website: dict[str, list[dict[str, Any]]] = {}
        for value in candidates:
            website = str(value.get("website") or "").strip()
            if website:
                by_website.setdefault(website, []).append(value)
        if len(by_website) != 1:
            return {"website": "", "website_evidence": [],
                    "website_evidence_status": "missing_or_conflicting"}
        website, evidence = next(iter(by_website.items()))
        return {"website": website, "website_evidence": evidence,
                "website_evidence_status": "independently_observed"}

    @staticmethod
    def _identity_observation(pages: list[Any]) -> dict[str, Any] | None:
        unique_pages: list[Any] = []
        seen: set[int] = set()
        for page in pages:
            marker = id(page)
            if marker not in seen:
                seen.add(marker)
                unique_pages.append(page)
        scoped: list[tuple[Any, dict[str, Any]]] = []
        qualifying: list[tuple[Any, dict[str, Any]]] = []
        for page in unique_pages:
            classified = _awin_identity_route(str(getattr(page, "url", "")))
            if classified["publisher_scoped"]:
                scoped.append((page, classified))
                if classified["allowed"]:
                    qualifying.append((page, classified))
        # No publisher surface exists yet: the caller may navigate the exact
        # read-only fallback.  Any scoped-but-invalid or ambiguous surface is a
        # terminal identity failure for this observation, never a fallback cue.
        if not scoped:
            return None
        if len(scoped) != len(qualifying):
            return {"authenticated": False, "publisher_id": "", "account_id": "",
                    "title": "", "website": "", "website_evidence": []}
        observations: list[tuple[str, str, dict[str, Any]]] = []
        for page, classified in qualifying:
            try:
                title = " ".join(str(page.title()).split())
            except Exception:  # noqa: BLE001 - uncertainty is an identity failure
                title = ""
            if (not title or AWIN_AUTH_TITLE_RE.search(title)
                    or normalized("Tilton Group") not in normalized(title)):
                return {"authenticated": False, "publisher_id": "", "account_id": "",
                        "title": "", "website": "", "website_evidence": []}
            observations.append((str(classified["publisher_id"]), title,
                                 AwinFreshBrowserTransport._observed_website(page)))
        observed_ids = {value[0] for value in observations}
        observed_websites = {
            str(value[2].get("website") or "") for value in observations
            if value[2].get("website_evidence_status") == "independently_observed"
        }
        if observed_ids != {AWIN_PUBLISHER_ID} or len(observed_websites) > 1:
            return {"authenticated": False, "publisher_id": "", "account_id": "",
                    "title": "", "website": "", "website_evidence": []}
        observed_id = next(iter(observed_ids))
        title = sorted(value[1] for value in observations)[0]
        website = ({"website": "", "website_evidence": [],
                    "website_evidence_status": "missing_or_conflicting"}
                   if not observed_websites else next(
                       value[2] for value in observations
                       if value[2].get("website_evidence_status") == "independently_observed"))
        return {"authenticated": True, "publisher_id": observed_id, "account_id": observed_id,
                "title": title, **website}

    def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        program_id = str(payload.get("program_id") or "")
        if action != "verify_identity" and self.driver is None:
            raise AwinAuthFailure("vault_login_required", "fresh_ai_browser_vault_stage_required")
        if action == "verify_identity":
            # Real transports are constructed through __init__ and carry the
            # run-start auth sentinel. Provider-free legacy identity fixtures
            # intentionally construct only the pure observation boundary.
            auth = self._ensure_authenticated_session() if hasattr(self, "_auth_report") else {}
            expected_id = str(payload.get("publisher_id") or "")
            if expected_id != AWIN_PUBLISHER_ID:
                return {"authenticated": False, "publisher_id": "", "account_id": "",
                        "title": "", "website": "", "website_evidence": []}
            if auth.get("identity_source") == "api":
                return {
                    "authenticated": True,
                    "publisher_id": str(auth.get("publisher_id") or ""),
                    "account_id": str(auth.get("publisher_id") or ""),
                    "title": str(auth.get("account_name") or ""),
                    "website": "", "website_evidence": [],
                    "identity_source": "api",
                }
            # The account-settings route can force a fresh ID-provider login even
            # while the existing publisher application tab remains authenticated.
            # Verify the exact official publisher route already present in the
            # CDP context first. Only fall back to the read-only program directory;
            # never use the reauthentication-only account route as identity proof.
            pages = list(getattr(self.driver._ctx, "pages", ()) or ())
            auxiliary = [
                page for page in pages
                if str(getattr(page, "url", "")) == AWIN_SITE_INFORMATION_URL
            ]
            other_scoped = [
                page for page in pages
                if _awin_identity_route(str(getattr(page, "url", ""))).get("publisher_scoped")
                and page not in auxiliary
            ]
            if len(auxiliary) == 1 and not other_scoped:
                # A prior identity read may leave the persistent tab on this
                # exact read-only settings surface. Preserve its independently
                # observed site, then prove identity again on the exact Programs
                # route. The settings route alone is never identity proof.
                site = self._observed_website(auxiliary[0])
                self.driver.goto(self.PROGRAMS)
                self.driver.wait(900)
                observed = self._identity_observation([self.driver.page])
                if observed is not None and site.get("website_evidence_status") == "independently_observed":
                    observed.update(site)
                return observed or {
                    "authenticated": False, "publisher_id": "", "account_id": "",
                    "title": "", "website": "", "website_evidence": [],
                }
            observed = self._identity_observation(pages)
            if observed is not None:
                if not observed.get("authenticated"):
                    return observed
                if observed.get("website_evidence_status") == "independently_observed":
                    return observed
            else:
                # Navigate once before touching login controls. An exact
                # authenticated redirect is accepted read-only; any hostile
                # publisher-scoped redirect fails closed without credential use.
                self.driver.goto(self.PROGRAMS)
                self.driver.wait(900)
                observed = self._identity_observation([self.driver.page])
                if observed is not None and not observed.get("authenticated"):
                    return observed
                if observed is None:
                    self._ensure_authenticated_session()
                    observed = self._identity_observation([self.driver.page])
                if observed is None or not observed.get("authenticated"):
                    return observed or {
                        "authenticated": False, "publisher_id": "", "account_id": "",
                        "title": "", "website": "", "website_evidence": [],
                    }
                if observed.get("website_evidence_status") == "independently_observed":
                    return observed
            # Website identity is read from Awin's authenticated Site
            # Information surface, not inferred from configured authority.
            self.driver.goto(AWIN_SITE_INFORMATION_URL)
            self.driver.wait(900)
            site = self._observed_website(self.driver.page)
            self.driver.goto(self.PROGRAMS)
            self.driver.wait(900)
            restored = self._identity_observation([self.driver.page])
            if restored is not None and site.get("website_evidence_status") == "independently_observed":
                restored.update(site)
            return restored or {
                "authenticated": False, "publisher_id": "", "account_id": "",
                "title": "", "website": "", "website_evidence": [],
            }
        if action == "capture_authenticated_terms":
            terms_url = str(payload.get("terms_url") or "")
            if (not program_id or program_id not in terms_url or not terms_url.startswith("/awin/affiliate/2979387/")
                    or AWIN_TRACKING_RE.search(terms_url)):
                return {"complete": False, "first_party": False, "program_id": ""}
            absolute = "https://ui.awin.com" + terms_url
            response = self.driver.page.context.request.get(absolute, timeout=30000)
            raw = response.body()
            text = extract_awin_terms_pdf_text(raw) if response.ok else ""
            return {"complete": response.ok and bool(raw), "first_party": absolute.startswith("https://ui.awin.com/"),
                    "program_id": program_id, "text": text, "document_sha256": hashlib.sha256(raw).hexdigest(),
                    "account_requirements": {}, "source_url": absolute}
        if action == "join_once":
            url = self.PROFILE.format(program_id=program_id)
            prepared_message = str(payload.get("prepared_message") or "")
            message_sha = hashlib.sha256(prepared_message.encode("utf-8")).hexdigest()
            row_sha = str(payload.get("row_authority_sha256") or "")
            if (not prepared_message or message_sha != payload.get("prepared_message_sha256")
                    or not re.fullmatch(r"[0-9a-f]{64}", row_sha)):
                return {"write_actions": 0, "control_kind": "unverified"}
            self.driver.goto(url)
            self.driver.wait(1000)
            body = compact(self.driver.body_text(), 12000)
            if program_id not in body and program_id not in self.driver.current_url():
                return {"write_actions": 0, "control_kind": "unverified"}
            # Claim exists before this method. Awin's current authenticated
            # profile exposes one exact, side-effect-free Join Program opener;
            # it reveals the form but does not apply. Only the final Join
            # button below is a provider write.
            page = self.driver.page
            if (page.locator('textarea[name="message"]:visible').count() != 0
                    or page.locator('input[name="accepted"][type="checkbox"]:visible').count() != 0):
                return {"write_actions": 0, "control_kind": "unverified"}
            opener = page.locator(
                f'a#actionProgramme.join.list-group-item[href="#"]'
                f'[data-merchantid="{program_id}"]:visible')
            if (opener.count() != 1
                    or " ".join(opener.inner_text().split()) != "Join Program"):
                return {"write_actions": 0, "control_kind": "unverified"}
            opener.click()
            message = page.locator('textarea[name="message"]:visible')
            accepted = page.locator('input[name="accepted"][type="checkbox"]:visible')
            for _ in range(20):
                if message.count() == 1 and accepted.count() == 1:
                    break
                self.driver.wait(100)
            else:
                return {"write_actions": 0, "control_kind": "unverified"}
            message.fill(prepared_message)
            accepted.evaluate("node => { node.checked = true; node.dispatchEvent(new Event('change', {bubbles: true})); }")
            final = page.locator('button[type="submit"].modal_save.btn-small-green:visible')
            for _ in range(20):
                if (accepted.is_checked() and final.count() == 1
                        and " ".join(final.inner_text().split()) == "Join"
                        and final.is_enabled()):
                    break
                self.driver.wait(100)
            else:
                return {"write_actions": 0, "control_kind": "unverified"}
            final.click()  # the one and only provider-write click
            self.driver.wait(1000)
            return {"write_actions": 1, "control_kind": "join_programme",
                    "prepared_message_sha256": message_sha,
                    "row_authority_sha256": row_sha}
        raise ValueError(f"unsupported_awin_cdp_action:{action}")


# Former name kept for existing importers; it no longer attaches to 9325.
AwinPlaywrightCDPTransport = AwinFreshBrowserTransport


# ---------------------------------------------------------------------- gate

def load_gate_module():
    # Under the recurring Awin lane every imported source byte lives in one
    # sealed, child-authenticated archive.  Never reopen the mutable workspace
    # gate path after the credential descriptor has crossed the process boundary.
    if getattr(sys, "_glp_awin_frozen_source_sha256", None):
        from scripts import affiliate_application_executor_gate
        return affiliate_application_executor_gate
    spec = importlib.util.spec_from_file_location("affiliate_application_executor_gate", GATE_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ApplicationGate:
    """One gate PASS per application, bound to that application's exact id.

    The preflight manifest holds exactly ONE target, so the gate's verdict is
    unambiguous: `approved_exact_identities == [network:program_id]` or the
    submit does not happen.
    """

    def __init__(self, run_dir: Path, *, authority_path: Path | None = None,
                 ledger_paths: list[Path] | None = None, gate_module=None):
        self.run_dir = Path(run_dir)
        self.authority_path = Path(authority_path or AUTHORITY_PATH)
        self.ledger_paths = [Path(p) for p in (ledger_paths if ledger_paths is not None else [LEDGER_PATH])]
        self.gate = gate_module or load_gate_module()

    def evaluate(self, target: dict) -> dict:
        """Return {status, receipt_path, preflight_path, failures}."""
        slug = re.sub(r"[^A-Za-z0-9]+", "-", identity(target["network"], target["program_id"])).strip("-")
        preflight_path = self.run_dir / "preflight" / f"{slug}.json"
        receipt_path = self.run_dir / "gate" / f"{slug}.json"
        preflight = {
            "schema": "glp_affiliate_execution_preflight_v1",
            "status": "PASS",
            "captured_at_utc": now(),
            "authority_manifest": str(self.authority_path),
            "authority_manifest_sha256": b.sha256_file(self.authority_path),
            "targets": [target],
            "excluded_existing_relationships": [],
            "failures": [],
            "effects": {"provider_calls": 1, "provider_writes": 0, "sheet_calls": 0, "sheet_writes": 0},
        }
        write_private(preflight_path, preflight)
        receipt = self.gate.validate(
            self.authority_path, preflight_path,
            [p for p in self.ledger_paths if Path(p).is_file()])
        write_private(receipt_path, receipt)
        approved = receipt.get("approved_exact_identities") or []
        wanted = identity(target["network"], target["program_id"])
        passed = receipt.get("status") == "PASS" and approved == [wanted]
        return {
            "status": "PASS" if passed else "BLOCKED",
            "preflight_path": str(preflight_path),
            "receipt_path": str(receipt_path),
            "approved_exact_identities": approved,
            "failures": receipt.get("failures") or ([] if passed else ["gate_identity_binding_mismatch"]),
        }


# ----------------------------------------------------------------- executors

class ApplicationExecutor:
    """One network's portal application flow.

    `capture()` is READ-ONLY and produces the evidence the gate binds against.
    `submit()` fires at most one provider mutation and is only ever reached from
    a gate PASS carrying this exact program id.
    """

    network = ""
    display = ""
    needs_login = False

    def __init__(self, *, driver=None, profile: dict | None = None, run_dir: Path | None = None,
                 evidence_dir: Path | None = None):
        self._driver = driver
        self.profile = profile or pl.application_profile()
        self.run_dir = Path(run_dir or APPLY_ROOT)
        self.evidence_dir = Path(evidence_dir or (self.run_dir / "evidence"))

    # -- lifecycle -----------------------------------------------------------
    def make_driver(self):  # pragma: no cover - provider construction
        raise NotImplementedError

    @property
    def driver(self):
        # A driver that failed to START must not be cached half-built: the next
        # row would get an unusable page object and a misleading AttributeError
        # instead of the real reason (observed 2026-08-06 with a missing
        # playwright in the calling interpreter).
        if self._driver is None:
            driver = self.make_driver()
            try:
                driver.start()
            except Exception:
                with contextlib.suppress(Exception):
                    driver.close()
                raise
            self._driver = driver
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            with contextlib.suppress(Exception):
                self._driver.close()
            self._driver = None

    # -- contract ------------------------------------------------------------
    def capture(self, row: dict) -> dict:
        """-> {'status': 'CAPTURED'|'BLOCKED'|'ALREADY_RELATIONSHIP', ...}"""
        raise NotImplementedError

    def submit(self, row: dict, capture: dict) -> dict:
        raise NotImplementedError

    # -- helpers -------------------------------------------------------------
    def publisher_message(self, name: str) -> str:
        return (
            f"{self.profile['brand']} is a U.S. website publisher creating evidence-conscious "
            "provider comparisons and health-education content. We use clear FTC affiliate "
            "disclosures, organic website content only, and no unsupported medical claims. "
            f"We would like to include {name} in relevant comparison and review content."
        )[:500]

    def evidence_paths(self, row: dict) -> dict:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", identity(row["network"], row["program_id"])).strip("-")
        return {
            "first_party": self.evidence_dir / f"{slug}-first-party.json",
            "terms": self.evidence_dir / f"{slug}-terms.json",
            "payload": self.evidence_dir / f"{slug}-payload.json",
            "screenshot": self.evidence_dir / f"{slug}-after-submit.png",
            "slug": slug,
        }

    def write_evidence(self, row: dict, *, page_text: str, page_url: str, observed: dict,
                       terms: dict, payload: dict, flags: dict) -> dict:
        """Persist the three hashed artifacts and build the gate target."""
        paths = self.evidence_paths(row)
        first_party_sha = write_private(paths["first_party"], {
            "captured_at_utc": now(), "network": row["network"], "program_id": row["program_id"],
            "name": row["name"], "url": page_url, "page_text": compact(page_text),
            "observed": observed, "write_actions": 0,
        })
        terms_sha = write_private(paths["terms"], {
            "captured_at_utc": now(), "network": row["network"], "program_id": row["program_id"],
            "terms": terms, "source_first_party_sha256": first_party_sha, "write_actions": 0,
        })
        payload_sha = write_private(paths["payload"], payload)
        # Authenticated Awin rows carry the exact parent-authorized lane.  The
        # source taxonomy may use a finer alias (for example
        # supplements-vitamins), but it must never erase that bound authority.
        lane = authority_lane(row.get("authority_lane") or row.get("lane"))
        target = {
            "network": row["network"], "program_id": str(row["program_id"]),
            "name": row["name"], "decision": "AUTO_APPLY",
            "fit_lane": lane, "source_lane": row.get("lane"),
            # The candidate choke admitted this exact explicit route before
            # capture. Never substitute an observed profile page here: an empty
            # candidate URL must remain gate-blocked rather than become eligible
            # merely because a portal page was reachable.
            "application_url": str(row.get("application_url") or row.get("url") or "").strip(),
            "relationship": "available",
            "first_party_evidence_path": str(paths["first_party"]),
            "first_party_evidence_sha256": first_party_sha,
            "terms_evidence_path": str(paths["terms"]), "terms_evidence_sha256": terms_sha,
            "payload_path": str(paths["payload"]), "payload_sha256": payload_sha,
            "terms": terms,
        }
        target.update({flag: bool(flags.get(flag, True)) for flag in GATE_FLAGS})
        return target


class KatalysApplicationExecutor(ApplicationExecutor):
    """Katalys / RevOffers KMP — proven three-dialog application flow.

    Apply To Program -> traffic-source dialog -> legal confirmation (two
    checkboxes + publisher message) -> Apply Now, then a fresh reload to VERIFY
    the relationship actually changed. Runs in a fresh per-job browser
    (GLP-268); Katalys has no application API, and until a vault login stage
    signs that browser in, the profile read blocks as a login wall instead of
    riding the old 127.0.0.1:9324 session.
    """

    network = "katalys"
    display = "Katalys / RevOffers KMP"
    needs_login = False
    PRIMARY_LABELS = ("Apply To Program", "Join Program")
    EXISTING_LABELS = ("Manage", "Leave Program")

    def make_driver(self):  # pragma: no cover - provider construction
        return PlaywrightApplyDriver(fresh=True)

    def program_url(self, program_id: str) -> str:
        return f"https://app.katalys.com/pub/{KATALYS_ORG}/explore/{program_id}"

    def capture(self, row: dict) -> dict:
        driver = self.driver
        url = self.program_url(row["program_id"])
        response = driver.goto(url)
        driver.wait(3000)
        text = compact(driver.body_text())
        if login_wall(driver.current_url(), "app.katalys.com"):
            return {"status": "BLOCKED", "blocker": "katalys_fresh_login_required"}
        if response.get("http_status") not in (None, 200):
            return {"status": "BLOCKED", "blocker": f"profile_http_{response.get('http_status')}"}
        if f"Program ID: {row['program_id']}" not in text:
            return {"status": "BLOCKED", "blocker": "exact_program_id_missing_on_profile"}
        if normalized(row["name"])[:40] not in normalized(text):
            return {"status": "BLOCKED", "blocker": "exact_program_name_mismatch"}
        names = driver.buttons()
        if any(label in names for label in self.EXISTING_LABELS) or "\nPending\n" in text:
            return {"status": "ALREADY_RELATIONSHIP", "buttons": names}
        primary = next((label for label in self.PRIMARY_LABELS if names.count(label) == 1), None)
        if primary is None:
            return {"status": "BLOCKED", "blocker": f"exact_application_control_missing:{names[:12]}"}
        terms = classify_terms(text)
        target = self.write_evidence(
            row, page_text=text, page_url=url,
            observed={"visible_buttons": names, "primary_label": primary},
            terms=terms,
            payload={
                "network": self.network, "program_id": str(row["program_id"]),
                "traffic_source": "GLP Clinic Scout Site",
                "publisher_message": self.publisher_message(row["name"]),
                "affiliate_disclosure_required": True,
                "tracking_generation": False, "public_wiring": False,
            },
            # Katalys application asks for a traffic source and two in-portal
            # terms checkboxes. Nothing else is requested at application time.
            flags={flag: False for flag in GATE_FLAGS},
        )
        return {"status": "CAPTURED", "target": target, "primary_label": primary,
                "url": url, "buttons": names}

    def submit(self, row: dict, capture: dict) -> dict:
        driver = self.driver
        url = capture["url"]
        primary = capture["primary_label"]
        payload = json.loads(Path(capture["target"]["payload_path"]).read_text())
        driver.click_button(primary)
        driver.wait(900)
        source_scope = driver.open_dialog("GLP Clinic Scout Site", "source")
        if not source_scope:
            driver.press_escape()
            return {"status": "BLOCKED", "blocker": "traffic_source_dialog_missing", "write_actions": 0}
        secondary = "Apply" if primary == "Apply To Program" else "Join"
        if driver.buttons(source_scope).count(secondary) != 1:
            driver.press_escape()
            return {"status": "BLOCKED", "blocker": "traffic_source_action_not_exact", "write_actions": 0}
        driver.click_button(secondary, source_scope)
        driver.wait(900)
        terms_scope = driver.open_dialog("legally bound", "terms")
        if not terms_scope:
            driver.press_escape()
            return {"status": "BLOCKED", "blocker": "legal_confirmation_dialog_missing", "write_actions": 0}
        legal_text = compact(driver.scope_text(terms_scope, 20000))
        live = classify_terms(legal_text)
        if live["has_special_terms"]:
            driver.press_escape()
            return {"status": "BLOCKED", "write_actions": 0,
                    "blocker": "live_terms_risk:" + ",".join(live["risk_classes"])}
        if (driver.scope_count("input[type='checkbox']:visible", terms_scope) != 2
                or driver.scope_count("textarea:visible", terms_scope) != 1):
            driver.press_escape()
            return {"status": "BLOCKED", "blocker": "legal_confirmation_shape_drift", "write_actions": 0}
        driver.scope_fill("textarea:visible", payload["publisher_message"], terms_scope)
        for index in range(2):
            driver.scope_check("input[type='checkbox']:visible", index, terms_scope)
        if driver.buttons(terms_scope).count("Apply Now") != 1 or not driver.button_enabled("Apply Now", terms_scope):
            driver.press_escape()
            return {"status": "BLOCKED", "blocker": "final_apply_control_not_exact_or_enabled", "write_actions": 0}

        driver.click_button("Apply Now", terms_scope)          # <-- the ONE mutation
        driver.wait(4500)
        shot = self.evidence_paths(row)["screenshot"]
        driver.mask_credentials()
        driver.screenshot(shot)

        driver.goto(url)
        driver.wait(3000)
        after_text = compact(driver.body_text())
        after_names = driver.buttons()
        verified = (primary not in after_names
                    and (any(label in after_names for label in self.EXISTING_LABELS)
                         or "\nPending\n" in after_text or "\nJoined\n" in after_text
                         or "\nApproved\n" in after_text))
        return {
            "status": "SUBMITTED_VERIFIED" if verified else "SUBMISSION_UNVERIFIED",
            "write_actions": 1,
            "confirmation_state": "pending_or_active" if verified else "unconfirmed",
            "portal_evidence": {"screenshot": str(shot), "after_buttons": after_names[:12]},
        }


class BlockedPortalExecutor(ApplicationExecutor):
    """A portal whose application surface is known-unreachable this run.

    It never opens a browser, never attempts a login, and never touches the
    recovery material. It produces a precise operator step instead — that IS the
    deliverable for these rows.
    """

    network = ""
    blocker_code = ""
    operator_step = ""
    surface_url = ""

    def capture(self, row: dict) -> dict:
        return {"status": "BLOCKED", "blocker": self.blocker_code,
                "operator_step": self.operator_step,
                "surface_url": self.surface_url.format(program_id=row["program_id"])}

    def submit(self, row: dict, capture: dict) -> dict:  # pragma: no cover - unreachable by design
        raise RuntimeError("blocked_portal_never_submits")


class AwinApplicationExecutor(ApplicationExecutor):
    """Official-API reads followed by one bounded authenticated UI join.

    Default/local construction is provider-free and never opens a driver. Live
    execution requires injected API/UI adapters and an exact reviewed authority
    subset; stale TOTP-only behavior is intentionally gone.
    """

    network = "awin"
    display = "Awin API-first / authenticated UI fallback"
    needs_login = False

    def __init__(self, *, execution_mode: bool = False, awin_api=None, awin_ui=None,
                 authority_program_ids=None, row_bindings=None,
                 authority_subset_sha256: str = "",
                 expected_identity: dict[str, str] | None = None, **kwargs):
        super().__init__(**kwargs)
        self.engine = AwinApiFirstJoin(
            authority_program_ids=authority_program_ids or (),
            expected_identity=expected_identity or {
                "publisher_id": "2979387", "account_id": "2979387",
                "legal_name": "Tilton Group", "website": "https://glpclinicscout.com",
            },
            claim_root=self.run_dir / "awin-write-claims", execution_mode=execution_mode,
            api=awin_api, ui=awin_ui, row_bindings=row_bindings,
            authority_subset_sha256=authority_subset_sha256)

    def capture(self, row: dict) -> dict:
        prepared = self.engine.preflight(row)
        if prepared.get("status") == "SAFE_SKIP":
            return {"status": "ALREADY_RELATIONSHIP", "relationship_state": prepared.get("relationship_state")}
        if prepared.get("status") != "READY":
            return {"status": "BLOCKED", "blocker": prepared.get("blocker") or prepared.get("status"),
                    "identity_failure_detail": prepared.get("identity_failure_detail") or [],
                    "operator_step": "review the exact Awin API/UI preflight receipt; do not use TOTP or tracking links",
                    "surface_url": f"https://ui.awin.com/awin/affiliate/2979387/merchant-profile/{row['program_id']}"}
        terms = prepared["terms"]
        classified_terms = classify_terms(str(terms.get("text") or ""))
        parent_binding = self.engine.row_bindings[str(row["program_id"])]["parent_terms_binding"]
        # Awin exposes some complete authenticated terms only as PDF bytes.  An
        # exact hash match to the reviewed parent PDF is complete evidence even
        # when no text layer exists.  Drifted opaque bytes stay incompatible;
        # text-bearing bytes still use the normal risk classifier.
        if (terms.get("complete") is True and terms.get("first_party") is True
                and self.engine._terms_match_parent(terms, parent_binding)):
            classified_terms["captured_complete"] = True
            classified_terms["compatible"] = not classified_terms["has_special_terms"]
        target = self.write_evidence(
            row, page_text=str(terms.get("text") or ""), page_url=str(terms.get("source_url") or ""),
            observed={"api_relationship": "notjoined", "publisher_identity_verified_twice": True},
            terms=classified_terms,
            payload={"network": "awin", "program_id": str(row["program_id"]),
                     "write_path": "one bounded Join Programme control", "tracking_generation": False},
            flags={flag: False for flag in GATE_FLAGS})
        bound_row = self.engine.row_bindings[str(row["program_id"])]
        return {"status": "CAPTURED", "target": target, "awin_prepared": prepared,
                "awin_bound_row": dict(bound_row),
                "parent_row_authority_sha256": bound_row["row_authority_sha256"],
                "parent_terms_binding": dict(bound_row["parent_terms_binding"]),
                "prepared_message": bound_row["prepared_message"]}

    def submit(self, row: dict, capture: dict) -> dict:
        return self.engine.commit(capture.get("awin_bound_row") or row,
                                  capture.get("awin_prepared") or {})

    def close(self) -> None:
        ui = self.engine.ui
        transport = getattr(ui, "transport", None)
        with contextlib.suppress(Exception):
            if transport is not None and hasattr(transport, "close"):
                transport.close()
        super().close()


class ProviderPortalApplicationExecutor(ApplicationExecutor):
    """Persistent UI mutation with provider-owned pre/post relationship reads.

    The UI half intentionally uses only exact visible controls and blocks on any
    shape drift. Provider state, never success copy, determines ALREADY and
    SUBMITTED_VERIFIED.
    """

    needs_login = True
    portal_network = ""
    APPLY_LABELS = ("Apply to Program", "Apply To Program", "Apply", "Join Program",
                    "Join program", "Join", "Request to Join", "Request partnership")
    CONFIRM_LABELS = ("Accept", "Agree", "Confirm", "Submit Application", "Apply Now",
                      "Apply", "Join")

    def __init__(self, *, transport=None, headless: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.transport = transport or b.RequestsTransport()
        self.headless = headless
        self._logged_in = False

    @property
    def spec(self):
        key = self.portal_network or self.network
        spec = pl.PORTALS.get(key) or pl.SIGNUP_PORTALS.get(key)
        if spec is None:
            raise KeyError(f"portal_spec_missing:{key}")
        return spec

    def make_driver(self):  # pragma: no cover - provider construction
        return PlaywrightApplyDriver(self.spec.profile_dir, headless=self.headless)

    def relationship(self, row: dict) -> dict:
        raise NotImplementedError

    def program_url(self, row: dict, before: dict) -> str:
        return str(row.get("application_url") or row.get("url") or before.get("invite_url") or "").strip()

    def ensure_login(self) -> dict:
        if self._logged_in:
            return {"outcome": "session_reused_in_run", "login_submits": 0}
        session = pl.PortalSession(self.spec, driver=self.driver, headless=self.headless)
        # ApplicationExecutor already started this exact driver. Mark it as such
        # so PortalSession does not start a second browser over the same profile.
        session.started = True
        session._owns_driver = False
        result = session.login()
        if result.get("outcome") not in {"logged_in", "session_reused", "logged_in_via_sso"}:
            return result
        self._logged_in = True
        return result

    def _exact_primary(self) -> tuple[str, list[str]]:
        buttons = self.driver.buttons()
        matches = [label for label in self.APPLY_LABELS if buttons.count(label) == 1]
        return (matches[0] if len(matches) == 1 else ""), buttons

    def capture(self, row: dict) -> dict:
        before = self.relationship(row)
        if before.get("present"):
            return {"status": "ALREADY_RELATIONSHIP", "relationship_state": before.get("state"),
                    "provider_readback": before}
        if before.get("blockers"):
            return {"status": "BLOCKED", "blocker": "provider_pre_readback_blocked",
                    "provider_readback": before}
        login = self.ensure_login()
        if login.get("outcome") not in {"logged_in", "session_reused", "logged_in_via_sso",
                                        "session_reused_in_run"}:
            return {"status": "BLOCKED", "blocker": f"portal_login:{login.get('reason') or 'unconfirmed'}",
                    "operator_step": login.get("operator_step", ""), "identity_failure_detail": [{
                        "network": self.network, "source": "portal_login", "key": "ui_login",
                        "check": str(login.get("reason") or "login_outcome_unconfirmed")} ]}
        url = self.program_url(row, before)
        if not url.startswith("https://"):
            return {"status": "BLOCKED", "blocker": "exact_https_application_url_missing"}
        nav = self.driver.goto(url); self.driver.wait(3500)
        text = compact(self.driver.body_text(30000))
        if nav.get("http_status") not in (None, 200):
            return {"status": "BLOCKED", "blocker": f"application_page_http_{nav.get('http_status')}"}
        name_match = normalized(row["name"])[:32] in normalized(text)
        id_match = str(row["program_id"]).lower() in text.lower()
        if not (name_match or id_match):
            return {"status": "BLOCKED", "blocker": "exact_program_identity_missing_on_application_page"}
        terms = classify_terms(text)
        if not terms["captured_complete"]:
            return {"status": "BLOCKED", "blocker": "portal_terms_incomplete_before_consent"}
        if terms["has_special_terms"]:
            return {"status": "BLOCKED", "blocker": "portal_terms_hold:" + ",".join(
                terms["risk_classes"] or ["legal_or_account_action"])}
        primary, buttons = self._exact_primary()
        if not primary:
            return {"status": "BLOCKED", "blocker": f"exact_application_control_missing:{buttons[:12]}"}
        target = self.write_evidence(
            row, page_text=text, page_url=url,
            observed={"provider_pre_readback": before, "visible_buttons": buttons,
                      "primary_label": primary, "login_outcome": login.get("outcome")},
            terms=terms,
            payload={"network": self.network, "program_id": str(row["program_id"]),
                     "publisher_message": self.publisher_message(row["name"]),
                     "primary_label": primary, "tracking_generation": False},
            flags={flag: False for flag in GATE_FLAGS})
        return {"status": "CAPTURED", "target": target, "url": url,
                "primary_label": primary, "provider_pre_readback": before}

    def _confirm_if_present(self) -> dict:
        scope = self.driver.open_dialog("", "application-confirm")
        if not scope:
            return {"clicked": False, "dialog": False}
        text = compact(self.driver.scope_text(scope, 30000))
        terms = classify_terms(text)
        if terms["has_special_terms"]:
            self.driver.press_escape()
            return {"clicked": False, "dialog": True,
                    "blocker": "live_terms_hold:" + ",".join(
                        terms["risk_classes"] or ["legal_or_account_action"])}
        labels = self.driver.buttons(scope)
        exact = [label for label in self.CONFIRM_LABELS if labels.count(label) == 1]
        if len(exact) != 1:
            self.driver.press_escape()
            return {"clicked": False, "dialog": True,
                    "blocker": f"exact_confirmation_control_missing:{labels[:12]}"}
        self.driver.click_button(exact[0], scope)
        return {"clicked": True, "dialog": True, "label": exact[0]}

    def submit(self, row: dict, capture: dict) -> dict:
        self.driver.click_button(capture["primary_label"])
        self.driver.wait(2500)
        after_first = self.relationship(row)
        confirmation = {"clicked": False, "dialog": False}
        if not after_first.get("present"):
            confirmation = self._confirm_if_present()
            if confirmation.get("blocker"):
                return {"status": "BLOCKED", "blocker": confirmation["blocker"],
                        "write_actions": 0, "provider_readback": after_first}
            if confirmation.get("clicked"):
                self.driver.wait(3500)
        after = self.relationship(row)
        shot = self.evidence_paths(row)["screenshot"]
        self.driver.mask_credentials(); self.driver.screenshot(shot)
        verified = bool(after.get("present"))
        return {"status": "SUBMITTED_VERIFIED" if verified else "SUBMISSION_UNVERIFIED",
                "write_actions": 1, "confirmation_state": after.get("state") if verified else "unconfirmed",
                "provider_readback": after, "provider_pre_readback": capture.get("provider_pre_readback"),
                "portal_evidence": {"screenshot": str(shot), "confirmation": confirmation,
                                    "final_url": self.driver.current_url()}}


class CjApplicationExecutor(ProviderPortalApplicationExecutor):
    network = "cj"
    portal_network = "cj"
    display = "CJ Affiliate"
    BASE = "https://members.cj.com/member/7688849/publisher/advertisers/findAdvertisers.cj"
    PUBLISHER_ID = 8010186

    def _record(self, row: dict) -> dict:
        self.driver.goto(self.BASE); self.driver.wait(1200)
        payload = {"pageNumber": 1, "publisherId": self.PUBLISHER_ID, "pageSize": "50",
                   "advertiserCountries": "US", "geographicSource": "", "approvalRequired": False,
                   "autoRollover": False, "newPendingOffers": False, "replacementPendingOffers": False,
                   "autoApprovedByPublisher": False, "manualApprovedByPublisher": False,
                   "autoRejectedByPublisher": False, "sortColumn": "advertiserName",
                   "sortDescending": False, "advertiserIds": str(row["program_id"])}
        result = self.driver.evaluate("""async p => { const u=new URL('/member/publisher/8010186/advertiserSearch.json', location.origin);
            for (const [k,v] of Object.entries(p)) u.searchParams.set(k,String(v));
            const r=await fetch(u,{credentials:'include'}); return {status:r.status,body:await r.json()}; }""", payload)
        rows = [x for x in (result.get("body") or {}).get("advertisers", [])
                if str(x.get("advertiserId") or "") == str(row["program_id"])]
        return {"http_status": result.get("status"), "record": rows[0] if len(rows) == 1 else None}

    @staticmethod
    def _related(record: dict) -> bool:
        return bool(record.get("joined") or record.get("pendingDate")
                    or record.get("activeProgramTermsId") or record.get("pendingProgramTermsId")
                    or set(record.get("statuses") or []).intersection({"pending_applications", "active"}))

    def relationship(self, row: dict) -> dict:
        got = self._record(row)
        record = got.get("record") or {}
        return {"present": self._related(record),
                "state": "approved" if record.get("joined") else "pending" if self._related(record) else "notjoined",
                "program_id": str(row["program_id"]), "http_status": got.get("http_status"),
                "blockers": [] if got.get("http_status") == 200 and record else [
                    f"cj_exact_readback_http_{got.get('http_status')}_record_{bool(record)}"],
                "record": record,
                "provider_readback": "GET members.cj.com/.../advertiserSearch.json"}

    def program_url(self, row: dict, before: dict) -> str:
        return self.BASE

    def capture(self, row: dict) -> dict:
        login = self.ensure_login()
        if login.get("outcome") not in {"logged_in", "session_reused", "logged_in_via_sso",
                                        "session_reused_in_run"}:
            return {"status": "BLOCKED", "blocker": f"portal_login:{login.get('reason') or 'unconfirmed'}"}
        before = self.relationship(row)
        if before.get("present"):
            return {"status": "ALREADY_RELATIONSHIP", "relationship_state": before["state"],
                    "provider_readback": before}
        if before.get("blockers"):
            return {"status": "BLOCKED", "blocker": "provider_pre_readback_blocked",
                    "provider_readback": before}
        record = before["record"]
        defaults = [x for x in (record.get("programTerms") or []) if x.get("isDefault")]
        if len(defaults) != 1:
            return {"status": "BLOCKED", "blocker": "cj_exact_default_program_terms_missing"}
        if defaults[0].get("hasSpecialTerms"):
            return {"status": "BLOCKED", "blocker": "cj_uncaptured_special_terms"}
        self.driver.goto(self.BASE); self.driver.wait(1000)
        page = self.driver.page
        page.locator("#keyword-search-box").fill(str(row["name"])); page.locator("#searchButton").click()
        self.driver.wait(3500)
        cards = page.locator("div.main-row-wrapper").filter(has_text=re.compile(rf"\b{re.escape(str(row['program_id']))}\b"))
        if cards.count() != 1:
            return {"status": "BLOCKED", "blocker": f"cj_exact_card_count_{cards.count()}"}
        controls = cards.locator("button").filter(has_text=re.compile("apply to program", re.I))
        if controls.count() != 1:
            return {"status": "BLOCKED", "blocker": f"cj_apply_control_count_{controls.count()}"}
        text = json.dumps(record, sort_keys=True)
        terms = classify_terms(text)
        # CJ's first-party default-term object is complete for a standard terms
        # application when the provider explicitly says no special terms.
        terms["captured_complete"] = True
        terms["compatible"] = not terms["has_special_terms"]
        target = self.write_evidence(
            row, page_text=text, page_url=self.BASE,
            observed={"provider_pre_readback": before, "default_program_terms": defaults[0]},
            terms=terms,
            payload={"network": "cj", "program_id": str(row["program_id"]),
                     "primary_label": "Apply to Program", "tracking_generation": False},
            flags={flag: False for flag in GATE_FLAGS})
        return {"status": "CAPTURED", "target": target, "primary_label": "Apply to Program",
                "provider_pre_readback": before}

    def submit(self, row: dict, capture: dict) -> dict:
        page = self.driver.page
        cards = page.locator("div.main-row-wrapper").filter(has_text=re.compile(rf"\b{re.escape(str(row['program_id']))}\b"))
        controls = cards.locator("button").filter(has_text=re.compile("apply to program", re.I))
        if controls.count() != 1:
            return {"status": "BLOCKED", "blocker": "cj_apply_control_shape_drift", "write_actions": 0}
        controls.click(); self.driver.wait(2500)
        after_first = self.relationship(row)
        confirmation = {"clicked": False, "dialog": False}
        if not after_first.get("present"):
            confirmation = self._confirm_if_present()
            if confirmation.get("blocker"):
                return {"status": "BLOCKED", "blocker": confirmation["blocker"],
                        "write_actions": 0, "provider_readback": after_first}
            if confirmation.get("clicked"):
                self.driver.wait(3000)
        after = self.relationship(row); verified = bool(after.get("present"))
        shot = self.evidence_paths(row)["screenshot"]
        self.driver.mask_credentials(); self.driver.screenshot(shot)
        return {"status": "SUBMITTED_VERIFIED" if verified else "SUBMISSION_UNVERIFIED",
                "write_actions": 1, "confirmation_state": after.get("state") if verified else "unconfirmed",
                "provider_readback": after, "provider_pre_readback": capture.get("provider_pre_readback"),
                "portal_evidence": {"screenshot": str(shot), "confirmation": confirmation}}


class RakutenApplicationExecutor(ProviderPortalApplicationExecutor):
    network = "rakuten"
    portal_network = "rakuten"
    display = "Rakuten Advertising"

    def relationship(self, row: dict) -> dict:
        from . import rakuten  # lazy: zero provider I/O at import
        return rakuten.relationship(row["program_id"], self.transport)


class ImpactApplicationExecutor(ProviderPortalApplicationExecutor):
    network = "impact"
    portal_network = "impact"
    display = "Impact.com"

    def relationship(self, row: dict) -> dict:
        from . import impact
        return impact.relationship(row["program_id"], self.transport)


class PartnerStackApplicationExecutor(ProviderPortalApplicationExecutor):
    network = "partnerstack"
    portal_network = "partnerstack"
    display = "PartnerStack"

    def relationship(self, row: dict) -> dict:
        from . import partnerstack
        return partnerstack.relationship(row["program_id"], self.transport,
                                         group_slug=row.get("group_slug", ""))


class Digistore24ApplicationExecutor(ProviderPortalApplicationExecutor):
    network = "digistore24"
    portal_network = "digistore24"
    display = "Digistore24"

    def relationship(self, row: dict) -> dict:
        from . import digistore24
        return digistore24.affiliation(row["program_id"], self.transport)


class AuthenticatedPortalRelationshipExecutor(ProviderPortalApplicationExecutor):
    """Provider-owned UI relationship readback for portal-only marketplaces.

    Login happens before the first exact program read. A page is accepted only
    on a provider host, only when its name/id binds to the queued row, and only
    when it exposes one exact apply control or an unambiguous relationship
    marker. Generic dashboard words such as "active" never prove a program
    relationship by themselves.
    """

    allowed_hosts: tuple[str, ...] = ()
    EXISTING_LABELS = ("Manage", "Manage Program", "Promote", "Get Links", "View Partnership")
    RELATIONSHIP_RE = re.compile(
        r"(?i)\b(?:application|partnership|relationship|program)\s*(?:status)?\s*[:\-]?\s*"
        r"(approved|joined|active|pending|requested|applied|awaiting review)\b")

    def capture(self, row: dict) -> dict:
        login = self.ensure_login()
        if login.get("outcome") not in {"logged_in", "session_reused", "logged_in_via_sso",
                                        "session_reused_in_run"}:
            return {"status": "BLOCKED", "blocker": f"portal_login:{login.get('reason') or 'unconfirmed'}",
                    "operator_step": login.get("operator_step", ""), "identity_failure_detail": [{
                        "network": self.network, "source": "portal_login", "key": "ui_login",
                        "check": str(login.get("reason") or "login_outcome_unconfirmed")} ]}
        return super().capture(row)

    def relationship(self, row: dict) -> dict:
        if not self._logged_in:
            return {"present": False, "state": "unknown", "blockers": ["portal_session_not_established"],
                    "provider_readback": "authenticated provider-owned program page"}
        url = self.program_url(row, {})
        if not url.startswith("https://"):
            return {"present": False, "state": "unknown", "blockers": ["exact_https_application_url_missing"],
                    "provider_readback": "authenticated provider-owned program page"}
        wanted_host = (urlsplit(url).hostname or "").lower()
        if not any(wanted_host == host or wanted_host.endswith("." + host) for host in self.allowed_hosts):
            return {"present": False, "state": "unknown", "blockers": ["application_url_not_provider_owned"],
                    "provider_readback": "authenticated provider-owned program page"}
        nav = self.driver.goto(url); self.driver.wait(3000)
        current = self.driver.current_url()
        host = (urlsplit(current).hostname or "").lower()
        text = compact(self.driver.body_text(30000))
        if (not any(host == allowed or host.endswith("." + allowed) for allowed in self.allowed_hosts)
                or nav.get("http_status") not in (None, 200)):
            return {"present": False, "state": "unknown",
                    "blockers": [f"provider_program_page_http_or_redirect:{nav.get('http_status')}:{host}"],
                    "provider_readback": "authenticated provider-owned program page"}
        name_match = normalized(row["name"])[:32] in normalized(text)
        id_match = str(row["program_id"]).lower() in text.lower()
        if not (name_match or id_match):
            return {"present": False, "state": "unknown",
                    "blockers": ["exact_program_identity_missing_on_provider_page"],
                    "provider_readback": "authenticated provider-owned program page"}
        buttons = self.driver.buttons()
        apply_labels = [label for label in self.APPLY_LABELS if buttons.count(label) == 1]
        existing_labels = [label for label in self.EXISTING_LABELS if buttons.count(label) == 1]
        status_match = self.RELATIONSHIP_RE.search(text)
        if status_match or existing_labels:
            state = status_match.group(1).lower().replace(" ", "_") if status_match else "existing"
            return {"present": True, "state": state, "program_id": str(row["program_id"]),
                    "blockers": [], "provider_readback": "authenticated provider-owned program page",
                    "surface_url": current, "relationship_controls": existing_labels}
        if len(apply_labels) != 1:
            return {"present": False, "state": "unknown",
                    "blockers": [f"provider_relationship_or_apply_control_not_exact:{apply_labels}"],
                    "provider_readback": "authenticated provider-owned program page",
                    "surface_url": current}
        return {"present": False, "state": "notjoined", "program_id": str(row["program_id"]),
                "blockers": [], "provider_readback": "authenticated provider-owned program page",
                "surface_url": current, "apply_control": apply_labels[0]}


class ClickBankApplicationExecutor(AuthenticatedPortalRelationshipExecutor):
    network = "clickbank"
    portal_network = "clickbank"
    display = "ClickBank Affiliate Marketplace"
    allowed_hosts = ("accounts.clickbank.com", "clickbank.com")

    def capture(self, row: dict) -> dict:
        approval = (row.get("application_meta") or {}).get("approval_required")
        if approval is False:
            from . import clickbank
            account = clickbank.readback(self.transport)
            hoplink = clickbank.tracking_link({"program_id": row["program_id"]}, "machine-preflight")
            if account.get("auth_state") == "PASS" and hoplink.get("state") == "resolved":
                return {"status": "ALREADY_RELATIONSHIP", "relationship_state": "open_marketplace",
                        "provider_readback": account, "tracking_preflight": {
                            "state": hoplink.get("state"), "method": hoplink.get("method")}}
            return {"status": "BLOCKED", "blocker": "clickbank_open_marketplace_account_or_hoplink_unverified",
                    "provider_readback": account}
        if approval is not True:
            return {"status": "BLOCKED", "blocker": "clickbank_approval_requirement_unclassified",
                    "operator_step": "None; wait for the full authenticated ClickBank catalog to classify this row."}
        return super().capture(row)


class UpPromoteApplicationExecutor(AuthenticatedPortalRelationshipExecutor):
    network = "uppromote"
    portal_network = "uppromote"
    display = "UpPromote Marketplace"
    allowed_hosts = ("marketplace.uppromote.com",)


class GoAffProApplicationExecutor(AuthenticatedPortalRelationshipExecutor):
    network = "goaffpro"
    portal_network = "goaffpro"
    display = "GoAffPro Marketplace"
    allowed_hosts = ("goaffpro.com",)


class DirectFormApplicationExecutor(ApplicationExecutor):
    """Merchant-hosted affiliate application forms (the `direct` network).

    There is no shared DOM here, so the executor DISCOVERS the form and maps
    each field onto the standard application profile. It is deliberately
    fail-closed: an unmapped required field, a credential-shaped field, a file
    upload, an account password, or more than one plausible application form and
    the row blocks with the reason instead of guessing.
    """

    network = "direct"
    display = "Merchant-hosted affiliate application"
    needs_login = False

    APPLY_WORDS = ("affiliate", "partner", "ambassador", "referral")
    FORM_WORDS = ("apply", "application", "join", "sign up", "signup", "register",
                  "become an affiliate", "become a partner")
    # Field name/label -> profile key. Order matters: the first match wins.
    FIELD_MAP = (
        (r"(?i)first[\s_-]*name|fname|given", "first_name"),
        (r"(?i)last[\s_-]*name|lname|surname|family", "last_name"),
        (r"(?i)full[\s_-]*name|^name$|your name|contact name|contact\[name\]", "full_name"),
        # BEFORE email on purpose. Mediclab's "Audience size" input carries the
        # word "email" in its own label ("email list"), so the email pattern was
        # winning and the lane typed our address into the audience field
        # (proven: direct-Mediclab-payload.json mapped 1-6 -> email).
        (r"(?i)audience|subscriber|follower|list size|reach|"
         r"monthly (visitors|traffic|views)", "audience_size"),
        (r"(?i)e-?mail", "email"),
        (r"(?i)compan|business|organi[sz]ation|legal entity", "business_name"),
        (r"(?i)web ?site|url|domain|site|blog|homepage", "website"),
        (r"(?i)user[\s_-]*name|handle", "username"),
        (r"(?i)countr", "country"),
        (r"(?i)promot|traffic|audience|how (do|will) you|describe|message|comment|"
         r"tell us|about (you|your)|marketing|strategy|experience", "promotional_model"),
    )

    def __init__(self, *, headless: bool = True, profile_dir: Path | None = None, **kwargs):
        super().__init__(**kwargs)
        self.headless = headless
        self.profile_dir = Path(profile_dir or (pl.MARKETPLACE_SECRETS / "direct-apply" / "browser-profile"))

    def make_driver(self):  # pragma: no cover - provider construction
        return PlaywrightApplyDriver(self.profile_dir, headless=self.headless)

    def values(self) -> dict:
        p = self.profile
        return {
            "first_name": p["first_name"], "last_name": p["last_name"],
            "full_name": f"{p['first_name']} {p['last_name']}".strip(),
            "email": p["email"], "business_name": p["business_name"],
            "website": p["website"], "username": p["username"],
            "country": p.get("audience_geo") or "United States",
            "promotional_model": p["promotional_model"],
            "audience_size": str(p.get("audience_size") or "").strip()
                             or "Growing GLP-1 comparison site; organic search traffic",
        }

    @staticmethod
    def field_text(field: dict) -> str:
        return " ".join(str(field.get(key) or "") for key in ("name", "id", "placeholder", "aria", "label"))

    def map_field(self, field: dict) -> str:
        text = self.field_text(field)
        for pattern, key in self.FIELD_MAP:
            if re.search(pattern, text):
                return key
        return ""

    def find_application_form(self, forms: list[dict]) -> tuple[dict | None, str]:
        """The single form that looks like an affiliate application."""
        scored: list[tuple[int, dict]] = []
        for form in forms:
            fields = [f for f in form.get("fields", []) if f.get("visible")
                      and f.get("type") not in {"submit", "button", "reset"}]
            if not any(re.search(r"(?i)e-?mail", self.field_text(f)) for f in fields):
                continue
            if len(fields) < 3:          # newsletter boxes are one or two fields
                continue
            blob = (form.get("text") or "").lower() + " " + " ".join(self.field_text(f) for f in fields).lower()
            score = sum(2 for word in self.APPLY_WORDS if word in blob)
            score += sum(1 for word in self.FORM_WORDS if word in blob)
            if score:
                scored.append((score, form))
        if not scored:
            return None, "application_form_not_found"
        scored.sort(key=lambda item: (-item[0], item[1]["index"]))
        if len(scored) > 1 and scored[0][0] == scored[1][0]:
            return None, "ambiguous_application_forms"
        return scored[0][1], ""

    def application_page(self, row: dict) -> dict:
        """Land on the merchant's application page. One hop from the given URL."""
        driver = self.driver
        start = str(row.get("url") or "").strip()
        if not start:
            return {"ok": False, "blocker": "no_application_url_on_row"}
        driver.goto(start)
        driver.wait(2500)
        forms = driver.scan_forms()
        form, _ = self.find_application_form(forms)
        if form is not None:
            return {"ok": True, "url": driver.current_url(), "forms": forms}
        # One hop: follow the most affiliate-application-looking on-page link.
        best, best_score = "", 0
        for link in driver.links():
            blob = f"{link.get('text','')} {link.get('href','')}".lower()
            if "://" not in str(link.get("href") or ""):
                continue
            score = sum(2 for word in self.APPLY_WORDS if word in blob)
            score += sum(1 for word in self.FORM_WORDS if word in blob)
            if score > best_score:
                best, best_score = link["href"], score
        if best_score < 3 or not best:
            return {"ok": False, "blocker": "application_form_not_found", "url": driver.current_url()}
        driver.goto(best)
        driver.wait(2500)
        return {"ok": True, "url": driver.current_url(), "forms": driver.scan_forms()}

    def plan_fill(self, form: dict) -> tuple[list[dict], list[str], dict]:
        """-> (fill plan, blockers, observed gate flags)"""
        values = self.values()
        plan: list[dict] = []
        blockers: list[str] = []
        flags = {flag: False for flag in GATE_FLAGS}
        if form.get("file_inputs"):
            blockers.append("file_upload_required")
        for field in form.get("fields", []):
            if not field.get("visible"):
                continue
            kind = str(field.get("type") or "")
            text = self.field_text(field)
            if kind == "password":
                blockers.append("account_password_required")
                flags["requires_account_creation"] = True
                continue
            if kind == "file":
                blockers.append("file_upload_required")
                continue
            if kind in {"submit", "button", "reset", "image"}:
                continue
            if CREDENTIAL_FIELD_RE.search(text):
                blockers.append(f"credential_or_finance_field:{(field.get('name') or text)[:40]}")
                flags["requires_banking_or_payment"] = True
                continue
            if kind in {"checkbox", "radio"}:
                if field.get("required") or re.search(r"(?i)terms|agree|accept|policy|consent", text):
                    plan.append({"key": field["key"], "action": "check"})
                continue
            key = self.map_field(field)
            if not key:
                if field.get("required"):
                    blockers.append(f"unmapped_required_field:{(field.get('name') or text)[:40]}")
                continue
            value = values[key]
            if kind == "select":
                options = field.get("options") or []
                match = next((o for o in options if normalized(o) == normalized(value)), "")
                if not match and key == "country":
                    match = next((o for o in options if o.upper() in {"US", "USA"}), "")
                if not match:
                    if field.get("required"):
                        blockers.append(f"unmapped_required_select:{(field.get('name') or text)[:40]}")
                    continue
                plan.append({"key": field["key"], "action": "select", "value": match, "profile_key": key})
                continue
            plan.append({"key": field["key"], "action": "fill", "value": value, "profile_key": key})
        if not any(step.get("profile_key") == "email" for step in plan):
            blockers.append("email_field_not_mappable")
        return plan, blockers, flags

    def capture(self, row: dict) -> dict:
        driver = self.driver
        landing = self.application_page(row)
        if not landing.get("ok"):
            return {"status": "BLOCKED", "blocker": landing.get("blocker", "application_page_unreachable"),
                    "operator_step": (f"find and complete the {row['name']} affiliate application by hand, "
                                      "or add the exact application URL to the direct supply inventory"),
                    "surface_url": landing.get("url") or row.get("url", "")}
        form, reason = self.find_application_form(landing["forms"])
        if form is None:
            return {"status": "BLOCKED", "blocker": reason, "surface_url": landing["url"],
                    "operator_step": (f"open the {row['name']} affiliate page and identify the exact "
                                      "application form URL for the lane")}
        plan, blockers, flags = self.plan_fill(form)
        submits = form.get("submits") or []
        primary = [s for s in submits if re.search(r"(?i)appl|submit|join|sign ?up|register|send|continue",
                                                   s.get("label") or "")]
        if len(primary) != 1:
            blockers.append(f"submit_control_not_exact:{len(primary)}")
        page_text = compact(driver.body_text())
        terms = classify_terms(page_text)
        if blockers:
            # Say WHICH field/step stopped us. The old text ("it asks for
            # something the lane is not authorized to provide") asserted an
            # authorization wall for what is usually a selector/mapping gap —
            # Mediclab's was literally the Shopify `contact[name]` input.
            return {"status": "BLOCKED", "blocker": ";".join(sorted(set(blockers)))[:400],
                    "surface_url": landing["url"],
                    "blocker_detail": describe_capture_blockers(blockers),
                    "operator_step": (
                        f"complete the {row['name']} affiliate application by hand at "
                        f"{landing['url']} — {describe_capture_blockers(blockers)}")}
        target = self.write_evidence(
            row, page_text=page_text, page_url=landing["url"],
            observed={"form_index": form["index"], "field_count": len(form.get("fields", [])),
                      "submit_label": primary[0].get("label", "")},
            terms=terms,
            payload={
                "network": row["network"], "program_id": str(row["program_id"]),
                "application_url": landing["url"],
                "fill_plan": [{k: v for k, v in step.items() if k != "value"} for step in plan],
                "publisher_message": self.publisher_message(row["name"]),
                "affiliate_disclosure_required": True,
                "tracking_generation": False, "public_wiring": False,
            },
            flags=flags,
        )
        return {"status": "CAPTURED", "target": target, "url": landing["url"],
                "form": form, "plan": plan, "submit_key": primary[0]["key"],
                "submit_label": primary[0].get("label", "")}

    def submit(self, row: dict, capture: dict) -> dict:
        driver = self.driver
        before = compact(driver.body_text())
        for step in capture["plan"]:
            if step["action"] == "fill":
                driver.fill_field(step["key"], step["value"])
            elif step["action"] == "select":
                driver.select_field(step["key"], step["value"])
            elif step["action"] == "check":
                driver.check_field(step["key"])
        driver.click_submit(capture["submit_key"])            # <-- the ONE mutation
        driver.wait(4500)
        after = compact(driver.body_text())
        shot = self.evidence_paths(row)["screenshot"]
        driver.mask_credentials()
        driver.screenshot(shot)
        raw_delta = pl._delta(before, after)
        delta = raw_delta.lower()
        final_url = driver.current_url()
        confirmed = any(marker in delta or marker in after.lower()
                        for marker in CONFIRMATION_MARKERS)
        errored = any(marker in delta for marker in FORM_ERROR_MARKERS)
        outcome = {
            "status": "SUBMITTED_VERIFIED" if (confirmed and not errored) else "SUBMISSION_UNVERIFIED",
            "write_actions": 1,
            "confirmation_state": "acknowledged" if confirmed else "unconfirmed",
            "portal_evidence": {"screenshot": str(shot), "page_delta": raw_delta[:1200],
                                "final_url": final_url},
        }
        if outcome["status"] == "SUBMISSION_UNVERIFIED":
            # NEVER return an unverified submit without saying what the page
            # actually did. `_ledger_row` used to fall back to the literal
            # string "unknown", which is where the operator's useless
            # "BLK-unknown ... asks for something the lane is not authorized to
            # provide" card came from (Mediclab, 2026-08-16: the real answer was
            # a Shopify 404 on the submit endpoint).
            outcome.update(classify_unverified_submit(
                row, raw_delta=raw_delta, final_url=final_url, errored=errored))
        return outcome


EXECUTORS: dict[str, type[ApplicationExecutor]] = {
    "katalys": KatalysApplicationExecutor,
    "awin": AwinApplicationExecutor,
    "cj": CjApplicationExecutor,
    "rakuten": RakutenApplicationExecutor,
    "impact": ImpactApplicationExecutor,
    "partnerstack": PartnerStackApplicationExecutor,
    "digistore24": Digistore24ApplicationExecutor,
    "clickbank": ClickBankApplicationExecutor,
    "uppromote": UpPromoteApplicationExecutor,
    "goaffpro": GoAffProApplicationExecutor,
    "direct": DirectFormApplicationExecutor,
}


def executor_for(network: str) -> type[ApplicationExecutor] | None:
    return EXECUTORS.get(str(network or "").strip().lower())


# -------------------------------------------------------------------- ledger

def load_ledger(path: Path | None = None) -> dict:
    path = Path(path or LEDGER_PATH)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"schema": "affiliate-applications-ledger-v1", "applications": []}


def queued_rows(ledger: dict) -> list[dict]:
    return [row for row in ledger.get("applications", [])
            if str(row.get("status")) == QUEUED_STATUS]


def _canonical_ledger_writer_guard(path: Path):
    """Return the Item E lock guard for this ledger's machine directory.

    Import lazily so the adapter package remains import-safe and so every
    canonical ledger writer derives the same ``.item-e-canonical-writers.lock``
    identity as the six-target Item E transaction.
    """
    from scripts import glp_item_e_operator_intake as intake_mod

    return intake_mod.canonical_writer_guard([Path(path)])


def _assert_canonical_ledger_writer_guard_current(path: Path) -> None:
    """Revalidate the stable directory lock and still-named lock-file inode."""
    from scripts import glp_item_e_operator_intake as intake_mod

    intake_mod.assert_canonical_writer_guard_current([Path(path)])


def _require_exact_awin_execute_ledger_identity(raw_path: str) -> tuple[Path, tuple[int, int]]:
    """Accept only the one literal, regular, unaliased live Awin replay ledger.

    A resolved-path or inode comparison is intentionally insufficient here: a
    symlink, hardlink, relative path, or ``..`` spelling could otherwise move
    the durable claim away from the one canonical replay guard while still
    authenticating against live manifest authority.
    """
    expected = str(LEDGER_PATH)
    if raw_path != expected:
        raise SystemExit("Awin execute requires the exact canonical applications ledger path")
    path = Path(raw_path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SystemExit("Awin canonical applications ledger is unavailable") from exc
    if (not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1):
        raise SystemExit("Awin canonical applications ledger identity is not a unique regular file")
    return path, (metadata.st_dev, metadata.st_ino)


def _require_exact_awin_execute_ledger(raw_path: str) -> Path:
    """Compatibility wrapper returning the validated exact canonical path."""
    return _require_exact_awin_execute_ledger_identity(raw_path)[0]


def _canonical_json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


class _ExactAwinLedgerBinding:
    """Open-descriptor binding for the live Awin replay ledger generation.

    The binding rejects same-byte pathname replacement, not merely content
    drift. Controlled claim/final commits explicitly advance the bound
    generation only after the new unique inode contains the exact bytes we
    intended to commit while the stable Item E writer lock is still current.
    """

    def __init__(self, path: Path, *, expected_opening_identity: tuple[int, int] | None = None):
        self.path = Path(path)
        self.fd = -1
        self.device = -1
        self.inode = -1
        self.body = b""
        self._bind_current(
            expected_body=None, expected_identity=expected_opening_identity, label="opening")

    @staticmethod
    def _read_descriptor(fd: int) -> bytes:
        chunks: list[bytes] = []
        offset = 0
        while True:
            chunk = os.pread(fd, 1024 * 1024, offset)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
        return b"".join(chunks)

    def _open_current(self, *, label: str) -> tuple[int, os.stat_result, bytes]:
        _assert_canonical_ledger_writer_guard_current(self.path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags)
        except OSError as exc:
            raise RuntimeError(f"awin_canonical_ledger_{label}_open_failed") from exc
        try:
            opened_before = os.fstat(fd)
            named_before = os.stat(self.path, follow_symlinks=False)
            if (not stat.S_ISREG(opened_before.st_mode) or not stat.S_ISREG(named_before.st_mode)
                    or stat.S_ISLNK(named_before.st_mode)
                    or opened_before.st_nlink != 1 or named_before.st_nlink != 1
                    or (opened_before.st_dev, opened_before.st_ino)
                    != (named_before.st_dev, named_before.st_ino)):
                raise RuntimeError(f"awin_canonical_ledger_{label}_identity_replaced")
            body = self._read_descriptor(fd)
            opened_after = os.fstat(fd)
            named_after = os.stat(self.path, follow_symlinks=False)
            if ((opened_before.st_dev, opened_before.st_ino, opened_before.st_size,
                 opened_before.st_mtime_ns)
                    != (opened_after.st_dev, opened_after.st_ino, opened_after.st_size,
                        opened_after.st_mtime_ns)
                    or (opened_after.st_dev, opened_after.st_ino)
                    != (named_after.st_dev, named_after.st_ino)
                    or named_after.st_nlink != 1
                    or len(body) != opened_after.st_size):
                raise RuntimeError(f"awin_canonical_ledger_{label}_changed_while_binding")
            return fd, opened_after, body
        except BaseException:
            os.close(fd)
            raise

    def _bind_current(self, *, expected_body: bytes | None,
                      expected_identity: tuple[int, int] | None = None, label: str) -> None:
        fd, metadata, body = self._open_current(label=label)
        if (expected_identity is not None
                and (metadata.st_dev, metadata.st_ino) != expected_identity):
            os.close(fd)
            raise RuntimeError(f"awin_canonical_ledger_{label}_identity_replaced")
        if expected_body is not None and body != expected_body:
            os.close(fd)
            raise RuntimeError(f"awin_canonical_ledger_{label}_commit_bytes_mismatch")
        previous = self.fd
        self.fd = fd
        self.device = metadata.st_dev
        self.inode = metadata.st_ino
        self.body = body
        if previous >= 0:
            os.close(previous)

    def assert_current(self, label: str) -> None:
        _assert_canonical_ledger_writer_guard_current(self.path)
        try:
            opened = os.fstat(self.fd)
            named = os.stat(self.path, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError(f"awin_canonical_ledger_{label}_identity_unavailable") from exc
        if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(named.st_mode)
                or stat.S_ISLNK(named.st_mode)
                or opened.st_nlink != 1 or named.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (self.device, self.inode)
                or (named.st_dev, named.st_ino) != (self.device, self.inode)
                or self._read_descriptor(self.fd) != self.body):
            raise RuntimeError(f"awin_canonical_ledger_{label}_identity_or_bytes_drift")

    def document(self, label: str) -> dict[str, Any]:
        self.assert_current(label)
        value = json.loads(self.body)
        if not isinstance(value, dict):
            raise RuntimeError(f"awin_canonical_ledger_{label}_object_required")
        return value

    def advance_after_commit(self, document: dict[str, Any], label: str) -> None:
        self._bind_current(expected_body=_canonical_json_bytes(document), label=label)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()


def update_ledger(rows: list[dict], path: Path | None = None,
                  *, ledger_binding: _ExactAwinLedgerBinding | None = None) -> dict:
    """Update the queued entries in place, keyed network:program_id.

    In place, not append: the auto-apply lane already wrote these rows when it
    queued them, and a second row for the same identity would defeat the
    double-application guard both lanes read.
    """
    path = Path(path or LEDGER_PATH)
    with _canonical_ledger_writer_guard(path):
        # The load belongs inside the shared lock. Loading even one instruction
        # earlier can let a completed Item E promotion be overwritten by stale
        # bytes when this writer commits.
        if ledger_binding is not None:
            if ledger_binding.path != path:
                raise RuntimeError("awin_canonical_ledger_binding_path_mismatch")
            ledger = ledger_binding.document("before_controlled_update")
        else:
            ledger = load_ledger(path)
        index = {identity(row.get("network"), row.get("program_id")): row
                 for row in ledger.get("applications", [])}
        added = 0
        for update in rows:
            key = identity(update["network"], update["program_id"])
            existing = index.get(key)
            if existing is None:
                ledger.setdefault("applications", []).append(dict(update))
                index[key] = ledger["applications"][-1]
                added += 1
            else:
                existing.update(update)
        write_private(path, ledger)
        if ledger_binding is not None:
            ledger_binding.advance_after_commit(ledger, "after_controlled_update")
        return {"updated": len(rows) - added, "appended": added, "path": str(path),
                "sha256": b.sha256_file(path)}


# ---------------------------------------------------------------------- drain

#: Page text that means the provider acknowledged the application. Kept module
#: level so the confirmation vocabulary is testable and extendable in one place.
#: "thanks! your application is in" (Mediclab's own success copy) used to score
#: UNVERIFIED because only "thanks for applying" was listed.
CONFIRMATION_MARKERS = (
    "thank you", "thanks for applying", "thanks for submitting",
    "thanks! your application", "your application is in",
    "application received", "we have received", "we've received",
    "application submitted", "successfully submitted", "under review",
    "we'll be in touch", "we will be in touch", "we'll get back to you",
    "we will get back to you", "check your email",
)
FORM_ERROR_MARKERS = (
    "required", "invalid", "error", "please enter", "please fill", "try again",
)
#: Provider-side error pages that answer a submit. These are NOT operator work
#: and NOT "something the lane is not authorized to provide" — the form was
#: filled correctly and the provider's own endpoint broke.
_PROVIDER_ERROR_PAGES = (
    (r"(?i)\b404\b|not found|page (?:you requested )?(?:does ?n[o']?t exist|cannot be found)",
     "provider_404_after_submit",
     "the provider's own site answered the completed form with a 404 error page"),
    (r"(?i)\b50[0234]\b|internal server error|service unavailable|bad gateway",
     "provider_server_error_after_submit",
     "the provider's own server answered the completed form with a 5xx error"),
    (r"(?i)something went wrong|unexpected error|oops",
     "provider_generic_error_after_submit",
     "the provider's own site answered the completed form with a generic error page"),
    (r"(?i)access denied|forbidden|are you a (?:human|robot)|captcha|cloudflare|"
     r"security check|verify you are human",
     "provider_bot_wall_after_submit",
     "the provider put a bot/security wall in front of the submitted form"),
)


#: capture()-stage blocker token -> the plain sentence the operator reads.
#: `{}` is filled with the field/label the token carries after the colon.
_CAPTURE_BLOCKER_WORDS = (
    ("account_password_required",
     "the form makes you create an account with a password first"),
    ("file_upload_required",
     "the form requires a file upload (screenshot, W-9 or media kit)"),
    ("credential_or_finance_field",
     "the form asks for a banking/tax/credential field ({}) the lane may not fill"),
    ("unmapped_required_select",
     "the form has a required dropdown ({}) with no option the lane can honestly pick"),
    ("unmapped_required_field",
     "the form has a required field ({}) the lane could not recognise"),
    ("submit_control_not_exact",
     "the page has {} plausible submit buttons, so the lane will not guess which one applies"),
    ("email_field_not_mappable",
     "the form has no email field the lane can identify"),
    ("application_page_unreachable",
     "the application page did not load"),
    ("application_form_not_found",
     "no affiliate application form was found on the page"),
)


def describe_capture_blockers(blockers) -> str:
    """Turn capture()'s blocker tokens into one plain, specific sentence."""
    tokens = [str(token) for token in (blockers or []) if str(token).strip()]
    said: list[str] = []
    for token in sorted(dict.fromkeys(tokens)):
        head, _, detail = token.partition(":")
        for prefix, phrase in _CAPTURE_BLOCKER_WORDS:
            if head == prefix:
                said.append(phrase.format(detail.strip() or "unnamed")
                            if "{}" in phrase else phrase)
                break
        else:
            said.append(f"the lane stopped on `{token}`")
    if not said:
        return "the lane stopped before it could fill the form"
    if len(said) == 1:
        return said[0]
    return "; and ".join([", ".join(said[:-1]), said[-1]])


def classify_unverified_submit(row: dict, *, raw_delta: str, final_url: str,
                               errored: bool) -> dict:
    """Name the SPECIFIC reason a submit could not be verified, in plain words.

    Returns `blocker`, `operator_step` and `blocker_detail`. Never returns a
    generic class: the operator's card is only as good as this function, so
    every branch has to name the page's own behaviour and the exact next action.
    """
    name = str(row.get("name") or "this provider")
    url = str(final_url or row.get("application_url") or row.get("url") or "").strip()
    excerpt = " ".join(str(raw_delta or "").split())[:300]
    where = f" at {url}" if url else ""

    for pattern, blocker, human in _PROVIDER_ERROR_PAGES:
        if re.search(pattern, raw_delta or ""):
            return {
                "blocker": blocker,
                "blocker_detail": f"{human}. Page said: \"{excerpt}\"",
                "operator_step": (
                    f"open {url or name} and submit the affiliate form by hand — "
                    f"we filled and submitted it, but {human}, so nothing reached "
                    f"{name}. If the form is broken for you too, email them instead."),
                "surface_url": url,
            }
    if errored:
        return {
            "blocker": "form_validation_rejected_submit",
            "blocker_detail": f"the form came back with a validation complaint: \"{excerpt}\"",
            "operator_step": (
                f"open {url or name} and finish the affiliate form by hand — the form "
                f"rejected our submission with a validation message we could not "
                f"satisfy automatically: \"{excerpt[:140]}\""),
            "surface_url": url,
        }
    return {
        "blocker": "no_confirmation_text_after_submit",
        "blocker_detail": (f"the form was submitted but the page showed no confirmation "
                           f"we recognise. Page said: \"{excerpt}\"" if excerpt else
                           "the form was submitted and the page did not change at all"),
        "operator_step": (
            f"check ops@glpclinicscout.com for a {name} confirmation — we submitted the "
            f"affiliate form{where} but the page never showed a confirmation we could "
            f"recognise. If no mail arrives, re-submit the form by hand."),
        "surface_url": url,
    }


# ---------------------------------------------------------------- outcome class
#
# Operator ruling, 2026-08-18, after independently reproducing Mediclab's 404:
#
#   "mediclab returned 404 for me too - affiliate application job will need to
#    add them to the list to email directly."
#
# A provider whose own form is broken is NOT an operator task. It is a ROUTING
# decision the machine already knows how to make: the email lane exists, is
# proven (6 real applications sent from partners@ on 2026-08-18), and is exactly
# the right channel when the form channel does not work. The rule:
#
#   provider_side_broken -> auto-route to the email lane, never an operator card
#   needs_human          -> operator action item (and ONLY this class may mint one)
#   machine_defect       -> the lane owes a fix; never an operator card
#   success              -> nothing to do
#
# The classes are keyed off the blocker vocabulary `classify_unverified_submit()`
# and `capture()` already emit, so a new blocker code must be classified here or
# it lands in `machine_defect` — the safe default, which asks the machine for a
# better receipt instead of asking the operator for work we cannot name.

OUTCOME_PROVIDER_BROKEN = "provider_side_broken"
OUTCOME_NEEDS_HUMAN = "needs_human"
OUTCOME_MACHINE_DEFECT = "machine_defect"
OUTCOME_SUCCESS = "success"

#: The provider's own application channel is broken or walled. Email them.
PROVIDER_BROKEN_BLOCKERS = frozenset({
    "provider_404_after_submit",
    "provider_server_error_after_submit",
    "provider_generic_error_after_submit",
    "provider_bot_wall_after_submit",
    "application_page_unreachable",
    "application_form_not_found",
    "application_form_ambiguous",
    "no_application_url_on_row",
})
#: A real human gate: something only a person with our legal/financial identity
#: can supply. These, and only these, become operator action items.
NEEDS_HUMAN_BLOCKERS = frozenset({
    "account_password_required",
    "credential_or_finance_field",
    "requires_signature",
    "requires_kyc",
    "requires_tax_action",
    "requires_banking_or_payment",
    "file_upload_required",
    "docusign_signature_required",
    "partner_call_required",
})
#: Executor gate flags that mean the same thing, whatever the blocker string says.
NEEDS_HUMAN_FLAGS = (
    "requires_account_creation", "requires_signature", "requires_kyc",
    "requires_tax_action", "requires_banking_or_payment",
)
#: Submitted, no error, no confirmation we recognise. Not operator work: the
#: machine checks ops@ for a confirmation, and the email lane is the fallback.
UNCONFIRMED_BLOCKERS = frozenset({"no_confirmation_text_after_submit"})


def outcome_class(blocker: Any, *, flags: dict | None = None,
                  status: str = "") -> str:
    """Which of the four buckets an application outcome falls into.

    `blocker` may carry several `;`-joined codes, each optionally `code:detail`.
    A single needs-human signal wins: if any part of the form demands a human,
    emailing them instead would be applying under false pretences.
    """
    if status in {"SUBMITTED_VERIFIED", "APPROVED_VERIFIED", "JOINED_VERIFIED",
                  "AUTO_APPROVED_VERIFIED", "PENDING_VERIFIED", "ALREADY_RELATIONSHIP"}:
        return OUTCOME_SUCCESS
    for flag in NEEDS_HUMAN_FLAGS:
        if (flags or {}).get(flag) is True:
            return OUTCOME_NEEDS_HUMAN
    codes = [part.split(":", 1)[0].strip()
             for part in str(blocker or "").split(";") if part.strip()]
    if any(code in NEEDS_HUMAN_BLOCKERS for code in codes):
        return OUTCOME_NEEDS_HUMAN
    if codes and all(code in PROVIDER_BROKEN_BLOCKERS | UNCONFIRMED_BLOCKERS
                     for code in codes) and any(
                         code in PROVIDER_BROKEN_BLOCKERS for code in codes):
        return OUTCOME_PROVIDER_BROKEN
    return OUTCOME_MACHINE_DEFECT


def next_route(outcome: str) -> str:
    """The channel the machine should try next. "" means no automatic retry."""
    return "email" if outcome == OUTCOME_PROVIDER_BROKEN else ""


#: Ledger blocker values that carry no information about what actually happened.
OPAQUE_BLOCKERS = {"unknown", "", "none", "null"}
#: The template sentence that asserted an authorization wall for every capture
#: blocker, including purely mechanical ones. Never written again; scrubbed
#: wherever a pre-fix run left it behind.
_GENERIC_STEP_RE = re.compile(r"(?i)asks for something the lane is not authorized to provide")


def reclassify_stale_ledger_blockers(path: Path | None = None,
                                     *, write: bool = True) -> dict:
    """Re-derive a specific blocker for every row still carrying an opaque one.

    Rows blocked before `classify_unverified_submit()` existed hold the literal
    string `"unknown"` plus, in many cases, the previous run's unrelated
    `operator_step`. The evidence needed to name the real blocker is already on
    the row (`portal_evidence.page_delta` / `final_url`), so this re-derives it
    from that stored receipt. It invents nothing: a row with no evidence is
    reported as `no_evidence` and left exactly as it is.
    """
    path = Path(path or LEDGER_PATH)
    ledger = load_ledger(path)
    repaired: list[dict] = []
    skipped: list[dict] = []
    stripped: list[dict] = []
    routed: list[dict] = []
    for row in ledger.get("applications", []):
        blocker = str(row.get("last_blocker") or "").strip().lower()
        step = str(row.get("operator_step") or "")
        # Backfill routing on EVERY blocked row, not just the opaque ones: rows
        # written before `outcome_class` existed must still be routed, or a
        # provider-broken row from last week stays an operator card forever.
        if row.get("last_blocker") and not row.get("outcome_class"):
            klass = outcome_class(row.get("last_blocker"), status=row.get("status"))
            routed.append({"network": row.get("network"), "program_id": row.get("program_id"),
                           "name": row.get("name"), "blocker": row.get("last_blocker"),
                           "outcome_class": klass, "route": next_route(klass)})
        if (blocker not in OPAQUE_BLOCKERS and _GENERIC_STEP_RE.search(step)):
            # The step is a template that asserts an authorization wall the
            # blocker class does not support. An empty step is honest; a
            # confident wrong sentence is not.
            stripped.append({"network": row.get("network"), "program_id": row.get("program_id"),
                             "name": row.get("name"), "blocker": row.get("last_blocker"),
                             "removed_step": step})
            continue
        if blocker not in OPAQUE_BLOCKERS or not row.get("last_blocker"):
            continue
        evidence = row.get("portal_evidence") or {}
        delta = str(evidence.get("page_delta") or "")
        if not delta:
            skipped.append({"network": row.get("network"), "program_id": row.get("program_id"),
                            "name": row.get("name"), "why": "no_evidence"})
            continue
        errored = any(marker in delta.lower() for marker in FORM_ERROR_MARKERS)
        derived = classify_unverified_submit(
            row, raw_delta=delta, final_url=str(evidence.get("final_url") or ""),
            errored=errored)
        repaired.append({
            "network": row.get("network"), "program_id": row.get("program_id"),
            "name": row.get("name"), "was": row.get("last_blocker"),
            "was_operator_step": row.get("operator_step"),
            "now": derived["blocker"], "now_operator_step": derived["operator_step"],
            "blocker_detail": derived["blocker_detail"],
        })
        row["last_blocker"] = derived["blocker"]
        row["blocker_detail"] = derived["blocker_detail"]
        row["operator_step"] = derived["operator_step"]
        row["outcome_class"] = outcome_class(derived["blocker"], status=row.get("status"))
        row["application_route_next"] = next_route(row["outcome_class"])
        row["blocker_reclassified_at_utc"] = now()
    if write and (repaired or stripped or routed):
        with _canonical_ledger_writer_guard(path):
            current = load_ledger(path)
            index = {identity(r.get("network"), r.get("program_id")): r
                     for r in current.get("applications", [])}
            for item in routed:
                target = index.get(identity(item["network"], item["program_id"]))
                if target is None or target.get("outcome_class"):
                    continue
                target["outcome_class"] = item["outcome_class"]
                target["application_route_next"] = item["route"]
            for item in repaired:
                target = index.get(identity(item["network"], item["program_id"]))
                if target is None:
                    continue
                target["last_blocker"] = item["now"]
                target["blocker_detail"] = item["blocker_detail"]
                target["operator_step"] = item["now_operator_step"]
                target["outcome_class"] = outcome_class(item["now"],
                                                        status=target.get("status"))
                target["application_route_next"] = next_route(target["outcome_class"])
                target["blocker_reclassified_at_utc"] = now()
            for item in stripped:
                target = index.get(identity(item["network"], item["program_id"]))
                if target is None:
                    continue
                target["operator_step"] = None
                target["blocker_reclassified_at_utc"] = now()
            write_private(path, current)
    return {"schema": "glp.portal_apply.blocker_reclassification.v1",
            "path": str(path), "repaired": repaired, "skipped": skipped,
            "stripped_generic_steps": stripped, "routed": routed,
            "repaired_count": len(repaired), "skipped_count": len(skipped),
            "stripped_count": len(stripped), "routed_count": len(routed)}


#: Set to a falsy value only by tests that construct their own index.
_DEDUPE_ENABLED = os.environ.get("GLP_APPLICATION_DEDUPE", "1").strip() != "0"


def _application_dedupe_index():
    """The shared dedupe index, or None when it is deliberately disabled.

    The site leg is a pure local read of the current release, so it works in the
    Playwright venv with no Google client and no network. It is REQUIRED: if the
    release cannot be read we cannot prove we are not re-applying to a program we
    already earn from, and that is exactly the failure this gate exists for.
    """
    if not _DEDUPE_ENABLED:
        return None
    # Loaded by explicit file path, NOT by mutating sys.path. The Item E clean
    # environment test pins `scripts not in sys.path` and `PYTHONPATH is None`
    # for the whole execute-mode critical section, and a dedupe that widens the
    # import surface of the credential-carrying process is not worth its keep.
    import importlib.util
    name = "glp_application_dedupe"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(
            name, BUSINESS / "scripts/glp_application_dedupe.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return module.build(check_live_manifest=False)


def _row_from_queue(entry: dict) -> dict:
    authenticated_awin = isinstance(entry, _AuthenticatedAwinQueueRow)
    if authenticated_awin:
        entry.verify_projection()
    return {
        "network": str(entry.get("network") or "").strip().lower(),
        "program_id": str(entry.get("program_id") or "").strip(),
        "name": str(entry.get("name") or "").strip(),
        "lane": str(entry.get("lane") or "").strip(),
        "url": str(entry.get("url") or "").strip(),
        "application_url": str(entry.get("application_url") or entry.get("url") or "").strip(),
        # Preserve the intake evidence rather than treating a routing lane as
        # GLP-fit proof. The choke below makes missing evidence an incomplete
        # parked record before any executor is constructed.
        "fit_evidence": str(entry.get("description") or entry.get("evidence_excerpt")
                            or entry.get("fit_reason") or entry.get("reason") or "").strip(),
        "categories": entry.get("categories") or entry.get("category") or entry.get("vertical")
                      or entry.get("tags") or (),
        "region": str(entry.get("region") or entry.get("geo") or entry.get("destination")
                      or entry.get("country") or "").strip(),
        "currency": str(entry.get("currency") or "").strip(),
        "advertiser_id": str(entry.get("advertiser_id") or "").strip(),
        "group_slug": str(entry.get("group_slug") or "").strip(),
        "application_meta": (dict(entry.get("application_meta"))
                             if isinstance(entry.get("application_meta"), dict) else {}),
        # This cannot be asserted by JSON/application-plan input: only the
        # post-validator binder can construct the private authenticated type.
        "awin_authority_bound": authenticated_awin,
        "choke_display_name": entry._choke_identity if authenticated_awin else "",
        "authority_lane": entry._authorized_lane if authenticated_awin else "",
        "route": "portal",
        "planned_at_utc": entry.get("planned_at_utc"),
    }


def _choke_projection_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Build the unchanged generic choke's input from a verified queue row."""
    return {
        # The exact parent authority has already adjudicated the canonical
        # display name. Feed the generic heuristic the authenticated network
        # identity so its broad brand-name veto cannot contradict that exact
        # adjudication; retain the canonical name for complete audit evidence.
        "display_name": row.get("choke_display_name") or row["name"],
        "canonical_display_name": row["name"],
        "network": row["network"],
        "program_id": row["program_id"],
        "description": row.get("fit_evidence"),
        "categories": row.get("categories"),
        "region": row.get("region"),
        "application_url": row.get("application_url"),
    }


def enrich_urls(rows: list[dict], plan_path: Path | None) -> list[dict]:
    """Backfill legacy queue rows from the latest screened plan by identity."""
    if not plan_path or not Path(plan_path).is_file():
        return rows
    try:
        shortlist = json.loads(Path(plan_path).read_text(encoding="utf-8")).get("shortlist") or []
    except ValueError:
        return rows
    metadata = {identity(str(c.get("network")), str(c.get("program_id"))): c
                for c in shortlist if isinstance(c, dict)}
    for row in rows:
        source = metadata.get(identity(row["network"], row["program_id"]), {})
        for key in ("url", "application_url", "description", "fit_reason", "region", "currency",
                    "advertiser_id", "group_slug"):
            if not row.get(key) and source.get(key):
                row[key] = source[key]
        for key in ("categories", "application_meta"):
            if not row.get(key) and source.get(key):
                row[key] = source[key]
    return rows


def drain(rows: list[dict], *, run_id: str = "", run_dir: Path | None = None,
          executors: dict | None = None, gate: ApplicationGate | None = None,
          ledger_path: Path | None = None, daily_cap: int = 25,
          write_ledger: bool = True, policy_snapshot=None,
          execution_mode: bool = False, executor_options: dict[str, dict[str, Any]] | None = None) -> dict:
    """Serialize the complete portal decision/effect/commit critical section.

    The ledger is both the replay guard and the durable provider-attempt claim.
    Therefore the first ledger read, exact-identity recheck, claim, provider
    action, and final ledger commit must share the same Item E writer lock.  A
    final-write-only lock would allow two drains to act from the same stale
    queue state and hide the duplicate effect behind one merged ledger row.
    """
    selected_ledger = Path(ledger_path or LEDGER_PATH)
    with _canonical_ledger_writer_guard(selected_ledger):
        return _drain_locked(
            rows, run_id=run_id, run_dir=run_dir, executors=executors, gate=gate,
            ledger_path=selected_ledger, daily_cap=daily_cap,
            write_ledger=write_ledger, policy_snapshot=policy_snapshot,
            execution_mode=execution_mode, executor_options=executor_options,
        )


def _drain_locked(rows: list[dict], *, run_id: str = "", run_dir: Path | None = None,
                  executors: dict | None = None, gate: ApplicationGate | None = None,
                  ledger_path: Path | None = None, daily_cap: int = 25,
                  write_ledger: bool = True, policy_snapshot=None,
                  execution_mode: bool = False,
                  executor_options: dict[str, dict[str, Any]] | None = None,
                  ledger_binding: _ExactAwinLedgerBinding | None = None) -> dict:
    """Submit every queued application whose portal path works.

    One executor per network, built once and reused across that network's rows.
    A network stops after an UNVERIFIED submission: an unverified submit means
    the lane no longer understands the portal's state, and continuing would risk
    a double application.
    """
    run_id = run_id or dt.datetime.now(dt.timezone.utc).strftime("portal-apply-%Y%m%dT%H%M%SZ")
    run_dir = Path(run_dir or (APPLY_ROOT / run_id))
    registry = executors if executors is not None else EXECUTORS
    executor_options = executor_options or {}
    gate = gate or ApplicationGate(run_dir, ledger_paths=[Path(ledger_path or LEDGER_PATH)])
    policy_snapshot = policy_snapshot or operator_rulings.load_snapshot()

    if ledger_binding is not None:
        if ledger_binding.path != Path(ledger_path or LEDGER_PATH):
            raise RuntimeError("awin_canonical_ledger_binding_path_mismatch")
        ledger = ledger_binding.document("drain_opening")
    else:
        ledger = load_ledger(ledger_path)
    consumed = {identity(row.get("network"), row.get("program_id"))
                for row in ledger.get("applications", [])
                if str(row.get("status")) in TERMINAL_STATUSES}
    # A verified terminal Awin descendant is a SEALED disposition. Its ledger
    # row is the only durable proof that WE submitted an application and that
    # the provider confirmed it, and the awin drain's parent reconciler requires
    # such a row to come out of a run byte-identical — anything else is
    # `AWIN_PREEXISTING_VERIFIED_TERMINAL_DRIFT` and fails the whole run closed.
    # 2026-08-19: the governed-Sheet dedupe leg (added 2026-08-18) started
    # matching these programs once their real approvals reached the Sheet, and
    # the dedupe write below downgraded ten `SUBMITTED_VERIFIED` rows to
    # `ALREADY_RELATIONSHIP`. Both facts were true — we submitted, and we now
    # have the relationship — but they answer different questions, and the
    # submission record is the one that cannot be re-derived. So a dedupe hit on
    # a sealed row is recorded in the run journal only, never in the ledger.
    sealed_verified = {identity(row.get("network"), row.get("program_id"))
                       for row in verified_awin_terminal_rows(ledger)}

    # THE last gate before a provider-side mutation. Whatever queued these rows
    # — the applications machine, the legacy direct-apply lane, a hand-built
    # plan — they all pass through here, so the shared dedupe lives here and not
    # in any one planner. 2026-08-18: the planner's private dedupe let a real
    # application go out to joinfound.com for **Found**, a Katalys program we
    # already earn from and serve at /go/found-health. The site leg catches it.
    dedupe_index = _application_dedupe_index()

    built: dict[str, ApplicationExecutor] = {}
    halted: set[str] = set()
    submitted_ids: set[str] = set()
    results: list[dict] = []
    ledger_updates: list[dict] = []
    submits = 0

    try:
        for entry in rows:
            row = _row_from_queue(entry)
            key = identity(row["network"], row["program_id"])
            result = {"network": row["network"], "program_id": row["program_id"],
                      "name": row["name"], "lane": row.get("lane"),
                      "started_at_utc": now(), "write_actions": 0}
            # Dedupe FIRST, and deliberately without the ledger leg (this row is
            # itself in the ledger; `consumed` below is the ledger replay guard).
            # First because a program we already earn from must have its stale
            # blocker text cleared, not merely be skipped in silence.
            already = (dedupe_index.verdict(
                row["name"], row["network"], row["program_id"],
                legs=("site", "sheet", "queue"))
                if dedupe_index is not None else None)
            if already is None and (key in consumed or key in submitted_ids):
                result.update(status="ALREADY_RELATIONSHIP",
                              capture_status="ALREADY_RELATIONSHIP",
                              confirmation_state="existing",
                              completed_at_utc=now())
                results.append(result)
                continue
            if already is not None:
                # Not a blocker and not operator work: we already have this
                # program. Record it as the existing relationship it is, and
                # clear any stale blocker text the row is carrying.
                result.update(status="ALREADY_RELATIONSHIP",
                              capture_status="ALREADY_RELATIONSHIP",
                              confirmation_state="existing",
                              dedupe=dict(already),
                              completed_at_utc=now())
                results.append(result)
                if key in sealed_verified:
                    # Sealed: the journal carries the dedupe evidence; the
                    # ledger keeps its verified submission disposition intact.
                    continue
                cleared = _ledger_row(row, result)
                cleared["last_blocker"] = None
                cleared["operator_step"] = None
                cleared["blocker_detail"] = None
                cleared["dedupe_skip"] = dict(already)
                # We may well have really submitted something to this provider
                # before we noticed we already had them. Deduping must not
                # rewrite that history, so the flag keeps whatever the ledger
                # already says rather than being reset to "we never acted".
                cleared.pop("submission_attempted", None)
                ledger_updates.append(cleared)
                continue
            terminal = policy_snapshot.evaluate_terminal_program(row["network"], row["program_id"])
            if terminal is not None:
                result.update(status=terminal.status, blocker=terminal.reason_code,
                              terminal_reason=terminal.reason, completed_at_utc=now())
                results.append(result)
                ledger_updates.append(_ledger_row(row, result))
                continue
            policy = policy_snapshot.evaluate_program(row.get("name"))
            if policy is not None and policy.verdict == "HOLD":
                result.update(status="POLICY_HOLD", blocker=f"operator_ruling_hold:{policy.instruction_key}",
                              policy_reason=policy.reason, completed_at_utc=now())
                results.append(result)
                ledger_updates.append(_ledger_row(row, result))
                continue
            if (execution_mode and row["network"] == "awin"
                    and row.get("awin_authority_bound") is not True):
                result.update(status="BLOCKED", blocker="awin_authenticated_manifest_authority_required",
                              completed_at_utc=now())
                results.append(result)
                ledger_updates.append(_ledger_row(row, result))
                continue
            candidate_gate = choke_candidate(_choke_projection_from_row(row),
                                              require_application_url=True, require_fit_evidence=True,
                                              operation="marketplace_adapters.portal_apply.drain", persist=True,
                                   parking_path=run_dir / "affiliate-history-parking.jsonl")
            if not candidate_gate["operational"]:
                result.update(status="PARKED", blocker=candidate_gate["decision"], gate=candidate_gate,
                              completed_at_utc=now())
                results.append(result)
                ledger_updates.append(_ledger_row(row, result))
                continue
            if not authority_lane(row.get("authority_lane") or row.get("lane")):
                result.update(status="BLOCKED", blocker=f"lane_not_authorized:{row.get('lane')}",
                              completed_at_utc=now())
                results.append(result)
                ledger_updates.append(_ledger_row(row, result))
                continue
            if submits >= daily_cap:
                result.update(status="DEFERRED", blocker="daily_application_cap_reached",
                              completed_at_utc=now())
                results.append(result)
                continue
            cls = registry.get(row["network"])
            if cls is None:
                result.update(status="BLOCKED", blocker=f"no_executor_for_network:{row['network']}",
                              completed_at_utc=now())
                results.append(result)
                ledger_updates.append(_ledger_row(row, result))
                continue
            if row["network"] in halted:
                result.update(status="DEFERRED", blocker="network_halted_after_unverified_submit",
                              completed_at_utc=now())
                results.append(result)
                continue

            executor = built.get(row["network"])
            if executor is None:
                if row["network"] == "awin" and execution_mode:
                    if ledger_binding is None:
                        raise RuntimeError("awin_canonical_ledger_binding_required")
                    ledger_binding.assert_current("before_executor_construction")
                options = dict(executor_options.get(row["network"]) or {})
                if row["network"] == "awin":
                    options.pop("auth_preflight", None)
                    options["execution_mode"] = execution_mode
                executor = cls(run_dir=run_dir, evidence_dir=run_dir / "evidence", **options)
                built[row["network"]] = executor

            if row["network"] == "awin" and execution_mode:
                ledger_binding.assert_current("before_provider_capture")
            try:
                capture = executor.capture(row)
            except Exception as exc:  # noqa: BLE001 - a portal failure is a receipt, not a crash
                capture = {"status": "BLOCKED", "blocker": f"{type(exc).__name__}:{str(exc)[:200]}"}
            result["capture_status"] = capture.get("status")
            if capture.get("status") == "ALREADY_RELATIONSHIP":
                result.update(status="ALREADY_RELATIONSHIP", confirmation_state="existing",
                              completed_at_utc=now())
                results.append(result)
                ledger_updates.append(_ledger_row(row, result))
                continue
            if capture.get("status") != "CAPTURED":
                result.update(status="BLOCKED", blocker=capture.get("blocker", "capture_failed"),
                              identity_failure_detail=capture.get("identity_failure_detail") or [],
                              operator_step=capture.get("operator_step", ""),
                              surface_url=capture.get("surface_url", ""),
                              completed_at_utc=now())
                results.append(result)
                ledger_updates.append(_ledger_row(row, result))
                continue

            verdict = gate.evaluate(capture["target"])
            result["gate"] = {k: verdict[k] for k in ("status", "receipt_path", "preflight_path")}
            if verdict["status"] != "PASS":
                result.update(status="BLOCKED", blocker="executor_gate_blocked",
                              gate_failures=verdict["failures"][:12], completed_at_utc=now())
                results.append(result)
                ledger_updates.append(_ledger_row(row, result))
                continue

            if not write_ledger:
                # Provider execution without a durable claim is never safe: a
                # crash could leave no local fact preventing a second submit.
                result.update(status="BLOCKED", blocker="durable_ledger_claim_disabled",
                              completed_at_utc=now())
                results.append(result)
                continue

            # Persist a fail-closed, explicitly UNVERIFIED attempt claim before
            # crossing the provider boundary.  If the process dies during the
            # submit or before the final commit, the next drain treats this
            # identity as consumed and requires provider readback rather than
            # guessing that nothing happened or fabricating SUBMITTED.
            claimed_at = now()
            claim_result = {
                "status": "SUBMISSION_UNVERIFIED",
                "blocker": "portal_provider_execution_claimed_outcome_unknown",
                "confirmation_state": "provider_outcome_unknown",
                "write_actions": 0,
            }
            claim_row = _ledger_row(row, claim_result)
            claim_row["portal_execution_claim"] = {
                "run_id": run_id,
                "claimed_at_utc": claimed_at,
                "state": "provider_outcome_unknown_until_final_commit",
            }
            if row["network"] == "awin" and execution_mode:
                ledger_binding.assert_current("before_durable_provider_claim")
            update_ledger([claim_row], ledger_path, ledger_binding=ledger_binding)
            submitted_ids.add(key)      # in-memory duplicate guard for this drain
            if row["network"] == "awin" and execution_mode:
                ledger_binding.assert_current("immediately_before_provider_submit")
            try:
                outcome = executor.submit(row, capture)
            except Exception as exc:  # noqa: BLE001
                outcome = {"status": "SUBMISSION_UNVERIFIED", "write_actions": 1,
                           "confirmation_state": "unconfirmed",
                           "blocker": f"{type(exc).__name__}:{str(exc)[:200]}"}
            if row["network"] == "awin" and execution_mode:
                ledger_binding.assert_current("immediately_after_provider_submit")
            result.update(outcome)
            result["submitted_at_utc"] = now() if outcome.get("write_actions") else None
            result["completed_at_utc"] = now()
            submits += int(outcome.get("write_actions") or 0)
            if outcome.get("status") == "SUBMISSION_UNVERIFIED":
                halted.add(row["network"])
            results.append(result)
            final_row = _ledger_row(row, result)
            final_row["portal_execution_claim"] = {
                "run_id": run_id,
                "claimed_at_utc": claimed_at,
                "resolved_at_utc": result["completed_at_utc"],
                "state": ("verified_final_commit" if outcome.get("status") == "SUBMITTED_VERIFIED"
                          else "provider_outcome_unverified" if outcome.get("write_actions")
                          else "no_provider_action"),
            }
            if outcome.get("status") in TERMINAL_STATUSES and outcome.get("status") != "SUBMISSION_UNVERIFIED":
                final_row["last_blocker"] = None
                final_row["operator_step"] = None
            ledger_updates.append(final_row)
    finally:
        for executor in built.values():
            executor.close()

    if ledger_binding is not None:
        ledger_binding.assert_current("before_final_ledger_commit")
    ledger_result = (update_ledger(
        ledger_updates, ledger_path, ledger_binding=ledger_binding)
        if (write_ledger and ledger_updates) else {})
    receipt = {
        "schema": "glp_portal_application_drain_v1",
        "run_id": run_id,
        "captured_at_utc": now(),
        "queued_count": len(rows),
        "submitted_verified": sum(r.get("status") == "SUBMITTED_VERIFIED" for r in results),
        "submission_unverified": sum(r.get("status") == "SUBMISSION_UNVERIFIED" for r in results),
        "already_relationship": sum(r.get("status") in {"ALREADY_RELATIONSHIP", "ALREADY_APPLIED"} for r in results),
        "blocked": sum(r.get("status") == "BLOCKED" for r in results),
        "policy_held": sum(r.get("status") == "POLICY_HOLD" for r in results),
        "terminal_non_executable": sum(r.get("status") == "terminal_provider_unavailable" for r in results),
        "deferred": sum(r.get("status") == "DEFERRED" for r in results),
        "provider_write_actions": sum(int(r.get("write_actions") or 0) for r in results),
        "sheet_write_actions": 0,
        "tracking_clicks": 0,
        "secret_printed": False,
        "operator_rulings": operator_rulings.policy_receipt(policy_snapshot),
        "applications": results,
        "unblock_cards": unblock_cards(results),
        "ledger": ledger_result,
        "run_dir": str(run_dir),
    }
    write_private(run_dir / "portal-apply-run.json", receipt)
    for result in results:
        slug = re.sub(r"[^A-Za-z0-9]+", "-", identity(result["network"], result["program_id"])).strip("-")
        write_private(run_dir / "applications" / f"{slug}.json",
                      {"schema": "glp_portal_application_receipt_v1", "run_id": run_id, **result})
    return receipt


def _ledger_row(row: dict, result: dict) -> dict:
    status = result.get("status")
    entry = {
        "network": row["network"], "program_id": row["program_id"], "name": row["name"],
        "lane": row.get("lane"), "route": "portal",
        "url": row.get("url", ""), "application_url": row.get("application_url", ""),
        "description": row.get("fit_evidence", ""), "categories": row.get("categories") or [],
        "region": row.get("region", ""), "currency": row.get("currency", ""),
        "advertiser_id": row.get("advertiser_id", ""), "group_slug": row.get("group_slug", ""),
        "application_meta": row.get("application_meta") or {},
        # A BLOCKED row STAYS in the portal queue. The blocker is recorded
        # beside it, not as its status: once the operator clears the wall the
        # next daily drain has to pick the row up again, and a terminal
        # `blocked:...` status would strand it forever (both this lane and
        # `affiliate_auto_apply` read any ledger row as already-handled).
        "status": status if status in TERMINAL_STATUSES else QUEUED_STATUS,
        "submission_attempted": bool(result.get("write_actions")),
    }
    if result.get("submitted_at_utc"):
        entry["submitted_at_utc"] = result["submitted_at_utc"]
    if result.get("confirmation_state"):
        entry["confirmation_state"] = result["confirmation_state"]
    if result.get("portal_evidence"):
        entry["portal_evidence"] = result["portal_evidence"]
    if result.get("gate"):
        entry["gate_receipt"] = result["gate"].get("receipt_path")
    if status in {"BLOCKED", "PARKED", "POLICY_HOLD", "SUBMISSION_UNVERIFIED",
                  "terminal_provider_unavailable"}:
        # `or "unknown"` used to live here and it is how the operator got a
        # `BLK-unknown` card. A blocker with no name is a machine defect, so it
        # is recorded AS one — named after the status that produced it — never
        # as the word "unknown".
        entry["last_blocker"] = str(
            result.get("blocker")
            or f"unclassified_outcome:{status}")[:180]
        entry["last_blocked_at_utc"] = result.get("completed_at_utc") or now()
        if result.get("blocker_detail"):
            entry["blocker_detail"] = str(result["blocker_detail"])[:600]
        else:
            entry["blocker_detail"] = None
        # The step must describe THIS outcome. Leaving the previous run's step
        # in place is how a 404-on-submit kept telling the operator the form
        # "asks for something the lane is not authorized to provide".
        entry["operator_step"] = result.get("operator_step") or None
        # ROUTING, decided here so every consumer reads one answer. A provider
        # whose own form is broken goes to the email lane automatically; only a
        # real human gate is ever allowed to become operator work.
        entry["outcome_class"] = outcome_class(
            entry["last_blocker"], flags=result.get("gate_flags") or result.get("flags"),
            status=status)
        entry["application_route_next"] = next_route(entry["outcome_class"])
    if status == "ALREADY_RELATIONSHIP":
        entry["current_relationship"] = "existing"
    return entry


def identity_failure_detail(results: list[dict]) -> list[dict[str, str]]:
    """Losslessly de-duplicate source/key/check findings across the run."""
    by_identity: dict[str, dict[str, str]] = {}
    for result in results:
        for raw in result.get("identity_failure_detail") or []:
            if not isinstance(raw, dict):
                continue
            item = {
                key: str(raw.get(key) or "")
                for key in ("source", "key", "check", "wanted", "actual")
            }
            if not item["source"] or not item["key"] or not item["check"]:
                continue
            encoded = json.dumps(item, sort_keys=True, separators=(",", ":"))
            by_identity[encoded] = item
    return [by_identity[key] for key in sorted(by_identity)]


def unblock_cards(results: list[dict]) -> list[dict]:
    """One card per BLOCKER CLASS, listing every affected program together."""
    grouped: dict[tuple[str, str], dict] = {}
    for result in results:
        if result.get("status") != "BLOCKED":
            continue
        blocker = str(result.get("blocker") or "unknown")
        key = (result["network"], blocker.split(":")[0])
        detail = identity_failure_detail([result])
        card = grouped.setdefault(key, {
            "network": result["network"], "blocker": blocker.split(":")[0],
            "operator_action": result.get("operator_step") or "",
            "surface_url": result.get("surface_url") or "",
            "identity_failure_detail": detail,
            "programs": [],
        })
        if detail and card["identity_failure_detail"] != detail:
            merged = {
                json.dumps(item, sort_keys=True, separators=(",", ":")): item
                for item in card["identity_failure_detail"] + detail
            }
            card["identity_failure_detail"] = [merged[key] for key in sorted(merged)]
        card["programs"].append({
            "network": result["network"], "program_id": result["program_id"], "name": result["name"],
            "lane": result.get("lane"), "url": result.get("surface_url") or "",
        })
        if not card["operator_action"] and result.get("operator_step"):
            card["operator_action"] = result["operator_step"]
    for card in grouped.values():
        card["programs"] = sorted(card["programs"], key=lambda row: (
            str(row.get("network")), str(row.get("program_id")), str(row.get("name"))))
        card["program_count"] = len(card["programs"])
        card["program_completeness"] = completeness_proof(
            card["programs"], expected_denominator=card["program_count"])
    return [grouped[key] for key in sorted(grouped)]


# ------------------------------------------------------------------------ cli

def latest_plan_path() -> Path | None:
    runs = sorted(MACHINE_DIR.glob("apply-*/application-plan.json"))
    return runs[-1] if runs else None


def verified_awin_terminal_rows(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Project exact no-retry Awin descendants back into authority binding."""
    selected: list[dict[str, Any]] = []
    for row in document.get("applications", []):
        if (not isinstance(row, dict)
                or str(row.get("network") or "").lower() != "awin"
                or row.get("status") != "SUBMITTED_VERIFIED"
                or row.get("submission_attempted") is not True):
            continue
        claim = row.get("portal_execution_claim")
        if (not isinstance(claim, dict)
                or frozenset(claim) != {
                    "run_id", "claimed_at_utc", "resolved_at_utc", "state"}
                or claim.get("state") != "verified_final_commit"
                or not re.fullmatch(
                    r"glp-awin-cron-[0-9]{8}T[0-9]{6}Z-[0-9]+",
                    str(claim.get("run_id") or ""))
                or not all(re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
                    str(claim.get(key) or ""))
                    for key in ("claimed_at_utc", "resolved_at_utc"))):
            continue
        selected.append(dict(row))
    return selected


def bind_exact_awin_ready_rows(rows: list[dict[str, Any]], authority: dict[str, Any]) -> list[dict[str, Any]]:
    """Replace the Awin lane with the exact parent-ordered ready subset.

    The current queue is evidence, not authority. Every approved parent id must
    have exactly one current row with the same immutable identity; all other Awin
    rows are excluded before an effectful executor is constructed.
    """
    # `cdp_port` is a field of the reviewed parent manifest and stays bound
    # exactly; since GLP-268 it no longer selects or attaches to any browser.
    if (str(authority.get("publisher_id") or "") != AWIN_PUBLISHER_ID
            or str(authority.get("account_name") or "") != "Tilton Group"
            or str(authority.get("site") or "") != "https://glpclinicscout.com"
            or int(authority.get("cdp_port") or 0) != 9325
            or AwinFreshBrowserTransport.PROFILE != AWIN_PROFILE_TEMPLATE):
        raise ValueError("awin_parent_publisher_or_profile_authority_mismatch")
    ready = tuple(str(value) for value in authority.get("ready_awin_program_ids") or ())
    bindings = authority.get("row_bindings") or {}
    if (not ready or len(ready) != len(set(ready)) or tuple(bindings) != ready
            or str(authority.get("ready_awin_subset_sha256") or "") != _sha256_json(list(ready))):
        raise ValueError("awin_parent_ready_bindings_count_or_order_mismatch")
    current: dict[str, list[dict[str, Any]]] = {}
    non_awin: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("network") or "").lower() != "awin":
            non_awin.append(row)
            continue
        current.setdefault(str(row.get("program_id") or ""), []).append(row)
    selected: list[dict[str, Any]] = []
    for program_id in ready:
        matches = current.get(program_id) or []
        if len(matches) != 1:
            raise ValueError(f"awin_ready_current_row_missing_or_duplicate:{program_id}")
        row, frozen_bound = matches[0], bindings[program_id]
        bound = _plain_json(frozen_bound)
        for key in ("network", "program_id", "name", "lane"):
            if str(row.get(key) or "") != str(bound.get(key) or ""):
                raise ValueError(f"awin_ready_current_row_parent_identity_drift:{program_id}:{key}")
        expected_row_sha = _sha256_json({
            key: value for key, value in bound.items()
            if key not in {"current_ledger_status", "row_authority_sha256"}
        })
        reason = str(bound.get("reason") or "").strip()
        prepared_message = str(bound.get("prepared_message") or "").strip()
        terms = bound.get("parent_terms_binding") or {}
        terms_url = str(terms.get("terms_url") or "")
        if (str(bound.get("network") or "") != "awin"
                or bound.get("execution_eligible") is not True
                or bound.get("disposition") != "SAFE_FOR_PARENT_APPLY_PENDING_LOCAL_GATE"
                or bound.get("lawful_fit_choke") != "LAWFUL_FIT_PASS"
                or bound.get("provider_route") != "awin"
                or bound.get("current_inventory_executable") is not True
                or bound.get("canonical_identity") != f"awin:{program_id}"
                or not reason
                or str(bound.get("row_authority_sha256") or "") != expected_row_sha
                or not prepared_message
                or hashlib.sha256(prepared_message.encode("utf-8")).hexdigest()
                != str(bound.get("prepared_message_sha256") or "")
                or not re.search(r"\bGLP(?:[- ]?1)\b", prepared_message, re.I)
                or not re.search(r"\b(?:US|United States)\b", prepared_message, re.I)
                or not terms_url.startswith(f"/awin/affiliate/{AWIN_PUBLISHER_ID}/")
                or program_id not in terms_url
                or not re.fullmatch(r"[0-9a-f]{64}", str(terms.get("terms_pdf_sha256") or ""))
                or not re.fullmatch(r"[0-9a-f]{64}", str(terms.get("terms_text_sha256") or ""))):
            raise ValueError(f"awin_ready_parent_binding_not_exact_lawful_fit:{program_id}")

        # No mutable application plan is allowed to contribute these fields to
        # the Awin authority lane. They are projected only from the validated
        # immutable parent binding below. The sole replay exception is the exact
        # canonical projection this machine persists for a queued row; this keeps
        # retries live without allowing generic input to become provenance.
        profile_url = AWIN_PROFILE_TEMPLATE.format(program_id=program_id)
        if profile_url != (
            f"https://ui.awin.com/awin/affiliate/{AWIN_PUBLISHER_ID}/merchant-profile/{program_id}"):
            raise ValueError(f"awin_authenticated_profile_url_mismatch:{program_id}")
        replay_projection = {
            "url": profile_url,
            "application_url": profile_url,
            "description": reason,
            "categories": ("GLP-1", str(bound["lane"])),
            "region": "United States",
        }
        protected = (
            "url", "application_url", "description", "evidence_excerpt", "fit_evidence",
            "fit_reason", "reason", "categories", "category", "vertical", "tags",
            "region", "geo", "destination", "country",
        )
        supplied = [key for key in protected if row.get(key) not in (None, "", (), [])]
        invalid = []
        for key in supplied:
            if key not in replay_projection:
                invalid.append(key)
                continue
            actual = row.get(key)
            expected = replay_projection[key]
            if key == "categories":
                if not isinstance(actual, (list, tuple)) or tuple(actual) != expected:
                    invalid.append(key)
            elif actual != expected:
                invalid.append(key)
        if invalid:
            raise ValueError(
                f"awin_generic_plan_authority_field_override:{program_id}:{','.join(invalid)}")
        projected = dict(row)
        projected.pop("description", None)
        projected.update({
            "url": profile_url,
            "application_url": profile_url,
            "fit_evidence": reason,
            "fit_reason": reason,
            # Both normalized values are required by the unchanged generic
            # choke and are grounded in the validated US/GLP prepared message;
            # the exact parent lane remains present as the second category.
            "categories": ("GLP-1", str(bound["lane"])),
            "region": "United States",
        })
        authorized_lane = (
            "supplements-nutrition" if str(bound["lane"]) == "supplements-vitamins"
            else str(bound["lane"])
        )
        if not authority_lane(authorized_lane):
            raise ValueError(f"awin_ready_parent_lane_not_authorized:{program_id}:{bound['lane']}")
        selected.append(_AuthenticatedAwinQueueRow(
            projected, authority_row_sha256=expected_row_sha,
            choke_identity=f"awin:{program_id}", authorized_lane=authorized_lane,
        ))
    return non_awin + selected


def _read_awin_token_from_anonymous_pipe(
        fd: int, *, argv: list[str] | tuple[str, ...] | None = None,
        environ: dict[str, str] | os._Environ[str] | None = None) -> str:
    """Read one bounded token from a closed-writer anonymous pipe only.

    ``S_ISFIFO`` alone also admits filesystem FIFOs.  Linux exposes an anonymous
    pipe as ``pipe:[inode]`` through procfs, so both checks are required before
    any secret byte is consumed.  Nonblocking reads make an inherited writer or
    other incomplete launcher composition fail closed instead of hanging.
    """
    if fd < 3:
        raise SystemExit("Awin token fd must be a dedicated anonymous pipe descriptor")
    try:
        descriptor = os.fstat(fd)
        target = os.readlink(f"/proc/self/fd/{fd}")
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    except (OSError, ValueError) as exc:
        raise SystemExit("Awin token fd was invalid or provenance was unavailable") from exc
    if (not stat.S_ISFIFO(descriptor.st_mode)
            or not re.fullmatch(r"pipe:\[[1-9][0-9]*\]", target)
            or flags & os.O_ACCMODE != os.O_RDONLY):
        raise SystemExit("Awin token fd was not the read end of an anonymous pipe")

    chunks: list[bytes] = []
    total = 0
    try:
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        while True:
            try:
                chunk = os.read(fd, 4097 - total)
            except BlockingIOError as exc:
                raise SystemExit("Awin token pipe did not have closed-writer EOF") from exc
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > 4096:
                raise SystemExit("Awin token pipe exceeded the bounded token size")
    finally:
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, flags)
        except OSError:
            pass

    raw = b"".join(chunks)
    try:
        token = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit("Awin token pipe was not valid UTF-8") from exc
    if (not token or token != token.strip() or "\x00" in token
            or "\n" in token or "\r" in token):
        raise SystemExit("Awin token pipe contained an invalid token shape")

    effective_argv = tuple(sys.argv[1:] if argv is None else argv)
    effective_environ = os.environ if environ is None else environ
    if any(token in str(value) for value in effective_argv):
        raise SystemExit("Awin token bytes were present in argv")
    if any(token in str(value) for value in effective_environ.values()):
        raise SystemExit("Awin token bytes were present in the environment")
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drain the affiliate portal application queue.")
    parser.add_argument("--execute", action="store_true",
                        help="required; without it the lane refuses to submit anything")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--ledger", default=str(LEDGER_PATH))
    parser.add_argument("--plan", default="", help="application-plan.json to join URLs from")
    parser.add_argument("--network", action="append", default=[],
                        help="restrict the drain to these networks")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--daily-cap", type=int, default=25)
    parser.add_argument("--awin-manifest", default="",
                        help="reviewed repaired exact-11 manifest; required for any Awin execution authority")
    parser.add_argument("--awin-token-fd", type=int, default=-1,
                        help="already-open fd containing the Awin token; execute mode only")
    parser.add_argument("--awin-cdp-endpoint", default="",
                        help="retired (GLP-268): any value is refused; a fresh browser is used per job")
    parser.add_argument("--awin-auth-preflight-only", action="store_true",
                        help="verify exact Awin identity via UI or official GET /accounts; no rows")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.awin_auth_preflight_only:
        if (args.execute or args.awin_manifest or args.network
                or args.limit or args.run_id or args.plan or args.ledger != str(LEDGER_PATH)):
            raise SystemExit("Awin auth preflight forbids drain/manifest/ledger arguments")
        if args.awin_token_fd < 0:
            raise SystemExit("Awin auth preflight requires an anonymous token fd")
        transport = None
        try:
            token = _read_awin_token_from_anonymous_pipe(
                args.awin_token_fd, argv=sys.argv[1:] if argv is None else argv)
            api_identity = OfficialAwinReadClient(token, AWIN_PUBLISHER_ID).verify_account()
            token = ""
            transport = AwinFreshBrowserTransport(
                args.awin_cdp_endpoint, api_identity=api_identity)
            report = transport.authenticate_and_verify()
        except AwinAuthFailure as exc:
            print("AWIN_AUTH_FAILURE_V1 " + json.dumps(
                AwinFreshBrowserTransport.failure_payload(exc),
                sort_keys=True, separators=(",", ":")), file=sys.stderr)
            return 78
        finally:
            if transport is not None:
                with contextlib.suppress(Exception):
                    transport.close()
        print(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return 0

    plan = Path(args.plan) if args.plan else latest_plan_path()
    wanted = {n.strip().lower() for n in args.network} if args.network else set()
    ledger_path = Path(args.ledger)
    bind_awin_ledger = args.execute and (not wanted or "awin" in wanted)
    opening_ledger_identity: tuple[int, int] | None = None
    if bind_awin_ledger:
        # Reject alternate replay ledgers before the first ledger/token read or
        # construction of any provider-facing object.
        ledger_path, opening_ledger_identity = _require_exact_awin_execute_ledger_identity(args.ledger)

    def selected_rows(ledger_document: dict[str, Any] | None = None) -> list[dict]:
        current_document = ledger_document if ledger_document is not None else load_ledger(ledger_path)
        selected = enrich_urls(queued_rows(current_document), plan)
        if bind_awin_ledger:
            selected.extend(verified_awin_terminal_rows(current_document))
        if wanted:
            selected = [row for row in selected
                        if str(row.get("network", "")).lower() in wanted]
        return selected

    if not args.execute:
        rows = selected_rows()
        if args.limit:
            rows = rows[:args.limit]
        print(json.dumps({"status": "DRY_RUN", "queued": len(rows),
                          "networks": sorted({str(r.get("network")) for r in rows})}, sort_keys=True))
        return 0

    # One continuous critical section owns the canonical ledger before its first
    # execute-mode read, through manifest/descendant authentication, exact-row
    # binding, durable claim, provider boundary, and final readback commit.
    with _canonical_ledger_writer_guard(ledger_path):
        binding_context = (_ExactAwinLedgerBinding(
            ledger_path, expected_opening_identity=opening_ledger_identity)
                           if bind_awin_ledger else contextlib.nullcontext(None))
        with binding_context as ledger_binding:
            opening_document = (ledger_binding.document("before_first_execute_read")
                                if ledger_binding is not None else None)
            rows = selected_rows(opening_document)
            has_awin = any(str(row.get("network") or "").lower() == "awin" for row in rows)
            executor_options: dict[str, dict[str, Any]] = {}
            if has_awin:
                if ledger_binding is None:
                    raise SystemExit("Awin execution requires a stable canonical ledger binding")
                # Also covers all-network execution where Awin was discovered only
                # after the first locked ledger read. The open descriptor remains
                # bound through token, constructors, claim, provider action, and
                # final commit; same-byte pathname replacement is not accepted.
                ledger_binding.assert_current("before_manifest_authentication")
                if args.limit:
                    raise SystemExit("Awin exact parent authority forbids --limit subset truncation")
                if not args.awin_manifest:
                    raise SystemExit("Awin execution requires --awin-manifest exact reviewed authority")
                manifest = json.loads(Path(args.awin_manifest).read_text(encoding="utf-8"))
                try:
                    authority = validate_execution_authority_manifest(manifest)
                    rows = bind_exact_awin_ready_rows(rows, authority)
                except Exception as exc:
                    raise SystemExit(f"Awin manifest failed exact repaired authority validation:{exc}") from exc
                if args.awin_token_fd < 0:
                    raise SystemExit("Awin execution requires --awin-token-fd")
                ledger_binding.assert_current("immediately_before_token_consumption")
                token = _read_awin_token_from_anonymous_pipe(
                    args.awin_token_fd, argv=sys.argv[1:] if argv is None else argv)
                ledger_binding.assert_current("before_cdp_transport_construction")
                try:
                    # API first; the fresh per-job browser launches only after
                    # the official account identity has been read.
                    api = OfficialAwinReadClient(token, authority["publisher_id"])
                    api_identity = api.verify_account()
                    cdp_transport = AwinFreshBrowserTransport(
                        args.awin_cdp_endpoint, api_identity=api_identity)
                    auth_preflight = cdp_transport.authenticate_and_verify()
                except AwinAuthFailure as exc:
                    print("AWIN_AUTH_FAILURE_V1 " + json.dumps(
                        AwinFreshBrowserTransport.failure_payload(exc),
                        sort_keys=True, separators=(",", ":")), file=sys.stderr)
                    return 78
                ledger_binding.assert_current("before_authenticated_adapter_construction")
                cdp = AuthenticatedAwinCDPAdapter(
                    cdp_transport, publisher_id=authority["publisher_id"],
                    account_name_contains=authority["account_name"], website=authority["site"],
                    cdp_endpoint=args.awin_cdp_endpoint,
                )
                ledger_binding.assert_current("before_awin_api_client_construction")
                executor_options["awin"] = {
                    "authority_program_ids": authority["ready_awin_program_ids"],
                    "authority_subset_sha256": authority["ready_awin_subset_sha256"],
                    "row_bindings": authority["row_bindings"],
                    "expected_identity": {"publisher_id": authority["publisher_id"],
                                          "account_id": authority["publisher_id"],
                                          "legal_name": authority["account_name"],
                                          "website": authority["site"]},
                    "awin_api": api, "awin_ui": cdp,
                    "auth_preflight": auth_preflight,
                }
                ledger_binding.assert_current("immediately_before_drain")
            elif args.limit:
                rows = rows[:args.limit]
            receipt = _drain_locked(
                rows, run_id=args.run_id, ledger_path=ledger_path, daily_cap=args.daily_cap,
                execution_mode=True, executor_options=executor_options,
                ledger_binding=ledger_binding if has_awin else None)

    # Bind provider outcomes to the exact application plan used to enrich the
    # queue. The machine card joins these immutable receipts for per-network
    # denominator accounting; no live provider state is inferred.
    receipt["plan"] = str(plan)
    write_private(Path(receipt["run_dir"]) / "portal-apply-run.json", receipt)

    summary = {key: receipt[key] for key in (
        "run_id", "queued_count", "submitted_verified", "submission_unverified",
        "already_relationship", "blocked", "deferred", "provider_write_actions", "run_dir", "plan")}
    summary["unblock_cards"] = receipt["unblock_cards"]
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
