"""
Deterministic measurement-markup takeoff reader.

Root cause it addresses (Harlem Valley Homestead / JW Estimating, 2026-08-12):
Rider hired an outside estimator whose deliverable was the architectural set
WITH their measurement markups burned in as PDF annotations (Bluebeam-style
tool marks). Every wall run is a PolyLine whose Contents is the measured
length ("58'-8\""), every ceiling a Polygon whose Contents is the area
("1,356 sf"), doors are count-group Polygons, and sealed-concrete areas are
filled Square annotations. That is a complete, structured, deterministic
takeoff embedded in the PDF — the exact numbers the customer treats as fact.

The pipeline ignored all of it: the LLM read the flattened page image, guessed
room dimensions, and the customer's own measured quantities never reached
pricing (walls 14,386 vs 17,402 SF; ceilings 3,211 vs 9,009 SF; doors 39 vs
29; concrete 3,480 vs 5,546 SF on that job).

This module extracts and classifies measurement annotations from the ORIGINAL
uploaded PDFs (annotations do not survive the normalization/rasterize path —
use analysis['_vme_pdf_paths'], same as the VME engine). Pure geometry + text
parsing; no LLM. Consumed by the flag-gated authoritative pass in
Takeoff_DIRECT (NIGHTSHIFT_MARKUP_TAKEOFF).

Hard-numbers policy: these ARE the hard numbers — a human measured them onto
the plans. Anything that cannot be classified is returned in `unclassified`
for an RFI, never guessed into a bucket.
"""

import re

# Annotation types that carry linear measurements vs area/count measurements.
_LINEAR_TYPES = {"PolyLine", "Line"}
_AREA_TYPES = {"Polygon", "Square", "Circle"}

# Feet-inches measurement: 58'-8", 9'-0", 127'-5 1/2"
_FTIN_RE = re.compile(
    r"^\s*(\d+)\s*'\s*(?:-?\s*(\d+)\s*(?:\s+(\d+)\s*/\s*(\d+))?\s*\"?)?\s*$")
# Area measurement: "1,356 sf" / "12.5 SF" / "416sf"
_SF_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*(?:sf|sq\.?\s*ft\.?)\s*$", re.I)
# Bare integer — Bluebeam count groups stamp the group total on every member.
_COUNT_RE = re.compile(r"^\s*[\d,]+\s*$")
# LF measurement: "986 lf" (some tools emit LF totals instead of ft-in)
_LF_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*(?:lf|lin\.?\s*ft\.?)\s*$", re.I)
# Door leaf width from a subject like "(10'-0''x6'-8'') Door Paint"
_DOOR_W_RE = re.compile(r"\(?\s*(\d+)\s*'[^x]*x", re.I)
# Drawing scale from page text: 1/8" = 1'-0" (also 1/4, 3/32, ...)
_SCALE_RE = re.compile(
    r"(\d+)\s*/\s*(\d+)\s*(?:\"|''|in\.?|inch)?\s*=\s*1\s*'", re.I)

# Overhead / coiling doors are wider than a normal leaf — flag them for the
# RFI (paint scope on overhead doors needs confirmation), but still count.
_LARGE_DOOR_MIN_WIDTH_FT = 6

# Filled rectangles smaller than this are legend swatches, not floor areas.
_MIN_FILLED_AREA_SF = 10.0


def parse_ftin(s):
    """'58\\'-8\"' -> 58.667 ft; returns None when not a feet-inches string."""
    m = _FTIN_RE.match(str(s or "").replace("’", "'").replace("”", '"'))
    if not m:
        return None
    ft = int(m.group(1))
    inches = float(m.group(2) or 0)
    if m.group(3) and m.group(4) and int(m.group(4)) != 0:
        inches += int(m.group(3)) / int(m.group(4))
    return ft + inches / 12.0


def _parse_sf(s):
    m = _SF_RE.match(str(s or ""))
    return float(m.group(1).replace(",", "")) if m else None


