#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Optional task-scoped filesystem sandbox policy.

The default desktop/local behavior remains disabled. Docker deployment can turn
this on through MLA_SANDBOX_ENABLED and a user_root config file.
"""

from __future__ import annotations

import json
import os
import re
import sys
import ctypes
import hashlib
import stat
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml


_SECRET_NAME_RE = re.compile(
    r"(API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE|SESSION|PRIVATE[_-]?KEY)",
    re.IGNORECASE,
)


DEFAULT_HOST_READ_ROOTS = [
    "/bin",
    "/sbin",
    "/usr",
    "/lib",
    "/lib64",
    "/opt",
    "/etc",
    "/dev",
    "/app",
]

DEFAULT_ENV_PASSTHROUGH = [
    "PATH",
    "PYTHONPATH",
    "VIRTUAL_ENV",
    "CONDA_PREFIX",
    "CONDA_DEFAULT_ENV",
    "MLA_EXTERNAL_TOOL_PYTHON",
    "MLA_PYTHON_BIN",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
    "LANG",
    "LC_ALL",
    "TZ",
    "GUROBI_HOME",
    "GRB_LICENSE_FILE",
    "LD_LIBRARY_PATH",
]


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _string_list(value: Any) -> List[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _dedupe_paths(paths: Iterable[str | Path]) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in paths:
        raw = str(item or "").strip()
        if not raw:
            continue
        try:
            path = Path(raw).expanduser().resolve(strict=False)
        except Exception:
            continue
        text = str(path)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def get_user_root() -> Optional[Path]:
    raw = os.environ.get("MLA_USER_DATA_ROOT") or os.environ.get("INFIAGENT_USER_ROOT") or ""
    if not str(raw).strip():
        return None
    return Path(raw).expanduser().resolve()


def get_sandbox_config_path() -> Optional[Path]:
    raw = str(os.environ.get("MLA_SANDBOX_CONFIG_PATH") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    user_root = get_user_root()
    if user_root:
        return user_root / "config" / "sandbox_config.yaml"
    return None


def load_sandbox_config() -> Dict[str, Any]:
    path = get_sandbox_config_path()
    payload: Dict[str, Any] = {}
    if path and path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                payload = data.get("sandbox") if isinstance(data.get("sandbox"), dict) else data
        except Exception:
            payload = {}

    env_enabled = os.environ.get("MLA_SANDBOX_ENABLED")
    if env_enabled not in (None, ""):
        payload["enabled"] = _truthy(env_enabled)
    env_engine = os.environ.get("MLA_SANDBOX_ENGINE")
    if env_engine:
        payload["engine"] = env_engine
    return payload


def sandbox_enabled() -> bool:
    return bool(load_sandbox_config().get("enabled", False))


def enforce_file_tools_task_root() -> bool:
    cfg = load_sandbox_config()
    if not bool(cfg.get("enabled", False)):
        return False
    file_tools = cfg.get("file_tools") if isinstance(cfg.get("file_tools"), dict) else {}
    return bool(file_tools.get("enforce_task_root", True))


def command_sandbox_enabled() -> bool:
    cfg = load_sandbox_config()
    if not bool(cfg.get("enabled", False)):
        return False
    command = cfg.get("command") if isinstance(cfg.get("command"), dict) else {}
    return bool(command.get("enabled", True))


def command_engine() -> str:
    cfg = load_sandbox_config()
    command = cfg.get("command") if isinstance(cfg.get("command"), dict) else {}
    return str(command.get("engine") or cfg.get("engine") or "landlock").strip().lower()


def command_fallback_engine() -> str:
    cfg = load_sandbox_config()
    command = cfg.get("command") if isinstance(cfg.get("command"), dict) else {}
    return str(command.get("fallback_engine") or cfg.get("fallback_engine") or "unix_permissions").strip().lower()


def command_fail_closed() -> bool:
    cfg = load_sandbox_config()
    command = cfg.get("command") if isinstance(cfg.get("command"), dict) else {}
    return bool(command.get("fail_closed", cfg.get("fail_closed", True)))


def landlock_abi() -> int:
    if not sys.platform.startswith("linux"):
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        # landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)
        abi = int(libc.syscall(444, 0, 0, 1))
        return abi if abi > 0 else 0
    except Exception:
        return 0


def landlock_available() -> bool:
    return landlock_abi() > 0


def command_effective_engine() -> str:
    engine = command_engine()
    if engine in {"auto", "best", "default"}:
        if landlock_available():
            return "landlock"
        return command_fallback_engine()
    return engine


def resolve_task_path(task_id: str, path_value: str, *, allow_empty: bool = False) -> Path:
    from utils.workspaces import files_root
    root = files_root(task_id)
    raw_text = str(path_value or "").strip()
    if not raw_text:
        if allow_empty:
            return root
        raise ValueError("路径不能为空")
    raw = Path(raw_text).expanduser()
    if raw.is_absolute():
        raise ValueError(f"沙箱模式不允许绝对路径: {path_value}")
    candidate = (root / raw).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError:
        raise ValueError(f"路径越界，只允许当前 task 内路径: {path_value}")
    return candidate


def apply_file_tool_path_policy(task_id: str, path_value: str, *, allow_empty: bool = True) -> Optional[Path]:
    if not enforce_file_tools_task_root():
        return None
    return resolve_task_path(task_id, path_value, allow_empty=allow_empty)


def task_runtime_dirs(task_root: str | Path) -> Dict[str, str]:
    root = Path(task_root).expanduser().resolve()
    home = root / ".home"
    tmp = root / ".tmp"
    cache = root / ".cache"
    pip_cache = cache / "pip"
    pycache = cache / "pycache"
    venv = root / ".venv"
    for path in (home, tmp, pip_cache, pycache):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "home": str(home),
        "tmp": str(tmp),
        "cache": str(cache),
        "pip_cache": str(pip_cache),
        "pycache": str(pycache),
        "venv": str(venv),
    }


def task_sandbox_ids(task_id: str | Path) -> Dict[str, int]:
    root = str(Path(str(task_id)).expanduser().resolve())
    digest = hashlib.sha256(root.encode("utf-8")).digest()
    # High, deterministic numeric ids avoid depending on /etc/passwd entries.
    uid = 200000 + (int.from_bytes(digest[:4], "big") % 50000000)
    return {"uid": uid, "gid": uid}


def _chmod_tree(root: Path, *, dir_mode: int, file_mode: int, executable_file_mode: Optional[int] = None) -> None:
    if not root.exists():
        return
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        try:
            if not current_path.is_symlink():
                os.chmod(current_path, dir_mode)
        except OSError:
            pass
        for dirname in dirnames:
            path = current_path / dirname
            try:
                if not path.is_symlink():
                    os.chmod(path, dir_mode)
            except OSError:
                pass
        for filename in filenames:
            path = current_path / filename
            try:
                if path.is_symlink():
                    continue
                mode = stat.S_IMODE(path.stat().st_mode)
                target_mode = executable_file_mode if executable_file_mode and (mode & 0o111) else file_mode
                os.chmod(path, target_mode)
            except OSError:
                pass


def _chown_task_tree(task_root: Path, uid: int, gid: int) -> None:
    if not task_root.exists():
        task_root.mkdir(parents=True, exist_ok=True)
    for current, dirnames, filenames in os.walk(task_root, topdown=True, followlinks=False):
        current_path = Path(current)
        try:
            os.chown(current_path, uid, gid, follow_symlinks=False)
            if not current_path.is_symlink():
                os.chmod(current_path, 0o700)
        except OSError:
            pass
        for dirname in dirnames:
            path = current_path / dirname
            try:
                os.chown(path, uid, gid, follow_symlinks=False)
                if not path.is_symlink():
                    os.chmod(path, 0o700)
            except OSError:
                pass
        for filename in filenames:
            path = current_path / filename
            try:
                os.chown(path, uid, gid, follow_symlinks=False)
                if path.is_symlink():
                    continue
                mode = stat.S_IMODE(path.stat().st_mode)
                os.chmod(path, 0o700 if (mode & 0o111) else 0o600)
            except OSError:
                pass


def prepare_unix_permission_sandbox(task_id: str, cwd: str) -> Dict[str, int]:
    if not should_apply_unix_permission_sandbox():
        return {}
    if os.geteuid() != 0:
        if command_fail_closed():
            raise PermissionError("Unix permission sandbox requires the server process to run as root")
        return {}

    task_root = Path(task_id).expanduser().resolve()
    ids = task_sandbox_ids(task_root)
    uid = ids["uid"]
    gid = ids["gid"]
    task_runtime_dirs(task_root)

    user_root = get_user_root()
    if user_root:
        # Keep server-only state and model credentials root-only, while allowing
        # traversal into tasks and read-only resource/library roots.
        for private_name in ("config", "conversations", "runtime"):
            private_path = user_root / private_name
            if private_path.exists():
                try:
                    os.chown(private_path, 0, 0, follow_symlinks=False)
                    os.chmod(private_path, 0o700)
                except OSError:
                    pass
        tasks_root = user_root / "tasks"
        if tasks_root.exists():
            try:
                os.chown(tasks_root, 0, 0, follow_symlinks=False)
                os.chmod(tasks_root, 0o711)
            except OSError:
                pass
            try:
                for child in tasks_root.iterdir():
                    if child.resolve(strict=False) == task_root:
                        continue
                    if child.is_symlink():
                        continue
                    try:
                        os.chmod(child, 0o700 if child.is_dir() else 0o600)
                    except OSError:
                        pass
            except OSError:
                pass
        for public_name in ("resources", "agent_library", "skills", "tools_library"):
            _chmod_tree(user_root / public_name, dir_mode=0o755, file_mode=0o644, executable_file_mode=0o755)

    _chown_task_tree(task_root, uid, gid)
    return {"user": uid, "group": gid, "umask": 0o077}


def sanitize_command_env(task_id: str, env: Dict[str, str]) -> Dict[str, str]:
    if not command_sandbox_enabled():
        return env

    cfg = load_sandbox_config()
    command = cfg.get("command") if isinstance(cfg.get("command"), dict) else {}
    extra_env = set(_string_list(command.get("env_passthrough")))
    passthrough = set(DEFAULT_ENV_PASSTHROUGH) | extra_env

    clean: Dict[str, str] = {}
    for key, value in env.items():
        if key in passthrough or key.startswith("PIP_") or key.startswith("pip_"):
            if _SECRET_NAME_RE.search(key):
                continue
            clean[key] = str(value)

    root = Path(task_id).expanduser().resolve()
    dirs = task_runtime_dirs(root)
    clean["HOME"] = dirs["home"]
    clean["TMPDIR"] = dirs["tmp"]
    clean["TMP"] = dirs["tmp"]
    clean["TEMP"] = dirs["tmp"]
    clean["PIP_CACHE_DIR"] = dirs["pip_cache"]
    clean["PYTHONPYCACHEPREFIX"] = dirs["pycache"]
    clean["PYTHONDONTWRITEBYTECODE"] = "1"
    clean["MLA_SANDBOX_TASK_ROOT"] = str(root)
    if command_fail_closed():
        clean["MLA_SANDBOX_FAIL_CLOSED"] = "1"
    return clean


def _existing(paths: Iterable[str | Path]) -> List[str]:
    result = []
    for item in paths:
        try:
            path = Path(str(item)).expanduser().resolve(strict=False)
            if path.exists():
                result.append(str(path))
        except Exception:
            continue
    return _dedupe_paths(result)


def build_landlock_rules(task_id: str, cwd: str) -> Dict[str, Any]:
    cfg = load_sandbox_config()
    command = cfg.get("command") if isinstance(cfg.get("command"), dict) else {}
    user_root = get_user_root()
    task_root = Path(task_id).expanduser().resolve()
    dirs = task_runtime_dirs(task_root)

    host_ro = _string_list(command.get("host_read_roots")) or list(DEFAULT_HOST_READ_ROOTS)
    host_ro.extend(_string_list(command.get("extra_read_roots")))
    host_ro.extend(_string_list(cfg.get("extra_read_roots")))

    if user_root:
        for child in ("resources", "agent_library", "skills", "tools_library"):
            host_ro.append(str(user_root / child))

    # Common Docker and scientific/commercial solver locations. Operators can
    # extend these in user_root/config/sandbox_config.yaml for Gurobi, CPLEX, etc.
    host_ro.extend(["/usr/local/gurobi", "/opt/gurobi", "/opt/gurobi1000", "/opt/gurobi1100", "/opt/gurobi1200"])

    task_rw = [str(task_root), dirs["home"], dirs["tmp"], dirs["cache"]]
    task_rw.extend(_string_list(command.get("extra_read_write_roots")))

    return {
        "enabled": command_sandbox_enabled(),
        "engine": command_effective_engine(),
        "configured_engine": command_engine(),
        "fail_closed": command_fail_closed(),
        "task_root": str(task_root),
        "cwd": str(Path(cwd).expanduser().resolve(strict=False)),
        "read_only": _existing(host_ro),
        "read_write": _existing(task_rw),
    }


def prepare_landlock_env(task_id: str, cwd: str, env: Dict[str, str]) -> Dict[str, str]:
    clean = sanitize_command_env(task_id, env)
    clean["MLA_LANDLOCK_RULES_JSON"] = json.dumps(build_landlock_rules(task_id, cwd), ensure_ascii=False)
    return clean


def should_wrap_command_with_landlock() -> bool:
    return command_sandbox_enabled() and command_effective_engine() == "landlock" and sys.platform.startswith("linux")


def should_apply_unix_permission_sandbox() -> bool:
    engine = command_effective_engine()
    return (
        command_sandbox_enabled()
        and engine in {"unix_permissions", "unix-permissions", "unix_permissions_fallback", "unix"}
        and sys.platform.startswith("linux")
    )
