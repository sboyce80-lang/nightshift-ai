"""
Tier-1 bounding-box anchoring for takeoff rooms — SPIKE.

Walks a finished takeoff result JSON and attaches `bbox` info to each room by
matching its name/id against the PyMuPDF text layer of the room's source_page.
Pure post-process, zero LLM calls.

Output schema added to each room dict:

    room["bbox"] = {
        "label_bbox_norm": [x0, y0, x1, y1] | None,   # 0..1 of page, top-left origin
        "page_size_pt": [width, height] | None,
        "match_quality": "exact" | "ci" | "normalized" | "token" | None,
        "match_text": "<actual text span that anchored the box>" | None,
        "candidates_on_page": int,                    # how many spans matched
        "source": "label_text_span",
    }

Designed to run standalone against any historical result JSON + its source PDF.
Not yet wired into TAKEOFF_DIRECT.py — that hook-up is the next step if the
spike validates.
"""

from __future__ import annotations

import re
from typing import Iterable

import fitz  # PyMuPDF


# Tokens we strip when normalizing a room_name to its drawing-sheet label.
# These are Claude's disambiguation prefixes/suffixes that don't appear on the
# physical sheet — the plan just says "BEDROOM", not "1BR Bedroom".
_QUALIFIER_TOKENS = {
    "1br", "2br", "3br", "studio", "typical",
    "apt", "apartment", "unit",
}

# Floor qualifiers we strip: "2nd Floor Corridor" → "Corridor", "3rd Floor Lobby" → "Lobby".
# Matched as a leading two-token unit ("<ord> floor").
_FLOOR_QUALIFIER_RE = re.compile(
    r"^(?:\d+(?:st|nd|rd|th)|first|second|third|fourth|fifth|basement|ground|roof|mezzanine)\s+floor\s+",
    re.IGNORECASE,
)

# Drawing-label aliases: Claude's JSON uses full words; the sheet uses short forms.
# Each entry: full-word room name → list of abbreviations actually drawn on plans.
# These are tried as alternate exact-match candidates after the normalizer runs.
_LABEL_ALIASES = {
    "bathroom": ["bath", "ba", "wc", "toilet"],
    "bedroom":  ["br", "bdrm", "bed"],
    "corridor": ["corr", "cor", "hall"],
    "closet":   ["cl", "clo", "clos"],
    "kitchen":  ["kit", "ktch"],
    "lobby":    ["lob"],
    "elevator": ["elev"],
    "mechanical": ["mech", "mech."],
    "electrical": ["elec", "elec."],
    "storage":  ["stor", "stg"],
    "utility":  ["util"],
    "stair":    ["str", "stairs"],
    "vestibule": ["vest"],
}

# Words that, when paired with an aliased label, often appear ALONE on the
# drawing — e.g. "Mechanical Room" labeled just "MECH." with no "ROOM".
# When we see a multi-word room name where one word aliases and the other is
# in this set, we also try the alias by itself as a candidate.
_GENERIC_ROOM_WORDS = {"room", "rm", "rm."}

# Numbered suffixes like "Storage Room 1", "Bedroom 2", "Stair 1".
# We try the un-suffixed form as a secondary candidate.
_NUMBERED_SUFFIX_RE = re.compile(r"\s+\d+$")

# Non-alpha chars (slashes, dashes, parentheses) — collapsed to spaces for
# token matching so "Living/Dining/Kitchen" tokens cleanly.
_NON_ALPHANUM_RE = re.compile(r"[^a-z0-9]+")


def _tokenize(s: str) -> list[str]:
    s = (s or "").lower()
    s = _NON_ALPHANUM_RE.sub(" ", s)
    return [tok for tok in s.split() if len(tok) >= 3]


