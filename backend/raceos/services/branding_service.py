"""A coach's logo and accent, and the limits on both.

Coach tier advertised a white-label export and none of it existed. What exists
now is deliberately small: a display name, one accent colour, a logo and a
footer line.

**It is not a theme, and that is the design rather than a first version.** The
printed race card is read in a transition tent, at hour nine, often through a
wet plastic sleeve and often after somebody photocopied it. Every safeguard in
it — the glyph beside every gate state, the monochrome-legible contrast, the
provenance footer — exists because of that. A coach who could restyle the
document could remove those without knowing they were load-bearing, and the
person holding the result cannot fix it.

So the accent replaces one colour, and it is checked for contrast before it is
accepted. Everything else is fixed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.errors import InvalidInput, UploadFailed
from raceos.config import Settings
from raceos.db.models import CoachBranding, User
from raceos.exports import tokens
from raceos.logging import get_logger
from raceos.storage.base import ObjectNotFoundError, get_storage_backend

logger = get_logger(__name__)

HEX_COLOUR = re.compile(r"^#[0-9a-fA-F]{6}$")

#: WCAG 2.1 AA for large text and graphical objects. The accent is used for
#: headings, rules and the state colours' neighbours — never for body copy —
#: so 3:1 is the applicable threshold rather than 4.5:1.
MIN_CONTRAST_RATIO = 3.0

#: What a logo may be. No SVG: it is a document format with script and external
#: references in it, and this file is rendered into a PDF on our own server.
ALLOWED_LOGO_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}

#: A logo is a mark in a corner. A megabyte of it is somebody's hero image.
MAX_LOGO_BYTES = 512 * 1024

#: Enough for a practice name. Beyond this it is not a name, it is copy.
MAX_DISPLAY_NAME = 60
MAX_FOOTER_NOTE = 120


def _channel(value: float) -> float:
    """One sRGB channel, linearised. WCAG's own formula."""
    scaled = value / 255.0
    return scaled / 12.92 if scaled <= 0.03928 else ((scaled + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_colour: str) -> float:
    raw = hex_colour.lstrip("#")
    red, green, blue = (int(raw[index : index + 2], 16) for index in (0, 2, 4))
    return 0.2126 * _channel(red) + 0.7152 * _channel(green) + 0.0722 * _channel(blue)


def contrast_ratio(first: str, second: str) -> float:
    """WCAG 2.1 contrast between two hex colours."""
    lighter, darker = sorted((relative_luminance(first), relative_luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def validate_accent(accent_hex: str) -> str:
    """Normalise and refuse anything that would not survive printing.

    Checked against the paper colour rather than against white: the artefact
    is printed on the surface token, and a pale accent that reads on a screen
    disappears on it.
    """
    cleaned = accent_hex.strip()
    if not HEX_COLOUR.match(cleaned):
        raise InvalidInput(
            "An accent colour looks like #E4622F — a hash and six hex digits.",
            field="accent_hex",
        )
    ratio = contrast_ratio(cleaned, tokens.SURFACE)
    if ratio < MIN_CONTRAST_RATIO:
        raise InvalidInput(
            f"That colour is too pale to read on the printed page — it reaches "
            f"{ratio:.1f}:1 against the paper and needs {MIN_CONTRAST_RATIO:.0f}:1. "
            f"A darker shade of the same hue will work.",
            field="accent_hex",
            details={"contrast_ratio": round(ratio, 2), "required": MIN_CONTRAST_RATIO},
        )
    return cleaned.upper()


def for_coach(session: Session, *, coach: User) -> CoachBranding | None:
    return session.scalar(select(CoachBranding).where(CoachBranding.coach_id == coach.id))


def _row(session: Session, coach: User) -> CoachBranding:
    existing = for_coach(session, coach=coach)
    if existing is not None:
        return existing
    created = CoachBranding(coach_id=coach.id)
    session.add(created)
    session.flush()
    return created


def update(
    session: Session,
    *,
    coach: User,
    display_name: str | None = None,
    accent_hex: str | None = None,
    footer_note: str | None = None,
    clear_accent: bool = False,
) -> CoachBranding:
    """Set whatever was sent. Absent means unchanged, not cleared."""
    row = _row(session, coach)

    if display_name is not None:
        cleaned = display_name.strip()
        if len(cleaned) > MAX_DISPLAY_NAME:
            raise InvalidInput(
                f"Keep the name under {MAX_DISPLAY_NAME} characters.", field="display_name"
            )
        row.display_name = cleaned or None

    if clear_accent:
        row.accent_hex = None
    elif accent_hex is not None:
        row.accent_hex = validate_accent(accent_hex)

    if footer_note is not None:
        cleaned = footer_note.strip()
        if len(cleaned) > MAX_FOOTER_NOTE:
            raise InvalidInput(
                f"Keep the footer under {MAX_FOOTER_NOTE} characters.", field="footer_note"
            )
        row.footer_note = cleaned or None

    session.flush()
    return row


def logo_key(coach: User, extension: str) -> str:
    return f"coach-branding/{coach.id}/logo{extension}"


def set_logo(
    session: Session,
    *,
    coach: User,
    data: bytes,
    content_type: str,
    settings: Settings,
) -> CoachBranding:
    """Store the logo and point the row at it.

    The declared content type is not trusted on its own — the bytes are
    checked for their own magic number. A file claiming to be a PNG and
    containing something else would be rendered by WeasyPrint on our server,
    which is not a place to find out.
    """
    declared = (content_type or "").split(";")[0].strip().lower()
    if declared not in ALLOWED_LOGO_TYPES:
        raise UploadFailed(
            "A logo must be a PNG, JPEG or WebP. SVG is not accepted: it can "
            "carry script and external references, and this file is rendered "
            "on our own server."
        )
    if not data:
        raise UploadFailed("That file is empty.")
    if len(data) > MAX_LOGO_BYTES:
        raise UploadFailed(
            f"A logo is a mark in a corner — keep it under " f"{MAX_LOGO_BYTES // 1024} KB."
        )
    if _sniff(data) != declared:
        raise UploadFailed("That file is not the format it says it is. Re-export it and try again.")

    row = _row(session, coach)
    extension = ALLOWED_LOGO_TYPES[declared]
    key = logo_key(coach, extension)
    storage = get_storage_backend(settings)
    storage.put(key, data, content_type=declared, public=False)

    row.logo_storage_key = key
    row.logo_content_type = declared
    session.flush()
    logger.info("branding.logo_set", extra={"coach_id": str(coach.id), "bytes": len(data)})
    return row


def _sniff(data: bytes) -> str | None:
    """The format the bytes actually are, from their magic number."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def clear_logo(session: Session, *, coach: User, settings: Settings) -> CoachBranding:
    row = _row(session, coach)
    if row.logo_storage_key:
        get_storage_backend(settings).delete(row.logo_storage_key)
    row.logo_storage_key = None
    row.logo_content_type = None
    session.flush()
    return row


@dataclass(frozen=True)
class RenderBranding:
    """What the renderer is given. Resolved, with the logo already read.

    A flat value rather than the ORM row, for the same reason
    ``PlanRenderData`` is: a PDF generated in a background task must not depend
    on a session that has closed, and it must not lazily fetch an image
    mid-render.
    """

    display_name: str | None
    accent_hex: str
    footer_note: str | None
    #: A `data:` URI, so the renderer needs no filesystem and no network.
    logo_data_uri: str | None


def resolve(session: Session, *, coach: User | None, settings: Settings) -> RenderBranding | None:
    """The branding to render with, or ``None`` for the house artefact.

    ``None`` for a plan with no coach behind it, and for a coach who has set
    nothing — an empty branding row should produce the ordinary document, not
    a subtly different one.
    """
    if coach is None:
        return None
    row = for_coach(session, coach=coach)
    if row is None:
        return None
    if not (row.display_name or row.accent_hex or row.logo_storage_key or row.footer_note):
        return None

    return RenderBranding(
        display_name=row.display_name,
        # Falls back to the house accent, so a coach who uploaded a logo and
        # chose no colour gets the document they expect rather than a black one.
        accent_hex=row.accent_hex or tokens.ACCENT,
        footer_note=row.footer_note,
        logo_data_uri=_logo_data_uri(row, settings),
    )


def _logo_data_uri(row: CoachBranding, settings: Settings) -> str | None:
    """Read the logo into a `data:` URI, or give up quietly.

    A missing logo must not fail an export. The athlete is trying to print
    their race card; a branding asset that has gone astray is the coach's
    problem to fix and not a reason to withhold the plan.
    """
    if not row.logo_storage_key or not row.logo_content_type:
        return None
    import base64

    try:
        raw = get_storage_backend(settings).get(row.logo_storage_key)
    except (ObjectNotFoundError, ValueError):
        logger.warning("branding.logo_missing", extra={"coach_id": str(row.coach_id)})
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{row.logo_content_type};base64,{encoded}"
