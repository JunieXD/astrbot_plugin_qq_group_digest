"""Validated configuration, independent of AstrBot and network clients."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import asdict, dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class DigestError(Exception):
    """A safe, user-facing error. Never put transport exception text here."""


class Deferred(DigestError):
    def __init__(self, message, seconds=60):
        super().__init__(message)
        self.seconds = max(1, seconds)


class IncompleteHistory(DigestError):
    pass


MODES = ("普通消息·整篇", "普通消息·分条", "合并转发·整篇", "合并转发·分条")
DEFAULT_FOCUS = "提取有实际用途的通知、截止时间、资源链接、问题结论和新的进展。忽略闲聊、广告和重复内容。"


def number(value, label, low, high, *, integral=True):
    try:
        if isinstance(value, bool):
            raise ValueError
        n = float(value)
        if not math.isfinite(n) or not low <= n <= high or (integral and not n.is_integer()):
            raise ValueError
        return int(n) if integral else n
    except (ValueError, TypeError, OverflowError) as exc:
        raise DigestError(f"{label}应为 {low}～{high} 之间的{'整数' if integral else '数字'}。") from exc


def flag(value, label):
    if isinstance(value, bool):
        return value
    raise DigestError(f"{label}应使用开关设置。")


def identifier(value, label, optional=False):
    text = str(value or "").strip()
    if optional and not text:
        return ""
    if not re.fullmatch(r"[1-9][0-9]{4,19}", text):
        raise DigestError(f"{label}应填写完整的数字号码。")
    return text


def obj(value, label):
    if not isinstance(value, dict):
        raise DigestError(f"{label}的配置格式不正确。")
    return value


@dataclass(frozen=True)
class Task:
    source_group: str
    name: str = ""
    enabled: bool = True
    target_groups: tuple[str, ...] = ()
    include_source: bool = True
    times: tuple[str, ...] = ("06:00", "18:00")
    focus: str = DEFAULT_FOCUS
    provider_id: str = ""
    mode: str = MODES[0]
    bot_qq: str = ""
    timezone: str = "Asia/Shanghai"
    overlap_minutes: int = 10
    initial_hours: int = 24
    catchup_hours: int = 24
    read_forwards: bool = False
    forward_limit: int = 5
    fallback_to_plain: bool = False
    max_topics: int = 8
    summary_chars: int = 1200

    @property
    def key(self):
        # A display-name edit must not reset the checkpoint.
        return hashlib.sha256(f"{self.bot_qq}:{self.source_group}".encode()).hexdigest()[:20]

    @property
    def label(self):
        return self.name or f"群 {self.source_group}"

    @property
    def targets(self):
        return tuple(
            dict.fromkeys((*self.target_groups, *((self.source_group,) if self.include_source else ())))
        )

    def dump(self):
        return asdict(self)

    @classmethod
    def restore(cls, data):
        return cls(**{**data, "target_groups": tuple(data["target_groups"]), "times": tuple(data["times"])})


@dataclass(frozen=True)
class Pace:
    read_min_seconds: float = 1.5
    read_max_seconds: float = 3
    send_min_seconds: float = 5
    send_max_seconds: float = 20
    gap_min_seconds: float = 8
    gap_max_seconds: float = 20
    recovery_min_seconds: float = 60
    recovery_max_seconds: float = 180
    failure_cooldown_seconds: float = 600
    failure_threshold: int = 3
    reads_per_hour: int = 360
    sends_per_day: int = 80

    def interval(self, kind):
        return getattr(self, kind + "_min_seconds"), getattr(self, kind + "_max_seconds")

    def guard_config(self):
        return asdict(self)


@dataclass(frozen=True)
class Limits:
    page_size: int = 20
    max_pages: int = 300
    max_messages: int = 5000
    max_history_chars: int = 300000
    llm_input_chars: int = 16000
    llm_calls_per_run: int = 32
    llm_calls_per_day: int = 100
    llm_timeout_seconds: int = 180
    api_timeout_seconds: int = 45
    max_attempts: int = 3
    message_chars: int = 2000
    max_parts: int = 12
    snapshot_days: int = 2
    result_days: int = 30
    log_megabytes: int = 20
    log_backups: int = 7


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    tasks: tuple[Task, ...] = ()
    pace: Pace = field(default_factory=Pace)
    limits: Limits = field(default_factory=Limits)

    def find(self, group):
        found = [t for t in self.tasks if t.source_group == group]
        if len(found) != 1:
            raise DigestError("请先在配置中为这个来源群添加一条任务。")
        return found[0]


def parse_settings(raw):
    raw = obj(raw, "插件")
    tasks = raw.get("tasks", [])
    if not isinstance(tasks, list) or len(tasks) > 30:
        raise DigestError("摘要任务应为列表，最多配置 30 个来源群。")
    parsed, seen = [], set()
    for data in tasks:
        d = obj(data, "摘要任务")
        more = obj(d.get("advanced", {}), "任务高级设置")
        gid = identifier(d.get("source_group"), "来源群")
        if gid in seen:
            raise DigestError(f"来源群 {gid} 重复；请把多个目标群放在同一条任务中。")
        seen.add(gid)
        targets = d.get("target_groups", [])
        if not isinstance(targets, list) or len(targets) > 20:
            raise DigestError("目标群应逐条填写，最多 20 个。")
        times = d.get("times", list(Task.times))
        if not isinstance(times, list) or not 1 <= len(times) <= 12:
            raise DigestError("每天发送时间应填写 1～12 个 HH:MM 时间。")
        if any(not isinstance(t, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in times):
            raise DigestError("时间格式应为 06:00、18:00 这样的 HH:MM。")
        zone = str(more.get("timezone", "Asia/Shanghai"))
        try:
            ZoneInfo(zone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise DigestError("时区无法识别；中国大陆使用 Asia/Shanghai。") from exc
        name = str(d.get("name", "")).strip()
        focus = str(d.get("focus", DEFAULT_FOCUS)).strip()
        if len(name) > 60 or any(ord(c) < 32 for c in name) or not 1 <= len(focus) <= 6000:
            raise DigestError("任务名称最多 60 字且不能换行；关注内容应为 1～6000 字。")
        mode = d.get("mode", MODES[0])
        if mode not in MODES:
            raise DigestError("请选择有效的展示方式。")
        kw = {
            "source_group": gid,
            "name": name,
            "focus": focus,
            "mode": mode,
            "target_groups": tuple(dict.fromkeys(identifier(t, "目标群") for t in targets)),
            "times": tuple(sorted(set(times))),
            "timezone": zone,
            "provider_id": str(d.get("provider_id", "")).strip(),
            "bot_qq": identifier(more.get("bot_qq"), "机器人 QQ", optional=True),
        }
        for key, default in [("enabled", True), ("include_source", True)]:
            kw[key] = flag(d.get(key, default), key)
        for key in ["read_forwards", "fallback_to_plain"]:
            kw[key] = flag(more.get(key, False), key)
        for key, low, high in [
            ("overlap_minutes", 0, 360),
            ("initial_hours", 1, 168),
            ("catchup_hours", 1, 168),
            ("forward_limit", 1, 20),
            ("max_topics", 1, 20),
            ("summary_chars", 300, 12000),
        ]:
            kw[key] = number(more.get(key, getattr(Task, key)), key, low, high)
        task = Task(**kw)
        if not task.targets:
            raise DigestError(f"{task.label} 至少需要一个目标群，或开启同时发到来源群。")
        parsed.append(task)
    pace_raw = obj(raw.get("pace", {}), "执行节奏")
    pace = {}
    for key, default in asdict(Pace()).items():
        if key.endswith("seconds"):
            pace[key] = number(pace_raw.get(key, default), key, 0, 3600, integral=False)
        else:
            pace[key] = number(pace_raw.get(key, default), key, 1, 10000)
    for kind in ["read", "send", "gap", "recovery"]:
        if pace[kind + "_max_seconds"] < pace[kind + "_min_seconds"]:
            raise DigestError(f"{kind} 的最长等待不能小于最短等待。")
    lr = obj(raw.get("limits", {}), "高级限制")
    ranges = {
        "page_size": (5, 100),
        "max_pages": (1, 1000),
        "max_messages": (20, 20000),
        "max_history_chars": (10000, 2000000),
        "llm_input_chars": (4000, 100000),
        "llm_calls_per_run": (1, 100),
        "llm_calls_per_day": (1, 2000),
        "llm_timeout_seconds": (10, 900),
        "api_timeout_seconds": (5, 180),
        "max_attempts": (1, 5),
        "message_chars": (500, 4000),
        "max_parts": (1, 30),
        "snapshot_days": (1, 30),
        "result_days": (7, 365),
        "log_megabytes": (1, 100),
        "log_backups": (1, 30),
    }
    limits = Limits(**{k: number(lr.get(k, v), k, *ranges[k]) for k, v in asdict(Limits()).items()})
    if limits.snapshot_days > limits.result_days:
        raise DigestError("输入快照的保留时间不能超过摘要保留时间。")
    return Settings(flag(raw.get("enabled", False), "启用定时摘要"), tuple(parsed), Pace(**pace), limits)
