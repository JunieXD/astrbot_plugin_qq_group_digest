import json

import pytest

from qq_group_digest.attribution import poster_points, resolve_names
from qq_group_digest.models import Activity, ActivityMember, Attribution, Digest, Item, Message, Speaker
from qq_group_digest.poster_data import build_activity, decorate_digest, qq_identity

START = 1790323200  # 2026-09-25 16:00 Asia/Shanghai
END = START + 7200


def message(key, sender="111", *, offset=0, name="", text="消息", forwards=()):
    return Message(key, key, START + offset, sender, text, sender_name=name, forward_ids=list(forwards))


def test_activity_counts_original_period_excluding_bot_and_overlap():
    messages = [
        message("overlap", offset=-1, name="旧名字"),
        message("first", offset=0, name="甲"),
        message("forward", offset=60, name="甲", forwards=("f1", "f2")),
        message("image", "222", offset=3600, text=""),
        message("bot", "333", offset=120),
        message("excluded", "444", offset=120),
        message("end", offset=7200),
        message("invalid", "nickname", offset=120),
    ]
    activity = build_activity(messages, START, END, "Asia/Shanghai", bot_id="333", excluded_members=("444",))
    assert activity.total_messages == 3
    assert activity.participants == 2
    assert activity.members == (ActivityMember("111", "甲", 2), ActivityMember("222", "", 1))
    assert activity.hourly[16:18] == (2, 1)
    assert sum(activity.hourly) == 3


def test_activity_deduplicates_and_uses_latest_nonempty_name_in_period():
    messages = [
        message("a", offset=100, name="旧名字"),
        message("b", offset=200, name="新名字"),
        message("b", offset=200, name="新名字"),
        message("c", offset=300, name=""),
        message("outside", offset=-1, name="不应使用"),
        message("d", "333", offset=100, name="同名"),
        message("e", "222", offset=100, name="同名"),
    ]
    activity = build_activity(reversed(messages), START, END, "Asia/Shanghai")
    assert activity.total_messages == 5
    assert activity.members == (
        ActivityMember("111", "新名字", 3),
        ActivityMember("222", "同名", 1),
        ActivityMember("333", "同名", 1),
    )


def test_activity_timezone_and_multiday_buckets_are_actual_local_hours():
    messages = [message("a", offset=10), message("b", offset=86410)]
    activity = build_activity(messages, START, START + 172800, "UTC")
    assert activity.hourly[8] == 2
    assert sum(activity.hourly) == 2


def test_speaker_metadata_tracks_identity_from_aliases_and_never_nickname_guess():
    indexed = {
        "u1": message("a", name="昵称u2"),
        "u2": message("b", "222", name="同名"),
        "u3": message("c", "333", name="同名"),
        "u4": message("d", name="更新名字"),
    }
    item = Item(
        "u3补充",
        "群友u1反馈，u2确认，{{u4}}再补充。https://example.org/u999?q=u1。",
        ("u3", "u1", "u2", "u4"),
    )
    resolved = resolve_names([item], indexed, True)[0]
    assert resolved.title == "“同名”补充"
    assert "“昵称u2”反馈" in resolved.body
    assert "https://example.org/u999?q=u1" in resolved.body
    assert resolved.speakers == (
        Speaker("333", "同名"),
        Speaker("111", "昵称u2"),
        Speaker("222", "同名"),
    )
    plain = resolve_names([Item("同名反馈", "同名说有名额", ())], indexed, True)[0]
    assert plain.speakers == ()


def test_disabled_attribution_has_no_avatar_identity_and_legacy_alias_works():
    indexed = {"m000001": message("a", name="甲")}
    item = Item("反馈", "群友{{m1}}提供名额。", ("m000001",))
    assert resolve_names([item], indexed, True)[0].speakers == (Speaker("111", "甲"),)
    disabled = resolve_names([item], indexed, False)[0]
    assert disabled.body == "群友（原消息作者）提供名额。"
    assert disabled.speakers == ()


