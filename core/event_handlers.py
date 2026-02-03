#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from .events import *
from utils.windows_compat import safe_print
from utils.event_emitter import get_event_emitter as get_jsonl_emitter

class ConsoleLogHandler:
    """
    控制台日志处理器.
    消费AgentEvent, 并以用户友好的格式打印到控制台.
    """
    def handle(self, event: AgentEvent):
        """根据事件类型, 调用不同的打印方法"""
        # 优先处理通用的CliDisplayEvent
        if isinstance(event, CliDisplayEvent):
            self._print_cli_display(event)
            return
            
        # 再处理核心生命周期事件
        method_name = f"_print_{event.event_type}"
        handler_method = getattr(self, method_name, self._print_default)
        handler_method(event)

    def _print_cli_display(self, event: CliDisplayEvent):
        """打印通用的CLI消息"""
        # 未来可根据 event.style 添加颜色
        safe_print(event.message)

    def _print_default(self, event: AgentEvent):
        """默认不打印任何内容"""
        pass

    def _print_agent_start(self, event: AgentStartEvent):
        safe_print(f"\n={'='*80}")
        safe_print(f"🤖 启动Agent: {event.agent_name}")
        safe_print(f"📝 任务: {event.task_input[:100]}...")
        safe_print(f"{ '='*80}\n")
    
    def _print_agent_end(self, event: AgentEndEvent):
        if event.status == "success":
            safe_print(f"\n={'='*80}")
            safe_print(f"✅ Agent完成: {event.result.get('tool_name', 'unknown')}")
            safe_print(f"📊 状态: {event.result.get('result', {}).get('status', 'unknown')}")
            safe_print(f"{ '='*80}\n")
        # 错误和超时的最终信息由 ErrorEvent 和 CliDisplayEvent 打印

    def _print_llm_call_start(self, event: LlmCallStartEvent):
        safe_print(f"🤖 调用LLM: {event.model}")
        safe_print(f"   📝 System Prompt长度: {len(event.system_prompt)} 字符")

    def _print_llm_call_end(self, event: LlmCallEndEvent):
        safe_print(f"📥 LLM输出: {event.llm_output[:100]}...")
        safe_print(f"🔧 工具调用数量: {len(event.tool_calls)}")
        
    def _print_tool_call_start(self, event: ToolCallStartEvent):
        safe_print(f"\n🔧 执行工具: {event.tool_name}")
        safe_print(f"📋 参数: {event.arguments}")
        
    def _print_tool_call_end(self, event: ToolCallEndEvent):
        safe_print(f"✅ 结果: {event.status}")
    
    def _print_thinking(self, event: ThinkingEvent):
        # thinking事件的结果同时用于CLI显示和JSONL，所以在这里打印
        safe_print(f"[{event.agent_name}] 进度分析: {event.result}")

    def _print_error(self, event: ErrorEvent):
        safe_print(event.error_display)


class JsonlStreamHandler:
    """
    JSONL流处理器
    只消费核心生命周期事件, 并将其转换为用于插件集成的JSONL格式
    """
    def __init__(self, enabled: bool):
        self.jsonl_emitter = get_jsonl_emitter()
        self.jsonl_emitter.enabled = enabled

    def handle(self, event: AgentEvent):
        if not self.jsonl_emitter.enabled or isinstance(event, CliDisplayEvent):
            # 不处理纯展示事件
            return

        method_name = f"_stream_{event.event_type}"
        handler_method = getattr(self, method_name, self._stream_default)
        handler_method(event)

    def _stream_default(self, event: AgentEvent):
        """默认不处理任何事件"""
        pass
        
    def _stream_tool_call_start(self, event: ToolCallStartEvent):
        params_str = json.dumps(event.arguments, ensure_ascii=False, indent=2)
        self.jsonl_emitter.token(f"调用工具: {event.tool_name}\n参数: {params_str}")

    def _stream_tool_call_end(self, event: ToolCallEndEvent):
        output_preview = str(event.result.get('output', ''))[:100]
        self.jsonl_emitter.token(f"工具 {event.tool_name} 完成: {event.status} - {output_preview}...")

    def _stream_thinking(self, event: ThinkingEvent):
        self.jsonl_emitter.token(f"[{event.agent_name}] 进度分析: {event.result}")

    def _stream_error(self, event: ErrorEvent):
        self.jsonl_emitter.error(event.error_display)