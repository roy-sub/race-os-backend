# Session B handover — the course data pipeline

**What this is:** everything you need to work on `pipelines/course-ingest/` without having been in
the session that built it. It assumes no prior context.

**Status at handover:** the pipeline is complete and three of nine seeded courses are generated.
The other six have finished seed specs marked `status: pending` and are deferred until after launch.

---

## 1. Why this exists

RaceOS is a race-planning product for long-course triathletes. The frontend ships a directory of nine
races whose names are **invented on purpose** — real Ironman-branded events are trademarked and their
course data is licensed commercially, so seeding the database with real branded races would create
exposure before any conversation has happened.

The consequence: no course files exist for those nine, and nothing can be downloaded. Without course
geometry the map renders nothing, the solver has no gradients, the elevation profile is empty and the
`.FIT` export has no route.

**The approach: fictional race name, real terrain.** The pipeline routes along actual OpenStreetMap
ways and samples actual public terrain data. "Tramuntana Full" is not a real event, but its bike leg
follows real Mallorcan roads with real gradients. The solver gets truthful terrain, the map renders
roads that exist, and nobody's trademark is used.

**This is not seed tooling.** The same code path ingests real licensed courses and handles athlete
GPX uploads for races not in the directory. The seeded courses are simply its first input.

---

## 2. What comes out

Per course, in `out/` (git-ignored — these are build artefacts):

| Artefact | Purpose |
|---|---|
| `bundles/<slug>.bundle.json` | The seed fixture. Shaped exactly as the `courses`, `course_bundles` and `course_bundle_legs` rows expect, with leg geometry as EWKT `LINESTRING Z` for direct PostGIS insert. |
| `bundles/<slug>.bundle.bin` | The packed bundle behind `course_bundles.bundle_asset_key`. Under the 400 KB budget. |
| `terrain/<slug>.pmtiles` | Terrarium DEM clipped to the course bounding box, for `course_bundles.terrain_pmtiles_key`. |
| `visual-check/<slug>.png` | Static map plus elevation profile, for human review. |

**Read `docs/SCHEMA_CHANGES.md` before loading a fixture.** It carries the exact replacement text for
`RaceOS_Build_Spec.md` Part 4.3 and Part 10.2, plus the migration DDL for the five schema additions
this pipeline depends on.

---

## 3. The ten stages

One module each, in `course_ingest/stages/`. `course_ingest/pipeline.py` orchestrates them.

| | Stage | What it does |
|---|---|---|
| 1 | `s01_ingest` | Resolve the seed spec into leg targets, lap structure and bounding boxes. |
| 2 | `s02_route` | Route bike and run along real ways to the required distance and terrain character. |
| 3 | `s03_swim` | Draw the swim as a buoy course inside a real water body. **The only drawn geometry in a bundle.** |
| 4 | `s04_clean` | Drop duplicate vertices and positional outliers, close loops. |
| 5 | `s05_mapmatch` | Snap bike and run onto road geometry. A verification pass on generated routes; the real work on the GPX path. |
| 6 | `s06_resample` | Re-space to ~10 m nodes. |
| 7 | `s07_elevation` | Sample the DEM for every node. |
| 8 | `s08_segments` | Per-node gradient, per-segment climb, named segments. |
| 9 | `s09_furniture` | Aid stations, transitions, special needs, distance markers, cut-off barriers. |
| 10 | `s10_emit` | Validate, then emit — or reject and write nothing. |

**One structural note that will otherwise confuse you:** stages 4–6 run *inside* stage 2's loop for
the bike and run legs. Cleaning and map-matching shorten a routed leg by a few hundred metres, so the
length that survives them is not the length that was routed. The pipeline measures the delivered leg
and re-cuts the distance-making spur until it lands inside tolerance — a fixed pass count, and only
the spur is re-routed, never the loop. The swim runs 4 and 6 separately; it has no road to match to.

---

## 4. Data sources, pinned versions and licences

### Roads — Overture Maps `transportation/segment`

