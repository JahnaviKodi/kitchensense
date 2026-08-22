"""receipt_uploads: the record of every upload URL the API has issued

Revision ID: 0004_receipt_uploads
Revises: 0003_drop_placeholder
Create Date: 2026-08-22

One table, no triggers. Unlike ``inventory_events`` this is not a system of
record — it tracks a file transfer in flight, not a belief about a kitchen —
so it is an ordinary mutable table and it drops cleanly on a downgrade. The
blobs themselves are deleted by a storage lifecycle rule after thirty days;
nothing in the database is load-bearing for that.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_receipt_uploads"
down_revision: str | None = "0003_drop_placeholder"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "receipt_uploads",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("household_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Derived server-side from the household and the id above; never
        # accepted from a client. See kitchensense.domain.receipts.
        sa.Column("blob_name", sa.String(length=400), nullable=False),
        sa.Column("content_type", sa.String(length=128), nullable=False),
        # Application-stamped, unlike every other timestamp in this schema.
        # It is the instant the SAS was signed from, so it must come from the
        # same clock as expires_at — see the model.
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # NULL until the client says the file arrived. That is the whole
        # state machine.
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_receipt_uploads"),
        # RESTRICT, matching inventory_events: removing a household has to be
        # a deliberate operation, not something a stray DELETE cascades into.
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name="fk_receipt_uploads_household_id_households",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("blob_name", name="uq_receipt_uploads_blob_name"),
        sa.CheckConstraint("length(btrim(blob_name)) > 0", name="blob_name_not_blank"),
        sa.CheckConstraint(
            "length(btrim(content_type)) > 0", name="content_type_not_blank"
        ),
        sa.CheckConstraint("expires_at > requested_at", name="expiry_after_request"),
        sa.CheckConstraint(
            "confirmed_at IS NULL OR confirmed_at >= requested_at",
            name="confirmation_after_request",
        ),
    )
    op.create_index(
        "ix_receipt_uploads_household_id_requested_at",
        "receipt_uploads",
        ["household_id", "requested_at"],
    )
    # Partial: the interesting rows are the ones that never completed, and a
    # confirmed upload is finished business.
    op.create_index(
        "ix_receipt_uploads_unconfirmed",
        "receipt_uploads",
        ["household_id", "expires_at"],
        postgresql_where=sa.text("confirmed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_receipt_uploads_unconfirmed", "receipt_uploads")
    op.drop_index("ix_receipt_uploads_household_id_requested_at", "receipt_uploads")
    op.drop_table("receipt_uploads")
