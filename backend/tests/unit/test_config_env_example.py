"""`.env.example` and `Settings` must describe exactly the same variable set.

The brief makes `.env.example` the single source of truth for configuration:
the operator pulls the repo, copies that file, fills it in, and expects it to
be complete and unambiguous on its own. That only stays true if it cannot
drift from the code, so this test checks the correspondence in *both*
directions and fails CI either way:

* a field added to :class:`~raceos.config.Settings` but not documented would
  be a variable the operator never learns they can set;
* a variable documented but read by nothing would be a value they set in
  Render expecting an effect that never comes.

It also enforces the documentation conventions the brief asks for, because a
comment block that is present but empty is worse than no convention at all.
"""

from __future__ import annotations

import re

import pytest

from raceos.config import REPO_ROOT, SECRET_FIELD_NAMES, Settings

ENV_EXAMPLE = REPO_ROOT / ".env.example"

# A variable assignment line: NAME=value, at column zero.
_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
_REQUIREDNESS = re.compile(r"^#\s*(REQUIRED|OPTIONAL)\b")


def _parse_env_example() -> dict[str, dict[str, object]]:
    """Return ``{VAR_NAME: {"value": str, "comments": [str], "marker": str}}``.

    Comments are the contiguous ``#`` block immediately above the assignment,
    which is where the conventions require the explanation to live.
    """
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    found: dict[str, dict[str, object]] = {}

    for index, line in enumerate(lines):
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        name, value = match.group(1), match.group(2)

        comments: list[str] = []
        cursor = index - 1
        while cursor >= 0 and lines[cursor].startswith("#"):
            comments.append(lines[cursor])
            cursor -= 1
        comments.reverse()

        marker = ""
        for comment in comments:
            requiredness = _REQUIREDNESS.match(comment)
            if requiredness:
                marker = comment.lstrip("# ").strip()

        found[name] = {"value": value, "comments": comments, "marker": marker}

    return found


@pytest.fixture(scope="module")
def documented() -> dict[str, dict[str, object]]:
    return _parse_env_example()


@pytest.fixture(scope="module")
def field_names() -> set[str]:
    return {name.upper() for name in Settings.model_fields}


def test_env_example_exists_at_the_repository_root() -> None:
    assert ENV_EXAMPLE.is_file(), (
        "`.env.example` must sit at the repository root: the operator copies it "
        "from there without reading any other file first."
    )


def test_every_settings_field_is_documented(
    documented: dict[str, dict[str, object]], field_names: set[str]
) -> None:
    undocumented = sorted(field_names - documented.keys())
    assert not undocumented, (
        f"These Settings fields are missing from .env.example: {undocumented}. "
        f"Every variable the application reads must be documented there."
    )


def test_every_documented_variable_is_read_by_a_field(
    documented: dict[str, dict[str, object]], field_names: set[str]
) -> None:
    unread = sorted(documented.keys() - field_names)
    assert not unread, (
        f"These variables are documented in .env.example but no Settings field "
        f"reads them: {unread}. Remove them, or add the field."
    )


def test_every_variable_carries_an_explanatory_comment(
    documented: dict[str, dict[str, object]],
) -> None:
    """Each variable needs prose above it saying what it is and what breaks."""
    thin: list[str] = []
    for name, entry in sorted(documented.items()):
        comments = [c for c in entry["comments"] if not _REQUIREDNESS.match(c)]  # type: ignore[union-attr]
        prose = " ".join(c.lstrip("# ").strip() for c in comments).strip()
        # Section banners are rules of '=' characters; they are not an
        # explanation of the variable underneath them.
        prose = prose.replace("=", "").strip()
        if len(prose) < 30:
            thin.append(name)
    assert not thin, (
        f"These variables have no meaningful explanation above them: {thin}. "
        f"Each needs a line saying what it is and what breaks without it."
    )


def test_every_variable_is_marked_required_or_optional(
    documented: dict[str, dict[str, object]],
) -> None:
    unmarked = sorted(n for n, e in documented.items() if not e["marker"])
    assert not unmarked, (
        f"These variables carry neither `# REQUIRED` nor `# OPTIONAL (default: x)`: " f"{unmarked}."
    )


def test_optional_markers_state_their_default(
    documented: dict[str, dict[str, object]],
) -> None:
    """`# OPTIONAL` without the default leaves the reader guessing."""
    missing_default = [
        name
        for name, entry in sorted(documented.items())
        if str(entry["marker"]).startswith("OPTIONAL") and "default:" not in str(entry["marker"])
    ]
    assert not missing_default, (
        f"These OPTIONAL variables do not state their default: {missing_default}. "
        f"Use `# OPTIONAL (default: x)` or `# OPTIONAL — V2 (default: x)`."
    )


def test_no_secret_variable_ships_a_usable_value(
    documented: dict[str, dict[str, object]],
) -> None:
    """Placeholders must be obviously placeholders, never a real credential.

    A secret's example value must be empty, or carry an unmistakable
    placeholder token: a run of `x` characters, or a SCREAMING_SNAKE stand-in
    such as `PASSWORD` or `PROJECT_REF` in a connection string. Anything that
    could be mistaken for a real credential fails here rather than in a leak.
    """
    placeholders = ("xxxx", "PASSWORD", "PROJECT_REF", "REGION")
    offenders: list[str] = []
    for field in sorted(SECRET_FIELD_NAMES):
        entry = documented.get(field.upper())
        if entry is None:
            continue
        value = str(entry["value"]).strip().strip('"')
        if not value:
            continue
        if not any(token.lower() in value.lower() for token in placeholders):
            offenders.append(field.upper())
    assert not offenders, (
        f"These secret variables have example values that do not look like "
        f"placeholders: {offenders}. Use an obviously fake value of the right shape."
    )


