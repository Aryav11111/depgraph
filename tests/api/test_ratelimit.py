"""Upload rate limiting: client IP extraction, bucketing, and 429s end to end."""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import delete, func, select
from starlette.requests import Request

from depgraph.api.ratelimit import client_bucket, client_ip
from depgraph.core.config import Settings
from depgraph.db.models import RateLimitBucket, Scan
from depgraph.db.session import create_sessionmaker
from tests.conftest import make_client


def _request(*xff: str, peer: str = "10.0.0.1") -> Request:
    headers = [(b"x-forwarded-for", v.encode()) for v in xff]
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


class TestClientIp:
    def test_no_proxy_uses_the_peer_and_ignores_the_header(self):
        assert client_ip(_request("1.2.3.4"), proxy_hops=0) == "10.0.0.1"

    def test_one_hop_takes_the_rightmost_entry(self):
        # Everything left of our proxy's entry is client-controlled.
        assert client_ip(_request("6.6.6.6, 1.2.3.4"), proxy_hops=1) == "1.2.3.4"

    def test_two_hops(self):
        assert client_ip(_request("6.6.6.6, 1.2.3.4, 35.1.1.1"), proxy_hops=2) == "1.2.3.4"

    def test_repeated_headers_are_one_list(self):
        assert client_ip(_request("6.6.6.6", "1.2.3.4"), proxy_hops=1) == "1.2.3.4"

    @pytest.mark.parametrize("xff", [None, "", "not-an-ip", " , "])
    def test_missing_or_garbage_falls_back_to_the_peer(self, xff):
        req = _request(xff) if xff is not None else _request()
        assert client_ip(req, proxy_hops=1) == "10.0.0.1"

    def test_too_few_entries_falls_back_to_the_peer(self):
        assert client_ip(_request("1.2.3.4"), proxy_hops=2) == "10.0.0.1"

    def test_ipv6_is_normalised(self):
        assert client_ip(_request("2001:DB8:0:0::1"), proxy_hops=1) == "2001:db8::1"


class TestClientBucket:
    def test_ipv6_shares_a_bucket_per_64(self):
        assert client_bucket("2001:db8:1:2::1") == client_bucket("2001:db8:1:2:ffff::9")
        assert client_bucket("2001:db8:1:2::1") != client_bucket("2001:db8:1:3::1")

    def test_ipv4_is_per_address_and_not_stored_raw(self):
        assert client_bucket("1.2.3.4") != client_bucket("1.2.3.5")
        assert "1.2.3.4" not in client_bucket("1.2.3.4")


@pytest.fixture
async def clean_buckets(engine) -> AsyncIterator[None]:
    async def wipe() -> None:
        async with create_sessionmaker(engine).begin() as s:
            await s.execute(delete(RateLimitBucket))

    await wipe()
    yield
    await wipe()


def _limited(**kw) -> Settings:
    base = dict(
        app_env="test", log_json=False, client_ip_proxy_hops=1,
        upload_limit_per_ip=0, upload_limit_global=0,
    )  # fmt: skip
    return Settings(**(base | kw))


async def _post(client: httpx.AsyncClient, ip: str) -> httpx.Response:
    # An empty body is refused with 422 *after* the limit is charged, so these
    # tests create no scans.
    return await client.post("/scans", content=b"", headers={"X-Forwarded-For": ip})


async def test_per_ip_limit_returns_429_with_retry_after(clean_buckets, engine):
    async with make_client(_limited(upload_limit_per_ip=2)) as c:
        assert (await _post(c, "1.2.3.4")).status_code == 422
        assert (await _post(c, "1.2.3.4")).status_code == 422
        resp = await _post(c, "1.2.3.4")
        assert resp.status_code == 429
        assert 1 <= int(resp.headers["retry-after"]) <= 600
        assert resp.json()["detail"].startswith("Too many uploads from your address.")

        # A different client is unaffected; a forged left entry doesn't help.
        assert (await _post(c, "5.6.7.8")).status_code == 422
        assert (await _post(c, "9.9.9.9, 1.2.3.4")).status_code == 429


async def test_refused_requests_are_not_counted(clean_buckets, engine):
    async with make_client(_limited(upload_limit_per_ip=1)) as c:
        await _post(c, "1.2.3.4")
        for _ in range(3):
            assert (await _post(c, "1.2.3.4")).status_code == 429
    async with create_sessionmaker(engine)() as s:
        assert await s.scalar(select(func.max(RateLimitBucket.hits))) == 1


async def test_global_limit_applies_across_addresses(clean_buckets):
    async with make_client(_limited(upload_limit_global=2)) as c:
        assert (await _post(c, "1.1.1.1")).status_code == 422
        assert (await _post(c, "2.2.2.2")).status_code == 422
        resp = await _post(c, "3.3.3.3")
        assert resp.status_code == 429
        assert "busy" in resp.json()["detail"]


async def test_limit_is_checked_before_the_body_is_read(clean_buckets, engine):
    """A blocked client can't make the API buffer or parse anything."""
    settings = _limited(upload_limit_per_ip=1, max_lockfile_bytes=10)
    async with make_client(settings) as c:
        await _post(c, "1.2.3.4")
        resp = await c.post("/scans", content=b"x" * 100, headers={"X-Forwarded-For": "1.2.3.4"})
        assert resp.status_code == 429  # not 413


async def test_valid_upload_still_succeeds_under_the_limit(clean_buckets, engine):
    lock = b'{"name":"x","lockfileVersion":3,"packages":{"":{"name":"x"}}}'
    async with make_client(_limited(upload_limit_per_ip=5)) as c:
        resp = await c.post("/scans", content=lock, headers={"X-Forwarded-For": "1.2.3.4"})
        assert resp.status_code == 201, resp.text
    async with create_sessionmaker(engine).begin() as s:
        await s.execute(delete(Scan).where(Scan.id == uuid.UUID(resp.json()["id"])))


async def test_retry_after_is_readable_cross_origin(clean_buckets):
    async with make_client(_limited(upload_limit_per_ip=1)) as c:
        await _post(c, "1.2.3.4")
        resp = await c.post(
            "/scans", content=b"",
            headers={"X-Forwarded-For": "1.2.3.4", "Origin": "http://localhost:3000"},
        )  # fmt: skip
    assert resp.status_code == 429
    assert "retry-after" in resp.headers["access-control-expose-headers"].lower()
