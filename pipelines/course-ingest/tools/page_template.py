"""HTML for the three-course visual check.

Kept apart from `build_review_page.py` so the data collection and the markup can
be read separately.
"""
from __future__ import annotations

CSS = """
<style>
:root{
  /* A cartographic workspace palette. The three leg colours are the ones the
     rendered map and profile images already use, so page and image read as one
     system; the accent is a bathymetric teal that sits beside the swim blue
     rather than competing with it. */
  --ground:#e9ecef; --surface:#ffffff; --surface-2:#f3f5f7; --surface-3:#eaeef1;
  --ink:#131b22; --muted:#5a6874; --faint:#8695a1;
  --line:#d3d9de; --line-strong:#b8c2c9;
  --accent:#0d5c63; --accent-soft:#e2eeef;
  --pass:#2c7a4b; --pass-bg:#e4f1e9;
  --tight:#a86412; --tight-bg:#f7ecdb;
  --fail:#9e2b2b; --fail-bg:#f7e3e3;
  --swim:#2b8cbe; --bike:#d95f0e; --run:#31a354;
  --shadow:0 1px 2px rgba(19,27,34,.06), 0 8px 24px rgba(19,27,34,.05);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --ground:#101519; --surface:#171e24; --surface-2:#1e262d; --surface-3:#232c34;
    --ink:#e2e8ed; --muted:#8fa0ad; --faint:#6f8090;
    --line:#2a343c; --line-strong:#3a4750;
    --accent:#5fb3ba; --accent-soft:#16323a;
    --pass:#5fbd84; --pass-bg:#16301f;
    --tight:#dfa04a; --tight-bg:#33240f;
    --fail:#e2706e; --fail-bg:#37191b;
    --swim:#5aaede; --bike:#f08034; --run:#57c07a;
    --shadow:0 1px 2px rgba(0,0,0,.35), 0 8px 24px rgba(0,0,0,.30);
  }
}
:root[data-theme="dark"]{
  --ground:#101519; --surface:#171e24; --surface-2:#1e262d; --surface-3:#232c34;
  --ink:#e2e8ed; --muted:#8fa0ad; --faint:#6f8090;
  --line:#2a343c; --line-strong:#3a4750;
  --accent:#5fb3ba; --accent-soft:#16323a;
  --pass:#5fbd84; --pass-bg:#16301f;
  --tight:#dfa04a; --tight-bg:#33240f;
  --fail:#e2706e; --fail-bg:#37191b;
  --swim:#5aaede; --bike:#f08034; --run:#57c07a;
  --shadow:0 1px 2px rgba(0,0,0,.35), 0 8px 24px rgba(0,0,0,.30);
}

*{box-sizing:border-box}
body{
  margin:0; background:var(--ground); color:var(--ink);
  font-family:"Source Serif 4",Georgia,serif; font-size:16px; line-height:1.6;
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1180px; margin:0 auto; padding:40px 28px 96px}
h1,h2,h3,h4,.disp{font-family:Archivo,"Helvetica Neue",Arial,sans-serif; text-wrap:balance}
.mono,td.num,.chip,.eyebrow,.kv dt{font-family:"IBM Plex Mono",ui-monospace,Menlo,monospace}
.num{font-variant-numeric:tabular-nums}

.eyebrow{
  font-size:11px; letter-spacing:.14em; text-transform:uppercase;
  color:var(--accent); font-weight:500; margin:0 0 10px;
}
header.masthead{border-bottom:2px solid var(--accent); padding-bottom:26px; margin-bottom:34px}
header.masthead h1{font-size:clamp(30px,4.4vw,46px); line-height:1.05; margin:0 0 12px; font-weight:700; letter-spacing:-.02em}
header.masthead p.lede{margin:0; max-width:66ch; color:var(--muted); font-size:17px}
.meta-row{display:flex; flex-wrap:wrap; gap:8px 22px; margin-top:18px; font-size:12.5px; color:var(--faint)}
.meta-row b{color:var(--muted); font-weight:500}

/* ---- summary strip ------------------------------------------------------ */
.summary{display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:14px; margin-bottom:44px}
.sumcard{
  background:var(--surface); border:1px solid var(--line); border-radius:3px;
  padding:16px 18px; box-shadow:var(--shadow); border-top:3px solid var(--accent);
}
.sumcard h3{margin:0 0 2px; font-size:17px; font-weight:600}
.sumcard .where{font-size:12.5px; color:var(--muted); margin:0 0 12px}
.sumcard .figs{display:flex; gap:16px; font-size:12px; color:var(--muted)}
.sumcard .figs b{display:block; font-size:15px; color:var(--ink); font-weight:500}

.chip{
  display:inline-flex; align-items:center; gap:6px; font-size:11px; font-weight:500;
  letter-spacing:.06em; padding:3px 9px; border-radius:2px; text-transform:uppercase;
}
.chip.pass{background:var(--pass-bg); color:var(--pass)}
.chip.tight{background:var(--tight-bg); color:var(--tight)}
.chip.fail{background:var(--fail-bg); color:var(--fail)}
.chip.plain{background:var(--surface-3); color:var(--muted)}

/* ---- course dossier ----------------------------------------------------- */
.course{margin-bottom:56px; border-top:1px solid var(--line-strong); padding-top:28px}
.course-head{display:flex; flex-wrap:wrap; align-items:baseline; gap:10px 16px; margin-bottom:6px}
.course-head h2{margin:0; font-size:27px; font-weight:700; letter-spacing:-.015em}
.course-head .place{color:var(--muted); font-size:14px}
.course-sub{margin:0 0 22px; color:var(--muted); font-size:14px; max-width:70ch}
.grid{display:grid; grid-template-columns:minmax(0,1.15fr) minmax(0,1fr); gap:26px; align-items:start}
@media (max-width:960px){.grid{grid-template-columns:1fr}}
figure{margin:0; background:var(--surface); border:1px solid var(--line); border-radius:3px; padding:10px; box-shadow:var(--shadow)}
figure img{display:block; width:100%; height:auto; border-radius:2px}
figcaption{font-size:11.5px; color:var(--faint); padding:8px 4px 2px; font-family:"IBM Plex Mono",monospace}

.rail{display:flex; flex-direction:column; gap:20px}
.block{background:var(--surface); border:1px solid var(--line); border-radius:3px; padding:16px 18px; box-shadow:var(--shadow)}
.block > h4{
  margin:0 0 12px; font-size:11px; letter-spacing:.13em; text-transform:uppercase;
  color:var(--accent); font-weight:600; font-family:"IBM Plex Mono",monospace;
}
table{width:100%; border-collapse:collapse; font-size:13px}
th{
  text-align:left; font-family:"IBM Plex Mono",monospace; font-size:10.5px;
  letter-spacing:.08em; text-transform:uppercase; color:var(--faint);
  font-weight:500; padding:0 8px 7px 0; border-bottom:1px solid var(--line);
}
th.r,td.r{text-align:right; padding-right:0}
td{padding:7px 8px 7px 0; border-bottom:1px solid var(--line)}
tr:last-child td{border-bottom:none}
td.num{font-variant-numeric:tabular-nums; font-size:12.5px}
.legdot{display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:7px; vertical-align:1px}
.ok{color:var(--pass)} .warn{color:var(--tight)} .bad{color:var(--fail)}

/* ---- margin bars -------------------------------------------------------- */
.profile{margin-bottom:18px}
.profile:last-child{margin-bottom:0}
.profile-head{display:flex; align-items:baseline; justify-content:space-between; gap:10px; margin-bottom:10px}
.profile-head .who{font-weight:600; font-size:13.5px; font-family:Archivo,sans-serif}
.profile-head .split{font-family:"IBM Plex Mono",monospace; font-size:11.5px; color:var(--muted); font-variant-numeric:tabular-nums}
.bar-row{display:grid; grid-template-columns:96px 1fr 74px; gap:10px; align-items:center; margin-bottom:6px}
.bar-row .label{font-family:"IBM Plex Mono",monospace; font-size:11px; color:var(--muted); overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.track{position:relative; height:14px; background:var(--surface-3); border-radius:2px; overflow:hidden}
.fill{position:absolute; inset:0 auto 0 0; border-radius:2px}
.fill.ok{background:var(--pass)} .fill.warn{background:var(--tight)} .fill.bad{background:var(--fail)}
.bar-row .val{font-family:"IBM Plex Mono",monospace; font-size:11.5px; text-align:right; font-variant-numeric:tabular-nums}
.legend{font-size:11px; color:var(--faint); margin-top:10px; font-family:"IBM Plex Mono",monospace}

/* ---- prose blocks ------------------------------------------------------- */
.note{background:var(--accent-soft); border-left:2px solid var(--accent); padding:14px 18px; border-radius:0 3px 3px 0; font-size:14.5px}
.note p{margin:0 0 10px} .note p:last-child{margin:0}
section.tail{margin-top:56px; border-top:2px solid var(--accent); padding-top:28px}
section.tail h2{font-size:22px; margin:0 0 6px; font-weight:700}
.cols{display:grid; grid-template-columns:repeat(auto-fit,minmax(280px,1fr)); gap:26px; margin-top:20px}
.kv{margin:0} .kv dt{font-size:10.5px; letter-spacing:.08em; text-transform:uppercase; color:var(--faint); margin-top:12px}
.kv dt:first-child{margin-top:0}
.kv dd{margin:2px 0 0; font-size:13.5px}
code{font-family:"IBM Plex Mono",monospace; font-size:12.5px; background:var(--surface-3); padding:1px 5px; border-radius:2px}
.pending{font-size:13.5px; color:var(--muted)}
.pending li{margin-bottom:3px}
footer{margin-top:44px; padding-top:18px; border-top:1px solid var(--line); font-size:11.5px; color:var(--faint); font-family:"IBM Plex Mono",monospace}
</style>
"""

