# KitchenSense

An autonomous agent that reduces household food waste. Reads a photographed
grocery receipt, predicts which items are likely to be wasted, and sends one
message with a recipe before they are.

## Running locally

    pip install -e ".[dev]"
    docker compose up -d db
    export DATABASE_URL=postgresql://postgres:local@localhost:5432/kitchensense
    alembic upgrade head
    uvicorn kitchensense.main:app --reload

Then open http://localhost:8000/docs

`DATABASE_URL` is optional. Without it the app looks the connection string up
in Key Vault, which needs a managed identity and so only works in Azure — but
it still starts, and says so on `/health/deep`.

## API

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `POST /inventory/events` | bearer | append an event to the kitchen record |
| `GET /inventory` | bearer | the current snapshot |
| `GET /inventory/as-of?timestamp=` | bearer | the snapshot at a past instant |
| `POST /uploads` | bearer | a five-minute, write-only URL for one receipt image |
| `POST /uploads/{id}/confirm` | bearer | record that the upload finished |
| `GET /health` | open | liveness; never touches the database |
| `GET /health/deep` | open | reports database reachability, always 200 |

`POST /inventory/events` is idempotent on `idempotency_key`: replaying a
request returns the original event with a 200 rather than a 201, so a retried
receipt upload is not a doubled purchase.

`GET /inventory/as-of` answers *what the system knew then*, not what we now
know was true then. An event that happened before the timestamp but was only
reported afterwards is left out — see the kitchen record below.

## Receipt uploads

The image never passes through the API. `POST /uploads` returns a **user
delegation SAS**: write-only, one blob, five minutes. The client PUTs the file
straight to blob storage and then calls `POST /uploads/{id}/confirm`.

The blob name is generated server-side from the household and a fresh id — the
request body accepts a `content_type` and nothing else, and any other field is
a 422. A row is written when the URL is issued, so a confirmation can be
matched to a request that actually happened, and an upload that was promised
and never delivered is a row that can be found rather than a blob nobody knows
about.

The SAS is signed with a key obtained over Entra using the container app's
managed identity. **The storage account key is never read** — the account is
deployed with shared key access disabled, so nothing could read one. See
[decision 005](docs/decisions/005-receipt-upload-sas.md).

Extraction of a confirmed receipt is not implemented yet: this is the upload
path only.

## Authentication

The inventory endpoints require an Entra External ID access token carrying the
`inventory.readwrite` scope. The signature, issuer, audience, expiry and
not-before are all checked; the issuer and signing keys are discovered from the
tenant's OpenID configuration and cached, refreshing when a token arrives
signed by a key id we have not seen. Any failure is a 401 with a
`WWW-Authenticate` challenge. `/health` and `/health/deep` are open, so the
container's probes work whatever the tenant is doing.

Three environment variables configure it, supplied by `infra/main.bicep`:
`ENTRA_TENANT_ID`, `ENTRA_CLIENT_ID` and `ENTRA_AUDIENCE`. **None has a
default.** Unset, every protected endpoint answers 503 rather than the app
guessing at which directory to trust.

`household_id` is derived from the token's subject and the issuer, so there is
no request field naming a household and nothing to tamper with. The household
row is created the first time a subject is seen. This means **one household per
user — a shared kitchen is not supported**; see
[decision 004](docs/decisions/004-household-per-user.md).

## Project structure

    src/kitchensense/api/           HTTP endpoints
    src/kitchensense/domain/        pure business logic, no I/O
    src/kitchensense/models/        SQLAlchemy models
    src/kitchensense/repositories/  data access, scoped to one household
    src/kitchensense/db/            engine and session wiring
    alembic/                        database migrations
    docs/diagrams/                  process flow figures
    docs/decisions/                 technical decision records
    infra/                          Azure infrastructure as code

## The kitchen record

`inventory_events` is the system of record: append-only, never updated, and
bitemporal. Each event knows both when it happened in the kitchen
(`occurred_at`) and when the system learned of it (`recorded_at`), which is
what lets a feature computed for last Tuesday see only what was knowable last
Tuesday. `inventory_snapshot` is a projection folded from those events, and
can be dropped and rebuilt at any time.

Read events through `InventoryEventRepository.events_known_as_of` — it filters
on both clocks. See [decision 003](docs/decisions/003-bitemporal-event-log.md)
for why that matters.

## Database

Migrations run through Alembic, which reads `KITCHENSENSE_DATABASE_URL` (or
`DATABASE_URL`):

    alembic upgrade head

### Where the connection string comes from

`DATABASE_URL` if it is set, otherwise the `postgres-connection-string` secret
in Key Vault, read with the container app's user-assigned managed identity.
`infra/main.bicep` supplies `KEY_VAULT_URI`, `AZURE_CLIENT_ID` and
`POSTGRES_SECRET_NAME` to the container, so no password is stored in the
template, the container definition or the deployment history.

Both are normalised on the way in: onto the `asyncpg` driver, and `sslmode`
(which asyncpg does not accept) is translated to `ssl`. Azure's connection
string ends in `?sslmode=require` and fails without that.

### It starts without a database

The PostgreSQL server is stopped outside working hours, so nothing opens a
connection at startup. The app comes up, `/health` answers, `/health/deep`
reports `unreachable` while still returning 200, and only the endpoints that
need rows return 503. The liveness probe points at `/health` for exactly this
reason: a probe that failed with the database would have Azure restarting a
healthy container all evening, for something restarting cannot fix.

## Tests

    pytest

The data-layer tests start a real Postgres with testcontainers and apply the
real migration, so Docker needs to be running. Without it they skip rather
than fail.