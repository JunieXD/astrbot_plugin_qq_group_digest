from dataclasses import replace

import pytest

from qq_group_digest.config import IMAGE_MODE, Deferred, DigestError, Task, parse_settings
from qq_group_digest.models import Digest, Item, Message
from qq_group_digest.preview import Preview, send_preview

from .conftest import NOW
from .test_entrypoint import PrivateEvent
from .test_poster_integration import Client, presentation, send_budget
from .test_service import make_service
from .test_store import make_run

CAPTION = "有价值的信息欢迎继续补充 ✨"


def digest():
    return Digest([Item("甲校补录报名", "甲校补录报名明天截止。", ())])


async def build_followup(store, task, settings, journal, tmp_path, *, pages=1):
    task = replace(task, mode=IMAGE_MODE, attribute_speakers=True, poster_followup_text=CAPTION)
    service, api = make_service(store, replace(settings, tasks=(task,)), journal)
    service.presentation = presentation(tmp_path, journal, pages=pages)
    service.client = Client()
    rid = await make_run(store, task)
    message = Message("native", "100", NOW - 60, "222222222", "甲校补录报名明天截止。", sender_name="小鼠")
    await store.call("fetched", rid, [message.dump()], [])
    await service.build(await store.call("run", rid), task, api)
    return service, api, task, await store.call("run", rid)


async def test_default_empty_followup_emits_only_image(tmp_path, task, settings, journal):
    task = replace(task, mode=IMAGE_MODE)
    presenter = presentation(tmp_path, journal)
    payloads = await presenter.payloads(task, digest(), NOW - 3600, NOW, "111111111", settings.limits)
    assert task.poster_followup_text == ""
    assert len(payloads) == 1 and payloads[0]["message"][0]["type"] == "image"


@pytest.mark.parametrize("pages", [1, 3])
async def test_followup_is_one_plain_payload_after_all_images(tmp_path, task, settings, journal, pages):
    task = replace(task, mode=IMAGE_MODE, poster_followup_text=CAPTION)
    presenter = presentation(tmp_path, journal, pages=pages)
    payloads = await presenter.payloads(task, digest(), NOW - 3600, NOW, "111111111", settings.limits)
    assert len(payloads) == pages + 1
    assert [payload["message"][0]["type"] for payload in payloads] == ["image"] * pages + ["text"]
    assert payloads[-1] == {"message": [{"type": "text", "data": {"text": CAPTION}}]}


@pytest.mark.parametrize("mode", ["普通消息·整篇", "普通消息·分条", "合并转发·整篇", "合并转发·分条"])
async def test_followup_never_applies_to_other_modes(tmp_path, task, settings, journal, mode):
    task = replace(task, mode=mode, poster_followup_text=CAPTION)
    presenter = presentation(tmp_path, journal)
    payloads = await presenter.payloads(task, digest(), NOW - 3600, NOW, "111111111", settings.limits)
    assert CAPTION not in str(payloads)
    assert not presenter.renderer.contexts


async def test_no_content_or_renderer_fallback_has_no_followup(tmp_path, task, settings, journal):
    task = replace(task, mode=IMAGE_MODE, poster_followup_text=CAPTION)
    presenter = presentation(tmp_path, journal)
    assert await presenter.payloads(task, Digest([]), NOW - 3600, NOW, "111111111", settings.limits) == []
    assert not presenter.renderer.contexts
    presenter = presentation(tmp_path, journal, failure=True)
    fallback = await presenter.payloads(task, digest(), NOW - 3600, NOW, "111111111", settings.limits)
    assert len(fallback) == 1 and fallback[0]["message"][0]["type"] == "text"
    assert CAPTION not in str(fallback)


async def test_unknown_first_image_stops_followup_and_all_automatic_retry(
    store, task, settings, journal, tmp_path
):
    service, api, task, run = await build_followup(store, task, settings, journal, tmp_path)
    api.failure_target = task.targets[0]
    await service.delivery.process(run)
    api.failure_target = None
    await service.delivery.process(run)
    assert len(api.sent) == send_budget(store) == 1
    assert api.sent[0][1]["message"][0]["type"] == "image"
    assert [row["state"] for row in await store.call("deliveries", run["id"])] == ["unknown", "pending"]
    assert CAPTION not in str(api.sent)


