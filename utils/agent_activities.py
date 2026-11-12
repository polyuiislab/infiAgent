"""Bounded, durable local child-agent activities. No services or database required.

Specs are committed before submission; results survive a parent's interrupted
collect. Unknown external side effects are never claimed to be exactly-once.
"""
from __future__ import annotations

import contextvars
import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from utils.atomic_state import read_json, write_json, StateCommitError
from utils.execution_scope import ExecutionScope, CURRENT, AgentCancelled, bind, check_cancelled


def positive_limit(name, default):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if not 1 <= value <= 256:
        raise ValueError(f"{name} must be between 1 and 256")
    return value


class AgentActivities:
    def __init__(self, task_id, owner, run_child, *, scope=None, message_count=None, snapshot_parent=None):
        self.task_id = str(task_id)
        self.owner = str(owner)
        self.run_child = run_child
        self.scope = scope or CURRENT.get() or ExecutionScope()
        self.message_count = message_count or (lambda: 0)
        self.snapshot_parent = snapshot_parent or (lambda: [])
        self.root = Path(task_id) / ".sdk_runtime" / "agent_activities" / hashlib.sha256(self.owner.encode()).hexdigest()[:24]
        self.limit = positive_limit("MLA_PARALLEL_AGENTS_MAX", 4)
        self.depth_limit = positive_limit("MLA_SPAWN_MAX_DEPTH", 1)
        self.pool = ThreadPoolExecutor(max_workers=self.limit, thread_name_prefix="infiagent-child")
        self.lock = threading.RLock()
        self.records = {}
        self.futures = {}
        self.scopes = {}
        self.storage_error = None
        self.closed = False
        for path in sorted(self.root.glob("activity_*.json")):
            rec = read_json(path)
            if not isinstance(rec, dict) or rec.get("owner") != self.owner or rec.get("handle") != path.stem:
                raise ValueError(f"Invalid child activity record: {path.name}")
            self.records[rec['handle']] = rec

    def _save(self, record):
        write_json(self.root / (record['handle'] + '.json'), record)
        self.records[record['handle']] = record

    def _healthy(self):
        if self.storage_error:
            raise self.storage_error

    def submit(self, name, task_input, call_id, index=0, context_mode='tool', background=True):
        check_cancelled()
        if self.closed:
            raise RuntimeError("Child scheduler is closed")
        if background and self.scope.depth >= self.depth_limit:
            raise ValueError("Child-agent spawn depth exceeded")
        ancestor = self.scope
        nesting = 0
        while ancestor is not None:
            nesting += 1
            ancestor = ancestor.parent_scope
        if nesting >= positive_limit('MLA_AGENT_MAX_DEPTH', 16):
            raise ValueError('Agent call depth exceeded')
        if context_mode not in {'tool', 'fresh', 'inherit'}:
            raise ValueError("context_mode must be tool, fresh or inherit")
        handle = 'activity_' + hashlib.sha256(f'{self.owner}|{call_id}|{index}'.encode()).hexdigest()[:32]
        with self.lock:
            self._healthy()
            rec = self.records.get(handle)
            if rec is None:
                live = sum(r['status'] in {'prepared', 'running'} for r in self.records.values())
                if live >= self.limit * 16:
                    raise ValueError("Child-agent queue capacity exceeded; collect existing activities")
                rec = dict(handle=handle, owner=self.owner, agent=name, task_input=task_input,
                           call_id=str(call_id), index=index, context_mode=context_mode,
                           background=bool(background),
                           status='prepared', desired='running', acknowledged=False)
                rec['context_seed'] = self.snapshot_parent() if context_mode == 'inherit' else []
                self._save(rec)
            elif (rec['agent'], rec['task_input'], rec['context_mode']) != (name, task_input, context_mode):
                raise ValueError("Activity identity reused with different arguments")
            self._schedule(rec)
        return handle

    def restore(self):
        with self.lock:
            self._healthy()
            for rec in list(self.records.values()):
                self._schedule(rec)

    def _schedule(self, rec):
        handle = rec['handle']
        if handle in self.futures or rec['status'] not in {'prepared', 'running'}:
            return
        if rec.get('desired') == 'cancelled':
            self._save({**rec, 'status': 'cancelled', 'result': {'status': 'cancelled', 'output': ''}})
            return
        scope = ExecutionScope(parent_id=self.owner, instance_key=handle,
                               depth=self.scope.depth + int(rec.get('background', True)),
                               parent_cancel=self.scope.cancel, context_mode=rec['context_mode'],
                               context_seed=rec.get('context_seed', []), parent_scope=self.scope,
                               usage_lock=self.scope.usage_lock)
        self.scopes[handle] = scope
        context = contextvars.copy_context()
        root = self.scope
        while root.parent_scope is not None:
            root = root.parent_scope
        with root.usage_lock:
            count = getattr(root, '_active_children', 0)
            if count >= positive_limit('MLA_ACTIVE_AGENTS_MAX', 64):
                self._save({**rec, 'status': 'completed', 'result': {'status': 'error', 'output': '',
                            'error_code': 'AGENT_CAPACITY', 'error_information': 'Run child-agent capacity reached'}})
                return
            root._active_children = count + 1
        def released(_future=None):
            with root.usage_lock:
                root._active_children -= 1
        try:
            self.futures[handle] = self.pool.submit(context.run, self._work, rec, scope)
            self.futures[handle].add_done_callback(released)
        except BaseException:
            released()
            raise

    def _work(self, rec, scope):
        try:
            with bind(scope):
                check_cancelled()
                with self.lock:
                    self._save({**self.records[rec['handle']], 'status': 'running'})
                result = self.run_child(rec['agent'], rec['task_input'], rec['context_mode'])
                check_cancelled()
        except AgentCancelled:
            result = {'status': 'cancelled', 'output': ''}
        except StateCommitError as exc:
            with self.lock:
                self.storage_error = exc
            return
        except Exception as exc:
            result = {'status': 'error', 'output': '', 'error_information': str(exc)}
        try:
            with self.lock:
                current = self.records[rec['handle']]
                if current.get('desired') == 'cancelled':
                    result = {'status': 'cancelled', 'output': ''}
                status = 'cancelled' if result.get('status') == 'cancelled' else 'completed'
                self._save({**current, 'status': status, 'result': result})
        except StateCommitError as exc:
            with self.lock:
                self.storage_error = exc

    def collect(self, handles=None, timeout_sec=0, k=None):
        timeout = float(timeout_sec or 0)
        if not 0 <= timeout <= 86400:
            raise ValueError("timeout_sec must be between 0 and 86400")
        with self.lock:
            targets = list(handles) if handles is not None else [h for h, r in self.records.items() if not r.get('acknowledged')]
            targets = list(dict.fromkeys(targets))
            unknown = [h for h in targets if h not in self.records]
            if unknown:
                raise ValueError("Unknown activity handle(s)")
        count = len(targets) if k is None else int(k)
        if targets and not 1 <= count <= len(targets):
            raise ValueError("k must be between 1 and the number of handles")
        deadline = time.monotonic() + timeout
        initial_messages = self.message_count()
        interrupted = False
        while True:
            check_cancelled()
            with self.lock:
                self._healthy()
                done = [h for h in targets if self.records[h]['status'] in {'completed', 'cancelled'}]
            if len(done) >= count or time.monotonic() >= deadline:
                break
            if self.message_count() > initial_messages:
                interrupted = True
                break
            time.sleep(min(.05, max(0, deadline - time.monotonic())))
        with self.lock:
            return {'status': 'success', 'results': [dict(handle=h, agent=self.records[h]['agent'],
                        result=self.records[h]['result']) for h in done],
                    'pending': [h for h in targets if h not in done],
                    'interrupted_by_user_message': interrupted}

    def acknowledge(self, handles):
        with self.lock:
            for handle in handles:
                rec = self.records[handle]
                if rec['status'] not in {'completed', 'cancelled'}:
                    raise ValueError("Cannot acknowledge a running activity")
                if not rec.get('acknowledged'):
                    self._save({**rec, 'acknowledged': True})

    def outstanding(self):
        with self.lock:
            self._healthy()
            return [h for h, r in self.records.items() if not r.get('acknowledged')]

    def cancel(self, handles=None):
        with self.lock:
            targets = list(handles) if handles is not None else list(self.records)
            if any(h not in self.records for h in targets):
                raise ValueError("Unknown activity handle(s)")
            for handle in targets:
                rec = self.records[handle]
                if rec['status'] in {'completed', 'cancelled'}:
                    continue
                self._save({**rec, 'desired': 'cancelled'})
                if handle in self.scopes:
                    self.scopes[handle].cancel.set()
                future = self.futures.get(handle)
                if future is None or future.cancel():
                    self._save({**self.records[handle], 'status': 'cancelled',
                                'result': {'status': 'cancelled', 'output': ''}})
        return {'status': 'success', 'handles': targets}

    def close(self, *, cancel_pending=True):
        if self.closed:
            return
        if not cancel_pending and self.outstanding():
            raise ValueError('Collect child results before closing an active scheduler')
        if cancel_pending:
            for child_scope in list(self.scopes.values()):
                child_scope.cancel.set()
        try:
            if cancel_pending:
                self.cancel()
        finally:
            self.closed = True
            self.pool.shutdown(wait=True, cancel_futures=cancel_pending)
