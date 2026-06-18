"""Regression tests for two INNIO Waukesha beta-job fixes (2026-06-17).

Fix #1 — Will must not claim a *processed* sheet was "not submitted".
  The synthesis layer hallucinated that A401 was missing while the pipeline
  had anchored all 6 of its rooms. _sanitize_missing_sheet_claims() is the
  deterministic backstop that strips such claims using analysis["_sheet_pages"].

Fix #2 — Cross-sheet wall-height back-fill (NIGHTSHIFT_CROSS_SHEET_HEIGHT_BACKFILL).
  Rooms whose source sheet carried a perimeter but no section/RCP land with
  ceiling_height_feet = 0 and wall_area_sqft = 0. When enabled, they inherit the
  project's confirmed scheduled height so the walls are recovered, classified,
  priced, and surfaced as a confirm-before-bid RFI.

Offline, no API.
"""
import os
import Takeoff_DIRECT as T
import will_synthesis as W

fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)


# ---------------------------------------------------------------------------
# Fix #1 — sanitize false "sheet not submitted" claims
# ---------------------------------------------------------------------------

def _analysis_with_sheets():
    return {"_sheet_pages": [
        {"page": 7, "sheet_id": "A401", "rooms": 6},
        {"page": 9, "sheet_id": "A403", "rooms": 5},
    ]}


# A401 IS processed -> the false claim must be stripped from both fields.
wo = {
    "pipeline_flags": {"missing_information": [
        "Sheet A401 (Production Break & Toilet Rooms Plan) — not submitted",
        "Finish schedule / room finish schedule — not submitted",
    ]},
    "additional_rfis": [
        {"question": "Sheet A401 was NOT included in the drawing set submitted for takeoff.",
         "action_required": "Provide sheet A401."},
        {"question": "No reflected ceiling plan was provided.",
         "action_required": "Provide RCP."},
    ],
}
wo = W._sanitize_missing_sheet_claims(wo, _analysis_with_sheets())
mi = wo["pipeline_flags"]["missing_information"]
check(all("A401" not in m for m in mi), "A401 'not submitted' must be removed from missing_information")
check(any("Finish schedule" in m for m in mi), "legitimate missing-schedule entry must survive")
check(len(wo["additional_rfis"]) == 1, "the 'provide sheet A401' RFI must be dropped")
check(wo["additional_rfis"][0]["question"].startswith("No reflected"), "legitimate RCP RFI must survive")
check(len(wo.get("_sanitized_sheet_claims", [])) == 2, "both sanitized claims must be logged")

# A genuinely-absent sheet (A201) must NOT be touched.
wo2 = {
    "pipeline_flags": {"missing_information": ["Sheet A201 (Elevations) — not submitted"]},
    "additional_rfis": [],
}
wo2 = W._sanitize_missing_sheet_claims(wo2, _analysis_with_sheets())
check(wo2["pipeline_flags"]["missing_information"] == ["Sheet A201 (Elevations) — not submitted"],
      "claim about a genuinely-missing sheet must be preserved")

# No _sheet_pages -> sanitizer is a no-op (can't prove anything present).
wo3 = {"pipeline_flags": {"missing_information": ["Sheet A401 — not submitted"]}, "additional_rfis": []}
wo3 = W._sanitize_missing_sheet_claims(wo3, {})
check(wo3["pipeline_flags"]["missing_information"] == ["Sheet A401 — not submitted"],
      "with no sheet list, nothing is stripped")

# Substring safety: "A40" must not match "A401".
wo4 = {"pipeline_flags": {"missing_information": ["Sheet A40 — not submitted"]}, "additional_rfis": []}
wo4 = W._sanitize_missing_sheet_claims(wo4, _analysis_with_sheets())
check(wo4["pipeline_flags"]["missing_information"] == ["Sheet A40 — not submitted"],
      "word-boundary match must not strip a different sheet id")

# Payload exposes sheets_processed for Will to read.
payload = W._build_review_payload(_analysis_with_sheets(),
                                  {"line_items": [], "subtotal": 0, "exclusions": []}, [], {})
ids = {s["sheet_id"] for s in payload["sheets_processed"]}
check(ids == {"A401", "A403"}, "sheets_processed must list every processed sheet")


