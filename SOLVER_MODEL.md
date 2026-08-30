# RaceOS — Solver Model

**Status:** Draft for review. No code has been written against this document.
**Produced by:** Session A (domain modelling).
**Consumed by:** the backend build, milestone 4 (`solver/` package), and its golden-file regression suite.
**Governs:** the mathematics inside the solver contract. It does not change the contract.

---

## §0 Reading this document

### 0.1 What this is, and what it is not

`RaceOS_Build_Spec.md` Part 5 defines the solver's **contract** — six stages, invariants, I/O shapes,
determinism, SLA, failure behaviour. `BACKENDREQUIREMENTS.md` §17.6 deliberately leaves the algorithm
open. This document closes that gap and nothing else.

It specifies, for each of the six stages: the formulas, every constant with its source and confidence,
the boundary conditions and clamps, and a fully worked numeric example whose arithmetic an implementer
can check their code against.

It does **not** redesign the contract. Where a contract element looked wrong during modelling, it is
raised in §D (Open questions), not worked around.

Three things this document deliberately refuses to do:

1. **It does not invent physiology.** Where the evidence does not support a number, the gap is written
   down as an open question rather than filled with a plausible-looking constant. §D lists eleven of these.
2. **It does not launder confidence.** Every constant carries a confidence rating and a statement of what
   would change it. A constant labelled *estimated* is an estimate in the document as well as in the UI.
3. **It does not hide a disagreement with the literature.** §0.7 records the two places where this model
   deliberately departs from a number already visible in the product, and why.

### 0.1b Scope — which distances this model is accountable for

**RaceOS targets Ironman (full) and Ironman 70.3.** Those two distances are the primary scope: their
constants are sourced, their golden cases are the ones that matter, and the validation protocol in §C is
written against them.

**Olympic and Sprint exist in the seeded course directory and must keep working, but they are not primary
and their intensity-factor bands are extrapolated rather than sourced.** Nothing in the short-course
literature I could reach supports specific age-group intensity targets, partly because short-course racing
is drafting-legal and therefore tactical in a way this model does not represent at all. The `olympic` and
`sprint` rows of `if_ref` were obtained by continuing the shape of the `full` and `half` rows, which is a
defensible way to produce a working number and not a defensible way to produce an evidenced one.

Consequences, applied throughout:

- The `olympic` and `sprint` rows are marked **out-of-primary-scope, unvalidated** in §4.2.1 and §A.
- §C sets no error target for them and excludes them from back-testing.
- Their golden cases (`G03`, `G04`) exist to exercise code paths — the short-distance branch, the
  `pace_target ≥ run_threshold_pace` clamp — **not** to assert that the numbers are right.
- A plan produced for those distances should not be marketed on the same accuracy claim as a full or 70.3
  plan until someone validates them.

### 0.2 Source verification status — read this before trusting a constant

Every constant below is cited. **However, this document was researched in an environment whose egress
policy blocked direct access to publishers** (`arxiv.org`, `pubmed.ncbi.nlm.nih.gov`,
`journals.humankinetics.com`, `en.wikipedia.org` and every other publisher host attempted returned
HTTP 403 at the proxy). Search was available; full text was not.

**Consequently: not one equation or constant in this document was verified against its primary source
by its author.** Citations were reconstructed from search-result summaries, cross-checked against each
other, and — where possible — validated numerically against independently reported values from the same
paper (see the Minetti check in §4.3.2, which reproduces two of the paper's reported measurements to
within 3%, and the Peiffer check in §I.1.4).

Constants where this gap is material are flagged **⚠ VERIFY** and collected into a checklist in **§E**.
That checklist should be worked before implementation, not after. It is roughly two hours of library
access for one person.

This is stated at the top because a constant sourced from a search summary and a constant read out of a
paper are not the same object, and a document whose entire purpose is trustworthiness should not blur them.

### 0.3 Notation and units

| Symbol | Meaning | Unit |
|---|---|---|
| `v` | ground speed | m·s⁻¹ |
| `v_a` | air speed (ground speed + headwind component) | m·s⁻¹ |
| `w` | wind speed | m·s⁻¹ |
| `g` | gradient, rise/run as a **fraction** (0.058 = 5.8%) | — |
| `θ` | road angle, `atan(g)` | rad |
| `G` | gravitational acceleration | 9.80665 m·s⁻² |
| `ρ` | air density | kg·m⁻³ |
| `m` | total mass, athlete + bike + kit | kg |
| `P` | rider power at the crank | W |
| `W` | Wet Bulb Globe Temperature | °C |
| `T` | dry-bulb air temperature | °C |
| `RH` | relative humidity | % (0–100) |
| `T_w` | psychrometric wet-bulb temperature | °C |
| `h` | elevation above sea level | m |

Internal computation is float64 throughout. All angles in radians internally; gradients are stored and
configured as fractions, never percentages.

**§0.9 is a glossary** of the terms whose everyday coaching usage is looser than this document's usage —
`run_threshold_pace`, CSS, NP, VI, "tightest" versus "earliest missed" barrier, and the difference between
an `estimated` constraint and an assumed field. Where the two usages differ, the glossary governs.

**Legs** are `swim | t1 | bike | t2 | run`. **Distance types** are `full | half | olympic | sprint`.
**Levels** are `first | improver | experienced`. **Risk** is `conservative | balanced | aggressive`.

### 0.4 Determinism and rounding

The contract requires byte-identical output for identical input. This model satisfies it as follows.

**Prohibited in the numeric path:** `random`, `datetime.now()`, `uuid4()`, iteration over `set`, iteration
over any `dict` whose insertion order is not itself deterministic, `sorted()` without an explicit total-order
key, and any tolerance-terminated loop.

**Root-finding is fixed-iteration, never tolerance-terminated.** Every speed solve is **bisection on the
bracket [0.5, 30.0] m·s⁻¹ for exactly 60 iterations**, returning the bracket midpoint. Sixty halvings of a
29.5 m·s⁻¹ bracket give a residual interval of 2.6 × 10⁻¹⁷ m·s⁻¹ — below float64 resolution at these
magnitudes, so the result is exact and identical on every platform. Bisection rather than Newton is
mandatory: Newton diverges on steep descents, where the gravity term makes the power function non-monotonic
near the lower bracket bound. (This was observed during modelling: Newton returned 1.8 km·h⁻¹ for a −4.5%
descent. Bisection returns 61.8 km·h⁻¹.)

**Rounding happens once, at `SolveOutput` construction, through a single `round_half_even(x, n)` helper.**
Never mid-computation, never via `repr`. Precisions are in `solver/tables/rounding.py`:

| Field | Precision |
|---|---|
| `target_watts` | 1 W |
| `target_pace_sec_per_km` | 1 s·km⁻¹ |
| swim pace | 1 s·(100 m)⁻¹ |
| `split_minutes`, `target_minutes`, `eta_minutes`, `margin_minutes`, `projected_minutes` | 0.1 min |
| `carb_g_per_hr`, `total_carb_g` | 1 g |
| `fluid_ml_per_hr` | 10 mL |
| `sodium_mg_per_hr` | 10 mg |
| `caffeine_mg_total` | 5 mg |
| `load_pct`, `readiness_fraction`, `drift_pct` | 0.1 |

**Summation order is fixed.** Segment quantities accumulate in `ordinal` order. Legs accumulate in the
fixed order `swim, t1, bike, t2, run`. Floating-point addition is not associative; this is why the order
is specified rather than left to the implementer.

**Cross-stage consistency rule.** Downstream stages consume **unrounded** values. `total_carb_g` is computed
from the unrounded rate and unrounded duration, then rounded — never from the rounded rate. Otherwise
Stage 5's arithmetic-consistency invariant fails on long races.

### 0.5 Bindingness is a first-class output

The product exposes a "Why this?" drawer on every number, so every emitted value must be able to name the
constraint that determined it. This is designed in, not bolted on.

**Every clamped quantity is computed through a `bind()` helper**, not through `min()`/`max()`:

```
bind(candidates) -> (value, binding_key)
    candidates : ordered tuple of (constraint_key, limit_value, direction)
                 direction ∈ {UPPER, LOWER}
    value        = the most restrictive limit
    binding_key  = the key of the candidate that produced it
```

Ties are broken by **position in the candidate tuple**, lowest index wins. The tuple order is a fixed,
configured precedence list per quantity (`solver/tables/precedence.py`), never the iteration order of a
runtime collection. This makes tie-breaking deterministic, which matters more often than it sounds: a
first-timer whose gut ceiling exactly equals the duration-based carbohydrate target is a common case.

Every `bind()` call site names its quantity. `SolveOutput.binding_constraint_key` is the binding key of the
quantity that determined `projected_minutes` — resolved by the precedence chain in §3.5.

Constraint keys used by `bind()` are of three kinds, and all three are legitimate binding keys:

- **Athlete constraint keys** — the eight canonical keys.
- **Barrier keys** — `barrier:<name>`, emitted when a cut-off forced intensity up.
- **Model limit keys** — `model:<limit_name>`, e.g. `model:gastric_emptying_cap`,
  `model:if_ceiling`, `model:vi_ceiling`, `model:carb_hard_max`. These are configured limits, and the
  config table in §A is their register.

### 0.5b `assumed_fields` — declaring what the solver had to guess

Four inputs are optional (§F). When one is absent the solver substitutes a documented default and **names
the substitution in the output**:

```
SolveOutput.assumed_fields : tuple[str, ...]      # sorted, dotted paths, empty when nothing was assumed
```

Emitted paths and their fallbacks:

| Path | Fallback | §  |
|---|---|---|
| `athlete.bike_setup` | `road_clipons` + `standard` helmet | I.2.3 |
| `forecast.pressure_hpa` | ISA standard, 101325 Pa at sea level | I.1.1 |
| `forecast.cloud_cover_pct` | categorical mapping from `conditions` | I.1.3 |
| `sweat_rate.measured_at_temp_c` | `w_sweat_ref = 15.0 °C WBGT` | 5.2 |

The tuple is **sorted lexicographically** so it is deterministic, and it is part of the golden-file output.

This exists because the alternative is worse in a specific way. A default that is silently substituted is
indistinguishable, downstream, from a value the athlete actually supplied — so the UI cannot mark it, the
back-test cannot exclude it, and the drift service cannot tell "the athlete told us something new" from
"the athlete changed their mind". `assumed_fields` keeps the numeric path unchanged (the default is used at
full weight, exactly as §0.6 requires of an estimate) while keeping the *fact of the assumption* visible.

Note the deliberate asymmetry with §0.6: an `estimated` **constraint** is a value the athlete supplied
through an estimator, and it is never flagged. An **assumed field** is a value nobody supplied at all. The
first is a statement about confidence; the second is a statement about absence.

### 0.6 Provenance and estimated constraints

Per contract: **an `estimated` constraint is used with exactly the numeric weight of a `measured` one.**
There is no down-weighting, no widening of an interval, no shrinkage toward a population mean, and no
branch anywhere in this model that reads `constraint.source`. Provenance travels through the solver
untouched into `plan_constraint_refs.source_label` and is a presentation concern only.

This is a deliberate and slightly uncomfortable choice, so it is worth stating why it is right: a model
that quietly hedged estimated inputs would produce a plan that is neither the plan implied by the athlete's
stated numbers nor the plan implied by any other numbers, and the "Why this?" drawer could not honestly
explain it.

### 0.7 Deviations from the published reference points

Two numbers already visible in the product were used as calibration targets. One is reproduced exactly.
One is superseded, on review, and the frontend will need updating.

**Reference point 1 — bike, −6 W at 31 °C: reproduced, and it was under-specified.**
Peiffer & Abbiss (2011) measured 40 km time-trial power at four ambient temperatures. Interpolating their
data to 31 °C against a 22 °C baseline gives −3.83%, which on a 157 W target — a mid-level athlete at
IF 0.70 off a 224 W FTP — is **−6.0 W**. The reference point falls on the curve to the tenth of a watt,
as a consequence of the data, with no special-casing.

The reference point as published names a temperature but no humidity, and this model is humidity-aware.
Once expressed on the WBGT axis this model uses (§I.1.4), **−6 W corresponds to WBGT ≈ 24.2 °C** — that is
31 °C at about 30% RH under clear sky, or 27 °C at about 55% RH. At 31 °C **and 55% RH** the model emits
**−7.3 W**, because it is a hotter condition than the reference point's author had in mind. This is not a
disagreement with the reference point; it is the reference point being pinned down. The published figure
should be restated with its humidity.

**Reference point 2 — run, +9 s·km⁻¹ at 31 °C: superseded.**

This one does not survive contact with the literature, and the product owner's decision on review was to
follow the literature.

Ely et al. (2007) measured marathon field performance against WBGT across 140 race-years: a 3-hour
marathoner slows approximately **10%** as WBGT rises from 10 °C to 25 °C; an elite slows about 2%; and
slower runners suffer progressively more, not less. At 31 °C / 55% RH under clear sky the WBGT is 27.7 °C
(§I.1.3), which on an Ely-anchored curve costs a mid-level athlete **+16.2%**, or **+53.5 s·km⁻¹** (§4.3.4) on a
331 s·km⁻¹ target pace.

To land on +9 s·km⁻¹ instead, one of two things must be true, and neither is defensible:

- the mid-level temperature coefficient is ≈ 0.0015 per °C WBGT — which is the sensitivity the literature
  assigns to **elite** marathoners, applied to a mid-pack age-grouper; or
- the zero-penalty baseline sits at roughly **26 °C** air temperature, which would then under-predict heat
  cost for every race between 15 °C and 26 °C — that is, for most of the race calendar.

Both errors run in the same direction: they make the model over-predict run performance in the heat. That
is the direction that produces a plan an athlete cannot execute, a cut-off margin that is not real, and a
race that ends in a medical tent. Given the product's claim is that its numbers are trustworthy, this
model takes the literature.

**Consequence for the frontend:** the mock's `+9 s/km` must be regenerated. At 31 °C / 55% RH / clear sky
the model's mid-level run delta is **+53 s·km⁻¹**, and the heat card copy should be reviewed against a
number of that magnitude before launch. This is a visible product change and is called out here so that it
is a decision rather than a surprise.


### 0.8 Performance — measured, and where the naive approach fails

The contract sets a hard 6 s SLA with stage targets of roughly 0.2 / 0.3 / 1.1 / 0.9 / 0.4 / 0.2 s. The
model below fits, but **only because of the quadrature choice in §1.1** — the obvious implementation does
not fit, and this section says so explicitly rather than leaving it to be discovered during the build.

All figures below are **measured**, in pure CPython with no `numpy`, on this session's container.

| Operation | Cost | Note |
|---|---|---|
| One speed solve (60-iteration bisection) | ~0.41 µs | 2.42 M evaluations·s⁻¹ |
| One bike-leg time evaluation, gradient-binned (≈80 bins) | ~1.7 ms | |
| **Stage 3 grid: 61 IF points, gradient-binned** | **105 ms** | Inside the 1.1 s target with 10× headroom |
| Stage 3 grid: 61 IF points, **per elevation node (1802 nodes)** | **2360 ms** | ✗ **Blows the 1.1 s target by 2×** |
| Stage 3 levers: 4 constraints × 1 profile | ~7 ms | See below |
| Stage 4: planned profile + up to 4 VI back-off passes | ~9 ms | |
| Stage 4 run and swim | closed-form | Microseconds; no iteration |
| Stages 1, 2, 5, 6 | closed-form | Dominated by I/O and object construction, not arithmetic |

**The approach that does not fit, and the cheaper one that does.** Solving speed at every elevation node for
every point on the Stage 3 intensity grid is the most obviously correct implementation, and it costs
2.36 s — more than double Stage 3's budget, before levers. The gradient-histogram quadrature of §1.1 gives
the same answer to within 0.1% for **22× less work**. It is not an approximation of convenience: it is exact
in distance and loses only ±0.00125 of gradient resolution.

**One deliberate approximation in the lever computation.** §3.4 evaluates each perturbed constraint at the
IF that minimised the base profile, rather than re-running the whole grid. Re-optimising per lever would be
more correct and would cost 4 × 105 ms = 420 ms; the optimum IF moves negligibly under a 5% constraint
perturbation, so the single-profile evaluation costs 7 ms instead. This is a stated approximation, not an
oversight, and it affects only the *ranking* of levers, never a plan's numbers.

**Total measured: well under 0.2 s of arithmetic for a full-distance solve.** The 6 s SLA is not at risk
from the mathematics. If a production solve approaches it, the cause will be bundle loading, serialisation
or database access — none of which is in this document's scope. That headroom is worth preserving: it means
a future decision to raise `if_grid_step` resolution, or to add a second optimisation dimension, has room
to land.

### 0.9 Glossary of the terms this model defines precisely

Terms whose everyday coaching usage is looser than this document's usage. Where the two differ, the
definition here governs the mathematics.

| Term | Definition as used in this model |
|---|---|
| **`run_threshold_pace`** | The pace the athlete could hold in an all-out **one-hour** race, s·km⁻¹. The running equivalent of FTP. Not 10 km pace, not lactate-threshold pace, not marathon pace. Entry routes and conversions: §2.5 |
| **`swim_threshold_pace` / CSS** | **Critical Swim Speed**, s·(100 m)⁻¹, from the 400/200 pair: `CSS = 200/(t₄₀₀ − t₂₀₀)`. An **asymptotic** threshold speed — the slope of the distance–time line — not the pace at any particular distance. Modelled in §4.4 |
| **`D′` (D-prime)** | The intercept of the swimming distance–time line, in metres: the finite distance available above CSS. The swimming analogue of W′. §4.4.1 |
| **FTP / `bike_threshold_power`** | The power sustainable for approximately one hour, W. Unchanged from standard usage |
| **IF (intensity factor)** | Planned normalised power as a fraction of FTP. In this model `IF_ref` is a *target*, not the output of a free search — §4.1 |
| **NP (normalised power)** | Fatigue-weighted mean power. Computed at **segment resolution** here (§4.2.2), which is not the same as the 30-second rolling definition and systematically reads lower |
| **VI (variability index)** | `NP / AP`. At segment resolution this model produces VI ≈ 1.00; it is a safety rail, not an active constraint (§4.2.2) |
| **CdA** | Drag area, m². Driven by the `bike_setup` input (§I.2.3), not by athlete level |
| **Crr** | Coefficient of rolling resistance. A property of the **course surface**, from the bundle, not of the athlete |
| **WBGT** | Wet Bulb Globe Temperature, °C. The heat-stress index both heat curves are expressed in (§I.1.3). Not dry-bulb temperature, and typically several degrees below it |
| **`T_w`** | Psychrometric **wet-bulb air** temperature (§I.1.2). Never the water temperature, which is `T_water` |
| **Barrier / gate / cut-off** | A mandatory time limit at a point on the course. `barrier` is the bundle's term, `gate` the plan's |
| **Tightest barrier** | The barrier with the smallest margin. Drives `worst_margin_minutes` and `margin_state` |
| **Earliest missed barrier** | The missed barrier with the lowest `limit_minutes_from_start`. This is what an infeasibility **reports**, because it is where the athlete's race actually ends (§3.3) |
| **Binding constraint** | The named limit that determined an emitted value, produced by `bind()` (§0.5). A first-class output, not a post-hoc explanation |
| **Assumed field** | An optional input that was absent, so the solver used a documented default. Listed in `assumed_fields` (§0.5b). Distinct from an `estimated` constraint |
| **Net gradient** | `(h_end − h_start) / distance` for a segment. Sets the segment's **power target**. Never used to compute its **time** — §1.1 |
| **Gradient histogram** | Per-segment map of gradient bin to total distance in that bin. What time is actually integrated over (§1.1). Quadrature, not smoothing |

---

# Part I — Shared machinery

Stages 3 through 6 all consume the environment model and the cycling power–speed relation. They are
specified once here and referenced by stage. This preserves the six-stage structure of the contract
without duplicating the physics five times.

## §I.1 The environment model

### I.1.1 Air density

Aerodynamic drag scales linearly with air density, and density moves by ~11% between a cold sea-level
morning and a hot mountain afternoon, so this is not a refinement.

Pressure from the International Standard Atmosphere, at the segment's elevation:

```
p(h) = p₀ · (1 − L·h / T₀) ^ (G·M / (R*·L))
     = 101325 · (1 − 0.0065·h / 288.15) ^ 5.25588          [Pa]
```

Density including the humidity correction (moist air is *less* dense than dry air, by ~0.6% at 31 °C
and 55% RH — small but free to include, and it moves in the opposite direction to intuition):

```
p_sat(T) = 610.78 · exp( 17.27·T / (T + 237.3) )            [Pa]   (Tetens)
p_v      = (RH/100) · p_sat(T)
p_d      = p(h) − p_v
ρ        = p_d / (R_d · T_K)  +  p_v / (R_v · T_K)          [kg·m⁻³]
T_K      = T + 273.15
```

| Constant | Value | Unit | Source | Confidence | What would change it |
|---|---|---|---|---|---|
| `p0_pa` | 101325 | Pa | ISA definition | Certain | Nothing |
| `lapse_rate_k_per_m` | 0.0065 | K·m⁻¹ | ISA definition | Certain | Nothing |
| `isa_exponent` | 5.25588 | — | ISA definition | Certain | Nothing |
| `R_d` | 287.058 | J·kg⁻¹·K⁻¹ | Specific gas constant, dry air | Certain | Nothing |
| `R_v` | 461.495 | J·kg⁻¹·K⁻¹ | Specific gas constant, water vapour | Certain | Nothing |
| `tetens_a` | 610.78 | Pa | Tetens equation | High | A switch to Magnus-Buck; effect < 0.1% |
| `tetens_b` | 17.27 | — | Tetens equation | High | as above |
| `tetens_c` | 237.3 | °C | Tetens equation | High | as above |

**`forecast.pressure_hpa` (new input, §F.3).** When supplied, it replaces the ISA term entirely:

```
p(h) = pressure_hpa · 100 · (1 − L·h_station / T₀)^5.25588  /  (1 − L·h_sea / T₀)^5.25588
```

