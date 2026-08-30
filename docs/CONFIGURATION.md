# Configuration — where each value comes from

`.env.example` is the source of truth for *what* every variable is and what
breaks without it, and it stands alone. This document says *where to get* each
value. If the two ever disagree, `.env.example` wins.

Only five sources exist. **Do not introduce a dependency on any other
third-party service, key or account** — not for development, not behind a flag.

| Source | Provides | Account needed |
|---|---|---|
| **Supabase** | Postgres + PostGIS, object storage | yes |
| **Stripe** (test mode) | Payments | yes |
| **Open-Meteo** | Weather forecast | **no — no key exists** |
| **Mapterhorn** (hosted) | Terrain PMTiles for the map | **no — a URL only** |
| **Locally generated** | JWT keypair, session secret, job secret, VAPID pair | no |

---

## 1 · Supabase

Dashboard → your project.

| Variable | Where |
|---|---|
| `SUPABASE_URL` | Settings → API → Project URL |
| `SUPABASE_SECRET_KEY` | Settings → API → service-role / secret key |
| `SUPABASE_PUBLISHABLE_KEY` | Settings → API → anon / publishable key |
| `DATABASE_URL` | Settings → Database → Connection string |

### The secret key is full administrative access

It bypasses row-level security entirely. Treat it as a root password: never
commit it, never log it, never paste it into a chat transcript. It is
server-side only. The publishable key is the safe one, and the backend does
not use it — it is carried so a single `.env` can also configure the
frontend's storage reads.

### Which connection string

Supabase offers two, and both work here with **no code change** — the
connection layer detects the form and adapts:

```
direct   postgresql+psycopg://postgres:PASSWORD@db.PROJECT_REF.supabase.co:5432/postgres
pooler   postgresql+psycopg://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:6543/postgres
```

**The direct host may resolve to IPv6 only, and Render's outbound network may
not have IPv6.** If the direct form fails from Render, the pooler form is the
fix, and it is a `DATABASE_URL` edit.

Port 6543 on the pooler is *transaction* mode: a connection is returned to the
pool after every transaction, so server-side prepared statements cannot
survive. `raceos/db/session.py` detects that port and disables them, and uses
`NullPool` because Supavisor is already the pool. Port 5432 on the pooler is
*session* mode and behaves like a direct connection.

Prefix normalisation is automatic: paste the dashboard's `postgresql://…` and
it becomes `postgresql+psycopg://…`.

**Run this before deploying:**

```bash
python scripts/check_supabase.py
```

It reports the detected form, whether the host is IPv6-only, whether the
connection works, and whether PostGIS, `citext` and `pgcrypto` are enabled. It
prints no credentials, so its output is safe to share.

### PostGIS must be enabled first

If the check reports PostGIS missing, run this in **Dashboard → SQL Editor →
New query**, then re-run the check:

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
```

Every geometry column in the schema depends on PostGIS; `citext` gives
case-insensitive email uniqueness; `pgcrypto` provides `gen_random_uuid()`.

### Buckets

Create two, under Storage → Buckets:

| Bucket | Public? | Holds |
|---|---|---|
| `raceos-private` | **no** | uploads, generated exports, race files |
| `raceos-public` | yes | `assets/` media only |

Nothing athlete-specific goes in the public bucket. Private objects are
reached only through short-lived signed URLs.

---

## 2 · Stripe (test mode)

Dashboard, with the **Test mode** toggle on.

| Variable | Where |
|---|---|
| `STRIPE_SECRET_KEY` | Developers → API keys → Secret key (`sk_test_…`) |
| `STRIPE_PUBLISHABLE_KEY` | Developers → API keys → Publishable key (`pk_test_…`) |
| `STRIPE_WEBHOOK_SECRET` | Developers → Webhooks → your endpoint → Signing secret (`whsec_…`) |
| `STRIPE_PRICE_ID_*` | Products → each product → Pricing → the price's API ID (`price_…`) |

Create three products with these prices (Build Spec Part 4.6):

| Product | GBP | USD | EUR | Variable |
|---|---|---|---|---|
| Race Plan | 15 | 19 | 18 | `STRIPE_PRICE_ID_RACE_PLAN` |
| Season Pass | 47 | 59 | 55 | `STRIPE_PRICE_ID_SEASON_PASS` |
| Coach | 79 | 99 | 92 | `STRIPE_PRICE_ID_COACH` |

Stripe is the source of truth for prices; the catalogue in the database
mirrors them for display only.

**The webhook secret comes last.** You cannot obtain it until the service has
a URL. Deploy first, register the endpoint against
`https://<service>.onrender.com/webhooks/payments`, then set the secret
Stripe shows you and redeploy.

