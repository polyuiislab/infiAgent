#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
任务历史索引：
- 持久化归档后的 task history 到 SQLite
- 基于 instruction bundle 建立可检索 chunk
- 提供 SQL / FTS / 可选 semantic 检索入口
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from utils.user_paths import get_user_task_history_dir


def get_task_history_db_path() -> Path:
    root = get_user_task_history_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root / "task_history.db"


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(get_task_history_db_path()))
    try:
        conn.row_factory = sqlite3.Row
        _ensure_schema(conn)
        with conn:
            yield conn
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS indexed_entries (
            entry_key TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            entry_index INTEGER NOT NULL,
            start_time TEXT,
            completion_time TEXT,
            agent_system TEXT,
            agent_name TEXT,
            indexed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS history_chunks (
            chunk_id TEXT PRIMARY KEY,
            entry_key TEXT NOT NULL,
            task_id TEXT NOT NULL,
            entry_index INTEGER NOT NULL,
            chunk_type TEXT NOT NULL,
            instruction_index INTEGER,
            instruction_text TEXT,
            final_output TEXT,
            latest_thinking TEXT,
            text TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS history_chunks_fts USING fts5(
            chunk_id UNINDEXED,
            text
        );

        CREATE VIEW IF NOT EXISTS task_history AS
        SELECT
            chunk_id,
            task_id,
            entry_index,
            chunk_type,
            instruction_index,
            instruction_text,
            final_output,
            latest_thinking,
            text,
            json_extract(metadata_json, '$.start_time') AS start_time,
            json_extract(metadata_json, '$.completion_time') AS completion_time,
            json_extract(metadata_json, '$.agent_name') AS agent_name,
            json_extract(metadata_json, '$.agent_system') AS agent_system,
            metadata_json
        FROM history_chunks;
        """
    )
    conn.commit()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _entry_key(task_id: str, entry: Dict[str, Any]) -> str:
    blob = _json_dumps({
        "task_id": task_id,
        "entry": entry,
    })
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def _chunk_id(entry_key: str, chunk_type: str, instruction_index: int) -> str:
    return f"{entry_key}:{chunk_type}:{instruction_index}"


def _extract_top_level_agents(entry: Dict[str, Any]) -> List[Dict[str, Any]]:
    hierarchy = entry.get("hierarchy", {}) if isinstance(entry, dict) else {}
    agents_status = entry.get("agents_status", {}) if isinstance(entry, dict) else {}
    if not isinstance(hierarchy, dict) or not isinstance(agents_status, dict):
        return []

    top_level_ids = [
        agent_id
        for agent_id, node in hierarchy.items()
        if isinstance(node, dict) and node.get("parent") is None
    ]
    if not top_level_ids:
        top_level_ids = list(agents_status.keys())

    agents: List[Dict[str, Any]] = []
    for agent_id in top_level_ids:
        info = agents_status.get(agent_id)
        if not isinstance(info, dict):
            continue
        if info.get("agent_name") == "judge_agent":
            continue
        agents.append(info)
    return agents


def _extract_entry_outputs(entry: Dict[str, Any]) -> Dict[str, str]:
    agents = _extract_top_level_agents(entry)
    final_output_parts: List[str] = []
    thinking_parts: List[str] = []
    agent_names: List[str] = []
    for info in agents:
        agent_name = str(info.get("agent_name") or "").strip()
        if agent_name:
            agent_names.append(agent_name)
        final_output = str(info.get("final_output") or "").strip()
        latest_thinking = str(info.get("latest_thinking") or "").strip()
        if final_output:
            title = f"[{agent_name}] " if agent_name else ""
            final_output_parts.append(f"{title}{final_output}")
        if latest_thinking:
            title = f"[{agent_name}] " if agent_name else ""
            thinking_parts.append(f"{title}{latest_thinking}")

    return {
        "agent_names": ", ".join(agent_names),
        "final_output": "\n\n".join(final_output_parts).strip(),
        "latest_thinking": "\n\n".join(thinking_parts).strip(),
    }


def _build_instruction_bundle_text(
    *,
    instruction_text: str,
    final_output: str,
    latest_thinking: str,
    start_time: str,
    completion_time: str,
    agent_names: str,
) -> str:
    parts = []
    if instruction_text:
        parts.append(f"Instruction:\n{instruction_text}")
    if final_output:
        parts.append(f"Final Output:\n{final_output}")
    if latest_thinking:
        parts.append(f"Latest Thinking:\n{latest_thinking}")
    meta_lines = []
    if start_time or completion_time:
        meta_lines.append(f"Time: {start_time or ''} -> {completion_time or ''}".strip())
    if agent_names:
        meta_lines.append(f"Top-level Agents: {agent_names}")
    if meta_lines:
        parts.append("\n".join(meta_lines))
    return "\n\n".join(part for part in parts if part).strip()


def _build_summary_text(
    instructions: Sequence[str],
    final_output: str,
    latest_thinking: str,
    start_time: str,
    completion_time: str,
    agent_names: str,
) -> str:
    parts = []
    if instructions:
        parts.append("Instructions:\n" + "\n".join(f"- {item}" for item in instructions if item))
    if final_output:
        parts.append(f"Final Output Summary:\n{final_output}")
    if latest_thinking:
        parts.append(f"Thinking Summary:\n{latest_thinking}")
    meta_lines = []
    if start_time or completion_time:
        meta_lines.append(f"Time: {start_time or ''} -> {completion_time or ''}".strip())
    if agent_names:
        meta_lines.append(f"Top-level Agents: {agent_names}")
    if meta_lines:
        parts.append("\n".join(meta_lines))
    return "\n\n".join(part for part in parts if part).strip()


def _build_chunks(task_id: str, entry: Dict[str, Any], entry_index: int, runtime_meta: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    runtime_meta = runtime_meta or {}
    entry_key = _entry_key(task_id, entry)
    start_time = str(entry.get("start_time") or "")
    completion_time = str(entry.get("completion_time") or "")
    outputs = _extract_entry_outputs(entry)
    instructions_raw = entry.get("instructions", [])
    instructions: List[str] = []
    if isinstance(instructions_raw, list):
        for item in instructions_raw:
            if isinstance(item, dict):
                text = str(item.get("instruction") or "").strip()
            else:
                text = str(item or "").strip()
            if text:
                instructions.append(text)

    metadata = {
        "task_id": task_id,
        "entry_index": entry_index,
        "start_time": start_time,
        "completion_time": completion_time,
        "agent_system": str(runtime_meta.get("agent_system") or ""),
        "agent_name": str(runtime_meta.get("agent_name") or outputs["agent_names"] or ""),
        "top_level_agents": outputs["agent_names"],
    }

    chunks: List[Dict[str, Any]] = []
    if instructions:
        for instruction_index, instruction_text in enumerate(instructions):
            text = _build_instruction_bundle_text(
                instruction_text=instruction_text,
                final_output=outputs["final_output"],
                latest_thinking=outputs["latest_thinking"],
                start_time=start_time,
                completion_time=completion_time,
                agent_names=outputs["agent_names"],
            )
            chunks.append({
                "chunk_id": _chunk_id(entry_key, "instruction_bundle", instruction_index),
                "entry_key": entry_key,
                "task_id": task_id,
                "entry_index": entry_index,
                "chunk_type": "instruction_bundle",
                "instruction_index": instruction_index,
                "instruction_text": instruction_text,
                "final_output": outputs["final_output"],
                "latest_thinking": outputs["latest_thinking"],
                "text": text,
                "metadata_json": _json_dumps(metadata),
                "created_at": datetime.now().isoformat(),
            })

    summary_text = _build_summary_text(
        instructions=instructions,
        final_output=outputs["final_output"],
        latest_thinking=outputs["latest_thinking"],
        start_time=start_time,
        completion_time=completion_time,
        agent_names=outputs["agent_names"],
    )
    if summary_text:
        chunks.append({
            "chunk_id": _chunk_id(entry_key, "task_summary", -1),
            "entry_key": entry_key,
            "task_id": task_id,
            "entry_index": entry_index,
            "chunk_type": "task_summary",
            "instruction_index": None,
            "instruction_text": "",
            "final_output": outputs["final_output"],
            "latest_thinking": outputs["latest_thinking"],
            "text": summary_text,
            "metadata_json": _json_dumps(metadata),
            "created_at": datetime.now().isoformat(),
        })

    return chunks


def sync_task_history_from_context(task_id: str, context: Optional[Dict[str, Any]] = None) -> int:
    if context is None:
        from core.hierarchy_manager import get_hierarchy_manager
        context = get_hierarchy_manager(task_id).get_context()

    if not isinstance(context, dict):
        return 0

    history = context.get("history", [])
    runtime_meta = context.get("runtime", {}) if isinstance(context.get("runtime"), dict) else {}
    if not isinstance(history, list) or not history:
        return 0

    indexed_count = 0
    with _connect() as conn:
        for entry_index, entry in enumerate(history):
            if not isinstance(entry, dict):
                continue
            entry_key = _entry_key(task_id, entry)
            exists = conn.execute(
                "SELECT 1 FROM indexed_entries WHERE entry_key = ?",
                (entry_key,),
            ).fetchone()
            if exists:
                continue

            chunks = _build_chunks(task_id, entry, entry_index, runtime_meta=runtime_meta)
            if not chunks:
                continue

            conn.execute(
                """
                INSERT OR REPLACE INTO indexed_entries
                (entry_key, task_id, entry_index, start_time, completion_time, agent_system, agent_name, indexed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_key,
                    task_id,
                    entry_index,
                    str(entry.get("start_time") or ""),
                    str(entry.get("completion_time") or ""),
                    str(runtime_meta.get("agent_system") or ""),
                    str(runtime_meta.get("agent_name") or ""),
                    datetime.now().isoformat(),
                ),
            )
            for chunk in chunks:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO history_chunks
                    (chunk_id, entry_key, task_id, entry_index, chunk_type, instruction_index, instruction_text,
                     final_output, latest_thinking, text, metadata_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk["chunk_id"],
                        chunk["entry_key"],
                        chunk["task_id"],
                        chunk["entry_index"],
                        chunk["chunk_type"],
                        chunk["instruction_index"],
                        chunk["instruction_text"],
                        chunk["final_output"],
                        chunk["latest_thinking"],
                        chunk["text"],
                        chunk["metadata_json"],
                        chunk["created_at"],
                    ),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO history_chunks_fts (rowid, chunk_id, text) VALUES ((SELECT rowid FROM history_chunks WHERE chunk_id = ?), ?, ?)",
                    (chunk["chunk_id"], chunk["chunk_id"], chunk["text"]),
                )
                indexed_count += 1
        conn.commit()

    return indexed_count


