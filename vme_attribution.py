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
    # foundation-plan-as-basement: when a set has no basement plan page but
    # the building has a basement, its walls are drawn on the foundation plan
    # (364 A-100). Claim it rather than dropping the floor.
    claimed_floors = {f for p in out for f in p["floors"]}
    if "basement" not in claimed_floors and not filename_only:
        for pdf in pdf_paths:
            doc = fitz.open(pdf)
            try:
                for i in range(len(doc)):
                    if any(_re.search(r"foundation\s+plan", l, _re.I)
                           for l in _top_font_lines(doc[i], 8)):
                        out.append({"pdf": pdf, "page": i,
                                    "floors": ["basement"], "sheet": None,
                                    "arch": False, "src": "foundation"})
                        raise StopIteration
            except StopIteration:
                break
            finally:
                doc.close()

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


# ---------------------------------------------------------------------------
# M2 — per-room scope filtering (geometry measures, reading decides billing)
# ---------------------------------------------------------------------------
_UNPAINTABLE_WALL_KW = ("tile", "frp", "storefront", "glass", "curtain",
                        "prefinish", "unpainted", "existing to remain",
                        "trusscore", "fiberglass")


# Residential-unit room labels. Units are only excluded from wall billing
# when the CALLER asks for a common-area basis (multifamily jobs where the
# contractor prices unit interiors per-unit, not by measured wall) — the
# anchor just carries the classification.
_UNIT_WORD = re.compile(r"\bunit\b", re.I)
# bare unit-type code label: "ALS1a (Room 060)", "AL2B (Unit 225)"
_UNIT_CODE = re.compile(r"^\s*[A-Z]{2,5}\d{0,2}[a-z]?\s*\((?:room|unit)\s*\d+\)", re.I)


def _is_unit_room(name) -> bool:
    n = str(name or "")
    return bool(_UNIT_WORD.search(n)) or bool(_UNIT_CODE.match(n))


def has_residential_units(analysis) -> bool:
    """True when the job actually contains residential units — several
    unit-named rooms, multiplied typical units, or a real unit count. A
    lone 'Cooler Unit' / 'AC Unit' room on a commercial job must NOT turn
    on unit-basis billing."""
    pi = analysis.get("project_info") or {}
    try:
        if float(pi.get("total_units") or 0) >= 3:
            return True
    except (TypeError, ValueError):
        pass
    unit_rooms = mult_rooms = 0
    for fl in (analysis.get("floors") or []):
        for room in (fl.get("rooms") or []):
            if _is_unit_room(room.get("room_name")):
                unit_rooms += 1
            try:
                if float(room.get("unit_multiplier") or 1) > 1:
                    mult_rooms += 1
            except (TypeError, ValueError):
                pass
    return unit_rooms >= 5 or mult_rooms >= 3


def _floor_tokens(name):
    n = str(name or "").lower()
    toks = set()
    for pat, tok in (("basement", "basement"), ("ground", "ground"),
                     ("first", "1"), ("1st", "1"),
                     ("second", "2"), ("2nd", "2"),
                     ("third", "3"), ("3rd", "3"),
                     ("fourth", "4"), ("4th", "4"),
                     ("mezz", "mezz")):
        if pat in n:
            toks.add(tok)
    m = re.search(r"\blevel\s*(\d)", n)
    if m:
        toks.add(m.group(1))
    return toks


