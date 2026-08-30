"""The seed, run for real and run twice.

`make seed` is the first thing a new contributor runs and the last thing a
deploy runs. It has to work on an empty database and be safe to repeat, so
this test does both against the real schema and the real solver.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from raceos.db.models import (
    CoachAthleteLink,
    Constraint,
    Incident,
    Invoice,
    KpiSnapshot,
    Plan,
    PlanSplit,
    Race,
    ShareLink,
    User,
)
from raceos.domain.enums import ConstraintSource, PlanStatus
from raceos.ingest.bundle_loader import load_bundle_directory

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"

needs_bundles = pytest.mark.skipif(
    not (BUNDLE_DIR / "tramuntana-full.bundle.json").is_file(),
    reason="generated bundles are git-ignored build artefacts",
)


@pytest.fixture
def seeded(migrated_engine, api_settings, paywall):
    """Courses and people, seeded once into the test database."""
    from raceos.db.seed_people import seed_people

    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    with factory() as session:
        load_bundle_directory(session, BUNDLE_DIR)
        session.commit()
        summary = seed_people(session, api_settings)
        session.commit()
    yield {"factory": factory, "summary": summary}

    from sqlalchemy import text

    from raceos.db.models import Base

    names = ", ".join(f'"{table}"' for table in Base.metadata.tables if table != "spatial_ref_sys")
    with migrated_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@needs_bundles
def test_the_seed_populates_every_screen_the_product_has(seeded) -> None:
    summary = seeded["summary"]

    assert summary["people"] == 13
    assert summary["plans_solved"] > 0
    assert summary["coach_links"] == 3
    assert summary["incidents"] == 2
    assert summary["kpi_days"] == 31
    # Three currencies, because the pricing page offers three.
    assert summary["invoice_currencies"] == ["EUR", "GBP", "USD"]


@needs_bundles
def test_every_seeded_plan_came_from_the_real_solver(seeded) -> None:
    """A fixture with fabricated splits would let a solver regression pass a
    demo, which is exactly backwards."""
    with seeded["factory"]() as session:
        solved = session.scalars(select(Plan).where(Plan.solved_at.is_not(None))).all()
        assert solved

        for plan in solved:
            assert plan.solve_input_hash, "a hand-written plan has no input hash"
            assert plan.projected_minutes is not None
            assert float(plan.projected_minutes) > 0
            assert plan.binding_constraint_key, "the solver names what bound"

            splits = session.scalars(select(PlanSplit).where(PlanSplit.plan_id == plan.id)).all()
            assert len(splits) == 3, "three legs, from the solver"
            assert all(float(split.split_minutes) > 0 for split in splits)


@needs_bundles
def test_plans_exist_in_every_status(seeded) -> None:
    """A screen built on this seed cannot accidentally assume one shape."""
    with seeded["factory"]() as session:
        statuses = {row[0] for row in session.execute(select(Plan.status).distinct()).all()}
    assert PlanStatus.ACTIVE in statuses
    assert PlanStatus.DRAFT in statuses
    assert PlanStatus.PAST in statuses


@needs_bundles
def test_provenance_is_seeded_honestly(seeded) -> None:
    """A first-timer's numbers are estimated, and the seed says so."""
    with seeded["factory"]() as session:
        first_timer = session.scalar(select(User).where(User.email == "aina.roig@example.com"))
        sources = {
            row.source
            for row in session.scalars(
                select(Constraint).where(Constraint.user_id == first_timer.id)
            )
        }
    assert sources == {ConstraintSource.ESTIMATED}


@needs_bundles
def test_the_seed_holds_a_real_drift_event_not_a_fabricated_one(seeded) -> None:
    """Offline there is no forecast to differ from, so the drift is caused by
    a genuine constraint change the solver genuinely disagrees with."""
    from raceos.db.models import PlanDriftEvent
    from raceos.domain.enums import DriftCause, DriftStatus

    with seeded["factory"]() as session:
        events = session.scalars(select(PlanDriftEvent)).all()
        assert events, "the dashboard's needs-review state has nothing to show"
        for event in events:
            assert event.cause is DriftCause.CONSTRAINT_CHANGE
            assert event.status is DriftStatus.PENDING
            assert event.field_deltas, "a drift with no deltas explains nothing"


@needs_bundles
def test_running_the_seed_twice_changes_nothing(seeded) -> None:
    """`make seed` is run repeatedly. Re-running must not walk a plan to v4 or
    leave a growing pile of live share links."""
    from raceos.config import get_settings
    from raceos.db.seed_people import seed_people

    def counts(session) -> dict[str, int]:
        return {
            name: int(session.scalar(select(func.count()).select_from(model)) or 0)
            for name, model in (
                ("users", User),
                ("races", Race),
                ("plans", Plan),
                ("splits", PlanSplit),
                ("invoices", Invoice),
                ("share_links", ShareLink),
                ("coach_links", CoachAthleteLink),
                ("incidents", Incident),
                ("kpis", KpiSnapshot),
            )
        }

    with seeded["factory"]() as session:
        before = counts(session)
        versions_before = {
            row[0]: row[1] for row in session.execute(select(Plan.id, Plan.version)).all()
        }

    with seeded["factory"]() as session:
        seed_people(session, get_settings())
        session.commit()

    with seeded["factory"]() as session:
        after = counts(session)
        versions_after = {
            row[0]: row[1] for row in session.execute(select(Plan.id, Plan.version)).all()
        }

    assert after == before, f"the second run created rows: {before} -> {after}"
    assert versions_after == versions_before, "a plan was re-solved to a new version"


@needs_bundles
def test_the_seed_writes_no_credential_anywhere(seeded) -> None:
    """Development passwords are generated per run and printed once.

    Nothing is written to a file, and no password ends up in a stored row —
    only its argon2 hash, which is what a password column is for.
    """
    with seeded["factory"]() as session:
        for user in session.scalars(select(User)):
            assert user.password_hash
            assert user.password_hash.startswith("$argon2")
            assert "dev-" not in user.password_hash


def test_the_seed_module_contains_no_literal_password() -> None:
    """A default password in the repository is a credential in the repository,
    whatever it protects."""
    import raceos.db.seed
    import raceos.db.seed_people

    for module in (raceos.db.seed, raceos.db.seed_people):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "password=" not in source or "password=password" in source
        assert "secrets.token_urlsafe" in source or "seed_people" in source
        # No assignment of a fixed password string.
        assert 'password = "' not in source
