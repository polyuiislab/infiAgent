#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一工具运行时注册中心。

职责：
1. 提供 built-in 工具注册表（single source of truth）
2. 扫描用户目录中的自定义 Python 工具并尝试加载
3. 为 direct-tools 与 legacy HTTP tool server 提供同一份运行时注册表
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from contextlib import contextmanager
import importlib.util
import inspect
import sys
import traceback
import types
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from utils.user_paths import get_user_tools_library_root
from tool_server_lite.tools.file_tools import BaseTool


ToolFactory = Callable[[], Any]


_EXTERNAL_TOOL_RUNNER_CODE = r"""
from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import inspect
import json
import sys
import traceback
import types
from pathlib import Path


class BaseTool:
    def execute(self, task_id, parameters):
        raise NotImplementedError


def get_abs_path(task_id, relative_path):
    workspace = Path(task_id)
    rel_text = str(relative_path or "").strip()
    if rel_text == "":
        rel_text = "."
    if rel_text.startswith("/"):
        rel_text = rel_text.lstrip("/")
    elif rel_text.startswith("./"):
        rel_text = rel_text[2:]
    return workspace / rel_text


def install_framework_shims():
    infiagent_module = types.ModuleType("infiagent")
    infiagent_module.BaseTool = BaseTool
    sys.modules.setdefault("infiagent", infiagent_module)

    file_tools_module = types.ModuleType("tool_server_lite.tools.file_tools")
    file_tools_module.BaseTool = BaseTool
    file_tools_module.get_abs_path = get_abs_path

    tools_module = types.ModuleType("tool_server_lite.tools")
    tools_module.BaseTool = BaseTool
    tools_module.get_abs_path = get_abs_path
    tools_module.file_tools = file_tools_module

    package_module = types.ModuleType("tool_server_lite")
    package_module.tools = tools_module

    sys.modules.setdefault("tool_server_lite", package_module)
    sys.modules.setdefault("tool_server_lite.tools", tools_module)
    sys.modules.setdefault("tool_server_lite.tools.file_tools", file_tools_module)


def discover_tool_class(module):
    candidates = []
    for _, obj in inspect.getmembers(module):
        if not inspect.isclass(obj):
            continue
        if obj.__module__ != module.__name__:
            continue
        if obj.__name__.startswith("_"):
            continue
        if hasattr(obj, "execute") or hasattr(obj, "execute_async"):
            candidates.append(obj)
    if not candidates:
        raise ValueError("未找到符合约定的工具类（需要公开类并实现 execute 或 execute_async）")
    if len(candidates) > 1:
        raise ValueError("检测到多个候选工具类，请保证每个文件只定义一个公开工具类")
    return candidates[0]


def run(payload):
    file_path = Path(payload["file_path"]).expanduser().resolve()
    custom_root = Path(payload["custom_root"]).expanduser().resolve()
    task_id = str(payload.get("task_id") or "")
    parameters = payload.get("parameters") or {}

    for root in (str(file_path.parent), str(custom_root)):
        if root and root not in sys.path:
            sys.path.insert(0, root)

    install_framework_shims()
    module_name = f"mla_external_tool_{file_path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {file_path.name} 创建模块规范")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tool_cls = discover_tool_class(module)
    tool = tool_cls()

    if hasattr(tool, "execute_async") and callable(getattr(tool, "execute_async")):
        result = asyncio.run(tool.execute_async(task_id, parameters))
    else:
        result = tool.execute(task_id, parameters)
    return result


def main():
    original_stdout = sys.stdout
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        with contextlib.redirect_stdout(sys.stderr):
            result = run(payload)
        original_stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False, default=str))
    except Exception as exc:
        original_stdout.write(json.dumps({
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
"""


def _custom_tools_execution_mode() -> str:
    raw = os.environ.get("MLA_CUSTOM_TOOLS_EXECUTION_MODE", "external").strip().lower()
    return raw if raw in {"external", "in_process"} else "external"


