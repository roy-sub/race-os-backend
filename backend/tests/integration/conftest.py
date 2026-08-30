"""API-level fixtures: a running app wired to the migrated test database.

A real RS256 keypair is generated **per test session, in memory**. It is never
written to disk and never committed — the suite has to exercise real signature
verification, and a fixed test key in the repository would be a credential in
the repository regardless of what it protects.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from raceos.api.main import create_app
from raceos.config import Settings
from raceos.db import session as session_module


@pytest.fixture(scope="session")
def rsa_keypair() -> tuple[str, str]:
    """A throwaway 2048-bit pair, generated fresh for this run."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


@pytest.fixture
def api_settings(rsa_keypair: tuple[str, str], database_url: str) -> Settings:
    """Development settings with real keys and the test database.

    argon2 is turned down to its floor: the suite hashes hundreds of
    passwords, and production parameters would add minutes for no coverage.
    The production values are asserted separately, from the settings defaults.
    """
    private_pem, public_pem = rsa_keypair
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=SecretStr(database_url),
        jwt_private_key=SecretStr(private_pem),
        jwt_public_key=public_pem,
        session_cookie_secret=SecretStr("t" * 48),
        internal_job_secret=SecretStr("j" * 48),
        argon2_time_cost=1,
        argon2_memory_cost_kib=8192,
        argon2_parallelism=1,
        rate_limit_enabled=False,
        # A random per-run webhook secret. The webhook route's whole
        # security model is the signature, so the suite has to verify real
        # signatures rather than skip the check.
        stripe_webhook_secret=SecretStr(secrets.token_urlsafe(32)),
    )


@pytest.fixture
def api(migrated_engine: Engine, api_settings: Settings) -> Iterator[TestClient]:
    """The real app, on the real schema.

    The engine is swapped rather than mocked, so requests go through the same
    session factory production uses. Tables are truncated between tests
    instead of rolling back, because the API commits and a rolled-back
    transaction would hide read-your-own-writes bugs.
    """
    session_module.reset_engine()
    session_module._engine = migrated_engine
    session_module._session_factory = None

    app = create_app(api_settings)
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client

    _truncate_all(migrated_engine)
    session_module.reset_engine()


def _truncate_all(engine: Engine) -> None:
    from sqlalchemy import text

    from raceos.db.models import Base

    names = ", ".join(f'"{table}"' for table in Base.metadata.tables if table != "spatial_ref_sys")
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def api_db(migrated_engine: Engine) -> Iterator[Session]:
    """A committing session for arranging fixtures the API will then read."""
    factory = sessionmaker(bind=migrated_engine, expire_on_commit=False)
    with factory() as session:
        yield session


@pytest.fixture
def signed_up(api: TestClient) -> dict[str, object]:
    """A registered, signed-in athlete. Returns their tokens and identity."""
    response = api.post(
        "/api/v1/auth/signup",
        json={
            "email": "elena.marsh@example.com",
            "password": "correct-horse-battery",
            "name": "Elena Marsh",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return {
        "user": body["user"],
        "access_token": body["access_token"],
        "headers": {"Authorization": f"Bearer {body['access_token']}"},
    }


@pytest.fixture
def paywall(api_settings: Settings) -> Iterator[None]:
    """A fresh in-memory payment gateway for each test.

    Real two-phase capture against a working gateway rather than a mock: an
    authorization can be captured once, or voided once, never both, so a test
    that passes here is testing the caller's sequencing rather than a stub's
    indulgence. No credential is involved, which is what lets the billing
    paths run fully offline.
    """
    from raceos.payments import InMemoryPaymentGateway, set_payment_gateway

    gateway = InMemoryPaymentGateway(api_settings)
    set_payment_gateway(gateway)
    yield
    set_payment_gateway(None)


def buy_plan(api: TestClient, headers: dict[str, str], plan_id: str) -> dict[str, object]:
    """Place the hold that a solve then captures.

    Helper rather than a fixture because most tests need it inline, right
    after the draft is created and before the solve they are actually about.
    """
    response = api.post(
        "/api/v1/checkout/authorize",
        headers=headers,
        json={"plan_id": plan_id, "currency": "GBP"},
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body
