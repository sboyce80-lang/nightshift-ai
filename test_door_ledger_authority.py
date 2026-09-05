#!/usr/bin/env python3
"""Door-ledger authority guard (flip-board catch 2026-09-05).

Locks in: a Mode-A parse may override priced door counts only when it
read a material column (proves a real door schedule), or agrees with a
live extraction count within 25%, or extraction found no doors at all
(the validated Caris case). A weak parse contradicting a live count is
demoted to a note + Door Count RFI — Fishkill's replay went 143 → 15.3
doors off a misparsed table before this guard.
"""
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["NIGHTSHIFT_DOOR_SCHEDULE_LEDGER"] = "1"

import Takeoff_DIRECT as T  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def fake_ledger(count, fp=0, hm=0, mode="schedule"):
    mod = types.ModuleType("door_ledger")
    mod.build_door_ledger = lambda paths: {
        "mode": mode, "count": count, "full_paint": fp, "hm_panel": hm,
        "detector": None, "sources": ["schedule(p4)"]}
    sys.modules["door_ledger"] = mod


def analysis(fp, hm):
    return {"_vme_pdf_paths": ["/tmp/fake.pdf"],
            "aggregated_totals": {"total_doors_full_paint": fp,
                                  "total_doors_hm_panel": hm},
            "notes": [], "rfi_items": []}


print("door-ledger authority checks")

# Weak parse (no materials) contradicting a live count → demoted.
fake_ledger(16)
a = T._apply_door_schedule_ledger(analysis(130, 13))
check(a["aggregated_totals"]["total_doors_full_paint"] == 130,
      "weak contradicting parse: extraction count kept (Fishkill class)")
check(a["_door_ledger"].get("demoted") is not None,
      "weak contradicting parse: demotion recorded")
check(any(r.get("category") == "Door Count" for r in a["rfi_items"]),
      "weak contradicting parse: Door Count RFI raised")

# Strong parse (materials read) still rules even against a live count.
fake_ledger(78, fp=60, hm=18)
a = T._apply_door_schedule_ledger(analysis(150, 10))
check(a["aggregated_totals"]["total_doors_full_paint"] == 60.0,
      "materials-rich parse: ledger overrides (Homewood/Northwell class)")

# Weak parse that AGREES within 25% may still rule.
fake_ledger(26)
a = T._apply_door_schedule_ledger(analysis(11, 18))
check(abs(a["aggregated_totals"]["total_doors_full_paint"] - 9.9) < 0.2,
      "agreeing weak parse: ledger count applied on extraction's split")

# Extraction found nothing: the schedule is the only signal (Caris).
fake_ledger(79)
a = T._apply_door_schedule_ledger(analysis(0, 0))
check(a["aggregated_totals"]["total_doors_full_paint"] == 79.0,
      "zero-extraction: ledger applies (validated Caris case)")

# Flag off: inert.
os.environ["NIGHTSHIFT_DOOR_SCHEDULE_LEDGER"] = "0"
fake_ledger(16)
a = T._apply_door_schedule_ledger(analysis(130, 13))
check(a["aggregated_totals"]["total_doors_full_paint"] == 130
      and "_door_ledger" not in a,
      "flag off: gate untouched")
os.environ["NIGHTSHIFT_DOOR_SCHEDULE_LEDGER"] = "1"

sys.modules.pop("door_ledger", None)

print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("all door-ledger authority checks passed")
