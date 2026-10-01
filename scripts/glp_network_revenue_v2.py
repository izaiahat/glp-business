#!/usr/bin/env python3
"""F08 additive v2 normalization; no remote writes and no v1 ledger migration.

Only typed registry identities and accounting allowlist fields survive. Currency
exponents are deliberately bounded: unsupported currencies must be reviewed.
"""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

SCHEMA = 'glp-network-revenue-v2'
IDENTITY = ('network', 'account_id', 'tenant_id', 'program_id')
EXPONENT = {'USD': 2, 'EUR': 2, 'GBP': 2, 'CAD': 2, 'AUD': 2, 'JPY': 0, 'KWD': 3, 'BHD': 3}
TEST_ID = 'cs-m1b-affiliati-jrnys-20260828'


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def token(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}', value):
        raise ValueError('identity_invalid')
    return value


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp_missing')
    t = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if t.tzinfo is None:
        raise ValueError('timezone_required')
    return t.astimezone(timezone.utc).isoformat(timespec='microseconds')


def money(value, currency):
    if value is None or value == '':
        return None
    if currency not in EXPONENT or isinstance(value, bool):
        raise ValueError('currency_or_money_unsupported')
    if not re.fullmatch(r'-?\d+(?:\.\d+)?', str(value)):
        raise ValueError('money_invalid')
    try:
        scaled = Decimal(str(value)) * (10 ** EXPONENT[currency])
        if not scaled.is_finite() or scaled != scaled.to_integral_value() or abs(scaled) > 2**63-1:
            raise ValueError('money_precision_or_range')
        return int(scaled)
    except InvalidOperation:
        raise ValueError('money_invalid') from None


def normalize(raw, identity, registry):
    ids = [token(identity.get(k)) for k in IDENTITY]
    matches = [i for i in registry if [i.get(k) for k in IDENTITY] == ids and i.get('status') == 'verified']
    if len(matches) != 1:
        raise ValueError('identity_unresolved_or_ambiguous')
    bound = matches[0]
    if bound.get('tenant_kind') not in {'not_applicable', 'host'} or not bound.get('evidence_ref'):
        raise ValueError('identity_registry_incomplete')
    if (bound['tenant_kind'] == 'not_applicable') != (ids[2] == 'not_applicable'):
        raise ValueError('tenant_kind_mismatch')
    if 'fella' in str(bound.get('provider_slug', '')).casefold():
        raise ValueError('excluded_fella')
    if not bound.get('provider_slug'):
        raise ValueError('provider_exclusion_identity_unresolved')
    event = token(raw.get('provider_event_id'))
    kind = raw.get('event_type')
    if kind not in {'commission', 'settlement'}:
        raise ValueError('event_type_invalid')
    currency = raw.get('currency')
    if currency is not None and currency not in EXPONENT:
        raise ValueError('currency_unsupported')
    status = token(raw.get('source_status'))
    mapped = raw.get('mapped_status', 'unknown')
    if mapped not in {'pending', 'approved', 'reversed', 'rejected', 'payable', 'paid', 'unknown'}:
        raise ValueError('mapped_status_invalid')
    classification = raw.get('classification', 'UNKNOWN')
    if classification not in {'TEST', 'REAL', 'UNKNOWN'}:
        raise ValueError('classification_invalid')
    if event == TEST_ID or raw.get('is_test') is True:
        classification = 'TEST'
    attribution = raw.get('attribution', 'UNKNOWN')
    if attribution not in {'GLP', 'NON_GLP', 'UNKNOWN'}:
        raise ValueError('attribution_invalid')
    receipt_sha = raw.get('receipt_sha256')
    if not isinstance(receipt_sha, str) or not re.fullmatch('[a-f0-9]{64}', receipt_sha):
        raise ValueError('receipt_hash_required')
    pointer = raw.get('receipt_pointer')
    if not isinstance(pointer, str) or not re.fullmatch(r'/[A-Za-z0-9_/-]{0,255}', pointer):
        raise ValueError('receipt_pointer_invalid')
    revision = raw.get('source_revision_id')
    if revision is not None:
        token(revision)
    result = dict(zip(IDENTITY, ids))
    result.update(schema=SCHEMA, provider_event_id=event, event_type=kind,
                  source_status=status, mapped_status=mapped,
                  occurred_at=timestamp(raw.get('occurred_at')),
                  source_updated_at=timestamp(raw.get('source_updated_at')),
                  source_revision_id=revision, currency=currency,
                  commission_minor=money(raw.get('commission'), currency),
                  net_paid_minor=money(raw.get('net_paid'), currency),
                  classification=classification, attribution=attribution,
                  authenticated=raw.get('authenticated') is True,
                  receipt_sha256=receipt_sha, receipt_pointer=pointer)
    result['logical_key'] = digest([SCHEMA, *ids, event, kind] + ([currency] if kind == 'settlement' else []))
    financial = {k: result[k] for k in ('commission_minor', 'net_paid_minor', 'mapped_status', 'classification', 'attribution')}
    result['revision_key'] = digest([result['logical_key'], revision, status, result['occurred_at'],
                                     result['source_updated_at'], currency, digest(financial)])
    return result


