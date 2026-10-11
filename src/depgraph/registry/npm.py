"""Async npm registry client: rate-limited, retrying, Redis-cached.

What we fetch is the *abbreviated* packument (``Accept:
application/vnd.npm.install-v1+json``), the document npm itself uses to install.
It is typically 10-100x smaller than the full one and contains everything we use:
the version list, dist-tags, per-version integrity and deprecation notices.

Layers, outermost first:

1. single-flight: concurrent calls for one package share one fetch;
2. Redis cache: fresh entries served directly; stale entries revalidated with
   their ETag (a 304 costs no body); 404s cached briefly;
3. retry: 429 / 5xx / network errors retried with exponential backoff and full
   jitter, honouring Retry-After;
4. rate limit: a concurrency cap plus a token bucket on requests per second.

The cache is an optimisation, never a dependency: if Redis is down we log and go
straight to the registry.
"""

import asyncio
import json
import random
import time
import zlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Self
from urllib.parse import quote

import httpx
import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from depgraph import __version__
from depgraph.core.config import Settings
from depgraph.registry.ratelimit import TokenBucket

log = structlog.get_logger(__name__)

ABBREVIATED = "application/vnd.npm.install-v1+json; q=1.0, application/json; q=0.8, */*"
USER_AGENT = f"depgraph/{__version__} (+https://github.com/Aryav11111/depgraph)"
# Bump when the cached payload shape changes; old entries are then simply ignored.
# v2: per-version dependency ranges (for parent-upgrade fix suggestions).
CACHE_FORMAT = "v2"
_NOT_FOUND = b"404"


class RegistryError(Exception):
    """Base for registry failures."""


class InvalidPackageNameError(RegistryError, ValueError):
    pass


class PackageNotFoundError(RegistryError):
    def __init__(self, name: str) -> None:
        super().__init__(f"package not found on registry: {name}")
        self.name = name


class RegistryUnavailableError(RegistryError):
    """Retries exhausted on 429/5xx/network errors."""


@dataclass(frozen=True, slots=True)
class VersionInfo:
    version: str
    integrity: str | None = None
    deprecated: str | None = None
    # name -> declared range across dependencies, optionalDependencies and
    # peerDependencies (a regular dependency wins if a name appears twice).
    dependencies: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Packument:
    name: str
    versions: Mapping[str, VersionInfo]
    dist_tags: Mapping[str, str] = field(default_factory=dict)
    modified: str | None = None

    @classmethod
    def from_registry(cls, doc: Mapping[str, Any]) -> Self:
        versions = {}
        for ver, meta in (doc.get("versions") or {}).items():
            dist = meta.get("dist") or {}
            deprecated = meta.get("deprecated")
            deps: dict[str, str] = {}
            for section in ("peerDependencies", "optionalDependencies", "dependencies"):
                for name, spec in (meta.get(section) or {}).items():
                    if isinstance(name, str) and isinstance(spec, str):
                        deps[name] = spec
            versions[ver] = VersionInfo(
                version=ver,
                integrity=dist.get("integrity"),
                # npm uses deprecated: "" to *un*-deprecate; treat as not deprecated.
                deprecated=deprecated if isinstance(deprecated, str) and deprecated else None,
                dependencies=deps,
            )
        return cls(
            name=doc["name"],
            versions=versions,
            dist_tags=dict(doc.get("dist-tags") or {}),
            modified=doc.get("modified"),
        )

    def to_cache(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "modified": self.modified,
            "dist_tags": dict(self.dist_tags),
            "versions": {
                v: [i.integrity, i.deprecated, dict(i.dependencies)]
                for v, i in self.versions.items()
            },
        }

    @classmethod
    def from_cache(cls, data: Mapping[str, Any]) -> Self:
        return cls(
            name=data["name"],
            modified=data["modified"],
            dist_tags=data["dist_tags"],
            versions={
                v: VersionInfo(v, integrity, deprecated, deps)
                for v, (integrity, deprecated, deps) in data["versions"].items()
            },
        )


