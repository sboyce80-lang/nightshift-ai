"""Tests for evidence-gated ALLOWANCE line items (the line-policy fix).

Allowances capture scope the drawings call for but that can't be measured to a
hard number (exterior CMU, columns, misc metals, accent bands). They must:
  - emit ONLY when config.ALLOWANCE_LINES_ENABLED is on,
  - emit ONLY when a trigger note is present (no bare assumptions),
  - size HYBRID: geometry where it exists, flat LS where it doesn't,
  - never fire for scope already measured (>0).
"""
import sys

import config
import Takeoff_DIRECT as T

_fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        _fails.append(name)


def build(notes, footprint=26387, columns_measured=0, ext_measured=0,
          is_commercial=True, enabled=True):
    config.ALLOWANCE_LINES_ENABLED = enabled
    analysis = {"notes": notes, "floors": []}
    agg = {"total_painted_columns_ea": columns_measured}
    ext = {"exterior_paint_sqft": ext_measured}
    return T._build_allowance_lines(
        analysis=analysis, aggregated_totals=agg, exterior=ext,
        project_info={"footprint_sqft": footprint}, footprint=footprint,
        is_commercial=is_commercial,
        rates={"columns": 125.0, "exterior_cmu": 1.80, "misc_metals": 18.0},
        markups={"columns": 0.06, "exterior_cmu": 0.06, "misc_metals": 0.06})


def items(lines):
    return {l["item"].split(" (")[0] for l in lines}


print("Gating — flag OFF suppresses everything")
check("flag OFF -> [] even with triggers",
      build(["paint all columns", "rfi: exterior scope"], enabled=False) == [])

print("\nEvidence-gating — no trigger -> no line")
check("commercial box, no notes -> no allowances", build([""]) == [])

print("\nColumns — trigger fires, qty = footprint / bay")
cols = [l for l in build(["General note E: paint all columns visible to customer"])
        if l["item"].startswith("Painted Columns")]
check("column allowance emitted", len(cols) == 1)
if cols:
    check("count = round(26387/1500) = 18", cols[0]["qty"] == 18, f"got {cols[0]['qty']}")
    check("unit EA", cols[0]["unit"] == "EA")
    check("basis cites the bay heuristic", "bay" in cols[0]["basis"].lower())

print("\nColumns — already measured -> NO allowance (no double-count)")
cols2 = [l for l in build(["paint all columns"], columns_measured=12)
         if l["item"].startswith("Painted Columns")]
check("measured columns suppress the allowance", cols2 == [])

print("\nExterior CMU — trigger + 0 measured -> geometry-sized SF")
ext = [l for l in build(["[RFI: Exterior Scope] commercial building with 0 sqft exterior"])
       if l["item"].startswith("Exterior CMU")]
check("exterior allowance emitted", len(ext) == 1)
if ext:
    check("qty in plausible range (8k-11k SF for 26k box)",
          8000 < ext[0]["qty"] < 11000, f"got {ext[0]['qty']}")
    check("unit SF", ext[0]["unit"] == "SF")

print("\nExterior — already measured -> NO allowance")
ext2 = [l for l in build(["rfi: exterior scope"], ext_measured=5000)
        if l["item"].startswith("Exterior CMU")]
check("measured exterior suppresses the allowance", ext2 == [])

print("\nMisc metals — railing trigger -> flat LS placeholder")
misc = [l for l in build(["coded note 25: straighten, repair and paint all railing"])
        if l["item"].startswith("Misc Metals")]
check("misc-metals allowance emitted", len(misc) == 1)
if misc:
    check("unit LS", misc[0]["unit"] == "LS")
    check("flat placeholder qty=1", misc[0]["qty"] == 1)

print("\nAccent band — only when a stripe/band note exists")
check("no stripe note -> no band line",
      not any(l["item"].startswith("Accent") for l in build(["paint all columns"])))
band = [l for l in build(["paint accent band per color schedule"])
        if l["item"].startswith("Accent")]
check("stripe note -> band line emitted", len(band) == 1)

print("\nTSC composite — columns + exterior + misc, no band/conduit")
tsc = build(["[a1.0] general note e: paint all columns",
             "[rfi: exterior scope] 0 sqft exterior paint from elevation pages",
             "coded note 25: paint all railing - lf unconfirmed",
             "general note 18: conceal all piping in walls"])
check("exactly 3 allowance lines", len(tsc) == 3, f"got {len(tsc)}: {sorted(items(tsc))}")
check("no accent band (no stripe note)", not any(l["item"].startswith("Accent") for l in tsc))
check("no conduit line (piping is concealed, not exposed)",
      not any("conduit" in l["item"].lower() for l in tsc))

config.ALLOWANCE_LINES_ENABLED = False  # restore default
print(f"\n=== {'ALL PASS' if not _fails else str(len(_fails)) + ' FAILED: ' + ', '.join(_fails)} ===")
sys.exit(1 if _fails else 0)
