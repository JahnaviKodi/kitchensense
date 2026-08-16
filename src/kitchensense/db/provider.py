"""Lazy database wiring.

The PostgreSQL server is stopped outside working hours, so the app has to come
up without it. Nothing here opens a connection — or even reads the connection
string — until something actually needs data. Starting the app touches the
database not at all, which means:

* the container starts and passes its liveness probe with the server stopped;
* ``/health`` and ``/`` answer normally;
* ``/health/deep`` reports the database as unreachable rather than pretending;
* only the endpoints that need rows fail, and they fail with 503 rather than
  a stack trace.

Resolution order is environment first, Key Vault second. ``DATABASE_URL``
covers local development and the test suite; in Azure it is unset, and the
connection string is read from the vault with the container app's
user-assigned managed identity.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from kitchensense.config import Settings
from kitchensense.db.session import (
    configured_database_url,
    create_engine,
    create_sessionmaker,
    normalize_database_url,
)

__all__ = [
    "Database",
    "DatabaseStatus",
    "DatabaseUnavailableError",
]

logger = logging.getLogger(__name__)

UrlSource = Literal["environment", "key_vault", "unresolved"]
Reachability = Literal["reachable", "unreachable", "not_configured"]


class DatabaseUnavailableError(RuntimeError):
    """No usable connection string could be resolved.

    Distinct from "the server is down": this means we never found out where
    the server *is*. Both end up as a 503, but only one of them is fixed by
    starting PostgreSQL.
    """


@dataclass(frozen=True, slots=True)
class DatabaseStatus:
    """What ``/health/deep`` reports. Never raises, never fails the app."""

    reachability: Reachability
    source: UrlSource
    detail: str | None = None


class Database:
    """Resolves a connection string once, then hands out sessions.

    Safe to construct at import time: ``__init__`` does no I/O, and
    ``create_async_engine`` does not connect until the pool is first used.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._engine: AsyncEngine | None = None
        self._sessionmaker: async_sessionmaker[AsyncSession] | None = None
        self._source: UrlSource = "unresolved"
        self._last_error: str | None = None
        # monotonic() deadline before which a failed Key Vault lookup is not
        # retried. See _resolve_url.
        self._retry_not_before: float = 0.0

    @property
    def source(self) -> UrlSource:
        return self._source

    async def sessionmaker(self) -> async_sessionmaker[AsyncSession]:
        """The session factory, building the engine on first call.

        Raises:
            DatabaseUnavailableError: no connection string could be resolved.
        """
        if self._sessionmaker is not None:
            return self._sessionmaker

        async with self._lock:
            # Checked again inside the lock: several requests can arrive
            # together on a cold start, and only one of them should call out
            # to Key Vault.
            if self._sessionmaker is not None:
                return self._sessionmaker

            url = await self._resolve_url()
            self._engine = create_engine(url)
            self._sessionmaker = create_sessionmaker(self._engine)
            return self._sessionmaker

    async def _resolve_url(self) -> str:
        from_environment = configured_database_url()
        if from_environment is not None:
            self._source = "environment"
            self._last_error = None
            logger.info("Database URL read from the environment")
            return from_environment

        # A failed lookup is not cheap: the Azure SDK retries internally, so
        # it costs seconds and occupies a worker thread. The usual cause — a
        # role assignment still propagating — lasts minutes, and retrying it
        # on every request would turn a burst of traffic into a thread pool
        # full of doomed calls to Key Vault. So back off, and answer the 503
        # immediately instead of slowly.
        if time.monotonic() < self._retry_not_before:
            raise DatabaseUnavailableError(
                self._last_error or "the connection string could not be read"
            )

        try:
            secret = await asyncio.wait_for(
                asyncio.to_thread(_read_secret, self._settings),
                timeout=self._settings.key_vault_timeout_seconds,
            )
        except TimeoutError as exc:
            raise self._vault_failed(
                f"timed out reading {self._settings.postgres_secret_name!r} from "
                f"{self._settings.key_vault_uri}"
            ) from exc
        except Exception as exc:
            raise self._vault_failed(
                f"could not read {self._settings.postgres_secret_name!r} from "
                f"{self._settings.key_vault_uri}: {exc}"
            ) from exc

        self._source = "key_vault"
        self._last_error = None
        self._retry_not_before = 0.0
        logger.info(
            "Database URL read from Key Vault secret %r",
            self._settings.postgres_secret_name,
        )
        return normalize_database_url(secret)

    def _vault_failed(self, reason: str) -> DatabaseUnavailableError:
        self._last_error = reason
        self._retry_not_before = (
            time.monotonic() + self._settings.key_vault_retry_cooldown_seconds
        )
        return DatabaseUnavailableError(reason)

    async def warm(self) -> None:
        """Resolve the connection string at startup, without insisting on it.

        Worth doing: it surfaces a misconfigured vault or a missing role
        assignment in the startup logs instead of in the first user's request.
        Worth swallowing: a vault that is briefly unreachable must not stop
        the container from starting, and neither must a stopped database — no
        connection is opened here, only the string looked up.
        """
        try:
            await self.sessionmaker()
        except DatabaseUnavailableError as exc:
            logger.warning(
                "Starting without a database connection string; endpoints that "
                "need data will answer 503 until it resolves (%s)",
                exc,
            )

    async def check(self) -> DatabaseStatus:
        """Probe the database for ``/health/deep``. Never raises."""
        try:
            factory = await self.sessionmaker()
        except DatabaseUnavailableError as exc:
            return DatabaseStatus("not_configured", self._source, str(exc))

        try:
            await asyncio.wait_for(
                self._select_one(factory),
                timeout=self._settings.database_probe_timeout_seconds,
            )
        except TimeoutError:
            return DatabaseStatus(
                "unreachable",
                self._source,
                f"no response within {self._settings.database_probe_timeout_seconds:g}s",
            )
        except Exception as exc:
            # Deliberately broad. This is a health report, and a probe that
            # raises is a probe that has failed at its one job. The full error
            # goes to the logs; only its type goes over HTTP.
            logger.warning("Database probe failed", exc_info=exc)
            return DatabaseStatus("unreachable", self._source, _brief(exc))

        return DatabaseStatus("reachable", self._source)

    @staticmethod
    async def _select_one(factory: async_sessionmaker[AsyncSession]) -> None:
        async with factory() as session:
            await session.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._sessionmaker = None


