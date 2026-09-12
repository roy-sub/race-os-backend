"""notification_type: telling a submitter what the reviewer decided

Revision ID: e2b5c6d7f8a9
Revises: d1a4b5c6e7f8
Created: 2026-09-12 17:15:00.000000

`course_reviewed` — a reviewer published or declined a course this athlete
submitted. Without it the curation queue would be a decision the only person
affected by it never hears.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block on
PostgreSQL, so the connection is committed first. Same shape, and same
one-way caveat, as `e5a1b2c3d4f6`: PostgreSQL has no `DROP VALUE`, so
`downgrade()` leaves the label and refuses only if rows are using it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e2b5c6d7f8a9"
down_revision: str | None = "d1a4b5c6e7f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_VALUES = ("course_reviewed",)

ENUM_COLUMNS = (
    ("notifications", "type_key"),
    ("notification_preferences", "type_key"),
)


def upgrade() -> None:
    op.execute("COMMIT")
    for value in NEW_VALUES:
        op.execute(f"ALTER TYPE notification_type ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    """Leaves the label; refuses if anything is using one."""
    values = ", ".join(f"'{value}'" for value in NEW_VALUES)
    in_use: list[str] = []
    connection = op.get_bind()
    for table, column in ENUM_COLUMNS:
        count = connection.exec_driver_sql(
            f"SELECT count(*) FROM {table} WHERE {column}::text IN ({values})"
        ).scalar()
        if count:
            in_use.append(f"{table}.{column}: {count} row(s)")

    if in_use:
        raise RuntimeError(
            "Cannot downgrade past this migration while rows are using the new "
            f"notification types ({'; '.join(in_use)}). Removing the enum "
            "labels would mean deleting those rows. Delete or re-key them "
            "deliberately, then downgrade."
        )
