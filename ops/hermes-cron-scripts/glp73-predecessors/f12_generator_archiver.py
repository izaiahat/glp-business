#!/usr/bin/env python3
"""Hash-pinned native Hermes cron carrier for the F12 archiver payload."""
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"F12 Generator Archiver","output_class":"report","composer_type":"no_agent_script_local_only","privacy_level":"private-local-only","detail_policy":"complete_summary_in_discord","fixture":"📊 **F12 Generator Archiver** — PASS · 0 removals\n\nRoot: **<75% target** · protected paths: **unchanged**\n→ local receipt: `archive/f12-disk-archival/run-YYYYMMDDTHHMMSSZ.json`\n-# contract: F12-GENERATOR-ARCHIVER-V2","fixture_output_class":"report","fixture_test":"/home/ubuntu/business/ops/tests/test_cron_discord_output_guard.py::test_registry_fixtures_lint","route":"local","effect_policy":"archive+sha256+R2-checksum-before-source-delete+active-reference+6GiB-floor gated"}
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import types
from pathlib import Path

PAYLOAD = Path("/home/ubuntu/business/scripts/reports_archiver.py")
EXPECTED_PAYLOAD_SHA256 = "ef09a4ab7b1cb4119340c233e07422a3702cc9bcef0583371fc316823a6462cf"
HELPER = Path("/home/ubuntu/business/scripts/r2_offload.py")
EXPECTED_HELPER_SHA256 = "4cf287076e9da579d976db0a4f9f0d3c885b5bd879eb6528baf02f3045e7a1c7"
SCHEMA = Path("/home/ubuntu/business/scripts/glp_current_content_base_schema.py")
EXPECTED_SCHEMA_SHA256 = "a2a65183b8f501213e9f03730a64101e141c79c43035009a7bf7b28c0a676e46"
F13 = Path("/home/ubuntu/business/scripts/f13_priority_archiver.py")
EXPECTED_F13_SHA256 = "505b0adeb85fe3c888b8aeb7fe85b44e3c42a45dccf46a62794a80ad31df1647"
OPS_ALERT = Path("/home/ubuntu/.hermes/scripts/ops_alert.py")
EXPECTED_OPS_ALERT_SHA256 = "a9e0707b0c7e78c30414f70dd2895b8bed40c43c46528487933f04966b39599b"
CHUNK = 1024 * 1024
MAX_SOURCE_BYTES = 10 * 1024 * 1024


class PayloadIntegrityError(RuntimeError):
    pass


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


def _bound_source_path(path: Path, *, label: str) -> os.stat_result:
    if not path.is_absolute() or Path(os.path.normpath(str(path))) != path:
        raise PayloadIntegrityError(f"{label}_invalid_canonical_path")
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            value = current.lstat()
        except OSError as exc:
            raise PayloadIntegrityError(f"{label}_invalid") from exc
        if stat.S_ISLNK(value.st_mode):
            raise PayloadIntegrityError(f"{label}_invalid_symlink")
        if current != path and not stat.S_ISDIR(value.st_mode):
            raise PayloadIntegrityError(f"{label}_invalid_parent")
    if not stat.S_ISREG(value.st_mode):
        raise PayloadIntegrityError(f"{label}_regular_file_required")
    if value.st_uid != os.geteuid():
        raise PayloadIntegrityError(f"{label}_owner_invalid")
    if stat.S_IMODE(value.st_mode) & 0o022:
        raise PayloadIntegrityError(f"{label}_group_or_world_writable")
    if value.st_nlink != 1:
        raise PayloadIntegrityError(f"{label}_link_count_invalid")
    if value.st_size > MAX_SOURCE_BYTES:
        raise PayloadIntegrityError(f"{label}_oversized")
    return value


def _read_verified_payload(
    path: Path, expected_sha256: str, *, label: str = "f12_payload"
) -> bytes:
    before = _bound_source_path(path, label=label)
    identity = _source_identity(before)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise PayloadIntegrityError(f"{label}_missing") from exc
    except OSError as exc:
        raise PayloadIntegrityError(f"{label}_invalid") from exc

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _source_identity(opened) != identity:
            raise PayloadIntegrityError(f"{label}_identity_drift")
        digest = hashlib.sha256()
        payload = bytearray()
        while True:
            chunk = os.read(descriptor, CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            payload.extend(chunk)
            if len(payload) > MAX_SOURCE_BYTES:
                raise PayloadIntegrityError(f"{label}_oversized")
        final = os.fstat(descriptor)
        try:
            path_after = path.lstat()
        except OSError as exc:
            raise PayloadIntegrityError(f"{label}_identity_drift") from exc
        if (
            _source_identity(final) != identity
            or _source_identity(path_after) != identity
            or len(payload) != before.st_size
        ):
            raise PayloadIntegrityError(f"{label}_identity_drift")
        if digest.hexdigest() != expected_sha256:
            raise PayloadIntegrityError(f"{label}_sha256_mismatch")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _execute_payload(
    payload: bytes,
    path: Path,
    *,
    ops_alert_source: bytes,
    ops_alert_path: Path,
) -> None:
    namespace = {
        "__name__": "__main__",
        "__file__": str(path),
        "__package__": None,
        "__cached__": None,
        "_F12_WRAPPER_VERIFIED_OPS_ALERT_SOURCE": ops_alert_source,
        "_F12_WRAPPER_VERIFIED_OPS_ALERT_PATH": ops_alert_path,
    }
    exec(compile(payload, str(path), "exec"), namespace, namespace)


def _install_verified_module(module_name: str, source: bytes, path: Path) -> types.ModuleType:
    """Replace any preloaded module with one executed only from verified bytes."""

    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[module_name] = module
    try:
        exec(compile(source, str(path), "exec"), module.__dict__, module.__dict__)
    except BaseException:
        if sys.modules.get(module_name) is module:
            sys.modules.pop(module_name, None)
        raise
    return module


def _execute_verified_schema(schema: bytes, path: Path) -> None:
    """Install the current-content schema from its exact verified bytes."""

    _install_verified_module("glp_current_content_base_schema", schema, path)


def _execute_verified_helper(helper: bytes, path: Path) -> None:
    """Install r2_offload from verified bytes before the payload imports it."""

    _install_verified_module("r2_offload", helper, path)


def main() -> int:
    try:
        payload = _read_verified_payload(PAYLOAD, EXPECTED_PAYLOAD_SHA256)
        helper = _read_verified_payload(
            HELPER,
            EXPECTED_HELPER_SHA256,
            label="f12_helper_r2_offload",
        )
        _f13 = _read_verified_payload(
            F13,
            EXPECTED_F13_SHA256,
            label="f13_priority_archiver",
        )
        schema = _read_verified_payload(
            SCHEMA,
            EXPECTED_SCHEMA_SHA256,
            label="f12_schema_glp_current_content_base",
        )
        ops_alert_source = _read_verified_payload(
            OPS_ALERT,
            EXPECTED_OPS_ALERT_SHA256,
            label="f12_ops_alert",
        )
    except PayloadIntegrityError as exc:
        print(
            json.dumps({"status": "BLOCKED", "reason": str(exc), "payload": str(PAYLOAD)}),
            file=sys.stderr,
        )
        return 2
    _execute_verified_schema(schema, SCHEMA)
    _execute_verified_helper(helper, HELPER)
    _execute_payload(
        payload,
        PAYLOAD,
        ops_alert_source=ops_alert_source,
        ops_alert_path=OPS_ALERT,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
