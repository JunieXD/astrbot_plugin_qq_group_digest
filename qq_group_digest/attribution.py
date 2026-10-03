"""Restore historical display names from model-visible author labels."""

import re

from .models import Attribution, Item, Speaker
from .poster_data import qq_identity
from .render import readable_body
from .transcript import source_id

REFERENCE = re.compile(r"\{\{([mu][1-9][0-9]*)\}\}")
# URLs are matched first and passed through. Resolve legacy placeholders and
# compact speaker labels in one pass so nicknames containing u1 stay literal.
DISPLAY_REFERENCE = re.compile(
    r"(?P<url>https?://[^\s<>，。；！？）]+)|\{\{(?P<legacy>[mu][1-9][0-9]*)\}\}"
    r"|(?<![A-Za-z0-9_/{])(?P<speaker>u[1-9][0-9]*)(?![A-Za-z0-9_/}])"
)


def reference_id(match):
    return match["legacy"] or match["speaker"]


def references(text):
    return [source_id(reference_id(m)) for m in DISPLAY_REFERENCE.finditer(text) if not m["url"]]


def resolve_names(items, indexed, enabled):
    result = []
    for item in items:
        speakers, spans = {}, []

        def resolve(text, *, track=False):
            delta = 0

            def name(match):
                nonlocal delta
                if match["url"]:
                    return match[0]
                message = indexed[source_id(reference_id(match))]
                qq = qq_identity(message.sender)
                replacement = f"“{message.sender_name or '昵称未提供'}”" if enabled else "（原消息作者）"
                if enabled and qq:
                    speakers.setdefault(qq, Speaker(qq, message.sender_name))
                    if track:
                        start = match.start() + delta
                        spans.append(Attribution(start, start + len(replacement), qq, message.sender_name))
                delta += len(replacement) - len(match[0])
                return replacement

            return DISPLAY_REFERENCE.sub(name, text)

        result.append(
            Item(
                resolve(item.title),
                resolve(item.body, track=True),
                item.sources,
                tuple(speakers.values()),
                tuple(spans),
            )
        )
    return result


def _speaker_boundaries(text, marker):
    """Separate explicit new statements without breaking quoted testimony."""
    quotes = {"“": "”", "‘": "’", "「": "」", "『": "』", '"': '"', "'": "'"}
    parentheses = {"（": "）", "(": ")", "【": "】", "[": "]"}
    stack, result = [], []
    quote_closers = set(quotes.values())
    for index, char in enumerate(text):
        quoted = stack and stack[-1] in quote_closers
        escaped = index > 0 and text[index - 1] == "\\"
        apostrophe = (
            char == "'"
            and 0 < index < len(text) - 1
            and text[index - 1].isalnum()
            and text[index + 1].isalnum()
        )
        if quoted:
            if char == stack[-1] and not escaped:
                stack.pop()
            elif char in quotes and not escaped and not apostrophe:
                stack.append(quotes[char])
        elif char in quotes and not escaped and not apostrophe:
            stack.append(quotes[char])
        elif char in parentheses:
            stack.append(parentheses[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif char in "；;" and not stack:
            following = index + 1
            while following < len(text) and text[following].isspace():
                following += 1
            if marker.match(text, following):
                char = "\n"
        result.append(char)
    return "".join(result)


def poster_points(item):
    """Shorten only names inserted by attribution, with exact per-point authors.

    Display names are never matched against a directory. Old digests lacking
    precise spans keep their prose; their item-level speakers remain available
    to the caller as a fallback. Transient markers survive paragraph wrapping,
    are collision-free for this body, and never reach the rendered text.
    """
    spans = item.body_attributions
    previous = 0
    for span in spans:
        if (
            not previous <= span.start < span.end <= len(item.body)
            or not qq_identity(span.qq)
            or item.body[span.start : span.end] != f"“{span.name or '昵称未提供'}”"
        ):
            spans = ()
            break
        previous = span.end
    if not spans:
        return [
            {"text": p.removeprefix("• "), "authors": ()} for p in readable_body(item.body).split("\n\n") if p
        ]
    prefix = "\ue000"
    while prefix in item.body:
        prefix += "\ue000"
    marker = re.compile(re.escape(prefix) + r"([0-9]+)\ue001")
    pieces, previous = [], 0
    for index, span in enumerate(spans):
        before = item.body[previous : span.start]
        # These labels describe the exact attributed speaker immediately after
        # them. Replace the whole label/name pair with one generic '群友'.
        pieces.extend((re.sub(r"(?:群友|群|同学)\s*$", "", before), f"{prefix}{index}\ue001"))
        previous = span.end
    pieces.append(item.body[previous:])
    result = []
    for paragraph in readable_body(_speaker_boundaries("".join(pieces), marker)).split("\n\n"):
        if not paragraph:
            continue
        authors = {}

        def author(match):
            span = spans[int(match[1])]
            authors.setdefault(span.qq, Speaker(span.qq, span.name))
            return "群友"

        text = marker.sub(author, paragraph.removeprefix("• ")).replace("群友等多位", "多位群友")
        if len(authors) > 1:
            text = re.sub(r"群友(?:\s*[、和与及]\s*群友)+", "多位群友", text)
        result.append({"text": text, "authors": tuple(authors.values())})
    return result
