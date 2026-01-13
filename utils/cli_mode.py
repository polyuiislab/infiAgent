#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
交互式 CLI 模式
"""

import os
import sys
from pathlib import Path
import subprocess
import threading
import queue
import signal
import time
import json
import hashlib
from datetime import datetime

try:
    from prompt_toolkit import PromptSession, print_formatted_text
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.patch_stdout import patch_stdout
    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from rich.live import Live
    from rich.table import Table
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False


def t(key: str, lang: str = 'en') -> str:
    """获取指定语言的文本（全局函数）"""
    return TEXTS.get(lang, TEXTS['en']).get(key, key)


# 多语言文本配置
TEXTS = {
    'en': {
        # System messages
        'select_agent_system': 'Select Agent System',
        'select_mode': 'Select Tool Execution Mode',
        'auto_mode': 'Auto Mode - All tools execute automatically (fast, risky)',
        'manual_mode': 'Manual Mode - File write/code exec/pip install need confirmation (safe)',
        'mode_set_auto': 'Set to: Auto Mode',
        'mode_set_manual': 'Set to: Manual Mode',
        'invalid_choice': 'Invalid choice, please enter',
        'default': 'default',
        
        # Banner
        'cli_title': 'MLA Agent - Interactive CLI',
        'work_dir': 'Work Directory',
        'default_agent': 'Default Agent',
        'available_agents': 'Available Agents',
        'usage': 'Usage',
        'usage_1': 'Enter task directly (use default Agent)',
        'usage_2': '@agent_name task (switch and use specified Agent)',
        'usage_3': 'HIL tasks will auto-prompt for response',
        'usage_4': 'Ctrl+C interrupt | /resume resume | /quit exit | /help help',
        
        # Commands
        'starting_task': 'Starting Task',
        'input': 'Input',
        'hint_resume': 'Hint: Enter /resume to resume, enter new content to start new task',
        'stopping_task': 'Stopping running task...',
        'task_stopped': 'Task stopped',
        'task_force_stopped': 'Task force stopped',
        'goodbye': 'Goodbye!',
        'available_agents_list': 'Available Agents',
        'current': 'current',
        'interrupting_task': 'Interrupting task...',
        'task_interrupted': 'Task interrupted',
        'no_running_task': 'No running task. Enter /quit to exit CLI',
        
        # HIL
        'hil_detected': 'HIL task detected! Press Enter to handle...',
        'hil_task': 'Human-in-Loop Task',
        'task_id': 'Task ID',
        'instruction': 'Instruction',
        'enter_response': 'Please enter your response (any text)',
        'skip_task': 'Enter /skip to skip this task',
        'hil_responded': 'HIL task responded',
        'content': 'Content',
        'hil_response_failed': 'HIL response failed, please retry',
        'hil_skipped': 'HIL task skipped',
        'response_empty': 'Response cannot be empty, please re-enter',
        
        # Tool confirmation
        'tool_confirm_detected': 'Tool execution request detected! Press Enter to confirm...',
        'tool_confirm_title': 'Tool Execution Confirmation',
        'tool_name': 'Tool Name',
        'confirm_id': 'Confirm ID',
        'parameters': 'Parameters',
        'choose_action': 'Choose action',
        'approve_tool': 'yes / y - Approve tool execution',
        'reject_tool': 'no / n  - Reject tool execution',
        'tool_approved': 'Tool approved',
        'tool_rejected': 'Tool rejected',
        'invalid_choice_yn': 'Invalid choice, please enter yes or no',
        
        # Resume
        'checking_task': 'Checking interrupted task...',
        'task_found': 'Interrupted task found',
        'agent': 'Agent',
        'task': 'Task',
        'interrupted_at': 'Interrupted at',
        'stack_depth': 'Stack depth',
        'resume_confirm': 'Resume this task? [y/N]',
        'resume_cancelled': 'Resume cancelled',
        'resuming_task': 'Resuming task...',
        
        # Pending task warning
        'pending_task_warning': 'Pending task detected, cannot start new task!',
        'hil_pending': 'HIL task waiting for response',
        'tool_confirm_pending': 'Tool confirmation waiting for processing',
        'press_enter_hint': 'Please press Enter to enter processing mode',
        
        # Toolbar
        'toolbar': '@agent switch | Ctrl+C interrupt | /resume resume | /quit exit',
        'toolbar_hil': 'HIL task waiting for response!',
    },
    'zh': {
        # System messages
        'select_agent_system': '选择 Agent 系统',
        'select_mode': '选择工具执行模式',
        'auto_mode': '自动模式 (Auto) - 所有工具自动执行（快速，但有风险）',
        'manual_mode': '手动模式 (Manual) - 文件写入、代码执行、包安装需要确认（安全）',
        'mode_set_auto': '已设置为: 自动模式 (Auto)',
        'mode_set_manual': '已设置为: 手动模式 (Manual)',
        'invalid_choice': '无效选择，请输入',
        'default': '默认',
        
        # Banner
        'cli_title': 'MLA Agent - 交互式 CLI',
        'work_dir': '工作目录',
        'default_agent': '默认Agent',
        'available_agents': '可用Agents',
        'usage': '使用说明',
        'usage_1': '直接输入任务（使用默认 Agent）',
        'usage_2': '@agent_name 任务（切换并使用指定 Agent）',
        'usage_3': 'HIL 任务出现时会自动提示，输入响应内容即可',
        'usage_4': 'Ctrl+C 中断任务 | /resume 恢复 | /quit 退出 | /help 帮助',
        
        # Commands
        'starting_task': '启动任务',
        'input': '输入',
        'hint_resume': '提示: 输入/resume回车可续跑，输入新内容开始新任务',
        'stopping_task': '正在停止运行中的任务...',
        'task_stopped': '任务已停止',
        'task_force_stopped': '任务已强制终止',
        'goodbye': '再见！',
        'available_agents_list': '可用 Agents',
        'current': '当前',
        'interrupting_task': '正在中断任务...',
        'task_interrupted': '任务已中断',
        'no_running_task': '没有运行中的任务。输入 /quit 退出 CLI',
        
        # HIL
        'hil_detected': '检测到 HIL 任务！请按回车处理...',
        'hil_task': '人类交互任务 (HIL)',
        'task_id': '任务ID',
        'instruction': '指令',
        'enter_response': '请输入您的响应（任何文本）',
        'skip_task': '输入 /skip 跳过此任务',
        'hil_responded': 'HIL 任务已响应',
        'content': '内容',
        'hil_response_failed': 'HIL 响应失败，请稍后重试',
        'hil_skipped': '已跳过此 HIL 任务',
        'response_empty': '响应内容不能为空，请重新输入',
        
        # Tool confirmation
        'tool_confirm_detected': '检测到工具执行请求！请按回车确认...',
        'tool_confirm_title': '工具执行确认请求',
        'tool_name': '工具名称',
        'confirm_id': '确认ID',
        'parameters': '参数',
        'choose_action': '选择操作',
        'approve_tool': 'yes / y - 批准执行此工具',
        'reject_tool': 'no / n  - 拒绝执行此工具',
        'tool_approved': '已批准执行工具',
        'tool_rejected': '已拒绝执行工具',
        'invalid_choice_yn': '无效选择，请输入 yes 或 no',
        
        # Resume
        'checking_task': '检查中断的任务...',
        'task_found': '发现中断的任务',
        'agent': 'Agent',
        'task': '任务',
        'interrupted_at': '中断于',
        'stack_depth': '栈深度',
        'resume_confirm': '是否恢复此任务？ [y/N]',
        'resume_cancelled': '已取消恢复',
        'resuming_task': '恢复任务...',
        
        # Pending task warning
        'pending_task_warning': '检测到待处理的任务，无法启动新任务！',
        'hil_pending': 'HIL 任务正在等待您的响应',
        'tool_confirm_pending': '工具确认请求正在等待您的处理',
        'press_enter_hint': '请直接按回车进入处理模式',
        
        # Toolbar
        'toolbar': '@agent 切换 | Ctrl+C 中断 | /resume 恢复 | /quit 退出',
        'toolbar_hil': '有HIL任务等待响应！',
    }
}


class InteractiveCLI:
    """交互式命令行界面"""
    
    def __init__(self, task_id: str, agent_system: str = "Test_agent"):
        self.task_id = task_id
        self.agent_system = agent_system
        self.current_agent = "alpha_agent"
        self.current_process = None
        self.output_queue = queue.Queue()
        self.output_lines = []  # 保存最近的输出
        self.max_output_lines = 20  # 最多保留20行输出
        self.hil_mode = False  # 是否处于 HIL 响应模式
        self.current_hil_task = None  # 当前的 HIL 任务
        self.pending_hil = None  # 待处理的 HIL 任务（后台线程检测到的）
        self.hil_processing = False  # 是否正在处理 HIL 任务（避免重复检测）
        self.hil_check_interval = 2  # HIL 检查间隔（秒）
        self.stop_hil_checker = False  # 停止 HIL 检查线程的标志
        
        # 工具确认相关
        self.pending_tool_confirmation = None  # 待处理的工具确认（后台线程检测到的）
        self.tool_confirmation_processing = False  # 是否正在处理工具确认
        self.auto_mode = None  # 权限模式（None=未设置, True=自动, False=手动）
        
        # 语言设置
        self.language = 'en'  # 默认英文
        
        # Rich console
        self.console = Console() if RICH_AVAILABLE else None
        
        # 加载可用 agent 列表
        self.available_agents = self._load_available_agents()
        
        # 获取工具服务器地址
        self._load_tool_server_url()
        
        # 启动后台 HIL 检查线程
        self._start_hil_checker()
    
    def t(self, key: str) -> str:
        """获取当前语言的文本"""
        return TEXTS.get(self.language, TEXTS['en']).get(key, key)
    
    def _load_available_agents(self):
        """加载 Level 2/3 Agent 列表"""
        try:
            from utils.config_loader import ConfigLoader
            config_loader = ConfigLoader(self.agent_system)
            
            agents = []
            for name, config in config_loader.all_tools.items():
                if config.get("type") == "llm_call_agent":
                    level = config.get("level", 0)
                    if level in [1,2, 3]:
                        agents.append(name)
            
            return agents
        except:
            return ["alpha_agent"]
    
    def _load_tool_server_url(self):
        """加载工具服务器地址"""
        try:
            import yaml
            config_path = Path(__file__).parent.parent / "config" / "run_env_config" / "tool_config.yaml"
            with open(config_path, 'r', encoding='utf-8') as f:
                tool_config = yaml.safe_load(f)
            self.server_url = tool_config.get('tools_server', 'http://127.0.0.1:8001').rstrip('/')
        except Exception:
            self.server_url = 'http://127.0.0.1:8001'
    
    def _check_hil_task(self) -> dict:
        """检查当前 workspace 是否有等待中的 HIL 任务"""
        try:
            import requests
            response = requests.get(
                f"{self.server_url}/api/hil/workspace/{self.task_id}",
                timeout=2
            )
            if response.status_code == 200:
                return response.json()
            return {"found": False}
        except Exception:
            return {"found": False}
    
    def _respond_hil_task(self, hil_id: str, response: str) -> bool:
        """响应 HIL 任务"""
        try:
            import requests
            resp = requests.post(
                f"{self.server_url}/api/hil/respond/{hil_id}",
                json={"response": response},
                timeout=5
            )
            result = resp.json()
            return result.get('success', False)
        except Exception:
            return False
    
    def _check_tool_confirmation(self) -> dict:
        """检查当前 workspace 是否有等待中的工具确认请求"""
        try:
            import requests
            response = requests.get(
                f"{self.server_url}/api/tool-confirmation/workspace/{self.task_id}",
                timeout=2
            )
            if response.status_code == 200:
                return response.json()
            return {"found": False}
        except Exception:
            return {"found": False}
    
    def _respond_tool_confirmation(self, confirm_id: str, approved: bool) -> bool:
        """响应工具确认请求"""
        try:
            import requests
            resp = requests.post(
                f"{self.server_url}/api/tool-confirmation/respond/{confirm_id}",
                json={"approved": approved},
                timeout=5
            )
            result = resp.json()
            return result.get('success', False)
        except Exception:
            return False
    
    def _get_interrupted_task(self) -> dict:
        """获取中断的任务（检查 stack）"""
        try:
            # 计算 task_id 的 hash（与 hierarchy_manager 一致）
            task_hash = hashlib.md5(self.task_id.encode()).hexdigest()[:8]  # 8位，不是12位
            
            # 跨平台路径处理
            task_folder = Path(self.task_id).name if (os.sep in self.task_id or '/' in self.task_id or '\\' in self.task_id) else self.task_id
            task_name = f"{task_hash}_{task_folder}"
            
            # Stack 文件位置（与 hierarchy_manager 一致）
            conversations_dir = Path.home() / "mla_v3" / "conversations"
            stack_file = conversations_dir / f"{task_name}_stack.json"
            
            if not stack_file.exists():
                return {"found": False, "message": f"没有找到中断的任务（文件不存在: {stack_file})"}
            
            # 读取 stack
            with open(stack_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                stack = data.get("stack", [])
            
            if not stack:
                return {"found": False, "message": "没有中断的任务（stack 为空）"}
            
            # 获取栈底任务（最初的用户输入）
            bottom_task = stack[0]
            agent_name = bottom_task.get("agent_name")
            user_input = bottom_task.get("user_input")
            
            if not agent_name or not user_input:
                return {"found": False, "message": "任务数据不完整"}
            
            return {
                "found": True,
                "agent_name": agent_name,
                "user_input": user_input,
                "interrupted_at": bottom_task.get("start_time", "未知"),
                "stack_depth": len(stack)
            }
        
        except Exception as e:
            return {"found": False, "message": f"读取任务失败: {e}"}
    
    def _start_hil_checker(self):
        """启动后台 HIL/工具确认检查线程"""
        def hil_checker_thread():
            while not self.stop_hil_checker:
                try:
                    # 检查 HIL 任务
                    if not self.pending_hil and not self.hil_processing:
                        hil_task = self._check_hil_task()
                        if hil_task.get("found"):
                            # 发现新的 HIL 任务
                            self.pending_hil = hil_task
                            # 打印提示音（ASCII bell）和可见提示
                            print("\n\n\a")  # \a 是响铃符号
                            print("\n" + "="*80)
                            print(f"🔔🔔🔔 {self.t('hil_detected')} 🔔🔔🔔")
                            print("="*80 + "\n")
                    
                    # 检查工具确认请求（仅在手动模式下）
                    if self.auto_mode == False and not self.pending_tool_confirmation and not self.tool_confirmation_processing:
                        tool_confirmation = self._check_tool_confirmation()
                        if tool_confirmation.get("found"):
                            # 发现新的工具确认请求
                            self.pending_tool_confirmation = tool_confirmation
                            # 打印提示音和可见提示
                            print("\n\n\a")
                            print("\n" + "="*80)
                            print(f"⚠️⚠️⚠️ {self.t('tool_confirm_detected')} ⚠️⚠️⚠️")
                            print("="*80 + "\n")
                except Exception:
                    pass
                
                # 等待一段时间再检查
                time.sleep(self.hil_check_interval)
        
        thread = threading.Thread(target=hil_checker_thread, daemon=True)
        thread.start()
    
    def _show_hil_prompt(self, hil_id: str, instruction: str):
        """显示 HIL 提示界面"""
        print("\n" + "="*80)
        print(f"🔔 {self.t('hil_task')}")
        print("="*80)
        print(f"📝 {self.t('task_id')}: {hil_id}")
        print(f"📋 {self.t('instruction')}: {instruction}")
        print("="*80)
        print(f"💡 {self.t('enter_response')}")
        print(f"   {self.t('skip_task')}")
        print("="*80 + "\n")
    
    def _show_tool_confirmation_prompt(self, confirm_id: str, tool_name: str, arguments: dict):
        """显示工具确认界面"""
        print("\n" + "="*80)
        print(f"⚠️  {self.t('tool_confirm_title')}")
        print("="*80)
        print(f"🔧 {self.t('tool_name')}: {tool_name}")
        print(f"📝 {self.t('confirm_id')}: {confirm_id}")
        print(f"📋 {self.t('parameters')}:")
        for key, value in arguments.items():
            # 截断过长的参数值
            value_str = str(value)
            if len(value_str) > 100:
                value_str = value_str[:100] + "..."
            print(f"     {key}: {value_str}")
        print("="*80)
        print(f"💡 {self.t('choose_action')}:")
        print(f"   {self.t('approve_tool')}")
        print(f"   {self.t('reject_tool')}")
        print("="*80 + "\n")
    
    def get_banner_text(self):
        """获取 banner 文本（用于顶部固定显示）"""
        return (
            "="*80 + "\n" +
            f"🤖 {self.t('cli_title')}\n" +
            "="*80 + "\n" +
            f"📂 {self.t('work_dir')}: {self.task_id}\n" +
            f"🤖 {self.t('default_agent')}: {self.current_agent}\n" +
            f"📋 {self.t('available_agents')}: {', '.join(self.available_agents[:3])}{'...' if len(self.available_agents) > 3 else ''}\n" +
            "-"*80 + "\n" +
            f"💡 {self.t('usage')}:\n" +
            f"  - {self.t('usage_1')}\n" +
            f"  - {self.t('usage_2')}\n" +
            f"  - 🔔 {self.t('usage_3')}\n" +
            f"  - {self.t('usage_4')}\n" +
            "-"*80 + "\n"
        )
    
    def show_banner(self):
        """显示欢迎信息（初始时）"""
        if RICH_AVAILABLE:
            self.console.clear()
            
            # 创建顶部 Panel
            header_table = Table.grid(padding=(0, 2))
            header_table.add_column(style="cyan")
            header_table.add_column()
            
            header_table.add_row(f"📂 {self.t('work_dir')}:", self.task_id)
            header_table.add_row(f"🤖 {self.t('default_agent')}:", f"[bold green]{self.current_agent}[/]")
            header_table.add_row(f"📋 {self.t('available_agents')}:", ", ".join(self.available_agents[:4]) + ("..." if len(self.available_agents) > 4 else ""))
            
            self.console.print(Panel(
                header_table,
                title=f"[bold blue]🤖 {self.t('cli_title')}[/]",
                border_style="blue"
            ))
            
            # 使用说明
            help_text = Text()
            help_text.append(f"💡 {self.t('usage')}:\n", style="bold yellow")
            help_text.append(f"  • {self.t('usage_1')}\n")
            help_text.append(f"  • {self.t('usage_2')}\n")
            help_text.append(f"  • 🔔 {self.t('usage_3')}\n", style="cyan")
            help_text.append(f"  • {self.t('usage_4')}\n")
            
            self.console.print(Panel(help_text, border_style="dim"))
            print()
        else:
            # 回退到简单模式
            os.system('clear' if os.name != 'nt' else 'cls')
            print(self.get_banner_text())
    
    def parse_input(self, user_input: str):
        """
        解析用户输入
        
        Returns:
            (agent_name, task_description)
        """
        user_input = user_input.strip()
        
        # 检查是否指定 agent
        if user_input.startswith('@'):
            parts = user_input[1:].split(None, 1)
            if len(parts) == 2:
                agent_name, task = parts
                # 验证 agent 是否存在
                if agent_name in self.available_agents:
                    return agent_name, task
                else:
                    print(f"⚠️  Agent '{agent_name}' 不存在，使用默认 Agent")
                    return self.current_agent, user_input
            elif len(parts) == 1:
                # 只有 @agent_name，没有任务
                agent_name = parts[0]
                if agent_name in self.available_agents:
                    self.current_agent = agent_name
                    print(f"✅ 已切换到: {agent_name}")
                    return None, None
                else:
                    print(f"⚠️  Agent '{agent_name}' 不存在")
                    return None, None
        
        # 没有 @，使用默认 agent
        return self.current_agent, user_input
    
    def stop_current_task(self):
        """停止当前运行的任务"""
        if self.current_process and self.current_process.poll() is None:
            try:
                if sys.platform == 'win32':
                    # Windows: 发送 Ctrl+Break 信号
                    self.current_process.send_signal(signal.CTRL_BREAK_EVENT)
                    try:
                        self.current_process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        # 如果信号无效，强制终止
                        self.current_process.terminate()
                        self.current_process.wait(timeout=1)
                else:
                    # Unix/Mac: 使用 terminate (发送 SIGTERM)
                    self.current_process.terminate()
                    self.current_process.wait(timeout=3)
                print("\n⚠️  已终止前一个任务\n")
            except Exception as e:
                # 最后手段：强制 kill
                try:
                    self.current_process.kill()
                    self.current_process.wait(timeout=1)
                except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
                    pass
    
    def run_task(self, agent_name: str, user_input: str):
        """
        在后台运行任务（JSONL模式）
        前台保持输入可用
        """
        # 终止当前任务（如果有）
        self.stop_current_task()
        
        print(f"\n{'='*80}")
        print(f"🤖 {self.t('starting_task')}: {agent_name}")
        print(f"📝 {self.t('input')}: {user_input}")
        print(f"💡 {self.t('hint_resume')}")
        print(f"{'='*80}\n")
        
        # 使用当前 Python 解释器调用 start.py（避免 venv 路径问题）
        start_py = Path(__file__).parent.parent / "start.py"
        
        # Windows 需要特殊的进程创建标志以支持信号处理
        popen_kwargs = {
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
            'text': True,
            'encoding': 'utf-8',
            'errors': 'replace',
            'bufsize': 0  # 无缓冲，实时输出
        }
        
        if sys.platform == 'win32':
            # Windows: 创建新的进程组，允许发送 Ctrl+Break
            popen_kwargs['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        
        # 构建命令参数（使用 Python 解释器直接运行 start.py）
        cmd_args = [
            sys.executable,
            str(start_py),
                '--task_id', self.task_id,
                '--agent_name', agent_name,
                '--user_input', user_input,
                '--agent_system', self.agent_system,
                '--jsonl'  # JSONL 模式，实时流式输出
        ]
        
        # 添加权限模式参数
        if self.auto_mode is not None:
            cmd_args.extend(['--auto-mode', 'true' if self.auto_mode else 'false'])
        
        # 启动子进程（JSONL模式 - 实时流式输出）
        self.current_process = subprocess.Popen(
            cmd_args,
            **popen_kwargs
        )
        
        # 后台线程读取输出（JSONL 模式，解析并显示）
        def read_output():
            try:
                import json
                for line in self.current_process.stdout:
                    if not line:
                        continue
                    line = line.rstrip('\n')
                    if not line.strip():
                        continue
                    
                    try:
                        # 解析 JSONL 事件
                        event = json.loads(line)
                        
                        # 显示所有事件（不截断）
                        if event['type'] == 'token':
                            text = event['text']
                            # 完整显示所有文本
                            display_line = f"  {text}"
                            
                            self.output_lines.append(display_line)
                            if len(self.output_lines) > self.max_output_lines:
                                self.output_lines.pop(0)
                            print(display_line)
                        
                        elif event['type'] == 'result':
                            # 显示完整结果
                            summary = event.get('summary', '')
                            
                            print(f"\n{'='*80}")
                            print("📊 执行结果:")
                            print(f"{'='*80}")
                            print(summary)  # 完整显示
                            print(f"{'='*80}\n")
                            
                            # 简短摘要到输出历史
                            self.output_lines.append(f"📊 结果: {summary[:100]}...")
                        
                        elif event['type'] == 'end':
                            status_icon = "✅" if event.get('status') == 'ok' else "❌"
                            duration_sec = event.get('duration_ms', 0) / 1000
                            display_line = f"{status_icon} 任务完成 ({duration_sec:.1f}s)"
                            self.output_lines.append(display_line)
                            print(display_line)
                            print()
                        
                        elif event['type'] == 'error':
                            # 错误事件 - 完整显示错误信息
                            error_text = event.get('text', '')
                            print(error_text)
                            self.output_lines.append(f"❌ 发生错误")
                    
                    except json.JSONDecodeError:
                        # 不是有效的 JSON，跳过
                        pass
            except Exception:
                pass
        
        thread = threading.Thread(target=read_output, daemon=True)
        thread.start()

        # 读取 stderr，防止管道阻塞（但不显示，因为 JSONL 模式下 print 被重定向到 stderr）
        def read_stderr():
            try:
                for err in self.current_process.stderr:
                    if not err:
                        continue
                    # 静默消费 stderr，防止管道写满阻塞
                    # 只在遇到真正的错误关键词时才显示
                    err = err.rstrip('\n')
                    if any(keyword in err for keyword in ['Error:', 'Exception:', 'Traceback', 'CRITICAL', 'FATAL']):
                        error_line = f"⚠️ {err[:200]}"
                        self.output_lines.append(error_line)
                        if len(self.output_lines) > self.max_output_lines:
                            self.output_lines.pop(0)
                        print(error_line)
            except Exception:
                pass

        thread_err = threading.Thread(target=read_stderr, daemon=True)
        thread_err.start()
    
    def get_bottom_toolbar(self):
        """获取底部工具栏文本"""
        # 检查是否有 HIL 任务（不频繁检查，避免性能问题）
        try:
            hil_task = self._check_hil_task()
            if hil_task.get("found"):
                return HTML(
                    f'<style bg="ansired" fg="ansiwhite"> 🔔 {self.t("toolbar_hil")} </style>'
                )
        except:
            pass
        
        return HTML(
            f'<style bg="ansiblue" fg="ansiwhite"> 💡 {self.t("toolbar")} </style>'
        )
    
    def run(self):
        """运行交互式 CLI"""
        self.show_banner()
        
        # 询问用户选择权限模式
        print("\n" + "="*80)
        print(f"🔐 {self.t('select_mode')}")
        print("="*80)
        print(f"1. {self.t('auto_mode')}")
        print(f"2. {self.t('manual_mode')}")
        print("="*80)
        
        while self.auto_mode is None:
            mode_input = input(f"{self.t('invalid_choice')} [1/2] ({self.t('default')}: 2): ").strip()
            if not mode_input or mode_input == '2':
                self.auto_mode = False
                print(f"✅ {self.t('mode_set_manual')}\n")
            elif mode_input == '1':
                self.auto_mode = True
                print(f"✅ {self.t('mode_set_auto')}\n")
            else:
                print(f"❌ {self.t('invalid_choice')} 1 {self.t('default')} 2\n")
        
        # 使用 prompt_toolkit（如果可用）
        if PROMPT_TOOLKIT_AVAILABLE:
            # 创建自动补全
            agent_completions = ['@' + agent for agent in self.available_agents]
            completer = WordCompleter(
                agent_completions + ['/quit', '/exit', '/help', '/agents', '/resume', '/zh', '/en'],
                ignore_case=True,
                sentence=True
            )
            
            session = PromptSession(
                completer=completer,
                bottom_toolbar=self.get_bottom_toolbar
            )
        
        while True:
            try:
                # 检查是否有待处理的 HIL 任务（由后台线程检测到的）
                if self.pending_hil:
                    hil_task = self.pending_hil
                    self.pending_hil = None  # 清除标志
                    self.hil_processing = True  # 标记正在处理，避免后台线程重复检测
                    
                    # 进入 HIL 响应模式
                    hil_id = hil_task["hil_id"]
                    instruction = hil_task["instruction"]
                    
                    # 显示 HIL 任务信息
                    self._show_hil_prompt(hil_id, instruction)
                    
                    # 等待用户响应
                    if PROMPT_TOOLKIT_AVAILABLE:
                        with patch_stdout():
                            user_response = session.prompt(f"[{self.current_agent}] HIL响应 > ").strip()
                    else:
                        user_response = input(f"[{self.current_agent}] HIL响应 > ").strip()
                    
                    if not user_response:
                        print(f"⚠️  {self.t('response_empty')}")
                        self.pending_hil = hil_task  # 恢复任务，下次继续处理
                        self.hil_processing = False  # 清除处理标志
                        continue
                    
                    if user_response == '/skip':
                        print(f"⏭️  {self.t('hil_skipped')}\n")
                        self.hil_processing = False  # 清除处理标志
                        continue
                    
                    # 提交响应
                    if self._respond_hil_task(hil_id, user_response):
                        print(f"✅ {self.t('hil_responded')}")
                        print(f"   {self.t('content')}: {user_response[:100]}{'...' if len(user_response) > 100 else ''}\n")
                    else:
                        print(f"❌ {self.t('hil_response_failed')}\n")
                    
                    self.hil_processing = False  # 清除处理标志，允许检测新的 HIL 任务
                    continue
                
                # 检查是否有待处理的工具确认请求
                if self.pending_tool_confirmation:
                    tool_confirmation = self.pending_tool_confirmation
                    self.pending_tool_confirmation = None  # 清除标志
                    self.tool_confirmation_processing = True  # 标记正在处理
                    
                    # 获取确认信息
                    confirm_id = tool_confirmation["confirm_id"]
                    tool_name = tool_confirmation["tool_name"]
                    arguments = tool_confirmation["arguments"]
                    
                    # 显示工具确认界面
                    self._show_tool_confirmation_prompt(confirm_id, tool_name, arguments)
                    
                    # 等待用户选择
                    if PROMPT_TOOLKIT_AVAILABLE:
                        with patch_stdout():
                            user_choice = session.prompt(f"[{self.current_agent}] 确认 [yes/no] > ").strip().lower()
                    else:
                        user_choice = input(f"[{self.current_agent}] 确认 [yes/no] > ").strip().lower()
                    
                    if not user_choice:
                        print(f"⚠️  {self.t('invalid_choice_yn')}")
                        self.pending_tool_confirmation = tool_confirmation  # 恢复任务
                        self.tool_confirmation_processing = False
                        continue
                    
                    # 处理用户选择
                    if user_choice in ['yes', 'y']:
                        # 批准执行
                        if self._respond_tool_confirmation(confirm_id, True):
                            print(f"✅ {self.t('tool_approved')}: {tool_name}\n")
                        else:
                            print(f"❌ {self.t('hil_response_failed')}\n")
                    elif user_choice in ['no', 'n']:
                        # 拒绝执行
                        if self._respond_tool_confirmation(confirm_id, False):
                            print(f"❌ {self.t('tool_rejected')}: {tool_name}\n")
                        else:
                            print(f"❌ {self.t('hil_response_failed')}\n")
                    else:
                        print(f"⚠️  {self.t('invalid_choice_yn')}")
                        self.pending_tool_confirmation = tool_confirmation  # 恢复任务
                        self.tool_confirmation_processing = False
                        continue
                    
                    self.tool_confirmation_processing = False
                    continue
                
                # 正常模式：显示提示符
                if PROMPT_TOOLKIT_AVAILABLE:
                    # 使用 patch_stdout 确保任务输出不影响输入
                    with patch_stdout():
                        user_input = session.prompt(f"[{self.current_agent}] > ").strip()
                else:
                    user_input = input(f"[{self.current_agent}] > ").strip()
                
                if not user_input:
                    continue
                
                # 处理管理命令（优先处理，不受待处理任务影响）
                if user_input in ['/quit', '/exit', '/q']:
                    # 停止 HIL 检查线程
                    self.stop_hil_checker = True
                    
                    # 终止运行中的任务
                    if self.current_process and self.current_process.poll() is None:
                        print("\n⏹️  正在停止运行中的任务...")
                        try:
                            if sys.platform == 'win32':
                                self.current_process.send_signal(signal.CTRL_BREAK_EVENT)
                                try:
                                    self.current_process.wait(timeout=2)
                                except subprocess.TimeoutExpired:
                                    self.current_process.terminate()
                                    self.current_process.wait(timeout=1)
                            else:
                                self.current_process.terminate()
                                self.current_process.wait(timeout=3)
                            print("✅ 任务已停止")
                        except (subprocess.TimeoutExpired, ProcessLookupError):
                            try:
                                self.current_process.kill()
                                print("✅ 任务已强制终止")
                            except (ProcessLookupError, PermissionError):
                                pass
                    print("\n👋 再见！\n")
                    break
                
                if user_input == '/help':
                    # 清屏并重新显示 banner
                    os.system('clear' if os.name != 'nt' else 'cls')
                    print(self.get_banner_text())
                    continue
                
                if user_input == '/agents':
                    print("\n📋 可用 Agents:")
                    for i, agent in enumerate(self.available_agents, 1):
                        mark = " (当前)" if agent == self.current_agent else ""
                        print(f"  {i}. {agent}{mark}")
                    print()
                    continue
                
                if user_input == '/resume':
                    # 恢复中断的任务
                    print(f"\n🔍 {self.t('checking_task')}")
                    interrupted = self._get_interrupted_task()
                    
                    if not interrupted["found"]:
                        print(f"❌ {interrupted['message']}\n")
                        continue
                    
                    # 显示任务信息
                    print(f"\n{'='*80}")
                    print(f"📋 {self.t('task_found')}")
                    print(f"{'='*80}")
                    print(f"🤖 {self.t('agent')}: {interrupted['agent_name']}")
                    print(f"📝 {self.t('task')}: {interrupted['user_input'][:100]}{'...' if len(interrupted['user_input']) > 100 else ''}")
                    print(f"⏸️  {self.t('interrupted_at')}: {interrupted['interrupted_at']}")
                    print(f"📊 {self.t('stack_depth')}: {interrupted['stack_depth']}")
                    print(f"{'='*80}\n")
                    
                    # 确认恢复
                    confirm = input(f"{self.t('resume_confirm')} ").strip().lower()
                    if confirm not in ['y', 'yes']:
                        print(f"⏭️  {self.t('resume_cancelled')}\n")
                        continue
                    
                    # 恢复任务
                    print(f"\n▶️  {self.t('resuming_task')}\n")
                    self.run_task(interrupted['agent_name'], interrupted['user_input'])
                    continue
                
                if user_input == '/zh':
                    # 切换到中文
                    self.language = 'zh'
                    print("\n✅ 已切换到中文\n")
                    continue
                
                if user_input == '/en':
                    # 切换到英文
                    self.language = 'en'
                    print("\n✅ Switched to English\n")
                    continue
                
                # 在执行新任务前，检查是否有待处理的 HIL 或工具确认
                # 防止用户不小心输入内容而不是按回车处理待处理任务
                if self.pending_hil or self.pending_tool_confirmation:
                    print("\n" + "="*80)
                    print(f"⚠️  {self.t('pending_task_warning')}")
                    print("="*80)
                    if self.pending_hil:
                        print(f"📌 {self.t('hil_pending')}")
                    if self.pending_tool_confirmation:
                        print(f"📌 {self.t('tool_confirm_pending')}")
                    print("="*80)
                    print(f"💡 {self.t('press_enter_hint')}")
                    print("="*80 + "\n")
                    continue
                
                # 解析输入
                agent_name, task = self.parse_input(user_input)
                
                if agent_name and task:
                    # 在任务末尾添加时间戳
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    task_with_timestamp = f"{task} [时间: {timestamp}]"
                    
                    # 执行任务
                    self.run_task(agent_name, task_with_timestamp)
                
            except KeyboardInterrupt:
                # Ctrl+C: 终止当前任务但不退出 CLI
                if self.current_process and self.current_process.poll() is None:
                    print("\n\n⚠️  正在中断任务...")
                    try:
                        if sys.platform == 'win32':
                            # Windows: 发送 Ctrl+Break 信号
                            self.current_process.send_signal(signal.CTRL_BREAK_EVENT)
                            try:
                                self.current_process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                self.current_process.terminate()
                                try:
                                    self.current_process.wait(timeout=1)
                                except (subprocess.TimeoutExpired, ProcessLookupError):
                                    self.current_process.kill()
                        else:
                            # Unix/Mac: 使用 terminate
                            self.current_process.terminate()
                            try:
                                self.current_process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                self.current_process.kill()
                    except Exception:
                        try:
                            self.current_process.kill()
                        except (ProcessLookupError, PermissionError):
                            pass
                    print("✅ 任务已中断\n")
                    print("💡 输入/resume回车可续跑，输入新内容开始新任务\n")
                else:
                    print("\n\n💡 没有运行中的任务。输入 /quit 退出 CLI\n")
                continue
            except EOFError:
                # Ctrl+D: 退出
                # 停止 HIL 检查线程
                self.stop_hil_checker = True
                
                if self.current_process and self.current_process.poll() is None:
                    print("\n\n⏹️  正在停止运行中的任务...")
                    try:
                        if sys.platform == 'win32':
                            self.current_process.send_signal(signal.CTRL_BREAK_EVENT)
                            try:
                                self.current_process.wait(timeout=2)
                            except subprocess.TimeoutExpired:
                                self.current_process.terminate()
                                self.current_process.wait(timeout=1)
                        else:
                            self.current_process.terminate()
                            self.current_process.wait(timeout=3)
                    except (subprocess.TimeoutExpired, ProcessLookupError, PermissionError):
                        try:
                            self.current_process.kill()
                        except (ProcessLookupError, PermissionError):
                            pass
                print("\n\n👋 再见！\n")
                break


def get_available_agent_systems():
    """获取可用的 Agent 系统列表"""
    try:
        # 查找 config/agent_library/ 目录
        project_root = Path(__file__).parent.parent
        agent_library_dir = project_root / "config" / "agent_library"
        
        if not agent_library_dir.exists():
            return ["Test_agent"]
        
        # 获取所有子目录作为可用系统
        systems = []
        for item in agent_library_dir.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                systems.append(item.name)
        
        return sorted(systems) if systems else ["Test_agent"]
    
    except Exception:
        return ["Test_agent"]


def start_cli_mode(agent_system: str = None, language: str = 'en'):
    """启动交互式 CLI 模式"""
    # task_id = 当前目录
    task_id = os.path.abspath(os.getcwd())
    
    # 如果没有指定 agent_system，让用户选择
    if agent_system is None:
        available_systems = get_available_agent_systems()
        
        print("\n" + "="*80)
        print(f"🤖 {t('select_agent_system', language)}")
        print("="*80)
        
        for i, system in enumerate(available_systems, 1):
            print(f"{i}. {system}")
        
        print("="*80)
        
        while True:
            choice = input(f"{t('invalid_choice', language)} [1-{len(available_systems)}] ({t('default', language)}: 1): ").strip()
            
            if not choice:
                agent_system = available_systems[0]
                break
            
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(available_systems):
                    agent_system = available_systems[idx]
                    break
                else:
                    print(f"❌ {t('invalid_choice', language)} 1-{len(available_systems)}\n")
            except ValueError:
                print(f"❌ {t('invalid_choice', language)} 1-{len(available_systems)}\n")
        
        if language == 'zh':
            print(f"✅ 已选择: {agent_system}\n")
        else:
            print(f"✅ Selected: {agent_system}\n")
    
    cli = InteractiveCLI(task_id, agent_system)
    cli.language = language  # 设置语言
    cli.run()


if __name__ == "__main__":
    start_cli_mode()

