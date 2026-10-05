import pytest

from qq_group_digest.album_transport import AlbumRejected

from .conftest import NOW
from .test_album_archive import Transport, prepared
from .test_delivery import Guard


@pytest.mark.parametrize("replacement", [None, "album-replacement"], ids=["deleted", "recreated"])
async def test_album_identity_stays_bound_when_album_changes_between_pages(
    store, task, settings, journal, replacement
):
    class ChangingAlbum(Transport):
        async def upload(self, *args, **kwargs):
            receipt = await super().upload(*args, **kwargs)
            type(self).album = replacement
            return receipt

    service, task, rid, _ = await prepared(store, task, settings, journal, transport=ChangingAlbum, pages=2)
    await service.album.process(task)
    rows = await store.call("archive_rows", rid)
    assert rows[0]["state"] == "uploaded" and rows[0]["album_id"] == "album-one"
    assert rows[1]["state"] == "pending" and "删除或更改" in rows[1]["error"]
    assert ChangingAlbum.writes == [("upload", task.album_target_groups[0])]


async def test_successfully_created_album_disappearing_is_not_recreated(store, task, settings, journal):
    service, task, rid, _ = await prepared(store, task, settings, journal)
    Transport.album = None
    await service.album.process(task)
    assert (await store.call("archive_rows", rid))[0]["album_id"] == "album-one"
    Transport.album = None
    await service.album.process(task)
    row = (await store.call("archive_rows", rid))[0]
    assert row["state"] == "pending" and "删除或更改" in row["error"]
    assert Transport.writes == [("create", task.album_target_groups[0])]


async def test_known_create_rejection_is_retryable_preparation_failure(store, task, settings, journal):
    service, task, rid, _ = await prepared(store, task, settings, journal)
    Transport.album = None
    Transport.create_error = AlbumRejected("QQ 明确拒绝了创建请求")
    await service.album.process(task)
    row = (await store.call("archive_rows", rid))[0]
    assert row["state"] == "blocked" and "明确拒绝" in row["error"]
    assert row["receipt"]["operation"] == "create"
    assert await store.call("archive_retry", rid) == 1
    Transport.create_error = None
    await service.album.process(task)
    await service.album.process(task)
    assert (await store.call("archive_rows", rid))[0]["state"] == "uploaded"
    assert [kind for kind, _ in Transport.writes] == ["create", "create", "upload"]


async def test_other_writer_creates_album_while_operation_waits_in_queue(store, task, settings, journal):
    service, task, rid, _ = await prepared(store, task, settings, journal)
    Transport.album = None

    class WaitingGuard(Guard):
        async def run(self, **kwargs):
            Transport.album = "another-writer-album"
            return await super().run(**kwargs)

    service.guard = WaitingGuard()
    await service.album.process(task)
    row = (await store.call("archive_rows", rid))[0]
    assert row["state"] == "pending" and row["album_id"] == "another-writer-album"
    assert not Transport.writes
    # The no-write recheck must not consume a creation reservation.
    await store.call("reserve_budget", "send", "111111111", NOW, 86400, 1)


async def test_account_change_after_queueing_prevents_archive_submission(store, task, settings, journal):
    service, task, rid, api = await prepared(store, task, settings, journal)

    class ChangedAccountGuard(Guard):
        async def run(self, **kwargs):
            api.account = "444444444"
            return await super().run(**kwargs)

    service.guard = ChangedAccountGuard()
    await service.album.process(task)
    row = (await store.call("archive_rows", rid))[0]
    assert row["state"] == "pending" and row["failures"] == 0
    assert not Transport.writes


async def test_persisted_pause_before_submission_prevents_archive_write(store, task, settings, journal):
    service, task, rid, _ = await prepared(store, task, settings, journal)

    class PausedGuard(Guard):
        async def run(self, **kwargs):
            await store.call("set", "paused:" + task.key, True)
            return await super().run(**kwargs)

    service.guard = PausedGuard()
    await service.album.process(task)
    row = (await store.call("archive_rows", rid))[0]
    assert row["state"] == "pending" and row["failures"] == 0
    assert not Transport.writes
