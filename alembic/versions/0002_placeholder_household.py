"""seed the placeholder household the API writes into until auth exists

Revision ID: 0002_placeholder_household
Revises: 0001_kitchen_record
Create Date: 2026-08-15

Every request currently resolves to one hardcoded household — see
``PLACEHOLDER_HOUSEHOLD_ID`` in ``kitchensense/config.py``. Events carry a
foreign key to ``households``, so without a row here the very first POST fails
on a constraint violation rather than working.

TODO(auth): delete this migration's row once Entra External ID is wired up and
households are created through a real sign-up flow. It is data, not schema, so
removing it is a one-line follow-up migration.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_placeholder_household"
down_revision: str | None = "0001_kitchen_record"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HOUSEHOLD_ID = "11111111-1111-4111-8111-111111111111"


def upgrade() -> None:
    # ON CONFLICT DO NOTHING so the migration is safe to re-run against a
    # database where the row was created some other way.
    op.execute(
        sa.text(
            """
            INSERT INTO households (id, name, timezone)
            VALUES (CAST(:id AS uuid), :name, :timezone)
            ON CONFLICT (id) DO NOTHING
            """
        ).bindparams(
            id=HOUSEHOLD_ID,
            name="Placeholder household",
            timezone="Europe/London",
        )
    )


def downgrade() -> None:
    # Only if nothing references it. Events hold a RESTRICT foreign key to
    # households, and deleting the row out from under a real kitchen record
    # would fail the migration halfway through — better to leave the row than
    # to break the downgrade.
    op.execute(
        sa.text(
            """
            DELETE FROM households
            WHERE id = CAST(:id AS uuid)
              AND NOT EXISTS (
                  SELECT 1 FROM inventory_events
                  WHERE household_id = CAST(:id AS uuid)
              )
            """
        ).bindparams(id=HOUSEHOLD_ID)
    )
