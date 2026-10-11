"""Advisory ingestion against the (isolated) test database."""

import base64
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from depgraph.advisories.ingest import IngestError, download, full_load
from depgraph.advisories.osv import parse_timestamp
from depgraph.core.config import Settings
from depgraph.db.models import AdvisoryAffected, AdvisoryAlias, AdvisoryRecord, AdvisorySyncRun
from depgraph.db.session import create_sessionmaker
from tests.helpers import make_zip, osv_records

FIXTURE_IDS = sorted(osv_records())
records = osv_records


@pytest.fixture
async def maker(engine) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    m = create_sessionmaker(engine)
    yield m
    async with m.begin() as s:  # test database only (see conftest)
        await s.execute(delete(AdvisoryRecord))
        await s.execute(delete(AdvisorySyncRun))


@pytest.fixture
def cfg() -> Settings:
    return Settings(app_env="test", osv_batch_size=3)  # small batches: exercise batching


async def _run(maker, cfg, archive: Path) -> AdvisorySyncRun:
    run_id = await full_load(maker, httpx.AsyncClient(), cfg, archive=archive)
    async with maker() as s:
        return await s.get(AdvisorySyncRun, run_id)


async def _count(maker, model, *where) -> int:
    async with maker() as s:
        return await s.scalar(select(func.count()).select_from(model).where(*where))


async def test_full_load_ingests_everything(maker, cfg, tmp_path):
    run = await _run(maker, cfg, make_zip(tmp_path / "all.zip", records()))
    assert run.status == "completed" and run.mode == "full"
    assert (run.seen, run.inserted, run.updated, run.unchanged) == (len(FIXTURE_IDS), 7, 0, 0)
    assert run.cursor == max(parse_timestamp(d["modified"]) for d in records().values())

    async with maker() as s:
        lodash = await s.get(AdvisoryRecord, "GHSA-35jh-r3h4-6jhm")
        assert (lodash.kind, lodash.severity, lodash.last_changed_run_id) == (
            "vulnerability", "HIGH", run.id,
        )  # fmt: skip
        assert lodash.raw["summary"] == "Command Injection in lodash"
        aliases = set(
            await s.scalars(
                select(AdvisoryAlias.alias).where(AdvisoryAlias.advisory_id == lodash.id)
            )
        )
        assert "CVE-2021-23337" in aliases
        assert (await s.get(AdvisoryRecord, "MAL-2026-17564")).kind == "malicious"
        assert (await s.get(AdvisoryRecord, "GHSA-236c-vhj4-gfxg")).withdrawn is not None
    # one row per affected *entry*: GHSA-27f5 lists its package four times
    assert (
        await _count(maker, AdvisoryAffected, AdvisoryAffected.advisory_id == "GHSA-27f5-xjrr-q9ff")
        == 4
    )


async def test_rerun_is_a_no_op(maker, cfg, tmp_path):
    archive = make_zip(tmp_path / "all.zip", records())
    await _run(maker, cfg, archive)
    affected_before = await _count(maker, AdvisoryAffected)
    run = await _run(maker, cfg, archive)
    assert (run.inserted, run.updated, run.unchanged) == (0, 0, 7)
    assert await _count(maker, AdvisoryAffected) == affected_before


