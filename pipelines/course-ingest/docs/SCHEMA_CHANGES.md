# Course bundle schema changes — replacement text for `RaceOS_Build_Spec.md`

**Audience:** the backend build session (milestone 3, "Courses + bundles, read-only").
**Status:** decided and implemented. `pipelines/course-ingest` already emits every field below, and
the three generated bundles in `out/bundles/` carry them. Six further courses have finished seed
specs marked `status: pending` and will produce the same shape.

This document is **exact replacement text**, not a description. Paste the marked blocks over the
corresponding blocks in `RaceOS_Build_Spec.md`, and apply the migration at the end.

---

## 1. Why these five columns exist

`SOLVER_MODEL.md` was produced after `RaceOS_Build_Spec.md` Part 4.3 was written, and it consumes
two things the `course_bundles` schema had no column for. Both were raised and approved before the
pipeline was built.

| Addition | Table | Why it cannot live somewhere else |
|---|---|---|
| `segments jsonb` | `course_bundles` | Named segments are the solver's **primary unit of work** (`SOLVER_MODEL.md` §1.1, §4.2.1): one power target per segment, time integrated over the node series inside it. `plan_segments` is solver *output*, keyed to `plan_id`, so it cannot be the input. Segments must also be diffable by the Part 6.1 blast-radius preview. |
| `surface_quality` | `course_bundle_legs` | `SOLVER_MODEL.md` §I.2.2 takes `Crr` "from the course bundle's surface descriptor rather than the athlete". The gap between `typical_road` (0.0050) and `rough_chipseal` (0.0065) is **≈8 minutes over 180 km** — comparable to the whole `clear`/`tight` margin band, so a surface change must show up in the blast-radius diff rather than passing silently. Per-segment values also live in `segments[].surface_quality`; the leg column is the queryable modal value. |
| `elevation_source text` | `course_bundles` | `SOLVER_MODEL.md` §1.2 raises `BundleIncomplete` for anything but `terrain`. Making it a column makes the invariant queryable rather than buried in a blob. |
| `attribution text` | `course_bundles` | ODbL obliges attribution wherever derived data is displayed (Part 9, Part 21.1). It is a licence-compliance artefact, so it must be auditable with a query. |
| `waypoints jsonb` | `course_bundles` | Transitions, special-needs points and distance markers. Deliberately **not** inside `aid_stations`: "one action per aid station" (`SOLVER_MODEL.md` §5.5) is a correctness property, and it should hold by construction rather than depend on every future reader remembering to filter on a type discriminator. |

---

## 2. Replacement text for Part 4.3 — `course_bundles` paragraph

> Replace the `**course_bundles**` paragraph and the two jsonb-shape lines that follow it.

```markdown
**`course_bundles`** — `course_id`, `version text` (e.g. `v2026.2`), `status bundle_status`, `provenance`, `verified_at date`, `published_at`, `route_geometry geometry(LineStringZ, 4326)` per leg (see `course_bundle_legs`), `elevation_profile jsonb`, `barriers jsonb`, `aid_stations jsonb`, `waypoints jsonb`, `segments jsonb`, `elevation_source text not null default 'terrain'`, `attribution text not null`, `changelog text`, `plans_affected_count int`, `season_year int`, `bundle_asset_key text` (packed binary bundle in object storage), `terrain_pmtiles_key text`. Unique on `(course_id, version)`.

**`course_bundle_legs`** — `bundle_id`, `leg`, `geometry geometry(LineStringZ,4326)`, `distance_m`, `elevation_gain_m`, `node_count`, `surface_quality surface_quality not null`. Separate table so PostGIS indexes work per leg.

**`barriers` jsonb shape:** `{name, leg, limit_minutes_from_start, km}`.
**`aid_stations` jsonb shape:** `{leg, name, km, contents[], provenance}` — aid stations only. Transitions, special needs and distance markers are in `waypoints`; the solver's "one action per aid station" invariant depends on this array containing nothing else.
**`waypoints` jsonb shape:** `{type, leg, name, km, provenance}` where `type ∈ transition | special_needs | distance_marker`. `km` is always kilometres; unit conversion is a frontend concern.
**`surface_quality` on the swim leg** is a placeholder. The column is `NOT NULL` because it is
meaningful on every leg the solver costs, and the solver reads it only for the bike
(`SOLVER_MODEL.md` §I.2.2). The pipeline writes `typical_road` on the swim row; do not read it
there.
**`segments` jsonb shape:** `{ordinal, leg, name, from_km, to_km, net_gradient, elevation_gain_m, surface_quality, name_source}`. `ordinal` is unique and ascending across the whole bundle in leg order `SWIM, BIKE, RUN`. `net_gradient` is a fraction, not a percentage. `name_source ∈ OSM_WAY | DERIVED_TERRAIN` — whether the segment took its name from an OpenStreetMap way or from its own terrain band.
**`elevation_source`** — `terrain` for every bundle this system produces. `SOLVER_MODEL.md` §1.2 raises `BundleIncomplete` for any other value; elevation is never GPS or barometric.
**`attribution`** — the ODbL attribution string, assembled from the licences the underlying ways actually carry. Must be displayed wherever the derived data is (see §4 below).
```

