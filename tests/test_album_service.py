"""Exercise real scheduling, generation, delivery and durable album integration."""

import sqlite3
from dataclasses import replace

import pytest
from PIL import Image

from qq_group_digest.config import IMAGE_MODE, DigestError
from qq_group_digest.render import make_payloads

from .conftest import NOW, raw_message
from .test_service import make_service


class Poster:
    def __init__(self, path):
        Image.new("RGB", (32, 32), "#f4e9d6").save(path, format="PNG")
        self.file = path.as_uri()
        self.calls = []
        self.fail = False

    async def payloads(self, task, digest, start, end, account, limits, mode=None):
        effective = mode or task.mode
        self.calls.append(effective)
        if effective != IMAGE_MODE:
            return make_payloads(task, digest, start, end, account, limits, mode)
        if self.fail:
            raise DigestError("海报渲染暂不可用。")
        if not digest.items:
            return []
        payloads = [{"message": [{"type": "image", "data": {"file": self.file}}]}]
        if task.poster_followup_text:
            payloads.append({"message": [{"type": "text", "data": {"text": task.poster_followup_text}}]})
        return payloads


class Transport:
    def __init__(self):
        self.uploads = []
        self.events = []

    async def find_album(self, group, name):
        return "existing-album"

    async def upload(self, group, album_id, name, file, description, *, on_submit):
        # The persisted intent must precede the simulated remote write.
        await on_submit({"operation": "upload", "file_name": file.name, "md5": "picture-md5"})
        self.uploads.append((group, album_id, name, file.read_bytes(), description))
        self.events.append(("album", group))
        return {"operation": "upload", "photo_id": f"photo-{len(self.uploads)}"}


def configured(store, task, settings, journal, tmp_path, *, mode="普通消息·整篇", enabled=True):
    task = replace(
        task,
        mode=mode,
        times=("10:00",),
        album_enabled=enabled,
        album_target_groups=("888888888", "777777777"),
        album_name="鼠群日报",
    )
    settings = replace(settings, tasks=(task,))
    service, api = make_service(store, settings, journal)
    poster, transport = Poster(tmp_path / "poster.png"), Transport()
    service.presentation = poster
    service.album.transport = lambda adapter, log: transport

    async def group_name(group):
        return "实际群名"

    api.group_name = group_name
    return service, api, poster, transport, task


async def ready(service, api, task):
    rid = await service.ensure_window(task, manual=True)
    await service.store.call("bind", rid, api.account, api.pid)
    run = await service.store.call("run", rid)
    api.history = [raw_message(10, run["end"] - 1), raw_message(1, run["read_start"] - 1)]
    return run


async def test_image_build_saves_message_and_album_plan_with_same_files(
    store, task, settings, journal, tmp_path
):
    service, api, poster, _, task = configured(store, task, settings, journal, tmp_path, mode=IMAGE_MODE)
    run = await ready(service, api, task)
    await service.build(run, task, api)
    messages = await store.call("deliveries", run["id"])
    archives = await store.call("archive_rows", run["id"])
    assert len(messages) == 1 and len(archives) == 2
    assert messages[0]["payload"]["message"][0]["data"]["file"] == poster.file
    assert all(row["file"] == poster.file and row["state"] == "pending" for row in archives)
    assert {row["target"] for row in archives} == set(task.album_target_groups)
    assert all(row["album_name"] == task.album_name for row in archives)
    assert service.client.calls == 1 and poster.calls == [IMAGE_MODE]
    assert not api.sent


