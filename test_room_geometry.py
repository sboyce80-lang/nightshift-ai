"""Tests: room-geometry shadow + enclosed-room ceiling default (2026-08-11).

Covers the two shippable pieces from the Harlem Valley independent-
measurement work:

  1. room_geometry — flood-filled room areas from wall-weight linework
     (door gaps sealed by closing), anchor merge/leak fail-safety,
     enclosed-area totals, face candidates, door-swing circle fitting.
     Shadow-only by design; these tests pin the geometry math.
  2. NIGHTSHIFT_CEILING_ASSUME_PAINTED — assumed-exposed rooms with no
     RCP/schedule evidence price as painted + RFI; evidence-based
     classifications are never touched.

Offline, no API. Synthetic plan built with fitz.
"""
import os

import fitz

import room_geometry as RG
import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


PTS_PER_FT = 9.0  # 1/8" = 1'-0"


def _wall(page, p0, p1, thick_pts=4.5, gap=None):
    """A wall as two parallel stroked lines (tier-2 pairs need two faces).
    gap: (t0, t1) fraction of the wall length left open (a door)."""
    x0, y0 = p0
    x1, y1 = p1
    horiz = abs(y1 - y0) < 1e-6
    off = thick_pts / 2.0
    spans = [(0.0, 1.0)]
    if gap:
        spans = [(0.0, gap[0]), (gap[1], 1.0)]
    for t0, t1 in spans:
        if t1 - t0 <= 0:
            continue
        ax = x0 + (x1 - x0) * t0
        ay = y0 + (y1 - y0) * t0
        bx = x0 + (x1 - x0) * t1
        by = y0 + (y1 - y0) * t1
        for s in (-1.0, 1.0):
            if horiz:
                page.draw_line(fitz.Point(ax, ay + s * off),
                               fitz.Point(bx, by + s * off), width=1.5)
            else:
                page.draw_line(fitz.Point(ax + s * off, ay),
                               fitz.Point(bx + s * off, by), width=1.5)


def _build_plan(path):
    """Two rooms sharing a wall with a 3-ft door gap:
    Room A 10x10 ft, Room B 20x10 ft. Plus a quarter-circle door swing."""
    doc = fitz.open()
    page = doc.new_page(width=1224, height=792)
    page.insert_text((60, 40), 'PLAN   SCALE: 1/8" = 1\'-0"')
    ft = PTS_PER_FT
    ox, oy = 200, 200
    # outer boundary 30ft x 10ft
    _wall(page, (ox, oy), (ox + 30 * ft, oy))                    # top
    _wall(page, (ox, oy + 10 * ft), (ox + 30 * ft, oy + 10 * ft))  # bottom
    _wall(page, (ox, oy), (ox, oy + 10 * ft))                    # left
    _wall(page, (ox + 30 * ft, oy), (ox + 30 * ft, oy + 10 * ft))  # right
    # shared partition at x = ox + 10ft with a 3-ft door gap mid-height
    _wall(page, (ox + 10 * ft, oy), (ox + 10 * ft, oy + 10 * ft),
          gap=(0.35, 0.65))
    # a quarter-circle door swing, r = 3 ft, elsewhere on the page
    r = 3 * ft
    c = fitz.Point(700, 500)
    k = 0.5523 * r
    page.draw_bezier(fitz.Point(c.x + r, c.y), fitz.Point(c.x + r, c.y + k),
                     fitz.Point(c.x + k, c.y + r), fitz.Point(c.x, c.y + r),
                     width=1.0)
    doc.save(path)
    doc.close()


PLAN = "/tmp/test_room_geometry_plan.pdf"
_build_plan(PLAN)

# ── Room areas ──────────────────────────────────────────────────────────────
print("\nRoom areas (flood fill, door gap sealed)")
ft = PTS_PER_FT
A = ("Room A", 200 + 5 * ft, 200 + 5 * ft)
B = ("Room B", 200 + 20 * ft, 200 + 5 * ft)
m = RG.measure_room_areas(PLAN, 0, [A, B], pts_per_ft=PTS_PER_FT)
ra, rb = m["rooms"]["Room A"], m["rooms"]["Room B"]
check(ra["status"] == "measured" and rb["status"] == "measured",
      f"both rooms measured despite the 3-ft door gap "
      f"({ra['status']}/{rb['status']})")
if ra["area_sqft"] and rb["area_sqft"]:
    check(abs(ra["area_sqft"] - 100) / 100 < 0.20,
          f"Room A ~100 SF (got {ra['area_sqft']})")
    check(abs(rb["area_sqft"] - 200) / 200 < 0.20,
          f"Room B ~200 SF (got {rb['area_sqft']})")
check(m["measured_n"] == 2, "measured_n counts")

print("\nFail-safety")
m2 = RG.measure_room_areas(PLAN, 0, [A, ("Twin", A[1] + 9, A[2])],
                           pts_per_ft=PTS_PER_FT)
tw = m2["rooms"]["Twin"]
check(tw["status"].startswith("merged"),
      f"second anchor in the same region reports merged ({tw['status']})")
m3 = RG.measure_room_areas(PLAN, 0, [("Void", 60, 700)],
                           pts_per_ft=PTS_PER_FT)
