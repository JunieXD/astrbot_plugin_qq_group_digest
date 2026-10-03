import json
from dataclasses import replace

import pytest

from qq_group_digest.attribution import missing_attributions, poster_points
from qq_group_digest.config import Limits
from qq_group_digest.models import Item, Message
from qq_group_digest.summarizer import Summarizer
from qq_group_digest.validation import OutputError

from .conftest import NOW


def response(body, title="甲校申请｜计算机与AI方向条件不同"):
    return json.dumps({"items": [{"title": title, "body": body}]}, ensure_ascii=False)


def conversation():
    return [
        Message(
            "a", "101", NOW - 2, "111111", "甲校计算机方向卡本科背景，AI方向可以报名。", sender_name="甲"
        ),
        Message(
            "b", "102", NOW - 1, "222222", "乙校收费要看项目，不能用学费推断录取要求。", sender_name="乙"
        ),
    ]


class Client:
    def __init__(self, responses, *, cached=None, journal=None):
        self.responses, self.cache, self.journal = list(responses), cached, journal
        self.calls, self.parsed_calls, self.saved = [], [], []

    async def cached(self, *args):
        return "test-key", self.cache

    async def generate(self, task, adapter, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return self.responses.pop(0)

    def parsed(self, text, error=None):
        self.parsed_calls.append(error)

    async def cache_result(self, key, text):
        self.saved.append((key, text))


async def summarize(client, task):
    return await Summarizer(client, Limits()).summarize(
        replace(task, attribute_speakers=True), None, conversation(), NOW - 60, NOW
    )


async def test_anonymous_personal_accounts_get_one_context_preserving_repair(task, journal):
    anonymous = response(
        ["群友反馈甲校计算机卡本科背景，但AI方向可报名。", "另有群友提醒乙校收费因项目不同。"]
    )
    signed = response(["u1反馈甲校计算机卡本科背景，但AI方向可报名。", "u2提醒乙校收费因项目不同。"])
    client = Client([anonymous, signed], journal=journal)
    digest = await summarize(client, task)
    assert len(client.calls) == 2
    first, second = [prompt for prompt, _ in client.calls]
    assert first in second
    assert "甲校计算机方向卡本科背景，AI方向可以报名。" in second
    assert "乙校收费要看项目，不能用学费推断录取要求。" in second
    assert "111111" not in second and "222222" not in second
    assert client.calls[1][1]["phase"] == "repair"
    assert client.calls[1][1]["attempt"] == 2
    assert client.parsed_calls[0].code == "missing_attribution"
    item = digest.items[0]
    assert [speaker.qq for speaker in item.speakers] == ["111111", "222222"]
    assert len(item.body_attributions) == 2
    assert [point["authors"][0].qq for point in poster_points(item)] == ["111111", "222222"]


async def test_normal_signed_result_keeps_one_llm_call(task, journal):
    client = Client(
        [response(["群友u1反馈甲校AI方向可报名。", "官方报名入口见https://example.org/apply。"])],
        journal=journal,
    )
    digest = await summarize(client, task)
    assert len(client.calls) == 1
    assert len(digest.items[0].body_attributions) == 1
    assert digest.items[0].speakers[0].qq == "111111"


async def test_signed_point_does_not_hide_another_anonymous_personal_account(task, journal):
    client = Client(
        [
            response(["u1反馈甲校AI方向可报名。", "另一位群友提醒乙校收费因项目不同。"]),
            response(["u1反馈甲校AI方向可报名。", "u2提醒乙校收费因项目不同。"]),
        ],
        journal=journal,
    )
    digest = await summarize(client, task)
    assert len(client.calls) == 2
    assert client.parsed_calls[0].code == "missing_attribution"
    assert [speaker.qq for speaker in digest.items[0].speakers] == ["111111", "222222"]
    assert len(digest.items[0].body_attributions) == 2


async def test_anonymous_cached_summary_is_replaced_instead_of_reused(task, journal):
    client = Client(
        [response(["u1反馈甲校AI方向可报名。"])],
        cached=response(["有群友建议申请甲校AI方向。"]),
        journal=journal,
    )
    digest = await summarize(client, task)
    assert len(client.calls) == 1
    assert digest.items[0].speakers[0].qq == "111111"
    assert client.saved
    assert any(event == "摘要结果缓存署名不足" for event, _ in journal.events)


async def test_second_unsigned_response_fails_without_infinite_repair(task, journal):
    client = Client([response(["群友讨论甲校AI方向可报名。"])] * 2, journal=journal)
    with pytest.raises(OutputError) as error:
        await summarize(client, task)
    assert error.value.code == "missing_attribution"
    assert len(client.calls) == 2
    assert not client.saved


async def test_title_only_attribution_cannot_replace_body_identity(task, journal):
    client = Client(
        [
            response(["群友反馈甲校AI方向可报名。"], title="u1分享甲校报名条件"),
            response(["u1反馈甲校AI方向可报名。"]),
        ],
        journal=journal,
    )
    digest = await summarize(client, task)
    assert len(client.calls) == 2
    assert digest.items[0].body_attributions


async def test_objective_notice_does_not_force_an_author(task, journal):
    client = Client(
        [response(["报名将于10月5日12点截止，入口：https://example.org/apply。"])], journal=journal
    )
    digest = await summarize(client, task)
    assert len(client.calls) == 1
    assert not digest.items[0].speakers


async def test_disabled_attribution_accepts_anonymous_summary_without_repair(task, journal):
    client = Client([response(["群友反馈甲校AI方向可报名。"])], journal=journal)
    digest = await Summarizer(client, Limits()).summarize(
        replace(task, attribute_speakers=False), None, conversation(), NOW - 60, NOW
    )
    assert len(client.calls) == 1
    assert not digest.items[0].speakers


async def test_attribution_repair_at_input_budget_preserves_every_original_message(task, journal):
    from qq_group_digest.token_budget import Counter, InputBudget

    signed = response(["u1反馈甲校AI方向可报名。"])
    baseline = Client([signed], journal=journal)
    await summarize(baseline, task)
    original = baseline.calls[0][0]
    original_history = original.split("\n聊天记录：\n", 1)[1].split("\n输入结束。", 1)[0]
    budget = InputBudget(len(original), counter=Counter("characters", len))
    client = Client([response(["群友反馈甲校AI方向可报名。"]), signed], journal=journal)

    async def fixed_budget(task, adapter):
        return budget

    client.budget = fixed_budget
    digest = await summarize(client, task)
    assert digest.items[0].body_attributions
    assert len(client.calls) == 2
    repair_prompt = client.calls[1][0]
    assert original_history in repair_prompt
    assert "待补署名摘要" not in repair_prompt
    assert budget.fits(repair_prompt)


async def test_legacy_reference_without_qq_keeps_notice_without_inventing_avatar(task, journal):
    client = Client([response(["群友{{m1}}转发报名通知：10月5日12点截止。"])], journal=journal)
    digest = await Summarizer(client, Limits()).summarize(
        replace(task, attribute_speakers=True),
        None,
        [Message("a", "101", NOW - 1, "", "官方报名通知，10月5日12点截止。", sender_name="原消息作者")],
        NOW - 60,
        NOW,
    )
    assert len(client.calls) == 1
    assert len(digest.items) == 1
    assert not digest.items[0].speakers
    assert not digest.items[0].body_attributions
    assert "10月5日12点截止" in digest.items[0].body


def test_attribution_detection_checks_independent_points_and_ignores_urls():
    items = [
        Item("u1的建议", "群友建议先确认AI项目要求。", ("u1",)),
        Item("通知", "官方截止时间见：https://example.org/u1。", ()),
        Item("经验", "u1分享经历。\n群友补充可关注官网更新。", ("u1",)),
        Item("申请", "群友反馈报名入口：https://example.org/u1。", ()),
        Item("讨论", "这是一种群友说法，并非官方通知。", ()),
        Item("建议", "群友“作者甲”建议先确认报名要求。", ()),
        Item("条件", "群友反馈甲校需要确认方向。\n群友补充乙校收费因项目不同。", ()),
        Item("公告", "u1分享经历。\n官方截止时间见：https://example.org/u1。", ("u1",)),
    ]
    assert missing_attributions(items) == [1, 3, 4, 6, 7]