def _normalized_candidates(room_name: str) -> list[str]:
    """Yield progressively-stripped variants of room_name to try as labels.

    Ordered most-specific → least-specific so earlier matches win.
    """
    if not room_name:
        return []

    seen: list[str] = []

    def _add(s: str) -> None:
        s = s.strip()
        if s and s not in seen:
            seen.append(s)

    _add(room_name)

    # Strip leading qualifier ("1BR Bedroom" → "Bedroom", "Studio Closet" → "Closet")
    parts = room_name.split()
    if parts and parts[0].lower().rstrip(".") in _QUALIFIER_TOKENS:
        _add(" ".join(parts[1:]))

    # Strip leading floor qualifier ("2nd Floor Corridor" → "Corridor")
    floor_stripped = _FLOOR_QUALIFIER_RE.sub("", room_name)
    if floor_stripped != room_name:
        _add(floor_stripped)

    # Strip numbered suffix ("Storage Room 1" → "Storage Room", "Stair 1" → "Stair")
    stripped = _NUMBERED_SUFFIX_RE.sub("", room_name)
    if stripped != room_name:
        _add(stripped)

    # Combine: strip both qualifier AND numbered suffix
    if parts and parts[0].lower().rstrip(".") in _QUALIFIER_TOKENS:
        combined = _NUMBERED_SUFFIX_RE.sub("", " ".join(parts[1:]))
        _add(combined)

    # For each variant produced so far, also yield any alias forms
    # ("Bathroom" → "BATH", "Closet" → "CL"). We do this last so that the
    # full-word variants are preferred when they exist on the sheet.
    alias_variants: list[str] = []
    for v in list(seen):
        v_l = v.lower().strip()

        # Whole-string alias ("Bathroom" → "BATH")
        if v_l in _LABEL_ALIASES:
            for alias in _LABEL_ALIASES[v_l]:
                alias_variants.append(alias)
            continue

        # Word-by-word alias substitution for multi-word names.
        # Handles BOTH "Mechanical Room" → "Mech Room" / "Mech. Room" (first
        # word aliases) AND "Primary Bathroom" → "Primary Bath" (last word
        # aliases). Also tries the alias by itself when the partner word is
        # generic ("Mechanical Room" → "Mech" alone).
        v_parts = v.split()
        for i, part in enumerate(v_parts):
            part_l = part.lower().rstrip(".")
            if part_l not in _LABEL_ALIASES:
                continue
            for alias in _LABEL_ALIASES[part_l]:
                # Replace just this position
                substituted = list(v_parts)
                substituted[i] = alias
                alias_variants.append(" ".join(substituted))
                # Also try the alias alone if the OTHER words are all generic
                # filler ("Mechanical Room" → "Mech", because "Room" is filler)
                other_parts = [p.lower().rstrip(".") for j, p in enumerate(v_parts) if j != i]
                if other_parts and all(p in _GENERIC_ROOM_WORDS for p in other_parts):
                    alias_variants.append(alias)

    for av in alias_variants:
        _add(av)

    return seen


def _extract_page_spans(page: fitz.Page) -> list[dict]:
    """Return all non-empty text spans on a page with their bboxes.

    Each span: {"text": str, "bbox": [x0,y0,x1,y1], "size": float}
    """
    spans: list[dict] = []
    td = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    for block in td.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = (span.get("text") or "").strip()
                if not t:
                    continue
                spans.append({
                    "text": t,
                    "bbox": list(span.get("bbox", [0, 0, 0, 0])),
                    "size": float(span.get("size", 0)),
                })
    return spans


def _norm_bbox(bbox: list[float], page_w: float, page_h: float) -> list[float]:
    if page_w <= 0 or page_h <= 0:
        return [0.0, 0.0, 0.0, 0.0]
    x0, y0, x1, y1 = bbox
    return [
        max(0.0, min(1.0, x0 / page_w)),
        max(0.0, min(1.0, y0 / page_h)),
        max(0.0, min(1.0, x1 / page_w)),
        max(0.0, min(1.0, y1 / page_h)),
    ]


