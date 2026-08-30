"""No secret reaches a log line, by any route.

Build Spec Part 16.2: "No secret ever logged; the logger has a redaction
filter with a test asserting it." This is that test.

It matters that the assertions go through the *real* logging stack — filter,
formatter, handler — rather than calling :func:`redact` directly, because the
failure mode being guarded against is a value arriving by a path the filter
does not walk. Each test below feeds a secret in by a different route.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from pydantic import SecretStr

from raceos.config import SECRET_FIELD_NAMES, Settings
from raceos.logging import (
    REDACTED,
    JsonFormatter,
    RedactionFilter,
    SafeExtraLogger,
    actor_id_var,
    literal_secrets,
    redact,
    request_id_var,
)

# Obvious fakes of the right shape. Never real credentials.
FAKE_SUPABASE_SECRET = "sb_secret_aaaabbbb"
FAKE_STRIPE_SECRET = "sk_test_aaaabbbb"
FAKE_STRIPE_WEBHOOK = "whsec_zzzzyyyy"
FAKE_INTERNAL_JOB = "u2VN0aaaaaaaabbbbbbbbccccccccddddddddeeeeeeee"
FAKE_SESSION_COOKIE = "Q7kZaaaaaaaabbbbbbbbccccccccddddddddeeeeeeeeffff"
FAKE_DB_URL = "postgresql+psycopg://postgres:hunter2hunter2@db.abcdefgh.supabase.co:5432/postgres"
FAKE_PRIVATE_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQAAAAAAAAAAAAAA\n"
    "-----END PRIVATE KEY-----\n"
)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_url=SecretStr(FAKE_DB_URL),
        supabase_secret_key=SecretStr(FAKE_SUPABASE_SECRET),
        stripe_secret_key=SecretStr(FAKE_STRIPE_SECRET),
        stripe_webhook_secret=SecretStr(FAKE_STRIPE_WEBHOOK),
        internal_job_secret=SecretStr(FAKE_INTERNAL_JOB),
        session_cookie_secret=SecretStr(FAKE_SESSION_COOKIE),
        jwt_private_key=SecretStr(FAKE_PRIVATE_KEY),
    )


@pytest.fixture
def capture(settings: Settings):
    """A logger wired exactly as production is, writing to a buffer."""
    stream = io.StringIO()
    secrets = literal_secrets(settings)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(secrets=secrets))
    handler.addFilter(RedactionFilter(secrets=secrets))

    underlying = logging.getLogger("raceos.test.redaction")
    underlying.handlers = [handler]
    underlying.setLevel(logging.DEBUG)
    underlying.propagate = False
    # Exercise the adapter the application actually uses, not a bare logger:
    # its `extra` handling is part of what is under test.
    logger = SafeExtraLogger(underlying, {})

    def emit(*args: object, **kwargs: object) -> str:
        stream.truncate(0)
        stream.seek(0)
        logger.info(*args, **kwargs)  # type: ignore[arg-type]
        return stream.getvalue()

    yield emit
    underlying.handlers = []


ALL_FAKES = (
    FAKE_SUPABASE_SECRET,
    FAKE_STRIPE_SECRET,
    FAKE_STRIPE_WEBHOOK,
    FAKE_INTERNAL_JOB,
    FAKE_SESSION_COOKIE,
    "hunter2hunter2",
)


def test_secret_in_the_message_is_redacted(capture) -> None:
    out = capture("connecting with %s", FAKE_SUPABASE_SECRET)
    assert FAKE_SUPABASE_SECRET not in out
    assert REDACTED in out


def test_secret_in_a_structured_field_is_redacted(capture) -> None:
    out = capture("stripe call failed", extra={"key_used": FAKE_STRIPE_SECRET})
    assert FAKE_STRIPE_SECRET not in out


def test_password_inside_a_connection_string_is_redacted(capture) -> None:
    out = capture("could not connect to %s", FAKE_DB_URL)
    assert "hunter2hunter2" not in out
    # The useful part survives, or the log line is worthless for debugging.
    assert "db.abcdefgh.supabase.co" in out


def test_secret_inside_an_exception_traceback_is_redacted(capture) -> None:
    try:
        raise RuntimeError(f"auth rejected for {FAKE_INTERNAL_JOB}")
    except RuntimeError:
        out = capture("job dispatch failed", exc_info=True)
    assert FAKE_INTERNAL_JOB not in out


def test_private_key_block_is_never_logged_whole(capture) -> None:
    out = capture("loaded signing key: %s", FAKE_PRIVATE_KEY)
    assert "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ" not in out
    assert "BEGIN PRIVATE KEY" not in out


def test_secret_in_a_nested_structure_is_redacted(capture) -> None:
    """A dict field is serialised by the formatter, not walked by the filter.

    This is exactly the route a naive redaction filter misses, which is why
    the formatter redacts its own rendered output as a second pass.
    """
    out = capture("upstream error", extra={"body": {"error": {"key": FAKE_STRIPE_SECRET}}})
    assert FAKE_STRIPE_SECRET not in out


def test_bearer_token_is_redacted_even_when_not_ours(capture) -> None:
    out = capture("proxying with Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345")
    assert "abcdefghijklmnopqrstuvwxyz012345" not in out


def test_jwt_is_redacted_even_when_not_ours(capture) -> None:
    jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLWhlcmU"
    out = capture("token rejected: %s", jwt)
    assert jwt not in out


@pytest.mark.parametrize("secret", ALL_FAKES)
def test_no_configured_secret_survives_any_route(capture, secret: str) -> None:
    """Sweep: each secret, through message, field and exception at once."""
    try:
        raise ValueError(secret)
    except ValueError:
        out = capture("failure with %s", secret, extra={"ctx": secret}, exc_info=True)
    assert secret not in out


def test_output_is_one_json_object_per_line(capture) -> None:
    out = capture("plain message")
    assert out.count("\n") == 1
    payload = json.loads(out)
    assert payload["message"] == "plain message"
    assert payload["level"] == "INFO"
    assert payload["service"] == "raceos-api"
    assert "ts" in payload


def test_request_id_and_actor_are_attached_when_set(capture) -> None:
    token_request = request_id_var.set("req_abc123")
    token_actor = actor_id_var.set("11111111-2222-3333-4444-555555555555")
    try:
        payload = json.loads(capture("handled"))
    finally:
        request_id_var.reset(token_request)
        actor_id_var.reset(token_actor)
    assert payload["request_id"] == "req_abc123"
    assert payload["actor_id"] == "11111111-2222-3333-4444-555555555555"


def test_request_id_absent_when_unset(capture) -> None:
    payload = json.loads(capture("no request context"))
    assert "request_id" not in payload


def test_redacted_dump_never_exposes_a_secret(settings: Settings) -> None:
    """The startup config log must be safe to emit verbatim."""
    dumped = json.dumps(settings.redacted_dump())
    for secret in ALL_FAKES:
        assert secret not in dumped
    for field in SECRET_FIELD_NAMES:
        assert settings.redacted_dump()[field] in {"<set>", "<unset>"}


def test_short_values_are_not_redacted(settings: Settings) -> None:
    """Redacting a short string would corrupt unrelated output.

    A secret of fewer than eight characters is not treated as a literal to
    replace; production values are far longer, and blanket-replacing a short
    string would mangle ordinary log lines.
    """
    short = Settings(_env_file=None, internal_job_secret=SecretStr("abc"))  # type: ignore[call-arg]
    assert "abc" not in literal_secrets(short)


def test_redact_is_stable_when_there_is_nothing_to_remove() -> None:
    clean = "solve completed in 412 ms for plan 7f3c"
    assert redact(clean, ("some-secret-value",)) == clean


# ---------------------------------------------------------------------------
# Structured context must never break the call that emits it
# ---------------------------------------------------------------------------


def test_reserved_extra_key_does_not_raise(capture) -> None:
    """`extra={"created": ...}` must not blow up the logging call.

    The standard library raises `KeyError: "Attempt to overwrite 'created' in
    LogRecord"` when an `extra` key shadows a built-in attribute, and the
    colliding names are ordinary words a caller reaches for: `created`,
    `name`, `module`, `message`, `filename`. The failure happens at emit time,
    turning a diagnostic line into an exception on a path that was working —
    which is exactly how it was found, in the seed script.
    """
    out = capture("bundle loaded", extra={"created": True, "name": "tramuntana"})
    payload = json.loads(out)
    assert payload["created_"] is True
    assert payload["name_"] == "tramuntana"
    # And the record's own fields are untouched.
    assert payload["message"] == "bundle loaded"
    assert payload["logger"] == "raceos.test.redaction"


def test_non_colliding_extra_keys_are_untouched(capture) -> None:
    payload = json.loads(capture("solved", extra={"plan_id": "abc", "duration_ms": 412}))
    assert payload["plan_id"] == "abc"
    assert payload["duration_ms"] == 412
