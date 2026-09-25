from dataclasses import replace

import pytest

from qq_group_digest.config import IncompleteHistory, Limits
from qq_group_digest.history import HistoryReader, flatten, normalize

from .conftest import NOW, raw_message


class HistoryAPI:
    account = "111111111"

    def __init__(self, pages):
        self.pages = pages
        self.cursors = []

    async def history_page(self, group, count, cursor=None):
        self.cursors.append(cursor)
        return self.pages[min(len(self.cursors) - 1, len(self.pages) - 1)]


async def test_cursor_dedup_boundaries_and_own_messages(task, journal):
    newer = raw_message(900, NOW + 1)
    own = raw_message(12, NOW - 2, sender=HistoryAPI.account)
    first = [newer, raw_message(11, NOW - 10), own]
    second = [raw_message(11, NOW - 10), raw_message(8, NOW - 30), raw_message(7, NOW - 101)]
    api = HistoryAPI([first, second])
    messages, _ = await HistoryReader(Limits(), journal).read(api, task, NOW - 100, NOW)
    assert [m.message_id for m in messages] == ["8", "11"]
    assert api.cursors == [None, "11"]


async def test_stalled_page_fails_instead_of_publishing_partial(task, journal):
    api = HistoryAPI([[raw_message(9, NOW - 1)]])
    with pytest.raises(IncompleteHistory, match="前进"):
        await HistoryReader(Limits(), journal).read(api, task, NOW - 100, NOW)


async def test_read_limit_is_not_success(task, journal):
    api = HistoryAPI([[raw_message(9, NOW - 1)]])
    with pytest.raises(IncompleteHistory, match="页数"):
        await HistoryReader(replace(Limits(), max_pages=1), journal).read(api, task, NOW - 100, NOW)


async def test_text_limit_is_not_silent_truncation(task, journal):
    api = HistoryAPI([[raw_message(9, NOW - 1, "x" * 200), raw_message(1, NOW - 200)]])
    with pytest.raises(IncompleteHistory, match="文字"):
        await HistoryReader(replace(Limits(), max_history_chars=100), journal).read(api, task, NOW - 100, NOW)


def test_milliseconds_and_cq_format():
    msg = raw_message(1, NOW * 1000)
    msg["message"] = "有用链接 &amp; [CQ:image,file=a.jpg][CQ:at,qq=12345]"
    parsed = normalize(msg, "123456789")
    assert parsed.time == NOW
    assert "有用链接 &" in parsed.text
    assert "图片未识别" in parsed.text
    assert "12345" not in parsed.text


@pytest.mark.parametrize("patch", [{"time": None}, {"time": 0}, {"group_id": "777777777"}])
def test_invalid_history_is_rejected(patch):
    with pytest.raises(IncompleteHistory):
        normalize({**raw_message(1, NOW), **patch}, "123456789")


async def test_forward_uses_outer_time_and_one_level(task, journal):
    class ForwardAPI(HistoryAPI):
        async def read(self, action, **kwargs):
            assert action == "get_forward_msg"
            return {
                "messages": [
                    {
                        "time": 1,
                        "message": [
                            {"type": "text", "data": {"text": "旧通知的新进展"}},
                            {"type": "forward", "data": {"id": "nested"}},
                        ],
                    }
                ]
            }

    message = raw_message(10, NOW - 10)
    message["message"].append({"type": "forward", "data": {"id": "forward-id"}})
    api = ForwardAPI([[message, raw_message(1, NOW - 1000)]])
    result, _ = await HistoryReader(Limits(), journal).read(
        api, replace(task, read_forwards=True), NOW - 100, NOW
    )
    assert result[0].time == NOW - 10
    assert "旧通知的新进展" in result[0].text


def test_card_parser_does_not_expose_entire_json():
    text, _ = flatten(
        [
            {
                "type": "json",
                "data": {
                    "data": {
                        "meta": {
                            "detail": {
                                "title": "通知",
                                "qqdocurl": "https://example.org",
                                "arbitrary": "private-unused",
                            }
                        }
                    }
                },
            }
        ]
    )
    assert "通知" in text and "https://example.org" in text
    assert "private-unused" not in text


async def test_empty_parsed_page_does_not_prove_full_coverage(task, journal):
    with pytest.raises(IncompleteHistory, match="空页"):
        await HistoryReader(Limits(), journal).read(HistoryAPI([[]]), task, NOW - 100, NOW)