def test_poster_metadata_survives_json_and_old_digests_remain_compatible():
    old = {"items": [{"title": "甲", "body": "信息", "sources": ["u1"]}], "notes": []}
    digest = Digest.restore(old)
    assert digest.activity is None and digest.items[0].speakers == ()
    assert json.loads(json.dumps(digest.dump(), ensure_ascii=False)) == old
    enriched = Digest(
        [Item("甲", "信息", ("u1",), (Speaker("111", "姓名"),))],
        ["诊断"],
        Activity(1, 1, (ActivityMember("111", "姓名", 1),), (0,) * 16 + (1,) + (0,) * 7),
    )
    assert Digest.restore(json.loads(json.dumps(enriched.dump(), ensure_ascii=False))) == enriched


def test_decorating_does_not_modify_original_digest():
    digest = Digest([Item("标题", "内容", ())])
    decorated = decorate_digest(digest, [message("a")], START, END, "Asia/Shanghai")
    assert decorated.activity.total_messages == 1
    assert decorated.items == digest.items
    assert digest.activity is None


def test_qq_identity_rejects_invalid_avatar_targets():
    assert qq_identity(" 00111 ") == "111"
    for value in ("0", "-1", "١١١", "111?url=x", "", "1" * 21, None):
        assert qq_identity(value) == ""


def test_exact_body_attribution_spans_survive_json_without_changing_plain_text():
    indexed = {
        "u1": message("a", name="长昵称" * 20),
        "u2": message("b", "222", name="同名"),
    }
    item = resolve_names([Item("群友u2补充", "群友u1认为有名额。\n群友u2听说截止了。", ())], indexed, True)[0]
    assert len(item.body_attributions) == 2
    for span in item.body_attributions:
        assert item.body[span.start : span.end] == f"“{span.name}”"
    restored = Digest.restore(json.loads(json.dumps(Digest([item]).dump(), ensure_ascii=False))).items[0]
    assert restored == item
    assert poster_points(item) == [
        {"text": "群友认为有名额。", "authors": (Speaker("111", "长昵称" * 20),)},
        {"text": "群友听说截止了。", "authors": (Speaker("222", "同名"),)},
    ]
    assert "长昵称" in item.body  # Ordinary text retains the explicit attribution.


def test_poster_points_uses_exact_spans_even_with_duplicate_names_and_many_authors():
    indexed = {f"u{i}": message(str(i), str(i), name="同名") for i in range(1, 6)}
    item = resolve_names(
        [Item("反馈", "、".join(f"群友u{i}" for i in range(1, 6)) + "均认为值得参考。", ())], indexed, True
    )[0]
    points = poster_points(item)
    assert len(points) == 1
    assert len(points[0]["authors"]) == 5
    assert points[0]["authors"] == tuple(Speaker(str(i), "同名") for i in range(1, 6))
    assert "同名" not in points[0]["text"]


def test_poster_points_preserves_literal_names_and_existing_marker_like_text():
    literal = "\ue0000\ue001"
    indexed = {"u1": message("a", name="危险<script>u2\ue000</script>")}
    item = resolve_names(
        [Item("反馈", f"原文标记{literal}；群u1反馈可报名。\n字面昵称“其他同学”不应修改。", ())],
        indexed,
        True,
    )[0]
    points = poster_points(item)
    assert points[0] == {"text": f"原文标记{literal}", "authors": ()}
    assert points[1]["text"] == "群友反馈可报名。"
    assert points[1]["authors"] == (Speaker("111", "危险<script>u2\ue000</script>"),)
    assert points[2] == {"text": "字面昵称“其他同学”不应修改。", "authors": ()}


def test_poster_points_old_and_invalid_metadata_preserve_prose():
    item = Item("标题", "群友“旧名字”说有消息。", (), (Speaker("111", "旧名字"),))
    assert poster_points(item) == [{"text": item.body, "authors": ()}]
    bad = Item("标题", item.body, (), item.speakers, (Attribution(0, 2, "111", "旧名字"),))
    assert poster_points(bad) == poster_points(item)


def test_poster_points_collapses_only_labels_directly_before_explicit_attribution():
    indexed = {"u1": message("a", name="成员甲")}
    item = resolve_names(
        [Item("标题", "群友 u1认为可报名。\n同学 u1听说有名额。\n其他群值得关注。", ())], indexed, True
    )[0]
    points = poster_points(item)
    assert [p["text"] for p in points] == ["群友认为可报名。", "群友听说有名额。", "其他群值得关注。"]


