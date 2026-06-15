#!/usr/bin/env python3
"""Offline tests for Phase 2.3: the quantity-adjustment ledger + the
build_priced_takeoff provenance gate.

Covers ledger snapshot/diff semantics, source tagging (measured/schedule/
derived/assumed/correction), the measured-vs-assumed breakdown, strict-mode
removal of assumed increments + unpriced-exposure notes, idempotency, the
late-recalc clamp, and flag gating. No API calls, no PDFs.

Run: python3 test_provenance_gate.py
"""
import importlib.util as iu
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
spec = iu.spec_from_file_location("T", os.path.join(HERE, "Takeoff_DIRECT.py"))
T = iu.module_from_spec(spec)
spec.loader.exec_module(T)

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def A(**totals):
    return {"aggregated_totals": dict(totals)}


def main():
    print("\n── Flag gating ──")
    os.environ.pop("NIGHTSHIFT_PROVENANCE_GATE", None)
    check("provenance gate defaults OFF", T._provenance_gate_enabled() is False)
    os.environ["NIGHTSHIFT_PROVENANCE_GATE"] = "1"
    check("provenance gate enables with env=1",
          T._provenance_gate_enabled() is True)
    os.environ.pop("NIGHTSHIFT_PROVENANCE_GATE", None)

    print("\n── Snapshot ──")
    snap = T._agg_snapshot(A(total_paintable_wall_sqft=1000, bad="x",
                             total_doors_full_paint=10))
    check("snapshot keeps numeric keys", snap.get("total_paintable_wall_sqft") == 1000)
    check("snapshot drops non-numeric keys", "bad" not in snap)
    check("snapshot of empty analysis is empty dict", T._agg_snapshot({}) == {})

    print("\n── Ledger stage diff ──")
    an = A(total_paintable_wall_sqft=1000, total_base_trim_lf=500)
    before = T._agg_snapshot(an)
    an["aggregated_totals"]["total_paintable_wall_sqft"] = 1200  # +200 boost
    after = T._ledger_stage(an, "wall_boost", before, source="derived")
    led = an["_quantity_adjustments"]
    check("one entry recorded for the changed key", len(led) == 1)
    e = led[0]
    check("entry has stage/item/from/to/delta",
          e["stage"] == "wall_boost" and e["item"] == "total_paintable_wall_sqft"
          and e["from"] == 1000 and e["to"] == 1200 and e["delta"] == 200)
    check("positive delta keeps its source tag", e["source"] == "derived")
    check("ledger_stage returns a fresh snapshot for chaining",
          after["total_paintable_wall_sqft"] == 1200)
    check("sub-0.5 changes are ignored",
          (an["aggregated_totals"].__setitem__("total_base_trim_lf", 500.2)
           or T._ledger_stage(an, "noop", after) is not None)
          and len(an["_quantity_adjustments"]) == 1)

    print("\n── Source tagging: reduction is always a correction ──")
    an = A(total_windows_painted_interior=40)
    before = T._agg_snapshot(an)
    an["aggregated_totals"]["total_windows_painted_interior"] = 0  # exclusion
    T._ledger_stage(an, "commercial_window_exclusion", before, source="assumed")
    check("a REDUCTION is tagged 'correction' even if source=assumed",
          an["_quantity_adjustments"][0]["source"] == "correction")

    print("\n── build_priced_takeoff: breakdown (non-strict) ──")
    # walls: 800 measured baseline, +100 derived boost, +200 assumed supplement
    an = A(total_paintable_wall_sqft=800)
    snap = T._agg_snapshot(an)
    an["aggregated_totals"]["total_paintable_wall_sqft"] = 900
    snap = T._ledger_stage(an, "wall_boost", snap, source="derived")
    an["aggregated_totals"]["total_paintable_wall_sqft"] = 1100
    snap = T._ledger_stage(an, "secondary_space_supplement", snap, source="assumed")
    T.build_priced_takeoff(an, strict=False)
    bd = an["_priced_takeoff"]["breakdown"]["total_paintable_wall_sqft"]
    check("non-strict leaves priced total intact",
          an["aggregated_totals"]["total_paintable_wall_sqft"] == 1100
          and bd["priced"] == 1100)
    check("derived portion captured", bd["derived"] == 100)
    check("assumed portion captured", bd["assumed"] == 200)
    check("measured = final - derived - assumed", bd["measured"] == 800)
    check("exposure listed (assumed>0)",
          any(x["item"] == "total_paintable_wall_sqft"
              for x in an["_priced_takeoff"]["exposures"]))
    check("non-strict adds no unpriced-exposure note",
          not any("Unpriced Exposure" in n for n in an.get("notes", [])))

    print("\n── build_priced_takeoff: strict mode removes assumed ──")
    an = A(total_paintable_wall_sqft=800, total_paintable_ceiling_sqft=600)
    snap = T._agg_snapshot(an)
    an["aggregated_totals"]["total_paintable_wall_sqft"] = 900       # +100 derived
    snap = T._ledger_stage(an, "wall_boost", snap, source="derived")
    an["aggregated_totals"]["total_paintable_wall_sqft"] = 1100      # +200 assumed
    an["aggregated_totals"]["total_paintable_ceiling_sqft"] = 950    # +350 assumed
    snap = T._ledger_stage(an, "supplement", snap, source="assumed")
    T.build_priced_takeoff(an, strict=True)
    agg = an["aggregated_totals"]
    check("strict removes assumed wall increment (1100 - 200 = 900)",
          agg["total_paintable_wall_sqft"] == 900)
    check("strict KEEPS the derived increment (measured-backed)",
          an["_priced_takeoff"]["breakdown"]["total_paintable_wall_sqft"]["priced"] == 900)
    check("strict removes assumed ceiling increment (950 - 350 = 600)",
          agg["total_paintable_ceiling_sqft"] == 600)
    notes = an.get("notes", [])
    check("strict files an unpriced-exposure note per exposed item",
          sum(1 for n in notes if "Unpriced Exposure" in n) == 2)
    check("exposure note carries RFI REQUIRED marker",
          all("RFI REQUIRED" in n for n in notes if "Unpriced Exposure" in n))

    print("\n── Idempotency ──")
    an = A(total_paintable_wall_sqft=1000)
    snap = T._agg_snapshot(an)
    an["aggregated_totals"]["total_paintable_wall_sqft"] = 1200
    T._ledger_stage(an, "supplement", snap, source="assumed")
    T.build_priced_takeoff(an, strict=True)
    first = an["aggregated_totals"]["total_paintable_wall_sqft"]
    T.build_priced_takeoff(an, strict=True)   # second call must be a no-op
    check("strict gate is idempotent (no double subtraction)",
          an["aggregated_totals"]["total_paintable_wall_sqft"] == first == 1000)
    check("idempotency flag set", an.get("_priced_takeoff_built") is True)

    print("\n── Late-recalc clamp ──")
    # Ledger says +500 assumed, but a later recalc wiped agg back below the
    # would-be assumed increment: gate must not drive the total negative.
    an = A(total_paintable_wall_sqft=300)
    led = an.setdefault("_quantity_adjustments", [])
    led.append({"stage": "supplement", "item": "total_paintable_wall_sqft",
                "from": 300, "to": 800, "delta": 500, "source": "assumed"})
    T.build_priced_takeoff(an, strict=True)
    check("clamp: wiped assumed increment never drives priced negative",
          an["aggregated_totals"]["total_paintable_wall_sqft"] >= 0)
    check("clamp: stale exposure not double-counted as removable",
          an["_priced_takeoff"]["breakdown"]["total_paintable_wall_sqft"]["priced"] == 300)

    print("\n── Robustness ──")
    check("build_priced_takeoff on non-dict returns input",
          T.build_priced_takeoff(None) is None)
    empty = {"aggregated_totals": {}}
    T.build_priced_takeoff(empty, strict=True)
    check("empty totals → empty breakdown, no crash",
          empty["_priced_takeoff"]["breakdown"] == {})
    no_ledger = A(total_doors_full_paint=159)
    T.build_priced_takeoff(no_ledger, strict=True)
    check("no ledger → everything is measured, nothing removed",
          no_ledger["aggregated_totals"]["total_doors_full_paint"] == 159
          and no_ledger["_priced_takeoff"]["breakdown"]
          ["total_doors_full_paint"]["measured"] == 159
          and no_ledger["_priced_takeoff"]["exposures"] == [])

    print(f"\n=== {PASS} passed, {FAIL} failed ===")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
