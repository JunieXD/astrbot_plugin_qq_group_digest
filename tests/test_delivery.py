import asyncio
from dataclasses import replace

import pytest

from qq_group_digest.delivery import Delivery
from qq_group_digest.models import Digest, Item
from qq_group_digest.render import make_payloads

from .conftest import NOW, raw_message
from .test_store import make_run


class Guard:
    scheduling_version = 3
    deferred_error = type("GuardDeferred", (Exception,), {})

    async def run(self, **kwargs):
        return await kwargs["action"]()


class API:
    account = "111111111"
    pid = "qq-test"

    def __init__(self):
        self.sent = []
        self.failure_target = None
        self.packet = True
        self.history = []

    async def ready_to_send(self):
        pass

    async def online(self):
        return True

    async def packet_ready(self):
        return self.packet

    async def read(self, action, **params):
        if action == "get_group_info":
            return {"group_id": params["group_id"], "group_all_shut": False}
        if action == "get_group_member_info":
            return {"user_id": self.account, "role": "member", "shut_up_timestamp": 0}
        raise AssertionError(action)

    async def transport(self, action, **params):
        self.sent.append((action, params))
        if params["group_id"] == self.failure_target:
            raise TimeoutError
        return {"message_id": 100 + len(self.sent)}

    async def history_page(self, *args):
        return self.history


class Router:
    def __init__(self, api):
        self.api = api

    async def resolve(self, task):
        return self.api


async def allowed(*args):
    return True


async def build_delivery(store, task, settings, journal, api=None):
    api = api or API()
    rid = await make_run(store, task)
    digest = Digest([Item("通知", "申请将在明天截止。", ("m1",)), Item("资料", "已发布新资料。", ("m2",))])
    payloads = make_payloads(task, digest, NOW - 3600, NOW, api.account, settings.limits)
    rows = [
        {"target": t, "part": i, "mode": task.mode, "payload": p}
        for t in task.targets
        for i, p in enumerate(payloads)
    ]
    await store.call("generated", rid, digest.dump(), rows)
    delivery = Delivery(store, Router(api), Guard(), lambda: settings, journal, allowed, clock=lambda: NOW)
    return delivery, await store.call("run", rid), api


async def test_successful_parts_not_repeated(store, task, settings, journal):
    task = replace(task, mode="普通消息·分条")
    delivery, run, api = await build_delivery(store, task, settings, journal)
    await delivery.process(run)
    await delivery.process(run)
    assert len(api.sent) == 2
    assert (await store.call("run", run["id"]))["status"] == "complete"


async def test_unknown_send_stops_that_target_but_other_target_succeeds(store, task, settings, journal):
    task = replace(task, mode="普通消息·分条", target_groups=("888888888", "987654321"))
    delivery, run, api = await build_delivery(store, task, settings, journal)
    api.failure_target = "888888888"
    await delivery.process(run)
    await delivery.process(run)
    assert [p["group_id"] for _, p in api.sent] == ["888888888", "987654321", "987654321"]
    rows = await store.call("deliveries", run["id"])
    assert [r["state"] for r in rows if r["target"] == "888888888"] == ["unknown", "pending"]
    assert all(r["state"] == "sent" for r in rows if r["target"] == "987654321")


async def test_cancellation_after_submit_becomes_unknown(store, task, settings, journal):
    class CancelAPI(API):
        async def transport(self, *args, **kwargs):
            raise asyncio.CancelledError

    delivery, run, _ = await build_delivery(store, task, settings, journal, CancelAPI())
    with pytest.raises(asyncio.CancelledError):
        await delivery.process(run)
    assert (await store.call("deliveries", run["id"]))[0]["state"] == "unknown"


async def test_packet_unavailable_never_uses_native_self_message_fallback(store, task, settings, journal):
    task = replace(task, mode="合并转发·分条")
    delivery, run, api = await build_delivery(store, task, settings, journal)
    api.packet = False
    await delivery.process(run)
    assert not api.sent
    assert (await store.call("deliveries", run["id"]))[0]["state"] == "pending"


