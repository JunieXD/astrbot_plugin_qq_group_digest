"""Restore historical display names from model-visible author labels."""

import re

from .models import Item
from .transcript import source_id

REFERENCE = re.compile(r"\{\{([mu][1-9][0-9]*)\}\}")


def references(text):
    return [source_id(match) for match in REFERENCE.findall(text)]


def resolve_names(items, indexed, enabled):
    def name(match):
        message = indexed[source_id(match[1])]
        return f"“{message.sender_name or '昵称未提供'}”" if enabled else "（原消息作者）"

    return [Item(REFERENCE.sub(name, i.title), REFERENCE.sub(name, i.body), i.sources) for i in items]
