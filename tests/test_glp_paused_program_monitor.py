import importlib.util
import json
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    'paused_monitor', Path(__file__).resolve().parents[1] / 'scripts/glp_paused_program_monitor.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def inbox(tmp_path, rows):
    db = tmp_path / 'inbox.sqlite3'
    con = sqlite3.connect(db)
    con.execute('create table inbox_events(event_id, sender_domain, subject, received_at, provider_message_id)')
    con.executemany('insert into inbox_events values (?,?,?,?,?)', rows)
    con.commit()
    return db


def row(n, name, network, status):
    cells = [''] * 27
    cells[2], cells[4], cells[10] = name, network, status
    return {'sheet_row': n, 'cells': cells}


def test_trimi_pause_notice_queues_one_sole_writer_pause_and_skips_recorded(tmp_path):
    db = inbox(tmp_path, [
        ('evt_p', 'katalys.com', 'Your Contract for Pallas Health has ended', '2026-09-30T13:44:08Z', 'm1'),
        ('evt_t', 'katalys.com', 'The Program Trimi GLP-1  Has Been Paused ⏸️', '2026-10-08T16:08:49Z', 'm2'),
        ('evt_g0', 'katalys.com', 'The Program Gala HRT Has Been Paused ⏸️', '2026-09-01T00:00:00Z', 'm3'),
        ('evt_g1', 'katalys.com', 'Advertiser Gala has Activated Their Program Gala HRT ! 🚀', '2026-09-28T00:00:00Z', 'm4'),
        ('evt_x', 'katalys.com', 'Weekly newsletter', '2026-10-01T00:00:00Z', 'm5'),
    ])
    events = monitor.lifecycle_events(db, tmp_path / 'absent.sqlite3')
    assert sorted(e['lifecycle'] for e in events) == ['active', 'ended', 'paused']
    snapshot = {'rows': [row(132, 'Trimi GLP-1 ', 'Katalys', 'live_wired_verified'),
                         row(776, 'Pallas Health', 'Katalys', 'ended_operator_contract_termination'),
                         row(140, 'Gala HRT', 'Katalys', 'live_wired_verified')]}
    queue, outcomes = monitor.decide(snapshot, events)
    assert len(queue) == 1
    q = queue[0]
    assert (q['kind'], q['sheet_row'], q['update_id']) == ('network_paused', 132, 'paused-monitor-evt_t')
    evidence = json.loads(q['evidence'])
    assert evidence['message_id'] == 'inbox:evt_t' and evidence['path'].startswith('reports/')
    decisions = {o['event_id']: o['decision'] for o in outcomes}
    assert decisions == {'evt_p': 'already_recorded', 'evt_t': 'queued', 'evt_g1': 'latest_notice_active'}
    # Idempotent: an already-queued notice is not queued again.
    assert monitor.decide(snapshot, events, frozenset({'paused-monitor-evt_t'}))[0] == []
