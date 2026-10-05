import json
import sqlite3
from dataclasses import replace

import pytest

from qq_group_digest.config import Deferred, DigestError
from qq_group_digest.store import Store

from .conftest import NOW
from .test_store import make_run


def plan(target="987654321", part=0, file="file:///poster.png"):
    return {
        "target": target,
        "part": part,
        "file": file,
        "album_name": "鼠群日报",
        "description": "10月4日 10:00—10月5日 10:00",
    }


async def prepare(store, task, archives=None, deliveries=()):
    rid = await make_run(store, task)
    await store.call("fetched", rid, {"messages": [{"text": "原始证据"}]}, [])
    await store.call("generated", rid, {"items": [], "notes": []}, deliveries, archives or [plan()])
    return rid


async def test_generated_album_plan_is_atomic_with_digest_and_deliveries(store, task):
    rid = await make_run(store, task)
    delivery = {"target": "987654321", "part": 0, "mode": "图片海报", "payload": {"message": []}}
    with pytest.raises(DigestError):
        await store.call("generated", rid, {"items": []}, [delivery], [plan(), plan()])
    row = await store.call("run", rid)
    assert row["status"] == "queued" and row["digest"] is None
    assert await store.call("deliveries", rid) == []
    assert await store.call("archive_rows", rid) == []


async def test_archive_plan_idempotence_and_associated_run_fields(store, task):
    rid = await prepare(store, task)
    await store.call("generated", rid, {"changed": True}, [], [plan("888888888")])
    rows = await store.call("archive_active", task.key, NOW)
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == f"{rid}:album:987654321:0"
    assert row["account"] == "111111111" and row["platform"] == "qq-test"
    assert row["config"] == json.loads(json.dumps(task.dump())) and row["receipt"] == {}
    assert row["start"] == NOW - 3600 and row["end"] == NOW
    assert (await store.call("run", rid))["status"] == "complete"


async def test_migration_v1_to_v2_keeps_existing_runs_and_cursor(tmp_path, task):
    path = tmp_path / "old.sqlite3"
    first = Store(path)
    await first.call("open_db")
    rid = await make_run(first, task)
    await first.close()
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE archives")
        db.execute("PRAGMA user_version=1")
    second = Store(path)
    await second.call("open_db")
    try:
        assert (await second.call("run", rid))["account"] == "111111111"
        assert (await second.call("get", "cursor:" + task.key))["end"] == NOW
        await second.call("generated", rid, {"items": []}, [], [plan()])
        assert len(await second.call("archive_rows", rid)) == 1
    finally:
        await second.close()
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2


async def test_submitted_write_recovers_as_unknown_without_automatic_retry(tmp_path, task):
    path = tmp_path / "recovery.sqlite3"
    first = Store(path)
    await first.call("open_db")
    rid = await prepare(first, task)
    aid = (await first.call("archive_rows", rid))[0]["id"]
    assert await first.call("archive_submit", aid, "111111111", NOW, 20)
    await first.close()
    second = Store(path)
    await second.call("open_db")
    try:
        row = (await second.call("archive_rows", rid))[0]
        assert row["state"] == "unknown" and row["submitted"] == NOW
        assert await second.call("archive_active", task.key, NOW + 3600) == []
        assert await second.call("archive_retry", rid) == 0
        assert not await second.call("archive_submit", aid, "111111111", NOW + 1, 20)
        with pytest.raises(DigestError, match="结果不明"):
            await second.call("archive_result", aid, "pending", "album-id")
        rows, total = await second.call("archive_attention", task.key)
        assert total == 1 and rows[0]["id"] == aid
    finally:
        await second.close()


