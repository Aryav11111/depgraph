"""Request-scoped logging context and access logging."""

import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = structlog.get_logger("depgraph.access")

REQUEST_ID_HEADER = "x-request-id"


class RequestContextMiddleware:
    """Binds a request_id to every log line emitted while handling a request.

    Honours an incoming X-Request-ID (e.g. from a load balancer) so a request can be
    traced across services, and echoes it back on the response. Written as pure ASGI
    rather than BaseHTTPMiddleware to avoid that class's known streaming/contextvar
    pitfalls.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(REQUEST_ID_HEADER.encode())
        request_id = incoming.decode() if incoming else uuid.uuid4().hex
        status_code = 500
        start = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", []).append(
                    (REQUEST_ID_HEADER.encode(), request_id.encode())
                )
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                log.info(
                    "request",
                    method=scope["method"],
                    path=scope["path"],
                    status=status_code,
                    duration_ms=round((time.perf_counter() - start) * 1000, 2),
                )
