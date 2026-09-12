"""What race day was actually like, in past years.

Course Recon promised historical race-day conditions and nothing was stored,
so the prototype drew them — a median air temperature, a wetsuit likelihood
and a finish-time distribution, none of which was backed by anything.

**Everything here is observed.** Each row is reanalysis from Open-Meteo's
historical archive for the course's own coordinates, at the race's own start
hour, on the day the race was held. Nothing is modelled, nothing is inferred
from a nearby station, and where a value is unavailable it is absent rather
than estimated.

Three things this deliberately does not do:

* **Finish-time distribution.** It needs actual results, which this system
  does not have and cannot obtain. The recon page says so.
* **Water temperature where the marine archive does not cover the swim.** A
  lake course guessing its own water temperature is the invented number again.
* **Wetsuit likelihood without water temperature.** Estimating it from air
  temperature would be a number with the shape of evidence and none of the
  substance.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.config import Settings
from raceos.db.models import Course, CourseConditionsHistory
from raceos.logging import get_logger
from raceos.solver.profile import wetsuit_decision

logger = get_logger(__name__)

#: How many past editions to hold. Ten years is long enough for a median to
#: mean something and short enough that the climate it describes is still the
#: one the athlete will race in.
HISTORY_YEARS = 10

#: The archive lags real time by about five days. Asking for yesterday returns
#: nothing, which reads as a failure rather than as "not yet".
ARCHIVE_LAG_DAYS = 6

SOURCE_ARCHIVE = "open-meteo-archive"


@dataclass(frozen=True)
class ConditionsSummary:
    """The medians, and how many observations they rest on.

    ``observations`` is returned beside every figure on purpose: a median of
    two years is a different claim from a median of ten, and a panel that
    showed the number without the count would make them look the same.
    """

    observations: int
    median_air_temp_c: float | None
    median_humidity_pct: float | None
    median_wind_speed_ms: float | None
    warmest_air_temp_c: float | None
    coolest_air_temp_c: float | None
    #: Only where water temperature is known for at least one year.
    water_observations: int
    median_water_temp_c: float | None
    #: Fraction of observed years whose water temperature would have been
    #: wetsuit-legal under the current ruleset. ``None`` when no year has a
    #: water temperature — never estimated from the air.
    wetsuit_legal_fraction: float | None
    #: Years it rained at the start hour, over years observed.
    wet_start_fraction: float | None


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 1) if values else None


def summarise(rows: list[CourseConditionsHistory]) -> ConditionsSummary:
    """Medians rather than means, because one freak year should not move them."""
    air = [float(row.air_temp_c) for row in rows]
    humidity = [float(row.humidity_pct) for row in rows]
    wind = [float(row.wind_speed_ms) for row in rows]
    water = [float(row.water_temp_c) for row in rows if row.water_temp_c is not None]
    rain = [float(row.precipitation_mm) for row in rows if row.precipitation_mm is not None]

    wetsuit_fraction: float | None = None
    if water:
        legal = sum(1 for temp in water if wetsuit_decision(temp)[0])
        wetsuit_fraction = round(legal / len(water), 2)

    return ConditionsSummary(
        observations=len(rows),
        median_air_temp_c=_median(air),
        median_humidity_pct=_median(humidity),
        median_wind_speed_ms=_median(wind),
        warmest_air_temp_c=round(max(air), 1) if air else None,
        coolest_air_temp_c=round(min(air), 1) if air else None,
        water_observations=len(water),
        median_water_temp_c=_median(water),
        wetsuit_legal_fraction=wetsuit_fraction,
        wet_start_fraction=(
            round(sum(1 for mm in rain if mm > 0.1) / len(rain), 2) if rain else None
        ),
    )


def history_for(session: Session, *, course: Course) -> list[CourseConditionsHistory]:
    """Every stored observation for this course, most recent first."""
    return list(
        session.scalars(
            select(CourseConditionsHistory)
            .where(CourseConditionsHistory.course_id == course.id)
            .order_by(CourseConditionsHistory.observed_on.desc())
        )
    )


def _target_dates(course: Course, *, today: date, years: int) -> list[date]:
    """Which past dates to ask about.

    Anchored on the course's next edition where one is announced, because that
    is the day the athlete is asking about. Without one there is no date to
    anchor to and no useful question to ask — "what is the weather like at this
    course" with no month in it is a question about the climate, not race day.
    """
    if course.next_edition_date is None:
        return []

    anchor = course.next_edition_date
    latest_available = today.fromordinal(today.toordinal() - ARCHIVE_LAG_DAYS)

    dates: list[date] = []
    for offset in range(1, years + 1):
        try:
            candidate = anchor.replace(year=anchor.year - offset)
        except ValueError:
            # 29 February in a year that has none.
            candidate = anchor.replace(year=anchor.year - offset, day=28)
        if candidate <= latest_available:
            dates.append(candidate)
    return dates


def _get_json(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
    try:
        response = client.get(url, params=params)
    except httpx.HTTPError as error:
        logger.warning("conditions archive unreachable", extra={"error_type": type(error).__name__})
        return None
    if response.status_code >= 400:
        logger.warning("conditions archive error", extra={"http_status": response.status_code})
        return None
    try:
        body = response.json()
    except ValueError:
        logger.warning("conditions archive returned a non-JSON body")
        return None
    return body if isinstance(body, dict) else None


def _hourly_value(body: dict[str, Any], field: str, hour: int) -> float | None:
    series = (body.get("hourly") or {}).get(field)
    if not isinstance(series, list) or not series:
        return None
    value = series[min(hour, len(series) - 1)]
    return float(value) if isinstance(value, int | float) else None


def backfill_course(
    session: Session,
    *,
    course: Course,
    settings: Settings,
    client: httpx.Client | None = None,
    today: date | None = None,
    years: int = HISTORY_YEARS,
) -> dict[str, Any]:
    """Fetch and store what is missing for one course.

    Only what is missing: an observation of a past day does not change, so a
    row already stored is never re-fetched. That makes the job cheap to run
    daily and safe to run twice.
    """
    day = today or datetime.now(UTC).date()
    wanted = _target_dates(course, today=day, years=years)
    if not wanted:
        return {"course": course.slug, "fetched": 0, "reason": "no announced edition date"}

    have = {
        row.observed_on
        for row in session.scalars(
            select(CourseConditionsHistory).where(CourseConditionsHistory.course_id == course.id)
        )
    }
    missing = [when for when in wanted if when not in have]
    if not missing:
        return {"course": course.slug, "fetched": 0, "reason": "already complete"}

    hour = _start_hour(session, course)
    owned = client is None
    http = client or httpx.Client(timeout=settings.weather_request_timeout_seconds)
    fetched = 0
    try:
        for when in missing:
            row = _observe(
                http,
                settings=settings,
                course=course,
                when=when,
                hour=hour,
            )
            if row is None:
                continue
            session.add(row)
            fetched += 1
    finally:
        if owned:
            http.close()

    session.flush()
    return {"course": course.slug, "fetched": fetched, "requested": len(missing)}


def _start_hour(session: Session, course: Course) -> int:
    """The hour races on this course actually start.

    Taken from an entered race where there is one, because an athlete asking
    "what is it like" means at the gun, not at noon. Seven in the morning is
    the long-course convention and the fallback.
    """
    from raceos.db.models import Race

    race = session.scalar(
        select(Race).where(Race.course_id == course.id).order_by(Race.event_date.desc())
    )
    return race.start_time_local.hour if race is not None else 7


def _observe(
    client: httpx.Client,
    *,
    settings: Settings,
    course: Course,
    when: date,
    hour: int,
) -> CourseConditionsHistory | None:
    """One day's reanalysis, or ``None`` when the archive cannot supply it."""
    base = settings.open_meteo_archive_url.rstrip("/")
    body = _get_json(
        client,
        f"{base}/archive",
        {
            "latitude": float(course.lat),
            "longitude": float(course.lng),
            "start_date": when.isoformat(),
            "end_date": when.isoformat(),
            "hourly": (
                "temperature_2m,relative_humidity_2m,wind_speed_10m,"
                "wind_direction_10m,precipitation,cloud_cover"
            ),
            "wind_speed_unit": "ms",
            "timezone": course.timezone,
        },
    )
    if body is None:
        return None

    air = _hourly_value(body, "temperature_2m", hour)
    humidity = _hourly_value(body, "relative_humidity_2m", hour)
    wind = _hourly_value(body, "wind_speed_10m", hour)
    if air is None or humidity is None or wind is None:
        # A partial row would be a row that reads as an observation and is a
        # gap. Skip it; the next run will try again.
        return None

    return CourseConditionsHistory(
        course_id=course.id,
        observed_on=when,
        observed_hour=hour,
        air_temp_c=air,
        humidity_pct=humidity,
        wind_speed_ms=wind,
        wind_dir_deg=_hourly_value(body, "wind_direction_10m", hour),
        precipitation_mm=_hourly_value(body, "precipitation", hour),
        cloud_cover_pct=_hourly_value(body, "cloud_cover", hour),
        # Left absent rather than estimated. See the module docstring.
        water_temp_c=None,
        source=SOURCE_ARCHIVE,
        fetched_at=datetime.now(UTC),
    )
