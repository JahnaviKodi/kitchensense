"""kitchen record: households, canonical products, event log, snapshot projection

Revision ID: 0001_kitchen_record
Revises:
Create Date: 2026-08-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_kitchen_record"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# create_type=False everywhere: the types are created once, explicitly, at the
# top of upgrade(). Left to itself SQLAlchemy would try to CREATE TYPE again
# for the second table that uses one.
EVENT_TYPE = postgresql.ENUM(
    "purchased",
    "opened",
    "consumed",
    "discarded",
    "corrected",
    "moved",
    name="inventory_event_type",
    create_type=False,
)
DATE_LABEL_TYPE = postgresql.ENUM(
    "use_by",
    "best_before",
    name="date_label_type",
    create_type=False,
)
STORAGE_LOCATION = postgresql.ENUM(
    "pantry",
    "fridge",
    "freezer",
    "counter",
    "other",
    name="storage_location",
    create_type=False,
)

# inventory_events is the system of record and is never rewritten. Application
# code could simply choose not to issue UPDATE or DELETE, but "could" is doing
# a lot of work there — a psql session, an ORM cascade or a well-meaning
# cleanup script would all bypass it. Statement-level triggers refuse at the
# only layer everything goes through, and fire even for a statement that
# happens to match no rows.
APPEND_ONLY_FUNCTION = """
CREATE OR REPLACE FUNCTION kitchensense_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        '% on % is not permitted: the kitchen record is append-only. '
        'Correct a mistake by appending a ''corrected'' event.',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""


# Check constraints are named in their short form ("name_not_blank", not
# "ck_households_name_not_blank"). Alembic builds these tables against
# Base.metadata's naming convention, whose "ck" rule already prepends
# "ck_%(table_name)s_"; spelling the prefix out here produces
# "ck_households_ck_households_name_not_blank", truncated to 63 characters with
# a hash on the end. Every other constraint kind takes its name verbatim,
# because only the "ck" rule interpolates the given name.