- **Licence: ODbL-1.0.** OSM-derived. Records also carry TomTom contributions, themselves ODbL.
- **Pinned release: `2026-08-19.0`**, in `config/sources.yaml → roads.release`, and carried into every
  bundle at `provenance_detail.road_source`.
- Read from public S3 (`overturemaps-us-west-2`) as GeoParquet over HTTP range requests, using
  row-group bbox statistics. The theme is 72 GB globally; a course bbox costs ~80 MB.
- **Why not a hosted routing API** (OSRM, Valhalla, openrouteservice): they re-import OpenStreetMap
  continuously with no way to pin a snapshot, so identical input stops producing identical output
  over time — which makes the determinism guarantee impossible and a season-over-season bundle diff
  meaningless. They are also rate-limited, and they do not return `road_surface`, way names or
  per-record licences, all three of which the bundle needs. Separately, every hosted OSM host was
  blocked by the build environment's egress policy, so the choice was also forced.

### Elevation — Terrarium-encoded DEM tiles

- **Source: AWS Terrain Tiles** (`elevation-tiles-prod`). Public, no key.
- Sampled at **z14** (~7 m/px at mid latitudes), bilinear.
- **Substitution, flagged:** `RaceOS_Build_Spec.md` Part 2 names the **Mapterhorn** tileset
  (Copernicus GLO-30 + national LiDAR). Mapterhorn was unreachable from the build environment. The
  Terrarium encoding is identical — `(R × 256 + G + B / 256) − 32768` metres — so the frontend's
  `raster-dem` source consumes either without change. **Swapping back is one line:**
  `config/sources.yaml → elevation.tile_url`. Coverage was verified at all nine course locations
  before any routing (Puig Major reads 1430 m against a 1436 m summit; Patagonia and Bergen both have
  full z14 coverage).

### Attribution

Generated **from the data**, not hardcoded: the pipeline reads `sources[].dataset` and
`sources[].license` from the ways each route actually used and assembles `course_bundles.attribution`
from them.

ODbL obliges attribution wherever the derived data is displayed. **Surfaces that must carry it:** the
2D and 3D map views, the no-WebGL static-map fallback, the elevation profile view, the course detail
page, the race-card PDF footer, `.FIT` and GPX export metadata, and any shared-plan or share-link page
that renders geometry.

---

## 5. Configuration — every knob and what it controls

Nothing in the pipeline inlines a threshold, spacing, cost or ratio. All of it is in
`course_ingest/config/`.

### `sources.yaml`
| Key | Controls |
|---|---|
| `roads.release` | The pinned Overture snapshot. **Changing this changes output; bump the bundle version.** |
| `roads.bucket_url`, `*_prefix` | Where the Parquet lives. |
| `elevation.tile_url` | The DEM tileset. One line to swap Mapterhorn back in. |
| `elevation.sample_zoom` | DEM sampling zoom (14). Higher is finer and slower. |
| `elevation.extract_min_zoom` / `extract_max_zoom` | Zoom range packed into the per-course PMTiles extract (8–13). |
| `elevation.fail_on_missing_tile` | Leave `true`. A missing tile must fail loudly, never interpolate. |
| `cache.dir` | Blob cache. Share it between runs; a cold first run downloads ~1 GB. |
| `licensing.*` | Display templates for the generated attribution string. |

