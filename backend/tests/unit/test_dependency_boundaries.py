"""Things this build deliberately does not contain.

The V1 brief defers or permanently excludes a number of components, and each
exclusion is easy to undo by accident — an import added for convenience, a
package pulled in as somebody's transitive dependency, a "just for dev"
placeholder endpoint. A comment saying "we don't use Redis" does not stop any
of that; a failing test does.

So each exclusion is asserted here, against the installed environment and
against the source tree, with the reason it exists.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = BACKEND_ROOT / "raceos"

#: Packages whose presence would mean a deferred component was built.
FORBIDDEN_PACKAGES = {
    "redis": "V1 has no Redis: caching, rate limits and idempotency are database-backed",
    "celery": "V1 has no Celery: jobs are service methods behind /internal/jobs/{name}",
    "kombu": "transitive of Celery; its presence means a broker was introduced",
    "flower": "Celery monitoring; there is no Celery",
    "sentry_sdk": "error reporting is structured stdout logging behind ErrorReporter",
    "supabase": "we use Supabase for Postgres and Storage only, never its auth SDK",
    "gotrue": "Supabase Auth client; RaceOS issues its own RS256 tokens",
    "stravalib": "there are no third-party athlete-data integrations (Part 0.4 C1)",
    "garminconnect": "there are no third-party athlete-data integrations",
}


@pytest.mark.parametrize(("package", "reason"), sorted(FORBIDDEN_PACKAGES.items()))
def test_forbidden_package_is_not_installed(package: str, reason: str) -> None:
    assert (
        importlib.util.find_spec(package) is None
    ), f"{package!r} is installed but must not be: {reason}."


def _requirement_names(path: Path) -> set[str]:
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "-")):
            continue
        names.add(re.split(r"[\[=<>;\s]", line, maxsplit=1)[0].lower())
    return names


def test_requirements_declare_no_forbidden_package() -> None:
    declared = _requirement_names(BACKEND_ROOT / "requirements.txt") | _requirement_names(
        BACKEND_ROOT / "requirements-dev.txt"
    )
    overlap = declared & {p.replace("_", "-") for p in FORBIDDEN_PACKAGES}
    assert not overlap, f"requirements declare forbidden package(s): {sorted(overlap)}"


def _python_sources() -> list[Path]:
    return sorted(SOURCE_ROOT.rglob("*.py"))


def test_no_module_imports_a_forbidden_package() -> None:
    """A source-level check, in case a package is vendored rather than installed."""
    pattern = re.compile(
        r"^\s*(?:import|from)\s+(" + "|".join(sorted(FORBIDDEN_PACKAGES)) + r")\b",
        re.MULTILINE,
    )
    offenders: list[str] = []
    for path in _python_sources():
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(BACKEND_ROOT)))
    assert not offenders, f"these modules import a forbidden package: {offenders}"


def test_no_integration_or_connection_module_exists() -> None:
    """Part 0.4 C1: deleted, not deferred.

    No `connections` table, no provider adapter, no OAuth token custody. The
    check is on names because that is how such a thing would reappear — as an
    `integrations/` package somebody adds "to be ready".
    """
    forbidden_dirs = ["integrations", "connections", "providers"]
    present = [d for d in forbidden_dirs if (SOURCE_ROOT / d).exists()]
    assert not present, (
        f"these packages must not exist: {present}. Third-party athlete-data "
        f"integrations are permanently out of scope (Build Spec Part 0.4 C1)."
    )


def test_no_source_file_references_a_device_provider_in_code() -> None:
    """No provider adapter — no import, no identifier, no endpoint string.

    The check is on **code**, not on prose, and that distinction was earned:
    an earlier version scanned raw text and flagged
    `solver/tables/intensity.py`, which cites TrainingPeaks as a *published
    coaching source* for the contested 70.3 intensity band. Citing a coaching
    publication is not building an integration, and a guard that cannot tell
    those apart would push authors to stop citing their sources — the opposite
    of what this codebase wants.

    The same distinction applies to a second case: ``exports/files.py`` names
    Garmin and Wahoo in the instructions shown beside a ``.fit`` download.
    Those exist *because* there is no integration — writing a file the athlete
    side-loads is what replaced one — so a string literal only counts as an
    integration when it is network-shaped: a URL, a hostname, an OAuth
    endpoint or a credential field. Naming a brand in a sentence is not.

    So: imports, identifiers and network-shaped string literals are checked;
    comments, docstrings and prose are not.
    """
    providers = ("garmin", "strava", "wahoo", "trainingpeaks", "intervals_icu")
    #: What makes a string a reference to a provider's *service* rather than
    #: to the brand: something a request could be built from, or a credential.
    network_shaped = re.compile(
        r"https?://|www\.|\.com\b|\.net\b|\bapi\.|/oauth|client_id|client_secret",
        re.IGNORECASE,
    )
    offenders: list[str] = []

    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = {
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef)
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            found: str | None = None
            if isinstance(node, ast.Import):
                found = next(
                    (a.name for a in node.names if any(p in a.name.lower() for p in providers)),
                    None,
                )
            elif isinstance(node, ast.ImportFrom) and node.module:
                if any(p in node.module.lower() for p in providers):
                    found = node.module
            elif isinstance(node, ast.Name):
                if any(p in node.id.lower() for p in providers):
                    found = node.id
            elif isinstance(node, ast.Attribute):
                if any(p in node.attr.lower() for p in providers):
                    found = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node in docstrings:
                    continue
                if any(p in node.value.lower() for p in providers) and network_shaped.search(
                    node.value
                ):
                    found = node.value[:60]
            if found:
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno}: {found}")

    assert not offenders, f"device-integration references found in code: {offenders}"


def test_the_device_provider_guard_still_catches_a_real_integration() -> None:
    """The narrowing above must not have defanged the guard.

    Two ways an integration actually arrives — an import and a base URL — are
    fed to the same predicates the scan uses, and both must still be caught.
    """
    providers = ("garmin", "strava", "wahoo", "trainingpeaks", "intervals_icu")
    network_shaped = re.compile(
        r"https?://|www\.|\.com\b|\.net\b|\bapi\.|/oauth|client_id|client_secret",
        re.IGNORECASE,
    )

    caught: list[str] = []
    source = (
        "import garminconnect\n"
        "BASE = 'https://connectapi.garmin.com/oauth-service/token'\n"
        "PROSE = 'Copy the file into the Garmin folder on your device.'\n"
    )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            caught += [a.name for a in node.names if any(p in a.name.lower() for p in providers)]
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and any(p in node.value.lower() for p in providers)
            and network_shaped.search(node.value)
        ):
            caught.append(node.value)

    assert "garminconnect" in caught, "an import of a provider SDK must still be caught"
    assert any(
        value.startswith("https://") for value in caught
    ), "a provider endpoint must still be caught"
    assert not any(
        value.startswith("Copy the file") for value in caught
    ), "instructions that name a brand are not an integration"


def test_no_dockerfile_or_compose_file_exists() -> None:
    """Deployment is Render's native Python runtime; no container tooling."""
    repo_root = BACKEND_ROOT.parent
    forbidden = [
        "Dockerfile",
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        ".dockerignore",
    ]
    found: list[str] = []
    for name in forbidden:
        found.extend(
            str(p.relative_to(repo_root))
            for p in repo_root.rglob(name)
            if ".venv" not in p.parts and ".git" not in p.parts
        )
    assert not found, (
        f"container tooling found: {found}. V1 deploys as a plain Python service "
        f"on Render's native runtime; no Docker, not even for local development."
    )


def test_no_placeholder_markers_in_source() -> None:
    """No TODO, no `pass  # later`, no NotImplementedError in shipped code.

    Abstract methods are the one legitimate use of a bare body, and they are
    marked with @abstractmethod rather than raising, so this can be strict.
    """
    markers = re.compile(r"\bTODO\b|\bFIXME\b|\bXXX\b|raise NotImplementedError")
    offenders: list[str] = []
    for path in _python_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if markers.search(line):
                offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{number}: {line.strip()}")
    assert not offenders, "placeholder markers found:\n" + "\n".join(offenders)
