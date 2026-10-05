"""Independent, durable poster archiving; previews and LLM calls stay separate."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from .album_transport import AlbumRejected, AlbumTransport, AlbumUncertain
from .config import IMAGE_MODE, Deferred, DigestError, Task
from .images import image_files, local_image_path
from .models import Digest


def album_description(task, start, end):
    zone = ZoneInfo(task.timezone)
    first, last = (datetime.fromtimestamp(stamp, zone) for stamp in (start, end))

    def date_label(value):
        # Python 3.10 on Windows routes strftime through the active ANSI
        # locale. Keep Chinese literals outside that API (including f-string
        # datetime formats) so English Windows can archive Chinese captions.
        return f"{value.year:04d}年{value.month:02d}月{value.day:02d}日 {value.hour:02d}:{value.minute:02d}"

    return date_label(first) + "—" + date_label(last)


def archive_plan(task, digest, start, end, payloads=(), *, targets=None):
    if not task.album_enabled or not digest.items:
        return []
    files = list(dict.fromkeys(file for payload in payloads for file in image_files(payload)))
    targets = task.album_target_groups if targets is None else targets
    description = album_description(task, start, end)
    return [
        {
            "target": target,
            "part": part,
            "file": file,
            "album_name": task.album_name,
            "description": description + (f" · 第 {part + 1}/{len(files)} 页" if len(files) > 1 else ""),
        }
        for target in targets
        for part, file in enumerate(files or [""])
    ]


class AlbumArchive:
    def __init__(self, service, *, transport=AlbumTransport):
        self.s, self.transport = service, transport

    async def allowed(self, row):
        live = self.s.current(row["task"])
        return (
            live is not None
            and live.album_enabled
            and row["target"] in live.album_target_groups
            and live.album_name == row["album_name"]
            and await self.s.allowed(row["task"])
        )

    async def ready(self, run, target):
        live = self.s.current(run["task"])
        if live is None or not live.album_enabled or target not in live.album_target_groups:
            return True
        rows = [row for row in await self.s.store.call("archive_rows", run["id"]) if row["target"] == target]
        # Enabling archives never retroactively changes an already generated run.
        # Explicit administrator skips intentionally release the following send.
        return all(row["state"] in {"uploaded", "sent", "skipped"} for row in rows)

    async def process(self, current):
        targets = current.album_target_groups if current.album_enabled else ()
        await self.s.store.call("archive_cancel_removed", current.key, targets)
        rows = await self.s.store.call("archive_active", current.key, self.s.clock())
        for row in rows:
            try:
                # Materializing one placeholder expands every target in this run.
                latest = next(
                    (r for r in await self.s.store.call("archive_rows", row["run"]) if r["id"] == row["id"]),
                    None,
                )
                if latest is None or latest["state"] not in {"render", "pending"}:
                    continue
                row = latest
                if not await self.allowed(row):
                    # A rename must not keep old archive intents indefinitely.
                    if current.album_name != row["album_name"]:
                        await self.s.store.call(
                            "archive_result", row["id"], "skipped", "", None, "相册名称已更改"
                        )
                    continue
                task = Task.restore(row["config"])
                if self.s.clock() - row["end"] > task.catchup_hours * 3600:
                    await self.s.store.call(
                        "archive_result", row["id"], "skipped", "", None, "已超过补报时长"
                    )
                    continue
                adapter = await self.s.router.resolve(task)
                if adapter.account != row["account"]:
                    raise DigestError("机器人身份变化，本批次停止群相册归档。")
                if row["state"] == "render":
                    await self.materialize(row, task, adapter)
                    continue
                # Keep page order; an uncertain/blocked earlier page is never bypassed.
                siblings = await self.s.store.call("archive_rows", row["run"])
                if any(
                    other["target"] == row["target"]
                    and other["part"] < row["part"]
                    and other["state"] not in {"uploaded", "sent", "skipped"}
                    for other in siblings
                ):
                    continue
                await self.write(row, task, adapter)
            except Deferred as exc:
                await self.defer(row, str(exc), exc.seconds, count=False)
            except Exception as exc:
                safe = str(exc) if isinstance(exc, DigestError) else "群相册归档准备失败，请检查插件日志。"
                await self.defer(row, safe, min(1800, 60 * 2 ** min(row["failures"], 5)))
                self.s.journal.record("群相册归档失败", run=row["run"], target=row["target"], reason=safe)

    async def defer(self, row, error, seconds, *, count=True):
        await self.s.store.call(
            "archive_defer",
            row["id"],
            error,
            self.s.clock() + seconds,
            self.s.settings().limits.max_attempts,
            count,
        )

    async def materialize(self, row, task, adapter):
        run = await self.s.store.call("run", row["run"])
        if not run["digest"] or not self.s.presentation:
            raise DigestError("归档所需的摘要或海报渲染器不可用。")
        digest = Digest.restore(run["digest"])
        payloads = await self.s.payloads(
            replace(task, poster_fallback_to_plain=False),
            digest,
            row["start"],
            row["end"],
            adapter.account,
            IMAGE_MODE,
        )
        files = [file for payload in payloads for file in image_files(payload)]
        if not files:
            raise DigestError("群相册归档没有生成可用海报，已暂缓。")
        await self.s.store.call("archive_materialize", row["run"], files)
        self.s.wake.set()

    async def preflight(self, row, task, adapter):
        if not await self.allowed(row):
            raise Deferred("任务已暂停、归档已关闭或目标相册已更改。")
        if self.s.clock() - row["end"] > task.catchup_hours * 3600:
            raise Deferred("归档已超过补报时长。")
        fresh = await self.s.router.resolve(task)
        if fresh is not adapter or fresh.account != row["account"]:
            raise Deferred("QQ 接入发生变化，暂缓群相册归档。")
        await adapter.ready_to_send()

    async def write(self, row, task, adapter):
        transport = self.transport(adapter, self.s.journal)
        # Resolve the name from a complete list; never infer absence from a partial page.
        await self.preflight(row, task, adapter)
        member = await adapter.read("get_group_member_info", group_id=row["target"], user_id=adapter.account)
        if not isinstance(member, dict) or str(member.get("user_id")) != adapter.account:
            raise DigestError("无法确认机器人在相册目标群中的成员身份。")
        album_id = await transport.find_album(row["target"], row["album_name"])
        siblings = await self.s.store.call("archive_rows", row["run"])
        bound = {r["album_id"] for r in siblings if r["target"] == row["target"] and r["album_id"]}
        if bound and bound != {album_id}:
            raise DigestError("本批次绑定的群相册已删除或更改，已停止归档，请检查相册状态。")
        creating = album_id is None
        if creating and not await adapter.packet_ready():
            raise Deferred("创建群相册所需的 NapCat 数据包能力暂不可用。", 600)

        async def submit(receipt):
            try:
                await self.preflight(row, task, adapter)
                changed = await self.s.store.call(
                    "archive_submit",
                    row["id"],
                    adapter.account,
                    self.s.clock(),
                    self.s.settings().pace.sends_per_day,
                    receipt,
                    album_id or "",
                )
                if not changed:
                    raise Deferred("这张归档图片已由其他操作处理。")
            except Deferred as exc:
                raise self.s.guard.deferred_error(str(exc)) from exc

        async def action():
            try:
                await self.preflight(row, task, adapter)
            except Deferred as exc:
                raise self.s.guard.deferred_error(str(exc)) from exc
            if creating:
                # Other tasks/users may have created it while this write waited
                # for the shared account queue. Recheck before any new intent.
                appeared = await transport.find_album(row["target"], row["album_name"])
                if appeared:
                    await self.s.store.call("archive_result", row["id"], "pending", appeared)
                    self.s.wake.set()
                    return
                aid = await transport.create_album(row["target"], row["album_name"], on_submit=submit)
                await self.s.store.call("archive_created", row["id"], aid)
                self.s.journal.record("群相册创建成功", target=row["target"], album_id=aid)
                self.s.wake.set()
            else:
                receipt = await transport.upload(
                    row["target"],
                    album_id,
                    row["album_name"],
                    local_image_path(row["file"]),
                    row["description"],
                    on_submit=submit,
                )
                await self.s.store.call("archive_result", row["id"], "uploaded", album_id, receipt)
                self.s.journal.record(
                    "群相册归档成功",
                    run=row["run"],
                    target=row["target"],
                    part=row["part"],
                    album_id=album_id,
                )
                self.s.wake.set()

        settings = self.s.settings()
        args = dict(
            account=adapter.pid,
            online=adapter.online,
            action=action,
            config=settings.pace.guard_config(),
            delay=settings.pace.interval("send"),
            gap=settings.pace.interval("gap"),
            key=None,
        )
        if getattr(self.s.guard, "scheduling_version", 1) >= 3:
            args.update(priority=2, group=row["target"], label="群摘要相册归档")
        try:
            await self.s.guard.run(**args)
        except BaseException as exc:
            latest = next(
                r for r in await self.s.store.call("archive_rows", row["run"]) if r["id"] == row["id"]
            )
            if latest["state"] == "submitted":
                receipt = exc.receipt if isinstance(exc, AlbumUncertain) else latest["receipt"]
                if isinstance(exc, AlbumRejected):
                    await self.s.store.call("archive_result", row["id"], "blocked", "", receipt, str(exc))
                    self.s.journal.record("群相册创建被拒绝", run=row["run"], target=row["target"])
                    return
                await self.s.store.call(
                    "archive_result",
                    row["id"],
                    "unknown",
                    "",
                    receipt,
                    "写入结果未确认，请使用 /群摘要 核对相册 来源群号。",
                )
                self.s.journal.record(
                    "群相册归档待核对",
                    run=row["run"],
                    target=row["target"],
                    operation=receipt.get("operation", ""),
                )
            elif latest["state"] in {"pending", "render"}:
                if isinstance(exc, (Deferred, self.s.guard.deferred_error)):
                    await self.defer(row, str(exc), getattr(exc, "seconds", 60), count=False)
                else:
                    await self.defer(row, "群相册归档准备失败，请检查插件日志。", 300)
            if isinstance(exc, asyncio.CancelledError):
                raise

    async def reconcile(self, current):
        rows, total = await self.s.store.call("archive_unknown", current.key)
        unknown = rows
        found = 0
        for row in unknown:
            task = Task.restore(row["config"])
            adapter = await self.s.router.resolve(task)
            if adapter.account != row["account"]:
                raise DigestError("机器人身份变化，不能核对旧账号的相册归档。")
            transport = self.transport(adapter, self.s.journal)
            if row["receipt"].get("operation") == "create":
                album_id = await transport.find_album(row["target"], row["album_name"])
                if album_id:
                    await self.s.store.call("archive_created", row["id"], album_id)
                    found += 1
            else:
                media = await transport.confirm(row["target"], row["album_id"], row["receipt"])
                if media is not None:
                    await self.s.store.call("archive_result", row["id"], "uploaded")
                    found += 1
        self.s.wake.set()
        self.s.journal.record("管理员核对群相册", group=current.source_group, confirmed=found)
        return f"已核对 {len(unknown)} 张，确认成功 {found} 张。尚未确认的记录不会自动重传。" + (
            "\n还有记录待处理，请再次核对或查看相册状态。" if total > len(rows) else ""
        )

    async def plan_existing(self, run, current, target=None):
        if not current.album_enabled or not await self.s.allowed(current.key):
            raise DigestError("请先启用这个任务的群相册归档。")
        if not run["digest"] or not run["account"]:
            raise DigestError("这个批次尚未生成摘要，不能归档。")
        if self.s.clock() - run["end"] > current.catchup_hours * 3600:
            raise DigestError("这个批次已超过补报时长，不能补传。")
        if target is not None and target not in current.album_target_groups:
            raise DigestError("指定群不在本任务的相册目标群中。")
        payloads = [row["payload"] for row in await self.s.store.call("deliveries", run["id"])]
        digest = Digest.restore(run["digest"])
        task = replace(
            Task.restore(run["config"]),
            album_enabled=True,
            album_name=current.album_name,
            album_target_groups=current.album_target_groups,
        )
        rows = archive_plan(
            task, digest, run["start"], run["end"], payloads, targets=(target,) if target else None
        )
        count = await self.s.store.call("archive_plan", run["id"], rows)
        self.s.wake.set()
        return f"已加入 {count} 张群相册归档任务；已有记录（含已跳过、待核对）不会重复上传。"
