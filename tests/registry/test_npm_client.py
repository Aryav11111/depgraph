"""npm registry client: HTTP is scripted with httpx.MockTransport (never the real
network), time is a fake clock (backoff tests take no wall time), Redis is real.
"""

import asyncio
import os
import random
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest
from redis.asyncio import Redis

from depgraph.core.config import Settings
from depgraph.registry.npm import (
    InvalidPackageNameError,
    NpmRegistryClient,
    PackageNotFoundError,
    RegistryError,
    RegistryUnavailableError,
)
from depgraph.registry.ratelimit import TokenBucket

REG = "https://registry.test"


def packument(name: str = "left-pad", latest: str = "1.3.0") -> dict:
    def ver(v: str, **extra: object) -> dict:
        return {
            "name": name,
            "version": v,
            "dist": {"integrity": f"sha512-{v}", "tarball": f"{REG}/{name}/-/{name}-{v}.tgz"},
            **extra,
        }

    return {
        "name": name,
        "modified": "2026-01-01T00:00:00.000Z",
        "dist-tags": {"latest": latest},
        "versions": {
            "1.0.0": ver("1.0.0", deprecated="use String.prototype.padStart()"),
            "1.3.0": ver("1.3.0", deprecated=""),  # "" means un-deprecated
            latest: ver(latest),
        },
    }


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Script:
    """Per-test fake registry: queue responses, record requests."""

    def __init__(self) -> None:
        self.queue: list[httpx.Response | Exception | Callable] = []
        self.requests: list[httpx.Request] = []

    def push(self, *items: httpx.Response | Exception | Callable) -> None:
        self.queue.extend(items)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        item = self.queue.pop(0)
        if callable(item) and not isinstance(item, httpx.Response):
            item = await item(request)
        if isinstance(item, Exception):
            raise item
        return item


def ok(doc: dict | None = None, etag: str | None = '"e1"') -> httpx.Response:
    return httpx.Response(200, json=doc or packument(), headers={"etag": etag} if etag else {})


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    r = Redis.from_url(Settings().redis_url.get_secret_value())
    yield r
    await r.aclose()


@pytest.fixture
async def env(redis):
    """Builds clients sharing one fake clock, one script and a per-test key prefix."""
    prefix = f"test:{uuid.uuid4().hex}"
    clock, script = FakeClock(), Script()
    clients: list[NpmRegistryClient] = []

    def make(redis_client: Redis | None = redis, **kw) -> NpmRegistryClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(script.handler))
        opts = dict(
            registry_url=REG, key_prefix=prefix, clock=clock, sleep=clock.sleep,
            rng=random.Random(0), requests_per_second=1000,
        )  # fmt: skip
        c = NpmRegistryClient(http, redis_client, **(opts | kw))
        clients.append(c)
        return c

    yield make, clock, script
    for c in clients:
        await c.aclose()
    async for key in redis.scan_iter(f"{prefix}:*"):
        await redis.delete(key)


# ------------------------------------------------------------------- parsing


async def test_fetches_and_parses_abbreviated_packument(env):
    make, _, script = env
    script.push(ok())
    p = await make().get_packument("left-pad")

    assert p.name == "left-pad"
    assert p.dist_tags == {"latest": "1.3.0"}
    assert set(p.versions) == {"1.0.0", "1.3.0"}
    assert p.versions["1.0.0"].deprecated == "use String.prototype.padStart()"
    assert p.versions["1.3.0"].deprecated is None
    assert p.versions["1.3.0"].integrity == "sha512-1.3.0"
    assert str(script.requests[0].url) == f"{REG}/left-pad"


async def test_from_settings_sends_abbreviated_accept_and_user_agent():
    client = NpmRegistryClient.from_settings(Settings(), redis=None)
    try:
        assert "application/vnd.npm.install-v1+json" in client._http.headers["accept"]
        assert client._http.headers["user-agent"].startswith("depgraph/")
    finally:
        await client.aclose()


async def test_scoped_names_are_url_encoded(env):
    make, _, script = env
    script.push(ok(packument("@types/node")))
    await make().get_packument("@types/node")
    assert script.requests[0].url.raw_path == b"/@types%2Fnode"


@pytest.mark.parametrize(
    "name", ["", ".hidden", "_private", "a/b", "@scope", "@/x", "@s/a/b", "a b", "a?x", "..", " a"]
)
async def test_invalid_names_rejected_before_any_request(env, name):
    make, _, script = env
    with pytest.raises(InvalidPackageNameError):
        await make().get_packument(name)
    assert script.requests == []


