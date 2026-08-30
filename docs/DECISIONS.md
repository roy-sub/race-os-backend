# Decisions

Every call made without asking, with the reasoning. A future session reading
this should understand *why*, not just *what*.

Format: what was decided, why, and what would reverse it. Newest last within
each milestone.

---

## Milestone 1 — Foundation

### D-001 · Python 3.11, not the specification's 3.12

`RaceOS_Build_Spec.md` Part 2 pins Python 3.12. The build environment has
3.11.15, and the entire test suite is proven against that interpreter.

Declaring 3.12 on Render while testing on 3.11 would mean deploying an
untested interpreter, which is the wrong risk to take for no gain — nothing
in this codebase uses a 3.12-only feature. `requires-python = ">=3.11"` and
Render is pinned to 3.11 to match.

*Reverses when:* the suite is run green on 3.12. It is then a one-line change
in `render.yaml`.

### D-002 · `backend/` holds the Python project; `.env.example` and `Makefile` sit at the repository root

The build spec's layout puts the package under `backend/`, and the golden
fixtures are already at `backend/tests/golden/`. But the brief requires
**one** `.env.example` at the repository root, because that is what the
operator copies without reading anything else first.

So configuration and developer entry points are at the root, and the
installable project is under `backend/`. Render sets `rootDir: backend`; the
settings object finds `.env` by walking up from its own location, so both
work.

### D-003 · Connection form is detected, not configured

Supabase offers a direct connection and a pooler, and we do not yet know which
Render can reach. Rather than a `DATABASE_MODE` variable the operator must set
correctly, `raceos/db/session.py` classifies the URL and adapts: transaction
mode pooling (port 6543) disables server-side prepared statements and uses
`NullPool`, because a pooled connection is handed to another client between
transactions and a prepared statement does not survive that.

The result is that switching forms is genuinely a `DATABASE_URL` edit, which
is what the brief asked for. `scripts/check_supabase.py` reports which form is
in use and whether it resolved to IPv6 only.

*Reverses when:* nothing foreseeable. If a third form appears, it is one more
branch in `describe_connection`.

### D-004 · A connection string contributes only its password to the redaction literals

Redacting the whole `DATABASE_URL` as one literal would turn every connection
failure into `could not connect to [REDACTED]`, which is useless exactly when
it is needed. Redacting nothing leaks the password.

So the URL contributes its password component as a literal, and a shape
pattern masks `://user:pass@host` in place. The host, port, database and user
survive; the password does not, wherever it appears.

### D-005 · The storage backend is chosen by configuration, not by environment name

`get_storage_backend()` returns the Supabase backend when a Supabase secret
key is present and the in-memory one otherwise. Choosing on `APP_ENV` instead
would mean a production boot with a missing key silently falling back to
in-memory storage and losing every upload.

That cannot happen here: a staging or production boot without the key is
already refused by `Settings`, so the fallback is only ever reachable in
development and test — which is what makes the suite run offline with no
credentials.

### D-006 · `InMemoryStorage` is a real implementation, not a mock

It enforces the same not-found semantics and the same public/private
separation as the Supabase backend, and its signed URLs carry real expiries.
A test that passes against a mock tests the mock; a test that passes against
this tests the caller.

### D-007 · `PHRASING_MODEL_API_KEY` and `EMAIL_PROVIDER_API_KEY` exist as `# OPTIONAL — V2`, unset

The brief permits the email adapter's variables to be documented but unset, so
enabling it later is filling in blanks. The phrasing adapter is in exactly the
same position — built, flag-gated off, no account in the allowlist — so it
gets the same treatment.

Neither introduces a dependency: both code paths are unreachable while their
flag is false, and `Settings` requires the key only when the flag is turned on.

*Reverses when:* an LLM or email provider is actually procured, at which point
these stop being V2 placeholders and become required.

### D-008 · `/healthz` deliberately checks nothing external

Render's health check polls it. If it verified the database, a brief database
blip would make Render kill and restart otherwise-healthy instances, turning a
recoverable outage into a worse one. Dependency checking belongs in `/readyz`,
which is for humans and deploy gating.

### D-009 · PostGIS absent counts as *not ready*, not as *ready with a warning*

