"""coach_branding: a logo and an accent, and nothing more

Revision ID: c9e3f4a5b6d7
Revises: b8d2e3f4a5c6
Created: 2026-09-12 16:03:00.000000

Coach tier advertised a white-label export and none of it existed: no logo
upload, no accent, no branded variant of the PDF.

One row per coach. **Not a theme.** The accent replaces one colour and the
logo sits in one corner; the layout, the typography and every safeguard in the
printed artefact stay as they are. Letting a coach restyle the document would
let them produce something that looks like a race card and is not one — and
the reason every gate carries a glyph as well as a colour is that the card has
to survive a monochrome print through a wet sleeve at hour nine, which a
chosen palette could quietly break.

Only the logo's storage key is here. Images are megabytes and a database is
not a file system.

Reversible: ``downgrade()`` drops the table.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9e3f4a5b6d7"
down_revision: str | None = "b8d2e3f4a5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "coach_branding",
        sa.Column("coach_id", sa.UUID(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("accent_hex", sa.String(length=7), nullable=True),
        sa.Column("logo_storage_key", sa.Text(), nullable=True),
        sa.Column("logo_content_type", sa.Text(), nullable=True),
        sa.Column("footer_note", sa.Text(), nullable=True),
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
            ["coach_id"],
            ["users.id"],
            name=op.f("fk_coach_branding_coach_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_coach_branding")),
        sa.UniqueConstraint("coach_id", name=op.f("uq_coach_branding_coach_id")),
    )
    op.create_index("ix_coach_branding_coach_id", "coach_branding", ["coach_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_coach_branding_coach_id", table_name="coach_branding")
    op.drop_table("coach_branding")
