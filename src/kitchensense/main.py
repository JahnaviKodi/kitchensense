"""Application assembly.

Startup does no I/O against the database. The connection string is looked up
best-effort and the engine is built lazily, so the container starts cleanly
while the PostgreSQL server is stopped — which it is, outside working hours.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from kitchensense.api import health, inventory
from kitchensense.api.dependencies import ensure_database
from kitchensense.config import Settings
from kitchensense.db.provider import DatabaseUnavailableError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    app.state.settings = settings
    database = ensure_database(app)

    # Best effort, and it never raises: this resolves the connection string so
    # a missing role assignment or a wrong vault URI shows up in the startup
    # logs instead of in the first user's request. It opens no connection, so
    # a stopped database does not affect it either way.
    await database.warm()

    try:
        yield
    finally:
        await database.dispose()


app = FastAPI(
    title="KitchenSense",
    summary="An autonomous agent that reduces household food waste.",
    lifespan=lifespan,
)


@app.exception_handler(DatabaseUnavailableError)
async def _database_unconfigured(
    request: Request, exc: DatabaseUnavailableError
) -> JSONResponse:
    logger.warning("Request needed a database connection string: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "The database is not configured yet."},
    )


@app.exception_handler(OperationalError)
@app.exception_handler(InterfaceError)
async def _database_unreachable(request: Request, exc: DBAPIError) -> JSONResponse:
    """A connection-level failure is a 503, not a 500.

    The usual cause is entirely expected: the PostgreSQL server is stopped
    outside working hours. That is the service being unavailable, not the
    application being broken, and the distinction is what stops it looking
    like a bug every evening. The driver's own message is kept out of the
    response — it quotes the DSN it failed on, password included.
    """
    logger.warning("Database unreachable while handling %s", request.url.path)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "The database is unavailable. Please try again shortly."},
    )


app.include_router(health.router)
app.include_router(inventory.router)
