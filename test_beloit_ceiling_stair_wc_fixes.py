"""Regression tests for the Beloit-round-2 scope-gate sub-fixes, all run inside
_enforce_ceiling_scope_gate and each independently flag-gated (default OFF):

  (1b) NIGHTSHIFT_COMMERCIAL_CEILING_GYP_GATE — a commercial room ceiling
       asserted as painted GYP with no soffit/feature evidence is the extractor's
       default when the RCP/finish-schedule ceiling type is unreadable; in
       commercial/healthcare space that is predominantly ACT. Demote to
       not-painted + RFI (Beloit clinic: 3,518 SF asserted GYP vs ~500 real).
  (1c) NIGHTSHIFT_STAIR_FINISH_GATE — stair/stairwell rooms extracted with paint
       scope (walls/ceiling/gyp-between/sections) are zeroed unless a note
       confirms the stair is finished (Beloit: two in-scope "Stair" rooms).
  (1d) NIGHTSHIFT_WALLCOVERING_RFI — promote a dedicated RFI when a WC-x code is
       present but no wallcovering area was quantified (Beloit: WC-1 on A120).

Guards: residential exempt; positive GYP evidence keeps the ceiling; a confirmed
stair note keeps the stair; each flag off is a no-op. Offline, no API. Also runs
the real saved Beloit prod JSON when present.
"""
import os
os.environ["NIGHTSHIFT_CEILING_SCOPE_GATE"] = "1"
os.environ["NIGHTSHIFT_COMMERCIAL_CEILING_GYP_GATE"] = "1"
os.environ["NIGHTSHIFT_STAIR_FINISH_GATE"] = "1"
os.environ["NIGHTSHIFT_WALLCOVERING_RFI"] = "1"
import json
import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def _room(name, ceil_area, painted=True, ceiling="GYP", walls="GYP",
          notes="", in_scope=True, elements=None):
    return {
        "room_name": name, "in_scope": in_scope,
        "materials": {"walls": walls, "ceiling": ceiling,
                      "ceiling_painted": painted},
        "dimensions": {"ceiling_area_sqft": ceil_area,
                       "floor_area_sqft": ceil_area,
                       "wall_area_sqft": ceil_area},
        "elements": elements or {},
        "notes": notes,
    }


def _an(building_type, rooms, agg=None):
    a = {
        "project_info": {"building_type": building_type},
        "aggregated_totals": agg or {},
        "floors": [{"floor_name": "1", "rooms": rooms}],
    }
    a["aggregated_totals"].setdefault(
        "total_paintable_ceiling_sqft",
        sum(_num_ceil(r) for r in rooms if r.get("in_scope", True)))
    return a


def _num_ceil(r):
    m = r.get("materials") or {}
    return (r.get("dimensions", {}).get("ceiling_area_sqft", 0)
            if m.get("ceiling_painted") else 0)


def _reset(a):
    a.pop("_ceiling_scope_gate", None)
    a.pop("_pre_pricing_rfis", None)


# ---------------------------------------------------------------------------
# (1b) Commercial GYP-without-evidence demote
# ---------------------------------------------------------------------------
# Beloit shape: many clinic rooms asserted painted GYP, one genuine feature.
a = _an("commercial", [
    _room("Waiting", 875),
    _room("Check In/Out", 375),
    _room("Consult", 120),
    _room("Priv 5 Office", 180),
    _room("Dr. Gold Design Feature", 300),      # feature -> KEEP
    _room("Corridor Soffit", 200,
          elements={"soffit_sqft": 120}),        # soffit callout -> KEEP
])
T._enforce_ceiling_scope_gate(a)
rooms = {r["room_name"]: r for r in a["floors"][0]["rooms"]}
check(rooms["Waiting"]["materials"]["ceiling_painted"] is False,
      "1b: bare-GYP clinic room not demoted")
check(rooms["Waiting"]["dimensions"]["ceiling_area_sqft"] == 0,
      "1b: demoted ceiling area not zeroed")
