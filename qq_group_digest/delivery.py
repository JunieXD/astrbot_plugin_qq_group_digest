"""Durable per-destination sends. Submitted requests are never blindly retried."""

from __future__ import annotations

import time

from .config import Deferred, DigestError, Task
from .history import normalize, segments
from .models import Digest
from .render import make_payloads, payload_fingerprint


class Delivery:
    def __init__(self, store, router, guard, settings, journal, allowed, *, clock=time.time):
        self.store, self.router, self.guard = store, router, guard
        self.settings, self.journal, self.allowed, self.clock = settings, journal, allowed, clock

    async def process(self, run):
        task = Task.restore(run["config"])
        if self.clock() - run["end"] > task.catchup_hours * 3600:
            await self.store.call("expire_pending", run["id"])
            return
        adapter = await self.router.resolve(task)
        if adapter.account != run["account"]:
            raise DigestError("机器人身份变化，本批次停止投递。")
        rows = await self.store.call("deliveries", run["id"])
        targets = list(dict.fromkeys(r["target"] for r in rows))
        for target in targets:
            group_rows = [r for r in rows if r["target"] == target]
            if not await self.allowed(task.key, target):
                continue
            # Keep order within a target. Other destinations may proceed independently.
            if any(r["state"] in {"submitted", "unknown", "blocked"} for r in group_rows):
                continue
            pending = [r for r in group_rows if r["state"] == "pending"]
            if not pending or pending[0]["next_try"] > self.clock():
                continue
            first = pending[0]
            try:
                await adapter.ready_to_send()
                if first["mode"].startswith("合并转发") and not await adapter.packet_ready():
                    if not task.fallback_to_plain:
                        raise DigestError("合并转发能力暂不可用，已暂缓；可配置改发普通整篇。")
                    payload = make_payloads(
                        task,
                        Digest.restore(run["digest"]),
                        run["start"],
                        run["end"],
                        adapter.account,
                        self.settings().limits,
                        "普通消息·整篇",
                    )[0]
                    await self.store.call(
                        "replace_pending_target", run["id"], target, "普通消息·整篇", payload
                    )
                    pending = [
                        r
                        for r in await self.store.call("deliveries", run["id"])
                        if r["target"] == target and r["state"] == "pending"
                    ]
                for row in pending:
                    if not await self.send_one(run, task, adapter, row):
                        break
            except Deferred as exc:
                await self.store.call(
                    "defer_delivery",
                    first["id"],
                    str(exc),
                    self.clock() + exc.seconds,
                    self.settings().limits.max_attempts,
                    False,
                )
            except DigestError as exc:
                await self.store.call(
                    "defer_delivery",
                    first["id"],
                    str(exc),
                    self.clock() + 300,
                    self.settings().limits.max_attempts,
                )
        await self.store.call("finish", run["id"])

    async def send_one(self, run, task, adapter, row):
        submitted = False
        settings = self.settings()

        async def action():
            nonlocal submitted
            try:
                if not await self.allowed(task.key, row["target"]):
                    raise Deferred("任务已暂停或目标群已移除。")
                fresh = await self.router.resolve(task)
                if fresh is not adapter or fresh.account != run["account"]:
                    raise Deferred("接入发生变化，稍后重新绑定。")
                await adapter.ready_to_send()
                if row["mode"].startswith("合并转发") and not await adapter.packet_ready():
                    raise Deferred("合并转发能力发生变化，稍后再检查。", 300)
                # Check access and speaking restrictions immediately before reserving the write.
                group = await adapter.read("get_group_info", group_id=row["target"])
                member = await adapter.read(
                    "get_group_member_info", group_id=row["target"], user_id=adapter.account
                )
                if not isinstance(group, dict) or not isinstance(member, dict):
                    raise Deferred("目标群资料不完整，暂缓发送。", 300)
                if (
                    str(group.get("group_id")) != row["target"]
                    or str(member.get("user_id")) != adapter.account
                ):
                    raise DigestError("目标群或机器人资料不匹配。")
                if group.get("group_all_shut") and member.get("role") not in {"owner", "admin"}:
                    raise Deferred("目标群正在全员禁言。", 600)
                if int(member.get("shut_up_timestamp") or 0) > self.clock():
                    raise Deferred("机器人在目标群中仍被禁言。", 600)
                if not await self.allowed(task.key, row["target"]):
                    raise Deferred("任务已暂停或目标群已移除。")
                submitted = await self.store.call(
                    "submit", row["id"], adapter.account, self.clock(), self.settings().pace.sends_per_day
                )
                if not submitted:
                    raise Deferred("这一分段已由其他操作处理。")
            except Deferred as exc:
                # The shared guard must know that no write has been attempted.
                raise self.guard.deferred_error(str(exc)) from exc
            action_name = "send_group_forward_msg" if row["mode"].startswith("合并转发") else "send_group_msg"
            result = await adapter.transport(action_name, group_id=row["target"], **row["payload"])
            mid = result.get("message_id") if isinstance(result, dict) else None
            if mid is None or isinstance(mid, bool) or not str(mid).lstrip("-").isdigit():
                raise DigestError("发送接口没有返回有效消息 ID。")
            await self.store.call("delivery_result", row["id"], "sent", str(mid))
            self.journal.record(
                "摘要发送成功", run=run["id"], target=row["target"], part=row["part"], message_id=str(mid)
            )

        args = dict(
            account=adapter.pid,
            online=adapter.online,
            action=action,
            config=settings.pace.guard_config(),
            delay=settings.pace.interval("send"),
            gap=settings.pace.interval("gap"),
            key=None,
        )
        if getattr(self.guard, "scheduling_version", 1) >= 3:
            args.update(priority=2, group=row["target"], label="群聊摘要")
        try:
            await self.guard.run(**args)
            return submitted
        except BaseException as exc:
            # The DB row is authoritative even if cancellation occurred immediately after its commit.
            latest = next(r for r in await self.store.call("deliveries", run["id"]) if r["id"] == row["id"])
            if latest["state"] == "submitted":
                await self.store.call(
                    "delivery_result", row["id"], "unknown", "", "发送结果不明，请核对后处理。"
                )
                self.journal.record(
                    "摘要发送结果不明", run=run["id"], target=row["target"], error_type=type(exc).__name__
                )
            elif latest["state"] == "pending":
                safe = (
                    str(exc)
                    if isinstance(exc, (DigestError, self.guard.deferred_error))
                    else "投递准备未完成。"
                )
                delay = getattr(exc.__cause__, "seconds", 300)
                await self.store.call(
                    "defer_delivery",
                    row["id"],
                    safe,
                    self.clock() + delay,
                    settings.limits.max_attempts,
                    not isinstance(exc, (Deferred, self.guard.deferred_error)),
                )
            if not isinstance(exc, Exception):
                raise
            return False

    async def reconcile(self, run):
        task = Task.restore(run["config"])
        adapter = await self.router.resolve(task)
        if adapter.account != run["account"]:
            raise DigestError("机器人身份不同，无法核对这份摘要。")
        rows = await self.store.call("deliveries", run["id"])
        outcomes = []
        for target in dict.fromkeys(r["target"] for r in rows if r["state"] == "unknown"):
            unknown = [r for r in rows if r["target"] == target and r["state"] == "unknown"]
            raw_messages = []
            cursor, anchors = None, set()
            for _ in range(3):
                page = await adapter.history_page(target, 20, cursor)
                if not page:
                    break
                raw_messages.extend(page)
                normalized = [normalize(m, target) for m in page]
                oldest = min(normalized, key=lambda m: (m.time, m.seq, m.message_id))
                if oldest.time < min(r["submitted"] for r in unknown) - 10 or oldest.message_id in anchors:
                    break
                cursor = oldest.message_id
                anchors.add(cursor)
            # At most five merged messages are fetched during one manual reconciliation.
            forward_cache, forward_reads = {}, 0
            for row in unknown:
                matches = set()
                expected = payload_fingerprint(row["payload"])
                for raw in raw_messages:
                    message = normalize(raw, target)
                    if message.sender != adapter.account or message.time < row["submitted"] - 10:
                        continue
                    candidate = {"message": segments(raw.get("message", []))}
                    if row["mode"].startswith("合并转发"):
                        for fid in message.forward_ids:
                            if fid not in forward_cache and forward_reads < 5:
                                forward_reads += 1
                                data = await adapter.read("get_forward_msg", message_id=fid)
                                forward_cache[fid] = (
                                    data.get("messages", []) if isinstance(data, dict) else []
                                )
                            nodes = forward_cache.get(fid, [])
                            payload = {
                                "messages": [
                                    {"data": {"content": segments(n.get("message", n.get("content", [])))}}
                                    for n in nodes
                                ]
                            }
                            if payload_fingerprint(payload) == expected:
                                matches.add(message.message_id)
                    elif payload_fingerprint(candidate) == expected:
                        matches.add(message.message_id)
                if len(matches) == 1:
                    await self.store.call("delivery_result", row["id"], "sent", matches.pop(), "历史核对匹配")
                    outcomes.append(f"群 {target} 第 {row['part'] + 1} 条：已找到匹配消息。")
                else:
                    outcomes.append(f"群 {target} 第 {row['part'] + 1} 条：无法唯一确认，保留待核对状态。")
        await self.store.call("finish", run["id"])
        return "\n".join(outcomes) or "这份摘要没有待核对的发送。"
