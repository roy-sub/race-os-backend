"""race_week_tasks: the checklist the ICS export could only describe

Revision ID: a7c1d2e3f4b5
Revises: f6b2c3d4e5a7
Created: 2026-09-12 15:52:46.878300

The race-week strip needed dated, checkable tasks. The ICS export already
derived the right dates from the event date, but a calendar event cannot be
ticked off and nothing was stored, so "have I handed in the special-needs
bag?" had no answer.

Generated and personal tasks share one table. Two would mean two queries, two
orderings and two ways to tick something off, for a distinction the athlete
does not have — to them it is one list. `generated` is what tells them apart
when a race is re-dated and only the derived rows are rebuilt.

`(race_id, key)` is unique so regeneration is an upsert rather than a
duplicate, and so a task already ticked off stays ticked when the checklist is
rebuilt.

Reversible: ``downgrade()`` drops the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7c1d2e3f4b5"
down_revision: str | None = "f6b2c3d4e5a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "race_week_tasks",
        sa.Column("race_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("generated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["race_id"],
            ["races.id"],
            name=op.f("fk_race_week_tasks_race_id_races"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_race_week_tasks_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_race_week_tasks")),
        sa.UniqueConstraint("race_id", "key", name="uq_race_week_tasks_race_id_key"),
    )
    op.create_index(
        "ix_race_week_tasks_race_id_due_date",
        "race_week_tasks",
        ["race_id", "due_date"],
        unique=False,
    )
    op.create_index("ix_race_week_tasks_user_id", "race_week_tasks", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_race_week_tasks_user_id", table_name="race_week_tasks")
    op.drop_index("ix_race_week_tasks_race_id_due_date", table_name="race_week_tasks")
    op.drop_table("race_week_tasks")
