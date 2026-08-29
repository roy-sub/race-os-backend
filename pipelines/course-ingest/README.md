# `pipelines/course-ingest`

Builds RaceOS course bundles: **real roads, real elevation, fictional race names.**

The nine seeded races in the frontend's directory are invented, so no course files exist for them
and nothing can be downloaded. This pipeline generates them — routing along actual OpenStreetMap
ways and sampling actual terrain — so the solver gets truthful gradients, the map renders roads that
exist, and no race organiser's trademark or licensed course data is used.

It is not seed tooling. The same code ingests real licensed courses and athlete GPX uploads for
races not in the directory; the seeded nine are simply its first input.

---

## Quick start

```bash
pip install -e .

# one course
course-ingest generate specs/01-tramuntana-full.yaml

# every course marked `status: ready` (currently three)
course-ingest regenerate-all

# check an emitted fixture
course-ingest validate out/bundles/tramuntana-full.bundle.json

# the contact sheet, and the full review page
course-ingest visual-check
python tools/build_review_page.py

# cut-off margins for three athlete profiles
python tools/margin_check.py

# the determinism proof: two full runs, byte-compared
./tools/determinism_check.sh
```

Useful flags: `--dry-run` (validate without writing), `--skip-terrain` (no PMTiles extract, much
faster), `--skip-visuals`, `--cache DIR` (share the blob cache between runs — strongly recommended;
a cold first run downloads ~1 GB of Overture footers and DEM tiles, a warm one is near-instant).

---

## What comes out

Per course, in `out/`:

| Artefact | What it is |
|---|---|
| `bundles/<slug>.bundle.json` | The seed fixture. Shaped exactly as the `courses`, `course_bundles` and `course_bundle_legs` rows expect, with leg geometry as EWKT `LINESTRING Z` for direct PostGIS insert. |
| `bundles/<slug>.bundle.bin` | The packed bundle behind `course_bundles.bundle_asset_key`. Under the 400 KB budget. |
| `terrain/<slug>.pmtiles` | Terrarium DEM clipped to the course bounding box, for `course_bundles.terrain_pmtiles_key`. |
| `visual-check/<slug>.png` | Static map plus elevation profile. |
| `visual-check/contact-sheet.png` | Every generated course on one sheet. |
| `visual-check/three-course-review.html` | The full review page: per course a map, profile, distances, character verdict, cut-off ladder and margin spot-check. |

`out/` is git-ignored. Bundles and terrain extracts are build artefacts that belong in object
storage behind a CDN (Part 10.3), and a terrain extract is tens of megabytes. `make regenerate`
rebuilds everything from the specs; the specs and this code are the source of truth.

**The schema changes the backend needs are written up as exact replacement text in
[`docs/SCHEMA_CHANGES.md`](docs/SCHEMA_CHANGES.md).** Read that before loading a fixture.

---

## The ten stages

One module each, in `course_ingest/stages/`.

| | Stage | Notes |
|---|---|---|
| 1 | `s01_ingest` | Resolve the seed spec into targets and bounding boxes. |
| 2 | `s02_route` | Route bike and run along real ways to the required distance and character. |
| 3 | `s03_swim` | Draw the swim as a buoy course in real water. **The only drawn geometry in a bundle.** |
| 4 | `s04_clean` | Dedupe, drop outliers, close loops, split into legs. |
| 5 | `s05_mapmatch` | Snap bike and run onto road geometry. A verification pass on generated routes; the real work on the GPX path. |
| 6 | `s06_resample` | Re-space to ~10 m nodes. |
| 7 | `s07_elevation` | Sample the DEM for every node. |
| 8 | `s08_segments` | Per-node gradient, per-segment climb, named segments. |
| 9 | `s09_furniture` | Aid stations, transitions, special needs, distance markers, cut-off barriers. |
| 10 | `s10_emit` | Validate, then emit — or reject and write nothing. |

---

## Data sources, and why these ones

### Roads — Overture Maps `transportation/segment`, pinned

