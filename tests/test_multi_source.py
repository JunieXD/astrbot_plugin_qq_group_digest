import json
from dataclasses import replace

import pytest

from qq_group_digest.config import IMAGE_MODE, Deferred, DigestError, Limits, Task, parse_settings
from qq_group_digest.history import HistoryReader, normalize
from qq_group_digest.history_cache import HistoryCache
from qq_group_digest.poster_data import build_activity
from qq_group_digest.transcript import Transcript

from .conftest import NOW, raw_message
from .test_delivery import API
from .test_poster_integration import presentation
from .test_service import make_service

SECOND, TARGET = "888888888", "987654321"


def multi(task, **kwargs):
    return replace(task, additional_source_groups=(SECOND,), **kwargs)


class MultiAPI(API):
    def __init__(self, history):
        super().__init__()
        self.by_group, self.queries = history, []

    async def history_page(self, group, count, cursor=None, **kwargs):
        self.queries.append((group, cursor))
        return self.by_group[group]


def pages(task, *, end=NOW, start=NOW - 100):
    return {
        group: [
            {
                **raw_message(10, end - 80 + index * 40, f"来源{index + 1}的具体信息", group=group),
                "sender": {"card": f"昵称{index + 1}"},
            },
            raw_message(1, start - 1, group=group),
        ]
        for index, group in enumerate(task.sources)
    }


def test_config_is_backward_compatible_and_source_sets_have_stable_keys(task):
    configured = parse_settings(
        {
            "tasks": [
                {
                    "source_group": task.source_group,
                    "additional_source_groups": [SECOND, SECOND, task.source_group],
                    "target_groups": [TARGET, SECOND, task.source_group],
                }
            ]
        }
    ).tasks[0]
    assert configured.sources == (task.source_group, SECOND)
    assert configured.targets == (TARGET, SECOND, task.source_group)
    assert configured.key != task.key
    reordered = replace(configured, source_group=SECOND, additional_source_groups=(task.source_group,))
    assert reordered.key == configured.key
    assert replace(configured, name="聚合日报").key == configured.key
    assert Task.restore(configured.dump()) == configured
    legacy = task.dump()
    legacy.pop("additional_source_groups")
    assert Task.restore(legacy).key == task.key
    settings = parse_settings(
        {"tasks": [{"source_group": task.source_group, "additional_source_groups": [SECOND]}]}
    )
    assert settings.find(SECOND) == settings.find(task.source_group)
    assert settings.tasks[0].targets == (task.source_group, SECOND)


@pytest.mark.parametrize("extra", ["123456789", ["bad"], list(map(str, range(10)))])
def test_bad_additional_sources_are_rejected(task, extra):
    with pytest.raises(DigestError):
        parse_settings({"tasks": [{"source_group": task.source_group, "additional_source_groups": extra}]})


def test_sources_cannot_silently_overlap_two_tasks(task):
    with pytest.raises(DigestError, match="重复"):
        parse_settings(
            {
                "tasks": [
                    {"source_group": task.source_group, "additional_source_groups": [SECOND]},
                    {"source_group": SECOND},
                ]
            }
        )


async def test_merge_keeps_distinct_native_ids_and_unifies_member_statistics(task, journal):
    task = multi(task, attribute_speakers=True)
    api = MultiAPI(pages(task))
    progress = []
    messages, _ = await HistoryReader(Limits(), journal).read(
        api, task, NOW - 100, NOW, progress=lambda p, n: progress.append((p, n))
    )
    assert [m.group_id for m in messages] == list(task.sources)
    assert len({m.key for m in messages}) == 2 and all(m.message_id == "10" for m in messages)
    assert [m.sender_name for m in messages] == ["昵称1", "昵称2"]
    assert progress[-1] == (2, 2)
    activity = build_activity(messages, NOW - 100, NOW, task.timezone)
    assert activity.total_messages == 2 and activity.participants == 1
    assert activity.members[0].count == 2 and activity.members[0].name == "昵称2"
    assert sum(activity.hourly) == 2


def test_reply_lookup_never_crosses_group_even_with_identical_ids(task):
    task = multi(task)
    raw = [
        raw_message(10, NOW - 30, "甲群问题", group=task.source_group),
        raw_message(10, NOW - 20, "乙群另一个问题", group=SECOND),
        raw_message(11, NOW - 10, "甲群回复", group=task.source_group),
    ]
    raw[-1]["message"].insert(0, {"type": "reply", "data": {"id": "10"}})
    messages = [normalize(row, str(row["group_id"])) for row in raw]
    data = Transcript(messages, task, NOW - 100)
    encoded = json.loads(data.serialize(data.records))
    assert encoded["messages"][-1][4] == [1]
    assert [r[5] for r in encoded["messages"]] == [1, 2, 1]
    assert "跨群" in encoded["group_note"]


