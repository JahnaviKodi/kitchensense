"""Shared FastAPI dependencies: the database session, the household, the blob store.

Authentication lives next door in ``security.py``; this module consumes the
principal it produces and turns it into the tenancy argument every repository
demands.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from kitchensense.api.security import PrincipalDep
from kitchensense.config import Settings
from kitchensense.db.provider import Database, DatabaseUnavailableError
from kitchensense.repositories import HouseholdRepository
from kitchensense.storage import ReceiptBlobStore

__all__ = [
    "DatabaseDep",
    "HouseholdDep",
    "ReceiptStoreDep",
    "SessionDep",
    "ensure_database",
    "ensure_receipt_store",
    "get_database",
    "get_household_id",
    "get_receipt_store",
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


# Annotated aliases rather than `= Depends(...)` defaults: the handlers read as
# ordinary typed functions, and a parameter with no default cannot be called
# with the dependency accidentally omitted.
SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def get_household_id(principal: PrincipalDep, session: SessionDep) -> uuid.UUID:
    """Which household this request is for. The single place tenancy is decided.

    The id is derived from the validated token, never read from the request,
    so there is no parameter a caller could supply to reach someone else's
    kitchen. Everything downstream takes it as a required keyword argument.

    ``principal`` is declared first on purpose. FastAPI resolves dependencies
    in parameter order, so an unauthenticated request is refused before a
    database session is opened — which matters out of hours, when opening one
    fails: a caller with no token should be told that, not handed a 503 about
    a database they were never going to be allowed to read.

    The household row is created here, on first sight of a new subject. It is
    committed immediately rather than left to the handler, because a read
    endpoint never commits and the row would vanish with the transaction.
    """
    household_id = principal.household_id

    created = await HouseholdRepository(session).ensure(
        household_id=household_id, name=principal.household_name()
    )
    if created:
        await session.commit()
        logger.info("Created household %s on first sight of its subject", household_id)

    return household_id


HouseholdDep = Annotated[uuid.UUID, Depends(get_household_id)]


def ensure_receipt_store(app: FastAPI) -> ReceiptBlobStore:
    """Attach the receipt blob store to the app, once.

    Built on demand for the same reason the database handle and the verifier
    are: a transport that skips the lifespan still has to find one.
    Constructing it opens no connection and fetches no key — an unconfigured
    deployment gets a store that refuses, not an error at startup.
    """
    store: ReceiptBlobStore | None = getattr(app.state, "receipt_store", None)
    if store is None:
        store = ReceiptBlobStore(Settings.from_env())
        app.state.receipt_store = store
    return store


def get_receipt_store(request: Request) -> ReceiptBlobStore:
    return ensure_receipt_store(request.app)


ReceiptStoreDep = Annotated[ReceiptBlobStore, Depends(get_receipt_store)]
