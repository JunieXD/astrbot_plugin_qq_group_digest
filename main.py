"""AstrBot registration only; business logic is tested without a running bot."""

from __future__ import annotations

import asyncio
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star, StarTools, register

from .qq_group_digest import __version__
from .qq_group_digest.commands import Commands
from .qq_group_digest.config import DigestError, parse_settings
from .qq_group_digest.llm_stats import UsageStore
from .qq_group_digest.pacing import get_guard
from .qq_group_digest.platform import Router
from .qq_group_digest.preview import Preview, send_preview
from .qq_group_digest.resources import InstanceLock, Journal
from .qq_group_digest.service import Service
from .qq_group_digest.store import Store
from .qq_group_digest.summarizer import LLMClient


@register("astrbot_plugin_qq_group_digest", "JunieXD", "定时提炼 QQ 群聊并可靠投递摘要", __version__)
class QQGroupDigest(Star):
    def __init__(self, context: Context, config=None):
        super().__init__(context=context, config=config)
        self.raw_config = config if config is not None else {}
        self.service = self.store = self.journal = self.lock = self.statistics = None
        self.start_error = "插件尚未初始化。"

    def settings(self):
        return parse_settings(dict(self.raw_config))

    async def initialize(self):
        try:
            settings = self.settings()
            root = StarTools.get_data_dir("astrbot_plugin_qq_group_digest")
            root.mkdir(parents=True, exist_ok=True)
            self.lock = InstanceLock(root / "instance.lock")
            self.journal = Journal(root, settings.limits)
            self.store = Store(root / "state.sqlite3")
            await self.store.call("open_db")
            router = Router(self.context, self.store, self.settings, self.journal)
            guard = get_guard(self.context, root.parent)
            try:
                self.statistics = UsageStore(root / "llm_usage.sqlite3", settings.llm_statistics)
            except Exception as exc:
                logger.warning("摘要 LLM 统计初始化失败：%s", type(exc).__name__)
            client = LLMClient(self.context, self.store, self.settings, self.journal, self.statistics)
            self.service = Service(
                self.context, self.settings, self.store, router, guard, client, self.journal
            )
            await self.service.start()
            if hasattr(self.context, "register_web_api"):
                self.context.register_web_api(
                    "/qq_group_digest/preview", self.api_preview, ["POST"], "管理员群摘要预览"
                )
            self.start_error = ""
            logger.info(
                f"QQ 群聊摘要 v{__version__} 已加载；在配置中添加任务，私聊 /群摘要 预览 群号 后启用。"
            )
        except BaseException as exc:
            if self.journal:
                self.journal.record("初始化失败", error_type=type(exc).__name__)
            self.start_error = (
                str(exc) if isinstance(exc, DigestError) else "初始化失败，请检查数据目录及 AstrBot 版本。"
            )
            logger.error("QQ 群聊摘要：%s [%s]", self.start_error, type(exc).__name__)
            await self.terminate()
            if not isinstance(exc, Exception):
                raise

    async def terminate(self):
        try:
            if self.service:
                await self.service.stop()
        finally:
            try:
                if self.statistics:
                    self.statistics.close()
                    self.statistics = None
                if self.store:
                    await self.store.close()
            finally:
                try:
                    if self.journal:
                        self.journal.close()
                finally:
                    try:
                        if self.lock:
                            self.lock.close()
                    finally:
                        self.service = self.store = self.journal = self.lock = None

    @filter.command("群摘要", alias={"qgdigest"})
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def digest_command(self, event):
        event.stop_event()
        if not event.is_admin():
            return
        service = self.service
        if service is None or service.stopping:
            await event.send(event.plain_result(self.start_error or "插件正在停止，请稍后重试。"))
            return
        current = asyncio.current_task()
        service.commands.add(current)

        async def reply(text):
            # stop_event also blocks yielded results in current AstrBot versions.
            await asyncio.wait_for(
                event.send(event.plain_result(text)), service.settings().limits.api_timeout_seconds
            )

        try:
            try:
                result = await Commands(service, notify=reply).run(event.get_message_str())
                if isinstance(result, Preview):
                    async with service.lock(result.task.key):
                        await send_preview(service, event, result, notify=reply)
                    return
            except DigestError as exc:
                result = str(exc)
            except Exception as exc:
                service.journal.record("管理员命令失败", error_type=type(exc).__name__)
                result = "操作未完成，请查看插件日志。"
            # Bound the reply as well; long status output is inspected per source group.
            if isinstance(result, Path):
                from astrbot.api.message_components import File

                await asyncio.wait_for(
                    event.send(event.chain_result([File(name=result.name, file=str(result.resolve()))])),
                    service.settings().limits.api_timeout_seconds,
                )
                return
            if len(result) > 3800:
                result = result[:3750] + "\n内容较长，请按来源群查看状态，或减少预览摘要长度。"
            await reply(result)
        finally:
            service.commands.discard(current)

    async def api_preview(self):
        """AstrBot authenticates plugin extensions; the endpoint only returns a preview."""
        from astrbot.api.web import request

        from .qq_group_digest.config import identifier
        from .qq_group_digest.render import full_text, make_payloads

        if not request.username:
            return {"status": "error", "message": "需要管理员身份。"}
        service = self.service
        if service is None or service.stopping:
            return {"status": "error", "message": "插件尚未就绪。"}
        current = asyncio.current_task()
        service.commands.add(current)
        try:
            body = await request.json(default={})
            if not isinstance(body, dict):
                raise DigestError("请求应为 JSON 对象。")
            task = service.settings().find(identifier(body.get("group_id"), "来源群"))
            if service.lock(task.key).locked():
                raise DigestError("这个群正在处理，请稍后再试。")
            digest, start, end = await service.preview(task, refresh=body.get("refresh") is True)
            adapter = await service.router.resolve(task)
            return {
                "status": "ok",
                "data": {
                    "digest": digest.dump(),
                    "start": start,
                    "end": end,
                    "text": full_text(task, digest, start, end),
                    "payloads": make_payloads(
                        task, digest, start, end, adapter.account, service.settings().limits
                    ),
                },
            }
        except DigestError as exc:
            return {"status": "error", "message": str(exc)}
        finally:
            service.commands.discard(current)