def _parse_lf(s):
    m = _LF_RE.match(str(s or ""))
    return float(m.group(1).replace(",", "")) if m else None


def detect_scale_pt_per_ft(page_text):
    """Points-per-foot from a '1/8\" = 1'-0\"' notation (72 pt/in / 8 = 9.0).

    Returns None when no scale notation is present — callers must then skip
    geometry-derived areas (fail-safe), never assume a scale.
    """
    for m in _SCALE_RE.finditer(page_text or ""):
        num, den = int(m.group(1)), int(m.group(2))
        if den > 0 and num > 0:
            return 72.0 * num / den
    return None


def _classify_subject(subject):
    """Map an annotation subject line to a takeoff category.

    Categories: wall / mr_wall (LF), ceiling / mr_ceiling (SF), door (count),
    railing (LF), stair (SF), concrete (SF), None = unclassified.
    Order matters — specific surfaces before the generic paint/finish wall
    match ('Interior Finish: Handrail Paint' must not read as a wall).
    """
    s = str(subject or "").lower()
    if not s:
        return None
    is_mr = bool(re.search(r"\bmr\b|moisture", s))
    if "handrail" in s or "railing" in s or re.search(r"\brail\b", s):
        return "railing"
    if "stair" in s:
        return "stair"
    if "door" in s:
        return "door"
    # JW's tool set spells it 'Celling'; accept both.
    if "ceiling" in s or "celling" in s:
        return "mr_ceiling" if is_mr else "ceiling"
    if "concrete" in s and ("seal" in s or "sealing" in s or "sealer" in s):
        return "concrete"
    if "seal" in s and "concrete" in s:
        return "concrete"
    if "paint" in s or "finish" in s:
        return "mr_wall" if is_mr else "wall"
    return None


