"""constraint_source: a value from an external tool is not a manual one

Revision ID: f6b2c3d4e5a7
Revises: e5a1b2c3d4f6
Created: 2026-09-12 19:30:00.000000

An athlete bringing a figure in from a bike-split modeller, a coach's
spreadsheet or a lab report had nowhere honest to put it. The nearest
available stamp was `manual`, which means "a person typed what they believe" —
a different claim, ageing differently and defended differently, and the wrong
thing to tell somebody in the "Why this?" drawer about their own number.

`imported` says where it came from; `source_detail` says which tool.

This changes no solver behaviour. Provenance is never read by a branch in the
solver: an `imported` value carries exactly the numeric weight of a `measured`
one, and a test asserts that by permuting every source in a golden input and
requiring byte-identical output.

Reversible in the same limited sense as the notification-type migration:
PostgreSQL cannot drop an enum label, so `downgrade()` leaves it and refuses
if any row is using it rather than deleting somebody's constraint history.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f6b2c3d4e5a7"
down_revision: str | None = "e5a1b2c3d4f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every column on the `constraint_source` enum.
ENUM_COLUMNS = (
    ("constraints", "source"),
    ("constraint_history", "source"),
    ("plan_constraint_refs", "source_label"),
)


def upgrade() -> None:
    # `ALTER TYPE ... ADD VALUE` is not transactional on PostgreSQL.
    op.execute("COMMIT")
    op.execute("ALTER TYPE constraint_source ADD VALUE IF NOT EXISTS 'imported'")


def downgrade() -> None:
    connection = op.get_bind()
    in_use: list[str] = []
    for table, column in ENUM_COLUMNS:
        exists = connection.exec_driver_sql(
            f"SELECT to_regclass('public.{table}') IS NOT NULL"
        ).scalar()
        if not exists:
            continue
        count = connection.exec_driver_sql(
            f"SELECT count(*) FROM {table} WHERE {column}::text = 'imported'"  # noqa: S608
        ).scalar()
        if count:
            in_use.append(f"{table}.{column}: {count} row(s)")

    if in_use:
        raise RuntimeError(
            "Cannot downgrade past this migration while rows are using the "
            f"'imported' provenance ({'; '.join(in_use)}). Removing the enum "
            "label would mean deleting those rows. Re-key them deliberately, "
            "then downgrade."
        )
