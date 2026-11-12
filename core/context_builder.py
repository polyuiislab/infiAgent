#!/usr/bin/env python3
from utils.windows_compat import safe_print
# -*- coding: utf-8 -*-
"""
上下文构造器 - 构建新的XML结构化上下文
"""

from typing import Dict, List, Optional
import json
import os
from pathlib import Path

from utils.context_hooks import apply_context_hooks
from utils.experience_store import (
    global_experience_path,
    parse_experience_entries,
    render_entries_block,
    task_experience_path,
)
from utils.token_budget import count_tokens_text
from utils.user_paths import get_context_settings, get_user_conversations_dir, get_user_skills_library_root


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
        
        # 初始化 Skill 加载器
        try:
            from utils.skill_loader import get_skill_loader
            self.skill_loader = get_skill_loader()
        except Exception:
            self.skill_loader = None
        
        self.encoding = None

    def _resolve_compressor_model(self) -> str:
        preferred = str(self.agent_config.get("compressor_model") or "").strip() or None
        return self.llm_client.resolve_model("compressor", preferred)

    def _resolve_compressor_tool_choice(self, model: str) -> str:
        return self.llm_client.resolve_tool_choice("compressor", model)

    def _build_framework_runtime_hint(self) -> str:
        return """<框架级工具规则>
1. 大多数文件类工具参数（如 path、save_path、bib_path、working_dir）默认都相对于当前 task 根目录解析；除 task_id 这类任务管理参数外，不要给文件类工具传绝对路径。
2. 隐藏目录是允许访问的，例如 .skills/demo/SKILL.md、.chatbi/notes.md、.cache/tmp.txt。
3. execute_command 默认工作目录就是当前 task 根目录；如果要访问 skill 内容，通常可以直接使用 .skills/<skill_name>/... 路径。
4. load_skill 会把 skill 部署到当前 task 的 .skills/<skill_name>/ 目录，并把对应 SKILL.md 注入当前 agent 的上下文；offload_skill 只卸载上下文中的 skill 内容，不会删除本地文件。
5. 维护 reference.bib 时优先使用 reference_add / reference_delete；只有在明确需要时才直接编辑 bib 文件。
6. 文件路径示例：
   - file_read: path=['main.tex']
   - file_read: path=['.skills/pptx/SKILL.md']
   - file_write: path='notes/result.md'
   - dir_list: path='.skills', recursive=true
</框架级工具规则>"""

    def _build_task_system_add(self, task_id: str, agent_name: str) -> str:
        try:
            task_dir = Path(task_id)
            candidate_paths = []
            normalized_agent_name = str(agent_name or "").strip()
            agent_system = str(getattr(self.config_loader, "agent_system_name", "") or "").strip()
            if agent_system:
                candidate_paths.append(task_dir / f"system-add.{agent_system}.md")
            if normalized_agent_name:
                candidate_paths.append(task_dir / f"system-add.{normalized_agent_name}.md")
            candidate_paths.append(task_dir / "system-add.md")

            for system_add_path in candidate_paths:
                if not system_add_path.exists():
                    continue
                content = system_add_path.read_text(encoding="utf-8").strip()
                if not content:
                    continue
                return f"<任务级补充系统提示>\n{content}\n</任务级补充系统提示>"
        except Exception:
            return ""
        return ""

    def _build_experience_blocks(self, task_id: str, agent_name: str) -> str:
        try:
            blocks = []
            task_path = task_experience_path(task_id, agent_name)
            # 只注入 status=active 的条目；inactive 保留在文件里但不进上下文
            task_entries = [e for e in parse_experience_entries(task_path) if e.status == "active"]
            if task_entries:
                blocks.append(f"<任务经验>\n{render_entries_block(task_entries)}\n</任务经验>")

            agent_system = str(getattr(self.config_loader, "agent_system_name", "") or "").strip() or "OpenCowork"
            global_path = global_experience_path(agent_system, agent_name)
            global_entries = [e for e in parse_experience_entries(global_path) if e.status == "active"]
            if global_entries:
                blocks.append(f"<全局经验>\n{render_entries_block(global_entries)}\n</全局经验>")

            return "\n\n".join(blocks)
        except Exception:
            return ""

    def _count_tokens(self, text: str) -> int:
        return count_tokens_text(str(text or ""))
    
    def build_context(self, task_id: str, agent_id: str, agent_name: str, task_input: str, 
                     action_history: List[Dict] = None,
                     include_action_history: bool = True) -> str:
        """
        构建完整的系统提示词（包含通用部分+动态上下文）
        
        Args:
            task_id: 任务ID（用于读取文件）
            agent_id: 当前Agent ID
            agent_name: 当前Agent名称
            task_input: 当前Agent的任务输入
            action_history: 当前Agent的动作历史（可选，优先使用）
            include_action_history: 是否在系统提示词中包含历史动作（默认True）
                                   主LLM调用时传False（历史动作改由messages承载），
                                   thinking/compression调用时传True（保持原有XML格式）
            
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
        framework_runtime_hint = self._build_framework_runtime_hint()
        task_system_add = self._build_task_system_add(task_id, agent_name)
        experience_blocks = self._build_experience_blocks(task_id, agent_name)
        
        # 2️⃣ 构建各个动态部分
        user_latest_input = self._build_user_latest_input(current)
        user_agent_history = self._build_user_agent_history(task_id, current)
        visible_history, visible_history_count, total_history_count = self._select_user_history_entries(history)
        history_search_hint = self._build_task_history_search_hint(visible_history_count, total_history_count)
        structured_call_info = self._build_structured_call_info(current, agent_id, task_id)
        current_thinking = self._build_current_thinking(task_id, agent_id, current)
        from utils.execution_scope import CURRENT
        scope = CURRENT.get()
        if scope and scope.context_mode == 'fresh':
            user_latest_input = task_input
            user_agent_history = ''
            history_search_hint = ''
            structured_call_info = ''
            task_system_add = ''
            experience_blocks = ''
        elif scope and scope.context_mode == 'inherit' and scope.context_seed:
            task_system_add += '\n<inherited_actions>\n' + json.dumps(scope.context_seed, ensure_ascii=False) + '\n</inherited_actions>'
        from utils.workspaces import files_root
        workspace_abs_path = str(files_root(task_id))
        skills_root_abs_path = str(get_user_skills_library_root())
        runtime_meta = context_data.get("runtime", {})
        visible_skills = None
        if isinstance(runtime_meta, dict):
            raw_visible = runtime_meta.get("visible_skills")
            if isinstance(raw_visible, list):
                visible_skills = [str(item).strip() for item in raw_visible if str(item).strip()]
        if visible_skills is None:
            env_json = os.environ.get("MLA_VISIBLE_SKILLS_JSON", "").strip()
            if env_json:
                try:
                    parsed = json.loads(env_json)
                    if isinstance(parsed, list):
                        visible_skills = [str(item).strip() for item in parsed if str(item).strip()]
                except Exception:
                    visible_skills = None
        
        # 2.5️⃣ 构建可用 skills 列表（如果有）
        available_skills_xml = ""
        if self.skill_loader:
            try:
                available_skills_xml = self.skill_loader.build_available_skills_xml(visible_skill_names=visible_skills)
            except Exception:
                pass

        loaded_skills_xml = self._build_loaded_skills_xml(agent_id)
        
        # 3️⃣ 组装完整上下文（通用部分在最前面）
        full_context = f"""{general_system_prompt}