In practice forecast providers report pressure already reduced to sea level (QNH), in which case
`h_station = h` and `h_sea = 0`, so this collapses to `p(h) = pressure_hpa · 100 · (1 − 0.0065·h/288.15)^5.25588`
— the ISA formula with the measured sea-level pressure substituted for the 101325 Pa standard. The adapter
must confirm which convention its provider uses; station pressure passed as sea-level pressure would be
wrong by roughly 1.2% per 100 m of elevation.

**When absent**, fall back to `p₀ = 101325 Pa` (the standard atmosphere) and add `forecast.pressure_hpa` to
`SolveOutput.assumed_fields`. Real pressure varies about ±4% around standard, which is ±4% on the
aerodynamic term — comparable to the entire heat effect — so this fallback is a genuine error source and
the output says so rather than hiding it. Range check: 870–1085 hPa; outside that, treat as absent.

**Clamps.** `h` clamped to [−430, 5000] m (Dead Sea to above any plausible race). `T` clamped to
[−20, 55] °C. `RH` clamped to [1, 100] %. Resulting `ρ` asserted within [0.55, 1.50] kg·m⁻³; outside is a
programming error, not an input error.

### I.1.2 Psychrometric wet-bulb temperature

Stull (2011), *Journal of Applied Meteorology and Climatology* 50:2267–2269. A single-expression empirical
fit, mean absolute error < 0.3 °C, valid for RH 5–99% and T −20 to +50 °C. Chosen because it is closed-form,
continuous, deterministic, and needs only the two variables the forecast actually carries.

```
T_w(T, RH) =  T · atan( 0.151977 · √(RH + 8.313659) )
            + atan(T + RH)
            − atan(RH − 1.676331)
            + 0.00391838 · RH^1.5 · atan(0.023101 · RH)
            − 4.686035
```

All `atan` results in **radians**. This is the single most common implementation error with this formula
and is worth a comment in the code. **⚠ VERIFY** the coefficient digits against the paper (§E-1).

Worked: `T_w(31.0, 55.0) = 24.041 °C`. `T_w(20.0, 55.0) = 14.36 °C`. `T_w(10.0, 60.0) = 6.02 °C`.

### I.1.3 Wet Bulb Globe Temperature

WBGT is the standard heat-stress index in sports medicine and occupational safety, and it is the index the
marathon-heat literature is expressed in. Using it means the run curve can be anchored to Ely directly
rather than through a conversion.

```
WBGT = 0.7 · T_w  +  0.2 · T_g  +  0.1 · T
T_g  = T + globe_offset
globe_offset = g_clear · (1 − c) + g_overcast · c
```

where `c` is cloud fraction. `ForecastSnapshot.conditions` is categorical, so it maps to a cloud fraction:

| `conditions` | cloud fraction `c` | resulting `globe_offset` (°C) |
|---|---|---|
| `clear` | 0.00 | 8.00 |
| `partly_cloudy` | 0.35 | 5.72 |
| `cloudy` | 0.75 | 3.13 |
| `overcast` | 0.95 | 1.83 |
| `rain` | 1.00 | 1.50 |

**`forecast.cloud_cover_pct` (new input, §F.3) removes this table from the numeric path.** When supplied:

```
c = clamp(cloud_cover_pct, 0, 100) / 100
```

and the globe offset is then continuous in cloud cover, exactly as it already is in temperature and
humidity. This closes the last discontinuity in the environment model.

**When absent**, fall back to the categorical mapping above and add `forecast.cloud_cover_pct` to
`SolveOutput.assumed_fields`. Under the fallback the input is genuinely discrete: a forecast moving from
`clear` to `partly_cloudy` moves WBGT by 0.46 °C and a mid-level run pace by roughly 1.5 s·km⁻¹, with no
intermediate values. The contract requires that a plan not jump because a forecast moved 0.1 °C; it says
nothing about a categorical change, but the effect on an athlete's plan is the same kind of surprise. The
mapping was written as a **linear interpolation in cloud fraction** from the outset precisely so that
supplying the numeric field is an adapter change and not a model change — the categories are simply the
four points this curve is sampled at when nothing better is available.

| Constant | Value | Source | Confidence | What would change it |
|---|---|---|---|---|
| `wbgt_w_wet` | 0.7 | Standard WBGT definition | Certain | Nothing |
| `wbgt_w_globe` | 0.2 | Standard WBGT definition | Certain | Nothing |
| `wbgt_w_dry` | 0.1 | Standard WBGT definition | Certain | Nothing |
| `globe_offset_clear_c` | 8.0 | **Estimate.** Typical clear-sky globe–air difference for an exposed athlete | **Low** | A course-side globe measurement, or a forecast carrying solar irradiance |
| `globe_offset_overcast_c` | 1.5 | **Estimate**, same basis | **Low** | as above |

`globe_offset_clear_c` is doing real work — it contributes 1.6 °C to WBGT under clear sky, which is
roughly 5 s·km⁻¹ of mid-level run pace — and it is an estimate, not a measurement. It is the single
highest-leverage low-confidence constant in the environment model and the first thing to calibrate. **Wind
speed also reduces globe temperature substantially and is not modelled here** (§D-5).

Worked: `31 °C, 55% RH, clear` → `T_w = 24.041`, `T_g = 39.0`,
`WBGT = 0.7(24.041) + 0.2(39.0) + 0.1(31.0) = 16.829 + 7.800 + 3.100 = 27.729 °C`.

### I.1.4 Heat effect on cycling power

Peiffer & Abbiss (2011), *International Journal of Sports Physiology and Performance* 6(2):208–220,
measured mean 40 km time-trial power at four ambient temperatures:

| Ambient (°C) | Mean power (W) | Relative to 22 °C |
|---|---|---|
| 17 | 329 ± 31 | +1.54% |
| 22 | 324 ± 34 | 0.00% (reference) |
| 27 | 322 ± 32 | −0.62% |
| 32 | 309 ± 35 | −4.63% |

Converted onto the WBGT axis assuming laboratory conditions of **40% RH and no radiant load**
(`T_g = T`, so `WBGT_indoor = 0.7·T_w + 0.3·T`), giving the configured knots:

| Ambient (°C) | `WBGT_indoor` (°C) | `bike_heat_factor` |
|---|---|---|
| 17 | 12.016 | 1.01543 |
| 22 | 16.361 | 1.00000 |
| 27 | 20.707 | 0.99383 |
| 32 | 25.053 | 0.95370 |

**Interpolation between knots is linear in WBGT. Below the first knot and above the last, the value is
held flat (clamped), not extrapolated.**

The flat clamp above WBGT 25.053 is a deliberate refusal to extrapolate, and it matters. Fitting a
power law through the top two knots gives an exponent of 2.9; extrapolating that to WBGT 27.7 predicts
−10.1%, or −15.8 W. There is no evidence for that number — the data simply stops at 32 °C. Holding the
last measured value is the honest choice and errs toward *under*-stating the bike penalty, which is the
safe direction here: under-stating bike heat cost means the model plans slightly less power reduction,
and the run model (which is anchored well past this range) carries the consequence.

**Confidence: Medium-Low, and this is the weakest quantitative link in the model.** Three reasons:

1. **The lab humidity is assumed, not known.** 40% RH is a plausible controlled-lab value but I could not
   read the paper. If they ran at 60% RH the knots shift ~2 °C and the reference-point reconciliation in
   §0.7 changes. **⚠ VERIFY** (§E-2).
2. **A 40 km time trial is ~60 minutes; a full-distance bike leg is 4.5–7 hours.** Thermal strain
   accumulates. This curve almost certainly *under*-states the decrement over a long-course bike leg, and
   there is no published dose–response over that duration to correct it with. Not modelled. See §D-1.
3. **Indoor result transferred to an outdoor WBGT axis.** The 8 °C clear-sky globe offset was not present
   in the laboratory. This mixes an indoor measurement with an outdoor index.

Given all three, this curve is a top-three calibration target in §C.

### I.1.5 Heat effect on running

Anchored to Ely et al. (2007), *Medicine & Science in Sports & Exercise* — 140 race-years of marathon field
data across a wide ability range. Two reported anchors: a 3-hour marathoner slows ≈10% and an elite ≈2% as
WBGT rises from 10 °C to 25 °C, with slower runners affected progressively more.

```
run_heat_factor(W, level) = 1 + k_level · max(0, W − W₀) ^ p_heat

k_level = pct_at_15[level] / 15 ^ p_heat
```

| Constant | Value | Source | Confidence | What would change it |
|---|---|---|---|---|
| `W₀` (`run_heat_wbgt_baseline_c`) | 10.0 °C | Ely: slowing begins above WBGT 5–10 °C | Med-High | Field data showing degradation below WBGT 10 |
| `p_heat` (`run_heat_exponent`) | 1.3 | **Estimate**, chosen to reproduce the reported non-linearity ("50→70 °F costs far less than 70→90 °F") | **Low** | A published quadratic fit with coefficients; Ely's own model is quadratic but I could not read the coefficients (**⚠ VERIFY**, §E-3) |
| `pct_at_15['experienced']` | 0.10 | Ely, 3-hour marathoner, directly anchored | Med-High | Back-testing |
| `pct_at_15['improver']` | 0.13 | Ely's "slower runners suffer more", extrapolated | **Low-Med** | Back-testing |
| `pct_at_15['first']` | 0.16 | as above | **Low-Med** | Back-testing |

Derived: `15^1.3 = 33.8002`, so `k_experienced = 0.0029586`, `k_improver = 0.0038461`,
`k_first = 0.0047337` per (°C)^1.3.

Note the level mapping. Ely's "3-hour marathoner" is a strong age-grouper, which maps to this product's
`experienced`, not to its middle tier. That is why all three of this model's levels sit at or above 10% —
every RaceOS level is at or below Ely's reference runner in ability, and Ely's finding is that slower is
worse. Ely's elite 2% figure is outside this product's user base and is not used.

`p_heat = 1.3` is the constant that most needs a real fit. It controls behaviour in the extrapolated region
above WBGT 25 — exactly where hot races live.

Worked: `W = 27.72875`, `improver`: `(27.72875 − 10)^1.3 = 17.72875^1.3 = 42.00313`;
`1 + 0.0038461 × 42.00313 = 1.161550`, i.e. **+16.2%**.

**Clamp:** `run_heat_factor ≤ 1.60`. Beyond +60% the model is far outside any data and the honest output is
a warning, not a number. If the clamp binds, `binding_key = model:run_heat_clamp` and the plan should be
treated as advisory. In practice this binds around WBGT 32 °C for a first-timer — conditions at which races
are cancelled.

### I.1.6 Altitude

Two effects that oppose each other: thinner air reduces drag (helps the bike, already handled by ρ in
§I.1.1), and reduced oxygen partial pressure reduces sustainable aerobic power (hurts everything).

```
alt_factor(h) = 1 − a₁ · min(h, 1500)/1000 − a₂ · max(0, h − 1500)/1000
```

Applied as a multiplier to `bike_threshold_power`, and as a divisor to running speed (i.e. run pace is
multiplied by `1 / alt_factor`). Continuous, and the two pieces meet at `h = 1500` by construction.

| Constant | Value | Source | Confidence | What would change it |
|---|---|---|---|---|
| `alt_a1` | 0.010 per 1000 m | ~1% VO₂max loss per 1000 m below 1500 m | Med | Little; effect is small |
| `alt_a2` | 0.070 per 1000 m | **Sources disagree: 6.3%, 8.1% and 9.2% per 1000 m all reported.** 7.0% is the midpoint | **Low-Med** | Picking a side; a mountain course in the seeded set |

The `a₂` disagreement is a genuine three-way split in the literature and 7.0% is a compromise, not a
finding. It only matters for courses above 1500 m; none of the currently seeded fictional courses reach it.
Acclimatisation is **not modelled** — the effect is large, highly individual, and the product captures
nothing about where the athlete lives. See §D-6.

**Elevation used** is the *mean elevation of the leg*, not per-segment. Per-segment would imply the
athlete's aerobic ceiling changes within a single climb, which is not how acclimatisation state works.
Air density, by contrast, *is* per-segment, because that is a property of the air and not of the athlete.

### I.1.7 Solar position, sunset and civil dusk

Required by Stage 6: the head-torch rule must compute dusk from date, latitude and longitude, never a
fixed clock hour. Standard NOAA solar-position algorithm; entirely deterministic and closed-form.

```
JD  = julian_day(y, m, d) + 0.5                       # local solar noon
T   = (JD − 2451545.0) / 36525.0                      # Julian centuries

L₀  = (280.46646 + T·(36000.76983 + T·0.0003032)) mod 360        # geometric mean longitude
M   = 357.52911 + T·(35999.05029 − 0.0001537·T)                  # geometric mean anomaly
e   = 0.016708634 − T·(0.000042037 + 0.0000001267·T)             # orbital eccentricity

C   = sin(M)·(1.914602 − T·(0.004817 + 0.000014·T))
    + sin(2M)·(0.019993 − 0.000101·T)
    + sin(3M)·0.000289                                           # equation of centre

Ω   = 125.04 − 1934.136·T
λ   = L₀ + C − 0.00569 − 0.00478·sin(Ω)                          # apparent longitude
ε₀  = 23 + (26 + (21.448 − T·(46.815 + T·(0.00059 − T·0.001813)))/60)/60
ε   = ε₀ + 0.00256·cos(Ω)                                        # corrected obliquity

δ   = asin( sin(ε) · sin(λ) )                                    # solar declination
y_t = tan²(ε/2)
EqT = 4 · degrees( y_t·sin(2L₀) − 2e·sin(M) + 4e·y_t·sin(M)·cos(2L₀)
                   − 0.5·y_t²·sin(4L₀) − 1.25·e²·sin(2M) )       # equation of time, minutes

solar_noon_local_min = 720 − 4·lng_east − EqT + tz_offset_hours·60

cos(HA) = cos(zenith) / (cos(lat)·cos(δ)) − tan(lat)·tan(δ)
sunset_local_min     = solar_noon_local_min + 4·HA_degrees
```

**Longitude is positive east** and `tz_offset_hours` is the UTC offset **in effect on the event date**
(i.e. including summer time). Getting either sign wrong silently returns sunrise instead of sunset; this
was observed during modelling and is worth an explicit unit test in both hemispheres.

| Zenith angle | Event |
|---|---|
| 90.833° | sunrise / sunset (includes refraction and solar radius) |
| 96.0° | civil twilight — **this is the one Stage 6 uses** |

If `|cos(HA)| > 1` the sun does not reach that altitude on that date at that latitude (polar day or night).
Return `None` and fall back to `options.night_flag`. No RaceOS course is above the Arctic Circle, but the
branch must exist rather than raise.

**Worked** — 2026-09-19, lat 39.85 N, lng 3.12 E, UTC+2:
`δ = 1.3628°`, `EqT = 6.1957 min`, `solar_noon = 720 − 12.48 − 6.196 + 120 = 821.32 min = 13:41 local`.
Sunset (z = 90.833°) = **19:50**; civil dusk (z = 96.0°) = **20:17**.
Cross-check: Mallorca, mid-September — correct to the minute.

## §I.2 Cycling power–speed

Martin et al. (1998), *Journal of Applied Biomechanics* 14(3):276–291, validated at R² = 0.97 with a
standard error of 2.7 W. This is the standard model and there is no serious competitor to it.

```
P_total = [  ½·ρ·(CdA + F_w)·v_a²·v          aerodynamic
           + Crr·m·G·cos(θ)·v                rolling resistance
           + v·(91 + 8.7·v)·10⁻³             wheel-bearing friction
           + m·G·sin(θ)·v          ] / η_dt  gravity, then drivetrain
```

**The kinetic-energy term is dropped**, deliberately. Martin's full model includes
`½(m + I/r²)(v_f² − v_i²)/Δt`. Segments here are 5–26 km long and the course returns to its start, so net ΔKE
is a rounding error at segment resolution. This is an explicit simplification, not an omission, and it is
the reason the model is described as *steady-state per segment*.

### I.2.1 Wind

`ForecastSnapshot` carries wind. Whether it carries **direction** determines which of two forms is used.

**Direction known** — project onto the segment's mean bearing (derived in Stage 1 from the geometry the
bundle already contains; this is a derived quantity, not new course data):

```
v_a = v + w · cos(θ_wind − bearing_segment)
```

**Direction unknown (default)** — use the closed-form direction-averaged aerodynamic power. Averaging
`(v + w·cos φ)²` over uniformly distributed φ gives `v² + w²/2` exactly, since `E[cos φ] = 0` and
`E[cos² φ] = ½`:

```
aerodynamic term = ½·ρ·(CdA + F_w)·(v² + w²/2)·v
```

This is worth dwelling on, because it is both free and correct: **wind of unknown direction always costs
time.** Drag is quadratic, so a headwind costs more than the equal-and-opposite tailwind saves, and the
`w²/2` term is precisely that asymmetry. A model that set unknown wind to zero would systematically
under-predict every windy race. Cost: one extra addition inside the bisection. No quadrature, no sampling,
no loss of determinism.

At `w = 3 m·s⁻¹` and `v ≈ 8.9 m·s⁻¹` this is equivalent to a 5.7% CdA penalty.

### I.2.2 Constants

| Constant | Value | Unit | Source | Confidence | What would change it |
|---|---|---|---|---|---|
| `F_w` spoke drag area | 0.0044 | m² | Martin 1998 | **Low** | **⚠ VERIFY** (§E-4). ~1.6% of CdA, so an error here is small but it is unverified |
| `bearing_c0` | 91 | mW | Martin 1998 | **Low** | **⚠ VERIFY** (§E-4). Contributes ~1 W; harmless if wrong |
| `bearing_c1` | 8.7 | mW·s·m⁻¹ | Martin 1998 | **Low** | as above |
| `eta_drivetrain` | 0.976 | — | Martin measured 97.7%; Kyle & Berto (*Human Power* 52, 2001) and Spicer et al. (2001) give 96–98% at 50–200 W | High | Little. Spicer's 80.9% figure is a 76 N chain-tension artefact, not a road condition |

**Rolling resistance**, from the course bundle's surface descriptor rather than the athlete:

| `surface_quality` | `Crr` | Source | Confidence |
|---|---|---|---|
| `smooth_asphalt` | 0.0040 | Roller and coast-down tests report 0.0027–0.0040 for clinchers on smooth asphalt | Med |
| `typical_road` *(default)* | 0.0050 | Standard bicycle-on-asphalt figure | Med |
| `rough_chipseal` | 0.0065 | **Extrapolated** beyond the published 0.0025–0.005 band | **Low — estimate** |

`rough_chipseal` is labelled an estimate because the published range does not reach it; it is an inference
from "rough paved = 0.005" plus a margin, not a measurement. On a 180 km leg, moving from 0.0050 to 0.0065
costs about 8 minutes, so it should not be assigned to a course casually.

### I.2.3 CdA and the new `bike_setup` input

CdA is the single largest lever on the bike split, and until now the product captured nothing that
determines it. Across the plausible age-group range the effect is decisive:

| CdA | Speed at 157 W | 180 km split |
|---|---|---|
| 0.26 | 32.32 km·h⁻¹ | 5:34 |
| 0.28 | 31.62 km·h⁻¹ | 5:42 |
| 0.30 | 30.98 km·h⁻¹ | 5:49 |
| 0.32 | 30.39 km·h⁻¹ | 5:55 |

(Flat, `Crr` 0.005, 84 kg, ρ 1.2, no wind, `F_w` included.)

**21.2 minutes across that range — larger than the 20-minute `clear`/`tight` margin band.** The CdA
assumption alone can flip a feasibility verdict. Inferring it from `athlete.level` would mismodel both the
well-equipped first-timer and the experienced athlete on a road bike, and the error lands squarely on the
cut-off verdict.

**Therefore this model requires a new athlete input, `bike_setup`** — a product decision taken on review and
specified in full in §F.2.

```
CdA = clamp( base[position] + level_adj[level] + helmet_adj[helmet],  0.19,  0.38 )
```

| `position` | base CdA | Source |
|---|---|---|
| `road_hoods` | 0.325 | Measured road position ≈ 0.316; rounded up for a non-racer |
| `road_drops` | 0.300 | Measured ≈ 0.296 |
| `road_clipons` | 0.280 | Reported band 0.26–0.30 |
| `tt_bike` | 0.255 | Age-group TT band 0.20–0.23 optimised; 0.255 for a typical un-fitted setup |

| Modifier | Value | Rationale |
|---|---|---|
| `level_adj['first']` | +0.020 | Less able to hold position over hours |
| `level_adj['improver']` | 0.000 | Reference |
| `level_adj['experienced']` | −0.020 | Holds position; likely fitted |
| `helmet_adj['aero']` | −0.010 | |
| `helmet_adj['standard']` | 0.000 | |

Sanity of the span: `experienced + tt_bike + aero = 0.225`, which matches the reported "well-optimised
age-grouper 0.20–0.23"; `first + road_hoods + standard = 0.345`, plausible for a nervous first-timer sitting
up. The table spans the right range at both ends.

**Confidence: Medium on the position bases** (industry and coaching sources, mutually corroborating, but
none peer-reviewed), **Low on the modifiers** (reasoned, not measured).

**Fallback when `bike_setup` is absent** — required for existing athletes and for `preview_only` solves:
`road_clipons` + level adjustment + `standard` helmet, i.e. CdA 0.30 / 0.28 / 0.26 by level. This must set
the CdA `constraint_ref` to `source = 'estimated'` and add `athlete.bike_setup` to
`SolveOutput.assumed_fields`. Because the fallback can sit up to 0.045 m² away from an athlete's true value
— about 15 minutes over 180 km — supplying `bike_setup` for an existing athlete will frequently cross the
drift thresholds in §A, which is the correct behaviour: it is new information that genuinely moves the plan.

**Bike and kit mass**, also not captured. Defaults by level, from `solver/tables/equipment.py`:
`first 11.0 kg`, `improver 10.0 kg`, `experienced 9.0 kg`. **Low confidence, but low stakes** — ±1 kg on an
85 kg system is 1.2%, and it only bites on climbs (≈40 s over 2100 m of ascent).

