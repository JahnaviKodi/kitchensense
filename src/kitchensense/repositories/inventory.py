"""Reading and writing the kitchen record.

Two invariants are enforced here by shape rather than by discipline.

**Tenancy.** ``household_id`` is a required keyword argument on every public
method. There is no default, no "current household" ambient state, and no
overload that omits it. Every statement is built by :meth:`_scoped`, which
applies the household filter before the caller gets a chance to add
predicates, so a query that spans households cannot be written without
deleting code that is here. ``tests/test_repository_contract.py`` asserts the
signature rule by introspection, so a new method that forgets it fails CI.

**Time.** :meth:`InventoryEventRepository.events_known_as_of` is the only
sanctioned way to read events for a feature computation, and it filters on
*both* clocks. See its docstring.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kitchensense.domain.inventory import (
    HouseholdMismatchError,
    InventoryEvent,
    InventorySnapshot,
    NewInventoryEvent,
    apply_events,
    empty_snapshot,
    fold_events,
    normalize_unit,
)
from kitchensense.models.inventory_event import InventoryEventRow
from kitchensense.models.inventory_snapshot import InventorySnapshotRow

__all__ = [
    "IdempotencyKeyConflictError",
    "InventoryEventRepository",
    "InventorySnapshotRepository",
]


class IdempotencyKeyConflictError(Exception):
    """An idempotency key is already in use by a *different* household.

    Keys are unique across the whole table, so they must be namespaced by
    whatever produced them (a receipt upload id, a device message id). A
    collision across households means two tenants generated the same key, and
    silently returning the other household's event would be a data leak. We
    refuse instead.
    """


class InventoryEventRepository:
    """Append-only access to ``inventory_events``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # scoping
    # ------------------------------------------------------------------

    def _scoped(self, *, household_id: uuid.UUID) -> Select[tuple[InventoryEventRow]]:
        """The only ``select`` of events in this module.

        Every read composes onto this, so the household predicate is never
        something a caller has to remember.
        """
        return select(InventoryEventRow).where(InventoryEventRow.household_id == household_id)

    @staticmethod
    def _ordered(statement: Select[tuple[InventoryEventRow]]) -> Select[tuple[InventoryEventRow]]:
        """A total order over events, so replays are reproducible.

        Ordered by system time first: that is the order the household's
        history actually became known, and the order an incremental projection
        catches up in. ``id`` breaks ties between events sharing both clocks.
        """
        return statement.order_by(
            InventoryEventRow.recorded_at,
            InventoryEventRow.occurred_at,
            InventoryEventRow.id,
        )

    async def _fetch(
        self, statement: Select[tuple[InventoryEventRow]]
    ) -> list[InventoryEvent]:
        result = await self._session.execute(self._ordered(statement))
        return [row.to_domain() for row in result.scalars().all()]

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------

    async def append(
        self, *, household_id: uuid.UUID, event: NewInventoryEvent
    ) -> InventoryEvent:
        """Append one event. Raises on a duplicate idempotency key.

        ``event`` carries no household of its own — the row's tenancy comes
        from this method's argument, so an event built in one request context
        cannot be written into another household's log.
        """
        appended = await self.append_many(household_id=household_id, events=[event])
        return appended[0]

    async def append_many(
        self, *, household_id: uuid.UUID, events: Sequence[NewInventoryEvent]
    ) -> list[InventoryEvent]:
        """Append a batch — one receipt's worth of purchases, typically."""
        if not events:
            return []
        rows = [self._to_row(household_id=household_id, event=event) for event in events]
        self._session.add_all(rows)
        await self._session.flush()
        for row in rows:
            # recorded_at is stamped by the database clock when the caller
            # did not supply one; read back what actually landed.
            await self._session.refresh(row)
        return [row.to_domain() for row in rows]

    async def append_if_new(
        self, *, household_id: uuid.UUID, event: NewInventoryEvent
    ) -> tuple[InventoryEvent, bool]:
        """Append unless the idempotency key is already present.

        Returns the event and whether it was newly appended. This is what a
        retried receipt upload or a redelivered queue message should call:
        the second delivery is a no-op rather than a doubled purchase.
        """
        values = self._to_values(household_id=household_id, event=event)
        statement = (
            pg_insert(InventoryEventRow)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["idempotency_key"])
            .returning(InventoryEventRow)
        )
        result = await self._session.execute(statement)
        row = result.scalars().first()
        if row is not None:
            return row.to_domain(), True

        existing = await self.get_by_idempotency_key(
            household_id=household_id, idempotency_key=event.idempotency_key
        )
        if existing is None:
            raise IdempotencyKeyConflictError(
                f"idempotency key {event.idempotency_key!r} is already used by "
                f"another household"
            )
        return existing, False

    def _to_row(
        self, *, household_id: uuid.UUID, event: NewInventoryEvent
    ) -> InventoryEventRow:
        values = self._to_values(household_id=household_id, event=event)
        return InventoryEventRow(**values)

    @staticmethod
    def _to_values(*, household_id: uuid.UUID, event: NewInventoryEvent) -> dict[str, Any]:
        values: dict[str, Any] = {
            "household_id": household_id,
            "canonical_product_id": event.canonical_product_id,
            "event_type": event.event_type,
            "quantity_delta": event.quantity_delta,
            "unit": normalize_unit(event.unit),
            "storage_location": event.storage_location,
            "printed_date": event.printed_date,
            "date_label_type": event.date_label_type,
            "occurred_at": event.occurred_at,
            "source": event.source,
            "confidence": event.confidence,
            "idempotency_key": event.idempotency_key,
            "event_metadata": dict(event.metadata),
        }
        if event.recorded_at is not None:
            values["recorded_at"] = event.recorded_at
        return values

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------

    async def events_known_as_of(
        self,
        *,
        household_id: uuid.UUID,
        as_of: datetime,
        canonical_product_id: uuid.UUID | None = None,
    ) -> list[InventoryEvent]:
        """Everything this household's log knew at ``as_of``. Both clocks.

        The filter is ``occurred_at <= as_of AND recorded_at <= as_of``, and
        the second half is the one that matters. Suppose a jar was thrown away
        on Monday but the household only told KitchenSense on Friday. A
        training example built for Tuesday must not see that event: on Tuesday
        the system had no idea. Filtering on ``occurred_at`` alone would let
        Friday's knowledge into Tuesday's features, the model would learn to
        predict waste from evidence it will never have at prediction time, and
        offline accuracy would look excellent right up until it shipped.

        Every feature computation, training-set build and backfill routes
        through this method. Reading ``inventory_events`` directly anywhere
        else is how that guarantee gets lost.
        """
        statement = self._scoped(household_id=household_id).where(
            InventoryEventRow.occurred_at <= as_of,
            InventoryEventRow.recorded_at <= as_of,
        )
        if canonical_product_id is not None:
            statement = statement.where(
                InventoryEventRow.canonical_product_id == canonical_product_id
            )
        return await self._fetch(statement)

    async def events_known_between(
        self, *, household_id: uuid.UUID, after: datetime, as_of: datetime
    ) -> list[InventoryEvent]:
        """Events that *became known* in ``(after, as_of]``.

        Exactly the set difference ``known_as_of(as_of) - known_as_of(after)``,
        which is what lets the snapshot advance incrementally without
        re-reading history. Note the ``OR``: an event can enter the window by
        either clock. One recorded long ago but stamped with a future
        ``occurred_at`` was excluded from the earlier snapshot and has to be
        picked up now, and a plain ``recorded_at > after`` window would lose it
        forever.
        """
        statement = self._scoped(household_id=household_id).where(
            InventoryEventRow.occurred_at <= as_of,
            InventoryEventRow.recorded_at <= as_of,
            (InventoryEventRow.occurred_at > after) | (InventoryEventRow.recorded_at > after),
        )
        return await self._fetch(statement)

    async def get_event(
        self, *, household_id: uuid.UUID, event_id: uuid.UUID
    ) -> InventoryEvent | None:
        """Fetch one event. Returns ``None`` if it belongs to another household."""
        statement = self._scoped(household_id=household_id).where(
            InventoryEventRow.id == event_id
        )
        result = await self._session.execute(statement)
        row = result.scalars().one_or_none()
        return None if row is None else row.to_domain()

    async def get_by_idempotency_key(
        self, *, household_id: uuid.UUID, idempotency_key: str
    ) -> InventoryEvent | None:
        statement = self._scoped(household_id=household_id).where(
            InventoryEventRow.idempotency_key == idempotency_key
        )
        result = await self._session.execute(statement)
        row = result.scalars().one_or_none()
        return None if row is None else row.to_domain()

    async def count_known_as_of(self, *, household_id: uuid.UUID, as_of: datetime) -> int:
        statement = self._scoped(household_id=household_id).where(
            InventoryEventRow.occurred_at <= as_of,
            InventoryEventRow.recorded_at <= as_of,
        )
        result = await self._session.execute(statement.with_only_columns(func.count()))
        return int(result.scalar_one())

    async def latest_recorded_at(
        self, *, household_id: uuid.UUID, as_of: datetime
    ) -> datetime | None:
        """The newest ``recorded_at`` visible at ``as_of``. Useful as a watermark."""
        statement = self._scoped(household_id=household_id).where(
            InventoryEventRow.occurred_at <= as_of,
            InventoryEventRow.recorded_at <= as_of,
        )
        result = await self._session.execute(
            statement.with_only_columns(func.max(InventoryEventRow.recorded_at))
        )
        return result.scalar_one_or_none()

    async def products_known_as_of(
        self, *, household_id: uuid.UUID, as_of: datetime
    ) -> list[uuid.UUID]:
        """Distinct products this household has ever touched, as of ``as_of``."""
        statement = self._scoped(household_id=household_id).where(
            InventoryEventRow.occurred_at <= as_of,
            InventoryEventRow.recorded_at <= as_of,
        )
        result = await self._session.execute(
            statement.with_only_columns(InventoryEventRow.canonical_product_id).distinct()
        )
        return list(result.scalars().all())