def _union_bbox(bboxes: Iterable[list[float]]) -> list[float] | None:
    bboxes = list(bboxes)
    if not bboxes:
        return None
    xs0 = [b[0] for b in bboxes]
    ys0 = [b[1] for b in bboxes]
    xs1 = [b[2] for b in bboxes]
    ys1 = [b[3] for b in bboxes]
    return [min(xs0), min(ys0), max(xs1), max(ys1)]


def _match_room_on_page(
    room_name: str,
    spans: list[dict],
) -> tuple[list[float] | None, str | None, str | None, int]:
    """Return (raw_bbox, quality, matched_text, candidate_count).

    Strategy:
        1. exact (case-sensitive)
        2. case-insensitive
        3. normalized variants (stripped qualifier / numbered suffix), CI
        4. token-overlap on multi-word names — union of matched-token bboxes
    """
    if not room_name or not spans:
        return None, None, None, 0

    name_u = room_name.upper()
    name_l = room_name.lower()

    # 1) exact
    exact = [s for s in spans if s["text"] == name_u or s["text"] == room_name]
    if exact:
        return exact[0]["bbox"], "exact", exact[0]["text"], len(exact)

    # 2) case-insensitive on the full name
    ci = [s for s in spans if s["text"].lower() == name_l]
    if ci:
        return ci[0]["bbox"], "ci", ci[0]["text"], len(ci)

    # 3) normalized variants (Claude prefixes/suffixes stripped)
    # Strip trailing punctuation from drawing spans too so "MECH." can match
    # alias "mech" and vice versa.
    def _strip_trail(s: str) -> str:
        return s.rstrip(" .,:;")
    for variant in _normalized_candidates(room_name)[1:]:  # skip original (already tried)
        v_l = _strip_trail(variant.lower())
        nm = [s for s in spans if _strip_trail(s["text"].lower()) == v_l]
        if nm:
            return nm[0]["bbox"], "normalized", nm[0]["text"], len(nm)

    # 4) token-overlap — find spans containing ANY required token, union their bboxes
    tokens = _tokenize(room_name)
    # Drop qualifier tokens from the requirement set
    required = [t for t in tokens if t not in _QUALIFIER_TOKENS]
    if not required:
        return None, None, None, 0

    matched_spans = []
    for s in spans:
        s_tokens = set(_tokenize(s["text"]))
        if not s_tokens:
            continue
        # A span matches if its tokens are a subset of our required set
        # (so "BEDROOM" matches required={"bedroom"}, but "BEDROOM CLOSET" wouldn't
        #  match required={"bedroom"} alone — it'd only match if "closet" was also
        #  required, preventing over-anchoring).
        if s_tokens.issubset(set(required)) and s_tokens & set(required):
            matched_spans.append(s)

    if matched_spans:
        bbox = _union_bbox(s["bbox"] for s in matched_spans)
        matched_text = " | ".join(sorted({s["text"] for s in matched_spans})[:3])
        return bbox, "token", matched_text, len(matched_spans)

    return None, None, None, 0


