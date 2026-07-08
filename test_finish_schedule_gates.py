"""Regression tests for the three deterministic finish-schedule gates
(2026-07-07, all default OFF pending a PNC rerun):

  NIGHTSHIFT_WC_SCHEDULE_GATE — wallcovering pays ONLY in rooms the machine-
      read finish schedule designates WC-x (PNC runs 2-4 invented up to $11.7k
      of WC; the I601 truth is ~103 SF on one pantry wall). Only-reduce; a
      designated-but-unquantified room gets an RFI, never a fabricated area.
  NIGHTSHIFT_BASE_TRIM_SCHEDULE_CONFIRM — the hard-numbers base-trim gate
      finally PAYS schedule-confirmed base: a commercial room whose schedule
      row says WB-x/wood base keeps (or derives) its perimeter LF instead of
      being zeroed as "unconfirmed" (Scott issue #3). RB-x still suppresses,
      including on residential (schedule beats the painted-wood default).
  NIGHTSHIFT_CEILING_SCHEDULE_TYPES — per-room ceiling TYPES come from the
      finish schedule, not the extractor's per-run read (PNC: 240 vs 2,400 SF
      GWB swing): schedule ACT/exposed demotes, schedule GWB confirms (strips
      the "(assumed)" marker so the 1b gate keeps it) and restores area from
      the measured floor area; the commercial aggregate rebuild may rise by at
      most the schedule-restored SF.

Guards tested: flags off = strict no-op; schedule thinner than 5 rows = no-op
(partial read must not zero real scope); duplicate schedule names never match
by name; residential exempt from the ceiling sub-gate. Offline, no API.
"""
import os
os.environ["NIGHTSHIFT_CEILING_SCOPE_GATE"] = "1"
os.environ["NIGHTSHIFT_WC_SCHEDULE_GATE"] = "1"
os.environ["NIGHTSHIFT_CEILING_SCHEDULE_TYPES"] = "1"
os.environ["NIGHTSHIFT_BASE_TRIM_SCHEDULE_CONFIRM"] = "1"
os.environ.setdefault("NIGHTSHIFT_COMMERCIAL_CEILING_GYP_GATE", "0")
import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def _row(num, name, wall="PT-1", ceil="ACT", base=""):
    return {"room_number": str(num), "room_name": name, "wall_finish": wall,
            "ceiling_finish": ceil, "base_finish": base, "floor_finish": ""}


def _sched6(**overrides):
    """Six schedule rows (>= authority threshold), individually overridable."""
    rows = [
        _row(101, "Office A"), _row(102, "Office B"), _row(103, "Corridor"),
        _row(104, "Break Room"), _row(105, "Pantry"), _row(106, "Storage"),
    ]
    for i, r in enumerate(rows):
        r.update(overrides.get(str(r["room_number"]), {}))
    return rows


def _room(num, name, wc=0, base_lf=0, perim=0, ceil_area=0, floor_area=0,
          painted=False, ceiling="ACT", notes="", in_scope=True):
    return {
        "room_id": str(num), "room_number": str(num), "room_name": name,
        "in_scope": in_scope,
        "materials": {"walls": "GYP", "ceiling": ceiling,
                      "ceiling_painted": painted},
        "dimensions": {"wall_area_sqft": 500, "floor_area_sqft": floor_area,
                       "ceiling_area_sqft": ceil_area, "perimeter_lf": perim,
                       "ceiling_height_feet": 9},
        "elements": {"wallcovering_sqft": wc, "base_trim_lf": base_lf},
        "notes": notes,
    }


def _an(rooms, sched, building_type="commercial", agg=None):
    return {
        "project_info": {"building_type": building_type},
        "room_finish_schedule": sched,
        "aggregated_totals": agg if agg is not None else {},
        "floors": [{"floor_name": "1", "rooms": rooms}],
    }


# ---------------------------------------------------------------------------
# WC schedule gate — only schedule-designated rooms keep wallcovering.
# ---------------------------------------------------------------------------
sched = _sched6(**{"105": {"wall_finish": "WC-1"}})
rooms = [
    _room(105, "Pantry", wc=103),          # designated -> kept
    _room(101, "Office A", wc=800),        # schedule says PT-1 -> zeroed
    _room(999, "Mystery Room", wc=400),    # not in schedule -> zeroed
]
a = _an(rooms, sched, agg={"total_wallcovering_sqft": 1303})
T._enforce_wallcovering_schedule_gate(a)
rec = a.get("_wc_schedule_gate", {})
check(rooms[0]["elements"]["wallcovering_sqft"] == 103,
      "WC: designated pantry lost its extracted 103 SF")
