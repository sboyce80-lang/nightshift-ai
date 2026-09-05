"""
Estimator-grade measurement markups — the WRITE side of markup_takeoff.py.

Root cause it addresses (JW Estimating markup sets, 2026-09-01):
Our "annotated drawings" attachment was a QA artifact, not a takeoff markup.
It burned a full-width banner across the top of every sheet ("EXTRACTION
FAILURE — this sheet should contain rooms"), drew a thin box around the room
*label text*, and captioned it with an internal match-quality token
(`[exact]`, `[token]`, `MISS: ...`). Everything was painted into the page
content stream, so nothing was selectable, filterable, or summable — and the
one number an estimator actually wants (the quantity we priced) appeared
nowhere on the sheet.

JW's markups are the opposite, and they are the convention the trade reads:
every measured surface is a real PDF annotation whose `/Subj` is the scope
("Wall Paint PT-1", "Ceiling Paint") and whose `/Contents` is the quantity
("57 sf", "58'-8\""), colored per finish code at 50% fill, carrying a
`/Measure` dictionary so Bluebeam treats it as a live measurement, with a
per-sheet legend table summing each scope. Their estimator can open the set,
sort the Markups List by Subject, and reconcile our number against the
drawing one polygon at a time.

This module emits that grammar. It is a deliberate mirror of
`markup_takeoff.py`: the annotations we write here parse cleanly back through
`extract_markup_takeoff()`, so our own reader is the round-trip test.

Hard-numbers policy, applied to geometry: never draw a shape we did not
measure. A room whose outline we traced gets a traced polygon; a room we
priced from stated dimensions but could not trace gets a nominal rectangle
that is explicitly labeled "(nominal)"; a quantity with no place to live on
the sheet (door counts, a room with no anchor) goes in the legend's
"not placed" block rather than being drawn somewhere plausible. Nothing is
silently dropped and nothing is invented.

Pure geometry + formatting; no LLM, no pricing.
"""

import math
import os

import numpy as np

try:
    import fitz
except ImportError:  # pragma: no cover
    fitz = None

MARKUP_AUTHOR_DEFAULT = "KnightShiftAI"

# ---------------------------------------------------------------------------
# Scope styling
# ---------------------------------------------------------------------------
# Colors mirror JW's own palette so a reviewer holding both sets sees the same
# code in the same color. RGB 0..1; (stroke, fill).
_RED = (1.0, 0.0, 0.0)

_SCOPE_STYLES = {
    "PT-1":            (_RED, (0.2510, 0.0, 0.5020)),      # purple
    "PT-2":            (_RED, (0.5020, 0.0, 0.7530)),      # violet
    "PT-3":            (_RED, (0.0, 0.5020, 0.0)),         # green
    "PT-4":            (_RED, (1.0, 0.0, 0.5020)),         # pink
    "PT-5":            (_RED, (1.0, 0.5020, 0.0)),         # orange
    "CEILING":         ((0.5020, 1.0, 1.0), (0.5020, 1.0, 1.0)),      # cyan
    "CEILING_MR":      ((0.5020, 1.0, 0.5020), (0.5020, 1.0, 0.5020)),  # lt green
    "WALLCOVERING":    ((0.0, 0.5020, 0.7530), (0.0, 0.2510, 0.5020)),  # blue
    "BASE":            ((1.0, 0.5020, 0.0), (1.0, 0.5020, 0.0)),      # orange
    "FLOOR":           ((0.4000, 0.4000, 0.4000), (0.7000, 0.7000, 0.7000)),
    "EXCLUDED":        ((0.4500, 0.4500, 0.4500), (0.7500, 0.7500, 0.7500)),
    "DEFAULT":         (_RED, (0.5020, 0.5020, 0.5020)),
}

# Deterministic cycle for finish codes we have no fixed color for, so the same
# code gets the same color on every sheet of a set (and across reruns).
_FALLBACK_FILLS = [
    (0.2510, 0.0, 0.5020), (1.0, 0.0, 0.5020), (0.0, 0.5020, 0.0),
    (1.0, 0.5020, 0.0), (0.0, 0.2510, 0.5020), (0.5020, 0.2510, 0.0),
]

_FILL_OPACITY = 0.5
_EXCLUDED_OPACITY = 0.18

# Bluebeam /MeasurementTypes bitfield values observed on JW's own markups.
_MT_AREA = 129
_MT_LENGTH = 130


def scope_style(code):
    """(stroke, fill) for a finish code or scope key. Stable across runs."""
    key = (code or "").strip().upper()
    if key in _SCOPE_STYLES:
        return _SCOPE_STYLES[key]
    if not key:
        return _SCOPE_STYLES["DEFAULT"]
    idx = sum(ord(c) for c in key) % len(_FALLBACK_FILLS)
    return (_RED, _FALLBACK_FILLS[idx])


