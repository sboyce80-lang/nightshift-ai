#!/usr/bin/env python3
"""Scope-mask contract (scope_mask.py + NIGHTSHIFT_SCOPE_MASK_SHADOW).

Phase 1 of the accuracy program: the LLM's only output is a scope mask
over deterministic geometry — never a quantity. Locks in: the v1 schema
validates happy paths and rejects malformed ones; the no-quantities rule
rejects sqft/_lf/_ea/count/area keys at any depth; apply_scope_mask is
pure, marks rooms in/out with reasons + notes, stores mask data under
the _scope_mask namespace and leaves aggregated_totals byte-identical
(quantity recomputation belongs to the geometry layer); an invalid mask
refuses to apply; mask_from_extraction round-trips a realistic synthetic
analysis into a clean-validating mask; the shadow hook OFF leaves
build_priced_takeoff byte-identical and ON only adds the shadow record.
"""
import copy
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# Deterministic posture: no ambient flag may perturb the fixture.
for _k in list(os.environ):
    if _k.startswith("NIGHTSHIFT_"):
        os.environ.pop(_k)

import scope_mask as sm  # noqa: E402
import Takeoff_DIRECT as T  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def room(num, name, in_scope=True, ceiling_painted=True, trim=10.0,
         rid=None, walls="GYP", ceiling="GYP", reason=""):
    return {
        "room_id": rid or f"R-{num or name}",
        "room_name": name, "room_number": num,
        "source_sheet": "A104", "in_scope": in_scope,
        "scope_exclusion_reason": reason,
        "dimensions": {"wall_area_sqft": 500.0, "ceiling_area_sqft": 120.0,
                       "perimeter_lf": 44.0},
        "materials": {"walls": walls, "ceiling": ceiling,
                      "ceiling_painted": ceiling_painted, "base": "Wood"},
        "elements": {"doors_full_paint": 1, "base_trim_lf": trim},
    }


def synthetic_analysis():
    return {
        "project_info": {"building_type": "commercial"},
        "floors": [
            {"floor_name": "1st Floor", "rooms": [
                room("101", "Office"),
                room("102", "Corridor", ceiling_painted=False,
                     ceiling="ACT"),
                room("103", "Storage", trim=0.0),
                room(None, "Vestibule", rid="F1-VEST"),
            ]},
            {"floor_name": "2nd Floor", "rooms": [
                room("201", "Mech", in_scope=False,
                     reason="not listed in the authoritative room finish "
                            "schedule"),
            ]},
        ],
        "aggregated_totals": {"total_paintable_wall_sqft": 2000.0,
                              "total_paintable_ceiling_sqft": 240.0,
                              "total_doors_full_paint": 4.0,
                              "total_base_trim_lf": 30.0},
        "_pre_pricing_rfis": [
            {"category": "Scope Boundary", "question": "Confirm room 201."}],
        "_template_floors_deduped": True,
        "_residential_corridor_ceiling_fixed": True,
    }


print("scope mask contract checks")

# ── schema validation: happy paths ─────────────────────────────────────
good = {
    "mask_version": "1.0",
    "source": "llm",
    "rooms": {
        "101": {"in_scope": True, "painted_fraction": 0.75,
                "classification": "GWB", "confidence": 0.9},
        "102": {"in_scope": True,
                "painted_fraction": {"walls": 1.0, "ceiling": 0.0,
                                     "trim": 1.0},
                "classification": {"ceiling": "ACT", "walls": "GYP"},
                "evidence": [{"sheet": "A104",
                              "citation": "RCP note 3"}]},
        "201": {"in_scope": False, "reason": "outside the renovation"},
    },
    "exclusions": ["Exterior work excluded"],
    "rfis": ["Confirm ceiling type in corridor 102."],
}
ok, errs = sm.validate_scope_mask(good)
check(ok and errs == [], f"well-formed mask validates clean: {errs}")

minimal = {"mask_version": "1.0", "rooms": {"A": {"in_scope": True}}}
ok, errs = sm.validate_scope_mask(minimal)
check(ok, f"minimal mask (version + rooms/in_scope) validates: {errs}")

# ── schema validation: sad paths ───────────────────────────────────────
ok, errs = sm.validate_scope_mask("nope")
check(not ok, "non-dict mask rejected")
ok, errs = sm.validate_scope_mask({"rooms": {}})
check(not ok and any("mask_version" in e for e in errs),
      "missing mask_version rejected")
ok, errs = sm.validate_scope_mask({"mask_version": "9.9", "rooms": {}})
check(not ok and any("unsupported" in e for e in errs),
      "unsupported version rejected")
ok, errs = sm.validate_scope_mask({"mask_version": "1.0"})
check(not ok and any("rooms" in e for e in errs),
      "missing rooms rejected")
ok, errs = sm.validate_scope_mask(
    {"mask_version": "1.0", "rooms": {"101": {}}})
