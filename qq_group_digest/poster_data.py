"""Deterministic poster statistics from original messages in the digest period."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from .models import Activity, ActivityMember


def qq_identity(value):
    """Validate identities before using them for attribution or avatar requests."""
    text = str(value).strip()
    if not re.fullmatch(r"[0-9]{1,20}", text) or int(text) == 0:
        return ""
    return str(int(text))


def build_activity(messages, start, end, timezone, *, bot_id="", excluded_members=()):
    """Count each native message once; overlap and forwarded internals do not count.

    ``hourly`` contains totals by local clock hour. For periods longer than a day,
    messages at the same hour on different dates contribute to the same bucket.
    Names come from the newest nonempty historical display name in this period.
    """
    excluded = {qq_identity(value) for value in (bot_id, *excluded_members)}
    zone = ZoneInfo(timezone)
    counts, names, hourly = Counter(), {}, [0] * 24
    seen = set()
    for message in sorted(messages, key=lambda item: (item.time, item.key), reverse=True):
        if not start <= message.time < end:
            continue
        qq = qq_identity(message.sender)
        if not qq or qq in excluded:
            continue
        # Normalized history uses the native sequence where available. A missing
        # key can still be deduplicated without flattening its forwarded content.
        identity = message.key or (message.message_id, message.time, qq)
        if identity in seen:
            continue
        seen.add(identity)
        counts[qq] += 1
        if message.sender_name and qq not in names:
            names[qq] = message.sender_name
        hourly[datetime.fromtimestamp(message.time, zone).hour] += 1
    members = tuple(
        ActivityMember(qq, names.get(qq, ""), count)
        for qq, count in sorted(counts.items(), key=lambda pair: (-pair[1], int(pair[0])))
    )
    return Activity(sum(counts.values()), len(counts), members, tuple(hourly))


def decorate_digest(digest, messages, start, end, timezone, *, bot_id="", excluded_members=()):
    return replace(
        digest,
        activity=build_activity(
            messages, start, end, timezone, bot_id=bot_id, excluded_members=excluded_members
        ),
    )
