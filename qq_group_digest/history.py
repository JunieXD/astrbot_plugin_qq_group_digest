"""History pagination and normalization; no live group-message listener."""

from __future__ import annotations

import hashlib
import json
import re

from .config import IncompleteHistory
from .models import Message

PARTIAL_FORWARD_NOTE = "部分合并转发未完整展开。"
SKIPPED_FORWARD_NOTE = "本期未展开合并转发内容。"
MEDIA_NOTE = "摘要依据可读取的文字；图片、语音和视频正文未识别。"
HISTORY_NOTES = frozenset((PARTIAL_FORWARD_NOTE, SKIPPED_FORWARD_NOTE, MEDIA_NOTE))


def segments(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        # Use the same CQ-code parser as the installed AstrBot transport.
        from aiocqhttp.message import Message as CQMessage

        return list(CQMessage(value))
    raise IncompleteHistory("历史消息的格式无法识别。")


def clean_text(value):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or "")).strip()


def display_name(sender):
    """Keep a bounded display label, never fall back to a QQ number."""
    if not isinstance(sender, dict):
        return ""
    for key in ("card", "nickname"):
        raw = sender.get(key)
        if isinstance(raw, str) and (name := clean_text(raw)):
            name = re.sub(r"\s+", " ", name)
            return re.sub(r"\d{5,}", "[号码]", name)[:80]
    return ""


def flatten(value):
    parts, forwards = [], []
    for seg in segments(value):
        if not isinstance(seg, dict):
            continue
        kind, data = seg.get("type"), seg.get("data", {})
        if not isinstance(data, dict):
            continue
        if kind == "text":
            parts.append(clean_text(data.get("text", "")))
        elif kind == "reply":
            parts.append(f"[引用消息 {data.get('id', '')}]")
        elif kind == "at":
            parts.append("[提及成员]")
        elif kind == "forward":
            parts.append("[合并转发]")
            if data.get("id"):
                forwards.append(str(data["id"]))
        elif kind == "json":
            try:
                raw = data.get("data", "")
                card = json.loads(raw) if isinstance(raw, str) else raw
                meta = card.get("meta", {})
                for detail in meta.values():
                    if not isinstance(detail, dict):
                        continue
                    if card.get("app") == "com.tencent.multimsg":
                        resid = detail.get("resid")
                        if resid:
                            forwards.append(str(resid))
                    for key in ("title", "desc", "summary", "qqdocurl", "jumpUrl", "url"):
                        if isinstance(detail.get(key), str):
                            parts.append(clean_text(detail[key]))
                parts.append("[分享卡片]")
            except (ValueError, TypeError, AttributeError):
                parts.append("[分享卡片无法解析]")
        elif kind in {"image", "record", "video", "file"}:
            label = {"image": "图片未识别", "record": "语音未转写", "video": "视频未解析", "file": "文件"}[
                kind
            ]
            filename = clean_text(data.get("name", "")) if kind == "file" else ""
            parts.append(f"[{label}{'：' + filename if filename else ''}]")
    return " ".join(p for p in parts if p), list(dict.fromkeys(forwards))


def normalize(raw, group, *, include_names=False):
    if not isinstance(raw, dict):
        raise IncompleteHistory("历史中出现无法识别的消息。")
    if str(raw.get("group_id", group)) != str(group):
        raise IncompleteHistory("历史接口返回了其他群的消息，已停止处理。")
    try:
        stamp = int(raw["time"])
        if stamp > 100000000000:
            stamp //= 1000
        if not 946684800 <= stamp < 4102444800:
            raise ValueError
        mid = str(raw["message_id"])
        seq = int(raw.get("real_seq") or 0)
        sender_data = raw.get("sender")
        sender_data = sender_data if isinstance(sender_data, dict) else {}
        sender = str(raw.get("user_id") or sender_data.get("user_id") or "")
        if not mid.lstrip("-").isdigit() or not sender.isdigit():
            raise ValueError
        text, forwards = flatten(raw.get("message", raw.get("raw_message", "")))
    except (TypeError, ValueError, KeyError) as exc:
        raise IncompleteHistory("历史消息缺少有效时间或标识，不能确认本期覆盖范围。") from exc
    key = (
        f"{group}:seq:{seq}"
        if seq
        else hashlib.sha256(f"{group}:{mid}:{stamp}:{sender}:{text}".encode()).hexdigest()
    )
    name = display_name(sender_data) if include_names else ""
    return Message(key, mid, stamp, sender, text, seq, forwards, name)


