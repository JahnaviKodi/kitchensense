"""Reading and writing the record of receipt uploads.

Same rule as everywhere else in this package: ``household_id`` is a required
keyword argument, every statement is built by :meth:`_scoped`, and there is no
method that can be called without saying whose data it is for. It matters
more here than usual — :meth:`ReceiptUploadRepository.confirm` is reachable
with an id a caller supplies, so the household predicate is the only thing
standing between one kitchen and another's upload.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from kitchensense.domain.receipts import ReceiptUpload
from kitchensense.models.receipt_upload import ReceiptUploadRow

__all__ = ["ReceiptUploadRepository"]


class ReceiptUploadRepository:
    """Access to ``receipt_uploads``, scoped to one household."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def _scoped(self, *, household_id: uuid.UUID) -> Select[tuple[ReceiptUploadRow]]:
        """The only ``select`` of uploads in this module."""
        return select(ReceiptUploadRow).where(
            ReceiptUploadRow.household_id == household_id
        )

    async def _row(
        self, *, household_id: uuid.UUID, upload_id: uuid.UUID
    ) -> ReceiptUploadRow | None:
        result = await self._session.execute(
            self._scoped(household_id=household_id).where(
                ReceiptUploadRow.id == upload_id
            )
        )
        return result.scalars().one_or_none()

    async def record_request(
        self,
        *,
        household_id: uuid.UUID,
        upload_id: uuid.UUID,
        blob_name: str,
        content_type: str,
        requested_at: datetime,
        expires_at: datetime,
    ) -> ReceiptUpload:
        """Note that a URL for ``blob_name`` has been handed out.

        Flushed but not committed. The caller decides when the request is
        finished, which lets a failure between here and the response leave no
        row behind rather than an upload nobody will ever deliver.
        """
        row = ReceiptUploadRow(
            id=upload_id,
            household_id=household_id,
            blob_name=blob_name,
            content_type=content_type,
            requested_at=requested_at,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.flush()
        return row.to_domain()

    async def get(
        self, *, household_id: uuid.UUID, upload_id: uuid.UUID
    ) -> ReceiptUpload | None:
        row = await self._row(household_id=household_id, upload_id=upload_id)
        return None if row is None else row.to_domain()

    async def confirm(
        self, *, household_id: uuid.UUID, upload_id: uuid.UUID, at: datetime
    ) -> ReceiptUpload | None:
        """Mark an upload as delivered. ``None`` if this household has no such row.

        ``None`` covers both "no upload was ever requested with that id" and
        "one was, by somebody else". Deliberately the same answer: telling
        those apart would confirm to a caller that an id they guessed belongs
        to a real upload in another household, which is a fact they have no
        business learning.

        Confirming twice is not an error. The second call returns the row with
        the timestamp the first one set, so a client retrying a dropped
        response gets the same answer rather than a refusal.
        """
        row = await self._row(household_id=household_id, upload_id=upload_id)
        if row is None:
            return None

        if row.confirmed_at is None:
            row.confirmed_at = at
            await self._session.flush()

        return row.to_domain()

    async def unconfirmed(
        self, *, household_id: uuid.UUID, as_of: datetime
    ) -> Sequence[ReceiptUpload]:
        """Uploads whose URL has expired with no confirmation.

        What a later sweep would read: each of these is either a blob that
        arrived and was never acknowledged, or a URL that was issued and never
        used. Nothing acts on them yet — the row is kept from the start so
        that when something does, the evidence is there rather than
        reconstructed from a container listing.
        """
        result = await self._session.execute(
            self._scoped(household_id=household_id)
            .where(ReceiptUploadRow.confirmed_at.is_(None))
            .where(ReceiptUploadRow.expires_at < as_of)
            .order_by(ReceiptUploadRow.requested_at)
        )
        return [row.to_domain() for row in result.scalars().all()]
