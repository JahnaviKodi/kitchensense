"""Engine and session construction.

Nothing here holds a module-level engine. The application wires one up at
startup and hands it to repositories; tests build a throwaway engine against a
container. A global would make the second of those impossible.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:local@localhost:5432/kitchensense"

SYNC_DRIVER_NAMES = {"postgres", "postgresql"}


def normalize_database_url(url: str) -> str:
    """Put a Postgres URL from anywhere onto the driver this app speaks.

    Two rewrites, both of which are failures in production if skipped.

    The driver: Azure, docker-compose and psql all produce plain
    ``postgresql://``, which SQLAlchemy resolves to the *synchronous* psycopg
    driver and then refuses to use from an async engine.

    ``sslmode``: SQLAlchemy's asyncpg dialect forwards unrecognised query
    parameters straight into ``asyncpg.connect()``, and asyncpg has no
    ``sslmode`` argument — it spells the same thing ``ssl``, accepting the
    identical set of values. Azure's connection string ends in
    ``?sslmode=require``, so leaving it alone turns every connection into a
    ``TypeError`` that only ever appears against a real Azure database.
    """
    parsed = make_url(url.strip())
    if parsed.drivername in SYNC_DRIVER_NAMES:
        parsed = parsed.set(drivername="postgresql+asyncpg")

    query = dict(parsed.query)
    sslmode = query.pop("sslmode", None)
    if sslmode is not None and "ssl" not in query:
        query["ssl"] = sslmode
    parsed = parsed.set(query=query)

    # str() on a URL masks the password with "***", which authenticates
    # against nothing.
    return parsed.render_as_string(hide_password=False)


def configured_database_url() -> str | None:
    """The URL from the environment, or ``None`` if none is set.

    ``None`` is meaningful: it is what tells the provider to go and ask Key
    Vault instead.
    """
    url = os.environ.get("KITCHENSENSE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url or not url.strip():
        return None
    return normalize_database_url(url)


def database_url() -> str:
    """The configured URL, falling back to a local docker-compose database.

    Used by Alembic, which always needs *some* URL to talk to.
    """
    return configured_database_url() or DEFAULT_DATABASE_URL


def create_engine(url: str | None = None, *, echo: bool = False) -> AsyncEngine:
    return create_async_engine(url or database_url(), echo=echo, pool_pre_ping=True)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False keeps loaded rows usable after a commit, which
    # matters because repositories return detached domain objects.
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One transaction, committed on success and rolled back on anything else."""
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
