"""Validate model results with actionable, content-free diagnostics."""

import json
import re

from .attribution import REFERENCE, references
from .config import DigestError
from .history import clean_text
from .models import Item
from .transcript import source_id


class OutputError(DigestError):
    def __init__(self, code, detail, *, items=None):
        super().__init__(f"摘要校验失败（{code}）：{detail}")
        self.code, self.detail, self.items = code, detail, items


def parse_digest(text, known, task, batch_id=None, *, enforce_budget=True):
    if len(text) > 100000:
        raise OutputError("output_size", "模型输出超过 100000 字符。")
    if text.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if match:
            text = match.group(1)
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        raise OutputError("json", "输出不是完整 JSON。") from None
    fields = {"items", "batch_id"} if batch_id is not None else {"items"}
    if not isinstance(data, dict) or set(data) != fields or not isinstance(data.get("items"), list):
        raise OutputError("structure", "顶层字段应为 " + ", ".join(sorted(fields)) + "，items 必须为数组。")
    if batch_id is not None and data.get("batch_id") != batch_id:
        raise OutputError("receipt", "本次输入校验标识不匹配。")
    items = []
    for index, raw in enumerate(data["items"], 1):
        if not isinstance(raw, dict) or set(raw) != {"title", "body", "sources"}:
            raise OutputError("item_structure", f"第 {index} 条应只有 title、body、sources。")
        if not isinstance(raw["title"], str) or not isinstance(raw["body"], str):
            raise OutputError("text_type", f"第 {index} 条标题和正文必须为文字。")
        title, body = clean_text(raw["title"]), clean_text(raw["body"])
        sources = raw["sources"]
        if not title or not body:
            raise OutputError("empty_item", f"第 {index} 条标题或正文为空。")
        if not isinstance(sources, list) or not 1 <= len(sources) <= 100:
            raise OutputError("sources", f"第 {index} 条应有 1～100 个来源编号。")
        normalized = [source_id(s) for s in sources]
        normalized += references(title + "\n" + body)
        if "{{" in REFERENCE.sub("", title + body) or "}}" in REFERENCE.sub("", title + body):
            raise OutputError("attribution", f"第 {index} 条署名格式错误，应为 {{{{u发言者编号}}}}。")
        if any(not isinstance(s, str) or s not in known for s in normalized):
            raise OutputError("unknown_source", f"第 {index} 条引用了本次输入中不存在的消息编号。")
        items.append(Item(title, body, tuple(dict.fromkeys(normalized))))
    if any(len(i.title) > 100 for i in items):
        raise OutputError("title_length", "标题超过 100 字。", items=items)
    if enforce_budget and len(items) > task.max_topics:
        raise OutputError("topic_count", f"实际 {len(items)} 个主题，上限 {task.max_topics}。", items=items)
    actual = sum(len(i.title) + len(i.body) for i in items)
    if enforce_budget and actual > task.summary_chars:
        raise OutputError(
            "length",
            f"标题与正文共 {actual} 字，上限 {task.summary_chars}，超出 {actual - task.summary_chars} 字。",
            items=items,
        )
    return items
