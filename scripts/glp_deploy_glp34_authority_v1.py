#!/usr/bin/env python3
"""Authority-bound one-time GLP34 carrier (r1 / v1).

The only accepted modes are ``--verify-only`` and ``--live``.  Verify-only runs
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


def main(argv: list[str] | None = None) -> int:
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


if __name__ == "__main__":
    raise SystemExit(main())