---

## 3. Replacement text for Part 4.1 — one new enum

> Add to the `4.1 Enum types` SQL block, after `provenance`.

```sql
surface_quality     : smooth_asphalt | typical_road | rough_chipseal
```

---

## 4. Replacement text for Part 10.2 — the pipeline step list

> Replace the numbered list under `## 10.2 Ingestion pipeline (pipelines/course-ingest)`.

```markdown
Offline Python job, run per course per season, producing a versioned bundle. Ten stages, one module each.

1. **Ingest** the seed specification (`specs/*.yaml`) or raw GPX/KML/published geometry per leg.
2. **Route** the bike and run legs along real OpenStreetMap ways to the required distance, honouring the course's declared terrain character.
3. **Draw** the swim leg as a buoy course inside a real water body. This is the only drawn geometry in a bundle; there are no roads in water.
4. **Clean** — drop duplicate and outlier points, close loops, split into swim/bike/run.
5. **Map-match** bike and run legs to OpenStreetMap road geometry to remove GPS noise.
6. **Resample** to ~10 m nodes so gradient is stable and comparable across courses.
7. **Sample elevation** from the terrain DEM — **never trust GPX elevation**, which comes from drifting barometers. A missing DEM tile fails the build; it is never interpolated across.
8. **Compute** per-node gradient, per-segment climb, and named segments (a contiguous run of similar gradient above a length threshold, named from OpenStreetMap where available), plus each segment's `surface_quality`.
9. **Attach** aid stations, transitions, special-needs points, distance markers, and per-leg cut-offs, each with its own provenance.
10. **Validate and emit** — a compact binary course bundle (< 400 KB) plus a clipped terrain PMTiles extract for the course bounding box, then publish as a draft bundle for admin review.

Validation rejects rather than publishes: all three legs present and within tolerance of nominal distance, barrier ordering chronologically sane, aid-station and waypoint km within leg distance, elevation series length matching node count exactly, no implausible gradients, delivered elevation gain matching the declared character, segments tiling each leg without gap, `elevation_source = 'terrain'`, attribution present, and the size budget met.

**Determinism is a contract.** The same seed specification produces byte-identical output on every run. This is what makes a season-over-season bundle diff meaningful: if the output moved, an input moved. It is enforced by pinning the road-data snapshot (carried in the bundle's `provenance_detail.road_source`) and proven by a test that runs the pipeline twice and compares bytes.
```

---

## 5. Replacement text for Part 10.3 — one added bullet

> Add as the final bullet of `## 10.3 Storage & serving`.

```markdown
- **Attribution is carried, not assumed.** `course_bundles.attribution` is generated from the `sources[].dataset` and `sources[].license` of the ways each route actually used, not from a UI constant. ODbL requires it wherever the derived data is displayed, which means: the 2D and 3D map views, the no-WebGL static-map fallback, the elevation profile view, the course detail page, the race-card PDF footer, `.FIT` and GPX export metadata, and any shared-plan or share-link page that renders geometry. A regression test should assert it is present on each of those surfaces.
```

---

## 6. Migration

```sql
-- Part 4.1: new enum
CREATE TYPE surface_quality AS ENUM ('smooth_asphalt', 'typical_road', 'rough_chipseal');

-- Part 4.3: course_bundles
ALTER TABLE course_bundles
    ADD COLUMN segments         jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN waypoints        jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN elevation_source text  NOT NULL DEFAULT 'terrain',
    ADD COLUMN attribution      text  NOT NULL DEFAULT '';

-- A bundle whose elevation is not terrain-sampled must not be storable at all:
-- SOLVER_MODEL.md 1.2 would reject it at solve time, which is far too late.
ALTER TABLE course_bundles
    ADD CONSTRAINT course_bundles_elevation_source_terrain
    CHECK (elevation_source = 'terrain');

-- Attribution is a licence obligation, so an empty one is a data error.
ALTER TABLE course_bundles
    ADD CONSTRAINT course_bundles_attribution_present
    CHECK (length(attribution) > 0);

-- Part 4.3: course_bundle_legs
ALTER TABLE course_bundle_legs
    ADD COLUMN surface_quality surface_quality NOT NULL DEFAULT 'typical_road';

-- Segments are queried by leg when the blast-radius preview diffs two bundles.
CREATE INDEX course_bundles_segments_gin ON course_bundles USING gin (segments jsonb_path_ops);
```

Drop the `DEFAULT`s on `segments`, `waypoints` and `attribution` once the nine seeded bundles are
loaded; they exist only so the migration can run against a populated table.

---

## 7. Loading the seed fixtures

Each `out/bundles/<slug>.bundle.json` is one course, shaped as the rows expect:

```
{
  "schema_version": 1,
  "course":              { … one `courses` row … },
  "course_bundle":       { … one `course_bundles` row … },
  "course_bundle_legs":  [ … three `course_bundle_legs` rows … ],
  "provenance_detail":   { … build provenance, see below … }
}
```

- `course_bundle_legs[].geometry` is **EWKT**: `SRID=4326;LINESTRING Z (lon lat elev, …)`, directly
  insertable into `geometry(LineStringZ, 4326)`. Coordinates are at 6 decimal places (~0.11 m) and
  elevation at 2 (1 cm), both far finer than the DEM's real accuracy.
- `course_bundle.bundle_asset_key` and `terrain_pmtiles_key` are the object-storage keys the packed
  bundle (`<slug>.bundle.bin`) and the terrain extract (`out/terrain/<slug>.pmtiles`) should be
  uploaded to. Upload both, then insert.
- `provenance_detail` is **not a column**. It records how the bundle was built — pinned road-data
  release, DEM tileset and sample zoom, node spacing, gain threshold, swim geometry, terrain
  character bands, cut-off ratios. Keep it with the artefact for auditability; it does not need a
  home in the schema.

### Two numbers that look like they disagree, and do not

`course_bundle_legs.elevation_gain_m` and `courses.elevation_gain_m` are **hysteresis-filtered**
surveyed ascent: a rise counts once it clears 3 m above the last reversal. This is the number a UI
should show, because a DEM sampled every 10 m has a vertical noise floor that an unfiltered sum
credits as climbing — a measured pancake-flat coastal marathon spanning eleven vertical metres end
to end reports 382 m unfiltered and 260 m filtered.

`SOLVER_MODEL.md` §1.1 computes a segment's gain as the plain sum of positive node differences. It
derives that from the node series itself, so it is unaffected by the column. Both figures are in
`elevation_profile.legs[*]` as `gain_m` and `gain_m_raw_nodes`, and `segments[].elevation_gain_m`
uses the **raw** definition so it matches what the solver recomputes. Nothing is smoothed: the
delivered elevation series is exactly what the DEM returned.

---

## 8. What has NOT changed

`plan_segments`, `plan_splits`, `plan_gates`, `plan_fuelling`, the solver contract in Part 5.1, the
`barriers` and `aid_stations` jsonb shapes, and every enum other than the new `surface_quality`.
The pipeline emits `barriers` and `aid_stations` in exactly the shapes Part 4.3 already specifies.