# ---------------------------------------------------------------------------
# Quantity formatting — must round-trip through markup_takeoff's parsers
# ---------------------------------------------------------------------------

def fmt_sf(value):
    """`1,356 sf` — matches markup_takeoff._SF_RE."""
    return f"{float(value):,.0f} sf"


def fmt_ftin(feet):
    """`58'-8"` — matches markup_takeoff.parse_ftin (inch-resolution)."""
    feet = float(feet)
    whole = int(feet)
    inches = int(round((feet - whole) * 12))
    if inches == 12:
        whole += 1
        inches = 0
    return f"{whole}'-{inches}\""


def fmt_scale(pts_per_ft):
    """Human scale label for the /Measure /R string, e.g. `1/8" = 1'-0"`."""
    if not pts_per_ft:
        return ""
    inches_per_ft = pts_per_ft / 72.0
    for num, den in ((1, 16), (3, 32), (1, 8), (3, 16), (1, 4), (3, 8),
                     (1, 2), (3, 4), (1, 1)):
        if abs(inches_per_ft - num / den) < 1e-3:
            frac = f"{num}/{den}" if den != 1 else f"{num}"
            return f'{frac}" = 1\'-0"'
    return f'{inches_per_ft:.4f}" = 1\'-0"'


# ---------------------------------------------------------------------------
# Raster contour tracing (numpy only — no scipy/skimage in the runtime)
# ---------------------------------------------------------------------------

# Moore-neighborhood offsets, clockwise starting east.
_MOORE = [(0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1), (-1, 0), (-1, 1)]


def trace_boundary(mask, max_steps=400000):
    """Moore-neighborhood boundary trace of the region in `mask`.

    Returns the closed list of (row, col) boundary pixels, or [] if empty.
    Termination uses the (cell, entry-direction) state rather than "back at the
    start": a room whose boundary revisits the start pixel — an L-shape with a
    one-cell neck, a doorway pier — would otherwise stop after a few steps and
    report a fraction of the true outline.
    """
    if mask is None or not mask.any():
        return []
    h, w = mask.shape
    ys, xs = np.nonzero(mask)
    start = (int(ys[0]), int(xs[0]))  # row-major => topmost, then leftmost

    def solid(r, c):
        return 0 <= r < h and 0 <= c < w and mask[r, c]

    contour = [start]
    current = start
    prev = (start[0], start[1] - 1)   # the cell west of start is empty
    seen = set()
    steps = 0

    while steps < max_steps:
        steps += 1
        state = (current, prev)
        if state in seen:
            break
        seen.add(state)

        offset = (prev[0] - current[0], prev[1] - current[1])
        try:
            base = _MOORE.index(offset)
        except ValueError:
            base = 4  # treat an unexpected entry as coming from the west
        nxt = None
        for k in range(1, 9):
            j = (base + k) % 8
            cand = (current[0] + _MOORE[j][0], current[1] + _MOORE[j][1])
            if solid(*cand):
                # The next backtrack is the last EMPTY cell we examined, i.e.
                # the neighbour one step counter-clockwise of the one we took.
                back = (base + k - 1) % 8
                prev = (current[0] + _MOORE[back][0],
                        current[1] + _MOORE[back][1])
                nxt = cand
                break
        if nxt is None:
            break  # isolated pixel
        current = nxt
        contour.append(current)

    return contour


def _rdp(points, eps):
    """Iterative Ramer-Douglas-Peucker. `points` is a list of (x, y)."""
    if len(points) < 3:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 <= i0 + 1:
            continue
        x0, y0 = points[i0]
        x1, y1 = points[i1]
        dx, dy = x1 - x0, y1 - y0
        norm = math.hypot(dx, dy)
        best_d, best_i = -1.0, None
        for i in range(i0 + 1, i1):
            px, py = points[i]
            if norm == 0:
                d = math.hypot(px - x0, py - y0)
            else:
                d = abs(dy * px - dx * py + x1 * y0 - y1 * x0) / norm
            if d > best_d:
                best_d, best_i = d, i
        if best_d > eps and best_i is not None:
            keep[best_i] = True
            stack.append((i0, best_i))
            stack.append((best_i, i1))
    return [p for p, k in zip(points, keep) if k]


