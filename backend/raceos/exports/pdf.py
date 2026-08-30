"""Race-card and bag-manifest PDFs, rendered by WeasyPrint.

The race card is designed to be **read through a wet plastic sleeve at hour
nine**: high contrast, large type, and no meaning encoded by colour alone —
every coloured element also carries a label or a glyph. That is a real
constraint on this service, not a design note, and the snapshot test asserts
it.

**Every PDF carries the provenance footer.** It names the course bundle
version and marks every estimated or crowd-sourced value. A regression test
asserts it exists, because its absence was a real past incident (Part 6.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from typing import Any

from weasyprint import HTML

from raceos.exports import tokens

#: Marks a value whose provenance is not `OFFICIAL`/`measured`. Rendered as a
#: glyph rather than a colour, so it survives monochrome printing.
PROVENANCE_MARK = "†"  # dagger


@dataclass(frozen=True)
class PlanRenderData:
    """Everything a print artefact needs, already resolved.

    A flat dataclass rather than ORM rows: the renderer must not be able to
    lazily load anything, because a PDF generated in a background task would
    then depend on a session that has closed.
    """

    athlete_name: str
    course_name: str
    course_place: str
    event_date: str
    start_time: str
    bundle_version: str
    bundle_provenance: str
    attribution: str
    projected_label: str
    feasibility: str
    splits: list[dict[str, Any]]
    gates: list[dict[str, Any]]
    segments: list[dict[str, Any]]
    fuelling: dict[str, Any]
    aid_actions: list[dict[str, Any]]
    bags: list[dict[str, Any]]
    constraint_refs: list[dict[str, Any]]
    assumed_fields: list[str]


def _footer(data: PlanRenderData) -> str:
    """The provenance footer. Present on every generated page.

    Names the bundle version so a printed card can be matched to the geometry
    it was solved against, and states which values were estimated — a plan
    built on an estimate is still a plan, but the athlete should know.
    """
    estimated = [
        ref["key"].replace("_", " ")
        for ref in data.constraint_refs
        if ref.get("source_label") in ("estimated", "manual")
    ]
    parts = [
        f"Course bundle {escape(data.bundle_version)} " f"({escape(data.bundle_provenance)})",
        escape(data.attribution),
    ]
    if estimated:
        parts.append(f"{PROVENANCE_MARK} estimated: {escape(', '.join(estimated))}")
    if data.assumed_fields:
        parts.append("assumed: " + escape(", ".join(f.split(".")[-1] for f in data.assumed_fields)))
    parts.append(f"Generated {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    return " · ".join(parts)


def _base_css() -> str:
    """One stylesheet, from the shared token module.

    Screen and print therefore cannot drift: both read the same values.
    """
    return f"""
    @page {{
      size: A5;
      margin: 9mm 9mm 13mm 9mm;
      @bottom-center {{
        content: element(provenance);
      }}
    }}
    * {{ box-sizing: border-box; }}
    body {{
      font-family: -apple-system, "Helvetica Neue", Helvetica, Arial, sans-serif;
      color: {tokens.INK};
      background: {tokens.SURFACE};
      font-size: 9.5pt;
      line-height: 1.35;
      margin: 0;
    }}
    h1 {{ font-size: 15pt; margin: 0 0 1mm 0; letter-spacing: -0.2pt; }}
    h2 {{
      font-size: 8pt; text-transform: uppercase; letter-spacing: 0.8pt;
      color: {tokens.MUTED}; margin: 4mm 0 1.5mm 0;
      border-bottom: 0.4pt solid {tokens.RULE}; padding-bottom: 0.8mm;
    }}
    .meta {{ color: {tokens.MUTED}; font-size: 8.5pt; margin-bottom: 1mm; }}
    .headline {{
      font-size: 26pt; font-weight: 700; letter-spacing: -0.6pt;
      color: {tokens.ACCENT}; margin: 1mm 0 0 0;
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th {{
      text-align: left; font-size: 8pt; text-transform: uppercase;
      letter-spacing: 0.5pt; color: {tokens.MUTED}; font-weight: 600;
      padding: 0.6mm 1.5mm 0.6mm 0;
    }}
    td {{
      padding: 0.9mm 1.5mm 0.9mm 0; font-size: 9.5pt;
      border-top: 0.3pt solid {tokens.RULE}; vertical-align: top;
    }}
    td.num {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
    .state {{ font-weight: 700; font-size: 9pt; }}
    .glyph {{
      font-weight: 700; display: inline-block; min-width: 4mm;
    }}
    .reason {{ color: {tokens.MUTED}; font-size: 8pt; }}
    #provenance {{
      position: running(provenance);
      font-size: 6.5pt; color: {tokens.MUTED};
      border-top: 0.3pt solid {tokens.RULE}; padding-top: 1mm;
      text-align: center;
    }}
    """


def _provenance_block(data: PlanRenderData) -> str:
    return f'<div id="provenance">{_footer(data)}</div>'


def race_card_html(data: PlanRenderData) -> str:
    """The markup, separately from the render.

    Split out so tests can assert on the document — that every gate carries
    a glyph, that the provenance footer is present — without parsing a PDF
    to get back to text that was already text.
    """
    split_rows = "".join(
        f"<tr><td><strong>{escape(str(s['leg']))}</strong></td>"
        f"<td class='num'>{s['distance']:.1f} km</td>"
        f"<td class='num'>{escape(str(s['target_pace_or_power']))}{escape(str(s['unit']))}</td>"
        f"<td class='num'>{escape(str(s.get('split_label') or ''))}</td>"
        f"<td class='reason'>{escape(str(s.get('note') or ''))}</td></tr>"
        for s in data.splits
    )

    gate_rows = "".join(
        f"<tr><td>{escape(str(g['name']).replace('_', ' '))}</td>"
        f"<td class='num'>{g['limit_minutes']:.0f} min</td>"
        f"<td class='num'>{g['eta_minutes']:.0f} min</td>"
        f"<td class='num state' style=\"color: {tokens.STATE_COLOURS.get(str(g['state']), tokens.INK)}\">"
        # The glyph is what survives a monochrome print; the colour is an
        # enhancement, never the only carrier of meaning.
        f"<span class='glyph'>{tokens.STATE_GLYPHS.get(str(g['state']), '')}</span>"
        f"{escape(str(g.get('margin_label') or ''))}</td></tr>"
        for g in data.gates
    )

    fuel = data.fuelling
    aid_rows = "".join(
        f"<tr><td class='num'>{a['at_clock_minutes']:.0f} min</td>"
        f"<td class='num'>{escape(str(a['leg']))} {a['at_km']:.1f}</td>"
        f"<td>{escape(str(a['action_text']))}</td></tr>"
        for a in data.aid_actions[:14]
    )

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{_base_css()}</style></head><body>
{_provenance_block(data)}
<div class="meta">{escape(data.athlete_name)} · {escape(data.course_name)},
{escape(data.course_place)} · {escape(data.event_date)} {escape(data.start_time)}</div>
<h1>Race card</h1>
<div class="headline">{escape(data.projected_label)}</div>
<div class="meta">projected finish · {escape(data.feasibility)}</div>

<h2>Pacing</h2>
<table><tr><th>Leg</th><th>Distance</th><th>Target</th><th>Split</th><th></th></tr>
{split_rows}</table>

<h2>Cut-off margins</h2>
<table><tr><th>Gate</th><th>Limit</th><th>ETA</th><th>Margin</th></tr>
{gate_rows}</table>

<h2>Fuelling per hour</h2>
<table><tr><th>Carb</th><th>Fluid</th><th>Sodium</th><th>Caffeine total</th></tr>
<tr><td class="num">{fuel.get('carb_g_per_hr', 0)} g</td>
<td class="num">{fuel.get('fluid_ml_per_hr', 0)} ml</td>
<td class="num">{fuel.get('sodium_mg_per_hr', 0)} mg</td>
<td class="num">{fuel.get('caffeine_mg_total', 0)} mg</td></tr></table>
{"<div class='reason'>Needs a 2:1 glucose:fructose mix above 60 g/hr.</div>"
 if fuel.get("requires_multiple_transportable") else ""}

<h2>Aid plan</h2>
<table><tr><th>Clock</th><th>At</th><th>Action</th></tr>{aid_rows}</table>
</body></html>"""
    return html


def render_race_card(data: PlanRenderData) -> bytes:
    """One A5 page. Regenerated on every solve."""
    return _to_pdf(race_card_html(data))


def bag_manifest_html(data: PlanRenderData) -> str:
    """One page per bag, five total. Each item annotated with its reason.

    "Nothing in the five bags is a generic checklist item" — so the reason is
    printed beside the item, not merely stored.
    """
    pages = []
    for bag in data.bags:
        items = (
            "".join(
                f"<tr><td>{escape(str(item['name']))}</td>"
                f"<td class='num'>{escape(str(item.get('qty') or ''))}</td>"
                f"<td class='reason'>{escape(str(item.get('reason_text') or ''))}</td></tr>"
                for item in bag.get("items", [])
            )
            or "<tr><td colspan='3' class='reason'>Nothing needed in this bag.</td></tr>"
        )

        pages.append(
            f"""<section style="page-break-after: always">
<div class="meta">{escape(data.athlete_name)} · {escape(data.course_name)}</div>
<h1>{escape(str(bag['name']))}</h1>
<div class="meta">{escape(str(bag['when_label']))} ·
{bag.get('item_count', 0)} item{'s' if bag.get('item_count', 0) != 1 else ''}</div>
<table><tr><th>Item</th><th>Qty</th><th>Why it is here</th></tr>{items}</table>
</section>"""
        )

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<style>{_base_css()}
section:last-of-type {{ page-break-after: auto; }}
</style></head><body>
{_provenance_block(data)}
{''.join(pages)}
</body></html>"""
    return html


def render_bag_manifests(data: PlanRenderData) -> bytes:
    """One page per bag."""
    return _to_pdf(bag_manifest_html(data))


def _to_pdf(html: str) -> bytes:
    rendered = HTML(string=html).write_pdf()
    if rendered is None:  # pragma: no cover - WeasyPrint returns bytes here
        raise RuntimeError("PDF rendering produced no output")
    return bytes(rendered)
