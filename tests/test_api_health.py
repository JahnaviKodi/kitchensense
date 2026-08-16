"""Health endpoints, and the promise that the app starts without a database.

The PostgreSQL server is stopped outside working hours. These tests pin the
behaviour that follows from that: the app comes up, ``/health`` answers,
``/health/deep`` tells the truth without failing, and only the endpoints that
actually need rows return 503.

Most of this needs no Docker. The one test that asserts *reachable* does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from kitchensense.api.security import get_verifier
from kitchensense.auth.verifier import TokenVerifier
from kitchensense.db import provider as provider_module
from kitchensense.main import app, lifespan
from tests import identity


@pytest.fixture(autouse=True)
def _forget_the_database() -> Iterator[None]:
    """Drop any provider left on the app by another test.

    ``app`` is a module-level singleton, and the database handle is cached on
    its state, so these tests would otherwise inherit each other's wiring.
    """
    if hasattr(app.state, "database"):
        del app.state.database
    yield
    if hasattr(app.state, "database"):
        del app.state.database


@pytest.fixture(autouse=True)
def _no_ambient_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("KITCHENSENSE_DATABASE_URL", raising=False)


def _no_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(_: object) -> str:
        raise RuntimeError("no managed identity in a test process")

    monkeypatch.setattr(provider_module, "_read_secret", _fail)


async def _get(path: str, **kwargs: object) -> tuple[int, dict[str, object]]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(path, **kwargs)  # type: ignore[arg-type]
    return response.status_code, response.json()


def test_the_app_starts_with_no_database_at_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The requirement, stated directly.

    Startup runs the real lifespan with no ``DATABASE_URL`` and a Key Vault
    that refuses. It must complete, not raise.
    """
    _no_vault(monkeypatch)

    async def scenario() -> None:
        async with lifespan(app):
            status, body = await _get("/health")
            assert status == 200
            assert body == {"status": "ok"}

    asyncio.run(scenario())


def test_liveness_never_touches_the_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe Azure restarts the container over.

    A ``/health`` that consulted the database would have Azure killing a
    perfectly healthy container every evening, for a condition restarting
    cannot fix.
    """

    def _explode(_: object) -> str:
        raise AssertionError("/health must not reach for a connection string")

    monkeypatch.setattr(provider_module, "_read_secret", _explode)

    status, body = asyncio.run(_get("/health"))

    assert status == 200
    assert body == {"status": "ok"}


def test_deep_health_reports_an_unconfigured_database_without_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_vault(monkeypatch)

    status, body = asyncio.run(_get("/health/deep"))

    assert status == 200
    assert body["status"] == "ok"
    assert body["database"] == "not_configured"
    assert body["database_url_source"] == "unresolved"


def test_deep_health_reports_a_stopped_server_without_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@127.0.0.1:1/kitchensense")

    status, body = asyncio.run(_get("/health/deep"))

    assert status == 200
    assert body["status"] == "ok"
    assert body["database"] == "unreachable"
    assert body["database_url_source"] == "environment"


def test_deep_health_never_leaks_the_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:hunter2@127.0.0.1:1/kitchensense")

    _, body = asyncio.run(_get("/health/deep"))

    assert "hunter2" not in str(body)


def _authenticated(_: pytest.MonkeyPatch) -> dict[str, str]:
    """Get past the door, so the tests below are about the database.

    A valid token is needed to reach any of this: authentication is checked
    before the session is opened, so an unauthenticated request gets a 401 and
    never finds out whether the database was up. That ordering has its own
    test in ``test_api_auth.py``.
    """
    stub = identity.TenantStub()
    app.dependency_overrides[get_verifier] = lambda: TokenVerifier(
        identity.settings(), transport=stub.transport
    )
    return identity.bearer(identity.make_token())


@pytest.fixture(autouse=True)
def _clear_verifier_override() -> Iterator[None]:
    yield
    app.dependency_overrides.pop(get_verifier, None)


@pytest.mark.parametrize(
    "path", ["/inventory", "/inventory/as-of?timestamp=2026-03-06T12:00:00Z"]
)
def test_data_endpoints_answer_503_without_a_database(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Only the endpoints that need rows fail, and they fail legibly."""
    _no_vault(monkeypatch)
    headers = _authenticated(monkeypatch)

    status, body = asyncio.run(_get(path, headers=headers))

    assert status == 503
    assert "database" in body["detail"].lower()  # type: ignore[union-attr]


def test_posting_an_event_answers_503_without_a_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_vault(monkeypatch)
    headers = _authenticated(monkeypatch)

    async def scenario() -> tuple[int, dict[str, object]]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/inventory/events",
                headers=headers,
                json={
                    "canonical_product_id": "00000000-0000-4000-8000-000000000001",
                    "event_type": "purchased",
                    "quantity_delta": "1",
                    "unit": "l",
                    "storage_location": "fridge",
                    "occurred_at": "2026-03-02T12:00:00Z",
                    "source": "receipt_ocr",
                    "idempotency_key": "no-database",
                },
            )
        return response.status_code, response.json()

    status, body = asyncio.run(scenario())

    assert status == 503
    assert "database" in body["detail"].lower()  # type: ignore[union-attr]


def test_unavailability_is_reported_before_the_body_is_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a malformed request gets the 503, not a 422.

    FastAPI resolves dependencies before validating the body, so the session
    dependency's refusal wins. That ordering is the useful one: there is no
    point telling a caller their JSON is wrong when the request could not have
    been served regardless.
    """
    _no_vault(monkeypatch)
    headers = _authenticated(monkeypatch)

    async def scenario() -> int:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/inventory/events", headers=headers, json={"nonsense": True}
            )
        return response.status_code

    assert asyncio.run(scenario()) == 503


def test_the_root_endpoint_still_describes_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_vault(monkeypatch)

    status, body = asyncio.run(_get("/"))

    assert status == 200
    assert body == {"service": "KitchenSense", "docs": "/docs"}


@pytest.mark.postgres
def test_deep_health_reports_a_reachable_database(
    postgres_url: str, loop: asyncio.AbstractEventLoop, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", postgres_url)

    async def scenario() -> tuple[int, dict[str, object]]:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/health/deep")
        return response.status_code, response.json()

    status, body = loop.run_until_complete(scenario())

    assert status == 200
    assert body["database"] == "reachable"
    assert body["database_url_source"] == "environment"

    database = app.state.database
    loop.run_until_complete(database.dispose())
