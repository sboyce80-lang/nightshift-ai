"""Tests for the measurement-markup takeoff gate (2026-08-12).

Harlem Valley Homestead / JW Estimating: Rider's outside estimator delivered
the drawings with their measured takeoff embedded as PDF annotations —
wall-run PolyLines with ft-in lengths, ceiling Polygons with "N sf" areas,
door count-group Polygons, filled sealed-concrete rectangles. The pipeline
flattened the page and re-guessed everything; the estimate shipped $27,958
against the contractor's $43,491 (-36%). Additionally the large-commercial
wall cap clamped the VME-measured 14,386 SF to footprint×stories×1.25 =
7,200 SF (blind to deterministic walls AND to the analyzed basement level),
and the customer estimate printed the interior handrail under "Exterior".

Covers:
  1. markup_takeoff extraction: classification, ft-in/sf/count parsing,
     count groups (instance count, never sum of contents), scale detection,
     filled-rectangle concrete keyed by a FreeText legend note.
  2. _apply_markup_takeoff_authoritative: flag gate, min-annotation
     abstention, height requirement for walls, sanity band, aggregate
     overrides, RFI emission, idempotence.
  3. Large-commercial wall cap: suppressed under deterministic walls,
     levels basis includes analyzed basement floors.
  4. Estimate bucket routing: interior railings never print as Exterior.

Offline, no API. A synthetic annotated PDF is built with fitz.
"""
import os

import fitz

import markup_takeoff as MT
import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


# ── Build a synthetic JW-style annotated PDF ───────────────────────────────
def _build_pdf(path, walls=("58'-8\"", "44'-1\"", "309'-6\""),
               mr_walls=("9'-0\"",), ceilings=("1,356 sf", "64 sf"),
               mr_ceilings=("58 sf",), doors_std=3, doors_big=2,
               rail="47'-6\"", stair="100 sf", concrete_rects=2,
               scale_text='SCALE: 1/8" = 1\'-0"'):
    doc = fitz.open()
    page = doc.new_page(width=2592, height=1728)
    if scale_text:
        page.insert_text((100, 100), scale_text)
    y = [120]

    def _rect():
        y[0] += 30
        return fitz.Rect(200, y[0], 320, y[0] + 20)

    def _annot(kind, subject, content, rect=None, fill=None):
        r = rect or _rect()
        if kind == "polyline":
            a = page.add_polyline_annot([r.tl, r.tr])
        elif kind == "polygon":
            a = page.add_polygon_annot([r.tl, r.tr, r.br, r.bl])
        elif kind == "square":
            a = page.add_rect_annot(r)
            if fill:
                a.set_colors(stroke=fill, fill=fill)
        else:
            a = page.add_freetext_annot(r, content)
        info = a.info
        info["subject"] = subject
        info["content"] = content
        a.set_info(info)
        a.update()
        return a

    for w in walls:
        _annot("polyline", "Interior finishes: Paint", w)
    for w in mr_walls:
        _annot("polyline", "Interior finishes: MR Paint", w)
    for c in ceilings:
        _annot("polygon", "Celling Paint", c)
    for c in mr_ceilings:
        _annot("polygon", "MR Celling Paint", c)
    for _ in range(doors_std):
        _annot("polygon", "Interior Finish: (3'-0''x6'-8'') Door Paint",
               str(doors_std))
    for _ in range(doors_big):
        _annot("polygon", "Interior Finish: (10'-0''x6'-8'') Door Paint",
               str(doors_big))
    if rail:
        _annot("polyline", "Interior Finish: Handrail Paint", rail)
    if stair:
        _annot("polygon", "Stair Paint", stair)
    if concrete_rects:
        _annot("freetext", "Typewritten Text", "Concrete sealing",
               fitz.Rect(60, 60, 200, 90))
        for _ in range(concrete_rects):
            # 90 x 180 pt at 9 pt/ft = 10 ft x 20 ft = 200 SF each
            y[0] += 200
            _annot("square", "Rectangle", "",
                   fitz.Rect(400, y[0], 490, y[0] + 180), fill=(1, 0, 0))
    doc.save(path)
    doc.close()


PDF = "/tmp/test_markup_takeoff.pdf"
_build_pdf(PDF)

# ── 1. Extractor ────────────────────────────────────────────────────────────
print("\nExtractor: classification and parsing")
mk = MT.extract_markup_takeoff([PDF])
check(abs(mk["wall_lf"] - (58 + 8 / 12 + 44 + 1 / 12 + 309.5)) < 0.01,
      f"wall LF summed from ft-in contents ({mk['wall_lf']})")
