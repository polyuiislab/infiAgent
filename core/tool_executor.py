#!/usr/bin/env python3
from utils.windows_compat import safe_print
# -*- coding: utf-8 -*-
"""
工具执行器 - 统一使用进程内 direct-tools 模式。
"""

import json
import hashlib
import time
import uuid
import asyncio
import threading
import os
import contextvars
from typing import Dict, Any, List

from tool_server_lite.registry import get_runtime_registry, get_runtime_registry_failures
from utils.mcp_manager import call_mcp_tool
from utils.tool_hooks import trigger_tool_hooks
from utils.atomic_state import StateCommitError, StateCorruptError
from utils.execution_scope import CURRENT, TOOL_CALL, check_cancelled
from utils.agent_activities import AgentActivities

class ToolExecutor:
    """工具执行器 - 统一使用进程内 direct-tools 模式"""
    
    # 危险工具列表（需要用户确认）
    DANGEROUS_TOOLS = [
        "file_write",      # 文件写入
        "execute_command", # 执行命令
    ]
    
    def __init__(
        self,
        config_loader,
        hierarchy_manager,
        direct_mode=False,
        extra_event_handlers=None,
        exit_on_error: bool = True,
        raise_on_error: bool = False,
        stream_llm_tokens: bool = False,
    ):
        """
        初始化工具执行器
        
        Args:
            config_loader: 配置加载器
            hierarchy_manager: 层级管理器
            direct_mode: 兼容旧参数，当前始终使用进程内 direct-tools 模式
        """
        self.config_loader = config_loader
        self.hierarchy_manager = hierarchy_manager
        self.direct_mode = True
        self.extra_event_handlers = list(extra_event_handlers or [])
        self.exit_on_error = bool(exit_on_error)
        self.raise_on_error = bool(raise_on_error)
        self.stream_llm_tokens = bool(stream_llm_tokens)
        self._tools_registry = None  # 懒加载
        safe_print("🔧 工具执行器: 进程内直接调用模式（无需 ToolServer）")
        
        # 权限管理：task_id → auto_mode 映射
        self.task_permissions = {}  # {task_id: {"auto_mode": True/False}}
        self.agent_id = ""
        self.agent_name = ""
        self.tool_runtime_defaults = self._load_tool_runtime_defaults()
        self.tool_runtime_defaults_log_mode = self._load_tool_runtime_defaults_log_mode()
        self.available_agent_tools = None
        self.activities = None
        self._activities_lock = threading.RLock()

    def set_agent_context(self, *, agent_id: str = "", agent_name: str = ""):
        self.agent_id = str(agent_id or "")
        self.agent_name = str(agent_name or "")

    def child_activities(self, task_id):
        with self._activities_lock:
            return self._child_activities_locked(task_id)

    def _child_activities_locked(self, task_id):
        if self.activities is None:
            def run_child(name, task_input, context_mode):
                if self.available_agent_tools is not None and name not in self.available_agent_tools:
                    return {'status': 'error', 'output': '', 'error_code': 'SCOPE_DENIED',
                            'error_information': 'Child agent is no longer available to the caller'}
                config = self.config_loader.get_tool_config(name)
                return self._execute_sub_agent(name, config, {'task_input': task_input,
                    '_context_mode': context_mode}, task_id)
            def messages():
                return len(self.hierarchy_manager.get_context().get('current', {}).get('instructions', []))
            def snapshot():
                from utils.conversation_storage import ConversationStorage
                return (ConversationStorage().load_actions(task_id, self.agent_id) or {}).get('action_history', [])
            self.activities = AgentActivities(task_id, self.agent_id, run_child, message_count=messages,
                                              snapshot_parent=snapshot)
            self.activities.restore()
        return self.activities

    def _orchestrate(self, name, arguments, task_id):
        manager = self.child_activities(task_id)
        if name in {'collect', 'collect_subagents'}:
            result = manager.collect(arguments.get('handles'), arguments.get('timeout_sec', 0), arguments.get('k'))
            result['_activity_receipts'] = [r['handle'] for r in result['results']]
            return result
        if name in {'cancel', 'cancel_subagents'}:
            return manager.cancel(arguments.get('handles'))
        tasks = arguments.get('tasks', [])
        if not isinstance(tasks, list) or not tasks:
            raise ValueError('tasks must be a non-empty array')
        # Validate the entire batch before launching any children.
        for item in tasks:
            child = item.get('agent') or item.get('agent_name')
            if self.available_agent_tools is not None and child not in self.available_agent_tools:
                raise ValueError('SCOPE_DENIED: child agent is not available to the caller')
            if self.config_loader.get_tool_config(child).get('type') != 'llm_call_agent':
                raise ValueError('spawn_subagents accepts only linked child agents')
            if not isinstance(item.get('task_input'), str):
                raise ValueError('Each child must have a string task_input')
        join = arguments.get('join', 'all')
        if join not in {'all', 'any', 'k', 'background'}:
            raise ValueError('join must be all, any, k or background')
        if join == 'k' and not 1 <= int(arguments.get('k_count', 1)) <= len(tasks):
            raise ValueError('k_count is outside the batch')
        timeout = float(arguments.get('timeout_sec', 86400))
        if not 0 <= timeout <= 86400:
            raise ValueError('timeout_sec is outside the supported range')
        handles = [manager.submit(item.get('agent') or item.get('agent_name'), item['task_input'],
                    TOOL_CALL.get() or uuid.uuid4().hex, i, item.get('context_mode', 'tool'))
                   for i, item in enumerate(tasks)]
        join = arguments.get('join', 'all')
        if join not in {'all', 'any', 'k', 'background'}:
            raise ValueError('join must be all, any, k or background')
        if join == 'background':
            return {'status': 'success', 'handles': handles, 'pending': handles}
        count = 1 if join == 'any' else int(arguments.get('k_count', 1)) if join == 'k' else len(handles)
        result = manager.collect(handles, arguments.get('timeout_sec', 86400), count)
        result['handles'] = handles
        result['_activity_receipts'] = [r['handle'] for r in result['results']]
        return result

    def _agent_level(self) -> int:
        try:
            context = self.hierarchy_manager.get_context()
            hierarchy = ((context or {}).get("current") or {}).get("hierarchy") or {}
            return int(((hierarchy.get(self.agent_id) or {}).get("level")) or 0)
        except Exception:
            return 0

    def _load_tool_runtime_defaults(self) -> Dict[str, Any]:
        raw = os.environ.get("MLA_TOOL_RUNTIME_DEFAULTS_JSON", "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                result: Dict[str, Any] = {}
                for tool_name, defaults in parsed.items():
                    if isinstance(defaults, dict):
                        result[str(tool_name)] = dict(defaults)
                return result
        except Exception as exc:
            safe_print(f"⚠️  解析 MLA_TOOL_RUNTIME_DEFAULTS_JSON 失败: {exc}")
        return {}

    def _merge_runtime_tool_defaults(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        defaults = self._resolve_runtime_tool_defaults(tool_name)
        merged = dict(arguments or {})
        if not defaults:
            return merged
        for key, value in defaults.items():
            merged[str(key)] = value
        return merged

    def _resolve_runtime_tool_defaults(self, tool_name: str) -> Dict[str, Any]:
        raw = self.tool_runtime_defaults
        if not isinstance(raw, dict):
            return {}
        tool_name = str(tool_name or "").strip()
        system_name = str(getattr(self.config_loader, "agent_system_name", "") or "").strip()

        # Backward compatibility: old flat structure {tool_name: {...}}
        if tool_name in raw and isinstance(raw.get(tool_name), dict):
            return dict(raw.get(tool_name) or {})

        merged: Dict[str, Any] = {}
        global_section = raw.get("global")
        if isinstance(global_section, dict) and isinstance(global_section.get(tool_name), dict):
            merged.update(global_section.get(tool_name) or {})

        system_section = raw.get("agent_systems")
        if isinstance(system_section, dict) and system_name:
            scoped = system_section.get(system_name)
            if isinstance(scoped, dict) and isinstance(scoped.get(tool_name), dict):
                merged.update(scoped.get(tool_name) or {})

        return merged

    def _load_tool_runtime_defaults_log_mode(self) -> str:
        mode = str(os.environ.get("MLA_TOOL_RUNTIME_DEFAULTS_LOG_MODE", "fingerprint") or "fingerprint").strip().lower()
        if mode not in {"plaintext", "redact", "fingerprint"}:
            return "fingerprint"
        return mode

    @staticmethod
    def _fingerprint_runtime_value(value: Any) -> str:
        try:
            normalized = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except Exception:
            normalized = repr(value)
        return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]

    def _hidden_runtime_placeholder(self, key: str, value: Any) -> str:
        mode = self.tool_runtime_defaults_log_mode
        if mode == "plaintext":
            return str(value)
        if mode == "redact":
            return f"[RUNTIME_HIDDEN_PARAM:{key}:REDACTED]"

        value_type = type(value).__name__
        if isinstance(value, str):
            shape = f"type=str len={len(value)}"
        elif isinstance(value, (list, tuple, set)):
            shape = f"type={value_type} size={len(value)}"
        elif isinstance(value, dict):
            shape = f"type=dict keys={len(value)}"
        else:
            shape = f"type={value_type}"
        return f"[RUNTIME_HIDDEN_PARAM:{key} {shape} fp={self._fingerprint_runtime_value(value)}]"

    def _sanitize_runtime_tool_arguments(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        defaults = self._resolve_runtime_tool_defaults(tool_name)
        if not defaults:
            return dict(arguments or {})
        sanitized = dict(arguments or {})
        for key, value in defaults.items():
            if str(key) in sanitized:
                sanitized[str(key)] = self._hidden_runtime_placeholder(str(key), value)
        return sanitized

    def _sanitize_runtime_tool_result(self, tool_name: str, result: Dict[str, Any]) -> Dict[str, Any]:
        defaults = self._resolve_runtime_tool_defaults(tool_name)
        if not defaults:
            return dict(result or {})
        if self.tool_runtime_defaults_log_mode == "plaintext":
            return dict(result or {})

        secret_strings: List[tuple[str, str]] = []
        for key, value in defaults.items():
            if isinstance(value, str) and value:
                secret_strings.append((value, self._hidden_runtime_placeholder(str(key), value)))

        def _walk(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: _walk(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [_walk(v) for v in obj]
            if isinstance(obj, tuple):
                return tuple(_walk(v) for v in obj)
            if isinstance(obj, str):
                text = obj
                for secret, placeholder in secret_strings:
                    if secret in text:
                        text = text.replace(secret, placeholder)
                return text
            return obj

        return _walk(dict(result or {}))
    
    def _init_tools_registry(self):
        """
        懒加载：初始化进程内工具注册表（与 server.py 的注册中心一致）
        仅在 direct_mode=True 时使用
        """
        if self._tools_registry is not None:
            return
        
        safe_print("🔧 初始化进程内工具注册表...")
        self._tools_registry = get_runtime_registry(force_reload=True)
        safe_print(f"✅ 工具注册表初始化完成，共 {len(self._tools_registry)} 个工具")
    
    def _call_direct(self, tool_name: str, arguments: Dict, task_id: str) -> Dict:
        if tool_name in {'human_in_loop', 'fresh', 'task_history_search'}:
            return self._call_direct_inner(tool_name, arguments, task_id)
        from utils.workspaces import file_access, concurrency
        if concurrency(task_id) == 'file_lock':
            config = self.config_loader.get_tool_config(tool_name)
            safe_files = {'file_read', 'file_write', 'file_move', 'dir_list', 'dir_create', 'file_search', 'grep', 'load_skill', 'offload_skill'}
            if tool_name not in safe_files and config.get('workspace_compatible') is not True:
                return {'status': 'error', 'output': '', 'error_code': 'WORKSPACE_INCOMPATIBLE',
                        'error_information': 'This tool is not declared compatible with concurrent workspaces; use a serial workspace or explicitly declare compatibility'}
        config = self.config_loader.get_tool_config(tool_name)
        readers = {'file_read', 'dir_list', 'file_search', 'grep'}
        if config.get('parallel_safe') is True and tool_name not in readers | {'file_write', 'file_move', 'dir_create'}:
            return self._call_direct_inner(tool_name, arguments, task_id)
        with file_access(task_id, shared=tool_name in readers):
            return self._call_direct_inner(tool_name, arguments, task_id)

    def _call_direct_inner(self, tool_name: str, arguments: Dict, task_id: str) -> Dict:
        """
        进程内直接调用工具（不经过 HTTP）
        
        返回格式保持与旧工具执行层一致（data 被 json.dumps 到 output 字符串），
        确保 agent_executor 中的后续处理逻辑（如 image_read base64 提取）兼容。
        """
        try:
            # 懒加载工具注册表
            self._init_tools_registry()
            
            tool = self._tools_registry.get(tool_name)
            if not tool:
                failure_reason = ""
                for item in get_runtime_registry_failures():
                    if item.get("name") == tool_name:
                        failure_reason = item.get("error", "")
                        break
                return {
                    "status": "error",
                    "output": "",
                    "error_information": (
                        f"工具未注册到运行时: {tool_name}"
                        + (f"（加载失败原因: {failure_reason}）" if failure_reason else "")
                    )
                }
            
            safe_print(f"   🔧 直接调用工具: {tool_name}")
            
            # 调用工具（保持与既有工具返回格式兼容）
            #
            # 注意：部分工具（如 human_in_loop）只实现 execute_async（用于非阻塞等待），
            # 在 direct_mode 下也必须支持，否则会触发 NotImplementedError。
            if hasattr(tool, "execute_async") and callable(getattr(tool, "execute_async")):
                # 在子线程里创建 event loop 执行，避免未来在已有 event loop 场景下崩溃
                result_holder: Dict[str, Any] = {}
                err_holder: Dict[str, Any] = {}

                def _runner():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        try:
                            coro = tool.execute_async(task_id, arguments)
                            result_holder["tool_result"] = loop.run_until_complete(coro)
                        finally:
                            loop.close()
                    except BaseException as e:
                        err_holder["error"] = e

                context = contextvars.copy_context()
                t = threading.Thread(target=context.run, args=(_runner,), daemon=True)
                t.start()
                t.join()

                if err_holder.get("error") is not None:
                    raise err_holder["error"]
                tool_result = result_holder.get("tool_result")
            else:
                tool_result = tool.execute(task_id, arguments)
            
            # 包装返回值（保持与既有工具执行层的 output 字段格式一致）
            wrapped_status = "success"
            wrapped_error_information = ""
            if isinstance(tool_result, dict):
                inner_status = str(tool_result.get("status") or "").strip().lower()
                if inner_status:
                    wrapped_status = inner_status
                wrapped_error_information = str(
                    tool_result.get("error_information")
                    or tool_result.get("error")
                    or ""
                )
            wrapped = {
                "status": wrapped_status,
                "output": json.dumps(tool_result, indent=2, ensure_ascii=False),
                "error_information": wrapped_error_information
            }
            # 透传内部控制字段，供 agent_executor 处理特殊副作用（load/offload skill、fresh 等）
            if isinstance(tool_result, dict):
                for key, value in tool_result.items():
                    if str(key).startswith("_"):
                        wrapped[key] = value
            return wrapped
        
        except Exception as e:
            try:
                safe_print(f"❌ 直接调用工具异常: {tool_name}: {str(e)[:300]}")
            except Exception:
                pass
            return {
                "status": "error",
                "output": "",
                "error_information": f"直接调用工具失败: {str(e)}"
            }
    
    def set_task_permission(self, task_id: str, auto_mode: bool):
        """设置任务的权限模式"""
        self.task_permissions[task_id] = {"auto_mode": auto_mode}
        safe_print(f"🔐 任务权限设置: {task_id} → auto_mode={auto_mode}")
    
    def is_auto_mode(self, task_id: str) -> bool:
        """检查任务是否为自动模式（默认 True）"""
        return self.task_permissions.get(task_id, {}).get("auto_mode", True)
    
    def _request_tool_confirmation(self, tool_name: str, arguments: Dict[str, Any], task_id: str) -> bool:
        """
        请求工具执行确认
        
        Returns:
            True - 用户批准执行
            False - 用户拒绝执行
        """
        try:
            # 生成唯一确认ID
            confirm_id = f"confirm_{tool_name}_{int(time.time())}_{uuid.uuid4().hex[:8]}"
            from tool_server_lite.tools.human_tools import (
                create_tool_confirmation,
                get_tool_confirmation_status,
            )

            created = create_tool_confirmation(confirm_id, task_id, tool_name, self._sanitize_runtime_tool_arguments(tool_name, arguments))
            if not created.get("success"):
                safe_print("⚠️  创建确认请求失败，默认拒绝执行")
                return False
            
            safe_print(f"⏸️  等待用户确认: {tool_name}")
            
            # 轮询等待用户响应（最多等待 300 秒）
            max_wait = 300
            check_interval = .2
            elapsed = 0
            
            while elapsed < max_wait:
                try:
                    check_cancelled()
                except BaseException:
                    from utils import interactions
                    path, _ = interactions.find(confirm_id, task_id)
                    if path:
                        interactions.end_wait(path, 'cancelled')
                    raise
                time.sleep(check_interval)
                elapsed += check_interval
                
                try:
                    result = get_tool_confirmation_status(confirm_id)
                    if result.get("found") and result.get("status") == "completed":
                        approved = result.get("result") == "approved"
                        if approved:
                            safe_print(f"✅ 用户批准执行: {tool_name}")
                        else:
                            safe_print(f"❌ 用户拒绝执行: {tool_name}")
                        return approved
                except Exception:
                    continue
            
            # 超时，默认拒绝
            safe_print(f"⏱️  确认超时，拒绝执行: {tool_name}")
            return False
            
        except Exception as e:
            safe_print(f"❌ 确认请求失败: {e}，拒绝执行")
            return False
    
    def execute(self, tool_name: str, arguments: Dict[str, Any], task_id: str) -> Dict:
        """
        执行工具调用
        
        Args:
            tool_name: 工具名称
            arguments: 工具参数
            task_id: 任务ID
            
        Returns:
            执行结果字典
        """
        check_cancelled()
        if self.available_agent_tools is not None and tool_name not in self.available_agent_tools:
            return {'status': 'error', 'output': '', 'error_code': 'SCOPE_DENIED',
                    'error_information': 'Tool is not available to this agent'}
        merged_arguments = self._merge_runtime_tool_defaults(tool_name, arguments)
        try:
            declaration = self.config_loader.get_tool_config(tool_name)
        except KeyError:
            return {'status': 'error', 'output': '', 'error_code': 'UNKNOWN_TOOL', 'error_information': 'Unknown tool'}
        schema = declaration.get('parameters')
        if isinstance(schema, dict) and schema:
            import jsonschema
            properties = schema.get('properties', {})
            if 'task_id' in properties:
                merged_arguments['task_id'] = task_id
            effective = dict(arguments or {})
            for key, value in merged_arguments.items():
                if key in properties:
                    effective[key] = value
            try:
                jsonschema.validate(effective, schema)
            except jsonschema.ValidationError as exc:
                return {'status': 'error', 'output': '', 'error_code': 'INVALID_TOOL_ARGUMENTS',
                        'error_information': 'Invalid tool arguments at ' + '.'.join(str(p) for p in exc.path)}
            except jsonschema.SchemaError:
                return {'status': 'error', 'output': '', 'error_code': 'INVALID_TOOL_SCHEMA',
                        'error_information': 'Tool declaration has an invalid JSON schema'}
        logged_arguments = self._sanitize_runtime_tool_arguments(tool_name, merged_arguments)
        try:
            trigger_tool_hooks(
                when="before",
                tool_name=tool_name,
                task_id=task_id,
                arguments=logged_arguments,
                agent_id=self.agent_id,
                agent_name=self.agent_name,
                agent_level=self._agent_level(),
            )
        except Exception:
            pass

        try:
            # 获取工具配置
            tool_config = self.config_loader.get_tool_config(tool_name)
            tool_type = tool_config.get("type")
            # 特殊处理final_output
            if tool_name in {'spawn_subagents', 'collect', 'cancel', 'collect_subagents', 'cancel_subagents'}:
                result = self._orchestrate(tool_name, merged_arguments, task_id)
            elif tool_name == "final_output":
                if self.activities is not None and self.activities.outstanding():
                    return {'status': 'error', 'output': '', 'error_code': 'CHILDREN_PENDING',
                            'error_information': 'Collect or cancel and collect all child activities before final_output'}
                result = {
                    "status": merged_arguments.get("status", "success"),
                    "output": merged_arguments.get("output", ""),
                    "error_information": merged_arguments.get("error_information", "")
                }
            elif tool_type == "tool_call_agent":
                if tool_name in self.DANGEROUS_TOOLS and not self.is_auto_mode(task_id):
                    approved = self._request_tool_confirmation(tool_name, merged_arguments, task_id)
                    if not approved:
                        result = {
                            "status": "error",
                            "output": "",
                            "error_information": f"工具执行被用户拒绝: {tool_name}"
                        }
                    elif tool_config.get("_mcp"):
                        result = call_mcp_tool(tool_config, merged_arguments)
                    else:
                        result = self._call_direct(tool_name, merged_arguments, task_id)
                elif tool_config.get("_mcp"):
                    result = call_mcp_tool(tool_config, merged_arguments)
                else:
                    result = self._call_direct(tool_name, merged_arguments, task_id)
            elif tool_type == "llm_call_agent":
                if merged_arguments.get('_background'):
                    handle = self.child_activities(task_id).submit(tool_name,
                        str(merged_arguments.get('task_input', '')), TOOL_CALL.get() or uuid.uuid4().hex,
                        context_mode=merged_arguments.get('_context_mode', 'tool'))
                    result = {'status': 'success', 'handle': handle, 'pending': [handle]}
                else:
                    manager = self.child_activities(task_id)
                    handle = manager.submit(tool_name, str(merged_arguments.get('task_input', '')),
                        TOOL_CALL.get() or uuid.uuid4().hex,
                        context_mode=merged_arguments.get('_context_mode', 'tool'), background=False)
                    joined = manager.collect([handle], 86400)
                    if joined['results']:
                        result = {**joined['results'][0]['result'], '_activity_receipts': [handle]}
                    else:
                        result = {'status': 'pending', 'output': 'Child is still running; collect its result later.',
                                  'handle': handle, 'interrupted_by_user_message': joined['interrupted_by_user_message']}
            else:
                result = {
                    "status": "error",
                    "output": "",
                    "error_information": f"不支持的工具类型: {tool_type}"
                }
        except (StateCommitError, StateCorruptError):
            raise
        except Exception as e:
            result = {
                "status": "error",
                "output": "",
                "error_information": f"工具执行失败: {str(e)}"
            }

        logged_result = self._sanitize_runtime_tool_result(tool_name, result)
        try:
            trigger_tool_hooks(
                when="after",
                tool_name=tool_name,
                task_id=task_id,
                arguments=logged_arguments,
                result=logged_result,
                agent_id=self.agent_id,
                agent_name=self.agent_name,
                agent_level=self._agent_level(),
            )
        except Exception:
            pass
        return result
    
    def _execute_sub_agent(
        self,
        agent_name: str,
        agent_config: Dict,
        arguments: Dict,
        task_id: str
    ) -> Dict:
        """执行子Agent调用"""
        try:
            # 导入Agent执行器（避免循环导入）
            from core.agent_executor import AgentExecutor
            
            # 获取任务输入
            task_input = arguments.get("task_input", "")
            
            # 创建子Agent执行器（传递 direct_mode 保持一致）
            sub_agent = AgentExecutor(
                agent_name=agent_name,
                agent_config=agent_config,
                config_loader=self.config_loader,
                hierarchy_manager=self.hierarchy_manager,
                direct_tools=self.direct_mode,
                extra_event_handlers=self.extra_event_handlers,
                exit_on_error=self.exit_on_error,
                raise_on_error=self.raise_on_error,
                stream_llm_tokens=self.stream_llm_tokens,
            )
            
            # 执行子Agent
            sub_agent.tool_executor.set_task_permission(task_id, self.is_auto_mode(task_id))
            sub_agent.context_mode = arguments.get('_context_mode', 'tool')
            result = sub_agent.run(task_id, task_input)
            
            return result
        
        except (StateCommitError, StateCorruptError):
            raise
        except Exception as e:
            import traceback
            error_detail = traceback.format_exc()
            safe_print(f"❌ 子Agent执行失败: {e}")
            safe_print(f"详细错误:\n{error_detail}")
            return {
                "status": "error",
                "output": "",
                "error_information": f"子Agent执行失败: {str(e)}\n{error_detail}"
            }


if __name__ == "__main__":
    from utils.config_loader import ConfigLoader
    from core.hierarchy_manager import get_hierarchy_manager
    
    # 测试工具执行器
    config_loader = ConfigLoader("infiHelper")
    hierarchy_manager = get_hierarchy_manager("test_task")
    
    executor = ToolExecutor(config_loader, hierarchy_manager)
    safe_print(f"✅ 工具执行器初始化成功")
    safe_print("   Mode: direct-tools")
    
    # 测试final_output
    result = executor.execute("final_output", {
        "task_id": "test",
        "status": "success",
        "output": "测试完成"
    }, "test_task")
    
    safe_print(f"✅ final_output测试: {result}")
