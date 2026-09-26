#!/usr/bin/env python3
"""Canonical multi-account Gmail API client for Hermes inbox paths.

All mailbox adapters use this module for Gmail authentication and provider
operations. Browser/CDP authentication is intentionally not supported here.
The caller chooses read or write capability; credentials are refreshed only in
memory and secrets are never included in receipts or exceptions.
"""
from __future__ import annotations

import base64
import json
import os
import stat
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from inbox_control_plane.outbound_style_guard import assert_raw_rfc822_dash_free

GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_MODIFY = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
GMAIL_FULL = "https://mail.google.com/"
SHEETS = "https://www.googleapis.com/auth/spreadsheets"
DRIVE = "https://www.googleapis.com/auth/drive"
DECISIONS_CHANNEL_ID = "1524254375453786294"
DEFAULT_TIMEOUT_SECONDS = 60
PARTNER_INFO_SEND_MAILBOX_IDS = {"glp_ops_gmail"}


class GmailClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mailbox:
    mailbox_id: str
    canonical_address: str
    provider: str
    adapter: str
    auth_reference: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Mailbox":
        return cls(
            mailbox_id=str(value.get("mailbox_id") or ""),
            canonical_address=str(value.get("canonical_address") or "").lower(),
            provider=str(value.get("provider") or ""),
            adapter=str(value.get("adapter") or ""),
            auth_reference=str(value.get("auth_reference") or ""),
        )


def _secure_json(path: Path) -> dict[str, Any]:
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise GmailClientError("gmail_auth_file_invalid")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GmailClientError("gmail_auth_file_unreadable") from exc


def _scopes(value: Iterable[str] | str | None) -> set[str]:
    if isinstance(value, str):
        value = value.split()
    return {str(item).strip() for item in (value or []) if str(item).strip()}


def _has(scopes: set[str], required: str) -> bool:
    return required in scopes or GMAIL_FULL in scopes or (required == GMAIL_SEND and GMAIL_MODIFY in scopes)


def _required_scopes(mode: str) -> set[str]:
    if mode == "read":
        return {GMAIL_READONLY}
    if mode == "label":
        return {GMAIL_MODIFY}
    if mode == "send":
        return {GMAIL_MODIFY, GMAIL_SEND}
    raise GmailClientError("gmail_capability_mode_invalid")