check(rooms[1]["elements"]["wallcovering_sqft"] == 0,
      "WC: non-designated office kept invented WC")
check(rooms[2]["elements"]["wallcovering_sqft"] == 0,
      "WC: unmatched room kept invented WC")
check(a["aggregated_totals"]["total_wallcovering_sqft"] == 103,
      f"WC: aggregate should be 103, got "
      f"{a['aggregated_totals']['total_wallcovering_sqft']}")
check(rec.get("zeroed_sqft") == 1200, f"WC: record zeroed_sqft {rec}")
check("[Wallcovering removed — schedule gate" in rooms[1]["notes"],
      "WC: zeroed room missing provenance note")
check(not a.get("_pre_pricing_rfis"),
      "WC: no RFI expected when the designated room is quantified")
# Idempotent: a second pass changes nothing.
T._enforce_wallcovering_schedule_gate(a)
check(a["aggregated_totals"]["total_wallcovering_sqft"] == 103,
      "WC: gate not idempotent")

# Designated but unquantified -> RFI, never fabricated area.
a = _an([_room(105, "Pantry", wc=0)],
        _sched6(**{"105": {"wall_finish": "WC-1 vinyl wallcovering"}}),
        agg={"total_wallcovering_sqft": 0})
T._enforce_wallcovering_schedule_gate(a)
check(a["floors"][0]["rooms"][0]["elements"]["wallcovering_sqft"] == 0,
      "WC: unquantified designated room must stay 0 (hard numbers)")
check(any(r["category"] == "Wallcovering"
          for r in a.get("_pre_pricing_rfis", [])),
      "WC: designated-but-unquantified room did not queue an RFI")

# Thin schedule (< 5 rows) -> no-op: a partial read must not zero real scope.
a = _an([_room(101, "Office A", wc=800)], [_row(101, "Office A")] * 3,
        agg={"total_wallcovering_sqft": 800})
T._enforce_wallcovering_schedule_gate(a)
check(a["floors"][0]["rooms"][0]["elements"]["wallcovering_sqft"] == 800,
      "WC: thin schedule must be a no-op")
check(a.get("_wc_schedule_gate", {}).get("noop") == "schedule_too_thin",
      "WC: thin-schedule noop not recorded")

# Flag off -> strict no-op.
os.environ["NIGHTSHIFT_WC_SCHEDULE_GATE"] = "0"
a = _an([_room(101, "Office A", wc=800)],
        _sched6(**{"105": {"wall_finish": "WC-1"}}),
        agg={"total_wallcovering_sqft": 800})
T._enforce_wallcovering_schedule_gate(a)
check(a["floors"][0]["rooms"][0]["elements"]["wallcovering_sqft"] == 800
      and "_wc_schedule_gate" not in a,
      "WC: flag off must be a strict no-op")
os.environ["NIGHTSHIFT_WC_SCHEDULE_GATE"] = "1"

# Duplicate schedule names must not match by name (six identical "Office"
# rows would otherwise apply one row's finishes to every Office).
sched = [_row(n, "Office", wall="WC-2" if n == 101 else "PT-1")
         for n in (101, 102, 103, 104, 105, 106)]
rooms = [_room("", "Office", wc=250)]  # no number -> name match only
a = _an(rooms, sched, agg={"total_wallcovering_sqft": 250})
T._enforce_wallcovering_schedule_gate(a)
check(rooms[0]["elements"]["wallcovering_sqft"] == 0,
      "WC: unmatched (ambiguous-name) room with WC must still be zeroed")

# Legend-style schedule (no room numbers — the Mazda pattern): the gate must
# be RFI-only. It never zeroes scope it can't prove undesignated, and the
# designated-but-never-extracted legend rows land in the RFI.
sched = [
    {"room_number": None, "room_name": n, "wall_finish": wf,
     "ceiling_finish": "", "base_finish": ""}
    for n, wf in (("General Showroom", "WC-1 or P-1 (Paint)"),
                  ("Customer Lounge", "WC-3"),
                  ("Conference Room", "P-4"),
                  ("Tech Breakroom", "P-9"),
                  ("Parts Counter", "P-1"),
                  ("Service Drive", "P-2"))]
