"""Coordinate configuration edits with local SDK runs."""
from contextlib import contextmanager
from pathlib import Path
from utils.experience_store import _file_lock


@contextmanager
def configuration_access(user_root, *, write=False):
    with _file_lock(Path(user_root) / 'runtime' / 'configuration', shared=not write, blocking=not write):
        yield
