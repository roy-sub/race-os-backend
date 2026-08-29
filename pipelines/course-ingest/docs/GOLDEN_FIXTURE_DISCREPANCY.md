# `SOLVER_MODEL.md` §B.1 — declared elevation gain does not match the stated segment gradients

**Raised by:** Session B (course data pipeline), while generating the solver's golden course fixtures.
**Owner:** Session A / whoever maintains `SOLVER_MODEL.md`.
**Status:** Fixtures generated and checked in. The discrepancy is recorded, not resolved — resolving
it means editing `SOLVER_MODEL.md`, which this session deliberately did not do.
**Impact:** None on the seeded course bundles. None on the solver's arithmetic. It is a
documentation inconsistency that will confuse the next reader, and would fail a naive assertion
written against the document.

---

## What was found

`SOLVER_MODEL.md` §B.1 specifies each golden course two ways at once:

1. **Constructively** — a list of segments with lengths and net gradients, plus the rule
   *"Elevation series: 100 m node spacing, generated to reproduce each segment's net gradient
   exactly."*
2. **By aggregate** — a stated total elevation gain, e.g. *"Bike 180.2 km … 2100 m gain."*

Following rule (1) exactly does not produce the number in (2). Four courses disagree:

| Course | Leg | Gain from §B.1's own segment gradients | §B.1's declared gain | Difference |
|---|---|---|---|---|
| `C-TRAM` | bike | **1518.2 m** | 2100 m | −581.8 m (−27.7%) |
| `C-TRAM` | run | **73.8 m** | 180 m | −106.2 m (−59.0%) |
| `C-FLAT` | bike | **135.0 m** | 220 m | −85.0 m (−38.6%) |
| `C-ALTA` | bike | **3565.0 m** | 3900 m | −335.0 m (−8.6%) |

The arithmetic for `C-TRAM`'s bike leg, which is the clearest case — sum of `length × gradient` over
the twelve segments, positive terms only:

```
24.0 km × +0.002 =  48.0        21.0 km × +0.010 = 210.0
18.0 km × +0.004 =  72.0         5.8 km × +0.049 = 284.2
 9.6 km × +0.018 = 172.8        26.0 km × +0.006 = 156.0
 8.4 km × +0.058 = 487.2        22.0 km × +0.001 =  22.0
                                22.0 km × +0.003 =  66.0
                              ------------------------------
                                total ascent      1518.2 m
```

`C-TRAM`'s run is stated as *"four laps of 10.549 km, each `[+0.004, −0.004, +0.003, −0.003]` over
equal quarters"*. Each lap climbs `2.63725 km × (0.004 + 0.003) = 18.46 m`; four laps give 73.8 m,
against the declared 180 m.

`C-OLY` and `C-SPR` show the same pattern (40 m vs 80 m declared; 20 m vs 40 m declared), consistent
with the declared figures being *round-trip* totals — ascent plus descent — rather than ascent. That
reading fits `C-OLY` and `C-SPR` exactly (their legs are symmetric out-and-backs, so ascent equals
descent and the declared figure is exactly double). It does **not** fit `C-TRAM`, whose bike segments
are asymmetric: ascent 1518.2 m, descent 762.0 m, sum 2280.2 m, against a declared 2100 m. So
"declared = ascent + descent" is a good hypothesis for the short courses and not a complete
explanation for the long ones.

---

## The resolution taken, and why

**The constructive rule wins. The segment gradients are the more specific statement**, they are the
thing the solver actually reads, and they are internally consistent: `SOLVER_MODEL.md` §1.4's worked
example derives *"Coll de Femenia, 8.4 km at 5.8%"* from the node series rather than from a stored
label, and §1.1 defines a segment's `elevation_gain_m` as `Σ max(0, h_{j+1} − h_j)` over the
delivered nodes. A fixture built to match the declared aggregate would have to contradict at least
one stated segment gradient, which would break §1.4's worked example — the one piece of arithmetic in
the document an implementer is told to check their code against.

Each fixture therefore carries **both** figures, explicitly named, so nothing is hidden:

```json
"BIKE": {
  "elevation_gain_m": 1518.2,             // computed from the delivered node series
  "declared_elevation_gain_m": 2100,      // as stated in SOLVER_MODEL.md B.1
  ...
}
```

`elevation_gain_m` is the one a golden case should assert against; it is reproducible from the nodes.
`declared_elevation_gain_m` is carried purely so this discrepancy stays visible to the next reader
rather than being quietly absorbed.

---

## The change `SOLVER_MODEL.md` needs

One line per affected course, in §B.1. For `C-TRAM`:

> **Replace:**
> `surface_quality = typical_road`, mean elevation 120 m, **2100 m gain**.
>
> **With:**
> `surface_quality = typical_road`, mean elevation 120 m, **1518 m ascent / 762 m descent** (both
> derived from the segment gradients below; the elevation series reproduces them exactly).

And the same substitution for the other three rows:

| Course | Leg | Replace | With |
|---|---|---|---|
| `C-TRAM` | run | `180 m gain` | `74 m ascent / 74 m descent` |
| `C-FLAT` | bike | `220 m gain` | `135 m ascent / 135 m descent` |
| `C-ALTA` | bike | `3900 m gain` | `3565 m ascent / 2635 m descent` |

State ascent and descent separately throughout. That removes the ambiguity that produced this in the
first place — "gain" was almost certainly meant as round-trip vertical on the short courses and as
ascent on the long ones, and a single word cannot carry both.

---

## What this does *not* affect

- **The nine seeded course bundles.** They are generated from real terrain and never read §B.1.
- **The solver's arithmetic.** Nothing in Part II reads a declared aggregate; §1.1 derives gain from
  the node series.
- **Golden determinism.** The fixtures are static files, and
  `pipelines/course-ingest/tests/test_golden_isolation.py` asserts they can never resolve to
  pipeline output.

## Where the fixtures are

```
backend/tests/golden/build_golden_courses.py     the generator (imports nothing from course_ingest)
backend/tests/golden/courses/C-TRAM.json         and C-FLAT, C-ALTA, C-HALF, C-OLY, C-SPR
```

Two further places where §B.1 is under-specified, resolved by construction and noted here for
completeness:

- **`C-HALF`'s bike leg** is *"segments 1–6 of `C-TRAM` truncated to 90.1 km"*, but segments 1–6 sum
  to 88.2 km. The fixture takes segments 1–6 in full plus the first 1.9 km of segment 7
  (`Coll d'Honor`) to reach 90.1 km.
- **`C-ALTA`'s run leg** is given only as aggregates (42.195 km, 620 m gain, mean 640 m); §B.1 states
  no segments. The fixture uses a symmetric four-lap profile scaled so total ascent is exactly 620 m
  and mean elevation exactly 640 m, reproducing the document's stated numbers rather than inventing
  different ones.
