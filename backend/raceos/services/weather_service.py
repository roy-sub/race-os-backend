"""Forecast ingestion from Open-Meteo.

**Open-Meteo needs no API key.** A base URL is the whole configuration, and
there is deliberately no ``WEATHER_PROVIDER_API_KEY`` variable anywhere — if
an implementation ever needs one, that implementation is wrong.

Caching is database-backed with a TTL column plus a small in-process layer.
V1 has no Redis, and a forecast is exactly the kind of value that suits a TTL
row: read far more often than written, cheap to re-fetch, and harmless to
serve slightly stale.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from raceos.config import Settings
from raceos.db.models import CacheEntry
from raceos.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from raceos.db.models import Race

logger = get_logger(__name__)

NAMESPACE = "forecast"

#: Open-Meteo's WMO weather codes, mapped onto the four `conditions` values the
#: solver's globe-offset table understands. Supplying `cloud_cover_pct`
#: alongside removes this table from the numeric path entirely (§F.3), which
#: is why it is fetched too.
_WMO_CONDITIONS: dict[range, str] = {
    range(0, 2): "clear",
    range(2, 3): "partly_cloudy",
    range(3, 4): "overcast",
    range(45, 100): "rain",
}


def _conditions_from_code(code: int, cloud_cover_pct: float | None) -> str:
    for span, label in _WMO_CONDITIONS.items():
        if code in span:
            return label
    if cloud_cover_pct is not None:
        if cloud_cover_pct < 20:
            return "clear"
        if cloud_cover_pct < 55:
            return "partly_cloudy"
        if cloud_cover_pct < 85:
            return "cloudy"
    return "overcast"


@dataclass(frozen=True)
class Forecast:
    temp_c: float
    humidity: float
    wind_speed_ms: float
    wind_dir_deg: float | None
    conditions: str
    water_temp_c: float
    pressure_hpa: float | None
    cloud_cover_pct: float | None

    def as_snapshot(self) -> dict[str, Any]:
        return {
            "temp_c": self.temp_c,
            "humidity": self.humidity,
            "wind_speed_ms": self.wind_speed_ms,
            "wind_dir_deg": self.wind_dir_deg,
            "conditions": self.conditions,
            "water_temp_c": self.water_temp_c,
            "pressure_hpa": self.pressure_hpa,
            "cloud_cover_pct": self.cloud_cover_pct,
        }


def _cache_key(lat: float, lng: float, on_date: date, hour: int) -> str:
    return f"{NAMESPACE}:{lat:.3f}:{lng:.3f}:{on_date.isoformat()}:{hour:02d}"


def read_cache(session: Session, key: str) -> dict[str, Any] | None:
    entry = session.scalar(select(CacheEntry).where(CacheEntry.cache_key == key))
    if entry is None or entry.expires_at <= datetime.now(UTC):
        return None
    loaded: dict[str, Any] = json.loads(entry.value.decode("utf-8"))
    return loaded


def write_cache(session: Session, key: str, payload: dict[str, Any], settings: Settings) -> None:
    session.execute(delete(CacheEntry).where(CacheEntry.cache_key == key))
    session.add(
        CacheEntry(
            cache_key=key,
            namespace=NAMESPACE,
            value=json.dumps(payload).encode("utf-8"),
            content_type="application/json",
            expires_at=datetime.now(UTC) + timedelta(minutes=settings.forecast_cache_ttl_minutes),
        )
    )


def fetch_forecast(
    session: Session,
    *,
    lat: float,
    lng: float,
    on_date: date,
    hour: int,
    settings: Settings,
    client: httpx.Client | None = None,
    water_temp_c: float = 19.0,
) -> Forecast | None:
    """The forecast for one hour at one place, or ``None`` when unavailable.

    Returns ``None`` rather than raising: a forecast is an *improvement* to a
    plan, not a precondition for one. A provider outage must not stop an
    athlete solving — it should leave the plan on its last known forecast and
    say so.
    """
    key = _cache_key(lat, lng, on_date, hour)
    cached = read_cache(session, key)
    if cached is not None:
        return Forecast(**cached)

    horizon = (on_date - datetime.now(UTC).date()).days * 24
    if horizon > settings.weather_forecast_horizon_hours:
        # Beyond the horizon a forecast is noise, and pretending otherwise
        # produces drift events that are not real information.
        return None

    params: dict[str, str | float] = {
        "latitude": lat,
        "longitude": lng,
        "hourly": (
            "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,"
            "pressure_msl,cloud_cover,weather_code"
        ),
        "wind_speed_unit": "ms",
        "timezone": "UTC",
        "start_date": on_date.isoformat(),
        "end_date": on_date.isoformat(),
    }
    owned = client is None
    http = client or httpx.Client(timeout=settings.weather_request_timeout_seconds)
    try:
        response = http.get(f"{settings.open_meteo_base_url.rstrip('/')}/forecast", params=params)
        if response.status_code >= 400:
            logger.warning(
                "forecast provider returned an error", extra={"http_status": response.status_code}
            )
            return None
        data = response.json()
    except httpx.HTTPError as exc:
        logger.warning("forecast unavailable", extra={"error_type": type(exc).__name__})
        return None
    finally:
        if owned:
            http.close()

    hourly = data.get("hourly") or {}
    try:
        index = min(hour, len(hourly["temperature_2m"]) - 1)
        cloud = float(hourly["cloud_cover"][index])
        forecast = Forecast(
            temp_c=float(hourly["temperature_2m"][index]),
            humidity=float(hourly["relative_humidity_2m"][index]),
            wind_speed_ms=float(hourly["wind_speed_10m"][index]),
            wind_dir_deg=float(hourly["wind_direction_10m"][index]),
            conditions=_conditions_from_code(int(hourly["weather_code"][index]), cloud),
            water_temp_c=water_temp_c,
            # Open-Meteo's `pressure_msl` IS sea-level (QNH) pressure, which is
            # the convention §I.1.1 expects. Passing station pressure here
            # instead would be wrong by ~1.2% per 100 m of course elevation.
            pressure_hpa=float(hourly["pressure_msl"][index]),
            cloud_cover_pct=cloud,
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        logger.warning("forecast payload unusable", extra={"error_type": type(exc).__name__})
        return None

    write_cache(session, key, forecast.as_snapshot(), settings)
    return forecast


def purge_expired_cache(session: Session) -> int:
    result = session.execute(delete(CacheEntry).where(CacheEntry.expires_at < datetime.now(UTC)))
    return int(result.rowcount or 0)


def fetch_for_race(
    session: Session,
    *,
    race: Race,
    settings: Settings,
    client: httpx.Client | None = None,
) -> dict[str, Any] | None:
    """The forecast snapshot for a race's start hour, or ``None``.

    Resolves place and time from the course the race is pinned to, so a caller
    sweeping many races does not have to re-derive the same three lookups and
    get the timezone conversion subtly wrong in one of them.
    """
    from zoneinfo import ZoneInfo

    from raceos.db.models import Course

    course = session.get(Course, race.course_id)
    if course is None:  # pragma: no cover - FK RESTRICT
        return None
    try:
        zone = ZoneInfo(course.timezone)
    except Exception:  # pragma: no cover - a bad tz is a bundle problem
        return None

    start_local = datetime.combine(race.event_date, race.start_time_local, tzinfo=zone)
    start_utc = start_local.astimezone(UTC)
    forecast = fetch_forecast(
        session,
        lat=float(course.lat),
        lng=float(course.lng),
        # The provider is queried in UTC, so the date and hour must be the UTC
        # ones — a 07:00 start in Auckland is the previous calendar day there.
        on_date=start_utc.date(),
        hour=start_utc.hour,
        settings=settings,
        client=client,
    )
    return forecast.as_snapshot() if forecast is not None else None
