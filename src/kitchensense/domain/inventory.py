"""The kitchen record: folding inventory events into an inventory snapshot.

This module is deliberately free of I/O. It imports nothing from sqlalchemy,
fastapi or httpx, so the fold can be reasoned about — and property-tested —
without a database anywhere near it.

Two ideas drive the design.

**Bitemporality.** Every event carries ``occurred_at`` (when the thing
happened in the kitchen) and ``recorded_at`` (when KitchenSense learned of
it). A snapshot is always taken *as of* a single instant, and an event only
belongs in it when *both* stamps are at or before that instant. An event that
occurred last Tuesday but was recorded today did not exist, as far as the
system was concerned, in any snapshot taken before today. Folding it into one
anyway would leak information backwards in time and inflate the accuracy of
any waste model trained on those snapshots. :func:`apply_events` refuses such
events outright rather than trusting callers to filter correctly.

**Order independence.** Because events arrive late, a snapshot built at T1 has
to absorb events that occurred before T1 but were recorded after it. That is
only safe if the fold does not care about the order in which events arrive,
so every accumulated field here uses a commutative, associative operation:
sum, min, max, count. Formally each lot is a commutative monoid, which gives
the law the projection depends on::

    fold(A ∪ B) == combine(fold(A), fold(B))      for disjoint A, B

Replaying the whole history and incrementally advancing an existing snapshot
therefore produce byte-identical results, whatever order the events show up
in. ``tests/test_snapshot_projection.py`` asserts exactly that against a real
Postgres, over arbitrary generated event sequences.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

__all__ = [
    "DateLabelType",
    "EventType",
    "HouseholdMismatchError",
    "InventoryEvent",
    "InventoryFoldError",
    "InventorySnapshot",
    "LabelLeakageError",
    "LotKey",
    "LotState",
    "NewInventoryEvent",
    "StorageLocation",
    "apply_events",
    "combine_snapshots",
    "empty_snapshot",
    "fold_events",
    "normalize_unit",
]

ZERO = Decimal("0")


class EventType(StrEnum):
    """What happened to a product.

    ``moved`` is recorded as a *pair* of events — a negative delta leaving the
    old ``storage_location`` and a positive delta arriving at the new one —
    which keeps a single ``storage_location`` column meaningful and lets the
    fold treat every event type identically.
    """

    PURCHASED = "purchased"
    OPENED = "opened"
    CONSUMED = "consumed"
    DISCARDED = "discarded"
    CORRECTED = "corrected"
    MOVED = "moved"


class DateLabelType(StrEnum):
    """The kind of date printed on the packaging.

    The distinction matters: ``use_by`` is a safety limit, ``best_before`` is a
    quality one, and a household that treats them the same throws away food it
    did not need to.
    """

    USE_BY = "use_by"
    BEST_BEFORE = "best_before"


class StorageLocation(StrEnum):
    PANTRY = "pantry"
    FRIDGE = "fridge"
    FREEZER = "freezer"
    COUNTER = "counter"
    OTHER = "other"


class InventoryFoldError(Exception):
    """Base class for refusals raised while folding events."""


class HouseholdMismatchError(InventoryFoldError):
    """An event from one household was folded into another's snapshot."""


class LabelLeakageError(InventoryFoldError):
    """An event was folded into a snapshot that could not have known about it.

    Raised when ``occurred_at`` or ``recorded_at`` is after the snapshot's
    ``as_of``. This is the last line of defence behind
    ``InventoryEventRepository.events_known_as_of``.
    """


def normalize_unit(unit: str) -> str:
    """Fold units to a single spelling so ``G`` and ``g`` share a lot."""
    return unit.strip().lower()


@dataclass(frozen=True, slots=True)
class NewInventoryEvent:
    """An event about to be appended.

    Carries no ``household_id``: the repository supplies it from its own
    required keyword argument, so a caller cannot write into the wrong
    household even by accident.

    ``recorded_at`` is normally left ``None`` so the database stamps it with
    its own clock. Tests set it explicitly to simulate late arrivals.
    """

    canonical_product_id: UUID
    event_type: EventType
    quantity_delta: Decimal
    unit: str
    storage_location: StorageLocation
    occurred_at: datetime
    source: str
    idempotency_key: str
    printed_date: date | None = None
    date_label_type: DateLabelType | None = None
    confidence: float = 1.0
    recorded_at: datetime | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InventoryEvent:
    """An event as it exists in the append-only log. Never mutated."""

    id: UUID
    household_id: UUID
    canonical_product_id: UUID
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
    idempotency_key: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, order=True)