check(not ok and any("in_scope" in e for e in errs),
      "entry without in_scope rejected")
ok, errs = sm.validate_scope_mask(
    {"mask_version": "1.0", "rooms": {"101": {"in_scope": "yes"}}})
check(not ok, "non-bool in_scope rejected")
ok, errs = sm.validate_scope_mask(
    {"mask_version": "1.0",
     "rooms": {"101": {"in_scope": True, "painted_fraction": 1.5}}})
check(not ok and any("0..1" in e for e in errs),
      "painted_fraction 1.5 out of range rejected")
ok, errs = sm.validate_scope_mask(
    {"mask_version": "1.0",
     "rooms": {"101": {"in_scope": True,
                       "painted_fraction": {"roof": 0.5}}}})
check(not ok, "unknown painted_fraction surface rejected")
ok, errs = sm.validate_scope_mask(
    {"mask_version": "1.0",
     "rooms": {"101": {"in_scope": True, "confidence": 7}}})
check(not ok, "confidence outside 0..1 rejected")
ok, errs = sm.validate_scope_mask(
    {"mask_version": "1.0",
     "rooms": {"101": {"in_scope": True,
                       "evidence": [{"citation": "no sheet"}]}}})
check(not ok, "evidence item without sheet rejected")
ok, errs = sm.validate_scope_mask(
    {"mask_version": "1.0",
     "rooms": {"101": {"in_scope": True, "mystery": 1}}})
check(not ok and any("unknown key 'mystery'" in e for e in errs),
      "unknown entry key rejected (closed schema)")
ok, errs = sm.validate_scope_mask(
    {"mask_version": "1.0", "rooms": {}, "rfis": ["ok", ""]})
check(not ok, "empty-string RFI rejected")

# ── the core rule: no quantity-shaped keys anywhere ────────────────────
for bad_key in ("wall_sqft", "base_trim_lf", "doors_ea", "door_count",
                "ceiling_area", "SQFT_total"):
    m = {"mask_version": "1.0",
         "rooms": {"101": {"in_scope": True}}, bad_key: 1}
    ok, errs = sm.validate_scope_mask(m)
    check(not ok and any("quantity-shaped" in e for e in errs),
          f"quantity key '{bad_key}' rejected at top level")

m = copy.deepcopy(good)
m["rooms"]["101"]["painted_fraction"] = {"walls": 1.0}
m["rooms"]["101"]["evidence"] = [
    {"sheet": "A1", "citation": "x", "wall_sqft": 900}]
ok, errs = sm.validate_scope_mask(m)
check(not ok and any("quantity-shaped" in e for e in errs),
      "quantity key nested in evidence rejected (recursive sweep)")

m = {"mask_version": "1.0",
     "rooms": {"storage_area": {"in_scope": True}}}
ok, errs = sm.validate_scope_mask(m)
check(not ok and any("quantity-shaped" in e for e in errs),
      "quantity-shaped room identity key rejected too")

# ── apply_scope_mask: pure, scoped, aggregate-blind ────────────────────
a = synthetic_analysis()
a_before = json.dumps(a, sort_keys=True)
agg_before = json.dumps(a["aggregated_totals"], sort_keys=True)
mask = {
    "mask_version": "1.0", "source": "llm",
    "rooms": {
        "101": {"in_scope": False, "reason": "outside the tenant fit-out"},
        "102": {"in_scope": True,
                "painted_fraction": {"walls": 1.0, "ceiling": 0.0},
                "classification": {"ceiling": "ACT"}, "confidence": 0.8},
        "201": {"in_scope": True, "reason": "schedule read was partial"},
    },
    "exclusions": ["Elevator interiors by others"],
    "rfis": ["Confirm suite boundary on A104."],
}
b = sm.apply_scope_mask(a, mask)
check(json.dumps(a, sort_keys=True) == a_before,
      "apply is pure: the input analysis is untouched")
check(json.dumps(b["aggregated_totals"], sort_keys=True) == agg_before,
      "aggregated_totals byte-identical after apply (geometry owns "
      "quantities)")
rooms_by_num = {r.get("room_number"): r
                for fl in b["floors"] for r in fl["rooms"]}
r101 = rooms_by_num["101"]
check(r101["in_scope"] is False and
      "excluded by scope mask" in r101["scope_exclusion_reason"] and
      "tenant fit-out" in r101["scope_exclusion_reason"],
      "masked-out room marked out of scope with the mask's reason")
r102 = rooms_by_num["102"]
check(r102["in_scope"] is True and
      r102["_scope_mask"]["painted_fraction"]["ceiling"] == 0.0 and
      r102["_scope_mask"]["classification"]["ceiling"] == "ACT" and
      r102["_scope_mask"]["confidence"] == 0.8,
      "mask data stored under the _scope_mask namespace")
