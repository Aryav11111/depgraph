"""Scan API end to end: real app (lifespan), real Postgres, in-process transport."""

import itertools
import json
import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy import delete, func, select

from depgraph.core.config import Settings
from depgraph.db.models import Scan
from depgraph.db.session import create_sessionmaker
from tests.conftest import make_client
from tests.helpers import lock, pkg, realworld_lock


@pytest.fixture
async def api(settings: Settings, engine) -> AsyncIterator[httpx.AsyncClient]:
    """API client; every scan created through it is deleted afterwards."""
    async with make_client(settings) as client:
        created: list[str] = []
        original_post = client.post

        async def post(url, *a, **kw):
            resp = await original_post(url, *a, **kw)
            if url == "/scans" and resp.status_code in (201, 500):
                body = resp.json()
                created.append(body.get("id") or body["detail"].split()[2])
            return resp

        client.post = post  # type: ignore[method-assign]
        yield client
    async with create_sessionmaker(engine).begin() as s:
        await s.execute(delete(Scan).where(Scan.id.in_([uuid.UUID(i) for i in created])))


@pytest.fixture
async def realworld_scan(api) -> dict:
    resp = await api.post("/scans", content=realworld_lock())
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _scan_count(engine) -> int:
    async with create_sessionmaker(engine)() as s:
        return await s.scalar(select(func.count()).select_from(Scan))


# ------------------------------------------------------------------ POST /scans


async def test_create_scan_builds_graph_synchronously(api, realworld_scan):
    scan = realworld_scan
    assert scan["status"] == "completed"
    assert scan["lockfile_version"] == 3
    assert scan["root_name"] == "acme-dashboard"
    assert scan["node_count"] == 385 and scan["edge_count"] == 570
    assert scan["warnings"] == [] and scan["error"] is None
    assert len(scan["lockfile_sha256"]) == 64


async def test_create_scan_sets_location_header(api):
    resp = await api.post("/scans", content=realworld_lock())
    assert resp.headers["location"] == f"/scans/{resp.json()['id']}"


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (b"", "empty request body"),
        (b"   ", "empty request body"),
        (b"{nope", "not valid JSON"),
        (json.dumps({"lockfileVersion": 1}).encode(), "unsupported lockfileVersion 1"),
        (json.dumps({"lockfileVersion": 3}).encode(), "packages"),
    ],
)
async def test_bad_lockfiles_are_422_and_create_nothing(api, engine, body, detail):
    before = await _scan_count(engine)
    resp = await api.post("/scans", content=body)
    assert resp.status_code == 422
    assert detail in resp.json()["detail"]
    assert await _scan_count(engine) == before


async def test_oversized_lockfile_is_413(engine):
    small = Settings(app_env="test", log_json=False, max_lockfile_bytes=1000)
    async with make_client(small) as client:
        before = await _scan_count(engine)
        resp = await client.post("/scans", content=b"{" + b" " * 2000 + b"}")
        assert resp.status_code == 413

        # also when the client doesn't declare a length (chunked upload)
        async def chunks():
            for _ in range(5):
                yield b" " * 300

        resp = await client.post("/scans", content=chunks())
        assert resp.status_code == 413
        assert await _scan_count(engine) == before


async def test_inconsistent_lockfile_completes_with_warnings(api):
    resp = await api.post("/scans", json=lock({}, root={"dependencies": {"ghost": "^1"}}))
    assert resp.status_code == 201
    assert resp.json()["warnings"][0]["kind"] == "unresolved_dependency"


# --------------------------------------------------------------- GET /scans/:id


async def test_get_scan(api, realworld_scan):
    resp = await api.get(f"/scans/{realworld_scan['id']}")
    assert resp.status_code == 200
    assert resp.json() == realworld_scan


async def test_unknown_scan_is_404_and_malformed_id_is_422(api):
    assert (await api.get(f"/scans/{uuid.uuid4()}")).status_code == 404
    assert (await api.get("/scans/not-a-uuid")).status_code == 422


# --------------------------------------------------------- GET /scans/:id/graph


async def test_graph_returns_every_node_and_edge(api, realworld_scan):
    resp = await api.get(f"/scans/{realworld_scan['id']}/graph")
    assert resp.status_code == 200
    g = resp.json()
    assert len(g["nodes"]) == realworld_scan["node_count"]
    assert len(g["edges"]) == realworld_scan["edge_count"]
    ids = {n["id"] for n in g["nodes"]}
    assert all(e["parent"] in ids and e["child"] in ids for e in g["edges"])
    root = next(n for n in g["nodes"] if n["id"] == g["root"])
    assert (root["source"], root["name"]) == ("root", "acme-dashboard")
    express = next(n for n in g["nodes"] if n["name"] == "express")
    assert express["version"].startswith("4.") and not express["dev"]


