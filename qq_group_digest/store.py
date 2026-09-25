"""SQLite is the authority for windows, quotas and write-ahead delivery intents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

from .config import Deferred, DigestError


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    def __init__(self, path):
        self.path = path
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qq-digest-db")
        self.closed = False

    async def call(self, method, *args):
        if self.closed:
            raise DigestError("摘要数据库已经关闭。")
        future = asyncio.get_running_loop().run_in_executor(self.worker, getattr(self, method), *args)
        cancelled = False
        # A reload must wait for an already submitted transaction before releasing the instance lock.
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
            except (sqlite3.Error, OSError) as exc:
                raise DigestError("摘要进度无法写入，请检查磁盘空间和数据目录权限。") from exc
        try:
            result = future.result()
        except (sqlite3.Error, OSError) as exc:
            raise DigestError("摘要进度无法写入，请检查磁盘空间和数据目录权限。") from exc
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def close(self):
        if not self.closed:
            try:
                await self.call("close_db")
            finally:
                self.closed = True
                self.worker.shutdown(wait=True)

    def open_db(self):
        self.db = sqlite3.connect(self.path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1):
            raise DigestError("摘要数据库来自较新版本，请升级插件。")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY, task TEXT NOT NULL, start INTEGER, end INTEGER,
                read_start INTEGER, config TEXT NOT NULL, created REAL, status TEXT NOT NULL,
                account TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT '',
                snapshot TEXT, digest TEXT, notes TEXT NOT NULL DEFAULT '[]',
                llm_calls INTEGER NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', UNIQUE(task,end)
            );
            CREATE INDEX IF NOT EXISTS runs_task ON runs(task,end DESC);
            CREATE TABLE IF NOT EXISTS deliveries (
                id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
                target TEXT NOT NULL, part INTEGER NOT NULL, mode TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending', message_id TEXT NOT NULL DEFAULT '',
                submitted REAL, next_try REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                failures INTEGER NOT NULL DEFAULT 0,
                UNIQUE(run,target,part)
            );
            CREATE INDEX IF NOT EXISTS deliveries_run ON deliveries(run,target,part);
            CREATE TABLE IF NOT EXISTS budget (kind TEXT, scope TEXT, at REAL);
            CREATE INDEX IF NOT EXISTS budget_window ON budget(kind,scope,at);
            PRAGMA user_version=1;
        """)
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET state='unknown', error='发送过程中插件退出，需要核对' WHERE state='submitted'"
            )

    def close_db(self):
        if hasattr(self, "db"):
            try:
                self.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
            finally:
                self.db.close()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, encode(value)))

    def extend(self, key, value):
        value = max(self.get(key, 0), value)
        self.set(key, value)
        return value

    def baseline(self, task, end):
        key = "cursor:" + task
        if self.get(key) is None:
            self.set(key, {"end": end, "first": True})

    def create_run(self, task, start, end, read_start, config, notes, now):
        rid = hashlib.sha256(f"{task}:{end}".encode()).hexdigest()[:20]
        with self.db:
            self.db.execute(
                "UPDATE runs SET status='superseded',error='并入后续补报' WHERE task=? AND end<? AND status='queued' AND snapshot IS NULL",
                (task, end),
            )
            self.db.execute(
                "INSERT OR IGNORE INTO runs(id,task,start,end,read_start,config,notes,created,status) VALUES (?,?,?,?,?,?,?,?,'queued')",
                (rid, task, start, end, read_start, encode(config), encode(notes), now),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO state VALUES (?,?)",
                ("cursor:" + task, encode({"end": end, "first": False})),
            )
        return rid

    def oldest_queued_start(self, task):
        row = self.db.execute(
            "SELECT MIN(start) FROM runs WHERE task=? AND status='queued' AND snapshot IS NULL", (task,)
        ).fetchone()
        return row[0]

    def expire_run(self, rid):
        with self.db:
            self.db.execute(
                "UPDATE runs SET status='superseded',error='超过补报时长，停止处理旧批次' WHERE id=? AND status IN ('queued','fetched')",
                (rid,),
            )

    @staticmethod
    def decode_run(row):
        if row is None:
            return None
        value = dict(row)
        for key in ("config", "notes", "snapshot", "digest"):
            if value[key] is not None:
                value[key] = json.loads(value[key])
        return value

    def run(self, rid):
        return self.decode_run(self.db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone())

    def lookup(self, prefix):
        rows = self.db.execute("SELECT id FROM runs WHERE id LIKE ? LIMIT 2", (prefix + "%",)).fetchall()
        if len(rows) != 1:
            raise DigestError("批次编号不存在或不唯一，请从状态中复制批次编号。")
        return self.run(rows[0][0])

    def latest(self, task, count=5):
        rows = self.db.execute("SELECT * FROM runs WHERE task=? ORDER BY end DESC LIMIT ?", (task, count))
        return [self.decode_run(r) for r in rows]

    def active(self, task, now):
        rows = self.db.execute(
            """SELECT * FROM runs WHERE task=? AND next_try<=? AND (
                status IN ('queued','fetched') OR (status='generated' AND EXISTS (
                    SELECT 1 FROM deliveries WHERE run=runs.id AND state IN ('pending','blocked')
                ))) ORDER BY end""",
            (task, now),
        )
        return [self.decode_run(r) for r in rows]

    def attention(self, task, limit=5):
        condition = """task=? AND (status='failed' OR EXISTS (
            SELECT 1 FROM deliveries WHERE run=runs.id AND state IN ('unknown','blocked')
        ))"""
        total = self.db.execute("SELECT COUNT(*) FROM runs WHERE " + condition, (task,)).fetchone()[0]
        rows = self.db.execute(
            "SELECT * FROM runs WHERE " + condition + " ORDER BY end LIMIT ?", (task, limit)
        )
        return [self.decode_run(r) for r in rows], total

    def bind(self, rid, account, platform):
        row = self.run(rid)
        binding = self.get("account:" + row["task"])
        if binding and binding != account:
            raise DigestError("该来源群绑定的机器人 QQ 已改变，请在任务高级设置中明确填写新的机器人 QQ。")
        if row["account"] and row["account"] != account:
            raise DigestError("机器人 QQ 已改变，本批次停止；请检查任务绑定。")
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO state VALUES (?,?)", ("account:" + row["task"], encode(account))
            )
            self.db.execute("UPDATE runs SET account=?, platform=? WHERE id=?", (account, platform, rid))

    def fetched(self, rid, snapshot, notes):
        with self.db:
            self.db.execute(
                "UPDATE runs SET snapshot=?,notes=?,status='fetched',error='' WHERE id=?",
                (encode(snapshot), encode(notes), rid),
            )

    def reconfigure(self, rid, config):
        row = self.run(rid)
        if row["status"] not in {"queued", "fetched"} or encode(row["config"]) == encode(config):
            return
        reread = any(row["config"].get(k) != config.get(k) for k in ("read_forwards", "forward_limit"))
        with self.db:
            self.db.execute("UPDATE runs SET config=? WHERE id=?", (encode(config), rid))
            if reread:
                self.db.execute("UPDATE runs SET snapshot=NULL,status='queued' WHERE id=?", (rid,))

    def generated(self, rid, digest, deliveries):
        with self.db:
            row = self.run(rid)
            if row["status"] not in ("queued", "fetched"):
                return
            self.db.execute(
                "UPDATE runs SET digest=?,status=?,error='',failures=0 WHERE id=?",
                (encode(digest), "generated" if deliveries else "complete", rid),
            )
            for delivery in deliveries:
                did = f"{rid}:{delivery['target']}:{delivery['part']}"
                self.db.execute(
                    "INSERT INTO deliveries(id,run,target,part,mode,payload) VALUES (?,?,?,?,?,?)",
                    (
                        did,
                        rid,
                        delivery["target"],
                        delivery["part"],
                        delivery["mode"],
                        encode(delivery["payload"]),
                    ),
                )

    def fail(self, rid, error, next_try, max_attempts, count=True):
        with self.db:
            if count:
                self.db.execute("UPDATE runs SET failures=failures+1 WHERE id=?", (rid,))
            self.db.execute("UPDATE runs SET error=?,next_try=? WHERE id=?", (error, next_try, rid))
            self.db.execute(
                "UPDATE runs SET status='failed' WHERE id=? AND failures>=? AND status IN ('queued','fetched')",
                (rid, max_attempts),
            )

    def retry(self, rid):
        row = self.run(rid)
        if row["status"] == "generated":
            with self.db:
                count = self.db.execute(
                    "UPDATE deliveries SET state='pending',failures=0,next_try=0,error='' WHERE run=? AND state='blocked'",
                    (rid,),
                ).rowcount
            if count:
                with self.db:
                    self.db.execute("UPDATE runs SET failures=0,next_try=0,error='' WHERE id=?", (rid,))
                return
        if row["status"] != "failed":
            raise DigestError("这个批次没有停止生成；结果不明的发送请使用核对或跳过。")
        with self.db:
            self.db.execute(
                "UPDATE runs SET status=?,failures=0,llm_calls=0,next_try=0,error='' WHERE id=?",
                ("fetched" if row["snapshot"] is not None else "queued", rid),
            )

    def previous_items(self, task, before):
        row = self.db.execute(
            "SELECT digest FROM runs WHERE task=? AND end<? AND digest IS NOT NULL ORDER BY end DESC LIMIT 1",
            (task, before),
        ).fetchone()
        return json.loads(row[0])["items"] if row else []

    def deliveries(self, rid):
        rows = self.db.execute("SELECT * FROM deliveries WHERE run=? ORDER BY target,part", (rid,))
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def _budget(self, kind, scope, now, seconds, limit):
        row = self.db.execute(
            "SELECT COUNT(*),MIN(at) FROM budget WHERE kind=? AND scope=? AND at>?",
            (kind, scope, now - seconds),
        ).fetchone()
        if row[0] >= limit:
            raise Deferred(
                f"{ {'read': '历史查询', 'send': '摘要发送', 'llm': '模型调用'}.get(kind, kind) }已达到配置额度，稍后继续。",
                row[1] + seconds - now + 1,
            )
        self.db.execute("INSERT INTO budget VALUES (?,?,?)", (kind, scope, now))

    def reserve_budget(self, kind, scope, now, seconds, limit):
        with self.db:
            self._budget(kind, scope, now, seconds, limit)

    def reserve_llm(self, rid, now, daily_limit, run_limit):
        # Automatic retries and reloads share a round's allowance. Only an explicit
        # administrator retry resets it; the rolling daily allowance never resets.
        with self.db:
            if rid is not None:
                row = self.db.execute("SELECT llm_calls FROM runs WHERE id=?", (rid,)).fetchone()
                if row is None or row[0] >= run_limit:
                    raise DigestError("本期模型调用次数达到上限，请调整配置后手动重试。")
            self._budget("llm", "plugin", now, 86400, daily_limit)
            if rid is not None:
                self.db.execute("UPDATE runs SET llm_calls=llm_calls+1 WHERE id=?", (rid,))

    def submit(self, did, account, now, limit):
        with self.db:
            row = self.db.execute("SELECT state FROM deliveries WHERE id=?", (did,)).fetchone()
            if not row or row[0] != "pending":
                return False
            self._budget("send", account, now, 86400, limit)
            self.db.execute(
                "UPDATE deliveries SET state='submitted',submitted=?,error='' WHERE id=?", (now, did)
            )
        return True

    def delivery_result(self, did, state, message_id="", error="", next_try=0):
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET state=?,message_id=?,error=?,next_try=? WHERE id=?",
                (state, str(message_id), error, next_try, did),
            )

    def defer_delivery(self, did, error, next_try, max_attempts, count=True):
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET failures=failures+?,error=?,next_try=? WHERE id=? AND state='pending'",
                (int(count), error, next_try, did),
            )
            self.db.execute(
                "UPDATE deliveries SET state='blocked' WHERE id=? AND state='pending' AND failures>=?",
                (did, max_attempts),
            )

    def replace_pending_target(self, rid, target, mode, payload):
        with self.db:
            rows = self.db.execute(
                "SELECT state FROM deliveries WHERE run=? AND target=?", (rid, target)
            ).fetchall()
            if not rows or any(r[0] != "pending" for r in rows):
                raise DigestError("该目标已经开始投递，无法再切换展示方式。")
            self.db.execute("DELETE FROM deliveries WHERE run=? AND target=?", (rid, target))
            self.db.execute(
                "INSERT INTO deliveries(id,run,target,part,mode,payload) VALUES (?,?,?,?,?,?)",
                (f"{rid}:{target}:0", rid, target, 0, mode, encode(payload)),
            )

    def skip(self, rid, target=None):
        with self.db:
            if target:
                self.db.execute(
                    "UPDATE deliveries SET state='skipped',error='已跳过' WHERE run=? AND target=? AND state IN ('pending','unknown','blocked')",
                    (rid, target),
                )
            else:
                self.db.execute(
                    "UPDATE deliveries SET state='skipped',error='已跳过' WHERE run=? AND state IN ('pending','unknown','blocked')",
                    (rid,),
                )
                self.db.execute(
                    "UPDATE runs SET status='complete',error='管理员已跳过本期摘要' WHERE id=? AND status IN ('queued','fetched','failed')",
                    (rid,),
                )
        self.finish(rid)

    def expire_pending(self, rid):
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET state='skipped',error='超过补报时长，停止投递旧摘要' WHERE run=? AND state IN ('pending','blocked')",
                (rid,),
            )
        self.finish(rid)

    def remove_target(self, rid, target):
        # A configuration edit only cancels requests we know were never submitted.
        # Unknown sends still need explicit administrator reconciliation or skipping.
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET state='skipped',error='目标群已移除' WHERE run=? AND target=? AND state IN ('pending','blocked')",
                (rid, target),
            )
        self.finish(rid)

    def finish(self, rid):
        remaining = self.db.execute(
            "SELECT COUNT(*) FROM deliveries WHERE run=? AND state NOT IN ('sent','skipped')", (rid,)
        ).fetchone()[0]
        if not remaining:
            with self.db:
                self.db.execute(
                    "UPDATE runs SET status='complete',error='' WHERE id=? AND status='generated'", (rid,)
                )

    def cleanup(self, now, snapshot_days, result_days):
        with self.db:
            self.db.execute("DELETE FROM budget WHERE at<?", (now - 86401,))
            self.db.execute(
                "UPDATE runs SET snapshot=NULL WHERE created<? AND status IN ('complete','failed','generated')",
                (now - snapshot_days * 86400,),
            )
            # Unresolved sends keep their evidence even beyond ordinary retention.
            self.db.execute(
                "DELETE FROM runs WHERE created<? AND status IN ('complete','superseded')",
                (now - result_days * 86400,),
            )
