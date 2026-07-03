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


def compute_vme_shadow(pdf_path):
    """M3 (shadow) — run the vector measurement engine on a plan and return a
    summary for COMPARISON alongside the pipeline. Does not replace anything.

    Sums centerline wall RUN length over the canonical per-floor source pages
    (M1 selection, so the all-floors composite isn't double-counted). Robust:
    returns None on any failure (this is a shadow, never load-bearing).

    Returns {total_wall_run_lf, n_floor_pages, primary_scale, by_page:[...]}.
    """
    if fitz is None:
        return None
    try:
        pages, primary = classify_pages(pdf_path)
        if not pages:
            return None
        sources = {}
        for p in select_floor_sources(pages).values():
            sources[id(p)] = p           # unique source pages, composite excluded
        total = 0.0
        by_page = []
        for p in sources.values():
            sc = p["scale"] or primary
            try:
                r = vm.measure_wall_runs(pdf_path, p["page"], pts_per_ft=sc)
            except Exception:
                continue
            lf = r.get("wall_run_lf") or 0.0
            total += lf
            by_page.append({"page": p["page"] + 1, "scale": sc, "wall_run_lf": round(lf, 1)})
        if not by_page:
            return None
        return {"total_wall_run_lf": round(total, 1),
                "n_floor_pages": len(by_page),
                "primary_scale": primary,
                "by_page": by_page}
    except Exception:
        return None


def recover_floor_wall_runs(demising_run_lf, unit_type_runs, unit_counts):
    """M2 part 2 — residential floor wall RUN with unit-partition recovery.

    A residential floor's walls = demising/corridor walls (drawn on the floor
    OVERVIEW) + each apartment's interior partitions, which are drawn ONCE on
    the enlarged typical-unit plans and repeated N times on the floor. So:

        floor_run = demising_run + sum_t(unit_type_run[t] * count_on_floor[t])

    `unit_type_runs`: {unit_type: interior-partition run LF} measured from the
    enlarged plans (centerline). `unit_counts`: {unit_type: instances on this
    floor} — from the unit schedule the pipeline already extracts ("Units
    201-206" -> 6), NOT fragile geometry detection. This is exactly Rider's
    typical-unit x count takeoff, made deterministic.
    """
    unit_total = sum(unit_type_runs.get(t, 0.0) * n for t, n in (unit_counts or {}).items())
    return float(demising_run_lf) + unit_total


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


# ---------------------------------------------------------------------------
# M4/M5 — TITLE-based page classification (no layer tags required)
# ---------------------------------------------------------------------------
# Sheet titles are the LARGEST text on a page. Classify each page from its
# top-font lines (plus the filename for sheet-per-file sets): floor-plan
# sheets are identified and each claims the floor(s) it names — one sheet may
# legitimately carry several floors ("FLOOR PLANS", "2nd and 3rd Floor
# Plans"). Reading titles is reliable; nothing here measures.

import re as _re

_FLOOR_WORDS = (
    (r"basement|cellar", "basement"),
    (r"ground", "ground"),
    (r"first|1st|level\s*(?:1|one)\b", "1"),
    (r"second|2nd|level\s*(?:2|two)\b", "2"),
    (r"third|3rd|level\s*(?:3|three)\b", "3"),
    (r"fourth|4th|level\s*(?:4|four)\b", "4"),
    (r"fifth|5th|level\s*(?:5|five)\b", "5"),
    (r"mezz", "mezz"),
)
_NOT_PLAN_RE = _re.compile(
    r"demolition|demo\s|reflected|ceiling|roof|site|foundation|framing|"
    r"electrical|mechanical|plumbing|fire|sprinkler|lighting|power|hvac|"
    r"piping|enlarged|furniture|finish|signage|erosion|landscape|utility|"
    r"grading|slab|equipment|casework|section|elevation|detail|schedule|"
    r"code|life\s*safety|accessib|structural|drainage", _re.I)
_PLAN_RE = _re.compile(r"floor\s+plans?\b|\bplan\b", _re.I)


def _top_font_lines(page, k=14):
    """Largest-font text lines on the page, biggest first."""
    lines = []
    try:
        d = page.get_text("dict")
    except Exception:
        return []
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            txt = "".join(sp.get("text", "") for sp in line.get("spans", []))
            if not txt.strip():
                continue
            size = max((sp.get("size", 0) for sp in line.get("spans", [])), default=0)
            lines.append((size, txt.strip()))
    lines.sort(key=lambda t: -t[0])
    return [t for _, t in lines[:k]]


