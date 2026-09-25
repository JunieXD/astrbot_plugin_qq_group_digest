from types import SimpleNamespace

import pytest

from qq_group_digest.config import Deferred, DigestError
from qq_group_digest.pacing import get_guard
from qq_group_digest.platform import Adapter, Router

from .conftest import NOW


class Bot:
    def __init__(self):
        self._wsr_api_clients = {"111111111": object()}
        self.calls = []
        self.result = None

    async def call_action(self, action, **params):
        self.calls.append((action, params))
        return self.result


async def test_history_request_has_lightweight_flags_and_explicit_routing(store, settings, journal):
    bot = Bot()
    bot.result = {"messages": []}
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    await adapter.history_page("123456789", 20, "987")
    action, params = bot.calls[0]
    assert action == "get_group_msg_history"
    assert params == {
        "group_id": "123456789",
        "count": 20,
        "message_seq": "987",
        "reverse_order": True,
        "disable_get_url": True,
        "parse_mult_msg": False,
        "quick_reply": True,
        "self_id": "111111111",
    }


async def test_packet_status_null_is_success(store, settings, journal):
    adapter = Adapter("platform", Bot(), store, lambda: settings, journal, clock=lambda: NOW)
    assert await adapter.packet_ready()


async def test_transport_envelope_failed_is_not_success(store, settings, journal):
    bot = Bot()
    bot.result = {"retcode": 1, "status": "failed", "data": None}
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    with pytest.raises(DigestError):
        await adapter.read("get_status")


async def test_quota_survives_adapter_recreation(store, settings, journal):
    from dataclasses import replace

    settings = replace(settings, pace=replace(settings.pace, reads_per_hour=1))
    bot = Bot()
    first = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    await first.read("get_status")
    second = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    with pytest.raises(Deferred):
        await second.read("get_status")
    assert len(bot.calls) == 1


async def test_reconnect_restarts_cooldown_and_prevents_write(store, settings, journal):
    from dataclasses import replace

    settings = replace(
        settings, pace=replace(settings.pace, recovery_min_seconds=60, recovery_max_seconds=60)
    )
    bot = Bot()
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    await adapter.check_connection()
    with pytest.raises(Deferred):
        await adapter.transport("send_group_msg", group_id="123456789", message=[])
    assert not bot.calls
    adapter.clock = lambda: NOW + 61
    await adapter.ready_to_send()
    bot._wsr_api_clients["111111111"] = object()
    with pytest.raises(Deferred):
        await adapter.ready_to_send()


async def test_changed_account_is_rejected(store, settings, journal):
    bot = Bot()
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    await adapter.check_connection()
    bot._wsr_api_clients = {"333333333": object()}
    with pytest.raises(DigestError, match="改变"):
        await adapter.check_connection()


async def test_multiple_connections_are_rejected(store, settings, journal):
    bot = Bot()
    bot._wsr_api_clients["333333333"] = object()
    adapter = Adapter("platform", bot, store, lambda: settings, journal)
    with pytest.raises(DigestError, match="多个"):
        await adapter.check_connection()


async def test_router_never_guesses_between_accounts(store, task, settings, journal):
    platforms = [
        SimpleNamespace(meta=lambda: SimpleNamespace(id=str(i), name="aiocqhttp"), bot=Bot())
        for i in range(2)
    ]
    context = SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: platforms))
    router = Router(context, store, lambda: settings, journal)
    with pytest.raises(DigestError, match="唯一"):
        await router.resolve(task)


def test_shared_guard_reused_without_class_replacement(tmp_path):
    existing = SimpleNamespace(scheduling_version=1, run=lambda **kwargs: None, deferred_error=ValueError)
    context = SimpleNamespace(platform_manager=SimpleNamespace(_qq_automation_guard_v1=existing))
    assert get_guard(context, tmp_path) is existing
    assert existing.__class__ is SimpleNamespace


def test_standalone_guard_is_registered(tmp_path):
    context = SimpleNamespace(platform_manager=SimpleNamespace())
    guard = get_guard(context, tmp_path)
    assert guard.scheduling_version == 3
    assert get_guard(context, tmp_path) is guard
