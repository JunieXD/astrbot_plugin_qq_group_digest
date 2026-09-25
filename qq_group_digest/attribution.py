"""Restore historical display names from model-visible author labels."""

import re

from .models import Item
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
    def name(match):
        if match["url"]:
            return match[0]
        message = indexed[source_id(reference_id(match))]
        return f"“{message.sender_name or '昵称未提供'}”" if enabled else "（原消息作者）"

    return [
        Item(DISPLAY_REFERENCE.sub(name, i.title), DISPLAY_REFERENCE.sub(name, i.body), i.sources)
        for i in items
    ]