def _external_tool_python_commands() -> List[List[str]]:
    raw = os.environ.get("MLA_EXTERNAL_TOOL_PYTHON", "").strip()
    if raw:
        import shlex
        parts = shlex.split(raw)
        if parts:
            return [parts]
    return [["python"], ["python3"]]


def _format_external_tool_python() -> str:
    return " or ".join(" ".join(cmd) for cmd in _external_tool_python_commands())


def _build_builtin_factories() -> Dict[str, ToolFactory]:
    from tool_server_lite.tools import (
        FileReadTool,
        FileWriteTool,
        DirListTool,
        DirCreateTool,
        FileMoveTool,
        FileDeleteTool,
        WebSearchTool,
        YouWebSearchTool,
        GoogleScholarSearchTool,
        ArxivSearchTool,
        CrawlPageTool,
        FileDownloadTool,
        ParseDocumentTool,
        VisionTool,
        ImageReadTool,
        CreateImageTool,
        AudioTool,
        PaperAnalyzeTool,
        MarkdownToPdfTool,
        MarkdownToDocxTool,
        TexToPdfTool,
        HumanInLoopTool,
        ExecuteCommandTool,
        GrepTool,
        ReferenceListTool,
        ReferenceAddTool,
        ReferenceDeleteTool,
        WriteExperienceTool,
        ImagesToPptTool,
        LoadSkillTool,
        OffloadSkillTool,
        FreshTool,
        AddMessageTool,
        StartBackgroundTaskTool,
        TaskShareContextPathTool,
        ListTaskIdsTool,
        TaskHistorySearchTool,
    )

    factories: Dict[str, ToolFactory] = {
        "file_read": FileReadTool,
        "file_write": FileWriteTool,
        "dir_list": DirListTool,
        "dir_create": DirCreateTool,
        "file_move": FileMoveTool,
        "file_delete": FileDeleteTool,
        "web_search": WebSearchTool,
        "youcom_search": YouWebSearchTool,
        "google_scholar_search": GoogleScholarSearchTool,
        "arxiv_search": ArxivSearchTool,
        "crawl_page": CrawlPageTool,
        "file_download": FileDownloadTool,
        "parse_document": ParseDocumentTool,
        "vision_tool": VisionTool,
        "image_read": ImageReadTool,
        "create_image": CreateImageTool,
        "audio_tool": AudioTool,
        "paper_analyze_tool": PaperAnalyzeTool,
        "md_to_pdf": MarkdownToPdfTool,
        "md_to_docx": MarkdownToDocxTool,
        "tex_to_pdf": TexToPdfTool,
        "human_in_loop": HumanInLoopTool,
        "execute_command": ExecuteCommandTool,
        "grep": GrepTool,
        "reference_list": ReferenceListTool,
        "reference_add": ReferenceAddTool,
        "reference_delete": ReferenceDeleteTool,
        "write_experience": WriteExperienceTool,
        "images_to_ppt": ImagesToPptTool,
        "load_skill": LoadSkillTool,
        "offload_skill": OffloadSkillTool,
        "fresh": FreshTool,
        "add_message": AddMessageTool,
        "start_background_task": StartBackgroundTaskTool,
        "task_share_context_path": TaskShareContextPathTool,
        "list_task_ids": ListTaskIdsTool,
        "task_history_search": TaskHistorySearchTool,
    }

    try:
        from tool_server_lite.tools import (
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
            BrowserScrollTool,
        )

        factories.update({
            "browser_launch": BrowserLaunchTool,
            "browser_close": BrowserCloseTool,
            "browser_new_page": BrowserNewPageTool,
            "browser_switch_page": BrowserSwitchPageTool,
            "browser_close_page": BrowserClosePageTool,
            "browser_list_pages": BrowserListPagesTool,
            "browser_navigate": BrowserNavigateTool,
            "browser_snapshot": BrowserSnapshotTool,
            "browser_execute_js": BrowserExecuteJsTool,
            "browser_click": BrowserClickTool,
            "browser_type": BrowserTypeTool,
            "browser_wait": BrowserWaitTool,
            "browser_mouse_move": BrowserMouseMoveTool,
            "browser_mouse_click_coords": BrowserMouseClickCoordsTool,
            "browser_drag_and_drop": BrowserDragAndDropTool,
            "browser_hover": BrowserHoverTool,
            "browser_scroll": BrowserScrollTool,
        })
    except ImportError:
        pass

    return factories


