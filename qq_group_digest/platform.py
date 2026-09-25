"""OneBot transport: explicit account routing and bounded reads."""

from __future__ import annotations

import asyncio
import random
import time

from .config import Deferred, DigestError, identifier


class RecoveryPending(Deferred):
    """A known send cooldown; no platform write has been attempted."""


class Adapter:
    def __init__(self, pid, bot, store, settings, journal, *, clock=time.time, sleep=asyncio.sleep):
        self.pid, self.bot = str(pid), bot
        self.store, self.settings, self.journal = store, settings, journal
        self.clock, self.sleep = clock, sleep
        self.account = ""
        self.read_lock = asyncio.Lock()
        self._connections = None
        self._objects = ()
        self.generation = 0
        # The OneBot client outlives a plugin reload. Keep only connection objects
        # here, never old adapters, stores or callbacks; retain references to
        # prevent object-id reuse from hiding a genuine reconnect.
        attribute = "_qq_group_digest_connections_v1"
        self._observed = getattr(bot, attribute, None)
        if not isinstance(self._observed, dict):
            self._observed = {}
            setattr(bot, attribute, self._observed)

    async def check_connection(self):
        clients = getattr(self.bot, "_wsr_api_clients", None)
        if isinstance(clients, dict):
            if len(clients) > 1:
                raise DigestError("同一接入连接了多个 QQ，请为每个 QQ 使用独立的 AstrBot 接入。")
            if not clients:
                self._connections = ()
                self._objects = ()
                self._observed.pop(self.pid, None)
                raise Deferred("NapCat 尚未连接，等待连接恢复。")
            current = tuple(sorted((str(k), id(v)) for k, v in clients.items()))
            account = current[0][0]
            if self.account and self.account != account:
                raise DigestError("这个接入的机器人 QQ 已改变，请检查绑定并重载插件。")
            self.account = account
            if current != self._connections:
                previous = self._observed.get(self.pid)
                self._connections = current
                self._objects = tuple(clients.values())
                self.generation += 1
                if previous is None or previous[0] != current:
                    seconds = random.uniform(*self.settings().pace.interval("recovery"))
                    await self.cooldown(seconds)
                    self.journal.record(
                        "连接保护等待",
                        platform=self.pid,
                        reason="连接变化" if previous else "首次识别连接",
                        seconds=round(seconds, 1),
                    )
                self._observed[self.pid] = (current, self._objects)

    async def cooldown(self, seconds):
        return await self.store.call("extend", "recovery:" + self.pid, self.clock() + seconds)

    async def ready_to_send(self):
        await self.check_connection()
        until = await self.store.call("get", "recovery:" + self.pid, 0)
        if until > self.clock():
            raise RecoveryPending("连接初始化或恢复后的发送保护等待尚未结束。", until - self.clock())

    async def transport(self, action, *, generation=None, **params):
        await self.check_connection()
        if generation is not None and generation != self.generation:
            raise Deferred("读取历史时连接发生变化，将从第一页重新读取。")
        started_generation = self.generation
        if action in {
            "send_group_msg",
            "send_group_forward_msg",
            "send_private_msg",
            "send_private_forward_msg",
        }:
            await self.ready_to_send()
        if self.account:
            params["self_id"] = self.account
        result = await asyncio.wait_for(
            self.bot.call_action(action=action, **params), self.settings().limits.api_timeout_seconds
        )
        if action == "get_group_msg_history":
            await self.check_connection()
            if started_generation != self.generation:
                raise Deferred("读取历史时连接发生变化，将从第一页重新读取。")
        # aiocqhttp normally unwraps data; support a full envelope in compatible transports too.
        if isinstance(result, dict) and "retcode" in result and "status" in result:
            if result["retcode"] != 0 or result["status"] != "ok":
                raise DigestError(f"{action} 接口返回失败。")
            result = result.get("data")
        return result

    async def read(self, action, *, generation=None, **params):
        async with self.read_lock:
            await self.check_connection()
            pace = self.settings().pace
            await self.ready_to_read()
            due = await self.store.call("get", "read-next:" + self.pid, 0)
            await self.sleep(max(0, due - self.clock()))
            await self.ready_to_read()
            await self.store.call(
                "reserve_budget", "read", self.account or self.pid, self.clock(), 3600, pace.reads_per_hour
            )
            await self.store.call(
                "set", "read-next:" + self.pid, self.clock() + random.uniform(*pace.interval("read"))
            )
            try:
                started = time.monotonic()
                result = await self.transport(action, generation=generation, **params)
            except Deferred:
                raise
            except Exception as exc:
                self.journal.record(
                    "接口查询失败", api=action, platform=self.pid, error_type=type(exc).__name__
                )
                if type(exc).__name__ in {
                    "TimeoutError",
                    "NetworkError",
                    "ApiNotAvailable",
                    "ConnectionError",
                }:
                    await self.cooldown(pace.failure_cooldown_seconds)
                    await self.store.call(
                        "extend",
                        "read-cooldown:" + self.pid,
                        self.clock() + pace.failure_cooldown_seconds,
                    )
                    raise Deferred("QQ 接口暂时不可用，已进入冷却。", pace.failure_cooldown_seconds) from exc
                raise DigestError(f"{action} 查询失败；请检查 NapCat 状态和群访问权限。") from exc
            elapsed = time.monotonic() - started
            # Slow successful reads also reduce pressure; normal fast reads keep the configured pace.
            if elapsed > max(5, pace.read_max_seconds * 2):
                await self.store.call("extend", "read-next:" + self.pid, self.clock() + min(30, elapsed / 2))
            self.journal.record("接口查询完成", api=action, duration_ms=round(elapsed * 1000, 1))
            return result

    async def ready_to_read(self):
        until = await self.store.call("get", "read-cooldown:" + self.pid, 0)
        if until > self.clock():
            raise Deferred("QQ 查询接口处于失败冷却期，稍后继续。", until - self.clock())

    async def identity(self):
        await self.check_connection()
        data = await self.read("get_login_info")
        account = str(data.get("user_id", "")) if isinstance(data, dict) else ""
        if not account.isdigit() or int(account) <= 0 or (self.account and self.account != account):
            raise DigestError("无法确认机器人 QQ 身份。")
        self.account = account

    async def online(self):
        await self.check_connection()
        result = await self.read("get_status")
        return isinstance(result, dict) and result.get("online") is True

    async def packet_ready(self):
        # This API returns null on SUCCESS, not a Boolean.
        try:
            await self.read("nc_get_packet_status")
            return True
        except Deferred:
            raise
        except DigestError:
            return False

    async def history_page(self, group, count, cursor=None, *, generation=None):
        params = dict(
            group_id=group,
            count=count,
            reverse_order=True,
            disable_get_url=True,
            parse_mult_msg=False,
            quick_reply=True,
        )
        if cursor is not None:
            params["message_seq"] = str(cursor)
        data = await self.read("get_group_msg_history", generation=generation, **params)
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise DigestError("历史接口没有返回有效的消息列表。")
        return data["messages"]

    async def refresh_forward(self, group, native_id):
        # Native 64-bit identifiers must remain strings (JS Numbers lose precision).
        page = await self.history_page(group, 1, native_id)
        if not page:
            raise DigestError("转发所在的历史消息暂不可用。")


