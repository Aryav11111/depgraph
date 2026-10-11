"""Command line entry point for advisory jobs.

    python -m depgraph.advisories.cli full [--archive PATH]   # full load
    python -m depgraph.advisories.cli sync                    # one incremental sync
    python -m depgraph.advisories.cli worker                  # sync every interval

`sync` is the shape for a scheduled job (Cloud Run Job + Cloud Scheduler);
`worker` is the long-running loop used by the docker-compose `worker` service.
`--archive` ingests an already-downloaded all.zip (offline / tests).
"""

import argparse
import asyncio
from pathlib import Path

import httpx

from depgraph import __version__
from depgraph.advisories.ingest import full_load
from depgraph.advisories.sync import install_stop_handlers, run_worker, sync
from depgraph.core.config import get_settings
from depgraph.core.logging import configure_logging
from depgraph.db.session import create_engine, create_sessionmaker


async def _run(command: str, archive: Path | None) -> None:
    settings = get_settings()
    configure_logging(settings)
    engine = create_engine(settings)
    try:
        async with httpx.AsyncClient(
            timeout=settings.osv_download_timeout_s,
            headers={"User-Agent": f"depgraph/{__version__}"},
        ) as http:
            if command == "full":
                await full_load(create_sessionmaker(engine), http, settings, archive=archive)
            elif command == "sync":
                await sync(engine, http, settings)
            else:
                stop = asyncio.Event()
                install_stop_handlers(stop)
                await run_worker(engine, http, settings, stop)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="depgraph.advisories")
    sub = parser.add_subparsers(dest="command", required=True)
    full = sub.add_parser("full", help="download and ingest the full OSV export")
    full.add_argument("--archive", type=Path, help="ingest a local all.zip instead")
    sub.add_parser("sync", help="run one incremental sync (full load if no cursor)")
    sub.add_parser("worker", help="sync every OSV_SYNC_INTERVAL_S until stopped")
    args = parser.parse_args(argv)
    asyncio.run(_run(args.command, getattr(args, "archive", None)))


if __name__ == "__main__":
    main()
