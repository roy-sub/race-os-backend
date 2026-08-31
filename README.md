# RaceOS — backend

The deterministic triathlon race-planning service. A solver produces every
number; the API serves them with their provenance attached; nothing is
recalculated behind an athlete's back.

Three laws shape the whole codebase, and each is enforced by a test rather
than by a convention:

1. **The solver decides, the model phrases.** Every number comes from
   deterministic code. A language model may only rewrite an already-correct
   sentence, and a rewrite that introduces a number is rejected.
2. **Provenance travels forever.** Every constraint and every course fact
   carries where it came from, from input to printed race card.
3. **Drift, never silent recalculation.** When the world moves, the plan is
   re-solved against a throwaway copy and the athlete is shown what *would*
   change. Their plan is untouched until they apply it.

---

## Getting started

Prerequisites: Python 3.11, and a local PostgreSQL 16 with PostGIS.

```bash
make install                       # virtualenv and dependencies
createdb raceos && createdb raceos_test
cp .env.example .env               # then fill in the local values
make check-env                     # validates .env against the required set
make migrate                       # apply migrations
make seed                          # courses, people, plans, history
make dev                           # http://127.0.0.1:8000
```

`make seed` prints a set of freshly generated development sign-ins to your
terminal. They are different on every run, are never written to a file, and
there is no default password anywhere in this repository.

Interactive API docs: `http://127.0.0.1:8000/api/v1/docs`.

### Generating local secrets

Nothing in this repository contains a credential. For local development:

```bash
openssl genrsa -out /tmp/raceos-dev.pem 2048              # JWT_PRIVATE_KEY
openssl rsa -in /tmp/raceos-dev.pem -pubout               # JWT_PUBLIC_KEY
openssl rand -hex 32                                      # SESSION_COOKIE_SECRET
openssl rand -hex 32                                      # INTERNAL_JOB_SECRET
```

Stripe and Supabase are optional locally. Without a Stripe key the in-memory
payment gateway is used; without a Supabase key, in-memory object storage.
Both are real working implementations, not stubs, and the whole test suite
runs against them with no credential at all.

---

## Layout

```
backend/raceos/
├─ api/           routers, schemas, error taxonomy, serialisation
├─ solver/        the deterministic model — six stages, no I/O of any kind
├─ domain/        enums and the entitlement decision table
├─ services/      everything between the API and the database
├─ db/            SQLAlchemy models, session, seed
├─ ingest/        course-bundle loading, race-file decoding
├─ exports/       PDF, FIT, GPX, ICS
├─ payments/      gateway interface + Stripe + in-memory
└─ storage/       object-store interface + Supabase + in-memory
```

The solver imports nothing from `api`, `db`, or `services`, reads no clock and
no random source, and a purity test enforces it. That is what makes the golden
suite meaningful: identical input produces byte-identical output, on any
machine, forever.

---

## Testing

```bash
make test              # everything
make test-unit         # no database required
make test-golden       # the solver golden suite — a diff blocks deploy
make lint              # ruff
make typecheck         # mypy, strict on solver/ and domain/
```

The suite runs **fully offline with no credentials**. Every external service
sits behind an interface with a working in-memory implementation, and a real
RS256 keypair is generated per test session in memory. A test that needed a
real key would be a broken test.

### The structural guarantees

Three properties are guaranteed by construction, and each has a test that
attempts the forbidden action through every path that exists:

| Guarantee | Where it is tested |
|---|---|
| Nothing but the athlete can write a constraint — not a coach at full permission, not an admin, not a background job | `tests/integration/test_coach.py`, `test_constraints.py` |
| No share scope exposes a constraint value or account data, including `full_plan` | `tests/integration/test_coach.py` |
| The language model cannot influence a number | `tests/unit/test_phrasing_boundary.py` |

The second of those found a real leak during development: bag items carried
the "Why this?" reason, and *"Swim leg planned at 1:56/100m"* is a constraint
value written out in prose. The share allow-list now runs field by field
inside every block.

---

## The API

116 routes. `GET /api/v1/docs` is the live reference; the shape is:

