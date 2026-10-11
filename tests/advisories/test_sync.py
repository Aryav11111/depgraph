"""Incremental sync against a fake OSV bucket (MockTransport) and the test DB."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, select, text

from depgraph.advisories import sync as sync_module
from depgraph.advisories.osv import parse_timestamp
from depgraph.advisories.sync import (
    SYNC_LOCK_KEY,
    advisory_lock,
    ids_modified_after,
    run_worker,
    sync,
)
from depgraph.core.config import Settings
from depgraph.db.models import AdvisoryAlias, AdvisoryRecord, AdvisorySyncRun
from depgraph.db.session import create_sessionmaker
from tests.helpers import make_zip
from tests.helpers import osv_records as records

BASE = "https://osv.test"


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"  # nanosecond-style, like OSV


class FakeBucket:
    """Serves npm/all.zip, npm/modified_id.csv (with ETag) and npm/<id>.json."""

    def __init__(self, tmp_path) -> None:
        self.docs = records()
        self.zip_path = tmp_path / "all.zip"
        self.csv_etag = '"csv-1"'
        self.requests: list[str] = []
        self.missing: set[str] = set()

    def csv(self) -> str:
        rows = sorted(
            ((parse_timestamp(d["modified"]), i) for i, d in self.docs.items()), reverse=True
        )
        return "".join(f"{ts(m)},{i}\n" for m, i in rows)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.requests.append(path)
        if path == "/npm/all.zip":
            make_zip(self.zip_path, self.docs)
            return httpx.Response(200, content=self.zip_path.read_bytes())
        if path == "/npm/modified_id.csv":
            if request.headers.get("if-none-match") == self.csv_etag:
                return httpx.Response(304)
            return httpx.Response(200, text=self.csv(), headers={"etag": self.csv_etag})
        adv_id = path.removeprefix("/npm/").removesuffix(".json")
        if adv_id in self.docs and adv_id not in self.missing:
            return httpx.Response(200, json=self.docs[adv_id])
        return httpx.Response(404)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    def touch(self, adv_id: str, when: datetime, **changes) -> None:
        self.docs[adv_id] = self.docs[adv_id] | {"modified": ts(when)} | changes
        self.csv_etag = f'"csv-{len(self.requests)}"'

    def record_fetches(self) -> list[str]:
        return sorted(
            p for p in self.requests if p.endswith(".json") and not p.endswith("modified_id.csv")
        )


@pytest.fixture
def cfg() -> Settings:
    return Settings(app_env="test", osv_base_url=BASE, osv_batch_size=3, osv_fetch_retries=0)


@pytest.fixture
async def clean(engine):
    yield
    async with create_sessionmaker(engine).begin() as s:
        await s.execute(delete(AdvisoryRecord))
        await s.execute(delete(AdvisorySyncRun))


@pytest.fixture
def bucket(tmp_path) -> FakeBucket:
    return FakeBucket(tmp_path)


async def _runs(engine) -> list[AdvisorySyncRun]:
    async with create_sessionmaker(engine)() as s:
        return list((await s.scalars(select(AdvisorySyncRun).order_by(AdvisorySyncRun.id))).all())


# ------------------------------------------------------------------- pure


def test_csv_reading_stops_at_the_cursor():
    now = datetime(2026, 10, 1, tzinfo=UTC)
    lines = [
        f"{ts(now)},NEW-2\n",
        f"{ts(now - timedelta(minutes=1))},NEW-1\n",
        "garbage line\n",
        "\n",
        f"{ts(now - timedelta(hours=2))},OLD-1\n",
        f"{ts(now - timedelta(hours=3))},OLD-0\n",
    ]
    got = ids_modified_after(lines, since=now - timedelta(hours=1))
    assert [i for _, i in got] == ["NEW-2", "NEW-1"]


def test_csv_boundary_is_exclusive():
    now = datetime(2026, 10, 1, tzinfo=UTC)
    assert ids_modified_after([f"{ts(now)},A\n"], since=parse_timestamp(ts(now))) == []


# ------------------------------------------------------------------- sync


async def test_first_sync_without_cursor_does_a_full_load(engine, clean, bucket, cfg):
    await sync(engine, bucket.client(), cfg)
    (run,) = await _runs(engine)
    assert (run.mode, run.status, run.inserted) == ("full", "completed", 7)
    assert bucket.requests == ["/npm/all.zip"]


async def test_incremental_fetches_only_what_changed(engine, clean, bucket, cfg):
    await sync(engine, bucket.client(), cfg)  # full load: cursor = newest fixture
    bucket.requests.clear()

    future = datetime.now(UTC) + timedelta(days=1)
    bucket.touch("GHSA-35jh-r3h4-6jhm", future, summary="changed upstream",
                 aliases=["CVE-2021-23337"])  # fmt: skip
    run_id = await sync(engine, bucket.client(), cfg)

    # lodash changed; MAL-2026-17564 is the record the cursor came from, so it sits
    # inside the overlap window and is re-checked (and found unchanged). Nothing
    # older is fetched.
    assert bucket.record_fetches() == [
        "/npm/GHSA-35jh-r3h4-6jhm.json",
        "/npm/MAL-2026-17564.json",
    ]
    run = (await _runs(engine))[-1]
    assert run.id == run_id
    assert (run.mode, run.status) == ("incremental", "completed")
    assert (run.inserted, run.updated, run.unchanged) == (0, 1, 1)
    assert run.cursor == parse_timestamp(ts(future))
    async with create_sessionmaker(engine)() as s:
        row = await s.get(AdvisoryRecord, "GHSA-35jh-r3h4-6jhm")
        assert (row.summary, row.last_changed_run_id) == ("changed upstream", run_id)
        aliases = await s.scalars(
            select(AdvisoryAlias.alias).where(AdvisoryAlias.advisory_id == row.id)
        )
        assert set(aliases) == {"CVE-2021-23337"}


async def test_new_advisory_is_inserted(engine, clean, bucket, cfg):
    await sync(engine, bucket.client(), cfg)
    doc = records()["GHSA-35jh-r3h4-6jhm"] | {"id": "GHSA-new0-new0-new0"}
    bucket.docs["GHSA-new0-new0-new0"] = doc
    bucket.touch("GHSA-new0-new0-new0", datetime.now(UTC) + timedelta(days=1))
    await sync(engine, bucket.client(), cfg)
    assert (await _runs(engine))[-1].inserted == 1


async def test_unchanged_export_is_a_304_no_op(engine, clean, bucket, cfg):
    await sync(engine, bucket.client(), cfg)
    bucket.touch("GHSA-35jh-r3h4-6jhm", datetime.now(UTC) + timedelta(days=1))
    await sync(engine, bucket.client(), cfg)  # incremental; stores the CSV etag
    bucket.requests.clear()

    await sync(engine, bucket.client(), cfg)
    assert bucket.requests == ["/npm/modified_id.csv"]  # 304, no record fetches
    run = (await _runs(engine))[-1]
    assert (run.status, run.inserted, run.updated) == ("completed", 0, 0)
    assert run.cursor == (await _runs(engine))[-2].cursor  # cursor carried forward


async def test_overlap_window_rechecks_late_arrivals(engine, clean, bucket, cfg):
    await sync(engine, bucket.client(), cfg)
    cursor = (await _runs(engine))[-1].cursor
    # lodash (stored modified 2026-09) reappears with a modified 5 minutes *before*
    # our cursor: newer than what we hold, but older than the cursor. Without the
    # overlap window an incremental sync would never see it.
    bucket.touch("GHSA-35jh-r3h4-6jhm", cursor - timedelta(minutes=5), summary="late arrival")
    bucket.requests.clear()
    await sync(engine, bucket.client(), cfg)
    assert "/npm/GHSA-35jh-r3h4-6jhm.json" in bucket.record_fetches()
    run = (await _runs(engine))[-1]
    assert run.updated == 1
    assert run.cursor == cursor  # the cursor never moves backwards
    async with create_sessionmaker(engine)() as s:
        assert (await s.get(AdvisoryRecord, "GHSA-35jh-r3h4-6jhm")).summary == "late arrival"


def test_overlap_window_is_needed():
    # Same scenario with no overlap: the late record is invisible to the CSV scan.
    cursor = datetime(2026, 10, 1, tzinfo=UTC)
    late = [f"{ts(cursor - timedelta(minutes=5))},LATE"]
    assert ids_modified_after(late, since=cursor) == []
    assert [i for _, i in ids_modified_after(late, since=cursor - timedelta(hours=1))] == ["LATE"]


async def test_large_backlog_falls_back_to_full_load(engine, clean, bucket, cfg):
    await sync(engine, bucket.client(), cfg)
    future = datetime.now(UTC) + timedelta(days=1)
    for i, adv_id in enumerate(list(bucket.docs)[:3]):
        bucket.touch(adv_id, future + timedelta(seconds=i))
    bucket.requests.clear()
    small = cfg.model_copy(update={"osv_incremental_max": 2})
    await sync(engine, bucket.client(), small)
    assert "/npm/all.zip" in bucket.requests
    assert bucket.record_fetches() == []
    assert [r.mode for r in await _runs(engine)][-2:] == ["incremental", "full"]


async def test_missing_record_is_skipped(engine, clean, bucket, cfg):
    await sync(engine, bucket.client(), cfg)
    future = datetime.now(UTC) + timedelta(days=1)
    bucket.touch("GHSA-35jh-r3h4-6jhm", future)
    bucket.touch("MAL-2026-17564", future + timedelta(seconds=1))
    bucket.missing.add("MAL-2026-17564")
    await sync(engine, bucket.client(), cfg)
    run = (await _runs(engine))[-1]
    assert (run.status, run.updated) == ("completed", 1)


async def test_failed_fetch_marks_run_failed_and_keeps_cursor(engine, clean, bucket, cfg):
    await sync(engine, bucket.client(), cfg)
    good_cursor = (await _runs(engine))[-1].cursor
    bucket.touch("GHSA-35jh-r3h4-6jhm", datetime.now(UTC) + timedelta(days=1))

    def boom(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("modified_id.csv"):
            return bucket.handler(request)
        return httpx.Response(503)

    with pytest.raises(RuntimeError, match="503"):
        await sync(engine, httpx.AsyncClient(transport=httpx.MockTransport(boom)), cfg)
    failed = (await _runs(engine))[-1]
    assert failed.status == "failed" and "503" in failed.error
    # next sync resumes from the last *completed* cursor
    await sync(engine, bucket.client(), cfg)
    latest = (await _runs(engine))[-1]
    assert latest.status == "completed" and latest.updated == 1
    assert latest.cursor > good_cursor


# ---------------------------------------------------------------- locking


async def test_concurrent_sync_is_skipped_while_lock_is_held(engine, clean, bucket, cfg):
    async with advisory_lock(engine) as got:
        assert got
        assert await sync(engine, bucket.client(), cfg) is None
    assert bucket.requests == []
    async with advisory_lock(engine) as got_again:  # released afterwards
        assert got_again


async def test_lock_dies_with_its_connection(engine):
    # A crashed worker must not wedge syncing forever: Postgres releases
    # session-level advisory locks when the holding connection goes away.
    async with engine.connect() as conn:
        assert await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": SYNC_LOCK_KEY})
        async with advisory_lock(engine) as got:
            assert not got  # held by the other connection
        await conn.invalidate()  # simulate the holder's connection dying
    async with advisory_lock(engine) as got:
        assert got


# ----------------------------------------------------------------- worker


async def test_worker_loops_until_stopped_and_survives_errors(engine, cfg, monkeypatch):
    calls = 0

    async def fake_sync(*_a, **_k):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")  # must not kill the worker
        if calls == 3:
            stop.set()

    monkeypatch.setattr(sync_module, "sync", fake_sync)
    stop = asyncio.Event()
    fast = cfg.model_copy(update={"osv_sync_interval_s": 0.01})
    async with asyncio.timeout(5):
        await run_worker(engine, httpx.AsyncClient(), fast, stop)
    assert calls == 3


async def test_worker_stops_promptly_while_sleeping(engine, cfg, monkeypatch):
    async def fake_sync(*_a, **_k):
        return None

    monkeypatch.setattr(sync_module, "sync", fake_sync)
    stop = asyncio.Event()
    task = asyncio.create_task(run_worker(engine, httpx.AsyncClient(), cfg, stop))  # 1h interval
    await asyncio.sleep(0.05)
    stop.set()
    async with asyncio.timeout(2):
        await task
