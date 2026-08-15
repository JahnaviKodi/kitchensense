"""The migration is the artefact that runs in production, so it gets tested.

Two failure modes are worth catching automatically. A migration that cannot be
undone turns a bad deploy into an outage with no way back. And a migration
that has quietly drifted from the models produces a schema the ORM half
believes in — the kind of bug that surfaces as a missing column three weeks
later, on a table nobody has queried since.
"""

from __future__ import annotations

import asyncio

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
