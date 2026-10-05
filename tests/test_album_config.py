import json
from dataclasses import replace
from pathlib import Path

import pytest

from qq_group_digest.config import DigestError, Task, parse_settings


def configured(album=None, **values):
    task = {"source_group": "123456789", **values}
    if album is not None:
        task["album"] = album
    return parse_settings({"tasks": [task]}).tasks[0]


def test_legacy_settings_do_not_enable_album_or_change_delivery_targets():
    task = configured(target_groups=["987654321"])
    assert not task.album_enabled
    assert task.album_name == "鼠群日报"
    assert task.album_target_groups == ()
    assert task.targets == ("987654321", "123456789")


def test_album_targets_are_normalized_deduplicated_and_independent_of_chat():
    task = configured(
        {"enabled": True, "target_groups": [987654321, " 987654321 ", "555555555"], "name": " 日报 "},
        target_groups=["444444444"],
    )
    assert task.album_enabled and task.album_name == "日报"
    assert task.album_target_groups == ("987654321", "555555555")
    assert task.targets == ("444444444", "123456789")


def test_album_can_be_enabled_for_plain_message_delivery():
    task = configured({"enabled": True, "target_groups": ["987654321"]}, mode="普通消息·整篇")
    assert task.album_enabled and task.mode == "普通消息·整篇"


def test_disabled_album_accepts_empty_selection_and_name():
    task = configured({"enabled": False, "target_groups": [], "name": ""})
    assert not task.album_enabled and task.album_name == ""


@pytest.mark.parametrize(
    "album",
    [
        {"enabled": True},
        {"enabled": True, "target_groups": ["987654321"], "name": " "},
        {"enabled": "true"},
        {"target_groups": "987654321"},
        {"target_groups": ["invalid"]},
        {"target_groups": [True]},
        {"target_groups": ["987654321"] * 21},
        {"name": None},
        {"name": ["日报"]},
        {"name": "日报\n标题"},
        {"name": "日报\u2028标题"},
        {"name": "日报\x7f标题"},
        {"name": "长" * 61},
    ],
    ids=[
        "enabled_without_target",
        "enabled_without_name",
        "invalid_switch",
        "targets_not_list",
        "invalid_group",
        "boolean_group",
        "too_many_groups",
        "null_name",
        "name_not_string",
        "newline_name",
        "line_separator_name",
        "control_name",
        "long_name",
    ],
)
def test_bad_album_settings_fail_with_readable_configuration_error(album):
    with pytest.raises(DigestError):
        configured(album)


def test_album_task_roundtrip_and_legacy_snapshot_are_backward_compatible():
    task = configured({"enabled": True, "target_groups": ["987654321"], "name": "日报"})
    assert Task.restore(json.loads(json.dumps(task.dump(), ensure_ascii=False))) == task
    legacy = task.dump()
    for key in ("album_enabled", "album_target_groups", "album_name"):
        legacy.pop(key)
    restored = Task.restore(legacy)
    assert not restored.album_enabled and restored.album_name == "鼠群日报"
    assert restored.album_target_groups == ()
    assert replace(task, album_name="另一本日报").key == task.key


def test_album_schema_defaults_match_runtime():
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    album = schema["tasks"]["templates"]["task"]["items"]["album"]["items"]
    values = {key: value["default"] for key, value in album.items()}
    task = configured(values)
    assert task.album_enabled == Task.album_enabled == values["enabled"]
    assert task.album_name == Task.album_name == values["name"]
    assert task.album_target_groups == Task.album_target_groups == tuple(values["target_groups"])
