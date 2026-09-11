"""The race catalogue: the declared set of events the product ships.

**The catalogue is a manifest, not "whatever bundles happen to exist."** That
distinction is the point of this module. The directory used to be the contents
of ``pipelines/course-ingest/out/bundles/``, which meant a fixture dropped in
for a test would appear on the live site, and an event the season announces
could not be listed until its bundle was finished. Both are wrong. What an
athlete sees is declared here, once, and the bundles fill it in.

Three kinds of row:

* **Available.** A generated bundle exists and is loaded, so the race can be
  entered and planned. One event today: IRONMAN 70.3 Italy Emilia-Romagna.
* **Coming soon.** Announced and dated, no course data yet. Listed honestly,
  and not enterable — :func:`raceos.services.course_service` refuses, and the
  directory renders the row as unavailable rather than hiding it.
* **Showcase.** Kalmar 70.3, and only Kalmar. It is the signed-out marketing
  map: a deliberately stylised, out-of-scale illustration of what a course map
  looks like. It is never listed to a signed-in athlete, because sitting it
  beside surveyed courses would invite it to be read as one.

Adding the fourteenth event is a spec in ``pipelines/course-ingest/specs/``,
a generated bundle, and flipping one ``availability`` value here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from raceos.domain.enums import (
    CourseAvailability,
    CourseVisibility,
    Difficulty,
    DistanceType,
)


@dataclass(frozen=True)
class CatalogueEntry:
    """One row of the directory, whether or not it has course data yet."""

    slug: str
    name: str
    place: str
    country: str
    timezone: str
    distance_type: DistanceType
    difficulty: Difficulty
    lat: float
    lng: float
    event_date: date | None
    availability: CourseAvailability
    visibility: CourseVisibility = CourseVisibility.CATALOGUE
    tone_color: str = "#3E352B"
    #: The file stem under ``pipelines/course-ingest/out/bundles/``. ``None``
    #: for a coming-soon row, which by definition has no course data.
    bundle_slug: str | None = None

    @property
    def has_bundle(self) -> bool:
        return self.bundle_slug is not None


#: The 2026/27 European season, in date order, exactly as published.
#:
#: Dates are the organisers' announced dates. They are carried on the course
#: row as ``next_edition_date`` for the directory to show; nothing is solved
#: from them — an athlete's own ``races.event_date`` is still the only date a
#: plan is built against.
CATALOGUE: tuple[CatalogueEntry, ...] = (
    CatalogueEntry(
        slug="nice-703-world-championship",
        name="IRONMAN 70.3 World Championship",
        place="Nice, France",
        country="FR",
        timezone="Europe/Paris",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.BRUTAL,
        lat=43.6957,
        lng=7.2656,
        event_date=date(2026, 9, 12),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#1f4e79",
    ),
    CatalogueEntry(
        slug="ironman-wales",
        name="IRONMAN Wales",
        place="Tenby, United Kingdom",
        country="GB",
        timezone="Europe/London",
        distance_type=DistanceType.FULL,
        difficulty=Difficulty.BRUTAL,
        lat=51.6725,
        lng=-4.7050,
        event_date=date(2026, 9, 13),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#2f4f3e",
    ),
    CatalogueEntry(
        slug="belgrade-703",
        name="IRONMAN 70.3 Belgrade",
        place="Belgrade, Serbia",
        country="RS",
        timezone="Europe/Belgrade",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.APPROACHABLE,
        lat=44.8125,
        lng=20.4612,
        event_date=date(2026, 9, 13),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#5c5240",
    ),
    CatalogueEntry(
        slug="erkner-703",
        name="IRONMAN 70.3 Erkner",
        place="Erkner, Germany",
        country="DE",
        timezone="Europe/Berlin",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.APPROACHABLE,
        lat=52.4225,
        lng=13.7514,
        event_date=date(2026, 9, 13),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#3f5d4a",
    ),
    CatalogueEntry(
        slug="italy-emilia-romagna-full",
        name="IRONMAN Italy Emilia-Romagna",
        place="Cervia, Italy",
        country="IT",
        timezone="Europe/Rome",
        distance_type=DistanceType.FULL,
        difficulty=Difficulty.MODERATE,
        lat=44.2646,
        lng=12.3556,
        event_date=date(2026, 9, 19),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#1d6f7a",
    ),
    # The one event with real course data behind it.
    CatalogueEntry(
        slug="italy-emilia-romagna-703",
        name="IRONMAN 70.3 Italy Emilia-Romagna",
        place="Cervia, Italy",
        country="IT",
        timezone="Europe/Rome",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.APPROACHABLE,
        lat=44.2646,
        lng=12.3556,
        event_date=date(2026, 9, 20),
        availability=CourseAvailability.AVAILABLE,
        tone_color="#1d6f7a",
        bundle_slug="italy-emilia-romagna-703",
    ),
    CatalogueEntry(
        slug="weymouth-703",
        name="IRONMAN 70.3 Weymouth",
        place="Weymouth, United Kingdom",
        country="GB",
        timezone="Europe/London",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.MODERATE,
        lat=50.6105,
        lng=-2.4573,
        event_date=date(2026, 9, 20),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#33506b",
    ),
    CatalogueEntry(
        slug="calella-barcelona-full",
        name="IRONMAN Calella-Barcelona",
        place="Calella, Spain",
        country="ES",
        timezone="Europe/Madrid",
        distance_type=DistanceType.FULL,
        difficulty=Difficulty.MODERATE,
        lat=41.6146,
        lng=2.6544,
        event_date=date(2026, 10, 4),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#8a5a2b",
    ),
    CatalogueEntry(
        slug="versailles-703",
        name="IRONMAN 70.3 Versailles",
        place="Versailles, France",
        country="FR",
        timezone="Europe/Paris",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.MODERATE,
        lat=48.8049,
        lng=2.1204,
        event_date=date(2026, 10, 11),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#4a4364",
    ),
    CatalogueEntry(
        slug="portugal-cascais-703",
        name="IRONMAN 70.3 Portugal-Cascais",
        place="Cascais, Portugal",
        country="PT",
        timezone="Europe/Lisbon",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.MODERATE,
        lat=38.6979,
        lng=-9.4215,
        event_date=date(2026, 10, 17),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#2b6ea8",
    ),
    CatalogueEntry(
        slug="portugal-cascais-full",
        name="IRONMAN Portugal-Cascais",
        place="Cascais, Portugal",
        country="PT",
        timezone="Europe/Lisbon",
        distance_type=DistanceType.FULL,
        difficulty=Difficulty.HARD,
        lat=38.6979,
        lng=-9.4215,
        event_date=date(2026, 10, 17),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#2b6ea8",
    ),
    CatalogueEntry(
        slug="porec-703",
        name="IRONMAN 70.3 Poreč",
        place="Poreč, Croatia",
        country="HR",
        timezone="Europe/Zagreb",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.MODERATE,
        lat=45.2270,
        lng=13.5940,
        event_date=date(2026, 10, 18),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#1f6f6b",
    ),
    CatalogueEntry(
        slug="malaga-703",
        name="IRONMAN 70.3 Málaga",
        place="Málaga, Spain",
        country="ES",
        timezone="Europe/Madrid",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.HARD,
        lat=36.7213,
        lng=-4.4214,
        event_date=date(2026, 10, 18),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#a4632a",
    ),
    CatalogueEntry(
        slug="costa-navarino-703",
        name="IRONMAN 70.3 Costa Navarino, Peloponnese, Greece",
        place="Peloponnese, Greece",
        country="GR",
        timezone="Europe/Athens",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.MODERATE,
        lat=36.9636,
        lng=21.6553,
        event_date=date(2026, 10, 25),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#22648c",
    ),
    CatalogueEntry(
        slug="turkiye-703",
        name="IRONMAN 70.3 Türkiye",
        place="Belek, Türkiye",
        country="TR",
        timezone="Europe/Istanbul",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.APPROACHABLE,
        lat=36.8630,
        lng=31.0560,
        event_date=date(2026, 11, 1),
        availability=CourseAvailability.COMING_SOON,
        tone_color="#1f7a6a",
    ),
    # ---- the showcase -------------------------------------------------
    # Kalmar is not part of the season. It is the signed-out hero map and
    # nothing else, and `CourseVisibility.SHOWCASE` is what keeps it out of a
    # signed-in athlete's directory.
    CatalogueEntry(
        slug="kalmar-703",
        name="Kalmar 70.3",
        place="Kalmar, Sweden",
        country="SE",
        timezone="Europe/Stockholm",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.APPROACHABLE,
        lat=56.6570,
        lng=16.3620,
        event_date=None,
        availability=CourseAvailability.AVAILABLE,
        visibility=CourseVisibility.SHOWCASE,
        tone_color="#2b6ea8",
        bundle_slug="kalmar-703",
    ),
)

#: Slug of the one course the signed-out marketing pages draw their map from.
SHOWCASE_SLUG = "kalmar-703"

BY_SLUG: dict[str, CatalogueEntry] = {entry.slug: entry for entry in CATALOGUE}

#: Bundles the seed loads. Anything else under ``out/bundles/`` is a test
#: fixture and must not reach the directory.
SHIPPING_BUNDLE_SLUGS: tuple[str, ...] = tuple(
    entry.bundle_slug for entry in CATALOGUE if entry.bundle_slug is not None
)