def test_generated_secrets_ship_their_generation_command(
    documented: dict[str, dict[str, object]],
) -> None:
    """The three values the operator generates must say exactly how.

    These are the ones with no provider to fetch them from, so if the command
    is not here the operator has nowhere else to look.
    """
    expectations = {
        "SESSION_COOKIE_SECRET": "secrets.token_urlsafe",
        "INTERNAL_JOB_SECRET": "secrets.token_urlsafe",
        "JWT_PRIVATE_KEY": "openssl",
        "JWT_PUBLIC_KEY": "openssl",
    }
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for name, needle in expectations.items():
        entry = documented[name]
        block = "\n".join(str(c) for c in entry["comments"])  # type: ignore[arg-type]
        # The JWT keys share one generation block above them both.
        assert (
            needle in block or needle in text
        ), f"{name} must ship the exact command that generates it, as a comment."


def test_no_forbidden_variable_is_present(documented: dict[str, dict[str, object]]) -> None:
    """Variables whose presence would mean a wrong implementation.

    Each of these belongs to a service this build does not use. Their absence
    is a design decision, so it is asserted rather than trusted.
    """
    forbidden = {
        "REDIS_URL": "V1 has no Redis",
        "CELERY_BROKER_URL": "V1 has no Celery",
        "SENTRY_DSN": "error reporting is structured stdout logging",
        "WEATHER_PROVIDER_API_KEY": "Open-Meteo needs no key",
        "OBJECT_STORAGE_BUCKET": "storage is Supabase, not R2/S3",
        "OBJECT_STORAGE_ACCESS_KEY": "storage is Supabase, not R2/S3",
        "OBJECT_STORAGE_SECRET_KEY": "storage is Supabase, not R2/S3",
        "SUPABASE_JWKS_URL": "we do not use Supabase Auth",
        "OAUTH_GOOGLE_CLIENT_ID": "no social login is built",
        "OAUTH_APPLE_CLIENT_ID": "no social login is built",
        "CONNECTION_TOKEN_ENCRYPTION_KEY": "there are no device integrations",
        "GARMIN_CLIENT_ID": "there are no device integrations",
        "STRAVA_CLIENT_ID": "there are no device integrations",
    }
    present = {name: why for name, why in forbidden.items() if name in documented}
    assert not present, (
        f"These variables must not exist in this build: {present}. "
        f"Their presence means a deferred feature was built."
    )


def test_settings_construct_from_the_documented_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """A development boot works with nothing set at all.

    The brief requires the suite to run fully offline with no real
    credentials. That is only true if `Settings()` is constructible from its
    defaults, so this asserts it directly.
    """
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.app_env.value == "development"
    assert settings.email_enabled is False
    assert settings.phrasing_enabled is False
    assert settings.push_enabled is False
    assert settings.require_email_verification is False


def test_all_env_example_paths_are_relative_to_the_repo_root() -> None:
    """No absolute path from the author's machine leaks into the template."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for leak in ("/home/", "/Users/", "C:\\"):
        assert leak not in text, f"{leak!r} appears in .env.example"


def test_no_committed_file_contains_a_provider_key_shaped_literal() -> None:
    """Placeholders must not look like real keys to a secret scanner.

    GitHub push protection rejected an earlier revision of `.env.example`
    because `sk_test_` followed by 24 alphanumerics *is* the shape of a real
    Stripe test key — the scanner was right, and a placeholder that trips it
    is a bad placeholder. Every fill-me-in value now breaks the alphanumeric
    run (`sk_test_<xxxx...>`), which keeps it obviously fake to a human and
    unmatchable to a scanner.

    This guards the whole tree, not just `.env.example`, so a fixture or a
    docstring cannot reintroduce the problem and block a push.
    """
    key_shapes = re.compile(
        r"\b(?:sk_live|sk_test|rk_live|rk_test|pk_live|pk_test|whsec)_[A-Za-z0-9]{20,}"
        r"|\bsb_(?:secret|publishable)_[A-Za-z0-9]{20,}"
    )
    repo_root = ENV_EXAMPLE.parent
    skip_dirs = {".git", ".venv", "node_modules", "__pycache__", "out", ".pytest_cache"}
    offenders: list[str] = []

    for path in repo_root.rglob("*"):
        if not path.is_file() or set(path.parts) & skip_dirs:
            continue
        if path.suffix not in {".py", ".md", ".txt", ".yaml", ".yml", ".toml", ".example", ""}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for match in key_shapes.finditer(text):
            offenders.append(f"{path.relative_to(repo_root)}: {match.group()[:20]}...")

    assert not offenders, (
        "These literals have the shape of a real provider key and will be "
        f"rejected by secret scanning: {offenders}. Break the alphanumeric run, "
        "e.g. sk_test_<xxxxxxxxxxxxxxxxxxxxxxxx>."
    )
