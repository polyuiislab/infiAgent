#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent执行器 - 使用标准消息格式的核心执行逻辑
历史动作通过 messages 数组传递（而非 system_prompt），支持多模态图片嵌入
"""
from typing import Dict, List
import sys
import json
import traceback
from collections import OrderedDict

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

from .agent_event_emitter import AgentEventEmitter
from .event_handlers import ConsoleLogHandler, JsonlStreamHandler
from .events import *
from utils.windows_compat import safe_print


class AgentExecutor:
    """Agent执行器 - 正确的XML上下文架构"""
    
    def __init__(
        self,
        agent_name: str,
        agent_config: Dict,
        config_loader,
        hierarchy_manager
    ):
        """初始化Agent执行器"""
        self.agent_name = agent_name
        self.agent_config = agent_config
        self.config_loader = config_loader
        self.hierarchy_manager = hierarchy_manager
        
        self._setup_event_emitter()

        # 从配置中提取信息
        self.available_tools = agent_config.get("available_tools", [])
        self.max_turns = 10000000
        requested_model = agent_config.get("model_type", "claude-3-7-sonnet-20250219")
        
        # 初始化LLM客户端
        self.llm_client = SimpleLLMClient()
        self.llm_client.set_tools_config(config_loader.all_tools)
        
        # 验证并调整模型
        available_models = self.llm_client.models
        final_model = requested_model
        is_fallback = False
        if requested_model not in available_models:
            final_model = available_models[0]
            is_fallback = True
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
        self.tool_executor = ToolExecutor(config_loader, hierarchy_manager)
        
        # 初始化对话存储
        self.conversation_storage = ConversationStorage()
        
        # Agent状态
        self.agent_id = None
        self.action_history = []  # 渲染用（会压缩）
        self.action_history_fact = []  # 完整轨迹（不压缩）
        self.pending_tools = []  # 待执行的工具（用于恢复）
        self.latest_thinking = ""
        self.first_thinking_done = False
        self.thinking_interval = 10  # 每10轮工具调用触发一次thinking
        self.tool_call_counter = 0
        self.llm_turn_counter = 0  # LLM调用轮次计数器（用于消息分组）

    def _setup_event_emitter(self):
        """初始化事件发射器并注册处理器"""
        self.event_emitter = AgentEventEmitter()
        self.event_emitter.register(ConsoleLogHandler())
        
        jsonl_emitter = get_jsonl_emitter()
        if jsonl_emitter.enabled:
            self.event_emitter.register(JsonlStreamHandler(enabled=True))
    
    def run(self, task_id: str, user_input: str) -> Dict:
        """执行Agent任务"""

        self.event_emitter.dispatch(AgentStartEvent(
            agent_name=self.agent_name, 
            task_input=user_input
        ))        
        # 存储 task_input 供压缩器使用
        self.current_task_input = user_input

        # Agent入栈
        self.agent_id = self.hierarchy_manager.push_agent(self.agent_name, user_input)

        # 尝试加载已有的对话历史
        start_turn = self._load_state_from_storage(task_id)
        
        try:
            # 首次thinking（初始规划）
            if start_turn == 0 and not self.first_thinking_done:
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
            self._handle_execution_error(e)
            # sys.exit(1) is called inside, so we don't need to return
        
        # 强制工具调用计数器
        max_tool_try = 0

        # 执行循环
        for turn in range(start_turn, self.max_turns):
            self.event_emitter.dispatch(CliDisplayEvent(
                message=f"\n--- 第 {turn + 1}/{self.max_turns} 轮执行 ---", 
                style='separator'
            ))
            
            try:
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
                
                # 调用LLM（使用标准 messages 格式）
                llm_response = self._execute_llm_call(full_system_prompt, messages)
                
                if llm_response.status != "success":
                    error_result = {
                        "status": "error",
                        "output": "LLM调用失败",
                        "error_information": llm_response.error_information
                    }
                    self.hierarchy_manager.pop_agent(self.agent_id, str(error_result))
                    self.event_emitter.dispatch(AgentEndEvent(status='error', result=error_result))
                    return error_result

                if not llm_response.tool_calls:
                    # 强制工具调用机制

                    if max_tool_try < 5:
                        max_tool_try += 1
                        self.event_emitter.dispatch(CliDisplayEvent(
                            message=f"⚠️ LLM未调用工具，第{max_tool_try}/5次提醒", 
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
                            "assistant_content": llm_response.output or ""
                        })
                        self.llm_turn_counter += 1
                        self._save_state(task_id, user_input, turn)
                        continue
                    else:
                        # 5次后仍不调用，触发thinking并报错
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
                        self.hierarchy_manager.pop_agent(self.agent_id, str(error_result))
                        self.event_emitter.dispatch(AgentEndEvent(status='error', result=error_result))
                        self.event_emitter.dispatch(ThinkingFailEvent(agent_name=self.agent_name, error_message=f"[{self.agent_name}] 强制thinking: {thinking_result if thinking_result else '分析失败'}"))
                        return error_result
                # 重置计数器（成功调用了工具）
                max_tool_try = 0

                # 提取本轮 LLM 输出的文本内容和推理内容（所有 tool_call 共享）
                current_assistant_content = llm_response.output or ""
                current_reasoning_content = llm_response.reasoning_content or ""
                current_llm_turn = self.llm_turn_counter

                # 执行所有工具调用
                for tool_call in llm_response.tool_calls:
                    final_output_result = self._execute_tool_call(
                        tool_call, task_id, user_input, turn,
                        assistant_content=current_assistant_content,
                        reasoning_content=current_reasoning_content,
                        llm_turn=current_llm_turn
                    )
                    if final_output_result:
                        self.event_emitter.dispatch(AgentEndEvent(status='success', result=final_output_result))
                        self.hierarchy_manager.pop_agent(self.agent_id, final_output_result.get("output", ""))
                        return final_output_result
                
                self.llm_turn_counter += 1
                
                # 检查是否该触发thinking（每N轮工具调用）
                # 用整除判断是否跨过了 thinking_interval 边界（避免多 tool_call 跳过边界值）
                counter_before = self.tool_call_counter - len(llm_response.tool_calls)
                crossed_boundary = (counter_before // self.thinking_interval) < (self.tool_call_counter // self.thinking_interval)
                if self.tool_call_counter > 0 and crossed_boundary:
                    thinking_result = self._trigger_thinking(task_id, user_input, is_initial=False)
                    if thinking_result:
                        self.latest_thinking = thinking_result
                        self.hierarchy_manager.update_thinking(self.agent_id, thinking_result)
                        self._save_state(task_id, user_input, turn)
                        self.action_history = []
                        self.llm_turn_counter = 0  # 重置轮次计数器
            
            except Exception as e:
                self._handle_execution_error(e)
        timeout_result = {
            "status": "error",
            "output": f"执行超过最大轮次限制: {self.max_turns}",
            "error_information": f"Max turns {self.max_turns} exceeded"
        }
        self.hierarchy_manager.pop_agent(self.agent_id, str(timeout_result))
        self.event_emitter.dispatch(AgentEndEvent(status='error', result=timeout_result))
        self.event_emitter.dispatch(CliDisplayEvent(
            message="\n⚠️ 达到最大轮次限制: {self.max_turns}"
        ))
        
        return timeout_result

    def _load_state_from_storage(self, task_id: str) -> int:
        """从存储加载状态, 返回起始轮次."""
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
            
            # 检查是否已经完成（有final_output）
            for action in self.action_history_fact:
                if action.get("tool_name") == "final_output":
                    final_result = action.get("result", {})
                    self.event_emitter.dispatch(CliDisplayEvent(
                        message=f"\n✅ 任务已完成，直接返回之前的final_output结果\n   状态: {final_result.get('status')}", 
                        style='success'
                    ))
                    return final_result
            
            # 恢复pending工具（如果有）
            if self.pending_tools:
                self._recover_pending_tools(task_id)

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
        # 初始 user 消息
        messages = [{
            "role": "user", 
            "content": "请根据当前任务和上下文，执行下一步操作。请调用合适的工具来完成任务。不要重复已执行的动作！"
        }]
        
        if not self.action_history:
            return messages
        
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
            
            # 特殊处理：LLM 未调用工具
            if tool_name == "_no_tool_call":
                assistant_content = action.get("assistant_content", "")
                if assistant_content:
                    messages.append({"role": "assistant", "content": assistant_content})
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
                    "tool_calls": [],
                    "tool_results": [],
                    "images": []
                }
            
            # 构建 tool_call 条目
            tool_call_id = action.get("tool_call_id", f"call_{turn}_{len(turns[turn]['tool_calls'])}")
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
            
            # assistant 消息（包含 content、tool_calls、reasoning_content）
            assistant_msg = {
                "role": "assistant",
                "content": turn_data["assistant_content"] or None,
                "tool_calls": turn_data["tool_calls"]
            }
            # 如果有 reasoning_content，添加到 assistant 消息中
            # LiteLLM 会将其传递给支持 thinking 的模型（如 Anthropic Claude）
            if turn_data.get("reasoning_content"):
                assistant_msg["reasoning_content"] = turn_data["reasoning_content"]
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
        
        return messages

    def _execute_llm_call(self, system_prompt: str, messages: List[Dict] = None):
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
            model=self.model_type, 
            system_prompt=system_prompt
        ))
        
        # 调用LLM（重试机制已在 llm_client 内部实现）
        llm_response = self.llm_client.chat(
            history=messages,
            model=self.model_type,
            system_prompt=system_prompt,
            tool_list=self.available_tools,
            tool_choice="required"  # 强制工具调用
        )
        
        self.event_emitter.dispatch(LlmCallEndEvent(
            llm_output=llm_response.output, 
            tool_calls=llm_response.tool_calls
        ))
        return llm_response

    def _execute_tool_call(self, tool_call: Dict, task_id: str, user_input: str, turn: int,
                          assistant_content: str = "", reasoning_content: str = "",
                          llm_turn: int = 0) -> Dict:
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
        arguments_with_uuid = self._add_uuid_if_needed(tool_call.name, tool_call.arguments)
        
        # ✅ 先标记为pending（保存带 uuid 的参数）
        # 发送工具调用开始事件
        self.event_emitter.dispatch(ToolCallStartEvent(
            tool_name=tool_call.name, 
            arguments=arguments_with_uuid
        ))

        pending_tool = {
            "id": tool_call.id,
            "name": tool_call.name,
            "arguments": arguments_with_uuid,
            "status": "pending"
        }
        self.pending_tools.append(pending_tool)
        self._save_state(task_id, user_input, turn)  # 保存pending状态

        # 执行工具（使用带 uuid 的参数）
        tool_result = self.tool_executor.execute(
            tool_call.name,
            arguments_with_uuid,
            task_id
        )

        # ✅ 执行后从pending移除
        self.pending_tools = [t for t in self.pending_tools if t["id"] != tool_call.id]
        
        # 发送工具结果事件
        self.event_emitter.dispatch(ToolCallEndEvent(
            tool_name=tool_call.name, 
            status=tool_result.get('status', 'unknown'), 
            result=tool_result
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
            "_has_image": False,
            "_image_base64": None
        }
        
        # 处理 image_read 工具返回（无论 multimodal 设置如何，都要清理 base64）
        if tool_call.name == "image_read":
            image_base64_list = None
            
            # ToolServer 的 _call_toolserver 会把工具返回值 json.dumps 到 output 字符串中
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
            
            # 也检查顶层（以防 ToolServer 未双重包装）
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
        
        # 添加到完整轨迹（永不压缩，但不存储 base64 以节省空间）
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

        # 工具执行后保存状态
        self._save_state(task_id, user_input, turn)
        
        # 增加工具调用计数
        self.tool_call_counter += 1
        
        # 如果是final_output，返回结果
        if tool_call.name == "final_output":
            return tool_result
        return None

    def _handle_execution_error(self, e: Exception):
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
💡 任务已保存在当前状态，请:
   1. 根据错误信息排查问题（修复网络、配置等）
   2. 重新启动 CLI 并输入 /resume 命令恢复任务
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
        # 通过事件发送错误
        self.event_emitter.dispatch(ErrorEvent(error_display=error_display))
        # 直接退出程序
        sys.exit(1)

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

            thinking_agent = ThinkingAgent()

            # 构建完整的系统提示词（包含历史动作XML，供 thinking agent 分析）
            full_system_prompt = self.context_builder.build_context(
                task_id,
                self.agent_id,
                self.agent_name,
                task_input,
                action_history=self.action_history,
                include_action_history=True  # thinking agent 需要看到历史动作
            )
            result = thinking_agent.analyze_first_thinking(
                task_description=task_input,
                agent_system_prompt=full_system_prompt,
                available_tools=self.available_tools,
                tools_config=self.config_loader.all_tools,
                action_history=self.action_history,  # 传递 action_history（含图片数据）
                multimodal=self.llm_client.multimodal  # 传递多模态标志
            )
            # 发送 thinking 事件（完整内容）
            self.event_emitter.dispatch(ThinkingEndEvent(
                agent_name=self.agent_name, 
                result=result,
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

    def _compress_action_history_if_needed(self):
        """检查并压缩历史动作（如果超过上下文窗口限制）"""
        if not self.action_history:
            return
        
        try:
            from services.action_compressor import ActionCompressor

            # 初始化压缩器（如果还没有）
            if not hasattr(self, 'action_compressor'):
                self.action_compressor = ActionCompressor(self.llm_client)
            
            # 使用新的压缩策略（传入 thinking 和 task_input）
            original_len = len(self.action_history)
            compressed = self.action_compressor.compress_if_needed(
                self.action_history,
                self.llm_client.max_context_window,
                thinking=self.latest_thinking,
                task_input=self.current_task_input
            )

            # 如果发生了压缩，替换
            if len(compressed) < original_len:
                self.event_emitter.dispatch(CliDisplayEvent(
                    message=f"✅ 历史动作已压缩: {original_len}条 → {len(compressed)}条", 
                    style='success'
                ))
                self.action_history = compressed
        except Exception as e:
            self.event_emitter.dispatch(CliDisplayEvent(
                message=f"⚠️ 压缩失败: {e}", 
                style='warning'
            ))
            traceback.print_exc()
    
    def _recover_pending_tools(self, task_id: str):
        """恢复pending状态的工具调用"""
        for pending_tool in self.pending_tools:
            tool_name, tool_args = pending_tool['name'], pending_tool['arguments']
            try:
                self.event_emitter.dispatch(CliDisplayEvent(
                    message=f"   🔄 恢复执行: {tool_name}\n   📋 参数: {tool_args}", 
                    style='info'
                ))
                
                # 重新执行工具
                tool_result = self.tool_executor.execute(
                    tool_name,
                    tool_args,
                    task_id
                )
                
                # 记录结果
                action_record = {
                    "tool_name": tool_name,
                    "arguments": tool_args,
                    "result": tool_result
                }
                
                self.action_history_fact.append(action_record)
                self.action_history.append(action_record)
                
                # 从pending移除
                self.pending_tools.remove(pending_tool)
                
                self.event_emitter.dispatch(CliDisplayEvent(
                    message=f"   ✅ 恢复完成: {tool_name}", 
                    style='success'
                ))
                
                # 如果是final_output，直接返回
                if tool_name == "final_output":
                    return tool_result
            except Exception as e:
                self.event_emitter.dispatch(CliDisplayEvent(
                    message=f"   ❌ 恢复失败: {tool_name} - {e}", 
                    style='error'
                ))
        # 清空pending列表
        self.pending_tools = []
    
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
