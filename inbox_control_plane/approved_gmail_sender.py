#!/usr/bin/env python3
"""Exact, event-bound Gmail reply transport for operator-approved inbox drafts.

This module contains the only Gmail write edge used by the inbox control plane.
It does not decide whether a SEND is authorized; the canonical ledger passes a
frozen, card-bound approval packet after claiming the operator decision.
"""
from __future__ import annotations

import base64
import hashlib
import json
import stat
import time
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr, getaddresses, parseaddr
from pathlib import Path
from typing import Any, Callable

from inbox_control_plane.outbound_style_guard import assert_dash_free, assert_message_dash_free

GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_FULL_SCOPE = "https://mail.google.com/"
SUPPORTED_MAILBOX_IDS = {"glp_ops_gmail", "glp_izaiah_gmail"}
DISPLAY_NAMES = {
    "ops@glpclinicscout.com": "Izaiah Tilton | GLP Clinic Scout",
    "izaiah@glpclinicscout.com": "Izaiah Tilton | GLP Clinic Scout",
}
GLP_PR_SENDER = "izaiah@glpclinicscout.com"
GLP_PR_APPROVAL_SCHEMA = "glp-digital-pr-pitch-approval-v1"
GLP_PR_HEADERS = {
    "pitch_id": "X-Hermes-GLP-PR-Pitch-ID",
    "observation_sha256": "X-Hermes-GLP-PR-Observation-SHA256",
    "approval_binding_sha256": "X-Hermes-GLP-PR-Approval-SHA256",
    "recipient_sha256": "X-Hermes-GLP-PR-Recipient-SHA256",
    "subject_sha256": "X-Hermes-GLP-PR-Subject-SHA256",
    "body_sha256": "X-Hermes-GLP-PR-Body-SHA256",
    "claims_sha256": "X-Hermes-GLP-PR-Claims-SHA256",
    "decision_message_id": "X-Hermes-Decision-ID",
}


