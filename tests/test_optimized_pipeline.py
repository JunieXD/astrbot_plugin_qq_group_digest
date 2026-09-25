import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_group_digest.config import Limits
from qq_group_digest.history import normalize
from qq_group_digest.model_options import DIGEST_RESPONSE_FORMAT, request_provider
from qq_group_digest.models import Message
from qq_group_digest.summarizer import Summarizer
from qq_group_digest.transcript import InputBudget, Transcript
from qq_group_digest.validation import OutputError, parse_digest

from .conftest import NOW, raw_message
from .test_summarizer_render import output


def test_real_failure_shape_reports_exact_length(task):
    task = replace(task, summary_chars=2600)
    text = output(title="标题", body="字" * 2636)
    with pytest.raises(OutputError, match="共 2638 字.*超出 38") as error:
        parse_digest(text, {"m000001"}, task)
    assert error.value.code == "length" and error.value.items


async def test_overlong_output_repairs_validated_candidates_only(task):
    task = replace(task, summary_chars=2600)

    class Client:
        prompts = []

        async def generate(self, task, adapter, prompt, **kwargs):
            self.prompts.append(prompt)
            return output(title="标题", body="字" * (2636 if len(self.prompts) == 1 else 1500))

    client = Client()
    digest = await Summarizer(client, replace(Limits(), llm_context_tokens=64000)).summarize(
        task, None, [Message("a", "100", NOW - 1, "123456", "原始聊天正文" * 1000)], NOW - 20, NOW
    )
    assert digest.items and len(client.prompts) == 2
    assert "单条信息" in client.prompts[1]
    assert "原始聊天正文" not in client.prompts[1]
    assert len(client.prompts[1]) < len(client.prompts[0])


async def test_total_length_budget_keeps_complete_ranked_items_in_one_call(task):
    class Client:
        calls = 0

        async def generate(self, *args, **kwargs):
            self.calls += 1
            return json.dumps(
                {
                    "items": [
                        {"title": f"主题{i}", "body": ("完整且带限定的反馈。" * 24), "sources": [1]}
                        for i in range(12)
                    ]
                },
                ensure_ascii=False,
            )

    client = Client()
    digest = await Summarizer(client, Limits()).summarize(
        replace(task, summary_chars=2600, max_topics=12),
        None,
        [Message("a", "1", NOW - 1, "123456", "来源原文")],
        NOW - 10,
        NOW,
    )
    assert client.calls == 1 and 0 < len(digest.items) < 12
    assert sum(len(i.title) + len(i.body) for i in digest.items) <= 2600
    assert all(i.body == "完整且带限定的反馈。" * 24 for i in digest.items)
    assert any("未展示" in note for note in digest.notes)


async def test_thousands_of_messages_fit_one_ecnu_call(task):
    class Client:
        calls = 0

        async def generate(self, task, adapter, prompt, **kwargs):
            self.calls += 1
            data = json.loads(prompt.split("\n聊天记录：\n")[1].split("\n输入结束。")[0])
            assert len(data["messages"]) == 7200
            assert len({r[2] for r in data["messages"]}) == 437
            return output()

    messages = [
        Message(
            str(i),
            str(i),
            NOW - 8000 + i,
            str(i % 437),
            "这是群友的一条简短反馈。",
            sender_name=f"成员{i % 437}",
        )
        for i in range(7200)
    ]
    client = Client()
    result = await Summarizer(client, Limits()).summarize(
        replace(task, provider_id="ecnu/ecnu-max", attribute_speakers=True), None, messages, NOW - 86400, NOW
    )
    assert result.items and client.calls == 1


def test_reply_ids_names_and_time_survive_compact_encoding(task):
    first = raw_message(101, NOW - 80, "某校有什么限制？")
    first["sender"] = {"card": "原昵称"}
    second = raw_message(909, NOW - 10, "只针对部分方向。")
    second["sender"] = {"card": "新昵称"}
    second["message"].insert(0, {"type": "reply", "data": {"id": "101"}})
    data = Transcript(
        [normalize(r, task.source_group, include_names=True) for r in (first, second)],
        replace(task, attribute_speakers=True),
        NOW - 100,
    )
    records = json.loads(data.serialize(data.records))
    assert records["messages"][1][4] == [1]
    assert [r[0] for r in records["messages"]] == [1, 2]
    assert "引用消息 101" not in records["messages"][1][3]
    assert records["messages"][1][1] == 90
    assert records["messages"][0][2] != records["messages"][1][2]
    assert data.indexed["m000001"].sender_name == "原昵称"
    assert data.indexed["m000002"].sender_name == "新昵称"
    assert "原昵称" not in data.serialize(data.records)


