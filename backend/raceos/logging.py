"""Structured JSON logging to stdout, with secrets redacted.

V1 has no Sentry. Error reporting is structured logging behind the
:class:`~raceos.observability.ErrorReporter` interface, so adding Sentry later
is one class rather than a refactor.

Two things this module guarantees, both asserted by
``tests/unit/test_logging_redaction.py``:

* **Every record is a single line of JSON on stdout**, so Render's log drain
  and any downstream tooling can parse it without a grok pattern.
* **No configured secret survives into a log line**, wherever it appears — in
  the message, in a structured field, inside a connection string, or in an
  exception traceback. The redaction runs on the rendered output, not on the
  arguments, because the arguments are not the only way a value gets in.

The redaction filter is built from the *live* settings values, so it redacts
the real secrets rather than pattern-matching things that look secret. It also
carries a small set of shape-based patterns, which catch credentials that are
not ours — a Stripe key in an upstream error body, a password inside a URL.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable, MutableMapping
from contextvars import ContextVar
from typing import Any
from urllib.parse import urlsplit

from pydantic import SecretStr

from raceos.config import SECRET_FIELD_NAMES, Settings, get_settings

#: Request id for the current request, set by the middleware and attached to
#: every record emitted while handling it.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

#: Authenticated user id for the current request, when there is one.
actor_id_var: ContextVar[str | None] = ContextVar("actor_id", default=None)

REDACTED = "[REDACTED]"

#: Shape-based patterns for credentials that never came from our own config:
#: an upstream error echoing a key back, a password inside a URL a caller
#: supplied. Ordered most specific first.
_SHAPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Passwords inside connection strings: keep the scheme, user and host.
    re.compile(r"(?P<keep>://[^:/@\s]+):[^@/\s]+@"),
    # Provider keys with recognisable prefixes.
    re.compile(r"\b(sk_live|sk_test|rk_live|rk_test|whsec|pk_live|pk_test)_[A-Za-z0-9]{8,}"),
    re.compile(r"\bsb_(secret|publishable)_[A-Za-z0-9_-]{8,}"),
    # PEM blocks, which must never be logged whole.
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    # Bearer tokens and JWTs.
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
)

#: Every attribute the standard library sets on a LogRecord. Passing any of
#: these through ``extra`` raises at emit time, so :class:`SafeExtraLogger`
#: renames them instead.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

#: The same set, under the name the formatter and filter use when deciding
#: which record attributes are caller-supplied context rather than the
#: standard library's own. They are the same question asked from two sides.
_STANDARD_ATTRS = _RESERVED_RECORD_ATTRS


def _decompose(name: str, value: str) -> list[str]:
    """Turn one configured secret into the literals worth redacting.

    Most secrets are redacted whole. A connection string is the exception, and
    treating it like the others is a mistake in both directions:

    * Redacting the *whole* URL destroys the host, database and user, which
      are exactly what a connection failure needs to be debuggable — the log
      line becomes ``could not connect to [REDACTED]``.
    * Redacting *nothing* leaks the password, and leaks it again whenever the
      password appears on its own rather than inside the URL.

    So a URL contributes its **password component** as the literal. The
    ``://user:pass@host`` shape pattern then masks the password in place when
    the whole URL is logged, and this literal catches the password anywhere
    else it surfaces.
    """
    text = value.strip()
    if not text:
        return []
    if "://" in text and "@" in text:
        parts = urlsplit(text)
        password = parts.password or ""
        return [password] if len(password) >= 8 else []
    return [text]


def literal_secrets(settings: Settings) -> tuple[str, ...]:
    """Every configured secret value, longest first.

    Longest first matters: if one secret is a substring of another, redacting
    the shorter one first would leave a fragment of the longer one behind.
    Values shorter than eight characters are skipped, because redacting a
    common short string would corrupt unrelated log output.
    """
    values: list[str] = []
    for name in SECRET_FIELD_NAMES:
        raw = getattr(settings, name, None)
        text = raw.get_secret_value() if isinstance(raw, SecretStr) else raw
        if isinstance(text, str):
            values.extend(t for t in _decompose(name, text) if len(t) >= 8)
    return tuple(sorted(set(values), key=len, reverse=True))


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove configured secrets and credential-shaped substrings from *text*."""
    for secret in secrets:
        if secret and secret in text:
            text = text.replace(secret, REDACTED)
    for pattern in _SHAPE_PATTERNS:
        if pattern.groups and "keep" in (pattern.groupindex or {}):
            text = pattern.sub(rf"\g<keep>:{REDACTED}@", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class RedactionFilter(logging.Filter):
    """Redacts the rendered message and every structured field on a record."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - malformed format args
            rendered = str(record.msg)
        record.msg = redact(rendered, self._secrets)
        record.args = ()

        for key, value in list(record.__dict__.items()):
            if key in _STANDARD_ATTRS or key.startswith("_"):
                continue
            if isinstance(value, str):
                record.__dict__[key] = redact(value, self._secrets)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the request context attached."""

    def __init__(self, secrets: tuple[str, ...] = (), service: str = "raceos-api") -> None:
        super().__init__()
        self._secrets = secrets
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service,
            "message": record.getMessage(),
        }

        request_id = request_id_var.get()
        if request_id:
            payload["request_id"] = request_id
        actor_id = actor_id_var.get()
        if actor_id:
            payload["actor_id"] = actor_id

        for key, value in record.__dict__.items():
            if key in _STANDARD_ATTRS or key.startswith("_") or key in payload:
                continue
            payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)

        line = json.dumps(payload, default=str, ensure_ascii=False)
        # Belt and braces: the formatter is the last thing to touch the text,
        # so a secret that reached a field the filter did not walk (a nested
        # dict, an exception's repr) is still removed here.
        return redact(line, self._secrets)


def configure_logging(settings: Settings | None = None, service: str = "raceos-api") -> None:
    """Install JSON logging on the root logger. Idempotent."""
    settings = settings or get_settings()
    secrets = literal_secrets(settings)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter(secrets=secrets, service=service))
    handler.addFilter(RedactionFilter(secrets=secrets))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Uvicorn installs its own handlers; route them through ours so access
    # logs are JSON too and are redacted on the same path.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True

    # SQLAlchemy's engine logger is extremely chatty at INFO and its messages
    # contain bound parameters; keep it at WARNING unless echo is on.
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.database_echo else logging.WARNING
    )


class SafeExtraLogger(logging.LoggerAdapter):  # type: ignore[type-arg]
    """A logger whose ``extra`` keys can never collide with a LogRecord's own.

    ``logging`` raises ``KeyError: "Attempt to overwrite 'created' in
    LogRecord"`` if an ``extra`` key shadows a built-in attribute — and the
    colliding names are ordinary words a caller reaches for naturally:
    ``created``, ``name``, ``module``, ``message``, ``filename``, ``args``.

    The failure is at *emit* time, in the logging call itself, so it turns a
    diagnostic line into an exception on a path that was working. Renaming the
    key with a trailing underscore keeps the value visible in the log and makes
    the collision impossible to trip over.
    """

    def process(
        self, msg: object, kwargs: MutableMapping[str, Any]
    ) -> tuple[object, MutableMapping[str, Any]]:
        extra = kwargs.get("extra")
        if extra:
            kwargs["extra"] = {
                (f"{key}_" if key in _RESERVED_RECORD_ATTRS else key): value
                for key, value in extra.items()
            }
        return msg, kwargs


def get_logger(name: str) -> SafeExtraLogger:
    return SafeExtraLogger(logging.getLogger(name), {})
