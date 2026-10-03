import json
from datetime import datetime
from zoneinfo import ZoneInfo

from qq_group_digest.models import Activity, Digest, Message
from qq_group_digest.poster_data import build_activity, decorate_digest


def stamp(text, timezone, *, fold=0):
    return int(datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(timezone), fold=fold).timestamp())


def message(key, time):
    return Message(key, key, time, "111111", "消息")


def labels(activity, timezone):
    return [datetime.fromtimestamp(value, ZoneInfo(timezone)).isoformat() for value in activity.hourly_times]


def test_chronological_bins_cross_midnight_with_partial_first_and_last_hours():
    zone = "Asia/Shanghai"
    start, end = stamp("2026-10-02T18:15", zone), stamp("2026-10-03T06:45", zone)
    messages = [
        message("outside-before", start - 1),
        message("first", start),
        message("late", stamp("2026-10-02T23:59", zone)),
        message("midnight", stamp("2026-10-03T00:00", zone)),
        message("last", end - 1),
        message("outside-after", end),
    ]
    activity = build_activity(messages, start, end, zone)
    assert len(activity.hourly) == len(activity.hourly_times) == 13
    assert labels(activity, zone)[0] == "2026-10-02T18:00:00+08:00"
    assert labels(activity, zone)[-1] == "2026-10-03T06:00:00+08:00"
    assert activity.hourly[0] == activity.hourly[5] == activity.hourly[6] == activity.hourly[-1] == 1
    assert sum(activity.hourly) == activity.total_messages == 4


def test_multiday_window_never_collapses_same_clock_hour_on_different_dates():
    zone = "Asia/Shanghai"
    start, end = stamp("2026-10-01T00:00", zone), stamp("2026-10-04T00:00", zone)
    activity = build_activity(
        [message("first", start + 3600), message("second", start + 90000)], start, end, zone
    )
    assert len(activity.hourly) == 72
    assert activity.hourly[1] == activity.hourly[25] == 1
    assert activity.hourly_times[25] - activity.hourly_times[1] == 86400
    assert sum(activity.hourly) == 2


def test_dst_spring_forward_skips_nonexistent_hour():
    zone = "America/New_York"
    start, end = stamp("2026-03-08T00:30", zone), stamp("2026-03-08T04:30", zone)
    activity = build_activity(
        [
            message("before", stamp("2026-03-08T01:59", zone)),
            message("after", stamp("2026-03-08T03:00", zone)),
        ],
        start,
        end,
        zone,
    )
    assert labels(activity, zone) == [
        "2026-03-08T00:00:00-05:00",
        "2026-03-08T01:00:00-05:00",
        "2026-03-08T03:00:00-04:00",
        "2026-03-08T04:00:00-04:00",
    ]
    assert activity.hourly == (0, 1, 1, 0)


def test_dst_fall_back_separates_repeated_clock_hour_and_offsets():
    zone = "America/New_York"
    start, end = stamp("2026-11-01T00:30", zone), stamp("2026-11-01T03:30", zone)
    activity = build_activity(
        [
            message("first-fold", stamp("2026-11-01T01:30", zone)),
            message("second-fold", stamp("2026-11-01T01:30", zone, fold=1)),
        ],
        start,
        end,
        zone,
    )
    assert labels(activity, zone) == [
        "2026-11-01T00:00:00-04:00",
        "2026-11-01T01:00:00-04:00",
        "2026-11-01T01:00:00-05:00",
        "2026-11-01T02:00:00-05:00",
        "2026-11-01T03:00:00-05:00",
    ]
    assert activity.hourly == (0, 1, 1, 0, 0)


def test_half_hour_dst_transition_creates_actual_partial_hour_bins():
    zone = "Australia/Lord_Howe"
    start, end = stamp("2026-10-04T00:00", zone), stamp("2026-10-04T04:00", zone)
    activity = build_activity([message("partial", stamp("2026-10-04T02:45", zone))], start, end, zone)
    assert labels(activity, zone) == [
        "2026-10-04T00:00:00+10:30",
        "2026-10-04T01:00:00+10:30",
        "2026-10-04T02:30:00+11:00",
        "2026-10-04T03:00:00+11:00",
    ]
    assert activity.hourly == (0, 0, 1, 0)
    assert activity.hourly_times[3] - activity.hourly_times[2] == 1800


def test_window_starting_in_repeated_partial_hour_uses_its_real_start():
    zone = "Australia/Lord_Howe"
    start, end = stamp("2026-04-05T01:45", zone, fold=1), stamp("2026-04-05T03:00", zone)
    activity = build_activity([message("first", start)], start, end, zone)
    assert labels(activity, zone) == ["2026-04-05T01:30:00+10:30", "2026-04-05T02:00:00+10:30"]
    assert activity.hourly == (1, 0)


def test_one_week_range_is_bounded_and_keeps_empty_hours():
    zone = "Asia/Kathmandu"
    start = stamp("2026-10-01T10:15", zone)
    activity = build_activity([], start, start + 168 * 3600, zone)
    assert len(activity.hourly) == 169
    assert activity.hourly_times[0] == stamp("2026-10-01T10:00", zone)
    assert not any(activity.hourly)
    assert activity.total_messages == activity.participants == 0


def test_legacy_activity_and_group_name_json_remain_compatible():
    legacy = {"total_messages": 0, "participants": 0, "members": [], "hourly": [0] * 24}
    restored = Activity.restore(legacy)
    assert restored.hourly_times == ()
    assert json.loads(json.dumps(restored.dump())) == legacy
    start, end = stamp("2026-10-01T08:00", "Asia/Shanghai"), stamp("2026-10-02T08:00", "Asia/Shanghai")
    digest = decorate_digest(Digest([], group_name="实际群名"), [], start, end, "Asia/Shanghai")
    assert digest.group_name == "实际群名"
    assert Digest.restore(json.loads(json.dumps(digest.dump(), ensure_ascii=False))) == digest
    assert "group_name" not in Digest([]).dump()
