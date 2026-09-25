"""One validated digest, four deterministic presentation styles."""

import hashlib

from .config import DigestError
from .schedule import period_text


def plain(text):
    # Never pass model output as a CQ-code string to the transport.
    return {"type": "text", "data": {"text": text}}


def header(task, start, end):
    return f"{task.label} · 群聊摘要\n{period_text(task, start, end)}"


def bodies(digest):
    return [f"{i + 1}. {item.title}\n{item.body}" for i, item in enumerate(digest.items)]


def full_text(task, digest, start, end):
    pieces = [header(task, start, end), *bodies(digest)]
    if digest.notes:
        pieces.append("说明：" + " ".join(dict.fromkeys(digest.notes)))
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
        if digest.notes:
            pieces.extend(split_text("说明：" + " ".join(dict.fromkeys(digest.notes)), room))
        if len(pieces) > limits.max_parts:
            raise DigestError("摘要分段超过发送上限，请缩短摘要或改用合并转发。")
        return [
            {"message": [plain(f"{title} · {i + 1}/{len(pieces)}\n\n{part}")]}
            for i, part in enumerate(pieces)
        ]
    if mode == "合并转发·整篇":
        texts = [complete]
    else:
        texts = [title, *bodies(digest)]
        if digest.notes:
            texts.append("说明：" + " ".join(dict.fromkeys(digest.notes)))
    if len(texts) > 24 or any(len(t) > 16000 for t in texts):
        raise DigestError("转发内容过长，请减少摘要长度。")
    return [
        {
            "messages": [
                {"type": "node", "data": {"user_id": account, "nickname": "群聊摘要", "content": [plain(t)]}}
                for t in texts
            ],
            "source": f"{task.label} · 群聊摘要",
            "news": [{"text": period_text(task, start, end)}],
            "summary": f"{len(digest.items)} 条信息",
            "prompt": "[群聊摘要]",
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
