"""Offline tests for jw_markups — JW-style estimator markups (write side).

Run as `python test_jw_markups.py` (the convention CI auto-discovers).

Covers the three things that make these markups trustworthy:
  1. the grammar round-trips through our own reader (markup_takeoff),
  2. geometry is never asserted where we did not measure it, and
  3. the output survives rotated sheets, which most plan sets are.
"""

import os
import tempfile

import numpy as np

import fitz

import jw_markups as jm
import markup_takeoff as mt

fails = []


def check(cond, label):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        fails.append(label)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ── 1. Quantity formatting must be readable by our own parser ───────────────
print("Quantity formatting round-trips through markup_takeoff")

for feet in (3.0, 9.5, 58.667, 127.4583, 0.5):
    parsed = mt.parse_ftin(jm.fmt_ftin(feet))
    check(parsed is not None and abs(parsed - feet) < 1 / 12.0 + 1e-6,
          f"ft-in {feet} -> {jm.fmt_ftin(feet)} parses back")

check(jm.fmt_ftin(8.9999) == "9'-0\"", "11.99 inches carries into the next foot")

for sf in (51.83, 1356.0, 3.2, 12000.0):
    parsed = mt._parse_sf(jm.fmt_sf(sf))
    check(parsed is not None and abs(parsed - round(sf)) < 1.0,
          f"area {sf} -> {jm.fmt_sf(sf)} parses back")

check(jm.fmt_scale(9.0) == "1/8\" = 1'-0\"", "9 pt/ft prints as 1/8\" scale")
check(jm.fmt_scale(18.0) == "1/4\" = 1'-0\"", "18 pt/ft prints as 1/4\" scale")


# ── 2. Contour tracing ──────────────────────────────────────────────────────
print("\nContour tracing")


def traced_ratio(mask):
    poly = jm.contour_to_polygon(mask, px_per_pt=1.0, simplify_ft=0.01,
                                 px_per_ft=1.0)
    return poly, jm.polygon_area_sqft(poly, 1.0) / max(1, mask.sum())


rect = np.zeros((40, 60), dtype=bool)
rect[5:35, 10:50] = True
poly, ratio = traced_ratio(rect)
check(len(poly) == 4, "a rectangle traces to four corners")
check(0.85 <= ratio <= 1.05, f"rectangle area recovered (ratio {ratio:.2f})")

ell = rect.copy()
ell[5:20, 30:50] = False
_, ratio = traced_ratio(ell)
# The bounding box would be 1200 px; the true L is 900.
check(0.85 <= ratio <= 1.05, f"L-shape traced, not its bbox (ratio {ratio:.2f})")

neck = np.zeros((60, 60), dtype=bool)
neck[5:25, 5:25] = True
neck[25:30, 14:16] = True
neck[30:50, 5:25] = True
_, ratio = traced_ratio(neck)
check(0.85 <= ratio <= 1.05,
      f"boundary revisiting the start does not stop early (ratio {ratio:.2f})")

disc = np.zeros((50, 50), dtype=bool)
yy, xx = np.ogrid[:50, :50]
disc[(yy - 25) ** 2 + (xx - 25) ** 2 < 400] = True
_, ratio = traced_ratio(disc)
check(0.85 <= ratio <= 1.05, f"curved region traced (ratio {ratio:.2f})")

check(jm.trace_boundary(np.zeros((10, 10), dtype=bool)) == [],
      "empty mask traces to nothing")
check(jm.contour_to_polygon(np.zeros((10, 10), dtype=bool), 1.0) == [],
      "empty mask yields no polygon")


# ── 3. Anchor hygiene — never place scope on a degenerate anchor ────────────
print("\nAnchor hygiene")

PAGE = fitz.Rect(0, 0, 1000, 800)

for norm, label in (
    (None, "missing anchor"),
    ([0.3, 1.0, 0.32, 1.0], "anchor collapsed onto the bottom edge"),
    ([0.3, 0.5, 0.3, 0.5], "zero-extent anchor"),
    ([0.0, 0.0, 0.0005, 0.0005], "anchor pinned to the page corner"),
):
    check(jm.anchor_point({"bbox": {"label_bbox_norm": norm}}, PAGE) is None,
          f"{label} is refused")

