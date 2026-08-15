"""The event log, against a real Postgres.

These run against the migrated schema in a container, not against
``create_all``, so the constraints and triggers under test are the ones a
deployment gets.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kitchensense.domain.inventory import (
    DateLabelType,
    EventType,
    NewInventoryEvent,
    StorageLocation,
)
from kitchensense.repositories import (
    IdempotencyKeyConflictError,
    InventoryEventRepository,
)
from tests.conftest import Database, at, make_household, make_product

pytestmark = pytest.mark.postgres


def purchase(
    product_id: uuid.UUID,
    *,
    key: str,
    delta: str = "2",
    occurred_day: int = 2,
    # Pinned by default. Left to the database clock, recorded_at would be the
    # real "now", which is years past the fixed dates these tests read at.
    recorded_day: int | None = 2,
    printed: date | None = None,
    label: DateLabelType | None = None,
) -> NewInventoryEvent:
    return NewInventoryEvent(
        canonical_product_id=product_id,
        event_type=EventType.PURCHASED,
        quantity_delta=Decimal(delta),
        unit="l",
        storage_location=StorageLocation.FRIDGE,
        printed_date=printed,
        date_label_type=label,
        occurred_at=at(occurred_day),
        recorded_at=None if recorded_day is None else at(recorded_day),
        source="receipt_ocr",
        confidence=0.9,
        idempotency_key=key,
    )


def test_an_appended_event_reads_back_intact(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        appended = await repository.append(
            household_id=household_id,
            event=purchase(
                product_id,
                key="receipt-1",
                printed=date(2026, 3, 9),
                label=DateLabelType.USE_BY,
            ),
        )

        assert appended.household_id == household_id
        assert appended.quantity_delta == Decimal("2")
        assert appended.date_label_type is DateLabelType.USE_BY
        assert appended.confidence == pytest.approx(0.9)
        assert appended.recorded_at == at(2)

        fetched = await repository.get_event(
            household_id=household_id, event_id=appended.id
        )
        assert fetched == appended

    db.run(scenario)


def test_recorded_at_is_stamped_by_the_database_when_the_caller_omits_it(
    db: Database,
) -> None:
    """System time comes from one clock, not from whichever machine called in."""

    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        before = await session.scalar(text("SELECT now()"))
        appended = await repository.append(
            household_id=household_id,
            event=purchase(product_id, key="server-clock", recorded_day=None),
        )

        assert appended.recorded_at.tzinfo is not None
        assert appended.recorded_at >= before
        # Kitchen time is still whatever the caller reported.
        assert appended.occurred_at == at(2)

    db.run(scenario)


def test_metadata_round_trips_as_jsonb(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        appended = await repository.append(
            household_id=household_id,
            event=NewInventoryEvent(
                canonical_product_id=product_id,
                event_type=EventType.PURCHASED,
                quantity_delta=Decimal("2"),
                unit="l",
                storage_location=StorageLocation.FRIDGE,
                occurred_at=at(2),
                source="receipt_ocr",
                idempotency_key="receipt-meta",
                metadata={"receipt_line": "SEMI SKIM MLK 2L", "ocr_score": 0.81},
            ),
        )

        assert appended.metadata["receipt_line"] == "SEMI SKIM MLK 2L"
        assert appended.metadata["ocr_score"] == pytest.approx(0.81)

    db.run(scenario)


# ----------------------------------------------------------------------
# Tenancy
# ----------------------------------------------------------------------


def test_one_household_cannot_see_anothers_events(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        ours = await make_household(session, "Ours")
        theirs = await make_household(session, "Theirs")
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        mine = await repository.append(
            household_id=ours, event=purchase(product_id, key="ours-1")
        )
        await repository.append(
            household_id=theirs, event=purchase(product_id, key="theirs-1")
        )

        visible = await repository.events_known_as_of(household_id=ours, as_of=at(20))
        assert [event.id for event in visible] == [mine.id]

        # Even with the other household's event id in hand.
        assert await repository.get_event(household_id=theirs, event_id=mine.id) is None
        assert await repository.count_known_as_of(household_id=ours, as_of=at(20)) == 1

    db.run(scenario)


def test_an_idempotency_key_belonging_to_another_household_is_refused(
    db: Database,
) -> None:
    """A shared key must not resolve to someone else's event."""

    async def scenario(session: AsyncSession) -> None:
        ours = await make_household(session, "Ours")
        theirs = await make_household(session, "Theirs")
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        await repository.append(
            household_id=theirs, event=purchase(product_id, key="collision")
        )

        with pytest.raises(IdempotencyKeyConflictError):
            await repository.append_if_new(
                household_id=ours, event=purchase(product_id, key="collision")
            )

    db.run(scenario)


