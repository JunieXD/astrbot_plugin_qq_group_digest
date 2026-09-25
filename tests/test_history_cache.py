import asyncio
from dataclasses import replace

import pytest

from qq_group_digest.config import IncompleteHistory, Limits
from qq_group_digest.history import HistoryReader
from qq_group_digest.history_cache import HistoryCache
from qq_group_digest.store import Store

from .conftest import NOW, raw_message
from .test_history import HistoryAPI


class API(HistoryAPI):
    pid = "qq-test"


def pages():
    return [
        [raw_message(9, NOW - 10), raw_message(8, NOW - 70)],
        [raw_message(8, NOW - 70), raw_message(7, NOW - 400)],
        [raw_message(7, NOW - 400), raw_message(1, NOW - 1001)],
    ]


async def seed(store, task, journal):
    cache = HistoryCache(store, 30, journal, clock=lambda: NOW)
    api = API(pages())
    messages, _ = await HistoryReader(Limits(), journal, cache=cache).read(api, task, NOW - 1000, NOW)
    assert len(api.cursors) == 3
    return cache, messages


async def test_incremental_preview_reuses_prefix_and_replaces_tail(store, task, journal):
    cache, original = await seed(store, task, journal)
    cache.clock = lambda: NOW + 90
    # Old message 9 was withdrawn. Refresh the last minute, and use fresh short IDs.
    recent = raw_message(101, NOW + 10)
    boundary = raw_message(102, NOW - 70, seq=8)
    api = API([[recent, boundary]])
    result, _ = await HistoryReader(Limits(), journal, cache=cache).read(api, task, NOW - 910, NOW + 90)
    assert [m.seq for m in result] == [7, 8, 101]
    assert api.cursors == [None]  # Never query using cached NapCat short IDs.
    assert original[-1].seq == 9
    stored = await store.call("history_cache_get", cache.scope(api, task))
    assert stored["captured"] == NOW  # Repeated hits cannot extend stale content forever.


@pytest.mark.parametrize("change", ["expired", "earlier", "account", "platform", "names", "off", "refresh"])
async def test_invalid_or_bypassed_cache_requires_complete_read(store, task, journal, change):
    cache, _ = await seed(store, task, journal)
    start, end = NOW - 1000, NOW
    api = API(pages())
    if change == "expired":
        cache.clock = lambda: NOW + 1800
    elif change == "earlier":
        start -= 1
        api.pages[-1][-1]["time"] = start - 1
    elif change == "account":
        api.account = "777777777"
    elif change == "platform":
        api.pid = "another-platform"
    elif change == "names":
        task = replace(task, attribute_speakers=True)
    elif change == "off":
        cache.minutes = 0
    await HistoryReader(Limits(), journal, cache=cache).read(
        api, task, start, end, refresh=change == "refresh"
    )
    assert len(api.cursors) == 3


async def test_failure_does_not_make_cache_coverage_complete(store, task, journal):
    cache = HistoryCache(store, 30, journal, clock=lambda: NOW)
    with pytest.raises(IncompleteHistory):
        await HistoryReader(replace(Limits(), max_pages=1), journal, cache=cache).read(
            API(pages()), task, NOW - 1000, NOW
        )
    assert await cache.load(API([]), task, NOW - 1000, NOW) is None
    await seed(store, task, journal)
    # A cache hit must still prove coverage of the new tail, even for an empty page.
    with pytest.raises(IncompleteHistory):
        await HistoryReader(Limits(), journal, cache=cache).read(API([[]]), task, NOW - 1000, NOW + 90)
    hit = await cache.load(API([]), task, NOW - 1000, NOW)
    assert hit is not None


async def test_cancelled_read_never_stores_partial_coverage(store, task, journal):
    class CancelAPI(API):
        async def history_page(self, *args, **kwargs):
            raise asyncio.CancelledError

    cache = HistoryCache(store, 30, journal, clock=lambda: NOW)
    with pytest.raises(asyncio.CancelledError):
        await HistoryReader(Limits(), journal, cache=cache).read(CancelAPI([]), task, NOW - 1000, NOW)
    assert await cache.load(API([]), task, NOW - 1000, NOW) is None


async def test_cache_still_obeys_new_message_and_text_limits(store, task, journal):
    cache, _ = await seed(store, task, journal)
    limits = replace(Limits(), max_messages=1)
    api = API(pages())
    with pytest.raises(IncompleteHistory, match="缓存"):
        await HistoryReader(limits, journal, cache=cache).read(api, task, NOW - 1000, NOW)
    assert not api.cursors


async def test_cache_persists_across_store_reopen_and_expires(tmp_path, task, journal):
    path = tmp_path / "cache.sqlite3"
    store = Store(path)
    await store.call("open_db")
    await seed(store, task, journal)
    await store.close()
    store = Store(path)
    await store.call("open_db")
    try:
        cache = HistoryCache(store, 30, journal, clock=lambda: NOW + 90)
        assert await cache.load(API([]), task, NOW - 1000, NOW + 90)
        await store.call("cleanup", NOW + 1800, 2, 30)
        assert await store.call("history_cache_get", cache.scope(API([]), task)) is None
    finally:
        await store.close()


async def test_forward_text_is_not_saved_in_the_raw_history_cache(store, task, journal):
    class ForwardAPI(API):
        async def read(self, action, **kwargs):
            return {"messages": [{"content": [{"type": "text", "data": {"text": "展开后内容"}}]}]}

    source = raw_message(7, NOW - 400)
    source["message"].append({"type": "forward", "data": {"id": "forward-id"}})
    cache = HistoryCache(store, 30, journal, clock=lambda: NOW)
    task = replace(task, read_forwards=True)
    result, _ = await HistoryReader(Limits(), journal, cache=cache).read(
        ForwardAPI([[source, raw_message(1, NOW - 1001)]]), task, NOW - 1000, NOW
    )
    assert "展开后内容" in result[0].text
    prefix, _, _ = await cache.load(API([]), task, NOW - 1000, NOW)
    assert "展开后内容" not in prefix[0].text


async def test_refresh_command_requests_full_history(store, task, settings, journal):
    from qq_group_digest.commands import Commands

    from .test_service import make_service

    service, api = make_service(store, settings, journal)
    cache = HistoryCache(store, 30, journal, clock=lambda: NOW)
    # Cache an empty window that would otherwise allow skipping older messages.
    await cache.save(api, task, NOW - 86400, NOW, [], NOW)
    api.history = [raw_message(7, NOW - 400), raw_message(1, NOW - 86401)]
    output = await Commands(service).run("群摘要 预览 " + task.source_group + " 刷新")
    assert "预览结果" in output