Every geometry column in the schema depends on it. A process that starts
without it will fail on the first course query, with an error far less clear
than "PostGIS extension is not enabled on this database".

### D-010 · Warning codes cannot be raised as errors

`STALE_DATA` and `PARTIAL_DATA` ride alongside a 200 in a `warnings` array.
`WarningCollector.add` rejects a non-warning code, and the HTTP mapping gives
warning codes a 500 so that raising one as an error is loud rather than
returning a plausible-looking 4xx.

### D-011 · Local Postgres uses `trust` authentication

The development and test databases accept local connections without a
password, so no credential — not even a throwaway one — appears in a committed
file, a test fixture, or a `Makefile` default. The alternative was inventing a
local password and then having to keep it out of the repository.

*Applies to:* the build container and a developer's machine only. It has no
bearing on any deployed environment.

---

## Milestone 2 — Schema

### D-012 · Two distance vocabularies, one configured mapping

`DistanceType` (`Sprint|Olympic|70.3|Full`) is a native database enum and is
what the frontend sends. `SolverDistance` (`full|half|olympic|sprint`) is not
a database enum at all — it lives in the solver's tables. `DISTANCE_TO_SOLVER`
in `domain/enums.py` is the only mapping.

Keeping the solver's vocabulary out of the schema is what stops the two being
conflated by a later reader, and `70.3` is not a valid Python identifier in
the first place. A test asserts `solver_distance` is not among the database's
enum types.

### D-013 · Native enums store *values*, not member names

`pg_enum()` passes `values_callable`, so the label in PostgreSQL is `70.3`,
not `HALF`. Without it SQLAlchemy stores member names, which would silently
diverge from both the frontend and `SOLVER_MODEL.md`, and nothing else would
catch it. Asserted directly by a test on `pg_enum` labels.

### D-014 · `updated_at` is maintained by a trigger, using `now()`

A trigger rather than an ORM default, so a row touched by a migration, a
backfill or a hand-run `UPDATE` is still stamped. `now()` is the *transaction*
timestamp, not `clock_timestamp()`: rows changed together in one transaction
changed at one moment, and an audit trail that says otherwise is harder to
reason about. The consequence is that a single-transaction test cannot observe
the trigger, so its test commits twice.

### D-015 · Every NOT NULL column with a default carries a *server* default

52 columns had ORM-side defaults only, which meant any writer that is not the
ORM — a migration backfill, a seed SQL script, a `psql` session — hit a NOT
NULL violation. Found by a test doing a raw insert. The schema is now correct
for any writer, not only for SQLAlchemy.

### D-016 · The downgrade drops enum types explicitly

Autogenerate emits `CREATE TYPE` implicitly (as a side effect of the first
column using an enum) but never the matching `DROP TYPE`. Without an explicit
list, `downgrade base` left 40 orphaned types and the next `upgrade` failed
with `type "distance_type" already exists`. Reversibility is a requirement, so
the migration carries the list and the drops.

### D-017 · Bundle invariants are database constraints, not loader checks

Zero barriers, a non-`terrain` elevation source, and empty attribution are all
CHECK constraints. `SOLVER_MODEL.md` §1.2 would reject such a bundle at *solve*
time — which is hours later and lands on an athlete rather than on the admin
who published it.

### D-018 · Five tables exist because V1 has no Redis or Celery

`rate_limit_counters`, `cache_entries`, `job_runs`, `email_messages` and
`push_subscriptions`. `idempotency_keys` was already in the specification;
only its backing store changed.

Rate-limit counters are rows in the shared database rather than in process
memory, so they stay correct if a second web instance is ever added. The V1
note that they are "per-instance-safe because there is one instance" is
therefore conservative: the mechanism is already instance-independent.

### D-019 · Alembic must not disable existing loggers

`fileConfig` defaults to `disable_existing_loggers=True`, which switched off
the application's own loggers in any process that had run a migration first.
Caught by the startup-log test failing only in a full-suite run. Passing
`False` is the fix; it would matter in production too, wherever migrations and
the app share a process.

### D-020 · The test database is built by running migrations, never `create_all()`

