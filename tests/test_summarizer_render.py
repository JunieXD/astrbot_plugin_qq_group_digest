import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_group_digest.config import MODES, DigestError, Limits
from qq_group_digest.models import Digest, Item, Message
from qq_group_digest.render import make_payloads, payload_fingerprint
from qq_group_digest.summarizer import LLMClient, Summarizer, fingerprint, parse_digest, usable_completion

from .conftest import NOW


def output(sources=("m000001",), title="申请通知", body="截止时间为明天。", prompt=None):
    return json.dumps(
        {"items": [{"title": title, "body": body, "sources": list(sources)}]}, ensure_ascii=False
    )


@pytest.mark.parametrize(
    "text",
    [
        '{"items": [{"title":"x", "body":"y", "sources":["missing"]}]}',
        '{"items": [], "instructions":"send_to_other_group"}',
        '{"items": "not-list"}',
        "not json",
        '{"items":[{"title":"x","body":"y","sources":[]}]}',
    ],
)
def test_invalid_model_output_rejected(task, text):
    with pytest.raises(DigestError):
        parse_digest(text, {"m000001"}, task)


def test_truncated_valid_json_is_still_rejected():
    response = SimpleNamespace(
        role="assistant",
        completion_text='{"items":[]}',
        raw_completion={"choices": [{"finish_reason": "length"}]},
    )
    with pytest.raises(DigestError, match="截断"):
        usable_completion(response)


def test_structured_topic_normalizes_without_losing_qualifiers_or_sources(task):
    raw = {
        "subject": "甲校软件学院",
        "title": "群友反馈候补已有递补电话",
        "body": ["群友{{u1}}反馈，其候补第96名，已接到电话。", "群友{{u2}}听说仍可能递补。"],
        "sources": ["u1", "u2"],
    }
    items = parse_digest(json.dumps({"items": [raw]}), {"u1", "u2"}, task)
    assert items[0].title == "甲校软件学院｜群友反馈候补已有递补电话"
    assert items[0].body == "\n".join(raw["body"])
    assert items[0].sources == ("u1", "u2")
    assert parse_digest(json.dumps({"items": [i.dump() for i in items]}), {"u1", "u2"}, task) == items


@pytest.mark.parametrize(
    "changes",
    [
        {"subject": ""},
        {"subject": ["甲校"]},
        {"title": ""},
        {"body": []},
        {"body": ["有效", " "]},
        {"body": ["有效", {}]},
        {"body": ["有效"] * 9},
        {"body": ["群友{{u999}}反馈有名额。"]},
    ],
)
def test_malformed_structured_topics_rejected(task, changes):
    raw = {"subject": "甲校", "title": "反馈", "body": ["有候补消息。"], "sources": ["u1"], **changes}
    with pytest.raises(DigestError):
        parse_digest(json.dumps({"items": [raw]}), {"u1"}, task)


def test_input_receipt_required_even_for_empty_summary(task):
    with pytest.raises(DigestError):
        parse_digest('{"items":[]}', {"m000001"}, task, batch_id="expected")
    with pytest.raises(DigestError):
        parse_digest('{"batch_id":"another-input","items":[]}', {"m000001"}, task, batch_id="expected")
    assert parse_digest('{"batch_id":"expected","items":[]}', {"m000001"}, task, batch_id="expected") == []


async def test_generation_uses_durable_model_allowance(store, task, settings, journal):
    from .test_store import make_run

    class Context:
        calls = 0

        async def llm_generate(self, **kwargs):
            self.calls += 1
            assert kwargs["tools"] is None
            assert kwargs["contexts"] == []
            return SimpleNamespace(role="assistant", completion_text=output(prompt=kwargs["prompt"]))

    task = replace(task, provider_id="configured-provider")
    settings = replace(settings, limits=replace(settings.limits, llm_calls_per_run=1, llm_cache_minutes=0))
    rid = await make_run(store, task)
    context = Context()
    client = LLMClient(context, store, lambda: settings, journal)
    messages = [Message("x", "1", NOW - 1, "2", "内容")]
    for attempt in range(2):
        summarizer = Summarizer(client, settings.limits, run_id=rid)
        if attempt == 0:
            result = await summarizer.summarize(task, SimpleNamespace(pid="qq"), messages, NOW - 10, NOW)
            assert result.items
        else:
            with pytest.raises(DigestError, match="本期模型"):
                await summarizer.summarize(task, SimpleNamespace(pid="qq"), messages, NOW - 10, NOW)
    assert context.calls == 1


