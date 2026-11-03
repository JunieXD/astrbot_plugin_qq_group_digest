"""Publication-only names; historical identity and evidence remain untouched."""

from __future__ import annotations

import re
from dataclasses import replace

from .models import Attribution

# Full-width and half-width forms of the same bracket may be mixed. Angle
# brackets used in emoticons or literal HTML are not nickname annotations.
_BRACKETS = ("(（", ")）"), ("[［【〔", "]］】〕"), ("{｛", "}｝"), ("「『", "」』"), ("《〈", "》〉")
_OPEN = {char: kind for kind, (opening, _) in enumerate(_BRACKETS) for char in opening}
_CLOSE = {char: kind for kind, (_, closing) in enumerate(_BRACKETS) for char in closing}
_CONTROLS = dict.fromkeys(
    map(ord, "\u00ad\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2060\u2066\u2067\u2068\u2069\ufeff")
)
_SEPARATORS = "-‐‑‒–—―_－﹣|｜丨"
_EDGE = " " + _SEPARATORS + "、，,;；:："
_BOUNDARY = rf"(?:^|[\s{re.escape(_EDGE)}]+)"
# These describe particular offers at the start of an appended segment. They
# are not a blacklist of subjects such as GPT, school names or 辅导 in general.
_OFFERS = (
    r"(?:低价|高价)?接(?:保研|科研|论文)辅导",
    r"(?:低价|优惠)?(?:代充|代购)\s*(?:GPT|ChatGPT|Codex)(?![A-Za-z])",
    r"(?:出|出售)\s*(?:GPT|ChatGPT|Codex)?\s*额度",
    r"辅导版(?:$|[\s\-—_｜|])",
)
_SUFFIXES = tuple(re.compile(_BOUNDARY + offer, re.IGNORECASE) for offer in _OFFERS)


def clean_name(name):
    """Remove annotations and explicit appended offers, never infer identity."""
    stack, visible = [], []
    for char in str(name or "").translate(_CONTROLS):
        if char in _OPEN:
            stack.append(_OPEN[char])
        elif char in _CLOSE:
            kind = _CLOSE[char]
            if kind in stack:
                # A mismatched nested annotation remains hidden until its
                # enclosing bracket closes. Unclosed annotations run to EOF.
                position = len(stack) - 1 - stack[::-1].index(kind)
                del stack[position:]
        elif not stack:
            visible.append(char)
    value = re.sub(r"\s+", " ", "".join(visible)).strip()
    value = re.split(r"[|｜丨]", value, maxsplit=1)[0]
    value = value.strip(_EDGE)
    matches = [match for rule in _SUFFIXES if (match := rule.search(value))]
    if matches:
        value = value[: min(match.start() for match in matches)]
    return value.strip(_EDGE) or "群友"


def _quoted_names(text, names):
    if not names:
        return text
    pattern = re.compile(
        r"(?P<url>https?://[^\s<>，。；！？]+)|"
        + "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    )
    return pattern.sub(lambda match: match[0] if match["url"] else names[match[0]], text)


def display_item(item):
    """Rebuild exact attribution spans when displayed names change length."""
    spans, previous = item.body_attributions, 0
    for span in spans:
        if (
            not previous <= span.start < span.end <= len(item.body)
            or item.body[span.start : span.end] != f"“{span.name or '昵称未提供'}”"
        ):
            spans = ()
            break
        previous = span.end
    names = {
        f"“{speaker.name or '昵称未提供'}”": f"“{clean_name(speaker.name)}”"
        for speaker in (*item.speakers, *spans)
    }
    if spans:
        pieces, updated, previous, length = [], [], 0, 0
        for span in spans:
            before = item.body[previous : span.start]
            name = clean_name(span.name)
            replacement = f"“{name}”"
            length += len(before)
            updated.append(Attribution(length, length + len(replacement), span.qq, name))
            pieces.extend((before, replacement))
            length += len(replacement)
            previous = span.end
        pieces.append(item.body[previous:])
        body = "".join(pieces)
    else:
        # Legacy snapshots have known quoted display names but no reliable
        # positions. Simplify those names without inventing avatar attribution.
        body, updated = _quoted_names(item.body, names), []
    return replace(
        item,
        title=_quoted_names(item.title, names),
        body=body,
        speakers=tuple(replace(speaker, name=clean_name(speaker.name)) for speaker in item.speakers),
        body_attributions=tuple(updated),
    )


def display_digest(digest):
    """Return a display copy, preserving QQ IDs, counts, ordering and sources."""
    activity = digest.activity
    if activity is not None:
        activity = replace(
            activity,
            members=tuple(replace(member, name=clean_name(member.name)) for member in activity.members),
        )
    return replace(digest, items=[display_item(item) for item in digest.items], activity=activity)
