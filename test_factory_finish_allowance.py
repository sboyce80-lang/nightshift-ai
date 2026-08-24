#!/usr/bin/env python3
"""Factory-finish siding prices as a strikeable ALLOWANCE, not $0.

Marathon 2026-08-24: estimators bid factory-finish-noted siding on 3 of 3
golden jobs (Caris JW 4,762 SF / Fishkill Rider 5,818 SF / Dutchess
"factory-primed, field-painted"). NIGHTSHIFT_FACTORY_FINISH_ALLOWANCE
keeps the measured quantity, labels the line, ships the strike-RFI, and
stops Will's factory-finish auto-removal from double-dipping.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import Takeoff_DIRECT as T  # noqa: E402
import will_synthesis as W  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def _clear():
    for k in ("NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE",
              "NIGHTSHIFT_FACTORY_FINISH_ALLOWANCE",
              "NIGHTSHIFT_WILL_SCOPE_REMOVAL"):
        os.environ.pop(k, None)


CARIS_NOTE = ("The four elevations show a facility clad primarily in Fiber "
              "Cement Lap Siding and Fiber Cement Board-and-Batten Siding "
              "(both are factory-finished products, not field-painted). "
              "Painted AZEK trim at entries.")


def caris():
    return {"exterior": {"hardie_siding_sqft": 4800, "azek_trim_lf": 280,
                         "notes": CARIS_NOTE, "paint_evidence": None},
            "aggregated_totals": {}}


print("— gate behavior —")
_clear()
os.environ["NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE"] = "1"
a = T._enforce_exterior_evidence(caris())
check(a["exterior"]["hardie_siding_sqft"] == 0,
      "flag off: veto no longer zeroes")

_clear()
os.environ["NIGHTSHIFT_EXTERIOR_EVIDENCE_GATE"] = "1"
os.environ["NIGHTSHIFT_FACTORY_FINISH_ALLOWANCE"] = "1"
a = T._enforce_exterior_evidence(caris())
check(a["exterior"]["hardie_siding_sqft"] == 4800,
      f"allowance: siding zeroed anyway: "
      f"{a['exterior']['hardie_siding_sqft']}")
check(a["exterior"]["azek_trim_lf"] == 280,
      f"allowance: painted azek lost: {a['exterior']['azek_trim_lf']}")
ffa = a["exterior"].get("_factory_finish_allowance") or {}
check("hardie_siding_sqft" in ffa, f"allowance marker missing: {ffa}")
check(any("ALLOWANCE" in str(n) for n in a.get("notes", [])),
      "no allowance note")

print("— line labeling —")
costs = T.calculate_costs(
    {"total_paintable_wall_sqft": 1000},
    exterior=a["exterior"], building_type="assisted living")
hardie_lines = [li for li in costs["line_items"]
                if "Hardie" in str(li.get("item"))]
check(hardie_lines and "ALLOWANCE — factory finish noted"
      in str(hardie_lines[0]["item"]),
      f"line not allowance-labeled: "
      f"{[str(li.get('item')) for li in hardie_lines]}")
check(hardie_lines and float(hardie_lines[0].get("total") or 0) > 0,
      "allowance line priced $0")
azek_lines = [li for li in costs["line_items"]
              if "Azek" in str(li.get("item"))]
check(azek_lines and "ALLOWANCE" not in str(azek_lines[0]["item"]),
      f"non-allowance line mislabeled: {str(azek_lines[0]['item'])}")

print("— Will interplay —")
adj = {"category": "Ext. Hardie Siding", "from_value": 4800, "to_value": 0,
       "reason": "factory-finished fiber cement, not field-painted"}
_clear()
os.environ["NIGHTSHIFT_WILL_SCOPE_REMOVAL"] = "1"
check(W._is_documented_scope_removal(adj),
      "baseline factory-finish removal broken")
os.environ["NIGHTSHIFT_FACTORY_FINISH_ALLOWANCE"] = "1"
check(not W._is_documented_scope_removal(adj),
      "Will can still auto-remove the allowance line")
check(W._is_documented_scope_removal(
    {**adj, "reason": "exterior repaint is by others per GC matrix"}),
      "by-others removal wrongly blocked under allowance policy")
_clear()

print("=== PASS ===" if not fails else
      "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
