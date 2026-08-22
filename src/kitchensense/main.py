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

from kitchensense.api import health, inventory, uploads
from kitchensense.api.dependencies import ensure_database, ensure_receipt_store
from kitchensense.api.security import ensure_verifier
from kitchensense.auth.errors import AuthConfigurationError
from kitchensense.config import Settings
from kitchensense.db.provider import DatabaseUnavailableError
from kitchensense.storage import StorageUnavailableError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings.from_env()
    app.state.settings = settings
    database = ensure_database(app)
    verifier = ensure_verifier(app)
    receipts = ensure_receipt_store(app)

    # Best effort, and it never raises: this resolves the connection string so
    # a missing role assignment or a wrong vault URI shows up in the startup
    # logs instead of in the first user's request. It opens no connection, so
    # a stopped database does not affect it either way.
    await database.warm()

    # Likewise loud but not fatal. A container that refused to start would
    # crash-loop instead of serving /health, and the protected endpoints
    # already fail closed with a 503 — nothing is reachable without a tenant
    # to validate against.
    try:
        verifier.require_configured()
        logger.info(
            "Validating tokens for audience %s via %s",
            settings.entra_audience,
            settings.entra_openid_configuration_url,
        )
    except AuthConfigurationError as exc:
        logger.error("%s; every protected endpoint will answer 503", exc)

    # And likewise for storage: no account configured is a working API with
    # one feature missing, not a container that should refuse to start. The
    # key itself is not fetched here — it is fetched on the first upload, and
    # cached from then on.
    try:
        receipts.require_configured()
        logger.info(
            "Receipt uploads go to container %r in %s",
            settings.receipts_container,
            settings.storage_blob_endpoint,
        )
    except StorageUnavailableError as exc:
        logger.error("%s; /uploads will answer 503", exc)

    try:
        yield
    finally:
        await database.dispose()
        await verifier.aclose()


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


@app.exception_handler(StorageUnavailableError)
async def _storage_unavailable(
    request: Request, exc: StorageUnavailableError
) -> JSONResponse:
    """No upload URL could be issued.

    Either nothing is configured or the delegation key could not be fetched —
    a missing role assignment, most likely, or one that has not propagated
    yet. Both are ours to fix, so they are a 503 and the reason goes to the
    logs rather than to the caller.
    """
    logger.warning("Could not issue an upload URL: %s", exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "Receipt uploads are unavailable. Please try again shortly."},
    )


app.include_router(health.router)
app.include_router(inventory.router)
app.include_router(uploads.router)
