"""Where a receipt image is stored, and what it is allowed to be.

Pure, like the rest of ``domain/``, and pure for a specific reason here: the
blob name is a security boundary. A caller who could influence it could aim an
upload at another household's prefix, or at a path that escapes the container
layout entirely. So the name is a function of three things the server already
knows — the household from the token, an id the server generated, and the
server's clock — and there is no parameter it could be smuggled through.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

__all__ = ["ReceiptContentType", "ReceiptUpload", "receipt_blob_name"]


class ReceiptContentType(StrEnum):
    """What a client may say it is about to upload.

    An allow-list rather than free text. It travels no further than the
    recorded row and the ``Content-Type`` header the client is told to send,
    but "what type is this file" is exactly the kind of field that ends up
    interpolated somewhere later, and a closed set costs nothing now.
    """

    JPEG = "image/jpeg"
    PNG = "image/png"
    HEIC = "image/heic"
    WEBP = "image/webp"
    PDF = "application/pdf"


def receipt_blob_name(
    *, household_id: uuid.UUID, upload_id: uuid.UUID, requested_at: datetime
) -> str:
    """The one place a receipt's blob name is decided.

    ``{household}/{yyyy}/{mm}/{dd}/{upload_id}``. The household prefix comes
    first so a household's receipts sit together — which is what makes a
    future per-household deletion a prefix scan rather than a full listing —
    and the date segments keep any single prefix from growing without bound.

    There is no extension. The file's type is recorded in the database, where
    it can be trusted, rather than encoded in a name that something downstream
    might be tempted to dispatch on.

    Both components are UUIDs, so the result is drawn from ``[0-9a-f-]`` and
    the separators this function puts in itself: it cannot contain ``..``, a
    backslash, a query string or anything else that would mean something to a
    URL parser. That is a property of the type, not of a sanitising pass that
    a later refactor could drop.
    """
    return f"{household_id}/{requested_at:%Y/%m/%d}/{upload_id}"


@dataclass(frozen=True, slots=True)
class ReceiptUpload:
    """An upload the API has issued a URL for, detached from its row.

    Immutable, like everything else in ``domain/``. Confirming an upload does
    not mutate one of these — it produces a new one, out of the repository,
    with ``confirmed_at`` set.
    """

    id: uuid.UUID
    household_id: uuid.UUID
    blob_name: str
    content_type: str
    requested_at: datetime
    expires_at: datetime
    confirmed_at: datetime | None = None

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_at is not None

    def has_expired(self, *, as_of: datetime) -> bool:
        """Whether the URL issued with this upload has stopped working.

        Says nothing about whether the file arrived. An expired *and*
        unconfirmed upload is the one worth chasing.
        """
        return as_of >= self.expires_at