pt = jm.anchor_point({"bbox": {"label_bbox_norm": [0.2, 0.4, 0.3, 0.5]}}, PAGE)
check(pt is not None and approx(pt[0], 250.0) and approx(pt[1], 360.0),
      "a real anchor resolves to its center")

nominal = jm._nominal_rect(
    {"bbox": {"label_bbox_norm": [0.49, 0.49, 0.51, 0.51]},
     "dimensions": {"length_feet": 20, "width_feet": 10}}, PAGE, 9.0)
xs = [p[0] for p in nominal]
ys = [p[1] for p in nominal]
check(len(nominal) == 4 and approx(max(xs) - min(xs), 180.0)
      and approx(max(ys) - min(ys), 90.0),
      "nominal box is the priced size at page scale (20x10 ft @ 9 pt/ft)")
check(jm._nominal_rect({"bbox": {"label_bbox_norm": [0.49, 0.49, 0.51, 0.51]},
                        "dimensions": {}}, PAGE, 9.0) == [],
      "no nominal box without priced dimensions")


# ── 4. A trace must corroborate the number it is labeled with ──────────────
print("\nTraced outline must agree with the priced quantity")


def square(side_ft, pts_per_ft):
    s = side_ft * pts_per_ft
    return [(0, 0), (s, 0), (s, s), (0, s)]


HUNDRED_SF = square(10, 9.0)
check(jm.trace_agrees_with_priced(HUNDRED_SF, {"dimensions":
                                               {"floor_area_sqft": 110}}, 9.0),
      "trace accepted when it agrees with the priced area")
for priced in (10, 5000):
    check(not jm.trace_agrees_with_priced(
        HUNDRED_SF, {"dimensions": {"floor_area_sqft": priced}}, 9.0),
        f"leaked trace rejected against priced {priced} sf")
check(not jm.trace_agrees_with_priced(HUNDRED_SF, {}, 9.0),
      "trace rejected with no priced area to check against")


# ── 5. Scope planning ───────────────────────────────────────────────────────
print("\nScope planning")

POLY = [(0, 0), (90, 0), (90, 90), (0, 90)]


def room(**over):
    r = {
        "room_name": "Exam 01", "in_scope": True, "unit_multiplier": 1,
        "dimensions": {"length_feet": 10, "width_feet": 10,
                       "ceiling_height_feet": 9, "floor_area_sqft": 100,
                       "ceiling_area_sqft": 100, "perimeter_lf": 40,
                       "wall_area_sqft": 360},
        "materials": {"walls": "GYP", "ceiling": "GYP",
                      "ceiling_painted": True, "base": "WD-2"},
        "elements": {"base_trim_lf": 40, "doors_full_paint": 2},
    }
    r.update(over)
    return r


markups, unplaced = jm.plan_room_markups(room(), POLY, False, 9.0)
subjects = {m["subject"] for m in markups}
check(any(s.startswith("Ceiling Paint") for s in subjects), "ceiling becomes a shape")
check(any("Wall Paint" in s for s in subjects), "walls become a shape")
check(any(s.startswith("Base Trim") for s in subjects), "base trim becomes a shape")
check(any(u["subject"].startswith("Door Paint") for u in unplaced),
      "door counts are reported in the legend")
check(not any("Door" in m["subject"] for m in markups),
      "door counts are never drawn as geometry")

wall = next(m for m in markups if "Wall Paint" in m["subject"])
check(wall["subject"].startswith("9'-0\" H Wall Paint"),
      "wall subject carries the height, like JW's own markups")
check(wall["contents"] == "40'-0\"", "wall contents is the measured run")
check(wall["qty"] == 360, "wall quantity is the priced area")

unpainted = room()
unpainted["materials"]["ceiling_painted"] = False
markups2, unplaced2 = jm.plan_room_markups(unpainted, POLY, False, 9.0)
check(not any("Ceiling" in m["subject"] for m in markups2),
      "an unpainted ceiling is not drawn as scope")
