import asyncio
from dataclasses import replace

import pytest

from qq_group_digest.album_transport import AlbumUncertain
from qq_group_digest.albums import archive_plan
from qq_group_digest.models import Digest, Item

from .conftest import NOW
from .test_service import make_service
from .test_store import make_run


class Transport:
    album = "album-one"
    upload_error = None
    create_error = None
    writes = None
    media = None

    def __init__(self, adapter, journal):
        self.adapter = adapter

    async def find_album(self, group, name):
        return self.album

    async def create_album(self, group, name, *, on_submit):
        await on_submit({"operation": "create", "group_id": group, "name": name})
        self.writes.append(("create", group))
        if self.create_error:
            raise self.create_error
        type(self).album = "album-one"
        return self.album

    async def upload(self, group, aid, name, path, description, *, on_submit):
        receipt = {
            "operation": "upload",
            "album_id": aid,
            "title": path.name,
            "description": description,
            "batch_id": "1",
            "uploader": self.adapter.account,
        }
        await on_submit(receipt)
        self.writes.append(("upload", group))
        if self.upload_error:
            raise self.upload_error
        return {**receipt, "photo_id": "photo-one"}

    async def confirm(self, *args):
        return self.media


async def prepared(store, task, settings, journal, *, transport=Transport, targets=None, pages=1):
    task = replace(task, album_enabled=True, album_target_groups=targets or ("777777777",))
    service, api = make_service(store, replace(settings, tasks=(task,)), journal)
    service.album.transport = transport
    transport.writes = []
    transport.album = "album-one"
    transport.upload_error = transport.create_error = transport.media = None
    rid = await make_run(store, task)
    digest = Digest([Item("有价值的信息", "正文", ())])
    payloads = [
        {"message": [{"type": "image", "data": {"file": f"file:///poster-{page}.png"}}]}
        for page in range(pages)
    ]
    plan = archive_plan(task, digest, NOW - 3600, NOW, payloads)
    await store.call("generated", rid, digest.dump(), [], plan)
    return service, task, rid, api


async def test_already_complete_run_archives_once_and_reuses_exact_poster(store, task, settings, journal):
    s, task, rid, _ = await prepared(store, task, settings, journal)
    assert (await store.call("run", rid))["status"] == "complete"
    await s.album.process(task)
    await s.album.process(task)
    row = (await store.call("archive_rows", rid))[0]
    assert row["state"] == "uploaded"
    assert row["file"] == "file:///poster-0.png"
    assert row["receipt"]["photo_id"] == "photo-one"
    assert Transport.writes == [("upload", task.album_target_groups[0])]
    assert s.client.calls == 0


async def test_new_album_create_and_upload_are_separate_paced_writes(store, task, settings, journal):
    s, task, rid, _ = await prepared(store, task, settings, journal)
    Transport.album = None
    await s.album.process(task)
    row = (await store.call("archive_rows", rid))[0]
    assert row["state"] == "pending" and row["album_id"] == "album-one"
    assert row["receipt"] == {}
    await s.album.process(task)
    assert [operation for operation, _ in Transport.writes] == ["create", "upload"]
    assert (await store.call("archive_rows", rid))[0]["state"] == "uploaded"


async def test_unknown_upload_stops_later_pages_but_other_group_continues(store, task, settings, journal):
    class PerTarget(Transport):
        async def upload(self, group, *args, **kwargs):
            self.upload_error = TimeoutError() if group == "777777777" else None
            return await super().upload(group, *args, **kwargs)

    s, task, rid, _ = await prepared(
        store, task, settings, journal, transport=PerTarget, targets=("777777777", "888888888"), pages=2
    )
    await s.album.process(task)
    await s.album.process(task)
    rows = await store.call("archive_rows", rid)
    assert [row["state"] for row in rows if row["target"] == "777777777"] == ["unknown", "pending"]
    assert [row["state"] for row in rows if row["target"] == "888888888"] == ["uploaded", "uploaded"]
    assert len(PerTarget.writes) == 3
    assert await store.call("archive_retry", rid) == 0


