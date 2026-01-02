#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MLA-V3 Web UI Server

Provides frontend interface and API endpoints.

Author: Songmiao Wang
MLA System: Chenlin Yu, Songmiao Wang
"""

import os
import sys
import json
import threading
import queue
import subprocess
import signal
import fcntl  # For file locking (Unix systems)
import yaml
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, Response, jsonify, session
from flask_cors import CORS

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Add server_dir to path (for importing output_capture)
server_dir = Path(__file__).parent
sys.path.insert(0, str(server_dir))

# OutputCapture is no longer used - we directly parse JSONL events
# from output_capture import OutputCapture

# Set template and static file paths (pointing to web_ui directory)
web_ui_dir = Path(__file__).parent.parent
app = Flask(__name__, 
            template_folder=str(web_ui_dir),
            static_folder=str(web_ui_dir / 'static'))
app.secret_key = 'mla-secret-key-2024'  # For session
CORS(app, supports_credentials=True)  # Support credentials (for session)

# Workspace root directory (all tasks are under this directory)
# 优先从环境变量读取，如果没有则使用当前工作目录（与 CLI 模式相同）
workspace_root_env = os.environ.get('WORKSPACE_ROOT')
if workspace_root_env:
    WORKSPACE_ROOT = Path(workspace_root_env)
else:
    # 默认使用当前工作目录（和 CLI 模式一样）
    WORKSPACE_ROOT = Path(os.getcwd())

# 确保目录存在
WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)

# Load user accounts
def load_users():
    """Load user accounts from users.yaml"""
    users_file = Path(__file__).parent / 'users.yaml'
    if users_file.exists():
        with open(users_file, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            return data.get('users', {})
    return {}

USER_ACCOUNTS = load_users()

# Global variable to store current execution state (per user)
# Format: {username: {'running': False, 'process': None, ...}}
current_executions = {}


def get_user_workspace(username: str) -> Path:
    """Get user-specific workspace root directory"""
    user_workspace = WORKSPACE_ROOT / username
    user_workspace.mkdir(parents=True, exist_ok=True)
    return user_workspace


def get_user_execution(username: str) -> dict:
    """Get or create user-specific execution state"""
    if username not in current_executions:
        current_executions[username] = {
            'running': False,
            'process': None,
            'output_queue': None,
            'stop_requested': False,
            'thread': None,
            'reader_thread': None,
            'sse_connections': set()  # Track active SSE connections
        }
    return current_executions[username]


def normalize_task_id(task_id: str, username: str = None) -> tuple[Path, str]:
    """
    Normalize task ID path (user-aware)
    
    Args:
        task_id: User input task ID (can be relative path or absolute path)
        username: Username for user-specific workspace isolation
    
    Returns:
        (absolute_path, display_path): 
        - absolute_path: Absolute path (under user workspace)
        - display_path: Display path to user (relative to user workspace)
    
    Raises:
        ValueError: If path is unsafe (contains .. or exceeds workspace root directory)
    """
    import os
    
    if not username:
        raise ValueError("Username is required for task isolation")
    
    # Get user-specific workspace
    user_workspace = get_user_workspace(username)
    
    # Remove leading/trailing spaces and slashes
    task_id = task_id.strip().strip('/')
    
    # If user input is absolute path, check if it's under user workspace
    if os.path.isabs(task_id):
        abs_path = Path(task_id)
        try:
            # Check if path is under user workspace
            abs_path.resolve().relative_to(user_workspace.resolve())
        except ValueError:
            raise ValueError(f"Path must be under user workspace directory ({user_workspace})")
    else:
        # Relative path: directly concatenate to user workspace
        abs_path = user_workspace / task_id
    
    # Normalize path (resolve .. and .)
    abs_path = abs_path.resolve()
    
    # Security check: ensure final path is under user workspace
    try:
        rel_path = abs_path.relative_to(user_workspace.resolve())
    except ValueError:
        raise ValueError(f"Path is unsafe: cannot exceed user workspace directory ({user_workspace})")
    
    # Check if contains .. (prevent path traversal)
    if '..' in rel_path.parts:
        raise ValueError("Path is unsafe: cannot contain '..'")
    
    # Return absolute path and display path
    display_path = str(rel_path) if rel_path != Path('.') else ''
    return abs_path, display_path


def normalize_file_path(path: str, task_id: str = None, username: str = None) -> Path:
    """
    Normalize file path (for file operations, user-aware)
    
    Args:
        path: File path (can be absolute or relative, relative paths are relative to user workspace)
        task_id: Task ID (if path is relative and needs to be relative to task directory, provide this)
        username: Username for user-specific workspace isolation
    
    Returns:
        Absolute path (under user workspace)
    
    Raises:
        ValueError: If path is unsafe
    """
    import os
    
    if not username:
        raise ValueError("Username is required for file path isolation")
    
    user_workspace = get_user_workspace(username)
    path = path.strip()
    
    # If absolute path, check if it's under user workspace
    if os.path.isabs(path):
        abs_path = Path(path).resolve()
        try:
            abs_path.relative_to(user_workspace.resolve())
        except ValueError:
            raise ValueError(f"Path must be under user workspace directory ({user_workspace})")
    else:
        # Relative path: relative to user workspace or task directory
        if task_id:
            # Relative to task directory
            _, _ = normalize_task_id(task_id, username)  # Validate task_id
            abs_path = (user_workspace / task_id / path).resolve()
        else:
            # Relative to user workspace root
            abs_path = (user_workspace / path).resolve()
    
    # Final security check
    try:
        abs_path.relative_to(user_workspace.resolve())
    except ValueError:
        raise ValueError(f"Path is unsafe: cannot exceed user workspace directory")
    
    return abs_path


# Login verification decorator
def login_required(f):
    """Login verification decorator"""
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('logged_in'):
            return jsonify({"error": "Login required"}), 401
        return f(*args, **kwargs)
    return decorated_function


def run_agent_task(task_id: str, agent_name: str, user_input: str, 
                   agent_system: str, output_queue: queue.Queue, username: str = None):
    """
    Run agent task in background thread
    
    Args:
        task_id: Task ID
        agent_name: Agent name
        user_input: User input
        agent_system: Agent system name
        output_queue: Output queue
        username: Username for execution state isolation
    """
    try:
        # Get user-specific execution state
        if not username:
            raise ValueError("Username is required for task execution")
        user_execution = get_user_execution(username)
        
        # Check stop flag
        if user_execution.get('stop_requested', False):
            send_message({
                "type": "error",
                "agent": agent_name,
                "content": "⏹️ Task stopped by user",
                "timestamp": None
            })
            output_queue.put(None)
            return
        # Import necessary modules
        from utils.config_loader import ConfigLoader
        from core.hierarchy_manager import get_hierarchy_manager
        from core.agent_executor import AgentExecutor
        from utils.event_emitter import init_event_emitter
        
        # Create output capture (deprecated - this function is not used)
        def send_message(msg):
            output_queue.put(msg)
        
        # OutputCapture is no longer used - this function is deprecated
        # capture = OutputCapture(send_message, agent_name)
        # capture.start()
        
        try:
            # Send start message
            send_message({
                "type": "start",
                "agent": agent_name,
                "content": f"🚀 Start task: {user_input}",
                "timestamp": None
            })
            
            # Initialize config loader
            send_message({
                "type": "info",
                "agent": agent_name,
                "content": "📦 Loading config...",
                "timestamp": None
            })
            
            config_loader = ConfigLoader(agent_system)
            
            send_message({
                "type": "info",
                "agent": agent_name,
                "content": f"✅ Configuration loaded successfully, {len(config_loader.all_tools)} tools/Agents",
                "timestamp": None
            })
            
            # Initialize hierarchy manager
            send_message({
                "type": "info",
                "agent": agent_name,
                "content": "📊 Initializing hierarchy manager...",
                "timestamp": None
            })
            
            hierarchy_manager = get_hierarchy_manager(task_id)
            
            send_message({
                "type": "info",
                "agent": agent_name,
                "content": "✅ Hierarchy manager initialized successfully",
                "timestamp": None
            })
            
            # Clean state
            send_message({
                "type": "info",
                "agent": agent_name,
                "content": "🧹 Checking and cleaning state...",
                "timestamp": None
            })
            
            from core.state_cleaner import clean_before_start
            clean_before_start(task_id, user_input)
            
            # Register user instruction
            instruction_id = hierarchy_manager.start_new_instruction(user_input)
            
            send_message({
                "type": "info",
                "agent": agent_name,
                "content": f"✅ Instruction registered: {instruction_id}",
                "timestamp": None
            })
            
            # Get Agent config
            agent_config = config_loader.get_tool_config(agent_name)
            
            if agent_config.get("type") != "llm_call_agent":
                send_message({
                    "type": "error",
                    "agent": agent_name,
                    "content": f"❌ Error: {agent_name} is not a LLM Agent",
                    "timestamp": None
                })
                return
            
            send_message({
                "type": "info",
                "agent": agent_name,
                "content": f"✅ Agent configuration loaded successfully (Level: {agent_config.get('level', 'unknown')})",
                "timestamp": None
            })
            
            # Create and run Agent
            send_message({
                "type": "info",
                "agent": agent_name,
                "content": "▶️ Start executing task",
                "timestamp": None
            })
            
            # Check stop flag
            if user_execution.get('stop_requested', False):
                send_message({
                    "type": "error",
                    "agent": agent_name,
                    "content": "⏹️ Task has been stopped by user",
                    "timestamp": None
                })
                output_queue.put(None)
                return
            
            agent = AgentExecutor(
                agent_name=agent_name,
                agent_config=agent_config,
                config_loader=config_loader,
                hierarchy_manager=hierarchy_manager
            )
            
            # Update captured agent name (deprecated)
            # capture.set_agent(agent_name)
            
            result = agent.run(task_id, user_input)
            
            # Check stop flag again after execution
            if user_execution.get('stop_requested', False):
                send_message({
                    "type": "error",
                    "agent": agent_name,
                    "content": "⏹️ Task stopped by user",
                    "timestamp": None
                })
                output_queue.put(None)
                return
            
            # Send result
            status = result.get('status', 'unknown')
            output = result.get('output', '')
            error_info = result.get('error_information', '')
            
            send_message({
                "type": "result",
                "agent": agent_name,
                "content": f"📊 Execution result:\nStatus: {status}\nOutput: {output}\n" + (f"Error: {error_info}" if error_info else ""),
                "timestamp": None
            })
            
            send_message({
                "type": "end",
                "agent": agent_name,
                "content": f"{'✅' if status == 'success' else '❌'} Task completed",
                "timestamp": None
            })
            
        finally:
            # capture.stop()  # Deprecated - OutputCapture no longer used
            output_queue.put(None)  # End marker
            
    except Exception as e:
        import traceback
        error_msg = f"❌ Execution failed: {str(e)}\n{traceback.format_exc()}"
        output_queue.put({
            "type": "error",
            "agent": agent_name,
            "content": error_msg,
            "timestamp": None
        })
        output_queue.put(None)  # End marker


@app.route('/')
def index():
    """Home page"""
    # If not logged in, redirect to login page
    if not session.get('logged_in'):
        return render_template('login.html')
    return render_template('index.html')


@app.route('/api/login', methods=['POST'])
def login():
    """Login verification"""
    try:
        data = request.json
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        
        # Verify username and password
        if username in USER_ACCOUNTS and USER_ACCOUNTS[username] == password:
            session['logged_in'] = True
            session['username'] = username
            # Initialize user workspace
            get_user_workspace(username)
            return jsonify({
                "success": True,
                "message": "Login successful"
            })
        else:
            return jsonify({
                "error": "Incorrect username or password"
            }), 401
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/logout', methods=['POST'])
def logout():
    """Logout"""
    session.clear()
    return jsonify({
        "success": True,
        "message": "Logged out"
    })


@app.route('/api/check-auth', methods=['GET'])
def check_auth():
    """Check login status"""
    if session.get('logged_in'):
        return jsonify({
            "logged_in": True,
            "username": session.get('username', '')
        })
    else:
        return jsonify({
            "logged_in": False
        })


@app.route('/api/run', methods=['POST'])
@login_required
def run_task():
    """Run agent task - SSE streaming output"""
    data = request.json
    
    username = session.get('username')
    if not username:
        return jsonify({"error": "User not authenticated"}), 401
    
    task_id_input = data.get('task_id')
    agent_name = data.get('agent_name', 'alpha_agent')
    user_input = data.get('user_input')
    agent_system = data.get('agent_system', 'Default')
    
    if not task_id_input or not user_input:
        return jsonify({"error": "Missing required parameters"}), 400
    
    # Normalize task ID path (user-aware)
    try:
        task_path, _ = normalize_task_id(task_id_input, username)
        task_id_absolute = str(task_path)  # Use absolute path to pass to start.py
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    
    # Get user-specific execution state
    user_execution = get_user_execution(username)
    
    # Check if task is already running for this user
    if user_execution['running']:
        return jsonify({"error": "Task already running"}), 409
    
    # Create output queue
    output_queue = queue.Queue()
    user_execution['output_queue'] = output_queue
    user_execution['running'] = True
    user_execution['stop_requested'] = False
    
    # Get path to start.py
    start_script = project_root / 'start.py'
    
    # Start subprocess to run start.py (using absolute path)
    process = subprocess.Popen(
        [
            sys.executable,
            str(start_script),
            '--task_id', task_id_absolute,
            '--agent_name', agent_name,
            '--user_input', user_input,
            '--agent_system', agent_system,
            '--jsonl'  # Use JSONL mode to parse output
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # Merge stderr to stdout
        text=True,
        bufsize=1,
        universal_newlines=True
    )
    
    user_execution['process'] = process
    
    # Helper function to convert JSONL event to frontend format
    def convert_event_to_frontend_format(event, default_agent_name):
        """Convert JSONL event to frontend message format"""
        event_type = event.get("type", "token")
        agent = event.get("agent", default_agent_name)
        
        frontend_event = {
            "type": event_type,
            "agent": agent,
            "content": "",
            "timestamp": datetime.now().isoformat()
        }
        
        # Map event types to frontend format
        if event_type == "tool_call":
            # 结构化工具调用事件
            tool_name = event.get("tool_name", "unknown")
            parameters = event.get("parameters", {})
            import json
            params_str = json.dumps(parameters, ensure_ascii=False, indent=2)
            frontend_event["type"] = "tool_call"
            frontend_event["content"] = f"🔧 [{default_agent_name}] calls tool: {tool_name}\n\n📋 Parameters:\n{params_str}"
            
        elif event_type == "agent_call":
            # 结构化子 Agent 调用事件
            agent_name = event.get("agent_name", "unknown")
            parameters = event.get("parameters", {})
            import json
            params_str = json.dumps(parameters, ensure_ascii=False, indent=2)
            frontend_event["type"] = "agent_call"
            frontend_event["content"] = f"🤖 [{default_agent_name}] calls sub-agent: {agent_name}\n\n📋 Parameters:\n{params_str}"
            
        elif event_type == "token":
            text = event.get("text", "")
            if not text.strip():
                return None  # Skip empty content
            
            # Filter initialization messages (不需要在前端显示)
            if "加载配置" in text or "配置加载成功" in text:
                return None  # Skip config loading messages
            
            # Check if this is a tool call text (backward compatibility - 保持向后兼容)
            if "调用工具:" in text and "参数:" in text:
                # Parse tool call text (兼容旧的文本格式)
                lines_split = text.split('\n')
                tool_line = lines_split[0] if lines_split else ""
                params_text = '\n'.join(lines_split[1:]) if len(lines_split) > 1 else ""
                
                # Extract tool name
                import re
                tool_match = re.search(r'调用工具:\s*(\w+)', tool_line)
                tool_name = tool_match.group(1) if tool_match else "unknown"
                
                frontend_event["type"] = "tool_call"
                frontend_event["content"] = f"🔧 [{default_agent_name}] calls tool: {tool_name}\n\n📋 Parameters:\n{params_text}"
            elif "调用子Agent:" in text and "参数:" in text:
                # 兼容旧的子 Agent 调用文本格式
                lines_split = text.split('\n')
                agent_line = lines_split[0] if lines_split else ""
                params_text = '\n'.join(lines_split[1:]) if len(lines_split) > 1 else ""
                
                import re
                agent_match = re.search(r'调用子Agent:\s*(\w+)', agent_line)
                agent_name = agent_match.group(1) if agent_match else "unknown"
                
                frontend_event["type"] = "agent_call"
                frontend_event["content"] = f"🤖 [{default_agent_name}] calls sub-agent: {agent_name}\n\n📋 Parameters:\n{params_text}"
            else:
                # Regular text information
                frontend_event["type"] = "info"
                frontend_event["content"] = text
                
        elif event_type == "start":
            frontend_event["type"] = "start"
            frontend_event["agent"] = event.get("agent", default_agent_name)
            frontend_event["content"] = f"🚀 任务开始: {event.get('task', '')}"
            
        elif event_type == "progress":
            # Filter initialization progress updates (不需要在前端显示)
            phase = event.get("phase", "")
            if phase == "init":
                return None  # Skip init phase progress updates
            
            pct = event.get("pct", 0)
            frontend_event["type"] = "info"
            frontend_event["content"] = f"📊 进度更新: {phase} ({pct}%)"
            
        elif event_type == "notice":
            frontend_event["type"] = "info"
            frontend_event["content"] = f"ℹ️ {event.get('text', '')}"
            
        elif event_type == "warn":
            frontend_event["type"] = "info"
            frontend_event["content"] = f"⚠️ {event.get('text', '')}"
            
        elif event_type == "error":
            frontend_event["type"] = "error"
            frontend_event["content"] = f"❌ {event.get('text', '')}"
            
        elif event_type == "result":
            summary = event.get("summary", "")
            ok = event.get("ok", False)
            icon = "✅" if ok else "❌"
            frontend_event["type"] = "info"
            frontend_event["content"] = f"{icon} 执行结果: {summary}"
            
        elif event_type == "end":
            status = event.get("status", "unknown")
            duration_ms = event.get("duration_ms", 0)
            duration = duration_ms / 1000 if duration_ms else 0
            icon = "✅" if status == "ok" else "❌"
            frontend_event["type"] = "end"
            frontend_event["content"] = f"{icon} 任务完成 ({duration:.1f}秒)"
            
        else:
            # Unknown event type, try to extract text field
            text = event.get("text", "")
            if text:
                frontend_event["type"] = "info"
                frontend_event["content"] = text
            else:
                # Skip events without content
                return None
        
        return frontend_event if frontend_event.get("content") else None
    
    # Read process output in background thread
    def read_process_output():
        """Read subprocess output - directly parse JSONL events"""
        end_event_received = False
        try:
            # Read JSONL event stream directly
            # Note: start event will come from JSONL stream (emitted by start.py)
            buffer = ""  # For handling multi-line JSON (though JSONL is usually one JSON per line)
            for line in process.stdout:
                if user_execution.get('stop_requested', False):
                    break
                
                # Handle possible multi-line JSON (though JSONL is usually one line per JSON)
                buffer += line
                if not line.endswith('\n'):
                    continue  # Continue accumulating
                
                # Try to parse each line
                lines = buffer.split('\n')
                buffer = lines[-1]  # Keep the last incomplete line
                
                for json_line in lines[:-1]:
                    json_line = json_line.strip()
                    if not json_line:
                        continue
                    
                    try:
                        # Parse JSONL event
                        event = json.loads(json_line)
                        
                        # Track if end event was received
                        if event.get("type") == "end":
                            end_event_received = True
                        
                        # Convert to frontend format
                        frontend_event = convert_event_to_frontend_format(event, agent_name)
                        
                        # Send event if valid
                        if frontend_event:
                            output_queue.put(frontend_event)
                            
                    except json.JSONDecodeError:
                        # Non-JSON line, may be error output
                        json_line = json_line.strip()
                        if json_line and ("Error" in json_line or "Exception" in json_line):
                            # Only handle obvious error information
                            output_queue.put({
                                "type": "error",
                                "agent": agent_name,
                                "content": f"❌ {json_line}",
                                "timestamp": datetime.now().isoformat()
                            })
                        continue
            
            # Process remaining buffer
            if buffer.strip():
                try:
                    event = json.loads(buffer.strip())
                    if event.get("type") == "end":
                        end_event_received = True
                    frontend_event = convert_event_to_frontend_format(event, agent_name)
                    if frontend_event:
                        output_queue.put(frontend_event)
                except json.JSONDecodeError:
                    pass
                
                # Wait for process to end
                process.wait()
                
            # Send end message if not already received
            if not end_event_received:
                if user_execution.get('stop_requested', False):
                    output_queue.put({
                        "type": "error",
                        "agent": agent_name,
                        "content": "⏹️ 任务已停止",
                        "timestamp": datetime.now().isoformat()
                    })
                elif process.returncode == 0:
                    output_queue.put({
                        "type": "end",
                        "agent": agent_name,
                        "content": "✅ 任务完成",
                        "timestamp": datetime.now().isoformat()
                    })
                else:
                    output_queue.put({
                        "type": "error",
                        "agent": agent_name,
                        "content": f"⚠️ 进程退出码: {process.returncode}",
                        "timestamp": datetime.now().isoformat()
                    })
            
                output_queue.put(None)  # End marker
            
        except Exception as e:
            import traceback
            error_detail = f"{str(e)}\n{traceback.format_exc()}"
            output_queue.put({
                "type": "error",
                "agent": agent_name,
                "content": f"❌ 读取输出异常: {str(e)}",
                "timestamp": datetime.now().isoformat()
            })
            output_queue.put(None)
        finally:
            user_execution['running'] = False
    
    reader_thread = threading.Thread(target=read_process_output, daemon=True)
    user_execution['reader_thread'] = reader_thread
    reader_thread.start()
    
    # Track this SSE connection
    import uuid
    connection_id = str(uuid.uuid4())
    user_execution['sse_connections'].add(connection_id)
    
    def generate():
        """Generate SSE event stream"""
        try:
            while True:
                try:
                    # Get message from queue (1 second timeout)
                    msg = output_queue.get(timeout=1)
                    
                    if msg is None:  # End marker
                        yield f"data: {json.dumps({'type': 'end', 'content': 'Task completed'}, ensure_ascii=False)}\n\n"
                        break
                    
                    # Add timestamp
                    if msg.get('timestamp') is None:
                        from datetime import datetime
                        msg['timestamp'] = datetime.now().isoformat()
                    
                    # Send SSE event
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                    
                except queue.Empty:
                    # Timeout, send heartbeat
                    yield f": heartbeat\n\n"
                    continue
                    
        except GeneratorExit:
            # Client disconnected (e.g., page refresh or new window)
            # Remove this connection from active connections
            user_execution['sse_connections'].discard(connection_id)
            
            # Only stop process if no active connections remain
            if len(user_execution['sse_connections']) == 0:
                process = user_execution.get('process')
                if process and process.poll() is None:  # Process still running
                    try:
                        user_execution['stop_requested'] = True
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                    except:
                        pass
                    finally:
                        user_execution['running'] = False
                        user_execution['process'] = None
                        user_execution['reader_thread'] = None
                        user_execution['stop_requested'] = False
        finally:
            # Remove connection on normal exit
            user_execution['sse_connections'].discard(connection_id)
            
            # Only mark as not running if no active connections remain
            if len(user_execution['sse_connections']) == 0:
                user_execution['running'] = False
                user_execution['stop_requested'] = False
    
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )


@app.route('/api/task/confirm', methods=['POST'])
@login_required
def confirm_task():
    """Confirm task ID, create if not exists"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        # Get user-specific execution state
        user_execution = get_user_execution(username)
        
        # Auto cleanup: if task is running, stop it first (prevent process residue after refresh)
        if user_execution.get('running'):
            process = user_execution.get('process')
            if process and process.poll() is None:  # Process still running
                try:
                    # Set stop flag
                    user_execution['stop_requested'] = True
                    # Terminate process
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                except Exception as e:
                    # If termination fails, try force kill
                    try:
                        process.kill()
                        process.wait()
                    except:
                        pass
                finally:
                    # Clean state
                    user_execution['running'] = False
                    user_execution['process'] = None
                    user_execution['reader_thread'] = None
        
        data = request.json
        task_id_input = data.get('task_id', '').strip()
        
        if not task_id_input:
            return jsonify({"error": "Missing task_id parameter"}), 400
        
        # Normalize task ID path (user-aware)
        try:
            task_path, display_path = normalize_task_id(task_id_input, username)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        
        is_new = False
        
        if task_path.exists():
            if not task_path.is_dir():
                return jsonify({"error": "Path exists but is not a directory"}), 400
            # Directory already exists
            is_new = False
        else:
            # Create new directory
            try:
                task_path.mkdir(parents=True, exist_ok=True)
                # Create necessary subdirectories
                (task_path / 'code_env').mkdir(exist_ok=True)
                (task_path / 'code_run').mkdir(exist_ok=True)
                (task_path / 'conversations').mkdir(exist_ok=True)
                (task_path / 'files').mkdir(exist_ok=True)
                is_new = True
            except Exception as e:
                return jsonify({"error": f"Create directory failed: {str(e)}"}), 500
        
        # Return display path (relative to workspace root)
        return jsonify({
            "success": True,
            "task_id": display_path if display_path else '/',
            "task_id_absolute": str(task_path),  # Internal use
            "is_new": is_new,
            "message": "New task created" if is_new else "Entered existing task"
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/stop', methods=['POST'])
@login_required
def stop_task():
    """Stop currently running task"""
    username = session.get('username')
    if not username:
        return jsonify({"error": "User not authenticated"}), 401
    
    user_execution = get_user_execution(username)
    
    if not user_execution['running']:
        return jsonify({"error": "No task is running"}), 400
    
    process = user_execution.get('process')
    if not process:
        return jsonify({"error": "Process object does not exist"}), 400
    
    try:
        # Set stop flag
        user_execution['stop_requested'] = True
        
        # Terminate process
        if process.poll() is None:  # Process still running
            try:
                # Try graceful termination first
                process.terminate()
                # Wait up to 2 seconds
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    # If not terminated after 2 seconds, force kill
                    process.kill()
                    process.wait()
            except Exception as e:
                # If termination fails, try force kill
                try:
                    process.kill()
                    process.wait()
                except:
                    pass
        
        user_execution['running'] = False
        
        return jsonify({
            "success": True,
            "message": "Task stopped"
        })
    except Exception as e:
        return jsonify({
            "error": f"Stop failed: {str(e)}"
        }), 500


@app.route('/api/agents', methods=['GET'])
@login_required
def get_agents():
    """Get available agent list"""
    try:
        agent_system = request.args.get('agent_system', 'Default')
        
        from utils.config_loader import ConfigLoader
        config_loader = ConfigLoader(agent_system)
        
        agents = []
        for name, config in config_loader.all_tools.items():
            if config.get("type") == "llm_call_agent":
                level = config.get("level", 0)
                agents.append({
                    "name": name,
                    "level": level,
                    "description": config.get("description", "")
                })
        
        # Sort by level
        agents.sort(key=lambda x: x["level"])
        
        return jsonify({"agents": agents})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/status', methods=['GET'])
@login_required
def get_status():
    """Get current execution status"""
    username = session.get('username')
    if not username:
        return jsonify({"error": "User not authenticated"}), 401
    
    user_execution = get_user_execution(username)
    process = user_execution.get('process')
    is_running = user_execution.get('running', False)
    
    # Check if process is really running
    if is_running and process:
        if process.poll() is not None:
            # Process ended but state not cleaned
            is_running = False
            user_execution['running'] = False
            user_execution['process'] = None
    
    return jsonify({
        "running": is_running,
        "has_process": process is not None
    })


@app.route('/api/tasks/list', methods=['GET'])
@login_required
def list_tasks():
    """Get all task directories under workspace root"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        tasks = []
        
        # Get user-specific workspace
        user_workspace = get_user_workspace(username)
        
        if not user_workspace.exists():
            return jsonify({"tasks": []})
        
        # Iterate all directories under user workspace
        for item in sorted(user_workspace.iterdir()):
            # Skip hidden files and files (only show directories)
            if item.name.startswith('.') or not item.is_dir():
                continue
            
            # Calculate relative path (for display)
            try:
                rel_path = item.relative_to(user_workspace)
                display_path = str(rel_path) if rel_path != Path('.') else ''
            except ValueError:
                display_path = item.name
            
            tasks.append({
                "name": item.name,
                "path": display_path,  # Relative path (for frontend display)
                "path_absolute": str(item)  # Absolute path (internal use)
            })
        
        return jsonify({"tasks": tasks})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/files/list', methods=['GET'])
@login_required
def list_files():
    """Get file list under specified path"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        path = request.args.get('path', '')
        task_id = request.args.get('task_id', '')
        if not path:
            return jsonify({"error": "Missing path parameter"}), 400
        
        # Normalize path (limited to user workspace directory)
        try:
            path_obj = normalize_file_path(path, task_id=task_id if task_id else None, username=username)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        
        if not path_obj.exists():
            return jsonify({"error": "Path does not exist"}), 404
        
        if not path_obj.is_dir():
            return jsonify({"error": "Path is not a directory"}), 400
        
        # Get user workspace for relative path calculation
        user_workspace = get_user_workspace(username)
        
        # Calculate relative path (for display)
        try:
            rel_path = path_obj.relative_to(user_workspace)
            display_path = str(rel_path) if rel_path != Path('.') else ''
        except ValueError:
            display_path = str(path_obj)
        
        files = []
        for item in sorted(path_obj.iterdir()):
            # Skip hidden files and special directories
            if item.name.startswith('.'):
                continue
            
            # Skip chat_history.json and conversations folder (hidden from users)
            # if item.name == 'chat_history.json' or item.name == 'conversations':
            #     continue
            
            # Calculate file relative path (for display)
            try:
                item_rel = item.relative_to(user_workspace)
                item_display_path = str(item_rel) if item_rel != Path('.') else ''
            except ValueError:
                item_display_path = str(item)
            
            files.append({
                "name": item.name,
                "path": item_display_path,  # Return relative path (for frontend display)
                "path_absolute": str(item),  # Absolute path for internal use
                "type": "directory" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else 0
            })
        
        return jsonify({
            "files": files,
            "path": display_path  # Current path (relative path)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/files/read', methods=['GET'])
@login_required
def read_file():
    """Read file content"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        path = request.args.get('path', '')
        task_id = request.args.get('task_id', '')
        if not path:
            return jsonify({"error": "Missing path parameter"}), 400
        
        # Normalize path (limited to user workspace directory)
        try:
            path_obj = normalize_file_path(path, task_id=task_id if task_id else None, username=username)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        
        if not path_obj.exists():
            return jsonify({"error": "File not found"}), 404
        
        if not path_obj.is_file():
            return jsonify({"error": "Path is not a file"}), 400
        
        # Try to read file
        try:
            content = path_obj.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            # If not text file, return binary hint
            return jsonify({
                "error": "Cannot read file as text (may be binary file)",
                "size": path_obj.stat().st_size
            }), 400
        
        # Get user workspace for relative path calculation
        user_workspace = get_user_workspace(username)
        
        # Calculate relative path (for display)
        try:
            rel_path = path_obj.relative_to(user_workspace)
            display_path = str(rel_path) if rel_path != Path('.') else ''
        except ValueError:
            display_path = str(path_obj)
        
        return jsonify({
            "content": content,
            "path": display_path,  # Return relative path (for frontend display)
            "size": path_obj.stat().st_size
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/task/clear', methods=['POST'])
@login_required
def clear_task():
    """Clear task directory and all its files"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        user_execution = get_user_execution(username)
        
        # Auto cleanup: if task is running, stop it first
        if user_execution.get('running'):
            process = user_execution.get('process')
            if process and process.poll() is None:  # Process still running
                try:
                    user_execution['stop_requested'] = True
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                except Exception as e:
                    try:
                        process.kill()
                        process.wait()
                    except:
                        pass
                finally:
                    user_execution['running'] = False
                    user_execution['process'] = None
                    user_execution['reader_thread'] = None
        
        data = request.json
        task_id_input = data.get('task_id', '').strip()
        
        if not task_id_input:
            return jsonify({"error": "Missing task_id parameter"}), 400
        
        # Normalize task ID path (limited to user workspace)
        try:
            task_path, display_path = normalize_task_id(task_id_input, username=username)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        
        if not task_path.exists():
            return jsonify({"error": "Task directory does not exist"}), 404
        
        if not task_path.is_dir():
            return jsonify({"error": "Path is not a directory"}), 400
        
        # Recursively delete entire directory
        import shutil
        shutil.rmtree(task_path)
        
        # Also delete corresponding conversation files in home directory
        # Generate task_hash and task_folder same way as hierarchy_manager
        # Use absolute path (task_path) to ensure hash matches what was used during storage
        import hashlib
        import os
        from pathlib import Path
        
        task_id_for_hash = str(task_path)  # Use absolute path for consistent hashing
        task_hash = hashlib.md5(task_id_for_hash.encode()).hexdigest()[:8]
        task_folder = Path(task_id_for_hash).name if (os.sep in task_id_for_hash or '/' in task_id_for_hash or '\\' in task_id_for_hash) else task_id_for_hash
        task_name = f"{task_hash}_{task_folder}"
        
        # Delete all files matching the pattern in home conversations directory
        conversations_dir = Path.home() / "mla_v3" / "conversations"
        if conversations_dir.exists():
            deleted_files = []
            # Pattern: {task_hash}_{task_folder}_*.json
            pattern = f"{task_name}_*.json"
            for file_path in conversations_dir.glob(pattern):
                try:
                    file_path.unlink()
                    deleted_files.append(file_path.name)
                except Exception as e:
                    # Log but don't fail if file deletion fails
                    print(f"⚠️ 删除对话历史文件失败: {file_path.name} - {e}")
            
            if deleted_files:
                print(f"✅ 已删除主目录下的对话历史文件: {len(deleted_files)} 个文件")
        
        return jsonify({
            "success": True,
            "message": f"task {display_path if display_path else '/'} and all its files have been cleared"
        })
    except Exception as e:
        import traceback
        return jsonify({"error": f"Clear failed: {str(e)}\n{traceback.format_exc()}"}), 500


# Global copy progress tracking (per user)
copy_progress = {}
copy_progress_lock = threading.Lock()


def get_copy_progress(username: str, task_id: str) -> dict:
    """Get copy progress for a task"""
    with copy_progress_lock:
        key = f"{username}:{task_id}"
        return copy_progress.get(key, {"status": "none", "progress": 0, "message": ""})


def set_copy_progress(username: str, task_id: str, status: str, progress: int, message: str = ""):
    """Set copy progress for a task"""
    with copy_progress_lock:
        key = f"{username}:{task_id}"
        copy_progress[key] = {"status": status, "progress": progress, "message": message}


def clear_copy_progress(username: str, task_id: str):
    """Clear copy progress for a task"""
    with copy_progress_lock:
        key = f"{username}:{task_id}"
        if key in copy_progress:
            del copy_progress[key]


def copy_tree_with_progress(src: Path, dst: Path, username: str, task_id: str):
    """Copy directory tree with progress tracking"""
    import shutil
    
    # Count total files first
    total_files = 0
    for root, dirs, files in os.walk(src):
        total_files += len(files)
    
    if total_files == 0:
        total_files = 1  # Avoid division by zero
    
    copied_files = 0
    
    # Create destination directory
    dst.mkdir(parents=True, exist_ok=True)
    
    try:
        set_copy_progress(username, task_id, "copying", 0, f"Starting copy: {total_files} files to copy")
        
        # Walk through source directory and copy files
        for root, dirs, files in os.walk(src):
            # Calculate relative path
            rel_path = Path(root).relative_to(src)
            dst_dir = dst / rel_path
            dst_dir.mkdir(parents=True, exist_ok=True)
            
            # Copy files
            for file in files:
                src_file = Path(root) / file
                dst_file = dst_dir / file
                shutil.copy2(src_file, dst_file)
                copied_files += 1
                progress = int((copied_files / total_files) * 100)
                set_copy_progress(username, task_id, "copying", progress, 
                                 f"Copying files: {copied_files}/{total_files}")
        
        set_copy_progress(username, task_id, "completed", 100, f"Copy completed: {total_files} files copied")
    except Exception as e:
        set_copy_progress(username, task_id, "error", 0, f"Copy failed: {str(e)}")
        raise


@app.route('/api/task/copy', methods=['POST'])
@login_required
def copy_task():
    """Copy task workspace to a new task"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        user_execution = get_user_execution(username)
        
        # Stop running task if any
        if user_execution.get('running'):
            process = user_execution.get('process')
            if process and process.poll() is None:
                try:
                    user_execution['stop_requested'] = True
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                except Exception as e:
                    try:
                        process.kill()
                        process.wait()
                    except:
                        pass
                finally:
                    user_execution['running'] = False
                    user_execution['process'] = None
                    user_execution['reader_thread'] = None
        
        data = request.json
        source_task_id = data.get('source_task_id', '').strip()
        target_task_id = data.get('target_task_id', '').strip()
        
        if not source_task_id or not target_task_id:
            return jsonify({"error": "Missing source_task_id or target_task_id"}), 400
        
        # Normalize paths
        try:
            source_path, _ = normalize_task_id(source_task_id, username)
            target_path, target_display = normalize_task_id(target_task_id, username)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        
        # Check if source task exists
        if not source_path.exists():
            return jsonify({"error": "Source task does not exist"}), 404
        
        if not source_path.is_dir():
            return jsonify({"error": "Source path is not a directory"}), 400
        
        # Check if target task already exists
        if target_path.exists():
            return jsonify({"error": "Target task already exists"}), 409
        
        # Initialize progress
        set_copy_progress(username, target_task_id, "starting", 0, "Preparing to copy...")
        
        # Copy directory in background thread
        def copy_in_thread():
            try:
                copy_tree_with_progress(source_path, target_path, username, target_task_id)
            except Exception as e:
                import traceback
                set_copy_progress(username, target_task_id, "error", 0, 
                                f"Copy failed: {str(e)}\n{traceback.format_exc()}")
        
        copy_thread = threading.Thread(target=copy_in_thread, daemon=True)
        copy_thread.start()
        
        return jsonify({
            "success": True,
            "task_id": target_display,
            "task_id_absolute": str(target_path),
            "message": f"Copy started for task {target_display}"
        })
    except Exception as e:
        import traceback
        return jsonify({"error": f"Copy failed: {str(e)}\n{traceback.format_exc()}"}), 500


@app.route('/api/task/copy/progress', methods=['GET'])
@login_required
def get_copy_progress_api():
    """Get copy progress for a task"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        task_id = request.args.get('task_id', '').strip()
        if not task_id:
            return jsonify({"error": "Missing task_id parameter"}), 400
        
        progress = get_copy_progress(username, task_id)
        return jsonify(progress)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/task/download', methods=['GET'])
@login_required
def download_task():
    """Download entire task directory as ZIP archive"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        task_id = request.args.get('task_id', '').strip()
        if not task_id:
            return jsonify({"error": "Missing task_id parameter"}), 400
        
        # Normalize task path
        try:
            task_path, display_path = normalize_task_id(task_id, username)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        
        if not task_path.exists():
            return jsonify({"error": "Task directory does not exist"}), 404
        
        if not task_path.is_dir():
            return jsonify({"error": "Path is not a directory"}), 400
        
        # Create ZIP file in memory
        import zipfile
        import io
        from flask import send_file
        
        # Create in-memory ZIP file
        zip_buffer = io.BytesIO()
        
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            # Collect empty directories and files to include
            empty_directories = set()
            files_to_add = []
            directories_with_files = set()  # Track directories that contain files
            
            # Walk through all files in task directory
            for root, dirs, files in os.walk(task_path):
                # Calculate relative path for current directory
                rel_root = Path(root).relative_to(task_path)
                rel_root_str = str(rel_root)
                
                # # Skip conversations directory and all its subdirectories
                # if 'conversations' in rel_root_str.split(os.sep):
                #     # Remove conversations from dirs to prevent further traversal
                #     if 'conversations' in dirs:
                #         dirs.remove('conversations')
                #     continue
                
                # # Remove conversations directory from dirs to skip it during walk
                # if 'conversations' in dirs:
                #     dirs.remove('conversations')
                
                # Check if directory has any valid files (excluding chat_history.json)
                valid_files = [f for f in files if f != 'chat_history.json']
                
                if not valid_files:
                    # Directory is empty (or only contains chat_history.json)
                    if rel_root != Path('.'):
                        empty_directories.add(rel_root_str)
                else:
                    # Directory has files, mark it and its parents
                    directories_with_files.add(rel_root_str)
                    for parent in rel_root.parents:
                        if parent != Path('.'):
                            directories_with_files.add(str(parent))
                
                # Process files
                for file in files:
                    # Skip chat_history.json file
                    if file == 'chat_history.json':
                        continue
                    
                    file_path = Path(root) / file
                    # Calculate relative path from task directory
                    arcname = file_path.relative_to(task_path)
                    files_to_add.append((file_path, arcname))
            
            # Add empty directories (excluding those that will be created by files)
            for dir_path in sorted(empty_directories):
                if dir_path not in directories_with_files:
                    # Create empty directory entry by adding a path ending with /
                    zip_file.writestr(dir_path + '/', b'')
            
            # Add all files
            for file_path, arcname in files_to_add:
                zip_file.write(file_path, arcname)
        
        zip_buffer.seek(0)
        
        # Generate filename (sanitize task_id for filename)
        safe_task_id = task_id.replace('/', '_').replace('\\', '_').replace('..', '_')
        zip_filename = f"{safe_task_id}.zip"
        
        return send_file(
            zip_buffer,
            mimetype='application/zip',
            as_attachment=True,
            download_name=zip_filename
        )
    except Exception as e:
        import traceback
        return jsonify({"error": f"Download failed: {str(e)}\n{traceback.format_exc()}"}), 500


@app.route('/api/files/delete', methods=['POST'])
@login_required
def delete_file():
    """Delete file or directory"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        data = request.json
        path = data.get('path', '')
        task_id = data.get('task_id', '')
        if not path:
            return jsonify({"error": "Missing path parameter"}), 400
        
        # Normalize path (limited to user workspace directory)
        try:
            path_obj = normalize_file_path(path, task_id=task_id if task_id else None, username=username)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        
        if not path_obj.exists():
            return jsonify({"error": "File or directory not found"}), 404
        
        if path_obj.is_file():
            path_obj.unlink()
            return jsonify({"success": True, "message": "File deleted"})
        elif path_obj.is_dir():
            # Recursively delete directory
            import shutil
            shutil.rmtree(path_obj)
            return jsonify({"success": True, "message": "Directory deleted"})
        else:
            return jsonify({"error": "Unknown path type"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/chat/history', methods=['GET'])
@login_required
def get_chat_history():
    """Get chat history"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        task_id_input = request.args.get('task_id', '').strip()
        
        if not task_id_input:
            return jsonify({"error": "Missing task_id parameter"}), 400
        
        # Normalize task ID path (limited to user workspace)
        try:
            task_path, _ = normalize_task_id(task_id_input, username=username)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        
        chat_history_file = task_path / 'chat_history.json'
        
        if not chat_history_file.exists():
            return jsonify({"messages": []})
        
        with open(chat_history_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        messages = data.get("messages", [])
        
        # Convert old messages with timestamp to sequence numbers and sort by sequence
        for idx, msg in enumerate(messages):
            if 'sequence' not in msg:
                # Old message without sequence, assign based on index
                msg['sequence'] = idx
            # Remove timestamp for privacy (if exists)
            if 'timestamp' in msg:
                del msg['timestamp']
        
        # Sort messages by sequence number
        messages.sort(key=lambda m: m.get('sequence', 0))
        
        return jsonify({"messages": messages})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def create_latest_output(chat_history_file: Path):
    """
    Create latest_output.json in current task folder from chat_history.json
    Excludes system messages and truncates content to 200 characters
    """
    try:
        if not chat_history_file.exists():
            print(f"[latest_output] chat_history.json not found: {chat_history_file}")
            return
        
        # Read chat_history.json
        with open(chat_history_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        messages = data.get("messages", [])
        print(f"[latest_output] Processing {len(messages)} messages from {chat_history_file}")
        
        # Filter out system messages and process content
        filtered_messages = []
        for msg in messages:
            # Skip system messages
            if msg.get('agent') == 'system':
                continue
            
            # Create a copy of the message
            filtered_msg = msg.copy()
            
            # Truncate content field to 200 characters
            if 'content' in filtered_msg and filtered_msg['content']:
                content = filtered_msg['content']
                if len(content) > 200:
                    filtered_msg['content'] = content[:200] + "..."
            
            filtered_messages.append(filtered_msg)
        
        print(f"[latest_output] Filtered to {len(filtered_messages)} messages (excluding system)")
        
        # Create latest_output.json in current task folder (same directory as chat_history.json)
        task_path = chat_history_file.parent
        latest_output_file = task_path / 'latest_output.json'
        with open(latest_output_file, 'w', encoding='utf-8') as f:
            json.dump({"messages": filtered_messages}, f, ensure_ascii=False, indent=2)
        
        print(f"[latest_output] Successfully created latest_output.json at {latest_output_file}")
    
    except Exception as e:
        # Log error but don't interrupt the main save operation
        print(f"[latest_output] Error creating latest_output.json: {e}")
        import traceback
        traceback.print_exc()


@app.route('/api/chat/save', methods=['POST'])
@login_required
def save_chat_message():
    """Save chat message (use file lock to ensure atomicity)"""
    try:
        data = request.json
        task_id_input = data.get('task_id', '').strip()
        message = data.get('message')
        
        if not task_id_input or not message:
            return jsonify({"error": "Missing required parameters"}), 400
        
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        # Normalize task ID path (limited to user workspace)
        try:
            task_path, _ = normalize_task_id(task_id_input, username=username)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        task_path.mkdir(parents=True, exist_ok=True)
        chat_history_file = task_path / 'chat_history.json'
        
        # Use file lock to ensure atomic operation, prevent data loss from concurrent writes
        try:
            # Open file (create if not exists)
            with open(chat_history_file, 'a+', encoding='utf-8') as f:
                # Get file lock (blocking mode)
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                
                try:
                    # Reposition to file beginning
                    f.seek(0)
                    
                    # Read existing records
                    content = f.read()
                    if content.strip():
                        try:
                            data = json.loads(content)
                            messages = data.get("messages", [])
                        except json.JSONDecodeError:
                            # If JSON parsing fails, start from empty list
                            messages = []
                    else:
                        messages = []
                    
                    # Convert old messages with timestamp to sequence numbers (for backward compatibility)
                    # Find max sequence number or calculate from message count
                    max_seq = -1
                    for idx, m in enumerate(messages):
                        # Remove timestamp from all messages for privacy
                        if 'timestamp' in m:
                            del m['timestamp']
                        
                        if 'sequence' in m:
                            max_seq = max(max_seq, m['sequence'])
                        else:
                            # Old message without sequence, assign sequence based on index
                            m['sequence'] = idx
                            max_seq = max(max_seq, idx)
                    
                    # Calculate next sequence number
                    next_sequence = max_seq + 1 if max_seq >= 0 else len(messages)
                    
                    # Remove timestamp from new message and add sequence number
                    message_content = message.get('content', '')[:50] if message.get('content') else ''
                    if 'timestamp' in message:
                        del message['timestamp']
                    message['sequence'] = next_sequence
                    
                    # Check if same message already exists (avoid duplicates)
                    # Judge by sequence and content
                    is_duplicate = any(
                        (m.get('sequence') == next_sequence and 
                         (m.get('content', '')[:50] if m.get('content') else '') == message_content) or
                        # Also check by content only for backward compatibility
                        ((m.get('content', '')[:50] if m.get('content') else '') == message_content and
                         m.get('agent') == message.get('agent') and
                         m.get('type') == message.get('type') and
                         m.get('isUser') == message.get('isUser'))
                        for m in messages
                    )
                    
                    if not is_duplicate:
                        # Add new message
                        messages.append(message)
                        
                        # Sort messages by sequence number before saving
                        messages.sort(key=lambda m: m.get('sequence', 0))
                        
                        # Clear file and write
                        f.seek(0)
                        f.truncate(0)
                        json.dump({"messages": messages}, f, ensure_ascii=False, indent=2)
                        f.flush()  # Ensure immediate write to disk
                        
                        # Check if this is a final_output message, create latest_output.json
                        if message.get('type') == 'final_output':
                            print(f"[latest_output] ✅ Detected final_output message - agent: {message.get('agent', 'unknown')}, task: {task_id_input}")
                            try:
                                create_latest_output(chat_history_file)
                            except Exception as e:
                                print(f"[latest_output] ❌ Error creating latest_output.json: {e}")
                                import traceback
                                traceback.print_exc()
                        else:
                            # Debug: log message type for troubleshooting
                            if message.get('type') in ['final_output', 'tool_call', 'start', 'info']:
                                print(f"[latest_output] Debug: Message type '{message.get('type')}' from agent '{message.get('agent', 'unknown')}' (not final_output)")
                finally:
                    # Release file lock
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        
        except OSError as e:
            # If file lock unavailable (Windows), use thread lock as fallback
            lock_key = f"chat_lock_{task_id_input}"
            if not hasattr(save_chat_message, '_locks'):
                save_chat_message._locks = {}
            if lock_key not in save_chat_message._locks:
                save_chat_message._locks[lock_key] = threading.Lock()
            
            with save_chat_message._locks[lock_key]:
                # Read existing records
                messages = []
                if chat_history_file.exists():
                    try:
                        with open(chat_history_file, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                            messages = data.get("messages", [])
                    except:
                        messages = []
                
                # Convert old messages with timestamp to sequence numbers (for backward compatibility)
                # Find max sequence number or calculate from message count
                max_seq = -1
                for idx, m in enumerate(messages):
                    # Remove timestamp from all messages for privacy
                    if 'timestamp' in m:
                        del m['timestamp']
                    
                    if 'sequence' in m:
                        max_seq = max(max_seq, m['sequence'])
                    else:
                        # Old message without sequence, assign sequence based on index
                        m['sequence'] = idx
                        max_seq = max(max_seq, idx)
                
                # Calculate next sequence number
                next_sequence = max_seq + 1 if max_seq >= 0 else len(messages)
                
                # Remove timestamp from new message and add sequence number
                message_content = message.get('content', '')[:50] if message.get('content') else ''
                if 'timestamp' in message:
                    del message['timestamp']
                message['sequence'] = next_sequence
                
                # Check if same message already exists (avoid duplicates)
                # Judge by sequence and content
                is_duplicate = any(
                    (m.get('sequence') == next_sequence and 
                     (m.get('content', '')[:50] if m.get('content') else '') == message_content) or
                    # Also check by content only for backward compatibility
                    ((m.get('content', '')[:50] if m.get('content') else '') == message_content and
                     m.get('agent') == message.get('agent') and
                     m.get('type') == message.get('type') and
                     m.get('isUser') == message.get('isUser'))
                    for m in messages
                )
                
                if not is_duplicate:
                    # Add new message
                    messages.append(message)
                    
                    # Sort messages by sequence number before saving
                    messages.sort(key=lambda m: m.get('sequence', 0))
                    
                    # Save
                    with open(chat_history_file, 'w', encoding='utf-8') as f:
                        json.dump({"messages": messages}, f, ensure_ascii=False, indent=2)
                    
                    # Check if this is a final_output message, create latest_output.json
                    if message.get('type') == 'final_output':
                        print(f"[latest_output] ✅ Detected final_output message - agent: {message.get('agent', 'unknown')}, task: {task_id_input}")
                        try:
                            create_latest_output(chat_history_file)
                        except Exception as e:
                            print(f"[latest_output] ❌ Error creating latest_output.json: {e}")
                            import traceback
                            traceback.print_exc()
                    else:
                        # Debug: log message type for troubleshooting
                        if message.get('type') in ['final_output', 'tool_call', 'start', 'info']:
                            print(f"[latest_output] Debug: Message type '{message.get('type')}' from agent '{message.get('agent', 'unknown')}' (not final_output)")
        
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/files/files', methods=['POST'])
@login_required
def upload_file():
    """Upload file"""
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file"}), 400
        
        file = request.files['file']
        if file.filename == '':
            return jsonify({"error": "Filename is empty"}), 400
        
        # Get target directory
        target_dir = request.form.get('target_dir', '')
        if not target_dir:
            return jsonify({"error": "Missing target directory parameter"}), 400
        
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        # Clean and extract filename
        # Decode URL-encoded filename if needed
        import urllib.parse
        filename = file.filename
        try:
            # Try to decode URL encoding (handle special characters)
            filename = urllib.parse.unquote(filename, encoding='utf-8')
        except:
            # If decoding fails, use original filename
            pass
        
        # Ensure filename is safe (remove any path components and normalize)
        filename = Path(filename).name  # Get only the filename part, remove any path
        # Normalize filename (remove leading/trailing spaces)
        filename = filename.strip()
        
        if not filename:
            return jsonify({"error": "Filename is empty after cleaning"}), 400
        
        # Normalize target directory path (limited to user workspace directory)
        try:
            target_dir_obj = normalize_file_path(target_dir, username=username)
        except ValueError as e:
            error_msg = f"Invalid target directory: {str(e)}"
            if "pattern" in str(e).lower():
                error_msg += f" (Directory path may contain invalid characters: {target_dir})"
            return jsonify({"error": error_msg}), 400
        
        if not target_dir_obj.exists():
            return jsonify({"error": "Target directory does not exist"}), 404
        
        if not target_dir_obj.is_dir():
            return jsonify({"error": "Target path is not a directory"}), 400
        
        # Build target file path
        target_path = target_dir_obj / filename
        
        # Additional security check: ensure final path is still under user workspace
        user_workspace = get_user_workspace(username)
        try:
            target_path.resolve().relative_to(user_workspace.resolve())
        except ValueError:
            return jsonify({"error": "File path is unsafe: cannot exceed user workspace directory"}), 400
        
        # Save file with comprehensive error handling
        try:
            # Ensure parent directory exists
            target_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Save the file
            file.save(str(target_path))
        except OSError as os_error:
            # Handle OS-level errors (permissions, disk full, etc.)
            error_msg = f"Failed to save file: {str(os_error)}"
            if "pattern" in str(os_error).lower() or "invalid" in str(os_error).lower():
                error_msg += f" (Filename may contain invalid characters: {filename})"
            return jsonify({"error": error_msg}), 500
        except Exception as save_error:
            # Handle other errors
            import traceback
            error_msg = f"Failed to save file: {str(save_error)}"
            if "pattern" in str(save_error).lower():
                error_msg += f" (Filename may contain invalid characters: {filename})"
            # Log full traceback for debugging
            print(f"Upload error: {traceback.format_exc()}")
            return jsonify({"error": error_msg}), 500
        
        # Get user workspace for relative path calculation
        # Calculate relative path (for display)
        try:
            rel_path = target_path.relative_to(user_workspace)
            display_path = str(rel_path) if rel_path != Path('.') else ''
        except ValueError:
            display_path = str(target_path)
        
        return jsonify({
            "success": True,
            "message": "File uploaded successfully",
            "path": display_path  # Return relative path (for frontend display)
        })
    except Exception as e:
        import traceback
        error_msg = str(e)
        # Check if error contains pattern-related message
        if "pattern" in error_msg.lower():
            error_msg = f"Path validation failed: {error_msg}"
        print(f"Upload error: {traceback.format_exc()}")
        return jsonify({"error": error_msg}), 500


@app.route('/api/files/preview', methods=['GET'])
@login_required
def preview_file():
    """Preview file (for images, etc.)"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        path = request.args.get('path', '')
        if not path:
            return jsonify({"error": "Missing path parameter"}), 400
        
        # path 已经是相对于用户工作空间的完整路径（包含 task_id）
        try:
            path_obj = normalize_file_path(path, task_id=None, username=username)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        
        if not path_obj.exists():
            return jsonify({"error": f"File not found: {path_obj}"}), 404
        
        if not path_obj.is_file():
            return jsonify({"error": "Path is not a file"}), 400
        
        # Use Flask's send_file to send file to client (not as attachment)
        from flask import send_file
        
        # Get filename for content type detection
        filename = path_obj.name
        
        # Determine MIME type
        import mimetypes
        mime_type, _ = mimetypes.guess_type(str(path_obj))
        if not mime_type:
            mime_type = 'application/octet-stream'
        
        return send_file(
            str(path_obj),
            mimetype=mime_type
        )
    except Exception as e:
        import traceback
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        return jsonify({"error": error_msg}), 500


@app.route('/api/files/download', methods=['GET'])
@login_required
def download_file():
    """Download file"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        path = request.args.get('path', '')
        if not path:
            return jsonify({"error": "Missing path parameter"}), 400
        
        # path 已经是相对于用户工作空间的完整路径（包含 task_id），不需要再传递 task_id
        # 直接使用 normalize_file_path，不传递 task_id 参数
        try:
            path_obj = normalize_file_path(path, task_id=None, username=username)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        
        if not path_obj.exists():
            return jsonify({"error": f"File not found: {path_obj}"}), 404
        
        if not path_obj.is_file():
            return jsonify({"error": "Path is not a file"}), 400
        
        # Use Flask's send_file to send file to client
        from flask import send_file
        
        # Get filename for download
        filename = path_obj.name
        
        # Determine MIME type
        import mimetypes
        mime_type, _ = mimetypes.guess_type(str(path_obj))
        if not mime_type:
            mime_type = 'application/octet-stream'
        
        return send_file(
            str(path_obj),
            mimetype=mime_type,
            as_attachment=True,
            download_name=filename
        )
    except Exception as e:
        import traceback
        error_msg = str(e)
        # For security, don't expose full traceback to frontend
        # Only include first line of error message
        error_lines = error_msg.split('\n')
        safe_error = error_lines[0] if error_lines else "Unknown error occurred"
        print(f"Download file error: {traceback.format_exc()}")  # Log full error to server
        return jsonify({"error": safe_error}), 500


@app.route('/api/hil/check', methods=['POST'])
@login_required
def check_hil_task():
    """Check if there's a pending HIL task for the current workspace"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        data = request.json
        task_id_input = data.get('task_id', '').strip()
        
        if not task_id_input:
            return jsonify({"error": "Missing task_id parameter"}), 400
        
        # Normalize task ID path (user-aware)
        try:
            task_path, _ = normalize_task_id(task_id_input, username=username)
            task_id_absolute = str(task_path)
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        
        # Call tool server to check for HIL tasks
        import requests
        import urllib.parse
        
        # Load tool server URL from config (same as tool_executor.py)
        config_path = project_root / "config" / "run_env_config" / "tool_config.yaml"
        with open(config_path, 'r', encoding='utf-8') as f:
            tool_config = yaml.safe_load(f)
        tool_server_url = tool_config.get('tools_server', 'http://127.0.0.1:8001/').rstrip('/')
        
        # URL encode task_id for the API call
        encoded_task_id = urllib.parse.quote(task_id_absolute, safe='')
        
        try:
            response = requests.get(
                f"{tool_server_url}/api/hil/workspace/{encoded_task_id}",
                timeout=5
            )
            
            if response.status_code == 200:
                hil_data = response.json()
                if hil_data.get("found"):
                    return jsonify({
                        "found": True,
                        "hil_id": hil_data.get("hil_id"),
                        "instruction": hil_data.get("instruction")
                    })
                else:
                    return jsonify({"found": False})
            else:
                return jsonify({"found": False, "error": f"Tool server returned {response.status_code}"})
        
        except requests.exceptions.RequestException as e:
            # Tool server may be unavailable, return not found
            return jsonify({"found": False, "error": str(e)})
    
    except Exception as e:
        import traceback
        print(f"Check HIL task error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/hil/respond', methods=['POST'])
@login_required
def respond_hil_task():
    """Respond to a HIL task"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        data = request.json
        hil_id = data.get('hil_id')
        response_text = data.get('response', '')
        
        if not hil_id:
            return jsonify({"error": "Missing hil_id parameter"}), 400
        
        # Call tool server to respond to HIL task
        import requests
        
        # Load tool server URL from config (same as tool_executor.py)
        config_path = project_root / "config" / "run_env_config" / "tool_config.yaml"
        with open(config_path, 'r', encoding='utf-8') as f:
            tool_config = yaml.safe_load(f)
        tool_server_url = tool_config.get('tools_server', 'http://127.0.0.1:8001/').rstrip('/')
        
        try:
            api_response = requests.post(
                f"{tool_server_url}/api/hil/respond/{hil_id}",
                json={"response": response_text},
                timeout=5
            )
            
            if api_response.status_code == 200:
                result = api_response.json()
                if result.get("success"):
                    return jsonify({"success": True, "message": result.get("message", "HIL task responded")})
                else:
                    return jsonify({"error": result.get("error", "Failed to respond to HIL task")}), 400
            else:
                return jsonify({"error": f"Tool server returned {api_response.status_code}"}), 500
        
        except requests.exceptions.RequestException as e:
            return jsonify({"error": f"Failed to connect to tool server: {str(e)}"}), 500
    
    except Exception as e:
        import traceback
        print(f"Respond to HIL task error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/config/list', methods=['GET'])
@login_required
def list_config_files():
    """List available configuration files"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        # Get config type from query parameter (run_env or agent)
        config_type = request.args.get('type', 'run_env')
        
        if config_type == 'run_env':
            config_dir = project_root / "config" / "run_env_config"
        elif config_type == 'agent':
            # Default to Default system, can be extended to support other systems
            config_dir = project_root / "config" / "agent_library" / "Default"
        else:
            return jsonify({"error": "Invalid config type. Use 'run_env' or 'agent'"}), 400
        
        if not config_dir.exists():
            return jsonify({"error": "Config directory not found"}), 404
        
        # List all YAML files in config directory
        config_files = []
        for file_path in config_dir.glob("*.yaml"):
            if file_path.is_file():
                config_files.append({
                    "name": file_path.name,
                    "path": str(file_path.relative_to(project_root))
                })
        
        # Sort by filename
        config_files.sort(key=lambda x: x['name'])
        
        return jsonify({"files": config_files})
    except Exception as e:
        import traceback
        print(f"List config files error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/config/read', methods=['GET'])
@login_required
def read_config_file():
    """Read configuration file content"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        filename = request.args.get('file', '')
        config_type = request.args.get('type', 'run_env')
        
        if not filename:
            return jsonify({"error": "Missing file parameter"}), 400
        
        # Security: only allow YAML files in config directory
        if not filename.endswith('.yaml') or '..' in filename or '/' in filename or '\\' in filename:
            return jsonify({"error": "Invalid file name"}), 400
        
        # Determine config directory based on type
        if config_type == 'run_env':
            config_dir = project_root / "config" / "run_env_config"
        elif config_type == 'agent':
            config_dir = project_root / "config" / "agent_library" / "Default"
        else:
            return jsonify({"error": "Invalid config type"}), 400
        
        config_path = config_dir / filename
        
        # Security: ensure file is within config directory
        try:
            config_path.resolve().relative_to(config_dir.resolve())
        except ValueError:
            return jsonify({"error": "Invalid file path"}), 400
        
        if not config_path.exists():
            return jsonify({"error": "File not found"}), 404
        
        if not config_path.is_file():
            return jsonify({"error": "Path is not a file"}), 400
        
        # Read file content
        try:
            content = config_path.read_text(encoding='utf-8')
        except Exception as e:
            return jsonify({"error": f"Failed to read file: {str(e)}"}), 500
        
        return jsonify({
            "content": content,
            "filename": filename
        })
    except Exception as e:
        import traceback
        print(f"Read config file error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/config/agent-tree', methods=['GET'])
@login_required
def get_agent_tree():
    """Get agent hierarchy tree structure"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        # Load agent configurations
        from utils.config_loader import ConfigLoader
        config_loader = ConfigLoader('Default')
        
        # Build agent tree
        all_agents = {}
        
        # First pass: collect all agents
        for name, config in config_loader.all_tools.items():
            if config.get("type") == "llm_call_agent":
                level = config.get("level", 0)
                available_tools = config.get("available_tools", [])
                
                all_agents[name] = {
                    "name": name,
                    "level": level,
                    "available_tools": available_tools,
                    "children": []  # Will be populated in second pass
                }
        
        # Second pass: build tree structure
        # Find child agents (agents that are in available_tools)
        for name, agent in all_agents.items():
            for tool in agent["available_tools"]:
                if tool in all_agents:
                    agent["children"].append(tool)
        
        # Find root agents (agents that are not children of any other agent)
        root_agents = []
        all_children = set()
        for agent in all_agents.values():
            all_children.update(agent["children"])
        
        for name in all_agents.keys():
            if name not in all_children:
                root_agents.append(name)
        
        # If no root agents found, use highest level agents
        if not root_agents:
            max_level = max([agent["level"] for agent in all_agents.values()], default=0)
            for name, agent in all_agents.items():
                if agent["level"] == max_level:
                    root_agents.append(name)
        
        # Build tree starting from root agents
        def build_tree_node(agent_name, visited=None):
            if visited is None:
                visited = set()
            
            if agent_name in visited:
                return None  # Circular reference
            
            if agent_name not in all_agents:
                return None
            
            visited.add(agent_name)
            agent = all_agents[agent_name]
            
            node = {
                "name": agent["name"],
                "level": agent["level"],
                "children": []
            }
            
            # Add child agents
            for child_name in agent["children"]:
                child_node = build_tree_node(child_name, visited.copy())
                if child_node:
                    node["children"].append(child_node)
            
            return node
        
        # Build trees for all root agents
        trees = []
        for root_name in root_agents:
            tree = build_tree_node(root_name)
            if tree:
                trees.append(tree)
        
        # If no root agents found, build from all agents
        if not trees:
            for name in all_agents.keys():
                tree = build_tree_node(name)
                if tree:
                    trees.append(tree)
        
        return jsonify({
            "trees": trees,
            "all_agents": {name: {
                "level": agent["level"]
            } for name, agent in all_agents.items()}
        })
    except Exception as e:
        import traceback
        print(f"Get agent tree error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/config/save', methods=['POST'])
@login_required
def save_config_file():
    """Save configuration file content"""
    try:
        username = session.get('username')
        if not username:
            return jsonify({"error": "User not authenticated"}), 401
        
        data = request.json
        filename = data.get('file', '')
        content = data.get('content', '')
        config_type = data.get('type', 'run_env')
        
        if not filename:
            return jsonify({"error": "Missing file parameter"}), 400
        
        # Security: only allow YAML files in config directory
        if not filename.endswith('.yaml') or '..' in filename or '/' in filename or '\\' in filename:
            return jsonify({"error": "Invalid file name"}), 400
        
        # Determine config directory based on type
        if config_type == 'run_env':
            config_dir = project_root / "config" / "run_env_config"
        elif config_type == 'agent':
            config_dir = project_root / "config" / "agent_library" / "Default"
        else:
            return jsonify({"error": "Invalid config type"}), 400
        
        config_path = config_dir / filename
        
        # Security: ensure file is within config directory
        try:
            config_path.resolve().relative_to(config_dir.resolve())
        except ValueError:
            return jsonify({"error": "Invalid file path"}), 400
        
        # Validate YAML syntax before saving
        try:
            yaml.safe_load(content)
        except yaml.YAMLError as e:
            return jsonify({"error": f"Invalid YAML syntax: {str(e)}"}), 400
        
        # Save file
        try:
            config_path.write_text(content, encoding='utf-8')
        except Exception as e:
            return jsonify({"error": f"Failed to save file: {str(e)}"}), 500
        
        return jsonify({"success": True, "message": f"Configuration saved successfully"})
    except Exception as e:
        import traceback
        print(f"Save config file error: {traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    # Default to use port 4242 (5000 may be occupied by macOS AirPlay)
    port = int(os.environ.get('PORT', 4242))
    print(f"🌐 Web UI server started at http://localhost:{port}")
    print(f"📂 Project root: {project_root}")
    print(f"💡 Tip: If port is occupied, specify another port via environment variable PORT=8080")
    app.run(host='0.0.0.0', port=port, debug=True, threaded=True)


