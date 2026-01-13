#!/usr/bin/env python3
from utils.windows_compat import safe_print
# -*- coding: utf-8 -*-
"""
Agent执行器 - 使用XML结构化上下文的核心执行逻辑
"""

# Windows兼容性：设置UTF-8编码
try:
    from utils.windows_compat import setup_console_encoding
    setup_console_encoding()
except ImportError:
    pass

import json
from typing import Dict, List
from services.llm_client import SimpleLLMClient, ChatMessage
from core.context_builder import ContextBuilder
from core.tool_executor import ToolExecutor
from utils.event_emitter import get_event_emitter


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
        
        # 从配置中提取信息
        self.available_tools = agent_config.get("available_tools", [])
        # self.max_turns = agent_config.get("max_turns", 100)
        self.max_turns = 10000000
        # 模型选择逻辑
        requested_model = agent_config.get("model_type", "claude-3-7-sonnet-20250219")
        self.model_type = requested_model
        
        # 初始化LLM客户端
        self.llm_client = SimpleLLMClient()
        self.llm_client.set_tools_config(config_loader.all_tools)
        
        # 验证并调整模型
        available_models = self.llm_client.models
        if self.model_type not in available_models:
            fallback_model = available_models[0]
            safe_print(f"⚠️请求的模型 '{self.model_type}' 不在可用列表中")
            safe_print(f"✅使用回退模型: {fallback_model}")
            self.model_type = fallback_model
        else:
            safe_print(f"✅使用请求的模型: {self.model_type}")
        
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
        from utils.conversation_storage import ConversationStorage
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
    
    def run(self, task_id: str, user_input: str) -> Dict:
        """执行Agent任务"""
        safe_print(f"\n{'='*80}")
        safe_print(f"🤖 启动Agent: {self.agent_name}")
        safe_print(f"📝 任务: {user_input[:100]}...")
        safe_print(f"{'='*80}\n")
        
        # 存储 task_input 供压缩器使用
        self.current_task_input = user_input
        
        # Agent入栈
        self.agent_id = self.hierarchy_manager.push_agent(self.agent_name, user_input)
        
        # 尝试加载已有的对话历史
        loaded_data = self.conversation_storage.load_actions(task_id, self.agent_id)
        start_turn = 0
        if loaded_data:
            self.action_history = loaded_data.get("action_history", [])
            self.action_history_fact = loaded_data.get("action_history_fact", [])
            self.pending_tools = loaded_data.get("pending_tools", [])
            self.latest_thinking = loaded_data.get("latest_thinking", "")
            self.first_thinking_done = loaded_data.get("first_thinking_done", False)
            self.tool_call_counter = loaded_data.get("tool_call_counter", 0)
            start_turn = loaded_data.get("current_turn", 0) + 1
            safe_print(f"📂 已加载对话历史，从第 {start_turn + 1} 轮继续")
            safe_print(f"   渲染历史: {len(self.action_history)}条, 完整轨迹: {len(self.action_history_fact)}条")
            
            # 检查是否已经完成（有final_output）
            for action in self.action_history_fact:
                if action.get("tool_name") == "final_output":
                    final_result = action.get("result", {})
                    safe_print(f"\n✅ 任务已完成，直接返回之前的final_output结果")
                    safe_print(f"   状态: {final_result.get('status')}")
                    return final_result
            
            # 恢复pending工具（如果有）
            if self.pending_tools:
                safe_print(f"🔄 发现{len(self.pending_tools)}个pending工具，恢复执行...")
                self._recover_pending_tools(task_id)
        try:
        # 首次thinking（初始规划）
            if start_turn == 0 and not self.first_thinking_done:
                safe_print(f"[{self.agent_name}] 开始行动前进行初始规划...")
                thinking_result = self._trigger_thinking(task_id, user_input, is_first=True)
                if thinking_result:
                    self.latest_thinking = thinking_result
                    self.first_thinking_done = True
                    self.hierarchy_manager.update_thinking(self.agent_id, thinking_result)
                    self._save_state(task_id, user_input, 0)
                    safe_print(f"[{self.agent_name}] 初始规划完成")
                    
                    # 发送 thinking 事件（完整内容）
                    emitter = get_event_emitter()
                    if emitter.enabled:
                        emitter.token(f"[{self.agent_name}] 初始规划: {thinking_result}")
        except Exception as e:
                import traceback
                import sys
                
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
                
                # 通过 event_emitter 发送错误（CLI 会显示）
                emitter = get_event_emitter()
                if emitter.enabled:
                    emitter.error(error_display)
                else:
                    # 非 JSONL 模式，直接打印
                    safe_print(error_display)
                
                # 不调用 pop_agent，保持 stack 不变（允许 resume）
                # 直接退出程序
                sys.exit(1)
        
        # 强制工具调用计数器
        max_tool_try = 0
        
        # 执行循环
        for turn in range(start_turn, self.max_turns):
            safe_print(f"\n--- 第 {turn + 1}/{self.max_turns} 轮执行 ---")
            
            try:
                # 每轮开始前保存状态
                self._save_state(task_id, user_input, turn)
                
                # 检查并压缩历史动作（如果超过限制）
                self._compress_action_history_if_needed()
                
                # 构建完整的系统提示词（包含通用prompts + 动态上下文）
                full_system_prompt = self.context_builder.build_context(
                    task_id,  # 添加task_id参数
                    self.agent_id,
                    self.agent_name,
                    user_input,
                    action_history=self.action_history  # 传入当前的动作历史
                )
                
                # 调用LLM（history永远只有一条）
                #history = [ChatMessage(role="user", content="请输出下一个动作")]
                history = [ChatMessage(role="user", content="<历史动作>是你之前已经执行的动作，不要重复<历史动作>内的动作！！请输出下一个动作")]
                
                safe_print(f"🤖 调用LLM: {self.model_type}")
                safe_print(f"   📝 System Prompt长度: {len(full_system_prompt)} 字符")
                safe_print(f"   🔧 可用工具: {len(self.available_tools)} 个")
                
                # 调用LLM（重试机制已在 llm_client 内部实现）
                llm_response = self.llm_client.chat(
                    history=history,
                    model=self.model_type,
                    system_prompt=full_system_prompt,
                    tool_list=self.available_tools,
                    tool_choice="required"  # 强制工具调用
                )
                
                if llm_response.status != "success":
                    error_result = {
                        "status": "error",
                        "output": f"LLM调用失败",
                        "error_information": llm_response.error_information
                    }
                    self.hierarchy_manager.pop_agent(self.agent_id, str(error_result))
                    return error_result
                
                safe_print(f"📥 LLM输出: {llm_response.output[:100]}...")
                safe_print(f"🔧 工具调用数量: {len(llm_response.tool_calls)}")
                
                # 检查是否有工具调用
                if not llm_response.tool_calls:
                    # 强制工具调用机制
                    if max_tool_try < 5:
                        max_tool_try += 1
                        safe_print(f"⚠️ LLM未调用工具，第{max_tool_try}/5次提醒")
                        # 下一轮会在XML上下文中看到之前的失败记录
                        # 可以选择在action_history中添加一个标记
                        self.action_history.append({
                            "tool_name": "_no_tool_call",
                            "arguments": {},
                            "result": {
                                "status": "error",
                                "output": f"第{max_tool_try}次：LLM未调用工具，请在下一轮中必须调用工具"
                            }
                        })
                        self._save_state(task_id, user_input, turn)
                        continue
                    else:
                        # 5次后仍不调用，触发thinking并报错
                        safe_print("❌ 5次提醒后仍未调用工具，触发thinking分析")
                        thinking_result = self._trigger_thinking(task_id, user_input, is_first=False)
                        
                        # 发送 thinking 事件（完整内容）
                        emitter = get_event_emitter()
                        if emitter.enabled:
                            emitter.warn(f"[{self.agent_name}] 强制thinking: {thinking_result if thinking_result else '分析失败'}")
                        
                        error_result = {
                            "status": "error",
                            "output": thinking_result if thinking_result else "多次未调用工具",
                            "error_information": "Agent拒绝调用工具"
                        }
                        self.hierarchy_manager.pop_agent(self.agent_id, str(error_result))
                        return error_result
                
                # 重置计数器（成功调用了工具）
                max_tool_try = 0
                
                # 执行所有工具调用
                for tool_call in llm_response.tool_calls:
                    safe_print(f"\n🔧 执行工具: {tool_call.name}")
                    safe_print(f"📋 参数: {tool_call.arguments}")
                    
                    # 发送工具调用事件（JSONL模式）
                    emitter = get_event_emitter()
                    if emitter.enabled:
                        import json
                        params_str = json.dumps(tool_call.arguments, ensure_ascii=False, indent=2)
                        emitter.token(f"调用工具: {tool_call.name}\n参数: {params_str}")
                    
                    # ✅ 在保存 pending 之前，为 level != 0 的工具添加 uuid
                    arguments_with_uuid = self._add_uuid_if_needed(tool_call.name, tool_call.arguments)
                    
                    # ✅ 先标记为pending（保存带 uuid 的参数）
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
                    
                    safe_print(f"✅ 结果: {tool_result.get('status', 'unknown')}")
                    
                    # 发送工具结果事件（JSONL模式）
                    emitter = get_event_emitter()
                    if emitter.enabled:
                        status = tool_result.get('status', 'unknown')
                        output_preview = tool_result.get('output', '')[:100]
                        emitter.token(f"工具 {tool_call.name} 完成: {status} - {output_preview}...")
                    
                    # 记录动作到历史（使用带 uuid 的参数）
                    action_record = {
                        "tool_name": tool_call.name,
                        "arguments": arguments_with_uuid,
                        "result": tool_result
                    }
                    
                    # 添加到完整轨迹（永不压缩）
                    self.action_history_fact.append(action_record)
                    
                    # 添加到渲染历史（会被压缩）
                    self.action_history.append(action_record)
                    
                    self.hierarchy_manager.add_action(self.agent_id, action_record)
                    
                    # 工具执行后保存状态
                    self._save_state(task_id, user_input, turn)
                    
                    # 增加工具调用计数
                    self.tool_call_counter += 1
                    
                    # 如果是final_output，返回结果
                    if tool_call.name == "final_output":
                        safe_print(f"\n{'='*80}")
                        safe_print(f"✅ Agent完成: {self.agent_name}")
                        safe_print(f"📊 状态: {tool_result.get('status', 'unknown')}")
                        safe_print(f"{'='*80}\n")
                        
                        self.hierarchy_manager.pop_agent(self.agent_id, tool_result.get("output", ""))
                        return tool_result
                
                # 检查是否该触发thinking（每N轮工具调用）
                if self.tool_call_counter % self.thinking_interval == 0:
                    safe_print(f"[{self.agent_name}] 第{self.tool_call_counter}轮工具调用，触发thinking分析")
                    thinking_result = self._trigger_thinking(task_id, user_input, is_first=False)
                    if thinking_result:
                        self.latest_thinking = thinking_result
                        self.hierarchy_manager.update_thinking(self.agent_id, thinking_result)
                        self._save_state(task_id, user_input, turn)
                        
                        # 发送 thinking 事件（完整内容）
                        emitter = get_event_emitter()
                        if emitter.enabled:
                            emitter.token(f"[{self.agent_name}] 进度分析: {thinking_result}")
                        safe_print(f"[{self.agent_name}] Thinking分析已更新")
                        self.action_history=[]
            
            except Exception as e:
                import traceback
                import sys
                
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
                
                # 通过 event_emitter 发送错误（CLI 会显示）
                emitter = get_event_emitter()
                if emitter.enabled:
                    emitter.error(error_display)
                else:
                    # 非 JSONL 模式，直接打印
                    safe_print(error_display)
                
                # 不调用 pop_agent，保持 stack 不变（允许 resume）
                # 直接退出程序
                sys.exit(1)
        
        # 超过最大轮次
        safe_print(f"\n⚠️ 达到最大轮次限制: {self.max_turns}")
        timeout_result = {
            "status": "error",
            "output": "执行超过最大轮次限制",
            "error_information": f"Max turns {self.max_turns} exceeded"
        }
        self.hierarchy_manager.pop_agent(self.agent_id, str(timeout_result))
        return timeout_result
    
    def _add_uuid_if_needed(self, tool_name: str, arguments: Dict) -> Dict:
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
                safe_print(f"   🔖 为 level {tool_level} 工具添加 uuid 后缀")
                return new_arguments
            
            # 其他情况返回原参数
            return arguments
        
        except Exception as e:
            safe_print(f"⚠️ 添加 uuid 时出错: {e}")
            return arguments
    
    def _trigger_thinking(self, task_id: str, task_input: str, is_first: bool = False) -> str:
        """
        触发Thinking Agent进行分析
        
        Args:
            task_id: 任务ID
            task_input: 任务输入
            is_first: 是否是首次thinking
            
        Returns:
            分析结果
        """
        try:
            from services.thinking_agent import ThinkingAgent
            
            thinking_agent = ThinkingAgent()
            
            # 构建完整的系统提示词
            full_system_prompt = self.context_builder.build_context(
                task_id,
                self.agent_id,
                self.agent_name,
                task_input,
                action_history=self.action_history
            )
            
            if is_first:
                # 首次thinking - 初始规划
                return thinking_agent.analyze_first_thinking(
                    task_description=task_input,
                    agent_system_prompt=full_system_prompt,  # 传入完整的prompt
                    available_tools=self.available_tools,
                    tools_config=self.config_loader.all_tools  # 传递工具配置
                )
            else:
                return thinking_agent.analyze_first_thinking(
                    task_description=task_input,
                    agent_system_prompt=full_system_prompt,  # 传入完整的prompt
                    available_tools=self.available_tools,
                    tools_config=self.config_loader.all_tools  # 传递工具配置
                )
                # 进度分析（full_system_prompt已包含<历史动作>）
                # return thinking_agent.analyze_progress(
                #     task_description=task_input,
                #     agent_system_prompt=full_system_prompt,  # 已包含完整上下文
                #     tool_call_counter=self.tool_call_counter
                # )
        except Exception as e:
            raise Exception(str(e))
            # safe_print(f"⚠️ Thinking触发失败: {e}")
            # import traceback
            # traceback.print_exc()
            # return ""
    
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
            compressed = self.action_compressor.compress_if_needed(
                self.action_history,
                self.llm_client.max_context_window,
                thinking=self.latest_thinking,
                task_input=getattr(self, 'current_task_input', '')
            )
            
            # 如果发生了压缩，替换
            if len(compressed) < len(self.action_history):
                safe_print(f"✅ 历史动作已压缩: {len(self.action_history)}条 → {len(compressed)}条")
                self.action_history = compressed
        
        except Exception as e:
            safe_print(f"⚠️ 压缩失败: {e}")
            import traceback
            traceback.print_exc()
    
    def _recover_pending_tools(self, task_id: str):
        """恢复pending状态的工具调用"""
        for pending_tool in self.pending_tools[:]:  # 复制列表
            try:
                safe_print(f"   🔄 恢复执行: {pending_tool['name']}")
                safe_print(f"   📋 参数: {pending_tool['arguments']}")
                
                # 发送恢复事件（JSONL模式）
                emitter = get_event_emitter()
                if emitter.enabled:
                    import json
                    params_str = json.dumps(pending_tool["arguments"], ensure_ascii=False, indent=2)
                    emitter.token(f"恢复工具: {pending_tool['name']}\n参数: {params_str}")
                
                # 重新执行工具
                tool_result = self.tool_executor.execute(
                    pending_tool["name"],
                    pending_tool["arguments"],
                    task_id
                )
                
                # 记录结果
                action_record = {
                    "tool_name": pending_tool["name"],
                    "arguments": pending_tool["arguments"],
                    "result": tool_result
                }
                
                self.action_history_fact.append(action_record)
                self.action_history.append(action_record)
                
                # 从pending移除
                self.pending_tools.remove(pending_tool)
                
                safe_print(f"   ✅ 恢复完成: {pending_tool['name']}")
                
                # 如果是final_output，直接返回
                if pending_tool["name"] == "final_output":
                    return tool_result
            
            except Exception as e:
                safe_print(f"   ❌ 恢复失败: {pending_tool['name']} - {e}")
        
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
        # 构建完整的系统提示词（包含XML上下文）
        full_system_prompt = self.context_builder.build_context(
            task_id,  # 添加task_id参数
            self.agent_id,
            self.agent_name,
            user_input,
            action_history=self.action_history  # 传入动作历史
        )
        
        # 保存状态（新格式）
        self.conversation_storage.save_actions(
            task_id=task_id,
            agent_id=self.agent_id,
            agent_name=self.agent_name,
            task_input=user_input,
            action_history=self.action_history,  # 渲染用（会压缩）
            action_history_fact=self.action_history_fact,  # 完整轨迹（不压缩）
            pending_tools=self.pending_tools,  # 待执行的工具
            current_turn=current_turn,
            latest_thinking=self.latest_thinking,
            first_thinking_done=self.first_thinking_done,
            tool_call_counter=self.tool_call_counter,
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
