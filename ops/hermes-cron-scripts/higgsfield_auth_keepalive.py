#!/usr/bin/env python3
"""Silent healthy cron wrapper for Higgsfield OAuth refresh."""

from pathlib import Path
import subprocess
import sys

script = Path.home() / ".hermes" / "scripts" / "higgsfield_auth_refresh.py"
result = subprocess.run([sys.executable, str(script), "--quiet-healthy"], text=True)
raise SystemExit(result.returncode)
