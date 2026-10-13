from pydantic import SecretStr

from depgraph.core.config import Settings
from tests.conftest import make_client


async def test_liveness_does_not_touch_dependencies(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_readiness_reports_postgres_and_redis(client):
    resp = await client.get("/health/ready")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "ok", "checks": {"postgres": "ok", "redis": "ok"}}


async def test_readiness_returns_503_when_a_dependency_is_down(settings: Settings):
    # Port 1 is never listening: a deterministic "Redis is down".
    broken = settings.model_copy(update={"redis_url": SecretStr("redis://127.0.0.1:1/0")})
    async with make_client(broken) as c:
        resp = await c.get("/health/ready")

    assert resp.status_code == 503
    assert resp.json() == {"status": "degraded", "checks": {"postgres": "ok", "redis": "error"}}


async def test_request_id_is_generated_and_echoed(client):
    resp = await client.get("/health")
    assert len(resp.headers["x-request-id"]) == 32


async def test_incoming_request_id_is_propagated(client):
    resp = await client.get("/health", headers={"X-Request-ID": "trace-abc"})
    assert resp.headers["x-request-id"] == "trace-abc"


async def test_cors_allows_the_configured_frontend_origin(client):
    resp = await client.options(
        "/scans",
        headers={"Origin": "http://localhost:3000", "Access-Control-Request-Method": "POST"},
    )
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-credentials" not in resp.headers


async def test_cors_rejects_other_origins(client):
    resp = await client.get("/health", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers


def test_cors_origins_from_json_list_env(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", '["https://a.example","https://b.example"]')
    assert Settings(_env_file=None).cors_origins == ["https://a.example", "https://b.example"]
