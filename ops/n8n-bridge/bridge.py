#!/usr/bin/env python3
"""n8n -> box webhook bridge.

Small authenticated HTTP endpoint bound to 127.0.0.1:8791 that lets a cloud n8n
instance (via a Cloudflare tunnel) do exactly three things on this machine:

  1. run allowlisted local actions (actions.json) -- no arbitrary commands
  2. run LLM calls on the operator's local CLI subscriptions (hermes / claude)
  3. read status

Design notes:
  * HTTP/runtime logic stays stdlib; Ed25519 entry admissions use the host's
    existing `cryptography` package.
  * Bearer token auth on every route, constant-time compare. Token never logged.
  * Jobs run in worker threads with a hard concurrency cap; timeouts kill the
    whole process group so no orphan children survive.
  * Every job writes a 0600 receipt JSON under reports/ops/n8n-bridge-jobs/.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

HOST = "127.0.0.1"
PORT = int(os.environ.get("N8N_BRIDGE_PORT", "8791"))

HOME = os.path.expanduser("~")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.environ.get(
    "N8N_BRIDGE_TOKEN_FILE", os.path.join(HOME, ".hermes/secrets/n8n-bridge/token")
)
ONBOARDING_TOKEN_PATH = os.environ.get(
    "N8N_BRIDGE_ONBOARDING_TOKEN_FILE",
    os.path.join(HOME, ".hermes/secrets/n8n-bridge/onboarding-token"),
)
ONBOARDING_SIGNING_KEY_PATH = os.environ.get(
    "N8N_BRIDGE_ONBOARDING_SIGNING_KEY_FILE",
    os.path.join(HOME, ".hermes/secrets/n8n-bridge/onboarding-entry-ed25519-private.pem"),
)
ACTIONS_PATH = os.environ.get(
    "N8N_BRIDGE_ACTIONS_FILE", os.path.join(BASE_DIR, "actions.json")
)
RECEIPT_DIR = os.environ.get(
    "N8N_BRIDGE_RECEIPT_DIR",
    "/home/ubuntu/business/reports/ops/n8n-bridge-jobs",
)
JOB_STATE_DIR = os.path.join(RECEIPT_DIR, ".job-state")

# Separate from analytics_brain.lock (held by the cadence child) and the
# reporting scheduler locks.  Never unlink this inode: the installer takes EX
# on the same path before the old-byte gate bootstrap and through readback.
SOURCE_ADMISSION_LOCK = "/home/ubuntu/business/ops/glp/glp44-source-admission.lock"
SOURCE_READER_ACTIONS = frozenset({
    "analytics_brain", "affiliate_postback_receive", "topic_data_score",
    "glp_ads_monitor", "glp_ads_monitor_daily_digest",
})

MAX_CONCURRENT_JOBS = int(os.environ.get("N8N_BRIDGE_MAX_JOBS", "3"))
MAX_QUEUED_JOBS = int(os.environ.get("N8N_BRIDGE_MAX_QUEUED_JOBS", "100"))
#: Every job thread carries this name prefix so a leaked concurrency slot can
#: be detected by OBSERVING the work in flight instead of trusting a counter.
JOB_THREAD_PREFIX = "bridge-job-"
#: How long running > live_job_threads must persist before it counts as a leak.
#: Longer than any reserve-to-thread-start window, shorter than any idle gate.
SLOT_LEAK_GRACE_S = float(os.environ.get("N8N_BRIDGE_SLOT_LEAK_GRACE_S", "30"))
JOB_HISTORY = int(os.environ.get("N8N_BRIDGE_JOB_HISTORY", "200"))
SYNC_WAIT_S = float(os.environ.get("N8N_BRIDGE_SYNC_WAIT_S", "25"))

# Admission control (added 2026-08-18 after the 429 storm that failed
# glp/topic-supply exec 81 and, in the same class, other lanes).
#
# The cap is real and stays: three concurrent jobs is what this box can carry.
# Callback callers enter the bounded FIFO below and consume no slot until they
# run. Synchronous callers cannot hand execution off to a callback, so they wait
# here for up to N8N_BRIDGE_ADMIT_WAIT_S before being told no.
#
# Default 0 = the historical instant-reject behaviour, so nothing changes for a
# bridge started without the env var (and the pre-existing concurrency test
# keeps testing the reject path). Production sets it in the systemd unit.
ADMIT_WAIT_S = float(os.environ.get("N8N_BRIDGE_ADMIT_WAIT_S", "0"))
# What a rejected caller is told to wait. Advertised in the Retry-After header
# AND in the JSON body, so a client that cannot read headers still learns it.
RETRY_AFTER_S = int(os.environ.get("N8N_BRIDGE_RETRY_AFTER_S", "30"))

# Callback delivery. A caller using callback_url gets its 202 immediately, then
# has to *register* the listener -- for n8n that means the execution still has
# to walk from the HTTP Request node into the Wait node before its resume URL
# exists. A job that finishes in milliseconds (echo_test returns in ~3ms) would
# otherwise be delivered into a 404 and the caller would hang until its own
# timeout: a false failure. So: wait out a short grace, then retry a rejected
# delivery on a backoff instead of dropping it after one attempt.
CALLBACK_GRACE_S = float(os.environ.get("N8N_BRIDGE_CALLBACK_GRACE_S", "3"))
CALLBACK_RETRY_DELAYS = [
    float(value)
    for value in os.environ.get(
        "N8N_BRIDGE_CALLBACK_RETRY_DELAYS_S", "3,8,20,45,90"
    ).split(",")
    if value.strip()
]
CALLBACK_TIMEOUT_S = 30

CAP_CAPTURE = 256 * 1024   # bytes retained per stream while running
# Bytes reported back to the caller. Raised from 8 KiB -> 64 KiB on 2026-08-18:
# the reddit-scanner lane returns its whole candidate set (and the model returns
# whole comment drafts) on stdout, and an 8 KiB tail silently ate the head of a
# JSON payload, which then failed to parse in n8n for no visible reason.
CAP_STDOUT_TAIL = int(os.environ.get("N8N_BRIDGE_STDOUT_TAIL", 64 * 1024))
CAP_STDERR_TAIL = int(os.environ.get("N8N_BRIDGE_STDERR_TAIL", 2 * 1024))
CAP_FAILURE_DIAGNOSTIC = min(
    CAP_STDERR_TAIL,
    int(os.environ.get("N8N_BRIDGE_FAILURE_DIAGNOSTIC", "1000")),
)
MAX_BODY = 4 * 1024 * 1024

# Ordinary tier-2 stays at 60 turns / 7,200 seconds. The repeat-offender lane is
# admitted up to 90 turns / 10,800 seconds after two real 60-turn deep workers
# exhausted their turns before writing mandatory closure artifacts.
HERMES_TIMEOUT_MAX = 10800
HERMES_MAX_TURNS = 90
CLAUDE_TIMEOUT_MAX = 3600
DEFAULT_HERMES_TIMEOUT = 1800
DEFAULT_CLAUDE_TIMEOUT = 600

HERMES_MODELS = {"gpt-5.6-sol", "gpt-5.6-luna", "gpt-5.6-kael", "gpt-6-astra"}
HERMES_REASONING = {"low", "medium", "high", "xhigh"}
HERMES_TOOLSET_PROFILES = {
    "none": "",
    "authoring": "web,file,terminal,firecrawl",
}

ARG_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
STRUCTURED_FAILURE_FIELDS = ("why", "reason", "error", "failure", "message")
URL_IN_DIAGNOSTIC_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:authorization|bearer|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|password|cookie|session)\b\s*[:=]\s*[^\s,;]+"
)

# Linux caps a SINGLE argv element at MAX_ARG_STRLEN (32 pages = 128 KB),
# separately from the far larger total-argv limit. Cross it and execve fails
# E2BIG before the target program starts, so the failure carries no application
# error at all: on 2026-08-25 glp/sheet-sync died rc=126 in 0.111s with empty
# stdout and n8n reported only "Drain Fail Loud: 'python3' [line 2]".
#
# We spill well below the ceiling because the kernel counts the raw bytes and an
# action's own argv sits alongside.
MAX_ARG_STRLEN = 32 * 4096
ARG_SPILL_THRESHOLD = int(os.environ.get("N8N_BRIDGE_ARG_SPILL_BYTES", 64 * 1024))

START_TIME = time.time()
INSTANCE_ID = uuid.uuid4().hex
GLOBAL_AUTH_SCOPE = "global"
ONBOARDING_AUTH_SCOPE = "onboarding_manifest"
ENTRY_AUTHORITY_SCHEMA = "n8n_bridge.entry_authority.v1"
ENTRY_AUTHORITY_TTL_S = 90

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def log(msg: str) -> None:
    """Structured-ish stderr log. Never receives the token."""
    sys.stderr.write("[%s] %s\n" % (utc_now(), msg))
    sys.stderr.flush()


def _load_token_file(path: str, *, required: bool) -> str:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if not required:
            return ""
        raise
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeError("bridge token is not a regular file: %s" % path)
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("bridge token permissions must be 0600: %s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        tok = fh.read().strip()
    if not tok:
        raise RuntimeError("bridge token file is empty: %s" % path)
    return tok


def load_token() -> str:
    return _load_token_file(TOKEN_PATH, required=True)


def load_scoped_tokens() -> dict[str, str]:
    token = _load_token_file(ONBOARDING_TOKEN_PATH, required=False)
    return {ONBOARDING_AUTH_SCOPE: token} if token else {}


def _canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_entry_signing_key() -> Ed25519PrivateKey:
    info = os.lstat(ONBOARDING_SIGNING_KEY_PATH)
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise RuntimeError("onboarding entry signing key is not a regular file")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("onboarding entry signing key permissions must be 0600")
    with open(ONBOARDING_SIGNING_KEY_PATH, "rb") as fh:
        value = serialization.load_pem_private_key(fh.read(), password=None)
    if not isinstance(value, Ed25519PrivateKey):
        raise RuntimeError("onboarding entry signing key type is invalid")
    return value


def mint_entry_authority(cmd: dict, *, now_epoch: int | None = None) -> dict:
    """Issue one short-lived admission bound to the exact manifest argv."""
    if cmd.get("entry_authority_scope") != ONBOARDING_AUTH_SCOPE:
        raise RuntimeError("entry authority scope invalid")
    argv = list(cmd.get("argv") or [])
    if len(argv) < 2 or argv[1] != "scripts/glp_onboarding_manifest.py":
        raise RuntimeError("entry authority target invalid")
    issued = int(time.time()) if now_epoch is None else int(now_epoch)
    unsigned = {
        "schema": ENTRY_AUTHORITY_SCHEMA,
        "scope": ONBOARDING_AUTH_SCOPE,
        "action_name": str(cmd.get("name") or ""),
        # Python drops the interpreter from sys.argv; sign exactly what the
        # manifest can recompute after removing the injected FD pair.
        "argv_sha256": hashlib.sha256(_canonical_bytes(argv[1:])).hexdigest(),
        "issued_at_epoch": issued,
        "expires_at_epoch": issued + ENTRY_AUTHORITY_TTL_S,
        "nonce": secrets.token_hex(24),
    }
    signature = _load_entry_signing_key().sign(_canonical_bytes(unsigned))
    return {
        **unsigned,
        "ed25519_signature_b64": base64.b64encode(signature).decode("ascii"),
    }


def load_actions() -> dict:
    with open(ACTIONS_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise RuntimeError("actions.json must be a JSON object")
    return data


def tail_bytes(buf: bytes, cap: int) -> str:
    if len(buf) > cap:
        buf = buf[-cap:]
    return buf.decode("utf-8", errors="replace")


def _diagnostic_scalar(value) -> str:
    if isinstance(value, str):
        text = value
    elif value is None:
        return ""
    else:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    text = " ".join(text.split())
    text = URL_IN_DIAGNOSTIC_RE.sub("[redacted-url]", text)
    text = SECRET_ASSIGNMENT_RE.sub("[redacted-secret]", text)
    return text


def structured_failure_diagnostic(stdout: bytes, rc) -> dict | None:
    """Extract one bounded safe failure reason from a structured stdout receipt.

    A large family of registered machines emits its authoritative receipt on
    stdout and leaves stderr empty. n8n resolvers historically checked rc first
    and surfaced only ``stderr_tail``, so the transport erased a perfectly good
    ``why`` before the error workflow/fixer could see it. Preserve raw stream
    provenance in explicit fields, but promote the named reason into the
    existing compatibility field those resolvers already consume.
    """
    if not isinstance(rc, int) or rc == 0 or not stdout:
        return None
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    candidates = [text]
    candidates.extend(
        line.strip() for line in reversed(text.splitlines()) if line.strip()
    )
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            payload = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        for field in STRUCTURED_FAILURE_FIELDS:
            reason = _diagnostic_scalar(payload.get(field))
            if not reason:
                continue
            status = _diagnostic_scalar(payload.get("status")) or "UNKNOWN"
            diagnostic = "structured_failure status=%s %s=%s" % (
                status, field, reason)
            return {
                "text": diagnostic[:CAP_FAILURE_DIAGNOSTIC],
                "source": "stdout_json.%s" % field,
                "field": field,
                "status": status[:80],
            }
    return None


def spill_stdout(job_id: str, buf: bytes) -> str:
    """Park the full captured stdout next to the receipt and return its path.

    2026-08-26, glp/marketplace-enumeration (iOJhvTtehlHGLUSs): the materializer
    finished rc=0 and wrote a valid report, but its report was larger than
    CAP_STDOUT_TAIL, so the bridge handed the workflow a tail that started in the
    middle of a JSON document. The Materializer Resolve node parsed that fragment
    as a complete receipt and the machine reported failure on a successful run.

    A tail is not a document. When one is all that fits inline, the caller gets a
    path to the whole thing and a flag saying so.
    """
    os.makedirs(RECEIPT_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(RECEIPT_DIR, "%s-%s.stdout" % (utc_stamp(), job_id))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(buf)
    return path


JOB_ID_RE = re.compile(r"^[0-9a-f]{16}$")
PERSIST_LOCK = threading.RLock()


def _process_start_ticks(pid: int):
    try:
        # comm may contain spaces and parentheses; field 22 is index 19 after
        # the final ')' that terminates comm.
        raw = open("/proc/%d/stat" % pid, "r", encoding="utf-8").read()
        return int(raw.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


PROCESS_START_TICKS = _process_start_ticks(os.getpid())


def _state_owner_is_alive(state: dict) -> bool:
    """Distinguish a live blue/green peer from a replaced bridge process."""
    try:
        pid = int(state.get("_bridge_pid"))
        expected_ticks = int(state.get("_bridge_start_ticks"))
    except (TypeError, ValueError):
        return False
    if _process_start_ticks(pid) != expected_ticks:
        return False
    try:
        cmdline = open("/proc/%d/cmdline" % pid, "rb").read().split(b"\0")
    except OSError:
        return False
    return os.path.realpath(__file__).encode("utf-8") in cmdline


def _atomic_json_write(path: str, payload: dict) -> None:
    """Write one private JSON object atomically without exposing partial bytes."""
    parent = os.path.dirname(path)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp = "%s.tmp-%d-%d" % (path, os.getpid(), threading.get_ident())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_private_json(path: str):
    """Read only a regular, non-symlink JSON object from the receipt tree."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else None
    except (FileNotFoundError, OSError, ValueError):
        return None