check(rooms["Dr. Gold Design Feature"]["materials"]["ceiling_painted"] is True,
      "1b: design-feature GYP wrongly demoted")
check(rooms["Corridor Soffit"]["materials"]["ceiling_painted"] is True,
      "1b: soffit_sqft evidence room wrongly demoted")
got = a["aggregated_totals"]["total_paintable_ceiling_sqft"]
check(abs(got - 500) < 1, f"1b: aggregate should be 300+200=500, got {got}")
check(a["_ceiling_scope_gate"]["commercial_gyp_demoted"] == 4,
      "1b: expected 4 rooms demoted")
rfis = a.get("_pre_pricing_rfis", [])
check(any(r["category"] == "Ceiling Scope" for r in rfis),
      "1b: no Ceiling Scope RFI queued")

# 1b guard: residential is exempt (no demotion, aggregate preserved).
a = _an("mixed-use residential", [_room("Apt Living", 1000)], agg={
    "total_paintable_ceiling_sqft": 34682})
T._enforce_ceiling_scope_gate(a)
check(a["aggregated_totals"]["total_paintable_ceiling_sqft"] == 34682,
      "1b: residential aggregate must be preserved")
check(a["floors"][0]["rooms"][0]["materials"]["ceiling_painted"] is True,
      "1b: residential ceiling wrongly demoted")

# 1b guard: flag off -> no demotion.
os.environ["NIGHTSHIFT_COMMERCIAL_CEILING_GYP_GATE"] = "0"
a = _an("commercial", [_room("Waiting", 875)])
T._enforce_ceiling_scope_gate(a)
check(a["floors"][0]["rooms"][0]["materials"]["ceiling_painted"] is True,
      "1b: flag-off should not demote")
os.environ["NIGHTSHIFT_COMMERCIAL_CEILING_GYP_GATE"] = "1"

# ---------------------------------------------------------------------------
# (1c) Stair finish gate
# ---------------------------------------------------------------------------
a = _an("commercial", [
    _room("Stair", 180, walls="GYP",
          elements={"gyp_between_stairs_sqft": 320, "stair_sections": 2}),
    _room("Stair", 300, walls="GYP",
          elements={"gyp_between_stairs_sqft": 320, "stair_sections": 1}),
    _room("Office", 200, notes="painted feature"),  # non-stair, untouched by 1c
], agg={"total_paintable_wall_sqft": 18902,
        "total_gyp_between_stairs_sqft": 640,
        "total_stair_sections": 3})
# turn OFF 1b so we isolate the stair gate's own reductions
os.environ["NIGHTSHIFT_COMMERCIAL_CEILING_GYP_GATE"] = "0"
T._enforce_ceiling_scope_gate(a)
os.environ["NIGHTSHIFT_COMMERCIAL_CEILING_GYP_GATE"] = "1"
agg = a["aggregated_totals"]
check(agg["total_gyp_between_stairs_sqft"] == 0,
      f"1c: gyp-between not zeroed, got {agg['total_gyp_between_stairs_sqft']}")
check(agg["total_stair_sections"] == 0,
      f"1c: stair sections not zeroed, got {agg['total_stair_sections']}")
check(abs(agg["total_paintable_wall_sqft"] - (18902 - 480)) < 1,
      f"1c: wall not reduced by 480, got {agg['total_paintable_wall_sqft']}")
stairs = [r for r in a["floors"][0]["rooms"] if r["room_name"] == "Stair"]
check(all(s["materials"]["ceiling_painted"] is False for s in stairs),
      "1c: stair ceilings not demoted")
check(a["_ceiling_scope_gate"]["stair_rooms_demoted"] == 2,
      "1c: expected 2 stair rooms demoted")
check(any(r["category"] == "Stair Scope"
          for r in a.get("_pre_pricing_rfis", [])),
      "1c: no Stair Scope RFI queued")

