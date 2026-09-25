import csv
import json
import sqlite3
import time
from decimal import Decimal
from types import SimpleNamespace as Obj

from qq_group_digest.llm_stats import UsageStore, format_statistics, summarize
from qq_group_digest.llm_usage import normalize_statistics


def begin(store, operation_id="r1", attempt=1, **overrides):
    fields = dict(
        operation_id=operation_id,
        attempt=attempt,
        run_id=123,
        platform_id="onebot",
        group_id="123",
        source="plugin",
        provider_id="p",
        model="configured",
        model_source="configured",
        system_prompt="固定规则",
        prompt="群聊输入",
    )
    fields.update(overrides)
    return store.begin(**fields)


def response(tokens=None, text='{"approve":true,"reason":"OK"}'):
    return Obj(
        completion_text=text,
        id="response-id",
        raw_completion={
            "model": "actual-model",
            "id": "response-id",
            "choices": [{"finish_reason": "stop"}],
            "usage": tokens
            if tokens is not None
            else {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
        },
    )


def store_config(**kwargs):
    return normalize_statistics(
        {
            "prices": [
                {
                    "model": "actual-model",
                    "cached_input_per_million": "1",
                    "uncached_input_per_million": "4",
                    "output_per_million": "8",
                }
            ],
            **kwargs,
        }
    )


def test_persist_each_retry_and_price_snapshot_without_content(tmp_path):
    store = UsageStore(tmp_path / "usage.db", store_config(store_response=False))
    for attempt, status in [(1, "invalid_json"), (2, "success")]:
        call_id = begin(store, attempt=attempt)
        store.finish(call_id, status="returned", duration_ms=125, response=response())
        store.parsed(call_id, status)
    store.config["prices"][0]["output_per_million"] = "999"
    rows = store.query(group_ids=["123"], content=True)
    assert [r["status"] for r in rows] == ["invalid_json", "success"]
    assert all(r["system_prompt_text"] is r["prompt_text"] is r["response_text"] is None for r in rows)
    assert all(r["model"] == "actual-model" and r["model_source"] == "response" for r in rows)
    assert json.loads(rows[0]["price_snapshot"])["output_per_million"] == "8"
    assert Decimal(rows[0]["estimated_cost"]) == Decimal("0.00032")
    store.close()
    reopened = UsageStore(tmp_path / "usage.db", store_config())
    assert len(reopened.query(group_ids=["123"])) == 2
    reopened.close()


def test_crash_and_cancel_remain_unknown_and_do_not_synthesize_latency(tmp_path):
    path = tmp_path / "usage.db"
    store = UsageStore(path, store_config())
    begin(store, operation_id="crash")
    cancelled = begin(store, operation_id="cancelled")
    store.finish(cancelled, status="cancelled", duration_ms=200)
    store.close()
    store = UsageStore(path, store_config())
    rows = store.query(group_ids=["123"])
    assert [r["status"] for r in rows] == ["interrupted", "cancelled"]
    assert rows[0]["duration_ms"] is None
    assert all(r["input_tokens"] is None and r["estimated_cost"] is None for r in rows)
    store.close()


def test_store_content_is_bounded_without_altering_hashes():
    store = UsageStore(":memory:", store_config(store_content=True, content_max_chars=256))
    call_id = begin(store, prompt="x" * 400)
    store.finish(call_id, status="returned", duration_ms=1, response=response(text="y" * 400))
    row = store.query(group_ids=["123"], content=True)[0]
    assert row["prompt_text"] == "x" * 256
    assert row["response_text"] == "y" * 256
    assert row["content_truncated"] == 1
    assert len(row["prompt_hash"]) == 64


def test_disabled_recording_still_allows_historical_queries():
    store = UsageStore(":memory:", store_config())
    begin(store)
    store.config["enabled"] = False
    assert begin(store, operation_id="disabled") is None
    assert len(store.query(group_ids=["123"])) == 1
    assert store.query(group_ids=[]) == []
    assert store.query(group_ids=["456"]) == []


def test_filters_scope_time_model_and_application():
    store = UsageStore(":memory:", store_config())
    begin(store)
    begin(store, operation_id="different-group", group_id="456")
    assert len(store.query(group_ids=["123"], model="configured", run_id=123)) == 1
    assert store.query(group_ids=["123"], model="other") == []
    assert store.query(group_ids=["123"], run_id=456) == []
    assert store.query(group_ids=["123"], since=time.time() + 60) == []


def test_retention_prunes_finished_records_but_preserves_pending(monkeypatch):
    store = UsageStore(":memory:", store_config(retention_days=1))
    finished = begin(store, operation_id="finished")
    store.finish(finished, status="provider_error", duration_ms=1, error_type="TimeoutError")
    begin(store, operation_id="pending")
    later = time.time() + 2 * 86400
    monkeypatch.setattr("qq_group_digest.llm_stats.time.time", lambda: later)
    store._next_prune = 0
    begin(store, operation_id="new")
    assert [r["operation_id"] for r in store.query(group_ids=["123"])] == ["pending", "new"]


def test_database_failure_does_not_raise_or_spam_logs(monkeypatch, caplog):
    store = UsageStore(":memory:", store_config())

    class BrokenDatabase:
        def execute(self, *args):
            raise sqlite3.OperationalError("disk full")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(store, "_db", BrokenDatabase())
    assert begin(store) is None
    assert begin(store) is None
    store.finish(1, status="provider_error", duration_ms=1)
    assert sum("统计写入失败" in r.message for r in caplog.records) == 1


def test_export_escapes_spreadsheet_formulas_and_cleans_only_expired_exports(tmp_path):
    store = UsageStore(":memory:", store_config(store_content=True))
    call_id = begin(store, prompt='=HYPERLINK("test")')
    store.finish(call_id, status="returned", duration_ms=1, response=response(text=" \t@formula"))
    untouched = tmp_path / "keep.csv"
    untouched.write_text("keep")
    path = store.export_csv(store.query(group_ids=["123"], content=True), tmp_path)
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["prompt_text"].startswith("'=HYPERLINK")
    assert rows[0]["response_text"].startswith("' \t@")
    assert untouched.read_text() == "keep"
    assert rows[0]["reasoning_tokens"] == ""


def test_summary_uses_weighted_cache_rate_and_separates_currencies():
    store = UsageStore(":memory:", store_config())
    for i, (total, cached) in enumerate([(100, 100), (900, 0), (500, None)]):
        tokens = {"prompt_tokens": total, "completion_tokens": 20}
        if cached is not None:
            tokens["prompt_tokens_details"] = {"cached_tokens": cached}
        store.config["prices"][0]["currency"] = "CNY" if i == 0 else "USD"
        call_id = begin(store, operation_id=str(i))
        store.finish(call_id, status="returned", duration_ms=(i + 1) * 100, response=response(tokens))
        store.parsed(call_id, "success")
    rows = store.query(group_ids=["123"])
    summary = summarize(rows)
    assert summary["cache_rate"] == 0.1
    assert summary["cache_known"] == 2
    assert summary["totals"]["input_tokens"] == 1500
    assert summary["average_ms"] == 200 and summary["p95_ms"] == 300
    assert summary["priced"] == 2
    assert set(summary["costs"]) == {"CNY", "USD"}
    text = format_statistics(rows, days=7, enabled=True)
    assert "10.0%" in text and "已计价 2/3" in text


def test_report_reader_does_not_block_live_writes(tmp_path):
    import concurrent.futures

    store = UsageStore(tmp_path / "usage.db", store_config())
    begin(store)
    with store._reader() as reader:
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            call_id = pool.submit(begin, store, operation_id="during-report").result(timeout=2)
        assert call_id is not None
        # The report has a consistent snapshot while the writer progresses.
        assert reader.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1
    assert len(store.query(group_ids=["123"])) == 2
    path = store.export_csv(store.query(group_ids=["123"]), tmp_path / "exports")
    with path.open(encoding="utf-8-sig") as f:
        assert len(list(csv.DictReader(f))) == 2
    store.close()


def test_response_only_recording_keeps_inputs_private_and_usage_available():
    store = UsageStore(":memory:", store_config(store_response=True))
    call = begin(store, prompt="private-input")
    store.finish(call, status="returned", duration_ms=2, response=response())
    row = store.query(group_ids=["123"], content=True)[0]
    assert row["system_prompt_text"] is row["prompt_text"] is None
    assert row["response_text"] == '{"approve":true,"reason":"OK"}'
    assert json.loads(row["raw_usage_json"])["prompt_tokens_details"]["cached_tokens"] == 80
    assert "private-input" not in str(row)
    store.close()


def test_usage_recording_can_be_disabled_without_losing_counters():
    store = UsageStore(":memory:", store_config(store_usage=False))
    call = begin(store)
    store.finish(call, status="returned", duration_ms=2, response=response())
    row = store.query(group_ids=["123"], content=True)[0]
    assert row["raw_usage_json"] is None and row["cached_tokens"] == 80
    store.close()


def test_inflight_call_keeps_price_configuration_from_start():
    store = UsageStore(":memory:", store_config())
    call = begin(store)
    store.config["prices"][0]["output_per_million"] = "999"
    store.finish(call, status="returned", duration_ms=2, response=response())
    row = store.query(group_ids=["123"], content=True)[0]
    assert Decimal(row["estimated_cost"]) == Decimal(".00032")
    assert json.loads(row["price_snapshot"])["output_per_million"] == "8"
    store.close()


def test_missing_cache_cost_interval_is_visible_and_not_counted_as_exact():
    store = UsageStore(":memory:", store_config())
    call = begin(store)
    store.finish(
        call,
        status="returned",
        duration_ms=2,
        response=response({"prompt_tokens": 100, "completion_tokens": 20}),
    )
    rows = store.query(group_ids=["123"])
    stats = summarize(rows)
    assert stats["priced"] == 0 and stats["bounded"] == 1
    assert "费用区间" in format_statistics(rows, days=7, enabled=True)
    assert rows[0]["estimated_cost"] is None
    store.close()


def test_v1_database_migrates_without_rewriting_old_calls(tmp_path):
    path = tmp_path / "old.db"
    store = UsageStore(path, store_config())
    call = begin(store)
    store.finish(call, status="returned", duration_ms=2, response=response())
    store.close()
    db = sqlite3.connect(path)
    for name in ("raw_usage_json", "cost_lower", "cost_upper", "pricing_config"):
        db.execute(f"ALTER TABLE llm_calls DROP COLUMN {name}")
    db.execute("PRAGMA user_version=1")
    db.commit()
    db.close()
    migrated = UsageStore(path, store_config())
    row = migrated.query(group_ids=["123"], content=True)[0]
    assert row["estimated_cost"] is not None and row["cost_lower"] is None
    assert summarize([row])["bounded"] == 1
    assert migrated._db.execute("PRAGMA user_version").fetchone()[0] == 2
    migrated.close()