_FTS_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _keyword_tokens(keyword: str) -> List[str]:
    return [token for token in _FTS_TOKEN_RE.findall(str(keyword or "")) if token]


def _normalize_fts_query(keyword: str) -> str:
    tokens = _keyword_tokens(keyword)
    if not tokens:
        return ""
    quoted = []
    for token in tokens[:20]:
        escaped = token.replace('"', '""')
        quoted.append(f'"{escaped}"')
    return " OR ".join(quoted)


def _escape_like(value: str) -> str:
    return (
        str(value or "")
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _like_entry_keys(conn: sqlite3.Connection, task_id: str, values: Sequence[str]) -> set[str]:
    search_values = [str(value or "").strip() for value in values if str(value or "").strip()]
    if not search_values:
        return set()

    clauses: List[str] = []
    params: List[Any] = [task_id]
    for value in search_values[:20]:
        pattern = f"%{_escape_like(value)}%"
        clauses.append(
            "("
            "c.text LIKE ? ESCAPE '\\' OR "
            "c.instruction_text LIKE ? ESCAPE '\\' OR "
            "c.final_output LIKE ? ESCAPE '\\' OR "
            "c.latest_thinking LIKE ? ESCAPE '\\'"
            ")"
        )
        params.extend([pattern, pattern, pattern, pattern])

    rows = conn.execute(
        f"""
        SELECT DISTINCT c.entry_key
        FROM history_chunks c
        WHERE c.task_id = ? AND ({' OR '.join(clauses)})
        """,
        params,
    ).fetchall()
    return {str(row["entry_key"]) for row in rows}


def _search_entry_keys_by_keyword(conn: sqlite3.Connection, task_id: str, keyword: str) -> tuple[set[str], str, str]:
    keyword = str(keyword or "").strip()
    if not keyword:
        return set(), "", ""

    fts_error = ""
    fts_query = _normalize_fts_query(keyword)
    if fts_query:
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT c.entry_key
                FROM history_chunks_fts f
                JOIN history_chunks c ON c.rowid = f.rowid
                WHERE c.task_id = ? AND history_chunks_fts MATCH ?
                """,
                (task_id, fts_query),
            ).fetchall()
            entry_keys = {str(row["entry_key"]) for row in rows}
            if entry_keys:
                return entry_keys, "fts", ""
        except sqlite3.Error as exc:
            fts_error = str(exc)

    entry_keys = _like_entry_keys(conn, task_id, [keyword])
    if entry_keys:
        return entry_keys, "like_fallback" if fts_query else "like", fts_error

    tokens = _keyword_tokens(keyword)
    if len(tokens) > 1:
        entry_keys = _like_entry_keys(conn, task_id, tokens)
        if entry_keys:
            return entry_keys, "token_like_fallback", fts_error

    return set(), "no_match", fts_error


def _search_chunk_rows_by_query(
    conn: sqlite3.Connection,
    *,
    query: str,
    task_id: Optional[str],
    limit: int,
) -> List[sqlite3.Row]:
    fts_query = _normalize_fts_query(query)
    if fts_query:
        base_sql = """
            SELECT c.*
            FROM history_chunks_fts f
            JOIN history_chunks c ON c.rowid = f.rowid
            WHERE history_chunks_fts MATCH ?
        """
        params: List[Any] = [fts_query]
        if task_id:
            base_sql += " AND c.task_id = ?"
            params.append(str(task_id))
        base_sql += " ORDER BY rank LIMIT ?"
        params.append(limit)
        try:
            rows = conn.execute(base_sql, params).fetchall()
            if rows:
                return rows
        except sqlite3.Error:
            pass

    pattern = f"%{_escape_like(query)}%"
    base_sql = """
        SELECT c.*
        FROM history_chunks c
        WHERE c.text LIKE ? ESCAPE '\\'
    """
    params = [pattern]
    if task_id:
        base_sql += " AND c.task_id = ?"
        params.append(str(task_id))
    base_sql += " ORDER BY c.entry_index ASC LIMIT ?"
    params.append(limit)
    return conn.execute(base_sql, params).fetchall()


def search_task_history_sql(
    *,
    sql: Optional[str] = None,
    query: Optional[str] = None,
    task_id: Optional[str] = None,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit or 10), 50))
    with _connect() as conn:
        if sql:
            normalized = str(sql or "").strip()
            if not normalized.lower().startswith("select"):
                raise ValueError("只允许执行只读 SELECT 语句")
            rows = conn.execute(normalized).fetchmany(limit)
        else:
            search_query = str(query or "").strip()
            if not search_query:
                raise ValueError("sql 模式至少需要 query 或 sql")
            rows = _search_chunk_rows_by_query(conn, query=search_query, task_id=task_id, limit=limit)

    return [_row_to_result(row) for row in rows]


def _simple_cosine_similarity(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (norm_a * norm_b)


def search_task_history_semantic(
    *,
    query: str,
    task_id: Optional[str] = None,
    limit: int = 10,
) -> Dict[str, Any]:
    limit = max(1, min(int(limit or 10), 50))
    query = str(query or "").strip()
    if not query:
        raise ValueError("semantic 模式需要 query")

    try:
        from sentence_transformers import SentenceTransformer
    except Exception as exc:
        return {
            "mode": "semantic",
            "fallback": "unavailable",
            "error": f"sentence_transformers 不可用: {exc}",
            "results": [],
        }

    model_name = "all-MiniLM-L6-v2"
    try:
        model = SentenceTransformer(model_name)
    except Exception as exc:
        return {
            "mode": "semantic",
            "fallback": "unavailable",
            "error": f"语义检索模型加载失败: {exc}",
            "results": [],
        }

    with _connect() as conn:
        sql = "SELECT * FROM history_chunks"
        params: List[Any] = []
        if task_id:
            sql += " WHERE task_id = ?"
            params.append(str(task_id))
        sql += " ORDER BY entry_index DESC LIMIT 300"
        rows = conn.execute(sql, params).fetchall()

    if not rows:
        return {"mode": "semantic", "fallback": None, "error": "", "results": []}

    texts = [str(row["text"] or "") for row in rows]
    embeddings = model.encode([query] + texts, normalize_embeddings=True)
    query_embedding = embeddings[0]
    scored = []
    for row, emb in zip(rows, embeddings[1:]):
        score = _simple_cosine_similarity(query_embedding, emb)
        scored.append((score, row))
    scored.sort(key=lambda item: item[0], reverse=True)

    results = []
    for score, row in scored[:limit]:
        item = _row_to_result(row)
        item["score"] = float(score)
        results.append(item)
    return {"mode": "semantic", "fallback": None, "error": "", "results": results}


def _row_to_result(row: sqlite3.Row | Dict[str, Any]) -> Dict[str, Any]:
    data = dict(row)
    metadata_raw = data.get("metadata_json")
    try:
        metadata = json.loads(metadata_raw) if metadata_raw else {}
    except Exception:
        metadata = {}
    return {
        "chunk_id": data.get("chunk_id", ""),
        "task_id": data.get("task_id", ""),
        "entry_index": data.get("entry_index"),
        "chunk_type": data.get("chunk_type", ""),
        "instruction_index": data.get("instruction_index"),
        "instruction_text": data.get("instruction_text", ""),
        "final_output": data.get("final_output", ""),
        "latest_thinking": data.get("latest_thinking", ""),
        "text": data.get("text", ""),
        "metadata": metadata,
    }


def _load_entry_chunks(conn: sqlite3.Connection, entry_key: str) -> List[sqlite3.Row]:
    return conn.execute(
        """
        SELECT *
        FROM history_chunks
        WHERE entry_key = ?
        ORDER BY
          CASE WHEN chunk_type = 'instruction_bundle' THEN 0 ELSE 1 END,
          instruction_index ASC
        """,
        (entry_key,),
    ).fetchall()


def _entry_to_task_result(entry_row: sqlite3.Row, chunk_rows: Sequence[sqlite3.Row]) -> Dict[str, Any]:
    instructions: List[str] = []
    final_output = ""
    latest_thinking = ""
    summary_text = ""
    matched_texts: List[str] = []

    for chunk in chunk_rows:
        instruction_text = str(chunk["instruction_text"] or "").strip()
        if instruction_text:
            instructions.append(instruction_text)
        if not final_output:
            final_output = str(chunk["final_output"] or "").strip()
        if not latest_thinking:
            latest_thinking = str(chunk["latest_thinking"] or "").strip()
        if chunk["chunk_type"] == "task_summary" and not summary_text:
            summary_text = str(chunk["text"] or "").strip()
        text = str(chunk["text"] or "").strip()
        if text:
            matched_texts.append(text[:800])

    return {
        "task_id": str(entry_row["task_id"] or ""),
        "entry_index": int(entry_row["entry_index"] or 0),
        "round": int(entry_row["entry_index"] or 0) + 1,
        "start_time": str(entry_row["start_time"] or ""),
        "completion_time": str(entry_row["completion_time"] or ""),
        "agent_system": str(entry_row["agent_system"] or ""),
        "agent_name": str(entry_row["agent_name"] or ""),
        "instructions": instructions,
        "final_output": final_output,
        "latest_thinking": latest_thinking,
        "summary_text": summary_text,
        "matched_texts": matched_texts[:3],
    }


def search_task_history_records(
    *,
    task_id: str,
    keyword: str = "",
    relevance_query_text: str = "",
    start_time_from: str = "",
    start_time_to: str = "",
    start_round: int = 0,
    end_round: int = 0,
    enable_vector_search: bool = False,
) -> Dict[str, Any]:
    task_id = str(task_id or "").strip()
    keyword = str(keyword or "").strip()
    relevance_query_text = str(relevance_query_text or "").strip()
    start_time_from = str(start_time_from or "").strip()
    start_time_to = str(start_time_to or "").strip()
    start_round = max(0, int(start_round or 0))
    end_round = max(0, int(end_round or 0))

    with _connect() as conn:
        entry_keys_filter: Optional[set[str]] = None
        keyword_search_mode = ""
        keyword_search_error = ""
        if keyword:
            entry_keys_filter, keyword_search_mode, keyword_search_error = _search_entry_keys_by_keyword(conn, task_id, keyword)

        semantic_scores: Dict[str, float] = {}
        semantic_error = ""
        if enable_vector_search and relevance_query_text:
            semantic_payload = search_task_history_semantic(
                query=relevance_query_text,
                task_id=task_id,
                limit=50,
            )
            semantic_error = str(semantic_payload.get("error") or "")
            for item in semantic_payload.get("results", []):
                chunk_id = str(item.get("chunk_id") or "")
                if not chunk_id:
                    continue
                entry_key = chunk_id.split(":", 1)[0]
                semantic_scores[entry_key] = max(float(item.get("score") or 0.0), semantic_scores.get(entry_key, 0.0))
            if entry_keys_filter is None and semantic_scores:
                entry_keys_filter = set(semantic_scores.keys())
            elif entry_keys_filter is not None and semantic_scores:
                entry_keys_filter &= set(semantic_scores.keys())
            elif semantic_error and entry_keys_filter is None and not keyword:
                entry_keys_filter = set()

        sql = """
            SELECT *
            FROM indexed_entries
            WHERE task_id = ?
        """
        params: List[Any] = [task_id]
        if start_time_from:
            sql += " AND (start_time IS NULL OR start_time >= ?)"
            params.append(start_time_from)
        if start_time_to:
            sql += " AND (start_time IS NULL OR start_time <= ?)"
            params.append(start_time_to)
        if start_round > 0:
            sql += " AND entry_index >= ?"
            params.append(start_round - 1)
        if end_round > 0:
            sql += " AND entry_index <= ?"
            params.append(end_round - 1)
        sql += " ORDER BY start_time ASC, entry_index ASC"
        rows = conn.execute(sql, params).fetchall()

        results: List[Dict[str, Any]] = []
        for row in rows:
            entry_key = str(row["entry_key"])
            if entry_keys_filter is not None and entry_key not in entry_keys_filter:
                continue
            chunk_rows = _load_entry_chunks(conn, entry_key)
            item = _entry_to_task_result(row, chunk_rows)
            if entry_key in semantic_scores:
                item["score"] = semantic_scores[entry_key]
            results.append(item)

    return {
        "task_id": task_id,
        "keyword": keyword,
        "relevance_query_text": relevance_query_text,
        "start_time_from": start_time_from,
        "start_time_to": start_time_to,
        "start_round": start_round,
        "end_round": end_round,
        "enable_vector_search": bool(enable_vector_search),
        "semantic_error": semantic_error,
        "keyword_search_mode": keyword_search_mode,
        "keyword_search_error": keyword_search_error,
        "results": results,
    }