OSM-derived, ODbL-1.0, read straight from public S3 as GeoParquet using HTTP range requests and
row-group bbox statistics, so a course bbox costs tens of megabytes rather than the 72 GB the theme
weighs globally. The release is pinned in `config/sources.yaml` and carried into every bundle's
`provenance_detail.road_source`.

**Chosen over a hosted routing API** (OSRM, Valhalla, openrouteservice) for one reason above all:
determinism. Those services re-import OpenStreetMap continuously with no way to pin a snapshot, so
an identical request returns different geometry over time — which makes a season-over-season bundle
diff meaningless and the byte-identical guarantee impossible. They are also rate-limited, and they
do not carry `road_surface`, `names.primary` or `sources[].license` in the routing response, all
three of which the bundle needs.

Routing itself is a Dijkstra over a graph built from the ways' own `connectors`, so topology comes
from the data rather than from coordinate-rounding heuristics. Roads are treated as bidirectional:
triathlon legs are raced on closed roads.

**Swapping the engine** is an implementation of `RoadSource` (`course_ingest/sources/base.py`) —
two methods. A locally-run OSRM or a licensed course file plugs in there without touching a stage.

### Elevation — Terrarium-encoded DEM tiles

Sampled at z14 (~7 m/px at mid latitudes) with bilinear interpolation, which resolves 10 m nodes
without staircasing a ~30 m native DEM.

> **Substitution, flagged.** `RaceOS_Build_Spec.md` Part 2 names the **Mapterhorn** tileset
> (Copernicus GLO-30 + national LiDAR). Mapterhorn is not reachable from this build environment's
> egress policy, so **AWS Terrain Tiles** is used instead. The Terrarium encoding is identical, so
> the frontend's `raster-dem` source consumes either without change, and the swap back is one line:
> `config/sources.yaml → elevation.tile_url`. Coverage was verified at all nine course locations
> before any routing, including the two doubtful ones — Puig Major reads 1430 m against a true
> 1436 m summit, and Patagonia and Bergen both have full z14 coverage with no gaps.

**A missing DEM tile fails the build.** It is never interpolated across. A smooth, plausible-looking,
wrong hill is worse than a loud failure.

### Licensing

Attribution is **generated from the data**, not hardcoded: the pipeline reads `sources[].dataset` and
`sources[].license` from the ways each route actually used and assembles
`course_bundles.attribution` from them. On the seeded courses that comes out as:

```
© OpenStreetMap contributors, © TomTom, ODbL 1.0 · Elevation: AWS Terrain Tiles
```

ODbL obliges attribution wherever the derived data is displayed. **The UI surfaces that must carry
it:** the 2D and 3D map views, the no-WebGL static-map fallback, the elevation profile view, the
course detail page, the race-card PDF footer, `.FIT` and GPX export metadata, and any shared-plan or
share-link page that renders geometry.

---

## Things this pipeline refuses to do

**No fabricated elevation.** Every height is a DEM sample. Where the terrain series says a road
climbs at 200%, the route has crossed something the DEM cannot see — a tunnel, an unflagged viaduct,
a cutting. The response is to **route somewhere else**, not to invent a plausible height: tunnels and
covered ways are excluded from the graph outright, long bridges are excluded by length, shorter ones
are cost-penalised, and any way still carrying an impossible gradient after sampling is banned and
the leg is routed again (a fixed two passes, so the search terminates).

**No smoothing.** `SOLVER_MODEL.md` §1.2 forbids it, because smoothing flattens a climb's peak
gradient and makes it cheaper than it is. The delivered node series is exactly what the DEM
returned. The windowed gradient in Stage 8 is used *only* to decide where a segment boundary falls.

**No synthetic geometry where real geometry is possible.** The swim is drawn — there are no roads in
water — and it is still constrained to lie inside a real water body with a configured shoreline
clearance. Bike and run follow real ways for every metre.

