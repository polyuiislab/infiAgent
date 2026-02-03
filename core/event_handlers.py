#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实现具体的事件处理器 (Event Handlers) - v2 (事件分类规范化)
"""

from gc import is_finalized
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

    def _stream_run_tool_start(self, event: ToolCallStartEvent):
        params_str = json.dumps(event.arguments, ensure_ascii=False, indent=2)
        self.jsonl_emitter.token(f"调用工具: {event.tool_name}\n参数: {params_str}")

    def _stream_run_tool_end(self, event: ToolCallEndEvent):
        output_preview = str(event.result.get('output', ''))[:100]
        self.jsonl_emitter.token(f"工具 {event.tool_name} 完成: {event.status} - {output_preview}...")

    def _stream_run_thinking_end(self, event: ThinkingEndEvent):
        if event.is_initial:
            self.jsonl_emitter.token(f"[{event.agent_name}] 初始规划: {event.result}")
        else:
            self.jsonl_emitter.token(f"[{event.agent_name}] 进度分析: {event.result}")
    
    def _stream_run_thinking_fail(self, event: ThinkingFailEvent):
        self.jsonl_emitter.warn(event.error_message)

    def _stream_system_error(self, event: ErrorEvent):
        self.jsonl_emitter.error(event.error_display)
