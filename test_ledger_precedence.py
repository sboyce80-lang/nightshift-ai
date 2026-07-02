"""Tests for ledger precedence enforcement (_enforce_ledger_precedence).

The pricing pipeline mutates aggregated_totals in ~10 sequential stages with
last-writer-wins semantics — the root of the recurring desync class (Devine
ceiling phantom, 364 Main caps-after-boost, Purdy door-schedule revert). The
enforcer replays the _quantity_adjustments ledger per key and re-asserts the
last authoritative (schedule/correction, rank 3) value over any later
derived/assumed write still in effect. Kill switch NIGHTSHIFT_LEDGER_ENFORCE,
default ON.

Offline, no API.
"""
import os
import sys

import Takeoff_DIRECT as T

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
    print(f"  {'PASS' if cond else 'FAIL'}  {msg}")


def purdy_ledger():
    """The exact conflict shape from construction_analysis_20260701_164933:
    schedule override lowers doors (recorded as correction), then the
    wall_boost stage snapshot records the recalc revert as derived."""
    return [
        {"stage": "schedule_overrides", "item": "total_doors_full_paint",
         "from": 136.0, "to": 14.0, "delta": -122.0, "source": "correction",
         "basis": "authoritative door/window/stair schedule"},
        {"stage": "schedule_overrides", "item": "total_doors_hm_panel",
         "from": 12.0, "to": 6.0, "delta": -6.0, "source": "correction",
         "basis": "authoritative door/window/stair schedule"},
        {"stage": "wall_boost", "item": "total_doors_hm_panel",
         "from": 6.0, "to": 12.0, "delta": 6.0, "source": "derived",
         "basis": "perimeter x height (Mode 1) / footprint ratio (Mode 2)"},
        {"stage": "wall_boost", "item": "total_doors_full_paint",
         "from": 14.0, "to": 136.0, "delta": 122.0, "source": "derived",
         "basis": "perimeter x height (Mode 1) / footprint ratio (Mode 2)"},
    ]


# ---------------------------------------------------------------------------
print("\n1) Purdy conflict: derived revert of a schedule correction is undone")
os.environ.pop("NIGHTSHIFT_LEDGER_ENFORCE", None)  # default ON
a = {"aggregated_totals": {"total_doors_full_paint": 136.0,
                           "total_doors_hm_panel": 12.0,
                           "total_paintable_wall_sqft": 63690.8},
     "_quantity_adjustments": purdy_ledger(), "notes": []}
T._enforce_ledger_precedence(a)
agg = a["aggregated_totals"]
check(agg["total_doors_full_paint"] == 14 and agg["total_doors_hm_panel"] == 6,
      "doors restored to schedule values (136/12 -> 14/6): got "
      f"{agg['total_doors_full_paint']}/{agg['total_doors_hm_panel']}")
check(agg["total_paintable_wall_sqft"] == 63690.8,
      "unrelated key untouched")
check(len(a.get("_ledger_precedence_enforced", [])) == 2,
      "2 enforcement records written")
check(all(e.get("reverted_by_precedence")
          for e in a["_quantity_adjustments"] if e["stage"] == "wall_boost"),
      "weaker entries marked reverted")
check(sum(1 for e in a["_quantity_adjustments"]
          if e["stage"] == "ledger_precedence_enforce") == 2,
      "enforcement appended its own correction entries to the ledger")
check(sum(1 for n in a["notes"] if "[Ledger Precedence]" in n) == 2,
      "notes written for the estimator")

print("\n1b) Idempotent: second pass is a no-op")
before = dict(a["aggregated_totals"])
n_ledger = len(a["_quantity_adjustments"])
T._enforce_ledger_precedence(a)
check(a["aggregated_totals"] == before
      and len(a["_quantity_adjustments"]) == n_ledger
      and len(a["_ledger_precedence_enforced"]) == 2,
      "no mutation, no new ledger/enforcement entries")

# ---------------------------------------------------------------------------
print("\n2) Kill switch: flag OFF leaves the conflict in place")
os.environ["NIGHTSHIFT_LEDGER_ENFORCE"] = "0"
b = {"aggregated_totals": {"total_doors_full_paint": 136.0,
                           "total_doors_hm_panel": 12.0},
     "_quantity_adjustments": purdy_ledger()}
T._enforce_ledger_precedence(b)
check(b["aggregated_totals"]["total_doors_full_paint"] == 136,
      "flag OFF: no enforcement")
os.environ.pop("NIGHTSHIFT_LEDGER_ENFORCE", None)

# ---------------------------------------------------------------------------
print("\n3) Aggregate moved outside the ledger -> warning only, no clobber")
c = {"aggregated_totals": {"total_doors_full_paint": 200.0,
                           "total_doors_hm_panel": 12.0},
     "_quantity_adjustments": purdy_ledger()}
