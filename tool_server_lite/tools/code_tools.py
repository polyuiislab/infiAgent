#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代码执行工具
"""

from pathlib import Path
from typing import Dict, Any, Tuple
import subprocess
import sys
from .file_tools import BaseTool, get_abs_path

#def _create_venv(self, venv_path: Path) -> Tuple[bool, str]:重复两遍要记得同时维护。
#为了美观，def _create_venv还是重复写两次吧，这样一个一个类比较独立。


class ExecuteCodeTool(BaseTool):
    """代码执行工具（支持虚拟环境）"""
    
    def _create_venv(self, venv_path: Path) -> Tuple[bool, str]:
        """
        创建虚拟环境（兼容 Anaconda 和标准 Python）
        
        策略：
        1. 优先尝试标准 venv（CPython）
        2. 失败时尝试 virtualenv（兼容 Anaconda）
        3. 都失败返回 False
        
        Returns:
            (是否成功创建, 错误信息)
        """
        # 创建父目录
        venv_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 策略 1: 标准 venv
        try:
            result = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_path)],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL  # 修复 Bad file descriptor
            )
            if result.returncode == 0:
                return True, ""
            venv_error = result.stderr or result.stdout
        except Exception as e:
            venv_error = str(e)
        
        # 策略 2: virtualenv（兼容 Anaconda）
        try:
            result = subprocess.run(
                [sys.executable, "-m", "virtualenv", str(venv_path)],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL  # 修复 Bad file descriptor
            )
            if result.returncode == 0:
                return True, ""
            virtualenv_error = result.stderr or result.stdout
        except Exception as e:
            virtualenv_error = str(e)
        
        # 都失败了，返回详细错误
        error_msg = f"venv: {venv_error[:200]}, virtualenv: {virtualenv_error[:200]}"
        return False, error_msg
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行代码
        
        Parameters:
            language (str): 编程语言 'python' 或 'bash'
            code (str, optional): 代码内容
            file_path (str, optional): 代码文件相对路径
            working_dir (str, optional): 执行目录相对路径，默认为code_run目录
            use_venv (bool, optional): 是否使用虚拟环境（仅Python），默认True
            timeout (int, optional): 超时时间（秒），默认30
        """
        try:
            language = parameters.get("language", "python")
            code = parameters.get("code")
            file_path = parameters.get("file_path")
            working_dir = parameters.get("working_dir", "code_run")
            use_venv = parameters.get("use_venv", True)
            timeout = parameters.get("timeout", 30)
            
            workspace = Path(task_id)
            exec_dir = get_abs_path(task_id, working_dir)
            
            # 准备代码文件
            if code:
                # 创建临时文件
                code_dir = workspace / "code_run"
                code_dir.mkdir(parents=True, exist_ok=True)
                
                if language == "python":
                    temp_file = code_dir / "temp_code.py"
                else:
                    temp_file = code_dir / "temp_code.sh"
                
                with open(temp_file, 'w', encoding='utf-8') as f:
                    f.write(code)
                
                exec_file = temp_file
            elif file_path:
                exec_file = get_abs_path(task_id, file_path)
                if not exec_file.exists():
                    return {
                        "status": "error",
                        "output": "",
                        "error": f"Code file not found: {file_path}"
                    }
            else:
                return {
                    "status": "error",
                    "output": "",
                    "error": "Either 'code' or 'file_path' must be provided"
                }
            
            # 执行代码
            if language == "python":
                output = self._execute_python(workspace, exec_file, exec_dir, use_venv, timeout)
            elif language == "bash":
                output = self._execute_bash(exec_file, exec_dir, timeout)
            else:
                return {
                    "status": "error",
                    "output": "",
                    "error": f"Unsupported language: {language}"
                }
            
            return {
                "status": "success",
                "output": output,
                "error": ""
            }
            
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "output": "",
                "error": f"Execution timeout ({timeout}s)"
            }
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }
    
    def _execute_python(self, workspace: Path, code_file: Path, exec_dir: Path, use_venv: bool, timeout: int) -> str:
        """执行Python代码"""
        env_dir = workspace / "code_env"
        
        if use_venv:
            # 检查虚拟环境
            venv_path = env_dir / "venv"
            if not venv_path.exists():
                # 创建虚拟环境（兼容 Anaconda 和标准 Python）
                venv_created, error_msg = self._create_venv(venv_path)
                if not venv_created:
                    # 创建失败，回退到系统 Python（并记录警告）
                    print(f"⚠️ venv 创建失败，使用系统 Python: {error_msg[:100]}")
                    python_exec = sys.executable
                else:
                    # 使用虚拟环境的 Python
                    if sys.platform == "win32":
                        python_exec = venv_path / "Scripts" / "python.exe"
                    else:
                        python_exec = venv_path / "bin" / "python"
            else:
                # 虚拟环境已存在
                if sys.platform == "win32":
                    python_exec = venv_path / "Scripts" / "python.exe"
                else:
                    python_exec = venv_path / "bin" / "python"
        else:
            python_exec = sys.executable
        
        # 执行代码
        result = subprocess.run(
            [str(python_exec), str(code_file)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(exec_dir),
            stdin=subprocess.DEVNULL  # 修复 "Bad file descriptor" 错误
        )
        
        output = result.stdout
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr}"
        if result.returncode != 0:
            output += f"\nExit code: {result.returncode}"
        
        return output
    
    def _execute_bash(self, code_file: Path, exec_dir: Path, timeout: int) -> str:
        """执行Shell脚本（跨平台）"""
        import sys
        
        if sys.platform == "win32":
            # Windows: 使用PowerShell或cmd
            # 将.sh文件转换为.bat或使用PowerShell执行
            shell_cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-File", str(code_file)]
        else:
            # Unix/Linux/Mac: 使用bash
            shell_cmd = ["bash", str(code_file)]
        
        result = subprocess.run(
            shell_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(exec_dir),
            stdin=subprocess.DEVNULL  # 修复 "Bad file descriptor" 错误
        )
        
        output = result.stdout
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr}"
        if result.returncode != 0:
            output += f"\nExit code: {result.returncode}"
        
        return output


