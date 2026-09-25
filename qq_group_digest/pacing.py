"""Compatible version-3 QQ pacing coordinator.

Derived from JunieXD's QQ auditor / GitHub subscriber coordinator. Reuse the
registered object without replacing its class or disturbing other plugins.
This fallback also makes the digest plugin independently installable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import random
import time
from pathlib import Path

logger = logging.getLogger(__name__)
_PRIORITY_AGING_SECONDS = 60


class ActionDeferred(Exception):
    """No platform write was attempted; leave the durable task pending."""


class ActionUncertain(Exception):
    """A previous write may have succeeded; do not blindly repeat it."""


def normalize_pacing(raw, defaults):
    raw = raw if isinstance(raw, dict) else {}
    result = dict(defaults)
    for key, default in defaults.items():
        try:
            value = float(raw.get(key, default))
            if not math.isfinite(value):
                raise ValueError(key)
            result[key] = max(0, min(86400, value))
        except (TypeError, ValueError, OverflowError):
            pass
    for key in defaults:
        if key.endswith("_min_seconds"):
            end = key.replace("_min_seconds", "_max_seconds")
            result[end] = max(result[key], result[end])
    if "failure_threshold" in result:
        result["failure_threshold"] = max(1, int(result["failure_threshold"]))
    return result


def delay_range(config, name):
    return (config[f"{name}_min_seconds"], config[f"{name}_max_seconds"])


def get_guard(context, data_root):
    owner = getattr(context, "platform_manager", None) or context
    attribute = "_qq_automation_guard_v1"
    guard = getattr(owner, attribute, None)
    if guard is None:
        guard = ActionGuard(Path(data_root) / "qq_automation_guard" / "state.json")
        setattr(owner, attribute, guard)
    elif not callable(getattr(guard, "run", None)) or not hasattr(guard, "deferred_error"):
        raise RuntimeError("QQ shared coordinator is incompatible")
    return guard


class ActionGuard:
    deferred_error = ActionDeferred
    scheduling_version = 3

    def __init__(self, path):
        self.path = Path(path)
        self.accounts = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        self.locks = {}
        self._init_scheduler()

    def _init_scheduler(self):
        self._pending = {}
        self._changed = {}
        self._last_served = {}
        self._sequence = 0

    def _wake_queue(self, account):
        changed = self._changed.get(account)
        self._changed[account] = asyncio.Event()
        if changed is not None:
            changed.set()

    def _next_ticket(self, account):
        now = time.time()
        ready = [ticket for ticket in self._pending[account] if ticket["ready_at"] <= now]
        if not ready:
            return None
        served = self._last_served.setdefault(account, {})

        def order(ticket):
            # Older ready tasks gradually gain priority, so sustained approvals
            # cannot indefinitely starve cards and administrator notices.
            aging = int((now - ticket["ready_at"]) // _PRIORITY_AGING_SECONDS)
            return (max(0, ticket["priority"] - aging), served.get(ticket["group"], 0), ticket["sequence"])

        return min(ready, key=order)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.accounts, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    async def run(
        self,
        *,
        account,
        online,
        action,
        config,
        delay=(0, 0),
        gap=(8, 15),
        key=None,
        dedup_seconds=604800,
        priority=1,
        group=None,
        label="自动操作",
    ):
        enqueued_at = time.time()
        ready_at = enqueued_at + random.uniform(*delay)
        self._sequence += 1
        ticket = dict(
            ready_at=ready_at,
            priority=max(0, int(priority)),
            group=str(group or "其他"),
            sequence=self._sequence,
        )
        self._pending.setdefault(account, []).append(ticket)
        self._wake_queue(account)
        logger.info(
            "QQ 操作入队：账号=%s，任务=%s，群=%s，随机等待=%.1f 秒，最早执行时间=%s",
            account,
            label,
            group or "其他",
            ready_at - enqueued_at,
            time.strftime("%H:%M:%S", time.localtime(ready_at)),
        )
        try:
            return await self._run_ticket(
                ticket=ticket,
                enqueued_at=enqueued_at,
                label=label,
                account=account,
                online=online,
                action=action,
                config=config,
                gap=gap,
                key=key,
                dedup_seconds=dedup_seconds,
            )
        finally:
            self._pending[account].remove(ticket)
            self._wake_queue(account)

    async def _run_ticket(
        self, *, ticket, enqueued_at, label, account, online, action, config, gap, key, dedup_seconds
    ):
        ready_at = ticket["ready_at"]
        lock = self.locks.setdefault(account, asyncio.Lock())
        state = self.accounts.setdefault(account, {})
        records = state.setdefault("operations", {})
        fingerprint = hashlib.sha256(key.encode()).hexdigest() if key else None

        def already_done():
            now = time.time()
            for old_key, record in list(records.items()):
                if record["expires"] <= now:
                    del records[old_key]
            if fingerprint in records:
                if records[fingerprint]["status"] == "success":
                    return True
                raise ActionUncertain("此前操作结果不明，需核对状态后人工处理")
            if now < state.get("paused_until", 0):
                raise self.deferred_error("账号自动操作处于冷却期")
            return False

        async def check_online():
            try:
                connected = await online()
            except Exception:
                connected = False
            if not connected:
                state["was_offline"] = True
                state["paused_until"] = time.time() + 300
                self._save()
                raise self.deferred_error("QQ 离线，自动操作暂停 300 秒")
            if state.pop("was_offline", False):
                # Recovery applies to the account, including tasks already
                # waiting outside the lock. Persist it across plugin reloads.
                state["recovery_until"] = time.time() + random.uniform(*delay_range(config, "recovery"))
                self._save()

        def deadline():
            return max(ready_at, state.get("next_at", 0), state.get("recovery_until", 0))

        last_wait_reason = None

        def waiting(reason):
            nonlocal last_wait_reason
            if reason != last_wait_reason:
                logger.info(
                    "QQ 操作等待：账号=%s，任务=%s，群=%s，原因=%s", account, label, ticket["group"], reason
                )
                last_wait_reason = reason

        if lock.locked():
            waiting("同账号正在执行其他操作")
        async with lock:
            if already_done():
                return None
            await check_online()
            spacing = random.uniform(*gap)
            self._save()

        while True:
            # A card/notification's long delay must not reserve the account or
            # prevent a later, already-ready approval from proceeding.
            now = time.time()
            if deadline() > now:
                waiting(
                    "账号恢复等待"
                    if state.get("recovery_until", 0) == deadline()
                    else "随机延迟"
                    if ready_at == deadline()
                    else "账号操作间隔"
                )
            await asyncio.sleep(max(0, deadline() - now))
            async with lock:
                if already_done():
                    return None
                if time.time() < deadline():
                    continue
                if self._next_ticket(account) is not ticket:
                    waiting("队列调度（审批优先、按群轮转）")
                    changed = self._changed[account]
                else:
                    await check_online()
                    if time.time() < deadline():
                        continue
                    self._sequence += 1
                    self._last_served[account][ticket["group"]] = self._sequence
                    state["next_at"] = time.time() + spacing
                    if fingerprint:
                        records[fingerprint] = {"status": "unknown", "expires": time.time() + dedup_seconds}
                    self._save()  # Persist before a potentially ambiguous platform write.
                    logger.info(
                        "QQ 操作开始：账号=%s，任务=%s，群=%s，累计等待=%.1f 秒",
                        account,
                        label,
                        ticket["group"],
                        time.time() - enqueued_at,
                    )
                    try:
                        result = await action()
                    except self.deferred_error:
                        if fingerprint:
                            records.pop(fingerprint, None)
                        self._save()
                        raise
                    except BaseException:
                        state["failures"] = state.get("failures", 0) + 1
                        if state["failures"] >= config["failure_threshold"]:
                            state["paused_until"] = time.time() + config["failure_cooldown_seconds"]
                        self._save()
                        raise
                    else:
                        state["failures"] = 0
                        if fingerprint:
                            records[fingerprint]["status"] = "success"
                        self._save()
                        return result
                    finally:
                        state["next_at"] = max(state["next_at"], time.time() + spacing)
                        self._save()
            # Another ready task owns this turn. Completion/cancellation or a
            # new arrival wakes the queue without polling or holding the lock.
            await changed.wait()