async def test_newer_record_replaces_row_aliases_and_ranges(maker, cfg, tmp_path):
    docs = records()
    first = await _run(maker, cfg, make_zip(tmp_path / "a.zip", docs))

    lodash = docs["GHSA-35jh-r3h4-6jhm"]
    lodash["modified"] = "2099-01-01T00:00:00Z"
    lodash["summary"] = "updated"
    lodash["aliases"] = ["CVE-2021-23337"]
    lodash["affected"] = [lodash["affected"][0] | {"versions": ["4.17.20"]}]
    second = await _run(maker, cfg, make_zip(tmp_path / "b.zip", docs))
    assert (second.inserted, second.updated, second.unchanged) == (0, 1, 6)

    async with maker() as s:
        row = await s.get(AdvisoryRecord, "GHSA-35jh-r3h4-6jhm")
        assert (row.summary, row.last_changed_run_id) == ("updated", second.id)
        assert set(
            await s.scalars(select(AdvisoryAlias.alias).where(AdvisoryAlias.advisory_id == row.id))
        ) == {"CVE-2021-23337"}
        (affected,) = (
            await s.scalars(select(AdvisoryAffected).where(AdvisoryAffected.advisory_id == row.id))
        ).all()
        assert affected.versions == ["4.17.20"]
        # untouched advisories keep the run that last changed them
        assert (await s.get(AdvisoryRecord, "MAL-2026-17564")).last_changed_run_id == first.id


async def test_older_record_never_rolls_data_back(maker, cfg, tmp_path):
    docs = records()
    await _run(maker, cfg, make_zip(tmp_path / "a.zip", docs))
    docs["GHSA-35jh-r3h4-6jhm"] |= {"modified": "2000-01-01T00:00:00Z", "summary": "stale"}
    run = await _run(maker, cfg, make_zip(tmp_path / "b.zip", docs))
    assert run.updated == 0
    async with maker() as s:
        assert (await s.get(AdvisoryRecord, "GHSA-35jh-r3h4-6jhm")).summary != "stale"


async def test_malicious_records_can_be_excluded(maker, tmp_path):
    cfg = Settings(app_env="test", osv_include_malicious=False)
    run = await _run(maker, cfg, make_zip(tmp_path / "all.zip", records()))
    assert run.inserted == 6
    assert await _count(maker, AdvisoryRecord, AdvisoryRecord.kind == "malicious") == 0


async def test_bad_records_are_skipped_not_fatal(maker, cfg, tmp_path):
    docs: dict = dict(records())
    docs["broken"] = b"{not json"
    docs["no-id"] = {"modified": "2026-01-01T00:00:00Z"}
    run = await _run(maker, cfg, make_zip(tmp_path / "all.zip", docs))
    assert run.status == "completed" and run.inserted == 7


async def test_nul_characters_are_replaced(maker, cfg, tmp_path):
    doc = records()["MAL-2026-17564"] | {"id": "MAL-NUL", "details": "payload\x00here"}
    await _run(maker, cfg, make_zip(tmp_path / "all.zip", {"MAL-NUL": doc}))
    async with maker() as s:
        assert (await s.get(AdvisoryRecord, "MAL-NUL")).raw["details"] == "payload�here"


# ------------------------------------------------------------------ download


def _served(body: bytes, md5: bytes | None = None) -> httpx.AsyncClient:
    digest = base64.b64encode(md5 or hashlib.md5(body).digest()).decode()  # noqa: S324

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=body,
            headers={"etag": '"abc"', "x-goog-hash": f"crc32c=AAAA, md5={digest}"},
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_download_verifies_md5_and_returns_etag(tmp_path):
    body = make_zip(tmp_path / "src.zip", records()).read_bytes()
    dest = tmp_path / "out.zip"
    assert await download(_served(body), "https://osv.test/npm/all.zip", dest) == '"abc"'
    assert dest.read_bytes() == body


async def test_download_rejects_checksum_mismatch(tmp_path):
    with pytest.raises(IngestError, match="checksum"):
        await download(_served(b"data", md5=b"\x00" * 16), "https://osv.test/x", tmp_path / "o")


async def test_failed_load_marks_run_failed(maker, cfg):
    http = _served(b"tampered", md5=b"\x00" * 16)
    with pytest.raises(IngestError):
        await full_load(maker, http, cfg)
    async with maker() as s:
        run = (await s.scalars(select(AdvisorySyncRun).order_by(AdvisorySyncRun.id.desc()))).first()
    assert run.status == "failed" and "checksum" in run.error
