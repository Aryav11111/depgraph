"""Error text that is safe to persist and show to API clients."""

from sqlalchemy.exc import DBAPIError


def safe_error_text(exc: BaseException, *, db_message: str = "database error") -> str:
    """Type and message only, never SQL.

    SQLAlchemy's DBAPIError stringifies with the statement and bound parameters,
    which must not leak into stored rows or API responses; those get a generic
    message (the full detail is in the server log).
    """
    if isinstance(exc, DBAPIError):
        return f"{type(exc).__name__}: {db_message}"
    return f"{type(exc).__name__}: {exc}"[:1000]
