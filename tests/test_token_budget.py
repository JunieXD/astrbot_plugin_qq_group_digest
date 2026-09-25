import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_group_digest import token_budget as budgeting
from qq_group_digest.config import DigestError, Limits
from qq_group_digest.llm_client import LLMClient
from qq_group_digest.model_options import DIGEST_RESPONSE_FORMAT
from qq_group_digest.models import Message
from qq_group_digest.prompts import SYSTEM
from qq_group_digest.summarizer import Summarizer
from qq_group_digest.token_budget import InputBudget, counter_for_model, request_budget
from qq_group_digest.transcript import Transcript, encode

from .conftest import NOW
from .test_model_options import Provider


def test_official_v4_counts_without_truncating_or_adding_tokens():
    counter = counter_for_model("ecnu-max")
    assert counter.name == "deepseek_v4" and not counter.fallback_reason
    # Reference IDs from the official V4 data: Hello! -> [19923, 3].
    assert counter.count("Hello!") == 2
    assert counter.count("你好，世界！") == 4
    assert counter.count("") == 0
    assert counter.count("Hello! " * 20000) > 16384
    assert counter_for_model("DeepSeek-V4-Flash-0731") is counter
    assert counter_for_model("ecnu-plus").name == "utf8_upper_bound"
    assert counter_for_model("unknown-model").name == "utf8_upper_bound"


def test_wrapped_budget_counts_actual_combination_and_preserves_char_limit():
    counter = counter_for_model("ecnu-max")
    assert counter.count("Hel") + counter.count("lo!") > counter.count("Hello!")
    budget = InputBudget(2, counter=counter).wrapped("Hel", "!")
    assert budget.count("lo") == 2 and budget.fits("lo")
    assert not replace(budget, limit=1).fits("lo")
    assert not replace(budget, chars=5).fits("lo")
    assert replace(budget, chars=6).fits("lo")
    assert InputBudget(100, 1).scaled(0.85).chars == 1


def test_output_schema_format_and_safety_reserves_are_separate():
    limits = replace(Limits(), llm_output_tokens=32000)
    budget, details = request_budget("ecnu-max", limits, SYSTEM, DIGEST_RESPONSE_FORMAT)
    assert details["context_tokens"] == 512000  # Not tokenizer_config.model_max_length.
    assert details["output_reserve_tokens"] == 32000
    assert details["safety_reserve_tokens"] == 10240
    assert details["chat_reserve_tokens"] == 256
    assert details["system_units"] == budget.counter.count(SYSTEM)
    assert details["schema_units"] == budget.counter.count(encode(DIGEST_RESPONSE_FORMAT))
    assert (
        budget.limit
        + sum(
            details[k]
            for k in (
                "output_reserve_tokens",
                "safety_reserve_tokens",
                "chat_reserve_tokens",
                "system_units",
                "schema_units",
            )
        )
        == 512000
    )
    smaller, _ = request_budget("ecnu-max", replace(limits, llm_context_tokens=64000), SYSTEM)
    assert smaller.limit < budget.limit
    clamped, _ = request_budget(
        "ecnu-max", replace(limits, llm_context_tokens=1000000), SYSTEM, DIGEST_RESPONSE_FORMAT
    )
    assert clamped.limit == budget.limit


@pytest.mark.parametrize("failure", ["missing", "corrupt", "dependency"])
def test_unavailable_tokenizer_falls_back_without_silent_underestimate(monkeypatch, tmp_path, failure):
    budgeting._v4_counter.cache_clear()
    budgeting.counter_for_model.cache_clear()
    try:
        with monkeypatch.context() as patch:
            path = tmp_path / "tokenizer.json"
            patch.setattr(budgeting, "V4_ASSET", path)
            if failure == "corrupt":
                path.write_text("{}", encoding="utf-8")
            elif failure == "dependency":

                def missing_dependency():
                    raise ImportError("unavailable")

                patch.setattr(budgeting, "_v4_counter", missing_dependency)
            counter = budgeting.counter_for_model("ecnu-max")
            assert counter.name == "utf8_upper_bound" and counter.fallback_reason
            assert counter.count("你好，世界！") == len("你好，世界！".encode("utf-8"))
            _, details = request_budget("ecnu-max", Limits(), SYSTEM)
            assert details["tokenizer_fallback_reason"]
    finally:
        budgeting._v4_counter.cache_clear()
        budgeting.counter_for_model.cache_clear()


