#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
轻量化工具服务器 - 基于 FastAPI
"""

import sys

# Windows控制台UTF-8编码支持（解决emoji显示问题）
if sys.platform == 'win32':
    try:
        import io
        # 强制行缓冲和立即写入，避免输出延迟
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True, write_through=True)
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', line_buffering=True, write_through=True)
    except Exception as e:
        # 如果设置失败，使用默认编码（静默失败）
        # 这可能发生在某些特殊的控制台环境中
        pass

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any, List, Tuple
import uvicorn
import asyncio
from pathlib import Path
from urllib.parse import urlparse

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
    CreateImageTool,
    AudioTool,
    PaperAnalyzeTool,
    MarkdownToPdfTool,
    MarkdownToDocxTool,
    TexToPdfTool,
    HumanInLoopTool,
    ExecuteCodeTool,
    PipInstallTool,
    ExecuteCommandTool,
    GrepTool,
    CodeProcessManagerTool,
    ReferenceListTool,
    ReferenceAddTool,
    ReferenceDeleteTool,
    ImagesToPptTool,
    BrowserLaunchTool,
    BrowserCloseTool,
    BrowserNewPageTool,
    BrowserSwitchPageTool,
    BrowserClosePageTool,
    BrowserListPagesTool,
    BrowserNavigateTool,
    BrowserSnapshotTool,
    BrowserExecuteJsTool,
    BrowserClickTool,
    BrowserTypeTool,
    BrowserWaitTool,
    BrowserMouseMoveTool,
    BrowserMouseClickCoordsTool,
    BrowserDragAndDropTool,
    BrowserHoverTool,
    BrowserScrollTool
)
from tools.human_tools import (
    get_hil_status, respond_hil_task, list_hil_tasks, get_hil_task_for_workspace,
    create_tool_confirmation, get_tool_confirmation_status, respond_tool_confirmation,
    get_tool_confirmation_for_workspace, list_tool_confirmations
)

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
    "create_image": CreateImageTool(),
    "audio_tool": AudioTool(),
    "paper_analyze_tool": PaperAnalyzeTool(),
    "md_to_pdf": MarkdownToPdfTool(),
    "md_to_docx": MarkdownToDocxTool(),
    "tex_to_pdf": TexToPdfTool(),
    "human_in_loop": HumanInLoopTool(),
    "execute_code": ExecuteCodeTool(),
    "pip_install": PipInstallTool(),
    "execute_command": ExecuteCommandTool(),
    "grep": GrepTool(),
    "manage_code_process": CodeProcessManagerTool(),
    "reference_list": ReferenceListTool(),
    "reference_add": ReferenceAddTool(),
    "reference_delete": ReferenceDeleteTool(),
    "images_to_ppt": ImagesToPptTool(),
    "browser_launch": BrowserLaunchTool(),
    "browser_close": BrowserCloseTool(),
    "browser_new_page": BrowserNewPageTool(),
    "browser_switch_page": BrowserSwitchPageTool(),
    "browser_close_page": BrowserClosePageTool(),
    "browser_list_pages": BrowserListPagesTool(),
    "browser_navigate": BrowserNavigateTool(),
    "browser_snapshot": BrowserSnapshotTool(),
    "browser_execute_js": BrowserExecuteJsTool(),
    "browser_click": BrowserClickTool(),
    "browser_type": BrowserTypeTool(),
    "browser_wait": BrowserWaitTool(),
    "browser_mouse_move": BrowserMouseMoveTool(),
    "browser_mouse_click_coords": BrowserMouseClickCoordsTool(),
    "browser_drag_and_drop": BrowserDragAndDropTool(),
    "browser_hover": BrowserHoverTool(),
    "browser_scroll": BrowserScrollTool(),
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
        # (workspace / "temp").mkdir(exist_ok=True)
        # (workspace / "code_run").mkdir(exist_ok=True)
        # (workspace / "code_env").mkdir(exist_ok=True)
        
        # # 创建默认的 reference.bib 文件（如果不存在）
        # reference_bib = workspace / "reference.bib"
        # if not reference_bib.exists():
        #     reference_bib.write_text("", encoding='utf-8')
        
        return {
            "success": True,
            "message": f"Task workspace ready: {workspace}",
            "data": {
                "workspace": str(workspace),
                # "created_folders": ["temp", "code_run", "code_env"],
                # "created_files": ["reference.bib"]
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
            # 异步工具直接 await
            result = await tool.execute_async(
                task_id=request.task_id,
                parameters=request.params
            )
        else:
            # 同步工具在线程池中执行，避免阻塞事件循环
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,  # 使用默认线程池
                tool.execute,
                request.task_id,
                request.params
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
            # 异步工具直接 await
            result = await tool.execute_async(
                task_id=request.task_id,
                parameters=request.parameters
            )
        else:
            # 同步工具在线程池中执行，避免阻塞事件循环
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,  # 使用默认线程池
                tool.execute,
                request.task_id,
                request.parameters
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


class HilRespondRequest(BaseModel):
    """HIL响应请求"""
    response: str


@app.post("/api/hil/respond/{hil_id}")
async def respond_hil(hil_id: str, request: HilRespondRequest):
    """响应 HIL 任务（用户可以回复任何内容）"""
    return respond_hil_task(hil_id, request.response)


@app.get("/api/hil/workspace/{task_id:path}")
async def get_workspace_hil(task_id: str):
    """获取指定 workspace 的 HIL 任务"""
    return get_hil_task_for_workspace(task_id)


# ===== 工具确认 API =====

class ToolConfirmationCreateRequest(BaseModel):
    """工具确认创建请求"""
    confirm_id: str
    task_id: str
    tool_name: str
    arguments: Dict[str, Any]


@app.post("/api/tool-confirmation/create")
async def create_confirmation(request: ToolConfirmationCreateRequest):
    """创建工具确认请求"""
    return create_tool_confirmation(
        request.confirm_id,
        request.task_id,
        request.tool_name,
        request.arguments
    )


@app.get("/api/tool-confirmation/{confirm_id}")
async def get_confirmation(confirm_id: str):
    """获取工具确认状态"""
    return get_tool_confirmation_status(confirm_id)


class ToolConfirmationRespondRequest(BaseModel):
    """工具确认响应请求"""
    approved: bool


@app.post("/api/tool-confirmation/respond/{confirm_id}")
async def respond_confirmation(confirm_id: str, request: ToolConfirmationRespondRequest):
    """响应工具确认请求"""
    return respond_tool_confirmation(confirm_id, request.approved)


@app.get("/api/tool-confirmation/workspace/{task_id:path}")
async def get_workspace_confirmation(task_id: str):
    """获取指定 workspace 的工具确认请求"""
    return get_tool_confirmation_for_workspace(task_id)


@app.get("/api/tool-confirmation/list")
async def get_all_confirmations():
    """列出所有工具确认请求"""
    return list_tool_confirmations()


def load_server_config() -> Tuple[str, int]:
    """
    从配置文件加载服务器地址和端口
    
    Returns:
        (host, port) 元组，失败时返回默认值 ("0.0.0.0", 8001)
    """
    try:
        import yaml
        config_path = Path(__file__).parent.parent / "config" / "run_env_config" / "tool_config.yaml"
        
        if not config_path.exists():
            # 配置文件不存在，静默使用默认值
            return "0.0.0.0", 8001
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        tools_server = config.get('tools_server', 'http://0.0.0.0:8001/')
        
        # 解析 URL
        parsed = urlparse(tools_server)
        
        # 提取 host（默认 0.0.0.0）
        host = parsed.hostname or "0.0.0.0"
        
        # 如果是 localhost 或 127.0.0.1，启动时使用 0.0.0.0 以监听所有接口
        # 这样既可以本地访问，也可以远程访问
        if host in ['localhost', '127.0.0.1']:
            host = "0.0.0.0"
        
        # 提取 port（默认 8001）
        port = parsed.port or 8001
        
        return host, port
    
    except Exception:
        # 配置文件读取失败，静默使用默认值
        return "0.0.0.0", 8001


def start_server(host: str = None, port: int = None):
    """启动服务器"""
    # 如果没有指定，从配置文件读取
    used_config = False
    if host is None or port is None:
        config_host, config_port = load_server_config()
        if host is None:
            host = config_host
            used_config = True
        if port is None:
            port = config_port
            used_config = True
    
    print(f"🚀 Starting Tool Server Lite on {host}:{port}")
    if used_config:
        print(f"📋 使用配置文件: config/run_env_config/tool_config.yaml")
    print(f"📚 Available tools: {len(TOOLS)}")
    print(f"🔗 API Docs: http://{host}:{port}/docs")
    
    uvicorn.run(app, host=host, port=port)


def get_server_pid() -> int:
    """获取服务器进程ID（跨平台）- 使用 psutil"""
    try:
        import psutil
        
        # 遍历所有进程查找tool_server
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = proc.info.get('cmdline')
                if not cmdline or len(cmdline) < 2:
                    continue
                
                # 检查是否是 Python 进程
                if 'python' not in proc.info.get('name', '').lower():
                    continue
                
                # 检查脚本路径是否包含 tool_server_lite/server.py
                script_path = cmdline[1] if len(cmdline) > 1 else ''
                if 'tool_server_lite' not in script_path or 'server.py' not in script_path:
                    continue
                
                # 检查命令行参数中是否包含管理命令（status/start/stop/restart）
                # 这些是在参数位置（cmdline[2:]）而不是路径中
                if any(cmd in cmdline[2:] for cmd in ['status', 'start', 'stop', 'restart']):
                    continue
                
                return proc.info['pid']
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except ImportError:
        # psutil 未安装，回退到简单方法
        pass
    except Exception:
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
    import signal
    import os
    
    try:
        import psutil
        # 使用 psutil 找到所有 tool_server 进程
        killed_pids = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = proc.info.get('cmdline')
                if not cmdline or len(cmdline) < 2:
                    continue
                
                if 'python' not in proc.info.get('name', '').lower():
                    continue
                
                script_path = cmdline[1] if len(cmdline) > 1 else ''
                if 'tool_server_lite' in script_path and 'server.py' in script_path:
                    # 排除管理命令（只检查参数部分 cmdline[2:]，避免误判路径中的关键词）
                    if any(cmd in cmdline[2:] for cmd in ['status', 'start', 'stop', 'restart']):
                        continue
                    
                    pid = proc.info['pid']
                    os.kill(pid, signal.SIGTERM)
                    killed_pids.append(pid)
                    print(f"✅ 已停止进程: {pid}")
            except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
                continue
        
        if killed_pids:
            print(f"✅ Tool Server 已停止（共 {len(killed_pids)} 个进程）")
        else:
            print("ℹ️  服务器未运行")
    
    except ImportError:
        # psutil 未安装，使用简单方法
        pid = get_server_pid()
        if not pid:
            print("ℹ️  服务器未运行")
            return
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"✅ Tool Server 已停止 (PID: {pid})")
        except Exception as e:
            print(f"❌ 停止失败: {e}")
    
    except Exception as e:
        print(f"❌ 停止失败: {e}")


def server_start_daemon(host=None, port=None):
    """后台启动服务器"""
    import subprocess
    import sys
    
    # 如果没有指定，从配置文件读取
    used_config = False
    if host is None or port is None:
        config_host, config_port = load_server_config()
        if host is None:
            host = config_host
            used_config = True
        if port is None:
            port = config_port
            used_config = True
    
    if used_config:
        print(f"📋 使用配置文件: config/run_env_config/tool_config.yaml")
        print(f"📍 服务器地址: {host}:{port}")
    
    if server_status():
        print("ℹ️  服务器已在运行")
        return
    
    # 创建日志文件
    log_file = Path(__file__).parent / "tool_server.log"
    
    # 后台启动
    try:
        log_handle = open(log_file, 'w', encoding='utf-8')
        
        if sys.platform == 'win32':
            # Windows: 使用DETACHED_PROCESS避免创建新窗口
            CREATE_NO_WINDOW = 0x08000000
            process = subprocess.Popen(
                [sys.executable, __file__, "--host", host, "--port", str(port)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
                close_fds=False  # Windows不关闭继承的句柄
            )
        else:
            # Unix/Linux/Mac: 使用标准后台启动
            process = subprocess.Popen(
                [sys.executable, __file__, "--host", host, "--port", str(port)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True
            )
        
        # 父进程关闭文件句柄，子进程已经继承了文件描述符
        log_handle.close()
        
        print(f"[INFO] 后台进程已启动 (PID: {process.pid})")
        print(f"[LOG] 日志文件: {log_file}")
    except Exception as e:
        print(f"❌ 启动失败: {e}")
        import traceback
        traceback.print_exc()
        return
    
    import time
    
    # 等待服务器启动（最多等待30秒，每2秒检查一次）
    print("⏳ 等待服务器启动...")
    max_retries = 15  # 30秒 / 2秒
    for i in range(max_retries):
        time.sleep(2)
        try:
            import requests
            response = requests.get(f"http://localhost:{port}/health", timeout=2)
            if response.status_code == 200:
                print("✅ Tool Server 已启动（后台）")
                print(f"   地址: http://localhost:{port}")
                return
        except:
            # 继续等待
            if i < max_retries - 1:
                print(f"   等待中... ({i+1}/{max_retries})")
    
    # 超时
    print(f"❌ 启动超时，请查看日志: {log_file}")


def main():
    """命令行入口"""
    import argparse
    
    # 从配置文件加载默认值
    default_host, default_port = load_server_config()
    
    parser = argparse.ArgumentParser(description="Tool Server Lite - 服务管理")
    parser.add_argument("command", nargs='?', default=None,
                       help="服务管理命令: start, stop, status, restart（不指定则前台运行）")
    parser.add_argument("--host", default=default_host, help=f"Host to bind (默认从配置文件读取: {default_host})")
    parser.add_argument("--port", default=default_port, type=int, help=f"Port to bind (默认从配置文件读取: {default_port})")
    
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

