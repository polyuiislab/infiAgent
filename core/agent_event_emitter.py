#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一事件发射器 (Event Emitter)
负责将事件分发给所有已注册的事件处理器
"""

from typing import List, Protocol
from .events import AgentEvent
from utils.execution_scope import CURRENT, TOOL_CALL
import threading

class EventHandler(Protocol):
    """
    事件处理器的协议 (Protocol)
    定义了所有具体事件处理器必须实现的接口
    """
    def handle(self, event: AgentEvent):
        """
        处理一个传入的AgentEvent
        
        Args:
            event: 从AgentExecutor分发来的事件对象
        """
        ...

class AgentEventEmitter:
    """
    事件发射器, 向所有注册的处理器分发事件
    """
    def __init__(self):
        self._handlers: List[EventHandler] = []
        self._lock = threading.RLock()

    def register(self, handler: EventHandler):
        """
        注册一个新的事件处理器
        
        Args:
            handler: 实现了EventHandler协议的对象
        """
        if handler not in self._handlers:
            self._handlers.append(handler)

    def dispatch(self, event: AgentEvent):
        """
        将事件分发给所有已注册的处理器
        
        Args:
            event: 要分发的事件对象
        """
        scope = CURRENT.get()
        if not getattr(event, 'agent_id', '') and scope:
            event.agent_id = scope.agent_id
        if not getattr(event, 'agent_name', '') and scope:
            event.agent_name = scope.agent_name
        if not getattr(event, 'tool_call_id', ''):
            event.tool_call_id = TOOL_CALL.get()
        for handler in list(self._handlers):
            try:
                with self._lock:
                    handler.handle(event)
            except Exception as e:
                # 避免一个处理器的失败影响其他处理器
                # todo: 这里后续需要换成统一的logger，方便采集服务日志
                print(f"[AgentEventEmitter] Error in handler {type(handler).__name__}: {e}")

