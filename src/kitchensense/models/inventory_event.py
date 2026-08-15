"""``inventory_events`` — the append-only kitchen record.

This table is the system of record. Nothing else is. Snapshots, features and
model training sets are all derivable from it, and any of them can be thrown
away and rebuilt. Rows are never updated and never deleted: a mistake is
corrected by appending a ``corrected`` event, so the history of what the
system *believed* stays legible alongside what turned out to be true. Database
triggers enforce that (see the Alembic migration) rather than trusting every
future caller to remember.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from kitchensense.db.base import Base, metadata
from kitchensense.domain.inventory import (
    DateLabelType,
    EventType,
    InventoryEvent,
    StorageLocation,
)


def _values(enum_cls: type[EventType | DateLabelType | StorageLocation]) -> list[str]:
    """Store the enum's *values*, not Python's uppercase member names."""
    return [member.value for member in enum_cls]


# Bound to the metadata so the Postgres types are created once, even though
# two tables use them.
EVENT_TYPE_ENUM = SAEnum(
    EventType,
    name="inventory_event_type",
    metadata=metadata,
    values_callable=lambda enum_cls: _values(EventType),
)
DATE_LABEL_TYPE_ENUM = SAEnum(
    DateLabelType,
    name="date_label_type",
    metadata=metadata,
    values_callable=lambda enum_cls: _values(DateLabelType),
)
STORAGE_LOCATION_ENUM = SAEnum(
    StorageLocation,
    name="storage_location",
    metadata=metadata,
    values_callable=lambda enum_cls: _values(StorageLocation),
)


class InventoryEventRow(Base):
    __tablename__ = "inventory_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    household_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("households.id", ondelete="RESTRICT")
    )
    canonical_product_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("canonical_products.id", ondelete="RESTRICT")
    )

    event_type: Mapped[EventType] = mapped_column(EVENT_TYPE_ENUM)
    # Signed. Positive puts stock in, negative takes it out; the fold only
    # ever sums, so no event type needs special arithmetic.
    quantity_delta: Mapped[Decimal] = mapped_column()
    unit: Mapped[str] = mapped_column(String(24))
    storage_location: Mapped[StorageLocation] = mapped_column(STORAGE_LOCATION_ENUM)

    printed_date: Mapped[date | None] = mapped_column(Date, default=None)
    date_label_type: Mapped[DateLabelType | None] = mapped_column(
        DATE_LABEL_TYPE_ENUM, default=None
    )

    # The two clocks. occurred_at is kitchen time; recorded_at is system time.
    # Every feature computation filters on both — see
    # InventoryEventRepository.events_known_as_of.
    occurred_at: Mapped[datetime] = mapped_column()
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Where the belief came from: receipt_ocr, barcode_scan, user_manual,
    # inference, ... Free text on purpose; sources multiply faster than
    # migrations ship.
    source: Mapped[str] = mapped_column(String(64))
    # How much we trust it. OCR of a crumpled receipt is not a typed entry,
    # and the waste model should be told the difference.
    confidence: Mapped[float] = mapped_column(Float, default=1.0, server_default=text("1.0"))

    # Globally unique, so a retried receipt upload or a redelivered queue
    # message appends nothing the second time.
    idempotency_key: Mapped[str] = mapped_column(String(200))

    # ``metadata`` is taken by Declarative, so the attribute is renamed while
    # the column keeps the name the schema was specified with.
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", default=dict, server_default=text("'{}'")
    )

    __table_args__ = (
        CheckConstraint(
            "quantity_delta >= 0 OR event_type <> 'purchased'",
            name="purchased_delta_not_negative",
        ),
        CheckConstraint(
            "quantity_delta <= 0 OR event_type NOT IN ('consumed', 'discarded')",
            name="removal_delta_not_positive",
        ),
        # A "use_by" with no date on it says nothing.
        CheckConstraint(
            "date_label_type IS NULL OR printed_date IS NOT NULL",
            name="label_type_needs_printed_date",
        ),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_is_a_probability"),
        CheckConstraint("length(btrim(unit)) > 0", name="unit_not_blank"),
        CheckConstraint("length(btrim(source)) > 0", name="source_not_blank"),
        CheckConstraint("length(btrim(idempotency_key)) > 0", name="idempotency_key_not_blank"),
        Index("uq_inventory_events_idempotency_key", "idempotency_key", unique=True),
        # Serves events_known_as_of and events_known_between: one household,
        # both clocks.
        Index("ix_inventory_events_household_id_recorded_at_occurred_at",
              "household_id", "recorded_at", "occurred_at"),
        Index("ix_inventory_events_household_id_occurred_at_recorded_at",
              "household_id", "occurred_at", "recorded_at"),
        # Per-product history, for shelf-life estimation. Named by hand
        # because the convention's full form runs past Postgres' 63-character
        # identifier limit.
        Index("ix_inventory_events_household_product_occurred_at",
              "household_id", "canonical_product_id", "occurred_at"),
    )

    def to_domain(self) -> InventoryEvent:
        """Detach the row into the pure domain type the fold consumes."""
        return InventoryEvent(
            id=self.id,
            household_id=self.household_id,
            canonical_product_id=self.canonical_product_id,
            event_type=self.event_type,
            quantity_delta=self.quantity_delta,
            unit=self.unit,
            storage_location=self.storage_location,
            printed_date=self.printed_date,
            date_label_type=self.date_label_type,
            occurred_at=self.occurred_at,
            recorded_at=self.recorded_at,
            source=self.source,
            confidence=self.confidence,
            idempotency_key=self.idempotency_key,
            metadata=dict(self.event_metadata or {}),
        )

    def __repr__(self) -> str:
        return (
            f"<InventoryEventRow {self.id} {self.event_type} "
            f"{self.quantity_delta}{self.unit} occurred={self.occurred_at}>"
        )