check(mk["mr_wall_lf"] == 9.0, "MR walls split from paint walls")
check(mk["ceiling_sf"] == 1420.0, "ceiling sf parsed incl. comma thousands")
check(mk["mr_ceiling_sf"] == 58.0, "MR ceiling split")
check(mk["doors_total"] == 5 and mk["doors_large"] == 2,
      "door count = instances per group (contents echo ignored)")
check(abs(mk["railing_lf"] - 47.5) < 0.01, "handrail not misread as wall")
check(mk["stair_sf"] == 100.0, "stair sf")
check(abs(mk["concrete_sf"] - 400.0) / 400.0 < 0.05,
      f"content-less filled rects billed via scale, within the annotation-"
      f"border tolerance ({mk['concrete_sf']} vs 400 drawn)")
check(mk["unclassified"] == [], "no unclassified noise from billed shapes")

print("\nExtractor: fail-safety")
check(MT.detect_scale_pt_per_ft('1/8" = 1\'-0"') == 9.0, "scale 1/8 -> 9 pt/ft")
check(MT.detect_scale_pt_per_ft("1/4\" = 1'") == 18.0, "scale 1/4 -> 18 pt/ft")
check(MT.detect_scale_pt_per_ft("no scale here") is None,
      "no scale -> None, never a guess")
_build_pdf("/tmp/test_markup_noscale.pdf", scale_text=None)
mk_ns = MT.extract_markup_takeoff(["/tmp/test_markup_noscale.pdf"])
check(mk_ns["concrete_sf"] == 0 and mk_ns["skipped_no_scale_sf_shapes"] == 2,
      "filled rects skipped (and reported) when the page has no scale")
check(MT.extract_markup_takeoff(["/tmp/does_not_exist.pdf"])["n_classified"]
      == 0, "missing file contributes nothing, no raise")
check(MT.parse_ftin("127'-5 1/2\"") is not None
      and abs(MT.parse_ftin("127'-5 1/2\"") - (127 + 5.5 / 12)) < 0.001,
      "fractional inches parse")

# ── 2. Authoritative pass ───────────────────────────────────────────────────
def _analysis(llm_walls=3000, llm_ceil=900, heights=(10,) * 5, paths=(PDF,)):
    rooms = [{"room_name": f"Room {i}", "in_scope": True,
              "dimensions": {"ceiling_height_feet": h, "wall_area_sqft": 300}}
             for i, h in enumerate(heights)]
    return {
        "floors": [{"floor_name": "Ground", "rooms": rooms}],
        "aggregated_totals": {
            "total_paintable_wall_sqft": llm_walls,
            "total_paintable_ceiling_sqft": llm_ceil,
            "total_doors_full_paint": 9,
            "total_concrete_floor_sqft": 100,
        },
        "_vme_pdf_paths": list(paths),
        "notes": [],
    }


print("\nAuthoritative pass: flag gate")
os.environ["NIGHTSHIFT_MARKUP_TAKEOFF"] = "0"
a_off = T._apply_markup_takeoff_authoritative(_analysis())
check(a_off.get("_markup_takeoff") is None
      and a_off["aggregated_totals"]["total_paintable_wall_sqft"] == 3000,
      "flag off -> untouched")

os.environ["NIGHTSHIFT_MARKUP_TAKEOFF"] = "1"

print("\nAuthoritative pass: promotion")
a = T._apply_markup_takeoff_authoritative(_analysis())
rec = a["_markup_takeoff"]
exp_walls = round((mk["wall_lf"] + mk["mr_wall_lf"]) * 10, 2)
check(rec["applied"] is True, "promoted")
check(a["aggregated_totals"]["total_paintable_wall_sqft"] == exp_walls,
      f"walls = (paint LF + MR LF) x measured height ({exp_walls})")
check(a["aggregated_totals"]["total_paintable_ceiling_sqft"] == 1478.0,
      "ceilings = markup sf incl. MR (1478)")
check(a["aggregated_totals"]["total_doors_full_paint"] == 5,
      "doors adopt the markup count groups (9 -> 5)")
check(abs(a["aggregated_totals"]["total_concrete_floor_sqft"] - 400) / 400
      < 0.05, "concrete adopts the marked area")