def room_anchors(analysis, page_number=None, sheet_token=None,
                 floor_labels=None):
    """Room label anchors for one plan page: [(x_norm, y_norm, painted,
    height_ft, is_unit)]. Rooms are matched by extraction source_page
    (single-file sets) or source_sheet token in the filename (sheet-per-file
    sets); pass floor_labels (e.g. {'2'}) INSTEAD to match rooms by their
    floor — the fallback when the extraction sourced a floor's rooms from a
    different sheet (demo plan) than the one being measured. 'painted' comes
    from READ scope: in_scope + paintable wall material. 'is_unit' marks
    residential-unit labels (see _is_unit_room), assigned only when the job
    has residential units at all."""
    out = []
    units_possible = has_residential_units(analysis)
    for fl in (analysis.get("floors") or []):
        fl_toks = _floor_tokens(fl.get("floor_name")) if floor_labels else None
        for room in (fl.get("rooms") or []):
            bb = (room.get("bbox") or {}).get("label_bbox_norm")
            if not bb:
                continue
            if floor_labels is not None:
                if not (fl_toks & set(floor_labels)):
                    continue
            elif page_number is not None and room.get("source_page") != page_number:
                continue
            if sheet_token:
                st = str(room.get("source_sheet") or "").replace("-", "").replace(".", "").upper()
                if st and st not in sheet_token:
                    continue
            mats = str((room.get("materials") or {}).get("walls", "")).lower()
            painted = bool(room.get("in_scope", True)) and not any(
                k in mats for k in _UNPAINTABLE_WALL_KW)
            h = (room.get("dimensions") or {}).get("ceiling_height_feet")
            try:
                h = float(h) if h else None
            except (TypeError, ValueError):
                h = None
            if h is not None and not (6 <= h <= 45):
                h = None
            out.append(((bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0,
                        painted, h,
                        units_possible and _is_unit_room(room.get("room_name"))))
    return out


def scope_filtered_walls(pdf_path, page_index, pts_per_ft, anchors,
                         default_height_ft=9.0, min_anchors=3):
    """Billable wall LF/SF on one page: each clustered run is assigned to the
    nearest room label anchor; runs in unpainted rooms are dropped; painted
    runs bill at their room's read height.

    Returns (billable_lf, billable_sf, total_lf, n_runs, n_anchors). With
    fewer than min_anchors anchors the filter abstains: billable == total
    at default height (caller may apply a global fraction instead)."""
    runs = vm.wall_runs_with_positions(pdf_path, page_index, pts_per_ft)
    doc = fitz.open(pdf_path)
    try:
        rect = doc[page_index].rect
    finally:
        doc.close()
    W, Hh = rect.width, rect.height
    total_lf = sum((hi - lo) for (_, _, lo, hi) in runs) / pts_per_ft
    if len(anchors) < min_anchors:
        return total_lf, total_lf * default_height_ft, total_lf, len(runs), len(anchors)
    pts = [(a[0] * W, a[1] * Hh, a[2], a[3]) for a in anchors]
    bill_lf = bill_sf = 0.0
    for orient, perp, lo, hi in runs:
        mx, my = ((lo + hi) / 2.0, perp) if orient == "H" else (perp, (lo + hi) / 2.0)
        best = None
        bd = None
        for (ax, ay, painted, h) in pts:
            d = (ax - mx) ** 2 + (ay - my) ** 2
            if bd is None or d < bd:
                bd = d
                best = (painted, h)
        if best and best[0]:
            lf = (hi - lo) / pts_per_ft
            bill_lf += lf
            bill_sf += lf * (best[1] or default_height_ft)
    return bill_lf, bill_sf, total_lf, len(runs), len(pts)


def walls_by_basis(pdf_path, page_index, pts_per_ft, anchors,
                   default_height_ft=9.0):
    """Wall quantities under every billing basis Rider uses, per page.

    For each clustered run, the room on EACH SIDE is sampled (nearest anchor
    to a point offset perpendicular from the run's midpoint). A face bills
    when its side's room is painted; a run bills when either side is painted.

        runs_lf       — total centerline runs (raw geometry)
        run_bill_lf   — runs with >=1 painted side
        face_bill_lf  — sum of painted faces (2x for both-sides-painted)
        run_bill_sf / face_bill_sf — billed at each side's room height
                        (read height, default when unknown)

    With no usable anchors the scope is unknowable here: billable == raw and
    the caller decides (reliability gate).
    """
    runs = vm.wall_runs_with_positions(pdf_path, page_index, pts_per_ft)
    doc = fitz.open(pdf_path)
    try:
        rect = doc[page_index].rect
    finally:
        doc.close()
    W, Hh = rect.width, rect.height
    pts = [(a[0] * W, a[1] * Hh, a[2], a[3]) for a in anchors]
    off = vm._WALL_MAX_THICK_FT * pts_per_ft * 1.5
    runs_lf = run_bill_lf = face_bill_lf = 0.0
    run_bill_sf = face_bill_sf = 0.0

    def nearest(mx, my):
        best, bd = None, None
        for (ax, ay, painted, h) in pts:
            d = (ax - mx) ** 2 + (ay - my) ** 2
            if bd is None or d < bd:
                bd, best = d, (painted, h)
        return best

    for orient, perp, lo, hi in runs:
        lf = (hi - lo) / pts_per_ft
        runs_lf += lf
        if not pts:
            continue
        mid = (lo + hi) / 2.0
        if orient == "H":
            sides = [nearest(mid, perp - off), nearest(mid, perp + off)]
        else:
            sides = [nearest(perp - off, mid), nearest(perp + off, mid)]
        painted_sides = [s for s in sides if s and s[0]]
        if painted_sides:
            run_bill_lf += lf
            h_run = max((s[1] or default_height_ft) for s in painted_sides)
            run_bill_sf += lf * h_run
            face_bill_lf += lf * len(painted_sides)
            face_bill_sf += sum(lf * (s[1] or default_height_ft)
                                for s in painted_sides)
    if not pts:
        run_bill_lf = face_bill_lf = runs_lf
        run_bill_sf = face_bill_sf = runs_lf * default_height_ft
    return {"runs_lf": runs_lf, "run_bill_lf": run_bill_lf,
            "face_bill_lf": face_bill_lf, "run_bill_sf": run_bill_sf,
            "face_bill_sf": face_bill_sf, "n_anchors": len(pts),
            "n_runs": len(runs)}


# ---------------------------------------------------------------------------
# M2 — room regions via rectangular decomposition + flood fill
# ---------------------------------------------------------------------------
# The wall-run network partitions the plan into rooms without needing true
# polygonization: distinct V-run x-coordinates and H-run y-coordinates form a
# grid; a wall run BLOCKS passage between the two cells it separates along
# its span; flood-filling unblocked adjacency merges cells into room regions.
# Each region then carries its label anchors (scope, height) and yields
# per-room floor area, ceiling area, and its bounding wall faces — the exact
# per-room accounting Rider's takeoffs use.

def room_regions(pdf_path, page_index, pts_per_ft, anchors,
                 min_room_ft2=15.0, max_room_ft2=20000.0):
    """Partition a plan page into room regions from its wall-run network.

    Returns list of regions: {cells:[(x0,y0,x1,y1)pts], area_ft2, anchors:
    [(painted,height,label_xy)], bbox}. Regions with no anchor are still
    returned (unlabeled closets/chases) — callers decide their scope.
    """
    runs = vm.wall_runs_with_positions(pdf_path, page_index, pts_per_ft,
                                       min_width="auto")
    if not runs:
        return []
    # TOPOLOGY blocks on ALL linework — a glazing/storefront line bounds a
    # room even though it is not a billable wall pair. Billing stays with
    # the paired runs; here we only need the space partition to be tight.
    doc = fitz.open(pdf_path)
    try:
        page0 = doc[page_index]
        pageW, pageH = page0.rect.width, page0.rect.height
        rawH, rawV = vm._axis_segments(page0, min_len_pts=2.5 * pts_per_ft)
    finally:
        doc.close()
    slop = 0.6 * pts_per_ft   # walls block slightly beyond their endpoints
    door_gap = 6.5 * pts_per_ft  # openings up to a double-leaf door still
    # separate rooms — bridge them for the BLOCKING test only, or the flood
    # fill walks through every doorway and merges the whole floor.
    H = [(y, lo - slop, hi + slop) for (y, lo, hi) in rawH]
    V = [(x, lo - slop, hi + slop) for (x, lo, hi) in rawV]

    def build_lines(spans):
        """Group spans into wall lines (perp within a thickness band), union
        each line's spans closing gaps <= door_gap. Returns
        [(perp, [(lo,hi),...unioned])]."""
        lines = []
        for perp, lo, hi in sorted(spans):
            if lines and perp - lines[-1][0] <= 0.9 * pts_per_ft:
                lines[-1][1].append((lo, hi))
            else:
                lines.append((perp, [(lo, hi)]))
        out = []
        for perp, ivs in lines:
            ivs.sort()
            merged = []
            cs, ce = ivs[0]
            for lo, hi in ivs[1:]:
                if lo - ce <= door_gap:
                    ce = max(ce, hi)
                else:
                    merged.append((cs, ce))
                    cs, ce = lo, hi
            merged.append((cs, ce))
            out.append((perp, merged))
        return out

    H_lines = build_lines(H)
    V_lines = build_lines(V)

    # Sheet-border / title-block-scale lines are not room walls; keeping them
    # as blockers seals the OUTSIDE into a false enclosing "room". Drop any
    # clustered line whose covered span exceeds 85% of the page dimension so
    # the true outside stays page-edge-connected (leaky) and callers discard it.
    def _drop_border(lines, page_dim):
        return [(perp, ivs) for (perp, ivs) in lines
                if sum(hi - lo for lo, hi in ivs) <= 0.85 * page_dim]
    H_lines = _drop_border(H_lines, pageW)
    V_lines = _drop_border(V_lines, pageH)

    # Grid coordinates: seed from the paired runs AND from the clustered wall
    # lines. Runs alone leave exterior/unpaired walls with no cell boundary, so
    # the flood fill escapes through them (the envelope leak). Seeding every
    # wall LINE as a grid coordinate makes each wall a cell edge that the
    # blocking test can seal against, while staying O(#walls) small.
    xs, ys = set(), set()
    for orient, perp, lo, hi in runs:
        if orient == "H":
            ys.add(round(perp, 1))
            xs.update((round(lo, 1), round(hi, 1)))
        else:
            xs.add(round(perp, 1))
            ys.update((round(lo, 1), round(hi, 1)))
    for perp, ivs in V_lines:            # vertical walls -> x cell boundaries
        xs.add(round(perp, 1))
        for lo, hi in ivs:
            ys.update((round(lo + slop, 1), round(hi - slop, 1)))
    for perp, ivs in H_lines:            # horizontal walls -> y cell boundaries
        ys.add(round(perp, 1))
        for lo, hi in ivs:
            xs.update((round(lo + slop, 1), round(hi - slop, 1)))

    xs = sorted(xs)
    ys = sorted(ys)
    if len(xs) < 2 or len(ys) < 2:
        return []
    if (len(xs) - 1) * (len(ys) - 1) > 250_000:
        return []

    def blocked(lines, line_coord, a, b):
        """Edge [a,b] on line_coord is blocked when wall spans cover >=90%."""
        need = (b - a) * 0.9
        for perp, ivs in lines:
            if abs(perp - line_coord) > 1.2:
                continue
            cov = 0.0
            for lo, hi in ivs:
                cov += max(0.0, min(hi, b) - max(lo, a))
            if cov >= need:
                return True
        return False

    def h_blocked(y_line, x_a, x_b):
        return blocked(H_lines, y_line, x_a, x_b)

    def v_blocked(x_line, y_a, y_b):
        return blocked(V_lines, x_line, y_a, y_b)

    nx, ny = len(xs) - 1, len(ys) - 1
    # union-find over cells
    parent = list(range(nx * ny))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for j in range(ny):
        for i in range(nx):
            c = j * nx + i
            if i + 1 < nx and not v_blocked(xs[i + 1], ys[j], ys[j + 1]):
                union(c, c + 1)
            if j + 1 < ny and not h_blocked(ys[j + 1], xs[i], xs[i + 1]):
                union(c, c + nx)

    groups = {}
    boundary_roots = set()
    for j in range(ny):
        for i in range(nx):
            r = find(j * nx + i)
            groups.setdefault(r, []).append((xs[i], ys[j], xs[i + 1], ys[j + 1]))
            if i == 0 or j == 0 or i == nx - 1 or j == ny - 1:
                boundary_roots.add(r)
    # regions connected to the page edge are outside space OR rooms whose
    # envelope leaks; keep them but tag so callers can treat separately
    outside = boundary_roots

    sq = pts_per_ft * pts_per_ft
    regions = []
    for root, cells in groups.items():
        area = sum((x1 - x0) * (y1 - y0) for (x0, y0, x1, y1) in cells) / sq
        if not (min_room_ft2 <= area <= max_room_ft2):
            continue
        bx0 = min(c[0] for c in cells)
        by0 = min(c[1] for c in cells)
        bx1 = max(c[2] for c in cells)
        by1 = max(c[3] for c in cells)
        regions.append({"cells": cells, "area_ft2": round(area, 1),
                        "bbox": (bx0, by0, bx1, by1), "anchors": [],
                        "leaky": root in outside})

    # attach anchors (page-normalized -> pts)
    if anchors:
        doc = fitz.open(pdf_path)
        try:
            rect = doc[page_index].rect
        finally:
            doc.close()
        for a in anchors:
            painted, h = a[2], a[3]
            unit = a[4] if len(a) > 4 else False
            px, py = a[0] * rect.width, a[1] * rect.height
            for reg in regions:
                if any(x0 <= px <= x1 and y0 <= py <= y1
                       for (x0, y0, x1, y1) in reg["cells"]):
                    reg["anchors"].append((painted, h, (round(px), round(py)),
                                           unit))
                    break
    return regions


# ---------------------------------------------------------------------------
# M2 — region-scoped wall billing
# ---------------------------------------------------------------------------
# walls_by_basis samples each run's two sides against the NEAREST label
# anchor — cheap, but a wall bounding a large unlabeled space (warehouse,
# racking mezzanine, exterior) is attributed to whatever labeled room happens
# to sit closest, leaking its scope across the partition. Here each side is
# resolved to the room REGION containing the sample point (room_regions),
# so scope stops at the wall like it does on the plans.

def _region_scope(reg):
    """(kind, height) for one region: kind in {'paint','nopaint','unit',
    'out'} or None when the region carries no verdict (unlabeled sealed)."""
    if reg is None:
        return None
    anchs = reg.get("anchors") or []
    if not anchs:
        return ("out", None) if reg.get("leaky") else None
    if any(len(a) > 3 and a[3] for a in anchs):
        return ("unit", None)
    painted = [a for a in anchs if a[0]]
    if not painted:
        return ("nopaint", None)
    hs = [a[1] for a in painted if a[1]]
    return ("paint", max(hs) if hs else None)


def walls_by_basis_regions(pdf_path, page_index, pts_per_ft, anchors,
                           default_height_ft=9.0, regions=None,
                           samples_per_side=3, use_raster=True):
    """walls_by_basis with region-resolved sides + common-area basis.

    Each run side is sampled at 25/50/75%% of its span, offset one wall
    thickness out; each sample resolves to the room region containing it
    ('paint'/'nopaint'/'unit'/'out'), falling back to the nearest anchor
    when no region claims the point. A side's verdict is the majority of
    its resolved samples.

    Returns walls_by_basis keys plus:
        common_run_lf / common_run_sf   — runs with >=1 painted NON-UNIT side
        common_face_lf / common_face_sf — painted non-unit faces
        region_coverage — fraction of samples resolved by a region (the
                          reliability signal; low coverage means the page's
                          partition didn't seal and callers should not trust
                          the scoped numbers)
    """
    if regions is None:
        regions = room_regions(pdf_path, page_index, pts_per_ft, anchors)
    raster = None
    if use_raster and anchors:
        try:
            raster = RasterRooms(pdf_path, page_index, pts_per_ft, anchors)
        except Exception:
            raster = None
    runs = vm.wall_runs_with_positions(pdf_path, page_index, pts_per_ft)
    doc = fitz.open(pdf_path)
    try:
        rect = doc[page_index].rect
    finally:
        doc.close()
    W, Hh = rect.width, rect.height
    pts = [(a[0] * W, a[1] * Hh, a[2], a[3], a[4] if len(a) > 4 else False)
           for a in anchors]
    cells = [(x0, y0, x1, y1, reg) for reg in regions
             for (x0, y0, x1, y1) in reg["cells"]]

    def region_at(px, py):
        for (x0, y0, x1, y1, reg) in cells:
            if x0 <= px <= x1 and y0 <= py <= y1:
                return reg
        return None

    def nearest(mx, my):
        best, bd = None, None
        for (ax, ay, painted, h, unit) in pts:
            d = (ax - mx) ** 2 + (ay - my) ** 2
            if bd is None or d < bd:
                bd, best = d, (painted, h, unit)
        return best

    off = vm._WALL_MAX_THICK_FT * pts_per_ft * 1.5
    n_samp = n_reg = 0
    runs_lf = run_bill_lf = face_bill_lf = 0.0
    run_bill_sf = face_bill_sf = 0.0
    common_run_lf = common_run_sf = common_face_lf = common_face_sf = 0.0

    def side_verdict(sample_pts):
        """Majority verdict over one side's samples -> (kind, height)."""
        nonlocal n_samp, n_reg
        votes = []
        for (sx, sy) in sample_pts:
            n_samp += 1
            sc = raster.resolve(sx, sy) if raster is not None else None
            if sc is None:
                sc = _region_scope(region_at(sx, sy))
            if sc is not None:
                n_reg += 1
                votes.append(sc)
            elif pts:
                nb = nearest(sx, sy)
                if nb:
                    kind = ("unit" if nb[2] else
                            ("paint" if nb[0] else "nopaint"))
                    votes.append((kind, nb[1]))
        if not votes:
            return None
        kinds = [v[0] for v in votes]
        kind = max(set(kinds), key=kinds.count)
        hs = [v[1] for v in votes if v[0] == kind and v[1]]
        return (kind, max(hs) if hs else None)

    for orient, perp, lo, hi in runs:
        lf = (hi - lo) / pts_per_ft
        runs_lf += lf
        fr = [lo + (hi - lo) * f for f in (0.25, 0.5, 0.75)][:samples_per_side]
        if orient == "H":
            sides = [side_verdict([(m, perp - off) for m in fr]),
                     side_verdict([(m, perp + off) for m in fr])]
        else:
            sides = [side_verdict([(perp - off, m) for m in fr]),
                     side_verdict([(perp + off, m) for m in fr])]
        painted_sides = [s for s in sides if s and s[0] == "paint"]
        unit_sides = [s for s in sides if s and s[0] == "unit"]
        if painted_sides or unit_sides:
            # legacy basis counts unit interiors as painted rooms
            all_paint = painted_sides + unit_sides
            run_bill_lf += lf
            h_run = max((s[1] or default_height_ft) for s in all_paint)
            run_bill_sf += lf * h_run
            face_bill_lf += lf * len(all_paint)
            face_bill_sf += sum(lf * (s[1] or default_height_ft)
                                for s in all_paint)
        if painted_sides:
            common_run_lf += lf
            h_run = max((s[1] or default_height_ft) for s in painted_sides)
            common_run_sf += lf * h_run
            common_face_lf += lf * len(painted_sides)
            common_face_sf += sum(lf * (s[1] or default_height_ft)
                                  for s in painted_sides)
    if not pts and not cells:
        run_bill_lf = face_bill_lf = runs_lf
        run_bill_sf = face_bill_sf = runs_lf * default_height_ft
    return {"runs_lf": runs_lf, "run_bill_lf": run_bill_lf,
            "face_bill_lf": face_bill_lf, "run_bill_sf": run_bill_sf,
            "face_bill_sf": face_bill_sf,
            "common_run_lf": common_run_lf, "common_run_sf": common_run_sf,
            "common_face_lf": common_face_lf,
            "common_face_sf": common_face_sf,
            "region_coverage": (n_reg / n_samp) if n_samp else 0.0,
            "n_anchors": len(pts), "n_runs": len(runs),
            "n_regions": len(regions)}


def unit_code_vocab(analysis):
    """Unit-type code tokens ('als1a', 'al2b', ...) from the extraction's
    unit-room names. Empty on jobs with no residential units, which turns
    page_text_unit_anchors into a no-op."""
    if not has_residential_units(analysis):
        return set()
    vocab = set()
    skip = {"unit", "room", "demo", "plan", "typ", "typical", "the"}
    for fl in (analysis.get("floors") or []):
        for room in (fl.get("rooms") or []):
            name = str(room.get("room_name") or "")
            if not _is_unit_room(name):
                continue
            for tok in re.findall(r"[A-Za-z]{2,5}\d{0,2}[a-z]?", name):
                t = tok.lower()
                if t not in skip and not t.isdigit() and len(t) >= 2:
                    vocab.add(t)
    return vocab


def page_text_unit_anchors(pdf_path, page_index, vocab):
    """Anchors [(x_norm, y_norm, painted=True, h=None, unit=True)] for every
    page WORD matching a unit-type code. Unit interiors are drawn on floor
    plans but their rooms are extracted from the ENLARGED unit plans, so
    label anchors from the extraction never land on them — the sheet's own
    code labels do."""
    if not vocab:
        return []
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_index]
        W, Hh = page.rect.width, page.rect.height
        out = []
        for (x0, y0, x1, y1, word, *_rest) in page.get_text("words"):
            if word.lower() in vocab:
                out.append((((x0 + x1) / 2.0) / W, ((y0 + y1) / 2.0) / Hh,
                            True, None, True))
        return out
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# M2b — raster room resolver (curves/diagonals-proof)
# ---------------------------------------------------------------------------
# Rect decomposition seals rooms only when every wall is axis-aligned. Curved
# corridors and angled wings (senior-living Y-plans) leak the flood and drop
# region coverage to ~0.1, pushing scope back onto nearest-anchor guessing —
# which votes across unsealed partitions. Rendering the page and flooding
# open PIXELS blocks on every drawn wall regardless of geometry. MuPDF
# enforces a 1-device-pixel minimum stroke, so even hairlines block.

