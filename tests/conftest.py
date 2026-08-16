"""Test harness for the data layer.

The database tests run against a real Postgres in a container, migrated with
the real Alembic migration. Nothing here uses ``create_all``: a schema built
from the models would test the models against themselves and let the migration
rot unnoticed, and the migration is the artefact that actually runs in
production.

Database tests are written as plain synchronous functions that hand a
coroutine to :meth:`Database.run`. That keeps every test — including the
Hypothesis ones, which cannot be ``async def`` — on one session-scoped event
loop, and one event loop is a hard requirement for asyncpg: a connection
opened on one loop cannot be used from another.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from kitchensense.db import provider
from kitchensense.db.session import create_engine
from kitchensense.models import CanonicalProduct, Household

ROOT = Path(__file__).resolve().parents[1]

T = TypeVar("T")


@pytest.fixture(autouse=True)
def _no_key_vault_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing in this suite talks to Azure.

    Without this, any test that reaches ``/health/deep`` with no
    ``DATABASE_URL`` set makes a real HTTPS request to the production vault
    and waits for it to fail — a network dependency in a unit test, and a slow
    one on a CI runner with no managed identity to present. Tests that mean to
    exercise the Key Vault path patch this again themselves; a later
    ``monkeypatch.setattr`` on the same target wins.
    """

    def _refuse(_: object) -> str:
        raise RuntimeError("Key Vault is not reachable from the test suite")

    monkeypatch.setattr(provider, "_read_secret", _refuse)


@pytest.fixture(scope="session")
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    """One event loop for the whole session, shared by every database test."""
    event_loop = asyncio.new_event_loop()
    try:
        yield event_loop
    finally:
        event_loop.close()


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A migrated, throwaway Postgres. Skips the suite when Docker is absent."""
    try:
        # testcontainers 4.x is moving modules under `community`; the old path
        # still works but warns.
        try:
            from testcontainers.community.postgres import PostgresContainer
        except ImportError:
            from testcontainers.postgres import PostgresContainer
    except ImportError as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"testcontainers is not installed: {exc}")

    try:
        container = PostgresContainer("postgres:16", driver="psycopg")
        container.start()
    except Exception as exc:  # pragma: no cover - depends on the environment
        pytest.skip(f"could not start a Postgres container (is Docker running?): {exc}")

    url = (
        f"postgresql+asyncpg://{container.username}:{container.password}"
        f"@{container.get_container_host_ip()}"
        f":{container.get_exposed_port(container.port)}/{container.dbname}"
    )
    try:
        run_migrations(url)
        yield url
    finally:
        container.stop()


def run_migrations(url: str, revision: str = "head") -> None:
    """Run the real migration, exactly as a deployment would.

    ``revision="base"`` downgrades instead, which is how the reversibility
    test walks the schema back down.
    """
    from alembic import command
    from alembic.config import Config

    previous = os.environ.get("KITCHENSENSE_DATABASE_URL")
    os.environ["KITCHENSENSE_DATABASE_URL"] = url
    try:
        config = Config(str(ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(ROOT / "alembic"))
        if revision == "base":
            command.downgrade(config, revision)
        else:
            command.upgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("KITCHENSENSE_DATABASE_URL", None)
        else:
            os.environ["KITCHENSENSE_DATABASE_URL"] = previous


@pytest.fixture(scope="session")
def engine(
    postgres_url: str, loop: asyncio.AbstractEventLoop
) -> Iterator[AsyncEngine]:
    async_engine = create_engine(postgres_url)
    try:
        yield async_engine
    finally:
        loop.run_until_complete(async_engine.dispose())


@dataclass
class Database:
    """Runs one coroutine per test inside a transaction that is always undone.

    Isolation is by rollback rather than by truncation, which matters more
    than usual here: ``inventory_events`` has triggers that refuse DELETE and
    TRUNCATE outright, so the usual "clean the tables between tests" approach
    is not available to us.
    """

    engine: AsyncEngine
    loop: asyncio.AbstractEventLoop

    def run(self, scenario: Callable[[AsyncSession], Awaitable[T]]) -> T:
        async def _run() -> T:
            async with self.engine.connect() as connection:
                transaction = await connection.begin()
                session = AsyncSession(
                    bind=connection, join_transaction_mode="create_savepoint"
                )
                try:
                    return await scenario(session)
                finally:
                    await session.close()
                    await transaction.rollback()

        return self.loop.run_until_complete(_run())


@pytest.fixture
def db(engine: AsyncEngine, loop: asyncio.AbstractEventLoop) -> Database:
    return Database(engine=engine, loop=loop)


async def make_household(session: AsyncSession, name: str = "Test household") -> uuid.UUID:
    household = Household(id=uuid.uuid4(), name=name, timezone="Europe/London")
    session.add(household)
    await session.flush()
    return household.id


async def make_product(
    session: AsyncSession,
    name: str = "Semi-skimmed milk",
    *,
    product_id: uuid.UUID | None = None,
) -> uuid.UUID:
    product = CanonicalProduct(
        id=product_id or uuid.uuid4(),
        canonical_name=name,
        default_unit="ml",
        typical_shelf_life_days=7,
    )
    session.add(product)
    await session.flush()
    return product.id


def at(day: int, hour: int = 12) -> datetime:
    """A readable instant in a fixed test month, always timezone-aware."""
    return datetime(2026, 3, day, hour, 0, tzinfo=UTC)
