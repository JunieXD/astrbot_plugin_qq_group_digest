from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from qq_group_digest.poster_chart import MAX_TICKS, activity_chart
from qq_group_digest.poster_data import build_activity
from qq_group_digest.poster_renderer import normalize_context


def stamp(text, timezone="Asia/Shanghai", *, fold=0):
    return int(datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(timezone), fold=fold).timestamp())


def points_from_times(times, timezone, *, counts=None):
    result = []
    for index, value in enumerate(times):
        local = datetime.fromtimestamp(value, ZoneInfo(timezone))
        result.append(
            {
                "label": f"{local.hour:02d}",
                "date_label": f"{local.month}月{local.day}日",
                "timestamp": value,
                "count": counts[index] if counts is not None else index % 5,
            }
        )
    return result


def hourly_points(hours, *, timezone="Asia/Shanghai"):
    start = stamp("2026-10-02T14:00", timezone)
    return points_from_times([start + index * 3600 for index in range(hours + 1)], timezone)


def assert_regular_ticks(chart, elapsed_hours):
    labels = chart["labels"]
    for previous, current in zip(labels, labels[1:]):
        assert current["timestamp"] - previous["timestamp"] == elapsed_hours * 3600
    if len(labels) > 2:
        intervals = [current["x"] - previous["x"] for previous, current in zip(labels, labels[1:])]
        assert intervals == pytest.approx([intervals[0]] * len(intervals), abs=0.02)


def test_24_hour_axis_has_seven_even_four_hour_ticks_across_midnight():
    chart = activity_chart(hourly_points(24))
    assert [point["label"] for point in chart["labels"]] == [
        "14:00",
        "18:00",
        "22:00",
        "02:00",
        "06:00",
        "10:00",
        "14:00",
    ]
    assert [point["x"] for point in chart["labels"]] == [70, 218, 366, 514, 662, 810, 958]
    assert_regular_ticks(chart, 4)
    assert len(chart["points"]) == 25
    assert [date["label"] for date in chart["dates"]] == ["10月2日", "10月3日"]
    assert chart["labels"][0]["anchor"] == "start"
    assert chart["labels"][-1]["anchor"] == "end"


def test_12_hour_axis_uses_two_hour_ticks():
    chart = activity_chart(hourly_points(12))
    assert [point["label"] for point in chart["labels"]] == [
        "14:00",
        "16:00",
        "18:00",
        "20:00",
        "22:00",
        "00:00",
        "02:00",
    ]
    assert_regular_ticks(chart, 2)
    assert len(chart["points"]) == 13


def test_30_hour_axis_uses_four_hour_ticks_without_forcing_uneven_tail():
    activity = hourly_points(30)
    chart = activity_chart(activity)
    assert len(chart["labels"]) == 8
    assert_regular_ticks(chart, 4)
    assert chart["labels"][-1]["timestamp"] == activity[0]["timestamp"] + 28 * 3600
    assert chart["labels"][-1]["timestamp"] < activity[-1]["timestamp"]
    assert chart["labels"][-1]["x"] == pytest.approx(898.8)
    assert chart["labels"][-1]["anchor"] == "middle"
    assert chart["points"][-1]["x"] == 958


def test_one_bucket_chart_has_one_valid_tick_and_preserves_zero_count():
    activity = points_from_times([stamp("2026-10-02T14:00")], "Asia/Shanghai", counts=[0])
    chart = activity_chart(activity)
    assert chart["maximum"] == 0
    assert len(chart["labels"]) == len(chart["points"]) == 1
    assert chart["labels"][0]["label"] == "14:00"
    assert chart["labels"][0]["x"] == chart["points"][0]["x"] == 70
    assert chart["points"][0]["y"] == 154
    assert "nan" not in chart["line"].lower() and "inf" not in chart["area"].lower()
    assert activity_chart([]) is None


def test_six_hour_axis_keeps_all_seven_hour_ticks():
    chart = activity_chart(hourly_points(6))
    assert len(chart["labels"]) == 7
    assert [label["label"] for label in chart["labels"]] == [
        "14:00",
        "15:00",
        "16:00",
        "17:00",
        "18:00",
        "19:00",
        "20:00",
    ]
    assert_regular_ticks(chart, 1)


