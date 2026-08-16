"""The kitchen record over HTTP.

Three endpoints, all of which go through the repository layer with
``household_id`` passed explicitly. No SQL, no folding, no policy here — the
handlers translate between HTTP and the repositories and nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import AwareDatetime
from sqlalchemy.exc import IntegrityError

from kitchensense.api.dependencies import HouseholdDep, SessionDep
from kitchensense.api.schemas import (
    InventoryEventCreate,
    InventoryEventResponse,
    SnapshotResponse,
)
from kitchensense.repositories import (
    IdempotencyKeyConflictError,
    InventoryEventRepository,
    InventorySnapshotRepository,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])


@router.post(
    "/events",
    response_model=InventoryEventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Append an inventory event",
    responses={
        200: {"description": "This idempotency key was already used; nothing appended."},
        409: {"description": "The idempotency key belongs to another household."},
        422: {"description": "The event references something that does not exist."},
    },
)
async def append_event(
    payload: InventoryEventCreate,
    response: Response,
    session: SessionDep,
    household_id: HouseholdDep,
) -> InventoryEventResponse:
    """Append one event to the log.

    The log is append-only, so this is the *only* way the kitchen record
    changes: there is no update and no delete, and a mistake is fixed by
    appending a ``corrected`` event.

    Replaying a request with the same ``idempotency_key`` appends nothing and
    returns the original event with a 200 instead of a 201. That is what makes
    a retried receipt upload safe rather than a doubled purchase.
    """
    repository = InventoryEventRepository(session)

    try:
        event, created = await repository.append_if_new(
            household_id=household_id, event=payload.to_domain()
        )
        await session.commit()
    except IdempotencyKeyConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "That idempotency_key is already in use by another household. "
                "Keys are unique across all households, so namespace them by "
                "whatever produced them."
            ),
        ) from exc
    except IntegrityError as exc:
        # Almost always a canonical_product_id or household that does not
        # exist. The database is the only thing that can tell us, and it tells
        # us by failing the insert.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "The event could not be stored. Check that canonical_product_id "
                "refers to a product that exists."
            ),
        ) from exc

    if not created:
        response.status_code = status.HTTP_200_OK

    return InventoryEventResponse.from_domain(event)


@router.get(
    "",
    response_model=SnapshotResponse,
    summary="The current kitchen snapshot",
)
async def current_snapshot(
    session: SessionDep, household_id: HouseholdDep
) -> SnapshotResponse:
    """What is in the kitchen right now, folded from the log.

    Computed rather than read from the stored ``inventory_snapshot``
    projection, so a GET stays free of side effects and cannot serve a
    projection that a background rebuild has not caught up with. The stored
    projection exists for workloads that cannot afford the replay; this
    endpoint is not one of them.
    """
    snapshot = await InventorySnapshotRepository(session).project_known_as_of(
        household_id=household_id, as_of=datetime.now(UTC)
    )
    return SnapshotResponse.from_domain(snapshot)


@router.get(
    "/as-of",
    response_model=SnapshotResponse,
    summary="The kitchen snapshot at a past point in time",
)
async def snapshot_as_of(
    timestamp: Annotated[
        AwareDatetime,
        Query(
            description=(
                "The instant to reconstruct the kitchen at. Must include a "
                "timezone offset, for example 2026-03-05T18:30:00Z."
            ),
        ),
    ],
    session: SessionDep,
    household_id: HouseholdDep,
) -> SnapshotResponse:
    """The kitchen as the system understood it at ``timestamp``.

    Not "what we now know was true then" — *what we knew then*. An event that
    happened before ``timestamp`` but was only reported afterwards is left
    out, because on that day nobody had told us. That distinction is the whole
    point of the endpoint: it is what lets a training example for last Tuesday
    be built from Tuesday's knowledge instead of today's, and a model trained
    on the other kind scores beautifully offline and disappoints in
    production.
    """
    snapshot = await InventorySnapshotRepository(session).project_known_as_of(
        household_id=household_id, as_of=timestamp
    )
    return SnapshotResponse.from_domain(snapshot)
