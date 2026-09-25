"""OneBot transport: explicit account routing and bounded reads."""

from __future__ import annotations

import asyncio
import random
import time

from .config import Deferred, DigestError


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

    async def check_connection(self):
        clients = getattr(self.bot, "_wsr_api_clients", None)
        if isinstance(clients, dict):
            if len(clients) > 1:
                raise DigestError("同一接入连接了多个 QQ，请为每个 QQ 使用独立的 AstrBot 接入。")
            if not clients:
                raise Deferred("NapCat 尚未连接，等待连接恢复。")
            current = tuple(sorted((str(k), id(v)) for k, v in clients.items()))
            account = current[0][0]
            if self.account and self.account != account:
                raise DigestError("这个接入的机器人 QQ 已改变，请检查绑定并重载插件。")
            self.account = account
            if current != self._connections:
                self._connections = current
                self._objects = tuple(clients.values())
                self.generation += 1
                await self.cooldown(random.uniform(*self.settings().pace.interval("recovery")))

    async def cooldown(self, seconds):
        return await self.store.call("extend", "recovery:" + self.pid, self.clock() + seconds)

    async def ready_to_send(self):
        await self.check_connection()
        until = await self.store.call("get", "recovery:" + self.pid, 0)
        if until > self.clock():
            raise Deferred("连接恢复后的等待尚未结束。", until - self.clock())

    async def transport(self, action, **params):
        await self.check_connection()
        if action in {"send_group_msg", "send_group_forward_msg"}:
            await self.ready_to_send()
        if self.account:
            params["self_id"] = self.account
        result = await asyncio.wait_for(
            self.bot.call_action(action=action, **params), self.settings().limits.api_timeout_seconds
        )
        # aiocqhttp normally unwraps data; support a full envelope in compatible transports too.
        if isinstance(result, dict) and "retcode" in result and "status" in result:
            if result["retcode"] != 0 or result["status"] != "ok":
                raise DigestError(f"{action} 接口返回失败。")
            result = result.get("data")
        return result

    async def read(self, action, **params):
        async with self.read_lock:
            await self.check_connection()
            pace = self.settings().pace
            due = await self.store.call("get", "read-next:" + self.pid, 0)
            await self.sleep(max(0, due - self.clock()))
            await self.store.call(
                "reserve_budget", "read", self.account or self.pid, self.clock(), 3600, pace.reads_per_hour
            )
            await self.store.call(
                "set", "read-next:" + self.pid, self.clock() + random.uniform(*pace.interval("read"))
            )
            try:
                result = await self.transport(action, **params)
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
                    raise Deferred("QQ 接口暂时不可用，已进入冷却。", pace.failure_cooldown_seconds) from exc
                raise DigestError(f"{action} 查询失败；请检查 NapCat 状态和群访问权限。") from exc
            return result

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

    async def history_page(self, group, count, cursor=None):
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
        data = await self.read("get_group_msg_history", **params)
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            raise DigestError("历史接口没有返回有效的消息列表。")
        return data["messages"]


class Router:
    def __init__(self, context, store, settings, journal):
        self.context, self.store, self.settings, self.journal = context, store, settings, journal
        self.adapters = {}
        self.lock = asyncio.Lock()

    async def resolve(self, task):
        async with self.lock:
            manager = self.context.platform_manager
            candidates = []
            for platform in manager.get_insts():
                meta = platform.meta()
                if meta.name != "aiocqhttp" or not hasattr(getattr(platform, "bot", None), "call_action"):
                    continue
                clients = getattr(platform.bot, "_wsr_api_clients", None)
                if (
                    task.bot_qq
                    and isinstance(clients, dict)
                    and clients
                    and task.bot_qq not in map(str, clients)
                ):
                    continue
                pid = str(meta.id)
                adapter = self.adapters.get(pid)
                if adapter is None or adapter.bot is not platform.bot:
                    adapter = self.adapters[pid] = Adapter(
                        pid, platform.bot, self.store, self.settings, self.journal
                    )
                candidates.append(adapter)
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