def contour_to_polygon(mask, px_per_pt, simplify_ft=0.5, px_per_ft=6.0,
                       max_vertices=60):
    """Traced room outline as PDF-point (x, y) vertices.

    `simplify_ft` sets the RDP tolerance: half a foot keeps real jogs (pilasters,
    closets) while dissolving the raster staircase along angled walls.
    """
    contour = trace_boundary(mask)
    if len(contour) < 4:
        return []
    pts = [(c / px_per_pt, r / px_per_pt) for r, c in contour]
    eps_pt = (simplify_ft * px_per_ft) / px_per_pt
    simplified = _rdp(pts, eps_pt)
    # RDP keeps endpoints; the trace is closed, so drop the duplicated tail.
    if len(simplified) > 1 and simplified[0] == simplified[-1]:
        simplified = simplified[:-1]
    while len(simplified) > max_vertices:
        eps_pt *= 1.6
        simplified = _rdp(pts, eps_pt)
        if len(simplified) > 1 and simplified[0] == simplified[-1]:
            simplified = simplified[:-1]
    return simplified if len(simplified) >= 3 else []


def polygon_area_sqft(points, pts_per_ft):
    """Shoelace area of a point list, converted to square feet."""
    if not points or len(points) < 3 or not pts_per_ft:
        return 0.0
    total = 0.0
    for i in range(len(points)):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % len(points)]
        total += x0 * y1 - x1 * y0
    return abs(total) / 2.0 / (float(pts_per_ft) ** 2)


def polygon_perimeter_pt(points):
    """Closed perimeter of a point list, in PDF points."""
    if len(points) < 2:
        return 0.0
    total = 0.0
    for i in range(len(points)):
        x0, y0 = points[i]
        x1, y1 = points[(i + 1) % len(points)]
        total += math.hypot(x1 - x0, y1 - y0)
    return total


# A traced outline is only usable as a markup if it depicts the quantity we
# label it with. Outside this band the flood fill leaked into the corridor (or
# stopped at a furniture line), and drawing it would assert a measurement we
# did not make — fall back to the nominal box instead.
_TRACE_AREA_MIN = 0.5
_TRACE_AREA_MAX = 2.0


def trace_agrees_with_priced(points, room, pts_per_ft):
    """True if a traced outline is close enough to the priced area to stand for it."""
    priced = float((room.get("dimensions") or {}).get("floor_area_sqft") or 0)
    if priced <= 0 or not pts_per_ft:
        return False
    traced = polygon_area_sqft(points, pts_per_ft)
    if traced <= 0:
        return False
    ratio = traced / priced
    return _TRACE_AREA_MIN <= ratio <= _TRACE_AREA_MAX


# ---------------------------------------------------------------------------
# Bluebeam /Measure dictionary
# ---------------------------------------------------------------------------

def make_measure_xref(doc, pts_per_ft):
    """Create a `/Measure` object so Bluebeam reads our shapes as measurements.

    `/X` carries feet-per-point (the inverse of the detected scale), `/A` the
    area unit, `/D` the ft'-in" distance formatting. Mirrors the dictionary on
    JW's own polygons.
    """
    if not pts_per_ft:
        return None
    ft_per_pt = 1.0 / float(pts_per_ft)
    label = fmt_scale(pts_per_ft)
    obj = (
        "<< /Type /Measure /Subtype /RL"
        f" /R ({label})"
        " /X [ << /Type /NumberFormat /U (') /C %.9f /F /F /D 4 /FD true /SS () >> ]"
        " /D [ << /Type /NumberFormat /U (') /C 1 /F /F /D 4 /FD true /PS () /SS (-) >>"
        "       << /Type /NumberFormat /U (\") /C 12 /F /F /D 4 /FD true /PS () /SS () >> ]"
        " /A [ << /Type /NumberFormat /U (sf) /C 1 /D 100 /FD true /SS () >> ]"
        " >>"
    ) % ft_per_pt
    xref = doc.get_new_xref()
    doc.update_object(xref, obj)
    return xref


# ---------------------------------------------------------------------------
# Markup planning — analysis dict -> shapes
# ---------------------------------------------------------------------------

_MAX_SUBJECT_TOKEN = 18


def _short_code(value):
    """Keep scope subjects sortable in Bluebeam's Markups List.

    Substrate strings can be a full spec line ("INSULATED PANEL - 4"
    POLYURETHANE"); the full text stays in the popup, the subject gets a stub.
    """
    value = (value or "").strip().upper()
    if len(value) <= _MAX_SUBJECT_TOKEN:
        return value
    return value[:_MAX_SUBJECT_TOKEN].rstrip(" -,") + "\u2026"


def _wall_code(room):
    """Finish code for a room's walls, e.g. 'PT-1'. Falls back to the material."""
    mats = room.get("materials") or {}
    for key in ("wall_finish_code", "wall_paint_code", "paint_code"):
        val = (mats.get(key) or room.get(key) or "").strip()
        if val:
            return val.upper()
    walls = (mats.get("walls") or "").strip()
    # "GYP (assumed)" -> "GYP"; assumed-ness is surfaced separately.
    return _short_code(walls.split("(")[0])


