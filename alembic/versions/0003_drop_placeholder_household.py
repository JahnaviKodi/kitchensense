"""remove the placeholder household now that tokens decide tenancy

Revision ID: 0003_drop_placeholder
Revises: 0002_placeholder_household
Create Date: 2026-08-16

Household ids are now derived from the access token's subject, so nothing
resolves to the placeholder any more and its row is dead weight.

It is removed **only if no events reference it**. A deployment that ran the
API before authentication landed has a real kitchen record sitting under that
id, and the alternatives are both worse than leaving the row: deleting the
events destroys data the user still owns, and deleting the household alone
fails on a RESTRICT foreign key and takes the whole migration down with it —
during a deploy, on a table this migration was never meant to touch.

So it warns instead. The row is harmless: it is unreachable, because no token
can derive that id.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Alembic stores this in a varchar(32); a longer id fails at upgrade time.
revision: str = "0003_drop_placeholder"
down_revision: str | None = "0002_placeholder_household"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

HOUSEHOLD_ID = "11111111-1111-4111-8111-111111111111"

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()

    events = bind.execute(
        sa.text(
            """
            SELECT count(*) FROM inventory_events
            WHERE household_id = CAST(:id AS uuid)
            """
        ).bindparams(id=HOUSEHOLD_ID)
    ).scalar_one()

    if events:
        logger.warning(
            "Leaving the placeholder household %s in place: %d inventory event(s) "
            "still reference it. Those events predate authentication and are now "
            "unreachable, since no access token derives that household id. Migrate "
            "them to a real household, or delete them deliberately, before removing "
            "the row.",
            HOUSEHOLD_ID,
            events,
        )
        return

    result = bind.execute(
        sa.text("DELETE FROM households WHERE id = CAST(:id AS uuid)").bindparams(
            id=HOUSEHOLD_ID
        )
    )
    if result.rowcount:
        logger.info("Removed the placeholder household %s", HOUSEHOLD_ID)


def downgrade() -> None:
    # Restores what 0002 created, so downgrading past this revision lands on
    # the state 0002 left behind whether or not the row was actually removed.
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
