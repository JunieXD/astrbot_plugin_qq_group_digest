"""Compact model input and bounded context-aware partitioning."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import DigestError


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def source_id(value):
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return f"m{value:06d}"
    if isinstance(value, str) and re.fullmatch(r"m[0-9]{1,9}", value):
        return f"m{int(value[1:]):06d}"
    return value


@dataclass(frozen=True)
class InputBudget:
    """UTF-8 bytes conservatively bound byte-token vocabularies; never call them measured tokens."""

    bytes: int
    chars: int = 0

    def fits(self, text):
        return len(text.encode("utf-8")) <= self.bytes and (not self.chars or len(text) <= self.chars)

    def subtract(self, text, reserve=256):
        return InputBudget(
            self.bytes - len(text.encode("utf-8")) - reserve,
            max(1, self.chars - len(text) - reserve) if self.chars else 0,
        )


class Transcript:
    def __init__(self, messages, task, start):
        # Consecutive visible IDs avoid holes from empty/unsupported native message segments.
        messages = [m for m in messages if m.text or m.reply_ids]
        self.indexed = {f"m{i + 1:06d}": m for i, m in enumerate(messages)}
        self.speakers = {}
        people, labels = {}, {}
        self.sources = dict(self.indexed)
        origin = min([start, *(m.time for m in messages)])
        native = {m.message_id: i + 1 for i, m in enumerate(messages)}
        self.records = []
        for i, message in enumerate(messages, 1):
            person = people.setdefault(message.sender, len(people) + 1)
            label = labels.setdefault((person, message.sender_name), len(labels) + 1)
            self.sources[f"u{label}"] = message
            self.speakers[f"u{label}"] = f"u{label}"
            # Names are resolved from cited messages after generation, never guessed by
            # the model from a distant lookup table. Each label preserves the author and historical display name.
            self.speakers[source_id(i)] = f"u{label}"
            text = message.text
            replies = []
            for reply in message.reply_ids:
                text = text.replace(f"[引用消息 {reply}]", "").strip()
                replies.append(native.get(reply, 0))
            if text or replies:
                record = [i, message.time - origin, label, text]
                if replies:
                    record.append(list(dict.fromkeys(replies)))
                self.records.append(record)
        self.header = {
            "time_origin": datetime.fromtimestamp(origin, ZoneInfo(task.timezone)).isoformat(),
            "period_start": start - origin,
            "columns": ["id", "seconds", "speaker", "text", "reply_to(optional;0=unavailable)"],
        }

    def serialize(self, records):
        # Line boundaries aid reference lookup at negligible cost in tokens.
        rows = []
        for record in records:
            # Numeric message/reply IDs save repeated string wrappers; author labels
            # stay explicitly prefixed to keep attribution distinct from message IDs.
            row = [record[0], record[1], f"u{record[2]}", record[3]]
            if len(record) > 4:
                row.append(record[4])
            rows.append(row)
        return encode(self.header)[:-1] + ',"messages":[\n' + ",\n".join(map(encode, rows)) + "\n]}"

    def chunks(self, budget, overlap=20):
        whole = self.serialize(self.records)
        if budget.fits(whole):
            return [whole]
        # Reserve a complete header, so the linear packer never relies on optimistic token estimates.
        header = encode({**self.header, "messages": []})
        packing = InputBudget(int(budget.bytes * 0.85), int(budget.chars * 0.85) if budget.chars else 0)
        room = packing.subtract(header, reserve=16)
        if room.bytes < 1000 or (room.chars and room.chars < 1000):
            # Keep progress possible when a small model leaves little room after its header.
            return self._small_chunks(budget, overlap)
        records = []
        for record in self.records:
            records.extend(self._split_record(record, room))
        groups, current, byte_size, char_size = [], [], 0, 0
        for record in records:
            text = encode(record)
            overhead = 5  # u prefix, quotes, comma and newline in the serialized row.
            b, c = len(text.encode("utf-8")) + overhead, len(text) + overhead
            if current and (byte_size + b > room.bytes or (room.chars and char_size + c > room.chars)):
                groups.append(current)
                current, byte_size, char_size = [], 0, 0
            current.append(record)
            byte_size += b
            char_size += c
        if current:
            groups.append(current)
        return self._with_context(groups, budget, overlap)

    def _small_chunks(self, budget, overlap):
        groups, current = [], []
        packing = InputBudget(int(budget.bytes * 0.85), int(budget.chars * 0.85) if budget.chars else 0)
        for record in self.records:
            # Account for per-record reply metadata even with a small configured budget.
            room = packing.subtract(self.serialize([[*record[:3], "", *record[4:]]]), reserve=32)
            for part in self._split_record(record, room):
                if current and not packing.fits(self.serialize([*current, part])):
                    groups.append(current)
                    current = []
                if not budget.fits(self.serialize([part])):
                    raise DigestError("模型输入空间不足，无法容纳一条消息及其署名。")
                current.append(part)
        if current:
            groups.append(current)
        return self._with_context(groups, budget, overlap)

    @staticmethod
    def _split_record(record, room):
        if room.fits(encode(record)):
            return [record]
        text, result = record[3], []
        if not text:
            raise DigestError("模型输入空间不足，无法容纳消息元数据。")
        while text:
            lo, hi, count = 1, len(text), 0
            while lo <= hi:
                mid = (lo + hi) // 2
                part = [*record[:3], text[:mid], *record[4:]]
                if room.fits(encode(part) + ","):
                    count, lo = mid, mid + 1
                else:
                    hi = mid - 1
            if not count:
                raise DigestError("模型输入空间不足，无法容纳消息元数据。")
            result.append([*record[:3], text[:count], *record[4:]])
            text = text[count:]
        return result

    def _with_context(self, groups, budget, overlap):
        lookup = {r[0]: r for r in self.records}
        result = []
        for i, group in enumerate(groups):
            existing = {r[0] for r in group}
            context = []
            # Explicit reply targets have priority over merely adjacent messages.
            candidates = [
                lookup[target]
                for r in group
                for target in (r[4] if len(r) > 4 else [])
                if target in lookup and target not in existing
            ]
            if i and overlap:
                candidates.extend(groups[i - 1][-overlap:])
            for record in candidates[: max(overlap, 5)]:
                if record[0] in existing:
                    continue
                candidate = self.serialize([*context, record, *group])
                if budget.fits(candidate):
                    context.append(record)
                    existing.add(record[0])
            result.append(self.serialize([*context, *group]))
        return result
