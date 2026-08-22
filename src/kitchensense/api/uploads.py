"""Receipt uploads over HTTP.

Two endpoints, and between them one idea: the API never carries the image.
The client asks for permission, uploads straight to blob storage, and comes
back to say it is done. The bytes go from the phone to Azure without passing
through a container app that would otherwise have to buffer megabytes of
photograph per request.

What the API keeps is the paperwork. A row is written when permission is
granted, and updated when the client confirms, so a confirmation can be
matched to a request that actually happened and an upload that never arrived
is a row that can be found later rather than a blob nobody knows about.

The upload path only. What happens to a confirmed receipt — extraction, the
events it turns into — is not here and is not implied by anything here.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status

from kitchensense.api.dependencies import HouseholdDep, ReceiptStoreDep, SessionDep
from kitchensense.api.schemas import (
    UploadConfirmation,
    UploadRequest,
    UploadTicketResponse,
)
from kitchensense.api.security import get_principal
from kitchensense.repositories import ReceiptUploadRepository

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/uploads",
    tags=["uploads"],
    # On the router, not the handlers, exactly as the inventory router does
    # it: router dependencies run first, so authentication is structural
    # rather than a consequence of where a parameter sits in a signature, and
    # an endpoint added here later cannot forget to opt in.
    dependencies=[Depends(get_principal)],
    responses={
        401: {"description": "No access token, or one that did not validate."},
        503: {
            "description": (
                "The database, the tenant configuration or the storage account "
                "is unavailable."
            )
        },
    },
)

UploadIdPath = Annotated[
    uuid.UUID,
    Path(description="The id returned when the upload URL was issued."),
]


@router.post(
    "",
    response_model=UploadTicketResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Ask for somewhere to put a receipt",
)
async def request_upload(
    payload: UploadRequest,
    session: SessionDep,
    household_id: HouseholdDep,
    store: ReceiptStoreDep,
) -> UploadTicketResponse:
    """Issue a write-only URL for one new receipt blob.

    The URL is a user delegation SAS: signed with a key obtained over Entra
    with the app's managed identity, never with a storage account key. It
    permits ``write`` and nothing else, it names exactly one blob, and it
    expires in five minutes.

    **The blob name is generated here, and there is no request field that can
    reach it.** It is derived from the household — which comes from the
    validated token, not from anything the caller sent — and a fresh id
    generated on this line. A client cannot choose where its file lands, write
    over an existing receipt, or aim at another household's prefix.

    The row is written before the response is returned and committed only once
    the URL exists, so there is no state in which a client holds a URL the
    database has no record of.
    """
    upload_id = uuid.uuid4()
    requested_at = datetime.now(UTC)

    # Signed first. If storage refuses, the transaction is rolled back by the
    # session dependency and no row is left describing an upload that was
    # never authorised.
    ticket = await store.upload_ticket(
        household_id=household_id,
        upload_id=upload_id,
        requested_at=requested_at,
        content_type=payload.content_type,
    )

    upload = await ReceiptUploadRepository(session).record_request(
        household_id=household_id,
        upload_id=upload_id,
        blob_name=ticket.blob_name,
        content_type=str(ticket.content_type),
        requested_at=requested_at,
        expires_at=ticket.expires_at,
    )
    await session.commit()

    # The blob name, not the URL: the URL contains the SAS token, and a
    # credential in a log file is a credential.
    logger.info(
        "Issued an upload URL for %s, expiring %s",
        upload.blob_name,
        ticket.expires_at.isoformat(),
    )

    return UploadTicketResponse(
        upload_id=upload.id,
        household_id=upload.household_id,
        blob_name=upload.blob_name,
        content_type=payload.content_type,
        upload_url=ticket.url,
        method=ticket.method,
        headers=ticket.headers,
        expires_at=ticket.expires_at,
    )


@router.post(
    "/{upload_id}/confirm",
    response_model=UploadConfirmation,
    summary="Report that a receipt finished uploading",
    responses={
        404: {"description": "This household requested no such upload."},
    },
)
async def confirm_upload(
    upload_id: UploadIdPath,
    session: SessionDep,
    household_id: HouseholdDep,
) -> UploadConfirmation:
    """Record that the client's PUT to the SAS URL succeeded.

    Only an upload *this household* requested can be confirmed. An id that
    belongs to nobody and an id that belongs to somebody else both come back
    as 404, with the same wording: distinguishing them would tell a caller
    that an id they guessed is real, which is not something they should be
    able to learn.

    Confirming twice is not an error — it returns the original timestamp — so
    a client whose response was dropped can safely retry.
    """
    upload = await ReceiptUploadRepository(session).confirm(
        household_id=household_id, upload_id=upload_id, at=datetime.now(UTC)
    )

    if upload is None:
        # Logged at info, not warning: the ordinary cause is a client
        # retrying against a deployment that lost the row, not an attack.
        logger.info(
            "Refused a confirmation for upload %s: household %s never requested it",
            upload_id,
            household_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                "No upload was requested with that id. Ask for an upload URL "
                "first, then confirm the id it returns."
            ),
        )

    await session.commit()
    return UploadConfirmation.from_domain(upload)
