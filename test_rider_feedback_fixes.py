"""Regression tests for the three Rider-feedback fixes (Biddle Residence,
2026-07-21):

  1. NIGHTSHIFT_RESIDENTIAL_ELEV_PASS — _exterior_paint_signal lets the
     dedicated elevation pass fire on non-commercial jobs whose extraction
     shows exterior-paint scope. Biddle's A200/A201 elevations were in the
     upload but never read because building_type was residential; the
     painted second-floor vinyl siding shipped at $0 with an RFI asking the
     customer for sheets they had already submitted.
  2. NIGHTSHIFT_PAINTED_CABINETS — _apply_painted_cabinet_gate: a finish-
     schedule row calling for field-painted cabinets ('Kitchen/Dining -
     Painted Cabinets / Paint - Benjamin Moore Advance (PT-4)') must price
     measured SF or RFI — never vanish silently.
  3. NIGHTSHIFT_STAINED_WOOD_GATE — _apply_stained_wood_gate: stained wood
     with no schedule confirmation of field stain/clear-coat scope (veneer/
     laminate are factory finishes) is zeroed with an RFI. Biddle shipped a
     $311 'Specialty coatings' line for 48 SF 'recorded as estimate'.

Offline, no API.
"""
import os
import Takeoff_DIRECT as T
import generate_estimate_pdf as G

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


def _clear_flags():
    for f in ("NIGHTSHIFT_RESIDENTIAL_ELEV_PASS", "NIGHTSHIFT_PAINTED_CABINETS",
              "NIGHTSHIFT_STAINED_WOOD_GATE"):
        os.environ.pop(f, None)


# ── 1) Flags default OFF ──────────────────────────────────────────────────
_clear_flags()
check(T._residential_elev_pass_enabled() is False, "res-elev flag should default off")
check(T._painted_cabinets_enabled() is False, "cabinets flag should default off")
check(T._stained_wood_gate_enabled() is False, "stained-wood flag should default off")

# ── 2) _exterior_paint_signal ─────────────────────────────────────────────
# Quantity signal (Biddle: hardie 1200 / soffit 120 with exterior_paint 0).
a = {"exterior": {"exterior_paint_sqft": 0, "hardie_siding_sqft": 1200.0,
                  "soffit_sqft": 120.0}, "notes": []}
sig = T._exterior_paint_signal(a)
check(sig is not None and "hardie_siding_sqft" in sig,
      f"quantity signal missed: {sig}")

# Note-language signal — the literal Biddle A500 wording.
a = {"exterior": {}, "notes": [
    "[A500] Scope note 3: Existing second floor vinyl siding, trim, and "
    "windows to be PAINTED to match new exterior siding and trim."]}
sig = T._exterior_paint_signal(a)
check(sig is not None and "siding" in sig.lower(), f"note signal missed: {sig}")

# Negated note must NOT trigger.
a = {"exterior": {}, "notes": [
    "No exterior painting in scope — exterior paint excluded per owner."]}
check(T._exterior_paint_signal(a) is None, "negated 'no exterior' note triggered")

# Interior-only veneer note (paint word without exterior surface word).
a = {"exterior": {}, "notes": [
    "These are stained/clear-coat wood surfaces, not painted."]}
check(T._exterior_paint_signal(a) is None, "interior veneer note triggered")

# ── 3) _schedule_painted_cabinet_rows ─────────────────────────────────────
biddle_rows = [
    {"room_name": "Kitchen/Dining - Painted Cabinets",
     "wall_finish": "Paint - Benjamin Moore Advance (PT-4)"},
    {"room_name": "Kitchen - Tall Cabinets", "wall_finish": "Wood Veneer (WD-03)"},
    {"room_name": "Kitchen - Tall Cabinets (Laminate)",
     "wall_finish": "Laminate - Fenix/Bianco Male Veneer (LM-2)"},
    {"room_name": "Throughout - Walls",
     "wall_finish": "Paint - Benjamin Moore/Moore Aura, Matte (PT-1)"},
]
rows = T._schedule_painted_cabinet_rows(biddle_rows)
check(len(rows) == 1 and "Painted Cabinets" in rows[0]["room_name"],
      f"painted-cabinet row detection wrong: {[r.get('room_name') for r in rows]}")

# ── 4) _apply_painted_cabinet_gate ────────────────────────────────────────
def _cab_analysis(schedule, measured_sqft):
    return {"room_finish_schedule": list(schedule),
            "aggregated_totals": {"total_painted_cabinet_sqft": measured_sqft},
            "floors": [], "notes": []}

os.environ["NIGHTSHIFT_PAINTED_CABINETS"] = "1"
# 0 SF measured → RFI, no fabricated quantity.
a = T._apply_painted_cabinet_gate(_cab_analysis(biddle_rows, 0))
rfis = a.get("_pre_pricing_rfis", [])
check(any(r.get("category") == "Painted Cabinets" for r in rfis),
      "0-SF painted-cabinet row did not RFI")
check(a["aggregated_totals"]["total_painted_cabinet_sqft"] == 0,
      "gate fabricated cabinet area")
check(a["_painted_cabinet_gate"]["rfi_issued"] is True, "audit missing rfi_issued")
# Measured SF → priced note, no RFI.
a = T._apply_painted_cabinet_gate(_cab_analysis(biddle_rows, 96.0))
check(not any(r.get("category") == "Painted Cabinets"
              for r in a.get("_pre_pricing_rfis", [])),
      "measured cabinets still RFI'd")
check(any("Painted Cabinets" in str(n) for n in a["notes"]),
      "measured cabinets note missing")
