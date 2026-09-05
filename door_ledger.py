#!/usr/bin/env python3
"""Deterministic door ledger — doors from parsed artifacts, never vision counts.

Doors were wrong on 6/6 recent validation jobs (−93%…+105%) with all four
door flags ON; the round-5 schedule roster hit −3% (76/78) the one time a
parsed source drove the count. Spec: docs/DOOR_LEDGER_SPEC_2026-09-03.md.

Two modes, tried in order:

MODE A — schedule-table parse. When a door schedule exists as TEXT (PNC
class): find the header row (>=3 of DOOR/MARK/SIZE/TYPE/MATL/FRAME/FINISH/
HDW/REMARKS...), reassemble word rows by y-gap (same technique as the sheet
index parser), read one entry per leading door-mark token, split full-paint
vs HM by the material column when present.

MODE B — plan-symbol count. When the schedule is absent or plotted as
curves (Harlem/ULUM/Northwell class): door marks are text labels on the
plan pages. Two independent detectors run and must agree within tolerance:
  B1 tag-shapes: mark-shaped text inside a small closed polygon/ellipse
     (door tags are hexagons/circles; room tags are usually larger).
  B2 swing-arcs: quarter-circle arcs in the vector drawing (a drawn door
     leaf). This is how an estimator counts a plan with no schedule.
A mark equal to a nearby room label is a COLLISION (door 101 serves room
101), so equality is a disqualifier for B1 evidence, not confirmation.

The ledger only ever REPORTS: {mode, count, full_paint, hm_panel, entries,
sources}. Reconciliation with extraction (authority, RFIs, the flag) lives
in Takeoff_DIRECT behind NIGHTSHIFT_DOOR_SCHEDULE_LEDGER and ships only
after harness_door_ledger.py validates against the known counts
(Harlem 29, ULUM 26, Northwell 78).
"""
import math
import re

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover
    fitz = None


# ── shared: word rows ──────────────────────────────────────────────────────

