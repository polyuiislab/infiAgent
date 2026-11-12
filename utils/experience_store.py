#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""经验存储：并发安全的 Markdown 经验文件读写。

设计要点（3.12.23）：
- 跨进程/跨线程互斥：每个经验文件配一个旁路锁文件（<name>.lock），flock（POSIX）/
  msvcrt.locking（Windows）阻塞获取；锁覆盖完整的 read-modify-write（含首次建文件）。
- 原子写入：同目录 mkstemp 临时文件 → write + flush + fsync → os.replace；
  失败清理临时文件，旧文件永不受损。
- entry_id：时间前缀 + uuid4 片段，跨进程不冲突（旧格式 时间+内容MD5 同秒同内容必撞）。
- 文件级 revision：标题后 `<!-- revision: N -->` 注释，每次成功变更 +1；
  旧文件无此注释按 0 解析（向后兼容）。
- 乐观并发：replace/delete 支持 expected_revision（文件级）与 expected_updated_at
  （条目级），不匹配抛 ConcurrencyConflictError，检查发生在任何写入之前。
- 多目标事务（scope=both）：按路径排序锁全部目标 → 全部先算后写 → 任一写失败
  回滚已写目标到旧字节。
- replace/delete 语义对齐：都作用于全部同 ID 条目（新 ID 唯一，仅旧重复 ID 受影响）。
- 淘汰：append 超出 max_tokens 从最旧开始淘汰，但绝不淘汰本次新增条目；
  被淘汰的 entry_id 随结果返回。
