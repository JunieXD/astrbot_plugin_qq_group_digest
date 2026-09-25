"""Provider usage normalization. Missing or inconsistent counters stay unknown."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

TOKEN_FIELDS = ("input_tokens", "cached_tokens", "uncached_tokens", "output_tokens", "reasoning_tokens")
PRICE_FIELDS = ("cached_input_per_million", "uncached_input_per_million", "output_per_million")
DEFAULT_STATISTICS = {
    "enabled": True,
    "store_content": False,
    "content_max_chars": 20000,
    "store_response": True,
    "store_usage": True,
    "retention_days": 90,
    "prices": [],
}


def field(value: Any, name: str, default: Any = None) -> Any:
    return value.get(name, default) if isinstance(value, dict) else getattr(value, name, default)


def _counter(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 2**63 - 1:
        return value
    return None


def decimal_price(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
        if number.is_finite() and 0 <= number <= Decimal("1000000000"):
            return number
    except (InvalidOperation, ValueError):
        pass
    return None


def normalize_statistics(value: Any) -> dict:
    raw = value if isinstance(value, dict) else {}
    config = dict(DEFAULT_STATISTICS)
    for key in ("enabled", "store_content", "store_response", "store_usage"):
        v = raw.get(key, config[key])
        config[key] = v if isinstance(v, bool) else str(v).lower() in {"1", "true", "yes", "on"}
    for key, minimum, maximum in (("retention_days", 0, 3650), ("content_max_chars", 256, 200000)):
        try:
            v = float(raw.get(key, config[key]))
            if math.isfinite(v):
                config[key] = max(minimum, min(maximum, int(v)))
        except (TypeError, ValueError, OverflowError):
            pass
    prices = raw.get("prices")
    config["prices"] = []
    for item in prices if isinstance(prices, list) else []:
        if not isinstance(item, dict) or not str(item.get("model") or "").strip():
            continue
        row = {
            "model": str(item["model"]).strip(),
            "provider_id": str(item.get("provider_id") or "").strip(),
            "currency": str(item.get("currency") or "CNY").strip().upper(),
        }
        if row["currency"] != "CREDITS" and (
            not row["currency"].isascii() or not row["currency"].isalpha() or len(row["currency"]) != 3
        ):
            continue
        row["schedule"] = "ecnu_peak" if item.get("schedule") == "ecnu_peak" else "flat"
        row["match_model"] = "configured" if item.get("match_model") == "configured" else "response"
        for key in PRICE_FIELDS:
            price = decimal_price(item.get(key))
            row[key] = str(price) if price is not None else None
        config["prices"].append(row)
    return config


def extract_usage(response: Any) -> dict:
    raw = field(field(response, "raw_completion"), "usage")
    normalized = field(response, "usage")
    result = dict.fromkeys(TOKEN_FIELDS)
    result["usage_source"] = "unknown"
    notes = []
    # Prefer raw OpenAI/DeepSeek counters: AstrBot's generic conversion may
    # turn an absent cache field into zero and misses DeepSeek-specific fields.
    if raw is not None:
        total = _counter(field(raw, "prompt_tokens"))
        output = _counter(field(raw, "completion_tokens"))
        hit = _counter(field(raw, "prompt_cache_hit_tokens"))
        miss = _counter(field(raw, "prompt_cache_miss_tokens"))
        standard_hit = _counter(field(field(raw, "prompt_tokens_details"), "cached_tokens"))
        if hit is not None and standard_hit is not None and hit != standard_hit:
            notes.append("conflicting_cache_counters")
            hit = miss = None
        elif hit is None:
            hit = standard_hit
        reasoning = _counter(field(field(raw, "completion_tokens_details"), "reasoning_tokens"))
        direct_reasoning = _counter(field(raw, "reasoning_tokens"))
        if reasoning is None:
            reasoning = direct_reasoning
        elif direct_reasoning is not None and direct_reasoning != reasoning:
            notes.append("conflicting_reasoning_counters")
            reasoning = None
        if total is None and hit is not None and miss is not None:
            total = _counter(hit + miss)
        if total is not None:
            if (
                (hit is not None and hit > total)
                or (miss is not None and miss > total)
                or (hit is not None and miss is not None and hit + miss != total)
            ):
                notes.append("inconsistent_cache_counters")
                hit = miss = None
            elif hit is not None:
                miss = total - hit
            elif miss is not None:
                hit = total - miss
        if reasoning is not None and output is not None and reasoning > output:
            notes.append("inconsistent_reasoning_counter")
            reasoning = None
        result.update(
            input_tokens=total,
            cached_tokens=hit,
            uncached_tokens=miss,
            output_tokens=output,
            reasoning_tokens=reasoning,
        )
        if any(result[key] is not None for key in TOKEN_FIELDS):
            result["usage_source"] = "provider"
    # Other provider adapters may expose only AstrBot's normalized usage.
    # For an OpenAI-shaped raw response with absent usage, a zero-filled
    # TokenUsage is a framework default, not evidence of a free API call.
    raw_completion = field(response, "raw_completion")
    openai_shape = field(raw_completion, "choices") is not None
    if result["usage_source"] == "unknown" and not openai_shape and normalized is not None:
        other = _counter(field(normalized, "input_other"))
        cached = _counter(field(normalized, "input_cached"))
        output = _counter(field(normalized, "output"))
        if any(v not in (None, 0) for v in (other, cached, output)):
            total = _counter(other + cached) if other is not None and cached is not None else None
            result.update(
                input_tokens=total,
                cached_tokens=cached,
                uncached_tokens=other,
                output_tokens=output,
                usage_source="astrbot",
            )
    result["usage_notes"] = ",".join(notes)
    return result


def usage_snapshot(response: Any) -> dict | None:
    """Only retain known usage fields, never arbitrary provider metadata or text."""
    raw = field(field(response, "raw_completion"), "usage")
    if raw is None:
        return None

    def scalar(value):
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value if abs(value) <= 2**63 - 1 else "invalid_number"
        if isinstance(value, float):
            return value if math.isfinite(value) and abs(value) <= 2**63 - 1 else "invalid_number"
        return "invalid_type"

    result = {}
    absent = object()
    for name in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "prompt_cache_hit_tokens",
        "prompt_cache_miss_tokens",
        "reasoning_tokens",
    ):
        value = field(raw, name, absent)
        if value is not absent:
            result[name] = scalar(value)
    for name, keys in (
        ("prompt_tokens_details", ("cached_tokens", "audio_tokens")),
        (
            "completion_tokens_details",
            ("reasoning_tokens", "audio_tokens", "accepted_prediction_tokens", "rejected_prediction_tokens"),
        ),
    ):
        value = field(raw, name, absent)
        if value is absent:
            continue
        result[name] = (
            None
            if value is None
            else {key: scalar(field(value, key)) for key in keys if field(value, key, absent) is not absent}
        )
    return result


def calculate_cost(
    usage: dict,
    prices: list[dict],
    provider_id: str,
    model: str,
    *,
    started_at: float | None = None,
    configured_model: str | None = None,
) -> dict:
    candidates = [
        p
        for p in prices
        if p["model"] == (configured_model if p.get("match_model") == "configured" else model)
        and p["provider_id"] in ("", provider_id)
    ]
    candidates.sort(key=lambda p: p["provider_id"] != provider_id)
    price = dict(candidates[0]) if candidates else None
    result = {
        "estimated_cost": None,
        "currency": price["currency"] if price else "",
        "price_snapshot": price,
        "cost_lower": None,
        "cost_upper": None,
    }
    if price is None:
        return result
    multiplier = 1
    if price.get("schedule") == "ecnu_peak":
        if started_at is None:
            return result
        local = datetime.fromtimestamp(started_at, timezone(timedelta(hours=8)))
        multiplier = 2 if local.weekday() < 5 and 8 <= local.hour < 23 else 1
        price.update(multiplier=multiplier, priced_at=local.isoformat(), timezone="UTC+08:00")
    cached, other, output = (usage.get(k) for k in ("cached_tokens", "uncached_tokens", "output_tokens"))
    hit_rate, miss_rate, output_rate = (
        None if decimal_price(price.get(k)) is None else decimal_price(price[k]) * multiplier
        for k in PRICE_FIELDS
    )
    # Unknown cache split is a range, never an invented point estimate.
    if (cached is None or other is None) and usage.get("input_tokens") is not None and output is not None:
        if all(rate is not None for rate in (hit_rate, miss_rate, output_rate)):
            total_input = usage["input_tokens"]
            result["cost_lower"] = str(
                (total_input * min(hit_rate, miss_rate) + output * output_rate) / Decimal(1000000)
            )
            result["cost_upper"] = str(
                (total_input * max(hit_rate, miss_rate) + output * output_rate) / Decimal(1000000)
            )
    # A missing cache split can still be priced when both input rates match.
    if (
        (cached is None or other is None)
        and usage.get("input_tokens") is not None
        and hit_rate == miss_rate
        and hit_rate is not None
    ):
        cached, other = 0, usage["input_tokens"]
    total = Decimal(0)
    for count, rate in ((cached, hit_rate), (other, miss_rate), (output, output_rate)):
        if count is None or (count > 0 and rate is None):
            return result
        if count and rate is not None:
            total += count * rate / Decimal(1000000)
    result["estimated_cost"] = str(total)
    result["cost_lower"] = result["cost_upper"] = str(total)
    return result
