#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent执行器 - 使用标准消息格式的核心执行逻辑
历史动作通过 messages 数组传递（而非 system_prompt），支持多模态图片嵌入
"""
from typing import Any, Dict, List, Optional
import sys
import copy
import json
import os
import time
import traceback
from collections import OrderedDict
import contextvars
import hashlib
from contextlib import ExitStack
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from utils.atomic_state import read_json, write_json, StateCommitError, StateCorruptError
from utils.execution_scope import ExecutionScope, CURRENT, TOOL_CALL, AgentCancelled, AgentStopped, bind, check_cancelled, tool_call as bind_tool_call
from utils.agent_activities import positive_limit

# Windows兼容性：设置UTF-8编码
try:
    from utils.windows_compat import setup_console_encoding
    setup_console_encoding()
except ImportError:
    pass

from services.llm_client import SimpleLLMClient, ChatMessage
from core.context_builder import ContextBuilder
from core.tool_executor import ToolExecutor
from utils.conversation_storage import ConversationStorage
from utils.event_emitter import get_event_emitter as get_jsonl_emitter
from utils.reasoning_modes import normalize_reasoning_mode
from utils.skill_loader import reset_skill_loader
from utils.user_paths import apply_runtime_env_defaults, get_runtime_settings
from utils.runtime_control import (
    is_task_running,
    pop_fresh_request,
    register_running_task,
    request_fresh,
    unregister_running_task,
)
from utils.task_runtime import resume_task_with_fresh
from tool_server_lite.registry import reload_runtime_registry
from tool_server_lite.tools.code_tools import has_running_background_processes
from utils.mcp_manager import get_mcp_tools, reload_mcp_tools

from .agent_event_emitter import AgentEventEmitter
from .event_handlers import ConsoleLogHandler, JsonlStreamHandler
from .events import *
from .runtime_exceptions import InfiAgentRunError
from utils.windows_compat import safe_print


def _positive_int_or_none(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return max(1, int(value))
    except Exception:
        return None


def _resolve_agent_action_window_steps(agent_config: Dict, runtime: Dict) -> int:
    values = [
        _positive_int_or_none(agent_config.get("action_window_steps")),
        _positive_int_or_none(agent_config.get("thinking_steps")),
        _positive_int_or_none(agent_config.get("thinking_interval")),
    ]
    explicit_values = [value for value in values if value is not None]
    if explicit_values:
        return max(explicit_values)
    return _positive_int_or_none(runtime.get("action_window_steps")) or 30


def _resolve_agent_reasoning_mode(agent_config: Dict, runtime: Dict) -> str:
    agent_mode = agent_config.get("reasoning_mode")
    if agent_mode not in (None, ""):
        return normalize_reasoning_mode(
            agent_mode,
            thinking_enabled=agent_config.get("thinking_enabled", runtime.get("thinking_enabled", True)),
        )
    if agent_config.get("thinking_enabled") not in (None, ""):
        return normalize_reasoning_mode(thinking_enabled=agent_config.get("thinking_enabled"))
    return normalize_reasoning_mode(
        runtime.get("reasoning_mode"),
        thinking_enabled=runtime.get("thinking_enabled", True),
    )


from core.local_execution import LocalExecution


class AgentExecutor(LocalExecution):
    """Agent执行器 - 正确的XML上下文架构"""
    
    def __init__(
        self,
        agent_name: str,
        agent_config: Dict,
        config_loader,
        hierarchy_manager,
        direct_tools: bool = False,
        extra_event_handlers: Optional[List[Any]] = None,
        exit_on_error: bool = True,
        raise_on_error: bool = False,
        stream_llm_tokens: bool = False,
    ):
        """初始化Agent执行器"""
        self.agent_name = agent_name
        self.agent_config = agent_config
        self.config_loader = config_loader
        self.hierarchy_manager = hierarchy_manager
        self.direct_tools = direct_tools
        self.extra_event_handlers = list(extra_event_handlers or [])
        self.exit_on_error = bool(exit_on_error)
        self.raise_on_error = bool(raise_on_error)
        self.stream_llm_tokens = bool(stream_llm_tokens)
        
        self._setup_event_emitter()

        # 从配置中提取信息
        self.available_tools = list(agent_config.get("available_tools", []))
        for framework_tool in ("task_history_search", "load_skill", "offload_skill", "spawn_subagents", "collect", "cancel", "collect_subagents", "cancel_subagents"):
            if framework_tool not in self.available_tools and framework_tool in config_loader.all_tools:
                self.available_tools.append(framework_tool)
        self.max_turns = self._resolve_max_turns()
        requested_model = self._get_agent_model_preference("execution")
        self._inject_mcp_tools()
        
        # 初始化LLM客户端
        self.llm_client = SimpleLLMClient()
        self.llm_client.set_tools_config(config_loader.all_tools)
        
        # 验证并调整模型
        available_models = self.llm_client.models
        final_model = self.llm_client.resolve_model("execution", requested_model)
        is_fallback = False
        if requested_model and requested_model not in available_models:
            is_fallback = True
        self.execution_model = final_model
        self.model_type = final_model
        
        # 发送模型选择事件
        self.event_emitter.dispatch(ModelSelectionEvent(
            requested_model=requested_model,
            final_model=final_model,
            is_fallback=is_fallback
        ))

        # 初始化上下文构造器（负责完整上下文构建）
        self.context_builder = ContextBuilder(
            hierarchy_manager,
            agent_config=agent_config,
            config_loader=config_loader,
            llm_client=self.llm_client,
            max_context_window=self.llm_client.max_context_window
        )
        
        # 初始化工具执行器
        self.tool_executor = ToolExecutor(
            config_loader,
            hierarchy_manager,
            direct_mode=direct_tools,
            extra_event_handlers=self.extra_event_handlers,
            exit_on_error=self.exit_on_error,
            raise_on_error=self.raise_on_error,
            stream_llm_tokens=self.stream_llm_tokens,
        )
        
        # 初始化对话存储
        self.conversation_storage = ConversationStorage()
        
        # Agent状态
        self.agent_id = None
        self.action_history = []  # 渲染用（会压缩）
        self.action_history_fact = []  # 完整轨迹（不压缩）
        self.execution_traces = []  # execution LLM 原生输出轨迹
        self.thinking_traces = []  # thinking LLM 原生输出轨迹
        self.pending_tools = []  # 待执行的工具（用于恢复）
        self.latest_thinking = ""
        self.first_thinking_done = False
        self._apply_runtime_settings(get_runtime_settings())
        self.last_fresh_at = time.time()
        self.tool_call_counter = 0
        self.llm_turn_counter = 0  # LLM调用轮次计数器（用于消息分组）
        self.current_task_id = None

    @staticmethod
    def _normalize_reasoning_mode(value: Any = None, *, thinking_enabled: Any = None) -> str:
        return normalize_reasoning_mode(value, thinking_enabled=thinking_enabled)

    def _apply_runtime_settings(self, runtime: Optional[Dict] = None) -> None:
        runtime = runtime or get_runtime_settings()
        self.reasoning_mode = _resolve_agent_reasoning_mode(self.agent_config, runtime)
        self.thinking_enabled = self.reasoning_mode == "thinking"
        self.action_window_steps = _resolve_agent_action_window_steps(self.agent_config, runtime)
        self.thinking_steps = self.action_window_steps
        self.thinking_interval = self.action_window_steps
        self.no_tool_retry_limit = int(
            self.agent_config.get("no_tool_retry_limit")
            or runtime.get("no_tool_retry_limit", 7)
        )
        self.fresh_enabled = runtime.get("fresh_enabled", False)
        self.fresh_interval_sec = runtime.get("fresh_interval_sec", 0)

    def _should_run_react_reflection(self) -> bool:
        return getattr(self, "reasoning_mode", "thinking") == "react"

    def _tool_call_required_for_response(self) -> bool:
        return getattr(self, "reasoning_mode", "thinking") == "thinking"

    def _execution_tool_choice_override(self) -> Optional[str]:
        if getattr(self, "reasoning_mode", "thinking") == "react_lite":
            return "auto"
        return None

    def _get_agent_model_preference(self, category: str) -> Optional[str]:
        field_map = {
            "execution": "execution_model",
            "thinking": "thinking_model",
            "compressor": "compressor_model",
            "image_generation": "image_generation_model",
            "read_figure": "read_figure_model",
        }
        field = field_map.get(category, "execution_model")
        value = self.agent_config.get(field)
        if category == "execution" and not value:
            value = self.agent_config.get("model_type")
        return str(value or "").strip() or None

    def _resolve_max_turns(self) -> int:
        env_value = str(os.environ.get("MLA_MAX_TURNS", "") or "").strip()
        if env_value:
            try:
                return max(1, int(env_value))
            except Exception:
                pass
        return 100000

    def _inject_mcp_tools(self):
        """将 MCP 动态工具并入当前 Agent 的工具配置与可见工具列表。"""
        try:
            mcp_tools = get_mcp_tools(force_reload=False)
            if not mcp_tools:
                return
            for tool_name, tool_config in mcp_tools.items():
                self.config_loader.all_tools[tool_name] = tool_config
                if tool_name not in self.available_tools:
                    self.available_tools.append(tool_name)
        except Exception:
            pass

    def _setup_event_emitter(self):
        """初始化事件发射器并注册处理器"""
        self.event_emitter = AgentEventEmitter()
        self.event_emitter.register(ConsoleLogHandler())
        for handler in self.extra_event_handlers:
            self.event_emitter.register(handler)
        
        jsonl_emitter = get_jsonl_emitter()
        if jsonl_emitter.enabled:
            self.event_emitter.register(JsonlStreamHandler(enabled=True))

    def _emit_sdk_stream_event(self, event_type: str, payload: Dict[str, Any]):
        parts = str(event_type or "").split(".", 2)
        normalized = {
            "event_type": str(event_type or ""),
            "phase": parts[0] if len(parts) > 0 else "",
            "domain": parts[1] if len(parts) > 1 else "",
            "action": parts[2] if len(parts) > 2 else "",
            "payload": {**payload, "agent_id": getattr(self, "agent_id", "") or "", "agent_name": self.agent_name},
        }
        for handler in self.extra_event_handlers:
            emitter = getattr(handler, "emit", None)
            if not callable(emitter):
                continue
            try:
                emitter(normalized)
            except Exception:
                pass
    
    def run(self, task_id: str, user_input: str) -> Dict:
        parent = CURRENT.get()
        if parent is not None and not parent.agent_id:
            scope = parent
        else:
            scope = ExecutionScope(parent_id=parent.agent_id if parent else None,
                instance_key=TOOL_CALL.get() if parent else '', depth=parent.depth if parent else 0,
                parent_cancel=parent.cancel if parent else None, parent_scope=parent)
            if parent:
                scope.usage_lock = parent.usage_lock
        scope.agent_name = self.agent_name
        scope.agent_system = self.config_loader.agent_system_name
        scope.task_id = str(Path(task_id).resolve())
        scope.context_mode = getattr(self, 'context_mode', scope.context_mode)
        if scope.context_mode == 'inherit' and parent and parent.agent_id:
            scope.context_seed = (self.conversation_storage.load_actions(task_id, parent.agent_id) or {}).get('action_history', [])
        with ExitStack() as resources:
            resources.enter_context(bind(scope))
            if not scope.parent_id:
                from utils.experience_store import _file_lock
                from utils.workspaces import files_root, concurrency
                task_root = Path(task_id).resolve()
                from utils.task_owner import task_owner
                if not resources.enter_context(task_owner(task_id)):
                    return {'status': 'busy', 'output': '', 'error': 'Task already running'}
                generation = read_json(task_root / '.sdk_runtime' / 'generation.json')
                if generation:
                    scope.instance_key = str(generation['id'])
                shared_files = files_root(task_id)
                if shared_files != task_root and concurrency(task_id) == 'serial':
                    resources.enter_context(_file_lock(shared_files.parent / 'run-owner'))
            result = None
            try:
                check_cancelled()
                result = self._run_inner(task_id, user_input)
                return result
            except AgentCancelled as exc:
                status = 'stopped' if isinstance(exc, AgentStopped) else 'cancelled'
                result = {'status': status, 'output': '', 'error_code': status.upper()}
                if self.agent_id:
                    self.hierarchy_manager.pop_agent(self.agent_id, status)
                self.event_emitter.dispatch(AgentEndEvent(status=status, result=result))
                return result
            finally:
                activities = getattr(self.tool_executor, 'activities', None)
                if activities:
                    activities.close()
                if isinstance(result, dict):
                    from utils.usage_metrics import _zero, _finalize
                    result['usage'] = _finalize({**_zero(), **scope.usage})
                if scope is parent:
                    scope.agent_id = ''

    def _run_inner(self, task_id: str, user_input: str) -> Dict:
        """执行Agent任务"""

        # 存储 task_input 供压缩器使用
        self.current_task_id = task_id
        self.current_task_input = user_input
        try:
            try:
                if not CURRENT.get() or not CURRENT.get().parent_id:
                    self.hierarchy_manager.set_runtime_metadata(
                        agent_system=self.config_loader.agent_system_name,
                        agent_name=self.agent_name,
                        user_input=user_input,
                    )
            except Exception:
                pass

            register_running_task(
                task_id=task_id,
                agent_name=self.agent_name,
                user_input=user_input,
                agent_system=self.config_loader.agent_system_name,
            )

            # Agent入栈
            self.agent_id = self.hierarchy_manager.push_agent(self.agent_name, user_input)
            if CURRENT.get():
                CURRENT.get().agent_id = self.agent_id
            self.tool_executor.set_agent_context(agent_id=self.agent_id, agent_name=self.agent_name)
            self.tool_executor.available_agent_tools = set(self.available_tools)
            self.event_emitter.dispatch(AgentStartEvent(agent_name=self.agent_name, task_input=user_input))
            activity_root = Path(task_id) / ".sdk_runtime" / "agent_activities" / hashlib.sha256(self.agent_id.encode()).hexdigest()[:24]
            if any(activity_root.glob("activity_*.json")):
                self.tool_executor.child_activities(task_id)

            # 尝试加载已有的对话历史。若历史里已有 final_output，
            # _load_state_from_storage 会直接返回该最终结果，避免把 dict
            # 当作 turn index 继续进入 range(...)。
            loaded_state = self._load_state_from_storage(task_id)
            if isinstance(loaded_state, dict):
                status = str(loaded_state.get("status") or "success")
                self.event_emitter.dispatch(AgentEndEvent(status=status, result=loaded_state))
                self.hierarchy_manager.pop_agent(self.agent_id, loaded_state.get("output", ""))
                return loaded_state
            start_turn = int(loaded_state or 0)
            
            try:
                # 首次thinking（初始规划）
                if self.thinking_enabled and start_turn == 0 and not self.first_thinking_done:
                    thinking_result = self._trigger_thinking(
                        task_id, 
                        user_input, 
                        is_initial=True
                    )
                    if thinking_result:
                        self.latest_thinking = thinking_result
                        self.first_thinking_done = True
                        self.hierarchy_manager.update_thinking(self.agent_id, thinking_result)
                        self._save_state(task_id, user_input, 0)
            except Exception as e:
                return self._handle_execution_error(e)
            
            # 强制工具调用计数器
            max_tool_try = 0

            # 执行循环
            for turn in range(start_turn, self.max_turns):
                self.event_emitter.dispatch(CliDisplayEvent(
                    message=f"\n--- 第 {turn + 1}/{self.max_turns} 轮执行 ---", 
                    style='separator'
                ))
                
                try:
                    self._auto_collect_children(task_id, user_input, turn)
                    self._maybe_run_scheduled_fresh(task_id, user_input, turn)

                    # 每轮开始前保存状态
                    self._save_state(task_id, user_input, turn)

                    # 检查并压缩历史动作（如果超过限制）
                    self._compress_action_history_if_needed()

                    # 构建系统提示词（不含历史动作，历史动作改由 messages 承载）
                    full_system_prompt = self.context_builder.build_context(
                        task_id,
                        self.agent_id,
                        self.agent_name,
                        user_input,
                        action_history=self.action_history,
                        include_action_history=False  # 历史动作通过 messages 传递
                    )
                    
                    # 从 action_history 构建标准 messages 数组
                    messages = self._build_messages_from_action_history()
                    
                    # ReAct 模式：在动作调用前先进行一轮纯文本反思；ReAct Lite 直接要求工具调用。
                    if self._should_run_react_reflection():
                        self._run_react_reflection(
                            task_id=task_id,
                            task_input=user_input,
                            system_prompt=full_system_prompt,
                            messages=messages,
                            turn=turn,
                        )
                        messages = self._build_messages_from_action_history()

                    full_system_prompt, messages = self._fit_execution_context_to_budget(
                        task_id=task_id,
                        user_input=user_input,
                        system_prompt=full_system_prompt,
                        messages=messages,
                        turn=turn,
                    )

                    # 调用LLM（使用标准 messages 格式）
                    llm_response = self._execute_llm_call(
                        full_system_prompt,
                        messages,
                        task_id=task_id,
                        tool_choice=self._execution_tool_choice_override(),
                    )
                    
                    if llm_response.status != "success":
                        error_result = {
                            "status": "error",
                            "output": "LLM调用失败",
                            "error_information": llm_response.error_information
                        }
                        error_result = self._with_model_outputs(error_result)
                        self.hierarchy_manager.pop_agent(self.agent_id, str(error_result))
                        self.event_emitter.dispatch(AgentEndEvent(status='error', result=error_result))
                        return error_result

                    if not llm_response.tool_calls:
                        if self._tool_call_required_for_response():
                            if max_tool_try < self.no_tool_retry_limit:
                                max_tool_try += 1
                                self.event_emitter.dispatch(CliDisplayEvent(
                                    message=f"⚠️ LLM未调用工具，第{max_tool_try}/{self.no_tool_retry_limit}次提醒",
                                    style='warning'
                                ))
                                self.action_history.append({
                                    "_turn": self.llm_turn_counter,
                                    "tool_name": "_no_tool_call",
                                    "arguments": {},
                                    "result": {
                                        "status": "error",
                                        "output": f"第{max_tool_try}次：LLM未调用工具，请在下一轮中必须调用工具"
                                    },
                                    "assistant_content": llm_response.output or "",
                                    "reasoning_content": llm_response.reasoning_content or "",
                                    "thinking_blocks": llm_response.thinking_blocks or [],
                                })
                                self.llm_turn_counter += 1
                                self._save_state(task_id, user_input, turn)
                                continue

                            thinking_result = ""
                            if self.thinking_enabled:
                                thinking_result = self._trigger_thinking(
                                    task_id,
                                    user_input,
                                    is_initial=False,
                                    is_forced=True
                                )
                            error_output = thinking_result or "多次未调用工具"
                            error_result = {
                                "status": "error",
                                "output": error_output,
                                "error_information": "Agent拒绝调用工具"
                            }
                            error_result = self._with_model_outputs(error_result)
                            self.hierarchy_manager.pop_agent(self.agent_id, str(error_result))
                            self.event_emitter.dispatch(AgentEndEvent(status='error', result=error_result))
                            if self.thinking_enabled:
                                self.event_emitter.dispatch(ThinkingFailEvent(agent_name=self.agent_name, error_message=f"[{self.agent_name}] 强制thinking: {thinking_result if thinking_result else '分析失败'}"))
                            return error_result

                        text_response = (llm_response.output or "").strip()
                        if text_response:
                            self._record_text_response(
                                tool_name="_assistant_text",
                                text=text_response,
                                llm_turn=self.llm_turn_counter,
                                reasoning_content=llm_response.reasoning_content or "",
                                thinking_blocks=llm_response.thinking_blocks or [],
                            )
                            self.llm_turn_counter += 1
                            self._save_state(task_id, user_input, turn)
                            continue

                        error_result = {
                            "status": "error",
                            "output": "",
                            "error_information": "ReAct模式下模型既未调用工具，也未返回可用文本"
                        }
                        error_result = self._with_model_outputs(error_result)
                        self.hierarchy_manager.pop_agent(self.agent_id, str(error_result))
                        self.event_emitter.dispatch(AgentEndEvent(status='error', result=error_result))
                        return error_result
                    # 重置计数器（成功调用了工具）
                    max_tool_try = 0

                    # 提取本轮 LLM 输出的文本内容和推理内容（所有 tool_call 共享）
                    current_assistant_content = llm_response.output or ""
                    current_reasoning_content = llm_response.reasoning_content or ""
                    current_thinking_blocks = llm_response.thinking_blocks or []
                    current_llm_turn = self.llm_turn_counter

                    # 执行所有工具调用
                    for final_output_result in self._execute_tool_batch(
                        llm_response.tool_calls, task_id, user_input, turn,
                        assistant_content=current_assistant_content,
                        reasoning_content=current_reasoning_content,
                        thinking_blocks=current_thinking_blocks, llm_turn=current_llm_turn,
                    ):
                        if final_output_result:
                            final_output_result = self._with_model_outputs(final_output_result)
                            self.event_emitter.dispatch(AgentEndEvent(status='success', result=final_output_result))
                            self.hierarchy_manager.pop_agent(self.agent_id, final_output_result.get("output", ""))
                            return final_output_result
                    
                    self.llm_turn_counter += 1
                    
                    counter_before = self.tool_call_counter - len(llm_response.tool_calls)

                    # 检查是否该触发thinking（每 N 轮工具调用）
                    # 用整除判断是否跨过了 thinking_interval 边界（避免多 tool_call 跳过边界值）
                    crossed_boundary = (counter_before // self.thinking_interval) < (self.tool_call_counter // self.thinking_interval)
                    if self.thinking_enabled and self.tool_call_counter > 0 and crossed_boundary:
                        thinking_result = self._trigger_thinking(task_id, user_input, is_initial=False)
                        if thinking_result:
                            self.latest_thinking = thinking_result
                            self.hierarchy_manager.update_thinking(self.agent_id, thinking_result)
                            self._save_state(task_id, user_input, turn)

                    # 检查动作窗口是否跨过边界：thinking 完成后再清空当前可见动作窗口
                    crossed_action_window = (counter_before // self.action_window_steps) < (self.tool_call_counter // self.action_window_steps)
                    if self.thinking_enabled and self.tool_call_counter > 0 and crossed_action_window:
                        self.action_history = []
                        self.llm_turn_counter = 0
                
                except Exception as e:
                    return self._handle_execution_error(e)
            timeout_result = {
                "status": "error",
                "output": f"执行超过最大轮次限制: {self.max_turns}",
                "error_information": f"Max turns {self.max_turns} exceeded"
            }
            timeout_result = self._with_model_outputs(timeout_result)
            self.hierarchy_manager.pop_agent(self.agent_id, str(timeout_result))
            self.event_emitter.dispatch(AgentEndEvent(status='error', result=timeout_result))
            self.event_emitter.dispatch(CliDisplayEvent(
                message="\n⚠️ 达到最大轮次限制: {self.max_turns}"
            ))
            
            return timeout_result
        finally:
            unregister_running_task(task_id)

    def _load_state_from_storage(self, task_id: str) -> Any:
        """从存储加载状态，返回起始轮次；若任务已完成则返回 final_result。"""
        loaded_data = self.conversation_storage.load_actions(task_id, self.agent_id)
        start_turn = 0
        
        if loaded_data:
            self.action_history = loaded_data.get("action_history", [])
            self.action_history_fact = loaded_data.get("action_history_fact", [])
            self.pending_tools = loaded_data.get("pending_tools", [])
            self.latest_thinking = loaded_data.get("latest_thinking", "")
            self.first_thinking_done = loaded_data.get("first_thinking_done", False)
            self.tool_call_counter = loaded_data.get("tool_call_counter", 0)
            self.llm_turn_counter = loaded_data.get("llm_turn_counter", 0)
            start_turn = loaded_data.get("current_turn", 0) + 1
            
            self.event_emitter.dispatch(HistoryLoadEvent(
                start_turn=start_turn,
                action_history_len=len(self.action_history),
                action_history_fact_len=len(self.action_history_fact),
                pending_tool_count=len(self.pending_tools)
            ))
            
            # History is committed before child acknowledgements. Repair that
            # boundary after interruption so auto-collect cannot deliver twice.
            manager = getattr(self.tool_executor, 'activities', None)
            if manager is not None:
                for record in self.action_history_fact:
                    receipts = record.get('result', {}).get('_activity_receipts', [])
                    known = [h for h in receipts if h in manager.records]
                    if known:
                        manager.acknowledge(known)

            # 检查是否已经完成（有final_output）
            for action in self.action_history_fact:
                if action.get("tool_name") == "final_output" and not action.get('result', {}).get('error_code'):
                    final_result = action.get("result", {})
                    self.event_emitter.dispatch(CliDisplayEvent(
                        message=f"\n✅ 任务已完成，直接返回之前的final_output结果\n   状态: {final_result.get('status')}", 
                        style='success'
                    ))
                    return final_result
            
            # 恢复pending工具（如果有）
            if self.pending_tools:
                recovered = self._recover_pending_tools(task_id, start_turn)
                if recovered is not None:
                    return recovered

        return start_turn

    def _build_messages_from_action_history(self) -> List[Dict]:
        """
        从 action_history 动态重建 OpenAI 标准格式的 messages 数组
        
        支持三种 action 类型：
        1. _historical_summary → user 消息（压缩后的历史摘要）
        2. _no_tool_call → assistant 消息（纯文本）+ user 消息（提醒）
        3. 普通 action → 按 _turn 分组为 assistant(tool_calls) + tool(results) + user(images)
        
        Returns:
            OpenAI 格式的 messages 列表
        """
        next_step_prompt = {
            "role": "user", 
            "content": "请根据当前任务和上下文，执行下一步操作。请调用合适的工具来完成任务。不要重复已执行的动作！"
        }
        
        if not self.action_history:
            return [next_step_prompt]

        messages = []

        use_kimi_history_tool_ids = self._should_normalize_kimi_history_tool_ids()
        history_tool_call_index = 0

        # 按 _turn 分组普通 action
        turns = OrderedDict()
        
        for action in self.action_history:
            tool_name = action.get("tool_name", "")
            
            # 特殊处理：历史摘要（压缩产物）
            if tool_name == "_historical_summary":
                messages.append({
                    "role": "user",
                    "content": f"[Previous actions summary]\n{action['result']['output']}"
                })
                continue

            if tool_name in {"_react_reflection", "_assistant_text"}:
                assistant_content = action.get("assistant_content", "") or action.get("result", {}).get("output", "")
                if assistant_content:
                    assistant_msg = {"role": "assistant", "content": assistant_content}
                    if action.get("reasoning_content"):
                        assistant_msg["reasoning_content"] = action["reasoning_content"]
                    if action.get("thinking_blocks"):
                        assistant_msg["thinking_blocks"] = action["thinking_blocks"]
                    messages.append(assistant_msg)
                continue
            
            # 特殊处理：LLM 未调用工具
            if tool_name == "_no_tool_call":
                assistant_content = action.get("assistant_content", "")
                if assistant_content:
                    assistant_msg = {"role": "assistant", "content": assistant_content}
                    if action.get("reasoning_content"):
                        assistant_msg["reasoning_content"] = action["reasoning_content"]
                    if action.get("thinking_blocks"):
                        assistant_msg["thinking_blocks"] = action["thinking_blocks"]
                    messages.append(assistant_msg)
                messages.append({
                    "role": "user",
                    "content": action["result"].get("output", "请调用工具")
                })
                continue
            
            # 普通 action - 按 _turn 分组
            turn = action.get("_turn", 0)  # 向后兼容：旧记录默认 turn=0
            
            if turn not in turns:
                turns[turn] = {
                    "assistant_content": action.get("assistant_content", ""),
                    "reasoning_content": action.get("reasoning_content", ""),
                    "thinking_blocks": action.get("thinking_blocks", []),
                    "tool_calls": [],
                    "tool_results": [],
                    "images": []
                }
            
            # 构建 tool_call 条目
            tool_call_id = action.get("tool_call_id", f"call_{turn}_{len(turns[turn]['tool_calls'])}")
            if use_kimi_history_tool_ids:
                tool_call_id = self._format_kimi_history_tool_call_id(
                    tool_name=action.get("tool_name", ""),
                    sequence_index=history_tool_call_index,
                )
            history_tool_call_index += 1
            turns[turn]["tool_calls"].append({
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": action["tool_name"],
                    "arguments": json.dumps(action["arguments"], ensure_ascii=False)
                }
            })
            
            # 构建 tool result 消息
            has_image = action.get("_has_image", False)
            has_base64 = bool(action.get("_image_base64"))
            
            if has_image and has_base64:
                # 有图片且有 base64 数据 → tool result 简短说明，图片在后续 user 消息中嵌入
                result_content = "Image loaded successfully. See below."
            else:
                # 无图片 或 有图片标记但 base64 丢失（Ctrl+C 恢复场景）→ 正常 JSON 结果
                # 排除 _image_base64 等内部字段
                result_clean = {k: v for k, v in action.get("result", {}).items() 
                               if not k.startswith("_")}
                result_content = json.dumps(result_clean, ensure_ascii=False)
            
            turns[turn]["tool_results"].append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result_content
            })
            
            # 收集图片数据（方案二：后续 user 消息嵌入）
            # 只有同时有 _has_image 标记和实际 base64 数据时才嵌入图片
            if has_image and has_base64:
                query = action.get("arguments", {}).get("query", "请分析这些图片")
                img_data = action["_image_base64"]
                # 兼容列表和单值
                if isinstance(img_data, list):
                    base64_list = img_data
                else:
                    base64_list = [img_data]
                turns[turn]["images"].append({
                    "base64_list": base64_list,
                    "query": query
                })
        
        # 从分组数据构建 messages
        for turn_num in sorted(turns.keys()):
            turn_data = turns[turn_num]
            
            # assistant 消息优先保留 LiteLLM 标准化 reasoning_content。
            # 若 provider 不兼容，LLMClient 会在本次运行中自动降级为 content 合并模式。
            assistant_msg = {
                "role": "assistant",
                "content": turn_data["assistant_content"] or None,
                "tool_calls": turn_data["tool_calls"]
            }
            if turn_data.get("reasoning_content"):
                assistant_msg["reasoning_content"] = turn_data["reasoning_content"]
            if turn_data.get("thinking_blocks"):
                assistant_msg["thinking_blocks"] = turn_data["thinking_blocks"]
            messages.append(assistant_msg)
            
            # tool result 消息（每个 tool_call 对应一个）
            messages.extend(turn_data["tool_results"])
            
            # 图片消息（方案二：跟在 tool result 后面的 user 消息，多张图合并到一条消息）
            for img_group in turn_data["images"]:
                content_parts = []
                for b64 in img_group["base64_list"]:
                    image_url = b64 if b64.startswith("data:") else f"data:image/jpeg;base64,{b64}"
                    content_parts.append({"type": "image_url", "image_url": {"url": image_url}})
                content_parts.append({
                    "type": "text",
                    "text": f"上面是 image_read 获取的 {len(img_group['base64_list'])} 张图片。Agent 的问题是: {img_group['query']}"
                })
                messages.append({"role": "user", "content": content_parts})

        messages.append(next_step_prompt)
        
        return messages

    def _should_normalize_kimi_history_tool_ids(self) -> bool:
        model_name = str(getattr(self, "execution_model", "") or "").strip().lower()
        if not model_name:
            return False
        return "kimi-k2" in model_name or ("moonshot" in model_name and "kimi" in model_name)

    @staticmethod
    def _format_kimi_history_tool_call_id(tool_name: str, sequence_index: int) -> str:
        safe_tool_name = str(tool_name or "").strip() or "tool"
        return f"functions.{safe_tool_name}:{max(0, int(sequence_index))}"
    def _execute_llm_call(
        self,
        system_prompt: str,
        messages: List[Dict] = None,
        task_id: Optional[str] = None,
        *,
        tool_list: Optional[List[str]] = None,
        tool_choice: Optional[str] = None,
        debug_label: str = "execution",
        stream_tokens: bool = True,
        stream_group: str = "llm",
        content_as_reasoning: bool = False,
    ):
        """
        执行LLM调用并分发事件
        
        Args:
            system_prompt: 系统提示词（不含历史动作）
            messages: OpenAI 标准格式的 messages 数组（包含历史动作）
        """
        if messages is None:
            # 向后兼容：如果没有传 messages，使用简单的 user 消息
            messages = [{"role": "user", "content": "请输出下一个动作"}]
        
        # 发送LLM调用开始事件
        self.event_emitter.dispatch(LlmCallStartEvent(
            model=self.execution_model, 
            system_prompt=system_prompt
        ))
        
        effective_tool_list = list(self.available_tools if tool_list is None else tool_list)
        execution_tool_choice = tool_choice or self.llm_client.resolve_tool_choice(
            category="execution",
            model=self.execution_model,
        )
        max_tokens_override = self.agent_config.get("max_tokens")
        # 调用LLM（重试机制已在 llm_client 内部实现）
        llm_response = self.llm_client.chat(
            history=messages,
            model=self.execution_model,
            system_prompt=system_prompt,
            tool_list=effective_tool_list,
            tool_choice=execution_tool_choice,
            max_tokens=max_tokens_override,
            emit_tokens="thinking" if stream_group == "thinking" else "token",
            debug_task_id=task_id,
            debug_label=debug_label,
            stream_callback=self._build_llm_stream_callback(
                stream_group=stream_group,
                agent_name=self.agent_name,
                model=self.execution_model,
                debug_label=debug_label,
                content_as_reasoning=content_as_reasoning,
            ) if self.stream_llm_tokens and stream_tokens else None,
        )

        llm_record = {
            "turn_index": self.llm_turn_counter,
            "debug_label": debug_label,
            "tool_choice": execution_tool_choice,
            "model": llm_response.model or self.execution_model,
            "content": llm_response.output or "",
            "reasoning_content": llm_response.reasoning_content or "",
            "thinking_blocks": llm_response.thinking_blocks or [],
            "finish_reason": llm_response.finish_reason or "",
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                }
                for tool_call in (llm_response.tool_calls or [])
            ],
            "status": llm_response.status,
        }
        self.execution_traces.append(llm_record)
        
        self.event_emitter.dispatch(LlmCallEndEvent(
            llm_output=llm_response.output, 
            tool_calls=llm_record["tool_calls"],
            model=llm_record["model"],
            reasoning_content=llm_record["reasoning_content"],
            finish_reason=llm_record["finish_reason"],
        ))
        return llm_response

    def _record_text_response(
        self,
        *,
        tool_name: str,
        text: str,
        llm_turn: int,
        reasoning_content: str = "",
        thinking_blocks: List[Dict] = None,
    ) -> None:
        action_record = {
            "_turn": llm_turn,
            "tool_name": tool_name,
            "arguments": {},
            "result": {
                "status": "success",
                "output": text,
            },
            "assistant_content": text,
            "reasoning_content": reasoning_content,
            "thinking_blocks": thinking_blocks or [],
            "_has_image": False,
            "_image_base64": None,
        }
        fact_record = dict(action_record)
        fact_record["_image_base64"] = None
        self.action_history_fact.append(fact_record)
        self.action_history.append(action_record)

    def _format_react_tools_info(self) -> str:
        """Format visible tools as prompt text for planning-only ReAct reflection."""
        available_tools = list(getattr(self, "available_tools", []) or [])
        if not available_tools:
            return "可用工具：(无)"

        tools_config = getattr(getattr(self, "config_loader", None), "all_tools", None) or {}
        if not tools_config:
            return f"可用工具：{', '.join(available_tools)}"

        tools_details = ["<可用工具详情>"]
        for tool_name in available_tools:
            tool_cfg = tools_config.get(tool_name)
            if not isinstance(tool_cfg, dict):
                tools_details.append(f"\n【{tool_name}】 (无详细信息)")
                continue

            description = tool_cfg.get("description", "无描述")
            params = tool_cfg.get("parameters", {}) or {}
            tools_details.append(f"\n【{tool_name}】")
            tools_details.append(f"  描述: {description}")

            if isinstance(params, dict) and "properties" in params:
                required_params = params.get("required", []) or []
                tools_details.append("  参数:")
                for param_name, param_info in (params.get("properties") or {}).items():
                    param_info = param_info if isinstance(param_info, dict) else {}
                    param_desc = param_info.get("description", "")
                    param_type = param_info.get("type", "")
                    required = "必需" if param_name in required_params else "可选"
                    tools_details.append(f"    - {param_name} ({param_type}, {required}): {param_desc}")

        tools_details.append("\n</可用工具详情>")
        return "\n".join(tools_details)

    def _build_react_reflection_prompt(self) -> str:
        tools_info = self._format_react_tools_info()
        return (
            "你当前处于 ReAct 反思阶段。请先简短输出当前进展、下一步最应该采取的动作、"
            "以及是否需要使用 task_history_search 检索历史任务。\n"
            "本阶段只能做规划，不能调用工具；但你必须参考下面的工具说明，确保下一步动作与工具能力、参数要求匹配。\n"
            "只输出纯文本思考，不要调用工具，不要输出 JSON/XML/markdown 标记。\n"
            f"{tools_info}"
        )

    def _run_react_reflection(
        self,
        *,
        task_id: str,
        task_input: str,
        system_prompt: str,
        messages: List[Dict],
        turn: int,
    ) -> None:
        reflection_messages = list(messages or [])
        reflection_messages.append({
            "role": "user",
            "content": self._build_react_reflection_prompt(),
        })
        llm_response = self._execute_llm_call(
            system_prompt,
            reflection_messages,
            task_id=task_id,
            tool_list=[],
            tool_choice="none",
            debug_label="react_reflection",
            stream_tokens=True,
            stream_group="llm",
            content_as_reasoning=True,
        )
        if llm_response.status != "success":
            raise Exception(llm_response.error_information or "ReAct reflection failed")
        reflection_text = str(llm_response.output or "").strip()
        if not reflection_text:
            return
        self._record_text_response(
            tool_name="_react_reflection",
            text=reflection_text,
            llm_turn=self.llm_turn_counter,
            reasoning_content=llm_response.reasoning_content or "",
            thinking_blocks=llm_response.thinking_blocks or [],
        )
        self.llm_turn_counter += 1
        self._save_state(task_id, task_input, turn)
        self.event_emitter.dispatch(CliDisplayEvent(
            message=f"🤔 ReAct反思已更新: {reflection_text[:160]}",
            style='info'
        ))

    def _execute_tool_call(self, tool_call: Dict, task_id: str, user_input: str, turn: int,
                          assistant_content: str = "", reasoning_content: str = "",
                          thinking_blocks: List[Dict] = None,
                          llm_turn: int = 0, _prepared: bool = False, _result: Optional[Dict] = None) -> Dict:
        """
        执行单个工具调用并分发事件
        
        Args:
            tool_call: 工具调用对象（包含 id, name, arguments）
            task_id: 任务ID
            user_input: 用户输入
            turn: 当前执行轮次
            assistant_content: 该轮 LLM 响应的文本内容（同轮所有 tool_call 共享）
            reasoning_content: 该轮 LLM 响应的推理/思考内容（同轮所有 tool_call 共享）
            llm_turn: LLM 调用轮次（用于消息分组）
        """
        # ✅ 在保存 pending 之前，为 level != 0 的工具添加 uuid
        arguments_with_uuid = tool_call.arguments if _prepared else self._add_uuid_if_needed(tool_call.name, tool_call.arguments)
        
        # ✅ 先标记为pending（保存带 uuid 的参数）
        # 发送工具调用开始事件
        if not _prepared:
            self.event_emitter.dispatch(ToolCallStartEvent(
                tool_name=tool_call.name, arguments=arguments_with_uuid, tool_call_id=tool_call.id
            ))

        logical_id = getattr(tool_call, 'logical_id', f"{self.tool_call_counter}:{tool_call.id}")
        pending_tool = {
            "logical_id": logical_id,
            "id": tool_call.id,
            "name": tool_call.name,
            "arguments": arguments_with_uuid,
            "status": "pending"
        }
        if not _prepared:
            pending_tool["metadata"] = dict(assistant_content=assistant_content, reasoning_content=reasoning_content, thinking_blocks=thinking_blocks, llm_turn=llm_turn)
            self.pending_tools.append(pending_tool)
        self._save_state(task_id, user_input, turn)  # 保存pending状态

        # 执行工具（使用带 uuid 的参数）
        invocation = SimpleNamespace(id=tool_call.id, name=tool_call.name,
                                     logical_id=logical_id)
        tool_result = _result if _result is not None else self._invoke_tool_durable(invocation, task_id, arguments_with_uuid)

        self._handle_special_tool_side_effects(
            tool_name=tool_call.name,
            tool_result=tool_result,
            task_id=task_id,
            user_input=user_input,
            turn=turn
        )

        # ✅ 执行后从pending移除
        self.pending_tools = [t for t in self.pending_tools if t["id"] != tool_call.id]
        
        # 发送工具结果事件
        self.event_emitter.dispatch(ToolCallEndEvent(
            tool_name=tool_call.name, 
            status=tool_result.get('status', 'unknown'), 
            result=tool_result, tool_call_id=tool_call.id
        ))

        # 记录动作到历史（增强格式：包含消息重建所需的字段）
        action_record = {
            "_turn": llm_turn,
            "tool_call_id": tool_call.id,
            "tool_name": tool_call.name,
            "arguments": arguments_with_uuid,
            "result": tool_result,
            "assistant_content": assistant_content,
            "reasoning_content": reasoning_content,  # 模型的推理/思考内容
            "thinking_blocks": thinking_blocks or [],
            "_has_image": False,
            "_image_base64": None
        }
        
        # 处理 image_read 工具返回（无论 multimodal 设置如何，都要清理 base64）
        if tool_call.name == "image_read":
            image_base64_list = None
            
            # 工具执行层会把工具返回值 json.dumps 到 output 字符串中
            # 所以 _image_base64_list 可能嵌套在 output 字符串里，需要解析提取
            output_str = tool_result.get("output", "")
            if isinstance(output_str, str) and ("_image_base64_list" in output_str or "_image_base64" in output_str):
                try:
                    inner_result = json.loads(output_str)
                    # 新格式：_image_base64_list（数组）
                    image_base64_list = inner_result.get("_image_base64_list")
                    # 兼容旧格式：_image_base64（单值）→ 转为列表
                    if not image_base64_list:
                        single = inner_result.get("_image_base64")
                        if single:
                            image_base64_list = [single]
                    
                    # 从 output 中移除所有 base64 数据
                    inner_result.pop("_image_base64_list", None)
                    inner_result.pop("_image_base64", None)
                    inner_result.pop("_multimodal", None)
                    tool_result["output"] = json.dumps(inner_result, indent=2, ensure_ascii=False)
                    action_record["result"] = tool_result
                except (json.JSONDecodeError, TypeError):
                    pass
            
            # 也检查顶层（以防未双重包装）
            if not image_base64_list:
                top_list = tool_result.get("_image_base64_list")
                top_single = tool_result.get("_image_base64")
                if top_list:
                    image_base64_list = top_list
                elif top_single:
                    image_base64_list = [top_single]
                tool_result.pop("_image_base64_list", None)
                tool_result.pop("_image_base64", None)
                tool_result.pop("_multimodal", None)
                action_record["result"] = tool_result
            
            # 只有当主模型支持多模态时，才将图片嵌入 messages
            if image_base64_list and self.llm_client.multimodal:
                action_record["_has_image"] = True
                action_record["_image_base64"] = image_base64_list  # 现在是列表
        
        if tool_call.name != 'final_output':
            tool_result = self._cap_tool_output(task_id, invocation.logical_id, tool_result)
            action_record['result'] = tool_result
        # Full tool output remains in the durable receipt and output_ref file.
        fact_record = {k: v for k, v in action_record.items() if k != "_image_base64"}
        fact_record["_image_base64"] = None  # fact 中不保留 base64，仅记录 _has_image 标志
        self.action_history_fact.append(fact_record)

        # 添加到渲染历史（会被压缩，保留 base64 用于 messages 重建）
        self.action_history.append(action_record)

        self.hierarchy_manager.add_action(self.agent_id, {
            "tool_name": tool_call.name,
            "arguments": arguments_with_uuid,
            "result": {k: v for k, v in tool_result.items() if not k.startswith("_")}
        })

        # Commit history before acknowledging child results.
        self.tool_call_counter += 1
        self._save_state(task_id, user_input, turn)
        self._acknowledge_activity_results(tool_result)
        self._check_tool_error_streak()
        
        # 如果是final_output，返回结果
        if tool_call.name == "final_output" and not tool_result.get("error_code"):
            return tool_result
        return None

    def _handle_special_tool_side_effects(self, tool_name: str, tool_result: Dict, task_id: str, user_input: str, turn: int):
        """处理 load_skill / offload_skill / fresh 等特殊副作用。"""
        if tool_name == "load_skill" and tool_result.get("status") == "success":
            skill_name = tool_result.get("_skill_name")
            if skill_name:
                self.hierarchy_manager.add_loaded_skill(self.agent_id, {
                    "name": skill_name,
                    "abs_path": tool_result.get("_skill_abs_path", ""),
                    "workspace_path": tool_result.get("_workspace_skill_path", ""),
                    "md_text": tool_result.get("_skill_md_text", "")
                })
            for k in ["_skill_name", "_skill_abs_path", "_workspace_skill_path", "_skill_md_text"]:
                tool_result.pop(k, None)

        elif tool_name == "offload_skill" and tool_result.get("status") == "success":
            skill_name = tool_result.get("_offload_skill_name")
            if skill_name:
                self.hierarchy_manager.remove_loaded_skill(self.agent_id, skill_name)
            tool_result.pop("_offload_skill_name", None)

        elif tool_name == "fresh" and tool_result.get("_fresh_requested"):
            reason = tool_result.get("_fresh_reason") or "manual fresh requested"
            target_task_id = str(tool_result.get("_fresh_task_id") or "").strip() or task_id
            if target_task_id == task_id:
                ok, msg = self._perform_fresh(task_id, user_input, turn, reason)
            elif is_task_running(target_task_id):
                request_fresh(reason=reason, task_id=target_task_id)
                ok = True
                msg = f"已向运行中的任务发送 fresh 请求: {target_task_id}"
            else:
                ok, msg = resume_task_with_fresh(
                    task_id=target_task_id,
                    reason=reason,
                    fallback_agent_system=self.config_loader.agent_system_name,
                    direct_tools=self.direct_tools,
                )
            if ok:
                tool_result["output"] = f"{tool_result.get('output', '').strip()}\n\n{msg}".strip()
            else:
                tool_result["status"] = "error"
                tool_result["error"] = msg
                tool_result["output"] = ""
            tool_result.pop("_fresh_requested", None)
            tool_result.pop("_fresh_reason", None)
            tool_result.pop("_fresh_task_id", None)

    def _maybe_run_scheduled_fresh(self, task_id: str, user_input: str, turn: int):
        check_cancelled()
        scope = CURRENT.get()
        if scope and scope.parent_id:
            return
        if getattr(self.tool_executor, 'activities', None) and self.tool_executor.activities.outstanding():
            return
        external_reason = pop_fresh_request(task_id)
        if external_reason:
            self._perform_fresh(task_id, user_input, turn, external_reason)
            return
        if not self.fresh_enabled or self.fresh_interval_sec <= 0:
            return
        if time.time() - self.last_fresh_at < self.fresh_interval_sec:
            return
        self._perform_fresh(task_id, user_input, turn, "scheduled fresh")

    def _perform_fresh(self, task_id: str, user_input: str, turn: int, reason: str):
        """
        在安全点刷新运行时配置/注册表/skill缓存，并继续当前任务。
        """
        if has_running_background_processes(task_id):
            return False, "当前仍有后台工具在运行，暂不允许 fresh。请等待后台工具结束后再刷新。"
        activities = getattr(self.tool_executor, 'activities', None)
        if activities is not None:
            if activities.outstanding():
                return False, 'Collect child results before refreshing the agent'
            activities.close(cancel_pending=False)

        self.event_emitter.dispatch(CliDisplayEvent(
            message=f"🔄 Fresh 开始: {reason}",
            style='info'
        ))

        apply_runtime_env_defaults()
        reset_skill_loader()
        reload_runtime_registry()
        reload_mcp_tools()

        # 重新读取运行时参数
        runtime = get_runtime_settings()
        self.last_fresh_at = time.time()

        # 重新加载 config_loader / agent_config / llm_client / context_builder / tool_executor
        loader_cls = self.config_loader.__class__
        self.config_loader = loader_cls(self.config_loader.agent_system_name)
        self.agent_config = self.config_loader.get_tool_config(self.agent_name)
        self.available_tools = list(self.agent_config.get("available_tools", []))
        for framework_tool in ("task_history_search", "load_skill", "offload_skill", "spawn_subagents", "collect", "cancel", "collect_subagents", "cancel_subagents"):
            if framework_tool not in self.available_tools and framework_tool in self.config_loader.all_tools:
                self.available_tools.append(framework_tool)
        self._inject_mcp_tools()

        self.llm_client = SimpleLLMClient()
        self.llm_client.set_tools_config(self.config_loader.all_tools)
        requested_model = self._get_agent_model_preference("execution")
        self.execution_model = self.llm_client.resolve_model("execution", requested_model)
        self.model_type = self.execution_model

        self.context_builder = ContextBuilder(
            self.hierarchy_manager,
            agent_config=self.agent_config,
            config_loader=self.config_loader,
            llm_client=self.llm_client,
            max_context_window=self.llm_client.max_context_window
        )
        self._apply_runtime_settings(runtime)
        self.tool_executor = ToolExecutor(
            self.config_loader,
            self.hierarchy_manager,
            direct_mode=self.direct_tools,
            extra_event_handlers=self.extra_event_handlers,
            exit_on_error=self.exit_on_error,
            raise_on_error=self.raise_on_error,
        )
        self.tool_executor.set_agent_context(agent_id=self.agent_id, agent_name=self.agent_name)
        self.tool_executor.available_agent_tools = set(self.available_tools)
        if hasattr(self, "action_compressor"):
            delattr(self, "action_compressor")

        self._save_state(task_id, user_input, turn)
        self.event_emitter.dispatch(CliDisplayEvent(
            message=f"✅ Fresh 完成，已重载配置/工具注册/skills 缓存，并继续当前任务",
            style='success'
        ))
        return True, "Fresh 完成，已重载配置/工具注册/skills 缓存，并继续当前任务。"

    def _handle_execution_error(self, e: Exception) -> Dict:
        """统一处理执行过程中的异常"""
        # 获取详细错误信息
        error_type = type(e).__name__
        error_msg = str(e)
        error_traceback = traceback.format_exc()
        
        # 构建友好的错误提示消息
        error_display = f"""