check(any("not painted" in u["subject"] for u in unplaced2),
      "an unpainted ceiling is still reported")

markups3, unplaced3 = jm.plan_room_markups(room(unit_multiplier=4), POLY,
                                           False, 9.0)
ceiling = next(m for m in markups3 if "Ceiling" in m["subject"])
doors = next(u for u in unplaced3 if u["subject"].startswith("Door Paint"))
check(ceiling["qty"] == 400, "shape quantities scale with the unit multiplier")
check(doors["qty"] == 8, "legend quantities scale with the unit multiplier")

markups4, unplaced4 = jm.plan_room_markups(room(), [], False, 9.0)
check(markups4 == [], "no geometry means nothing is drawn")
check(len(unplaced4) >= 3,
      "every scope falls through to the legend when there is no geometry")

assumed = room()
assumed["materials"]["walls"] = "GYP (assumed)"
wall_a = next(m for m in jm.plan_room_markups(assumed, POLY, False, 9.0)[0]
              if "Wall Paint" in m["subject"])
check("RFI" in wall_a["popup"], "assumed substrate is flagged in the popup")

longmat = room()
longmat["materials"]["walls"] = "INSULATED PANEL - 4\" POLYURETHANE"
wall_l = next(m for m in jm.plan_room_markups(longmat, POLY, False, 9.0)[0]
              if "Wall Paint" in m["subject"])
check(len(wall_l["subject"]) < 45,
      "a spec-line substrate is stubbed so subjects stay sortable")

excluded = jm.plan_excluded_markup(
    room(in_scope=False, scope_exclusion_reason="template-instance dedup"),
    POLY, False)
check(excluded["excluded"] is True, "excluded rooms are marked excluded")
check("template-instance dedup" in excluded["popup"]
      and "NOT PRICED" in excluded["popup"],
      "an excluded room carries its reason on the sheet")
check(jm.plan_excluded_markup(room(in_scope=False), [], False) is None,
      "an excluded room with no geometry draws nothing")


# ── 6. Emission: real annotations in Bluebeam's grammar ────────────────────
print("\nAnnotation emission")


def one_page(rotation=0, width=1224, height=792):
    doc = fitz.open()
    page = doc.new_page(width=width, height=height)
    if rotation:
        page.set_rotation(rotation)
    return doc, page


doc, page = one_page()
measure = jm.make_measure_xref(doc, 9.0)
annot = jm.add_markup(doc, page, {
    "kind": "polygon", "points": POLY, "style_key": "PT-1",
    "subject": "Wall Paint PT-1", "contents": "468 sf", "qty": 468,
    "unit": "sf", "popup": "Exam 01", "measure": 129}, "KnightShiftAI", measure)
raw = doc.xref_object(annot.xref) if annot else ""
for key in ("/Subj (Wall Paint PT-1)", "/Contents (468 sf)",
            "/IT /PolygonDimension", "/MeasurementTypes 129", "/Measure ",
            "/FillOpacity"):
    check(key in raw, f"emitted annotation carries {key.strip()}")
doc.close()

doc, _ = one_page()
mraw = doc.xref_object(jm.make_measure_xref(doc, 9.0))
check("/Subtype /RL" in mraw and "/U (sf)" in mraw,
      "/Measure declares a real-length subtype and area unit")
check(".111111" in mraw, "/Measure encodes feet-per-point (1/9)")
check(jm.make_measure_xref(doc, None) is None,
      "no /Measure dictionary without a detected scale")
doc.close()

doc, page = one_page()
check(jm.add_markup(doc, page, {
    "kind": "polygon", "points": [(0, 0), (1, 1)], "style_key": "PT-1",
    "subject": "Wall Paint PT-1", "contents": "1 sf", "qty": 1, "unit": "sf",
    "measure": 129}, "KnightShiftAI", None) is None,
    "a degenerate shape emits nothing")
doc.close()

check(jm.scope_style("PT-1") == jm.scope_style("pt-1"),
      "scope colors are case-insensitive")
