"""Exercise entrypoint cleanup and authorization without starting the AstrBot server."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


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
async def test_preview_ack_progress_and_result_survive_stopped_event(
    entrypoint, store, task, settings, journal, fail_model
):
    from qq_group_digest.commands import Commands
    from qq_group_digest.config import DigestError

    from .conftest import NOW, raw_message
    from .test_service import make_service
    from .test_summarizer_render import output

    service, api = make_service(store, settings, journal)
    entrypoint.service = service
    event = PrivateEvent("/群摘要 预览 " + task.source_group)
    api.history = [raw_message(10, NOW - 1), raw_message(1, NOW - 86401)]

    class Client:
        async def generate(self, task, adapter, prompt, **kwargs):
            assert len(event.sent) == 1 and "已收到" in event.sent[0]
            status = await Commands(service).run("群摘要 状态 " + task.source_group)
            assert "模型生成中" in status and "已读取 1 页、1 条" in status
            if fail_model:
                raise DigestError("模型暂时不可用")
            return output(prompt=prompt)

    service.client = Client()
    await entrypoint.digest_command(event)
    assert len(event.sent) == 2
    assert ("模型暂时不可用" if fail_model else "预览结果") in event.sent[-1]
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
