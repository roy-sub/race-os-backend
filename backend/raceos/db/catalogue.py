"""The race catalogue: the declared set of events the product ships.

**The catalogue is a manifest, not "whatever bundles happen to exist."** That
distinction is the point of this module. The directory used to be the contents
of ``pipelines/course-ingest/out/bundles/``, which meant a fixture dropped in
for a test would appear on the live site, and an event the season announces
could not be listed until its bundle was finished. Both are wrong. What an
athlete sees is declared here, once, and the bundles fill it in.

Three kinds of row:

* **Available.** A generated bundle exists and is loaded, so the race can be
  entered and planned. Seven events, plus the showcase.
* **Coming soon.** Announced and dated, no course data yet. Listed honestly,
  and not enterable — :func:`raceos.services.course_service` refuses, and the
  directory renders the row as unavailable rather than hiding it.
* **Showcase.** Kalmar 70.3, and only Kalmar. It is the signed-out marketing
  map: a deliberately stylised, out-of-scale illustration of what a course map
  looks like. It is never listed to a signed-in athlete, because sitting it
  beside surveyed courses would invite it to be read as one.

Adding an event is a spec in ``pipelines/course-ingest/specs/``, a generated
bundle, and flipping one ``availability`` value here.

**Races that have been run are removed outright.** The 2026 September events —
Nice, Wales, Belgrade, Erkner, both Emilia-Romagna distances and Weymouth —
are gone from this manifest, which retires their rows on the next seed rather
than deleting them, so plans already solved against one keep working.
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
        availability=CourseAvailability.AVAILABLE,
        tone_color="#8a5a2b",
        bundle_slug="calella-barcelona-full",
    ),
    # Announced and dated, and the one row in the season with no course data.
    # The Yvelines has no mapped open water wide enough to hold a 70.3 swim —
    # see `pipelines/course-ingest/specs/12-versailles-703.yaml`, which records
    # what is missing — so the bundle cannot be generated and the row stays
    # honest about it rather than pretending to be enterable.
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
        availability=CourseAvailability.AVAILABLE,
        tone_color="#2b6ea8",
        bundle_slug="portugal-cascais-703",
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
        availability=CourseAvailability.AVAILABLE,
        tone_color="#2b6ea8",
        bundle_slug="portugal-cascais-full",
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
        availability=CourseAvailability.AVAILABLE,
        tone_color="#a4632a",
        bundle_slug="malaga-703",
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
        availability=CourseAvailability.AVAILABLE,
        tone_color="#1f6f6b",
        bundle_slug="porec-703",
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
        availability=CourseAvailability.AVAILABLE,
        tone_color="#22648c",
        bundle_slug="costa-navarino-703",
    ),
    CatalogueEntry(
        slug="turkiye-703",
        name="IRONMAN 70.3 Türkiye",
        # Lara, not Belek: the bundle is routed from the Antalya side, because
        # Belek's resort road network cannot close a 90 km ring. The spec says
        # so, and the two should not disagree on the page.
        place="Antalya, Türkiye",
        country="TR",
        timezone="Europe/Istanbul",
        distance_type=DistanceType.HALF,
        difficulty=Difficulty.APPROACHABLE,
        lat=36.8630,
        lng=31.0560,
        event_date=date(2026, 11, 1),
        availability=CourseAvailability.AVAILABLE,
        tone_color="#1f7a6a",
        bundle_slug="turkiye-703",
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