def _is_assumed(room, field):
    mats = room.get("materials") or {}
    return "(assumed)" in (mats.get(field) or "").lower()


def plan_excluded_markup(room, polygon, nominal):
    """Outline-only shape for a room we deliberately did NOT price.

    Dropping these silently is how a scope error hides: the Northwell miss
    (2026-08-31) was 161 of 185 rooms removed by template-instance dedup, and
    nothing on the drawings said so. A grey outline carrying the exclusion
    reason makes that decision reviewable on the sheet where it applies.
    """
    if not polygon:
        return None
    dims = room.get("dimensions") or {}
    name = room.get("room_name") or room.get("room_id") or "?"
    reason = (room.get("scope_exclusion_reason") or "not in priced scope")
    area = float(dims.get("floor_area_sqft") or 0)
    return {
        "kind": "polygon", "points": polygon, "style_key": "EXCLUDED",
        "subject": "Excluded from scope",
        "contents": fmt_sf(area) if area else "excluded",
        "qty": area, "unit": "sf",
        "popup": f"{name} — NOT PRICED{' (nominal)' if nominal else ''}\n"
                 f"Reason: {reason}",
        "measure": _MT_AREA, "excluded": True,
    }


def plan_room_markups(room, polygon, nominal, pts_per_ft):
    """Scope shapes for one room. Returns (markups, unplaced).

    Area scopes become polygons on the room outline; wall and base-trim scopes
    become polylines on that same outline, which is the actual run they are
    measured along. Counts (doors, windows) have no defensible location on a
    plan sheet, so they are returned as `unplaced` for the legend.
    """
    dims = room.get("dimensions") or {}
    mats = room.get("materials") or {}
    els = room.get("elements") or {}
    name = room.get("room_name") or room.get("room_id") or "?"
    mult = int(room.get("unit_multiplier") or 1)
    suffix = f" (x{mult})" if mult > 1 else ""
    nom = " (nominal)" if nominal else ""

    markups, unplaced = [], []

    def note(label, qty, unit):
        unplaced.append({"room": name, "subject": label,
                         "qty": float(qty), "unit": unit})

    # --- Ceiling: the room outline IS the ceiling plane ---------------------
    ceil_sf = float(dims.get("ceiling_area_sqft") or 0)
    if ceil_sf > 0 and mats.get("ceiling_painted") and polygon:
        ceil_type = _short_code((mats.get("ceiling") or "").split("(")[0])
        style_key = "CEILING_MR" if "MR" in ceil_type else "CEILING"
        subject = "Ceiling Paint" + (f" {ceil_type}" if ceil_type else "")
        markups.append({
            "kind": "polygon", "points": polygon, "style_key": style_key,
            "subject": subject, "contents": fmt_sf(ceil_sf * mult),
            "qty": ceil_sf * mult, "unit": "sf",
            "popup": f"{name}{suffix} — ceiling{nom}"
                     + (" — type assumed, confirm (RFI)"
                        if _is_assumed(room, "ceiling") else ""),
            "measure": _MT_AREA,
        })
    elif ceil_sf > 0 and mats.get("ceiling_painted"):
        note("Ceiling Paint", ceil_sf * mult, "sf")
    elif ceil_sf > 0:
        note("Ceiling — not painted (scope gate)", ceil_sf * mult, "sf")

    # --- Walls: measured along the room perimeter --------------------------
    wall_sf = float(dims.get("wall_area_sqft") or 0)
    perim_lf = float(dims.get("perimeter_lf") or 0)
    height = float(dims.get("ceiling_height_feet") or 0)
    if wall_sf > 0:
        code = _wall_code(room)
        subject = (f"{fmt_ftin(height)} H Wall Paint {code}".strip()
                   if height else f"Wall Paint {code}".strip())
        if perim_lf > 0 and polygon:
            markups.append({
                "kind": "polyline", "points": polygon, "style_key": code,
                "subject": subject, "contents": fmt_ftin(perim_lf),
                "qty": wall_sf * mult, "unit": "sf",
                "popup": (f"{name}{suffix} — walls{nom}\n"
                          f"{perim_lf:,.0f} LF x {height:g}' H = "
                          f"{fmt_sf(wall_sf)}"
                          + (f" x {mult} units = {fmt_sf(wall_sf * mult)}"
                             if mult > 1 else "")
                          + ("\nSubstrate assumed, confirm (RFI)"
                             if _is_assumed(room, "walls") else "")),
                "measure": _MT_LENGTH,
            })
        else:
            note(f"Wall Paint {code}".strip(), wall_sf * mult, "sf")

    # --- Base trim: same run as the walls ----------------------------------
    base_lf = float(els.get("base_trim_lf") or 0)
    if base_lf > 0:
        base_mat = _short_code((mats.get("base") or "").split("(")[0])
        if polygon:
            markups.append({
                "kind": "polyline", "points": polygon, "style_key": "BASE",
                "subject": f"Base Trim {base_mat}".strip(),
                "contents": fmt_ftin(base_lf),
                "qty": base_lf * mult, "unit": "lf",
                "popup": f"{name}{suffix} — base trim{nom}",
                "measure": _MT_LENGTH, "inset_pt": 3.0,
            })
        else:
            note(f"Base Trim {base_mat}".strip(), base_lf * mult, "lf")

    # --- Wallcovering ------------------------------------------------------
    wc_sf = float(els.get("wallcovering_sqft") or 0)
    if wc_sf > 0:
        if polygon:
            markups.append({
                "kind": "polyline", "points": polygon,
                "style_key": "WALLCOVERING", "subject": "Wall Covering WC-1",
                "contents": fmt_sf(wc_sf * mult), "qty": wc_sf * mult,
                "unit": "sf", "popup": f"{name}{suffix} — wallcovering{nom}",
                "measure": _MT_LENGTH, "inset_pt": 6.0,
            })
        else:
            note("Wall Covering WC-1", wc_sf * mult, "sf")

    # --- Counts: no defensible location on a plan --------------------------
    for key, label, unit in (
        ("doors_full_paint", "Door Paint — full", "ea"),
        ("doors_hm_panel", "Door Paint — HM panel", "ea"),
        ("doors_frame_only", "Door Frame Paint", "ea"),
        ("windows_painted_interior", "Window Paint — interior", "ea"),
        ("painted_columns_ea", "Painted Columns", "ea"),
    ):
        val = float(els.get(key) or 0)
        if val > 0:
            note(label, val * mult, unit)

    return markups, unplaced