async def test_deferred_followup_retry_does_not_repeat_images_and_uses_same_queue(
    store, task, settings, journal, tmp_path
):
    service, api, task, run = await build_followup(store, task, settings, journal, tmp_path, pages=2)
    queued = []

    async def paced(**kwargs):
        queued.append(kwargs)
        if len(queued) == 3:
            raise service.guard.deferred_error("附言暂缓") from Deferred("附言暂缓", 60)
        return await kwargs["action"]()

    service.guard.run = paced
    await service.delivery.process(run)
    rows = await store.call("deliveries", run["id"])
    assert [row["state"] for row in rows] == ["sent", "sent", "pending"]
    assert rows[-1]["next_try"] == NOW + 60
    assert len(api.sent) == send_budget(store) == 2
    service.delivery.clock = lambda: NOW + 61
    await service.delivery.process(run)
    assert [payload["message"][0]["type"] for _, payload in api.sent] == ["image", "image", "text"]
    assert api.sent[-1][1]["message"][0]["data"]["text"] == CAPTION
    assert all(action == "send_group_msg" for action, _ in api.sent)
    assert send_budget(store) == 3
    assert all(entry["account"] == api.pid and entry["group"] == task.targets[0] for entry in queued)
    assert all(entry["delay"] == settings.pace.interval("send") for entry in queued)
    assert all(entry["gap"] == settings.pace.interval("gap") for entry in queued)
    assert service.client.calls == len(service.presentation.renderer.contexts) == 1
    assert (await store.call("run", run["id"]))["status"] == "complete"


async def test_followup_consumes_its_own_quota_and_does_not_bypass_exhausted_budget(
    store, task, settings, journal, tmp_path
):
    settings = replace(settings, pace=replace(settings.pace, sends_per_day=1))
    service, api, _, run = await build_followup(store, task, settings, journal, tmp_path)
    await service.delivery.process(run)
    assert len(api.sent) == send_budget(store) == 1
    rows = await store.call("deliveries", run["id"])
    assert [row["state"] for row in rows] == ["sent", "pending"]
    assert "达到配置额度" in rows[-1]["error"]
    assert rows[-1]["submitted"] is None


async def test_private_followup_is_plain_text_after_image_with_separate_pacing_and_budget(
    store, task, settings, journal, tmp_path
):
    task = replace(task, mode=IMAGE_MODE, poster_followup_text=CAPTION)
    service, api = make_service(store, replace(settings, tasks=(task,)), journal)
    service.presentation = presentation(tmp_path, journal)
    queued = []

    async def paced(**kwargs):
        queued.append(kwargs)
        return await kwargs["action"]()

    service.guard.run = paced
    await send_preview(service, PrivateEvent(""), Preview(task, digest(), NOW - 3600, NOW))
    assert [payload["message"][0]["type"] for _, payload in api.private_sent] == ["image", "text"]
    assert api.private_sent[-1][1]["message"][0]["data"]["text"] == CAPTION
    assert all(action == "send_private_msg" for action, _ in api.private_sent)
    assert len(queued) == send_budget(store) == 2
    assert all(entry["gap"] == settings.pace.interval("gap") for entry in queued)
    assert all(entry["group"] == "private:222222222" for entry in queued)
    assert not api.sent


@pytest.mark.parametrize("pages", [1, 2])
async def test_followup_counts_toward_part_limit(tmp_path, task, settings, journal, pages):
    task = replace(task, mode=IMAGE_MODE, poster_followup_text=CAPTION, poster_fallback_to_plain=False)
    presenter = presentation(tmp_path, journal, pages=pages)
    with pytest.raises(DigestError, match="渲染未完成"):
        await presenter.payloads(
            task, digest(), NOW - 3600, NOW, "111111111", replace(settings.limits, max_parts=pages)
        )
    payloads = await presenter.payloads(
        task, digest(), NOW - 3600, NOW, "111111111", replace(settings.limits, max_parts=pages + 1)
    )
    assert len(payloads) == pages + 1 and payloads[-1]["message"][0]["type"] == "text"


async def test_part_limit_plain_fallback_does_not_publish_caption(tmp_path, task, settings, journal):
    task = replace(task, mode=IMAGE_MODE, poster_followup_text=CAPTION)
    payloads = await presentation(tmp_path, journal).payloads(
        task, digest(), NOW - 3600, NOW, "111111111", replace(settings.limits, max_parts=1)
    )
    assert len(payloads) == 1 and payloads[0]["message"][0]["type"] == "text"
    assert CAPTION not in str(payloads)


def test_followup_config_defaults_old_snapshot_restore_and_trimmed_multiline_text(task):
    old = task.dump()
    old.pop("poster_followup_text")
    assert Task.restore(old).poster_followup_text == ""
    assert (
        parse_settings({"tasks": [{"source_group": task.source_group}]}).tasks[0].poster_followup_text == ""
    )
    changed = parse_settings(
        {"tasks": [{"source_group": task.source_group, "poster": {"followup_text": "  第一行\n第二行 ✨  "}}]}
    ).tasks[0]
    assert changed.poster_followup_text == "第一行\n第二行 ✨"
    assert Task.restore(changed.dump()) == changed
    limit = parse_settings(
        {"tasks": [{"source_group": task.source_group, "poster": {"followup_text": "字" * 500}}]}
    ).tasks[0]
    assert len(limit.poster_followup_text) == 500


@pytest.mark.parametrize(
    "bad", [None, 123, True, ["text"], "字" * 501, "前\x00后", "前\t后", "前\r后", "前\x7f后", "前\x85后"]
)
def test_invalid_followup_config_is_rejected(task, bad):
    with pytest.raises(DigestError, match="最多 500 字"):
        parse_settings({"tasks": [{"source_group": task.source_group, "poster": {"followup_text": bad}}]})
