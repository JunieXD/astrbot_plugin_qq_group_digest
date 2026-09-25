from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_group_digest.config import DigestError
from qq_group_digest.models import Digest, Item
from qq_group_digest.preview import Preview, send_preview

from .conftest import NOW
from .test_entrypoint import PrivateEvent
from .test_entrypoint import entrypoint as entrypoint
from .test_service import make_service


def result(task, *, size=10):
    return Preview(task, Digest([Item("完整标题", "信息" * size, ("u1",))]), NOW - 3600, NOW)


@pytest.mark.parametrize("fallback", [False, True])
async def test_unavailable_forward_only_falls_back_when_configured(store, task, settings, journal, fallback):
    service, api = make_service(store, settings, journal)
    api.packet = False
    task = replace(task, mode="合并转发·分条", fallback_to_plain=fallback)
    event = PrivateEvent("/群摘要 预览 " + task.source_group)
    if fallback:
        await send_preview(service, event, result(task))
        assert len(api.private_sent) == 1 and api.private_sent[0][0] == "send_private_msg"
    else:
        with pytest.raises(DigestError, match="合并转发能力暂不可用"):
            await send_preview(service, event, result(task))
        assert not api.private_sent
    assert not api.sent


async def test_long_preview_bypasses_plain_command_reply_truncation(
    entrypoint, store, task, settings, journal
):
    task = replace(task, mode="合并转发·分条", summary_chars=6000)
    service, api = make_service(store, replace(settings, tasks=(task,)), journal)
    preview = result(task, size=2200)

    async def generate(*args, **kwargs):
        return preview.digest, preview.start, preview.end

    service.preview = generate
    entrypoint.service = service
    event = PrivateEvent("/群摘要 预览 " + task.source_group)
    await entrypoint.digest_command(event)
    text = api.private_sent[0][1]["messages"][1]["data"]["content"][0]["data"]["text"]
    assert preview.digest.items[0].body in text and len(text) > 3800
    assert not event.sent and not api.sent and not service.commands


@pytest.mark.parametrize("bad_receipt", [False, True])
async def test_uncertain_forward_never_falls_back_or_retries(store, task, settings, journal, bad_receipt):
    service, api = make_service(store, settings, journal)
    task = replace(task, mode="合并转发·分条", fallback_to_plain=True)
    calls = []

    async def failing(action, **params):
        calls.append(action)
        if bad_receipt:
            return {}
        raise TimeoutError

    api.transport = failing
    with pytest.raises(DigestError, match="发送结果未确认"):
        await send_preview(service, PrivateEvent(""), result(task))
    assert calls == ["send_private_forward_msg"]


async def test_plain_parts_stop_after_uncertain_send_and_use_shared_pacing(store, task, settings, journal):
    service, api = make_service(store, settings, journal)
    task = replace(task, mode="普通消息·分条")
    preview = Preview(task, Digest([Item(str(i), "信息", ("u1",)) for i in range(3)]), NOW - 60, NOW)
    sent, queued = [], []

    async def transport(action, **params):
        sent.append(action)
        if len(sent) == 2:
            raise TimeoutError
        return {"message_id": 101}

    async def paced(**kwargs):
        queued.append(kwargs)
        return await kwargs["action"]()

    api.transport = transport
    service.guard.run = paced
    with pytest.raises(DigestError, match="第 2 条发送结果未确认"):
        await send_preview(service, PrivateEvent(""), preview)
    assert sent == ["send_private_msg"] * 2
    assert all(q["account"] == api.pid and q["gap"] == settings.pace.interval("gap") for q in queued)
    assert all(q["delay"] == settings.pace.interval("send") for q in queued)
    assert await store.call("latest", task.key) == []


async def test_private_send_quota_stops_before_write(store, task, settings, journal):
    settings = replace(settings, pace=replace(settings.pace, sends_per_day=1))
    service, api = make_service(store, settings, journal)
    await store.call("reserve_budget", "send", api.account, NOW, 86400, 1)
    with pytest.raises(DigestError, match="达到配置额度"):
        await send_preview(service, PrivateEvent(""), result(task))
    assert not api.sent and not api.private_sent


@pytest.mark.parametrize("admin,group", [(False, ""), (True, "123456789")])
async def test_sender_rejects_non_private_or_unprivileged_context(admin, group):
    event = SimpleNamespace(is_admin=lambda: admin, get_group_id=lambda: group)
    with pytest.raises(DigestError, match="管理员私聊"):
        await send_preview(None, event, None)
