# Definition of done — RaceOS V1 backend

Each line of the brief, with the evidence for it. Where something is *not*
done, it says so and points at `LAUNCH_BLOCKERS.md`.

Run `make test` to reproduce every claim below.

---

## The three laws

| Law | How it is enforced | Evidence |
|---|---|---|
| The solver decides every number; the LLM only phrases | Rewrites validated on numeric tokens — any number not already in the input is rejected; one entry point, enforced by a source scan; solver package may not mention phrasing | `tests/unit/test_phrasing_boundary.py` (22 tests) |
| Provenance travels forever | `ConstraintSource` on every constraint, `Provenance` on every course fact, both carried into `plan_constraint_refs`, the PDF footer and the Race Mode payload | `test_constraints.py`, `test_exports.py::test_every_pdf_carries_the_provenance_footer` |
| Drift, never silent recalculation | Shadow recompute on a throwaway input; the plan is untouched until applied; applying makes a new version | `test_drift.py::test_a_constraint_change_produces_deltas_without_touching_the_plan` |

## The three structural guarantees

Each is tested by attempting the forbidden action through **every path that
exists**, not by asserting a flag.

| Guarantee | Paths attempted | Evidence |
|---|---|---|
| Only the athlete writes a constraint | Coach at full permission via their own constraint endpoints; every `coach/athletes/{id}/constraints` shape; the permission name itself; the calibration path with a coach as actor; a source-level assertion that the coach router and service never reference `write_constraint` | `test_coach.py::test_no_coach_endpoint_can_read_or_write_a_constraint` and four companions; `test_postrace.py::test_calibration_goes_through_the_constraint_write_guard` |
| No share scope exposes a constraint or account data | All four scopes against the real response body, at every depth | `test_coach.py::test_no_share_scope_exposes_a_constraint_or_account_data` |
| The LLM cannot influence a number | Six adversarial rewrites — changed, rounded, added, hedged, false-precision, derived-from-athlete-data | `test_phrasing_boundary.py` |

**Found by these tests, not by review:** the share allow-list leaked constraint
values through bag-item reason text (`"Swim leg planned at 1:56/100m"`) and
fuelling binding keys. The allow-list now runs field by field inside every
block. Recorded as D-084.

---

## Hard rules

| Rule | Evidence |
|---|---|
| No placeholders — no `TODO`, `pass`, `NotImplementedError`, mock returns | `test_dependency_boundaries.py::test_no_placeholder_markers_in_source` |
| No invented field names | `docs/FIELD_NAME_RECONCILIATION.md` records each of the 3 deliberate divergences and why |
| No magic numbers | Every constant is named, sourced and commented in `solver/tables/` |
| Solver purity — no db, network, `datetime.now()`, `random`, `uuid4()` | `test_solver_purity.py` |
| Every endpoint has an authorization test | Each integration file ends with a parametrised `…rejects_an_absent_token`; role-scoped surfaces additionally test the wrong role |
| Every migration reversible, expand-contract | `test_schema_constraints.py` runs `downgrade base` and asserts no orphaned enum types |
| `mypy --strict` on `solver/` and `domain/` | `make typecheck` — clean on 120 files |
| No Docker | `test_deployment_config.py::test_no_container_tooling_exists_anywhere` |
| No Redis, no Celery | `test_dependency_boundaries.py::test_forbidden_package_is_not_installed` |
| No deferred features built early | No device-integration module exists; a boundary test asserts no provider SDK is installed or referenced in code |

---

## Milestones

| # | Scope | State |
|---|---|---|
| 1 | Repo, config, logging, health | Done |
| 2 | Schema, PostGIS, reversible migration | Done |
| 3 | Course bundles loaded and served | Done — 3 of 9 courses; six specs are `status: pending` and deliberately not fabricated (`LAUNCH_BLOCKERS.md` #1) |
| 4 | The solver, golden suite | Done — 15 golden cases frozen; P50 1,348 ms against a 3,100 ms target |
| 5 | Auth, constraints, plans, versioning | Done |
| 6 | Checkout, two-phase capture, entitlements | Done |
| 7 | Exports — race card, bags, FIT, GPX, ICS | Done |
| 8 | Dashboard, My Plans, notifications | Done |
| 9 | Drift, bundle cascade, internal jobs | Done |
| 10 | Post-race, analysis, calibration | Done |
| 11 | Coach domain, share links | Done |
| 12 | Admin/ops, Race Mode, phrasing boundary | Done |
| — | Seed data, deployment, docs | Done |

---

## Deliverables

| Item | Evidence |
|---|---|
| 116 API routes | `GET /api/v1/docs`; the README's table |
| 13 scheduled jobs behind one secret | `GET /internal/jobs`; `test_admin.py::test_thirteen_jobs_are_registered` |
| Deterministic solver, six stages | `tests/golden/` — 15 frozen expectations |
| Seed data, idempotent | `make seed`; `test_seed.py::test_running_the_seed_twice_changes_nothing` |
| Deployment config verified against the code | `test_deployment_config.py` (12 tests) |
| No secret anywhere in the repository | `test_dependency_boundaries.py`, `test_seed.py::test_the_seed_module_contains_no_literal_password`; seed passwords are generated per run and printed once |
| Every decision recorded | `docs/DECISIONS.md` — 110 entries, D-001 to D-110 |

### The seed, specifically

`make seed` produces, on an empty database:

- 3 courses from the real generated bundles — no fabricated geometry
- 13 people: Elena Marsh and Jonas Feldt from the product copy, 9 more
  athletes across every tier, level and currency, plus an ops and a support
  account
- 16 plans **produced by the real solver**, in every status — active, draft
  and past
- 4 pending drift events, each caused by a genuine constraint change the
  solver genuinely disagrees with
- invoices in GBP, USD and EUR
- 3 coach links at three different permission levels, 3 share links
- a crowd report with 8 independent uploaders, 2 incidents
- 31 days of KPI snapshots, aggregated by the same function the nightly job
  uses

Re-running changes nothing: no new rows, and no plan walks to a new version.

---

## Known gaps

Recorded in full in `docs/LAUNCH_BLOCKERS.md`. The material ones:

1. **Six of nine courses are not built.** Their specs are marked
   `status: pending` in the pipeline. Fabricating geometry would put a course
   in the directory that does not match the road.
2. **G08-FIRSTTIMER is infeasible**, so it never reaches Stage 6. The inputs
   are specified in `SOLVER_MODEL.md` §B and were not retuned to make a case
   pass.
3. **Email delivery is a no-op** and push is off. Both render and store the
   message; flipping either is a config change.
4. **Media assets are absent.** The audit job reports every missing key.
5. **Two normative-vs-illustrative divergences** in `SOLVER_MODEL.md`
   (D-027 per-segment air density, D-028 run histogram integration), together
   0.02% on the flagship example, documented rather than silently resolved.

## One thing still owed by you

`python scripts/check_supabase.py`, run against the real Supabase project, to
confirm which connection form works. The connection layer handles both the
direct URL and the Supavisor pooler with no code change — on the
transaction-mode port it disables prepared statements and uses a `NullPool` —
but which one your project needs cannot be determined without a credential,
and no credential belongs in this session.