async def test_overlap_only_result_and_identical_old_result_not_published(task):
    class Client:
        async def generate(self, task, adapter, prompt, **kwargs):
            return output(prompt=prompt)

    messages = [Message("old", "10", NOW - 100, "222", "old"), Message("new", "11", NOW - 1, "222", "new")]
    summarizer = Summarizer(Client(), Limits())
    result = await summarizer.summarize(task, None, messages, NOW - 10, NOW)
    assert result.items == []
    result = await summarizer.summarize(
        task, None, messages, NOW - 200, NOW, previous=[{"title": "申请通知", "body": "截止时间为明天。"}]
    )
    assert result.items == []


async def test_model_format_repair_is_bounded(task):
    class Client:
        calls = 0

        async def generate(self, task, adapter, prompt, **kwargs):
            self.calls += 1
            return "invalid"

    client = Client()
    with pytest.raises(DigestError):
        await Summarizer(client, Limits()).summarize(
            task, None, [Message("x", "1", NOW - 1, "2", "内容")], NOW - 10, NOW
        )
    assert client.calls == 2


async def test_large_single_message_is_not_silently_dropped(task):
    class Client:
        prompts = []

        async def generate(self, task, adapter, prompt, **kwargs):
            self.prompts.append(prompt)
            return output(prompt=prompt)

    client = Client()
    text = "a" * 9000 + "这段在末尾"
    result = await Summarizer(client, replace(Limits(), llm_input_chars=4000)).summarize(
        task, None, [Message("x", "1", NOW - 1, "2", text)], NOW - 10, NOW
    )
    assert result.items
    assert any("这段在末尾" in p for p in client.prompts)
    assert len(client.prompts) > 1


@pytest.mark.parametrize("attribute_speakers", [False, True])
async def test_speaker_provenance_survives_chunking_and_reduction(task, attribute_speakers):
    class Client:
        extracted = {}
        reductions = 0

        async def generate(self, task, adapter, prompt, **kwargs):
            from qq_group_digest.transcript import source_id

            merge = kwargs.get("phase") == "reduce"
            marker = (
                "\n候选摘要（sources 和 source_speakers 保留原始归属）：\n" if merge else "\n聊天记录：\n"
            )
            data = json.loads(prompt.split(marker, 1)[1].split("\n输入结束。")[0])
            assert "222222222" not in prompt and "333333333" not in prompt
            if merge:
                self.reductions += 1
                for item in data:
                    if attribute_speakers:
                        assert item["source_speakers"] == {
                            source_id(s): self.extracted[source_id(s)] for s in item["sources"]
                        }
                    else:
                        assert "source_speakers" not in item
                sources = list(dict.fromkeys(s for item in data for s in item["sources"]))
            else:
                for record in data["messages"]:
                    if attribute_speakers:
                        self.extracted[source_id(record[0])] = record[2]
                sources = list(dict.fromkeys(r[0] for r in data["messages"]))
            return output(sources=sources, prompt=prompt, body="群友说法存在分歧，未经核实。")

    messages = [
        Message("a", "1", NOW - 4, "222222222", "甲校卡 rk1。" * 600, sender_name="同名昵称"),
        Message("b", "2", NOW - 3, "333333333", "不一定。", sender_name="同名昵称"),
        Message("c", "3", NOW - 2, "222222222", "只是听说。", sender_name="新名片"),
        Message("d", "4", NOW - 1, "444444444", "还有其他情况。"),
    ]
    client = Client()
    result = await Summarizer(client, replace(Limits(), llm_input_chars=6000)).summarize(
        replace(task, attribute_speakers=attribute_speakers), None, messages, NOW - 10, NOW
    )
    assert result.items and client.reductions >= 1
    if attribute_speakers:
        speakers = client.extracted
        assert speakers["m000001"] != speakers["m000002"]
        assert speakers["m000001"] != speakers["m000003"]


