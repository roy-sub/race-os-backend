"""The findings from the security review, each with the case that caught it.

Every test here failed before the change it guards. They are grouped by what
an attacker would have got, because that is what decides whether a regression
matters: a header that quietly stops being sent is not the same class of
problem as a credential that starts being written down again.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.api.main import create_app
from raceos.config import AppEnv, Settings
from raceos.db.models import RateLimitCounter
from raceos.ingest import gpx_course, racefile
from raceos.ingest.xml_guard import XmlNotAllowedError, ensure_no_doctype

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Reflected input
# ---------------------------------------------------------------------------


def test_a_validation_error_does_not_echo_what_was_sent(api: TestClient) -> None:
    """Pydantic puts the offending value in `input`, and we used to forward it.

    A mistyped password came back in the response body verbatim — into
    devtools, into any client-side error reporter, into a proxy log and into
    every support screenshot of "it says my password is wrong".
    """
    secret = "hunter2-this-should-never-come-back"
    response = api.post(
        "/api/v1/auth/login",
        json={"email": "someone@example.com", "password": {"nested": secret}},
    )
    assert response.status_code == 422
    assert secret not in response.text

    # Still diagnosable: the caller learns which field and why.
    body = response.json()["error"]
    assert body["field"] == "password"
    assert body["details"]["errors"][0]["msg"]
    assert "input" not in body["details"]["errors"][0]


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Cross-Origin-Resource-Policy", "same-site"),
        ("Cross-Origin-Opener-Policy", "same-origin"),
    ],
)
def test_every_response_carries_its_security_headers(
    api: TestClient, header: str, expected: str
) -> None:
    response = api.get("/api/v1/courses")
    assert response.headers.get(header) == expected


def test_the_api_forbids_being_framed_and_loading_anything(api: TestClient) -> None:
    csp = api.get("/api/v1/courses").headers["Content-Security-Policy"]
    for directive in ("default-src 'none'", "frame-ancestors 'none'", "form-action 'none'"):
        assert directive in csp


# ---------------------------------------------------------------------------
# What production exposes
# ---------------------------------------------------------------------------


def test_the_schema_and_docs_are_not_served_in_production(api_settings: Settings) -> None:
    """A precise map of every route, its shape and its error codes only helps
    someone probing them. The frontend builds against its own committed copy."""
    # The enum, not the string: `is_production` compares identity.
    production = api_settings.model_copy(update={"app_env": AppEnv.PRODUCTION})
    app = create_app(production)
    assert app.docs_url is None
    assert app.openapi_url is None

    # ...and it is still served outside production, where it is the contract.
    assert create_app(api_settings).openapi_url is not None


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_a_failed_request_is_still_counted(api_db, api_settings: Settings) -> None:
    """The counter used to live in the request's transaction and die with it.

    Every request that *raised* — a rejected login above all — was therefore
    never counted, so the limiter throttled successful traffic and let
    brute-force through unmetered. That is the limiter inverted.
    """
    from raceos.services import rate_limit

    limited = api_settings.model_copy(update={"rate_limit_enabled": True})
    subject = "ip:198.51.100.7"

    rate_limit.check_rate_limit(
        api_db, subject=subject, bucket="test.failed", limit=10, settings=limited
    )
    # Discard everything the "request" did, exactly as a raised error would.
    api_db.rollback()

    counter = api_db.scalar(
        select(RateLimitCounter).where(
            RateLimitCounter.subject == subject, RateLimitCounter.bucket == "test.failed"
        )
    )
    assert counter is not None, "the count did not survive the request being rolled back"
    assert counter.count == 1


def test_the_default_limit_applies_to_routes_with_no_limit_of_their_own(
    api_db, api_settings: Settings
) -> None:
    """`RATE_LIMIT_DEFAULT_PER_MINUTE` was configuration nothing read."""
    limited = api_settings.model_copy(
        update={"rate_limit_enabled": True, "rate_limit_default_per_minute": 3}
    )
    with TestClient(create_app(limited)) as client:
        statuses = [client.get("/api/v1/courses").status_code for _ in range(6)]

    assert 429 in statuses, f"never throttled: {statuses}"
    assert statuses[0] == 200, "and it did not throttle from the first request"


def test_health_checks_are_never_throttled(api_settings: Settings) -> None:
    """The platform polls these every few seconds; a 429 would look like an
    outage and get healthy instances restarted."""
    limited = api_settings.model_copy(
        update={"rate_limit_enabled": True, "rate_limit_default_per_minute": 2}
    )
    with TestClient(create_app(limited)) as client:
        assert {client.get("/healthz").status_code for _ in range(8)} == {200}


# ---------------------------------------------------------------------------
# Cross-site session denial of service
# ---------------------------------------------------------------------------


def test_refresh_requires_a_header_a_cross_origin_page_cannot_send(
    api: TestClient, signed_up: dict
) -> None:
    """`/auth/refresh` is authenticated by the cookie alone, and that cookie is
    `SameSite=None` in production. A POST with only safelisted headers is a
    CORS simple request, so any page could make it with the victim's cookie:
    it could not read the reply, but the rotated `Set-Cookie` still landed,
    and the victim's next real refresh tripped reuse detection and revoked
    every session. Any website could sign any athlete out, repeatedly.

    A header off the CORS safelist forces a preflight, and the preflight is
    answered against the origin allowlist — so the browser never sends it.
    """
    forged = api.post("/api/v1/auth/refresh")
    assert forged.status_code == 401

    # ...and the answer is indistinguishable from having no session at all, so
    # a cross-origin caller cannot use it to probe whether one exists.
    assert forged.json()["error"]["message"] == "No session to refresh."

    # Our own client, sending the header, still rotates normally.
    ours = api.post("/api/v1/auth/refresh", headers={"X-RaceOS-Client": "web"})
    assert ours.status_code == 200
    assert ours.json()["access_token"]


def test_the_client_header_is_allowed_through_cors(api: TestClient) -> None:
    """The guard is worthless if the preflight refuses our own client too."""
    preflight = api.options(
        "/api/v1/auth/refresh",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "X-RaceOS-Client",
        },
    )
    allowed = preflight.headers.get("access-control-allow-headers", "").lower()
    assert "x-raceos-client" in allowed


# ---------------------------------------------------------------------------
# Untrusted XML
# ---------------------------------------------------------------------------

#: A few hundred bytes that expand to gigabytes during parsing. The upload
#: size cap does not help: being small is the whole point of the attack.
BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE gpx [
  <!ENTITY a "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">
  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
  <!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
  <!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">
]>
<gpx><trk><trkseg><trkpt lat="1" lon="2"><name>&d;</name></trkpt></trkseg></trk></gpx>"""


