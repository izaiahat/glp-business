"""GLP-113 IMAP sent-copy classification; synthetic mail, SQLite and IMAP only.

All Mail carries our own sent copies. The poller may skip a message only when
From is owned by the polled mailbox AND no recipient is owned by any registry
mailbox (counted as ``sent_only_skipped``). Every other shape must reach ingest
and raise a non-silent notification.
"""
from __future__ import annotations

import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

KAEL = "kael.fixture@example.invalid"
KAEL_ALIAS = "kael.alias@example.invalid"
GLP = "ops@glp-fixture.invalid"
BORING = "hello@boring-fixture.invalid"
EXTERNAL = "person@customer.invalid"


def _mailbox(mailbox_id, venture, address, adapter, aliases=()):
    return {
        "mailbox_id": mailbox_id, "venture": venture, "company": venture,
        "canonical_address": address, "aliases": list(aliases), "groups": [],
        "provider": "gmail", "adapter": adapter, "auth_reference": "synthetic-unused",
        "owner": "synthetic", "lifecycle": "active", "poll_seconds": 300,
        "freshness_sla_seconds": 900, "inbound_surface": "email",
        "routing_policy": "default", "dmarc_domains": [],
    }


def mail(sender, to, *, cc=None, message_id, subject="Pricing question",
         body="Can you send pricing for next week?", headers=None):
    m = EmailMessage()
    m["From"] = sender
    m["To"] = to
    if cc:
        m["Cc"] = cc
    m["Subject"] = subject
    m["Date"] = "Sat, 03 Oct 2026 09:00:00 +0000"
    m["Message-ID"] = f"<{message_id}>"
    for key, value in (headers or {}).items():
        m[key] = value
    m.set_content(body)
    return m.as_bytes()


class Imap:
    """Read-only All Mail stub serving one raw message per sequence id."""

    def __init__(self, raws):
        self.raws = raws

    def login(self, *_args):
        return "OK", []

    def select(self, *_args, **_kwargs):
        return "OK", []

    def search(self, *_args):
        return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.raws)))]

    def fetch(self, sequence_id, query):
        index = int(sequence_id) - 1
        meta = f"{index + 1} (X-GM-MSGID {9000 + index} X-GM-THRID {8000 + index})".encode()
        if "HEADER.FIELDS" in query:
            return "OK", [(meta, b"Message-ID: <id-%d@x>\r\n\r\n" % index)]
        return "OK", [(meta, self.raws[index])]

    def store(self, *_args):
        raise AssertionError("provider mutation attempted")

    def logout(self):
        return "BYE", []


