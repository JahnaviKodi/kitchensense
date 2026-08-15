"""Engine and session construction.

Nothing here holds a module-level engine. The application wires one up at
startup and hands it to repositories; tests build a throwaway engine against a
container. A global would make the second of those impossible.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:local@localhost:5432/kitchensense"


def database_url() -> str:
    """The configured database URL, normalised onto the asyncpg driver.

    Azure and docker-compose both hand us a plain ``postgresql://`` URL, which
    SQLAlchemy would otherwise resolve to the synchronous psycopg driver.
    """
    url = os.environ.get("KITCHENSENSE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        return DEFAULT_DATABASE_URL
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+asyncpg://", 1)
    return url


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
