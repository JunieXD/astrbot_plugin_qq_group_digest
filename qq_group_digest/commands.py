"""Private AstrBot-administrator operations, with no implicit group publication."""

import asyncio
import re
import time
from collections import Counter
from pathlib import Path

from .config import DigestError, Task, identifier
from .preview import Preview
from .schedule import next_boundary, period_text

HELP = """群聊摘要（仅 AstrBot 管理员私聊使用）：
/群摘要 状态 [来源群号]
/群摘要 预览 来源群号
/群摘要 预览 来源群号 刷新
/群摘要 统计 [来源群号] [天数]
/群摘要 记录 来源群号
/群摘要 详情 调用编号
/群摘要 导出 来源群号 [天数]
/群摘要 执行 来源群号
/群摘要 暂停 来源群号
/群摘要 恢复 来源群号
/群摘要 重试 批次编号
/群摘要 核对 批次编号
/群摘要 跳过 批次编号 [目标群号]
预览会调用模型，按配置的展示方式在私聊返回，不推进定时进度。
执行会处理最近一个计划时点，按配置投递；已处理的批次不会重复生成。
核对只查历史；跳过会放弃指定批次的生成或尚未完成的投递，不会重新发送。"""

LABELS = {
    "superseded": "已过期或并入补报",
    "queued": "等待读取",
    "fetched": "等待生成",
    "generated": "等待投递",
    "complete": "已处理",
    "failed": "生成失败",
    "pending": "待发送",
    "submitted": "已提交",
    "unknown": "待核对",
    "sent": "已发送",
    "skipped": "已跳过",
    "blocked": "投递准备失败",
}


