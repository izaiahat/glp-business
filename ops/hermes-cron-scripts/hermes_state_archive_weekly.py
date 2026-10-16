#!/usr/bin/env python3
"""Weekly dispatch only. Detached service owns final verified receipt."""
# CRON_DISCORD_HOUSE_STYLE_V1
# CRON_DISCORD_OUTPUT_DECLARATION_V1={"short_name": "State Archive", "output_class": "report", "composer_type": "no_agent_script_discord_fmt", "privacy_level": "internal", "fixture": "\u2699\ufe0f **State Archive** \u2014 REFUSED_PREFLIGHT; bytes UNKNOWN \u2192 UNKNOWN; effects_started=false; quiet window required", "fixture_output_class": "report", "fixture_test": "/home/ubuntu/business/scripts/hermes_state_archive/tests/test_weekly.py", "route": "1487300132054765629", "detail_policy": "complete_summary_in_discord"}
from pathlib import Path
import hashlib
import json
import subprocess
import sys
PIN_FILE = Path('/home/ubuntu/business/scripts/hermes_state_archive/weekly-pins.json')
PIN_HASH = '8fc8dc4d37f4fa3dae5f0e63a0ec2ce829e71c6994e736aad6a58ff051f41737'
def main():
    try:
        if hashlib.sha256(PIN_FILE.read_bytes()).hexdigest() != PIN_HASH:
            raise RuntimeError('pin_manifest_drift')
        for path, expected in json.loads(PIN_FILE.read_text())['files'].items():
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
                raise RuntimeError('source_pin_drift')
        p = subprocess.run(['systemctl', '--user', 'start', '--no-block',
                            'hermes-state-archive-weekly.service'],
                           capture_output=True, timeout=20, check=False)
        if p.returncode:
            raise RuntimeError('unit_launch_refused')
        # Starting the service does NOT establish an archive effect.
        print('[SILENT]')
        return 0
    except Exception as exc:
        print('⚙️ **State Archive** — launch blocked; ' + type(exc).__name__ + '; no archive completion claimed')
        return 1
if __name__ == '__main__':
    sys.exit(main())
