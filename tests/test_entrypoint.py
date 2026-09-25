"""Exercise entrypoint cleanup and authorization without starting the AstrBot server."""

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from qq_group_digest.config import MODES


@pytest.fixture
def entrypoint(monkeypatch):
    def decorator(*args, **kwargs):
        return lambda handler: handler

    modules = {
        name: ModuleType(name) for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star")
    }
    modules["astrbot.api"].logger = SimpleNamespace()
    modules["astrbot.api.event"].filter = SimpleNamespace(
        command=decorator,
        event_message_type=decorator,
        platform_adapter_type=decorator,
        EventMessageType=SimpleNamespace(PRIVATE_MESSAGE=1),
        PlatformAdapterType=SimpleNamespace(AIOCQHTTP=1),
    )
    star = modules["astrbot.api.star"]
    star.Context = star.Star = star.StarTools = object
    star.register = decorator
    package = ModuleType("entrypoint_test")
    package.__path__ = [str(Path.cwd())]
    modules["entrypoint_test"] = package
    import qq_group_digest
    import qq_group_digest.preview

    modules["entrypoint_test.qq_group_digest"] = qq_group_digest
    for name, module in list(sys.modules.items()):
        if name.startswith("qq_group_digest."):
            modules["entrypoint_test." + name] = module
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("entrypoint_test.main", Path("main.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.QQGroupDigest.__new__(module.QQGroupDigest)


async def test_unprivileged_command_does_not_create_automatic_reply(entrypoint):
    class Event:
        def stop_event(self):
            self.stopped = True

        def is_admin(self):
            return False

        def plain_result(self, text):
            raise AssertionError("unauthorized commands must not trigger reply spam")

    event = Event()
    await entrypoint.digest_command(event)
    assert event.stopped


class PrivateEvent:
    def __init__(self, text):
        self.text, self.sent, self.stopped = text, [], False

    def stop_event(self):
        self.stopped = True

    def is_admin(self):
        return True

    def get_message_str(self):
        return self.text

    def get_group_id(self):
        return ""

    def get_sender_id(self):
        return "222222222"

    def plain_result(self, text):
        return text

    async def send(self, text):
        # Current AstrBot still permits direct sends after force-stopping an event;
        # its scheduler discards yielded responses in that state.
        assert self.stopped
        self.sent.append(text)


async def test_stopped_admin_event_sends_help_directly(entrypoint, store, settings, journal):
    from .test_service import make_service

    entrypoint.service, _ = make_service(store, settings, journal)
    event = PrivateEvent("/群摘要")
    await entrypoint.digest_command(event)
    assert len(event.sent) == 1 and "预览" in event.sent[0]
    assert not entrypoint.service.commands


async def test_startup_failure_is_sent_after_stopping_event(entrypoint):
    entrypoint.service = None
    entrypoint.start_error = "初始化失败"
    event = PrivateEvent("/群摘要 状态")
    await entrypoint.digest_command(event)
    assert event.sent == ["初始化失败"]


@pytest.mark.parametrize("fail_model", [False, True])
@pytest.mark.parametrize("mode", MODES)
async def test_preview_ack_progress_and_result_survive_stopped_event(
    entrypoint, store, task, settings, journal, fail_model, mode
):
    from qq_group_digest.commands import Commands
    from qq_group_digest.config import DigestError

    from .conftest import NOW, raw_message
    from .test_service import make_service
    from .test_summarizer_render import output

    task = replace(task, mode=mode, card_title="✨ 保研情报站", content_title="今日保研精选")
    settings = replace(settings, enabled=False, tasks=(task,))
    service, api = make_service(store, settings, journal)
    entrypoint.service = service
    event = PrivateEvent("/群摘要 预览 " + task.source_group)
    api.history = [
        raw_message(10, NOW - 1),
        raw_message(1, NOW - task.initial_hours * 3600 - task.overlap_minutes * 60 - 1),
    ]

    class Client:
        async def generate(self, task, adapter, prompt, **kwargs):
            assert len(event.sent) == 1 and "已收到" in event.sent[0]
            status = await Commands(service).run("群摘要 状态 " + task.source_group)
            assert "模型生成中" in status and "已查询 1 页" in status and "获得 1 条" in status
            if fail_model:
                raise DigestError("模型暂时不可用")
            return output(prompt=prompt)

    service.client = Client()
    await entrypoint.digest_command(event)
    if fail_model:
        assert len(event.sent) == 2 and "模型暂时不可用" in event.sent[-1]
        assert not api.private_sent
    else:
        assert len(event.sent) == 1 and len(api.private_sent) == 1
        action, payload = api.private_sent[0]
        assert payload["user_id"] == event.get_sender_id() and "group_id" not in payload
        if mode.startswith("合并转发"):
            assert action == "send_private_forward_msg"
            assert payload["source"] == "✨ 保研情报站" and payload["summary"] == "共 1 条 · 点开查看"
            assert payload["prompt"] == "[✨ 保研情报站]"
            assert len(payload["messages"]) == (1 if mode.endswith("整篇") else 2)
            assert payload["messages"][0]["data"]["content"][0]["data"]["text"].startswith("今日保研精选\n\n")
        else:
            assert action == "send_private_msg"
            assert "截止时间为明天" in payload["message"][0]["data"]["text"]
            assert payload["message"][0]["data"]["text"].startswith("今日保研精选\n\n")
        assert await store.call("get", "cursor:" + task.key) is None
        assert not await store.call("latest", task.key)
    assert not service.previews and not service.commands
    assert not api.sent  # No group publication from a private preview.
    event = PrivateEvent("/群摘要 预览 " + task.source_group)
    await entrypoint.digest_command(event)
    assert len(event.sent) == 1 and "至少间隔" in event.sent[0]


async def test_failed_log_close_still_releases_instance_lock(entrypoint):
    closed = []

    class Journal:
        def close(self):
            raise OSError("disk error")

    entrypoint.service = entrypoint.store = None
    entrypoint.journal = Journal()
    entrypoint.lock = SimpleNamespace(close=lambda: closed.append(True))
    with pytest.raises(OSError):
        await entrypoint.terminate()
    assert closed == [True]
    assert entrypoint.lock is None


async def test_authenticated_web_preview_uses_real_service_without_sending(
    entrypoint, monkeypatch, store, task, settings, journal
):
    from .conftest import NOW, raw_message
    from .test_service import make_service
    from .test_summarizer_render import output

    web = ModuleType("astrbot.api.web")

    async def body(**kwargs):
        return {"group_id": task.source_group}

    web.request = SimpleNamespace(username=None, json=body)
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web)
    assert (await entrypoint.api_preview())["status"] == "error"
    service, api = make_service(store, settings, journal)
    entrypoint.service = service
    api.history = [
        raw_message(10, NOW - 1),
        raw_message(1, NOW - task.initial_hours * 3600 - task.overlap_minutes * 60 - 1),
    ]

    async def generate(*args, **kwargs):
        return output()

    service.client = SimpleNamespace(generate=generate)
    web.request.username = "admin"
    result = await entrypoint.api_preview()
    assert result["status"] == "ok" and result["data"]["digest"]["items"]
    assert result["data"]["payloads"] and not api.sent
    assert not service.commands and not service.previews