def get_builtin_tool_factories() -> Dict[str, ToolFactory]:
    return dict(_build_builtin_factories())


def _discover_helper_module_names(search_roots: List[Path]) -> List[str]:
    names = set()
    for root in search_roots:
        if not root.exists() or not root.is_dir():
            continue
        for child in root.iterdir():
            if child.name.startswith("."):
                continue
            if child.is_file() and child.suffix == ".py":
                names.add(child.stem)
            elif child.is_dir() and (child / "__init__.py").exists():
                names.add(child.name)
    return sorted(names)


@contextmanager
def _temporary_tool_import_context(search_roots: List[Path]):
    inserted_paths: List[str] = []
    for root in [str(path) for path in search_roots if path and path.exists()]:
        if root not in sys.path:
            sys.path.insert(0, root)
            inserted_paths.append(root)

    helper_names = _discover_helper_module_names(search_roots)
    backups: Dict[str, types.ModuleType] = {}
    backup_prefixes: Dict[str, types.ModuleType] = {}

    for name in helper_names:
        for module_name in list(sys.modules.keys()):
            if module_name == name or module_name.startswith(f"{name}."):
                target = sys.modules.pop(module_name)
                if module_name == name:
                    backups[module_name] = target
                else:
                    backup_prefixes[module_name] = target

    try:
        yield
    finally:
        for name in helper_names:
            for module_name in list(sys.modules.keys()):
                if module_name == name or module_name.startswith(f"{name}."):
                    sys.modules.pop(module_name, None)

        for module_name, module in backup_prefixes.items():
            sys.modules[module_name] = module
        for module_name, module in backups.items():
            sys.modules[module_name] = module

        for root in inserted_paths:
            while root in sys.path:
                sys.path.remove(root)


def _load_module_from_path(file_path: Path, *, custom_root: Path) -> types.ModuleType:
    module_name = f"mla_custom_tool_{file_path.stem}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为 {file_path.name} 创建模块规范")
    module = importlib.util.module_from_spec(spec)
    with _temporary_tool_import_context([file_path.parent, custom_root]):
        spec.loader.exec_module(module)
    return module


def _is_valid_custom_tool_class(obj: Any, module_name: str) -> bool:
    if not inspect.isclass(obj):
        return False
    if obj.__module__ != module_name:
        return False
    if obj is BaseTool:
        return False
    if obj.__name__.startswith("_"):
        return False
    if not (hasattr(obj, "execute") or hasattr(obj, "execute_async")):
        return False
    return True


def _discover_custom_tool_class(module: types.ModuleType) -> type:
    candidates = [
        obj for _, obj in inspect.getmembers(module)
        if _is_valid_custom_tool_class(obj, module.__name__)
    ]
    if not candidates:
        raise ValueError("未找到符合约定的工具类（需要公开类并实现 execute 或 execute_async）")
    if len(candidates) > 1:
        raise ValueError("检测到多个候选工具类，请保证每个文件只定义一个公开工具类")
    return candidates[0]


def _instantiate_custom_tool(file_path: Path, *, custom_root: Path) -> Tuple[str, Any, Dict[str, Any]]:
    module = _load_module_from_path(file_path, custom_root=custom_root)
    tool_cls = _discover_custom_tool_class(module)
    tool = tool_cls()

    tool_name = getattr(tool, "name", None) or getattr(tool_cls, "name", None) or file_path.stem
    tool_name = str(tool_name).strip()
    if not tool_name:
        raise ValueError("工具名称为空，请设置类属性 name 或使用合法文件名")

    metadata = {
        "name": tool_name,
        "source": "custom",
        "path": str(file_path),
        "class_name": tool_cls.__name__,
        "status": "loaded",
        "error": "",
    }
    return tool_name, tool, metadata


