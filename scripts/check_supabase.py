#!/usr/bin/env python3
"""Verify a Supabase connection before deploying against it.

Run this locally with your real ``DATABASE_URL`` set. It answers the three
questions the build cannot answer for itself, because none of them can be
determined without a real project:

1. **Which connection form works?** Supabase's direct host
   (``db.<ref>.supabase.co``) may resolve to IPv6 only, and Render's outbound
   network may not have IPv6. If the direct form fails, the pooler form
   (``aws-0-<region>.pooler.supabase.com``) is the fix — and it is a
   ``DATABASE_URL`` edit, not a code change.
2. **Is PostGIS enabled?** Every geometry column in the schema needs it. If it
   is not, this prints the exact SQL to run in the Supabase SQL editor.
3. **Are the other required extensions present?** ``citext`` for
   case-insensitive email uniqueness, ``pgcrypto`` for ``gen_random_uuid()``.

It prints no credentials: hosts, ports and versions only, so its output is
safe to paste back into a conversation.

    python scripts/check_supabase.py
    python scripts/check_supabase.py --url "postgresql://postgres:...@host:5432/postgres"

Exit status is 0 when the database is usable, 1 when it is not.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

TICK = "PASS"
CROSS = "FAIL"
WARN = "WARN"

POSTGIS_SQL = """\
-- Run this in the Supabase SQL editor (Dashboard -> SQL Editor -> New query),
-- then re-run this script.
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
"""


def _load_env_file() -> None:
    """Read the repository-root .env, if present, without extra dependencies."""
    env_path = REPO_ROOT / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def _resolve(host: str, port: int) -> tuple[list[str], list[str]]:
    """Return (ipv4_addresses, ipv6_addresses) for *host*."""
    ipv4: list[str] = []
    ipv6: list[str] = []
    try:
        for family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(host, port):
            if family == socket.AF_INET:
                ipv4.append(str(sockaddr[0]))
            elif family == socket.AF_INET6:
                ipv6.append(str(sockaddr[0]))
    except socket.gaierror:
        pass
    return sorted(set(ipv4)), sorted(set(ipv6))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=None,
        help="Connection string to test. Defaults to DATABASE_URL from the environment or .env.",
    )
    args = parser.parse_args()

    _load_env_file()
    raw_url = args.url or os.environ.get("DATABASE_URL", "")
    if not raw_url:
        print(f"{CROSS}  No connection string.")
        print("      Set DATABASE_URL in .env, or pass --url. See .env.example.")
        return 1

    from raceos.db.session import (  # noqa: PLC0415 - after sys.path setup
        ConnectionForm,
        check_database,
        create_db_engine,
        describe_connection,
        normalise_database_url,
    )
    from raceos.config import Settings  # noqa: PLC0415

    url = normalise_database_url(raw_url)
    description = describe_connection(url)

    print("RaceOS — Supabase connection check")
    print("=" * 62)
    print(f"  form      : {description.form.value}")
    print(f"  host      : {description.host}")
    print(f"  port      : {description.port}")
    print(f"  database  : {description.database}")
    print(f"  pooling   : {description.pooling}")
    print(f"  prepared statements: {'enabled' if description.prepared_statements_enabled else 'disabled (transaction pooling)'}")
    print()

    # --- DNS, which is where the IPv6 problem shows itself ------------
    ipv4, ipv6 = _resolve(description.host, description.port)
    print("DNS")
    print(f"  IPv4: {', '.join(ipv4) if ipv4 else '(none)'}")
    print(f"  IPv6: {', '.join(ipv6) if ipv6 else '(none)'}")
    if not ipv4 and ipv6:
        print(f"  {WARN}  This host is IPv6-only.")
        print("        Render's outbound network may not reach it. If the connection")
        print("        below fails from Render but works here, switch DATABASE_URL to")
        print("        the pooler form:")
        print("          postgresql+psycopg://postgres.PROJECT_REF:PASSWORD"
              "@aws-0-REGION.pooler.supabase.com:6543/postgres")
    elif ipv4:
        print(f"  {TICK}  IPv4 available; reachable from Render's network.")
    print()

    # --- connect ------------------------------------------------------
    settings = Settings(_env_file=None, database_url=raw_url)  # type: ignore[call-arg,arg-type]
    try:
        engine = create_db_engine(settings)
        info = check_database(engine)
    except Exception as exc:  # noqa: BLE001 - this script reports, never raises
        print(f"{CROSS}  Could not connect: {type(exc).__name__}")
        detail = str(exc)
        password = urlsplit(url).password
        if password:
            detail = detail.replace(password, "[REDACTED]")
        print(f"      {detail.splitlines()[0][:300]}")
        print()
        if description.form is ConnectionForm.SUPABASE_DIRECT:
            print("      The direct host failed. Try the pooler URL — it is IPv4 and")
            print("      needs no code change, only a different DATABASE_URL:")
            print("        postgresql+psycopg://postgres.PROJECT_REF:PASSWORD"
                  "@aws-0-REGION.pooler.supabase.com:6543/postgres")
        return 1

    print("CONNECTION")
    print(f"  {TICK}  Connected. PostgreSQL {info['server_version']}")
    print()

    print("EXTENSIONS")
    ok = True
    if info["postgis_enabled"]:
        print(f"  {TICK}  postgis {info['postgis_version']}")
    else:
        ok = False
        print(f"  {CROSS}  postgis is NOT enabled.")
        print("        Every geometry column in the schema needs it, and the")
        print("        migration will fail without it. Run this and re-check:")
        print()
        for line in POSTGIS_SQL.strip().splitlines():
            print(f"          {line}")
        print()

    for required in ("citext", "pgcrypto"):
        if required in info["extensions"]:
            print(f"  {TICK}  {required}")
        else:
            ok = False
            print(f"  {CROSS}  {required} is NOT enabled — "
                  f"run: CREATE EXTENSION IF NOT EXISTS {required};")

    print()
    print("=" * 62)
    if ok:
        print(f"{TICK}  Database is usable. Record this in docs/CONFIGURATION.md:")
        print(f"      working connection form = {description.form.value}")
    else:
        print(f"{CROSS}  Database is reachable but not yet usable. See above.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
