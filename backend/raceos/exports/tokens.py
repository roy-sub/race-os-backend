"""Design tokens, exported from the frontend's own values.

Build Spec C6 and Part 12.3: a generated PDF must match the shipped
frontend's tokens, read from a **single shared token file**, so screen and
print cannot drift.

The values here are the ones the frontend actually ships — accent `#E4622F`,
surface `#FBF8F2`. When the frontend's token file becomes machine-readable
this module should import it rather than restate it; until then it is one
place rather than scattered literals, and a test asserts the accent and
surface match what `lib/` uses.
"""

from __future__ import annotations

from typing import Final

ACCENT: Final[str] = "#E4622F"
SURFACE: Final[str] = "#FBF8F2"
INK: Final[str] = "#1C1917"
MUTED: Final[str] = "#6B6459"
RULE: Final[str] = "#D9D2C5"

#: Leg colours, from `lib/sharedPlan.ts`.
LEG_COLOURS: Final[dict[str, str]] = {
    "SWIM": "#4F7C93",
    "BIKE": "#E4622F",
    "RUN": "#64707A",
}

#: Margin-state colours. **Never the only carrier of meaning**: every coloured
#: element in a generated PDF also carries a label or a glyph, because the race
#: card has to be monochrome-legible through a wet plastic sleeve at hour nine.
STATE_COLOURS: Final[dict[str, str]] = {
    "clear": "#2F6B4F",
    "tight": "#9A6B1E",
    "bad": "#9B2C2C",
}

STATE_GLYPHS: Final[dict[str, str]] = {
    "clear": "OK",
    "tight": "!",
    "bad": "X",
}

#: Minimum computed font size anywhere in a print artefact. Asserted by the
#: PDF snapshot test, because "readable at hour nine" is a real constraint on
#: the export service rather than a design note.
MIN_PRINT_PT: Final[float] = 8.0
