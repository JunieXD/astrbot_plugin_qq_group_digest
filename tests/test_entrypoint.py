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
    assert [reply async for reply in entrypoint.digest_command(event)] == []
    assert event.stopped


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
