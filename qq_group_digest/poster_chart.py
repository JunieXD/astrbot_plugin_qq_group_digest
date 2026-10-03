"""Local time-series layout with regular, readable hour ticks."""

from __future__ import annotations

import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

HOUR_STEPS = (1, 2, 3, 4, 6, 12, 24, 48, 72, 96, 168)
MAX_TICKS = 8


def _timestamps(activity):
    values = [point.get("timestamp") for point in activity]
    if all(
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        for value in values
    ) and all(b > a for a, b in zip(values, values[1:])):
        return values, True
    # Older rendering contexts contain only chronological hourly labels.
    return [index * 3600 for index in range(len(activity))], False


def _clock_label(value):
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?", value)
    if match and int(match[1]) < 24 and int(match[2] or 0) < 60:
        return f"{int(match[1]):02d}:{int(match[2] or 0):02d}"
    return value


def activity_chart(activity, timezone="Asia/Shanghai"):
    """Keep every data point; choose evenly spaced ticks without forcing the tail."""
    if not activity:
        return None
    left, right, top, baseline = 70, 958, 18, 154
    times, dated = _timestamps(activity)
    duration = times[-1] - times[0]
    maximum = max(point["count"] for point in activity)

    def x(stamp):
        return round(left + (right - left) * (stamp - times[0]) / (duration or 1), 2)

    points = [
        {
            **point,
            "x": x(stamp),
            "y": round(baseline - (baseline - top) * point["count"] / (maximum or 1), 2),
        }
        for point, stamp in zip(activity, times)
    ]
    path = f"M {points[0]['x']},{points[0]['y']}"
    for previous, point in zip(points, points[1:]):
        middle = round((previous["x"] + point["x"]) / 2, 2)
        path += f" C {middle},{previous['y']} {middle},{point['y']} {point['x']},{point['y']}"
    area = f"{path} L {points[-1]['x']},{baseline} L {points[0]['x']},{baseline} Z"

    # A 24-hour chart uses 4-hour ticks (seven labels). Rounding eight
    # arbitrary indices would alternate 3/4-hour gaps and distort the axis.
    step_hours = next(
        (step for step in HOUR_STEPS if math.floor(duration / (step * 3600)) + 1 <= MAX_TICKS),
        max(24, math.ceil(duration / ((MAX_TICKS - 1) * 86400)) * 24),
    )
    step = step_hours * 3600
    zone = ZoneInfo(timezone) if dated else None
    labels = []
    for index in range(math.floor(duration / step) + 1):
        stamp = times[0] + index * step
        if dated:
            local = datetime.fromtimestamp(stamp, zone)
            label, date = f"{local:%H:%M}", f"{local.month}月{local.day}日"
        else:
            point = points[index * step_hours]
            label, date = _clock_label(point["label"]), point["date_label"]
        labels.append(
            {
                "label": label,
                "date_label": date,
                "timestamp": stamp,
                "x": x(stamp),
                "anchor": "start" if index == 0 else "end" if stamp == times[-1] else "middle",
            }
        )

    dates, previous_end = [], -1
    for point in labels:
        date = point["date_label"]
        if not date or (dates and date == dates[-1]["label"]):
            continue
        width = min(220, len(date) * 22)
        anchor = "end" if point["x"] + width > right else "start"
        start = point["x"] - width if anchor == "end" else point["x"]
        if start < previous_end + 12:
            continue
        dates.append({"label": date, "x": point["x"], "anchor": anchor})
        previous_end = start + width
    return {
        "points": points,
        "line": path,
        "area": area,
        "labels": labels,
        "dates": dates,
        "maximum": maximum,
    }
