#!/usr/bin/env python3
"""Verified-byte native carrier for the independent F13 growth-retention lane."""
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"F13 Growth Retention","output_class":"alert","composer_type":"no_agent_script_discord_fmt","privacy_level":"private-local-only","fixture":"⚠️ **F13 Growth Retention** — retention run stopped before terminal proof\n\nremoved files: **8,827** · archive files: **2** · status: **BLOCKED**\n→ inspect the private run receipt before the next scheduled pass; no safety gate was bypassed\n-# receipt: /home/ubuntu/business/archive/f13-growth-retention/run-YYYYMMDDTHHMMSSZ.json","fixture_output_class":"alert","fixture_test":"python3 -m pytest -q tests/test_f13_growth_retention.py tests/test_reports_archiver_process_snapshot_reuse.py","route":"1487300132054765629","detail_policy":"complete_summary_in_discord","success_silence":false}
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import types
from pathlib import Path
from typing import Any

PAYLOAD = Path("/home/ubuntu/business/scripts/f13_growth_retention.py")
EXPECTED_PAYLOAD_SHA256 = "0fee03479abf2d28d80217bfa6e9c3bec0e663268a280c1049e3875425949819"
REPORTS_ARCHIVER = Path("/home/ubuntu/business/scripts/reports_archiver.py")
EXPECTED_REPORTS_ARCHIVER_SHA256 = "ef09a4ab7b1cb4119340c233e07422a3702cc9bcef0583371fc316823a6462cf"
R2_OFFLOAD = Path("/home/ubuntu/business/scripts/f13_r2_sigv4.py")
EXPECTED_R2_OFFLOAD_SHA256 = "802f88d0fa273f22de6f87c2c0643f6eeef56cfd5fcfe10631959059ff76b553"
SCHEMA = Path("/home/ubuntu/business/scripts/glp_current_content_base_schema.py")
EXPECTED_SCHEMA_SHA256 = "a2a65183b8f501213e9f03730a64101e141c79c43035009a7bf7b28c0a676e46"
OPS_ALERT = Path("/home/ubuntu/.hermes/scripts/ops_alert.py")
EXPECTED_OPS_ALERT_SHA256 = "a9e0707b0c7e78c30414f70dd2895b8bed40c43c46528487933f04966b39599b"
GIT_EXECUTABLE = Path("/usr/bin/git")
EXPECTED_GIT_SHA256 = "2a8c18fbf43da9f692d75474c72bea9dfd796c260b0f3dfe456376abc3bbd668"
EXPECTED_GIT_UID = 0
EXPECTED_GIT_GID = 0
EXPECTED_GIT_MODE = 0o755
EXPECTED_GIT_NLINK = 1
TAR_EXECUTABLE = Path("/usr/bin/tar")
EXPECTED_TAR_SHA256 = "3ee2c3c0b4dd9aacebfd2f0fbae44bad36348203acff78a44888dd58c05f811c"
EXPECTED_TAR_UID = 0
EXPECTED_TAR_GID = 0
EXPECTED_TAR_MODE = 0o755
EXPECTED_TAR_NLINK = 1
PIGZ_EXECUTABLE = Path("/usr/bin/pigz")
EXPECTED_PIGZ_SHA256 = "53528c9cc662ccb6f7ca19c5b9a1e2823fd1f055136ad6706d67429d9be34824"
EXPECTED_PIGZ_UID = 0
EXPECTED_PIGZ_GID = 0
EXPECTED_PIGZ_MODE = 0o755
EXPECTED_PIGZ_NLINK = 2
ARCHIVE_ROOT = "/home/ubuntu/business/archive/f13-growth-retention"
CHUNK = 1024 * 1024
MAX_SOURCE_BYTES = 10 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 64 * 1024 * 1024


class ExecutableIntegrityError(RuntimeError):
    """An executable source failed canonical metadata, identity, or hash checks."""


def _source_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_uid,
        value.st_gid,
        stat.S_IMODE(value.st_mode),
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
    )