async def test_large_chinese_window_fits_one_call_without_dropping_messages(task):
    class Client:
        calls = 0

        async def generate(self, task, adapter, prompt, **kwargs):
            self.calls += 1
            data = json.loads(prompt.split("\n聊天记录：\n", 1)[1].split("\n输入结束。", 1)[0])
            assert len(data["messages"]) == 4000
            assert data["messages"][-1][3].endswith("最后一句也必须保留")
            assert len((SYSTEM + prompt).encode("utf-8")) > 512000
            return '{"items":[]}'

    messages = [
        Message(
            str(i), str(i), NOW - 4000 + i, str(i % 30), "甲校软件学院仅部分方向允许补报，条件需核对。" * 3
        )
        for i in range(4000)
    ]
    messages[-1].text += "最后一句也必须保留"
    client = Client()
    await Summarizer(client, Limits()).summarize(
        replace(task, provider_id="ecnu/ecnu-max"), None, messages, NOW - 86400, NOW
    )
    assert client.calls == 1


def test_token_limited_chunks_preserve_long_unicode_text_and_complete_envelopes(task):
    text = '甲校仅部分方向可以申请；不要删掉条件。✨ https://example.org/a?x=1&y=2 "引号"\n' * 300
    transcript = Transcript([Message("a", "1", NOW - 1, "2", text)], task, NOW - 10)
    counter = counter_for_model("ecnu-max")
    budget = InputBudget(1800, counter=counter).wrapped("前置提示词\n", "\n后置提示词")
    chunks = transcript.chunks(budget, overlap=0)
    assert len(chunks) > 1 and all(budget.count(c) <= budget.limit for c in chunks)
    rows = [r for c in chunks for r in json.loads(c)["messages"]]
    assert {r[0] for r in rows} == {1}
    assert {r[2] for r in rows} == {"u1"}
    assert "".join(r[3] for r in rows) == text
    with pytest.raises(DigestError, match="元数据"):
        transcript.chunks(InputBudget(1, counter=counter))


async def test_final_request_preflight_rejects_overflow_before_model_call(store, task, settings, journal):
    task = replace(task, provider_id="ecnu/ecnu-max")
    provider = Provider()
    context = SimpleNamespace(get_provider_by_id=lambda _: provider)
    settings = replace(settings, limits=replace(settings.limits, llm_context_tokens=20000))
    client = LLMClient(context, store, lambda: settings, journal)
    with pytest.raises(DigestError, match="完整摘要请求"):
        await client.generate(task, SimpleNamespace(pid="qq"), "你好" * 20000)
    assert not provider.calls
    assert not any(event == "模型调用开始" for event, _ in journal.events)


async def test_estimated_tokens_are_logged_separately_from_provider_usage(store, task, settings, journal):
    task = replace(task, provider_id="ecnu/ecnu-max")
    provider = Provider()
    client = LLMClient(
        SimpleNamespace(get_provider_by_id=lambda _: provider), store, lambda: settings, journal
    )
    await client.generate(task, SimpleNamespace(pid="qq"), "Hello!")
    start = next(fields for event, fields in journal.events if event == "模型调用开始")
    end = next(fields for event, fields in journal.events if event == "模型调用完成")
    assert start["token_counter"] == "deepseek_v4"
    assert start["user_input_units"] == 2
    assert start["estimated_input_tokens"] == 2 + start["system_units"] + start["schema_units"]
    assert end["input_tokens"] == 100  # The provider value, not our estimate.
