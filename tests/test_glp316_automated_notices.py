"""Offline ingestion contracts for network status mail; no provider or send calls."""
import tempfile
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from inbox_control_plane import affiliate_supply_intake
from inbox_control_plane import control_plane as cp


MAILBOX = {
    "mailbox_id": "glp_ops_gmail", "venture": "glp", "company": "GLP Clinic Scout",
    "canonical_address": "ops@glpclinicscout.com", "aliases": ["partners@glpclinicscout.com"],
    "groups": [], "provider": "gmail", "adapter": "gmail_api",
    "auth_reference": "offline-fixture", "owner": "fixture", "lifecycle": "active",
    "poll_seconds": 300, "freshness_sla_seconds": 600, "inbound_surface": "affiliate",
    "routing_policy": "glp_affiliate", "dmarc_domains": [],
}


def message(sender, subject, body, *, headers=None):
    mail = EmailMessage()
    mail["From"] = sender
    mail["To"] = "ops@glpclinicscout.com"
    mail["Subject"] = subject
    mail["Message-ID"] = "<fixture-glp316@example.test>"
    mail["Date"] = "Sat, 26 Sep 2026 00:00:00 +0000"
    for key, value in (headers or {}).items():
        mail[key] = value
    mail.set_content(body)
    return mail.as_bytes()


def ingest_fixture(sender, subject, body, *, headers=None):
    with tempfile.TemporaryDirectory(prefix="glp316-") as temporary:
        root = Path(temporary)
        with mock.patch.object(cp, "STATE_ROOT", root / "state"), mock.patch.object(
            cp, "PRIVATE_ROOT", root / "private"
        ), mock.patch.object(cp, "RAW_ROOT", root / "raw"), mock.patch.object(
            cp, "ATTACHMENT_ROOT", root / "attachments"
        ), mock.patch.object(cp, "BACKUP_ROOT", root / "backups"), mock.patch.object(
            cp, "REPORT_ROOT", root / "reports"
        ), mock.patch.object(cp, "DAILY_AUDIT_DIR", root / "audit"), mock.patch.object(
            affiliate_supply_intake, "INTAKE_ROOT", root / "supply-intake"
        ), mock.patch.object(
            cp, "load_reply_registry", return_value=({
                "templates": {"glp_affiliate": {"partner_vendor_action": {
                    "draft": True, "never_auto_send": True,
                    "body": "Thank you for your specific note. I will answer your partnership question after review."
                }}}
            }, "fixture-registry-sha256")
        ), mock.patch.object(
            cp, "run_fable_email_drafter", return_value={
                "status": "pass", "composer": "fable_claude_tool",
                "composer_version": cp.FABLE_EMAIL_DRAFTER_VERSION,
                "reply_class": "partner_vendor_action", "subject": "Re: Fixture inquiry",
                "body": "Thank you for your specific note. I will answer your partnership question after review.",
                "grounded_facts": ["offline fixture"], "risk_flags": [],
                "rationale": "Offline fixture", "provider_draft_created": False,
                "provider_mutation": False, "outbound_send": False, "never_auto_send": True,
                "model": "fixture", "effort": "fixture", "model_policy_sha256": "fixture",
                "prompt_sha256": "fixture", "response_id": "fixture", "response_sha256": "fixture",
            }) as drafter:
            ledger = cp.Ledger(root / "inbox.sqlite3")
            try:
                ledger.sync_registry({"mailboxes": [MAILBOX]})
                event = ledger.ingest(MAILBOX, "fixture-316", message(sender, subject, body, headers=headers), provider_thread_id="thread-316")
                row = ledger.conn.execute(
                    "SELECT category,reason_code,route FROM inbox_classifications WHERE event_id=?",
                    (event["event_id"],),
                ).fetchone()
                drafts = ledger.conn.execute("SELECT COUNT(*) FROM local_drafts WHERE event_id=?", (event["event_id"],)).fetchone()[0]
                sends = dict(ledger.conn.execute("SELECT name,value FROM side_effect_counters").fetchall())
                return event, tuple(row), drafts, sends, drafter.call_count
            finally:
                ledger.close()


def assert_silent_notice(sender, subject, body, headers=None, *, reason="affiliate_network_automated_notification"):
    event, classification, drafts, sends, composer_calls = ingest_fixture(sender, subject, body, headers=headers)
    assert event["status"] == "persisted"
    assert classification == ("affiliate_network_response", reason, "silent")
    assert drafts == composer_calls == 0
    assert sends == {"external_sends": 0, "provider_drafts": 0, "provider_mutations": 0}


def test_approval_notice_is_ingested_without_reply_draft_or_send():
    assert_silent_notice("affiliates@maxbounty.com", "Application approved",
                         "Your campaign application has been approved. You can now view the campaign.",
                         {"Auto-Submitted": "auto-generated"})


def test_cancellation_notice_is_ingested_without_reply_draft_or_send():
    assert_silent_notice("affiliates@maxbounty.com", "Campaign Cancellation Notice",
                         "Campaign Fixture One has been cancelled. Contact your affiliate manager if you have questions.")


def test_newsletter_is_ingested_without_reply_draft_or_send():
    assert_silent_notice("newsletter@awin.com", "Affiliate newsletter",
                         "This week's affiliate program updates.",
                         {"Precedence": "bulk", "List-Unsubscribe": "<mailto:unsubscribe@awin.com>"})


def test_no_reply_notice_is_ingested_without_reply_draft_or_send():
    assert_silent_notice("no-reply@everflow.io", "Account update",
                         "This is an automated status notification.")


def test_real_correspondent_retains_decisions_and_local_reply_draft_no_send():
    event, classification, drafts, sends, composer_calls = ingest_fixture(
        "Alex Morgan <alex.morgan@everflow.io>", "Re: Publisher partnership",
        "Could we schedule a call to discuss the affiliate partnership?")
    assert event["status"] == "persisted"
    assert classification == ("partner_vendor_action", "affiliate_network_genuine_human_reply", "decisions")
    assert drafts == 1
    assert composer_calls == 0  # Ingestion uses its local safe draft path.
    assert sends == {"external_sends": 0, "provider_drafts": 0, "provider_mutations": 0}
