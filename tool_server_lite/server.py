#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量化工具服务器 - 基于 FastAPI
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import uvicorn
from pathlib import Path
import sys

# 添加父目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from tools import (
    FileReadTool,
    FileWriteTool,
    DirListTool,
    DirCreateTool,
    FileMoveTool,
    FileDeleteTool,
    WebSearchTool,
    GoogleScholarSearchTool,
    ArxivSearchTool,
    CrawlPageTool,
    FileDownloadTool,
    ParseDocumentTool,
    VisionTool,
    AudioTool,
    PaperAnalyzeTool,
    MarkdownToPdfTool,
    MarkdownToDocxTool,
    TexToPdfTool,
    HumanInLoopTool,
    ExecuteCodeTool,
    PipInstallTool,
    ExecuteCommandTool
)
from tools.human_tools import get_hil_status, complete_hil_task, cancel_hil_task, list_hil_tasks

app = FastAPI(
    title="Tool Server Lite",
    description="轻量化工具服务器",
    version="1.0.0"
)

# 初始化所有工具
TOOLS = {
    "file_read": FileReadTool(),
    "file_write": FileWriteTool(),
    "dir_list": DirListTool(),
    "dir_create": DirCreateTool(),
    "file_move": FileMoveTool(),
    "file_delete": FileDeleteTool(),
    "web_search": WebSearchTool(),
    "google_scholar_search": GoogleScholarSearchTool(),
    "arxiv_search": ArxivSearchTool(),
    "crawl_page": CrawlPageTool(),
    "file_download": FileDownloadTool(),
    "parse_document": ParseDocumentTool(),
    "vision_tool": VisionTool(),
    "audio_tool": AudioTool(),
    "paper_analyze_tool": PaperAnalyzeTool(),
    "md_to_pdf": MarkdownToPdfTool(),
    "md_to_docx": MarkdownToDocxTool(),
    "tex_to_pdf": TexToPdfTool(),
    "human_in_loop": HumanInLoopTool(),
    "execute_code": ExecuteCodeTool(),
    "pip_install": PipInstallTool(),
    "execute_command": ExecuteCommandTool(),
}


# ===== 请求模型 =====
class ToolExecuteRequest(BaseModel):
    """工具执行请求"""
    task_id: str  # 绝对路径，作为 workspace
    parameters: Dict[str, Any]


class TaskCreateRequest(BaseModel):
    """任务创建请求"""
    task_id: str  # 绝对路径


# ===== API 端点 =====
@app.get("/")
async def root():
    """服务器基本信息"""
    return {
        "message": "Tool Server Lite is running",
        "version": "1.0.0",
        "tools": list(TOOLS.keys())
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {
        "status": "healthy",
        "service": "tool_server_lite",
        "version": "1.0.0"
    }


@app.get("/api/tools")
async def get_tools():
    """获取可用工具列表"""
    return {
        "success": True,
        "data": list(TOOLS.keys())
    }


@app.get("/api/task/{task_id}/status")
async def get_task_status(task_id: str):
    """
    获取任务状态（兼容旧API）
    
    Args:
        task_id: 任务ID（绝对路径）
    """
    try:
        workspace = Path(task_id)
        
        if workspace.exists() and workspace.is_dir():
            return {
                "success": True,
                "data": {
                    "task_id": task_id,
                    "status": "active",
                    "workspace": str(workspace)
                }
            }
        else:
            raise HTTPException(status_code=404, detail="Task not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/task/create")
async def create_task(request: TaskCreateRequest = None, task_id: str = None, task_name: str = None):
    """
    创建任务 - 兼容两种调用方式
    
    方式1（新）: JSON body {"task_id": "..."}
    方式2（旧）: Query params ?task_id=...&task_name=...
    """
    try:
        # 优先使用 request body
        if request:
            workspace_path = request.task_id
        elif task_id:
            workspace_path = task_id
        else:
            raise HTTPException(status_code=400, detail="task_id is required")
        
        workspace = Path(workspace_path)
        
        # 检查目录是否存在，不存在则创建
        if not workspace.exists():
            workspace.mkdir(parents=True, exist_ok=True)
        
        # 创建必要的子文件夹
        (workspace / "upload").mkdir(exist_ok=True)
        (workspace / "code_run").mkdir(exist_ok=True)
        (workspace / "code_env").mkdir(exist_ok=True)
        
        return {
            "success": True,
            "message": f"Task workspace ready: {workspace}",
            "data": {
                "workspace": str(workspace),
                "created_folders": ["upload", "code_run", "code_env"]
            }
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class OldToolExecuteRequest(BaseModel):
    """旧版工具执行请求（兼容）"""
    task_id: str
    tool_name: str
    params: Dict[str, Any]


@app.post("/api/tool/execute")
async def execute_tool_old_api(request: OldToolExecuteRequest):
    """
    执行工具（旧版API兼容）
    
    Args:
        request: {"task_id": "...", "tool_name": "...", "params": {...}}
    """
    try:
        tool_name = request.tool_name
        
        # 检查工具是否存在
        if tool_name not in TOOLS:
            return {
                "success": False,
                "error": f"Tool '{tool_name}' not found. Available tools: {list(TOOLS.keys())}"
            }
        
        tool = TOOLS[tool_name]
        
        # 执行工具（支持异步工具）
        if hasattr(tool, 'execute_async'):
            result = await tool.execute_async(
                task_id=request.task_id,
                parameters=request.params
            )
        else:
            result = tool.execute(
                task_id=request.task_id,
                parameters=request.params
            )
        
        # 返回旧版格式
        if result["status"] == "success":
            return {
                "success": True,
                "data": result
            }
        else:
            return {
                "success": False,
                "error": result.get("error", "Unknown error"),
                "data": result
            }
        
    except Exception as e:
        import traceback
        return {
            "success": False,
            "error": str(e),
            "traceback": traceback.format_exc()
        }


@app.post("/api/execute/{tool_name}")
async def execute_tool(tool_name: str, request: ToolExecuteRequest):
    """
    执行工具（新版API）
    
    Args:
        tool_name: 工具名称
        request: 包含 task_id(workspace绝对路径) 和 parameters
    """
    try:
        # 检查工具是否存在
        if tool_name not in TOOLS:
            raise HTTPException(
                status_code=404,
                detail=f"Tool '{tool_name}' not found. Available tools: {list(TOOLS.keys())}"
            )
        
        tool = TOOLS[tool_name]
        
        # 执行工具（支持异步工具）
        if hasattr(tool, 'execute_async'):
            result = await tool.execute_async(
                task_id=request.task_id,
                parameters=request.parameters
            )
        else:
            result = tool.execute(
                task_id=request.task_id,
                parameters=request.parameters
            )
        
        return {
            "success": result["status"] == "success",
            "data": result
        }
        
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "data": {
                    "status": "error",
                    "output": "",
                    "error": str(e),
                    "traceback": traceback.format_exc()
                }
            }
        )


