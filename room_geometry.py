"""
Room-level geometric measurement — SHADOW stage (calibration, not pricing).

Targets the independent-measurement gaps the Harlem Valley blind test
quantified (2026-08-11, JW takeoff as held-out truth, annotations
stripped): LLM-guessed room areas (ceilings −33..−64%, concrete −37%),
run-vs-painted-face wall convention (−21%), shared-door double counts
(+10..34%).

Method: the page's wall-weight linework (auto lineweight filter — the same
one tier-2 uses to drop dimension ink) is rasterized; door gaps are sealed
by morphological closing; the outside is flood-filled away; every enclosed
interior region is measured, and room-label anchors attribute regions to
rooms where they land. Wall-run sides are probed against interior space as
paint-face candidates, and curve paths are circle-fitted as door-swing
candidates.

CERTIFICATION STATUS (why shadow): on the JW sheet, cleanly-attributed
rooms measure within ±2–9% of the estimator's polygons (Vehicle Shop −3%,
Water Treatment −2%, Storage +2%), but label anchors frequently sit in
adjacent spaces, non-room enclosures (legend boxes, shafts, covered entry)
inflate anchor-free totals ~+55%, this sheet's door symbols draw ~30° arcs
(not the quarter-sweep the detector expects), and face billing needs
painted-scope semantics geometry alone cannot see. Exactly the VME
playbook: run as shadow on every job, score against verified takeoffs,
promote per-quantity when a metric holds across the corpus. Never prices.

Pure geometry — no LLM. Callers must pass annotation-stripped PDFs (see
vme_attribution._measurement_copies) so markup ink cannot leak in.
"""

import math

import numpy as np

try:
    import fitz
except ImportError:  # pragma: no cover
    fitz = None

import vector_measure as vm

_PX_PER_FT = 6.0        # raster resolution (px per drawing-foot)
_CLOSE_FT = 0.5         # small closing radius — welds corner joints only;
                        # door gaps are sealed by explicit bridging below
_BRIDGE_MAX_FT = 6.0    # bridge collinear wall-segment gaps up to a double
                        # door; wider openings are real open-plan connections
_AREA_CORR_PX = 2       # dilate filled region: measure to the wall face,
                        # not to the inner edge of the drawn line
_PLUG_PX = 2            # erode this much less than the dilation: a plug
                        # sealing a door gap near 2x the closing radius is a
                        # thin neck that symmetric erosion would dissolve
_LEAK_MAX_SF = 6500.0   # a single region larger than this is suspect
_MIN_REGION_SF = 25.0   # smaller enclosures are poché/fixtures
_SNAP_FT = 6.0          # anchor snap radius to the nearest free cell

_FACE_PROBE_FT = 1.6
_FACE_STEP_FT = 1.0
_FACE_MIN_HIT = 0.35

_DOOR_R_MIN_FT = 1.8    # door-leaf swing radius band
_DOOR_R_MAX_FT = 5.0
_DOOR_FIT_RESID_PT = 1.5


def _binary_dilate(mask, r):
    """Square dilation radius r via iterated 4-neighbour max (bool grid)."""
    out = mask.copy()
    for _ in range(r):
        nxt = out.copy()
        nxt[1:, :] |= out[:-1, :]
        nxt[:-1, :] |= out[1:, :]
        nxt[:, 1:] |= out[:, :-1]
        nxt[:, :-1] |= out[:, 1:]
        out = nxt
    return out


def _binary_erode(mask, r):
    return ~_binary_dilate(~mask, r)


def _flood(free, label, start, region_id):
    """Frontier-vectorized BFS fill on `free`, writing region_id to label."""
    frontier = np.zeros_like(free)
    frontier[start] = True
    frontier &= free
    filled = frontier.copy()
    while frontier.any():
        grown = np.zeros_like(frontier)
        grown[1:, :] |= frontier[:-1, :]
        grown[:-1, :] |= frontier[1:, :]
        grown[:, 1:] |= frontier[:, :-1]
        grown[:, :-1] |= frontier[:, 1:]
        frontier = grown & free & ~filled
        filled |= frontier
    label[filled] = region_id
    return filled