def _select_custom_tool_entry_file(tool_dir: Path, py_files: List[Path]) -> Path:
    main_candidate = tool_dir / f"{tool_dir.name}.py"
    if main_candidate.exists() and main_candidate.is_file():
        return main_candidate
    if len(py_files) == 1:
        return py_files[0]
    raise ValueError(
        "工具目录中存在多个 Python 文件；请将主工具文件命名为与目录同名的 <tool_name>.py，"
        "其他 Python 文件可作为共享 helper 模块。"
    )


def _extract_custom_tool_name_from_ast(file_path: Path, fallback: str) -> Tuple[str, str]:
    """Best-effort static discovery so custom tools need not import in backend Python."""
    try:
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    except Exception:
        return fallback, ""

    candidates: List[Tuple[str, str]] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
            continue
        has_execute = any(
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name in {"execute", "execute_async"}
            for child in node.body
        )
        if not has_execute:
            continue

        declared_name = ""
        for child in node.body:
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if isinstance(target, ast.Name) and target.id == "name":
                        if isinstance(child.value, ast.Constant) and isinstance(child.value.value, str):
                            declared_name = child.value.value.strip()
                            break
                if declared_name:
                    break
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name) and child.target.id == "name":
                if isinstance(child.value, ast.Constant) and isinstance(child.value.value, str):
                    declared_name = child.value.value.strip()
                    break
        candidates.append((declared_name or fallback, node.name))

    if len(candidates) == 1:
        return candidates[0]
    return fallback, ""


class ExternalPythonToolProxy(BaseTool):
    """Proxy a custom tool into the user's default Python environment."""

    def __init__(self, *, tool_name: str, file_path: Path, custom_root: Path, class_name: str = ""):
        self.name = str(tool_name or file_path.stem)
        self.file_path = Path(file_path).expanduser().resolve()
        self.custom_root = Path(custom_root).expanduser().resolve()
        self.class_name = str(class_name or "")

    def execute(self, task_id: str, parameters: Dict[str, Any]) -> Dict[str, Any]:
        payload = {
            "file_path": str(self.file_path),
            "custom_root": str(self.custom_root),
            "task_id": task_id,
            "parameters": parameters or {},
        }
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        completed = None
        last_missing = ""
        try:
            for python_cmd in _external_tool_python_commands():
                cmd = python_cmd + ["-c", _EXTERNAL_TOOL_RUNNER_CODE]
                try:
                    completed = subprocess.run(
                        cmd,
                        input=json.dumps(payload, ensure_ascii=False, default=str),
                        capture_output=True,
                        text=True,
                        timeout=int(os.environ.get("MLA_EXTERNAL_TOOL_TIMEOUT_SEC", "3600") or "3600"),
                        env=env,
                    )
                    break
                except FileNotFoundError:
                    last_missing = " ".join(python_cmd)
                    continue
            if completed is None:
                raise FileNotFoundError(last_missing or _format_external_tool_python())
        except FileNotFoundError:
            return {
                "status": "error",
                "output": "",
                "error": f"External tool Python not found: {_format_external_tool_python()}",
            }
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "output": "",
                "error": "External tool execution timeout",
            }

        stdout = (completed.stdout or "").strip()
        stderr = (completed.stderr or "").strip()
        if completed.returncode != 0 and not stdout:
            return {
                "status": "error",
                "output": "",
                "error": stderr or f"External tool process exited with code {completed.returncode}",
            }

        try:
            data = json.loads(stdout)
        except Exception:
            return {
                "status": "error",
                "output": stdout,
                "error": (
                    "External tool returned non-JSON output"
                    + (f": {stderr}" if stderr else "")
                ),
            }

        if not data.get("ok"):
            detail = str(data.get("error") or "External tool failed")
            tb = str(data.get("traceback") or "")
            if tb:
                detail = f"{detail}\n{tb}"
            return {"status": "error", "output": stderr, "error": detail}

        result = data.get("result")
        if isinstance(result, dict):
            return result
        return {"status": "success", "output": str(result), "error": ""}


