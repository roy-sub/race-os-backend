"""Database engine and session management.

The one interesting problem here is that **Supabase offers two connection
forms and we do not yet know which one Render can reach**, so both must work
with no code change:

``direct``
    ``db.<project_ref>.supabase.co:5432``. A real Postgres connection with
    every feature: prepared statements, ``LISTEN``/``NOTIFY``, session-scoped
    settings. Its hostname may resolve to **IPv6 only**, and Render's outbound
    network may not have IPv6, in which case connecting fails with a network
    error rather than an authentication one.

``pooler``
    ``aws-0-<region>.pooler.supabase.com``, running Supavisor in front of the
    same database. Reachable over IPv4. Port ``6543`` is *transaction* mode
    and port ``5432`` is *session* mode.

Transaction-mode pooling is the case that breaks a naive engine, and it breaks
it subtly: a connection is returned to the pool after every transaction, so
anything that outlives a transaction is unsafe. Specifically, **server-side
prepared statements must be disabled** — psycopg prepares a statement on one
backend and then finds itself on another, raising a confusing
``prepared statement "_pg3_0" does not exist``. Client-side pooling must also
get out of the way, because Supavisor is already the pool.

:func:`engine_options_for` encodes those differences, so switching between the
two forms is genuinely a `DATABASE_URL` edit. :func:`describe_connection`
reports which form was detected and is used by ``scripts/check_supabase.py``
and by ``/readyz``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from raceos.config import Settings, get_settings


class ConnectionForm(str, Enum):
    """Which shape of connection string we were given."""

    SUPABASE_DIRECT = "supabase_direct"
    SUPABASE_POOLER_TRANSACTION = "supabase_pooler_transaction"
    SUPABASE_POOLER_SESSION = "supabase_pooler_session"
    LOCAL = "local"


#: Port on which Supavisor runs its transaction-mode pooler.
POOLER_TRANSACTION_PORT = 6543


@dataclass(frozen=True)
class ConnectionDescription:
    """What we can say about a connection string without connecting.

    Deliberately carries no credentials: ``host`` and ``port`` only, so it is
    safe to log and to return from ``/readyz``.
    """

    form: ConnectionForm
    host: str
    port: int
    database: str
    prepared_statements_enabled: bool
    pooling: str

    def as_log_fields(self) -> dict[str, object]:
        return {
            "db_form": self.form.value,
            "db_host": self.host,
            "db_port": self.port,
            "db_name": self.database,
            "db_prepared_statements": self.prepared_statements_enabled,
            "db_pooling": self.pooling,
        }


def describe_connection(database_url: str) -> ConnectionDescription:
    """Classify a connection string. Never connects, never logs a password."""
    parts = urlsplit(database_url)
    host = parts.hostname or ""
    port = parts.port or 5432
    database = (parts.path or "/").lstrip("/") or "postgres"

    is_pooler = "pooler.supabase.com" in host
    is_direct = host.startswith("db.") and host.endswith(".supabase.co")

    if is_pooler and port == POOLER_TRANSACTION_PORT:
        form = ConnectionForm.SUPABASE_POOLER_TRANSACTION
    elif is_pooler:
        form = ConnectionForm.SUPABASE_POOLER_SESSION
    elif is_direct:
        form = ConnectionForm.SUPABASE_DIRECT
    else:
        form = ConnectionForm.LOCAL

    transaction_pooled = form is ConnectionForm.SUPABASE_POOLER_TRANSACTION
    return ConnectionDescription(
        form=form,
        host=host,
        port=port,
        database=database,
        prepared_statements_enabled=not transaction_pooled,
        pooling="supavisor" if is_pooler else "sqlalchemy_queuepool",
    )


def engine_options_for(settings: Settings, description: ConnectionDescription) -> dict[str, Any]:
    """Engine keyword arguments appropriate to the detected connection form."""
    connect_args: dict[str, Any] = {
        "connect_timeout": settings.database_connect_timeout_seconds,
        # Applied per connection by the server, so a runaway query is killed
        # by Postgres rather than holding a pooled connection open.
        "options": f"-c statement_timeout={settings.database_statement_timeout_ms}",
    }

    options: dict[str, Any] = {
        "echo": settings.database_echo,
        "future": True,
        "pool_pre_ping": True,
        "connect_args": connect_args,
    }

    if description.form is ConnectionForm.SUPABASE_POOLER_TRANSACTION:
        # Supavisor is the pool; a second pool in front of it only holds
        # connections it cannot reuse. Prepared statements cannot survive a
        # connection being handed to another client between transactions.
        connect_args["prepare_threshold"] = None
        options["poolclass"] = NullPool
    else:
        options["pool_size"] = settings.database_pool_size
        options["max_overflow"] = settings.database_max_overflow
        options["pool_timeout"] = settings.database_pool_timeout_seconds
        options["pool_recycle"] = 1800

    return options


def normalise_database_url(url: str) -> str:
    """Force the psycopg 3 driver, whatever prefix the operator pasted.

    Supabase's dashboard hands out ``postgresql://…`` and SQLAlchemy would
    then look for psycopg2, which is not a dependency. Accepting the
    dashboard's own string verbatim is worth this small normalisation.
    """
    if url.startswith("postgresql+"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    return url


_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def create_db_engine(settings: Settings | None = None) -> Engine:
    """Build an engine configured for whichever connection form is in use."""
    settings = settings or get_settings()
    url = normalise_database_url(settings.database_url.get_secret_value())
    description = describe_connection(url)
    engine = create_engine(url, **engine_options_for(settings, description))

    # PostGIS types live in the `public` schema by default; nothing here
    # depends on a custom search_path, but pinning it makes behaviour the same
    # on a fresh Supabase project and a local database.
    @event.listens_for(engine, "connect")
    def _set_session_defaults(dbapi_connection: Any, _record: Any) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")

    return engine


def get_engine() -> Engine:
    """The process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        _engine = create_db_engine()
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transactional scope: commit on success, roll back on any exception.

    Used by services and jobs. The API's per-request session is provided by
    :func:`raceos.api.deps.get_db` instead, so a request's transaction is tied
    to the request's lifetime.
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine() -> None:
    """Dispose of the engine and forget it. For tests and for config reloads."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def check_database(engine: Engine | None = None) -> dict[str, object]:
    """Liveness detail for ``/readyz`` and for ``scripts/check_supabase.py``.

    Returns the server version and whether PostGIS is installed. PostGIS is
    checked explicitly because the schema's geometry columns cannot be created
    without it, and the failure it produces during migration is far less clear
    than this one.
    """
    engine = engine or get_engine()
    with engine.connect() as connection:
        server_version = connection.execute(text("SHOW server_version")).scalar_one()
        postgis_version = connection.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'postgis'")
        ).scalar_one_or_none()
        extensions = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT extname FROM pg_extension "
                    "WHERE extname IN ('postgis','citext','pgcrypto') ORDER BY extname"
                )
            )
        ]
    return {
        "server_version": str(server_version),
        "postgis_version": postgis_version,
        "postgis_enabled": postgis_version is not None,
        "extensions": extensions,
    }