def _bridge_gaps(segs, pts_per_ft, max_gap_ft=_BRIDGE_MAX_FT,
                 bucket_pts=3.0):
    """Bridge door-sized gaps between collinear wall segments.

    segs: [(perp, lo, hi)]. Segments whose perpendicular coordinate falls
    in the same bucket are sorted along the run; successive intervals with
    a gap of at most max_gap_ft get a bridging segment. Deterministic door
    sealing that (unlike morphological closing, whose erosion dissolves a
    plug once the gap nears 2x the radius) preserves room area exactly.
    """
    from collections import defaultdict
    buckets = defaultdict(list)
    for perp, lo, hi in segs:
        buckets[round(perp / bucket_pts)].append((perp, lo, hi))
    bridges = []
    max_gap = max_gap_ft * pts_per_ft
    for items in buckets.values():
        items.sort(key=lambda t: t[1])
        for a, b in zip(items, items[1:]):
            gap = b[1] - a[2]
            if 0 < gap <= max_gap:
                bridges.append(((a[0] + b[0]) / 2.0, a[2], b[1]))
    return bridges


def build_enclosure_grid(pdf_path, page_index, pts_per_ft,
                         px_per_ft=_PX_PER_FT, close_ft=_CLOSE_FT):
    """Rasterized wall-weight linework -> (free, interior, outside, meta).

    free     = not-wall after closing (door gaps sealed)
    outside  = free space connected to the page border
    interior = free & ~outside  (enclosed space: rooms + shafts + boxes)
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) required")
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    H, V = vm._axis_segments(page, min_width="auto")
    rect = page.rect
    doc.close()
    H = list(H) + _bridge_gaps(H, pts_per_ft)
    V = list(V) + _bridge_gaps(V, pts_per_ft)
    px_per_pt = px_per_ft / pts_per_ft
    w = int(rect.width * px_per_pt) + 1
    h = int(rect.height * px_per_pt) + 1
    mask = np.zeros((h, w), dtype=bool)
    for y, x0, x1 in H:
        yy = int(round(y * px_per_pt))
        if 0 <= yy < h:
            mask[yy, max(0, int(x0 * px_per_pt)):
                 min(w, int(x1 * px_per_pt) + 1)] = True
    for x, y0, y1 in V:
        xx = int(round(x * px_per_pt))
        if 0 <= xx < w:
            mask[max(0, int(y0 * px_per_pt)):
                 min(h, int(y1 * px_per_pt) + 1), xx] = True
    r = max(1, int(round(close_ft * px_per_ft)))
    closed = _binary_erode(_binary_dilate(mask, r), max(1, r - _PLUG_PX))
    free = ~closed
    label = np.zeros((h, w), dtype=np.int32)
    outside = np.zeros_like(free)
    for seed in ((0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)):
        if free[seed] and not outside[seed]:
            outside |= _flood(free, label, seed, -1)
    label[:] = 0
    return free, free & ~outside, outside, {
        "px_per_pt": px_per_pt, "px_per_ft": px_per_ft,
        "n_segments": len(H) + len(V), "shape": (h, w)}


# v2 measurement (NIGHTSHIFT_ROOM_GEOMETRY_V2, read by the shadow builder
# in Takeoff_DIRECT): the round-5 shadow audit (2026-09-04) showed the two
# dominant loss modes are page-wide enclosure failure (interior fraction
# ~0 — door openings are 3 ft, the 0.5 ft closing radius seals 1 ft, so
# the floor leaks to the page border and EVERY anchor reads on_wall:
# fishkill p6 29/29, hudson p8 21/21) and merged components (door openings
# connect rooms; only the first owner got an area: homewood 102 rooms).
_ADAPTIVE_CLOSE_LADDER = (1.0, 2.0, 3.5)   # ft, tried in order
_ADAPTIVE_MIN_INTERIOR = 0.05              # of page area


def _render_enclosure_grid(pdf_path, page_index, pts_per_ft,
                           px_per_ft=_PX_PER_FT):
    """Enclosure grid from a rasterized RENDER of the page instead of
    axis-segment linework. Fallback for drawing styles the segment
    extractor cannot see (walls as polylines/fills/curves — fishkill p6
    reads 29/29 on_wall with only 2k axis segments). Any dark-enough
    pixel is wall; the same border-flood then separates outside from
    enclosed interior."""
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) required")
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        px_per_pt = px_per_ft / pts_per_ft
        mat = fitz.Matrix(px_per_pt, px_per_pt)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csGRAY,
                              alpha=False)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width)
    finally:
        doc.close()
    mask = arr < 128          # dark ink = wall-ish
    r = max(1, int(round(_CLOSE_FT * px_per_ft)))
    closed = _binary_erode(_binary_dilate(mask, r), max(1, r - _PLUG_PX))
    free = ~closed
    h, w = free.shape
    label = np.zeros((h, w), dtype=np.int32)
    outside = np.zeros_like(free)
    for seed in ((0, 0), (0, w - 1), (h - 1, 0), (h - 1, w - 1)):
        if free[seed] and not outside[seed]:
            outside |= _flood(free, label, seed, -1)
    return free, free & ~outside, outside, {
        "px_per_pt": px_per_pt, "px_per_ft": px_per_ft,
        "n_segments": -1, "shape": (h, w), "source": "render"}


def _adaptive_enclosure(pdf_path, page_index, pts_per_ft, px_per_ft,
                        close_ft):
    """build_enclosure_grid, escalating the closing radius while the
    interior fraction is degenerate; falls back to a page-render mask
    when segment linework never encloses. Returns (free, interior,
    outside, meta) of the best attempt; meta gains close_ft_used /
    interior_frac / source."""
    best = None
    for cf in (close_ft,) + _ADAPTIVE_CLOSE_LADDER:
        free, interior, outside, meta = build_enclosure_grid(
            pdf_path, page_index, pts_per_ft, px_per_ft, cf)
        frac = float(interior.sum()) / max(1, interior.size)
        meta["close_ft_used"] = cf
        meta["interior_frac"] = round(frac, 4)
        meta.setdefault("source", "segments")
        if best is None or frac > best[4]:
            best = (free, interior, outside, meta, frac)
        if frac >= _ADAPTIVE_MIN_INTERIOR:
            break
    if best[4] < _ADAPTIVE_MIN_INTERIOR:
        try:
            free, interior, outside, meta = _render_enclosure_grid(
                pdf_path, page_index, pts_per_ft, px_per_ft)
            frac = float(interior.sum()) / max(1, interior.size)
            meta["close_ft_used"] = _CLOSE_FT
            meta["interior_frac"] = round(frac, 4)
            if frac > best[4]:
                best = (free, interior, outside, meta, frac)
        except Exception:
            pass
    return best[0], best[1], best[2], best[3]


def _split_merged_component(label, rid, group_px, px_per_ft):
    """Partition one merged flood component among its anchors by nearest
    anchor (in pixels). group_px: [(name, (cy, cx))]. Returns
    {name: area_sqft}."""
    ys, xs = np.nonzero(label == rid)
    if ys.size == 0 or not group_px:
        return {}
    names = [n for n, _ in group_px]
    d = np.stack([(ys - cy) ** 2 + (xs - cx) ** 2
                  for _, (cy, cx) in group_px])
    nearest = np.argmin(d, axis=0)
    counts = np.bincount(nearest, minlength=len(names))
    return {names[i]: float(counts[i]) / (px_per_ft ** 2)
            for i in range(len(names))}


def measure_room_areas(pdf_path, page_index, anchors, pts_per_ft=None,
                       px_per_ft=_PX_PER_FT, close_ft=_CLOSE_FT,
                       adaptive_close=False, split_merged=False):
    """Flood-filled room areas from label anchors (name, x_pt, y_pt).

    Per room: {area_sqft|None, status} with status one of
    measured | merged:<first-owner> | leaked | on_wall | no_scale.
    Merged rooms share one region — the region's area is reported on the
    FIRST owner and None on joiners, with the group recorded, so aggregate
    consumers can sum groups without double counting.

    v2 options (both default OFF so v1 callers are byte-identical):
      adaptive_close: escalate the closing radius when the page's interior
        fraction is degenerate (see _ADAPTIVE_CLOSE_LADDER).
      split_merged: partition merged components among their anchors by
        nearest-anchor distance; every group member reports its share as
        status 'measured' (basis recorded as 'split').
    """
    if pts_per_ft is None:
        try:
            pts_per_ft, _src = vm.detect_scale_robust(pdf_path, page_index)
        except Exception:
            pts_per_ft = None
    if not pts_per_ft:
        return {"rooms": {n: {"area_sqft": None, "status": "no_scale"}
                          for n, _, _ in anchors},
                "pts_per_ft": None, "measured_n": 0, "groups": []}
    if adaptive_close:
        free, interior, _outside, meta = _adaptive_enclosure(
            pdf_path, page_index, pts_per_ft, px_per_ft, close_ft)
    else:
        free, interior, _outside, meta = build_enclosure_grid(
            pdf_path, page_index, pts_per_ft, px_per_ft, close_ft)
    px_per_pt = meta["px_per_pt"]
    h, w = meta["shape"]
    label = np.zeros((h, w), dtype=np.int32)
    out, owners, groups = {}, {}, {}
    anchor_px = {}   # component id -> [(name, (cy, cx))] for merged splits
    snap = int(_SNAP_FT * px_per_ft)
    for idx, (name, xpt, ypt) in enumerate(anchors, start=1):
        cy = min(max(int(ypt * px_per_pt), 0), h - 1)
        cx = min(max(int(xpt * px_per_pt), 0), w - 1)
        if not interior[cy, cx]:
            found = None
            for rad in range(1, snap + 1):
                y0, x0 = max(0, cy - rad), max(0, cx - rad)
                sub = interior[y0:min(h, cy + rad + 1),
                               x0:min(w, cx + rad + 1)]
                if sub.any():
                    ys, xs = np.nonzero(sub)
                    k = int(np.argmin((ys + y0 - cy) ** 2
                                      + (xs + x0 - cx) ** 2))
                    found = (ys[k] + y0, xs[k] + x0)
                    break
            if found is None:
                out[name] = {"area_sqft": None, "status": "on_wall"}
                continue
            cy, cx = found
        rid = int(label[cy, cx])
        if rid:
            owner = owners.get(rid)
            out[name] = {"area_sqft": None, "status": f"merged:{owner}"}
            groups.setdefault(rid, [owner]).append(name)
            anchor_px.setdefault(rid, []).append((name, (cy, cx)))
            continue
        filled = _flood(interior, label, (cy, cx), idx)
        area = float(_binary_dilate(filled, _AREA_CORR_PX).sum()) \
            / (px_per_ft ** 2)
        owners[idx] = name
        anchor_px.setdefault(idx, []).append((name, (cy, cx)))
        if area > _LEAK_MAX_SF:
            out[name] = {"area_sqft": None, "status": "leaked"}
        else:
            out[name] = {"area_sqft": round(area, 1), "status": "measured"}

    if split_merged and groups:
        for rid, members in groups.items():
            owner = members[0]
            owner_rec = out.get(owner) or {}
            # A leaked component with a SINGLE anchor is untrusted (one
            # label claiming a whole floor). A leaked component with
            # several anchors is the opposite: the floor flooded as one
            # blob because door openings connect the rooms, and the
            # anchors are exactly the information that partitions it
            # (homewood: 20+ rooms in one component). Split, then
            # leak-check each share individually.
            if owner_rec.get("status") == "leaked" and len(members) < 3:
                continue
            # Template-instance rosters give every sibling the SAME label
            # bbox (homewood: 19 of 21 anchors at one point) — identical
            # anchors are not separable, so only distinct-coordinate
            # anchors participate; duplicates stay merged (their real fix
            # is a template-unit basis, not a fake partition).
            distinct, seen_px = [], set()
            for name, (cy, cx) in anchor_px.get(rid) or []:
                if (cy, cx) in seen_px:
                    continue
                seen_px.add((cy, cx))
                distinct.append((name, (cy, cx)))
            if len(distinct) < 2:
                continue
            shares = _split_merged_component(
                label, rid, distinct, px_per_ft)
            for member, _pt in distinct:
                share = shares.get(member)
                if share and 0 < share <= _LEAK_MAX_SF:
                    out[member] = {"area_sqft": round(share, 1),
                                   "status": "measured", "basis": "split"}

    return {"rooms": out, "pts_per_ft": pts_per_ft,
            "measured_n": sum(1 for v in out.values()
                              if v["status"] == "measured"),
            "groups": [v for v in groups.values()],
            "_label": label, "_interior": interior, "_meta": meta}


def total_enclosed_area(pdf_path, page_index, pts_per_ft=None,
                        px_per_ft=_PX_PER_FT, close_ft=_CLOSE_FT):
    """Sum of ALL enclosed interior regions in the plausible-room band.

    Anchor-free upper bound on room area — includes shafts, legend boxes
    and unpainted enclosures (JW sheet: +55% over the painted-room truth),
    which is why it is a shadow metric, not a price.
    """
    if pts_per_ft is None:
        pts_per_ft, _src = vm.detect_scale_robust(pdf_path, page_index)
    if not pts_per_ft:
        return {"total_sqft": None, "n_regions": 0}
    _free, interior, _outside, meta = build_enclosure_grid(
        pdf_path, page_index, pts_per_ft, px_per_ft, close_ft)
    label = np.zeros(meta["shape"], dtype=np.int32)
    remaining = interior.copy()
    total, n, rid = 0.0, 0, 0
    while remaining.any():
        ys, xs = np.nonzero(remaining)
        rid += 1
        filled = _flood(remaining, label, (ys[0], xs[0]), rid)
        remaining &= ~filled
        area = float(_binary_dilate(filled, _AREA_CORR_PX).sum()) \
            / (px_per_ft ** 2)
        if _MIN_REGION_SF <= area <= _LEAK_MAX_SF:
            total += area
            n += 1
    return {"total_sqft": round(total, 0), "n_regions": n}


def measure_face_candidates(pdf_path, page_index, pts_per_ft=None,
                            px_per_ft=_PX_PER_FT, close_ft=_CLOSE_FT):
    """Wall-run sides adjacent to enclosed interior space (face candidates).

    Interior adjacency alone over-bills (unpainted shafts/closets read as
    faces — JW sheet: 2,497 candidate LF vs 1,740 billed) — a candidate
    face still needs painted-scope semantics. Shadow metric.
    """
    if pts_per_ft is None:
        pts_per_ft, _src = vm.detect_scale_robust(pdf_path, page_index)
    if not pts_per_ft:
        return {"face_candidate_lf": None, "run_lf": None}
    _free, interior, _outside, meta = build_enclosure_grid(
        pdf_path, page_index, pts_per_ft, px_per_ft, close_ft)
    px_per_pt = meta["px_per_pt"]
    h, w = meta["shape"]
    runs = vm.wall_runs_with_positions(pdf_path, page_index, pts_per_ft)
    probe = (0.35 + _FACE_PROBE_FT) * px_per_ft
    step = _FACE_STEP_FT * pts_per_ft
    face_lf = run_lf = 0.0
    by_faces = {0: 0.0, 1: 0.0, 2: 0.0}
    for orient, perp, lo, hi in runs:
        length_ft = (hi - lo) / pts_per_ft
        run_lf += length_ft
        n = max(2, int((hi - lo) / step))
        ts = np.linspace(lo + 0.5 * step, hi - 0.5 * step, n)
        hits = [0.0, 0.0]
        for si, sign in enumerate((-1.0, 1.0)):
            if orient == "H":
                ys = np.full(n, int(perp * px_per_pt + sign * probe))
                xs = (ts * px_per_pt).astype(int)
            else:
                xs = np.full(n, int(perp * px_per_pt + sign * probe))
                ys = (ts * px_per_pt).astype(int)
            ok = (ys >= 0) & (ys < h) & (xs >= 0) & (xs < w)
            if ok.any():
                hits[si] = float(interior[ys[ok], xs[ok]].mean())
        faces = sum(1 for hf in hits if hf >= _FACE_MIN_HIT)
        by_faces[faces] += length_ft
        face_lf += faces * length_ft
    return {"face_candidate_lf": round(face_lf, 1),
            "run_lf": round(run_lf, 1),
            "by_faces": {k: round(v, 1) for k, v in by_faces.items()}}


def _fit_circle(pts):
    """Least-squares circle fit -> (cx, cy, r, mean_abs_residual)."""
    A = np.c_[2 * pts[:, 0], 2 * pts[:, 1], np.ones(len(pts))]
    b = (pts ** 2).sum(axis=1)
    try:
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return None
    cx, cy, c = sol
    r = math.sqrt(max(0.0, c + cx * cx + cy * cy))
    resid = float(np.abs(
        np.hypot(pts[:, 0] - cx, pts[:, 1] - cy) - r).mean())
    return cx, cy, r, resid


def door_swing_stats(pdf_path, page_index, pts_per_ft=None):
    """Door-swing candidates: curve paths circle-fitted at leaf radius.

    Returns {quarter_sweeps, arc_candidates, span_histogram, pts_per_ft}.
    quarter_sweeps counts ~90° sweeps (the classic symbol); span_histogram
    exposes the drawing's actual arc style (JW sheet: ~45 arcs at ~30°) so
    a per-corpus door count can be calibrated before this ever prices.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) required")
    if pts_per_ft is None:
        pts_per_ft, _src = vm.detect_scale_robust(pdf_path, page_index)
    if not pts_per_ft:
        return {"quarter_sweeps": None, "arc_candidates": 0,
                "span_histogram": {}, "pts_per_ft": None}
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    quarter, cands, hist = 0, 0, {}
    for path in page.get_drawings():
        pts = []
        for it in path["items"]:
            if it[0] == "c":
                p0, p1, p2, p3 = it[1], it[2], it[3], it[4]
                for t in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
                    mt = 1.0 - t
                    pts.append((
                        mt**3*p0.x + 3*mt*mt*t*p1.x + 3*mt*t*t*p2.x + t**3*p3.x,
                        mt**3*p0.y + 3*mt*mt*t*p1.y + 3*mt*t*t*p2.y + t**3*p3.y))
        if len(pts) < 6:
            continue
        fit = _fit_circle(np.array(pts))
        if fit is None:
            continue
        cx, cy, r, resid = fit
        r_ft = r / pts_per_ft
        if resid > _DOOR_FIT_RESID_PT or not (
                _DOOR_R_MIN_FT <= r_ft <= _DOOR_R_MAX_FT):
            continue
        arr = np.array(pts)
        angs = np.degrees(np.arctan2(arr[:, 1] - cy, arr[:, 0] - cx))
        span = float(angs.max() - angs.min())
        if span > 180:
            span = 360 - span
        if span < 8:
            continue
        cands += 1
        bucket = int(span // 15) * 15
        hist[bucket] = hist.get(bucket, 0) + 1
        if 55 <= span <= 130:
            quarter += 1
    doc.close()
    return {"quarter_sweeps": quarter, "arc_candidates": cands,
            "span_histogram": hist, "pts_per_ft": pts_per_ft}


def compute_room_geometry_shadow(pdf_paths, anchors_by_page=None):
    """Full shadow record for one job (all floor-plan pages of all PDFs).

    anchors_by_page: {(pdf_path, page_idx0): [(name, x_pt, y_pt), ...]}
    Returns a JSON-safe dict for analysis['_room_geometry_shadow'].
    Never raises: a page that fails contributes an 'error' entry.
    """
    pages = []
    try:
        from vme_attribution import select_floor_plan_pages
        sel = select_floor_plan_pages(pdf_paths)
    except Exception:
        sel = [{"pdf": p, "page": 0} for p in (pdf_paths or [])]
    for pg in sel or []:
        pdf, idx = pg["pdf"], pg["page"]
        rec = {"pdf": pdf.rsplit("/", 1)[-1], "page": idx + 1}
        try:
            pts_per_ft, _src = vm.detect_scale_robust(pdf, idx)
            if not pts_per_ft:
                rec["error"] = "no scale"
                pages.append(rec)
                continue
            rec["enclosed"] = total_enclosed_area(
                pdf, idx, pts_per_ft=pts_per_ft)
            rec["faces"] = measure_face_candidates(
                pdf, idx, pts_per_ft=pts_per_ft)
            rec["doors"] = door_swing_stats(pdf, idx, pts_per_ft=pts_per_ft)
            anchors = (anchors_by_page or {}).get((pdf, idx)) or []
            if anchors:
                m = measure_room_areas(pdf, idx, anchors,
                                       pts_per_ft=pts_per_ft,
                                       adaptive_close=_v2_enabled(),
                                       split_merged=_v2_enabled())
                rec["rooms"] = {
                    n: {k: v[k] for k in ("area_sqft", "status", "basis")
                        if k in v}
                    for n, v in m["rooms"].items()}
                rec["rooms_measured"] = m["measured_n"]
        except Exception as exc:  # pragma: no cover
            rec["error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
        pages.append(rec)
    return {"engine": ("room-geometry-shadow-v2" if _v2_enabled()
                       else "room-geometry-shadow-v1"),
            "pages": pages}


def _v2_enabled():
    """NIGHTSHIFT_ROOM_GEOMETRY_V2 (default off): adaptive enclosure +
    merged-component splitting. Read at call time; v1 output is
    byte-identical with the flag off."""
    import os
    return os.environ.get(
        "NIGHTSHIFT_ROOM_GEOMETRY_V2", "0").strip() in ("1", "true", "True")
