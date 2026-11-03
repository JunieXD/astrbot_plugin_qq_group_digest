import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from qq_group_digest.attribution import poster_points, resolve_names
from qq_group_digest.config import DigestError, Limits, Task, parse_settings
from qq_group_digest.display_names import clean_name, display_digest
from qq_group_digest.models import Activity, ActivityMember, Attribution, Digest, Item, Message, Speaker
from qq_group_digest.render import full_text, make_payloads

from .conftest import NOW
from .test_poster_integration import presentation


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("junie（0.15倍率gpt中转）", "junie"),
        ("小明(低价保研辅导)", "小明"),
        ("kk(mac孝子)", "kk"),
        ("小明（普通备注）(实习中)", "小明"),
        ("年级（说明[联系方式]）-学校-昵称", "年级-学校-昵称"),
        ("昵称（备注)", "昵称"),
        ("昵称(备注）", "昵称"),
        ("昵称（说明[嵌套）-学校", "昵称-学校"),
        ("昵称（漏写右括号", "昵称"),
        ("昵称）-学校", "昵称-学校"),
        ('南宫云洛print("\u2067("!', "南宫云洛print"),
        ("lucky | 在线求职中(日常实习)", "lucky"),
        ("lucky｜幸福主义", "lucky"),
        ("lucky丨签名", "lucky"),
        ("23-uestc-小明-接保研辅导", "23-uestc-小明"),
        ("小明 ecnu 低价接科研辅导", "小明 ecnu"),
        ("小明-高价接论文辅导-私聊", "小明"),
        ("小明-辅导版-看空间", "小明"),
        ("小明 低价代充GPT 可开票", "小明"),
        ("小明-代购ChatGPT", "小明"),
        ("小明-出售 Codex 额度 私聊", "小明"),
        ("小明-出codex额度", "小明"),
        ("小明-不接保研辅导", "小明-不接保研辅导"),
        ("23-uestc-小明", "23-uestc-小明"),
        ("24-四非 洛书", "24-四非 洛书"),
        ("25-jxau-Dusk witnesses devout believers", "25-jxau-Dusk witnesses devout believers"),
        ("小明 GPT 学习笔记", "小明 GPT 学习笔记"),
        ("小明-研究保研辅导效果", "小明-研究保研辅导效果"),
        ("小明-辅导员", "小明-辅导员"),
        ("23-⚡️-👩‍💻小明(实习中)", "23-⚡️-👩‍💻小明"),
        ("  23 - jxufe - 鸭鸭山u  hhhz  ", "23 - jxufe - 鸭鸭山u hhhz"),
        ("昵称\u200b\u202e\u2066（备注）\ufeff", "昵称"),
        ("小明_<3", "小明_<3"),
        ("危险<script>u2\ue000</script>", "危险<script>u2\ue000</script>"),
        ("（全是备注）", "群友"),
        ("接保研辅导", "群友"),
        ("|签名", "群友"),
        ("", "群友"),
    ],
)
def test_clean_name_preserves_subject_and_removes_only_defined_formats(raw, expected):
    assert clean_name(raw) == expected
    assert clean_name(clean_name(raw)) == expected


@pytest.mark.parametrize("opening,closing", tuple(zip("([【［〔{｛「『《〈", ")]】］〕}｝」』》〉")))
def test_all_annotation_brackets_remove_contents(opening, closing):
    assert clean_name(f"昵称{opening}普通备注{closing}") == "昵称"


def original_digest():
    messages = {
        "u1": Message("a", "1", NOW, "111111", "", sender_name="同名（低价保研辅导）"),
        "u2": Message("b", "2", NOW, "222222", "", sender_name="同名（代充GPT）"),
    }
    item = resolve_names(
        [Item("u1补充", "群友u1反馈，学费8000；但u2提醒可能变更，u1补充看公告。", ("u1", "u2"))],
        messages,
        True,
    )[0]
    activity = Activity(
        10,
        2,
        (ActivityMember("111111", messages["u1"].sender_name, 6), ActivityMember("222222", "同名（研0）", 4)),
        (10,),
        (NOW,),
    )
    return Digest([item], activity=activity)


