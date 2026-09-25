import json
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from qq_group_digest.config import DigestError, GenerationOptions, Limits, Pace, parse_settings
from qq_group_digest.schedule import latest_boundary, next_boundary


def test_schema_defaults_and_example_match_runtime():
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    defaults = {k: v["default"] for k, v in schema.items() if "default" in v}
    for name, cls in [("pace", Pace), ("limits", Limits), ("llm_generation", GenerationOptions)]:
        values = {k: v["default"] for k, v in schema[name]["items"].items()}
        assert values == asdict(cls())
        defaults[name] = values
    assert not parse_settings(defaults).enabled
    example = json.loads(Path("examples/config.json").read_text(encoding="utf-8"))
    parsed = parse_settings(example)
    assert parsed.tasks[0].targets == ("987654321",)
    assert parsed.tasks[0].mode == "合并转发·分条"


@pytest.mark.parametrize(
    "patch",
    [
        {"source_group": "not-a-group"},
        {"times": ["6:00"]},
        {"times": []},
        {"mode": "invalid"},
        {"include_source": False, "target_groups": []},
        {"enabled": "false"},
        {"advanced": {"timezone": "Invalid/Zone"}},
        {"advanced": {"summary_chars": True}},
        {"advanced": {"overlap_minutes": -1}},
        {"advanced": {"attribute_speakers": "true"}},
    ],
)
def test_invalid_task_rejected(patch):
    with pytest.raises(DigestError):
        parse_settings({"tasks": [{"source_group": "123456789", **patch}]})


def test_duplicate_source_and_bad_ranges():
    with pytest.raises(DigestError):
        parse_settings({"tasks": [{"source_group": "123456789"}] * 2})
    with pytest.raises(DigestError):
        parse_settings({"pace": {"send_min_seconds": 30, "send_max_seconds": 2}})
    with pytest.raises(DigestError):
        parse_settings({"pace": {"read_min_seconds": float("nan")}})


def test_name_change_preserves_key(task):
    assert replace(task, name="新的名称").key == task.key
    assert replace(task, source_group="777777777").key != task.key


def test_boundaries_use_scheduled_time_not_execution_time(task):
    now = datetime(2026, 9, 25, 18, 3, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    latest = datetime.fromtimestamp(latest_boundary(task, now), ZoneInfo(task.timezone))
    following = datetime.fromtimestamp(next_boundary(task, now), ZoneInfo(task.timezone))
    assert (latest.hour, latest.minute) == (18, 0)
    assert (following.day, following.hour) == (26, 6)


def test_dst_nonexistent_hour_is_skipped(task):
    t = replace(task, timezone="America/New_York", times=("02:30",))
    now = datetime(2026, 3, 8, 0, tzinfo=ZoneInfo(t.timezone)).timestamp()
    due = datetime.fromtimestamp(next_boundary(t, now), ZoneInfo(t.timezone))
    assert (due.day, due.hour, due.minute) == (9, 2, 30)


def test_dst_repeated_time_fires_once(task):
    t = replace(task, timezone="America/New_York", times=("01:30",))
    now = datetime(2026, 11, 1, 1, 45, tzinfo=ZoneInfo(t.timezone), fold=0).timestamp()
    following = datetime.fromtimestamp(next_boundary(t, now), ZoneInfo(t.timezone))
    assert following.day == 2


def test_package_version_and_metadata_consistent():
    from qq_group_digest import __version__

    metadata = yaml.safe_load(Path("metadata.yaml").read_text(encoding="utf-8"))
    assert metadata["version"] == "v" + __version__
    assert metadata["support_platforms"] == ["aiocqhttp"]
