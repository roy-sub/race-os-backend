# Launch blockers

**This is the pre-commercial-launch checklist.** Everything below needs a
human before real money changes hands. Nothing here blocks the build, and
nothing here is a bug — these are things the code cannot settle for itself.

Ordered by consequence. Each entry states what is unverified, what it costs if
it is wrong, and precisely what closes it.

---

## 1 · E-1 — Stull wet-bulb coefficients — **CLOSED**

**Status:** verified, by reproduction rather than by transcription.

The publisher host is still unreachable, so checking six digits against
Stull (2011) *J. Appl. Meteor. Climatol.* 50:2267–2269 is still not available
and probably never will be from a build environment. That turned out not to be
the only route.

Stull's expression is an **empirical fit to the psychrometer equation**. That
equation — `e_s(T_w) − A·P·(T − T_w) = e_a` — can be solved numerically from
Magnus saturation vapour pressure with no reference to Stull whatsoever. If any
coefficient were wrong, the fit would stop reproducing the thing it is a fit to.

Measured across 5–40 °C and 30–100% RH, the conditions a triathlon is actually
held in:

| | |
|---|---|
| mean absolute difference | **0.24 °C** |
| worst case | **0.68 °C** |
| Stull's own stated fit error | < 0.3 °C mean |

Agreeing with the underlying physics to the order of the paper's own stated
error is the coefficients doing their job. The radians question — the single
most common implementation error with this formula — is settled the same way:
degrees puts the expression two orders of magnitude out.

The check is a test, not a note: `tests/unit/test_solver_environment.py`,
`test_stull_reproduces_the_psychrometer_equation_across_racing_conditions`. A
companion test shows that the most plausible transcription error in
`0.00391838` — a slipped decimal — would miss by 41 °C, so the 1.0 °C tolerance
is worth asserting.

**What this does not establish:** that the sixth digit of each coefficient is
the published one. It establishes that any error large enough to move a bike
target, a run pace or a cut-off margin is not present. A last-digit typo moves
the answer by under a thousandth of a degree, which nothing downstream can
notice.

---

## 2 · E-13 — The 70.3 intensity-factor band is contested, and we sit on one side of it by assumption

**Status:** `if_ref['half']` implemented at 0.72 / 0.78 / 0.83 per
`SOLVER_MODEL.md` §4.2.1.

**Consequence if wrong:** **≈ 8 minutes over 90 km on a primary distance.**
Allen & Coggan are cited at 0.83–0.87; TrainingPeaks spans 0.72–0.85; most
coaching sources put age-groupers at 0.75–0.80. The model takes the lower,
age-grouper-weighted figure on the reasoning that the higher band describes
athletes racing for a result rather than athletes trying to run well off the
bike. §4.2.1 calls that reasoning "plausible and unverified".

Eight minutes is a third of the entire 20-minute `clear`/`tight` margin band,
and the error propagates into the run through the over-biking term, so it
compounds rather than staying on the bike.

**What closes it:** read the stated 70.3 intensity-factor range in Allen &
Coggan, *Training and Racing with a Power Meter*. Twenty minutes.

**Where the value lives:** `solver/tables/intensity.py` → `if_ref['half']`.

---

## 3 · D-1 — The bike heat curve has no duration term

**Status:** a known, documented structural gap, not an error.
`SOLVER_MODEL.md` §D-1 calls it the highest-priority open gap.

**Consequence:** Peiffer's 40 km time trial is about 60 minutes. Both primary
distances have bike legs far longer — roughly 2.5 h at 70.3 and 4.5–7 h at
full distance — and thermal strain accumulates over exactly that span. The
curve therefore **under-states the decrement for the only two formats we
sell, and the bias grows with race length.** The flat clamp above WBGT 25.05
pushes the same optimistic direction, and neither corrects the other.

The model's own instruction: *"treat hot full-distance projections as the
model's least trustworthy output."*

**What closes it:** back-testing full-distance hot races and fitting a
duration term empirically. No published dose–response over that duration is
believed to exist. **Still open**, and no number has been invented for it: a
fitted coefficient with nothing behind it would read on the page exactly like
a measured one.

**What has changed:** the athlete is now told. The solver emits
`model:bike_heat_duration` whenever a heat decrement is applied over a bike leg
longer than the ~60-minute ride the knots were measured over — which is
essentially every hot long-course bike, correctly, since that is the population
the curve is weakest for. `model:bike_heat_clamp` marks the flat hold above the
top knot. Both travel on the plan as `advisories`, are rendered as prose beside
the numbers they concern, and say the figure is soft **without quoting a
correction**, because there is none to quote.

