#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定义Agent执行过程中的所有事件契约 (Event Schema) - 简化版
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Literal
import time

# =================================================================================
# 1. 核心生命周期事件 (Lifecycle Events)
# - 标志着Agent执行过程中的关键状态转变。
# - 携带结构化数据，是外部系统(WebUI, 测试框架)消费的主要对象。
# =================================================================================

@dataclass
class AgentEvent:
    """所有事件的基类"""
    event_type: str
    timestamp: float = field(default_factory=time.time)

@dataclass
class AgentStartEvent(AgentEvent):
    """Agent任务开始"""
    event_type: str = "agent_start"
    agent_name: str
    task_input: str

@dataclass
class AgentEndEvent(AgentEvent):
    """Agent任务结束（无论成功、失败或超时）"""
    event_type: str = "agent_end"
    status: str  # e.g., 'success', 'error'
    result: Dict[str, Any]

@dataclass
class LlmCallStartEvent(AgentEvent):
    """LLM调用开始"""
    event_type: str = "llm_call_start"
    model: str
    system_prompt: str

@dataclass
class LlmCallEndEvent(AgentEvent):
    """LLM调用结束"""
    event_type: str = "llm_call_end"
    llm_output: str
    tool_calls: List[Dict]

@dataclass
class ToolCallStartEvent(AgentEvent):
    """工具调用开始"""
    event_type: str = "tool_call_start"
    tool_name: str
    arguments: Dict[str, Any]

@dataclass
class ToolCallEndEvent(AgentEvent):
    """工具调用结束"""
    event_type: str = "tool_call_end"
    tool_name: str
    status: str
    result: Dict[str, Any]

@dataclass
class ThinkingEvent(AgentEvent):
    """Agent进行思考或规划"""
    event_type: str = "thinking"
    agent_name: str
    result: str

@dataclass
class ErrorEvent(AgentEvent):
    """发生导致执行中断的严重错误"""
    event_type: str = "error"
    error_display: str

# =================================================================================
# 2. CLI展示事件 (Display Events)
# - 仅用于在命令行界面向用户展示信息，是UI/UX的一部分。
# - 通常不被外部程序化系统消费。
# =================================================================================

@dataclass
class CliDisplayEvent(AgentEvent):
    """向CLI输出一条格式化消息"""
    event_type: str = "cli_display"
    message: str
    style: Literal['info', 'warning', 'success', 'error', 'separator'] = 'info'