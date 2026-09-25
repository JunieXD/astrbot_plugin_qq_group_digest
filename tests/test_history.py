from dataclasses import replace

import pytest

from qq_group_digest.config import IncompleteHistory, Limits
from qq_group_digest.history import HistoryReader, flatten, normalize
from qq_group_digest.models import Message

from .conftest import NOW, raw_message


class HistoryAPI:
    account = "111111111"
    generation = 1

    def __init__(self, pages):
        self.pages = pages
        self.cursors = []

    async def history_page(self, group, count, cursor=None, **kwargs):
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


@pytest.mark.parametrize(
    "sender,expected",
    [
        ({"card": "23级-某校-小林", "nickname": "旧昵称"}, "23级-某校-小林"),
        ({"card": "  ", "nickname": " 小林\n同学\x00 "}, "小林 同学"),
        ({"nickname": "QQ123456789"}, "QQ[号码]"),
        ({"user_id": "222222222"}, ""),
        (None, ""),
    ],
)
def test_history_names_are_optional_and_do_not_fall_back_to_qq(sender, expected):
    raw = {**raw_message(1, NOW), "sender": sender}
    assert normalize(raw, "123456789").sender_name == ""
    named = normalize(raw, "123456789", include_names=True)
    assert named.sender_name == expected
    assert Message(**named.dump()) == named
    old_snapshot = named.dump()
    old_snapshot.pop("sender_name")
    assert Message(**old_snapshot).sender_name == ""


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


@pytest.mark.parametrize("include_names", [False, True])
async def test_forward_authors_stay_separate_from_the_group_forwarder(task, journal, include_names):
    class ForwardAPI(HistoryAPI):
        async def read(self, action, **kwargs):
            return {
                "messages": [
                    {
                        "sender": {"nickname": "内层作者", "user_id": "333333333"},
                        "content": [{"type": "text", "data": {"text": "听说甲校卡 rk1"}}],
                    },
                    {"content": [{"type": "text", "data": {"text": "另一条未署名说法"}}]},
                ]
            }

    raw = {**raw_message(10, NOW - 10), "sender": {"card": "外层转发者"}}
    raw["message"].append({"type": "forward", "data": {"id": "forward-id"}})
    result, _ = await HistoryReader(Limits(), journal).read(
        ForwardAPI([[raw, raw_message(1, NOW - 1000)]]),
        replace(task, read_forwards=True, attribute_speakers=include_names),
        NOW - 100,
        NOW,
    )
    message = result[0]
    assert message.sender_name == ("外层转发者" if include_names else "")
    assert ("内层作者" in message.text) == include_names
    assert "333333333" not in message.text
    assert "不能归为外层转发者本人说法" in message.text
    assert "另一条未署名说法" in message.text


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


async def test_text_limit_stops_pagination_before_more_network_reads(task, journal):
    api = HistoryAPI([[raw_message(9, NOW - 1, "x" * 200)], [raw_message(1, NOW - 1000)]])
    with pytest.raises(IncompleteHistory, match="文字"):
        await HistoryReader(replace(Limits(), max_history_chars=100), journal).read(api, task, NOW - 100, NOW)
    assert api.cursors == [None]


async def test_reader_passes_connection_generation_with_cursor(task, journal):
    class CheckedAPI(HistoryAPI):
        async def history_page(self, group, count, cursor=None, *, generation=None):
            assert generation == (None if cursor is None else self.generation)
            return await super().history_page(group, count, cursor)

    api = CheckedAPI([[raw_message(9, NOW - 1)], [raw_message(1, NOW - 1000)]])
    messages, _ = await HistoryReader(Limits(), journal).read(api, task, NOW - 100, NOW)
    assert len(messages) == 1