class InventorySnapshotRepository:
    """The derived projection. Everything here is rebuildable from the log."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._events = InventoryEventRepository(session)

    def _scoped(self, *, household_id: uuid.UUID) -> Select[tuple[InventorySnapshotRow]]:
        return select(InventorySnapshotRow).where(
            InventorySnapshotRow.household_id == household_id
        )

    async def get(self, *, household_id: uuid.UUID) -> InventorySnapshot | None:
        """The stored projection, or ``None`` if it has never been built."""
        result = await self._session.execute(self._scoped(household_id=household_id))
        rows = list(result.scalars().all())
        if not rows:
            return None
        lots = [row.to_domain() for row in rows]
        return InventorySnapshot(
            household_id=household_id,
            as_of=max(row.as_of for row in rows),
            lots=tuple(sorted(lots, key=lambda lot: lot.key.sort_key())),
        )

    async def project_known_as_of(
        self, *, household_id: uuid.UUID, as_of: datetime
    ) -> InventorySnapshot:
        """Fold the whole log into a snapshot without storing it.

        The read path for features and backtests: it never touches the stored
        projection, so asking "what did the kitchen look like last March?"
        cannot disturb — or be disturbed by — what it looks like now.
        """
        events = await self._events.events_known_as_of(household_id=household_id, as_of=as_of)
        return fold_events(events, household_id=household_id, as_of=as_of)

    async def rebuild(
        self, *, household_id: uuid.UUID, as_of: datetime
    ) -> InventorySnapshot:
        """Replay the entire log and persist the result."""
        snapshot = await self.project_known_as_of(household_id=household_id, as_of=as_of)
        await self.save(household_id=household_id, snapshot=snapshot)
        return snapshot

    async def advance(
        self, *, household_id: uuid.UUID, as_of: datetime
    ) -> InventorySnapshot:
        """Move the stored projection forward using only newly known events.

        Equivalent to :meth:`rebuild` but proportional to what changed rather
        than to the household's whole history. The equivalence holds because
        the fold is a commutative monoid and
        :meth:`InventoryEventRepository.events_known_between` returns exactly
        the set difference — see ``domain/inventory.py``. It is not an
        optimisation we hope is right; it is one the property test checks.
        """
        current = await self.get(household_id=household_id)
        if current is None:
            return await self.rebuild(household_id=household_id, as_of=as_of)
        if as_of < current.as_of:
            return await self.project_known_as_of(household_id=household_id, as_of=as_of)

        new_events = await self._events.events_known_between(
            household_id=household_id, after=current.as_of, as_of=as_of
        )
        snapshot = apply_events(current, new_events, as_of=as_of)
        await self.save(household_id=household_id, snapshot=snapshot)
        return snapshot

    async def save(
        self, *, household_id: uuid.UUID, snapshot: InventorySnapshot
    ) -> None:
        """Replace this household's projection wholesale.

        Delete-then-insert inside the caller's transaction. A lot that has
        vanished from the fold — because a ``corrected`` event moved it to a
        different printed date, say — must not survive as a stale row, and
        rewriting the household's handful of lots is cheaper than working out
        which ones those are.
        """
        if snapshot.household_id != household_id:
            raise HouseholdMismatchError(
                f"snapshot belongs to household {snapshot.household_id}, not {household_id}"
            )

        await self._session.execute(
            delete(InventorySnapshotRow).where(
                InventorySnapshotRow.household_id == household_id
            )
        )
        if not snapshot.lots:
            return

        self._session.add_all(
            InventorySnapshotRow(
                household_id=household_id,
                canonical_product_id=lot.key.canonical_product_id,
                storage_location=lot.key.storage_location,
                unit=lot.key.unit,
                printed_date=lot.key.printed_date,
                date_label_type=lot.key.date_label_type,
                quantity=lot.quantity,
                purchased_quantity=lot.purchased_quantity,
                consumed_quantity=lot.consumed_quantity,
                discarded_quantity=lot.discarded_quantity,
                event_count=lot.event_count,
                first_occurred_at=lot.first_occurred_at,
                last_occurred_at=lot.last_occurred_at,
                last_recorded_at=lot.last_recorded_at,
                opened_at=lot.opened_at,
                as_of=snapshot.as_of,
            )
            for lot in snapshot.lots
        )
        await self._session.flush()

    async def clear(self, *, household_id: uuid.UUID) -> None:
        """Drop the projection. The log is untouched, so it can be rebuilt."""
        await self._session.execute(
            delete(InventorySnapshotRow).where(
                InventorySnapshotRow.household_id == household_id
            )
        )

    @staticmethod
    def empty(*, household_id: uuid.UUID, as_of: datetime) -> InventorySnapshot:
        return empty_snapshot(household_id=household_id, as_of=as_of)
