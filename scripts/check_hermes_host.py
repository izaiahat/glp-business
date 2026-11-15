#!/usr/bin/env python3
"""Hermes acceptance entrypoint: installed sources are mandatory, never portable."""
import os
from pathlib import Path


def main():
    # Override inherited CI flags/settings before pytest imports either suite.
    os.environ['HERMES_HOST_CHECK'] = '1'
    import pytest

    root = Path(__file__).resolve().parents[1]
    return pytest.main(['-q', '-p', 'no:cacheprovider',
                        str(root / 'tests/test_hermes_active_work_gate.py'),
                        str(root / 'tests/test_glp80_eden_policy.py')])


if __name__ == '__main__':
    raise SystemExit(main())
