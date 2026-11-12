#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Task 运行时辅助：
- 读取 task 的可恢复元数据
- 当目标 task 不在运行时，按 fresh 语义重载配置并后台 resume
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import traceback
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from datetime import datetime

from core.hierarchy_manager import get_hierarchy_manager
from utils.mcp_manager import reload_mcp_tools
from utils.skill_loader import reset_skill_loader
from utils.runtime_control import get_running_task, is_task_running
from utils.user_paths import (
    get_user_conversations_dir,
    get_user_runtime_logs_dir,
    runtime_env_scope,
)
from utils.windows_compat import safe_print

_launch_lock = threading.Lock()
_launching_tasks: set[str] = set()


def _task_file_prefix(task_id: str) -> str:
    task_hash = hashlib.md5(task_id.encode("utf-8")).hexdigest()[:8]
    task_name = Path(task_id).name or "task"
    return f"{task_hash}_{task_name}"


def load_task_resume_meta(task_id: str, fallback_agent_system: Optional[str] = None) -> Tuple[Optional[Dict], str]:
    task_id = str(task_id or "").strip()
    if not task_id:
        return None, "缺少 task_id"

    manager = get_hierarchy_manager(task_id)
    stack = manager._load_stack()
    if not stack:
        return None, f"任务 {task_id} 没有可恢复的栈，无法 resume。"

    bottom = stack[0] or {}
    context = manager._load_context()
    runtime_meta = context.get("runtime", {})
    if not isinstance(runtime_meta, dict):
        runtime_meta = {}

    agent_name = str(bottom.get("agent_name") or runtime_meta.get("agent_name") or "").strip()
    user_input = str(bottom.get("user_input") or runtime_meta.get("user_input") or "").strip()
    agent_system = str(runtime_meta.get("agent_system") or fallback_agent_system or "").strip()

    if not agent_name:
        return None, f"任务 {task_id} 的恢复信息缺少 agent_name。"
    if not user_input:
        return None, f"任务 {task_id} 的恢复信息缺少 user_input。"
    if not agent_system:
        return None, f"任务 {task_id} 的恢复信息缺少 agent_system。"

    return {
        "task_id": task_id,
        "agent_name": agent_name,
        "user_input": user_input,
        "agent_system": agent_system,
    }, ""


