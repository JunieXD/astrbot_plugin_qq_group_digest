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


@pytest.mark.parametrize("action", ["send_group_msg", "send_private_msg", "send_private_forward_msg"])
async def test_reconnect_restarts_cooldown_and_prevents_write(store, settings, journal, action):
    from dataclasses import replace

    settings = replace(
        settings, pace=replace(settings.pace, recovery_min_seconds=60, recovery_max_seconds=60)
    )
    bot = Bot()
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    await adapter.check_connection()
    with pytest.raises(Deferred):
        await adapter.transport(action, message=[])
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


async def test_private_reply_routes_by_event_and_rejects_changed_account(store, settings, journal):
    router = Router(object(), store, lambda: settings, journal)
    source, reply = Bot(), Bot()
    source._wsr_api_clients = {"333333333": object()}
    router.adapter("source", source)
    event = SimpleNamespace(bot=reply, get_platform_id=lambda: "reply", get_self_id=lambda: "111111111")
    adapter = await router.for_event(event)
    assert adapter.bot is reply and adapter.account == "111111111"
    assert await router.for_event(event) is adapter
    reply._wsr_api_clients = {"444444444": object()}
    with pytest.raises(DigestError, match="改变"):
        await router.for_event(event)


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


async def test_read_failure_cooldown_survives_reload_and_blocks_other_queries(store, settings, journal):
    class FailingBot(Bot):
        async def call_action(self, action, **params):
            self.calls.append(action)
            raise TimeoutError

    bot = FailingBot()
    first = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    with pytest.raises(Deferred):
        await first.read("get_status")
    second = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW + 1)
    with pytest.raises(Deferred, match="冷却"):
        await second.history_page("123456789", 20)
    assert len(bot.calls) == 1


async def test_reconnection_rejects_old_history_cursor_before_call(store, settings, journal):
    bot = Bot()
    bot.result = {"messages": []}
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    await adapter.history_page("123456789", 20)
    generation = adapter.generation
    bot._wsr_api_clients["111111111"] = object()
    with pytest.raises(Deferred, match="连接发生变化"):
        await adapter.history_page("123456789", 20, "123", generation=generation)
    assert len(bot.calls) == 1


async def test_connection_changed_during_first_page_discards_response(store, settings, journal):
    class ReconnectingBot(Bot):
        async def call_action(self, action, **params):
            self._wsr_api_clients["111111111"] = object()
            return {"messages": []}

    adapter = Adapter("platform", ReconnectingBot(), store, lambda: settings, journal, clock=lambda: NOW)
    with pytest.raises(Deferred, match="连接发生变化"):
        await adapter.history_page("123456789", 20)


async def test_explicit_account_not_blocked_by_unrelated_offline_adapter(store, task, settings, journal):
    from dataclasses import replace

    online, offline = Bot(), Bot()
    offline._wsr_api_clients = {}
    platforms = [
        SimpleNamespace(meta=lambda: SimpleNamespace(id="online", name="aiocqhttp"), bot=online),
        SimpleNamespace(meta=lambda: SimpleNamespace(id="offline", name="aiocqhttp"), bot=offline),
    ]
    context = SimpleNamespace(platform_manager=SimpleNamespace(get_insts=lambda: platforms))
    router = Router(context, store, lambda: settings, journal)
    adapter = await router.resolve(replace(task, bot_qq="111111111"))
    assert adapter.bot is online


async def test_reload_preserves_connection_cooldown_without_extending_it(store, settings, journal):
    from dataclasses import replace

    settings = replace(
        settings, pace=replace(settings.pace, recovery_min_seconds=60, recovery_max_seconds=60)
    )
    bot = Bot()
    first = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    await first.check_connection()
    second = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW + 30)
    with pytest.raises(Deferred) as pending:
        await second.ready_to_send()
    assert pending.value.seconds == 30
    third = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW + 61)
    await third.ready_to_send()
    assert third.generation == 1
    bot._wsr_api_clients["111111111"] = object()
    fourth = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW + 62)
    with pytest.raises(Deferred) as pending:
        await fourth.ready_to_send()
    assert pending.value.seconds == 60


async def test_observed_disconnect_restarts_protection_even_with_same_client(store, settings, journal):
    from dataclasses import replace

    settings = replace(
        settings, pace=replace(settings.pace, recovery_min_seconds=60, recovery_max_seconds=60)
    )
    bot = Bot()
    clients = bot._wsr_api_clients
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    await adapter.check_connection()
    adapter.clock = lambda: NOW + 61
    bot._wsr_api_clients = {}
    with pytest.raises(Deferred, match="尚未连接"):
        await adapter.check_connection()
    bot._wsr_api_clients = clients
    with pytest.raises(Deferred) as pending:
        await adapter.ready_to_send()
    assert pending.value.seconds == 60


async def test_connection_cache_ignores_dynamic_api_attributes(store, settings, journal):
    class DynamicBot(Bot):
        def __getattr__(self, name):
            return lambda **params: self.call_action(name, **params)

    bot = DynamicBot()
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    await adapter.ready_to_send()
    recreated = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    await recreated.ready_to_send()
    assert recreated._observed is adapter._observed


