"""Local, deterministic newsletter posters with no browser network access."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import struct
import uuid
from contextlib import suppress
from pathlib import Path

from .config import DigestError
from .images import MAX_IMAGE_BYTES

WIDTH = 1080
MAX_AVATAR_BYTES = 2 * 1024 * 1024
THEMES = {
    "cream": {"background": "#f7f3eb", "ink": "#192c3b", "accent": "#d96b50", "mint": "#e4efdf"},
    "mint": {"background": "#edf4ee", "ink": "#183c36", "accent": "#bb6849", "mint": "#d7e9da"},
    "night": {"background": "#182735", "ink": "#eaf0ee", "accent": "#ffc499", "mint": "#314943"},
}
THEME_ALIASES = {"奶油白": "cream", "薄荷绿": "mint", "夜间蓝": "night"}
_IMAGE = re.compile(r"data:image/(?:png|jpeg|webp);base64,([A-Za-z0-9+/=]+)\Z")


class PosterRenderError(DigestError):
    """The image cannot be rendered safely without changing its contents."""


def avatar_url(value):
    """Accept only small embedded raster images, never HTML or remote URLs."""
    if not isinstance(value, str) or len(value) > MAX_AVATAR_BYTES * 4 // 3 + 128:
        return ""
    match = _IMAGE.fullmatch(value)
    if not match:
        return ""
    try:
        raw = base64.b64decode(match.group(1), validate=True)
    except ValueError:
        return ""
    return value if raw and len(raw) <= MAX_AVATAR_BYTES else ""


def _person(value):
    name = str(value.get("name") or "群友")
    return {"name": name, "initial": name[:1], "avatar": avatar_url(value.get("avatar_data_url"))}


def _point(value):
    """Preserve author positions inside a sentence, without parsing model HTML."""
    segments = []
    if value.get("segments") is not None:
        for segment in value["segments"]:
            if segment.get("author"):
                segments.append({"author": _person(segment["author"]), "text": ""})
            elif segment.get("text"):
                segments.append({"author": None, "text": str(segment["text"])})
    else:
        # Old previews use a small author list plus a sentence. Keep them
        # readable while new previews carry the exact positions explicitly.
        segments.extend({"author": _person(author), "text": ""} for author in value.get("authors", []))
        if value.get("text"):
            segments.append({"author": None, "text": str(value["text"])})
    return {"segments": segments}


def _activity_chart(activity):
    """Build trusted SVG coordinates from chronological, counted buckets."""
    if not activity:
        return None
    left, right, top, baseline = 70, 958, 18, 154
    maximum = max((point["count"] for point in activity), default=0)
    scale = maximum or 1
    points = [
        {
            **point,
            "x": round(left + (right - left) * index / max(1, len(activity) - 1), 2),
            "y": round(baseline - (baseline - top) * point["count"] / scale, 2),
        }
        for index, point in enumerate(activity)
    ]
    path = f"M {points[0]['x']},{points[0]['y']}"
    for previous, point in zip(points, points[1:]):
        middle = round((previous["x"] + point["x"]) / 2, 2)
        path += f" C {middle},{previous['y']} {middle},{point['y']} {point['x']},{point['y']}"
    area = f"{path} L {points[-1]['x']},{baseline} L {points[0]['x']},{baseline} Z"
    label_count = min(8, len(points))
    selected = sorted(
        {round(index * (len(points) - 1) / max(1, label_count - 1)) for index in range(label_count)}
    )
    labels = []
    for index in selected:
        point = points[index]
        labels.append(
            {**point, "anchor": "start" if index == 0 else "end" if index == len(points) - 1 else "middle"}
        )
    dates = []
    previous_end = -1
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


def normalize_context(context):
    """Keep model text as text, with a small fixed set of layout options."""
    theme_name = THEME_ALIASES.get(context.get("theme"), context.get("theme", "cream"))
    theme_name = theme_name if theme_name in THEMES else "cream"
    items = []
    for index, raw in enumerate(context.get("items", [])):
        body = raw.get("body", [])
        body = [body] if isinstance(body, str) else body
        points = raw.get("body_points")
        if points is None:
            points = [{"text": text, "authors": []} for text in body]
        items.append(
            {
                "number": index + 1,
                "index": index,
                "title": str(raw.get("title") or "话题"),
                "points": [point for point in map(_point, points) if point["segments"]],
                "authors": [_person(a) for a in raw.get("authors", [])],
            }
        )
    leaders = []
    for index, person in enumerate(context.get("leaderboard", [])):
        leaders.append({**_person(person), "rank": index + 1, "count": max(0, int(person.get("count", 0)))})
    maximum = max((p["count"] for p in leaders), default=1) or 1
    for person in leaders:
        person["percentage"] = round(person["count"] / maximum * 100, 2)
    activity = [
        {
            "label": str(p.get("label", "")),
            "date_label": str(p.get("date_label", "")),
            "count": max(0, int(p.get("count", 0))),
        }
        for p in context.get("activity", [])
    ]
    stats = context.get("stats") or {}
    return {
        "title": str(context.get("title") or "群聊总结"),
        "subtitle": str(context.get("subtitle") or ""),
        "period_label": str(context.get("period_label") or ""),
        "group_avatar": avatar_url(context.get("group_avatar_data_url")),
        "theme_name": theme_name,
        "theme": THEMES[theme_name],
        "stats": {
            "message_count": max(0, int(stats.get("message_count", 0))),
            "speaker_count": max(0, int(stats.get("speaker_count", 0))),
        },
        "show_stats": bool(stats),
        "leaders": leaders,
        "activity": activity,
        "chart": _activity_chart(activity),
        "items": items,
        "topic_count": len(items),
    }


def paginate_heights(card_heights, first_overhead, following_overhead, *, max_height, max_pages, gap=26):
    """Pack whole cards; no shrinking, cutting or silent dropping of topics."""
    pages, page, height = [], [], first_overhead
    for index, card_height in enumerate(card_heights):
        extra = card_height + (gap if page else 0)
        if height + extra > max_height:
            if page:
                pages.append(page)
                page, height = [], following_overhead
            elif not pages and first_overhead > following_overhead:
                # A tall first section may occupy its own page, but topics
                # remain intact on the following, more compact pages.
                pages.append([])
                height = following_overhead
            if height + card_height > max_height:
                raise PosterRenderError(
                    "单个话题过长，无法完整放入海报，请缩短该话题正文或提高海报高度上限。"
                )
            extra = card_height
        page.append(index)
        height += extra
    if page:
        pages.append(page)
    if len(pages) > max_pages:
        raise PosterRenderError("海报页数超过发送上限，请缩短摘要或提高允许发送的图片数量。")
    return pages


class PosterRenderer:
    def __init__(self, cache_dir, *, max_height=7200, max_pages=4):
        self.cache_dir = Path(cache_dir)
        self.max_height = min(16000, max(1800, int(max_height)))
        self.max_pages = min(20, max(1, int(max_pages)))
        self._lock = asyncio.Lock()
        self._playwright = None
        self._browser = None
        self._template_text = (Path(__file__).parent / "assets" / "poster.html").read_text(encoding="utf-8")
        self._font_path = Path(__file__).parent / "assets" / "fonts" / "LXGWWenKaiScreen.woff2"
        self._font_fingerprint = None
        self._font_data_url = None
        self._template = None

    def _load_font(self):
        if self._font_data_url is not None:
            return
        try:
            font_bytes = self._font_path.read_bytes()
        except OSError as exc:
            raise PosterRenderError("本地霞鹜文楷字体资源缺失，请重新安装插件字体资源。") from exc
        if len(font_bytes) < 44 or font_bytes[:4] != b"wOF2":
            raise PosterRenderError("本地霞鹜文楷字体资源已损坏，请重新安装插件字体资源。")
        self._font_fingerprint = hashlib.sha256(font_bytes).hexdigest()
        self._font_data_url = "data:font/woff2;base64," + base64.b64encode(font_bytes).decode("ascii")

    def html(self, context, indices=None, *, page=1, pages=1):
        self._load_font()
        if self._template is None:
            from jinja2 import Environment, StrictUndefined

            env = Environment(autoescape=True, undefined=StrictUndefined)
            self._template = env.from_string(self._template_text)
        normalized = normalize_context(context)
        if indices is not None:
            normalized["items"] = [normalized["items"][i] for i in indices]
        return self._template.render(
            **normalized, page=page, pages=pages, first_page=page == 1, font_data_url=self._font_data_url
        )

    async def _launch(self):
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise PosterRenderError(
                "图片摘要需要本地 Playwright，请安装插件依赖并安装 Chromium 浏览器。"
            ) from exc
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(headless=True)
        except Exception as exc:
            raise PosterRenderError(
                "本地 Chromium 无法启动，请运行 playwright install chromium 并检查运行环境。"
            ) from exc
        return self._browser

    async def _measure(self, page, html):
        await page.set_content(html, wait_until="load")
        font_loaded = await page.evaluate(
            """async () => {
                const faces = await document.fonts.load('44px "DigestWenKaiScreen"', '群聊总结');
                await document.fonts.ready;
                const avatar = document.querySelector('.group-avatar');
                const copy = document.querySelector('.hero-copy');
                for (let i = 0; i < 3; i++) {
                    const size = Math.min(216, Math.ceil(copy.getBoundingClientRect().height));
                    avatar.style.width = `${size}px`;
                    avatar.style.height = `${size}px`;
                }
                return faces.some(face => face.status === 'loaded');
            }"""
        )
        if not font_loaded:
            raise PosterRenderError("本地霞鹜文楷字体加载失败，已停止生成海报，请重新安装插件字体资源。")
        return await page.evaluate(
            """() => ({
                height: Math.ceil(document.querySelector('.poster').getBoundingClientRect().height),
                width: document.documentElement.scrollWidth,
                cards: [...document.querySelectorAll('.topic')].map(el => el.getBoundingClientRect().height),
                gap: parseFloat(getComputedStyle(document.querySelector('.topics')).rowGap),
            })"""
        )

    async def render(self, context):
        normalized = normalize_context(context)
        if not normalized["items"]:
            return []
        self._load_font()
        content_key = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        fingerprint = hashlib.sha256(
            f"{WIDTH}|{self.max_height}|{self.max_pages}|{self._font_fingerprint}|{self._template_text}|{content_key}".encode()
        ).hexdigest()
        async with self._lock:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            manifest = self.cache_dir / f"{fingerprint}.json"
            try:
                metadata = json.loads(manifest.read_text(encoding="utf-8"))
                count = int(metadata["pages"])
                if not 0 < count <= self.max_pages:
                    raise ValueError("invalid page count")
                cached = [self.cache_dir / f"{fingerprint}-{index + 1}.png" for index in range(count)]
                integrity = metadata["images"]
                if len(integrity) != count:
                    raise ValueError("invalid cache manifest")
                valid = True
                for path, expected in zip(cached, integrity):
                    if not path.is_file() or not 24 <= path.stat().st_size <= MAX_IMAGE_BYTES:
                        valid = False
                        break
                    raw = path.read_bytes()
                    if (
                        raw[:8] != b"\x89PNG\r\n\x1a\n"
                        or struct.unpack(">II", raw[16:24]) != (WIDTH, expected["height"])
                        or not 0 < expected["height"] <= self.max_height
                        or hashlib.sha256(raw).hexdigest() != expected["sha256"]
                    ):
                        valid = False
                        break
                if valid:
                    # A reused preview is recent work as well. Do not let
                    # maintenance remove it between rendering and persistence.
                    for path in [*cached, manifest]:
                        path.touch()
                    return cached
            except (OSError, ValueError, KeyError, TypeError):
                pass
            browser = await self._launch()
            try:
                browser_context = await browser.new_context(
                    viewport={"width": WIDTH, "height": 900},
                    device_scale_factor=1,
                    java_script_enabled=False,
                    service_workers="block",
                    locale="zh-CN",
                )
            except Exception as exc:
                raise PosterRenderError("本地浏览器连接中断，请稍后重新生成海报。") from exc
            results = []
            try:
                await browser_context.route("**/*", lambda route: route.abort())
                page = await browser_context.new_page()
                first = await self._measure(page, self.html(context))
                if first["width"] > WIDTH:
                    raise PosterRenderError("海报出现横向溢出，已停止发送，请检查过长的标题或正文。")
                compact = await self._measure(page, self.html(context, [], page=2))
                overhead = first["height"] - sum(first["cards"]) - first["gap"] * (len(first["cards"]) - 1)
                indices = paginate_heights(
                    first["cards"],
                    overhead,
                    compact["height"],
                    max_height=self.max_height,
                    max_pages=self.max_pages,
                    gap=first["gap"],
                )
                image_metadata = []
                for index, selected in enumerate(indices):
                    measured = await self._measure(
                        page, self.html(context, selected, page=index + 1, pages=len(indices))
                    )
                    height = math.ceil(measured["height"])
                    if measured["width"] > WIDTH or height > self.max_height:
                        raise PosterRenderError("海报超出可安全发送的尺寸，已停止发送，请缩短正文。")
                    await page.set_viewport_size({"width": WIDTH, "height": height})
                    path = self.cache_dir / f"{fingerprint}-{index + 1}.png"
                    temporary = self.cache_dir / f".{fingerprint}-{uuid.uuid4().hex}.png"
                    try:
                        await page.screenshot(
                            path=str(temporary), type="png", animations="disabled", caret="hide"
                        )
                        if temporary.stat().st_size > MAX_IMAGE_BYTES:
                            raise PosterRenderError("海报文件超过发送大小上限，请减少本期正文。")
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
                    results.append(path)
                    image_metadata.append(
                        {"height": height, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                    )
                temporary_manifest = self.cache_dir / f".{fingerprint}-{uuid.uuid4().hex}.json"
                try:
                    temporary_manifest.write_text(
                        json.dumps({"pages": len(results), "images": image_metadata}), encoding="utf-8"
                    )
                    temporary_manifest.replace(manifest)
                finally:
                    temporary_manifest.unlink(missing_ok=True)
                return results
            except PosterRenderError:
                for path in results:
                    path.unlink(missing_ok=True)
                raise
            except Exception as exc:
                for path in results:
                    path.unlink(missing_ok=True)
                raise PosterRenderError("本地海报生成失败，请检查浏览器和字体环境；摘要尚未发送。") from exc
            finally:
                with suppress(Exception):
                    await browser_context.close()

    async def close(self):
        async with self._lock:
            browser, playwright = self._browser, self._playwright
            self._browser = self._playwright = None
            if browser is not None:
                with suppress(Exception):
                    await browser.close()
            if playwright is not None:
                with suppress(Exception):
                    await playwright.stop()
