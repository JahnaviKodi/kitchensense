# KitchenSense

An autonomous agent that reduces household food waste. Reads a photographed
grocery receipt, predicts which items are likely to be wasted, and sends one
message with a recipe before they are.

## Running locally

    pip install -e ".[dev]"
    uvicorn kitchensense.main:app --reload

Then open http://localhost:8000/health

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

    docker compose up -d db
    export DATABASE_URL=postgresql://postgres:local@localhost:5432/kitchensense
    alembic upgrade head

## Tests

    pytest

The data-layer tests start a real Postgres with testcontainers and apply the
real migration, so Docker needs to be running. Without it they skip rather
than fail.