#!/usr/bin/env python3
"""Privileged, read-only /proc veto; emit no process data or secrets."""
import os
from pathlib import Path
import sys


def main():
    target = os.path.realpath(sys.argv[1]).rstrip('/')
    caller = int(sys.argv[2])
    # The checked retirement handles belong to the caller, not a live builder.
    excluded = {os.getpid(), os.getppid(), caller}
    normalized = {}
    def used(value):
        if not value.startswith('/'):
            return False
        value = value.removesuffix(' (deleted)')
        if value not in normalized:
            normalized[value] = os.path.realpath(value)
        value = normalized[value]
        return value == target or value.startswith(target + '/')
    try:
        for entry in os.scandir('/proc'):
            if not entry.name.isdigit() or int(entry.name) in excluded:
                continue
            root = Path(entry.path)
            try:
                for name in ('cmdline', 'environ'):
                    if target.encode() in (root/name).read_bytes():
                        print('ACTIVE'); return 0
                for name in ('cwd', 'exe', 'root'):
                    try:
                        if used(os.readlink(root/name)):
                            print('ACTIVE'); return 0
                    except FileNotFoundError:
                        pass
                try:
                    with os.scandir(root/'fd') as fds:
                        for fd in fds:
                            try:
                                if used(os.readlink(fd.path)):
                                    print('ACTIVE'); return 0
                            except FileNotFoundError:
                                pass
                except FileNotFoundError:
                    pass
                try:
                    for line in (root/'maps').read_text(errors='replace').splitlines():
                        fields = line.split(maxsplit=5)
                        if len(fields) == 6 and used(fields[5]):
                            print('ACTIVE'); return 0
                except FileNotFoundError:
                    pass
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                print('UNKNOWN'); return 2
        print('INACTIVE'); return 0
    except OSError:
        print('UNKNOWN'); return 2


if __name__ == '__main__':
    raise SystemExit(main())
