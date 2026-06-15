"""Vector wall measurement — Phase 3 VME, M1: sheet / floor attribution.

M0 (vector_measure) measures wall faces on ONE sheet. But a plan set draws
each floor's walls on multiple sheets — a per-floor plan, an all-floors
COMPOSITE, enlarged unit plans, sections. Naively measuring every wall-bearing
page double-counts floors (the composite repeats them all).

M1 picks ONE canonical source per physical floor:
  1. classify each page -> {floors present (from wall-layer prefix), scale}
  2. greedily claim each floor from the page covering the FEWEST floors
     (a single-floor sheet beats the all-floors composite), so each floor is
     counted exactly once and the composite is excluded when per-floor sheets
     exist.
  3. measure each claimed floor's wall RUN length (M0 face-union / 2) on its
     source sheet, x per-floor height.

SCOPE (M1): correct sheet selection + no double-counting. It does NOT fix
per-floor SCOPE accuracy (a sheet may carry structural/non-paint walls, or the
real partition detail may live on enlarged plans) — that over/under-count is
M2. Heights come in from the schedule (caller-supplied here).
"""
from __future__ import annotations

import collections
import math
import re

import vector_measure as vm

try:
    import fitz
except ImportError:  # pragma: no cover
    fitz = None

# A wall-layer group token like "M-2", "M-1_SD", "M-Sections" -> floor index.
_FLOOR_TOK = re.compile(r'^m[-_ ]?(\d+)', re.I)
_NONFLOOR = ("section", "roof", "elev", "demo", "site", "schedule")


def floor_of(group: str):
    """Floor index from a wall-layer group prefix, or None if not a floor."""
    g = (group or "").lower()
    if any(k in g for k in _NONFLOOR):
        return None
    m = _FLOOR_TOK.match((group or "").strip())
    return int(m.group(1)) if m else None


def _page_floor_face_pts(page) -> dict:
    """{floor: union'd wall-face length in POINTS} for one page (scale-free)."""
    h_by_floor: dict = collections.defaultdict(dict)
    v_by_floor: dict = collections.defaultdict(dict)
    for path in page.get_drawings():
        layer = path.get("layer") or ""
        if not vm.is_wall_layer(layer):
            continue
        fl = floor_of(layer.split("|")[0])
        if fl is None:
            continue
        H, V = h_by_floor[fl], v_by_floor[fl]
        for it in path["items"]:
            if it[0] != "l":
                continue
            a, b = it[1], it[2]
            if math.hypot(b.x - a.x, b.y - a.y) < 0.5:
                continue
            if abs(a.y - b.y) <= 1.0:
                H.setdefault(round((a.y + b.y) / 2.0), []).append((min(a.x, b.x), max(a.x, b.x)))
            elif abs(a.x - b.x) <= 1.0:
                V.setdefault(round((a.x + b.x) / 2.0), []).append((min(a.y, b.y), max(a.y, b.y)))
    out = {}
    for fl in set(h_by_floor) | set(v_by_floor):
        out[fl] = (sum(vm.union_length(iv) for iv in h_by_floor[fl].values())
                   + sum(vm.union_length(iv) for iv in v_by_floor[fl].values()))
    return out


def classify_pages(pdf_path: str):
    """Return (pages, primary_scale). Each page: {page, scale, pts:{floor:pts}}."""
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required")
    doc = fitz.open(pdf_path)
    pages, scales = [], []
    try:
        for i in range(len(doc)):
            pts = {f: p for f, p in _page_floor_face_pts(doc[i]).items() if p > 50}
            if not pts:
                continue
            sc = vm.detect_scale(pdf_path, i)
            pages.append({"page": i, "scale": sc, "pts": pts, "floors": set(pts)})
            if sc:
                scales.append(sc)
    finally:
        doc.close()
    primary = collections.Counter(scales).most_common(1)[0][0] if scales else 9.0
    return pages, primary


def select_floor_sources(pages) -> dict:
    """Greedy: each floor is claimed by the page covering the FEWEST floors
    (single-floor sheets beat the composite). Returns {floor: page_dict}."""
    order = sorted(pages, key=lambda p: (len(p["floors"]), -sum(p["pts"].values())))
    claimed: dict = {}
    for p in order:
        for fl in p["floors"]:
            claimed.setdefault(fl, p)
    return claimed


def measure_building(pdf_path: str, heights: dict) -> dict:
    """Per-floor wall measurement with one canonical source per floor.

    heights: {floor_index: ceiling_height_ft}. A floor with no height (e.g.
    foundation/roof) is measured but its area is None and it's left out of the
    paintable total.
    Returns {floors: {fl: {...}}, excluded_pages: [...], total_area_sqft, total_runs_lf}.
    """
    pages, primary = classify_pages(pdf_path)
    claimed = select_floor_sources(pages)
    selected_pages = {id(p) for p in claimed.values()}
    floors = {}
    total_area = total_runs = 0.0
    for fl, pg in sorted(claimed.items()):
        sc = pg["scale"] or primary
        runs = pg["pts"][fl] / (2.0 * sc)         # faces/2 = wall runs (LF)
        h = heights.get(fl)
        area = runs * h if h else None
        floors[fl] = {"page": pg["page"] + 1, "scale": sc, "runs_lf": round(runs, 1),
                      "height": h, "area_sqft": round(area, 0) if area else None}
        total_runs += runs
        if area:
            total_area += area
    excluded = [p["page"] + 1 for p in pages
                if id(p) not in selected_pages]   # composites/dupes not used
    return {"floors": floors, "excluded_pages": sorted(excluded),
            "total_area_sqft": round(total_area, 0), "total_runs_lf": round(total_runs, 1),
            "primary_scale": primary}