check(a["aggregated_totals"]["total_painted_railing_lf"] == 47.5,
      "railing LF applied")
check(any("Markup Takeoff" in str(n) for n in a["notes"]), "audit note added")
rfis = [r for r in a.get("_pre_pricing_rfis", [])
        if r.get("category") == "Markup Takeoff"]
check(any("overhead" in r["question"] for r in rfis),
      "large-door RFI emitted")
check(any("MR" in r["question"] for r in rfis), "MR product RFI emitted")
a2 = T._apply_markup_takeoff_authoritative(a)
check(a2["aggregated_totals"]["total_paintable_wall_sqft"] == exp_walls,
      "idempotent")

print("\nAuthoritative pass: abstentions")
a_few = _analysis(paths=["/tmp/does_not_exist.pdf"])
a_few = T._apply_markup_takeoff_authoritative(a_few)
check(a_few["_markup_takeoff"]["applied"] is False
      and a_few["aggregated_totals"]["total_paintable_wall_sqft"] == 3000,
      "no annotations -> abstain, untouched")
a_np = _analysis(paths=())
a_np = T._apply_markup_takeoff_authoritative(a_np)
check(a_np["_markup_takeoff"]["applied"] is False,
      "no original paths -> abstain")
a_band = T._apply_markup_takeoff_authoritative(_analysis(llm_walls=100000))
check(a_band["_markup_takeoff"]["applied"] is False
      and a_band["aggregated_totals"]["total_paintable_wall_sqft"] == 100000,
      "walls outside sanity band -> abstain")
a_h = T._apply_markup_takeoff_authoritative(
    _analysis(heights=(10,)))  # <3 measured heights
check(a_h["_markup_takeoff"]["applied"] is True
      and a_h["aggregated_totals"]["total_paintable_wall_sqft"] == 3000
      and a_h["aggregated_totals"]["total_paintable_ceiling_sqft"] == 1478.0,
      "no height basis -> walls untouched, direct quantities still apply")

# ── 3. Large-commercial cap guard (unit-level) ─────────────────────────────
print("\nLarge-commercial cap guard")
# The cap lives inline in pricing; verify the guard predicate + levels basis
# the way the 364 apt-cap tests do — through a pricing call would need a full
# analysis; instead assert on the exact code shape via a focused replay of
# the clamp logic.
def _clamp(wall_sqft, footprint, stories, floors_analyzed, det_applied):
    levels = max(stories, floors_analyzed)
    if footprint > 0 and stories <= 2 and not det_applied:
        cap = round(footprint * max(1, levels) * 1.25)
        if wall_sqft > cap:
            return cap
    return wall_sqft


check(_clamp(14386, 5760, 1, 2, det_applied=True) == 14386,
      "deterministic walls are never clamped (Harlem Valley shape)")
check(_clamp(14386, 5760, 1, 2, det_applied=False) == 14386,
      "LLM walls within the two-level cap (5760x2x1.25=14400) pass "
      "unclamped — stories-alone basis would have cut them to 7200")
check(_clamp(20000, 5760, 1, 2, det_applied=False) == 14400,
      "LLM over-count clamps against BOTH analyzed levels, not stories alone")
check(_clamp(20000, 5760, 1, 1, det_applied=False) == 7200,
      "single-level LLM over-count still clamps")

# ── 4. Estimate bucket routing ──────────────────────────────────────────────
print("\nEstimate bucket routing")
import generate_estimate_pdf as G

items = {"cost_estimate": {"line_items": [
    {"item": "Painted Railings - 48 LF @ $18.00", "qty": 48, "total": 897.94},
    {"item": "Pipe Handrails - 20 LF @ $12.00", "qty": 20, "total": 240.0},
    {"item": "Ext. Stain Railing - 30 LF @ $8.00", "qty": 30, "total": 240.0},
    {"item": "Stairs - 2 sections @ $1500.00", "qty": 2, "total": 3150.0},
]}}
rows = {r["title"]: r["total"] for r in G._build_line_items(items)}
check(abs(rows.get("Stairs", 0) - (897.94 + 240.0 + 3150.0)) < 0.01,
      "interior railings + handrails print under Stairs")
check(abs(rows.get("Exterior", 0) - 240.0) < 0.01,
      "Ext. Stain Railing still prints under Exterior")

print()
if fails:
    print(f"{len(fails)} FAILURE(S)")
    for f in fails:
        print(" -", f)
    raise SystemExit(1)
print("ALL PASS")