class RasterRooms:
    """Lazy connected-component room resolver over a rendered plan page.

    resolve(x_pt, y_pt) -> ('paint'|'nopaint'|'unit'|'out', height) or None
    when the point's component carries no label anchor (caller falls back).
    """

    def __init__(self, pdf_path, page_index, pts_per_ft, anchors,
                 px_per_ft=3.0, dark_threshold=200, max_room_ft2=20000.0,
                 max_anchors_per_room=8):
        import numpy as np
        self._np = np
        self.px_per_ft = px_per_ft
        self.max_px = max_room_ft2 * px_per_ft * px_per_ft
        self.max_anchors = max_anchors_per_room
        zoom = px_per_ft / pts_per_ft if pts_per_ft else 0.05
        doc = fitz.open(pdf_path)
        try:
            page = doc[page_index]
            rect = page.rect
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                                  colorspace=fitz.csGRAY, alpha=False,
                                  annots=False)
        finally:
            doc.close()
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width)
        self.zoom = zoom
        self.W, self.H = pix.width, pix.height
        self.open = arr > dark_threshold
        self.label = np.full(arr.shape, -1, dtype=np.int32)
        self.comp_anchors = {}          # comp id -> list of anchor tuples
        self.comp_out = set()           # comps touching the page border
        self._next = 0
        # anchors in px: (px, py, painted, h, unit)
        self.apx = [(a[0] * rect.width * zoom, a[1] * rect.height * zoom,
                     a[2], a[3], a[4] if len(a) > 4 else False)
                    for a in anchors]
        for (px, py, painted, h, unit) in self.apx:
            c = self._component_at(px, py)
            if c is not None:
                self.comp_anchors.setdefault(c, []).append((painted, h, unit))

    def _open_near(self, x, y, radius_px=8):
        """Nearest open pixel to (x, y) within radius (labels sit on text)."""
        np = self._np
        xi, yi = int(round(x)), int(round(y))
        for r in range(radius_px + 1):
            x0, x1 = max(0, xi - r), min(self.W - 1, xi + r)
            y0, y1 = max(0, yi - r), min(self.H - 1, yi + r)
            win = self.open[y0:y1 + 1, x0:x1 + 1]
            if win.any():
                ys, xs = np.nonzero(win)
                k = ((ys + y0 - yi) ** 2 + (xs + x0 - xi) ** 2).argmin()
                return int(xs[k] + x0), int(ys[k] + y0)
        return None

    def _component_at(self, x, y):
        """Component id containing point (px), flooding lazily. None if no
        open pixel nearby."""
        pt = self._open_near(x, y)
        if pt is None:
            return None
        xi, yi = pt
        if self.label[yi, xi] >= 0:
            return int(self.label[yi, xi])
        cid = self._next
        self._next += 1
        from collections import deque
        q = deque([(yi, xi)])
        self.label[yi, xi] = cid
        touches_border = False
        npx = 0
        lab, op = self.label, self.open
        Wp, Hp = self.W, self.H
        while q:
            cy, cx = q.popleft()
            npx += 1
            if cx == 0 or cy == 0 or cx == Wp - 1 or cy == Hp - 1:
                touches_border = True
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < Hp and 0 <= nx < Wp and lab[ny, nx] < 0 and op[ny, nx]:
                    lab[ny, nx] = cid
                    q.append((ny, nx))
        if touches_border:
            self.comp_out.add(cid)
        self.comp_px = getattr(self, "comp_px", {})
        self.comp_px[cid] = npx
        return cid

    def resolve(self, x_pt, y_pt):
        c = self._component_at(x_pt * self.zoom, y_pt * self.zoom)
        if c is None:
            return None
        anchs = self.comp_anchors.get(c)
        if not anchs:
            return ("out", None) if c in self.comp_out else None
        # Trust only ROOM-SIZED, label-coherent components. A component that
        # swallowed the floor (door openings leak the flood) would smear one
        # room's scope everywhere — refuse a verdict and let the caller fall
        # back to local evidence.
        if getattr(self, "comp_px", {}).get(c, 0) > self.max_px:
            return None
        if c in self.comp_out:
            return None    # open to the page edge yet carrying room labels:
            # the envelope leaked; the labels don't bound this space
        if len(anchs) > self.max_anchors:
            return None
        units = [a for a in anchs if a[2]]
        commons = [a for a in anchs if not a[2]]
        if len(units) >= 2 and len(commons) >= 2:
            return None
        if units:
            return ("unit", None)
        painted = [a for a in anchs if a[0]]
        unpainted = [a for a in anchs if not a[0]]
        if painted and unpainted and len(painted) < 2 * len(unpainted):
            return None    # conflicting labels without a clear majority
        if not painted:
            return ("nopaint", None)
        hs = [a[1] for a in painted if a[1]]
        return ("paint", max(hs) if hs else None)


