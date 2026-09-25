"""Prefer one complete transcript; partition only when the model budget requires it."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid

from .attribution import resolve_names
from .config import DigestError
from .llm_client import LLMClient as LLMClient
from .llm_client import usable_completion as usable_completion
from .models import Digest
from .prompts import SYSTEM
from .schedule import period_text
from .token_budget import request_budget
from .transcript import Transcript, encode, source_id
from .validation import OutputError, parse_digest


def fingerprint(item):
    title, body = (item.title, item.body) if hasattr(item, "title") else (item["title"], item["body"])
    return title.strip(), body.strip()


class Summarizer:
    def __init__(self, client, limits, *, run_id=None, progress=None):
        self.client, self.limits, self.run_id = client, limits, run_id
        self.progress = progress or (lambda **kwargs: None)

    async def summarize(self, task, adapter, messages, start, end, notes=(), previous=()):
        if not messages or not any(m.time >= start and m.text.strip() for m in messages):
            return Digest([], list(notes))
        transcript = Transcript(messages, task, start)
        known = set(transcript.sources)
        prior = encode([{"title": i["title"], "body": i["body"]} for i in previous])
        if len(prior) > 2000:
            prior = encode([i["title"] for i in previous])
        if len(prior) > 2000:
            prior = "[]"
        per_item = max(8, task.summary_chars // task.max_topics)
        instructions = (
            f"关注内容：{task.focus}\n本期：{period_text(task, start, end)}（{task.timezone}）。\n"
            f"最多 {task.max_topics} 个主题，无最低数量，不凑数；通常每条标题和正文合计约 {max(30, int(per_item * 0.75))} 字。"
            f"全部标题正文目标不超过 {int(task.summary_chars * 0.8)} 字，硬上限 {task.summary_chars} 字。\n"
        )
        if prior != "[]":
            instructions += f"上期内容供去重：{prior}\n仅收录新增信息或进展。\n"
        if task.attribute_speakers:
            instructions += "署名开启：群友说法使用群友u代号，程序填入真实昵称。\n"
        else:
            instructions += "无需引用昵称或成员代号，直接写群友反馈、转发信息等；不要输出署名占位符。\n"
        footer = (
            "\n输入结束。尽量覆盖有价值的独立信息，按价值排序，不凑数。标题用于目录，写清对象与关键增量；"
            "扩大覆盖不降低选材标准：个人履历、互夸调侃和空泛安慰即使有学校或数字也不收录；不要替它们编写启示。正文每条1～2点，压缩转述，为其他独立信息留出篇幅；保留必要限定和链接。"
            f"输出 items JSON，标题正文目标 {int(task.summary_chars * 0.8)} 字。"
            + ("群友署名用u代号。" if task.attribute_speakers else "正文不署名。")
        )
        merge_marker = "候选摘要（sources 和 source_speakers 保留原始归属）："
        if hasattr(self.client, "budget"):
            budget = await self.client.budget(task, adapter)
        else:
            budget, _ = await asyncio.to_thread(
                request_budget, task.provider_id.rsplit("/", 1)[-1], self.limits, SYSTEM
            )
        room = budget.wrapped(instructions + "\n聊天记录：\n", footer)
        merge_room = budget.wrapped(instructions + "\n" + merge_marker + "\n", footer)
        if not await asyncio.to_thread(room.fits, transcript.serialize([])):
            raise DigestError("关注内容过长，模型输入空间不足。")
        chunks = await asyncio.to_thread(transcript.chunks, room, self.limits.llm_overlap_messages)
        calls, job_id = 0, self.run_id or "preview:" + uuid.uuid4().hex
        self.progress(phase="模型生成中", chunks=len(chunks), chunk=0, calls=0)

        def candidate(item):
            data = item.dump()
            # Current results already cite u labels. Only legacy/message citations
            # need a lookup; repeating u1 -> u1 adds no attribution information.
            if task.attribute_speakers:
                aliases = {s: transcript.speakers[s] for s in item.sources if s != transcript.speakers[s]}
                if aliases:
                    data["source_speakers"] = aliases
            return data

        async def extract(content, allowed, phase, index=1, total=1):
            nonlocal calls
            marker = merge_marker if phase == "reduce" else "聊天记录："
            prompt = instructions + "\n" + marker + "\n" + content + footer
            if not await asyncio.to_thread(budget.fits, prompt):
                raise DigestError("摘要输入超过模型预算。")
            key = None
            if hasattr(self.client, "cached"):
                key, cached = await self.client.cached(task, adapter, prompt)
                if cached:
                    try:
                        result = parse_digest(cached, allowed, task, enforce_budget=False)
                        self.progress(phase="复用已完成摘要", chunk=index, chunks=total, calls=calls)
                        if hasattr(self.client, "journal"):
                            self.client.journal.record(
                                "摘要结果缓存命中", group=task.source_group, phase=phase
                            )
                        return result
                    except DigestError:
                        pass
            operation = hashlib.sha256((job_id + prompt).encode()).hexdigest()
            for attempt in range(2):
                if calls >= self.limits.llm_calls_per_run:
                    raise DigestError("本期模型调用次数达到上限。")
                calls += 1
                self.progress(
                    phase="修正摘要中" if attempt else "模型生成中", chunk=index, chunks=total, calls=calls
                )
                text = await self.client.generate(
                    task,
                    adapter,
                    prompt,
                    run_id=self.run_id,
                    job_id=job_id,
                    operation_id=operation,
                    attempt=attempt + 1,
                    phase="repair" if attempt else phase,
                    allowed_sources=allowed,
                )
                try:
                    result = parse_digest(text, allowed, task, enforce_budget=False)
                    # A publication budget is different from JSON/source validity.
                    # Only an indivisibly large item needs semantic rewriting.
                    resolved = resolve_names(result, transcript.sources, task.attribute_speakers)
                    if any(len(item.title) + len(item.body) > task.summary_chars for item in resolved):
                        raise OutputError(
                            "length",
                            "单条信息超过整份摘要字数预算，请拆分独立对象或精炼完整句子。",
                            items=result,
                        )
                except OutputError as exc:
                    if hasattr(self.client, "parsed"):
                        self.client.parsed(text, exc)
                    if attempt:
                        raise
                    # Length/quantity repair only sees already source-validated candidates.
                    # Do not reread all history to shorten a few sentences.
                    correction = (
                        f"\n上次问题：{exc.detail} 请只修复该问题，保留来源、署名、条件与反例。"
                        f"总长度目标 {int(task.summary_chars * 0.75)} 字，不能超过 {task.summary_chars} 字。"
                    )
                    if exc.items is not None:
                        reduced = [candidate(item) for item in exc.items]
                        repaired = instructions + correction + "\n待修正摘要：\n" + encode(reduced) + footer
                    else:
                        repaired = prompt + correction
                    if not await asyncio.to_thread(budget.fits, repaired):
                        raise DigestError("修正摘要所需输入超过模型预算，请提高输入限制。") from exc
                    prompt = repaired
                    continue
                if hasattr(self.client, "parsed"):
                    self.client.parsed(text)
                if key and hasattr(self.client, "cache_result"):
                    await self.client.cache_result(key, encode({"items": [i.dump() for i in result]}))
                return result

        candidates = []
        for index, chunk in enumerate(chunks, 1):
            records = json.loads(chunk)["messages"]
            candidates.extend(
                await extract(
                    chunk,
                    {source_id(r[0]) for r in records} | {r[2] for r in records},
                    "single" if len(chunks) == 1 else "map",
                    index,
                    len(chunks),
                )
            )
        if len(chunks) > 1 and candidates:
            for _ in range(8):
                batches, current = [], []
                for item in candidates:
                    data = candidate(item)
                    if current and not await asyncio.to_thread(merge_room.fits, encode([*current, data])):
                        batches.append(current)
                        current = []
                    if not await asyncio.to_thread(merge_room.fits, encode([data])):
                        raise DigestError("单条候选摘要超过合并输入限制。")
                    current.append(data)
                if current:
                    batches.append(current)
                candidates = []
                for index, batch in enumerate(batches, 1):
                    allowed = {source_id(s) for item in batch for s in item["sources"]}
                    allowed.update(s for item in batch for s in item.get("source_speakers", {}).values())
                    candidates.extend(await extract(encode(batch), allowed, "reduce", index, len(batches)))
                if len(batches) == 1:
                    break
            else:
                raise DigestError("摘要合并未能收敛，请缩短单次统计时间。")
        old = {fingerprint(i) for i in previous}
        seen = set()
        result = []
        for item in resolve_names(candidates, transcript.sources, task.attribute_speakers):
            if not set(item.sources) <= known or (
                item.sources and not any(transcript.sources[s].time >= start for s in item.sources)
            ):
                continue
            key = fingerprint(item)
            if key not in seen and key not in old:
                result.append(item)
                seen.add(key)
        selected, used = [], 0
        for item in result:
            size = len(item.title) + len(item.body)
            if len(selected) >= task.max_topics or used + size > task.summary_chars:
                break
            selected.append(item)
            used += size
        final_notes = list(notes)
        if len(selected) < len(result):
            final_notes.append(
                f"按摘要长度与主题数设置保留前 {len(selected)} 条完整信息，另有 {len(result) - len(selected)} 条未展示；可提高摘要长度或主题上限。"
            )
            if hasattr(self.client, "journal"):
                self.client.journal.record(
                    "摘要预算整理",
                    group=task.source_group,
                    candidates=len(result),
                    selected=len(selected),
                    characters=used,
                )
        if final_notes and hasattr(self.client, "journal"):
            self.client.journal.record("摘要诊断", group=task.source_group, notes=final_notes)
        return Digest(selected, final_notes)
