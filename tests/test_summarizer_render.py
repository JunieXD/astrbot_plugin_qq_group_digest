import json
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_group_digest.config import MODES, DigestError, Limits
from qq_group_digest.models import Digest, Item, Message
from qq_group_digest.render import make_payloads, payload_fingerprint
from qq_group_digest.summarizer import LLMClient, Summarizer, fingerprint, parse_digest, usable_completion

from .conftest import NOW


def output(sources=("m000001",), title="申请通知", body="截止时间为明天。", prompt=None):
    batch = {"batch_id": re.search(r"本次校验标识：([a-f0-9]{16})", prompt).group(1)} if prompt else {}
    return json.dumps(
        {**batch, "items": [{"title": title, "body": body, "sources": list(sources)}]}, ensure_ascii=False
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
    settings = replace(settings, limits=replace(settings.limits, llm_calls_per_run=1))
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


async def test_empty_window_never_calls_model(task):
    class Client:
        async def generate(self, *args, **kwargs):
            raise AssertionError("should not call model")

    result = await Summarizer(Client(), Limits()).summarize(task, None, [], NOW - 10, NOW)
    assert not result.items


@pytest.mark.parametrize("mode", MODES)
def test_four_modes_use_literal_text_and_robot_identity(task, mode):
    digest = Digest(
        [Item("通知", "[CQ:at,qq=all] 不应触发提及", ("m1",)), Item("链接", "https://example.org", ("m2",))]
    )
    payloads = make_payloads(replace(task, mode=mode), digest, NOW - 100, NOW, "111111111", Limits())
    if mode.startswith("普通"):
        assert all(seg["type"] == "text" for p in payloads for seg in p["message"])
        assert len(payloads) == (1 if mode.endswith("整篇") else 2)
    else:
        assert len(payloads) == 1
        assert len(payloads[0]["messages"]) == (1 if mode.endswith("整篇") else 3)
        assert all(n["data"]["user_id"] == "111111111" for n in payloads[0]["messages"])


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