def test_an_entity_expansion_bomb_is_refused_by_the_post_race_importer() -> None:
    with pytest.raises(racefile.RaceFileError, match="document type declaration"):
        racefile.parse_gpx(BILLION_LAUGHS)


def test_an_entity_expansion_bomb_is_refused_by_the_course_importer() -> None:
    with pytest.raises(gpx_course.GpxError, match="document type declaration"):
        gpx_course.parse_gpx(BILLION_LAUGHS, filename="route.gpx")


def test_an_ordinary_gpx_file_still_imports() -> None:
    """The guard has to be narrow enough to be free: no exporter emits a DTD."""
    clean = (
        b'<?xml version="1.0"?><gpx><trk><trkseg>'
        b'<trkpt lat="51.500" lon="-0.100"><ele>10</ele></trkpt>'
        b'<trkpt lat="51.510" lon="-0.110"><ele>12</ele></trkpt>'
        b"</trkseg></trk></gpx>"
    )
    assert len(gpx_course.parse_gpx(clean, filename="ok.gpx")) == 2


@pytest.mark.parametrize("marker", [b"<!DOCTYPE", b"<!doctype", b"<!DoCtYpE", b"<!ENTITY"])
def test_the_guard_is_not_fooled_by_case(marker: bytes) -> None:
    """XML keywords are case-sensitive; an attacker is not obliged to be tidy."""
    with pytest.raises(XmlNotAllowedError):
        ensure_no_doctype(b'<?xml version="1.0"?>' + marker + b" gpx []><gpx/>", filename="f.gpx")
