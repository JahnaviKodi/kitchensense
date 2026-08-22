"""What the upload URL actually grants.

No database and no Docker: these run everywhere, which is deliberate, because
what they check is the security property the whole feature rests on. A SAS
that quietly granted read, or lasted an hour, or could be aimed at a name a
caller supplied, would be a hole that no amount of endpoint testing above it
would find.

The delegation key is faked — see ``tests/storage.py`` — and nothing else is.
The tokens asserted on here are signed by the real SDK.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from kitchensense.domain.receipts import ReceiptContentType, receipt_blob_name
from kitchensense.storage import StorageUnavailableError
from tests import identity, storage

HOUSEHOLD = uuid.UUID("3f6b8a1c-1d2e-4a5b-8c9d-0e1f2a3b4c5d")
OTHER_HOUSEHOLD = uuid.UUID("9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d")

NOW = datetime(2026, 3, 5, 18, 30, tzinfo=UTC)


def query_of(url: str) -> dict[str, str]:
    """The SAS parameters, one value each."""
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


async def ticket_for(
    *, household_id: uuid.UUID = HOUSEHOLD, requested_at: datetime = NOW, **overrides
):
    settings = identity.settings(**overrides)
    blob_store, keys = storage.store(settings)
    ticket = await blob_store.upload_ticket(
        household_id=household_id,
        upload_id=uuid.uuid4(),
        requested_at=requested_at,
    )
    return ticket, keys


# ----------------------------------------------------------------------
# What the token permits
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_sas_permits_write_and_nothing_else() -> None:
    """``sp`` is the permission string, and it must be exactly ``w``.

    Read would let whoever holds the URL fetch the receipt back; list would
    expose the container, which holds every household's; delete would let them
    remove one. The client uploading a photograph it already has needs none of
    those.
    """
    ticket, _ = await ticket_for()

    assert query_of(ticket.url)["sp"] == "w"


@pytest.mark.asyncio
async def test_the_sas_expires_within_five_minutes() -> None:
    ticket, _ = await ticket_for()

    expiry = datetime.strptime(
        query_of(ticket.url)["se"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC)

    assert expiry - NOW == timedelta(minutes=5)
    assert expiry <= NOW + timedelta(minutes=5)
    # And the value handed to the caller agrees with the token itself, so a
    # client that trusts `expires_at` is not misled about when the URL dies.
    assert ticket.expires_at == expiry


@pytest.mark.asyncio
async def test_the_sas_names_one_blob_and_requires_https() -> None:
    ticket, _ = await ticket_for()
    query = query_of(ticket.url)

    # sr=b: scoped to a single blob. A container-scoped token (sr=c) with the
    # same permission would let the holder write anywhere in the container.
    assert query["sr"] == "b"
    assert query["spr"] == "https"
    assert ticket.url.startswith("https://")


@pytest.mark.asyncio
async def test_the_sas_is_signed_by_a_delegation_key_not_an_account_key() -> None:
    """``skoid`` and friends only appear on a user delegation SAS.

    An account-key SAS carries no ``sk*`` parameters at all, so their presence
    is the observable difference between the two — and the reason this API
    never needs a storage account key.
    """
    ticket, keys = await ticket_for()
    query = query_of(ticket.url)

    assert query["skoid"] == storage.SIGNED_OID
    assert query["sktid"] == storage.SIGNED_TID
    assert query["sks"] == "b"
    assert len(keys.calls) == 1


@pytest.mark.asyncio
async def test_the_sas_starts_slightly_in_the_past() -> None:
    """Clock skew: storage rejects a start time in its own future."""
    ticket, _ = await ticket_for()

    start = datetime.strptime(
        query_of(ticket.url)["st"], "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC)

    assert start < NOW


# ----------------------------------------------------------------------
# Where it points
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_blob_name_is_derived_from_the_household_and_the_id() -> None:
    """There is no argument through which a caller could supply a name."""
    settings = identity.settings()
    blob_store, _ = storage.store(settings)
    upload_id = uuid.uuid4()

    ticket = await blob_store.upload_ticket(
        household_id=HOUSEHOLD, upload_id=upload_id, requested_at=NOW
    )

    assert ticket.blob_name == receipt_blob_name(
        household_id=HOUSEHOLD, upload_id=upload_id, requested_at=NOW
    )
    assert ticket.blob_name == f"{HOUSEHOLD}/2026/03/05/{upload_id}"


@pytest.mark.asyncio
async def test_two_requests_never_land_on_the_same_blob() -> None:
    """So one upload cannot overwrite another, even from the same household."""
    settings = identity.settings()
    blob_store, _ = storage.store(settings)

    names = set()
    for _ in range(5):
        ticket = await blob_store.upload_ticket(
            household_id=HOUSEHOLD, upload_id=uuid.uuid4(), requested_at=NOW
        )
        names.add(ticket.blob_name)

    assert len(names) == 5


@pytest.mark.asyncio
async def test_each_households_receipts_sit_under_its_own_prefix() -> None:
    settings = identity.settings()
    blob_store, _ = storage.store(settings)

    mine = await blob_store.upload_ticket(
        household_id=HOUSEHOLD, upload_id=uuid.uuid4(), requested_at=NOW
    )
    theirs = await blob_store.upload_ticket(
        household_id=OTHER_HOUSEHOLD, upload_id=uuid.uuid4(), requested_at=NOW
    )

    assert mine.blob_name.startswith(f"{HOUSEHOLD}/")
    assert theirs.blob_name.startswith(f"{OTHER_HOUSEHOLD}/")


@pytest.mark.asyncio
async def test_the_url_points_at_the_configured_account_and_container() -> None:
    ticket, _ = await ticket_for()

    assert ticket.url.startswith(
        f"{identity.BLOB_ENDPOINT}/{identity.RECEIPTS_CONTAINER}/{HOUSEHOLD}/"
    )


@pytest.mark.asyncio
async def test_the_client_is_told_the_header_azure_insists_on() -> None:
    settings = identity.settings()
    blob_store, _ = storage.store(settings)

    ticket = await blob_store.upload_ticket(
        household_id=HOUSEHOLD,
        upload_id=uuid.uuid4(),
        requested_at=NOW,
        content_type=ReceiptContentType.PNG,
    )

    assert ticket.method == "PUT"
    assert ticket.headers["x-ms-blob-type"] == "BlockBlob"
    assert ticket.headers["Content-Type"] == "image/png"


# ----------------------------------------------------------------------
# The delegation key
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_delegation_key_is_fetched_once_and_reused() -> None:
    """Signing is local, so the key is the only round trip in this path."""
    settings = identity.settings()
    blob_store, keys = storage.store(settings)

    for _ in range(3):
        await blob_store.upload_ticket(
            household_id=HOUSEHOLD, upload_id=uuid.uuid4(), requested_at=NOW
        )

    assert len(keys.calls) == 1


@pytest.mark.asyncio
async def test_the_key_is_retired_before_it_expires() -> None:
    """A URL must never outlive the key that signed it.

    The key is refetched a full SAS lifetime before its own expiry, so the
    last token signed from it still dies first. Here the key lasts ten
    minutes and the SAS five, so a request at the eleventh minute — past the
    point where a five-minute URL would survive it — gets a fresh key.
    """
    settings = identity.settings(
        delegation_key_ttl_seconds=600.0, upload_sas_ttl_seconds=300.0
    )
    blob_store, keys = storage.store(settings)

    await blob_store.upload_ticket(
        household_id=HOUSEHOLD, upload_id=uuid.uuid4(), requested_at=NOW
    )
    # Four minutes on: the key is good until minute five, so it is reused.
    await blob_store.upload_ticket(
        household_id=HOUSEHOLD,
        upload_id=uuid.uuid4(),
        requested_at=NOW + timedelta(minutes=4),
    )
    assert len(keys.calls) == 1

    # Six minutes on: a five-minute URL would now outlive the key, so a new
    # one is fetched instead.
    await blob_store.upload_ticket(
        household_id=HOUSEHOLD,
        upload_id=uuid.uuid4(),
        requested_at=NOW + timedelta(minutes=6),
    )
    assert len(keys.calls) == 2


@pytest.mark.asyncio
async def test_a_key_that_cannot_be_fetched_is_reported_as_unavailable() -> None:
    """The usual cause is a role assignment that has not propagated yet."""
    settings = identity.settings()
    keys = storage.DelegationKeyStub()
    keys.error = RuntimeError("AuthorizationPermissionMismatch")
    blob_store, _ = storage.store(settings, keys=keys)

    with pytest.raises(StorageUnavailableError) as refused:
        await blob_store.upload_ticket(
            household_id=HOUSEHOLD, upload_id=uuid.uuid4(), requested_at=NOW
        )

    assert identity.STORAGE_ACCOUNT in str(refused.value)


# ----------------------------------------------------------------------
# Not configured
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unconfigured_deployment_signs_nothing() -> None:
    """No account name means no URL, rather than one aimed at nowhere.

    ``https://.blob.core.windows.net`` is not this account, and a URL built
    from a blank name is a request pointed at whatever DNS makes of it.
    """
    settings = identity.settings(storage_account_name="", storage_blob_endpoint="")
    blob_store, keys = storage.store(settings)

    with pytest.raises(StorageUnavailableError):
        await blob_store.upload_ticket(
            household_id=HOUSEHOLD, upload_id=uuid.uuid4(), requested_at=NOW
        )

    # And it refused before asking Azure for anything.
    assert keys.calls == []
