"""Authentication at the HTTP edge.

The database-backed cases run against a real Postgres, because tenancy is only
meaningfully tested by writing two households' rows and checking neither can
see the other's.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kitchensense.api.security import get_verifier
from kitchensense.auth.verifier import TokenVerifier
from kitchensense.main import app
from tests import identity
from tests.conftest import Database, make_product
from tests.test_api_inventory import api, event_body

PROTECTED = [
    ("GET", "/inventory"),
    ("GET", "/inventory/as-of?timestamp=2026-03-06T12:00:00Z"),
    ("POST", "/inventory/events"),
    ("POST", "/uploads"),
    ("POST", "/uploads/8f14e45f-ceea-4b3c-9c4e-1a2b3c4d5e6f/confirm"),
]


async def _request(client: AsyncClient, method: str, path: str, **kwargs: object):
    if method == "POST":
        return await client.post(path, json={}, **kwargs)  # type: ignore[arg-type]
    return await client.get(path, **kwargs)  # type: ignore[arg-type]


# ----------------------------------------------------------------------
# No database needed: these are refused before anything is read
# ----------------------------------------------------------------------


@pytest.fixture
def unauthenticated_client() -> AsyncClient:
    """The app with a verifier pointed at the fake tenant, and no session override."""
    stub = identity.TenantStub()
    app.dependency_overrides[get_verifier] = lambda: TokenVerifier(
        identity.settings(), transport=stub.transport
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_verifier, None)


@pytest.mark.parametrize(("method", "path"), PROTECTED, ids=lambda v: str(v))
def test_a_request_with_no_token_is_refused(
    loop, unauthenticated_client: AsyncClient, method: str, path: str
) -> None:
    async def scenario():
        async with unauthenticated_client as client:
            return await _request(client, method, path)

    response = loop.run_until_complete(scenario())

    assert response.status_code == 401
    # RFC 6750: a challenge for an absent credential carries no error code,
    # because nothing the client sent was wrong.
    challenge = response.headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'realm="kitchensense"' in challenge
    assert "error=" not in challenge


@pytest.mark.parametrize(
    ("description", "header"),
    [
        ("an empty Authorization header", ""),
        ("a bare token with no scheme", "abc.def.ghi"),
        ("the wrong scheme", "Basic dXNlcjpwYXNz"),
        ("Bearer with nothing after it", "Bearer"),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_a_malformed_authorization_header_is_refused(
    loop, unauthenticated_client: AsyncClient, description: str, header: str
) -> None:
    async def scenario():
        async with unauthenticated_client as client:
            return await client.get("/inventory", headers={"Authorization": header})

    response = loop.run_until_complete(scenario())

    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


@pytest.mark.parametrize(
    ("description", "token_kwargs"),
    [
        ("signed by the wrong key", {"key": identity.impostor_key()}),
        ("expired", {"expires_in": timedelta(seconds=-30)}),
        ("not yet valid", {"not_before": timedelta(hours=1)}),
        ("for another audience", {"audience": "11111111-2222-3333-4444-555555555555"}),
        ("from another issuer", {"issuer": "https://evil.example.invalid/v2.0"}),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_an_invalid_token_is_refused_with_a_challenge(
    loop, unauthenticated_client: AsyncClient, description: str, token_kwargs: dict
) -> None:
    async def scenario():
        async with unauthenticated_client as client:
            return await client.get(
                "/inventory",
                headers=identity.bearer(identity.make_token(**token_kwargs)),
            )

    response = loop.run_until_complete(scenario())

    assert response.status_code == 401
    assert 'error="invalid_token"' in response.headers["WWW-Authenticate"]


def test_a_token_without_the_scope_is_refused(
    loop, unauthenticated_client: AsyncClient
) -> None:
    async def scenario():
        async with unauthenticated_client as client:
            return await client.get(
                "/inventory",
                headers=identity.bearer(identity.make_token(scopes="inventory.read")),
            )

    response = loop.run_until_complete(scenario())

    assert response.status_code == 401
    assert 'error="insufficient_scope"' in response.headers["WWW-Authenticate"]


@pytest.mark.parametrize("with_token", [True, False], ids=["with a token", "without one"])
def test_an_unconfigured_tenant_is_a_503_not_a_401(loop, with_token: bool) -> None:
    """A server misconfiguration must not read as a credential problem.

    A 401 would send the caller off to re-authenticate against a tenant this
    deployment has never been told about. And the answer must not depend on
    whether a token was presented: a server with no tenant cannot accept
    anyone's, so replying 503 to one caller and 401 to another would be two
    different stories about the same broken deployment.
    """
    stub = identity.TenantStub()
    app.dependency_overrides[get_verifier] = lambda: TokenVerifier(
        identity.settings(entra_tenant_id="", entra_client_id="", entra_audience=""),
        transport=stub.transport,
    )
    headers = identity.bearer(identity.make_token()) if with_token else {}

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/inventory", headers=headers)

    response = loop.run_until_complete(scenario())

    assert response.status_code == 503


def test_authentication_is_checked_before_the_database(loop) -> None:
    """Order matters out of hours.

    With PostgreSQL stopped, a request carrying no token should still be told
    it needs one — not handed a 503 about a database it was never going to be
    allowed to read.
    """
    stub = identity.TenantStub()
    app.dependency_overrides[get_verifier] = lambda: TokenVerifier(
        identity.settings(), transport=stub.transport
    )
    # No session override at all, and no DATABASE_URL, so the session
    # dependency would fail if it were ever reached.
    if hasattr(app.state, "database"):
        del app.state.database

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.get("/inventory")

    response = loop.run_until_complete(scenario())

    assert response.status_code == 401
    if hasattr(app.state, "database"):
        del app.state.database


# ----------------------------------------------------------------------
# Health stays open
# ----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/health", "/health/deep", "/"])
def test_health_endpoints_need_no_token(
    loop, unauthenticated_client: AsyncClient, path: str
) -> None:
    async def scenario():
        async with unauthenticated_client as client:
            return await client.get(path)

    response = loop.run_until_complete(scenario())

    assert response.status_code == 200
    assert "WWW-Authenticate" not in response.headers


# ----------------------------------------------------------------------
# Tenancy, against a real database
# ----------------------------------------------------------------------


@pytest.mark.postgres
def test_two_subjects_cannot_see_each_others_inventory(db: Database) -> None:
    """The property the whole design exists to guarantee.

    Two users, two tokens, one deployment. Each writes a purchase and reads
    the kitchen back; neither sees a trace of the other.
    """

    async def scenario(session: AsyncSession) -> None:
        product_id = await make_product(session)

        async with api(session, subject="alice") as alice:
            created = await alice.post(
                "/inventory/events",
                json=event_body(product_id, quantity_delta="3"),
            )
            assert created.status_code == 201, created.text

        async with api(session, subject="bob") as bob:
            created = await bob.post(
                "/inventory/events",
                json=event_body(product_id, quantity_delta="7"),
            )
            assert created.status_code == 201, created.text
            bobs_kitchen = await bob.get("/inventory")

        async with api(session, subject="alice") as alice:
            alices_kitchen = await alice.get("/inventory")

        alices = alices_kitchen.json()
        bobs = bobs_kitchen.json()

        assert alices["household_id"] != bobs["household_id"]
        assert alices["lots"][0]["quantity"] == "3.000"
        assert bobs["lots"][0]["quantity"] == "7.000"

    db.run(scenario)


@pytest.mark.postgres
def test_a_household_row_is_created_on_first_sight_of_a_subject(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        from kitchensense.domain.household import household_id_for
        from kitchensense.repositories import HouseholdRepository

        households = HouseholdRepository(session)
        expected = household_id_for(issuer=identity.ISSUER, subject="newcomer")

        assert await households.exists(household_id=expected) is False

        async with api(session, subject="newcomer") as client:
            response = await client.get("/inventory")

        assert response.status_code == 200
        assert response.json()["household_id"] == str(expected)
        assert await households.exists(household_id=expected) is True

    db.run(scenario)


@pytest.mark.postgres
def test_the_household_is_created_once_not_on_every_request(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        from kitchensense.repositories import HouseholdRepository

        async with api(session, subject="repeat-visitor") as client:
            for _ in range(3):
                assert (await client.get("/inventory")).status_code == 200

        household = await HouseholdRepository(session).get(
            household_id=identity.household_for("repeat-visitor")
        )
        assert household is not None
        assert household.name == "Test Person"

    db.run(scenario)