# ---------------------------------------------------------------------------
# M3 — VME primary (measured provenance)
# ---------------------------------------------------------------------------

def _read_heights(analysis, default=9.0):
    """Wall-area-weighted READ ceiling height per floor + job default."""
    per_floor = {}
    num = den = 0.0
    for fl in (analysis.get("floors") or []):
        fnum = fden = 0.0
        for room in (fl.get("rooms") or []):
            d = room.get("dimensions") or {}
            try:
                h = float(d.get("ceiling_height_feet") or 0)
                w = float(d.get("wall_area_sqft") or 0)
            except (TypeError, ValueError):
                continue
            if 6 <= h <= 45 and w > 0:
                fnum += h * w
                fden += w
        if fden > 0:
            per_floor[str(fl.get("floor_name", "")).lower()] = fnum / fden
            num += fnum
            den += fden
    return per_floor, (num / den) if den else default


def _height_for(per_floor, default, floor_label):
    for name, h in per_floor.items():
        if floor_label in ("1", "ground") and any(
                k in name for k in ("first", "1st", "ground")):
            return h
        if floor_label == "2" and any(k in name for k in ("second", "2nd")):
            return h
        if floor_label == "3" and any(k in name for k in ("third", "3rd")):
            return h
        if floor_label == "basement" and "basement" in name:
            return h
        if floor_label == "mezz" and "mezz" in name:
            return h
    return default