FONTS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
    "family=Archivo:wght@500;600;700&"
    "family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&"
    'family=IBM+Plex+Mono:wght@400;500&display=swap">'
)


def _verdict_chip(verdict: str, esc) -> str:
    cls = {"CLEAR": "pass", "TIGHT": "tight", "INFEASIBLE": "fail"}[verdict]
    return f'<span class="chip {cls}">{esc(verdict)}</span>'


def _legs_table(course, esc, leg_color) -> str:
    rows = []
    for leg in course["legs"]:
        dev = leg["deviation_pct"]
        dev_cls = "ok" if abs(dev) <= 0.5 else "bad"
        rows.append(
            f'<tr><td><span class="legdot" style="background:{leg_color[leg["leg"]]}"></span>'
            f'{esc(leg["leg"].title())}</td>'
            f'<td class="num r">{leg["distance_m"]/1000:.3f}</td>'
            f'<td class="num r {dev_cls}">{dev:+.2f}%</td>'
            f'<td class="num r">{leg["gain_m"]:,.0f}</td>'
            f'<td class="num r">{leg["gain_per_km"]:.1f}</td>'
            f'<td class="num r">{leg["nodes"]:,}</td></tr>'
        )
    return (
        '<table><thead><tr><th>Leg</th><th class="r">km</th><th class="r">vs nominal</th>'
        '<th class="r">gain m</th><th class="r">m/km</th><th class="r">nodes</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _character_block(course, esc) -> str:
    rows = []
    for leg in ("BIKE", "RUN"):
        band = course["character"][leg]
        data = next(l for l in course["legs"] if l["leg"] == leg)
        lo, hi = float(band["min_gain_per_km"]), float(band["max_gain_per_km"])
        got = data["gain_per_km"]
        ok = lo <= got <= hi
        rows.append(
            f'<tr><td>{esc(leg.title())}</td>'
            f'<td><code>{esc(band["character"])}</code></td>'
            f'<td class="num r">{lo:.1f}–{hi:.1f}</td>'
            f'<td class="num r"><b>{got:.1f}</b></td>'
            f'<td class="r">{"<span class=ok>within</span>" if ok else "<span class=bad>outside</span>"}</td></tr>'
        )
    notes = "".join(
        f"<div>{esc(v)}</div>" for k, v in sorted(course["gradient_notes"].items()) if k != "SWIM"
    )
    return (
        '<table><thead><tr><th>Leg</th><th>Declared character</th>'
        '<th class="r">requires m/km</th><th class="r">delivered</th><th class="r"></th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
        f'<div class="legend">{notes}</div>'
    )


def _cutoff_block(course, esc, hhmm) -> str:
    ratios = course["bundle"]["provenance_detail"]["cutoff_ratios"]
    rows = []
    for b in course["bundle"]["course_bundle"]["barriers"]:
        rows.append(
            f'<tr><td><code>{esc(b["name"])}</code></td>'
            f'<td class="num r">{b["km"]:.1f}</td>'
            f'<td class="num r">{b["limit_minutes_from_start"]:.0f}</td>'
            f'<td class="num r">{hhmm(b["limit_minutes_from_start"])}</td></tr>'
        )
    return (
        f'<div class="legend" style="margin:0 0 10px">generosity dial '
        f'<b style="color:var(--ink)">{ratios["generosity"]:.2f}</b> × the reference ladder '
        f'({ratios["reference_swim_exit_min"]:.0f} / {ratios["reference_bike_cutoff_min"]:.0f} / '
        f'{ratios["reference_finish_min"]:.0f} min)</div>'
        '<table><thead><tr><th>Barrier</th><th class="r">km</th>'
        '<th class="r">min</th><th class="r">h:mm</th></tr></thead>'
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _margin_block(course, esc, hhmm) -> str:
    out = []
    for key in ("strong", "mid-pack", "first-timer"):
        m = course["margins"][key]
        s = m["splits"]
        bars = []
        for b in m["barriers"]:
            load = b["load_pct"]
            cls = "bad" if load > 100 else ("warn" if load > 93 else "ok")
            width = min(100.0, load)
            margin = b["margin_minutes"]
            bars.append(
                f'<div class="bar-row"><div class="label" title="{esc(b["name"])}">{esc(b["name"])}</div>'
                f'<div class="track"><div class="fill {cls}" style="width:{width:.1f}%"></div></div>'
                f'<div class="val {cls}">{margin:+.0f} min</div></div>'
            )
        out.append(
            f'<div class="profile"><div class="profile-head">'
            f'<span class="who">{esc(m["label"])}</span>'
            f'{_verdict_chip(m["verdict"], esc)}'
            f'<span class="split">{hhmm(s["total"])} total · '
            f'swim {s["swim"]:.0f} / bike {s["bike"]:.0f} / run {s["run"]:.0f} min</span>'
            f"</div>{''.join(bars)}</div>"
        )
    return (
        "".join(out)
        + '<div class="legend">Bar is elapsed time against the barrier’s limit; '
        "the number is margin in hand. Cool conditions, no wind — not a solve.</div>"
    )


def build_html(cfg, courses, esc, hhmm, leg_color) -> str:
    first = courses[0]["bundle"]
    attribution = first["course_bundle"]["attribution"]
    road_src = first["provenance_detail"]["road_source"]
    dem_zoom = first["provenance_detail"]["dem_sample_zoom"]

    summary = []
    for c in courses:
        b = c["bundle"]
        bike = next(l for l in c["legs"] if l["leg"] == "BIKE")
        run = next(l for l in c["legs"] if l["leg"] == "RUN")
        worst = min(
            (c["margins"][k]["worst"]["margin_minutes"] for k in c["margins"]),
        )
        summary.append(
            f'<div class="sumcard" style="border-top-color:{leg_color["BIKE"] if bike["gain_per_km"]>10 else leg_color["SWIM"]}">'
            f'<h3>{esc(b["course"]["name"])}</h3>'
            f'<p class="where">{esc(b["course"]["place"])} · {esc(b["course"]["distance_type"])} · '
            f'{esc(b["course"]["difficulty"].title())}</p>'
            f'<div class="figs">'
            f'<div>bike<b class="num">{bike["distance_m"]/1000:.1f} km</b></div>'
            f'<div>climb<b class="num">{bike["gain_m"]:,.0f} m</b></div>'
            f'<div>run<b class="num">{run["distance_m"]/1000:.1f} km</b></div>'
            f'<div>tightest margin<b class="num">{worst:+.0f} min</b></div>'
            f"</div></div>"
        )

    dossiers = []
    for c in courses:
        b = c["bundle"]
        cb = b["course_bundle"]
        rt = c["bundle"]["provenance_detail"]["routing"]
        swim = c["bundle"]["provenance_detail"]["swim"]
        aid = len(cb["aid_stations"])
        wps = len(cb["waypoints"])
        segs_bike = len([s for s in cb["segments"] if s["leg"] == "BIKE"])
        segs_run = len([s for s in cb["segments"] if s["leg"] == "RUN"])
        named = len([s for s in cb["segments"] if s["name_source"] == "OSM_WAY"])
        dossiers.append(f"""
<article class="course">
  <div class="course-head">
    <h2>{esc(b['course']['name'])}</h2>
    <span class="place">{esc(b['course']['place'])}</span>
    <span class="chip pass">validation pass</span>
    <span class="chip plain">{esc(cb['provenance'])}</span>
    <span class="chip plain">{esc(cb['version'])}</span>
  </div>
  <p class="course-sub">
    {esc(b['course']['distance_type'])} · {esc(b['course']['difficulty'].title())} ·
    bike routed as <code>{esc(c['character']['BIKE']['character'])}</code>,
    run as <code>{esc(c['character']['RUN']['character'])}</code> ·
    {segs_bike + segs_run} named segments ({named} from OpenStreetMap ways) ·
    {aid} aid stations, {wps} waypoints ·
    swim drawn as a {swim['laps']}-lap {esc(swim['shape'].split('_')[0])} in {esc(swim['water_body'] or 'open water')}
  </p>
  <div class="grid">
    <figure>
      <img src="data:image/png;base64,{c['image_b64']}" alt="Map and elevation profile for {esc(b['course']['name'])}">
      <figcaption>bike {rt['BIKE']['ways_used']} ways · run {rt['RUN']['ways_used']} ways ·
        packed {c['packed_bytes']/1024:.0f} KB · terrain extract {c['terrain_bytes']/1e6:.1f} MB</figcaption>
    </figure>
    <div class="rail">
      <div class="block"><h4>Legs delivered</h4>{_legs_table(c, esc, leg_color)}</div>
      <div class="block"><h4>Terrain character</h4>{_character_block(c, esc)}</div>
      <div class="block"><h4>Cut-off ladder</h4>{_cutoff_block(c, esc, hhmm)}</div>
      <div class="block"><h4>Margin spot-check</h4>{_margin_block(c, esc, hhmm)}</div>
    </div>
  </div>
</article>""")

    return f"""<title>Three-Course Review</title>
{FONTS}
{CSS}
<div class="wrap">
<header class="masthead">
  <p class="eyebrow">RaceOS · course-ingest · seed bundle review</p>
  <h1>Three courses, generated and validated</h1>
  <p class="lede">Fictional race names on real terrain. Every metre of the bike and run legs follows
  an OpenStreetMap way; every height is a terrain sample. The swim is the only drawn geometry, and it
  is drawn inside a real water body. Nothing here is stamped official.</p>
  <div class="meta-row">
    <span><b>roads</b> {esc(road_src)}</span>
    <span><b>elevation</b> Terrarium z{dem_zoom}, bilinear</span>
    <span><b>node spacing</b> 10 m</span>
    <span><b>distance tolerance</b> ±0.5%</span>
    <span><b>provenance</b> ESTIMATED throughout</span>
  </div>
</header>

<div class="summary">{''.join(summary)}</div>

<div class="note" style="margin-bottom:44px">
  <p><b>What to look for.</b> Do these read as races someone would enter, rather than routes that
  wander? The three things worth a second look are the shape of the bike loop, whether the elevation
  profile matches the course’s claimed character, and whether the margin bars put the cut-off
  genuinely in play on Skagen and out of play on Kalmar.</p>
</div>

{''.join(dossiers)}

<section class="tail">
  <p class="eyebrow">Context</p>
  <h2>How these were built, and what is still to come</h2>
  <div class="cols">
    <div>
      <dl class="kv">
        <dt>Roads</dt>
        <dd>Overture Maps <code>transportation/segment</code>, OSM-derived, ODbL-1.0, release pinned
        at <code>{esc(road_src.split(':', 1)[1])}</code> and carried in every bundle’s provenance.
        Chosen over a hosted routing API because a hosted router cannot pin a snapshot, and without a
        pinned snapshot byte-identical regeneration is impossible.</dd>
        <dt>Elevation</dt>
        <dd>Terrarium-encoded AWS Terrain Tiles, sampled at z{dem_zoom} with bilinear interpolation.
        Substituted for the Mapterhorn tileset named in the build spec, which is unreachable from the
        build environment; identical encoding, one config line to swap back.</dd>
        <dt>Attribution</dt>
        <dd>{esc(attribution)}<br>
        Generated from the licences the ways actually carry. ODbL obliges it wherever the derived
        data is displayed — map views, the static fallback, the elevation profile, the course page,
        the race-card PDF, and FIT/GPX exports.</dd>
      </dl>
    </div>
    <div>
      <dl class="kv">
        <dt>Two elevation-gain numbers</dt>
        <dd>The gain shown above is hysteresis-filtered at 3 m — a rise counts once it clears that
        much above the last reversal. A DEM sampled every 10 m has a noise floor that an unfiltered
        sum credits as climbing; Skagen’s flat dune coast measures 3.7 m/km unfiltered against
        2.0 m/km filtered. The raw node-series total is carried alongside, and it is what the solver
        recomputes. Nothing is smoothed.</dd>
        <dt>Determinism</dt>
        <dd>The same seed spec produces byte-identical output on every run, proven by regenerating
        all three courses twice and diffing. That is what makes a season-over-season bundle diff
        meaningful: if the output moved, an input moved.</dd>
        <dt>Still to come</dt>
        <dd class="pending">Six courses have finished seed specs marked <code>status: pending</code>
        and are skipped by <code>regenerate-all</code> — North Shore Full, Cala Olympic, Roth Long
        Course, Patagonia Full, Serra Classic, Bergen Sprint. Coordinates, character, lap structure
        and cut-off dial are all settled; flip the status and generate.</dd>
      </dl>
    </div>
  </div>
</section>

<footer>
  Generated by pipelines/course-ingest · {esc(attribution)} ·
  race names are fictional, terrain is real
</footer>
</div>
"""
