"""Lifecycle and orchestration; the scheduler never owns delivery progress."""

from __future__ import annotations

import asyncio
import random
import time

from .config import Deferred, DigestError, Task
from .delivery import Delivery
from .history import HistoryReader
from .models import Message
from .render import make_payloads
from .schedule import latest_boundary
from .summarizer import Summarizer

OWNER = "astrbot_plugin_qq_group_digest"


class Service:
    def __init__(self, context, settings, store, router, guard, client, journal, *, clock=time.time):
        self.context, self.settings, self.store, self.router = context, settings, store, router
        self.guard, self.client, self.journal, self.clock = guard, client, journal, clock
        self.wake = asyncio.Event()
        self.workers, self.locks = {}, {}
        self.commands = set()
        self.cron_ids = []
        self.loop_task = None
        self.stopping = False
        self.last_cleanup = 0
        self.delivery = Delivery(store, router, guard, settings, journal, self.allowed, clock=clock)

    def lock(self, key):
        return self.locks.setdefault(key, asyncio.Lock())

    def current(self, key):
        return next((t for t in self.settings().tasks if t.key == key), None)

    async def allowed(self, key, target=None):
        settings = self.settings()
        task = next((t for t in settings.tasks if t.key == key), None)
        return (
            not self.stopping
            and settings.enabled
            and task is not None
            and task.enabled
            and (target is None or target in task.targets)
            and not await self.store.call("get", "paused:" + key, False)
        )

    async def start(self):
        manager = self.context.cron_manager
        # Only remove jobs explicitly owned by this plugin. This also handles an unclean exit.
        for job in await manager.list_jobs(job_type="basic"):
            payload = getattr(job, "payload", None)
            if isinstance(payload, dict) and payload.get("owner") == OWNER:
                await manager.delete_job(job.job_id)
        if self.settings().enabled:
            for task in self.settings().tasks:
                if not task.enabled:
                    continue
                await self.store.call("baseline", task.key, latest_boundary(task, self.clock()))
                for stamp in task.times:
                    hour, minute = map(int, stamp.split(":"))
                    job = await manager.add_basic_job(
                        name=f"群聊摘要 · {task.label} · {stamp}",
                        cron_expression=f"{minute} {hour} * * *",
                        handler=self.trigger,
                        timezone=task.timezone,
                        persistent=False,
                        payload={"owner": OWNER, "task_key": task.key},
                    )
                    self.cron_ids.append(job.job_id)
        self.loop_task = asyncio.create_task(self.loop(), name="qq-digest-scheduler")

    async def trigger(self, owner=None, task_key=None):
        self.wake.set()

    async def stop(self):
        cleanup = asyncio.create_task(self._stop())
        cancelled = False
        while True:
            try:
                await asyncio.shield(cleanup)
                break
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError

    async def _stop(self):
        self.stopping = True
        self.wake.set()
        for jid in self.cron_ids:
            try:
                await self.context.cron_manager.delete_job(jid)
            except Exception as exc:
                self.journal.record("定时任务清理失败", error_type=type(exc).__name__)
        self.cron_ids.clear()
        tasks = [
            t
            for t in [self.loop_task, *self.workers.values(), *self.commands]
            if t is not None and t is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.workers.clear()
        self.loop_task = None

    async def ensure_window(self, task, *, manual=False):
        now = self.clock()
        end = latest_boundary(task, now)
        cursor = await self.store.call("get", "cursor:" + task.key)
        if cursor is None:
            await self.store.call("baseline", task.key, end)
            cursor = {"end": end, "first": True}
            if not manual:
                return None
        if end <= cursor["end"]:
            recent = await self.store.call("latest", task.key, 1)
            if not (manual and cursor["first"] and not recent):
                return recent[0]["id"] if recent else None
        notes = []
        if cursor["first"]:
            start = end - task.initial_hours * 3600
            read_start = start
        else:
            oldest = await self.store.call("oldest_queued_start", task.key)
            requested = min(cursor["end"], oldest) if oldest is not None else cursor["end"]
            start = max(requested, end - task.catchup_hours * 3600)
            if start > requested:
                notes.append(f"本期为恢复后的补报，仅覆盖最近 {task.catchup_hours} 小时。")
            read_start = start - task.overlap_minutes * 60
        rid = await self.store.call("create_run", task.key, start, end, read_start, task.dump(), notes, now)
        self.journal.record(
            "摘要批次创建", run=rid, group=task.source_group, start=start, end=end, read_start=read_start
        )
        return rid

    async def loop(self):
        while not self.stopping:
            try:
                self.wake.clear()
                for task in self.settings().tasks:
                    if await self.allowed(task.key):
                        worker = self.workers.get(task.key)
                        if worker is None or worker.done():
                            self.workers[task.key] = asyncio.create_task(
                                self.step(task), name="qq-digest-group"
                            )
                if self.clock() - self.last_cleanup >= 3600:
                    limits = self.settings().limits
                    await self.store.call("cleanup", self.clock(), limits.snapshot_days, limits.result_days)
                    self.last_cleanup = self.clock()
            except Exception as exc:
                self.journal.record("调度异常", error_type=type(exc).__name__)
            try:
                await asyncio.wait_for(self.wake.wait(), 30)
            except asyncio.TimeoutError:
                pass

    async def step(self, current):
        async with self.lock(current.key):
            try:
                await self.ensure_window(current)
                runs = await self.store.call("active", current.key, self.clock())
                for run in runs:
                    if not await self.allowed(current.key):
                        break
                    try:
                        task = Task.restore(run["config"])
                        if (
                            run["status"] in {"queued", "fetched"}
                            and self.clock() - run["end"] > task.catchup_hours * 3600
                        ):
                            await self.store.call("expire_run", run["id"])
                            continue
                        adapter = await self.router.resolve(task)
                        await self.store.call("bind", run["id"], adapter.account, adapter.pid)
                        run = await self.store.call("run", run["id"])
                        if run["status"] in {"queued", "fetched"}:
                            await self.build(run, task, adapter)
                            run = await self.store.call("run", run["id"])
                        if run["status"] == "generated":
                            for target in {
                                r["target"] for r in await self.store.call("deliveries", run["id"])
                            }:
                                live = self.current(current.key)
                                if live is not None and target not in live.targets:
                                    await self.store.call("skip", run["id"], target)
                                    self.journal.record("目标已移除", run=run["id"], target=target)
                            await self.delivery.process(run)
                    except Deferred as exc:
                        await self.store.call(
                            "fail",
                            run["id"],
                            str(exc),
                            self.clock() + exc.seconds,
                            self.settings().limits.max_attempts,
                            False,
                        )
                    except Exception as exc:
                        safe = str(exc) if isinstance(exc, DigestError) else "处理未完成，请检查插件日志。"
                        delay = min(1800, 60 * 2 ** min(run["failures"], 5)) + random.uniform(5, 30)
                        await self.store.call(
                            "fail", run["id"], safe, self.clock() + delay, self.settings().limits.max_attempts
                        )
                        self.journal.record(
                            "摘要处理失败", run=run["id"], reason=safe, error_type=type(exc).__name__
                        )
            except Exception as exc:
                self.journal.record("群任务异常", group=current.source_group, error_type=type(exc).__name__)

    async def build(self, run, task, adapter):
        limits = self.settings().limits
        if run["snapshot"] is None:
            messages, notes = await HistoryReader(limits, self.journal).read(
                adapter, task, run["read_start"], run["end"]
            )
            notes = list(dict.fromkeys(run["notes"] + notes))
            await self.store.call("fetched", run["id"], [m.dump() for m in messages], notes)
        else:
            messages, notes = [Message(**m) for m in run["snapshot"]], run["notes"]
        previous = await self.store.call("previous_items", task.key, run["start"] + 1)
        digest = await Summarizer(self.client, limits, run_id=run["id"]).summarize(
            task, adapter, messages, run["start"], run["end"], notes, previous
        )
        payloads = make_payloads(task, digest, run["start"], run["end"], adapter.account, limits)
        deliveries = [
            {"target": target, "part": i, "mode": task.mode, "payload": payload}
            for target in task.targets
            for i, payload in enumerate(payloads)
        ]
        await self.store.call("generated", run["id"], digest.dump(), deliveries)
        self.journal.record(
            "摘要生成完成", run=run["id"], topics=len(digest.items), deliveries=len(deliveries)
        )

    async def preview(self, task):
        async with self.lock(task.key):
            until = await self.store.call("get", "preview:" + task.key, 0)
            if until > self.clock():
                raise DigestError("刚预览过这个群，请至少间隔 60 秒再试。")
            await self.store.call("set", "preview:" + task.key, self.clock() + 60)
            end = int(self.clock())
            start = end - task.initial_hours * 3600
            adapter = await self.router.resolve(task)
            messages, notes = await HistoryReader(self.settings().limits, self.journal).read(
                adapter, task, start, end
            )
            digest = await Summarizer(self.client, self.settings().limits).summarize(
                task, adapter, messages, start, end, notes
            )
            return digest, start, end
