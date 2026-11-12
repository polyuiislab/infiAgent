"""Local durable records. A failed commit is never reported as success."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class StateCommitError(RuntimeError):
    code = "STORAGE_UNAVAILABLE"


class StateCorruptError(RuntimeError):
    code = "STATE_CORRUPT"


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
        os.replace(temporary, path)
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
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return missing
    except Exception as exc:
        raise StateCorruptError(f"Cannot read state: {path}") from exc
