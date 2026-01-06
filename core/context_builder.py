#!/usr/bin/env python3
from utils.windows_compat import safe_print
# -*- coding: utf-8 -*-
"""
上下文构造器 - 构建新的XML结构化上下文
"""

from typing import Dict, List, Optional
import json


class ContextBuilder:
    """构建XML结构化的Agent上下文（完整）"""
    
    def __init__(self, hierarchy_manager, agent_config: Dict, config_loader, llm_client=None, max_context_window=100000):
        """
        初始化上下文构造器
        
        Args:
            hierarchy_manager: 层级管理器实例
            agent_config: Agent配置（包含prompts）
            config_loader: 配置加载器（用于读取general_prompts）
            llm_client: LLM客户端（用于压缩总结）
            max_context_window: 最大上下文窗口
        """
        self.hierarchy_manager = hierarchy_manager
        self.agent_config = agent_config
        self.config_loader = config_loader
        self.current_action_history = []  # 当前Agent的动作历史（从外部传入）
        self.llm_client = llm_client
        self.max_context_window = max_context_window
        
        # 初始化tiktoken
        try:
            import tiktoken
            self.encoding = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            self.encoding = None
    
    def build_context(self, task_id: str, agent_id: str, agent_name: str, task_input: str, 
                     action_history: List[Dict] = None) -> str:
        """
        构建完整的系统提示词（包含通用部分+动态上下文）
        
        Args:
            task_id: 任务ID（用于读取文件）
            agent_id: 当前Agent ID
            agent_name: 当前Agent名称
            task_input: 当前Agent的任务输入
            action_history: 当前Agent的动作历史（可选，优先使用）
            
        Returns:
            完整的XML结构化上下文字符串（包含通用提示词）
        """
        context_data = self.hierarchy_manager.get_context()
        current = context_data.get("current", {})
        history = context_data.get("history", [])
        
        # 使用传入的action_history
        if action_history is not None:
            self.current_action_history = action_history
        
        # 1️⃣ 读取通用系统提示词（general_prompts.yaml，包含<智能体经验>）
        general_system_prompt = self._load_general_system_prompt(agent_name)
        
        # 2️⃣ 构建各个动态部分
        user_latest_input = self._build_user_latest_input(current)
        user_agent_history = self._build_user_agent_history(task_id, current)
        structured_call_info = self._build_structured_call_info(current, agent_id)
        current_thinking = self._build_current_thinking(task_id, agent_id, current)
        action_history_xml = self._build_action_history(task_id, agent_id)
        
        # 3️⃣ 组装完整上下文（通用部分在最前面）
        full_context = f"""{general_system_prompt}

<用户最新输入>
{user_latest_input}
</用户最新输入>

<用户-智能体历史交互>
{user_agent_history}
</用户-智能体历史交互>

<当前运行智能体名称>
{agent_name}
</当前运行智能体名称>

<结构化调用信息>
{structured_call_info}
</结构化调用信息>

<当前智能体任务>
{task_input}
</当前智能体任务>

<当前进度思考>
{current_thinking}
</当前进度思考>

<历史动作>
{action_history_xml}
</历史动作>
"""
        
        return full_context
    
    def _load_general_system_prompt(self, agent_name: str) -> str:
        """
        读取并格式化通用系统提示词（包含<智能体经验>）
        
        Args:
            agent_name: Agent名称
            
        Returns:
            格式化后的通用系统提示词（XML格式）
        """
        # 读取general_prompts.yaml
        import yaml
        from pathlib import Path
        
        agent_system_name = self.config_loader.agent_system_name
        prompts_file = Path(self.config_loader.config_root) / "agent_library" / agent_system_name / "general_prompts.yaml"
        
        if not prompts_file.exists():
            return ""
        
        with open(prompts_file, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            system_prompt_xml = data.get("system_prompt_xml", "")
        
        # 格式化变量
        prompts = self.agent_config.get("prompts", {})
        agent_responsibility = prompts.get("agent_responsibility", "完成分配的任务")
        agent_workflow = prompts.get("agent_workflow", "(无特定流程)")
        
        return system_prompt_xml.format(
            agent_name=agent_name,
            agent_responsibility=agent_responsibility,
            agent_workflow=agent_workflow
        )
    
    def _build_user_latest_input(self, current: Dict) -> str:
        """构建用户最新输入部分"""
        instructions = current.get("instructions", [])
        if not instructions:
            return "(无)"
        
        # 返回所有指令（按时间顺序）
        result = []
        for i, instr in enumerate(instructions, 1):
            instruction_text = instr.get("instruction", "")
            start_time = instr.get("start_time", "")
            result.append(f"{i}. {instruction_text} (开始时间: {start_time})")
        
        return "\n".join(result)
    
    def _build_user_agent_history(self, task_id: str, current: Dict = None) -> str:
        """
        检查并压缩用户-智能体历史交互（只在启动时执行一次）
        
        Args:
            task_id: 任务ID
            current: 当前任务数据（包含用户输入）
        
        Returns:
            压缩后的历史交互文本（已包含<用户-智能体历史交互>标签）
        """
        context = self.hierarchy_manager.get_context()
        if current is None:
            current = context.get("current", {})
        history = context.get("history", [])
        
        if not history:
            return "(无历史交互)"
        

        
        compressed_history = current.get("_compressed_user_agent_history")
        if compressed_history:
            safe_print("使用已有的压缩历史交互")
            return compressed_history
        
        safe_print("未到历史交互压缩阈值")
        if len(str(history)) < 5000:
            return str(history)
        
        # 提取当前任务的用户输入
        current_task = ""
        instructions = current.get("instructions", [])
        if instructions:
            # 将所有用户输入拼接起来
            user_inputs = [instr.get("instruction", "") for instr in instructions]
            current_task = "\n".join(user_inputs)
        
        safe_print("首次压缩历史交互...")
        compressed_result = self._compress_user_agent_history_with_llm(history, task_id, current_task)
        
        context["current"]["_compressed_user_agent_history"] = compressed_result
        self.hierarchy_manager._save_context(context)
        
        return compressed_result
    
    def _compress_user_agent_history_with_llm(self, history: List[Dict], task_id: str, current_task: str = "") -> str:
        """
        使用LLM压缩历史交互（直接返回LLM输出，不解析）
        
        Args:
            history: 历史任务列表
            task_id: 任务ID
            current_task: 当前任务的用户输入内容
            
        Returns:
            压缩后的文本（LLM原始输出）
        """
        full_history_data = []
        
        for i, hist_item in enumerate(history, 1):
            instructions = hist_item.get("instructions", [])
            agents_status = hist_item.get("agents_status", {})
            start_time = hist_item.get("start_time", "")
            completion_time = hist_item.get("completion_time", "")
            
            user_inputs = []
            for instr in instructions:
                user_inputs.append(instr.get("instruction", ""))
            
            agent_summaries = []
            for agent_id, agent_info in agents_status.items():
                if agent_info.get("level") == 0 and agent_info.get("agent_name") != "judge_agent":
                    agent_name = agent_info.get("agent_name", "")
                    status = agent_info.get("status", "")
                    
                    final_output = agent_info.get("final_output", "")
                    thinking = agent_info.get("latest_thinking", "")
                    
                    agent_summaries.append({
                        "agent_name": agent_name,
                        "status": status,
                        "final_output": final_output,
                        "thinking": thinking
                    })
            
            full_history_data.append({
                "task_id": i,
                "time_range": f"{start_time} → {completion_time}",
                "user_inputs": user_inputs,
                "agents": agent_summaries
            })
        
        # 构建prompt，根据是否有当前任务来调整重点
        if current_task:
            task_context = f"""
当前任务：
{current_task}

请特别关注与当前任务相关的历史信息，重点介绍相关的历史任务、生成的文件和中间结果。"""
        else:
            task_context = ""
        
        prompt = f"""请分析以下历史交互数据，提取关键信息并总结。{task_context}

历史任务数据：
{json.dumps(full_history_data, ensure_ascii=False, indent=2)}

请总结以下内容：
1. 文件空间总结：描述当前工作空间文件结构，结果文件对应的task，和简要介绍，同时列出一些重点的中间材料和文件。基于历史的final_output和thinking来推断
2. 历史交互概览：简要描述每次任务的用户输入和完成情况
{"3. 相关性分析：重点说明哪些历史任务、文件和结果与当前任务相关，以及如何利用这些信息" if current_task else ""}

要求：
- 每个描述要简洁明了
{"- 优先详细介绍与当前任务相关的历史内容" if current_task else ""}
- 总字符数控制在3000字以内
- 优先使用用户的输入习惯语言进行输出。
- 直接输出总结内容文本，不需要任何标记，不要使用markdown格式"""

        from services.llm_client import ChatMessage
        
        history_messages = [ChatMessage(role="user", content=prompt)]
        
        response = self.llm_client.chat(
            history=history_messages,
            model=self.llm_client.models[0],
            system_prompt="你是一个专业的内容总结助手。请简洁明了地总结历史交互信息。",
            tool_list=[],  # 空列表表示不使用工具
            tool_choice="none"  # 明确表示不调用工具（总结任务）
        )
        
        if response.status != "success":
            raise Exception(f"LLM压缩失败: {response.output}")

        output_text = response.output
        
        safe_print(f"✅ 历史交互压缩成功，长度: {len(output_text)} 字符")
        
        return output_text
    
    def _build_structured_call_info(self, current: Dict, current_agent_id: str) -> str:
        """构建结构化调用信息（JSON格式，更清晰）"""
        hierarchy = current.get("hierarchy", {})
        agents_status = current.get("agents_status", {})
        
        if not agents_status:
            return "(无调用关系)"
        
        # 找到根Agent（Level 0）
        root_agents = [
            aid for aid, info in hierarchy.items()
            if info.get("parent") is None
        ]
        
        if not root_agents:
            return "(无调用关系)"
        
        # 构建JSON结构（添加已访问集合防止循环）
        call_tree = []
        visited = set()  # 防止循环引用
        for root_id in root_agents:
            tree_node = self._build_agent_tree_json(
                root_id, hierarchy, agents_status, current_agent_id, visited
            )
            if tree_node:
                call_tree.append(tree_node)
        
        # 转换为易读的JSON字符串
        return json.dumps(call_tree, indent=2, ensure_ascii=False)
    
    def _build_agent_tree_json(
        self,
        agent_id: str,
        hierarchy: Dict,
        agents_status: Dict,
        current_agent_id: str,
        visited: set = None
    ) -> Dict:
        """递归构建Agent树的JSON结构（带循环检测）"""
        # 初始化visited集合
        if visited is None:
            visited = set()
        
        # 检查是否已访问（防止循环）
        if agent_id in visited:
            return None
        
        visited.add(agent_id)
        
        if agent_id not in agents_status:
            return None
        
        agent_info = agents_status[agent_id]
        agent_name = agent_info.get("agent_name", "")
        
        # 完全跳过judge_agent（不显示也不处理）
        if agent_name == "judge_agent":
            return None
        
        level = agent_info.get("level", 0)
        status = agent_info.get("status", "")
        is_current = (agent_id == current_agent_id)
        
        # 构建节点数据
        node = {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "level": level,
            "status": status,
            "is_current": is_current
        }
        
        # 添加thinking或final_output
        if status == "completed":
            final_output = agent_info.get("final_output", "")
            if final_output:
                # 限制长度
                node["final_output"] = final_output[:500] + "..." if len(final_output) > 500 else final_output
        else:
            thinking = agent_info.get("latest_thinking", "")
            if thinking:
                # 限制长度
                node["thinking"] = thinking[:500] + "..." if len(thinking) > 500 else thinking
        
        # 递归处理子节点
        children = hierarchy.get(agent_id, {}).get("children", [])
        if children:
            child_nodes = []
            for child_id in children:
                child_node = self._build_agent_tree_json(
                    child_id, hierarchy, agents_status, current_agent_id, visited
                )
                if child_node:
                    if isinstance(child_node, list):
                        child_nodes.extend(child_node)
                    else:
                        child_nodes.append(child_node)
            
            if child_nodes:
                node["children"] = child_nodes
        
        return node
    
    def _format_agent_tree(
        self, 
        agent_id: str, 
        hierarchy: Dict, 
        agents_status: Dict, 
        indent: int,
        current_agent_id: str
    ) -> str:
        """递归格式化Agent树（清晰展示层级和状态）"""
        if agent_id not in agents_status:
            return ""
        
        agent_info = agents_status[agent_id]
        agent_name = agent_info.get("agent_name", "")
        
        # 跳过judge_agent的显示（避免干扰）
        if agent_name == "judge_agent":
            # 但仍需递归处理它的子节点
            children = hierarchy.get(agent_id, {}).get("children", [])
            child_lines = []
            for child_id in children:
                child_tree = self._format_agent_tree(
                    child_id, hierarchy, agents_status, indent, current_agent_id
                )
                if child_tree:
                    child_lines.append(child_tree)
            return "\n".join(child_lines)
        
        level = agent_info.get("level", 0)
        status = agent_info.get("status", "")
        
        # 当前Agent标记
        current_marker = " [当前Agent]" if agent_id == current_agent_id else ""
        
        # 状态图标
        status_icon = "✅" if status == "completed" else "⏳"
        
        # 缩进
        indent_str = "  " * indent
        
        # 构建输出
        lines = []
        
        # 第一行：Agent ID和名称
        lines.append(f"{indent_str}{status_icon} {agent_id} ({agent_name}, Level {level}){current_marker}")
        
        # 第二行：状态信息
        if status == "completed":
            # 已完成：显示final_output
            final_output = agent_info.get("final_output", "")
            if final_output:
                # 限制输出长度
                output_preview = final_output[:300] + "..." if len(final_output) > 300 else final_output
                lines.append(f"{indent_str}  📊 Final Output: {output_preview}")
        else:
            # 运行中：显示latest_thinking
            thinking = agent_info.get("latest_thinking", "")
            if thinking:
                # 限制thinking长度
                thinking_preview = thinking[:300] + "..." if len(thinking) > 300 else thinking
                lines.append(f"{indent_str}  💭 Thinking: {thinking_preview}")
        
        # 递归处理子Agent
        children = hierarchy.get(agent_id, {}).get("children", [])
        for child_id in children:
            child_tree = self._format_agent_tree(
                child_id, hierarchy, agents_status, indent + 1, current_agent_id
            )
            if child_tree:  # 只添加非空的子树
                lines.append(child_tree)
        
        return "\n".join(lines)
    
    def _build_current_thinking(self, task_id: str, agent_id: str, current: Dict) -> str:
        """构建当前进度思考（从文件读取最新的thinking）"""
        # 从_actions.json文件读取（使用正确的路径）
        from pathlib import Path
        import json
        import hashlib
        import os
        
        # 使用与ConversationStorage相同的路径生成逻辑
        conversations_dir = Path.home() / "mla_v3" / "conversations"
        task_hash = hashlib.md5(task_id.encode()).hexdigest()[:8]
        task_folder = Path(task_id).name if (os.sep in task_id or '/' in task_id or '\\' in task_id) else task_id
        task_name = f"{task_hash}_{task_folder}"
        filepath = conversations_dir / f"{task_name}_{agent_id}_actions.json"
        
        try:
            if filepath.exists():
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    thinking = data.get("latest_thinking", "")
                    if thinking:
                        return thinking
        except Exception as e:
            safe_print(f"⚠️ 读取thinking失败: {e}")
        
        # 备用：从share_context读取
        agents_status = current.get("agents_status", {})
        if agent_id in agents_status:
            thinking = agents_status[agent_id].get("latest_thinking", "")
            if thinking:
                return thinking
        
        return "(无)"
    
    def _build_action_history(self, task_id: str, agent_id: str) -> str:
        """构建历史动作记录（从文件读取，XML格式）"""
        # 优先使用传入的action_history
        action_history = self.current_action_history
        
        # 如果没有传入，从文件读取
        if not action_history:
            from pathlib import Path
            import json
            import hashlib
            import os
            
            # 使用与ConversationStorage相同的路径生成逻辑
            conversations_dir = Path.home() / "mla_v3" / "conversations"
            task_hash = hashlib.md5(task_id.encode()).hexdigest()[:8]
            task_folder = Path(task_id).name if (os.sep in task_id or '/' in task_id or '\\' in task_id) else task_id
            task_name = f"{task_hash}_{task_folder}"
            filepath = conversations_dir / f"{task_name}_{agent_id}_actions.json"
            
            try:
                if filepath.exists():
                    with open(filepath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        action_history = data.get("action_history", [])
            except Exception as e:
                safe_print(f"⚠️ 读取action_history失败: {e}")
        
        if not action_history:
            return "(无历史动作)"
        
        # 构建XML格式的动作历史
        actions_xml = []
        for action in action_history:
            tool_name = action.get("tool_name", "")
            
            # 检查是否是历史总结
            if tool_name == "_historical_summary":
                # 渲染为<已压缩信息>
                summary_text = action.get("result", {}).get("output", "")
                actions_xml.append(f"<已压缩信息>\n{summary_text}\n</已压缩信息>")
                continue
            
            # 普通action
            arguments = action.get("arguments", {})
            result = action.get("result", {})
            
            # 构建单个动作的XML
            # action_xml = f"<action>\n"
            # action_xml += f"  <tool_name>{tool_name}</tool_name>\n"
            action_xml = f"action:\n"
            action_xml += f"  tool_name:{tool_name}\n"            
            # 添加参数
            for param_name, param_value in arguments.items():
                # 转义XML特殊字符
                param_value_str = str(param_value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                #action_xml += f"  <tool_use:{param_name}>{param_value_str}</tool_use:{param_name}>\n"
                action_xml += f"  {param_name}:{param_value_str}\n"
            
            # 添加结果（JSON格式）
            try:
                result_json = json.dumps(result, ensure_ascii=False, indent=2)
                action_xml += f"  <result>\n{result_json}\n  </result>\n"
            except:
                action_xml += f"  <result>{str(result)}</result>\n"
            
            # action_xml += "</action>"
            actions_xml.append(action_xml)
        
        return "\n\n".join(actions_xml)


if __name__ == "__main__":
    # 测试上下文构造器
    from hierarchy_manager import HierarchyManager
    
    manager = HierarchyManager("test_task")
    manager.start_new_instruction("测试任务：生成一个文件")
    
    agent_id = manager.push_agent("test_agent", "生成hello.py文件")
    manager.update_thinking(agent_id, "我需要先创建文件，然后写入内容")
    manager.add_action(agent_id, {
        "tool_name": "file_write",
        "arguments": {"path": "hello.py", "content": "safe_print('hello')"},
        "result": {"status": "success", "output": "文件已创建"}
    })
    
    builder = ContextBuilder(manager)
    context = builder.build_context(agent_id, "test_agent", "生成hello.py文件")
    
    safe_print("=" * 80)
    safe_print("生成的上下文:")
    safe_print("=" * 80)
    safe_print(context)