"""

from __future__ import annotations
from utils.atomic_state import retry_windows_sharing


import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from core.hierarchy_manager import get_hierarchy_manager
from utils.user_paths import get_user_data_root


ENTRY_SEPARATOR = "\n\n---\n\n"
_REVISION_RE = re.compile(r"<!--\s*revision:\s*(\d+)\s*-->")


class ConcurrencyConflictError(RuntimeError):
    """乐观并发检查失败（expected_revision / expected_updated_at 不匹配）。"""


@dataclass
class ExperienceEntry:
    entry_id: str
    created_at: str
    updated_at: str
    source: str
    status: str
    content: str


@dataclass
class AppendResult:
    entry: ExperienceEntry
    revision: int
    evicted_ids: List[str] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, len(text) // 4)


def resolve_runtime_agent_context(task_id: str) -> Dict[str, str]:
    manager = get_hierarchy_manager(task_id)
    runtime = manager.get_runtime_metadata()
    stack = manager._load_stack()

    from utils.execution_scope import CURRENT
    scope = CURRENT.get()
    if scope and scope.agent_name and scope.task_id == str(Path(task_id).resolve()):
        return {"agent_name": scope.agent_name, "agent_system": str(runtime.get("agent_system") or "OpenCowork")}
    current_agent_name = ""
    if isinstance(stack, list) and stack:
        top = stack[-1] or {}
        current_agent_name = str(top.get("agent_name") or "").strip()
    if not current_agent_name:
        current_agent_name = str((runtime or {}).get("agent_name") or "alpha_agent").strip() or "alpha_agent"

    current_agent_system = str((runtime or {}).get("agent_system") or "OpenCowork").strip() or "OpenCowork"
    return {
        "agent_name": current_agent_name,
        "agent_system": current_agent_system,
    }


def task_experience_path(task_id: str | Path, agent_name: str) -> Path:
    """纯路径解析：不再在读路径上创建文件（创建统一发生在锁内的写操作里）。"""
    task_root = Path(task_id).expanduser().resolve()
    return task_root / "experience" / f"{str(agent_name or '').strip() or 'alpha_agent'}.md"


def global_experience_path(agent_system: str, agent_name: str) -> Path:
    root = get_user_data_root()
    return (
        root
        / "knowledge"
        / "experience"
        / (str(agent_system or "").strip() or "OpenCowork")
        / f"{str(agent_name or '').strip() or 'alpha_agent'}.md"
    )


# ==================== 锁与原子写 ====================

@contextmanager
def _file_lock(path: Path, *, shared: bool = False, blocking: bool = True, lock_file: bool = False) -> Iterator[None]:
    """Cross-process file lock; writers may request an immediate busy result."""
    lock_path = path if lock_file else path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b'0')
                handle.flush()
            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
                    break
                except OSError as exc:
                    if not blocking:
                        raise BlockingIOError('File is busy') from exc
                    from utils.execution_scope import check_cancelled
                    check_cancelled()
        else:
            import fcntl
            mode = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            fcntl.flock(handle.fileno(), mode | (0 if blocking else fcntl.LOCK_NB))
        locked = True
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def _locked_many(paths: List[Path]) -> Iterator[None]:
    """按绝对路径排序依次加锁（全局固定顺序，避免死锁）。"""
    ordered = sorted({str(Path(p).resolve()): Path(p) for p in paths}.items())
    ctxs = []
    try:
        for _, p in ordered:
            ctx = _file_lock(p)
            ctx.__enter__()
            ctxs.append(ctx)
        yield
    finally:
        for ctx in reversed(ctxs):
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + flush + fsync + os.replace；失败不破坏旧文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        retry_windows_sharing(lambda: os.replace(tmp_name, path))
        try:  # 目录项持久化（尽力而为）
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ==================== 解析与渲染 ====================

def _strip_header(text: str) -> str:
    raw = str(text or "").strip()
    if raw.startswith("# "):
        parts = raw.split("\n\n", 1)
        raw = parts[1].strip() if len(parts) == 2 else ""
    return raw


def read_experience_file(path: Path) -> Tuple[int, List[ExperienceEntry]]:
    """返回 (revision, entries)；文件不存在 → (0, [])；无 revision 注释按 0（旧格式兼容）。"""
    if not path.exists():
        return 0, []
    text = retry_windows_sharing(lambda: path.read_text(encoding="utf-8"))
    match = _REVISION_RE.search(text.split("\n\n", 1)[0] if text.startswith("# ") else text[:200])
    revision = int(match.group(1)) if match else 0
    return revision, _parse_entries_text(text)


def _parse_entries_text(text: str) -> List[ExperienceEntry]:
    body = _strip_header(text)
    if not body:
        return []
    entries: List[ExperienceEntry] = []
    for block in [item.strip() for item in body.split(ENTRY_SEPARATOR) if item.strip()]:
        lines = block.splitlines()
        if not lines or not lines[0].startswith("## "):
            continue
        entry_id = lines[0][3:].strip()
        created_at = ""
        updated_at = ""
        source = ""
        status = "active"
        content_lines: List[str] = []
        in_content = False
        for line in lines[1:]:
            if not in_content and line.startswith("- "):
                key, _, value = line[2:].partition(":")
                key = key.strip()
                value = value.strip()
                if key == "created_at":
                    created_at = value
                elif key == "updated_at":
                    updated_at = value
                elif key == "source":
                    source = value
                elif key == "status":
                    status = value or "active"
                continue
            if not in_content and line.strip() == "":
                in_content = True
                continue
            in_content = True
            content_lines.append(line)
        entries.append(
            ExperienceEntry(
                entry_id=entry_id,
                created_at=created_at,
                updated_at=updated_at or created_at,
                source=source or "agent",
                status=status or "active",
                content="\n".join(content_lines).strip(),
            )
        )
    return entries


def parse_experience_entries(path: Path) -> List[ExperienceEntry]:
    if not path.exists():
        return []
    return _parse_entries_text(path.read_text(encoding="utf-8"))


def _render_entry(entry: ExperienceEntry) -> str:
    return "\n".join(
        [
            f"## {entry.entry_id}",
            f"- created_at: {entry.created_at}",
            f"- updated_at: {entry.updated_at}",
            f"- source: {entry.source}",
            f"- status: {entry.status}",
            "",
            entry.content.strip(),
        ]
    ).strip()


def render_entries_block(entries: List[ExperienceEntry]) -> str:
    """渲染纯条目区块（无文件头），供上下文注入使用。"""
    return ENTRY_SEPARATOR.join(_render_entry(entry) for entry in entries)


def render_experience_entries(title: str, entries: List[ExperienceEntry], revision: int = 0) -> str:
    body = render_entries_block(entries)
    header = f"# {title}\n<!-- revision: {int(revision)} -->\n\n"
    return header + (body + "\n" if body else "")


def write_experience_entries(path: Path, title: str, entries: List[ExperienceEntry]) -> None:
    """整体覆写（兼容 API）：加锁 + 原子写 + revision 自增。"""
    with _file_lock(path):
        revision, _ = read_experience_file(path)
        _atomic_write_text(path, render_experience_entries(title, entries, revision + 1))


def make_entry_id(content: str = "") -> str:
    """时间前缀（可读）+ uuid4 片段（唯一性）；content 参数仅为签名兼容保留。"""
    return f"exp_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:12]}"


# ==================== 变更操作（锁内 read-modify-write） ====================

def _check_expected(
    entries: List[ExperienceEntry],
    revision: int,
    entry_id: str,
    expected_revision: Optional[int],
    expected_updated_at: Optional[str],
    path: Path,
) -> None:
    if expected_revision is not None and int(expected_revision) != revision:
        raise ConcurrencyConflictError(
            f"revision 冲突: 期望 {expected_revision}，当前 {revision}（{path.name}）。请重新 list 后重试。"
        )
    if expected_updated_at:
        matched = [e for e in entries if e.entry_id == entry_id]
        if matched and any(e.updated_at != str(expected_updated_at) for e in matched):
            current = ", ".join(sorted({e.updated_at for e in matched}))
            raise ConcurrencyConflictError(
                f"updated_at 冲突: 期望 {expected_updated_at}，当前 {current}（{entry_id}）。请重新 list 后重试。"
            )


def _evict_over_budget(entries: List[ExperienceEntry], max_tokens: int, protect_id: str) -> List[str]:
    """从最旧开始淘汰直到预算内；绝不淘汰 protect_id（本次新增条目）。"""
    evicted: List[str] = []
    def _total() -> int:
        return estimate_tokens("\n".join(item.content for item in entries))
    index = 0
    while _total() > max_tokens and index < len(entries):
        if entries[index].entry_id == protect_id:
            index += 1
            continue
        evicted.append(entries[index].entry_id)
        entries.pop(index)
    return evicted


def append_experience_many(
    targets: List[Tuple[Path, str]],
    content: str,
    *,
    source: str,
    max_tokens: int,
) -> List[Dict[str, Any]]:
    """向多个目标事务性追加同一条经验（scope=both 的一致性保证）。"""
    return _transactional_mutation(
        targets,
        lambda entries, revision, path, title: _append_mutation(entries, content, source, max_tokens),
    )


def _append_mutation(entries: List[ExperienceEntry], content: str, source: str, max_tokens: int):
    now = datetime.now().isoformat(timespec="seconds")
    entry = ExperienceEntry(
        entry_id=make_entry_id(),
        created_at=now,
        updated_at=now,
        source=source,
        status="active",
        content=str(content or "").strip(),
    )
    entries.append(entry)
    evicted = _evict_over_budget(entries, max_tokens, protect_id=entry.entry_id)
    return entries, {"entry_id": entry.entry_id, "evicted_ids": evicted, "entry": entry}


def replace_experience_many(
    targets: List[Tuple[Path, str]],
    entry_id: str,
    content: str,
    *,
    source: str,
    expected_revision: Optional[int] = None,
    expected_updated_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    def mutation(entries, revision, path, title):
        _check_expected(entries, revision, entry_id, expected_revision, expected_updated_at, path)
        now = datetime.now().isoformat(timespec="seconds")
        replaced_ids: List[str] = []
        for idx, entry in enumerate(entries):
            if entry.entry_id != entry_id:
                continue
            entries[idx] = ExperienceEntry(
                entry_id=entry.entry_id,
                created_at=entry.created_at,
                updated_at=now,
                source=source or entry.source,
                status=entry.status,
                content=str(content or "").strip(),
            )
            replaced_ids.append(entry.entry_id)
        return entries, {"replaced": bool(replaced_ids), "affected": len(replaced_ids)}

    return _transactional_mutation(targets, mutation, skip_write_if_noop="replaced")


def delete_experience_many(
    targets: List[Tuple[Path, str]],
    entry_id: str,
    *,
    expected_revision: Optional[int] = None,
    expected_updated_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    def mutation(entries, revision, path, title):
        _check_expected(entries, revision, entry_id, expected_revision, expected_updated_at, path)
        kept = [item for item in entries if item.entry_id != entry_id]
        affected = len(entries) - len(kept)
        return kept, {"deleted": affected > 0, "affected": affected}

    return _transactional_mutation(targets, mutation, skip_write_if_noop="deleted")


def _transactional_mutation(
    targets: List[Tuple[Path, str]],
    mutation: Callable[[List[ExperienceEntry], int, Path, str], Tuple[List[ExperienceEntry], Dict[str, Any]]],
    *,
    skip_write_if_noop: str = "",
) -> List[Dict[str, Any]]:
    """锁全部目标 → 先全部计算（含乐观检查）→ 再依次原子写；任一写失败回滚已写目标。

    - 计算阶段抛出（含 ConcurrencyConflictError）时尚未写任何文件，天然一致；
    - 写阶段失败时，用进入锁后快照的旧字节恢复已写目标（不存在则删除）。
    """
    paths = [path for path, _ in targets]
    with _locked_many(paths):
        plans: List[Dict[str, Any]] = []
        for path, title in targets:
            old_bytes = retry_windows_sharing(path.read_bytes) if path.exists() else None
            revision, entries = read_experience_file(path)
            new_entries, info = mutation(list(entries), revision, path, title)
            noop = bool(skip_write_if_noop) and not info.get(skip_write_if_noop)
            plans.append(
                {
                    "path": path,
                    "title": title,
                    "old_bytes": old_bytes,
                    "revision": revision,
                    "new_entries": new_entries,
                    "info": info,
                    "noop": noop,
                }
            )

        written: List[Dict[str, Any]] = []
        try:
            for plan in plans:
                if plan["noop"]:
                    continue
                new_revision = plan["revision"] + 1
                _atomic_write_text(
                    plan["path"],
                    render_experience_entries(plan["title"], plan["new_entries"], new_revision),
                )
                plan["info"]["revision"] = new_revision
                written.append(plan)
        except Exception:
            for plan in reversed(written):  # 回滚已写目标
                try:
                    if plan["old_bytes"] is None:
                        plan["path"].unlink(missing_ok=True)
                    else:
                        plan["path"].write_bytes(plan["old_bytes"])
                except OSError:
                    pass
            raise

        results = []
        for plan in plans:
            info = dict(plan["info"])
            info.setdefault("revision", plan["revision"])  # noop 时 revision 不变
            info["path"] = str(plan["path"])
            results.append(info)
        return results


# ==================== 单目标兼容 API ====================

def append_experience_entry(path: Path, title: str, content: str, *, source: str, max_tokens: int) -> AppendResult:
    result = append_experience_many([(path, title)], content, source=source, max_tokens=max_tokens)[0]
    return AppendResult(entry=result["entry"], revision=result["revision"], evicted_ids=result["evicted_ids"])


def replace_experience_entry(
    path: Path,
    title: str,
    entry_id: str,
    content: str,
    *,
    source: str,
    expected_revision: Optional[int] = None,
    expected_updated_at: Optional[str] = None,
) -> bool:
    result = replace_experience_many(
        [(path, title)], entry_id, content,
        source=source, expected_revision=expected_revision, expected_updated_at=expected_updated_at,
    )[0]
    return bool(result["replaced"])


def delete_experience_entry(
    path: Path,
    title: str,
    entry_id: str,
    *,
    expected_revision: Optional[int] = None,
    expected_updated_at: Optional[str] = None,
) -> bool:
    result = delete_experience_many(
        [(path, title)], entry_id,
        expected_revision=expected_revision, expected_updated_at=expected_updated_at,
    )[0]
    return bool(result["deleted"])


def list_experience_entries(path: Path) -> List[Dict[str, Any]]:
    return [
        {
            "entry_id": item.entry_id,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "source": item.source,
            "status": item.status,
            "content": item.content,
        }
        for item in parse_experience_entries(path)
    ]


def file_revision(path: Path) -> int:
    return read_experience_file(path)[0]
