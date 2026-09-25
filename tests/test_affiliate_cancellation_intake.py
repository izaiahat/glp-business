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
    queue = receipt['sheet_queue_receipt']['queue_row']
    evidence = json.loads(queue['evidence'])
    entry = drain.build_entry(queue['kind'], queue, evidence, 2, row)
    assert queue['kind'] == 'network_cancelled'
    assert queue['program_id'] == '29946'
    assert entry['cells']['10'] == 'cancelled_by_network'
    assert entry['cells']['19'] == 'FALSE'
    assert event['event_id'] in entry['cells']['23']
    assert evidence['event_at_utc'] == event['received_at']
    assert receipt['sheet_queue_receipt']['queued'] is False
    assert receipt['sheet_write_count'] == 0
    assert receipt['sheet_readback_verified'] is False
    assert receipt['signal'] == 'campaign_cancellation'
    assert receipt['outbound_send'] is False
