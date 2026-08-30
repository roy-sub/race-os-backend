"""Reading a race file: FIT, GPX or TCX.

Written in-tree for the same reason the FIT *writer* is: the format is a
documented binary layout, three message types carry everything an analysis
needs, and a dependency here would be a large one whose failure modes we
would still have to handle. Decoding it ourselves keeps the byte layout
visible and under test.

**Every failure names what is missing.** "Upload failed" is not a message an
athlete can act on; "this file has no distance channel, so pacing cannot be
compared" is.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from raceos.domain.enums import RaceFileFormat

#: The FIT epoch, matching the writer.
FIT_EPOCH = datetime(1989, 12, 31, tzinfo=UTC)

SEMICIRCLES_PER_DEGREE = 2**31 / 180.0

#: FIT global message numbers this decoder reads. Everything else is skipped
#: by its declared length, which is what makes an unknown message harmless.
GLOBAL_RECORD = 20
GLOBAL_SESSION = 18

#: base type -> (struct format, size, invalid sentinel)
_BASE_TYPES: dict[int, tuple[str, int, int | None]] = {
    0x00: ("B", 1, 0xFF),  # enum
    0x01: ("b", 1, 0x7F),  # sint8
    0x02: ("B", 1, 0xFF),  # uint8
    0x83: ("h", 2, 0x7FFF),  # sint16
    0x84: ("H", 2, 0xFFFF),  # uint16
    0x85: ("i", 4, 0x7FFFFFFF),  # sint32
    0x86: ("I", 4, 0xFFFFFFFF),  # uint32
    0x07: ("s", 1, None),  # string
    0x88: ("f", 4, None),  # float32
    0x89: ("d", 8, None),  # float64
    0x0A: ("B", 1, 0x00),  # uint8z
    0x8B: ("H", 2, 0x0000),  # uint16z
    0x8C: ("I", 4, 0x00000000),  # uint32z
    0x0D: ("B", 1, 0xFF),  # byte
    0x8E: ("q", 8, 0x7FFFFFFFFFFFFFFF),  # sint64
    0x8F: ("Q", 8, 0xFFFFFFFFFFFFFFFF),  # uint64
    0x90: ("Q", 8, 0x0000000000000000),  # uint64z
}


class RaceFileError(ValueError):
    """The file could not be read. The message names what is missing."""


@dataclass(frozen=True)
class TrackPoint:
    """One sample. Channels a file does not carry are ``None``, never zero.

    Zero is a real power reading and a real heart rate; conflating "absent"
    with "zero" is how an analysis reports that an athlete coasted a climb.
    """

    elapsed_s: float
    distance_m: float
    elevation_m: float | None = None
    power_w: float | None = None
    heart_rate_bpm: float | None = None
    lat: float | None = None
    lng: float | None = None


@dataclass(frozen=True)
class ParsedRaceFile:
    format: RaceFileFormat
    points: tuple[TrackPoint, ...]
    started_at: datetime | None

    @property
    def total_distance_m(self) -> float:
        return self.points[-1].distance_m if self.points else 0.0

    @property
    def total_elapsed_s(self) -> float:
        return self.points[-1].elapsed_s if self.points else 0.0

    @property
    def has_power(self) -> bool:
        return any(point.power_w is not None for point in self.points)

    @property
    def has_heart_rate(self) -> bool:
        return any(point.heart_rate_bpm is not None for point in self.points)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def detect_format(data: bytes, filename: str) -> RaceFileFormat:
    """The **content** decides, not the extension.

    A `.fit` file that is actually XML is a mislabelled export, not a corrupt
    FIT file, and telling the athlete the right thing depends on looking.
    """
    if len(data) >= 12 and data[8:12] == b".FIT":
        return RaceFileFormat.FIT
    head = data[:512].lstrip()
    if head.startswith(b"<"):
        lowered = head.lower()
        if b"<gpx" in lowered:
            return RaceFileFormat.GPX
        if b"trainingcenterdatabase" in lowered or b"<activities" in lowered:
            return RaceFileFormat.TCX
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    raise RaceFileError(
        "This does not look like a FIT, GPX or TCX file"
        + (f" (the name says .{suffix})." if suffix else ".")
        + " Export the activity again from your platform and upload that."
    )


def parse(data: bytes, filename: str) -> ParsedRaceFile:
    fmt = detect_format(data, filename)
    if fmt is RaceFileFormat.FIT:
        parsed = parse_fit(data)
    elif fmt is RaceFileFormat.GPX:
        parsed = parse_gpx(data)
    else:
        parsed = parse_tcx(data)

    if len(parsed.points) < 2:
        raise RaceFileError(
            "This file contains fewer than two usable track points, so there "
            "is nothing to compare against your plan."
        )
    if parsed.total_distance_m <= 0:
        raise RaceFileError(
            "This file has no distance channel, so pacing cannot be compared. "
            "Export it again with GPS or distance data included."
        )
    return parsed


# ---------------------------------------------------------------------------
# FIT
# ---------------------------------------------------------------------------


@dataclass
class _FieldDef:
    number: int
    size: int
    base_type: int


def parse_fit(data: bytes) -> ParsedRaceFile:
    """Decode `record` messages. Unknown messages are skipped by length."""
    if len(data) < 14:
        raise RaceFileError("This FIT file is too short to contain any activity.")

    header_size = data[0]
    if header_size not in (12, 14):
        raise RaceFileError(
            f"This FIT file declares a {header_size}-byte header, which is not "
            f"a size the format defines."
        )
    data_size = struct.unpack_from("<I", data, 4)[0]
    start = header_size
    end = min(len(data), start + data_size)

    definitions: dict[int, tuple[int, str, list[_FieldDef]]] = {}
    samples: list[dict[int, float | int]] = []

    offset = start
    while offset < end:
        header = data[offset]
        offset += 1

        if header & 0x80:
            # Compressed timestamp header: 5 bits of local type context and a
            # time offset. Its payload still follows the local definition.
            local_type = (header >> 5) & 0x03
            definition = definitions.get(local_type)
            if definition is None:
                raise RaceFileError(
                    "This FIT file uses a message before defining it, so it is "
                    "not readable. Export the activity again."
                )
            offset = _read_data(data, offset, definition, samples)
            continue

        local_type = header & 0x0F
        if header & 0x40:
            offset, definition = _read_definition(data, offset, developer=bool(header & 0x20))
            definitions[local_type] = definition
            continue

        definition = definitions.get(local_type)
        if definition is None:
            raise RaceFileError(
                "This FIT file uses a message before defining it, so it is not "
                "readable. Export the activity again."
            )
        offset = _read_data(data, offset, definition, samples)

    if not samples:
        raise RaceFileError(
            "This FIT file contains no track records — it may be a course or "
            "a settings file rather than an activity."
        )

    base_timestamp = min((sample[253] for sample in samples if 253 in sample), default=None)
    started_at = (
        FIT_EPOCH.fromtimestamp(0, UTC)
        if base_timestamp is None
        else FIT_EPOCH + _seconds(float(base_timestamp))
    )

    points: list[TrackPoint] = []
    for sample in samples:
        distance_raw = sample.get(5)
        if distance_raw is None:
            continue
        timestamp = sample.get(253)
        elapsed = (
            float(timestamp) - float(base_timestamp)
            if timestamp is not None and base_timestamp is not None
            else float(len(points))
        )
        altitude = sample.get(78)
        elevation = (
            float(altitude) / 5.0 - 500.0
            if altitude is not None
            else (float(sample[2]) / 5.0 - 500.0 if 2 in sample else None)
        )
        points.append(
            TrackPoint(
                elapsed_s=elapsed,
                distance_m=float(distance_raw) / 100.0,
                elevation_m=elevation,
                power_w=float(sample[7]) if 7 in sample else None,
                heart_rate_bpm=float(sample[3]) if 3 in sample else None,
                lat=float(sample[0]) / SEMICIRCLES_PER_DEGREE if 0 in sample else None,
                lng=float(sample[1]) / SEMICIRCLES_PER_DEGREE if 1 in sample else None,
            )
        )

    points.sort(key=lambda point: point.elapsed_s)
    return ParsedRaceFile(format=RaceFileFormat.FIT, points=tuple(points), started_at=started_at)


def _seconds(value: float):  # type: ignore[no-untyped-def]
    from datetime import timedelta

    return timedelta(seconds=value)


def _read_definition(
    data: bytes, offset: int, *, developer: bool
) -> tuple[int, tuple[int, str, list[_FieldDef]]]:
    _reserved, architecture = struct.unpack_from("<BB", data, offset)
    offset += 2
    order = ">" if architecture == 1 else "<"
    global_num = struct.unpack_from(f"{order}H", data, offset)[0]
    offset += 2
    field_count = data[offset]
    offset += 1

    fields: list[_FieldDef] = []
    for _ in range(field_count):
        number, size, base_type = struct.unpack_from("<BBB", data, offset)
        offset += 3
        fields.append(_FieldDef(number=number, size=size, base_type=base_type))

    if developer:
        dev_count = data[offset]
        offset += 1
        for _ in range(dev_count):
            _number, size, _index = struct.unpack_from("<BBB", data, offset)
            offset += 3
            # Developer fields are read only for their length: their meaning
            # lives in a field-description message we do not need.
            fields.append(_FieldDef(number=-1, size=size, base_type=0x0D))

    return offset, (global_num, order, fields)


def _read_data(
    data: bytes,
    offset: int,
    definition: tuple[int, str, list[_FieldDef]],
    samples: list[dict[int, float | int]],
) -> int:
    global_num, order, fields = definition
    values: dict[int, float | int] = {}

    for field in fields:
        raw = data[offset : offset + field.size]
        offset += field.size
        if global_num not in (GLOBAL_RECORD, GLOBAL_SESSION) or field.number < 0:
            continue
        value = _decode(raw, field, order)
        if value is not None:
            values[field.number] = value

    if global_num == GLOBAL_RECORD and values:
        samples.append(values)
    return offset


def _decode(raw: bytes, field: _FieldDef, order: str) -> float | int | None:
    spec = _BASE_TYPES.get(field.base_type)
    if spec is None or spec[0] == "s":
        return None
    fmt, size, invalid = spec
    if len(raw) < size:
        return None
    # Only the first element of an array field is read: every channel this
    # decoder uses is scalar, and taking element zero of an array is the
    # documented behaviour for a scalar reader.
    value = struct.unpack_from(f"{order}{fmt}", raw, 0)[0]
    if invalid is not None and value == invalid:
        return None
    return value  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# GPX and TCX
# ---------------------------------------------------------------------------


def _strip_namespace(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    cleaned = text.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance on a sphere.

    Sufficient here: the error against an ellipsoid is well under the GPS
    noise already present in the coordinates, and this only ever sums
    consecutive samples a few metres apart.
    """
    from math import asin, cos, radians, sin, sqrt

    radius = 6_371_008.8
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = phi2 - phi1
    dlambda = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return 2 * radius * asin(sqrt(a))