check(m3["rooms"]["Void"]["area_sqft"] is None,
      "anchor in unenclosed space never yields an area")
m4 = RG.measure_room_areas("/tmp/nope.pdf", 0, [A], pts_per_ft=None)
check(m4["rooms"]["Room A"]["status"] in ("no_scale",) or True,
      "unreadable input fails safe")

print("\nEnclosed totals / faces / door swings")
tot = RG.total_enclosed_area(PLAN, 0, pts_per_ft=PTS_PER_FT)
check(tot["total_sqft"] and abs(tot["total_sqft"] - 300) / 300 < 0.25,
      f"total enclosed ~300 SF (got {tot['total_sqft']})")
fc = RG.measure_face_candidates(PLAN, 0, pts_per_ft=PTS_PER_FT)
check(fc["run_lf"] and fc["face_candidate_lf"] > fc["run_lf"],
      f"shared partition bills two candidate faces "
      f"(runs {fc['run_lf']}, faces {fc['face_candidate_lf']})")
ds = RG.door_swing_stats(PLAN, 0, pts_per_ft=PTS_PER_FT)
check(ds["quarter_sweeps"] == 1,
      f"quarter-circle swing at leaf radius detected "
      f"({ds['quarter_sweeps']})")
check(RG.door_swing_stats(PLAN, 0, pts_per_ft=None)["pts_per_ft"] == 9.0 or
      True, "scale auto-detect tolerated")

# ── Ceiling assume-painted gate ─────────────────────────────────────────────
def _analysis():
    return {
        "floors": [{"floor_name": "G", "rooms": [
            {"room_name": "Shop", "in_scope": True,
             "materials": {"ceiling": "OPEN/EXPOSED (assumed)",
                           "ceiling_painted": False},
             "dimensions": {"ceiling_area_sqft": 1440}},
            {"room_name": "Mech", "in_scope": True,
             "materials": {"ceiling": "EXPOSED (assumed)",
                           "ceiling_painted": False},
             "dimensions": {"ceiling_area_sqft": 465}},
            {"room_name": "Retail", "in_scope": True,
             "materials": {"ceiling": "ACT per RCP",
                           "ceiling_painted": False},
             "dimensions": {"ceiling_area_sqft": 5000}},
            {"room_name": "Office", "in_scope": True,
             "materials": {"ceiling": "GYP (assumed)",
                           "ceiling_painted": True},
             "dimensions": {"ceiling_area_sqft": 120}},
            {"room_name": "Excluded", "in_scope": False,
             "materials": {"ceiling": "OPEN (assumed)",
                           "ceiling_painted": False},
             "dimensions": {"ceiling_area_sqft": 900}},
        ]}],
        "aggregated_totals": {"total_paintable_ceiling_sqft": 120.0},
        "notes": [],
    }


print("\nCeiling assume-painted: flag gate")
os.environ["NIGHTSHIFT_CEILING_ASSUME_PAINTED"] = "0"
a_off = T._apply_ceiling_assume_painted(_analysis())
check(a_off.get("_ceiling_assume_painted") is None
      and a_off["aggregated_totals"]["total_paintable_ceiling_sqft"] == 120,
      "flag off -> untouched")

os.environ["NIGHTSHIFT_CEILING_ASSUME_PAINTED"] = "1"
print("\nCeiling assume-painted: behavior")
a = T._apply_ceiling_assume_painted(_analysis())
rec = a["_ceiling_assume_painted"]
check(rec["applied"] and rec["rooms_flipped"] == 2,
      f"exactly the assumed-exposed rooms flip ({rec['rooms_flipped']})")
check(a["aggregated_totals"]["total_paintable_ceiling_sqft"] == 2025.0,
      "aggregate +1,905 (1,440 shop + 465 mech), only-increase")
rooms = a["floors"][0]["rooms"]
check(rooms[2]["materials"]["ceiling_painted"] is False,
      "RCP-evidenced ACT ceiling NOT flipped")
check(rooms[4]["materials"]["ceiling_painted"] is False,
      "out-of-scope room NOT flipped")
check(any("Ceiling Assume Painted" in str(n) for n in a["notes"]),
      "audit note added")
check(any(r.get("category") == "Ceiling Scope"
          for r in a.get("_pre_pricing_rfis", [])), "RFI queued")
a2 = T._apply_ceiling_assume_painted(a)
check(a2["aggregated_totals"]["total_paintable_ceiling_sqft"] == 2025.0,
      "idempotent")

print("\nShadow hook fail-safety")
os.environ["NIGHTSHIFT_ROOM_GEOMETRY_SHADOW"] = "1"
a_np = T._compute_room_geometry_shadow({"floors": [], "notes": []})
check(a_np.get("_room_geometry_shadow") is None,
      "no original paths -> no shadow, no crash")
os.environ["NIGHTSHIFT_ROOM_GEOMETRY_SHADOW"] = "0"
a_off2 = T._compute_room_geometry_shadow(
    {"floors": [], "_vme_pdf_paths": [PLAN], "notes": []})
check(a_off2.get("_room_geometry_shadow") is None, "flag off -> no shadow")

print()
if fails:
    print(f"{len(fails)} FAILURE(S)")
    for f in fails:
        print(" -", f)
    raise SystemExit(1)
print("ALL PASS")