def extract_markup_takeoff(pdf_paths):
    """Read measurement annotations from the original PDFs.

    Returns a dict:
      wall_lf, mr_wall_lf, ceiling_sf, mr_ceiling_sf, railing_lf, stair_sf,
      concrete_sf, doors: {subject: {count, leaf_width_ft, large}},
      doors_total, doors_large,
      n_classified   — measurement annotations that landed in a bucket,
      n_measurement  — annotations that carried any measurement content,
      unclassified   — [(subject, content), ...] for the RFI,
      skipped_no_scale_sf_shapes — filled shapes dropped (no scale on page),
      by_page        — per-page classified counts (provenance).
    Never raises on a bad file — that file contributes nothing.
    """
    out = {
        "wall_lf": 0.0, "mr_wall_lf": 0.0,
        "ceiling_sf": 0.0, "mr_ceiling_sf": 0.0,
        "railing_lf": 0.0, "stair_sf": 0.0, "concrete_sf": 0.0,
        "doors": {}, "doors_total": 0, "doors_large": 0,
        "n_classified": 0, "n_measurement": 0,
        "unclassified": [], "skipped_no_scale_sf_shapes": 0,
        "by_page": [],
    }
    try:
        import fitz
    except ImportError:
        return out

    for path in pdf_paths or []:
        try:
            doc = fitz.open(path)
        except Exception:
            continue
        for pno in range(len(doc)):
            page = doc[pno]
            try:
                annots = list(page.annots() or [])
            except Exception:
                continue
            if not annots:
                continue
            pt_per_ft = None
            page_classified = 0
            for a in annots:
                try:
                    info = a.info or {}
                    atype = a.type[1]
                except Exception:
                    continue
                subject = (info.get("subject") or "").strip()
                content = (info.get("content") or "").strip()
                cat = _classify_subject(subject)

                lf = parse_ftin(content)
                if lf is None:
                    lf = _parse_lf(content)
                sf = _parse_sf(content)
                is_count = bool(content) and _COUNT_RE.match(content) and sf is None

                # Content-less filled shapes with no classifiable subject are
                # deferred to the keyed-fill pass below (sealed-concrete
                # rectangles) — not measurements yet, not unclassified noise.
                if (cat is None and not content and atype in _AREA_TYPES
                        and _is_filled(a)):
                    continue

                has_measurement = bool(
                    lf is not None or sf is not None or is_count)
                if not has_measurement:
                    continue
                out["n_measurement"] += 1

                if cat is None:
                    if subject or content:
                        out["unclassified"].append((subject, content))
                    continue

                if cat in ("wall", "mr_wall", "railing") and lf is not None:
                    key = {"wall": "wall_lf", "mr_wall": "mr_wall_lf",
                           "railing": "railing_lf"}[cat]
                    out[key] += lf
                elif cat in ("ceiling", "mr_ceiling", "stair") and sf is not None:
                    key = {"ceiling": "ceiling_sf",
                           "mr_ceiling": "mr_ceiling_sf",
                           "stair": "stair_sf"}[cat]
                    out[key] += sf
                elif cat == "door":
                    # Count groups: one annotation per counted door; the
                    # Contents echoes the group total on every member, so the
                    # instance count is the quantity — never sum the contents.
                    d = out["doors"].setdefault(
                        subject, {"count": 0, "leaf_width_ft": None,
                                  "large": False})
                    d["count"] += 1
                    wm = _DOOR_W_RE.search(subject)
                    if wm:
                        d["leaf_width_ft"] = int(wm.group(1))
                        d["large"] = d["leaf_width_ft"] >= _LARGE_DOOR_MIN_WIDTH_FT
                elif cat == "concrete" and sf is not None:
                    out["concrete_sf"] += sf
                elif cat == "concrete":
                    out["unclassified"].append((subject, content))
                    continue
                else:
                    out["unclassified"].append((subject, content))
                    continue
                out["n_classified"] += 1
                page_classified += 1

            # Sealed-concrete areas are often content-less filled rectangles
            # keyed by a legend note rather than per-shape subjects. When the
            # page carries a "concrete seal" marker (FreeText or subject) and
            # a machine-readable scale, bill the filled shapes' geometric
            # areas. No scale -> skip and report (fail-safe, never guess).
            has_concrete_key = any(
                "concrete seal" in ((x.info.get("content") or "") +
                                    (x.info.get("subject") or "")).lower()
                for x in annots if x.type[1] == "FreeText")
            unkeyed_fills = [
                x for x in annots
                if x.type[1] in ("Square", "Polygon") and _is_filled(x)
                and not (x.info.get("content") or "").strip()
                and _classify_subject(x.info.get("subject")) is None]
            if has_concrete_key and unkeyed_fills and out["concrete_sf"] <= 0:
                if pt_per_ft is None:
                    try:
                        pt_per_ft = detect_scale_pt_per_ft(page.get_text())
                    except Exception:
                        pt_per_ft = None
                if pt_per_ft:
                    for x in unkeyed_fills:
                        r = x.rect
                        area = (r.width / pt_per_ft) * (r.height / pt_per_ft)
                        if area >= _MIN_FILLED_AREA_SF:
                            out["concrete_sf"] += area
                            out["n_classified"] += 1
                            out["n_measurement"] += 1
                            page_classified += 1
                else:
                    out["skipped_no_scale_sf_shapes"] += len(unkeyed_fills)

            if page_classified:
                out["by_page"].append({
                    "pdf": path, "page": pno + 1,
                    "classified": page_classified})
        doc.close()

    out["doors_total"] = sum(d["count"] for d in out["doors"].values())
    out["doors_large"] = sum(d["count"] for d in out["doors"].values()
                             if d["large"])
    for k in ("wall_lf", "mr_wall_lf", "ceiling_sf", "mr_ceiling_sf",
              "railing_lf", "stair_sf", "concrete_sf"):
        out[k] = round(out[k], 2)
    return out


def _is_filled(annot):
    try:
        fill = (annot.colors or {}).get("fill")
        return bool(fill)
    except Exception:
        return False