### I.2.4 Speed solve

```
solve_speed(P, CdA, Crr, m, ρ, g, w) -> v
    P_wheel = max(0, P) · η_dt
    lo, hi  = 0.5, 30.0
    repeat exactly 60 times:
        v   = (lo + hi) / 2
        lhs = ½·ρ·(CdA + F_w)·(v² + w²/2)·v
            + Crr·m·G·cos(atan(g))·v
            + m·G·sin(atan(g))·v
            + v·(91 + 8.7·v)·10⁻³
        if lhs < P_wheel: lo = v  else: hi = v
    return min( (lo + hi)/2,  v_descent_max )
```

**Clamps.** `v_descent_max = 20.83 m·s⁻¹` (75 km·h⁻¹) — a safety and realism ceiling; no plan should
instruct an age-grouper to hold 80 km·h⁻¹. `P` floored at 0: on a steep descent the modulated target may go
negative, which means freewheeling, not braking-as-power. Speed floored at the bracket bottom of
0.5 m·s⁻¹; if that binds, the gradient is beyond rideable and the segment should be flagged, since a
triathlete will be walking.

---

# Part II — The six stages

## §1 Stage 1 — Load the course (~0.2 s)

### 1.1 What this stage computes

The bundle arrives as pinned data. Stage 1 turns it into the derived geometry the later stages consume,
and **derives nothing that is not already implied by the delivered node series.**

For each leg, the elevation series is a sequence of nodes `(s_j, h_j)` where `s_j` is cumulative distance
in metres and `h_j` is terrain-sampled elevation in metres.

**Node gradient** — a plain forward difference, with no smoothing:

```
g_j = (h_{j+1} − h_j) / (s_{j+1} − s_j)
```

**Segment aggregates**, over the nodes falling in `[from_km, to_km)`:

```
d_seg      = Σ (s_{j+1} − s_j)
Δh_seg     = Σ max(0, h_{j+1} − h_j)          # gain only, for elevation_gain_m
g_seg      = (h_end − h_start) / d_seg        # NET gradient, not mean of node gradients
h_mean_seg = Σ ((h_j + h_{j+1})/2 · (s_{j+1} − s_j)) / d_seg
bearing_seg= atan2( Σ Δeast_j , Σ Δnorth_j )  # from the leg geometry, for the wind term
hist_seg   = gradient histogram, see below
```

`g_seg` is the **net** gradient across the segment. It is what sets the segment's *power target* in §4.2.1,
because the gravity term integrates to `m·G·Δh` regardless of the path taken, and because a target the
athlete can hold has to describe the segment as a whole.

**But net gradient must never be used to compute the segment's time.** This is the most consequential
modelling detail in Stage 1, so it is worth showing rather than asserting. Time is *convex* in gradient — a
kilometre up at 4% and a kilometre down at 4% take much longer than two kilometres at 0% — so by Jensen's
inequality, solving speed once at the net gradient is always too fast, never too slow. Measured against a
per-node solve over an 1802-node bike leg:

| Terrain (node-gradient SD) | Per-node time | Net-gradient-only time | Error |
|---|---|---|---|
| Near-flat coastal (0.010) | 377.75 min | 365.15 min | **−3.34%** |
| Gently rolling (0.020) | 410.65 min | 366.34 min | **−10.79%** |
| Rolling (0.035) | 478.83 min | 368.15 min | **−23.11%** |
| Mountainous (0.055) | 586.12 min | 370.59 min | **−36.77%** |

Even a near-flat course is 3.3% fast, which is the whole error budget of §C.3 spent on a quadrature choice.
On rolling terrain the model would be unusable.

**Therefore Stage 1 also emits a gradient histogram per segment**, and Stage 4 integrates time over it:

```
hist_seg = { g_bin : total_distance_in_bin }
g_bin    = round(g_j / gradient_bin_width) · gradient_bin_width       # width 0.0025
```

Distance in each bin is exact — no node is discarded and no gradient is averaged away. The only loss is
gradient resolution within a bin (±0.00125), and the resulting time error is **under 0.1% on every terrain
type above**, against a 22× reduction in cost (§0.8).

**This is quadrature, not smoothing.** The distinction matters because §1.2 forbids smoothing. A moving
average would flatten a climb's peak gradient and make it easier; binning preserves the exact
distance-at-each-gradient distribution, so a 12% pitch stays a 12% pitch and merely shares a bin with 11.9%.
A typical bike leg reduces from ~1800 nodes to 30–130 bins.

### 1.2 Invariants — hard code paths

- **Elevation is terrain-sampled, never barometric.** Enforced at ingest, asserted here: if the bundle
  carries an `elevation_source` field that is not `terrain`, raise `BundleIncomplete`.
- **Never invent an aid station.** Aid stations are read from the bundle verbatim. If a leg has none, the
  aid-station list for that leg is empty and Stage 5 emits no actions on it — it does not synthesise one at
  a plausible interval.
- **Never smooth a climb.** No moving average, no Savitzky–Golay, no spline, no gradient clipping. The
  node series is used as delivered. A model that smoothed would systematically under-predict climbing time,
  which is the failure mode this invariant exists to prevent.
- **Zero barriers is a data error**, not a solvable plan: raise `BundleIncomplete`.
- Barrier chronology is asserted monotonic in `limit_minutes_from_start`. A bundle with a bike cut-off
  before the swim exit is corrupt.

### 1.3 Clamps

Node gradient is clamped to **[−0.30, +0.30]** before use. This is not smoothing — it is a guard against a
single bad terrain sample producing a 400% gradient that makes a segment unrideable. A clamped node sets a
`terrain_quality` flag on the segment which propagates to the plan's provenance display. If more than 2% of
a leg's nodes clamp, raise `BundleIncomplete`: the elevation series is not fit for purpose.

### 1.4 Worked example — segment aggregation

Course `C-TRAM`, bike leg, the named climb "Coll de Femenia", `from_km = 51.6`, `to_km = 60.0`.
Five nodes delivered by the bundle:

| j | `s_j` (m) | `h_j` (m) |
|---|---|---|
| 0 | 51600 | 62 |
| 1 | 53700 | 168 |
| 2 | 55800 | 302 |
| 3 | 57900 | 421 |
| 4 | 60000 | 549 |

```
d_seg  = 60000 − 51600 = 8400 m = 8.4 km
node gradients: 106/2100 = 0.05048
                134/2100 = 0.06381
                119/2100 = 0.05667
                128/2100 = 0.06095
   all within [−0.30, 0.30] → no clamping
Δh_seg = 106 + 134 + 119 + 128 = 487 m   (all positive; gain = net here)
g_seg  = (549 − 62) / 8400 = 487 / 8400 = 0.057976  → 5.80%
h_mean = [ (62+168)/2·2100 + (168+302)/2·2100 + (302+421)/2·2100 + (421+549)/2·2100 ] / 8400
       = [ 241500 + 493500 + 759150 + 1018500 ] / 8400
       = 2512650 / 8400 = 299.1 m
```

Which reproduces the product's published "Coll de Femenia, 8.4 km at 5.8%" from the node series, as a
consequence of the data rather than as a stored label.

---

## §2 Stage 2 — Read athlete constraints (~0.3 s)

> **The two threshold definitions, stated once so an implementer cannot get them wrong:**
>
> **`run_threshold_pace` is the pace the athlete could hold in an all-out ONE-HOUR race**, in seconds per
> kilometre — the running equivalent of FTP. It is *not* 10 km pace, *not* lactate-threshold pace, and
> *not* marathon pace. §2.5 gives the conversion from a recent race result and the exact onboarding wording.
>
> **`swim_threshold_pace` is Critical Swim Speed**, in seconds per 100 m, as derived from the standard
> 400 m / 200 m time-trial pair: `CSS = 200 / (t₄₀₀ − t₂₀₀)`. CSS is an **asymptotic** threshold speed, not
> the pace at any particular distance. §4.4 models it as such.
>
> These two definitions are *not* the same kind of quantity, and §4.4 explains why that matters: a one-hour
> anchor is a point on a distance–time curve, while an asymptote is its slope. Modelling both with the same
> Riegel decay would be wrong, and an earlier draft of this document did exactly that.

### 2.1 What this stage computes

Loads all eight constraints with their `source`, validates them against plausibility ranges, converts to
internal SI-ish units, and derives the equipment parameters.

**No branch in this stage or any later stage reads `constraint.source`.** Provenance is carried, never
consulted. This is asserted by a CI test that mutates every `source` field in a golden input and requires
byte-identical numeric output.

### 2.2 Plausibility ranges

These live in `solver/tables/plausibility.py` and are shared with the API layer, which enforces them and
returns `INVALID_INPUT` (Part 13 §13.1) before a solve is ever attempted. The solver re-asserts them as a
defensive postcondition.

| Key | Unit | Min | Max | Basis |
|---|---|---|---|---|
| `swim_threshold_pace` | s·(100 m)⁻¹ | 60 | 240 | 60 = world-record territory; 240 = 4:00/100 m, slower than any cut-off permits |
| `bike_threshold_power` | W | 80 | 500 | 500 W FTP is beyond any age-grouper |
| `run_threshold_pace` | s·km⁻¹ | 180 | 540 | 3:00/km to 9:00/km |
| `weight` | kg | 35 | 200 | |
| `sweat_rate` | L·h⁻¹ | 0.3 | 3.0 | Baker 2017 reports ~0.5–2.0 L·h⁻¹; widened for tails |
| `sodium_loss` | mg·L⁻¹ | 200 | 2200 | Baker 2017: 10–90 mmol·L⁻¹ = 230–2070 mg·L⁻¹ |
| `gut_carb_ceiling` | g·h⁻¹ | 20 | 120 | 120 = the evidenced hard maximum (§5.2) |
| `caffeine_tolerance` | mg | 0 | 600 | |

A **missing** required constraint raises `MissingConstraint` naming the key. Never a silent default. The
eight are all required. Four inputs are **optional** and each has a documented fallback and an
`assumed_fields` entry when it is absent: `bike_setup` (§I.2.3), `forecast.pressure_hpa` (§I.1.1),
`forecast.cloud_cover_pct` (§I.1.3), and `sweat_rate.measured_at_temp_c` (§5.2). All four are specified in
§F.

### 2.3 Derived quantities

```
m_total   = weight + bike_kit_mass[level]
CdA       = clamp(base[position] + level_adj[level] + helmet_adj[helmet], 0.19, 0.38)
FTP_alt   = bike_threshold_power · alt_factor(h_mean_bike_leg)
d_thresh  = 3600 / run_threshold_pace          # km covered in 1 h at threshold
```

`d_thresh` is the distance the athlete covers in one hour at threshold, and it is the anchor point for the
Riegel extrapolation in §4.3.1. It is meaningful **only** under the one-hour definition stated at the head
of this section. §2.5 exists to make sure all three entry routes produce that quantity and not a
near-neighbour of it.

Note there is deliberately no `d_thresh` analogue for the swim. CSS is an asymptote, so it has no
characteristic distance; §4.4 uses the critical-speed model directly rather than an anchor distance.

### 2.4 Worked example — Athlete M

The canonical athlete used throughout this document.

| Key | Value | Unit | Source |
|---|---|---|---|
| `swim_threshold_pace` | 105 | s·(100 m)⁻¹ | `tested` |
| `bike_threshold_power` | 224 | W | `tested` |
| `run_threshold_pace` | 282 | s·km⁻¹ | `tested` |
| `weight` | 75 | kg | `measured` |
| `sweat_rate` | 1.1 | L·h⁻¹ | `estimated` |
| `sodium_loss` | 900 | mg·L⁻¹ | `estimated` |
| `gut_carb_ceiling` | 75 | g·h⁻¹ | `tested` |
| `caffeine_tolerance` | 300 | mg | `manual` |
| `level` | `improver` | | |
| `bike_setup` | `tt_bike` / `standard` helmet | | `manual` |

All eight inside range. Derived:

```
m_total  = 75 + 10.0                       = 85.0 kg
CdA      = 0.255 + 0.000 + 0.000           = 0.255 m²
FTP_alt  = 224 · alt_factor(120 m)
         = 224 · (1 − 0.010·120/1000)
         = 224 · 0.99880                   = 223.731 W
d_thresh = 3600 / 282                      = 12.7660 km
```

Note `sweat_rate` and `sodium_loss` are `estimated`. They are used at exactly these values. The plan's
fluid and sodium numbers in §5.5 carry full weight from an estimate, and the UI shows lower trust. That is
the contract working as designed.


### 2.5 The three entry routes for `run_threshold_pace`, and how each derives it

A provenance stamp is honest about *where* a number came from. It says nothing about whether two numbers
are the same quantity. If the typed route means one-hour pace, the estimator means 10 km pace and the
upload route means something else again, then `measured` and `estimated` values are not comparable, every
back-test mixes two populations, and the calibration table in §C.3 will chase a constant that is not
actually wrong. All three routes must produce the one-hour quantity.

#### 2.5.1 Route 1 — typed directly

The athlete enters a pace. The UI must state the definition inline, not in a tooltip.

#### 2.5.2 Route 2 — converted from a recent race result

Most athletes do not know their one-hour pace but do know a recent race. The conversion uses **the same
Riegel exponent the model itself uses**, so the anchor and the extrapolation are inverses of each other and
round-trip exactly. Given a recent race of `d_race` km in `t_race` seconds:

```
d_1h                = d_race · (3600 / t_race) ^ (1 / r[level])
run_threshold_pace  = 3600 / d_1h
```

Derivation: Riegel says `t_race · (d_1h / d_race)^r = 3600`; solve for `d_1h`. Because §4.3.1 applies
`(d_run / d_thresh)^(r−1)` with the same `r`, an athlete who enters a race result and then races that same
distance gets their actual result back — a property worth an explicit unit test.

**The asymmetry, which is the reason this helper must exist.** Accepting a 10 km pace directly as threshold
pace is not a small approximation applied evenly; its size depends on how fast the athlete is, because a
10 km race lasts a very different time for each of them:

| Athlete | Recent race | Race pace | Derived `d_1h` | Correct threshold pace | Error if the race pace were used directly |
|---|---|---|---|---|---|
| Fast (`experienced`, r 1.06) | 10 km in 35:00 | 210 s·km⁻¹ | 16.63 km | 216.5 s·km⁻¹ | **3.00% optimistic** |
| Mid (`improver`, r 1.07) | 10 km in 45:00 | 270 s·km⁻¹ | 13.09 km | 275.1 s·km⁻¹ | **1.86% optimistic** |
| Slow (`first`, r 1.08) | 10 km in 55:00 | 330 s·km⁻¹ | 10.84 km | 332.1 s·km⁻¹ | 0.64% optimistic |
| Slow (`first`, r 1.08) | 10 km in 60:00 | 360 s·km⁻¹ | 10.00 km | 360.0 s·km⁻¹ | **0.00%** |

For the slowest athlete a 10 km race *is* a one-hour race, so the two coincide exactly. For a fast athlete
a 10 km race lasts 35 minutes and is materially quicker than anything they could hold for an hour, so
accepting it directly biases them optimistic by 3% — which compounds through `D_dist` into a marathon
projection and lands on a cut-off margin. **The bias runs the wrong way: it is largest for the athletes who
race closest to their limits.**

The helper works in both directions. A half-marathon result converts *conservatively* (an `improver` half in
1:40:00 gives 275.0 s·km⁻¹ against a race pace of 284.4, a 3.4% correction the other way), and a marathon
result corrects by 8.8%. Accept any race distance from 3 km to marathon; below 3 km the Riegel form is
unreliable and the input should be refused rather than converted.

#### 2.5.3 Route 3 — derived from an uploaded file

Post-race and training-file uploads must apply one rule, not a heuristic per file type:

```
1. Find the single longest continuous effort in the file of duration ≥ 20 min and ≤ 90 min
   whose pace coefficient of variation is < 5% (i.e. an actual sustained effort, not a session average).
2. Take its distance d_eff (km) and duration t_eff (s).
3. Apply the §2.5.2 conversion with the athlete's r[level].
4. If no qualifying effort exists, DO NOT derive a value. Leave the constraint as it was
   and record why. A derived value from a 12-minute interval or a 4-hour ride is worse
   than no value, because it will carry a `measured` stamp.
```

Step 4 matters more than the rest. The calibration service (Build Spec §6.4) writes constraints with
`source = 'measured'`; a bad derivation therefore *upgrades* provenance while *degrading* the number, which
is the worst combination the product can produce. The 20–90 minute window brackets the one-hour anchor
closely enough that the Riegel correction stays small.

#### 2.5.4 Required onboarding wording

The estimator must elicit the one-hour quantity explicitly. Copy is a product decision, but the *semantics*
are not, and the following is the minimum that makes the three routes commensurable:

> **Threshold running pace**
> The pace you could hold in an all-out race lasting about one hour — roughly a 15 km to half-marathon
> effort for most people. Not your easy pace, and not a pace you could only hold for 20 minutes.
>
> *Don't know it?* Enter a recent race instead and we'll work it out. → [distance] [time]
>
> Whichever you enter, this is stored as **one-hour threshold pace**.

The last line is not decoration. It is what makes the stored number self-describing when it is read back
months later by the drift service, the coach view, or a back-test.

**Confidence.** The definition is settled by decision, not by evidence, so it carries no confidence rating.
The *conversion* inherits the confidence of `riegel_r[level]` (Low-Med, §4.3.1) — but note it is used here
over a much shorter extrapolation (10 km → ~13–17 km) than in §4.3.1 (13 km → 42 km), so the error it
contributes at this step is small.

---

## §3 Stage 3 — Protect the barriers (~1.1 s)

### 3.1 Why this stage runs first

The contract puts barrier protection before optimisation, and the reason is a product one: a plan that is
beautifully optimised and misses a cut-off is worthless, whereas a plan that is ugly and makes every cut-off
is a finish. This stage establishes what is *possible* before Stage 4 decides what is *good*.

### 3.2 The intensity grid — and a correction to the obvious approach

The naive reading of "evaluate every barrier against the athlete's maximum sustainable output" is to
construct one maximum-effort profile and check every barrier against it. **That is wrong, and the model
itself demonstrates why.**

For Athlete M on course `C-TRAM` in hot conditions:

| Profile | Bike | Run | Total |
|---|---|---|---|
| Planned, IF 0.70 | 406.11 min | 270.57 min | **762.75 min** |
| "Maximum", IF 0.80 | 376.93 min | 310.65 min | **773.65 min** |

Riding at maximum sustainable intensity saves 29.2 minutes on the bike and loses 40.1 minutes on the run,
for a **net loss of 10.9 minutes to the finish**. The maximum-output profile reaches the bike cut-off sooner
and the *finish* later. Checking the finish barrier against it would wrongly declare feasible plans
infeasible — precisely the over-biking failure mode the product exists to prevent, showing up inside the
feasibility check.

**Therefore: each barrier is evaluated against the minimum ETA achievable at that barrier, taken
independently per barrier over a fixed intensity grid.**

```
IF_grid = [ IF_ref[level][distance] − 0.20,  …,  IF_max_feas[level] ]  in steps of 0.005
          clamped below at 0.50

for each barrier b:
    eta_min[b], IF_at_min[b] = min over IF_grid of eta(b, IF)
```

Ties in `eta` are broken toward the **lowest IF** (most conservative). This matters for the swim-exit
barrier, whose ETA does not depend on bike intensity at all: every grid point ties, and the tie-break makes
the reported `IF_at_min` deterministic rather than an artefact of grid order.

Grid size is 61–71 points depending on level. Evaluated over the gradient histograms of §1.1 this is
**measured at 105 ms** in pure Python — inside the 1.1 s target with an order of magnitude to spare. Note
that the same grid evaluated per elevation node costs 2.36 s and would *not* fit; see §0.8.

The maximum-effort profile also applies a **transition hurry factor** of `0.85` to T1 and T2. An athlete
racing a cut-off does move through transition faster, but not by an unbounded amount — they still have to
find their bag. `0.85` is an estimate (**Low confidence**), worth about 2 minutes at full distance.

| Constant | `first` | `improver` | `experienced` | Source |
|---|---|---|---|---|
| `IF_max_feas` | 0.75 | 0.80 | 0.85 | Top of the published raceable band per level, +0.05 |
| `transition_hurry_factor` | 0.85 | 0.85 | 0.85 | **Estimate** |

### 3.3 Feasibility verdict

```
margin_min[b]        = limit[b] − eta_min[b]
feasible             = all( margin_min[b] ≥ 0 )
```

If any `margin_min[b] < 0`, the plan is infeasible and Stage 4 does not run. Return
`Infeasibility(barrier, miss_minutes, levers, tightest_barrier, tightest_miss_minutes)` with:

```
missed       = [ b for b in barriers if margin_min[b] < 0 ]
barrier      = min(missed, key = limit_minutes_from_start)   # the EARLIEST missed — see below
miss_minutes = -margin_min[barrier]                          # rounded to 0.1 min

tightest_barrier      = argmin over ALL b of margin_min[b]   # retained for diagnostics
tightest_miss_minutes = -margin_min[tightest_barrier]
```

**This is a deliberate change to the contract, applied on review.** The Build Spec (Part 5 §5.2, Stage 3)
specifies the *tightest* barrier. That is the wrong one to report, for a concrete reason: timing errors
accumulate along a race, so an athlete who misses a mid-race bike cut-off necessarily misses the finish by
more. "Tightest by margin" therefore almost always names the **finish**, while the athlete's race actually
ends at the bike cut-off, hours earlier.

Golden case `G14-EARLIESTMISS` pins exactly this down. Athlete `A-Y` on `C-TRAM` misses two barriers:

| Barrier | Limit | Minimum ETA | Miss |
|---|---|---|---|
| **Bike cut-off** | 630.0 | 640.1 | **10.1 min** — earliest missed, and what is reported |
| Finish | 960.0 | 1091.8 | 131.8 min — tightest, and materially misleading |