| Area | Routes |
|---|---|
| Auth and sessions | 9 |
| Constraints and estimators | 4 |
| Courses, bundles, free recon and cut-off calculator | 6 |
| Races (enter, list, edit) | 5 |
| Plans, solving, versions | 10 |
| Drift | 4 |
| Exports | 6 |
| Billing, entitlements, invoices | 7 |
| Dashboard, My Plans, notifications, push | 10 |
| Post-race and calibration | 6 |
| Coach | 14 |
| Sharing | 4 |
| Race Mode | 1 |
| Admin and ops | 13 |
| Support access | 5 |
| Internal jobs | 3 |
| Health, docs, webhook | 6 |

---

## Scheduled jobs

V1 ships **no Redis and no Celery**. Every background job is a service
function exposed at `/internal/jobs/{name}` behind a shared-secret header, and
an external cron calls it. Every run is recorded — including failures, because
a job that failed silently is indistinguishable from one the cron never
called.

`GET /internal/jobs` returns this table live, with each job's last run:

| Job | Cadence | What it does |
|---|---|---|
| `drift-sweep` | `0 */6 * * *` | Shadow-recompute plans inside the forecast horizon |
| `kpi-snapshot` | `20 2 * * *` | Aggregate yesterday's KPIs from real rows |
| `service-health` | `*/15 * * * *` | Probe each dependency |
| `expire-support-grants` | `*/10 * * * *` | Close grants past their hour |
| `expire-share-links` | `5 * * * *` | Retire lapsed links |
| `race-status-rollover` | `0 5 * * *` | Complete yesterday's races |
| `notification-digest` | `0 8 * * 1` | Weekly digest, where there is something to say |
| `media-asset-audit` | `0 4 * * 1` | Check every course image still resolves |
| `purge-expired-tokens` | `15 3 * * *` | Delete spent credentials |
| `purge-idempotency-keys` | `45 3 * * *` | Drop records past TTL |
| `purge-rate-limit-counters` | `*/30 * * * *` | Drop rolled-past windows |
| `purge-forecast-cache` | `30 * * * *` | Drop expired forecasts |
| `purge-cache-entries` | `50 * * * *` | Drop expired cache rows |

Call one with:

```bash
curl -X POST https://<service>/internal/jobs/drift-sweep \
     -H "X-Internal-Job-Secret: $INTERNAL_JOB_SECRET"
```

---

## Deployment

Render's native Python runtime. **No Docker anywhere** — not for production,
not for local development, not for tests, and a test asserts no container
tooling exists.

`render.yaml` is the deployment. Migrations run as `preDeployCommand`, so the
schema is current before the new instance takes traffic. Every secret is
`sync: false` and set in the dashboard; a test asserts that no secret has a
literal value in the file, that every declared variable is one the application
actually reads, and that the health-check and webhook paths resolve to real
routes.

Before the first deploy, run `python scripts/check_supabase.py` against the
real `DATABASE_URL`. It reports which connection form works and whether
PostGIS is enabled. Both the direct URL and the Supavisor pooler URL work with
no code change: the connection layer detects the form and, on the
transaction-mode pooler port, disables prepared statements and uses a
`NullPool`.

---

## Documentation

| File | What it holds |
|---|---|
| `SOLVER_MODEL.md` | The model, its constants, and every worked example |
| `docs/DECISIONS.md` | Every judgement call, with one line of reasoning |
| `docs/CONFIGURATION.md` | Every environment variable and where to get it |
| `docs/LAUNCH_BLOCKERS.md` | What is knowingly incomplete for V1 |
| `docs/FIELD_NAME_RECONCILIATION.md` | Where storage names differ from the mock's |
| `docs/DEFINITION_OF_DONE.md` | Each requirement, with the evidence for it |

---

## What V1 deliberately does not do

Email delivery is a no-op that renders and stores the message; push is off;
phrasing uses deterministic templates. Each is a configuration flag, not a
code change. There are no device integrations by design — RaceOS writes a
`.fit` course file the athlete side-loads, which is why the export exists and
why no provider SDK is installed. A boundary test asserts none ever is.
