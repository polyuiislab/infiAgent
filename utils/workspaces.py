"""Local shared files with task-local conversation state. No tenant model."""
from __future__ import annotations

import contextlib
import contextvars
import re
from pathlib import Path
from utils.atomic_state import read_json, write_json
from utils.user_paths import get_user_data_root

_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
_HELD_FILE_LOCKS = contextvars.ContextVar('infiagent_file_access_locks', default=None)


def _base(workspace_id):
    if not isinstance(workspace_id, str) or not _ID.fullmatch(workspace_id):
        raise ValueError('workspace_id must contain 1–80 letters, digits, underscores or hyphens')
    root = get_user_data_root().resolve() / 'workspaces'
    if root.is_symlink():
        raise ValueError('Workspace registry cannot be a symlink')
    path = root / workspace_id
    if path.resolve().parent != root.resolve() or path.is_symlink():
        raise ValueError('Workspace symlinks are not supported')
    return path


def create(workspace_id, title='', concurrency='serial'):
    if concurrency not in {'serial', 'file_lock'}:
        raise ValueError('Workspace concurrency must be serial or file_lock')
    from utils.experience_store import _file_lock
    base = _base(workspace_id)
    base.mkdir(parents=True, exist_ok=True)
    with _file_lock(base / 'workspace'):
        record = read_json(base / 'workspace.json')
        if record is None:
            record = {'id': workspace_id, 'title': str(title or workspace_id), 'concurrency': concurrency}
            write_json(base / 'workspace.json', record)
        (base / 'files').mkdir(exist_ok=True)
    return {**record, 'path': str(base / 'files')}


def list_workspaces():
    root = get_user_data_root() / 'workspaces'
    result = []
    for base in sorted(root.glob('*')):
        if base.is_dir() and not base.is_symlink() and _ID.fullmatch(base.name):
            record = read_json(base / 'workspace.json')
            if record:
                result.append({**record, 'path': str(base / 'files')})
    return result


def attach(task_id, workspace_id):
    base = _base(workspace_id)
    if not (base / 'workspace.json').is_file():
        raise ValueError('Workspace does not exist')
    path = Path(task_id).resolve() / '.sdk_runtime' / 'workspace.json'
    previous = read_json(path)
    if previous and previous['workspace_id'] != workspace_id:
        raise ValueError('An existing task cannot silently switch workspaces; start a new task')
    write_json(path, {'workspace_id': workspace_id})
    return files_root(task_id)


def files_root(task_id):
    task = Path(task_id).resolve()
    binding = read_json(task / '.sdk_runtime' / 'workspace.json')
    if not binding:
        return task
    base = _base(binding['workspace_id'])
    files = base / 'files'
    if not (base / 'workspace.json').is_file() or not files.is_dir() or files.is_symlink():
        raise ValueError('Workspace is missing or invalid')
    return files


def concurrency(task_id):
    task = Path(task_id).resolve()
    binding = read_json(task / '.sdk_runtime' / 'workspace.json')
    if not binding:
        return 'serial'
    record = read_json(_base(binding['workspace_id']) / 'workspace.json')
    if not isinstance(record, dict) or record.get('concurrency', 'serial') not in {'serial', 'file_lock'}:
        raise ValueError('Invalid workspace configuration')
    return record.get('concurrency', 'serial')


def resolve_file(task_id, relative):
    root = files_root(task_id).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('File is outside the workspace')
    if '.sdk_runtime' in path.relative_to(root).parts:
        raise ValueError('Runtime records are not workspace files')
    return path


@contextlib.contextmanager
def file_access(task_id, *, shared=False, blocking=True):
    """Serialize file-mutating tool calls across tasks sharing a workspace."""
    from utils.experience_store import _file_lock
    root = files_root(task_id)
    lock_path = root / '.sdk_runtime' / 'files' if root == Path(task_id).resolve() else root.parent / 'files'
    key = str(lock_path)
    held = _HELD_FILE_LOCKS.get() or {}
    if key in held:
        if held[key] and not shared:
            raise ValueError('A read-only tool cannot upgrade its file-access lock')
        yield
    else:
        with _file_lock(lock_path, shared=shared, blocking=blocking):
            token = _HELD_FILE_LOCKS.set({**held, key: shared})
            try:
                yield
            finally:
                _HELD_FILE_LOCKS.reset(token)