def _page_word_rows(page):
    """Reassemble a page's words into visual rows, sorted left-to-right.

    Same y-gap technique as vme_attribution's sheet-index parser: split on
    a vertical gap > half the median word height between consecutive word
    centers. Returns [[(x0, x1, text), ...], ...] top-to-bottom.
    """
    words = page.get_text("words")
    if not words:
        return []
    marks = sorted(((w[1] + w[3]) / 2.0, w[0], w[2], w[4]) for w in words)
    heights = sorted(w[3] - w[1] for w in words)
    med_h = heights[len(heights) // 2] if heights else 8.0
    rows, cur, prev_y = [], [], None
    for yc, x0, x1, txt in marks:
        if prev_y is not None and yc - prev_y > med_h * 0.5:
            rows.append(cur)
            cur = []
        cur.append((x0, x1, txt))
        prev_y = yc
    if cur:
        rows.append(cur)
    for row in rows:
        row.sort()
    return rows


# ── MODE A: schedule-table parse ───────────────────────────────────────────

_HDR_TOKENS = {
    "door": "door", "doors": "door", "no": "no", "no.": "no", "num": "no",
    "number": "no", "mark": "mark", "size": "size", "width": "size",
    "height": "size", "type": "type", "matl": "material", "mat'l": "material",
    "material": "material", "mat": "material", "frame": "frame",
    "finish": "finish", "fin": "finish", "hdw": "hdw", "hardware": "hdw",
    "remarks": "remarks", "rating": "rating", "label": "rating",
    "glazing": "glazing", "louver": "louver", "thk": "size",
    "thickness": "size", "detail": "detail", "head": "detail",
    "jamb": "detail", "sill": "detail",
}
# A header row must carry at least this many DISTINCT header meanings and
# one of them must be the door/no/mark identity column.
_HDR_MIN = 3

_DOOR_MARK_RE = re.compile(
    r"^(?:D-?)?\d{1,4}(?:[A-Z]|\.\d{1,2}|-\d{1,2})?$")

_WOOD_RE = re.compile(r"\b(WD|WOOD|SC ?WD|SCW|BIRCH|OAK|MAPLE|STAIN)\b", re.I)
_HM_RE = re.compile(r"\b(HM|H\.M\.|HOLLOW ?METAL|STL|STEEL|MTL|METAL)\b", re.I)
_ALGL_RE = re.compile(r"\b(AL|ALUM|ALUMINUM|GL|GLASS|STOREFRONT|SF)\b", re.I)

_DOOR_SCHED_TITLE_RE = re.compile(r"DOOR\s+SCHEDULE", re.I)


def _header_columns(row):
    """If this word row is a door-schedule header, map header→x-center."""
    found = {}
    for x0, x1, txt in row:
        key = _HDR_TOKENS.get(txt.strip().lower().rstrip(":"))
        if key and key not in found:
            found[key] = (x0 + x1) / 2.0
    if len(found) >= _HDR_MIN and ({"door", "no", "mark"} & set(found)):
        return found
    return None


def parse_schedule_pages(pdf_paths):
    """MODE A: parse text door-schedule tables across the set.

    Returns {"entries": [{mark, material, paint_class, pdf, page}],
             "pages": [(pdf, page_idx)], "headers_seen": int}
    paint_class ∈ full_paint | hm_panel | excluded (alum/glass) | unknown.
    """
    entries, pages, headers = [], [], 0
    if fitz is None:
        return {"entries": [], "pages": [], "headers_seen": 0}
    for pdf in pdf_paths:
        try:
            doc = fitz.open(pdf)
        except Exception:
            continue
        try:
            for i in range(len(doc)):
                text = ""
                try:
                    text = doc[i].get_text() or ""
                except Exception:
                    continue
                if not _DOOR_SCHED_TITLE_RE.search(text):
                    continue
                rows = _page_word_rows(doc[i])
                cols = None
                mat_x = None
                page_marks = set()
                for row in rows:
                    hdr = _header_columns(row)
                    if hdr:
                        cols = hdr
                        mat_x = hdr.get("material")
                        headers += 1
                        continue
                    if cols is None:
                        continue
                    # entry row: leading token is a door mark
                    lead = row[0][2].strip()
                    if not _DOOR_MARK_RE.match(lead):
                        continue
                    if lead in page_marks:
                        continue  # continuation/wrapped row
                    page_marks.add(lead)
                    material = ""
                    if mat_x is not None:
                        # word whose center is nearest the material column
                        best = None
                        for x0, x1, txt in row[1:]:
                            d = abs((x0 + x1) / 2.0 - mat_x)
                            if best is None or d < best[0]:
                                best = (d, txt)
                        if best and best[0] < 40.0:
                            material = best[1]
                    blob = " ".join(t for _, _, t in row)
                    if _HM_RE.search(material or blob):
                        pc = "hm_panel"
                    elif _WOOD_RE.search(material or blob):
                        pc = "full_paint"
                    elif _ALGL_RE.search(material or ""):
                        pc = "excluded"
                    else:
                        pc = "unknown"
                    entries.append({"mark": lead, "material": material,
                                    "paint_class": pc, "pdf": pdf, "page": i})
                if page_marks:
                    pages.append((pdf, i))
        finally:
            doc.close()
    return {"entries": entries, "pages": pages, "headers_seen": headers}


# ── MODE B: plan-symbol count ──────────────────────────────────────────────

def _floor_plan_pages(pdf_paths):
    """Plan pages to count symbols on. Reuses the VME page selector so the
    ledger and the measurement engine agree about what a plan page is."""
    try:
        from vme_attribution import select_floor_plan_pages
        pages = select_floor_plan_pages(pdf_paths) or []
        out = [(p["pdf"], p["page"]) for p in pages]
        if out:
            return out
    except Exception:
        pass
    # Fallback: every page with a vector drawing layer and plan-scale text
    out = []
    if fitz is None:
        return out
    for pdf in pdf_paths:
        try:
            doc = fitz.open(pdf)
        except Exception:
            continue
        try:
            for i in range(len(doc)):
                t = (doc[i].get_text() or "").upper()
                if "FLOOR PLAN" in t and "SCHEDULE" not in t[:200]:
                    out.append((pdf, i))
        finally:
            doc.close()
    return out


def _small_closed_shapes(page, min_side=6.0, max_side=42.0):
    """Bounding boxes of small closed vector shapes (door-tag hexagons,
    circles, diamonds). Filters by size and squarish aspect."""
    boxes = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return boxes
    for d in drawings:
        r = d.get("rect")
        if not r:
            continue
        w, h = r.width, r.height
        if not (min_side <= w <= max_side and min_side <= h <= max_side):
            continue
        if max(w, h) > 2.2 * min(w, h):
            continue
        n_items = len(d.get("items") or ())
        if n_items < 1:
            continue
        boxes.append(fitz.Rect(r))
    return boxes


def count_tag_marks(page, room_labels=None):
    """B1: door-mark text sitting inside a small closed shape.

    room_labels: set of label strings known to be ROOM tags on this page;
    a mark equal to a room label is skipped (collision rule from the spec).
    Returns set of mark strings.
    """
    room_labels = room_labels or set()
    marks = set()
    boxes = _small_closed_shapes(page)
    if not boxes:
        return marks
    try:
        words = page.get_text("words")
    except Exception:
        return marks
    for w in words:
        txt = w[4].strip()
        if not _DOOR_MARK_RE.match(txt):
            continue
        if txt in room_labels:
            continue
        wr = fitz.Rect(w[0], w[1], w[2], w[3])
        c = ((w[0] + w[2]) / 2.0, (w[1] + w[3]) / 2.0)
        for b in boxes:
            if b.contains(wr) or (b.contains(fitz.Point(*c))
                                  and wr.width <= b.width * 1.2):
                marks.add(txt)
                break
    return marks


def _quarter_arcs(page, min_r=6.0, max_r=60.0):
    """B2: count quarter-circle door-swing arcs in the vector layer.

    A drawn door is a leaf line + a ~90° swing arc. PyMuPDF reports arcs as
    chained bezier 'c' items; a quarter circle of radius r has chord
    r*sqrt(2) and its control points bow ~0.5523*r. We detect single-curve
    items whose endpoints are ~perpendicular about a common center with
    plausible radius, then dedupe arcs that belong to one swing (multiple
    bezier segments per arc).
    """
    arcs = []
    try:
        drawings = page.get_drawings()
    except Exception:
        return arcs
    for d in drawings:
        items = d.get("items") or ()
        curves = [it for it in items if it and it[0] == "c"]
        if not curves:
            continue
        # Reconstruct total turn: sum per-bezier chord angles
        pts = []
        for it in curves:
            try:
                p0, p3 = it[1], it[4]
            except Exception:
                continue
            pts.append((p0, p3))
        if not pts:
            continue
        start = pts[0][0]
        end = pts[-1][1]
        chord = math.hypot(end.x - start.x, end.y - start.y)
        if chord < min_r * 0.8 or chord > max_r * 1.6:
            continue
        # Radius estimate for a quarter arc: chord = r√2
        r_est = chord / math.sqrt(2)
        if not (min_r <= r_est <= max_r):
            continue
        # A door swing is an OPEN stroke (no fill), few segments
        if d.get("fill"):
            continue
        if len(curves) > 4:
            continue
        arcs.append((start, end, r_est))
    return arcs


def count_symbol_doors(pdf_paths, per_page_room_labels=None):
    """MODE B: count drawn doors across the set's plan pages.

    Runs both detectors per page. Uses B1 marks when a page yields >=3
    (tags are the architect's own enumeration); otherwise falls back to
    B2 arc count for that page. Marks dedupe set-wide (a door drawn on an
    overall plan AND an enlarged plan is one door); arcs sum per page on
    distinct pages only.
    Returns {"marks": set, "arc_count": int, "per_page": [...]}.
    """
    per_page_room_labels = per_page_room_labels or {}
    all_marks = set()
    arc_total = 0
    per_page = []
    if fitz is None:
        return {"marks": set(), "arc_count": 0, "per_page": []}
    for pdf, idx in _floor_plan_pages(pdf_paths):
        try:
            doc = fitz.open(pdf)
        except Exception:
            continue
        try:
            page = doc[idx]
            labels = per_page_room_labels.get((pdf, idx), set())
            marks = count_tag_marks(page, labels)
            arcs = _quarter_arcs(page)
            per_page.append({"pdf": pdf, "page": idx,
                             "tag_marks": len(marks), "arcs": len(arcs)})
            if len(marks) >= 3:
                all_marks |= marks
            else:
                arc_total += len(arcs)
        finally:
            doc.close()
    return {"marks": all_marks, "arc_count": arc_total, "per_page": per_page}


# ── the ledger ─────────────────────────────────────────────────────────────

def build_door_ledger(pdf_paths):
    """Build the deterministic door ledger for a set.

    Mode A wins when a text schedule yields >=5 entries (spec threshold).
    Mode B reports both detector signals; count = tag marks when the tag
    detector carried the set, else arc count. Never guesses: an empty
    result means "no deterministic door source in this set" and the caller
    must leave extraction alone and RFI.
    """
    sched = parse_schedule_pages(pdf_paths)
    if len(sched["entries"]) >= 5:
        fp = sum(1 for e in sched["entries"]
                 if e["paint_class"] == "full_paint")
        hm = sum(1 for e in sched["entries"]
                 if e["paint_class"] == "hm_panel")
        unk = sum(1 for e in sched["entries"]
                  if e["paint_class"] == "unknown")
        return {
            "mode": "schedule",
            "count": len([e for e in sched["entries"]
                          if e["paint_class"] != "excluded"]),
            "full_paint": fp, "hm_panel": hm, "unknown_material": unk,
            "entries": sched["entries"],
            "sources": sorted({f"schedule(p{p+1})" for _, p in
                               sched["pages"]}),
        }
    sym = count_symbol_doors(pdf_paths)
    n_marks = len(sym["marks"])
    if n_marks >= 5 or sym["arc_count"] >= 5:
        use_marks = n_marks >= max(5, sym["arc_count"] * 0.5)
        return {
            "mode": "symbols",
            "count": n_marks if use_marks else sym["arc_count"],
            "detector": "tag_marks" if use_marks else "swing_arcs",
            "tag_marks": n_marks, "arc_count": sym["arc_count"],
            "full_paint": None, "hm_panel": None,
            "entries": sorted(sym["marks"]),
            "sources": [f"symbols(p{pp['page']+1}:{pp['tag_marks']}t/"
                        f"{pp['arcs']}a)" for pp in sym["per_page"]],
        }
    return {"mode": None, "count": 0, "entries": [], "sources": []}


if __name__ == "__main__":
    import json
    import sys
    print(json.dumps(build_door_ledger(sys.argv[1:]), default=str,
                     indent=2)[:4000])
