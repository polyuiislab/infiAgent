"""Task-local human interaction records, with unambiguous occurrence IDs."""
from __future__ import annotations

import hashlib
import threading
import uuid
from pathlib import Path

from utils.atomic_state import read_json, write_json
from utils.execution_scope import CURRENT, TOOL_CALL
from utils.user_paths import get_user_data_root

_paths = set()
_lock = threading.RLock()


def create(task_id, hil_id, instruction):
    task = str(Path(task_id).resolve())
    scope = CURRENT.get()
    call = TOOL_CALL.get() or uuid.uuid4().hex
    key = hashlib.sha256(f'{task}|{scope.agent_id if scope else ""}|{call}|{hil_id}'.encode()).hexdigest()
    occurrence = 'hil_' + key[:32]
    path = Path(task) / '.sdk_runtime' / 'interactions' / (occurrence + '.json')
    with _lock:
        record = read_json(path)
        if record is None:
            record = dict(occurrence_id=occurrence, hil_id=hil_id, instruction=instruction,
                          task_id=task, status='waiting', result=None)
            write_json(path, record)
        _paths.add(path)
    return path, record


def records(task_id=None):
    with _lock:
        paths = set(_paths)
    if task_id:
        paths.update((Path(task_id).resolve() / '.sdk_runtime' / 'interactions').glob('*.json'))
    else:
        paths.update((get_user_data_root() / 'tasks').glob('**/.sdk_runtime/interactions/*.json'))
    result = []
    for path in sorted(paths):
        value = read_json(path)
        if value and (task_id is None or value['task_id'] == str(Path(task_id).resolve())):
            result.append((path, value))
    return result


def find(reference, task_id=None):
    candidates = [(path, rec) for path, rec in records(task_id)
                  if rec['occurrence_id'] == reference or rec['hil_id'] == reference]
    exact = [x for x in candidates if x[1]['occurrence_id'] == reference]
    waiting = [x for x in candidates if x[1]['status'] == 'waiting']
    chosen = exact or waiting or candidates
    if len(chosen) > 1:
        raise ValueError('Ambiguous hil_id; provide occurrence_id or task_id')
    return chosen[0] if chosen else (None, None)


def answer(reference, response, task_id=None):
    from utils.experience_store import _file_lock
    with _lock:
        path, record = find(reference, task_id)
        if not record:
            return {'success': False, 'error': 'HIL task not found'}
        with _file_lock(path):
            record = read_json(path)
            if record['status'] == 'completed':
                return {'success': record['result'] == response, 'occurrence_id': record['occurrence_id']}
            if record['status'] != 'waiting':
                return {'success': False, 'error': 'HIL task is no longer waiting'}
            write_json(path, {**record, 'status': 'completed', 'result': response})
            return {'success': True, 'occurrence_id': record['occurrence_id']}


def end_wait(path, status):
    from utils.experience_store import _file_lock
    with _file_lock(path):
        record = read_json(path)
        if record['status'] == 'waiting':
            write_json(path, {**record, 'status': status})
