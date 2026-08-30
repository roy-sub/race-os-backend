"""The app boots, health endpoints answer, and the error envelope is uniform."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from raceos.api.errors import ErrorCode, Forbidden, ForbiddenStructural, RateLimited
from raceos.api.main import API_PREFIX, create_app
from raceos.api.middleware import REQUEST_ID_HEADER
from raceos.config import AppEnv, Settings
from raceos.storage.base import InMemoryStorage, set_storage_backend


@pytest.fixture
def client() -> TestClient:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    set_storage_backend(InMemoryStorage(settings))
    app = create_app(settings)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    set_storage_backend(None)


def test_healthz_is_ok_and_touches_nothing(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_every_response_carries_a_request_id(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.headers[REQUEST_ID_HEADER].startswith("req_")


def test_inbound_request_id_is_honoured(client: TestClient) -> None:
    response = client.get("/healthz", headers={REQUEST_ID_HEADER: "req_from_frontend_1"})
    assert response.headers[REQUEST_ID_HEADER] == "req_from_frontend_1"


def test_inbound_request_id_is_sanitised(client: TestClient) -> None:
    """A caller-controlled value ends up in logs; it must not carry injection."""
    response = client.get(
        "/healthz", headers={REQUEST_ID_HEADER: 'evil"\n{"level":"ERROR"} ' + "x" * 200}
    )
    returned = response.headers[REQUEST_ID_HEADER]
    assert '"' not in returned
    assert "\n" not in returned
    assert " " not in returned
    assert len(returned) <= 64


def test_security_headers_are_present(client: TestClient) -> None:
    headers = client.get("/healthz").headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_hsts_only_in_production() -> None:
    dev = Settings(_env_file=None)  # type: ignore[call-arg]
    set_storage_backend(InMemoryStorage(dev))
    with TestClient(create_app(dev)) as c:
        assert "Strict-Transport-Security" not in c.get("/healthz").headers
    set_storage_backend(None)


def test_unknown_route_uses_the_error_envelope(client: TestClient) -> None:
    response = client.get("/no-such-route")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == ErrorCode.NOT_FOUND.value
    assert body["error"]["request_id"].startswith("req_")


def test_raceos_errors_map_to_their_documented_status(client: TestClient) -> None:
    """One code, one status, wherever it is raised."""
    app = client.app

    @app.get("/_test/forbidden")
    async def _forbidden() -> None:
        raise Forbidden("nope")

    @app.get("/_test/structural")
    async def _structural() -> None:
        raise ForbiddenStructural("a coach may never write an athlete's constraints")

    @app.get("/_test/limited")
    async def _limited() -> None:
        raise RateLimited("slow down", retry_after_seconds=30)

    assert client.get("/_test/forbidden").status_code == 403

    structural = client.get("/_test/structural")
    assert structural.status_code == 403
    assert structural.json()["error"]["code"] == ErrorCode.FORBIDDEN_STRUCTURAL.value

    limited = client.get("/_test/limited")
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "30"


def test_unhandled_exception_never_leaks_its_message(client: TestClient) -> None:
    app = client.app

    @app.get("/_test/boom")
    async def _boom() -> None:
        raise RuntimeError("connection string postgres://u:sekrit@host/db failed")

    response = client.get("/_test/boom")
    assert response.status_code == 500
    body = response.text
    assert "sekrit" not in body
    assert "RuntimeError" not in body
    assert response.json()["error"]["request_id"].startswith("req_")


def test_openapi_is_served_and_names_the_configured_server(client: TestClient) -> None:
    schema = client.get(f"{API_PREFIX}/openapi.json").json()
    assert schema["info"]["title"] == "RaceOS API"
    assert schema["servers"][0]["url"] == "http://localhost:8000"


def test_docs_are_disabled_in_production() -> None:
    """Interactive docs are a development affordance, not a public surface."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env=AppEnv.PRODUCTION,
        app_base_url="https://app.example",
        api_base_url="https://api.example",
        cors_allowed_origins="https://app.example",
        database_url=SecretStr("postgresql+psycopg://u:ppppppppp@db.ref.supabase.co:5432/postgres"),
        supabase_url="https://ref.supabase.co",
        supabase_secret_key=SecretStr("sb_secret_aaaabbbb"),
        jwt_private_key=SecretStr(
            "-----BEGIN PRIVATE KEY-----\\nAAAA\\n-----END PRIVATE KEY-----\\n"
        ),
        jwt_public_key="-----BEGIN PUBLIC KEY-----\\nAAAA\\n-----END PUBLIC KEY-----\\n",
        session_cookie_secret=SecretStr("s" * 40),
        internal_job_secret=SecretStr("j" * 40),
        terrain_pmtiles_base_url="https://terrain.example",
        email_from_address="noreply@example.com",
        stripe_secret_key=SecretStr("sk_test_aaaabbbb"),
        stripe_publishable_key="pk_test_aaaabbbb",
        stripe_webhook_secret=SecretStr("whsec_aaaabbbb"),
        stripe_price_id_race_plan="price_aaaa",
        stripe_price_id_season_pass="price_bbbb",
        stripe_price_id_coach="price_cccc",
    )
    assert create_app(settings).docs_url is None


def test_production_settings_refuse_to_boot_when_incomplete() -> None:
    """The whole point of the production gate: fail at boot, name the gaps."""
    with pytest.raises(ValueError, match="requires these variables") as excinfo:
        Settings(_env_file=None, app_env=AppEnv.PRODUCTION)  # type: ignore[call-arg]
    message = str(excinfo.value)
    for expected in ("JWT_PRIVATE_KEY", "SESSION_COOKIE_SECRET", "INTERNAL_JOB_SECRET"):
        assert expected in message


def test_startup_log_contains_no_secret(client: TestClient, capsys) -> None:
    """The resolved config is logged at boot; it must be safe to emit."""
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        internal_job_secret=SecretStr("supersecretjobvalue1234567890"),
    )
    set_storage_backend(InMemoryStorage(settings))
    with TestClient(create_app(settings)):
        pass
    set_storage_backend(None)
    captured = capsys.readouterr().out
    assert "supersecretjobvalue1234567890" not in captured
    startup = [
        json.loads(line)
        for line in captured.splitlines()
        if line.startswith("{") and '"event": "startup"' in line
    ]
    assert startup, "no startup log line was emitted"
    assert startup[-1]["config"]["internal_job_secret"] == "<set>"
