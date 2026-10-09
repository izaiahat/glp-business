"""Protect extensionless database payloads in disposable inventories."""
import os
from pathlib import Path
import stat


def reject_database_payloads(root,rows):
    for row in rows:
        if not stat.S_ISREG(row[3]):continue
        p=Path(root)/row[0]
        fd=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_NOATIME)
        with os.fdopen(fd,'rb') as f:header=f.read(16)
        if header==b'SQLite format 3\0' or header[8:12]==b'DUCK':
            raise ValueError('protected database payload')