### `routing.yaml`
| Key | Controls |
|---|---|
| `class_cost.bike` / `.run` | Relative cost per OSM road class. `null` means not routable for that leg. This is what keeps a bike leg off motorways and farm tracks. |
| `structures.excluded_flags` | Way flags removed from the graph outright — tunnels, covered, indoor, abandoned. The DEM cannot see under a tunnel. |
| `structures.bridge_cost_multiplier`, `max_bridge_length_m` | Bridges are discouraged and excluded past a length where the DEM reads the gap below as a gorge. |
| `structures.max_reroute_passes` | How many times a leg is re-routed after banning ways whose terrain is impossible. |
| `character.<name>.climb_bias` | Reshapes edge cost by gradient. Positive makes uphill cheap (mountainous seeks climbs); negative makes it dear (flat avoids them). |
| `character.<name>.waypoint_relief_fraction` | How far up the **local relief** waypoints aim. See §7, decision 3. |
| `character.<name>.min_gain_per_km` / `max_gain_per_km` | The enforced gain band. Measured against filtered ascent (§7, decision 1). |
| `loop.waypoint_count_by_distance` | Waypoints per lap by distance type. |
| `loop.waypoint_arc_deg` | The arc waypoints spread over, per character. A coastal start cannot reach mountains in every direction. |
| `loop.cul_de_sac_penalty_m` | Soft penalty on a non-junction waypoint, in metres of elevation error. |
| `loop.bisection_iterations`, `radius_bracket_fraction` | The fixed-length ring-radius search. |
| `loop.length_correction_passes` | How many times the spur is re-cut to hit the distance tolerance. |
| `loop.repeat_edge_penalty` | Discourages retracing the same road on one lap. |
| `map_match.max_snap_distance_m`, `continuity_window_m` | Map-matching bounds. |

### `course.yaml`
| Key | Controls |
|---|---|
| `distances` | Nominal leg distances per distance type. |
| `distance_tolerance` | How far a delivered leg may sit from nominal (±0.5%). |
| `resample.node_spacing_m` | Node spacing (10 m). |
| `elevation.gain_threshold_m` | Hysteresis threshold for reported ascent (§7, decision 1). |
| `surface_map` | Overture `road_surface` → `surface_quality`. **An unmapped value fails the build**; it must not be guessed, because it becomes `Crr`. |
| `segmentation.bands` | Gradient bands that define where a segment boundary falls. |
| `segmentation.min_segment_m` / `max_segment_m` / `max_segments` | Segment sizing. `max_segment_m` is what gives a flat marathon per-lap segments instead of one 42 km block. |
| `segmentation.naming.*` | How a segment takes an OSM way's name, and the blocklist. |
| `swim.*` | Buoy shapes per distance, aspect ratio, shore offset, water clearance. |
| `validation.*` | Every rejection threshold. |

### `furniture.yaml`
Aid-station spacing and contents per leg and distance, full-service cadence, special-needs placement,
distance-marker intervals, and the cut-off reference ladder. Full rationale in `docs/CUTOFF_LADDER.md`.

---

## 6. Running it

```bash
pip install -e ".[dev]"

course-ingest generate specs/01-tramuntana-full.yaml   # one course
course-ingest regenerate-all                            # every `status: ready` course
course-ingest regenerate-all --include-pending          # including the six deferred ones
course-ingest validate out/bundles/tramuntana-full.bundle.json
course-ingest visual-check                              # the contact sheet
python tools/margin_check.py                            # cut-off margins for two athlete profiles
```

Flags: `--dry-run`, `--skip-terrain` (much faster while iterating), `--skip-visuals`,
`--cache DIR` (**share this between runs**).

`make regenerate`, `make test`, `make validate`, `make golden`.

### Adding a course later

1. Copy a spec from `specs/` and edit it. Required: `course_id`, `name`, `place`, `country`,
   `timezone`, `distance_type`, `difficulty`, `character` (a key in `routing.yaml → character`), a
   `start` lat/lng **on the shoreline**, a `swim` block with water kind and offshore bearing, and
   `bike`/`run` blocks with lap counts and a `bearing_offset_deg` pointing the loop at the terrain
   you want.
2. `course-ingest generate specs/<file>.yaml`.
3. **Look at `out/visual-check/<slug>.png`.** This is the review step that matters — does it look
   like a race someone would enter?
4. If the character check rejects it, the terrain did not match the claim. Either the declared
   character is wrong or `bearing_offset_deg` points the loop the wrong way. **Move the start point
   or the bearing; do not widen the gain band** — the band is what stops a "brutal" course coming
   out flat.