class HistoryReader:
    def __init__(self, limits, journal):
        self.limits, self.journal = limits, journal

    async def read(self, adapter, task, start, end):
        found, cursor, anchors, previous = {}, None, set(), None
        total_chars = 0
        crossed = False
        generation = None
        for _ in range(self.limits.max_pages):
            page = await adapter.history_page(
                task.source_group, self.limits.page_size, cursor, generation=generation
            )
            generation = adapter.generation
            if not page:
                # NapCat can filter unparseable native messages into an empty array.
                # That does not establish that the requested boundary was reached.
                raise IncompleteHistory("历史接口返回空页，无法确认时间范围已经覆盖；可缩短回溯时间后重试。")
            messages = [normalize(m, task.source_group, include_names=task.attribute_speakers) for m in page]
            ordered = sorted(messages, key=lambda m: (m.time, m.seq, m.message_id))
            oldest = ordered[0]
            for message in ordered:
                if start <= message.time < end and message.sender != adapter.account:
                    old = found.get(message.key)
                    total_chars += len(message.text) - (len(old.text) if old else 0)
                    if total_chars > self.limits.max_history_chars:
                        raise IncompleteHistory("本期文字超过读取上限；请增加发送次数或调整高级限制。")
                    found[message.key] = message
            if len(found) > self.limits.max_messages:
                raise IncompleteHistory("本期消息超过读取上限；请增加发送次数或调整高级限制。")
            if oldest.time < start:
                crossed = True
                break
            # Native pages may include the anchor itself. Reject non-progress instead of looping.
            next_cursor = oldest.message_id
            position = (oldest.time, oldest.seq)
            if next_cursor in anchors or (previous is not None and position > previous):
                raise IncompleteHistory("历史翻页没有继续前进，已停止本期处理。")
            anchors.add(next_cursor)
            cursor, previous = next_cursor, position
        if not crossed:
            raise IncompleteHistory("历史读取达到页数上限，尚未覆盖时间窗口。")
        messages = sorted(found.values(), key=lambda m: (m.time, m.seq, m.key))
        notes, expanded, failed, seen_forwards = [], 0, 0, set()
        if task.read_forwards:
            for message in messages:
                for fid in message.forward_ids:
                    if fid in seen_forwards or expanded >= task.forward_limit:
                        continue
                    seen_forwards.add(fid)
                    expanded += 1
                    before = len(message.text)
                    try:
                        result = await adapter.read("get_forward_msg", message_id=fid)
                        nodes = result.get("messages") if isinstance(result, dict) else None
                        if not isinstance(nodes, list):
                            raise ValueError
                        texts = []
                        for node in nodes[:100]:
                            if not isinstance(node, dict):
                                continue
                            value = node.get("message", node.get("content", []))
                            text, _ = flatten(value)
                            if text:
                                entry = {"text": text}
                                if task.attribute_speakers:
                                    entry["display_name"] = display_name(node.get("sender"))
                                texts.append(json.dumps(entry, ensure_ascii=False))
                        message.text += (
                            "\n[转发内容，发布时间以外层消息为准；以下是独立转发节点，"
                            "不能归为外层转发者本人说法；节点显示名可自定义，身份未核实]\n" + "\n".join(texts)
                        )
                        if len(nodes) > 100:
                            failed += 1
                    except Exception as exc:
                        # Optional enrichment failure never erases the enclosing text.
                        self.journal.record("转发内容未展开", error_type=type(exc).__name__)
                        failed += 1
                    total_chars += len(message.text) - before
                    if total_chars > self.limits.max_history_chars:
                        raise IncompleteHistory("展开后的文字超过本期上限，已停止生成。")
            total = sum(len(m.forward_ids) for m in messages)
            if failed or total > expanded:
                notes.append(PARTIAL_FORWARD_NOTE)
        elif any(m.forward_ids for m in messages):
            notes.append(SKIPPED_FORWARD_NOTE)
        if any(
            any(label in m.text for label in ("图片未识别", "语音未转写", "视频未解析")) for m in messages
        ):
            notes.append(MEDIA_NOTE)
        self.journal.record(
            "历史读取完成",
            group=task.source_group,
            start=start,
            end=end,
            messages=len(messages),
            forwards=expanded,
        )
        return messages, notes