def _floors_in(text):
    out = []
    for pat, label in _FLOOR_WORDS:
        if _re.search(pat, text, _re.I):
            out.append(label)
    return out


def _norm(t):
    return _re.sub(r"[-_]+", " ", t or "")


_BARE_FLOOR_RE = _re.compile(
    r"^(?:basement|ground|first|second|third|fourth|fifth|"
    r"1st|2nd|3rd|4th|5th)\s+floor\s*$", _re.I)


def classify_title_lines(lines, filename="", filename_only=False):
    """{kind, floors, sheet} from title candidates (largest text first).

    Route A: a line naming a "floor plan" (not disqualified).
    Route B: bare viewport titles ("FIRST  FLOOR") on an architectural
             plan sheet (A-1xx) — several may appear on one sheet
             (Livestock A-102 carries FIRST and SECOND FLOOR viewports).
    Multi-file sets classify by FILENAME only: sheets embed unrelated plan
    diagrams (code-summary pages show life-safety floor plans) that would
    misclassify by page text.
    """
    fname = _norm(filename.rsplit("/", 1)[-1]) if filename else ""
    sheet = None
    m = _SHEET_ID_RE.search(fname)
    if not m:
        for l in lines[:4]:
            m = _SHEET_ID_RE.search(l)
            if m:
                break
    if m:
        sheet = m.group(1).upper().replace(".", "-")
    cands = ([fname] if fname else [])
    if not filename_only:
        cands += [_norm(l) for l in lines]
    floors = []
    is_plan = False
    is_arch_plan_sheet = bool(sheet and _re.match(r"A-?D?-?1", sheet))
    for l in cands:
        if _PLAN_RE.search(l) and not _NOT_PLAN_RE.search(l):
            is_plan = True
            floors.extend(f for f in _floors_in(l) if f not in floors)
        elif (not filename_only and is_arch_plan_sheet
              and _BARE_FLOOR_RE.match(l.strip())):
            is_plan = True
            floors.extend(f for f in _floors_in(l) if f not in floors)
    if not is_plan:
        return {"kind": "other", "floors": [], "sheet": sheet}
    return {"kind": "floor_plan", "floors": floors or ["all"], "sheet": sheet}


_SHEET_ID_RE = _re.compile(r"\b(A[D]?[-.]?\d{1,3}(?:\.\d{1,2})?[A-Za-z]?)\b")


