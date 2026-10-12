#!/usr/bin/env python3
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name":"Swap Cleaner","output_class":"alert","composer_type":"deterministic_script","privacy_level":"internal","fixture":"✅ **Swap Cleaner** — idle maintenance completed\n\nDormant processes ended: **2** · swap files rotated: **1**\n→ 6GiB RAM reserve, low-load/PSI/IO guards and per-file daily cooldown preserved\n-# detail: /home/ubuntu/.hermes/runstate/host-swap-cleaner/latest.json","fixture_output_class":"alert","fixture_test":"python3 /home/ubuntu/business/tests/test_host_swap_cleaner.py","route":"local","detail_policy":"complete_summary_in_discord","success_silence":true}
"""Pinned no-agent host maintenance launcher. Local receipts; no third-party send."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

SOURCE = Path('/home/ubuntu/business/scripts/host_swap_cleaner.py')
EXPECTED = 'a546fa4eaf85151780b9e0d08636b3164f8b38cad2ddcc9f540373403296b16f'
if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != EXPECTED:
    print('🔴 **Swap Cleaner** — source pin mismatch; no maintenance executed')
    sys.exit(1)
run = subprocess.run(['/usr/bin/python3', str(SOURCE), '--apply', *sys.argv[1:]], capture_output=True, text=True)
if run.returncode:
    print('🔴 **Swap Cleaner** — maintenance failed; inspect local receipt before retry\n-# detail: /home/ubuntu/.hermes/runstate/host-swap-cleaner/latest.json')
    print(run.stderr[-1000:], file=sys.stderr)
    sys.exit(run.returncode)
receipt = json.loads(run.stdout)
reaped = len(receipt.get('reaper', {}).get('terminated', []))
rotated = len(receipt.get('rotations', []))
if reaped or rotated:
    print(f'✅ **Swap Cleaner** — idle maintenance completed\n\nDormant processes ended: **{reaped}** · swap files rotated: **{rotated}**\n→ 6GiB RAM reserve, low-load/PSI/IO guards and per-file daily cooldown preserved\n-# detail: /home/ubuntu/.hermes/runstate/host-swap-cleaner/latest.json')
