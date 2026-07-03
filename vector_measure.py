"""Vector wall measurement — Phase 3 VME, M0.

Measures paintable wall geometry directly from a vector PDF's CAD line
geometry — no vision, no ratios, no heuristics. This is the "truest number"
primitive: it traces and measures the hard lines the architect drew.

De-risked 2026-06-14 on 364 Main: reproduced Rider's hand takeoff
(85,353 SF golden) within 2% (composite all-floors sheet -> 83,656 SF).

Algorithm per sheet (proven in the spike):
  1. LAYER-FILTER get_drawings() to wall-face layers (A-Wall family /
     partition); the per-path `layer` key is populated by PyMuPDF on
     BDC/OCG-tagged pages.
  2. DROP DIAGONAL segments: wall poché/hatch fill is ~45 deg diagonal;
     true wall faces are orthogonal. This removes the dominant overcount.
  3. INTERVAL-UNION collinear axis-aligned segments: collapses coincident
     duplicate face lines (a wall drawn as many overlapping sub-segments,
     or redrawn per discipline) to its true covered length.

Output: wall-FACE linear footage (both faces of each wall are drawn, so
LF already counts both painted sides). Paintable area = wall_face_lf x height.

SCOPE (M0): single-sheet measurement + scale auto-detection. Floor/sheet
attribution and cross-sheet de-duplication are M1; height/paintability are
M2; the geometric classifier for flattened-vector (no-layer) sheets is M5.
"""
from __future__ import annotations

import math
import re

try:
    import fitz  # PyMuPDF
except ImportError:  # pragma: no cover - exercised only without the dep
    fitz = None

PTS_PER_INCH = 72.0

# --------------------------------------------------------------------------
# Wall-layer classification
# --------------------------------------------------------------------------
# AIA-standard wall layers carry these tokens; the negatives strip annotation,
# room-id text, hatch *patterns*, dashed below-walls, and glazing ("lite").
_WALL_POS = ("a-wall", "wall-demising", "partition")
# 'eifs' — exterior substrate; interior wall runs must not absorb exterior
# skin lines (exterior is scoped separately). NOTE: 'belw' (A-WALL-BELW) is
# deliberately NOT excluded — CenHud Fishkill draws real above-grade
# partitions on it (drafters misuse layer names), which is exactly why layer
# names alone cannot classify walls; the geometric wall test (M5) validates
# every candidate layer's segments regardless of name.
_WALL_NEG = ("anno", "iden", "patt", "hatch", "-blow", "below", "-lite",
             "eifs")


def is_wall_layer(layer: str) -> bool:
    """True when a CAD layer name denotes paintable wall-face geometry."""
    l = (layer or "").lower()
    if not l:
        return False
    if any(n in l for n in _WALL_NEG):
        return False
    if any(p in l for p in _WALL_POS):
        return True
    return l == "wall" or l.endswith("|wall")


# --------------------------------------------------------------------------
# Drawing-scale detection  (e.g.  1/8"=1'-0"  ->  9 points/foot)
# --------------------------------------------------------------------------
# paper-inches " = feet ' - inches "   ; tolerate straight + curly quotes.
_SCALE_RE = re.compile(
    r'(\d+(?:-\d+/\d+|/\d+)?)\s*["″”]\s*=\s*'
    r'(\d+)\s*[\'′’]\s*(?:-\s*(\d+)\s*["″”])?'
)


def _inches(token: str) -> float:
    """Parse a paper measurement token into inches: '1/8', '3/32', '1', '1-1/2'."""
    token = token.strip()
    if "-" in token and "/" in token:          # mixed number, e.g. 1-1/2
        whole, frac = token.split("-", 1)
        n, d = frac.split("/")
        return float(whole) + float(n) / float(d)
    if "/" in token:
        n, d = token.split("/")
        return float(n) / float(d)
    return float(token)


def parse_scale(text: str):
    """Points-per-foot from an architectural scale string, or None.

    '1/8"=1'-0"'  ->  0.125 in paper per 1 ft real  ->  0.125*72/1 = 9 pts/ft.
    """
    if not text:
        return None
    m = _SCALE_RE.search(text)
    if not m:
        return None
    try:
        paper_in = _inches(m.group(1))
    except (ValueError, ZeroDivisionError):
        return None
    feet = float(m.group(2)) + (float(m.group(3)) / 12.0 if m.group(3) else 0.0)
    if paper_in <= 0 or feet <= 0:
        return None
    return (paper_in * PTS_PER_INCH) / feet