async def test_empty_window_never_calls_model(task):
    class Client:
        async def generate(self, *args, **kwargs):
            raise AssertionError("should not call model")

    result = await Summarizer(Client(), Limits()).summarize(task, None, [], NOW - 10, NOW)
    assert not result.items


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "card_title,content_title",
    [("", ""), ("✨ 自定义封面", ""), ("", "自定义正文"), ("封面", "✨ [CQ:at,qq=all] 正文")],
)
def test_four_modes_use_literal_text_and_robot_identity(task, mode, card_title, content_title):
    digest = Digest(
        [
            Item("通知", "[CQ:at,qq=all] 不应触发提及（未经核实）", ("m1",)),
            Item("链接", "https://example.org", ("m2",)),
        ],
        ["图片未识别", "技术诊断不对群成员展示"],
    )
    task = replace(task, mode=mode, card_title=card_title, content_title=content_title)
    payloads = make_payloads(task, digest, NOW - 100, NOW, "111111111", Limits())
    expected_heading = (content_title or f"✨ {task.label} · 群聊摘要") + "\n\n"
    serialized = json.dumps(payloads, ensure_ascii=False)
    assert "未经核实" not in serialized and "技术诊断" not in serialized and "图片未识别" not in serialized
    assert "https://example.org" in serialized
    if mode.startswith("普通"):
        assert all(seg["type"] == "text" for p in payloads for seg in p["message"])
        assert len(payloads) == (1 if mode.endswith("整篇") else 2)
        assert all(p["message"][0]["data"]["text"].startswith(expected_heading) for p in payloads)
    else:
        assert len(payloads) == 1
        assert len(payloads[0]["messages"]) == (1 if mode.endswith("整篇") else 3)
        assert all(n["data"]["user_id"] == "111111111" for n in payloads[0]["messages"])
        assert payloads[0]["source"] == (card_title or f"✨ {task.label} · 群聊摘要")
        assert payloads[0]["prompt"] == (f"[{card_title}]" if card_title else "[✨ 群聊摘要]")
        assert all(s["type"] == "text" for n in payloads[0]["messages"] for s in n["data"]["content"])
        assert payloads[0]["messages"][0]["data"]["content"][0]["data"]["text"].startswith(expected_heading)


def test_single_message_limit_and_split_limit(task):
    digest = Digest([Item("很长的主题", "x" * 4000, ("m1",))])
    with pytest.raises(DigestError, match="单条"):
        make_payloads(task, digest, NOW - 100, NOW, "1", Limits())
    with pytest.raises(DigestError, match="分段"):
        make_payloads(
            replace(task, mode="普通消息·分条"), digest, NOW - 100, NOW, "1", replace(Limits(), max_parts=1)
        )


def test_fingerprint_preserves_forward_node_boundaries():
    a = {"messages": [{"data": {"content": [{"type": "text", "data": {"text": t}}]}} for t in ["a", "b"]]}
    b = {"messages": [{"data": {"content": [{"type": "text", "data": {"text": "ab"}}]}}]}
    assert payload_fingerprint(a) != payload_fingerprint(b)


@pytest.mark.parametrize(
    "first,second",
    [
        (("课程", "C++"), ("课程", "C")),
        (("链接", "https://example.org/A"), ("链接", "https://example.org/a")),
        (("甲乙", "丙"), ("甲", "乙丙")),
    ],
)
def test_meaningful_punctuation_case_and_field_boundaries_not_deduplicated(first, second):
    assert fingerprint(Item(*first, ("m1",))) != fingerprint(Item(*second, ("m2",)))