class Router:
    def __init__(self, context, store, settings, journal):
        self.context, self.store, self.settings, self.journal = context, store, settings, journal
        self.adapters = {}
        self.lock = asyncio.Lock()

    def adapter(self, pid, bot):
        adapter = self.adapters.get(pid)
        if adapter is None or adapter.bot is not bot:
            adapter = self.adapters[pid] = Adapter(pid, bot, self.store, self.settings, self.journal)
        return adapter

    async def for_event(self, event):
        """Reply through the requesting account, even when the source uses another bot."""
        async with self.lock:
            bot = getattr(event, "bot", None)
            if not callable(getattr(bot, "call_action", None)):
                raise DigestError("私聊接入无法调用 OneBot 接口。")
            account = identifier(event.get_self_id(), "私聊机器人")
            adapter = self.adapter(str(event.get_platform_id()), bot)
            await adapter.check_connection()
            if not adapter.account:
                await adapter.identity()
            if adapter.account != account:
                raise DigestError("私聊机器人身份发生变化，请重新发送命令。")
            return adapter

    async def resolve(self, task):
        async with self.lock:
            manager = self.context.platform_manager
            candidates = []
            for platform in manager.get_insts():
                meta = platform.meta()
                if meta.name != "aiocqhttp" or not hasattr(getattr(platform, "bot", None), "call_action"):
                    continue
                clients = getattr(platform.bot, "_wsr_api_clients", None)
                if task.bot_qq and isinstance(clients, dict) and task.bot_qq not in map(str, clients):
                    continue
                pid = str(meta.id)
                candidates.append(self.adapter(pid, platform.bot))
            if not candidates:
                raise Deferred("没有找到可用的 QQ 接入。")
            if len(candidates) != 1:
                raise DigestError("无法唯一确定机器人；请在任务的高级设置中填写机器人 QQ，并清理重复接入。")
            adapter = candidates[0]
            await adapter.check_connection()
            if not adapter.account:
                await adapter.identity()
            if task.bot_qq and adapter.account != task.bot_qq:
                raise DigestError("接入的机器人 QQ 与任务配置不符。")
            return adapter