def validate_package_name(name: str) -> None:
    """Reject names that can't be npm packages, before they reach a URL.

    Deliberately looser than npm's rules for *new* packages (legacy names may
    contain capitals), but strict about anything that could alter the request path.
    """
    if not name or len(name) > 214 or name != name.strip() or name.startswith((".", "_")):
        raise InvalidPackageNameError(f"invalid npm package name: {name!r}")
    if name.startswith("@"):
        scope, sep, pkg = name[1:].partition("/")
        if not (scope and sep and pkg) or "/" in pkg:
            raise InvalidPackageNameError(f"invalid scoped npm package name: {name!r}")
    elif "/" in name:
        raise InvalidPackageNameError(f"invalid npm package name: {name!r}")
    if any(c.isspace() or c in "\\?#%" for c in name):
        raise InvalidPackageNameError(f"invalid npm package name: {name!r}")


class NpmRegistryClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        redis: Redis | None,
        *,
        registry_url: str = "https://registry.npmjs.org",
        max_concurrency: int = 16,
        requests_per_second: float = 50.0,
        max_retries: int = 4,
        backoff_base_s: float = 0.5,
        backoff_max_s: float = 30.0,
        cache_fresh_s: int = 3600,
        cache_retain_s: int = 7 * 24 * 3600,
        negative_cache_s: int = 300,
        key_prefix: str = "depgraph:npm",
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._http = http
        self._redis = redis
        self._base = registry_url.rstrip("/")
        self._sem = asyncio.Semaphore(max_concurrency)
        # Rate limiting wants a monotonic clock; tests inject one fake clock for both.
        bucket_clock = time.monotonic if clock is time.time else clock
        self._bucket = TokenBucket(requests_per_second, clock=bucket_clock, sleep=sleep)
        self._max_retries = max_retries
        self._backoff_base = backoff_base_s
        self._backoff_max = backoff_max_s
        self._fresh = cache_fresh_s
        self._retain = cache_retain_s
        self._negative = negative_cache_s
        self._prefix = f"{key_prefix}:{CACHE_FORMAT}"
        self._clock = clock
        self._sleep = sleep
        self._rng = rng or random.Random()  # noqa: S311 (jitter, not crypto)
        self._inflight: dict[str, asyncio.Task[Packument]] = {}

    @classmethod
    def from_settings(cls, settings: Settings, redis: Redis | None) -> Self:
        http = httpx.AsyncClient(
            timeout=settings.npm_timeout_s,
            headers={"User-Agent": USER_AGENT, "Accept": ABBREVIATED},
            limits=httpx.Limits(max_connections=settings.npm_max_concurrency),
        )
        return cls(
            http,
            redis,
            registry_url=settings.npm_registry_url,
            max_concurrency=settings.npm_max_concurrency,
            requests_per_second=settings.npm_requests_per_second,
            max_retries=settings.npm_max_retries,
            backoff_base_s=settings.npm_backoff_base_s,
            backoff_max_s=settings.npm_backoff_max_s,
            cache_fresh_s=settings.npm_cache_fresh_s,
            cache_retain_s=settings.npm_cache_retain_s,
            negative_cache_s=settings.npm_negative_cache_s,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ public

    async def get_packument(self, name: str) -> Packument:
        validate_package_name(name)
        task = self._inflight.get(name)
        if task is None:
            task = asyncio.create_task(self._get_cached(name))
            self._inflight[name] = task
            task.add_done_callback(lambda t: self._fetch_done(name, t))
        # shield: one caller being cancelled must not cancel the shared fetch.
        return await asyncio.shield(task)

    def _fetch_done(self, name: str, task: asyncio.Task[Packument]) -> None:
        if self._inflight.get(name) is task:
            del self._inflight[name]
        if not task.cancelled():
            task.exception()  # mark retrieved even if every waiter was cancelled

    # ------------------------------------------------------------------- cache

    async def _get_cached(self, name: str) -> Packument:
        key = f"{self._prefix}:packument:{name}"
        cached = await self._cache_get(key)

        if cached == _NOT_FOUND:
            log.debug("npm_cache_hit", package=name, negative=True)
            raise PackageNotFoundError(name)

        envelope = json.loads(zlib.decompress(cached)) if cached else None
        if envelope and self._clock() - envelope["fetched_at"] < self._fresh:
            log.debug("npm_cache_hit", package=name)
            return Packument.from_cache(envelope["packument"])

        headers = {}
        if envelope and envelope.get("etag"):
            headers["If-None-Match"] = envelope["etag"]

        try:
            resp = await self._request(name, headers)
        except PackageNotFoundError:
            await self._cache_set(key, _NOT_FOUND, self._negative)
            raise

        if resp.status_code == 304 and envelope:
            log.debug("npm_cache_revalidated", package=name)
            envelope["fetched_at"] = self._clock()
        else:
            log.debug("npm_cache_miss", package=name, stale=envelope is not None)
            try:
                packument = Packument.from_registry(resp.json())
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise RegistryError(f"malformed packument for {name}: {exc!r}") from exc
            envelope = {
                "etag": resp.headers.get("etag"),
                "fetched_at": self._clock(),
                "packument": packument.to_cache(),
            }
        await self._cache_set(key, zlib.compress(json.dumps(envelope).encode()), self._retain)
        return Packument.from_cache(envelope["packument"])

    async def _cache_get(self, key: str) -> bytes | None:
        if self._redis is None:
            return None
        try:
            return await self._redis.get(key)
        except RedisError as exc:
            log.warning("npm_cache_unavailable", op="get", error=repr(exc))
            return None

    async def _cache_set(self, key: str, value: bytes, ttl_s: int) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(key, value, ex=ttl_s)
        except RedisError as exc:
            log.warning("npm_cache_unavailable", op="set", error=repr(exc))

    # ------------------------------------------------------------------- HTTP

    async def _request(self, name: str, headers: Mapping[str, str]) -> httpx.Response:
        # "@scope/pkg" -> "@scope%2Fpkg": the registry wants the slash encoded.
        url = f"{self._base}/{quote(name, safe='@')}"
        for attempt in range(self._max_retries + 1):
            retry_after: float | None = None
            async with self._sem:
                await self._bucket.acquire()
                try:
                    resp = await self._http.get(url, headers=headers)
                except httpx.TransportError as exc:  # includes timeouts
                    reason = repr(exc)
                else:
                    if resp.status_code in (200, 304):
                        return resp
                    if resp.status_code == 404:
                        raise PackageNotFoundError(name)
                    if resp.status_code != 429 and resp.status_code < 500:
                        raise RegistryError(f"npm registry returned {resp.status_code} for {name}")
                    reason = f"HTTP {resp.status_code}"
                    retry_after = self._parse_retry_after(resp.headers.get("retry-after"))

            if attempt == self._max_retries:
                raise RegistryUnavailableError(
                    f"npm registry unavailable for {name} after {attempt + 1} attempts: {reason}"
                )
            delay = retry_after if retry_after is not None else self._backoff(attempt)
            log.warning(
                "npm_registry_retry",
                package=name,
                attempt=attempt + 1,
                delay_s=delay,
                reason=reason,
            )
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def _backoff(self, attempt: int) -> float:
        # "Full jitter": uniform in [0, min(cap, base * 2^attempt)]. Spreads retries
        # from many clients so they don't re-collide in synchronized waves.
        return self._rng.uniform(0, min(self._backoff_max, self._backoff_base * 2**attempt))

    def _parse_retry_after(self, value: str | None) -> float | None:
        """Retry-After is either delta-seconds or an HTTP date. Capped at backoff max."""
        if not value:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - self._clock()
            except (TypeError, ValueError):
                return None
        return min(max(seconds, 0.0), self._backoff_max)
