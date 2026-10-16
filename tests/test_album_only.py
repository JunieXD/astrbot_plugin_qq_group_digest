"""Album copies can replace group images without losing previews or captions."""

from dataclasses import replace

import pytest
from PIL import Image

from qq_group_digest.config import IMAGE_MODE

from .test_album_service import Transport, configured, ready


def configure(service, task, **changes):
    task = replace(task, album_send_image=False, **changes)
    settings = service.settings()
    service.settings = lambda: replace(settings, tasks=(task,))
    return task


@pytest.mark.parametrize("caption", ["鼠群日报，请查收～", ""])
async def test_confirmed_album_replaces_images_and_keeps_optional_caption(
    store, task, settings, journal, tmp_path, caption
):
    service, api, _, transport, task = configured(store, task, settings, journal, tmp_path, mode=IMAGE_MODE)
    task = configure(service, task, album_target_groups=task.targets, poster_followup_text=caption)
    original = api.transport

    async def track_send(action, **params):
        transport.events.append((params["message"][0]["type"], params["group_id"]))
        return await original(action, **params)

    api.transport = track_send
    run = await ready(service, api, task)
    await service.step(task)
    await service.step(task)
    assert transport.events == [("album", task.targets[0])] + ([("text", task.targets[0])] if caption else [])
    assert len(api.sent) == bool(caption)
    assert (await store.call("run", run["id"]))["status"] == "complete"
    assert [row["state"] for row in await store.call("deliveries", run["id"])] == ["skipped"] + (
        ["sent"] if caption else []
    )
    assert service.client.calls == 1


async def test_groups_without_album_still_receive_images(store, task, settings, journal, tmp_path):
    service, api, _, transport, task = configured(store, task, settings, journal, tmp_path, mode=IMAGE_MODE)
    task = configure(
        service, task, target_groups=("987654321", "666666666"), album_target_groups=("987654321",)
    )
    run = await ready(service, api, task)
    await service.step(task)
    assert [params["group_id"] for _, params in api.sent] == ["666666666"]
    assert api.sent[0][1]["message"][0]["type"] == "image"
    assert len(transport.uploads) == 1
    assert (await store.call("run", run["id"]))["status"] == "complete"


@pytest.mark.parametrize("state", ["pending", "blocked", "unknown"])
async def test_latest_setting_handles_generated_images_without_overwriting_unknown_send(
    store, task, settings, journal, tmp_path, state
):
    service, api, _, _, task = configured(store, task, settings, journal, tmp_path, mode=IMAGE_MODE)
    task = replace(task, album_target_groups=task.targets)
    original = service.settings()
    service.settings = lambda: replace(original, tasks=(task,))
    run = await ready(service, api, task)
    await service.build(run, task, api)
    row = (await store.call("deliveries", run["id"]))[0]
    await store.call("delivery_result", row["id"], state)
    configure(service, task)
    await service.step(task)
    assert not api.sent
    expected = "unknown" if state == "unknown" else "skipped"
    assert (await store.call("deliveries", run["id"]))[0]["state"] == expected


async def test_all_pages_must_be_confirmed_before_images_or_caption_are_released(
    store, task, settings, journal, tmp_path
):
    service, api, poster, _, task = configured(store, task, settings, journal, tmp_path, mode=IMAGE_MODE)
    task = configure(service, task, album_target_groups=task.targets, poster_followup_text="日报已存相册")
    second = tmp_path / "second.png"
    Image.new("RGB", (32, 32), "white").save(second)
    original = poster.payloads

    async def two_pages(*args, **kwargs):
        payloads = await original(*args, **kwargs)
        return (
            payloads[:1]
            + [{"message": [{"type": "image", "data": {"file": second.as_uri()}}]}]
            + payloads[1:]
        )

    poster.payloads = two_pages

    class Interrupted(Transport):
        async def upload(self, *args, **kwargs):
            result = await super().upload(*args, **kwargs)
            if len(self.uploads) == 2:
                raise TimeoutError("second page acknowledgement lost")
            return result

    transport = Interrupted()
    service.album.transport = lambda adapter, log: transport
    run = await ready(service, api, task)
    await service.step(task)
    archives = await store.call("archive_rows", run["id"])
    assert [row["state"] for row in archives] == ["uploaded", "unknown"]
    assert not api.sent
    assert all(row["state"] == "pending" for row in await store.call("deliveries", run["id"]))
    # A later successful reconciliation releases only the caption, without reuploading.
    await store.call("archive_result", archives[1]["id"], "uploaded")
    await service.step(task)
    await service.step(task)
    assert len(transport.uploads) == 2
    assert len(api.sent) == 1
    assert api.sent[0][1]["message"] == [{"type": "text", "data": {"text": "日报已存相册"}}]
    assert (await store.call("run", run["id"]))["status"] == "complete"


@pytest.mark.parametrize("release", ["skip", "disable", "remove", "no_plan"])
async def test_without_a_confirmed_album_copy_chat_image_is_preserved(
    store, task, settings, journal, tmp_path, release
):
    service, api, _, _, task = configured(store, task, settings, journal, tmp_path, mode=IMAGE_MODE)
    task = configure(service, task, album_target_groups=task.targets, album_enabled=release != "no_plan")
    run = await ready(service, api, task)
    await service.build(run, task, api)
    if release == "skip":
        await store.call("archive_skip", run["id"])
    elif release == "disable":
        configure(service, task, album_enabled=False)
    elif release == "remove":
        configure(service, task, album_target_groups=("555555555",))
    else:
        configure(service, task, album_enabled=True)
    await service.step(task)
    assert len(api.sent) == 1
    assert api.sent[0][1]["message"][0]["type"] == "image"


async def test_plain_text_is_not_suppressed_by_album_setting(store, task, settings, journal, tmp_path):
    service, api, _, transport, task = configured(store, task, settings, journal, tmp_path)
    task = configure(service, task, album_target_groups=task.targets)
    await ready(service, api, task)
    await service.step(task)
    await service.step(task)
    assert len(api.sent) == len(transport.uploads) == 1
    assert api.sent[0][1]["message"][0]["type"] == "text"
