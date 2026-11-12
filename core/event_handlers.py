#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实现具体的事件处理器 (Event Handlers) - v2 (事件分类规范化)
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Callable, Dict, List, Optional

from .events import *
from utils.windows_compat import safe_print
from utils.event_emitter import get_event_emitter as get_jsonl_emitter


def _normalize_event_value(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _normalize_event_value(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _normalize_event_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_event_value(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_event_value(item) for item in value]
    return value


def serialize_agent_event(event: AgentEvent) -> Dict[str, Any]:
    event_type = str(getattr(event, "event_type", "") or "")
    parts = event_type.split(".", 2)
    phase = parts[0] if len(parts) > 0 else ""
    domain = parts[1] if len(parts) > 1 else ""
    action = parts[2] if len(parts) > 2 else ""

    if is_dataclass(event):
        payload = _normalize_event_value(asdict(event))
    else:
        payload = {}
    if getattr(event, 'agent_name', None):
        payload.setdefault('agent_name', event.agent_name)

    return {
        "event_type": event_type,
        "phase": phase,
        "domain": domain,
        "action": action,
        "payload": payload,
    }


class StructuredEventHandler:
    """将内部 AgentEvent 规范化为 SDK 友好的结构化字典。"""

    def __init__(
        self,
        *,
        callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        collector: Optional[List[Dict[str, Any]]] = None,
    ):
        self.callback = callback
        self.collector = collector

    def handle(self, event: AgentEvent):
        normalized = serialize_agent_event(event)
        self.emit(normalized)

    def emit(self, normalized: Dict[str, Any]):
        if self.collector is not None:
            self.collector.append(normalized)
        if self.callback is not None:
            self.callback(normalized)

class ConsoleLogHandler:
    """
    控制台日志处理器.
    消费AgentEvent, 并以用户友好的格式打印到控制台.
    """
    def handle(self, event: AgentEvent):
        """根据事件类型, 调用不同的打印方法"""
        # 将 event_type 中的 '.' 替换为 '_', 以匹配方法名
        method_name = f"_print_{event.event_type.replace('.', '_')}"
        handler_method = getattr(self, method_name, self._print_default)
        handler_method(event)

    def _print_default(self, event: AgentEvent):
        """默认不打印任何内容"""
        pass

    # Agent Lifecycle
    def _print_agent_start(self, event: AgentStartEvent):
        safe_print(f"\n{ '='*80}")
        safe_print(f"🤖 启动Agent: {event.agent_name}")
        safe_print(f"📝 任务: {event.task_input[:100]}...")
        safe_print(f"{ '='*80}\n")
    
    def _print_agent_end(self, event: AgentEndEvent):
        if event.status == "success":
            final_result = event.result.get('result', {})
            safe_print(f"\n{ '='*80}")
            safe_print(f"✅ Agent完成: {event.result.get('tool_name', 'unknown')}")
            safe_print(f"📊 状态: {final_result.get('status', 'unknown')}")
            safe_print(f"{ '='*80}\n")
            
    # Prepare Phase
    def _print_prepare_model_select(self, event: ModelSelectionEvent):
        if event.is_fallback:
            safe_print(f"⚠️请求的模型 '{event.requested_model}' 不在可用列表中")
            safe_print(f"✅使用回退模型: {event.final_model}")
        else:
            safe_print(f"✅使用请求的模型: {event.final_model}")

    def _print_prepare_history_load(self, event: HistoryLoadEvent):
        safe_print(f"📂 已加载对话历史，从第 {event.start_turn + 1} 轮继续")
        safe_print(f"   渲染历史: {event.action_history_len}条, 完整轨迹: {event.action_history_fact_len}条")
        if event.pending_tool_count > 0:
            safe_print(f"🔄 发现{event.pending_tool_count}个pending工具，恢复执行...")

    # Run Phase
    def _print_run_llm_start(self, event: LlmCallStartEvent):
        safe_print(f"🤖 调用LLM: {event.model}")
        safe_print(f"   📝 System Prompt长度: {len(event.system_prompt)} 字符")

    def _print_run_llm_end(self, event: LlmCallEndEvent):
        safe_print(f"📥 LLM输出: {event.llm_output[:100]}...")
        safe_print(f"🔧 工具调用数量: {len(event.tool_calls)}")
        if event.usage:
            safe_print(
                f"🧮 本轮tokens: prompt={event.usage.get('prompt_tokens', 0)}, "
                f"completion={event.usage.get('completion_tokens', 0)}, "
                f"total={event.usage.get('total_tokens', 0)}"
            )
        if event.cumulative_usage:
            safe_print(
                f"📈 累计tokens: prompt={event.cumulative_usage.get('prompt_tokens', 0)}, "
                f"completion={event.cumulative_usage.get('completion_tokens', 0)}, "
                f"total={event.cumulative_usage.get('total_tokens', 0)}"
            )
        
    def _print_run_tool_start(self, event: ToolCallStartEvent):
        safe_print(f"\n🔧 执行工具: {event.tool_name}")
        safe_print(f"📋 参数: {event.arguments}")
        
    def _print_run_tool_end(self, event: ToolCallEndEvent):
        safe_print(f"✅ 结果: {event.status}")
    
    def _print_run_thinking_start(self, event: ThinkingStartEvent):
        if event.is_forced:
            safe_print("❌ 5次提醒后仍未调用工具，触发thinking分析")
        else:
            if event.is_initial:
                safe_print(f"[{event.agent_name}] 开始行动前进行初始规划...")
            else:
                safe_print(f"[{event.agent_name}] Thinking分析已更新")

    def _print_run_thinking_end(self, event: ThinkingEndEvent):
        safe_print(f"[{event.agent_name}] Thinking分析已更新: {event.result}")
        if event.usage:
            safe_print(
                f"🧮 Thinking本轮tokens: prompt={event.usage.get('prompt_tokens', 0)}, "
                f"completion={event.usage.get('completion_tokens', 0)}, "
                f"total={event.usage.get('total_tokens', 0)}"
            )
        if event.cumulative_usage:
            safe_print(
                f"📈 累计tokens: prompt={event.cumulative_usage.get('prompt_tokens', 0)}, "
                f"completion={event.cumulative_usage.get('completion_tokens', 0)}, "
                f"total={event.cumulative_usage.get('total_tokens', 0)}"
            )
        
    def _print_run_thinking_fail(self, event: ThinkingFailEvent):
        safe_print(f"⚠️ Thinking触发失败: {event.error_message}")

    # System
    def _print_system_error(self, event: ErrorEvent):
        safe_print(event.error_display)

    def _print_system_cli_display(self, event: CliDisplayEvent):
        safe_print(event.message)


class JsonlStreamHandler:
    """
    JSONL流处理器.
    消费核心生命周期事件, 并将其转换为用于插件集成的JSONL格式.
    """
    def __init__(self, enabled: bool):
        self.jsonl_emitter = get_jsonl_emitter()
        self.jsonl_emitter.enabled = enabled

    def handle(self, event: AgentEvent):
        if not self.jsonl_emitter.enabled or event.event_type.startswith('system.'):
            # 不处理纯展示或内部系统事件
            return

        # 直接将事件对象序列化为JSON
        # 这比之前手动格式化字符串更健壮、更具扩展性
        method_name = f"_stream_{event.event_type.replace('.', '_')}"
        handler_method = getattr(self, method_name, self._stream_default)
        handler_method(event)
    
    def _stream_default(self, event: AgentEvent):
        """默认不处理任何事件"""
        pass

    def _stream_agent_start(self, event: AgentStartEvent):
        self.jsonl_emitter.emit({
            "type": "agent_start",
            "agent": event.agent_name,
            "task": event.task_input[:200]
        })
    
    def _stream_agent_end(self, event: AgentEndEvent):
        self.jsonl_emitter.emit({
            "type": "agent_end",
            "status": event.status
        })
    
    def _stream_run_thinking_start(self, event: ThinkingStartEvent):
        self.jsonl_emitter.emit({
            "type": "thinking_start",
            "agent": event.agent_name,
            "is_initial": event.is_initial,
            "is_forced": event.is_forced
        })
    
    def _stream_run_tool_start(self, event: ToolCallStartEvent):
        # 使用 tool_call 事件类型（而非 token），让渲染器展示为工具卡片
        self.jsonl_emitter.emit({
            "type": "tool_call",
            "name": event.tool_name,
            "arguments": event.arguments
        })

    def _stream_run_tool_end(self, event: ToolCallEndEvent):
        # 兼容不同工具返回格式：
        # - ToolExecutor 包装层错误：error_information
        # - 工具本身错误：error
        # - 工具输出：output
        err = ""
        try:
            err = str(event.result.get("error_information") or event.result.get("error") or "")
        except Exception:
            err = ""
        out = ""
        try:
            out = str(event.result.get("output", "") or "")
        except Exception:
            out = ""
        preview = out[:200] if out else (err[:200] if err else "")
        self.jsonl_emitter.emit({
            "type": "tool_result",
            "name": event.tool_name,
            "status": event.status,
            "output_preview": preview,
            "error": err[:1000] if err else ""
        })

    def _stream_run_llm_end(self, event: LlmCallEndEvent):
        self.jsonl_emitter.emit({
            "type": "llm_end",
            "model": event.model,
            "finish_reason": event.finish_reason,
            "usage": event.usage or {},
            "cumulative_usage": event.cumulative_usage or {},
            "request_budget": event.request_budget or {},
        })

    def _stream_run_thinking_end(self, event: ThinkingEndEvent):
        # 发送完整 thinking 内容（thinking agent 独立 LLM 调用，token 不经过主 agent 事件流）
        self.jsonl_emitter.emit({
            "type": "thinking_end",
            "agent": event.agent_name,
            "is_initial": event.is_initial,
            "result": event.result,
            "usage": event.usage or {},
            "cumulative_usage": event.cumulative_usage or {},
        })
    
    def _stream_run_thinking_fail(self, event: ThinkingFailEvent):
        self.jsonl_emitter.warn(event.error_message)

    def _stream_system_error(self, event: ErrorEvent):
        self.jsonl_emitter.error(event.error_display)