Told "you miss the finish by 132 minutes", this athlete would reasonably conclude the race is far out of
reach. Told "you miss the bike cut-off by 10 minutes", they learn the truth: the race ends mid-bike, and ten
minutes is a gap that a winter of work — or a flatter race — genuinely closes. The two framings lead to
opposite decisions, which is what makes this worth changing the contract for.

`worst_margin_minutes` keeps its contract meaning — the minimum margin across all gates, i.e. the tightest —
because that is what drives `margin_state` and the drift thresholds in §3.5, and redefining it would ripple
into the notification logic. Both barriers are returned, so nothing is lost: `tightest_barrier` remains
available to the admin blast-radius view, which genuinely does want the worst case.

**Levers are computed at the reported (earliest missed) barrier**, not the tightest. This follows
necessarily: the levers must change *the outcome the athlete was told about*. Offering "raise FTP" because
it would help a finish the athlete will never reach would be advice about a hypothetical race.

Precise replacement text for the Build Spec is in **§F.5**.

### 3.4 Levers

`levers` names one or two concrete changes that would alter the outcome. Computed by **one-at-a-time
numerical sensitivity**, which is deterministic, cheap and directly explainable:

```
for each lever-eligible constraint c:
    perturb c by +5% in the improving direction
    Δ[c] = eta_min[barrier] (perturbed) − eta_min[barrier] (base)      # negative = helps
rank by Δ ascending; emit the top 1–2 whose Δ magnitude ≥ lever_significance_minutes
```

`barrier` in that expression is the **reported** barrier from §3.3 — the earliest missed one, not
the tightest.

`lever_significance_minutes = 2.0`. If no lever clears it, emit `lower_goal` alone — an honest "nothing
you can change before race day closes this gap."

Lever-eligible constraints and their emitted keys: `bike_threshold_power` → `raise_ftp`,
`run_threshold_pace` → `improve_run_pace`, `swim_threshold_pace` → `improve_swim_pace`, `weight` →
`reduce_weight`, plus the always-available `lower_goal`. `sweat_rate`, `sodium_loss`, `gut_carb_ceiling` and
`caffeine_tolerance` are **not** lever-eligible: they do not enter the time model, so perturbing them
returns zero and offering them would be dishonest.

**Cost, and one stated approximation:** each perturbed constraint is evaluated at `IF_at_min[barrier]` —
the IF that produced the base minimum — rather than by re-running the whole grid. Re-optimising per lever
would cost 4 × 105 ms; this costs ~7 ms in total. The optimum IF moves negligibly under a 5% perturbation,
and the approximation affects only the *ranking* of levers, never any number in a plan. See §0.8.

Measured for Athlete M at the finish barrier (each constraint improved by 5%):

| Constraint | Δ finish ETA | Rank |
|---|---|---|
| `run_threshold_pace` | **−14.45 min** | 1 → `improve_run_pace` |
| `bike_threshold_power` | **−11.04 min** | 2 → `raise_ftp` |
| `swim_threshold_pace` | −3.24 min | 3 (emitted only if top two are unavailable) |

### 3.5 Gates, margins and the binding key

For the **planned** profile that Stage 4 produces (not the grid minimum):

```
margin_minutes[b] = limit[b] − eta_planned[b]
load_pct[b]       = 100 · eta_planned[b] / limit[b]
worst_margin_minutes = min over b of margin_minutes[b]
```

Margin state, with the interval boundaries specified exactly:

| State | Condition |
|---|---|
| `clear` | `worst_margin_minutes ≥ 20.0` |
| `tight` | `0.0 ≤ worst_margin_minutes < 20.0` |
| `bad` | `worst_margin_minutes < 0.0` |

Both boundaries are **closed from above**: exactly 20.0 is `clear`, exactly 0.0 is `tight`. Comparison is
against the value rounded to 0.1 min, so a plan does not flicker between states on a float-representation
difference.

**`binding_constraint_key` resolution order**, fixed and configured in `solver/tables/precedence.py`:

1. If any gate's `margin_minutes < 20.0` → `barrier:<name>` of the tightest gate. A cut-off in play outranks
   everything: it is what the athlete needs to know.
2. Otherwise, the binding key of the quantity that determined the largest leg by projected time — in
   practice `bike_threshold_power` or `run_threshold_pace`, whichever leg is longer.
3. Otherwise `model:if_ceiling`.

### 3.6 Worked example — Athlete M, course `C-TRAM`, hot forecast

Forecast 31 °C / 55% RH / clear / 3 m·s⁻¹ wind / water 22.5 °C. Barriers per the product's published
structure. Grid scan over IF ∈ [0.50, 0.80] step 0.005, hurry factor 0.85:

| Barrier | Limit | min ETA | at IF | Slack | Verdict |
|---|---|---|---|---|---|
| Swim exit | 140.0 | 71.07 | 0.500 (tie) | +68.93 | feasible |
| Bike km 120 | 510.0 | 345.72 | 0.800 | +164.28 | feasible |
| Bike cut-off | 630.0 | 455.65 | 0.800 | +174.35 | feasible |
| Finish | 960.0 | 760.50 | **0.700** | +199.50 | feasible |

All four feasible → Stage 4 proceeds. Note the finish's minimum sits at IF 0.700, not 0.800: the grid finds
the over-biking penalty on its own.

Gates against the Stage 4 planned profile (IF 0.700, full-length transitions):

| Gate | Limit | ETA | Margin | Load | State |
|---|---|---|---|---|---|
| Swim exit | 140.0 | 71.1 | +68.9 | 50.8% | clear |
| Bike km 120 | 510.0 | 369.7 | +140.3 | 72.5% | clear |
| Bike cut-off | 630.0 | 486.2 | +143.8 | 77.2% | clear |
| Finish | 960.0 | 762.8 | +197.2 | 79.5% | clear |

`worst_margin_minutes = +68.9` → `margin_state = clear`. No gate under 20 min, so rule 1 does not fire;
the bike is the largest leg, so `binding_constraint_key = bike_threshold_power`.

---

## §4 Stage 4 — Solve pacing (~0.9 s)

### 4.1 The shape of the optimisation, and why it is not a free search

The most important design decision in this stage: **the solver does not search freely over intensity.**
`IF_ref[distance][level]` is the evidence-based target, and the solver departs from it only when a named
constraint requires it. What the solver actually optimises is the *distribution* of effort across terrain,
subject to a variability ceiling.

Two reasons, and the second is the one that matters:

1. **Explainability.** Every number must name what bound it. "IF 0.70 because that is the published band for
   an improver at full distance, and no barrier required more" is an explanation. "IF 0.7043 because that
   was the argmin" is not.
2. **The free optimum coincides with `IF_ref` anyway** — and that is a consistency check on the model, not a
   coincidence. For Athlete M, raising IF above 0.70 gains bike time at −291.8 min per unit IF and loses
   run time at +400.8 min per unit IF, so the total-time derivative at `IF_ref` is +109.0 min per unit IF:
   strictly worse. Below `IF_ref` the run coupling term is inactive, so the bike simply gets slower with no
   compensation. `IF_ref` is a genuine interior optimum.

Worth stating honestly: **that second property depends on the over-biking coefficient `c₁`, which is the
least evidenced constant in this document.** For Athlete M the optimum stays at `IF_ref` for any
`c₁ ≥ 1.165`; at `c₁ = 1.6` there is reasonable headroom, but if calibration pushes `c₁` below ~1.2 the free
optimum moves above `IF_ref` and this design rationale weakens. Flagged in §C as a thing to re-check after
back-testing rather than assumed permanent.

### 4.2 Bike

#### 4.2.1 Target power per segment

```
P_base   = FTP · alt_factor(h_mean_leg) · IF_plan · bike_heat_factor(W)
IF_plan  = clamp( IF_ref[distance][level] + risk_adj[risk] + barrier_adj,  0.40,  IF_max_feas[level] )
P_i      = P_base · grade_mod(g_seg,i)          # ONE target per named segment, from NET gradient
```

**Power target is per segment; time is integrated over the segment's gradient histogram (§1.1).** The
athlete holds one number for the whole climb — that is what a raceable plan looks like and what the head
unit displays — but the *time* that target produces must be integrated over the real terrain inside it:

```
t_seg = Σ over bins b in hist_seg[i] of  distance[b] / solve_speed(P_i, …, g_bin[b], …)
```

Using `solve_speed(P_i, …, g_seg,i, …)` once for the whole segment would make the model 3–37% fast (§1.1).
This is the single most important implementation detail in Stage 4.

`barrier_adj` is zero unless Stage 3 found a gate that the planned profile misses, in which case IF is
raised along the grid to the lowest value that clears every gate, and `binding_constraint_key` becomes
`barrier:<name>`. This is the *only* mechanism that moves intensity above `IF_ref`.

**Gradient modulation** — Swain (1997) and Atkinson et al. (2007) show the optimal strategy is to vary power
in parallel with gradient, and Atkinson measured ~26 s saved over 40 km at 10% power variability:

```
grade_mod(g) = 1 + k_grade · tanh( g / g_scale )
```

`tanh` is chosen over a linear ramp with clamps because it is smooth everywhere, bounded by construction
(no clamp needed), monotonic, symmetric about zero, and has no knee to tune. Output is confined to
`[1 − k_grade, 1 + k_grade]` = `[0.88, 1.12]` for any gradient, including a bad terrain sample.

| Constant | Value | Source | Confidence | What would change it |
|---|---|---|---|---|
| `k_grade` | 0.12 | Atkinson's ~10% variability, rounded up slightly | Med | Back-testing climb splits |
| `g_scale` | 0.04 | **Estimate** — sets the gradient at which modulation reaches 76% of maximum | **Low** | Nothing published; pure shape parameter |

| `IF_ref` | `first` | `improver` | `experienced` | Scope |
|---|---|---|---|---|
| `full` | 0.65 | 0.70 | 0.75 | **Primary** — sourced |
| `half` | 0.72 | 0.78 | 0.83 | **Primary** — sourced, but contested (below) |
| `olympic` | 0.80 | 0.85 | 0.88 | ⚠️ Out of primary scope — **extrapolated, unvalidated** |
| `sprint` | 0.85 | 0.90 | 0.95 | ⚠️ Out of primary scope — **extrapolated, unvalidated** |

`risk_adj`: `conservative −0.03`, `balanced 0.00`, `aggressive +0.03`.

**Confidence, and the one disagreement that lands on a primary distance.** The `full` row is well supported:
multiple independent sources converge on 0.65–0.78 for age-groupers, derived from Allen & Coggan.

**The `half` row is contested, and this matters more than anything else in this table**, because 70.3 is one
of our two primary distances and we currently sit on one side of the disagreement by assumption. Allen &
Coggan are cited as **0.83–0.87**; TrainingPeaks guidance spans 0.72–0.85; most coaching sources put
age-groupers at 0.75–0.80. This model takes **0.78** for an improver — the lower, age-grouper-weighted
figure — on the reasoning that the higher band describes athletes racing for a result rather than athletes
trying to run well off the bike. That reasoning is plausible and unverified. The gap is worth roughly
**8 minutes over 90 km**, which is within a factor of three of the entire `clear`/`tight` margin band. It is
the reason **E-13 sits in the top verification tier alongside E-1** rather than at the bottom of the list.

`olympic` and `sprint` are extrapolated from the shape of the other two rows and are **out of primary scope**
per §0.1b — Low confidence, unvalidated, and not to be treated as evidence-backed.

#### 4.2.2 Variability ceiling

Normalised power at segment resolution — segments are all ≫ 30 s, so the usual 30 s rolling average
degenerates to the segment itself:

```
NP = ( Σ t_i · P_i⁴ / Σ t_i ) ^ 0.25
AP = Σ t_i · P_i / Σ t_i
VI = NP / AP
```

If `VI > VI_max[distance]`, scale `k_grade` down by a fixed sequence of factors
`[0.75, 0.50, 0.25, 0.0]`, recomputing until satisfied. Fixed sequence, not a search: deterministic,
terminates in at most four steps, and `k_grade = 0` (flat power) always satisfies any ceiling ≥ 1.0. If the
ceiling binds, `binding_key = model:vi_ceiling`.

| `VI_max` | `full` | `half` | `olympic` | `sprint` |
|---|---|---|---|---|
| | 1.05 | 1.06 | 1.08 | 1.10 |

**Honest note on what this ceiling actually does.** At segment resolution with `k_grade = 0.12`, Athlete M's
VI on a genuinely mountainous course comes out at **1.003** — the ceiling is nowhere near binding. That is
not because the plan is smooth; it is because segment-level modelling cannot see the sub-30-second surges
that produce real-world VI of 1.03–1.06. **The VI ceiling as specified is a safety rail against a
pathological configuration, not an active constraint**, and it should not be described to users as though
the model is managing their variability. Doing so would be a claim the mathematics does not support.

#### 4.2.3 Clamps

- `P_i ≥ 0` (a negative modulated target on a descent means freewheeling).
- `P_i ≤ FTP · if_segment_ceiling`, `if_segment_ceiling = 1.05`. A short steep ramp may exceed threshold
  briefly; a segment may not be planned at a sustained supra-threshold target. If this binds,
  `binding_key = model:if_segment_ceiling`.
- `v ≤ 75 km·h⁻¹` (§I.2.4).
- `IF_plan ≤ IF_max_feas[level]` — the contract's "no sustained power above threshold" bound, and it binds
  well below threshold for every level.

#### 4.2.4 Worked example — Athlete M, `C-TRAM`, hot

```
W                 = 27.729 °C                    (§I.1.3)
bike_heat_factor  = 0.95370                      (§I.1.4, clamped at top knot)
alt_factor(120 m) = 0.99880                      (§I.1.6)
IF_plan           = 0.70 + 0.00 + 0.00 = 0.700
P_base            = 224 × 0.99880 × 0.700 × 0.95370 = 149.361 W
ρ(31 °C, 55%, 120 m) = 1.13342 kg·m⁻³             (§I.1.1)
CdA + F_w         = 0.255 + 0.0044 = 0.2594 m²
m                 = 85.0 kg,  Crr = 0.005,  w = 3.0 m·s⁻¹
```

Three segments in full:

| Segment | `g` | `grade_mod` | `P_i` (W) | `v` (km·h⁻¹) | min |
|---|---|---|---|---|---|
| Coastal out | +0.002 | 1.005995 | 150.257 | 30.577 | 47.095 |
| **Coll de Femenia** | **+0.058** | **1.107483** | **165.415** | **10.665** | **47.256** |
| Femenia descent | −0.055 | 0.894421 | 133.592 | 65.002 | 6.646 |

Coll de Femenia arithmetic, shown fully:

```
tanh(0.058 / 0.04) = tanh(1.45)             = 0.895703
grade_mod          = 1 + 0.12 × 0.895703    = 1.107484
P_i                = 149.361 × 1.107484     = 165.415 W
P_wheel            = 165.415 × 0.976        = 161.445 W

bisection converges to v = 2.962566 m·s⁻¹ = 10.6652 km·h⁻¹; check the balance at that v:
  θ         = atan(0.058) = 0.0579351 rad,  sin θ = 0.0579027,  cos θ = 0.998322
  aero      = ½ × 1.133424 × 0.2594 × (2.962566² + 3.0²/2) × 2.962566
            = 0.147005 × (8.77680 + 4.5) × 2.962566            =   5.7822 W
  rolling   = 0.005 × 85 × 9.80665 × 0.998322 × 2.962566       =  12.3267 W
  gravity   = 85 × 9.80665 × 0.0579027 × 2.962566              = 142.9902 W
  bearings  = 2.962566 × (91 + 8.7 × 2.962566) × 10⁻³          =   0.3460 W
  Σ                                                            = 161.4452 W  = P_wheel ✓

time = 8.4 km / 10.6652 km·h⁻¹ × 60 = 47.256 min
```

The climb is 88.6% gravity — which is why an 8.4 km segment at 5.8% gets its own wattage target and why
treating it as "average terrain" would be badly wrong. This is the product's published segmentation
behaviour arising from the physics rather than from a stored label.

**Whole bike leg:** 180.2 km in **406.11 min** (6:46), `NP = 153.96 W`, `AP = 153.54 W`, `VI = 1.0028`,
realised `NP/FTP = 0.687` (below the planned 0.700 because heat derated the target — correct, and worth
surfacing in the "Why this?" drawer).

### 4.3 Run

#### 4.3.1 Pace chain

Multiplicative, each factor independently sourced and independently inspectable — which is what makes the
"Why this?" drawer possible:

```
pace_target = run_threshold_pace
            × D_dist          # distance decay from the 1-hour threshold anchor
            × D_bike          # cost of running off the bike, incl. over-biking
            × D_heat          # heat  (§I.1.5)
            ÷ alt_factor      # altitude (§I.1.6)
pace_i      = pace_target × D_grade(g_i)      # per segment
```

**Distance decay** — Riegel (1977), anchored on the 1-hour threshold distance from §2.3:

```
D_dist = ( d_run / d_thresh ) ^ (r − 1)
```

| `r` | `first` | `improver` | `experienced` | Source |
|---|---|---|---|---|
| | 1.08 | 1.07 | 1.06 | Riegel 1.06 baseline; Vickers & Vertosick (2016, n = 2303) found 1.06 optimistic at marathon for about half of recreational runners, so the two less-experienced tiers are raised |

The level split is **my inference** from Vickers & Vertosick, not a published table. **Low-Med confidence.**

**Bike coupling:**

```
D_bike = 1 + c₀[distance] + c₁ · max(0, IF_realised − IF_ref[distance][level])
```

| Constant | `full` | `half` | `olympic` | `sprint` |
|---|---|---|---|---|
| `c₀` | 0.08 | 0.05 | 0.03 | 0.02 |

`c₁ = 1.6` (all distances).

**This is the weakest-evidenced part of the model and it is load-bearing, so it gets a full paragraph.**
I found **no peer-reviewed dose–response** relating triathlon run pace to bike intensity factor. What exists
is coaching-derived: "exceed target IF by 0.03–0.05 and you run materially slower." The mechanism is not in
doubt — the bike sets the rate of glycogen depletion, and long-course run failure is overwhelmingly a
fuelling-and-pacing failure rather than a running-fitness failure — but the *coefficient* is an estimate.
`c₀ = 0.08` at full distance was chosen so that the model's cool-conditions triathlon run lands at +8% over
its own predicted open-marathon pace, consistent with the low end of the widely-quoted "10–15% slower than
an equivalent open marathon" band. `c₁ = 1.6` makes a +0.05 IF overshoot cost +8% of run pace, which is the
right order of magnitude for the folklore and, as shown in §4.1, places the free optimum at `IF_ref`.

Both are `estimated`. Both are top calibration targets in §C. Neither should be described to a user as
though it were measured.

#### 4.3.2 Gradient

Minetti et al. (2002), *Journal of Applied Physiology* 93:1039–1046 — metabolic cost of running as a
5th-order polynomial in gradient, J·kg⁻¹·m⁻¹, valid −0.45 ≤ `i` ≤ 0.45:

```
Cr(i) = 155.4·i⁵ − 30.4·i⁴ − 43.3·i³ + 46.3·i² + 19.5·i + 3.6
```

**Numerical check against the paper's own reported measurements** — the one place I could validate a
recalled equation without reading it:

| `i` | `Cr(i)` from the polynomial | Minetti's reported measurement | Agreement |
|---|---|---|---|
| +0.45 | 19.43 | 18.93 ± 1.74 | within 1 SD ✓ |
| −0.20 | 1.800 | 1.73 ± 0.36 (reported minimum) | within 1 SD ✓ |
| 0.00 | 3.60 | 3.40 ± 0.24 | polynomial intercept sits 6% above the measured level value |

Two independent reported values reproduced. The coefficients are almost certainly right. Still
**⚠ VERIFY** (§E-5), and note the intercept discrepancy: `Cr(0) = 3.6` is the polynomial's constant, while
the measured level cost was 3.40. Because `D_grade` is a *ratio* to `Cr(0)`, this cancels exactly and does
not affect any output.

**Cost ratio is not a pace ratio.** At `i = −0.20` the metabolic cost halves, but nobody runs a marathon
descent at double pace — eccentric loading and biomechanical speed limits intervene. So the conversion is
damped asymmetrically:

```
D_grade(i) = clamp( ( Cr(i) / Cr(0) ) ^ α ,  0.85,  2.00 )
α = α_up = 1.00   for i ≥ 0
α = α_dn = 0.50   for i < 0
```

| `i` | `Cr(i)/Cr(0)` | `D_grade` | Reading |
|---|---|---|---|
| +0.10 | 1.6578 | 1.658 | 66% slower |
| +0.05 | 1.3015 | 1.302 | 30% slower |
| 0.00 | 1.0000 | 1.000 | — |
| −0.05 | 0.7626 | 0.873 | 13% faster |
| −0.20 | 0.5000 | 0.850 (clamped) | 15% faster, clamped |

| Constant | Value | Source | Confidence |
|---|---|---|---|
| `alpha_up` | 1.00 | At constant effort, uphill metabolic cost converts near-fully to pace | Med |
| `alpha_dn` | 0.50 | **Estimate** — no published damping coefficient found | **Low** |
| `d_grade_min` | 0.85 | **Estimate** — biomechanical descent limit | **Low** |
| `d_grade_max` | 2.00 | **Estimate** — beyond ≈ +13% most long-course athletes walk | **Low** |

The `d_grade_max` clamp is a modelling convenience, not physiology: above it the athlete is walking, which
has a different cost curve (Minetti gives one) and is **not modelled**. See §D-9.

#### 4.3.3 Clamps

- `pace_target ≥ run_threshold_pace` — the contract's "no long-leg pace faster than threshold". Binds via
  `bind()` with key `run_threshold_pace`. It cannot bind at full or half distance, where `D_dist` alone
  exceeds 1; it can bind at sprint distance in cool conditions, which is correct — a sprint run *is* near
  threshold.
- Every `split_minutes` and `target_minutes` must be **> 0**. Asserted as a postcondition on every emitted
  segment, satisfying the contract's "never emit a negative split time".
- `D_heat ≤ 1.60` (§I.1.5).

#### 4.3.4 Worked example — Athlete M, `C-TRAM`, hot

