# 005 — Receipt uploads use a user delegation SAS, not an account key

## Decision
`POST /uploads` returns a **user delegation SAS**: signed with a key obtained
from Azure Storage over Entra using the container app's managed identity,
granting `write` on exactly one blob for five minutes. The blob name is
generated server-side. The storage account is deployed with
`allowSharedKeyAccess: false`, so the account keys cannot be used at all.

The image is uploaded by the client directly to blob storage. The API never
carries it.

## Reasoning

### Why not upload through the API
A receipt photograph is a few megabytes, and the container app runs on 0.25
CPU and 0.5 GiB with a maximum of two replicas. Proxying the bytes would mean
buffering them in a process sized for JSON, and paying for the transfer twice.
It would also put a slow, size-unbounded request in front of an endpoint whose
readiness probe has a five-second timeout.

### Why not an account-key SAS
Signing a SAS with a storage account key is the common approach and the wrong
one here. An account key is:

- **Permanent.** It does not expire.
- **Unscoped.** It authorises every operation on every container in the
  account.
- **Effectively unrevocable.** The only remedy for a leak is rotating the key,
  which breaks every other holder of it at the same moment.

Holding one in the application would mean the API possessed a credential far
stronger than anything it does, read from a place — the vault, an environment
variable — where it would sit indefinitely.

A user delegation key is signed by Entra instead. It expires on its own, it
grants no more than the delegating identity itself holds, and revoking the
identity's role assignment invalidates every SAS derived from it retroactively.
Nothing in this system reads an account key, and with shared key access
disabled on the account, nothing *can* — including whatever gets added to this
codebase later.

### Why the blob name is not the client's
It is derived from the household id (which comes from the validated token, per
[decision 004](004-household-per-user.md)) and a UUID generated at the moment
of the request. The request model forbids extra fields, so a `filename`,
`path` or `blob_name` is a 422 rather than something quietly ignored.

Both components are UUIDs, so the resulting name cannot contain `..`, a
backslash or a query string — that is a property of the type, not of a
sanitising pass that a later refactor could delete. A client that could steer
the name could aim an upload at another household's prefix or overwrite an
existing receipt.

### Why a row is written before the upload happens
`receipt_uploads` records the request at the moment the URL is issued, not when
the client says the file arrived. That ordering is what makes `confirm`
meaningful: a confirmation is matched against a request the server knows it
made, for the household that made it. Without the row, `confirm` would be the
client asserting something about a blob, and the API taking its word.

It also gives the abandoned case a name. An upload that was authorised and
never delivered is a row with `confirmed_at IS NULL` past its `expires_at` —
findable with an index, rather than reconstructed by listing a container and
diffing it against the database.

### Five minutes
The client already holds the file when the URL is issued; the upload starts
immediately. A longer window buys nothing and widens the period in which a URL
captured from a log, a crash report or a proxy is still usable. The delegation
key it is signed from is cached for an hour and retired a full five minutes
early, so no URL can outlive the key behind it.

## Consequences

- The client makes three calls, not one: request, PUT, confirm. A client that
  crashes after the PUT leaves a blob and an unconfirmed row; both are visible
  and the blob is deleted by the lifecycle rule within thirty days.
- Confirmation is the client's assertion that the PUT succeeded. It is not
  proof the blob exists — nothing here reads back from storage. That check
  belongs with whatever consumes the receipt, which does not exist yet.
- Blobs are deleted 30 days after last modification by a storage lifecycle
  policy. The receipt image is transient; the events extracted from it are the
  record.
- **Nothing consumes a confirmed upload yet.** There is no Event Grid
  subscription, no queue and no worker. `confirm` records a fact and stops.
