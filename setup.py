#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MLA V3 - Multi-Level Agent System
安装配置
"""

from setuptools import setup, find_packages
from pathlib import Path
import re

# 读取 README
readme_file = Path(__file__).parent / "README.md"
long_description = readme_file.read_text(encoding='utf-8') if readme_file.exists() else ""
long_description = re.sub(r'!\[([^\]]*)\]\((assets/[^)]+)\)', r'![\1](https://raw.githubusercontent.com/polyuiislab/infiAgent/main/\2)', long_description)
long_description = re.sub(r'\]\(((?:docs/|README|LICENSE)[^)]+|LICENSE)\)', r'](https://github.com/polyuiislab/infiAgent/blob/main/\1)', long_description)
long_description = re.sub(r'src="(assets/[^"]+)"', r'src="https://raw.githubusercontent.com/polyuiislab/infiAgent/main/\1"', long_description)
long_description = re.sub(r'href="(README[^"]+)"', r'href="https://github.com/polyuiislab/infiAgent/blob/main/\1"', long_description)
long_description = re.sub(r'\]\(((?:config/|CHANGELOG)[^)]+)\)', r'](https://github.com/polyuiislab/infiAgent/blob/main/\1)', long_description)


# 读取依赖
requirements_file = Path(__file__).parent / "requirements.txt"
if requirements_file.exists():
    raw_requirements = [
        line.strip() for line in requirements_file.read_text(encoding='utf-8').strip().split('\n')
        if line.strip() and not line.strip().startswith('#')
    ]
else:
    raw_requirements = []

requirements = []
seen = set()
for req in raw_requirements:
    normalized = req.split(";")[0].strip().lower()
    if normalized.startswith("pytest"):
        continue
    if normalized in seen:
        continue
    seen.add(normalized)
    requirements.append(req)


def _collect_data_files(base_dir: Path, install_prefix: str):
    files = []
    if not base_dir.exists():
        return files
    project_root = Path(__file__).parent
    for path in base_dir.rglob("*"):
        if not path.is_file():
            continue
        relative_parent = path.parent.relative_to(base_dir)
        target_dir = Path(install_prefix) / relative_parent
        files.append((str(target_dir), [str(path.relative_to(project_root))]))
    return files


data_files = []
project_root = Path(__file__).parent
data_files.extend(_collect_data_files(project_root / "config", "config"))
data_files.extend(_collect_data_files(project_root / "skills", "skills"))

setup(
    name="infiagent",
    version="3.13.0",
    author="Chenglin Yu",
    author_email="yuchenglin96@qq.com",
    license="GPL-3.0-only",
    description="InfiAgent multi-agent framework for long-running task automation",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/polyuiislab/infiAgent",
    packages=find_packages(
        include=[
            'core',
            'infiagent',
            'infiagent.runtime',
            'services',
            'tool_server_lite',
            'tool_server_lite.tools',
            'utils',
        ],
    ),
    py_modules=['start'],
    include_package_data=True,
    package_data={
        'infiagent.runtime': ['static/*.html', 'static/*.css', 'static/*.js', 'static/*.png'],
        'tool_server_lite': ['requirements.txt'],
        'tool_server_lite.tools': ['*.md', '*.txt'],
    },
    data_files=data_files,
    license_files=("LICENSE",),
    install_requires=requirements,
    python_requires='>=3.10',
    entry_points={
        'console_scripts': [
            'infiagent=start:main',
            'mla-agent=start:main',
            'infiagent-runtime=infiagent.runtime.server:main',
        ],
    },
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Topic :: Scientific/Engineering :: Artificial Intelligence',
        'License :: OSI Approved :: GNU General Public License v3 (GPLv3)',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
    ],
)
