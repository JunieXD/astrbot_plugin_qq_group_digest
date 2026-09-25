"""Request-local structured output; never change the shared AstrBot provider."""

from copy import copy, deepcopy

from .config import DigestError, GenerationOptions

CONTEXTS = {"ecnu-max": 512000, "ecnu-plus": 256000}
ECNU_EFFORTS = {"ecnu-max": ("low", "high", "max"), "ecnu-plus": ("low", "medium", "xhigh")}
DIGEST_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "qq_group_digest",
        "schema": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "subject": {"type": "string"},
                            "title": {"type": "string"},
                            "body": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 8,
                            },
                            "sources": {
                                "type": "array",
                                "items": {"type": "string", "pattern": "^u[1-9][0-9]*$"},
                                "minItems": 1,
                                "maxItems": 20,
                            },
                        },
                        "required": ["subject", "title", "body", "sources"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    },
}


def effective_options(model, config, limits, generation=None):
    generation = generation or GenerationOptions()
    model = model.strip().lower()
    options = deepcopy(config.get("custom_extra_body") or {})
    if model in CONTEXTS:
        # Constrain structure, not sentence length; validate source membership locally
        # instead of adding a potentially huge per-transcript enum to the schema.
        options.update(
            response_format=(
                deepcopy(DIGEST_RESPONSE_FORMAT)
                if generation.ecnu_structured_output
                else {"type": "json_object"}
            ),
            max_tokens=limits.llm_output_tokens,
        )
        if generation.ecnu_thinking != "inherit":
            options["thinking"] = {"type": generation.ecnu_thinking}
            options.pop("reasoning_effort", None)
            if generation.ecnu_thinking == "enabled":
                effort = generation.ecnu_reasoning_effort
                if effort not in ECNU_EFFORTS[model]:
                    raise DigestError(
                        f"{model} 不支持思考程度 {effort}，请选择 {' / '.join(ECNU_EFFORTS[model])}。"
                    )
                options["reasoning_effort"] = effort
        thinking = options.get("thinking")
        if isinstance(thinking, dict) and thinking.get("type") == "enabled":
            # ECNU recommends default sampling in thinking mode.
            options.pop("temperature", None)
            options.pop("top_p", None)
        else:
            options.setdefault("temperature", 0.2)
        options.pop("max_completion_tokens", None)
    return options


async def _no_implicit_retry(error, *args, **kwargs):
    # AstrBot otherwise may pop input history or switch keys inside one text_chat.
    raise error


def request_provider(provider, task, limits, allowed_sources=None, *, generation=None):
    model = (
        str(provider.get_model() or "").strip().lower()
        if callable(getattr(provider, "get_model", None))
        else ""
    )
    if model not in CONTEXTS:
        return None, {}
    config = getattr(provider, "provider_config", None)
    client = getattr(provider, "client", None)
    if not isinstance(config, dict) or not callable(getattr(client, "with_options", None)):
        raise RuntimeError("ECNU 摘要需要 AstrBot OpenAI Chat Completion 适配器")
    local = copy(provider)
    local.provider_config = deepcopy(config)
    options = effective_options(model, config, limits, generation)
    local.provider_config["custom_extra_body"] = options
    local.client = client.with_options(max_retries=0, timeout=limits.llm_timeout_seconds)
    local._handle_api_error = _no_implicit_retry
    if local.client is client:
        raise RuntimeError("无法隔离模型请求设置")
    return local, options
