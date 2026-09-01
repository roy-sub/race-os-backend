"""Application configuration.

One frozen settings object, constructed once at startup and read everywhere.

Three rules this module exists to enforce, from the build specification
(Part 3.2) and the V1 build brief:

1.  **No ``os.getenv`` in business logic.** Every value the application reads
    is a field here. Import ``get_settings()`` instead of reaching for the
    environment.
2.  **Every threshold named in the specification is a config value**, never a
    literal in a conditional. Where the spec names a number, it appears below
    as a default and is documented in ``.env.example``.
3.  **The app refuses to boot in production with a required variable
    missing.** Requirements that only bind in production are checked by
    :meth:`Settings._check_production_requirements`, so a development run
    stays frictionless while a production run cannot start half-configured.

``.env.example`` at the repository root is the single source of truth for the
variable set. ``tests/unit/test_config_env_example.py`` asserts the two agree
in both directions: every field here appears there, and every variable there
is read by a field here. Adding a field without documenting it fails CI.
"""

from __future__ import annotations

import re
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The repository root, two levels above this file (backend/raceos/config.py).
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Fields whose values must never reach a log line, an error message, or the
#: ``/readyz`` payload. Consumed by :mod:`raceos.logging` for its redaction
#: filter and by :func:`Settings.redacted_dump`.
SECRET_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "database_url",
        "supabase_secret_key",
        "supabase_publishable_key",
        "jwt_private_key",
        "session_cookie_secret",
        "internal_job_secret",
        "stripe_secret_key",
        "stripe_webhook_secret",
        "email_provider_api_key",
        "phrasing_model_api_key",
        "vapid_private_key",
    }
)


