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
_WALL_NEG = ("anno", "iden", "patt", "hatch", "-blow", "below", "-lite")


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