class LotKey:
    """The grain of the snapshot.

    A *lot* is everything the household cannot tell apart when deciding what to
    cook tonight: same product, same place, same unit, same printed date. Two
    yoghurts with different use-by dates are different lots, because one of
    them is the one about to be wasted.
    """

    canonical_product_id: UUID
    storage_location: StorageLocation
    unit: str
    printed_date: date | None
    date_label_type: DateLabelType | None

    @classmethod
    def for_event(cls, event: InventoryEvent) -> LotKey:
        return cls(
            canonical_product_id=event.canonical_product_id,
            storage_location=event.storage_location,
            unit=normalize_unit(event.unit),
            printed_date=event.printed_date,
            date_label_type=event.date_label_type,
        )

    def sort_key(self) -> tuple[str, str, str, str, str]:
        """A total order over lots, so snapshots compare and serialise stably."""
        return (
            str(self.canonical_product_id),
            self.storage_location.value,
            self.unit,
            self.printed_date.isoformat() if self.printed_date is not None else "",
            self.date_label_type.value if self.date_label_type is not None else "",
        )


@dataclass(frozen=True, slots=True)
class LotState:
    """Everything the fold accumulates about one lot.

    Every field is a commutative accumulation — see :func:`combine_lots`. The
    consumed and discarded totals are kept as positive magnitudes because the
    waste model wants "how much was thrown away", not a signed delta.
    """

    key: LotKey
    quantity: Decimal
    purchased_quantity: Decimal
    consumed_quantity: Decimal
    discarded_quantity: Decimal
    event_count: int
    first_occurred_at: datetime
    last_occurred_at: datetime
    last_recorded_at: datetime
    opened_at: datetime | None

    @property
    def is_depleted(self) -> bool:
        return self.quantity <= ZERO

    @property
    def is_opened(self) -> bool:
        return self.opened_at is not None


def lot_from_event(event: InventoryEvent) -> LotState:
    """The single-event lot. The identity-adjacent building block of the fold."""
    delta = event.quantity_delta
    return LotState(
        key=LotKey.for_event(event),
        quantity=delta,
        purchased_quantity=delta if event.event_type is EventType.PURCHASED else ZERO,
        consumed_quantity=-delta if event.event_type is EventType.CONSUMED else ZERO,
        discarded_quantity=-delta if event.event_type is EventType.DISCARDED else ZERO,
        event_count=1,
        first_occurred_at=event.occurred_at,
        last_occurred_at=event.occurred_at,
        last_recorded_at=event.recorded_at,
        opened_at=event.occurred_at if event.event_type is EventType.OPENED else None,
    )


def combine_lots(left: LotState, right: LotState) -> LotState:
    """Merge two states of the same lot. Commutative and associative."""
    if left.key != right.key:
        raise InventoryFoldError(f"cannot combine different lots: {left.key} vs {right.key}")
    return LotState(
        key=left.key,
        quantity=left.quantity + right.quantity,
        purchased_quantity=left.purchased_quantity + right.purchased_quantity,
        consumed_quantity=left.consumed_quantity + right.consumed_quantity,
        discarded_quantity=left.discarded_quantity + right.discarded_quantity,
        event_count=left.event_count + right.event_count,
        first_occurred_at=min(left.first_occurred_at, right.first_occurred_at),
        last_occurred_at=max(left.last_occurred_at, right.last_occurred_at),
        last_recorded_at=max(left.last_recorded_at, right.last_recorded_at),
        opened_at=_earliest(left.opened_at, right.opened_at),
    )


def _earliest(left: datetime | None, right: datetime | None) -> datetime | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