@app.get("/api/hil/tasks")
async def get_all_hil_tasks():
    """获取所有 HIL 任务"""
    return list_hil_tasks()


@app.get("/api/hil/{hil_id}")
async def get_hil_task(hil_id: str):
    """获取指定 HIL 任务状态"""
    return get_hil_status(hil_id)


class HilCompleteRequest(BaseModel):
    """HIL完成请求"""
    result: str = "完成"


@app.post("/api/hil/complete/{hil_id}")
async def complete_hil(hil_id: str, request: HilCompleteRequest = None):
    """完成 HIL 任务"""
    result = request.result if request else "完成"
    return complete_hil_task(hil_id, result)


class HilCancelRequest(BaseModel):
    """HIL取消请求"""
    reason: str = "用户取消"


@app.post("/api/hil/cancel/{hil_id}")
async def cancel_hil(hil_id: str, request: HilCancelRequest = None):
    """取消 HIL 任务"""
    reason = request.reason if request else "用户取消"
    return cancel_hil_task(hil_id, reason)


def start_server(host: str = "0.0.0.0", port: int = 8001):
    """启动服务器"""
    print(f"🚀 Starting Tool Server Lite on {host}:{port}")
    print(f"📚 Available tools: {len(TOOLS)}")
    print(f"🔗 API Docs: http://{host}:{port}/docs")
    
    uvicorn.run(app, host=host, port=port)


def get_server_pid() -> int:
    """获取服务器进程ID"""
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-f", "tool_server_lite.*server.py"],
            capture_output=True,
            text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            return int(result.stdout.strip().split('\n')[0])
    except:
        pass
    return None


def server_status():
    """检查服务器状态"""
    import requests
    
    pid = get_server_pid()
    
    if pid:
        # 进程存在，检查是否响应
        try:
            response = requests.get("http://localhost:8001/health", timeout=2)
            if response.status_code == 200:
                print(f"✅ Tool Server 运行中")
                print(f"   PID: {pid}")
                print(f"   地址: http://localhost:8001")
                return True
        except:
            print(f"⚠️  进程存在但未响应 (PID: {pid})")
            return False
    
    print("❌ Tool Server 未运行")
    return False


def server_stop():
    """停止服务器（杀掉所有匹配进程）"""
    import subprocess
    import signal
    import os
    
    try:
        # 获取所有匹配的进程 PID
        result = subprocess.run(
            ["pgrep", "-f", "tool_server_lite.*server.py"],
            capture_output=True,
            text=True
        )
        
        if result.returncode != 0 or not result.stdout.strip():
            print("ℹ️  服务器未运行")
            return
        
        # 杀掉所有进程
        pids = [int(pid) for pid in result.stdout.strip().split('\n') if pid.strip()]
        killed_count = 0
        
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
                print(f"✅ 已停止进程: {pid}")
                killed_count += 1
            except Exception as e:
                print(f"⚠️  停止 PID {pid} 失败: {e}")
        
        if killed_count > 0:
            print(f"✅ Tool Server 已停止（共 {killed_count} 个进程）")
        
    except Exception as e:
        print(f"❌ 停止失败: {e}")


def server_start_daemon(host="0.0.0.0", port=8001):
    """后台启动服务器"""
    import subprocess
    import sys
    
    if server_status():
        print("ℹ️  服务器已在运行")
        return
    
    # 后台启动
    subprocess.Popen(
        [sys.executable, __file__, "--host", host, "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    
    import time
    time.sleep(2)
    
    if server_status():
        print("✅ Tool Server 已启动（后台）")
    else:
        print("❌ 启动失败")


def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description="Tool Server Lite - 服务管理")
    parser.add_argument("command", nargs='?', default=None,
                       help="服务管理命令: start, stop, status, restart（不指定则前台运行）")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", default=8001, type=int, help="Port to bind")
    
    args = parser.parse_args()
    
    # 根据命令执行
    if args.command == "status":
        server_status()
    elif args.command == "stop":
        server_stop()
    elif args.command == "start":
        server_start_daemon(args.host, args.port)
    elif args.command == "restart":
        server_stop()
        import time
        time.sleep(1)
        server_start_daemon(args.host, args.port)
    elif args.command is None:
        # 无命令 - 前台启动
        start_server(host=args.host, port=args.port)
    else:
        print(f"❌ 未知命令: {args.command}")
        print("可用命令: start, stop, status, restart")
        print("或不带命令参数以前台运行")


if __name__ == "__main__":
    main()