def _dispersed(anchors):
    if len(anchors) < 3:
        return False
    xs = sorted(a[0] for a in anchors)
    ys = sorted(a[1] for a in anchors)
    return (xs[-1] - xs[0]) > 0.15 or (ys[-1] - ys[0]) > 0.15


def compute_vme_primary(pdf_paths, analysis, default_height_ft=9.0):
    """Measured wall quantity for a whole job, with an explicit reliability
    verdict — the M3 'VME primary' path.

    Reliable ONLY when every identified plan page measures with a detected
    scale AND carries >=3 spatially-dispersed room-label anchors (so per-room
    scope filtering actually ran). Any page that fails demotes the whole job
    to unreliable — the caller keeps vision quantities and this result stays
    a shadow.

    Returns {reliable, reasons, measured_wall_sf, measured_wall_run_lf,
    raw_lf, basis, by_page}.
    """
    reasons = []
    try:
        pages = select_floor_plan_pages(pdf_paths)
    except Exception as e:
        return {"reliable": False, "reasons": [f"page selection failed: {e}"]}
    if not pages:
        return {"reliable": False, "reasons": ["no floor-plan pages identified"]}
    multi = len(pdf_paths) > 3
    per_floor_h, def_h = _read_heights(analysis, default_height_ft)
    vocab = unit_code_vocab(analysis)
    face_basis = has_residential_units(analysis)
    scales = []
    measured = {}
    for p in pages:
        r = vm.measure_wall_runs_geometric(p["pdf"], p["page"])
        measured[id(p)] = r
        if r.get("wall_run_lf") is not None:
            scales.append(r["pts_per_ft"])
    sib = max(set(scales), key=scales.count) if scales else None
    tot_bill_sf = tot_run_lf = raw_lf = 0.0
    by_page = []
    for p in pages:
        r = measured[id(p)]
        if r.get("wall_run_lf") is None and sib:
            r = vm.measure_wall_runs_geometric(p["pdf"], p["page"], pts_per_ft=sib)
        if r.get("wall_run_lf") is None:
            reasons.append(f"unmeasured page {p['pdf']}#{p['page'] + 1} (no scale)")
            continue
        if multi:
            tok = (p["pdf"].rsplit("/", 1)[-1].upper()
                   .replace("-", "").replace(".", ""))
            anchors = room_anchors(analysis, sheet_token=tok)
        else:
            anchors = room_anchors(analysis, page_number=p["page"] + 1)
        anchors = anchors + page_text_unit_anchors(p["pdf"], p["page"], vocab)
        if not _dispersed(anchors):
            reasons.append(
                f"page {p['pdf'].rsplit('/', 1)[-1]}#{p['page'] + 1}: "
                f"{len(anchors)} usable room anchors (need >=3, dispersed)")
            continue
        if len(p["floors"]) == 1 and p["floors"][0] != "all":
            h = _height_for(per_floor_h, def_h, p["floors"][0])
        else:
            h = def_h
        wb = walls_by_basis_regions(p["pdf"], p["page"], r["pts_per_ft"],
                                    anchors, default_height_ft=h)
        bill = wb["face_bill_sf"] if face_basis else wb["run_bill_sf"]
        tot_bill_sf += bill
        tot_run_lf += wb["run_bill_lf"]
        raw_lf += r["wall_run_lf"]
        by_page.append({"pdf": p["pdf"].rsplit("/", 1)[-1],
                        "page": p["page"] + 1,
                        "bill_sf": round(bill),
                        "run_bill_lf": round(wb["run_bill_lf"], 1),
                        "coverage": round(wb["region_coverage"], 2),
                        "n_anchors": wb["n_anchors"]})
    n_ok = len(by_page)
    reliable = n_ok == len(pages) and n_ok > 0 and not reasons
    return {"reliable": reliable, "reasons": reasons,
            "measured_wall_sf": round(tot_bill_sf),
            "measured_wall_run_lf": round(tot_run_lf, 1),
            "raw_lf": round(raw_lf, 1),
            "basis": "face" if face_basis else "run",
            "by_page": by_page}
