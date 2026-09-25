"""Audited AstrBot requests with isolated ECNU options and bounded retries."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid

from .config import DigestError
from .llm_usage import extract_usage
from .llm_usage import field as value
from .model_options import CONTEXTS, effective_options, request_provider
from .prompts import SYSTEM
from .transcript import InputBudget, encode


def usable_completion(response):
    if value(response, "role") != "assistant" or value(response, "tools_call_name", []):
        raise DigestError("模型没有返回有效的摘要正文。")
    raw = value(response, "raw_completion")
    choices = value(raw, "choices", [])
    if choices:
        first = choices[0]
        if value(first, "finish_reason") in {"length", "content_filter", "tool_calls", "function_call"}:
            raise DigestError("模型输出被截断或中止，请调整摘要长度或模型设置。")
        if value(value(first, "message"), "refusal"):
            raise DigestError("模型拒绝了这次摘要请求。")
    if value(raw, "stop_reason") in {"max_tokens", "refusal"} or value(raw, "status") == "incomplete":
        raise DigestError("模型输出未完整结束。")
    for candidate in value(raw, "candidates", []) or []:
        finish = str(value(candidate, "finish_reason", "")).upper()
        if "MAX_TOKENS" in finish or "SAFETY" in finish:
            raise DigestError("模型输出被截断或中止。")
    text = str(value(response, "completion_text", "") or "").strip()
    if not text:
        raise DigestError("模型返回了空正文。")
    return text


class Generation(str):
    def __new__(cls, text, call_id=None):
        result = super().__new__(cls, text)
        result.call_id = call_id
        return result


class LLMClient:
    def __init__(self, context, store, settings, journal, statistics=None):
        self.context, self.store, self.settings, self.journal = context, store, settings, journal
        self.statistics = statistics
        self.locks = {}

    async def describe(self, task, adapter):
        umo = f"{adapter.pid}:GroupMessage:{task.source_group}"
        pid = task.provider_id or await self.context.get_current_chat_provider_id(umo)
        provider = (
            self.context.get_provider_by_id(pid) if hasattr(self.context, "get_provider_by_id") else None
        )
        model = (
            str(provider.get_model() or "")
            if callable(getattr(provider, "get_model", None))
            else pid.rsplit("/", 1)[-1]
        )
        return str(pid), model, provider

    async def budget(self, task, adapter):
        _, model, provider = await self.describe(task, adapter)
        settings = self.settings()
        limits = settings.limits
        known = CONTEXTS.get(model.lower())
        context = limits.llm_context_tokens or known or 32000
        if known:
            context = min(context, known)
        config = getattr(provider, "provider_config", {})
        options = effective_options(
            model, config if isinstance(config, dict) else {}, limits, settings.llm_generation
        )
        format_bytes = len(encode(options.get("response_format", {})).encode("utf-8"))
        return InputBudget(context - limits.llm_output_tokens - 2048 - format_bytes, limits.llm_input_chars)

    async def cached(self, task, adapter, prompt):
        settings = self.settings()
        limits = settings.limits
        pid, model, provider = await self.describe(task, adapter)
        config = getattr(provider, "provider_config", {})
        # Credentials never become part of the cache or its diagnostics.
        options = effective_options(
            model, config if isinstance(config, dict) else {}, limits, settings.llm_generation
        )
        key = (
            "llm:"
            + hashlib.sha256(
                json.dumps(
                    [
                        3,
                        adapter.pid,
                        getattr(adapter, "account", ""),
                        task.source_group,
                        pid,
                        model,
                        SYSTEM,
                        prompt,
                        limits.llm_output_tokens,
                        options,
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        )
        if not limits.llm_cache_minutes:
            return key, None
        result = await self.store.call("result_cache_get", key, time.time())
        return key, result

    async def cache_result(self, key, text):
        minutes = self.settings().limits.llm_cache_minutes
        if minutes:
            await self.store.call("result_cache_put", key, text, time.time() + minutes * 60, time.time())

    def parsed(self, generation, error=None):
        if self.statistics:
            self.statistics.parsed(
                getattr(generation, "call_id", None), "invalid_json" if error else "success", str(error or "")
            )
        if error:
            self.journal.record(
                "模型输出校验失败", code=getattr(error, "code", "completion"), detail=str(error)
            )

    async def generate(
        self, task, adapter, prompt, *, run_id=None, operation_id=None, attempt=1, phase="single", **kwargs
    ):
        pid, model, provider = await self.describe(task, adapter)
        async with self.locks.setdefault(pid, asyncio.Lock()):
            settings = self.settings()
            limits = settings.limits
            local, options = request_provider(
                provider,
                task,
                limits,
                kwargs.get("allowed_sources"),
                generation=settings.llm_generation,
            )
            await self.store.call(
                "reserve_llm", run_id, time.time(), limits.llm_calls_per_day, limits.llm_calls_per_run
            )
            call_id = None
            if self.statistics:
                call_id = self.statistics.begin(
                    operation_id=operation_id or uuid.uuid4().hex,
                    run_id=run_id or kwargs.get("job_id"),
                    platform_id=adapter.pid,
                    group_id=task.source_group,
                    source=phase,
                    attempt=attempt,
                    provider_id=pid,
                    model=model,
                    model_source="configured",
                    system_prompt=SYSTEM,
                    prompt=prompt,
                    request_options=json.dumps(options, ensure_ascii=False),
                )
            started, response, status, error_type = time.perf_counter(), None, "provider_error", ""
            self.journal.record(
                "模型调用开始",
                group=task.source_group,
                phase=phase,
                attempt=attempt,
                input_chars=len(prompt) + len(SYSTEM),
                call_id=call_id,
            )
            try:
                request = (
                    local.text_chat(
                        prompt=prompt,
                        system_prompt=SYSTEM,
                        contexts=[],
                        func_tool=None,
                        request_max_retries=1,
                    )
                    if local
                    else self.context.llm_generate(
                        chat_provider_id=pid,
                        prompt=prompt,
                        system_prompt=SYSTEM,
                        contexts=[],
                        tools=None,
                        request_max_retries=1,
                    )
                )
                response = await asyncio.wait_for(request, limits.llm_timeout_seconds)
                status = "returned"
                text = usable_completion(response)
                return Generation(text, call_id)
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except DigestError as exc:
                status, error_type = "invalid_json", str(exc)
                raise
            except Exception as exc:
                error_type = type(exc).__name__
                raise DigestError("模型调用未完成，请检查模型配置、额度及超时设置。") from exc
            finally:
                duration = (time.perf_counter() - started) * 1000
                if self.statistics:
                    self.statistics.finish(
                        call_id, status=status, response=response, duration_ms=duration, error_type=error_type
                    )
                usage = extract_usage(response)
                self.journal.record(
                    "模型调用完成" if status == "returned" else "模型调用失败",
                    group=task.source_group,
                    call_id=call_id,
                    phase=phase,
                    status=status,
                    duration_ms=round(duration, 1),
                    input_tokens=usage["input_tokens"],
                    cached_tokens=usage["cached_tokens"],
                    output_tokens=usage["output_tokens"],
                    error_type=error_type,
                )