class AppEnv(str, Enum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class EmailTransport(str, Enum):
    """Which :class:`~raceos.email.sender.EmailSender` implementation to bind.

    ``logging`` is the V1 transport: it renders the message in full and writes
    it to structured logs and to the database, so the whole email subsystem is
    exercised without a provider account. The other two are the real adapters,
    unused in V1 and reachable by config alone.
    """

    LOGGING = "logging"
    RESEND = "resend"
    POSTMARK = "postmark"


Weekday = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_PEM_PRIVATE = re.compile(r"-----BEGIN (RSA )?PRIVATE KEY-----")
_PEM_PUBLIC = re.compile(r"-----BEGIN (RSA )?PUBLIC KEY-----")


def _normalise_pem(value: str) -> str:
    r"""Accept a PEM key however a ``.env`` file managed to carry it.

    A PEM block is multi-line and ``.env`` files are line-oriented, which is
    the single most common configuration mistake on this project. Three forms
    are accepted and normalised to real newlines:

    * ``"-----BEGIN...\n...\n-----END...\n"`` — double-quoted with escaped
      newlines. This is the form ``.env.example`` documents and the one the
      generation commands there produce.
    * A genuinely multi-line value (possible in Render's dashboard, which has
      a multi-line editor, and in shell exports).
    * The same with CRLF line endings, from a file that went via Windows.

    Anything that does not then look like a PEM block is rejected at startup
    with a message naming the field, rather than failing later inside a token
    signature where the cause is unrecognisable.
    """
    text = value.strip().strip('"').strip("'")
    text = text.replace("\\n", "\n").replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text


def _origin_of(url: str) -> str:
    """``https://app.example/anything`` -> ``https://app.example``.

    An ``Origin`` header is scheme, host and port and nothing else, so a value
    carrying a path or a trailing slash has to be reduced before it can be
    compared against one. Anything unparseable returns "" and is dropped by the
    caller rather than being sent to the browser as a broken origin.
    """
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


class Settings(BaseSettings):
    """Every variable the application reads. Frozen after construction."""

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app_env: AppEnv = AppEnv.DEVELOPMENT
    log_level: str = "INFO"
    app_base_url: str = "http://localhost:3000"
    api_base_url: str = "http://localhost:8000"
    cors_allowed_origins: str = "http://localhost:3000"

    # ------------------------------------------------------------------
    # Database — Supabase Postgres + PostGIS
    # ------------------------------------------------------------------
    database_url: SecretStr = SecretStr("postgresql+psycopg://raceos@localhost:5432/raceos_dev")
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)
    database_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    database_connect_timeout_seconds: int = Field(default=10, ge=1, le=120)
    database_statement_timeout_ms: int = Field(default=30_000, ge=1_000, le=300_000)
    database_echo: bool = False

    # ------------------------------------------------------------------
    # Supabase Storage (object storage only — never Supabase Auth)
    # ------------------------------------------------------------------
    supabase_url: str = "http://localhost:54321"
    supabase_secret_key: SecretStr = SecretStr("")
    supabase_publishable_key: SecretStr = SecretStr("")
    supabase_storage_bucket_private: str = "raceos-private"
    supabase_storage_bucket_public: str = "raceos-public"
    storage_signed_url_ttl_seconds: int = Field(default=3600, ge=60, le=604_800)
    storage_request_timeout_seconds: int = Field(default=30, ge=1, le=300)
    media_base_url: str = ""

    # ------------------------------------------------------------------
    # Authentication — our own RS256 keypair, not Supabase Auth
    # ------------------------------------------------------------------
    jwt_private_key: SecretStr = SecretStr("")
    jwt_public_key: str = ""
    jwt_access_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    jwt_refresh_ttl_seconds: int = Field(default=2_592_000, ge=3_600, le=31_536_000)
    jwt_issuer: str = "raceos"
    jwt_audience: str = "raceos-api"

    session_cookie_secret: SecretStr = SecretStr("")
    session_cookie_name: str = "raceos_refresh"
    session_cookie_domain: str = ""

    argon2_time_cost: int = Field(default=3, ge=1, le=10)
    argon2_memory_cost_kib: int = Field(default=65_536, ge=8_192, le=1_048_576)
    argon2_parallelism: int = Field(default=4, ge=1, le=16)

    require_email_verification: bool = False
    email_verification_ttl_hours: int = Field(default=48, ge=1, le=336)
    password_reset_ttl_minutes: int = Field(default=60, ge=5, le=1_440)
    login_max_attempts: int = Field(default=3, ge=1, le=20)
    login_lock_minutes: int = Field(default=15, ge=1, le=1_440)

    # ------------------------------------------------------------------
    # Internal job endpoints (called by an external cron)
    # ------------------------------------------------------------------
    internal_job_secret: SecretStr = SecretStr("")

    # ------------------------------------------------------------------
    # Payments — Stripe, test mode in V1
    # ------------------------------------------------------------------
    stripe_secret_key: SecretStr = SecretStr("")
    stripe_publishable_key: str = ""
    stripe_webhook_secret: SecretStr = SecretStr("")
    stripe_price_id_race_plan: str = ""
    stripe_price_id_season_pass: str = ""
    stripe_price_id_coach: str = ""
    stripe_request_timeout_seconds: int = Field(default=20, ge=1, le=120)

    # ------------------------------------------------------------------
    # Weather — Open-Meteo. No API key exists for this provider.
    # ------------------------------------------------------------------
    open_meteo_base_url: str = "https://api.open-meteo.com/v1"
    weather_forecast_horizon_hours: int = Field(default=72, ge=1, le=384)
    weather_request_timeout_seconds: int = Field(default=15, ge=1, le=120)
    forecast_cache_ttl_minutes: int = Field(default=180, ge=1, le=1_440)

    # ------------------------------------------------------------------
    # Terrain tiles (frontend map)
    # ------------------------------------------------------------------
    terrain_pmtiles_base_url: str = "https://demo.mapterhorn.com"

    # ------------------------------------------------------------------
    # Solver
    # ------------------------------------------------------------------
    solver_sla_ms: int = Field(default=6_000, ge=100, le=120_000)
    solver_sync_budget_ms: int = Field(default=5_000, ge=100, le=120_000)
    solver_target_p50_ms: int = Field(default=3_100, ge=1, le=120_000)
    solver_target_p95_ms: int = Field(default=5_400, ge=1, le=120_000)

    # ------------------------------------------------------------------
    # Phrasing layer — off in V1; templates are the shipping path
    # ------------------------------------------------------------------
    phrasing_enabled: bool = False
    phrasing_timeout_ms: int = Field(default=2_500, ge=100, le=60_000)
    phrasing_model_id: str = ""
    phrasing_model_api_key: SecretStr = SecretStr("")

    # ------------------------------------------------------------------
    # Email — subsystem built, transport no-op in V1
    # ------------------------------------------------------------------
    email_enabled: bool = False
    email_transport: EmailTransport = EmailTransport.LOGGING
    email_from_address: str = "noreply@raceos.example"
    email_from_name: str = "RaceOS"
    email_provider_api_key: SecretStr = SecretStr("")

    # ------------------------------------------------------------------
    # Web push — subsystem built, disabled in V1
    # ------------------------------------------------------------------
    push_enabled: bool = False
    vapid_public_key: str = ""
    vapid_private_key: SecretStr = SecretStr("")
    vapid_subject: str = "mailto:noreply@raceos.example"

    # ------------------------------------------------------------------
    # Product thresholds (Build Spec Part 3.1)
    # ------------------------------------------------------------------
    course_bundle_freeze_days: str = "Thu,Fri,Sat,Sun"
    crowd_verified_min_uploads: int = Field(default=40, ge=1)
    crowd_confidence_high_uploads: int = Field(default=30, ge=1)
    crowd_confidence_high_agreement_pct: int = Field(default=65, ge=0, le=100)
    crowd_confidence_low_uploads: int = Field(default=15, ge=1)
    drift_split_threshold_minutes: float = Field(default=2.0, gt=0)
    drift_margin_risk_minutes: float = Field(default=20.0, gt=0)
    constraint_staleness_days_default: int = Field(default=180, ge=1)
    race_notification_suppression_buffer_hours: int = Field(default=3, ge=0, le=48)
    support_grant_ttl_minutes: int = Field(default=60, ge=1, le=1_440)
    share_token_bytes: int = Field(default=16, ge=16, le=64)
    upload_max_bytes: int = Field(default=20_971_520, ge=1_024)
    coach_max_athletes: int = Field(default=15, ge=1, le=500)

    # ------------------------------------------------------------------
    # Rate limiting and caching — database-backed, no Redis
    # ------------------------------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_default_per_minute: int = Field(default=120, ge=1)
    rate_limit_auth_per_minute: int = Field(default=10, ge=1)
    rate_limit_share_code_per_minute: int = Field(default=5, ge=1)
    idempotency_key_ttl_hours: int = Field(default=24, ge=1, le=720)
    course_bundle_cache_ttl_seconds: int = Field(default=300, ge=0, le=86_400)

    # ==================================================================
    # Validators
    # ==================================================================

    @field_validator("jwt_private_key", mode="before")
    @classmethod
    def _normalise_private_key(cls, v: object) -> object:
        if isinstance(v, str) and v.strip():
            return _normalise_pem(v)
        return v

    @field_validator("jwt_public_key", mode="before")
    @classmethod
    def _normalise_public_key(cls, v: object) -> object:
        if isinstance(v, str) and v.strip():
            return _normalise_pem(v)
        return v

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, v: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}, got {v!r}")
        return upper

    @field_validator("course_bundle_freeze_days")
    @classmethod
    def _valid_freeze_days(cls, v: str) -> str:
        allowed = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
        days = [d.strip() for d in v.split(",") if d.strip()]
        bad = [d for d in days if d not in allowed]
        if bad:
            raise ValueError(
                f"COURSE_BUNDLE_FREEZE_DAYS contains unknown day(s) {bad}; "
                f"use three-letter names from {sorted(allowed)}"
            )
        return ",".join(days)

    @model_validator(mode="after")
    def _check_crowd_bands_are_ordered(self) -> Settings:
        if self.crowd_confidence_low_uploads >= self.crowd_confidence_high_uploads:
            raise ValueError(
                "CROWD_CONFIDENCE_LOW_UPLOADS must be below "
                "CROWD_CONFIDENCE_HIGH_UPLOADS, otherwise the 'med' band is empty"
            )
        return self

    @model_validator(mode="after")
    def _check_solver_budgets_are_ordered(self) -> Settings:
        if self.solver_sync_budget_ms > self.solver_sla_ms:
            raise ValueError(
                "SOLVER_SYNC_BUDGET_MS must not exceed SOLVER_SLA_MS: the "
                "synchronous path has to hand off before the hard SLA, not after"
            )
        if self.solver_target_p95_ms > self.solver_sla_ms:
            raise ValueError("SOLVER_TARGET_P95_MS must not exceed SOLVER_SLA_MS")
        if self.solver_target_p50_ms > self.solver_target_p95_ms:
            raise ValueError("SOLVER_TARGET_P50_MS must not exceed SOLVER_TARGET_P95_MS")
        return self

    @model_validator(mode="after")
    def _check_production_requirements(self) -> Settings:
        """Refuse to boot in production with a required variable missing.

        Development and test runs use working defaults so the suite needs no
        secrets at all. Staging and production must be configured explicitly:
        a missing value there is a deployment error that should surface at
        boot, loudly, rather than as a 500 on the first request that needs it.
        """
        if self.app_env is AppEnv.DEVELOPMENT:
            return self

        missing: list[str] = []

        def require(env_name: str, value: object) -> None:
            raw = value.get_secret_value() if isinstance(value, SecretStr) else value
            if not str(raw).strip():
                missing.append(env_name)

        require("DATABASE_URL", self.database_url)
        require("SUPABASE_URL", self.supabase_url)
        require("SUPABASE_SECRET_KEY", self.supabase_secret_key)
        require("JWT_PRIVATE_KEY", self.jwt_private_key)
        require("JWT_PUBLIC_KEY", self.jwt_public_key)
        require("SESSION_COOKIE_SECRET", self.session_cookie_secret)
        require("INTERNAL_JOB_SECRET", self.internal_job_secret)
        require("APP_BASE_URL", self.app_base_url)
        require("API_BASE_URL", self.api_base_url)
        require("CORS_ALLOWED_ORIGINS", self.cors_allowed_origins)
        require("TERRAIN_PMTILES_BASE_URL", self.terrain_pmtiles_base_url)
        require("EMAIL_FROM_ADDRESS", self.email_from_address)

        # Stripe is required in production because checkout is a shipped
        # surface. Its price IDs are required with it: an authorize call
        # against an empty price ID fails at the provider, which is a worse
        # place to discover it than here.
        require("STRIPE_SECRET_KEY", self.stripe_secret_key)
        require("STRIPE_PUBLISHABLE_KEY", self.stripe_publishable_key)
        require("STRIPE_WEBHOOK_SECRET", self.stripe_webhook_secret)
        require("STRIPE_PRICE_ID_RACE_PLAN", self.stripe_price_id_race_plan)
        require("STRIPE_PRICE_ID_SEASON_PASS", self.stripe_price_id_season_pass)
        require("STRIPE_PRICE_ID_COACH", self.stripe_price_id_coach)

        # Conditionally required: only when the feature is switched on.
        if self.email_enabled and self.email_transport is not EmailTransport.LOGGING:
            require("EMAIL_PROVIDER_API_KEY", self.email_provider_api_key)
        if self.push_enabled:
            require("VAPID_PUBLIC_KEY", self.vapid_public_key)
            require("VAPID_PRIVATE_KEY", self.vapid_private_key)
        if self.phrasing_enabled:
            require("PHRASING_MODEL_ID", self.phrasing_model_id)
            require("PHRASING_MODEL_API_KEY", self.phrasing_model_api_key)

        if missing:
            raise ValueError(
                f"APP_ENV={self.app_env.value} requires these variables, which are "
                f"unset or empty: {', '.join(sorted(missing))}. "
                f"See .env.example for the shape of each."
            )

        if "*" in self.cors_allowed_origins:
            raise ValueError("CORS_ALLOWED_ORIGINS must not contain a wildcard outside development")

        if self.jwt_private_key.get_secret_value() and not _PEM_PRIVATE.search(
            self.jwt_private_key.get_secret_value()
        ):
            raise ValueError(
                "JWT_PRIVATE_KEY does not look like a PEM private key. It must "
                "contain a '-----BEGIN PRIVATE KEY-----' header; see .env.example "
                "for the exact quoting a .env file needs."
            )
        if self.jwt_public_key and not _PEM_PUBLIC.search(self.jwt_public_key):
            raise ValueError(
                "JWT_PUBLIC_KEY does not look like a PEM public key. It must "
                "contain a '-----BEGIN PUBLIC KEY-----' header."
            )

        return self

    # ==================================================================
    # Derived accessors
    # ==================================================================

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    @property
    def cors_origin_list(self) -> tuple[str, ...]:
        """Origins the browser may call this API from.

        ``CORS_ALLOWED_ORIGINS`` is the explicit list, and the origin of
        ``APP_BASE_URL`` is folded in on top of it.

        That second half is not a convenience. ``APP_BASE_URL`` is already the
        declared public origin of the frontend — it is what every link this
        service builds is rooted at — so a deployment where the frontend can
        receive our emails but not call our API is incoherent. Deriving it here
        means the one origin that must work cannot be left out of a second
        variable by accident, and an operator adding a preview or a custom
        domain still lists those in ``CORS_ALLOWED_ORIGINS`` as before.

        A wildcard is still rejected outside development by ``_validate``,
        which reads the raw setting, so nothing here can widen that.
        """
        origins: list[str] = []
        for candidate in (
            *(o.strip() for o in self.cors_allowed_origins.split(",")),
            _origin_of(self.app_base_url),
        ):
            if candidate and candidate not in origins:
                origins.append(candidate)
        return tuple(origins)

    @property
    def freeze_day_set(self) -> frozenset[str]:
        """Weekday names on which a course-bundle publish is refused.

        Evaluated in UTC by the publish endpoint, per Build Spec Part 6.1.
        """
        return frozenset(d.strip() for d in self.course_bundle_freeze_days.split(",") if d.strip())

    @property
    def public_media_base_url(self) -> str:
        """Where ``assets/...`` paths resolve.

        Defaults to the Supabase public bucket when ``MEDIA_BASE_URL`` is
        unset, so a working deployment needs one fewer variable. None of the
        41 catalogued assets exists yet; the media service serves what is
        configured and 404s cleanly otherwise, and the weekly audit job
        reports every one that does not resolve.
        """
        if self.media_base_url:
            return self.media_base_url.rstrip("/")
        return f"{self.supabase_url.rstrip('/')}/storage/v1/object/public/{self.supabase_storage_bucket_public}"

    def redacted_dump(self) -> dict[str, object]:
        """Config for the startup log, with every secret replaced.

        Build Spec Part 3: "log the resolved config with secrets redacted".
        ``tests/unit/test_logging_redaction.py`` asserts no secret value
        survives this.
        """
        out: dict[str, object] = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if name in SECRET_FIELD_NAMES:
                raw = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
                out[name] = "<set>" if str(raw).strip() else "<unset>"
            elif isinstance(value, SecretStr):  # pragma: no cover - defensive
                out[name] = "<set>" if value.get_secret_value() else "<unset>"
            elif isinstance(value, Enum):
                out[name] = value.value
            else:
                out[name] = value
        return out


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings object, constructed once.

    Cached rather than global so tests can clear it
    (``get_settings.cache_clear()``) after changing the environment. Nothing
    in the application should construct :class:`Settings` directly.
    """
    return Settings()


SettingsDep = Annotated[Settings, "raceos.config.get_settings"]