# No cabinet rows → noop.
a = T._apply_painted_cabinet_gate(_cab_analysis(biddle_rows[3:], 0))
check(a["_painted_cabinet_gate"].get("noop") == "no_painted_cabinet_rows",
      "no-rows case not a noop")
# Flag off → untouched.
_clear_flags()
a = _cab_analysis(biddle_rows, 0)
T._apply_painted_cabinet_gate(a)
check("_painted_cabinet_gate" not in a, "gate ran with flag off")

# ── 5) _schedule_confirms_field_stained_wood ──────────────────────────────
veneer_only = [
    {"room_name": "Kitchen - Tall Cabinets", "wall_finish": "Wood Veneer (WD-03)"},
    {"room_name": "Entry Closet",
     "floor_finish": "Wood Flooring - Oak, Site Finished, 7\" Boards (WD-01)"},
    {"room_name": "Service", "wall_finish": "Stainless steel panels"},
]
check(T._schedule_confirms_field_stained_wood(veneer_only) is None,
      "veneer/floor/stainless schedule wrongly confirmed field stain")
confirmed = veneer_only + [
    {"room_name": "Library", "wall_finish": "Oak panels - stain and clear coat (ST-1)"}]
check(T._schedule_confirms_field_stained_wood(confirmed) is not None,
      "explicit stain row did not confirm")

# ── 6) _apply_stained_wood_gate ───────────────────────────────────────────
def _sw_analysis(schedule, sw_sqft):
    return {
        "room_finish_schedule": list(schedule),
        "aggregated_totals": {"total_stained_wood_sqft": sw_sqft},
        "floors": [{"floor_name": "1", "rooms": [
            {"room_name": "Kitchen", "in_scope": True,
             "elements": {"stained_wood_sqft": sw_sqft},
             "dimensions": {}}]}],
        "notes": [],
    }

# Authoritative schedule (>=5 rows), no confirmation → zeroed + RFI.
thick = veneer_only + [
    {"room_name": f"Room {i}", "wall_finish": "Paint (PT-1)"} for i in range(4)]
os.environ["NIGHTSHIFT_STAINED_WOOD_GATE"] = "1"
a = T._apply_stained_wood_gate(_sw_analysis(thick, 48.0))
check(a["aggregated_totals"]["total_stained_wood_sqft"] == 0,
      "unconfirmed stained wood not zeroed")
check(a["floors"][0]["rooms"][0]["elements"]["stained_wood_sqft"] == 0,
      "per-room stained wood not zeroed")
check(any(r.get("category") == "Stained Wood"
          for r in a.get("_pre_pricing_rfis", [])), "zeroing did not RFI")
# Confirmed schedule → kept.
a = T._apply_stained_wood_gate(_sw_analysis(
    thick + [{"room_name": "Library",
              "wall_finish": "Oak - stain and clear coat"}], 48.0))
check(a["aggregated_totals"]["total_stained_wood_sqft"] == 48.0,
      "confirmed stained wood wrongly zeroed")
# Thin schedule → fail-safe keep.
a = T._apply_stained_wood_gate(_sw_analysis(veneer_only[:2], 48.0))
check(a["aggregated_totals"]["total_stained_wood_sqft"] == 48.0,
      "thin-schedule job wrongly zeroed")
check(a["_stained_wood_gate"].get("noop") == "schedule_too_thin",
      "thin-schedule audit wrong")
# Zero quantity → noop.
a = T._apply_stained_wood_gate(_sw_analysis(thick, 0))
check(a["_stained_wood_gate"].get("noop") == "no_stained_wood",
      "zero-SF case not a noop")
# Flag off → untouched.
_clear_flags()
a = _sw_analysis(thick, 48.0)
T._apply_stained_wood_gate(a)
check("_stained_wood_gate" not in a and
      a["aggregated_totals"]["total_stained_wood_sqft"] == 48.0,
      "stained-wood gate ran with flag off")

# ── 7) Pricing plumbing ───────────────────────────────────────────────────
import config
check("painted_cabinets" in config.PRICING_MODEL,
      "config missing painted_cabinets pricing entry")
check(config.PRICING_MODEL["painted_cabinets"]["unit"] == "sqft",
      "painted_cabinets unit should be sqft")

# ── 8) Estimate-PDF grouping ──────────────────────────────────────────────
def _pdf_result(items):
    return {"cost_estimate": {"line_items": items}}

sw_only = [{"item": "Stained Wood Panels - 48 sqft @ $6.00",
            "qty": 48, "total": 311.04}]
# Flag off → legacy label.
_clear_flags()
rows = G._build_line_items(_pdf_result(sw_only))
check(rows and rows[0]["title"] == "Specialty coatings",
      f"flag-off specialty label changed: {rows}")
# Flag on + stained-wood-only bucket → renamed.
os.environ["NIGHTSHIFT_STAINED_WOOD_GATE"] = "1"
rows = G._build_line_items(_pdf_result(sw_only))
check(rows and rows[0]["title"] == "Stained & clear-coat woodwork",
      f"stained-wood-only bucket not renamed: {rows}")
# Mixed specialty bucket → label unchanged.
mixed = sw_only + [{"item": "CMU Walls (Full System) - 100 sqft @ $1.10",
                    "qty": 100, "total": 121.0}]
rows = G._build_line_items(_pdf_result(mixed))
check(rows and rows[0]["title"] == "Specialty coatings",
      f"mixed specialty bucket wrongly renamed: {rows}")
# Painted-cabinet line lands in the Trim bucket.
cab = [{"item": "Painted Cabinets - 96 sqft @ $8.00", "qty": 96, "total": 798.72}]
rows = G._build_line_items(_pdf_result(cab))
check(rows and rows[0]["title"] == "Trim, doors, and windows",
      f"cabinet line not grouped under Trim: {rows}")
_clear_flags()

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
