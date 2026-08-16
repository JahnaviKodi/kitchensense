"""URL normalisation — small, pure, and the difference between working and not.

Azure hands us ``postgresql://...?sslmode=require``. Both halves of that are
wrong for this app: the driver resolves to synchronous psycopg, and asyncpg
has no ``sslmode`` argument. SQLAlchemy's asyncpg dialect forwards unknown
query parameters straight into ``asyncpg.connect()``, so an untranslated
``sslmode`` is a ``TypeError`` that appears only against a real Azure
database — never locally, never in CI.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from kitchensense.db.session import (
    configured_database_url,
    database_url,
    normalize_database_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:pw@host:5432/db",
        "postgres://user:pw@host:5432/db",
        "postgresql+asyncpg://user:pw@host:5432/db",
    ],
)
def test_every_flavour_ends_up_on_asyncpg(url: str) -> None:
    assert make_url(normalize_database_url(url)).drivername == "postgresql+asyncpg"


def test_sslmode_becomes_the_argument_asyncpg_understands() -> None:
    result = make_url(
        normalize_database_url("postgresql://user:pw@host:5432/db?sslmode=require")
    )

    assert "sslmode" not in result.query
    assert result.query["ssl"] == "require"


def test_an_explicit_ssl_argument_wins() -> None:
    result = make_url(
        normalize_database_url("postgresql://u:p@h:5432/db?ssl=verify-full&sslmode=require")
    )

    assert result.query["ssl"] == "verify-full"


def test_the_password_survives() -> None:
    """``str()`` on a SQLAlchemy URL masks the password with ``***``.

    Rendering it that way produces a URL that parses cleanly and then fails to
    authenticate, which is a confusing way to spend an afternoon.
    """
    result = normalize_database_url("postgresql://user:s3cr3t@host:5432/db")

    assert "s3cr3t" in result
    assert "***" not in result


def test_other_query_parameters_are_left_alone() -> None:
    result = make_url(
        normalize_database_url(
            "postgresql://u:p@h:5432/db?sslmode=require&application_name=kitchensense"
        )
    )

    assert result.query["application_name"] == "kitchensense"
    assert result.query["ssl"] == "require"


def test_an_unset_environment_reads_as_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """``None`` is what tells the provider to go and ask Key Vault."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("KITCHENSENSE_DATABASE_URL", raising=False)

    assert configured_database_url() is None


def test_a_blank_environment_variable_reads_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KITCHENSENSE_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "   ")

    assert configured_database_url() is None


def test_the_environment_is_normalised_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KITCHENSENSE_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/db?sslmode=require")

    result = make_url(configured_database_url() or "")
    assert result.drivername == "postgresql+asyncpg"
    assert result.query["ssl"] == "require"


def test_alembic_always_gets_some_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("KITCHENSENSE_DATABASE_URL", raising=False)

    assert make_url(database_url()).drivername == "postgresql+asyncpg"
