"""Presentation boundary: local statistics, cached avatars and browser posters."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .attribution import poster_points
from .config import IMAGE_MODE, DigestError
from .render import default_title, display_text, make_payloads, readable_body
from .schedule import poster_period


class Presentation:
    def __init__(self, root, journal):
        from .avatars import AvatarCache
        from .poster_renderer import PosterRenderer

        self.root, self.journal = Path(root), journal
        self.avatars = AvatarCache(self.root, journal)
        self.renderer = PosterRenderer(self.root / "posters", max_height=12000)

    async def payloads(self, task, digest, start, end, account, limits, mode=None):
        mode = mode or task.mode
        if mode != IMAGE_MODE or not digest.items:
            return make_payloads(task, digest, start, end, account, limits, mode)
        began = time.monotonic()
        try:
            context = await self.context(task, digest, start, end)
            images = await asyncio.wait_for(self.renderer.render(context), timeout=90)
            if not images or len(images) + bool(task.poster_followup_text) > limits.max_parts:
                raise DigestError("海报页数超过发送上限，请减少主题数或正文长度。")
            result = [
                {
                    "message": [
                        {
                            "type": "image",
                            "data": {
                                "file": path.resolve().as_uri(),
                            },
                        }
                    ]
                }
                for path in images
            ]
            if task.poster_followup_text:
                result.append({"message": [{"type": "text", "data": {"text": task.poster_followup_text}}]})
            self.journal.record(
                "本地海报渲染完成",
                group=task.source_group,
                pages=len(images),
                duration_ms=round((time.monotonic() - began) * 1000),
            )
            return result
        except Exception as exc:
            self.journal.record("本地海报渲染失败", group=task.source_group, error_type=type(exc).__name__)
            if not task.poster_fallback_to_plain:
                raise DigestError("海报渲染未完成；请检查 Chromium 安装和插件日志，摘要尚未发送。") from exc
            try:
                result = make_payloads(task, digest, start, end, account, limits, "普通消息·整篇")
            except DigestError as length_error:
                raise DigestError("海报渲染失败且正文超过普通消息上限，已暂缓发送。") from length_error
            self.journal.record("海报按配置改用普通消息", group=task.source_group)
            return result

    async def context(self, task, digest, start, end):
        activity = digest.activity
        leaders = list(activity.members[: task.poster_leaderboard_size]) if activity else []
        users = {s.qq for item in digest.items for s in item.speakers}
        users.update(m.qq for m in leaders)
        faces = await self.avatars.load(users, task.source_group) if task.poster_show_avatars else {}

        def face(member):
            return {
                "name": member.name or "昵称未提供",
                "avatar_data_url": faces.get("user:" + member.qq, ""),
            }

        items = []
        for item in digest.items:
            points = poster_points(item) if task.poster_show_avatars else []
            attributed = any(point["authors"] for point in points)
            body = [p.removeprefix("• ") for p in readable_body(item.body).split("\n\n") if p]
            items.append(
                {
                    "title": display_text(item.title),
                    "body": body,
                    "body_points": [
                        {
                            "segments": [
                                {"author": face(segment["speaker"])} if "speaker" in segment else segment
                                for segment in p["segments"]
                            ]
                        }
                        for p in points
                    ]
                    if attributed
                    else [{"text": p, "authors": []} for p in body],
                    "authors": [face(s) for s in item.speakers]
                    if task.poster_show_avatars and not attributed
                    else [],
                }
            )

        zone = ZoneInfo(task.timezone)
        # Old saved digests used clock-hour totals that cannot be reliably
        # unfolded across a date boundary. Do not invent a timeline for them.
        activity_points = []
        if activity and task.poster_show_activity:
            dates = [datetime.fromtimestamp(stamp, zone) for stamp in activity.hourly_times]
            for stamp, count in zip(activity.hourly_times, activity.hourly):
                local = datetime.fromtimestamp(stamp, zone)
                label = f"{local.hour:02d}" if local.minute == 0 else f"{local:%H:%M}"
                # Repeated DST hours are distinct data points; make that visible.
                if any(
                    other.date() == local.date()
                    and other.hour == local.hour
                    and other.utcoffset() != local.utcoffset()
                    for other in dates
                ):
                    label += f" ({local:%z})"
                activity_points.append(
                    {
                        "label": label,
                        "date_label": f"{local.month}月{local.day}日",
                        "timestamp": stamp,
                        "count": count,
                    }
                )

        return {
            "title": task.content_title or default_title(task),
            "subtitle": digest.group_name or f"QQ群 {task.source_group}",
            "period_label": poster_period(task, start, end),
            "theme": task.poster_theme,
            "group_avatar_data_url": faces.get("group:" + task.source_group, ""),
            "stats": {
                "message_count": activity.total_messages if activity else 0,
                "speaker_count": activity.participants if activity else 0,
            },
            "leaderboard": [{**face(m), "count": m.count} for m in leaders],
            "items": items,
            "activity": activity_points,
        }

    async def close(self):
        await self.renderer.close()

    def cleanup(self, protected, days=7):
        cutoff = time.time() - days * 86400
        # Only old unreferenced render artifacts. Pending and uncertain sends
        # retain their exact image even beyond the ordinary retention period.
        removable, total = [], 0
        for path in (self.root / "posters").glob("*"):
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
                total += stat.st_size
                if path.resolve().as_uri() not in protected:
                    removable.append((stat.st_mtime, path, stat.st_size))
            except OSError:
                continue
        for modified, path, size in sorted(removable):
            if modified >= cutoff and total <= 256 * 1024 * 1024:
                continue
            try:
                path.unlink(missing_ok=True)
                total -= size
            except OSError:
                self.journal.record("海报缓存清理失败", error_type="OSError")