There is no code path per course.

### The six deferred courses

`specs/03`, `04`, `05`, `07`, `08`, `09` are marked `status: pending` and skipped by
`regenerate-all`. Their coordinates, terrain character, lap structure and cut-off dial are all
settled and reviewed — **nothing needs re-deriving**. Change `status: pending` to `status: ready` and
generate.

### Swapping a generated course for a real licensed one

The bundle shape is the contract, not how the bundle was made.

1. Implement `RoadSource` (`course_ingest/sources/base.py` — two methods) over the licensed geometry.
   For a course delivered as a track rather than a route, feed the track into stages 4–10 directly and
   skip 2 and 3; stage 5's map-matcher exists for exactly that input.
2. Set `provenance: OFFICIAL` in the spec and set `verified_at`.
3. **Replace the generated furniture with the published aid stations and cut-offs**, changing their
   provenance from `ESTIMATED` to `OFFICIAL` as you do. This step must not be skipped: a real course
   carrying invented aid stations still marked `ESTIMATED` is honest; the same course with them
   marked `OFFICIAL` is not.
4. Bump `version`, keep `course_id` and `slug`. Part 6.1's blast-radius preview will show what moves
   for every pinned plan before anything publishes.

The athlete GPX upload path (Part 10.5) is the same shape: parse the file into a point list, then run
stages 4–10. If the file has no elevation, stage 7 supplies it; if it has barometric elevation,
stage 7 discards it.

---

## 7. Four decisions that were flagged, with reasoning

These were raised at the time rather than made silently. Each is reversible in config.

### 1. Reported ascent is hysteresis-filtered at 3 m

`course_bundle_legs.elevation_gain_m` and `courses.elevation_gain_m` count a rise only once it clears
3 m above the last reversal (`course.yaml → elevation.gain_threshold_m`).

**Why:** a DEM sampled every 10 m has a vertical noise floor, and an unfiltered sum credits that noise
as climbing. Measured: Skagen's genuinely flat dune coast reports **3.7 m/km unfiltered against
2.0 m/km filtered**. Setting terrain character bands against the unfiltered figure would have been
calibrating against DEM noise rather than terrain.

**What it does not affect:** `SOLVER_MODEL.md` §1.1 defines a segment's gain as the plain sum of
positive node differences and derives it from the node series, so the solver is untouched.
`segments[].elevation_gain_m` uses that raw definition so the two can never disagree, and
`elevation_profile.legs[*]` carries both as `gain_m` and `gain_m_raw_nodes`. **Nothing is smoothed** —
both are statistics over an untouched series.

### 2. Tunnels and long bridges are routed around, not patched

Tunnels, covered and indoor ways are excluded from the graph; bridges are cost-penalised and excluded
past `max_bridge_length_m`; and any way still carrying an impossible gradient after sampling is banned
and the leg routed again.

**Why:** a DEM cannot see under a tunnel — the sample is the mountain above it — and reads a long
viaduct as the gorge below. Both would put fabricated elevation into a bundle. The brief's rule is
that every height comes from the DEM, so the answer is to route somewhere else rather than to invent a
plausible height.

### 3. Waypoints aim at a fraction of local relief, not a node percentile

**Why:** a road network is dominated by dense town streets. On Mallorca the 97th-percentile *node*
sits at 212 m while the network reaches 834 m, so percentile targeting put the flagship mountain
course in the foothills at 9.7 m/km. Relief-fraction targeting reaches 12.5 m/km on the same start
point and the same distance.

### 4. `SOLVER_MODEL.md` §B.1's declared elevation gains contradict its own segment gradients

Four mismatches, up to −59%. The golden fixtures follow the constructive rule (the segment gradients)
because it is the more specific statement and the one the solver reads. Both figures are carried in
every fixture.

**Full write-up, including the exact one-line change §B.1 needs:
[`docs/GOLDEN_FIXTURE_DISCREPANCY.md`](GOLDEN_FIXTURE_DISCREPANCY.md).** This is Session A's to
resolve; this session did not edit `SOLVER_MODEL.md`.

