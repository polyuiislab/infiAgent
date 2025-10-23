#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MLA V3 - Multi-Level Agent System
安装配置
"""

from setuptools import setup, find_packages
from setuptools.command.install import install
from setuptools.command.develop import develop
from setuptools.command.egg_info import egg_info
from pathlib import Path
import sys
import atexit

# 读取 README
readme_file = Path(__file__).parent / "README.md"
long_description = readme_file.read_text(encoding='utf-8') if readme_file.exists() else ""

# 读取依赖
requirements_file = Path(__file__).parent / "requirements.txt"
if requirements_file.exists():
    requirements = [
        line.strip() for line in requirements_file.read_text(encoding='utf-8').strip().split('\n')
        if line.strip() and not line.strip().startswith('#')
    ]
else:
    requirements = [
        "fastapi>=0.104.0",
        "uvicorn[standard]>=0.24.0",
        "pydantic>=2.0.0",
        "requests>=2.31.0",
        "pyyaml>=6.0.0",
        "litellm>=1.0.0",
        "tiktoken>=0.5.0",
        "crawl4ai>=0.3.0",
        "ddgs>=1.0.0",
        "arxiv>=2.0.0",
        "pdfplumber>=0.10.0",
        "python-docx>=1.1.0",
        "chardet>=5.2.0",
        "prompt_toolkit>=3.0.0",
    ]


def _print_path_info():
    """打印PATH配置信息 - 通用函数"""
    try:
        import os
        import site
        
        # 获取用户级 Scripts 目录
        if sys.platform == 'win32':
            user_base = site.USER_BASE
            if user_base:
                scripts_dir = os.path.join(user_base, 'Scripts')
            else:
                return
        else:
            user_base = site.USER_BASE
            if user_base:
                scripts_dir = os.path.join(user_base, 'bin')
            else:
                return
        
        if not os.path.exists(scripts_dir):
            return
            
        # 检查是否在 PATH 中
        path_env = os.environ.get('PATH', '')
        path_dirs = path_env.split(os.pathsep)
        scripts_dir_normalized = os.path.normpath(scripts_dir).lower()
        in_path = any(os.path.normpath(p).lower() == scripts_dir_normalized for p in path_dirs)
        
        if not in_path:
            print("\n" + "="*80, file=sys.stderr)
            print("[!] 重要提示：命令行工具 PATH 配置", file=sys.stderr)
            print("="*80, file=sys.stderr)
            print(f"\n为了能够直接使用 'mla-agent' 命令，需要将以下目录添加到 PATH：", file=sys.stderr)
            print(f"\n  {scripts_dir}\n", file=sys.stderr)
            
            if sys.platform == 'win32':
                print("Windows 配置方法（选择其一）：\n", file=sys.stderr)
                print("方法1：PowerShell 命令（推荐）", file=sys.stderr)
                print("-" * 80, file=sys.stderr)
                print(f'  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")', file=sys.stderr)
                print(f'  [Environment]::SetEnvironmentVariable("Path", "$userPath;{scripts_dir}", "User")\n', file=sys.stderr)
                
                print("方法2：系统设置", file=sys.stderr)
                print("-" * 80, file=sys.stderr)
                print("  系统属性 -> 高级 -> 环境变量 -> 用户变量 Path -> 新建", file=sys.stderr)
                print(f"  添加: {scripts_dir}\n", file=sys.stderr)
            else:
                shell = os.environ.get('SHELL', '')
                if 'zsh' in shell:
                    rc_file = '~/.zshrc'
                elif 'bash' in shell:
                    rc_file = '~/.bashrc'
                else:
                    rc_file = '~/.profile'
                print(f"Mac/Linux 配置方法：在 {rc_file} 中添加：", file=sys.stderr)
                print(f'  export PATH="{scripts_dir}:$PATH"\n', file=sys.stderr)
            
            print("="*80, file=sys.stderr)
            print("[TIP] 不想配置 PATH？可以使用: python check_path.py", file=sys.stderr)
            print("      或直接运行: python start.py --help", file=sys.stderr)
            print("="*80 + "\n", file=sys.stderr)
    except Exception:
        pass


class CustomEggInfo(egg_info):
    """自定义 egg_info 命令，在最后显示PATH提示"""
    def run(self):
        egg_info.run(self)
        # egg_info 是最后执行的命令之一，在这里显示提示
        if self.dry_run:
            return
        _print_path_info()


class PostInstallCommand(install):
    """自定义安装命令，安装后显示 PATH 提示"""
    def run(self):
        install.run(self)
        # 在安装完成后输出PATH配置提示
        self._show_path_message()
    
    def _show_path_message(self):
        """显示PATH配置消息"""
        import os
        import site
        
        # 获取用户级 Scripts 目录  
        if sys.platform == 'win32':
            user_base = site.USER_BASE
            if user_base:
                scripts_dir = os.path.join(user_base, 'Scripts')
            else:
                return
        else:
            user_base = site.USER_BASE
            if user_base:
                scripts_dir = os.path.join(user_base, 'bin')
            else:
                return
        
        if not os.path.exists(scripts_dir):
            return
            
        # 检查是否在 PATH 中
        path_env = os.environ.get('PATH', '')
        path_dirs = path_env.split(os.pathsep)
        scripts_dir_normalized = os.path.normpath(scripts_dir).lower()
        in_path = any(os.path.normpath(p).lower() == scripts_dir_normalized for p in path_dirs)
        
        if not in_path:
            msg = f"""
