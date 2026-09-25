"""Request-local structured output; never change the shared AstrBot provider."""

from copy import copy, deepcopy

CONTEXTS = {"ecnu-max": 512000, "ecnu-plus": 256000}


def effective_options(model, config, limits):
    options = deepcopy(config.get("custom_extra_body") or {})
    if model.lower() in CONTEXTS:
        # Preserve semantic lengths and do not constrain characters while decoding.
        options.update(
            response_format={"type": "json_object"},
            thinking={"type": "disabled"},
            max_tokens=limits.llm_output_tokens,
        )
        options.setdefault("temperature", 0.2)
        options.pop("reasoning_effort", None)
        options.pop("max_completion_tokens", None)
    return options


async def _no_implicit_retry(error, *args, **kwargs):
    # AstrBot otherwise may pop input history or switch keys inside one text_chat.
    raise error


def request_provider(provider, task, limits, allowed_sources=None):
    model = str(provider.get_model() or "").lower() if callable(getattr(provider, "get_model", None)) else ""
    if model not in CONTEXTS:
        return None, {}
    config = getattr(provider, "provider_config", None)
    client = getattr(provider, "client", None)
    if not isinstance(config, dict) or not callable(getattr(client, "with_options", None)):
        raise RuntimeError("ECNU 摘要需要 AstrBot OpenAI Chat Completion 适配器")
    local = copy(provider)
    local.provider_config = deepcopy(config)
    options = effective_options(model, config, limits)
    local.provider_config["custom_extra_body"] = options
    local.client = client.with_options(max_retries=0, timeout=limits.llm_timeout_seconds)
    local._handle_api_error = _no_implicit_retry
    if local.client is client:
        raise RuntimeError("无法隔离模型请求设置")
    return local, options