`solver/tables/heat_curve.py` → `BIKE_HEAT_SOURCE_MINUTES` is where the
threshold lives.

**Commercial consequence:** unchanged, and now visible in the product rather
than only in this file. A hot full-distance projection should not be marketed
on the same accuracy basis as a cool one until this is measured.

---

## 3b · The run heat clamp does not protect anything a race is held in

**Status:** found while surfacing D-1. Not previously recorded anywhere.

`model:run_heat_clamp` is documented as the signal that the run model has left
its data and the plan "should be treated as advisory". It first binds at:

| level | WBGT |
|---|---|
| first timer | 51.5 °C |
| improver | 59.0 °C |
| experienced | 70.0 °C |

A WBGT in the mid-thirties is already the range in which events are cancelled.
The clamp is therefore **unreachable in any race that happens**, and between
Ely's anchors and there the run heat curve extrapolates unchecked — on the leg
where heat costs most. This is the same shape of gap as D-1, on the other leg,
and it was hidden by a guard that reads as though it were doing something.

Compounding it: `RunHeat.clamped` was computed and consumed nowhere, so even
where it did bind nothing would have been said. That half is fixed — it now
travels on the plan as an advisory like the bike pair.

**What closes it:** the same work as D-1 — back-tested hot races — plus a
decision about what the run model should do between WBGT 30 and 40, where it is
currently extrapolating a power law three anchors wide. A clamp set where races
actually get hot would change plans, so it is a model change and not a
reporting one, and it should not be made without data.

**Where it lives:** `solver/tables/heat_curve.py` → `RUN_HEAT_FACTOR_MAX`,
`RUN_HEAT_PCT_AT_15`, `RUN_HEAT_EXPONENT`.

---

## 4 · Olympic and Sprint are unvalidated and must not be marketed on the same accuracy claim

**Status:** working, deliberately out of primary scope (`SOLVER_MODEL.md`
§0.1b, §D-13).

`if_ref['olympic']` and `if_ref['sprint']` were obtained by continuing the
shape of the `full` and `half` rows. That is a defensible way to produce a
working number and not a defensible way to produce an evidenced one. Short
course is also drafting-legal, and the tactical reality is not represented
anywhere in this model.

Golden cases `G03` and `G04` assert that those code paths run and stay
deterministic. They assert nothing about accuracy, and `SOLVER_MODEL.md` §C.1
sets no error target for them.

**What closes it:** original work, since the evidence does not currently
exist. Until then, short-course plans must not carry the same accuracy claim
as full and 70.3.

---

## 5 · The back-test in §C has not been run

`SOLVER_MODEL.md` §C.5's results table is deliberately empty — *"a pre-filled
expectation is an invitation to tune toward it."* The model's stated accuracy
bar (±3% total time, no barrier-margin sign errors) is therefore a target, not
a measurement.

**What closes it:** three to four real races with pre-race constraints, the
course, the conditions, actual splits, and **power data on the bike** — without
power, a total-time match cannot distinguish a correct model from two errors
cancelling. §C.4's diagnostic table maps each error pattern to the constant to
move.

**Judge legs before totals.** A 1% total built from a bike 4% fast and a run 5%
slow is two errors that happen to cancel, and they will not cancel for the
next athlete.

---

## 6 · Media: all 41 assets are absent

Expected and correct for this build. The API serves what is configured and
returns a clean 404 otherwise; the weekly media-asset audit reports every one
that does not resolve, which on its first run is all 41.

Three have **no documented frontend fallback** and need an integration-session
decision rather than just a file: `assets/hero/hero-loop.mp4` (landing hero
video), `assets/authors/jonas.jpg` (Guide byline), and
`assets/coach/feldt-mark.png` (coach brand mark). Course art falls back to the
course's `tone_color`; these three do not. Recorded in
`FIELD_NAME_RECONCILIATION.md`.

---

## 7 · Six of nine courses are not generated

Expected and correct. The race directory renders three until the six
`status: pending` specs are generated by `pipelines/course-ingest`. Their
coordinates, terrain character, lap structure and cut-off dial are settled and
reviewed; generating them is `status: ready` plus a pipeline run, not new work.

**Commercially:** a directory of three is a thin catalogue to charge for. This
is a launch decision, not an engineering one.

---

## 8 · Wetsuit legality thresholds are rules, and rules change — **mechanised**

