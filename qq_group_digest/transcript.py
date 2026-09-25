"""Compact model input and bounded context-aware partitioning."""

from __future__ import annotations

import json
import re
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
        # Only an oversized window is partitioned. Leave room for reply/adjacent
        # context and check complete serialized prompts, never sums of row counts.
        packing = budget.scaled(0.85)
        if not packing.fits(self.serialize([])):
            packing = budget
        records = []
        for record in self.records:
            records.extend(self._split_record(record, packing))
        groups, start = [], 0
        while start < len(records):
            # Exponential growth followed by binary search avoids a quadratic
            # tokenize-every-growing-prefix pass on a large chat window.
            good, step = start + 1, 1
            while good < len(records):
                end = min(len(records), start + step * 2)
                if not packing.fits(self.serialize(records[start:end])):
                    break
                good, step = end, step * 2
            else:
                end = good
            low, high = good + 1, end - 1
            while low <= high:
                mid = (low + high) // 2
                if packing.fits(self.serialize(records[start:mid])):
                    good, low = mid, mid + 1
                else:
                    high = mid - 1
            groups.append(records[start:good])
            start = good
        return self._with_context(groups, budget, overlap)

    def _split_record(self, record, budget):
        if budget.fits(self.serialize([record])):
            return [record]
        text, result = record[3], []
        if not text:
            raise DigestError("模型输入空间不足，无法容纳消息元数据。")
        while text:
            lo, hi, count = 1, len(text), 0
            while lo <= hi:
                mid = (lo + hi) // 2
                part = [*record[:3], text[:mid], *record[4:]]
                if budget.fits(self.serialize([part])):
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
