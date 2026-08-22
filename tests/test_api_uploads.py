"""The two upload endpoints, end to end.

Same arrangement as ``test_api_inventory``: the real app, the real router, the
real token validation, a real Postgres behind a transaction that is rolled
back. The one addition is the blob store, which is given a fake delegation key
so the SAS is signed locally and no request ever leaves the process.

The tests that need rows are marked ``postgres`` and skip without Docker. The
one that does not — an unauthenticated request, refused before anything is
read — runs everywhere, which is where it belongs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kitchensense.api.dependencies import get_receipt_store, get_session
from kitchensense.api.security import get_verifier
from kitchensense.auth.verifier import TokenVerifier
from kitchensense.main import app
from kitchensense.repositories import ReceiptUploadRepository
from tests import identity, storage
from tests.conftest import Database

DEFAULT_SUBJECT = "upload-tests"
OTHER_SUBJECT = "someone-else"


@asynccontextmanager
async def api(
    session: AsyncSession, *, subject: str = DEFAULT_SUBJECT
) -> AsyncIterator[AsyncClient]:
    """The real app, wired to the test's transaction, a fake tenant, a fake key."""

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield session

    stub = identity.TenantStub()
    verifier = TokenVerifier(identity.settings(), transport=stub.transport)
    blob_store, _ = storage.store(identity.settings())

    app.dependency_overrides[get_session] = _session_override
    app.dependency_overrides[get_verifier] = lambda: verifier
    app.dependency_overrides[get_receipt_store] = lambda: blob_store
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=identity.bearer(identity.make_token(subject=subject)),
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_verifier, None)
        app.dependency_overrides.pop(get_receipt_store, None)
        await verifier.aclose()


