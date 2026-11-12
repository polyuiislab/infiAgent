"""Per-agent execution context shared by model, tools and child activities."""
from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field


class AgentCancelled(BaseException):
    pass


class AgentStopped(AgentCancelled):
    pass


@dataclass
class ExecutionScope:
    parent_id: str | None = None
    instance_key: str = ""
    depth: int = 0
    agent_id: str = ""
    agent_name: str = ""
    task_id: str = ""
    agent_system: str = ""
    context_mode: str = "tool"
    context_seed: list = field(default_factory=list)
    parent_scope: object = None
    usage: dict = field(default_factory=dict)
    usage_lock: object = field(default_factory=threading.RLock)
    cancel: threading.Event = field(default_factory=threading.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    parent_cancel: threading.Event | None = None


CURRENT = contextvars.ContextVar("infiagent_execution_scope", default=None)
TOOL_CALL = contextvars.ContextVar("infiagent_tool_call", default="")


@contextmanager
def bind(scope):
    token = CURRENT.set(scope)
    call_token = TOOL_CALL.set('')
    try:
        yield scope
    finally:
        TOOL_CALL.reset(call_token)
        CURRENT.reset(token)


def check_cancelled():
    scope = CURRENT.get()
    if scope and (scope.cancel.is_set() or (scope.parent_cancel and scope.parent_cancel.is_set())):
        raise AgentCancelled()
    current = scope
    while current is not None:
        if current.stop.is_set():
            raise AgentStopped()
        current = current.parent_scope


def record_usage(usage, *, model='', purpose='execution'):
    current = CURRENT.get()
    if current is None:
        return
    from utils.usage_metrics import usage_numbers, METRIC_COLUMNS
    metrics = usage_numbers(usage)
    values = {key: 0 for key in METRIC_COLUMNS}
    values.update(metrics or {})
    values['calls'] = values['model_calls'] = 1
    values['calls_without_usage'] = int(metrics is None)
    with current.usage_lock:
        scope = current
        while scope is not None:
            for key, value in values.items():
                scope.usage[key] = scope.usage.get(key, 0) + value
            scope = scope.parent_scope
        try:
            import uuid, time
            from utils.usage_ledger import UsageLedger
            from utils.user_paths import get_user_data_root
            UsageLedger(get_user_data_root()).record({
                'event_id': uuid.uuid4().hex, 'timestamp': time.time(), 'task_id': current.task_id,
                'agent_name': current.agent_name, 'agent_id': current.agent_id,
                'agent_system': current.agent_system, 'model': str(model), 'model_alias': str(model),
                'purpose': str(purpose), 'usage': usage or {},
            })
        except Exception:
            import logging
            logging.getLogger('infiagent.usage').warning('Usage persistence failed; inspect local storage before rebuilding the usage panel')


@contextmanager
def tool_call(call_id):
    token = TOOL_CALL.set(str(call_id))
    try:
        yield
    finally:
        TOOL_CALL.reset(token)