def detect_scale(pdf_path: str, page_index: int):
    """Read the drawing scale from a page's title-block text.

    Returns points-per-foot, or None if no scale string is found. Prefers a
    line that mentions 'scale'; falls back to the first scale-shaped match.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required for detect_scale")
    doc = fitz.open(pdf_path)
    try:
        txt = doc[page_index].get_text()
    finally:
        doc.close()
    for line in txt.splitlines():
        if "scale" in line.lower():
            s = parse_scale(line)
            if s:
                return s
    return parse_scale(txt)


# --------------------------------------------------------------------------
# M4 — geometry-based scale inference (door-swing arc calibration)
# --------------------------------------------------------------------------
# Many CAD exports plot text as curves: the page has NO extractable text, so
# title-block scale detection is impossible (CenHud Fishkill). But door swings
# are drawn as quarter-circle arcs whose real-world radius equals the door
# leaf width — overwhelmingly 2'-6", 2'-8", or 3'-0". Architectural plot
# scales come from a small discrete set, so instead of estimating a continuous
# scale we SCORE each standard candidate by how many arc radii land on a
# plausible door leaf under that candidate, and require an unambiguous winner.
# No winner -> None (never guess silently; the caller flags the sheet).

# pts-per-foot for the standard imperial plot scales:
# 1/16"  3/32"  1/8"  3/16"  1/4"  3/8"  1/2"  3/4"  1"  = 1'-0"
_STANDARD_SCALES = (4.5, 6.75, 9.0, 13.5, 18.0, 27.0, 36.0, 54.0, 72.0)
# Door leaf widths (feet) that anchor the arc-radius scoring. 2'-0" is
# deliberately EXCLUDED: standard scales sit in 1.5x ratios (9 -> 13.5), and a
# 3'-0" leaf at scale s aliases exactly to a 2'-0" leaf at 1.5s, poisoning the
# vote. Leafs below 2'-4" are rare enough to give up for disambiguation.
_DOOR_LEAF_FT = (2.5, 2.67, 3.0)
_LEAF_TOL_FT = 0.07


def _arc_radii_pts(page, max_paths=40000):
    """Radii (points) of circular-ish arcs on the page.

    A door swing exported to PDF is a chain of cubic beziers. For each bezier
    we estimate a radius from the chord + sagitta of the curve (control points
    give the sagitta direction); segments whose two control-point distances
    from the chord agree (circular, not spline-y) are kept.
    """
    import math
    radii = []
    for path in page.get_drawings()[:max_paths]:
        for item in path["items"]:
            if item[0] != "c":
                continue
            p0, p1, p2, p3 = item[1], item[2], item[3], item[4]
            chord = math.hypot(p3.x - p0.x, p3.y - p0.y)
            if chord < 4:
                continue
            # exact cubic midpoint: B(0.5) = (p0 + 3p1 + 3p2 + p3) / 8
            mx = (p0.x + 3 * p1.x + 3 * p2.x + p3.x) / 8.0
            my = (p0.y + 3 * p1.y + 3 * p2.y + p3.y) / 8.0
            # circumcircle through (p0, mid, p3) — exact for a circular arc
            ax, ay, bx, by, cx, cy = p0.x, p0.y, mx, my, p3.x, p3.y
            d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
            if abs(d) < 1e-6:
                continue  # collinear — a straight "curve", not an arc
            ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
                  + (cx * cx + cy * cy) * (ay - by)) / d
            uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
                  + (cx * cx + cy * cy) * (bx - ax)) / d
            r = math.hypot(ax - ux, ay - uy)
            # circularity check: the two control points must sit near the
            # same circle (splines/ellipses drift)
            r1 = math.hypot(p1.x - ux, p1.y - uy)
            r2 = math.hypot(p2.x - ux, p2.y - uy)
            if abs(r1 - r) > 0.12 * r or abs(r2 - r) > 0.12 * r:
                continue
            if 6 <= r <= 400:   # sane door-swing radii in points at any scale
                radii.append(r)
    return radii


def infer_scale_from_arcs(pdf_path: str, page_index: int,
                          min_hits: int = 4, min_margin: float = 2.0):
    """Infer pts-per-foot by scoring standard scales against door-swing arcs.

    Returns (pts_per_ft, diagnostics) — pts_per_ft is None when no candidate
    wins unambiguously (fewer than `min_hits` door-like arcs, or the best
    candidate doesn't beat the runner-up by `min_margin`x).
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required")
    doc = fitz.open(pdf_path)
    try:
        radii = _arc_radii_pts(doc[page_index])
    finally:
        doc.close()
    if not radii:
        return None, {"arcs": 0}
    scores = {}
    for cand in _STANDARD_SCALES:
        hits = 0
        for r in radii:
            ft = r / cand
            if any(abs(ft - leaf) <= _LEAF_TOL_FT for leaf in _DOOR_LEAF_FT):
                hits += 1
        scores[cand] = hits
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best, second = ranked[0], ranked[1]
    diag = {"arcs": len(radii), "scores": scores}
    if best[1] < min_hits:
        return None, diag
    if second[1] and best[1] < min_margin * second[1]:
        return None, diag  # ambiguous between two scales
    return best[0], diag


def detect_scale_robust(pdf_path: str, page_index: int):
    """Scale via title-block text, falling back to door-arc geometry.

    Returns (pts_per_ft or None, source) where source is 'text' | 'arcs' |
    'none'. This is the M4 entry point — callers must treat None as
    unmeasured (flag/RFI), never substitute a guessed scale.
    """
    s = detect_scale(pdf_path, page_index)
    if s:
        return s, "text"
    s, _diag = infer_scale_from_arcs(pdf_path, page_index)
    if s:
        return s, "arcs"
    return None, "none"


# --------------------------------------------------------------------------
# M2 — wall-RUN length via centerline clustering
# --------------------------------------------------------------------------
def cluster_wall_runs(segments, thickness_pts):
    """Collapse parallel axis-aligned wall lines into one RUN per wall.

    M0's measure_wall_faces sums face length (~2x run) and is inflated by
    walls drawn as multi-line assemblies (two faces + cavity/layer lines) and
    by orthogonal poché fill — each extra parallel line is counted again.
    cluster_wall_runs groups lines whose perpendicular coordinate is within
    `thickness_pts` (one wall's thickness) and unions their span, so a wall's
    2+ parallel lines collapse to a single run measured once.

    `segments`: list of (perp_coord, lo, hi) for one orientation (all H or all
    V), where perp_coord is y for horizontals / x for verticals.
    Returns total run length (same units as inputs).

    LIMITATION (M2): a single perpendicular threshold can't tell "two faces of
    one wall" from "two distinct walls a threshold apart", so dense partition
    layouts can still under-collapse/merge; and it cannot recover partitions
    that aren't on the sheet at all (residential unit interiors live on the
    enlarged typical-unit plans). Pair with unit-partition recovery.
    """
    if not segments:
        return 0.0
    segments = sorted(segments)
    total = 0.0
    band, band_perp = [], None
    for perp, lo, hi in segments:
        if band and perp - band_perp <= thickness_pts:
            band.append((lo, hi))
        else:
            total += _union_intervals(band)
            band = [(lo, hi)]
        band_perp = perp
    total += _union_intervals(band)
    return total


def _union_intervals(ivs):
    if not ivs:
        return 0.0
    ivs = sorted(ivs)
    tot = 0.0
    cs, ce = ivs[0]
    for s, e in ivs[1:]:
        if s <= ce + 0.5:
            ce = max(ce, e)
        else:
            tot += ce - cs
            cs, ce = s, e
    return tot + (ce - cs)


def measure_wall_runs(pdf_path, page_index, layer_prefix=None, pts_per_ft=None,
                      thickness_ft=0.85):
    """Wall RUN linear footage on one sheet via centerline clustering (M2).

    Optionally restrict to layers whose group prefix == layer_prefix (per-floor
    filtering). Returns {wall_run_lf, pts_per_ft}. Wall paint area = run x
    height (Rider's convention; see rider-takeoff-convention).
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required")
    if pts_per_ft is None:
        pts_per_ft = detect_scale(pdf_path, page_index)
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        H, V = [], []
        for path in page.get_drawings():
            layer = path.get("layer") or ""
            if not is_wall_layer(layer):
                continue
            if layer_prefix and not layer.startswith(layer_prefix):
                continue
            for it in path["items"]:
                if it[0] != "l":
                    continue
                a, b = it[1], it[2]
                if abs(a.y - b.y) <= 1.0 and abs(a.x - b.x) > 1.0:
                    H.append((round((a.y + b.y) / 2.0, 1), min(a.x, b.x), max(a.x, b.x)))
                elif abs(a.x - b.x) <= 1.0 and abs(a.y - b.y) > 1.0:
                    V.append((round((a.x + b.x) / 2.0, 1), min(a.y, b.y), max(a.y, b.y)))
        t = thickness_ft * (pts_per_ft or 9.0)
        run_pts = cluster_wall_runs(H, t) + cluster_wall_runs(V, t)
        return {"wall_run_lf": (run_pts / pts_per_ft) if pts_per_ft else None,
                "pts_per_ft": pts_per_ft}
    finally:
        doc.close()


# --------------------------------------------------------------------------
# M5 Tier-2 — geometric wall detection (no layer tags required)
# --------------------------------------------------------------------------
# Flattened-vector exports (Livestock, Honey, most GC-issued sets) carry no
# CAD layer names, and even tagged sets misuse them (CenHud draws real
# partitions on A-WALL-BELW). The reliable definition of a wall is geometric:
# TWO PARALLEL LINES (the faces) at wall-thickness spacing, overlapping for a
# meaningful length. Dimension lines pair at far larger gaps, grid lines don't
# pair at all, door leafs are too short, poché hatch is diagonal, and
# casework is deeper than any wall — the thickness band + overlap length do
# almost all of the filtering.

_WALL_MIN_THICK_FT = 0.28   # 3.5" stud partition (bare)
_WALL_MAX_THICK_FT = 1.15   # 12" CMU + furring
_WALL_MIN_RUN_FT = 1.5      # ignore pairs overlapping less than a door jamb


def _axis_segments(page, min_len_pts=2.0, include_layers=None):
    """All axis-aligned line segments (+ thin filled rects as face pairs).

    Returns (H, V): H = [(y, x0, x1)], V = [(x, y0, y1)]. When
    `include_layers` is not None, only paths whose layer passes the filter
    are used; None means every path (tier-2, layerless).
    """
    H, V = [], []
    for path in page.get_drawings():
        if include_layers is not None and not include_layers(path.get("layer") or ""):
            continue
        for it in path["items"]:
            if it[0] == "l":
                a, b = it[1], it[2]
                if abs(a.y - b.y) <= 1.0 and abs(a.x - b.x) > min_len_pts:
                    H.append((round((a.y + b.y) / 2.0, 1),
                              min(a.x, b.x), max(a.x, b.x)))
                elif abs(a.x - b.x) <= 1.0 and abs(a.y - b.y) > min_len_pts:
                    V.append((round((a.x + b.x) / 2.0, 1),
                              min(a.y, b.y), max(a.y, b.y)))
            elif it[0] == "re":
                r = it[1]
                # a thin filled rectangle IS a wall chunk: emit both faces
                if r.width > min_len_pts and r.height <= 12:
                    H.append((round(r.y0, 1), r.x0, r.x1))
                    H.append((round(r.y1, 1), r.x0, r.x1))
                elif r.height > min_len_pts and r.width <= 12:
                    V.append((round(r.x0, 1), r.y0, r.y1))
                    V.append((round(r.x1, 1), r.y0, r.y1))
    return H, V


def _pair_centerlines(segments, min_gap_pts, max_gap_pts, min_overlap_pts):
    """Wall centerline intervals from parallel face pairs (one orientation).

    For each pair of segments whose perpendicular gap is inside the wall
    thickness band and whose spans overlap >= min_overlap_pts, emit
    (centerline_coord, overlap_lo, overlap_hi). Multi-line wall assemblies
    (face+face+cavity lines) produce several nearby centerlines for the same
    wall — the caller collapses them with cluster_wall_runs, so each wall is
    counted once.
    """
    out = []
    by_coord = {}
    for perp, lo, hi in segments:
        by_coord.setdefault(perp, []).append((lo, hi))
    coords = sorted(by_coord)
    for i, c1 in enumerate(coords):
        for j in range(i + 1, len(coords)):
            c2 = coords[j]
            gap = c2 - c1
            if gap > max_gap_pts:
                break
            if gap < min_gap_pts:
                continue
            for (a1, b1) in by_coord[c1]:
                for (a2, b2) in by_coord[c2]:
                    lo, hi = max(a1, a2), min(b1, b2)
                    if hi - lo >= min_overlap_pts:
                        out.append((round((c1 + c2) / 2.0, 1), lo, hi))
    return out


def measure_wall_runs_geometric(pdf_path, page_index, pts_per_ft=None,
                                include_layers=None):
    """Tier-2 wall RUN measurement: parallel-pair detection, no layers needed.

    Returns {wall_run_lf, n_face_segments, n_pair_intervals, pts_per_ft,
    scale_source}. wall_run_lf is None when no scale can be established —
    callers must treat that as unmeasured (RFI), never guess.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required")
    scale_source = "given"
    if pts_per_ft is None:
        pts_per_ft, scale_source = detect_scale_robust(pdf_path, page_index)
    if not pts_per_ft:
        return {"wall_run_lf": None, "pts_per_ft": None,
                "scale_source": "none"}
    doc = fitz.open(pdf_path)
    try:
        H, V = _axis_segments(doc[page_index], include_layers=include_layers)
    finally:
        doc.close()
    min_gap = _WALL_MIN_THICK_FT * pts_per_ft
    max_gap = _WALL_MAX_THICK_FT * pts_per_ft
    min_ov = _WALL_MIN_RUN_FT * pts_per_ft
    ch = _pair_centerlines(H, min_gap, max_gap, min_ov)
    cv = _pair_centerlines(V, min_gap, max_gap, min_ov)
    # Collapse the multiple centerlines of one wall assembly into single runs.
    band = max_gap  # centerlines of one wall sit within its thickness
    run_pts = cluster_wall_runs(ch, band) + cluster_wall_runs(cv, band)
    return {"wall_run_lf": run_pts / pts_per_ft,
            "n_face_segments": len(H) + len(V),
            "n_pair_intervals": len(ch) + len(cv),
            "pts_per_ft": pts_per_ft,
            "scale_source": scale_source}


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------
def union_length(intervals) -> float:
    """Total covered length of 1-D intervals, merging overlaps/duplicates.

    This is what turns "a face line drawn 5x as overlapping sub-segments"
    into a single covered length, removing the duplicate-line overcount.
    """
    if not intervals:
        return 0.0
    intervals = sorted(intervals)
    total = 0.0
    cur_s, cur_e = intervals[0]
    for s, e in intervals[1:]:
        if s <= cur_e + 0.5:        # touch/overlap (0.5pt slop) -> merge
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s
            cur_s, cur_e = s, e
    total += cur_e - cur_s
    return total


def measure_wall_faces(pdf_path: str, page_index: int, pts_per_ft=None,
                       axis_tol: float = 1.0, min_seg_pts: float = 0.5) -> dict:
    """Measure paintable wall-face linear footage on one vector PDF page.

    Returns a dict:
        wall_face_lf          : float | None  (None when scale unknown)
        wall_face_pts         : float          (scale-independent raw length)
        pts_per_ft            : float | None
        by_layer              : {short_layer_name: raw_pts}
        dropped_diagonal_pts  : float          (poché/hatch removed)
        n_h, n_v, n_diag      : int            (segment-orientation counts)
        has_layer_attribution : bool           (False => needs M5 classifier)
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required for measure_wall_faces")
    if pts_per_ft is None:
        pts_per_ft = detect_scale(pdf_path, page_index)

    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        h_buckets: dict = {}   # y -> [(x0,x1), ...]
        v_buckets: dict = {}   # x -> [(y0,y1), ...]
        by_layer: dict = {}
        diag_pts = 0.0
        n_h = n_v = n_diag = 0
        saw_any_layer = False

        for path in page.get_drawings():
            layer = path.get("layer") or ""
            if layer:
                saw_any_layer = True
            if not is_wall_layer(layer):
                continue
            short = layer.split("|")[-1]
            for it in path["items"]:
                if it[0] == "l":
                    a, b = it[1], it[2]
                    dx, dy = b.x - a.x, b.y - a.y
                    seg = math.hypot(dx, dy)
                    if seg < min_seg_pts:
                        continue
                    if abs(dy) <= axis_tol:               # horizontal face
                        key = round((a.y + b.y) / 2.0)
                        h_buckets.setdefault(key, []).append(
                            (min(a.x, b.x), max(a.x, b.x)))
                        n_h += 1
                        by_layer[short] = by_layer.get(short, 0.0) + seg
                    elif abs(dx) <= axis_tol:             # vertical face
                        key = round((a.x + b.x) / 2.0)
                        v_buckets.setdefault(key, []).append(
                            (min(a.y, b.y), max(a.y, b.y)))
                        n_v += 1
                        by_layer[short] = by_layer.get(short, 0.0) + seg
                    else:                                 # diagonal poché/hatch
                        diag_pts += seg
                        n_diag += 1
                elif it[0] == "re":                       # rectangle -> 4 faces
                    r = it[1]
                    h_buckets.setdefault(round(r.y0), []).append((r.x0, r.x1))
                    h_buckets.setdefault(round(r.y1), []).append((r.x0, r.x1))
                    v_buckets.setdefault(round(r.x0), []).append((r.y0, r.y1))
                    v_buckets.setdefault(round(r.x1), []).append((r.y0, r.y1))

        union_pts = (sum(union_length(iv) for iv in h_buckets.values())
                     + sum(union_length(iv) for iv in v_buckets.values()))
        lf = (union_pts / pts_per_ft) if pts_per_ft else None
        return {
            "wall_face_pts": union_pts,
            "wall_face_lf": lf,
            "pts_per_ft": pts_per_ft,
            "by_layer": by_layer,
            "dropped_diagonal_pts": diag_pts,
            "n_h": n_h, "n_v": n_v, "n_diag": n_diag,
            "has_layer_attribution": saw_any_layer,
        }
    finally:
        doc.close()


def wall_area_sqft(pdf_path: str, page_index: int, height_ft: float,
                   pts_per_ft=None):
    """Paintable wall area (both faces) for one sheet = wall-face LF x height."""
    m = measure_wall_faces(pdf_path, page_index, pts_per_ft=pts_per_ft)
    if m["wall_face_lf"] is None:
        return None
    return m["wall_face_lf"] * height_ft


def cluster_wall_runs_list(segments, thickness_pts):
    """Like cluster_wall_runs but returns the merged run intervals
    [(perp, lo, hi)] instead of only the total length — callers that need
    per-run positions (room-scope assignment) use this."""
    if not segments:
        return []
    segments = sorted(segments)
    out = []

    def flush(band, perp):
        if not band:
            return
        ivs = sorted(band)
        cs, ce = ivs[0]
        for s, e in ivs[1:]:
            if s <= ce + 0.5:
                ce = max(ce, e)
            else:
                out.append((perp, cs, ce))
                cs, ce = s, e
        out.append((perp, cs, ce))

    band, band_perp = [], None
    for perp, lo, hi in segments:
        if band and perp - band_perp <= thickness_pts:
            band.append((lo, hi))
        else:
            flush(band, band_perp)
            band = [(lo, hi)]
        band_perp = perp
    flush(band, band_perp)
    return out


def wall_runs_with_positions(pdf_path, page_index, pts_per_ft):
    """Tier-2 wall runs as positioned intervals: (orient, perp, lo, hi).
    orient 'H': perp=y, span on x; 'V': perp=x, span on y. Same pairing and
    clustering as measure_wall_runs_geometric."""
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required")
    doc = fitz.open(pdf_path)
    try:
        H, V = _axis_segments(doc[page_index])
    finally:
        doc.close()
    min_gap = _WALL_MIN_THICK_FT * pts_per_ft
    max_gap = _WALL_MAX_THICK_FT * pts_per_ft
    min_ov = _WALL_MIN_RUN_FT * pts_per_ft
    ch = _pair_centerlines(H, min_gap, max_gap, min_ov)
    cv = _pair_centerlines(V, min_gap, max_gap, min_ov)
    runs = [("H", p, lo, hi) for (p, lo, hi) in cluster_wall_runs_list(ch, max_gap)]
    runs += [("V", p, lo, hi) for (p, lo, hi) in cluster_wall_runs_list(cv, max_gap)]
    return runs
