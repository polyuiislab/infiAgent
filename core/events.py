#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
定义Agent执行过程中的所有事件契约 (Event Schema) - v2 (规范化)
"""

from dataclasses import dataclass, field
from typing import Dict, Any, List, Literal, ClassVar
import time

# =================================================================================
# 规范：
# event_type 格式: "phase.domain.action"
# - phase: prepare, run, end
# - domain: agent, model, history, llm, tool, thinking, system
# - action: start, end, fail, select, load, etc.
# =================================================================================

@dataclass
class AgentEvent:
    """所有事件的基类"""
    event_type: str

# region 1. Prepare Phase Events
@dataclass
class ModelSelectionEvent(AgentEvent):
    """模型选择事件"""
    event_type: ClassVar[str] = "prepare.model.select" # Class variable

    requested_model: str
    final_model: str
    is_fallback: bool
    # Default arguments last
    timestamp: float = field(default_factory=time.time)

@dataclass
class HistoryLoadEvent(AgentEvent):
    """加载历史记录事件"""
    event_type: ClassVar[str] = "prepare.history.load"
    
    start_turn: int
    action_history_len: int
    action_history_fact_len: int
    pending_tool_count: int
    # Default arguments last
    timestamp: float = field(default_factory=time.time)
# endregion

# region 2. Run Phase Events

# Thinking Events
@dataclass
class ThinkingStartEvent(AgentEvent):
    """Thinking过程开始事件"""
    event_type: ClassVar[str] = "run.thinking.start"
    
    agent_name: str
    is_initial: bool
    is_forced: bool = False 
    timestamp: float = field(default_factory=time.time)


@dataclass
class ThinkingEndEvent(AgentEvent):
    """Thinking过程成功结束事件"""
    event_type: ClassVar[str] = "run.thinking.end"
    
    agent_name: str
    is_initial: bool
    result: str
    # Default arguments last
    is_forced: bool = False 
    timestamp: float = field(default_factory=time.time)

@dataclass
class ThinkingFailEvent(AgentEvent):
    """Thinking过程失败事件"""
    event_type: ClassVar[str] = "run.thinking.fail"
    
    agent_name: str
    error_message: str
    # Default arguments last
    timestamp: float = field(default_factory=time.time)

# LLM Call Events
@dataclass
class LlmCallStartEvent(AgentEvent):
    """LLM调用开始"""
    event_type: ClassVar[str] = "run.llm.start"
    
    model: str
    system_prompt: str
    # Default arguments last
    timestamp: float = field(default_factory=time.time)

@dataclass
class LlmCallEndEvent(AgentEvent):
    """LLM调用结束"""
    event_type: ClassVar[str] = "run.llm.end"
    
    llm_output: str
    tool_calls: List[Dict]
    # Default arguments last
    timestamp: float = field(default_factory=time.time)

# Tool Call Events
@dataclass
class ToolCallStartEvent(AgentEvent):
    """工具调用开始"""
    event_type: ClassVar[str] = "run.tool.start"
    
    tool_name: str
    arguments: Dict[str, Any]
    # Default arguments last
    timestamp: float = field(default_factory=time.time)

@dataclass
class ToolCallEndEvent(AgentEvent):
    """工具调用结束"""
    event_type: ClassVar[str] = "run.tool.end"
    
    tool_name: str
    status: str
    result: Dict[str, Any]
    # Default arguments last
    timestamp: float = field(default_factory=time.time)
# endregion

# region 3. General Events (Can occur in any phase)

# Agent Lifecycle
@dataclass
class AgentStartEvent(AgentEvent):
    """Agent任务开始"""
    event_type: ClassVar[str] = "agent.start"
    
    agent_name: str
    task_input: str
    # Default arguments last
    timestamp: float = field(default_factory=time.time)

@dataclass
class AgentEndEvent(AgentEvent):
    """Agent任务结束（无论成功、失败或超时）"""
    event_type: ClassVar[str] = "agent.end"
    
    status: str
    result: Dict[str, Any]
    # Default arguments last
    timestamp: float = field(default_factory=time.time)

# System & Display Events
@dataclass
class ErrorEvent(AgentEvent):
    """发生导致执行中断的严重错误"""
    event_type: ClassVar[str] = "system.error"
    
    error_display: str
    # Default arguments last
    timestamp: float = field(default_factory=time.time)

@dataclass
class CliDisplayEvent(AgentEvent):
    """向CLI输出一条格式化消息"""
    event_type: ClassVar[str] = "system.cli_display"
    
    message: str
    # Default arguments last
    style: Literal['info', 'warning', 'success', 'error', 'separator'] = 'info'
    timestamp: float = field(default_factory=time.time)
# endregion