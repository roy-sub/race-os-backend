# Cut-off ladder — the rules, and the spread across the nine

Every barrier, aid station, special-needs point and distance marker on the nine seeded courses is
**invented**, because the races are. All of it therefore carries `provenance: ESTIMATED`, and all of
it is generated from `config/furniture.yaml` rather than placed by hand, so the numbers are
defensible and a course's difficulty can be changed by moving one value.

## The reference ladder

One row per distance type, in minutes from the athlete's start:

| Distance | `swim_exit` | intermediate bike checkpoint | `bike_cutoff` | `finish` |
|---|---|---|---|---|
| Full | 140 | km 120 @ 510 | 630 | 960 |
| 70.3 | 70 | km 60 @ 266 | 330 | 510 |
| Olympic | 50 | — | 170 | 240 |
| Sprint | 30 | — | 95 | 130 |

The Full row is the reference structure given in the brief. The other three reproduce
`SOLVER_MODEL.md` §B.1's `C-HALF`, `C-OLY` and `C-SPR` exactly, so the seeded courses and the golden
fixtures agree on the *shape* of a cut-off while remaining entirely separate artefacts.

**The intermediate bike checkpoint** sits at 66.67% of bike distance but 75.51% of the
swim-exit-to-bike-cutoff time window. The two fractions differ deliberately: an athlete slows through
a long bike leg, so a checkpoint two thirds of the way along should fall three quarters of the way
through the available time. At full distance that reproduces km 120 at 510 minutes. Olympic and
Sprint have no intermediate checkpoint — the leg is too short for one to mean anything.

## The per-course dial

Each spec carries one number, `cutoff_generosity`, which multiplies every limit on that course.
`1.00` is the reference ladder. Bounds are `[0.80, 1.20]`, enforced at build time.

This is the whole mechanism. There is no per-course barrier table to hand-edit, so a course cannot
acquire a bike cut-off that falls before its own swim exit.

## The spread, and why it is not uniform

The cut-off feature is one of the product's two course-data differentiators, so it has to be
*visible* in seed data. A set of nine courses that were all comfortable would leave it untestable.

| Course | Distance | Terrain | `generosity` | swim exit | bike cut-off | finish | Intent |
|---|---|---|---|---|---|---|---|
| Cala Olympic | Olympic | approachable | **1.10** | 55 | 187 | 264 | The gentlest in the set. A first-timer should never see a cut-off warning here. |
| Kalmar 70.3 | 70.3 | flat coastal | **1.05** | 74 | 347 | 536 | Fast and forgiving — the reference "clear" 70.3. |
| Bergen Sprint | Sprint | fjord town | **1.05** | 32 | 100 | 137 | Short and comfortable. |
| Roth Long Course | Full | rural rolling | **1.03** | 144 | 649 | 989 | Classic long course, generous ladder. |
| North Shore Full | Full | harbour volcanic | **1.02** | 143 | 643 | 979 | Rolling, slightly generous. |
| **Tramuntana Full** | Full | mountainous | **1.00** | 140 | 630 | 960 | Reference ladder on hard terrain. Tight for a mid-packer *because of the climbing*, not because the limits were moved. |
| **Serra Classic** | Full | rugged mountain | **0.97** | 136 | 611 | 931 | Rugged terrain plus a slightly tightened ladder. |
| **Patagonia Full** | Full | lake mountain | **0.95** | 133 | 599 | 912 | Brutal terrain on a tightened ladder. The hardest course in the set. |
| **Skagen 70.3** | 70.3 | flat exposed | **0.84** | 59 | 277 | 428 | **The tight 70.3.** |

### Why Skagen is tightened rather than another mountain course

RaceOS's primary market is Ironman **and** Ironman 70.3 (`SOLVER_MODEL.md` §0.1b). If the only
genuinely tight courses were full distance, an athlete planning a 70.3 would never see the cut-off
feature work, and half the primary market would be looking at seed data that never exercises it.

Skagen is flat and fast, so tightness cannot come from the terrain — it has to come from the ladder.
`0.84` turns a comfortable day into a real one. Measured with `tools/margin_check.py`, the tightest
barrier for each profile:

| Profile | Skagen margin | Peak load | Verdict | Kalmar, for contrast |
|---|---|---|---|---|
| Strong age-grouper | +25 min | 75% | CLEAR | +40 min, CLEAR |
| **Mid-pack** | **+19 min** | **82%** | **TIGHT** | +34 min, CLEAR |
| First-timer | +11 min | 93% | TIGHT | +26 min, CLEAR |

The dial was set by measurement rather than by feel. `0.88` put a first-timer inside the 20-minute
clear/tight band but left mid-pack at +22 min, just outside it; `0.84` brings the athlete the feature
is really about inside the band without making the course unfinishable for anyone.

Note which barrier binds: it is `swim_exit` on every profile, not the bike cut-off. On a flat, fast
70.3 the bike and run are never the problem — a slow swimmer getting pulled at the water exit is. That
is a realistic failure mode and a useful one to have in seed data, but it is worth knowing that
Skagen exercises the swim barrier rather than the bike one.

The three tight courses therefore span both primary distances and both mechanisms: **Tramuntana**
tight from terrain alone at reference limits, **Patagonia and Serra** tight from terrain *and* a
tightened ladder, **Skagen** tight from the ladder alone on flat ground.

## Aid stations

| Distance | Bike | Run |
|---|---|---|
| Full | every 25 km from km 20 (7 stations) | every 2.1 km (20 stations) |
| 70.3 | every 22.5 km from km 20 | every 2.1 km |
| Olympic | every 20 km from km 20 | every 2.5 km |
| Sprint | every 10 km from km 10 | every 2.5 km |

Spacing is a real constraint the solver reasons over, so it has to be plausible: bike stations far
enough apart that an athlete must carry between them, run stations close enough that a walk-through
is always within reach. The full-distance rows match the reference structure in `SOLVER_MODEL.md`
§B.1 exactly.

Every second bike station and every fourth run station carries the full-service contents list
(gels, salt, toilets, mechanical or medical support) rather than the standard one. A station within
`min_tail_km` of the finish is dropped — a station 300 m from the line is not a station.

## Special needs

At the leg midpoint, then snapped to the nearest aid station within 4 km so the bag sits somewhere an
athlete actually stops. Bike and run at full distance, bike only at 70.3, none at Olympic or Sprint.

## What is *not* generated

Nothing here is stamped `OFFICIAL`. When a real licensed course replaces a generated one, its
published aid stations and cut-offs replace these, and their provenance changes with them. A real
course carrying invented aid stations still marked `ESTIMATED` is honest; the same course with them
marked `OFFICIAL` is not.