def attach_label_bboxes(result: dict, pdf_path: str) -> dict:
    """Walk floors/rooms in `result` and attach bbox info to each room.

    Accepts either shape:
        {"analysis": {"floors": [...]}}        # full submission result
        {"floors": [...]}                       # raw analysis dict from Claude

    Mutates and returns `result`. Adds a `bbox_spike_summary` block at the same
    level where "floors" was found, so callers can read coverage stats without
    re-walking.

    Never raises — if the PDF can't be opened or no source_page info is present,
    rooms get `bbox=None`-shaped entries and the summary records the failure.
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        # Don't break the takeoff if bbox attachment fails — just record the
        # error and leave rooms unmodified.
        target = result.get("analysis") if isinstance(result.get("analysis"), dict) else result
        target["bbox_spike_summary"] = {
            "total_rooms": 0,
            "anchored": 0,
            "coverage_pct": 0.0,
            "by_quality": {},
            "per_page": {},
            "pdf_path": pdf_path,
            "error": f"could not open PDF: {type(e).__name__}: {str(e)[:200]}",
        }
        return result

    # Cache spans + page size per page (don't re-extract for each room)
    page_cache: dict[int, tuple[list[dict], float, float]] = {}

    def _spans_for(page_1based: int):
        if page_1based in page_cache:
            return page_cache[page_1based]
        pg_0 = page_1based - 1
        if pg_0 < 0 or pg_0 >= len(doc):
            page_cache[page_1based] = ([], 0.0, 0.0)
            return page_cache[page_1based]
        page = doc[pg_0]
        spans = _extract_page_spans(page)
        page_cache[page_1based] = (spans, page.rect.width, page.rect.height)
        return page_cache[page_1based]

    counts = {"exact": 0, "ci": 0, "normalized": 0, "token": 0, "miss": 0, "no_page": 0}
    per_page: dict[int, dict] = {}

    # Tolerate either {"analysis": {"floors": ...}} or {"floors": ...}
    if isinstance(result.get("analysis"), dict) and "floors" in result["analysis"]:
        target = result["analysis"]
    else:
        target = result
    floors = target.get("floors") or []
    for floor in floors:
        for room in floor.get("rooms") or []:
            sp = room.get("source_page")
            if not sp:
                counts["no_page"] += 1
                room["bbox"] = {
                    "label_bbox_norm": None,
                    "page_size_pt": None,
                    "match_quality": None,
                    "match_text": None,
                    "candidates_on_page": 0,
                    "source": "label_text_span",
                    "source_pdf": pdf_path,
                }
                continue

            spans, pw, ph = _spans_for(sp)
            per_page.setdefault(sp, {"total": 0, "hits": 0, "qualities": {}})
            per_page[sp]["total"] += 1

            raw_bbox, quality, matched_text, n_cand = _match_room_on_page(
                room.get("room_name", ""), spans,
            )

            if raw_bbox is not None and pw > 0:
                counts[quality] += 1
                per_page[sp]["hits"] += 1
                per_page[sp]["qualities"][quality] = per_page[sp]["qualities"].get(quality, 0) + 1
                room["bbox"] = {
                    "label_bbox_norm": _norm_bbox(raw_bbox, pw, ph),
                    "page_size_pt": [pw, ph],
                    "match_quality": quality,
                    "match_text": matched_text,
                    "candidates_on_page": n_cand,
                    "source": "label_text_span",
                    "source_pdf": pdf_path,
                }
            else:
                counts["miss"] += 1
                room["bbox"] = {
                    "label_bbox_norm": None,
                    "page_size_pt": [pw, ph] if pw > 0 else None,
                    "match_quality": None,
                    "match_text": None,
                    "candidates_on_page": 0,
                    "source": "label_text_span",
                    "source_pdf": pdf_path,
                }

    doc.close()

    total = sum(counts.values())
    hits = total - counts["miss"] - counts["no_page"]
    target["bbox_spike_summary"] = {
        "total_rooms": total,
        "anchored": hits,
        "coverage_pct": round(100.0 * hits / total, 1) if total else 0.0,
        "by_quality": counts,
        "per_page": per_page,
        "pdf_path": pdf_path,
    }

    return result


# ---------------------------------------------------------------------------
# Annotated PDF rendering
# ---------------------------------------------------------------------------

_ANNOTATED_SUFFIX = ".annotated.pdf"


def annotated_drawings_filename(source_basename: str) -> str:
    """Public so the worker and the UI agree on the suffix.

    `source_basename` is the original uploaded PDF's filename (may include
    extension; either way the extension is replaced).
    """
    import os
    stem = os.path.splitext(source_basename)[0]
    return f"{stem}{_ANNOTATED_SUFFIX}"


def is_annotated_drawings_filename(filename: str) -> bool:
    """Filename convention used to distinguish annotated drawings from the
    estimate PDF and the raw job PDF/JSON in the results listing."""
    return bool(filename) and filename.lower().endswith(_ANNOTATED_SUFFIX)


# Color per match_quality, used for visual confidence triage. RGB in 0..1.
_QUALITY_COLORS = {
    "exact":      (0.10, 0.65, 0.10),   # green
    "ci":         (0.10, 0.55, 0.75),   # teal
    "normalized": (0.95, 0.65, 0.10),   # amber
    "token":      (0.85, 0.35, 0.10),   # orange
    None:         (0.85, 0.10, 0.10),   # red (used for misses)
}


# Keywords used to classify a sheet (by its drawing-index title) as either
# expected to contain rooms or being reference-only material. Mirrors the
# logic in Takeoff_DIRECT's manual_review check so both surfaces agree.
_SHEET_ROOMS_EXPECTED_KW = (
    "floor plan", "foundation plan", "roof plan",
    "apartment plan", "ceiling plan", "rcp",
)
_SHEET_REFERENCE_ONLY_KW = (
    "elevation", "section", "schedule", "detail",
    "wall section", "stair section", "canopy", "key plan",
)


def _classify_sheet_categories(pdf_path: str) -> dict[int, str]:
    """Return {page_1based: category} where category is one of:
        - "rooms_expected": floor plan / foundation / roof / RCP / apartment
        - "reference_only": elevation / section / schedule / detail
        - "unknown":         classifier couldn't decide

    Uses PyMuPDF text extraction only — zero API cost.
    """
    import re
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return {}

    # Step 1: pull the drawing index text from first 3 pages
    index_text = ""
    for pg_0 in range(min(3, len(doc))):
        try:
            t = doc[pg_0].get_text() or ""
        except Exception:
            continue
        if "DRAWING LIST" in t.upper() or "DRAWING INDEX" in t.upper() or "SHEET INDEX" in t.upper():
            index_text += "\n" + t

    # Step 2: build {sheet_id -> category} from the index titles
    sheet_to_cat: dict[str, str] = {}
    if index_text:
        # Each sheet ID appears followed by its title — look at next ~80 chars
        for m in re.finditer(r'\b[AD]-?\d{2,3}[A-Z]?\b', index_text.upper()):
            sid = re.sub(r'\s+', '', m.group())
            ctx = index_text.lower()[m.end():m.end() + 80]
            if any(kw in ctx for kw in _SHEET_ROOMS_EXPECTED_KW):
                sheet_to_cat[sid] = "rooms_expected"
            elif any(kw in ctx for kw in _SHEET_REFERENCE_ONLY_KW):
                sheet_to_cat[sid] = "reference_only"

    # Step 3: for each page, find its sheet ID (largest-font disciplinary ID
    # anywhere on the page) and map to category.
    page_cat: dict[int, str] = {}
    _sheet_re = re.compile(r'\b([ADCSEMPGLT]{1,2}|FP|FA|ID|AI|AD)\s*[-.]?\s*(\d{1,3}(?:\.\d{1,2})?)\b',
                           re.IGNORECASE)
    for pg_0 in range(len(doc)):
        page = doc[pg_0]
        try:
            td = page.get_text("dict")
        except Exception:
            page_cat[pg_0 + 1] = "unknown"
            continue
        cands = []
        for block in td.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    t = (span.get("text") or "").strip()
                    if not t:
                        continue
                    sz = float(span.get("size", 0))
                    for m in _sheet_re.finditer(t):
                        prefix = m.group(1).upper()
                        # Only A-series sheets are candidates for room data;
                        # other disciplines are inherently reference_only.
                        cands.append((sz, f"{prefix}{m.group(2)}", prefix))
        cands.sort(key=lambda x: -x[0])
        if not cands:
            page_cat[pg_0 + 1] = "unknown"
            continue
        # Try in font-size order for the first sheet ID we have a category for
        cat = None
        for sz, sid, prefix in cands:
            if sid in sheet_to_cat:
                cat = sheet_to_cat[sid]
                break
            # If not in our classified set, infer from prefix
            if prefix.startswith("A"):
                # A-series with no index match — assume rooms_expected only if
                # it looks like a floor plan series (A-1xx)
                if sid.startswith("A1") and len(sid) >= 3:
                    cat = "rooms_expected"
                else:
                    cat = "reference_only"
                break
            # Non-A discipline (S, E, M, P, C, L, FP, FA, G, T) → reference
            cat = "reference_only"
            break
        page_cat[pg_0 + 1] = cat or "unknown"

    doc.close()
    return page_cat


def render_annotated_pdf(pdf_in: str, result_or_analysis: dict, pdf_out: str) -> dict:
    """Render an annotated copy of `pdf_in` with room bboxes drawn on each
    source page. Non-referenced pages get a banner classified by category:
        - blue:  page is referenced in the takeoff JSON (has rooms)
        - gray:  page is reference material — elevation/section/schedule/details
                 (NOT an extraction failure — these sheets don't have rooms)
        - red:   page is a floor plan / RCP / foundation that SHOULD have rooms
                 but didn't get extracted (real extraction failure)

    Accepts either a full submission result ({"analysis": {...}}) or a raw
    analysis dict ({"floors": [...]}).

    Returns a summary: {pages, referenced_pages, rooms_drawn, misses_marked,
    output_size_bytes, extraction_failures}.
    """
    import os
    from collections import defaultdict

    if isinstance(result_or_analysis.get("analysis"), dict) and "floors" in result_or_analysis["analysis"]:
        analysis = result_or_analysis["analysis"]
    else:
        analysis = result_or_analysis

    doc = fitz.open(pdf_in)

    rooms_by_page: dict[int, list] = defaultdict(list)
    for floor in analysis.get("floors", []) or []:
        for r in floor.get("rooms", []) or []:
            sp = r.get("source_page")
            if sp:
                rooms_by_page[int(sp)].append((floor.get("floor_name", ""), r))

    referenced = set(rooms_by_page.keys())
    per_page = (analysis.get("bbox_spike_summary") or {}).get("per_page") or {}
    n_pages = len(doc)

    # Classify every page by sheet category so we can color non-referenced
    # pages appropriately. Closed-source PDFs may return {} on failure — in
    # that case every non-referenced page gets the legacy "unknown → red"
    # treatment.
    page_categories = _classify_sheet_categories(pdf_in)

    rooms_drawn = 0
    misses_marked = 0
    extraction_failures = 0  # pages that SHOULD have rooms but don't

    for pg_0 in range(n_pages):
        page = doc[pg_0]
        pg_1 = pg_0 + 1

        is_ref = pg_1 in referenced
        stats = per_page.get(pg_1) or per_page.get(str(pg_1)) or {}
        if is_ref:
            label = (f"p{pg_1}  |  {stats.get('hits', 0)}/{stats.get('total', len(rooms_by_page[pg_1]))}"
                     f" rooms anchored  |  {len(rooms_by_page[pg_1])} room entries")
            banner_color = (0.10, 0.40, 0.65)  # blue
        else:
            cat = page_categories.get(pg_1, "unknown")
            if cat == "rooms_expected":
                label = f"p{pg_1}  |  EXTRACTION FAILURE — this sheet should contain rooms"
                banner_color = (0.70, 0.10, 0.10)  # red
                extraction_failures += 1
            elif cat == "reference_only":
                label = f"p{pg_1}  |  Reference material — no rooms expected"
                banner_color = (0.40, 0.40, 0.40)  # gray
            else:
                label = f"p{pg_1}  |  Not referenced in takeoff (category unknown)"
                banner_color = (0.55, 0.40, 0.10)  # amber

        # Draw banner at the *visible* top, which depends on page rotation.
        # PyMuPDF's draw_* methods use the unrotated mediabox coordinate
        # system. We temporarily suspend rotation, compute the rect that
        # corresponds to the visible top (varies by rotation), draw the
        # banner + text with appropriate rotation so it reads upright, then
        # restore the original rotation.
        orig_rotation = page.rotation
        page.set_rotation(0)
        mw, mh = page.rect.width, page.rect.height
        banner_h = 36

        if orig_rotation in (0, None):
            banner_rect = fitz.Rect(0, 0, mw, banner_h)
            text_origin = (12, 24)
            text_rotate = 0
        elif orig_rotation == 90:
            # Visible top of CW-rotated page = bottom of mediabox
            banner_rect = fitz.Rect(0, mh - banner_h, mw, mh)
            text_origin = (12, mh - 12)
            text_rotate = 90
        elif orig_rotation == 180:
            banner_rect = fitz.Rect(0, mh - banner_h, mw, mh)
            text_origin = (mw - 12, mh - 12)
            text_rotate = 180
        elif orig_rotation == 270:
            # Visible top of CCW-rotated page = right of mediabox
            banner_rect = fitz.Rect(mw - banner_h, 0, mw, mh)
            text_origin = (mw - 12, 12)
            text_rotate = 270
        else:
            banner_rect = fitz.Rect(0, 0, mw, banner_h)
            text_origin = (12, 24)
            text_rotate = 0

        page.draw_rect(banner_rect, color=banner_color,
                       fill=banner_color, overlay=True)
        page.insert_text(text_origin, label,
                         fontsize=14, color=(1, 1, 1),
                         rotate=text_rotate, overlay=True)
        page.set_rotation(orig_rotation)

        if not is_ref:
            continue

        # Below, room-bbox drawing uses page.rect coordinates which DO respect
        # rotation for the get_text(dict) bboxes captured at attach time. So
        # those calls work as-is regardless of rotation.
        pw, ph = page.rect.width, page.rect.height

        # Stable corner-stack ordering for misses on this page
        misses_on_page = [(f, r) for f, r in rooms_by_page[pg_1]
                          if (r.get("bbox") or {}).get("label_bbox_norm") is None]
        miss_index = {id(r): i for i, (_, r) in enumerate(misses_on_page)}

        for floor_name, room in rooms_by_page[pg_1]:
            b = room.get("bbox") or {}
            quality = b.get("match_quality")
            color = _QUALITY_COLORS.get(quality, _QUALITY_COLORS[None])
            name = room.get("room_name", "?")
            mult = room.get("unit_multiplier", 1) or 1
            label_text = f"{name}" + (f"  (x{mult})" if mult > 1 else "")

            norm = b.get("label_bbox_norm")
            if norm is None:
                idx = miss_index.get(id(room), 0)
                y_off = 50 + idx * 18
                page.insert_text((12, y_off),
                                 f"MISS: {label_text}  (no label match on this page)",
                                 fontsize=11, color=color, overlay=True)
                misses_marked += 1
                continue

            x0 = norm[0] * pw
            y0 = norm[1] * ph
            x1 = norm[2] * pw
            y1 = norm[3] * ph

            pad = 6
            page.draw_rect(fitz.Rect(x0 - pad, y0 - pad, x1 + pad, y1 + pad),
                           color=color, width=1.5, overlay=True)
            cap_y = y0 - 4 if y0 > 20 else y1 + 14
            page.insert_text((x0, cap_y),
                             f"{label_text}  [{quality}]",
                             fontsize=9, color=color, overlay=True)
            rooms_drawn += 1

    doc.save(pdf_out, deflate=True)
    doc.close()

    return {
        "pages": n_pages,
        "referenced_pages": len(referenced),
        "rooms_drawn": rooms_drawn,
        "misses_marked": misses_marked,
        "extraction_failures": extraction_failures,
        "output_size_bytes": os.path.getsize(pdf_out) if os.path.exists(pdf_out) else 0,
    }