rooms = [_room("", "Sales Floor", wc=600)]  # extractor-invented WC
a = _an(rooms, sched, agg={"total_wallcovering_sqft": 600})
T._enforce_wallcovering_schedule_gate(a)
rec = a.get("_wc_schedule_gate", {})
check(rooms[0]["elements"]["wallcovering_sqft"] == 600,
      "WC: legend schedule must not zero (RFI-only mode)")
check(rec.get("zeroing_enabled") is False,
      f"WC: legend schedule should disable zeroing, rec={rec}")
check(any(r["category"] == "Wallcovering"
          for r in a.get("_pre_pricing_rfis", [])),
      "WC: legend schedule with unmatched WC rows must queue an RFI")
check(any("not extracted" in n
          for n in rec.get("unquantified_rooms", [])),
      "WC: unmatched designated legend rows missing from record")

# A numbered schedule whose designated WC room was never extracted (the
# missing-pantry case) must surface that row in the RFI.
sched = _sched6(**{"105": {"wall_finish": "WC-1"}})
rooms = [_room(101, "Office A", wc=0)]  # pantry 105 never extracted
a = _an(rooms, sched, agg={"total_wallcovering_sqft": 0})
T._enforce_wallcovering_schedule_gate(a)
check(any("Pantry (not extracted)" in n
          for n in a.get("_wc_schedule_gate", {}).get(
              "unquantified_rooms", [])),
      "WC: never-extracted designated room missing from RFI record")

# ---------------------------------------------------------------------------
# Base-trim schedule confirm — WB-x pays, RB-x suppresses, blank falls back.
# ---------------------------------------------------------------------------
check(T._schedule_base_paintable("WB-1") is True, "base: WB-1 not paintable")
check(T._schedule_base_paintable("WB-2 Wood Base") is True,
      "base: WB-2 Wood Base not paintable")
check(T._schedule_base_paintable("RB-1") is False, "base: RB-1 not resilient")
check(T._schedule_base_paintable("Rubber Base") is False,
      "base: rubber base not resilient")
check(T._schedule_base_paintable("") is None, "base: blank should be None")
check(T._schedule_base_paintable("Z-9") is None,
      "base: unrecognized code should be None")

# Schedule beats room evidence and building-type default.
r = _room(101, "Office A")
check(T._base_confirmed_paintable(r, "commercial", schedule_base="WB-1"),
      "base: WB-1 must confirm on commercial")
check(not T._base_confirmed_paintable(r, "residential", schedule_base="RB-1"),
      "base: RB-1 must suppress even on residential")
check(not T._base_confirmed_paintable(r, "commercial", schedule_base="Z-9"),
      "base: unrecognized code must fall back to commercial default")

# Integration through _recalculate_totals on a commercial job:
#   101 WB-1, no emitted trim, perimeter 100  -> derives + pays 100 LF
#   102 RB-1, emitted trim 80                 -> suppressed
#   103 blank schedule base, emitted trim 50  -> suppressed (default)
sched = _sched6(**{"101": {"base_finish": "WB-1"},
                   "102": {"base_finish": "RB-1 Rubber Base"}})
rooms = [
    _room(101, "Office A", perim=100),
    _room(102, "Office B", base_lf=80, perim=80),
    _room(103, "Corridor", base_lf=50, perim=50),
]
a = _an(rooms, sched)
T._recalculate_totals(a)
agg = a["aggregated_totals"]
check(agg.get("total_base_trim_lf") == 100,
      f"base: expected 100 LF paid, got {agg.get('total_base_trim_lf')}")
check(rooms[0]["elements"]["base_trim_lf"] == 100,
      "base: WB-1 room did not derive trim from perimeter")
check("[Base Trim] finish schedule confirms" in rooms[0]["notes"],
      "base: confirmed room missing schedule provenance note")
check(rooms[1]["elements"]["base_trim_lf"] == 0,
      "base: RB-1 room trim not suppressed")
check(rooms[2]["elements"]["base_trim_lf"] == 0,
      "base: unconfirmed commercial room trim not suppressed")
check(a.get("_base_trim_schedule_confirmed", {}).get("lf") == 100,
      "base: schedule-confirmed record missing/wrong")

