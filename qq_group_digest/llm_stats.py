"""Durable, best-effort accounting for this plugin's LLM attempts."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .llm_usage import TOKEN_FIELDS, calculate_cost, extract_usage, field, usage_snapshot

try:
    from astrbot.api import logger
except ImportError:
    logger = logging.getLogger(__name__)
STATUS_LABELS = {
    "pending": "进行中",
    "returned": "已返回待校验",
    "success": "有效输出",
    "invalid_json": "无效输出",
    "provider_error": "接口异常",
    "cancelled": "已取消",
    "interrupted": "进程中断",
}
SUMMARY_COLUMNS = (
    "id",
    "operation_id",
    "run_id",
    "group_id",
    "source",
    "attempt",
    "started_at",
    "provider_id",
    "model",
    "status",
    "duration_ms",
    *TOKEN_FIELDS,
    "estimated_cost",
    "cost_lower",
    "cost_upper",
    "currency",
    "usage_source",
    "error_detail",
)


class UsageStore:
    def __init__(self, database: str | Path, config: dict) -> None:
        self.database = str(database)
        self.config = config
        self._lock = threading.RLock()
        self._closed = False
        self._last_warning = float("-inf")
        self._next_prune = 0.0
        if self.database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.database, check_same_thread=False, timeout=0.2)
        self._db.row_factory = sqlite3.Row
        try:
            if self.database != ":memory:":
                self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS llm_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL, run_id TEXT,
                    platform_id TEXT NOT NULL, group_id TEXT NOT NULL,
                    source TEXT NOT NULL, attempt INTEGER NOT NULL,
                    started_at REAL NOT NULL, finished_at REAL, duration_ms REAL,
                    provider_id TEXT NOT NULL, model TEXT NOT NULL DEFAULT '',
                    model_source TEXT NOT NULL DEFAULT 'unknown', response_id TEXT,
                    status TEXT NOT NULL DEFAULT 'pending', finish_reason TEXT,
                    error_type TEXT, input_tokens INTEGER, cached_tokens INTEGER,
                    uncached_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
                    usage_source TEXT, usage_notes TEXT,
                    estimated_cost TEXT, currency TEXT, price_snapshot TEXT,
                    system_prompt_hash TEXT NOT NULL, prompt_hash TEXT NOT NULL,
                    system_prompt_text TEXT, prompt_text TEXT, response_text TEXT,
                    content_truncated INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(operation_id, attempt)
                );
                CREATE INDEX IF NOT EXISTS idx_llm_calls_group_time ON llm_calls(group_id, started_at);
                CREATE INDEX IF NOT EXISTS idx_llm_calls_application ON llm_calls(run_id, group_id);
            """)
            with self._db:
                columns = {row["name"] for row in self._db.execute("PRAGMA table_info(llm_calls)")}
                for name in (
                    "raw_usage_json",
                    "cost_lower",
                    "cost_upper",
                    "pricing_config",
                    "error_detail",
                    "request_options",
                ):
                    if name not in columns:
                        self._db.execute(f"ALTER TABLE llm_calls ADD COLUMN {name} TEXT")
                self._db.execute("PRAGMA user_version=2")
                # A process exit cannot tell us whether the provider billed a
                # pending request. Preserve NULL counters and never retry here.
                self._db.execute("UPDATE llm_calls SET status='interrupted' WHERE status='pending'")
            self._prune()
        except Exception:
            self._db.close()
            raise

    def _safe(self, operation):
        with self._lock:
            if self._closed:
                return None
            try:
                return operation()
            except Exception as exc:
                if time.monotonic() - self._last_warning >= 60:
                    logger.warning("LLM 统计写入失败，摘要继续：%s", type(exc).__name__)
                    self._last_warning = time.monotonic()
                return None

    def _prune(self) -> None:
        if time.monotonic() < self._next_prune:
            return
        days = self.config["retention_days"]
        if days:
            with self._db:
                self._db.execute(
                    "DELETE FROM llm_calls WHERE started_at < ? AND status != 'pending'",
                    (time.time() - days * 86400,),
                )
        self._next_prune = time.monotonic() + 3600

    def maintenance(self) -> None:
        self._safe(self._prune)

    @contextmanager
    def _reader(self):
        # Large reports use an independent WAL reader so they do not hold
        # the write lock while live requests are saving their counters.
        if self.database == ":memory:":
            with self._lock:
                yield self._db
        else:
            uri = Path(self.database).resolve().as_uri() + "?mode=ro"
            reader = sqlite3.connect(uri, uri=True, timeout=0.2)
            reader.row_factory = sqlite3.Row
            try:
                yield reader
            finally:
                reader.close()

    def begin(self, *, system_prompt: str, prompt: str, **metadata) -> int | None:
        if not self.config["enabled"]:
            return None

        def write():
            self._prune()
            values = dict(
                metadata,
                started_at=time.time(),
                pricing_config=json.dumps(self.config["prices"], ensure_ascii=False),
                system_prompt_hash=hashlib.sha256(system_prompt.encode()).hexdigest(),
                prompt_hash=hashlib.sha256(prompt.encode()).hexdigest(),
            )
            if self.config["store_content"]:
                maximum = self.config["content_max_chars"]
                values.update(
                    system_prompt_text=system_prompt[:maximum],
                    prompt_text=prompt[:maximum],
                    content_truncated=int(len(system_prompt) > maximum or len(prompt) > maximum),
                )
            columns = ",".join(values)
            with self._db:
                cursor = self._db.execute(
                    f"INSERT INTO llm_calls ({columns}) VALUES ({','.join('?' for _ in values)})",
                    tuple(values.values()),
                )
            return cursor.lastrowid

        return self._safe(write)

    def finish(
        self,
        call_id: int | None,
        *,
        status: str,
        duration_ms: float,
        response: Any = None,
        error_type: str = "",
    ) -> None:
        if call_id is None:
            return

        def write():
            record = self._db.execute("SELECT * FROM llm_calls WHERE id=?", (call_id,)).fetchone()
            if record is None or record["status"] != "pending":
                return
            usage = extract_usage(response)
            raw = field(response, "raw_completion")
            actual_model = field(raw, "model")
            model = str(actual_model)[:256] if actual_model else record["model"]
            choices = field(raw, "choices", []) or []
            finish_reason = (
                field(choices[0], "finish_reason") if isinstance(choices, (list, tuple)) and choices else None
            )
            prices = (
                json.loads(record["pricing_config"]) if record["pricing_config"] else self.config["prices"]
            )
            cost = calculate_cost(
                usage,
                prices,
                record["provider_id"],
                model,
                started_at=record["started_at"],
                configured_model=record["model"],
            )
            values = dict(
                usage,
                **cost,
                status=status,
                duration_ms=max(0.0, duration_ms),
                finished_at=time.time(),
                error_type=error_type[:128],
                model=model,
                model_source="response" if actual_model else record["model_source"],
                response_id=str(field(response, "id") or field(raw, "id") or "")[:256],
                finish_reason=str(finish_reason)[:128] if finish_reason is not None else None,
            )
            values["price_snapshot"] = (
                json.dumps(cost["price_snapshot"], ensure_ascii=False) if cost["price_snapshot"] else None
            )
            if self.config.get("store_usage", True):
                snapshot = usage_snapshot(response)
                values["raw_usage_json"] = (
                    json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None
                )
            if (
                self.config["store_content"] or self.config.get("store_response", False)
            ) and response is not None:
                text = str(field(response, "completion_text", "") or "")
                maximum = self.config["content_max_chars"]
                values.update(
                    response_text=text[:maximum],
                    content_truncated=int(record["content_truncated"] or len(text) > maximum),
                )
            with self._db:
                self._db.execute(
                    f"UPDATE llm_calls SET {','.join(k + '=?' for k in values)} WHERE id=?",
                    (*values.values(), call_id),
                )

        self._safe(write)

    def parsed(self, call_id: int | None, status: str, detail: str = "") -> None:
        if call_id is None or status not in {"success", "invalid_json"}:
            return

        def write():
            with self._db:
                self._db.execute(
                    "UPDATE llm_calls SET status=?,error_detail=? WHERE id=? AND status='returned'",
                    (status, detail[:1000], call_id),
                )

        self._safe(write)

    def query(
        self,
        *,
        group_ids: list[str],
        since: float = 0,
        until: float | None = None,
        model: str | None = None,
        run_id: str | None = None,
        content: bool = False,
    ) -> list[dict]:
        if not group_ids:
            return []
        clauses = [f"group_id IN ({','.join('?' for _ in group_ids)})", "started_at>=?", "started_at<=?"]
        params: list = [*group_ids, since, time.time() if until is None else until]
        if model is not None:
            clauses.append("model=?")
            params.append(model)
        if run_id is not None:
            clauses.append("run_id=?")
            params.append(run_id)
        columns = "*" if content else ",".join(SUMMARY_COLUMNS)
        self.maintenance()
        with self._reader() as reader:
            rows = reader.execute(
                f"SELECT {columns} FROM llm_calls WHERE {' AND '.join(clauses)} ORDER BY started_at,id LIMIT 100001",
                params,
            ).fetchall()
        if len(rows) > 100000:
            raise ValueError("记录超过 10 万条，请缩短统计时间或按群、模型筛选")
        return [dict(row) for row in rows]

    def export_csv(self, rows: list[dict], directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob("llm-*.csv"):
            with suppress(OSError):
                if old.stat().st_mtime < time.time() - 86400:
                    old.unlink(missing_ok=True)
        path = directory / f"llm-{uuid.uuid4().hex}.csv"
        # Read large optional text fields one row at a time, not all into RAM.
        try:
            with self._reader() as reader, path.open("x", newline="", encoding="utf-8-sig") as f:
                columns = [row[1] for row in reader.execute("PRAGMA table_info(llm_calls)")]
                columns += ["started_at_utc", "finished_at_utc"]
                writer = csv.DictWriter(f, fieldnames=columns)
                writer.writeheader()
                for row in rows:
                    stored = reader.execute(
                        "SELECT * FROM llm_calls WHERE id=? AND group_id=?", (row["id"], row["group_id"])
                    ).fetchone()
                    if stored is None:
                        continue
                    values = dict(stored)
                    for key in ("started_at", "finished_at"):
                        values[key + "_utc"] = (
                            datetime.fromtimestamp(values[key], timezone.utc).isoformat()
                            if values[key] is not None
                            else ""
                        )
                    writer.writerow({key: _csv_cell(value) for key, value in values.items()})
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def detail(self, call_id, group_ids):
        if not group_ids:
            return None
        with self._reader() as reader:
            row = reader.execute(
                f"SELECT * FROM llm_calls WHERE id=? AND group_id IN ({','.join('?' for _ in group_ids)})",
                (call_id, *group_ids),
            ).fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._db.close()


def _csv_cell(value: Any) -> Any:
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def summarize(rows: list[dict]) -> dict:
    statuses = Counter(row["status"] for row in rows)
    durations = sorted(row["duration_ms"] for row in rows if row["duration_ms"] is not None)
    totals = {key: sum(row[key] for row in rows if row[key] is not None) for key in TOKEN_FIELDS}
    known = {key: sum(row[key] is not None for row in rows) for key in TOKEN_FIELDS}
    cache_rows = [r for r in rows if r["input_tokens"] is not None and r["cached_tokens"] is not None]
    cache_inputs = sum(r["input_tokens"] for r in cache_rows)
    costs = defaultdict(Decimal)
    bounds = defaultdict(lambda: [Decimal(0), Decimal(0)])
    priced = 0
    bounded = 0
    for row in rows:
        if row["estimated_cost"] is not None:
            costs[row["currency"]] += Decimal(row["estimated_cost"])
            priced += 1
        lower, upper = row.get("cost_lower"), row.get("cost_upper")
        if lower is None and row["estimated_cost"] is not None:
            lower = upper = row["estimated_cost"]
        if lower is not None and upper is not None:
            bounds[row["currency"]][0] += Decimal(lower)
            bounds[row["currency"]][1] += Decimal(upper)
            bounded += 1
    attempts = Counter(row["operation_id"] for row in rows)
    return dict(
        count=len(rows),
        statuses=statuses,
        totals=totals,
        known=known,
        operations=len(attempts),
        retries=sum(row["attempt"] > 1 for row in rows),
        cache_known=len(cache_rows),
        cache_rate=sum(r["cached_tokens"] for r in cache_rows) / cache_inputs if cache_inputs else None,
        average_ms=sum(durations) / len(durations) if durations else None,
        p95_ms=durations[math.ceil(len(durations) * 0.95) - 1] if durations else None,
        costs=dict(costs),
        priced=priced,
        cost_bounds=dict(bounds),
        bounded=bounded,
    )


def format_statistics(rows: list[dict], *, days: int, enabled: bool) -> str:
    if not rows:
        return f"最近 {days} 天没有可见的 LLM 调用记录。" + ("当前统计已关闭。" if not enabled else "")
    stats = summarize(rows)
    count = stats["count"]

    def tokens(key):
        return f"{stats['totals'][key]}（已知 {stats['known'][key]}/{count} 次）"

    rate = "未知" if stats["cache_rate"] is None else f"{stats['cache_rate']:.1%}"
    latency = (
        "未知"
        if stats["average_ms"] is None
        else f"{stats['average_ms'] / 1000:.2f}s / {stats['p95_ms'] / 1000:.2f}s"
    )
    costs = "；".join(f"{currency} {cost:.6f}" for currency, cost in sorted(stats["costs"].items())) or "未知"
    models = Counter((row["provider_id"], row["model"]) for row in rows)
    lines = [
        f"LLM 统计 · 最近 {days} 天（管理员可见）",
        f"调用 {count} 次；调用单元 {stats['operations']}；重试 {stats['retries']} 次",
        "状态：" + "、".join(f"{STATUS_LABELS.get(k, k)}={v}" for k, v in sorted(stats["statuses"].items())),
        f"输入：{tokens('input_tokens')}",
        f"缓存命中：{tokens('cached_tokens')}",
        f"缓存未命中：{tokens('uncached_tokens')}",
        f"输出：{tokens('output_tokens')}",
        f"思考（输出明细，不重复相加）：{tokens('reasoning_tokens')}",
        f"缓存命中率：{rate}（用量完整 {stats['cache_known']}/{count} 次）",
        f"调用平均 / P95 耗时：{latency}",
        f"已知估算费用：{costs}（已计价 {stats['priced']}/{count} 次）",
    ]
    if stats["bounded"] > stats["priced"]:
        bounds = "；".join(
            f"{unit} {lo:.6f}–{hi:.6f}" for unit, (lo, hi) in sorted(stats["cost_bounds"].items())
        )
        lines.append(
            f"含缓存未知调用的费用区间：{bounds}（覆盖 {stats['bounded']}/{count} 次，包含上述已计价调用）"
        )
    lines.extend(
        f"模型 {provider} / {model or '未知'}：{n} 次" for (provider, model), n in models.most_common(10)
    )
    if len(models) > 10:
        lines.append("其余模型请筛选查询或导出 CSV。")
    lines.append("包含测试及无效输出；不含 QQ 查询和发送等待。缺失用量和内部网络重试可能使费用不完整。")
    if not enabled:
        lines.append("当前统计已关闭，以上为历史数据。")
    return "\n".join(lines)


def format_call_detail(rows: list[dict]) -> str:
    if not rows:
        return "\nLLM：没有统计记录（可能未调用、未开启统计或已过保留期）。"
    stats = summarize(rows)
    costs = "；".join(f"{currency} {cost:.6f}" for currency, cost in sorted(stats["costs"].items())) or "未知"
    lines = [
        f"\nLLM 调用记录：{len(rows)} 次；已知累计估算费用：{costs}（已计价 {stats['priced']}/{len(rows)} 次）"
    ]
    for row in rows[-10:]:
        duration = f"{row['duration_ms'] / 1000:.2f}s" if row["duration_ms"] is not None else "未知"
        values = [
            str(row[k]) if row[k] is not None else "未知"
            for k in ("input_tokens", "output_tokens", "cached_tokens")
        ]
        cost = f"{row['currency']} {row['estimated_cost']}" if row["estimated_cost"] is not None else "未知"
        if row["estimated_cost"] is None and row.get("cost_lower") is not None:
            cost = f"{row['currency']} {row['cost_lower']}–{row['cost_upper']}（缓存未知）"
        lines.append(
            f"#{row['id']} {row['source']} 第{row['attempt']}次 {row['status']} "
            f"{row['model'] or '模型未知'}；输入/输出/命中={'/'.join(values)}；{duration}；费用={cost}"
        )
        if row.get("error_detail"):
            lines.append("  " + row["error_detail"])
    if len(rows) > 10:
        lines.append("仅展示最近 10 次，完整记录可导出 CSV。")
    return "\n".join(lines)
