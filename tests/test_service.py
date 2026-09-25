from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_group_digest.config import DigestError
from qq_group_digest.schedule import latest_boundary
from qq_group_digest.service import OWNER, Service

from .conftest import NOW, raw_message
from .test_delivery import API, Guard, Router
from .test_summarizer_render import output


class Cron:
    def __init__(self):
        self.jobs = {
            "old": SimpleNamespace(job_id="old", payload={"owner": OWNER}),
            "unrelated": SimpleNamespace(job_id="unrelated", payload={"owner": "other"}),
        }

    async def list_jobs(self, **kwargs):
        return list(self.jobs.values())

    async def delete_job(self, jid):
        self.jobs.pop(jid, None)

    async def add_basic_job(self, **kwargs):
        jid = str(len(self.jobs)) + kwargs["cron_expression"]
        job = self.jobs[jid] = SimpleNamespace(job_id=jid, **kwargs)
        return job


class Client:
    calls = 0

    async def generate(self, *args, **kwargs):
        self.calls += 1
        return output(prompt=args[-1])


def make_service(store, settings, journal, now=NOW):
    api = API()
    service = Service(
        SimpleNamespace(cron_manager=Cron()),
        lambda: settings,
        store,
        Router(api),
        Guard(),
        Client(),
        journal,
        clock=lambda: now,
    )
    return service, api


async def test_initial_wait_then_fixed_boundary_and_overlap(store, task, settings, journal):
    service, _ = make_service(store, settings, journal)
    assert await service.ensure_window(task) is None
    initial = latest_boundary(task, NOW)
    service.clock = lambda: initial + 43200 + 180
    rid = await service.ensure_window(task)
    run = await store.call("run", rid)
    assert run["end"] == initial + 43200
    assert run["start"] == run["end"] - 86400
    assert run["read_start"] == run["start"]
    await store.call("generated", rid, {"items": [], "notes": []}, [])
    service.clock = lambda: initial + 86400 + 180
    rid2 = await service.ensure_window(task)
    second = await store.call("run", rid2)
    assert second["start"] == run["end"]
    assert second["read_start"] == second["start"] - 600


async def test_long_downtime_coalesces_to_one_bounded_run(store, task, settings, journal):
    service, _ = make_service(store, settings, journal)
    await service.ensure_window(task, manual=True)
    service.clock = lambda: NOW + 10 * 86400
    rid = await service.ensure_window(task)
    run = await store.call("run", rid)
    assert run["end"] - run["start"] == 86400
    assert run["notes"]
    assert len(await store.call("latest", task.key)) == 2


async def test_cron_reload_only_removes_owned_jobs(store, task, settings, journal):
    service, _ = make_service(store, settings, journal)
    await service.start()
    cron = service.context.cron_manager
    assert "old" not in cron.jobs
    assert "unrelated" in cron.jobs
    assert {j.cron_expression for k, j in cron.jobs.items() if k != "unrelated"} == {
        "0 6 * * *",
        "0 18 * * *",
    }
    await service.stop()
    assert list(cron.jobs) == ["unrelated"]


async def test_generated_digest_survives_retry_without_recalling_model(store, task, settings, journal):
    service, api = make_service(store, settings, journal)
    rid = await service.ensure_window(task, manual=True)
    run = await store.call("run", rid)
    api.history = [raw_message(10, run["end"] - 1), raw_message(1, run["read_start"] - 1)]
    api.failure_target = task.targets[0]
    await service.step(task)
    assert service.client.calls == 1
    assert (await store.call("run", rid))["status"] == "generated"
    await service.step(task)
    assert service.client.calls == 1
    assert len(api.sent) == 1


async def test_preview_does_not_advance_cursor_or_create_deliveries(store, task, settings, journal):
    service, api = make_service(store, settings, journal)
    api.history = [raw_message(10, NOW - 1), raw_message(1, NOW - 86401)]
    digest, _, _ = await service.preview(task)
    assert digest.items
    assert await store.call("get", "cursor:" + task.key) is None
    assert await store.call("latest", task.key) == []
    assert not api.sent
    with pytest.raises(DigestError, match="60 秒"):
        await service.preview(task)


async def test_read_failure_never_publishes_partial_run(store, task, settings, journal):
    service, api = make_service(store, settings, journal)
    rid = await service.ensure_window(task, manual=True)
    run = await store.call("run", rid)
    api.history = [raw_message(10, run["end"] - 1)]  # repeated page never reaches boundary
    await service.step(task)
    assert not api.sent
    assert service.client.calls == 0
    assert (await store.call("run", rid))["error"]


async def test_disabled_plugin_can_preview_but_cannot_send(store, task, settings, journal):
    service, _ = make_service(store, replace(settings, enabled=False), journal)
    assert not await service.allowed(task.key)


async def test_offline_backlog_is_coalesced_without_sending_old_editions(store, task, settings, journal):
    service, api = make_service(store, settings, journal)
    first = await service.ensure_window(task, manual=True)
    service.clock = lambda: NOW + 43200
    second = await service.ensure_window(task)
    service.clock = lambda: NOW + 86400
    third = await service.ensure_window(task)
    assert first != second != third
    assert (await store.call("run", first))["status"] == "superseded"
    assert (await store.call("run", second))["status"] == "superseded"
    active = await store.call("active", task.key, NOW + 86400)
    assert [r["id"] for r in active] == [third]
    assert active[0]["end"] - active[0]["start"] <= 86400
    assert not api.sent


async def test_failed_cron_cleanup_still_stops_workers(store, settings, journal):
    import asyncio

    service, _ = make_service(store, settings, journal)
    service.cron_ids = ["bad-job"]

    async def broken(*args):
        raise RuntimeError("scheduler unavailable")

    service.context.cron_manager.delete_job = broken
    service.loop_task = asyncio.create_task(asyncio.sleep(100))
    job = service.loop_task
    await service.stop()
    assert job.cancelled()