@pytest.mark.parametrize(
    "limits",
    [
        replace(Limits(), max_pages=1),
        replace(Limits(), max_messages=1),
        replace(Limits(), max_history_chars=15),
    ],
)
async def test_limits_cover_all_sources_together(task, journal, limits):
    task = multi(task)
    with pytest.raises(DigestError):
        await HistoryReader(limits, journal).read(MultiAPI(pages(task)), task, NOW - 100, NOW)


async def test_one_failed_source_prevents_model_generation_and_delivery(store, task, settings, journal):
    task = multi(task)
    service, _ = make_service(store, replace(settings, tasks=(task,)), journal)
    rid = await service.ensure_window(task, manual=True)
    run = await store.call("run", rid)
    data = pages(task, start=run["read_start"], end=run["end"])
    data[SECOND] = []
    api = MultiAPI(data)
    service.router.api = api
    await service.step(task)
    saved = await store.call("run", rid)
    assert SECOND in saved["error"] and saved["snapshot"] is None
    assert service.client.calls == 0 and not api.sent
    assert not await store.call("deliveries", rid)


async def test_reconnect_between_sources_does_not_publish_mixed_connections(task, journal):
    task = multi(task)

    class Reconnected(MultiAPI):
        async def history_page(self, group, *args, **kwargs):
            if group == SECOND:
                self.generation = 2
            return await super().history_page(group, *args, **kwargs)

    with pytest.raises(Deferred, match="连接"):
        await HistoryReader(Limits(), journal).read(Reconnected(pages(task)), task, NOW - 100, NOW)


async def test_cache_reuse_remains_scoped_to_each_group(store, task, journal):
    task = multi(task, attribute_speakers=True)
    api = MultiAPI(pages(task, start=NOW - 300))
    cache = HistoryCache(store, 30, journal, clock=lambda: NOW)
    reader = HistoryReader(Limits(), journal, cache=cache)
    first, _ = await reader.read(api, task, NOW - 300, NOW)
    second, _ = await reader.read(api, task, NOW - 300, NOW)
    assert [m.dump() for m in first] == [m.dump() for m in second]
    assert len([event for event, _ in journal.events if event == "历史缓存命中"]) == 2


async def test_forward_expansion_limit_is_shared_by_all_sources(task, journal):
    task = multi(task, read_forwards=True, forward_limit=1)
    data = pages(task)
    for group, rows in data.items():
        rows[0]["message"].append({"type": "forward", "data": {"id": group + "-forward"}})

    class ForwardAPI(MultiAPI):
        forwards = 0

        async def read(self, action, **params):
            assert action == "get_forward_msg"
            self.forwards += 1
            return {"messages": [{"message": [{"type": "text", "data": {"text": "内层消息"}}]}]}

    api = ForwardAPI(data)
    _, notes = await HistoryReader(Limits(), journal).read(api, task, NOW - 100, NOW)
    assert api.forwards == 1 and notes


async def test_one_generation_and_poster_are_reused_for_three_targets(
    store, task, settings, journal, tmp_path
):
    task = multi(task, name="鼠群", include_source=True, mode=IMAGE_MODE, attribute_speakers=True)
    service, _ = make_service(store, replace(settings, tasks=(task,)), journal)
    rid = await service.ensure_window(task, manual=True)
    run = await store.call("run", rid)
    api = MultiAPI(pages(task, start=run["read_start"], end=run["end"]))
    service.router.api = api
    service.presentation = presentation(tmp_path, journal)
    prompts = []

    class Client:
        async def generate(self, task, adapter, prompt, **kwargs):
            data = json.loads(prompt.split("\n聊天记录：\n")[1].split("\n输入结束。")[0])
            prompts.append(data)
            return json.dumps(
                {"items": [{"title": "两群信息合并", "body": ["群友u1反馈具体安排，u2补充适用条件。"]}]},
                ensure_ascii=False,
            )

    service.client = Client()
    await store.call("bind", rid, api.account, api.pid)
    run = await store.call("run", rid)
    await service.build(run, task, api)
    run = await store.call("run", rid)
    assert len(prompts) == 1 and len(prompts[0]["messages"]) == 2
    assert run["digest"]["group_name"] == "鼠群"
    assert run["digest"]["activity"]["total_messages"] == 2
    assert run["digest"]["activity"]["participants"] == 1
    deliveries = await store.call("deliveries", rid)
    assert len(deliveries) == 3 and len({json.dumps(d["payload"]) for d in deliveries}) == 1
    assert len(service.presentation.renderer.contexts) == 1
    await service.delivery.process(run)
    await service.delivery.process(run)
    assert len(api.sent) == 3 and {p["group_id"] for _, p in api.sent} == set(task.targets)
    assert len({p["message"][0]["data"]["file"] for _, p in api.sent}) == 1
