"""Shared FastAPI dependencies: the database session and the household."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from kitchensense.config import PLACEHOLDER_HOUSEHOLD_ID, Settings
from kitchensense.db.provider import Database, DatabaseUnavailableError

__all__ = [
    "DatabaseDep",
    "HouseholdDep",
    "SessionDep",
    "ensure_database",
    "get_database",
    "get_household_id",
    "get_session",
]

logger = logging.getLogger(__name__)


def ensure_database(app: FastAPI) -> Database:
    """Attach a lazy database handle to the app, once.

    Normally done by the lifespan handler. Built on demand if it is missing,
    because an ASGI transport that skips the lifespan — which is how the tests
    drive the app — would otherwise find no database at all. Constructing one
    is free: it opens nothing and reads nothing.
    """
    database: Database | None = getattr(app.state, "database", None)
    if database is None:
        database = Database(Settings.from_env())
        app.state.database = database
    return database


def get_database(request: Request) -> Database:
    return ensure_database(request.app)


DatabaseDep = Annotated[Database, Depends(get_database)]


async def get_session(database: DatabaseDep) -> AsyncIterator[AsyncSession]:
    """One session per request, rolled back if the handler raises.

    Committing is left to the handlers that write. A read path that never
    commits cannot accidentally persist half of something, and a write that
    commits explicitly surfaces the commit's own failure in the response
    rather than in a teardown nobody sees.
    """
    try:
        factory = await database.sessionmaker()
    except DatabaseUnavailableError as exc:
        # No connection string. Distinct from a database that is merely down,
        # though both are a 503 to the caller.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The database is not configured yet.",
        ) from exc

    async with factory() as session:
        try:
            yield session
        except Exception:
            try:
                await session.rollback()
            except Exception:
                # The usual reason a handler raised is that the connection
                # never came up, in which case rolling it back fails too.
                # Letting that replace the original error would report a
                # symptom instead of the cause.
                logger.warning("Rollback failed after a request error", exc_info=True)
            raise


def get_household_id() -> uuid.UUID:
    """Which household this request is for.

    TODO(auth): hardcoded. Once Entra External ID is wired up this reads the
    household claim from the validated access token, and becomes the one place
    tenancy is decided. Everything downstream already takes ``household_id``
    as a required keyword argument, so only this function changes.
    """
    return PLACEHOLDER_HOUSEHOLD_ID


# Annotated aliases rather than `= Depends(...)` defaults: the handlers read as
# ordinary typed functions, and a parameter with no default cannot be called
# with the dependency accidentally omitted.
SessionDep = Annotated[AsyncSession, Depends(get_session)]
HouseholdDep = Annotated[uuid.UUID, Depends(get_household_id)]