@dataclass(frozen=True, slots=True)
class InventorySnapshot:
    """What a household had, as far as the system knew at ``as_of``.

    ``lots`` is always sorted by :meth:`LotKey.sort_key`, so two snapshots
    built by different routes compare equal with ``==``.
    """

    household_id: UUID
    as_of: datetime
    lots: tuple[LotState, ...]

    def lot(self, key: LotKey) -> LotState | None:
        for lot in self.lots:
            if lot.key == key:
                return lot
        return None

    def quantity_of(self, canonical_product_id: UUID) -> Decimal:
        """Total across every lot of a product, whatever its unit or location.

        Only meaningful when a product is stocked in one unit; callers dealing
        in mixed units should read :attr:`lots` directly.
        """
        matching = (
            lot.quantity
            for lot in self.lots
            if lot.key.canonical_product_id == canonical_product_id
        )
        return sum(matching, ZERO)

    def in_stock(self) -> tuple[LotState, ...]:
        return tuple(lot for lot in self.lots if not lot.is_depleted)


def _freeze(
    household_id: UUID, as_of: datetime, lots: Mapping[LotKey, LotState]
) -> InventorySnapshot:
    return InventorySnapshot(
        household_id=household_id,
        as_of=as_of,
        lots=tuple(sorted(lots.values(), key=lambda lot: lot.key.sort_key())),
    )


def empty_snapshot(*, household_id: UUID, as_of: datetime) -> InventorySnapshot:
    return InventorySnapshot(household_id=household_id, as_of=as_of, lots=())


def apply_events(
    snapshot: InventorySnapshot,
    events: Iterable[InventoryEvent],
    *,
    as_of: datetime,
) -> InventorySnapshot:
    """Advance ``snapshot`` to ``as_of`` by folding in newly known events.

    ``events`` must be the events that became known in ``(snapshot.as_of,
    as_of]`` — exactly what
    ``InventoryEventRepository.events_known_between`` returns. Applying the
    same event twice double-counts it; the fold is a sum, not a set union, and
    deliberately so, because deduplicating on every apply would cost a scan of
    the whole history.

    Raises:
        HouseholdMismatchError: an event belongs to another household.
        LabelLeakageError: ``as_of`` moved backwards, or an event is stamped
            after ``as_of`` and so could not have been known then.
    """
    if as_of < snapshot.as_of:
        raise LabelLeakageError(
            f"as_of moved backwards: {as_of.isoformat()} < {snapshot.as_of.isoformat()}"
        )

    lots: dict[LotKey, LotState] = {lot.key: lot for lot in snapshot.lots}
    for event in events:
        if event.household_id != snapshot.household_id:
            raise HouseholdMismatchError(
                f"event {event.id} belongs to household {event.household_id}, "
                f"not {snapshot.household_id}"
            )
        if event.occurred_at > as_of or event.recorded_at > as_of:
            raise LabelLeakageError(
                f"event {event.id} (occurred_at={event.occurred_at.isoformat()}, "
                f"recorded_at={event.recorded_at.isoformat()}) was not known "
                f"as of {as_of.isoformat()}"
            )
        incoming = lot_from_event(event)
        existing = lots.get(incoming.key)
        lots[incoming.key] = incoming if existing is None else combine_lots(existing, incoming)

    return _freeze(snapshot.household_id, as_of, lots)


def fold_events(
    events: Iterable[InventoryEvent],
    *,
    household_id: UUID,
    as_of: datetime,
) -> InventorySnapshot:
    """Build a snapshot from scratch. A full replay of the household's log."""
    return apply_events(empty_snapshot(household_id=household_id, as_of=as_of), events, as_of=as_of)


def combine_snapshots(left: InventorySnapshot, right: InventorySnapshot) -> InventorySnapshot:
    """Merge two snapshots of disjoint event sets for the same household.

    Exists so the monoid law can be exercised directly, and so a future
    parallel replay can fold shards independently and merge the results.
    """
    if left.household_id != right.household_id:
        raise HouseholdMismatchError(
            f"cannot combine snapshots for {left.household_id} and {right.household_id}"
        )
    lots: dict[LotKey, LotState] = {lot.key: lot for lot in left.lots}
    for lot in right.lots:
        existing = lots.get(lot.key)
        lots[lot.key] = lot if existing is None else combine_lots(existing, lot)
    return _freeze(left.household_id, max(left.as_of, right.as_of), lots)
