#!/usr/bin/env python3
from utils.windows_compat import safe_print
# -*- coding: utf-8 -*-
"""
状态清理器 - 启动前清理栈和current状态
参考原项目的smart_clean_for_restart逻辑
"""

import sys
from pathlib import Path

# 确保可以导入
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

from core.hierarchy_manager import get_hierarchy_manager


def clean_before_start(task_id: str, new_user_input: str = None):
    """
    启动前清理状态
    
    策略：
    1. 如果用户输入改变 → 归档 running agents 到 history，清空 current
    2. 如果用户输入相同 → 保留 running agents（续跑）
    3. 清空栈（因为要重新建立层级）
    
    Args:
        task_id: 任务ID
        new_user_input: 新的用户输入（用于判断是否续跑）
    """
    try:
        hierarchy_manager = get_hierarchy_manager(task_id)
        context = hierarchy_manager._load_context()
        
        # 检查是否有current数据
        if not context.get("current") or not context["current"].get("agents_status"):
            safe_print("ℹ️ 无需清理，状态为空")
            return
        
        current_agents = context["current"]["agents_status"]
        current_hierarchy = context["current"]["hierarchy"]
        
        safe_print(f"🧹 启动前清理状态...")
        safe_print(f"   当前agents数量: {len(current_agents)}")
        
        # 检查用户输入是否改变
        last_instruction = context["current"].get("instructions", [])
        is_same_task = False
        
        if last_instruction and new_user_input:
            last_input = last_instruction[-1].get("instruction", "")
            is_same_task = (last_input == new_user_input)
            if is_same_task:
                safe_print(f"   ℹ️ 检测到相同任务，将续跑")
        
        # 分类：completed vs running
        completed_agents = {}
        completed_hierarchy = {}
        running_agents = {}
        running_count = 0
        
        for agent_id, agent_info in current_agents.items():
            if agent_info.get("status") == "completed":
                # 保留已完成的
                completed_agents[agent_id] = agent_info
                if agent_id in current_hierarchy:
                    completed_hierarchy[agent_id] = current_hierarchy[agent_id]
                safe_print(f"   ✅ 保留已完成: {agent_info.get('agent_name')}")
            else:
                # 收集运行中的（准备归档）
                running_agents[agent_id] = agent_info
                running_count += 1
                safe_print(f"   📦 归档运行中: {agent_info.get('agent_name')}")
        
        # 清理completed agents的children引用（移除running的children）
        for agent_id, hierarchy_info in completed_hierarchy.items():
            # 只保留completed的children
            filtered_children = [
                child_id for child_id in hierarchy_info.get("children", [])
                if child_id in completed_agents
            ]
            completed_hierarchy[agent_id]["children"] = filtered_children
        
        # ✅ 如果有 running agents 且任务改变，归档到 history
        if running_count > 0 and not is_same_task:
            # 找到顶层 running agent（Level 0，即直接调用的）
            top_running = None
            for agent_id, agent_info in running_agents.items():
                parent = current_hierarchy.get(agent_id, {}).get("parent")
                if parent is None:  # 顶层
                    top_running = (agent_id, agent_info)
                    break
            
            if top_running:
                agent_id, agent_info = top_running
                
                # 构造 final_output: latest_thinking + 子 agent 的 final_output
                thinking = agent_info.get("latest_thinking", "(无思考记录)")
                
                # 收集所有已完成的子 agent 的 final_output
                children_outputs = []
                for child_id, child_info in completed_agents.items():
                    child_parent = completed_hierarchy.get(child_id, {}).get("parent")
                    if child_parent == agent_id and child_info.get("final_output"):
                        agent_name = child_info.get("agent_name", "unknown")
                        output = child_info.get("final_output", "")
                        children_outputs.append(f"【{agent_name}】\n{output}")
                
                # 组合 final_output
                final_output = f"【中断任务归档】\n\n"
                final_output += f"## 最新思考\n{thinking}\n\n"
                
                if children_outputs:
                    final_output += f"## 已完成的子任务\n"
                    final_output += "\n\n".join(children_outputs)
                else:
                    final_output += "## 已完成的子任务\n(无)"
                
                # 标记为 completed 并设置 final_output
                agent_info["status"] = "completed"
                agent_info["final_output"] = final_output
                
                # 移到 history
                if "history" not in context:
                    context["history"] = []
                
                history_entry = {
                    "instructions": context["current"].get("instructions", []),
                    "start_time": context["current"].get("start_time", ""),
                    "completion_time": context.get("agent_time_history", {}).get(agent_id, {}).get("end_time", ""),
                    "agents_status": {
                        agent_id: agent_info,
                        **{k: v for k, v in completed_agents.items() 
                           if completed_hierarchy.get(k, {}).get("parent") == agent_id}
                    },
                    "hierarchy": {
                        agent_id: current_hierarchy.get(agent_id, {}),
                        **{k: v for k, v in completed_hierarchy.items() 
                           if v.get("parent") == agent_id}
                    }
                }
                
                context["history"].append(history_entry)
                safe_print(f"   📦 已将中断任务归档到 history")
                safe_print(f"      顶层 agent: {agent_info.get('agent_name')}")
                safe_print(f"      子任务数: {len(children_outputs)}")
        
        # 更新context
        if not is_same_task:
            # 新任务：清空 current
            context["current"]["agents_status"] = {}
            context["current"]["hierarchy"] = {}
            context["current"]["instructions"] = []
            # 删除压缩的历史（如果有）
            if "_compressed_user_agent_history" in context["current"]:
                del context["current"]["_compressed_user_agent_history"]
            # 删除所有agent的结构化调用信息压缩缓存
            keys_to_delete = [k for k in context["current"].keys() if k.startswith("_compressed_structured_call_info_")]
            for key in keys_to_delete:
                del context["current"][key]
            safe_print(f"   🗑️ 清空 current，准备新任务")
        else:
            # 续跑：保留 running agents
            context["current"]["agents_status"] = {**completed_agents, **running_agents}
            # hierarchy 保留所有
            safe_print(f"   ♻️ 保留 running agents，继续任务")
            safe_print(f"      Running: {running_count} 个")
            safe_print(f"      Completed: {len(completed_agents)} 个")
        
        # 保存
        hierarchy_manager._save_context(context)
        
        # 清空栈
        hierarchy_manager._save_stack([])
        
        safe_print(f"✅ 清理完成:")
        safe_print(f"   保留: {len(completed_agents)} 个已完成agent")
        safe_print(f"   删除: {running_count} 个运行中agent")
        safe_print(f"   栈已清空")
    
    except Exception as e:
        safe_print(f"⚠️ 清理失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).parent.parent))
    
    # 测试清理功能
    clean_before_start("test_task_123")

