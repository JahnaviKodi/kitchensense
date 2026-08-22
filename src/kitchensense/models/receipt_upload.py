"""``receipt_uploads`` — one row per upload URL the API has handed out.

The row is written when the URL is issued, not when the file arrives, and that
ordering is the point of the table. It is what lets a later ``confirm`` be
matched to a request that actually happened, rather than taken on the client's
word; and it is what makes an upload that was promised and never delivered a
row you can find — ``confirmed_at IS NULL`` and past its expiry — instead of a
blob nobody knows about and nobody is looking for.

Unlike ``inventory_events`` this is not a system of record: it tracks a
transfer in flight, and a row here is not a belief about anyone's kitchen. So
it is an ordinary mutable table, with exactly one thing that changes —
``confirmed_at``, once, from NULL.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from kitchensense.db.base import Base
from kitchensense.domain.receipts import ReceiptUpload


class ReceiptUploadRow(Base):
    __tablename__ = "receipt_uploads"

    # Supplied by the API rather than defaulted here: the id is generated
    # before the row exists, because the blob name is derived from it and the
    # SAS has to be signed before there is anything worth committing.
    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    household_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("households.id", ondelete="RESTRICT")
    )

    # Recorded, not accepted: see kitchensense.domain.receipts. Unique, so the
    # same blob cannot be claimed by two rows even if a future caller found a
    # way to ask for one twice.
    blob_name: Mapped[str] = mapped_column(String(400))
    content_type: Mapped[str] = mapped_column(String(128))

    # Stamped by the application, not by ``now()`` — the one place in this
    # schema that is true. It has to be: this is the same instant the SAS was
    # signed from, and a row whose ``requested_at`` came from a different
    # clock than its ``expires_at`` would disagree with the token it
    # describes. It is also what keeps the two constraints below from firing
    # on ordinary drift between the container and the database server.
    requested_at: Mapped[datetime] = mapped_column()
    # When the URL issued alongside this row stops working. Kept so an
    # abandoned upload can be told from one that is merely still in progress,
    # without the reader having to know what the SAS lifetime was on the day
    # it was signed.
    expires_at: Mapped[datetime] = mapped_column()
    confirmed_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        UniqueConstraint("blob_name", name="uq_receipt_uploads_blob_name"),
        CheckConstraint("length(btrim(blob_name)) > 0", name="blob_name_not_blank"),
        CheckConstraint("length(btrim(content_type)) > 0", name="content_type_not_blank"),
        CheckConstraint("expires_at > requested_at", name="expiry_after_request"),
        # Both timestamps are the application's, taken from the same clock in
        # two separate requests, so a confirmation before its own request is
        # not a race — it is a bug, and this is where it would surface.
        CheckConstraint(
            "confirmed_at IS NULL OR confirmed_at >= requested_at",
            name="confirmation_after_request",
        ),
        Index(
            "ix_receipt_uploads_household_id_requested_at",
            "household_id",
            "requested_at",
        ),
        # Serves the "what never arrived" query, and only that. Partial,
        # because a confirmed upload is finished business and there will be
        # far more of those than of the other kind.
        Index(
            "ix_receipt_uploads_unconfirmed",
            "household_id",
            "expires_at",
            postgresql_where=text("confirmed_at IS NULL"),
        ),
    )

    def to_domain(self) -> ReceiptUpload:
        """Detach the row into the immutable type the API layer sees."""
        return ReceiptUpload(
            id=self.id,
            household_id=self.household_id,
            blob_name=self.blob_name,
            content_type=self.content_type,
            requested_at=self.requested_at,
            expires_at=self.expires_at,
            confirmed_at=self.confirmed_at,
        )

    def __repr__(self) -> str:
        state = "confirmed" if self.confirmed_at else "awaiting upload"
        return f"<ReceiptUploadRow {self.id} {state}>"