---

## 8. Validation — what each rule protects against

A bundle failing any rule is **rejected and no artefact is written**, so a bad bundle cannot be
mistaken later for a good one. `course-ingest validate <fixture>` re-runs exactly what `generate`
ran, so a hand-edited fixture cannot slip past.

| Rule | Protects against |
|---|---|
| `legs_present` | A bundle the solver cannot load at all. |
| `distance_<leg>` | A marathon 700 m short. Tolerance ±0.5%. |
| `barriers_present`, `barrier_order` | `SOLVER_MODEL.md` §1.2: zero barriers is a data error, and a bike cut-off before the swim exit is a corrupt bundle. |
| `barrier_km_*`, `aid_station_km`, `waypoint_km` | Furniture positioned off the end of its own leg. |
| `aid_stations_pure` | Anything but an aid station in `aid_stations`. "One action per aid station" (§5.5) is a solver correctness property; keeping the array pure makes it hold by construction. |
| `node_count_<leg>` | Geometry, declared node count and profile disagreeing — the classic way an elevation series desynchronises from its geometry. |
| `gradient_<leg>` | Implausible gradients. Limit 2% of nodes over 30%, mirroring §1.3's `node_clamp_fail_fraction`. |
| `hard_gradient_<leg>` | A route that jumped a valley, or crossed a structure the DEM cannot see. No road exceeds 100%. |
| `elevation_range_<leg>` | A DEM that returned a constant — a dead tile, a stubbed source. Distinct from the character check. |
| `character_<leg>` | **A "brutal" course whose bike leg comes out flat.** This is what protects the seeded feasibility states. |
| `bundle_size` | The map's instant-load path. 400 KB. |
| `elevation_source` | §1.2 raises `BundleIncomplete` for anything but `terrain`. Better to fail here than in a solve. |
| `attribution` | Shipping ODbL-derived geometry with no attribution. A licence obligation, not a courtesy. |
| `segments_<leg>` | Segments that do not tile the leg. The solver aggregates over `[from_km, to_km)`; a gap is silently unpaced road. |

---

## 9. Determinism

**The same seed spec produces byte-identical output on every run.** This is what makes a
season-over-season bundle diff meaningful: if the output moved, an input moved.

How it is achieved: the road-data snapshot is pinned; there is no randomness, no clock and no
tolerance-terminated loop in the numeric path; every collection is iterated in an explicit sorted
order; ties break on explicit total-order keys; the ring-radius search is a fixed-length scan plus a
fixed number of bisections; the packed bundle uses no compression (zlib output can vary between
library builds); and every float is formatted at fixed precision rather than `repr`'d.

Proven by `tests/test_determinism.py`, which runs the whole pipeline twice and compares bytes.

---

## 10. Tests

```bash
pytest
```

Runs **offline**, against a checked-in 1.8 MB slice of real Overture roads and real Terrarium tiles in
`tests/fixtures/`. Covers determinism, every validation rule (each has a case that trips it), one
end-to-end generation, the codec, and the golden-fixture isolation guard.

Rebuild the fixtures (needs network): `python tests/build_fixtures.py`.

### The golden-fixture isolation guard — read this before touching it

`SOLVER_MODEL.md` §B.1 defines six synthetic golden courses (`C-TRAM`, `C-FLAT`, `C-ALTA`, `C-HALF`,
`C-OLY`, `C-SPR`) as frozen inputs to the solver's regression suite. They live at
`backend/tests/golden/courses/` as static files, generated by
`backend/tests/golden/build_golden_courses.py`, which imports nothing from `course_ingest`.

`C-TRAM` shares a name and coordinates with the seeded Tramuntana Full course, **which is exactly why
the guard exists.** If a golden case ever resolved to a pipeline-generated bundle, regenerating a
course would silently move the golden expectations, and the solver's determinism guarantee would stop
being a property of the solver and become a property of the routing engine.

