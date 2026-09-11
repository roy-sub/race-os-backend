"""Full-access accounts: the developer and demo path.

The grant exists so an operator can hand one address every entitlement without
a payment. Three properties matter and each has a test here:

1. It really does grant **everything**, asserted across the whole rule table
   rather than a sampled action, so a rule added later cannot quietly fall
   outside it.
2. It is **configuration**, not a column and not a hardcoded address — the same
   account on the same database is refused by an app configured without it,
   which is what makes the grant withdrawable in one deploy.
3. It creates **no purchase and no invoice**. A full-access account must never
   appear in revenue, or every figure on the Ops page is wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from raceos.db.models import Invoice, Purchase
from raceos.ingest.bundle_loader import load_bundle_file

pytestmark = pytest.mark.integration

BUNDLE_DIR = Path(__file__).resolve().parents[3] / "pipelines" / "course-ingest" / "out" / "bundles"
TRAMUNTANA = BUNDLE_DIR / "tramuntana-full.bundle.json"

needs_bundle = pytest.mark.skipif(
    not TRAMUNTANA.is_file(), reason="generated bundles are git-ignored build artefacts"
)


def test_every_entitlement_is_granted(
    full_access_api: TestClient, full_access_headers: dict
) -> None:
    rows = full_access_api.get("/api/v1/entitlements", headers=full_access_headers).json()
    assert rows, "the entitlement matrix should never come back empty"
    refused = [row["action"] for row in rows if not row["allowed"]]
    assert refused == [], f"full access refused {refused}"
    assert all(row["reason"] == "" for row in rows)


def test_the_grant_is_case_insensitive(
    full_access_api: TestClient, full_access_headers: dict
) -> None:
    """`users.email` is CITEXT, and the fixture signs up in a different case.

    A grant that depended on how someone typed their address at signup would
    be a grant that silently stopped working.
    """
    rows = full_access_api.get("/api/v1/entitlements", headers=full_access_headers).json()
    assert all(row["allowed"] for row in rows)


def test_the_same_account_is_refused_without_the_configuration(api: TestClient) -> None:
    """Withdrawable in one deploy, because it lives nowhere but configuration."""
    from tests.integration.conftest import FULL_ACCESS_EMAIL

    signup = api.post(
        "/api/v1/auth/signup",
        json={"email": FULL_ACCESS_EMAIL, "password": "correct-horse-battery", "name": "Dev"},
    )
    assert signup.status_code == 201
    headers = {"Authorization": f"Bearer {signup.json()['access_token']}"}

    rows = api.get("/api/v1/entitlements", headers=headers).json()
    solve = next(row for row in rows if row["action"] == "solve_plan")
    assert solve["allowed"] is False
    assert solve["purchasable_per_race"] is True


def test_full_access_creates_no_purchase_and_no_invoice(
    full_access_api: TestClient, full_access_headers: dict, api_db
) -> None:
    """A grant is not a sale. Nothing about it may reach revenue."""
    full_access_api.get("/api/v1/entitlements", headers=full_access_headers)
    full_access_api.get("/api/v1/invoices", headers=full_access_headers)

    assert api_db.scalar(select(func.count()).select_from(Purchase)) == 0
    assert api_db.scalar(select(func.count()).select_from(Invoice)) == 0
    assert full_access_api.get("/api/v1/invoices", headers=full_access_headers).json() == []


def test_full_access_does_not_pretend_to_be_a_tier(
    full_access_api: TestClient, full_access_headers: dict
) -> None:
    """The account keeps whatever tier it has.

    Nothing downstream — the Ops page's tier counts, the pricing screen's
    "current plan" — should mistake a granted account for a paying one.
    """
    me = full_access_api.get("/api/v1/auth/me", headers=full_access_headers).json()
    assert me["tier"] == "free"


@needs_bundle
def test_the_course_map_opens_without_a_payment(
    full_access_api: TestClient, full_access_headers: dict, migrated_engine
) -> None:
    """The thing the grant exists for: everything, without paying for it."""
    from sqlalchemy.orm import sessionmaker

    with sessionmaker(bind=migrated_engine)() as session:
        load_bundle_file(session, TRAMUNTANA)
        session.commit()

    body = full_access_api.get(
        "/api/v1/courses/tramuntana-full/recon", headers=full_access_headers
    ).json()

    assert body["access"]["map_unlocked"] is True
    assert body["access"]["map_locked_reason"] is None
    assert any(leg["coordinates"] for leg in body["legs"])
    assert body["barriers"]
    assert body["aid_stations"]
    assert body["segments"]