r201 = rooms_by_num["201"]
check(r201["in_scope"] is True and
      r201["scope_exclusion_reason"] == "" and
      r201["_scope_mask"]["prior_in_scope"] is False,
      "mask can return a room to scope, recording the prior state")
check(any("[Scope Mask]" in n and "out of scope" in n
          for n in b.get("notes", [])),
      "exclusion note recorded")
check(any("Elevator interiors" in n for n in b.get("notes", [])),
      "job-level exclusions surface as notes")
check(any(r.get("category") == "Scope Mask" and "A104" in r.get("question")
          for r in b.get("_pre_pricing_rfis", [])),
      "mask RFIs queued in _pre_pricing_rfis")
rec = b.get("_scope_mask_applied")
check(isinstance(rec, dict) and rec["version"] == "1.0" and
      rec["rooms_masked"] == 3 and rec["rooms_excluded"] == 1 and
      rec["rooms_included"] == 1 and rec["source"] == "llm",
      f"_scope_mask_applied record: {rec}")

try:
    sm.apply_scope_mask(a, {"mask_version": "1.0",
                            "rooms": {"101": {"in_scope": True,
                                              "wall_sqft": 1}}})
    check(False, "invalid mask must not apply")
except ValueError as e:
    check("quantity-shaped" in str(e), "invalid mask refuses to apply "
                                       "with the validator's error")

# ── mask_from_extraction round-trip ────────────────────────────────────
a = synthetic_analysis()
m = sm.mask_from_extraction(a)
ok, errs = sm.validate_scope_mask(m)
check(ok, f"extraction-derived mask validates clean: {errs[:3]}")
check(len(m["rooms"]) == 5, f"5 identities masked: {len(m['rooms'])}")
check("F1-VEST" in m["rooms"],
      "room without a number keys by room_id")
e101 = m["rooms"]["101"]
check(e101["in_scope"] is True and
      e101["painted_fraction"] == {"walls": 1.0, "ceiling": 1.0,
                                   "trim": 1.0},
      "in-scope painted room derives full fractions")
e102 = m["rooms"]["102"]
check(e102["painted_fraction"]["ceiling"] == 0.0 and
      e102["classification"]["ceiling"] == "ACT",
      "unpainted ACT ceiling derives fraction 0 + classification")
check(m["rooms"]["103"]["painted_fraction"]["trim"] == 0.0,
      "no base trim derives trim fraction 0")
e201 = m["rooms"]["201"]
check(e201["in_scope"] is False and "finish" in e201["reason"],
      "out-of-scope room carries its exclusion reason")
check(e101["evidence"][0]["sheet"] == "A104",
      "evidence cites the source sheet")
check(m["exclusions"] and "finish" in m["exclusions"][0],
      "job-level exclusions derived from exclusion reasons")
check(m["rfis"] == ["Confirm room 201."],
      f"pre-pricing RFIs carried as mask RFIs: {m['rfis']}")

b = sm.apply_scope_mask(a, m)
check(b["_scope_mask_applied"]["rooms_masked"] == 5 and
      b["_scope_mask_applied"]["rooms_excluded"] == 0 and
      b["_scope_mask_applied"]["rooms_included"] == 0,
      "round-trip apply is a no-op on scope (mask mirrors extraction)")
check(json.dumps(b["aggregated_totals"], sort_keys=True) ==
      json.dumps(a["aggregated_totals"], sort_keys=True),
      "round-trip leaves aggregates byte-identical")

# ── shadow hook: OFF = byte identity, ON = record only ─────────────────
os.environ.pop("NIGHTSHIFT_SCOPE_MASK_SHADOW", None)
base = synthetic_analysis()
off1 = T.build_priced_takeoff(copy.deepcopy(base))
off2 = T.build_priced_takeoff(copy.deepcopy(base))
check(json.dumps(off1, sort_keys=True) == json.dumps(off2, sort_keys=True),
      "fixture sanity: build_priced_takeoff is deterministic here")
check("_scope_mask_shadow" not in off1,
      "flag OFF records nothing")

os.environ["NIGHTSHIFT_SCOPE_MASK_SHADOW"] = "1"
on = T.build_priced_takeoff(copy.deepcopy(base))
os.environ.pop("NIGHTSHIFT_SCOPE_MASK_SHADOW", None)
shadow = on.get("_scope_mask_shadow")
check(isinstance(shadow, dict) and shadow.get("valid") is True and
      shadow.get("errors") == [] and shadow.get("rooms") == 5,
      f"flag ON stores a valid shadow mask: "
      f"{ {k: v for k, v in (shadow or {}).items() if k != 'mask'} }")
on_stripped = copy.deepcopy(on)
on_stripped.pop("_scope_mask_shadow", None)
check(json.dumps(on_stripped, sort_keys=True) ==
      json.dumps(off1, sort_keys=True),
      "flag ON changes NOTHING but the shadow record (byte identity "
      "after stripping it)")

print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all scope-mask checks passed")