def _job_state_path(job_id: str) -> str:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError("invalid job id")
    return os.path.join(JOB_STATE_DIR, "%s.json" % job_id)


def persist_job_record(record: dict) -> None:
    """Persist a sanitized lifecycle record before acknowledging a job."""
    job_id = str(record.get("job_id") or "")
    payload = dict(record)
    # Capability callback URLs and command details are deliberately absent from
    # records. These private fields distinguish live state from state left by a
    # bridge process that systemd replaced.
    payload["_bridge_instance_id"] = INSTANCE_ID
    payload["_bridge_pid"] = os.getpid()
    payload["_bridge_start_ticks"] = PROCESS_START_TICKS
    payload["_updated_utc"] = utc_now()
    with PERSIST_LOCK:
        _atomic_json_write(_job_state_path(job_id), payload)


def _public_state(state: dict) -> dict:
    return {key: value for key, value in state.items() if not key.startswith("_")}


def _receipt_candidates(job_id: str) -> list[str]:
    """Return exact top-level terminal receipts for one validated job id."""
    if not JOB_ID_RE.fullmatch(job_id):
        return []
    suffix = "-%s.json" % job_id
    try:
        names = [
            entry.name
            for entry in os.scandir(RECEIPT_DIR)
            if entry.is_file(follow_symlinks=False) and entry.name.endswith(suffix)
        ]
    except OSError:
        return []
    return [os.path.join(RECEIPT_DIR, name) for name in sorted(names, reverse=True)]