class Commands:
    def __init__(self, service, *, notify=None):
        self.s = service
        self.notify = notify

    async def run(self, text):
        parts = text.strip().lstrip("/").split()
        if parts and parts[0] in {"群摘要", "qgdigest"}:
            parts.pop(0)
        if not parts or parts == ["帮助"]:
            return HELP
        action = parts[0]
        if len(text) > 200 or len(parts) > 3:
            return HELP
        if action in {"统计", "记录", "详情", "导出"}:
            return await self.statistics(parts)
        if action == "状态" and len(parts) in (1, 2):
            tasks = (
                [self.s.settings().find(identifier(parts[1], "来源群"))]
                if len(parts) == 2
                else self.s.settings().tasks
            )
            result = []
            for task in tasks:
                recent = await self.s.store.call("latest", task.key, 3)
                attention, attention_count = await self.s.store.call("attention", task.key)
                runs = list({r["id"]: r for r in [*attention, *recent]}.values())
                active = await self.s.allowed(task.key)
                due = next_boundary(task, self.s.clock())
                result.append(
                    f"{task.label}（{task.source_group}）：{'已启用' if active else '已暂停/未启用'}\n"
                    f"下一计划时间：{period_text(task, due, due).split('—')[0]}（{task.timezone}）"
                )
                progress = self.s.previews.get(task.key)
                if progress:
                    elapsed = max(0, int(self.s.clock() - progress["started"]))
                    result.append(
                        f"私聊预览：{progress['phase']}，已查询 {progress['pages']} 页，"
                        f"获得 {progress['messages']} 条消息（含复用缓存），"
                        f"已用 {elapsed // 60} 分 {elapsed % 60} 秒。"
                    )
                    if progress.get("chunks"):
                        result.append(
                            f"模型分块 {progress.get('chunk', 0)}/{progress['chunks']}，实际调用 {progress.get('calls', 0)} 次。"
                        )
                if attention_count:
                    result.append(f"需要处理 {attention_count} 个批次，优先显示最早的未解决记录。")
                for run in runs:
                    result.append(
                        f"{run['id'][:12]}：{LABELS[run['status']]} · {period_text(task, run['start'], run['end'])}"
                    )
                    if run["error"]:
                        result.append(run["error"])
                    rows = await self.s.store.call("deliveries", run["id"])
                    for target in dict.fromkeys(r["target"] for r in rows):
                        group_rows = [r for r in rows if r["target"] == target]
                        counts = Counter(r["state"] for r in group_rows)
                        states = "、".join(f"{LABELS[state]} {n} 条" for state, n in counts.items())
                        errors = list(dict.fromkeys(r["error"] for r in group_rows if r["error"]))
                        result.append(
                            f"  群 {target}：{states}" + (f"（{'；'.join(errors)}）" if errors else "")
                        )
                if attention_count > len(attention):
                    result.append("还有其他未解决记录；处理以上批次后，再查看状态即可。")
            return "\n".join(result) or "尚未配置来源群，请先在插件配置中添加任务。"
        refresh = action == "预览" and len(parts) == 3 and parts[2] == "刷新"
        if action in {"预览", "执行", "暂停", "恢复"} and (len(parts) == 2 or refresh):
            task = self.s.settings().find(identifier(parts[1], "来源群"))
            if action in {"暂停", "恢复"}:
                await self.s.store.call("set", "paused:" + task.key, action == "暂停")
                self.s.wake.set()
                return (
                    "已暂停尚未提交的投递。" if action == "暂停" else "已解除命令暂停；仍需在配置中启用任务。"
                )
            if self.s.lock(task.key).locked():
                raise DigestError("这个群正在读取、生成或发送，请稍后再试。")
            if action == "预览":
                digest, start, end = await self.s.preview(task, notify=self.notify, refresh=refresh)
                return (
                    Preview(task, digest, start, end)
                    if digest.items
                    else "本次预览没有提取到值得发布的新信息。"
                )
            if not await self.s.allowed(task.key):
                raise DigestError("请先在配置中启用摘要和这个群，并解除命令暂停。")
            async with self.s.lock(task.key):
                rid = await self.s.ensure_window(task, manual=True)
            self.s.wake.set()
            return f"已检查最近计划批次 {rid[:12]}，待处理内容会进入队列。"
        if action in {"核对", "重试", "跳过"} and len(parts) in (2, 3):
            if not re.fullmatch(r"[a-f0-9]{8,20}", parts[1]) or (len(parts) == 3 and action != "跳过"):
                return HELP
            run = await self.s.store.call("lookup", parts[1])
            task = Task.restore(run["config"])
            if self.s.lock(task.key).locked():
                raise DigestError("这个群的任务仍在运行，请稍后再处理批次。")
            async with self.s.lock(task.key):
                if action == "核对":
                    return await self.s.delivery.reconcile(run)
                if action == "重试":
                    await self.s.store.call("retry", run["id"])
                    self.s.wake.set()
                    return "已恢复可重试部分；待核对的发送不会重发。"
                target = identifier(parts[2], "目标群") if len(parts) == 3 else None
                if target and target not in {
                    r["target"] for r in await self.s.store.call("deliveries", run["id"])
                }:
                    raise DigestError("这个批次没有向指定目标群投递的计划。")
                await self.s.store.call("skip", run["id"], target)
                self.s.journal.record("管理员跳过投递", run=run["id"], target=target)
                return "已跳过指定批次的生成或尚未完成的投递。"
        return HELP

    async def statistics(self, parts):
        from .llm_stats import format_call_detail, format_statistics

        statistics = getattr(self.s.client, "statistics", None)
        if statistics is None:
            raise DigestError("LLM 统计暂不可用，请检查插件数据目录。")
        groups = [task.source_group for task in self.s.settings().tasks]
        if parts[0] == "详情":
            if len(parts) != 2 or not parts[1].isdigit():
                return "用法：/群摘要 详情 调用编号"
            row = await asyncio.to_thread(statistics.detail, int(parts[1]), groups)
            if row is None:
                return "没有找到可见的调用记录。"
            text = format_call_detail([row])
            if row.get("response_text"):
                text += "\n模型原始输出：\n" + row["response_text"][:2400]
            return text
        if len(parts) >= 2:
            groups = [self.s.settings().find(identifier(parts[1], "来源群")).source_group]
        days = 7
        if len(parts) == 3:
            if not parts[2].isdigit() or not 1 <= int(parts[2]) <= 3650:
                raise DigestError("统计天数应为 1～3650。")
            days = int(parts[2])
        rows = await asyncio.to_thread(statistics.query, group_ids=groups, since=time.time() - days * 86400)
        if parts[0] == "导出":
            if not rows:
                return "没有可导出的 LLM 调用记录。"
            directory = Path(statistics.database).parent / "llm_exports"
            return await asyncio.to_thread(statistics.export_csv, rows, directory)
        if parts[0] == "记录":
            return format_call_detail(rows)
        return format_statistics(rows, days=days, enabled=self.s.settings().llm_statistics["enabled"])
