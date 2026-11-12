"""Token normalization, time buckets and optional model pricing."""
from __future__ import annotations
from datetime import datetime, timedelta
from pathlib import Path
import math
from typing import Any, Dict, List, Optional, Tuple
METRIC_COLUMNS = ("calls", "calls_without_usage", "input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens", "reasoning_tokens", "total_tokens", "reported_cost")
GRANULARITIES = ("hour", "day", "week", "month", "total")
GROUP_BYS = ("", "model", "agent_system", "agent_name", "task", "user", "purpose", "model_alias")
MAX_BUCKETS = 5000
MAX_RANGE_HOURS = 24 * 366 * 5
_HOUR_FMT = "%Y-%m-%dT%H"


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0

def usage_numbers(usage: Any) -> Optional[Dict[str, Any]]:
    """把各家 usage 形状归一成本模块的计量列；没有任何 token 数 → None（计入 calls_without_usage）。"""
    if not isinstance(usage, dict):
        return None
    prompt = _as_int(usage.get("prompt_tokens") if usage.get("prompt_tokens") is not None else usage.get("input_tokens"))
    completion = _as_int(usage.get("completion_tokens") if usage.get("completion_tokens") is not None else usage.get("output_tokens"))
    if prompt <= 0 and completion <= 0:
        return None
    pdet = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    cdet = usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    cached = _as_int(pdet.get("cached_tokens")) or _as_int(usage.get("prompt_cache_hit_tokens")) or _as_int(usage.get("cache_read_input_tokens"))
    cache_write = _as_int(pdet.get("cache_write_tokens")) or _as_int(usage.get("cache_creation_input_tokens"))
    reasoning = _as_int(usage.get("reasoning_tokens")) or _as_int(cdet.get("reasoning_tokens"))
    total = _as_int(usage.get("total_tokens")) or (prompt + completion)
    cost = usage.get("cost")
    cached = min(cached, prompt)
    cache_write = min(cache_write, max(0, prompt - cached))
    return {
        "input_tokens": prompt, "cached_input_tokens": cached, "cache_write_tokens": cache_write,
        "output_tokens": completion, "reasoning_tokens": min(reasoning, completion) if completion else reasoning,
        "total_tokens": total, "reported_cost": float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(float(cost)) else 0.0,
    }

def parse_range(start: str = "", end: str = "", *, now: Optional[datetime] = None) -> Tuple[str, str]:
    """返回 [start_hour, end_hour)。

    start：按给出的精度取起点（日期=当天 0 点，月份=当月 1 日，带时间=所在小时）。
    end：包含给出的那一段（日期=整天，月份=整月，带时间=所在那一小时）。
    缺省：近 7 天（含今天）到当前小时。
    """
    now = now or datetime.now()

    def _parse(value: str, is_end: bool) -> Optional[datetime]:
        text = str(value or "").strip().replace(" ", "T")
        if not text:
            return None
        if len(text) > 19 and text[19] in "+-Z.":
            text = text[:19]
        for fmt, unit in (("%Y-%m-%dT%H:%M:%S", "hour"), ("%Y-%m-%dT%H:%M", "hour"), ("%Y-%m-%dT%H", "hour"),
                          ("%Y-%m-%d", "day"), ("%Y-%m", "month")):
            try:
                dt = datetime.strptime(text, fmt)
            except ValueError:
                continue
            if unit == "hour":
                dt = dt.replace(minute=0, second=0)
                return dt + timedelta(hours=1) if is_end else dt
            if unit == "day":
                return dt + timedelta(days=1) if is_end else dt
            nxt = (dt.replace(day=28) + timedelta(days=4)).replace(day=1)
            return nxt if is_end else dt
        raise ValueError(f"无法解析时间：{value!r}（支持 YYYY-MM-DD、YYYY-MM-DDTHH[:MM[:SS]]、YYYY-MM）")

    end_dt = _parse(end, True) or now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    start_dt = _parse(start, False) or (now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6))
    if start_dt >= end_dt:
        raise ValueError("开始时间必须早于结束时间")
    if (end_dt - start_dt) > timedelta(hours=MAX_RANGE_HOURS):
        raise ValueError("时间范围过长（上限 5 年）")
    return start_dt.strftime(_HOUR_FMT), end_dt.strftime(_HOUR_FMT)

def bucket_of(hour: str, granularity: str, range_start: str = "") -> Tuple[str, str]:
    dt = datetime.strptime(hour, _HOUR_FMT)
    if granularity == "hour":
        return hour, hour
    if granularity == "day":
        return dt.date().isoformat(), f"{dt.date().isoformat()}T00"
    if granularity == "week":
        monday = dt.date() - timedelta(days=dt.weekday())
        iso = monday.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}", f"{monday.isoformat()}T00"
    if granularity == "month":
        return dt.strftime("%Y-%m"), dt.strftime("%Y-%m-01T00")
    return "total", range_start or hour