{framework_runtime_hint}

{task_system_add}
{experience_blocks}
<用户最新输入>
{user_latest_input}
</用户最新输入>

<用户-智能体历史交互>
{user_agent_history}
</用户-智能体历史交互>

<历史任务检索提示>
{history_search_hint}
</历史任务检索提示>

<当前运行智能体名称>
{agent_name}
</当前运行智能体名称>

<当前工作空间绝对路径>
{workspace_abs_path}
</当前工作空间绝对路径>

<当前可见Skills目录绝对路径>
{skills_root_abs_path}
</当前可见Skills目录绝对路径>

<结构化调用信息>
{structured_call_info}
</结构化调用信息>

<当前智能体任务>
{task_input}
</当前智能体任务>

<当前进度思考>
{current_thinking}
</当前进度思考>
"""
        
        # 3.5️⃣ 可选：包含可用 skills（仅当有 skills 时）
        if available_skills_xml:
            full_context += f"\n{available_skills_xml}\n"

        if loaded_skills_xml:
            full_context += f"\n{loaded_skills_xml}\n"
        
        # 4️⃣ 可选：包含历史动作（thinking/compression时包含，主LLM调用时不包含）
        if include_action_history:
            action_history_xml = self._build_action_history(task_id, agent_id)
            full_context += f"""