Those two can drift, and if they do it is the migration that is wrong — so the
migration is what the suite exercises. The fixture also recovers from a
database stamped with a revision that no longer exists, which is the ordinary
state after a migration is rewritten during development.

---

## Milestone 3 — Courses and bundles

### D-021 · A logger adapter renames reserved `extra` keys

`logging` raises `KeyError: "Attempt to overwrite 'created' in LogRecord"`
when an `extra` key shadows a built-in attribute, and the colliding names are
ordinary words a caller reaches for: `created`, `name`, `module`, `message`,
`filename`. The failure is at emit time, so it turns a diagnostic line into an
exception on a path that was working — which is how it was found, in the seed
script.

`SafeExtraLogger` renames a colliding key with a trailing underscore rather
than dropping it, so the value still reaches the log and the collision cannot
be tripped over again.

### D-022 · The bundle loader re-validates what the pipeline already asserts

The pipeline is trusted and its 33 tests pass, but a fixture can be
hand-edited between generation and load, and the loader is the last point at
which a bad bundle is cheap. Rejection at load time costs an admin a minute;
the same bundle rejected at solve time costs an athlete their plan, hours
later, with an error that names a barrier rather than the bundle.

Validation reports *every* problem rather than the first, because an admin
fixing one problem at a time is an admin fixing it four times.

### D-023 · The directory falls back to the newest draft bundle

`_active_bundle` prefers `published` and falls back to the newest `draft`. The
seeded bundles arrive from the pipeline as drafts, honestly, and hiding them
until an admin publishes would leave the directory empty for no good reason.

*Reverses when:* the publish workflow ships in milestone 9 and the seed can
publish. The fallback then becomes dead weight rather than wrong.

### D-024 · `cutoff_minutes` is derived, never stored

The frontend's `cutoff: "10:30 bike"` is rendered from the bundle's real
barriers. Storing the string would let a bundle's cut-off move and leave a
stale label behind — on the one number the product's differentiator depends on.

### D-025 · Course routes resolve by uuid *or* slug

The frontend links by slug and the API returns uuids. Accepting either costs
one `try/except` and removes a class of "which id is this?" bugs from the
integration session.

### D-026 · The local database needs restarting when the session resumes

`pg_ctlcluster 16 main start`. The container stops Postgres between turns, and
the data directory survives, so this is a restart rather than a rebuild. Noted
because a suite that skips every integration test looks like a passing suite.

---

## Milestone 4 — The solver

### D-027 · Air density is per segment; `alt_factor` uses the leg mean

§I.1.6 is explicit: *"Elevation used is the mean elevation of the leg, not
per-segment... Air density, by contrast, IS per-segment, because that is a
property of the air and not of the athlete."* Implemented exactly that.