def bucket_sequence(start_hour: str, end_hour: str, granularity: str) -> List[Tuple[str, str]]:
    if granularity not in GRANULARITIES:
        raise ValueError(f"granularity 只能是 {', '.join(GRANULARITIES)}")
    if granularity == "total":
        return [("total", start_hour)]
    start = datetime.strptime(start_hour, _HOUR_FMT)
    end = datetime.strptime(end_hour, _HOUR_FMT)
    hours = int((end - start).total_seconds() // 3600)
    if granularity == "hour" and hours > MAX_BUCKETS:
        raise ValueError(f"按小时最多 {MAX_BUCKETS} 个时段，请缩短范围或改用天/周/月")
    out: List[Tuple[str, str]] = []
    seen = set()
    cur = start
    step = timedelta(hours=1) if granularity == "hour" else timedelta(days=1)
    if granularity != "hour":
        cur = start.replace(hour=0)
    while cur < end:
        key, bstart = bucket_of(cur.strftime(_HOUR_FMT), granularity, start_hour)
        if key not in seen:
            seen.add(key)
            out.append((key, bstart))
        cur += step
    return out

def load_pricing(user_root: str | Path) -> Dict[str, Any]:
    """`config/usage_pricing.yaml`：每百万 token 单价。没有这个文件就只统计 token、不算费用。

        currency: CNY
        models:
          demo-model: {input: 1.0, cached_input: 0.2, output: 2.0}
    模型名先精确匹配，再按最后一个 `/` 之后的部分匹配（openai/demo-model → demo-model）。
    """
    path = Path(user_root) / "config" / "usage_pricing.yaml"
    if not path.exists():
        return _pricing_info(str(path))
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # 单价写错不能让统计接口 500
        info = _pricing_info(str(path))
        info["error"] = f"读取 usage_pricing.yaml 失败：{exc}"
        return info
    return pricing_from_text(text, source=str(path))

def _pricing_info(source: str) -> Dict[str, Any]:
    return {"configured": False, "currency": "", "unit": "per_million_tokens", "models": {}, "source": source, "error": ""}

def pricing_from_text(text: str, *, source: str = "") -> Dict[str, Any]:
    """解析可选的模型单价；error 非空表示文件无效。"""
    info = _pricing_info(source)
    try:
        import yaml
        data = yaml.safe_load(text) or {}
        models: Dict[str, Dict[str, Optional[float]]] = {}
        for name, raw in (data.get("models") or {}).items():
            if not isinstance(raw, dict):
                continue
            def _f(key):
                value = raw.get(key)
                return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
            if _f("input") is None and _f("output") is None:
                continue
            models[str(name)] = {"input": _f("input") or 0.0, "cached_input": _f("cached_input"),
                                 "cache_write": _f("cache_write"), "output": _f("output") or 0.0}
        info.update({"configured": bool(models), "currency": str(data.get("currency") or ""), "models": models})
    except Exception as exc:  # 单价写错不能让统计接口 500
        info["error"] = f"读取 usage_pricing.yaml 失败：{exc}"
    return info

def _price_for(model: str, pricing: Dict[str, Any]) -> Optional[Dict[str, Optional[float]]]:
    models = pricing.get("models") or {}
    if not models or not model:
        return None
    if model in models:
        return models[model]
    tail = model.rsplit("/", 1)[-1]
    if tail in models:
        return models[tail]
    for name, price in models.items():
        if name.rsplit("/", 1)[-1] == tail:
            return price
    return None

def cost_of(metrics: Dict[str, Any], price: Dict[str, Optional[float]]) -> float:
    cached = metrics["cached_input_tokens"]
    write = metrics["cache_write_tokens"]
    uncached = max(0, metrics["input_tokens"] - cached - write)
    in_price = price.get("input") or 0.0
    cached_price = price["cached_input"] if price.get("cached_input") is not None else in_price
    write_price = price["cache_write"] if price.get("cache_write") is not None else in_price
    return (uncached * in_price + cached * cached_price + write * write_price
            + metrics["output_tokens"] * (price.get("output") or 0.0)) / 1_000_000

def _zero(key: Optional[str] = None, label: str = "") -> Dict[str, Any]:
    """key=None：时段/总计累加器；分组累加器总带 key（空串也是合法分组：未记录模型、未设用户）。"""
    acc: Dict[str, Any] = {c: 0 for c in METRIC_COLUMNS}
    acc["reported_cost"] = 0.0
    acc["cost"] = None
    if key is not None:
        acc["key"] = key
        acc["label"] = label or ""
    return acc

def _add(acc: Dict[str, Any], metrics: Dict[str, Any], cost: Optional[float]) -> None:
    for c in METRIC_COLUMNS:
        acc[c] += metrics[c]
    if cost is not None:
        acc["cost"] = (acc["cost"] or 0.0) + cost

def _finalize(acc: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(acc)
    out["uncached_input_tokens"] = max(0, out["input_tokens"] - out["cached_input_tokens"] - out["cache_write_tokens"])
    out["cache_hit_rate"] = round(out["cached_input_tokens"] / out["input_tokens"], 4) if out["input_tokens"] else None
    out["reported_cost"] = round(out["reported_cost"], 6)
    if out["cost"] is not None:
        out["cost"] = round(out["cost"], 6)
    return out
