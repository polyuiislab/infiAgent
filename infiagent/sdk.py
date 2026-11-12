#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
InfiAgent Python SDK。

设计原则：
- 实例化阶段只负责配置运行时环境
- 具体任务在 run(..., task_id=...) 阶段绑定
- user_data_root 一旦指定，conversation/share/stack/runtime 都跟随该根目录
- skills 仍保持独立目录语义，不强制跟随 user_data_root
"""

from __future__ import annotations

import asyncio
import json
import hashlib
import shutil
import time
from copy import deepcopy
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
import yaml

from core.agent_executor import AgentExecutor
from core.event_handlers import StructuredEventHandler
from core.hierarchy_manager import get_hierarchy_manager
from core.runtime_exceptions import InfiAgentRunError
from core.state_cleaner import clean_before_start
from utils.config_loader import ConfigLoader
from utils.llm_config_builder import build_llm_config_from_profiles
from utils.reasoning_modes import normalize_reasoning_mode, reasoning_mode_to_thinking_enabled
from utils.runtime_control import is_task_running, request_fresh
from utils.task_runtime import (
    append_task_message,
    get_task_share_paths,
    launch_task_process,
    load_task_resume_meta,
    list_known_tasks,
    reset_task_state,
    resume_task_with_fresh,
)
from utils.user_paths import (
    get_user_agent_library_root,
    get_user_config_dir,
    get_user_conversations_dir,
    get_user_data_root,
    get_user_llm_config_path,
    get_user_logs_dir,
    get_user_runtime_dir,
    get_task_file_prefix,
    get_user_data_root,
    get_user_skills_library_root,
    get_user_tools_library_root,
    runtime_env_scope,
)


def _normalize_path(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    return str(Path(path).expanduser().resolve())


def _as_root_dir(path: Optional[str], expected_leaf: Optional[str] = None) -> Optional[str]:
    normalized = _normalize_path(path)
    if not normalized:
        return None
    p = Path(normalized)
    if expected_leaf and p.name == expected_leaf:
        return str(p.parent)
    return normalized


def _positive_int_or_none(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return max(1, int(value))
    except Exception:
        return None


def _resolve_action_window_override(*values: Optional[int]) -> Optional[int]:
    parsed = [_positive_int_or_none(value) for value in values]
    parsed = [value for value in parsed if value is not None]
    if not parsed:
        return None
    return max(parsed)


def _materialize_inline_llm_config(
    *,
    user_data_root: Optional[str],
    llm_config: Dict[str, Any],
) -> str:
    root = Path(user_data_root).expanduser().resolve() if user_data_root else get_user_data_root()
    cfg_root = root / "runtime" / "sdk_llm_configs"
    cfg_root.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_dump(llm_config or {}, allow_unicode=True, sort_keys=False)
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]
    path = cfg_root / f"llm_config_{digest}.yaml"
    path.write_text(raw, encoding="utf-8")
    return str(path)


def _is_system_add_filename(name: str) -> bool:
    name = str(name or "").strip()
    if name == "system-add.md":
        return True
    return name.startswith("system-add.") and name.endswith(".md")


def _clear_materialized_system_add_files(task_dir: Path) -> None:
    if not task_dir.exists():
        return
    for item in task_dir.iterdir():
        if item.is_file() and _is_system_add_filename(item.name):
            item.unlink()


def _materialize_system_add(
    *,
    task_dir: Path,
    system_add_path: Optional[str] = None,
    system_add_content: Optional[str] = None,
) -> None:
    if system_add_path and system_add_content is not None:
        raise ValueError("system_add_path 与 system_add_content 只能二选一")

    task_dir.mkdir(parents=True, exist_ok=True)

    if system_add_path is None and system_add_content is None:
        return

    _clear_materialized_system_add_files(task_dir)

    if system_add_content is not None:
        (task_dir / "system-add.md").write_text(str(system_add_content), encoding="utf-8")
        return

    src = Path(str(system_add_path)).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"system_add_path 不存在: {src}")

    if src.is_dir():
        candidates = [p for p in sorted(src.iterdir()) if p.is_file() and _is_system_add_filename(p.name)]
        if not candidates:
            raise FileNotFoundError(f"system_add_path 目录中未找到 system-add.md 或 system-add.<agent_name>.md: {src}")
        default_files = [p for p in candidates if p.name == "system-add.md"]
        if len(default_files) > 1:
            raise ValueError(f"system_add_path 目录中包含多个 system-add.md: {src}")
        for file_path in candidates:
            shutil.copy2(file_path, task_dir / file_path.name)
        return

    target_name = src.name if _is_system_add_filename(src.name) else "system-add.md"
    shutil.copy2(src, task_dir / target_name)


class InfiAgent:
    def __init__(
        self,
        workspace: Optional[str] = None,
        user_data_root: Optional[str] = None,
        llm_config_path: Optional[str] = None,
        llm_config: Optional[Dict[str, Any]] = None,
        model_profiles: Optional[Dict[str, Any]] = None,
        library_dir: Optional[str] = None,
        agent_library_dir: Optional[str] = None,
        skills_dir: Optional[str] = None,
        tools_dir: Optional[str] = None,
        default_agent_system: Optional[str] = None,
        default_agent_name: Optional[str] = None,
        action_window_steps: Optional[int] = None,
        thinking_interval: Optional[int] = None,
        thinking_enabled: Optional[bool] = None,
        reasoning_mode: Optional[str] = None,
        thinking_steps: Optional[int] = None,
        no_tool_retry_limit: Optional[int] = None,
        user_history_compress_threshold_tokens: Optional[int] = None,
        user_history_recent_items: Optional[int] = None,
        max_turns: Optional[int] = None,
        auto_resume_attempts: Optional[int] = None,
        auto_resume_delay_sec: float = 1.0,
        fresh_enabled: Optional[bool] = None,
        fresh_interval_sec: Optional[int] = None,
        mcp_servers: Optional[List[Dict[str, Any]]] = None,
        tool_runtime_defaults: Optional[Dict[str, Any]] = None,
        tool_runtime_defaults_log_mode: str = "fingerprint",
        tool_hooks: Optional[List[Dict[str, Any]]] = None,
        context_hooks: Optional[List[Dict[str, Any]]] = None,
        visible_skills: Optional[List[str]] = None,
        seed_builtin_resources: bool = True,
        direct_tools: bool = True,
    ):
        if llm_config_path and (llm_config is not None or model_profiles is not None):
            raise ValueError("llm_config_path 与 llm_config/model_profiles 只能二选一")

        self.default_agent_system = str(default_agent_system or "OpenCowork").strip() or "OpenCowork"
        self.default_agent_name = str(default_agent_name or "alpha_agent").strip() or "alpha_agent"
        self.direct_tools = bool(direct_tools)

        # `workspace` 仅保留为兼容旧构造参数；新语义要求在 run(...) 时显式提供 task_id。
        self.legacy_workspace = _normalize_path(workspace)

        explicit_agent_library_root = _as_root_dir(agent_library_dir or library_dir, "agent_library")
        resolved_user_data_root = _normalize_path(user_data_root) or explicit_agent_library_root
        inline_llm_config = None
        if model_profiles is not None:
            inline_llm_config = build_llm_config_from_profiles(model_profiles)
        elif llm_config is not None:
            inline_llm_config = dict(llm_config)
        resolved_llm_config_path = _normalize_path(llm_config_path)
        if inline_llm_config is not None:
            resolved_llm_config_path = _materialize_inline_llm_config(
                user_data_root=resolved_user_data_root,
                llm_config=inline_llm_config,
            )

        self.runtime_env_overrides: Dict[str, Any] = {}
        if resolved_user_data_root:
            self.runtime_env_overrides["MLA_USER_DATA_ROOT"] = resolved_user_data_root
        if resolved_llm_config_path:
            self.runtime_env_overrides["MLA_LLM_CONFIG_PATH"] = resolved_llm_config_path
        if explicit_agent_library_root:
            self.runtime_env_overrides["MLA_AGENT_LIBRARY_DIR"] = explicit_agent_library_root
        if skills_dir:
            self.runtime_env_overrides["MLA_SKILLS_LIBRARY_DIR"] = _normalize_path(skills_dir)
        if tools_dir:
            self.runtime_env_overrides["MLA_TOOLS_LIBRARY_DIR"] = _normalize_path(tools_dir)
        resolved_action_window_steps = _resolve_action_window_override(
            action_window_steps,
            thinking_interval,
            thinking_steps,
        )
        if resolved_action_window_steps is not None:
            window_value = str(resolved_action_window_steps)
            self.runtime_env_overrides["MLA_ACTION_WINDOW_STEPS"] = window_value
            # 兼容旧启动器/旧配置读取方；三者始终写成同一个值，不再分叉。
            self.runtime_env_overrides["MLA_THINKING_INTERVAL"] = window_value
            self.runtime_env_overrides["MLA_THINKING_STEPS"] = window_value
        resolved_reasoning_mode = normalize_reasoning_mode(
            reasoning_mode,
            thinking_enabled=thinking_enabled,
        ) if reasoning_mode is not None else None
        if resolved_reasoning_mode is not None:
            self.runtime_env_overrides["MLA_REASONING_MODE"] = resolved_reasoning_mode
            self.runtime_env_overrides["MLA_THINKING_ENABLED"] = (
                "true" if reasoning_mode_to_thinking_enabled(resolved_reasoning_mode) else "false"
            )
        elif thinking_enabled is not None:
            self.runtime_env_overrides["MLA_THINKING_ENABLED"] = "true" if thinking_enabled else "false"
        if no_tool_retry_limit is not None:
            self.runtime_env_overrides["MLA_NO_TOOL_RETRY_LIMIT"] = str(max(1, int(no_tool_retry_limit)))
        if user_history_compress_threshold_tokens is not None:
            self.runtime_env_overrides["MLA_USER_HISTORY_COMPRESS_THRESHOLD_TOKENS"] = str(max(0, int(user_history_compress_threshold_tokens)))
        if user_history_recent_items is not None:
            self.runtime_env_overrides["MLA_USER_HISTORY_RECENT_ITEMS"] = str(max(0, int(user_history_recent_items)))
        if max_turns is not None:
            self.runtime_env_overrides["MLA_MAX_TURNS"] = str(max(1, int(max_turns)))
        if auto_resume_attempts is not None:
            self.runtime_env_overrides["MLA_AUTO_RESUME_ATTEMPTS"] = str(max(0, int(auto_resume_attempts)))
        if fresh_enabled is not None:
            self.runtime_env_overrides["MLA_FRESH_ENABLED"] = "true" if fresh_enabled else "false"
        if fresh_interval_sec is not None:
            self.runtime_env_overrides["MLA_FRESH_INTERVAL_SEC"] = str(max(0, int(fresh_interval_sec)))
        if mcp_servers is not None:
            self.runtime_env_overrides["MLA_MCP_CONFIG_JSON"] = json.dumps({"servers": mcp_servers}, ensure_ascii=False)
        if tool_runtime_defaults is not None:
            self.runtime_env_overrides["MLA_TOOL_RUNTIME_DEFAULTS_JSON"] = json.dumps(tool_runtime_defaults, ensure_ascii=False)
        self.runtime_env_overrides["MLA_TOOL_RUNTIME_DEFAULTS_LOG_MODE"] = str(tool_runtime_defaults_log_mode or "fingerprint").strip().lower() or "fingerprint"
        if tool_hooks is not None:
            self.runtime_env_overrides["MLA_TOOL_HOOKS_JSON"] = json.dumps(tool_hooks, ensure_ascii=False)
        if context_hooks is not None:
            self.runtime_env_overrides["MLA_CONTEXT_HOOKS_JSON"] = json.dumps(context_hooks, ensure_ascii=False)
        if visible_skills is not None:
            self.runtime_env_overrides["MLA_VISIBLE_SKILLS_JSON"] = json.dumps(visible_skills, ensure_ascii=False)
        self.runtime_env_overrides["MLA_SEED_BUILTIN_RESOURCES"] = "true" if seed_builtin_resources else "false"

        self.user_data_root = resolved_user_data_root
        self.llm_config_path = resolved_llm_config_path
        self.inline_llm_config = inline_llm_config
        self.model_profiles = deepcopy(model_profiles) if model_profiles is not None else None
        self.agent_library_root = explicit_agent_library_root
        self.skills_dir = _normalize_path(skills_dir)
        self.tools_dir = _normalize_path(tools_dir)
        self.action_window_steps = resolved_action_window_steps
        self.thinking_interval = resolved_action_window_steps
        self.reasoning_mode = resolved_reasoning_mode
        self.thinking_enabled = bool(thinking_enabled) if thinking_enabled is not None else None
        self.thinking_steps = resolved_action_window_steps
        self.no_tool_retry_limit = max(1, int(no_tool_retry_limit)) if no_tool_retry_limit is not None else None
        self.user_history_compress_threshold_tokens = max(0, int(user_history_compress_threshold_tokens)) if user_history_compress_threshold_tokens is not None else None
        self.user_history_recent_items = max(0, int(user_history_recent_items)) if user_history_recent_items is not None else None
        self.max_turns = max(1, int(max_turns)) if max_turns is not None else None
        self.auto_resume_attempts = max(0, int(auto_resume_attempts)) if auto_resume_attempts is not None else None
        self.auto_resume_delay_sec = max(0.0, float(auto_resume_delay_sec or 0.0))
        self.fresh_enabled = fresh_enabled if fresh_enabled is not None else None
        self.fresh_interval_sec = max(0, int(fresh_interval_sec)) if fresh_interval_sec is not None else None
        self.mcp_servers = mcp_servers
        self.tool_runtime_defaults = tool_runtime_defaults
        self.tool_runtime_defaults_log_mode = str(tool_runtime_defaults_log_mode or "fingerprint").strip().lower() or "fingerprint"
        self.tool_hooks = tool_hooks
        self.context_hooks = context_hooks
        self.visible_skills = [str(item).strip() for item in visible_skills if str(item).strip()] if visible_skills is not None else None
        self.seed_builtin_resources = bool(seed_builtin_resources)

    def _runtime_scope(self, extra_overrides: Optional[Dict[str, Any]] = None):
        merged = dict(self.runtime_env_overrides)
        if extra_overrides:
            merged.update(extra_overrides)
        return runtime_env_scope(merged)

    def _resolve_task_id(
        self,
        *,
        task_id: Optional[str] = None,
        workspace: Optional[str] = None,
        required: bool = True,
    ) -> Optional[str]:
        raw_task_id = task_id or workspace
        if not raw_task_id and not required:
            return None
        if not raw_task_id:
            raise ValueError("必须显式提供 task_id。SDK 实例化阶段不再绑定 workspace。")
        return str(Path(raw_task_id).expanduser().resolve())

    def _build_launch_config(self) -> Dict[str, Any]:
        config: Dict[str, Any] = {}
        if self.user_data_root:
            config["user_data_root"] = self.user_data_root
        if self.llm_config_path:
            config["llm_config_path"] = self.llm_config_path
        if self.agent_library_root:
            config["agent_library_dir"] = self.agent_library_root
        if self.skills_dir:
            config["skills_dir"] = self.skills_dir
        if self.tools_dir:
            config["tools_dir"] = self.tools_dir
        if self.action_window_steps is not None:
            config["action_window_steps"] = self.action_window_steps
        if self.thinking_interval is not None:
            config["thinking_interval"] = self.thinking_interval
        if self.reasoning_mode is not None:
            config["reasoning_mode"] = self.reasoning_mode
        if self.thinking_enabled is not None:
            config["thinking_enabled"] = self.thinking_enabled
        if self.thinking_steps is not None:
            config["thinking_steps"] = self.thinking_steps
        if self.no_tool_retry_limit is not None:
            config["no_tool_retry_limit"] = self.no_tool_retry_limit
        if self.user_history_compress_threshold_tokens is not None:
            config["user_history_compress_threshold_tokens"] = self.user_history_compress_threshold_tokens
        if self.user_history_recent_items is not None:
            config["user_history_recent_items"] = self.user_history_recent_items
        if self.max_turns is not None:
            config["max_turns"] = self.max_turns
        if self.auto_resume_attempts is not None:
            config["auto_resume_attempts"] = self.auto_resume_attempts
            config["auto_resume_delay_sec"] = self.auto_resume_delay_sec
        if self.fresh_enabled is not None:
            config["fresh_enabled"] = self.fresh_enabled
        if self.fresh_interval_sec is not None:
            config["fresh_interval_sec"] = self.fresh_interval_sec
        if self.mcp_servers is not None:
            config["mcp_servers"] = self.mcp_servers
        if self.tool_runtime_defaults is not None:
            config["tool_runtime_defaults"] = self.tool_runtime_defaults
        if self.tool_runtime_defaults_log_mode:
            config["tool_runtime_defaults_log_mode"] = self.tool_runtime_defaults_log_mode
        if self.tool_hooks is not None:
            config["tool_hooks"] = self.tool_hooks
        if self.context_hooks is not None:
            config["context_hooks"] = self.context_hooks
        if self.visible_skills is not None:
            config["visible_skills"] = self.visible_skills
        config["seed_builtin_resources"] = self.seed_builtin_resources
        return config

    def _build_sdk_event_handler(
        self,
        *,
        collect_events: bool = False,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Tuple[List[StructuredEventHandler], Optional[List[Dict[str, Any]]]]:
        collected_events: Optional[List[Dict[str, Any]]] = [] if collect_events else None
        if not collect_events and on_event is None:
            return [], collected_events
        return [
            StructuredEventHandler(
                callback=on_event,
                collector=collected_events,
            )
        ], collected_events

    def _attach_optional_run_artifacts(
        self,
        payload: Dict[str, Any],
        *,
        task_id: str,
        collected_events: Optional[List[Dict[str, Any]]] = None,
        collect_events: bool = False,
        include_trace: bool = False,
    ) -> Dict[str, Any]:
        enriched = dict(payload)
        if collect_events:
            enriched["events"] = list(collected_events or [])
        if include_trace:
            enriched["trace"] = self.task_trace(task_id=task_id)
        return enriched

    def create_workspace(self, workspace_id: str, title: str = "", concurrency: str = "serial") -> Dict[str, Any]:
        from utils.workspaces import create
        with self._runtime_scope():
            return create(workspace_id, title, concurrency)

    def list_workspaces(self) -> List[Dict[str, Any]]:
        from utils.workspaces import list_workspaces
        with self._runtime_scope():
            return list_workspaces()

    def run(
        self,
        user_input: str,
        *,
        task_id: str,
        agent_system: Optional[str] = None,
        agent_name: Optional[str] = None,
        force_new: bool = False,
        workspace: Optional[str] = None,
        workspace_id: Optional[str] = None,
        collect_events: bool = False,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        include_trace: bool = False,
        raise_on_error: bool = False,
        stream_llm_tokens: bool = False,
        thinking_enabled: Optional[bool] = None,
        reasoning_mode: Optional[str] = None,
        action_window_steps: Optional[int] = None,
        thinking_steps: Optional[int] = None,
        no_tool_retry_limit: Optional[int] = None,
        visible_skills: Optional[List[str]] = None,
        user_history_compress_threshold_tokens: Optional[int] = None,
        user_history_recent_items: Optional[int] = None,
        max_turns: Optional[int] = None,
        auto_resume_attempts: Optional[int] = None,
        auto_resume_delay_sec: Optional[float] = None,
        auto_mode: Optional[bool] = None,
        tool_runtime_defaults: Optional[Dict[str, Any]] = None,
        tool_runtime_defaults_log_mode: Optional[str] = None,
        system_add_path: Optional[str] = None,
        system_add_content: Optional[str] = None,
    ) -> Dict[str, Any]:
        target_task_id = self._resolve_task_id(task_id=task_id, workspace=workspace)
        Path(target_task_id).mkdir(parents=True, exist_ok=True)
        target_agent_system = agent_system or self.default_agent_system
        target_agent_name = agent_name or self.default_agent_name

        extra_runtime_overrides: Dict[str, Any] = {}
        if reasoning_mode is not None:
            resolved_reasoning_mode = normalize_reasoning_mode(
                reasoning_mode,
                thinking_enabled=thinking_enabled,
            )
            extra_runtime_overrides["MLA_REASONING_MODE"] = resolved_reasoning_mode
            extra_runtime_overrides["MLA_THINKING_ENABLED"] = (
                "true" if reasoning_mode_to_thinking_enabled(resolved_reasoning_mode) else "false"
            )
        elif thinking_enabled is not None:
            extra_runtime_overrides["MLA_THINKING_ENABLED"] = "true" if thinking_enabled else "false"
        resolved_action_window_steps = _resolve_action_window_override(action_window_steps, thinking_steps)
        if resolved_action_window_steps is not None:
            window_value = str(resolved_action_window_steps)
            extra_runtime_overrides["MLA_ACTION_WINDOW_STEPS"] = window_value
            extra_runtime_overrides["MLA_THINKING_INTERVAL"] = window_value
            extra_runtime_overrides["MLA_THINKING_STEPS"] = window_value
        if no_tool_retry_limit is not None:
            extra_runtime_overrides["MLA_NO_TOOL_RETRY_LIMIT"] = str(max(1, int(no_tool_retry_limit)))
        if visible_skills is not None:
            extra_runtime_overrides["MLA_VISIBLE_SKILLS_JSON"] = json.dumps(
                [str(item).strip() for item in visible_skills if str(item).strip()],
                ensure_ascii=False,
            )
        if user_history_compress_threshold_tokens is not None:
            extra_runtime_overrides["MLA_USER_HISTORY_COMPRESS_THRESHOLD_TOKENS"] = str(
                max(0, int(user_history_compress_threshold_tokens))
            )
        if user_history_recent_items is not None:
            extra_runtime_overrides["MLA_USER_HISTORY_RECENT_ITEMS"] = str(
                max(0, int(user_history_recent_items))
            )
        if max_turns is not None:
            extra_runtime_overrides["MLA_MAX_TURNS"] = str(max(1, int(max_turns)))
        resolved_auto_resume_attempts = (
            max(0, int(auto_resume_attempts))
            if auto_resume_attempts is not None
            else (self.auto_resume_attempts if self.auto_resume_attempts is not None else 0)
        )
        resolved_auto_resume_delay = (
            max(0.0, float(auto_resume_delay_sec))
            if auto_resume_delay_sec is not None
            else self.auto_resume_delay_sec
        )
        if resolved_auto_resume_attempts:
            extra_runtime_overrides["MLA_AUTO_RESUME_ATTEMPTS"] = str(resolved_auto_resume_attempts)
        if tool_runtime_defaults is not None:
            extra_runtime_overrides["MLA_TOOL_RUNTIME_DEFAULTS_JSON"] = json.dumps(tool_runtime_defaults, ensure_ascii=False)
        if tool_runtime_defaults_log_mode is not None:
            extra_runtime_overrides["MLA_TOOL_RUNTIME_DEFAULTS_LOG_MODE"] = str(tool_runtime_defaults_log_mode or "fingerprint").strip().lower() or "fingerprint"
        _materialize_system_add(
            task_dir=Path(target_task_id),
            system_add_path=system_add_path,
            system_add_content=system_add_content,
        )

        from utils.task_owner import task_owner
        from utils.configuration_lock import configuration_access
        with self._runtime_scope(extra_runtime_overrides or None), configuration_access(get_user_data_root()), task_owner(target_task_id) as owned:
            if workspace_id is not None:
                from utils.workspaces import attach
                attach(target_task_id, workspace_id)
            extra_event_handlers, collected_events = self._build_sdk_event_handler(
                collect_events=collect_events,
                on_event=on_event,
            )
            if not owned or is_task_running(target_task_id):
                busy_result = {
                    "status": "busy",
                    "task_id": target_task_id,
                    "output": "",
                    "error": f"任务已在运行: {target_task_id}",
                }
                return self._attach_optional_run_artifacts(
                    busy_result,
                    task_id=target_task_id,
                    collected_events=collected_events,
                    collect_events=collect_events,
                    include_trace=include_trace,
                )
            config_loader = ConfigLoader(target_agent_system)
            hierarchy_manager = get_hierarchy_manager(target_task_id)

            if force_new:
                hierarchy_manager.clear_current_state()
                from utils.atomic_state import write_json
                import uuid
                write_json(Path(target_task_id) / '.sdk_runtime' / 'generation.json', {'id': uuid.uuid4().hex})
            else:
                clean_before_start(target_task_id, user_input)

            hierarchy_manager.start_new_instruction(user_input)

            agent_config = config_loader.get_tool_config(target_agent_name)
            if agent_config.get("type") != "llm_call_agent":
                raise ValueError(f"{target_agent_name} 不是一个 LLM Agent")
            current_agent_name = target_agent_name
            current_user_input = user_input
            auto_resume_used = 0
            result: Dict[str, Any] = {}
            while True:
                agent = AgentExecutor(
                    agent_name=current_agent_name,
                    agent_config=agent_config,
                    config_loader=config_loader,
                    hierarchy_manager=hierarchy_manager,
                    direct_tools=self.direct_tools,
                    extra_event_handlers=extra_event_handlers,
                    exit_on_error=False,
                    raise_on_error=False,
                    stream_llm_tokens=stream_llm_tokens,
                )
                if auto_mode is not None:
                    agent.tool_executor.set_task_permission(target_task_id, bool(auto_mode))
                try:
                    result = agent.run(target_task_id, current_user_input)
                except InfiAgentRunError as exc:
                    exc.events = list(collected_events or exc.events or [])
                    if include_trace:
                        exc.trace = self.task_trace(task_id=target_task_id)
                    raise

                if result.get("status") == "success":
                    if resolved_auto_resume_attempts:
                        hierarchy_manager.set_runtime_metadata(auto_resume={
                            "attempts": 0,
                            "max_attempts": resolved_auto_resume_attempts,
                            "cleared_at": datetime.now().isoformat(),
                            "reason": "task_completed",
                        })
                    break

                if result.get('status') in {'stopped', 'cancelled'}:
                    break

                if auto_resume_used >= resolved_auto_resume_attempts:
                    break

                meta, resume_error = load_task_resume_meta(
                    target_task_id,
                    fallback_agent_system=target_agent_system,
                )
                if not meta:
                    result.setdefault("auto_resume", {
                        "attempts": auto_resume_used,
                        "max_attempts": resolved_auto_resume_attempts,
                        "stopped_reason": resume_error,
                    })
                    break

                auto_resume_used += 1
                hierarchy_manager.set_runtime_metadata(auto_resume={
                    "attempts": auto_resume_used,
                    "max_attempts": resolved_auto_resume_attempts,
                    "last_error": result.get("error_message") or result.get("error") or result.get("error_information") or "",
                    "last_attempt_at": datetime.now().isoformat(),
                })
                current_agent_name = meta["agent_name"]
                current_user_input = meta["user_input"]
                agent_config = config_loader.get_tool_config(current_agent_name)
                if resolved_auto_resume_delay:
                    time.sleep(resolved_auto_resume_delay)

            enriched_result = self._attach_optional_run_artifacts(
                result,
                task_id=target_task_id,
                collected_events=collected_events,
                collect_events=collect_events,
                include_trace=include_trace,
            )
            if raise_on_error and enriched_result.get("status") == "error":
                raise InfiAgentRunError.from_result(
                    enriched_result,
                    task_id=target_task_id,
                    agent_name=target_agent_name,
                    stage="run",
                    events=enriched_result.get("events"),
                    trace=enriched_result.get("trace"),
                )
            return enriched_result

    def fresh(
        self,
        *,
        task_id: str,
        reason: str = "",
    ) -> Dict[str, Any]:
        target_task_id = self._resolve_task_id(task_id=task_id)
        with self._runtime_scope():
            if is_task_running(target_task_id):
                request_fresh(reason=reason, task_id=target_task_id)
                output = f"已向运行中的任务发送 fresh 请求: {target_task_id}"
            else:
                ok, msg = resume_task_with_fresh(
                    task_id=target_task_id,
                    reason=reason,
                    fallback_agent_system=self.default_agent_system,
                    direct_tools=self.direct_tools,
                    env_overrides=self.runtime_env_overrides,
                )
                if not ok:
                    return {
                        "status": "error",
                        "task_id": target_task_id,
                        "output": "",
                        "error": msg,
                    }
                output = msg
            return {
                "status": "success",
                "task_id": target_task_id,
                "output": output,
            }

    def add_message(
        self,
        message: str,
        *,
        task_id: str,
        source: str = "agent",
        resume_if_needed: bool = False,
        agent_system: Optional[str] = None,
    ) -> Dict[str, Any]:
        target_task_id = self._resolve_task_id(task_id=task_id)
        with self._runtime_scope():
            ok, payload = append_task_message(
                task_id=target_task_id,
                message=message,
                source=source,
                resume_if_needed=resume_if_needed,
                fallback_agent_system=agent_system or self.default_agent_system,
                direct_tools=self.direct_tools,
                env_overrides=self.runtime_env_overrides,
            )
            if not ok:
                return {
                    "status": "error",
                    "task_id": target_task_id,
                    "output": "",
                    "error": payload.get("error") or "add_message failed",
                }
            return {
                "status": "success",
                **payload,
            }

    def start_background_task(
        self,
        *,
        task_id: str,
        user_input: str,
        agent_system: Optional[str] = None,
        agent_name: Optional[str] = None,
        force_new: bool = False,
        direct_tools: Optional[bool] = None,
        max_turns: Optional[int] = None,
        auto_resume_attempts: Optional[int] = None,
        auto_resume_delay_sec: Optional[float] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        merged_config = self._build_launch_config()
        if max_turns is not None:
            merged_config["max_turns"] = max(1, int(max_turns))
        if auto_resume_attempts is not None:
            merged_config["auto_resume_attempts"] = max(0, int(auto_resume_attempts))
        if auto_resume_delay_sec is not None:
            merged_config["auto_resume_delay_sec"] = max(0.0, float(auto_resume_delay_sec))
        if config:
            merged_config.update(config)
        with self._runtime_scope():
            ok, payload = launch_task_process(
                task_id=self._resolve_task_id(task_id=task_id),
                user_input=user_input,
                agent_system=agent_system or self.default_agent_system,
                agent_name=agent_name or self.default_agent_name,
                force_new=force_new,
                direct_tools=self.direct_tools if direct_tools is None else bool(direct_tools),
                config=merged_config,
            )
        if not ok:
            return {"status": "error", **payload}
        return {"status": "success", **payload}

    def task_share_context_path(self, *, task_id: str) -> Dict[str, Any]:
        with self._runtime_scope():
            return {
                "status": "success",
                **get_task_share_paths(self._resolve_task_id(task_id=task_id)),
            }

    def list_task_ids(self, *, only_running: bool = False) -> Dict[str, Any]:
        with self._runtime_scope():
            return {
                "status": "success",
                **list_known_tasks(only_running=only_running),
            }

    def describe_runtime(self) -> Dict[str, Any]:
        with self._runtime_scope():
            return {
                "status": "success",
                "user_data_root": str(get_user_data_root()),
                "config_dir": str(get_user_config_dir()),
                "llm_config_path": str(get_user_llm_config_path()),
                "agent_library_dir": str(get_user_agent_library_root()),
                "skills_dir": str(get_user_skills_library_root()),
                "tools_dir": str(get_user_tools_library_root()),
                "conversations_dir": str(get_user_conversations_dir()),
                "logs_dir": str(get_user_logs_dir()),
                "runtime_dir": str(get_user_runtime_dir()),
                "app_config_path": str(get_user_config_dir() / "app_config.json"),
                "default_agent_system": self.default_agent_system,
                "default_agent_name": self.default_agent_name,
                "direct_tools": self.direct_tools,
                "seed_builtin_resources": self.seed_builtin_resources,
                "max_turns": self.max_turns if self.max_turns is not None else 100000,
            }

    def list_agent_systems(self) -> Dict[str, Any]:
        with self._runtime_scope():
            root = get_user_agent_library_root()
            systems = []
            if root.exists():
                for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
                    if not child.is_dir() or child.name.startswith("."):
                        continue
                    agent_names: List[str] = []
                    agents_path = child / "level_3_agents.yaml"
                    if agents_path.exists():
                        try:
                            payload = yaml.safe_load(agents_path.read_text(encoding="utf-8")) or {}
                            tools = payload.get("tools", {}) if isinstance(payload, dict) else {}
                            if isinstance(tools, dict):
                                for name, config in tools.items():
                                    if not isinstance(config, dict):
                                        continue
                                    if config.get("type") == "llm_call_agent":
                                        agent_names.append(str(config.get("name") or name))
                        except Exception:
                            agent_names = []
                    systems.append({
                        "name": child.name,
                        "path": str(child),
                        "has_general_prompts": (child / "general_prompts.yaml").exists(),
                        "has_level_0_tools": (child / "level_0_tools.yaml").exists(),
                        "agent_names": sorted(set(agent_names)),
                    })
            return {
                "status": "success",
                "agent_systems": systems,
            }

    def task_snapshot(self, *, task_id: str) -> Dict[str, Any]:
        target_task_id = self._resolve_task_id(task_id=task_id)
        with self._runtime_scope():
            paths = get_task_share_paths(target_task_id)
            share_context_path = Path(paths["share_context_path"])
            data: Dict[str, Any] = {}
            if share_context_path.exists():
                try:
                    data = json.loads(share_context_path.read_text(encoding="utf-8"))
                except Exception:
                    data = {}

            current = data.get("current", {}) if isinstance(data, dict) else {}
            runtime_meta = data.get("runtime", {}) if isinstance(data, dict) else {}
            history = data.get("history", []) if isinstance(data, dict) else []
            instructions = current.get("instructions", []) if isinstance(current, dict) else []
            agents_status = current.get("agents_status", {}) if isinstance(current, dict) else {}

            latest_thinking = ""
            latest_thinking_at = ""
            last_final_output = ""
            last_final_output_at = ""

            agent_infos: List[Dict[str, Any]] = []
            if isinstance(agents_status, dict):
                agent_infos.extend([item for item in agents_status.values() if isinstance(item, dict)])
            if isinstance(history, list):
                for entry in history:
                    if not isinstance(entry, dict):
                        continue
                    archived_agents = entry.get("agents_status", {})
                    if isinstance(archived_agents, dict):
                        agent_infos.extend([item for item in archived_agents.values() if isinstance(item, dict)])

            for agent_info in agent_infos:
                if not isinstance(agent_info, dict):
                    continue
                thinking_at = str(agent_info.get("thinking_updated_at") or "")
                if thinking_at >= latest_thinking_at and agent_info.get("latest_thinking"):
                    latest_thinking = str(agent_info.get("latest_thinking") or "")
                    latest_thinking_at = thinking_at
                final_at = str(agent_info.get("end_time") or "")
                if final_at >= last_final_output_at and agent_info.get("final_output"):
                    last_final_output = str(agent_info.get("final_output") or "")
                    last_final_output_at = final_at

            return {
                "status": "success",
                "task_id": target_task_id,
                "running": is_task_running(target_task_id),
                "share_context_path": paths["share_context_path"],
                "stack_path": paths["stack_path"],
                "instruction_count": len(instructions) if isinstance(instructions, list) else 0,
                "latest_instruction": instructions[-1] if isinstance(instructions, list) and instructions else None,
                "history_count": len(history) if isinstance(history, list) else 0,
                "last_updated": str(current.get("last_updated") or ""),
                "runtime": runtime_meta if isinstance(runtime_meta, dict) else {},
                "latest_thinking": latest_thinking,
                "latest_thinking_at": latest_thinking_at,
                "last_final_output": last_final_output,
                "last_final_output_at": last_final_output_at,
            }

    def task_trace(
        self,
        *,
        task_id: str,
        include_render_history: bool = False,
        include_system_prompt: bool = False,
    ) -> Dict[str, Any]:
        target_task_id = self._resolve_task_id(task_id=task_id)
        with self._runtime_scope():
            conversations_dir = get_user_conversations_dir()
            prefix = get_task_file_prefix(target_task_id)
            agent_traces: List[Dict[str, Any]] = []

            for path in sorted(conversations_dir.glob(f"{prefix}_*_actions.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception as e:
                    agent_traces.append({
                        "path": str(path),
                        "load_error": str(e),
                    })
                    continue

                trace_item = {
                    "path": str(path),
                    "agent_id": str(data.get("agent_id") or ""),
                    "agent_name": str(data.get("agent_name") or ""),
                    "task_input": str(data.get("task_input") or ""),
                    "current_turn": int(data.get("current_turn") or 0),
                    "tool_call_counter": int(data.get("tool_call_counter") or 0),
                    "llm_turn_counter": int(data.get("llm_turn_counter") or 0),
                    "latest_thinking": str(data.get("latest_thinking") or ""),
                    "pending_tools": data.get("pending_tools") or [],
                    "action_history_fact": data.get("action_history_fact") or data.get("action_history") or [],
                    "last_updated": str(data.get("last_updated") or ""),
                }
                if include_render_history:
                    trace_item["action_history"] = data.get("action_history") or []
                if include_system_prompt:
                    trace_item["system_prompt"] = str(data.get("system_prompt") or "")
                agent_traces.append(trace_item)

            debug_path = conversations_dir / f"{prefix}_llm_debug.jsonl"
            paths = get_task_share_paths(target_task_id)
            return {
                "status": "success",
                "task_id": target_task_id,
                "share_context_path": paths["share_context_path"],
                "stack_path": paths["stack_path"],
                "agent_trace_count": len(agent_traces),
                "agent_traces": agent_traces,
                "llm_debug_path": str(debug_path) if debug_path.exists() else "",
            }

    def reset_task(
        self,
        *,
        task_id: str,
        preserve_history: bool = True,
        kill_background_processes: bool = True,
        reason: str = "",
    ) -> Dict[str, Any]:
        target_task_id = self._resolve_task_id(task_id=task_id)
        with self._runtime_scope():
            ok, payload = reset_task_state(
                task_id=target_task_id,
                preserve_history=preserve_history,
                kill_background_processes=kill_background_processes,
                reason=reason,
            )
        if not ok:
            return {"status": "error", "task_id": target_task_id, **payload}
        return {"status": "success", **payload}

    def pause_task(
        self,
        *,
        task_id: str,
        reason: str = "",
        preserve_history: bool = True,
        kill_background_processes: bool = True,
    ) -> Dict[str, Any]:
        return self.reset_task(
            task_id=task_id,
            preserve_history=preserve_history,
            kill_background_processes=kill_background_processes,
            reason=reason or "manual pause",
        )

    async def run_async(
        self,
        user_input: str,
        *,
        task_id: str,
        agent_system: Optional[str] = None,
        agent_name: Optional[str] = None,
        force_new: bool = False,
        workspace: Optional[str] = None,
        workspace_id: Optional[str] = None,
        collect_events: bool = False,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        include_trace: bool = False,
        raise_on_error: bool = False,
        stream_llm_tokens: bool = False,
        thinking_enabled: Optional[bool] = None,
        reasoning_mode: Optional[str] = None,
        action_window_steps: Optional[int] = None,
        thinking_steps: Optional[int] = None,
        no_tool_retry_limit: Optional[int] = None,
        visible_skills: Optional[List[str]] = None,
        user_history_compress_threshold_tokens: Optional[int] = None,
        user_history_recent_items: Optional[int] = None,
        max_turns: Optional[int] = None,
    ) -> Dict[str, Any]:
        fn = partial(
            self.run,
            user_input,
            task_id=task_id,
            agent_system=agent_system,
            agent_name=agent_name,
            force_new=force_new,
            workspace=workspace,
            workspace_id=workspace_id,
            collect_events=collect_events,
            on_event=on_event,
            include_trace=include_trace,
            raise_on_error=raise_on_error,
            stream_llm_tokens=stream_llm_tokens,
            thinking_enabled=thinking_enabled,
            reasoning_mode=reasoning_mode,
            action_window_steps=action_window_steps,
            thinking_steps=thinking_steps,
            no_tool_retry_limit=no_tool_retry_limit,
            visible_skills=visible_skills,
            user_history_compress_threshold_tokens=user_history_compress_threshold_tokens,
            user_history_recent_items=user_history_recent_items,
            max_turns=max_turns,
        )
        return await asyncio.to_thread(fn)

    async def fresh_async(self, *, task_id: str, reason: str = "") -> Dict[str, Any]:
        return await asyncio.to_thread(self.fresh, task_id=task_id, reason=reason)

    async def add_message_async(
        self,
        message: str,
        *,
        task_id: str,
        source: str = "agent",
        resume_if_needed: bool = False,
        agent_system: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.add_message,
            message,
            task_id=task_id,
            source=source,
            resume_if_needed=resume_if_needed,
            agent_system=agent_system,
        )

    async def start_background_task_async(
        self,
        *,
        task_id: str,
        user_input: str,
        agent_system: Optional[str] = None,
        agent_name: Optional[str] = None,
        force_new: bool = False,
        direct_tools: Optional[bool] = None,
        max_turns: Optional[int] = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.start_background_task,
            task_id=task_id,
            user_input=user_input,
            agent_system=agent_system,
            agent_name=agent_name,
            force_new=force_new,
            direct_tools=direct_tools,
            max_turns=max_turns,
            config=config,
        )

    async def task_share_context_path_async(self, *, task_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.task_share_context_path, task_id=task_id)

    async def list_task_ids_async(self, *, only_running: bool = False) -> Dict[str, Any]:
        return await asyncio.to_thread(self.list_task_ids, only_running=only_running)

    async def describe_runtime_async(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self.describe_runtime)

    async def list_agent_systems_async(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self.list_agent_systems)

    async def task_snapshot_async(self, *, task_id: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.task_snapshot, task_id=task_id)

    async def task_trace_async(
        self,
        *,
        task_id: str,
        include_render_history: bool = False,
        include_system_prompt: bool = False,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.task_trace,
            task_id=task_id,
            include_render_history=include_render_history,
            include_system_prompt=include_system_prompt,
        )

    async def reset_task_async(
        self,
        *,
        task_id: str,
        preserve_history: bool = True,
        kill_background_processes: bool = True,
        reason: str = "",
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.reset_task,
            task_id=task_id,
            preserve_history=preserve_history,
            kill_background_processes=kill_background_processes,
            reason=reason,
        )

    async def pause_task_async(
        self,
        *,
        task_id: str,
        reason: str = "",
        preserve_history: bool = True,
        kill_background_processes: bool = True,
    ) -> Dict[str, Any]:
        return await asyncio.to_thread(
            self.pause_task,
            task_id=task_id,
            reason=reason,
            preserve_history=preserve_history,
            kill_background_processes=kill_background_processes,
        )


def infiagent(**kwargs) -> InfiAgent:
    return InfiAgent(**kwargs)