def upgrade() -> None:
    bind = op.get_bind()
    EVENT_TYPE.create(bind, checkfirst=True)
    DATE_LABEL_TYPE.create(bind, checkfirst=True)
    STORAGE_LOCATION.create(bind, checkfirst=True)

    op.create_table(
        "households",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_households"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="name_not_blank"),
        sa.CheckConstraint(
            "length(btrim(timezone)) > 0", name="timezone_not_blank"
        ),
    )

    op.create_table(
        "canonical_products",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("canonical_name", sa.String(length=200), nullable=False),
        sa.Column("brand", sa.String(length=120), nullable=True),
        sa.Column("category", sa.String(length=80), nullable=True),
        sa.Column("default_unit", sa.String(length=24), nullable=False),
        sa.Column("typical_shelf_life_days", sa.Integer(), nullable=True),
        sa.Column("gtin", sa.String(length=14), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_canonical_products"),
        sa.CheckConstraint(
            "length(btrim(canonical_name)) > 0",
            name="canonical_name_not_blank",
        ),
        sa.CheckConstraint(
            "typical_shelf_life_days IS NULL OR typical_shelf_life_days > 0",
            name="shelf_life_positive",
        ),
        sa.CheckConstraint(
            "gtin IS NULL OR gtin ~ '^[0-9]{8,14}$'",
            name="gtin_numeric",
        ),
    )
    op.create_index(
        "uq_canonical_products_gtin", "canonical_products", ["gtin"], unique=True
    )
    op.create_index("ix_canonical_products_category", "canonical_products", ["category"])

    op.create_table(
        "inventory_events",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("household_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canonical_product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", EVENT_TYPE, nullable=False),
        sa.Column("quantity_delta", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column("unit", sa.String(length=24), nullable=False),
        sa.Column("storage_location", STORAGE_LOCATION, nullable=False),
        sa.Column("printed_date", sa.Date(), nullable=True),
        sa.Column("date_label_type", DATE_LABEL_TYPE, nullable=True),
        # occurred_at is kitchen time, recorded_at is system time. Both are
        # needed to reconstruct what was knowable at any past instant.
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column(
            "confidence", sa.Float(), server_default=sa.text("1.0"), nullable=False
        ),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_inventory_events"),
        # RESTRICT, not CASCADE: erasing a household has to be a deliberate
        # operation that also disables the append-only triggers, not something
        # a stray DELETE can trigger.
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name="fk_inventory_events_household_id_households",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_product_id"],
            ["canonical_products.id"],
            name="fk_inventory_events_canonical_product_id_canonical_products",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "quantity_delta >= 0 OR event_type <> 'purchased'",
            name="purchased_delta_not_negative",
        ),
        sa.CheckConstraint(
            "quantity_delta <= 0 OR event_type NOT IN ('consumed', 'discarded')",
            name="removal_delta_not_positive",
        ),
        sa.CheckConstraint(
            "date_label_type IS NULL OR printed_date IS NOT NULL",
            name="label_type_needs_printed_date",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="confidence_is_a_probability",
        ),
        sa.CheckConstraint(
            "length(btrim(unit)) > 0", name="unit_not_blank"
        ),
        sa.CheckConstraint(
            "length(btrim(source)) > 0", name="source_not_blank"
        ),
        sa.CheckConstraint(
            "length(btrim(idempotency_key)) > 0",
            name="idempotency_key_not_blank",
        ),
    )
    op.create_index(
        "uq_inventory_events_idempotency_key",
        "inventory_events",
        ["idempotency_key"],
        unique=True,
    )
    op.create_index(
        "ix_inventory_events_household_id_recorded_at_occurred_at",
        "inventory_events",
        ["household_id", "recorded_at", "occurred_at"],
    )
    op.create_index(
        "ix_inventory_events_household_id_occurred_at_recorded_at",
        "inventory_events",
        ["household_id", "occurred_at", "recorded_at"],
    )
    op.create_index(
        "ix_inventory_events_household_product_occurred_at",
        "inventory_events",
        ["household_id", "canonical_product_id", "occurred_at"],
    )

    op.execute(APPEND_ONLY_FUNCTION)
    op.execute(
        "CREATE TRIGGER inventory_events_no_update BEFORE UPDATE ON inventory_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION kitchensense_reject_mutation()"
    )
    op.execute(
        "CREATE TRIGGER inventory_events_no_delete BEFORE DELETE ON inventory_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION kitchensense_reject_mutation()"
    )
    op.execute(
        "CREATE TRIGGER inventory_events_no_truncate BEFORE TRUNCATE ON inventory_events "
        "FOR EACH STATEMENT EXECUTE FUNCTION kitchensense_reject_mutation()"
    )

    op.create_table(
        "inventory_snapshot",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("household_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("canonical_product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_location", STORAGE_LOCATION, nullable=False),
        sa.Column("unit", sa.String(length=24), nullable=False),
        sa.Column("printed_date", sa.Date(), nullable=True),
        sa.Column("date_label_type", DATE_LABEL_TYPE, nullable=True),
        sa.Column("quantity", sa.Numeric(precision=14, scale=3), nullable=False),
        sa.Column(
            "purchased_quantity", sa.Numeric(precision=14, scale=3), nullable=False
        ),
        sa.Column(
            "consumed_quantity", sa.Numeric(precision=14, scale=3), nullable=False
        ),
        sa.Column(
            "discarded_quantity", sa.Numeric(precision=14, scale=3), nullable=False
        ),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("first_occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "computed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_inventory_snapshot"),
        # CASCADE here, unlike the log: this table is derived, so dropping it
        # with the household loses nothing.
        sa.ForeignKeyConstraint(
            ["household_id"],
            ["households.id"],
            name="fk_inventory_snapshot_household_id_households",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["canonical_product_id"],
            ["canonical_products.id"],
            name="fk_inventory_snapshot_canonical_product_id_canonical_products",
            ondelete="RESTRICT",
        ),
        # NULLS NOT DISTINCT (Postgres 15+): printed_date and date_label_type
        # are part of the lot key and are legitimately NULL for unlabelled
        # produce, so two NULL-dated rows for the same product are the same
        # lot, not two.
        sa.UniqueConstraint(
            "household_id",
            "canonical_product_id",
            "storage_location",
            "unit",
            "printed_date",
            "date_label_type",
            name="uq_inventory_snapshot_lot",
            postgresql_nulls_not_distinct=True,
        ),
        sa.CheckConstraint(
            "event_count > 0", name="event_count_positive"
        ),
        sa.CheckConstraint(
            "date_label_type IS NULL OR printed_date IS NOT NULL",
            name="label_type_needs_printed_date",
        ),
    )
    op.create_index(
        "ix_inventory_snapshot_household_id_as_of",
        "inventory_snapshot",
        ["household_id", "as_of"],
    )
    op.create_index(
        "ix_inventory_snapshot_household_id_printed_date",
        "inventory_snapshot",
        ["household_id", "printed_date"],
        postgresql_where=sa.text("quantity > 0"),
    )


def downgrade() -> None:
    op.drop_index("ix_inventory_snapshot_household_id_printed_date", "inventory_snapshot")
    op.drop_index("ix_inventory_snapshot_household_id_as_of", "inventory_snapshot")
    op.drop_table("inventory_snapshot")

    op.execute("DROP TRIGGER IF EXISTS inventory_events_no_truncate ON inventory_events")
    op.execute("DROP TRIGGER IF EXISTS inventory_events_no_delete ON inventory_events")
    op.execute("DROP TRIGGER IF EXISTS inventory_events_no_update ON inventory_events")
    op.execute("DROP FUNCTION IF EXISTS kitchensense_reject_mutation()")

    op.drop_index("ix_inventory_events_household_product_occurred_at", "inventory_events")
    op.drop_index(
        "ix_inventory_events_household_id_occurred_at_recorded_at", "inventory_events"
    )
    op.drop_index(
        "ix_inventory_events_household_id_recorded_at_occurred_at", "inventory_events"
    )
    op.drop_index("uq_inventory_events_idempotency_key", "inventory_events")
    op.drop_table("inventory_events")

    op.drop_index("ix_canonical_products_category", "canonical_products")
    op.drop_index("uq_canonical_products_gtin", "canonical_products")
    op.drop_table("canonical_products")

    op.drop_table("households")

    bind = op.get_bind()
    STORAGE_LOCATION.drop(bind, checkfirst=True)
    DATE_LABEL_TYPE.drop(bind, checkfirst=True)
    EVENT_TYPE.drop(bind, checkfirst=True)