# Flag off -> WB-1 room is zeroed again (the pre-fix behavior).
os.environ["NIGHTSHIFT_BASE_TRIM_SCHEDULE_CONFIRM"] = "0"
a = _an([_room(101, "Office A", base_lf=100, perim=100)],
        _sched6(**{"101": {"base_finish": "WB-1"}}))
T._recalculate_totals(a)
check(a["aggregated_totals"].get("total_base_trim_lf") == 0,
      "base: flag off must reproduce the old suppression")
os.environ["NIGHTSHIFT_BASE_TRIM_SCHEDULE_CONFIRM"] = "1"

# ---------------------------------------------------------------------------
# Ceiling types from the schedule — (1e) inside _enforce_ceiling_scope_gate.
# ---------------------------------------------------------------------------
# Classifier: mixed/ambiguous codes must return None (the dealership SHOWROOM
# row "GWB/OPEN/EXPOSED - GWB sprayed PT4" must NOT demote real spray scope).
check(T._schedule_ceiling_class("ACT-1 (Armstrong lay-in)") == "not_painted",
      "ceiling class: ACT not classified")
check(T._schedule_ceiling_class("GWB - Paint") == "painted",
      "ceiling class: GWB-Paint not classified")
check(T._schedule_ceiling_class(
      "GWB/OPEN/EXPOSED - GWB sprayed PT4 or PT1; refer to ceiling plan")
      is None, "ceiling class: mixed GWB/EXPOSED code must be ambiguous")
check(T._schedule_ceiling_class("OPEN/EXPOSED - sprayed PT4") is None,
      "ceiling class: exposed-but-sprayed must be ambiguous (spray scope)")
check(T._schedule_ceiling_class("OPEN/EXPOSED - NO FINISH") == "not_painted",
      "ceiling class: bare exposed deck not classified")
check(T._schedule_ceiling_class("Dryfall") == "dryfall",
      "ceiling class: dryfall not classified")
check(T._schedule_ceiling_class("") is None,
      "ceiling class: blank must be None")
# 201: extractor says ACT/unpainted, schedule says GWB-Paint -> restored from
#      floor area. 202: extractor priced "GYP (assumed)", schedule says ACT ->
#      demoted. 203: blank schedule code -> untouched. 204: painted GYP kept.
sched = _sched6(**{"101": {"ceiling_finish": "GWB - Paint"},
                   "102": {"ceiling_finish": "ACT"},
                   "103": {"ceiling_finish": ""},
                   "104": {"ceiling_finish": "GYP"}})
rooms = [
    _room(101, "Office A", floor_area=500, ceiling="ACT", painted=False),
    _room(102, "Office B", ceil_area=300, floor_area=300,
          ceiling="GYP (assumed)", painted=True),
    _room(103, "Corridor", ceil_area=240, floor_area=240, ceiling="GYP",
          painted=True),
    _room(104, "Break Room", ceil_area=200, floor_area=200, ceiling="GYP",
          painted=True),
]
a = _an(rooms, sched, agg={"total_paintable_ceiling_sqft": 740})
T._enforce_ceiling_scope_gate(a)
rec = a.get("_ceiling_scope_gate", {}).get("ceiling_schedule_types", {})
m101, m102 = rooms[0]["materials"], rooms[1]["materials"]
check(m101["ceiling_painted"] is True and
      rooms[0]["dimensions"]["ceiling_area_sqft"] == 500,
      "ceiling: schedule-GWB room not restored from floor area")
check(m101["ceiling"] == "GYP (finish schedule)",
      "ceiling: restored room material not stamped from schedule")
check(m102["ceiling_painted"] is False and
      rooms[1]["dimensions"]["ceiling_area_sqft"] == 0,
      "ceiling: schedule-ACT room not demoted")
check(rooms[2]["materials"]["ceiling_painted"] is True and
      rooms[2]["dimensions"]["ceiling_area_sqft"] == 240,
      "ceiling: blank schedule code must leave the room alone")
check(rec.get("demoted") == 1 and rec.get("restored") == 1,
      f"ceiling: record wrong: {rec}")
# Aggregate: prev 740, demoted -300, restored +500 -> gated 940, cap
# prev + restored = 1240 -> 940 wins.
check(a["aggregated_totals"]["total_paintable_ceiling_sqft"] == 940,
      f"ceiling: aggregate should be 940, got "
      f"{a['aggregated_totals']['total_paintable_ceiling_sqft']}")

