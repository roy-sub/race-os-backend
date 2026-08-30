#!/usr/bin/env python3
"""Validate a ``.env`` against what the application actually requires.

``make check-env``. Run it before a deploy and after editing ``.env``: it
constructs the real :class:`~raceos.config.Settings` object under the target
environment, so it catches exactly what a boot would catch — and nothing it
misses would have been caught at boot either.

It prints no values, only names and verdicts, so its output is safe to share.

    python scripts/check_env.py                 # check as configured
    python scripts/check_env.py --env production  # check production readiness
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

ENV_EXAMPLE = REPO_ROOT / ".env.example"
ENV_FILE = REPO_ROOT / ".env"


def _documented_names() -> list[str]:
    names: list[str] = []
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith(("#", " ")) and "=" in line:
            names.append(line.split("=", 1)[0])
    return names


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env",
        choices=["development", "staging", "production"],
        default=None,
        help="Environment to validate against. Defaults to APP_ENV.",
    )
    args = parser.parse_args()

    from raceos.config import SECRET_FIELD_NAMES, Settings  # noqa: PLC0415

    print("RaceOS — environment check")
    print("=" * 62)

    if not ENV_FILE.is_file():
        print(f"WARN  No .env at {ENV_FILE.relative_to(REPO_ROOT)}.")
        print("      Checking the ambient environment only. To create one:")
        print("        cp .env.example .env")
        print()

    if args.env:
        os.environ["APP_ENV"] = args.env

    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 - this script reports, never raises
        print("FAIL  Configuration is not valid:")
        for line in str(exc).splitlines():
            print(f"      {line}")
        print()
        print("      Every variable and its shape is documented in .env.example.")
        return 1

    documented = set(_documented_names())
    fields = {name.upper() for name in Settings.model_fields}

    print(f"PASS  Configuration is valid for APP_ENV={settings.app_env.value}.")
    print()
    print(f"  variables read by the app : {len(fields)}")
    print(f"  documented in .env.example: {len(documented)}")

    undocumented = sorted(fields - documented)
    if undocumented:
        print(f"  FAIL  not documented    : {undocumented}")
        return 1

    unread = sorted(documented - fields)
    if unread:
        print(f"  FAIL  documented but unread: {unread}")
        return 1

    print()
    print("  Secrets (values never printed):")
    for name in sorted(SECRET_FIELD_NAMES):
        state = settings.redacted_dump()[name]
        marker = "set  " if state == "<set>" else "unset"
        print(f"    {marker}  {name.upper()}")

    print()
    print("  Feature flags:")
    for flag in (
        "email_enabled",
        "push_enabled",
        "phrasing_enabled",
        "require_email_verification",
        "rate_limit_enabled",
    ):
        print(f"    {str(getattr(settings, flag)).lower():5}  {flag.upper()}")

    print()
    print("=" * 62)
    print("PASS  Ready. Next: `make check-db` to verify the database and PostGIS.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
