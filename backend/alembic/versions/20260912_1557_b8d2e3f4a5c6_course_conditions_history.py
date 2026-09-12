"""course_conditions_history: what race day was actually like

Revision ID: b8d2e3f4a5c6
Revises: a7c1d2e3f4b5
Created: 2026-09-12 15:57:00.000000

Course Recon promised historical race-day conditions and nothing was stored,
so the prototype drew them: a median air temperature, a wetsuit likelihood and
a finish-time distribution, none of which was backed by anything.

One row per past edition, every value reanalysis from the weather archive for
this course's coordinates on that date. Observed, not modelled.

`water_temp_c` is nullable and will often be null. Sea-surface temperature is
available for a coastal swim and not for a lake, and a lake course guessing at
its own water temperature would be the invented number again. Where it is
absent the wetsuit likelihood is absent too, rather than estimated from air
temperature.

There is no finish-time column. That needs actual results, which this system
does not have and cannot obtain.

Reversible: ``downgrade()`` drops the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8d2e3f4a5c6"
down_revision: str | None = "a7c1d2e3f4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "course_conditions_history",
        sa.Column("course_id", sa.UUID(), nullable=False),
        sa.Column("observed_on", sa.Date(), nullable=False),
        sa.Column("observed_hour", sa.Integer(), nullable=False),
        sa.Column("air_temp_c", sa.Numeric(), nullable=False),
        sa.Column("humidity_pct", sa.Numeric(), nullable=False),
        sa.Column("wind_speed_ms", sa.Numeric(), nullable=False),
        sa.Column("wind_dir_deg", sa.Numeric(), nullable=True),
        sa.Column("precipitation_mm", sa.Numeric(), nullable=True),
        sa.Column("cloud_cover_pct", sa.Numeric(), nullable=True),
        sa.Column("water_temp_c", sa.Numeric(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint(
            "observed_hour BETWEEN 0 AND 23",
            name=op.f("ck_course_conditions_history_course_conditions_history_hour_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["course_id"],
            ["courses.id"],
            name=op.f("fk_course_conditions_history_course_id_courses"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_course_conditions_history")),
        sa.UniqueConstraint(
            "course_id", "observed_on", name="uq_course_conditions_history_course_date"
        ),
    )
    op.create_index(
        "ix_course_conditions_history_course_id",
        "course_conditions_history",
        ["course_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_course_conditions_history_course_id", table_name="course_conditions_history")
    op.drop_table("course_conditions_history")
