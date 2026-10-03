import json
import os
import sqlite3
import sys
import time
from dataclasses import replace
from types import ModuleType, SimpleNamespace

import pytest

from qq_group_digest.config import IMAGE_MODE, DigestError
from qq_group_digest.models import Digest, Item, Message, Speaker
from qq_group_digest.presentation import Presentation
from qq_group_digest.preview import Preview, send_preview

from .conftest import NOW, raw_message
from .test_entrypoint import PrivateEvent
from .test_entrypoint import entrypoint as entrypoint
from .test_images import PNG, image_payload
from .test_service import make_service
from .test_store import make_run


class Avatars:
    def __init__(self):
        self.calls = []

    async def load(self, users, group):
        self.calls.append((set(users), group))
        return {"user:" + qq: "data:image/jpeg;base64,avatar" for qq in users}


class Renderer:
    def __init__(self, root, *, failure=False, pages=1):
        self.root, self.failure, self.pages = root, failure, pages
        self.contexts = []

    async def render(self, context):
        self.contexts.append(context)
        if self.failure:
            raise OSError("renderer unavailable")
        self.root.mkdir(parents=True, exist_ok=True)
        paths = [self.root / f"poster-{index}.png" for index in range(self.pages)]
        for path in paths:
            path.write_bytes(PNG)
        return paths

    async def close(self):
        pass


def presentation(tmp_path, journal, *, failure=False, pages=1):
    result = Presentation.__new__(Presentation)
    result.root, result.journal = tmp_path, journal
    result.avatars = Avatars()
    result.renderer = Renderer(tmp_path / "posters", failure=failure, pages=pages)
    return result


class Client:
    def __init__(self):
        self.calls = 0

    async def generate(self, *args, **kwargs):
        self.calls += 1
        return json.dumps(
            {"items": [{"title": "甲校补录明天截止", "body": "群友u1反馈，甲校补录报名明天截止。"}]}
        )


def send_budget(store):
    with sqlite3.connect(store.path) as database:
        return database.execute("SELECT COUNT(*) FROM budget WHERE kind='send'").fetchone()[0]


async def build_poster(store, task, settings, journal, tmp_path, *, failure=False):
    task = replace(task, mode=IMAGE_MODE, attribute_speakers=True, poster_excluded_members=("444444444",))
    service, api = make_service(store, replace(settings, tasks=(task,)), journal)
    service.presentation = presentation(tmp_path, journal, failure=failure)
    service.client = Client()
    rid = await make_run(store, task)
    messages = [
        Message("native", "100", NOW - 60, "222222222", "甲校补录报名明天截止。", sender_name="小鼠"),
        Message("overlap", "90", NOW - 3610, "333333333", "背景内容。", sender_name="背景同学"),
        Message("bot", "99", NOW - 50, api.account, "机器人发言。"),
        Message("excluded", "101", NOW - 40, "444444444", "其他机器人发言。"),
    ]
    await store.call("fetched", rid, [message.dump() for message in messages], [])
    await service.build(await store.call("run", rid), task, api)
    return service, api, task, await store.call("run", rid)


async def test_scheduled_poster_keeps_stats_and_reuses_exact_artifact_for_delivery(
    store, task, settings, journal, tmp_path
):
    service, api, task, run = await build_poster(store, task, settings, journal, tmp_path)
    saved = Digest.restore(run["digest"])
    assert saved.activity.total_messages == saved.activity.participants == 1
    assert saved.activity.members[0].name == "小鼠"
    assert saved.items[0].speakers == (Speaker("222222222", "小鼠"),)
    context = service.presentation.renderer.contexts[0]
    assert context["stats"] == {"message_count": 1, "speaker_count": 1}
    assert context["leaderboard"][0]["name"] == "小鼠"
    assert context["items"][0]["authors"] == []
    assert context["items"][0]["body_points"][0]["authors"][0]["name"] == "小鼠"
    assert context["items"][0]["body_points"][0]["text"] == "群友反馈，甲校补录报名明天截止。"
    rows = await store.call("deliveries", run["id"])
    uri = rows[0]["payload"]["message"][0]["data"]["file"]
    assert uri.startswith("file://")
    assert await store.call("protected_image_files") == {uri}
    queued = []

    async def paced(**kwargs):
        queued.append(kwargs)
        return await kwargs["action"]()

    service.guard.run = paced
    await service.delivery.process(run)
    await service.delivery.process(run)
    assert len(api.sent) == 1
    action, payload = api.sent[0]
    assert action == "send_group_msg" and payload["group_id"] == task.targets[0]
    assert payload["message"][0]["data"]["file"].startswith("base64://")
    assert queued[0]["delay"] == settings.pace.interval("send")
    assert queued[0]["gap"] == settings.pace.interval("gap")
    assert queued[0]["label"] == "群聊摘要"
    assert send_budget(store) == 1
    assert service.client.calls == len(service.presentation.renderer.contexts) == 1
    assert (await store.call("deliveries", run["id"]))[0]["payload"] == rows[0]["payload"]
    assert await store.call("protected_image_files") == set()


