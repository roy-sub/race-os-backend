"""notification_type: the kinds of event that had no way to be told

Revision ID: e5a1b2c3d4f6
Revises: d4f0a5e6c7b8
Created: 2026-09-12 18:00:00.000000

Seven new values on the `notification_type` enum. Each corresponds to an
event the code already produces and could not previously report in its own
right:

* `plan_ready` — a coach-built plan is waiting for the athlete's approval.
* `coach_shared` — a coach shared a plan with an athlete.
* `athlete_accepted` — an athlete accepted a coach's invitation.
* `payment_succeeded`, `payment_failed` — the provider webhook.
* `subscription_renewing` — a renewal is due.
* `support_access` — a support agent asked to read this account. This one was
  being sent as `digest`, so an athlete who switched the weekly digest off
  would never have been told. A privacy notice a convenience preference can
  mute is not a notice.

Three of these describe subscriptions and payments, which only became things
an athlete could do in this branch.

`ALTER TYPE ... ADD VALUE` cannot run inside a transaction block on
PostgreSQL, so the connection is committed first. That is why this migration
does not look like the others.

**Reversible, with a caveat stated rather than hidden.** PostgreSQL has no
`ALTER TYPE ... DROP VALUE`, so `downgrade()` leaves the labels in place. They
are inert to the older schema: nothing older writes them, and a column that
accepts a superset of what is written is not a defect.

What `downgrade()` does do is refuse if any row is *using* one of them. The
only other way to remove a label is to rebuild the type and rewrite both
columns that reference it, which means deleting those rows — and silently
deleting somebody's notifications to satisfy a schema rollback is not a
trade this migration is willing to make on its own.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e5a1b2c3d4f6"
down_revision: str | None = "d4f0a5e6c7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_VALUES = (
    "plan_ready",
    "coach_shared",
    "athlete_accepted",
    "payment_succeeded",
    "payment_failed",
    "subscription_renewing",
    "support_access",
)


def upgrade() -> None:
    # `ALTER TYPE ... ADD VALUE` is not transactional on PostgreSQL.
    op.execute("COMMIT")
    for value in NEW_VALUES:
        # IF NOT EXISTS so a partially-applied run can be repeated, which is
        # the state an interrupted non-transactional migration leaves behind.
        op.execute(f"ALTER TYPE notification_type ADD VALUE IF NOT EXISTS '{value}'")


#: Every column on the `notification_type` enum. Both are checked before a
#: downgrade is allowed to claim it left nothing behind.
ENUM_COLUMNS = (
    ("notifications", "type_key"),
    ("notification_preferences", "type_key"),
)


def downgrade() -> None:
    """Leaves the labels; refuses if anything is using one.

    See the module docstring. The labels cannot be dropped, and removing them
    the only other way would mean deleting rows.
    """
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
