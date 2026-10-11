"""Test data builders shared across test packages: lockfiles and OSV archives."""

import json
import zipfile
from pathlib import Path
from typing import Any

from depgraph.lockfile.npm import ROOT

REG = "https://registry.npmjs.org"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def pkg(name: str, version: str, **extra: Any) -> dict[str, Any]:
    """A registry package entry with a realistic resolved URL."""
    unscoped = name.rsplit("/", 1)[-1]
    return {"version": version, "resolved": f"{REG}/{name}/-/{unscoped}-{version}.tgz", **extra}


def lock(packages: dict[str, Any], *, version: int = 3, root: dict | None = None) -> dict:
    """A package-lock.json document with root project "app"."""
    return {
        "name": "app",
        "version": "1.0.0",
        "lockfileVersion": version,
        "requires": True,
        "packages": {ROOT: {"name": "app", "version": "1.0.0", **(root or {})}, **packages},
    }


def realworld_lock(version: int = 3) -> bytes:
    return (FIXTURES / "npm" / "realworld" / f"package-lock.v{version}.json").read_bytes()


def osv_records() -> dict[str, dict]:
    """The real OSV records in tests/fixtures/osv, by id."""
    return {
        p.stem: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted((FIXTURES / "osv").glob("*.json"))
    }


def make_zip(path: Path, docs: dict[str, dict | bytes]) -> Path:
    """An OSV-style all.zip: one <id>.json per record."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, doc in docs.items():
            zf.writestr(f"{name}.json", doc if isinstance(doc, bytes) else json.dumps(doc))
    return path