❌ 执行过程中发生错误，任务已中断
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🔴 错误类型: {error_type}
📝 错误信息: {error_msg}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📋 详细堆栈:
{error_traceback}
"""
        
        # 添加当前进度信息
        if self.latest_thinking:
            error_display += f"\n💭 当前进度:\n{self.latest_thinking[:500]}\n"
        
        error_display += """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
💡 请检查最后一次成功保存的状态；存储错误时本轮可能尚未提交。请:
   1. 根据错误信息排查问题（修复网络、配置等）
   2. 重新启动 CLI 并输入 /resume 命令恢复任务
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
        error_result = {
            "status": "error",
            "task_id": self.current_task_id,
            "agent_name": self.agent_name,
            "output": "",
            "error_type": error_type,
            "error_code": getattr(e, "code", "EXECUTION_ERROR"),
            "error_message": error_msg,
            "error_information": error_display,
            "latest_thinking": self.latest_thinking,
        }
        error_result = self._with_model_outputs(error_result)
        # 通过事件发送错误
        self.event_emitter.dispatch(ErrorEvent(error_display=error_display))
        self.event_emitter.dispatch(AgentEndEvent(status='error', result=error_result))
        if self.raise_on_error:
            raise InfiAgentRunError.from_result(
                error_result,
                task_id=self.current_task_id or "",
                agent_name=self.agent_name,
                stage="run",
            )
        if self.exit_on_error:
            sys.exit(1)
        return error_result

    def _add_uuid_if_needed(
            self, 
            tool_name: str, 
            arguments: Dict
        ) -> Dict:
        """
        为 level != 0 的工具添加 uuid 后缀到 task_input
        
        Args:
            tool_name: 工具名称
            arguments: 原始参数
            
        Returns:
            处理后的参数（如果需要添加 uuid，返回新字典；否则返回原字典）
        """
        try:
            # 获取工具配置
            tool_config = self.config_loader.get_tool_config(tool_name)
            tool_level = tool_config.get("level", 0)
            tool_type = tool_config.get("type", "")
            
            # 只对 level != 0 的 llm_call_agent 添加 uuid
            if tool_type == "llm_call_agent" and tool_level != 0 and "task_input" in arguments:
                import uuid
                # 创建新字典（避免修改原始参数）
                new_arguments = arguments.copy()
                original_input = arguments["task_input"]
                random_suffix = f" [call-{uuid.uuid4().hex[:8]}]"
                new_arguments["task_input"] = original_input + random_suffix
                self.event_emitter.dispatch(CliDisplayEvent(
                    message=f"   🔖 为 level {tool_level} 工具添加 uuid 后缀", 
                    style='info'
                ))
                return new_arguments
            
            # 其他情况返回原参数
            return arguments
        
        except Exception as e:
            self.event_emitter.dispatch(CliDisplayEvent(
                message=f"⚠️ 添加 uuid 时出错: {e}", 
                style='warning'
            ))
            return arguments
    
    def _trigger_thinking(self, task_id: str, task_input: str, is_initial: bool = False, is_forced: bool = False) -> str:
        """
        触发Thinking Agent进行分析
        
        Args:
            task_id: 任务ID
            task_input: 任务输入
            is_initial: 是否是首次thinking
            is_forced: 是否因为多次未调用工具而被强制触发thinking
            
        Returns:
            分析结果
        """
        # 发送Thinking开始事件
        self.event_emitter.dispatch(ThinkingStartEvent(
            agent_name=self.agent_name, 
            is_initial=is_initial, 
            is_forced=is_forced
        ))
        try:
            from services.thinking_agent import ThinkingAgent

            thinking_agent = ThinkingAgent(
                preferred_model=self._get_agent_model_preference("thinking"),
                max_tokens=self.agent_config.get("max_tokens"),
            )

            # 构建完整的系统提示词（包含历史动作XML，供 thinking agent 分析）
            full_system_prompt = self.context_builder.build_context(
                task_id,
                self.agent_id,
                self.agent_name,
                task_input,
                action_history=self.action_history,
                include_action_history=True  # thinking agent 需要看到历史动作
            )
            thinking_payload = thinking_agent.analyze_first_thinking_detail(
                task_description=task_input,
                agent_system_prompt=full_system_prompt,
                available_tools=self.available_tools,
                tools_config=self.config_loader.all_tools,
                action_history=self.action_history,  # 传递 action_history（含图片数据）
                multimodal=self.llm_client.multimodal,  # 传递多模态标志
                debug_task_id=task_id,
                stream_callback=self._build_llm_stream_callback(
                    stream_group="thinking",
                    agent_name=self.agent_name,
                    model=thinking_agent.llm_client.resolve_model("thinking", thinking_agent.preferred_model),
                    is_initial=is_initial,
                    is_forced=is_forced,
                ) if self.stream_llm_tokens else None,
            )
            result = thinking_payload["formatted_result"]
            thinking_record = {
                "model": thinking_payload.get("model", ""),
                "content": thinking_payload.get("raw_output", ""),
                "reasoning_content": thinking_payload.get("raw_reasoning_content", ""),
                "formatted_result": result,
                "finish_reason": thinking_payload.get("finish_reason", ""),
                "status": thinking_payload.get("status", "success"),
                "is_initial": bool(is_initial),
                "is_forced": bool(is_forced),
            }
            self.thinking_traces.append(thinking_record)
            # 发送 thinking 事件（完整内容）
            self.event_emitter.dispatch(ThinkingEndEvent(
                agent_name=self.agent_name, 
                result=result,
                model=thinking_record["model"],
                raw_output=thinking_record["content"],
                raw_reasoning_content=thinking_record["reasoning_content"],
                finish_reason=thinking_record["finish_reason"],
                is_initial=is_initial,
                is_forced=is_forced
            ))
            return result
        except Exception as e:
            error_msg = str(e)
            # 发送Thinking失败事件
            self.event_emitter.dispatch(ThinkingFailEvent(
                agent_name=self.agent_name, 
                error_message=error_msg
            ))
            raise Exception(str(e))

    def _build_llm_stream_callback(
        self,
        *,
        stream_group: str,
        agent_name: str,
        model: str,
        debug_label: str = "execution",
        content_as_reasoning: bool = False,
        is_initial: bool = False,
        is_forced: bool = False,
    ):
        def _callback(chunk: Dict[str, Any]):
            kind = str((chunk or {}).get("kind") or "content").strip().lower()
            text = str((chunk or {}).get("text") or "")
            attempt = int((chunk or {}).get("attempt") or 1)
            if kind == "reset":
                event_type = "run.thinking.reset" if stream_group == "thinking" else "run.llm.reset"
                payload = {
                    "agent_name": agent_name,
                    "model": str((chunk or {}).get("model") or model or ""),
                    "attempt": attempt,
                    "reason": str((chunk or {}).get("reason") or "retry"),
                    "debug_label": str((chunk or {}).get("debug_label") or debug_label or ""),
                }
                self._emit_sdk_stream_event(event_type, payload)
                return
            if not text:
                return
            current_model = str((chunk or {}).get("model") or model or "")
            if stream_group == "thinking":
                event_type = "run.thinking.reasoning_token" if kind == "reasoning" else "run.thinking.token"
                payload = {
                    "agent_name": agent_name,
                    "model": current_model,
                    "text": text,
                    "token_kind": kind,
                    "attempt": attempt,
                    "is_initial": bool(is_initial),
                    "is_forced": bool(is_forced),
                    "debug_label": str((chunk or {}).get("debug_label") or debug_label or ""),
                }
            else:
                effective_kind = "reasoning" if content_as_reasoning and kind == "content" else kind
                event_type = "run.llm.reasoning_token" if effective_kind == "reasoning" else "run.llm.token"
                payload = {
                    "agent_name": agent_name,
                    "model": current_model,
                    "text": text,
                    "token_kind": effective_kind,
                    "attempt": attempt,
                    "debug_label": str((chunk or {}).get("debug_label") or debug_label or ""),
                }
            self._emit_sdk_stream_event(event_type, payload)

        return _callback

    def _build_model_outputs_payload(self) -> Dict[str, Any]:
        execution_traces = list(getattr(self, "execution_traces", []) or [])
        thinking_traces = list(getattr(self, "thinking_traces", []) or [])
        execution_only = [item for item in execution_traces if str(item.get("debug_label") or "execution") == "execution"]
        last_execution = execution_only[-1] if execution_only else (execution_traces[-1] if execution_traces else None)
        last_thinking = thinking_traces[-1] if thinking_traces else None
        return {
            "execution_turns": execution_traces,
            "thinking_turns": thinking_traces,
            "last_execution": last_execution,
            "last_thinking": last_thinking,
        }

    def _with_model_outputs(self, result: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(result or {})
        model_outputs = self._build_model_outputs_payload()
        payload["model_outputs"] = model_outputs

        last_execution = model_outputs.get("last_execution") or {}
        last_thinking = model_outputs.get("last_thinking") or {}

        payload["last_execution_output"] = str(last_execution.get("content") or "")
        payload["last_execution_reasoning_content"] = str(last_execution.get("reasoning_content") or "")
        payload["last_execution_model"] = str(last_execution.get("model") or "")
        payload["last_thinking_output"] = str(last_thinking.get("content") or "")
        payload["last_thinking_reasoning_content"] = str(last_thinking.get("reasoning_content") or "")
        payload["last_thinking_model"] = str(last_thinking.get("model") or "")
        return payload

    def _fit_execution_context_to_budget(
        self,
        *,
        task_id: str,
        user_input: str,
        system_prompt: str,
        messages: List[Dict],
        turn: int,
    ) -> tuple[str, List[Dict]]:
        """
        Final preflight for the provider-facing execution request.

        The normal compressor only sees action_history. The real request also
        includes system context, sanitized messages, tool schemas, output
        reservation, and safety margin. If that final request is over budget,
        compress action_history with a tighter target and rebuild the context.
        """
        effective_tool_list = list(self.available_tools)
        max_tokens_override = self.agent_config.get("max_tokens")

        for attempt in range(4):
            budget, outbound_message_count, tool_count = self.llm_client.build_chat_request_budget(
                system_prompt=system_prompt,
                history=messages,
                tool_list=effective_tool_list,
                max_tokens=max_tokens_override,
                force_exact=False,
            )
            if attempt > 0 or budget.total_with_reserved_tokens >= int(self.llm_client.max_context_window * 0.7):
                budget, outbound_message_count, tool_count = self.llm_client.build_chat_request_budget(
                    system_prompt=system_prompt,
                    history=messages,
                    tool_list=effective_tool_list,
                    max_tokens=max_tokens_override,
                    force_exact=True,
                )
            if not budget.over_budget:
                if attempt > 0:
                    self.event_emitter.dispatch(CliDisplayEvent(
                        message=(
                            "✅ 上下文已压缩到请求预算内: "
                            f"used_input={budget.used_input_tokens}, "
                            f"available_input={budget.available_input_tokens}, "
                            f"messages={outbound_message_count}, tools={tool_count}"
                        ),
                        style='success'
                    ))
                return system_prompt, messages

            self.event_emitter.dispatch(CliDisplayEvent(
                message=(
                    "⚠️ 最终LLM请求超出上下文预算，准备压缩历史动作: "
                    f"used_input={budget.used_input_tokens}, "
                    f"available_input={budget.available_input_tokens}, "
                    f"system={budget.system_prompt_tokens}, "
                    f"messages={budget.message_tokens}, tools={budget.tool_tokens}"
                ),
                style='warning'
            ))

            if not self.action_history:
                return system_prompt, messages

            non_action_budget = (
                budget.system_prompt_tokens
                + budget.tool_tokens
                + budget.image_surcharge_tokens
                + 2048
            )
            target_action_tokens = int((budget.available_input_tokens - non_action_budget) * 0.85)
            # Tighten on repeated attempts; message JSON/tool-call structure can be larger
            # than the XML-ish estimate used by ActionCompressor.
            target_action_tokens = int(target_action_tokens * (0.75 ** attempt))
            target_action_tokens = max(512, target_action_tokens)

            changed = self._compress_action_history_if_needed(
                max_action_tokens=target_action_tokens,
                force=True,
                reason=f"preflight_over_budget_attempt_{attempt + 1}",
            )
            if not changed:
                changed = self._hard_trim_action_history_for_budget(target_action_tokens)
                if changed:
                    self.event_emitter.dispatch(CliDisplayEvent(
                        message=f"⚠️ LLM压缩未缩小上下文，已启用保底截断到约 {target_action_tokens} tokens",
                        style='warning'
                    ))

            if not changed:
                return system_prompt, messages

            system_prompt = self.context_builder.build_context(
                task_id,
                self.agent_id,
                self.agent_name,
                user_input,
                action_history=self.action_history,
                include_action_history=False
            )
            messages = self._build_messages_from_action_history()
            try:
                self._save_state(task_id, user_input, turn)
            except Exception:
                pass

        return system_prompt, messages

    def _truncate_text_for_context(self, text: Any, max_tokens: int) -> str:
        value = str(text or "")
        if not value:
            return value
        try:
            if not hasattr(self, 'action_compressor'):
                from services.action_compressor import ActionCompressor
                self.action_compressor = ActionCompressor(
                    self.llm_client,
                    preferred_model=self._get_agent_model_preference("compressor"),
                    max_tokens=self.agent_config.get("max_tokens"),
                    debug_task_id=self.current_task_id,
                )
            return self.action_compressor._fallback_compress(value, max(64, int(max_tokens)))
        except Exception:
            chars = max(256, int(max_tokens) * 3)
            half = max(128, chars // 2)
            return f"{value[:half]}\n\n[中间内容因上下文超限被省略]\n\n{value[-half:]}"

    def _hard_trim_action_for_context(self, action: Dict[str, Any], max_tokens: int) -> Dict[str, Any]:
        trimmed = copy.deepcopy(action or {})
        per_field_tokens = max(128, int(max_tokens) // 4)

        for field_name in ("assistant_content", "reasoning_content"):
            if trimmed.get(field_name):
                trimmed[field_name] = self._truncate_text_for_context(trimmed.get(field_name), per_field_tokens)

        arguments = trimmed.get("arguments")
        if isinstance(arguments, dict):
            trimmed_args = {}
            for key, value in arguments.items():
                value_text = str(value)
                if len(value_text) > per_field_tokens * 4:
                    trimmed_args[key] = self._truncate_text_for_context(value_text, per_field_tokens)
                else:
                    trimmed_args[key] = value
            trimmed["arguments"] = trimmed_args

        result = trimmed.get("result")
        if isinstance(result, dict) and result.get("output"):
            result["output"] = self._truncate_text_for_context(result.get("output"), max(128, int(max_tokens) // 2))
            result["_hard_trimmed"] = True

        if trimmed.get("_image_base64"):
            trimmed["_image_base64"] = None
            trimmed["_has_image"] = False
            result = trimmed.setdefault("result", {})
            if isinstance(result, dict):
                output = str(result.get("output") or "")
                result["output"] = (output + "\n\n[图片base64已因上下文预算被省略]").strip()

        return trimmed

    def _hard_trim_action_history_for_budget(self, max_action_tokens: int) -> bool:
        if not self.action_history:
            return False

        original = copy.deepcopy(self.action_history)
        if len(self.action_history) == 1:
            self.action_history = [
                self._hard_trim_action_for_context(
                    self.action_history[0],
                    max(256, int(max_action_tokens)),
                )
            ]
        else:
            older_actions = self.action_history[:-1]
            latest_action = self.action_history[-1]
            tool_names = [str(action.get("tool_name") or "") for action in older_actions if action.get("tool_name")]
            summary = (
                "Older actions were omitted by deterministic context-budget fallback. "
                f"count={len(older_actions)}; tools={', '.join(tool_names[-20:])}"
            )
            self.action_history = [
                {
                    "tool_name": "_historical_summary",
                    "arguments": {},
                    "result": {
                        "status": "success",
                        "output": summary,
                        "_is_summary": True,
                        "_hard_trimmed": True,
                    },
                },
                self._hard_trim_action_for_context(
                    latest_action,
                    max(256, int(max_action_tokens) // 2),
                ),
            ]

        return self.action_history != original

    def _compress_action_history_if_needed(
        self,
        *,
        max_action_tokens: Optional[int] = None,
        force: bool = False,
        reason: str = "auto",
    ) -> bool:
        """检查并压缩历史动作（如果超过上下文窗口限制）"""
        if not self.action_history:
            return False
        
        try:
            from services.action_compressor import ActionCompressor

            # 初始化压缩器（如果还没有）
            if not hasattr(self, 'action_compressor'):
                self.action_compressor = ActionCompressor(
                    self.llm_client,
                    preferred_model=self._get_agent_model_preference("compressor"),
                    max_tokens=self.agent_config.get("max_tokens"),
                    debug_task_id=self.current_task_id,
                )
            else:
                self.action_compressor.debug_task_id = self.current_task_id
	            
            # 使用新的压缩策略（传入 thinking 和 task_input）
            original_len = len(self.action_history)
            original_snapshot = copy.deepcopy(self.action_history)
            compressed = self.action_compressor.compress_if_needed(
                self.action_history,
                self.llm_client.max_context_window,
                thinking=self.latest_thinking,
                task_input=self.current_task_input,
                max_action_tokens=max_action_tokens,
                force=force,
            )

            # 如果发生了压缩，替换
            changed = compressed != original_snapshot
            if changed:
                self.event_emitter.dispatch(CliDisplayEvent(
                    message=f"✅ 历史动作已压缩({reason}): {original_len}条 → {len(compressed)}条", 
                    style='success'
                ))
                self.action_history = compressed
            return changed
        except Exception as e:
            self.event_emitter.dispatch(CliDisplayEvent(
                message=f"⚠️ 压缩失败: {e}", 
                style='warning'
            ))
            traceback.print_exc()
            return False
    
    def _save_state(self, task_id: str, user_input: str, current_turn: int):
        """
        保存当前状态
        
        Args:
            task_id: 任务ID
            user_input: 用户输入
            current_turn: 当前轮次
        """
        # 构建完整的系统提示词（包含历史动作XML，用于调试/参考）
        full_system_prompt = self.context_builder.build_context(
            task_id,
            self.agent_id,
            self.agent_name,
            user_input,
            action_history=self.action_history,
            include_action_history=True  # 保存时包含完整上下文
        )

        # 保存状态
        self.conversation_storage.save_actions(
            task_id=task_id,
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            task_input=user_input,
            action_history=self.action_history,  # 渲染用（会压缩，含 base64）
            action_history_fact=self.action_history_fact,  # 完整轨迹（不含 base64）
            pending_tools=self.pending_tools,
            current_turn=current_turn,
            latest_thinking=self.latest_thinking,
            first_thinking_done=self.first_thinking_done,
            tool_call_counter=self.tool_call_counter,
            llm_turn_counter=self.llm_turn_counter,
            system_prompt=full_system_prompt
        )


if __name__ == "__main__":
    from utils.config_loader import ConfigLoader
    from core.hierarchy_manager import get_hierarchy_manager
    
    # 测试
    config_loader = ConfigLoader("infiHelper")
    hierarchy_manager = get_hierarchy_manager("test_task")

    hierarchy_manager.start_new_instruction("测试任务")

    # 获取writing_agent配置
    agent_config = config_loader.get_tool_config("alpha_agent")

    safe_print(f"✅ Agent配置: {agent_config.get('name')}")
    safe_print(f"   Tools: {len(agent_config.get('available_tools', []))}")