def select_floor_plan_pages(pdf_paths):
    """Canonical floor-plan pages for a job (single- or multi-file).

    Returns [{pdf, page, floors, sheet}] deduped so each floor is claimed
    once; arch sheets (A-1xx) win over sketches/others. A bare "FLOOR
    PLANS"/["all"] page is used only when no floor-specific pages exist.
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF (fitz) is required")
    filename_only = len(pdf_paths) > 3   # sheet-per-file set
    plan_pages = []
    for pdf in pdf_paths:
        fname = pdf.rsplit("/", 1)[-1]
        doc = fitz.open(pdf)
        try:
            for i in range(len(doc)):
                lines = [] if filename_only else _top_font_lines(doc[i])
                info = classify_title_lines(lines, fname,
                                            filename_only=filename_only)
                if info["kind"] != "floor_plan" and not filename_only:
                    # strict caption fallback: viewport captions are exact
                    # lines like "2nd Floor Plan" (364 p10's title block is
                    # unlabeled, but its caption line is present in text)
                    cap_re = _re.compile(
                        r"^\s*\d{0,2}\s*(basement|ground|first|second|third|"
                        r"fourth|fifth|1st|2nd|3rd|4th|5th)?\s*"
                        r"(?:and\s+(?:\d\w\w|\w+)\s*)?floor\s+plans?\s*$",
                        _re.I)
                    try:
                        for tl in doc[i].get_text().splitlines():
                            tln = _norm(tl).strip()
                            if cap_re.match(tln) and not _NOT_PLAN_RE.search(tln):
                                fls = _floors_in(tln)
                                info = {"kind": "floor_plan",
                                        "floors": fls or ["all"],
                                        "sheet": info.get("sheet"),
                                        "src": "caption"}
                                break
                    except Exception:
                        pass
                if info["kind"] == "floor_plan":
                    arch = bool(info["sheet"]
                                and _re.match(r"A-?D?-?\d", info["sheet"]))
                    plan_pages.append({"pdf": pdf, "page": i,
                                       "floors": info["floors"],
                                       "sheet": info["sheet"], "arch": arch,
                                       "src": info.get("src", "font")})
                if filename_only:
                    break   # one classification per file
        finally:
            doc.close()
    # arch sheets first, then file/page order
    plan_pages.sort(key=lambda p: (not p["arch"],))
    specific = [p for p in plan_pages if p["floors"] != ["all"]]
    claimed, out = set(), []
    for p in specific:
        new = [f for f in p["floors"] if f not in claimed]
        if new:
            claimed.update(new)
            out.append({**p, "floors": new})
    if not out:
        allp = [p for p in plan_pages if p["floors"] == ["all"]]
        # font-classified titles beat strict-caption fallbacks
        allp.sort(key=lambda p: p.get("src", "font") != "font")
        if allp:
            out = [allp[0]]
    # ordinal gap-fill: floors "1" and "3" claimed on consecutive sheets with
    # the page between them unclaimed -> that page is the missing floor whose
    # caption text is unextractable (364 p10: title plotted as curves)
    have = {f: p for p in out for f in p["floors"]}
    for miss, lo, hi in (("2", "1", "3"), ("3", "2", "4"), ("4", "3", "5")):
        if miss not in have and lo in have and hi in have:
            p_lo, p_hi = have[lo], have[hi]
            if (p_lo["pdf"] == p_hi["pdf"]
                    and p_hi["page"] - p_lo["page"] == 2):
                out.append({"pdf": p_lo["pdf"], "page": p_lo["page"] + 1,
                            "floors": [miss], "sheet": None, "arch": True,
                            "src": "gap-fill"})
    return out


def compute_vme_shadow_v2(pdf_paths, default_height_ft=9.0):
    """M4/M5 shadow: title-based page selection + tier-2 geometric walls.

    Works on single- AND multi-file jobs, tagged or untagged. Returns
    {total_wall_run_lf, est_wall_sf, n_floor_pages, by_page:[...],
    unmeasured:[...]} or None. Comparison-only until the accuracy bar is
    met; never load-bearing, never guesses a scale.
    """
    if fitz is None:
        return None
    try:
        paths = [p for p in (pdf_paths or []) if p]
        if not paths:
            return None
        # single-page single-file sets ARE the floor plan (CenHud)
        if len(paths) == 1:
            try:
                _doc = fitz.open(paths[0])
                single_page = len(_doc) == 1
                _doc.close()
            except Exception:
                single_page = False
        else:
            single_page = False
        if single_page:
            pages = [{"pdf": paths[0], "page": 0, "floors": ["all"]}]
        else:
            pages = select_floor_plan_pages(paths)
        if not pages:
            return None
        total_lf, by_page, unmeasured = 0.0, [], []
        for p in pages:
            r = vm.measure_wall_runs_geometric(p["pdf"], p["page"])
            lf = r.get("wall_run_lf")
            if lf is None:
                unmeasured.append({"pdf": p["pdf"].rsplit("/", 1)[-1],
                                   "page": p["page"] + 1,
                                   "reason": "no scale"})
                continue
            total_lf += lf
            by_page.append({"pdf": p["pdf"].rsplit("/", 1)[-1],
                            "page": p["page"] + 1, "floors": p["floors"],
                            "wall_run_lf": round(lf, 1),
                            "scale": r["pts_per_ft"],
                            "scale_source": r["scale_source"]})
        if not by_page:
            return None
        return {"total_wall_run_lf": round(total_lf, 1),
                "est_wall_sf": round(total_lf * default_height_ft),
                "n_floor_pages": len(by_page),
                "by_page": by_page,
                "unmeasured": unmeasured,
                "engine": "tier2-geometric+title-attribution"}
    except Exception:
        return None


# ---------------------------------------------------------------------------
# M4 — viewport segmentation (multiple drawings per sheet)
# ---------------------------------------------------------------------------
# Permit sets cram several viewports onto one sheet (364 A-1.14: 2nd + 3rd
# floor plans AND both RCPs). Measuring the whole page mixes plan walls with
# RCP linework and applies one scale to viewports that may differ. Segment
# the sheet: cluster drawing geometry into spatial regions, read each
# region's caption (CAD convention: "<n> <Title>  SCALE: 1/8"=1'-0"" sits
# BELOW its viewport), classify, and measure plan viewports independently.

def segment_viewports(page, grid=28, min_cells=4):
    """Spatial clusters of drawing geometry -> list of fitz.Rect regions."""
    rect = page.rect
    cw, ch = rect.width / grid, rect.height / grid
    occupied = set()
    for path in page.get_drawings():
        r = path.get("rect")
        if r is None or r.width * r.height > rect.width * rect.height * 0.6:
            continue
        cx = min(grid - 1, max(0, int((r.x0 + r.x1) / 2 / cw)))
        cy = min(grid - 1, max(0, int((r.y0 + r.y1) / 2 / ch)))
        occupied.add((cx, cy))
    # connected components over the occupancy grid (8-neighborhood)
    seen, comps = set(), []
    for cell in occupied:
        if cell in seen:
            continue
        stack, comp = [cell], []
        seen.add(cell)
        while stack:
            cx, cy = stack.pop()
            comp.append((cx, cy))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nb = (cx + dx, cy + dy)
                    if nb in occupied and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        if len(comp) >= min_cells:
            comps.append(comp)
    regions = []
    for comp in comps:
        xs = [c[0] for c in comp]
        ys = [c[1] for c in comp]
        regions.append(fitz.Rect(min(xs) * cw, min(ys) * ch,
                                 (max(xs) + 1) * cw, (max(ys) + 1) * ch))
    regions.sort(key=lambda r: (-r.width * r.height))
    return regions


def viewport_caption(page, region):
    """Caption text for a viewport: the text lines inside the region's lower
    band or just below it (title + scale note)."""
    band = fitz.Rect(region.x0, region.y0 + region.height * 0.72,
                     region.x1, min(page.rect.y1, region.y1 + region.height * 0.18))
    try:
        return page.get_text(clip=band) or ""
    except Exception:
        return ""


def measure_plan_viewports(pdf_path, page_index, fallback_scale=None):
    """Measure wall runs per PLAN viewport on one sheet.

    Returns list of {region, title_line, floors, scale, scale_source,
    wall_run_lf}. RCP/section/detail viewports are excluded by caption;
    a viewport with no resolvable scale is returned with wall_run_lf None.
    """
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        out = []
        for region in segment_viewports(page):
            cap = viewport_caption(page, region)
            cap_lines = [l.strip() for l in cap.splitlines() if l.strip()]
            title_line = None
            for l in cap_lines:
                if _PLAN_RE.search(_norm(l)) and not _NOT_PLAN_RE.search(_norm(l)):
                    title_line = l
                    break
            if not title_line:
                continue
            floors = _floors_in(_norm(title_line)) or ["all"]
            scale = vm.parse_scale(cap) or fallback_scale
            src = "caption" if vm.parse_scale(cap) else ("fallback" if fallback_scale else "none")
            lf = None
            if scale:
                H, V = vm._axis_segments(page)
                Hc = [(p, lo, hi) for (p, lo, hi) in H
                      if region.y0 <= p <= region.y1 and lo >= region.x0 - 5 and hi <= region.x1 + 5]
                Vc = [(p, lo, hi) for (p, lo, hi) in V
                      if region.x0 <= p <= region.x1 and lo >= region.y0 - 5 and hi <= region.y1 + 5]
                min_gap = vm._WALL_MIN_THICK_FT * scale
                max_gap = vm._WALL_MAX_THICK_FT * scale
                min_ov = vm._WALL_MIN_RUN_FT * scale
                ch_ = vm._pair_centerlines(Hc, min_gap, max_gap, min_ov)
                cv_ = vm._pair_centerlines(Vc, min_gap, max_gap, min_ov)
                run_pts = vm.cluster_wall_runs(ch_, max_gap) + vm.cluster_wall_runs(cv_, max_gap)
                lf = run_pts / scale
            out.append({"region": tuple(round(v) for v in region),
                        "title_line": title_line[:60], "floors": floors,
                        "scale": scale, "scale_source": src,
                        "wall_run_lf": None if lf is None else round(lf, 1)})
        return out
    finally:
        doc.close()