`tests/test_golden_isolation.py` asserts: all six exist as static files; none resolves inside a
pipeline output path; each declares itself synthetic; none carries pipeline markers; the golden
builder does not import the pipeline; and no pipeline module names the golden directory.

**Never wire a golden case to `out/bundles/`.**

---

## 11. Known limitations

- **DEM resolution sets a floor on gradient fidelity.** The native model is ~30 m; a road cut into a
  hillside will always produce a few nodes over 30% because the cell straddles road and rock face.
  The build tolerates a small fraction of these and re-routes once when there are too many. Chasing
  them to zero is not achievable with public terrain data.
- **Bridges and tunnels are avoided rather than modelled.** A course whose only plausible route
  crosses a long viaduct will route around it or fail. Correct given the no-fabricated-elevation
  rule, but it does constrain some start points.
- **One-way restrictions are ignored.** Triathlon legs are raced on closed roads; modelling ordinary
  traffic restrictions would produce detours a race would never take. This means a generated route
  may run against the flow of a one-way street that a real event would close.
- **The swim is drawn, not surveyed.** It is constrained to lie inside a real water body with a
  shoreline clearance margin, but depth, currents, tides and navigational hazards are not modelled.
- **Swim elevation is the DEM median over the course, held level.** A water surface is level by
  definition; sampling per node would inject gradient into a leg that physically has none.
- **All furniture is invented.** Aid stations, transitions, special needs, distance markers and
  cut-offs are generated from rules and stamped `ESTIMATED`. They are plausible, not official.
- **`tools/margin_check.py` is not the solver.** It omits heat, wind, altitude, air density, the
  barrier-protection grid, fuelling and every clamp. Expect it a few per cent optimistic.
- **Terrain PMTiles extracts are large** (~12 MB per course at z8–13) and git-ignored. They belong in
  object storage behind a CDN, per Part 10.3.
- **A cold first run is slow.** ~260 MB of Overture footers for the row-group manifest (once, then
  cached) plus DEM tiles. Warm, a full-distance course is two to four minutes.

---

## 12. Where everything is

```
pipelines/course-ingest/
├─ README.md                          how to run it
├─ Makefile                           make test / regenerate / validate / golden
├─ pyproject.toml
├─ course_ingest/
│  ├─ cli.py                          generate | validate | regenerate-all | visual-check
│  ├─ pipeline.py                     the ten stages, orchestrated
│  ├─ spec.py                         seed spec model
│  ├─ bundle.py                       bundle assembly, attribution
│  ├─ codec.py                        packed bundle container, EWKT
│  ├─ validate.py                     every rejection rule
│  ├─ graph.py                        routing graph and loop router
│  ├─ geo.py                          geodesy, resampling, elevation gain
│  ├─ render.py                       visual-check images
│  ├─ terrain_extract.py              clipped PMTiles
│  ├─ config/                         sources | routing | course | furniture .yaml
│  ├─ sources/                        RoadSource / DemSource + Overture, Terrarium, fixtures
│  └─ stages/                         s01_ingest … s10_emit
├─ specs/                             nine seed specs, three `ready`, six `pending`
├─ tools/margin_check.py              cut-off margin spot-check
├─ tests/                             determinism, validation, e2e, codec, golden isolation
└─ docs/
   ├─ SCHEMA_CHANGES.md               replacement text for the backend build  <- read first
   ├─ CUTOFF_LADDER.md                cut-off rules and the difficulty spread
   ├─ DATA_SOURCES.md                 what was chosen and rejected, with evidence
   ├─ GOLDEN_FIXTURE_DISCREPANCY.md   the SOLVER_MODEL.md B.1 finding
   └─ SESSION_B_HANDOFF.md            this file

backend/tests/golden/
├─ build_golden_courses.py            synthetic fixture generator (imports nothing from the pipeline)
└─ courses/                           C-TRAM, C-FLAT, C-ALTA, C-HALF, C-OLY, C-SPR
```
