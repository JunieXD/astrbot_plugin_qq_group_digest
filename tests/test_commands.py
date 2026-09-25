from qq_group_digest.commands import Commands

from .conftest import NOW
from .test_service import make_service
from .test_store import make_run, prepare


async def test_old_unknown_batch_stays_visible_and_does_not_poll_network(store, task, settings, journal):
    rid = await prepare(store, task)
    for row in await store.call("deliveries", rid):
        await store.call("delivery_result", row["id"], "unknown")
    for i in range(1, 6):
        newer = await make_run(store, task, NOW + i * 3600)
        await store.call("generated", newer, {"items": [], "notes": []}, [])
    service, _ = make_service(store, settings, journal)
    assert rid[:12] in await Commands(service).run("群摘要 状态")
    assert await store.call("active", task.key, NOW + 30000) == []


async def test_admin_can_skip_failed_generation_without_affecting_other_rounds(
    store, task, settings, journal
):
    rid = await make_run(store, task)
    await store.call("fail", rid, "无可用模型", 0, 1)
    other = await make_run(store, task, NOW + 3600)
    service, api = make_service(store, settings, journal)
    result = await Commands(service).run("群摘要 跳过 " + rid[:12])
    assert "已跳过" in result
    assert (await store.call("run", rid))["status"] == "complete"
    assert (await store.call("run", other))["status"] == "queued"
    assert not api.sent