class ApprovedSendError(RuntimeError):
    """A redacted transport failure with provider-submission state."""

    def __init__(
        self,
        reason: str,
        *,
        submission_started: bool = False,
        provider_message_id: str = "",
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.submission_started = submission_started
        self.provider_message_id = provider_message_id


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_body(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")


def normalize_message_id(value: str) -> str:
    return "".join(str(value or "").split()).strip("<>")


def normalize_header_value(value: str) -> str:
    return "".join(str(value or "").split())


def decode_raw(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def plain_body(parsed: Any) -> str:
    part = parsed.get_body(preferencelist=("plain",)) if parsed.is_multipart() else None
    return str(part.get_content() if part is not None else parsed.get_content())


def attachment_count(parsed: Any) -> int:
    return sum(1 for part in parsed.walk() if part.get_content_disposition() == "attachment")


def _scopes_from_token(data: dict[str, Any]) -> set[str]:
    raw = data.get("scopes") or []
    if isinstance(raw, str):
        raw = raw.split()
    return {str(item).strip() for item in raw if str(item).strip()}


def gmail_service_for_approved_send(mailbox: dict[str, Any]):
    """Build a broad in-memory Gmail client only for an allowlisted mailbox."""
    if str(mailbox.get("mailbox_id") or "") not in SUPPORTED_MAILBOX_IDS:
        raise ApprovedSendError("approved_send_mailbox_not_allowlisted")
    if str(mailbox.get("provider") or "") not in {"gmail_api", "gmail_api_dwd"}:
        # gmail_api_dwd added 2026-08-05: the DWD service-account path is the
        # canonical persistent transport for these Workspace mailboxes (mode=
        # "send" grants full-mail scope, capability still constrained by mode).
        # The old gmail_api-only gate blocked every operator-approved send from
        # ops@glpclinicscout.com while the SAME client sent partner mail daily.
        raise ApprovedSendError("approved_send_provider_not_gmail_api")
    try:
        from inbox_control_plane.gmail_client import GmailClient, GmailClientError
        client = GmailClient.from_mailbox(mailbox, mode="send")
        client.preflight()
        return client.service
    except GmailClientError as exc:
        raise ApprovedSendError(str(exc)) from exc


def _sender_ready(service: Any, sender: str) -> tuple[str, bool]:
    profile = str(service.users().getProfile(userId="me").execute().get("emailAddress") or "").lower()
    rows = service.users().settings().sendAs().list(userId="me").execute().get("sendAs") or []
    selected = next((row for row in rows if str(row.get("sendAsEmail") or "").lower() == sender), None)
    ready = bool(
        selected
        and (
            selected.get("verificationStatus") == "accepted"
            or (profile == sender and bool(selected.get("isDefault")))
        )
    )
    return profile, ready


def _addresses(value: str) -> list[str]:
    return [address.lower() for _name, address in getaddresses([value]) if address]


def _material_checks(parsed: Any, got: dict[str, Any], binding: dict[str, Any]) -> dict[str, bool]:
    body = normalize_body(plain_body(parsed))
    original_mid = normalize_message_id(binding.get("rfc_message_id") or "")
    references = {
        normalize_message_id(item)
        for item in str(parsed.get("References") or "").split()
        if normalize_message_id(item)
    }
    checks = {
        "sent_label_present": "SENT" in set(got.get("labelIds") or []),
        "to_exact": _addresses(str(parsed.get("To") or "")) == [binding["recipient"]],
        "from_exact": parseaddr(str(parsed.get("From") or ""))[1].lower() == binding["from_identity"],
        "reply_to_exact": parseaddr(str(parsed.get("Reply-To") or ""))[1].lower() == binding["from_identity"],
        "subject_exact": str(parsed.get("Subject") or "") == binding["subject"],
        "body_hash_exact": sha256_text(body) == binding["body_sha256"],
        "body_exact": body == binding["body"],
        "thread_exact": str(got.get("threadId") or "") == binding["provider_thread_id"],
        "in_reply_to_exact": (not original_mid)
        or normalize_message_id(str(parsed.get("In-Reply-To") or "")) == original_mid,
        "references_exact": (not original_mid) or original_mid in references,
        "attachment_count_zero": attachment_count(parsed) == 0,
    }
    return checks


def _identity_checks(parsed: Any, binding: dict[str, Any]) -> dict[str, bool]:
    return {
        "event_header_exact": normalize_header_value(parsed.get("X-Hermes-Event-ID") or "") == binding["event_id"],
        "draft_header_exact": normalize_header_value(parsed.get("X-Hermes-Draft-ID") or "") == binding["draft_id"],
        "revision_header_exact": normalize_header_value(parsed.get("X-Hermes-Draft-Revision") or "") == str(binding["draft_revision"]),
        "body_header_exact": normalize_header_value(parsed.get("X-Hermes-Draft-SHA256") or "") == binding["body_sha256"],
        "decision_header_exact": normalize_header_value(parsed.get("X-Hermes-Decision-ID") or "") == binding["decision_message_id"],
    }


def verify_provider_message(
    service: Any,
    message_id: str,
    binding: dict[str, Any],
    *,
    require_identity_headers: bool,
) -> dict[str, Any]:
    got = service.users().messages().get(userId="me", id=message_id, format="raw").execute()
    parsed = BytesParser(policy=policy.default).parsebytes(decode_raw(str(got.get("raw") or "")))
    checks = _material_checks(parsed, got, binding)
    if require_identity_headers:
        checks.update(_identity_checks(parsed, binding))
    checks["all_passed"] = all(checks.values())
    return {
        "provider_message_id": str(message_id),
        "provider_thread_id": str(got.get("threadId") or ""),
        "provider_internal_date": str(got.get("internalDate") or ""),
        "checks": checks,
    }


def exact_prior_messages(service: Any, binding: dict[str, Any]) -> list[dict[str, Any]]:
    query = f'in:sent to:{binding["recipient"]}'
    rows = service.users().messages().list(userId="me", q=query, maxResults=50).execute().get("messages") or []
    exact: list[dict[str, Any]] = []
    for row in rows:
        message_id = str(row.get("id") or "")
        if not message_id:
            continue
        verification = verify_provider_message(service, message_id, binding, require_identity_headers=False)
        if all(value for key, value in verification["checks"].items() if key != "all_passed"):
            exact.append(verification)
    return exact


def _build_message(binding: dict[str, Any]) -> dict[str, str]:
    sender = binding["from_identity"]
    recipient = binding["recipient"]
    message = EmailMessage()
    message["To"] = formataddr((binding.get("recipient_display") or "", recipient))
    message["From"] = formataddr((DISPLAY_NAMES.get(sender, sender), sender))
    message["Reply-To"] = sender
    message["Subject"] = binding["subject"]
    if binding.get("rfc_message_id"):
        message["In-Reply-To"] = binding["rfc_message_id"]
        message["References"] = binding["rfc_message_id"]
    message["X-Hermes-Event-ID"] = binding["event_id"]
    message["X-Hermes-Draft-ID"] = binding["draft_id"]
    message["X-Hermes-Draft-Revision"] = str(binding["draft_revision"])
    message["X-Hermes-Draft-SHA256"] = binding["body_sha256"]
    message["X-Hermes-Decision-ID"] = binding["decision_message_id"]
    message.set_content(binding["body"])
    assert_message_dash_free(message, context="approved_gmail_sender")
    request = {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}
    if binding.get("provider_thread_id"):
        request["threadId"] = binding["provider_thread_id"]
    return request


def run_approved_gmail_send_transport(
    binding: dict[str, Any],
    *,
    allow_new_submission: bool,
    on_submission_start: Callable[[], None],
    service_factory: Callable[[dict[str, Any]], Any] = gmail_service_for_approved_send,
) -> dict[str, Any]:
    """Reconcile or send once. Never retries a submission without provider proof."""
    assert_dash_free(
        f"{binding.get('subject', '')}\n{binding.get('body', '')}",
        context="approved_gmail_sender.preflight",
    )
    service = service_factory(binding["mailbox"])
    profile, sender_ready = _sender_ready(service, binding["from_identity"])
    if profile != str(binding["mailbox"].get("canonical_address") or "").lower():
        raise ApprovedSendError("approved_send_profile_mismatch")
    if not sender_ready:
        raise ApprovedSendError("approved_send_identity_not_ready")

    prior = exact_prior_messages(service, binding)
    if len(prior) > 1:
        raise ApprovedSendError("multiple_exact_prior_messages")
    if prior:
        verification = verify_provider_message(
            service,
            prior[0]["provider_message_id"],
            binding,
            require_identity_headers=False,
        )
        if not verification["checks"]["all_passed"]:
            raise ApprovedSendError("exact_prior_readback_failed")
        return {
            "status": "ALREADY_SENT_VERIFIED",
            **verification,
            "send_executed_now": False,
            "authenticated_profile": profile,
            "sender_ready": True,
            "secret_printed": False,
        }
    if not allow_new_submission:
        raise ApprovedSendError("send_outcome_uncertain", submission_started=True)

    request = _build_message(binding)
    on_submission_start()
    try:
        sent = service.users().messages().send(userId="me", body=request).execute()
        message_id = str(sent.get("id") or "")
        if not message_id:
            raise ApprovedSendError("gmail_send_returned_no_message_id", submission_started=True)
    except ApprovedSendError:
        raise
    except Exception as exc:
        # The provider may have accepted the message before the HTTP error. Search
        # deterministically before returning an uncertain outcome; never resubmit.
        for delay in (0.5, 1.0, 2.0):
            time.sleep(delay)
            prior = exact_prior_messages(service, binding)
            if len(prior) == 1:
                message_id = prior[0]["provider_message_id"]
                break
            if len(prior) > 1:
                raise ApprovedSendError("multiple_exact_prior_messages", submission_started=True) from exc
        else:
            raise ApprovedSendError("gmail_send_outcome_uncertain", submission_started=True) from exc

    verification = verify_provider_message(service, message_id, binding, require_identity_headers=True)
    if not verification["checks"]["all_passed"]:
        raise ApprovedSendError(
            "gmail_sent_readback_failed",
            submission_started=True,
            provider_message_id=message_id,
        )
    return {
        "status": "SENT_VERIFIED",
        **verification,
        "send_executed_now": True,
        "authenticated_profile": profile,
        "sender_ready": True,
        "secret_printed": False,
    }


def _canonical_sha256(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _glp_pr_approval_binding(binding: dict[str, Any]) -> dict[str, str]:
    return {
        "schema": GLP_PR_APPROVAL_SCHEMA,
        "pitch_id": str(binding.get("pitch_id") or ""),
        "sender": str(binding.get("from_identity") or ""),
        "recipient": str(binding.get("recipient") or ""),
        "recipient_sha256": str(binding.get("recipient_sha256") or ""),
        "subject": str(binding.get("subject") or ""),
        "subject_sha256": str(binding.get("subject_sha256") or ""),
        "body_sha256": str(binding.get("body_sha256") or ""),
        "observation_sha256": str(binding.get("observation_sha256") or ""),
        "claims_sha256": str(binding.get("claims_sha256") or ""),
    }


def _validate_glp_pr_new_thread_binding(binding: dict[str, Any]) -> None:
    required = {
        "pitch_id", "body", "body_sha256", "subject", "subject_sha256", "recipient",
        "recipient_sha256", "from_identity", "observation_sha256", "claims_sha256",
        "approval_binding_sha256", "decision_message_id", "mailbox",
    }
    if not required <= set(binding):
        raise ApprovedSendError("glp_pr_new_thread_binding_incomplete")
    if str(binding["from_identity"]).lower() != GLP_PR_SENDER:
        raise ApprovedSendError("glp_pr_sender_invalid")
    if str(binding["recipient"]).lower() != str(binding["recipient"]):
        raise ApprovedSendError("glp_pr_recipient_not_normalized")
    if sha256_text(str(binding["recipient"])) != str(binding["recipient_sha256"]):
        raise ApprovedSendError("glp_pr_recipient_hash_mismatch")
    if sha256_text(str(binding["subject"])) != str(binding["subject_sha256"]):
        raise ApprovedSendError("glp_pr_subject_hash_mismatch")
    if sha256_text(normalize_body(str(binding["body"]))) != str(binding["body_sha256"]):
        raise ApprovedSendError("glp_pr_body_hash_mismatch")
    for key in ("body_sha256", "subject_sha256", "recipient_sha256", "observation_sha256", "claims_sha256", "approval_binding_sha256"):
        if not re_full_hash(str(binding.get(key) or "")):
            raise ApprovedSendError(f"glp_pr_{key}_invalid")
    if not str(binding["pitch_id"]).startswith("pitch_") or not str(binding["decision_message_id"]):
        raise ApprovedSendError("glp_pr_identity_invalid")
    if _canonical_sha256(_glp_pr_approval_binding(binding)) != str(binding["approval_binding_sha256"]):
        raise ApprovedSendError("glp_pr_approval_binding_hash_mismatch")


def re_full_hash(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def glp_pr_rfc_message_id(binding: dict[str, Any]) -> str:
    identity = sha256_text(f"{binding['pitch_id']}|{binding['approval_binding_sha256']}")[:40]
    return f"<glp-pr-{identity}@glpclinicscout.com>"


def _build_glp_pr_new_thread_message(binding: dict[str, Any]) -> dict[str, str]:
    _validate_glp_pr_new_thread_binding(binding)
    message = EmailMessage()
    message["To"] = formataddr((binding.get("recipient_display") or "", binding["recipient"]))
    message["From"] = formataddr((DISPLAY_NAMES[GLP_PR_SENDER], GLP_PR_SENDER))
    message["Reply-To"] = GLP_PR_SENDER
    message["Subject"] = binding["subject"]
    message["Message-ID"] = glp_pr_rfc_message_id(binding)
    for binding_key, header_name in GLP_PR_HEADERS.items():
        message[header_name] = str(binding[binding_key])
    message.set_content(binding["body"])
    assert_message_dash_free(message, context="approved_gmail_new_thread_sender")
    # Omitting threadId is a material safety property: Gmail must create a new
    # thread rather than attaching the pitch to any inbound or prior thread.
    return {"raw": base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")}


def verify_provider_new_thread_message(
    service: Any,
    message_id: str,
    binding: dict[str, Any],
) -> dict[str, Any]:
    got = service.users().messages().get(userId="me", id=message_id, format="raw").execute()
    parsed = BytesParser(policy=policy.default).parsebytes(decode_raw(str(got.get("raw") or "")))
    body = normalize_body(plain_body(parsed))
    expected_message_id = normalize_message_id(glp_pr_rfc_message_id(binding))
    identity_headers = {
        header_name: str(parsed.get(header_name) or "")
        for header_name in GLP_PR_HEADERS.values()
    }
    checks = {
        "sent_label_present": "SENT" in set(got.get("labelIds") or []),
        "to_exact": _addresses(str(parsed.get("To") or "")) == [binding["recipient"]],
        "from_exact": parseaddr(str(parsed.get("From") or ""))[1].lower() == GLP_PR_SENDER,
        "reply_to_exact": parseaddr(str(parsed.get("Reply-To") or ""))[1].lower() == GLP_PR_SENDER,
        "subject_exact": str(parsed.get("Subject") or "") == binding["subject"],
        "subject_hash_exact": sha256_text(str(parsed.get("Subject") or "")) == binding["subject_sha256"],
        "body_hash_exact": sha256_text(body) == binding["body_sha256"],
        "body_exact": body == normalize_body(binding["body"]),
        "new_thread_id_nonempty": bool(str(got.get("threadId") or "")),
        "message_id_exact": normalize_message_id(str(parsed.get("Message-ID") or "")) == expected_message_id,
        "in_reply_to_absent": not str(parsed.get("In-Reply-To") or "").strip(),
        "references_absent": not str(parsed.get("References") or "").strip(),
        "attachment_count_zero": attachment_count(parsed) == 0,
    }
    for binding_key, header_name in GLP_PR_HEADERS.items():
        checks[f"{binding_key}_header_exact"] = (
            normalize_header_value(identity_headers[header_name])
            == normalize_header_value(str(binding[binding_key]))
        )
    checks["all_passed"] = all(checks.values())
    return {
        "provider_message_id": str(message_id),
        "provider_thread_id": str(got.get("threadId") or ""),
        "provider_internal_date": str(got.get("internalDate") or ""),
        "checks": checks,
        "readback": {
            "from": parseaddr(str(parsed.get("From") or ""))[1].lower(),
            "to": binding["recipient"] if checks["to_exact"] else str(parsed.get("To") or ""),
            "reply_to": parseaddr(str(parsed.get("Reply-To") or ""))[1].lower(),
            "subject": str(parsed.get("Subject") or ""),
            "subject_sha256": sha256_text(str(parsed.get("Subject") or "")),
            "body": body,
            "body_sha256": sha256_text(body),
            "message_id": f"<{normalize_message_id(str(parsed.get('Message-ID') or ''))}>",
            "message_id_raw": str(parsed.get("Message-ID") or ""),
            "identity_headers": identity_headers,
        },
    }


def exact_prior_new_thread_messages(service: Any, binding: dict[str, Any]) -> list[dict[str, Any]]:
    query = f'in:sent rfc822msgid:{glp_pr_rfc_message_id(binding)}'
    rows = service.users().messages().list(userId="me", q=query, maxResults=10).execute().get("messages") or []
    exact: list[dict[str, Any]] = []
    for row in rows:
        message_id = str(row.get("id") or "")
        if not message_id:
            continue
        verification = verify_provider_new_thread_message(service, message_id, binding)
        if verification["checks"]["all_passed"]:
            exact.append(verification)
    return exact


def run_approved_gmail_new_thread_transport(
    binding: dict[str, Any],
    *,
    allow_new_submission: bool,
    on_submission_start: Callable[[], None],
    service_factory: Callable[[dict[str, Any]], Any] = gmail_service_for_approved_send,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Send or reconcile one approval-bound GLP journalist pitch as a new thread."""
    _validate_glp_pr_new_thread_binding(binding)
    assert_dash_free(
        f"{binding.get('subject', '')}\n{binding.get('body', '')}",
        context="approved_gmail_new_thread_sender.preflight",
    )
    service = service_factory(binding["mailbox"])
    profile, sender_ready = _sender_ready(service, GLP_PR_SENDER)
    if profile != str(binding["mailbox"].get("canonical_address") or "").lower() or profile != GLP_PR_SENDER:
        raise ApprovedSendError("glp_pr_profile_mismatch")
    if not sender_ready:
        raise ApprovedSendError("glp_pr_sender_identity_not_ready")

    prior = exact_prior_new_thread_messages(service, binding)
    if len(prior) > 1:
        raise ApprovedSendError("multiple_exact_prior_glp_pr_messages")
    if prior:
        return {
            "status": "ALREADY_SENT_VERIFIED",
            **prior[0],
            "send_executed_now": False,
            "authenticated_profile": profile,
            "sender_ready": True,
            "secret_printed": False,
        }
    if not allow_new_submission:
        raise ApprovedSendError("glp_pr_send_outcome_uncertain", submission_started=True)

    request = _build_glp_pr_new_thread_message(binding)
    if "threadId" in request:
        raise ApprovedSendError("glp_pr_new_thread_request_contaminated")
    on_submission_start()
    recovered_after_error = False
    try:
        sent = service.users().messages().send(userId="me", body=request).execute()
        message_id = str(sent.get("id") or "")
        if not message_id:
            raise ApprovedSendError("glp_pr_gmail_send_returned_no_message_id", submission_started=True)
    except ApprovedSendError:
        raise
    except Exception as exc:
        for delay in (0.5, 1.0, 2.0):
            sleep(delay)
            prior = exact_prior_new_thread_messages(service, binding)
            if len(prior) == 1:
                message_id = prior[0]["provider_message_id"]
                recovered_after_error = True
                break
            if len(prior) > 1:
                raise ApprovedSendError("multiple_exact_prior_glp_pr_messages", submission_started=True) from exc
        else:
            raise ApprovedSendError("glp_pr_gmail_send_outcome_uncertain", submission_started=True) from exc

    verification = verify_provider_new_thread_message(service, message_id, binding)
    if not verification["checks"]["all_passed"]:
        raise ApprovedSendError(
            "glp_pr_gmail_sent_readback_failed",
            submission_started=True,
            provider_message_id=message_id,
        )
    return {
        "status": "SENT_VERIFIED",
        **verification,
        "send_executed_now": True,
        "recovered_after_provider_error": recovered_after_error,
        "authenticated_profile": profile,
        "sender_ready": True,
        "secret_printed": False,
    }


def mark_source_read_verified(
    mailbox: dict[str, Any],
    source_message_id: str,
    *,
    service_factory: Callable[[dict[str, Any]], Any] = gmail_service_for_approved_send,
) -> dict[str, Any]:
    if not source_message_id:
        raise ApprovedSendError("source_message_id_missing")
    service = service_factory(mailbox)
    before = service.users().messages().get(userId="me", id=source_message_id, format="metadata").execute()
    before_labels = set(before.get("labelIds") or [])
    was_unread = "UNREAD" in before_labels
    if was_unread:
        service.users().messages().modify(
            userId="me", id=source_message_id, body={"removeLabelIds": ["UNREAD"]}
        ).execute()
    after = service.users().messages().get(userId="me", id=source_message_id, format="metadata").execute()
    after_labels = set(after.get("labelIds") or [])
    if "UNREAD" in after_labels:
        raise ApprovedSendError("source_message_mark_read_failed")
    if before_labels - {"UNREAD"} != after_labels:
        raise ApprovedSendError("source_message_label_delta_invalid")
    return {
        "attempted": was_unread,
        "was_unread": was_unread,
        "verified": True,
        "labels_preserved": True,
        "provider_mutation_executed": was_unread,
    }