def _bound_source_path(
    path: Path,
    *,
    label: str,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
    expected_mode: int | None = None,
    expected_nlink: int = 1,
    max_bytes: int = MAX_SOURCE_BYTES,
) -> os.stat_result:
    if not path.is_absolute() or Path(os.path.normpath(str(path))) != path:
        raise ExecutableIntegrityError(f"{label}_invalid_canonical_path")
    current = Path(path.anchor)
    value: os.stat_result | None = None
    for component in path.parts[1:]:
        current /= component
        try:
            value = current.lstat()
        except OSError as exc:
            raise ExecutableIntegrityError(f"{label}_invalid") from exc
        if stat.S_ISLNK(value.st_mode):
            raise ExecutableIntegrityError(f"{label}_invalid_symlink")
        if current != path and not stat.S_ISDIR(value.st_mode):
            raise ExecutableIntegrityError(f"{label}_invalid_parent")
    if value is None or not stat.S_ISREG(value.st_mode):
        raise ExecutableIntegrityError(f"{label}_regular_file_required")
    required_uid = os.geteuid() if expected_uid is None else expected_uid
    required_gid = os.getegid() if expected_gid is None else expected_gid
    if value.st_uid != required_uid or value.st_gid != required_gid:
        raise ExecutableIntegrityError(f"{label}_owner_invalid")
    mode = stat.S_IMODE(value.st_mode)
    if expected_mode is not None and mode != expected_mode:
        raise ExecutableIntegrityError(f"{label}_mode_invalid")
    if mode & 0o022:
        raise ExecutableIntegrityError(f"{label}_group_or_world_writable")
    if value.st_nlink != expected_nlink:
        raise ExecutableIntegrityError(f"{label}_link_count_invalid")
    if value.st_size > max_bytes:
        raise ExecutableIntegrityError(f"{label}_oversized")
    return value