def _inset_polygon(points, inset_pt):
    """Shrink a polygon toward its centroid so stacked runs stay readable."""
    if not points or inset_pt <= 0:
        return points
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    out = []
    for x, y in points:
        dx, dy = x - cx, y - cy
        d = math.hypot(dx, dy)
        if d <= inset_pt:
            out.append((x, y))
        else:
            out.append((x - dx / d * inset_pt, y - dy / d * inset_pt))
    return out


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------

def add_markup(doc, page, mk, author, measure_xref):
    """Emit one markup as a real PDF annotation in JW's grammar."""
    pts = mk["points"]
    if mk.get("inset_pt"):
        pts = _inset_polygon(pts, mk["inset_pt"])
    if len(pts) < 3:
        return None

    stroke, fill = scope_style(mk.get("style_key"))
    if mk["kind"] == "polygon":
        annot = page.add_polygon_annot(pts)
        annot.set_colors(stroke=stroke, fill=fill)
    else:
        annot = page.add_polyline_annot(list(pts) + [pts[0]])  # closed run
        annot.set_colors(stroke=fill)

    annot.set_info(title=author, subject=mk["subject"],
                   content=mk.get("popup") or mk["subject"])
    if mk.get("excluded"):
        # Dashed and faint: present for review, never mistaken for priced work.
        annot.set_border(width=1, dashes=[4, 3])
        annot.set_opacity(_EXCLUDED_OPACITY)
    else:
        annot.set_border(width=2 if mk["kind"] == "polyline" else 1)
        annot.set_opacity(_FILL_OPACITY if mk["kind"] == "polygon" else 1.0)
    annot.update()

    # Keys PyMuPDF has no setter for — these are what make Bluebeam treat the
    # shape as a measurement rather than a doodle.
    xr = annot.xref
    doc.xref_set_key(xr, "IT", "/PolygonDimension"
                     if mk["kind"] == "polygon" else "/PolyLineDimension")
    doc.xref_set_key(xr, "MeasurementTypes", str(mk.get("measure", _MT_AREA)))
    doc.xref_set_key(xr, "Contents", fitz.get_pdf_str(mk["contents"]))
    if mk["kind"] == "polygon":
        doc.xref_set_key(xr, "FillOpacity",
                         str(_EXCLUDED_OPACITY if mk.get("excluded")
                             else _FILL_OPACITY))
    if measure_xref:
        doc.xref_set_key(xr, "Measure", f"{measure_xref} 0 R")
    return annot


# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------

_LEGEND_ROW_H = 16
_LEGEND_PAD = 6