async def test_opt_in_plain_fallback_happens_only_before_submission(store, task, settings, journal):
    task = replace(task, mode="合并转发·分条", fallback_to_plain=True)
    delivery, run, api = await build_delivery(store, task, settings, journal)
    api.packet = False
    await delivery.process(run)
    assert len(api.sent) == 1
    assert api.sent[0][0] == "send_group_msg"


async def test_forward_timeout_does_not_fallback_or_retry(store, task, settings, journal):
    task = replace(task, mode="合并转发·分条", fallback_to_plain=True)
    delivery, run, api = await build_delivery(store, task, settings, journal)
    api.failure_target = task.targets[0]
    await delivery.process(run)
    api.packet = False
    await delivery.process(run)
    assert len(api.sent) == 1
    assert api.sent[0][0] == "send_group_forward_msg"


async def test_successful_ack_durable_before_guard_exit_failure(store, task, settings, journal):
    delivery, run, api = await build_delivery(store, task, settings, journal)

    class GuardSaveFails(Guard):
        async def run(self, **kwargs):
            await kwargs["action"]()
            raise OSError("shared guard state write failed")

    delivery.guard = GuardSaveFails()
    await delivery.process(run)
    await delivery.process(run)
    assert len(api.sent) == 1
    assert (await store.call("deliveries", run["id"]))[0]["state"] == "sent"


async def test_last_minute_pause_prevents_submission(store, task, settings, journal):
    delivery, run, api = await build_delivery(store, task, settings, journal)
    calls = 0

    async def paused_later(*args):
        nonlocal calls
        calls += 1
        return calls <= 2

    delivery.allowed = paused_later
    await delivery.process(run)
    assert not api.sent
    assert (await store.call("deliveries", run["id"]))[0]["state"] == "pending"


async def test_reconcile_own_exact_message_and_keep_ambiguity(store, task, settings, journal):
    delivery, run, api = await build_delivery(store, task, settings, journal)
    api.failure_target = task.targets[0]
    await delivery.process(run)
    sent = api.sent[0][1]["message"]
    message = raw_message(444, NOW, sender=api.account, group=task.targets[0])
    message["message"] = sent
    api.history = [message, raw_message(1, NOW - 100, group=task.targets[0])]
    result = await delivery.reconcile(run)
    assert "已找到" in result
    assert (await store.call("deliveries", run["id"]))[0]["message_id"] == "444"
    assert len(api.sent) == 1


async def test_ambiguous_duplicate_history_does_not_confirm(store, task, settings, journal):
    delivery, run, api = await build_delivery(store, task, settings, journal)
    api.failure_target = task.targets[0]
    await delivery.process(run)
    api.history = [
        {
            **raw_message(mid, NOW, sender=api.account, group=task.targets[0]),
            "message": api.sent[0][1]["message"],
        }
        for mid in (11, 12)
    ]
    api.history.append(raw_message(1, NOW - 100, group=task.targets[0]))
    assert "无法唯一" in await delivery.reconcile(run)
    assert (await store.call("deliveries", run["id"]))[0]["state"] == "unknown"


async def test_invalid_ack_is_unknown(store, task, settings, journal):
    class EmptyAck(API):
        async def transport(self, *args, **kwargs):
            return {"other": "value"}

    delivery, run, _ = await build_delivery(store, task, settings, journal, EmptyAck())
    await delivery.process(run)
    assert (await store.call("deliveries", run["id"]))[0]["state"] == "unknown"


async def test_old_unsent_digest_expires_without_resolving_unknown(store, task, settings, journal):
    task = replace(task, mode="普通消息·分条")
    delivery, run, api = await build_delivery(store, task, settings, journal)
    api.failure_target = task.targets[0]
    await delivery.process(run)
    delivery.clock = lambda: NOW + 2 * 86400
    await delivery.process(run)
    rows = await store.call("deliveries", run["id"])
    assert [r["state"] for r in rows] == ["unknown", "skipped"]
    assert len(api.sent) == 1