`SOLVER_MODEL.md` §E-12: the water-temperature thresholds in
`solver/tables/swim_model.py` are Ironman competition rules, not physics, and
should be re-checked against the current season's rules annually. They produce
a genuine ~4.5% pace step at 24.5 °C, so a stale threshold is a visible error.

The thresholds are no longer three loose constants. `WETSUIT_RULESETS` holds
them as a named ruleset carrying its **federation, season, source and a
`review_by` date**, and `wetsuit_decision` reads whichever ruleset it is given.

The annual re-check is now a test rather than a hope:
`test_the_wetsuit_rules_have_not_gone_out_of_date` fails once `review_by`
passes, naming the ruleset, the season it was read for and the document to open.
**That failing is the mechanism** — a note asking someone to remember is what
this entry used to be.

**Still open, deliberately:** only the Ironman ruleset is populated. World
Triathlon publishes a different table, keyed by swim distance as well as
temperature, and those numbers are not in front of anyone here. Transcribing
them from memory would put figures in a rulebook table that no rulebook
supports — which is the failure the structure exists to prevent. Adding a
federation is one entry in `WETSUIT_RULESETS` and nothing else.

---

## 9 · G08-FIRSTTIMER does not test what it was designed to test

**Status:** the case runs and passes; its *purpose* is not met.

`SOLVER_MODEL.md` §B.4 defines `G08-FIRSTTIMER` as `C-HALF` + `A-F` + `F-MILD`,
exercising *"First-timer bag set; `first` level tables throughout"*. In this
implementation **athlete `A-F` is infeasible on `C-HALF`**, missing the bike
cut-off by 12.2 minutes, so Stage 4 never runs, Stage 6 never runs, and no bag
set is produced.

This is the same defect the document itself found and fixed for `G10-NIGHT`,
where §B.2 records: *"`A-X` was replaced outright: its previous profile turned
out to be infeasible on `C-TRAM`, so `G10-NIGHT` never reached Stage 6 and
tested nothing at all — the case asserted a head torch on a plan that was never
produced."*

**Why the inputs were not retuned here.** §B specifies case inputs exactly, and
the brief says to implement the document rather than adjust it. Retuning `A-F`
would also change `G05-INFEASIBLE`, which uses the same athlete and whose
infeasible verdict is definitional.

**Coverage is not lost**, which is why this is a launch blocker rather than a
stop: the first-timer bag set and the `first` level tables are exercised by
`G06-TIGHT` (athlete `A-T`) and `G10-NIGHT` (athlete `A-X`), both feasible,
both first-timers, and both asserted to emit the five first-timer items. What
`G08` no longer covers is that combination **at half distance**.

**What closes it:** a decision by whoever maintains `SOLVER_MODEL.md` — either
a new athlete for `G08` (as was done for `A-X`), a different course, or an
explicit note that `G08` now exercises half-distance infeasibility instead.
`C-HALF` is segments 1-6 of `C-TRAM`, so it carries Coll de Femenia and is a
genuinely hilly 90 km; a 155 W first-timer struggling on it is not obviously a
model error.

---

## 10 · Real course bundles produce ~20x more histogram bins than §0.8 assumed

**Status:** measured, inside the SLA, worth knowing before the catalogue grows.

§0.8's performance budgets were measured against a bike leg "gradient-binned
(≈80 bins)". The actual Tramuntana bundle, resampled to 10 m nodes, produces
**1,813 bins across 18,000 node pairs** — real terrain simply has more distinct
gradients than a synthetic profile.

Stage 3 evaluates the whole grid against every bin, so the cost scales with
them. Measured on this hardware after the optimisation in D-032:

| | §0.8 | real bundle |
|---|---|---|
| bike-leg nodes | ~1,800 | 18,001 |
| histogram bins | ~80 | 1,813 |
| full solve | not stated | **1,348 ms** |
| targets | — | P50 3,100 · P95 5,400 · **hard SLA 6,000** |

Comfortable today. The things that would erode it: a course with more segments
than Tramuntana's 24, a narrower `gradient_bin_width` (config, range
0.001-0.005), or a raised `if_grid_step` resolution. All three are the levers
§0.8 says the headroom exists to spend, so the headroom should be measured
again before any of them is spent.

The performance test runs against the **real** bundle for exactly this reason:
the golden fixtures are built with a constant gradient inside each segment, so
their histograms collapse to a single bin and a revert to per-node solving
would cost them nothing and pass unnoticed.