```
d_thresh = 3600 / 282 = 12.7660 km
D_dist   = (42.195 / 12.7660) ^ (1.07 − 1)
         = 3.304634 ^ 0.07
         = exp(0.07 × ln 3.304634) = exp(0.07 × 1.195341) = exp(0.0836739) = 1.087273

implied open-marathon pace = 282 × 1.087273 = 306.615 s·km⁻¹
                           → 42.195 × 306.615 = 12 937.6 s = 3:35:38   (sanity: plausible ✓)

D_bike   = 1 + 0.08 + 1.6 × max(0, 0.700 − 0.700) = 1.080
cool triathlon pace = 306.615 × 1.080 = 331.144 s·km⁻¹

D_heat   = 1 + 0.0038461 × (27.72875 − 10)^1.3
         = 1 + 0.0038461 × 17.72875^1.3
         = 1 + 0.0038461 × 42.00313 = 1.161550

alt_factor(run leg mean elevation 25 m) = 1 − 0.010 × 25/1000 = 0.99975
   (the RUN leg's mean elevation, not the bike leg's — §I.1.6)

pace_target = 331.144 × 1.161550 / 0.99975 = 384.737 s·km⁻¹   (6:25·km⁻¹)
run time    = 42.195 × 384.737 / 60 = 270.57 min = 4:30:34
```

**Heat delta on the run = 384.737 − 331.227 = +53.5 s·km⁻¹** (both paces taken after the altitude divisor, so the delta is the heat effect alone). This is the number that supersedes the
product's published +9 s·km⁻¹ (§0.7).

Whole-race sanity: 3:35 open-marathon capability → 4:31 Ironman marathon in 31 °C. Total race 12:41 (761.5 min). Both
are squarely in the range real athletes of this profile produce in those conditions.

### 4.4 Swim

#### 4.4.1 CSS is an asymptote, and the model must treat it as one

Per the definition settled in §2, `swim_threshold_pace` is Critical Swim Speed from the 400/200 pair. In
the two-parameter critical-speed model, the athlete's maximal distance–time relationship is a straight line:

```
d = CSS · t + D′
```

`CSS` is its **slope** — the asymptotic speed as duration grows without bound — and `D′` is its intercept,
the finite distance available above that asymptote (the swimming analogue of W′). Rearranging for maximal
speed at a finite distance `d`:

```
v_max(d)    = CSS · d / (d − D′)          >  CSS  for all finite d
pace_max(d) = CSS_pace · (d − D′) / d     <  CSS_pace   (pace, so smaller is faster)
```

**Maximal swim pace at any race distance is therefore faster than CSS, not slower.** An earlier draft of
this document applied a Riegel-form decay anchored at `d_ref_swim = 2000 m`, which made every race
distance *slower* than CSS — the right shape for a one-hour anchor, the wrong shape for an asymptote, and
wrong by about 2.3% at full distance. That model is withdrawn.

The critical-speed model is a *maximal-effort* model, and it holds only over the duration range the
underlying test spans — conventionally up to about 30 minutes. Beyond that, real sustainable speed falls
below CSS rather than approaching it from above. So a durability term is applied past that window:

```
T_max_est   = d · pace_max(d) / 100 / 60                        [minutes]
excess_min  = max(0, T_max_est − css_validity_min)
pace_dur(d) = pace_max(d) · (1 + k_swim_dur · excess_min)

pace = ( pace_dur(d) × wetsuit_factor ) + ow_overhead[level] + water_temp_adj(T_water)
```

Order matters and is deliberate: the wetsuit multiplies *swimming* pace, while sighting is additive time
that a wetsuit does not reduce.

#### 4.4.2 What this produces, and why it is more credible than the model it replaces

For an athlete with CSS = 105 s·(100 m)⁻¹, before wetsuit and sighting:

| Race distance | `pace_max` | Est. duration | Excess over window | **Planned pace** | vs CSS |
|---|---|---|---|---|---|
| 750 m (sprint) | 102.90 | 12.9 min | 0.0 | **102.90** | −2.10 s |
| 1500 m (olympic) | 103.95 | 26.0 min | 0.0 | **103.95** | −1.05 s |
| 1900 m (70.3) | 104.17 | 33.0 min | 3.0 | **104.55** | −0.46 s |
| 3800 m (full) | 104.59 | 66.2 min | 36.2 | **109.13** | **+4.13 s** |

These land on the standard coaching heuristics without being fitted to them: a 70.3 swim is raced at
approximately CSS, a full-distance swim at roughly CSS + 4 s·(100 m)⁻¹, and the short-course swims
slightly faster than CSS. That the corrected physical form reproduces the coaching consensus, while the
withdrawn Riegel form did not, is the main reason for confidence in the change.

#### 4.4.3 Constants

| Constant | Value | Source | Confidence | What would change it |
|---|---|---|---|---|
| `d_prime_m` (`D′`) | 15.0 m | Population-typical swimming `D′`. **Estimate** | **Low — but low-stakes** | See sensitivity note below |
| `css_validity_min` | 30.0 min | Conventional upper duration of the critical-speed model's validity | **Low-Med** | A study of CS-model validity limits in swimming |
| `k_swim_dur` | 0.0012 per min | **Estimate**, calibrated so a 3800 m swim lands at ≈ CSS + 4 s·(100 m)⁻¹ | **Low** | Back-testing swim splits — the single most useful swim calibration |
| `wetsuit_factor` | 0.955 | Chatard & Wilson: ~6–7% over 400 m for triathletes, 14% drag reduction at 1.25 m·s⁻¹; discounted for sustained long-course pace | Med | Back-testing; the 400 m figure is a max-effort short-distance result |
| `ow_overhead['first']` | 12.0 s·(100 m)⁻¹ | Coaching consensus 5–15 s·(100 m)⁻¹; sighting alone 3–5 | **Low** | Back-testing |
| `ow_overhead['improver']` | 8.0 | as above | **Low** | as above |
| `ow_overhead['experienced']` | 5.0 | as above | **Low** | as above |

**`D′` sensitivity is low, which is why a population default is acceptable.** Across the plausible range
`D′` ∈ [10, 25] m, planned pace moves by 0.4% at 3800 m and 2.0% at 750 m — an order of magnitude smaller
than the constants it sits beside. It matters most where it matters least, on the short-course distances
that are out of primary scope.

**`D′` is nonetheless recoverable, and should be.** It falls straight out of the test the athlete already
performed: with two time-trial points, `D′ = 400 − CSS·t₄₀₀ = 200 − CSS·t₂₀₀`. The product currently stores
only the derived CSS and discards `t₄₀₀`/`t₂₀₀`. Persisting the raw pair would replace this estimate with a
measurement at zero cost to the athlete. That is a fifth candidate input beyond the four specified in §F;
it is **not** added here because it was not requested, and it is recorded as **§D-12** instead.

**Wetsuit legality** — Ironman competition rules, High confidence:

| Water temperature | Rule | Model behaviour |
|---|---|---|
| `< 16.0 °C` | Wetsuit mandatory | `wetsuit = True` |
| `16.0 – 24.5 °C` | Wetsuit permitted | `wetsuit = True` |
| `24.55 – 28.77 °C` | Permitted, not award-eligible | `wetsuit = False` + warning flag |
| `> 28.77 °C` | Prohibited | `wetsuit = False` |

The 24.55–28.77 °C band assumes an athlete racing for a result. That is an assumption about intent, not
physiology, and a first-timer might reasonably choose the wetsuit and forgo awards. It is exposed as
`options`-level behaviour rather than hard-coded. Note also that these thresholds are **genuinely
discontinuous** — 24.5 °C and 24.6 °C produce different equipment, hence a ~4.5% pace step. That
discontinuity is in the *rules*, not the model, and smoothing it would be wrong.

**Water temperature adjustment** — and this is an honest gap:

```
water_temp_adj(T_water) = c_cold · max(0, cold_threshold_c − clamp(T_water, 12.0, 40.0))
                        + c_warm · max(0, T_water − warm_threshold_c)
```

(`T_water` is the forecast's water temperature. Note it is **not** `T_w`, which §0.3 reserves for
psychrometric wet-bulb air temperature — an easy and consequential confusion in implementation.)

`c_cold = 0.8`, `c_warm = 1.0`, both s·(100 m)⁻¹·°C⁻¹, both **Low confidence estimates**.

**The direct effect of water temperature on swim speed within the triathlon-legal 16–28 °C range is
essentially unstudied.** Everything I could find is cold-water work at 10–16 °C, concerned with core
temperature and hypothermia rather than pace. The dominant water-temperature effect in the legal range is
the wetsuit legality step above, which *is* well documented. These two coefficients are placeholders that
produce a small, monotonic, sensible-signed effect. They should be treated as such. See §D-10.

**Drafting is deliberately not modelled.** The effect is large and well documented (15–25% energy saving on
feet), but the solver cannot know whether an athlete will find feet, and modelling it would mean inventing
an input. Its absence makes swim projections systematically slightly slow for strong swimmers who draft
well — a known, directional, documented bias rather than a hidden one.

**Worked example** — Athlete M, 3800 m, water 22.5 °C, `improver`:

```
pace_max    = 105 × (3800 − 15) / 3800
            = 105 × 0.9960526                     = 104.5855 s·(100 m)⁻¹
T_max_est   = 3800 × 104.5855 / 100 / 60          =  66.237 min
excess_min  = max(0, 66.237 − 30.0)               =  36.237 min
pace_dur    = 104.5855 × (1 + 0.0012 × 36.237)
            = 104.5855 × 1.0434844                = 109.1334 s·(100 m)⁻¹
                                                    (= CSS + 4.13 s, as expected)
22.5 °C ≤ 24.5 → wetsuit legal
wetsuit     = 109.1334 × 0.955                    = 104.2224
+ overhead  = 104.2224 + 8.0                      = 112.2224
water_temp_adj(22.5) = 0.8 × max(0, 18 − 22.5) + 1.0 × max(0, 22.5 − 26) = 0
swim time   = 38 × 112.2224 = 4264.45 s           =  71.07 min
```

The withdrawn model gave 69.81 min for this athlete. The correction is **+1.27 min**, and it propagates into
every gate ETA and the projected total below.

### 4.5 Transitions

Explicit segments with their own durations, per contract.

```
T1 = t1_base[distance][level] + (wetsuit_removal[level] if wetsuit else 0)
T2 = t2_base[distance][level]
```

| `t1_base` (min) | `first` | `improver` | `experienced` |
|---|---|---|---|
| `full` | 9.0 | 6.5 | 4.5 |
| `half` | 6.5 | 4.5 | 3.0 |
| `olympic` | 3.5 | 2.5 | 1.6 |
| `sprint` | 2.6 | 1.8 | 1.2 |

| `t2_base` (min) | `first` | `improver` | `experienced` |
|---|---|---|---|
| `full` | 8.0 | 6.0 | 4.0 |
| `half` | 5.0 | 4.0 | 2.5 |
| `olympic` | 2.5 | 2.0 | 1.2 |
| `sprint` | 2.0 | 1.5 | 1.0 |

`wetsuit_removal`: `first 3.0`, `improver 2.5`, `experienced 2.0` min.

**This is the lowest-confidence table in the entire model.** Public data on age-group transition times is
not merely imprecise, it is mutually contradictory: three sources describing the same population reported
"6–12 min combined for a 12-hour finisher", "17 min combined", and "26–29 min combined". None specifies
whether it includes the run from swim exit to the change tent, which at a large race is several minutes on
its own. These values are **reasoned estimates constrained to lie inside that contradictory range**, not
measurements. At full distance they total 9.0 min (T1, with wetsuit) + 6.0 min (T2) for an improver.

Transitions are the **first thing to recalibrate** from real race data (§C), because unlike every other
constant here they can be read directly off a results page with no modelling assumptions at all.

---

## §5 Stage 5 — Balance fuelling (~0.4 s)

### 5.1 Carbohydrate

```
carb_target = L(duration_hours)
carb_planned, binding = bind([
    ('model:carb_duration_target', L(duration_hours), UPPER),
    ('gut_carb_ceiling',           gut_carb_ceiling,  UPPER),
])
```

`L(t)`, the duration-based literature target — piecewise-linear and continuous, from Jeukendrup (2014),
*Sports Medicine* 44(S1):S25–S33:

| Duration `t` (h) | `L(t)` (g·h⁻¹) |
|---|---|
| `t ≤ 1` | 30 |
| `1 < t ≤ 2` | `30 + 30·(t − 1)` |
| `2 < t ≤ 2.5` | 60 |
| `2.5 < t ≤ 4` | `60 + 30·(t − 2.5)/1.5` |
| `t > 4` | 90 |

Continuity at every knot is by construction: `L(1) = 30`, `L(2) = 60`, `L(2.5) = 60`, `L(4) = 90`.

`duration_hours` is bike + run **moving time only** — not swim, not transitions. Carbohydrate is not
ingested at a meaningful rate during a swim, and including it would inflate `total_carb_g` beyond what the
plan actually asks the athlete to consume.

**Override.** If `options.carb_override` is present (which requires the calling service to have written an
`override_events` row first):

```
carb_planned = min(carb_override, carb_hard_max)
overridden   = True
binding      = 'model:carb_hard_max' if clamped else 'options:carb_override'
```

`carb_hard_max = 120 g·h⁻¹` — the highest rate with published oxidation evidence (Podlogar et al. 2022,
120 vs 90 g·h⁻¹ fructose–maltodextrin). **The override cannot exceed it.** An override is a statement that
the athlete knows their gut better than the stored constraint; it is not a statement that they have
repealed intestinal transport.

**Transporter constraint.** Above 60 g·h⁻¹ a single carbohydrate source cannot be oxidised (SGLT1
saturates). The plan must therefore specify a glucose:fructose mix of at least 2:1 when
`carb_planned > 60`. This is an output flag, `requires_multiple_transportable = carb_planned > 60`, which
Stage 6 uses to select products. It is not a numeric adjustment.

| Constant | Value | Source | Confidence |
|---|---|---|---|
| `carb_single_transporter_max` | 60 g·h⁻¹ | Jeukendrup 2014 — SGLT1 saturation | High |
| `carb_ultra_target` | 90 g·h⁻¹ | Jeukendrup 2014 | High |
| `carb_hard_max` | 120 g·h⁻¹ | Podlogar & Wallis 2022; Podlogar et al. 2022 | Med — the authors call the benefit above 90 "speculative" |
| `glucose_fructose_ratio` | 2:1 | Jeukendrup 2014; GSSI SSE-108 | High |

**A reality check worth carrying into validation.** Pfeiffer et al. (2012), *MSSE*, measured what Ironman
athletes actually consume: **62 ± 26 g·h⁻¹** at IM Hawaii and **71 ± 25 g·h⁻¹** at IM Germany. The
literature *recommendation* of 90 g·h⁻¹ is at roughly the 80th percentile of observed behaviour. The model
does not apply a level-based discount to reach those observed numbers — the `gut_carb_ceiling` constraint is
the mechanism for athlete-specific limits, and adding a second discount would double-count and would
violate the "estimated constraints carry full weight" rule. But if back-testing shows plans routinely
prescribing 90 g·h⁻¹ to athletes whose ceiling is `estimated`, the problem is in the onboarding estimator's
default, not here. Pfeiffer's band is the right yardstick for that check.

### 5.2 Fluid

Sweat rate rises with heat stress, and the stored constraint was measured under unknown conditions:

```
sweat_eff = sweat_rate · (1 + k_sweat · max(0, W − W_sweat_ref))

fluid_ml_per_hr, binding = bind([
    ('sweat_rate',                    sweat_eff · 1000 · replace_frac, UPPER),
    ('model:gastric_emptying_cap',    gastric_cap_ml,                  UPPER),
])
```

| Constant | Value | Source | Confidence | What would change it |
|---|---|---|---|---|
| `k_sweat` | 0.030 per °C WBGT | **Estimate.** No clean published %-per-°C coefficient found | **Low** | A published sweat-rate-vs-WBGT regression |
| `W_sweat_ref` | 15.0 °C | **Estimate** — assumed condition under which a sweat test was run | **Low** | Capturing test conditions alongside the constraint |
| `replace_frac` | 0.75 | Targets ≤2% body-mass loss rather than full replacement; ACSM cautions against both under- and over-drinking | Med | |
| `gastric_cap_ml` | 1000 mL·h⁻¹ | Gastric emptying maximum ~15–20 mL·min⁻¹ | Med | |

**`sweat_rate.measured_at_temp_c` (new input, §F.4) removes the guess.** When supplied, the athlete's own
test condition replaces the assumed reference, converted to the WBGT axis the scaler works on:

```
W_sweat_ref = wbgt(measured_at_temp_c, sweat_test_default_rh, sweat_test_default_conditions)
```

with `sweat_test_default_rh = 55%` and `sweat_test_default_conditions = 'partly_cloudy'`, because a sweat
test records a temperature but almost never a humidity or a sky state. Those two defaults are themselves
estimates and are config rows; the gain over the status quo is that the *temperature* — the term that
dominates — becomes a measurement instead of an assumption.

**When absent**, fall back to `w_sweat_ref = 15.0 °C WBGT` and add `sweat_rate.measured_at_temp_c` to
`SolveOutput.assumed_fields`, so the UI can mark the fluid and sodium numbers as resting on an assumed test
condition. This matters more than it looks: an athlete who sweat-tested on a hot day and one who tested
indoors in winter currently get identical treatment from identical stored values, and the resulting fluid
plan can differ by 20% or more between those two readings of the same number.

Note that `replace_frac = 0.75` is deliberately **not** 1.0. Full sweat replacement over a 9-hour race is
both unachievable (it would exceed gastric capacity) and dangerous (exercise-associated hyponatraemia is a
real cause of long-course medical events, and it is caused by drinking too much, not too little).

### 5.3 Sodium

Two independent floors; the binding one is reported:

```
na_from_losses = sodium_loss · sweat_eff · replace_frac_na
na_acsm_floor  = acsm_min_g_per_l · (fluid_ml_per_hr / 1000) · 1000

sodium_mg_per_hr, binding = bind([
    ('sodium_loss',           na_from_losses, LOWER),
    ('model:acsm_sodium_floor', na_acsm_floor, LOWER),
])   then clamped to [sodium_min, sodium_max]
```

| Constant | Value | Source | Confidence |
|---|---|---|---|
| `replace_frac_na` | 0.80 | Sodium losses need not be fully replaced within a race | Med |
| `acsm_min_g_per_l` | 0.5 g·L⁻¹ | ACSM Position Stand, *Exercise and Fluid Replacement* (2007) | High |
| `sodium_min` | 300 mg·h⁻¹ | Practical floor | Med |
| `sodium_max` | 1500 mg·h⁻¹ | Practical ceiling; above this is unpalatable and GI-provocative | **Low-Med** |

Note the units: `sodium_loss` is a **concentration** (mg·L⁻¹ of sweat), so it must be multiplied by sweat
*volume* to yield a rate. Confusing the two is a plausible implementation error producing a number ~1.5×
wrong; worth an explicit unit test.

### 5.4 Caffeine

```
caffeine_mg_total, binding = bind([
    ('model:caffeine_dose_per_kg', caffeine_mg_per_kg · weight, UPPER),
    ('caffeine_tolerance',         caffeine_tolerance,          UPPER),
])
```

`caffeine_mg_per_kg = 6.0` — the top of the ISSN's evidenced 3–6 mg·kg⁻¹ range (Guest et al. 2021, ISSN
Position Stand, *JISSN*). **High confidence**, and note the position stand explicitly supports 3–6 mg·kg⁻¹
for endurance exercise *in the heat*, which is the case that matters here.

Deterministic dose schedule, fixed fractions summing to 1.0 (`solver/tables/fuelling.py`):

| Timing | Fraction |
|---|---|
| 45 min pre-start | 0.30 |
| Bike, at 55% of bike duration | 0.35 |
| Start of run | 0.35 |

The 45-minute pre-start figure is the ISSN's "most commonly used timing of 60 min pre-exercise", pulled
slightly forward to account for the swim.

### 5.5 Aid actions and cumulative consistency

One action per aid station from the bundle, in course order. Never a synthesised station (§1.2).

```
for each station s on legs bike, run, in ascending km:
    at_clock_minutes[s]  = ETA at that km, interpolated within its segment
    cumulative_carb_g[s] = carb_planned · (moving_hours elapsed at s)
```

`total_carb_g = carb_planned × duration_hours`, computed from **unrounded** inputs then rounded once
(§0.4). The contract's arithmetic-consistency invariant is asserted as a stage postcondition:

```
| total_carb_g − carb_g_per_hr × duration_hours |  ≤  0.5 g
```

The tolerance exists solely to absorb the single rounding of `carb_g_per_hr` to 1 g. If the assertion fails
by more than that, a rounded value has leaked into a computation — exactly the bug §0.4 exists to prevent.

### 5.6 Worked example — Athlete M, `C-TRAM`, hot

```
duration_hours = (406.11 + 270.57) / 60 = 11.278 h        (bike + run moving time;
                                                             the swim is excluded, so §4.4's
                                                             correction does not move this)

CARBOHYDRATE
  L(11.278) = 90 g·h⁻¹                                     (t > 4)
  bind([ ('model:carb_duration_target', 90, UPPER),
         ('gut_carb_ceiling',           75, UPPER) ])
    → carb_planned = 75 g·h⁻¹,  binding = 'gut_carb_ceiling'
  requires_multiple_transportable = (75 > 60) = True
  total_carb_g = 75 × 11.278 = 845.9 → 846 g

FLUID
  sweat_eff = 1.1 × (1 + 0.030 × (27.729 − 15.0))
            = 1.1 × (1 + 0.030 × 12.729)
            = 1.1 × 1.381870                              = 1.52005 L·h⁻¹
  bind([ ('sweat_rate',               1.52005 × 1000 × 0.75 = 1140.0, UPPER),
         ('model:gastric_emptying_cap', 1000.0,                       UPPER) ])
    → fluid_ml_per_hr = 1000 mL·h⁻¹, binding = 'model:gastric_emptying_cap'

  Note: the athlete cannot absorb their own sweat rate. The plan should say so, and the
  "Why this?" drawer names the gastric cap, not the sweat rate. This is a genuine and
  common long-course situation, and naming it correctly is the point of bind().

SODIUM
  na_from_losses = 900 × 1.52005 × 0.80                    = 1094.4 mg·h⁻¹
  na_acsm_floor  = 0.5 × (1000/1000) × 1000                =  500.0 mg·h⁻¹
  → sodium_mg_per_hr = 1094.4, binding = 'sodium_loss'
  within [300, 1500] → no clamp.  Rounded: 1090 mg·h⁻¹

CAFFEINE
  bind([ ('model:caffeine_dose_per_kg', 6.0 × 75 = 450, UPPER),
         ('caffeine_tolerance',                    300, UPPER) ])
    → caffeine_mg_total = 300 mg, binding = 'caffeine_tolerance'
  schedule: 90 mg at −45 min ; 105 mg at bike 223.4 min (55% of 406.11) ; 105 mg at run start
```

