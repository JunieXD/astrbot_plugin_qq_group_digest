"""Offline text counts and exact checks of the prompt that will actually be sent."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Callable

from .model_options import CONTEXTS

V4_ASSET = Path(__file__).parent / "assets" / "deepseek_v4" / "tokenizer.json"
V4_SHA256 = "89085f12ef79460ac5f66d1119325ddfc694b4ab209d80bbd81d35f081dc9614"
V4_MODELS = {"ecnu-max", "deepseek-v4-flash", "deepseek-v4-flash-0731"}
CHAT_RESERVE = 256


def utf8_size(text):
    return len(text.encode("utf-8"))


@dataclass(frozen=True)
class Counter:
    name: str
    count: Callable[[str], int]
    fallback_reason: str = ""


BYTE_COUNTER = Counter("utf8_upper_bound", utf8_size)


@lru_cache(maxsize=1)
def _v4_counter():
    # No remote code, network fetch, model weights or Transformers dependency.
    from tokenizers import Tokenizer

    data = V4_ASSET.read_bytes()
    if hashlib.sha256(data).hexdigest() != V4_SHA256:
        raise ValueError("tokenizer checksum mismatch")
    tokenizer = Tokenizer.from_str(data.decode("utf-8"))
    tokenizer.no_truncation()
    tokenizer.no_padding()
    return Counter("deepseek_v4", lambda text: len(tokenizer.encode(text, add_special_tokens=False).ids))


@lru_cache(maxsize=16)
def counter_for_model(model):
    if model.strip().lower() not in V4_MODELS:
        return BYTE_COUNTER
    try:
        return _v4_counter()
    except Exception as exc:
        # Missing dependencies/data must never be mistaken for a zero token count.
        return Counter("utf8_upper_bound", utf8_size, type(exc).__name__)


@dataclass(frozen=True)
class InputBudget:
    limit: int
    chars: int = 0
    counter: Counter = BYTE_COUNTER
    prefix: str = ""
    suffix: str = ""

    def count(self, text):
        return self.counter.count(self.prefix + text + self.suffix)

    def fits(self, text):
        if self.chars and len(self.prefix) + len(text) + len(self.suffix) > self.chars:
            return False
        # A byte-level tokenizer cannot use more tokens than UTF-8 bytes.
        # This fast positive check avoids tokenizing tiny rows thousands of times.
        if (
            self.counter.name in {"deepseek_v4", "utf8_upper_bound"}
            and utf8_size(self.prefix + text + self.suffix) <= self.limit
        ):
            return True
        return self.count(text) <= self.limit

    def wrapped(self, prefix, suffix):
        # BPE counts are not additive: count the actual combined user message.
        return replace(self, prefix=self.prefix + prefix, suffix=suffix + self.suffix)

    def scaled(self, fraction):
        return replace(
            self,
            limit=int(self.limit * fraction),
            chars=max(1, int(self.chars * fraction)) if self.chars else 0,
        )


def request_budget(model, limits, system_prompt, response_format=None):
    """Reserve each component separately; only the user prompt remains in the budget."""
    model = model.strip().lower()
    known = CONTEXTS.get(model)
    context = limits.llm_context_tokens or known or 32000
    if known:
        context = min(context, known)
    counter = counter_for_model(model)
    schema = json.dumps(response_format, ensure_ascii=False, separators=(",", ":")) if response_format else ""
    system_units, schema_units = counter.count(system_prompt), counter.count(schema)
    safety = max(2048, math.ceil(context * 0.02))
    available = context - limits.llm_output_tokens - CHAT_RESERVE - safety - system_units - schema_units
    chars = max(1, limits.llm_input_chars - len(system_prompt)) if limits.llm_input_chars else 0
    details = {
        "token_counter": counter.name,
        "budget_unit": "tokens" if counter.name == "deepseek_v4" else "utf8_bytes",
        "context_tokens": context,
        "output_reserve_tokens": limits.llm_output_tokens,
        "chat_reserve_tokens": CHAT_RESERVE,
        "safety_reserve_tokens": safety,
        "system_units": system_units,
        "schema_units": schema_units,
        "user_budget_units": available,
    }
    if counter.fallback_reason:
        details["tokenizer_fallback_reason"] = counter.fallback_reason
    return InputBudget(available, chars, counter), details
