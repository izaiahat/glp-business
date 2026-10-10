"""Cancellation fixture travels through intake to the single-writer dry plan."""
import asyncio
import importlib.util
import json
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock

SPEC = importlib.util.spec_from_file_location(
    'cancellation_reconciler', Path(__file__).resolve().parents[1] / 'scripts/affiliate_approval_auto_reconciler.py')
reconciler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reconciler)
import glp_sheet_sync_drain as drain


def test_cancellation_email_dry_run_status_update(tmp_path, monkeypatch):
    message = EmailMessage()
    message['From'] = 'affiliates@maxbounty.com'
    message['Subject'] = 'Campaign Cancellation Notice'
    message['Authentication-Results'] = 'mx.google.com; dmarc=pass header.from=maxbounty.com'
    name = 'Roman Health - Metabolic Weight Loss - CPL (US)'
    message.set_content(f'The advertiser for campaign {name} requires that it expire on 09/14/2026 21:30.')
    raw = tmp_path / 'cancellation.eml'
    raw.write_bytes(message.as_bytes())
    monkeypatch.setattr(reconciler, 'BUSINESS', tmp_path)
    monkeypatch.setattr(reconciler, 'REPORT_DIR', tmp_path / 'reports')
    monkeypatch.setattr(reconciler, 'STATE_DB', tmp_path / 'state.sqlite3')
    row = reconciler.pad([])
    row[2], row[4], row[5] = name, 'MaxBounty', 'https://affiliates.maxbounty.com/campaign/29946'
    row[10], row[19], row[20] = 'approved_active_tracking_ready', True, False
    service = MagicMock()
    service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {'values': [reconciler.HEADERS, row]}
    monkeypatch.setattr(reconciler.n8n_dt, 'insert', lambda *args: (_ for _ in ()).throw(AssertionError('dry run enqueued')))
    event = {'event_id': 'evt_fixture_cancellation', 'raw_path': str(raw), 'received_at': '2026-09-11T19:25:14Z', 'state': 'ingested', 'canonical_category': 'affiliate_network_response'}
    state = reconciler.init_state()
    try:
        result = asyncio.run(reconciler.process_event(event, service, None, state, True, {}))
    finally:
        state.close()
    receipt = json.loads(Path(result['receipt']).read_text())
    assert result['disposition'] == 'requires_provider_verification'
    assert receipt['status'] == 'BLOCKED'
    assert receipt['sheet_write_count'] == 0
    assert receipt['sheet_readback_verified'] is False
    assert receipt['provider_readback_verified'] is False
    assert receipt['action_completed'] is False
    assert receipt['signal'] == 'campaign_cancellation'
    assert receipt['outbound_send'] is False
    service.spreadsheets.assert_not_called()


def test_cancellation_real_sqlite_row_preserves_provider_verification_hold(tmp_path, monkeypatch):
    import sqlite3
    message = EmailMessage()
    message['From'] = 'affiliates@maxbounty.com'
    message['Subject'] = 'Campaign Cancellation Notice'
    message['Authentication-Results'] = 'mx.google.com; dmarc=pass header.from=maxbounty.com'
    message.set_content('The advertiser for campaign Roman Health - Metabolic Weight Loss - CPL (US) requires that it expire on 10/09/2026 21:30.')
    raw = tmp_path / 'cancellation.eml'
    raw.write_bytes(message.as_bytes())
    monkeypatch.setattr(reconciler, 'REPORT_DIR', tmp_path / 'reports')
    monkeypatch.setattr(reconciler, 'STATE_DB', tmp_path / 'state.sqlite3')
    with sqlite3.connect(':memory:') as inbox:
        inbox.row_factory = sqlite3.Row
        event = inbox.execute(
            'SELECT ? AS event_id, ? AS raw_path, ? AS received_at, '
            '? AS state, ? AS canonical_category',
            ('evt_sqlite_cancellation', str(raw), '2026-10-09T12:00:00Z',
             'ingested', 'affiliate_network_response'),
        ).fetchone()
    service = MagicMock()
    state = reconciler.init_state()
    try:
        result = asyncio.run(reconciler.process_event(event, service, None, state, False, {}))
    finally:
        state.close()
    receipt = json.loads(Path(result['receipt']).read_text())
    assert result['disposition'] == 'requires_provider_verification'
    assert receipt['status'] == 'BLOCKED'
    assert receipt['sheet_write_count'] == 0
    assert receipt['outbound_send'] is False
    service.spreadsheets.assert_not_called()


def test_cancellation_consumer_does_not_report_pass_for_verification_hold(tmp_path, monkeypatch):
    import sqlite3
    message = EmailMessage()
    message['From'] = 'affiliates@maxbounty.com'
    message['Subject'] = 'Campaign Cancellation Notice'
    message['Authentication-Results'] = 'mx.google.com; dmarc=pass header.from=maxbounty.com'
    message.set_content('The advertiser for campaign Roman Health - Metabolic Weight Loss - CPL (US) requires that it expire on 10/09/2026 21:30.')
    raw = tmp_path / 'cancellation.eml'
    raw.write_bytes(message.as_bytes())
    monkeypatch.setattr(reconciler, 'REPORT_DIR', tmp_path / 'reports')
    monkeypatch.setattr(reconciler, 'STATE_DB', tmp_path / 'state.sqlite3')
    with sqlite3.connect(':memory:') as inbox:
        inbox.row_factory = sqlite3.Row
        event = inbox.execute('SELECT ? AS event_id, ? AS raw_path, ? AS received_at, ? AS state, ? AS canonical_category', ('evt_sqlite_cancellation', str(raw), '2026-10-09T12:00:00Z', 'ingested', 'affiliate_network_response')).fetchone()
    state = reconciler.init_state()
    try:
        result = asyncio.run(reconciler.process_selected_events([(event, None)], MagicMock(), None, state, False, {}))
        assert result[0]['status'] == 'BLOCKED'
        saved = state.execute('SELECT status,receipt_path FROM events WHERE event_id=?', (event['event_id'],)).fetchone()
        assert saved['status'] == 'blocked'
        assert saved['receipt_path'] == result[0]['receipt']
    finally:
        state.close()