---

## 3 · Open-Meteo — no key

`OPEN_METEO_BASE_URL` is `https://api.open-meteo.com/v1` and that is the whole
configuration. **There is deliberately no `WEATHER_PROVIDER_API_KEY`.** If an
implementation ever needs one, that implementation is wrong.

---

## 4 · Mapterhorn — no key

`TERRAIN_PMTILES_BASE_URL` is a URL handed to the frontend so MapLibre can
stream Terrarium-encoded PMTiles. Hosted; no account, no key, no self-hosting
in V1.

Note that `pipelines/course-ingest` sampled elevation from **AWS Terrain
Tiles**, not Mapterhorn, because Mapterhorn was unreachable from that build
environment. The Terrarium encoding is identical, so the frontend consumes
either without change. See `pipelines/course-ingest/docs/DATA_SOURCES.md`.

---

## 5 · Generated locally

Nothing to fetch. Generate these yourself; the exact commands are also in
`.env.example` directly above each variable.

### JWT RS256 keypair

```bash
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out jwt_private.pem
openssl rsa -in jwt_private.pem -pubout -out jwt_public.pem
```

A PEM block is multi-line and a `.env` file is line-oriented, which is the
single most common thing to get wrong. Convert each to one line with escaped
newlines and paste it between double quotes:

```bash
awk 'BEGIN{ORS="\\n"} 1' jwt_private.pem
awk 'BEGIN{ORS="\\n"} 1' jwt_public.pem
```

```
JWT_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\nMIIEvQ...\n-----END PRIVATE KEY-----\n"
```

In Render's dashboard you may instead paste the real multi-line key into the
multi-line editor. Both forms are accepted and normalised; anything that is
not a PEM block is rejected at boot with a message naming the field, rather
than failing later inside a signature check.

**Use a different keypair in production than in development.** Rotating the
pair invalidates every existing token.

### Session cookie secret and internal job secret

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

Run it twice — these must not be the same value. Rotating
`SESSION_COOKIE_SECRET` signs every user out. `INTERNAL_JOB_SECRET` is the
only thing standing between the public internet and every background job.

### VAPID keypair (push, disabled in V1)

```bash
vapid --gen                    # writes private_key.pem / public_key.pem
vapid --applicationServerKey   # prints the base64url public key
```

The `vapid` CLI ships with `pywebpush`, already a dependency. Only needed if
`PUSH_ENABLED` is turned on.

---

## What is deliberately absent

Each of these would mean a deferred or excluded feature had been built. Their
absence is asserted by `backend/tests/unit/test_config_env_example.py`.

| Not present | Why |
|---|---|
| `REDIS_URL`, `CELERY_BROKER_URL` | no Redis, no Celery; caching, rate limits, idempotency and jobs are database-backed |
| `SENTRY_DSN` | error reporting is structured stdout logging behind an `ErrorReporter` interface |
| `WEATHER_PROVIDER_API_KEY` | Open-Meteo needs no key |
| `OBJECT_STORAGE_*` | storage is Supabase; there is no separate object-storage account |
| `SUPABASE_JWKS_URL` | Supabase Auth is not used; we issue our own RS256 tokens |
| `OAUTH_GOOGLE_*`, `OAUTH_APPLE_*` | no social login; `GET /auth/providers` returns an empty list |
| `CONNECTION_TOKEN_ENCRYPTION_KEY`, provider keys | no third-party athlete-data integrations, permanently (Build Spec Part 0.4 C1) |

`EMAIL_PROVIDER_API_KEY` and `PHRASING_MODEL_API_KEY` *are* present, marked
`# OPTIONAL — V2` and unset. Both subsystems are fully built behind flags that
are off, so enabling either later is filling in a blank rather than writing
code. Neither is contacted while its flag is false.

---

## Checking your work

```bash
make check-env    # every required variable present and valid for APP_ENV
make check-db     # connection form, PostGIS, required extensions
```

`check-env` constructs the real settings object, so it catches exactly what a
boot would catch. Neither script prints a value.