T._enforce_ledger_precedence(c)
check(c["aggregated_totals"]["total_doors_full_paint"] == 200.0,
      "current value matches neither side: untouched")
check(len(c.get("_ledger_precedence_warnings", [])) == 1
      and c["_ledger_precedence_warnings"][0]["item"] == "total_doors_full_paint",
      "warning recorded for the moved key")
check(c["aggregated_totals"]["total_doors_hm_panel"] == 6.0,
      "the still-in-effect conflict on the other key is enforced normally")

# ---------------------------------------------------------------------------
print("\n4) Later equal-rank entry takes over authority (no false positive)")
d = {"aggregated_totals": {"total_paintable_ceiling_sqft": 500.0},
     "_quantity_adjustments": [
         {"stage": "schedule_overrides", "item": "total_paintable_ceiling_sqft",
          "from": 900.0, "to": 700.0, "delta": -200.0, "source": "correction"},
         {"stage": "ceiling_gate", "item": "total_paintable_ceiling_sqft",
          "from": 700.0, "to": 500.0, "delta": -200.0, "source": "correction"},
     ]}
T._enforce_ledger_precedence(d)
check(d["aggregated_totals"]["total_paintable_ceiling_sqft"] == 500.0,
      "correction-after-correction: later one is authoritative, no revert")
check("_ledger_precedence_enforced" not in d, "no enforcement recorded")

# ---------------------------------------------------------------------------
print("\n5) Weak-over-weak is not a conflict (boost after supplement)")
e = {"aggregated_totals": {"total_paintable_wall_sqft": 1100.0},
     "_quantity_adjustments": [
         {"stage": "secondary_space_supplement", "item":
          "total_paintable_wall_sqft", "from": 800.0, "to": 1000.0,
          "delta": 200.0, "source": "assumed"},
         {"stage": "wall_boost", "item": "total_paintable_wall_sqft",
          "from": 1000.0, "to": 1100.0, "delta": 100.0, "source": "derived"},
     ]}
T._enforce_ledger_precedence(e)
check(e["aggregated_totals"]["total_paintable_wall_sqft"] == 1100.0,
      "no rank-3 authority for the key: enforcement stays out of it")

# ---------------------------------------------------------------------------
print("\n6) Integration: build_priced_takeoff enforces + keeps books honest")
f = {"aggregated_totals": {"total_doors_full_paint": 136.0,
                           "total_doors_hm_panel": 12.0},
     "_quantity_adjustments": purdy_ledger(), "notes": []}
T.build_priced_takeoff(f, strict=False)
agg = f["aggregated_totals"]
check(agg["total_doors_full_paint"] == 14 and agg["total_doors_hm_panel"] == 6,
      "choke point enforces before pricing: got "
      f"{agg['total_doors_full_paint']}/{agg['total_doors_hm_panel']}")
bd = f["_priced_takeoff"]["breakdown"]["total_doors_full_paint"]
check(bd["derived"] == 0 and bd["priced"] == 14,
      "reverted derived entries excluded from the provenance breakdown: "
      f"derived={bd['derived']}, priced={bd['priced']}")
check(not any(x["item"] == "total_doors_full_paint"
              for x in f["_priced_takeoff"]["exposures"]),
      "no phantom exposure from the reverted write")

# ---------------------------------------------------------------------------
print("\n7) Strict mode still removes live assumed increments after enforcement")
os.environ["NIGHTSHIFT_LEDGER_ENFORCE"] = "1"
g = {"aggregated_totals": {"total_doors_full_paint": 136.0,
                           "total_paintable_wall_sqft": 1200.0},
     "_quantity_adjustments": purdy_ledger() + [
         {"stage": "supplement", "item": "total_paintable_wall_sqft",
          "from": 1000.0, "to": 1200.0, "delta": 200.0, "source": "assumed"},
     ], "notes": []}
g["aggregated_totals"]["total_doors_hm_panel"] = 12.0
T.build_priced_takeoff(g, strict=True)
check(g["aggregated_totals"]["total_doors_full_paint"] == 14,
      "enforcement ran under strict mode")
check(g["aggregated_totals"]["total_paintable_wall_sqft"] == 1000.0,
      "strict gate still removed the live assumed wall increment: got "
      f"{g['aggregated_totals']['total_paintable_wall_sqft']}")
os.environ.pop("NIGHTSHIFT_LEDGER_ENFORCE", None)

# ---------------------------------------------------------------------------
print()
if fails:
    print(f"❌ {len(fails)} FAILURE(S):")
    for m in fails:
        print(f"   - {m}")
    sys.exit(1)
print("✅ all ledger precedence tests passed")