def _read_verified_source(
    path: Path,
    expected_sha256: str,
    *,
    label: str,
    expected_uid: int | None = None,
    expected_gid: int | None = None,
) -> bytes:
    before = _bound_source_path(
        path,
        label=label,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
    )
    identity = _source_identity(before)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExecutableIntegrityError(f"{label}_open_invalid") from exc
    payload = bytearray()
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _source_identity(opened) != identity:
            raise ExecutableIntegrityError(f"{label}_identity_drift")
        while True:
            chunk = os.read(descriptor, CHUNK)
            if not chunk:
                break
            payload.extend(chunk)
            digest.update(chunk)
            if len(payload) > MAX_SOURCE_BYTES:
                raise ExecutableIntegrityError(f"{label}_oversized")
        final = os.fstat(descriptor)
        try:
            path_after = path.lstat()
        except OSError as exc:
            raise ExecutableIntegrityError(f"{label}_identity_drift") from exc
        if (
            _source_identity(final) != identity
            or _source_identity(path_after) != identity
            or len(payload) != before.st_size
        ):
            raise ExecutableIntegrityError(f"{label}_identity_drift")
        if digest.hexdigest() != expected_sha256:
            raise ExecutableIntegrityError(f"{label}_sha256_mismatch")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _open_verified_executable(
    path: Path,
    expected_sha256: str,
    *,
    label: str,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
    expected_nlink: int,
) -> tuple[int, tuple[int, ...]]:
    before = _bound_source_path(
        path,
        label=label,
        expected_uid=expected_uid,
        expected_gid=expected_gid,
        expected_mode=expected_mode,
        expected_nlink=expected_nlink,
        max_bytes=MAX_EXECUTABLE_BYTES,
    )
    if not (stat.S_IMODE(before.st_mode) & 0o111):
        raise ExecutableIntegrityError(f"{label}_not_executable")
    identity = _source_identity(before)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExecutableIntegrityError(f"{label}_open_invalid") from exc
    digest = hashlib.sha256()
    total = 0
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _source_identity(opened) != identity:
            raise ExecutableIntegrityError(f"{label}_identity_drift")
        while True:
            chunk = os.read(descriptor, CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total > MAX_EXECUTABLE_BYTES:
                raise ExecutableIntegrityError(f"{label}_oversized")
        final = os.fstat(descriptor)
        path_after = path.lstat()
        if (
            _source_identity(final) != identity
            or _source_identity(path_after) != identity
            or total != before.st_size
        ):
            raise ExecutableIntegrityError(f"{label}_identity_drift")
        if digest.hexdigest() != expected_sha256:
            raise ExecutableIntegrityError(f"{label}_sha256_mismatch")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _install_verified_module(
    module_name: str,
    source: bytes,
    path: Path,
    *,
    injected: dict[str, Any] | None = None,
) -> types.ModuleType:
    """Install one dependency from retained bytes; never invoke a path loader."""

    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = ""
    module.__cached__ = None
    if injected:
        module.__dict__.update(injected)
    sys.modules[module_name] = module
    try:
        exec(compile(source, str(path), "exec"), module.__dict__, module.__dict__)
    except BaseException:
        if sys.modules.get(module_name) is module:
            sys.modules.pop(module_name, None)
        raise
    return module


def _execute_payload(
    source: bytes,
    path: Path,
    *,
    injected: dict[str, Any] | None = None,
) -> None:
    namespace = {
        "__name__": "__main__",
        "__file__": str(path),
        "__package__": None,
        "__cached__": None,
    }
    if injected:
        namespace.update(injected)
    exec(compile(source, str(path), "exec"), namespace, namespace)


def main() -> int:
    """Verify every executable source before compiling or executing any of it."""

    git_descriptor = -1
    git_identity: tuple[int, ...] | None = None
    tar_descriptor = -1
    tar_identity: tuple[int, ...] | None = None
    pigz_descriptor = -1
    pigz_identity: tuple[int, ...] | None = None
    try:
        payload_source = _read_verified_source(
            PAYLOAD, EXPECTED_PAYLOAD_SHA256, label="f13_growth_payload"
        )
        reports_source = _read_verified_source(
            REPORTS_ARCHIVER,
            EXPECTED_REPORTS_ARCHIVER_SHA256,
            label="f13_growth_reports_archiver",
        )
        r2_source = _read_verified_source(
            R2_OFFLOAD,
            EXPECTED_R2_OFFLOAD_SHA256,
            label="f13_growth_r2_offload",
        )
        schema_source = _read_verified_source(
            SCHEMA,
            EXPECTED_SCHEMA_SHA256,
            label="f13_growth_current_content_schema",
        )
        ops_alert_source = _read_verified_source(
            OPS_ALERT,
            EXPECTED_OPS_ALERT_SHA256,
            label="f13_growth_ops_alert",
        )
        git_descriptor, git_identity = _open_verified_executable(
            GIT_EXECUTABLE,
            EXPECTED_GIT_SHA256,
            label="f13_growth_git_executable",
            expected_uid=EXPECTED_GIT_UID,
            expected_gid=EXPECTED_GIT_GID,
            expected_mode=EXPECTED_GIT_MODE,
            expected_nlink=EXPECTED_GIT_NLINK,
        )
        tar_descriptor, tar_identity = _open_verified_executable(
            TAR_EXECUTABLE,
            EXPECTED_TAR_SHA256,
            label="f13_growth_tar_executable",
            expected_uid=EXPECTED_TAR_UID,
            expected_gid=EXPECTED_TAR_GID,
            expected_mode=EXPECTED_TAR_MODE,
            expected_nlink=EXPECTED_TAR_NLINK,
        )
        pigz_descriptor, pigz_identity = _open_verified_executable(
            PIGZ_EXECUTABLE,
            EXPECTED_PIGZ_SHA256,
            label="f13_growth_pigz_executable",
            expected_uid=EXPECTED_PIGZ_UID,
            expected_gid=EXPECTED_PIGZ_GID,
            expected_mode=EXPECTED_PIGZ_MODE,
            expected_nlink=EXPECTED_PIGZ_NLINK,
        )
    except ExecutableIntegrityError as exc:
        for descriptor in (git_descriptor, tar_descriptor, pigz_descriptor):
            if descriptor >= 0:
                os.close(descriptor)
        print(
            json.dumps(
                {
                    "schema": "f13-growth-retention-wrapper-terminal-v1",
                    "status": "BLOCKED",
                    "reason": str(exc),
                    "exit_code": 2,
                    "effects": {
                        "executable_modules_installed": 0,
                        "payload_executed": 0,
                    },
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        return 2

    # No executable bytes are compiled until every pathname/metadata/identity/
    # hash binding above has succeeded. Retained descriptors remain open through
    # payload completion; tar and pigz run only via /proc/<wrapper-pid>/fd.
    assert git_descriptor >= 0 and git_identity is not None
    assert tar_descriptor >= 0 and tar_identity is not None
    assert pigz_descriptor >= 0 and pigz_identity is not None
    try:
        os.environ.setdefault("F12_ARCHIVE_ROOT", ARCHIVE_ROOT)
        _install_verified_module(
            "glp_current_content_base_schema",
            schema_source,
            SCHEMA,
        )
        _install_verified_module("r2_offload", r2_source, R2_OFFLOAD)
        _install_verified_module(
            "reports_archiver",
            reports_source,
            REPORTS_ARCHIVER,
            injected={
                "_F12_WRAPPER_VERIFIED_OPS_ALERT_SOURCE": ops_alert_source,
                "_F12_WRAPPER_VERIFIED_OPS_ALERT_PATH": OPS_ALERT,
            },
        )
        _execute_payload(
            payload_source,
            PAYLOAD,
            injected={
                "_F13_WRAPPER_VERIFIED_GIT_FD": git_descriptor,
                "_F13_WRAPPER_VERIFIED_GIT_IDENTITY": git_identity,
                "_F13_WRAPPER_VERIFIED_GIT_SHA256": EXPECTED_GIT_SHA256,
                "_F13_WRAPPER_VERIFIED_TAR_FD": tar_descriptor,
                "_F13_WRAPPER_VERIFIED_TAR_IDENTITY": tar_identity,
                "_F13_WRAPPER_VERIFIED_TAR_SHA256": EXPECTED_TAR_SHA256,
                "_F13_WRAPPER_VERIFIED_PIGZ_FD": pigz_descriptor,
                "_F13_WRAPPER_VERIFIED_PIGZ_IDENTITY": pigz_identity,
                "_F13_WRAPPER_VERIFIED_PIGZ_SHA256": EXPECTED_PIGZ_SHA256,
            },
        )
        return 0
    finally:
        for descriptor in (git_descriptor, tar_descriptor, pigz_descriptor):
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
