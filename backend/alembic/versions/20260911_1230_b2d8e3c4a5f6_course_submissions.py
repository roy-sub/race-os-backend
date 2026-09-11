"""course_submissions: an athlete adding a race we have not built yet

Revision ID: b2d8e3c4a5f6
Revises: a1c7f2b9d3e4
Created: 2026-09-11 12:30:00.000000

One table and one enum type. The uploaded route files themselves live in
object storage and only their keys are stored here — a 90 km GPX is megabytes,
and a database is not a file system.

Reversible: ``downgrade()`` drops the table and the enum type.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2d8e3c4a5f6"
down_revision: str | None = "a1c7f2b9d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Created as a side effect of the column that uses it, which is how every
# other enum in this schema is made — see the initial migration. Pre-creating
# it *and* declaring it on the column emits CREATE TYPE twice, and the second
# one fails.
SUBMISSION_STATUS = sa.Enum(
    "draft", "queued", "processing", "ready", "failed", name="submission_status"
)

# `distance_type` is not ours: the initial migration made it. Referencing it
# with `create_type=False` uses the existing type instead of trying to declare
# a second one with the same name.
DISTANCE_TYPE = postgresql.ENUM(
    "Sprint", "Olympic", "70.3", "Full", name="distance_type", create_type=False
)


def upgrade() -> None:
    op.create_table(
        "course_submissions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
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
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status", SUBMISSION_STATUS, server_default=sa.text("'draft'"), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("place", sa.Text(), nullable=False),
        sa.Column("country", sa.String(length=2), nullable=True),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("distance_type", DISTANCE_TYPE, nullable=False),
        sa.Column("lat", sa.Numeric(), nullable=False),
        sa.Column("lng", sa.Numeric(), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("start_time_local", sa.Time(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("swim_file_key", sa.Text(), nullable=True),
        sa.Column("bike_file_key", sa.Text(), nullable=True),
        sa.Column("run_file_key", sa.Text(), nullable=True),
        sa.Column(
            "file_names",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "problems",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("course_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.CheckConstraint("lat BETWEEN -90 AND 90", name="course_submissions_lat_range"),
        sa.CheckConstraint("lng BETWEEN -180 AND 180", name="course_submissions_lng_range"),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_course_submissions_user_id", "course_submissions", ["user_id"])
    op.create_index("ix_course_submissions_status", "course_submissions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_course_submissions_status", table_name="course_submissions")
    op.drop_index("ix_course_submissions_user_id", table_name="course_submissions")
    op.drop_table("course_submissions")
    # Explicit, because dropping the table does not drop the type it used and
    # an orphaned type makes the next upgrade fail.
    SUBMISSION_STATUS.drop(op.get_bind(), checkfirst=True)
