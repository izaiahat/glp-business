"""Exact-source AST unit slice; omitted host imports are NOT integration coverage."""
def load():
    source = Path(__file__).resolve().parents[1] / 'scripts/affiliate_approval_auto_reconciler.py'
    tree = ast.parse(source.read_text())
    chosen, found = [], set()
    for node in tree.body:
        name = getattr(node, 'name', None)
        if isinstance(node, ast.Assign):
            name = getattr(node.targets[0], 'id', None)
        if name in FUNCTIONS | CONSTANTS:
            chosen.append(node)
            found.add(name)
    assert found == FUNCTIONS | CONSTANTS, (FUNCTIONS | CONSTANTS) - found
    def forbidden(*args, **kwargs):
        raise AssertionError('Uncovered provider/Sheet edge must not run')
    module = types.ModuleType('inbox_notice_unit_slice')
    module.__dict__.update(globals())
    module.load_snapshot = forbidden
    module.n8n_dt = types.SimpleNamespace(insert=forbidden)
    exec(compile(ast.Module(chosen, type_ignores=[]), str(source), 'exec'), module.__dict__)
    return module


import ast, asyncio, argparse, hashlib, json, sqlite3, sys, re, types, html, os
from pathlib import Path
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from inbox_control_plane.affiliate_network_registry import network_for_domain, network_for_text
from affiliate_sheet_schema_guard import EXPECTED_HEADERS
CONSTANTS = set('BUSINESS INBOX_DB STATE_DB SHEET_ID TAB PUBLISHER_AWIN PUBLISHER_PARTNERIZE KATALYS_ORG KATALYS_AFF AUTHORITY_MESSAGE AUTOMATION_ID REPORT_DIR HEADERS SIGNALS MAXBOUNTY_SIGNAL_PATTERNS TERMINAL_EVENT_STATES DIRECT_OPERATOR_NETWORKS DIRECT_PARTNER_DOMAINS DIRECT_PARTNER_NETWORKS DIRECT_ROUTINE_OUTCOME_DOMAINS DIRECT_PARTNER_PROGRAMS AFFILIATE_AUTH_DOMAINS DIRECT_OPERATOR_PROGRAMS OPERATOR_PREREQUISITE_RE IDENTITY_PREREQUISITE_RE'.split())

FUNCTIONS = set('AffiliateReviewRequired AffiliateCandidateParked now norm sha pad init_state mark_state message_text latest_unquoted_reply maxbounty_signal ordered_unique impact_typed_identifiers detect private_bytes save_receipt process_event process_selected_events reconcile_campaign_cancellation verify_affiliate_authentication terms_notice_recovery_allowed main load_control_plane fetch_inbox_events prepare_sheet_schema approval_test_label require_operator_ruling_clear google_service gmail_service'.split())