# ---------------------------------------------------------------------------
# Fix #2 — cross-sheet wall-height back-fill
# ---------------------------------------------------------------------------

def _job():
    # One room carries a confirmed 9' height (the A403-style companion sheet);
    # two rooms have perimeter/dims but no height (the A401-style thin sheet).
    return {"project_info": {"building_type": "commercial"},
            "floors": [{"floor_name": "1st Floor", "rooms": [
                {"room_name": "Test Cell", "in_scope": True,
                 "dimensions": {"ceiling_height_feet": 9, "perimeter_lf": 200,
                                "wall_area_sqft": 1800, "floor_area_sqft": 2400},
                 "materials": {"walls": "GYP"}, "elements": {}},
                {"room_name": "Men Toilet", "in_scope": True,
                 "dimensions": {"ceiling_height_feet": 0, "perimeter_lf": 60,
                                "wall_area_sqft": 0, "floor_area_sqft": 219},
                 "materials": {"walls": "CMU"}, "elements": {}},
                {"room_name": "Closet", "in_scope": True,  # no perimeter; derive from L/W
                 "dimensions": {"ceiling_height_feet": 0, "length_feet": 10, "width_feet": 10,
                                "wall_area_sqft": 0, "floor_area_sqft": 100},
                 "materials": {"walls": "CMU"}, "elements": {}},
            ]}]}


def _walls(a):
    rs = a["floors"][0]["rooms"]
    return {r["room_name"]: T._num(r["dimensions"].get("wall_area_sqft", 0)) for r in rs}


# Flag OFF -> no-op.
os.environ.pop("NIGHTSHIFT_CROSS_SHEET_HEIGHT_BACKFILL", None)
T._cfg.CROSS_SHEET_HEIGHT_BACKFILL = False
a = _job()
T._backfill_missing_wall_heights(a)
check(a.get("_cross_sheet_height_backfilled") is None, "flag OFF must skip back-fill")
check(_walls(a)["Men Toilet"] == 0, "flag OFF must leave zero-wall rooms at zero")

# Flag ON -> recover walls at the project height (9').
T._cfg.CROSS_SHEET_HEIGHT_BACKFILL = True
a = _job()
T._backfill_missing_wall_heights(a)
w = _walls(a)
check(a.get("_cross_sheet_height_backfilled") is True, "flag ON must run and set idempotency flag")
check(w["Test Cell"] == 1800, "room that already had walls must be untouched")
check(w["Men Toilet"] == round(60 * 9), "Men Toilet walls = perimeter(60) x 9")
check(w["Closet"] == round(40 * 9), "Closet perimeter derived from 10x10 -> 40 LF, x 9")
rms = a["floors"][0]["rooms"]
src = {r["room_name"]: r["dimensions"].get("_wall_height_source") for r in rms}
check(src["Men Toilet"] == "cross_sheet_schedule", "back-filled room must be tagged with provenance")
check(src["Test Cell"] is None, "untouched room must NOT be tagged")
rfis = [x for x in a.get("rfi_items", []) if x.get("source") == "cross_sheet_height_backfill"]
check(len(rfis) == 1, "exactly one confirm-before-bid RFI must be raised")
check("9" in rfis[0]["question"], "RFI must state the height that was applied")

# Idempotent — a second pass changes nothing.
w_before = dict(w)
T._backfill_missing_wall_heights(a)
check(_walls(a) == w_before, "back-fill must be idempotent")

# No confirmed height anywhere -> nothing to apply, leave walls at zero.
a2 = {"project_info": {}, "floors": [{"floor_name": "1", "rooms": [
    {"room_name": "X", "in_scope": True,
     "dimensions": {"ceiling_height_feet": 0, "perimeter_lf": 50, "wall_area_sqft": 0},
     "materials": {"walls": "CMU"}, "elements": {}}]}]}
T._backfill_missing_wall_heights(a2)
check(T._num(a2["floors"][0]["rooms"][0]["dimensions"]["wall_area_sqft"]) == 0,
      "with no confirmed height in the project, walls stay zero (no fabrication)")

T._cfg.CROSS_SHEET_HEIGHT_BACKFILL = False

print("=== PASS ===" if not fails else "=== ISSUES: " + "; ".join(fails) + " ===")
raise SystemExit(1 if fails else 0)
