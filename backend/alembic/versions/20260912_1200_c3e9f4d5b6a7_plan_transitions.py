"""plans: the two transition splits, stored rather than inferred

Revision ID: c3e9f4d5b6a7
Revises: b2d8e3c4a5f6
Created: 2026-09-12 12:00:00.000000

The solver has always computed T1 and T2 — ``RaceProfile`` accumulates swim,
t1, bike, t2, run in that fixed order — but only the three leg splits were
persisted. Total transition time was recoverable by subtracting them from
``projected_minutes``; the *per-transition* split was not, and T1 is the one
that carries the wetsuit strip.

Both nullable. A draft has not been solved, and a plan solved before this
column existed has nothing to backfill from: a guessed split would be
indistinguishable from a solved one, and the race card would print it with the
same authority. Absent says "re-solve to see this"; a fabricated 6:00 does not.

Reversible: ``downgrade()`` drops both columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3e9f4d5b6a7"
down_revision: str | None = "b2d8e3c4a5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("t1_minutes", sa.Numeric(), nullable=True))
    op.add_column("plans", sa.Column("t2_minutes", sa.Numeric(), nullable=True))
    # A transition takes time. Zero would mean the athlete teleported, and a
    # negative one is a sign error the plan would otherwise render as a
    # plausible-looking clock time.
    op.create_check_constraint(
        "plans_t1_minutes_positive", "plans", "t1_minutes IS NULL OR t1_minutes > 0"
    )
    op.create_check_constraint(
        "plans_t2_minutes_positive", "plans", "t2_minutes IS NULL OR t2_minutes > 0"
    )


def downgrade() -> None:
    op.drop_constraint("plans_t2_minutes_positive", "plans", type_="check")
    op.drop_constraint("plans_t1_minutes_positive", "plans", type_="check")
    op.drop_column("plans", "t2_minutes")
    op.drop_column("plans", "t1_minutes")