async def test_album_insert_failure_rolls_back_generated_digest_and_message_plan(
    store, task, settings, journal, tmp_path, monkeypatch
):
    service, api, _, _, task = configured(store, task, settings, journal, tmp_path, mode=IMAGE_MODE)
    run = await ready(service, api, task)
    original = store.albums.insert

    def failed_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("simulated disk failure")

    monkeypatch.setattr(store.albums, "insert", failed_after_insert)
    with pytest.raises(DigestError, match="进度无法写入"):
        await service.build(run, task, api)
    saved = await store.call("run", run["id"])
    assert saved["status"] == "fetched" and saved["digest"] is None
    assert saved["snapshot"]  # The separately completed historical read remains reusable.
    assert await store.call("deliveries", run["id"]) == []
    assert await store.call("archive_rows", run["id"]) == []
    assert not api.sent


@pytest.mark.parametrize("mode", ["普通消息·整篇", IMAGE_MODE])
async def test_disabled_album_never_creates_archive_intents(store, task, settings, journal, tmp_path, mode):
    service, api, _, _, task = configured(store, task, settings, journal, tmp_path, mode=mode, enabled=False)
    run = await ready(service, api, task)
    await service.build(run, task, api)
    assert await store.call("archive_rows", run["id"]) == []
    assert await store.call("deliveries", run["id"])


async def test_opted_in_private_preview_can_render_but_never_plans_album_uploads(
    store, task, settings, journal, tmp_path
):
    service, api, poster, transport, task = configured(
        store, task, settings, journal, tmp_path, mode=IMAGE_MODE
    )
    api.history = [raw_message(10, NOW - 1), raw_message(1, NOW - 86400 - 601)]
    digest, start, end = await service.preview(task)
    payloads = await service.payloads(task, digest, start, end, api.account)
    assert payloads[0]["message"][0]["data"]["file"] == poster.file
    assert await store.call("latest", task.key) == []
    assert await store.call("archive_active", task.key, NOW) == []
    assert not api.sent and not transport.uploads


async def test_image_delivery_and_archiving_reuse_render_without_recalling_model(
    store, task, settings, journal, tmp_path
):
    service, api, poster, transport, task = configured(
        store, task, settings, journal, tmp_path, mode=IMAGE_MODE
    )
    run = await ready(service, api, task)
    await service.step(task)
    await service.step(task)
    assert (await store.call("run", run["id"]))["status"] == "complete"
    assert service.client.calls == 1 and poster.calls == [IMAGE_MODE]
    assert len(api.sent) == 1 and len(transport.uploads) == 2
    assert all(row["state"] == "uploaded" for row in await store.call("archive_rows", run["id"]))
    # Group delivery reads the image into bytes; archiving receives the same source file.
    assert api.sent[0][1]["message"][0]["data"]["file"].startswith("base64://")
    assert all(upload[3].startswith(b"\x89PNG") for upload in transport.uploads)


async def test_album_render_failure_does_not_block_or_resend_successful_plain_digest(
    store, task, settings, journal, tmp_path
):
    service, api, poster, transport, task = configured(store, task, settings, journal, tmp_path)
    poster.fail = True
    run = await ready(service, api, task)
    await service.step(task)
    saved = await store.call("run", run["id"])
    assert saved["status"] == "complete" and not saved["error"]
    assert len(api.sent) == 1 and api.sent[0][0] == "send_group_msg"
    assert not transport.uploads
    archives = await store.call("archive_rows", run["id"])
    assert all(row["state"] == "render" and row["failures"] == 1 for row in archives)
    assert all(row["next_try"] > NOW for row in archives)
    service.clock = lambda: NOW + 61
    await service.step(task)
    assert service.client.calls == 1 and len(api.sent) == 1
    assert all(row["failures"] == 2 for row in await store.call("archive_rows", run["id"]))


async def test_complete_plain_run_keeps_processing_archive_without_regeneration(
    store, task, settings, journal, tmp_path
):
    service, api, poster, transport, task = configured(store, task, settings, journal, tmp_path)
    run = await ready(service, api, task)
    await service.step(task)
    assert (await store.call("run", run["id"]))["status"] == "complete"
    assert await store.call("active", task.key, NOW) == []
    await service.step(task)
    await service.step(task)
    assert service.client.calls == 1
    assert poster.calls.count(IMAGE_MODE) == 1
    assert len(api.sent) == 1 and len(transport.uploads) == 2
    assert all(row["state"] == "uploaded" for row in await store.call("archive_rows", run["id"]))
    saved = await store.call("run", run["id"])
    assert saved["digest"]["group_name"] == "实际群名"
    assert saved["digest"]["activity"]["total_messages"] == 1