async def test_deferred_image_send_does_not_rerender_or_recall_model(
    store, task, settings, journal, tmp_path
):
    service, api, _, run = await build_poster(store, task, settings, journal, tmp_path)
    original_read = api.read
    muted = [True]

    async def read(action, **params):
        result = await original_read(action, **params)
        if action == "get_group_info":
            result["group_all_shut"] = muted[0]
        return result

    api.read = read
    await service.delivery.process(run)
    assert not api.sent and send_budget(store) == 0
    assert (await store.call("deliveries", run["id"]))[0]["state"] == "pending"
    muted[0] = False
    service.delivery.clock = lambda: NOW + 601
    await service.delivery.process(run)
    assert len(api.sent) == 1 and send_budget(store) == 1
    assert service.client.calls == len(service.presentation.renderer.contexts) == 1


async def test_uncertain_image_send_is_not_retried_or_confirmed_by_empty_text(
    store, task, settings, journal, tmp_path
):
    service, api, task, run = await build_poster(store, task, settings, journal, tmp_path)
    api.failure_target = task.targets[0]
    await service.delivery.process(run)
    await service.delivery.process(run)
    assert len(api.sent) == 1

    async def unavailable(*args, **kwargs):
        raise AssertionError("image-only reconciliation has no useful history query")

    api.history_page = unavailable
    response = await service.delivery.reconcile(run)
    assert "人工核对" in response
    assert (await store.call("deliveries", run["id"]))[0]["state"] == "unknown"
    assert await store.call("protected_image_files")


async def test_scheduled_renderer_fallback_retains_stats_and_sends_one_plain_message(
    store, task, settings, journal, tmp_path
):
    service, api, _, run = await build_poster(store, task, settings, journal, tmp_path, failure=True)
    assert run["digest"]["activity"]["total_messages"] == 1
    row = (await store.call("deliveries", run["id"]))[0]
    assert row["mode"] == IMAGE_MODE and row["payload"]["message"][0]["type"] == "text"
    api.packet = False  # Plain fallback does not depend on merged-forward support.
    await service.delivery.process(run)
    assert len(api.sent) == 1 and api.sent[0][0] == "send_group_msg"
    assert "小鼠" in api.sent[0][1]["message"][0]["data"]["text"]
    assert service.client.calls == 1 and send_budget(store) == 1


async def test_missing_saved_artifact_keeps_delivery_unsubmitted_without_send_budget(
    store, task, settings, journal, tmp_path
):
    service, api, _, run = await build_poster(store, task, settings, journal, tmp_path)
    (tmp_path / "posters" / "poster-0.png").unlink()
    await service.delivery.process(run)
    assert not api.sent and send_budget(store) == 0
    row = (await store.call("deliveries", run["id"]))[0]
    assert row["state"] == "pending" and "无法读取" in row["error"]


async def test_disabling_avatars_preserves_inline_names_and_skips_download(
    store, task, settings, journal, tmp_path
):
    service, _, task, run = await build_poster(store, task, settings, journal, tmp_path)
    service.presentation.avatars.calls.clear()
    context = await service.presentation.context(
        replace(task, poster_show_avatars=False), Digest.restore(run["digest"]), run["start"], run["end"]
    )
    assert not service.presentation.avatars.calls
    assert context["items"][0]["authors"] == []
    assert context["items"][0]["body_points"] == [
        {"text": "群友“小鼠”反馈，甲校补录报名明天截止。", "authors": []}
    ]


async def test_private_image_preview_uses_shared_budget_without_group_publication(
    store, task, settings, journal, tmp_path
):
    task = replace(task, mode=IMAGE_MODE)
    service, api = make_service(store, replace(settings, tasks=(task,)), journal)
    service.presentation = presentation(tmp_path, journal)
    preview = Preview(task, Digest([Item("标题", "有价值的信息", ())]), NOW - 3600, NOW)
    await send_preview(service, PrivateEvent(""), preview)
    assert len(api.private_sent) == 1 and not api.sent
    action, payload = api.private_sent[0]
    assert action == "send_private_msg"
    assert payload["message"][0]["data"]["file"].startswith("base64://")
    assert send_budget(store) == 1
    assert await store.call("latest", task.key) == []


async def test_private_image_preview_cannot_bypass_exhausted_send_budget(
    store, task, settings, journal, tmp_path
):
    task = replace(task, mode=IMAGE_MODE)
    settings = replace(settings, tasks=(task,), pace=replace(settings.pace, sends_per_day=1))
    service, api = make_service(store, settings, journal)
    service.presentation = presentation(tmp_path, journal)
    await store.call("reserve_budget", "send", api.account, NOW, 86400, 1)
    preview = Preview(task, Digest([Item("标题", "内容", ())]), NOW - 3600, NOW)
    with pytest.raises(DigestError, match="达到配置额度"):
        await send_preview(service, PrivateEvent(""), preview)
    assert not api.private_sent and not api.sent


