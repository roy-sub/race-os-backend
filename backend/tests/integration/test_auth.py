"""Authentication, including the failure modes that are security requirements.

Build Spec Part 19.3 makes several of these mandatory: expired tokens, login
lockout counting correctly, password reset invalidating other sessions, and
forgot-password not leaking account existence. Each is here.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from raceos.config import Settings
from raceos.db.models import EmailMessage, PasswordResetToken, User
from raceos.services import security

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Signup
# ---------------------------------------------------------------------------


def test_signup_creates_a_verified_account_in_v1(api: TestClient) -> None:
    """`REQUIRE_EMAIL_VERIFICATION=false`, so accounts start verified.

    Gating on verification while email delivery is a no-op would lock every
    user out of the product on day one.
    """
    response = api.post(
        "/api/v1/auth/signup",
        json={"email": "new@example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 201
    assert response.json()["user"]["email_verified_at"] is not None


def test_signup_still_renders_the_verification_email(api: TestClient, api_db, signed_up) -> None:
    """The flow is built and exercised even though nothing is delivered.

    That is what makes flipping the flag a config change rather than a code
    change.
    """
    message = api_db.scalar(
        select(EmailMessage).where(EmailMessage.template_key == "auth.verify_email")
    )
    assert message is not None
    assert message.delivered is False, "EMAIL_ENABLED is false in V1"
    assert message.delivery_error == "EMAIL_ENABLED=false"
    assert "verify-email" in message.body_text


def test_signup_sets_the_refresh_cookie_and_not_a_body_field(api: TestClient) -> None:
    response = api.post(
        "/api/v1/auth/signup",
        json={"email": "cookie@example.com", "password": "correct-horse-battery"},
    )
    assert "raceos_refresh" in response.cookies
    assert (
        "refresh_token" not in response.json()
    ), "a body-borne refresh token is readable by any script on the page"


def test_signup_rejects_a_short_password(api: TestClient) -> None:
    response = api.post(
        "/api/v1/auth/signup", json={"email": "short@example.com", "password": "abc"}
    )
    assert response.status_code == 422


def test_signup_rejects_a_duplicate_address(api: TestClient, signed_up) -> None:
    response = api.post(
        "/api/v1/auth/signup",
        json={"email": "elena.marsh@example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["field"] == "email"


def test_signup_seeds_the_notification_preference_matrix(api: TestClient, api_db) -> None:
    from raceos.db.models import NotificationPreference

    api.post(
        "/api/v1/auth/signup",
        json={"email": "prefs@example.com", "password": "correct-horse-battery"},
    )
    rows = list(api_db.scalars(select(NotificationPreference)))
    assert len(rows) == 6, "one row per notification type"
    digest = next(r for r in rows if r.type_key.value == "digest")
    assert (digest.channel_email, digest.channel_push, digest.channel_inapp) == (
        False,
        False,
        False,
    )


# ---------------------------------------------------------------------------
# Login and lockout
# ---------------------------------------------------------------------------


def test_login_succeeds_with_the_right_password(api: TestClient, signed_up) -> None:
    response = api.post(
        "/api/v1/auth/login",
        json={"email": "elena.marsh@example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 200
    assert response.json()["access_token"]


def test_login_failure_states_attempts_remaining_accurately(
    api: TestClient, signed_up, api_settings: Settings
) -> None:
    """The frontend renders this number, so it has to be right.

    Part 8.5: "The failure copy states attempts remaining and must be
    accurate."
    """
    for attempt in range(1, api_settings.login_max_attempts):
        response = api.post(
            "/api/v1/auth/login",
            json={"email": "elena.marsh@example.com", "password": "wrong-password"},
        )
        assert response.status_code == 401
        remaining = api_settings.login_max_attempts - attempt
        assert response.json()["error"]["details"]["attempts_remaining"] == remaining
        assert str(remaining) in response.json()["error"]["message"]


def test_account_locks_after_the_configured_attempts(
    api: TestClient, signed_up, api_settings: Settings
) -> None:
    for _ in range(api_settings.login_max_attempts):
        api.post(
            "/api/v1/auth/login",
            json={"email": "elena.marsh@example.com", "password": "wrong-password"},
        )

    # Even the *correct* password is refused while locked.
    response = api.post(
        "/api/v1/auth/login",
        json={"email": "elena.marsh@example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 403
    assert "Too many failed attempts" in response.json()["error"]["message"]


def test_a_successful_login_resets_the_failure_counter(api: TestClient, signed_up, api_db) -> None:
    api.post(
        "/api/v1/auth/login",
        json={"email": "elena.marsh@example.com", "password": "wrong-password"},
    )
    api.post(
        "/api/v1/auth/login",
        json={"email": "elena.marsh@example.com", "password": "correct-horse-battery"},
    )
    user = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    api_db.refresh(user)
    assert user.failed_login_count == 0


def test_login_with_an_unknown_address_is_indistinguishable(api: TestClient) -> None:
    """Same status and same message as a wrong password."""
    response = api.post(
        "/api/v1/auth/login",
        json={"email": "nobody@example.com", "password": "correct-horse-battery"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["message"].startswith("Email or password is incorrect")
    assert "details" not in response.json()["error"], "no attempts counter for a non-account"


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_me_requires_a_token(api: TestClient) -> None:
    assert api.get("/api/v1/auth/me").status_code == 401


def test_me_rejects_a_garbage_token(api: TestClient) -> None:
    response = api.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


def test_a_refresh_token_is_not_accepted_as_an_access_token(api: TestClient, signed_up) -> None:
    """Without the `typ` check the 15-minute access window is decorative."""
    refresh = api.cookies.get("raceos_refresh")
    assert refresh
    response = api.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {refresh}"})
    assert response.status_code == 401


def test_refresh_rotates_the_token(api: TestClient, signed_up) -> None:
    first = api.cookies.get("raceos_refresh")
    response = api.post("/api/v1/auth/refresh")
    assert response.status_code == 200
    assert api.cookies.get("raceos_refresh") != first


def test_reusing_a_rotated_refresh_token_revokes_the_whole_family(
    api: TestClient, signed_up
) -> None:
    """Reuse is the signal that a token was captured.

    The legitimate holder is signed out too, which is the correct trade
    against an attacker holding a valid refresh token.
    """
    stolen = api.cookies.get("raceos_refresh")
    api.post("/api/v1/auth/refresh")  # rotates; `stolen` is now spent

    api.cookies.set("raceos_refresh", stolen)
    replay = api.post("/api/v1/auth/refresh")
    assert replay.status_code == 401

    # And the session that legitimately rotated is dead too.
    assert api.post("/api/v1/auth/refresh").status_code == 401


def test_logout_revokes_the_session(api: TestClient, signed_up) -> None:
    assert api.post("/api/v1/auth/logout", headers=signed_up["headers"]).status_code == 204
    assert api.post("/api/v1/auth/refresh").status_code == 401


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


def test_forgot_password_never_leaks_account_existence(api: TestClient, signed_up) -> None:
    """**Hard requirement.** Identical status and body, known or not."""
    known = api.post("/api/v1/auth/forgot-password", json={"email": "elena.marsh@example.com"})
    unknown = api.post("/api/v1/auth/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()


def test_the_reset_link_is_never_in_the_public_response(api: TestClient, signed_up, api_db) -> None:
    """With email disabled the link is written to the database and the log.

    Returning it here would turn "forgot password" into an account-takeover
    primitive — anyone who can name an address could seize the account.
    """
    response = api.post("/api/v1/auth/forgot-password", json={"email": "elena.marsh@example.com"})
    assert "token" not in response.text
    assert "reset-password?token" not in response.text

    stored = api_db.scalar(select(PasswordResetToken))
    assert stored is not None
    assert stored.delivery_link is not None
    assert "reset-password?token=" in stored.delivery_link


def test_reset_password_signs_every_other_session_out(
    api: TestClient, signed_up, api_db, api_settings: Settings
) -> None:
    """One column bump ends every outstanding token."""
    old_headers = signed_up["headers"]
    assert api.get("/api/v1/auth/me", headers=old_headers).status_code == 200

    api.post("/api/v1/auth/forgot-password", json={"email": "elena.marsh@example.com"})
    api_db.commit()
    record = api_db.scalar(select(PasswordResetToken))
    raw_token = record.delivery_link.split("token=")[1]

    # `iat` has one-second resolution, so a reset in the same second as the
    # token's issue would not appear to postdate it.
    time.sleep(1.1)

    reset = api.post(
        "/api/v1/auth/reset-password",
        json={"token": raw_token, "new_password": "a-brand-new-passphrase"},
    )
    assert reset.status_code == 200

    assert api.get("/api/v1/auth/me", headers=old_headers).status_code == 401
    assert (
        api.post(
            "/api/v1/auth/login",
            json={
                "email": "elena.marsh@example.com",
                "password": "a-brand-new-passphrase",
            },
        ).status_code
        == 200
    )


def test_a_reset_token_is_single_use(api: TestClient, signed_up, api_db) -> None:
    api.post("/api/v1/auth/forgot-password", json={"email": "elena.marsh@example.com"})
    api_db.commit()
    raw = api_db.scalar(select(PasswordResetToken)).delivery_link.split("token=")[1]

    assert (
        api.post(
            "/api/v1/auth/reset-password",
            json={"token": raw, "new_password": "first-new-passphrase"},
        ).status_code
        == 200
    )
    assert (
        api.post(
            "/api/v1/auth/reset-password",
            json={"token": raw, "new_password": "second-new-passphrase"},
        ).status_code
        == 422
    )


def test_an_expired_reset_token_is_refused(api: TestClient, signed_up, api_db) -> None:
    api.post("/api/v1/auth/forgot-password", json={"email": "elena.marsh@example.com"})
    api_db.commit()
    record = api_db.scalar(select(PasswordResetToken))
    raw = record.delivery_link.split("token=")[1]
    record.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    api_db.commit()

    response = api.post(
        "/api/v1/auth/reset-password",
        json={"token": raw, "new_password": "another-new-passphrase"},
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Storage of credentials
# ---------------------------------------------------------------------------


def test_passwords_are_stored_as_argon2id_hashes(api: TestClient, signed_up, api_db) -> None:
    user = api_db.scalar(select(User).where(User.email == "elena.marsh@example.com"))
    assert user.password_hash.startswith("$argon2id$")
    assert "correct-horse-battery" not in user.password_hash


def test_refresh_tokens_are_stored_hashed(api: TestClient, signed_up, api_db) -> None:
    from raceos.db.models import Session as SessionRow

    raw = api.cookies.get("raceos_refresh")
    row = api_db.scalar(select(SessionRow))
    assert row.refresh_token_hash != raw
    assert row.refresh_token_hash == security.hash_token(raw)


def test_ip_addresses_are_stored_as_keyed_hashes(
    api: TestClient, signed_up, api_db, api_settings: Settings
) -> None:
    """An unkeyed hash of an IPv4 address is not an anonymisation.

    The space is small enough to enumerate exhaustively, so the hash is keyed
    with the session secret.
    """
    from raceos.db.models import Session as SessionRow

    row = api_db.scalar(select(SessionRow))
    assert row.ip_hash is not None
    assert row.ip_hash != "testclient"
    import hashlib

    assert row.ip_hash != hashlib.sha256(b"testclient").hexdigest(), "must be keyed"


def test_auth_providers_is_empty(api: TestClient) -> None:
    assert api.get("/api/v1/auth/providers").json() == {"providers": []}
