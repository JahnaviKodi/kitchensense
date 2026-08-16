"""The three kitchen-record endpoints, against a real Postgres.

Each test drives the real ASGI app. Only the session dependency is overridden,
so routing, validation, serialisation and the repository calls are all the
production ones — the substitution is the transaction, which is rolled back
afterwards rather than committed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kitchensense.api.dependencies import get_session
from kitchensense.config import PLACEHOLDER_HOUSEHOLD_ID
from kitchensense.domain.inventory import (
    EventType,
    NewInventoryEvent,
    StorageLocation,
)
from kitchensense.main import app
from kitchensense.repositories import InventoryEventRepository
from tests.conftest import Database, make_household, make_product

pytestmark = pytest.mark.postgres


@asynccontextmanager
async def api(session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """The real app, wired to the test's transaction."""

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = _session_override
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        app.dependency_overrides.pop(get_session, None)


def event_body(product_id: uuid.UUID, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "canonical_product_id": str(product_id),
        "event_type": "purchased",
        "quantity_delta": "2",
        "unit": "l",
        "storage_location": "fridge",
        "occurred_at": "2026-03-02T12:00:00Z",
        "source": "receipt_ocr",
        "idempotency_key": f"api-{uuid.uuid4()}",
    }
    body.update(overrides)
    return body


# ----------------------------------------------------------------------
# POST /inventory/events
# ----------------------------------------------------------------------


