"""Private, bounded CJ read continuations. No provider I/O or financial writes."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


@contextmanager
def checkpoint(path, context):
    if path is None:
        yield None, lambda value: None
        return
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path)+'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = None
        if path.exists() or path.is_symlink():
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as source:
                stat = os.fstat(source.fileno())
                if stat.st_uid != os.getuid() or stat.st_mode & 0o077 or stat.st_size > 32*1024*1024:
                    raise ValueError('cj_checkpoint_unsafe')
                doc = json.loads(source.read())
            envelope = doc['envelope']
            if doc['sha256'] != digest(envelope) or envelope['context'] != context:
                raise ValueError('cj_checkpoint_context_or_hash_mismatch')
            captured = datetime.fromisoformat(envelope['created_at'])
            now = datetime.now(timezone.utc)
            if not now-timedelta(days=8) <= captured <= now:
                raise ValueError('cj_checkpoint_expired_operator_refresh_required')
            state = envelope['state']
        else:
            captured = datetime.now(timezone.utc)
        def save(value):
            envelope = {'context': context, 'created_at': captured.isoformat(), 'state': value}
            doc = {'envelope': envelope, 'sha256': digest(envelope)}
            fd, name = tempfile.mkstemp(prefix='.'+path.name, dir=path.parent)
            try:
                with os.fdopen(fd, 'w') as dest:
                    json.dump(doc, dest, sort_keys=True)
                    dest.flush()
                    os.fsync(dest.fileno())
                os.replace(name, path)
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
        yield state, save
