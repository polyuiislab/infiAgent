"""Local durable records. A failed commit is never reported as success."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path


class StateCommitError(RuntimeError):
    code = "STORAGE_UNAVAILABLE"


class StateCorruptError(RuntimeError):
    code = "STATE_CORRUPT"


def retry_windows_sharing(operation, *, timeout=2.0):
    """Retry Windows sharing/access conflicts during an atomic file replacement.

    Unlocked readers and antivirus scanners may briefly hold a handle that denies
    delete sharing. Keep the existing record intact, and propagate persistent
    permission failures; POSIX errors and other Windows errors are not retried.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            return operation()
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def write_json(path, data):
    path = Path(path)
    temporary = None
    try:
        # Serialize before touching the old record.
        raw = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        retry_windows_sharing(lambda: os.replace(temporary, path))
        temporary = None
        if os.name != "nt":
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except Exception as exc:
        raise StateCommitError(f"Cannot commit state: {path}") from exc
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def read_json(path, *, missing=None):
    path = Path(path)
    try:
        return json.loads(retry_windows_sharing(lambda: path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return missing
    except Exception as exc:
        raise StateCorruptError(f"Cannot read state: {path}") from exc