async def test_malformed_registry_json_is_a_registry_error(env):
    make, _, script = env
    script.push(httpx.Response(200, json={"versions": {}}))  # no name
    with pytest.raises(RegistryError, match="malformed"):
        await make().get_packument("left-pad")


# ------------------------------------------------------------------- retries


async def test_retries_5xx_with_jittered_exponential_backoff(env):
    make, clock, script = env
    script.push(httpx.Response(503), httpx.Response(502), ok())
    p = await make(backoff_base_s=1.0).get_packument("left-pad")

    assert p.name == "left-pad"
    assert len(script.requests) == 3
    # full jitter: attempt n waits uniform(0, base * 2^n)
    assert len(clock.sleeps) == 2
    assert 0 <= clock.sleeps[0] <= 1.0
    assert 0 <= clock.sleeps[1] <= 2.0


async def test_backoff_is_capped(env):
    make, clock, script = env
    script.push(*[httpx.Response(500)] * 6, ok())
    await make(max_retries=6, backoff_base_s=1.0, backoff_max_s=3.0).get_packument("left-pad")
    assert max(clock.sleeps) <= 3.0


async def test_429_honours_retry_after_seconds(env):
    make, clock, script = env
    script.push(httpx.Response(429, headers={"retry-after": "7"}), ok())
    await make().get_packument("left-pad")
    assert clock.sleeps == [7.0]


async def test_429_honours_retry_after_http_date(env):
    make, clock, script = env
    when = datetime.fromtimestamp(clock.now + 12, tz=UTC)
    script.push(
        httpx.Response(429, headers={"retry-after": format_datetime(when, usegmt=True)}), ok()
    )
    await make().get_packument("left-pad")
    assert clock.sleeps == [pytest.approx(12.0)]


async def test_retry_after_is_capped_by_backoff_max(env):
    make, clock, script = env
    script.push(httpx.Response(429, headers={"retry-after": "3600"}), ok())
    await make(backoff_max_s=30.0).get_packument("left-pad")
    assert clock.sleeps == [30.0]


async def test_network_errors_and_timeouts_are_retried(env):
    make, _, script = env
    script.push(httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), ok())
    assert (await make().get_packument("left-pad")).name == "left-pad"
    assert len(script.requests) == 3


async def test_gives_up_after_max_retries(env):
    make, clock, script = env
    script.push(*[httpx.Response(503)] * 3)
    with pytest.raises(RegistryUnavailableError, match="after 3 attempts: HTTP 503"):
        await make(max_retries=2).get_packument("left-pad")
    assert len(script.requests) == 3
    assert len(clock.sleeps) == 2  # no pointless sleep after the final attempt


async def test_404_is_not_retried_and_is_negatively_cached(env):
    make, _, script = env
    client = make()
    script.push(httpx.Response(404))
    with pytest.raises(PackageNotFoundError):
        await client.get_packument("no-such-pkg")
    with pytest.raises(PackageNotFoundError):
        await client.get_packument("no-such-pkg")  # served from the negative cache
    assert len(script.requests) == 1


async def test_other_4xx_fail_fast(env):
    make, _, script = env
    script.push(httpx.Response(403))
    with pytest.raises(RegistryError, match="403") as exc:
        await make().get_packument("left-pad")
    assert not isinstance(exc.value, RegistryUnavailableError)
    assert len(script.requests) == 1


# --------------------------------------------------------------------- cache


async def test_fresh_cache_hit_makes_no_request(env):
    make, _, script = env
    script.push(ok())
    first = await make().get_packument("left-pad")
    second = await make().get_packument("left-pad")  # new client, same Redis
    assert first == second
    assert len(script.requests) == 1


async def test_stale_entry_is_revalidated_with_etag_and_304_keeps_data(env):
    make, clock, script = env
    client = make(cache_fresh_s=60)
    script.push(ok(etag='"v1"'))
    original = await client.get_packument("left-pad")

    clock.now += 61
    script.push(httpx.Response(304))
    assert await client.get_packument("left-pad") == original
    assert script.requests[1].headers["if-none-match"] == '"v1"'

    # The 304 refreshed the entry: no request inside the new freshness window.
    clock.now += 30
    await client.get_packument("left-pad")
    assert len(script.requests) == 2