def _compose_legend(totals, unplaced_totals, title):
    """Render the legend table onto its own upright page; returns (doc, rect).

    Composing separately and stamping the result keeps the table readable on
    rotated sheets: `insert_text` writes into the unrotated mediabox, so a 270-
    rotated plan (most of them) would otherwise get a sideways legend.
    """
    rows = [(subj, qty, unit, style, False)
            for (subj, unit, style), qty in sorted(totals.items())]
    rows += [(subj, qty, unit, None, True)
             for (subj, unit), qty in sorted(unplaced_totals.items())]
    if not rows:
        return None, None

    col_swatch, col_desc, col_qty, col_unit = 18, 214, 78, 40
    width = col_swatch + col_desc + col_qty + col_unit + _LEGEND_PAD * 2
    header_h = _LEGEND_ROW_H + 4
    foot_h = 12 if unplaced_totals else 0
    height = header_h * 2 + _LEGEND_ROW_H * len(rows) + foot_h + _LEGEND_PAD * 2

    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    page.draw_rect(fitz.Rect(0, 0, width, height), color=_RED,
                   fill=(1, 1, 1), width=1.2)

    y = _LEGEND_PAD
    page.insert_text((_LEGEND_PAD, y + 11), title, fontsize=10,
                     fontname="hebo", color=(0, 0, 0))
    y += header_h

    page.draw_line(fitz.Point(0, y), fitz.Point(width, y), color=_RED,
                   width=0.8)
    hx = _LEGEND_PAD
    for label, w in (("", col_swatch), ("Description", col_desc),
                     ("Quantity", col_qty), ("Unit", col_unit)):
        if label:
            page.insert_text((hx + 3, y + 11), label, fontsize=8,
                             fontname="hebo", color=(0, 0, 0))
        hx += w
    y += header_h
    page.draw_line(fitz.Point(0, y), fitz.Point(width, y), color=_RED,
                   width=0.8)

    for subj, qty, unit, style_key, is_unplaced in rows:
        cx = _LEGEND_PAD
        if style_key is not None:
            _, fill = scope_style(style_key)
            page.draw_rect(fitz.Rect(cx + 2, y + 3, cx + 13, y + 13),
                           color=(0, 0, 0), fill=fill, width=0.5,
                           fill_opacity=_FILL_OPACITY)
        cx += col_swatch
        color = (0.35, 0.35, 0.35) if is_unplaced else (0, 0, 0)
        page.insert_text((cx + 3, y + 11), subj[:46], fontsize=8, color=color)
        cx += col_desc
        qty_s = f"{qty:,.0f}"
        tw = fitz.get_text_length(qty_s, fontname="hebo", fontsize=8)
        page.insert_text((cx + col_qty - 6 - tw, y + 11), qty_s, fontsize=8,
                         fontname="hebo", color=color)
        cx += col_qty
        page.insert_text((cx + 3, y + 11), unit, fontsize=8, color=color)
        y += _LEGEND_ROW_H

    if unplaced_totals:
        page.insert_text((_LEGEND_PAD, y + 8),
                         "Grey = measured but not located on this sheet",
                         fontsize=6.5, color=(0.35, 0.35, 0.35))
    return doc, fitz.Rect(0, 0, width, height)


def draw_legend(page, totals, unplaced_totals, title="KnightShiftAI Takeoff"):
    """Stamp the legend at the visible bottom-left, as JW places theirs."""
    legend_doc, rect = _compose_legend(totals, unplaced_totals, title)
    if legend_doc is None:
        return None
    pr = page.rect
    scale = min(1.0, (pr.width * 0.30) / rect.width)
    w, h = rect.width * scale, rect.height * scale
    target = fitz.Rect(24, pr.height - 24 - h, 24 + w, pr.height - 24)
    # show_pdf_page writes into the unrotated content stream, so place the rect
    # in mediabox space and pre-rotate by the page's own /Rotate to land
    # upright in the view.
    placed = target * page.derotation_matrix
    try:
        page.show_pdf_page(placed, legend_doc, 0,
                           rotate=(page.rotation or 0) % 360)
    finally:
        legend_doc.close()
    return target


# ---------------------------------------------------------------------------
# Page rendering
# ---------------------------------------------------------------------------

def _derotated_copy(pdf_path, cache):
    """Path to a rotation-zeroed copy of `pdf_path`, built once per render.

    `page.get_drawings()` reports unrotated mediabox coordinates while
    `page.rect` reports the rotated view, so the geometry stack rasterizes a
    270-rotated sheet into a transposed grid. Zeroing `/Rotate` on a working
    copy makes both agree; the caller maps results back to view space.
    """
    if "path" in cache:
        return cache["path"]
    cache["path"] = None
    try:
        import tempfile
        doc = fitz.open(pdf_path)
        for page in doc:
            page.set_rotation(0)
        fd, tmp = tempfile.mkstemp(suffix=".derotated.pdf")
        os.close(fd)
        doc.save(tmp, deflate=True)
        doc.close()
        cache["path"] = tmp
    except Exception:
        cache["path"] = None
    return cache["path"]


