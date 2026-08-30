"""Shared fixtures.

Unit tests need nothing external. Integration tests need a local PostgreSQL
with PostGIS and are skipped — not failed — when one is not configured, so a
contributor without a database can still run the unit suite.

The test database is built by running the real migrations, never by
``metadata.create_all()``. Those two can drift, and if they do it is the
migration that is wrong, so the migration is what gets exercised.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from raceos.config import Settings, get_settings
from raceos.storage.base import InMemoryStorage, set_storage_backend

BACKEND_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_TEST_DATABASE_URL = "postgresql+psycopg://raceos@localhost:5432/raceos_test"


def _test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def _database_available(url: str) -> bool:
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 3})
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        engine.dispose()
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def database_url() -> str:
    url = _test_database_url()
    if not _database_available(url):
        pytest.skip(
            f"no test database at {url.split('@')[-1]}; "
            f"set TEST_DATABASE_URL or run `make install` prerequisites"
        )
    return url


@pytest.fixture(scope="session")
def migrated_engine(database_url: str) -> Iterator[Engine]:
    """A database at ``head``, built by running the real migrations."""
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = database_url
    get_settings.cache_clear()

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))

    # Start from a known-empty schema so a leftover from an interrupted run
    # cannot make a passing test lie.
    #
    # The database may be stamped with a revision that no longer exists — a
    # migration rewritten during development is the ordinary case, and it
    # makes `downgrade` fail with ResolutionError rather than doing anything
    # useful. Recover by dropping the schema outright: this is a *test*
    # database, so there is nothing to preserve, and a hard reset is more
    # honest than a partially-migrated one.
    reset_engine_ = create_engine(database_url)
    try:
        with reset_engine_.begin() as connection:
            head = connection.execute(
                text(
                    "SELECT version_num FROM alembic_version "
                    "WHERE to_regclass('public.alembic_version') IS NOT NULL"
                )
            ).scalar_one_or_none()
        known = {rev.revision for rev in ScriptDirectory.from_config(config).walk_revisions()}
        if head is not None and head not in known:
            with reset_engine_.begin() as connection:
                connection.execute(text("DROP SCHEMA public CASCADE"))
                connection.execute(text("CREATE SCHEMA public"))
        else:
            command.downgrade(config, "base")
    finally:
        reset_engine_.dispose()

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        get_settings.cache_clear()


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Session]:
    """A session in a transaction that is rolled back after each test.

    Nothing a test writes survives it, so tests are order-independent without
    truncating tables between them.
    """
    connection = migrated_engine.connect()
    transaction = connection.begin()
    # `create_savepoint` lets a test provoke an IntegrityError — which aborts
    # the innermost transaction — without poisoning the outer one that the
    # rollback below depends on. Several tests here exist precisely to provoke
    # one, so this is load-bearing rather than defensive.
    session = sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )()
    try:
        yield session
    finally:
        session.close()
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture(autouse=True)
def _offline_storage(settings: Settings) -> Iterator[None]:
    """Every test runs against in-memory storage; nothing touches the network."""
    set_storage_backend(InMemoryStorage(settings))
    yield
    set_storage_backend(None)
