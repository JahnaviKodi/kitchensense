"""Blob storage. One thing lives here: issuing upload permissions."""

from kitchensense.storage.receipts import (
    ReceiptBlobStore,
    StorageUnavailableError,
    UploadTicket,
)

__all__ = [
    "ReceiptBlobStore",
    "StorageUnavailableError",
    "UploadTicket",
]
