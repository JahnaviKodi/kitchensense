"""The fold, tested without a database anywhere near it."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import given, settings, strategies as st

from kitchensense.domain.inventory import (
    DateLabelType,
    EventType,
    HouseholdMismatchError,
    InventoryEvent,
    LabelLeakageError,
    LotKey,
    StorageLocation,
    apply_events,
    combine_snapshots,
    empty_snapshot,
    fold_events,
)
from tests.strategies import HORIZON, EventSpec, histories

HOUSEHOLD = uuid.UUID(int=1)
MILK = uuid.UUID(int=0xA1)
YOGHURT = uuid.UUID(int=0xA2)


def event(
    *,
    product: uuid.UUID = MILK,
    event_type: EventType = EventType.PURCHASED,
    delta: str = "1",
    unit: str = "l",
    location: StorageLocation = StorageLocation.FRIDGE,
    printed_date: date | None = None,
    date_label_type: DateLabelType | None = None,
    occurred: datetime | None = None,
    recorded: datetime | None = None,
    household: uuid.UUID = HOUSEHOLD,
) -> InventoryEvent:
    when = occurred or datetime(2026, 3, 1, 9, tzinfo=UTC)
    return InventoryEvent(
        id=uuid.uuid4(),
        household_id=household,
        canonical_product_id=product,
        event_type=event_type,
        quantity_delta=Decimal(delta),
        unit=unit,
        storage_location=location,
        printed_date=printed_date,
        date_label_type=date_label_type,
        occurred_at=when,
        recorded_at=recorded or when,
        source="user_manual",
        confidence=1.0,
        idempotency_key=str(uuid.uuid4()),
        metadata={},
    )


AS_OF = datetime(2026, 3, 20, tzinfo=UTC)


def test_purchase_then_consume_leaves_the_remainder() -> None:
    snapshot = fold_events(
        [
            event(delta="2", event_type=EventType.PURCHASED),
            event(delta="-0.5", event_type=EventType.CONSUMED),
        ],
        household_id=HOUSEHOLD,
        as_of=AS_OF,
    )

    assert len(snapshot.lots) == 1
    lot = snapshot.lots[0]
    assert lot.quantity == Decimal("1.5")
    assert lot.purchased_quantity == Decimal("2")
    assert lot.consumed_quantity == Decimal("0.5")
    assert lot.event_count == 2
    assert not lot.is_depleted


def test_printed_date_separates_lots() -> None:
    """Two tubs of the same yoghurt are different lots if they expire apart.

    This is the whole reason the snapshot is not keyed on product alone: one
    of the two is the one about to be wasted.
    """
    snapshot = fold_events(
        [
            event(product=YOGHURT, delta="4", unit="item", printed_date=date(2026, 3, 5),
                  date_label_type=DateLabelType.USE_BY),
            event(product=YOGHURT, delta="4", unit="item", printed_date=date(2026, 3, 12),
                  date_label_type=DateLabelType.USE_BY),
        ],
        household_id=HOUSEHOLD,
        as_of=AS_OF,
    )

    assert len(snapshot.lots) == 2
    assert {lot.key.printed_date for lot in snapshot.lots} == {
        date(2026, 3, 5),
        date(2026, 3, 12),
    }


def test_storage_location_separates_lots() -> None:
    snapshot = fold_events(
        [
            event(delta="2", location=StorageLocation.FRIDGE),
            event(delta="3", location=StorageLocation.FREEZER),
        ],
        household_id=HOUSEHOLD,
        as_of=AS_OF,
    )

    assert len(snapshot.lots) == 2
    assert snapshot.quantity_of(MILK) == Decimal("5")


def test_a_move_is_a_pair_of_events() -> None:
    """Moving stock leaves the source empty and the destination stocked."""
    snapshot = fold_events(
        [
            event(delta="3", location=StorageLocation.FRIDGE),
            # The two legs of one move.
            event(delta="-3", event_type=EventType.MOVED, location=StorageLocation.FRIDGE),
            event(delta="3", event_type=EventType.MOVED, location=StorageLocation.FREEZER),
        ],
        household_id=HOUSEHOLD,
        as_of=AS_OF,
    )

    by_location = {lot.key.storage_location: lot.quantity for lot in snapshot.lots}
    assert by_location[StorageLocation.FRIDGE] == Decimal("0")
    assert by_location[StorageLocation.FREEZER] == Decimal("3")


def test_units_are_normalised_onto_one_lot() -> None:
    snapshot = fold_events(
        [event(delta="500", unit="G"), event(delta="250", unit=" g ")],
        household_id=HOUSEHOLD,
        as_of=AS_OF,
    )

    assert len(snapshot.lots) == 1
    assert snapshot.lots[0].quantity == Decimal("750")


def test_opened_at_is_the_earliest_opening_whatever_the_order() -> None:
    early = datetime(2026, 3, 2, tzinfo=UTC)
    late = datetime(2026, 3, 4, tzinfo=UTC)

    forwards = fold_events(
        [
            event(delta="0", event_type=EventType.OPENED, occurred=early),
            event(delta="0", event_type=EventType.OPENED, occurred=late),
        ],
        household_id=HOUSEHOLD,
        as_of=AS_OF,
    )
    backwards = fold_events(
        [
            event(delta="0", event_type=EventType.OPENED, occurred=late),
            event(delta="0", event_type=EventType.OPENED, occurred=early),
        ],
        household_id=HOUSEHOLD,
        as_of=AS_OF,
    )

    assert forwards.lots[0].opened_at == early
    assert backwards.lots[0].opened_at == early
    assert forwards.lots[0].is_opened


def test_correction_adjusts_without_rewriting_history() -> None:
    snapshot = fold_events(
        [
            event(delta="6", event_type=EventType.PURCHASED),
            event(delta="-2", event_type=EventType.CORRECTED),
        ],
        household_id=HOUSEHOLD,
        as_of=AS_OF,
    )

    lot = snapshot.lots[0]
    assert lot.quantity == Decimal("4")
    # The original purchase is still on the record at its original size.
    assert lot.purchased_quantity == Decimal("6")


def test_folding_an_event_recorded_after_as_of_is_refused() -> None:
    """The fold will not accept knowledge the snapshot could not have had."""
    late_arrival = event(
        occurred=datetime(2026, 3, 2, tzinfo=UTC),
        recorded=datetime(2026, 3, 9, tzinfo=UTC),
    )

    with pytest.raises(LabelLeakageError):
        fold_events(
            [late_arrival], household_id=HOUSEHOLD, as_of=datetime(2026, 3, 5, tzinfo=UTC)
        )


def test_folding_an_event_that_has_not_happened_yet_is_refused() -> None:
    future = event(occurred=datetime(2026, 3, 20, tzinfo=UTC))

    with pytest.raises(LabelLeakageError):
        fold_events(
            [future], household_id=HOUSEHOLD, as_of=datetime(2026, 3, 5, tzinfo=UTC)
        )


def test_as_of_cannot_move_backwards() -> None:
    snapshot = empty_snapshot(household_id=HOUSEHOLD, as_of=AS_OF)

    with pytest.raises(LabelLeakageError):
        apply_events(snapshot, [], as_of=AS_OF - timedelta(days=1))


def test_another_households_event_is_refused() -> None:
    stranger = event(household=uuid.UUID(int=99))

    with pytest.raises(HouseholdMismatchError):
        fold_events([stranger], household_id=HOUSEHOLD, as_of=AS_OF)


def test_lot_lookup_and_stock_filter() -> None:
    snapshot = fold_events(
        [
            event(delta="2"),
            event(product=YOGHURT, delta="1", unit="item"),
            event(product=YOGHURT, delta="-1", unit="item", event_type=EventType.CONSUMED),
        ],
        household_id=HOUSEHOLD,
        as_of=AS_OF,
    )

    milk_key = LotKey(
        canonical_product_id=MILK,
        storage_location=StorageLocation.FRIDGE,
        unit="l",
        printed_date=None,
        date_label_type=None,
    )
    assert snapshot.lot(milk_key) is not None
    assert snapshot.lot(
        LotKey(uuid.UUID(int=1234), StorageLocation.PANTRY, "g", None, None)
    ) is None
    assert [lot.key.canonical_product_id for lot in snapshot.in_stock()] == [MILK]


# ----------------------------------------------------------------------
# Properties of the fold itself
# ----------------------------------------------------------------------


def _events(specs: list[EventSpec]) -> list[InventoryEvent]:
    return [
        spec.as_event(household_id=HOUSEHOLD, index=index) for index, spec in enumerate(specs)
    ]


@settings(max_examples=200, deadline=None)
@given(specs=histories(), data=st.data())
def test_the_fold_ignores_arrival_order(specs: list[EventSpec], data: st.DataObject) -> None:
    """Shuffling the history cannot change the snapshot.

    Without this, an incremental projection would be wrong the first time an
    event arrived late — which, for a household that photographs its receipts
    on Sunday evening, is most of them.
    """
    events = _events(specs)
    shuffled = data.draw(st.permutations(events))

    assert fold_events(events, household_id=HOUSEHOLD, as_of=HORIZON) == fold_events(
        shuffled, household_id=HOUSEHOLD, as_of=HORIZON
    )


@settings(max_examples=200, deadline=None)
@given(specs=histories(), data=st.data())
def test_folding_in_chunks_matches_folding_all_at_once(
    specs: list[EventSpec], data: st.DataObject
) -> None:
    events = _events(specs)
    split = data.draw(st.integers(min_value=0, max_value=len(events)))

    partial = fold_events(events[:split], household_id=HOUSEHOLD, as_of=HORIZON)
    incremental = apply_events(partial, events[split:], as_of=HORIZON)

    assert incremental == fold_events(events, household_id=HOUSEHOLD, as_of=HORIZON)


@settings(max_examples=200, deadline=None)
@given(specs=histories(), data=st.data())
def test_snapshots_of_disjoint_histories_merge(
    specs: list[EventSpec], data: st.DataObject
) -> None:
    """The monoid law, stated directly: fold(A ∪ B) == fold(A) ⊕ fold(B)."""
    events = _events(specs)
    split = data.draw(st.integers(min_value=0, max_value=len(events)))

    left = fold_events(events[:split], household_id=HOUSEHOLD, as_of=HORIZON)
    right = fold_events(events[split:], household_id=HOUSEHOLD, as_of=HORIZON)

    assert combine_snapshots(left, right) == fold_events(
        events, household_id=HOUSEHOLD, as_of=HORIZON
    )


@settings(max_examples=200, deadline=None)
@given(specs=histories())
def test_quantity_is_the_sum_of_its_deltas(specs: list[EventSpec]) -> None:
    events = _events(specs)
    snapshot = fold_events(events, household_id=HOUSEHOLD, as_of=HORIZON)

    total = sum((lot.quantity for lot in snapshot.lots), Decimal("0"))
    assert total == sum((e.quantity_delta for e in events), Decimal("0"))
    assert sum(lot.event_count for lot in snapshot.lots) == len(events)
