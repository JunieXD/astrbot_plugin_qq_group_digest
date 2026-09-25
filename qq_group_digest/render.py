"""One validated digest, four deterministic presentation styles."""

import hashlib
import re

from .config import DigestError
from .schedule import period_text


def plain(text):
    # Never pass model output as a CQ-code string to the transport.
    return {"type": "text", "data": {"text": text}}


def header(task, start, end):
    title = task.content_title or default_title(task)
    return f"{title}\n\n{period_text(task, start, end)}"


def default_title(task):
    return f"✨ {task.label} · 群聊摘要"


def display_text(text):
    # Older saved digests may still contain the previous stock disclaimer.
    # Keep actual qualifiers such as 听说/可能 and text inside quotations intact.
    text = re.sub(r"[（(](?:群友(?:反馈|说法)[，,]\s*)?未经核实[）)]", "", text)
    return re.sub(
        r"(?:以上(?:内容|信息|说法)?(?:均为|均是|都是)?群友(?:反馈|说法|转述)[，,；;]?\s*未经核实[。.]?)\s*$",
        "",
        text,
    ).strip()


def readable_body(text):
    text = display_text(text)
    paragraphs = []
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』", "（": "）", "(": ")"}
    for line in text.splitlines():
        line = re.sub(r"^\s*[•●·*-]\s+", "", line).strip()
        if not line:
            continue
        # Preserve model-supplied short paragraphs. For legacy walls of text,
        # wrap complete sentences without breaking quoted nicknames or URLs.
        if len(line) <= 120:
            paragraphs.append(line)
            continue
        stack, start, group = [], 0, ""
        urls = iter(re.finditer(r"https?://\S+", line))
        url = next(urls, None)
        for i, char in enumerate(line):
            while url and i >= url.end():
                url = next(urls, None)
            if url and url.start() <= i < url.end():
                continue
            if char in pairs:
                stack.append(pairs[char])
            elif stack and char == stack[-1]:
                stack.pop()
            if char not in "。！？" or stack:
                continue
            sentence = line[start : i + 1]
            if group and len(group) + len(sentence) > 120:
                paragraphs.append(group)
                group = ""
            group += sentence
            start = i + 1
        tail = line[start:]
        if group and tail and len(group) + len(tail) > 120:
            paragraphs.append(group)
            group = ""
        if group or tail:
            paragraphs.append(group + tail)
    return "\n\n".join("• " + paragraph for paragraph in paragraphs)


def item_heading(index, item):
    title = re.sub(r"\s+", " ", display_text(item.title)).strip()
    return f"{index + 1:02d} · {title}"


def contents(task, digest):
    # Use the final selected titles, not another model-generated summary:
    # numbering, qualifiers and ordering must agree with the following nodes.
    title = task.content_title or default_title(task)
    entries = "\n".join(item_heading(i, item) for i, item in enumerate(digest.items))
    return f"{title}\n\n目录\n{entries}"


def bodies(digest):
    return [f"{item_heading(i, item)}\n\n{readable_body(item.body)}" for i, item in enumerate(digest.items)]


def full_text(task, digest, start, end):
    pieces = [header(task, start, end), *bodies(digest)]
    return "\n\n".join(pieces)


def split_text(text, limit):
    parts = []
    while len(text) > limit:
        pos = text.rfind("\n", 0, limit + 1)
        if pos < limit // 2:
            pos = limit
        parts.append(text[:pos].rstrip())
        text = text[pos:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


def make_payloads(task, digest, start, end, account, limits, mode=None):
    mode = mode or task.mode
    if not digest.items:
        return []
    title = header(task, start, end)
    complete = full_text(task, digest, start, end)
    if mode == "普通消息·整篇":
        if len(complete) > limits.message_chars:
            raise DigestError("整篇摘要超过单条消息长度，请缩短摘要或选择分条展示。")
        return [{"message": [plain(complete)]}]
    if mode == "普通消息·分条":
        pieces = []
        room = limits.message_chars - len(title) - 32
        for body in bodies(digest):
            pieces.extend(split_text(body, room))
        if len(pieces) > limits.max_parts:
            raise DigestError("摘要分段超过发送上限，请缩短摘要或改用合并转发。")
        return [
            {"message": [plain(f"{title} · {i + 1}/{len(pieces)}\n\n{part}")]}
            for i, part in enumerate(pieces)
        ]
    if mode == "合并转发·整篇":
        texts = [complete]
    else:
        texts = [contents(task, digest), *bodies(digest)]
    if len(texts) > 24 or any(len(t) > 16000 for t in texts):
        raise DigestError("转发内容过长，请减少摘要长度。")
    return [
        {
            "messages": [
                {"type": "node", "data": {"user_id": account, "nickname": "群聊摘要", "content": [plain(t)]}}
                for t in texts
            ],
            "source": task.card_title or default_title(task),
            "news": [{"text": period_text(task, start, end)}],
            "summary": f"共 {len(digest.items)} 条 · 点开查看",
            "prompt": f"[{task.card_title}]" if task.card_title else "[✨ 群聊摘要]",
        }
    ]


def payload_fingerprint(payload):
    if "message" in payload:
        texts = [
            "".join(
                str(s.get("data", {}).get("text", "")) for s in payload["message"] if s.get("type") == "text"
            )
        ]
    else:
        texts = [
            "".join(
                str(s.get("data", {}).get("text", ""))
                for s in n["data"]["content"]
                if s.get("type") == "text"
            )
            for n in payload["messages"]
        ]
    # Preserve node boundaries; an entire digest and split digest are different payloads.
    return hashlib.sha256("\x00".join(t.strip() for t in texts).encode()).hexdigest()
