"""Short-lived coverage snapshots, independent of NapCat's transient page cursors."""

import hashlib
import json
import time

from .models import Message


class HistoryCache:
    def __init__(self, store, minutes, journal, *, clock=time.time):
        self.store, self.minutes, self.journal, self.clock = store, minutes, journal, clock

    @staticmethod
    def scope(adapter, task):
        # Names change the collected data. Prompts/providers/forward rendering do not.
        parts = (1, adapter.pid, adapter.account, task.source_group, task.attribute_speakers)
        return hashlib.sha256(json.dumps(parts).encode()).hexdigest()

    async def load(self, adapter, task, start, end):
        if not self.minutes:
            return None
        data = await self.store.call("history_cache_get", self.scope(adapter, task))
        if not data:
            return None
        now = self.clock()
        if (
            not 0 <= now - data["captured"] < self.minutes * 60
            or data["expires"] <= now
            or data["start"] > start
        ):
            return None
        # Re-read at least the last minute, including any newly received/withdrawn messages.
        cutoff = min(end, data["end"]) - 60
        if cutoff <= start:
            return None
        try:
            messages = [Message(**m) for m in json.loads(data["messages"])]
            messages = [m for m in messages if start <= m.time < cutoff]
        except (ValueError, TypeError, KeyError):
            self.journal.record("历史缓存不可用", group=task.source_group)
            return None
        self.journal.record("历史缓存命中", group=task.source_group, messages=len(messages))
        return messages, cutoff, data["captured"]

    async def save(self, adapter, task, start, end, messages, captured):
        if self.minutes:
            # Reuse never extends the age of the oldest data in the snapshot.
            await self.store.call(
                "history_cache_put",
                self.scope(adapter, task),
                start,
                end,
                captured,
                captured + self.minutes * 60,
                [m.dump() for m in messages],
                self.clock(),
            )

    async def invalidate(self, adapter, task):
        await self.store.call("history_cache_delete", self.scope(adapter, task))
