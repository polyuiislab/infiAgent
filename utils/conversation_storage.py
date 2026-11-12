#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对话历史存储 - 简化版
只保存action_history，不保存传统的user/assistant对话
"""

import json
import hashlib
from pathlib import Path
from typing import Dict, List
from datetime import datetime

from utils.user_paths import get_user_conversations_dir
from utils.atomic_state import write_json, StateCommitError, StateCorruptError


class ConversationStorage:
    """对话历史存储器"""
    
    def __init__(self, task_id: str = None):
        """初始化存储器 - 使用用户主目录（跨平台）"""
        self.conversations_dir = get_user_conversations_dir()
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        self.task_id = task_id
    
    def _generate_filename(self, task_id: str, agent_id: str) -> str:
        """生成对话文件名：hash + 最后文件夹名 + agent_id"""
        from pathlib import Path
        import hashlib
        
        task_hash = hashlib.md5(task_id.encode()).hexdigest()[:8]
        # 跨平台路径处理：检查是否是路径（包含/或\）
        import os
        task_folder = Path(task_id).name if (os.sep in task_id or '/' in task_id or '\\' in task_id) else task_id
        task_name = f"{task_hash}_{task_folder}"
        
        return str(self.conversations_dir / f"{task_name}_{agent_id}_actions.json")
    
    def save_actions(self, task_id: str, agent_id: str, agent_name: str, 
                    task_input: str, action_history: List[Dict], current_turn: int,
                    latest_thinking: str = "", first_thinking_done: bool = False,
                    tool_call_counter: int = 0, system_prompt: str = "",
                    action_history_fact: List[Dict] = None,
                    pending_tools: List[Dict] = None,
                    llm_turn_counter: int = 0):
        """
        保存动作历史和完整状态
        
        Args:
            task_id: 任务ID
            agent_id: Agent ID
            agent_name: Agent名称
            task_input: 任务输入
            action_history: 动作历史列表（含新字段：_turn, tool_call_id, assistant_content, 
                           _has_image, _image_base64 等）
            current_turn: 当前执行轮次
            latest_thinking: 最新的thinking内容
            first_thinking_done: 是否已完成首次thinking
            tool_call_counter: 工具调用计数
            system_prompt: 完整的system_prompt（包含XML上下文，用于调试参考）
            action_history_fact: 完整动作轨迹（不含 base64 图片数据）
            pending_tools: 待执行的工具列表
            llm_turn_counter: LLM 调用轮次计数器（用于消息分组）
        """
        try:
            filepath = self._generate_filename(task_id, agent_id)
            
            data = {
                "task_id": task_id,
                "agent_id": agent_id,
                "agent_name": agent_name,
                "task_input": task_input,
                "current_turn": current_turn,
                "action_history": action_history,  # 含 base64 图片数据（用于 messages 重建）
                "action_history_fact": action_history_fact if action_history_fact else action_history,  # 完整轨迹（不含 base64）
                "pending_tools": pending_tools if pending_tools else [],
                "latest_thinking": latest_thinking,
                "first_thinking_done": first_thinking_done,
                "tool_call_counter": tool_call_counter,
                "llm_turn_counter": llm_turn_counter,
                "system_prompt": system_prompt,
                "last_updated": datetime.now().isoformat()
            }
            
            write_json(filepath, data)
        
        except Exception as e:
            raise StateCommitError(f"Cannot save conversation: {filepath}") from e
    
    def load_actions(self, task_id: str, agent_id: str) -> Dict:
        """
        加载动作历史
        
        Args:
            task_id: 任务ID
            agent_id: Agent ID
            
        Returns:
            动作历史数据，如果不存在则返回None
        """
        try:
            filepath = self._generate_filename(task_id, agent_id)
            
            if not Path(filepath).exists():
                return None
            
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            print(f"📂 已加载动作历史: 第{data.get('current_turn', 0)}轮, {len(data.get('action_history', []))}个动作")
            return data
        
        except Exception as e:
            raise StateCorruptError(f"Cannot load conversation: {filepath}") from e


if __name__ == "__main__":
    # 测试存储器
    storage = ConversationStorage()
    
    # 测试保存
    storage.save_actions(
        task_id="test",
        agent_id="agent_123",
        agent_name="test_agent",
        task_input="测试任务",
        action_history=[
            {"tool_name": "file_read", "arguments": {}, "result": {}}
        ],
        current_turn=1
    )
    
    # 测试加载
    data = storage.load_actions("test", "agent_123")
    print(f"✅ 加载的数据: {data}")

