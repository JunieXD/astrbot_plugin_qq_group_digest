import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_group_digest.config import DigestError, GenerationOptions, Limits, parse_settings
from qq_group_digest.llm_client import LLMClient, usable_completion
from qq_group_digest.llm_stats import UsageStore
from qq_group_digest.model_options import DIGEST_RESPONSE_FORMAT, effective_options, request_provider
from qq_group_digest.transcript import encode


class SDK:
    def with_options(self, **kwargs):
        return SDK()


class Provider:
    def __init__(self, model="ecnu-max"):
        self.model = model
        self.client = SDK()
        self.calls = []
        self.provider_config = {
            "custom_extra_body": {
                "thinking": {"type": "enabled"},
                "reasoning_effort": "high",
                "temperature": 0.7,
                "top_p": 0.8,
            }
        }

    def get_model(self):
        return self.model

    async def text_chat(self, **kwargs):
        self.calls.append(deepcopy(self.provider_config["custom_extra_body"]))
        return SimpleNamespace(
            role="assistant",
            completion_text='{"items":[]}',
            reasoning_content="private-reasoning-test-marker",
            raw_completion={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"reasoning_content": "private-reasoning-test-marker"},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "reasoning_tokens": 30},
            },
        )


@pytest.mark.parametrize(
    "model,effort,valid",
    [
        ("ecnu-max", "low", True),
        ("ecnu-max", "high", True),
        ("ecnu-max", "max", True),
        ("ecnu-max", "medium", False),
        ("ecnu-plus", "low", True),
        ("ecnu-plus", "medium", True),
        ("ecnu-plus", "xhigh", True),
        ("ecnu-plus", "high", False),
    ],
)
def test_model_specific_efforts_fail_before_request_without_mutating_provider(task, model, effort, valid):
    provider = Provider(model)
    before = deepcopy(provider.provider_config)
    generation = GenerationOptions("enabled", effort)
    if valid:
        local, options = request_provider(provider, task, Limits(), generation=generation)
        assert local.client is not provider.client
        assert options["thinking"] == {"type": "enabled"} and options["reasoning_effort"] == effort
        assert "temperature" not in options and "top_p" not in options
    else:
        with pytest.raises(DigestError, match="不支持思考程度"):
            request_provider(provider, task, Limits(), generation=generation)
    assert provider.provider_config == before and not provider.calls


def test_disabled_inherited_and_non_ecnu_settings(task):
    provider = Provider()
    before = deepcopy(provider.provider_config)
    _, disabled = request_provider(
        provider, task, Limits(), generation=GenerationOptions("disabled", "xhigh")
    )
    assert disabled["thinking"] == {"type": "disabled"} and "reasoning_effort" not in disabled
    assert disabled["temperature"] == 0.7
    _, inherited = request_provider(
        provider, task, Limits(), generation=GenerationOptions("inherit", "xhigh")
    )
    assert inherited["thinking"] == before["custom_extra_body"]["thinking"]
    assert inherited["reasoning_effort"] == "high"
    assert provider.provider_config == before
    provider.model = "other-model"
    assert request_provider(provider, task, Limits()) == (None, {})
    assert effective_options(provider.model, before, Limits()) == before["custom_extra_body"]


@pytest.mark.parametrize(
    "raw",
    [[], {"ecnu_thinking": True}, {"ecnu_reasoning_effort": "unknown"}, {"ecnu_structured_output": "false"}],
)
def test_invalid_generation_settings_rejected(raw):
    with pytest.raises(DigestError):
        parse_settings({"llm_generation": raw})