def test_poster_splits_semicolon_separated_statements_with_one_author_each():
    indexed = {f"u{i}": message(str(i), str(i), name=f"成员{i}") for i in range(1, 4)}
    body = (
        "群u1称“混电对学硕有文章要求”，专硕15000学费，学硕8000；"
        "群u2补“学硕发论文就能实习，专硕还得干横向”；"
        "群u3表示组内“一篇论文放实习”，导师人不在成都“名义上放养”。"
    )
    item = resolve_names([Item("反馈", body, ())], indexed, True)[0]
    points = poster_points(item)
    assert [point["text"] for point in points] == [
        "群友称“混电对学硕有文章要求”，专硕15000学费，学硕8000",
        "群友补“学硕发论文就能实习，专硕还得干横向”",
        "群友表示组内“一篇论文放实习”，导师人不在成都“名义上放养”。",
    ]
    assert [point["authors"] for point in points] == [(Speaker(str(i), f"成员{i}"),) for i in range(1, 4)]


@pytest.mark.parametrize("opening,closing", [("“", "”"), ('"', '"'), ("（", "）"), ("(", ")"), ("「", "」")])
def test_poster_preserves_semicolons_inside_quotes_and_parentheses(opening, closing):
    indexed = {f"u{i}": message(str(i), str(i), name=f"成员{i}") for i in range(1, 4)}
    body = f"群u1称{opening}学硕8000；群u2认为专硕15000{closing}；群u3补充学费情况。"
    item = resolve_names([Item("反馈", body, ())], indexed, True)[0]
    points = poster_points(item)
    assert len(points) == 2
    assert points[0]["text"] == f"群友称{opening}学硕8000；群友认为专硕15000{closing}"
    assert points[0]["authors"] == (Speaker("1", "成员1"), Speaker("2", "成员2"))
    assert points[1] == {"text": "群友补充学费情况。", "authors": (Speaker("3", "成员3"),)}


def test_poster_preserves_clause_semicolons_and_only_splits_direct_attributed_followup():
    indexed = {"u1": message("a", name="甲"), "u2": message("b", "222", name="乙")}
    body = "群u1反馈学硕8000；专硕15000；据学校说不收附件;  同学u2提醒名额有限。"
    points = poster_points(resolve_names([Item("反馈", body, ())], indexed, True)[0])
    assert [point["text"] for point in points] == [
        "群友反馈学硕8000；专硕15000；据学校说不收附件",
        "群友提醒名额有限。",
    ]
    assert points[0]["authors"] == (Speaker("111", "甲"),)
    assert points[1]["authors"] == (Speaker("222", "乙"),)


def test_poster_handles_nested_quotes_without_splitting_quoted_testimony():
    indexed = {f"u{i}": message(str(i), str(i), name=f"成员{i}") for i in range(1, 4)}
    body = "群u1称‘我听说“学硕8000；群u2认为专硕15000”’；群u3说don't扩写条件。"
    points = poster_points(resolve_names([Item("反馈", body, ())], indexed, True)[0])
    assert len(points) == 2
    assert points[0]["text"] == "群友称‘我听说“学硕8000；群友认为专硕15000”’"
    assert points[1]["text"] == "群友说don't扩写条件。"


def test_grouped_author_labels_remain_natural_after_shortening():
    from qq_group_digest.attribution import poster_points, resolve_names
    from qq_group_digest.models import Item, Message

    indexed = {
        "u1": Message("1", "1", 1, "12345", "", sender_name="甲"),
        "u2": Message("2", "2", 1, "23456", "", sender_name="乙"),
    }
    item = resolve_names([Item("观点", "群u1、群u2反馈：申请条件因学校而异。", ("u1", "u2"))], indexed, True)[
        0
    ]
    points = poster_points(item)
    assert points[0]["text"] == "多位群友反馈：申请条件因学校而异。"
    assert [author.qq for author in points[0]["authors"]] == ["12345", "23456"]