@pytest.fixture
def plane(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    from inbox_control_plane import affiliate_supply_intake as supply
    from inbox_control_plane import control_plane as cp
    assert Path(cp.__file__).resolve().is_relative_to(ROOT)
    for name in ("PRIVATE_ROOT", "RAW_ROOT", "ATTACHMENT_ROOT", "BACKUP_ROOT", "REPORT_ROOT", "DAILY_AUDIT_DIR"):
        monkeypatch.setattr(cp, name, tmp_path / name)
    monkeypatch.setattr(supply, "INTAKE_ROOT", tmp_path / "supply")
    monkeypatch.setattr(cp, "run_fable_email_drafter", lambda request: {"status": "blocked", "reason": "offline_fixture"})
    registry = {"mailboxes": [
        _mailbox("kael_generic_gmail", "kael", KAEL, "generic_imap_readonly", aliases=[KAEL_ALIAS]),
        _mailbox("glp_ops_gmail", "glp_clinic_scout", GLP, "gmail_api_readonly"),
        _mailbox("boring_hello_gmail", "boring_ai", BORING, "gmail_api_readonly"),
    ]}
    ledger = cp.Ledger(tmp_path / "inbox.sqlite3")
    ledger.sync_registry(registry)
    monkeypatch.setattr(cp, "load_env_value", lambda _paths, key: "app-password" if key == "GMAIL_APP_PASSWORD" else KAEL)
    monkeypatch.setattr(cp.time, "sleep", lambda _seconds: None)

    def poll(*raws):
        monkeypatch.setattr(cp.imaplib, "IMAP4_SSL", lambda *_a, **_k: Imap(list(raws)))
        return cp.poll_generic_imap(ledger, registry["mailboxes"][0], "run-glp113", False, 250)

    yield cp, ledger, poll
    ledger.close()


def alarms(ledger):
    """Return (route, reason_code, has_notification) per ingested event."""
    rows = ledger.conn.execute(
        """SELECT c.route, c.reason_code,
                  EXISTS(SELECT 1 FROM notification_outbox o WHERE o.event_id=e.event_id) AS notified
           FROM inbox_events e JOIN inbox_classifications c ON c.event_id=e.event_id"""
    ).fetchall()
    return [(r["route"], r["reason_code"], bool(r["notified"])) for r in rows]


def assert_ingested_and_alarmed(result, ledger, expected_reason=None):
    assert result["status"] == "pass"
    assert result["sent_only_skipped"] == 0
    assert result["persisted"] == 1
    assert result["provider_mutations"] == 0 and result["external_sends"] == 0
    [(route, reason, notified)] = alarms(ledger)
    assert route != "silent", (route, reason)
    assert notified, (route, reason)
    if expected_reason:
        assert reason == expected_reason


def test_sent_only_copy_is_skipped_and_recorded(plane):
    cp, ledger, poll = plane
    result = poll(mail(KAEL, EXTERNAL, message_id="sent-only@x"))
    assert result["sent_only_skipped"] == 1
    assert result["persisted"] == 0
    assert alarms(ledger) == []


def test_sent_only_copy_from_alias_is_skipped(plane):
    cp, ledger, poll = plane
    result = poll(mail(f"Kael <{KAEL_ALIAS.upper()}>", EXTERNAL, message_id="alias-sent@x"))
    assert result["sent_only_skipped"] == 1
    assert alarms(ledger) == []


def test_unknown_inbound_still_alarms(plane):
    cp, ledger, poll = plane
    result = poll(mail(EXTERNAL, "list@lists.invalid", message_id="unknown@x"))
    assert_ingested_and_alarmed(result, ledger, "unregistered_recipient_mapping")


def test_owned_address_recipient_still_alarms(plane):
    cp, ledger, poll = plane
    result = poll(mail(KAEL, GLP, message_id="owned-recipient@x"))
    assert_ingested_and_alarmed(result, ledger)


def test_owned_recipient_in_cc_still_alarms(plane):
    cp, ledger, poll = plane
    result = poll(mail(KAEL, EXTERNAL, cc=BORING, message_id="owned-cc@x"))
    assert_ingested_and_alarmed(result, ledger)


def test_ambiguous_ownership_still_alarms(plane):
    cp, ledger, poll = plane
    result = poll(mail(KAEL, f"{GLP}, {BORING}", message_id="ambiguous@x"))
    assert_ingested_and_alarmed(result, ledger, "ambiguous_recipient_ownership")


def test_foreign_sender_still_alarms(plane):
    cp, ledger, poll = plane
    result = poll(mail(EXTERNAL, KAEL, message_id="foreign@x"))
    assert_ingested_and_alarmed(result, ledger)


def test_foreign_sender_spoofing_display_name_still_alarms(plane):
    cp, ledger, poll = plane
    result = poll(mail(f'"{KAEL}" <attacker@evil.invalid>', EXTERNAL, message_id="spoof@x"))
    assert_ingested_and_alarmed(result, ledger, "unregistered_recipient_mapping")


def test_multiple_from_addresses_are_not_treated_as_sent_copy(plane):
    cp, ledger, poll = plane
    result = poll(mail(f"{KAEL}, {EXTERNAL}", EXTERNAL, message_id="multi-from@x"))
    assert_ingested_and_alarmed(result, ledger)


def test_forged_own_from_delivered_to_us_still_alarms(plane):
    cp, ledger, poll = plane
    raw = mail(KAEL, EXTERNAL, message_id="forged-from@x", headers={"Delivered-To": KAEL})
    result = poll(raw)
    assert_ingested_and_alarmed(result, ledger)
