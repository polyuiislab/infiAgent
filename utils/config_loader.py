#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置加载器 - 读取agent_library中的配置文件
"""

import os
import yaml
from typing import Dict, List, Any
from pathlib import Path

from utils.user_paths import get_project_root, get_user_data_root


class ConfigLoader:
    """配置加载器，负责读取和合并agent配置"""
    
    def __init__(self, agent_system_name: str = "infiHelper", agent_library_root: str | None = None):
        """
        初始化配置加载器
        
        Args:
            agent_system_name: Agent系统名称，对应agent_library下的文件夹
            agent_library_root: 可选。显式指定用户 agent_library 根目录（其下应包含 agent_library/<system>）
        """
        self.agent_system_name = agent_system_name
        self.agent_library_root = str(agent_library_root).strip() if agent_library_root else ""
        
        # 查找配置目录（支持：项目内 config + 用户导入目录）
        # - 项目内: <project_root>/config/agent_library/<system>
        # - 用户导入: $MLA_AGENT_LIBRARY_DIR/agent_library/<system>
        self.config_root = self._find_config_root()
        self.agent_config_dir = self._find_agent_system_dir(agent_system_name)
        
        if not os.path.exists(self.agent_config_dir):
            raise FileNotFoundError(f"Agent配置目录不存在: {self.agent_config_dir}")

        # Agent-system requirements are no longer auto-installed. Installing
        # into a hidden venv but executing arbitrary commands elsewhere creates
        # confusing dependency behavior, so dependency setup is now explicit.
        self.agent_system_requirements_status = {
            "status": "disabled",
            "reason": "agent_system_requirements_auto_install_removed",
        }
        
        # 加载所有配置
        self.general_prompts = self._load_general_prompts()
        self.all_tools = self._load_all_tools()
        self._inject_framework_default_tools()
        
    def _find_config_root(self) -> str:
        """查找配置根目录"""
        mla_v3_config = get_project_root() / "config"

        if not mla_v3_config.exists():
            raise FileNotFoundError(f"配置目录不存在: {mla_v3_config}")
        
        return str(mla_v3_config)

    def _find_agent_system_dir(self, agent_system_name: str) -> str:
        """按优先级查找 agent_system 配置目录"""
        candidates = []

        # 1) 用户导入目录（用于桌面端打包后的可扩展配置）
        # 约定：MLA_AGENT_LIBRARY_DIR 指向包含 agent_library/ 的根目录（例如 ~/mla_v3）
        user_root = self.agent_library_root or os.environ.get("MLA_AGENT_LIBRARY_DIR", "").strip()
        if not user_root:
            user_root = str(get_user_data_root())
        candidates.append(Path(user_root) / "agent_library" / agent_system_name)

        # 2) 项目内 config
        candidates.append(Path(self.config_root) / "agent_library" / agent_system_name)

        for p in candidates:
            if p.exists():
                return str(p)

        # 默认回退到项目路径（抛错由上层处理）
        return str(candidates[-1])
    
    def _load_general_prompts(self) -> Dict:
        """
        加载通用提示词配置
        
        注意：general_prompts.yaml 现在使用 XML 格式
        由 ContextBuilder 直接读取，此方法保留为兼容性
        """
        prompts_file = os.path.join(self.agent_config_dir, "general_prompts.yaml")
        if not os.path.exists(prompts_file):
            return {}
        
        with open(prompts_file, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            # 兼容旧格式
            return data.get("general_prompts", {})
    
    def _load_all_tools(self) -> Dict[str, Dict]:
        """加载所有工具和Agent配置"""
        all_tools = {}
        
        # 查找所有level配置文件
        for filename in os.listdir(self.agent_config_dir):
            if filename.startswith("level_") and filename.endswith(".yaml"):
                filepath = os.path.join(self.agent_config_dir, filename)
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                    tools = data.get("tools", {})
                    all_tools.update(tools)
        
        return all_tools

    def _inject_framework_default_tools(self):
        """注入框架级默认工具，无需逐个 agent_system 重复声明。"""
        self.all_tools.setdefault(
            "load_skill",
            {
                "level": 0,
                "type": "tool_call_agent",
                "name": "load_skill",
                "description": "将指定 skill 部署到当前 task 的 .skills/<skill_name>/ 目录，并把对应 SKILL.md 注入当前 agent 的上下文。部署后，execute_command 默认就在 task 根目录执行，因此可直接读取或运行 .skills/<skill_name>/ 下的文件与脚本。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "要加载的 skill 名称，例如 pptx、pdf、webapp-testing。",
                        }
                    },
                    "required": ["skill_name"],
                },
            },
        )
        self.all_tools.setdefault(
            "offload_skill",
            {
                "level": 0,
                "type": "tool_call_agent",
                "name": "offload_skill",
                "description": "从当前 agent 的上下文中卸载已经加载的 skill 内容，不删除 task 工作目录中的 .skills/<skill_name>/ 文件。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "skill_name": {
                            "type": "string",
                            "description": "要卸载的 skill 名称。",
                        }
                    },
                    "required": ["skill_name"],
                },
            },
        )
        self.all_tools.setdefault(
            "task_history_search",
            {
                "level": 0,
                "type": "tool_call_agent",
                "name": "task_history_search",
                "description": "检索当前 task_id 已归档的历史任务记录。只保留两种查询能力：1) round_range 按历史轮次查，例如 '1-3' 查第1到第3轮，'4' 查第4轮，'-2' 查最近两轮；2) keyword 全字段关键词查，会同时检索用户输入、agent final_output、thinking 和文件名/路径。keyword 可以是中文、文件名、路径或关键短语。两个参数同时提供时取交集。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "round_range": {
                            "type": "string",
                            "description": "可选。按历史轮次查询。'1-3' 表示第1到第3轮，'4' 表示第4轮，'-2' 表示最近两轮。轮次从1开始，按任务归档顺序计算。",
                        },
                        "keyword": {
                            "type": "string",
                            "description": "可选。全字段关键词检索文本。会同时检索历史用户输入、agent 输出、thinking、文件名和路径；可以包含中文、斜杠、冒号、括号等普通文本。",
                        },
                    },
                    "required": [],
                },
            },
        )
        from utils.user_paths import get_project_root
        definitions = get_project_root() / "config" / "orchestration_tools.yaml"
        if definitions.is_file():
            for name, definition in (yaml.safe_load(definitions.read_text(encoding="utf-8")) or {}).get("tools", {}).items():
                self.all_tools.setdefault(name, definition)
        for definition in self.all_tools.values():
            if definition.get("type") == "llm_call_agent":
                schema = definition.setdefault("parameters", {"type": "object", "properties": {}})
                if 'required' in schema:
                    schema['required'] = [name for name in schema['required'] if name != 'task_id']
                schema.setdefault("properties", {}).setdefault("_background", {
                    "type": "boolean", "description": "Return a handle immediately; collect the child result later."})
                schema["properties"].setdefault("_context_mode", {"type": "string", "enum": ["tool", "fresh", "inherit"]})
        # The public runtime has no enterprise program/data-source subsystems.
    
    def get_tool_config(self, tool_name: str) -> Dict:
        """
        获取指定工具的配置，并处理available_tool_level字段
        
        Args:
            tool_name: 工具名称
            
        Returns:
            工具配置字典
        """
        if tool_name not in self.all_tools:
            raise KeyError(f"工具 {tool_name} 不存在于配置中")
        
        config = self.all_tools[tool_name].copy()
        config = self._normalize_agent_runtime_config(config)
        
        # 处理available_tool_level（特殊情况：judge_agent）
        if "available_tool_level" in config and "available_tools" not in config:
            tool_level = config["available_tool_level"]
            # 获取该level的所有工具
            level_tools = self.get_available_tools_by_level(tool_level)
            config["available_tools"] = level_tools
            print(f"✅ 为{tool_name}自动生成工具列表（Level {tool_level}）: {len(level_tools)}个工具")
        
        return config

    def _normalize_agent_runtime_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """兼容旧 agent YAML，并补齐新的运行时字段。"""
        normalized = dict(config)

        execution_model = (
            normalized.get("execution_model")
            or normalized.get("model")
            or normalized.get("model_type")
            or ""
        )
        if execution_model:
            normalized["execution_model"] = execution_model

        alias_fields = {
            "thinking_model": ["thinking_model"],
            "compressor_model": ["compressor_model"],
            "image_generation_model": ["image_generation_model", "figure_model", "generate_figure_model"],
            "read_figure_model": ["read_figure_model", "vision_model"],
            "max_tokens": ["max_tokens"],
            "action_window_steps": ["action_window_steps", "thinking_steps", "thinking_interval"],
            "thinking_interval": ["thinking_interval", "thinking_steps", "action_window_steps"],
            "thinking_steps": ["thinking_steps", "thinking_interval", "action_window_steps"],
            "reasoning_mode": ["reasoning_mode", "reasoning"],
            "thinking_enabled": ["thinking_enabled"],
            "no_tool_retry_limit": ["no_tool_retry_limit"],
        }

        for canonical_name, aliases in alias_fields.items():
            if normalized.get(canonical_name) not in (None, ""):
                continue
            for alias in aliases:
                value = normalized.get(alias)
                if value not in (None, ""):
                    normalized[canonical_name] = value
                    break

        return normalized
    
    def build_agent_system_prompt(self, agent_config: Dict) -> str:
        """
        ⚠️ 已废弃：此方法不再使用
        
        上下文构建已移至 ContextBuilder.build_context()
        该方法负责读取 general_prompts.yaml（XML格式）并构建完整上下文
        """
        # 保留此方法仅为向后兼容
        return ""
    
    def get_available_tools_by_level(self, level: int) -> List[str]:
        """
        获取指定level的所有工具名称
        
        Args:
            level: 工具级别
            
        Returns:
            工具名称列表
        """
        tools = []
        for tool_name, tool_config in self.all_tools.items():
            if tool_config.get("level") == level:
                tools.append(tool_name)
        return tools


if __name__ == "__main__":
    # 测试配置加载
    loader = ConfigLoader("infiHelper")
    print(f"✅ 成功加载配置系统: {loader.agent_system_name}")
    print(f"📁 配置目录: {loader.agent_config_dir}")
    print(f"🔧 总共加载 {len(loader.all_tools)} 个工具/Agent")
    print(f"\nLevel 0 工具数量: {len(loader.get_available_tools_by_level(0))}")
    print(f"Level 1 Agent数量: {len(loader.get_available_tools_by_level(1))}")
