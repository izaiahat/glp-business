#!/usr/bin/env python3
"""Canonical, secret-safe loader for the Impact partner API credential.

All active Impact API consumers must use this module. Values are read only from
``~/.hermes/secrets/impact/api.json`` and are never included in exceptions,
logs, receipts, command-line arguments, or subprocess environments.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

IMPACT_API_SECRET = Path.home() / ".hermes" / "secrets" / "impact" / "api.json"


class ImpactCredentialError(RuntimeError):
    """A value-blind credential configuration failure."""


@dataclass(frozen=True)
class ImpactApiCredentials:
    account_sid: str
    auth_token: str
    api_base: str

    @property
    def basic_auth(self) -> tuple[str, str]:
        return self.account_sid, self.auth_token


def load_impact_api_credentials(path: Path | str = IMPACT_API_SECRET) -> ImpactApiCredentials:
    """Load and validate the one canonical Impact credential without exposing it."""
    credential_path = Path(path).expanduser()
    try:
        info = credential_path.stat()
    except FileNotFoundError as exc:
        raise ImpactCredentialError("impact_api_credential_missing") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ImpactCredentialError("impact_api_credential_not_regular_file")
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise ImpactCredentialError("impact_api_credential_permissions_invalid")
    try:
        document = json.loads(credential_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ImpactCredentialError("impact_api_credential_json_invalid") from exc
    if not isinstance(document, dict):
        raise ImpactCredentialError("impact_api_credential_shape_invalid")

    account_sid = str(document.get("account_sid") or "").strip()
    auth_token = str(document.get("auth_token") or "").strip()
    api_base = str(document.get("api_base") or "").strip().rstrip("/")
    if not account_sid or not auth_token or not api_base:
        raise ImpactCredentialError("impact_api_credential_fields_incomplete")
    if any(char in account_sid or char in auth_token for char in ("\r", "\n", "\x00")):
        raise ImpactCredentialError("impact_api_credential_fields_invalid")

    parsed = urlsplit(api_base)
    if (parsed.scheme != "https" or parsed.hostname != "api.impact.com"
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise ImpactCredentialError("impact_api_base_invalid")
    return ImpactApiCredentials(account_sid=account_sid, auth_token=auth_token,
                                api_base="https://api.impact.com")


__all__ = [
    "IMPACT_API_SECRET", "ImpactApiCredentials", "ImpactCredentialError",
    "load_impact_api_credentials",
]