def sas_of(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


# ----------------------------------------------------------------------
# Authentication
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "path"),
    [
        ("asking for an upload URL", "/uploads"),
        (
            "confirming one",
            f"/uploads/{uuid.uuid4()}/confirm",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_an_unauthenticated_request_is_refused(
    loop, description: str, path: str
) -> None:
    """No token, no upload URL — and no database touched to find that out.

    There is no session override here on purpose. If authentication were
    checked after the session dependency, this test would need a Postgres to
    reach its 401, and its passing without one is part of the point.
    """
    stub = identity.TenantStub()
    app.dependency_overrides[get_verifier] = lambda: TokenVerifier(
        identity.settings(), transport=stub.transport
    )

    async def scenario():
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(path, json={})

    try:
        response = loop.run_until_complete(scenario())
    finally:
        app.dependency_overrides.pop(get_verifier, None)

    assert response.status_code == 401
    challenge = response.headers["WWW-Authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'realm="kitchensense"' in challenge


# ----------------------------------------------------------------------
# POST /uploads
# ----------------------------------------------------------------------


@pytest.mark.postgres
def test_requesting_an_upload_returns_a_url_and_records_it(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        async with api(session) as client:
            response = await client.post("/uploads", json={"content_type": "image/png"})

            assert response.status_code == 201, response.text
            body = response.json()

            # The row exists before the response is read, not after the client
            # gets around to confirming.
            recorded = await ReceiptUploadRepository(session).get(
                household_id=identity.household_for(DEFAULT_SUBJECT),
                upload_id=uuid.UUID(body["upload_id"]),
            )

        assert body["household_id"] == str(identity.household_for(DEFAULT_SUBJECT))
        assert body["method"] == "PUT"
        assert body["headers"]["x-ms-blob-type"] == "BlockBlob"
        assert body["headers"]["Content-Type"] == "image/png"

        assert recorded is not None
        assert recorded.blob_name == body["blob_name"]
        assert recorded.content_type == "image/png"
        assert recorded.confirmed_at is None

    db.run(scenario)


@pytest.mark.postgres
def test_the_url_permits_write_only_and_expires_within_five_minutes(
    db: Database,
) -> None:
    """The security property of the whole feature, checked through the API.

    ``tests/test_receipt_blob_store.py`` asserts the same thing directly. It
    is repeated here because what matters is what the *endpoint* hands a
    client, and a handler that quietly passed different permissions to the
    store would satisfy the unit test and fail this one.
    """

    async def scenario(session: AsyncSession) -> None:
        before = datetime.now(UTC)
        async with api(session) as client:
            response = await client.post("/uploads", json={})
        after = datetime.now(UTC)

        assert response.status_code == 201, response.text
        body = response.json()
        query = sas_of(body["upload_url"])

        # Write. Not read, not delete, not list.
        assert query["sp"] == "w"
        # One blob, over HTTPS.
        assert query["sr"] == "b"
        assert query["spr"] == "https"
        # And signed by a delegation key: an account-key SAS carries no sk*
        # parameters at all, so this is the observable difference between the
        # two, and the reason no storage account key exists in this system.
        assert "skoid" in query

        expiry = datetime.strptime(query["se"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        assert expiry <= after + timedelta(minutes=5)
        assert expiry > before

        # The expiry it reports and the expiry the token carries are the same
        # instant, to the second the token records it at.
        reported = datetime.fromisoformat(body["expires_at"])
        assert abs((reported - expiry).total_seconds()) < 1

    db.run(scenario)


@pytest.mark.postgres
@pytest.mark.parametrize(
    ("description", "body"),
    [
        ("a blob name", {"blob_name": "chosen-by-the-client"}),
        ("a filename", {"filename": "receipt.jpg"}),
        ("a path", {"path": "../../someone-else/receipt.jpg"}),
        ("another household", {"household_id": str(uuid.uuid4())}),
        ("an upload id", {"upload_id": str(uuid.uuid4())}),
        ("a prefix", {"prefix": "../"}),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_client_cannot_name_the_blob(
    db: Database, description: str, body: dict[str, str]
) -> None:
    """Every field that might steer the name is refused outright.

    ``extra="forbid"`` on the request model turns each of these into a 422.
    That is stronger than ignoring them: a field that is silently dropped is
    one somebody wires up by accident later, and by then a client is already
    sending it.
    """

    async def scenario(session: AsyncSession) -> None:
        async with api(session) as client:
            response = await client.post("/uploads", json=body)

        assert response.status_code == 422, response.text

    db.run(scenario)


@pytest.mark.postgres
def test_the_blob_name_is_the_servers_regardless_of_what_is_asked_for(
    db: Database,
) -> None:
    """And the positive half: the name that comes back is derived, every time.

    Two requests from the same client, with the only field it is allowed to
    send, land on two different blobs under this household's prefix — so one
    upload cannot be aimed at another's blob even by replaying the request
    that produced it.
    """

    async def scenario(session: AsyncSession) -> None:
        household_id = identity.household_for(DEFAULT_SUBJECT)

        async with api(session) as client:
            first = await client.post("/uploads", json={"content_type": "image/jpeg"})
            second = await client.post("/uploads", json={"content_type": "image/jpeg"})

        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text

        names = [first.json()["blob_name"], second.json()["blob_name"]]
        assert names[0] != names[1]
        for name, response in zip(names, [first, second], strict=True):
            # household/yyyy/mm/dd/upload-id, and nothing the client sent.
            assert name.startswith(f"{household_id}/")
            assert name.endswith(response.json()["upload_id"])
            assert ".." not in name

    db.run(scenario)


@pytest.mark.postgres
def test_an_unknown_content_type_is_refused(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        async with api(session) as client:
            response = await client.post(
                "/uploads", json={"content_type": "application/x-msdownload"}
            )

        assert response.status_code == 422, response.text

    db.run(scenario)


# ----------------------------------------------------------------------
# POST /uploads/{id}/confirm
# ----------------------------------------------------------------------


@pytest.mark.postgres
def test_confirming_an_upload_records_that_it_arrived(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        async with api(session) as client:
            requested = await client.post("/uploads", json={})
            upload_id = requested.json()["upload_id"]

            confirmed = await client.post(f"/uploads/{upload_id}/confirm")

        assert confirmed.status_code == 200, confirmed.text
        body = confirmed.json()
        assert body["upload_id"] == upload_id
        assert body["blob_name"] == requested.json()["blob_name"]
        assert body["confirmed_at"] is not None
        assert (
            datetime.fromisoformat(body["confirmed_at"])
            >= datetime.fromisoformat(body["requested_at"])
        )

    db.run(scenario)


@pytest.mark.postgres
def test_confirming_an_upload_that_was_never_requested_is_refused(
    db: Database,
) -> None:
    """A confirm has to be matched to a request, not taken on the client's word."""

    async def scenario(session: AsyncSession) -> None:
        async with api(session) as client:
            response = await client.post(f"/uploads/{uuid.uuid4()}/confirm")

        assert response.status_code == 404, response.text
        assert "No upload was requested" in response.json()["detail"]

    db.run(scenario)


@pytest.mark.postgres
def test_one_household_cannot_confirm_anothers_upload(db: Database) -> None:
    """The id is the caller's to supply, so the household filter is what protects it.

    A 404 rather than a 403, and the same 404 an invented id gets: telling
    those apart would confirm to the caller that the id they used is a real
    upload belonging to somebody else.
    """

    async def scenario(session: AsyncSession) -> None:
        async with api(session, subject=DEFAULT_SUBJECT) as client:
            mine = await client.post("/uploads", json={})
            upload_id = mine.json()["upload_id"]

        async with api(session, subject=OTHER_SUBJECT) as intruder:
            stolen = await intruder.post(f"/uploads/{upload_id}/confirm")

        assert stolen.status_code == 404, stolen.text
        assert "No upload was requested" in stolen.json()["detail"]

        # And the attempt left the real upload untouched, still awaiting its file.
        untouched = await ReceiptUploadRepository(session).get(
            household_id=identity.household_for(DEFAULT_SUBJECT),
            upload_id=uuid.UUID(upload_id),
        )
        assert untouched is not None
        assert untouched.confirmed_at is None

    db.run(scenario)


def test_the_two_households_really_are_different() -> None:
    """Guards the test above: it proves nothing if both subjects collide."""
    assert identity.household_for(DEFAULT_SUBJECT) != identity.household_for(
        OTHER_SUBJECT
    )


@pytest.mark.postgres
def test_confirming_twice_returns_the_first_confirmation(db: Database) -> None:
    """A client whose response was dropped can retry without being refused."""

    async def scenario(session: AsyncSession) -> None:
        async with api(session) as client:
            upload_id = (await client.post("/uploads", json={})).json()["upload_id"]

            first = await client.post(f"/uploads/{upload_id}/confirm")
            second = await client.post(f"/uploads/{upload_id}/confirm")

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["confirmed_at"] == second.json()["confirmed_at"]

    db.run(scenario)


@pytest.mark.postgres
def test_a_malformed_upload_id_is_rejected_before_anything_is_read(
    db: Database,
) -> None:
    async def scenario(session: AsyncSession) -> None:
        async with api(session) as client:
            response = await client.post("/uploads/not-a-uuid/confirm")

        assert response.status_code == 422, response.text

    db.run(scenario)


# ----------------------------------------------------------------------
# What never arrived
# ----------------------------------------------------------------------


@pytest.mark.postgres
def test_an_upload_that_was_never_confirmed_can_be_found_later(db: Database) -> None:
    """The reason the row is written up front rather than on confirmation.

    One upload is confirmed and one is not. Once both URLs have expired, only
    the unconfirmed one is outstanding — and it is outstanding as a row, not
    as a blob somebody has to go looking for in a container listing.
    """

    async def scenario(session: AsyncSession) -> None:
        household_id = identity.household_for(DEFAULT_SUBJECT)

        async with api(session) as client:
            abandoned = (await client.post("/uploads", json={})).json()
            delivered = (await client.post("/uploads", json={})).json()
            await client.post(f"/uploads/{delivered['upload_id']}/confirm")

        # Ten minutes on: both five-minute URLs are dead.
        outstanding = await ReceiptUploadRepository(session).unconfirmed(
            household_id=household_id, as_of=datetime.now(UTC) + timedelta(minutes=10)
        )

        assert [str(upload.id) for upload in outstanding] == [abandoned["upload_id"]]
        assert outstanding[0].has_expired(
            as_of=datetime.now(UTC) + timedelta(minutes=10)
        )

    db.run(scenario)


@pytest.mark.postgres
def test_one_households_uploads_are_invisible_to_another(db: Database) -> None:
    """Read the same way from two households and only one of them sees the row."""

    async def scenario(session: AsyncSession) -> None:
        async with api(session, subject=DEFAULT_SUBJECT) as client:
            mine = (await client.post("/uploads", json={})).json()

        repository = ReceiptUploadRepository(session)
        later = datetime.now(UTC) + timedelta(minutes=10)

        ours = await repository.unconfirmed(
            household_id=identity.household_for(DEFAULT_SUBJECT), as_of=later
        )
        theirs = await repository.unconfirmed(
            household_id=identity.household_for(OTHER_SUBJECT), as_of=later
        )

        assert [str(upload.id) for upload in ours] == [mine["upload_id"]]
        assert theirs == []

    db.run(scenario)