def test_appending_an_event_returns_it_with_both_clocks(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        product_id = await make_product(session)

        async with api(session) as client:
            response = await client.post(
                "/inventory/events",
                json=event_body(
                    product_id,
                    printed_date="2026-03-09",
                    date_label_type="use_by",
                    confidence=0.82,
                    metadata={"receipt_line": "SEMI SKIM MLK 2L"},
                ),
            )

        assert response.status_code == 201
        body = response.json()
        assert body["household_id"] == str(PLACEHOLDER_HOUSEHOLD_ID)
        assert body["quantity_delta"] == "2.000"
        assert body["date_label_type"] == "use_by"
        assert body["metadata"] == {"receipt_line": "SEMI SKIM MLK 2L"}
        assert body["occurred_at"].startswith("2026-03-02T12:00:00")
        # Stamped by the server, not the client.
        assert body["recorded_at"] is not None
        assert uuid.UUID(body["id"])

    db.run(scenario)


def test_the_event_is_actually_readable_afterwards(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        product_id = await make_product(session)

        async with api(session) as client:
            await client.post("/inventory/events", json=event_body(product_id))
            snapshot = await client.get("/inventory")

        assert snapshot.status_code == 200
        lots = snapshot.json()["lots"]
        assert len(lots) == 1
        assert lots[0]["quantity"] == "2.000"
        assert lots[0]["canonical_product_id"] == str(product_id)

    db.run(scenario)


def test_replaying_the_same_request_appends_nothing(db: Database) -> None:
    """A retried receipt upload must not double the purchase."""

    async def scenario(session: AsyncSession) -> None:
        product_id = await make_product(session)
        body = event_body(product_id)

        async with api(session) as client:
            first = await client.post("/inventory/events", json=body)
            second = await client.post("/inventory/events", json=body)
            snapshot = await client.get("/inventory")

        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert snapshot.json()["lots"][0]["quantity"] == "2.000"

    db.run(scenario)


def test_an_unknown_product_is_rejected_as_unprocessable(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        async with api(session) as client:
            response = await client.post(
                "/inventory/events", json=event_body(uuid.uuid4())
            )

        assert response.status_code == 422
        assert "canonical_product_id" in response.json()["detail"]

    db.run(scenario)


@pytest.mark.parametrize(
    ("description", "overrides"),
    [
        ("an unknown field", {"recorded_at": "2026-03-02T12:00:00Z"}),
        ("a naive occurred_at", {"occurred_at": "2026-03-02T12:00:00"}),
        ("an unknown event type", {"event_type": "eaten"}),
        ("an unknown storage location", {"storage_location": "shed"}),
        ("a confidence above one", {"confidence": 1.5}),
        ("a negative confidence", {"confidence": -0.1}),
        ("a blank unit", {"unit": ""}),
        ("a label type with no printed date", {"date_label_type": "use_by"}),
        ("a negative purchase", {"quantity_delta": "-2"}),
        ("a consumption that adds stock", {"event_type": "consumed", "quantity_delta": "2"}),
        ("more decimal places than the column holds", {"quantity_delta": "1.00005"}),
        ("a blank idempotency key", {"idempotency_key": ""}),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_the_request_model_rejects(
    db: Database, description: str, overrides: dict[str, object]
) -> None:
    """Strict validation, checked one bad request at a time.

    Every one of these is a 422 before anything touches the database — which
    is the point: the same mistakes would otherwise surface as constraint
    violations, or worse, be silently dropped.
    """

    async def scenario(session: AsyncSession) -> None:
        product_id = await make_product(session)

        async with api(session) as client:
            response = await client.post(
                "/inventory/events", json=event_body(product_id, **overrides)
            )

        assert response.status_code == 422, response.text

    db.run(scenario)


def test_recorded_at_cannot_be_set_by_the_client(db: Database) -> None:
    """The one rejection worth stating on its own.

    A client that could stamp system time could backdate its own arrival and
    walk straight through the filter that stops a snapshot of last Tuesday
    seeing what was reported on Friday.
    """

    async def scenario(session: AsyncSession) -> None:
        product_id = await make_product(session)

        async with api(session) as client:
            response = await client.post(
                "/inventory/events",
                json=event_body(product_id, recorded_at="2020-01-01T00:00:00Z"),
            )

        assert response.status_code == 422
        assert any(
            "recorded_at" in str(error.get("loc", ""))
            for error in response.json()["detail"]
        )

    db.run(scenario)


# ----------------------------------------------------------------------
# GET /inventory
# ----------------------------------------------------------------------


def test_an_empty_kitchen_is_an_empty_snapshot(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        async with api(session) as client:
            response = await client.get("/inventory")

        assert response.status_code == 200
        body = response.json()
        assert body["household_id"] == str(PLACEHOLDER_HOUSEHOLD_ID)
        assert body["lot_count"] == 0
        assert body["lots"] == []

    db.run(scenario)


def test_the_snapshot_folds_a_week_of_events(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        product_id = await make_product(session)

        async with api(session) as client:
            for event_type, delta, day in [
                ("purchased", "2", "02"),
                ("opened", "0", "03"),
                ("consumed", "-1.5", "05"),
            ]:
                response = await client.post(
                    "/inventory/events",
                    json=event_body(
                        product_id,
                        event_type=event_type,
                        quantity_delta=delta,
                        occurred_at=f"2026-03-{day}T12:00:00Z",
                    ),
                )
                assert response.status_code == 201, response.text

            snapshot = await client.get("/inventory")

        lot = snapshot.json()["lots"][0]
        assert lot["quantity"] == "0.500"
        assert lot["purchased_quantity"] == "2.000"
        assert lot["consumed_quantity"] == "1.500"
        assert lot["event_count"] == 3
        assert lot["is_opened"] is True
        assert lot["is_depleted"] is False
        assert lot["opened_at"].startswith("2026-03-03T12:00:00")

    db.run(scenario)


# ----------------------------------------------------------------------
# GET /inventory/as-of
# ----------------------------------------------------------------------


def test_a_past_snapshot_excludes_what_was_only_learned_later(db: Database) -> None:
    """The endpoint's whole reason for existing.

    The milk was binned on the 4th and reported on the 12th. A snapshot of the
    6th has to show two litres in the fridge, because on the 6th that is what
    the system believed. Anything else is tomorrow's knowledge leaking into
    yesterday's training data.
    """

    async def scenario(session: AsyncSession) -> None:
        product_id = await make_product(session)

        async with api(session) as client:
            bought = await client.post(
                "/inventory/events",
                json=event_body(product_id, occurred_at="2026-03-02T12:00:00Z"),
            )
            assert bought.status_code == 201

            # recorded_at is the server's, and the server's clock is now — so
            # the discard is, from the log's point of view, reported today.
            binned = await client.post(
                "/inventory/events",
                json=event_body(
                    product_id,
                    event_type="discarded",
                    quantity_delta="-2",
                    occurred_at="2026-03-04T12:00:00Z",
                ),
            )
            assert binned.status_code == 201

            past = await client.get(
                "/inventory/as-of", params={"timestamp": "2026-03-06T12:00:00Z"}
            )
            present = await client.get("/inventory")

        assert past.status_code == 200
        assert past.json()["as_of"].startswith("2026-03-06T12:00:00")
        # Both events occurred before the 6th, but neither was *known* then:
        # the server stamped both with today's date.
        assert past.json()["lot_count"] == 0

        assert present.json()["lots"][0]["quantity"] == "0.000"
        assert present.json()["lots"][0]["discarded_quantity"] == "2.000"

    db.run(scenario)


def test_a_past_snapshot_sees_events_already_known_then(db: Database) -> None:
    """The other half: a timestamp after the events shows them."""

    async def scenario(session: AsyncSession) -> None:
        product_id = await make_product(session)

        async with api(session) as client:
            await client.post("/inventory/events", json=event_body(product_id))
            # A minute's headroom: recorded_at comes from the database clock
            # at the start of this transaction, which is a moment ago.
            just_after = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
            response = await client.get(
                "/inventory/as-of", params={"timestamp": just_after}
            )

        assert response.status_code == 200
        assert response.json()["lot_count"] == 1

    db.run(scenario)


@pytest.mark.parametrize(
    ("description", "timestamp"),
    [
        ("a naive timestamp", "2026-03-06T12:00:00"),
        ("not a timestamp at all", "yesterday"),
        ("an empty timestamp", ""),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_as_of_rejects(db: Database, description: str, timestamp: str) -> None:
    async def scenario(session: AsyncSession) -> None:
        async with api(session) as client:
            response = await client.get(
                "/inventory/as-of", params={"timestamp": timestamp}
            )

        assert response.status_code == 422, response.text

    db.run(scenario)


def test_as_of_requires_a_timestamp(db: Database) -> None:
    async def scenario(session: AsyncSession) -> None:
        async with api(session) as client:
            response = await client.get("/inventory/as-of")

        assert response.status_code == 422

    db.run(scenario)


# ----------------------------------------------------------------------
# Tenancy
# ----------------------------------------------------------------------


def test_another_households_events_are_invisible(db: Database) -> None:
    """The API reads only the household the request resolves to.

    Hardcoded today, from a token tomorrow — either way it is the argument the
    repository requires, and nothing else can widen the query.
    """

    async def scenario(session: AsyncSession) -> None:
        product_id = await make_product(session)
        stranger = await make_household(session, "Someone else")
        await InventoryEventRepository(session).append(
            household_id=stranger,
            event=NewInventoryEvent(
                canonical_product_id=product_id,
                event_type=EventType.PURCHASED,
                quantity_delta=Decimal("99"),
                unit="l",
                storage_location=StorageLocation.FRIDGE,
                occurred_at=datetime(2026, 3, 2, tzinfo=UTC),
                source="receipt_ocr",
                idempotency_key=f"stranger-{uuid.uuid4()}",
            ),
        )

        async with api(session) as client:
            response = await client.get("/inventory")

        assert response.json()["lot_count"] == 0

    db.run(scenario)
