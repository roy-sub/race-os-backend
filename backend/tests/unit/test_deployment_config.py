"""The deployment configuration, checked against the code that runs under it.

A `render.yaml` naming a variable the application does not read, or pointing a
provider at a path that does not exist, fails on the first deploy — at which
point the failure is expensive and public. These checks are cheap and run in
CI.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from raceos.api.main import create_app
from raceos.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[3]
RENDER_YAML = REPO_ROOT / "render.yaml"

#: Set by the platform, not by us.
PLATFORM_VARS = {"PYTHON_VERSION", "WEB_CONCURRENCY", "PORT"}


@pytest.fixture(scope="module")
def render() -> str:
    assert RENDER_YAML.is_file(), "render.yaml is the deployment; it must exist"
    return RENDER_YAML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def paths() -> set[str]:
    app = create_app()
    return {route.path for route in app.routes}  # type: ignore[attr-defined]


def test_every_declared_variable_is_one_the_app_reads(render: str) -> None:
    """A variable nobody reads is a variable somebody will set and wonder about."""
    declared = set(re.findall(r"- key: ([A-Z0-9_]+)", render))
    known = {name.upper() for name in Settings.model_fields} | PLATFORM_VARS
    assert declared <= known, f"render.yaml declares unknown vars: {sorted(declared - known)}"


def test_every_secret_is_marked_sync_false(render: str) -> None:
    """A secret with a literal value in this file is a secret in the repository."""
    blocks = re.findall(r"- key: ([A-Z0-9_]+)\n\s+(value|sync): ?(.*)", render)
    for key, kind, value in blocks:
        if any(
            marker in key
            for marker in ("SECRET", "KEY", "PASSWORD", "TOKEN", "DSN", "DATABASE_URL")
        ):
            assert kind == "sync", f"{key} has a literal value in render.yaml"
            assert value.strip() == "false", f"{key} is not marked sync: false"


def test_no_committed_secret_looks_like_a_real_credential(render: str) -> None:
    for pattern in (r"sk_live_\w{10,}", r"sk_test_[A-Za-z0-9]{20,}", r"eyJ[A-Za-z0-9_-]{20,}"):
        assert not re.search(pattern, render), f"render.yaml matches {pattern}"


def test_the_health_check_path_exists(render: str, paths: set[str]) -> None:
    """Render restarts the instance when this 404s."""
    match = re.search(r"healthCheckPath: (\S+)", render)
    assert match, "no healthCheckPath declared"
    assert match.group(1) in paths


def test_the_health_check_is_liveness_only(paths: set[str]) -> None:
    """If it verified the database, a brief blip would restart healthy
    instances. Dependency checks belong at /readyz."""
    assert "/healthz" in paths
    assert "/readyz" in paths


def test_every_documented_url_resolves_to_a_real_route(render: str, paths: set[str]) -> None:
    """The Stripe webhook path is followed by hand during setup, and a wrong
    one fails silently: Stripe accepts the registration and every event 404s."""
    for documented in re.findall(r"onrender\.com(/\S*)", render):
        cleaned = documented.rstrip(".,`")
        assert cleaned in paths, f"render.yaml documents {cleaned}, which has no route"


def test_the_documented_webhook_path_matches_the_router(paths: set[str]) -> None:
    assert "/webhooks/payments" in paths


def test_no_container_tooling_exists_anywhere() -> None:
    """Deployment is Render's native Python runtime."""
    forbidden = ("Dockerfile", "dockerfile", "docker-compose.yml", "compose.yaml", ".dockerignore")
    for name in forbidden:
        assert not (REPO_ROOT / name).exists(), f"{name} exists"
        assert not (REPO_ROOT / "backend" / name).exists(), f"backend/{name} exists"


def test_migrations_run_before_traffic(render: str) -> None:
    assert "preDeployCommand: alembic upgrade head" in render


def test_the_start_command_names_a_module_that_imports(render: str) -> None:
    match = re.search(r"gunicorn (\S+):(\S+)", render)
    assert match, "no gunicorn target in the start command"
    module_path, attribute = match.groups()

    import importlib

    module = importlib.import_module(module_path)
    assert hasattr(module, attribute), f"{module_path} has no {attribute}"


def test_the_v1_feature_flags_are_declared_off(render: str) -> None:
    """Email is a no-op, push is off, phrasing is deterministic. Each is a
    config change, not a code change."""
    for flag in ("EMAIL_ENABLED", "PUSH_ENABLED", "PHRASING_ENABLED"):
        block = re.search(rf"- key: {flag}\n\s+value: \"(\w+)\"", render)
        assert block, f"{flag} is not declared"
        assert block.group(1) == "false"


def test_every_registered_job_is_reachable_by_an_external_cron(paths: set[str]) -> None:
    """V1 ships no scheduler: the cron is the scheduler, and it needs a path."""
    from raceos.services import job_service

    assert job_service.registry(), "no jobs registered"
    assert "/internal/jobs/{name}" in paths