def _read_terminal_receipt(job_id: str, state: dict | None = None):
    paths = []
    if state:
        state_path = state.get("receipt_path")
        if isinstance(state_path, str):
            expected_parent = os.path.realpath(RECEIPT_DIR)
            if os.path.dirname(os.path.realpath(state_path)) == expected_parent:
                paths.append(state_path)
    paths.extend(path for path in _receipt_candidates(job_id) if path not in paths)
    for path in paths:
        result = _read_private_json(path)
        if result and result.get("job_id") == job_id:
            result.setdefault("receipt_path", path)
            return result
    return None


def load_durable_job(job_id: str):
    """Recover an evicted/restarted job, never converting known work to 404.

    Terminal receipts are authoritative. A nonterminal record owned by a prior
    bridge instance means systemd interrupted the worker. It becomes an explicit
    terminal bridge error and is never re-run, because its external effects may
    be ambiguous.
    """
    if not JOB_ID_RE.fullmatch(job_id):
        return None
    with PERSIST_LOCK:
        state = _read_private_json(_job_state_path(job_id))
        receipt = _read_terminal_receipt(job_id, state)
        if receipt is not None:
            return receipt
        if state is None:
            return None

        public = _public_state(state)
        if (
            state.get("_bridge_instance_id") != INSTANCE_ID
            and state.get("status") in {"queued", "accepted", "running"}
            and not _state_owner_is_alive(state)
        ):
            result = {
                "job_id": job_id,
                "action": state.get("action"),
                "name": state.get("name"),
                "auth_scope": state.get("auth_scope"),
                "rc": 125,
                "stdout_tail": "",
                "stderr_tail": (
                    "bridge service restarted before a terminal receipt was written; "
                    "external effects may be ambiguous; job was not re-run"
                ),
                "stdout_truncated": False,
                "stdout_bytes": 0,
                "stdout_file": None,
                "queued_utc": state.get("queued_utc"),
                "started_utc": state.get("started_utc"),
                "finished_utc": utc_now(),
                "duration_s": 0.0,
                "timed_out": False,
                "termination": {
                    "classification": "BRIDGE_INTERRUPTED",
                    "signal_number": None,
                    "signal_name": None,
                },
                "status": "error",
                "interrupted": True,
                "interruption_reason": "bridge_restart_before_terminal_receipt",
            }
            result["receipt_path"] = write_receipt(result)
            persist_job_record(result)
            log("recovered interrupted job=%s as terminal rc=125" % job_id)
            return result
        return public


def persist_job_annotations(job_id: str, **fields) -> None:
    """Persist callback delivery outcome into live and durable readback."""
    STORE.annotate(job_id, **fields)
    with PERSIST_LOCK:
        record = STORE.get(job_id)
        if record is None:
            state = _read_private_json(_job_state_path(job_id))
            record = _read_terminal_receipt(job_id, state) or (
                _public_state(state) if state else None
            )
        if record is None:
            log("callback annotation persistence FAILED unknown job=%s" % job_id)
            return
        record.update(fields)
        receipt_path = record.get("receipt_path")
        if isinstance(receipt_path, str):
            _atomic_json_write(receipt_path, record)
        persist_job_record(record)


