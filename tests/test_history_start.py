from types import MethodType

import pytest

from qq_group_digest.config import IncompleteHistory
from qq_group_digest.history import HISTORY_START_NOTE, HistoryReader
from qq_group_digest.history_cache import HistoryCache
from qq_group_digest.platform import Adapter
from qq_group_digest.schedule import latest_boundary

from .conftest import NOW, raw_message
from .test_history import HistoryAPI
from .test_service import make_service


class BoundaryAPI(HistoryAPI):
    pid = "history-boundary-test"
    confirm_history_start = Adapter.confirm_history_start

    def __init__(self, pages):
        super().__init__(pages)
        self.requests = []

    async def history_page(self, group, count, cursor=None, **kwargs):
        self.requests.append((count, cursor, kwargs.get("generation")))
        return await super().history_page(group, count, cursor, **kwargs)


async def test_confirmed_anchor_only_boundary_collects_available_messages(task, settings, journal):
    newer, first = raw_message(12, NOW - 1), raw_message(9, NOW - 30)
    api = BoundaryAPI([[newer, first], [first], [first], [first]])
    result, notes = await HistoryReader(settings.limits, journal).read(api, task, NOW - 100, NOW)
    assert [message.message_id for message in result] == ["9", "12"]
    assert HISTORY_START_NOTE in notes
    assert api.requests[-2:] == [(2, "9", 1), (200, "9", 1)]
    assert any(event == "可读取历史起点已确认" for event, _ in journal.events)


@pytest.mark.parametrize(
    "probe", [[], [raw_message(8, NOW - 31)], [raw_message(9, NOW - 30), raw_message(8, NOW - 31)]]
)
async def test_empty_different_anchor_or_older_message_does_not_confirm_start(task, settings, journal, probe):
    first = raw_message(9, NOW - 30)
    api = BoundaryAPI([[first], [first], probe])
    with pytest.raises(IncompleteHistory, match="前进"):
        await HistoryReader(settings.limits, journal).read(api, task, NOW - 100, NOW)


async def test_wider_probe_catches_false_boundary_in_small_probe(task, settings, journal):
    first, older = raw_message(9, NOW - 30), raw_message(8, NOW - 40)
    api = BoundaryAPI([[first], [first], [first], [older, first]])
    with pytest.raises(IncompleteHistory, match="前进"):
        await HistoryReader(settings.limits, journal).read(api, task, NOW - 100, NOW)


async def test_repeated_multi_message_page_is_not_a_history_start(task, settings, journal):
    api = BoundaryAPI([[raw_message(9, NOW - 30), raw_message(10, NOW - 10)]])
    with pytest.raises(IncompleteHistory, match="前进"):
        await HistoryReader(settings.limits, journal).read(api, task, NOW - 100, NOW)
    assert len(api.requests) == 2  # Do not reinterpret a stuck middle page as a boundary.


async def test_partial_start_is_not_cached_as_an_earlier_complete_window(store, task, settings, journal):
    first = raw_message(9, NOW - 70)
    api = BoundaryAPI([[first], [first], [first], [first]])
    cache = HistoryCache(store, 30, journal, clock=lambda: NOW)
    await HistoryReader(settings.limits, journal, cache=cache).read(api, task, NOW - 100, NOW)
    saved = await store.call("history_cache_get", cache.scope(api, task))
    assert saved["start"] == NOW - 70
    assert await cache.load(api, task, NOW - 100, NOW) is None


async def test_probe_reconnect_does_not_accept_or_cache_boundary(store, task, settings, journal):
    from qq_group_digest.config import Deferred

    class Reconnected(BoundaryAPI):
        async def history_page(self, group, count, cursor=None, **kwargs):
            if count == 2:
                raise Deferred("读取历史时连接发生变化，将从第一页重新读取。")
            return await super().history_page(group, count, cursor, **kwargs)

    cache = HistoryCache(store, 30, journal, clock=lambda: NOW)
    api = Reconnected([[raw_message(9, NOW - 70)]])
    with pytest.raises(Deferred):
        await HistoryReader(settings.limits, journal, cache=cache).read(api, task, NOW - 100, NOW)
    assert await store.call("history_cache_get", cache.scope(api, task)) is None


async def test_current_stalled_batch_is_retried_once_and_completes(store, task, settings, journal):
    service, api = make_service(store, settings, journal)
    boundary = latest_boundary(task, NOW)
    rid = await store.call(
        "create_run", task.key, boundary - 86400, boundary, boundary - 87000, task.dump(), [], NOW
    )
    for _ in range(3):
        await store.call("fail", rid, "历史翻页没有继续前进，已停止本期处理。", NOW, 3)
    assert (await store.call("run", rid))["status"] == "failed"
    assert await store.call("resume_history_start_failure", task.key, boundary) == rid
    assert await store.call("resume_history_start_failure", task.key, boundary) is None

    first = raw_message(9, boundary - 30, "公开招生通知已更新，可查看学院官网。")
    replay = BoundaryAPI([[first]])
    api.history_page = replay.history_page
    api.confirm_history_start = MethodType(Adapter.confirm_history_start, api)
    await service.step(task)
    run = await store.call("run", rid)
    assert run["status"] == "complete" and service.client.calls == 1
    assert len(api.sent) == 1 and HISTORY_START_NOTE in run["notes"]
    await service.step(task)
    assert len(api.sent) == 1 and service.client.calls == 1


@pytest.mark.parametrize("condition", ["old", "different_error", "snapshot"])
async def test_upgrade_retry_does_not_touch_unrelated_failed_work(store, task, settings, condition):
    boundary = latest_boundary(task, NOW)
    end = boundary - 86400 if condition == "old" else boundary
    rid = await store.call("create_run", task.key, end - 86400, end, end - 87000, task.dump(), [], NOW)
    if condition == "snapshot":
        await store.call("fetched", rid, [], [])
    error = "模型摘要失败" if condition == "different_error" else "历史翻页没有继续前进，已停止本期处理。"
    for _ in range(3):
        await store.call("fail", rid, error, NOW, 3)
    assert await store.call("resume_history_start_failure", task.key, boundary) is None
    assert (await store.call("run", rid))["status"] == "failed"