async def test_authenticated_api_image_preview_returns_local_artifact_and_never_sends(
    entrypoint, monkeypatch, store, task, settings, journal, tmp_path
):
    # API overrides the ordinary mode for this preview only; live task stays unchanged.
    task = replace(task, attribute_speakers=True)
    service, api = make_service(store, replace(settings, tasks=(task,)), journal)
    service.client = Client()
    service.presentation = presentation(tmp_path, journal)
    entrypoint.service = service
    web = ModuleType("astrbot.api.web")

    async def body(**kwargs):
        return {"group_id": task.source_group, "mode": IMAGE_MODE}

    web.request = SimpleNamespace(username="admin", json=body)
    monkeypatch.setitem(sys.modules, "astrbot.api.web", web)
    native = raw_message(10, NOW - 1)
    native["sender"] = {"card": "小鼠"}
    api.history = [native, raw_message(1, NOW - task.initial_hours * 3600 - 601)]
    result = await entrypoint.api_preview()
    assert result["status"] == "ok"
    data = result["data"]
    assert data["digest"]["activity"]["total_messages"] == 1
    assert data["digest"]["items"][0]["speakers"][0]["name"] == "小鼠"
    assert data["payloads"][0]["message"][0]["type"] == "image"
    assert data["payloads"][0]["message"][0]["data"]["file"].startswith("file://")
    assert not api.sent and not api.private_sent and send_budget(store) == 0
    assert not service.commands and not service.previews
    assert await store.call("get", "cursor:" + task.key) is None
    assert service.settings().tasks[0].mode == task.mode != IMAGE_MODE


@pytest.mark.parametrize("fallback", [False, True])
async def test_renderer_failure_obeys_fallback_config(tmp_path, task, settings, journal, fallback):
    task = replace(task, mode=IMAGE_MODE, poster_fallback_to_plain=fallback)
    digest = Digest([Item("甲校补录", "明天截止，请关注报名时间。", ())])
    presenter = presentation(tmp_path, journal, failure=True)
    if fallback:
        payloads = await presenter.payloads(task, digest, NOW - 3600, NOW, "111111111", settings.limits)
        assert payloads[0]["message"][0]["type"] == "text"
        assert "明天截止" in payloads[0]["message"][0]["data"]["text"]
    else:
        with pytest.raises(DigestError, match="渲染未完成"):
            await presenter.payloads(task, digest, NOW - 3600, NOW, "111111111", settings.limits)
    assert digest.items[0].body == "明天截止，请关注报名时间。"


async def test_failed_poster_with_long_body_is_deferred_without_truncating(tmp_path, task, settings, journal):
    task = replace(task, mode=IMAGE_MODE, poster_fallback_to_plain=True)
    body = "信息" * settings.limits.message_chars
    digest = Digest([Item("完整主题", body, ())])
    with pytest.raises(DigestError, match="超过普通消息上限"):
        await presentation(tmp_path, journal, failure=True).payloads(
            task, digest, NOW - 3600, NOW, "111111111", settings.limits
        )
    assert digest.items[0].body == body


async def test_empty_digest_skips_renderer_and_avatar_download(tmp_path, task, settings, journal):
    presenter = presentation(tmp_path, journal)
    assert (
        await presenter.payloads(
            replace(task, mode=IMAGE_MODE), Digest([]), NOW - 60, NOW, "111111111", settings.limits
        )
        == []
    )
    assert not presenter.renderer.contexts and not presenter.avatars.calls


async def test_cleanup_preserves_unresolved_image_beyond_normal_retention(
    store, task, settings, journal, tmp_path
):
    presenter = presentation(tmp_path, journal)
    presenter.renderer.root.mkdir(parents=True)
    protected = presenter.renderer.root / "pending.png"
    orphan = presenter.renderer.root / "orphan.png"
    recent = presenter.renderer.root / "recent.png"
    for path in (protected, orphan, recent):
        path.write_bytes(PNG)
    old = time.time() - 8 * 86400
    for path in (protected, orphan):
        os.utime(path, (old, old))
    rid = await make_run(store, replace(task, mode=IMAGE_MODE))
    await store.call(
        "generated",
        rid,
        Digest([]).dump(),
        [{"target": task.targets[0], "part": 0, "mode": IMAGE_MODE, "payload": image_payload(protected)}],
    )
    row = (await store.call("deliveries", rid))[0]
    await store.call("delivery_result", row["id"], "unknown")
    await store.call("cleanup", NOW + 60 * 86400, 2, 30)
    presenter.cleanup(await store.call("protected_image_files"))
    assert protected.exists() and recent.exists() and not orphan.exists()
    assert (await store.call("deliveries", rid))[0]["state"] == "unknown"


async def test_switching_fetched_unattributed_text_to_image_rereads_names(store, task):
    original = replace(task, attribute_speakers=False)
    rid = await make_run(store, original)
    await store.call("fetched", rid, [Message("a", "1", NOW - 1, "222222222", "信息").dump()], [])
    await store.call("reconfigure", rid, replace(original, mode=IMAGE_MODE).dump())
    run = await store.call("run", rid)
    assert run["status"] == "queued" and run["snapshot"] is None