async def test_enabling_album_does_not_retroactively_archive_completed_runs(
    store, task, settings, journal, tmp_path
):
    service, api, _, transport, task = configured(
        store, task, settings, journal, tmp_path, mode=IMAGE_MODE, enabled=False
    )
    run = await ready(service, api, task)
    await service.step(task)
    assert (await store.call("run", run["id"]))["status"] == "complete"
    task = replace(task, album_enabled=True)
    previous = service.settings()
    service.settings = lambda: replace(previous, tasks=(task,))
    await service.step(task)
    assert await store.call("archive_rows", run["id"]) == []
    assert service.client.calls == 1 and len(api.sent) == 1
    assert not transport.uploads


async def test_empty_summary_does_not_upload_an_activity_only_poster(
    store, task, settings, journal, tmp_path
):
    service, api, _, transport, task = configured(store, task, settings, journal, tmp_path, mode=IMAGE_MODE)
    run = await ready(service, api, task)

    class EmptyClient:
        async def generate(self, *args, **kwargs):
            return '{"items":[]}'

    service.client = EmptyClient()
    await service.step(task)
    assert (await store.call("run", run["id"]))["status"] == "complete"
    assert await store.call("archive_rows", run["id"]) == []
    assert not api.sent and not transport.uploads


async def test_same_group_receives_album_before_image_and_caption(store, task, settings, journal, tmp_path):
    service, api, _, transport, task = configured(store, task, settings, journal, tmp_path, mode=IMAGE_MODE)
    task = replace(task, album_target_groups=task.target_groups, poster_followup_text="鼠群日报，请查收～")
    initial = service.settings()
    service.settings = lambda: replace(initial, tasks=(task,))
    original = api.transport

    async def track_send(action, **params):
        kind = params["message"][0]["type"]
        transport.events.append((kind, params["group_id"]))
        return await original(action, **params)

    api.transport = track_send
    run = await ready(service, api, task)
    await service.step(task)
    await service.step(task)
    assert transport.events == [
        ("album", task.targets[0]),
        ("image", task.targets[0]),
        ("text", task.targets[0]),
    ]
    assert (await store.call("run", run["id"]))["status"] == "complete"
    assert service.client.calls == 1


async def test_unknown_album_upload_blocks_only_matching_message_target(
    store, task, settings, journal, tmp_path
):
    service, api, _, _, task = configured(store, task, settings, journal, tmp_path, mode=IMAGE_MODE)
    task = replace(task, target_groups=("987654321", "666666666"), album_target_groups=("987654321",))
    initial = service.settings()
    service.settings = lambda: replace(initial, tasks=(task,))

    class UnknownUpload(Transport):
        async def upload(self, group, album_id, name, file, description, *, on_submit):
            await on_submit({"operation": "upload", "file_name": file.name, "md5": "picture-md5"})
            self.uploads.append(group)
            raise TimeoutError("simulated response loss")

    transport = UnknownUpload()
    service.album.transport = lambda adapter, log: transport
    run = await ready(service, api, task)
    await service.step(task)
    await service.step(task)
    assert [params["group_id"] for _, params in api.sent] == ["666666666"]
    assert transport.uploads == ["987654321"]
    assert (await store.call("archive_rows", run["id"]))[0]["state"] == "unknown"
    deliveries = await store.call("deliveries", run["id"])
    assert {row["target"]: row["state"] for row in deliveries} == {
        "666666666": "sent",
        "987654321": "pending",
    }
    assert service.client.calls == 1