# Schedule-GWB confirmation must protect an "(assumed)" ceiling from the 1b
# provenance demote when that gate is also on.
os.environ["NIGHTSHIFT_COMMERCIAL_CEILING_GYP_GATE"] = "1"
sched = _sched6(**{"101": {"ceiling_finish": "GWB - Paint"}})
rooms = [_room(101, "Office A", ceil_area=400, floor_area=400,
               ceiling="GYP (assumed)", painted=True)]
a = _an(rooms, sched, agg={"total_paintable_ceiling_sqft": 400})
T._enforce_ceiling_scope_gate(a)
check(rooms[0]["materials"]["ceiling_painted"] is True and
      rooms[0]["dimensions"]["ceiling_area_sqft"] == 400,
      "ceiling: schedule-confirmed GWB was demoted by the 1b assumed gate")
os.environ["NIGHTSHIFT_COMMERCIAL_CEILING_GYP_GATE"] = "0"

# Schedule-GWB with no measured area anywhere -> RFI, stays unpainted.
sched = _sched6(**{"101": {"ceiling_finish": "GWB - Paint"}})
rooms = [_room(101, "Office A", ceiling="ACT", painted=False)]
a = _an(rooms, sched, agg={"total_paintable_ceiling_sqft": 0})
T._enforce_ceiling_scope_gate(a)
check(rooms[0]["materials"]["ceiling_painted"] is False,
      "ceiling: unmeasurable room must not be marked painted")
check(any(r["category"] == "Ceiling Scope"
          for r in a.get("_pre_pricing_rfis", [])),
      "ceiling: unmeasurable schedule-GWB room did not queue an RFI")

# Residential exempt: the 364 Main GSF floor/supplements must not be touched.
sched = _sched6(**{"102": {"ceiling_finish": "ACT"}})
rooms = [_room(102, "Bedroom", ceil_area=300, floor_area=300, ceiling="GYP",
               painted=True)]
a = _an(rooms, sched, building_type="residential multifamily",
        agg={"total_paintable_ceiling_sqft": 300})
T._enforce_ceiling_scope_gate(a)
check(rooms[0]["materials"]["ceiling_painted"] is True and
      a["aggregated_totals"]["total_paintable_ceiling_sqft"] == 300,
      "ceiling: residential must be exempt from the schedule sub-gate")

# Flag off -> (1e) is a strict no-op (only the ACT hard-demote runs).
os.environ["NIGHTSHIFT_CEILING_SCHEDULE_TYPES"] = "0"
sched = _sched6(**{"101": {"ceiling_finish": "GWB - Paint"}})
rooms = [_room(101, "Office A", floor_area=500, ceiling="ACT", painted=False)]
a = _an(rooms, sched, agg={"total_paintable_ceiling_sqft": 0})
T._enforce_ceiling_scope_gate(a)
check(rooms[0]["materials"]["ceiling_painted"] is False and
      "ceiling_schedule_types" not in a.get("_ceiling_scope_gate", {}),
      "ceiling: flag off must be a strict no-op")
os.environ["NIGHTSHIFT_CEILING_SCHEDULE_TYPES"] = "1"

# ---------------------------------------------------------------------------
# No-schedule safety: all three gates must be inert when the analysis carries
# no room_finish_schedule at all (every pre-PR-#15 job).
# ---------------------------------------------------------------------------
rooms = [_room(101, "Office A", wc=200, base_lf=50, perim=50, ceil_area=300,
               floor_area=300, ceiling="GYP", painted=True)]
a = _an(rooms, [], agg={"total_wallcovering_sqft": 200,
                        "total_paintable_ceiling_sqft": 300})
T._enforce_wallcovering_schedule_gate(a)
T._enforce_ceiling_scope_gate(a)
check(rooms[0]["elements"]["wallcovering_sqft"] == 200,
      "no-schedule: WC gate must be inert")
check(rooms[0]["materials"]["ceiling_painted"] is True,
      "no-schedule: ceiling sub-gate must be inert")
a2 = _an([_room(101, "Office A", base_lf=60, perim=60)], [])
T._recalculate_totals(a2)
check("_base_trim_schedule_confirmed" not in a2,
      "no-schedule: base-trim confirm must be inert")

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
