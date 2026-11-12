"""Local SQLite usage projection with per-call deduplication and replay."""
from __future__ import annotations

import hashlib
import contextlib
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils.usage_metrics import *
from utils.usage_metrics import _zero, _add, _finalize, _price_for

KEYS = ('task_key', 'hour', 'agent_name', 'model', 'model_alias', 'purpose')


_DIM_SQL = {
    "": "''",
    "model": "u.model",
    "agent_system": "COALESCE(t.agent_system, '')",
    "agent_name": "u.agent_name",
    "task": "u.task_key",
    "user": "COALESCE(t.user_id, '')",
    "purpose": "u.purpose",
    "model_alias": "u.model_alias",
}

_FILTER_SQL = {
    "agent_system": "COALESCE(t.agent_system, '') = ?",
    "agent_name": "u.agent_name = ?",
    "model": "u.model = ?",
    "user_id": "COALESCE(t.user_id, '') = ?",
    "task_key": "u.task_key = ?",
    "purpose": "u.purpose = ?",
    "model_alias": "u.model_alias = ?",
}


class UsageLedger:
    def __init__(self, user_root):
        self.root = Path(user_root).resolve()
        self.path = self.root / 'runtime' / 'usage.sqlite'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            columns = ','.join(c + (' REAL' if c == 'reported_cost' else ' INTEGER') + ' NOT NULL DEFAULT 0' for c in METRIC_COLUMNS)
            conn.executescript('''CREATE TABLE IF NOT EXISTS tasks(task_key TEXT PRIMARY KEY,task_id TEXT,name TEXT,agent_system TEXT,agent_name TEXT,user_id TEXT);
                CREATE TABLE IF NOT EXISTS seen(event_id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS sources(path TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY,v TEXT);
            ''' + 'CREATE TABLE IF NOT EXISTS usage_hourly(' + ','.join(k + ' TEXT NOT NULL' for k in KEYS) + ',' + columns + ', PRIMARY KEY(' + ','.join(KEYS) + '));' +
                'CREATE INDEX IF NOT EXISTS usage_hour ON usage_hourly(hour);')

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')
        return conn

    @contextlib.contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def task_key(task_id):
        return hashlib.sha256(str(Path(task_id).resolve()).encode()).hexdigest()[:24]

    def _record(self, conn, record):
        if not conn.execute('INSERT OR IGNORE INTO seen(event_id) VALUES(?)', (record['event_id'],)).rowcount:
            return
        task_id = str(record['task_id'])
        task_key = self.task_key(task_id)
        system = record.get('agent_system', '')
        agent = record.get('agent_name', '')
        conn.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?) ON CONFLICT(task_key) DO UPDATE SET agent_system=excluded.agent_system',
                     (task_key, task_id, Path(task_id).name, system, agent, ''))
        metrics = usage_numbers(record.get('usage'))
        values = {c: 0 for c in METRIC_COLUMNS}
        values.update(metrics or {})
        values['calls'] = 1
        values['calls_without_usage'] = int(metrics is None)
        hour = datetime.fromtimestamp(float(record['timestamp'])).strftime('%Y-%m-%dT%H')
        key_values = [task_key, hour, agent, record.get('model', ''), record.get('model_alias', record.get('model', '')), record.get('purpose', 'execution')]
        names = KEYS + METRIC_COLUMNS
        sql = 'INSERT INTO usage_hourly (' + ','.join(names) + ') VALUES (' + ','.join('?' for _ in names) + ') ON CONFLICT (' + ','.join(KEYS) + ') DO UPDATE SET ' + ','.join(c + '=usage_hourly.' + c + '+excluded.' + c for c in METRIC_COLUMNS)
        conn.execute(sql, key_values + [values[c] for c in METRIC_COLUMNS])
        conn.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('refreshed_at', datetime.now().isoformat(timespec='seconds')))

    def record(self, record):
        source = Path(record['task_id']) / '.sdk_runtime' / 'usage.ndjson'
        source.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(record, ensure_ascii=False) + '\n'
        with source.open('a', encoding='utf-8') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        with self._connection() as conn:
            conn.execute('INSERT OR IGNORE INTO sources(path) VALUES(?)', (str(source),))
            self._record(conn, record)

    def refresh(self, rebuild=False):
        conn = self._connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            paths = {row['path'] for row in conn.execute('SELECT path FROM sources')}
            paths.update(str(p) for p in (self.root / 'tasks').glob('**/.sdk_runtime/usage.ndjson'))
            if rebuild:
                conn.execute('DELETE FROM seen')
                conn.execute('DELETE FROM usage_hourly')
                conn.execute('UPDATE sources SET offset=0')
            for name in sorted(paths):
                path = Path(name)
                if not path.is_file():
                    continue
                row = conn.execute('SELECT offset FROM sources WHERE path=?', (name,)).fetchone()
                offset = int(row['offset']) if row else 0
                if offset > path.stat().st_size:
                    offset = 0
                with path.open('rb') as source:
                    source.seek(offset)
                    while True:
                        start = source.tell()
                        line = source.readline()
                        if not line or not line.endswith(b'\n'):
                            offset = start
                            break
                        self._record(conn, json.loads(line))
                        offset = source.tell()
                conn.execute('INSERT OR REPLACE INTO sources(path,offset) VALUES(?,?)', (name, offset))
            conn.commit()
        finally:
            conn.close()

    def info(self):
        with self._connection() as conn:
            row = conn.execute('SELECT v FROM meta WHERE k=?', ('refreshed_at',)).fetchone()
        return {'refreshed_at': row['v'] if row else '', 'backend': 'sqlite'}

    @staticmethod
    def _where(start_hour: str, end_hour: str, filters: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
        clauses = ["u.hour >= ?", "u.hour < ?"]
        params: List[Any] = [start_hour, end_hour]
        for name, value in (filters or {}).items():
            if name in _FILTER_SQL and value not in (None, ""):
                clauses.append(_FILTER_SQL[name])
                params.append(str(value))
        return " AND ".join(clauses), params

    def summary(self, *, start_hour: str, end_hour: str, granularity: str = "day", group_by: str = "",
                filters: Optional[Dict[str, Any]] = None, pricing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if group_by not in GROUP_BYS:
            raise ValueError(f"group_by 只能是 {', '.join(g for g in GROUP_BYS if g) } 或留空")
        order = bucket_sequence(start_hour, end_hour, granularity)
        pricing = pricing or {"configured": False, "models": {}}
        where, params = self._where(start_hour, end_hour, filters)
        sums = ", ".join(f"SUM(u.{c}) AS {c}" for c in METRIC_COLUMNS)
        sql = (f"SELECT u.hour AS hour, u.model AS model, {_DIM_SQL[group_by]} AS grp, MAX(t.name) AS label, {sums} "
               f"FROM usage_hourly u LEFT JOIN tasks t ON t.task_key = u.task_key WHERE {where} "
               f"GROUP BY u.hour, u.model, grp")
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
            dimensions = self._dimensions(conn, where, params)
        finally:
            conn.close()
        buckets = {key: {"key": key, "start": start, "totals": _zero(), "groups": {}} for key, start in order}
        totals = _zero()
        groups: Dict[str, Dict[str, Any]] = {}
        unpriced = set()
        for row in rows:
            key, _ = bucket_of(row["hour"], granularity, start_hour)
            bucket = buckets.get(key)
            if bucket is None:
                continue
            metrics = {c: (row[c] or 0) for c in METRIC_COLUMNS}
            price = _price_for(str(row["model"] or ""), pricing)
            if price is not None:
                cost: Optional[float] = cost_of(metrics, price)
            else:
                cost = metrics["reported_cost"] if metrics["reported_cost"] > 0 else None
                if metrics["input_tokens"] or metrics["output_tokens"]:
                    unpriced.add(str(row["model"] or ""))
            _add(bucket["totals"], metrics, cost)
            _add(totals, metrics, cost)
            if group_by:
                gkey = str(row["grp"] or "")
                label = str(row["label"] or "") if group_by == "task" else ""
                _add(bucket["groups"].setdefault(gkey, _zero(gkey, label)), metrics, cost)
                _add(groups.setdefault(gkey, _zero(gkey, label)), metrics, cost)
        by_total = lambda item: (-item["total_tokens"], item.get("key", ""))
        out_buckets = []
        for key, _start in order:
            b = buckets[key]
            out_buckets.append({"key": b["key"], "start": b["start"], "totals": _finalize(b["totals"]),
                                "groups": sorted((_finalize(g) for g in b["groups"].values()), key=by_total)})
        final_totals = _finalize(totals)
        return {
            "range": {"from": start_hour, "to": end_hour},
            "granularity": granularity,
            "group_by": group_by,
            "totals": final_totals,
            "groups": sorted((_finalize(g) for g in groups.values()), key=by_total),
            "buckets": out_buckets,
            "coverage": {"calls": final_totals["calls"], "calls_without_usage": final_totals["calls_without_usage"]},
            "pricing": {"configured": bool(pricing.get("configured")), "currency": pricing.get("currency", ""),
                        "unit": pricing.get("unit", "per_million_tokens"), "source": pricing.get("source", ""),
                        "error": pricing.get("error", ""),
                        "unpriced_models": sorted(unpriced) if pricing.get("configured") else []},
            "dimensions": dimensions,
        }

    @staticmethod
    def _dimensions(conn: Any, where: str, params: List[Any]) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for name, expr in (("model", "u.model"), ("agent_system", "COALESCE(t.agent_system, '')"),
                           ("agent_name", "u.agent_name"), ("purpose", "u.purpose"), ("user", "COALESCE(t.user_id, '')"),
                           ("model_alias", "u.model_alias")):
            rows = conn.execute(f"SELECT DISTINCT {expr} AS v FROM usage_hourly u LEFT JOIN tasks t ON t.task_key = u.task_key "
                                f"WHERE {where} ORDER BY v", params).fetchall()
            out[name] = [str(r["v"] or "") for r in rows]
        return out

    def top_tasks(self, *, start_hour: str, end_hour: str, limit: int = 20, filters: Optional[Dict[str, Any]] = None,
                  pricing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        pricing = pricing or {"configured": False, "models": {}}
        where, params = self._where(start_hour, end_hour, filters)
        sums = ", ".join(f"SUM(u.{c}) AS {c}" for c in METRIC_COLUMNS)
        sql = (f"SELECT u.task_key AS task_key, u.model AS model, MAX(u.hour) AS last_hour, "
               f"MAX(t.task_id) AS task_id, MAX(t.name) AS name, MAX(t.agent_system) AS agent_system, "
               f"MAX(t.agent_name) AS agent_name, MAX(t.user_id) AS user_id, {sums} "
               f"FROM usage_hourly u LEFT JOIN tasks t ON t.task_key = u.task_key WHERE {where} GROUP BY u.task_key, u.model")
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        tasks: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            metrics = {c: (row[c] or 0) for c in METRIC_COLUMNS}
            price = _price_for(str(row["model"] or ""), pricing)
            cost = cost_of(metrics, price) if price is not None else (metrics["reported_cost"] if metrics["reported_cost"] > 0 else None)
            item = tasks.setdefault(row["task_key"], {**_zero(row["task_key"], str(row["name"] or "")),
                                                      "task_id": str(row["task_id"] or ""), "agent_system": str(row["agent_system"] or ""),
                                                      "agent_name": str(row["agent_name"] or ""), "user_id": str(row["user_id"] or ""),
                                                      "last_hour": "", "models": []})
            _add(item, metrics, cost)
            item["last_hour"] = max(item["last_hour"], str(row["last_hour"] or ""))
            if row["model"]:
                item["models"].append(str(row["model"]))
        items = sorted((_finalize(t) for t in tasks.values()), key=lambda t: (-t["total_tokens"], t["key"]))
        return {"range": {"from": start_hour, "to": end_hour}, "count": len(items), "items": items[:max(1, int(limit or 20))]}
