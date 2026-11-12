#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
运行时控制信号。

目前用于：
- 外部 fresh 请求（支持按 task_id 定向）
- 运行中 task 注册（支持跨进程查询某个 task 是否仍在运行）
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from utils.user_paths import get_user_runtime_dir

_lock = threading.Lock()
_local_task_refcounts: Dict[str, int] = {}


def _runtime_root() -> Path:
    root = get_user_runtime_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _fresh_request_dir() -> Path:
    path = _runtime_root() / "fresh_requests"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _running_task_dir() -> Path:
    path = _runtime_root() / "running_tasks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _task_key(task_id: str) -> str:
    task_id = str(task_id or "").strip()
    task_hash = hashlib.md5(task_id.encode("utf-8")).hexdigest()[:8]
    task_name = Path(task_id).name or "task"
    return f"{task_hash}_{task_name}"


def _fresh_request_file(task_id: Optional[str]) -> Path:
    if task_id:
        return _fresh_request_dir() / f"{_task_key(task_id)}.json"
    return _fresh_request_dir() / "_broadcast.json"


def _running_task_file(task_id: str) -> Path:
    return _running_task_dir() / f"{_task_key(task_id)}.json"


def _write_json_atomic(path: Path, payload: Dict):
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _read_json(path: Path) -> Optional[Dict]:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def request_fresh(reason: str = "", task_id: Optional[str] = None):
    task_id = str(task_id or "").strip() or None
    payload = {
        "reason": str(reason or "").strip() or "external fresh request",
        "task_id": task_id,
        "requested_at": datetime.now().isoformat(),
        "requested_pid": os.getpid(),
    }
    with _lock:
        _write_json_atomic(_fresh_request_file(task_id), payload)


def pop_fresh_request(task_id: str) -> Optional[str]:
    task_id = str(task_id or "").strip()
    if not task_id:
        return None

    with _lock:
        for path in (_fresh_request_file(task_id), _fresh_request_file(None)):
            data = _read_json(path)
            if not data:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return str(data.get("reason") or "").strip() or "external fresh request"
    return None


def register_running_task(task_id: str, agent_name: str, user_input: str, agent_system: str):
    task_id = str(task_id or "").strip()
    if not task_id:
        return

    with _lock:
        refcount = _local_task_refcounts.get(task_id, 0) + 1
        _local_task_refcounts[task_id] = refcount
        if refcount > 1:
            return

        payload = {
            "task_id": task_id,
            "agent_name": str(agent_name or "").strip(),
            "user_input": str(user_input or "").strip(),
            "agent_system": str(agent_system or "").strip(),
            "pid": os.getpid(),
            "registered_at": datetime.now().isoformat(),
        }
        import psutil
        payload['process_created'] = psutil.Process(os.getpid()).create_time()
        _write_json_atomic(_running_task_file(task_id), payload)


def unregister_running_task(task_id: str):
    task_id = str(task_id or "").strip()
    if not task_id:
        return

    with _lock:
        current = _local_task_refcounts.get(task_id, 0)
        if current > 1:
            _local_task_refcounts[task_id] = current - 1
            return
        _local_task_refcounts.pop(task_id, None)

        path = _running_task_file(task_id)
        data = _read_json(path)
        if not data:
            return
        if int(data.get("pid") or 0) != os.getpid():
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def get_running_task(task_id: str) -> Optional[Dict]:
    task_id = str(task_id or "").strip()
    if not task_id:
        return None

    with _lock:
        path = _running_task_file(task_id)
        data = _read_json(path)
        if not data:
            return None

        pid = int(data.get("pid") or 0)
        if _is_pid_alive(pid):
            expected = data.get('process_created')
            if expected is None:
                return data
            import psutil
            try:
                if abs(psutil.Process(pid).create_time() - float(expected)) < .001:
                    return data
            except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
                pass

        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return None


def is_task_running(task_id: str) -> bool:
    return get_running_task(task_id) is not None
