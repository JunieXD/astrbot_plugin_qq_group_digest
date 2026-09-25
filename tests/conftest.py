from dataclasses import replace

import pytest

from qq_group_digest.config import Pace, Settings, Task
from qq_group_digest.store import Store

NOW = 1790323200  # 2026-09-25 16:00 Asia/Shanghai


class Journal:
    def __init__(self):
        self.events = []

    def record(self, event, **fields):
        self.events.append((event, fields))


@pytest.fixture
def journal():
    return Journal()


@pytest.fixture
async def store(tmp_path):
    result = Store(tmp_path / "state.sqlite3")
    await result.call("open_db")
    yield result
    await result.close()


@pytest.fixture
def task():
    return Task("123456789", name="资讯", target_groups=("987654321",), include_source=False)


@pytest.fixture
def settings(task):
    pace = replace(
        Pace(),
        read_min_seconds=0,
        read_max_seconds=0,
        send_min_seconds=0,
        send_max_seconds=0,
        gap_min_seconds=0,
        gap_max_seconds=0,
        recovery_min_seconds=0,
        recovery_max_seconds=0,
    )
    return Settings(True, (task,), pace=pace)


def raw_message(mid, stamp, text="原文", *, sender="222222222", seq=None, group="123456789"):
    return {
        "message_id": mid,
        "real_seq": seq if seq is not None else int(mid),
        "time": stamp,
        "group_id": group,
        "user_id": sender,
        "message": [{"type": "text", "data": {"text": text}}],
    }