async def test_stale_entry_replaced_when_registry_has_new_data(env):
    make, clock, script = env
    client = make(cache_fresh_s=60)
    script.push(ok(etag='"v1"'))
    await client.get_packument("left-pad")

    clock.now += 61
    script.push(ok(packument(latest="2.0.0"), etag='"v2"'))
    p = await client.get_packument("left-pad")
    assert p.dist_tags["latest"] == "2.0.0"
    assert "2.0.0" in p.versions


async def test_works_without_redis_configured(env):
    make, _, script = env
    script.push(ok(), ok())
    client = make(redis_client=None)
    await client.get_packument("left-pad")
    await client.get_packument("left-pad")
    assert len(script.requests) == 2  # no cache, but no failure either


async def test_redis_outage_degrades_to_direct_fetch(env):
    make, _, script = env
    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    script.push(ok())
    try:
        assert (await make(redis_client=dead).get_packument("left-pad")).name == "left-pad"
    finally:
        await dead.aclose()


# ---------------------------------------------------------------- concurrency


async def test_concurrent_calls_for_one_package_share_one_request(env):
    make, _, script = env
    release = asyncio.Event()

    async def slow(_req: httpx.Request) -> httpx.Response:
        await release.wait()
        return ok()

    script.push(slow)
    client = make()
    waiters = [asyncio.create_task(client.get_packument("left-pad")) for _ in range(10)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*waiters)
    assert len(script.requests) == 1
    assert all(r == results[0] for r in results)


async def test_cancelling_one_waiter_does_not_cancel_the_shared_fetch(env):
    make, _, script = env
    release = asyncio.Event()

    async def slow(_req: httpx.Request) -> httpx.Response:
        await release.wait()
        return ok()

    script.push(slow)
    client = make()
    a = asyncio.create_task(client.get_packument("left-pad"))
    b = asyncio.create_task(client.get_packument("left-pad"))
    await asyncio.sleep(0)
    a.cancel()
    release.set()
    assert (await b).name == "left-pad"


async def test_concurrency_is_capped(env):
    make, _, script = env
    in_flight = peak = 0

    async def tracked(req: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        name = req.url.path.lstrip("/")
        return ok(packument(name))

    names = [f"pkg-{i}" for i in range(8)]
    script.push(*[tracked] * len(names))
    client = make(max_concurrency=2)
    await asyncio.gather(*(client.get_packument(n) for n in names))
    assert peak == 2


# --------------------------------------------------------------- token bucket


async def test_token_bucket_allows_burst_then_paces():
    clock = FakeClock()
    bucket = TokenBucket(rate=10, burst=3, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        await bucket.acquire()
    assert clock.sleeps == []  # burst capacity used without waiting
    await bucket.acquire()
    await bucket.acquire()
    assert clock.sleeps == [pytest.approx(0.1), pytest.approx(0.1)]  # then 1 per 1/rate s


async def test_token_bucket_refills_over_time():
    clock = FakeClock()
    bucket = TokenBucket(rate=2, burst=2, clock=clock, sleep=clock.sleep)
    await bucket.acquire()
    await bucket.acquire()
    clock.now += 1.0  # refills 2 tokens
    await bucket.acquire()
    await bucket.acquire()
    assert clock.sleeps == []


def test_token_bucket_rejects_non_positive_rate():
    with pytest.raises(ValueError):
        TokenBucket(rate=0)


# ------------------------------------------------------------------- live


@pytest.mark.live
@pytest.mark.skipif(not os.getenv("NPM_LIVE_TESTS"), reason="set NPM_LIVE_TESTS=1 to hit npm")
async def test_live_registry_smoke():
    client = NpmRegistryClient.from_settings(Settings(), redis=None)
    try:
        p = await client.get_packument("@types/node")
        assert p.name == "@types/node" and p.dist_tags.get("latest") in p.versions
    finally:
        await client.aclose()


async def test_token_bucket_terminates_despite_float_rounding():
    # Regression: 0.1 s * 10/s refills 0.9999999 tokens, not 1.0. A re-check loop
    # then slept ~1e-17 s forever (a no-op on a large clock value).
    clock = FakeClock()
    bucket = TokenBucket(rate=10, burst=1, clock=clock, sleep=clock.sleep)
    async with asyncio.timeout(1):
        for _ in range(50):
            await bucket.acquire()
    assert len(clock.sleeps) == 49