# 1c guard: a note confirming the stair is finished keeps it.
a = _an("commercial", [
    _room("Stair", 300, walls="GYP", notes="finished stair — painted per A501",
          elements={"stair_sections": 2})])
T._enforce_ceiling_scope_gate(a)
check(a["floors"][0]["rooms"][0]["elements"]["stair_sections"] == 2,
      "1c: confirmed-finished stair wrongly zeroed")

# 1c guard: flag off -> no-op.
os.environ["NIGHTSHIFT_STAIR_FINISH_GATE"] = "0"
a = _an("commercial", [_room("Stair", 300, walls="GYP",
        elements={"stair_sections": 2})])
T._enforce_ceiling_scope_gate(a)
check(a["floors"][0]["rooms"][0]["elements"]["stair_sections"] == 2,
      "1c: flag-off should not zero stairs")
os.environ["NIGHTSHIFT_STAIR_FINISH_GATE"] = "1"

# ---------------------------------------------------------------------------
# (1d) Wallcovering RFI promotion
# ---------------------------------------------------------------------------
a = _an("commercial", [_room("Waiting", 200,
        notes="[A120] WALLCOVERING: WC-1 = TBD, extent unconfirmed — RFI")],
        agg={"total_wallcovering_sqft": 0})
T._enforce_ceiling_scope_gate(a)
wc_rfis = [r for r in a.get("_pre_pricing_rfis", [])
           if r["category"] == "Wallcovering"]
check(len(wc_rfis) == 1, "1d: expected exactly one Wallcovering RFI")
check("WC-1" in wc_rfis[0]["question"], "1d: RFI should name WC-1")
check(a["_ceiling_scope_gate"].get("wallcovering_codes_unquantified") == ["WC-1"],
      "1d: WC-1 not recorded")

# 1d guard: no RFI when wallcovering already quantified.
a = _an("commercial", [_room("Lobby", 200, notes="WC-1 accent wall")],
        agg={"total_wallcovering_sqft": 350})
T._enforce_ceiling_scope_gate(a)
check(not any(r["category"] == "Wallcovering"
              for r in a.get("_pre_pricing_rfis", [])),
      "1d: RFI raised despite quantified wallcovering")

# 1d guard: flag off -> no RFI.
os.environ["NIGHTSHIFT_WALLCOVERING_RFI"] = "0"
a = _an("commercial", [_room("Lobby", 200, notes="WC-1 TBD accent")],
        agg={"total_wallcovering_sqft": 0})
T._enforce_ceiling_scope_gate(a)
check(not any(r["category"] == "Wallcovering"
              for r in a.get("_pre_pricing_rfis", [])),
      "1d: flag-off should raise no Wallcovering RFI")
os.environ["NIGHTSHIFT_WALLCOVERING_RFI"] = "1"

# ---------------------------------------------------------------------------
# Real saved Beloit prod JSON (when present) — the live run values.
# ---------------------------------------------------------------------------
for p in ("/tmp/results_json/Beloit.json",
          os.path.expanduser(
              "~/Downloads/construction_analysis_20260620_162908 (1).json")):
    if not os.path.exists(p):
        continue
    an = json.load(open(p))["analysis"]
    _reset(an)
    T._enforce_ceiling_scope_gate(an)
    agg = an["aggregated_totals"]
    check(abs(T._num(agg.get("total_paintable_ceiling_sqft", 0)) - 300) <= 5,
          f"Beloit prod: ceiling expected ~300, got "
          f"{agg.get('total_paintable_ceiling_sqft')}")
    check(T._num(agg.get("total_gyp_between_stairs_sqft", 0)) == 0,
          "Beloit prod: gyp-between not zeroed")
    check(T._num(agg.get("total_stair_sections", 0)) == 0,
          "Beloit prod: stair sections not zeroed")
    check(any(r["category"] == "Wallcovering"
              for r in an.get("_pre_pricing_rfis", [])),
          "Beloit prod: no wallcovering RFI")
    break

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
