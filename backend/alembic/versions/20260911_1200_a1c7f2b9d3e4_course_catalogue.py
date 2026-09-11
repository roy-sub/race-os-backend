"""course catalogue: availability, visibility, announced date, submitter

Revision ID: a1c7f2b9d3e4
Revises: 93f5c4c7f83a
Created: 2026-09-11 12:00:00.000000

The directory stops being "whatever bundles exist" and becomes a declared
catalogue. Four columns carry that, plus the two enum types they need.

Existing rows are backfilled as ``available`` / ``catalogue``, which is what
they were before this migration: everything already listed was listed, and
everything with a bundle was enterable. ``db.seed`` then applies the real
catalogue on top, retiring whatever it no longer names — so no course is
deleted here and no plan is orphaned.

Reversible: ``downgrade()`` drops the columns and both enum types.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7f2b9d3e4"
down_revision: str | None = "93f5c4c7f83a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


COURSE_AVAILABILITY = sa.Enum("available", "coming_soon", name="course_availability")
COURSE_VISIBILITY = sa.Enum("catalogue", "showcase", "retired", name="course_visibility")


def upgrade() -> None:
    bind = op.get_bind()
    COURSE_AVAILABILITY.create(bind, checkfirst=True)
    COURSE_VISIBILITY.create(bind, checkfirst=True)

    # Added with a server default so the column is non-null from the first
    # row, then backfilled: an existing directory row was, by definition,
    # already listed and already enterable.
    op.add_column(
        "courses",
        sa.Column(
            "availability",
            COURSE_AVAILABILITY,
            server_default=sa.text("'coming_soon'"),
            nullable=False,
        ),
    )
    op.add_column(
        "courses",
        sa.Column(
            "visibility",
            COURSE_VISIBILITY,
            server_default=sa.text("'catalogue'"),
            nullable=False,
        ),
    )
    op.add_column("courses", sa.Column("next_edition_date", sa.Date(), nullable=True))
    op.add_column("courses", sa.Column("official_event_name", sa.Text(), nullable=True))
    op.add_column(
        "courses",
        sa.Column("submitted_by_user_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_courses_submitted_by_user_id_users",
        "courses",
        "users",
        ["submitted_by_user_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_courses_visibility", "courses", ["visibility"])
    op.create_index("ix_courses_submitted_by_user_id", "courses", ["submitted_by_user_id"])

    op.execute("UPDATE courses SET availability = 'available'")


def downgrade() -> None:
    op.drop_index("ix_courses_submitted_by_user_id", table_name="courses")
    op.drop_index("ix_courses_visibility", table_name="courses")
    op.drop_constraint("fk_courses_submitted_by_user_id_users", "courses", type_="foreignkey")
    op.drop_column("courses", "submitted_by_user_id")
    op.drop_column("courses", "official_event_name")
    op.drop_column("courses", "next_edition_date")
    op.drop_column("courses", "visibility")
    op.drop_column("courses", "availability")

    bind = op.get_bind()
    COURSE_VISIBILITY.drop(bind, checkfirst=True)
    COURSE_AVAILABILITY.drop(bind, checkfirst=True)