---

## §6 Stage 6 — Pack the bags (~0.2 s)

### 6.1 Structure

Exactly five bags, always, in fixed order: `morning`, `bike_t1`, `run_t2`, `bike_sn`, `run_sn`. Five bags
even when one is empty — an empty Run Special Needs bag is information, not an omission.

**Every item carries `reason_constraint_key` and `reason_text`.** An item with no upstream justification
cannot be emitted; this is a stage postcondition, asserted, not a convention:

```
assert all(item.reason_constraint_key is not None for bag in bags for item in bag.items)
```

`reason_constraint_key` must be one of the eight athlete keys, a `barrier:` key, or a `model:` key — the
same namespace `bind()` uses (§0.5). This is what makes "Why this?" work on a bag item exactly as it works
on a wattage target.

### 6.2 Rules are declarative

`solver/tables/bag_rules.py` holds rules as data, never nested conditionals:

```
Rule(bag, item_name, qty_expr, condition_expr, reason_constraint_key, reason_text_template)
```

Rules are evaluated in a fixed declaration order and each appends at most one item.

**On continuity.** Bag contents are inherently discrete — an athlete either carries arm coolers or does not
— so the model's continuity requirement cannot apply here and does not. What *is* required is that the
underlying quantities be continuous, so that a bag rule fires on a stable threshold rather than on a
number that itself jitters. All the thresholds below are config values in `solver/tables/bag_rules.py`.

### 6.3 The three specified conditional rules

**Arm coolers** — `forecast.temp_c > arm_cooler_temp_c`, `arm_cooler_temp_c = 28.0`, contract-specified.
Note it keys off **dry-bulb temperature**, not WBGT, because that is what the contract says and because it
is what the athlete reads on a forecast. `reason_constraint_key = 'model:arm_cooler_threshold'`.

**Head torch** — computed from solar position, never a fixed hour:

```
finish_clock_local = start_time_local + projected_minutes
dusk_local         = civil_dusk(event.date, event.lat, event.lng, tz)     # §I.1.7, zenith 96°
head_torch         = (finish_clock_local > dusk_local − dusk_buffer_min)
                     or options.night_flag
```

`dusk_buffer_min = 15.0`. If `civil_dusk` returns `None` (polar cases), fall back to `options.night_flag`
alone. `reason_constraint_key = 'model:dusk_buffer'`.

**Salt capsules**:

```
qty = ceil( sodium_mg_per_hr · leg_hours · sn_fraction / mg_per_capsule )
```

`mg_per_capsule = 300`, `sn_fraction = 0.5` for a special-needs bag (half the leg's need, since the rest is
carried from T1 or taken at aid stations). `reason_constraint_key = 'sodium_loss'` — which is correct and
important: the quantity traces to the athlete's measured sodium concentration, and the drawer should say so.

