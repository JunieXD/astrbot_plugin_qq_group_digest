"""Deterministic poster statistics from original messages in the digest period."""

from __future__ import annotations

import re
from bisect import bisect_right
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


def _hour_key(stamp, zone):
    local = datetime.fromtimestamp(stamp, zone)
    # The UTC offset separates repeated clock hours when daylight saving ends.
    # It also splits real partial hours in zones with half-hour DST changes.
    return local.date(), local.hour, local.utcoffset()


def _hour_start(stamp, zone):
    local = datetime.fromtimestamp(stamp, zone)
    candidate = int(local.replace(minute=0, second=0, microsecond=0).timestamp())
    key = _hour_key(stamp, zone)
    if candidate <= stamp and _hour_key(candidate, zone) == key:
        return candidate
    # Some DST changes begin a real hour at xx:30, so xx:00 is nonexistent
    # or belongs to the earlier UTC offset. Find this actual partial-hour start.
    low, high = int(stamp) - 3600, int(stamp)
    while low < high:
        mid = (low + high) // 2
        if _hour_key(mid, zone) == key:
            high = mid
        else:
            low = mid + 1
    return low


def _next_hour(stamp, zone):
    key = _hour_key(stamp, zone)
    low, high = stamp + 1, stamp + 3600
    while low < high:
        mid = (low + high) // 2
        if _hour_key(mid, zone) == key:
            low = mid + 1
        else:
            high = mid
    return low


def _hourly_times(start, end, zone):
    if end <= start:
        return ()
    times, stamp = [], _hour_start(start, zone)
    while stamp < end:
        times.append(stamp)
        stamp = _next_hour(stamp, zone)
    return tuple(times)


def build_activity(messages, start, end, timezone, *, bot_id="", excluded_members=()):
    """Count each native message once; overlap and forwarded internals do not count.

    ``hourly`` is chronological and ``hourly_times`` contains each real local
    hour's Unix start. Different dates and repeated DST hours remain separate;
    partially covered first/last hours count only messages inside [start, end).
    Names come from the newest nonempty historical display name in this period.
    """
    excluded = {qq_identity(value) for value in (bot_id, *excluded_members)}
    zone = ZoneInfo(timezone)
    hourly_times = _hourly_times(start, end, zone)
    counts, names, hourly = Counter(), {}, [0] * len(hourly_times)
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
        hourly[bisect_right(hourly_times, message.time) - 1] += 1
    members = tuple(
        ActivityMember(qq, names.get(qq, ""), count)
        for qq, count in sorted(counts.items(), key=lambda pair: (-pair[1], int(pair[0])))
    )
    return Activity(sum(counts.values()), len(counts), members, tuple(hourly), hourly_times)


def decorate_digest(digest, messages, start, end, timezone, *, bot_id="", excluded_members=()):
    return replace(
        digest,
        activity=build_activity(
            messages, start, end, timezone, bot_id=bot_id, excluded_members=excluded_members
        ),
    )