def child_env(job_id: str = "") -> dict:
    """The child's environment, stamped with its bridge admission.

    An engine-routed step must be able to PROVE it was admitted by the bridge
    rather than hand-run: these two variables plus the job-state record under
    JOB_STATE_DIR are that proof. They are identifiers, never credentials.
    """
    env = dict(os.environ)
    if job_id:
        env["N8N_BRIDGE_JOB_ID"] = str(job_id)
        env["N8N_BRIDGE_INSTANCE_ID"] = str(INSTANCE_ID)
        env["N8N_BRIDGE_JOB_STATE_DIR"] = JOB_STATE_DIR
    extra = [
        os.path.join(HOME, ".local/bin"),
        os.path.join(HOME, ".npm-global/bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    parts = [p for p in env.get("PATH", "").split(":") if p]
    for p in extra:
        if p not in parts:
            parts.append(p)
    env["PATH"] = ":".join(parts)
    env.setdefault("HOME", HOME)
    return env


# --------------------------------------------------------------------------
# Job store
# --------------------------------------------------------------------------


class JobStore:
    def __init__(self) -> None:
        self._lock = threading.Condition()
        self._jobs: "OrderedDict[str, dict]" = OrderedDict()
        self._events: dict = {}
        self._queue = deque()
        self._eligible = set()
        self._next_ticket = 0
        self.running = 0
        #: Slot-leak accounting. `running` is a counter, not a fact: any path
        #: that reserves a slot and then dies without releasing pins it forever,
        #: and every idle gate that waits for jobs_running==0 then waits for
        #: ever. Measured 2026-09-03: jobs_running stuck at 1 for >10 minutes
        #: with zero live job threads and zero non-terminal receipts, which is
        #: what the remainder deploy lane's serialization gate reads.
        self.slot_leaks_healed = 0
        self.last_leak_healed_utc = None
        self.last_leak_detail = None
        self._leak_since = None

    def try_reserve(self, wait_s: float = 0.0) -> bool:
        """Reserve a concurrency slot, optionally waiting up to `wait_s` for one.

        `wait_s <= 0` is the original instant answer. A positive wait makes an
        ordinary scheduling overlap queue instead of 429-ing; the cap itself is
        never exceeded.
        """
        deadline = time.monotonic() + max(0.0, wait_s)
        with self._lock:
            while True:
                # A synchronous caller may wait for capacity, but it must not
                # leapfrog callback jobs the bridge has already accepted.
                if self.running < MAX_CONCURRENT_JOBS and not self._eligible:
                    self.running += 1
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._lock.wait(timeout=min(remaining, 1.0))

    def enqueue(self, eligible: bool = True):
        """Add one callback job to the bounded FIFO admission queue.

        Queued jobs consume no execution slot. The bound prevents a caller storm
        from replacing honest backpressure with unbounded threads and memory.
        """
        with self._lock:
            if len(self._queue) >= MAX_QUEUED_JOBS:
                return None
            self._next_ticket += 1
            ticket = self._next_ticket
            self._queue.append(ticket)
            if eligible:
                self._eligible.add(ticket)
            self._lock.notify_all()
            return ticket

    def cancel_queued(self, ticket: int) -> None:
        with self._lock:
            try:
                self._queue.remove(ticket)
            except ValueError:
                return
            self._eligible.discard(ticket)
            self._lock.notify_all()

    def claim_queued(self, ticket: int) -> None:
        """Claim the oldest eligible ticket, skipping fence waiters without slots."""
        with self._lock:
            self._eligible.add(ticket)
            self._lock.notify_all()
            while True:
                if (
                    next((t for t in self._queue if t in self._eligible), None) == ticket
                    and self.running < MAX_CONCURRENT_JOBS
                ):
                    self._queue.remove(ticket)
                    self._eligible.remove(ticket)
                    self.running += 1
                    self._lock.notify_all()
                    return
                self._lock.wait(timeout=1.0)

    def release(self) -> None:
        with self._lock:
            self.running = max(0, self.running - 1)
            self._lock.notify_all()

    def running_count(self) -> int:
        with self._lock:
            return self.running

    def queued_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def live_job_threads(self) -> int:
        """How many job-executing threads are actually alive right now.

        Every job runs on a thread named JOB_THREAD_PREFIX + job_id (both the
        sync and the queued admission paths), so this is a direct observation of
        the work in flight rather than a number we incremented and hoped to
        decrement.
        """
        return sum(
            1 for thread in threading.enumerate()
            if thread.is_alive() and str(thread.name).startswith(JOB_THREAD_PREFIX)
        )

    def reconcile(self) -> dict:
        """Heal a leaked concurrency counter, and say so out loud.

        `running` legitimately runs AHEAD of the live thread count for a moment:
        the sync path reserves its slot, then persists the record and creates the
        job before the worker thread starts. So a discrepancy only counts as a
        leak once it has persisted for SLOT_LEAK_GRACE_S; anything shorter is
        ordinary scheduling and is left alone.

        Healing never invents work: it can only lower `running` to the number of
        threads actually executing, and it never touches the admission queue.
        """
        live = self.live_job_threads()
        now = time.monotonic()
        healed = 0
        with self._lock:
            if self.running <= live:
                self._leak_since = None
            else:
                if self._leak_since is None:
                    self._leak_since = now
                elif now - self._leak_since >= SLOT_LEAK_GRACE_S:
                    healed = self.running - live
                    detail = ("healed %d leaked slot(s): running=%d live_job_threads=%d "
                              "stuck_for=%.0fs" % (healed, self.running, live,
                                                   now - self._leak_since))
                    self.running = live
                    self.slot_leaks_healed += healed
                    self.last_leak_healed_utc = utc_now()
                    self.last_leak_detail = detail
                    self._leak_since = None
                    self._lock.notify_all()
            state = {
                "jobs_running": self.running,
                "live_job_threads": live,
                "leak_grace_s": SLOT_LEAK_GRACE_S,
                "slot_leaks_healed": self.slot_leaks_healed,
                "last_leak_healed_utc": self.last_leak_healed_utc,
                "last_leak_detail": self.last_leak_detail,
                "leak_suspected": self._leak_since is not None,
            }
        if healed:
            log("slot leak %s" % state["last_leak_detail"])
        return state

    def create(self, job_id: str, record: dict) -> threading.Event:
        ev = threading.Event()
        with self._lock:
            self._jobs[job_id] = record
            self._events[job_id] = ev
            while len(self._jobs) > JOB_HISTORY:
                old_id, _ = self._jobs.popitem(last=False)
                self._events.pop(old_id, None)
        return ev

    def update(self, job_id: str, record: dict) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id] = record

    def get(self, job_id: str):
        with self._lock:
            rec = self._jobs.get(job_id)
            return dict(rec) if rec else None

    def event(self, job_id: str):
        with self._lock:
            return self._events.get(job_id)

    def annotate(self, job_id: str, **fields) -> None:
        """Merge extra fields into a stored job record (callback outcome)."""
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is not None:
                rec.update(fields)

    def finish(self, job_id: str) -> None:
        with self._lock:
            ev = self._events.get(job_id)
        if ev:
            ev.set()


STORE = JobStore()


# --------------------------------------------------------------------------
# Command building
# --------------------------------------------------------------------------


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def build_script_command(payload: dict, actions: dict):
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise BadRequest("script action requires string 'name'")
    spec = actions.get(name)
    if spec is None:
        raise BadRequest("unknown action name: %s" % name, status=404)

    argv = list(spec["argv"])
    cwd = spec.get("cwd") or BASE_DIR
    timeout_s = int(spec.get("timeout_s", 300))
    cleanup: list[str] = []
    # {"queue-json": "queue-file"} means: when the value is too large to survive
    # argv, write it to a private temp file and pass --queue-file instead.
    file_variants = spec.get("arg_file_variants") or {}
    # {"mode": {"sweep": ["--sweep-only"], "tier2": ["--tier2-only"], "full": []}}
    # means: an enum-style arg selects a FIXED, pre-baked flag list instead of
    # being passed through as "--<key> <value>". This exists because some
    # target scripts (e.g. ops/error-fixer/fixer_runtime.py) use a strict
    # argparse.parse_args() that has no "--mode" flag at all -- passing one
    # through literally would crash the script with "unrecognized arguments"
    # before it printed a single line. value_flags lets one bridge action
    # name (e.g. the collapsed `error_fixer`) stand in for what used to be
    # several static-argv alias actions, with the value still validated
    # against an explicit allowlist (2026-09-01, fold 2-3 consolidation).
    value_flags = spec.get("value_flags") or {}

    args = payload.get("args") or {}
    if args:
        if not isinstance(args, dict):
            raise BadRequest("'args' must be an object of string->string")
        allowed = spec.get("allowed_args")
        if not allowed:
            raise BadRequest("action '%s' does not accept args" % name)
        allowed_set = set(allowed)
        for key in sorted(args.keys()):
            val = args[key]
            if not isinstance(key, str) or not isinstance(val, str):
                raise BadRequest("'args' must be an object of string->string")
            if key not in allowed_set:
                raise BadRequest(
                    "arg '%s' not allowed for action '%s'" % (key, name)
                )
            if not ARG_KEY_RE.match(key):
                raise BadRequest("invalid arg key: %s" % key)

            if key in value_flags:
                flag_map = value_flags[key]
                if val not in flag_map:
                    raise BadRequest(
                        "invalid value '%s' for arg '%s' in action '%s' "
                        "(allowed: %s)"
                        % (val, key, name, ", ".join(sorted(flag_map)))
                    )
                argv.extend(flag_map[val])
                continue

            size = len(val.encode("utf-8"))
            if size > ARG_SPILL_THRESHOLD:
                variant = file_variants.get(key)
                if not variant:
                    # Fail here, loudly and by name. Passing it through would
                    # kill execve with an errno the caller cannot interpret.
                    raise BadRequest(
                        "arg '--%s' for action '%s' is %d bytes, over the %d-byte "
                        "argv spill threshold (kernel MAX_ARG_STRLEN is %d), and the "
                        "action declares no 'arg_file_variants' entry for it. Passing "
                        "it would fail execve with E2BIG before the program starts."
                        % (key, name, size, ARG_SPILL_THRESHOLD, MAX_ARG_STRLEN)
                    )
                if variant not in allowed_set:
                    raise BadRequest(
                        "action '%s' maps '--%s' to '--%s', which is not in its "
                        "allowed_args" % (name, key, variant)
                    )
                path = _write_spill_file(val, key)
                cleanup.append(path)
                log("spilled arg --%s (%d bytes) to %s for action %s"
                    % (key, size, path, name))
                argv.append("--%s" % variant)
                argv.append(path)
                continue

            argv.append("--%s" % key)
            argv.append(val)

    return {
        "argv": argv,
        "cwd": cwd,
        "timeout_s": timeout_s,
        "name": name,
        "cleanup": cleanup,
        "entry_authority_scope": spec.get("entry_authority_scope"),
    }


def _write_spill_file(value: str, key: str) -> str:
    """Park an oversized arg value on disk so execve never sees it.

    Same shape as _write_prompt_file: 0600, and the caller registers the path in
    `cleanup` so it is removed when the job finishes, including on failure.
    """
    safe = re.sub(r"[^A-Za-z0-9]+", "-", key).strip("-") or "arg"
    fd, path = tempfile.mkstemp(prefix="n8n-bridge-arg-%s-" % safe, suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(value)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def _write_prompt_file(prompt: str) -> str:
    fd, path = tempfile.mkstemp(prefix="n8n-bridge-prompt-", suffix=".txt")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(prompt)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path


def build_hermes_command(payload: dict):
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BadRequest("hermes_chat requires non-empty string 'prompt'")

    model = payload.get("model", "gpt-5.6-sol")
    if model not in HERMES_MODELS:
        raise BadRequest("unsupported model: %s" % model)

    reasoning = payload.get("reasoning", "medium")
    if reasoning not in HERMES_REASONING:
        raise BadRequest("unsupported reasoning: %s" % reasoning)

    timeout_s = int(payload.get("timeout_s", DEFAULT_HERMES_TIMEOUT))
    if timeout_s <= 0 or timeout_s > HERMES_TIMEOUT_MAX:
        raise BadRequest("timeout_s must be 1..%d" % HERMES_TIMEOUT_MAX)

    max_turns = int(payload.get("max_turns", 1))
    # The transport ceiling admits the 90-turn deep-fix contract. Ordinary
    # callers retain their lower defaults; turns remain a bounded runaway stop.
    if max_turns <= 0 or max_turns > HERMES_MAX_TURNS:
        raise BadRequest("max_turns must be 1..%d" % HERMES_MAX_TURNS)

    toolset_profile = payload.get("toolset_profile", "none")
    if toolset_profile not in HERMES_TOOLSET_PROFILES:
        raise BadRequest("unsupported toolset_profile: %s" % toolset_profile)
    toolsets = HERMES_TOOLSET_PROFILES[toolset_profile]

    prompt_path = _write_prompt_file(prompt)
    argv = [
        "hermes", "chat",
        "--quiet",
        "--cli",
        "--source", "subagent",
        "--provider", "openai-codex",
        "--model", model,
        "--reasoning", reasoning,
        "--max-turns", str(max_turns),
        # The default remains exactly empty. Authoring receives one narrowly
        # named profile; callers cannot submit arbitrary toolset strings.
        "--toolsets", toolsets,
        "--query-file", prompt_path,
    ]
    return {
        "argv": argv,
        "cwd": "/home/ubuntu/business",
        "timeout_s": timeout_s,
        "name": model,
        "cleanup": [prompt_path],
    }


def build_claude_command(payload: dict):
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise BadRequest("claude_p requires non-empty string 'prompt'")

    timeout_s = int(payload.get("timeout_s", DEFAULT_CLAUDE_TIMEOUT))
    if timeout_s <= 0 or timeout_s > CLAUDE_TIMEOUT_MAX:
        raise BadRequest("timeout_s must be 1..%d" % CLAUDE_TIMEOUT_MAX)

    argv = ["claude", "-p", prompt, "--output-format", "json"]
    model = payload.get("model")
    if model is not None:
        if not isinstance(model, str) or not re.match(r"^[A-Za-z0-9._-]{1,64}$", model):
            raise BadRequest("invalid model")
        argv += ["--model", model]

    return {
        "argv": argv,
        "cwd": "/home/ubuntu/business",
        "timeout_s": timeout_s,
        "name": "claude_p",
        "cleanup": [],
    }


def build_command(payload: dict, actions: dict):
    action = payload.get("action")
    if action == "script":
        return build_script_command(payload, actions)
    if action == "hermes_chat":
        return build_hermes_command(payload)
    if action == "claude_p":
        return build_claude_command(payload)
    raise BadRequest("unknown action: %r" % (action,), status=404)


def authorize_scope(payload: dict, actions: dict, auth_scope: str) -> None:
    """Make bearer scope an exact action boundary, not a caller label."""
    if payload.get("action") != "script":
        if auth_scope != GLOBAL_AUTH_SCOPE:
            raise BadRequest("forbidden", status=403)
        return
    name = payload.get("name")
    spec = actions.get(name) if isinstance(name, str) else None
    required = str((spec or {}).get("required_auth_scope") or "")
    if required:
        if auth_scope != required:
            raise BadRequest("forbidden", status=403)
        return
    if auth_scope != GLOBAL_AUTH_SCOPE:
        raise BadRequest("forbidden", status=403)


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _drain(stream, sink: list, cap: int) -> None:
    total = 0
    try:
        for chunk in iter(lambda: stream.read(65536), b""):
            sink.append(chunk)
            total += len(chunk)
            if total > cap * 2:
                # keep memory bounded: collapse to the trailing cap bytes
                joined = b"".join(sink)[-cap:]
                sink[:] = [joined]
                total = len(joined)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


@contextmanager
def source_reader_admission(cmd: dict):
    """Defer affected launches while the installer holds EX, without dropping jobs.

    Acquire before exec and hold until the child exits; admitted callback jobs
    retain their FIFO ticket and ordinary jobs retain their durable job ID.
    Unrelated bridge actions and receiver HTTP endpoints remain available.
    """
    if cmd.get("name") not in SOURCE_READER_ACTIONS:
        yield
        return
    fd = os.open(SOURCE_ADMISSION_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield
    finally:
        os.close(fd)

def acquire_source_reader(cmd: dict):
    """Acquire before reserving a slot; the worker owns the returned fd."""
    if cmd.get("name") not in SOURCE_READER_ACTIONS:
        return None
    fd = os.open(SOURCE_ADMISSION_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
    except BaseException:
        os.close(fd)
        raise
    return fd


def run_command(cmd: dict, job_id: str = "", source_admitted: bool = False) -> dict:
    if source_admitted:
        return _run_command_unlocked(cmd, job_id)
    with source_reader_admission(cmd):
        return _run_command_unlocked(cmd, job_id)


def _run_command_unlocked(cmd: dict, job_id: str = "") -> dict:
    argv = list(cmd["argv"])
    started = time.time()
    out_chunks: list = []
    err_chunks: list = []
    timed_out = False
    authority_read_fd = None

    try:
        pass_fds: tuple[int, ...] = ()
        if cmd.get("entry_authority_scope"):
            admission = mint_entry_authority(cmd)
            authority_read_fd, authority_write_fd = os.pipe2(os.O_CLOEXEC)
            try:
                os.set_inheritable(authority_read_fd, True)
                os.write(authority_write_fd, _canonical_bytes(admission))
            finally:
                os.close(authority_write_fd)
            argv.extend(["--entry-authority-fd", str(authority_read_fd)])
            pass_fds = (authority_read_fd,)
        proc = subprocess.Popen(
            argv,
            cwd=cmd["cwd"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=child_env(job_id),
            start_new_session=True,  # own process group -> killable as a unit
            close_fds=True,
            pass_fds=pass_fds,
        )
    except FileNotFoundError as exc:
        return {
            "rc": 127,
            "stdout": b"",
            "stderr": str(exc).encode(),
            "timed_out": False,
            "duration_s": round(time.time() - started, 3),
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "rc": 126,
            "stdout": b"",
            "stderr": ("failed to launch: %s" % exc).encode(),
            "timed_out": False,
            "duration_s": round(time.time() - started, 3),
        }
    finally:
        if authority_read_fd is not None:
            try:
                os.close(authority_read_fd)
            except OSError:
                pass

    started_drains = []
    t_out = t_err = None
    try:
        t_out = threading.Thread(
            target=_drain, args=(proc.stdout, out_chunks, CAP_CAPTURE), daemon=True
        )
        t_err = threading.Thread(
            target=_drain, args=(proc.stderr, err_chunks, CAP_CAPTURE), daemon=True
        )
        t_out.start()
        started_drains.append(t_out)
        t_err.start()
        started_drains.append(t_err)
        try:
            rc = proc.wait(timeout=cmd["timeout_s"])
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                rc = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                rc = proc.wait()
        t_out.join()
        t_err.join()
    except BaseException:
        # An unexpected post-Popen error must not release SH or the slot while
        # a child still has effects in flight. Do not terminate it for a bridge
        # bookkeeping failure; drain unowned pipes so it can exit naturally.
        # A single sequential read can deadlock if both pipes fill. Select
        # over precisely the streams without an already-running drain thread.
        assert proc.stdout is not None and proc.stderr is not None
        with selectors.DefaultSelector() as pending:
            if t_out is None or t_out not in started_drains:
                pending.register(proc.stdout.fileno(), selectors.EVENT_READ, proc.stdout)
            if t_err is None or t_err not in started_drains:
                pending.register(proc.stderr.fileno(), selectors.EVENT_READ, proc.stderr)
            while pending.get_map():
                for key, _ in pending.select():
                    if not os.read(key.fd, 65536):
                        pending.unregister(key.fd)
                        key.data.close()
        if proc.returncode is None:
            try:
                proc.wait()
            except Exception:
                # Even an unexpectedly broken Popen.wait must not end the hold.
                os.waitpid(proc.pid, 0)
        for drain in started_drains:
            drain.join()
        raise

    stdout = b"".join(out_chunks)[-CAP_CAPTURE:]
    stderr = b"".join(err_chunks)[-CAP_CAPTURE:]
    if timed_out:
        stderr += b"\n[bridge] killed after timeout of %ds\n" % cmd["timeout_s"]
        if rc == 0:
            rc = -1

    return {
        "rc": rc,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "duration_s": round(time.time() - started, 3),
    }


def classify_termination(rc, timed_out=False) -> dict:
    """Classify signal exits so ``rc=-15`` is KILLED, not an engine defect."""
    if timed_out:
        return {"classification": "TIMED_OUT", "signal_number": None,
                "signal_name": None}
    if isinstance(rc, int) and rc < 0:
        signum = -rc
        try:
            signal_name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            signal_name = "SIGNAL_%s" % signum
        return {"classification": "KILLED", "signal_number": signum,
                "signal_name": signal_name}
    return {"classification": "EXITED", "signal_number": None,
            "signal_name": None}


def write_receipt(result: dict) -> str:
    with PERSIST_LOCK:
        path = os.path.join(
            RECEIPT_DIR, "%s-%s.json" % (utc_stamp(), result["job_id"])
        )
        result["receipt_path"] = path
        _atomic_json_write(path, result)
    return path


def post_callback(url: str, result: dict) -> None:
    """Fire the completion callback. n8n resume URLs are capability URLs, so no
    auth header is attached -- and the bridge token is never sent outbound.

    Delivery is deliberately patient: a grace pause first (the caller may still
    be registering its listener -- see CALLBACK_GRACE_S) and then a backoff
    retry, because a dropped callback strands the caller until *its* timeout
    and reads as a failure of a job that actually succeeded.
    """
    job_id = result.get("job_id")
    host = urllib.parse.urlsplit(url).hostname
    body = json.dumps(result).encode("utf-8")

    if CALLBACK_GRACE_S > 0:
        time.sleep(CALLBACK_GRACE_S)

    attempts = 0
    for idx in range(len(CALLBACK_RETRY_DELAYS) + 1):
        attempts += 1
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=CALLBACK_TIMEOUT_S) as resp:
                log("callback job=%s host=%s status=%s attempt=%d"
                    % (job_id, host, resp.status, attempts))
            persist_job_annotations(job_id, callback_status="delivered",
                                    callback_attempts=attempts)
            return
        except Exception as exc:  # noqa: BLE001
            log("callback attempt %d/%d FAILED job=%s host=%s err=%s"
                % (attempts, len(CALLBACK_RETRY_DELAYS) + 1, job_id, host, exc))
            if idx < len(CALLBACK_RETRY_DELAYS):
                time.sleep(CALLBACK_RETRY_DELAYS[idx])

    log("callback GAVE UP job=%s host=%s after %d attempts"
        % (job_id, host, attempts))
    persist_job_annotations(job_id, callback_status="failed",
                            callback_attempts=attempts)


def execute_job(job_id: str, action: str, cmd: dict, callback_url, source_fd=None) -> None:
    """Run one job and ALWAYS give the concurrency slot back.

    DEFECT 2026-09-03: the release lived on the success path, so any exception
    between reserving the slot and reaching it pinned that slot for the life of
    the process. jobs_running never returned to 0 again, and every caller whose
    idle gate reads jobs_running==0 waited forever on a bridge that was in fact
    completely idle. The release belongs in a finally, and nowhere else.
    """
    try:
        _execute_job_body(job_id, action, cmd, callback_url, source_fd is not None)
    finally:
        try:
            if source_fd is not None:
                os.close(source_fd)
        finally:
            STORE.release()

def _execute_job_body(job_id: str, action: str, cmd: dict, callback_url,
                      source_admitted: bool = False,
                      admission_error: Exception | None = None) -> None:
    started_utc = utc_now()
    record = STORE.get(job_id) or {}
    record.update({"status": "running", "started_utc": started_utc})
    STORE.update(job_id, record)
    try:
        persist_job_record(record)
    except Exception as exc:  # noqa: BLE001
        log("running state persistence FAILED job=%s err=%s" % (job_id, exc))

    try:
        if admission_error is not None:
            raise admission_error
        raw = run_command(cmd, job_id=job_id, source_admitted=source_admitted)
        finished_utc = utc_now()
        stdout_bytes = len(raw["stdout"])
        stderr_bytes = len(raw["stderr"])
        stdout_truncated = stdout_bytes > CAP_STDOUT_TAIL
        raw_stderr_tail = tail_bytes(raw["stderr"], CAP_STDERR_TAIL)
        promoted = None
        if raw["rc"] != 0 and not raw["stderr"].strip():
            promoted = structured_failure_diagnostic(raw["stdout"], raw["rc"])
        result = {
            "job_id": job_id,
            "action": action,
            "name": cmd.get("name"),
            "rc": raw["rc"],
            "stdout_tail": tail_bytes(raw["stdout"], CAP_STDOUT_TAIL),
            "stderr_tail": promoted["text"] if promoted else raw_stderr_tail,
            "raw_stderr_tail": raw_stderr_tail,
            "stderr_bytes": stderr_bytes,
            "stderr_promoted_from_stdout": bool(promoted),
            "failure_diagnostic": promoted["text"] if promoted else "",
            "failure_diagnostic_source": promoted["source"] if promoted else "",
            # A consumer that JSON-parses stdout_tail MUST check this first: a
            # truncated tail begins mid-document and is not a receipt.
            "stdout_truncated": stdout_truncated,
            "stdout_bytes": stdout_bytes,
            "stdout_file": None,
            "started_utc": started_utc,
            "finished_utc": finished_utc,
            "duration_s": raw["duration_s"],
            "timed_out": raw["timed_out"],
            "termination": classify_termination(raw["rc"], raw["timed_out"]),
            "status": "done",
        }
        if stdout_truncated:
            try:
                result["stdout_file"] = spill_stdout(job_id, raw["stdout"])
                log("stdout truncated for job=%s name=%s (%d bytes > %d cap); "
                    "full output at %s"
                    % (job_id, cmd.get("name"), stdout_bytes, CAP_STDOUT_TAIL,
                       result["stdout_file"]))
            except Exception as exc:  # noqa: BLE001
                # Still say it was truncated. Losing the file is recoverable;
                # a consumer silently parsing a fragment is not.
                log("stdout spill FAILED for job=%s: %s" % (job_id, exc))
    except Exception as exc:  # noqa: BLE001
        result = {
            "job_id": job_id,
            "action": action,
            "name": cmd.get("name"),
            "rc": 125,
            "stdout_tail": "",
            "stdout_truncated": False,
            "stdout_bytes": 0,
            "stdout_file": None,
            "stderr_tail": "bridge internal error: %s\n%s"
            % (exc, traceback.format_exc()),
            "raw_stderr_tail": "",
            "stderr_bytes": 0,
            "stderr_promoted_from_stdout": False,
            "failure_diagnostic": "",
            "failure_diagnostic_source": "",
            "started_utc": started_utc,
            "finished_utc": utc_now(),
            "duration_s": 0.0,
            "timed_out": False,
            "status": "error",
        }
    finally:
        for path in cmd.get("cleanup", []):
            try:
                os.unlink(path)
            except OSError:
                pass

    # Scope is stamped at admission, never taken from caller payload or the
    # mutable action allowlist. Preserve it in successful and error receipts.
    result["auth_scope"] = record.get("auth_scope")
    try:
        write_receipt(result)
    except Exception as exc:  # noqa: BLE001
        result["receipt_path"] = None
        log("receipt write FAILED job=%s err=%s" % (job_id, exc))

    STORE.update(job_id, result)
    try:
        persist_job_record(result)
    except Exception as exc:  # noqa: BLE001
        log("terminal state persistence FAILED job=%s err=%s" % (job_id, exc))
    # The slot is released by execute_job's finally, on every path.
    STORE.finish(job_id)
    log("job done id=%s action=%s name=%s rc=%s dur=%ss"
        % (job_id, action, cmd.get("name"), result["rc"], result["duration_s"]))

    if callback_url:
        threading.Thread(
            target=post_callback, args=(callback_url, result), daemon=True
        ).start()


def execute_queued_job(
    job_id: str, action: str, cmd: dict, callback_url: str, ticket: int
) -> None:
    """Admit one accepted callback job in FIFO order, then execute normally."""
    # The fence precedes the slot. Ineligible tickets remain bounded and durable
    # but cannot starve an unrelated job; eligible tickets retain FIFO order.
    try:
        source_fd = acquire_source_reader(cmd)
    except Exception as exc:
        # The ID was already accepted. A failed lock open still gets a terminal
        # receipt, without taking or releasing another job's execution slot.
        STORE.cancel_queued(ticket)
        _execute_job_body(job_id, action, cmd, callback_url, admission_error=exc)
        return
    try:
        STORE.claim_queued(ticket)
    except BaseException:
        if source_fd is not None:
            os.close(source_fd)
        raise
    # From here the slot IS held. execute_job's finally gives it back, but the
    # admission bookkeeping below runs first, so it needs its own guard.
    try:
        record = STORE.get(job_id) or {}
        record["status"] = "accepted"
        STORE.update(job_id, record)
        try:
            persist_job_record(record)
        except Exception as exc:  # noqa: BLE001
            log("admitted state persistence FAILED job=%s err=%s" % (job_id, exc))
        log("job admitted id=%s action=%s name=%s queued=%d running=%d"
            % (job_id, action, cmd.get("name"), STORE.queued_count(),
               STORE.running_count()))
    except BaseException:
        try:
            if source_fd is not None:
                os.close(source_fd)
        finally:
            STORE.release()
        raise
    execute_job(job_id, action, cmd, callback_url, source_fd)


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------


def host_disk_observation():
    # Existing authenticated health path; no new sender or action admission.
    try:
        scripts = os.path.abspath(os.path.join(BASE_DIR, "..", "..", "scripts"))
        if scripts not in sys.path:
            sys.path.insert(0, scripts)
        from host_capacity_observer import root_disk
        return root_disk()
    except (ImportError, OSError, ValueError):
        return {"status": "UNKNOWN", "used_pct": None, "new_copy_allowed": False}


class Handler(BaseHTTPRequestHandler):
    server_version = "n8n-bridge/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: A003
        # Default logging would echo the request line only; keep it but route
        # through our logger. Never includes headers, so the token is safe.
        log("http %s %s" % (self.address_string(), fmt % args))

    def _send(self, status: int, payload: dict, headers: dict = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, str(value))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _auth_scope(self, *, allow_scoped: bool) -> str:
        header = self.headers.get("Authorization", "")
        supplied = header.strip()
        if hmac.compare_digest(supplied, "Bearer " + TOKEN):
            return GLOBAL_AUTH_SCOPE
        matched = ""
        for scope, token in SCOPED_TOKENS.items():
            if token and hmac.compare_digest(supplied, "Bearer " + token):
                matched = scope
        if matched and allow_scoped:
            return matched
        self._send(403 if matched else 401, {"error": "forbidden" if matched else "unauthorized"})
        return ""

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise BadRequest("bad Content-Length")
        if length < 0 or length > MAX_BODY:
            raise BadRequest("body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError) as exc:
            raise BadRequest("invalid JSON: %s" % exc)
        if not isinstance(data, dict):
            raise BadRequest("body must be a JSON object")
        return data

    # -- routes -----------------------------------------------------------
    def do_GET(self):  # noqa: N802
        auth_scope = self._auth_scope(allow_scoped=True)
        if not auth_scope:
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if auth_scope != GLOBAL_AUTH_SCOPE and not path.startswith("/job/"):
            self._send(403, {"error": "forbidden"})
            return
        if path == "/health":
            # Reconcile BEFORE reporting: /health is what every idle gate reads,
            # so a leaked slot has to self-heal here or it stalls callers for the
            # life of the process. The reconciliation is also REPORTED, so a heal
            # is visible evidence rather than a number that quietly changed.
            slots = STORE.reconcile()
            self._send(200, {
                "status": "ok",
                "uptime_s": round(time.time() - START_TIME, 1),
                "jobs_running": slots["jobs_running"],
                "jobs_queued": STORE.queued_count(),
                "slot_reconciliation": slots,
                "host_disk": host_disk_observation(),
            })
            return
        if path.startswith("/job/"):
            job_id = path[len("/job/"):]
            rec = STORE.get(job_id)
            if rec is None:
                rec = load_durable_job(job_id)
            if auth_scope != GLOBAL_AUTH_SCOPE and (
                rec is None or rec.get("auth_scope") != auth_scope
            ):
                self._send(403, {"error": "forbidden"})
                return
            if rec is None:
                self._send(404, {"error": "unknown job_id"})
                return
            self._send(200, rec)
            return
        if path == "/actions":
            self._send(200, {
                "actions": {
                    k: {
                        "description": v.get("description", ""),
                        "timeout_s": v.get("timeout_s"),
                        "allowed_args": v.get("allowed_args", []),
                    }
                    for k, v in ACTIONS.items()
                }
            })
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        auth_scope = self._auth_scope(allow_scoped=True)
        if not auth_scope:
            return
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != "/run":
            self._send(404, {"error": "not found"})
            return

        try:
            payload = self._read_json()
            callback_url = payload.get("callback_url")
            if callback_url is not None:
                if not isinstance(callback_url, str) or not callback_url.startswith(
                    ("http://", "https://")
                ):
                    raise BadRequest("callback_url must be an http(s) URL")
            authorize_scope(payload, ACTIONS, auth_scope)
            cmd = build_command(payload, ACTIONS)
        except BadRequest as exc:
            # Body/args can contain provider identities or temporary file paths.
            # Log only the bounded route selectors and fail-closed reason so a
            # registered workflow 4xx is diagnosable without exposing payloads.
            log(
                "request rejected status=%s action=%r name=%r error=%s"
                % (
                    exc.status,
                    payload.get("action") if "payload" in locals() else None,
                    payload.get("name") if "payload" in locals() else None,
                    exc.message,
                )
            )
            self._send(exc.status, {"error": exc.message})
            return

        action = payload["action"]

        # Callback callers already have a durable continuation (for n8n, a Wait
        # node at `$execution.resumeUrl`). Accept those jobs into a bounded FIFO
        # instead of turning an ordinary overlap with a long-running child into
        # five identical HTTP 429 failures. The execution cap remains unchanged:
        # a queued job consumes a slot only when `execute_queued_job` admits it.
        if callback_url:
            ticket = STORE.enqueue(eligible=cmd.get("name") not in SOURCE_READER_ACTIONS)
            if ticket is None:
                for p in cmd.get("cleanup", []):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
                log("job REJECTED queue_full action=%s name=%s queued=%d max_queued=%d"
                    % (action, cmd.get("name"), STORE.queued_count(),
                       MAX_QUEUED_JOBS))
                self._send(
                    429,
                    {
                        "error": "busy",
                        "busy": True,
                        "retry_after_s": RETRY_AFTER_S,
                        "running": STORE.running_count(),
                        "max_jobs": MAX_CONCURRENT_JOBS,
                        "queued": STORE.queued_count(),
                        "max_queued": MAX_QUEUED_JOBS,
                    },
                    headers={"Retry-After": RETRY_AFTER_S},
                )
                return

            job_id = uuid.uuid4().hex[:16]
            record = {
                "job_id": job_id,
                "action": action,
                "name": cmd.get("name"),
                "status": "queued",
                "auth_scope": auth_scope,
                "queued_utc": utc_now(),
                "started_utc": None,
                "finished_utc": None,
            }
            try:
                persist_job_record(record)
            except Exception as exc:  # noqa: BLE001
                STORE.cancel_queued(ticket)
                for p in cmd.get("cleanup", []):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
                log("job REJECTED persistence_failed action=%s name=%s err=%s"
                    % (action, cmd.get("name"), exc))
                self._send(500, {"error": "job admission persistence failed"})
                return
            STORE.create(job_id, record)
            log("job queued id=%s action=%s name=%s queued=%d"
                % (job_id, action, cmd.get("name"), STORE.queued_count()))
            threading.Thread(
                target=execute_queued_job,
                args=(job_id, action, cmd, callback_url, ticket),
                daemon=True,
                name=JOB_THREAD_PREFIX + job_id,
            ).start()
            self._send(202, {
                "job_id": job_id,
                "status": "accepted",
                "queued": True,
            })
            return

        # A synchronous reader waits for SH *before* reserving execution
        # capacity; its fd is transferred to the worker through completion.
        source_fd = acquire_source_reader(cmd)
        admit_started = time.monotonic()
        if not STORE.try_reserve(ADMIT_WAIT_S):
            if source_fd is not None:
                os.close(source_fd)
            for p in cmd.get("cleanup", []):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            waited = round(time.monotonic() - admit_started, 1)
            # An honest, machine-readable "come back later". `busy: true` is the
            # unambiguous flag a caller branches on -- the bare `error` string is
            # kept for the existing clients that already match on it.
            log("job REJECTED busy action=%s name=%s waited=%ss running=%d"
                % (action, cmd.get("name"), waited, STORE.running_count()))
            self._send(
                429,
                {
                    "error": "busy",
                    "busy": True,
                    "retry_after_s": RETRY_AFTER_S,
                    "running": STORE.running_count(),
                    "max_jobs": MAX_CONCURRENT_JOBS,
                    "waited_s": waited,
                },
                headers={"Retry-After": RETRY_AFTER_S},
            )
            return

        job_id = uuid.uuid4().hex[:16]
        record = {
            "job_id": job_id,
            "action": action,
            "name": cmd.get("name"),
            "status": "accepted",
            "auth_scope": auth_scope,
            "started_utc": None,
            "finished_utc": None,
        }
        try:
            persist_job_record(record)
        except Exception as exc:  # noqa: BLE001
            STORE.release()
            if source_fd is not None:
                os.close(source_fd)
            for p in cmd.get("cleanup", []):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            log("job REJECTED persistence_failed action=%s name=%s err=%s"
                % (action, cmd.get("name"), exc))
            self._send(500, {"error": "job admission persistence failed"})
            return
        event = STORE.create(job_id, record)
        log("job accepted id=%s action=%s name=%s async=%s"
            % (job_id, action, cmd.get("name"), bool(callback_url)))

        worker = threading.Thread(
            target=execute_job,
            args=(job_id, action, cmd, callback_url, source_fd),
            daemon=True,
            name=JOB_THREAD_PREFIX + job_id,
        )
        worker.start()

        if event.wait(timeout=SYNC_WAIT_S):
            rec = STORE.get(job_id) or {"job_id": job_id, "status": "unknown"}
            self._send(200, rec)
        else:
            self._send(202, {
                "job_id": job_id,
                "status": "running",
                "note": "exceeded %ds sync window; poll GET /job/%s"
                        % (int(SYNC_WAIT_S), job_id),
            })


def main() -> int:
    global TOKEN, SCOPED_TOKENS, ACTIONS
    TOKEN = load_token()
    SCOPED_TOKENS = load_scoped_tokens()
    ACTIONS = load_actions()
    log("loaded %d allowlisted actions and %d scoped credentials from %s"
        % (len(ACTIONS), len(SCOPED_TOKENS), ACTIONS_PATH))
    os.makedirs(RECEIPT_DIR, mode=0o700, exist_ok=True)
    os.makedirs(JOB_STATE_DIR, mode=0o700, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    log("n8n-bridge listening on http://%s:%d (max_jobs=%d max_queued=%d "
        "admit_wait=%.0fs retry_after=%ds)"
        % (HOST, PORT, MAX_CONCURRENT_JOBS, MAX_QUEUED_JOBS, ADMIT_WAIT_S,
           RETRY_AFTER_S))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


TOKEN = ""
SCOPED_TOKENS: dict[str, str] = {}
ACTIONS: dict = {}

if __name__ == "__main__":
    sys.exit(main())
