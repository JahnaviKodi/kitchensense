"""``inventory_snapshot`` — a derived projection, safe to drop and rebuild.

One row per *lot*: a product in a place, in a unit, under a printed date. Every
column is a fold of ``inventory_events`` and holds no information the log does
not. If this table and the log ever disagree, the log wins.

``as_of`` is repeated on every row rather than kept in a separate watermark
table. It is the same value across a household's rows by construction — the
projection advances atomically — and carrying it here keeps the schema at the
four tables the kitchen record actually needs.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from kitchensense.db.base import Base
from kitchensense.domain.inventory import (
    DateLabelType,
    LotKey,
    LotState,
    StorageLocation,
)
from kitchensense.models.inventory_event import (
    DATE_LABEL_TYPE_ENUM,
    STORAGE_LOCATION_ENUM,
)


class InventorySnapshotRow(Base):
    __tablename__ = "inventory_snapshot"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    household_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("households.id", ondelete="CASCADE")
    )

    # --- the lot key ---
    canonical_product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("canonical_products.id", ondelete="RESTRICT")
    )
    storage_location: Mapped[StorageLocation] = mapped_column(STORAGE_LOCATION_ENUM)
    unit: Mapped[str] = mapped_column(String(24))
    printed_date: Mapped[date | None] = mapped_column(Date)
    date_label_type: Mapped[DateLabelType | None] = mapped_column(DATE_LABEL_TYPE_ENUM)

    # --- the fold ---
    quantity: Mapped[Decimal] = mapped_column()
    purchased_quantity: Mapped[Decimal] = mapped_column()
    consumed_quantity: Mapped[Decimal] = mapped_column()
    discarded_quantity: Mapped[Decimal] = mapped_column()
    event_count: Mapped[int] = mapped_column(Integer)
    first_occurred_at: Mapped[datetime] = mapped_column()
    last_occurred_at: Mapped[datetime] = mapped_column()
    last_recorded_at: Mapped[datetime] = mapped_column()
    opened_at: Mapped[datetime | None] = mapped_column(default=None)

    # --- provenance of the projection itself ---
    as_of: Mapped[datetime] = mapped_column()
    computed_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        # printed_date and date_label_type are nullable and NULL is part of the
        # key — an unlabelled bag of carrots is one lot, not a new lot per
        # event — so the uniqueness has to treat NULLs as equal.
        UniqueConstraint(
            "household_id",
            "canonical_product_id",
            "storage_location",
            "unit",
            "printed_date",
            "date_label_type",
            name="uq_inventory_snapshot_lot",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("event_count > 0", name="event_count_positive"),
        CheckConstraint(
            "date_label_type IS NULL OR printed_date IS NOT NULL",
            name="label_type_needs_printed_date",
        ),
        Index("ix_inventory_snapshot_household_id_as_of", "household_id", "as_of"),
        # "What is about to go off?" — the query the whole product exists for.
        Index(
            "ix_inventory_snapshot_household_id_printed_date",
            "household_id",
            "printed_date",
            postgresql_where=text("quantity > 0"),
        ),
    )

    def to_domain(self) -> LotState:
        return LotState(
            key=LotKey(
                canonical_product_id=self.canonical_product_id,
                storage_location=self.storage_location,
                unit=self.unit,
                printed_date=self.printed_date,
                date_label_type=self.date_label_type,
            ),
            quantity=self.quantity,
            purchased_quantity=self.purchased_quantity,
            consumed_quantity=self.consumed_quantity,
            discarded_quantity=self.discarded_quantity,
            event_count=self.event_count,
            first_occurred_at=self.first_occurred_at,
            last_occurred_at=self.last_occurred_at,
            last_recorded_at=self.last_recorded_at,
            opened_at=self.opened_at,
        )

    def __repr__(self) -> str:
        return (
            f"<InventorySnapshotRow {self.canonical_product_id} "
            f"{self.quantity}{self.unit} @{self.storage_location}>"
        )
