#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件操作工具
"""

from pathlib import Path
from typing import Dict, Any, Optional
import shutil
import chardet


class BaseTool:
    """工具基类"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """执行工具"""
        raise NotImplementedError


def get_abs_path(task_id: str, relative_path: str) -> Path:
    """
    将相对路径转换为绝对路径
    
    Args:
        task_id: workspace 绝对路径
        relative_path: 相对路径
        
    Returns:
        绝对路径
    """
    workspace = Path(task_id)
    # 移除开头的 / 或 ./
    rel_path = str(relative_path).lstrip('/').lstrip('./')
    return workspace / rel_path


def detect_encoding(file_path: Path) -> str:
    """检测文件编码"""
    try:
        with open(file_path, 'rb') as f:
            raw_data = f.read(10240)
        result = chardet.detect(raw_data)
        encoding = result.get('encoding', 'utf-8')
        return encoding or 'utf-8'
    except Exception:
        return 'utf-8'


# 常见二进制文件后缀名（黑名单）
BINARY_EXTENSIONS = {
    # 图片
    '.png', '.jpg', '.jpeg', '.gif', '.bmp', '.ico', '.svg', '.webp', '.tiff', '.tif',
    # 文档
    '.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    # 压缩文件
    '.zip', '.tar', '.gz', '.bz2', '.rar', '.7z', '.xz',
    # 可执行文件
    '.exe', '.dll', '.so', '.dylib', '.bin', '.app',
    # 音视频
    '.mp3', '.mp4', '.avi', '.mov', '.wav', '.flac', '.mkv', '.webm',
    # 其他
    '.pyc', '.pyo', '.o', '.a', '.lib', '.class', '.jar'
}


def is_binary_file(file_path: Path) -> bool:
    """
    判断是否为二进制文件
    
    检测策略：
    1. 先检查文件后缀名（高效）
    2. 如果后缀名不在黑名单中，再检查文件内容（准确）
    """
    try:
        # 策略1：检查文件后缀名（快速路径）
        if file_path.suffix.lower() in BINARY_EXTENSIONS:
            return True
        
        # 策略2：检查文件内容（检查是否包含 null 字节）
        with open(file_path, 'rb') as f:
            chunk = f.read(1024)
        return b'\x00' in chunk
    except Exception:
        return False


class FileReadTool(BaseTool):
    """文件读取工具"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        读取文件内容（支持单个或多个文件）
        
        Parameters:
            path (list): 文件路径数组，单个文件传 ['file.txt']
            file_path (str or list): 同 path（兼容性参数）
            start_line (int, optional): 起始行号（从1开始）
            end_line (int, optional): 结束行号
            encoding (str, optional): 文件编码
            show_line_numbers (bool, optional): 是否显示行号，默认 True
        """
        try:
            # 兼容 path 和 file_path 两种参数名
            path = parameters.get("path") or parameters.get("file_path")
            
            if not path:
                return {
                    "status": "error",
                    "output": "",
                    "error": "Missing required parameter: 'path' or 'file_path'"
                }
            
            # 统一转换为列表
            if isinstance(path, str):
                # 兼容旧版本：如果是字符串，转换为单元素列表
                path = [path]
            elif not isinstance(path, list):
                return {
                    "status": "error",
                    "output": "",
                    "error": f"Invalid path type: {type(path).__name__}, expected list"
                }
            
            # 根据列表长度决定使用哪种模式
            if len(path) == 1:
                # 单文件模式
                return self._read_single_file(task_id, path[0], parameters)
            else:
                # 多文件模式
                return self._read_multiple_files(task_id, path, parameters)
            
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }
    
    def _read_single_file(self, task_id: str, path: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """读取单个文件"""
        start_line = parameters.get("start_line")
        end_line = parameters.get("end_line")
        encoding = parameters.get("encoding")
        show_line_numbers = parameters.get("show_line_numbers", True)
        
        abs_path = get_abs_path(task_id, path)
        
        # 检查文件是否存在
        if not abs_path.exists():
            return {
                "status": "error",
                "output": "",
                "error": f"File not found: {path}"
            }
        
        # 检查是否为二进制文件
        if is_binary_file(abs_path):
            return {
                "status": "error",
                "output": "",
                "error": f"Cannot read binary file: {path}. Use other tools to analyze the file."
            }
        
        # 自动检测编码
        if not encoding:
            encoding = detect_encoding(abs_path)
        
        # 读取文件
        try:
            with open(abs_path, 'r', encoding=encoding) as f:
                lines = f.readlines()
        except UnicodeDecodeError:
            # 如果指定编码失败，尝试 utf-8
            with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
        
        # 处理行范围
        start_idx = (start_line - 1) if start_line else 0
        end_idx = end_line if end_line else len(lines)
        selected_lines = lines[start_idx:end_idx]
        
        # 格式化输出
        if show_line_numbers:
            # 带行号格式
            import json
            output_lines = []
            for i, line in enumerate(selected_lines, start=start_idx + 1):
                output_lines.append({
                    "line": i,
                    "content": line.rstrip('\n\r')
                })
            content = json.dumps(output_lines, ensure_ascii=False, indent=2)
        else:
            # 纯文本格式
            content = ''.join(selected_lines)
        
        return {
            "status": "success",
            "output": content,
            "error": ""
        }
    
    def _read_multiple_files(self, task_id: str, paths: list, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """读取多个文件"""
        import json
        
        start_line = parameters.get("start_line")
        end_line = parameters.get("end_line")
        encoding = parameters.get("encoding")
        show_line_numbers = parameters.get("show_line_numbers", True)
        
        results = {}
        errors = []
        success_count = 0
        
        for path in paths:
            try:
                abs_path = get_abs_path(task_id, path)
                
                # 检查文件是否存在
                if not abs_path.exists():
                    errors.append(f"File not found: {path}")
                    results[path] = {
                        "status": "error",
                        "error": f"File not found: {path}"
                    }
                    continue
                
                # 检查是否为二进制文件
                if is_binary_file(abs_path):
                    errors.append(f"Cannot read binary file: {path}")
                    results[path] = {
                        "status": "error",
                        "error": f"Binary file, use other tools"
                    }
                    continue
                
                # 自动检测编码
                file_encoding = encoding or detect_encoding(abs_path)
                
                # 读取文件
                try:
                    with open(abs_path, 'r', encoding=file_encoding) as f:
                        lines = f.readlines()
                except UnicodeDecodeError:
                    with open(abs_path, 'r', encoding='utf-8', errors='ignore') as f:
                        lines = f.readlines()
                
                # 处理行范围
                start_idx = (start_line - 1) if start_line else 0
                end_idx = end_line if end_line else len(lines)
                selected_lines = lines[start_idx:end_idx]
                
                # 格式化内容
                if show_line_numbers:
                    output_lines = []
                    for i, line in enumerate(selected_lines, start=start_idx + 1):
                        output_lines.append({
                            "line": i,
                            "content": line.rstrip('\n\r')
                        })
                    content = output_lines  # 保持为列表，稍后统一序列化
                else:
                    content = ''.join(selected_lines)
                
                results[path] = {
                    "status": "success",
                    "content": content,
                    "total_lines": len(lines)
                }
                success_count += 1
                
            except Exception as e:
                errors.append(f"{path}: {str(e)}")
                results[path] = {
                    "status": "error",
                    "error": str(e)
                }
        
        # 构建输出
        output_data = {
            "total_files": len(paths),
            "success_count": success_count,
            "error_count": len(errors),
            "files": results
        }
        
        if errors:
            output_data["errors"] = errors
        
        return {
            "status": "success" if success_count > 0 else "error",
            "output": json.dumps(output_data, ensure_ascii=False, indent=2),
            "error": "\n".join(errors) if errors else ""
        }


class FileWriteTool(BaseTool):
    """文件写入工具"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        写入文件
        
        Parameters:
            path (str): 相对路径
            content (str): 文件内容
            mode (str, optional): 写入模式 'write'(覆盖) 或 'append'(追加)，默认 'write'
            start_line (int, optional): replace模式 - 起始行号
            end_line (int, optional): replace模式 - 结束行号
        """
        try:
            path = parameters.get("path")
            content = parameters.get("content", "")
            mode = parameters.get("mode", "write")
            start_line = parameters.get("start_line")
            end_line = parameters.get("end_line")
            
            # 禁止写入 reference.bib 文件
            # if path and path.endswith("reference.bib"):
            #     return {
            #         "status": "error",
            #         "output": "",
            #         "error": "禁止使用 file_write 工具写入 reference.bib 文件。请使用专门的参考文献管理工具：reference_add（添加）、reference_delete（删除）。"
            #     }
            
            abs_path = get_abs_path(task_id, path)
            
            # 确保父目录存在
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Replace line 模式
            if start_line is not None:
                if not abs_path.exists():
                    return {
                        "status": "error",
                        "output": "",
                        "error": f"File not found for line replacement: {path}"
                    }
                
                with open(abs_path, 'r', encoding='utf-8') as f:
                    lines = f.readlines()
                
                start_idx = start_line - 1
                end_idx = end_line if end_line else start_line
                
                # 替换指定行
                new_lines = lines[:start_idx] + [content + '\n'] + lines[end_idx:]
                
                with open(abs_path, 'w', encoding='utf-8') as f:
                    f.writelines(new_lines)
                
                return {
                    "status": "success",
                    "output": f"Replaced lines {start_line}-{end_idx} in {path}",
                    "error": ""
                }
            
            # 普通写入模式
            if mode == "append":
                with open(abs_path, 'a', encoding='utf-8') as f:
                    f.write(content)
                msg = f"Appended to {path}"
            else:
                with open(abs_path, 'w', encoding='utf-8') as f:
                    f.write(content)
                msg = f"Written to {path}"
            
            return {
                "status": "success",
                "output": msg,
                "error": ""
            }
            
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }


class DirListTool(BaseTool):
    """目录列表工具"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        列出目录内容
        
        Parameters:
            path (str): 相对路径，默认 '.'
            recursive (bool): 是否递归列出子目录，默认 False
        """
        try:
            path = parameters.get("path", ".")
            recursive = parameters.get("recursive", False)
            abs_path = get_abs_path(task_id, path)
            
            if not abs_path.exists():
                return {
                    "status": "error",
                    "output": "",
                    "error": f"Directory not found: {path}"
                }
            
            if not abs_path.is_dir():
                return {
                    "status": "error",
                    "output": "",
                    "error": f"Not a directory: {path}"
                }
            
            if recursive:
                items = self._list_recursive(abs_path, abs_path)
            else:
                items = []
                for item in sorted(abs_path.iterdir()):
                    item_type = "dir" if item.is_dir() else "file"
                    items.append(f"[{item_type}] {item.name}")
            
            output = "\n".join(items) if items else "(empty directory)"
            
            return {
                "status": "success",
                "output": output,
                "error": ""
            }
            
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }
    
    def _list_recursive(self, root: Path, current: Path, indent: int = 0) -> list:
        """递归列出目录内容，排除 code_env 目录"""
        items = []
        
        try:
            for item in sorted(current.iterdir()):
                # 跳过 code_env 目录
                if item.name == "code_env":
                    continue
                
                # 只显示当前项的名称（不带父路径）
                indent_str = "  " * indent
                item_type = "dir" if item.is_dir() else "file"
                items.append(f"{indent_str}[{item_type}] {item.name}")
                
                # 如果是目录且不是 code_env，递归处理
                if item.is_dir():
                    items.extend(self._list_recursive(root, item, indent + 1))
        except PermissionError:
            pass
        
        return items


class DirCreateTool(BaseTool):
    """创建目录工具"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        创建目录
        
        Parameters:
            path (str): 相对路径
        """
        try:
            path = parameters.get("path")
            abs_path = get_abs_path(task_id, path)
            
            abs_path.mkdir(parents=True, exist_ok=True)
            
            return {
                "status": "success",
                "output": f"Directory created: {path}",
                "error": ""
            }
            
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }


class FileMoveTool(BaseTool):
    """文件移动/复制工具"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        移动/重命名/复制文件
        
        Parameters:
            source (str): 源文件相对路径
            destination (str): 目标相对路径
            copy (bool): 是否复制（保留原文件），默认 False（移动）
        """
        try:
            sources = parameters.get("source")
            destination = parameters.get("destination")
            copy_mode = parameters.get("copy", False)
            dst_path = get_abs_path(task_id, destination)
            for source in sources:
            
                src_path = get_abs_path(task_id, source)
            
            
                if not src_path.exists():
                    return {
                        "status": "error",
                        "output": "",
                        "error": f"Source not found: {source}"
                    }
            
            # 确保目标目录存在
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                
                if copy_mode:
                    # 复制模式
                    if src_path.is_dir():
                        shutil.copytree(str(src_path), str(dst_path), dirs_exist_ok=True)
                    else:
                        shutil.copy2(str(src_path), str(dst_path))
                    action = "Copied"
                else:
                    # 移动模式
                    shutil.move(str(src_path), str(dst_path))
                    action = "Moved"
                
            return {
                    "status": "success",
                    "output": f"全部移动成功",
                    "error": ""
                }
            
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }


class FileDeleteTool(BaseTool):
    """文件删除工具"""
    
    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        """
        删除文件或目录
        
        Parameters:
            path (str): 相对路径
        """
        try:
            path = parameters.get("path")
            abs_path = get_abs_path(task_id, path)
            
            if not abs_path.exists():
                return {
                    "status": "error",
                    "output": "",
                    "error": f"Path not found: {path}"
                }
            
            if abs_path.is_dir():
                shutil.rmtree(abs_path)
            else:
                abs_path.unlink()
            
            return {
                "status": "success",
                "output": f"Deleted: {path}",
                "error": ""
            }
            
        except Exception as e:
            return {
                "status": "error",
                "output": "",
                "error": str(e)
            }