class PipInstallTool(BaseTool):
    """pip 包安装工具"""
    
    def _create_venv(self, venv_path: Path) -> Tuple[bool, str]:
        """
        创建虚拟环境（兼容 Anaconda 和标准 Python）
        与 ExecuteCodeTool 使用相同的策略
        
        Returns:
            (是否成功创建, 错误信息)
        """
        venv_path.parent.mkdir(parents=True, exist_ok=True)
        
        # 策略 1: 标准 venv
        try:
            result = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_path)],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL  # 修复 Bad file descriptor
            )
            if result.returncode == 0:
                return True, ""
            venv_error = result.stderr or result.stdout
        except Exception as e:
            venv_error = str(e)
        
        # 策略 2: virtualenv（兼容 Anaconda）
        try:
            result = subprocess.run(
                [sys.executable, "-m", "virtualenv", str(venv_path)],
                capture_output=True,
                text=True,
                timeout=60,
                stdin=subprocess.DEVNULL  # 修复 Bad file descriptor
            )
            if result.returncode == 0:
                return True, ""
            virtualenv_error = result.stderr or result.stdout
        except Exception as e:
            virtualenv_error = str(e)
        
        # 都失败了，返回详细错误
        error_msg = f"venv: {venv_error[:200]}, virtualenv: {virtualenv_error[:200]}"
        return False, error_msg
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        在虚拟环境中安装 Python 包
        
        Parameters:
            packages (list or str): 要安装的包列表或单个包名
            timeout (int, optional): 超时时间（秒），默认300
        """
        try:
            packages = parameters.get("packages")
            timeout = parameters.get("timeout", 300)
            
            if not packages:
                return {
                    "status": "error",
                    "output": "",
                    "error": "packages is required"
                }
            
            # 转换为列表
            if isinstance(packages, str):
                packages = [packages]
            
            workspace = Path(task_id)
            env_dir = workspace / "code_env"
            venv_path = env_dir / "venv"
            
            # 检查并创建虚拟环境
            if not venv_path.exists():
                venv_created, error_msg = self._create_venv(venv_path)
                if not venv_created:
                    return {
                        "status": "error",
                        "output": "",
                        "error": f"无法创建虚拟环境: {error_msg}"
                    }
            
            # 获取虚拟环境的 pip
            if sys.platform == "win32":
                pip_exec = venv_path / "Scripts" / "pip.exe"
            else:
                pip_exec = venv_path / "bin" / "pip"
            
            # 安装包
            results = []
            for package in packages:
                result = subprocess.run(
                    [str(pip_exec), "install", package],
                    capture_output=True,
                    text=True,
                    timeout=timeout
                )
                
                if result.returncode == 0:
                    results.append(f"✅ {package}: installed")
                else:
                    results.append(f"❌ {package}: {result.stderr.strip()[:200]}")
            
            output = "\n".join(results)
            
            return {
                "status": "success",
                "output": output,
                "error": ""
            }
            
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "output": "",
                "error": f"Installation timeout ({timeout}s)"
            }
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }


class ExecuteCommandTool(BaseTool):
    """命令行执行工具"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行命令行命令
        
        Parameters:
            command (str): 要执行的命令
            working_dir (str, optional): 工作目录相对路径，默认为workspace根目录
            timeout (int, optional): 超时时间（秒），默认30
        """
        try:
            command = parameters.get("command")
            working_dir = parameters.get("working_dir", ".")
            timeout = parameters.get("timeout", 30)
            
            abs_working_dir = get_abs_path(task_id, working_dir)
            
            if not abs_working_dir.exists():
                return {
                    "status": "error",
                    "output": "",
                    "error": f"Working directory not found: {working_dir}"
                }
            
            # 执行命令
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=str(abs_working_dir)
            )
            
            output = result.stdout
            if result.stderr:
                output += f"\nSTDERR:\n{result.stderr}"
            if result.returncode != 0:
                output += f"\nExit code: {result.returncode}"
            
            return {
                "status": "success",
                "output": output,
                "error": ""
            }
            
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "output": "",
                "error": f"Command timeout ({timeout}s)"
            }
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }

