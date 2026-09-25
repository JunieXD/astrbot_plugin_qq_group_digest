import asyncio
import threading

import pytest

from qq_group_digest.config import Deferred, DigestError
from qq_group_digest.store import Store

from .conftest import NOW


async def make_run(store, task, end=NOW):
    rid = await store.call("create_run", task.key, end - 3600, end, end - 4200, task.dump(), [], end)
    await store.call("bind", rid, "111111111", "qq-test")
    return rid


async def prepare(store, task):
    rid = await make_run(store, task)
    await store.call(
        "generated",
        rid,
        {"items": [], "notes": []},
        [
            {"target": "987654321", "part": 0, "mode": "普通消息·整篇", "payload": {"message": []}},
            {"target": "888888888", "part": 0, "mode": "普通消息·整篇", "payload": {"message": []}},
        ],
    )
    return rid


async def test_round_idempotence_and_rename(store, task):
    first = await make_run(store, task)
    second = await make_run(store, task)
    assert first == second
    assert len(await store.call("latest", task.key)) == 1
    assert await store.call("get", "cursor:" + task.key) == {"end": NOW, "first": False}


async def test_write_intent_recovered_as_unknown(tmp_path, task):
    path = tmp_path / "state.sqlite3"
    first = Store(path)
    await first.call("open_db")
    rid = await prepare(first, task)
    did = f"{rid}:987654321:0"
    assert await first.call("submit", did, "111111111", NOW, 10)
    await first.close()
    second = Store(path)
    await second.call("open_db")
    try:
        rows = await second.call("deliveries", rid)
        row = next(r for r in rows if r["id"] == did)
        assert row["state"] == "unknown"
        assert not await second.call("submit", did, "111111111", NOW, 10)
    finally:
        await second.close()


async def test_send_quota_and_intent_are_one_transaction(store, task):
    rid = await prepare(store, task)
    await store.call("submit", f"{rid}:987654321:0", "111111111", NOW, 1)
    with pytest.raises(Deferred):
        await store.call("submit", f"{rid}:888888888:0", "111111111", NOW, 1)
    row = next(r for r in await store.call("deliveries", rid) if r["target"] == "888888888")
    assert row["state"] == "pending"


async def test_one_destination_failure_does_not_erase_other_progress(store, task):
    rid = await prepare(store, task)
    await store.call("delivery_result", f"{rid}:987654321:0", "sent", "123")
    await store.call("delivery_result", f"{rid}:888888888:0", "unknown")
    await store.call("finish", rid)
    assert (await store.call("run", rid))["status"] == "generated"
    await store.call("skip", rid, "888888888")
    assert (await store.call("run", rid))["status"] == "complete"
    assert (
        next(r for r in await store.call("deliveries", rid) if r["target"] == "987654321")["message_id"]
        == "123"
    )


async def test_budget_rolls_off_and_persists(store):
    await store.call("reserve_budget", "read", "qq", NOW, 3600, 1)
    with pytest.raises(Deferred):
        await store.call("reserve_budget", "read", "qq", NOW + 1, 3600, 1)
    await store.call("reserve_budget", "read", "qq", NOW + 3601, 3600, 1)


async def test_retention_preserves_unresolved_delivery(store, task):
    rid = await prepare(store, task)
    await store.call("delivery_result", f"{rid}:987654321:0", "unknown")
    await store.call("cleanup", NOW + 60 * 86400, 2, 30)
    assert await store.call("run", rid) is not None


async def test_cancelled_transaction_finishes_before_close(store):
    began, release = threading.Event(), threading.Event()

    def slow_set():
        began.set()
        release.wait(2)
        store.set("value", "durable")

    store.slow_set = slow_set
    task = asyncio.create_task(store.call("slow_set"))
    await asyncio.to_thread(began.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await store.call("get", "value") == "durable"


async def test_account_binding_persists_across_rounds(store, task):
    await make_run(store, task)
    rid = await store.call("create_run", task.key, NOW, NOW + 3600, NOW, task.dump(), [], NOW)
    from qq_group_digest.config import DigestError

    with pytest.raises(DigestError, match="改变"):
        await store.call("bind", rid, "333333333", "another-platform")


async def test_model_round_limit_survives_reload_and_manual_retry_keeps_daily_limit(tmp_path, task):
    path = tmp_path / "model-budget.sqlite3"
    first = Store(path)
    await first.call("open_db")
    rid = await make_run(first, task)
    await first.call("reserve_llm", rid, NOW, 2, 1)
    await first.close()
    second = Store(path)
    await second.call("open_db")
    try:
        with pytest.raises(DigestError, match="本期模型"):
            await second.call("reserve_llm", rid, NOW + 1, 2, 1)
        await second.call("fail", rid, "模型中断", 0, 1)
        await second.call("retry", rid)
        await second.call("reserve_llm", rid, NOW + 2, 2, 1)
        await second.call("fail", rid, "模型中断", 0, 1)
        await second.call("retry", rid)
        with pytest.raises(Deferred, match="配置额度"):
            await second.call("reserve_llm", rid, NOW + 3, 2, 1)
        assert (await second.call("run", rid))["llm_calls"] == 0
    finally:
        await second.close()


async def test_preview_shares_daily_model_budget_with_scheduled_round(store, task):
    rid = await make_run(store, task)
    await store.call("reserve_llm", None, NOW, 1, 10)
    with pytest.raises(Deferred):
        await store.call("reserve_llm", rid, NOW + 1, 1, 10)
    assert (await store.call("run", rid))["llm_calls"] == 0
