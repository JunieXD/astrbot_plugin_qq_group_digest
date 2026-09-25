"""Private preview presentation using the same payloads and pacing as publication."""

import asyncio
import math
from dataclasses import dataclass

from .config import Deferred, DigestError, Task, identifier
from .models import Digest
from .platform import RecoveryPending
from .render import make_payloads


@dataclass(frozen=True)
class Preview:
    task: Task
    digest: Digest
    start: int
    end: int


async def send_preview(service, event, preview, *, notify=None, sleep=asyncio.sleep):
    if not event.is_admin() or event.get_group_id():
        raise DigestError("预览只能返回管理员私聊。")
    recipient = identifier(event.get_sender_id(), "预览接收者")
    adapter = await service.router.for_event(event)
    task, settings = preview.task, service.settings()
    mode = task.mode
    if mode.startswith("合并转发") and not await adapter.packet_ready():
        if not task.fallback_to_plain:
            raise DigestError("合并转发能力暂不可用，本次预览未发送；请检查 NapCat 状态后重试。")
        mode = "普通消息·整篇"
        service.journal.record("预览按配置改用普通消息", group=task.source_group)
    payloads = make_payloads(
        task, preview.digest, preview.start, preview.end, adapter.account, settings.limits, mode
    )
    for index, payload in enumerate(payloads, 1):
        submitted = False
        recovery = None
        waits = 0

        async def action():
            nonlocal submitted, recovery
            try:
                if service.stopping:
                    raise Deferred("插件正在停止。")
                if await service.router.for_event(event) is not adapter:
                    raise Deferred("私聊接入发生变化，请稍后重试。")
                await adapter.ready_to_send()
                if mode.startswith("合并转发") and not await adapter.packet_ready():
                    raise Deferred("合并转发能力发生变化，本次预览暂停发送。")
                await service.store.call(
                    "reserve_budget",
                    "send",
                    adapter.account,
                    service.clock(),
                    86400,
                    settings.pace.sends_per_day,
                )
            except RecoveryPending as exc:
                recovery = exc
                raise service.guard.deferred_error(str(exc)) from exc
            except DigestError as exc:
                raise service.guard.deferred_error(str(exc)) from exc
            action_name = "send_private_forward_msg" if mode.startswith("合并转发") else "send_private_msg"
            submitted = True
            result = await adapter.transport(action_name, user_id=recipient, **payload)
            mid = result.get("message_id") if isinstance(result, dict) else None
            if mid is None or isinstance(mid, bool) or not str(mid).lstrip("-").isdigit():
                raise DigestError("发送接口没有返回有效消息 ID。")
            service.journal.record(
                "私聊预览发送成功",
                group=task.source_group,
                recipient=recipient,
                mode=mode,
                part=index,
                message_id=str(mid),
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
        if getattr(service.guard, "scheduling_version", 1) >= 3:
            args.update(priority=2, group="private:" + recipient, label="群聊摘要私聊预览")
        while True:
            recovery = None
            try:
                if service.stopping:
                    raise Deferred("插件正在停止。")
                # Wait outside the shared account queue. Revalidate again inside
                # action(), since the socket can change while the queue waits.
                await adapter.ready_to_send()
                await service.guard.run(**args)
                break
            except BaseException as exc:
                pending = exc if isinstance(exc, RecoveryPending) else recovery
                if isinstance(exc, Exception) and pending and not submitted and waits < 3:
                    waits += 1
                    seconds = math.ceil(pending.seconds)
                    service.journal.record(
                        "私聊预览等待连接保护", group=task.source_group, part=index, seconds=seconds
                    )
                    if notify:
                        await notify(
                            f"摘要已生成，发送保护还需约 {seconds} 秒；结束后自动发送，无需重新预览。"
                        )
                    await sleep(seconds)
                    continue
                service.journal.record(
                    "私聊预览发送未完成",
                    group=task.source_group,
                    part=index,
                    submitted=submitted,
                    error_type=type(exc).__name__,
                )
                if not isinstance(exc, Exception):
                    raise
                if submitted:
                    raise DigestError(
                        f"预览第 {index} 条发送结果未确认，已停止后续发送且不会自动重发；请先检查私聊消息。"
                    ) from exc
                if isinstance(exc, (DigestError, service.guard.deferred_error)):
                    raise DigestError(f"预览第 {index} 条尚未发送：{exc}") from exc
                raise DigestError(f"预览第 {index} 条发送准备失败，请检查插件日志。") from exc
