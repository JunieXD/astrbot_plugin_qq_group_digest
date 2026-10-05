"""Durable album intents, sharing the digest database worker and transactions."""

from __future__ import annotations

import json

from .config import DigestError

SCHEMA = """
    CREATE TABLE IF NOT EXISTS archives (
        id TEXT PRIMARY KEY, run TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        target TEXT NOT NULL, part INTEGER NOT NULL, file TEXT NOT NULL DEFAULT '',
        album_name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL DEFAULT 'pending', album_id TEXT NOT NULL DEFAULT '',
        receipt TEXT NOT NULL DEFAULT '{}', submitted REAL,
        next_try REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '', UNIQUE(run,target,part)
    );
    CREATE INDEX IF NOT EXISTS archives_run ON archives(run,target,part);
    CREATE INDEX IF NOT EXISTS archives_ready ON archives(state,next_try);
"""

TERMINAL = ("uploaded", "sent", "skipped")


class AlbumStore:
    """All methods run on Store's worker; album and message progress stay separate."""

    def __init__(self, owner):
        self.owner = owner

    @property
    def db(self):
        return self.owner.db

    @staticmethod
    def identity(rid, target, part):
        return f"{rid}:album:{target}:{part}"

    @staticmethod
    def decode(row):
        value = dict(row)
        value["receipt"] = json.loads(value["receipt"])
        value["config"] = json.loads(value["config"])
        return value

    def select(self, condition, values=(), suffix="ORDER BY r.end,a.target,a.part"):
        return [
            self.decode(row)
            for row in self.db.execute(
                "SELECT a.*,r.task,r.account,r.platform,r.start,r.end,r.config "
                "FROM archives a JOIN runs r ON r.id=a.run WHERE " + condition + " " + suffix,
                values,
            )
        ]

    def insert(self, rid, archives, ignore=False):
        # Deliberately no nested transaction: generated() commits every intent atomically.
        for archive in archives:
            target = str(archive["target"])
            part = archive["part"]
            file = archive.get("file", "")
            name = archive["album_name"]
            if not target or not name or not isinstance(part, int) or part < 0:
                raise DigestError("群相册归档任务无效。")
            if not isinstance(file, str):
                raise DigestError("群相册图片路径无效。")
            self.db.execute(
                ("INSERT OR IGNORE" if ignore else "INSERT")
                + " INTO archives(id,run,target,part,file,album_name,description,state) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    self.identity(rid, target, part),
                    rid,
                    target,
                    part,
                    file,
                    name,
                    archive.get("description", ""),
                    "pending" if file else "render",
                ),
            )

    def plan(self, rid, archives):
        # Used only by explicit administrator archival of an already generated batch.
        with self.db:
            if not self.db.execute("SELECT 1 FROM runs WHERE id=? AND digest IS NOT NULL", (rid,)).fetchone():
                raise DigestError("这个批次尚未生成摘要，无法归档。")
            before = self.db.total_changes
            self.insert(rid, archives, ignore=True)
            return self.db.total_changes - before

    def active(self, task_key, now):
        # Unknown writes require explicit reconciliation; polling must never replay them.
        return self.select("r.task=? AND a.next_try<=? AND a.state IN ('render','pending')", (task_key, now))

    def rows(self, rid):
        return self.select("a.run=?", (rid,))

    def materialize(self, rid, files):
        files = list(files)
        if not files or any(not isinstance(file, str) or not file for file in files):
            raise DigestError("群相册归档没有可用的图片。")
        count = 0
        with self.db:
            rows = self.db.execute(
                "SELECT * FROM archives WHERE run=? ORDER BY target,part", (rid,)
            ).fetchall()
            targets = {row["target"] for row in rows if row["state"] == "render"}
            for target in sorted(targets):
                pending = [row for row in rows if row["target"] == target]
                # A completed/materialized target cannot be expanded a second time.
                if any(row["state"] != "render" for row in pending):
                    continue
                first = pending[0]
                self.db.execute("DELETE FROM archives WHERE run=? AND target=?", (rid, target))
                self.insert(
                    rid,
                    [
                        {
                            "target": target,
                            "part": part,
                            "file": file,
                            "album_name": first["album_name"],
                            "description": first["description"]
                            + (f" · 第{part + 1}/{len(files)}页" if len(files) > 1 else ""),
                        }
                        for part, file in enumerate(files)
                    ],
                )
                count += len(files)
        return count

    def submit(self, aid, account, now, limit, receipt=None, album_id=""):
        if receipt is not None and not isinstance(receipt, dict):
            raise DigestError("群相册写入凭据无效。")
        with self.db:
            row = self.db.execute(
                "SELECT a.state,a.receipt,a.album_id,r.account "
                "FROM archives a JOIN runs r ON r.id=a.run WHERE a.id=?",
                (aid,),
            ).fetchone()
            if not row or row["state"] != "pending":
                return False
            if row["account"] != account:
                raise DigestError("群相册归档绑定的机器人 QQ 已改变，请检查任务绑定。")
            self.owner._budget("send", account, now, 86400, limit)
            self.db.execute(
                "UPDATE archives SET state='submitted',submitted=?,receipt=?,album_id=?,error='' WHERE id=?",
                (
                    now,
                    json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
                    if receipt is not None
                    else row["receipt"],
                    str(album_id) if album_id else row["album_id"],
                    aid,
                ),
            )
        return True

    def created(self, aid, album_id):
        if not album_id:
            raise DigestError("群相册创建结果缺少相册编号。")
        with self.db:
            row = self.db.execute("SELECT * FROM archives WHERE id=?", (aid,)).fetchone()
            if not row or row["state"] not in {"submitted", "unknown"}:
                return False
            if json.loads(row["receipt"]).get("operation") != "create":
                raise DigestError("这个写入不是创建相册，不能恢复为待上传。")
            self.db.execute(
                "UPDATE archives SET state='pending',album_id=?,receipt='{}',next_try=0,error='' WHERE id=?",
                (str(album_id), aid),
            )
        return True

    def result(self, aid, state, album_id="", receipt=None, error="", next_try=0):
        if state not in {"pending", "unknown", "blocked", "uploaded", "sent", "skipped"}:
            raise DigestError("群相册归档结果状态无效。")
        with self.db:
            row = self.db.execute("SELECT * FROM archives WHERE id=?", (aid,)).fetchone()
            if not row or row["state"] in TERMINAL:
                return False
            if row["state"] == "unknown" and state in {"pending", "blocked"}:
                raise DigestError("群相册写入结果不明，请先核对或明确跳过，不能自动重试。")
            if state == "pending" and not row["file"]:
                raise DigestError("群相册图片尚未生成，不能提交归档。")
            if state == "pending" and row["state"] == "submitted":
                raise DigestError("群相册写入尚未确认；创建成功请明确确认创建结果。")
            # Empty parameters preserve a known album ID and receipt across later phases.
            self.db.execute(
                "UPDATE archives SET state=?,album_id=?,receipt=?,error=?,next_try=? WHERE id=?",
                (
                    state,
                    str(album_id) if album_id else row["album_id"],
                    json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
                    if receipt is not None
                    else row["receipt"],
                    error,
                    next_try,
                    aid,
                ),
            )
        return True

    def defer(self, aid, error, next_try, max_attempts, count=True):
        with self.db:
            self.db.execute(
                "UPDATE archives SET failures=failures+?,error=?,next_try=? "
                "WHERE id=? AND state IN ('render','pending')",
                (int(count), error, next_try, aid),
            )
            self.db.execute(
                "UPDATE archives SET state='blocked' "
                "WHERE id=? AND state IN ('render','pending') AND failures>=?",
                (aid, max_attempts),
            )

    def cancel_removed(self, task_key, targets):
        targets = list(dict.fromkeys(str(target) for target in targets))
        condition = ""
        if targets:
            condition = " AND target NOT IN (" + ",".join("?" for _ in targets) + ")"
        with self.db:
            return self.db.execute(
                "UPDATE archives SET state='skipped',error='群相册目标已移除' "
                "WHERE run IN (SELECT id FROM runs WHERE task=?) "
                "AND state IN ('render','pending','blocked')" + condition,
                (task_key, *targets),
            ).rowcount

    def retry(self, rid):
        with self.db:
            return self.db.execute(
                "UPDATE archives SET state=CASE WHEN file='' THEN 'render' ELSE 'pending' END,"
                "failures=0,next_try=0,error='' WHERE run=? AND state='blocked'",
                (rid,),
            ).rowcount

    def skip(self, rid, target=None):
        condition = " AND target=?" if target is not None else ""
        values = (rid, str(target)) if target is not None else (rid,)
        with self.db:
            return self.db.execute(
                "UPDATE archives SET state='skipped',error='管理员已跳过群相册归档' "
                "WHERE run=? AND state IN ('render','pending','blocked','unknown')" + condition,
                values,
            ).rowcount

    def attention(self, task_key, limit=5):
        condition = "r.task=? AND a.state IN ('unknown','blocked')"
        total = self.db.execute(
            "SELECT COUNT(*) FROM archives a JOIN runs r ON r.id=a.run WHERE " + condition,
            (task_key,),
        ).fetchone()[0]
        return self.select(condition, (task_key, limit), "ORDER BY r.end,a.target,a.part LIMIT ?"), total

    def unknown(self, task_key, limit=5):
        condition = "r.task=? AND a.state='unknown'"
        total = self.db.execute(
            "SELECT COUNT(*) FROM archives a JOIN runs r ON r.id=a.run WHERE " + condition,
            (task_key,),
        ).fetchone()[0]
        return self.select(condition, (task_key, limit), "ORDER BY r.end,a.target,a.part LIMIT ?"), total