async def test_configuration_reaches_request_statistics_and_result_cache(
    store, settings, task, journal, tmp_path
):
    provider = Provider()
    before = deepcopy(provider.provider_config)
    context = SimpleNamespace(get_provider_by_id=lambda pid: provider)
    enabled = parse_settings({"llm_generation": {"ecnu_thinking": "enabled", "ecnu_reasoning_effort": "low"}})
    current = [replace(settings, llm_generation=enabled.llm_generation)]
    statistics = UsageStore(tmp_path / "usage.db", settings.llm_statistics)
    client = LLMClient(context, store, lambda: current[0], journal, statistics)
    task = replace(task, provider_id="ecnu/ecnu-max")
    adapter = SimpleNamespace(pid="qq", account="111")
    try:
        enabled_key, _ = await client.cached(task, adapter, "input")
        response = await client.generate(task, adapter, "input")
        client.parsed(response)
        assert response == '{"items":[]}'
        await client.cache_result(enabled_key, response)
        current[0] = replace(settings, llm_generation=GenerationOptions("disabled", "low"))
        disabled_key, cached = await client.cached(task, adapter, "input")
        assert disabled_key != enabled_key and cached is None
        response = await client.generate(task, adapter, "input")
        client.parsed(response)
        current[0] = replace(settings, llm_generation=GenerationOptions("enabled", "high"))
        high_key, cached = await client.cached(task, adapter, "input")
        assert high_key not in (enabled_key, disabled_key) and cached is None
        current[0] = replace(settings, llm_generation=GenerationOptions("enabled", "low"))
        assert (await client.cached(task, adapter, "input"))[1] == '{"items":[]}'
        schema_budget = await client.budget(task, adapter)
        current[0] = replace(settings, llm_generation=GenerationOptions("enabled", "low", False))
        plain_key, cached = await client.cached(task, adapter, "input")
        assert plain_key not in (enabled_key, disabled_key, high_key) and cached is None
        plain_budget = await client.budget(task, adapter)
        assert plain_budget.limit - schema_budget.limit == schema_budget.count(
            encode(DIGEST_RESPONSE_FORMAT)
        ) - plain_budget.count(encode({"type": "json_object"}))
        assert provider.calls[0]["reasoning_effort"] == "low"
        assert provider.calls[1]["thinking"] == {"type": "disabled"}
        assert "reasoning_effort" not in provider.calls[1]
        assert provider.provider_config == before
        rows = statistics.query(group_ids=[task.source_group], content=True)
        assert len(rows) == 2 and all(row["status"] == "success" for row in rows)
        assert json.loads(rows[0]["request_options"])["reasoning_effort"] == "low"
        assert json.loads(rows[0]["request_options"])["response_format"] == DIGEST_RESPONSE_FORMAT
        assert rows[0]["reasoning_tokens"] == 30 and rows[0]["output_tokens"] == 50
        assert "private-reasoning-test-marker" not in json.dumps(rows)
    finally:
        statistics.close()


def test_reasoning_only_response_is_not_used_as_summary():
    with pytest.raises(DigestError, match="空正文"):
        usable_completion(SimpleNamespace(role="assistant", completion_text="", reasoning_content="thinking"))


def test_structured_output_is_isolated_bounded_and_can_use_plain_json(task):
    provider = Provider()
    before = deepcopy(provider.provider_config)
    original_format = deepcopy(DIGEST_RESPONSE_FORMAT)
    _, options = request_provider(provider, task, Limits(), {f"u{i}" for i in range(1, 20001)})
    assert options["response_format"] == original_format
    assert len(encode(options["response_format"])) < 1000  # No giant source enum per call.
    schema = options["response_format"]["json_schema"]["schema"]
    item = schema["properties"]["items"]["items"]
    assert schema["required"] == ["items"] and not schema["additionalProperties"]
    assert set(item["required"]) == {"title", "body"}
    assert set(item["properties"]) == {"title", "body"}
    assert not item["additionalProperties"]
    assert item["properties"]["body"]["type"] == "array"
    item["required"].clear()
    assert DIGEST_RESPONSE_FORMAT == original_format and provider.provider_config == before
    generation = parse_settings({"llm_generation": {"ecnu_structured_output": False}}).llm_generation
    _, options = request_provider(provider, task, Limits(), generation=generation)
    assert options["response_format"] == {"type": "json_object"}