§4.2.4's worked example computes one ρ at 120 m — the *leg* mean — and applies
it to three segments whose own mean elevations differ substantially. That is an
illustrative simplification in the worked total, not the normative rule, so the
normative rule wins. It costs about 0.26% on the Tramuntana bike split
(405.07 min against the document's 406.11).

### D-028 · Run time is integrated over the gradient histogram, like the bike

§1.1 says Stage 1 emits a histogram per segment and *"Stage 4 integrates time
over it"*, and §4.3.1 gives `pace_i = pace_target × D_grade(g_i)` per segment.
Applying `D_grade` over the histogram is the reading consistent with both, and
it is the one that avoids the Jensen error the histogram exists to prevent.

§4.3.4's worked total (`42.195 × 384.737 / 60 = 270.57`) applies no gradient
modulation at all, which is again the illustration simplifying rather than the
model specifying. The difference is 0.45% on C-TRAM's near-flat run.

Combined effect of D-027 and D-028 on the flagship worked example: 762.95 min
against the document's 762.8 — **0.02%**.

### D-029 · Levers iterate a fixed tuple, never a dict

Ranking ties must break deterministically. `compute_levers` walks an explicit
tuple of the four lever-eligible keys and sorts on `(delta, name)`, so two
levers with identical effect always come out in the same order.

### D-030 · Floats are frozen as their `repr`, not as JSON numbers

`repr` is the shortest string that round-trips a float exactly, so the golden
comparison is exact and independent of any reader's float formatting. Storing
them as JSON numbers would put the diff at the mercy of a parser.

### D-031 · `stage_timings_ms` is excluded from the frozen output

It is a real measurement, so it differs every run and on every machine.
Freezing it would make the golden suite fail for reasons unrelated to
correctness — the exact "flaky test nobody trusts" outcome the determinism
guarantee exists to avoid. The solver still emits it; a test asserts both.

### D-032 · The hot loop is inlined, with evaluation order preserved exactly

`solve_speed` runs 60 fixed bisection iterations, and one Stage 3 grid over the
real Tramuntana bundle executes it ~110,000 times. The original body
recomputed `atan`/`cos`/`sin` of the gradient on every iteration although they
are constant per histogram bin, and called through two function layers.

Hoisting them and inlining the balance took a full solve from **3,791 ms to
1,348 ms — 2.8x** — with the golden files **byte-identical**, which is the only
acceptable proof that the grouping preserved the arithmetic. The bisection is
unchanged: same fixed iteration count, same bracket, same branch.

### D-033 · Solver errors live in `solver/errors.py`

The API imports the solver, never the reverse. A solver that imported its own
exception types from `raceos.api` could not be called from a CLI, a job or a
test harness without dragging FastAPI in behind it, and the build spec requires
it to be "callable in-process and from workers". `raceos.api.errors`
re-exports them and maps them onto the taxonomy at the boundary.

### D-034 · `adapters.py` is the solver's only I/O boundary

It reads a course file and returns a frozen snapshot; nothing it returns
depends on when or where it ran. The purity lint allows file reads there and
nowhere else in the package, and mypy's explicit-`Any` ban is lifted only
there, because it parses JSON whose shape the type system cannot know.
Everything downstream of it is `Any`-free.

## Milestone 7 — exports

- **D-029 Exports are generated per request, never stored.** A cached PDF is a
  copy of a plan that can go stale silently, which is exactly what Law 3
  forbids. Regeneration costs a few hundred milliseconds and deletes the whole
  class of problem. `plans.race_card_pdf_url` stays in the schema for a future
  storage-backed artefact but is not written by V1.
- **D-030 A course file is per leg, defaulting to `BIKE`.** A head unit follows
  one route; concatenating swim, bike and run into a single course produces a
  track that teleports at each transition. `SWIM` is not offered.
- **D-031 GPX carries the route only; waypoints go in the FIT file.** Part 12.2.
  GPX waypoint support across head units is inconsistent enough that emitting
  them would produce different behaviour per device from one file.
- **D-032 The FIT encoder is written directly rather than pulled from an SDK.**
  A course file needs four message types and a documented binary layout. Doing
  it in-tree keeps the byte layout and the CRC — which head units do check —
  visible and under test, and adds no dependency.
- **D-033 Race-week calendar dates are derived from the event date.** The mock's
  "Thursday: bike check-in" is a rendering of "one day before"; storing weekday
  names would be wrong for any race not held on a Sunday.
- **D-034 Plan serialisation moved to `raceos/api/serialise.py`.** The JSON the
  app renders and the PDF the athlete prints must be the same numbers formatted
  once. Two formatting paths would be two chances to disagree, and a
  disagreement would read as a solver bug.
- **D-035 The device-provider boundary guard now only flags network-shaped
  string literals.** Naming Garmin in the sentence "copy the file into
  /Garmin/NewFiles" is not an integration — it exists *because* there is no
  integration. A literal counts only when a request could be built from it (a
  URL, a hostname, an OAuth path) or it is a credential field. A companion test
  feeds the guard a real SDK import and a real endpoint to prove the narrowing
  did not defang it.
- **D-036 Exports require a solved plan and return 409 on a draft.** The plan
  exists and the athlete may read it; it is the state that makes the request
  wrong, so the UI can say "solve first" rather than "not found".

## Milestone 6 — checkout and entitlements

- **D-037 Entitlements are a pure decision over a table** (`raceos/domain/entitlements.py`),
  with the service layer gathering facts and the domain deciding. Every row of
  the published pricing matrix has an entry, so a claim on the pricing page and
  the server's answer cannot disagree.
- **D-038 A captured purchase is permanent and race-scoped.** It is joined
  through `plans` rather than stored on the purchase, because a race carries
  many plan versions and a purchase against version 1 must still cover version 4
  after three re-solves. It survives cancellation and downgrade: a plan you paid
  for stays yours.
- **D-039 An open authorization grants the solve it is paying for, and nothing
  else.** Capture is triggered by solve success, so at the moment a first solve
  begins there is by definition nothing captured. Requiring a capture would mean
  charging before the athlete had the plan. A hold is not a payment, so it does
  not unlock exports, drift re-solves or Race Mode — a separate `Grant` value
  makes that distinction explicit rather than special-casing an action.
- **D-040 `PAYMENT_REQUIRED` (402) is distinct from `FORBIDDEN` (403).** The
  caller is who they say they are and may hold the resource; they have not paid
  for this action. The UI shows an upgrade path for one and an apology for the
  other, so collapsing them produces the wrong screen. The 402 body carries
  `required_tiers` and `purchasable_per_race`, so the client can offer the
  cheapest correct upgrade instead of a generic paywall.
- **D-041 The void happens after a commit on the solve-failure path.** No
  database error has occurred, so the session is sound, and the commit persists
  the solve-timing measurement. That row is telemetry rather than plan state,
  and discarding it would bias the P95 by dropping exactly the solves that took
  longest to fail. The plan itself is still never persisted on failure.
- **D-042 Stripe over `httpx` rather than the `stripe` SDK.** Four calls are
  needed. The SDK brings a large dependency, its own retry and logging
  behaviour, and a habit of reading `STRIPE_API_KEY` from the environment on
  import — and reading configuration only from `Settings` is a rule here.
  The API version is pinned so an upgrade is deliberate.
- **D-043 `InMemoryPaymentGateway` is a working gateway, not a mock.** It
  enforces the same state machine — capture once, or void once, never both —
  and implements the real webhook signature scheme, so the rejection path is
  exercised offline. The whole billing suite runs with no credential.
- **D-044 The webhook is unauthenticated by design: the signature is the
  authentication.** Nothing is read from the body before the signature verifies
  against the raw bytes, comparison is constant-time, and a timestamp outside
  the five-minute tolerance is refused so yesterday's "payment succeeded"
  cannot be replayed today. An event type we do not handle is acknowledged,
  never rejected — a 400 makes the provider retry it forever.
- **D-045 Refund reasons stay ours.** `race_cancelled` and `bundle_error` are
  operational paths; Stripe accepts three reason strings. Ours is stored on the
  `refunds` row and sent as provider metadata rather than squeezed into their
  vocabulary.

## Milestone 8 — dashboard, My Plans, notifications

- **D-046 The dashboard is assembled in one request.** Its cards must agree
  with each other; a "next race" derived from a different read than the season
  list is how a screen ends up showing two different next races.
- **D-047 An unsolved race reports nulls, never a plausible figure.** The
  athlete cannot distinguish an invented projection from a real one, so an
  honest gap is better than a confident guess.
- **D-048 The drift summary is derived from the stored deltas.** A summary
  written alongside the numbers could contradict them; one built from them
  cannot.
- **D-049 `notify()` is the single delivery entry point.** The preference
  matrix, the critical floor and the race window are applied in exactly one
  place, because a second delivery path is a second place to forget them.
- **D-050 Turning in-app off for a critical type is clamped, not rejected.**
  The response returns the clamped value and an `inapp_locked` flag, so the
  settings screen tells the truth about what the system will do rather than
  arguing with the request. The floor is re-asserted at delivery too, so a row
  written around the endpoint still cannot silence a cut-off warning.
- **D-051 The quiet window is scoped to the race it concerns.** A drift alert
  for August is still worth sending while the athlete is racing in June; only
  the race they are standing in goes quiet. The window runs from
  `RACE_NOTIFICATION_SUPPRESSION_BUFFER_HOURS` before the local start to 24
  hours after it — a full-distance athlete can still be on course seventeen
  hours in. Open windows are surfaced on the dashboard, because an athlete who
  notices the alerts have stopped deserves to know it is deliberate.
- **D-052 Push defaults to off.** It needs an explicit browser grant, so
  defaulting it on would promise a delivery that silently never happens.

## Milestone 9 — drift and the bundle publish cascade

- **D-053 Drift detection is a real shadow solve, not an estimate.** Guessing
  what a 3.4 °C forecast move does to a bike target is exactly the shortcut
  that reports the wrong number. The recompute runs the full solver on a
  throwaway copy of the input and diffs the result against the stored rows.
- **D-054 The plan's frozen forecast is overridden only on the copy.**
  `plan_service.replace_forecast` returns a new `SolveInput`; the plan keeps
  the forecast it was solved against until the athlete applies the drift.
- **D-055 Only material changes are reported.** A split moving under
  `DRIFT_SPLIT_THRESHOLD_MINUTES`, or fuelling under the per-field thresholds,
  is real but not actionable — and an alert an athlete cannot act on trains
  them to ignore the ones they can.
- **D-056 Severity is decided on the recomputed margin, not on how far it
  moved.** A margin that was always thin and stayed thin is still the thing
  worth shouting about.
- **D-057 Sensitivity governs notification, never detection.** The event is
  recorded whatever the athlete's setting, so the plan page can always show
  what moved even when they asked not to be told.
- **D-058 A second pending event for the same plan and cause replaces the
  first.** Two alerts saying the forecast moved are one alert with newer
  numbers.
- **D-059 A dismissed event is kept, not deleted.** The record that the
  athlete was told and chose is the difference between an informed decision
  and a surprise on race morning.
- **D-060 Publishing supersedes the bundle the course is actually served
  from, not merely a published predecessor.** Pipeline bundles arrive as
  `draft` and races get pinned to them; a publish that only looked for a
  published predecessor would leave that draft winning the reader's fallback
  forever, and would produce an empty diff telling athletes the course changed
  while naming nothing. Found by a test; `current_active()` now mirrors
  `course_service._active_bundle`.
- **D-061 The cascade re-pins the race and shadow-recomputes per plan.**
  Athletes are told what changed *for them*, not that "the course changed" —
  which for most of them means nothing. Where nothing material moved, no alert
  is manufactured. The plan keeps every number it was solved with.
- **D-062 Blast radius answers during a freeze.** The refusal belongs on the
  publish, not on the read-only question "how bad is this?".
- **D-063 The freeze override requires a written reason.** A bundle correcting
  a *wrong* cut-off is more dangerous to withhold than to publish, but the
  audit entry has to say why athletes were interrupted in race week.
- **D-064 Jobs are a registry, not a scheduler.** Each is a service function
  behind one shared-secret guard, with every run recorded — failures included,
  because a job that failed silently is indistinguishable from one the cron
  never called. `/internal/jobs` is excluded from the public schema.
- **D-065 Expired session rows are kept for a grace period.** Refresh-token
  reuse detection works by finding an already-rotated row; delete it on expiry
  and a stolen token becomes indistinguishable from an unknown one.

## Milestone 10 — post-race, analysis and calibration

- **D-066 The FIT reader is written in-tree, like the writer.** Three message
  types carry everything an analysis needs; a dependency would be a large one
  whose failure modes we would still have to handle. The reader is tested
  against the writer, which is the strongest round-trip available without a
  device.
- **D-067 Content decides the format, not the extension.** A `.fit` that is
  actually XML is a mislabelled export, and saying so is more useful than
  reporting a corrupt FIT file.
- **D-068 An absent channel is `None`, never zero.** Zero is a real power
  reading and a real heart rate; conflating them is how an analysis reports
  that an athlete coasted a climb.
- **D-069 GPX distance is integrated from the coordinates.** Most GPX exports
  carry no distance channel, and deriving it is what makes a plain GPX
  comparable at all. TCX's declared channel is preferred where present.
- **D-070 §2.5.3 step 4 is implemented as a real refusal.** No qualifying
  20–90 minute effort under 5% pace CV means **no value is derived** and the
  reason is recorded. A `measured` stamp on a value derived from a twelve-
  minute interval upgrades provenance while degrading the number, which is the
  worst combination this product can produce.
- **D-071 The effort search is linear, not quadratic.** A moving end index
  over a six-hour file at 1 Hz stays tractable; testing every pair would not.
- **D-072 `plan_version` is stored on the analysis, not joined.** A plan
  re-solved after the race would otherwise silently judge the athlete against
  something they never raced.
- **D-073 Calibration is applied one proposal at a time**, through
  `constraint_service.write_constraint` — so structural guarantee 1 holds on
  this path too: not even an internal caller can write a constraint on
  someone's behalf. A bulk "apply all" would let one bad derivation ride in
  behind four good ones.
- **D-074 A failed upload is committed before the error is raised.** The
  endpoint's error handler rolls back, and a flushed-but-uncommitted row would
  vanish — leaving support with nothing to look at when an athlete says it
  would not take their file. No database error has occurred, so only the
  failure record is committed.
- **D-075 The object key is random, never the filename.** The uploaded name is
  untrusted input; putting it in the key would let an athlete choose where
  their file lands. The original name is stored separately for display.

## Milestone 11 — coach domain and share links

- **D-076 A pending invite occupies a seat.** Otherwise a coach at their limit
  could hold fifteen athletes plus an unbounded queue of invitations, every one
  of which would exceed the limit the moment it was accepted.
- **D-077 An accepted invite grants nothing.** Permissions start empty and the
  athlete chooses what to share. Acceptance links the accounts; it does not
  share anything.
- **D-078 A forwarded invite tells the wrong person nothing.** A valid token
  presented by anyone other than the named athlete returns the same 404 as an
  unknown one.
- **D-079 `require_permission` raises `ValueError` on an unknown permission
  name.** A caller passing `"constraints"` gets a programming error naming why,
  not a `getattr` that quietly returns `False`. Structural guarantee 1 is
  reinforced in three independent places: no column, no schema field, no
  vocabulary entry — and a test asserts the coach router and service never even
  reference `write_constraint`.
- **D-080 A coach-built plan is stamped before the solve, not after.** The
  persist step reads `built_by_coach_id` to decide whether the new version
  lands `pending_athlete_approval` or `active`; stamping afterwards would put a
  plan live in the athlete's account without them ever seeing it.
- **D-081 Notes are escaped on write, not on render.** There are several
  renderers — the app, the PDF, an email — and one writer. Escaping at the
  writer makes the stored value inert everywhere.
- **D-082 An athlete who has not granted access still appears on the board**,
  with their numbers withheld and a reason. Hiding them would make a coach
  think the invite failed.
- **D-083 Compare is built from the board, not a second query**, so the two
  views cannot disagree about a margin.
- **D-084 The share allow-list runs field by field inside each block.** A
  top-level allow-list was not enough, and a test proved it: bag items carry
  `reason_constraint_key` and `reason_text`, and a reason reading "Swim leg
  planned at 1:56/100m" *is* a constraint value written out in prose. Fuelling
  carries `binding_*_key`, which names the constraint that bound each number.
  Neither survives into a share response at any scope, `full_plan` included.
  A recipient packing a bag needs the item and the quantity; the reasoning is
  the athlete's.
- **D-085 An unknown, a revoked and an expired share link are
  indistinguishable.** One message for all three, so a probe learns nothing
  about which links exist.
- **D-086 Share expiry is bounded at 180 days as well as being mandatory.** A
  coach reviewing race week does not need a year, and every extra day is
  exposure with nobody watching it.

## Milestone 12 — phrasing boundary, admin/ops, Race Mode

- **D-087 The phrasing layer validates on numeric tokens, not on meaning.**
  Every number in a rewrite must already appear in the input. Dropping a number
  is allowed — "your bike target" for "208 w" is a legitimate improvement —
  inventing or altering one is not. The scan is deliberately broad: better to
  reject a harmless rewrite than to let one number through.
- **D-088 There is exactly one entry point, and a test enforces it.** A second
  phrasing path would be a second place validation is skipped, so a test scans
  every module for a direct `rewrite(`/`get_model(` call. A companion test
  asserts the solver package never so much as mentions phrasing.
- **D-089 Phrasing failure is always a fallback, never a wrong number.**
  Disabled, timed out, provider error, failed validation — all four return the
  deterministic string. `PHRASING_ENABLED` defaults to off and the product is
  complete without it.
- **D-090 Solver percentiles use `percentile_disc`, not `percentile_cont`.**
  Every value it can return is a latency that actually happened, which makes
  "P95 is 5.4 s" a statement about a real request rather than an interpolation
  between two.
- **D-091 A KPI with no measurement reports `None`, never zero.** An operator
  who cannot tell "nobody solved anything" from "every solve was instant" will
  eventually act on the wrong one.
- **D-092 KPI snapshots are idempotent per date.** A cron that fires twice, or
  a backfill over a week that already has rows, must not produce two truths for
  one day.
- **D-093 Support-grant expiry is enforced on every read**, not trusted from a
  flag set at approval. A grant that lapses mid-session stops working at the
  next request rather than at the next sweep; the sweeper job is belt and
  braces.
- **D-094 The support access log is athlete-visible.** Transparency, not
  internal audit — the athlete sees every screen an agent opened, appended at
  the moment of access by the same function that authorises it.
- **D-095 Crowd confidence counts distinct uploaders, not uploads.** One
  athlete uploading forty times is one observation; the unique index makes that
  true in the data and the query counts what the index guarantees.
- **D-096 A promoted crowd finding never becomes `OFFICIAL`.** Agreement across
  forty athletes is strong evidence and still not the organiser's word, so the
  provenance label stays honest.
- **D-097 Service health is written by probes only.** Each is the cheapest call
  that would genuinely fail if the dependency were down; a hand-set "nominal"
  is worth nothing.
- **D-098 Race Mode is one request carrying everything**, including the "Why
  this?" content and any outstanding drift. On course with no signal is exactly
  when an athlete wants to know why a number is what it is — and better to
  learn at check-in that a change is unapplied than at kilometre ninety.
- **D-099 The digest job sends nothing when there is nothing to say.** An empty
  weekly notification is how people learn to filter the ones that matter.
- **D-100 Race rollover waits a day.** A full-distance athlete can still be on
  course at local midnight, and several timezones run behind UTC.

## Close-out — seed, push, recon, deployment

- **D-101 Every seeded plan is produced by the real solver.** A fixture with
  hand-written splits would let a solver regression pass a demo, which is
  exactly backwards. `_solve` is idempotent, because each solve inserts a new
  version and re-running would quietly walk a seeded plan to v4.
- **D-102 Seed passwords are generated per run and printed once.** Never
  written to a file, never logged — a structured log ships somewhere, and a
  credential in a log is a credential in a log aggregator. There is no default
  password anywhere in this repository.
- **D-103 The seed's drift events come from a real constraint change.** A seed
  run has no forecast provider, so a forecast recompute correctly finds nothing.
  Rather than fabricate a drift row to make the dashboard look right, the
  athlete's threshold is genuinely moved and the solver genuinely disagrees.
- **D-104 A first-timer's seeded constraints are stamped `estimated`.**
  Provenance is the product's spine; a seed that stamped everything `tested`
  would hide the very thing the UI exists to show.
- **D-105 Push is built and disabled.** Subscriptions are stored, the
  preference matrix is honoured, failures are counted and dead endpoints
  pruned — everything except the network call is real, so enabling it is a
  keypair and a flag. Delivery reports honestly rather than pretending.
- **D-106 A push endpoint is never echoed back.** It is a capability URL:
  anyone holding it can push to that browser. Only the host is returned, and
  only the host is logged.
- **D-107 Re-subscribing a browser re-points the existing row.** A shared
  browser is a real situation, and leaving the endpoint attached to the
  previous account would send them somebody else's race.
- **D-108 Course recon and the cut-off calculator are free and
  unauthenticated.** The course library is the front door; a cut-off
  calculator behind a paywall makes the product impossible to evaluate. No
  athlete data is involved, and a test asserts the response contains none.
- **D-109 The cut-off calculator says in every row that it is not a solve.**
  An estimate that looks like a plan is worse than no estimate.
- **D-110 `render.yaml` is verified against the running app by a test.** It
  found a real bug: the deployment notes pointed Stripe at
  `/api/v1/webhooks/payments`, which has no route. Stripe accepts a wrong
  registration and every event 404s silently, so this would have surfaced as
  "payments stopped working" days later.
