"""Upload rate limiting, counted in Postgres.

Why Postgres and not Redis: the production demo runs without Redis, and every
API instance already shares the database, so limits hold across Cloud Run
instances (an in-process counter would multiply by the instance count).

Fixed windows: one upsert per check, atomic under concurrency (``INSERT ... ON
CONFLICT DO UPDATE`` serialises on the row). The known trade-off is a burst of
up to 2x the limit straddling a window boundary; for "stop one client hogging
a free demo" that is fine, and far simpler than a sliding log.

Client identity: the IP, from the X-Forwarded-For entry our own proxy wrote
(`client_ip_proxy_hops`), never an entry the client could have forged. IPv6
clients are bucketed by /64, because one subscriber typically owns a whole /64
and could otherwise rotate addresses for free. Keys store a SHA-256 of the
address rather than the address itself, and rows are pruned within a day.
"""

import hashlib
import ipaddress
import math
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from depgraph.core.config import Settings

_HIT = text("""
    INSERT INTO rate_limit_buckets (key, window_start, hits)
    VALUES (:key, to_timestamp(floor(extract(epoch FROM now()) / :w) * :w), 1)
    ON CONFLICT (key, window_start) DO UPDATE SET hits = rate_limit_buckets.hits + 1
    RETURNING hits, extract(epoch FROM window_start - now()) + :w AS resets_in
""")


@dataclass(frozen=True, slots=True)
class Limit:
    key: str
    limit: int
    window_s: int
    message: str


def client_ip(request: Request, proxy_hops: int) -> str:
    """The caller's IP: the TCP peer, or the X-Forwarded-For entry `proxy_hops`
    from the right. Anything unparseable falls back to the peer."""
    peer = request.client.host if request.client else "unknown"
    if proxy_hops <= 0:
        return peer
    # Several X-Forwarded-For headers are equivalent to one comma-joined list.
    entries = [e.strip() for h in request.headers.getlist("x-forwarded-for") for e in h.split(",")]
    entries = [e for e in entries if e]
    if len(entries) < proxy_hops:
        return peer
    try:
        return str(ipaddress.ip_address(entries[-proxy_hops]))
    except ValueError:
        return peer


def client_bucket(ip: str) -> str:
    """Stable, non-reversible-at-a-glance bucket id for an address (/64 for IPv6)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        network = ip
    else:
        network = str(ipaddress.ip_network(f"{addr}/64", strict=False)) if addr.version == 6 else ip
    return hashlib.sha256(network.encode()).hexdigest()[:32]


def upload_limits(settings: Settings, ip: str) -> list[Limit]:
    limits = []
    if settings.upload_limit_per_ip > 0:
        limits.append(Limit(
            f"upload:ip:{client_bucket(ip)}", settings.upload_limit_per_ip,
            settings.upload_limit_ip_window_s, "Too many uploads from your address.",
        ))  # fmt: skip
    if settings.upload_limit_global > 0:
        limits.append(Limit(
            "upload:global", settings.upload_limit_global,
            settings.upload_limit_global_window_s, "The demo is busy (too many uploads overall).",
        ))  # fmt: skip
    return limits


async def enforce(session: AsyncSession, limits: list[Limit]) -> None:
    """Count one request against each limit, in order; raise 429 at the first
    one exceeded.

    Run it in its own transaction: raising rolls the transaction back, so a
    refused request is charged to no limit, and retrying while blocked doesn't
    extend the block.
    """
    for lim in limits:
        hits, resets_in = (await session.execute(_HIT, {"key": lim.key, "w": lim.window_s})).one()
        if hits > lim.limit:
            retry = max(1, math.ceil(resets_in))
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"{lim.message} Try again in {_human(retry)}.",
                headers={"Retry-After": str(retry)},
            )


def _human(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds} seconds"
    return f"{math.ceil(seconds / 60)} minutes"
