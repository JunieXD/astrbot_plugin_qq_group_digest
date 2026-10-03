import base64
import struct

import pytest

from qq_group_digest.poster_renderer import (
    PosterRenderer,
    PosterRenderError,
    avatar_url,
    normalize_context,
    paginate_heights,
)

PIXEL = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j3ioAAAAASUVORK5CYII="


def context(count=2):
    return {
        "title": "✨ 四非保研群 · 每日总结",
        "subtitle": "四非保研交流群",
        "period_label": "10月2日 05:00 — 10月3日 05:00",
        "stats": {"message_count": 923, "speaker_count": 47},
        "leaderboard": [
            {"name": "一位认真讨论的群友", "count": 41, "avatar_data_url": PIXEL},
            {"name": "长昵称" * 15, "count": 28},
        ],
        "items": [
            {
                "title": f"计算机科研｜第{index + 1}个值得了解的话题",
                "body": ["群友分享的个人经验需要结合具体研究方向理解，不宜把单次结果当作普遍规则。"],
                "authors": [{"name": "小林", "avatar_data_url": PIXEL}],
            }
            for index in range(count)
        ],
    }


def png_size(path):
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", raw[16:24])


def test_model_text_and_nicknames_cannot_create_html(tmp_path):
    data = context(1)
    data["title"] = '<script>fetch("https://bad.invalid")</script>'
    data["items"][0]["body"] = ['<img src="https://bad.invalid" onerror="alert(1)"> & a']
    data["items"][0]["authors"][0]["name"] = '<b onclick="alert(1)">昵称</b>'
    result = PosterRenderer(tmp_path).html(data)
    assert "<script>" not in result
    assert '<img src="https://' not in result
    assert "&lt;script&gt;" in result
    assert "&lt;b onclick=" in result
    assert " &amp; a" in result
    assert "script-src 'none'" in result


@pytest.mark.parametrize(
    "value",
    [
        None,
        "https://bad.invalid/avatar.png",
        "file:///etc/passwd",
        "data:image/png;base64,!",
        "data:image/png;base64,====",
    ],
)
def test_avatar_disallows_remote_local_and_invalid_data(value):
    assert avatar_url(value) == ""


def test_avatar_disallows_svg():
    svg = base64.b64encode(b'<svg onload="alert(1)"></svg>').decode()
    assert avatar_url(f"data:image/svg+xml;base64,{svg}") == ""
    assert avatar_url(PIXEL) == PIXEL


def test_unknown_theme_does_not_inject_css(tmp_path):
    data = context()
    data["theme"] = "red; background:url(https://bad.invalid)"
    result = normalize_context(data)
    assert result["theme_name"] == "cream"
    assert "bad.invalid" not in PosterRenderer(tmp_path).html(data)


def test_pagination_keeps_whole_topics():
    assert paginate_heights([800, 800, 800], 700, 350, max_height=2400, max_pages=3) == [[0, 1], [2]]
    assert paginate_heights([1700, 600], 800, 250, max_height=2400, max_pages=3) == [[], [0], [1]]
    with pytest.raises(PosterRenderError, match="单个话题"):
        paginate_heights([2200], 500, 300, max_height=2400, max_pages=3)
    with pytest.raises(PosterRenderError, match="页数"):
        paginate_heights([1000] * 8, 700, 350, max_height=2400, max_pages=2)


async def test_empty_digest_does_not_launch_browser(tmp_path):
    renderer = PosterRenderer(tmp_path)
    renderer._font_path = tmp_path / "absent-font.woff2"
    assert await renderer.render({"items": []}) == []
    assert not list(tmp_path.iterdir())


async def test_missing_font_is_local_render_error_not_initialization_error(tmp_path):
    renderer = PosterRenderer(tmp_path)
    renderer._font_path = tmp_path / "absent-font.woff2"
    with pytest.raises(PosterRenderError, match="字体资源缺失"):
        await renderer.render(context())
    assert renderer._playwright is None


