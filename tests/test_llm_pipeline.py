import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_group_digest.config import DigestError
from qq_group_digest.llm_client import LLMClient
from qq_group_digest.llm_stats import UsageStore
from qq_group_digest.models import Message
from qq_group_digest.summarizer import Summarizer

from .conftest import NOW


async def test_retry_statistics_and_completed_work_reuse(store, settings, task, journal, tmp_path):
    class Context:
        calls = 0

        async def llm_generate(self, **kwargs):
            self.calls += 1
            content = (
                "not JSON"
                if self.calls == 1
                else json.dumps(
                    {"items": [{"title": "反馈", "body": "群友{{u1}}称有条件限制。", "sources": ["u1"]}]}
                )
            )
            return SimpleNamespace(
                role="assistant",
                completion_text=content,
                raw_completion={
                    "model": "test",
                    "choices": [{"finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 20},
                },
            )

    settings = replace(settings, tasks=(replace(task, provider_id="test", attribute_speakers=True),))
    task = settings.tasks[0]
    context = Context()
    with_stats = UsageStore(tmp_path / "usage.db", settings.llm_statistics)
    client = LLMClient(context, store, lambda: settings, journal, with_stats)
    adapter = SimpleNamespace(pid="onebot", account="111")
    messages = [Message("a", "1", NOW - 1, "222", "有条件限制。", sender_name="真实昵称")]
    try:
        first = await Summarizer(client, settings.limits).summarize(task, adapter, messages, NOW - 10, NOW)
        second = await Summarizer(client, settings.limits).summarize(task, adapter, messages, NOW - 10, NOW)
        assert context.calls == 2 and first.dump() == second.dump()
        assert "真实昵称" in first.items[0].body
        rows = with_stats.query(group_ids=[task.source_group], content=True)
        assert [r["status"] for r in rows] == ["invalid_json", "success"]
        assert rows[0]["error_detail"] and rows[0]["input_tokens"] == 100
        changed = replace(task, focus="新的关注规则")
        await Summarizer(client, settings.limits).summarize(changed, adapter, messages, NOW - 10, NOW)
        assert context.calls == 3
    finally:
        with_stats.close()


@pytest.mark.parametrize("cancel", [True, False])
async def test_failed_or_cancelled_request_is_recorded(store, settings, task, journal, tmp_path, cancel):
    class Context:
        async def llm_generate(self, **kwargs):
            raise asyncio.CancelledError() if cancel else TimeoutError()

    stats = UsageStore(tmp_path / "usage.db", settings.llm_statistics)
    client = LLMClient(Context(), store, lambda: settings, journal, stats)
    try:
        with pytest.raises(asyncio.CancelledError if cancel else DigestError):
            await client.generate(
                replace(task, provider_id="test"), SimpleNamespace(pid="p", account="111"), "input"
            )
        row = stats.query(group_ids=[task.source_group])[0]
        assert row["status"] == ("cancelled" if cancel else "provider_error")
        assert row["input_tokens"] is None
    finally:
        stats.close()