**Nothing invented is stamped as official.** Aid stations, transitions, special needs, distance
markers and cut-offs are all generated from the rules in `config/furniture.yaml`, and every one of
them carries `provenance: ESTIMATED`.

---

## Two elevation-gain numbers, and why

`course_bundle_legs.elevation_gain_m` is **hysteresis-filtered** surveyed ascent: a rise counts once
it clears 3 m above the last reversal (`config/course.yaml → elevation.gain_threshold_m`). This is
the figure a UI should show.

The reason is measured, not assumed. A DEM sampled every 10 m has a vertical noise floor, and an
unfiltered sum credits that noise as climbing. Skagen's genuinely flat dune coast — total relief
24 m over a 90 km bike leg — reports 3.7 m/km unfiltered and 2.0 m/km filtered. Setting terrain
character bands against the unfiltered number would have been calibrating against DEM noise.

`SOLVER_MODEL.md` §1.1 defines a segment's gain as the plain sum of positive node differences, and
derives it from the node series, so it is unaffected. `segments[].elevation_gain_m` uses that raw
definition so the two can never disagree, and `elevation_profile.legs[*]` carries both as `gain_m`
and `gain_m_raw_nodes`. Neither is smoothing; both are statistics over an untouched series.

---

## Configuration, not conditionals

Every threshold, spacing, cost, ratio and rule lives in `course_ingest/config/`:

| File | Holds |
|---|---|
| `sources.yaml` | Pinned Overture release, DEM tileset and zooms, cache, attribution templates. |
| `routing.yaml` | Per-leg road-class costs, terrain character (climb bias, waypoint percentile, **gain bands**), structure handling, loop search, map-matching. |
| `course.yaml` | Nominal distances and tolerances, node spacing, gain threshold, Overture-surface → `surface_quality` map, segmentation bands and naming rules, swim shapes, validation limits. |
| `furniture.yaml` | Aid-station spacing and contents, special-needs placement, distance-marker intervals, the cut-off reference ladder. |

An **unmapped road surface fails the build** rather than defaulting. `SOLVER_MODEL.md` §I.2.2 turns
`surface_quality` into `Crr`, and the gap between `typical_road` and `rough_chipseal` is worth about
eight minutes over 180 km — too much to guess.

---

## Aid stations, special needs and cut-offs — the rules

All invented, all `ESTIMATED`, all generated from `config/furniture.yaml`.

**Aid stations** — `first_km` plus a fixed `spacing_km` per leg per distance type. Full distance:
bike every 25 km from km 20 (7 stations), run every 2.1 km (20 stations), which is conventional
long-course spacing and matches the reference structure in `SOLVER_MODEL.md` §B.1. Every second
bike station and every fourth run station carries the full-service contents list. A station closer
than `min_tail_km` to the finish is dropped.

**Special needs** — at the leg midpoint, then snapped to the nearest aid station within 4 km so the
bag sits somewhere an athlete actually stops. Bike and run at full distance, bike only at 70.3, none
below that.

**Cut-offs** — one reference ladder per distance type, scaled by a single per-course
`cutoff_generosity` dial in the spec. The Full row is the structure from the brief; the other three
reproduce `SOLVER_MODEL.md` §B.1's `C-HALF`, `C-OLY` and `C-SPR` exactly, so seeded courses and
golden fixtures agree on the *shape* of a cut-off while remaining separate artefacts.

The intermediate bike checkpoint sits at 66.7% of bike distance but 75.5% of the swim-exit-to-bike-
cutoff time window — the two fractions differ because athletes slow through a long bike leg. At full
distance that reproduces km 120 at 510 minutes.

Making a course harder is one number in its spec. See `docs/CUTOFF_LADDER.md` for the spread across
the nine and why three of them are deliberately tight.

---

## The shipping set, and the six deferred courses

Three courses are generated: **Tramuntana Full** (full distance, mountainous),
**Kalmar 70.3** (flat coastal, generous cut-offs) and **Skagen 70.3** (flat and exposed, deliberately
tight cut-offs). Between them they cover both primary distances, both terrain extremes, and a
feasibility spread from CLEAR to INFEASIBLE.