def test_segments_preserve_multiple_author_positions(tmp_path):
    data = context(1)
    data["items"][0]["authors"] = []
    data["items"][0]["body_points"] = [
        {
            "segments": [
                {"author": {"name": "小林"}},
                {"text": "反馈这个方向值得了解；"},
                {"author": {"name": "小赵"}},
                {"text": "补充了不同的经历。"},
            ]
        }
    ]
    result = PosterRenderer(tmp_path).html(data)
    first, second = result.index("小林</span>"), result.index("小赵</span>")
    assert first < result.index("反馈这个方向值得了解；") < second < result.index("补充了不同的经历。")
    assert 'point-authors">' not in result


def test_activity_order_and_tick_limits():
    data = context()
    data["activity"] = [
        {"label": str((13 + hour) % 24), "date_label": "10月2日" if hour < 11 else "10月3日", "count": hour}
        for hour in range(25)
    ]
    chart = normalize_context(data)["chart"]
    assert [point["count"] for point in chart["points"]] == list(range(25))
    assert len(chart["labels"]) <= 8
    assert chart["labels"][0]["label"] == chart["labels"][-1]["label"] == "13"
    assert [date["label"] for date in chart["dates"]] == ["10月2日", "10月3日"]
    assert " C " in chart["line"]


def test_local_font_and_numeric_badges(tmp_path):
    renderer = PosterRenderer(tmp_path)
    html = renderer.html(context())
    assert "data:font/woff2;base64," in html
    assert "font-src data:" in html
    assert 'class="topic-number">1</span>' in html
    assert "按小时合计" not in html
    assert "按信息价值排序" not in html
    assert "群聊手记" not in html


@pytest.fixture
async def renderer(tmp_path):
    pytest.importorskip("playwright.async_api")
    result = PosterRenderer(tmp_path)
    try:
        yield result
    finally:
        await result.close()


async def test_real_browser_image_size_and_cache(renderer, monkeypatch):
    images = await renderer.render(context())
    assert len(images) == 1
    width, height = png_size(images[0])
    assert width == 1080
    assert 1000 < height < 7200
    original = images[0].read_bytes()

    async def must_not_launch():
        raise AssertionError("cached poster launched browser")

    monkeypatch.setattr(renderer, "_launch", must_not_launch)
    assert await renderer.render(context()) == images
    assert images[0].read_bytes() == original


async def test_real_browser_paginates_without_smaller_text(renderer):
    renderer.max_height = 2400
    data = context(4)
    for item in data["items"]:
        item["body"] = ["经验需要结合研究方向和适用条件理解。" * 9]
    images = await renderer.render(data)
    assert 2 <= len(images) <= 4
    assert all(png_size(path)[0] == 1080 and png_size(path)[1] <= 2400 for path in images)


async def test_corrupt_nonempty_cached_image_is_regenerated(renderer):
    images = await renderer.render(context())
    expected = images[0].read_bytes()
    images[0].write_bytes(expected[:-8] + b"corrupt!")
    assert await renderer.render(context()) == images
    assert images[0].read_bytes() == expected


async def test_real_browser_rejects_giant_topic(renderer):
    data = context(1)
    data["items"][0]["body"] = ["这是不能被截断或隐藏的正文。" * 1000]
    with pytest.raises(PosterRenderError, match="单个话题"):
        await renderer.render(data)
    assert not list(renderer.cache_dir.glob("*.png"))


async def test_failed_render_does_not_leave_partial_cache(renderer, monkeypatch):
    original = renderer._measure

    async def overflow(page, html):
        measured = await original(page, html)
        measured["width"] = 2000
        return measured

    monkeypatch.setattr(renderer, "_measure", overflow)
    with pytest.raises(PosterRenderError, match="横向溢出"):
        await renderer.render(context())
    assert not list(renderer.cache_dir.iterdir())