# ----------------------------------------------------------------------
# The two clocks
# ----------------------------------------------------------------------


def test_an_event_recorded_later_is_invisible_to_an_earlier_snapshot(
    db: Database,
) -> None:
    """The label-leakage guard, in the case it exists for.

    Milk was thrown away on the 2nd; the household told KitchenSense on the
    9th. A feature computed for the 5th must not know about it — on the 5th,
    nobody had told us.
    """

    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        await repository.append(
            household_id=household_id,
            event=purchase(product_id, key="late", occurred_day=2, recorded_day=9),
        )

        assert await repository.events_known_as_of(household_id=household_id, as_of=at(5)) == []
        later = await repository.events_known_as_of(household_id=household_id, as_of=at(10))
        assert len(later) == 1

    db.run(scenario)


def test_an_event_that_has_not_happened_yet_is_invisible(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        await repository.append(
            household_id=household_id,
            event=purchase(product_id, key="future", occurred_day=20, recorded_day=2),
        )

        assert await repository.events_known_as_of(household_id=household_id, as_of=at(5)) == []
        assert (
            len(await repository.events_known_as_of(household_id=household_id, as_of=at(21)))
            == 1
        )

    db.run(scenario)


def test_events_known_between_is_exactly_the_difference_of_the_two_windows(
    db: Database,
) -> None:
    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        await repository.append_many(
            household_id=household_id,
            events=[
                # known by the 5th
                purchase(product_id, key="a", occurred_day=2, recorded_day=3),
                # occurred before the 5th, recorded after it: the late arrival
                purchase(product_id, key="b", occurred_day=2, recorded_day=8),
                # recorded before the 5th but dated after it: the future arrival
                # a recorded_at-only watermark would lose forever
                purchase(product_id, key="c", occurred_day=7, recorded_day=3),
                # entirely outside both windows
                purchase(product_id, key="d", occurred_day=20, recorded_day=20),
            ],
        )

        early = {e.idempotency_key for e in
                 await repository.events_known_as_of(household_id=household_id, as_of=at(5))}
        late = {e.idempotency_key for e in
                await repository.events_known_as_of(household_id=household_id, as_of=at(10))}
        between = {e.idempotency_key for e in
                   await repository.events_known_between(
                       household_id=household_id, after=at(5), as_of=at(10)
                   )}

        assert early == {"a"}
        assert late == {"a", "b", "c"}
        assert between == late - early == {"b", "c"}

    db.run(scenario)


def test_latest_recorded_at_respects_the_as_of_window(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        await repository.append_many(
            household_id=household_id,
            events=[
                purchase(product_id, key="w1", occurred_day=2, recorded_day=3),
                purchase(product_id, key="w2", occurred_day=4, recorded_day=9),
            ],
        )

        assert await repository.latest_recorded_at(household_id=household_id, as_of=at(5)) == at(3)
        assert await repository.latest_recorded_at(household_id=household_id, as_of=at(10)) == at(9)
        assert await repository.latest_recorded_at(household_id=household_id, as_of=at(1)) is None

    db.run(scenario)


# ----------------------------------------------------------------------
# Idempotency
# ----------------------------------------------------------------------


def test_replaying_an_upload_appends_nothing_the_second_time(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        first, created = await repository.append_if_new(
            household_id=household_id, event=purchase(product_id, key="upload-77")
        )
        second, created_again = await repository.append_if_new(
            household_id=household_id, event=purchase(product_id, key="upload-77")
        )

        assert created is True
        assert created_again is False
        assert first.id == second.id
        assert await repository.count_known_as_of(household_id=household_id, as_of=at(20)) == 1

    db.run(scenario)


def test_a_duplicate_key_through_plain_append_is_an_error(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        await repository.append(household_id=household_id, event=purchase(product_id, key="dup"))

        nested = await session.begin_nested()
        try:
            with pytest.raises(IntegrityError):
                await repository.append(
                    household_id=household_id, event=purchase(product_id, key="dup")
                )
        finally:
            await nested.rollback()

    db.run(scenario)


# ----------------------------------------------------------------------
# Append-only
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("operation", "statement"),
    [
        ("UPDATE", "UPDATE inventory_events SET confidence = 0.1 WHERE id = :id"),
        ("DELETE", "DELETE FROM inventory_events WHERE id = :id"),
        ("TRUNCATE", "TRUNCATE inventory_events"),
    ],
)
def test_the_log_refuses_to_be_rewritten(
    db: Database, operation: str, statement: str
) -> None:
    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)
        appended = await repository.append(
            household_id=household_id, event=purchase(product_id, key=f"immutable-{operation}")
        )

        nested = await session.begin_nested()
        try:
            with pytest.raises(DBAPIError) as caught:
                await session.execute(text(statement), {"id": appended.id})
            assert "append-only" in str(caught.value)
        finally:
            await nested.rollback()

        # Still there, unchanged.
        survivor = await repository.get_event(
            household_id=household_id, event_id=appended.id
        )
        assert survivor is not None
        assert survivor.confidence == pytest.approx(0.9)

    db.run(scenario)


# ----------------------------------------------------------------------
# Constraints
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "event_factory"),
    [
        (
            "a use-by label with no date on it",
            lambda product_id: NewInventoryEvent(
                canonical_product_id=product_id,
                event_type=EventType.PURCHASED,
                quantity_delta=Decimal("1"),
                unit="l",
                storage_location=StorageLocation.FRIDGE,
                printed_date=None,
                date_label_type=DateLabelType.USE_BY,
                occurred_at=at(2),
                source="receipt_ocr",
                idempotency_key="bad-label",
            ),
        ),
        (
            "a confidence outside [0, 1]",
            lambda product_id: NewInventoryEvent(
                canonical_product_id=product_id,
                event_type=EventType.PURCHASED,
                quantity_delta=Decimal("1"),
                unit="l",
                storage_location=StorageLocation.FRIDGE,
                occurred_at=at(2),
                source="receipt_ocr",
                confidence=1.4,
                idempotency_key="bad-confidence",
            ),
        ),
        (
            "a purchase of a negative quantity",
            lambda product_id: NewInventoryEvent(
                canonical_product_id=product_id,
                event_type=EventType.PURCHASED,
                quantity_delta=Decimal("-1"),
                unit="l",
                storage_location=StorageLocation.FRIDGE,
                occurred_at=at(2),
                source="receipt_ocr",
                idempotency_key="bad-purchase",
            ),
        ),
        (
            "a consumption that adds stock",
            lambda product_id: NewInventoryEvent(
                canonical_product_id=product_id,
                event_type=EventType.CONSUMED,
                quantity_delta=Decimal("1"),
                unit="l",
                storage_location=StorageLocation.FRIDGE,
                occurred_at=at(2),
                source="receipt_ocr",
                idempotency_key="bad-consumption",
            ),
        ),
        (
            "a blank unit",
            lambda product_id: NewInventoryEvent(
                canonical_product_id=product_id,
                event_type=EventType.PURCHASED,
                quantity_delta=Decimal("1"),
                unit="   ",
                storage_location=StorageLocation.FRIDGE,
                occurred_at=at(2),
                source="receipt_ocr",
                idempotency_key="bad-unit",
            ),
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_schema_rejects_nonsense(
    db: Database, description: str, event_factory: object
) -> None:
    async def scenario(session: AsyncSession) -> None:
        household_id = await make_household(session)
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        nested = await session.begin_nested()
        try:
            with pytest.raises(IntegrityError):
                await repository.append(
                    household_id=household_id,
                    event=event_factory(product_id),  # type: ignore[operator]
                )
        finally:
            await nested.rollback()

    db.run(scenario)


def test_an_event_cannot_reference_a_household_that_does_not_exist(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        product_id = await make_product(session)
        repository = InventoryEventRepository(session)

        nested = await session.begin_nested()
        try:
            with pytest.raises(IntegrityError):
                await repository.append(
                    household_id=uuid.uuid4(),
                    event=purchase(product_id, key="orphan"),
                )
        finally:
            await nested.rollback()

    db.run(scenario)