def test_display_copy_rebuilds_spans_without_changing_identity_or_history():
    original = original_digest()
    saved = copy.deepcopy(original.dump())
    shown = display_digest(original)
    item = shown.items[0]
    assert item.title == "“同名”补充"
    assert item.body == "群友“同名”反馈，学费8000；但“同名”提醒可能变更，“同名”补充看公告。"
    assert [span.qq for span in item.body_attributions] == ["111111", "222222", "111111"]
    for span in item.body_attributions:
        assert item.body[span.start : span.end] == f"“{span.name}”"
    assert item.speakers == (Speaker("111111", "同名"), Speaker("222222", "同名"))
    assert shown.activity.members == (
        ActivityMember("111111", "同名", 6),
        ActivityMember("222222", "同名", 4),
    )
    assert item.sources == original.items[0].sources
    assert shown.activity.total_messages == 10 and shown.activity.participants == 2
    assert original.dump() == saved
    assert display_digest(shown) == shown
    points = poster_points(item)
    assert [
        segment["speaker"].qq for point in points for segment in point["segments"] if "speaker" in segment
    ] == ["111111", "222222", "111111"]


def test_legacy_quoted_names_are_cleaned_without_guessing_new_avatar_attribution():
    name = "小明（备注）"
    item = Item(
        "反馈",
        f"群友“{name}”反馈学费。字面“小明（备注）甲”不变。https://example.com/“{name}”",
        (),
        (Speaker("111111", name),),
    )
    shown = display_digest(Digest([item])).items[0]
    assert shown.body == f"群友“小明”反馈学费。字面“小明（备注）甲”不变。https://example.com/“{name}”"
    assert shown.body_attributions == ()
    assert poster_points(shown)[0]["authors"] == ()
    bad = replace(item, body_attributions=(Attribution(0, 2, "111111", name),))
    assert display_digest(Digest([bad])).items[0].body_attributions == ()


@pytest.mark.parametrize("mode", ["普通消息·整篇", "普通消息·分条", "合并转发·整篇", "合并转发·分条"])
def test_text_forward_and_preview_use_same_names_and_keep_original_when_disabled(task, mode):
    task = replace(task, mode=mode)
    digest = original_digest()
    payloads = make_payloads(task, digest, NOW - 3600, NOW, "333333", Limits())
    serialized = json.dumps(payloads, ensure_ascii=False)
    assert "低价保研辅导" not in serialized and "代充GPT" not in serialized
    assert "同名" in serialized
    assert "低价保研辅导" not in full_text(task, digest, NOW - 3600, NOW)
    raw = make_payloads(replace(task, clean_display_names=False), digest, NOW - 3600, NOW, "333333", Limits())
    assert "低价保研辅导" in json.dumps(raw, ensure_ascii=False)


@pytest.mark.parametrize("avatars", [True, False])
async def test_poster_chips_leaderboard_and_plain_fallback_share_clean_names(
    task, journal, tmp_path, avatars
):
    task = replace(task, poster_show_avatars=avatars)
    presenter = presentation(tmp_path, journal)
    context = await presenter.context(task, original_digest(), NOW - 3600, NOW)
    assert [p["name"] for p in context["leaderboard"]] == ["同名", "同名"]
    content = json.dumps(context, ensure_ascii=False)
    assert "低价保研辅导" not in content and "代充GPT" not in content
    if avatars:
        assert presenter.avatars.calls[0][0] == {"111111", "222222"}
        assert "user:111111" not in content  # Only the corresponding avatar data reaches the page.
    presenter.renderer.failure = True
    payloads = await presenter.payloads(
        replace(task, mode="图片海报"), original_digest(), NOW - 3600, NOW, "333333", Limits()
    )
    assert "低价保研辅导" not in json.dumps(payloads, ensure_ascii=False)


def test_setting_default_validation_and_old_snapshot_compatibility(task):
    schema = json.loads(Path("_conf_schema.json").read_text())
    default = schema["tasks"]["templates"]["task"]["items"]["advanced"]["items"]["clean_display_names"][
        "default"
    ]
    assert default is True and task.clean_display_names
    for enabled in (True, False):
        setting = parse_settings(
            {"tasks": [{"source_group": task.source_group, "advanced": {"clean_display_names": enabled}}]}
        ).tasks[0]
        assert setting.clean_display_names is enabled
        assert setting.key == task.key
    with pytest.raises(DigestError):
        parse_settings(
            {"tasks": [{"source_group": task.source_group, "advanced": {"clean_display_names": "true"}}]}
        )
    legacy = task.dump()
    legacy.pop("clean_display_names")
    assert Task.restore(legacy).clean_display_names