async def test_graph_of_failed_scan_is_409(api, engine):
    async with create_sessionmaker(engine).begin() as s:
        scan = Scan(lockfile_sha256="0" * 64, status="failed", error="x")
        s.add(scan)
        await s.flush()
        scan_id = scan.id
    try:
        resp = await api.get(f"/scans/{scan_id}/graph")
        assert resp.status_code == 409
        assert "failed" in resp.json()["detail"]
    finally:
        async with create_sessionmaker(engine).begin() as s:
            await s.execute(delete(Scan).where(Scan.id == scan_id))


# --------------------------------------------------------- GET /scans/:id/paths


async def test_paths_to_a_transitive_package(api, realworld_scan):
    resp = await api.get(f"/scans/{realworld_scan['id']}/paths", params={"package": "debug"})
    assert resp.status_code == 200
    body = resp.json()
    assert {t["version"] for t in body["targets"]} >= {"2.6.9"}  # plus the hoisted 4.x
    assert body["paths"]

    graph = (await api.get(f"/scans/{realworld_scan['id']}/graph")).json()
    edges = {(e["parent"], e["child"], e["dep_type"]) for e in graph["edges"]}
    lengths = [len(p) for p in body["paths"]]
    assert lengths == sorted(lengths), "shortest first"
    for path in body["paths"]:
        assert path[0]["id"] == graph["root"] and path[0]["dep_type"] is None
        assert path[-1]["name"] == "debug"
        for a, b in itertools.pairwise(path):
            assert (a["id"], b["id"], b["dep_type"]) in edges  # every hop is a real edge
        assert len({s["id"] for s in path}) == len(path)  # simple


async def test_paths_through_express_to_nested_debug(api, realworld_scan):
    resp = await api.get(
        f"/scans/{realworld_scan['id']}/paths", params={"package": "debug", "version": "2.6.9"}
    )
    body = resp.json()
    assert [t["version"] for t in body["targets"]] == ["2.6.9"]
    first = [s["name"] for s in body["paths"][0]]
    assert first[0] == "acme-dashboard" and first[-1] == "debug"
    assert any([s["name"] for s in p][:2] == ["acme-dashboard", "express"] for p in body["paths"])


async def test_direct_dependency_has_a_one_hop_path(api, realworld_scan):
    body = (
        await api.get(f"/scans/{realworld_scan['id']}/paths", params={"package": "express"})
    ).json()
    shortest = body["paths"][0]
    assert [(s["name"], s["dep_type"]) for s in shortest] == [
        ("acme-dashboard", None),
        ("express", "prod"),
    ]


async def test_path_cap_is_applied_and_reported(api, realworld_scan):
    url = f"/scans/{realworld_scan['id']}/paths"
    uncapped = (await api.get(url, params={"package": "ms"})).json()
    assert len(uncapped["paths"]) > 1
    capped = (await api.get(url, params={"package": "ms", "max_paths": 1})).json()
    assert len(capped["paths"]) == 1 and capped["truncated"]
    assert capped["paths"][0] == uncapped["paths"][0]


async def test_requested_caps_are_clamped_to_server_limits(api, realworld_scan, settings):
    body = (
        await api.get(
            f"/scans/{realworld_scan['id']}/paths",
            params={"package": "ms", "max_paths": 10**6, "max_depth": 10**6},
        )
    ).json()
    assert body["max_paths"] == settings.paths_hard_max
    assert body["max_depth"] == settings.paths_max_depth


async def test_unknown_package_has_no_targets(api, realworld_scan):
    body = (
        await api.get(f"/scans/{realworld_scan['id']}/paths", params={"package": "left-pad"})
    ).json()
    assert body["targets"] == [] and body["paths"] == [] and not body["truncated"]


async def test_paths_require_package_param(api, realworld_scan):
    assert (await api.get(f"/scans/{realworld_scan['id']}/paths")).status_code == 422


async def test_paths_in_a_cyclic_graph(api):
    doc = lock(
        {
            "node_modules/a": pkg("a", "1.0.0", dependencies={"b": "*"}),
            "node_modules/b": pkg("b", "1.0.0", dependencies={"a": "*", "c": "*"}),
            "node_modules/c": pkg("c", "1.0.0"),
        },
        root={"dependencies": {"a": "*"}},
    )
    scan = (await api.post("/scans", json=doc)).json()
    body = (await api.get(f"/scans/{scan['id']}/paths", params={"package": "c"})).json()
    assert [[s["name"] for s in p] for p in body["paths"]] == [["app", "a", "b", "c"]]
    assert not body["truncated"]


# ------------------------------------------------------------------- OpenAPI


async def test_openapi_documents_the_raw_lockfile_body(api):
    spec = (await api.get("/openapi.json")).json()
    post = spec["paths"]["/scans"]["post"]
    assert "package-lock.json" in post["requestBody"]["description"]
    assert {"/scans/{scan_id}", "/scans/{scan_id}/graph", "/scans/{scan_id}/paths"} <= set(
        spec["paths"]
    )