def _room_geometry_for_page(pdf_path, page_index, rooms, page_rect,
                            rotation=0, derotate_cache=None):
    """Traced outlines keyed by room id, plus the page scale.

    Returns ({id(room): [(x, y) pt]}, pts_per_ft). Best-effort: any failure in
    the geometry stack yields no outlines and the caller falls back to nominal
    rectangles.
    """
    outlines = {}
    pts_per_ft = None
    try:
        import room_geometry as rg
        import vector_measure as vm
    except Exception:
        return outlines, None

    geom_path = pdf_path
    to_geom = to_view = fitz.Matrix(1, 1)
    if rotation:
        geom_path = _derotated_copy(pdf_path, derotate_cache
                                    if derotate_cache is not None else {})
        if not geom_path:
            return outlines, None
        doc = fitz.open(pdf_path)
        page = doc[page_index]
        to_geom, to_view = page.derotation_matrix, page.rotation_matrix
        doc.close()

    try:
        pts_per_ft, _src = vm.detect_scale_robust(geom_path, page_index)
    except Exception:
        pts_per_ft = None
    if not pts_per_ft:
        return outlines, None

    anchors, anchor_rooms = [], []
    for room in rooms:
        center = anchor_point(room, page_rect)
        if not center:
            continue
        pt = fitz.Point(*center) * to_geom
        anchors.append((room.get("room_id") or room.get("room_name") or "?",
                        pt.x, pt.y))
        anchor_rooms.append(room)
    if not anchors:
        return outlines, pts_per_ft

    try:
        res = rg.measure_room_areas(geom_path, page_index, anchors,
                                    pts_per_ft=pts_per_ft)
    except Exception:
        return outlines, pts_per_ft

    label = res.get("_label")
    meta = res.get("_meta") or {}
    px_per_pt = meta.get("px_per_pt")
    px_per_ft = meta.get("px_per_ft", 6.0)
    if label is None or not px_per_pt:
        return outlines, pts_per_ft

    for idx, room in enumerate(anchor_rooms, start=1):
        info = res["rooms"].get(anchors[idx - 1][0]) or {}
        if info.get("status") != "measured":
            continue  # merged/leaked/on_wall regions are not this room's outline
        mask = (label == idx)
        if not mask.any():
            continue
        try:
            poly = contour_to_polygon(rg._binary_dilate(mask, rg._AREA_CORR_PX),
                                      px_per_pt, px_per_ft=px_per_ft)
        except Exception:
            continue
        if poly:
            if rotation:
                poly = [tuple(fitz.Point(x, y) * to_view) for x, y in poly]
            outlines[id(room)] = poly
    return outlines, pts_per_ft


_SIZE_TOL_PT = 2.0
_EDGE_MARGIN = 0.002   # anchors pinned to the page edge are extraction noise


def anchor_point(room, page_rect):
    """Center of a room's label anchor in visible page points, or None.

    Rejects degenerate anchors — zero extent, or collapsed onto a page edge.
    Old runs emit boxes like [0.31, 1.0, 0.32, 1.0]; drawing a room there puts
    scope on the sheet border, which reads as a measurement and is not one.
    """
    norm = (room.get("bbox") or {}).get("label_bbox_norm")
    if not norm or len(norm) != 4:
        return None
    x0, y0, x1, y1 = (float(v) for v in norm)
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return None
    if min(x0, y0) < _EDGE_MARGIN or max(x1, y1) > 1.0 - _EDGE_MARGIN:
        return None
    return ((x0 + x1) / 2 * page_rect.width,
            (y0 + y1) / 2 * page_rect.height)


def _page_size_mismatch(rooms, page_rect):
    """True if the anchors on this page were measured against a different page."""
    for room in rooms:
        size = (room.get("bbox") or {}).get("page_size_pt")
        if not size or len(size) != 2:
            continue
        if (abs(float(size[0]) - page_rect.width) > _SIZE_TOL_PT
                or abs(float(size[1]) - page_rect.height) > _SIZE_TOL_PT):
            return True
    return False


def _nominal_rect(room, page_rect, pts_per_ft):
    """Rectangle of the dimensions we priced, centered on the room label.

    Not a measurement of the drawing — a picture of the numbers we used, so a
    reviewer can see at a glance where our box disagrees with the room.
    """
    dims = room.get("dimensions") or {}
    L = float(dims.get("length_feet") or 0)
    W = float(dims.get("width_feet") or 0)
    center = anchor_point(room, page_rect)
    if not center or not pts_per_ft or L <= 0 or W <= 0:
        return []
    cx, cy = center
    hw = L * pts_per_ft / 2.0
    hh = W * pts_per_ft / 2.0
    return [(cx - hw, cy - hh), (cx + hw, cy - hh),
            (cx + hw, cy + hh), (cx - hw, cy + hh)]


