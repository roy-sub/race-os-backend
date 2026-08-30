# Field-name reconciliation

Every place a name, enum value or shape was resolved against **frontend
source** rather than against `BACKENDREQUIREMENTS.md`, so the integration
session can spot-check the reading rather than rediscover it.

**The hierarchy** (`RaceOS_Build_Spec.md` §0.1): this document → the build
spec → `BACKENDREQUIREMENTS.md` → the frontend source itself → media
requirements → UX spec. In practice the frontend source is authoritative for
field names and enum values, and `BACKENDREQUIREMENTS.md` is its transcription.

**The frontend checkout** is read-only, outside the backend tree, and never
committed: `/home/user/roy-sub/race-os-frontend` at `608fcde`. 23 screens
(`app/**/page.tsx`), 20 mock-data modules (`lib/*.ts`), 11 components.

**The governing rule**, from `BACKENDREQUIREMENTS.md` §4's own opening: *store
minutes as integers/floats for anything that feeds the solver, and format to
`H:MM` strings only at the API/view boundary.* So where a `lib/*.ts` literal
holds `"1:06"` and the column is named `split_minutes`, that is the rule
working — a display string and a storage name are different objects, and it is
not a conflict to be resolved.

---

## Resolved

### R-001 · `plan_splits.split_minutes`, not `split_time`

| Source | Says |
|---|---|
| `BACKENDREQUIREMENTS.md` §4.6 | `split_time` |
| `RaceOS_Build_Spec.md` Part 4.4 | `split_minutes` |
| `lib/sharedPlan.ts` `LEGS` | `split: "1:06"` |
| `lib/myPlans.ts` `CARD_BLOCKS` | `v: "1:44/100m · 1:06"` |

**Taken: `split_minutes` (numeric).**

The frontend value is a **formatted display string**, not a column. Storing
`"1:06"` would violate `BACKENDREQUIREMENTS.md`'s own §4 instruction, and the
solver produces minutes rounded to 0.1 (`SOLVER_MODEL.md` §0.4). The API
formats to `H:MM` at the boundary.

This is the hierarchy agreeing with itself, not an override of it.

### R-002 · `analysis_actions.description`, not `desc`

| Source | Says |
|---|---|
| `BACKENDREQUIREMENTS.md` §4.8 | `desc` |
| `RaceOS_Build_Spec.md` Part 4.5 | `description` |
| `lib/postRace.ts` `ACTIONS` | `{ n, gain, name, delay, desc, how }` |

**Taken: `description` for the column; the API serialises it as the frontend
expects.**

`desc` is mock-literal shorthand alongside `n`, `gain`, `how` and `delay` —
`delay: ".08s"` is a CSS animation delay, which settles that these keys are a
presentation object rather than a row. `DESC` is also a SQL reserved word.

Same reasoning applies to `analysis_actions.how_to` (frontend `how`) and
`analysis_compare_rows.drift_pct` (frontend `drift`, plus `dot` and `bold`,
which are colour and weight and are not persisted at all).

### R-003 · Two distance vocabularies, both kept

| Layer | Vocabulary | Source |
|---|---|---|
| Database, API, frontend | `Sprint` · `Olympic` · `70.3` · `Full` | `lib/raceDirectory.ts`, Build Spec 4.1 |
| Solver | `full` · `half` · `olympic` · `sprint` | `SOLVER_MODEL.md` §0.3 |
| Pipeline bundles | `Full` · `70.3` | `out/bundles/*.bundle.json` |
| Golden fixtures | `full` · `half` · `olympic` · `sprint` | `backend/tests/golden/courses/*.json` |

**Both kept, with one configured adapter holding the mapping** (`70.3 ↔ half`
being the only non-identity pair). Confirmed as a decision, not an accident:
normalising to one vocabulary would either put a solver term in front of users
or a marketing term inside the model's tables.

### R-004 · The nine course names are exactly the frontend's

`lib/raceDirectory.ts` carries all nine: Tramuntana Full, Kalmar 70.3, North
Shore Full, Cala Olympic, Roth Long Course, Skagen 70.3, Patagonia Full, Serra
Classic, Bergen Sprint. Three are generated; six specs are `status: pending`.
The directory will render three until those are generated.

### R-005 · `provenance` is stored `CROWD`, served `CROWD-VERIFIED`

| Source | Says |
|---|---|
| `RaceOS_Build_Spec.md` Part 4.1 (SQL DDL) | `provenance : OFFICIAL \| CROWD \| ESTIMATED` |
| `lib/raceDirectory.ts` | `type Provenance = "OFFICIAL" \| "CROWD-VERIFIED" \| "ESTIMATED"` |
| `BACKENDREQUIREMENTS.md` §4.4 | `provenance: OFFICIAL\|CROWD-VERIFIED` on aid stations |

**Taken: stored as `CROWD`, serialised as `CROWD-VERIFIED`.**

Unlike R-001 and R-002 this is a genuine value conflict, not display
shorthand — `CROWD-VERIFIED` is a real enum *value* in the frontend's union,
not a formatted string. Both sources are authoritative in their own layer: the
build spec gives explicit DDL and ranks first for storage; the frontend ranks
first for what it renders and will compare against the literal it declares.

So the value is translated at the API boundary by `PROVENANCE_DISPLAY` in
`api/schemas/course.py`, which is the one place the mapping exists. A test
asserts all three mappings.

*If the frontend is ever regenerated from the OpenAPI schema*, drop the
translation and let `CROWD` reach it — but that is a frontend change, not a
backend one, so it is not made unilaterally here.

---

## For the integration session

### I-001 · Three media assets have no documented fallback

Course art falls back to `courses.tone_color` and avatars to initials, both
specified. These three have neither, and all 41 assets are absent in V1:

| Path | Screen | Missing-state decision needed |
|---|---|---|
| `assets/hero/hero-loop.mp4` | Landing hero | Poster frame? Static image? Solid `tone_color` field? |
| `assets/authors/jonas.jpg` | Guide byline | Initials avatar, or omit the byline photo? |
| `assets/coach/feldt-mark.png` | Coach branding | Wordmark fallback, or omit the mark? |

The backend serves what is configured and returns a clean 404 otherwise; no
placeholder asset is invented. `MediaPlaceholder.tsx` keeps its fixed-container
behaviour, so nothing shifts either way — the question is what renders, not
whether the layout holds.

### I-002 · 19 literal asset paths in source, 41 in the catalogue

`grep` over `app/`, `components/` and `lib/` finds 19 literal `assets/…`
strings. The remaining 22 are templated against data files (course cards from
`raceDirectory.ts`, avatars from athlete records), which is how
`Media_Assets.md` reached 41 by resolving them. Consistent, not contradictory —
recorded so nobody re-derives it from the grep count alone.

### I-003 · 23 screens in source, "21 source screens" in the media doc

`app/**/page.tsx` yields 23. `Media_Assets.md` says its grep covered 21. The
likely explanation is that two screens carry no `data-media` usage, so they did
not appear in a media-oriented sweep. Not reconciled; flagged so the integration
session confirms no screen was missed rather than assuming the counts should
match.