check(jm.scope_style("ODDCODE") == jm.scope_style("ODDCODE"),
      "an unknown code gets a stable color across runs")
check(jm.scope_style("PT-1") != jm.scope_style("PT-4"),
      "different finish codes get different colors")


# ── 7. Legend ───────────────────────────────────────────────────────────────
print("\nLegend")

for rotation in (0, 90, 180, 270):
    doc, page = one_page(rotation)
    rect_l = jm.draw_legend(page, {("Wall Paint PT-1", "sf", "PT-1"): 2849.0},
                            {("Door Paint - full", "ea"): 6.0})
    ok = (rect_l is not None and approx(rect_l.x0, 24, 0.01)
          and approx(rect_l.y1, page.rect.height - 24, 0.01)
          and rect_l.width > 0 and rect_l.height > 0)
    check(ok, f"legend lands at the visible bottom-left at rotation {rotation}")
    doc.close()

doc, page = one_page()
check(jm.draw_legend(page, {}, {}) is None, "no legend without quantities")
doc.close()


# ── 8. End to end ───────────────────────────────────────────────────────────
print("\nEnd-to-end rendering")


def analysis(rooms):
    return {"floors": [{"floor_name": "First Floor", "rooms": rooms}]}


def write_plans(path, pages=2, rotation=0):
    """A minimal sheet carrying a scale note — without a scale there is no way
    to size a shape in feet."""
    d = fitz.open()
    for _ in range(pages):
        pg = d.new_page(width=1224, height=792)
        pg.insert_text((40, 60), "SCALE: 1/8\" = 1'-0\"", fontsize=11)
        if rotation:
            pg.set_rotation(rotation)
    d.save(path)
    d.close()


ANCHORED = {"label_bbox_norm": [0.4, 0.4, 0.42, 0.42],
            "page_size_pt": [1224, 792]}

with tempfile.TemporaryDirectory() as tmp:
    src = os.path.join(tmp, "plans.pdf")
    out = os.path.join(tmp, "markups.pdf")
    write_plans(src)
    summary = jm.render_markup_pdf(src, analysis([
        room(source_page=1, bbox=dict(ANCHORED)),
        room(source_page=1, room_name="No Anchor",
             bbox={"label_bbox_norm": None}),
    ]), out)
    check(summary["markups"] > 0, "renders annotations for an anchored room")
    check(summary["unplaced"] > 0, "reports the room it could not place")
    check(os.path.getsize(out) > 0, "writes a non-empty PDF")
    d = fitz.open(out)
    subs = [a.info.get("subject") for a in (d[0].annots() or [])]
    check(any("Wall Paint" in (s or "") for s in subs),
          "emitted annotations are readable from the output PDF")
    d.close()

with tempfile.TemporaryDirectory() as tmp:
    src = os.path.join(tmp, "plans.pdf")
    out = os.path.join(tmp, "markups.pdf")
    write_plans(src)
    # Anchors normalized against a different sheet size must not be drawn.
    summary = jm.render_markup_pdf(src, analysis([
        room(source_page=1, bbox={"label_bbox_norm": [0.4, 0.4, 0.42, 0.42],
                                  "page_size_pt": [2592, 1728]})]), out)
    check(summary["skipped_size_mismatch"] == 1,
          "a page whose size differs from extraction time is skipped")
    check(summary["markups"] == 0, "no scope is drawn on a mismatched sheet")

with tempfile.TemporaryDirectory() as tmp:
    src = os.path.join(tmp, "plans.pdf")
    out = os.path.join(tmp, "markups.pdf")
    write_plans(src)
    summary = jm.render_markup_pdf(src, analysis([
        room(source_page=1, in_scope=False,
             scope_exclusion_reason="template-instance dedup",
             bbox=dict(ANCHORED))]), out)
    check(summary["excluded_shown"] == 1, "an excluded room is shown")
    d = fitz.open(out)
    subs = [a.info.get("subject") for a in (d[0].annots() or [])]
    check(subs == ["Excluded from scope"],
          "an excluded room is never emitted as priced scope")
    d.close()