**First-timer set** — `level == 'first'` adds a defined beginner set, each item still carrying a reason
(`reason_constraint_key = 'model:first_timer_set'`). It is a set of items, not a generic checklist: spare
goggles (reason: a goggle failure ends a first-timer's race), written transition sequence, and so on.

### 6.4 Worked example — Athlete M, `C-TRAM`, hot

```
Event: 2026-09-19, start 07:00 local, lat 39.85 N, lng 3.12 E, UTC+2
projected_minutes = 762.8

ARM COOLERS
  forecast.temp_c = 31.0 > 28.0  → INCLUDED
  reason: 'model:arm_cooler_threshold' — "Forecast 31 °C is above the 28 °C threshold."

HEAD TORCH
  civil dusk (§I.1.7)      = 20:17 local
  threshold = dusk − 15min = 20:02 local
  finish    = 07:00 + 762.8 min = 07:00 + 12:42.8 = 19:43 local
  19:43 > 20:02 ?  NO  → NOT INCLUDED
  (this athlete would need projected_minutes > 782.2 to trigger the torch — a 19.4 min margin.
   G10-NIGHT uses athlete A-X at 870.6 min, which clears the threshold by 88 min.)

SALT CAPSULES, bike special needs
  sodium_mg_per_hr = 1094.4,  bike_hours = 406.11/60 = 6.7685
  qty = ceil( 1094.4 × 6.7685 × 0.5 / 300 )
      = ceil( 3703.7 / 300 ) = ceil(12.346) = 13 capsules
  reason: 'sodium_loss' — "900 mg·L⁻¹ sweat sodium at 1.52 L·h⁻¹ over 6.8 h on the bike."
```

The head-torch case is instructive: the athlete finishes 19.4 minutes inside the threshold, so a forecast that
slowed them by half an hour would add a head torch to the bag. That is exactly the kind of change the drift
mechanism should surface, and it is computed from the sun's actual position rather than from a guess about
when it gets dark in September.

---

# Part III — Appendices

## §A Config surface — every tunable value in the model

The rule from the contract is absolute: **nothing in this model may be a literal in a conditional.** This
appendix is the register. If a number appears anywhere in `solver/` that is not in this table, either the
table is incomplete or the code is wrong.

Ranges are *plausible tuning ranges* — the span within which a value could be moved during calibration
without the model becoming incoherent. They are not validation bounds.

### `solver/tables/physics.py`

| Key | Default | Unit | Range | Affects |
|---|---|---|---|---|
| `gravity` | 9.80665 | m·s⁻² | fixed | Everything with mass |
| `p0_pa` | 101325 | Pa | fixed | Air density |
| `lapse_rate_k_per_m` | 0.0065 | K·m⁻¹ | fixed | Air density vs elevation |
| `isa_exponent` | 5.25588 | — | fixed | Air density vs elevation |
| `r_dry_air` | 287.058 | J·kg⁻¹·K⁻¹ | fixed | Air density |
| `r_water_vapour` | 461.495 | J·kg⁻¹·K⁻¹ | fixed | Humidity correction to density |
| `tetens_a` | 610.78 | Pa | fixed | Saturation vapour pressure |
| `tetens_b` | 17.27 | — | fixed | Saturation vapour pressure |
| `tetens_c` | 237.3 | °C | fixed | Saturation vapour pressure |
| `eta_drivetrain` | 0.976 | — | 0.96–0.98 | All bike speeds (≈ ±0.5% on split) |
| `spoke_drag_area_fw` | 0.0044 | m² | 0.000–0.008 | Bike aero term (≈1.6% of CdA) |
| `bearing_c0` | 91 | mW | 60–120 | Bike, ≈1 W |
| `bearing_c1` | 8.7 | mW·s·m⁻¹ | 5–12 | Bike, ≈1 W |
| `bisection_iterations` | 60 | — | 50–80 | Determinism; never below 50 |
| `speed_bracket_lo` | 0.5 | m·s⁻¹ | fixed | Speed solve bracket |
| `speed_bracket_hi` | 30.0 | m·s⁻¹ | fixed | Speed solve bracket |
| `v_descent_max` | 20.83 | m·s⁻¹ | 16.7–25.0 | Descent realism/safety cap |
| `gradient_bin_width` | 0.0025 | — | 0.001–0.005 | Stage 1 quadrature; accuracy vs cost (§1.1, §0.8) |
| `node_gradient_clamp` | ±0.30 | — | 0.25–0.40 | Guard against bad terrain samples |
| `node_clamp_fail_fraction` | 0.02 | — | 0.01–0.05 | `BundleIncomplete` threshold |

### `solver/tables/equipment.py`

| Key | Default | Unit | Range | Affects |
|---|---|---|---|---|
| `cda_base['road_hoods']` | 0.325 | m² | 0.30–0.35 | Bike split |
| `cda_base['road_drops']` | 0.300 | m² | 0.28–0.32 | Bike split |
| `cda_base['road_clipons']` | 0.280 | m² | 0.26–0.30 | Bike split (also the fallback) |
| `cda_base['tt_bike']` | 0.255 | m² | 0.22–0.28 | Bike split |
| `cda_level_adj['first']` | +0.020 | m² | 0.00–0.04 | Bike split |
| `cda_level_adj['improver']` | 0.000 | m² | fixed | Reference |
| `cda_level_adj['experienced']` | −0.020 | m² | −0.04–0.00 | Bike split |
| `cda_helmet_adj['aero']` | −0.010 | m² | −0.02–0.00 | Bike split |
| `cda_helmet_adj['standard']` | 0.000 | m² | fixed | Reference |
| `cda_min` / `cda_max` | 0.19 / 0.38 | m² | — | Clamp |
| `cda_fallback_position` | `road_clipons` | — | — | Used when `bike_setup` absent |
| `bike_kit_mass['first']` | 11.0 | kg | 8–14 | Climbing |
| `bike_kit_mass['improver']` | 10.0 | kg | 8–13 | Climbing |
| `bike_kit_mass['experienced']` | 9.0 | kg | 7–12 | Climbing |
| `crr['smooth_asphalt']` | 0.0040 | — | 0.0027–0.0045 | Bike split |
| `crr['typical_road']` | 0.0050 | — | 0.0040–0.0055 | Bike split (default) |
| `crr['rough_chipseal']` | 0.0065 | — | 0.0050–0.0080 | Bike split (≈8 min per 180 km vs default) |
| `pressure_hpa_valid` | 870–1085 | hPa | — | Range outside which `pressure_hpa` is treated as absent (§I.1.1) |

### `solver/tables/heat_curve.py`

| Key | Default | Unit | Range | Affects |
|---|---|---|---|---|
| `wbgt_w_wet` / `wbgt_w_globe` / `wbgt_w_dry` | 0.7 / 0.2 / 0.1 | — | fixed | WBGT definition |
| `globe_offset_clear_c` | 8.0 | °C | 4–12 | WBGT; ≈5 s·km⁻¹ of run pace |
| `globe_offset_overcast_c` | 1.5 | °C | 0–4 | WBGT |
| `cloud_fraction['clear']` | 0.00 | — | fixed | Globe offset interpolation |
| `cloud_fraction['partly_cloudy']` | 0.35 | — | 0.2–0.5 | as above |
| `cloud_fraction['cloudy']` | 0.75 | — | 0.6–0.85 | as above |
| `cloud_fraction['overcast']` | 0.95 | — | 0.9–1.0 | as above |
| `cloud_fraction['rain']` | 1.00 | — | fixed | as above |
| `stull_coeffs` | (6 constants, §I.1.2) | — | fixed | Wet-bulb temperature |
| `peiffer_lab_rh` | 40.0 | % | 30–60 | **Shifts the whole bike heat curve** |
| `bike_heat_knots` | 4 pairs, §I.1.4 | (°C, —) | — | Bike power in heat |
| `bike_heat_extrapolate` | `false` (clamp) | — | — | Behaviour above WBGT 25.05 |
| `run_heat_wbgt_baseline_c` | 10.0 | °C | 8–15 | Onset of run heat penalty |
| `run_heat_exponent` | 1.3 | — | 1.0–1.8 | Curve shape above WBGT 25 |
| `run_heat_pct_at_15['experienced']` | 0.10 | — | 0.06–0.14 | Run pace in heat |
| `run_heat_pct_at_15['improver']` | 0.13 | — | 0.08–0.18 | Run pace in heat |
| `run_heat_pct_at_15['first']` | 0.16 | — | 0.10–0.22 | Run pace in heat |
| `run_heat_factor_max` | 1.60 | — | 1.4–1.8 | Clamp |
| `alt_a1` | 0.010 | per 1000 m | 0.005–0.02 | FTP below 1500 m |
| `alt_a2` | 0.070 | per 1000 m | 0.060–0.095 | FTP above 1500 m |
| `alt_breakpoint_m` | 1500 | m | 1200–1800 | Altitude piecewise knot |

### `solver/tables/intensity.py`

| Key | Default | Unit | Range | Affects |
|---|---|---|---|---|
| `if_ref['full'][level]` | 0.65 / 0.70 / 0.75 | — | ±0.05 | **Primary scope.** Bike target power — the biggest single lever |
| `if_ref['half'][level]` | 0.72 / 0.78 / 0.83 | — | ±0.05 | **Primary scope, contested** — see E-13; ±8 min over 90 km |
| `if_ref['olympic'][level]` | 0.80 / 0.85 / 0.88 | — | ±0.05 | ⚠️ **Out of primary scope — extrapolated, unvalidated** (§0.1b) |
| `if_ref['sprint'][level]` | 0.85 / 0.90 / 0.95 | — | ±0.05 | ⚠️ **Out of primary scope — extrapolated, unvalidated** (§0.1b) |
| `risk_adj['conservative'/'balanced'/'aggressive']` | −0.03 / 0.00 / +0.03 | — | ±0.05 | Bike target power |
| `if_max_feas['first'/'improver'/'experienced']` | 0.75 / 0.80 / 0.85 | — | ±0.05 | Barrier feasibility ceiling |
| `if_grid_step` | 0.005 | — | 0.002–0.01 | Stage 3 resolution vs cost |
| `if_grid_span_below_ref` | 0.20 | — | 0.10–0.30 | Stage 3 grid lower bound |
| `if_grid_floor` | 0.50 | — | 0.40–0.60 | Stage 3 grid absolute floor |
| `if_plan_min` | 0.40 | — | — | Clamp |
| `if_segment_ceiling` | 1.05 | — | 1.00–1.15 | Per-segment supra-threshold cap |
| `k_grade` | 0.12 | — | 0.05–0.20 | Power modulation amplitude |
| `g_scale` | 0.04 | — | 0.02–0.08 | Gradient at which modulation saturates |
| `vi_max[distance]` | 1.05 / 1.06 / 1.08 / 1.10 | — | 1.02–1.15 | Variability rail |
| `vi_backoff_sequence` | [0.75, 0.50, 0.25, 0.0] | — | — | Deterministic VI reduction |
| `transition_hurry_factor` | 0.85 | — | 0.75–1.00 | Stage 3 max profile |
| `lever_reoptimise` | `false` | — | — | If `true`, levers re-run the full grid (4× 105 ms) instead of one profile (§3.4, §0.8) |

### `solver/tables/run_model.py`

| Key | Default | Unit | Range | Affects |
|---|---|---|---|---|
| `riegel_r['first'/'improver'/'experienced']` | 1.08 / 1.07 / 1.06 | — | 1.04–1.10 | Run pace vs distance |
| `bike_coupling_c0[distance]` | 0.08 / 0.05 / 0.03 / 0.02 | — | 0.00–0.15 | Run pace off the bike |
| `bike_coupling_c1` | 1.6 | — | 0.8–3.0 | Over-biking penalty; sets the IF optimum |
| `minetti_coeffs` | (155.4, −30.4, −43.3, 46.3, 19.5, 3.6) | J·kg⁻¹·m⁻¹ | fixed | Run pace vs gradient |
| `alpha_up` | 1.00 | — | 0.85–1.10 | Uphill run pace |
| `alpha_dn` | 0.50 | — | 0.3–0.8 | Downhill run pace |
| `d_grade_min` / `d_grade_max` | 0.85 / 2.00 | — | 0.80–0.90 / 1.8–2.5 | Gradient clamps |

### `solver/tables/swim_model.py`

| Key | Default | Unit | Range | Affects |
|---|---|---|---|---|
| `d_prime_m` | 15.0 | m | 8–30 | Swim pace at all distances (low sensitivity, §4.4.3) |
| `css_validity_min` | 30.0 | min | 20–45 | Onset of the swim durability term |
| `k_swim_dur` | 0.0012 | per min | 0.0005–0.0025 | Swim pace beyond the CS validity window |
| `wetsuit_factor` | 0.955 | — | 0.93–0.98 | Swim pace |
| `ow_overhead['first'/'improver'/'experienced']` | 12.0 / 8.0 / 5.0 | s·(100 m)⁻¹ | 3–18 | Swim pace |
| `wetsuit_legal_max_c` | 24.5 | °C | fixed (rule) | Wetsuit legality |
| `wetsuit_mandatory_below_c` | 16.0 | °C | fixed (rule) | Wetsuit legality |
| `wetsuit_non_award_max_c` | 28.77 | °C | fixed (rule) | Wetsuit legality |
| `c_cold` | 0.8 | s·(100 m)⁻¹·°C⁻¹ | 0.0–2.0 | Cold-water swim pace |
| `cold_threshold_c` | 18.0 | °C | 15–20 | Cold-water onset |
| `c_warm` | 1.0 | s·(100 m)⁻¹·°C⁻¹ | 0.0–2.5 | Warm-water swim pace |
| `warm_threshold_c` | 26.0 | °C | 24–28 | Warm-water onset |

### `solver/tables/transitions.py`

| Key | Default | Unit | Range | Affects |
|---|---|---|---|---|
| `t1_base[distance][level]` | 12 values, §4.5 | min | ±50% | T1 duration |
| `t2_base[distance][level]` | 12 values, §4.5 | min | ±50% | T2 duration |
| `wetsuit_removal[level]` | 3.0 / 2.5 / 2.0 | min | 1.0–5.0 | T1 duration |

### `solver/tables/fuelling.py`

| Key | Default | Unit | Range | Affects |
|---|---|---|---|---|
| `carb_duration_knots` | 5 pieces, §5.1 | (h, g·h⁻¹) | — | Carbohydrate target |
| `carb_single_transporter_max` | 60 | g·h⁻¹ | fixed | Multiple-transportable flag |
| `carb_hard_max` | 120 | g·h⁻¹ | 100–120 | Override ceiling |
| `glucose_fructose_ratio` | 2.0 | — | 1.5–2.5 | Product selection |
| `k_sweat` | 0.030 | per °C WBGT | 0.010–0.060 | Fluid and sodium |
| `w_sweat_ref` | 15.0 | °C WBGT | 10–20 | Fluid and sodium — **fallback only**, used when `measured_at_temp_c` absent |
| `sweat_test_default_rh` | 55 | % | 40–70 | Converting `measured_at_temp_c` to the WBGT axis (§5.2) |
| `sweat_test_default_conditions` | `partly_cloudy` | — | — | As above |
| `replace_frac` | 0.75 | — | 0.60–0.90 | Fluid |
| `gastric_cap_ml` | 1000 | mL·h⁻¹ | 800–1200 | Fluid ceiling |
| `replace_frac_na` | 0.80 | — | 0.5–1.0 | Sodium |
| `acsm_min_g_per_l` | 0.5 | g·L⁻¹ | 0.5–0.7 | Sodium floor |
| `sodium_min` / `sodium_max` | 300 / 1500 | mg·h⁻¹ | — | Sodium clamps |
| `caffeine_mg_per_kg` | 6.0 | mg·kg⁻¹ | 3.0–6.0 | Caffeine total |
| `caffeine_schedule` | [0.30, 0.35, 0.35] | — | must sum to 1.0 | Caffeine timing |
| `caffeine_pre_start_min` | 45 | min | 30–60 | Caffeine timing |
| `caffeine_bike_fraction` | 0.55 | — | 0.4–0.7 | Caffeine timing |
| `carb_consistency_tolerance_g` | 0.5 | g | — | Postcondition tolerance |

### `solver/tables/bag_rules.py`

| Key | Default | Unit | Range | Affects |
|---|---|---|---|---|
| `arm_cooler_temp_c` | 28.0 | °C | 26–32 | Arm coolers |
| `dusk_zenith_deg` | 96.0 | ° | fixed (civil twilight) | Head torch |
| `sunset_zenith_deg` | 90.833 | ° | fixed | Reference only |
| `dusk_buffer_min` | 15.0 | min | 0–45 | Head torch |
| `salt_mg_per_capsule` | 300 | mg | 200–500 | Capsule count |
| `sn_fraction` | 0.5 | — | 0.3–0.7 | Capsule count |
| `first_timer_item_set` | list | — | — | First-timer bag contents |
| `bag_order` | `[morning, bike_t1, run_t2, bike_sn, run_sn]` | — | fixed | Output order |

### `solver/tables/margins.py`, `plausibility.py`, `rounding.py`, `precedence.py`

| Key | Default | Unit | Range | Affects |
|---|---|---|---|---|
| `margin_clear_min` | 20.0 | min | 10–30 | `margin_state` |
| `margin_tight_min` | 0.0 | min | fixed | `margin_state` |
| `lever_significance_minutes` | 2.0 | min | 1–5 | Which levers are offered |
| `drift_split_threshold_min` | 2.0 | min | 1–5 | Drift detection |
| `drift_margin_threshold_min` | 20.0 | min | — | Drift detection |
| `plausibility[key]` | 8 ranges, §2.2 | — | — | `INVALID_INPUT` |
| `rounding[field]` | 12 precisions, §0.4 | — | — | Output precision |
| `binding_precedence[quantity]` | ordered key tuples | — | — | Tie-breaking |

---

## §B Golden test case definitions

Twelve cases. **Inputs are specified exactly here; expected outputs are captured from the first correct
implementation run and then frozen.** They are not guessed in this document, because a guessed expectation
that the implementation is then tuned to match would test nothing.

Two of the twelve — `G05-INFEASIBLE` and `G06-TIGHT` — are *definitional*: their inputs were chosen by
running the model until they produced the required verdict, and the stated verdict is therefore a genuine
expectation. They are given below.

### B.1 Courses

**`C-TRAM` — "Serra de Tramuntana" (fictional), full distance.**
`lat 39.85, lng 3.12, timezone Europe/Madrid`. Swim 3800 m sea. Bike 180.2 km,
`surface_quality = typical_road`, mean elevation 120 m, **1518 m ascent / 762 m descent** (both derived
from the segment gradients below; the elevation series reproduces them exactly). Run 42.195 km,
**74 m ascent / 74 m descent**, mean elevation 25 m. Bike segments, in order:

| # | Name | km | Net gradient |
|---|---|---|---|
| 1 | Coastal out | 24.0 | +0.002 |
| 2 | Sa Pobla flats | 18.0 | +0.004 |
| 3 | Femenia approach | 9.6 | +0.018 |
| 4 | **Coll de Femenia** | 8.4 | **+0.058** |
| 5 | Femenia descent | 7.2 | −0.055 |
| 6 | Orient valley | 21.0 | +0.010 |
| 7 | Coll d'Honor | 5.8 | +0.049 |
| 8 | Honor descent | 6.4 | −0.048 |
| 9 | Inland rollers | 26.0 | +0.006 |
| 10 | Sencelles flats | 22.0 | +0.001 |
| 11 | Return coastal | 22.0 | +0.003 |
| 12 | Alcudia run-in | 9.8 | −0.006 |

Run: four laps of 10.549 km, each `[+0.004, −0.004, +0.003, −0.003]` over equal quarters.
Barriers: `swim_exit 140`, `bike_km_120 510`, `bike_cutoff 630`, `finish 960` minutes from start.
Aid stations: bike at km 20, 45, 70, 95, 120, 145, 170; run at every 2.1 km (20 stations).
Elevation series: 100 m node spacing, generated to reproduce each segment's net gradient exactly.

**`C-FLAT` — "Costa Plana" (fictional), full distance, flat.** Same lat/lng/timezone, same swim, same
barriers. Bike 180.0 km, **135 m ascent / 135 m descent**, `smooth_asphalt`, mean elevation 15 m,
eight segments each 22.5 km with gradients `[+0.001, −0.001, +0.002, −0.002, +0.001, −0.001, +0.002, −0.002]`. Run 42.195 km flat
(all segment gradients 0.000).

**`C-ALTA` — "Alta Ruta" (fictional), full distance, mountainous.** `lat 46.52, lng 7.98,
timezone Europe/Zurich`. Swim 3800 m lake. Bike 176.0 km, **3565 m ascent / 2635 m descent**,
`typical_road`, mean elevation 980 m, six segments: `[28.0 km +0.004, 21.0 km +0.071, 18.0 km −0.062, 34.0 km +0.008, 26.0 km +0.065,
49.0 km −0.031]`. Run 42.195 km, 620 m gain, mean elevation 640 m.
Barriers: `swim_exit 150`, `bike_cutoff 690`, `finish 1020`.

**`C-HALF` — "Tramuntana 70.3" (fictional).** Same location as `C-TRAM`. Swim 1900 m, bike 90.1 km
(segments 1–6 of `C-TRAM` truncated to 90.1 km), run 21.1 km.
Barriers: `swim_exit 70`, `bike_cutoff 330`, `finish 510`.

**`C-OLY` — "Alcudia Olympic" (fictional).** Swim 1500 m, bike 40.0 km flat (two 20 km segments at
+0.002 / −0.002), run 10.0 km flat. Barriers: `swim_exit 50`, `bike_cutoff 170`, `finish 240`.

**`C-SPR` — "Alcudia Sprint" (fictional).** Swim 750 m, bike 20.0 km flat (two 10 km segments at
+0.002 / −0.002), run 5.0 km flat. Barriers: `swim_exit 30`, `bike_cutoff 95`, `finish 130`.

### B.2 Athletes

| | `A-M` | `A-E` | `A-F` | `A-T` | `A-X` | `A-Y` |
|---|---|---|---|---|---|---|
| `level` | improver | experienced | first | first | first | first |
| `swim_threshold_pace` (s·100m⁻¹) | 105 | 88 | 145 | **133** | **122** | **150** |
| `bike_threshold_power` (W) | 224 | 285 | 155 | **176** | **195** | **150** |
| `run_threshold_pace` (s·km⁻¹) | 282 | 240 | 400 | **363** | **325** | **420** |
| `weight` (kg) | 75 | 68 | 82 | 77 | **73** | 84 |
| `sweat_rate` (L·h⁻¹) | 1.1 | 1.4 | 0.9 | 1.0 | 0.9 | 0.9 |
| `sodium_loss` (mg·L⁻¹) | 900 | 1250 | 700 | 800 | 700 | 700 |
| `gut_carb_ceiling` (g·h⁻¹) | 75 | 95 | 50 | 55 | 50 | 50 |
| `caffeine_tolerance` (mg) | 300 | 400 | 150 | 200 | 150 | 150 |
| `bike_setup` | tt_bike / standard | tt_bike / aero | road_clipons / standard | road_clipons / standard | road_clipons / standard | road_clipons / standard |
| Role | baseline | short course | infeasible | tight margin | night finish | earliest-miss |

**`A-T`, `A-X` and `A-Y` were retuned or introduced in this revision.** `A-T` moved because the corrected
swim model (§4.4) shifted every projected total; at its previous numbers it was no longer inside the `tight`
band. `A-X` was **replaced outright**: its previous profile turned out to be *infeasible* on `C-TRAM`, so
`G10-NIGHT` never reached Stage 6 and tested nothing at all — the case asserted a head torch on a plan that
was never produced. `A-Y` is new, for `G14`.

Constraint `source` values, fixed per case so the provenance-invariance CI test has something to permute:
`A-M` = `tested` except `weight: measured`, `sweat_rate: estimated`, `sodium_loss: estimated`,
`caffeine_tolerance: manual`. `A-E` = all `measured`. `A-F`, `A-T`, `A-X` = all `estimated`.

### B.3 Forecasts

| Id | temp_c | humidity | wind | conditions | water_temp | `pressure_hpa` | `cloud_cover_pct` |
|---|---|---|---|---|---|---|---|
| `F-MILD` | 22.0 | 60 | 3.0 | partly_cloudy | 21.0 | 1015.0 | 40 |
| `F-HOT` | 31.0 | 55 | 3.0 | clear | 22.5 | 1013.0 | 5 |
| `F-COOL` | 14.0 | 70 | 5.0 | cloudy | 15.5 | 1008.0 | 80 |
| `F-WARMWATER` | 27.0 | 65 | 2.0 | clear | 26.0 | 1012.0 | 10 |
| `F-MILD-BARE` | 22.0 | 60 | 3.0 | partly_cloudy | 21.0 | *(omitted)* | *(omitted)* |

`F-MILD-BARE` is `F-MILD` with both new optional fields absent, for the `assumed_fields` case.

### B.4 The fifteen cases

`schema_version = 2` throughout (§F.1). `goal.goal_minutes = None`, `goal.risk = balanced`,
`goal.first_timer = (level == 'first')`, `options = {carb_override: None, night_flag: false,
preview_only: false}` unless stated. Event date `2026-09-19`, `start_time_local 07:00` unless stated.
**Every case sets `bike_setup` explicitly** so that `assumed_fields` is empty except in `G15`, which exists
to exercise the fallback path on its own.

| Id | Course | Athlete | Forecast | Overrides | Exercises |
|---|---|---|---|---|---|
| `G01-FULL` | `C-TRAM` | `A-M` | `F-MILD` | — | **Primary distance.** Full-distance baseline; projected ≈ 735.3 min |
| `G02-HALF` | `C-HALF` | `A-M` | `F-MILD` | — | **Primary distance.** `if_ref` half row; swim durability term nearly inert at 1900 m |
| `G03-OLYMPIC` | `C-OLY` | `A-E` | `F-MILD` | — | ⚠️ Out of primary scope (§0.1b). Asserts the short-distance path runs and stays deterministic — **not** that the numbers are right. `pace_target ≥ run_threshold_pace` clamp fires |
| `G04-SPRINT` | `C-SPR` | `A-E` | `F-MILD` | — | ⚠️ Out of primary scope. As `G03`; shortest path, CSS model gives pace *faster* than CSS |
| `G05-INFEASIBLE` | `C-TRAM` | `A-F` | `F-MILD` | — | **Expect `feasibility = infeasible`**; `barrier = finish`, `miss_minutes ≈ 90.6`, `tightest_barrier = finish` (they coincide here). Only the finish is missed — swim +38.6, km 120 +28.9, bike cut-off +8.5. Levers `[improve_run_pace, raise_ftp]` |
| `G06-TIGHT` | `C-TRAM` | `A-T` | `F-MILD` | — | **Expect `margin_state = tight`**, `worst_margin_minutes ≈ +6.2` at `finish`, projected ≈ 953.9 min. `binding_constraint_key = barrier:finish` via §3.5 precedence rule 1 |
| `G07-HOT` | `C-TRAM` | `A-M` | `F-HOT` | — | WBGT 27.729; bike heat clamped at the top knot; run `D_heat = 1.1616`; arm coolers included; projected ≈ 762.8 min |
| `G08-FIRSTTIMER` | `C-HALF` | `A-F` | `F-MILD` | `first_timer = true` | First-timer bag set; `first` level tables throughout |
| `G09-CARBOVERRIDE` | `C-TRAM` | `A-M` | `F-MILD` | `carb_override = 95` | `overridden = true`; 95 > ceiling 75, below `carb_hard_max` 120; `OVER_CEILING` warning |
| `G10-NIGHT` | `C-TRAM` | `A-X` | `F-MILD` | `start_time_local = 08:00` | Projected ≈ 870.6 min → finish 22:30 local, past civil dusk 20:17. **Feasible** (worst margin ≈ +54.8), so Stage 6 actually runs and the head torch is emitted with a real `reason_constraint_key` |
| `G11-FLAT` | `C-FLAT` | `A-M` | `F-MILD` | — | `grade_mod ≈ 1` throughout; VI ≈ 1.000; `smooth_asphalt` Crr; gradient histogram collapses to few bins |
| `G12-MOUNTAIN` | `C-ALTA` | `A-E` | `F-COOL` | — | Only case exercising `alt_factor` (mean 980 m); `d_grade_max` clamp reachable on the run; large `grade_mod` swings; widest gradient histogram |
| `G13-NOWETSUIT` | `C-TRAM` | `A-M` | `F-WARMWATER` | — | **New.** Water 26.0 °C is in the permitted-but-not-award-eligible band, so the model races **without** a wetsuit and sets the warning flag. `c_warm` term active. This branch was previously untested |
| `G14-EARLIESTMISS` | `C-TRAM` | `A-Y` | `F-MILD` | — | **New, and the case that pins §3.3 / §F.5.** Two barriers missed. Expect `barrier = bike_cutoff`, `miss_minutes ≈ 10.1`; `tightest_barrier = finish`, `tightest_miss_minutes ≈ 131.8`. A regression that reverted to "tightest" would report 131.8 min at the finish and this case would fail loudly |
| `G15-ASSUMED` | `C-TRAM` | `A-M` *(no `bike_setup`)* | `F-MILD-BARE` | `sweat_rate.measured_at_temp_c` omitted | **New.** Expect `assumed_fields = ("athlete.bike_setup", "forecast.cloud_cover_pct", "forecast.pressure_hpa", "sweat_rate.measured_at_temp_c")` — exactly those four, in that sorted order. CdA falls back to `road_clipons` + `improver` = 0.280, so the bike split differs from `G01` |

**Coverage notes.** `G01`, `G02`, `G05`, `G06`, `G07`, `G09`, `G10`, `G13`, `G14` and `G15` are all on
primary distances and are the cases whose *numbers* matter. `G03` and `G04` are path coverage only. `G11`
and `G12` bracket the terrain range and are the cases that would catch a regression in the §1.1 gradient
histogram — a bug there is invisible on flat courses and severe on `C-ALTA`.

**Determinism tests that accompany the golden files:**

1. **Byte-identical repeat** — solve each case twice in one process, assert identical output.
2. **Provenance invariance** — permute every constraint `source` value, assert identical *numeric* output.
   This is the CI enforcement of §0.6.
3. **Input-hash invariance** — assert `solve_input_hash` is stable across process restarts and across
   dict insertion orders in the input JSON.
4. **Continuity** — for `G07`, perturb `temp_c` by ±0.1 °C and assert every output moves by less than a
   configured epsilon. This is the CI enforcement of the contract's "must not jump because a forecast moved
   0.1 °C". Note it will *fail by design* if `conditions` is perturbed instead — see §D-4.
5. **Monotonicity** — for `G01`, assert `projected_minutes` is non-increasing in `bike_threshold_power` and
   non-increasing as `run_threshold_pace` falls. A model that fails this has a sign error somewhere.

---

## §C Validation protocol

### C.1 Scope of validation

**Back-testing covers full distance and 70.3 only** (§0.1b). Olympic and Sprint are out of primary scope,
their intensity bands are extrapolated rather than sourced, and **no error target is set for them below.**
Their golden cases assert that the code paths run and stay deterministic; they assert nothing about
accuracy. If short-course ever becomes a primary distance, `if_ref['olympic']` and `if_ref['sprint']` need
sourcing before, not after.

Within the two primary distances, get **full distance validated first**. It has the longest legs, so it has
the most leverage on every constant, and three of the model's weakest terms — the bike heat duration gap
(§D-1), the swim durability term (§4.4.3) and the bike-run coupling (§4.3.1) — are all near-inert at 70.3
and dominant at full distance.

### C.2 Method

Back-test against real races where the athlete's pre-race constraints, the course, the conditions and the
actual splits are all known. Three to four races is enough for a first pass; the requirement is that each
one has **power data on the bike**, since without it a total-time match cannot distinguish a correct model
from two errors cancelling.

For each race: assemble the `SolveInput` as it would have existed the day before the race — pre-race
constraint values, the actual course bundle, the *forecast* (not the observed weather, unless the plan is
being judged in hindsight) — solve, and compare leg by leg.

**Judge legs before totals.** A total-time error of 1% built from a bike that is 4% fast and a run that is
5% slow is not a good model; it is two errors that happen to cancel, and it will not cancel for the next
athlete.

### C.3 Acceptable error

| Scope | Target | Rationale |
|---|---|---|
| Total time | within **±3%** | The stated bar for a first model. On a 12-hour race, ±22 min |
| Bike split | within **±4%** | Dominated by CdA and Crr, both estimated |
| Run split | within **±6%** | Compounds three estimated multipliers |
| Swim split | within **±8%** | Open-water overhead is the least evidenced constant in the model |
| Transitions | within **±3 min** | Absolute, not relative — a percentage on 6 minutes is meaningless |
| **Barrier margins** | **no sign errors** | A `clear` that was really `bad` is a product failure regardless of the percentage |

The last row is the one that matters most. A model that is 5% slow but never wrongly tells an athlete they
will make a cut-off is more useful than a model that is 2% accurate and occasionally does.

**Systematic bias is more informative than scatter.** Three races all 4% fast on the bike is a constant that
needs moving. Three races scattered ±5% with no mean offset is irreducible athlete variability and should
not be tuned away — tuning to it is overfitting to three data points.

### C.4 Diagnostic table — symptom to constant

This is the table to use when a back-test disagrees. It maps an *observed error pattern* to the *specific
constant* most likely responsible, ordered so that the first candidate is the one with both the largest
leverage and the weakest evidence.

| Observed pattern | Most likely constant | Table | Direction | Note |
|---|---|---|---|---|
| Bike consistently **fast** by 3–6% | `cda_base[position]` too low | `equipment.py` | Increase | Largest single lever; 21.2 min across the plausible range (§I.2.3) |
| Bike consistently fast, **only on rough courses** | `crr['rough_chipseal']` too low | `equipment.py` | Increase | This value is extrapolated, not measured |
| Bike consistently **slow** on flat courses only | `cda_base` too high, or wind term over-applied | `equipment.py` / `physics.py` | Decrease | Check whether wind direction was actually available |
| Bike fast on **climbs**, right on flats | `bike_kit_mass[level]` too low | `equipment.py` | Increase | Only the gravity term is affected |
| Bike error scales with **race duration** | Bike heat curve lacks duration scaling | `heat_curve.py` | See §D-1 | The known structural gap; a 40 km TT is not a 180 km ride |
| **Run fades worse than projected in heat** | `run_heat_pct_at_15[level]` too low, or `run_heat_exponent` too shallow | `heat_curve.py` | Increase | Try the exponent first if the error grows with WBGT; the level coefficient if it is flat across conditions |
| Run fades worse in heat **only above ~28 °C** | `run_heat_exponent` | `heat_curve.py` | Increase | The exponent governs the extrapolated region specifically |
| Run slow in heat, bike **right** in the same race | `globe_offset_clear_c` is fine; the two curves disagree | `heat_curve.py` | Investigate | Expected: the bike curve is clamped and the run curve is not. Confirms §I.1.4's known weakness |
| Run consistently slow by 5–10%, **all conditions** | `bike_coupling_c0[distance]` too low | `run_model.py` | Increase | Check against the "10–15% slower than open marathon" band |
| Run slow **only when the bike was ridden hard** | `bike_coupling_c1` too low | `run_model.py` | Increase | Re-check the §4.1 optimum argument afterwards — below ≈1.2 it stops holding |
| Run slow **only at full distance**, right at 70.3 | `riegel_r[level]` too low | `run_model.py` | Increase | Consistent with Vickers & Vertosick's marathon finding |
| Run too **fast** on hilly courses | `alpha_dn` too high, or `d_grade_min` too low | `run_model.py` | Decrease `alpha_dn` | Descent damping is a pure estimate |
| Run too slow on hilly courses | `alpha_up` too high | `run_model.py` | Decrease | Minetti coefficients themselves are near-certainly right (§4.3.2) |
| Swim consistently slow by 5–12% | `ow_overhead[level]` too high | `swim_model.py` | Decrease | Also the first place drafting shows up — see §D-11 |
| Swim slow **only in wetsuit races** | `wetsuit_factor` too high | `swim_model.py` | Decrease | 0.955 is discounted from a 400 m result; long-course may warrant more |
| Swim error scales with **distance** | `k_swim_dur`, then `css_validity_min` | `swim_model.py` | Adjust `k_swim_dur` first | It carries almost all the distance dependence; `d_prime_m` is too insensitive to be the cause (§4.4.3) |
| Swim slow at full distance, right at 70.3 | `k_swim_dur` too low | `swim_model.py` | Increase | The durability term only engages past ~30 min, so it is nearly inert at 70.3 |
| Transitions consistently wrong | `t1_base` / `t2_base` | `transitions.py` | Set from data | **Fix this first.** It is readable straight off results pages with no modelling assumptions |
| Fluid plan unachievable in practice | `replace_frac` or `gastric_cap_ml` | `fuelling.py` | Decrease | Check whether the gastric cap was binding (it usually is in heat) |
| Sodium plan implausible (too high) | Unit confusion: concentration vs rate | — | **Bug, not a constant** | See the §5.3 warning |
| Athletes routinely prescribed 90 g·h⁻¹ | `gut_carb_ceiling` default in onboarding | *not this model* | — | Compare against Pfeiffer's 62–71 g·h⁻¹ observed band (§5.1) |
| Head torch appears/disappears wrongly | Timezone offset or longitude sign | — | **Bug, not a constant** | §I.1.7; test both hemispheres |
| Every leg slow by a similar % at altitude | `alt_a2` | `heat_curve.py` | Adjust | Sources span 6.3–9.2% per 1000 m; 7.0 is a compromise |

### C.5 Results table

*To be completed. Leave the expectations empty until real races are entered — a pre-filled expectation is
an invitation to tune toward it.*

| Race | Date | Athlete level | Conditions (T/RH/WBGT) | Leg | Projected | Actual | Δ | Δ% | Constant implicated |
|---|---|---|---|---|---|---|---|---|---|
| | | | | Swim | | | | | |
| | | | | T1 | | | | | |
| | | | | Bike | | | | | |
| | | | | T2 | | | | | |
| | | | | Run | | | | | |
| | | | | **Total** | | | | | |
| | | | | Swim | | | | | |
| | | | | T1 | | | | | |
| | | | | Bike | | | | | |
| | | | | T2 | | | | | |
| | | | | Run | | | | | |
| | | | | **Total** | | | | | |
| | | | | Swim | | | | | |
| | | | | T1 | | | | | |
| | | | | Bike | | | | | |
| | | | | T2 | | | | | |
| | | | | Run | | | | | |
| | | | | **Total** | | | | | |

**Summary after back-testing** *(to be completed)*

| Metric | Value |
|---|---|
| Mean absolute error, total time | |
| Mean signed error, total time (bias) | |
| Mean signed error, bike | |
| Mean signed error, run | |
| Barrier margin sign errors | |
| Constants adjusted as a result | |

---

## §D Open questions

Five of the original eleven are now closed. What remains is written down rather than filled with a plausible
number, per the "do not invent physiology" rule.

### Closed since the first draft

| # | Was | Closed by |
|---|---|---|
| **D-2** | `sweat_rate` had no reference condition | **`sweat_rate.measured_at_temp_c`** (§F.4). Fallback to 15 °C WBGT remains, but is now declared in `assumed_fields` |
| **D-3** | Barometric pressure not in the forecast | **`forecast.pressure_hpa`** (§F.3). ISA fallback remains, declared |
| **D-4** | `conditions` categorical, so the globe offset stepped | **`forecast.cloud_cover_pct`** (§F.3). The environment model is now continuous in all three of temperature, humidity and cloud when the field is supplied |
| **D-7** | Threshold definitions unconfirmed | **Decided** (§2): run = one-hour pace, swim = CSS from the 400/200 pair. §2.5 specifies all three entry routes; §4.4 was rewritten because CSS is an asymptote and the previous Riegel anchoring was wrong by ~2.3% at full distance |
| **D-8** | Infeasibility reported the tightest barrier | **Changed to earliest missed** (§3.3), with `tightest_barrier` retained for diagnostics. Contract change specified in §F.5 |
| **D-14** | §B.1 declared each golden course's elevation gain twice — once constructively (segment gradients plus the "reproduce each net gradient exactly" rule) and once as an aggregate — and the two disagreed on four legs, by up to −59% | **Closed. The constructive rule is authoritative**; the aggregates above were wrong and are corrected in §B.1 to state ascent and descent separately. A segment's `elevation_gain_m` is `Σ max(0, h_{j+1} − h_j)` over the delivered nodes (§1.1), derived from the node series, never from a stored total. Raised and evidenced by Session B in `pipelines/course-ingest/docs/GOLDEN_FIXTURE_DISCREPANCY.md`; the fixtures already followed the constructive rule and carry both figures as `elevation_gain_m` and `declared_elevation_gain_m` |

### Still open

**D-1 — Bike heat curve has no duration term. HIGHEST-PRIORITY OPEN GAP.** Peiffer's 40 km time trial is
~60 minutes. **Both of our primary distances have bike legs far longer than that** — roughly 2.5 h at 70.3
and 4.5–7 h at full distance — and thermal strain accumulates over exactly that span. The curve therefore
under-states the decrement for the only two race formats we sell, and **the bias grows with race length**:
smallest where it is least consequential and largest at full distance in heat, which is the single scenario
where an optimistic bike split does the most damage, because it also feeds the over-biking term into an
already heat-degraded run.

The flat clamp above WBGT 25.05 (§I.1.4) compounds this: the duration gap and the refusal to extrapolate
push in the same optimistic direction, and neither corrects the other.

*Impact:* bike splits optimistic in heat, error growing with race length; downstream, run splits optimistic
too. *What would close it:* a study of sustained-power decrement over 4+ hours at controlled WBGT — which I
do not believe exists — or, practically, back-testing full-distance hot races and fitting a duration term
empirically. **Until then, treat hot full-distance projections as the model's least trustworthy output**, and
prefer the conservative side of any `if_ref` tuning for those races.

**D-5 — Wind is not in the WBGT calculation.** Wind substantially reduces globe temperature and increases
evaporative cooling, so a hot windy day is meaningfully less stressful than a hot still day. The model uses
wind for aerodynamics only. *Impact:* over-states heat cost on windy days — the opposite sign to D-1, but
they do not reliably cancel. *What would close it:* a full WBGT model with a wind term, at the cost of more
assumed constants.

**D-6 — Altitude acclimatisation is not modelled.** `alt_factor` applies the same derate to a Colorado
resident and a sea-level athlete arriving two days before. The effect is large and highly individual, and
the product captures nothing about where the athlete lives or when they arrive. *What would close it:*
either new inputs, or an explicit statement in the UI that mountain projections assume no acclimatisation.
Low urgency — no currently seeded course exceeds 1500 m except `C-ALTA`.

**D-9 — Walking is not modelled.** Above `d_grade_max` (≈ +13%) the model clamps rather than switching to a
walking cost curve, and long-course athletes routinely walk both steep pitches and aid stations. Minetti
gives a walking polynomial, so the physiology exists; what is missing is a defensible rule for *when* an
athlete switches. *Impact:* steep run courses under-predicted; aid-station walking absent everywhere, which
at full distance is plausibly several minutes across 20 stations.

**D-10 — Water temperature's direct effect on swim speed in the legal range is unstudied.** `c_cold` and
`c_warm` are placeholders producing a small, monotonic, correctly-signed effect. Everything I could find on
water temperature and swimming concerns hypothermia at 10–16 °C, not pace at 16–28 °C.

**D-11 — Drafting is not modelled, deliberately.** 15–25% energy saving on feet is well documented, but the
solver cannot know whether an athlete will find feet, and modelling it would mean inventing an input. The
consequence is a known directional bias: swim projections are slightly slow for strong swimmers who draft
well. Documented rather than hidden.

**D-12 — `D′` is estimated when it is recoverable (new).** §4.4 needs the swimmer's `D′`, and uses a
population default of 15 m. The true value falls straight out of the test the athlete already performed:
`D′ = 400 − CSS·t₄₀₀ = 200 − CSS·t₂₀₀`. The product computes CSS from `t₄₀₀`/`t₂₀₀` and then **discards
both**. Persisting the raw pair would replace an estimate with a measurement at zero additional cost to the
athlete. *Why it is not in §F:* it was not among the four inputs requested, and §4.4.3 shows the sensitivity
is genuinely low (0.4% at 3800 m). It is the obvious fifth candidate.

**D-13 — Short-course intensity bands are unsourced (new, from the §0.1b scope review).** `if_ref['olympic']`
and `if_ref['sprint']` are extrapolations, not findings. They are out of primary scope so this is not
blocking, but it should be closed before short-course is ever marketed on the same accuracy claim as full
and 70.3 — and closing it likely means original work, since short-course racing is drafting-legal and the
tactical reality is not represented anywhere in this model.

---

## §E Source verification checklist

Per §0.2, **no constant in this document was verified against primary full text**, because the research
environment's egress policy blocked every publisher host. The checklist is banded by consequence, not by
convenience. Roughly two hours of library access covers all of it; **Tier 1 alone is about forty minutes and
is the part that must happen before implementation.**

### Tier 1 — do these before writing solver code

Both items can change plan outputs on a **primary** distance by more than the width of the `clear`/`tight`
margin band.

| # | Verify | Source | If wrong |
|---|---|---|---|
| **E-1** | Stull wet-bulb coefficients, all six digits, and that `atan` is in radians | Stull 2011, *J Appl Meteor Climatol* 50:2267–2269 | **Every heat number in the model is wrong** — both curves are expressed on the WBGT axis this feeds |
| **E-13** | Allen & Coggan's stated **70.3 intensity-factor range** | *Training and Racing with a Power Meter* | 70.3 is a **primary distance** and we sit on the low side (0.78 vs a cited 0.83–0.87) **by assumption**. Worth ~8 min over 90 km — a third of the entire margin band — and it propagates into the run through the over-biking term. Promoted from Tier 3 on the §0.1b scope review |

### Tier 2 — before back-testing

Wrong values here will be mistaken for model error and tuned around, corrupting the §C.4 diagnosis.

| # | Verify | Source | If wrong |
|---|---|---|---|
| **E-2** | Peiffer's **laboratory humidity**, and the four power values | Peiffer & Abbiss 2011, *IJSPP* 6(2):208–220 | Shifts the whole bike heat curve and the §0.7 reconciliation. Compounds with D-1 |
| **E-3** | Ely's quadratic model coefficients, and the exact "10% for a 3-hour marathoner over WBGT 10→25" figure | Ely et al. 2007, *MSSE* | The run heat curve's anchor and `run_heat_exponent` |
| **E-11** | Riegel 1.06 and Vickers & Vertosick's marathon finding | Riegel 1977; Vickers & Vertosick 2016 | Run distance decay **and** the §2.5.2 threshold conversion, which uses the same exponent |

### Tier 3 — before launch

| # | Verify | Source | If wrong |
|---|---|---|---|
| **E-4** | `F_w = 0.0044 m²`, wheel-bearing `91 + 8.7v` mW, `η = 0.977` | Martin et al. 1998, *J Appl Biomech* 14(3):276–291 | Small (≈2% of bike power total) but unverified |
| **E-5** | Minetti running polynomial coefficients | Minetti et al. 2002, *J Appl Physiol* 93:1039–1046 | Run gradient handling. **Partially self-verified** — the polynomial reproduces two of the paper's own reported measurements to within 1 SD (§4.3.2), so confidence here is higher than the others |
| **E-6** | Carbohydrate duration bands and the 60 g·h⁻¹ SGLT1 figure | Jeukendrup 2014, *Sports Med* 44(S1):S25–S33 | Fuelling targets. Corroborated across several independent summaries, so lower risk |
| **E-7** | 120 g·h⁻¹ hard maximum and the authors' own hedging on it | Podlogar & Wallis 2022, *Sports Med*; Podlogar et al. 2022 | The override ceiling |
| **E-8** | ACSM 0.5–0.7 g·L⁻¹ sodium figure | ACSM Position Stand, *Exercise and Fluid Replacement*, 2007 | Sodium floor |
| **E-9** | Sweat sodium 10–90 mmol·L⁻¹ range | Baker 2017, *Sports Med* 47:1391–1409 | Plausibility range only |
| **E-10** | 3–6 mg·kg⁻¹ caffeine and the 60-min timing | Guest et al. 2021, ISSN Position Stand, *JISSN* | Caffeine dose |
| **E-12** | Wetsuit temperature thresholds against the **current season's** Ironman rules | Ironman competition rules | These change; they are rules, not physics, and should be re-checked annually |
| **E-14** | Critical-speed model form and typical swimming `D′` magnitude | Critical power/speed literature | §4.4.1. The *form* is standard and I am confident in it; the population `D′` is an estimate, and §4.4.3 shows it is low-sensitivity |

**Not on this list, deliberately:** `if_ref['olympic']` and `if_ref['sprint']` have no source to verify —
they were extrapolated, they are out of primary scope (§0.1b), and verifying them means finding evidence
that does not currently exist rather than checking a citation.

Constants with **no primary source at all**, which no amount of library access will fix — they are
estimates and are labelled as such throughout: `globe_offset_clear_c`, `globe_offset_overcast_c`,
`cloud_fraction[*]`, `g_scale`, `alpha_dn`, `d_grade_min`, `d_grade_max`, `bike_coupling_c0`,
`bike_coupling_c1`, `d_prime_m`, `css_validity_min`, `k_swim_dur`, `ow_overhead[*]`, `c_cold`, `c_warm`,
`k_sweat`,
`w_sweat_ref`, `transition_hurry_factor`, all of `transitions.py`, `cda_level_adj[*]`,
`crr['rough_chipseal']`, and the `first`/`improver` rows of `run_heat_pct_at_15`.

That list is long, and it should be. It is the honest answer to "which of these numbers is a guess?" —
and every one of them is a row in §A, tunable without a deploy, and a row in §C.4, with a symptom that
identifies it.


---

## §F Contract changes for the build specification

Everything in this appendix is a change to `RaceOS_Build_Spec.md`. It is written as **replacement text**, not
as a description, so it can be applied directly. Five changes: four new inputs and one changed output shape.

None of them alters the six stages, the stage ordering, the determinism requirement, the SLA, or the failure
behaviour.

### F.1 Summary

| # | Change | Build Spec location | Breaking? |
|---|---|---|---|
| F.2 | `bike_setup` on `AthleteSnapshot` | Part 5 §5.1; Part 4 §4.2 | No — optional, documented fallback |
| F.3 | `pressure_hpa`, `cloud_cover_pct` on `ForecastSnapshot` | Part 5 §5.1; Part 4 §4.4 `forecast_snapshot` | No — optional |
| F.4 | `measured_at_temp_c` on the `sweat_rate` constraint | Part 4 §4.2 `constraints` | No — nullable column |
| F.5 | `Infeasibility` reports earliest missed barrier; gains two fields | Part 5 §5.2 Stage 3 | **Yes** — output shape and semantics both change |
| F.6 | `assumed_fields` on `SolveOutput` | Part 5 §5.1 | **Yes** — output shape changes |

`schema_version` moves **1 → 2**. Golden files must be regenerated; §F.8 lists which cases change inputs.

### F.2 `bike_setup` — new field on `AthleteSnapshot`

**Rationale.** §I.2.3: CdA spans 21.2 minutes over 180 km across the plausible age-group range, against a
20-minute `clear`/`tight` margin boundary. The assumption alone can flip a feasibility verdict.

**Replacement text — Part 5 §5.1, add above `SolveInput`:**

```python
class BikePosition(str, Enum):
    ROAD_HOODS   = "road_hoods"     # road bike, hands on the hoods
    ROAD_DROPS   = "road_drops"     # road bike, hands in the drops
    ROAD_CLIPONS = "road_clipons"   # road bike with clip-on aero bars
    TT_BIKE      = "tt_bike"        # time-trial / triathlon bike with aero bars

class HelmetType(str, Enum):
    STANDARD = "standard"           # standard road helmet
    AERO     = "aero"               # aero or time-trial helmet

@dataclass(frozen=True)
class BikeSetup:
    position: BikePosition
    helmet: HelmetType
```

**Replacement text — Part 5 §5.1, `AthleteSnapshot`:** add the field

```python
    bike_setup: BikeSetup | None = None   # None -> solver assumes ROAD_CLIPONS + STANDARD,
                                          # and emits "athlete.bike_setup" in assumed_fields
```

**Replacement text — Part 4 §4.2, `users` table:** add two columns

```
bike_position bike_position_enum null,     -- road_hoods | road_drops | road_clipons | tt_bike
bike_helmet   bike_helmet_enum   null      -- standard | aero
```

with the matching enum types in Part 4 §4.1:

```sql
create type bike_position_enum as enum ('road_hoods','road_drops','road_clipons','tt_bike');
create type bike_helmet_enum   as enum ('standard','aero');
```

These live on `users`, not on `constraints`, because they are equipment facts rather than measured
physiology — they have no `source`, no staleness window, and no calibration path.

**Onboarding wording** (the athlete must recognise their own bike without knowing what CdA is):

> **What are you riding?**
> ○ Road bike — hands on the hoods
> ○ Road bike — hands in the drops most of the time
> ○ Road bike with clip-on aero bars
> ○ Time-trial or triathlon bike
>
> **Helmet?**  ○ Normal road helmet   ○ Aero or TT helmet

**CdA mapping** (from §I.2.3, `solver/tables/equipment.py`):

| Position | Base CdA | `first` | `improver` | `experienced` |
|---|---|---|---|---|
| `road_hoods` | 0.325 | 0.345 | 0.325 | 0.305 |
| `road_drops` | 0.300 | 0.320 | 0.300 | 0.280 |
| `road_clipons` | 0.280 | 0.300 | 0.280 | 0.260 |
| `tt_bike` | 0.255 | 0.275 | 0.255 | 0.235 |

`aero` helmet subtracts a further 0.010. Result clamped to [0.19, 0.38].

**Degradation when absent.** Fall back to `road_clipons` + `standard`. The fallback can sit up to 0.045 m²
from the athlete's true value — about 15 minutes over 180 km — so supplying it later will frequently cross
the drift thresholds. That is correct behaviour: it is new information that genuinely moves the plan.

### F.3 `pressure_hpa` and `cloud_cover_pct` — new fields on `ForecastSnapshot`

**Replacement text — Part 5 §5.1, `ForecastSnapshot`:** add the fields

```python
    pressure_hpa: float | None = None      # sea-level (QNH) pressure. None -> ISA standard 101325 Pa;
                                           # emits "forecast.pressure_hpa" in assumed_fields.
                                           # Treated as absent outside [870, 1085].
    cloud_cover_pct: float | None = None   # 0-100. None -> categorical mapping from `conditions`;
                                           # emits "forecast.cloud_cover_pct" in assumed_fields.
```

**Replacement text — Part 4 §4.4, `plans.forecast_snapshot jsonb` documented shape:**

```json
{
  "temp_c": 31.0,
  "humidity": 55,
  "wind_speed_ms": 3.0,
  "wind_dir_deg": null,
  "conditions": "clear",
  "water_temp_c": 22.5,
  "pressure_hpa": 1013.2,
  "cloud_cover_pct": 5
}
```

Both new keys are nullable. The snapshot is frozen at solve time and is part of `solve_input_hash`, so
adding them changes the hash for every plan — which is why `schema_version` bumps.

**Degradation.** `pressure_hpa` absent costs up to ±4% on the aerodynamic term (§I.1.1) — comparable to the
entire heat effect. `cloud_cover_pct` absent reintroduces the only discontinuity in the environment model
(§I.1.3). Both are declared in `assumed_fields` rather than silently defaulted.

**Adapter note that belongs in the ingest code, not the solver:** confirm whether the provider reports
sea-level (QNH) or station pressure. Passing station pressure as sea-level pressure is wrong by roughly
1.2% per 100 m of course elevation.

### F.4 `measured_at_temp_c` — new column on `constraints`

**Replacement text — Part 4 §4.2, `constraints` table:** add one column to the existing definition

```
measured_at_temp_c numeric null   -- dry-bulb air temperature (C) at which this value was measured.
                                  -- Currently meaningful for sweat_rate only; nullable for all keys.
```

**Replacement text — Part 5 §5.1, the constraint entry inside `AthleteSnapshot`:**

```python
@dataclass(frozen=True)
class ConstraintValue:
    key: str
    value: float
    unit: str
    source: ConstraintSource
    measured_at_temp_c: float | None = None   # sweat_rate only; None -> solver assumes
                                              # w_sweat_ref = 15.0 C WBGT and emits
                                              # "sweat_rate.measured_at_temp_c" in assumed_fields
```

**Onboarding wording**, appended to the sweat-rate step:

> **Roughly what temperature was it when you measured this?**
> We use it to scale your fluid plan to race-day heat. If you skip it we'll assume mild conditions —
> your plan will still work, we'll just mark it as an assumption.

**Degradation.** Fall back to `w_sweat_ref = 15.0 °C WBGT`. This matters more than it looks: an athlete who
sweat-tested on a hot day and one who tested indoors in winter currently get identical treatment from
identical stored values, and the resulting fluid plan can differ by 20% or more between those two readings.

### F.5 `Infeasibility` — earliest missed barrier, plus diagnostics

**This is a semantic change, not only a shape change.** The reported barrier changes from the *tightest* to
the *earliest missed*. Reasoning and the worked case are in §3.3.

**Replacement text — Part 5 §5.1:**

```python
@dataclass(frozen=True)
class Infeasibility:
    barrier: str                      # EARLIEST missed barrier, by limit_minutes_from_start.
                                      # This is where the athlete's race actually ends.
    miss_minutes: float               # how far past `barrier`'s limit, rounded to 0.1
    levers: tuple[str, ...]           # 1-2 keys, computed AT `barrier` (not at the tightest)
    tightest_barrier: str             # smallest margin across all gates; diagnostics only
    tightest_miss_minutes: float      # rounded to 0.1
```

**Replacement text — Part 5 §5.2, Stage 3, replacing the bullet beginning "If the tightest barrier cannot be
met…":**

> - If any barrier cannot be met even at the most conservative pacing the solver can produce, return
>   `Infeasibility`. The reported `barrier` is the **earliest missed** barrier by
>   `limit_minutes_from_start` — the point at which the athlete's race actually ends — not the one with the
>   smallest margin. Because timing error accumulates, an athlete who misses a mid-race bike cut-off
>   necessarily misses the finish by more, so reporting the smallest margin would almost always name the
>   finish and would materially misinform the athlete. The tightest barrier is still returned, as
>   `tightest_barrier`, for the admin blast-radius view. `levers` are computed at the reported barrier.
> - `worst_margin_minutes` is unchanged: the minimum margin across all gates, driving `margin_state`
>   (`clear` ≥ 20 min, `tight` 0–20 min, `bad` < 0).

**Replacement text — Part 13 §13.3, the `INFEASIBLE` error `details` object:**

```json
{
  "barrier": "bike_cutoff",
  "miss_minutes": 10.1,
  "levers": ["raise_ftp", "improve_run_pace"],
  "tightest_barrier": "finish",
  "tightest_miss_minutes": 131.8
}
```

The user-facing `message` must be built from `barrier`/`miss_minutes`, never from the tightest pair.

### F.6 `assumed_fields` — new field on `SolveOutput`

**Replacement text — Part 5 §5.1, `SolveOutput`:** add the field

```python
    assumed_fields: tuple[str, ...]   # sorted dotted paths of optional inputs that were absent
                                      # and for which the solver substituted a documented default.
                                      # Empty tuple when every optional input was supplied.
```

Possible values: `athlete.bike_setup`, `forecast.cloud_cover_pct`, `forecast.pressure_hpa`,
`sweat_rate.measured_at_temp_c`. Always sorted lexicographically, so it is deterministic and diffable in
golden files.

**Replacement text — Part 4 §4.4, `plans` table:** add one column

```
assumed_fields text[] not null default '{}'
```

Persisted with the plan so the UI can mark affected numbers, and so back-testing can exclude or stratify
plans that rested on assumptions. Rationale in §0.5b.

### F.7 What does *not* change

Stated explicitly so a reviewer can confirm the blast radius is bounded:

- The six stages, their order, and every stage invariant.
- `SolveInput`'s top-level shape — the four new inputs are fields on existing nested objects.
- Determinism, the `round_half_even` boundary, and the `solve_input_hash` construction *method* (the hashes
  themselves change because the input contains new keys).
- The 6 s SLA and the per-stage targets. The new inputs are all reads; §0.8's measurements are unaffected.
- `Feasibility`, `Split`, `Segment`, `Gate`, `Fuelling`, `AidAction`, `Bag`, `ConstraintRef`.
- The five-bag structure and the `reason_constraint_key` requirement.

### F.8 Golden cases affected

| Case | What changes |
|---|---|
| **All cases** | `schema_version` 1 → 2; every `SolveInput` gains four optional fields; every `SolveOutput` gains `assumed_fields`. All expected outputs must be regenerated |
| `G01`, `G07`, `G09`, `G11`, `G12` | Swim splits change under §4.4's corrected CSS model — every downstream ETA, margin and projected total moves with them |
| `G05-INFEASIBLE` | Miss at the finish is now **90.6 min** (was ≈86.1 under the withdrawn swim model). Earliest missed = tightest = `finish`, so the F.5 change does not alter this case's verdict |
| `G06-TIGHT` | Athlete `A-T` retuned to `ftp 176 / rtp 363 / css 133 / wt 77`; `worst_margin_minutes ≈ +6.2`, still `tight` |
| `G10-NIGHT` | Athlete `A-X` **replaced** — the previous profile was infeasible on `C-TRAM`, so it never reached the bag stage and tested nothing |
| `G13-NOWETSUIT` | **New.** Exercises the wetsuit-legality branch, previously untested |
| `G14-EARLIESTMISS` | **New.** The case that exercises F.5: earliest missed (`bike_cutoff`, 10.1 min) differs from tightest (`finish`, 131.8 min) |
| `G15-ASSUMED` | **New.** `G01` with all four optional inputs omitted, asserting `assumed_fields` contains exactly the four paths in sorted order |

---
