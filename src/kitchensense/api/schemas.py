"""Request and response models.

Validation is strict in both directions: unknown fields are rejected rather
than ignored, every constraint the database enforces is also checked here so a
bad request comes back as a 422 instead of a 500, and the mapping to and from
the domain types lives in these classes so the routers stay thin.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from kitchensense.domain.inventory import (
    DateLabelType,
    EventType,
    InventoryEvent,
    InventorySnapshot,
    LotState,
    NewInventoryEvent,
    StorageLocation,
)
from kitchensense.domain.receipts import ReceiptContentType, ReceiptUpload

__all__ = [
    "DeepHealthResponse",
    "HealthResponse",
    "InventoryEventCreate",
    "InventoryEventResponse",
    "LotResponse",
    "RootResponse",
    "SnapshotResponse",
    "UploadConfirmation",
    "UploadRequest",
    "UploadTicketResponse",
]

# extra="forbid" is the strictness that matters most in a write API: a
# misspelled "quantity" silently dropped is a receipt recorded with the wrong
# amount, and nothing downstream would ever notice.
STRICT = ConfigDict(extra="forbid", str_strip_whitespace=True)


class InventoryEventCreate(BaseModel):
    """One thing that happened to one product.

    Note what is *not* here: ``recorded_at``. System time is the server's to
    stamp. A client that could set it could backdate its own arrival and walk
    straight through the bitemporal filter that stops training data seeing the
    future — so the field is not accepted, and ``extra="forbid"`` means sending
    it anyway is a 422 rather than a silent no-op.
    """

    model_config = STRICT

    canonical_product_id: uuid.UUID
    event_type: EventType
    # Signed: positive adds stock, negative removes it. Bounded to the
    # column's precision so an over-long decimal is a 422 and not a database
    # error rolled back mid-request.
    quantity_delta: Decimal = Field(max_digits=14, decimal_places=3)
    unit: str = Field(min_length=1, max_length=24)
    storage_location: StorageLocation
    # Timezone-required. A naive timestamp is an unanswerable question once
    # the clocks change, and this one drives every expiry calculation.
    occurred_at: AwareDatetime
    source: str = Field(min_length=1, max_length=64)
    # Client-supplied, and required: it is what makes a retried receipt upload
    # or a redelivered queue message safe. Namespace it by whatever produced
    # it — keys are unique across the whole table.
    idempotency_key: str = Field(min_length=1, max_length=200)

    printed_date: date | None = None
    date_label_type: DateLabelType | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_label_has_a_date(self) -> Self:
        if self.date_label_type is not None and self.printed_date is None:
            raise ValueError(
                "date_label_type needs a printed_date: a 'use by' with no date "
                "on it says nothing"
            )
        return self

    @model_validator(mode="after")
    def _check_delta_matches_event_type(self) -> Self:
        """Mirror the database's sign constraints, for a readable error.

        Without this the same mistake still gets caught — by a CHECK
        constraint, as a 500 with an SQL fragment in the logs.
        """
        if self.event_type is EventType.PURCHASED and self.quantity_delta < 0:
            raise ValueError(
                "a purchased event cannot have a negative quantity_delta; "
                "record a return as a 'corrected' event"
            )
        if (
            self.event_type in (EventType.CONSUMED, EventType.DISCARDED)
            and self.quantity_delta > 0
        ):
            raise ValueError(
                f"a {self.event_type.value} event cannot have a positive "
                "quantity_delta; it takes stock out, so the delta is negative"
            )
        return self

    def to_domain(self) -> NewInventoryEvent:
        """No household_id: the repository supplies that from its own argument."""
        return NewInventoryEvent(
            canonical_product_id=self.canonical_product_id,
            event_type=self.event_type,
            quantity_delta=self.quantity_delta,
            unit=self.unit,
            storage_location=self.storage_location,
            printed_date=self.printed_date,
            date_label_type=self.date_label_type,
            occurred_at=self.occurred_at,
            source=self.source,
            confidence=self.confidence,
            idempotency_key=self.idempotency_key,
            metadata=self.metadata,
        )


class InventoryEventResponse(BaseModel):
    """An event as it now sits in the log, immutably."""

    model_config = STRICT

    id: uuid.UUID
    household_id: uuid.UUID
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
    idempotency_key: str
    metadata: dict[str, Any]

    @classmethod
    def from_domain(cls, event: InventoryEvent) -> Self:
        return cls(
            id=event.id,
            household_id=event.household_id,
            canonical_product_id=event.canonical_product_id,
            event_type=event.event_type,
            quantity_delta=event.quantity_delta,
            unit=event.unit,
            storage_location=event.storage_location,
            printed_date=event.printed_date,
            date_label_type=event.date_label_type,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            source=event.source,
            confidence=event.confidence,
            idempotency_key=event.idempotency_key,
            metadata=dict(event.metadata),
        )


class LotResponse(BaseModel):
    """One lot: a product, in a place, in a unit, under a printed date.

    The grain matters — two tubs of the same yoghurt with different use-by
    dates are two lots, because only one of them is about to be wasted.
    """

    model_config = STRICT

    canonical_product_id: uuid.UUID
    storage_location: StorageLocation
    unit: str
    printed_date: date | None
    date_label_type: DateLabelType | None

    quantity: Decimal
    purchased_quantity: Decimal
    consumed_quantity: Decimal
    discarded_quantity: Decimal
    event_count: int

    first_occurred_at: datetime
    last_occurred_at: datetime
    last_recorded_at: datetime
    opened_at: datetime | None

    is_opened: bool
    is_depleted: bool

    @classmethod
    def from_domain(cls, lot: LotState) -> Self:
        return cls(
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
            is_opened=lot.is_opened,
            is_depleted=lot.is_depleted,
        )


class SnapshotResponse(BaseModel):
    """What the household had, as far as the system knew at ``as_of``.

    ``as_of`` is echoed back rather than left implicit: for the historical
    endpoint it is the caller's timestamp, and for the current one it is the
    server's clock at the moment of the read. A snapshot without the instant
    it was taken at is not interpretable.
    """

    model_config = STRICT

    household_id: uuid.UUID
    as_of: datetime
    lot_count: int
    lots: list[LotResponse]

    @classmethod
    def from_domain(cls, snapshot: InventorySnapshot) -> Self:
        return cls(
            household_id=snapshot.household_id,
            as_of=snapshot.as_of,
            lot_count=len(snapshot.lots),
            lots=[LotResponse.from_domain(lot) for lot in snapshot.lots],
        )


class UploadRequest(BaseModel):
    """What a client may say about a receipt it is about to upload.

    Note how little that is. There is no filename, no path, no blob name and
    no household — every one of those is decided by the server, and
    ``extra="forbid"`` means a client that sends one anyway gets a 422 rather
    than having it quietly ignored. That is deliberate: a field that is
    ignored today is a field somebody wires up by accident in six months.
    """

    model_config = STRICT

    # What the client intends to send, from a closed set. Recorded, and
    # echoed back as the Content-Type header to use; it does not reach the
    # blob name.
    content_type: ReceiptContentType = ReceiptContentType.JPEG


class UploadTicketResponse(BaseModel):
    """Permission to upload one receipt, and the address to send it to.

    ``upload_url`` carries the SAS token in its query string, so it is a
    credential: it is write-only, it names one blob, and it stops working at
    ``expires_at``. It is returned to the client that asked for it and is not
    logged anywhere.
    """

    model_config = STRICT

    upload_id: uuid.UUID
    household_id: uuid.UUID
    # Returned so a client can correlate, and so the confirm step has
    # something to show a human when it goes wrong. Knowing the name grants
    # nothing on its own — the container is private.
    blob_name: str
    content_type: ReceiptContentType
    upload_url: str
    method: str
    # x-ms-blob-type, which Azure requires on the PUT and which a client has
    # no way of guessing. Sent rather than documented.
    headers: dict[str, str]
    expires_at: datetime


class UploadConfirmation(BaseModel):
    """An upload the client has told us arrived."""

    model_config = STRICT

    upload_id: uuid.UUID
    household_id: uuid.UUID
    blob_name: str
    content_type: str
    requested_at: datetime
    expires_at: datetime
    confirmed_at: datetime

    @classmethod
    def from_domain(cls, upload: ReceiptUpload) -> Self:
        if upload.confirmed_at is None:  # pragma: no cover - defensive
            raise ValueError("an unconfirmed upload has no confirmation to report")
        return cls(
            upload_id=upload.id,
            household_id=upload.household_id,
            blob_name=upload.blob_name,
            content_type=upload.content_type,
            requested_at=upload.requested_at,
            expires_at=upload.expires_at,
            confirmed_at=upload.confirmed_at,
        )


class HealthResponse(BaseModel):
    model_config = STRICT

    status: str


class DeepHealthResponse(BaseModel):
    """Reports the database without depending on it.

    Always 200. The container's liveness and readiness probes point at
    ``/health``, and a deep check that returned 503 for a stopped database
    would get the app restarted for a condition restarting cannot fix.
    """

    model_config = STRICT

    status: str
    database: str
    database_url_source: str
    detail: str | None = None


class RootResponse(BaseModel):
    model_config = STRICT

    service: str
    docs: str