def render_markup_pdf(pdf_in, result_or_analysis, pdf_out,
                      author=MARKUP_AUTHOR_DEFAULT, legend=True,
                      show_excluded=True):
    """Write a JW-style measurement-markup copy of `pdf_in`.

    Returns a summary dict: {pages, annotated_pages, markups, traced, nominal,
    unplaced, excluded_shown, trace_rejected, skipped_size_mismatch,
    output_size_bytes}, plus the legacy key names the QA renderer returns so
    existing callers keep working.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required")

    from collections import defaultdict

    analysis = result_or_analysis
    inner = result_or_analysis.get("analysis")
    if isinstance(inner, dict) and "floors" in inner:
        analysis = inner

    rooms_by_page = defaultdict(list)
    for floor in analysis.get("floors", []) or []:
        for room in floor.get("rooms", []) or []:
            page_no = room.get("source_page")
            if page_no:
                rooms_by_page[int(page_no)].append(room)

    doc = fitz.open(pdf_in)
    derotate_cache = {}
    stats = {"pages": len(doc), "annotated_pages": 0, "markups": 0,
             "traced": 0, "nominal": 0, "unplaced": 0, "excluded_shown": 0,
             "skipped_size_mismatch": 0, "trace_rejected": 0,
             "marked_page_numbers": []}

    for pg_0 in range(len(doc)):
        rooms = rooms_by_page.get(pg_0 + 1)
        if not rooms:
            continue
        page = doc[pg_0]
        # Anchors are normalized against the page size seen at extraction time.
        # If this PDF's page differs, every coordinate would be silently wrong —
        # skip the sheet rather than paint shapes in the wrong places.
        if _page_size_mismatch(rooms, page.rect):
            stats["skipped_size_mismatch"] += 1
            continue
        outlines, pts_per_ft = _room_geometry_for_page(
            pdf_in, pg_0, rooms, page.rect, rotation=page.rotation,
            derotate_cache=derotate_cache)
        measure_xref = make_measure_xref(doc, pts_per_ft)

        totals = defaultdict(float)
        unplaced_totals = defaultdict(float)
        excluded_totals = defaultdict(float)
        page_markups = 0

        for room in rooms:
            poly = outlines.get(id(room))
            nominal = False
            if poly and not trace_agrees_with_priced(poly, room, pts_per_ft):
                poly = None
                stats["trace_rejected"] += 1
            if not poly:
                poly = _nominal_rect(room, page.rect, pts_per_ft)
                nominal = bool(poly)
            if poly:
                stats["nominal" if nominal else "traced"] += 1

            if room.get("in_scope") is False:
                if not show_excluded:
                    continue
                mk = plan_excluded_markup(room, poly, nominal)
                if mk and add_markup(doc, page, mk, author,
                                     measure_xref) is not None:
                    reason = (room.get("scope_exclusion_reason")
                              or "not in priced scope")
                    excluded_totals[(f"Excluded — {reason}", "sf")] += mk["qty"]
                    stats["excluded_shown"] += 1
                    page_markups += 1
                continue

            markups, unplaced = plan_room_markups(room, poly, nominal,
                                                  pts_per_ft)
            for mk in markups:
                if add_markup(doc, page, mk, author, measure_xref) is not None:
                    totals[(mk["subject"], mk["unit"],
                            mk["style_key"])] += mk["qty"]
                    page_markups += 1
            for item in unplaced:
                unplaced_totals[(item["subject"], item["unit"])] += item["qty"]
                stats["unplaced"] += 1

        secondary = dict(unplaced_totals)
        secondary.update(excluded_totals)
        if legend and (totals or secondary):
            draw_legend(page, totals, secondary)
        if page_markups or secondary:
            stats["annotated_pages"] += 1
            stats["marked_page_numbers"].append(pg_0 + 1)
        stats["markups"] += page_markups

    # Keys the existing annotated-drawings caller (jobs.py) logs by name. The
    # QA renderer is the incumbent shape of this summary; matching it keeps
    # every consumer working whichever renderer produced the file.
    stats["referenced_pages"] = stats["annotated_pages"]
    stats["rooms_drawn"] = stats["markups"]
    stats["misses_marked"] = stats["unplaced"]
    stats["extraction_failures"] = 0   # not a concept for markups

    doc.save(pdf_out, deflate=True, garbage=3)
    doc.close()
    tmp = derotate_cache.get("path")
    if tmp and os.path.exists(tmp):
        try:
            os.unlink(tmp)
        except OSError:
            pass
    stats["output_size_bytes"] = (os.path.getsize(pdf_out)
                                  if os.path.exists(pdf_out) else 0)
    return stats
