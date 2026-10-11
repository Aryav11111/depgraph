"""Structured logging setup.

structlog is the front end; stdlib logging (uvicorn, sqlalchemy, alembic) is routed
through the same processor chain so *every* line comes out in one format. In
containers that format is one JSON object per line on stdout, which Cloud Logging
ingests as structured entries without any agent configuration.
"""

import logging
import sys

import structlog

from depgraph.core.config import Settings


def configure_logging(settings: Settings) -> None:
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,  # pulls in request_id etc.
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: structlog.types.Processor
    if settings.log_json:
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    # Uvicorn installs its own handlers; strip them so its logs flow through ours.
    # The access log is replaced by our request middleware, which carries request_id.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True
    logging.getLogger("uvicorn.access").disabled = True

    # httpx/httpcore log every request at INFO: thousands of lines per advisory
    # sync. Our own events (npm_registry_retry, advisory_sync_completed, ...)
    # already record what matters; keep their warnings and errors only.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(max(root.level, logging.WARNING))