class Store:
    """Dedicated local reporting DB; refuses every pre-existing foreign schema.

    Revisions and provenance are immutable. Conflicting latest source timestamps
    produce no accepted current state, rather than choosing retrieval order.
    """
    def __init__(self, path):
        import os
        import sqlite3
        from pathlib import Path
        path = Path(path)
        if path.exists():
            check = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables != {'reporting_v2_meta', 'reporting_v2_revisions'}:
                    raise ValueError('not_a_dedicated_reporting_v2_database')
                if check.execute('SELECT schema FROM reporting_v2_meta').fetchall() != [(SCHEMA,)]:
                    raise ValueError('reporting_schema_mismatch')
            finally:
                check.close()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS reporting_v2_meta(schema TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reporting_v2_revisions(
          revision_key TEXT PRIMARY KEY, logical_key TEXT NOT NULL,
          source_updated_at TEXT NOT NULL, payload TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS reporting_v2_logical ON reporting_v2_revisions(logical_key,source_updated_at);
        CREATE TRIGGER IF NOT EXISTS reporting_v2_no_update BEFORE UPDATE ON reporting_v2_revisions
          BEGIN SELECT RAISE(ABORT,'append_only'); END;
        CREATE TRIGGER IF NOT EXISTS reporting_v2_no_delete BEFORE DELETE ON reporting_v2_revisions
          BEGIN SELECT RAISE(ABORT,'append_only'); END;
        CREATE VIEW IF NOT EXISTS reporting_v2_current AS
          SELECT r.* FROM reporting_v2_revisions r
          WHERE r.source_updated_at=(SELECT max(s.source_updated_at) FROM reporting_v2_revisions s WHERE s.logical_key=r.logical_key)
          AND 1=(SELECT count(*) FROM reporting_v2_revisions s WHERE s.logical_key=r.logical_key AND s.source_updated_at=r.source_updated_at);
        ''')
        if not self.db.execute('SELECT 1 FROM reporting_v2_meta').fetchone():
            self.db.execute('INSERT INTO reporting_v2_meta VALUES (?)', (SCHEMA,))
        self.db.commit()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def append(self, record):
        expected = set(IDENTITY) | {'schema','provider_event_id','event_type','source_status','mapped_status',
            'occurred_at','source_updated_at','source_revision_id','currency','commission_minor','net_paid_minor',
            'classification','attribution','authenticated','receipt_sha256','receipt_pointer','logical_key','revision_key'}
        if set(record) != expected or record['schema'] != SCHEMA:
            raise ValueError('normalized_allowlist_required')
        with self.db:
            old = self.db.execute('SELECT payload FROM reporting_v2_revisions WHERE revision_key=?', (record['revision_key'],)).fetchone()
            if old:
                # New receipt provenance cannot replace the prior immutable capture.
                return 'duplicate'
            self.db.execute('INSERT INTO reporting_v2_revisions VALUES (?,?,?,?)',
                (record['revision_key'], record['logical_key'], record['source_updated_at'], canonical(record)))
        return 'inserted'

    def count(self):
        return self.db.execute('SELECT count(*) FROM reporting_v2_revisions').fetchone()[0]

    def current(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM reporting_v2_current ORDER BY logical_key')]

    def payment_candidates(self):
        result = []
        for r in self.current():
            if (r['event_type'] == 'settlement' and r['mapped_status'] == 'paid'
                and r['authenticated'] and r['classification'] == 'REAL' and r['attribution'] == 'GLP'
                and r['net_paid_minor'] is not None and r['net_paid_minor'] > 0 and r['currency']):
                result.append({'schema': 'glp-payment-local-candidate-v1', 'business': 'GLP Clinic Scout',
                    'candidate_id': r['revision_key'], 'amount_minor': r['net_paid_minor'], 'currency': r['currency'],
                    'reference': r['provider_event_id'], 'network': r['network'], 'account_id': r['account_id'],
                    'tenant_id': r['tenant_id'], 'program_id': r['program_id'], 'classification': 'REAL',
                    'attribution': 'GLP', 'receipt_sha256': r['receipt_sha256'], 'receipt_pointer': r['receipt_pointer'],
                    'target_channel': '1525219463866089566', 'cash_basis': 'provider_reported_not_bank_verified',
                    'delivery_state': 'LOCAL_CANDIDATE_NOT_SENT'})
        return result