================================================================================
[重要] mla-agent 安装完成！但需要配置 PATH 才能直接使用命令
================================================================================

要直接使用 'mla-agent' 命令，需要将以下目录添加到 PATH：

  {scripts_dir}

"""
            if sys.platform == 'win32':
                msg += f"""Windows 配置方法（选择其一）：

方法1：PowerShell 命令（推荐，立即生效）
--------------------------------------------------------------------------------
  $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
  [Environment]::SetEnvironmentVariable("Path", "$userPath;{scripts_dir}", "User")

执行后重启终端即可使用 mla-agent 命令

方法2：系统设置（图形界面）
--------------------------------------------------------------------------------
  1. Win+R 输入: sysdm.cpl
  2. 高级 -> 环境变量 -> 用户变量 -> Path -> 编辑 -> 新建
  3. 添加: {scripts_dir}

"""
            else:
                shell = os.environ.get('SHELL', '')
                if 'zsh' in shell:
                    rc_file = '~/.zshrc'
                elif 'bash' in shell:
                    rc_file = '~/.bashrc'
                else:
                    rc_file = '~/.profile'
                msg += f"""Mac/Linux 配置方法：
--------------------------------------------------------------------------------
在 {rc_file} 文件末尾添加：

  export PATH="{scripts_dir}:$PATH"

然后执行: source {rc_file}

"""
            
            msg += """================================================================================
[提示] 不想配置 PATH？可以运行: python check_path.py 查看详细说明
       或直接使用: python start.py --help
================================================================================
"""
            # 使用 announce 确保消息被显示
            self.announce(msg, level=3)  # level=3 是 WARN 级别，会被显示
            print(msg)  # 同时也直接打印
        
        # 提示安装 Playwright 浏览器（crawl4ai 依赖）
        self._show_playwright_message()
    
    def _show_playwright_message(self):
        """提示安装 Playwright 浏览器"""
        try:
            import crawl4ai
            # 如果 crawl4ai 已安装，提示安装浏览器
            msg = """
================================================================================
[重要] 网页爬取功能需要安装浏览器驱动
================================================================================

mla-agent 使用 crawl4ai 进行网页爬取，需要安装 Chromium 浏览器驱动。

安装命令：
  playwright install chromium

或者安装所有浏览器（可选）：
  playwright install

首次使用网页爬取功能前，请务必执行上述命令！

================================================================================
"""
            print(msg)
        except ImportError:
            # crawl4ai 还未安装，跳过提示
            pass


class PostDevelopCommand(develop):
    """自定义开发模式安装命令，安装后显示 PATH 提示"""
    def run(self):
        develop.run(self)
        # 使用和 install 相同的提示
        PostInstallCommand._show_path_message(self)


# 移除 atexit（在pip子进程中不生效）
# atexit.register(_print_path_info)

setup(
    name="mla-agent",
    version="3.0.0",
    author="Chenglin Yu",
    author_email="yuchenglin96@qq.com",
    description="Multi-Level Agent System for complex task automation",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/ChenglinPoly/Multi-Level-Agent",
    packages=find_packages(exclude=['test*', 'task_*', 'conversations']),
    py_modules=['start'],
    include_package_data=True,
    package_data={
        'MLA_V3': [
            'config/**/*.yaml',
            'tool_server_lite/**/*.py',
            'tool_server_lite/**/*.md',
            'tool_server_lite/requirements.txt',
        ],
    },
    install_requires=requirements,
    python_requires='>=3.9',
    entry_points={
        'console_scripts': [
            'mla-agent=start:main',
            'mla-tool-server=tool_server_lite.server:main',
        ],
    },
    cmdclass={
        'egg_info': CustomEggInfo,
        'install': PostInstallCommand,
        'develop': PostDevelopCommand,
    },
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Topic :: Scientific/Engineering :: Artificial Intelligence',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.9',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
    ],
)

# 在setup执行完成后显示中文提示（仅在安装时，而非导入时）
if __name__ == '__main__':
    try:
        import os
        post_install_file = Path(__file__).parent / "POST_INSTALL_ZH.txt"
        if post_install_file.exists():
            content = post_install_file.read_text(encoding='utf-8')
            # 输出到 stderr 确保被看到
            print(content, file=sys.stderr)
    except Exception:
        pass

