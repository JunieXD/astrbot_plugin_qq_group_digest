"""Fetch forwards while cursor mappings are fresh; retain only bounded normalized text."""

import hashlib
import json
import time


class ForwardReader:
    def __init__(self, adapter, task, limits, journal):
        self.adapter, self.task, self.limits, self.journal = adapter, task, limits, journal
        self.results = {}
        self.attempts = self.failures = self.hits = 0

    async def collect(self, message, *, fresh=False):
        if not self.task.read_forwards:
            return
        from .history import display_name, flatten

        for fid in message.forward_ids:
            if fid in self.results or self.attempts >= self.task.forward_limit:
                continue
            self.attempts += 1
            self.results[fid] = None
            store = getattr(self.adapter, "store", None)
            scope = [
                1,
                getattr(self.adapter, "pid", ""),
                self.adapter.account,
                self.task.source_group,
                fid,
                self.task.attribute_speakers,
            ]
            key = "forward:" + hashlib.sha256(json.dumps(scope).encode()).hexdigest()
            cached = await store.call("result_cache_get", key, time.time()) if store else None
            if cached:
                try:
                    result = json.loads(cached)
                    if (
                        isinstance(result, dict)
                        and isinstance(result.get("text"), str)
                        and isinstance(result.get("partial"), bool)
                    ):
                        self.results[fid] = result
                        self.hits += 1
                        self.failures += result["partial"]
                        continue
                except (ValueError, TypeError):
                    pass
            try:
                # A cached raw message does not prove NapCat still remembers MsgId -> peer.
                # Its explicit-group history API also accepts a native long MsgId directly.
                if not fresh and fid.isdigit() and len(fid) > 12 and hasattr(self.adapter, "refresh_forward"):
                    await self.adapter.refresh_forward(self.task.source_group, fid)
                result = await self.adapter.read("get_forward_msg", message_id=fid)
                nodes = result.get("messages") if isinstance(result, dict) else None
                if not isinstance(nodes, list):
                    raise ValueError
                texts, partial, chars = [], len(nodes) > 100, 0
                for node in nodes[:100]:
                    if not isinstance(node, dict):
                        partial = True
                        continue
                    text, nested = flatten(node.get("message", node.get("content", [])))
                    partial = partial or bool(nested)
                    if text:
                        entry = {"text": text}
                        if self.task.attribute_speakers:
                            entry["display_name"] = display_name(node.get("sender"))
                        text = json.dumps(entry, ensure_ascii=False)
                        chars += len(text)
                        if chars > min(100000, self.limits.max_history_chars):
                            partial = True
                            break
                        texts.append(text)
                result = {"text": "\n".join(texts), "partial": partial}
                self.results[fid] = result
                self.failures += partial
                if store:
                    await store.call(
                        "result_cache_put",
                        key,
                        json.dumps(result, ensure_ascii=False),
                        time.time() + 1800,
                        time.time(),
                    )
            except Exception as exc:
                self.failures += 1
                self.journal.record(
                    "转发内容未展开", group=self.task.source_group, error_type=type(exc).__name__, fresh=fresh
                )

    def apply(self, message):
        seen = set()
        for fid in message.forward_ids:
            result = self.results.get(fid)
            if result and fid not in seen:
                message.text += (
                    "\n[转发内容；以下各节点的署名可自定义，身份未核实；不能归为外层转发者本人说法]\n"
                    + result["text"]
                )
            seen.add(fid)
