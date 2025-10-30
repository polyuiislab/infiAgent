#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
人类交互工具
"""

from pathlib import Path
from typing import Dict, Any
import asyncio
from .file_tools import BaseTool

# 全局 HIL 任务状态存储
HIL_TASKS = {}


class HumanInLoopTool(BaseTool):
    """人类交互工具 - 挂起等待人类完成任务（异步，不阻塞服务器）"""
    
    async def execute_async(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        人类交互 - 挂起等待人类完成
        
        Parameters:
            hil_id (str): 人类任务唯一ID
            instruction (str): 给人类的指令
            timeout (int, optional): 超时时间（秒），默认 None（无限等待）
        """
        try:
            hil_id = parameters.get("hil_id")
            instruction = parameters.get("instruction")
            timeout = parameters.get("timeout")  # 默认 None
            
            if not hil_id:
                return {
                    "status": "error",
                    "output": "",
                    "error": "hil_id is required"
                }
            
            if not instruction:
                return {
                    "status": "error",
                    "output": "",
                    "error": "instruction is required"
                }
            
            # 注册 HIL 任务
            HIL_TASKS[hil_id] = {
                "status": "waiting",
                "instruction": instruction,
                "task_id": task_id,
                "result": None
            }
            
            # 异步轮询等待完成
            start_time = asyncio.get_event_loop().time()
            check_interval = 2  # 每2秒检查一次
            
            while True:
                # 检查是否超时
                if timeout is not None:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    if elapsed > timeout:
                        HIL_TASKS[hil_id]["status"] = "timeout"
                        return {
                            "status": "error",
                            "output": "",
                            "error": f"Human task timeout ({timeout}s)"
                        }
                
                # 检查任务状态
                task = HIL_TASKS.get(hil_id)
                if task and task["status"] == "completed":
                    result = task.get("result", "任务已完成")
                    # 清理任务
                    del HIL_TASKS[hil_id]
                    return {
                        "status": "success",
                        "output": f"人类任务已完成: {result}",
                        "error": ""
                    }
                
                # 检查是否取消
                if task and task["status"] == "cancelled":
                    reason = task.get("result", "用户取消操作")
                    # 清理任务
                    del HIL_TASKS[hil_id]
                    return {
                        "status": "success",
                        "output": f"用户取消操作: {reason}",
                        "error": ""
                    }
                
                # 异步等待（不阻塞服务器）
                await asyncio.sleep(check_interval)
                
        except Exception as e:
            # 清理任务
            if hil_id in HIL_TASKS:
                del HIL_TASKS[hil_id]
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }


def get_hil_status(hil_id: str) -> Dict[str, Any]:
    """获取 HIL 任务状态"""
    task = HIL_TASKS.get(hil_id)
    if not task:
        return {
            "found": False,
            "error": f"HIL task not found: {hil_id}"
        }
    
    return {
        "found": True,
        "hil_id": hil_id,
        "status": task["status"],
        "instruction": task["instruction"],
        "task_id": task["task_id"]
    }


def complete_hil_task(hil_id: str, result: str = "完成") -> Dict[str, Any]:
    """完成 HIL 任务"""
    task = HIL_TASKS.get(hil_id)
    if not task:
        return {
            "success": False,
            "error": f"HIL task not found: {hil_id}"
        }
    
    # 标记为完成
    HIL_TASKS[hil_id]["status"] = "completed"
    HIL_TASKS[hil_id]["result"] = result
    
    return {
        "success": True,
        "message": f"HIL task {hil_id} marked as completed"
    }


def cancel_hil_task(hil_id: str, reason: str = "用户取消") -> Dict[str, Any]:
    """取消 HIL 任务"""
    task = HIL_TASKS.get(hil_id)
    if not task:
        return {
            "success": False,
            "error": f"HIL task not found: {hil_id}"
        }
    
    # 标记为取消
    HIL_TASKS[hil_id]["status"] = "cancelled"
    HIL_TASKS[hil_id]["result"] = reason
    
    return {
        "success": True,
        "message": f"HIL task {hil_id} marked as cancelled"
    }


def list_hil_tasks() -> Dict[str, Any]:
    """列出所有 HIL 任务"""
    tasks = []
    for hil_id, task in HIL_TASKS.items():
        tasks.append({
            "hil_id": hil_id,
            "status": task["status"],
            "instruction": task["instruction"],
            "task_id": task["task_id"]
        })
    
    return {
        "total": len(tasks),
        "tasks": tasks
    }

