from dataclasses import replace

from qq_group_digest.config import Limits
from qq_group_digest.forwards import ForwardReader
from qq_group_digest.history import HistoryReader
from qq_group_digest.models import Message

from .conftest import NOW, raw_message
from .test_history import HistoryAPI


async def test_forward_is_read_before_later_pages_can_evict_mapping(task, journal):
    class API(HistoryAPI):
        phase = 0
        reads = 0

        async def history_page(self, *args, **kwargs):
            self.phase += 1
            return await super().history_page(*args, **kwargs)

        async def read(self, action, **kwargs):
            assert self.phase == 1
            self.reads += 1
            return {"messages": [{"content": [{"type": "text", "data": {"text": "完整转发正文"}}]}]}

    raw = raw_message(9, NOW - 10)
    raw["message"].append({"type": "forward", "data": {"id": "1234567890123456789"}})
    api = API([[raw, raw_message(8, NOW - 50)], [raw_message(1, NOW - 1001)]])
    result, _ = await HistoryReader(Limits(), journal).read(
        api, replace(task, read_forwards=True), NOW - 1000, NOW
    )
    assert api.reads == 1 and "完整转发正文" in result[-1].text


async def test_cached_forward_content_survives_missing_native_mapping(store, task, journal):
    class API:
        pid, account = "qq", "111111"
        refreshes = reads = 0

        async def refresh_forward(self, group, fid):
            assert group == task.source_group and fid == "1234567890123456789"
            self.refreshes += 1

        async def read(self, action, **kwargs):
            self.reads += 1
            return {"messages": [{"content": [{"type": "text", "data": {"text": "已展开"}}]}]}

    api = API()
    api.store = store
    task = replace(task, read_forwards=True)
    message = Message("x", "1", NOW - 1, "222222", "[合并转发]", forward_ids=["1234567890123456789"])
    first = ForwardReader(api, task, Limits(), journal)
    await first.collect(message)
    assert api.refreshes == api.reads == 1
    second = ForwardReader(api, task, Limits(), journal)
    await second.collect(message)
    second.apply(message)
    assert api.refreshes == api.reads == 1 and second.hits == 1
    assert "已展开" in message.text