async def test_creation_and_upload_each_reserve_shared_send_budget(store, task):
    rid = await prepare(store, task)
    aid = (await store.call("archive_rows", rid))[0]["id"]
    await store.call("archive_submit", aid, "111111111", NOW, 2, {"operation": "create"})
    with pytest.raises(DigestError, match="未确认"):
        await store.call("archive_result", aid, "pending")
    assert await store.call("archive_created", aid, "album-id")
    assert await store.call("archive_submit", aid, "111111111", NOW + 1, 2)
    await store.call("archive_result", aid, "uploaded", "", {"photo_id": "photo-id"})
    row = (await store.call("archive_rows", rid))[0]
    assert row["album_id"] == "album-id" and row["receipt"] == {"photo_id": "photo-id"}
    with pytest.raises(Deferred):
        await store.call("reserve_budget", "send", "111111111", NOW + 2, 86400, 2)


async def test_quota_failure_never_marks_unsubmitted_target_or_leaks_reservation(store, task):
    rid = await prepare(store, task, [plan(), plan("888888888")])
    rows = await store.call("archive_rows", rid)
    with pytest.raises(DigestError, match="绑定"):
        await store.call("archive_submit", rows[0]["id"], "222222222", NOW, 1)
    assert await store.call("archive_submit", rows[0]["id"], "111111111", NOW, 1)
    with pytest.raises(Deferred):
        await store.call("archive_submit", rows[1]["id"], "111111111", NOW, 1)
    assert (await store.call("archive_rows", rid))[1]["state"] == "pending"


async def test_render_expands_pages_per_target_atomically_and_idempotently(store, task):
    rid = await prepare(store, task, [plan(file=""), plan("888888888", file="")])
    files = ["file:///page-one.png", "file:///page-two.png"]
    assert await store.call("archive_materialize", rid, files) == 4
    rows = await store.call("archive_rows", rid)
    assert [(row["part"], row["file"]) for row in rows] == list(enumerate(files)) * 2
    assert all(row["state"] == "pending" for row in rows)
    assert [row["description"] for row in rows] == [
        plan()["description"] + f" · 第{part + 1}/2页" for part in range(2)
    ] * 2
    assert await store.call("archive_materialize", rid, ["file:///new.png"]) == 0
    assert await store.call("archive_rows", rid) == rows


async def test_invalid_render_preserves_placeholder_and_can_be_deferred(store, task):
    rid = await prepare(store, task, [plan(file="")])
    row = (await store.call("archive_rows", rid))[0]
    with pytest.raises(DigestError, match="可用"):
        await store.call("archive_materialize", rid, [])
    with pytest.raises(DigestError, match="尚未生成"):
        await store.call("archive_result", row["id"], "pending", "album-id")
    await store.call("archive_defer", row["id"], "渲染暂不可用", NOW + 100, 2, False)
    assert await store.call("archive_active", task.key, NOW) == []
    assert (await store.call("archive_active", task.key, NOW + 101))[0]["state"] == "render"
    await store.call("archive_defer", row["id"], "渲染失败", NOW + 200, 1)
    assert (await store.call("archive_rows", rid))[0]["state"] == "blocked"
    assert await store.call("archive_retry", rid) == 1
    assert (await store.call("archive_rows", rid))[0]["state"] == "render"


async def test_target_progress_is_independent_of_messages_and_other_archives(store, task):
    delivery = {"target": "987654321", "part": 0, "mode": "图片海报", "payload": {"message": []}}
    rid = await prepare(store, task, [plan(), plan("888888888")], [delivery])
    rows = await store.call("archive_rows", rid)
    await store.call("archive_result", rows[0]["id"], "uploaded", "album", {"photo_id": "one"})
    await store.call("archive_defer", rows[1]["id"], "相册权限不足", NOW + 200, 1)
    await store.call("delivery_result", f"{rid}:987654321:0", "sent", "message-id")
    await store.call("finish", rid)
    assert (await store.call("run", rid))["status"] == "complete"
    assert await store.call("active", task.key, NOW + 1000) == []
    assert await store.call("archive_retry", rid) == 1
    states = [row["state"] for row in await store.call("archive_rows", rid)]
    assert states == ["uploaded", "pending"]
    assert (await store.call("deliveries", rid))[0]["message_id"] == "message-id"
    assert not await store.call("archive_result", rows[0]["id"], "unknown", "", None, "迟到的失败")