def _read_secret(settings: Settings) -> str:
    """Fetch the connection string from Key Vault. Runs in a worker thread.

    The synchronous SDK is used on purpose. This happens once per process, and
    the async Azure clients pull in a separate HTTP transport for no benefit
    at that frequency.

    Imported here rather than at module scope so local development and the
    test suite — which both set ``DATABASE_URL`` and never reach this — do not
    pay for the Azure SDK import, and do not need it installed.
    """
    from azure.identity import ManagedIdentityCredential
    from azure.keyvault.secrets import SecretClient

    # The SDK's HTTP policy logs every request line and header at INFO, which
    # at uvicorn's default level buries the startup logs in vault traffic for
    # a call that happens once. Raised to WARNING here rather than in logging
    # config, so it stays next to the reason.
    logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(
        logging.WARNING
    )

    # The client id is required: the container app has a *user-assigned*
    # identity, and a credential given no client id would look for a
    # system-assigned one that does not exist.
    credential = ManagedIdentityCredential(
        client_id=settings.managed_identity_client_id
    )
    try:
        client = SecretClient(vault_url=settings.key_vault_uri, credential=credential)
        try:
            secret = client.get_secret(settings.postgres_secret_name)
        finally:
            client.close()
    finally:
        credential.close()

    if not secret.value:
        raise ValueError(
            f"Key Vault secret {settings.postgres_secret_name!r} is empty"
        )
    return secret.value


def _brief(exc: Exception) -> str:
    """A one-line reason, with no connection string smuggled into it.

    Driver errors quote the DSN they failed on, password included, and this
    string is returned over HTTP.
    """
    return type(exc).__name__
