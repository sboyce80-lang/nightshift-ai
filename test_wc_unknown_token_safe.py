#!/usr/bin/env python3
"""WC unknown-token safe mode (NIGHTSHIFT_WC_UNKNOWN_TOKEN_SAFE).

Caris 2026-08-25 (K=3 validation): the finish schedule's common-area
rows read 'GYP BD / BB PNT / FF'. The extraction pass — which saw the
sheet's legend — assigned those 13 hallway/foyer rooms wallcovering
(matching JW's bid), but the WC gate treated the unrecognized 'FF'
token as a paint-only designation and zeroed 8-10k SF in every draw
(~$15-40k, the whole band miss). An unknown token is not a non-WC
designation. Locks in: flag off = legacy zeroing; on = matched rows
with unknown tokens keep extracted WC + RFI; fully-understood paint
rows still zero; token classifier ignores numbered codes."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import Takeoff_DIRECT as T  # noqa: E402

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"  ❌ {msg}")
    else:
        print("  ✓ " + msg.split(":")[0])


def _clear():
    for k in ("NIGHTSHIFT_WC_SCHEDULE_GATE", "NIGHTSHIFT_WC_UNKNOWN_TOKEN_SAFE",
              "NIGHTSHIFT_WC_SCHEDULE_AUTHORITATIVE",
              "NIGHTSHIFT_WC_MIXED_SHARE", "NIGHTSHIFT_WC_TYPICAL_MATCH"):
        os.environ.pop(k, None)


print("— token classifier —")
unk = T._wall_finish_unknown_tokens
check(unk("GYP BD / BB PNT / FF") == {"ff"},
      f"FF must be unknown: {unk('GYP BD / BB PNT / FF')}")
check(unk("GYP BD PNT") == set(), "plain paint row fully understood")
check(unk("WC 01, PT 03") == set(),
      f"numbered codes understood: {unk('WC 01, PT 03')}")
check(unk("PT-3 / WD1 / WC01") == set(),
      f"compact numbered codes understood: {unk('PT-3 / WD1 / WC01')}")
check("vin" in unk("VIN / GYP BD"),
      f"ambiguous vinyl token stays unknown: {unk('VIN / GYP BD')}")


def caris():
    # 8 numbered rows (>= authority min), no recognizable WC rows;
    # extraction assigned WC to the hallway/foyer rooms per the legend.
    rows = [{"room_name": n, "room_number": str(130 + i),
             "wall_finish": wf}
            for i, (n, wf) in enumerate([
                ("Foyer", "GYP BD / BB PNT / FF"),
                ("Office", "GYP BD / BB PNT / FF"),
                ("Hallway", "GYP BD / BB PNT / FF"),
                ("Single", "GYP BD PNT"),
                ("Single", "GYP BD PNT"),
                ("Bathroom", "GYP BD PNT"),
                ("Library", "GYP BD / BB PNT / FF"),
                ("Storage", "GYP BD PNT"),
            ])]
    return {
        "room_finish_schedule": rows,
        "has_finish_schedule": True,
        "floors": [{"floor_name": "Main Level", "rooms": [
            {"room_name": "Foyer", "room_number": "130", "in_scope": True,
             "dimensions": {"wall_area_sqft": 800.0},
             "elements": {"wallcovering_sqft": 800.0}},
            {"room_name": "Hallway", "room_number": "132",
             "in_scope": True,
             "dimensions": {"wall_area_sqft": 600.0},
             "elements": {"wallcovering_sqft": 600.0}},
            {"room_name": "Storage", "room_number": "137",
             "in_scope": True,
             "dimensions": {"wall_area_sqft": 300.0},
             "elements": {"wallcovering_sqft": 300.0}},
        ]}],
        "aggregated_totals": {"total_wallcovering_sqft": 1700.0},
    }


print("\n— flag off: legacy zeroing (all three rooms) —")
_clear()
os.environ["NIGHTSHIFT_WC_SCHEDULE_GATE"] = "1"
a = T._enforce_wallcovering_schedule_gate(caris())
rec = a["_wc_schedule_gate"]
check(rec["zeroed_sqft"] == 1700.0,
      f"legacy behavior must zero all: {rec['zeroed_sqft']}")

print("\n— flag on: unknown-token rooms keep WC, understood rooms zero —")
_clear()
os.environ["NIGHTSHIFT_WC_SCHEDULE_GATE"] = "1"
os.environ["NIGHTSHIFT_WC_UNKNOWN_TOKEN_SAFE"] = "1"
a = T._enforce_wallcovering_schedule_gate(caris())
rec = a["_wc_schedule_gate"]
rooms = a["floors"][0]["rooms"]
check(rooms[0]["elements"]["wallcovering_sqft"] == 800.0
      and rooms[1]["elements"]["wallcovering_sqft"] == 600.0,
      f"FF rooms must keep WC: {[r['elements'] for r in rooms]}")
check(rooms[2]["elements"]["wallcovering_sqft"] == 0,
      f"fully-understood paint row must still zero: {rooms[2]['elements']}")
check(rec["unknown_token_kept_sqft"] == 1400.0
      and rec["zeroed_sqft"] == 300.0,
      f"record wrong: {rec}")
check(any("UNRECOGNIZED finish token" in str(n) for n in a["notes"]),
      "reviewer note missing")
rfis = a.get("_pre_pricing_rfis") or []
check(any("does not recognize" in str(r) for r in rfis)
      or any("Wallcovering" == r.get("category") for r in rfis),
      f"RFI missing: {rfis}")

print("\n— mode 'paint': unknown-token WC reclassifies to painted wall —")
_clear()
os.environ["NIGHTSHIFT_WC_SCHEDULE_GATE"] = "1"
os.environ["NIGHTSHIFT_WC_UNKNOWN_TOKEN_SAFE"] = "paint"
ap = caris()
# give the Foyer a wall area that ALREADY includes the WC area — the
# reclass must not double-count it
ap["floors"][0]["rooms"][0]["dimensions"]["wall_area_sqft"] = 900.0
ap["aggregated_totals"]["total_paintable_wall_sqft"] = 900.0
a = T._enforce_wallcovering_schedule_gate(ap)
rec = a["_wc_schedule_gate"]
rooms = a["floors"][0]["rooms"]
check(rooms[0]["elements"]["wallcovering_sqft"] == 0
      and rooms[1]["elements"]["wallcovering_sqft"] == 0,
      f"paint mode must clear room WC: {[r['elements'] for r in rooms]}")
check(rooms[0]["dimensions"]["wall_area_sqft"] == 900.0,
      f"wall area already >= WC must not grow: "
      f"{rooms[0]['dimensions']}")
check(rooms[1]["dimensions"]["wall_area_sqft"] == 600.0,
      f"wall area == WC must stay (full wall already recorded): "
      f"{rooms[1]['dimensions']}")
check(rec["unknown_token_paint_sqft"] == 1400.0
      and rec["unknown_token_kept_sqft"] == 0,
      f"paint record wrong: {rec}")
agg = a["aggregated_totals"]
check(agg["total_wallcovering_sqft"] == 0.0,
      f"agg WC must drop by reclassified + zeroed SF: {agg}")
check(agg["total_paintable_wall_sqft"] == 900.0,
      f"agg painted walls must NOT change (deduct no-ops on zero WC): "
      f"{agg}")
# a room whose extraction recorded LESS wall than WC gets repaired
short = caris()
short["floors"][0]["rooms"][0]["dimensions"]["wall_area_sqft"] = 500.0
s = T._enforce_wallcovering_schedule_gate(short)
check(s["floors"][0]["rooms"][0]["dimensions"]["wall_area_sqft"] == 800.0,
      f"short wall area must repair to WC size: "
      f"{s['floors'][0]['rooms'][0]['dimensions']}")
check(any("PRICED AS PAINTED WALL" in str(n) for n in a["notes"]),
      "paint-mode note missing")
check(rooms[2]["elements"]["wallcovering_sqft"] == 0,
      "fully-understood paint row still zeroes in paint mode")

print("\n— idempotence-ish: second pass is a no-op —")
before = rec
a2 = T._enforce_wallcovering_schedule_gate(a)
check(a2["_wc_schedule_gate"] is before, "gate must not re-run")

_clear()
print()
if fails:
    print(f"❌ {len(fails)} check(s) failed")
    sys.exit(1)
print("✅ all WC unknown-token checks passed")
