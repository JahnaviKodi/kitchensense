"""The lazy provider: where the connection string comes from, and when.

No Docker and no Azure. The Key Vault call is the one thing substituted —
everything around it is the real resolution logic, including the ordering that
decides whether the vault is consulted at all.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.engine import make_url

from kitchensense.config import Settings
from kitchensense.db import provider as provider_module
from kitchensense.db.provider import Database, DatabaseUnavailableError

VAULT_URL = "postgresql://vault:pw@vault-host:5432/kitchensense?sslmode=require"


@pytest.fixture(autouse=True)
def _no_ambient_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("KITCHENSENSE_DATABASE_URL", raising=False)


def settings() -> Settings:
    return Settings.from_env()


def test_constructing_a_database_touches_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The property the whole design rests on.

    If constructing this reached for Key Vault or opened a socket, the app
    could not start with the PostgreSQL server stopped — and it is stopped
    every evening.
    """

    def _explode(_: Settings) -> str:
        raise AssertionError("Key Vault must not be consulted at construction time")

    monkeypatch.setattr(provider_module, "_read_secret", _explode)

    database = Database(settings())

    assert database.source == "unresolved"


def test_the_environment_wins_and_the_vault_is_never_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local development and CI must not need Azure credentials."""

    def _explode(_: Settings) -> str:
        raise AssertionError("Key Vault must not be consulted when DATABASE_URL is set")

    monkeypatch.setattr(provider_module, "_read_secret", _explode)
    monkeypatch.setenv("DATABASE_URL", "postgresql://local:pw@localhost:5432/kitchensense")

    database = Database(settings())
    asyncio.run(database.sessionmaker())

    assert database.source == "environment"
    asyncio.run(database.dispose())


def test_the_vault_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(provider_module, "_read_secret", lambda _: VAULT_URL)

    database = Database(settings())
    asyncio.run(database.sessionmaker())

    assert database.source == "key_vault"
    asyncio.run(database.dispose())


def test_the_vaults_connection_string_is_normalised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Azure's string is not one asyncpg can use as written."""
    captured: list[str] = []
    real_create_engine = provider_module.create_engine

    def _capture(url: str, *, echo: bool = False) -> object:
        captured.append(url)
        return real_create_engine(url, echo=echo)

    monkeypatch.setattr(provider_module, "_read_secret", lambda _: VAULT_URL)
    monkeypatch.setattr(provider_module, "create_engine", _capture)

    database = Database(settings())
    asyncio.run(database.sessionmaker())

    resolved = make_url(captured[0])
    assert resolved.drivername == "postgresql+asyncpg"
    assert resolved.query["ssl"] == "require"


def test_the_vault_is_consulted_once_for_concurrent_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cold start under load must not stampede Key Vault."""
    calls = 0

    def _count(_: Settings) -> str:
        nonlocal calls
        calls += 1
        return VAULT_URL

    monkeypatch.setattr(provider_module, "_read_secret", _count)
    database = Database(settings())

    async def scenario() -> None:
        await asyncio.gather(*(database.sessionmaker() for _ in range(8)))
        await database.dispose()

    asyncio.run(scenario())

    assert calls == 1


def test_a_failing_vault_is_reported_not_raised_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``warm()`` is what the lifespan calls, and it must never raise."""

    def _fail(_: Settings) -> str:
        raise RuntimeError("no managed identity here")

    monkeypatch.setattr(provider_module, "_read_secret", _fail)
    database = Database(settings())

    asyncio.run(database.warm())  # must not raise

    status = asyncio.run(database.check())
    assert status.reachability == "not_configured"


def test_asking_for_a_session_without_a_string_is_an_explicit_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(_: Settings) -> str:
        raise RuntimeError("no managed identity here")

    monkeypatch.setattr(provider_module, "_read_secret", _fail)
    database = Database(settings())

    with pytest.raises(DatabaseUnavailableError):
        asyncio.run(database.sessionmaker())


def test_a_failing_vault_is_not_re_asked_on_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Back off after a failure instead of stalling each request in turn.

    A failed lookup costs seconds and a worker thread — the SDK retries
    internally — and the usual cause, a role assignment still propagating,
    lasts minutes. Retrying per request would fill the thread pool with doomed
    calls and make every 503 slow.
    """
    calls = 0

    def _fail(_: Settings) -> str:
        nonlocal calls
        calls += 1
        raise RuntimeError("role assignment still propagating")

    monkeypatch.setattr(provider_module, "_read_secret", _fail)
    monkeypatch.setenv("KEY_VAULT_RETRY_COOLDOWN_SECONDS", "300")
    database = Database(settings())

    for _ in range(5):
        with pytest.raises(DatabaseUnavailableError):
            asyncio.run(database.sessionmaker())

    assert calls == 1


def test_the_cooldown_expires_so_recovery_is_noticed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backing off must not mean giving up."""
    attempts = 0

    def _fail_once(_: Settings) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("role assignment still propagating")
        return VAULT_URL

    monkeypatch.setattr(provider_module, "_read_secret", _fail_once)
    # Zero cooldown: the next request tries again immediately.
    monkeypatch.setenv("KEY_VAULT_RETRY_COOLDOWN_SECONDS", "0")
    database = Database(settings())

    with pytest.raises(DatabaseUnavailableError):
        asyncio.run(database.sessionmaker())

    asyncio.run(database.sessionmaker())

    assert attempts == 2
    assert database.source == "key_vault"
    asyncio.run(database.dispose())


def test_an_unreachable_server_reports_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolved, but nothing answering — the stopped-out-of-hours case."""
    # Port 1 refuses immediately, so this is fast and deterministic.
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/kitchensense")
    monkeypatch.setenv("DATABASE_PROBE_TIMEOUT_SECONDS", "5")

    database = Database(settings())
    status = asyncio.run(database.check())

    assert status.reachability == "unreachable"
    assert status.source == "environment"
    asyncio.run(database.dispose())


def test_the_failure_detail_never_carries_a_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Driver errors quote the DSN they failed on, and this goes over HTTP."""
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://u:hunter2@127.0.0.1:1/kitchensense"
    )

    database = Database(settings())
    status = asyncio.run(database.check())

    assert "hunter2" not in (status.detail or "")
    asyncio.run(database.dispose())
