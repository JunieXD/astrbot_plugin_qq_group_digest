import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from qq_group_digest.config import IMAGE_MODE, DigestError
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
    assert run["read_start"] == run["start"] - 600
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


@pytest.mark.parametrize("overlap", [0, 10])
async def test_preview_does_not_advance_cursor_or_create_deliveries(store, task, settings, journal, overlap):
    task = replace(task, overlap_minutes=overlap)
    service, api = make_service(store, settings, journal)
    start = NOW - 86400
    api.history = [
        raw_message(10, start + 1, "候补到我了"),
        raw_message(5, start - 60, "讨论的是甲校软件学院"),
        raw_message(1, start - 601, "更早的无关信息"),
    ]
    captured = []

    class CaptureClient:
        async def generate(self, task, adapter, prompt, **kwargs):
            data, _ = json.JSONDecoder().raw_decode(prompt.split("聊天记录：\n", 1)[1])
            captured.append(data)
            return output(sources=[data["messages"][-1][0]])

    service.client = CaptureClient()
    digest, actual_start, end = await service.preview(task)
    assert digest.items
    assert (actual_start, end) == (start, NOW)
    rows = captured[0]["messages"]
    assert [row[3] for row in rows] == (["讨论的是甲校软件学院", "候补到我了"] if overlap else ["候补到我了"])
    assert captured[0]["period_start"] == (60 if overlap else 0)
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


async def test_cancelled_cron_delete_does_not_hang_reload(store, settings, journal):
    import asyncio

    service, _ = make_service(store, settings, journal)
    service.cron_ids = ["cancelled-job"]

    async def cancelled(*args):
        raise asyncio.CancelledError

    service.context.cron_manager.delete_job = cancelled
    job = service.loop_task = asyncio.create_task(asyncio.sleep(100))
    await asyncio.wait_for(service.stop(), 1)
    assert job.cancelled()
    await asyncio.wait_for(service.stop(), 1)


async def test_cancelled_stop_waits_for_worker_cleanup(store, settings, journal):
    import asyncio

    service, _ = make_service(store, settings, journal)
    started, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def worker():
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaning.set()
            await finish.wait()

    job = service.loop_task = asyncio.create_task(worker())
    await started.wait()
    stopping = asyncio.create_task(service.stop())
    await cleaning.wait()
    stopping.cancel()
    await asyncio.sleep(0)
    assert not stopping.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stopping, 1)
    assert job.done()


async def test_retry_uses_new_prompt_and_mode_without_moving_window(store, task, settings, journal):
    service, api = make_service(store, settings, journal)
    rid = await service.ensure_window(task, manual=True)
    before = await store.call("run", rid)
    api.history = [raw_message(10, before["end"] - 1), raw_message(1, before["read_start"] - 1)]
    await store.call("fail", rid, "生成失败", 0, 1)
    changed = replace(task, focus="只总结新通知", mode="合并转发·分条")
    service.settings = lambda: replace(settings, tasks=(changed,))
    seen = []

    class CheckClient:
        async def generate(self, task, adapter, prompt, **kwargs):
            seen.append(task.focus)
            return output(prompt=prompt)

    service.client = CheckClient()
    await store.call("retry", rid)
    await service.step(changed)
    after = await store.call("run", rid)
    assert (after["start"], after["end"]) == (before["start"], before["end"])
    assert seen == [changed.focus]
    assert [a for a, _ in api.sent] == ["send_group_forward_msg"]


@pytest.mark.parametrize("option", ["read_forwards", "attribute_speakers"])
async def test_changed_history_options_reread_snapshot_and_refresh_notes(
    store, task, settings, journal, option
):
    from qq_group_digest.history import SKIPPED_FORWARD_NOTE

    service, api = make_service(store, settings, journal)
    rid = await service.ensure_window(task, manual=True)
    run = await store.call("run", rid)
    await store.call("fetched", rid, [], [SKIPPED_FORWARD_NOTE])
    changed = replace(task, **{option: True})
    service.settings = lambda: replace(settings, tasks=(changed,))
    api.history = [raw_message(10, run["end"] - 1), raw_message(1, run["read_start"] - 1)]
    await service.step(changed)
    latest = await store.call("run", rid)
    assert latest["digest"]["items"]
    assert SKIPPED_FORWARD_NOTE not in latest["notes"]


async def test_image_preview_carries_actual_group_name_and_preserves_snapshot_metadata(
    store, task, settings, journal
):
    from qq_group_digest.models import Digest

    task = replace(task, mode=IMAGE_MODE, name="这是任务标签，不是群名")
    service, api = make_service(store, replace(settings, tasks=(task,)), journal)
    api.history = [raw_message(10, NOW - 1), raw_message(1, NOW - 86400 - 601)]
    looked_up = []

    async def group_name(group):
        looked_up.append(group)
        return "四非计算机保研群"

    api.group_name = group_name
    digest, _, _ = await service.preview(task)
    assert digest.group_name == "四非计算机保研群"
    assert looked_up == [task.source_group]
    assert digest.activity.total_messages == 1
    assert Digest.restore(digest.dump()).group_name == digest.group_name


async def test_actual_group_name_lookup_is_only_used_for_image_mode(store, task, settings, journal):
    from qq_group_digest.models import Digest, Item

    service, api = make_service(store, settings, journal)
    called = []

    async def group_name(group):
        called.append(group)
        return "真实群名"

    api.group_name = group_name
    digest = Digest([Item("通知", "正文", ())])
    result = await service.with_metadata(task, digest, [], NOW - 3600, NOW, api)
    assert result is digest
    assert not called


async def test_image_build_passes_actual_group_name_to_presentation_and_saves_it(
    store, task, settings, journal
):
    task = replace(task, mode=IMAGE_MODE, name="可配置的任务标签")
    service, api = make_service(store, replace(settings, tasks=(task,)), journal)
    looked_up, presented = [], []

    async def group_name(group):
        looked_up.append(group)
        return "真实的保研交流群"

    async def payloads(task, digest, *args):
        presented.append(digest)
        return [{"message": [{"type": "image", "data": {"file": "file:///tmp/test-poster.png"}}]}]

    api.group_name = group_name
    service.presentation = SimpleNamespace(payloads=payloads)
    rid = await service.ensure_window(task, manual=True)
    run = await store.call("run", rid)
    api.history = [raw_message(10, run["end"] - 1), raw_message(1, run["read_start"] - 1)]
    await service.build(run, task, api)
    saved = await store.call("run", rid)
    assert saved["status"] == "generated"
    assert saved["digest"]["group_name"] == "真实的保研交流群"
    assert presented[0].group_name == "真实的保研交流群"
    assert presented[0].activity.total_messages == 1
    assert looked_up == [task.source_group]


async def test_image_preview_cancellation_during_metadata_cleans_progress(store, task, settings, journal):
    import asyncio

    task = replace(task, mode=IMAGE_MODE)
    service, api = make_service(store, replace(settings, tasks=(task,)), journal)
    api.history = [raw_message(10, NOW - 1), raw_message(1, NOW - 86400 - 601)]
    started = asyncio.Event()

    async def group_name(group):
        started.set()
        await asyncio.sleep(100)

    api.group_name = group_name
    pending = asyncio.create_task(service.preview(task))
    await started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert task.key not in service.previews
    assert await store.call("latest", task.key) == []
    assert not api.sent