with tempfile.TemporaryDirectory() as tmp:
    src = os.path.join(tmp, "plans.pdf")
    out = os.path.join(tmp, "markups.pdf")
    write_plans(src, rotation=270)
    summary = jm.render_markup_pdf(src, analysis([
        room(source_page=1, bbox={"label_bbox_norm": [0.4, 0.4, 0.42, 0.42],
                                  "page_size_pt": [792, 1224]})]), out)
    check(summary["markups"] > 0, "rotated sheets still get markups")
    check(os.path.getsize(out) > 0, "rotated sheet output is non-empty")

with tempfile.TemporaryDirectory() as tmp:
    src = os.path.join(tmp, "plans.pdf")
    out = os.path.join(tmp, "markups.pdf")
    write_plans(src)
    jm.render_markup_pdf(src, analysis([room(source_page=1,
                                             bbox=dict(ANCHORED))]), out)
    back = mt.extract_markup_takeoff([out])
    check(back["n_classified"] > 0, "our markups classify through our own reader")
    check(back["ceiling_sf"] > 0, "ceiling area survives the round trip")
    check(back["wall_lf"] > 0, "wall run survives the round trip")


# ── 9. Flag gating ──────────────────────────────────────────────────────────
print("\nFlag gating")

import bbox_spike

os.environ.pop("NIGHTSHIFT_JW_STYLE_MARKUPS", None)
check(bbox_spike._jw_style_markups_enabled() is False,
      "NIGHTSHIFT_JW_STYLE_MARKUPS defaults off")
os.environ["NIGHTSHIFT_JW_STYLE_MARKUPS"] = "1"
check(bbox_spike._jw_style_markups_enabled() is True, "the flag turns it on")

try:
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "plans.pdf")
        out = os.path.join(tmp, "markups.pdf")
        write_plans(src)
        summary = bbox_spike.render_annotated_pdf(
            src, analysis([room(source_page=1, bbox=dict(ANCHORED))]), out)
        check(summary.get("style") == "jw_markups",
              "the flag routes render_annotated_pdf through the markup renderer")
        d = fitz.open(out)
        check("rooms anchored" not in d[0].get_text(),
              "the QA banner never reaches a customer-facing markup set")
        d.close()
finally:
    os.environ.pop("NIGHTSHIFT_JW_STYLE_MARKUPS", None)



# ── 10. Caller compatibility ───────────────────────────────────────────────
# Regression: jobs._build_and_upload_annotated_drawings logs summary keys by
# name inside a best-effort try/except. A missing key raised KeyError there,
# which swallowed the upload — the flag would have silently removed annotated
# drawings from every job instead of changing how they look.
print("\nCaller compatibility")

with tempfile.TemporaryDirectory() as tmp:
    src = os.path.join(tmp, "plans.pdf")
    out = os.path.join(tmp, "markups.pdf")
    write_plans(src)
    summary = jm.render_markup_pdf(src, analysis([
        room(source_page=1, bbox=dict(ANCHORED))]), out)
    for key in ("pages", "referenced_pages", "rooms_drawn", "misses_marked",
                "extraction_failures", "output_size_bytes"):
        check(key in summary, f"summary carries the legacy key {key!r}")

    # Exactly the format string jobs.py uses.
    try:
        ("%s/%s (%s): %d/%d pages marked, %d shapes, %d not placed, %.1f MB" % (
            "sid", "f.pdf", summary.get("style", "qa_overlay"),
            summary.get("referenced_pages", 0), summary.get("pages", 0),
            summary.get("rooms_drawn", 0), summary.get("misses_marked", 0),
            summary.get("output_size_bytes", 0) / 1024 / 1024))
        check(True, "the jobs.py log line formats against a markup summary")
    except Exception as exc:
        check(False, f"the jobs.py log line formats against a markup summary ({exc!r})")


print()
if fails:
    print(f"{len(fails)} FAILURE(S)")
    for f in fails:
        print(" -", f)
    raise SystemExit(1)
print("ALL PASS")