def test_one_week_axis_has_at_most_eight_ticks_and_keeps_every_hour():
    chart = activity_chart(hourly_points(168))
    assert len(chart["labels"]) == MAX_TICKS == 8
    assert_regular_ticks(chart, 24)
    assert len(chart["points"]) == 169
    assert chart["labels"][-1]["timestamp"] == chart["points"][-1]["timestamp"]


@pytest.mark.parametrize(
    "start,end,expected",
    [
        ("2026-03-08T00:00", "2026-03-08T04:01", ["00:00", "01:00", "03:00", "04:00"]),
        ("2026-11-01T00:00", "2026-11-01T03:01", ["00:00", "01:00", "01:00", "02:00", "03:00"]),
    ],
)
def test_full_hour_dst_transitions_use_real_elapsed_axis_and_local_clock_labels(start, end, expected):
    zone = "America/New_York"
    activity = build_activity([], stamp(start, zone), stamp(end, zone), zone)
    chart = activity_chart(points_from_times(activity.hourly_times, zone), zone)
    assert [point["label"] for point in chart["labels"]] == expected
    assert_regular_ticks(chart, 1)
    assert [point["timestamp"] for point in chart["labels"]] == list(activity.hourly_times)
    assert len({point["timestamp"] for point in chart["labels"]}) == len(expected)


def test_half_hour_dst_uses_timestamp_proportions_and_uniform_elapsed_ticks():
    zone = "Australia/Lord_Howe"
    activity = build_activity([], stamp("2026-10-04T00:00", zone), stamp("2026-10-04T04:01", zone), zone)
    chart = activity_chart(points_from_times(activity.hourly_times, zone), zone)
    assert [point["label"] for point in chart["labels"]] == ["00:00", "01:00", "02:30", "03:30"]
    assert_regular_ticks(chart, 1)
    assert len(chart["points"]) == 5
    points = chart["points"]
    hour_width = points[1]["x"] - points[0]["x"]
    half_hour_width = points[3]["x"] - points[2]["x"]
    assert hour_width == pytest.approx(half_hour_width * 2, abs=0.02)
    assert chart["labels"][-1]["timestamp"] != points[-1]["timestamp"]
    assert chart["labels"][-1]["label"] != "04:00"


def test_starting_in_repeated_half_hour_keeps_actual_30_minute_origin():
    zone = "Australia/Lord_Howe"
    activity = build_activity(
        [], stamp("2026-04-05T01:45", zone, fold=1), stamp("2026-04-05T03:01", zone), zone
    )
    chart = activity_chart(points_from_times(activity.hourly_times, zone), zone)
    assert [label["label"] for label in chart["labels"]] == ["01:30", "02:30"]
    assert_regular_ticks(chart, 1)
    assert [point["x"] for point in chart["points"]] == [70, 366, 958]


def test_legacy_context_without_timestamps_uses_regular_index_spacing():
    activity = hourly_points(24)
    for point in activity:
        point.pop("timestamp")
    chart = activity_chart(activity, "Invalid/Unused")
    assert [point["label"] for point in chart["labels"]] == [
        "14:00",
        "18:00",
        "22:00",
        "02:00",
        "06:00",
        "10:00",
        "14:00",
    ]
    assert [point["x"] for point in chart["labels"]] == [70, 218, 366, 514, 662, 810, 958]
    assert_regular_ticks(chart, 4)


@pytest.mark.parametrize("invalid", [None, True, "not-a-timestamp", float("nan"), float("inf")])
def test_invalid_timestamp_falls_back_for_entire_context(invalid):
    activity = hourly_points(2)
    activity[1]["timestamp"] = invalid
    chart = activity_chart(activity)
    assert [point["x"] for point in chart["points"]] == [70, 514, 958]
    assert_regular_ticks(chart, 1)


def test_renderer_normalization_preserves_timezone_and_actual_timestamp_spacing():
    zone = "Australia/Lord_Howe"
    activity = build_activity([], stamp("2026-10-04T00:00", zone), stamp("2026-10-04T04:01", zone), zone)
    normalized = normalize_context(
        {"activity": points_from_times(activity.hourly_times, zone), "activity_timezone": zone}
    )
    assert [point["timestamp"] for point in normalized["activity"]] == list(activity.hourly_times)
    assert [label["label"] for label in normalized["chart"]["labels"]] == ["00:00", "01:00", "02:30", "03:30"]