@pytest.mark.parametrize("seconds", [5, 45])
@pytest.mark.parametrize("action", ["send_group_msg", "send_private_msg"])
async def test_image_upload_acknowledgement_fits_transport_deadline(
    store, settings, journal, seconds, action
):
    from dataclasses import replace

    settings = replace(settings, limits=replace(settings.limits, api_timeout_seconds=seconds))
    bot = Bot()
    bot.result = {"message_id": 123}
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    result = await adapter.transport(action, message=[{"type": "image", "data": {"file": "base64://image"}}])
    assert result == {"message_id": 123}
    params = bot.calls[0][1]
    assert params["self_id"] == "111111111"
    assert 1000 <= params["timeout"] <= seconds * 1000 - 3000
    assert params["timeout"] == 42000 if seconds == 45 else params["timeout"] == 2000


async def test_plain_message_preserves_napcat_default_timeout(store, settings, journal):
    bot = Bot()
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    await adapter.transport("send_private_msg", message=[{"type": "text", "data": {"text": "摘要"}}])
    assert "timeout" not in bot.calls[0][1]


async def test_actual_group_name_is_cached_across_adapter_reload(store, settings, journal):
    bot = Bot()
    bot.result = {"group_id": 123456789, "group_name": "  四非计算机保研群  "}
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    assert await adapter.group_name("123456789") == "四非计算机保研群"
    assert await adapter.group_name("123456789") == "四非计算机保研群"
    reloaded = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW + 60)
    await reloaded.check_connection()
    assert await reloaded.group_name("123456789") == "四非计算机保研群"
    assert bot.calls == [("get_group_info", {"group_id": "123456789", "self_id": "111111111"})]
    bot.result["group_name"] = "新的真实群名"
    reloaded.clock = lambda: NOW + 86401
    assert await reloaded.group_name("123456789") == "新的真实群名"
    assert len(bot.calls) == 2


async def test_missing_group_name_is_negative_cached_and_never_invented(store, settings, journal):
    bot = Bot()
    bot.result = {"group_id": 123456789}
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    assert await adapter.group_name("123456789") == ""
    assert await adapter.group_name("123456789") == ""
    assert len(bot.calls) == 1
    bot.result["group_name"] = "真实群名"
    adapter.clock = lambda: NOW + 3601
    assert await adapter.group_name("123456789") == "真实群名"
    assert len(bot.calls) == 2


async def test_group_name_failure_preserves_previous_actual_name(store, settings, journal):
    bot = Bot()
    bot.result = {"group_name": "以前的实际群名"}
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    assert await adapter.group_name("123456789") == "以前的实际群名"
    adapter.clock = lambda: NOW + 86401
    bot.result = None
    assert await adapter.group_name("123456789") == "以前的实际群名"
    assert await adapter.group_name("123456789") == "以前的实际群名"
    assert len(bot.calls) == 2


async def test_group_name_lookup_deadline_does_not_hold_read_lock(store, settings, journal):
    import asyncio

    class SlowBot(Bot):
        async def call_action(self, action, **params):
            self.calls.append((action, params))
            await asyncio.sleep(100)

    adapter = Adapter("platform", SlowBot(), store, lambda: settings, journal, clock=lambda: NOW)
    adapter.GROUP_NAME_TIMEOUT = 0.2
    assert await adapter.group_name("123456789") == ""
    assert not adapter.read_lock.locked()
    assert await adapter.group_name("123456789") == ""
    assert len(adapter.bot.calls) == 1
    assert await store.call("get", "read-cooldown:platform", 0) == 0


async def test_group_name_caller_cancellation_is_not_a_lookup_failure(store, settings, journal):
    import asyncio

    started = asyncio.Event()

    class SlowBot(Bot):
        async def call_action(self, action, **params):
            started.set()
            await asyncio.sleep(100)

    adapter = Adapter("platform", SlowBot(), store, lambda: settings, journal, clock=lambda: NOW)
    pending = asyncio.create_task(adapter.group_name("123456789"))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not adapter._group_names
    assert not adapter.read_lock.locked()


async def test_concurrent_group_name_lookups_only_query_once(store, settings, journal):
    import asyncio

    bot = Bot()
    bot.result = {"group_name": "真实群名"}
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    assert await asyncio.gather(*[adapter.group_name("123456789") for _ in range(3)]) == ["真实群名"] * 3
    assert len(bot.calls) == 1


async def test_group_name_cache_is_bounded(store, settings, journal):
    bot = Bot()
    bot.result = {"group_name": "真实群名"}
    adapter = Adapter("platform", bot, store, lambda: settings, journal, clock=lambda: NOW)
    adapter._group_names.update({f"111111111:{group}": ("旧群名", NOW - 1) for group in range(10000, 10200)})
    assert await adapter.group_name("123456789") == "真实群名"
    assert len(adapter._group_names) == 128