async def test_cancelled_upload_persists_receipt_and_can_be_confirmed_without_resending(
    store, task, settings, journal
):
    s, task, rid, _ = await prepared(store, task, settings, journal)
    Transport.upload_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await s.album.process(task)
    row = (await store.call("archive_rows", rid))[0]
    assert row["state"] == "unknown" and row["receipt"]["operation"] == "upload"
    Transport.media = {"image": {"lloc": "photo-one"}}
    assert "确认成功 1" in await s.album.reconcile(task)
    await s.album.process(task)
    assert len(Transport.writes) == 1
    assert (await store.call("archive_rows", rid))[0]["state"] == "uploaded"


async def test_unknown_create_reconciles_then_uploads_without_recreating(store, task, settings, journal):
    s, task, rid, _ = await prepared(store, task, settings, journal)
    Transport.album = None
    Transport.create_error = AlbumUncertain("unknown", receipt={"operation": "create"})
    await s.album.process(task)
    assert (await store.call("archive_rows", rid))[0]["state"] == "unknown"
    Transport.album = "album-one"
    await s.album.reconcile(task)
    await s.album.process(task)
    assert [operation for operation, _ in Transport.writes] == ["create", "upload"]


async def test_recovery_pause_before_submission_never_writes_or_counts_failure(
    store, task, settings, journal
):
    from qq_group_digest.config import Deferred

    s, task, rid, api = await prepared(store, task, settings, journal)

    async def waiting():
        raise Deferred("connection recovering", 120)

    api.ready_to_send = waiting
    await s.album.process(task)
    row = (await store.call("archive_rows", rid))[0]
    assert row["state"] == "pending" and row["next_try"] == NOW + 120
    assert row["failures"] == 0
    assert not Transport.writes


async def test_last_minute_disable_and_send_quota_prevent_platform_write(store, task, settings, journal):
    from .test_delivery import Guard

    s, task, rid, _ = await prepared(store, task, settings, journal)

    class DisableGuard(Guard):
        async def run(self, **kwargs):
            s.settings = lambda: replace(settings, tasks=(replace(task, album_enabled=False),))
            return await super().run(**kwargs)

    s.guard = DisableGuard()
    await s.album.process(task)
    assert not Transport.writes
    row = (await store.call("archive_rows", rid))[0]
    assert row["state"] == "pending" and row["failures"] == 0


async def test_guard_failure_after_success_never_loses_success_or_repeats(store, task, settings, journal):
    from .test_delivery import Guard

    s, task, rid, _ = await prepared(store, task, settings, journal)

    class FailedGuardSave(Guard):
        async def run(self, **kwargs):
            await kwargs["action"]()
            raise OSError("guard state failure")

    s.guard = FailedGuardSave()
    await s.album.process(task)
    await s.album.process(task)
    assert len(Transport.writes) == 1
    assert (await store.call("archive_rows", rid))[0]["state"] == "uploaded"


async def test_removed_target_and_renamed_album_cancel_only_unsubmitted_intents(
    store, task, settings, journal
):
    s, task, rid, _ = await prepared(store, task, settings, journal)
    renamed = replace(task, album_name="新日报")
    s.settings = lambda: replace(settings, tasks=(renamed,))
    await s.album.process(renamed)
    assert (await store.call("archive_rows", rid))[0]["state"] == "skipped"
    assert not Transport.writes


async def test_manual_archive_is_idempotent_and_target_must_be_configured(store, task, settings, journal):
    from qq_group_digest.config import DigestError

    s, task, rid, _ = await prepared(store, task, settings, journal)
    run = await store.call("run", rid)
    assert "0 张" in await s.album.plan_existing(run, task)
    with pytest.raises(DigestError, match="不在"):
        await s.album.plan_existing(run, task, "999999999")
    assert len(await store.call("archive_rows", rid)) == 1
