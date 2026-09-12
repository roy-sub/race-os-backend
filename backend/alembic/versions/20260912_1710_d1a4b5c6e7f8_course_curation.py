"""course curation: the difference between "mine" and "the catalogue"

Revision ID: d1a4b5c6e7f8
Revises: c9e3f4a5b6d7
Created: 2026-09-12 17:10:00.000000

An athlete-submitted course has always been private to the athlete who
submitted it: `courses.submitted_by_user_id` being non-null is what hides it
from the directory everyone else reads. That was the safe default and it is
still the default, but it left no way to say yes. A course somebody surveyed
properly and offered to the catalogue had nowhere to go.

These four columns are the record of a review having happened:

* `curation_status` — unreviewed, published, or rejected.
* `curation_note` — why, in the submitter's terms.
* `curated_at`, `curated_by_user_id` — when, and by whom.

**Existing rows do not change hands.** The column defaults to `unreviewed`,
which is exactly what every submitted course already is in behaviour: visible
to its submitter and to nobody else. No course becomes more visible because
this migration ran. A house course carries `unreviewed` too, and it means the
same thing there — nobody reviewed it — but it is ignored, because the
visibility rule only consults it when `submitted_by_user_id` is set.

`curated_by_user_id` is `ON DELETE SET NULL` rather than `CASCADE`: a reviewer
leaving must not delete the courses they approved. The decision outlives the
person who made it, and the note is still there to say what the decision was.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d1a4b5c6e7f8"
down_revision: str | None = "c9e3f4a5b6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURATION_STATUS = postgresql.ENUM(
    "unreviewed",
    "published",
    "rejected",
    name="curation_status",
    create_type=False,
)


def upgrade() -> None:
    CURATION_STATUS.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "courses",
        sa.Column(
            "curation_status",
            CURATION_STATUS,
            nullable=False,
            server_default=sa.text("'unreviewed'"),
        ),
    )
    op.add_column("courses", sa.Column("curation_note", sa.Text(), nullable=True))
    op.add_column(
        "courses", sa.Column("curated_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "courses",
        sa.Column("curated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_courses_curated_by_user_id_users",
        "courses",
        "users",
        ["curated_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_courses_curation_status", "courses", ["curation_status"])


def downgrade() -> None:
    """Reversible without losing a course.

    Dropping these columns loses the *decisions*, not the courses. A published
    course reverts to being private to its submitter, which is where it
    started and is the safe direction to fail in: a rollback that left
    somebody's unreviewed GPX trace listed in the catalogue would be the
    dangerous one.
    """
    op.drop_index("ix_courses_curation_status", table_name="courses")
    op.drop_constraint("fk_courses_curated_by_user_id_users", "courses", type_="foreignkey")
    op.drop_column("courses", "curated_by_user_id")
    op.drop_column("courses", "curated_at")
    op.drop_column("courses", "curation_note")
    op.drop_column("courses", "curation_status")
    CURATION_STATUS.drop(op.get_bind(), checkfirst=True)
