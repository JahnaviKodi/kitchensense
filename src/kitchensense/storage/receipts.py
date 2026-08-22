"""Handing a client a five-minute permission to write exactly one blob.

The permission is a **user delegation SAS**. That choice is the substance of
this module, so it is worth being explicit about what it is not: the usual way
to sign a SAS is with one of the storage account's two access keys, and an
account key is a permanent, unscoped, unrevocable credential for everything in
the account. Reading one into the application would mean the API held a secret
that opens every container, and that a leak of it could only be answered by
rotating the key and breaking every other holder at the same time.

A user delegation key is signed by Entra instead. It is obtained with the
container app's managed identity, it expires on its own, it can grant no more
than the identity itself holds, and revoking the identity's role assignment
invalidates every SAS derived from it. Nothing here reads an account key —
the account is deployed with key access switched off entirely, so nothing
*could*.

Two boundaries are worth noticing while reading:

* The only network call is fetching the delegation key. Signing is local
  HMAC — no request is made when a URL is minted, and the URL is valid the
  instant it is returned.
* :meth:`ReceiptBlobStore.upload_ticket` takes no blob name. It derives one,
  from the household and an id its caller generated. There is no parameter
  through which a client's input could reach the name.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

# Imported at module scope, unlike the Key Vault client in ``db/provider``.
# ``UserDelegationKey`` is part of this module's own interface — it is what a
# test injects in place of the fetch — so deferring the import would only move
# the cost, not avoid it. ``generate_blob_sas`` does no I/O: it is HMAC over a
# string, and it runs offline.
from azure.storage.blob import (
    BlobSasPermissions,
    UserDelegationKey,
    generate_blob_sas,
)

from kitchensense.config import Settings
from kitchensense.domain.receipts import ReceiptContentType, receipt_blob_name

__all__ = [
    "DelegationKeySource",
    "ReceiptBlobStore",
    "StorageUnavailableError",
    "UploadTicket",
]

logger = logging.getLogger(__name__)

# The header Azure requires on a PUT that creates a block blob. Returned to
# the client rather than left for it to discover, because without it the
# upload fails with a 400 that says nothing useful.
BLOB_TYPE_HEADER = {"x-ms-blob-type": "BlockBlob"}


class StorageUnavailableError(RuntimeError):
    """No upload URL could be issued.

    Either the deployment has no storage account configured, or the delegation
    key could not be fetched. Both are the server's problem, not the caller's,
    and both come back as a 503.
    """


@dataclass(frozen=True, slots=True)
class UploadTicket:
    """Everything a client needs to upload one receipt, and nothing more."""

    blob_name: str
    url: str
    expires_at: datetime
    content_type: ReceiptContentType
    method: str = "PUT"

    @property
    def headers(self) -> dict[str, str]:
        return {**BLOB_TYPE_HEADER, "Content-Type": str(self.content_type)}


# Given a start and an expiry, produce a delegation key. The seam the tests
# replace: everything downstream of it is the SDK's real signing code.
DelegationKeySource = Callable[[datetime, datetime], UserDelegationKey]


class ReceiptBlobStore:
    """Issues short-lived, single-blob, write-only upload URLs.

    Safe to construct at import time and safe to construct without an account
    configured: ``__init__`` does no I/O and does not validate anything. The
    check happens in :meth:`require_configured`, so an unconfigured deployment
    starts and serves everything else, exactly as it does with no tenant or no
    database.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        delegation_key_source: DelegationKeySource | None = None,
    ) -> None:
        self._settings = settings
        self._fetch_key = delegation_key_source or self._fetch_key_from_azure
        self._lock = asyncio.Lock()
        self._key: UserDelegationKey | None = None
        # When the cached key stops being usable for a *new* SAS — earlier
        # than the key's own expiry, by the SAS lifetime, so a URL signed at
        # the last moment cannot outlive the key that signed it.
        self._key_usable_until = datetime.min.replace(tzinfo=UTC)

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------

    @property
    def is_configured(self) -> bool:
        return bool(
            self._settings.storage_account_name
            and self._settings.storage_blob_endpoint
            and self._settings.receipts_container
        )

    def require_configured(self) -> None:
        """Raise unless this deployment can actually issue an upload URL."""
        if not self.is_configured:
            raise StorageUnavailableError(
                "no storage account is configured; set STORAGE_ACCOUNT_NAME to "
                "the account infra/main.bicep deploys"
            )

    # ------------------------------------------------------------------
    # issuing a ticket
    # ------------------------------------------------------------------

    async def upload_ticket(
        self,
        *,
        household_id: uuid.UUID,
        upload_id: uuid.UUID,
        requested_at: datetime,
        content_type: ReceiptContentType = ReceiptContentType.JPEG,
    ) -> UploadTicket:
        """A write-only URL for one blob, expiring in ``upload_sas_ttl_seconds``.

        Note the argument list: there is no blob name in it. The name is
        derived from ``household_id`` — which comes from the validated token,
        never from the request — and ``upload_id``, which the caller
        generates. A client has nothing to say about where its file lands.

        Raises:
            StorageUnavailableError: nothing is configured, or the delegation
                key could not be obtained.
        """
        self.require_configured()

        settings = self._settings
        skew = timedelta(seconds=settings.storage_clock_skew_seconds)
        expires_at = requested_at + timedelta(seconds=settings.upload_sas_ttl_seconds)

        key = await self._delegation_key(now=requested_at)

        blob_name = receipt_blob_name(
            household_id=household_id,
            upload_id=upload_id,
            requested_at=requested_at,
        )

        # Write, and only write. Not read: the client that uploads a receipt
        # has the file already and never needs to fetch it back. Not delete,
        # not list — a SAS that could list would expose the container's other
        # blobs, which is every other household's receipts.
        token = generate_blob_sas(
            account_name=settings.storage_account_name,
            container_name=settings.receipts_container,
            blob_name=blob_name,
            user_delegation_key=key,
            permission=BlobSasPermissions(write=True),
            # Backdated by the skew allowance. Storage compares this against
            # its own clock, and a start time a few seconds in its future
            # makes the URL fail for as long as the drift lasts.
            start=requested_at - skew,
            expiry=expires_at,
            # HTTPS only. The token is a bearer credential and there is no
            # reason for one to travel in the clear.
            protocol="https",
        )

        return UploadTicket(
            blob_name=blob_name,
            url=f"{self.blob_url(blob_name)}?{token}",
            expires_at=expires_at,
            content_type=content_type,
        )

    def blob_url(self, blob_name: str) -> str:
        """The blob's address, without any credential attached.

        ``safe="/"`` keeps the prefix separators as separators. Nothing else
        in the name needs escaping — it is UUIDs and digits — but quoting it
        anyway means this stays correct if the naming rule ever changes.
        """
        container = self._settings.receipts_container
        endpoint = self._settings.storage_blob_endpoint.rstrip("/")
        return f"{endpoint}/{container}/{quote(blob_name, safe='/')}"

    # ------------------------------------------------------------------
    # the delegation key
    # ------------------------------------------------------------------

    async def _delegation_key(self, *, now: datetime) -> UserDelegationKey:
        """The cached delegation key, fetching one if there is none to use.

        Cached because this is the only round trip in the upload path and the
        key outlives an individual URL many times over. Guarded by a lock for
        the same reason the database's engine is: several uploads can arrive
        together on a cold replica, and only one of them should go and ask.
        """
        if self._key is not None and now < self._key_usable_until:
            return self._key

        async with self._lock:
            # Checked again inside the lock: whoever held it may have just
            # fetched the key this caller was about to ask for.
            if self._key is not None and now < self._key_usable_until:
                return self._key

            settings = self._settings
            skew = timedelta(seconds=settings.storage_clock_skew_seconds)
            start = now - skew
            expiry = now + timedelta(seconds=settings.delegation_key_ttl_seconds)

            try:
                key = await asyncio.wait_for(
                    asyncio.to_thread(self._fetch_key, start, expiry),
                    timeout=settings.storage_timeout_seconds,
                )
            except TimeoutError as exc:
                raise StorageUnavailableError(
                    "timed out asking "
                    f"{settings.storage_account_name} for a user delegation key"
                ) from exc
            except Exception as exc:
                # Usually a role assignment that has not propagated yet. The
                # SDK's message names the account and the missing permission,
                # and it carries no credential, so it is safe to keep.
                raise StorageUnavailableError(
                    f"could not obtain a user delegation key for "
                    f"{settings.storage_account_name}: {exc}"
                ) from exc

            self._key = key
            # Retire the key a full SAS lifetime before it actually expires,
            # so nothing signed from it can outlive it.
            self._key_usable_until = expiry - timedelta(
                seconds=settings.upload_sas_ttl_seconds
            )
            logger.info(
                "Obtained a user delegation key for %s, usable until %s",
                settings.storage_account_name,
                self._key_usable_until.isoformat(),
            )
            return key

    def _fetch_key_from_azure(
        self, start: datetime, expiry: datetime
    ) -> UserDelegationKey:
        """Ask storage for a delegation key. Runs in a worker thread.

        The synchronous SDK, for the same reason the Key Vault lookup uses it:
        this happens once an hour, and an async transport buys nothing at that
        frequency.

        ``azure.identity`` is imported here rather than at module scope so the
        credential's lifetime is visibly bounded by this function, and so a
        test or a local run that never reaches this line never constructs one.
        """
        from azure.identity import ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient

        # Required, as with Key Vault: the app's identity is user-assigned,
        # and a credential given no client id looks for a system-assigned one
        # that does not exist.
        credential = ManagedIdentityCredential(
            client_id=self._settings.managed_identity_client_id
        )
        try:
            client = BlobServiceClient(
                account_url=self._settings.storage_blob_endpoint,
                credential=credential,
            )
            try:
                return client.get_user_delegation_key(
                    key_start_time=start, key_expiry_time=expiry
                )
            finally:
                client.close()
        finally:
            credential.close()