The other six specs are complete and marked `status: pending`. `regenerate-all` skips them; pass
`--include-pending` to build them. **Their coordinates, terrain character, lap structure and cut-off
dial are settled and reviewed — nothing needs re-deriving.** Change `status: pending` to
`status: ready` and generate.

## Adding a course

1. Copy a spec from `specs/` and edit it. Required: `course_id`, `name`, `place`, `country`,
   `timezone`, `distance_type`, `difficulty`, `character` (one of the keys in
   `routing.yaml → character`), a `start` lat/lng **on the shoreline**, a `swim` block naming the
   water kind and the bearing to swim out on, and `bike`/`run` blocks with lap counts and a
   `bearing_offset_deg` pointing the loop at the terrain you want.
2. `course-ingest generate specs/<your-file>.yaml`.
3. Look at `out/visual-check/<slug>.png`. This is the review step that matters — does it look like a
   race someone would enter?
4. If the character check rejects it, the terrain did not match the claim. Either the declared
   character is wrong or the `bearing_offset_deg` is pointing the loop the wrong way. Fixing it by
   widening the gain band is almost always the wrong answer; the band is what stops a "brutal"
   course coming out flat.

Nothing else needs touching. There is no code path per course.

---

## Swapping a generated course for a real licensed one

The bundle shape is the contract, not how the bundle was made.

1. Implement `RoadSource` over the licensed geometry — or, for a course delivered as a track rather
   than a route, feed the track into stages 4–10 directly and skip stages 2 and 3. Stage 5's
   map-matcher exists for exactly this input.
2. Change `provenance` to `OFFICIAL` in the spec and set `verified_at`.
3. Replace the generated furniture with the published aid stations and cut-offs, changing their
   `provenance` from `ESTIMATED` to `OFFICIAL` as you do. This is the step that must not be skipped:
   a real course with invented aid stations still stamped `ESTIMATED` is honest, but a real course
   with invented aid stations stamped `OFFICIAL` is not.
4. Bump `version`, keep `course_id` and `slug`. Part 6.1's blast-radius preview will show what moves
   for every plan pinned to the old bundle before anything publishes.

The athlete GPX upload path (Part 10.5) is the same: parse the file into a point list, then run
stages 4–10. If the file carries no elevation, Stage 7 supplies it; if it carries barometric
elevation, Stage 7 discards it.

---

## Golden fixtures stay synthetic — do not wire them to this pipeline

`SOLVER_MODEL.md` §B.1 defines `C-TRAM`, `C-FLAT`, `C-ALTA`, `C-HALF`, `C-OLY` and `C-SPR` with
node series generated to reproduce exact net gradients. They are checked in as static files under
`backend/tests/golden/courses/` and are **entirely separate from the nine seeded bundles**, even
though `C-TRAM` shares a name and coordinates with Tramuntana Full.

If a golden case ever read a pipeline-generated bundle, regenerating a course would silently break
the golden suite, and the solver's determinism guarantee would become dependent on the routing
engine. `tests/test_golden_isolation.py` asserts the separation.

---

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite runs **offline**, against a checked-in slice of real Overture roads and real Terrarium
tiles in `tests/fixtures/`. It covers determinism (the pipeline runs twice and the bytes are
compared), every validation rule (each has a case that trips it), one end-to-end generation, and the
golden-fixture isolation guard.

Rebuild the fixtures — needs network — with `python tests/build_fixtures.py`.

---

## Performance

A cold, uncached first course spends most of its time downloading: ~260 MB of Overture Parquet
footers for the row-group manifest (once, then cached for every later course) and the DEM tiles under
its bbox. Warm, a full-distance course takes roughly two to four minutes, dominated by routing
(~60–90 s per leg for the fixed radius scan and bisection, doubled when a re-route pass fires).
`regenerate-all` across the nine is well under an hour warm.
