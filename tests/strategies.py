"""Hypothesis strategies for arbitrary kitchen histories.

One generator feeds both the pure fold tests and the Postgres-backed
projection test, so the two are provably exercised on the same shapes of
data.

The generator deliberately produces awkward histories: lots that differ only
by printed date, quantities that go negative, and — importantly — events whose
``recorded_at`` bears no fixed relationship to their ``occurred_at``. Both
orderings are legal. An event recorded days after it happened is the late
receipt upload the bitemporal filter exists for, and one stamped with a future
``occurred_at`` is what a plain ``recorded_at`` watermark would silently lose.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hypothesis import strategies as st

from kitchensense.domain.inventory import (
    DateLabelType,
    EventType,
    InventoryEvent,
    NewInventoryEvent,
    StorageLocation,
)

# A small pool, so generated events collide onto the same lots often enough
# for the fold's merging to be exercised rather than trivially skipped.
PRODUCT_IDS = [uuid.UUID(int=index) for index in (0xA1, 0xA2, 0xA3)]
UNITS = ["g", "ml", "item"]
PRINTED_DATES = [None, date(2026, 3, 5), date(2026, 3, 12)]
SOURCES = ["receipt_ocr", "barcode_scan", "user_manual"]

EPOCH = datetime(2026, 3, 1, tzinfo=UTC)
# Both clocks range over the same 48 hours, independently.
MAX_OFFSET_MINUTES = 48 * 60

REMOVALS = (EventType.CONSUMED, EventType.DISCARDED)


@dataclass(frozen=True)
class EventSpec:
    """A generated event, not yet bound to a household."""

    canonical_product_id: uuid.UUID
    event_type: EventType
    quantity_delta: Decimal
    unit: str
    storage_location: StorageLocation
    printed_date: date | None
    date_label_type: DateLabelType | None
    occurred_at: datetime
    recorded_at: datetime
    source: str
    confidence: float

    def as_event(self, *, household_id: uuid.UUID, index: int) -> InventoryEvent:
        return InventoryEvent(
            id=uuid.UUID(int=index + 1),
            household_id=household_id,
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
            idempotency_key=f"spec-{index}",
            metadata={},
        )

    def as_new_event(self, *, run_id: uuid.UUID, index: int) -> NewInventoryEvent:
        return NewInventoryEvent(
            canonical_product_id=self.canonical_product_id,
            event_type=self.event_type,
            quantity_delta=self.quantity_delta,
            unit=self.unit,
            storage_location=self.storage_location,
            printed_date=self.printed_date,
            date_label_type=self.date_label_type,
            occurred_at=self.occurred_at,
            # Set explicitly rather than left to the database clock: the whole
            # point is to simulate histories that arrived out of order.
            recorded_at=self.recorded_at,
            source=self.source,
            confidence=self.confidence,
            idempotency_key=f"{run_id}-{index}",
            metadata={},
        )


_MAGNITUDES = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("500"),
    places=3,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def event_specs(draw: st.DrawFn) -> EventSpec:
    event_type = draw(st.sampled_from(list(EventType)))
    magnitude = draw(_MAGNITUDES)

    if event_type is EventType.PURCHASED:
        delta = magnitude
    elif event_type in REMOVALS:
        delta = -magnitude
    else:
        # opened, corrected and moved may go either way: a correction can add
        # or subtract, and a move is a negative leg and a positive one.
        delta = magnitude if draw(st.booleans()) else -magnitude

    printed = draw(st.sampled_from(PRINTED_DATES))
    label = (
        None if printed is None else draw(st.sampled_from([None, *list(DateLabelType)]))
    )

    return EventSpec(
        canonical_product_id=draw(st.sampled_from(PRODUCT_IDS)),
        event_type=event_type,
        quantity_delta=delta,
        unit=draw(st.sampled_from(UNITS)),
        storage_location=draw(st.sampled_from(list(StorageLocation))),
        printed_date=printed,
        date_label_type=label,
        occurred_at=EPOCH + timedelta(minutes=draw(st.integers(0, MAX_OFFSET_MINUTES))),
        recorded_at=EPOCH + timedelta(minutes=draw(st.integers(0, MAX_OFFSET_MINUTES))),
        source=draw(st.sampled_from(SOURCES)),
        confidence=draw(st.sampled_from([0.4, 0.75, 1.0])),
    )


def histories(*, max_size: int = 14) -> st.SearchStrategy[list[EventSpec]]:
    return st.lists(event_specs(), max_size=max_size)


# An ``as_of`` late enough that every generated event is known by it.
HORIZON = EPOCH + timedelta(minutes=MAX_OFFSET_MINUTES + 1)