async def test_cancel_removed_targets_does_not_cancel_submitted_or_unknown(store, task):
    rid = await prepare(
        store,
        task,
        [plan("111111111", file=""), plan("222222222"), plan("333333333"), plan("444444444")],
    )
    rows = await store.call("archive_rows", rid)
    await store.call("archive_defer", rows[1]["id"], "准备失败", NOW, 1)
    await store.call("archive_submit", rows[2]["id"], "111111111", NOW, 10)
    await store.call("archive_submit", rows[3]["id"], "111111111", NOW, 10)
    await store.call("archive_result", rows[3]["id"], "unknown")
    assert await store.call("archive_cancel_removed", task.key, []) == 2
    assert [row["state"] for row in await store.call("archive_rows", rid)] == [
        "skipped",
        "skipped",
        "submitted",
        "unknown",
    ]
    assert await store.call("archive_skip", rid) == 1
    assert (await store.call("archive_rows", rid))[2]["state"] == "submitted"


async def test_cancel_keeps_selected_target_and_unrelated_task(store, task):
    rid = await prepare(store, task, [plan(), plan("888888888")])
    assert await store.call("archive_cancel_removed", "other-task", []) == 0
    assert await store.call("archive_cancel_removed", task.key, ["888888888"]) == 1
    rows = await store.call("archive_rows", rid)
    assert [(row["target"], row["state"]) for row in rows] == [
        ("888888888", "pending"),
        ("987654321", "skipped"),
    ]


async def test_cleanup_keeps_archive_run_snapshot_and_file_after_message_completion(store, task):
    rid = await prepare(store, task)
    await store.call("cleanup", NOW + 60 * 86400, 2, 30)
    row = await store.call("run", rid)
    assert row is not None and row["snapshot"] is not None
    assert await store.call("protected_image_files") == {"file:///poster.png"}
    await store.call("archive_skip", rid)
    assert await store.call("protected_image_files") == set()
    await store.call("cleanup", NOW + 60 * 86400, 2, 30)
    assert await store.call("run", rid) is None


async def test_protected_files_union_message_and_unresolved_album_paths(store, task):
    delivery = {
        "target": "987654321",
        "part": 0,
        "mode": "图片海报",
        "payload": {"message": [{"type": "image", "data": {"file": "file:///message.png"}}]},
    }
    rid = await prepare(store, task, deliveries=[delivery])
    assert await store.call("protected_image_files") == {"file:///poster.png", "file:///message.png"}
    aid = (await store.call("archive_rows", rid))[0]["id"]
    await store.call("archive_result", aid, "uploaded", "", {"photo_id": "proof"})
    assert await store.call("protected_image_files") == {"file:///message.png"}


async def test_archive_error_cannot_defer_an_already_submitted_write(store, task):
    rid = await prepare(store, task)
    aid = (await store.call("archive_rows", rid))[0]["id"]
    await store.call("archive_submit", aid, "111111111", NOW, 10)
    await store.call("archive_defer", aid, "请求超时", NOW + 10, 1)
    assert (await store.call("archive_rows", rid))[0]["state"] == "submitted"
    await store.call("archive_result", aid, "unknown", "", None, "请求超时")
    await store.call("archive_defer", aid, "请求超时", NOW + 20, 1)
    assert (await store.call("archive_rows", rid))[0]["state"] == "unknown"
    assert await store.call("archive_active", task.key, NOW + 1000) == []


async def test_unknown_can_be_resolved_with_read_only_upload_evidence(store, task):
    rid = await prepare(store, task)
    aid = (await store.call("archive_rows", rid))[0]["id"]
    await store.call("archive_submit", aid, "111111111", NOW, 10)
    await store.call("archive_result", aid, "unknown", "album-id", {"digest": "sha256"})
    await store.call("archive_result", aid, "uploaded", "", {"photo_id": "confirmed"})
    assert (await store.call("archive_rows", rid))[0]["album_id"] == "album-id"
    assert await store.call("archive_attention", task.key) == ([], 0)
    assert await store.call("archive_retry", rid) == 0


