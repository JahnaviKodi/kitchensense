"""A fake user delegation key, so no test reaches a storage account.

The mock is placed at exactly one seam: the call that asks Azure for a
delegation key. Everything downstream of it is the real SDK — the same
``generate_blob_sas`` that runs in production, over the same string-to-sign,
producing a real HMAC. Signing needs no network, only a key, and the key is
the one thing here that is invented.

That matters for what the tests can claim. Asserting that a SAS grants write
and expires in five minutes is only worth doing if the token was built the way
a real one is; a stubbed-out ``upload_ticket`` returning a hand-written string
would assert the fixture, not the code.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

from azure.storage.blob import UserDelegationKey

from kitchensense.config import Settings
from kitchensense.storage import ReceiptBlobStore

__all__ = ["DelegationKeyStub", "store"]

# A key-shaped value that opens nothing. Storage would reject any SAS signed
# with it; no test presents one to storage.
FAKE_KEY_MATERIAL = base64.b64encode(b"not-a-real-delegation-key").decode()

SIGNED_OID = "00000000-0000-4000-8000-000000000001"
SIGNED_TID = "00000000-0000-4000-8000-000000000002"

# The service version the key claims to be issued under. Only ever echoed back
# into the token as ``skv``.
SIGNED_VERSION = "2024-11-04"


def _timestamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class DelegationKeyStub:
    """Stands in for the one network call, and remembers being asked.

    The call count is what lets a test tell "the key was cached" from "a key
    was fetched again", which is the only externally visible behaviour the
    cache has.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[datetime, datetime]] = []
        self.error: Exception | None = None

    def __call__(self, start: datetime, expiry: datetime) -> UserDelegationKey:
        self.calls.append((start, expiry))
        if self.error is not None:
            raise self.error

        key = UserDelegationKey()
        key.signed_oid = SIGNED_OID
        key.signed_tid = SIGNED_TID
        key.signed_start = _timestamp(start)
        key.signed_expiry = _timestamp(expiry)
        key.signed_service = "b"
        key.signed_version = SIGNED_VERSION
        key.value = FAKE_KEY_MATERIAL
        return key


def store(
    settings: Settings, *, keys: DelegationKeyStub | None = None
) -> tuple[ReceiptBlobStore, DelegationKeyStub]:
    """A blob store that signs real tokens and makes no real calls."""
    source = keys if keys is not None else DelegationKeyStub()
    return ReceiptBlobStore(settings, delegation_key_source=source), source