def test_bounded_chunks_include_reply_context(task):
    messages = [Message(str(i), str(i), NOW - 100 + i, str(i % 3), "内容" * 30) for i in range(60)]
    messages[30].reply_ids = ["0"]
    transcript = Transcript(messages, task, NOW - 100)
    budget = InputBudget(5000, 2200)
    chunks = transcript.chunks(budget, 10)
    assert len(chunks) > 1 and all(budget.fits(chunk) for chunk in chunks)
    records = [json.loads(chunk)["messages"] for chunk in chunks]
    assert set(range(1, 61)) == {r[0] for chunk in records for r in chunk}
    reply_chunks = [chunk for chunk in records if any(r[0] == 31 for r in chunk)]
    assert any(any(r[0] == 1 for r in chunk) for chunk in reply_chunks)
    assert any({r[0] for r in a} & {r[0] for r in b} for a, b in zip(records, records[1:]))


def test_ecnu_options_do_not_mutate_shared_provider(task):
    class SDK:
        def with_options(self, **kwargs):
            return SimpleNamespace(options=kwargs)

    provider = SimpleNamespace(
        get_model=lambda: "ecnu-max",
        client=SDK(),
        provider_config={"custom_extra_body": {"thinking": {"type": "enabled"}}},
    )
    original = deepcopy(provider.provider_config)
    client = provider.client
    local, options = request_provider(provider, task, Limits())
    assert provider.provider_config == original and provider.client is client
    assert local.client is not client
    assert options["thinking"] == {"type": "enabled"}
    assert options["reasoning_effort"] == "low"
    assert options["response_format"] == DIGEST_RESPONSE_FORMAT
    assert "temperature" not in options


async def test_ecnu_context_error_cannot_silently_remove_history(task):
    class SDK:
        def with_options(self, **kwargs):
            return SDK()

    provider = SimpleNamespace(get_model=lambda: "ecnu-max", client=SDK(), provider_config={})
    local, _ = request_provider(provider, task, Limits())
    with pytest.raises(ValueError, match="context length"):
        await local._handle_api_error(ValueError("context length"), {})
    assert not hasattr(provider, "_handle_api_error")


async def test_attribution_uses_exact_historical_name_and_final_length(task):
    class Client:
        async def generate(self, *args, **kwargs):
            return output(body="群友{{u1}}说卡排名；群友{{u3}}说只是听说。", sources=["u1", "u3"])

    messages = [
        Message("a", "1", NOW - 3, "111", "卡排名。", sender_name="早期昵称"),
        Message("b", "2", NOW - 2, "222", "不一定", sender_name="早期昵称"),
        Message("c", "3", NOW - 1, "111", "只是听说。", sender_name="新昵称"),
    ]
    digest = await Summarizer(Client(), Limits()).summarize(
        replace(task, attribute_speakers=True), None, messages, NOW - 10, NOW
    )
    assert digest.items[0].body == "群友“早期昵称”说卡排名；群友“新昵称”说只是听说。"


def test_empty_records_cannot_leave_source_number_holes(task):
    transcript = Transcript(
        [
            Message("a", "1", NOW - 3, "111", "有文字"),
            Message("b", "2", NOW - 2, "222", ""),
            Message("c", "3", NOW - 1, "111", "回复", reply_ids=["2", "1"]),
        ],
        task,
        NOW - 10,
    )
    assert [r[0] for r in transcript.records] == [1, 2]
    assert transcript.records[-1][4] == [0, 1]
    assert json.loads(transcript.serialize(transcript.records))["messages"][-1][4] == [0, 1]


def test_attribution_placeholder_must_resolve_to_visible_source(task):
    with pytest.raises(OutputError, match="不存在"):
        parse_digest(output(body="群友{{m999}}说", sources=[1]), {"m000001"}, task)
