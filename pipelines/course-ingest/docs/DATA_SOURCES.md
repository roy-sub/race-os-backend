# Data sources — what was chosen, what was rejected, and why

## Roads: Overture Maps `transportation/segment`, pinned release

**Chosen.** OSM-derived, ODbL-1.0, read from public S3 as GeoParquet over HTTP range requests.

### Why not a hosted routing API

The brief asked for a justified choice between a hosted public routing API and a locally-run engine.
The decision turned on one requirement: **the same seed spec must produce byte-identical output
across runs**, this season and next.

| | Hosted API (OSRM / Valhalla / ORS) | Overture on S3, pinned |
|---|---|---|
| Reproducible next season | ✗ continuously re-imports OSM; no snapshot to pin | ✅ release pinned in config, carried in bundle provenance |
| Rate limits | ✗ | ✅ none |
| `road_surface` for `Crr` | ✗ not in a routing response | ✅ same row |
| OSM way names for segment naming | ✗ needs a second Overpass call | ✅ same row |
| Per-record licence for attribution | ✗ | ✅ `sources[].dataset` / `.license` |
| Reachable from this build environment | ✗ (see below) | ✅ |

A hosted router would also have meant a second data source for surface and names, and Overpass —
the usual answer for that — has the same reproducibility problem.

### What the environment settled

The build environment's egress policy blocks every hosted routing and OSM bulk host. Verified, not
assumed:

```
router.project-osrm.org:443    403 CONNECT (policy denial)
valhalla1.openstreetmap.de     403
api.openrouteservice.org       403
overpass-api.de                403
overpass.kumi.systems          403
api.openstreetmap.org          403
download.geofabrik.de          403
```

So the hosted path was closed regardless. Overture on S3 was reachable and is the better answer on
the merits, which is a fortunate coincidence rather than a compromise.

### How the bbox query stays cheap

The transportation theme is 72 GB across 128 Parquet files. Reading a course bbox costs:

1. One pass to build a **row-group bbox manifest** — ~2 MB of Parquet footer per file, 128 files,
   16 384 row groups, about 10 s. Cached; every later course reuses it.
2. Range-reading only the intersecting row groups. The Tramuntana bbox is 14 row groups, **81 MB,
   12 s**, yielding 66 492 road segments with real names.

### Routing

A Dijkstra over a graph built from each way's own `connectors`, so topology comes from the data
rather than from coordinate-rounding heuristics. Roads are treated as bidirectional: triathlon legs
are raced on closed roads, and modelling ordinary one-way restrictions would produce detours a race
would never take.

Swapping the engine is an implementation of `RoadSource` — two methods, no stage changes.

---

## Elevation: Terrarium-encoded DEM tiles

### The substitution, stated plainly

`RaceOS_Build_Spec.md` Part 2 names the **Mapterhorn** tileset (Copernicus GLO-30 plus national
LiDAR). `demo.mapterhorn.com` returns 403 at this environment's egress proxy.

**AWS Terrain Tiles** (`elevation-tiles-prod`) is used instead. The Terrarium encoding is
byte-identical in meaning — `(R × 256 + G + B / 256) − 32768` metres — so the frontend's
`raster-dem` source consumes either without change, and the swap back is one line in
`config/sources.yaml`.

### Coverage, verified before any routing

| Location | Reading | Check |
|---|---|---|
| Puig Major, Mallorca | z12 tile max **1430 m** | true summit 1436 m ✅ |
| Patagonia (Puerto Varas / Osorno) | z14 range 1007–1499 m | no gap ✅ |
| Bergen | z14 range 58–220 m | no gap ✅ |
| Skagen / Kalmar | 3.1 m / 6.3 m | plausible flat ✅ |
| Roth (Main-Donau-Kanal) | 348 m | canal ~370 m ✅ |
| Takapuna, Auckland | 5.8 m | coastal ✅ |

Patagonia was the coverage risk named in the brief. It is not one.

### Sampling

z14 (~7 m/px at mid latitudes), bilinear. Bilinear rather than nearest because a 10 m node spacing
against a ~7 m pixel would otherwise produce a staircase, and a staircase in elevation is a square
wave in gradient.

**A missing tile fails the build.** Never interpolated across.

---

## Water: Overture `base/type=water`

Used only to constrain the swim leg. Around Alcúdia the bbox returns 1 467 rings across
`ocean`, `physical` (named seas and bays), `lake`, `reservoir`, `river`, `canal` and `pond`, which is
enough to place a buoy course inside real water with a shoreline clearance margin.

---

## Licensing position

Every Overture record carries its own `sources[].dataset` and `sources[].license`. On the Mallorca
pull those are `OpenStreetMap / ODbL-1.0` and `TomTom / ODbL-1.0` — TomTom contributes to OSM under
ODbL, so the whole set is ODbL-1.0.

The bundle's `attribution` string is therefore **generated from the data**, not hardcoded:

```
© OpenStreetMap contributors, © TomTom, ODbL 1.0 · Elevation: AWS Terrain Tiles
```

ODbL obliges attribution wherever the derived data is displayed. The surfaces that must carry it are
listed in the README and in `docs/SCHEMA_CHANGES.md` §5.

No race organiser's site was scraped, no branded course data was used, and no real event name
appears in any spec or bundle. The race names are fictional; only the terrain is real.
