"""Command line entry point for advisory jobs.

    python -m depgraph.advisories.cli full [--archive PATH]

`--archive` ingests an already-downloaded all.zip (useful offline / in tests).
"""

import argparse
import asyncio
from pathlib import Path

import httpx

from depgraph import __version__
from depgraph.advisories.ingest import full_load
from depgraph.core.config import get_settings
from depgraph.core.logging import configure_logging
from depgraph.db.session import create_engine, create_sessionmaker


async def _full(archive: Path | None) -> int:
    settings = get_settings()
    configure_logging(settings)
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient(
            timeout=settings.osv_download_timeout_s,
            headers={"User-Agent": f"depgraph/{__version__}"},
        ) as http:
            return await full_load(create_sessionmaker(engine), http, settings, archive=archive)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="depgraph.advisories")
    sub = parser.add_subparsers(dest="command", required=True)
    full = sub.add_parser("full", help="download and ingest the full OSV export")
    full.add_argument("--archive", type=Path, help="ingest a local all.zip instead")
    args = parser.parse_args(argv)
    if args.command == "full":
        asyncio.run(_full(args.archive))


if __name__ == "__main__":
    main()
