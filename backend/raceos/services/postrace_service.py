"""Post-race: upload, parse, analyse, calibrate.

Three rules shape this module.

**The comparison is against the plan version that was live at race time.**
Not the current one. A plan re-solved after the race would silently judge the
athlete against something they never raced, which is why ``plan_version`` is
stored on the analysis rather than joined at read time.

**Calibration is the only path that writes ``source = 'measured'``.** That
makes it the only path that can *upgrade* provenance while *degrading* a
number, which is the worst thing this product can produce. So a value is
derived only when the file contains evidence that genuinely qualifies
(``SOLVER_MODEL.md`` §2.5.3), and where it does not, nothing is written and
the reason is recorded.

**Every proposal is accepted or dismissed individually.** A bulk "apply all"
would let one bad derivation ride in behind four good ones.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean, pstdev
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.errors import Conflict, InvalidInput, NotFound, UploadFailed
from raceos.config import Settings
from raceos.db.models import (
    AnalysisAction,
    AnalysisCalibration,
    AnalysisCompareRow,
    Plan,
    PlanSplit,
    PostRaceAnalysis,
    PostRaceFile,
    Race,
    User,
)
from raceos.domain.enums import (
    CompareState,
    ConstraintSource,
    Leg,
    NotificationSeverity,
    NotificationType,
    RaceFileStatus,
)
from raceos.ingest.racefile import ParsedRaceFile, RaceFileError, TrackPoint, parse
from raceos.logging import get_logger
from raceos.services import constraint_service, notification_service
from raceos.solver.stages.s2_athlete import threshold_pace_from_race
from raceos.storage.base import get_storage_backend

logger = get_logger(__name__)

#: §2.5.3 step 1. The window brackets the one-hour anchor closely enough that
#: the Riegel correction stays small.
EFFORT_MIN_SECONDS = 20 * 60
EFFORT_MAX_SECONDS = 90 * 60
#: §2.5.3 step 1: a sustained effort, not a session average.
EFFORT_MAX_PACE_CV = 0.05

#: Comparison bands, as a fraction of the planned value.
_GOOD = 0.02
_OK = 0.05
_WARN = 0.10


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def _storage_key(user_id: UUID) -> str:
    """A random key, never a user-controlled path.

    The uploaded filename is untrusted input: putting it in the object key
    would let an athlete choose where their file lands.
    """
    return f"race-files/{user_id}/{secrets.token_urlsafe(24)}"


def upload(
    session: Session,
    *,
    user: User,
    plan: Plan | None,
    filename: str,
    data: bytes,
    settings: Settings,
) -> PostRaceFile:
    """Store the bytes and record the file. Parsing happens on analysis.

    Storing first means a file that we cannot parse today is still available
    when the parser improves, and the athlete does not have to find it again.
    """
    if not data:
        raise UploadFailed("That file is empty.")
    if len(data) > settings.upload_max_bytes:
        raise UploadFailed(
            f"That file is {len(data) / 1_048_576:.1f} MB. The limit is "
            f"{settings.upload_max_bytes / 1_048_576:.0f} MB — export just the "
            f"race activity rather than a whole season."
        )
    if plan is not None and plan.user_id != user.id:
        raise NotFound("Plan not found.")

    try:
        parsed = parse(data, filename)
    except RaceFileError as error:
        # Recorded as a failed row rather than discarded: the athlete gets a
        # specific reason, and support can see what they actually sent.
        record = PostRaceFile(
            user_id=user.id,
            plan_id=plan.id if plan else None,
            storage_key=_storage_key(user.id),
            original_filename=filename[:255],
            format=_guess_format(filename),
            size_bytes=len(data),
            checksum=hashlib.sha256(data).hexdigest(),
            uploaded_at=datetime.now(UTC),
            status=RaceFileStatus.FAILED,
            failure_reason=str(error),
        )
        session.add(record)
        # Committed before raising, deliberately. The endpoint's error handler
        # rolls back, and a flushed-but-uncommitted row would vanish — leaving
        # support with nothing to look at when the athlete says "it wouldn't
        # take my file". No database error has occurred, so the session is
        # sound and this commits only the failure record.
        session.commit()
        raise UploadFailed(str(error)) from error

    key = _storage_key(user.id)
    get_storage_backend(settings).put(key, data, content_type="application/octet-stream")

    record = PostRaceFile(
        user_id=user.id,
        plan_id=plan.id if plan else None,
        storage_key=key,
        original_filename=filename[:255],
        format=parsed.format,
        size_bytes=len(data),
        checksum=hashlib.sha256(data).hexdigest(),
        uploaded_at=datetime.now(UTC),
        status=RaceFileStatus.PENDING,
    )
    session.add(record)
    session.flush()
    logger.info(
        "postrace.uploaded",
        extra={
            "file_id": str(record.id),
            "format": parsed.format.value,
            "points": len(parsed.points),
        },
    )
    return record


def _guess_format(filename: str):  # type: ignore[no-untyped-def]
    from raceos.domain.enums import RaceFileFormat

    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return {
        "fit": RaceFileFormat.FIT,
        "gpx": RaceFileFormat.GPX,
        "tcx": RaceFileFormat.TCX,
    }.get(suffix, RaceFileFormat.GPX)


def load_parsed(session: Session, *, record: PostRaceFile, settings: Settings) -> ParsedRaceFile:
    data = get_storage_backend(settings).get(record.storage_key)
    return parse(data, record.original_filename)


# ---------------------------------------------------------------------------
# §2.5.3 — the qualifying effort
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualifyingEffort:
    """The single longest sustained effort in the file, if there is one."""

    start_index: int
    end_index: int
    distance_km: float
    duration_s: float
    pace_cv: float

    @property
    def pace_s_per_km(self) -> float:
        return self.duration_s / self.distance_km


def _segment_paces(points: tuple[TrackPoint, ...], start: int, end: int) -> list[float]:
    """Per-sample pace in s/km across a window.

    Samples that cover no ground are skipped rather than treated as infinitely
    slow: a stationary second at an aid station is not a pace reading.
    """
    paces: list[float] = []
    for index in range(start + 1, end + 1):
        distance = points[index].distance_m - points[index - 1].distance_m
        elapsed = points[index].elapsed_s - points[index - 1].elapsed_s
        if distance <= 0.5 or elapsed <= 0.0:
            continue
        paces.append(elapsed / (distance / 1000.0))
    return paces


def find_qualifying_effort(parsed: ParsedRaceFile) -> QualifyingEffort | None:
    """§2.5.3 steps 1–2, or ``None``.

    Returning ``None`` is a real answer, not a failure: step 4 says a derived
    value from a 12-minute interval or a four-hour ride is *worse* than no
    value, because it will carry a ``measured`` stamp.

    The search walks candidate windows with a moving end index rather than
    testing every pair, so a six-hour file at one sample per second stays
    linear in the number of samples rather than quadratic.
    """
    points = parsed.points
    if len(points) < 3:
        return None

    best: QualifyingEffort | None = None
    end = 0
    for start in range(len(points) - 1):
        if end < start + 1:
            end = start + 1
        # Grow the window to the longest duration still inside the ceiling.
        while (
            end + 1 < len(points)
            and points[end + 1].elapsed_s - points[start].elapsed_s <= EFFORT_MAX_SECONDS
        ):
            end += 1

        duration = points[end].elapsed_s - points[start].elapsed_s
        if duration < EFFORT_MIN_SECONDS:
            continue
        if best is not None and duration <= best.duration_s:
            # A later start can only match this duration, never beat it, so a
            # tie is not worth re-measuring.
            continue

        distance_km = (points[end].distance_m - points[start].distance_m) / 1000.0
        if distance_km <= 0.0:
            continue
        paces = _segment_paces(points, start, end)
        if len(paces) < 2:
            continue
        average = mean(paces)
        if average <= 0.0:
            continue
        cv = pstdev(paces) / average
        if cv >= EFFORT_MAX_PACE_CV:
            continue

        best = QualifyingEffort(
            start_index=start,
            end_index=end,
            distance_km=distance_km,
            duration_s=duration,
            pace_cv=cv,
        )
    return best


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def _state(planned: float, actual: float) -> CompareState:
    """Bands on the fraction missed, in the direction that matters.

    Being faster than planned is not automatically good — over-biking is how
    athletes blow up — but it is not the thing an analysis flags red, so the
    band is on absolute deviation and the `why` text carries the direction.
    """
    if planned <= 0:
        return CompareState.OK
    drift = abs(actual - planned) / planned
    if drift <= _GOOD:
        return CompareState.GOOD
    if drift <= _OK:
        return CompareState.OK
    if drift <= _WARN:
        return CompareState.WARN
    return CompareState.BAD


def _clock(minutes: float) -> str:
    total = int(round(minutes))
    sign = "-" if total < 0 else ""
    total = abs(total)
    return f"{sign}{total // 60}:{total % 60:02d}"


@dataclass(frozen=True)
class CalibrationProposal:
    constraint_key: str
    was: float
    now: float
    evidence_text: str


def _propose_run_threshold(
    session: Session, *, user: User, parsed: ParsedRaceFile
) -> tuple[CalibrationProposal | None, str]:
    """§2.5.3 in full, including step 4.

    Returns the proposal and the reason — the reason is recorded whether or
    not a value was derived, because "we looked and your file had no
    qualifying effort" is information the athlete should be able to read.
    """
    effort = find_qualifying_effort(parsed)
    if effort is None:
        return None, (
            "No sustained effort of 20 to 90 minutes with steady pacing was "
            "found in this file, so run threshold was left as it was. A value "
            "derived from a short interval or a whole-day average would carry "
            "a measured stamp it has not earned."
        )

    derived = threshold_pace_from_race(effort.distance_km, effort.duration_s, user.level)
    current = constraint_service.get_value(session, athlete_id=user.id, key="run_threshold_pace")
    if current is None:
        return None, (
            "Run threshold has never been set, so there is nothing to " "calibrate against."
        )

    evidence = (
        f"Derived from {effort.distance_km:.2f} km in "
        f"{effort.duration_s / 60:.0f} min at a pace variation of "
        f"{effort.pace_cv * 100:.1f}%, converted to a one-hour threshold."
    )
    return CalibrationProposal(
        constraint_key="run_threshold_pace",
        was=float(current),
        now=round(derived, 1),
        evidence_text=evidence,
    ), evidence


def analyse(
    session: Session,
    *,
    record: PostRaceFile,
    plan: Plan,
    user: User,
    settings: Settings,
) -> PostRaceAnalysis:
    """Compare the file against the plan **as it was at race time**."""
    if plan.user_id != user.id:
        raise NotFound("Plan not found.")
    if plan.solved_at is None:
        raise Conflict("This plan was never solved, so there is nothing to compare.")

    existing = session.scalar(
        select(PostRaceAnalysis).where(
            PostRaceAnalysis.plan_id == plan.id,
            PostRaceAnalysis.race_file_id == record.id,
        )
    )
    if existing is not None:
        return existing

    parsed = load_parsed(session, record=record, settings=settings)

    analysis = PostRaceAnalysis(
        plan_id=plan.id,
        # Stored, not joined. A later re-solve must not change what this
        # analysis was measured against.
        plan_version=plan.version,
        race_file_id=record.id,
        generated_at=datetime.now(UTC),
    )
    session.add(analysis)
    session.flush()

    _build_compare_rows(session, analysis=analysis, plan=plan, parsed=parsed)
    proposal, reason = _propose_run_threshold(session, user=user, parsed=parsed)
    if proposal is not None:
        session.add(
            AnalysisCalibration(
                analysis_id=analysis.id,
                constraint_key=proposal.constraint_key,
                was=proposal.was,
                now=proposal.now,
                evidence_text=proposal.evidence_text,
            )
        )
    _build_actions(session, analysis=analysis, plan=plan, parsed=parsed, reason=reason)

    record.status = RaceFileStatus.PROCESSED
    record.plan_id = plan.id
    session.flush()

    notification_service.notify(
        session,
        user=user,
        settings=settings,
        type_key=NotificationType.ANALYSIS,
        severity=NotificationSeverity.OK,
        title="Your race file is processed.",
        body=("Planned against actual, segment by segment, with what to change " "next time."),
        tag="ANALYSIS READY",
        race_id=plan.race_id,
        plan_id=plan.id,
        cta_label="See analysis",
        cta_href=f"/post-race/{analysis.id}",
    )

    logger.info(
        "postrace.analysed",
        extra={"analysis_id": str(analysis.id), "plan_version": plan.version},
    )
    return analysis


def _build_compare_rows(
    session: Session, *, analysis: PostRaceAnalysis, plan: Plan, parsed: ParsedRaceFile
) -> None:
    """Overall time first, then per leg where the file supports it.

    A single-sport file (a run watch that recorded only the run) is compared
    on the leg it covers rather than being rejected: partial evidence is still
    evidence, and it is labelled as partial.
    """
    splits = {
        row.leg: row
        for row in session.scalars(select(PlanSplit).where(PlanSplit.plan_id == plan.id))
    }
    planned_total = float(plan.projected_minutes or 0.0)
    actual_total = parsed.total_elapsed_s / 60.0

    ordinal = 0
    session.add(
        AnalysisCompareRow(
            analysis_id=analysis.id,
            ordinal=ordinal,
            name="Total time",
            planned=_clock(planned_total),
            actual=_clock(actual_total),
            delta=_clock(actual_total - planned_total),
            state=_state(planned_total, actual_total),
            why=("Faster than planned" if actual_total < planned_total else "Slower than planned"),
            drift_pct=(
                round((actual_total - planned_total) / planned_total * 100.0, 1)
                if planned_total > 0
                else None
            ),
        )
    )

    planned_distance_m = sum(float(row.distance) for row in splits.values()) * 1000.0
    if planned_distance_m > 0:
        ordinal += 1
        session.add(
            AnalysisCompareRow(
                analysis_id=analysis.id,
                ordinal=ordinal,
                name="Distance recorded",
                planned=f"{planned_distance_m / 1000:.1f} km",
                actual=f"{parsed.total_distance_m / 1000:.1f} km",
                delta=f"{(parsed.total_distance_m - planned_distance_m) / 1000:+.1f} km",
                state=_state(planned_distance_m, parsed.total_distance_m),
                why=(
                    "The file covers the whole race"
                    if abs(parsed.total_distance_m - planned_distance_m) / planned_distance_m <= _OK
                    else "The file covers part of the race, so this is partial evidence"
                ),
            )
        )

    if parsed.has_power:
        powers = [p.power_w for p in parsed.points if p.power_w is not None]
        bike = splits.get(Leg.BIKE)
        if powers and bike is not None:
            actual_power = mean(powers)
            planned_power = _numeric(bike.target_pace_or_power)
            if planned_power is not None:
                ordinal += 1
                session.add(
                    AnalysisCompareRow(
                        analysis_id=analysis.id,
                        ordinal=ordinal,
                        name="Bike power",
                        planned=f"{planned_power:.0f} w",
                        actual=f"{actual_power:.0f} w",
                        delta=f"{actual_power - planned_power:+.0f} w",
                        state=_state(planned_power, actual_power),
                        why=(
                            "Rode above the plan — the classic way to lose the run"
                            if actual_power > planned_power * (1 + _OK)
                            else "Held the planned power"
                        ),
                    )
                )
    session.flush()


def _numeric(text: str) -> float | None:
    try:
        return float(str(text).strip())
    except (TypeError, ValueError):
        return None


def _build_actions(
    session: Session,
    *,
    analysis: PostRaceAnalysis,
    plan: Plan,
    parsed: ParsedRaceFile,
    reason: str,
) -> None:
    """Ranked recommendations, each with the time it is worth.

    Derived from what the compare rows actually found, so an action never
    recommends fixing something the file shows the athlete did well.
    """
    rows = list(
        session.scalars(
            select(AnalysisCompareRow).where(AnalysisCompareRow.analysis_id == analysis.id)
        )
    )
    actions: list[tuple[float, str, str, str]] = []

    total_row = next((row for row in rows if row.name == "Total time"), None)
    if total_row is not None and total_row.drift_pct and total_row.drift_pct > 5.0:
        planned_total = float(plan.projected_minutes or 0.0)
        gain = planned_total * float(total_row.drift_pct) / 100.0
        actions.append(
            (
                round(gain, 1),
                "Close the gap to your planned pacing",
                (
                    f"You finished {total_row.delta} against the plan, "
                    f"{total_row.drift_pct:+.1f}%."
                ),
                (
                    "Re-solve with your calibrated numbers and hold the "
                    "opening leg at the target rather than above it."
                ),
            )
        )

    power_row = next((row for row in rows if row.name == "Bike power"), None)
    if power_row is not None and power_row.state in (CompareState.WARN, CompareState.BAD):
        actions.append(
            (
                8.0,
                "Ride the bike target, not your legs",
                f"Your average power was {power_row.delta} against plan.",
                (
                    "Set the target as a field on your head unit and treat the "
                    "first hour's ceiling as a hard limit."
                ),
            )
        )

    if not parsed.has_power:
        actions.append(
            (
                0.0,
                "Record power next time",
                "This file has no power channel, so the bike leg could not be compared.",
                "Pair your meter before the start and check it records in the warm-up.",
            )
        )

    if "No sustained effort" in reason:
        actions.append(
            (
                0.0,
                "Upload a steady effort to calibrate",
                reason,
                (
                    "A 20 to 90 minute steady run, raced or tested, gives the "
                    "calibration something it can trust."
                ),
            )
        )

    actions.sort(key=lambda item: -item[0])
    for rank, (gain, name, description, how_to) in enumerate(actions, start=1):
        session.add(
            AnalysisAction(
                analysis_id=analysis.id,
                rank=rank,
                projected_gain_minutes=gain,
                name=name,
                description=description,
                how_to=how_to,
            )
        )
    session.flush()


# ---------------------------------------------------------------------------
# Calibration write-back
# ---------------------------------------------------------------------------


def apply_calibration(
    session: Session, *, calibration: AnalysisCalibration, user: User
) -> AnalysisCalibration:
    """Write the derived value with ``source = 'measured'``.

    Routed through :func:`constraint_service.write_constraint`, which requires
    the actor to **be** the athlete — so a calibration cannot be applied on
    someone's behalf by a coach, an admin, or a background job.
    """
    if calibration.applied:
        raise Conflict("This calibration was already applied.")
    if calibration.dismissed_at is not None:
        raise Conflict("This calibration was dismissed.")

    analysis = session.get(PostRaceAnalysis, calibration.analysis_id)
    if analysis is None:  # pragma: no cover - FK CASCADE
        raise NotFound("Analysis not found.")
    plan = session.get(Plan, analysis.plan_id)
    if plan is None or plan.user_id != user.id:
        raise NotFound("Analysis not found.")

    constraint_service.write_constraint(
        session,
        athlete_id=user.id,
        actor=user,
        key=calibration.constraint_key,
        value=float(calibration.now),
        source=ConstraintSource.MEASURED,
        source_detail="post-race calibration",
        evidence_note=calibration.evidence_text,
        change_reason=f"calibrated from race file after plan v{analysis.plan_version}",
    )
    calibration.applied = True
    calibration.applied_at = datetime.now(UTC)
    session.flush()
    logger.info(
        "postrace.calibration_applied",
        extra={"calibration_id": str(calibration.id), "key": calibration.constraint_key},
    )
    return calibration


def dismiss_calibration(
    session: Session, *, calibration: AnalysisCalibration, user: User
) -> AnalysisCalibration:
    if calibration.applied:
        raise Conflict("This calibration was already applied.")
    analysis = session.get(PostRaceAnalysis, calibration.analysis_id)
    plan = session.get(Plan, analysis.plan_id) if analysis else None
    if plan is None or plan.user_id != user.id:
        raise NotFound("Analysis not found.")
    calibration.dismissed_at = datetime.now(UTC)
    session.flush()
    return calibration


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_analysis(session: Session, *, analysis_id: UUID, user: User) -> PostRaceAnalysis:
    analysis = session.get(PostRaceAnalysis, analysis_id)
    if analysis is None:
        raise NotFound("Analysis not found.")
    plan = session.get(Plan, analysis.plan_id)
    if plan is None or plan.user_id != user.id:
        raise NotFound("Analysis not found.")
    return analysis


def list_analyses(session: Session, *, user: User) -> list[PostRaceAnalysis]:
    return list(
        session.scalars(
            select(PostRaceAnalysis)
            .join(Plan, Plan.id == PostRaceAnalysis.plan_id)
            .where(Plan.user_id == user.id)
            .order_by(PostRaceAnalysis.generated_at.desc())
        )
    )


def get_file(session: Session, *, file_id: UUID, user: User) -> PostRaceFile:
    record = session.get(PostRaceFile, file_id)
    if record is None or record.user_id != user.id:
        raise NotFound("Race file not found.")
    return record


def plan_for_race(session: Session, *, race_id: UUID, user: User) -> Plan:
    """The version that was live at race time.

    ``ACTIVE`` if it still is; otherwise the newest solved version, because a
    plan superseded *after* the race is still the one that was raced.
    """
    from raceos.domain.enums import PlanStatus

    race = session.get(Race, race_id)
    if race is None or race.user_id != user.id:
        raise NotFound("Race not found.")
    plan = session.scalar(
        select(Plan)
        .where(
            Plan.race_id == race_id,
            Plan.status.in_((PlanStatus.ACTIVE, PlanStatus.PAST)),
            Plan.solved_at.is_not(None),
        )
        .order_by(Plan.version.desc())
        .limit(1)
    )
    if plan is None:
        raise InvalidInput("This race has no solved plan to compare against.")
    return plan