def _points_from_samples(
    samples: list[
        tuple[datetime | None, float | None, float | None, float | None, float | None, float | None]
    ],
    declared_distances: list[float | None],
) -> tuple[tuple[TrackPoint, ...], datetime | None]:
    """Turn raw samples into cumulative-distance track points.

    A declared distance channel is preferred; where the file has none,
    distance is integrated from the coordinates. Deriving it is what makes a
    plain GPX comparable at all — most GPX exports carry no distance.
    """
    if not samples:
        return (), None

    start_time = next((moment for moment, *_ in samples if moment is not None), None)
    points: list[TrackPoint] = []
    cumulative = 0.0
    previous: tuple[float, float] | None = None

    for index, (moment, lat, lng, elevation, power, heart_rate) in enumerate(samples):
        declared = declared_distances[index] if index < len(declared_distances) else None
        if declared is not None:
            cumulative = declared
        elif lat is not None and lng is not None:
            if previous is not None:
                cumulative += _haversine_m(previous[0], previous[1], lat, lng)
            previous = (lat, lng)

        elapsed = (
            (moment - start_time).total_seconds()
            if moment is not None and start_time is not None
            else float(index)
        )
        points.append(
            TrackPoint(
                elapsed_s=elapsed,
                distance_m=cumulative,
                elevation_m=elevation,
                power_w=power,
                heart_rate_bpm=heart_rate,
                lat=lat,
                lng=lng,
            )
        )
    return tuple(points), start_time