<历史动作>
{action_history_xml}
</历史动作>
"""
        
        return apply_context_hooks(
            stage="after_build",
            task_id=task_id,
            agent_id=agent_id,
            agent_name=agent_name,
            task_input=task_input,
            context_data=context_data,
            context_text=full_context,
        )

    def _select_user_history_entries(self, history: List[Dict]) -> tuple[List[Dict], int, int]:
        if not isinstance(history, list) or not history:
            return [], 0, 0
        settings = get_context_settings()
        recent_items = settings.get("user_history_recent_items", 0)
        if recent_items and recent_items > 0:
            selected = history[-recent_items:]
        else:
            selected = history
        return selected, len(selected), len(history)

    def _build_task_history_search_hint(self, visible_count: int, total_count: int) -> str:
        if total_count <= 0:
            return "当前没有历史交互记录。"
        return (
            f"你现在能看到最近{visible_count}条历史交互信息，总交互信息有{total_count}条。"
            "如果当前提供的历史信息不足以涵盖本次任务的相关历史，请使用 task_history_search 工具进行检索；"
            "它只支持两种简单查询：round_range（如 '1-3'、'4'、'-2'）和 keyword（中文、文件名、路径或关键短语）。"
        )

    def _build_loaded_skills_xml(self, agent_id: str) -> str:
        """构建当前已加载 skill 的注入内容。"""
        try:
            loaded_skills = self.hierarchy_manager.get_loaded_skills(agent_id)
        except Exception:
            loaded_skills = []

        if not loaded_skills:
            return ""

        parts = ["<已加载技能内容>"]
        for skill in loaded_skills:
            parts.append(f'  <skill name="{skill.get("name", "")}">')
            if skill.get("abs_path"):
                parts.append(f'    <absolute_path>{skill["abs_path"]}</absolute_path>')
            if skill.get("workspace_path"):
                parts.append(f'    <workspace_path>{skill["workspace_path"]}</workspace_path>')
            parts.append("    <skill_md>")
            parts.append(str(skill.get("md_text", "")).strip())
            parts.append("    </skill_md>")
            parts.append("  </skill>")
        parts.append("</已加载技能内容>")
        return "\n".join(parts)
    
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
        
        # Use the resolved agent system directory from ConfigLoader.
        # This supports both bundled systems and user-imported systems
        # under ~/mla_v3/agent_library/<system>/.
        prompts_file = Path(self.config_loader.agent_config_dir) / "general_prompts.yaml"
        
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
            source = str(instr.get("source") or "user").strip()
            result.append(f"{i}. [{source}] {instruction_text} (开始时间: {start_time})")
        
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
        try:
            from utils.task_history_index import sync_task_history_from_context
            sync_task_history_from_context(task_id, context)
        except Exception:
            pass

        selected_history, visible_count, total_count = self._select_user_history_entries(history)
        compressed_history = current.get("_compressed_user_agent_history")
        compressed_meta = current.get("_compressed_user_agent_history_meta") or {}
        settings = get_context_settings()
        threshold_tokens = settings.get("user_history_compress_threshold_tokens", 1500)
        recent_items = settings.get("user_history_recent_items", 0)
        if (
            compressed_history
            and compressed_meta.get("recent_items") == recent_items
            and compressed_meta.get("threshold_tokens") == threshold_tokens
            and compressed_meta.get("visible_count") == visible_count
            and compressed_meta.get("total_count") == total_count
        ):
            safe_print("使用已有的压缩历史交互")
            return compressed_history

        history_tokens = self._count_tokens(json.dumps(selected_history, ensure_ascii=False))
        safe_print("未到历史交互压缩阈值")
        if history_tokens < threshold_tokens:
            return str(selected_history)
        
        # 提取当前任务的用户输入
        current_task = ""
        instructions = current.get("instructions", [])
        if instructions:
            # 将所有用户输入拼接起来
            user_inputs = [instr.get("instruction", "") for instr in instructions]
            current_task = "\n".join(user_inputs)
        
        safe_print("首次压缩历史交互...")
        compressed_result = self._compress_user_agent_history_with_llm(selected_history, task_id, current_task)
        
        compressed_meta = {
            "recent_items": recent_items,
            "threshold_tokens": threshold_tokens,
            "visible_count": visible_count,
            "total_count": total_count,
        }

        def _store_user_history_cache(latest_context):
            latest_current = latest_context.setdefault("current", {})
            latest_current["_compressed_user_agent_history"] = compressed_result
            latest_current["_compressed_user_agent_history_meta"] = compressed_meta

        self.hierarchy_manager.update_context(_store_user_history_cache)
        
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
- 强调当前任务应该复用的历史工作，除非用户明确指示重新开始。
{"- 优先详细介绍与当前任务相关的历史内容" if current_task else ""}
- 总字符数控制在3000字以内
- 优先使用用户的输入习惯语言进行输出。
- 直接输出总结内容文本，不需要任何标记，不要使用markdown格式"""

        from services.llm_client import ChatMessage
        
        history_messages = [ChatMessage(role="user", content=prompt)]
        
        compressor_model = self._resolve_compressor_model()
        response = self.llm_client.chat(
            history=history_messages,
            model=compressor_model,  # 使用压缩专用模型
            system_prompt="你是一个专业的内容总结助手。请简洁明了地总结历史交互信息。",
            tool_list=[],  # 空列表表示不使用工具
            tool_choice=self._resolve_compressor_tool_choice(compressor_model),
            debug_task_id=task_id,
            debug_label="context_builder",
        )
        
        if response.status != "success":
            raise Exception(f"LLM压缩失败: {response.output}")

        output_text = response.output
        
        safe_print(f"✅ 历史交互压缩成功，长度: {len(output_text)} 字符")
        
        return output_text
    
    def _build_structured_call_info(self, current: Dict, current_agent_id: str, task_id: str) -> str:
        """
        构建结构化调用信息（JSON格式，更清晰）
        支持压缩机制：当agent数量超过阈值时，使用LLM压缩
        注意：每个agent的压缩结果单独缓存（因为is_current标记不同）
        """
        hierarchy = current.get("hierarchy", {})
        agents_status = current.get("agents_status", {})
        
        if not agents_status:
            return "(无调用关系)"
        
        # 检查是否已有该agent的压缩结果（每个agent单独缓存）
        cache_key = f"_compressed_structured_call_info_{current_agent_id}"
        compressed_call_info = current.get(cache_key)
        if compressed_call_info:
            safe_print(f"使用已有的压缩结构化调用信息 (agent: {current_agent_id})")
            return compressed_call_info
        
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
        call_tree_json = json.dumps(call_tree, indent=2, ensure_ascii=False)
        
        settings = get_context_settings()
        compress_agent_threshold = settings.get("structured_call_info_compress_threshold_agents", 10)
        compress_token_threshold = settings.get("structured_call_info_compress_threshold_tokens", 2200)
        # 检查是否需要压缩
        agent_count = len(agents_status)
        if agent_count > compress_agent_threshold or self._count_tokens(call_tree_json) > compress_token_threshold:
            safe_print(f"检测到较大的结构化调用信息（{agent_count}个agents，{len(call_tree_json)}字符），启动压缩...")
            compressed_result = self._compress_structured_call_info_with_llm(
                call_tree, current_agent_id, task_id
            )
            
            # 保存压缩结果（针对当前agent）
            cache_key = f"_compressed_structured_call_info_{current_agent_id}"

            def _store_structured_call_cache(latest_context):
                latest_context.setdefault("current", {})[cache_key] = compressed_result

            self.hierarchy_manager.update_context(_store_structured_call_cache)
            
            return compressed_result
        
        return call_tree_json
    
    def _compress_structured_call_info_with_llm(self, call_tree: List[Dict], current_agent_id: str, task_id: str) -> str:
        """
        使用LLM压缩结构化调用信息
        
        Args:
            call_tree: Agent调用树结构
            current_agent_id: 当前正在运行的Agent ID
            
        Returns:
            压缩后的文本（LLM原始输出）
        """
        prompt = f"""请分析以下Agent调用树结构，提取关键信息并总结。

当前正在运行的Agent ID: {current_agent_id}

Agent调用树数据：
{json.dumps(call_tree, ensure_ascii=False, indent=2)}

请总结以下内容：
1. **调用关系概览**：描述整体的Agent层级结构和调用关系
2. **已完成的Agent**：列出已完成的Agent及其关键输出（特别是可能对当前Agent有用的信息）
3. **运行中的Agent**：列出运行中的Agent及其当前thinking
4. **当前Agent的上下文**：重点说明当前Agent的父Agent、兄弟Agent状态，以及可用的上下文信息

要求：
- 保留关键的agent_id、agent_name、level、status信息
- 对于已完成的Agent，保留重要的final_output（可适当精简）
- 对于运行中的Agent，保留关键的thinking（可适当精简）
- 重点突出与当前Agent相关的信息（包括可能用到的文件，可以复用的历史成果）
- 总字符数控制在2000字以内
- 优先使用用户的输入习惯语言进行输出
- 直接输出总结内容文本，不需要任何标记，不要使用markdown格式"""

        from services.llm_client import ChatMessage
        
        messages = [ChatMessage(role="user", content=prompt)]
        
        compressor_model = self._resolve_compressor_model()
        response = self.llm_client.chat(
            history=messages,
            model=compressor_model,  # 使用压缩专用模型
            system_prompt="你是一个专业的内容总结助手。请简洁明了地总结Agent调用树信息。",
            tool_list=[],
            tool_choice=self._resolve_compressor_tool_choice(compressor_model),
            debug_task_id=task_id,
            debug_label="context_builder",
        )
        
        if response.status != "success":
            # 压缩失败时返回原始JSON（截断版）
            safe_print(f"⚠️ LLM压缩失败: {response.output}，使用截断版本")
            original_json = json.dumps(call_tree, indent=2, ensure_ascii=False)
            return original_json[:5000] + "\n...(已截断)"

        output_text = response.output
        
        safe_print(f"✅ 结构化调用信息压缩成功，长度: {len(output_text)} 字符")
        
        return output_text
    
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
        conversations_dir = get_user_conversations_dir()
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
            conversations_dir = get_user_conversations_dir()
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
            except Exception:
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