def load_task_launch_meta(
    task_id: str,
    fallback_agent_system: Optional[str] = None,
    fallback_agent_name: Optional[str] = None,
) -> Tuple[Optional[Dict], str]:
    """
    读取“可新启动”所需的最小 task 元信息。

    与 resume 不同，这里不要求 stack 非空。
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        return None, "缺少 task_id"

    manager = get_hierarchy_manager(task_id)
    runtime_meta = manager.get_runtime_metadata()
    if not isinstance(runtime_meta, dict):
        runtime_meta = {}

    agent_name = str(runtime_meta.get("agent_name") or fallback_agent_name or "alpha_agent").strip()
    agent_system = str(runtime_meta.get("agent_system") or fallback_agent_system or "OpenCowork").strip()

    if not agent_name:
        return None, f"任务 {task_id} 的启动信息缺少 agent_name。"
    if not agent_system:
        return None, f"任务 {task_id} 的启动信息缺少 agent_system。"

    return {
        "task_id": task_id,
        "agent_name": agent_name,
        "agent_system": agent_system,
    }, ""


def _parse_env_flag(value: Any) -> Optional[bool]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return None


def _positive_int_or_none(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return max(1, int(value))
    except Exception:
        return None


def _resolve_action_window_config(config: Dict[str, Any]) -> Optional[int]:
    values = [
        _positive_int_or_none(config.get("action_window_steps")),
        _positive_int_or_none(config.get("thinking_steps")),
        _positive_int_or_none(config.get("thinking_interval")),
    ]
    values = [value for value in values if value is not None]
    if not values:
        return None
    return max(values)


def _build_launch_config_from_env_overrides(env_overrides: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    overrides = dict(env_overrides or {})
    config: Dict[str, Any] = {}

    mapping = {
        "MLA_USER_DATA_ROOT": "user_data_root",
        "MLA_LLM_CONFIG_PATH": "llm_config_path",
        "MLA_AGENT_LIBRARY_DIR": "agent_library_dir",
        "MLA_SKILLS_LIBRARY_DIR": "skills_dir",
        "MLA_TOOLS_LIBRARY_DIR": "tools_dir",
    }
    for env_key, config_key in mapping.items():
        value = overrides.get(env_key)
        if value:
            config[config_key] = value

    action_window_values = [
        _positive_int_or_none(overrides.get("MLA_ACTION_WINDOW_STEPS")),
        _positive_int_or_none(overrides.get("MLA_THINKING_STEPS")),
        _positive_int_or_none(overrides.get("MLA_THINKING_INTERVAL")),
    ]
    action_window_values = [value for value in action_window_values if value is not None]
    if action_window_values:
        action_window_steps = max(action_window_values)
        config["action_window_steps"] = action_window_steps
        config["thinking_steps"] = action_window_steps
        config["thinking_interval"] = action_window_steps

    int_mapping = {
        "MLA_MAX_TURNS": "max_turns",
        "MLA_FRESH_INTERVAL_SEC": "fresh_interval_sec",
        "MLA_AUTO_RESUME_ATTEMPTS": "auto_resume_attempts",
    }
    for env_key, config_key in int_mapping.items():
        value = overrides.get(env_key)
        if value is None or value == "":
            continue
        try:
            config[config_key] = int(value)
        except Exception:
            pass

    fresh_enabled = _parse_env_flag(overrides.get("MLA_FRESH_ENABLED"))
    if fresh_enabled is not None:
        config["fresh_enabled"] = fresh_enabled

    seed_builtin = _parse_env_flag(overrides.get("MLA_SEED_BUILTIN_RESOURCES"))
    if seed_builtin is not None:
        config["seed_builtin_resources"] = seed_builtin

    json_mapping = {
        "MLA_TOOL_RUNTIME_DEFAULTS_JSON": "tool_runtime_defaults",
        "MLA_TOOL_HOOKS_JSON": "tool_hooks",
        "MLA_CONTEXT_HOOKS_JSON": "context_hooks",
    }
    for env_key, config_key in json_mapping.items():
        value = overrides.get(env_key)
        if not value:
            continue
        try:
            config[config_key] = json.loads(value)
        except Exception:
            pass

    mcp_config = overrides.get("MLA_MCP_CONFIG_JSON")
    if mcp_config:
        try:
            parsed = json.loads(mcp_config)
            if isinstance(parsed, dict) and isinstance(parsed.get("servers"), list):
                config["mcp_servers"] = parsed["servers"]
        except Exception:
            pass

    log_mode = str(overrides.get("MLA_TOOL_RUNTIME_DEFAULTS_LOG_MODE") or "").strip().lower()
    if log_mode:
        config["tool_runtime_defaults_log_mode"] = log_mode

    return config


def resume_task_with_fresh(
    task_id: str,
    reason: str = "",
    fallback_agent_system: Optional[str] = None,
    direct_tools: bool = True,
    env_overrides: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    meta, error = load_task_resume_meta(task_id, fallback_agent_system=fallback_agent_system)
    if not meta:
        return False, error

    with _launch_lock:
        if task_id in _launching_tasks:
            return True, f"任务 {task_id} 已在后台启动中。"
        _launching_tasks.add(task_id)

    def _runner():
        try:
            with runtime_env_scope(env_overrides):
                from tool_server_lite.registry import reload_runtime_registry
                reset_skill_loader()
                reload_runtime_registry()
                reload_mcp_tools()

                from core.agent_executor import AgentExecutor
                from utils.config_loader import ConfigLoader

                config_loader = ConfigLoader(meta["agent_system"])
                hierarchy_manager = get_hierarchy_manager(meta["task_id"])

                agent_config = config_loader.get_tool_config(meta["agent_name"])
                if agent_config.get("type") != "llm_call_agent":
                    safe_print(
                        f"❌ 后台恢复失败: {meta['agent_name']} 不是 LLM Agent "
                        f"(task_id={meta['task_id']})"
                    )
                    return

                safe_print(
                    f"🔄 后台恢复任务: {meta['task_id']} | "
                    f"system={meta['agent_system']} | agent={meta['agent_name']} | reason={reason or 'fresh request'}"
                )

                agent = AgentExecutor(
                    agent_name=meta["agent_name"],
                    agent_config=agent_config,
                    config_loader=config_loader,
                    hierarchy_manager=hierarchy_manager,
                    direct_tools=direct_tools,
                )
                agent.run(meta["task_id"], meta["user_input"])
        except Exception as e:
            safe_print(f"❌ 后台恢复任务失败: {task_id} -> {e}")
            traceback.print_exc()
        finally:
            with _launch_lock:
                _launching_tasks.discard(task_id)

    thread = threading.Thread(
        target=_runner,
        daemon=True,
        name=f"mla-resume-{abs(hash(task_id)) % 1000000}"
    )
    thread.start()

    return True, f"目标任务未在运行，已重载配置并在后台 resume: {task_id}"


def append_task_message(
    task_id: str,
    message: str,
    source: str = "agent",
    resume_if_needed: bool = False,
    fallback_agent_system: Optional[str] = None,
    direct_tools: bool = True,
    env_overrides: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict]:
    task_id = str(task_id or "").strip()
    message = str(message or "").strip()
    if not task_id:
        return False, {"error": "缺少 task_id"}
    if not message:
        return False, {"error": "缺少 message"}

    manager = get_hierarchy_manager(task_id)
    instruction_id = manager.append_instruction(message, dedupe=False, source=source)
    stack = manager._load_stack()
    payload = {
        "task_id": task_id,
        "instruction_id": instruction_id,
        "share_context_path": str(manager.context_file),
        "stack_path": str(manager.stack_file),
        "running": is_task_running(task_id),
        "resumed": False,
        "launched": False,
    }

    if payload["running"]:
        payload["message"] = "消息已追加，目标 task 将在下一轮上下文构建时看到该消息。"
        return True, payload

    if resume_if_needed:
        if stack:
            ok, resume_msg = resume_task_with_fresh(
                task_id=task_id,
                reason=f"resume after add_message: {message[:80]}",
                fallback_agent_system=fallback_agent_system,
                direct_tools=direct_tools,
                env_overrides=env_overrides,
            )
            payload["resumed"] = ok
            payload["message"] = resume_msg
            if not ok:
                payload["error"] = resume_msg
                return False, payload
            return True, payload

        launch_meta, launch_error = load_task_launch_meta(
            task_id,
            fallback_agent_system=fallback_agent_system,
        )
        if not launch_meta:
            payload["message"] = launch_error
            payload["error"] = launch_error
            return False, payload

        ok, launch_payload = launch_task_process(
            task_id=task_id,
            user_input=message,
            agent_system=launch_meta["agent_system"],
            agent_name=launch_meta["agent_name"],
            config=_build_launch_config_from_env_overrides(env_overrides),
            force_new=False,
            direct_tools=direct_tools,
        )
        payload["launched"] = ok
        payload["message"] = launch_payload.get("message") if isinstance(launch_payload, dict) else ""
        if isinstance(launch_payload, dict):
            payload.update({
                "pid": launch_payload.get("pid"),
                "log_path": launch_payload.get("log_path", ""),
                "agent_system": launch_payload.get("agent_system", launch_meta["agent_system"]),
                "agent_name": launch_payload.get("agent_name", launch_meta["agent_name"]),
            })
        if not ok:
            payload["error"] = (launch_payload or {}).get("error") if isinstance(launch_payload, dict) else "后台启动失败"
            return False, payload
        return True, payload

    payload["message"] = "消息已追加，但目标 task 当前未运行。"
    return True, payload


def get_task_share_paths(task_id: str) -> Dict[str, str]:
    manager = get_hierarchy_manager(str(task_id or "").strip())
    return {
        "task_id": manager.task_id,
        "share_context_path": str(manager.context_file),
        "stack_path": str(manager.stack_file),
    }


def list_known_tasks(only_running: bool = False) -> Dict[str, list]:
    conversations_dir = get_user_conversations_dir()
    tasks = []
    seen = set()

    if conversations_dir.exists():
        for path in sorted(conversations_dir.glob("*_share_context.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            task_id = str(data.get("task_id") or "").strip()
            if not task_id or task_id in seen:
                continue
            seen.add(task_id)
            running = is_task_running(task_id)
            if only_running and not running:
                continue
            task_path = Path(task_id)
            tasks.append({
                "task_id": task_id,
                "task_name": task_path.name or task_id,
                "share_context_path": str(path),
                "stack_path": str(get_hierarchy_manager(task_id).stack_file),
                "running": running,
            })

    tasks.sort(key=lambda item: (not item["running"], item["task_name"], item["task_id"]))
    return {"tasks": tasks}


def reset_task_state(
    task_id: str,
    preserve_history: bool = True,
    kill_background_processes: bool = True,
    reason: str = "",
) -> Tuple[bool, Dict[str, Any]]:
    task_id = str(task_id or "").strip()
    if not task_id:
        return False, {"error": "缺少 task_id"}

    killed_pid = None
    running_meta = get_running_task(task_id)
    if running_meta:
        pid = int(running_meta.get('pid') or 0)
        expected = running_meta.get('process_created')
        if not kill_background_processes or pid == os.getpid() or expected is None:
            return False, {'error': 'Task is running; stop it before resetting', 'code': 'TASK_RUNNING'}
        import psutil
        try:
            process = psutil.Process(pid)
            if abs(process.create_time() - float(expected)) >= .001:
                return False, {'error': 'Process identity changed', 'code': 'PROCESS_CHANGED'}
            process.terminate()
            process.wait(timeout=3)
            killed_pid = pid
        except psutil.NoSuchProcess:
            pass
        except (psutil.AccessDenied, psutil.TimeoutExpired):
            return False, {'error': 'Task did not stop; state was not reset', 'code': 'TASK_RUNNING'}

    from utils.task_owner import task_owner
    with task_owner(task_id) as owned:
        if not owned:
            return False, {'error': 'Task is owned by another run', 'code': 'TASK_RUNNING'}
        return _reset_idle_task_state(task_id, preserve_history, kill_background_processes, reason, killed_pid)


def _reset_idle_task_state(task_id, preserve_history, kill_background_processes, reason, killed_pid):
    manager = get_hierarchy_manager(task_id)
    with manager._state_lock():
        context = manager._load_context()
        if not isinstance(context, dict):
            context = {"task_id": task_id, "runtime": {}, "current": {}, "history": []}

        current = context.get("current", {})
        if not isinstance(current, dict):
            current = {}

        if preserve_history and (
            current.get("instructions")
            or current.get("hierarchy")
            or current.get("agents_status")
        ):
            history_entry = {
                "archived_at": datetime.now().isoformat(),
                "type": "task_reset",
                "reason": reason or "manual reset",
                "instructions": current.get("instructions", []),
                "hierarchy": current.get("hierarchy", {}),
                "agents_status": current.get("agents_status", {}),
                "start_time": current.get("start_time", ""),
                "last_updated": current.get("last_updated", ""),
            }
            history = context.get("history", [])
            if not isinstance(history, list):
                history = []
            history.append(history_entry)
            context["history"] = history

        runtime = context.get("runtime", {})
        if not isinstance(runtime, dict):
            runtime = {}
        runtime["last_reset_at"] = datetime.now().isoformat()
        runtime["last_reset_reason"] = reason or "manual reset"
        context["runtime"] = runtime
        context["current"] = manager._new_current_context()
        manager._save_context(context)
        manager._save_stack([])

    conversations_dir = get_user_conversations_dir()
    prefix = _task_file_prefix(task_id)
    removed_action_files = []
    for path in conversations_dir.glob(f"{prefix}_*_actions.json"):
        try:
            path.unlink()
            removed_action_files.append(str(path))
        except FileNotFoundError:
            pass

    from utils.atomic_state import write_json
    import uuid
    write_json(Path(task_id) / '.sdk_runtime' / 'generation.json', {'id': uuid.uuid4().hex})
    return True, {
        "task_id": task_id,
        "preserve_history": bool(preserve_history),
        "kill_background_processes": bool(kill_background_processes),
        "killed_pid": killed_pid,
        "share_context_path": str(manager.context_file),
        "stack_path": str(manager.stack_file),
        "removed_action_files": removed_action_files,
        "message": f"已重置任务状态: {task_id}",
    }


def _as_root_dir(path: Optional[str], expected_leaf: Optional[str] = None) -> Optional[str]:
    if not path:
        return None
    p = Path(path).expanduser().resolve()
    if expected_leaf and p.name == expected_leaf:
        return str(p.parent)
    return str(p)


def _runtime_logs_dir() -> Path:
    path = get_user_runtime_logs_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def launch_task_process(
    *,
    task_id: str,
    user_input: str,
    agent_system: str = "OpenCowork",
    agent_name: str = "alpha_agent",
    config: Optional[Dict] = None,
    force_new: bool = False,
    direct_tools: bool = True,
) -> Tuple[bool, Dict]:
    task_id = str(task_id or "").strip()
    user_input = str(user_input or "").strip()
    agent_system = str(agent_system or "OpenCowork").strip() or "OpenCowork"
    agent_name = str(agent_name or "alpha_agent").strip() or "alpha_agent"
    config = config if isinstance(config, dict) else {}

    if not task_id:
        return False, {"error": "缺少 task_id"}
    if not user_input:
        return False, {"error": "缺少 user_input"}
    with _launch_lock:
        if task_id in _launching_tasks:
            return False, {"error": f"任务正在启动中: {task_id}"}
        if is_task_running(task_id):
            return False, {"error": f"任务已在运行: {task_id}"}
        _launching_tasks.add(task_id)

    try:
        if bool(getattr(sys, "frozen", False)):
            # PyInstaller 冻结模式：可执行文件本身就是 start.main 入口（盘上没有 start.py），
            # 直接重执行自身即可启动新任务进程。
            launcher = [sys.executable]
            launch_cwd = Path(sys.executable).resolve().parent
        else:
            start_py = Path(__file__).resolve().parent.parent / "start.py"
            if not start_py.exists():
                return False, {"error": f"start.py 不存在: {start_py}"}
            launcher = [sys.executable, str(start_py)]
            launch_cwd = start_py.parent

        task_path = str(Path(task_id).expanduser().resolve())
        Path(task_path).mkdir(parents=True, exist_ok=True)
        user_data_root = config.get("user_data_root")
        resolved_user_data_root = str(Path(user_data_root).expanduser().resolve()) if user_data_root else None

        effective_agent_library_dir = config.get("agent_library_dir") or config.get("library_dir")
        inferred_root = _as_root_dir(effective_agent_library_dir, "agent_library") if effective_agent_library_dir else None
        log_scope_root = resolved_user_data_root or inferred_root
        with runtime_env_scope({"MLA_USER_DATA_ROOT": log_scope_root} if log_scope_root else None):
            log_path = _runtime_logs_dir() / f"{Path(task_path).name or 'task'}_{abs(hash((task_path, user_input))) % 100000000}.log"

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        if resolved_user_data_root:
            env["MLA_USER_DATA_ROOT"] = resolved_user_data_root

        llm_config_path = config.get("llm_config_path")
        if llm_config_path:
            env["MLA_LLM_CONFIG_PATH"] = str(Path(llm_config_path).expanduser().resolve())

        if inferred_root:
            if not env.get("MLA_USER_DATA_ROOT"):
                env["MLA_USER_DATA_ROOT"] = inferred_root
            env["MLA_AGENT_LIBRARY_DIR"] = inferred_root

        if config.get("skills_dir"):
            env["MLA_SKILLS_LIBRARY_DIR"] = str(Path(config["skills_dir"]).expanduser().resolve())
        if config.get("tools_dir"):
            env["MLA_TOOLS_LIBRARY_DIR"] = str(Path(config["tools_dir"]).expanduser().resolve())
        action_window_steps = _resolve_action_window_config(config)
        if action_window_steps is not None:
            window_value = str(action_window_steps)
            env["MLA_ACTION_WINDOW_STEPS"] = window_value
            env["MLA_THINKING_INTERVAL"] = window_value
            env["MLA_THINKING_STEPS"] = window_value
        if config.get("max_turns") is not None:
            env["MLA_MAX_TURNS"] = str(max(1, int(config["max_turns"])))
        if config.get("fresh_enabled") is not None:
            env["MLA_FRESH_ENABLED"] = "true" if bool(config["fresh_enabled"]) else "false"
        if config.get("fresh_interval_sec") is not None:
            env["MLA_FRESH_INTERVAL_SEC"] = str(max(0, int(config["fresh_interval_sec"])))
        if config.get("auto_resume_attempts") is not None:
            env["MLA_AUTO_RESUME_ATTEMPTS"] = str(max(0, int(config["auto_resume_attempts"])))
        if config.get("auto_resume_delay_sec") is not None:
            env["MLA_AUTO_RESUME_DELAY_SEC"] = str(max(0.0, float(config["auto_resume_delay_sec"])))
        if config.get("mcp_servers") is not None:
            env["MLA_MCP_CONFIG_JSON"] = json.dumps({"servers": config["mcp_servers"]}, ensure_ascii=False)
        if config.get("tool_runtime_defaults") is not None:
            env["MLA_TOOL_RUNTIME_DEFAULTS_JSON"] = json.dumps(config["tool_runtime_defaults"], ensure_ascii=False)
        if config.get("tool_runtime_defaults_log_mode") is not None:
            env["MLA_TOOL_RUNTIME_DEFAULTS_LOG_MODE"] = str(config["tool_runtime_defaults_log_mode"]).strip().lower()
        if config.get("tool_hooks") is not None:
            env["MLA_TOOL_HOOKS_JSON"] = json.dumps(config["tool_hooks"], ensure_ascii=False)
        if config.get("context_hooks") is not None:
            env["MLA_CONTEXT_HOOKS_JSON"] = json.dumps(config["context_hooks"], ensure_ascii=False)
        if config.get("visible_skills") is not None:
            env["MLA_VISIBLE_SKILLS_JSON"] = json.dumps(config["visible_skills"], ensure_ascii=False)
        if config.get("seed_builtin_resources") is not None:
            env["MLA_SEED_BUILTIN_RESOURCES"] = "true" if bool(config["seed_builtin_resources"]) else "false"

        args = [
            *launcher,
            "--task_id", task_path,
            "--agent_system", agent_system,
            "--agent_name", agent_name,
            "--user_input", user_input,
        ]
        if force_new or bool(config.get("force_new")):
            args.append("--force-new")
        if direct_tools or bool(config.get("direct_tools")):
            args.append("--direct-tools")
        if config.get("auto_mode") is not None:
            args.extend(["--auto-mode", "true" if bool(config.get("auto_mode")) else "false"])
        if config.get("auto_resume_attempts") is not None:
            args.extend(["--auto-resume-attempts", str(max(0, int(config["auto_resume_attempts"])))])
        if config.get("auto_resume_delay_sec") is not None:
            args.extend(["--auto-resume-delay-sec", str(max(0.0, float(config["auto_resume_delay_sec"])))])

        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(
                f"[launch] task_id={task_path} agent_system={agent_system} agent_name={agent_name}\n"
            )
            process = subprocess.Popen(
                args,
                cwd=str(launch_cwd),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )

        return True, {
            "task_id": task_path,
            "agent_system": agent_system,
            "agent_name": agent_name,
            "pid": process.pid,
            "log_path": str(log_path),
            "message": f"已在后台启动任务: {task_path}",
        }
    finally:
        def _release_launch_flag():
            with _launch_lock:
                _launching_tasks.discard(task_id)

        timer = threading.Timer(5.0, _release_launch_flag)
        timer.daemon = True
        timer.start()
