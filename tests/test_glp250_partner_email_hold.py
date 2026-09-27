"""Registered drain/writer integration with synthetic mail and in-memory Sheets.

No live credentials, Sheet, inbox, queue or runtime registry are used. The Google
transport and schema audit are fixture boundaries; the real drain, projection,
mode, preconditions, CAS, batch write, readback and replay run unchanged.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys
import types
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
KIND = 'partner_email_hold'
EVENT = 'evt_synthetic_hold_only'
WHEN = '2026-09-21T14:06:14Z'
CORRECTED = '2026-09-27T02:00:00Z'
RAW = (b'From: synthetic@xentraffic.com\r\n'
       b'Authentication-Results: mx.google.com; dmarc=pass header.from=xentraffic.com\r\n'
       b'Subject: SYNTHETIC fixture, not retained partner mail\r\n\r\n'
       b'SYNTHETIC: advertiser clearance and postback readback remain pending.\r\n')


def load(name, path, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class Sheets:
    def __init__(self, values, tab):
        self.rows = copy.deepcopy(values)
        self.tab = tab
        self.writes = []
        self.in_values = False

    def spreadsheets(self):
        self.in_values = False
        return self

    def values(self):
        self.in_values = True
        return self

    def get(self, **kwargs):
        if self.in_values:
            data = {'values': copy.deepcopy(self.rows)}
        else:
            data = {'sheets': [{'properties': {'title': self.tab, 'sheetId': 1,
                    'gridProperties': {'rowCount': len(self.rows), 'columnCount': 27}}}]}
        return types.SimpleNamespace(execute=lambda: data)

    def batchUpdate(self, **kwargs):
        assert self.in_values, 'No structural Sheet mutations are allowed'
        body = kwargs['body']
        assert body['valueInputOption'] == 'RAW'
        def execute():
            self.writes.append(copy.deepcopy(body['data']))
            for cell in body['data']:
                letters, row = re.search(r'!([A-Z]+)([0-9]+)$', cell['range']).groups()
                col = 0
                for letter in letters:
                    col = col * 26 + ord(letter) - ord('A') + 1
                self.rows[int(row) - 1][col - 1] = cell['values'][0][0]
            return {}
        return types.SimpleNamespace(execute=execute)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    # Import-only Google SDK stand-ins, scoped to this test, never real OAuth.
    for name in ('google', 'google.auth', 'google.auth.transport',
                 'google.auth.transport.requests', 'google.oauth2',
                 'google.oauth2.credentials', 'googleapiclient', 'googleapiclient.discovery'):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules['google.auth.transport.requests'].Request = Mock()
    sys.modules['google.oauth2.credentials'].Credentials = Mock()
    discovery = sys.modules['googleapiclient.discovery']
    discovery.build = Mock(side_effect=AssertionError('unbound transport'))
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    writer = load('affiliate_sheet_batch_writer', ROOT / 'scripts/affiliate_sheet_batch_writer.py', monkeypatch)
    drain = load('glp_sheet_sync_drain', ROOT / 'scripts/glp_sheet_sync_drain.py', monkeypatch)
    monkeypatch.setattr(writer, '_REPO_ROOT', tmp_path)
    monkeypatch.setattr(drain, 'BUSINESS', tmp_path)
    monkeypatch.setattr(writer, 'creds', lambda: None)
    monkeypatch.setattr(writer, 'require_checkbox_schema_pass', lambda *a: {'status': 'PASS'})
    # Synthetic custody is deliberately distinct from the nonsecret production
    # event pin. Never copy a retained mailbox body into a source fixture.
    digest = hashlib.sha256(RAW).hexdigest()
    monkeypatch.setattr(writer, 'PARTNER_EMAIL_HOLD_EVENT',
                        {'event_id': EVENT, 'raw_sha256': digest, 'received_at': WHEN}, raising=False)
    dbpath = tmp_path / 'fixture-inbox.sqlite3'
    rawpath = tmp_path / 'synthetic.eml'
    rawpath.write_bytes(RAW)
    with sqlite3.connect(dbpath) as db:
        db.execute('CREATE TABLE inbox_events (event_id TEXT, received_at TEXT, raw_path TEXT, raw_sha256 TEXT)')
        db.execute('INSERT INTO inbox_events VALUES (?,?,?,?)', (EVENT, WHEN, str(rawpath), digest))
    monkeypatch.setattr(writer, 'PARTNER_EMAIL_HOLD_INBOX_DB', dbpath, raising=False)
    source = [f'synthetic preserved column {i}' for i in range(27)]
    source[2] = 'Synthetic GLP-1 provider'
    source[4] = 'XenTraffic'
    source[5] = 'https://xentraffic.everflowclient.io/offers/3277'
    source[10] = 'approved_active_tracking_ready'
    source[14] = 'https://example.test/synthetic-never-click'
    for col in (1, 11, 19):
        source[col] = True
    source[20] = False
    source[23] = 'Old readiness provenance.\n  Preserve all old spacing and notes. '
    source[24] = '2026-08-28T20:45:03Z'
    evidence = {'event_id': EVENT, 'raw_sha256': digest, 'received_at': WHEN,
                'corrected_at_utc': CORRECTED, 'source_row': list(source)}
    queue = {'kind': KIND, 'network': 'XenTraffic', 'program_id': '3277',
             'program_name': source[2], 'sheet_row': 759,
             'update_id': f'partner-email-hold-{EVENT}-{digest}',
             'evidence': evidence, 'status': 'queued'}
    values = [writer.HEADERS] + [[''] * 27 for _ in range(757)] + [source]
    service = Sheets(values, writer.TAB)
    monkeypatch.setattr(writer, 'build', lambda *a, **k: service)
    monkeypatch.setattr(discovery, 'build', lambda *a, **k: service)
    counter = 0
    def run(q=None, apply=True):
        nonlocal counter
        counter += 1
        return drain.drain([queue if q is None else q], tmp_path / f'run-{counter}', apply=apply)
    return types.SimpleNamespace(writer=writer, drain=drain, queue=queue, source=source,
                                 service=service, run=run, rawpath=rawpath, dbpath=dbpath)


def test_registered_drain_writes_only_status_readiness_and_appended_note(lane):
    before = copy.deepcopy(lane.service.rows)
    result = lane.run()
    assert result['results'][0]['status'] == 'applied', result
    after = lane.service.rows
    changed = [(r + 1, c) for r in range(len(before)) for c in range(27)
               if before[r][c] != after[r][c]]
    assert changed == [(759, 10), (759, 19), (759, 23)]
    assert result['cells_written'] == 3
    assert after[758][10] == 'hold_advertiser_postback_pending'
    assert after[758][19] is False
    assert after[758][23].startswith(lane.source[23] + '\n')
    assert f'email_received_at={WHEN}' in after[758][23]
    assert f'metadata_corrected_at={CORRECTED}' in after[758][23]
    assert 'Current network eligibility UNKNOWN' in after[758][23]
    receipt = json.loads((lane.drain.BUSINESS / result['results'][0]['receipt']).read_text())
    assert receipt['collateral_changes'] == []
    assert receipt['idempotent_rerun'] is True


def assert_refused_without_write(lane, result, reason):
    row = result['results'][0]
    assert row['status'] == 'refused', row
    assert reason in row['reason'], row
    assert result['cells_written'] == 0
    assert not lane.service.writes


def test_wrong_event_refuses(lane):
    lane.queue['evidence']['event_id'] = 'evt_synthetic_other'
    assert_refused_without_write(lane, lane.run(), 'exact_event_mismatch')


@pytest.mark.parametrize('axis', ['network', 'program_id', 'apply_url', 'duplicate'])
def test_wrong_or_ambiguous_identity_refuses(lane, axis):
    if axis in ('network', 'program_id'):
        lane.queue[axis] = 'Other' if axis == 'network' else '3278'
        reason = 'identity_or_projection_mismatch'
    elif axis == 'apply_url':
        lane.service.rows[758][5] += '0'
        reason = 'current_identity_missing_or_ambiguous'
    else:
        lane.service.rows[750] = list(lane.service.rows[758])
        reason = 'current_identity_missing_or_ambiguous'
    assert_refused_without_write(lane, lane.run(), reason)


def test_stale_full_preimage_refuses_even_with_same_status(lane):
    lane.service.rows[758][16] = 'newer payout, never overwrite'
    assert_refused_without_write(lane, lane.run(), 'full_preimage_or_postimage_required')


def test_exact_replay_is_zero_write_noop(lane):
    assert lane.run()['results'][0]['status'] == 'applied'
    post = copy.deepcopy(lane.service.rows)
    lane.service.writes.clear()
    result = lane.run()
    assert result['results'][0]['status'] == 'noop', result
    assert result['cells_written'] == 0
    assert lane.service.rows == post
    assert not lane.service.writes


def test_partial_postimage_is_not_a_replay(lane):
    assert lane.run()['results'][0]['status'] == 'applied'
    lane.service.writes.clear()
    lane.service.rows[758][19] = True
    assert_refused_without_write(lane, lane.run(), 'full_preimage_or_postimage_required')


def test_regression_mail_still_refused_by_portal_status_correction(lane):
    # Exact failure that led to GLP-250: do not add a fabricated portal field.
    lane.queue['kind'] = 'status_correction'
    assert_refused_without_write(lane, lane.run(),
                                 'status_correction_evidence_field_missing:portal_relationship')
    assert lane.service.rows[758][19] is True


def test_retained_raw_hash_drift_refuses(lane):
    lane.rawpath.write_bytes(RAW + b'tamper')
    assert_refused_without_write(lane, lane.run(), 'raw_hash_drift')


def test_unauthenticated_fixture_refuses_even_with_matching_hash(lane, monkeypatch):
    raw = RAW.replace(b'dmarc=pass', b'dmarc=fail')
    digest = hashlib.sha256(raw).hexdigest()
    lane.rawpath.write_bytes(raw)
    lane.queue['evidence']['raw_sha256'] = digest
    lane.queue['update_id'] = f'partner-email-hold-{EVENT}-{digest}'
    monkeypatch.setitem(lane.writer.PARTNER_EMAIL_HOLD_EVENT, 'raw_sha256', digest)
    with sqlite3.connect(lane.dbpath) as db:
        db.execute('UPDATE inbox_events SET raw_sha256=?', (digest,))
    assert_refused_without_write(lane, lane.run(), 'authentication_failed')


def test_historical_email_time_cannot_be_correction_time(lane):
    lane.queue['evidence']['corrected_at_utc'] = WHEN
    assert_refused_without_write(lane, lane.run(), 'correction_time_invalid')


def test_actual_preview_plans_three_cells_without_effect(lane):
    before = copy.deepcopy(lane.service.rows)
    db_sha = hashlib.sha256(lane.dbpath.read_bytes()).hexdigest()
    result = lane.run(apply=False)
    assert result['results'][0]['reason'] == 'dry_run', result
    assert result['results'][0]['planned_cells'] == 3
    assert result['cells_written'] == 0
    assert not lane.service.writes
    assert lane.service.rows == before
    assert hashlib.sha256(lane.dbpath.read_bytes()).hexdigest() == db_sha


def test_direct_writer_rejects_injected_tracker_cell(lane):
    entry = lane.drain.build_entry(KIND, lane.queue, lane.queue['evidence'], 759, lane.source)
    entry['cells']['14'] = 'https://example.test/forged'
    plan = lane.drain.plan_for(KIND, entry, lane.queue)
    with pytest.raises(RuntimeError, match='identity_or_projection_mismatch'):
        lane.writer.sheet_sync_plan_rows(KIND, plan)
    assert not lane.service.writes


def test_conflicting_same_update_id_refuses_before_dedupe(lane):
    other = copy.deepcopy(lane.queue)
    other['evidence']['source_row'][16] = 'conflicting payout'
    with pytest.raises(RuntimeError, match='partner_email_hold_duplicate_update_id_conflict'):
        lane.drain.dedupe_by_update_id([lane.queue, other])
    assert not lane.service.writes
