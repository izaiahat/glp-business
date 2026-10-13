#!/home/ubuntu/.hermes/venvs/rook-mcp/bin/python
"""Supervised launch gate for the Rook MCP adapter (user systemd).

Each start takes a FRESH exact-channel cursor (latest #rook message id), so a
restart never replays old requests; messages posted while the adapter is down
are skipped (documented gap). Grants, scopes, state dir and writer lock are the
adapter's own; this gate adds nothing else.
"""
import os
import sys

CHECKOUT = '/home/ubuntu/business'
sys.path.insert(0, CHECKOUT + '/scripts')
import rook_mcp_events as m  # noqa: E402

latest = m.read_messages({'limit': 1})['structuredContent']['messages'][0]['message_id']
if not latest.isdigit():
    raise SystemExit('cursor read failed')
argv = [sys.executable, '-B', CHECKOUT + '/scripts/rook_mcp_events.py', '--overnight-pilot',
        '--local-tunnel-metadata', '--issuer', 'https://dev-qv5n6hpb2umayqwt.us.auth0.com/',
        '--audience', 'https://api.openai.com/v1/mcp/tunnel_6abc70ee3f548191bfaabda8ee682eee',
        '--operator-subject', 'google-oauth2|102663398483169401447',
        '--state-dir', '/home/ubuntu/.hermes/state/rook-mcp-pilot', '--port', '18796',
        '--after-message', latest, '--max-run-seconds', '57600']
print(f'launch cursor={latest}', flush=True)
os.execv(argv[0], argv)
