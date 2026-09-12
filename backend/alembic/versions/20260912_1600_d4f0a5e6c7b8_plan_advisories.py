"""plans: where a solve sits outside its own evidence

Revision ID: d4f0a5e6c7b8
Revises: c3e9f4d5b6a7
Created: 2026-09-12 16:00:00.000000

The solver has always known when it was operating outside the data behind a
curve — `RunHeat.clamped` has been computed since the environment model was
written — and nothing was ever done with it. `SOLVER_MODEL.md` says such a
plan "should be treated as advisory"; no athlete was ever told.

`advisories` stores the `model:` keys for it, beside `assumed_fields`. The two
are deliberately separate: `assumed_fields` records inputs the athlete did not
supply, and this records the model's own range — every input present, and the
answer still resting on ground the data does not cover.

Defaults to empty, so every plan solved before this column existed reads as
carrying no advisory. That is the right default rather than a lie: those plans
were solved by a version that did not compute one, and backfilling would mean
asserting something about solves nobody examined.

Reversible: ``downgrade()`` drops the column.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d4f0a5e6c7b8"
down_revision: str | None = "c3e9f4d5b6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plans",
        sa.Column(
            "advisories",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("plans", "advisories")