async def test_submission_saves_remote_intent_and_album_id_before_io(store, task):
    rid = await prepare(store, task)
    aid = (await store.call("archive_rows", rid))[0]["id"]
    intent = {"operation": "upload", "name": "日报.png", "md5": "expected-photo"}
    assert await store.call("archive_submit", aid, "111111111", NOW, 10, intent, "album-id")
    row = (await store.call("archive_rows", rid))[0]
    assert row["state"] == "submitted" and row["receipt"] == intent
    assert row["album_id"] == "album-id"
    with pytest.raises(DigestError, match="不是创建"):
        await store.call("archive_created", aid, "different-album")


async def test_confirmed_unknown_creation_can_resume_upload_without_recreating(store, task):
    rid = await prepare(store, task)
    aid = (await store.call("archive_rows", rid))[0]["id"]
    intent = {"operation": "create", "album_name": "鼠群日报"}
    await store.call("archive_submit", aid, "111111111", NOW, 10, intent)
    await store.call("archive_result", aid, "unknown", "", None, "创建响应丢失")
    assert await store.call("archive_created", aid, "confirmed-album-id")
    row = (await store.call("archive_rows", rid))[0]
    assert row["state"] == "pending" and row["album_id"] == "confirmed-album-id"
    assert row["receipt"] == {} and row["error"] == ""
    assert (await store.call("archive_active", task.key, NOW))[0]["id"] == aid
    assert not await store.call("archive_created", aid, "later-album-id")


async def test_explicit_archive_plan_keeps_successful_and_unknown_progress(store, task):
    rid = await prepare(store, task)
    aid = (await store.call("archive_rows", rid))[0]["id"]
    await store.call("archive_result", aid, "uploaded", "album-id", {"photo_id": "done"})
    assert await store.call("archive_plan", rid, [plan(), plan("888888888")]) == 1
    rows = await store.call("archive_rows", rid)
    assert [(row["target"], row["state"]) for row in rows] == [
        ("888888888", "pending"),
        ("987654321", "uploaded"),
    ]
    await store.call("archive_submit", rows[0]["id"], "111111111", NOW, 10)
    await store.call("archive_result", rows[0]["id"], "unknown")
    assert await store.call("archive_plan", rid, [plan("888888888")]) == 0
    assert (await store.call("archive_rows", rid))[0]["state"] == "unknown"


async def test_manual_archive_rejects_unfinished_generation(store, task):
    rid = await make_run(store, task)
    with pytest.raises(DigestError, match="尚未生成"):
        await store.call("archive_plan", rid, [plan()])
    assert await store.call("archive_rows", rid) == []


async def test_unknown_query_is_not_starved_by_earlier_blocked_rows(store, task):
    rid = await prepare(store, task, [plan(str(111111110 + part)) for part in range(7)])
    rows = await store.call("archive_rows", rid)
    for row in rows[:6]:
        await store.call("archive_defer", row["id"], "权限不足", NOW, 1)
    await store.call("archive_submit", rows[-1]["id"], "111111111", NOW, 10)
    await store.call("archive_result", rows[-1]["id"], "unknown")
    limited, total = await store.call("archive_attention", task.key)
    assert len(limited) == 5 and total == 7
    assert all(row["state"] == "blocked" for row in limited)
    unknown, total = await store.call("archive_unknown", task.key)
    assert total == 1 and unknown[0]["id"] == rows[-1]["id"]


async def test_enabling_album_on_fetched_plain_run_requires_names_in_snapshot(store, task):
    plain = replace(task, attribute_speakers=False, mode="普通消息·整篇", album_enabled=False)
    rid = await make_run(store, plain)
    await store.call("fetched", rid, [{"sender_name": ""}], [])
    await store.call("reconfigure", rid, replace(plain, album_enabled=True).dump())
    run = await store.call("run", rid)
    assert run["status"] == "queued" and run["snapshot"] is None
    assert run["config"]["album_enabled"]
