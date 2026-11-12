"""Single-host task ownership, including SDK preparation before agent entry."""
from __future__ import annotations

import contextvars
import errno
import os
from contextlib import contextmanager
from pathlib import Path

_HELD = contextvars.ContextVar('infiagent_owned_tasks', default=frozenset())


@contextmanager
def task_owner(task_id):
    root = Path(task_id).resolve()
    key = str(root)
    held = _HELD.get()
    if key in held:
        yield True
        return
    path = root / '.sdk_runtime' / 'run-owner.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        acquired = False
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno not in {errno.EAGAIN, errno.EACCES}:
                raise
        if not acquired:
            yield False
            return
        token = _HELD.set(held | {key})
        try:
            yield True
        finally:
            _HELD.reset(token)
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