def _discord_token() -> str:
    path = Path("/home/ubuntu/.hermes/.env")
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        return ""
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if line.startswith("DISCORD_BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def alert_token_health_failure(mailbox: Mailbox, reason: str) -> dict[str, Any]:
    """Alert #decisions without exposing credentials or raw provider data."""
    payload = {
        "content": (
            "⚠️ **Gmail token health failure**\n\n"
            f"mailbox: `{mailbox.mailbox_id}` · account: `{mailbox.canonical_address}`\n"
            f"reason: `{reason}`\n"
            "No inbox mutation, draft, or send was attempted."
        ),
        "allowed_mentions": {"parse": []},
    }
    token = _discord_token()
    if not token:
        return {"status": "blocked", "reason": "discord_token_missing", "channel_id": DECISIONS_CHANNEL_ID}
    request = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{DECISIONS_CHANNEL_ID}/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "DiscordBot (https://discord.com, 10)",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
        return {"status": "sent", "channel_id": DECISIONS_CHANNEL_ID, "message_id": str(body.get("id") or "")}
    except urllib.error.HTTPError as exc:
        return {"status": "blocked", "channel_id": DECISIONS_CHANNEL_ID, "http_status": exc.code}


class GmailClient:
    def __init__(self, mailbox: Mailbox, service: Any, scopes: set[str], mode: str) -> None:
        self.mailbox = mailbox
        self.service = service
        self.scopes = scopes
        self.mode = mode

    @classmethod
    def from_mailbox(cls, mailbox_value: dict[str, Any] | Mailbox, *, mode: str = "read") -> "GmailClient":
        mailbox = mailbox_value if isinstance(mailbox_value, Mailbox) else Mailbox.from_dict(mailbox_value)
        if mailbox.provider not in {"gmail_api", "gmail_api_dwd", "gmail_api_oauth"}:
            raise GmailClientError("mailbox_not_gmail_api")
        required = _required_scopes(mode)
        refresh_transport = Request()

        def bounded_refresh_request(*args: Any, **kwargs: Any):
            kwargs.setdefault("timeout", DEFAULT_TIMEOUT_SECONDS)
            return refresh_transport(*args, **kwargs)
        if mailbox.provider == "gmail_api_dwd":
            data = _secure_json(Path(mailbox.auth_reference))
            # Workspace DWD was authorized with the canonical full-mail scope
            # (`mail.google.com/`), not the narrower consumer OAuth scopes.
            # Capability remains constrained by `mode`; the broad scope is only
            # the transport authorization required by the delegated identity.
            requested = [GMAIL_FULL]
            creds = service_account.Credentials.from_service_account_info(
                data, scopes=requested, subject=mailbox.canonical_address
            )
            creds.refresh(bounded_refresh_request)
            granted = _scopes(getattr(creds, "granted_scopes", None) or getattr(creds, "scopes", None))
        elif mailbox.adapter == "gmail_api_refresh_narrowed_readonly" and mode == "send":
            # Normal monitoring for this mailbox remains narrowed read-only.
            # The partner-info loop is an explicit, user-authorized B2B lane and
            # is allowlisted to the GLP mailbox only; it uses the existing
            # token's declared send/modify scopes without changing the registry's
            # default read path.
            if mailbox.mailbox_id not in PARTNER_INFO_SEND_MAILBOX_IDS:
                raise GmailClientError("gmail_partner_info_send_mailbox_not_allowlisted")
            from google.oauth2 import credentials as credentials_module
            data = _secure_json(Path(mailbox.auth_reference))
            declared = _scopes(data.get("scopes"))
            if not _has(declared, GMAIL_SEND) or not _has(declared, GMAIL_MODIFY):
                raise GmailClientError("gmail_partner_info_send_scopes_missing")
            creds = credentials_module.Credentials.from_authorized_user_info(data)
            if not creds.valid and creds.expired and creds.refresh_token:
                creds.refresh(bounded_refresh_request)
            if not creds.valid:
                raise GmailClientError("gmail_credentials_invalid")
            granted = _scopes(getattr(creds, "granted_scopes", None) or getattr(creds, "scopes", None) or declared)
        elif mailbox.adapter == "gmail_api_refresh_narrowed_readonly":
            from google.oauth2 import credentials as credentials_module
            data = _secure_json(Path(mailbox.auth_reference))
            required_fields = {"refresh_token", "token_uri", "client_id", "client_secret"}
            if any(not data.get(field) for field in required_fields):
                raise GmailClientError("gmail_narrow_refresh_fields_missing")
            creds = credentials_module.Credentials(
                token=None,
                refresh_token=data["refresh_token"],
                token_uri=data["token_uri"],
                client_id=data["client_id"],
                client_secret=data["client_secret"],
                scopes=[GMAIL_READONLY],
            )
            creds.refresh(bounded_refresh_request)
            granted = _scopes(getattr(creds, "granted_scopes", None) or getattr(creds, "scopes", None))
        else:
            from google.oauth2 import credentials as credentials_module
            data = _secure_json(Path(mailbox.auth_reference))
            declared = _scopes(data.get("scopes"))
            if declared != {GMAIL_READONLY}:
                raise GmailClientError("gmail_credentials_scope_not_readonly")
            creds = credentials_module.Credentials.from_authorized_user_info(data, scopes=[GMAIL_READONLY])
            if not creds.valid and creds.expired and creds.refresh_token:
                creds.refresh(bounded_refresh_request)
            if not creds.valid:
                raise GmailClientError("gmail_credentials_invalid")
            granted = _scopes(getattr(creds, "granted_scopes", None) or getattr(creds, "scopes", None) or declared)
        for required_scope in required:
            if not _has(granted, required_scope):
                raise GmailClientError("gmail_effective_scope_missing")
        if mode == "read" and not (
            granted == {GMAIL_READONLY}
            or (mailbox.provider == "gmail_api_dwd" and granted == {GMAIL_FULL})
        ):
            raise GmailClientError("gmail_credentials_scope_not_readonly")
        import google_auth_httplib2
        import httplib2
        from googleapiclient import discovery as discovery_module
        authorized_http = google_auth_httplib2.AuthorizedHttp(
            creds,
            http=httplib2.Http(timeout=DEFAULT_TIMEOUT_SECONDS),
        )
        service = discovery_module.build(
            "gmail", "v1", http=authorized_http, cache_discovery=False,
        )
        return cls(mailbox, service, granted, mode)

    def preflight(self) -> dict[str, Any]:
        try:
            profile = self.service.users().getProfile(userId="me").execute()
            actual = str(profile.get("emailAddress") or "").lower()
            if actual != self.mailbox.canonical_address:
                raise GmailClientError("gmail_profile_mismatch")
            return {
                "status": "pass",
                "mailbox_id": self.mailbox.mailbox_id,
                "account": actual,
                "scope_count": len(self.scopes),
                "mode": self.mode,
            }
        except GmailClientError:
            raise
        except Exception as exc:
            raise GmailClientError("gmail_profile_probe_failed") from exc

    def list_messages(self, query: str, *, max_results: int = 500, page_limit: int = 1000) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        for _ in range(page_limit):
            kwargs: dict[str, Any] = {"userId": "me", "q": query, "maxResults": min(max_results, 500), "includeSpamTrash": False}
            if token:
                if token in seen_tokens:
                    raise GmailClientError("gmail_pagination_token_repeated")
                seen_tokens.add(token)
                kwargs["pageToken"] = token
            response = self.service.users().messages().list(**kwargs).execute()
            results.extend(response.get("messages") or [])
            token = response.get("nextPageToken")
            if not token:
                return results
        raise GmailClientError("gmail_pagination_page_limit_exceeded")

    def get_message(self, message_id: str, *, format: str = "full") -> dict[str, Any]:
        return self.service.users().messages().get(userId="me", id=message_id, format=format).execute()

    def get_thread(self, thread_id: str, *, format: str = "full") -> dict[str, Any]:
        return self.service.users().threads().get(userId="me", id=thread_id, format=format).execute()

    def modify_labels(self, message_id: str, *, remove: list[str] | None = None, add: list[str] | None = None) -> dict[str, Any]:
        if not _has(self.scopes, GMAIL_MODIFY):
            raise GmailClientError("gmail_modify_scope_missing")
        return self.service.users().messages().modify(
            userId="me", id=message_id, body={"removeLabelIds": remove or [], "addLabelIds": add or []}
        ).execute()

    def send_raw(self, raw_b64url: str) -> dict[str, Any]:
        if not _has(self.scopes, GMAIL_SEND) or not _has(self.scopes, GMAIL_MODIFY):
            raise GmailClientError("gmail_send_scope_missing")
        try:
            raw = base64.urlsafe_b64decode(raw_b64url + "=" * (-len(raw_b64url) % 4))
            assert_raw_rfc822_dash_free(raw, context="gmail_client.send_raw")
        except GmailClientError:
            raise
        except Exception as exc:
            raise GmailClientError("gmail_outbound_dash_rule_violation") from exc
        return self.service.users().messages().send(userId="me", body={"raw": raw_b64url}).execute()


def load_registry(path: Path = Path("/home/ubuntu/business/inbox_control_plane/registry.json")) -> list[Mailbox]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [Mailbox.from_dict(item) for item in data.get("mailboxes", []) if item.get("provider", "").startswith("gmail_api")]


def preflight_gmail_mailboxes(*, mode: str = "read") -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for mailbox in load_registry():
        client = GmailClient.from_mailbox(mailbox, mode=mode)
        results.append(client.preflight())
    return results
