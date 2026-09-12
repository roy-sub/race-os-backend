"""plans: when this plan was first taken out of the building

Revision ID: f3c6d7e8a9b0
Revises: e2b5c6d7f8a9
Created: 2026-09-12 18:30:00.000000

"Exported" was specified as a plan status and could not be implemented,
because nothing recorded that an export had ever happened. A status derived
from no fact is a label, and this codebase does not ship those.

One nullable timestamp, set the first time any export of the plan is served
and never updated again. First rather than last on purpose: the question the
status answers is "does the athlete have this in their hands", and the answer
does not become more true the fourth time they download the PDF. A `last_`
column would also turn every download into a write on the plan row, which is
a lot of contention for a fact nobody reads.

Null means no export has been served. Existing rows are null, which is
accurate: this build has not been recording it, so it genuinely does not know.
Back-filling from invoices or solve dates would be inventing a history.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3c6d7e8a9b0"
down_revision: str | None = "e2b5c6d7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "plans", sa.Column("first_exported_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("plans", "first_exported_at")
