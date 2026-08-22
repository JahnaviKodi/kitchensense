"""The migration is the artefact that runs in production, so it gets tested.

Two failure modes are worth catching automatically. A migration that cannot be
undone turns a bad deploy into an outage with no way back. And a migration
that has quietly drifted from the models produces a schema the ORM half
believes in — the kind of bug that surfaces as a missing column three weeks
later, on a table nobody has queried since.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from kitchensense.db.base import Base
from kitchensense.db.session import create_engine
from tests.conftest import run_migrations

pytestmark = pytest.mark.postgres

SCRATCH_DATABASE = "migration_roundtrip"


def _url_for(postgres_url: str, database: str) -> str:
    # render_as_string(hide_password=False), because str() on a URL replaces
    # the password with "***" and the connection then fails to authenticate.
    return make_url(postgres_url).set(database=database).render_as_string(
        hide_password=False
    )


def _differences(connection: Connection) -> list[object]:
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "target_metadata": Base.metadata}
    )
    return list(compare_metadata(context, Base.metadata))


def test_the_migrated_schema_matches_the_models(
    engine: AsyncEngine, loop: asyncio.AbstractEventLoop
) -> None:
    """No drift between ``alembic/versions`` and ``src/kitchensense/models``."""

    async def scenario() -> list[object]:
        async with engine.connect() as connection:
            return await connection.run_sync(_differences)

    assert loop.run_until_complete(scenario()) == []


PLACEHOLDER_ID = "11111111-1111-4111-8111-111111111111"
BEFORE_AUTH = "0002_placeholder_household"


@contextmanager
def scratch_database(
    postgres_url: str, loop: asyncio.AbstractEventLoop, name: str
) -> Iterator[str]:
    """An empty database of its own, dropped afterwards."""
    admin = create_engine(_url_for(postgres_url, "postgres"))

    async def with_admin(statement: str) -> None:
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(statement))

    try:
        loop.run_until_complete(with_admin(f'DROP DATABASE IF EXISTS "{name}"'))
        loop.run_until_complete(with_admin(f'CREATE DATABASE "{name}"'))
        yield _url_for(postgres_url, name)
    finally:
        loop.run_until_complete(
            with_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        )
        loop.run_until_complete(admin.dispose())


def _placeholder_exists(url: str, loop: asyncio.AbstractEventLoop) -> bool:
    engine = create_engine(url)

    async def scenario() -> bool:
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT count(*) FROM households "
                        "WHERE id = CAST(:id AS uuid)"
                    ),
                    {"id": PLACEHOLDER_ID},
                )
                return bool(result.scalar_one())
        finally:
            await engine.dispose()

    return loop.run_until_complete(scenario())


def test_the_placeholder_household_is_removed_when_nothing_references_it(
    postgres_url: str, loop: asyncio.AbstractEventLoop
) -> None:
    with scratch_database(postgres_url, loop, "placeholder_clean") as url:
        run_migrations(url, revision=BEFORE_AUTH)
        assert _placeholder_exists(url, loop) is True

        run_migrations(url)

        assert _placeholder_exists(url, loop) is False


def test_the_placeholder_household_survives_if_events_reference_it(
    postgres_url: str, loop: asyncio.AbstractEventLoop
) -> None:
    """The branch that would otherwise take a deploy down.

    A deployment that ran the API before authentication existed has a real
    kitchen record under the placeholder. Deleting the household would fail on
    a RESTRICT foreign key, mid-migration, on a table this revision was never
    meant to touch — so it warns and leaves the row instead. The row is
    unreachable either way: no token derives that id.
    """
    with scratch_database(postgres_url, loop, "placeholder_in_use") as url:
        run_migrations(url, revision=BEFORE_AUTH)
        _seed_legacy_event(url, loop)

        run_migrations(url)

        assert _placeholder_exists(url, loop) is True


def _seed_legacy_event(url: str, loop: asyncio.AbstractEventLoop) -> None:
    """One event under the placeholder, as a pre-auth deployment would have."""
    engine = create_engine(url)

    async def scenario() -> None:
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        """
                        INSERT INTO canonical_products (id, canonical_name, default_unit)
                        VALUES (CAST(:id AS uuid), 'Legacy milk', 'l')
                        """
                    ),
                    {"id": "22222222-2222-4222-8222-222222222222"},
                )
                await connection.execute(
                    text(
                        """
                        INSERT INTO inventory_events (
                            household_id, canonical_product_id, event_type,
                            quantity_delta, unit, storage_location, occurred_at,
                            source, idempotency_key
                        )
                        VALUES (
                            CAST(:household AS uuid), CAST(:product AS uuid),
                            'purchased', 1, 'l', 'fridge', now(),
                            'receipt_ocr', 'legacy-1'
                        )
                        """
                    ),
                    {
                        "household": PLACEHOLDER_ID,
                        "product": "22222222-2222-4222-8222-222222222222",
                    },
                )
        finally:
            await engine.dispose()

    loop.run_until_complete(scenario())


def test_the_migration_can_be_undone_and_reapplied(
    postgres_url: str, loop: asyncio.AbstractEventLoop
) -> None:
    """upgrade → downgrade → upgrade, on a database of its own.

    Run against a scratch database rather than the one the rest of the suite
    shares, so a downgrade cannot take the other tests' schema with it.
    """
    admin = create_engine(_url_for(postgres_url, "postgres"))
    scratch_url = _url_for(postgres_url, SCRATCH_DATABASE)

    async def with_admin(statement: str) -> None:
        async with admin.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(statement))

    async def table_names() -> set[str]:
        scratch = create_engine(scratch_url)
        try:
            async with scratch.connect() as connection:
                result = await connection.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
                return set(result.scalars().all())
        finally:
            await scratch.dispose()

    try:
        loop.run_until_complete(with_admin(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}"'))
        loop.run_until_complete(with_admin(f'CREATE DATABASE "{SCRATCH_DATABASE}"'))

        run_migrations(scratch_url)
        after_upgrade = loop.run_until_complete(table_names())
        assert {
            "households",
            "canonical_products",
            "inventory_events",
            "inventory_snapshot",
            "receipt_uploads",
        } <= after_upgrade

        run_migrations(scratch_url, revision="base")
        after_downgrade = loop.run_until_complete(table_names())
        assert after_downgrade <= {"alembic_version"}

        # And it goes back up cleanly — including the enum types, which a
        # downgrade that forgot to drop them would collide with here.
        run_migrations(scratch_url)
        assert loop.run_until_complete(table_names()) == after_upgrade
    finally:
        loop.run_until_complete(
            with_admin(f'DROP DATABASE IF EXISTS "{SCRATCH_DATABASE}" WITH (FORCE)')
        )
        loop.run_until_complete(admin.dispose())
