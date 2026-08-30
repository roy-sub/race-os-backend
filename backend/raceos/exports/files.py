"""FIT, GPX and ICS exports.

**Writing a file requires no API partnership**, which is the whole reason the
"exportable to your head unit" promise survives Part 0.4 C1's removal of every
device integration. RaceOS writes a `.FIT` course with waypoints; the athlete
side-loads it through their platform's normal import path or by copying to
`/Garmin/NewFiles` over USB.

One geometry, three consumers: the map, the solver and these exports all read
the same bundle. Divergence would be a bug, so they share one source.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from xml.etree import ElementTree as ET

#: The FIT epoch: 1989-12-31 00:00:00 UTC.
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)

#: Semicircles per degree: 2^31 / 180. FIT stores coordinates this way.
SEMICIRCLES_PER_DEGREE = 2**31 / 180.0


@dataclass(frozen=True)
class RoutePoint:
    lat: float
    lng: float
    elevation_m: float
    distance_m: float


@dataclass(frozen=True)
class Waypoint:
    """An aid station, cut-off, transition or special-needs point."""

    name: str
    lat: float
    lng: float
    distance_m: float
    kind: str


# ---------------------------------------------------------------------------
# GPX — route only, no waypoints
# ---------------------------------------------------------------------------


def render_gpx(*, course_name: str, points: list[RoutePoint], attribution: str) -> bytes:
    """Route only. Waypoints belong in the FIT export (Part 12.2).

    Attribution is written into the metadata because ODbL obliges it wherever
    the derived data is displayed, and an exported file is a display surface
    that outlives the session that produced it.
    """
    gpx = ET.Element(
        "gpx",
        {
            "version": "1.1",
            "creator": "RaceOS",
            "xmlns": "http://www.topografix.com/GPX/1/1",
        },
    )
    metadata = ET.SubElement(gpx, "metadata")
    ET.SubElement(metadata, "name").text = course_name
    ET.SubElement(metadata, "desc").text = attribution
    ET.SubElement(metadata, "time").text = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )

    track = ET.SubElement(gpx, "trk")
    ET.SubElement(track, "name").text = course_name
    segment = ET.SubElement(track, "trkseg")
    for point in points:
        node = ET.SubElement(
            segment, "trkpt", {"lat": f"{point.lat:.6f}", "lon": f"{point.lng:.6f}"}
        )
        ET.SubElement(node, "ele").text = f"{point.elevation_m:.2f}"

    document: bytes = ET.tostring(gpx, encoding="utf-8", xml_declaration=True)
    return document


# ---------------------------------------------------------------------------
# ICS — race-week calendar
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarEvent:
    summary: str
    description: str
    on_date: date
    all_day: bool = True


def race_week_events(
    *, event_date: date, course_name: str, has_special_needs: bool
) -> list[CalendarEvent]:
    """Derived from the **actual event date**, never fixed weekday labels.

    The mock's "Thursday: bike check-in" is a rendering of "two days before";
    storing the weekday would be wrong for any race that is not on a Sunday.
    """
    events = [
        CalendarEvent(
            summary=f"{course_name} — race day",
            description="Race morning. Everything is already decided.",
            on_date=event_date,
        ),
        CalendarEvent(
            summary=f"{course_name} — bike check-in",
            description="Rack the bike and hand in your bags.",
            on_date=event_date - timedelta(days=1),
        ),
        CalendarEvent(
            summary=f"{course_name} — pack bags",
            description="Pack all five bags against the manifests.",
            on_date=event_date - timedelta(days=2),
        ),
        CalendarEvent(
            summary=f"{course_name} — registration opens",
            description="Collect your race pack and timing chip.",
            on_date=event_date - timedelta(days=3),
        ),
    ]
    if has_special_needs:
        events.append(
            CalendarEvent(
                summary=f"{course_name} — special-needs deadline",
                description="Special-needs bags must be handed in.",
                on_date=event_date - timedelta(days=1),
            )
        )
    return sorted(events, key=lambda e: e.on_date)


def render_ics(*, events: list[CalendarEvent], calendar_name: str) -> bytes:
    """RFC 5545. Hand-rolled rather than pulled from a library.

    The output is a dozen lines of a stable format, and the alternative adds a
    dependency whose escaping rules we would still have to verify.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//RaceOS//Race week//EN",
        "CALSCALE:GREGORIAN",
        f"X-WR-CALNAME:{_ics_escape(calendar_name)}",
    ]
    for index, event in enumerate(events):
        start = event.on_date.strftime("%Y%m%d")
        end = (event.on_date + timedelta(days=1)).strftime("%Y%m%d")
        lines += [
            "BEGIN:VEVENT",
            f"UID:raceos-{stamp}-{index}@raceos",
            f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{start}",
            f"DTEND;VALUE=DATE:{end}",
            f"SUMMARY:{_ics_escape(event.summary)}",
            f"DESCRIPTION:{_ics_escape(event.description)}",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", r"\;").replace(",", "\\,").replace("\n", "\\n")


# ---------------------------------------------------------------------------
# FIT — course with waypoints
# ---------------------------------------------------------------------------


def _fit_timestamp(moment: datetime) -> int:
    return int((moment - FIT_EPOCH).total_seconds())


def _semicircles(degrees: float) -> int:
    return int(degrees * SEMICIRCLES_PER_DEGREE)


class _FitWriter:
    """A minimal FIT encoder for the course profile.

    Written directly rather than through the SDK's higher-level helpers
    because a course file needs exactly four message types and the encoding is
    a documented binary layout. Doing it here keeps the byte layout — and the
    CRC, which head units do check — visible and testable.
    """

    def __init__(self) -> None:
        self._body = bytearray()

    def definition(self, local: int, global_num: int, fields: list[tuple[int, int, int]]) -> None:
        """`fields` is (field_def_num, size_bytes, base_type)."""
        self._body.append(0x40 | local)
        self._body += struct.pack("<BBHB", 0, 0, global_num, len(fields))
        for number, size, base_type in fields:
            self._body += struct.pack("<BBB", number, size, base_type)

    def data(self, local: int, payload: bytes) -> None:
        self._body.append(local)
        self._body += payload

    def finish(self) -> bytes:
        header = struct.pack("<BBHI4s", 12, 0x20, 2195, len(self._body), b".FIT")
        content = header + bytes(self._body)
        return content + struct.pack("<H", _fit_crc(content))


_CRC_TABLE = (
    0x0000,
    0xCC01,
    0xD801,
    0x1400,
    0xF001,
    0x3C00,
    0x2800,
    0xE401,
    0xA001,
    0x6C00,
    0x7800,
    0xB401,
    0x5000,
    0x9C01,
    0x8801,
    0x4400,
)


def _fit_crc(data: bytes) -> int:
    """The FIT 16-bit CRC. Head units reject a file whose CRC is wrong."""
    crc = 0
    for byte in data:
        for nibble in (byte & 0x0F, (byte >> 4) & 0x0F):
            tmp = _CRC_TABLE[crc & 0xF]
            crc = (crc >> 4) & 0x0FFF
            crc = crc ^ tmp ^ _CRC_TABLE[nibble]
    return crc


def render_fit_course(
    *,
    course_name: str,
    points: list[RoutePoint],
    waypoints: list[Waypoint],
    created_at: datetime | None = None,
) -> bytes:
    """A FIT course with a waypoint at every aid station and cut-off.

    ``created_at`` is a parameter rather than a clock read so the output is
    reproducible: two exports of the same plan should differ only if the plan
    differs.
    """
    moment = created_at or datetime.now(UTC)
    timestamp = _fit_timestamp(moment)
    writer = _FitWriter()

    # file_id (global 0)
    writer.definition(0, 0, [(0, 1, 0x00), (1, 2, 0x84), (4, 4, 0x86)])
    writer.data(0, struct.pack("<BHI", 6, 1, timestamp))

    # course (global 31): just the name
    name = course_name.encode("utf-8")[:31] + b"\x00"
    writer.definition(1, 31, [(5, len(name), 0x07)])
    writer.data(1, name)

    # lap (global 19): totals, so a head unit can show distance remaining
    total_distance = points[-1].distance_m if points else 0.0
    writer.definition(2, 19, [(253, 4, 0x86), (3, 4, 0x85), (4, 4, 0x85), (9, 4, 0x86)])
    writer.data(
        2,
        struct.pack(
            "<IiiI",
            timestamp,
            _semicircles(points[0].lat) if points else 0,
            _semicircles(points[0].lng) if points else 0,
            int(total_distance * 100),
        ),
    )

    # record (global 20): the route itself
    writer.definition(
        3, 20, [(253, 4, 0x86), (0, 4, 0x85), (1, 4, 0x85), (2, 2, 0x84), (5, 4, 0x86)]
    )
    for point in points:
        writer.data(
            3,
            struct.pack(
                "<IiiHI",
                timestamp,
                _semicircles(point.lat),
                _semicircles(point.lng),
                # FIT altitude: (metres + 500) * 5, unsigned 16-bit.
                max(0, min(65535, int((point.elevation_m + 500.0) * 5))),
                int(point.distance_m * 100),
            ),
        )

    # course_point (global 32): the waypoints that make this useful on a bike
    for waypoint in waypoints:
        label = waypoint.name.encode("utf-8")[:15] + b"\x00"
        writer.definition(
            4,
            32,
            [(1, 4, 0x86), (2, 4, 0x85), (3, 4, 0x85), (4, 4, 0x86), (6, len(label), 0x07)],
        )
        writer.data(
            4,
            struct.pack(
                "<IiiI",
                timestamp,
                _semicircles(waypoint.lat),
                _semicircles(waypoint.lng),
                int(waypoint.distance_m * 100),
            )
            + label,
        )

    return writer.finish()


#: Shown alongside the download. Writing the file is the whole integration;
#: this is the rest of the promise (Part 9.3).
FIT_IMPORT_INSTRUCTIONS: dict[str, list[str]] = {
    "garmin": [
        "Connect your device by USB and open its drive.",
        "Copy the .fit file into /Garmin/NewFiles.",
        "Eject the device. The course appears under Navigation > Courses.",
    ],
    "wahoo": [
        "Open the Wahoo companion app.",
        "Routes > Add route > Import from file, and choose the .fit file.",
        "Sync; the route appears on the head unit.",
    ],
    "other": [
        "Most head units import a .fit course from their companion app's "
        "route or course import screen.",
        "If yours takes GPX instead, use the GPX export — it carries the same "
        "geometry without waypoints.",
    ],
}