_registry_cache: Dict[str, Any] | None = None
_registry_metadata_cache: Dict[str, Dict[str, Any]] | None = None
_registry_failures_cache: List[Dict[str, Any]] | None = None


def build_runtime_registry() -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    registry: Dict[str, Any] = {}
    metadata: Dict[str, Dict[str, Any]] = {}
    failures: List[Dict[str, Any]] = []

    for name, factory in get_builtin_tool_factories().items():
        try:
            registry[name] = factory()
            metadata[name] = {
                "name": name,
                "source": "builtin",
                "path": "",
                "class_name": type(registry[name]).__name__,
                "status": "loaded",
                "error": "",
            }
        except Exception as exc:
            failures.append({
                "name": name,
                "source": "builtin",
                "path": "",
                "status": "error",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })

    custom_root = get_user_tools_library_root()
    custom_root.mkdir(parents=True, exist_ok=True)
    for tool_dir in sorted(custom_root.iterdir()):
        if not tool_dir.is_dir() or tool_dir.name.startswith((".", "_")):
            continue
        py_files = [p for p in sorted(tool_dir.glob("*.py")) if p.is_file()]
        if not py_files:
            failures.append({
                "name": tool_dir.name,
                "source": "custom",
                "path": str(tool_dir),
                "status": "error",
                "error": "工具目录中未找到 Python 文件",
                "traceback": "",
            })
            continue

        file_path: Path = tool_dir
        try:
            file_path = _select_custom_tool_entry_file(tool_dir, py_files)
            if _custom_tools_execution_mode() == "in_process":
                tool_name, tool, tool_meta = _instantiate_custom_tool(file_path, custom_root=custom_root)
            else:
                tool_name, class_name = _extract_custom_tool_name_from_ast(file_path, tool_dir.name)
                tool_name = str(tool_name or tool_dir.name).strip()
                if not tool_name:
                    raise ValueError("工具名称为空，请设置类属性 name 或使用合法目录名")
                tool = ExternalPythonToolProxy(
                    tool_name=tool_name,
                    file_path=file_path,
                    custom_root=custom_root,
                    class_name=class_name,
                )
                tool_meta = {
                    "name": tool_name,
                    "source": "custom_external",
                    "path": str(file_path),
                    "class_name": class_name,
                    "status": "loaded",
                    "error": "",
                    "python": _format_external_tool_python(),
                }
            if tool_name in registry:
                raise ValueError(f"工具名冲突: {tool_name}")
            registry[tool_name] = tool
            metadata[tool_name] = tool_meta
        except Exception as exc:
            failures.append({
                "name": tool_dir.name,
                "source": "custom",
                "path": str(file_path),
                "status": "error",
                "error": str(exc),
                "traceback": traceback.format_exc(),
            })

    return registry, metadata, failures


def reload_runtime_registry() -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    global _registry_cache, _registry_metadata_cache, _registry_failures_cache
    _registry_cache, _registry_metadata_cache, _registry_failures_cache = build_runtime_registry()
    return _registry_cache, _registry_metadata_cache, _registry_failures_cache


def get_runtime_registry(force_reload: bool = False) -> Dict[str, Any]:
    global _registry_cache
    if force_reload or _registry_cache is None:
        reload_runtime_registry()
    return dict(_registry_cache or {})


def get_runtime_registry_metadata(force_reload: bool = False) -> Dict[str, Dict[str, Any]]:
    global _registry_metadata_cache
    if force_reload or _registry_metadata_cache is None:
        reload_runtime_registry()
    return dict(_registry_metadata_cache or {})


def get_runtime_registry_failures(force_reload: bool = False) -> List[Dict[str, Any]]:
    global _registry_failures_cache
    if force_reload or _registry_failures_cache is None:
        reload_runtime_registry()
    return list(_registry_failures_cache or [])
