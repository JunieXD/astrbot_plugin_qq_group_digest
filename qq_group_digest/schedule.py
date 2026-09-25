"""Civil-time boundaries are independent of execution and random delays."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def boundaries(task, now):
    zone = ZoneInfo(task.timezone)
    day = datetime.fromtimestamp(now, zone).date()
    result = set()
    for offset in (-2, -1, 0, 1, 2):
        date = day + timedelta(days=offset)
        for value in task.times:
            hour, minute = map(int, value.split(":"))
            # Repeated civil times run once; nonexistent times are skipped.
            local = datetime(date.year, date.month, date.day, hour, minute, tzinfo=zone, fold=0)
            stamp = int(local.timestamp())
            if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == local.replace(tzinfo=None):
                result.add(stamp)
    return sorted(result)


def latest_boundary(task, now):
    return max(t for t in boundaries(task, now) if t <= now)


def next_boundary(task, now):
    return min(t for t in boundaries(task, now) if t > now)


def period_text(task, start, end):
    zone = ZoneInfo(task.timezone)
    return "—".join(datetime.fromtimestamp(t, zone).strftime("%m/%d %H:%M") for t in (start, end))
