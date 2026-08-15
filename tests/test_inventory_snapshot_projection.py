"""The snapshot projection, against a real Postgres.

The centrepiece is
:func:`test_incremental_advance_matches_a_full_replay`, a property test over
arbitrary event sequences. Everything the product does — deciding what is
about to be wasted, building training examples for last month — reads a
snapshot, and snapshots are maintained incrementally because replaying a
household's whole history on every write does not scale. That optimisation is
only safe if advancing step by step lands on exactly the same state as
replaying from nothing, for *any* history, including the awkward ones where
events arrive out of order. So it is checked that way rather than asserted in
a comment.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, event as hypothesis_event, given, settings, strategies as st
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kitchensense.domain.inventory import (
    DateLabelType,
    EventType,
    HouseholdMismatchError,
    NewInventoryEvent,
    StorageLocation,
)
from kitchensense.models import InventorySnapshotRow
from kitchensense.repositories import (
    InventoryEventRepository,
    InventorySnapshotRepository,
)
from tests.conftest import Database, at, make_household, make_product
from tests.strategies import (
    EPOCH,
    HORIZON,
    MAX_OFFSET_MINUTES,
    PRODUCT_IDS,
    EventSpec,
    histories,
)

pytestmark = pytest.mark.postgres


async def _seed_products(session: AsyncSession) -> None:
    for index, product_id in enumerate(PRODUCT_IDS):
        await make_product(session, f"Generated product {index}", product_id=product_id)


# ----------------------------------------------------------------------
# Worked examples
# ----------------------------------------------------------------------


def test_a_week_of_milk(db: Database) -> None:
    """Buy two litres, open them, drink some, throw the rest away."""

    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        events = InventoryEventRepository(session)
        snapshots = InventorySnapshotRepository(session)

        def milk(
            event_type: EventType, delta: str, day: int, key: str
        ) -> NewInventoryEvent:
            return NewInventoryEvent(
                canonical_product_id=product_id,
                event_type=event_type,
                quantity_delta=Decimal(delta),
                unit="l",
                storage_location=StorageLocation.FRIDGE,
                printed_date=date(2026, 3, 9),
                date_label_type=DateLabelType.USE_BY,
                occurred_at=at(day),
                recorded_at=at(day),
                source="receipt_ocr",
                idempotency_key=key,
            )

        await events.append_many(
            household_id=household_id,
            events=[
                milk(EventType.PURCHASED, "2", 2, "milk-bought"),
                milk(EventType.OPENED, "0", 3, "milk-opened"),
                milk(EventType.CONSUMED, "-1.5", 5, "milk-drunk"),
                milk(EventType.DISCARDED, "-0.5", 10, "milk-binned"),
            ],
        )

        snapshot = await snapshots.rebuild(household_id=household_id, as_of=at(15))

        assert len(snapshot.lots) == 1
        lot = snapshot.lots[0]
        assert lot.quantity == Decimal("0")
        assert lot.purchased_quantity == Decimal("2")
        assert lot.consumed_quantity == Decimal("1.5")
        assert lot.discarded_quantity == Decimal("0.5")
        assert lot.opened_at == at(3)
        assert lot.is_depleted
        assert snapshot.in_stock() == ()

        # And it survives the round trip through the table.
        assert await snapshots.get(household_id=household_id) == snapshot

    db.run(scenario)


def test_a_snapshot_of_the_past_ignores_what_was_learned_later(db: Database) -> None:
    """The read path a training-set build uses, with a late arrival present."""

    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        events = InventoryEventRepository(session)
        snapshots = InventorySnapshotRepository(session)

        await events.append_many(
            household_id=household_id,
            events=[
                NewInventoryEvent(
                    canonical_product_id=product_id,
                    event_type=EventType.PURCHASED,
                    quantity_delta=Decimal("2"),
                    unit="l",
                    storage_location=StorageLocation.FRIDGE,
                    occurred_at=at(2),
                    recorded_at=at(2),
                    source="receipt_ocr",
                    idempotency_key="bought",
                ),
                # Binned on the 4th; only reported on the 12th.
                NewInventoryEvent(
                    canonical_product_id=product_id,
                    event_type=EventType.DISCARDED,
                    quantity_delta=Decimal("-2"),
                    unit="l",
                    storage_location=StorageLocation.FRIDGE,
                    occurred_at=at(4),
                    recorded_at=at(12),
                    source="user_manual",
                    idempotency_key="binned",
                ),
            ],
        )

        on_the_sixth = await snapshots.project_known_as_of(
            household_id=household_id, as_of=at(6)
        )
        today = await snapshots.project_known_as_of(household_id=household_id, as_of=at(20))

        # On the 6th the system still believed there were two litres in the
        # fridge, and a feature built for the 6th must say so.
        assert on_the_sixth.lots[0].quantity == Decimal("2")
        assert on_the_sixth.lots[0].discarded_quantity == Decimal("0")
        assert today.lots[0].quantity == Decimal("0")
        assert today.lots[0].discarded_quantity == Decimal("2")

    db.run(scenario)


def test_advancing_picks_up_a_late_arrival(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        events = InventoryEventRepository(session)
        snapshots = InventorySnapshotRepository(session)

        await events.append(
            household_id=household_id,
            event=NewInventoryEvent(
                canonical_product_id=product_id,
                event_type=EventType.PURCHASED,
                quantity_delta=Decimal("2"),
                unit="l",
                storage_location=StorageLocation.FRIDGE,
                occurred_at=at(2),
                recorded_at=at(2),
                source="receipt_ocr",
                idempotency_key="bought",
            ),
        )
        stored = await snapshots.rebuild(household_id=household_id, as_of=at(6))
        assert stored.lots[0].quantity == Decimal("2")

        await events.append(
            household_id=household_id,
            event=NewInventoryEvent(
                canonical_product_id=product_id,
                event_type=EventType.CONSUMED,
                quantity_delta=Decimal("-2"),
                unit="l",
                storage_location=StorageLocation.FRIDGE,
                occurred_at=at(3),  # before the stored snapshot's as_of
                recorded_at=at(12),  # but only reported now
                source="user_manual",
                idempotency_key="drunk",
            ),
        )

        advanced = await snapshots.advance(household_id=household_id, as_of=at(20))
        replayed = await snapshots.project_known_as_of(household_id=household_id, as_of=at(20))

        assert advanced.lots[0].quantity == Decimal("0")
        assert advanced == replayed

    db.run(scenario)


def test_saving_replaces_lots_that_no_longer_exist(db: Database) -> None:
    """A stale row must not outlive the fold that produced it."""

    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        events = InventoryEventRepository(session)
        snapshots = InventorySnapshotRepository(session)

        await events.append(
            household_id=household_id,
            event=NewInventoryEvent(
                canonical_product_id=product_id,
                event_type=EventType.PURCHASED,
                quantity_delta=Decimal("1"),
                unit="l",
                storage_location=StorageLocation.FRIDGE,
                occurred_at=at(2),
                recorded_at=at(2),
                source="receipt_ocr",
                idempotency_key="fridge",
            ),
        )
        await snapshots.rebuild(household_id=household_id, as_of=at(5))

        await snapshots.save(
            household_id=household_id,
            snapshot=InventorySnapshotRepository.empty(
                household_id=household_id, as_of=at(6)
            ),
        )

        remaining = await session.execute(
            select(func.count())
            .select_from(InventorySnapshotRow)
            .where(InventorySnapshotRow.household_id == household_id)
        )
        assert remaining.scalar_one() == 0

    db.run(scenario)


def test_an_unlabelled_lot_stays_one_row(db: Database) -> None:
    """NULL printed dates are equal for uniqueness, not distinct.

    Without NULLS NOT DISTINCT on the lot key, a bag of loose carrots would
    accumulate a fresh row on every projection rebuild.
    """

    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        events = InventoryEventRepository(session)
        snapshots = InventorySnapshotRepository(session)

        await events.append_many(
            household_id=household_id,
            events=[
                NewInventoryEvent(
                    canonical_product_id=product_id,
                    event_type=EventType.PURCHASED,
                    quantity_delta=Decimal("1"),
                    unit="kg",
                    storage_location=StorageLocation.PANTRY,
                    occurred_at=at(day),
                    recorded_at=at(day),
                    source="receipt_ocr",
                    idempotency_key=f"carrots-{day}",
                )
                for day in (2, 3)
            ],
        )

        snapshot = await snapshots.rebuild(household_id=household_id, as_of=at(5))
        assert len(snapshot.lots) == 1
        assert snapshot.lots[0].quantity == Decimal("2")

    db.run(scenario)


def test_a_snapshot_cannot_be_saved_into_the_wrong_household(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        ours = await make_household(session, "Ours")
        theirs = await make_household(session, "Theirs")
        snapshots = InventorySnapshotRepository(session)

        with pytest.raises(HouseholdMismatchError):
            await snapshots.save(
                household_id=ours,
                snapshot=InventorySnapshotRepository.empty(
                    household_id=theirs, as_of=at(5)
                ),
            )

    db.run(scenario)


# ----------------------------------------------------------------------
# The property
# ----------------------------------------------------------------------


CHECKPOINTS = st.lists(
    st.integers(min_value=0, max_value=MAX_OFFSET_MINUTES),
    min_size=1,
    max_size=5,
).map(lambda offsets: sorted(set(offsets)))


@settings(
    max_examples=60,
    deadline=None,
    suppress_health_check=[
        # Each example runs in its own transaction and rolls it back, so the
        # per-test database fixture carries nothing between examples.
        HealthCheck.function_scoped_fixture,
        HealthCheck.too_slow,
    ],
)
@given(specs=histories(max_size=10), checkpoint_offsets=CHECKPOINTS)
def test_incremental_advance_matches_a_full_replay(
    db: Database, specs: list[EventSpec], checkpoint_offsets: list[int]
) -> None:
    """For any history and any schedule of updates, the two agree.

    The incremental path advances the stored projection through each
    checkpoint in turn, reading only the events that became known since the
    last one. The replay path folds the entire log at the final instant. They
    must produce the same snapshot — same lots, same quantities, same opened
    timestamps — or every snapshot the product has ever served is a guess.
    """

    async def scenario(session: AsyncSession) -> None:
        run_id = uuid.uuid4()
        household_id = await make_household(session, f"Household {run_id}")
        await _seed_products(session)

        events = InventoryEventRepository(session)
        snapshots = InventorySnapshotRepository(session)

        await events.append_many(
            household_id=household_id,
            events=[
                spec.as_new_event(run_id=run_id, index=index)
                for index, spec in enumerate(specs)
            ],
        )

        checkpoints = [
            EPOCH + timedelta(minutes=offset) for offset in checkpoint_offsets
        ] + [HORIZON]

        incremental = None
        advanced_from_stored = 0
        for checkpoint in checkpoints:
            if await snapshots.get(household_id=household_id) is not None:
                advanced_from_stored += 1
            incremental = await snapshots.advance(
                household_id=household_id, as_of=checkpoint
            )
            # Not just at the end: the two must agree at every step, or a
            # snapshot served between updates is already wrong.
            assert incremental == await snapshots.project_known_as_of(
                household_id=household_id, as_of=checkpoint
            )

        # Reported so the statistics show how often the incremental branch —
        # rather than the rebuild fallback — was the one under test.
        hypothesis_event(
            "advanced from a stored snapshot"
            if advanced_from_stored
            else "rebuilt from scratch only"
        )

        replayed = await snapshots.project_known_as_of(
            household_id=household_id, as_of=HORIZON
        )

        assert incremental is not None
        assert incremental == replayed

        # And what is on disk is what the fold produced. An empty fold stores
        # no rows at all, so there is nothing to read back in that case.
        stored = await snapshots.get(household_id=household_id)
        assert stored == (replayed if replayed.lots else None)

    db.run(scenario)


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(specs=histories(max_size=10))
def test_the_stored_projection_round_trips_through_postgres(
    db: Database, specs: list[EventSpec]
) -> None:
    """Numerics, enums and nullable dates survive the write and the read."""

    async def scenario(session: AsyncSession) -> None:
        run_id = uuid.uuid4()
        household_id = await make_household(session, f"Household {run_id}")
        await _seed_products(session)

        events = InventoryEventRepository(session)
        snapshots = InventorySnapshotRepository(session)

        await events.append_many(
            household_id=household_id,
            events=[
                spec.as_new_event(run_id=run_id, index=index)
                for index, spec in enumerate(specs)
            ],
        )

        built = await snapshots.rebuild(household_id=household_id, as_of=HORIZON)
        stored = await snapshots.get(household_id=household_id)

        if not built.lots:
            assert stored is None
        else:
            assert stored == built

    db.run(scenario)


def test_the_epoch_used_by_the_generators_is_timezone_aware() -> None:
    assert EPOCH.tzinfo is UTC
    assert HORIZON > EPOCH + timedelta(minutes=MAX_OFFSET_MINUTES)
    assert isinstance(HORIZON, datetime)