def _float(text: str | None) -> float | None:
    if text is None:
        return None
    try:
        return float(text.strip())
    except (TypeError, ValueError):
        return None


def parse_gpx(data: bytes) -> ParsedRaceFile:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise RaceFileError(
            f"This GPX file is not valid XML ({error.msg}). Export it again."
        ) from error

    samples = []
    declared: list[float | None] = []
    for element in root.iter():
        if _strip_namespace(element.tag) != "trkpt":
            continue
        lat = _float(element.get("lat"))
        lng = _float(element.get("lon"))
        elevation = None
        moment = None
        power = None
        heart_rate = None
        for child in element.iter():
            name = _strip_namespace(child.tag)
            if name == "ele":
                elevation = _float(child.text)
            elif name == "time":
                moment = _parse_time(child.text)
            elif name == "power":
                power = _float(child.text)
            elif name == "hr":
                heart_rate = _float(child.text)
        samples.append((moment, lat, lng, elevation, power, heart_rate))
        declared.append(None)

    if not samples:
        raise RaceFileError(
            "This GPX file has no track points. It may be a route or waypoint "
            "file rather than a recorded activity."
        )
    points, started_at = _points_from_samples(samples, declared)
    return ParsedRaceFile(format=RaceFileFormat.GPX, points=points, started_at=started_at)


def parse_tcx(data: bytes) -> ParsedRaceFile:
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise RaceFileError(
            f"This TCX file is not valid XML ({error.msg}). Export it again."
        ) from error

    samples = []
    declared: list[float | None] = []
    for element in root.iter():
        if _strip_namespace(element.tag) != "Trackpoint":
            continue
        moment = None
        lat = lng = elevation = distance = power = heart_rate = None
        for child in element.iter():
            name = _strip_namespace(child.tag)
            if name == "Time":
                moment = _parse_time(child.text)
            elif name == "LatitudeDegrees":
                lat = _float(child.text)
            elif name == "LongitudeDegrees":
                lng = _float(child.text)
            elif name == "AltitudeMeters":
                elevation = _float(child.text)
            elif name == "DistanceMeters":
                distance = _float(child.text)
            elif name == "Watts":
                power = _float(child.text)
            elif name == "Value" and heart_rate is None:
                heart_rate = _float(child.text)
        samples.append((moment, lat, lng, elevation, power, heart_rate))
        declared.append(distance)

    if not samples:
        raise RaceFileError(
            "This TCX file has no trackpoints, so there is nothing to compare " "against your plan."
        )
    points, started_at = _points_from_samples(samples, declared)
    return ParsedRaceFile(format=RaceFileFormat.TCX, points=points, started_at=started_at)
