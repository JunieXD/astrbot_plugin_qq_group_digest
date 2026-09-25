"""Private AstrBot-administrator operations, with no implicit group publication."""

import re

from .config import DigestError, Task, identifier
from .render import full_text
from .schedule import next_boundary, period_text

HELP = """群聊摘要（仅 AstrBot 管理员私聊使用）：
/群摘要 状态 [来源群号]
/群摘要 预览 来源群号
/群摘要 执行 来源群号
/群摘要 暂停 来源群号
/群摘要 恢复 来源群号
/群摘要 重试 批次编号
/群摘要 核对 批次编号
/群摘要 跳过 批次编号 [目标群号]
预览会调用模型，仅在私聊返回，不推进定时进度。
执行会处理最近一个计划时点，按配置投递；已处理的批次不会重复生成。
核对只查历史；跳过会放弃指定批次尚未完成的投递，不会重新发送。"""

LABELS = {
    "superseded": "已并入后续补报",
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
    def __init__(self, service):
        self.s = service

    async def run(self, text):
        parts = text.strip().lstrip("/").split()
        if parts and parts[0] in {"群摘要", "qgdigest"}:
            parts.pop(0)
        if not parts or parts == ["帮助"]:
            return HELP
        action = parts[0]
        if len(text) > 200 or len(parts) > 3:
            return HELP
        if action == "状态" and len(parts) in (1, 2):
            tasks = (
                [self.s.settings().find(identifier(parts[1], "来源群"))]
                if len(parts) == 2
                else self.s.settings().tasks
            )
            result = []
            for task in tasks:
                runs = await self.s.store.call("latest", task.key, 3)
                active = await self.s.allowed(task.key)
                due = next_boundary(task, self.s.clock())
                result.append(
                    f"{task.label}（{task.source_group}）：{'已启用' if active else '已暂停/未启用'}\n"
                    f"下一计划时间：{period_text(task, due, due).split('—')[0]}（{task.timezone}）"
                )
                for run in runs:
                    result.append(
                        f"{run['id'][:12]}：{LABELS[run['status']]} · {period_text(task, run['start'], run['end'])}"
                    )
                    if run["error"]:
                        result.append(run["error"])
                    for row in await self.s.store.call("deliveries", run["id"]):
                        result.append(
                            f"  群 {row['target']} 第 {row['part'] + 1} 条：{LABELS[row['state']]}"
                            + (f"（{row['error']}）" if row["error"] else "")
                        )
            return "\n".join(result) or "尚未配置来源群，请先在插件配置中添加任务。"
        if action in {"预览", "执行", "暂停", "恢复"} and len(parts) == 2:
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
                digest, start, end = await self.s.preview(task)
                return (
                    ("预览结果（未向目标群发送）：\n" + full_text(task, digest, start, end))
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
                if run["status"] in {"queued", "fetched", "failed"}:
                    raise DigestError("该批次还未生成摘要，没有可跳过的投递；可先暂停来源群。")
                await self.s.store.call("skip", run["id"], target)
                self.s.journal.record("管理员跳过投递", run=run["id"], target=target)
                return "已跳过指定批次尚未完成的投递。"
        return HELP
