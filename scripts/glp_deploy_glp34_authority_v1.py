#!/usr/bin/env python3
"""Authority-bound one-time GLP34 carrier (r1 / v1).

Legacy modes are ``--verify-only`` and ``--live``.  The separate
``--package-only`` mode packages explicitly hash-bound public bytes without
release authority, signatures, or provider writes. ``--prepare-current`` adds
the exact gate-bound Functions runtime offline; ``--publish-current`` consumes
only that prepared closure after explicit independent and operator approval.  Verify-only runs
all immutable local gates, the actual pinned completeness sensor, a direct
Wrangler 4.114.0 Pages Functions build, a direct Worker dry-run, and an atomic
CAS canary.  It performs no provider call and never touches the production
content-base pointer.  Live mode uses the same gates before its first provider
read or write, permits one Pages production deployment (and a fail-safe Pages
rollback only), and advances the content-base pointer with a same-directory,
fsync-backed atomic compare-and-swap while the weekly lock is held.
"""
from __future__ import annotations

import argparse
import base64
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

BUSINESS = Path("/home/ubuntu/business")
CANDIDATE = BUSINESS / "site-system/flagship-v2/candidates/glp34-provider-families-20260912"
RELEASE = BUSINESS / "site-system/flagship-v2/releases/glp34-provider-families-20260918-r1"
TOOLCHAIN = BUSINESS / "site-system/toolchains/wrangler-4.114.0-r13"
NODE = TOOLCHAIN / "runtime/node"
WRANGLER = TOOLCHAIN / "node_modules/wrangler/bin/wrangler.js"
ESBUILD = TOOLCHAIN / "node_modules/@esbuild/linux-x64/bin/esbuild"
WRANGLER_WORKSPACE = Path("/tmp/glp34-r1-wrangler-workspace")
AUTHORITY_DIR = BUSINESS / "reports/glp-clinic-scout/growth/glp34/production-authority-20260918-r1"
AUTHORITY = AUTHORITY_DIR / "DEPLOY-AUTHORITY-v3.json"
VERIFY_RUNS = AUTHORITY_DIR / "verify-runs"
LIVE_RUNS = AUTHORITY_DIR / "live-runs"
ONE_TIME_STATE = AUTHORITY_DIR / "ONE-TIME-STATE-v3.json"
CARRIER = BUSINESS / "scripts/glp_deploy_glp34_authority_v1.py"
TEST_FILE = BUSINESS / "tests/test_glp_deploy_glp34_authority_v1.py"
SENSOR = AUTHORITY_DIR / "glp_deploy_completeness_sensor.py"
SURFACE_AUDIT = AUTHORITY_DIR / "affiliate_surface_coverage_audit.py"
PYTHON = Path("/usr/bin/python3.12")
OPENSSL = Path("/usr/bin/openssl")
POINTER = BUSINESS / "reports/glp-clinic-scout/seo-machine/current-content-base.json"
SEO_REPORT_ROOT = POINTER.parent
LOCK_PATH = Path("/home/ubuntu/.hermes/locks/glp-price-index-weekly.lock")
ENV_FILE = Path("/home/ubuntu/.hermes/.env")
ACCOUNT_ID = "f860943dff399ffb1db0a28dcd3dadfd"
PROJECT = "glp-clinic-scout"
PRODUCTION_BRANCH = "main"
API_BASE = "https://api.cloudflare.com/client/v4"
SITE = "https://glpclinicscout.com"
OPENSSL_SHA256 = "b86b739329008369aebe1f7cff6c2adb18965609d68a19456fca55232f2908f5"
PYTHON_SHA256 = "1643dacd9feaedc58f3cc581e4d22577dfe25c09b10282936186ccf0f2e61118"
PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAoOuzZrgDEJ32xvZ2qM+b6Pun7W4Ku+L8g/5ruH/mi8Q=
-----END PUBLIC KEY-----
"""
EXPECTED_SENSOR_PASS_CHECKS = [
    "coverage", "surface_completeness", "count_copy", "derived_tallies",
    "research_binding", "freshness_stamps", "page_fact_consistency",
    "identity_agreement", "identity_dedup", "link_graph_floor", "score_binding",
]
EXPECTED_LIVE_SENSOR_PASS_CHECKS = [*EXPECTED_SENSOR_PASS_CHECKS, "base_chain"]
EXPECTED_ZERO_EFFECTS = {
    "provider_mutations": 0, "deploys": 0, "sheet_writes": 0,
    "tracking_clicks": 0, "sends": 0,
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DEPLOYMENT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_CAS_FAULT_HOOK = None


class GateError(RuntimeError):
    """Fail-closed carrier gate."""


def fail(code: str) -> None:
    raise GateError(code)


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha_file(path: Path) -> str:
    with path.open("rb") as handle:
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        return digest.hexdigest()


def _mode(st: os.stat_result) -> int:
    return stat.S_IMODE(st.st_mode)


def require_regular(path: Path, *, root_owned: bool | None = None,
                    non_writable: bool | None = None, mode: int | None = None,
                    nlink_one: bool = True) -> os.stat_result:
    try:
        st = path.lstat()
    except FileNotFoundError:
        fail(f"missing_regular:{path}")
    if not stat.S_ISREG(st.st_mode) or path.is_symlink():
        fail(f"not_plain_regular:{path}")
    if nlink_one and st.st_nlink != 1:
        fail(f"unexpected_link_count:{path}:{st.st_nlink}")
    if root_owned is True and (st.st_uid != 0 or st.st_gid != 0):
        fail(f"not_root_owned:{path}:{st.st_uid}:{st.st_gid}")
    if non_writable is True and _mode(st) & 0o222:
        fail(f"writable_immutable_file:{path}:{_mode(st):04o}")
    if mode is not None and _mode(st) != mode:
        fail(f"mode_mismatch:{path}:{_mode(st):04o}:{mode:04o}")
    return st


def _walk_plain(root: Path) -> tuple[list[Path], list[Path]]:
    try:
        root_st = root.lstat()
    except FileNotFoundError:
        fail(f"missing_tree:{root}")
    if not stat.S_ISDIR(root_st.st_mode) or root.is_symlink():
        fail(f"not_plain_directory:{root}")
    dirs: list[Path] = []
    files: list[Path] = []
    for current, names, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        names.sort(key=lambda item: item.encode())
        filenames.sort(key=lambda item: item.encode())
        for name in list(names):
            path = current_path / name
            st = path.lstat()
            if not stat.S_ISDIR(st.st_mode) or path.is_symlink():
                fail(f"tree_special_directory:{path}:{st.st_mode:o}")
            dirs.append(path)
        for name in filenames:
            path = current_path / name
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode) or path.is_symlink():
                fail(f"tree_special_file:{path}:{st.st_mode:o}")
            files.append(path)
    dirs.sort(key=lambda p: p.relative_to(root).as_posix().encode())
    files.sort(key=lambda p: p.relative_to(root).as_posix().encode())
    return dirs, files


def file_closure(root: Path) -> dict[str, Any]:
    _dirs, files = _walk_plain(root)
    digest = hashlib.sha256()
    total = 0
    for path in files:
        rel = path.relative_to(root).as_posix()
        size = path.stat().st_size
        item_hash = sha_file(path)
        digest.update(f"{rel}\0{size}\0{item_hash}\n".encode())
        total += size
    return {"tree_sha256": digest.hexdigest(), "file_count": len(files), "total_bytes": total}


def file_rows(root: Path) -> list[dict[str, Any]]:
    _dirs, files = _walk_plain(root)
    return [
        {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size,
         "sha256": sha_file(path)}
        for path in files
    ]


def toolchain_closure(root: Path) -> dict[str, Any]:
    dirs, files = _walk_plain(root)
    entries = [("d", path) for path in dirs] + [("f", path) for path in files]
    entries.sort(key=lambda item: item[1].relative_to(root).as_posix().encode())
    digest = hashlib.sha256()
    total = 0
    for kind, path in entries:
        st = path.lstat()
        rel = path.relative_to(root).as_posix()
        if kind == "d":
            size = 0
            item_hash = ""
        else:
            size = st.st_size
            item_hash = sha_file(path)
            total += size
        digest.update(f"{kind}\0{rel}\0{_mode(st):o}\0{size}\0{item_hash}\n".encode())
    return {
        "closure_sha256": digest.hexdigest(), "file_count": len(files),
        "directory_count": len(dirs), "total_bytes": total,
    }


def verify_immutable_tree(root: Path, expected: dict[str, Any], *, toolchain: bool = False) -> dict[str, Any]:
    root_st = root.lstat()
    if not stat.S_ISDIR(root_st.st_mode) or root.is_symlink():
        fail(f"immutable_root_invalid:{root}")
    dirs, files = _walk_plain(root)
    for path in [root, *dirs, *files]:
        st = path.lstat()
        if st.st_uid != 0 or st.st_gid != 0:
            fail(f"immutable_owner_drift:{path}:{st.st_uid}:{st.st_gid}")
        if _mode(st) & 0o222:
            fail(f"immutable_mode_drift:{path}:{_mode(st):04o}")
        if not toolchain:
            expected_mode = 0o555 if stat.S_ISDIR(st.st_mode) else 0o444
            if _mode(st) != expected_mode:
                fail(f"immutable_seal_mode_drift:{path}:{_mode(st):04o}")
            if stat.S_ISREG(st.st_mode) and st.st_nlink != 1:
                fail(f"immutable_hardlink_drift:{path}:{st.st_nlink}")
    got = toolchain_closure(root) if toolchain else file_closure(root)
    if got != expected:
        fail(f"immutable_closure_drift:{root}:{got}:{expected}")
    return got


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            fail("short_write")
        view = view[written:]


def exclusive_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, mode)
    try:
        _write_all(fd, data)
        os.fsync(fd)
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    fsync_dir(path.parent)


def atomic_file(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    exclusive_file(temp, data, mode)
    os.replace(temp, path)
    fsync_dir(path.parent)


def checkpoint(path: Path, value: Any) -> None:
    atomic_file(path, json_bytes(value), 0o600)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"json_invalid:{path}:{type(exc).__name__}")
    if not isinstance(value, dict):
        fail(f"json_not_object:{path}")
    return value


def verify_signature(payload: dict[str, Any], signature_b64: str) -> None:
    require_regular(OPENSSL, root_owned=True, mode=0o755)
    if sha_file(OPENSSL) != OPENSSL_SHA256:
        fail("openssl_bootstrap_hash_drift")
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except Exception:
        fail("authority_signature_base64_invalid")
    with tempfile.TemporaryDirectory(prefix="glp-r13-signature-") as temp:
        root = Path(temp)
        pub = root / "public.pem"
        message = root / "payload.bin"
        sig = root / "signature.bin"
        pub.write_text(PUBLIC_KEY_PEM, encoding="ascii")
        message.write_bytes(canonical(payload))
        sig.write_bytes(signature)
        result = subprocess.run(
            [str(OPENSSL), "pkeyutl", "-verify", "-pubin", "-inkey", str(pub),
             "-rawin", "-in", str(message), "-sigfile", str(sig)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=30, env={"PATH": "/usr/bin:/bin"},
        )
    if result.returncode != 0:
        fail("authority_signature_invalid")


def verify_file_spec(path: Path, spec: dict[str, Any], *, root_owned: bool = True,
                     nlink_one: bool = True, non_writable: bool | None = None) -> None:
    if spec.get("path") != str(path):
        fail(f"authority_path_mismatch:{path}")
    immutable = root_owned if non_writable is None else non_writable
    st = require_regular(path, root_owned=root_owned, non_writable=immutable,
                         nlink_one=nlink_one)
    if st.st_size != spec.get("bytes") or sha_file(path) != spec.get("sha256"):
        fail(f"authority_file_drift:{path}")


def capture_pointer(path: Path) -> tuple[bytes, os.stat_result]:
    parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    flags = os.O_RDONLY | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path.name, flags, dir_fd=parent_fd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                fail(f"pointer_not_single_regular:{path}")
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)
    return b"".join(chunks), st


def verify_signed_predecessor(spec: dict[str, Any]) -> tuple[bytes, os.stat_result]:
    if spec.get("path") != str(POINTER):
        fail("predecessor_pointer_path_invalid")
    try:
        expected = base64.b64decode(spec["bytes_base64"], validate=True)
    except Exception:
        fail("predecessor_bytes_encoding_invalid")
    data, st = capture_pointer(POINTER)
    exact = {
        "sha256": sha_bytes(data), "bytes": len(data), "st_dev": st.st_dev,
        "st_ino": st.st_ino, "uid": st.st_uid, "gid": st.st_gid,
        "mode": f"{_mode(st):04o}", "nlink": st.st_nlink,
    }
    for key, value in exact.items():
        if spec.get(key) != value:
            fail(f"signed_predecessor_{key}_drift:{value}:{spec.get(key)}")
    if data != expected:
        fail("signed_predecessor_bytes_drift")
    parsed = json.loads(data)
    if parsed.get("deployment_id") != spec.get("deployment_id") or parsed.get("release_root") != spec.get("release_root"):
        fail("signed_predecessor_identity_drift")
    return data, st


def load_authority() -> tuple[dict[str, Any], str]:
    require_regular(AUTHORITY, root_owned=True, non_writable=True, mode=0o444)
    envelope = load_json(AUTHORITY)
    if set(envelope) != {"schema", "payload", "signature"}:
        fail("authority_envelope_keys_invalid")
    if envelope.get("schema") != "glp34-production-authority-v1":
        fail("authority_envelope_schema_invalid")
    payload = envelope.get("payload")
    signature = envelope.get("signature")
    if not isinstance(payload, dict) or not isinstance(signature, dict):
        fail("authority_envelope_shape_invalid")
    if signature.get("algorithm") != "Ed25519" or signature.get("public_key_sha256") != sha_bytes(PUBLIC_KEY_PEM.encode("ascii")):
        fail("authority_signature_metadata_invalid")
    verify_signature(payload, str(signature.get("signature_base64", "")))
    payload_hash = sha_bytes(canonical(payload))
    if signature.get("payload_sha256") != payload_hash:
        fail("authority_payload_hash_invalid")
    if payload.get("schema") != "glp34-production-authority-v1-payload" or payload.get("status") != "PASS_LOCAL_AUTHORITY":
        fail("authority_payload_status_invalid")
    paths = payload.get("paths") or {}
    expected_paths = {
        "release": str(RELEASE), "toolchain": str(TOOLCHAIN), "carrier": str(CARRIER),
        "tests": str(TEST_FILE), "sensor": str(SENSOR), "surface_audit": str(SURFACE_AUDIT),
        "pointer": str(POINTER), "weekly_lock": str(LOCK_PATH), "authority": str(AUTHORITY),
    }
    if paths != expected_paths:
        fail("authority_exact_paths_invalid")
    artifacts = payload.get("artifacts") or {}
    verify_file_spec(CARRIER, artifacts.get("carrier") or {})
    verify_file_spec(TEST_FILE, artifacts.get("tests") or {})
    verify_file_spec(SENSOR, artifacts.get("sensor") or {})
    verify_file_spec(SURFACE_AUDIT, artifacts.get("surface_audit") or {})
    verify_file_spec(PYTHON, artifacts.get("python") or {}, non_writable=False)
    verify_file_spec(OPENSSL, artifacts.get("openssl") or {}, non_writable=False)
    if artifacts["python"].get("sha256") != PYTHON_SHA256 or artifacts["openssl"].get("sha256") != OPENSSL_SHA256:
        fail("authority_bootstrap_runtime_hash_invalid")
    verify_file_spec(Path(artifacts["preflight_candidate"]["path"]), artifacts["preflight_candidate"])
    verify_file_spec(Path(artifacts["preflight_release"]["path"]), artifacts["preflight_release"])
    release_spec = payload["release"]
    verify_immutable_tree(RELEASE, release_spec["closure"])
    candidate_twin = release_spec.get("candidate_byte_twin") or {}
    if candidate_twin.get("path") != str(CANDIDATE):
        fail("authority_candidate_twin_path_invalid")
    verify_immutable_tree(CANDIDATE, candidate_twin.get("closure") or {})
    if candidate_twin.get("closure") != release_spec["closure"]:
        fail("authority_candidate_release_twin_closure_drift")
    if file_closure(RELEASE / "public") != payload["release"]["public"]:
        fail("release_public_closure_drift")
    if file_closure(RELEASE / "functions") != payload["release"]["functions"]:
        fail("release_functions_closure_drift")
    expected_routes = {
        ("/", "public/index.html"),
        ("/best/online-glp-1-providers/", "public/best/online-glp-1-providers/index.html"),
        ("/cost/online-glp-1-programs/", "public/cost/online-glp-1-programs/index.html"),
        ("/reviews/trt-nation/", "public/reviews/trt-nation/index.html"),
        ("/quiz/provider-fit/", "public/quiz/provider-fit/index.html"),
        ("/data/therapeutic-taxonomy.json", "public/data/therapeutic-taxonomy.json"),
        ("/glp-1-price-index/", "public/glp-1-price-index/index.html"),
    }
    routes = payload.get("route_probes")
    if not isinstance(routes, list) or len(routes) != len(expected_routes):
        fail("authority_route_inventory_invalid")
    observed_routes = set()
    for item in routes:
        if not isinstance(item, dict) or set(item) != {"url_path", "path", "sha256", "bytes"}:
            fail("authority_route_spec_invalid")
        pair = (item["url_path"], item["path"])
        if not all(isinstance(value, str) for value in pair) or pair not in expected_routes:
            fail("authority_route_path_invalid")
        path = RELEASE / item["path"]
        if type(item["bytes"]) is not int or item["bytes"] != path.stat().st_size or item["sha256"] != sha_file(path):
            fail("authority_route_bytes_invalid")
        observed_routes.add(pair)
    if observed_routes != expected_routes:
        fail("authority_route_inventory_invalid")
    verify_immutable_tree(TOOLCHAIN, payload["toolchain"]["closure"], toolchain=True)
    if payload["toolchain"].get("version") != "4.114.0":
        fail("wrangler_version_authority_invalid")
    verify_file_spec(NODE, payload["toolchain"]["node"], nlink_one=False)
    verify_file_spec(WRANGLER, payload["toolchain"]["wrangler_js"], nlink_one=False)
    verify_file_spec(ESBUILD, payload["toolchain"]["esbuild"], nlink_one=False)
    for item in payload["toolchain"].get("executable_files", []):
        verify_file_spec(TOOLCHAIN / item["path"], {**item, "path": str(TOOLCHAIN / item["path"])},
                         nlink_one=False)
    provider = payload.get("provider_boundary") or {}
    if provider.get("account_id") != ACCOUNT_ID or provider.get("project") != PROJECT or provider.get("production_branch") != PRODUCTION_BRANCH:
        fail("provider_boundary_identity_invalid")
    if provider.get("allowed_mutations") != ["pages_production_deploy", "pages_rollback_only_on_failure"]:
        fail("provider_mutation_boundary_invalid")
    if provider.get("max_pages_production_deployments") != 1:
        fail("provider_deployment_count_boundary_invalid")
    if any(provider.get(key) != 0 for key in (
        "worker_deploys", "d1_mutations", "cron_mutations", "indexnow_posts",
        "discord_sends", "email_sends", "spend_usd", "pointer_non_cas_writes",
    )):
        fail("provider_zero_effect_boundary_invalid")
    commands = payload.get("commands") or {}
    joined = canonical(commands).decode("utf-8").lower()
    if "npx" in joined or '"npm"' in joined or "registry.npmjs" in joined:
        fail("mutable_package_layer_declared")
    if commands.get("node") != str(NODE) or commands.get("wrangler_js") != str(WRANGLER):
        fail("direct_wrangler_command_binding_invalid")
    expected_one_time = {
        "initial_status": "UNCONSUMED",
        "deploy_r12": False,
        "duplicate_deploy": False,
        "independent_review_rounds_completed": 2,
        "final_review_status": "BLOCK_INDEPENDENT_EXACT_BYTES",
        "requires_parent_recheck": True,
        "requires_parent_accepted_risk_closure": True,
        "requires_post_cas_live_completeness_pass_zero": True,
        "requires_post_cas_live_facet_binding_pass": True,
    }
    if payload.get("one_time") != expected_one_time:
        fail("one_time_authority_boundary_invalid")
    verify_signed_predecessor(payload["predecessor"]["pointer"])
    return payload, payload_hash


def sensor_env(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1",
    }


def run_actual_preflight_sensor(run_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    _attempt_id, run_dir = new_run(run_dir / "preflight-attempts", "preflight")
    output = run_dir / "preflight-completeness.json"
    if output.exists():
        fail("preflight_sensor_output_collision")
    else:
        command = [
            str(PYTHON), "-I", "-B", str(SENSOR), "--offline", "--release",
            str(RELEASE), "--output", str(output),
        ]
        proc = subprocess.run(
            command, cwd=str(BUSINESS), env=sensor_env(run_dir / "sensor-home"),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=1200,
        )
        atomic_file(run_dir / "preflight-sensor-command.json", json_bytes({
            "argv": command, "returncode": proc.returncode,
            "stdout_sha256": sha_bytes(proc.stdout.encode()),
            "stderr_sha256": sha_bytes(proc.stderr.encode()),
        }))
        if proc.returncode != 0 or not output.is_file():
            fail(f"preflight_sensor_failed:{proc.returncode}")
        result = load_json(output)
    checks = result.get("checks")
    if (not isinstance(checks, list) or len(checks) != len(EXPECTED_SENSOR_PASS_CHECKS) + 1
            or any(not isinstance(item, dict) or item.get("status") not in ("PASS", "SKIPPED") for item in checks)):
        fail("preflight_sensor_raw_checks_invalid")
    passed = [item.get("check") for item in checks if item.get("status") == "PASS"]
    skipped = [item.get("check") for item in checks if item.get("status") == "SKIPPED"]
    if (result.get("status") != "PASS_PARTIAL" or result.get("failed_checks") != []
            or type(result.get("failure_count")) is not int or result.get("failure_count") != 0):
        fail("preflight_sensor_not_passing")
    if passed != EXPECTED_SENSOR_PASS_CHECKS or skipped != ["base_chain"] or result.get("skipped_checks") != ["base_chain"]:
        fail("preflight_sensor_check_set_invalid")
    if result.get("effects") != EXPECTED_ZERO_EFFECTS or result.get("release_root") != str(RELEASE):
        fail("preflight_sensor_effect_or_release_invalid")
    receipt = {
        "status": result["status"], "receipt": str(output), "sha256": sha_file(output),
        "bytes": output.stat().st_size, "passed_checks": passed, "skipped_checks": skipped,
        "failure_count": 0, "effects": result["effects"],
    }
    atomic_file(run_dir / "preflight-sensor-gate.json", json_bytes(receipt))
    return receipt


def run_live_completeness_sensor(run_dir: Path, deployment_id: str) -> dict[str, Any]:
    """Require the post-CAS sensor to close with PASS, zero skips, and zero failures."""
    _attempt_id, run_dir = new_run(run_dir / "live-attempts", "live")
    output = run_dir / "live-completeness.json"
    if output.exists():
        fail("live_sensor_output_collision")
    else:
        command = [
            str(PYTHON), "-I", "-B", str(SENSOR), "--release", str(RELEASE),
            "--output", str(output), "--site", SITE,
        ]
        proc = subprocess.run(
            command, cwd=str(BUSINESS), env=sensor_env(run_dir / "live-sensor-home"),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=1200,
        )
        atomic_file(run_dir / "live-sensor-command.json", json_bytes({
            "argv": command, "returncode": proc.returncode,
            "stdout_sha256": sha_bytes(proc.stdout.encode()),
            "stderr_sha256": sha_bytes(proc.stderr.encode()),
        }))
        if proc.returncode != 0 or not output.is_file():
            fail(f"live_completeness_sensor_failed:{proc.returncode}")
        result = load_json(output)
    checks = result.get("checks")
    if (not isinstance(checks, list) or len(checks) != len(EXPECTED_LIVE_SENSOR_PASS_CHECKS)
            or any(not isinstance(item, dict) or item.get("status") != "PASS" for item in checks)):
        fail("live_sensor_raw_checks_invalid")
    passed = [item.get("check") for item in checks if item.get("status") == "PASS"]
    skipped = [item.get("check") for item in checks if item.get("status") == "SKIPPED"]
    base_chain = next((item for item in checks if item.get("check") == "base_chain"), {})
    if (result.get("status") != "PASS" or result.get("failed_checks") != []
            or type(result.get("failure_count")) is not int or result.get("failure_count") != 0):
        fail("live_completeness_sensor_not_passing")
    if passed != EXPECTED_LIVE_SENSOR_PASS_CHECKS or skipped or result.get("skipped_checks") != []:
        fail("live_completeness_sensor_check_set_invalid")
    if (result.get("effects") != EXPECTED_ZERO_EFFECTS or result.get("release_root") != str(RELEASE)
            or base_chain.get("base_deployment_id") != deployment_id
            or base_chain.get("live_canonical_deployment_id") != deployment_id):
        fail("live_completeness_sensor_binding_invalid")
    provider_reads = base_chain.get("provider_reads")
    if type(provider_reads) is not int or provider_reads not in (1, 2):
        fail("live_completeness_sensor_provider_read_count_invalid")
    receipt = {
        "status": result["status"], "receipt": str(output), "sha256": sha_file(output),
        "bytes": output.stat().st_size, "passed_checks": passed, "skipped_checks": skipped,
        "failure_count": 0, "provider_reads": provider_reads, "effects": result["effects"],
        "deployment_id": deployment_id,
    }
    atomic_file(run_dir / "live-sensor-gate.json", json_bytes(receipt))
    return receipt


def run_facet_binding_sensor() -> dict[str, Any]:
    scripts = [
        RELEASE / "tests/therapeutic-wire-in-propagation-build6.test.mjs",
        RELEASE / "tests/therapeutic-taxonomy-build6.test.mjs",
        RELEASE / "tests/therapeutic-surfaces-build6.test.mjs",
    ]
    for script in scripts:
        require_regular(script, root_owned=True, non_writable=True)
    proc = subprocess.run(
        [str(NODE), "--test", "--test-reporter=tap", *[str(script) for script in scripts]], cwd=str(RELEASE),
        env={"HOME": "/tmp", "PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=300,
    )
    if proc.returncode != 0:
        fail(f"facet_binding_sensor_failed:{proc.returncode}")
    counts = {}
    for key in ("tests", "pass", "fail", "cancelled", "skipped", "todo"):
        values = re.findall(rf"^# {key} ([0-9]+)$", proc.stdout, re.MULTILINE)
        if len(values) != 1:
            fail(f"facet_binding_sensor_tap_summary_invalid:{key}")
        counts[key] = int(values[0])
    if (counts["tests"] <= 0 or counts["pass"] != counts["tests"]
            or any(counts[key] != 0 for key in ("fail", "cancelled", "skipped", "todo"))):
        fail("facet_binding_sensor_tap_nonpass")
    return {"status": "PASS_FACET_BINDING_SENSOR", "stdout_sha256": sha_bytes(proc.stdout.encode()),
            "stderr_sha256": sha_bytes(proc.stderr.encode()), "provider_mutations": 0,
            "summary": counts}


def _rmtree_writable(path: Path) -> None:
    if not path.exists():
        return
    for current, dirs, files in os.walk(path, topdown=False, followlinks=False):
        for name in files:
            try:
                os.chmod(Path(current) / name, 0o600)
            except OSError:
                pass
        for name in dirs:
            try:
                os.chmod(Path(current) / name, 0o700)
            except OSError:
                pass
        try:
            os.chmod(current, 0o700)
        except OSError:
            pass
    shutil.rmtree(path)


def copytree_exact(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, symlinks=False)
    if file_closure(source) != file_closure(destination):
        fail(f"private_workspace_copy_drift:{source.name}")


def wrangler_env(workspace: Path, token: str | None = None) -> dict[str, str]:
    home = workspace / ".carrier-home"
    temp = workspace / ".carrier-tmp"
    home.mkdir(mode=0o700, exist_ok=True)
    temp.mkdir(mode=0o700, exist_ok=True)
    env = {
        "HOME": str(home), "TMPDIR": str(temp), "PATH": str(TOOLCHAIN / "runtime"),
        "CI": "1", "NO_COLOR": "1", "WRANGLER_HIDE_BANNER": "true",
        "WRANGLER_SEND_METRICS": "false", "WRANGLER_SEND_ERROR_REPORTS": "false",
        "ESBUILD_BINARY_PATH": str(ESBUILD),
    }
    if token is not None:
        env["CLOUDFLARE_API_TOKEN"] = token
        env["CLOUDFLARE_ACCOUNT_ID"] = ACCOUNT_ID
    return env


def prepare_workspace(run_dir: Path) -> Path:
    # Wrangler embeds absolute source-map paths.  A lock-serialized fixed path
    # is therefore part of the deterministic deploy closure; per-run paths
    # would produce byte-different bundles from identical release inputs.
    workspace = WRANGLER_WORKSPACE
    if workspace.exists():
        _rmtree_writable(workspace)
    workspace.mkdir(mode=0o700)
    for name in ("functions", "src", "data"):
        copytree_exact(RELEASE / name, workspace / name)
    workers = workspace / "workers"
    price = workers / "price-watch"
    price.mkdir(parents=True, mode=0o700)
    for rel in ("workers/price-watch/wrangler.toml", "workers/price-watch-scheduler.mjs", "workers/email-scheduler.mjs"):
        source = RELEASE / rel
        destination = workspace / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        if sha_file(source) != sha_file(destination):
            fail(f"worker_workspace_copy_drift:{rel}")
    (workspace / ".wrangler").mkdir(mode=0o700)
    (price / ".wrangler").mkdir(mode=0o700)
    return workspace


def canonicalize_wrangler_outputs(workspace: Path, pages_out: Path, price_out: Path) -> dict[str, int]:
    # Wrangler injects a random temp-module name into one generated JS comment
    # and a wall-clock timestamp into the dry-run README.  Neither is runtime
    # code.  Replace exactly those two nondeterministic metadata fields and
    # reject missing/additional matches so the deploy closure is byte-stable.
    pages_path = pages_out / "index.js"
    pages_raw = pages_path.read_bytes()
    pages_fixed, pages_count = re.subn(
        rb"// \.\./\.wrangler/tmp/pages-[^/\r\n]+/functionsRoutes-[^\r\n]+",
        b"// r13-canonical-functions-routes.mjs", pages_raw,
    )
    readme_path = price_out / "README.md"
    readme_raw = readme_path.read_bytes()
    readme_fixed, readme_count = re.subn(
        rb" generated at [^\.]+\.[0-9]+Z\.",
        b" generated deterministically by r13.", readme_raw,
    )
    if pages_count != 1 or readme_count != 1:
        fail(f"wrangler_nondeterminism_shape_drift:{pages_count}:{readme_count}")
    atomic_file(pages_path, pages_fixed, 0o600)
    atomic_file(readme_path, readme_fixed, 0o600)
    return {"pages_random_comment_replacements": pages_count,
            "price_readme_timestamp_replacements": readme_count}


def run_direct_wrangler_probe(run_dir: Path, payload: dict[str, Any], *, keep_workspace: bool = False) -> tuple[dict[str, Any], Path | None]:
    workspace = prepare_workspace(run_dir)
    pages_out = workspace / ".wrangler/pages-functions-build"
    price_out = workspace / ".wrangler/price-watch-dry-run"
    pages_out.mkdir(parents=True, mode=0o700)
    price_out.mkdir(parents=True, mode=0o700)
    env = wrangler_env(workspace)
    commands = [
        [str(NODE), str(WRANGLER), "pages", "functions", "build", "functions",
         "--outdir", str(pages_out)],
        [str(NODE), str(WRANGLER), "deploy", "--dry-run", "--config",
         "workers/price-watch/wrangler.toml", "--outdir", str(price_out)],
    ]
    command_receipts = []
    for index, command in enumerate(commands, 1):
        proc = subprocess.run(
            command, cwd=str(workspace), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=1200,
        )
        combined = (proc.stdout + "\n" + proc.stderr).lower()
        if proc.returncode != 0:
            fail(f"direct_wrangler_command_failed:{index}:{proc.returncode}")
        if "download" in combined or "registry.npmjs" in combined:
            fail(f"direct_wrangler_external_resolution_detected:{index}")
        command_receipts.append({
            "argv": command, "returncode": proc.returncode,
            "stdout_sha256": sha_bytes(proc.stdout.encode()),
            "stderr_sha256": sha_bytes(proc.stderr.encode()),
        })
    canonicalization = canonicalize_wrangler_outputs(workspace, pages_out, price_out)
    deploy_public = workspace / "deploy-public"
    copytree_exact(RELEASE / "public", deploy_public)
    os.chmod(deploy_public, 0o700)
    shutil.copyfile(pages_out / "index.js", deploy_public / "_worker.js")
    if sha_file(pages_out / "index.js") != sha_file(deploy_public / "_worker.js"):
        fail("canonical_worker_copy_drift")
    (workspace / "deploy-cwd").mkdir(mode=0o700)
    result = {
        "schema": "glp-direct-wrangler-probe-v3", "status": "PASS",
        "toolchain_version": "4.114.0", "commands": command_receipts,
        "canonicalization": canonicalization,
        "pages_functions_build": file_closure(pages_out),
        "price_watch_dry_run": file_closure(price_out),
        "deploy_directory": file_closure(deploy_public),
        "source_functions_after": file_closure(RELEASE / "functions"),
        "provider_calls": 0, "provider_mutations": 0, "registry_downloads": 0,
        "mutable_package_resolutions": 0, "npm_invocations": 0, "npx_invocations": 0,
    }
    expected = payload.get("wrangler_expected") or {}
    if result["pages_functions_build"] != expected.get("pages_functions_build"):
        fail("pages_functions_bundle_closure_drift")
    if result["price_watch_dry_run"] != expected.get("price_watch_dry_run"):
        fail("price_watch_dry_run_closure_drift")
    if result["deploy_directory"] != expected.get("deploy_directory"):
        fail("deploy_directory_closure_drift")
    if result["source_functions_after"] != payload["release"]["functions"]:
        fail("release_functions_changed_during_wrangler_probe")
    atomic_file(run_dir / "direct-wrangler-probe.json", json_bytes(result))
    if keep_workspace:
        return result, workspace
    _rmtree_writable(workspace)
    return result, None


def _cas_fault(point: str) -> None:
    hook = _CAS_FAULT_HOOK
    if hook is not None:
        hook(point)


def assert_lock_identity(lock_fd: int, lock_path: Path) -> None:
    fd_st = os.fstat(lock_fd)
    path_st = lock_path.lstat()
    if not stat.S_ISREG(fd_st.st_mode) or not stat.S_ISREG(path_st.st_mode) or lock_path.is_symlink():
        fail("weekly_lock_not_plain_regular")
    if (fd_st.st_dev, fd_st.st_ino) != (path_st.st_dev, path_st.st_ino):
        fail("weekly_lock_inode_replaced")


def _exact_artifact(path: Path, data: bytes, mode: int) -> None:
    if path.exists():
        st = require_regular(path, root_owned=False, mode=mode)
        if st.st_uid != os.getuid() or st.st_gid != os.getgid() or path.read_bytes() != data:
            fail(f"pointer_artifact_collision:{path}")
        return
    exclusive_file(path, data, mode)


def _pointer_exact(path: Path, expected: bytes, mode: int, uid: int, gid: int) -> os.stat_result:
    data, st = capture_pointer(path)
    if data != expected or _mode(st) != mode or st.st_uid != uid or st.st_gid != gid:
        fail(f"pointer_reconciliation_failed:{path}")
    return st


def atomic_pointer_cas(intent: dict[str, Any], lock_fd: int, *, production: bool = True) -> dict[str, Any]:
    pointer = Path(intent["pointer_path"])
    lock_path = Path(intent["lock_path"])
    if production and (pointer != POINTER or lock_path != LOCK_PATH):
        fail("production_cas_path_bypass")
    assert_lock_identity(lock_fd, lock_path)
    predecessor = base64.b64decode(intent["predecessor_bytes_base64"], validate=True)
    successor = base64.b64decode(intent["successor_bytes_base64"], validate=True)
    parsed = json.loads(successor)
    if not isinstance(parsed, dict):
        fail("pointer_successor_shape_invalid")
    for key in ("release_root", "deployment_id", "manifest", "final_receipt"):
        if key not in parsed:
            fail(f"pointer_successor_missing:{key}")
    temp_name = intent["temp_name"]
    if not isinstance(temp_name, str) or temp_name in ("", ".", "..") or Path(temp_name).name != temp_name:
        fail("pointer_temp_name_invalid")
    mode = int(intent["final_mode"], 8)
    uid = int(intent["final_uid"])
    gid = int(intent["final_gid"])
    current, current_st = capture_pointer(pointer)
    if current == successor:
        for artifact in intent.get("artifacts", []):
            _exact_artifact(Path(artifact["path"]), base64.b64decode(artifact["bytes_base64"], validate=True), int(artifact["mode"], 8))
        fsync_dir(pointer.parent)
        final_st = _pointer_exact(pointer, successor, mode, uid, gid)
        return {"status": "PASS_CAS_RECONCILED_AFTER_CRASH", "sha256": sha_bytes(successor),
                "st_dev": final_st.st_dev, "st_ino": final_st.st_ino}
    if current != predecessor:
        fail("pointer_cas_neither_predecessor_nor_successor")
    expected_stat = intent["predecessor_stat"]
    observed = {
        "st_dev": current_st.st_dev, "st_ino": current_st.st_ino, "uid": current_st.st_uid,
        "gid": current_st.st_gid, "mode": f"{_mode(current_st):04o}", "nlink": current_st.st_nlink,
    }
    if observed != expected_stat:
        fail(f"pointer_cas_predecessor_inode_or_mode_drift:{observed}:{expected_stat}")
    _cas_fault("after_immediate_predecessor_check")
    for artifact in intent.get("artifacts", []):
        _exact_artifact(Path(artifact["path"]), base64.b64decode(artifact["bytes_base64"], validate=True), int(artifact["mode"], 8))
    _cas_fault("after_artifacts")
    # Re-read exact bytes and exact inode immediately before the temp/replace sequence.
    immediate, immediate_st = capture_pointer(pointer)
    if immediate != predecessor or (immediate_st.st_dev, immediate_st.st_ino) != (current_st.st_dev, current_st.st_ino):
        fail("pointer_cas_immediate_predecessor_recheck_failed")
    parent_fd = os.open(pointer.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    temp_name = intent["temp_name"]
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        try:
            temp_fd = os.open(temp_name, flags, mode, dir_fd=parent_fd)
            try:
                _write_all(temp_fd, successor)
                _cas_fault("after_temp_write")
                os.fsync(temp_fd)
                _cas_fault("after_temp_bytes_fsync")
                os.fchmod(temp_fd, mode)
                os.fsync(temp_fd)
                temp_st = os.fstat(temp_fd)
                if temp_st.st_uid != uid or temp_st.st_gid != gid:
                    try:
                        os.fchown(temp_fd, uid, gid)
                        os.fsync(temp_fd)
                    except PermissionError:
                        fail("pointer_temp_owner_mismatch")
            finally:
                os.close(temp_fd)
        except FileExistsError:
            temp_path = pointer.parent / temp_name
            _pointer_exact(temp_path, successor, mode, uid, gid)
        _cas_fault("after_temp_fsync_and_mode")
        assert_lock_identity(lock_fd, lock_path)
        final_predecessor, final_predecessor_st = capture_pointer(pointer)
        if final_predecessor != predecessor or (final_predecessor_st.st_dev, final_predecessor_st.st_ino) != (current_st.st_dev, current_st.st_ino):
            fail("pointer_cas_final_predecessor_recheck_failed")
        _cas_fault("before_replace")
        os.replace(temp_name, pointer.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        _cas_fault("after_replace_before_parent_fsync")
        os.fsync(parent_fd)
        _cas_fault("after_parent_fsync")
    finally:
        os.close(parent_fd)
    final_st = _pointer_exact(pointer, successor, mode, uid, gid)
    return {"status": "PASS_ATOMIC_POINTER_CAS", "sha256": sha_bytes(successor),
            "st_dev": final_st.st_dev, "st_ino": final_st.st_ino}


def build_pointer_intent(payload: dict[str, Any], deployment_id: str, run_id: str,
                         *, now: str | None = None) -> dict[str, Any]:
    if not DEPLOYMENT_ID.fullmatch(deployment_id):
        fail("deployment_id_invalid_for_pointer")
    predecessor_spec = payload["predecessor"]["pointer"]
    predecessor, st = verify_signed_predecessor(predecessor_spec)
    created = now or utc()
    rows = file_rows(RELEASE)
    release_tree = payload["release"]["closure"]["tree_sha256"]
    public_tree = payload["release"]["public"]["tree_sha256"]
    report_dir = SEO_REPORT_ROOT / f"base-remint-r13-{deployment_id[:8]}-{run_id}"
    if report_dir.parent != SEO_REPORT_ROOT:
        fail("pointer_report_directory_escape")
    manifest_path = report_dir / "01-deployment-manifest.json"
    receipt_path = report_dir / "10-final-publication-receipt.json"
    manifest = {
        "schema": "glp-opus-release-deployment-manifest-v1", "created_at_utc": created,
        "release_root": str(RELEASE), "release_rows": rows,
        "release_tree_sha256": release_tree,
        "note": "purpose-specific r13 atomic pointer CAS manifest",
    }
    manifest_data = json_bytes(manifest)
    terminal = {
        "schema": "glp-opus-production-terminal-receipt-v1", "status": "PASS_PRODUCTION_TERMINAL",
        "created_at_utc": created, "deployment": {"deployment_id": deployment_id},
        "evidence_sha256": {manifest_path.name: sha_bytes(manifest_data)},
        "immutable_closure": {"release_tree_sha256": release_tree, "public_tree_sha256": public_tree},
        "note": f"terminal chain for release {RELEASE.name} @ deployment {deployment_id}",
    }
    terminal_data = json_bytes(terminal)
    predecessor_json = json.loads(predecessor)
    successor = copy.deepcopy(predecessor_json)
    prior = {"deployment_id": predecessor_json.get("deployment_id"),
             "release_root": predecessor_json.get("release_root")}
    successor.update({
        "release_root": str(RELEASE), "deployment_id": deployment_id,
        "manifest": {"path": str(manifest_path), "sha256": sha_bytes(manifest_data)},
        "final_receipt": {"path": str(receipt_path), "sha256": sha_bytes(terminal_data)},
        "status": "PASS", "reminted_at_utc": created,
        "release_tree_sha256": release_tree, "public_tree_sha256": public_tree,
        "supersedes": prior,
    })
    successor.pop("deployment_reconciliation", None)
    successor.pop("gsc_closeout", None)
    successor_data = json_bytes(successor)
    return {
        "schema": "glp-purpose-specific-atomic-pointer-cas-intent-v1",
        "pointer_path": str(POINTER), "lock_path": str(LOCK_PATH),
        "deployment_id": deployment_id, "release_root": str(RELEASE),
        "predecessor_sha256": sha_bytes(predecessor),
        "predecessor_bytes_base64": base64.b64encode(predecessor).decode(),
        "predecessor_stat": {"st_dev": st.st_dev, "st_ino": st.st_ino,
                             "uid": st.st_uid, "gid": st.st_gid,
                             "mode": f"{_mode(st):04o}", "nlink": st.st_nlink},
        "successor_sha256": sha_bytes(successor_data),
        "successor_bytes_base64": base64.b64encode(successor_data).decode(),
        "final_mode": f"{_mode(st):04o}", "final_uid": st.st_uid, "final_gid": st.st_gid,
        "temp_name": f".{POINTER.name}.r13-{run_id}.tmp",
        "artifacts": [
            {"path": str(manifest_path), "mode": "0600", "sha256": sha_bytes(manifest_data),
             "bytes_base64": base64.b64encode(manifest_data).decode()},
            {"path": str(receipt_path), "mode": "0600", "sha256": sha_bytes(terminal_data),
             "bytes_base64": base64.b64encode(terminal_data).decode()},
        ],
        "created_at_utc": created,
    }


def run_cas_canary(run_dir: Path) -> dict[str, Any]:
    root = run_dir / "atomic-cas-canary"
    root.mkdir(mode=0o700)
    lock_path = root / "weekly.lock"
    pointer = root / "current.json"
    predecessor = json_bytes({"schema": "canary", "value": "predecessor"})
    successor = json_bytes({"schema": "canary", "value": "successor", "release_root": "canary",
                            "deployment_id": "00000000-0000-0000-0000-000000000000",
                            "manifest": {}, "final_receipt": {}})
    exclusive_file(pointer, predecessor, 0o600)
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        st = pointer.lstat()
        intent = {
            "pointer_path": str(pointer), "lock_path": str(lock_path),
            "predecessor_bytes_base64": base64.b64encode(predecessor).decode(),
            "successor_bytes_base64": base64.b64encode(successor).decode(),
            "predecessor_stat": {"st_dev": st.st_dev, "st_ino": st.st_ino,
                                 "uid": st.st_uid, "gid": st.st_gid,
                                 "mode": "0600", "nlink": 1},
            "final_mode": "0600", "final_uid": st.st_uid, "final_gid": st.st_gid,
            "temp_name": ".current.json.canary.tmp", "artifacts": [],
        }
        first = atomic_pointer_cas(intent, lock_fd, production=False)
        second = atomic_pointer_cas(intent, lock_fd, production=False)
    finally:
        os.close(lock_fd)
    if pointer.read_bytes() != successor or first["status"] != "PASS_ATOMIC_POINTER_CAS" or second["status"] != "PASS_CAS_RECONCILED_AFTER_CRASH":
        fail("atomic_cas_canary_failed")
    result = {"status": "PASS", "first": first["status"], "resume": second["status"],
              "production_pointer_writes": 0, "canary_pointer": str(pointer)}
    atomic_file(run_dir / "atomic-cas-canary.json", json_bytes(result))
    return result


def acquire_lock() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(LOCK_PATH, flags, 0o640)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        fail("weekly_lock_busy")
    assert_lock_identity(fd, LOCK_PATH)
    return fd


def new_run(parent: Path, prefix: str) -> tuple[str, Path]:
    parent.mkdir(parents=True, exist_ok=True)
    run_id = f"{prefix}-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:12]}"
    run_dir = parent / run_id
    run_dir.mkdir(mode=0o700)
    fsync_dir(parent)
    return run_id, run_dir


def run_verify_only(payload: dict[str, Any], payload_hash: str) -> dict[str, Any]:
    pointer_before, pointer_st = capture_pointer(POINTER)
    run_id, run_dir = new_run(VERIFY_RUNS, "verify")
    sensor = run_actual_preflight_sensor(run_dir, payload)
    facet = run_facet_binding_sensor()
    probe, _workspace = run_direct_wrangler_probe(run_dir, payload, keep_workspace=False)
    canary = run_cas_canary(run_dir)
    pointer_after, pointer_after_st = capture_pointer(POINTER)
    if pointer_after != pointer_before or (pointer_after_st.st_dev, pointer_after_st.st_ino) != (pointer_st.st_dev, pointer_st.st_ino):
        fail("verify_only_production_pointer_effect")
    receipt = {
        "schema": "glp34-authority-v1-verify-receipt", "status": "PASS_VERIFY_ONLY",
        "run_id": run_id, "captured_at_utc": utc(), "authority_payload_sha256": payload_hash,
        "release": payload["release"], "toolchain": payload["toolchain"]["closure"],
        "sensor": sensor, "facet_binding_sensor": facet,
        "direct_wrangler": probe, "atomic_cas_canary": canary,
        "effects": {"provider_calls": 0, "provider_mutations": 0,
                    "production_pointer_writes": 0, "one_time_marker_writes": 0,
                    "indexnow_posts": 0, "d1_mutations": 0, "cron_mutations": 0,
                    "discord_sends": 0, "email_sends": 0, "spend_usd": 0},
        "production_pointer": {"sha256_before": sha_bytes(pointer_before),
                               "sha256_after": sha_bytes(pointer_after),
                               "inode_before": pointer_st.st_ino,
                               "inode_after": pointer_after_st.st_ino},
    }
    receipt_path = run_dir / "VERIFY-RECEIPT-v3.json"
    checkpoint(receipt_path, receipt)
    return {"status": receipt["status"], "receipt": str(receipt_path),
            "receipt_sha256": sha_file(receipt_path), "effects": receipt["effects"]}


def read_token() -> str:
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if token:
        return token
    try:
        for line in ENV_FILE.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("CLOUDFLARE_API_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    except OSError:
        pass
    if not token:
        fail("cloudflare_token_missing")
    return token


class CloudflareClient:
    def __init__(self, token: str):
        self.token = token
        self.provider_calls = 0
        self.provider_mutations = 0

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, *, envelope: bool = False) -> Any:
        data = None if body is None else canonical(body)
        request = urllib.request.Request(
            API_BASE + path, method=method, data=data,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                     "User-Agent": "glp-r13-authority-carrier/3"},
        )
        self.provider_calls += 1
        if method != "GET":
            self.provider_mutations += 1
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read())
        except (urllib.error.URLError, ValueError) as exc:
            fail(f"cloudflare_request_failed:{method}:{path}:{type(exc).__name__}")
        if not payload.get("success"):
            fail(f"cloudflare_api_unsuccessful:{method}:{path}")
        return payload if envelope else payload.get("result")

    def project(self) -> dict[str, Any]:
        return self.request("GET", f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}")

    def deployments(self) -> list[dict[str, Any]]:
        deployments = []
        seen = set()
        page = 1
        totals = None
        while True:
            response = self.request("GET", f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}/deployments?page={page}&per_page=100", envelope=True)
            result = response.get("result")
            info = response.get("result_info")
            if not isinstance(result, list) or not isinstance(info, dict):
                fail("deployment_list_shape_invalid")
            pages, count = info.get("total_pages"), info.get("total_count")
            if (type(pages) is not int or type(count) is not int or pages < 0 or count < 0
                    or type(info.get("page")) is not int or info["page"] != page):
                fail("deployment_list_pagination_invalid")
            if totals is None:
                totals = (pages, count)
            if totals != (pages, count) or (pages == 0 and (page != 1 or count != 0 or result)):
                fail("deployment_list_changed_during_read")
            for item in result:
                if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                        or not DEPLOYMENT_ID.fullmatch(item["id"]) or item["id"] in seen
                        or item.get("environment") not in ("production", "preview")):
                    fail("deployment_list_identity_invalid")
                seen.add(item["id"])
                deployments.append(item)
            if page >= pages:
                if len(deployments) != count:
                    fail("deployment_list_incomplete")
                return deployments
            page += 1

    def rollback(self, deployment_id: str) -> None:
        self.request("POST", f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}/deployments/{deployment_id}/rollback", {})


def production_deployment(project: dict[str, Any]) -> dict[str, Any]:
    if project.get("name") != PROJECT or project.get("production_branch") != PRODUCTION_BRANCH:
        fail("pages_project_identity_mismatch")
    if project.get("subdomain") != f"{PROJECT}.pages.dev":
        fail("pages_project_subdomain_mismatch")
    # Deployment-list order and latest_deployment are attribution evidence, not
    # the production routing pointer. Cloudflare's canonical_deployment field
    # is the only accepted current head.
    deployment = project.get("canonical_deployment")
    if not isinstance(deployment, dict):
        fail("pages_project_has_no_canonical_deployment")
    return deployment


def deployment_commit(item: dict[str, Any]) -> str | None:
    return ((item.get("deployment_trigger") or {}).get("metadata") or {}).get("commit_hash")


def deployment_branch(item: dict[str, Any]) -> str | None:
    return ((item.get("deployment_trigger") or {}).get("metadata") or {}).get("branch")


def successful_production(item: dict[str, Any]) -> bool:
    return (
        item.get("environment") == "production"
        and deployment_branch(item) == PRODUCTION_BRANCH
        and (item.get("latest_stage") or {}).get("status") == "success"
    )


def reserve_live(payload_hash: str) -> tuple[str, Path, Path, dict[str, Any]]:
    if ONE_TIME_STATE.exists():
        state = load_json(ONE_TIME_STATE)
        if state.get("authority_payload_sha256") != payload_hash:
            fail("one_time_state_authority_mismatch")
        if state.get("status") != "ACTIVE":
            fail(f"one_time_authority_consumed:{state.get('status')}")
        run_dir = Path(state["run_dir"])
        receipt_path = Path(state["receipt"])
        if run_dir.parent != LIVE_RUNS or receipt_path.parent != run_dir:
            fail("one_time_state_path_invalid")
        return state["run_id"], run_dir, receipt_path, state
    run_id, run_dir = new_run(LIVE_RUNS, "live")
    receipt_path = run_dir / "LIVE-RECEIPT-v3.json"
    state = {
        "schema": "glp34-authority-v1-one-time-state", "status": "ACTIVE",
        "run_id": run_id, "run_dir": str(run_dir), "receipt": str(receipt_path),
        "authority_payload_sha256": payload_hash, "created_at_utc": utc(),
    }
    exclusive_file(receipt_path, json_bytes({
        "schema": "glp34-authority-v1-live-receipt", "status": "ACTIVE",
        "run_id": run_id, "stages": {}, "effects": {},
    }))
    exclusive_file(ONE_TIME_STATE, json_bytes(state))
    return run_id, run_dir, receipt_path, state


def route_probe(url: str, expected_sha256: str, expected_bytes: int) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "glp-r13-authority-carrier/3",
                                                   "Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            status_code = response.status
    except urllib.error.URLError as exc:
        fail(f"route_probe_failed:{url}:{type(exc).__name__}")
    if status_code != 200 or len(body) != expected_bytes or sha_bytes(body) != expected_sha256:
        fail(f"route_probe_invariant_failed:{url}:{status_code}")
    return {"url": url, "status": status_code, "bytes": len(body),
            "body_sha256": sha_bytes(body)}


def route_matrix(client: CloudflareClient, deployment: dict[str, Any],
                 route_specs: list[dict[str, Any]], *, timeout_seconds: int = 120) -> list[dict[str, Any]]:
    deployment_id = str(deployment.get("id", ""))
    immutable = str(deployment.get("url", "")).rstrip("/")
    if (not DEPLOYMENT_ID.fullmatch(deployment_id) or not immutable.startswith("https://")
            or not immutable.endswith(f".{PROJECT}.pages.dev")):
        fail("deployment_immutable_identity_invalid")
    bases = [immutable, f"https://{PROJECT}.pages.dev", SITE]
    deadline = time.monotonic() + timeout_seconds
    last_error = "route_matrix_not_attempted"
    while True:
        project = client.project()
        canonical_head = production_deployment(project)
        if canonical_head.get("id") != deployment_id:
            fail(f"canonical_head_changed_during_route_matrix:{canonical_head.get('id')}")
        try:
            results = []
            for base in bases:
                for item in route_specs:
                    results.append(route_probe(base + item["url_path"], item["sha256"], item["bytes"]))
            return results
        except GateError as exc:
            last_error = str(exc)
            if time.monotonic() >= deadline:
                fail(f"route_matrix_did_not_converge:{last_error}")
            time.sleep(5)


def run_live(payload: dict[str, Any], payload_hash: str, lock_fd: int) -> dict[str, Any]:
    run_id, run_dir, receipt_path, state = reserve_live(payload_hash)
    receipt = load_json(receipt_path)
    stages = receipt.setdefault("stages", {})
    stages["preflight_sensor"] = run_actual_preflight_sensor(run_dir, payload)
    checkpoint(receipt_path, receipt)
    stages["facet_binding_sensor"] = run_facet_binding_sensor()
    checkpoint(receipt_path, receipt)
    # Rebuild from the sealed release on every invocation, including a crash
    # resume, so a stale/tampered deployment workspace can never be trusted.
    probe, workspace = run_direct_wrangler_probe(run_dir, payload, keep_workspace=True)
    stages["direct_wrangler"] = probe
    stages["workspace"] = str(workspace)
    checkpoint(receipt_path, receipt)
    _canary_id, canary_dir = new_run(run_dir / "canary-attempts", "canary")
    stages["atomic_cas_canary"] = run_cas_canary(canary_dir)
    checkpoint(receipt_path, receipt)
    # No provider object, credential read, provider request, or provider process exists above this line.
    client = CloudflareClient(read_token())
    predecessor_id = payload["predecessor"]["deployment_id"]
    # Cloudflare's commit identity is git-shaped (40 hexadecimal characters),
    # while the authority retains and verifies the complete SHA-256 closure.
    commit_hash = payload["release"]["closure"]["tree_sha256"][:40]
    project = client.project()
    current = production_deployment(project)
    current_id = current.get("id")
    deployments = client.deployments()
    candidates = [item for item in deployments
                  if deployment_commit(item) == commit_hash and item.get("environment") == "production" and deployment_branch(item) == PRODUCTION_BRANCH]
    deployment: dict[str, Any] | None = None
    reconciled_prior_provider_mutation = False
    prior_intent = stages.get("pages_deploy_intent")
    if prior_intent is not None and (not isinstance(prior_intent, dict)
            or prior_intent.get("predecessor_id") != predecessor_id
            or prior_intent.get("commit_hash") != commit_hash):
        fail("pages_deploy_intent_binding_invalid")
    if current_id != predecessor_id and prior_intent is None:
        fail("candidate_head_without_this_run_deploy_intent")
    if current_id == predecessor_id:
        if candidates:
            fail("ambiguous_existing_candidate_deployment_before_effect")
        created_intent = False
        if "pages_deploy_intent" not in stages:
            stages["pages_deploy_intent"] = {"predecessor_id": predecessor_id,
                                               "commit_hash": commit_hash, "created_at_utc": utc()}
            checkpoint(receipt_path, receipt)
            created_intent = True
        # A resumed ACTIVE state with an existing intent is reconciliation-only.
        # It never replays the provider mutation after a crash or timeout.
        if created_intent:
            immediate_head = production_deployment(client.project())
            if immediate_head.get("id") != predecessor_id or not successful_production(immediate_head):
                fail("canonical_predecessor_changed_before_publish")
            verify_signed_predecessor(payload["predecessor"]["pointer"])
            if file_closure(workspace / "deploy-public") != payload["wrangler_expected"]["deploy_directory"]:
                fail("deploy_directory_changed_before_publish")
            command = [str(NODE), str(WRANGLER), "pages", "deploy", str(workspace / "deploy-public"),
                       "--project-name", PROJECT, "--branch", PRODUCTION_BRANCH,
                       "--commit-hash", commit_hash, "--commit-message", "GLP34 r1 authority carrier",
                       "--no-bundle"]
            proc = subprocess.run(command, cwd=str(workspace / "deploy-cwd"), env=wrangler_env(workspace, client.token),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, timeout=1800)
            client.provider_calls += 1
            client.provider_mutations += 1
            stages["pages_deploy_cli"] = {
                "returncode": proc.returncode,
                "stdout_sha256": sha_bytes(proc.stdout.encode()),
                "stderr_sha256": sha_bytes(proc.stderr.encode()),
            }
            checkpoint(receipt_path, receipt)
        project = client.project()
        current = production_deployment(project)
        deployments = client.deployments()
        candidates = [item for item in deployments
                      if deployment_commit(item) == commit_hash and item.get("environment") == "production" and deployment_branch(item) == PRODUCTION_BRANCH]
    if (deployment_commit(current) == commit_hash and successful_production(current)
            and current.get("id") != predecessor_id):
        matching = [item for item in candidates if item.get("id") == current.get("id")]
        if len(candidates) != 1 or len(matching) != 1:
            fail("deployed_candidate_reconciliation_ambiguous")
        deployment = current
        if client.provider_mutations == 0 and "pages_deploy_intent" in stages:
            reconciled_prior_provider_mutation = True
            stages["provider_effect_reconciliation"] = {
                "status": "PASS_RECONCILED_PRIOR_PROCESS_DEPLOYMENT_EFFECT",
                "provider_mutations": 1,
                "provider_calls_prior_process": "unknown_nonzero",
            }
    else:
        fail(f"unexpected_production_head_after_deploy:{current.get('id')}")
    deployment_id = str(deployment.get("id"))
    if not DEPLOYMENT_ID.fullmatch(deployment_id):
        fail("new_deployment_id_invalid")
    stages["pages_deployment"] = {"deployment_id": deployment_id, "commit_hash": commit_hash,
                                   "immutable_url": deployment.get("url")}
    checkpoint(receipt_path, receipt)
    try:
        probes = route_matrix(client, deployment, payload.get("route_probes", []))
        stages["route_probes"] = probes
        checkpoint(receipt_path, receipt)
        pre_pointer_head = production_deployment(client.project())
        if pre_pointer_head.get("id") != deployment_id:
            fail(f"canonical_head_changed_before_pointer:{pre_pointer_head.get('id')}")
        stages["canonical_pre_pointer"] = {"deployment_id": deployment_id}
        checkpoint(receipt_path, receipt)
        if "pointer_intent" not in stages:
            stages["pointer_intent"] = build_pointer_intent(payload, deployment_id, run_id)
            checkpoint(receipt_path, receipt)
        intent = stages["pointer_intent"]
        if not isinstance(intent, dict) or not isinstance(intent.get("created_at_utc"), str) or not intent["created_at_utc"]:
            fail("pointer_intent_shape_invalid")
        expected_intent = build_pointer_intent(payload, deployment_id, run_id, now=intent["created_at_utc"])
        if intent != expected_intent:
            fail("pointer_intent_authority_binding_invalid")
        stages["pointer_cas"] = atomic_pointer_cas(intent, lock_fd, production=True)
        checkpoint(receipt_path, receipt)
        post_pointer_head = production_deployment(client.project())
        if post_pointer_head.get("id") != deployment_id:
            fail(f"canonical_head_changed_after_pointer:{post_pointer_head.get('id')}")
        stages["canonical_post_pointer"] = {"deployment_id": deployment_id}
        checkpoint(receipt_path, receipt)
        stages["live_completeness_sensor"] = run_live_completeness_sensor(run_dir, deployment_id)
        checkpoint(receipt_path, receipt)
        stages["live_facet_binding_sensor"] = run_facet_binding_sensor()
        checkpoint(receipt_path, receipt)
        post_sensor_head = production_deployment(client.project())
        if post_sensor_head.get("id") != deployment_id:
            fail(f"canonical_head_changed_after_live_sensor:{post_sensor_head.get('id')}")
        stages["canonical_post_live_sensor"] = {"deployment_id": deployment_id}
        successor = base64.b64decode(intent["successor_bytes_base64"], validate=True)
        pointer_st = _pointer_exact(POINTER, successor, int(intent["final_mode"], 8), int(intent["final_uid"]), int(intent["final_gid"]))
        if (pointer_st.st_dev, pointer_st.st_ino) != (stages["pointer_cas"]["st_dev"], stages["pointer_cas"]["st_ino"]):
            fail("pointer_changed_after_live_sensor")
        stages["pointer_post_live_sensor"] = {"sha256": sha_bytes(successor), "st_dev": pointer_st.st_dev, "st_ino": pointer_st.st_ino}
        checkpoint(receipt_path, receipt)
    except Exception as exc:
        current_pointer, _ = capture_pointer(POINTER)
        successor = base64.b64decode((stages.get("pointer_intent") or {}).get("successor_bytes_base64", "")) if stages.get("pointer_intent") else b""
        if successor and current_pointer == successor:
            provider_mutations = max(client.provider_mutations, 1 if reconciled_prior_provider_mutation else 0)
            receipt["status"] = "BLOCKED_POST_CAS_VALIDATION"
            receipt["blocker"] = {"stage": "post_cas_validation", "error_type": type(exc).__name__}
            receipt["effects"] = {
                "provider_calls_current_process": client.provider_calls,
                "provider_calls_prior_process": "unknown_nonzero" if reconciled_prior_provider_mutation else 0,
                "provider_mutations": provider_mutations,
                "pages_deployments": 1,
                "pages_rollbacks": 0,
                "pointer_atomic_cas": 1,
                "worker_deploys": 0,
                "d1_mutations": 0,
                "cron_mutations": 0,
                "discord_sends": 0,
                "email_sends": 0,
                "spend_usd": 0,
            }
            checkpoint(receipt_path, receipt)
            state["status"] = "CONSUMED_BLOCKED_POST_CAS_VALIDATION"
            state["deployment_id"] = deployment_id
            state["completed_at_utc"] = utc()
            checkpoint(ONE_TIME_STATE, state)
            raise
        rollback_head = production_deployment(client.project())
        if rollback_head.get("id") != deployment_id:
            receipt["status"] = "BLOCKED_FOREIGN_CANONICAL_HEAD_NO_ROLLBACK"
            receipt["effects"] = {"provider_calls": client.provider_calls,
                                  "provider_mutations": client.provider_mutations,
                                  "pages_rollback": 0}
            checkpoint(receipt_path, receipt)
            state["status"] = "CONSUMED_BLOCKED_FOREIGN_HEAD"
            checkpoint(ONE_TIME_STATE, state)
            raise
        verify_signed_predecessor(payload["predecessor"]["pointer"])
        client.rollback(predecessor_id)
        restored = production_deployment(client.project())
        if restored.get("id") != predecessor_id:
            fail(f"rollback_canonical_reconciliation_failed:{restored.get('id')}")
        verify_signed_predecessor(payload["predecessor"]["pointer"])
        receipt["status"] = "BLOCKED_ROLLED_BACK"
        receipt["effects"] = {"provider_calls": client.provider_calls,
                              "provider_mutations": client.provider_mutations,
                              "pages_rollback": 1}
        checkpoint(receipt_path, receipt)
        state["status"] = "CONSUMED_BLOCKED_ROLLED_BACK"
        checkpoint(ONE_TIME_STATE, state)
        raise
    receipt["status"] = "PASS_PRODUCTION_TERMINAL"
    provider_mutations = max(client.provider_mutations, 1 if reconciled_prior_provider_mutation else 0)
    receipt["effects"] = {
        "provider_calls": (None if reconciled_prior_provider_mutation else client.provider_calls + stages["live_completeness_sensor"]["provider_reads"]),
        "provider_calls_current_process": client.provider_calls + stages["live_completeness_sensor"]["provider_reads"],
        "provider_calls_prior_process": "unknown_nonzero" if reconciled_prior_provider_mutation else 0,
        "sensor_provider_reads": stages["live_completeness_sensor"]["provider_reads"],
        "provider_mutations": provider_mutations,
        "pages_deployments": 1, "pages_rollbacks": 0, "pointer_atomic_cas": 1,
        "indexnow_posts": 0, "worker_deploys": 0, "d1_mutations": 0,
        "cron_mutations": 0, "discord_sends": 0, "email_sends": 0, "spend_usd": 0,
    }
    receipt["completed_at_utc"] = utc()
    checkpoint(receipt_path, receipt)
    state["status"] = "CONSUMED_PASS"
    state["deployment_id"] = deployment_id
    state["completed_at_utc"] = utc()
    checkpoint(ONE_TIME_STATE, state)
    _rmtree_writable(workspace)
    return {"status": receipt["status"], "receipt": str(receipt_path),
            "receipt_sha256": sha_file(receipt_path), "deployment_id": deployment_id}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--verify-only", action="store_true")
    modes.add_argument("--live", action="store_true")
    return parser.parse_args(argv)


def legacy_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    lock_fd = -1
    try:
        lock_fd = acquire_lock()
        payload, payload_hash = load_authority()
        result = run_verify_only(payload, payload_hash) if args.verify_only else run_live(payload, payload_hash, lock_fd)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        print(json.dumps({"status": "BLOCK", "error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 1
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)


# Maintained package-only extension, embedded by generate_glp34_deploy_carrier.py.
# Package-only remains GET-only. Current preparation and approved consumption
# are separate branches below; neither changes historical authority/pins.

class PackageNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fail("package_provider_redirect_refused")


def package_get(path: str, token: str) -> dict[str, Any]:
    """Fixed-origin GET-only transport; never reuse the live mutation client."""
    prefix = f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}"
    if path != prefix and not re.fullmatch(re.escape(prefix) + r"/deployments/" + DEPLOYMENT_ID.pattern[1:-1], path):
        fail("package_read_path_invalid")
    request = urllib.request.Request(API_BASE + path, method="GET", headers={
        "Authorization": f"Bearer {token}", "User-Agent": "glp34-package-only/1"})
    try:
        with urllib.request.build_opener(PackageNoRedirect()).open(request, timeout=45) as response:
            envelope = json.loads(response.read())
    except (urllib.error.URLError, ValueError) as exc:
        fail(f"package_provider_read_failed:{type(exc).__name__}")
    if not isinstance(envelope, dict) or envelope.get("success") is not True or not isinstance(envelope.get("result"), dict):
        fail("package_provider_response_invalid")
    return envelope["result"]


def package_rollback(rollback_id: str, token: str, *, detail: bool) -> None:
    prefix = f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}"
    canonical_deployment = production_deployment(package_get(prefix, token))
    if canonical_deployment.get("id") != rollback_id or not successful_production(canonical_deployment):
        fail("package_rollback_not_current_successful_production")
    if detail:
        deployment = package_get(prefix + "/deployments/" + rollback_id, token)
        if deployment.get("id") != rollback_id or not successful_production(deployment):
            fail("package_rollback_detail_mismatch")


def package_plain_path(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        fail("package_path_must_be_absolute")
    for component in reversed([path, *path.parents]):
        if component.is_symlink():
            fail(f"package_symlink_path:{component}")
    return path


def package_tree(root: Path) -> tuple[str, list[dict[str, str]]]:
    """Site ci-release-gate.mjs treeFiles v1: recursive JS-sort + JSON rows.

    This is deliberately NOT the historical carrier's size-bearing file_closure.
    Nonregular members refuse rather than silently falling out of the census.
    """
    package_plain_path(root)
    _walk_plain(root)
    rows = []

    def walk(directory):
        for path in sorted(directory.iterdir(), key=lambda p: p.name.encode("utf-16-be")):
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                walk(path)
            elif stat.S_ISREG(mode):
                rows.append({"file": path.relative_to(root).as_posix(), "sha256": sha_file(path)})
            else:
                fail(f"package_tree_special:{path}")
    walk(root)
    if not rows:
        fail("package_tree_empty")
    # Preserve key insertion order to match JSON.stringify, not canonical().
    raw = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return sha_bytes(raw), rows


def package_build_receipt(args: argparse.Namespace, rows: list[dict[str, str]]) -> dict[str, str]:
    """Consume the site's existing gate receipt; never mint build evidence.

    The expected digest must come from reviewed build evidence, not be refreshed
    to make a changed receipt pass. This is a pinned receipt, not a signature.
    """
    if not HEX64.fullmatch(args.expected_build_receipt_sha256):
        fail("package_build_receipt_hash_invalid")
    path = package_plain_path(Path(args.build_receipt))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as reader:
        if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
            fail("package_build_receipt_not_regular")
        raw = reader.read()
    if sha_bytes(raw) != args.expected_build_receipt_sha256:
        fail("package_build_receipt_hash_mismatch")
    receipt = json.loads(raw)
    if not isinstance(receipt, dict) or receipt.get("schema") != "glp-d1-release-result-v1" or receipt.get("status") != "PASS":
        fail("package_build_receipt_invalid")
    if receipt.get("head") != args.site_head:
        fail("package_build_receipt_head_mismatch")
    for phase in ("output_before", "output_after"):
        output = receipt.get(phase)
        if not isinstance(output, dict) or output.get("sha256") != args.expected_tree_sha256 or output.get("files") != rows:
            fail("package_build_receipt_tree_mismatch")
    return {"path": str(path), "sha256": sha_bytes(raw), "schema": receipt["schema"]}


def package_site_commit(site_head: str) -> None:
    """The only package subprocess: fixed-repository, explicit GitHub GET."""
    if not re.fullmatch(r"[0-9a-f]{40}", site_head):
        fail("package_site_head_invalid")
    command = ["gh", "api", "--hostname", "github.com", "--method", "GET",
               "repos/izaiahat/glp-site-candidate/commits/" + site_head]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=45)
        commit = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        # Includes missing commit, auth/network failures and unavailable gh.
        # Never print subprocess output, headers or credentials.
        fail("package_site_commit_read_failed")
    if not isinstance(commit, dict) or commit.get("sha") != site_head:
        fail("package_site_commit_mismatch")


def run_package_only(args: argparse.Namespace) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", args.site_head):
        fail("package_site_head_invalid")
    if not HEX64.fullmatch(args.expected_tree_sha256):
        fail("package_expected_tree_invalid")
    if not DEPLOYMENT_ID.fullmatch(args.rollback_deployment_id):
        fail("package_rollback_id_invalid")
    public = package_plain_path(Path(args.public_root))
    output = package_plain_path(Path(args.package_dir))
    # POSIX pathlib preserves //, but Linux resolves it to the same / root.
    # Compare normalized anchors before any remote read or output creation.
    overlap_public = Path("/" + str(public).lstrip("/"))
    overlap_output = Path("/" + str(output).lstrip("/"))
    if overlap_output == overlap_public or overlap_public in overlap_output.parents or overlap_output in overlap_public.parents:
        fail("package_output_overlaps_input")
    if output.exists() or not output.parent.is_dir():
        fail("package_output_must_be_new_with_existing_parent")
    tree_hash, rows = package_tree(public)
    if tree_hash != args.expected_tree_sha256:
        fail("package_public_tree_mismatch")
    receipt_binding = package_build_receipt(args, rows)
    package_site_commit(args.site_head)
    token = read_token()
    package_rollback(args.rollback_deployment_id, token, detail=True)
    # First local write follows receipt/head/tree + current rollback checks.
    output.mkdir(mode=0o700)
    try:
        target = output / "public"
        target.mkdir()
        for row in rows:
            source = public / row["file"]
            destination = target / row["file"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            # No symlink following during copy; destination is a new private tree.
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as reader:
                if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                    fail("package_copy_not_regular")
                with destination.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
        if package_tree(target) != (tree_hash, rows) or package_tree(public) != (tree_hash, rows):
            fail("package_tree_changed_during_copy")
        package_rollback(args.rollback_deployment_id, token, detail=False)
        package_build_receipt(args, rows)  # refuse changed receipt before completion
        manifest = {
            "schema": "glp34-deploy-package-v1", "mode": "package-only",
            "site_head": args.site_head, "site_head_binding": "sha256-pinned-site-gate-receipt-and-github-commit-get",
            "site_repository": "izaiahat/glp-site-candidate", "github_reads": 1,
            "build_receipt": receipt_binding,
            "public_root": str(public), "public_tree_sha256": tree_hash,
            "tree_hash_algorithm": "site-treeFiles-v1-json-file-sha256-recursive-js-sort",
            "file_count": len(rows), "files": rows,
            "rollback_deployment_id": args.rollback_deployment_id,
            "rollback_environment": "production", "rollback_branch": PRODUCTION_BRANCH,
            "account_id": ACCOUNT_ID, "project": PROJECT,
            "rollback_checked_at_utc": utc(), "provider_reads": 3, "provider_mutations": 0,
            "release_authorized": False, "authority_created": False, "signature_created": False,
            "scope": "public bytes only; not Functions build or release acceptance",
        }
        # A final readback must cover all writes and the last rollback GET.
        manifest_bytes = json_bytes(manifest)
        exclusive_file(output / "PACKAGE-MANIFEST.json", manifest_bytes, mode=0o444)
        persisted_manifest = (output / "PACKAGE-MANIFEST.json").read_bytes()
        if persisted_manifest != manifest_bytes:
            fail("package_final_manifest_mismatch")
        manifest_sha256 = sha_bytes(persisted_manifest)
        if package_tree(target) != (manifest["public_tree_sha256"], manifest["files"]):
            fail("package_final_tree_mismatch")
        return {"status": "PASS_PACKAGE_ONLY", "package_dir": str(output),
                "manifest_sha256": manifest_sha256,
                "public_tree_sha256": tree_hash, "file_count": len(rows),
                "provider_reads": 3, "github_reads": 1, "provider_mutations": 0, "release_authorized": False}
    except BaseException:
        shutil.rmtree(output)
        raise


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if any(mode in argv for mode in ("--prepare-current", "--publish-current", "--promote-current-existing")):
        return current_main(argv)
    if "--package-only" not in argv:
        return legacy_main(argv)
    parser = argparse.ArgumentParser(description="Package public bytes; no release authorization or deployment")
    parser.add_argument("--package-only", action="store_true", required=True)
    for flag in ("site-head", "public-root", "expected-tree-sha256", "build-receipt",
                 "expected-build-receipt-sha256", "rollback-deployment-id", "package-dir"):
        parser.add_argument("--" + flag, required=True)
    args = parser.parse_args(argv)
    try:
        result = run_package_only(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (GateError, OSError, ValueError, TypeError, AttributeError) as exc:
        # Never print credential-bearing HTTP objects or response bodies.
        error = str(exc) if isinstance(exc, GateError) else type(exc).__name__
        print(json.dumps({"status": "BLOCK", "error": error}), file=sys.stderr)
        return 1


# Current-head successor. Historical authority bodies above stay unchanged.
CURRENT_ROLLBACK = "ca0251a2-dd4e-469c-be3b-79ee8805c5f3"
CURRENT_REPOSITORY = "izaiahat/glp-site-candidate"
CURRENT_TOOLCHAIN = {"closure_sha256": "1af0afc423948202725ae9299b4803c46b68cbb053358943a17123c6dfbc4ad7",
                     "file_count": 1662, "directory_count": 258, "total_bytes": 621665046}
CURRENT_AUTHORITIES = {
    "ops/linear/approvals/OPERATOR-DECISION-20260926-release-target-root-195.md":
        "f9ff3e92029b5fd6a3582ca7e7dc64fad9e699adc2b28f0b842d62d17a7870ec",
    "ops/linear/approvals/OPERATOR-DECISION-20260926-release-review-astra.md":
        "7d0d49def4254a19e088d4d6586c9963325311da17cfbd6054dd175876240fb0",
}


def current_json(path: Path, digest: str) -> dict[str, Any]:
    package_plain_path(path)
    require_regular(path)
    raw = path.read_bytes()
    if not HEX64.fullmatch(digest) or sha_bytes(raw) != digest:
        fail("current_record_pin_mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict):
        fail("current_record_not_object")
    return value


def current_git(source: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(source), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60).stdout


def current_source(source: Path, head: str, tree: str, gate: dict[str, Any]) -> dict[str, Any]:
    package_plain_path(source)
    if (not re.fullmatch(r"[0-9a-f]{40}", tree)
            or current_git(source, "rev-parse", "HEAD").decode().strip() != head
            or current_git(source, "rev-parse", "HEAD^{tree}").decode().strip() != tree
            or current_git(source, "diff", "--cached", "--name-only").strip()):
        fail("current_source_git_identity_drift")
    files = current_git(source, "ls-files", "-z").decode().rstrip("\0").split("\0")
    committed = current_git(source, "ls-tree", "-rz", "--name-only", "HEAD").decode().rstrip("\0").split("\0")
    if files != committed or len(files) != len(set(files)):
        fail("current_source_index_drift")
    rows = []
    for name in files:
        path = package_plain_path(source / name)
        require_regular(path)
        rows.append([name, sha_file(path)])
    digest = sha_bytes(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode())
    diff = current_git(source, "diff", "--no-ext-diff", "--binary")
    changed = current_git(source, "diff", "--name-only").decode().strip().splitlines()
    if (digest != gate.get("built_source_sha256") or sha_bytes(diff) != gate.get("generated_diff_sha256")
            or changed != gate.get("generated_diff_paths")):
        fail("current_source_gate_drift")
    closures = {}
    for name in ("functions", "src", "data"):
        members = [p.relative_to(source).as_posix() for p in _walk_plain(source / name)[1]]
        if sorted(members) != sorted(f for f in files if f.startswith(name + "/")):
            fail("current_untracked_runtime_source")
        closures[name] = file_closure(source / name)
    if closures["functions"]["file_count"] != 17:
        fail("current_functions_inventory_drift")
    for row in gate.get("authority_files", []):
        if row.get("file") not in ("data/go-runtime-selection.json", "data/go-runtime-promotion-authority.json",
                                    "data/tracking-allowed-hosts.json", "artifacts/ops/build-report.json"):
            fail("current_gate_authority_path_invalid")
        require_regular(package_plain_path(source / row["file"]))
        if sha_file(source / row["file"]) != row["sha256"]:
            fail("current_gate_authority_drift")
    go_source = (source / "functions/go/program-manifest.js").read_text()
    go_match = re.fullmatch(r"// Generated by scripts/build-staging\.mjs\. Do not edit by hand\.\nexport const GO_RUNTIME_MANIFEST = Object\.freeze\((.*)\);\s*", go_source, re.S)
    if go_match is None:
        fail("current_generated_go_manifest_shape")
    go = json.loads(go_match[1])
    if (not HEX64.fullmatch(go["manifest_sha256"])
            or go["internal_fallback"] not in (SITE + "/providers/", SITE + "/best/online-glp-1-providers/")):
        fail("current_generated_go_manifest_identity")
    return {"git_tree": tree, "built_source_sha256": digest, "tracked_files": rows,
            "go_embedded_sha256": go["manifest_sha256"], "go_internal_fallback": go["internal_fallback"],
            "generated_diff_paths": changed, "generated_diff_sha256": sha_bytes(diff),
            "runtime": closures, "authority_files": gate["authority_files"],
            "go_manifest_sha256": sha_file(source / "functions/go/program-manifest.js"),
            "wrangler_config_sha256": sha_file(source / "wrangler.toml")}


def current_pointer() -> dict[str, Any]:
    raw, st = capture_pointer(POINTER)
    parsed = json.loads(raw)
    if parsed.get("deployment_id") != CURRENT_ROLLBACK:
        fail("current_pointer_rollback_mismatch")
    return {"path": str(POINTER), "bytes_base64": base64.b64encode(raw).decode(),
            "sha256": sha_bytes(raw), "bytes": len(raw), "st_dev": st.st_dev, "st_ino": st.st_ino,
            "uid": st.st_uid, "gid": st.st_gid, "mode": f"{_mode(st):04o}", "nlink": st.st_nlink,
            "deployment_id": parsed["deployment_id"], "release_root": parsed["release_root"]}


def current_routes(worker: bytes) -> list[dict[str, str]]:
    # Read the pinned compiler's actual router, not an invented _routes.json.
    blocks = re.findall(rb"var routes = \[\n(.*?)\n\];", worker, re.S)
    if len(blocks) != 1:
        fail("current_compiled_router_shape")
    pattern = (rb'\{\s*routePath: "([^"]+)",\s*mountPath: "([^"]*)",\s*method: "([^"]*)",'
               rb'\s*middlewares: \[([^\]]*)\],\s*modules: \[([^\]]*)\]\s*\}')
    rows = [dict(zip(("route", "mount", "method", "middlewares", "modules"),
                     (v.decode() for v in match))) for match in re.findall(pattern, blocks[0])]
    if (len(rows) != blocks[0].count(b"routePath:") or not rows
            or not any(r["route"] == "/go/:program" and r["modules"] for r in rows)
            or not any(r["route"] == "/api/postback/affiliati" and r["modules"] for r in rows)
            or not any(r["route"] == "/providers" and r["middlewares"] and not r["modules"] for r in rows)
            or b'env["ASSETS"].fetch(request)' not in worker):
        fail("current_compiled_routes_missing")
    return rows


def current_compile(workspace: Path) -> dict[str, Any]:
    pages_out = workspace / ".wrangler/pages-functions-build"
    command = [str(NODE), str(WRANGLER), "pages", "functions", "build", "functions", "--outdir", str(pages_out)]
    proc = subprocess.run(command, cwd=str(workspace), env=wrangler_env(workspace),
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=1200)
    if proc.returncode or any(word in (proc.stdout + proc.stderr).lower() for word in ("download", "registry.npmjs")):
        fail("current_functions_build_failed")
    path = pages_out / "index.js"
    raw = path.read_bytes()
    # Wrangler 4.114.0 getPagesProjectRoot: nearest package.json, else cwd.
    # Its esbuild projectRoot is functions/, not that Pages temporary root.
    project_root = next((p for p in (workspace, *workspace.parents) if (p / "package.json").is_file()), workspace)
    routes_tmp = os.path.relpath(project_root / ".wrangler/tmp", workspace / "functions").encode()
    fixed, count = re.subn(rb"(?m)^// " + re.escape(routes_tmp) + rb"/pages-[^/\r\n]+/functionsRoutes-[^\r\n]+$",
                          b"// r13-canonical-functions-routes.mjs", raw)
    if count != 1:
        fail("current_wrangler_nondeterminism_shape_drift")
    atomic_file(path, fixed)
    return {"argv": command, "returncode": proc.returncode,
            "stdout_sha256": sha_bytes(proc.stdout.encode()), "stderr_sha256": sha_bytes(proc.stderr.encode()),
            "raw_worker_sha256": sha_bytes(raw), "worker_sha256": sha_bytes(fixed),
            "pages_random_comment_replacements": count, "routes": current_routes(fixed)}


def current_deploy_public(deploy: Path, public_rows: list[dict[str, str]], worker_sha256: str) -> None:
    """Require accepted public bytes plus exactly the compiled worker, not a self-recorded closure."""
    rows = package_tree(deploy)[1]
    if ([r for r in rows if r["file"] != "_worker.js"] != public_rows
            or [r for r in rows if r["file"] == "_worker.js"] !=
            [{"file": "_worker.js", "sha256": worker_sha256}]):
        fail("current_deploy_public_package_mismatch")


def run_prepare_current(args: argparse.Namespace) -> dict[str, Any]:
    source, package, output = [package_plain_path(Path(p)) for p in
                               (args.source_root, args.package_dir, args.prepared_dir)]
    for input_path in (source, package, Path(args.build_receipt)):
        a, b = Path("/" + str(input_path).lstrip("/")), Path("/" + str(output).lstrip("/"))
        if a == b or a in b.parents or b in a.parents:
            fail("current_output_overlaps_input")
    if output.exists() or not output.parent.is_dir():
        fail("current_output_must_be_new")
    manifest = current_json(package / "PACKAGE-MANIFEST.json", args.expected_package_manifest_sha256)
    if (manifest.get("schema") != "glp34-deploy-package-v1" or manifest.get("site_head") != args.site_head
            or manifest.get("site_repository") != CURRENT_REPOSITORY or manifest.get("project") != PROJECT
            or manifest.get("account_id") != ACCOUNT_ID or manifest.get("rollback_branch") != PRODUCTION_BRANCH
            or manifest.get("rollback_deployment_id") != CURRENT_ROLLBACK
            or manifest.get("build_receipt", {}).get("sha256") != args.expected_build_receipt_sha256):
        fail("current_package_binding_mismatch")
    args.expected_tree_sha256 = manifest["public_tree_sha256"]
    public = package / "public"
    tree_hash, rows = package_tree(public)
    if (tree_hash != args.expected_tree_sha256 or rows != manifest.get("files")
            or any(r["file"] in ("_worker.js", "_routes.json") for r in rows)):
        fail("current_public_package_drift")
    route_specs = current_route_specs(public)  # Resolve required static files before any effects.
    gate_binding = package_build_receipt(args, rows)
    gate = current_json(Path(args.build_receipt), args.expected_build_receipt_sha256)
    binding = current_source(source, args.site_head, args.git_tree, gate)
    holds = next((s["evidence"]["raw_held"] for s in gate["steps"] if s["id"] == "affiliate-census" and s["status"] == "PASS"), None)
    if (not holds or holds.get("anchors") != [] or holds.get("routes") != [] or not holds.get("held")
            or any(v != 0 for v in holds["counts"].values()) or "fella-health" not in holds["counts"]):
        fail("current_gate_holds_nonpass")
    pointer = current_pointer()
    verify_immutable_tree(TOOLCHAIN, CURRENT_TOOLCHAIN, toolchain=True)
    subprocess.run([sys.executable, str(BUSINESS / "scripts/candidate_build_lock.py"),
                    "require-held", "--seat", args.build_seat], check=True, capture_output=True, timeout=30)
    package_site_commit(args.site_head)
    token = read_token()
    package_rollback(CURRENT_ROLLBACK, token, detail=True)
    output.mkdir(mode=0o700)
    try:
        release, compile_root = output / "release", output / "compile"
        release.mkdir(); compile_root.mkdir()
        for name in dict.fromkeys([r[0] for r in binding["tracked_files"]] + [r["file"] for r in binding["authority_files"]]):
            destination = release / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, destination, follow_symlinks=False)
        for name in ("functions", "src", "data"):
            copytree_exact(source / name, compile_root / name)
        if current_source(source, args.site_head, args.git_tree, gate) != binding:
            fail("current_source_changed_during_copy")
        for name, digest in binding["tracked_files"] + [[r["file"], r["sha256"]] for r in binding["authority_files"]]:
            if sha_file(release / name) != digest:
                fail("current_release_copy_drift")
        compiled = current_compile(compile_root)
        for name, closure in binding["runtime"].items():
            if file_closure(compile_root / name) != closure:
                fail("current_compile_input_drift")
        copytree_exact(public, release / "public")
        deploy = output / "deploy-public"
        copytree_exact(public, deploy)
        shutil.copyfile(compile_root / ".wrangler/pages-functions-build/index.js", deploy / "_worker.js")
        if sha_file(deploy / "_worker.js") != compiled["worker_sha256"]:
            fail("current_worker_copy_drift")
        shutil.rmtree(compile_root)
        exclusive_file(output / "gate.json", Path(args.build_receipt).read_bytes(), 0o444)
        exclusive_file(output / "PACKAGE-MANIFEST.json", (package / "PACKAGE-MANIFEST.json").read_bytes(), 0o444)
        prepared = {"schema": "glp43-current-prepared-v1", "prepared_dir": str(output),
                    "site_head": args.site_head, "site_repository": CURRENT_REPOSITORY,
                    "project": PROJECT, "branch": PRODUCTION_BRANCH, "account_id": ACCOUNT_ID,
                    "public_tree_sha256": tree_hash, "package_manifest_sha256": args.expected_package_manifest_sha256,
                    "build_receipt": gate_binding, "source": binding, "compiled": compiled,
                    "toolchain": CURRENT_TOOLCHAIN, "rollback_deployment_id": CURRENT_ROLLBACK,
                    "predecessor_pointer": pointer, "release": file_closure(release),
                    "deploy_directory": file_closure(deploy), "holds": holds, "route_specs": route_specs,
                    "provider_reads": 3, "provider_mutations": 0, "release_authorized": False}
        package_rollback(CURRENT_ROLLBACK, token, detail=False)
        verify_signed_predecessor(pointer)
        if current_source(source, args.site_head, args.git_tree, gate) != binding or package_tree(public) != (tree_hash, rows):
            fail("current_inputs_changed_before_completion")
        current_deploy_public(deploy, rows, compiled["worker_sha256"])
        raw = json_bytes(prepared)
        exclusive_file(output / "PREPARED.json", raw, 0o444)
        current_prepared(output, sha_bytes(raw))
        return {"status": "PASS_PREPARED_CURRENT", "prepared_dir": str(output),
                "manifest_sha256": sha_bytes(raw), "deploy_directory": prepared["deploy_directory"],
                "compiled_worker_sha256": compiled["worker_sha256"],
                "provider_reads": 3, "provider_mutations": 0, "release_authorized": False}
    except BaseException:
        shutil.rmtree(output)
        raise


def current_prepared(root: Path, digest: str) -> dict[str, Any]:
    value = current_json(root / "PREPARED.json", digest)
    if (value.get("schema") != "glp43-current-prepared-v1" or value.get("prepared_dir") != str(root)
            or value.get("site_repository") != CURRENT_REPOSITORY or value.get("project") != PROJECT
            or value.get("branch") != PRODUCTION_BRANCH or value.get("account_id") != ACCOUNT_ID
            or value.get("rollback_deployment_id") != CURRENT_ROLLBACK or value.get("toolchain") != CURRENT_TOOLCHAIN):
        fail("current_prepared_identity_mismatch")
    if (file_closure(root / "release") != value["release"]
            or file_closure(root / "deploy-public") != value["deploy_directory"]
            or sha_file(root / "deploy-public/_worker.js") != value["compiled"]["worker_sha256"]):
        fail("current_prepared_bytes_drift")
    package = current_json(root / "PACKAGE-MANIFEST.json", value["package_manifest_sha256"])
    current_deploy_public(root / "deploy-public", package["files"], value["compiled"]["worker_sha256"])
    gate = current_json(root / "gate.json", value["build_receipt"]["sha256"])
    if (package_tree(root / "release/public") != (value["public_tree_sha256"], package["files"])
            or current_route_specs(root / "release/public") != value["route_specs"]):
        fail("current_prepared_public_drift")
    return value


def current_approval(args: argparse.Namespace, prepared: dict[str, Any]) -> dict[str, Any]:
    review = current_json(Path(args.approval), args.expected_approval_sha256)
    if (review.get("schema") != "glp42-designated-astra-release-review-v1"
            or review.get("verdict") != "APPROVE" or review.get("release_accepted") is not True
            or review.get("blocking_findings") != []):
        fail("current_release_not_approved")
    reviewer = review.get("reviewer", {})
    if (reviewer.get("model") != "gpt-6-astra-900k" or reviewer.get("provider") != "openai-codex"
            or "not an author" not in reviewer.get("role", "")
            or reviewer.get("authority") != list(CURRENT_AUTHORITIES)[1]
            or reviewer.get("original_target_authority") != list(CURRENT_AUTHORITIES)[0]):
        fail("current_designated_independent_reviewer_required")
    required = {"repository": CURRENT_REPOSITORY, "head": prepared["site_head"],
                "git_tree": prepared["source"]["git_tree"], "public_tree_sha256": prepared["public_tree_sha256"],
                "package_manifest_sha256": prepared["package_manifest_sha256"],
                "project": PROJECT, "rollback_branch": PRODUCTION_BRANCH, "rollback_environment": "production",
                "rollback_deployment_id": CURRENT_ROLLBACK,
                "prepared_manifest_sha256": args.expected_prepared_manifest_sha256,
                "deploy_directory_sha256": prepared["deploy_directory"]["tree_sha256"],
                "compiled_worker_sha256": prepared["compiled"]["worker_sha256"]}
    if any(review.get("subject", {}).get(k) != v for k, v in required.items()):
        fail("current_approval_subject_mismatch")
    if review.get("reused_technical_acceptance", {}).get("gate_sha256") != prepared["build_receipt"]["sha256"]:
        fail("current_approval_gate_mismatch")
    for name, pin in CURRENT_AUTHORITIES.items():
        require_regular(package_plain_path(BUSINESS / name))
        if sha_file(BUSINESS / name) != pin:
            fail("current_operator_authority_drift")
    if not args.authorize_production:
        fail("current_explicit_production_authorization_required")
    return review


class CurrentNoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Read a Location header; never visit a provider/tracking URL.


def current_runtime_readback(deployment: dict[str, Any], prepared: dict[str, Any]) -> list[dict[str, Any]]:
    bases = [deployment["url"].rstrip("/"), f"https://{PROJECT}.pages.dev", SITE]
    paths = ["/go/_manifest", "/best/online-glp-1-providers/", "/api/postback/affiliati", "/providers/?glp43=readback"] + ["/go/" + row["id"] for row in prepared["holds"]["held"]]
    results = []
    for base in bases:
        for path in paths:
            if not re.fullmatch(r"/(?:best/online-glp-1-providers/|go/(?:_manifest|[a-z0-9-]+)|api/postback/affiliati|providers/\?glp43=readback)", path):
                fail("current_runtime_probe_path_invalid")
            method = "GET" if path.startswith("/best/") else "HEAD"
            request = urllib.request.Request(base + path, method=method, headers={"User-Agent": "glp-r13-authority-carrier/3",
                                                                             "Cache-Control": "no-cache"})
            try:
                response = urllib.request.build_opener(CurrentNoRedirect()).open(request, timeout=30)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                status_code, headers = response.code, response.headers
            if path.startswith("/go/"):
                go_status = (status_code == 200 if path == "/go/_manifest" else
                             status_code == 307 and headers.get("Location") == prepared["source"]["go_internal_fallback"])
                ok = (go_status and headers.get("X-GLP-Program-Manifest-SHA256") == prepared["source"]["go_embedded_sha256"]
                      and "no-store" in headers.get("Cache-Control", "") and "noindex" in headers.get("X-Robots-Tag", ""))
            elif path.startswith("/best/"):
                ok = status_code == 301 and headers.get("Location") == base + "/providers/"
            elif path.startswith("/api/"):
                ok = status_code == 405 and "no-store" in headers.get("Cache-Control", "") and "noindex" in headers.get("X-Robots-Tag", "")
            else:
                ok = status_code == 200 and headers.get("X-Robots-Tag") == "noindex, follow"
            if not ok:
                fail("current_runtime_readback_failed:" + path)
            results.append({"base": base, "path": path, "method": method, "status": status_code,
                            "location": headers.get("Location"), "robots": headers.get("X-Robots-Tag"),
                            "go_manifest_sha256": headers.get("X-GLP-Program-Manifest-SHA256"),
                            "cache_control": headers.get("Cache-Control")})
    return results


def current_route_specs(root: Path) -> list[dict[str, Any]]:
    # Bind canonical families, disclosures, held surfaces, runtime assets and sitemap.
    names = ["index.html", "providers/index.html",
             "tools/glp-1-cost-calculator/index.html", "lp/quiz/index.html", "quiz/provider-fit/index.html",
             "reviews/nu-image-medical-review/index.html", "reviews/mydrhank/index.html",
             "privacy/index.html", "privacy/cookies/index.html", "sitemap.xml"]
    names += [r["path"] for r in file_rows(root) if r["path"].startswith("assets/") and r["path"].endswith((".js", ".css"))]
    return [{"url_path": "/" + name.removesuffix("index.html"), "sha256": sha_file(root / name),
             "bytes": (root / name).stat().st_size} for name in names]


# Exact mailto/span reversal plus the retained providers plaintext shape.
# Do not import the historical normalizer's query links or generic cleanup.
CURRENT_CF_EMAIL_ANCHOR_RE = re.compile(
    rb'<a href="/cdn-cgi/l/email-protection#([0-9A-Fa-f]+)"><span class="__cf_email__" data-cfemail="([0-9A-Fa-f]+)">\[email&#160;protected\]</span></a>'
)
CURRENT_CF_EMAIL_PLAIN_RE = re.compile(
    rb'<a href="/cdn-cgi/l/email-protection" class="__cf_email__" data-cfemail="([0-9A-Fa-f]+)">\[email&#160;protected\]</a>'
)
CURRENT_CF_EMAIL_DECODER_SCRIPT = b'<script data-cfasync="false" src="/cdn-cgi/scripts/5c5dd728/cloudflare-static/email-decode.min.js"></script>'


def current_restore_cf_email(value: bytes) -> bytes:
    """Reverse only proven CF shapes; the caller still requires all source bytes."""
    def decode_cloudflare_hex(encoded: bytes, *, plaintext: bool = False) -> bytes:
        if len(encoded) < 4 or len(encoded) % 2:
            fail("cloudflare_email_hex_invalid")
        try:
            raw = bytes.fromhex(encoded.decode("ascii"))
            decoded = bytes(byte ^ raw[0] for byte in raw[1:]).decode("utf-8")
        except (UnicodeDecodeError, ValueError):
            fail("cloudflare_email_decode_invalid")
        # The mailto allowlist is unchanged. Only the exact retained plaintext
        # spelling is supported; this is not a general provider-email decoder.
        allowed = ({"care@BiltRx.com"} if plaintext else
                   {"ops@glpclinicscout.com", "support@glpclinicscout.com", "privacy@glpclinicscout.com"})
        if decoded not in allowed:
            fail("cloudflare_email_unapproved")
        return decoded.encode("ascii")

    def restore_anchor(match: re.Match[bytes]) -> bytes:
        href_address = decode_cloudflare_hex(match.group(1))
        span_address = decode_cloudflare_hex(match.group(2))
        if href_address != span_address:
            fail("cloudflare_email_anchor_span_mismatch")
        return b'<a href="mailto:' + href_address + b'">' + href_address + b'</a>'

    anchors = list(CURRENT_CF_EMAIL_ANCHOR_RE.finditer(value))
    plain = list(CURRENT_CF_EMAIL_PLAIN_RE.finditer(value))
    # Retained providers evidence has two regions flushed at their next original
    # scripts. Privacy has mailto anchors after its last original script; its
    # one decoder instead ends immediately at the actual </body></html> boundary.
    # Neither shape permits arbitrary placement, scripts, or byte changes.
    if plain and (len(plain) != 1 or len(anchors) != 1 or plain[0].end() > anchors[0].start()):
        fail("cloudflare_email_plaintext_shape_invalid")
    restored, count = CURRENT_CF_EMAIL_ANCHOR_RE.subn(restore_anchor, value)
    restored = CURRENT_CF_EMAIL_PLAIN_RE.sub(
        lambda match: decode_cloudflare_hex(match.group(1), plaintext=True), restored)
    script_count = value.count(CURRENT_CF_EMAIL_DECODER_SCRIPT)
    if count or plain:
        boundaries = {value.find(b"<script", match.end()) for match in [*anchors, *plain]}
        if (script_count < 1 or script_count != len(boundaries)
                or (plain and len(boundaries) != 2)):
            fail("cloudflare_email_decoder_script_count_invalid")
        for position in boundaries:
            after = position + len(CURRENT_CF_EMAIL_DECODER_SCRIPT)
            # The only supported terminal insertion is immediately before the
            # literal closing body, with no later original script. Exact source
            # equality after restoration remains the final authority.
            terminal_body = value[after:] == b"</body></html>"
            if (position < 0 or not value.startswith(CURRENT_CF_EMAIL_DECODER_SCRIPT, position)
                    or not (value.startswith((b"<script ", b"<script>"), after) or terminal_body)):
                fail("cloudflare_email_decoder_script_location_invalid")
        restored = restored.replace(CURRENT_CF_EMAIL_DECODER_SCRIPT, b"")
    elif script_count:
        fail("cloudflare_email_decoder_without_anchor")
    if any(marker in restored for marker in (b"/cdn-cgi/l/email-protection", b"__cf_email__",
                                             b"data-cfemail", b"email-decode.min.js")):
        fail("cloudflare_email_transform_unhandled")
    return restored


def current_route_probe(base: str, item: dict[str, Any], public: Path,
                        receipt_path: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    path = item["url_path"]
    if (not isinstance(path, str) or not re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]*", path)
            or any(part in (".", "..") for part in path.split("/"))):
        fail("current_route_path_invalid")
    source = package_plain_path(public / (path.lstrip("/") + ("index.html" if path.endswith("/") else "")))
    require_regular(source)
    expected = source.read_bytes()
    if len(expected) != item["bytes"] or sha_bytes(expected) != item["sha256"]:
        fail("current_route_expected_bytes_drift")
    url = base + path
    request = urllib.request.Request(url, headers={"User-Agent": "glp-r13-authority-carrier/3",
                                                   "Cache-Control": "no-cache"})
    try:
        try:
            response = urllib.request.build_opener(CurrentNoRedirect()).open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            status_code, headers = response.status, response.headers
    except urllib.error.URLError as exc:
        fail(f"route_probe_failed:{url}:{type(exc).__name__}")
    # Use the existing run report + receipt. Save RAW bytes and their digest
    # before status, parsing or comparison can refuse; never overwrite a probe.
    body_path = receipt_path.parent / ("route-probe-" + uuid.uuid4().hex + ".body")
    exclusive_file(body_path, body)
    row = {"url": url, "status": status_code, "bytes": len(body), "body_sha256": sha_bytes(body),
           "body_path": str(body_path), "expected_sha256": item["sha256"], "expected_bytes": item["bytes"],
           "headers": {name: headers.get(name) for name in
                       ("Content-Type", "Cache-Control", "ETag", "Location", "CF-Cache-Status", "Content-Encoding")}}
    receipt["stages"].setdefault("route_probe_raw", []).append(row)
    checkpoint(receipt_path, receipt)
    compared, comparison = body, "raw_exact"
    if (status_code == 200 and body != expected and base == SITE and source.suffix == ".html"
            and headers.get("Content-Type", "").split(";", 1)[0].strip().lower() == "text/html"):
        compared, comparison = current_restore_cf_email(body), "exact_cf_email_reversal"
    if (status_code != 200 or len(compared) != item["bytes"] or sha_bytes(compared) != item["sha256"]
            or compared != expected):
        fail(f"route_probe_invariant_failed:{url}:{status_code}")
    row.update(comparison=comparison, compared_sha256=sha_bytes(compared), compared_bytes=len(compared))
    checkpoint(receipt_path, receipt)
    return row


def current_route_matrix(client: CloudflareClient, deployment: dict[str, Any],
                         route_specs: list[dict[str, Any]], public: Path,
                         receipt_path: Path, receipt: dict[str, Any], *, timeout_seconds: int = 120) -> list[dict[str, Any]]:
    # Carry historical ordering, canonical-head guard and convergence policy.
    # The historical route_matrix/route_probe and their authority pins stay frozen.
    deployment_id = str(deployment.get("id", ""))
    immutable = str(deployment.get("url", "")).rstrip("/")
    if (not DEPLOYMENT_ID.fullmatch(deployment_id) or not immutable.startswith("https://")
            or not immutable.endswith(f".{PROJECT}.pages.dev")):
        fail("deployment_immutable_identity_invalid")
    bases = [immutable, f"https://{PROJECT}.pages.dev", SITE]
    deadline = time.monotonic() + timeout_seconds
    last_error = "route_matrix_not_attempted"
    while True:
        project = client.project()
        canonical_head = production_deployment(project)
        if canonical_head.get("id") != deployment_id:
            fail(f"canonical_head_changed_during_route_matrix:{canonical_head.get('id')}")
        try:
            results = []
            for base in bases:
                for item in route_specs:
                    results.append(current_route_probe(base, item, public, receipt_path, receipt))
            return results
        except GateError as exc:
            last_error = str(exc)
            if time.monotonic() >= deadline:
                fail(f"route_matrix_did_not_converge:{last_error}")
            time.sleep(5)


def current_pointer_intent(root: Path, prepared: dict[str, Any], deployment_id: str, run_dir: Path) -> dict[str, Any]:
    spec = prepared["predecessor_pointer"]
    predecessor = base64.b64decode(spec["bytes_base64"], validate=True)
    manifest_path, terminal_path = run_dir / "01-deployment-manifest.json", run_dir / "10-final-publication-receipt.json"
    manifest = json_bytes({"schema": "glp-opus-release-deployment-manifest-v1", "release_root": str(root / "release"),
                           "release_rows": file_rows(root / "release"), "release_tree_sha256": prepared["release"]["tree_sha256"],
                           "site_head": prepared["site_head"], "deploy_directory": prepared["deploy_directory"]})
    terminal = json_bytes({"schema": "glp-opus-production-terminal-receipt-v1", "status": "PASS_PRODUCTION_TERMINAL",
                           "deployment": {"deployment_id": deployment_id}, "site_head": prepared["site_head"],
                           "evidence_sha256": {manifest_path.name: sha_bytes(manifest)},
                           "immutable_closure": {"release_tree_sha256": prepared["release"]["tree_sha256"],
                                                  "public_tree_sha256": file_closure(root / "release/public")["tree_sha256"]}})
    successor = json.loads(predecessor)
    successor.update({"release_root": str(root / "release"), "deployment_id": deployment_id, "status": "PASS",
                      "manifest": {"path": str(manifest_path), "sha256": sha_bytes(manifest)},
                      "final_receipt": {"path": str(terminal_path), "sha256": sha_bytes(terminal)},
                      "release_tree_sha256": prepared["release"]["tree_sha256"],
                      "public_tree_sha256": file_closure(root / "release/public")["tree_sha256"],
                      "site_head": prepared["site_head"], "deploy_directory_sha256": prepared["deploy_directory"]["tree_sha256"],
                      "supersedes": {"deployment_id": CURRENT_ROLLBACK, "release_root": spec["release_root"]}})
    for key in ("deployment_reconciliation", "gsc_closeout"):
        successor.pop(key, None)
    raw = json_bytes(successor)
    return {"pointer_path": str(POINTER), "lock_path": str(LOCK_PATH),
            "predecessor_bytes_base64": spec["bytes_base64"], "successor_bytes_base64": base64.b64encode(raw).decode(),
            "predecessor_stat": {k: spec[k] for k in ("st_dev", "st_ino", "uid", "gid", "mode", "nlink")},
            "final_mode": spec["mode"], "final_uid": spec["uid"], "final_gid": spec["gid"],
            "temp_name": "." + POINTER.name + ".glp43-" + prepared["site_head"] + ".tmp",
            "artifacts": [{"path": str(path), "mode": "0600", "bytes_base64": base64.b64encode(data).decode()}
                          for path, data in ((manifest_path, manifest), (terminal_path, terminal))]}


def current_deployments(client: CloudflareClient) -> list[dict[str, Any]]:
    """Current reconciliation: server-default page size, all environments.

    Keep the historical reader frozen; retain its full-pagination guards here.
    The provider accepts omitted per_page but rejected the legacy explicit 100.
    """
    deployments = []
    seen = set()
    page = 1
    totals = None
    while True:
        response = client.request("GET", f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}/deployments?page={page}", envelope=True)
        result = response.get("result")
        info = response.get("result_info")
        if not isinstance(result, list) or not isinstance(info, dict):
            fail("deployment_list_shape_invalid")
        pages, count = info.get("total_pages"), info.get("total_count")
        if (type(pages) is not int or type(count) is not int or pages < 0 or count < 0
                or type(info.get("page")) is not int or info["page"] != page):
            fail("deployment_list_pagination_invalid")
        if totals is None:
            totals = (pages, count)
        if totals != (pages, count) or (pages == 0 and (page != 1 or count != 0 or result)):
            fail("deployment_list_changed_during_read")
        for item in result:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or not DEPLOYMENT_ID.fullmatch(item["id"]) or item["id"] in seen
                    or item.get("environment") not in ("production", "preview")):
                fail("deployment_list_identity_invalid")
            seen.add(item["id"])
            deployments.append(item)
        if page >= pages:
            if len(deployments) != count:
                fail("deployment_list_incomplete")
            return deployments
        page += 1


def run_publish_current(args: argparse.Namespace) -> dict[str, Any]:
    root = package_plain_path(Path(args.prepared_dir))
    prepared = current_prepared(root, args.expected_prepared_manifest_sha256)
    current_approval(args, prepared)  # No lock, client, credential, or output before approval.
    verify_immutable_tree(TOOLCHAIN, CURRENT_TOOLCHAIN, toolchain=True)
    lock_fd = acquire_lock()
    try:
        run_dir = SEO_REPORT_ROOT / ("current-release-" + prepared["site_head"])
        receipt_path = run_dir / "CURRENT-RECEIPT.json"
        binding = {"prepared_manifest_sha256": args.expected_prepared_manifest_sha256,
                   "approval_sha256": args.expected_approval_sha256}
        if receipt_path.exists():
            receipt = load_json(receipt_path)
            if receipt.get("binding") != binding or receipt.get("status") != "ACTIVE":
                fail("current_release_consumed_or_binding_changed")
        else:
            verify_signed_predecessor(prepared["predecessor_pointer"])
            receipt = {"schema": "glp43-current-deploy-receipt-v1", "status": "ACTIVE", "binding": binding,
                       "site_head": prepared["site_head"], "rollback_deployment_id": CURRENT_ROLLBACK,
                       "deploy_directory": prepared["deploy_directory"], "stages": {}}
            exclusive_file(receipt_path, json_bytes(receipt))
        stages = receipt["stages"]
        client = CloudflareClient(read_token())
        head = prepared["site_head"]
        current = production_deployment(client.project())
        candidates = [d for d in current_deployments(client) if deployment_commit(d) == head
                      and d.get("environment") == "production" and deployment_branch(d) == PRODUCTION_BRANCH]
        if "rollback_intent" in stages:
            # An uncertain rollback is also reconciliation-only: never replay it.
            if current.get("id") == CURRENT_ROLLBACK and successful_production(current):
                verify_signed_predecessor(prepared["predecessor_pointer"])
                receipt["status"] = "BLOCKED_ROLLED_BACK"
                checkpoint(receipt_path, receipt)
            fail("current_prior_rollback_no_replay")
        if "pages_deploy_intent" not in stages:
            if current.get("id") != CURRENT_ROLLBACK or not successful_production(current) or candidates:
                fail("current_predecessor_or_existing_candidate_mismatch")
            current_prepared(root, args.expected_prepared_manifest_sha256)
            current_approval(args, prepared)
            verify_signed_predecessor(prepared["predecessor_pointer"])
            stages["pages_deploy_intent"] = {"predecessor_id": CURRENT_ROLLBACK, "commit_hash": head, "created_at_utc": utc()}
            checkpoint(receipt_path, receipt)
            immediate = production_deployment(client.project())
            if immediate.get("id") != CURRENT_ROLLBACK or not successful_production(immediate):
                fail("current_canonical_changed_before_publish")
            current_prepared(root, args.expected_prepared_manifest_sha256)
            cwd = run_dir / "deploy-cwd"
            cwd.mkdir(exist_ok=True)
            command = [str(NODE), str(WRANGLER), "pages", "deploy", str(root / "deploy-public"),
                       "--project-name", PROJECT, "--branch", PRODUCTION_BRANCH, "--commit-hash", head,
                       "--commit-message", "GLP43 approved current package", "--no-bundle"]
            try:
                proc = subprocess.run(command, cwd=str(cwd), env=wrangler_env(run_dir, client.token),
                                      stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      text=True, timeout=1800)
                stages["pages_deploy_cli"] = {"returncode": proc.returncode, "stdout_sha256": sha_bytes(proc.stdout.encode()),
                                               "stderr_sha256": sha_bytes(proc.stderr.encode())}
            except (OSError, subprocess.SubprocessError) as exc:
                stages["pages_deploy_cli"] = {"ambiguous": type(exc).__name__}
            checkpoint(receipt_path, receipt)
        # Same historical no-replay rule: every existing intent reconciles only.
        current = production_deployment(client.project())
        candidates = [d for d in current_deployments(client) if deployment_commit(d) == head
                      and d.get("environment") == "production" and deployment_branch(d) == PRODUCTION_BRANCH]
        if (len(candidates) != 1 or current.get("id") != candidates[0].get("id")
                or deployment_commit(current) != head or not successful_production(current)
                or current.get("id") == CURRENT_ROLLBACK):
            fail("current_deploy_outcome_unsettled_reconcile_only")
        deployment_id = current["id"]
        stages["pages_deployment"] = {"deployment_id": deployment_id, "commit_hash": head}
        checkpoint(receipt_path, receipt)
        current_posteffect(args, root, prepared, run_dir, receipt_path, receipt, client, current, lock_fd)
        receipt["status"] = "PASS_PRODUCTION_TERMINAL"
        receipt["effects"] = {"pages_deployments": 1, "pages_rollbacks": 0, "pointer_atomic_cas": 1,
                              "worker_deploys": 0, "d1_mutations": 0, "cron_mutations": 0, "email_sends": 0}
        checkpoint(receipt_path, receipt)
        return {"status": receipt["status"], "receipt": str(receipt_path), "receipt_sha256": sha_file(receipt_path),
                "deployment_id": deployment_id, "site_head": head}
    finally:
        os.close(lock_fd)


def current_posteffect(args: argparse.Namespace, root: Path, prepared: dict[str, Any], run_dir: Path,
                       receipt_path: Path, receipt: dict[str, Any], client: CloudflareClient,
                       current: dict[str, Any], lock_fd: int) -> None:
    # Exact dedented former publish block: same oracle, native CAS and rollback.
    stages, deployment_id, head = receipt["stages"], current["id"], prepared["site_head"]
    try:
        stages["route_probes"] = current_route_matrix(client, current, prepared["route_specs"],
                                                      root / "release/public", receipt_path, receipt)
        stages["runtime_and_holds"] = current_runtime_readback(current, prepared)
        current_prepared(root, args.expected_prepared_manifest_sha256)
        intent = current_pointer_intent(root, prepared, deployment_id, run_dir)
        if "pointer_intent" in stages and stages["pointer_intent"] != intent:
            fail("current_pointer_intent_drift")
        stages["pointer_intent"] = intent
        checkpoint(receipt_path, receipt)
        if production_deployment(client.project()).get("id") != deployment_id:
            fail("current_canonical_changed_before_pointer")
        stages["pointer_cas"] = atomic_pointer_cas(intent, lock_fd, production=True)
        if production_deployment(client.project()).get("id") != deployment_id:
            fail("current_canonical_changed_after_pointer")
        raw = base64.b64decode(intent["successor_bytes_base64"], validate=True)
        _pointer_exact(POINTER, raw, int(intent["final_mode"], 8), intent["final_uid"], intent["final_gid"])
        stages["canonical_pointer_readback"] = {"deployment_id": deployment_id, "site_head": head, "pointer_sha256": sha_bytes(raw)}
    except Exception:
        pointer, _ = capture_pointer(POINTER)
        if json.loads(pointer).get("deployment_id") == deployment_id:
            # Never roll back the provider behind an already advanced pointer.
            receipt["status"] = "BLOCKED_POST_CAS_VALIDATION"
            checkpoint(receipt_path, receipt)
            raise
        if production_deployment(client.project()).get("id") != deployment_id:
            fail("current_foreign_head_no_rollback")
        verify_signed_predecessor(prepared["predecessor_pointer"])
        stages["rollback_intent"] = {"deployment_id": CURRENT_ROLLBACK}
        checkpoint(receipt_path, receipt)
        try:
            client.rollback(CURRENT_ROLLBACK)
        finally:
            restored = production_deployment(client.project())
            if restored.get("id") == CURRENT_ROLLBACK and successful_production(restored):
                verify_signed_predecessor(prepared["predecessor_pointer"])
                receipt["status"] = "BLOCKED_ROLLED_BACK"
                checkpoint(receipt_path, receipt)
        raise


def current_promotion_source(args: argparse.Namespace, prepared: dict[str, Any], path: Path) -> dict[str, Any]:
    # The original consumed upload receipt is input only, never a writable target.
    original = current_json(path, args.expected_rolled_back_receipt_sha256)
    binding = {"prepared_manifest_sha256": args.expected_prepared_manifest_sha256,
               "approval_sha256": args.expected_approval_sha256}
    stages = original.get("stages", {})
    if (not args.authorize_existing_promotion or not re.fullmatch(r"[0-9a-f]{40}", args.site_head)
            or args.site_head != prepared["site_head"] or not DEPLOYMENT_ID.fullmatch(args.candidate_deployment_id)
            or args.candidate_deployment_id == CURRENT_ROLLBACK
            or original.get("schema") != "glp43-current-deploy-receipt-v1"
            or original.get("status") != "BLOCKED_ROLLED_BACK" or original.get("binding") != binding
            or original.get("site_head") != args.site_head or original.get("rollback_deployment_id") != CURRENT_ROLLBACK
            or original.get("deploy_directory") != prepared["deploy_directory"]
            or stages.get("pages_deploy_cli", {}).get("returncode") != 0
            or stages.get("pages_deploy_intent", {}).get("commit_hash") != args.site_head
            or stages.get("pages_deploy_intent", {}).get("predecessor_id") != CURRENT_ROLLBACK
            or stages.get("pages_deployment") != {"deployment_id": args.candidate_deployment_id, "commit_hash": args.site_head}
            or stages.get("rollback_intent") != {"deployment_id": CURRENT_ROLLBACK}
            or "pointer_intent" in stages or "pointer_cas" in stages
            or prepared["predecessor_pointer"].get("deployment_id") != CURRENT_ROLLBACK):
        fail("current_promotion_source_or_authorization_mismatch")
    return {**binding, "original_receipt_sha256": args.expected_rolled_back_receipt_sha256,
            "candidate_deployment_id": args.candidate_deployment_id, "site_head": args.site_head,
            "predecessor_id": CURRENT_ROLLBACK, "predecessor_pointer_sha256": prepared["predecessor_pointer"]["sha256"]}


# The second forward edge is bound to the one actual rolled-back successor.
# These are not caller-supplied journal names or a recursive renewal scheme.
CURRENT_PROMOTION_ROOT_SHA256 = "162966b57bf902f14dce9dc43a13d3c639f77af4f35fd7f375dadbda3bb7d5e6"
CURRENT_PROMOTION_ROLLED_BACK_SUCCESSOR_SHA256 = "3d00967317267cb5b7bbb0925ad86d0809c4fd70cd6a3b25b44dc8548226377b"

def current_promotion_terminal_source(args: argparse.Namespace, binding: dict[str, Any],
                                      original_path: Path, prior_path: Path,
                                      *, expected_sha: str | None = None) -> dict[str, Any]:
    # Original terminal -> first successor, or the fixed authenticated second edge.
    prior_sha = expected_sha if expected_sha is not None else args.expected_terminal_promotion_sha256
    if (not args.authorize_renewed_promotion or not isinstance(prior_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", prior_sha)):
        fail("current_promotion_renewal_authorization_missing")
    prior = current_json(prior_path, prior_sha)
    stages, effects = prior.get("stages"), prior.get("effects")
    selection = {"deployment_id": args.candidate_deployment_id, "commit_hash": args.site_head,
                 "predecessor_id": CURRENT_ROLLBACK,
                 "original_receipt_sha256": args.expected_rolled_back_receipt_sha256,
                 "effect": "select_existing_successful_production_deployment"}
    if (prior.get("schema") != "glp43-current-promotion-receipt-v1"
            or prior.get("status") != "BLOCKED_ROLLED_BACK" or prior.get("binding") != binding
            or prior.get("original_receipt") != str(original_path) or prior.get("site_head") != args.site_head
            or not isinstance(stages, dict) or not isinstance(effects, dict)
            or stages.get("existing_candidate_selection_intent") != selection
            or stages.get("existing_candidate_selection_result") != {"returned": True}
            or stages.get("existing_candidate_selection_confirmed") != {
                "deployment_id": args.candidate_deployment_id, "commit_hash": args.site_head}
            or stages.get("rollback_intent") != {"deployment_id": CURRENT_ROLLBACK}
            or "pointer_intent" in stages or "pointer_cas" in stages
            or effects != {
                "pages_deployments": 0, "existing_candidate_selection_post_intents": 1,
                "existing_candidate_selections_confirmed": 1, "guarded_rollback_post_intents": 1,
                "guarded_rollbacks_confirmed": 1, "pointer_atomic_cas": 0}):
        fail("current_promotion_terminal_preimage_mismatch")
    return {**binding, "terminal_promotion_sha256": prior_sha}

def current_promotion_rolled_back_successor_source(args: argparse.Namespace, binding: dict[str, Any],
                                                    original_path: Path, root_path: Path,
                                                    prior_path: Path) -> dict[str, Any]:
    if (not args.authorize_rolled_back_successor_renewal
            or args.expected_terminal_promotion_sha256 != CURRENT_PROMOTION_ROLLED_BACK_SUCCESSOR_SHA256):
        fail("current_promotion_second_renewal_authorization_missing")
    root_binding = current_promotion_terminal_source(
        args, binding, original_path, root_path, expected_sha=CURRENT_PROMOTION_ROOT_SHA256)
    return current_promotion_terminal_source(args, root_binding, original_path, prior_path)


def run_promote_current_existing(args: argparse.Namespace) -> dict[str, Any]:
    root = package_plain_path(Path(args.prepared_dir))
    prepared = current_prepared(root, args.expected_prepared_manifest_sha256)
    current_approval(args, prepared)
    run_dir = SEO_REPORT_ROOT / ("current-release-" + prepared["site_head"])
    original_path = run_dir / "CURRENT-RECEIPT.json"
    binding = current_promotion_source(args, prepared, original_path)
    renewing = (args.authorize_renewed_promotion or args.expected_terminal_promotion_sha256 is not None
                or args.authorize_rolled_back_successor_renewal)
    second_renewal = args.authorize_rolled_back_successor_renewal
    root_path = run_dir / ("CURRENT-PROMOTION-" + args.candidate_deployment_id + ".json")
    prior_path = (run_dir / ("CURRENT-PROMOTION-" + args.candidate_deployment_id + "-SUCCESSOR-"
                             + CURRENT_PROMOTION_ROOT_SHA256 + ".json") if second_renewal else root_path)
    if second_renewal:
        binding = current_promotion_rolled_back_successor_source(args, binding, original_path, root_path, prior_path)
    elif renewing:
        binding = current_promotion_terminal_source(args, binding, original_path, prior_path)
    verify_immutable_tree(TOOLCHAIN, CURRENT_TOOLCHAIN, toolchain=True)
    lock_fd = acquire_lock()
    try:
        def recheck_source() -> None:
            source = current_promotion_source(args, prepared, original_path)
            if second_renewal:
                current_promotion_rolled_back_successor_source(args, source, original_path, root_path, prior_path)
            elif renewing:
                current_promotion_terminal_source(args, source, original_path, prior_path)

        recheck_source()  # full root and prior lineage while holding the native lock
        deployment_id, head = args.candidate_deployment_id, args.site_head
        receipt_path = (run_dir / ("CURRENT-PROMOTION-" + deployment_id + "-SUCCESSOR-"
                                   + binding["terminal_promotion_sha256"] + ".json") if renewing else prior_path)
        selection = {"deployment_id": deployment_id, "commit_hash": head, "predecessor_id": CURRENT_ROLLBACK,
                     "original_receipt_sha256": args.expected_rolled_back_receipt_sha256,
                     "effect": "select_existing_successful_production_deployment"}
        recheck_source()
        client = CloudflareClient(read_token())
        if receipt_path.exists():
            require_regular(package_plain_path(receipt_path))
            receipt = load_json(receipt_path)
            if (receipt.get("schema") != "glp43-current-promotion-receipt-v1" or receipt.get("binding") != binding
                    or receipt.get("status") != "ACTIVE"
                    or receipt.get("stages", {}).get("existing_candidate_selection_intent") != selection):
                fail("current_promotion_consumed_or_binding_changed")
        else:
            candidate = client.request("GET", f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}/deployments/{deployment_id}")
            current = production_deployment(client.project())
            if (not isinstance(candidate, dict) or candidate.get("id") != deployment_id
                    or deployment_commit(candidate) != head or not successful_production(candidate)
                    or current.get("id") != CURRENT_ROLLBACK or not successful_production(current)):
                fail("current_promotion_predecessor_or_candidate_mismatch")
            current_prepared(root, args.expected_prepared_manifest_sha256)
            current_approval(args, prepared)
            recheck_source()
            verify_signed_predecessor(prepared["predecessor_pointer"])
            receipt = {"schema": "glp43-current-promotion-receipt-v1", "status": "ACTIVE", "binding": binding,
                       "original_receipt": str(original_path), "site_head": head,
                       "stages": {"existing_candidate_selection_intent": selection},
                       "effects": {"pages_deployments": 0, "existing_candidate_selection_post_intents": 1,
                                   "existing_candidate_selections_confirmed": None, "guarded_rollback_post_intents": 0,
                                   "guarded_rollbacks_confirmed": 0, "pointer_atomic_cas": 0}}
            exclusive_file(receipt_path, json_bytes(receipt))
            # Only this exclusive creation permits the one selection POST.
            # Cloudflare documents /rollback as selecting a prior successful
            # production deployment; it uploads/builds nothing.
            immediate = production_deployment(client.project())
            if immediate.get("id") != CURRENT_ROLLBACK or not successful_production(immediate):
                fail("current_canonical_changed_before_promotion")
            current_prepared(root, args.expected_prepared_manifest_sha256)
            current_approval(args, prepared)
            recheck_source()
            if renewing:
                # An old candidate read is not a license for another POST.
                candidate = client.request("GET", f"/accounts/{ACCOUNT_ID}/pages/projects/{PROJECT}/deployments/{deployment_id}")
                if (not isinstance(candidate, dict) or candidate.get("id") != deployment_id
                        or deployment_commit(candidate) != head or not successful_production(candidate)):
                    fail("current_promotion_candidate_changed_before_selection")
            verify_signed_predecessor(prepared["predecessor_pointer"])
            try:
                client.rollback(deployment_id)
                receipt["stages"]["existing_candidate_selection_result"] = {"returned": True}
            except Exception as exc:
                receipt["stages"]["existing_candidate_selection_result"] = {"ambiguous": type(exc).__name__}
            checkpoint(receipt_path, receipt)
        stages = receipt["stages"]
        try:
            current = production_deployment(client.project())
            if "rollback_intent" in stages:
                # An uncertain guarded rollback also cannot be repeated.
                if current.get("id") == CURRENT_ROLLBACK and successful_production(current):
                    verify_signed_predecessor(prepared["predecessor_pointer"])
                    receipt["status"] = "BLOCKED_ROLLED_BACK"
                fail("current_prior_rollback_no_replay")
            if (current.get("id") != deployment_id or deployment_commit(current) != head
                    or not successful_production(current)):
                fail("current_promotion_outcome_unsettled_reconcile_only")
            stages["existing_candidate_selection_confirmed"] = {"deployment_id": deployment_id, "commit_hash": head}
            checkpoint(receipt_path, receipt)
            current_posteffect(args, root, prepared, run_dir, receipt_path, receipt, client, current, lock_fd)
            receipt["status"] = "PASS_PRODUCTION_TERMINAL"
        finally:
            # Intent counts are upper bounds on POSTs, not fabricated outcomes.
            # Unknown completion remains null until canonical/CAS readback.
            receipt["effects"] = {"pages_deployments": 0, "existing_candidate_selection_post_intents": 1,
                                  "existing_candidate_selections_confirmed": 1 if "existing_candidate_selection_confirmed" in stages else None,
                                  "guarded_rollback_post_intents": int("rollback_intent" in stages),
                                  "guarded_rollbacks_confirmed": (1 if receipt["status"] == "BLOCKED_ROLLED_BACK"
                                                                 else None if "rollback_intent" in stages else 0),
                                  "pointer_atomic_cas": 1 if "pointer_cas" in stages else None if "pointer_intent" in stages else 0}
            checkpoint(receipt_path, receipt)
        return {"status": receipt["status"], "receipt": str(receipt_path), "receipt_sha256": sha_file(receipt_path),
                "deployment_id": deployment_id, "site_head": head}
    finally:
        os.close(lock_fd)


def current_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="GLP43 exact current package: offline prepare OR explicitly approved production consumption")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare-current", action="store_true")
    modes.add_argument("--publish-current", action="store_true")
    modes.add_argument("--promote-current-existing", action="store_true")
    parser.add_argument("--prepared-dir", required=True)
    preparing = "--prepare-current" in argv
    flags = (("source-root", "site-head", "git-tree", "build-seat", "package-dir", "expected-package-manifest-sha256",
              "build-receipt", "expected-build-receipt-sha256") if preparing else
             ("expected-prepared-manifest-sha256", "approval", "expected-approval-sha256"))
    for flag in flags:
        parser.add_argument("--" + flag, required=True)
    if not preparing:
        parser.add_argument("--authorize-production", action="store_true")
    if "--promote-current-existing" in argv:
        for flag in ("site-head", "candidate-deployment-id", "expected-rolled-back-receipt-sha256"):
            parser.add_argument("--" + flag, required=True)
        parser.add_argument("--authorize-existing-promotion", action="store_true")
        parser.add_argument("--authorize-renewed-promotion", action="store_true")
        parser.add_argument("--authorize-rolled-back-successor-renewal", action="store_true")
        parser.add_argument("--expected-terminal-promotion-sha256")
    args = parser.parse_args(argv)
    try:
        runner = (run_prepare_current if preparing else run_promote_current_existing
                  if args.promote_current_existing else run_publish_current)
        result = runner(args)
        print(json.dumps(result, sort_keys=True, indent=2))
        return 0
    except (GateError, OSError, ValueError, TypeError, AttributeError, KeyError, subprocess.SubprocessError) as exc:
        error = str(exc) if isinstance(exc, GateError) else type(exc).__name__
        print(json.dumps({"status": "BLOCK", "error": error}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
